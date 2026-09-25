"""Live Client Data API（127.0.0.1:2999，免密）轮询器

P0 说明：字段 schema 按公开文档编写并全程容错——playerlist 是否含
items[]/全量敌方数据属设计假设，以真机 fixtures dump 实测为准（计划 §8 P0）。
"""
import threading
import time
from dataclasses import dataclass, field

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://127.0.0.1:2999/liveclientdata"
TIMEOUT = 2
_SESSION = requests.Session()  # 复用连接，省去每请求 TLS 握手


@dataclass
class PlayerInfo:
    champion: str = ""          # 中文英雄名（经 KB 转换）
    champion_en: str = ""       # 原始英文名
    team: str = ""              # "ORDER"(蓝) / "CHAOS"(红) / ""
    is_dead: bool = False
    respawn_timer: float = 0.0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    items: list = field(default_factory=list)  # [{"name": ..., "count": ...}]
    riot_id: str = ""


@dataclass
class LiveSnapshot:
    ts: float = 0.0
    ok: bool = False
    # 我的信息（activeplayer + playerlist 中我自己）
    my_champion: str = ""
    my_level: int = 0
    my_current_gold: float = 0.0
    my_total_gold: float = 0.0
    my_is_dead: bool = False
    my_respawn_timer: float = 0.0
    my_items: list = field(default_factory=list)
    # 全场 10 人
    players: list = field(default_factory=list)  # [PlayerInfo]
    # 原始响应（fixtures dump 用）
    raw_active: dict = field(default_factory=dict)
    raw_playerlist: list = field(default_factory=list)

    def enemies(self):
        if not self.players or not self.my_team:
            return []
        return [p for p in self.players if p.team and p.team != self.my_team]

    def allies(self):
        if not self.players or not self.my_team:
            return []
        return [p for p in self.players if p.team == self.my_team]

    # 我所在队伍（由 my_champion 匹配 playerlist 推得）
    @property
    def my_team(self) -> str:
        for p in self.players:
            if p.champion == self.my_champion or (
                    p.champion_en and p.champion_en == self.my_champion):
                return p.team
        return ""


class LivePoller(threading.Thread):
    """游戏内 5s 轮询；每次快照经 on_snapshot 回调（轮询线程内执行）"""

    POLL_INTERVAL = 5.0

    def __init__(self, kb, on_snapshot=None, fixture_dumper=None):
        super().__init__(daemon=True, name="LivePoller")
        self.kb = kb
        self.on_snapshot = on_snapshot
        self.dumper = fixture_dumper
        self._running = False
        self._in_game = threading.Event()
        self.last_snapshot: LiveSnapshot | None = None

    # ── 生命周期（由 PhaseMachine 事件驱动） ───────────────────────

    def enter_game(self):
        self._in_game.set()

    def leave_game(self):
        self._in_game.clear()
        self.last_snapshot = None

    def stop(self):
        self._running = False
        self._in_game.set()  # 唤醒线程退出

    def run(self):
        self._running = True
        while self._running:
            if not self._in_game.wait(timeout=1.0):
                continue
            if not self._running:
                break
            snap = self._poll_once()
            if snap is not None:
                self.last_snapshot = snap
                if self.on_snapshot:
                    try:
                        self.on_snapshot(snap)
                    except Exception as e:
                        print(f"[WARN] on_snapshot 回调异常: {e}")
            time.sleep(self.POLL_INTERVAL)

    # ── 单次快照 ──────────────────────────────────────────────────

    def _poll_once(self) -> LiveSnapshot | None:
        snap = LiveSnapshot(ts=time.time())
        active = self._get_json("/activeplayer")
        playerlist = self._get_json("/playerlist")
        if active is None and playerlist is None:
            return None
        snap.ok = True
        snap.raw_active = active or {}
        snap.raw_playerlist = playerlist or []

        # activeplayer（免密；只在游戏内可用）
        snap.my_champion_en = active.get("championName", "") if active else ""
        snap.my_level = int(active.get("level") or 0) if active else 0
        # gold 字段名按实测为准（currentGold / goldTotal 均做容错）
        snap.my_current_gold = float(active.get("currentGold") or 0) if active else 0.0
        snap.my_total_gold = float(active.get("goldTotal") or 0) if active else 0.0

        # playerlist → PlayerInfo
        my_en = snap.my_champion_en.lower().replace(" ", "")
        for p in (playerlist or []):
            en = str(p.get("championName", ""))
            cn = self.kb.champion_cn(en) or ""
            scores = p.get("scores") or {}
            info = PlayerInfo(
                champion=cn,
                champion_en=en,
                team=str(p.get("team", "")),
                is_dead=bool(p.get("isDead", False)),
                respawn_timer=float(p.get("respawnTimer") or 0),
                kills=int(scores.get("kills") or 0),
                deaths=int(scores.get("deaths") or 0),
                assists=int(scores.get("assists") or 0),
                items=[{"name": it.get("name", ""), "count": it.get("count", 1)}
                       for it in (p.get("items") or [])],
                riot_id=str(p.get("riotId", "")),
            )
            snap.players.append(info)
            if en.lower().replace(" ", "") == my_en:
                snap.my_champion = cn or en
                snap.my_is_dead = info.is_dead
                snap.my_respawn_timer = info.respawn_timer
                snap.my_items = info.items

        # dump（开关在 dumper 内部判断）
        if self.dumper:
            self.dumper.dump("live_activeplayer", snap.raw_active)
            self.dumper.dump("live_playerlist", snap.raw_playerlist)
        return snap

    @staticmethod
    def _get_json(endpoint):
        try:
            resp = _SESSION.get(f"{BASE}{endpoint}", verify=False, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            return None
        return None
