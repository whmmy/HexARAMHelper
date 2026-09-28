"""System 2：加载期 ChampionContext 生成（计划 §2，每局 1 次）

输入：英雄 + 双方阵容（含敌方伤害构成）+ OP.GG builds + 海克斯先验 top20
  （逐条带 KB 语义标签：path_bias/tags/升级型——新模式海克斯基座模型多无
  先验，喂事实把"评亲和"从考幻觉变成按事实+阵容做语境排序）
输出：ChampionContext（海克斯亲和/出门装方案/内部多路线/海克斯→出装联动）
质量链：JSON 解析 → schema 校验 → 装备/海克斯名过 B 层校验（防幻觉）
      → 失败重试 1 次（附上次失败原因）→ 再失败降级规则版 Context（离线兜底）。
"""
import threading

from hexa.ai.client import LLMError, parse_json

SYSTEM_PROMPT = """你是英雄联盟「海克斯大乱斗（ARAM Mayhem）」的版本军师。\
你的任务：为指定英雄预生成一局的知识上下文（ChampionContext），供局内快速裁决使用。

铁律：
1. 输出中出现的装备名/海克斯名必须严格使用我提供的《候选装备名单》和《海克斯候选名单》中的原名，禁止编造、改写、缩写。
2. 版本答案以《OP.GG 数据》为准，你可以基于英雄机制、敌方阵容构成和海克斯语义标签做适配性微调并写明理由。
3. "升级型"海克斯的决策语义是"升级哪件已购装备"，写亲和 note 时据此与普通海克斯区分。
4. 只输出 JSON，不输出任何其他文字。

输出 JSON schema（字段缺一不可）：
{
 "augment_affinity": [{"name":"海克斯名","bias":"ad|ap|tank|none","note":"<=18字"}],   // 12~20 个，按该英雄适配度降序
 "opening_items":    [{"combo":["装备名"],"when":"<=10字适用场景"}],                    // 恰好 3 组
 "build_paths":      {"ap":{"items":["装备名"],"skill_order":"主X副Y","note":"<=20字"},  // 2~3 条路线
                      "ad":{...}, "tank":{...}},
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
        parts.append(f"{name}（{'，'.join(bits)}）")
    return "；".join(parts) or "（无）"


def _fmt_teams(ally: list, enemy: list) -> str:
    return f"我方阵容：{'、'.join(ally) or '未知'}\n敌方阵容：{'、'.join(enemy) or '未知'}"


def _fmt_enemy_comp(kb, enemy: list) -> str:
    """敌方伤害构成一行（champ_damage_profile 现成画像）。

    tank 路线的抗性方向（堆甲 vs 堆魔抗）在加载期就要定，只给英雄名
    等于让模型自己背每个英雄的伤害类型。坦克按 is_tank 标志计数
    （damage 字段只有 ad/ap，如牛头酋长 damage=ap + is_tank=True）。
    """
    counts = {}
    tanks = 0
    for name in enemy or []:
        info = kb.champ_profile.get(name, {})
        dmg = info.get("damage")
        if dmg:
            counts[dmg] = counts.get(dmg, 0) + 1
        if info.get("is_tank"):
            tanks += 1
    label = {"ap": "法伤", "ad": "物伤"}
    bits = [f"{label[k]}×{counts[k]}" for k in ("ap", "ad") if counts.get(k)]
    if tanks:
        bits.append(f"坦克×{tanks}")
    return "敌方构成：" + "、".join(bits) if bits else ""


class ChampionContext:
    """System2 产物（内存缓存，整局有效；schema 已校验、名字已过 B 层）"""

    def __init__(self, data: dict, hero: str, source: str = "ai", meta: dict = None):
        self.raw = data
        self.hero = hero
        self.source = source          # ai / rule（降级）
        self.meta = meta or {}
        self.augment_affinity = data.get("augment_affinity", [])
        self.opening_items = data.get("opening_items", [])
        self.build_paths = data.get("build_paths", {})
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

    def skill_order_text(self) -> str:
        """加点一行（build_paths 中第一条有效路线）"""
        for path in self.build_paths.values():
            so = (path or {}).get("skill_order") or ""
            if so:
                return so
        return ""


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

    paths = {}
    for key in ("ap", "ad", "tank"):
        p = (data.get("build_paths") or {}).get(key) or {}
        items = [n for n in (p.get("items") or []) if kb.valid_item(n)]
        if items:
            paths[key] = {"items": items[:6],
                          "skill_order": str(p.get("skill_order") or "")[:16],
                          "note": str(p.get("note") or "")[:24]}
    if not paths:
        raise LLMError("build_paths 无有效路线")

    links = []
    for l in (data.get("augment_item_links") or [])[:6]:
        aug = (l or {}).get("augment") or ""
        if kb.valid_augment(aug):
            links.append({"augment": aug,
                          "shift": str((l or {}).get("shift") or "")[:24],
                          "reason": str((l or {}).get("reason") or "")[:28]})

    # component_rules 已从 schema 移除：全项目零消费（小件持有决策由规则层
    # component_advice/saving_advice 承担），每局白烧 ~150 输出 token。
    return {"augment_affinity": affinity, "opening_items": openings,
            "build_paths": paths, "augment_item_links": links}


# ── 规则版 Context（离线兜底，全部确定性） ───────────────────────────

def rule_context(kb, hero: str, rules=None) -> dict:
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
    # 路线键须与 infer_current_path 的取值域（ap/ad/tank）对齐：坦克英雄
    # （如牛头 damage=ap + is_tank=True）的主线是坦装，OP.GG 核心件投票为
    # tank——键若仍按 damage 落 ap/ad，降级局 current_path=tank 时
    # _route_line 查不到，整条路线上下文静默丢失。完整版
    # validate_and_clean 的键集即 ("ap", "ad", "tank")，无其他遗漏。
    dmg = "tank" if profile.get("is_tank") else (profile.get("damage") or "ad")
    main_items = kb.core_item_pool(hero)[:6]
    paths = {}
    if main_items:
        paths[dmg] = {"items": main_items,
                      "skill_order": kb.skill_advice(hero)["priority"],
                      "note": "OP.GG 主线"}
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
    return {"augment_affinity": affinity, "opening_items": openings,
            "build_paths": paths, "augment_item_links": links}


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
                "bias=路线倾向，tags=玩法标签，升级型=升级已购装备）\n"
                f"{_fmt_augments(kb, hero)}",
                f"数据版本：{data_version or kb.dd_version}",
                "请输出 ChampionContext JSON。",
            ])
            if attempt and last_err is not None:
                user += (f"\n\n注意：上一次输出未通过校验（{last_err}）。"
                         "请严格按 schema 输出，名字必须逐字取自名单。")
            text, meta = client.chat(SYSTEM_PROMPT, user, max_tokens=1600)
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
    return ChampionContext(rule_context(kb, hero, rules), hero,
                           source="rule", meta={"error": str(last_err)[:120]})


class ContextCache:
    """整局缓存：新局 start_game 覆盖，线程安全（System2 在后台线程生成）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._ctx = None
        self._pending = False

    def set(self, ctx: ChampionContext):
        with self._lock:
            self._ctx = ctx
            self._pending = False

    def get(self) -> ChampionContext | None:
        with self._lock:
            return self._ctx

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._pending

    def invalidate(self):
        with self._lock:
            self._ctx = None

    def start_pending(self):
        with self._lock:
            self._ctx = None
            self._pending = True
