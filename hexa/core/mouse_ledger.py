"""鼠标记账（计划 §3：界面活跃窗口内，点击落点 = 卡片矩形 → 记账本轮已选）

实现（v4，20261002 虚空之女案）：WH_MOUSE_LL 钩子 + 独立捕获子进程 +
局内 GetAsyncKeyState 轮询三通道。v3 双通道首局实证：选卡瞬间钩子与
轮询仍双双零捕获（轮询 50Hz 采样、LSB 位只要有按压必留痕，零留痕 =
采样线程在按压前后完全没跑到），而非选卡期点击双双正常、主进程明明
活着——指向主进程内部 GIL/调度饥饿（选卡期恰是检测器逐秒全帧 OCR
风暴）或系统层过滤。v4 加第三通道：click_watcher.py 独立子进程（独立
解释器独立 GIL、NORMAL 优先级），采样结果写会话文件、主进程 tail 进
统一管道——它既是修复候选也是鉴别仪器（它也瞎 = 系统层过滤的铁证）。

点击时刻（time.time() 墙钟）全链透传：去重按"点击时刻±50ms 同位"对
最近路由记录判定（与到达顺序无关，子进程 tail 的 ~0.25s 迟到不误判）；
路由回调 on_click/on_reroll 收 (x, y, ts)，调用方门控按点击时刻评估
（处理线程被拖 late 时不再误丢点击）。

旧版 20ms GetAsyncKeyState 轮询的缺陷（v2 重写根因）：海克斯界面活跃
期恰是检测器每秒全帧 OCR 最忙时，轮询线程被饿出 100ms+ 间隙。v2 换
钩子、v3 加局内轮询、v4 加子进程——每次都是同一病灶换位置复发，最终
以进程隔离兜底。

架构：钩子线程（安装钩子 + 消息循环；回调只投队列即返回）+ 子进程
tail 线程 + 局内轮询线程（子进程不可用时兜底）+ 处理线程（消费队列，
去重/路由/诊断/记账）。钩子安装失败回退纯轮询（无子进程）。

只读约束不变：CallNextHookEx 透传，不拦截、不注入、不改鼠标行为
（合规边界与 P0 结论一致）；是否消费由调用方按坐标 + 海克斯证据决定。
reroll 按钮矩形命中 → on_reroll 回调（触发主动重扫）。
"""
import ctypes
import ctypes.wintypes
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque

from hexa.config import BASE_DIR
from hexa.core.screen_detect import point_in_rect

WM_LBUTTONDOWN = 0x0201
WM_MOUSEMOVE = 0x0200
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_XBUTTONDOWN = 0x020B
WM_QUIT = 0x0012
WH_MOUSE_LL = 14
HC_ACTION = 0
LLMHF_INJECTED = 0x1   # MSLLHOOKSTRUCT.flags：事件为注入（驱动/软件合成）
_BUTTON_NAMES = {WM_LBUTTONDOWN: "LB", WM_RBUTTONDOWN: "RB",
                 WM_MBUTTONDOWN: "MB", WM_XBUTTONDOWN: "XB"}

# 2026-10-02 真机案：CallNextHookEx 无原型时 ctypes 把 l_param 按 32 位
# c_int 转换，而 64 位下它是 MSLLHOOKSTRUCT 堆指针（普遍 > 2^31）——每个
# 鼠标事件 OverflowError，被回调机制吞成"Exception ignored"刷屏。钩子
# 全家桶显式定型（LRESULT = LONG_PTR，与回调签名同参）。
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(
    LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)


