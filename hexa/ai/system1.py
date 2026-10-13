"""System 1：局内快照建议器（计划 §2，每局 8~11 次，flash 档）

职责边界：层 0 已构造候选池、已定 current_path；System 1 只做
「候选内排序 + 结合语境写理由」，不允许引入候选外的装备名
（输出过 B 层校验，幻觉条目直接丢弃；有效项 <2 降级规则排序）。
满六神装时层 0 给的是替换候选对（SwapCandidate），本模块自动切到
替换 prompt：在候选对内选优，picks.name 填买件名（与下一件协议同构，
引擎层无需感知替换类型）。

军师上下文（v4.5）：GameSession 把规则层军师提示（阵容缺口/顺逆风/
攒钱决策等）写入模块态（set_coach_context），suggest_next/swap 组装
prompt 时带上，供模型参考；模型可在输出 JSON 里给可选字段 coach
（≤30字）精修军师文案，GameSession 消费（last_coach）后覆盖规则版。
"""
import threading

from hexa.ai.client import LLMError, parse_json
from hexa.core.rules_engine import SwapCandidate

SYSTEM_PROMPT = """你是英雄联盟海克斯大乱斗的局内快讯参谋。给你当前对局快照、\
预生成的英雄上下文（ChampionContext，含 System2 按双方阵容适配的多流派出装方案）\
和层 0 规则引擎已筛出的候选清单。\
你的任务：结合当下局面，在候选内排序/微调档位，并写精炼理由。

铁律：
1. 装备名/海克斯名只能用输入清单里的原名，一个字都不能改，\
也不得引入清单外的任何名字。
2. 理由 <=22 字，结合敌方阵容/威胁/已选海克斯/身上小件，说人话。
3. 只输出 JSON，不输出任何其他文字；具体字段以任务末尾给出的《输出格式》为准。"""

# 三选任务（opening/next/swap）共用的输出协议。原写在 SYSTEM_PROMPT 里，
# 但 rank_augments 任务要求输出 grades 数组——全局写死 picks 格式与之一致性
# 冲突，flash 档模型可能服从 system 格式导致评级精修静默失效，故格式下放
# 到各任务 user prompt 末尾（rank_augments 自带 grades 格式）。
PICKS_FORMAT = """《输出格式》只输出 JSON：
{"picks": [{"name": "候选原名", "reason": "一句话理由"}, \
{"name": "...", "reason": "..."}, {"name": "...", "reason": "..."}], \
"coach": "≤30字军师总提示（可选）"}
picks 按推荐度降序，恰好 3 项（候选不足 3 时给实际数量）。\
coach 可省略；给出时结合《军师提示》修正规则层判断，须 <=30 字。"""

# ── 军师上下文模块态（GameSession ↔ System1 桥接） ────────────────
# engine 层接口（next_candidates）不透传军师数据（本期未改 engine.py），
# 以模块态作桥。写入发生在 GameSession 锁内（建议生成前），读取在后台
# 线程 prompt 组装时——毫秒级窗口内的竞态最坏影响是文案略旧，可接受。
_coach_lock = threading.Lock()
_coach_lines = []   # 最近一次写入的军师提示（规则层文案）
_last_coach = ""    # 最近一次模型输出的 coach 字段（消费后由 clear_coach 清空）


def set_coach_context(lines):
    """GameSession 写入军师上下文（列表，自动截断到 4 条 ×60 字）"""
    global _coach_lines
    with _coach_lock:
        _coach_lines = [str(x).strip()[:60] for x in (lines or [])
                        if str(x).strip()][:4]


def coach_context():
    with _coach_lock:
        return list(_coach_lines)


def _set_last_coach(text):
    global _last_coach
    with _coach_lock:
        _last_coach = text


def last_coach():
    with _coach_lock:
        return _last_coach


def clear_coach():
    _set_last_coach("")


def _clean_coach(text) -> str:
    """模型 coach 字段清洗：去空白、截 30 字（任务口径 ≤30 字）"""
    return str(text or "").strip()[:30]


def _builds_line(ctx, current_path: str) -> str:
    """ChampionContext 出装方案 → 语境段（全部方案，当前路线方案置前）。

    方案 = System2 基于双方阵容预生成的完整流派出装（含情境件，是否放
    破甲/重伤等由预生成侧判断）。模型在此语境上做方案权衡：优先当前
    方案主线，敌情/海克斯指向其他方案时可跨方案取件（allowed 已含
    全方案并集，见 suggest_next 的 extra_allowed）。
    """
    builds = getattr(ctx, "builds", None) if ctx else None
    if not builds:
        return ""
    cur = (current_path or "").lower()
    ordered = sorted(builds, key=lambda b: 0 if (b.get("path") or "").lower() == cur
                     else 1)  # stable：同路线方案保持预生成序
    lines = []
    for b in ordered:
        items = "→".join(b.get("items") or [])
        if not items:
            continue
        note = b.get("note") or ""
        lines.append(f"{b.get('name') or '方案'}"
                     f"（{(b.get('path') or '?').upper()}）：{items}"
                     + (f"——{note}" if note else ""))
    if not lines:
        return ""
    return ("《本局适配出装方案（优先当前方案，敌情/已选海克斯指向"
            "其他方案时可跨方案取件）》\n" + "\n".join(lines))


