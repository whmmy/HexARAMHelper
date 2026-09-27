"""层 0 确定性规则引擎（计划 §5.2 / §5.3）

职责：
1. current_path 确定性推断 —— 已购装备 + 已选海克斯 → ap/ad/tank（防 System1 路线漂移）
2. 敌方威胁画像 —— 英雄伤害粗分类 + 装备扫描（无 per-player 实时伤害数据，推断式）
3. 三类候选池构造 —— 出门装 / 下一件 / 小件合成
   （P1 起：下一件用英雄 OP.GG 高频装，出门装用 OP.GG starter_items；
    P2 由 ChampionContext + System1 排序接管，本引擎仍为离线兜底）
4. 海克斯时刻规则评级（S/A/B：先验排名 + 语境修正合成评分，非胜率分桶）

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
    price: int = 0          # 装备总价（DD gold）
    afford_note: str = ""   # 金币感知标注（"可购买"/"还差N"），由 GameSession 按序累计填写


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
        if bias:  # none/其他 → 无偏移，落回内置小表兜底
            return AUGMENT_PATH_BIAS.get(name)
        return AUGMENT_PATH_BIAS.get(name)

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
            bias = self.augment_path_bias(aug)
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
        return tp

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
                             limit: int = 3, game_map: str = "") -> list:
        """在 current_path 内、排除已拥有、按敌方构成加权的传说件候选。

        P1 候选池 = 英雄 OP.GG 高频核心装（顺序即登场率），坦克路线另开放
        DD 抗性件全库扫描（输出位不因敌方构成推抗性装）。分数 = OP.GG 高频
        (3→0) + 路线匹配(2) + 敌方抗性方向(2) + 正在合成(3)。
        game_map: "11"/"12"，当前地图不可买的名字直接出局。
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
                score += 3.0  # 正在合成的线优先
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
                       limit: int = 3, game_map: str = "") -> list:
        """出装主线序列（v4.3）：英雄核心套装线优先，敌情评分补位。

        v4.2 按"敌情加权评分"直接排会出"还没出核心就被阵容带偏"的序列；
        v4.3 改为：①按当前路线选英雄的核心组合（kb.core_build_line，
        爆杀流/半肉/法系线由路线决定），未拥有的核心件按登场序占满前排；
        ②核心出完或不足 limit 再用 next_item_candidates（敌情/合成线/其余
        高频件）补位。每件带价格，可买性由 GameSession 按金币累计填写。
        """
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
                                           game_map=game_map):
            if c.name in seen:
                continue
            seen.add(c.name)
            if not c.price:
                c.price = self.kb.items.get(c.name, {}).get("gold", 0)
            seq.append(c)
            if len(seq) >= limit:
                break
        return seq

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
                         game_map: str = "", prefer: list = None):
        """身上组件 → (建议合成目标 | None, 提示语)

        目标不能是已拥有的装备（真机 2026-09-27：心之钢已买出、身上是
        第二件巨人腰带，按 into 数据序把心之钢又顶回第①位）。prefer 给
        当前推荐序列时优先从中选（腰带往正在攒的坦克核心件合），否则按
        合成链数据序兜底。
        """
        owned = set(my_items)

        def _targets(item):
            info = self.kb.items.get(item)
            if not info or info["gold"] >= LEGENDARY_GOLD:
                return []
            out = []
            for tid in info.get("into", []):
                target = self.kb.item_id_to_name.get(tid)
                if (target and target not in owned
                        and self.kb.item_available(target, game_map)
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
        for item in my_items:
            if by_item[item]:
                target = by_item[item][0]
                return target, (f"身上【{item}】可合成【{target}】，"
                                f"与当前路线一致")
        return None, "无待合成小件，攒钱即可"