class _MSLLHOOKSTRUCT(ctypes.Structure):
    """低级鼠标钩子事件体（pt = 屏幕物理坐标，与 GetCursorPos 同基准）"""
    _fields_ = [("pt", ctypes.wintypes.POINT),
                ("mouseData", ctypes.wintypes.DWORD),
                ("flags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


# ── 完整性级别 / 前台窗口探针（v3 诊断用，只读，尽力而为不抛） ─────
# 20261002 寒冰案：LL 钩子游戏窗口焦点期零事件而客户端窗口正常——
# 需要在日志里留下"前台窗口是谁、什么完整性级别"的现场证据。
# 64 位下凡返回指针的 API 必须显式 restype（默认 c_int 截断 = 段错误，
# 与 CallNextHookEx 溢出同类病）；私有实例不污染全进程 argtypes。

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_a32 = ctypes.WinDLL("advapi32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                             ctypes.wintypes.DWORD]
_k32.CloseHandle.restype = ctypes.wintypes.BOOL
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.LPWSTR,
    ctypes.POINTER(ctypes.wintypes.DWORD)]
_k32.GetCurrentProcessId.restype = ctypes.wintypes.DWORD
_k32.GetCurrentProcessId.argtypes = []
_u32.GetForegroundWindow.restype = ctypes.c_void_p
_u32.GetForegroundWindow.argtypes = []
_u32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
_u32.GetWindowThreadProcessId.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD)]
_a32.OpenProcessToken.restype = ctypes.wintypes.BOOL
_a32.OpenProcessToken.argtypes = [
    ctypes.c_void_p, ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.wintypes.HANDLE)]
_a32.GetTokenInformation.restype = ctypes.wintypes.BOOL
_a32.GetTokenInformation.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
    ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD)]
_a32.GetSidSubAuthorityCount.restype = ctypes.c_void_p
_a32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_a32.GetSidSubAuthority.restype = ctypes.c_void_p
_a32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]

_PROCESS_QUERY_LIMITED = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25
_IL_NAMES = {0x0: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
             0x2100: "MediumPlus", 0x3000: "High", 0x4000: "System"}


