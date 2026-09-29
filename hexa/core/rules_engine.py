"""层 0 确定性规则引擎（计划 §5.2 / §5.3）

职责：
1. current_path 确定性推断 —— 已购装备 + 已选海克斯 → ap/ad/tank（防 System1 路线漂移）
2. 敌方威胁画像 —— 英雄伤害粗分类 + 装备扫描（无 per-player 实时伤害数据，推断式）
3. 三类候选池构造 —— 出门装 / 下一件 / 小件合成
   （P1 起：下一件用英雄 OP.GG 高频装，出门装用 OP.GG starter_items；
    P2 由 ChampionContext + System1 排序接管，本引擎仍为离线兜底）
4. 海克斯时刻规则评级（S/A/B：先验排名 + 语境修正合成评分，非胜率分桶）
5. 己方阵容缺口 ally_gap + 攒钱决策 saving_advice（v4.5 军师/流派倾向：
   缺前排 × 可扛英雄 → tank 路线加分；下一大件差价 ≤500 且无组件可买 → 攒）

铁律：数据出事实，模型出判断 —— 本模块全部确定性，无随机无 AI。
"""
from dataclasses import dataclass, field

# ── 知名海克斯 → 路线偏移（兜底小表；语义标签库就位后由其接管） ────
# 注：键名以真机 229 海克斯名单实测为准校准（"心之刚"为计划示例名，
# 当前 hero_augments.csv 名单中未收录，实测后统一修正）。
AUGMENT_PATH_BIAS = {
    "心之刚": "tank",
    "不灭之握": "tank",
    "贪欲束缚": "ad",
}

# 知名海克斯 → 出门装联动（计划验收场景：心之刚 → 巨人腰带）
AUGMENT_OPENING_SHIFT = {
    "心之刚": ("巨人腰带", "直接放大其成长收益"),
}

# 重伤装备名（P0 无语义标签时的直接名字匹配，标签库就位后由 tag="重伤" 接管）
ANTIHEAL_ITEMS = {"凡性的提醒", "莫雷洛秘典", "死刑宣告", "炼金朋克链锯剑"}

# ── 治疗系英雄（敌方重伤判断依据，P0 手工粗表，P1 精化） ────────────
HEALERS = {
    "众星之子", "魔法猫咪", "唤潮鲛姬", "琴瑟仙女", "天启者", "雪原双子",
    "沙漠皇帝", "血港鬼影", "永猎双子", "解脱者", "炼金男爵", "审判天使",
}

# ── 敌方关键装备（克制提示触发器，P0 表） ─────────────────────────
KEY_ENEMY_ITEMS = {
    "荆棘之甲": "高护甲反弹伤：AD 打坦乏力，考虑破甲/法伤",
    "中娅沙漏": "AP 爆发被金身化解：避免一套全交",
    "莫雷洛秘典": "敌方有重伤：治疗/回复效果减半",
    "凡性的提醒": "敌方有重伤：治疗/回复效果减半",
    "自然之力": "高魔抗持续回复：AP 消耗乏力",
    "兰顿之兆": "高护甲减速暴击：暴击流受阻",
}

LEGENDARY_GOLD = 2200  # 传说件价格门槛（粗分）

# 满六神装判定的传说件下限：6 占格中至少 5 件"传说档非鞋"才算成型。
# 依据 2026-09-28 真机复盘：按"6 占格"判满装会把 3 传说+2 组件+鞋的
# 中期局面切进替换流，推"卖X换Y"幻影替换（炽炎雏龙局更早：1 传说+
# 魄罗佳肴凑格即触发）。判据收紧后这类局面回买装流，由
# component_advice（合成腾格）+ saving_advice 接手。
FULL_BUILD_LEGENDARY_MIN = 5


def is_full_build(kb, items) -> bool:
    """满六神装判定（rules_engine / game_state 两处调用点单一口径）。

    格满（permanent_items ≥6）且其中传说档非靴件 ≥FULL_BUILD_LEGENDARY_MIN。
    鞋/组件/消耗品占格不再单独构成满装；库外名仍按占格保留（kb 口径），
    但不计传说数。5 传说+1 组件（无靴）等边界翻为满装属期望行为。
    """
    perms = kb.permanent_items(items)
    if len(perms) < 6:
        return False
    return sum(
        1 for n in perms
        if kb.items.get(n, {}).get("gold", 0) >= LEGENDARY_GOLD
        and "Boots" not in kb.items.get(n, {}).get("tags", [])
    ) >= FULL_BUILD_LEGENDARY_MIN

# 攒大件阈值：下一大件按组件抵扣后差价 ≤ 此值、且身上没有现金能买的
# 未拥有组件 → 建议攒钱直购。依据：大乱斗被动收入约 44G/5s + 一波兵线，
# 差价 500G ≈ 一次回城间隔内即可攒够；整件成型立即获得完整属性/被动，
# 优于把钱拆进小件（组件价值虽可抵扣，但多占一格、无成型收益）。
SAVE_GOLD_THRESHOLD = 500

# ── 属性堆叠杂项守卫（推荐池过滤；P0-3 2026-09-28 真机复盘） ────────
# DD tags 与此"属性家族"交集 ≥8 → 该件是纯属性加成型杂项（全库实测仅
# 属性加成=8 / 金铲铲=9 命中，正常传说件交集最高 5）。这类件 tags 全家桶，
# 任何"魔抗优先/护甲优先"短板都会误命中替换池（金铲铲案），合成链又把
# 它顶成"先合成"目标。只进推荐池过滤——不进步格计数/台账/幻觉校验。
FULLSTAT_FAMILY_TAGS = frozenset({
    "SpellDamage", "Damage", "CriticalStrike", "AttackSpeed", "LifeSteal",
    "ArmorPenetration", "Armor", "SpellBlock", "Health", "HealthRegen",
})


