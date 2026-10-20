"""上局回顾数据准备（纯函数、零 Tk 依赖）

复盘 JSON 由 game_state._write_review_log 局终落盘到
log/games/<时刻>_<英雄>.json（对局期草稿周期覆盖写同一份；20261013
回顾面板案之前，这份产物玩家根本看不到）。本模块只读消费：

- latest_review_path(base_dir)  最近一份复盘文件（mtime 最新；
  空/缺目录 → None）
- load_review(path)             读 + 解析（坏 JSON / 顶层非 dict → None，
  绝不抛）
- format_review(doc)            JSON → 面板展示行（缺字段/类型异常一律
  占位，绝不抛）

边界（AGENTS「数据出事实，模型出判断」）：format_review 只做字段映射
与占位，不做知识库再判——出装偏差以「终局出装 × 主线(终局)」两行事实
并列呈现，偏差判断留给玩家；全部字段原值直出，无预消化结论。suggestions
字段新旧双格式兼容（N2 复盘分层案 20261019：新落盘为按 kind 分桶 dict，
旧文件平列表照读不崩；两桶合并按 ts 升序，"建议 N 次/最近一条"语义
不变）；其余落盘格式零改动（向后兼容新字段＝多出的键读不到就占位）。

本局账本（N4 20261019 产品评估 §3.5/§4-N4）：compute_ledger /
format_ledger 把复盘 JSON 升级成「本局账本」一屏——出门装听劝、每轮
海克斯三选采纳、出装建议采纳、胜负 + KDA + 分均经济。采纳判定是纯
**事实对账**（推荐集合 vs 购买事件/台账选择的时间对齐），不做语义裁判：
不维护任何装备/海克斯名单、不设阈值，parse 只从本仓库 render() 固定
行格式提取名字/档位，对不上一律占位。旧文件缺 item_events 等新键时
对应维度如实标"未记录"，绝不编造。
"""
import json
import re
from pathlib import Path

from hexa import config as _config

# 复盘目录相对 BASE_DIR（与 game_state._write_review_log 同一定位）
GAME_DIR = Path("log") / "games"


def latest_review_path(base_dir=None):
    """log/games/ 下最近写入的一份复盘 JSON；无目录/无文件返回 None。

    mtime 为准（草稿周期覆盖写，mtime 即最后一次落盘时刻；文件名里的
    时间戳是对局开始时刻，跨局比较不如 mtime 可靠），mtime 平局按文件
    名兜底。目录缺失/不可读按"无复盘"处理，不抛。
    """
    base = Path(base_dir or _config.BASE_DIR)
    try:
        files = list((base / GAME_DIR).glob("*.json"))
    except OSError:
        return None
    if not files:
        return None

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    return max(files, key=lambda p: (mtime(p), p.name))


def load_review(path):
    """读一份复盘 JSON；任何失败（缺失/坏 JSON/顶层非 dict）→ None。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _as_list(v):
    return v if isinstance(v, list) else []


def _as_str(v):
    return v if isinstance(v, str) else ""


def _suggestion_entries(doc) -> list:
    """建议条目（新旧双格式，N2 复盘分层案 20261019）。

    新格式 suggestions={decision:[...], refresh:[...]}（按 kind 分桶落
    盘）；旧格式为平列表——两桶合并按 ts 升序返回，面板"建议 N 次/
    最近一条"语义不变（旧文件照读不崩）。"""
    raw = doc.get("suggestions")
    if isinstance(raw, dict):
        entries = []
        for bucket in ("decision", "refresh"):
            for s in _as_list(raw.get(bucket)):
                if isinstance(s, dict):
                    entries.append(s)
        entries.sort(key=lambda s: s.get("ts")
                     if isinstance(s.get("ts"), (int, float)) else 0)
        return entries
    return [s for s in _as_list(raw) if isinstance(s, dict)]


def _dur_text(duration_s):
    """秒 → 「23.4 分钟」；缺失/非法（含 bool）→ 「-」"""
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        return "-"
    return f"{duration_s / 60:.1f} 分钟"


