#!/usr/bin/env python
"""生成知识库派生数据（纯标准库，可重复运行）：

1. hexa/knowledge/augment_names.json      —— 从 hero_augments.csv 提取 229 海克斯唯一名
2. hexa/knowledge/champ_damage_profile.json —— 从 DD champion.json 生成英雄伤害类型粗分类

用法: python scripts/gen_champ_profile.py
"""
import csv
import json
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOW = ROOT / "hexa" / "knowledge"
DATA = ROOT / "data"

# ── 1. 海克斯唯一名单 ────────────────────────────────────────────

def gen_augment_names():
    names = set()
    with open(DATA / "hero_augments.csv", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # 表头
        for row in reader:
            if len(row) >= 6 and row[5].strip():
                names.add(row[5].strip())
    out = {
        "count": len(names),
        "names": sorted(names),
    }
    path = KNOW / "augment_names.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[OK] augment_names.json: {len(names)} 个唯一海克斯名 -> {path}")

# ── 2. 英雄伤害粗分类画像 ─────────────────────────────────────────
# P0 启发式（P1 用 DD 精化/特例覆盖）：
#   damage  按 tags 顺序 Marksman>Mage>Assassin>Support>Fighter 映射（Tank 单独只置 is_tank）
#   is_tank 'Tank' in tags

TAG_DAMAGE_PRIORITY = [
    ("Marksman", "ad"),
    ("Mage", "ap"),
    ("Assassin", "ad"),
    ("Support", "ap"),
    ("Fighter", "ad"),
]

# 已知与启发式结论冲突的特例（当前版本主流玩法），P1 持续精化。
# 值为 dict：damage 覆写 ap/ad，is_tank 覆写坦度；键 = DD 英雄 key（非中文名）
MANUAL_OVERRIDES = {
    # ── 修正启发式误判（覆盖后结论实际翻转）────────────────────────
    "Akali": {"damage": "ap"},        # 离群之刺：全技能魔法加成，主流 AP（仅 Assassin 标签被误判 ad）
    "Azir": {"damage": "ap"},         # 沙漠皇帝：纯 AP 法核（Mage+Marksman 被 Marksman 优先级抢成 ad）
    "TwistedFate": {"damage": "ap"},  # 卡牌大师：主流 AP（同为 Mage+Marksman 被 Marksman 抢成 ad）
    "Corki": {"damage": "ap"},        # 英勇投弹手：被动普攻 80% 转魔法且技能全魔法，主流魔法伤害
    "Teemo": {"damage": "ap"},        # 迅捷斥候：Q/毒/蘑菇全魔法，主流 AP（Marksman 标签误导）
    "Kayle": {"damage": "ap"},        # 正义天使：主流纳什之牙混装，输出以魔法为主
    "Diana": {"damage": "ap"},        # 皎月女神：纯 AP 突进法师（tags 无 Mage 被误判 ad）
    "Fizz": {"damage": "ap"},         # 潮汐海灵：全技能魔法，纯 AP 刺客（Assassin+Fighter 无 Mage 标签）
    "Gwen": {"damage": "ap"},         # 灵罗娃娃：Q 剪刀全魔法含真伤，纯 AP（仅 Fighter 标签）
    "Shaco": {"damage": "ap"},        # 恶魔小丑：经典双修，大乱斗主流 AP 盒子流
    "Zac": {"damage": "ap", "is_tank": True},   # 生化魔人：全技能魔法伤害的 AP 坦
    "DrMundo": {"damage": "ap", "is_tank": True},  # 祖安狂人：Q/R 魔法百分比伤害为主
    "Rammus": {"damage": "ap"},       # 披甲龙龟：反伤 W/大招全魔法（仅 Tank 标签兜底成 ad）
    # ── 显式锚定（启发式当前同值，防 DD 标签顺序/优先级表漂移）────────
    "Evelynn": {"damage": "ap"},      # 痛苦之拥：纯 AP 刺客
    "Katarina": {"damage": "ap"},     # 不祥之刃：全技能魔法
    "Kennen": {"damage": "ap"},       # 狂暴之心：技能全魔法的 AP 法核
    "Mordekaiser": {"damage": "ap"},  # 铁铠冥魂：纯 AP 战士
    "Rumble": {"damage": "ap"},       # 机械公敌：全技能魔法
    "Singed": {"damage": "ap"},       # 炼金术士：毒雾魔法伤害的 AP 坦
    "Ezreal": {"damage": "ad"},       # 探险家：经典双修，主流 AD 魔宗
    "Kaisa": {"damage": "ad"},        # 虚空之女：经典双修，默认 AD 特效流
    "Jax": {"damage": "ad"},          # 武器大师：混伤但主流 AD 武器
    "Sion": {"is_tank": True},        # 亡灵战神：主流坦克（Q/E 物理伤害为主，保持 ad）
}

def gen_damage_profile():
    raw = json.loads((KNOW / "champions_raw.json").read_text(encoding="utf-8"))
    version = raw.get("version", "")
    profile = {}
    for key, c in raw["data"].items():
        tags = c.get("tags", [])
        damage = None
        for tag, d in TAG_DAMAGE_PRIORITY:
            if tag in tags:
                damage = d
                break
        if damage is None:
            damage = "ad"  # 兜底
        is_tank = "Tank" in tags
        # 人工特例覆盖：与 tags 启发式冲突时以人工结论为准（P1 持续精化）
        ov = MANUAL_OVERRIDES.get(key)
        if ov:
            damage = ov.get("damage", damage)
            is_tank = ov.get("is_tank", is_tank)
        profile[c["name"]] = {  # 键 = 中文名（与 champions.json/LCU 一致）
            "key": key,
            "damage": damage,
            "is_tank": "Tank" in tags,
            "tags": tags,
        }
    out = {
        "version": version,
        "generated_at": datetime.date.today().isoformat(),
        "note": "P0 启发式粗分类（DD tags 推断）+ 23 条人工特例覆盖（双修/无 Mage 标签 AP 等）",
        "champions": profile,
    }
    path = KNOW / "champ_damage_profile.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    ap = sum(1 for v in profile.values() if v["damage"] == "ap")
    ad = sum(1 for v in profile.values() if v["damage"] == "ad")
    tk = sum(1 for v in profile.values() if v["is_tank"])
    print(f"[OK] champ_damage_profile.json: {len(profile)} 英雄 (ap={ap} ad={ad} 坦克={tk}) -> {path}")

if __name__ == "__main__":
    KNOW.mkdir(parents=True, exist_ok=True)
    gen_augment_names()
    gen_damage_profile()
