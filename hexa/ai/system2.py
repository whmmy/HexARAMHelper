"""System 2：加载期 ChampionContext 生成（计划 §2，每局 1 次）

输入：英雄 + 双方阵容（含敌方伤害构成）+ OP.GG builds + 海克斯先验 top20
  （逐条带 KB 语义标签：path_bias/tags/升级型——新模式海克斯基座模型多无
  先验，喂事实把"评亲和"从考幻觉变成按事实+阵容做语境排序）
输出：ChampionContext（海克斯亲和/出门装方案/多流派出装方案/海克斯→出装联动）
质量链：JSON 解析 → schema 校验 → 装备/海克斯名过 B 层校验（防幻觉）
      → 失败重试 1 次（附上次失败原因）→ 再失败降级规则版 Context（离线兜底）。
"""
import threading

from hexa.ai.client import LLMError, parse_json
from hexa.core.rules_engine import _is_fullstat_item

SYSTEM_PROMPT = """你是英雄联盟「海克斯大乱斗（ARAM Mayhem）」的版本军师。\
你的任务：为指定英雄预生成一局的知识上下文（ChampionContext），供局内快速裁决使用。

铁律：
1. 海克斯名必须严格使用《海克斯候选名单》中的原名；装备名必须是《OP.GG 数据》中的原名或英雄联盟国服真实装备名（输出会逐名校验，无效名直接丢弃），禁止编造、改写、缩写。
2. 出装方案以《OP.GG 数据》为核心锚点：每个方案的核心三件取自 OP.GG 高频组合；在此基础上结合双方阵容构成做适配性权衡并写进 note——例如敌方坦克多/堆护甲时考虑破甲或百分比伤害件、敌方堆魔抗时考虑法穿件、敌方治疗体系强时考虑重伤件、敌方爆发/刺客威胁高时考虑保命件。针对方向由你自行判断，方案里不需要的就不加。
3. 方案是"流派"而非伤害类型：同一伤害类型可以有多个流派方案（如 AP 的灼烧消耗流/金身容错流/爆发流），也要覆盖其他伤害类型的方案；每个方案是一套完整出装序列，供局内按战局切换。
4. "升级型"海克斯的决策语义是"升级哪件已购装备"，写亲和 note 时据此与普通海克斯区分。
5. 只输出 JSON，不输出任何其他文字。

输出 JSON schema（字段缺一不可）：
{
 "briefing":         "<=40字开局定调",                                                      // 一句话概括本局要点：敌方构成威胁/我方阵容缺口/出装大方向，供局内常驻展示
 "augment_affinity": [{"name":"海克斯名","bias":"ad|ap|tank|none","note":"<=18字"}],   // 12~20 个，按该英雄适配度降序
 "opening_items":    [{"combo":["装备名"],"when":"<=10字适用场景"}],                    // 恰好 3 组
 "builds":           [{"name":"<=8字流派名","path":"ap|ad|tank","items":["装备名"],      // 2~4 个方案
                       "note":"<=24字本局适配理由"}],
 "skill_order":      "主X副Y",
 "augment_item_links":[{"augment":"海克斯名","shift":"出装影响<=20字","reason":"<=24字"}] // 0~5 条
}"""


def _fmt_builds(kb, hero: str) -> str:
    b = kb.build_for(hero)
    if not b:
        return "（该英雄暂无 OP.GG builds 数据，请基于你的知识谨慎给出）"
    lines = [f"版本 {b.get('patch')}（抓取 {b.get('fetched_at')}）："]
    if b.get("core_builds"):
        lines.append("高频核心出装方案：" +
                     "；".join("+".join(c) for c in b["core_builds"][:4]))
    if b.get("starter_items"):
        lines.append("出门装：" + "；".join("+".join(c) for c in b["starter_items"][:2]))
    if b.get("boots"):
        lines.append("鞋子：" + "/".join(c[0] for c in b["boots"][:2] if c))
    sa = kb.skill_advice(hero)
    if sa["priority"]:
        lines.append(f"加点：{sa['priority']}")
    return "\n".join(lines)