def format_review(doc):
    """复盘 dict → 面板展示行 list[str]（纯映射，绝不抛）。

    行序固定（UI 逐行 join 渲染，缺字段占位 "-"）：
      1. 英雄 · 开始时刻 · 时长
      2. 海克斯台账: 1. 档位 名；2. …（空 → "-"）
      3. 建议 N 次（最近: 最后一条建议首行）
      4. 终局出装: a、b、c
      5. 主线(终局): ap/ad/tank 原值
    """
    if not isinstance(doc, dict):
        doc = {}
    hero = _as_str(doc.get("hero")) or "-"
    started = _as_str(doc.get("started_at")) or "-"
    head = f"{hero} · {started} · {_dur_text(doc.get('duration_s'))}"

    aug_parts = []
    for a in _as_list(doc.get("augments")):
        if not isinstance(a, dict):
            continue
        name = _as_str(a.get("name"))
        if not name:
            continue
        tier = _as_str(a.get("tier"))
        # 先验推定档位标「先验」（N1 20261019）：reroll 换卡后先验≠实抽，
        # 上局回顾不把推定当实测念给玩家听
        if tier and _as_str(a.get("tier_src")) == "prior":
            tier = f"先验{tier}"
        aug_parts.append(f"{tier} {name}".strip())
    ledger = ("；".join(f"{i}. {p}" for i, p in enumerate(aug_parts, 1))
              if aug_parts else "-")

    sug = _suggestion_entries(doc)
    sug_line = f"建议 {len(sug)} 次"
    last_text = _as_str(sug[-1].get("text")) if sug else ""
    if last_text:
        first_line = last_text.splitlines()[0].strip()
        if first_line:
            sug_line += f"（最近: {first_line}）"

    items = [x for x in (_as_str(i) for i in _as_list(doc.get("final_items")))
             if x]
    items_line = "终局出装: " + ("、".join(items) if items else "-")
    path_line = "主线(终局): " + (_as_str(doc.get("current_path")) or "-")
    return [head, f"海克斯台账: {ledger}", sug_line, items_line, path_line]


# ── 本局账本（N4 20261019：事实对账，判断仍归玩家/模型） ──────────

def _parse_opening(text) -> list:
    """出门装建议文本 → 候选名列表。行格式出自 game_state.Suggestion.render
    通用分支：`[推荐1] 名 —— 理由`（固定输出，非自由文本）。"""
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^\[推荐\d\]\s*(.+?)\s+——", line)
        if m:
            out.append(m.group(1))
    return out


def _parse_card_ranks(text) -> list:
    """海克斯三选评级文本 → [(序号①②③, 名, 档)]。行格式
    `① 名（档）—— 理由`（game_state._render_card_ranks 固定输出）。"""
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^([①②③])\s*(.+?)（(.+?)）\s*——", line)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _parse_next(text) -> list:
    """出装主线建议文本 → 候选名列表。行格式
    `① 名（价格G，可买性）—— 理由`（render next_items 分支固定输出）。"""
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^[①②③]\s*(.+?)（", line)
        if m:
            out.append(m.group(1))
    return out


def _parse_swap(text) -> list:
    """替换建议文本 → 买件名列表。行格式
    `① 🔄 卖X → 买Y（差价NG…）—— 理由`（render swap_items 分支固定输出）；
    锚定「卖X → 买Y（」结构（该行唯一特征，emoji 前缀不参与匹配——
    UI 源码字符串禁彩色 emoji，见冒烟 UI 守卫）。"""
    out = []
    for line in str(text or "").splitlines():
        m = re.search(r"卖(.+?)\s*→\s*买(.+?)（", line)
        if m:
            out.append(m.group(2))
    return out


def _purchase_events(item_events) -> list:
    """装备变更序列 → 购买事件 [(ts, [新增名...])]：相邻变更帧 diff 取新增
    （卖出/合成掉的小件不是购买；组件合成成品时新增=成品，落在推荐集合
    内即视为采纳该推荐——时间对齐口径，无语义判断）。"""
    out, prev = [], set()
    for e in _as_list(item_events):
        if not isinstance(e, dict):
            continue
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        cur = [n for n in (_as_str(i) for i in _as_list(e.get("items"))) if n]
        fresh = [n for n in cur if n not in prev]
        if fresh:
            out.append((float(ts), fresh))
        prev = set(cur)
    return out


def _entry_ts(e) -> float | None:
    ts = e.get("ts") if isinstance(e, dict) else None
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    return None


