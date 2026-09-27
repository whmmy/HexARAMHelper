"""鼠标记账（计划 §3：界面活跃窗口内，点击落点 = 卡片矩形 → 记账本轮已选）

实现说明（v4.1 §3 澄清）：`keyboard` 库只提供键盘低级钩子，不含鼠标事件；
按计划明文的兜底方案实现——50ms 轮询 GetAsyncKeyState(VK_LBUTTON) 边沿 +
GetCursorPos 取落点。纯 Win32 只读查询（不拦截、不注入、不改鼠标行为），
合规边界与 P0 结论一致。零新增依赖（ctypes 标准库）。

只读约束：仅当调用方处于"海克斯界面活跃"窗口时才消费点击；
reroll 按钮矩形命中 → on_reroll 回调（触发主动重扫）。
"""
import ctypes
import ctypes.wintypes
import threading
import time

from hexa.core.screen_detect import point_in_rect

VK_LBUTTON = 0x01


class MouseLedger(threading.Thread):
    """点击观察线程：50ms 轮询左键边沿 → 回调 (x, y)。

    调用方在回调里自行判断坐标是否落在卡片/reroll 矩形内（gate 属性控制
    是否消费——非海克斯活跃期置 gate=False 时回调仍触发但消费方忽略，
    这里直接不回调更省事：gate 关闭时静默丢弃）。
    reroll_rect_provider 可返回单矩形或矩形列表（2026-09 起：每卡下方
    各一个刷新按钮，共 3 个）。
    """

    POLL_INTERVAL = 0.05

    def __init__(self, on_click, gate=lambda: True, on_reroll=None,
                 reroll_rect_provider=lambda: None):
        super().__init__(daemon=True, name="MouseLedger")
        self.on_click = on_click            # fn(x, y)
        self.gate = gate                    # fn() -> bool（海克斯界面活跃?）
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
        # GetAsyncKeyState：返回值最高位=当前按下；取 SHORT 后判断符号位
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)

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
                if down and not self._prev_down and self.gate():
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
