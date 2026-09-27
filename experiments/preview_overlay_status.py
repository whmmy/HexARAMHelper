"""浮窗状态标识预览：假 session 驱动 4 个状态，各截一张 PNG（视觉自检用）。

用法: .venv/Scripts/python.exe experiments/preview_overlay_status.py [输出目录]
状态: 1 识别中 / 2 识别成功+已选+AI生成中 / 3 关闭后失败摘要 / 4 AI 空闲常规
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from hexa.ui.overlay import Overlay

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "media" / "analysis")

STATE = {
    "stage": "in_game", "my_hero": "暗裔剑魔", "ally": [], "enemy": [],
    "augments": [], "my_items": [], "current_path": "ad", "path_drift": False,
    "threat": "AP×3", "suggestion": "出装主线\n① 星蚀 —— 测试",
    "suggestion_kind": "next_items", "suggestion_source": "rule",
    "card_ranks": [], "skill_order": "主Q副E", "dd_version": "16.1.1",
    "hex_active": False, "hex_recognized": False, "hex_picked": None,
    "hex_last": "",
    "engine": {"mode": "ai", "calls": 3, "budget": 15, "inflight": 0,
               "ctx_source": "ai"},
}


class FakeSession:
    def __init__(self):
        self.state = dict(STATE)

    def ui_state(self):
        return self.state


def shot(root, overlay, name):
    overlay._refresh()          # 立即渲染当前假状态（不等 500ms 轮询）
    root.update_idletasks()
    root.update()
    time.sleep(0.3)
    root.update()
    x = overlay.win.winfo_rootx()
    y = overlay.win.winfo_rooty()
    w = overlay.win.winfo_width()
    h = overlay.win.winfo_height()
    from PIL import ImageGrab
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(OUT / name)
    print(f"saved {OUT / name}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    sess = FakeSession()
    overlay = Overlay(root, sess, None, visible=True)
    # 挪到左侧：右缘贴边位置可能与正在运行的真实实例重叠，截图会混叠
    overlay.win.geometry("+40+120")

    # 1. 界面在屏未识别
    sess.state["hex_active"] = True
    shot(root, overlay, "overlay_status_1_detecting.png")

    # 2. 识别成功 + 点击第 2 张 + AI 精修在途
    sess.state["card_ranks"] = [
        {"name": "贪欲束缚", "grade": "S", "reason": "契合出装主线"},
        {"name": "极冰寒影", "grade": "A", "reason": "团战控场"},
        {"name": "魔宗 rooted", "grade": "B", "reason": "泛用"}]
    sess.state["hex_recognized"] = True
    sess.state["hex_picked"] = [1, "极冰寒影"]
    sess.state["engine"]["inflight"] = 2
    shot(root, overlay, "overlay_status_2_ok_picked_busy.png")

    # 3. 界面关闭后：失败摘要 + AI 空闲
    sess.state.update(card_ranks=[], hex_active=False, hex_recognized=False,
                      hex_picked=None, hex_last="上轮: 未识别成功 ✗")
    sess.state["engine"]["inflight"] = 0
    shot(root, overlay, "overlay_status_3_closed_fail.png")

    # 4. 常规（隐藏摘要块）
    sess.state["hex_last"] = ""
    shot(root, overlay, "overlay_status_4_idle.png")
    root.destroy()


if __name__ == "__main__":
    main()
