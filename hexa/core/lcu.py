"""LCU 连接器 + 游戏阶段状态机

fork 自参考项目 scripts/lcu_connector.py，扩展：
- get_both_teams(): 双方 10 人阵容（ChampSelect session / gameflow gameData）
- PhaseMachine: 后台线程轮询 gameflow-phase，驱动
  ChampSelect → GameStart → InProgress → EndOfGame 生命周期事件（含秒退重开重置、LCU 断连容忍）
"""
import threading
import time

import psutil
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 常见安装路径（lockfile 兜底，fork 自参考项目）
COMMON_INSTALL_PATHS = [
    r"C:\Riot Games\League of Legends",
    r"D:\Riot Games\League of Legends",
    r"E:\Riot Games\League of Legends",
    r"C:\Program Files\Riot Games\League of Legends",
    r"D:\Program Files\Riot Games\League of Legends",
    r"C:\Riot Games\英雄联盟",
    r"D:\Riot Games\英雄联盟",
    r"D:\WeGameApps\英雄联盟",
    r"D:\WeGame\英雄联盟",
    r"E:\WeGame\英雄联盟",
    r"D:\腾讯游戏\英雄联盟",
    r"E:\腾讯游戏\英雄联盟",
]

# gameflow-phase 归并后的逻辑阶段
# 真机 gameflow 序列：InProgress → PreEndOfGame → EndOfGame → None——
# PreEndOfGame 不算收尾时 InProgress→IDLE 永不匹配，on_end_of_game 整局
# 不触发（会话卡"对局中"、复盘日志不落盘）
IDLE_PHASES = {"None", "Lobby", "Matchmaking", "ReadyCheck",
               "WaitingForStats", "PreEndOfGame", "EndOfGame"}


