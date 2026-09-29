"""威胁画像加权重定向（2026-09-29 剑圣案）：真机案例 × 修复后行为对照（只读诊断脚本）。

用户案：殇之木乃伊（TANK）局，敌方 AP/AD 2v2 打平，肥剑圣连杀用户——
威胁画像只按人头计数（旧构建泰坦还双计成法伤×3），resist_advice 给出
"魔抗优先"，替换建议整串"卖日炎圣盾换魔抗"；实际砍死用户的是 AD 剑圣，
方向完全反了。

修复后语义（本脚本固化）：
A. threat_profile 为 ap/ad 两侧累计装备估值（成型度面板同源口径），并逐敌
   登记 champ_damage（死亡实证映射方向用）；
B. resist_advice：① 被同一英雄杀 ≥2 次 → 按其实际伤害路线 +1 票（破
   "差一口气"）；② 人数打平时比两侧估值，显著肥（≥20% 且 ≥1500G）的
   一侧定向；估值接近/证据不足维持"双抗均衡"（不武断）；
C. summary 显示"死于:XX×N"；
D. live_api._process_kill_events 从 /eventdata 增量统计击杀我的英雄
   （EventID 单调去重、新局 EventID 回退清零），GameSession 挂到
   threat.killer_counts，军师同时点名"XX已N次击杀你"。

运行：.venv/Scripts/python.exe experiments/repro_threat_weight.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexa.knowledge.kb import KnowledgeBase
from hexa.core.rules_engine import RulesEngine, ThreatProfile
from hexa.core.live_api import LivePoller, PlayerInfo

kb = KnowledgeBase()
rules = RulesEngine(kb)

_result = {"pass": 0, "fail": 0}


def expect(desc, cond, detail=""):
    ok = bool(cond)
    _result["pass" if ok else "fail"] += 1
    print(f"  [assert] {desc}: {'✅' if ok else '❌ ' + str(detail)}")


def divider(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ── Part A：真机木木局数值还原（双方都肥、AD 略高、剑圣连杀） ──────
divider("Part A 真机数值：AD 23850G vs AP 22100G，剑圣×3 击杀用户")
tp_real = ThreatProfile(ap_count=2, ad_count=2,
                        ap_value=22100, ad_value=23850)
advice_no_kill = tp_real.resist_advice()
print(f"resist_advice（无死亡数据）: {advice_no_kill!r}")
expect("估值接近不武断定向（旧'魔抗优先'误报消失）",
       advice_no_kill == "双抗均衡", advice_no_kill)

tp_real.killer_counts = {"无极剑圣": 3}
tp_real.champ_damage = {"无极剑圣": "ad", "灰烬驱魔人": "ap", "迅捷斥候": "ap",
                        "深海泰坦": "", "沙漠玫瑰": "ad"}
advice_killed = tp_real.resist_advice()
print(f"resist_advice（剑圣连杀×3）: {advice_killed!r}")
expect("死亡实证破平局 → 护甲优先", advice_killed == "护甲优先",
       advice_killed)
expect("摘要显示死亡来源", "死于:无极剑圣×3" in tp_real.summary(),
       tp_real.summary())

# 挂上死亡实证后，坦克用户的下一件应出现护甲向（剑圣在砍你）
my_items = ["日炎圣盾", "心之钢", "无终恨意", "棘刺背心", "巨人腰带", "暴食胫甲"]
seq = rules.build_sequence(my_items, "tank", tp_real, hero="殇之木乃伊",
                           game_map="12")
from hexa.core.rules_engine import LEGENDARY_GOLD  # noqa: E402
top3 = [(c.name, kb.items.get(c.name, {}).get("tags", [])) for c in seq[:3]]
print("build_sequence top3:", [n for n, _t in top3])
has_armor_top3 = any("Armor" in (t or []) for _n, t in top3)
expect("护甲向建议进入 top3（对位剑圣）", has_armor_top3, top3)
expect("建议仍为买装流（3/6 不满装，无替换流）",
       seq and all(type(c).__name__ == "Candidate" for c in seq),
       [type(c).__name__ for c in seq])

# ── Part B：threat_profile 估值/路线登记集成（假快照） ─────────────
divider("Part B threat_profile 装备估值与 champ_damage 登记")
players = [
    PlayerInfo(champion="无极剑圣",
               items=[{"name": "破败王者之刃", "count": 1},
                      {"name": "鬼索的狂暴之刃", "count": 1},
                      {"name": "暴食胫甲", "count": 1}]),
    PlayerInfo(champion="迅捷斥候",
               items=[{"name": "增幅典籍", "count": 1}]),
    PlayerInfo(champion="深海泰坦",
               items=[{"name": "日炎圣盾", "count": 1},
                      {"name": "荆棘之甲", "count": 1}]),
]
tp_b = rules.threat_profile(["无极剑圣", "迅捷斥候", "深海泰坦"], players)
print(f"summary: {tp_b.summary()}")
print(f"ap_value={tp_b.ap_value:.0f} ad_value={tp_b.ad_value:.0f} "
      f"champ_damage={tp_b.champ_damage}")
expect("人数 1v1+坦克（泰坦案口径）",
       tp_b.ap_count == 1 and tp_b.ad_count == 1 and tp_b.tank_count == 1,
       tp_b.summary())
expect("AD 侧估值来自肥剑圣", tp_b.ad_value >= 5000, tp_b.ad_value)
expect("AP 侧估值来自小件 Teemo", 0 < tp_b.ap_value < tp_b.ad_value,
       tp_b.ap_value)
expect("champ_damage 逐敌登记（坦线记空串）",
       tp_b.champ_damage == {"无极剑圣": "ad", "迅捷斥候": "ap",
                             "深海泰坦": ""}, tp_b.champ_damage)

# ── Part C：估值定向阈值（≥20% 且 ≥1500G） ────────────────────────
divider("Part C 人数打平时估值定向")
cases = [
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=3000, ad_value=11000),
     "护甲优先", "AD 侧显著肥"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=11000, ad_value=3000),
     "魔抗优先", "AP 侧显著肥"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=22100, ad_value=23850),
     "双抗均衡", "估值接近（真机 7.5% 差）"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=0, ad_value=0),
     "双抗均衡", "无估值数据（游戏早期）"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=5000, ad_value=6400),
     "双抗均衡", "比率 1.28 够但差 1400G 不足 1500G"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=20000, ad_value=22000),
     "双抗均衡", "差 2000G 够但比率 1.1 不足 20%"),
    (ThreatProfile(ap_count=1, ad_count=1, ap_value=2000, ad_value=9000),
     "护甲优先", "1v1 打平同样适用"),
]
for tp_c, want, desc in cases:
    got = tp_c.resist_advice()
    expect(f"{desc} → {want}", got == want, got)

# ── Part D：死亡实证票细则 ────────────────────────────────────────
divider("Part D 死亡实证（killer_counts）细则")
d_cases = [
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=22100, ad_value=23850,
                   champ_damage={"无极剑圣": "ad"},
                   killer_counts={"无极剑圣": 2}),
     "护甲优先", "杀 2 次即实证"),
    (ThreatProfile(ap_count=2, ad_count=2,
                   champ_damage={"九尾妖狐": "ap"},
                   killer_counts={"九尾妖狐": 2}),
     "魔抗优先", "AP 击杀者反向加票"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=22100, ad_value=23850,
                   champ_damage={"九尾妖狐": "ap"},
                   killer_counts={"九尾妖狐": 1}),
     "双抗均衡", "单杀 1 次视为噪音"),
    (ThreatProfile(ap_count=2, ad_count=2, ap_value=22100, ad_value=23850,
                   champ_damage={"深海泰坦": ""},
                   killer_counts={"深海泰坦": 4}),
     "双抗均衡", "坦线击杀者不映射抗性方向"),
    (ThreatProfile(ap_count=1, ad_count=2,
                   champ_damage={"无极剑圣": "ad"},
                   killer_counts={"无极剑圣": 5}),
     "护甲优先", "2v1 落后侧也可被死亡实证拉平（ad 2+1=3）"),
]
for tp_d, want, desc in d_cases:
    got = tp_d.resist_advice()
    expect(f"{desc} → {want}", got == want, got)

# ── Part E：live_api eventdata 增量统计（离线） ───────────────────
divider("Part E /eventdata ChampionKill 增量统计")
poller = LivePoller(kb)
poller._my_event_names = {"我我我"}
poller._name_to_champ = {"yisum": "无极剑圣", "nautsum": "深海泰坦"}
payload = {"events": [
    {"EventID": 1, "EventName": "GameStart"},
    {"EventID": 2, "EventName": "ChampionKill",
     "KillerName": "yisum", "VictimName": "我我我"},
    {"EventID": 3, "EventName": "ChampionKill",
     "KillerName": "Turret", "VictimName": "我我我"},
    {"EventID": 4, "EventName": "ChampionKill",
     "KillerName": "nautsum", "VictimName": "队友甲"},
    {"EventID": 5, "EventName": "ChampionKill",
     "KillerName": "yisum#CN1", "VictimName": "我我我#CN1"},
]}
poller._process_kill_events(payload, my_disp="殇之木乃伊")
print("kill_counts:", poller._kill_counts)
expect("只认英雄击杀者 + Victim=我（塔/队友死亡忽略，#tag 归一）",
       poller._kill_counts == {"无极剑圣": 2}, poller._kill_counts)
poller._process_kill_events(payload, my_disp="殇之木乃伊")
expect("EventID 单调去重不重复计数",
       poller._kill_counts == {"无极剑圣": 2}, poller._kill_counts)
poller._process_kill_events({"events": [
    {"EventID": 2, "EventName": "ChampionKill",
     "KillerName": "nautsum", "VictimName": "我我我"}]}, my_disp="x")
expect("新局 EventID 回退 → 清零重计",
       poller._kill_counts == {"深海泰坦": 1}, poller._kill_counts)
poller._process_kill_events({"events": [
    {"EventID": 3, "EventName": "ChampionKill",
     "KillerName": "yisum", "VictimName": "我我我"}]}, my_disp="x")
expect("新局内继续单调累加",
       poller._kill_counts == {"深海泰坦": 1, "无极剑圣": 1},
       poller._kill_counts)
snapshot_counts = dict(poller._kill_counts)
poller.leave_game()
expect("leave_game 清零（跨局不残留）", poller._kill_counts == {}
       and poller._last_event_id == 0, poller._kill_counts)
expect("快照字段可安全转交（GameSession 挂 killer_counts 口径）",
       isinstance(snapshot_counts, dict), snapshot_counts)

print(f"\n==== 修复后行为对照: {_result['pass']} 通过 / "
      f"{_result['fail']} 失败 ====")
sys.exit(1 if _result["fail"] else 0)
