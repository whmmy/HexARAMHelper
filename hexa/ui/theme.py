"""UI 主题与日志重定向（fork 自参考项目 gui_launcher.py）"""
import datetime
import io
import tkinter as tk
from tkinter import ttk


class Theme:
    """GitHub Dark 配色常量（单一来源）"""
    BG          = "#0d1117"
    BG_CARD     = "#161b22"
    BG_INPUT    = "#0d1117"
    ACCENT      = "#58a6ff"
    ACCENT_HVR  = "#79c0ff"
    SUCCESS     = "#3fb950"
    WARNING     = "#d29922"
    ERROR       = "#f85149"
    TEXT        = "#e6edf3"
    TEXT_DIM    = "#8b949e"
    BORDER      = "#30363d"

    FONT      = ("Microsoft YaHei", 10)
    FONT_BOLD = ("Microsoft YaHei", 10, "bold")
    FONT_MONO = ("Consolas", 9)

    @classmethod
    def style_root(cls, root: tk.Tk):
        root.configure(bg=cls.BG)
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=cls.BG, foreground=cls.TEXT, font=cls.FONT)
        style.configure("TFrame", background=cls.BG)
        style.configure("Card.TFrame", background=cls.BG_CARD)
        style.configure("TLabel", background=cls.BG, foreground=cls.TEXT)
        style.configure("Dim.TLabel", background=cls.BG, foreground=cls.TEXT_DIM)
        style.configure("Accent.TLabel", background=cls.BG, foreground=cls.ACCENT)
        style.configure("Warn.TLabel", background=cls.BG, foreground=cls.WARNING)
        style.configure("TButton", background=cls.BG_CARD, foreground=cls.TEXT,
                        borderwidth=1, focusthickness=1, font=cls.FONT)
        style.map("TButton", background=[("active", "#21262d")])
        style.configure("TEntry", fieldbackground=cls.BG_INPUT, foreground=cls.TEXT)
        style.configure("TScrollbar", background=cls.BG_CARD)


class LogRedirector(io.TextIOBase):
    """stdout/stderr → Queue（GUI 日志面板），同时保留原流输出。

    tee_path 可选：同步追加落盘（log/app_启动时刻.log）。控制台窗口一关
    输出全丢，真机排障（如 2026-09-29 台账复发案）必须有事后可查的留档；
    多写入方（stdout/stderr 两个重定向实例）共享同一文件句柄，append 模式
    + 逐条小写入，无需加锁交叉也不会撕裂单行（单次 write 原子性足够）。"""

    def __init__(self, log_queue, original_stream=None, tee_path=None):
        super().__init__()
        self.log_queue = log_queue
        self.original = original_stream
        self._tee = None
        if tee_path is not None:
            try:
                self._tee = open(tee_path, "a", encoding="utf-8")
            except Exception:
                self._tee = None   # 留档失败不阻塞日志主链路

    def write(self, text):
        if text and text.strip():
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_queue.put(f"[{ts}] {text.rstrip()}")
        if self.original:
            try:
                self.original.write(text)
                self.original.flush()
            except Exception:
                pass
        if self._tee and text:
            try:
                self._tee.write(text)
                self._tee.flush()
            except Exception:
                pass
        return len(text) if text else 0

    def flush(self):
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass
