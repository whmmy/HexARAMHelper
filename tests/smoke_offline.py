"""离线冒烟测试：mock 驱动 P0 全链路（无 LCU / 无游戏 / 无网络）

覆盖：
1. KnowledgeBase 加载与查询
2. RulesEngine：current_path / 出门装联动 / 下一件候选 / 小件合成
3. GameSession 全生命周期：选人→加载→对局→出门装状态机→死亡触发→
   敌方关键装/KDA 触发→结束重置（含秒退重开）
4. PhaseMachine 状态转换（含 dodge_requeue 与断连事件）

运行: uv run python tests/smoke_offline.py   （全绿 exit 0）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.core.game_state import GameSession
from hexa.core.live_api import LiveSnapshot, PlayerInfo
from hexa.core.lcu import PhaseMachine
from hexa.core.rules_engine import RulesEngine, ThreatProfile, KEY_ENEMY_ITEMS
from hexa.knowledge.kb import KnowledgeBase

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


# ── 测试用公共对象 ────────────────────────────────────────────────

kb = KnowledgeBase()
rules = RulesEngine(kb)

# 用真实存在的海克斯名做台账测试（"心之刚"是计划示例名，不在当前名单，
# 联动逻辑单独用直调测试覆盖）
REAL_AUG = sorted(kb.augment_names)[0]


def make_snap(my_hero_en="Annie", my_items=None, dead=False, enemy_items=None,
              enemy_kda=(0, 0, 0), my_team="ORDER", my_gold=0):
    snap = LiveSnapshot(ts=time.time(), ok=True)
    snap.my_champion_en = my_hero_en
    snap.my_champion = kb.champion_cn(my_hero_en) or my_hero_en
    snap.my_is_dead = dead
    snap.my_current_gold = my_gold
    snap.my_items = [{"name": n, "count": 1} for n in (my_items or [])]
    snap.players.append(PlayerInfo(
        champion=snap.my_champion, champion_en=my_hero_en, team=my_team,
        items=snap.my_items, kills=2, deaths=0, assists=3))
    snap.players.append(PlayerInfo(
        champion="盲僧", champion_en="LeeSin", team="CHAOS",
        items=[{"name": n, "count": 1} for n in (enemy_items or [])],
        kills=enemy_kda[0], deaths=enemy_kda[1], assists=enemy_kda[2]))
    for cn in ("黑暗之女", "九尾妖狐", "皎月女神"):  # 3 个 AP 英雄做威胁画像
        snap.players.append(PlayerInfo(champion=cn, champion_en="", team="CHAOS"))
    return snap


# ── 1. KnowledgeBase ─────────────────────────────────────────────

def test_kb():
    print("\n[1] KnowledgeBase")
    check("173 英雄先验", len(kb.hero_augments) == 173, f"got {len(kb.hero_augments)}")
    check("229 海克斯名单", len(kb.augment_names) == 229, f"got {len(kb.augment_names)}")
    check("装备库加载", len(kb.items) > 500, f"got {len(kb.items)}")
    check("DD 版本号", kb.dd_version.startswith("16."), kb.dd_version)
    check("拼音搜索", kb.search_hero("ayjm") == (["暗裔剑魔"], True))
    check("英中转换", kb.champion_cn("Aatrox") == "暗裔剑魔")
    check("英雄校验模糊", kb.validate_hero("剑魔") == "暗裔剑魔")
    top = kb.top_augments("暗裔剑魔", 10)
    check("海克斯 top 查询", len(top) == 10 and all(kb.valid_augment(a) for a in top))


# ── 2. RulesEngine ───────────────────────────────────────────────

def test_rules():
    print("\n[2] RulesEngine")
    check("空输入路线未定", rules.infer_current_path([], []) == "")
    check("AD 装备推断", rules.infer_current_path(["无尽之刃"], []) == "ad")
    check("AP 装备推断", rules.infer_current_path(["灭世者的死亡之帽", "爆裂魔杖"], []) == "ap")
    check("坦克装备推断", rules.infer_current_path(["日炎斗篷"], []) == "tank")
    check("海克斯偏移(心之刚示例)",
          rules.infer_current_path(["巨人腰带"], ["心之刚"]) == "tank")

    # 出门装：标准池按英雄伤害类型
    cands = rules.opening_candidates("黑暗之女")  # Annie=ap
    check("AP 出门装三选", len(cands) == 3 and cands[0].name == "多兰之戒",
          str([c.name for c in cands]))
    # 心之刚联动（计划验收场景：拿到心之刚 → 巨人腰带）
    cands = rules.opening_candidates("任意", first_augment="心之刚")
    check("心之刚→巨人腰带联动", cands[0].name == "巨人腰带", str([c.name for c in cands]))
    check("联动候选全部真实装备", all(kb.valid_item(c.name) for c in cands))

    # 下一件：AP 路线 + 3 法师威胁 → 魔抗优先，抗性件跨路线放行
    threat = ThreatProfile(ap_count=3, ad_count=1)
    check("威胁画像→魔抗建议", threat.resist_advice() == "魔抗优先")
    cands = rules.next_item_candidates(["爆裂魔杖"], "ap", threat)
    check("下一件三选", len(cands) == 3, str([c.name for c in cands]))
    check("下一件候选全真实", all(kb.valid_item(c.name) for c in cands))
    # 抗性件跨路线放行（女妖面纱 bias=tank 但 ap 路线可拿）
    tp = ThreatProfile(ap_count=3)
    cands2 = rules.next_item_candidates(["爆裂魔杖"], "ap", tp, limit=20)
    check("抗性件跨路线放行",
          any("SpellBlock" in kb.items[c.name]["tags"] for c in cands2 if c.name in kb.items),
          "ap 路线候选中无任何魔抗件")

    # 小件合成：爆裂魔杖 → 某个 AP 传说件
    target, note = rules.component_advice(["爆裂魔杖"], "ap")
    check("小件合成建议存在", target is not None, note)
    check("合成目标路线一致", target and kb.item_path_bias(target) == "ap", str(target))

    # 威胁画像：敌方英雄 + 装备扫描
    tp = rules.threat_profile(
        ["九尾妖狐", "皎月女神", "黑暗之女", "盲僧", "众星之子"],
        [PlayerInfo(champion="盲僧", items=[{"name": "荆棘之甲", "count": 1}])])
    check("AP 数统计", tp.ap_count == 3, str(tp.ap_count))
    check("治疗识别", tp.healers == ["众星之子"])
    check("敌方关键装扫描", tp.key_items and tp.key_items[0]["item"] == "荆棘之甲")


# ── 3. GameSession 生命周期 ──────────────────────────────────────

def test_session():
    print("\n[3] GameSession 全生命周期")
    session = GameSession(kb, rules)

    # 选人
    session.on_champ_select({"ally": ["黑暗之女"], "enemy": [], "my_hero": "黑暗之女",
                             "source": "test"})
    check("选人阶段", session.stage == "champ_select")
    check("英雄跟随", session.my_hero == "黑暗之女")

    # 骰子换人 → 重新跟随
    session.on_champ_select({"ally": ["暗裔剑魔"], "enemy": [], "my_hero": "暗裔剑魔",
                             "source": "test"})
    check("换人跟随", session.my_hero == "暗裔剑魔")

    # 加载（阵容锁定）
    teams = {"ally": ["暗裔剑魔", "九尾妖狐", "皎月女神", "盲僧", "众星之子"],
             "enemy": ["黑暗之女", "狂暴之心", "放逐之刃", "诺克萨斯之手", "琴瑟仙女"],
             "my_hero": "暗裔剑魔", "source": "test"}
    session.on_game_start(teams)
    check("阵容锁定", len(session.ally) == 5 and len(session.enemy) == 5)
    check("威胁画像生成", session.threat and session.threat.ap_count >= 2,
          session.threat.summary() if session.threat else "None")
    session.on_in_progress()
    check("对局中", session.stage == "in_game")

    # 出门装状态机分支一：首个海克斯记账时未购装备 → 出门装三选
    snap = make_snap(my_hero_en="Aatrox", my_items=[])
    session.on_snapshot(snap)
    session.record_augment(REAL_AUG, tier="棱彩")
    check("首海克斯→出门装", session.suggestion and session.suggestion.kind == "opening",
          session.suggestion.render() if session.suggestion else "None")
    check("出门装三选项", len(session.suggestion.candidates) == 3)

    # 分支二 + 定时快照 diff（v4.2）：买到装备后（无死亡）签名变化即转入出装主线
    snap = make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"])
    session.on_snapshot(snap)
    check("AD 路线推断", session.current_path == "ad", session.current_path)
    check("购装备后自动转主线(无死亡)", session.suggestion.kind == "next_items"
          and session.opening_state == "bypassed", session.suggestion.kind)
    seq1 = session.suggestion
    check("主线≥2件且带价格", len(seq1.candidates) >= 2
          and all(c.price > 0 for c in seq1.candidates),
          str([(c.name, c.price) for c in seq1.candidates]))
    # 相同快照 → 不重算（防刷新）；金币档变化 → 重算
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"]))
    check("快照无变化不刷新", session.suggestion is seq1)
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"], my_gold=4000))
    check("金币变化触发重算", session.suggestion is not seq1)
    text = session.suggestion.render()
    check("金币感知渲染", "金币 4000" in text and "可购买" in text, text.split(chr(10))[0])
    # 首件若可负担，"可购买"必须标在首件（防"先算钱后插入合成线"的顺序回归）
    first = session.suggestion.candidates[0]
    if first.price <= 4000:
        check("首件可购买标注", first.afford_note == "可购买",
              f"{first.name} {first.price}G -> {first.afford_note!r}")

    # 死亡（事件触发保留）→ 立即重算主线
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"], dead=True))
    check("死亡→重算主线", session.suggestion.kind == "next_items",
          session.suggestion.kind)

    # 小件合成优先呈现
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"]))
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"], dead=True))
    check("小件合成判断", session.suggestion.candidates[0].reason
          and ("合成" in session.suggestion.candidates[0].reason
               or "攒钱" in session.suggestion.candidates[0].reason),
          session.suggestion.candidates[0].reason)

    # 敌方关键装备触发
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"],
                                  enemy_items=["荆棘之甲"]))
    check("敌方关键装提示", session.suggestion and session.suggestion.kind == "event_note"
          and "荆棘之甲" in session.suggestion.render(),
          session.suggestion.render() if session.suggestion else "None")

    # 敌方 KDA 起飞触发（每局限 2 次）
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["十字镐"],
                                  enemy_kda=(6, 0, 4)))
    check("敌方起飞提示", session.suggestion and "起飞" in session.suggestion.render(),
          session.suggestion.render() if session.suggestion else "None")

    # 海克斯台账 + current_path 重估
    before = len(session.augments)
    check("台账防重复", session.record_augment(REAL_AUG) is False)
    check("结束前台账非空", len(session.augments) == max(before, 1))

    # 结束 → 重置
    session.on_end_of_game("EndOfGame")
    st = session.ui_state()
    check("结束重置", session.stage == "idle" and not st["augments"]
          and session.suggestion is None and session.my_hero is None)

    # ui_state 快照完整性
    check("ui_state 键齐全", {"stage", "my_hero", "ally", "enemy", "augments",
                              "current_path", "threat", "suggestion",
                              "dd_version", "path_drift"} <= set(st.keys()))
    fresh = session.data_freshness()
    check("新鲜度字段", "date" in fresh and fresh["dd_version"].startswith("16."))


# ── 4. PhaseMachine ──────────────────────────────────────────────

class FakeLCU:
    def __init__(self):
        self.phase_script = []
        self.teams = {"ally": ["黑暗之女"], "enemy": [], "my_hero": "黑暗之女",
                      "source": "FakeLCU"}

    def is_connected(self):
        return True

    def connect(self):
        return True

    def get_gameflow_phase(self):
        return self.phase_script.pop(0) if self.phase_script else None

    def get_both_teams(self):
        return self.teams


def test_phase_machine():
    print("\n[4] PhaseMachine 状态转换")
    lcu = FakeLCU()
    events = []
    pm = PhaseMachine(lcu, callbacks={
        "on_champ_select": lambda t: events.append(("champ_select",)),
        "on_game_start": lambda t: events.append(("game_start",)),
        "on_in_progress": lambda: events.append(("in_progress",)),
        "on_end_of_game": lambda r: events.append(("end_of_game", r)),
        "on_disconnect": lambda: events.append(("disconnect",)),
        "on_reconnect": lambda: events.append(("reconnect",)),
    })

    def run(phase):
        pm._handle(phase)

    run("Lobby")            # idle
    run("ChampSelect")      # 进入选人
    run("ChampSelect")      # 选人持续刷新（骰子跟随）
    run("GameStart")
    run("InProgress")
    run("WaitingForStats")  # 正常结束
    kinds = [e[0] for e in events]
    check("选人事件≥2（含刷新）", kinds.count("champ_select") >= 2, str(kinds))
    check("GameStart 事件", "game_start" in kinds)
    check("InProgress 事件", "in_progress" in kinds)
    check("结束事件", "end_of_game" in kinds and
          events[kinds.index("end_of_game")][1] == "WaitingForStats")

    # 秒退重开：InProgress → ChampSelect → end_of_game(dodge_requeue) + 新选人
    events.clear()
    run("InProgress")
    run("ChampSelect")
    kinds = [e[0] for e in events]
    check("秒退触发结束", "end_of_game" in kinds and
          events[kinds.index("end_of_game")][1] == "dodge_requeue", str(events))
    check("秒退后重新选人", kinds.count("champ_select") >= 1)

    # 断连（In game 中连续取不到 phase）
    class DeadLCU(FakeLCU):
        def is_connected(self):
            return False

        def connect(self):
            return False

    pm2 = PhaseMachine(DeadLCU(), callbacks={"on_disconnect": lambda: events.append(("disconnect",))})
    pm2._phase = "InProgress"
    for _ in range(PhaseMachine.DISCONNECT_NEEDED):
        check_phase = pm2._poll_phase()
    check("断连事件", ("disconnect",) in events, str(events[-3:]))


# ── main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_kb()
    test_rules()
    test_session()
    test_phase_machine()
    print(f"\n{'='*40}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✅")