def _cand_line(c) -> str:
    """候选行带价格/金币标注。System1 手里有金币却不知道单价，无法判断
    "攒钱 vs 买小件"（军师提示里恰有攒钱文案，等于让它对着提示猜）；
    afford_note 由 GameSession 建议定型后按序累计填写，AI 线程组装 prompt
    时可能尚未填上（良性竞态，缺省只少标注），价格来自规则层恒在。"""
    price = f"{c.price}G" if getattr(c, "price", 0) else ""
    afford = getattr(c, "afford_note", "") or ""
    meta = "，".join(x for x in (price, afford) if x)
    return f"- {c.name}" + (f"（{meta}）" if meta else "") + f"：{c.reason}"


def _fmt_snapshot(my_items, gold, current_path, threat_summary, augments,
                  enemy, first_augment="", coach=None, allies=None) -> str:
    lines = [
        f"我英雄已选海克斯：{'、'.join(augments) or '暂无'}",
        f"我身上装备：{'、'.join(my_items) or '无'}（金币 {gold:.0f}）",
        f"当前路线：{current_path.upper() if current_path else '未定（出门装阶段）'}",
        f"敌方阵容与威胁：{threat_summary or '未知'}",
    ]
    if enemy:
        lines.append("敌方明细：" + "；".join(enemy))
    # 队友出装摘要（快照直出，含我；判断"谁在游走/谁缺装"归模型）
    if allies:
        lines.append("《队友装备摘要（快照直出）》\n" + "\n".join(allies))
    if first_augment:
        lines.append(f"首海克斯联动参考：{first_augment}")
    # 军师上下文（v4.5）：coach=None 表示读取模块态（GameSession 写入）；
    # 显式传 [] 表示本调用不带军师（出门装阶段等）
    if coach is None:
        coach = coach_context()
    if coach:
        lines.append("《军师提示（规则层判断，供参考/纠正）》：" + "；".join(coach))
    return "\n".join(lines)


def _dedup_picks(picks: list, allowed: set) -> list:
    """防幻觉过滤：只留候选内名字，保持模型给出的顺序"""
    out, seen = [], set()
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if name in allowed and name not in seen:
            seen.add(name)
            out.append({"name": name, "reason": str(p.get("reason") or "")[:24]})
    return out


def suggest_opening(client, ctx, hero: str, first_augment: str,
                    threat_summary: str, rule_candidates: list,
                    enemy=None) -> list:
    """出门装三选排序。ctx.opening_items 提供组合与场景；失败 → 规则版原序。

    返回 [{"name","reason","combo"}]（与 Candidate 解耦，调用方转换）。
    enemy: 敌方明细行 ["名(伤害)"]（engine 保留的 start_game 名单+画像）。
    """
    combos = ctx.opening_for(first_augment) if ctx else []
    if not combos:
        return []
    allowed = set()
    for grp in combos:
        allowed.update(grp.get("combo", []))
    user = "\n\n".join([
        _fmt_snapshot([], 0, "", threat_summary,
                      [first_augment] if first_augment else [],
                      enemy=enemy or [],
                      coach=[]),  # 出门装阶段不带军师（阵容类提示尚未生成）
        "《出门装候选组合》\n" + "\n".join(
            f"{i + 1}. {'+'.join(g['combo'])}（{g.get('when', '')}）"
            for i, g in enumerate(combos)),
        "选出装顺序建议的三组出门装（每组=组合里的一个主件，按主件名输出 picks）。",
        PICKS_FORMAT,
    ])
    try:
        text, _ = client.chat(SYSTEM_PROMPT, user, max_tokens=300)
        picks = _dedup_picks(parse_json(text).get("picks"), allowed)
    except LLMError as e:
        print(f"[WARN] System1 出门装降级规则序: {e}")
        picks = []
    out = []
    for grp in combos:
        main = grp["combo"][0]
        hit = next((p for p in picks if p["name"] == main), None)
        reason = hit["reason"] if hit else grp.get("when") or "OP.GG 高频出门"
        out.append({"name": main, "reason": reason, "combo": grp["combo"]})
    # 按模型给出的排序稳定重排（未命中的按原序排后）
    rank = {p["name"]: i for i, p in enumerate(picks)}
    out.sort(key=lambda x: rank.get(x["name"], len(rank)))
    return out[:3]


