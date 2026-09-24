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

# 已知与启发式结论冲突的特例（当前版本主流玩法），P1 持续精化
MANUAL_OVERRIDES = {
    # 例: "Kaisa": "ad",   # 卡莎双修，主流 AD
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
        if key in MANUAL_OVERRIDES:
            damage = MANUAL_OVERRIDES[key]
        profile[c["name"]] = {  # 键 = 中文名（与 champions.json/LCU 一致）
            "key": key,
            "damage": damage,
            "is_tank": "Tank" in tags,
            "tags": tags,
        }
    out = {
        "version": version,
        "generated_at": datetime.date.today().isoformat(),
        "note": "P0 启发式粗分类（DD tags 推断，非逐英雄校准），P1 精化",
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
