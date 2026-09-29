"""临时核验脚本（核验用，非回归资产）：hex 域 4 项修复 + 2 项额外必做。v3

v3（2026-09-29 二轮核验回摆后）：断言更新为修复后行为——
  P1-6  sync_ocr_card_raw 改直连 recognize 原文（不再经 _ocr_cards@72
        预过滤），命中透传装饰原文、库外名留原文，留痕不再恒 raw=None；
        flush 非空门扩入 augment_unverified（纯留痕局也落盘）
  额外2 record_augment 拒记时不再错位 ✓已选（保持原卡），并留
        augment_duplicate_click 轨迹供真机定性；防重复记账本身保留
        （防同卡重按误记；模式是否允许同名重复无仓库数据可证）
  augments.json ？？？ 残留条目已剔除

覆盖：
  P2-9  hero_augments.csv「？？？」占位行装载过滤（词表/榜/valid_augment/OCR 词表）
  P1-5  复盘日志兜底 flush_pending_review 场景（App 退出 / 下局补写 / 空局 /
        写盘失败可重试 / 正常终局后空转）——BASE_DIR 指临时目录，不污染 log/games/
  P1-6  sync_ocr_card_raw 路径 + app._on_click 丢弃分支写 augment_unverified
        且随复盘日志落盘（点击分支按 app.py:139-175 逐行镜像）
  P1-7  app._on_reroll 活跃度门（源码断言 + 行为镜像：无证据不重扫）
  额外1 未知名/缺 KB 条目评级不凭空给高分（rank_augments + RuleDecisionEngine）
  额外2 同名重复记账：拒记+不错位+轨迹事件

运行：.venv/Scripts/python.exe experiments/verify_hex_area_fixes.py
"""
import csv
import json
import shutil
import sys
import tempfile
import time
import atexit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import hexa.core.game_state as _game_state
from hexa.core.game_state import GameSession
from hexa.core.rules_engine import RulesEngine
from hexa.core.screen_detect import Layout, ScreenDetector, point_in_rect
from hexa.knowledge.kb import KnowledgeBase
from hexa.core.ocr_engine import OcrEngine
from hexa.ai.engine import RuleDecisionEngine
from hexa.core.mouse_ledger import CardLedger

_TMP = Path(tempfile.mkdtemp(prefix="verify_hex_"))
_game_state.BASE_DIR = _TMP
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


ROOT = Path(__file__).resolve().parent.parent
kb = KnowledgeBase()
rules = RulesEngine(kb)
print("\n[P2-9] hero_augments.csv「？？？」占位行")

# ① CSV 原始数据：占位行仍在上游数据里（本修复只做装载过滤，不重抓）
n_ph = 0
sample_amumu = None
names_raw = set()
with open(ROOT / "data" / "hero_augments.csv", encoding="utf-8-sig") as f:
    reader = csv.reader(f)
    next(reader, None)                      # 表头（kb 装载同样跳过）
    for row in reader:
        if len(row) < 6:
            continue
        names_raw.add(row[5].strip())
        if row[5].strip() == "？？？":
            n_ph += 1
            if row[0].strip() == "殇之木乃伊" and sample_amumu is None:
                sample_amumu = row
check(f"CSV（除表头）含 ？？？ 占位行 97 条", n_ph == 97, f"n_ph={n_ph}")
check("CSV 样例行 = ['殇之木乃伊','Amumu','棱彩',…,…,'？？？']",
      sample_amumu is not None and sample_amumu[1] == "Amumu"
      and sample_amumu[2] == "棱彩" and sample_amumu[5] == "？？？",
      str(sample_amumu))

# ② 装载后：词表/榜/valid_augment 全部剔除占位（真名 229 个里被上游缺失 1 个
#    → CSV 真名唯一数 = 228；kb.augment_names 与之完全一致）
check("kb.augment_names = 228 且与 CSV 去占位唯一名集合完全一致",
      len(kb.augment_names) == 228
      and kb.augment_names == (names_raw - {"？？？"}),
      f"got {len(kb.augment_names)}")
check("valid_augment('？？？') = False", kb.valid_augment("？？？") is False)
leak = [h for h, d in kb.hero_augments.items() if "？？？" in d]
check("全英雄先验榜无 ？？？ 条目", not leak, str(leak[:5]))

# ③ OCR 词表源头即 kb.augment_names → 占位不进匹配名单
ocr = OcrEngine(kb)
ocr.match_augment("不祥契约", 72)          # 触发惰性固化
check("OCR 匹配名单（kb.augment_names 固化）不含 ？？？",
      ocr._augment_names is not None and "？？？" not in ocr._augment_names,
      f"n={len(ocr._augment_names or [])}")