def suggest_next(client, ctx, my_items: list, gold: float, current_path: str,
                 threat_summary: str, augments: list,
                 rule_candidates: list, coach_lines=None, enemy=None,
                 allies=None, extra_allowed: list = None) -> list:
    """下一件三选重排+写理由。返回 [{"name","reason"}]；失败/有效<2 → 规则版。

    rule_candidates: 层 0 Candidate 列表（已带规则理由，作降级文案）。
    满六神装时层 0 传 SwapCandidate 替换对 → 自动切 suggest_swap。
    coach_lines: 军师上下文（None=读模块态；GameSession 已写入）。
    enemy: 敌方明细行 ["名(伤害,Role)[ 召唤师技能:X+Y]"]。ctx：出装方案段进 prompt。
    allies: 队友出装摘要行 ["英雄: 装备1、装备2"]（快照直出，含我）。
    extra_allowed: 跨方案权衡白名单（全方案池并集，engine 已过校验/
    地图可买性/杂项过滤）——模型可从其他方案取件，防幻觉边界不变。
    模型可选输出 coach 字段（≤30字）→ 模块态 last_coach，由
    GameSession._apply_ai_coach 消费覆盖规则军师文案。
    """
    if not rule_candidates:
        return []
    if isinstance(rule_candidates[0], SwapCandidate):
        return suggest_swap(client, ctx, my_items, gold, current_path,
                            threat_summary, augments, rule_candidates,
                            coach_lines=coach_lines, enemy=enemy,
                            allies=allies)
    allowed = {c.name for c in rule_candidates}
    allowed |= {n for n in (extra_allowed or []) if isinstance(n, str)}
    parts = [
        _fmt_snapshot(my_items, gold, current_path, threat_summary, augments,
                      enemy=enemy or [], coach=coach_lines, allies=allies),
        "《候选清单（只能从中选）》\n" + "\n".join(
            _cand_line(c) for c in rule_candidates),
    ]
    route = _builds_line(ctx, current_path)
    if route:
        parts.append(route)
    parts.append(PICKS_FORMAT)
    user = "\n\n".join(parts)
    try:
        text, _ = client.chat(SYSTEM_PROMPT, user, max_tokens=400)
        data = parse_json(text)
        picks = _dedup_picks(data.get("picks"), allowed)
        _set_last_coach(_clean_coach(data.get("coach")))
    except LLMError as e:
        print(f"[WARN] System1 下一件降级规则序: {e}")
        picks = []
        _set_last_coach("")
    if len(picks) < 2:
        picks = [{"name": c.name, "reason": c.reason} for c in rule_candidates]
    else:
        # 模型没覆盖到的候选用规则理由补齐（保持模型顺序优先）
        named = {p["name"] for p in picks}
        picks += [{"name": c.name, "reason": c.reason}
                  for c in rule_candidates if c.name not in named]
    return picks[:3]


def suggest_swap(client, ctx, my_items: list, gold: float, current_path: str,
                 threat_summary: str, augments: list,
                 swap_candidates: list, coach_lines=None, enemy=None,
                 allies=None) -> list:
    """满六神装替换对选优。返回 [{"name","reason"}]（name=买件名，
    与 suggest_next 同构，引擎层按 Candidate 协议转换）；失败/有效<2 → 规则版原序。

    铁律延续：只能从候选对的买件里选（卖件由对子绑定，不存在单独选），
    理由 <=22 字。净差已含在规则理由/对子事实里，模型只判断"哪对最值得"。
    coach_lines 语义同 suggest_next；enemy/allies/ctx 同 suggest_next。
    """
    if not swap_candidates:
        return []
    allowed = {c.buy for c in swap_candidates}
    parts = [
        _fmt_snapshot(my_items, gold, current_path, threat_summary, augments,
                      enemy=enemy or [], coach=coach_lines, allies=allies),
        "已满六神装，以下是《替换候选对（只能从中选，name 填买件名）》：",
    ] + [
        f"- 卖[{c.sell}] → 买[{c.buy}]（卖回{c.sell_price}G / 买价{c.price}G）：{c.reason}"
        for c in swap_candidates
    ]
    route = _builds_line(ctx, current_path)
    if route:
        parts.append(route)
    parts += [
        "选出当下最值得执行的替换对（1~3 个，按推荐度降序），"
        "picks 的 name 必须是上面的买件名原文。",
        PICKS_FORMAT,
    ]
    user = "\n\n".join(parts)
    try:
        # 输出与下一件同规模（3 项 × 名字+短理由），max_tokens 相应收窄
        text, _ = client.chat(SYSTEM_PROMPT, user, max_tokens=300)
        data = parse_json(text)
        picks = _dedup_picks(data.get("picks"), allowed)
        _set_last_coach(_clean_coach(data.get("coach")))
    except LLMError as e:
        print(f"[WARN] System1 替换建议降级规则序: {e}")
        picks = []
        _set_last_coach("")
    if len(picks) < 2:
        picks = [{"name": c.buy, "reason": c.reason} for c in swap_candidates]
    else:
        named = {p["name"] for p in picks}
        picks += [{"name": c.buy, "reason": c.reason}
                  for c in swap_candidates if c.buy not in named]
    return picks[:3]


