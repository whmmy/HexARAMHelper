"""历史对局回放：用 log/games/*.json 重建局中快照，模拟当前版本系统
会给出的军师提示与装备建议（只读诊断脚本，不落盘、不联网）。

数据来源：复盘日志 log/games/*.json（记录英雄/双方阵容/威胁画像/当时
实际输出的建议文本/时间线）。日志未存敌方逐件装备与局中我方装备快照，
因此局中时刻按时间线事件 + 记录建议里的金币重建（标注"重建"），终局
装备用 final_items 实录（标注"实录"）。

可核对的版本行为差异（历史建议 vs 当前模拟）：
- 卡特局旧建议含 麦瑞德裂血手套/飞升护符（竞技场/KIWI 专属件）→
  42af06c 后已入库过滤，模拟中不应再出现；
- 龙局终局 3 传说+鞋+零食曾误判满装进替换流 → 389ff98 后传说档≥5
  才算满装，模拟应回到 next_items 流；
- 真满装（S6 合成 6 格 5 传说）应给出 卖X→换Y 替换建议。

运行：.venv/Scripts/python.exe experiments/replay_history.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hexa.ai.engine import RuleDecisionEngine
from hexa.core.game_state import GameSession
from hexa.core.live_api import LiveSnapshot, PlayerInfo
from hexa.core.rules_engine import RulesEngine
from hexa.knowledge.kb import KnowledgeBase
from hexa.ui.review import _suggestion_entries

GAMES = [
    Path("log/games/20260925_210743_不祥之刃.json"),
    Path("log/games/20260928_222132_炽炎雏龙.json"),
]

_result = {"pass": 0, "fail": 0}


def expect(desc, cond, detail=""):
    ok = bool(cond)
    _result["pass" if ok else "fail"] += 1
    print(f"  [assert] {desc}: {'✅' if ok else '❌ ' + str(detail)}")


def divider(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def make_session(rec, kb, rules):
    engine = RuleDecisionEngine(kb, rules)
    sess = GameSession(kb, rules, engine=engine)
    sess.stage = "in_game"
    sess.my_hero = rec["hero"]
    sess.ally = [a for a in rec["ally"] if a]
    sess.enemy = [e for e in rec["enemy"] if e]
    sess.game_map = "12"                      # 大乱斗
    sess.augments = list(rec.get("augments") or [])
    # 两局实录均未经历出门装流（卡特局中途加入 hero_late_detected；龙局
    # 建议已直接产出），opening_state 对齐"已购装备/跳过"态
    sess.opening_state = "bypassed"
    engine.start_game(rec["hero"], sess.ally, sess.enemy, game_map="12")
    return sess


def make_snapshot(rec, items, gold, ts, dead=False, respawn=0.0,
                  ally_kda=None, foe_kda=None, ally_extra_items=()):
    """按记录阵容 + 重建的我的状态拼一个 LiveSnapshot（对齐 5s 轮询帧）。"""
    me = PlayerInfo(champion=rec["hero"], team="ORDER",
                    kills=ally_kda[0][0] if ally_kda else 0,
                    deaths=ally_kda[0][1] if ally_kda else 0,
                    assists=ally_kda[0][2] if ally_kda else 0,
                    items=[{"name": n, "count": 1} for n in items])
    allies = []
    for i, name in enumerate(rec["ally"]):
        k, d, a = ally_kda[i + 1] if ally_kda else (0, 0, 0)
        its = [{"name": n, "count": 1} for n in
               (ally_extra_items[i] if i < len(ally_extra_items) else ())]
        allies.append(PlayerInfo(champion=name, team="ORDER",
                                 kills=k, deaths=d, assists=a, items=its))
    foes = []
    for i, name in enumerate(rec["enemy"]):
        k, d, a = foe_kda[i] if foe_kda else (0, 0, 0)
        foes.append(PlayerInfo(champion=name, team="CHAOS",
                               kills=k, deaths=d, assists=a))
    return LiveSnapshot(ts=ts, ok=True, my_champion=rec["hero"],
                        my_items=[{"name": n, "count": 1} for n in items],
                        my_current_gold=gold, my_is_dead=dead,
                        my_respawn_timer=respawn,
                        players=[me] + allies + foes)


def replay_stage(rec, kb, rules, stage):
    """单时刻回放：先发一帧热身快照（对齐真实局的路线/军师已就绪状态），
    再发本时刻快照，收集 overlay 建议 + 军师滚动列表。"""
    sess = make_session(rec, kb, rules)
    items, gold, ts = stage["items"], stage["gold"], stage["ts"]
    warm = make_snapshot(rec, items, gold, ts - 5)
    sess.on_snapshot(warm)
    snap = make_snapshot(rec, items, gold, ts, dead=stage.get("dead", False),
                         respawn=stage.get("respawn", 0.0),
                         ally_kda=stage.get("ally_kda"),
                         foe_kda=stage.get("foe_kda"),
                         ally_extra_items=stage.get("ally_extra_items", ()))
    sess.on_snapshot(snap)
    return sess


def nearest_recorded(rec, ts):
    # 建议条目解析复用 review.py（N2 复盘分层案后落盘为按 kind 分桶
    # dict，新旧双格式单一事实源在读取端 review.py，此处不再自持一份）
    cands = _suggestion_entries(rec)
    if not cands:
        return None
    return min(cands, key=lambda s: abs(s["ts"] - ts))


def print_stage(rec, kb, rules, stage):
    sess = replay_stage(rec, kb, rules, stage)
    print(f"\n── 时刻[{stage['label']}]  ts≈{stage['ts']:.0f}  "
          f"输入: 金币={stage['gold']:.0f} 装备={stage['items'] or '∅'}"
          f"{'  ☠死亡' if stage.get('dead') else ''} ──")
    t = sess.threat
    print(f"威胁画像(模拟): {t.summary() if t else '∅'}"
          f"   <日志记录: {rec['threat']}>")
    print(f"路线(模拟): {sess.current_path or '∅'}   "
          f"<日志记录: {rec['current_path']}>")
    if sess.swap_forecast:
        print(f"替换预告: {sess.swap_forecast}")
    notes = [c["text"] for c in sess.coach_notes]
    print("军师提示(滚动):")
    for n in notes:
        print(f"  · {n}")
    if not notes:
        print("  (无)")
    print("当前建议(overlay 文本):")
    sug = sess.suggestion.render() if sess.suggestion else "(空)"
    for line in sug.splitlines():
        print(f"  | {line}")
    ref = nearest_recorded(rec, stage["ts"])
    if ref:
        dt = ref["ts"] - stage["ts"]
        print(f"[对照] 当时记录（ts差{dt:+.0f}s, {ref['kind']}）:")
        for line in ref["text"].splitlines():
            print(f"  | {line}")
    return sess


def main():
    kb = KnowledgeBase()
    rules = RulesEngine(kb)
    for path in GAMES:
        rec = json.loads(path.read_text(encoding="utf-8"))
        divider(f"{rec['hero']} 局  {rec['started_at']}  "
                f"时长{rec['duration_s']:.0f}s  engine.mode="
                f"{rec['engine'].get('mode')}")
        print(f"我方: 我[{rec['hero']}] + {rec['ally']}")
        print(f"敌方: {rec['enemy']}")
        print(f"终局装备(实录): {rec['final_items']}")
        print("注: 日志未存敌方逐件装备，模拟威胁画像按名字先验计（坦克底板"
              "无实证不计伤害）；记录值为局内装备投票口径且含已修复的双计"
              "缺陷（祖安狂人 法伤×3 实为×2），差异属修复而非回归。")
        for stage in stages_for(rec):
            sess = print_stage(rec, kb, rules, stage)
            verify_stage(rec, stage, sess)


def stages_for(rec):
    """按局定义回放时刻（ts 取自该局时间线/记录建议）。

    开局时刻按记录口径补出门件（龙局 5388s 已推断出 ad 路线，说明当时
    身上有小件；空装备+无首海克斯时真实系统会静默等待出门装流）。
    """
    if "不祥之刃" in rec["hero"]:
        final = rec["final_items"]
        return [
            {"label": "开局重建", "ts": 1790341663.9,
             "items": ["长剑"], "gold": 1400},
            {"label": "记录时刻① 金币2785", "ts": 1790341664.0,
             "items": ["长剑"], "gold": 2785},
            {"label": "终局实录 金币3303", "ts": 1790341684.0,
             "items": final, "gold": 3303},
        ]
    final = rec["final_items"]
    wind_ally = [(4, 1, 6), (3, 1, 4), (2, 2, 3), (1, 0, 2), (0, 1, 1)]
    wind_foe = [(1, 3, 2), (1, 2, 1), (0, 2, 1), (1, 3, 2), (0, 2, 1)]
    return [
        {"label": "开局·距死亡之舞差2150（记录coach）", "ts": 1790605340.0,
         "items": ["长剑"], "gold": 1150},
        {"label": "中期·记录建议金币2023+考尔菲德", "ts": 1790605584.1,
         "items": ["考尔菲德的战锤"], "gold": 2023},
        {"label": "死亡·respawn=12s（时间线）", "ts": 1790605609.0,
         "items": ["考尔菲德的战锤", "吸血鬼节杖"], "gold": 803,
         "dead": True, "respawn": 12},
        {"label": "后期·顺风（KDA/成型度合成）", "ts": 1790605945.0,
         "items": ["暴食胫甲", "夺萃之镰"], "gold": 500,
         "ally_kda": wind_ally, "foe_kda": wind_foe,
         "ally_extra_items": [("无尽之刃",), ("无尽之刃",), (), (), ()]},
        {"label": "终局实录快照（金币未知=0）", "ts": 1790606100.0,
         "items": final, "gold": 0},
        {"label": "真满装对照（终局装补成6格5传说）", "ts": 1790606100.0,
         "items": ["夺萃之镰", "暴食胫甲", "无尽之刃", "疾射火炮",
                   "死亡之舞", "多米尼克领主的致意"], "gold": 1500},
    ]


def verify_stage(rec, stage, sess):
    """对可固化的版本行为差异做断言（历史误报 vs 当前模拟）。"""
    txt = sess.suggestion.render() if sess.suggestion else ""
    seq = sess.suggestion.candidates if sess.suggestion else []
    kinds = [type(c).__name__ for c in seq]
    if "不祥之刃" in rec["hero"]:
        if stage["label"].startswith("终局实录"):
            expect("竞技场/KIWI 专属件不再入推荐（麦瑞德裂血手套/飞升护符）",
                   "麦瑞德裂血手套" not in txt and "飞升护符" not in txt, txt)
    else:
        if stage["label"].startswith("开局"):
            expect("祖安狂人不再双计法伤（法伤×2 实证口径）",
                   sess.threat and sess.threat.ap_count == 2,
                   sess.threat.summary() if sess.threat else "∅")
            expect("祖安狂人计入坦克（坦克×1）",
                   sess.threat and sess.threat.tank_count == 1,
                   sess.threat.summary() if sess.threat else "∅")
            expect("核心优先：①位为核心件或进攻向合成（吸血鬼节杖）",
                   seq and seq[0].name in ("夺萃之镰", "吸血鬼节杖"),
                   [c.name for c in seq])
            expect("防御件（饮魔刀）不入推荐序列",
                   "饮魔刀" not in txt, txt)
        if stage["label"].startswith("终局实录"):
            expect("3传说+鞋+零食不再误判满装（应仍是买装流）",
                   kinds and all(k == "Candidate" for k in kinds), kinds)
        if stage["label"].startswith("真满装"):
            expect("真满装（5传说+鞋）进替换流（SwapCandidate）",
                   kinds and "SwapCandidate" in kinds, kinds)
            expect("替换建议含 卖X→换Y 文案", "🔄" in txt, txt)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"\n==== 回放完成: {_result['pass']} 通过 / {_result['fail']} 失败 "
          f"({time.time() - t0:.1f}s) ====")
    sys.exit(1 if _result["fail"] else 0)
