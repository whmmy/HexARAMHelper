"""鼠标记账（计划 §3：界面活跃窗口内，点击落点 = 卡片矩形 → 记账本轮已选）

实现（v2，2026-09-30 牛头案重写）：WH_MOUSE_LL 低级鼠标钩子——系统把每个
左键按下事件直接投递给钩子，不依赖轮询与 GIL，彻底消灭"漏点击"。

旧版 20ms GetAsyncKeyState 轮询的致命缺陷（两局 7 轮海克斯选卡零记账的
根因）：海克斯界面活跃期恰是检测器每秒全帧 OCR 最忙时（14MB 帧拷贝+
识别），轮询线程被 GIL/调度饿出 100ms+ 间隙，叠加 Windows sleep 精度
15-31ms，玩家 60-80ms 的快速点卡整段落进间隙——高位（按下中）没赶上，
低位（近期按过）被游戏每帧调用抢走（MSDN：其他进程调用会消费该位）。
而所有被捕获的点击全部产生于 OCR 空闲的非活跃期/局后，定向证据闭环。

架构：钩子线程（安装钩子 + 消息循环；回调只投队列即返回——系统会强摘
响应过慢的钩子）+ 处理线程（消费队列，执行路由/诊断/记账，慢不影响钩子）。
安装失败回退旧轮询（带 [WARN]）。

只读约束不变：CallNextHookEx 透传，不拦截、不注入、不改鼠标行为
（合规边界与 P0 结论一致）；是否消费由调用方按坐标 + 海克斯证据决定。
reroll 按钮矩形命中 → on_reroll 回调（触发主动重扫）。
"""
import ctypes
import ctypes.wintypes
import queue
import threading
import time

from hexa.core.screen_detect import point_in_rect

WM_LBUTTONDOWN = 0x0201
WM_QUIT = 0x0012
WH_MOUSE_LL = 14
HC_ACTION = 0


class _MSLLHOOKSTRUCT(ctypes.Structure):
    """低级鼠标钩子事件体（pt = 屏幕物理坐标，与 GetCursorPos 同基准）"""
    _fields_ = [("pt", ctypes.wintypes.POINT),
                ("mouseData", ctypes.wintypes.DWORD),
                ("flags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MouseLedger(threading.Thread):
    """鼠标点击观察：LL 钩子（默认）或轮询回退 → 路由 (x, y)。

    纯边沿转发：所有左键按下沿都回调 on_click，是否消费由调用方按
    坐标 + 海克斯证据决定——不在观察线程做状态门控（旧版 gate 在
    检测器迟滞确认活跃前静默丢点击，台账空白的根因之一）。
    reroll_rect_provider 可返回单矩形或矩形列表（2026-09 起：每卡下方
    各一个刷新按钮，共 3 个）。
    """

    POLL_INTERVAL = 0.02   # 回退轮询间隔（旧值沿用）
    STAT_INTERVAL = 60.0   # 按下沿汇总周期（观测性，0 次也打）
    QUEUE_SIZE = 64        # 处理不过来丢最旧（点击风暴时保最新）

    def __init__(self, on_click, on_reroll=None,
                 reroll_rect_provider=lambda: None):
        super().__init__(daemon=True, name="MouseLedger")
        self.on_click = on_click            # fn(x, y)
        self.on_reroll = on_reroll          # fn(x, y) —— reroll 矩形命中
        self.reroll_rect_provider = reroll_rect_provider  # fn() -> 矩形|列表|None
        self._running = False
        self._prev_down = False             # 仅回退轮询用
        self._user32 = ctypes.windll.user32
        self._edge_count = 0      # 统计窗内左键按下沿数（60s 汇总打点）
        self._stat_ts = time.monotonic()
        self.mode = "hook"                  # hook / poll（安装失败回退）
        self._queue = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._hook = None
        self._hook_proc_ref = None          # 防 GC（回调被系统持有）
        self._hook_thread_id = None

    def stop(self):
        self._running = False
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

    def _route(self, x: int, y: int):
        rr = self.reroll_rect_provider() if self.reroll_rect_provider else None
        if any(point_in_rect(x, y, r) for r in self._as_rect_list(rr)):
            if self.on_reroll:
                self.on_reroll(x, y)
        elif self.on_click:
            self.on_click(x, y)

    def _count_edge(self):
        self._edge_count += 1

    def _maybe_report_edges(self, now: float):
        """60s 左键按下沿汇总（观测性打点，0 次也打）。

        2026-09-30 牛头案对账锚点：按下沿>0 而路由痕迹=0 → 坐标/状态
        门控问题；按下沿=0 → 键位/捕获问题（v2 钩子后应不再出现）。"""
        if now - self._stat_ts < self.STAT_INTERVAL:
            return
        print(f"[MOUSE] 近{int(self.STAT_INTERVAL)}s 左键按下沿 "
              f"{self._edge_count} 次（{self.mode}）")
        self._edge_count = 0
        self._stat_ts = now

    # ── 钩子模式（默认） ──────────────────────────────────────────

    def _install_hook(self) -> bool:
        try:
            self._hook_proc_ref = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_int,
                ctypes.c_size_t, ctypes.c_ssize_t)(self._hook_proc)
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
            if n_code == HC_ACTION and w_param == WM_LBUTTONDOWN:
                pt = ctypes.cast(
                    l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents.pt
                try:
                    self._queue.put_nowait((pt.x, pt.y))
                except queue.Full:
                    try:
                        self._queue.get_nowait()   # 丢最旧保最新
                    except queue.Empty:
                        pass
                    try:
                        self._queue.put_nowait((pt.x, pt.y))
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

    # ── 处理线程（消费钩子队列） ──────────────────────────────────

    def _process_loop(self):
        while self._running:
            try:
                x, y = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._maybe_report_edges(time.monotonic())
                continue
            try:
                self._count_edge()
                self._route(x, y)
            except Exception as e:  # noqa: BLE001 —— 单击处理失败不断链
                print(f"[WARN] 点击处理异常: {e}")
            self._maybe_report_edges(time.monotonic())

    # ── 回退轮询（钩子安装失败时） ────────────────────────────────

    def _run_poll(self):
        while self._running:
            try:
                down = self.left_button_down()
                if down and not self._prev_down:
                    self._count_edge()
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
        if self._install_hook():
            self._run_hook()
        else:
            print("[WARN] 低级鼠标钩子安装失败，回退轮询模式"
                  "（OCR 忙时快速点击可能漏捕获）")
            self.mode = "poll"
            self._run_poll()


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