def _fmt_augments(kb, hero: str, n: int = 20) -> str:
    """先验 top20 逐条带 KB 语义标签（bias/tags/升级型）。

    海克斯大乱斗是新模式，基座模型对很多海克斯名零先验——只给"名(档位)"
    等于考幻觉；augments.json 的翻译事实喂进来，亲和评级才有依据。
    20261013 描述自举贯通：augment_descs 的卡面 OCR 原文一并直出——
    标签是离线压缩结论（可能过时/误标，实锤 大法师 被标 ap/法穿/爆发
    而原文是技能花费返还），带原文的条目以原文为准，与 System1 评级
    prompt 同口径。截 60 字防 prompt 膨胀（top20 × 60 ≈ 1.2k 上限）。
    """
    top = kb.top_augments(hero, n)
    if not top:
        return "（该英雄暂无海克斯先验数据）"
    infos = kb.hero_augments.get(hero, {})
    parts = []
    for name in top:
        if name not in infos:
            continue
        tag = kb.augment_tag_info(name) or {}
        bits = [infos[name]["tier"], f"bias={tag.get('path_bias') or 'none'}"]
        tags = tag.get("tags") or []
        if tags:
            bits.append("tags:" + "/".join(tags))
        if tag.get("is_upgrade"):
            bits.append("升级型")
        desc = (kb.augment_desc(name) or "").strip()
        if desc:
            bits.append("原文:" + desc[:60])
        parts.append(f"{name}（{'，'.join(bits)}）")
    return "；".join(parts) or "（无）"


def _fmt_teams(ally: list, enemy: list) -> str:
    return f"我方阵容：{'、'.join(ally) or '未知'}\n敌方阵容：{'、'.join(enemy) or '未知'}"


def _fmt_enemy_comp(kb, enemy: list) -> str:
    """敌方构成一行（champ_damage_profile 现成画像，数据直出）。

    每敌喂 damage + Role 标签原文（如 Tank/Support/Mage），治疗系、
    坦克、刺客威胁的识别与针对方向全部交给模型判断——代码侧不写
    任何语义表（泰坦案口径推广：数据出事实，模型出判断）。
    """
    bits = []
    for name in enemy or []:
        info = kb.champ_profile.get(name, {})
        dmg = info.get("damage")
        tags = info.get("tags") or []
        if not dmg and not tags:
            continue
        role = "/".join(tags) if tags else "?"
        bits.append(f"{name}({dmg or '?'},{role})")
    return "敌方构成：" + "；".join(bits) if bits else ""


class ChampionContext:
    """System2 产物（内存缓存，整局有效；schema 已校验、名字已过 B 层）"""

    def __init__(self, data: dict, hero: str, source: str = "ai", meta: dict = None):
        self.raw = data
        self.hero = hero
        self.source = source          # ai / rule（降级）
        self.meta = meta or {}
        # 开局定调（提示语优化案 20261013）：<=40 字一句话，KIND_CTX 就绪
        # 时由 GameSession 推进军师列表常驻展示；缺失容忍为空（旧 mock/旧
        # 模型输出不带此字段不炸）
        self.briefing = str(data.get("briefing") or "").strip()
        self.augment_affinity = data.get("augment_affinity", [])
        self.opening_items = data.get("opening_items", [])
        self.builds = data.get("builds", [])          # 出装方案列表（流派制）
        self.skill_order = data.get("skill_order", "")
        self.augment_item_links = data.get("augment_item_links", [])

    # ── 查询接口（System1 / GameSession 消费） ────────────────────

    def affinity_rank(self, name: str) -> int:
        """海克斯在亲和表中位次（1 起；榜外返回大数）"""
        for i, a in enumerate(self.augment_affinity):
            if a.get("name") == name:
                return i + 1
        return 999

    def augment_bias(self, name: str) -> str:
        for a in self.augment_affinity:
            if a.get("name") == name:
                return a.get("bias") or "none"
        return "none"

    def opening_for(self, first_augment: str = None) -> list:
        """出门装 3 组（联动优先置顶）"""
        combos = []
        if first_augment:
            for link in self.augment_item_links:
                if link.get("augment") == first_augment:
                    for grp in self.opening_items:
                        text = "".join(grp.get("combo", [])) + grp.get("when", "")
                        # "心之刚"不在 kb.augment_names 名单内，永不可能成为
                        # 合法首个海克斯，关键词只留通用"联动"与实际海克斯名
                        if any(k in text for k in ("联动", first_augment)):
                            combos.insert(0, grp)
                            break
        out = list(combos)
        for grp in self.opening_items:
            if grp not in out:
                out.append(grp)
        return out[:3]

    def pool_for(self, path: str) -> list:
        """path 匹配方案 items 并集（第一匹配方案序优先，其余方案独有件
        按序追加）——规则底线 build_sequence 的主池数据源。无匹配 → []。"""
        path = (path or "").lower()
        out, seen = [], set()
        for b in self.builds:
            if (b.get("path") or "").lower() != path:
                continue
            for n in (b.get("items") or []):
                if n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    def all_pools(self) -> list:
        """全方案 items 并集（跨方案权衡白名单的数据源）"""
        out, seen = [], set()
        for b in self.builds:
            for n in (b.get("items") or []):
                if n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    def skill_order_text(self) -> str:
        """加点一行（顶层单值）"""
        return self.skill_order or ""


