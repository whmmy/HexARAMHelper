"""层 0 确定性规则引擎 v1（计划 §5.2 / §5.3）

三块职责（P0 粒度，P1/P2 数据到位后增强）：
1. current_path 确定性推断 —— 已购装备 + 已选海克斯 → ap/ad/tank（防 System1 路线漂移）
2. 敌方威胁画像 —— 英雄伤害粗分类 + 装备扫描（无 per-player 实时伤害数据，推断式）
3. 三类候选池构造 —— 出门装 / 下一件 / 小件合成（P0 规则降级版，
   P2 由 ChampionContext + OP.GG builds 接管）

铁律：数据出事实，模型出判断 —— 本模块全部确定性，无随机无 AI。
"""
from dataclasses import dataclass, field

# ── 知名海克斯 → 路线偏移（P0 手工小表；P1 由语义标签库替换） ──────
# 注：键名以真机 229 海克斯名单实测为准校准（"心之刚"为计划示例名，
# 当前 hero_augments.csv 名单中未收录，实测后统一修正）。
AUGMENT_PATH_BIAS = {
    "心之刚": "tank",
    "不灭之握": "tank",
    "贪欲束缚": "ad",
}

# 重伤装备名（P0 无语义标签时的直接名字匹配，P1 换语义标签库）
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


@dataclass
class ThreatProfile:
    ap_count: int = 0
    ad_count: int = 0
    tank_count: int = 0
    healers: list = field(default_factory=list)
    key_items: list = field(default_factory=list)  # [{"champion", "item", "note"}]

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
            parts.append(f"关键装:{self.key_items[0]['item']}")
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


class RulesEngine:
    def __init__(self, kb):
        self.kb = kb

    # ── 1. current_path 确定性推断 ────────────────────────────────

    def infer_current_path(self, my_items: list, augments: list) -> str:
        """已购装备（按 DD 标签投票，传说件权重 ×2）+ 已选海克斯偏移 → ap/ad/tank

        空输入（出门装前）返回 ""（路线未定，出门装阶段不约束）。
        """
        votes = {"ap": 0.0, "ad": 0.0, "tank": 0.0}
        for item in my_items:
            bias = self.kb.item_path_bias(item)
            if not bias:
                continue
            weight = 2.0 if self.kb.items.get(item, {}).get("gold", 0) >= LEGENDARY_GOLD else 1.0
            votes[bias] += weight
        for aug in augments:
            bias = AUGMENT_PATH_BIAS.get(aug)
            if bias:
                votes[bias] += 1.5  # 海克斯偏移权重：介于小件与传说件之间
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

    def threat_profile(self, enemy_champions: list, enemy_players: list = None) -> ThreatProfile:
        """enemy_champions: 敌方英雄中文名列表；
        enemy_players: [PlayerInfo]（Live 快照敌方，可为空 = 游戏早期/无数据）
        """
        tp = ThreatProfile()
        for cn in enemy_champions:
            prof = self.kb.champ_profile.get(cn, {})
            if prof.get("damage") == "ap":
                tp.ap_count += 1
            elif prof.get("damage") == "ad":
                tp.ad_count += 1
            if prof.get("is_tank"):
                tp.tank_count += 1
            if cn in HEALERS:
                tp.healers.append(cn)
        for p in (enemy_players or []):
            for it in p.items:
                note = KEY_ENEMY_ITEMS.get(it.get("name", ""))
                if note:
                    tp.key_items.append({
                        "champion": p.champion, "item": it["name"], "note": note})
        return tp

    # ── 3a. 出门装候选池（P0 规则降级版） ─────────────────────────

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

    def opening_candidates(self, my_hero: str, first_augment: str = None) -> list:
        """出门装三选。P2 由 ChampionContext.opening_items 接管（含海克斯联动）；
        P0 先兑现最知名的联动规则：心之刚 → 巨人腰带。"""
        if first_augment == "心之刚":
            return [Candidate("巨人腰带", "已选【心之刚】：腰带直接放大其成长收益"),
                    Candidate("红水晶", "心之刚联动的小件版，经济紧张时"),
                    Candidate("多兰之刃", "无视联动的标准出门")]
        path = self.kb.champ_profile.get(my_hero, {}).get("damage", "")
        return [Candidate(n, r) for n, r in self.OPENING_POOL.get(path, self.OPENING_POOL[""])]

    # ── 3b. 下一件候选池（DD 装备库过滤 + 敌方构成加权） ───────────

    def next_item_candidates(self, my_items: list, current_path: str,
                             threat: ThreatProfile, limit: int = 3) -> list:
        """在 current_path 内、排除已拥有、按敌方构成加权的传说件候选。

        P0 分数 = 路线匹配(2) + 敌方抗性方向(2) + 高频通用件加成(1)
        P1 替换为 OP.GG 高频装 ∪ 抗性件的英雄粒度池。
        """
        owned = set(my_items)
        # 我已有装备的升级目标（优先：正在攒的合成线）
        building_into = set()
        for item in my_items:
            for tid in self.kb.items.get(item, {}).get("into", []):
                tname = self.kb.item_id_to_name.get(tid)
                if tname:
                    building_into.add(tname)

        advice = threat.resist_advice()
        scored = []
        for name, info in self.kb.items.items():
            if name in owned or info["gold"] < LEGENDARY_GOLD:
                continue
            bias = self.kb.item_path_bias(name)
            # 主线件必须落在 current_path 内；抗性功能件（护甲/魔抗）跨路线放行
            # （"3 法师 → 魔抗↑"是路线内换装而非换路线）
            is_resist = bool({"SpellBlock", "Armor"} & set(info["tags"]))
            if current_path and bias != current_path and not is_resist:
                continue
            score = 2.0 if bias == current_path else 0.5
            tags = info["tags"]
            if advice == "魔抗优先" and "SpellBlock" in tags:
                score += 2.0
            elif advice == "护甲优先" and "Armor" in tags:
                score += 2.0
            elif advice == "重伤考虑" and name in ANTIHEAL_ITEMS:
                score += 2.0
            if name in building_into:
                score += 3.0  # 正在合成的线优先
            scored.append((score, name))

        scored.sort(reverse=True)
        out = []
        for score, name in scored[:limit]:
            reason_bits = []
            if name in building_into:
                reason_bits.append("身上小件正在往它合")
            bias = self.kb.item_path_bias(name)
            if bias == current_path:
                reason_bits.append(f"当前{current_path.upper()}路线")
            if advice:
                reason_bits.append(f"敌方{advice}")
            out.append(Candidate(name, "；".join(reason_bits) or "通用推荐"))
        return out

    # ── 3c. 小件合成判断 ─────────────────────────────────────────

    def component_advice(self, my_items: list, current_path: str):
        """身上组件 → (建议合成目标 | None, 提示语)"""
        for item in my_items:
            info = self.kb.items.get(item)
            if not info or info["gold"] >= LEGENDARY_GOLD:
                continue
            for tid in info.get("into", []):
                target = self.kb.item_id_to_name.get(tid)
                if not target:
                    continue
                tbias = self.kb.item_path_bias(target)
                if not current_path or tbias == current_path:
                    return target, f"身上【{item}】可合成【{target}】，与当前路线一致"
        return None, "无待合成小件，攒钱即可"
