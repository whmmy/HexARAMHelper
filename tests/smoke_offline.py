"""离线冒烟测试：mock 驱动 P0~P2 全链路（无 LCU / 无游戏 / 无网络）

覆盖：
1. KnowledgeBase 加载与查询（含 173 英雄 OP.GG builds / 语义标签接口）
2. RulesEngine：current_path / OP.GG 出门装+联动 / OP.GG 高频下一件候选 /
   小件合成 / 海克斯时刻评级
3. GameSession 全生命周期：选人→加载→对局→出门装状态机→死亡触发→
   敌方关键装/KDA 触发→结束重置（含秒退重开）+ AI 精修 serial 防旧覆新
4. PhaseMachine 状态转换（含 dodge_requeue 与断连事件）
5. DetectorCore 迟滞状态机 + Layout 坐标 + 鼠标落点映射
6. AI 引擎（FakeClient）：System2 生成/幻觉过滤/规则降级/预算与间隔守门/
   System1 防幻觉/复盘日志

运行: uv run python tests/smoke_offline.py   （全绿 exit 0）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.ai.client import UsageStats
from hexa.ai.engine import (AIDecisionEngine, KIND_NEXT, KIND_OPENING,
                            RuleDecisionEngine)
from hexa.core.game_state import GameSession
from hexa.core.live_api import LivePoller, LiveSnapshot, PlayerInfo
from hexa.core.lcu import PhaseMachine
from hexa.core.mouse_ledger import CardLedger
from hexa.core.rules_engine import RulesEngine, ThreatProfile
from hexa.core.screen_detect import (DEFAULT_LAYOUT_169, DetectorCore, Layout,
                                     point_in_rect)
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

    # P1：OP.GG builds 接入
    check("173 英雄 builds", len(kb.builds) == 173, f"got {len(kb.builds)}")
    check("builds 版本", kb.builds_patch.startswith("16."), kb.builds_patch)
    pool = kb.core_item_pool("暗裔剑魔")
    check("核心装池非空", len(pool) >= 5, str(pool[:3]))
    check("核心装池全真实", all(kb.valid_item(n) for n in pool))
    combos = kb.starter_combos("暗裔剑魔")
    check("出门装组合", len(combos) >= 1 and all(kb.valid_item(n)
                                                for c in combos for n in c))
    skill = kb.skill_advice("暗裔剑魔")
    check("加点建议", bool(skill["priority"]) and len(skill["order"]) >= 10,
          str(skill["priority"]))
    boots = kb.boots_options("暗裔剑魔")
    check("鞋子选项", len(boots) >= 1 and kb.valid_item(boots[0][0]))
    # 语义标签接口（库缺失时返回空 dict 不报错）
    check("标签接口安全", isinstance(kb.augment_tag_info(REAL_AUG), dict))


# ── 2. RulesEngine ───────────────────────────────────────────────

def test_rules():
    print("\n[2] RulesEngine")
    check("空输入路线未定", rules.infer_current_path([], []) == "")
    check("AD 装备推断", rules.infer_current_path(["无尽之刃"], []) == "ad")
    check("AP 装备推断", rules.infer_current_path(["灭世者的死亡之帽", "爆裂魔杖"], []) == "ap")
    check("坦克装备推断", rules.infer_current_path(["日炎斗篷"], []) == "tank")
    check("海克斯偏移(心之刚示例)",
          rules.infer_current_path(["巨人腰带"], ["心之刚"]) == "tank")

    # 出门装：OP.GG starter 优先（P1 升级），手工池补足，全部真实装备
    cands = rules.opening_candidates("黑暗之女")  # Annie=ap
    check("出门装三选", len(cands) == 3, str([c.name for c in cands]))
    check("出门装 OP.GG 优先", cands[0].name ==
          kb.starter_combos("黑暗之女")[0][0], str([c.name for c in cands]))
    check("出门装全真实", all(kb.valid_item(c.name) for c in cands))
    check("出门装带价格", all(c.price > 0 for c in cands))
    # 心之刚联动（计划验收场景：拿到心之刚 → 巨人腰带，联动置顶）
    cands = rules.opening_candidates("暗裔剑魔", first_augment="心之刚")
    check("心之刚→巨人腰带联动置顶", cands[0].name == "巨人腰带",
          str([c.name for c in cands]))
    check("联动候选全部真实装备", all(kb.valid_item(c.name) for c in cands))

    # 下一件：OP.GG 高频池 + 路线约束 + 抗性放行
    threat = ThreatProfile(ap_count=3, ad_count=1)
    cands = rules.next_item_candidates(["爆裂魔杖"], "ap", threat, hero="黑暗之女")
    check("下一件三选", len(cands) == 3, str([c.name for c in cands]))
    check("下一件候选全真实", all(kb.valid_item(c.name) for c in cands))
    opgg = set(kb.core_item_pool("黑暗之女"))
    got = {c.name for c in rules.next_item_candidates(
        ["爆裂魔杖"], "ap", ThreatProfile(), hero="黑暗之女", limit=6)}
    check("AP 下一件 OP.GG 高频主导", len(got & opgg) >= 3,
          f"{sorted(got)} vs pool {sorted(opgg)[:6]}")
    # AD 路线不串 AP 主线件（抗性件除外）
    ad_cands = rules.next_item_candidates(["多兰之刃"], "ad", ThreatProfile(),
                                          hero="暗裔剑魔", limit=6)
    bad = [c.name for c in ad_cands
           if kb.item_path_bias(c.name) not in ("ad", "tank", None)]
    check("AD 候选不串 AP 主线", not bad, str(bad))
    # 抗性件跨路线放行（女妖面纱 bias=tank 但 ap 路线可拿）
    tp = ThreatProfile(ap_count=3)
    cands2 = rules.next_item_candidates(["爆裂魔杖"], "ap", tp, hero="黑暗之女",
                                        limit=20)
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

    # 海克斯时刻评级（S/A/B 合成评分）
    top3 = kb.top_augments("暗裔剑魔", 3)
    ranks = rules.rank_augments("暗裔剑魔", top3)
    check("评级三项", len(ranks) == 3 and all(r["grade"] in "SABC" for r in ranks))
    check("榜首 S 档", ranks[0]["grade"] == "S", str(ranks[0]))
    offmeta = sorted(kb.augment_names - set(kb.top_augments("暗裔剑魔", 40)))[0]
    check("榜内但榜外(该英雄) C 档",
          rules.rank_augments("暗裔剑魔", [offmeta])[0]["grade"] == "C", offmeta)
    check("非库名 '?' 档", rules.rank_augments(
        "暗裔剑魔", ["不存在的海克斯名"])[0]["grade"] == "?")
    check("评级名字全真实", all(kb.valid_augment(r["name"]) for r in ranks))


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

    # 回归（2026-09-25 真机丢日志）：局终前接口瞬时 None 曾把 _phase 从
    # InProgress 覆盖成 None，永久错过 InProgress→IDLE 转换 → 复盘日志不落盘
    events.clear()
    pm2 = PhaseMachine(FakeLCU(), callbacks={
        "on_in_progress": lambda: events.append(("in_progress",)),
        "on_end_of_game": lambda r: events.append(("end_of_game", r)),
    })
    pm2._handle("InProgress")
    pm2._handle(None)               # 瞬时抖动：保留上一阶段
    pm2._handle("InProgress")       # 恢复（cur==prev 不重发）
    pm2._handle("WaitingForStats")  # 局终必须照常触发
    kinds2 = [e[0] for e in events]
    check("瞬时 None 不吞局终事件",
          kinds2 == ["in_progress", "end_of_game"], str(kinds2))


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


# ── 4.5 Live API 本地玩家定位（国服兜底） ────────────────────────

def test_live_me_match():
    print("\n[4.5] Live API 本地玩家定位（国服 activeplayer 缺字段兜底）")
    poller = LivePoller(kb)
    # 国服实测形态：activeplayer.championName=None，playerlist 中文名 + raw 英文键
    cn_pl = [
        {"championName": "曙光女神",
         "rawChampionName": "game_character_displayname_Leona",
         "summonerName": "玩家甲", "team": "ORDER",
         "items": [{"name": "圣物之盾", "count": 1}]},
        {"championName": "黑暗之女",
         "rawChampionName": "game_character_displayname_Annie",
         "summonerName": "我我我", "team": "CHAOS"},
    ]
    me, via = poller._match_me(
        {"championName": None, "currentGold": 2774.9,
         "rawChampionName": "game_character_displayname_Annie"}, cn_pl)
    check("国服 rawChampionName 定位", me is cn_pl[1] and via == "rawChampionName",
          f"via={via}")

    # 极端：raw 也缺失 → 退到 LCU 召唤师名（summonerName / riotId）
    poller2 = LivePoller(kb, summoner_name_fn=lambda: "我我我")
    me2, via2 = poller2._match_me({"championName": None}, cn_pl)
    check("召唤师名定位", me2 is cn_pl[1] and via2 == "summoner", f"via={via2}")

    # 国际服 championName 路径不回归
    intl = [{"championName": "Leona", "team": "ORDER"},
            {"championName": "Annie", "team": "CHAOS"}]
    me3, via3 = poller2._match_me({"championName": "Annie"}, intl)
    check("国际服 championName 定位", me3 is intl[1] and via3 == "championName",
          f"via={via3}")

    # 定位不到 → (None, "")，且不误配
    me4, via4 = poller2._match_me({}, [{"championName": "Leona"}])
    check("定位不到不误配", me4 is None and via4 == "", f"via={via4}")
    # playerlist 空 / championName 为 null 均安全
    check("空 playerlist 安全", poller._match_me({}, []) == (None, ""))
    check("None 防护", str(poller._norm_champ_raw(None)) == "")


# ── 5. 画面检测状态机 / 坐标 / 记账映射（纯逻辑，离线） ──────────

def test_detect_and_layout():
    print("\n[5] DetectorCore / Layout / CardLedger")
    core = DetectorCore()
    check("初始未激活", core.state == "inactive")
    core.feed_titles(2)
    check("单帧命中不激活（迟滞）", core.state == "inactive")
    core.feed_titles(3)
    check("连续 2 帧命中激活", core.state == "active" and core.round_id == 1)
    core.feed_cards(["贪欲束缚", None, "秘术射击"])
    check("单帧内容不稳定", core.stable_cards is None)
    core.feed_cards(["贪欲束缚", None, "秘术射击"])
    check("稳定 2 帧出结果", core.stable_cards == ["贪欲束缚", None, "秘术射击"])
    core.consume_cards()
    # reroll：内容变化 → 重新等待稳定
    core.feed_cards(["秘术射击", "贪欲束缚", None])
    check("内容变化重置", core.stable_cards is None)
    core.feed_cards(["秘术射击", "贪欲束缚", None])
    check("重扫后稳定", core.stable_cards == ["秘术射击", "贪欲束缚", None])
    # 消失迟滞：连续 3 帧无命中
    core.feed_titles(0)
    check("1 帧无命中仍活跃", core.state == "active")
    core.feed_titles(0)
    core.feed_titles(0)
    check("连续 3 帧无命中消失", core.state == "inactive")
    # 再出现 → 轮次 +1
    core.feed_titles(2)
    core.feed_titles(2)
    check("再出现轮次递增", core.state == "active" and core.round_id == 2)

    # Layout：默认 16:9 → 像素；21:9 中心锚定
    lay = Layout()
    px = lay.to_pixels(1920, 1080)
    cards = px["cards"]
    check("卡片像素在屏内", all(0 <= r[0] and r[0] + r[2] <= 1920 and
                                0 <= r[1] and r[1] + r[3] <= 1080 for r in cards))
    check("三卡片水平有序", cards[0][0] < cards[1][0] < cards[2][0])
    wide = lay.to_pixels(3440, 1440)  # 21:9
    wcx = wide["cards"][1][0] + wide["cards"][1][2] / 2
    check("21:9 中心锚定", abs(wcx - 3440 / 2) < 120, f"cx={wcx}")
    # 往返一致性（H 单位换算无损）
    lay2 = Layout(lay.as_169())
    check("Layout 往返一致", lay2.title_bands == lay.title_bands
          and lay2.cards == lay.cards and lay2.reroll == lay.reroll)

    # 鼠标落点 → 卡片序号
    rects = [tuple(r) for r in px["cards"]]
    cl = CardLedger(rects_provider=lambda: rects)
    inside = (rects[1][0] + 5, rects[1][1] + 5)
    check("落点映射卡片2", cl.card_at(*inside) == 1)
    check("空白处无映射", cl.card_at(5, 5) is None)
    check("point_in_rect", point_in_rect(10, 10, [5, 5, 10, 10])
          and not point_in_rect(16, 10, [5, 5, 10, 10]))


# ── 6. AI 引擎（FakeClient，不联网） ─────────────────────────────

class FakeSettings:
    """Settings 协议子集（get/set），预置 AI 开 + 两槽位已配置"""

    def __init__(self, budget=15, min_interval=0.0):
        self._d = {
            "ai": {"enabled": True, "budget_per_game": budget,
                   "min_interval": min_interval},
            "system1": {"model": "fake-flash", "price_in": 0.8, "price_out": 2.7},
            "system2": {"model": "fake-max", "price_in": 2.4, "price_out": 9.6},
            "triggers": {},
        }

    def get(self, *keys, default=None):
        node = self._d
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def get_float(self, *keys, default):
        try:
            return float(self.get(*keys, default=default))
        except (TypeError, ValueError):
            return default

    def set(self, *keys_and_value):
        *keys, value = keys_and_value
        node = self._d
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value


class FakeClient:
    """LLMClient 协议子集：按脚本依次返回 canned 文本；记录调用"""

    def __init__(self, script):
        self.script = list(script)   # 每次调用弹出一个（耗尽则返回 "{}"）
        self.calls = []
        self.usage = UsageStats()
        self.slot = "fake"

    @property
    def configured(self):
        return True

    def chat(self, system, user, max_tokens=None, json_mode=True):
        self.calls.append(user[:60])
        text = self.script.pop(0) if self.script else "{}"
        self.usage.add(100, 50)
        return text, {"model": "fake", "latency": 0.01,
                      "tokens_in": 100, "tokens_out": 50}


def build_ai_engine(script_s2=(), script_s1=(), budget=15, min_interval=0.0):
    st = FakeSettings(budget=budget, min_interval=min_interval)
    delegate = RuleDecisionEngine(kb, rules)
    c2 = FakeClient(list(script_s2))
    c1 = FakeClient(list(script_s1))
    from hexa.ai import system1, system2
    eng = AIDecisionEngine(delegate, st, c2, c1, system2, system1)
    return eng, c1, c2


def test_ai_engine():
    print("\n[6] AI 引擎（FakeClient）")
    hero = "暗裔剑魔"
    # ---- System2 正常生成（canned 合法 JSON，全部真实名字） ----
    top = kb.top_augments(hero, 10)
    pool = kb.core_item_pool(hero)
    ctx_json = {
        "augment_affinity": [{"name": n, "bias": "ad", "note": "测试"} for n in top],
        "opening_items": [{"combo": pool[:1] or ["长剑"], "when": "标准"},
                          {"combo": ["多兰之刃"], "when": "进攻"},
                          {"combo": ["红水晶"], "when": "防守"}],
        "build_paths": {"ad": {"items": pool[:4], "skill_order": "主Q副E",
                               "note": "主线"}},
        "component_rules": [{"item": "考尔菲德的战锤",
                             "upgrade_to": pool[0] if pool else "星蚀",
                             "hold_when": "钱不够先留"}],
        "augment_item_links": [{"augment": top[0], "shift": "偏 ad",
                                "reason": "测试联动"}],
    }
    import json as _json
    eng, c1, c2 = build_ai_engine(script_s2=[_json.dumps(ctx_json)])
    results = []
    eng.bind(lambda kind, payload, serial: results.append((kind, payload, serial)))
    eng.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    ctx = eng.cache.get()
    check("System2 上下文生成", ctx is not None and ctx.source == "ai")
    check("亲和表有效", ctx and len(ctx.augment_affinity) == 10)
    check("加点来自 ctx", eng.skill_order(hero) == "主Q副E")
    check("规则模式无 ctx 时加点", RuleDecisionEngine(kb, rules)
          .skill_order(hero) == "主Q副E")
    # ---- System1 出门装精修（异步回调） ----
    threat = ThreatProfile(ap_count=2)
    base = eng.opening_candidates(top[0], threat, serial=1)
    check("出门装同步规则底线", len(base) == 3)
    order = [p["name"] for p in _json.dumps({}) and []]  # noqa: F841（占位说明）
    c1.script.append(_json.dumps({"picks": [
        {"name": "红水晶", "reason": "对面法伤多"},
        {"name": "多兰之刃", "reason": "标准"},
        {"name": "幻觉装备名", "reason": "应被过滤"}]}))
    eng.opening_candidates(top[0], threat, serial=2)
    deadline = time.time() + 5
    while not any(r[0] == KIND_OPENING for r in results) and time.time() < deadline:
        time.sleep(0.02)
    opening_payloads = [p for k, p, _s in results if k == KIND_OPENING]
    check("出门装 AI 精修回调", bool(opening_payloads))
    if opening_payloads:
        names = [c.name for c in opening_payloads[-1]]
        check("AI 精修防幻觉过滤", "幻觉装备名" not in names and len(names) == 3,
              str(names))
    # ---- System1 下一件 + 守门 ----
    c1.script.append(_json.dumps({"picks": [
        {"name": pool[0], "reason": "版本答案"},
        {"name": pool[1] if len(pool) > 1 else "焚天", "reason": "次选"}]}))
    eng.next_candidates(["多兰之刃"], 3000, "ad", threat, serial=3, reason="event")
    deadline = time.time() + 5
    while not any(r[0] == KIND_NEXT for r in results) and time.time() < deadline:
        time.sleep(0.02)
    next_payloads = [p for k, p, _s in results if k == KIND_NEXT]
    check("下一件 AI 精修回调", bool(next_payloads))
    if next_payloads:
        check("精修保持候选内",
              all(kb.valid_item(c.name) for c in next_payloads[-1]))
    # ---- 海克斯时刻评级精修 ----
    c1.script.append(_json.dumps({"grades": [
        {"name": top[0], "grade": "S", "reason": "版本亲和第一"}]}))
    eng.rank_cards(hero, top[:3], [], [], threat, serial=4)
    deadline = time.time() + 5
    got_ranks = None
    while time.time() < deadline:
        got_ranks = getattr(eng, "_last_rank_result", None)
        r0 = [p for k, p, _s in results if k == "augment_ranks"]
        if r0:
            got_ranks = r0[-1]
            break
        time.sleep(0.02)
    check("评级 AI 精修回调", got_ranks is not None and got_ranks[0]["grade"] == "S",
          str(got_ranks[:1] if got_ranks else None))

    # ---- 预算守门：budget=2 时第 3 次 periodic 不发调用 ----
    eng_b, c1b, _c2b = build_ai_engine(
        script_s2=[_json.dumps(ctx_json)], budget=2)
    eng_b.bind(lambda *a: None)
    eng_b.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng_b.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    eng_b.opening_candidates(top[0], threat, serial=11)   # 1 次
    time.sleep(0.3)
    eng_b.next_candidates(["多兰之刃"], 0, "ad", threat, serial=12,
                          reason="event")                 # 2 次
    time.sleep(0.3)
    calls_after_budget = len(c1b.calls)
    eng_b.next_candidates(["多兰之刃"], 0, "ad", threat, serial=13,
                          reason="event")                 # 超预算，不调用
    time.sleep(0.3)
    check("预算守门生效", len(c1b.calls) == calls_after_budget == 2,
          f"calls={len(c1b.calls)}")

    # ---- 间隔守门：min_interval 很大时 periodic 被压制，event 不受限 ----
    eng_i, c1i, _ = build_ai_engine(script_s2=[_json.dumps(ctx_json)],
                                    min_interval=3600)
    eng_i.bind(lambda *a: None)
    eng_i.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng_i.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    eng_i._last_refine["next_items"] = time.time()  # 模拟刚精修过
    eng_i.next_candidates(["多兰之刃"], 0, "ad", threat, serial=21,
                          reason="periodic")
    time.sleep(0.3)
    check("间隔守门压制 periodic", len(c1i.calls) == 0, f"calls={len(c1i.calls)}")
    eng_i.next_candidates(["多兰之刃"], 0, "ad", threat, serial=22, reason="event")
    time.sleep(0.5)
    check("event 不受间隔守门", len(c1i.calls) >= 1, f"calls={len(c1i.calls)}")

    # ---- System2 幻觉/坏 JSON → 规则版上下文降级 ----
    eng_bad, _c1b2, _c2b2 = build_ai_engine(
        script_s2=["{\"augment_affinity\": [{\"name\": \"幻觉海克斯\"}]}",
                   "not a json"])
    eng_bad.bind(lambda *a: None)
    eng_bad.start_game(hero, ["暗裔剑魔"], ["黑暗之女"])
    deadline = time.time() + 5
    while eng_bad.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    ctx_bad = eng_bad.cache.get()
    check("坏输出降级规则版 Context",
          ctx_bad is not None and ctx_bad.source == "rule", str(ctx_bad.source if ctx_bad else None))


def test_session_ai_serial():
    print("\n[6b] GameSession × AI 精修 serial 防旧覆新")
    session = GameSession(kb, rules)
    session.on_game_start({"ally": ["暗裔剑魔"], "enemy": ["黑暗之女"],
                           "my_hero": "暗裔剑魔", "source": "test"})
    session.on_in_progress()
    session.record_augment(REAL_AUG, tier="棱彩")
    serial_at_opening = session.suggestion.serial
    # 旧 serial 的 AI 精修结果（应被忽略）
    session._on_engine_result(KIND_OPENING,
                              [__import__("hexa.core.rules_engine",
                                          fromlist=["Candidate"]).Candidate(
                                  "幻觉覆盖", "旧包")], serial_at_opening - 1)
    check("旧 serial 不覆盖", session.suggestion.source == "rule")
    # 新 serial 覆盖（source→ai）
    from hexa.core.rules_engine import Candidate
    session._on_engine_result(KIND_OPENING,
                              [Candidate("红水晶", "AI 建议防守")],
                              serial_at_opening)
    check("新 serial 覆盖为 ai", session.suggestion.source == "ai"
          and session.suggestion.candidates[0].name == "红水晶")
    # next_items 精修覆盖 + 金币可买性重算
    session.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                                  my_gold=5000))
    serial_next = session.suggestion.serial
    session._on_engine_result(KIND_NEXT,
                              [Candidate("星蚀", "AI 版本答案", price=3300),
                               Candidate("焚天", "AI 次选", price=3200)],
                              serial_next)
    check("下一件 AI 覆盖", session.suggestion.source == "ai"
          and session.suggestion.candidates[0].name == "星蚀")
    check("AI 覆盖后可买性重算",
          session.suggestion.candidates[0].afford_note == "可购买"
          and "还差" in session.suggestion.candidates[1].afford_note,
          str([(c.name, c.afford_note) for c in session.suggestion.candidates]))
    # 类型不匹配不覆盖
    session._on_engine_result(KIND_NEXT, [Candidate("X", "y")], serial_at_opening)
    check("类型不匹配不覆盖", session.suggestion.candidates[0].name == "星蚀")

    # 复盘日志（结束触发写盘到 log/games/）
    import json as _json2
    from hexa.config import BASE_DIR
    n_logs = len(list((BASE_DIR / "log" / "games").glob("*.json"))) \
        if (BASE_DIR / "log" / "games").exists() else 0
    session.on_end_of_game("EndOfGame")
    logs = list((BASE_DIR / "log" / "games").glob("*.json")) \
        if (BASE_DIR / "log" / "games").exists() else []
    check("复盘日志落盘", len(logs) == n_logs + 1)
    if logs:
        doc = _json2.loads(logs[-1].read_text(encoding="utf-8"))
        check("复盘日志字段", doc.get("hero") == "暗裔剑魔"
              and doc.get("suggestions"), str(doc.get("hero")))


# ── 7. Review 修复回归（P0 闩锁 / epoch 代际 / 双触发 / 排序 / 单位） ──

def test_review_fixes():
    print("\n[7] Review 修复回归")
    hero = "暗裔剑魔"
    import json as _json

    def ctx_json_for(h):
        top = kb.top_augments(h, 8)
        pool = kb.core_item_pool(h) or ["长剑"]
        return _json.dumps({
            "augment_affinity": [{"name": n, "bias": "ad", "note": "t"} for n in top],
            "opening_items": [{"combo": pool[:1], "when": "标准"},
                              {"combo": ["多兰之刃"], "when": "进攻"},
                              {"combo": ["红水晶"], "when": "防守"}],
            "build_paths": {"ad": {"items": pool[:3], "skill_order": "主Q副E",
                                   "note": "主线"}},
            "component_rules": [],
            "augment_item_links": []})

    # -- P0：稳定内容闩锁——同内容不重复发射 on_cards（防烧穿 AI 预算） --
    core = DetectorCore()
    core.feed_titles(2); core.feed_titles(2)
    cards = ["贪欲束缚", None, "秘术射击"]
    emissions = 0
    for _ in range(12):  # 12 帧稳定内容
        core.feed_cards(cards)
        if core.consume_cards():
            emissions += 1
    check("闩锁：稳定内容只发一次", emissions == 1, f"emissions={emissions}")
    # reroll 闩锁清零后可再次发射
    core.reset_round()
    core.feed_cards(cards); core.feed_cards(cards)
    check("闩锁：reroll 后可重发", core.consume_cards() is not None)

    # -- P1：epoch 代际——旧局在途结果整包丢弃 --
    eng, c1, _c2 = build_ai_engine(script_s2=[ctx_json_for(hero),
                                              ctx_json_for("黑暗之女")])
    results = []
    eng.bind(lambda kind, payload, serial: results.append((kind, payload, serial)))
    eng.start_game(hero, [], [])
    eng.start_game("黑暗之女", [], [])   # 秒退重开：代际 +1，game1 在途作废
    deadline = time.time() + 6
    while time.time() < deadline and (eng.cache.get() is None
                                      or eng.cache.pending):
        time.sleep(0.02)
    ctx = eng.cache.get()
    check("epoch：缓存英雄=新局", ctx is not None and ctx.hero == "黑暗之女",
          str(ctx.hero if ctx else None))
    eng.rank_cards("黑暗之女", kb.top_augments("黑暗之女", 3), [], [],
                   ThreatProfile(), serial=1)
    deadline = time.time() + 6
    while time.time() < deadline and not any(k == "augment_ranks"
                                             for k, _p, _s in results):
        time.sleep(0.02)
    check("epoch：新局结果正常回调",
          any(k == "augment_ranks" for k, _p, _s in results))

    # -- P2：死亡同帧不双发（event 发出后 periodic 跳过） --
    st = FakeSettings()
    sess = GameSession(kb, rules, settings=st,
                       engine=build_ai_engine(
                           script_s2=[ctx_json_for(hero)],
                           script_s1=[_json.dumps({"picks": [
                               {"name": "星蚀", "reason": "r"}]})])[0])
    sess.on_game_start({"ally": [hero], "enemy": ["黑暗之女"],
                        "my_hero": hero, "source": "test"})
    sess.on_in_progress()
    sess.record_augment(REAL_AUG)
    fake_eng, fc1, _ = build_ai_engine(
        script_s2=[ctx_json_for(hero)],
        script_s1=[_json.dumps({"picks": [{"name": "星蚀", "reason": "r"}]}),
                   _json.dumps({"picks": [{"name": "焚天", "reason": "r"}]})])
    sess.engine = fake_eng
    sess.engine.bind(sess._on_engine_result)
    sess.engine.start_game(hero, [hero], ["黑暗之女"])
    deadline = time.time() + 6
    while sess.engine.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    sess.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                               my_gold=1000))
    deadline = time.time() + 6
    while time.time() < deadline and len(fc1.calls) < 1:
        time.sleep(0.02)  # 首次 periodic 精修
    n_before_death = len(fc1.calls)
    sess.on_snapshot(make_snap(my_hero_en="Aatrox", my_items=["多兰之刃"],
                               my_gold=1000, dead=True))
    time.sleep(1.0)
    check("死亡同帧不双发", len(fc1.calls) == n_before_death + 1,
          f"before={n_before_death} after={len(fc1.calls)}")

    # -- P2：suggest_opening 保留模型排序 --
    from hexa.ai import system1
    from hexa.ai import system2 as system2_mod
    class _C:
        configured = True
        usage = UsageStats()
        def chat(self, *a, **k):
            return (_json.dumps({"picks": [
                {"name": "红水晶", "reason": "b"},
                {"name": "多兰之刃", "reason": "c"},
                {"name": "长剑", "reason": "a"}]}), {})
    ctx_o = system2_mod.ChampionContext(
        {"augment_affinity": [], "opening_items": [
            {"combo": ["长剑"], "when": "1"}, {"combo": ["多兰之刃"], "when": "2"},
            {"combo": ["红水晶"], "when": "3"}],
         "build_paths": {}, "component_rules": [], "augment_item_links": []},
        hero)
    out = system1.suggest_opening(_C(), ctx_o, hero, "", "", [])
    check("opening 保留模型排序", [o["name"] for o in out] ==
          ["红水晶", "多兰之刃", "长剑"], str([o["name"] for o in out]))

    # -- P2：Layout units:"h" 往返零漂移（任意宽高比） --
    h_regions = {"units": "h",
                 "title_bands": [[-0.3, 0.42, 0.32, 0.05],
                                 [0.0, 0.42, 0.32, 0.05],
                                 [0.3, 0.42, 0.32, 0.05]],
                 "cards": [[-0.35, 0.46, 0.35, 0.4],
                           [0.0, 0.46, 0.35, 0.4],
                           [0.35, 0.46, 0.35, 0.4]],
                 "reroll": [0.0, 0.9, 0.2, 0.05]}
    for w, hgt in ((1920, 1080), (3440, 1440), (2560, 1600), (1600, 1200)):
        lay = Layout(dict(h_regions))
        px = lay.to_pixels(w, hgt)
        # 逐矩形比对独立公式（H 单位：x=W/2+xu*H，宽高/纵向均为 *H）
        ok = True
        for got, (xu, y, wu, hh) in zip(
                px["cards"] + [px["reroll"]],
                h_regions["cards"] + [h_regions["reroll"]]):
            exp = [int(w / 2 + xu * hgt), int(y * hgt),
                   int(wu * hgt), int(hh * hgt)]
            if got != exp:
                ok = False
                check(f"units:h {w}x{hgt} 零漂移", False,
                      f"{got} != {exp}")
                break
        if not ok:
            break
    else:
        check("units:h 4 种分辨率零漂移", True)

    # -- P2：预算原子占位（budget=1 顺序+并发提交只打 1 次） --
    eng_b, c1b, _ = build_ai_engine(script_s2=[ctx_json_for(hero)], budget=1)
    eng_b.bind(lambda *a: None)
    eng_b.start_game(hero, [], [])
    deadline = time.time() + 6
    while eng_b.cache.get() is None and time.time() < deadline:
        time.sleep(0.02)
    for s in range(5):  # 连发 5 个请求，只有第 1 个应占位成功
        eng_b.next_candidates(["多兰之刃"], 0, "ad", ThreatProfile(),
                              serial=100 + s, reason="event")
    time.sleep(1.0)
    check("预算原子占位", len(c1b.calls) == 1, f"calls={len(c1b.calls)}")

    # -- P0 回归：历史 settings.json 价格空串 → stats() 不炸（防误报"AI: 关"） --
    eng_s, _fc1, _fc2 = build_ai_engine(script_s2=[ctx_json_for(hero)])
    eng_s.settings._d["system1"]["price_in"] = ""    # 旧版设置窗口保存的空串
    eng_s.settings._d["system2"]["price_out"] = ""
    try:
        st_stats = eng_s.stats()
        ok = (st_stats["mode"] == "ai" and st_stats["budget"] == 15
              and st_stats["cost_game"] >= 0)
    except Exception as e:  # noqa: BLE001 —— 回归点就是"不该抛"
        ok = False
        print("stats() 异常:", e)
    check("stats 空价格回退默认", ok, str(st_stats.get("cost_game")))

    # -- Settings.get_float：空串/非法值/缺失键均回退默认 --
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from hexa.config import Settings as _Settings
    with _tempfile.TemporaryDirectory() as td:
        _p = _Path(td) / "settings.json"
        _p.write_text(_json.dumps(
            {"system1": {"price_in": "", "timeout": "abc"}}), encoding="utf-8")
        s = _Settings(path=_p)
        check("get_float 空串回退",
              s.get_float("system1", "price_in", default=0.8) == 0.8)
        check("get_float 非法回退",
              s.get_float("system1", "timeout", default=60) == 60)
        check("get_float 缺键回退",
              s.get_float("system1", "price_out", default=2.7) == 2.7)
        check("get_float 默认价合并",
              s.get_float("system2", "price_in", default=0) == 2.4)


# ── main ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_kb()
    test_rules()
    test_session()
    test_phase_machine()
    test_live_me_match()
    test_detect_and_layout()
    test_ai_engine()
    test_session_ai_serial()
    test_review_fixes()
    print(f"\n{'='*40}\n通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✅")