# ── 校验（防幻觉：名字必须过 B 层） ──────────────────────────────────

def validate_and_clean(data: dict, kb, hero: str) -> dict:
    """schema 校验 + 名字过滤。关键结构不完整 → raise LLMError（上层重试/降级）"""
    if not isinstance(data, dict):
        raise LLMError("Context 不是 JSON 对象")

    affinity = []
    for a in (data.get("augment_affinity") or [])[:20]:
        name = (a or {}).get("name") or ""
        if kb.valid_augment(name):
            affinity.append({"name": name,
                             "bias": (a.get("bias") or "none")[:4],
                             "note": str(a.get("note") or "")[:24]})
    if len(affinity) < 8:
        raise LLMError(f"海克斯亲和有效项不足（{len(affinity)}/8）")

    openings = []
    for grp in (data.get("opening_items") or [])[:4]:
        combo = [n for n in ((grp or {}).get("combo") or [])
                 if kb.valid_item(n)]
        if combo and len(openings) < 3:
            openings.append({"combo": combo,
                             "when": str((grp or {}).get("when") or "标准")[:12]})
    if len(openings) < 2:
        raise LLMError(f"出门装有效组不足（{len(openings)}/2）")

    builds = []
    seen_keys = set()
    for b in (data.get("builds") or [])[:4]:
        if not isinstance(b, dict):
            continue
        path = str(b.get("path") or "").lower()
        if path not in ("ap", "ad", "tank"):
            continue
        items = []
        for n in (b.get("items") or []):
            if (isinstance(n, str) and kb.valid_item(n) and n not in items
                    and not _is_fullstat_item(kb, n)):
                items.append(n)
        if len(items) < 3:
            continue  # 流派方案至少要成线（核心+后续），碎件组不收
        key = (path, tuple(items))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        builds.append({"name": str(b.get("name") or "")[:8] or "流派",
                       "path": path, "items": items[:8],
                       "note": str(b.get("note") or "")[:24]})
    if not builds:
        raise LLMError("builds 无有效方案")
    skill_order = str(data.get("skill_order") or "")[:16]

    links = []
    for l in (data.get("augment_item_links") or [])[:6]:
        aug = (l or {}).get("augment") or ""
        if kb.valid_augment(aug):
            links.append({"augment": aug,
                          "shift": str((l or {}).get("shift") or "")[:24],
                          "reason": str((l or {}).get("reason") or "")[:28]})

    # component_rules 已从 schema 移除：全项目零消费（小件持有决策由规则层
    # component_advice/saving_advice 承担），每局白烧 ~150 输出 token。
    # briefing 开局定调：超长截断 40 字（防大模型啰嗦把 overlay 军师行撑爆），
    # 缺失容忍为空串——它是增强展示不是结构骨架，不参与"结构不完整→重试"判定。
    briefing = str(data.get("briefing") or "").strip()[:40]
    return {"briefing": briefing,
            "augment_affinity": affinity, "opening_items": openings,
            "builds": builds, "skill_order": skill_order,
            "augment_item_links": links}


# ── 规则版 Context（离线兜底，全部确定性） ───────────────────────────