def _is_fullstat_item(kb, name: str) -> bool:
    """金铲铲/属性加成这类全家桶 tags 杂项 → True（不进任何推荐池）。"""
    tags = kb.items.get(name, {}).get("tags", [])
    return len(set(tags) & FULLSTAT_FAMILY_TAGS) >= 8


@dataclass
class ThreatProfile:
    ap_count: int = 0
    ad_count: int = 0
    tank_count: int = 0
    healers: list = field(default_factory=list)
    key_items: list = field(default_factory=list)  # [{"champion", "item", "note"}]
    # 敌方传说级抗性件总数（换装克制判断：三人堆魔抗 vs 我方法伤等场景）
    enemy_mr_items: int = 0
    enemy_armor_items: int = 0
    # 己方阵容缺口（v4.5 军师/流派倾向数据源）。engine 层接口不透传
    # gap，GameSession 把它挂到威胁对象上，随 threat 穿透到
    # build_sequence / next_item_candidates（"缺前排→tank 加分"链路）
    ally_gap: object = None

    def summary(self) -> str:
        parts = []
        if self.ap_count:
            parts.append(f"法伤×{self.ap_count}")
        if self.ad_count:
            parts.append(f"物伤×{self.ad_count}")
        if self.tank_count:
            parts.append(f"坦克×{self.tank_count}")
        if self.healers:
            parts.append(f"治疗:{'/'.join(self.healers)}")
        if self.key_items:
            # 去重取前 3（原只显示第一件，克制信息丢了一截）
            names = list(dict.fromkeys(k["item"] for k in self.key_items))
            parts.append("关键装:" + "/".join(names[:3]))
        return " | ".join(parts) or "暂无数据"

    def resist_advice(self) -> str:
        """敌方构成 → 抗性采购方向（候选池加权依据）"""
        if self.ap_count >= 3:
            return "魔抗优先"
        if self.ad_count >= 3:
            return "护甲优先"
        if self.healers and len(self.healers) >= 2:
            return "重伤考虑"
        if self.ap_count == 2 and self.ad_count == 2:
            return "双抗均衡"
        return ""


@dataclass
class Candidate:
    name: str
    reason: str
    price: int = 0          # 装备总价（DD gold）
    afford_note: str = ""   # 金币感知标注（"可购买"/"还差N"），由 GameSession 按序累计填写


@dataclass
class AllyGap:
    """己方阵容缺口（champ_damage_profile 画像统计；军师文案与流派倾向共用）"""
    tanks: int = 0
    ap: int = 0
    ad: int = 0
    gaps: list = field(default_factory=list)  # ["缺前排"/"缺AP"/"缺AD"]
    complete: bool = True  # 名单完整性门槛（ally_gap 不足 5 人置 False，不判缺）

    @property
    def need_tank(self) -> bool:
        return "缺前排" in self.gaps

    @property
    def need_ap(self) -> bool:
        return "缺AP" in self.gaps

    @property
    def need_ad(self) -> bool:
        return "缺AD" in self.gaps

    def summary(self) -> str:
        if not self.complete:
            return "暂无数据"   # 名单不全，缺位结论不可信（同威胁画像口径）
        return "、".join(self.gaps) if self.gaps else "阵容均衡"


@dataclass
class SwapCandidate(Candidate):
    """满六神装替换建议对：卖 sell 换买 name（Candidate 子类）。

    继承 Candidate 使替换对能沿用引擎/建议管道（engine.next_candidates
    的同步底线与 AI 精修转换都按 Candidate 协议收发，无需感知新类型，
    system1 按 buy 属性识别切 prompt）。net_cost = 买价 − 卖价 − 身上
    金币（<=0 = 现金足够立即换）；引擎层拿不到金币时先按价差计，
    GameSession 收到后再补减（见 _suggest_swap）。
    """
    sell: str = ""
    net_cost: int = 0
    sell_price: int = 0

    @property
    def buy(self) -> str:
        return self.name