check("？？？ 全标点清洗后 len<2 → match_augment 直接 None",
      ocr.match_augment("？？？", 0) is None)
tag_leftover = kb.augment_tags.get("？？？")
check("augments.json ？？？ 残留条目已剔除（空 tags 纯占位，运行时本无消费者）",
      tag_leftover is None and "？？？" not in kb.augment_tags)

# ── P1-5 复盘日志兜底 ────────────────────────────────────────────
TEAMS = {"ally": ["殇之木乃伊", "黑暗之女"], "enemy": ["黑暗之女"],
         "my_hero": "殇之木乃伊", "map_id": "12", "source": "verify"}
_T0 = time.time()


def new_session(t0):
    """独立 _game_t0 → 复盘文件名互不冲突（同秒同英雄会同名覆盖）"""
    s = GameSession(kb, rules)
    s.on_champ_select(dict(TEAMS))
    s.on_game_start(dict(TEAMS))
    s.on_in_progress()
    s._game_t0 = t0
    return s


def log_files():
    d = _TMP / "log" / "games"
    return sorted(d.glob("*.json")) if d.exists() else []


print("\n[P1-5] flush_pending_review 场景")
# 场景A：对局中 App 退出（app.py:401 finally 同路径）→ flush 落盘；幂等
sa = new_session(_T0 - 50000)
assert sa.record_augment("不祥契约", tier="棱彩")
n0 = len(log_files())
sa.flush_pending_review()                      # ≈ app.py:401
files_a = log_files()
check("场景A 对局中退出 → flush 产出复盘文件", len(files_a) == n0 + 1,
      f"files={[p.name for p in files_a]}")
sa.flush_pending_review()                      # 再调一次（幂等）
check("场景A 重复 flush 幂等（_review_flushed 置位不再写）",
      len(log_files()) == n0 + 1)
doc_a = json.loads(files_a[-1].read_text(encoding="utf-8"))
check("场景A 文件含台账（退出兜底 flush 带 review_flush 标记，game_state.py:993 对一切兜底补写统一打标）",
      [a["name"] for a in doc_a["augments"]] == ["不祥契约"]
      and any(e["event"] == "review_flush" for e in doc_a["timeline"]))

# 场景B：终局事件丢失 → 下一局 champ_select 兜底补写，timeline 带 review_flush
sb = new_session(_T0 - 40000)
assert sb.record_augment("不祥契约", tier="棱彩")
n1 = len(log_files())
sb.on_champ_select(dict(TEAMS))                # 下一局选人（未发生任何 end_of_game）
files_b = log_files()
check("场景B 下一局 champ_select 补写上一局", len(files_b) == n1 + 1,
      f"files={[p.name for p in files_b]}")
doc_b = json.loads(files_b[-1].read_text(encoding="utf-8"))
check("场景B 文件 timeline 含 review_flush 事件且台账保留",
      any(e["event"] == "review_flush" for e in doc_b["timeline"])
      and [a["name"] for a in doc_b["augments"]] == ["不祥契约"])

# 场景C：空局（无台账无历史）不产文件
sc = new_session(_T0 - 30000)
sc.on_champ_select(dict(TEAMS))                # 秒退重开，全程无记账
check("场景C 空局 flush 不产文件", len(log_files()) == n1 + 1)

# 场景D：写盘失败 → 标记保持 False，恢复后可重试
sd = new_session(_T0 - 20000)
assert sd.record_augment("不祥契约", tier="棱彩")
bad = _TMP / "not_a_dir"
bad.write_text("x", encoding="utf-8")          # BASE_DIR 指向文件 → mkdir 必败
orig_base = _game_state.BASE_DIR
_game_state.BASE_DIR = bad
sd.flush_pending_review()
check("场景D 写盘失败不崩溃且 _review_flushed 保持 False",
      sd._review_flushed is False)
_game_state.BASE_DIR = orig_base
sd.flush_pending_review()
check("场景D 恢复后重试成功产文件", len(log_files()) == n1 + 2,
      f"files={[p.name for p in log_files()]}")

# 正常终局后 flush 空转（reset 已清场 + _review_flushed 由 reset 复位但 stage=idle 拦住）
se = new_session(_T0 - 10000)
assert se.record_augment("不祥契约", tier="棱彩")
n2 = len(log_files())
se.on_end_of_game("PreEndOfGame")
se.flush_pending_review()
check("正常终局（on_end_of_game 已写+已 reset）后 flush 空转不重复产文件",
      len(log_files()) == n2 + 1)

# ── P1-6 sync_ocr_card_raw 路径 + 丢弃分支留痕 ───────────────────
print("\n[P1-6] sync_ocr_card_raw / augment_unverified 留痕")