def rule_context(kb, hero: str, rules=None, ally: list = None,
                 enemy: list = None) -> dict:
    top = kb.top_augments(hero, 16)
    affinity = [{"name": n, "bias": (kb.augment_tag_info(n).get("path_bias") or "none"),
                 "note": "先验榜第" + str(i + 1)}
                for i, n in enumerate(top)]
    openings = [{"combo": list(c), "when": f"OP.GG 高频{i + 1}"}
                for i, c in enumerate(kb.starter_combos(hero)[:3])]
    if not openings:
        openings = [{"combo": ["多兰之刃"], "when": "标准"},
                    {"combo": ["多兰之戒"], "when": "AP"},
                    {"combo": ["红水晶"], "when": "防守"}]
    profile = kb.champ_profile.get(hero, {})
    # 方案 path 须与 infer_current_path 的取值域（ap/ad/tank）对齐：
    # OP.GG core_builds 每组组合按组内件 bias 投票定 path（坦克英雄
    # damage=ap 的组合组内坦件占多 → 投 tank，降级局 current_path=tank
    # 时 pool_for(tank) 仍能查到）；同票/零票回落英雄画像。
    fallback = ("tank" if profile.get("is_tank")
                else (profile.get("damage") or "ad"))
    builds = []
    b = kb.build_for(hero)
    for i, combo in enumerate((b.get("core_builds") if b else []) or []):
        items = [n for n in combo if kb.valid_item(n)]
        if len(items) < 3:
            continue
        votes = {"ap": 0, "ad": 0, "tank": 0}
        for n in items:
            bias = kb.item_path_bias(n)
            if bias:
                votes[bias] += 1
        path = (max(votes, key=votes.get)
                if any(votes.values()) else fallback)
        builds.append({"name": f"OP.GG高频{i + 1}", "path": path,
                       "items": items[:8], "note": "OP.GG 高频组合"})
        if len(builds) >= 3:
            break
    if not builds:
        main_items = kb.core_item_pool(hero)[:8]
        if main_items:
            builds.append({"name": "主线", "path": fallback,
                           "items": main_items, "note": "OP.GG 主线"})
    # 联动不硬编码海克斯名（曾写死"心之刚"，不在 kb.augment_names 229 名单
    # 内——AI 路径有 validate_and_clean 过滤、规则降级路径没有，恰在最需要
    # 兜底时产出无效名）。改取知识层 augments.json 的 opening_shift 事实，
    # 名字过 kb.valid_augment、装备过 kb.valid_item（与 AI 路径同口径），
    # 英雄先验榜内的排前面提相关性，至多 5 条（与 schema 0~5 一致）。
    ranked = list(top) + [n for n in kb.augment_tags if n not in top]
    links = []
    for name in ranked:
        info = kb.augment_tag_info(name) or {}
        shift = str(info.get("opening_shift") or "").strip()
        if not (shift and kb.valid_augment(name) and kb.valid_item(shift)):
            continue
        links.append({"augment": name,
                      "shift": f"出门装→{shift}"[:24],
                      "reason": str(info.get("opening_shift_reason") or "")[:28]})
        if len(links) >= 5:
            break
    return {"briefing": _rule_briefing(kb, rules, hero, ally, enemy),
            "augment_affinity": affinity, "opening_items": openings,
            "builds": builds, "skill_order": kb.skill_advice(hero)["priority"],
            "augment_item_links": links}


def _rule_briefing(kb, rules, hero: str, ally: list, enemy: list) -> str:
    """降级版开局定调：威胁画像 / 阵容缺口两条现成事实句拼接（<=40 字）。

    只组事实不组结论（数据直出口径，同 _fmt_enemy_comp）：敌方构成取
    threat_profile.summary()（法伤×N | 坦克×N | 治疗:…），我方缺口取
    ally_gap().summary()（缺前排/缺AP…）。名单不全（<5 人/空名单）时
    相应侧落"暂无数据"，按空跳过不编造；两者都缺 → 空串（KIND_CTX
    消费侧对空 briefing 不推送）。
    """
    if rules is None:
        return ""
    ally5 = list(dict.fromkeys(
        [c for c in (ally or []) if c] + ([hero] if hero else [])))
    enemy5 = [c for c in (enemy or []) if c]
    if not ally5 and not enemy5:
        return ""
    bits = []
    if enemy5:
        ts = rules.threat_profile(enemy5).summary()
        if ts and ts != "暂无数据":
            bits.append("敌方" + ts)
    if len(ally5) >= 5:
        gs = rules.ally_gap(ally5, hero).summary()
        if gs and gs != "暂无数据":
            bits.append("我方" + gs)
    return "；".join(bits)[:40]


# ── 生成入口 ─────────────────────────────────────────────────────────

