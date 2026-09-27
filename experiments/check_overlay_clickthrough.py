"""最小离线检查：overlay 完整穿透样式代码路径可执行且实际生效。

只建 Overlay + StubSession，不启动任何服务/线程；
无显示环境（Tk 初始化失败）时安全跳过，exit 0。
用法: uv run python experiments/check_overlay_clickthrough.py
"""
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GWL_EXSTYLE = -20


def main() -> int:
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001 —— 无显示环境
        print(f"[SKIP] 无显示环境，跳过穿透检查: {e}")
        return 0
    root.withdraw()

    from hexa.ui.overlay import (CLICK_THROUGH_MASK, WS_EX_TOPMOST, Overlay)

    class StubSession:
        def ui_state(self):
            return {"my_hero": "Check", "stage": "in_game", "current_path": "",
                    "path_drift": False, "skill_order": "Q>E>W", "augments": [],
                    "threat": "", "suggestion": "", "card_ranks": []}

    try:
        ov = Overlay(root, StubSession(), None, visible=True)
        root.update()  # 触发 realize + 首次穿透设置（构造内完成）
        ret = ov._apply_click_through()  # 显式再走一遍代码路径
        hwnd = ov._top_hwnd()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        want = CLICK_THROUGH_MASK | WS_EX_TOPMOST
        geom = ov.win.geometry()
        topmost = bool(ov.win.attributes("-topmost"))
    finally:
        root.destroy()

    print(f"[INFO] hwnd=0x{hwnd:X} exstyle=0x{style:08X} "
          f"want_mask=0x{want:08X} geometry={geom} topmost={topmost}")
    if ret and (style & want) == want and topmost:
        print("[PASS] LAYERED|TRANSPARENT|NOACTIVATE|TOPMOST 全部置上（点击/悬停全穿透）")
        return 0
    print("[FAIL] 穿透样式未全部生效")
    return 1


if __name__ == "__main__":
    sys.exit(main())
