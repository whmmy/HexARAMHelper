"""Live Client Data API runes 字段落库探针（诊断专用，不进推荐链）

背景（docs/真机验证清单.md §6）：竞品 ARAM-tool 实证 allgamedata 的
allPlayers[].runes.generalRunes[] 中 id>100000 的条目疑似已选海克斯；
本项目 live_api.py 从未解析过 runes 字段，"海克斯 API 读不到"的旧结论
待真机复核。本探针只做事实落库——这些字段到底存不存在、长什么样，
逐局追加写 log/runes_probe/runes_probe.jsonl，打完一局跑
experiments/summarize_runes_probe.py 出汇总。

红线：
- id>100000 只是探针启发式（落库记录用），绝不参与任何推荐判断，
  也不新增任何"哪些 id 是海克斯"类语义名单；
- 探针任何异常只 print WARN（限频）绝不外抛——轮询/快照/建议主链无感。
"""
import json
import threading
import time

import hexa.config as _config

AUG_ID_FLOOR = 100000   # 启发式阈值：仅探针落库标记用（§6 待真机复核）


def _raw_champ_tail(raw) -> str:
    """rawChampionName → 尾段："game_character_displayname_Leona"→"Leona"，
    裸名原样返回（与 live_api._norm_champ_raw 同口径但保留原大小写，照实落库）"""
    s = str(raw or "").strip()
    return s.rsplit("_", 1)[-1] if s else ""


def extract_players(players) -> list:
    """原始玩家列表 → 探针事实行（纯函数：无 IO、无判断、无语义表）。

    每个玩家输出 {champion, team, runes_field, total, augs}：
    - champion: championName，缺失退 rawChampionName 尾段，再缺失为 ""
    - runes_field: "present"（有 runes 容器键）/"absent"（无此键或非容器）
    - total: generalRunes 条目数（present 时照实计数，含解析失败的条目）
    - augs: 其中 id>100000 的 id 列表（启发式，仅落库）
    玩家元素不是 dict 就跳过；字段缺失一律容错照实记录（缺失本身即证据）。
    """
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        champ = str(p.get("championName") or "").strip()
        if not champ:
            champ = _raw_champ_tail(p.get("rawChampionName"))
        runes = p.get("runes")
        present = isinstance(runes, (dict, list))
        entries = []
        if isinstance(runes, dict):
            cand = runes.get("generalRunes")
            if isinstance(cand, list):
                entries = cand
        elif isinstance(runes, list):
            entries = runes
        augs = []
        for r in entries:
            rid = r.get("id") if isinstance(r, dict) else r
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            if rid > AUG_ID_FLOOR:
                augs.append(rid)
        out.append({
            "champion": champ,
            "team": str(p.get("team") or ""),
            "runes_field": "present" if present else "absent",
            "total": len(entries),
            "augs": augs,
        })
    return out


class RunesProbe:
    """逐局海克斯字段证据落库（JSONL 追加写；写盘异常限频 WARN 不外抛）"""

    LOG_NAME = "runes_probe.jsonl"

    def __init__(self, log_dir=None):
        # 运行时读 _config.BASE_DIR 属性（非 import 期绑定）——冒烟测试
        # 把 BASE_DIR 指到临时目录后，未显式传 log_dir 的构造也自动隔离
        self.log_dir = log_dir or (_config.BASE_DIR / "log" / "runes_probe")
        self._session = 0    # 局号：new_game() 时 +1（0 = 首局开局前的行）
        self._session_lock = threading.Lock()  # new_game 在状态机线程、
        # _write 在 LivePoller 线程——边界一行 JSONL 的局号不许错位
        self._warn_ts = 0.0  # 写盘异常限频（60s 一条，防日志刷屏）

    def new_game(self):
        """局号 +1（GameSession.on_in_progress → poller.enter_game 同点触发）"""
        with self._session_lock:
            self._session += 1

    def on_playerlist(self, raw_players, ts):
        """playerlist 原始 payload 逐帧落库（LivePoller._poll_once 喂）"""
        self._write("playerlist", extract_players(raw_players), ts)

    def on_allgamedata(self, payload, ts):
        """allgamedata 落库：收整包（取 allPlayers，再退 playerlist 键）
        或裸玩家列表均可——喂入口径不锁死，采集侧怎么方便怎么传；
        None（端点 404/应答无 allPlayers）也落一帧 players:[] 空证据行，
        "通道死"与"字段缺失"在落库里可分（20261014 双前缀修复案）"""
        players = payload
        if isinstance(payload, dict):
            players = payload.get("allPlayers")
            if players is None:
                players = payload.get("playerlist")
        self._write("allgamedata", extract_players(players), ts)

    def _write(self, src, players, ts):
        with self._session_lock:
            session = self._session
        row = {"ts": ts, "session": session, "src": src,
               "players": players}
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / self.LOG_NAME, "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 —— 探针绝不外抛，主链无感
            now = time.monotonic()
            if now - self._warn_ts >= 60.0:
                self._warn_ts = now
                print(f"[WARN] runes 探针落盘失败（60s 限频一条）: {e}")