class FakeScreenDetector(ScreenDetector):
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


det = FakeScreenDetector(kb, ocr, layout=Layout())
det.last_cards = None
det.last_cards_raw = None

ocr.recognize = lambda image, _out=[("「不祥契约」", [10, 220, 120, 40], 0.95)]: list(_out)
hit = det.sync_ocr_card_raw(0)
check("路径① 命中 → (库名, 装饰原文)。补扫直连 recognize，不经 "
      "match_augments_in@72 归一化，OCR 装饰原文透传（2026-09-29 核验后修正）",
      hit == ("不祥契约", "「不祥契约」"), str(hit))

ocr.recognize = lambda image, _out=[("无漆安途", [10, 220, 120, 40], 0.95)]: list(_out)
miss = det.sync_ocr_card_raw(1)
check("路径② 库外文本 → (None, OCR原文)：补扫直连 recognize 原文，"
      "@80 不命中时留名字带内相似度最高原文（修复前经 _ocr_cards@72 "
      "预过滤被置 None，留痕恒 raw=None）",
      miss == (None, "无漆安途"), str(miss))

ocr.recognize = lambda image: []
empty = det.sync_ocr_card_raw(2)
check("路径③ 空读数 → (None, None)", empty == (None, None), str(empty))

oor = det.sync_ocr_card_raw(9)
check("路径④ idx 越界安全 → (None, None)", oor == (None, None), str(oor))

ocr.recognize = lambda image, _out=[("不祥契约", [10, 220, 120, 40], 0.95)]: list(_out)
thin = det.sync_ocr_card(0)   # 薄壳委托：原返回值语义不变（库名|None）
check("薄壳 sync_ocr_card 保持原返回值（库名|None）", thin == "不祥契约", str(thin))

# P1-6b async 补扫（产品路径 2026-09-29）：点击抓帧定格 → 后台线程识别 →
# 回调，记账线程不再被同步补扫阻塞 ~2s（用户案："选的时候直接截个图
# 后台分析"）
ocr.recognize = lambda image, _out=[("「不祥契约」", [10, 220, 120, 40], 0.95)]: list(_out)
_cb = []
det.async_ocr_card_raw(0, lambda n, r: _cb.append((n, r)))
_deadline = time.time() + 5
while not _cb and time.time() < _deadline:
    time.sleep(0.02)
check("P1-6b async 后台识别回调 (库名, 装饰原文)，与 sync 同口径",
      _cb == [("不祥契约", "「不祥契约」")], str(_cb))
_cb2 = []
det.async_ocr_card_raw(9, lambda n, r: _cb2.append((n, r)))
check("P1-6b 抓帧越界当场回调 (None, None)（不丢回调）",
      _cb2 == [(None, None)], str(_cb2))

# app.py 源码断言：丢弃分支调 raw 版 + 写 timeline + 提示语含刷新指引；退出 flush
app_src = (ROOT / "app.py").read_text(encoding="utf-8")
seg = app_src[app_src.index("def _on_click"):app_src.index("def _on_reroll")]
seg_r = app_src[app_src.index("def _on_reroll"):app_src.index("ledger = MouseLedger")]
check("app._on_click 点击抓帧走 async_ocr_card_raw 后台补扫，"
      "augment_unverified 留痕 + 提示刷新 augments.json（_record_click 共用）",
      "async_ocr_card_raw" in seg and "augment_unverified" in seg
      and "请刷新 augments.json" in seg and "_record_click" in seg)
check("app._on_reroll 门控先于 force_rescan（recent_hex_activity）",
      seg_r.index("recent_hex_activity") < seg_r.index("force_rescan"))
check("app.py finally 在 ledger.stop() 后调 flush_pending_review()",
      app_src.rindex("ledger.stop()") < app_src.rindex("session.flush_pending_review()"))

# 行为镜像（app.py:139-175 逐行同逻辑）：库外名点击 → 不进台账、timeline 留痕 → 随复盘落盘
card_ledger = CardLedger(rects_provider=lambda: (
    det.layout.to_pixels(2560, 1440)["cards"] if det.layout else None))
s_click = new_session(_T0 - 5000)
det.core.state = "active"          # 活跃期（绕过迟滞，聚焦丢弃分支）
det._last_hex_evidence = time.time()
det.last_cards = ["无漆安途", None, None]
ocr.recognize = lambda image, _out=[("无漆安途", [10, 220, 120, 40], 0.95)]: list(_out)

