"""游戏内常驻信息条（置顶 + 全鼠标穿透 + 紧凑尺寸）

fork 参考项目 main.py OverlayApp 的穿透技术，信息架构按计划 §7：
加点顺序（OP.GG 静态顺序）+ 当前建议 + 已选海克斯台账 +
敌方威胁摘要；海克斯界面出现时顶部高亮三卡片档位（S/A/B）。

紧凑化按 2560x1440 基准设计，按屏幕高度等比缩放（限制 0.7~1.3）。
穿透为完整三标志：WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
（点击与悬停全部落到游戏，且永不抢焦点），在窗口 realize 之后对
GA_ROOT 顶层 HWND 设置（winfo_id 只是 Tk 内层窗口，realize 前的
顶层句柄可能在首次映射时重建导致样式丢失）。
"""
import ctypes
import queue
import tkinter as tk

# ── Win32 扩展样式 ────────────────────────────────────────────
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020       # 鼠标事件全穿透（点击+悬停）
WS_EX_LAYERED = 0x00080000           # 分层窗口（配合 -alpha）
WS_EX_NOACTIVATE = 0x08000000        # 永不抢焦点
GA_ROOT = 2
CLICK_THROUGH_MASK = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE

# ── 紧凑尺寸（2560x1440 基准，原 300 宽 / 760 高上限） ─────────
DESIGN_H = 1440
BASE_WIDTH = 236
BASE_HEIGHT = 560

FG = "#e6edf3"
BG = "#10151c"
FG_DIM = "#8b949e"
ACCENT = "#58a6ff"
WARN = "#d29922"
GOOD = "#3fb950"
GRADE_COLOR = {"S": GOOD, "A": ACCENT, "B": FG, "C": WARN, "?": WARN}


