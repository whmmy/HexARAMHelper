"""N2 复盘分层 + N3 结论指纹去重 行为复现（20261019 产品评估 §3.2/§3.3/§N2/§N3）。

跑法：uv run python experiments/repro_n2_n3_fingerprint.py
无 LCU/无游戏/无网络/无 Key（FakeClient 同款 canned 输出，规则模式 + AI 模式各一段）。

断言面：
①history 分桶：决策类（opening/card_ranks/swap_items/event_note）全量保留；
  next_items 只存结论变化快照——纯金币数字漂移不存、可买性跨档才存。
②KIND_OPENING 精修与 KIND_RANKS 档位真正进 history（旧平列表下 10 局全集为空）。
③指纹不变：不落位（overlay 内容不换）、不写史、不重派 AI；事实变化照常派发。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.knowledge.kb import KnowledgeBase
from hexa.core.rules_engine import RulesEngine, Candidate
from hexa.core.game_state import GameSession, card_ranks_fingerprint
from hexa.ai.engine import AIDecisionEngine, RuleDecisionEngine, KIND_NEXT, KIND_RANKS

FAIL = []


def check(name, cond, detail=""):
    print(("  OK  " if cond else "  FAIL") + f" {name} {detail if not cond else ''}")
    if not cond:
        FAIL.append(name)


def make_snap(kb, my_hero_en, my_items=None, my_gold=0, dead=False,
              enemy_items=None):
    from hexa.core.live_api import LiveSnapshot, PlayerInfo
    snap = LiveSnapshot(ts=time.time(), ok=True)
    snap.my_champion_en = my_hero_en
    snap.my_champion = kb.champion_cn(my_hero_en) or my_hero_en
    snap.my_is_dead = dead
    snap.my_current_gold = my_gold
    snap.my_items = [{"name": n, "count": 1} for n in (my_items or [])]
    snap.players.append(PlayerInfo(
        champion=snap.my_champion, champion_en=my_hero_en, team="ORDER",
        items=snap.my_items, kills=0, deaths=0, assists=0))
    snap.players.append(PlayerInfo(
        champion="黑暗之女", champion_en="Annie", team="CHAOS",
        items=[{"name": n, "count": 1} for n in (enemy_items or [])],
        kills=0, deaths=0, assists=0))
    return snap


def fake_settings():
    class _S:
        _d = {"ai": {"enabled": True, "budget_per_game": 0, "min_interval": 0},
              "system1": {"model": "fake-flash"}, "system2": {"model": "fake-max"},
              "triggers": {}}
        def get(self, *keys, default=None):
            node = self._d
            for k in keys:
                if not isinstance(node, dict) or k not in node:
                    return default
                node = node[k]
            return node
        def get_float(self, *keys, default):
            return float(self.get(*keys, default=default))
        def resolve_api_key(self, slot):
            return "sk-test"
    return _S()


class FakeClient:
    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self.slot = "fake"
    @property
    def configured(self):
        return True
    def chat(self, system, user, max_tokens=None, json_mode=True, purpose=""):
        self.calls.append(user)
        text = self.script.pop(0) if self.script else "{}"
        return text, {"model": "fake", "latency": 0.01, "tokens_in": 10, "tokens_out": 5}


def build_ai_engine(script_s1=()):
    from hexa.ai import system1, system2
    st = fake_settings()
    delegate = RuleDecisionEngine(KB, RULES)
    c1 = FakeClient(list(script_s1))
    eng = AIDecisionEngine(delegate, st, FakeClient([_ctx_json()]), c1,
                           system2, system1)
    return eng, c1


def _ctx_json():
    pool = KB.core_item_pool(HERO)
    return json.dumps({
        "augment_affinity": [{"name": n, "bias": "ad", "note": "t"}
                             for n in KB.top_augments(HERO, 8)],
        "opening_items": [{"combo": pool[:1] or ["长剑"], "when": "标准"}],
        "builds": [{"name": "爆杀流", "path": "ad", "items": pool[:3], "note": "主线"}],
        "skill_order": "主Q副E", "augment_item_links": []})


def wait_ctx(eng, timeout=5.0):
    deadline = time.time() + timeout
    while eng.cache.get() is None and time.time() < deadline:
        time.time and time.sleep(0.02)
    return eng.cache.get() is not None


KB = KnowledgeBase()
RULES = RulesEngine(KB)
HERO = "暗裔剑魔"
REAL_AUG = sorted(KB.augment_names)[0]

print("── ① 规则模式：分桶 + 金币档去重 ──")
s = GameSession(KB, RULES)
s.on_game_start({"ally": [HERO], "enemy": ["黑暗之女"], "my_hero": HERO, "source": "t"})
s.on_in_progress()
s.record_augment(REAL_AUG, tier="棱彩")
check("出门装三选入决策桶",
      s.suggestion.kind == "opening"
      and any(k == "opening" for _, k, _ in s.history["decision"]))
s.on_hex_ui(True)
s.rank_augment_cards([REAL_AUG] * 3)
check("海克斯三选档位入决策桶",
      any(k == "card_ranks" for _, k, _ in s.history["decision"]))

# 购装备 → 主线（首条 refresh）
s.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"], my_gold=500))
check("主线建议入刷新桶", len(s.history["refresh"]) == 1,
      str(len(s.history["refresh"])))
first_price = s.suggestion.candidates[0].price
# 同金币档（可买性都为空/同档）内数字漂移：不落位不写史
sug_before = s.suggestion
s.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"], my_gold=600))
check("金币桶内漂移不重播不重记",
      s.suggestion is sug_before and len(s.history["refresh"]) == 1,
      f"entries={len(s.history['refresh'])}")
# 跨档到「可购买」：结论刷新，写史
s.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"],
                        my_gold=first_price + 500))
check("可买性跨档刷新入史",
      s.suggestion.candidates[0].afford_note == "可购买"
      and len(s.history["refresh"]) == 2,
      str([(c.name, c.afford_note) for c in s.suggestion.candidates[:1]])
      + f" entries={len(s.history['refresh'])}")
# 敌方关键装 → event_note 决策桶
s.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"],
                        my_gold=first_price + 500))
s.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"],
                        enemy_items=["莫雷洛秘典"]))
check("event_note 入决策桶",
      any(k == "event_note" for _, k, _ in s.history["decision"]))
# 落盘结构 + 读取端
s.on_end_of_game("EndOfGame")
games = sorted((Path("log") / "games").glob("*.json"), key=lambda p: p.stat().st_mtime)
doc = json.loads(games[-1].read_text(encoding="utf-8"))
sug = doc.get("suggestions")
check("复盘按桶落盘（dict 双桶）",
      isinstance(sug, dict) and set(sug) == {"decision", "refresh"},
      str(type(sug)))
check("决策桶含 opening/card_ranks/event_note",
      {"opening", "card_ranks", "event_note"}
      <= {e["kind"] for e in sug["decision"]},
      str(sorted({e['kind'] for e in sug['decision']})))
check("刷新桶只含 next_items 且条数=结论变化数",
      {e["kind"] for e in sug["refresh"]} <= {"next_items"}
      and len(sug["refresh"]) == 2, str(len(sug["refresh"])))

from hexa.ui.review import format_review
lines = format_review(doc)
check("新格式读回渲染", lines[2].startswith(f"建议 {len(sug['decision']) + len(sug['refresh'])} 次"),
      lines[2])
old_doc = {"hero": "x", "suggestions": [{"ts": 1.0, "kind": "next_items", "text": "旧"}]}
check("旧平列表读回不崩", format_review(old_doc)[2] == "建议 1 次（最近: 旧）",
      format_review(old_doc)[2])

print("── ② AI 模式：不重派 + 精修入史 ──")
picks_opening = json.dumps({"picks": [{"name": "红水晶", "reason": "出门防守"}]})
picks_std = json.dumps({"picks": [
    {"name": "焚天", "reason": "版本答案先成型"},
    {"name": "星蚀", "reason": "穿甲次选"}]})
picks_upg = json.dumps({"picks": [
    {"name": "星蚀", "reason": "升级目标先出"},
    {"name": "焚天", "reason": "次选"}]})
UPG_AUG = "升级：中娅"   # 升级型前缀双保险判真（事实变化的重派触发源）
eng, c1 = build_ai_engine()
c1.script = [picks_opening, picks_std, picks_upg]
s2 = GameSession(KB, RULES, engine=eng)
s2.on_game_start({"ally": [HERO], "enemy": ["黑暗之女"], "my_hero": HERO, "source": "t"})
s2.on_in_progress()
check("ctx 就绪", wait_ctx(eng))
s2.record_augment(REAL_AUG, tier="棱彩")
s2.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"], my_gold=500))
deadline = time.time() + 5
while len(c1.calls) < 2 and time.time() < deadline:
    time.sleep(0.02)
while s2.suggestion.source != "ai" and time.time() < deadline:
    time.sleep(0.02)
check("AI 精修覆盖 + 理由重写入刷新桶",
      s2.suggestion.source == "ai"
      and s2.suggestion.candidates[0].name == "焚天"
      and any(k == "next_items" and "版本答案" in t
              for _, k, t in s2.history["refresh"]),
      str(s2.suggestion.source))
n_calls = len(c1.calls)
n_refresh = len(s2.history["refresh"])
# 同情形（事实位一致 + 在屏 AI 已答）：事件帧直调也不重派不重记
s2._suggest_next_items(reason="event")
time.sleep(0.2)
check("同情形不重派不重记",
      len(c1.calls) == n_calls and len(s2.history["refresh"]) == n_refresh
      and s2.suggestion.source == "ai",
      f"calls={len(c1.calls)}/{n_calls} refresh={len(s2.history['refresh'])}/{n_refresh}")
# 事实变化（升级型海克斯入账）：结论未变也重派
s2.record_augment(UPG_AUG, round_id=99)
deadline = time.time() + 5
while len(c1.calls) < n_calls + 1 and time.time() < deadline:
    time.sleep(0.02)
check("事实变化照常重派", len(c1.calls) == n_calls + 1,
      f"calls={len(c1.calls)}/{n_calls + 1}")
# 同结论精修回包：不重写不重记（overlay 时间戳不动、历史不涨）
eng2, c1b = build_ai_engine()
c1b.script = [picks_opening, picks_std]
s3 = GameSession(KB, RULES, engine=eng2)
s3.on_game_start({"ally": [HERO], "enemy": ["黑暗之女"], "my_hero": HERO, "source": "t"})
s3.on_in_progress()
check("ctx 就绪(2)", wait_ctx(eng2))
s3.record_augment(REAL_AUG, tier="棱彩")
s3.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"], my_gold=500))
deadline = time.time() + 5
while s3.suggestion.source != "ai" and time.time() < deadline:
    time.sleep(0.02)
ts_before = s3.suggestion.ts
n_refresh3 = len(s3.history["refresh"])
s3.on_snapshot(make_snap(KB, "Aatrox", my_items=["多兰之刃"], my_gold=700))
time.sleep(0.3)
check("同结论回包不重写（overlay 内容与时间戳不动）",
      s3.suggestion.ts == ts_before
      and len(s3.history["refresh"]) == n_refresh3,
      f"entries={len(s3.history['refresh'])}/{n_refresh3}")

print("── ③ KIND_RANKS 精修入史/去重 ──")
from hexa.ai.engine import KIND_RANKS as KR
s4 = GameSession(KB, RULES)
s4.on_game_start({"ally": [HERO], "enemy": ["黑暗之女"], "my_hero": HERO, "source": "t"})
s4.on_in_progress()
s4.on_hex_ui(True)
s4.rank_augment_cards([REAL_AUG] * 3)   # 规则基准 → 决策桶
check("规则基准档位入决策桶",
      any(k == "card_ranks" for _, k, _ in s4.history["decision"]))
changed = [dict(r, grade=("A" if r["grade"] != "A" else "S"))
           for r in s4.card_ranks]
s4._on_engine_result(KR, changed, s4._card_serial)
check("评级结论变化 → 覆盖 + 决策桶新增",
      s4.card_ranks == changed
      and sum(1 for _, k, _ in s4.history["decision"] if k == "card_ranks") == 2,
      str([r['grade'] for r in s4.card_ranks]))
s4._on_engine_result(KR, [dict(r) for r in changed], s4._card_serial)
check("同结论评级回包不重记",
      sum(1 for _, k, _ in s4.history["decision"] if k == "card_ranks") == 2)

print(f"\n{'=' * 30}\n通过，失败 {len(FAIL)} 项 {FAIL if FAIL else ''}")
sys.exit(1 if FAIL else 0)