idx = card_ledger.card_at(*(lambda x, y, w, h: (x + w // 2, y + h // 2))(
    *det.layout.to_pixels(2560, 1440)["cards"][0]))
name = (det.last_cards or det.last_cards_raw)[idx]
raw = None
if not name or not kb.valid_augment(name):
    name, raw = det.sync_ocr_card_raw(idx)
if not name or not kb.valid_augment(name):
    s_click._log("augment_unverified", f"卡{idx + 1} raw={raw}")
check("库外名点击不进台账、不设 hex_picked", len(s_click.augments) == 0
      and s_click.hex_picked is None)
ev = [e for e in s_click.timeline if e[1] == "augment_unverified"]
check("timeline 留痕 augment_unverified（detail 含 raw 原文）",
      len(ev) == 1 and ev[0][2] == "卡1 raw=无漆安途", str(s_click.timeline[-3:]))
# 纯留痕局（零台账零建议）也要落盘：flush 非空门含 augment_unverified
# （否则知识库落后案的线索随 App 退出丢失；2026-09-29 核验后扩门）
n3 = len(log_files())
s_click.flush_pending_review()
doc_c = json.loads(log_files()[-1].read_text(encoding="utf-8"))
check("纯留痕局随复盘日志落盘（无需建议历史；augments 空、timeline 含该事件）",
      len(log_files()) == n3 + 1 and doc_c["augments"] == []
      and any(e["event"] == "augment_unverified" for e in doc_c["timeline"]))

# ── P1-7 reroll 门控行为 ─────────────────────────────────────────
print("\n[P1-7] _on_reroll 活跃度门")
rescans = []
det.force_rescan = lambda: rescans.append(1)
rerolled = []


def _on_reroll_mirror():                      # app.py:177-184 同逻辑
    if not det.recent_hex_activity():
        return
    det.force_rescan()
    det.last_cards = None
    det.last_cards_raw = None
    rerolled.append(1)


det.core.state = "inactive"
det._last_hex_evidence = time.time() - 30.0   # 界面关闭 >10s
_on_reroll_mirror()
check("无海克斯证据 → 不重扫、不动会话状态", not rescans and not rerolled)
det.core.state = "active"
det._last_hex_evidence = time.time()
_on_reroll_mirror()
check("活跃期 → 正常触发重扫", bool(rescans) and bool(rerolled))

# ── 额外1：未知名/缺 KB 条目评级不凭空给高分 ─────────────────────
print("\n[额外1] 未知名评级")
top1 = kb.top_augments("殇之木乃伊", 5)[0]   # 木木先验 top1（必 S 档）
r = rules.rank_augments("殇之木乃伊",
                        ["zzqqxx", "无漆安途", "", top1], taken=["zzqqxx"])
by = {x["name"]: x for x in r}
check("未知名/空名 grade='?' reason='识别失败'（非 S/A/B）",
      by.get("zzqqxx", {}).get("grade") == "?"
      and by.get("zzqqxx", {}).get("reason") == "识别失败"
      and by.get("无漆安途", {}).get("grade") == "?"
      and by.get("?", {}).get("name") == "?", str(r))
check(f"库名正常评级（先验榜内，{top1} 应 S）",
      by[top1]["grade"] == "S", str(by.get(top1)))
eng = RuleDecisionEngine(kb, rules)
r2 = eng.rank_cards("殇之木乃伊", ["zzqqxx", top1], [], [], None, 1)
check("RuleDecisionEngine.rank_cards 同口径（未知名 '?'）",
      r2[0]["grade"] == "?" and r2[1]["grade"] == "S", str(r2))

# ── 额外2：同名重复记账（拒记保留——防同卡重按误记；错位已修+留轨迹） ─
print("\n[额外2] 同名重复记账")
sdup = GameSession(kb, rules)
first = sdup.record_augment("不祥契约", card_idx=0)
second = sdup.record_augment("不祥契约", card_idx=1)
check("同名第二次拒记（False）且 ✓已选不错位（保持原卡 0）",
      first is True and second is False
      and sdup.hex_picked == (0, "不祥契约") and len(sdup.augments) == 1,
      f"hex_picked={sdup.hex_picked} augments={sdup.augments}")
check("同名重复点击留 augment_duplicate_click 轨迹（真机定性"
      "『模式是否允许同名重复』的数据依据）",
      any(e[1] == "augment_duplicate_click" and "卡2" in e[2]
          for e in sdup.timeline), str(sdup.timeline[-2:]))
check("无 card_idx 的防重复路径不产生轨迹（同 smoke :429 口径）",
      sdup.record_augment("不祥契约") is False
      and sum(1 for e in sdup.timeline
              if e[1] == "augment_duplicate_click") == 1)

print(f"\n===== 核验 {len(PASS)}/{len(PASS) + len(FAIL)} 通过 =====")
for f in FAIL:
    print(f"  ❌ FAILED: {f}")
sys.exit(1 if FAIL else 0)