def ui_scale(screen_h: int) -> float:
    """按 1440p 基准的界面缩放比（2560x1440 → 1.0）"""
    return max(0.7, min(1.3, screen_h / DESIGN_H))


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
            self.win.update_idletasks()   # 强制 realize，顶层 HWND 稳定后再设穿透
        self._apply_click_through()
        self.win.after(800, self._apply_click_through)   # 兜底重申（防映射期句柄重建）
        self.win.after(500, self._refresh)

    # ── 窗口技术（fork：无边框+置顶+完整穿透） ─────────────────────

    def _setup_window(self):
        scale = ui_scale(self.win.winfo_screenheight())
        self.w = max(160, round(BASE_WIDTH * scale))
        self.pad_x = max(6, round(8 * scale))
        self.pad_y = max(1, round(2 * scale))
        self.f_hero = max(9, round(11 * scale))   # 原 13 bold
        self.f_body = max(8, round(9 * scale))    # 原 10
        self.f_small = max(7, round(8 * scale))   # 原 9
        self.wrap = self.w - 2 * self.pad_x

        self.win.title("HexaOverlay")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.config(bg=BG)
        # 半透明卡片感：整体 alpha 0.88
        try:
            self.win.attributes("-alpha", 0.88)
        except Exception:
            pass
        # 右侧居中贴边（高度按内容自适应，见 _fit_height；此处为初始/下限值）
        self.win.update_idletasks()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        h = min(round(BASE_HEIGHT * scale), screen_h - 40)
        self._default_h = h        # 内容不足时的高度下限（原固定默认值）
        self._screen_h = screen_h
        self._x = screen_w - self.w - 12
        self._h = h
        self.win.geometry(f"{self.w}x{h}+{self._x}+{(screen_h - h) // 2}")

    def _top_hwnd(self) -> int:
        """真实顶层 HWND：winfo_id 是 Tk 内层窗口，须取 GA_ROOT（回退 GetParent）"""
        try:
            hwnd = ctypes.windll.user32.GetAncestor(self.win.winfo_id(), GA_ROOT)
            if hwnd:
                return hwnd
        except Exception:
            pass
        return ctypes.windll.user32.GetParent(self.win.winfo_id())

    def _apply_click_through(self) -> bool:
        """置 WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_NOACTIVATE（+TOPMOST 呼应置顶）"""
        try:
            hwnd = self._top_hwnd()
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            old = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  old | CLICK_THROUGH_MASK | WS_EX_TOPMOST)
            want = CLICK_THROUGH_MASK | WS_EX_TOPMOST
            return (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & want) == want
        except Exception as e:
            print(f"[WARN] overlay 穿透设置失败: {e}")
            return False

    def _build(self):
        pad = {"padx": self.pad_x, "pady": self.pad_y, "anchor": "w"}
        wrap = self.wrap
        self.lbl_hero = tk.Label(self.win, text="待机中…",
                                 font=("Microsoft YaHei", self.f_hero, "bold"),
                                 bg=BG, fg=ACCENT)
        self.lbl_hero.pack(**pad)
        # 军师提示（v4.5）：顶部显著位置（英雄行正下方），最新一条
        self.lbl_coach = tk.Label(self.win, text="",
                                  font=("Microsoft YaHei", self.f_body, "bold"),
                                  bg=BG, fg=GOOD, justify=tk.LEFT,
                                  wraplength=wrap)
        self._coach_shown = False   # 未 pack；显示时插到路线行之前（置顶区）
        self.lbl_path = tk.Label(self.win, text="", font=("Microsoft YaHei", self.f_body),
                                 bg=BG, fg=FG_DIM)
        self.lbl_path.pack(**pad)
        self.sep(6)

        self.lbl_skill = tk.Label(self.win, text="加点: -",
                                  font=("Microsoft YaHei", self.f_body),
                                  bg=BG, fg=FG_DIM, justify=tk.LEFT, wraplength=wrap)
        self.lbl_skill.pack(**pad)
        self.sep(8)

        # 海克斯时刻（界面出现时高亮三卡片档位 + 识别状态标识）
        self.lbl_moment = tk.Label(self.win, text="",
                                   font=("Microsoft YaHei", self.f_body, "bold"),
                                   bg=BG, fg=ACCENT, justify=tk.LEFT,
                                   wraplength=wrap)
        # 锚点：建议标题（moment / AI 状态显示时插在它之前）
        self._moment_anchor = tk.Label(self.win, text="── 建议 ──",
                                       font=("Microsoft YaHei", self.f_small),
                                       bg=BG, fg=FG_DIM)
        self._moment_anchor.pack(**pad)
        # AI 生成中标识（模型在途时显示，完成即消失）
        self.lbl_ai = tk.Label(self.win, text="⟳ AI 生成中…",
                               font=("Microsoft YaHei", self.f_small),
                               bg=BG, fg=WARN)
        self._ai_shown = False
        self.lbl_moment.pack(**pad)
        self.lbl_moment.pack_forget()  # 默认隐藏，海克斯界面出现时显示
        self.lbl_suggest = tk.Label(self.win, text="对局内自动触发",
                                    font=("Microsoft YaHei", self.f_body),
                                    bg=BG, fg=FG, justify=tk.LEFT, wraplength=wrap)
        self.lbl_suggest.pack(**pad)
        self.sep(8)

        tk.Label(self.win, text="── 海克斯台账 ──",
                 font=("Microsoft YaHei", self.f_small),
                 bg=BG, fg=FG_DIM).pack(**pad)
        self.lbl_augments = tk.Label(self.win, text="-",
                                     font=("Microsoft YaHei", self.f_body),
                                     bg=BG, fg=FG, justify=tk.LEFT, wraplength=wrap)
        self.lbl_augments.pack(**pad)
        self.sep(8)

        tk.Label(self.win, text="── 敌方威胁 ──",
                 font=("Microsoft YaHei", self.f_small),
                 bg=BG, fg=FG_DIM).pack(**pad)
        self.lbl_threat = tk.Label(self.win, text="-",
                                   font=("Microsoft YaHei", self.f_body),
                                   bg=BG, fg=WARN, justify=tk.LEFT, wraplength=wrap)
        self.lbl_threat.pack(**pad)
        # 满装替换预告（威胁 vs 已有装备错配时显示；提示级，不改主建议）
        self.lbl_forecast = tk.Label(self.win, text="",
                                     font=("Microsoft YaHei", self.f_small),
                                     bg=BG, fg=WARN, justify=tk.LEFT,
                                     wraplength=wrap)
        self._forecast_shown = False

    def sep(self, pady):
        tk.Frame(self.win, height=1, bg="#21262d").pack(fill=tk.X,
                                                        padx=self.pad_x, pady=pady)

    # ── 刷新（UI 线程 after 轮询 session 快照） ────────────────────

    def _refresh(self):
        state = self.session.ui_state()
        hero = state["my_hero"] or "待机中…"
        stage = {"idle": "", "champ_select": "·选人", "game_start": "·加载",
                 "in_game": "·对局中"}.get(state["stage"], "")
        self.lbl_hero.config(text=hero + stage)
        self._render_coach(state.get("coach_latest") or "")
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
        # 满六神装替换建议视觉区分：琥珀色（普通"下一件"为常规前景色）
        self.lbl_suggest.config(
            fg=WARN if state.get("suggestion_kind") == "swap_items" else FG)
        self._render_forecast(state.get("swap_forecast") or "")
        self._render_ai_busy(state.get("engine") or {})
        self._render_moment(state)
        self._fit_height()
        self.win.after(500, self._refresh)

    def _fit_height(self):
        """按内容动态调窗高。原固定 min(560*scale, 屏-40) 不随内容伸缩，
        军师/替换预告叠上时刻块+4 行 swap 建议后 1440p 最坏 ~604px 超窗，
        底部"替换预告/敌方威胁"面板被 pack 挤到不可见（winfo_ismapped=False）。
        取内容所需高度与默认值较大者再夹到屏幕内：内容不足不小于原默认，
        高度无变化不重设 geometry（防 500ms 轮询抖动）。"""
        try:
            self.win.update_idletasks()   # 刷新 label 文本/显隐后再量所需高度
            want = self.win.winfo_reqheight()
        except Exception:
            return
        h = min(max(want, self._default_h), self._screen_h - 40)
        if h > 0 and h != self._h:
            self._h = h
            y = max(0, (self._screen_h - h) // 2)
            self.win.geometry(f"{self.w}x{h}+{self._x}+{y}")

    def _render_coach(self, text: str):
        """军师提示行：最新一条显示在顶部（英雄行下、路线行上），空则收起"""
        if text:
            self.lbl_coach.config(text="🧠 军师：" + text)
            if not self._coach_shown:
                # before= 固定插入位置：重新 pack 不会跑到内容区末尾
                self.lbl_coach.pack(before=self.lbl_path,
                                    padx=self.pad_x, pady=self.pad_y,
                                    anchor="w")
                self._coach_shown = True
        elif self._coach_shown:
            self.lbl_coach.pack_forget()
            self._coach_shown = False

    def _render_forecast(self, text: str):
        """替换预告行：有错配提示时显示在威胁块下方，无则收起"""
        if text:
            self.lbl_forecast.config(text="⚠ 替换预告: " + text)
            if not self._forecast_shown:
                self.lbl_forecast.pack(padx=self.pad_x, pady=self.pad_y,
                                       anchor="w")
                self._forecast_shown = True
        elif self._forecast_shown:
            self.lbl_forecast.pack_forget()
            self._forecast_shown = False

    def _render_ai_busy(self, engine_stats):
        """AI 在途生成标识：System1 精修（inflight>0）或 System2 上下文生成中"""
        busy = int(engine_stats.get("inflight") or 0) > 0 or \
            engine_stats.get("ctx_source") == "生成中"
        if busy and not self._ai_shown:
            self.lbl_ai.pack(before=self._moment_anchor,
                             padx=self.pad_x, pady=self.pad_y, anchor="w")
            self._ai_shown = True
        elif not busy and self._ai_shown:
            self.lbl_ai.pack_forget()
            self._ai_shown = False

    def _render_moment(self, state):
        """海克斯时刻：识别状态标识 + 三卡片档位（S 绿 / A 蓝 / B 白 / C 黄）

        状态流转（多局零识别成功曾无从判断，全程可见）：
        界面在屏未识别 → "⏳ 识别中…"；识别成功 → 标题 ✓、点选卡按卡名标 ✓已选
        （AI 精修可能重排三卡，按下标会标错行）；reroll/新轮识别复位 →
        上一轮旧评级直接丢弃并回"识别中"；界面关闭 → 保留上轮 成功✓/失败✗ 一行。
        """
        ranks = state.get("card_ranks") or []
        active = state.get("hex_active", False)
        last = state.get("hex_last", "")
        picked = state.get("hex_picked")
        if active and not state.get("hex_recognized"):
            # reroll/新轮已把识别复位但 card_ranks 未清（game_state 保留旧值），
            # 此时在屏的评级必是上一轮旧卡名，直接丢弃防错位显示
            ranks = []
        if not ranks and not active and not last:
            if self.lbl_moment.winfo_ismapped() or self._moment_shown:
                self.lbl_moment.pack_forget()
                self._moment_shown = False
            return
        if ranks:
            lines = ["── 海克斯时刻 ✓已识别 ──"
                     if state.get("hex_recognized") else "── 海克斯时刻 ──"]
            # "✓已选"按稳定身份（卡名）匹配：hex_picked=(idx, name) 的 name
            # 与评级条目 name 同源于检测卡名，模型重排后仍能标对行
            picked_name = picked[1] if picked and len(picked) > 1 else ""
            for i, r in enumerate(ranks[:3], 1):
                mark = (" ✓已选"
                        if picked_name and r.get("name") == picked_name else "")
                lines.append(f"{'①②③'[i-1]} [{r.get('grade', '?')}] "
                             f"{r.get('name', '?')}  {r.get('reason', '')}{mark}")
            text = "\n".join(lines)
            fg = GRADE_COLOR.get(ranks[0].get("grade", "?"), ACCENT)
        elif active:
            text = "── 海克斯时刻 ──\n⏳ 识别中…（读取三张卡面）"
            fg = ACCENT
        else:
            text = f"海克斯 {last}"
            fg = GOOD if "✓" in last else WARN
        self.lbl_moment.config(text=text, fg=fg)
        if not self._moment_shown:
            # 插到加点行之后（pack 顺序：before=建议标题）
            self.lbl_moment.pack(before=self._moment_anchor,
                                 padx=self.pad_x, pady=self.pad_y, anchor="w")
            self._moment_shown = True

    # ── 显隐控制（计划：仅留一个可选 overlay 显隐键） ───────────────

    def toggle(self):
        if self.win.state() == "withdrawn":
            self.win.deiconify()
            self.win.update_idletasks()
            self._apply_click_through()   # 重新显示后确保穿透仍在
        else:
            self.win.withdraw()
