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
并列呈现，偏差判断留给玩家；全部字段原值直出，无预消化结论。落盘格式
一字不改（旧文件直接可读；向后兼容新字段＝多出的键读不到就占位）。
"""
import json
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
        aug_parts.append(f"{_as_str(a.get('tier'))} {name}".strip())
    ledger = ("；".join(f"{i}. {p}" for i, p in enumerate(aug_parts, 1))
              if aug_parts else "-")

    sug = [s for s in _as_list(doc.get("suggestions"))
           if isinstance(s, dict)]
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
