"""真实窗口截图（主窗口 / 设置窗口 / overlay）——UI 重构的实机验证。

驱动序列与 app.py 的 HEXA_MOCK 模式同源（同一批 session 回调），但同步
执行、无 sleep，便于脚本化出图。视觉稿（docs/ui/preview.html）是纸面，
这份是真机渲染。
用法: .venv/Scripts/python.exe experiments/preview_windows.py [输出目录]
"""
import os
import queue as _queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk
from PIL import ImageGrab

from hexa.config import Settings
from hexa.core.game_state import GameSession
from hexa.core.live_api import LiveSnapshot, PlayerInfo
from hexa.core.rules_engine import RulesEngine
from hexa.knowledge.kb import KnowledgeBase
from hexa.ui.main_window import MainWindow
from hexa.ui.overlay import Overlay
from hexa.ui.settings_window import SettingsWindow

OUT = Path(sys.argv[1] if len(sys.argv) > 1
           else Path(__file__).parent / "media" / "analysis")
# LIGHT=1 走轻量局（少两条军师），便于 1080p 截屏机一屏展示完整主窗口
LIGHT = os.environ.get("HEXA_PREVIEW_LIGHT", "1") == "1"


def drive(session, kb, hero="暗裔剑魔", light=False):
    """与 app.py mock_drive 同一序列（同步版）

    light=True：跳过死亡与敌方关键装两步（少两条军师），用于在 1080p 截屏机
    上让满载内容能一屏装下——军师条数本就是数据决定的，不是伪造布局。
    """
    session.on_champ_select({"ally": [hero], "enemy": [],
                             "my_hero": hero, "source": "mock"})
    session.on_game_start({
        "ally": [hero, "九尾妖狐", "皎月女神", "盲僧", "众星之子"],
        "enemy": ["黑暗之女", "狂暴之心", "放逐之刃", "诺克萨斯之手", "琴瑟仙女"],
        "my_hero": hero, "source": "mock"})
    session.on_in_progress()
    top = kb.top_augments(hero, 3)
    cards = (["贪欲束缚"] + [n for n in top if n != "贪欲束缚"])[:3]
    session.rank_augment_cards(cards)
    session.record_augment("贪欲束缚", tier="棱彩")

    def snap(items, dead=False, enemy_items=None):
        s = LiveSnapshot(ts=time.time(), ok=True)
        s.my_champion = hero
        s.my_is_dead = dead
        s.my_current_gold = 4000
        s.my_items = [{"name": n, "count": 1} for n in items]
        s.players.append(PlayerInfo(champion=hero, champion_en="Aatrox",
                                    team="ORDER", items=s.my_items,
                                    kills=2, deaths=1 if dead else 0, assists=3))
        s.players.append(PlayerInfo(champion="诺克萨斯之手", champion_en="Darius",
                                    team="CHAOS",
                                    items=[{"name": n, "count": 1}
                                           for n in (enemy_items or ["多兰之刃"])],
                                    kills=5, deaths=1, assists=3))
        for cn in ("黑暗之女", "狂暴之心", "琴瑟仙女"):
            s.players.append(PlayerInfo(champion=cn, champion_en="", team="CHAOS"))
        session.on_snapshot(s)

    snap(["多兰之刃"])
    snap(["多兰之刃", "吸血鬼节杖"])
    if not light:
        snap(["多兰之刃", "吸血鬼节杖"], dead=True)
        snap(["多兰之刃", "吸血鬼节杖"], enemy_items=["荆棘之甲"])


def _toplevel_hwnd(win):
    """Tk 的 winfo_id 只是内层窗口，取 GA_ROOT 顶层 HWND（与 overlay 同口径）"""
    import ctypes
    hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
    return hwnd or ctypes.windll.user32.GetParent(win.winfo_id())


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def _unlayer(win):
    """临时摘掉分层/穿透样式：分层窗口 PrintWindow 只能抓出全黑
    （20261021 实测），出图需要真实像素。抓完用 _relayer 恢复。"""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _toplevel_hwnd(win)
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                           ex & ~WS_EX_LAYERED & ~WS_EX_TRANSPARENT)
    try:
        win.attributes("-alpha", 1.0)
    except Exception:
        pass
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                        | SWP_FRAMECHANGED)


def _relayer(win):
    """恢复分层/穿透样式（出图后调用，运行期行为不变）"""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _toplevel_hwnd(win)
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                           ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)
    try:
        win.attributes("-alpha", 0.88)
    except Exception:
        pass
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                        | SWP_FRAMECHANGED)


def grab(win, name, settle=0.6, flag=2):
    """抓窗口自身内容（Win32 PrintWindow），不依赖 z-order。

    ImageGrab 抓的是屏幕像素：窗口被别的窗口压住、或跨显示器置顶失效时，
    截出来的是别人的界面/壁纸（2026-09-30 连续踩坑：viewable=1 但抓屏是壁纸）。
    PrintWindow 直接让窗口把自己画进离屏 DC，与谁在上面无关。
    """
    import ctypes
    from ctypes import wintypes

    win.update_idletasks()
    win.update()
    time.sleep(settle)
    win.update()
    w, h = win.winfo_width(), win.winfo_height()
    if w <= 1 or h <= 1:
        print(f"[SKIP] {name}: 未映射 (w={w} h={h})")
        return

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd = _toplevel_hwnd(win)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    ww, hh = rect.right - rect.left, rect.bottom - rect.top
    if ww <= 1 or hh <= 1:
        print(f"[SKIP] {name}: hwnd 尺寸异常 ({ww}x{hh})")
        return

    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, hh)
    gdi32.SelectObject(mdc, bmp)
    # flag=2 = PW_RENDERFULLCONTENT（Vista+；分层窗口必须带）；浮窗出图用
    # flag=0——对无边框顶层窗，flag=2 会把内容**画两遍**（重影，20261021 实测）
    ok = user32.PrintWindow(hwnd, mdc, flag)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    hdr = BITMAPINFOHEADER()
    hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    hdr.biWidth = ww
    hdr.biHeight = -hh            # 负数 = 自上而下
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = 0
    buf = ctypes.create_string_buffer(ww * hh * 4)
    gdi32.GetDIBits(mdc, bmp, 0, hh, buf, ctypes.byref(hdr), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)

    from PIL import Image
    img = Image.frombuffer("RGBA", (ww, hh), buf, "raw", "BGRA", 0, 1)
    img.convert("RGB").save(OUT / name)
    print(f"saved {OUT / name}  ({ww}x{hh} PrintWindow={ok})")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    root.withdraw()

    kb = KnowledgeBase()
    settings = Settings()
    rules = RulesEngine(kb)
    import hexa.ai.engine as engine_mod
    engine = engine_mod.build_engine(kb, rules, settings)
    session = GameSession(kb, rules, settings=settings, engine=engine)

    ui_queue, log_queue = _queue.Queue(), _queue.Queue()
    main_win = MainWindow(root, session, ui_queue, log_queue)
    overlay = Overlay(root, session, ui_queue, visible=True, plain=True)
    overlay.win.geometry("+40+120")
    settings_win = SettingsWindow(root, settings, kb,
                                  lambda: session.engine.stats(),
                                  lambda: None)
    settings_win.withdraw()

    drive(session, kb, light=LIGHT)
    for _ in range(6):          # 让 500ms 轮询链把内容填进面板
        root.update()
        time.sleep(0.3)

    root.deiconify()
    root.update()
    # 20261021 双栏案：主窗口自带几何校准（横向 1040 双栏），这里**不再**
    # 强改 geometry——旧脚本写死 560 宽会把两栏压扁。新布局满载内容约
    # 780px 高，1080p 截屏机一屏装得下。
    if LIGHT:
        main_win.txt_log.config(height=5)
        root.update_idletasks()
        print(f"[note] 主窗口 {root.winfo_width()}x{root.winfo_height()}"
              "（双栏仪表盘，自带几何校准）")
    # 挪到 (20,20)：窗口比屏幕工作区高时，WM 可能把它放到屏幕外，
    # PrintWindow 对屏外区域渲染成白块（底部日志卡整块变白）
    root.geometry("+20+20")
    root.update()
    grab(root, "ui_v4_main_window.png")

    # 设置窗口：抓图前转无边框并挪到 (10,10)。带标题栏时客户区起点低于 0，
    # 内容 1009px 的窗口底部会落到屏幕外（抓屏得到黑边），无边框后整窗进屏内。
    settings_win.overrideredirect(True)
    settings_win.geometry("+10+10")
    settings_win.deiconify()
    settings_win.update()
    grab(settings_win, "ui_v4_settings_window.png")

    # 浮窗：分层窗口（WS_EX_LAYERED|WS_EX_TRANSPARENT）PrintWindow 抓出来
    # 是全黑（20261021 实测），抓图前临时摘掉分层/穿透样式 + 置 alpha=1，
    # 抓完再恢复——只影响出图，不改运行期行为。
    _unlayer(overlay.win)
    overlay.win.update()
    grab(overlay.win, "ui_v4_overlay_live.png", flag=0)
    _relayer(overlay.win)

    # 海克斯时刻评级卡单独出一张：主窗口满载内容 ~1200px，而 1080p 截屏机的
    # 窗口管理器把客户区限在 1061px，最后一张卡会被压到屏外（真机 1440p 无此
    # 问题，屏外部分抓屏也只会得到黑块）。此处用同一套 theme 组件单开小窗渲染，
    # 样式与主窗口完全一致，不是另画一张。
    _grab_ranks_card(root)
    _grab_log_card(root)
    root.destroy()