class RulesEngine:
    def __init__(self, kb):
        self.kb = kb

    # ── 0. 语义标签接入（标签库优先，内置表兜底） ─────────────────

    def augment_path_bias(self, name: str):
        """海克斯 → 路线偏移（B 层语义标签优先；'none'/'unknown' 视为无偏移）"""
        info = self.kb.augment_tag_info(name)
        bias = info.get("path_bias")
        if bias in ("ap", "ad", "tank"):
            return bias
        if bias:  # 显式标注 none/unknown → 无偏移，不回落内置表（防标签被小表覆盖）
            return None
        return AUGMENT_PATH_BIAS.get(name)  # 未标注/数据缺失（augments.json 无此条）→ 内置小表兜底

    def augment_opening_shift(self, name: str):
        """海克斯 → 出门装联动 (item, reason) | None"""
        info = self.kb.augment_tag_info(name)
        shift = info.get("opening_shift")
        if shift and self.kb.valid_item(shift):
            return shift, info.get("opening_shift_reason") or "与该海克斯联动收益高"
        if name in AUGMENT_OPENING_SHIFT:
            item, why = AUGMENT_OPENING_SHIFT[name]
            if self.kb.valid_item(item):
                return item, why
        return None

    def is_antiheal_item(self, name: str) -> bool:
        if name in ANTIHEAL_ITEMS:
            return True
        return "重伤" in self.kb.augment_tag_info(name).get("tags", [])

    # ── 1. current_path 确定性推断 ────────────────────────────────

    def infer_current_path(self, my_items: list, augments: list,
                           hero: str = "", gap=None) -> str:
        """已购装备（按 DD 标签投票，传说件权重 ×2）+ 已选海克斯偏移 → ap/ad/tank

        空输入（出门装前）返回 ""（路线未定，出门装阶段不约束）。
        v4.5 阵容缺口加分：缺前排且我的英雄可扛（is_tank/Fighter）时
        tank 票 +tank_lean——只放大已有的坦向证据（身上至少一件坦件），
        让"坦/输出各一件"的并列压向坦线；不凭空把纯输出出装判成坦
        （单件散件路线不受阵容缺口翻转，防误伤出门装回归）。
        """
        votes = {"ap": 0.0, "ad": 0.0, "tank": 0.0}
        for item in my_items:
            bias = self.kb.item_path_bias(item)
            if not bias:
                continue
            weight = 2.0 if self.kb.items.get(item, {}).get("gold", 0) >= LEGENDARY_GOLD else 1.0
            votes[bias] += weight
        for aug in augments:
            bias = self.augment_path_bias(aug)
            if bias:
                votes[bias] += 1.5  # 海克斯偏移权重：介于小件与传说件之间
        lean = self.tank_lean(hero, gap)
        if lean and votes["tank"] > 0:
            votes["tank"] += lean
        if sum(votes.values()) == 0:
            return ""
        return max(votes, key=votes.get)

    def path_drift(self, current_path: str, my_items: list) -> bool:
        """已购装备偏离当前 path 判断（overlay 提示用，不拦截）"""
        if not current_path:
            return False
        off, total = 0, 0
        for item in my_items:
            bias = self.kb.item_path_bias(item)
            if bias:
                total += 1
                if bias != current_path:
                    off += 1
        return total > 0 and off / total > 0.5

    # ── 2. 敌方威胁画像 ───────────────────────────────────────────

    def _item_damage_vote(self, items: list):
        """身上装备 → (伤害路线, 最高票重)；票重规则同 infer_current_path"""
        votes = {"ap": 0.0, "ad": 0.0, "tank": 0.0}
        for item in items:
            bias = self.kb.item_path_bias(item)
            if not bias:
                continue
            weight = 2.0 if self.kb.items.get(item, {}).get("gold", 0) >= LEGENDARY_GOLD else 1.0
            votes[bias] += weight
        best = max(votes, key=votes.get)
        return best, votes[best]

    def threat_profile(self, enemy_champions: list, enemy_players: list = None) -> ThreatProfile:
        """enemy_champions: 敌方英雄中文名列表；
        enemy_players: [PlayerInfo]（Live 快照敌方，可为空 = 游戏早期/无数据）
        """
        tp = ThreatProfile()
        # 敌方英雄 → 身上装备（伤害类型按实际出装推断，英雄默认类型兜底：
        # 女枪/辛德拉下路常规外，AP 女枪、AD 卡尔玛等非常规出装以装备为准）
        items_by_champ = {}
        for p in (enemy_players or []):
            cn = p.champion or self.kb.champion_cn(p.champion_en) or ""
            if cn and cn not in items_by_champ:
                items_by_champ[cn] = [it.get("name", "") for it in p.items
                                      if it.get("name")]
        for cn in enemy_champions:
            prof = self.kb.champ_profile.get(cn, {})
            damage = prof.get("damage", "")
            is_tank = bool(prof.get("is_tank"))
            vote, weight = self._item_damage_vote(items_by_champ.get(cn) or [])
            # 一件传说件或两件小件即认定实际路线；单小件证据不足以推翻默认
            if weight >= 2.0 or (vote and not damage):
                if vote == "tank":
                    is_tank = True
                elif vote:
                    damage = vote
            if damage == "ap":
                tp.ap_count += 1
            elif damage == "ad":
                tp.ad_count += 1
            if is_tank:
                tp.tank_count += 1
            if cn in HEALERS:
                tp.healers.append(cn)
        for p in (enemy_players or []):
            for it in p.items:
                note = KEY_ENEMY_ITEMS.get(it.get("name", ""))
                if note:
                    tp.key_items.append({
                        "champion": p.champion, "item": it["name"], "note": note})
                # 敌方传说级抗性件计数（换装克制判断：敌方全队堆魔抗/护甲）
                info = self.kb.items.get(it.get("name", ""))
                if info and info["gold"] >= LEGENDARY_GOLD:
                    tags = set(info["tags"])
                    if "SpellBlock" in tags:
                        tp.enemy_mr_items += 1
                    if "Armor" in tags:
                        tp.enemy_armor_items += 1
        return tp

    # ── 2b. 己方阵容缺口（v4.5 军师 / 流派倾向） ──────────────────

    def ally_gap(self, champions: list, my_hero: str = "") -> AllyGap:
        """己方 5 人（含我）阵容缺口：前排数（is_tank）+ AP/AD 输出数。

        判据（5 人编制）：无前排=缺前排；法伤位 0=缺AP；物伤位 0=缺AD；
        全都有 → 均衡（gaps 空）。敌方阵容同样可用本函数统计，
        反向利用（敌方缺 AP → 我方护甲更值钱）由军师层取 need_ap/need_ad。
        my_hero 仅作记录用途（加分判断在 tank_lean，单独传 hero）。
        名单完整性门槛：不足 5 人（对局刚开始快照不全、LCU 报错、名单缺
        id）时只记数不判缺，summary 落"暂无数据"（同威胁画像口径）——
        缺位结论建立在"5 人看全"的前提上，否则空名单会同时得出"缺AP"
        "缺AD"，军师层同帧互斥提示双双触发；信息不足不出声（同顺逆风）。
        """
        gap = AllyGap()
        for cn in champions:
            prof = self.kb.champ_profile.get(cn, {})
            if prof.get("is_tank"):
                gap.tanks += 1
            damage = prof.get("damage")
            if damage == "ap":
                gap.ap += 1
            elif damage == "ad":
                gap.ad += 1
        if len(champions) < 5:
            gap.complete = False
            return gap
        if gap.tanks == 0:
            gap.gaps.append("缺前排")
        if gap.ap == 0:
            gap.gaps.append("缺AP")
        if gap.ad == 0:
            gap.gaps.append("缺AD")
        return gap

    def tank_lean(self, hero: str, gap) -> float:
        """缺前排 × 我的英雄可扛（is_tank 或 Fighter 类 tags）→ tank 加分权重。

        取 1.5：高于单小件票重(1.0)——缺前排时能把"坦/输出各一件"的
        并列压向坦线；调用侧要求已有坦向证据（infer_current_path 里
        votes["tank"]>0 才生效），纯输出出装不受阵容缺口翻转。
        """
        if not gap or not getattr(gap, "need_tank", False) or not hero:
            return 0.0
        prof = self.kb.champ_profile.get(hero, {})
        if prof.get("is_tank") or "Fighter" in (prof.get("tags") or []):
            return 1.5
        return 0.0

    # ── 3a. 出门装候选池（OP.GG starter_items + 海克斯联动，手工池兜底） ──

    OPENING_POOL = {
        "ap":  [("多兰之戒", "标准 AP 出门：法强+生命+回蓝"),
                ("黑暗封印", "成长型出门：有击杀信心时"),
                ("红水晶", "偏防守：对面 poke 猛")],
        "ad":  [("多兰之刃", "标准 AD 出门：攻击+生命+吸血"),
                ("长剑", "进攻型：直奔大件"),
                ("红水晶", "偏防守：对面 poke 猛")],
        "tank": [("红水晶", "标准坦出门：直奔腰带/抗性"),
                 ("布甲", "对面物理多"),
                 ("抗魔斗篷", "对面法术多")],
        "":    [("多兰之戒", "AP 英雄标准出门"),
                ("多兰之刃", "AD 英雄标准出门"),
                ("红水晶", "坦克/防守出门")],
    }

    def opening_candidates(self, my_hero: str, first_augment: str = None,
                           threat: ThreatProfile = None,
                           game_map: str = "") -> list:
        """出门装三选（P1：OP.GG 高频 starter 组合优先 + 海克斯联动置顶）。

        P2 由 ChampionContext.opening_items 接管；本函数为规则版/离线兜底。
        game_map: "11"=峡谷 / "12"=大乱斗，候选须在当前地图可买。
        """
        cands = []
        # 1. 海克斯联动置顶（计划验收场景：心之刚 → 巨人腰带）
        if first_augment:
            shift = self.augment_opening_shift(first_augment)
            if shift:
                cands.append(Candidate(
                    shift[0], f"已选【{first_augment}】：{shift[1]}"))
        # 2. OP.GG 高频出门组合（各组合取首件，价格按总价标注）
        for combo in self.kb.starter_combos(my_hero)[:2]:
            name = combo[0]
            if any(c.name == name for c in cands):
                continue
            price = sum(self.kb.items.get(n, {}).get("gold", 0) for n in combo)
            cands.append(Candidate(name, "OP.GG 高频出门组合（" + "+".join(combo) + "）",
                                   price=price))
        # 3. 手工池补足到 3（按英雄伤害类型 + 敌方构成微调）
        if len(cands) < 3:
            path = self.kb.champ_profile.get(my_hero, {}).get("damage", "")
            for n, r in self.OPENING_POOL.get(path, self.OPENING_POOL[""]):
                if any(c.name == n for c in cands):
                    continue
                cands.append(Candidate(n, r))
                if len(cands) >= 3:
                    break
        # 4. 对面 poke 猛/坦克多时提示防守位（不改变首位，语境修正）
        if threat and threat.ap_count >= 3 and len(cands) >= 3:
            for c in cands:
                if "poke" in c.reason:
                    c.reason = "对面法伤多，血量装更稳"
        # 当前地图买不到的（乱斗专属件混进峡谷 OP.GG 池等）不占三选名额
        cands = [c for c in cands
                 if self.kb.item_available(c.name, game_map)]
        for c in cands:  # 统一补价格（含手工池/联动项）
            if not c.price:
                c.price = self.kb.items.get(c.name, {}).get("gold", 0)
        return cands[:3]

    # ── 3b. 下一件候选池（OP.GG 高频装 ∪ DD 抗性件，敌方构成加权） ──

    def next_item_candidates(self, my_items: list, current_path: str,
                             threat: ThreatProfile, hero: str = "",
                             limit: int = 3, game_map: str = "",
                             gap=None) -> list:
        """在 current_path 内、排除已拥有、按敌方构成加权的传说件候选。

        P1 候选池 = 英雄 OP.GG 高频核心装（顺序即登场率），坦克路线另开放
        DD 抗性件全库扫描（输出位不因敌方构成推抗性装）。分数 = OP.GG 高频
        (3→0) + 路线匹配(2) + 敌方抗性方向(2) + 正在合成(3)。
        game_map: "11"/"12"，当前地图不可买的名字直接出局。
        gap: 己方阵容缺口（缺前排且英雄可扛 → 坦向件 +tank_lean 分）；
        显式传入优先，否则从 threat.ally_gap 取（engine 层经 threat 穿透）。
        """
        if gap is None and threat is not None:
            gap = getattr(threat, "ally_gap", None)
        lean = self.tank_lean(hero, gap)
        owned = set(my_items)
        # 我已有装备的升级目标（优先：正在攒的合成线）
        building_into = set()
        for item in my_items:
            for tid in self.kb.items.get(item, {}).get("into", []):
                tname = self.kb.item_id_to_name.get(tid)
                if tname:
                    building_into.add(tname)

        advice = threat.resist_advice()
        opgg_pool = self.kb.core_item_pool(hero)
        opgg_rank = {name: i for i, name in enumerate(opgg_pool)}
        # 抗性件池：仅坦克路线开放全库扫描。输出位（ad/ap）对抗敌方构成的
        # 方式是继续出伤害装/走位，OP.GG 池自带的顺带抗性件（如中娅）足够；
        # 真机反馈 2026-09-27：AD 小丑被"对面物伤多"推护甲装，方向反了
        resist_pool = set()
        if current_path == "tank":
            for name, info in self.kb.items.items():
                if info["gold"] >= LEGENDARY_GOLD and (
                        {"SpellBlock", "Armor"} & set(info["tags"])):
                    resist_pool.add(name)

        scored = []
        seen = set()
        for name in list(opgg_pool) + sorted(resist_pool):
            if name in owned or name in seen:
                continue
            info = self.kb.items.get(name)
            if not info or info["gold"] < LEGENDARY_GOLD:
                continue
            if _is_fullstat_item(self.kb, name):
                continue  # 金铲铲/属性加成类全家桶杂项，不进推荐池
            if not self.kb.item_available(name, game_map):
                continue  # 当前地图商店没有（模式专属件），推了也买不了
            seen.add(name)
            bias = self.kb.item_path_bias(name)
            is_resist = bool({"SpellBlock", "Armor"} & set(info["tags"]))
            if current_path and bias != current_path and not is_resist:
                continue
            score = 0.5 if bias != current_path else 2.0
            if name in opgg_rank:
                score += max(0.0, 3.0 - 0.3 * opgg_rank[name])
            tags = info["tags"]
            if advice == "魔抗优先" and "SpellBlock" in tags:
                score += 2.0
            elif advice == "护甲优先" and "Armor" in tags:
                score += 2.0
            elif advice == "重伤考虑" and self.is_antiheal_item(name):
                score += 2.0
            if name in building_into:
                # 正在合成的线优先；OP.GG 池内目标 +3，池外 +1（数据序
                # into 目标未必是本命核心——心之钢旁路案 2026-09-28：
                # 巨人腰带 into 的池外件不该与池内核心同权重）
                score += 3.0 if name in opgg_rank else 1.0
            if lean and bias == "tank":
                # 己方缺前排且英雄可扛：坦向件加分（权重同路线推断，
                # 让"下一件"排序与路线推断的倾向一致）
                score += lean
            scored.append((score, name))

        scored.sort(reverse=True)
        # 抗性建议文案：只对本身带对应抗性的装备写进理由（对纯输出件写"考虑魔抗"是误导）
        out = []
        for score, name in scored[:limit]:
            reason_bits = []
            info = self.kb.items.get(name, {})
            tags = set(info.get("tags", []))
            bias = self.kb.item_path_bias(name)
            # "补抗性"文案只挂真防守件（bias=tank）：中娅/血手这类带抗性的
            # 输出件按输出件描述，不误导（复盘文档遗留待办）
            if advice == "魔抗优先" and "SpellBlock" in tags and bias == "tank":
                reason_bits.append("对面法伤多，补魔抗")
            elif advice == "护甲优先" and "Armor" in tags and bias == "tank":
                reason_bits.append("对面物伤多，补护甲")
            elif advice == "重伤考虑" and self.is_antiheal_item(name):
                reason_bits.append("对面治疗多，考虑重伤")
            if name in building_into:
                reason_bits.insert(0, "身上小件正在往它合")
            if name in opgg_rank:
                reason_bits.append(f"OP.GG 高频（第{opgg_rank[name] + 1}位）")
            elif bias == current_path:
                reason_bits.append(f"当前{current_path.upper()}路线")
            out.append(Candidate(name, "；".join(reason_bits) or "通用推荐"))
        return out

    def build_sequence(self, my_items: list, current_path: str,
                       threat: ThreatProfile, hero: str = "",
                       limit: int = 3, game_map: str = "",
                       gap=None) -> list:
        """出装主线序列（v4.3）：英雄核心套装线优先，敌情评分补位。

        v4.2 按"敌情加权评分"直接排会出"还没出核心就被阵容带偏"的序列；
        v4.3 改为：①按当前路线选英雄的核心组合（kb.core_build_line，
        爆杀流/半肉/法系线由路线决定），未拥有的核心件按登场序占满前排；
        ②核心出完或不足 limit 再用 next_item_candidates（敌情/合成线/其余
        高频件）补位。每件带价格，可买性由 GameSession 按金币累计填写。
        满六神装（v4.4）：格满后"下一件"买不进格，主线切为替换建议
        （replacement_candidates）。gold/augments 引擎接口不透传，替换对
        先按价差计 net_cost，GameSession 收到后补减金币并按台账联动重排。
        gap: 己方阵容缺口，经 next_item_candidates 参与坦向件加分；
        缺省从 threat.ally_gap 取（GameSession 挂载，穿透 engine 层）。
        """
        if gap is None and threat is not None:
            gap = getattr(threat, "ally_gap", None)
        if is_full_build(self.kb, my_items):
            return self.replacement_candidates(
                my_items, 0, current_path, threat,
                augments=None, hero=hero, limit=limit, game_map=game_map)
        owned = set(my_items)
        seq, seen = [], set()
        line = self.kb.core_build_line(hero, current_path)
        for idx, name in enumerate(line, 1):
            if name in owned or name in seen:
                continue
            if not self.kb.item_available(name, game_map):
                continue
            seen.add(name)
            seq.append(Candidate(
                name,
                f"核心装（{current_path.upper()}线第{idx}件）"
                if current_path else f"核心装（OP.GG 主线第{idx}件）",
                price=self.kb.items.get(name, {}).get("gold", 0)))
            if len(seq) >= limit:
                return seq
        for c in self.next_item_candidates(my_items, current_path, threat,
                                           hero=hero, limit=limit * 2,
                                           game_map=game_map, gap=gap):
            if c.name in seen:
                continue
            seen.add(c.name)
            if not c.price:
                c.price = self.kb.items.get(c.name, {}).get("gold", 0)
            seq.append(c)
            if len(seq) >= limit:
                break
        return seq

    # ── 3e. 满六神装替换候选（v4.4：格满后改推"卖X换Y"） ──────────

    def _sell_contribution(self, item: str, current_path: str,
                           threat: ThreatProfile, augments=None) -> float:
        """已拥有传说件的当前局势贡献分（越高越不该卖）。

        三个参考维度（任务口径）：威胁画像（敌方 AP 多而这件毫无魔抗
        贡献 → 弱）、与 current_path 一致性（path_drift 思路复用）、
        与已选海克斯联动度（augments 可得时）。
        """
        info = self.kb.items.get(item, {})
        tags = set(info.get("tags", []))
        bias = self.kb.item_path_bias(item)
        # 基础底价：能进六神装的传说件都有基本贡献，只在相对排序中变弱
        score = 1.0
        if bias and bias == current_path:
            score += 2.0        # 与主力路线一致（核心输出/承伤件）
        elif bias and current_path:
            score -= 1.5        # 偏离主力路线（早期过渡件/路线切换遗留）
        advice = threat.resist_advice() if threat else ""
        if advice == "魔抗优先":
            # +3.0 与 buy 侧短板动机同价：敌方 AP 成型时，身上的魔抗件
            # 正是为此买的，不因"偏离输出路线"被反卖（-1.5 扣不过 +3.0）
            score += 3.0 if "SpellBlock" in tags else -1.0
        elif advice == "护甲优先":
            score += 3.0 if "Armor" in tags else -1.0
        elif advice == "重伤考虑" and self.is_antiheal_item(item):
            score += 1.0
        for aug in augments or []:
            if bias and self.augment_path_bias(aug) == bias:
                score += 0.5    # 与已选海克斯同向，联动收益被放大
        return score

    def _replacement_buy_pool(self, owned_after: set, current_path: str,
                              threat: ThreatProfile, hero: str = "",
                              game_map: str = "") -> list:
        """替换买件池（复用 next_item_candidates 的池子思路再补短板）。

        与"下一件"的关键差异：替换的核心动机就是针对性补威胁短板，
        抗性/重伤件不限坦克路线开放（next_item_candidates 里只有 tank
        才开抗性池——那是"成长期不因敌情改线"，满装换装反过来正是
        为了敌情调整）。返回按 适配分 降序的 [(score, name, reason)]。
        """
        advice = threat.resist_advice() if threat else ""
        scored = {}
        # 1. 威胁短板件（替换的第一动机）：敌方 AP 多 → 魔抗件；物伤多 → 护甲
        want_tag = {"魔抗优先": "SpellBlock", "护甲优先": "Armor"}.get(advice)
        if want_tag:
            for name, info in self.kb.items.items():
                if (info["gold"] >= LEGENDARY_GOLD
                        and want_tag in info["tags"]
                        and name not in owned_after
                        and self.kb.item_available(name, game_map)
                        and not _is_fullstat_item(self.kb, name)):
                    label = ("补魔抗短板" if want_tag == "SpellBlock"
                             else "补护甲短板")
                    scored[name] = (3.0, label)
        elif advice == "重伤考虑":
            for name, info in self.kb.items.items():
                if (info["gold"] >= LEGENDARY_GOLD
                        and self.is_antiheal_item(name)
                        and name not in owned_after
                        and self.kb.item_available(name, game_map)
                        and not _is_fullstat_item(self.kb, name)):
                    scored[name] = (3.0, "敌方治疗多，重伤压制")
        # 2. OP.GG 高频/路线内传说件（换装也可以是同级更优输出件）；
        #    分值封顶 2.7 < 短板件 3.0——替换的第一动机是补威胁短板
        opgg_pool = self.kb.core_item_pool(hero)
        for rank, name in enumerate(opgg_pool):
            if name in scored or name in owned_after:
                continue
            info = self.kb.items.get(name)
            if not info or info["gold"] < LEGENDARY_GOLD:
                continue
            if not self.kb.item_available(name, game_map):
                continue
            bias = self.kb.item_path_bias(name)
            if current_path and bias != current_path:
                continue  # 换装不换路线（换路线是重开局级决策）
            scored[name] = (max(0.3, 2.7 - 0.3 * rank),
                            f"OP.GG 高频（第{rank + 1}位）")
        return sorted(((s, n, r) for n, (s, r) in scored.items()), reverse=True)

    def replacement_candidates(self, my_items: list, gold: float,
                               current_path: str, threat: ThreatProfile,
                               augments=None, hero: str = "", limit: int = 3,
                               game_map: str = "") -> list:
        """满六神装替换建议：[{sell, buy, reason, net_cost}]（SwapCandidate）。

        sell 池 = 已拥有传说件（靴子默认不卖——移动速度无替代品，且
        kb 鞋类价位远低于传说线，卖了也回不了多少款；无卖价不入池，
        投机者之刃是库内唯一无 gold.sell 的传说件，混进来卖价按 0 算，
        差价/净差全错）；按"当前局势贡献最低"升序取前 3。buy 池不与
        已拥有重复（含被卖件本身，卖X买回X是无意义换手）、当前地图可买。
        同一买件只保留最优卖件对（AI 精修按买件名映射回对，买件名必须唯一）。
        net_cost = 买价 − 卖价 − gold（<=0 = 现金足够立即换）。
        """
        # sell 池：传说件、库内（要核卖价）、非靴子、有卖价
        sellable = [it for it in my_items
                    if it in self.kb.items
                    and self.kb.items[it]["gold"] >= LEGENDARY_GOLD
                    and "Boots" not in self.kb.items[it]["tags"]
                    and self.kb.item_sell_price(it) > 0]
        if not sellable:
            return []
        # 同分兜底排序：先卖英雄高频池外的散件（本命核心件不轻易动，
        # 键取 in core_set——池外 False 升序在前），再按价格升序（回款
        # 小、试错成本低）
        core_set = set(self.kb.core_item_pool(hero))
        sellable.sort(key=lambda it: (
            self._sell_contribution(it, current_path, threat, augments),
            it in core_set,
            self.kb.items[it]["gold"]))
        advice = threat.resist_advice() if threat else ""
        best_by_buy = {}
        # buy 池排除"全部已拥有"（含被卖件：卖X买回X是无意义换手）；
        # 池子只依赖已拥有/路线/敌情，与卖件无关，循环外算一次即可
        # （原来在 sellable[:3] 循环内重复算 3 遍，结果相同纯属白做）
        owned_all = set(my_items)
        buy_pool = self._replacement_buy_pool(
            owned_all, current_path, threat, hero, game_map)[:3]
        for sell in sellable[:3]:
            sp = self.kb.item_sell_price(sell)
            # sell 侧弱点文案（威胁错配 > 路线偏离 > 兜底）
            s_bias = self.kb.item_path_bias(sell)
            s_tags = set(self.kb.items[sell]["tags"])
            if advice == "魔抗优先" and "SpellBlock" not in s_tags:
                weak = "敌方法伤成型而它零魔抗"
            elif advice == "护甲优先" and "Armor" not in s_tags:
                weak = "敌方物伤成型而它零护甲"
            elif s_bias and current_path and s_bias != current_path:
                weak = f"偏离当前{current_path.upper()}路线"
            else:
                weak = "当前局势贡献最低"
            for b_score, buy, b_reason in buy_pool:
                info = self.kb.items.get(buy, {})
                sw = SwapCandidate(
                    name=buy, reason=f"{weak}，换入{b_reason}",
                    price=info.get("gold", 0),
                    sell=sell, sell_price=sp,
                    net_cost=info.get("gold", 0) - sp - int(gold))
                # 同一买件只留分更高的对（卖件更弱 = 该对更值得执行）
                cur = best_by_buy.get(buy)
                if cur is None or b_score > cur[0]:
                    best_by_buy[buy] = (b_score, sw)
        out = [sw for _s, sw in sorted(best_by_buy.values(),
                                       key=lambda t: -t[0])]
        return out[:limit]

    def swap_augment_rank(self, swaps: list, augments: list) -> list:
        """替换对按已选海克斯联动度稳定重排（只定序不减员）。

        引擎接口不透传 augments，规则层排序拿不到台账——由 GameSession
        收到替换对后用台账补排。集合不变仅重排，AI 精修按买件名映射
        不受影响。
        """
        if not augments or not swaps:
            return swaps
        biases = [b for b in (self.augment_path_bias(a) for a in augments) if b]
        if not biases:
            return swaps

        def synergy(sw):
            b = self.kb.item_path_bias(sw.buy)
            return -sum(1.0 for x in biases if x == b) if b else 0.0
        return sorted(swaps, key=synergy)  # stable：同分保持规则序

    def swap_forecast(self, my_items: list, current_path: str,
                      threat: ThreatProfile) -> str:
        """威胁画像 vs 已有装备明显错配 → 替换预告文案（提示级，不改主建议）。

        任务示例场景：敌方三人堆魔抗而你全身 AP；敌方 AP 成型而你
        毫无魔抗。空串 = 无明显错配。
        """
        if not threat:
            return ""
        tags_all = [set(self.kb.items.get(n, {}).get("tags", []))
                    for n in my_items]
        advice = threat.resist_advice()
        if advice == "魔抗优先" and not any("SpellBlock" in t for t in tags_all):
            return (f"敌方法伤×{threat.ap_count}成型而你无魔抗装，"
                    "后续替换优先补魔抗")
        if advice == "护甲优先" and not any("Armor" in t for t in tags_all):
            return (f"敌方物伤×{threat.ad_count}成型而你无护甲装，"
                    "后续替换优先补护甲")
        if current_path == "ap" and threat.enemy_mr_items >= 3:
            return "敌方三人以上堆魔抗克制法伤，替换时考虑法穿/真伤方向"
        if current_path == "ad" and threat.enemy_armor_items >= 3:
            return "敌方三人以上堆护甲克制物伤，替换时考虑破甲/法伤方向"
        return ""

    # ── 3d. 海克斯时刻规则评级（S/A/B 合成评分，非胜率分桶） ────────

    def rank_augments(self, hero_cn: str, card_names: list,
                      taken: list = None) -> list:
        """三张卡片 → [{name, grade, reason}]（规则版；P2 由 System1 接管）。

        评分 = 英雄先验排名（top5=S / top12=A / 榜内=B）+ 语境修正
        （与当前路线一致 +1 档上限 S；"升级：X"型标注依赖已购装备）。
        """
        taken = taken or []
        top = self.kb.top_augments(hero_cn, 40)
        rank = {name: i for i, name in enumerate(top)}
        path = self.infer_current_path([], taken)  # 海克斯阶段一般未购装，用台账偏移
        out = []
        for name in card_names:
            if not name or not self.kb.valid_augment(name):
                out.append({"name": name or "?", "grade": "?", "reason": "识别失败"})
                continue
            i = rank.get(name)
            if i is None:
                grade, reason = "C", "该英雄先验榜外，谨慎选取"
            elif i < 5:
                grade, reason = "S", f"该英雄海克斯第{i + 1}位"
            elif i < 12:
                grade, reason = "A", f"该英雄海克斯第{i + 1}位"
            else:
                grade, reason = "B", f"该英雄海克斯第{i + 1}位"
            tag_info = self.kb.augment_tag_info(name)
            bias = tag_info.get("path_bias") or AUGMENT_PATH_BIAS.get(name)
            if bias and path and bias == path and grade != "S":
                grade = "A"
                reason += f"，与当前{path.upper()}方向一致"
            if bias and path and bias != path:
                reason += f"（偏{bias.upper()}，注意路线）"
            if tag_info.get("is_upgrade"):
                reason += "（升级型：效果取决于已购装备）"
            out.append({"name": name, "grade": grade, "reason": reason})
        return out

    # ── 3c. 小件合成判断 ─────────────────────────────────────────

    def component_advice(self, my_items: list, current_path: str,
                         game_map: str = "", prefer: list = None,
                         hero: str = "", threat=None):
        """身上组件 → (建议合成目标 | None, 提示语)

        目标不能是已拥有的装备（真机 2026-09-27：心之钢已买出、身上是
        第二件巨人腰带，按 into 数据序把心之钢又顶回第①位）。prefer 给
        当前推荐序列时优先从中选（腰带往正在攒的坦克核心件合），否则
        兜底按"OP.GG 池内目标优先（高频序）→ 敌方抗性方向 → 原数据序"排
        （hero 形参供池查询；心之钢旁路案 2026-09-28：数据序把池外千变者
        贾修顶到池内核心件前；二番案 2026-09-29：主线换新后双目标均池外，
        into 数据序仍让纯生命的心之钢压过千变者贾修——补抗性方向层，
        敌方 3 法伤时魔抗件先出，无敌情保持数据序不变）。
        """
        owned = set(my_items)
        advice = threat.resist_advice() if threat is not None else ""
        want_tag = {"魔抗优先": "SpellBlock",
                    "护甲优先": "Armor"}.get(advice, "")

        def _targets(item):
            info = self.kb.items.get(item)
            if not info or info["gold"] >= LEGENDARY_GOLD:
                return []
            out = []
            for tid in info.get("into", []):
                target = self.kb.item_id_to_name.get(tid)
                if (target and target not in owned
                        and self.kb.item_available(target, game_map)
                        and not _is_fullstat_item(self.kb, target)
                        and (not current_path
                             or self.kb.item_path_bias(target) == current_path)):
                    out.append(target)
            return out

        by_item = {item: _targets(item) for item in my_items}
        if prefer:
            for target in prefer:
                for item in my_items:
                    if target in by_item[item]:
                        return target, (f"身上【{item}】可合成【{target}】，"
                                        f"与当前路线一致")
        pairs = [(item, t) for item in my_items for t in by_item[item]]
        if pairs:
            pool_rank = {n: i for i, n in
                         enumerate(self.kb.core_item_pool(hero))}
            # 池内目标按高频序排前；池外目标先按敌方抗性方向（魔抗/护甲
            # 件前移），同向内保持原数据序（稳定排序）
            pairs.sort(key=lambda p: (
                0 if p[1] in pool_rank else 1,
                pool_rank.get(p[1], 0),
                1 if want_tag and want_tag not in self.kb.items.get(
                    p[1], {}).get("tags", []) else 0))
            item, target = pairs[0]
            note = f"身上【{item}】可合成【{target}】，与当前路线一致"
            if want_tag and want_tag in self.kb.items.get(
                    target, {}).get("tags", []):
                side, dmg = (("魔抗", "法") if want_tag == "SpellBlock"
                             else ("护甲", "物"))
                note += f"（敌方{dmg}伤成型，优先补{side}）"
            return target, note
        return None, "无待合成小件，攒钱即可"

    # ── 3f. 攒钱决策（v4.5：这波回城攒大件 vs 先买小件） ──────────

    def saving_advice(self, my_items: list, gold: float, seq: list,
                      game_map: str = "") -> str:
        """下一大件（seq 首位）差价与组件可买性 → 建议行附注。

        口径与 _apply_afford 一致：差价 = 总价 − 现金 − 身上已拥有的该件
        组件价值。分支：
        - 差价 ≤ SAVE_GOLD_THRESHOLD 且没有现金能买的未拥有组件 → 攒
          （差价约一波兵收入，整件成型优先）；
        - 有现金能买的未拥有组件 → 建议买"最贵可负担"组件（同等现金
          抵扣更多、格子效率最高），写清剩多少金；
        - 差价还大且无组件可买 → 攒钱中（中性进度提示）。
        整件已买得起 / 金币未知 → 空串（金币感知行已说清，不重复）。
        """
        if not seq or gold <= 0:
            return ""
        target = seq[0]
        if not target or not target.price:
            return ""
        info = self.kb.items.get(target.name, {})
        frm = {self.kb.item_id_to_name.get(f)
               for f in info.get("from", [])} - {None}
        owned = set(my_items)
        discount = sum(self.kb.items.get(c, {}).get("gold", 0)
                       for c in frm & owned)
        gap = target.price - gold - discount
        if gap <= 0:
            return ""
        # 未拥有、当前地图有售、现金买得起的组件（"高性价比"取最贵：
        # 同样的钱抵扣更多合成款，不买 435G 碎件浪费格子）
        affordable = []
        for comp in frm - owned:
            cinfo = self.kb.items.get(comp)
            if not cinfo or cinfo["gold"] <= 0:
                continue
            if not self.kb.item_available(comp, game_map):
                continue
            if cinfo["gold"] <= gold:
                affordable.append((comp, cinfo["gold"]))
        if gap <= SAVE_GOLD_THRESHOLD and not affordable:
            return f"💰 距{target.name}还差{gap:.0f}，可攒"
        if affordable:
            comp, cprice = max(affordable, key=lambda t: t[1])
            return f"💰 建议买小件【{comp}】{cprice}G，剩{gold - cprice:.0f}金"
        return f"💰 距{target.name}还差{gap:.0f}，先攒钱"
