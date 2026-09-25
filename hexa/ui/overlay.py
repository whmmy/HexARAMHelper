"""游戏内常驻 overlay：右侧窄条（置顶 + 全鼠标穿透）

fork 参考项目 main.py OverlayApp 的穿透技术（WS_EX_LAYERED|WS_EX_TRANSPARENT），
信息架构按计划 §7：加点顺序（OP.GG 静态顺序）+ 当前建议 + 已选海克斯台账 +
敌方威胁摘要；海克斯界面出现时顶部高亮三卡片档位（S/A/B）。
"""
import ctypes
import queue
import tkinter as tk

WIDTH = 300
FG = "#e6edf3"
BG = "#10151c"
FG_DIM = "#8b949e"
ACCENT = "#58a6ff"
WARN = "#d29922"
GOOD = "#3fb950"
GRADE_COLOR = {"S": GOOD, "A": ACCENT, "B": FG, "C": WARN, "?": WARN}


class Overlay:
    def __init__(self, root: tk.Tk, session, ui_queue: queue.Queue, visible: bool = True):
        self.session = session
        self.ui_queue = ui_queue
        self._moment_shown = False
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self._setup_window()
        self._build()
        if visible:
            self.win.deiconify()
        self.win.after(500, self._refresh)

    # ── 窗口技术（fork：无边框+置顶+穿透） ─────────────────────────

    def _setup_window(self):
        self.win.title("HexaOverlay")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.config(bg=BG)
        # 半透明卡片感：整体 alpha 0.88
        try:
            self.win.attributes("-alpha", 0.88)
        except Exception:
            pass
        try:
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
            old = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            # WS_EX_LAYERED | WS_EX_TRANSPARENT —— 鼠标全穿透（P0 只读展示）
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, old | 0x80000 | 0x20)
        except Exception as e:
            print(f"[WARN] overlay 穿透设置失败: {e}")
        # 右侧居中贴边（高度按内容上限；时刻块出现时内容变长，留足空间）
        self.win.update_idletasks()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        h = min(760, screen_h - 60)
        self.win.geometry(f"{WIDTH}x{h}+{screen_w - WIDTH - 12}+{(screen_h - h) // 2}")

    def _build(self):
        pad = {"padx": 12, "pady": 4, "anchor": "w"}
        self.lbl_hero = tk.Label(self.win, text="待机中…", font=("Microsoft YaHei", 13, "bold"),
                                 bg=BG, fg=ACCENT)
        self.lbl_hero.pack(**pad)
        self.lbl_path = tk.Label(self.win, text="", font=("Microsoft YaHei", 10),
                                 bg=BG, fg=FG_DIM)
        self.lbl_path.pack(**pad)
        self.sep(8)

        self.lbl_skill = tk.Label(self.win, text="加点: -", font=("Microsoft YaHei", 10),
                                  bg=BG, fg=FG_DIM, justify=tk.LEFT, wraplength=WIDTH - 24)
        self.lbl_skill.pack(**pad)
        self.sep(12)

        # 海克斯时刻（界面出现时高亮三卡片档位）
        self.lbl_moment = tk.Label(self.win, text="", font=("Microsoft YaHei", 10, "bold"),
                                   bg=BG, fg=ACCENT, justify=tk.LEFT,
                                   wraplength=WIDTH - 24)
        # 锚点：建议标题（moment 显示时插在它之前）
        self._moment_anchor = tk.Label(self.win, text="── 建议 ──",
                                       font=("Microsoft YaHei", 9),
                                       bg=BG, fg=FG_DIM)
        self._moment_anchor.pack(**pad)
        self.lbl_moment.pack(**pad)
        self.lbl_moment.pack_forget()  # 默认隐藏，海克斯界面出现时显示
        self.lbl_suggest = tk.Label(self.win, text="对局内自动触发", font=("Microsoft YaHei", 10),
                                    bg=BG, fg=FG, justify=tk.LEFT, wraplength=WIDTH - 24)
        self.lbl_suggest.pack(**pad)
        self.sep(12)

        tk.Label(self.win, text="── 海克斯台账 ──", font=("Microsoft YaHei", 9),
                 bg=BG, fg=FG_DIM).pack(**pad)
        self.lbl_augments = tk.Label(self.win, text="-", font=("Microsoft YaHei", 10),
                                     bg=BG, fg=FG, justify=tk.LEFT, wraplength=WIDTH - 24)
        self.lbl_augments.pack(**pad)
        self.sep(12)

        tk.Label(self.win, text="── 敌方威胁 ──", font=("Microsoft YaHei", 9),
                 bg=BG, fg=FG_DIM).pack(**pad)
        self.lbl_threat = tk.Label(self.win, text="-", font=("Microsoft YaHei", 10),
                                   bg=BG, fg=WARN, justify=tk.LEFT, wraplength=WIDTH - 24)
        self.lbl_threat.pack(**pad)

    def sep(self, pady):
        tk.Frame(self.win, height=1, bg="#21262d").pack(fill=tk.X, padx=12, pady=pady)

    # ── 刷新（UI 线程 after 轮询 session 快照） ────────────────────

    def _refresh(self):
        state = self.session.ui_state()
        hero = state["my_hero"] or "待机中…"
        stage = {"idle": "", "champ_select": "·选人", "game_start": "·加载",
                 "in_game": "·对局中"}.get(state["stage"], "")
        self.lbl_hero.config(text=hero + stage)
        path = state["current_path"].upper()
        if state["path_drift"]:
            path += "（装备偏离已重定）" if path else ""
        self.lbl_path.config(text=f"路线: {path or '-'}")
        skill = state.get("skill_order") or "-"
        self.lbl_skill.config(text=f"加点: {skill}")
        augs = state["augments"]
        self.lbl_augments.config(
            text="\n".join(f"{i+1}. {a}" for i, a in enumerate(augs)) or "-")
        self.lbl_threat.config(text=state["threat"] or "-")
        self.lbl_suggest.config(text=state["suggestion"] or "对局内自动触发")
        self._render_moment(state.get("card_ranks") or [])
        self.win.after(500, self._refresh)

    def _render_moment(self, ranks):
        """海克斯时刻：三卡片档位高亮（S 绿 / A 蓝 / B 白 / C 黄）"""
        if not ranks:
            if self.lbl_moment.winfo_ismapped() or self._moment_shown:
                self.lbl_moment.pack_forget()
                self._moment_shown = False
            return
        lines = ["── 海克斯时刻 ──"]
        for i, r in enumerate(ranks[:3], 1):
            lines.append(f"{'①②③'[i-1]} [{r.get('grade', '?')}] {r.get('name', '?')}"
                         f"  {r.get('reason', '')}")
        self.lbl_moment.config(text="\n".join(lines),
                               fg=GRADE_COLOR.get(
                                   ranks[0].get("grade", "?"), ACCENT))
        if not self._moment_shown:
            # 插到加点行之后（pack 顺序：before=建议标题）
            self.lbl_moment.pack(before=self._moment_anchor, padx=12, pady=4,
                                 anchor="w")
            self._moment_shown = True

    # ── 显隐控制（计划：仅留一个可选 overlay 显隐键） ───────────────

    def toggle(self):
        if self.win.state() == "withdrawn":
            self.win.deiconify()
        else:
            self.win.withdraw()