def process_il(pid: int) -> str:
    """pid → 'Medium(0x2000)'；查询失败 → '未知(err=N)'。只读。"""
    try:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
        if not h:
            return f"未知(err={ctypes.get_last_error()})"
        try:
            tok = ctypes.wintypes.HANDLE()
            if not _a32.OpenProcessToken(h, _TOKEN_QUERY, ctypes.byref(tok)):
                return f"未知(err={ctypes.get_last_error()})"
            try:
                need = ctypes.wintypes.DWORD(0)
                _a32.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, None,
                                         0, ctypes.byref(need))
                buf = ctypes.create_string_buffer(need.value or 64)
                if not _a32.GetTokenInformation(
                        tok, _TOKEN_INTEGRITY_LEVEL, buf, need.value,
                        ctypes.byref(need)):
                    return "未知"
                sid = ctypes.cast(
                    buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
                cnt_p = _a32.GetSidSubAuthorityCount(sid)
                cnt = ctypes.cast(
                    cnt_p, ctypes.POINTER(ctypes.c_ubyte)).contents.value
                rid_p = _a32.GetSidSubAuthority(sid, cnt - 1)
                rid = ctypes.cast(
                    rid_p, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value
                return f"{_IL_NAMES.get(rid, '?')}(0x{rid:x})"
            finally:
                _k32.CloseHandle(tok)
        finally:
            _k32.CloseHandle(h)
    except Exception:  # noqa: BLE001 —— 探针失败只降级为"未知"
        return "未知"


def self_il() -> str:
    return process_il(_k32.GetCurrentProcessId())


_TOKEN_ELEVATION = 20   # TokenElevationClass


def is_elevated() -> bool:
    """当前进程是否提权（High）运行。20261004 皮城女警案定案：游戏画面
    焦点期输入对 Medium 进程完全不可见（ev=0 连移动都没有），提权是与
    游戏同级后的解法——app 启动时据此决定是否自动弹 UAC 重启。"""
    try:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED, False,
                             _k32.GetCurrentProcessId())
        if not h:
            return False
        try:
            tok = ctypes.wintypes.HANDLE()
            if not _a32.OpenProcessToken(h, _TOKEN_QUERY, ctypes.byref(tok)):
                return False
            try:
                elev = ctypes.wintypes.DWORD(0)
                ret_len = ctypes.wintypes.DWORD(0)
                if not _a32.GetTokenInformation(
                        tok, _TOKEN_ELEVATION, ctypes.byref(elev),
                        ctypes.sizeof(elev), ctypes.byref(ret_len)):
                    return False
                return bool(elev.value)
            finally:
                _k32.CloseHandle(tok)
        finally:
            _k32.CloseHandle(h)
    except Exception:  # noqa: BLE001 —— 探针失败按未提权处理
        return False


def foreground_owner() -> str:
    """前台窗口属主 'exe(pid=N, IL=High(0x3000))'；取不到 → '未知'。"""
    try:
        hwnd = _u32.GetForegroundWindow()
        if not hwnd:
            return "未知(无前台窗口)"
        pid = ctypes.wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid = pid.value or 0
        name = f"pid{pid}"
        try:
            h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    n = ctypes.wintypes.DWORD(512)
                    if _k32.QueryFullProcessImageNameW(h, 0, buf,
                                                       ctypes.byref(n)):
                        name = buf.value.rsplit("\\", 1)[-1]
                finally:
                    _k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass
        return f"{name}(pid={pid}, IL={process_il(pid)})"
    except Exception:  # noqa: BLE001
        return "未知"


class MouseLedger(threading.Thread):
    """鼠标点击观察：LL 钩子 + 捕获子进程 + 局内轮询三通道 → 路由。

    纯边沿转发：所有左键按下沿都回调 on_click(x, y, ts)，是否消费由
    调用方按坐标 + 点击时刻的海克斯证据决定——不在观察线程做状态门控
    （旧版 gate 在检测器迟滞确认活跃前静默丢点击，台账空白的根因之一）。
    set_game_mode(True/False) 由 app 相位回调驱动：局内（InProgress）
    备用轮询通道的开关；独立捕获子进程（click_watcher.py）常驻，兜住
    选卡 OCR 风暴期主进程的失明（20261002 虚空之女案）；跨通道按
    点击时刻 ±50ms 同位去重。
    reroll_rect_provider 可返回单矩形或矩形列表（2026-09 起：每卡下方
    各一个刷新按钮，共 3 个）。
    """

    POLL_INTERVAL = 0.02   # 局内轮询间隔（旧值沿用）
    STAT_INTERVAL = 60.0   # 按下沿汇总周期（观测性，0 次也打）
    QUEUE_SIZE = 64        # 处理不过来丢最旧（点击风暴时保最新）
    POLL_IDLE_SLEEP = 0.25   # 轮询通道局外挂起周期（近乎零开销）
    DEDUP_WINDOW = 0.05    # 跨通道同一次按下去重窗（按点击时刻，s）
    DEDUP_POS_TOL = 6      # 去重坐标容差（px，两次采样间指针微动）
    STAT_HISTORY_MAX = 240   # 统计窗历史条数上限（240 窗 = 4 小时）
    HELPER_TAIL_INTERVAL = 0.25   # 子进程会话文件 tail 周期
    HELPER_SPAWN_WAIT = 8.0   # 等 ready 行超时（s），超时回退局内轮询

    def __init__(self, on_click, on_reroll=None,
                 reroll_rect_provider=lambda: None):
        super().__init__(daemon=True, name="MouseLedger")
        self.on_click = on_click            # fn(x, y)
        self.on_reroll = on_reroll          # fn(x, y) —— reroll 矩形命中
        self.reroll_rect_provider = reroll_rect_provider  # fn() -> 矩形|列表|None
        self._running = False
        self._prev_down = False             # 轮询沿检测（局内通道/回退共用）
        # 私有实例而非 windll 共享对象：argtypes 设在共享对象上会泄漏
        # 全进程，殃及 screen_detect/overlay 等处按默认约定调 user32
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        u = self._user32
        u.SetWindowsHookExW.restype = ctypes.wintypes.HHOOK
        u.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC,
            ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD]
        u.CallNextHookEx.restype = LRESULT
        u.CallNextHookEx.argtypes = [
            ctypes.wintypes.HHOOK, ctypes.c_int,
            ctypes.c_size_t, ctypes.c_ssize_t]
        u.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL
        u.UnhookWindowsHookEx.argtypes = [ctypes.wintypes.HHOOK]
        u.GetMessageW.restype = ctypes.c_int
        u.GetMessageW.argtypes = [
            ctypes.POINTER(ctypes.wintypes.MSG), ctypes.wintypes.HWND,
            ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        u.TranslateMessage.restype = ctypes.wintypes.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.DispatchMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
        u.PostThreadMessageW.restype = ctypes.wintypes.BOOL
        u.PostThreadMessageW.argtypes = [
            ctypes.wintypes.DWORD, ctypes.wintypes.UINT,
            ctypes.c_size_t, ctypes.c_ssize_t]
        self._hook_edges = 0     # 统计窗内各来源左键按下沿数（60s 汇总打点）
        self._poll_edges = 0
        self._dup_edges = 0
        self._event_count = 0    # 统计窗内钩子收到的全事件数（含移动）
        self._btn_log_ts = 0.0   # 按键事件日志限频
        self._last_helper_hb = time.time()   # 子进程心跳时刻（停滞检测）
        self._hb_warned = False  # 心跳停滞 WARN 只打一次
        self._zero_windows = 0   # 局内连续零沿统计窗数（≥2 触发告警探针）
        self._stat_history = []  # 统计窗历史（复盘 JSON mouse_stats 字段源）
        self._recent_routes = deque(maxlen=32)   # 跨通道去重（点击时刻锚）
        self._stat_ts = time.monotonic()
        self.mode = "hook"                  # hook / poll（安装失败回退）
        self._game_mode = False             # 局内轮询通道开关（app 相位切换）
        self._helper_proc = None            # click_watcher 子进程
        self._helper_out = None             # 会话文件路径
        self._helper_alive = False          # ready 收到且进程存活
        self._queue = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._hook = None
        self._hook_proc_ref = None          # 防 GC（回调被系统持有）
        self._hook_thread_id = None

    def set_game_mode(self, active: bool) -> None:
        """局内/局外切换（app 在 on_in_progress / on_end_of_game 时调）。

        局内启用 GetAsyncKeyState 轮询通道（LL 钩子在游戏窗口焦点期失明
        的兜底，20261002 寒冰案），局外停用省 CPU。重复同值调用静默
        （champ select session_reset 重入）。"""
        if active == self._game_mode:
            return
        self._game_mode = active
        self._zero_windows = 0
        print(f"[MOUSE] {'进入局内：轮询通道启用' if active else '退出局内：轮询通道停用'}"
              "（LL 钩子并行不变）")

    def stop(self):
        self._running = False
        self._kill_helper()
        if self._hook_thread_id:
            self._user32.PostThreadMessageW(
                self._hook_thread_id, WM_QUIT, 0, 0)

    @staticmethod
    def cursor_pos():
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    @staticmethod
    def left_button_down() -> bool:
        state = ctypes.windll.user32.GetAsyncKeyState(0x01)
        # 高位(0x8000)=当前按下；低位(0x0001)=自上次调用以来按过——
        # 轮询间隔内完成的快速点击只看高位会整次漏掉
        return bool(state & 0x8000) or bool(state & 0x0001)

    @staticmethod
    def _as_rect_list(rr) -> list:
        """单矩形 [x,y,w,h] → [rect]；矩形列表原样；None/垃圾 → []"""
        if not rr:
            return []
        if rr and isinstance(rr[0], (int, float)):
            return [rr]
        return list(rr)

    # ── 路由与观测（处理线程执行） ────────────────────────────────

    def _route(self, x: int, y: int, ts: float = None):
        if ts is None:
            ts = time.time()
        rr = self.reroll_rect_provider() if self.reroll_rect_provider else None
        if any(point_in_rect(x, y, r) for r in self._as_rect_list(rr)):
            if self.on_reroll:
                self.on_reroll(x, y, ts)
        elif self.on_click:
            self.on_click(x, y, ts)

    def _feed(self, src: str, x: int, y: int, ts: float) -> None:
        """处理线程入口：跨通道去重 → 分源计数 → 路由。

        去重（v4）：同一物理按压会同时出现在多个通道（钩子同步、局内
        轮询 ≤20ms、子进程经文件 tail 迟到 ~0.25s）——按**点击时刻**
        ±50ms 且同位 ±6px 对最近路由记录判重，与到达顺序无关，迟到副
        本不误判；窗取 50ms 覆盖采样偏差、远小于人类连点间隔（≥80ms），
        真点击不误吞。漏去重仅多一条重复记录（record_augment 已有
        duplicate 防御），误去重才致命，坐标容差从严。"""
        if src == "hook":
            self._hook_edges += 1
        else:   # poll（局内轮询 / 子进程 tail 同标记）
            for t0, x0, y0 in self._recent_routes:
                if (abs(ts - t0) <= self.DEDUP_WINDOW
                        and abs(x - x0) <= self.DEDUP_POS_TOL
                        and abs(y - y0) <= self.DEDUP_POS_TOL):
                    self._dup_edges += 1
                    self._maybe_report_edges(time.monotonic())
                    return
            self._poll_edges += 1
        self._recent_routes.append((ts, x, y))
        try:
            self._route(x, y, ts)
        except Exception as e:  # noqa: BLE001 —— 单击处理失败不断链
            print(f"[WARN] 点击处理异常: {e}")
        self._maybe_report_edges(time.monotonic())

    def stats_history(self) -> list:
        """统计窗历史快照（复盘 JSON mouse_stats 字段源，调用方只读）。

        台账排障的局级证据：局内钩子/轮询两条通道到底收没收到点击，
        直接看复盘 JSON 就能定案，不必再翻 app_*.log。"""
        return list(self._stat_history)

    def _maybe_report_edges(self, now: float):
        """60s 左键按下沿汇总（观测性打点，0 次也打，分来源+留史）。

        2026-09-30 牛头案对账锚点：按下沿>0 而路由痕迹=0 → 坐标/状态
        门控问题；按下沿=0 → 捕获问题。20261002 寒冰案升级：局内双通
        道全零连续 2 窗 → 探针打前台窗口属主与双方完整性级别，把"输入
        观察被系统性拦截"钉进日志现场；每窗记录进 _stat_history 随
        复盘 JSON 落盘（进程死亡/日志丢失后仍有局级证据）。"""
        if now - self._stat_ts < self.STAT_INTERVAL:
            return
        print(f"[MOUSE] 近{int(self.STAT_INTERVAL)}s 左键按下沿 "
              f"hook={self._hook_edges} poll={self._poll_edges} "
              f"去重={self._dup_edges} 事件={self._event_count}"
              f"（{self.mode}）")
        warned = False
        if (self._game_mode and self._hook_edges == 0
                and self._poll_edges == 0):
            self._zero_windows += 1
            if self._zero_windows >= 2:
                self._zero_windows = 0
                warned = True
                print(f"[MOUSE][WARN] 局内连续 2 窗零按下沿——钩子/子进程/"
                      f"轮询三通道都未捕获任何点击（事件={self._event_count}"
                      f"，0=钩子连移动都没收到）。前台 {foreground_owner()}，"
                      f"本进程 IL={self_il()}。若人在游戏内，说明输入观察被"
                      "系统性拦截（提权/反作弊升级），台账将空——请回报这行"
                      "日志")
        else:
            self._zero_windows = 0
        self._stat_history.append({
            "ts": round(time.time(), 1),
            "hook": self._hook_edges, "poll": self._poll_edges,
            "dup": self._dup_edges, "ev": self._event_count,
            "game": bool(self._game_mode),
            "helper": bool(self._helper_alive), "warn": warned})
        if len(self._stat_history) > self.STAT_HISTORY_MAX:
            del self._stat_history[:len(self._stat_history)
                                   - self.STAT_HISTORY_MAX]
        self._hook_edges = 0
        self._poll_edges = 0
        self._dup_edges = 0
        self._event_count = 0
        self._stat_ts = now

    # ── 钩子模式（默认） ──────────────────────────────────────────

    def _install_hook(self) -> bool:
        try:
            self._hook_proc_ref = HOOKPROC(self._hook_proc)
            self._hook = self._user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._hook_proc_ref, None, 0)
            if not self._hook:
                return False
            self._hook_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            return True
        except Exception:  # noqa: BLE001 —— 钩子不可用走回退
            return False

    def _hook_proc(self, n_code, w_param, l_param):
        # 回调在系统鼠标路径上执行：任何慢/阻塞都会拖慢全系统鼠标
        # （游戏内"鼠标动不了"的元凶，同 keyboard 库教训）——只允许
        # 非阻塞 O(1) 投递，一切异常兜底，永远立即透传
        try:
            if n_code == HC_ACTION:
                # 全事件计数（含移动）：20261003 皮城女警案——选卡按压对
                # 全部通道不可见而进程活着，需分辨"输入整体消失"与"仅按
                # 键消失"。移动事件游戏内恒流，本计数归零即钩子没跑。
                self._event_count += 1
                btn = _BUTTON_NAMES.get(w_param)
                if btn is not None and w_param != WM_LBUTTONDOWN:
                    # 非左键按下沿限频留痕（排除"用户用非左键选卡/驱动
                    # 重映射"这类最后的平凡解释）；左键沿由 60s 边沿汇总
                    # 统计承担，不逐条入队。留痕文案投队列由处理线程打：
                    # 回调在系统鼠标路径上，print 的控制台锁阻塞会拖慢
                    # 全系统鼠标
                    pt = ctypes.cast(
                        l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    now = time.monotonic()
                    if now - self._btn_log_ts >= 2.0:
                        self._btn_log_ts = now
                        inj = " 注入" if pt.flags & LLMHF_INJECTED else ""
                        self._queue.put_nowait(
                            ("log", f"[MOUSE] 按键 {btn} ({pt.pt.x},{pt.pt.y})"
                                    f"{inj}"))
                if w_param == WM_LBUTTONDOWN:
                    p = ctypes.cast(
                        l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents.pt
                    try:
                        self._queue.put_nowait(
                            ("hook", p.x, p.y, time.time()))
                    except queue.Full:
                        try:
                            self._queue.get_nowait()   # 丢最旧保最新
                        except queue.Empty:
                            pass
                        try:
                            self._queue.put_nowait(
                                ("hook", p.x, p.y, time.time()))
                        except queue.Full:
                            pass
        except Exception:  # noqa: BLE001 —— 钩子回调绝不允许异常逃逸
            pass
        # 永远透传：不拦截不吞事件（只读观察合规边界）
        return self._user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _run_hook(self):
        # 消息循环：LL 钩子回调在本线程被系统调用，必须有消息泵
        msg = ctypes.wintypes.MSG()
        while self._running:
            r = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:   # 0 = WM_QUIT，-1 = 错误
                break
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))
        if self._hook:
            self._user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    # ── 独立捕获子进程（v4，20261002 虚空之女案） ─────────────────

    def _spawn_helper(self) -> bool:
        """拉起 click_watcher.py 子进程（独立解释器独立 GIL，NORMAL 优
        先级）。主进程在选卡 OCR 风暴期被饿死时，它仍能采样——v3 双通
        道在选卡瞬间双双失明的兜底，同时是"系统层过滤"的鉴别仪器。"""
        try:
            if getattr(sys, "frozen", False):
                # 冻结构建下 __file__ 是临时解包目录、sys.executable 是
                # 本体——拉不起 watcher 脚本，直接走局内轮询兜底
                print("[WARN] 冻结构建无独立捕获子进程，局内轮询兜底")
                return False
            out_dir = BASE_DIR / "log" / "clicks_live"
            out_dir.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - 7 * 86400
            for old in out_dir.glob("clicks_*.txt"):
                try:   # 会话文件每次启动一份（~0.1MB），7 天外清理防堆积
                    if old.stat().st_mtime < cutoff:
                        old.unlink()
                except OSError:
                    pass
            self._helper_out = out_dir / (
                f"clicks_{time.strftime('%Y%m%d_%H%M%S')}.txt")
            script = os.path.join(os.path.dirname(__file__),
                                  "click_watcher.py")
            self._helper_proc = subprocess.Popen(
                [sys.executable, script, str(os.getpid()),
                 str(self._helper_out)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception as e:  # noqa: BLE001 —— 子进程不可用走局内轮询
            print(f"[WARN] 点击捕获子进程启动失败，退回局内轮询: {e}")
            self._helper_proc = None
            return False

    def _kill_helper(self):
        proc, self._helper_proc = self._helper_proc, None
        self._helper_alive = False
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 —— 收尾失败不必纠缠
                pass

    @staticmethod
    def _parse_helper_line(line: str):
        """子进程会话文件行 → (ts, x, y) | None（ready 行/坏行过滤）"""
        parts = line.split()
        if len(parts) != 3 or parts[0] == "ready":
            return None
        try:
            return float(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return None

    def _tail_loop(self):
        """tail 子进程会话文件：点击行 → 统一队列（按点击时刻参与去重）。
        子进程死亡（崩溃/被杀）→ [WARN] 并交回局内轮询兜底。"""
        path = self._helper_out
        deadline = time.monotonic() + self.HELPER_SPAWN_WAIT
        while self._running and path is not None:
            if path.exists():
                break
            if time.monotonic() > deadline:
                print("[WARN] 点击捕获子进程会话文件未就绪，退回局内轮询")
                self._kill_helper()
                return
            time.sleep(0.1)
        else:
            return
        try:
            with open(path, "r", encoding="ascii", errors="ignore") as fh:
                self._helper_alive = True
                print("[MOUSE] 捕获子进程在线：三通道就绪"
                      "（钩子+子进程+局内轮询兜底）")
                while self._running:
                    proc = self._helper_proc
                    if proc is None or proc.poll() is not None:
                        print("[WARN] 点击捕获子进程退出，退回局内轮询")
                        self._helper_alive = False
                        return
                    line = fh.readline()
                    if not line:
                        # 心跳停滞检测：子进程活着但 30s 无心跳 = 被挂起/
                        # 输入路径被拦（20261003 皮城女警案的鉴别仪器）
                        if (self._helper_alive and self._game_mode
                                and not self._hb_warned
                                and time.time() - self._last_helper_hb > 30):
                            self._hb_warned = True
                            print("[MOUSE][WARN] 捕获子进程心跳停滞（>30s）"
                                  "——子进程被挂起或其输入观察被拦截，"
                                  "退回局内轮询兜底")
                            self._helper_alive = False
                        time.sleep(self.HELPER_TAIL_INTERVAL)
                        continue
                    parts = line.split()
                    if parts and parts[0] == "hb":
                        self._last_helper_hb = time.time()
                        self._hb_warned = False
                        continue
                    if parts and parts[0] == "bye":
                        print("[WARN] 点击捕获子进程自行退出，退回局内轮询")
                        self._helper_alive = False
                        return
                    parsed = self._parse_helper_line(line)
                    if parsed is None:
                        continue
                    ts, x, y = parsed
                    try:
                        self._queue.put_nowait(("poll", x, y, ts))
                    except queue.Full:
                        pass   # 队列满=处理积压，丢副本（其他通道兜底）
        except Exception as e:  # noqa: BLE001 —— tail 故障不影响主链路
            print(f"[WARN] 捕获子进程 tail 异常: {e}")
            self._helper_alive = False

    # ── 局内轮询通道（子进程不可用时的兜底） ─────────────────────

    def _poll_loop(self):
        """局内 GetAsyncKeyState 轮询：LL 钩子在选卡期失明的兜底。
        子进程在线时挂起（三通道同采样只会多出迟到副本）；局外挂起
        省 CPU；回退 poll 模式时让位（_run_poll 已覆盖全时段）。"""
        while self._running:
            if (not self._game_mode or self.mode != "hook"
                    or self._helper_alive):
                time.sleep(self.POLL_IDLE_SLEEP)
                continue
            try:
                down = self.left_button_down()
                if down and not self._prev_down:
                    x, y = self.cursor_pos()
                    try:
                        self._queue.put_nowait(
                            ("poll", x, y, time.time()))
                    except queue.Full:
                        pass      # 队列满=处理积压，钩子侧保最新已兜底
                    self._prev_down = True
                elif not down:
                    self._prev_down = False
            except Exception as e:  # noqa: BLE001 —— 轮询异常不中断线程
                print(f"[WARN] 局内轮询异常: {e}")
            time.sleep(self.POLL_INTERVAL)

    # ── 处理线程（消费钩子/轮询/子进程队列） ─────────────────────

    def _process_loop(self):
        while self._running:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._maybe_report_edges(time.monotonic())
                continue
            if item[0] == "log":
                # 钩子回调投回的留痕文案：由处理线程打——回调在系统鼠标
                # 路径上执行，print 遇控制台阻塞会拖慢全系统鼠标（与
                # "回调只允许非阻塞 O(1) 投递"的自述一致化）
                print(item[1])
                continue
            _, x, y, ts = item
            self._feed(item[0], x, y, ts)

    # ── 回退轮询（钩子安装失败时） ────────────────────────────────

    def _run_poll(self):
        while self._running:
            try:
                down = self.left_button_down()
                if down and not self._prev_down:
                    self._poll_edges += 1
                    x, y = self.cursor_pos()
                    self._route(x, y)
                self._prev_down = down
                self._maybe_report_edges(time.monotonic())
            except Exception as e:  # noqa: BLE001 —— 轮询异常不中断线程
                print(f"[WARN] 鼠标观察异常: {e}")
            time.sleep(self.POLL_INTERVAL)

    # ── 生命周期 ──────────────────────────────────────────────────

    def run(self):
        self._running = True
        worker = threading.Thread(target=self._process_loop,
                                  daemon=True, name="MouseLedger-Process")
        worker.start()
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="MouseLedger-Poll").start()
        print(f"[MOUSE] 观察启动：本进程 IL={self_il()}，LL 钩子 + 捕获"
              "子进程 + 局内轮询三通道（选卡期主进程失明由子进程兜底）")
        try:
            if self._install_hook():
                if self._spawn_helper():
                    threading.Thread(target=self._tail_loop, daemon=True,
                                     name="MouseLedger-Tail").start()
                self._run_hook()
            else:
                print("[WARN] 低级鼠标钩子安装失败，回退轮询模式"
                      "（OCR 忙时快速点击可能漏捕获）")
                self.mode = "poll"
                self._run_poll()
        finally:
            self._kill_helper()


class CardLedger:
    """点击 → 卡片序号映射（界面活跃窗口内由 GameLedger 编排消费）"""

    def __init__(self, rects_provider):
        self.rects_provider = rects_provider  # fn() -> [像素矩形 x3] | None

    def card_at(self, x: int, y: int):
        rects = self.rects_provider()
        if not rects:
            return None
        for i, r in enumerate(rects):
            if point_in_rect(x, y, r):
                return i
        return None