class LCUConnector:
    """英雄联盟客户端 LCU API 连接器（REST + riot Basic Auth）"""

    LCU_TIMEOUT = 3

    def __init__(self, kb):
        self.kb = kb  # hexa.knowledge.kb.KnowledgeBase
        self.port = None
        self.auth_token = None
        self.base_url = None
        self._connected = False
        self._summoner_id = None
        self._summoner_name = ""
        self.id_to_cn = {}
        self._sess = requests.Session()  # 复用连接，省去每请求 TLS 握手
        # 互斥（N8 工程债 20261020，竞品报告 §3.7 hex-helper lcu-service.js
        # 同思路）：PhaseMachine 线程（phase/teams/eog）与 LivePoller 线程
        # （summoner_name 兜底重试 → _cache_summoner_id）并发打同一个
        # Session——HTTP 请求与重连探测全部串行化。用 RLock：connect()
        # 持锁期间要走 _request 建英雄映射/缓存召唤师（同线程重入不自锁）；
        # get_eog_stats 的重试 sleep 在锁外，不会长时间占锁饿死别的线程
        self._io_lock = threading.RLock()

    # ── 连接 ──────────────────────────────────────────────────────

    def connect(self) -> bool:
        with self._io_lock:   # 互斥：进程扫描/lockfile 探测不许双线程并发
            if self._connect_via_process() or self._connect_via_lockfile():
                self._connected = True
                self._build_champion_id_map()
                self._cache_summoner_id()
                print(f"[OK] LCU connected (port: {self.port})")
                return True
            self._connected = False
            return False

    def _connect_via_process(self):
        """两段式扫描（性能关键）：先只取 pid+name（psutil 缓存，便宜），
        命中 leagueclientux.exe 才查 cmdline（昂贵系统调用）——
        全量读 cmdline 每次连接尝试都要几百 ms 且高频尖峰。"""
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    name = (proc.info.get('name') or '').lower()
                    if name != 'leagueclientux.exe':
                        continue
                    cmdline = proc.cmdline() or []
                    port = token = None
                    for arg in cmdline:
                        if '--app-port=' in arg:
                            port = arg.split('=', 1)[1]
                        elif '--remoting-auth-token=' in arg:
                            token = arg.split('=', 1)[1]
                    if port and token:
                        self.port, self.auth_token = port, token
                        self.base_url = f"https://127.0.0.1:{port}"
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
        return False

    def _connect_via_lockfile(self):
        import os
        for base in COMMON_INSTALL_PATHS:
            lockfile = os.path.join(base, 'lockfile')
            if os.path.exists(lockfile):
                try:
                    with open(lockfile) as f:
                        parts = f.read().strip().split(':')
                    if len(parts) >= 5:
                        self.port, self.auth_token = parts[2], parts[3]
                        self.base_url = f"https://127.0.0.1:{self.port}"
                        return True
                except Exception:
                    continue
        return False

    def is_connected(self):
        return self._connected

    def _request(self, method, endpoint, **kwargs):
        if not self.base_url or not self.auth_token:
            return None
        try:
            with self._io_lock:   # 互斥：跨线程共享 Session 串行化
                return self._sess.request(
                    method, f"{self.base_url}{endpoint}",
                    auth=('riot', self.auth_token),
                    verify=False, timeout=self.LCU_TIMEOUT, **kwargs)
        except requests.exceptions.ConnectionError:
            self._connected = False
            return None
        except Exception:
            return None

    # ── 基础信息 ──────────────────────────────────────────────────

    def _build_champion_id_map(self):
        resp = self._request('GET', '/lol-game-data/assets/v1/champion-summary.json')
        if not resp or resp.status_code != 200:
            return
        try:
            for champ in resp.json():
                cid, alias = champ.get('id'), champ.get('alias', '')
                if cid is None or cid == -1:
                    continue
                cn = self.kb.en_to_cn.get(alias.lower())
                if cn:
                    self.id_to_cn[cid] = cn
            print(f"[OK] LCU ID map: {len(self.id_to_cn)} champions")
        except Exception:
            pass

    def _cache_summoner_id(self):
        resp = self._request('GET', '/lol-summoner/v1/current-summoner')
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                self._summoner_id = data.get('summonerId')
                # 国服 Live API playerlist 匹配锚点（riotId/displayName 兜底）
                self._summoner_name = str(data.get('gameName')
                                          or data.get('displayName')
                                          or data.get('summonerName') or "")
            except Exception:
                pass

    def summoner_name(self) -> str:
        """当前召唤师名（缓存；未连接返回空——LivePoller 每 5s 重试至拿到）"""
        if not self._summoner_name and self._connected:
            self._cache_summoner_id()
        return self._summoner_name

    # ── 终局统计（N4 复盘结果维度 20261019：胜负 + 我的 KDA） ─────

    @staticmethod
    def _eog_stat(stats: dict, *names):
        """eog stats 值防御式读取：直接键（如 "WIN"）与 statistics 数组
        （[{"name":..., "value":...}]，旧版风格）两种 schema 都认。"""
        if not isinstance(stats, dict):
            return None
        for n in names:
            if n in stats:
                return stats[n]
        arr = stats.get("statistics")
        if isinstance(arr, list):
            for row in arr:
                if isinstance(row, dict) and row.get("name") in names:
                    return row.get("value")
        return None

    def _parse_eog_player(self, p: dict):
        """eog-stats-block 我方条目 → {"result","kills","deaths","assists"}。

        schema 按公开文档编写并全程容错（同 live_api P0 声明：以真机
        fixtures dump 实测为准）：WIN=true/false 或 statistics WIN=1 判
        胜负，拿不到的键如实缺省（None 不编造 0）。"""
        stats = p.get("stats")
        win = self._eog_stat(stats, "WIN", "win")
        if win is None:
            result = ""
        elif win is True or win == 1 or str(win).lower() in ("true", "1"):
            result = "win"
        else:
            result = "loss"
        def _int(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        return {
            "result": result,
            "kills": _int(self._eog_stat(stats, "KILLS", "CHAMPIONS_KILLED",
                                         "kills")),
            "deaths": _int(self._eog_stat(stats, "DEATHS", "NUM_DEATHS",
                                          "deaths")),
            "assists": _int(self._eog_stat(stats, "ASSISTS", "assists")),
        }

    def get_eog_stats(self, attempts: int = 20, interval: float = 1.5):
        """终局统计块 → 我方条目 dict；拿不到返回 None（复盘如实缺省）。

        端点 /lol-end-of-game/v1/eog-stats-block 仅收尾相位可用，且就绪
        不早于 PreEndOfGame（InProgress 刚离开时常 404）→ 短间隔重试，
        默认窗口 ≈20×(1.5s 间隔+最坏 3s 超时)≈90s：收尾相位推进节奏无
        保证，调用方是局终后台回填线程（GameSession._attach_eog_result，
        daemon）等得起——窗口远短于下一局可能的终局时点，不会取到别局
        数据；LCU 未连接/请求失败/statistics 缺失一律 None，绝不抛。重试
        sleep 在 LCU io 锁外，不占锁饿死 phase/快照请求。"""
        if not self.base_url or not self.auth_token:
            return None
        for i in range(max(1, attempts)):
            data = self._get_json('/lol-end-of-game/v1/eog-stats-block')
            players = (data or {}).get("allPlayers")
            if isinstance(players, list):
                for p in players:
                    if not isinstance(p, dict):
                        continue
                    sid = p.get("summonerId")
                    if (self._summoner_id is not None
                            and sid == self._summoner_id) or (
                            self._summoner_name
                            and str(p.get("summonerName") or "")
                            == self._summoner_name):
                        out = self._parse_eog_player(p)
                        # 只有身份命中才算拿到（result/kda 全缺也如实
                        # 返回——JSON 侧按缺省处理）
                        return out
            if i < attempts - 1:
                time.sleep(interval)
        return None

    # ── 阶段与英雄（fork 参考项目，接口保持一致） ──────────────────

    def get_gameflow_phase(self):
        if not self._connected:
            return None
        resp = self._request('GET', '/lol-gameflow/v1/gameflow-phase')
        if resp and resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                pass
        return None

    def get_champ_select_champion(self):
        resp = self._request('GET', '/lol-champ-select/v1/session')
        if not resp or resp.status_code != 200:
            return None
        try:
            data = resp.json()
            local_cell = data.get('localPlayerCellId')
            if local_cell is None:
                return None
            for player in data.get('myTeam', []):
                if player.get('cellId') == local_cell:
                    cid = player.get('championId', 0)
                    if cid and cid > 0:
                        return self.id_to_cn.get(cid)
        except Exception:
            pass
        return None

    def get_gameflow_champion(self):
        resp = self._request('GET', '/lol-gameflow/v1/session')
        if not resp or resp.status_code != 200:
            return None
        try:
            data = resp.json().get('gameData', {})
            all_players = data.get('teamOne', []) + data.get('teamTwo', [])
            for player in all_players:
                if player.get('summonerId') == self._summoner_id:
                    cid = player.get('championId', 0)
                    if cid and cid > 0:
                        return self.id_to_cn.get(cid)
            for sel in data.get('playerChampionSelections', []):
                if sel.get('summonerId') == self._summoner_id:
                    cid = sel.get('championId', 0)
                    if cid and cid > 0:
                        return self.id_to_cn.get(cid)
        except Exception:
            pass
        return None

    def get_ingame_champion(self):
        """Live Client Data API 兜底（:2999 免密，仅游戏内可用）"""
        try:
            resp = requests.get('https://127.0.0.1:2999/liveclientdata/activeplayer',
                                verify=False, timeout=2)
            if resp.status_code == 200:
                return self.kb.champion_cn(resp.json().get('championName', ''))
        except Exception:
            pass
        return None

    def get_champion_auto(self):
        """全生命周期自动获取当前英雄，返回 (中文名|None, 来源)"""
        if not self._connected:
            if self.connect():
                pass
            else:
                hero = self.get_ingame_champion()
                return (hero, "Live API") if hero else (None, "")
        phase = self.get_gameflow_phase()
        if phase == "ChampSelect":
            hero = self.get_champ_select_champion()
            if hero:
                return hero, "ChampSelect"
        if phase in ("InProgress", "GameStart"):
            hero = self.get_gameflow_champion()
            if hero:
                return hero, "GameFlow"
            hero = self.get_ingame_champion()
            if hero:
                return hero, "Live API"
        if phase not in IDLE_PHASES:
            hero = self.get_champ_select_champion()
            if hero:
                return hero, "ChampSelect"
        return None, phase or ""

    # ── 扩展：双方阵容 ────────────────────────────────────────────

    @staticmethod
    def _session_map_id(data) -> str:
        """gameflow session → 地图 id 字符串（"11"=峡谷 / "12"=大乱斗；
        缺字段返回 ""，调用方按未知地图宽松兜底）"""
        game = (data or {}).get('gameData') or {}
        mid = ((data or {}).get('map') or {}).get('id') \
            or (game.get('queue') or {}).get('mapId')
        return str(mid) if mid else ""

    def get_both_teams(self):
        """双方 10 人阵容（中文英雄名）。

        返回 {"ally": [...], "enemy": [...], "my_hero": str|None,
        "map_id": "11"/"12"/"", "source": str}
        ChampSelect 阶段：myTeam + enemyTeam（大乱斗双方同时选，敌方缺失时为空，
        由 GameStart 阶段的 gameflow 数据补全）。
        """
        phase = self.get_gameflow_phase()
        if phase == "ChampSelect":
            data = self._get_json('/lol-champ-select/v1/session') or {}
            # 选人阶段 gameflow session 一般已带地图信息（拿不到留空兜底）
            map_id = self._session_map_id(
                self._get_json('/lol-gameflow/v1/session'))
            local_cell = data.get('localPlayerCellId')
            ally, enemy, my_hero = [], [], None
            for player in data.get('myTeam', []):
                cid = player.get('championId', 0)
                if not cid or cid <= 0:
                    continue
                cn = self.id_to_cn.get(cid)
                if cn:
                    ally.append(cn)
                    if player.get('cellId') == local_cell:
                        my_hero = cn
            for player in data.get('enemyTeam', []):
                cid = player.get('championId', 0)
                if not cid or cid <= 0:
                    continue
                cn = self.id_to_cn.get(cid)
                if cn:
                    enemy.append(cn)
            return {"ally": ally, "enemy": enemy, "my_hero": my_hero,
                    "map_id": map_id, "source": "ChampSelect"}

        if phase in ("InProgress", "GameStart"):
            data = self._get_json('/lol-gameflow/v1/session') or {}
            game = data.get('gameData', {})
            team_one = game.get('teamOne', [])
            team_two = game.get('teamTwo', [])
            my_side = None
            for team_idx, team in enumerate((team_one, team_two)):
                for player in team:
                    if player.get('summonerId') == self._summoner_id:
                        my_side = team_idx
                        break
                if my_side is not None:
                    break
            if my_side is None:
                # ARAM 模式 gameflow 偶发按观察者视角排列，保守假定 teamOne 为己方
                my_side = 0
            def names(team):
                out = []
                for p in team:
                    cn = self.id_to_cn.get(p.get('championId', 0) or -1)
                    if cn:
                        out.append(cn)
                return out
            ally = names(team_one if my_side == 0 else team_two)
            enemy = names(team_two if my_side == 0 else team_one)
            my_hero = self.get_gameflow_champion() or (
                self.kb.champion_cn(
                    (team_one if my_side == 0 else team_two)[0].get('championId')
                ) if ally else None)
            return {"ally": ally, "enemy": enemy, "my_hero": my_hero,
                    "map_id": self._session_map_id(data),
                    "source": "GameFlow"}

        return {"ally": [], "enemy": [], "my_hero": None, "map_id": "",
                "source": phase or ""}

    def _get_json(self, endpoint):
        resp = self._request('GET', endpoint)
        if resp and resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        return None


class PhaseMachine(threading.Thread):
    """gameflow-phase 轮询状态机（全自动生命周期驱动，无热键）

    事件回调（在状态机线程执行，UI 层需自行经 queue 转发）：
      on_champ_select(teams)   进入选人（teams = get_both_teams() 快照）
      on_game_start(teams)     进入 GameStart（阵容锁定，System2 ChampionContext 时机）
      on_in_progress()         加载完成进入对局（启动 live_api 轮询）
      on_end_of_game(reason)   对局结束或秒退重开（GameSession 重置）
      on_disconnect() / on_reconnect()   LCU 断连/恢复
      on_phase(phase)          原始阶段变化（调试/UI 显示）

    秒退/掉线回大厅同样触发 on_end_of_game —— 会话重置逻辑统一在回调侧。
    """

    POLL_INTERVAL = 2.0
    DISCONNECT_NEEDED = 3  # 连续 N 次获取不到 phase 视为断连
    # 失败退避梯子（秒）：「进程不在」的重连探测与「连着但取不到 phase」
    # 的挂起态轮询共用（见 _loop_interval，N8 20261020）——避免每 2s 全量
    # 进程扫描 / 3s 超时请求的高频尖峰
    CONNECT_BACKOFF = [2, 4, 8, 15, 30, 60]

    def __init__(self, lcu: LCUConnector, callbacks: dict = None):
        super().__init__(daemon=True, name="PhaseMachine")
        self.lcu = lcu
        self.cb = callbacks or {}
        self._running = True
        self._phase = None           # 上次逻辑阶段
        self._fail_count = 0
        self._disconnected = False
        self._backoff_idx = 0

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            phase = self._poll_phase()
            self._handle(phase)
            self._check_stall()
            time.sleep(self._loop_interval())

    def _loop_interval(self) -> float:
        """本轮休眠：健康连接 = 常规轮询间隔；连不上（进程不在）或连着
        但连续取不到 phase（客户端挂起/重启窗口期）= 指数退避梯子。

        N8 工程债 20261020：此前只有「进程不在」路径退避，「连着但取不
        到 phase」仍每 2s+3s 超时硬敲半死客户端——现两条失败路径共用
        同一条梯子（竞品报告 §3.7 hex-helper lcu-service.js 指数退避同
        思路）。代价注记：梯子深处 _check_stall 间隔同步变长——「UX 已
        关」场景现状本就退避，挂起场景（罕见）等价放宽，可接受。"""
        if self._backoff_idx:
            return self.CONNECT_BACKOFF[self._backoff_idx]
        return self.POLL_INTERVAL

    def _check_stall(self):
        """数据流停摆兜底：phase 查询成败都查（终局收尾不依赖 LCU，
        见 GameSession.report_data_stall——20260929 人马案）。"""
        check_stall = self.cb.get("check_stall")
        if check_stall and check_stall():
            self._fire("on_end_of_game", "data_stall")
            self._phase = "None"   # 防后续 phase 恢复时按旧转换重触发

    def _poll_phase(self):
        if not self.lcu.is_connected():
            if not self.lcu.connect():
                self._fail()
                self._backoff_idx = min(self._backoff_idx + 1,
                                        len(self.CONNECT_BACKOFF) - 1)
                return None
            self._backoff_idx = 0
        phase = self.lcu.get_gameflow_phase()
        if phase is None:
            self._fail()
            # 连着但取不到 phase 也爬梯（防挂起态高频硬敲，见 _loop_interval）
            self._backoff_idx = min(self._backoff_idx + 1,
                                    len(self.CONNECT_BACKOFF) - 1)
        else:
            self._fail_count = 0
            self._backoff_idx = 0
            if self._disconnected:
                self._disconnected = False
                self._fire("on_reconnect")
        return phase

    def _fail(self):
        self._fail_count += 1
        if (self._fail_count >= self.DISCONNECT_NEEDED
                and not self._disconnected
                and self._phase in ("ChampSelect", "GameStart", "InProgress")):
            self._disconnected = True
            self._fire("on_disconnect")

    def _handle(self, phase):
        if phase is None:
            # 瞬时失败（接口抖动/超时）：保留上一阶段，防止把 InProgress
            # 覆盖成 None 后永久错过 InProgress→IDLE 的局终转换（丢复盘日志）
            return
        prev, cur = self._phase, phase
        if cur == "ChampSelect":
            # 选人阶段持续刷新快照（骰子换人自动跟随），回调侧按 my_hero 去重
            self._phase = cur
            if prev != cur:
                print(f"[LCU] phase: {prev} -> {cur}")
                self._fire("on_phase", cur)
                if prev == "InProgress":
                    self._fire("on_end_of_game", "dodge_requeue")
            self._fire("on_champ_select", self._safe_teams())
            return
        if cur == prev:
            return
        self._phase = cur
        print(f"[LCU] phase: {prev} -> {cur}")
        self._fire("on_phase", cur)

        if cur == "GameStart":
            self._fire("on_game_start", self._safe_teams())
        elif cur == "InProgress":
            if prev in (None, "ChampSelect"):
                # GameStart 阶段一闪而过（2s 轮询粒度抓不住）或国服直接跳过：
                # 选人直进对局/启动即已在对局时补发开局。20260929 曙光女神案
                # ——on_game_start 永不触发，System2 整局空转（ctx_source="-"
                # 、calls=0、¥0.0000），System1 预算守门全被 _ctx_ready 挡死。
                self._fire("on_game_start", self._safe_teams())
            self._fire("on_in_progress")
        elif prev == "InProgress" and cur in IDLE_PHASES:
            self._fire("on_end_of_game", cur)

    def _safe_teams(self):
        try:
            return self.lcu.get_both_teams()
        except Exception as e:
            print(f"[WARN] get_both_teams 异常: {e}")
            return {"ally": [], "enemy": [], "my_hero": None, "source": "error"}

    def _fire(self, event, *args):
        handler = self.cb.get(event)
        if handler:
            try:
                handler(*args)
            except Exception as e:
                print(f"[WARN] 回调 {event} 异常: {e}")