def rank_augments(client, ctx, hero: str, card_names: list, augments_taken: list,
                  my_items: list, threat_summary: str,
                  rule_ranks: list, enemy=None, descriptions=None,
                  allies=None) -> list:
    """海克斯三卡片评级（S/A/B + 一句理由）。

    ctx.augment_affinity 已是 System2 翻译结果——先按位次给基准档，
    System1 用极短 prompt 做语境微调；失败 → 规则版 rule_ranks 原样返回。
    rule_ranks: rules_engine.rank_augments 的输出（含 name/grade/reason）。
    enemy: 敌方明细行（评级也吃阵容语境，如全 AD 局抗性海克斯升值）。
    allies: 队友出装摘要行（同 enemy 参数模式，快照直出——队友成型度/
    装备方向是评级语境事实，怎么用归模型）。
    descriptions: {卡名: 卡面原文描述}（20261013 描述自举：OCR 直出的
    事实层——标签/亲和是离线压缩结论，真机实锤 大法师 被标 ap/法穿/爆发
    而原文是技能花费返还；prompt 明示冲突时以描述为准）。
    输出格式（grades）只在 user prompt 声明——SYSTEM_PROMPT 已不再全局
    写死 picks 格式，二者曾互斥导致评级精修静默回落规则版。
    """
    if not ctx or not rule_ranks:
        return rule_ranks
    try:
        # prompt 组装同在 try 内：ctx.augment_affinity 等数据形状跑偏时
        # 取值路径会抛 AttributeError，漏到引擎层只剩一行 WARN，评级
        # 精修静默失败（烧掉一次预算占位）——与 system2.generate 同口径收编。
        aff = "\n".join(f"- {a.get('name')}（bias={a.get('bias')}，{a.get('note', '')}）"
                        for a in ctx.augment_affinity[:12])
        head = (f"英雄：{hero}；已拿海克斯：{'、'.join(augments_taken) or '无'}；"
                f"身上装备：{'、'.join(my_items) or '无'}；威胁：{threat_summary or '未知'}")
        if enemy:
            head += f"；敌方：{'、'.join(enemy)}"
        parts = [
            head,
            f"《该英雄海克斯亲和（ChampionContext）》\n{aff}",
        ]
        # 队友装备摘要段（快照直出，有才加）
        if allies:
            parts.append("《队友装备摘要（快照直出）》\n" + "\n".join(allies))
        # 原文描述段（有才加）：事实直出，冲突时压过压缩结论
        desc_lines = [f"- {r['name']}：{descriptions[r['name']]}"
                      for r in rule_ranks if descriptions.get(r["name"])] \
            if descriptions else []
        if desc_lines:
            parts.append("《本轮卡片原文描述（卡面 OCR 直出；与亲和标签冲突时"
                         "以描述为准——标签可能过时或误标）》\n" + "\n".join(desc_lines))
        parts += [
            "《本轮三张卡片（含规则基准档）》\n" + "\n".join(
                f"- {r['name']}：基准 {r['grade']}（{r['reason']}）" for r in rule_ranks),
            "结合当前局面微调档位与理由。只输出 JSON："
            '{"grades":[{"name":"卡片原名","grade":"S|A|B|C","reason":"<=20字"}]}',
        ]
        user = "\n\n".join(parts)
        text, _ = client.chat(SYSTEM_PROMPT, user, max_tokens=260)
        data = parse_json(text)
        valid = {r["name"] for r in rule_ranks}
        by_name = {r["name"]: r for r in rule_ranks}
        out = []
        # 模型把 grades 返回成字典而非数组时，迭代出的是键（字符串），
        # (g or {}).get 会抛 AttributeError——同样必须走降级而非外抛。
        for g in (data.get("grades") or []):
            name = (g or {}).get("name")
            if name in valid:
                base = by_name[name]
                grade = str((g or {}).get("grade") or base["grade"]).upper()[:1]
                if grade not in ("S", "A", "B", "C"):
                    grade = base["grade"]
                out.append({"name": name, "grade": grade,
                            "reason": str((g or {}).get("reason") or base["reason"])[:22]})
        # 未被模型覆盖的卡片保留规则基准
        named = {g["name"] for g in out}
        out += [r for r in rule_ranks if r["name"] not in named]
        return out
    except (LLMError, AttributeError, TypeError) as e:
        print(f"[WARN] System1 海克斯评级降级规则版: {e}")
        return rule_ranks