def generate(kb, rules, client, hero: str, ally: list, enemy: list,
             data_version: str = "") -> ChampionContext:
    """System2 一次调用生成 ChampionContext；失败重试 1 次；再失败降级规则版。

    重试时附上次校验失败原因（原样重发等于再掷一次同样的骰子）。
    返回的 ChampionContext.source 标明 'ai' 或 'rule'（UI 据此显示模式）。
    """
    last_err = None
    for attempt in range(2):  # 失败重试 1 次
        try:
            # prompt 组装（_fmt_* 读 KB 数据）同在 try 内：知识库数据形状
            # 变动抛 AttributeError/TypeError 时同样必须走重试/降级——否则
            # 异常直接炸出 engine.gen 后台线程，cache.set 永不执行，
            # pending 永久挂起、本局后续所有精修静默跳过。
            comp = _fmt_enemy_comp(kb, enemy)
            head = f"英雄：{hero}\n{_fmt_teams(ally, enemy)}"
            if comp:
                head += f"\n{comp}"
            user = "\n\n".join([
                head,
                f"《OP.GG 数据》\n{_fmt_builds(kb, hero)}",
                "《海克斯候选名单》（该英雄先验 top20；含档位与语义标签："
                "bias=路线倾向，tags=玩法标签，升级型=升级已购装备；"
                "带「原文」的是卡面 OCR 直出事实，与标签冲突时以原文为准）\n"
                f"{_fmt_augments(kb, hero)}",
                f"数据版本：{data_version or kb.dd_version}",
                "请输出 ChampionContext JSON。",
            ])
            if attempt and last_err is not None:
                user += (f"\n\n注意：上一次输出未通过校验（{last_err}）。"
                         "请严格按 schema 输出，名字必须逐字取自名单。")
            text, meta = client.chat(SYSTEM_PROMPT, user, max_tokens=2000,
                             purpose="ctx")
            data = validate_and_clean(parse_json(text), kb, hero)
            return ChampionContext(data, hero, source="ai", meta=meta)
        except (LLMError, AttributeError, TypeError) as e:
            # LLMError=质量链契约异常（client.chat 已把网络/SDK 失败收敛为其，
            # parse_json/validate_and_clean 亦只 raise 它）；AttributeError/
            # TypeError=模型输出形状跑偏（如 bias 给数字、affinity 给字符串）
            # 时 validate_and_clean 的取值路径会抛，同样必须走重试/降级——
            # engine.gen 后台线程无兜底 except，漏掉即静默挂起 pending。
            last_err = e
    print(f"[WARN] System2 降级规则版 Context: {last_err}")
    return ChampionContext(rule_context(kb, hero, rules, ally=ally, enemy=enemy),
                           hero, source="rule", meta={"error": str(last_err)[:120]})


class ContextCache:
    """整局缓存：按 (hero, ally, enemy) 阵容键控，线程安全（System2 在后台线程生成）。

    选人期预生成（engine.prepare_game）→ 开局同键命中复用（零新增 LLM 调用）；
    骰子换人/阵容变化 → 键失配即视为无效（开局照旧重派，绝不复用错阵容的
    方案池）。不带键的 get()/pending 为兼容口径：只看"当前缓存"本身，键
    匹配判断由 engine 侧经 ready_for/pending_for 完成。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._ctx = None
        self._key = None             # _ctx 对应的阵容键
        self._pending = False
        self._pending_key = None     # 在途生成对应的阵容键

    @staticmethod
    def make_key(hero: str, ally: list, enemy: list) -> tuple:
        """阵容键：英雄 + 己方名单（剔除英雄并排序去重）+ 敌方名单（排序去重）。

        己方剔除英雄本人：同一局的三个派发点 ally 口径不一（LCU 选人/
        GameFlow 含我、Live 快照兜底不含我），归一后同局必同键；排序去重
        防名单顺序抖动造成假性失配（失配的代价只是开局重派一次，不会
        复用错阵容的上下文，宁失配勿错配）。"""
        hero = str(hero or "")
        mates = tuple(sorted({str(a) for a in (ally or []) if a} - {hero}))
        foes = tuple(sorted({str(e) for e in (enemy or []) if e}))
        return (hero, mates, foes)

    def set(self, ctx: ChampionContext, key: tuple = None):
        with self._lock:
            self._ctx = ctx
            self._key = key
            self._pending = False
            self._pending_key = None

    def get(self, key: tuple = None) -> ChampionContext | None:
        """取当前上下文；给定 key 时键失配返回 None（防跨局/跨阵容串用）"""
        with self._lock:
            if key is not None and self._key != key:
                return None
            return self._ctx

    def ready_for(self, key: tuple) -> bool:
        """键匹配且已生成完毕（开局复用的判据）"""
        with self._lock:
            return (self._ctx is not None and self._key == key
                    and not self._pending)

    def pending_for(self, key: tuple) -> bool:
        """键匹配的生成在途（开局等待其落位、不重复派发的判据）"""
        with self._lock:
            return self._pending and self._pending_key == key

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending

    def invalidate(self):
        with self._lock:
            self._ctx = None
            self._key = None

    def start_pending(self, key: tuple = None):
        with self._lock:
            self._ctx = None
            self._key = None
            self._pending = True
            self._pending_key = key

    def fail(self):
        """生成失败落位：清 pending（ctx_source 从"生成中"转"生成失败"）。

        看门狗消费（engine._ctx_ready）：System2 长时间不返回时放行——
        否则浮窗永久"AI 生成中…"且用户无从判断（2026-09-30 牛头案：
        qwen3 思考挂死 12 分钟，ctx_source 全局卡"生成中"）。"""
        with self._lock:
            self._ctx = None
            self._key = None
            self._pending = False
            self._pending_key = None
