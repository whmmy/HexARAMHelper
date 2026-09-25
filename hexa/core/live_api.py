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

    def __init__(self, kb, on_snapshot=None, fixture_dumper=None,
                 summoner_name_fn=None):
        super().__init__(daemon=True, name="LivePoller")
        self.kb = kb
        self.on_snapshot = on_snapshot
        self.dumper = fixture_dumper
        # 本地玩家定位（国服 activeplayer 缺 championName/items 时的兜底）
        self.summoner_name_fn = summoner_name_fn
        self._my_summoner = ""        # LCU 召唤师名缓存（非空后不再请求）
        self._me_matched_via = ""     # 本局命中的匹配策略（日志诊断用）
        self._me_miss_logged = False
        self._running = False
        self._in_game = threading.Event()
        self.last_snapshot: LiveSnapshot | None = None

    # ── 生命周期（由 PhaseMachine 事件驱动） ───────────────────────

    def enter_game(self):
        self._in_game.set()

    def leave_game(self):
        self._in_game.clear()
        self.last_snapshot = None
        self._me_matched_via = ""
        self._me_miss_logged = False

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

    # ── 本地玩家定位（国服/国际服字段兜底） ────────────────────────

    @staticmethod
    def _norm_champ_raw(raw) -> str:
        """rawChampionName → 尾段小写英文名。
        兼容 "game_character_displayname_Leona" 与裸名 "Leona"；
        raw 字段是游戏内部键，国服也保持英文，可作稳定匹配锚点。"""
        s = str(raw or "").strip().lower()
        return s.rsplit("_", 1)[-1] if s else ""

    def _match_me(self, active, playerlist):
        """在 playerlist 中定位本地玩家，返回 (条目, 命中策略)。

        国服 activeplayer 实测 championName=None、items=[]，championName
        匹配必失败，按序兜底：championName（国际服）→ rawChampionName
        尾段 → LCU 召唤师名（summonerName / riotId）。
        """
        if not playerlist:
            return None, ""
        active = active or {}
        my_champ = str(active.get("championName") or "").strip().lower().replace(" ", "")
        my_raw = self._norm_champ_raw(active.get("rawChampionName"))
        if not self._my_summoner and self.summoner_name_fn:
            try:
                self._my_summoner = str(self.summoner_name_fn() or "").strip()
            except Exception:
                self._my_summoner = ""
        my_names = {self._my_summoner.lower()} - {""}
        for p in playerlist:
            p_champ = str(p.get("championName") or "").strip().lower().replace(" ", "")
            if my_champ and p_champ == my_champ:
                return p, "championName"
            if my_raw and self._norm_champ_raw(p.get("rawChampionName")) == my_raw:
                return p, "rawChampionName"
            p_names = {str(p.get(k) or "").strip().lower()
                       for k in ("summonerName", "riotId")} - {""}
            if my_names & p_names:
                return p, "summoner"
        return None, ""

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
        # 国服实测 championName=null：or "" 防旧写法 .get(k, "") 取到 None 崩线程
        snap.my_champion_en = str(active.get("championName") or "") if active else ""
        snap.my_level = int(active.get("level") or 0) if active else 0
        # gold 字段名按实测为准（currentGold / goldTotal 均做容错）
        snap.my_current_gold = float(active.get("currentGold") or 0) if active else 0.0
        snap.my_total_gold = float(active.get("goldTotal") or 0) if active else 0.0

        # playerlist → PlayerInfo；本地玩家按多策略锚定
        me, via = self._match_me(active, playerlist)
        for p in (playerlist or []):
            en = str(p.get("championName") or "")
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
            if p is me:
                snap.my_champion = cn or en
                snap.my_is_dead = info.is_dead
                snap.my_respawn_timer = info.respawn_timer
                snap.my_items = info.items
        if via and via != self._me_matched_via:
            self._me_matched_via = via
            print(f"[LIVE] 本地玩家定位成功（{via}）→ {snap.my_champion or '?'}")
        elif not me and not self._me_miss_logged:
            self._me_miss_logged = True
            print("[LIVE] WARN: playerlist 中定位不到本地玩家"
                  "（国服字段缺失且召唤师名不可用）——装备/金币追踪将缺失")

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
