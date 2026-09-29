"""满装误判/金铲铲推荐问题：真机案例 × 修复后行为对照（只读诊断脚本）。

2026-09-28 两局问题（炽炎雏龙 22:21-22:35 / 殇之木乃伊 22:42-22:51）的
根因已在产品代码修复，本脚本从"复现 bug"转为"修复后行为对照"：各
[assert] 固化的是修复后语义，行为回退（再出现满装误报/金铲铲推荐）
时这里会红。

Part A  炽炎雏龙局：1 传说+鞋+魄罗佳肴+组件凑满 6 格曾被判满装
        （卖夺萃之镰 → 钢铁烈阳之匣/金铲铲/败魔）。修复后
        permanent_items 剔除 gold=0 且无 tags 的占位条目（魄罗佳肴等
        全库 7 条），is_full_build 还要求传说档非靴件 ≥5 → 该局面回
        买装流，替换建议不再触发。
Part B  木木局（殇之木乃伊/TANK）：3 传说+2 组件+鞋 = 6 占格曾被判
        满装（截图三条替换：卖日炎圣盾 → 钢铁烈阳之匣(差价240)/
        金铲铲(540)/败魔(940)）。修复后 is_full_build=False →
        build_sequence 保持买装（Candidate 流），替换池也不再含
        金铲铲（P0-3 全家桶 tags 守卫）。
Part C  心之钢推线来源：core_build_line 选中组合 + building_into
        条件加分（池内 +3 / 池外 +1，P2-8）+ component_advice
        池内目标优先（hero 形参）三条路径的修复后行为。
Part D  金铲铲数据定位：KB 规范条目仍是全家桶 tags（9 个属性家族
        tag 命中 FULLSTAT_FAMILY_TAGS），推荐池过滤后不再出现在
        任何"下一件/替换买池"（仅退出推荐池，格计数/台账/幻觉校验
        口径不变）。

运行：.venv/Scripts/python.exe experiments/repro_fullbuild.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexa.knowledge.kb import KnowledgeBase
from hexa.core.rules_engine import (RulesEngine, ThreatProfile,
                                    FULLSTAT_FAMILY_TAGS, _is_fullstat_item,
                                    is_full_build)

kb = KnowledgeBase()
rules = RulesEngine(kb)

_result = {"pass": 0, "fail": 0}


def expect(desc, cond, detail=""):
    """修复后行为断言：失败计入退出码（供 CI/手工跑回归）。"""
    ok = bool(cond)
    _result["pass" if ok else "fail"] += 1
    print(f"  [assert] {desc}: {'✅' if ok else '❌ ' + str(detail)}")


def divider(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ── Part A：炽炎雏龙局（修复后：不再误判满装） ────────────────────
divider("Part A 炽炎雏龙局 22:33 时点还原（修复后回买装流）")
log = json.loads(Path("log/games/20260928_222132_炽炎雏龙.json")
                 .read_text(encoding="utf-8"))
threat_a = ThreatProfile(ap_count=3, ad_count=2, tank_count=1)  # 日志 threat 同款
path_a = log["current_path"]                                    # "ad"

print("final_items:", log["final_items"])
placeholders = sorted(n for n, v in kb.items.items()
                      if not v.get("gold") and not v.get("tags"))
print("KB 占位条目（gold=0 且 tags=[]，permanent_items 剔除）:",
      len(placeholders), placeholders)

# 22:33 时点还原（复盘日志只存 final_items，两个组件位未知，任意组件补齐）
recon = ["夺萃之镰", "暴食胫甲", "魄罗佳肴", "狂热", "红水晶", "布甲"]
n_perm = len(kb.permanent_items(recon))
print(f"\n[还原] permanent={n_perm}（魄罗佳肴已剔除） → "
      f"is_full_build={is_full_build(kb, recon)}")
expect("魄罗佳肴不入占格（permanent 5/6）", n_perm == 5, n_perm)
expect("1 传说+鞋+组件凑格不判满装", not is_full_build(kb, recon))
seq_a = rules.build_sequence(recon, path_a, threat_a, hero=log["hero"],
                             game_map="12")
kinds_a = {type(c).__name__ for c in seq_a}
print("build_sequence:", [(c.name, type(c).__name__) for c in seq_a[:3]])
expect("回买装流（Candidate，无 SwapCandidate）",
       seq_a and kinds_a == {"Candidate"}, kinds_a)
expect("建议中无替换文案（金铲铲/卖X换Y 均消失）",
       "金铲铲" not in {c.name for c in seq_a})

# 结算快照口径（final 5 件 + 狂热）同样不再判满装：3 传说 < 5
recon2 = log["final_items"] + ["狂热"]
expect(f"结算快照口径（permanent={len(kb.permanent_items(recon2))}）"
       f"同样不判满装", not is_full_build(kb, recon2))

# ── Part B：木木局（修复后：买装流 + 替换池无金铲铲） ─────────────
divider("Part B 木木局（殇之木乃伊/TANK）修复后行为")
hero_b = "殇之木乃伊"
items_b = ["日炎圣盾", "心之钢", "无终恨意", "棘刺背心", "巨人腰带", "暴食胫甲"]
threat_b = ThreatProfile(ap_count=3, ad_count=2, tank_count=1)  # 法伤×3|物伤×2|坦克×1

n_perm_b = len(kb.permanent_items(items_b))
legendary_b = sum(1 for n in items_b
                  if kb.items[n]["gold"] >= 2200
                  and "Boots" not in kb.items[n]["tags"])
print("身上装备:", items_b)
print(f"permanent={n_perm_b}，传说档非靴件={legendary_b}/6 "
      f"→ is_full_build={is_full_build(kb, items_b)}")
expect("3 传说+2 组件+鞋不判满装（修复前 6 占格即满装）",
       n_perm_b == 6 and not is_full_build(kb, items_b))

seq_b = rules.build_sequence(items_b, "tank", threat_b, hero=hero_b,
                             game_map="12")
kinds_b = {type(c).__name__ for c in seq_b}
print("build_sequence:", [(c.name, type(c).__name__) for c in seq_b[:3]])
expect("保持买装流（修复前是替换流）", kinds_b == {"Candidate"}, kinds_b)
expect("建议全集无金铲铲", "金铲铲" not in {c.name for c in seq_b})

tgt_b, note_b = rules.component_advice(items_b, "tank", game_map="12",
                                       hero=hero_b)
print("合成提示:", note_b)
expect("棘刺背心→荆棘之甲（OP.GG 池内目标优先，P2-8 hero 形参）",
       tgt_b == "荆棘之甲" and "棘刺背心" in note_b, (tgt_b, note_b))

buy_pool_b = rules._replacement_buy_pool(set(), "tank", threat_b,
                                         hero_b, "12")
print("替换买池 top6:", [(n, s) for s, n, _r in buy_pool_b[:6]])
expect("替换池无金铲铲（修复前 3.0 同分进 top3）",
       "金铲铲" not in [n for _s, n, _r in buy_pool_b])

# ── Part C：tank 阿木木的心之钢路径（修复后） ─────────────────────
divider("Part C 心之钢进入建议的路径（修复后口径）")
builds = kb.build_for(hero_b)
pool = kb.core_item_pool(hero_b)
print("amumu.json fetched_at:", builds.get("fetched_at"),
      "| core_builds ×", len(builds.get("core_builds", [])))
for i, combo in enumerate(builds.get("core_builds", []), 1):
    votes = sum(1 for n in combo if kb.item_path_bias(n) == "tank")
    print(f"  组合{i}: {combo} → tank 票数={votes}")
line = kb.core_build_line(hero_b, "tank")
print("core_build_line(hero, 'tank') 选中:", line)
print("core_item_pool:", pool)
print("心之钢在池内:", "心之钢" in pool,
      "（P2-8 后：池外目标 building_into 只 +1，不再与池内核心同权）")

nxt1 = rules.next_item_candidates(["女神之泪", "日炎圣盾", "末日寒冬",
                                   "无终恨意"], "tank", ThreatProfile(),
                                  hero=hero_b, game_map="12")
print("①核心线出完、敌情未知 → 下一件top3:", [c.name for c in nxt1[:3]])
expect("① 下一件全部为池内/路线内件（无金铲铲类杂项）",
       bool(nxt1) and all(not _is_fullstat_item(kb, c.name) for c in nxt1))

nxt2 = rules.next_item_candidates(["巨人腰带"], "tank", threat_b,
                                  hero=hero_b, game_map="12")
print("②身上【巨人腰带】+敌方法伤×3 → 下一件top3:",
      [(c.name, c.reason) for c in nxt2[:3]])
expect("② 千变者贾修（魔抗+合成线）仍在 top3（池外 +1 后排序后移但可及）",
       "千变者贾修" in [c.name for c in nxt2[:3]],
       [c.name for c in nxt2[:3]])
tgt3, note3 = rules.component_advice(["巨人腰带"], "tank", game_map="12",
                                     hero=hero_b)
print("③身上【巨人腰带】合成提示（无敌情）:", note3)
expect("③ 无敌情保持数据序兜底=心之钢（向后兼容）", tgt3 == "心之钢", tgt3)
tgt3b, note3b = rules.component_advice(["巨人腰带"], "tank", game_map="12",
                                       hero=hero_b, threat=threat_b)
print("③b身上【巨人腰带】合成提示（敌方法伤×3）:", note3b)
expect("③b 敌方法伤×3 改推千变者贾修（魔抗方向，心之钢旁路闭合）",
       tgt3b == "千变者贾修", (tgt3b, note3b))
starter = kb.starter_combos(hero_b)
print("出门装路径 starter_combos:", starter,
      "（不含心之钢，排除该路径）")

# ── Part D：金铲铲（全家桶 tags 杂项退出推荐池） ──────────────────
divider("Part D 金铲铲：P0-3 全家桶 tags 守卫")
info = kb.items.get("金铲铲", {})
inter = len(set(info.get("tags", [])) & FULLSTAT_FAMILY_TAGS)
print("KB 规范条目: gold=%s sell=%s maps=%s 可买(12)=%s"
      % (info.get("gold"), info.get("sell"), info.get("maps"),
         kb.item_available("金铲铲", "12")))
print("KB tags(%d): %s" % (len(info.get("tags", [])), info.get("tags")))
print("属性家族交集:", inter, "→ _is_fullstat_item:", _is_fullstat_item(kb, "金铲铲"))
hits = sorted(n for n in kb.items if _is_fullstat_item(kb, n))
expect("守卫命中集 = 属性加成/金铲铲（全库仅 2 条，正常件交集 ≤5）",
       hits == ["属性加成", "金铲铲"], hits)
expect("金铲铲仍在 KB/可买口径（守卫只进推荐池，不删数据）",
       kb.valid_item("金铲铲") and kb.item_available("金铲铲", "12"))
expect("金铲铲不进 next_item_candidates",
       "金铲铲" not in [c.name for c in rules.next_item_candidates(
           [], "tank", threat_b, hero=hero_b, game_map="12")])
expect("属性加成不进替换买池",
       "属性加成" not in [n for _s, n, _r in buy_pool_b])

print(f"\n==== 修复后行为对照: {_result['pass']} 通过 / "
      f"{_result['fail']} 失败 ====")
sys.exit(1 if _result["fail"] else 0)
