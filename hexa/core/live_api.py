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
    raw_events: dict = field(default_factory=dict)
    # 本局累计：击杀我的敌方英雄（显示名）→ 次数。来源 /eventdata
    # ChampionKill 事件（KillerName×VictimName），EventID 单调去重；
    # GameSession 挂到 threat.killer_counts 供 resist_advice 破平局
    killers_of_me: dict = field(default_factory=dict)

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
                 summoner_name_fn=None, runes_probe=None):
        super().__init__(daemon=True, name="LivePoller")
        self.kb = kb
        self.on_snapshot = on_snapshot
        self.dumper = fixture_dumper
        # 海克斯落库探针（诊断专用，20261013）：真机复核 §6"海克斯 API
        # 读不到"旧结论用，None = 未接探针（零开销零额外请求）
        self.runes_probe = runes_probe
        self._agd_tick = 0            # allgamedata 低频采集周期计数
        # 本地玩家定位（国服 activeplayer 缺 championName/items 时的兜底）
        self.summoner_name_fn = summoner_name_fn
        self._my_summoner = ""        # LCU 召唤师名缓存（非空后不再请求）
        self._me_matched_via = ""     # 本局命中的匹配策略（日志诊断用）
        self._me_miss_logged = False
        # 死亡来源追踪（/eventdata）状态
        self._last_event_id = 0
        self._kill_counts = {}        # 击杀我的英雄显示名 → 次数（本局累计）
        self._name_to_champ = {}      # 归一化玩家名 → 英雄显示名（playerlist 逐帧合并）
        self._my_event_names = set()  # 我的名字候选（VictimName 归一化后比对）
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
        # 新一局死亡计数清零（eventdata 跨局从 EventID 1 重新计）
        self._last_event_id = 0
        self._kill_counts = {}
        self._name_to_champ = {}
        self._my_event_names = set()
        self._agd_tick = 0   # allgamedata 低频节奏随局重置

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
            # 顶层兜底（20261013）：_poll_once 里的 int()/float() 强转遇到
            # 异常字段会抛——异常冒出即杀死本线程，快照断流 300s 后被
            # GameSession 数据停摆兜底误判"局已结束"。单帧失败只跳过，
            # 下帧照常轮询（脏数据是暂时的，线程死了整局观察全瞎）。
            try:
                snap = self._poll_once()
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] Live 快照单帧异常（下帧重试）: "
                      f"{type(e).__name__}: {e}")
                snap = None
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

    @staticmethod
    def _name_set(src, keys=("summonerName", "riotId", "riotIdGameName")):
        """名字候选集：全名 + 去 #tag 裸名（LCU 召唤师名常无 tag，Live API 带 tag）"""
        out = set()
        for k in keys:
            v = str((src or {}).get(k) or "").strip().lower()
            if v:
                out.add(v)
                out.add(v.split("#", 1)[0])
        out.discard("")
        return out

    def _match_me(self, active, playerlist):
        """在 playerlist 中定位本地玩家，返回 (条目, 命中策略)。

        国服 activeplayer 实测 championName=None、items=[] 且无
        rawChampionName，前两级匹配必失败；名字兜底用 activeplayer
        自带 summonerName/riotId（与 playerlist 同格式，免 LCU 即可
        锚定），再补 LCU 召唤师名，比对均含去 #tag 裸名。
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
        my_names = self._name_set(active) | self._name_set(
            {"summonerName": self._my_summoner})
        for p in playerlist:
            p_champ = str(p.get("championName") or "").strip().lower().replace(" ", "")
            if my_champ and p_champ == my_champ:
                return p, "championName"
            if my_raw and self._norm_champ_raw(p.get("rawChampionName")) == my_raw:
                return p, "rawChampionName"
            if my_names & self._name_set(p):
                return p, "summoner"
        return None, ""

    # ── 死亡来源（/eventdata ChampionKill） ──────────────────────

    @staticmethod
    def _norm_event_name(raw) -> str:
        """事件玩家名归一化：去空白/小写/去 #tag（与 _name_set 同口径）"""
        s = str(raw or "").strip().lower()
        return s.split("#", 1)[0] if s else ""

    def _process_kill_events(self, payload: dict, my_disp: str = ""):
        """eventdata 增量 → 谁在杀我（英雄显示名 → 次数，本局累计）。

        EventID 单调去重；整表最大 ID 回退（新一局 eventdata 重新从
        1 计）时清零重计，防跨局累加。击杀者只认 playerlist 映射出
        的英雄——Turret/Minion/Monster 等不在映射内自然忽略。"""
        events = (payload or {}).get("events") or []
        eids = []
        for e in events:
            try:
                i = int(e.get("EventID") or 0)
            except (TypeError, ValueError):
                continue
            if i > 0:
                eids.append(i)
        if eids and max(eids) < self._last_event_id:
            # 新一局的 eventdata 从头计数 → 清零重计
            self._kill_counts = {}
            self._last_event_id = 0
        top = self._last_event_id
        for ev in events:
            try:
                eid = int(ev.get("EventID") or 0)
            except (TypeError, ValueError):
                continue
            if eid <= self._last_event_id:
                continue
            top = max(top, eid)
            if str(ev.get("EventName") or "").strip().lower() != "championkill":
                continue
            if self._norm_event_name(
                    ev.get("VictimName")) not in self._my_event_names:
                continue
            champ = self._name_to_champ.get(
                self._norm_event_name(ev.get("KillerName")))
            if not champ or champ == my_disp:
                continue  # 未知来源/自杀不计
            self._kill_counts[champ] = self._kill_counts.get(champ, 0) + 1
        self._last_event_id = top

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
        # 海克斯落库探针（20261013，docs/真机验证清单.md §6 复核）：竞品
        # 实证 allgamedata.allPlayers[].runes.generalRunes[] 里 id>100000
        # 疑似已选海克斯；playerlist 是否同带该字段未验证——两条路都落库
        # 对照。探针炸只 print 不外抛：单帧失败跳过即可，绝不能拖垮快照
        # 主链（同 run() 顶层兜底同理）。未接探针（None）零开销零请求。
        if self.runes_probe is not None:
            if playerlist:
                try:
                    self.runes_probe.on_playerlist(playerlist, snap.ts)
                except Exception as e:  # noqa: BLE001
                    print(f"[WARN] runes 探针 playerlist 落库异常: {e}")
            # 低频 allgamedata 采集：每 6 个轮询周期（约 30s）请求一次，
            # 且仅在探针在位时才发——不给未开探针的用户加流量
            self._agd_tick += 1
            if self._agd_tick >= 6:
                self._agd_tick = 0
                try:
                    agd = self._get_json("/liveclientdata/allgamedata")
                    allp = (agd or {}).get("allPlayers")
                    if allp is not None:
                        self.runes_probe.on_allgamedata(allp, snap.ts)
                except Exception as e:  # noqa: BLE001
                    print(f"[WARN] runes 探针 allgamedata 采集异常: {e}")
        # 死亡来源（游戏内才可用；未在对局/旧版本客户端返回 None → 空表跳过）
        events = self._get_json("/eventdata")

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
            raw = str(p.get("championName") or "").strip()
            if raw and not raw.isascii():
                # 国服 championName 直接是中文（如 "不祥之刃"）：
                # 反查英文 slug 存 champion_en，中文名存 champion
                en = self.kb.cn_to_en.get(raw, "")
                cn = raw
            else:
                en = raw
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
                items=[{"name": str(it.get("displayName") or it.get("name") or ""),
                        "count": it.get("count", 1)}
                       for it in (p.get("items") or [])],
                riot_id=str(p.get("riotId", "")),
            )
            champ_disp = cn or en
            if champ_disp:
                # 事件 KillerName/VictimName → 英雄显示名映射（逐帧合并）
                for nm in self._name_set(p):
                    self._name_to_champ[nm] = champ_disp
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

        # 我的名字候选（VictimName 归一化后比对；me 定位失败时仍可用
        # activeplayer / LCU 召唤师名兜底）
        self._my_event_names = (self._name_set(me or {})
                                | self._name_set(active or {})
                                | self._name_set(
                                    {"summonerName": self._my_summoner}))
        # 死亡来源增量处理（EventID 去重；击杀者→英雄映射本帧已就绪）
        self._process_kill_events(events or {}, my_disp=snap.my_champion)
        snap.killers_of_me = dict(self._kill_counts)

        # dump（开关在 dumper 内部判断）
        if self.dumper:
            self.dumper.dump("live_activeplayer", snap.raw_active)
            self.dumper.dump("live_playerlist", snap.raw_playerlist)
            if snap.raw_events:
                self.dumper.dump("live_eventdata", snap.raw_events)
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
