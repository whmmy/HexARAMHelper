"""装备库对账（离线审计：items_raw.json × kb 入库规则，不改数据）

用法：
  uv run python scripts/reconcile_items.py

背景（虚空献祭案 2026-09-29）：DD/游戏客户端数据都把海斗"任务合成件"
标成 map12 可买（inStore=True），字段层面无法与货架件区分（CDT zh_cn
v1/items.json 逐字段对比过：categories/inStore/displayInItemSets 全同），
只能靠结构判据 + 定期人核。本脚本固化对账四项，重抓 DD 后跑一遍复查：

  1. 任务/特殊件守卫命中——仅 map12 可买主张 + 无 from 合成链
     （kb._load_items 已按此摘除可买地图；这里列命中名单供人核，
       价格 ≥5000G 的按"任务终极件"对待，低价的留意误伤）
  2. 库内不可直购件（maps=[]）清单——任务件 / 升级件（魔切类）/
     已移除/特殊获取，确认"留在库里"的理由（台账/幻觉校验/卖价）成立
  3. 大乱斗同名价差——map12 条目价 vs 库内取价（dedup 峡谷优先；
       心之钢 3000/2500 类，是否有真机货架价证据后再动）
  4. OP.GG builds 引用但库外装备名——新装备未入库或命名不一致
     （core_item_pool 防御性过滤会静默丢件，这里点名）

退出码：0=对账通过（任务件全部摘除可买地图）；1=守卫失效。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.config import DATA_DIR, KNOW_DIR  # noqa: E402
from hexa.knowledge.kb import KnowledgeBase  # noqa: E402

RAW_PATH = KNOW_DIR / "items_raw.json"
_TAG_HTML_RE = re.compile(r"<[^>]+>")


def main():
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    entries = raw.get("data", {})
    kb = KnowledgeBase()
    issues = 0

    # ── 1. 任务/特殊件守卫命中 ────────────────────────────────────
    buy11, buy12, from12 = set(), {}, {}
    for iid, it in entries.items():
        name = (it.get("name") or "").strip()
        if not name or iid.startswith("77"):
            continue
        if not (it.get("gold") or {}).get("purchasable"):
            continue
        maps = it.get("maps") or {}
        if maps.get("11"):
            buy11.add(name)
        if maps.get("12"):
            buy12.setdefault(name, []).append(iid)
            if it.get("from"):
                from12[name] = True
    guarded = sorted(_TAG_HTML_RE.sub("", n) for n in buy12
                     if n not in buy11 and not from12.get(n))
    print(f"\n[1] 任务/特殊件守卫命中 {len(guarded)} 件（kb 已摘除可买地图）")
    for n in guarded:
        gold = kb.items.get(n, {}).get("gold", 0)
        mark = " ← 任务终极件（≥5000G）" if gold >= 5000 else ""
        print(f"    {n}（{gold}G）{mark}")

    # ── 2. 库内不可直购件（maps=[]）按原因分类 ────────────────────
    nobuy = {"任务件": [], "商店不可直购（升级/已移除/特殊获取）": [],
             "其它": []}
    guarded_set = set(guarded)
    for name, info in kb.items.items():
        if info["maps"]:
            continue
        if name in guarded_set:
            nobuy["任务件"].append(name)
            continue
        iid = info.get("id", "")
        it = entries.get(iid, {})
        if not (it.get("gold") or {}).get("purchasable"):
            nobuy["商店不可直购（升级/已移除/特殊获取）"].append(name)
        else:
            nobuy["其它"].append(name)
    print("\n[2] 库内不可直购件（maps=[]，保留供台账/校验/卖价）")
    for why, names in nobuy.items():
        print(f"    {why} {len(names)}: {'、'.join(sorted(names))}")

    # ── 3. 大乱斗同名价差 ─────────────────────────────────────────
    diff = []
    for name, info in kb.items.items():
        kb_price = info.get("gold", 0)
        if not kb_price:
            continue
        for iid in buy12.get(name, []):
            p = (entries[iid].get("gold") or {}).get("total") or 0
            if p and p != kb_price:
                diff.append((name, kb_price, p, iid))
    print(f"\n[3] 大乱斗条目价 ≠ 库内取价 {len(diff)} 件（货架价待核）")
    for name, kb_price, p, iid in sorted(diff):
        print(f"    {name}: 库内 {kb_price}G vs map12 条目 {iid}={p}G")

    # ── 4. OP.GG builds 引用但库外装备名 ──────────────────────────
    outside = {}
    for path in sorted((DATA_DIR / "builds").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        names = []
        for combo in (data.get("core_builds") or []):
            names += list(combo)
        for combo in (data.get("starter_items") or []):
            names += list(combo)
        for combo in (data.get("boots") or []):
            names += list(combo)
        for n in names:
            if n and not kb.valid_item(n):
                outside.setdefault(n, set()).add(data.get("hero_cn") or "?")
    print(f"\n[4] OP.GG 引用但库外装备名 {len(outside)} 个（候选池会静默丢弃）")
    for n, heroes in sorted(outside.items()):
        print(f"    {n} ← {'、'.join(sorted(heroes)[:5])}")

    # ── 汇总 ─────────────────────────────────────────────────────
    # 守卫失效判定：任务件里只要有一件在大乱斗图仍"可买"，
    # 就说明 kb 摘除逻辑没兜住（虚空献祭案复发信号）。
    hard = [n for n in set(guarded)
            if kb.valid_item(n) and kb.item_available(n, "12")]
    print(f"\n{'=' * 56}")
    if hard:
        print(f"[!] 对账不通过：{'、'.join(hard)} 仍可推荐（守卫失效？）")
    else:
        print("[OK] 对账通过：任务件均已摘除可买地图，无混入可推荐池")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