RANKS = [
    {"name": "贪欲束缚", "grade": "S", "reason": "契合出装主线"},
    {"name": "升级: 死亡之舞", "grade": "A", "reason": "开级型: 效果取决于已购装备"},
    {"name": "漏血", "grade": "B", "reason": "泛用"},
]

LOG_LINES = [
    ("10:10:13", "[SESSION] session_reset", "text_dim"),
    ("10:10:13", "[OK] HexARAMHelper 启动 (决策引擎: ai)", "text_dim"),
    ("10:10:13", "[OK] 数据就绪: 16.19.1 | builds: 16.19 | AI: 开", "text_dim"),
    ("10:10:16", "[SESSION] hero_selected 暗裔剑魔", "text_dim"),
    ("10:10:18", "[SESSION] in_progress", "accent"),
    ("10:10:21", "[OK] 对手出装: 荆棘之甲 —— 高护甲反弹伤", "text_dim"),
]


def _grab_log_card(root):
    """日志卡单独出图：主窗口满载内容超出 1080p 屏幕，日志区落在屏幕外
    （PrintWindow 对屏外区域渲染为白 / ImageGrab 渲染为黑），并非控件没上色
    ——隔离探测已确认 tk.Text 本身 bg 为 surface_inset。此处用同一套组件
    单开小窗渲染，样式与主窗口一致，并顺带验证深色 ttk 滚动条。
    """
    from tkinter import ttk as _ttk
    from hexa.ui.theme import Palette, Theme, card_frame
    P = Palette
    mini = tk.Toplevel(root)
    mini.overrideredirect(True)
    mini.configure(bg=P["bg"])
    Theme.style_root(mini)
    box = card_frame(mini, title="日志", pack=dict(padx=10, pady=10))
    body = tk.Frame(box, bg=P["surface_inset"])
    body.pack(fill=tk.BOTH, expand=True)
    txt = tk.Text(body, height=6, bg=P["surface_inset"], fg=P["text_dim"],
                  font=Theme.FONT_MONO, relief=tk.FLAT, highlightthickness=0,
                  bd=0, wrap=tk.WORD, padx=7, pady=5, state=tk.NORMAL)
    txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    bar = _ttk.Scrollbar(body, orient=tk.VERTICAL, command=txt.yview)
    bar.pack(side=tk.RIGHT, fill=tk.Y)
    txt.config(yscrollcommand=bar.set)
    for ts, msg, color in LOG_LINES:
        txt.insert(tk.END, f"[{ts}] {msg}\n", color)
    grab(mini, "ui_v4_log_card.png")


def _grab_ranks_card(root):
    from hexa.ui.theme import (GRADES, Palette, Theme, Type, card_frame,
                               grade_badge, strip_emoji)
    P = Palette
    mini = tk.Toplevel(root)
    mini.overrideredirect(True)      # 绕开窗口管理器的高度限制
    mini.configure(bg=P["bg"])
    Theme.style_root(mini)
    box = card_frame(mini, title="海克斯时刻评级",
                     pack=dict(padx=10, pady=10))
    for i, r in enumerate(RANKS):
        row = tk.Frame(box, bg=P["surface"])
        row.pack(anchor=tk.W, fill=tk.X, pady=2)
        grade_badge(row, r["grade"]).pack(side=tk.LEFT, pady=(1, 0))
        tk.Label(row, text="①②③"[i],
                 font=(Theme.FAMILY, Type["small"], "bold"),
                 bg=P["surface"], fg=P["text_mute"]).pack(side=tk.LEFT, padx=(5, 3))
        tk.Label(row, text=strip_emoji(r["name"]), font=Theme.FONT,
                 bg=P["surface"], fg=GRADES[r["grade"]]).pack(side=tk.LEFT)
        tk.Label(row, text=strip_emoji(r["reason"]),
                 font=(Theme.FAMILY, Type["small"]),
                 bg=P["surface"], fg=P["text_mute"]).pack(side=tk.LEFT, padx=(6, 0))
    grab(mini, "ui_v4_ranks_card.png")


if __name__ == "__main__":
    main()
