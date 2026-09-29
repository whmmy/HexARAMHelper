"""鼠标记账（计划 §3：界面活跃窗口内，点击落点 = 卡片矩形 → 记账本轮已选）

实现说明（v4.1 §3 澄清）：`keyboard` 库只提供键盘低级钩子，不含鼠标事件；
按计划明文的兜底方案实现——20ms 轮询 GetAsyncKeyState(VK_LBUTTON) 边沿 +
GetCursorPos 取落点。纯 Win32 只读查询（不拦截、不注入、不改鼠标行为），
合规边界与 P0 结论一致。零新增依赖（ctypes 标准库）。

只读约束：所有左键按下沿都回调 on_click，是否消费由调用方按坐标 +
海克斯证据决定（2026-09-28 真机复盘：旧版在观察线程按检测状态门控，
迟滞确认前的点击被静默丢弃且无日志 → 台账空白根因之一）；
reroll 按钮矩形命中 → on_reroll 回调（触发主动重扫）。
"""
import ctypes
import ctypes.wintypes
import threading
import time

from hexa.core.screen_detect import point_in_rect

VK_LBUTTON = 0x01


class MouseLedger(threading.Thread):
    """点击观察线程：20ms 轮询左键边沿 → 回调 (x, y)。

    纯边沿转发：所有左键按下沿都回调 on_click，是否消费由调用方按
    坐标 + 海克斯证据决定——不在观察线程做状态门控（旧版 gate 在
    检测器迟滞确认活跃前静默丢点击，台账空白的根因之一）。
    reroll_rect_provider 可返回单矩形或矩形列表（2026-09 起：每卡下方
    各一个刷新按钮，共 3 个）。
    """

    POLL_INTERVAL = 0.02   # 20ms：抓住 >=20ms 的点击（50ms 间隔内完成的快速点击只靠高位会整次漏掉）

    def __init__(self, on_click, on_reroll=None,
                 reroll_rect_provider=lambda: None):
        super().__init__(daemon=True, name="MouseLedger")
        self.on_click = on_click            # fn(x, y)
        self.on_reroll = on_reroll          # fn() —— reroll 矩形命中
        self.reroll_rect_provider = reroll_rect_provider  # fn() -> 矩形|列表|None
        self._running = False
        self._prev_down = False
        self._user32 = ctypes.windll.user32

    def stop(self):
        self._running = False

    @staticmethod
    def cursor_pos():
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    @staticmethod
    def left_button_down() -> bool:
        state = ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON)
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

    def run(self):
        self._running = True
        while self._running:
            try:
                down = self.left_button_down()
                if down and not self._prev_down:
                    x, y = self.cursor_pos()
                    rr = self.reroll_rect_provider() if self.reroll_rect_provider else None
                    if any(point_in_rect(x, y, r) for r in self._as_rect_list(rr)):
                        if self.on_reroll:
                            self.on_reroll()
                    elif self.on_click:
                        self.on_click(x, y)
                self._prev_down = down
            except Exception as e:  # noqa: BLE001 —— 轮询异常不中断线程
                print(f"[WARN] 鼠标观察异常: {e}")
            time.sleep(self.POLL_INTERVAL)


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
