"""合成链路测试：海克斯界面点击 → 记账台账 → 复盘日志（离线，不碰真屏/真鼠标）。

复现 app.py 的记账闭环接线（app.py:100-182 的 _on_state/_on_cards/_on_click/
_on_reroll 同逻辑），只打桩两层：
  1) 视觉层：OcrEngine.recognize 按用例返回预置 OCR 文本框（thefuzz 模糊
     匹配、valid_augment、NAME_ZONE 过滤全部走真实代码）；
  2) 屏幕层：FakeScreenDetector 覆写 _grab/_screen_size（返回合成帧，
     不截真屏），并短路 _save_templates（防写真实 calibrations/ 目录）。
其余全部真实：MouseLedger 20ms 轮询线程（GetAsyncKeyState/GetCursorPos
按脚本打桩，模拟 <50ms 快速点击的 LSB 捕获）、CardLedger 矩形映射、
DetectorCore 迟滞状态机、GameSession.record_augment / on_hex_ui、
PhaseMachine._handle（PreEndOfGame → on_end_of_game → _write_review_log）。

复盘日志落盘隔离：game_state.BASE_DIR 指到临时目录（同 tests/smoke_offline.py
做法），测试结束自动清理，不污染 log/games/。

运行：.venv/Scripts/python.exe experiments/test_hex_click_synthetic.py
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import hexa.core.game_state as _game_state
from hexa.core.game_state import GameSession
from hexa.core.lcu import IDLE_PHASES, PhaseMachine
from hexa.core.mouse_ledger import CardLedger, MouseLedger
from hexa.core.ocr_engine import OcrEngine
from hexa.core.rules_engine import RulesEngine
from hexa.core.screen_detect import (Layout, ScreenDetector, point_in_rect)
from hexa.knowledge.kb import KnowledgeBase

_TMP = Path(tempfile.mkdtemp(prefix="hex_click_syn_"))
_game_state.BASE_DIR = _TMP      # 复盘日志写入临时目录
import atexit
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

PASS, FAIL, PRINTED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


def wait_until(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


# ── 公共对象 ──────────────────────────────────────────────────────
kb = KnowledgeBase()
rules = RulesEngine(kb)
ocr = OcrEngine(kb)
N = sorted(kb.augment_names)[:6]          # 6 个真实库名
GARBAGE = "无漆安途"                       # 探针实测 thefuzz 对 229 库 @72 都不命中
assert not kb.valid_augment(GARBAGE)
assert ocr.match_augment(GARBAGE, 72) is None

HERO = "殇之木乃伊"
assert kb.validate_hero(HERO) == HERO


class FakeScreenDetector(ScreenDetector):
    """不截真屏：_grab 返回合成黑帧；模板保存短路（防写真实 calibrations/）"""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._fake_size = (2560, 1440)

    def _screen_size(self):
        return self._fake_size

    def _primary_size(self):
        return self._fake_size

    def _grab(self, screen=None):
        return np.zeros((self._fake_size[1], self._fake_size[0], 3), np.uint8)

    def _save_templates(self, frame, regions):
        pass


SCREEN_W, SCREEN_H = 2560, 1440
detector = FakeScreenDetector(kb, ocr, layout=Layout())
detector.last_cards = None
detector.last_cards_raw = None

session = GameSession(kb, rules)          # 主会话（用例 1-6 + 复盘前状态）
_teams = {"ally": [HERO, "黑暗之女"], "enemy": ["黑暗之女"],
          "my_hero": HERO, "map_id": "12", "source": "syn"}
session.on_champ_select(dict(_teams))
session.on_game_start(dict(_teams))
session.on_in_progress()

card_ledger = CardLedger(rects_provider=lambda: (
    detector.layout.to_pixels(SCREEN_W, SCREEN_H)["cards"]
    if detector.layout else None))


def _reroll_rects():
    px = detector.layout.to_pixels(SCREEN_W, SCREEN_H) if detector.layout else None
    return px["rerolls"] if px else None


def card_center(idx):
    x, y, w, h = detector.layout.to_pixels(SCREEN_W, SCREEN_H)["cards"][idx]
    return x + w // 2, y + h // 2


def reroll_center(idx=0):
    x, y, w, h = _reroll_rects()[idx]
    return x + w // 2, y + h // 2


_hint_ts = [0.0]
_active = [session]   # _on_click 当前写入的会话（app 闭包固定为唯一 session；
                      # 本测试多个会话分用例断言，用槽位切换）


def _on_click(x, y):
    # ↓↓↓ 与 app.py:139-169 逐行同逻辑（record_augment 目标 = _active[0]）↓↓↓
    for rr in _reroll_rects() or []:
        if point_in_rect(x, y, rr):
            return
    idx = card_ledger.card_at(x, y)
    if idx is None:
        return
    if not detector.recent_hex_activity():
        return
    cards = detector.last_cards or detector.last_cards_raw
    name = cards[idx] if cards and idx < len(cards) else None
    if not name or not kb.valid_augment(name):
        name = detector.sync_ocr_card(idx)
    if not name or not kb.valid_augment(name):
        now = time.time()
        if now - _hint_ts[0] > 5.0:
            _hint_ts[0] = now
            PRINTED.append(f"[LEDGER] 卡片{idx + 1} 点击未记账：海克斯名未识别出来")
        return
    if _active[0].record_augment(name, card_idx=idx):
        PRINTED.append(f"[LEDGER] 记账: 卡片{idx + 1} {name}")
    else:
        PRINTED.append(f"[LEDGER] {name} 已在台账，忽略重复记录")


def _on_state(active, round_id):
    # 与 app.py:100-109 同逻辑
    if active:
        session.on_hex_ui(True)
        PRINTED.append("[DETECT] 海克斯界面活跃")
    else:
        detector.last_cards = None
        detector.last_cards_raw = None
        session.clear_card_ranks()
        session.on_hex_ui(False)
        PRINTED.append("[DETECT] 海克斯界面结束")


def _on_cards(stable_cards, round_id):
    # 与 app.py:111-117 同逻辑（本测试不需要 AI 评级）
    detector.last_cards = list(stable_cards)
    PRINTED.append(f"[DETECT] 本轮卡片: {stable_cards}")


detector.on_state = _on_state
detector.on_cards = _on_cards
detector.on_calibrate_hint = None


def ocr_stub(text, rect=(10, 220, 120, 40), score=0.95):
    """把 OcrEngine.recognize 打桩为恒返回一条预置文本（rect 落 NAME_ZONE 内）
    rect 中心 y=240，卡框高 ~704px 的 0.30~0.62 带 = 211~436 ✓"""
    ocr.recognize = lambda image, _out=[(text, list(rect), score)]: list(_out)


def ocr_silent_spy(record):
    """recognize 桩：返回空且记录被调用（断言"未走补扫"用）"""
    ocr.recognize = lambda image: record.append(1) or []


def activate_ui():
    """DetectorCore 两帧标题命中 → active，并触发 on_state(True)（同 _tick）"""
    if detector.core.state != "active":
        for _ in range(2):
            detector.core.feed_titles(2)
            detector._fire_state_change()


def close_ui():
    """连续 3 帧无命中 → inactive + on_state(False)（同 _tick 迟滞）"""
    for _ in range(3):
        detector.core.feed_titles(0)
        detector._fire_state_change()


def pump_stable_cards(cards):
    """喂两帧相同卡片内容 → 稳定闩锁 → 消费并回调 on_cards（同 _tick 尾部）"""
    detector.core.feed_cards(list(cards))
    detector.core.feed_cards(list(cards))
    stable = detector.core.consume_cards()
    if stable and detector.on_cards and detector.count_valid_cards(stable) >= 2:
        detector.on_cards(stable, detector.core.round_id)


# ── 键盘/鼠标态脚本（打桩 GetAsyncKeyState / GetCursorPos） ────────
class KeyScript:
    def __init__(self, seq):
        self.seq = list(seq)

    def pop(self):
        return self.seq.pop(0) if self.seq else False


_holder = {"script": KeyScript([])}
MouseLedger.left_button_down = staticmethod(lambda: _holder["script"].pop())
MouseLedger.cursor_pos = staticmethod(lambda: (0, 0))


def run_click_through_ledger(x, y, on_reroll=None):
    """把一次"按下沿"灌进真实 MouseLedger 轮询线程（20ms 周期）。
    [True, False] 序列 = 按下+抬起在一个轮询间隔内完成（<50ms 快速点击，
    旧版只看高位会整次漏掉、新版靠 LSB 捕获）——验证 mouse_ledger.py:56-59。"""
    _holder["script"] = KeyScript([True, False])
    MouseLedger.cursor_pos = staticmethod(lambda: (x, y))
    led = MouseLedger(on_click=_on_click,
                      on_reroll=on_reroll or (lambda: None),
                      reroll_rect_provider=_reroll_rects)
    led.start()
    led_stopped = threading.Event()

    def _stop():
        led.stop()
        led.join(timeout=2.0)
        led_stopped.set()
    return _stop


# ══ 用例 1：<50ms 快速点击（LSB）经真实轮询线程 → 记账 ═════════════
print("\n[1] 稳定卡片 + <50ms 快速左键点击（经 MouseLedger 真实线程）")
activate_ui()
pump_stable_cards([N[0], N[1], N[2]])
check("界面活跃 + 卡片稳定回调", detector.core.state == "active"
      and detector.last_cards == [N[0], N[1], N[2]],
      f"state={detector.core.state} last_cards={detector.last_cards}")
stop = run_click_through_ledger(*card_center(0))
ok = wait_until(lambda: len(session.augments) == 1)
stop()
check("<50ms 快速点击记账成功且仅一条",
      ok and len(session.augments) == 1 and session.augments[0]["name"] == N[0],
      f"augments={session.augments}")

# ══ 用例 2：同卡重复点击 → 防重复 ═════════════════════════════════
print("\n[2] 同名强化重复点击防重")
_on_click(*card_center(0))
check("重复点击不产生第二条台账", len(session.augments) == 1
      and any("已在台账" in p for p in PRINTED[-1:]),
      f"augments={session.augments}")

# ══ 用例 3：刷新按钮矩形命中 → 路由 on_reroll，不记账 ═════════════
print("\n[3] reroll 矩形命中独占路由")
reroll_flag = []
stop = run_click_through_ledger(*reroll_center(0), on_reroll=lambda: reroll_flag.append(1))
wait_until(lambda: bool(reroll_flag))
stop()
check("reroll 命中触发 on_reroll 且不改台账",
      bool(reroll_flag) and len(session.augments) == 1)

# ══ 用例 4：reroll 后识别未稳定（无稳定/原始读数）→ sync_ocr_card 补扫记账 ══
print("\n[4] 识别未稳定时的点击补扫")
detector.force_rescan()
detector.last_cards = None
detector.last_cards_raw = None
session.on_hex_reroll()          # 与 app._on_reroll（app.py:171-176）一致
ocr_calls = []
ocr_silent_spy(ocr_calls)
ocr_stub(N[1])                   # 补扫单卡应读出卡 2 的名字
_on_click(*card_center(1))
check("补扫成功记账", any(a["name"] == N[1] for a in session.augments)
      and any(f"记账: 卡片2 {N[1]}" in p for p in PRINTED),
      f"augments={session.augments} printed={PRINTED[-3:]}")

# ══ 用例 5：界面关闭后的 10s 证据窗内点击 → 仍可补扫记账 ══════════
print("\n[5] 界面关闭后时间窗内点击")
close_ui()
check("三帧无命中 → 界面判定关闭 + 读数清空",
      detector.core.state == "inactive" and detector.last_cards is None
      and detector.last_cards_raw is None)
detector._last_hex_evidence = time.time() - 2.0    # 2s 前尚有海克斯证据
check("关闭后 2s 点击仍在证据窗内", detector.recent_hex_activity() is True)
ocr_stub(N[2])
_on_click(*card_center(2))
check("关闭后窗内点击补扫记账", any(a["name"] == N[2] for a in session.augments),
      f"augments={session.augments}")

# ══ 用例 6：证据窗过期（>10s）→ 静默丢弃 ═════════════════════════
print("\n[6] 界面关闭超过时间窗")
detector._last_hex_evidence = time.time() - 11.0
check("recent_hex_activity 判 False", detector.recent_hex_activity() is False)
ocr_stub(N[3])
n_before = len(session.augments)
_on_click(*card_center(0))
check("过期点击不记账（且不触发补扫语义——gate 先行）",
      len(session.augments) == n_before)

# ══ 用例 7：OCR 读出的名字不在 KB → 丢弃 + 限频提示（无兜底记录） ══
print("\n[7] 库外名字（valid_augment 失败）")
s2 = GameSession(kb, rules)
_active[0] = s2
detector._last_hex_evidence = time.time()
detector.last_cards = [GARBAGE, None, None]      # 稳定读数里是库外名
detector.last_cards_raw = None
ocr_stub(GARBAGE)                                 # 补扫也读出库外名
_on_click(*card_center(0))
check("库外名字被丢弃（不记账）", len(s2.augments) == 0)
check("丢弃时有限频提示日志", any("未记账" in p for p in PRINTED),
      f"printed={PRINTED[-2:]}")

# ══ 用例 8：last_cards 为空但 last_cards_raw 命中库名 → 免补扫直记 ═
print("\n[8] 最近一帧读数兜底")
detector.last_cards = None
detector.last_cards_raw = [N[3], None, None]
spy = []
ocr_silent_spy(spy)
_on_click(*card_center(0))
check("raw 读数直记（未走补扫）", any(a["name"] == N[3] for a in s2.augments)
      and not spy, f"augments={s2.augments} spy={spy}")

# ══ 用例 9：PreEndOfGame → 复盘日志落盘 ══════════════════════════
print("\n[9] PreEndOfGame 触发复盘日志")
_active[0] = session
check("IDLE_PHASES 含 PreEndOfGame（lcu.py:37-38）", "PreEndOfGame" in IDLE_PHASES)

s4 = GameSession(kb, rules)
s4.on_champ_select(dict(_teams))
s4.on_game_start(dict(_teams))
s4.on_in_progress()
check("记账 N[4]", s4.record_augment(N[4], tier="棱彩"))


class FakeLCU:
    def is_connected(self):
        return True

    def connect(self):
        return True

    def get_gameflow_phase(self):
        return None

    def get_both_teams(self):
        return dict(_teams)


pm = PhaseMachine(FakeLCU(), callbacks={
    "on_champ_select": s4.on_champ_select,
    "on_game_start": s4.on_game_start,
    "on_in_progress": s4.on_in_progress,
    "on_end_of_game": s4.on_end_of_game,
})
pm._handle("InProgress")
pm._handle("PreEndOfGame")        # 真机序列：InProgress → PreEndOfGame → EndOfGame


def _log_files():
    d = _TMP / "log" / "games"
    return sorted(d.glob("*.json")) if d.exists() else []


ok = wait_until(lambda: len(_log_files()) >= 1)
check("PreEndOfGame → 复盘日志已落盘", ok and len(_log_files()) == 1,
      f"files={[p.name for p in _log_files()]}")
if ok:
    doc = json.loads(_log_files()[0].read_text(encoding="utf-8"))
    check("日志含台账/结局/时间线",
          [a["name"] for a in doc["augments"]] == [N[4]]
          and any(e["event"] == "end_of_game" and e["detail"] == "PreEndOfGame"
                  for e in doc["timeline"])
          and doc["hero"] == HERO,
          f"augments={doc['augments']} hero={doc['hero']}")
check("on_end_of_game 后会话已重置（日志先行于 reset，game_state.py:213-221）",
      s4.stage == "idle" and s4.augments == [])

# ══ 汇总 ═════════════════════════════════════════════════════════
print(f"\n===== 合成链路 {len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
for f in FAIL:
    print(f"  ❌ FAILED: {f}")
sys.exit(1 if FAIL else 0)
