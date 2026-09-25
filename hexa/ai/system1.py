"""System 1：局内快照建议器（计划 §2，每局 8~11 次，flash 档）

职责边界：层 0 已构造候选池、已定 current_path；System 1 只做
「候选内排序 + 结合语境写理由」，不允许引入候选外的装备名
（输出过 B 层校验，幻觉条目直接丢弃；有效项 <2 降级规则排序）。
"""
from hexa.ai.client import LLMError, parse_json

SYSTEM_PROMPT = """你是英雄联盟海克斯大乱斗的局内快讯参谋。给你当前对局快照、\
预生成的英雄上下文（ChampionContext）和层 0 规则引擎已筛出的候选清单。\
你的唯一任务：在候选内按当下局面排序，并为每个候选写一句理由。

铁律：
1. 只能从《候选清单》里选，一个字都不能改成别的名字；也不得引入清单外的任何装备名。
2. 理由 <=22 字，结合敌方阵容/威胁/已选海克斯/身上小件，说人话。
3. 只输出 JSON，不输出任何其他文字。格式：
{"picks": [{"name": "候选原名", "reason": "一句话理由"}, {"name": "...", "reason": "..."}, {"name": "...", "reason": "..."}]}
picks 按推荐度降序，恰好 3 项（候选不足 3 时给实际数量）。"""


def _fmt_snapshot(my_items, gold, current_path, threat_summary, augments,
                  enemy, first_augment="") -> str:
    lines = [
        f"我英雄已选海克斯：{'、'.join(augments) or '暂无'}",
        f"我身上装备：{'、'.join(my_items) or '无'}（金币 {gold:.0f}）",
        f"当前路线：{current_path.upper() if current_path else '未定（出门装阶段）'}",
        f"敌方阵容与威胁：{threat_summary or '未知'}",
    ]
    if enemy:
        lines.append("敌方明细：" + "；".join(enemy))
    if first_augment:
        lines.append(f"首海克斯联动参考：{first_augment}")
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
                    threat_summary: str, rule_candidates: list) -> list:
    """出门装三选排序。ctx.opening_items 提供组合与场景；失败 → 规则版原序。

    返回 [{"name","reason","combo"}]（与 Candidate 解耦，调用方转换）。
    """
    combos = ctx.opening_for(first_augment) if ctx else []
    if not combos:
        return []
    allowed = set()
    for grp in combos:
        allowed.update(grp.get("combo", []))
    user = "\n\n".join([
        _fmt_snapshot([], 0, "", threat_summary,
                      [first_augment] if first_augment else [], enemy=[]),
        "《出门装候选组合》\n" + "\n".join(
            f"{i + 1}. {'+'.join(g['combo'])}（{g.get('when', '')}）"
            for i, g in enumerate(combos)),
        "选出装顺序建议的三组出门装（每组=组合里的一个主件，按主件名输出 picks）。",
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
                 rule_candidates: list) -> list:
    """下一件三选重排+写理由。返回 [{"name","reason"}]；失败/有效<2 → 规则版。

    rule_candidates: 层 0 Candidate 列表（已带规则理由，作降级文案）。
    """
    if not rule_candidates:
        return []
    allowed = {c.name for c in rule_candidates}
    rule_reason = {c.name: c.reason for c in rule_candidates}
    user = "\n\n".join([
        _fmt_snapshot(my_items, gold, current_path, threat_summary, augments,
                      enemy=[]),
        "《候选清单（只能从中选）》\n" + "\n".join(
            f"- {c.name}：{c.reason}" for c in rule_candidates),
    ])
    try:
        text, _ = client.chat(SYSTEM_PROMPT, user, max_tokens=400)
        picks = _dedup_picks(parse_json(text).get("picks"), allowed)
    except LLMError as e:
        print(f"[WARN] System1 下一件降级规则序: {e}")
        picks = []
    if len(picks) < 2:
        picks = [{"name": c.name, "reason": c.reason} for c in rule_candidates]
    else:
        # 模型没覆盖到的候选用规则理由补齐（保持模型顺序优先）
        named = {p["name"] for p in picks}
        picks += [{"name": c.name, "reason": c.reason}
                  for c in rule_candidates if c.name not in named]
    return picks[:3]


def rank_augments(client, ctx, hero: str, card_names: list, augments_taken: list,
                  my_items: list, threat_summary: str,
                  rule_ranks: list) -> list:
    """海克斯三卡片评级（S/A/B + 一句理由）。

    ctx.augment_affinity 已是 System2 翻译结果——先按位次给基准档，
    System1 用极短 prompt 做语境微调；失败 → 规则版 rule_ranks 原样返回。
    rule_ranks: rules_engine.rank_augments 的输出（含 name/grade/reason）。
    """
    if not ctx or not rule_ranks:
        return rule_ranks
    aff = "\n".join(f"- {a.get('name')}（bias={a.get('bias')}，{a.get('note', '')}）"
                    for a in ctx.augment_affinity[:12])
    user = "\n\n".join([
        f"英雄：{hero}；已拿海克斯：{'、'.join(augments_taken) or '无'}；"
        f"身上装备：{'、'.join(my_items) or '无'}；威胁：{threat_summary or '未知'}",
        f"《该英雄海克斯亲和（ChampionContext）》\n{aff}",
        "《本轮三张卡片（含规则基准档）》\n" + "\n".join(
            f"- {r['name']}：基准 {r['grade']}（{r['reason']}）" for r in rule_ranks),
        "结合当前局面微调档位与理由。只输出 JSON："
        '{"grades":[{"name":"卡片原名","grade":"S|A|B|C","reason":"<=20字"}]}',
    ])
    try:
        text, _ = client.chat(SYSTEM_PROMPT, user, max_tokens=260)
        data = parse_json(text)
    except LLMError as e:
        print(f"[WARN] System1 海克斯评级降级规则版: {e}")
        return rule_ranks
    valid = {r["name"] for r in rule_ranks}
    by_name = {r["name"]: r for r in rule_ranks}
    out = []
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