def compute_ledger(doc) -> dict:
    """复盘 dict → 本局账本（事实对账；任何维度缺数据 → None/[]/"" 占位，
    绝不抛、绝不编造）。

    对账口径（全部时间对齐，无名单无阈值）：
    - 出门装：最早一条 opening 建议 → 其后首个购买事件（同帧新增集合
      与候选集合有交集 = 听劝）。
    - 海克斯：card_ranks 条目按「相邻条目 ts」切窗（同轮"基准+精修"
      双条按候选名集合相同合并为同一轮，防采纳率分母虚增）→ 落在窗内
      的台账（augments）选择即该轮所选；选中名 ∈ 当轮选项名集合 = 采纳，
      位次为选中卡的推荐序（数据直出）。
    - 出装：next_items/swap_items 条目逐条切窗 → 窗内购买事件与候选
      集合有交集 = 该条建议被采纳一次（窗互斥，一次购买至多归一条）。
    - 结果：落盘 result/kda/summoner_name/total_gold 原值直出，分均
      经济 = total_gold ÷ 时长分钟。
    """
    if not isinstance(doc, dict):
        doc = {}
    sug = _suggestion_entries(doc)
    picks = []
    for a in _as_list(doc.get("augments")):
        if not isinstance(a, dict):
            continue
        ts, name = _entry_ts(a), _as_str(a.get("name"))
        if ts is not None and name:
            picks.append((ts, name))
    picks.sort(key=lambda x: x[0])
    buys = _purchase_events(doc.get("item_events"))

    # ① 出门装（听劝）
    opening = None
    for e in sug:
        if e.get("kind") != "opening":
            continue
        ts0 = _entry_ts(e)
        cands = _parse_opening(_as_str(e.get("text")))
        first = next((f for f in buys
                      if ts0 is not None and f[0] >= ts0 and f[1]), None)
        opening = {
            "candidates": cands,
            "bought": (first[1] if first else []),
            "adopted": (bool(set(first[1]) & set(cands))
                        if first and cands else None),
        }
        break

    # ② 海克斯轮次（先按候选名集合合并"基准+精修"同轮双条）
    rounds, groups = [], []
    for e in (x for x in sug if x.get("kind") == "card_ranks"):
        opts = _parse_card_ranks(_as_str(e.get("text")))
        if not opts:
            continue        # 文本对不上（异常条目）不硬算，如实少一轮
        names = tuple(n for _, n, _ in opts)
        if groups and groups[-1][0] == names:
            groups[-1][2] = e   # 同轮：窗口终点延展（下一轮首条 ts 定界）
        else:
            groups.append([names, opts, e])
    for gi, (names, opts, first_e) in enumerate(groups):
        ts_i = _entry_ts(first_e)
        ts_next = (_entry_ts(groups[gi + 1][2])
                   if gi + 1 < len(groups) else None)
        # 窗口 [本轮首条 ts, 下一轮首条 ts)：同轮基准+精修合并后玩家点击
        # 落在精修条目前后都算本轮；台账选择（augments ts 晚于评级）落窗
        # 即为该轮所选，选中名 ∈ 当轮选项名集合 = 采纳（纯事实交集）
        picked = next((nm for t, nm in picks
                       if ts_i is not None and t >= ts_i
                       and (ts_next is None or t < ts_next)), "")
        name_list = list(names)
        adopted = (picked in name_list) if picked else None
        rank = (name_list.index(picked) + 1) if adopted else None
        rounds.append({"options": [dict(zip(("no", "name", "grade"), o))
                                   for o in opts],
                       "picked": picked,
                       "adopted": adopted, "rank": rank})

    # ③ 出装建议采纳（next_items/swap_items 混序逐条切窗，窗互斥）
    suggested = adopted_n = 0
    item_suggs = [x for x in sug
                  if x.get("kind") in ("next_items", "swap_items")]
    for i, e in enumerate(item_suggs):
        cands = (_parse_next if e.get("kind") == "next_items"
                 else _parse_swap)(_as_str(e.get("text")))
        ts_i, ts_next = _entry_ts(e), (_entry_ts(item_suggs[i + 1])
                                       if i + 1 < len(item_suggs) else None)
        if not cands or ts_i is None:
            continue
        suggested += 1
        cset = set(cands)
        hit = any(t >= ts_i and (ts_next is None or t < ts_next)
                  and cset.intersection(items)
                  for t, items in buys)
        if hit:
            adopted_n += 1

    # ④ 结果（原值直出 + 分均折算）
    kda = doc.get("kda")
    if not isinstance(kda, dict):
        kda = None
    duration = doc.get("duration_s")
    total_gold = doc.get("total_gold")
    gpm = None
    if (isinstance(total_gold, (int, float)) and not isinstance(total_gold, bool)
            and isinstance(duration, (int, float))
            and not isinstance(duration, bool) and duration > 0):
        gpm = round(total_gold / (duration / 60.0), 1)
    return {
        "result": _as_str(doc.get("result")),
        "kda": kda,
        "summoner_name": _as_str(doc.get("summoner_name")),
        "gpm": gpm,
        "opening": opening,
        "hex_rounds": rounds,
        "items": {"suggested": suggested, "adopted": adopted_n,
                  "has_buys": bool(buys)},
    }


def format_ledger(doc) -> list:
    """账本 dict → 面板展示行（纯映射，绝不抛）。行布局对应 §4-N4 表格：
    结果 / 出门装听劝 / 海克斯采纳 / 出装采纳。缺数据段占位，旧复盘
    （无 item_events/result 等新键）如实标「未记录」。"""
    led = compute_ledger(doc)

    # 结果行：胜负 · KDA · 分均经济 · 召唤师名（拿得到才显示该段）
    kda = led.get("kda") or {}
    parts = []
    if led.get("result") == "win":
        parts.append("胜")
    elif led.get("result"):
        parts.append("负")
    if all(isinstance(kda.get(k), int)
           and not isinstance(kda.get(k), bool)
           for k in ("kills", "deaths", "assists")):
        parts.append(f"KDA {kda['kills']}/{kda['deaths']}/{kda['assists']}")
    if led.get("gpm") is not None:
        parts.append(f"分均 {led['gpm']:.0f}G")
    if led.get("summoner_name"):
        parts.append(str(led["summoner_name"]))
    rows = ["结果: " + (" · ".join(parts) if parts else "-（未记录）")]

    # 出门装行：推荐 vs 实买（听劝率）
    op = led.get("opening")
    if not op or not op.get("candidates"):
        rows.append("出门装: -（无推荐记录）")
    else:
        verdict = {True: "✓听劝", False: "未听劝", None: "实买未记录"}[
            op.get("adopted")]
        bought = "、".join(op.get("bought") or [])
        rows.append(f"出门装: 推荐 {'/'.join(op['candidates'])}"
                    f" → 实买 {bought or '？'}（{verdict}）")

    # 海克斯行：每轮给了什么、选了哪张（采纳率）
    rounds = led.get("hex_rounds") or []
    judged = [r for r in rounds if r.get("adopted") is not None]
    adopted = [r for r in judged if r.get("adopted")]
    if not rounds:
        rows.append("海克斯: -（无三选评级记录）")
    else:
        rows.append(f"海克斯: {len(adopted)}/{len(judged)} 轮选中推荐卡")
        for i, r in enumerate(rounds, 1):
            opts = " ".join(f"{o.get('no', '')}{o.get('name', '')}"
                            f"（{o.get('grade', '')}）"
                            for o in (r.get("options") or []))
            if r.get("adopted"):
                tail = f"→ 选 {r.get('picked', '?')}（推荐第{r.get('rank')}位）✓"
            elif r.get("picked"):
                tail = f"→ 选 {r.get('picked', '?')}（不在当轮推荐内）"
            else:
                tail = "→ 选卡未记录"
            rows.append(f" · 第{i}轮 {opts} {tail}")

    # 出装采纳行：建议 N 次 · 采纳 M 次
    items = led.get("items") or {}
    if not items.get("suggested"):
        rows.append("出装采纳: -（无建议记录）")
    else:
        line = (f"出装采纳: 建议 {items['suggested']} 次 · "
                f"采纳 {items.get('adopted', 0)} 次")
        if not items.get("has_buys"):
            line += "（购买序列未记录）"
        rows.append(line)
    return rows
