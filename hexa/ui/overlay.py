"""游戏内常驻信息条（置顶 + 全鼠标穿透 + 紧凑尺寸）

fork 参考项目 main.py OverlayApp 的穿透技术，信息架构按计划 §7：
加点顺序（OP.GG 静态顺序）+ 当前建议 + 已选海克斯台账 + 敌方威胁摘要；
海克斯界面出现时顶部高亮三卡片档位（S/A/B）。

2026-09-30 UI 重构（Quiet Dark v3）——**只换皮肤，骨架不动**：
- 色值全部来自 `hexa.ui.theme`（本文件曾自带一套重复 hex，与 theme 分叉，已删）
- 档位**逐行分色 + 15px 徽章**：旧版用 `ranks[0]` 的档位色给整个多行块上色，
  S/A/B 三行被涂成同一色（实机截图已证实），档位信息视觉失效
- 高度**贴内容**：旧版强撑 560px 下限，1440p 下底部约 130px 死区白占屏幕
  （用户口径「画面优先」：少占屏，而不是把字调淡——alpha 维持 0.88 不动）
- 去 emoji（brain/refresh/hourglass/warning）：彩色字形是干扰游戏元素的主源；
  保留 ①②③ 卡序与 ✓✗ 识别标记（黑白几何字形，渲染稳定）
- 删掉「路线: AD」整行：路线不是 ad/ap/坦克三键而是不同装备玩法，拿伤害
  类型键当"路线"显示既没用又与系统自身的方案制矛盾；`current_path` 仍留在
  引擎里用于选候选与复盘，只是不再显示
- 信息条目、显示顺序、数据源与 `ui_state()` 消费方式一字未改

紧凑化按 2560x1440 基准设计，按屏幕高度等比缩放（限制 0.7~1.3）。
穿透为完整三标志：WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
（点击与悬停全部落到游戏，且永不抢焦点），在窗口 realize 之后对
GA_ROOT 顶层 HWND 设置（winfo_id 只是 Tk 内层窗口，realize 前的
顶层句柄可能在首次映射时重建导致样式丢失）。
"""
import ctypes
import queue
import tkinter as tk

from hexa.ui.theme import (CIRCLED, GRADES, Metrics, PENDING_TEXT, PENDING_WAIT,
                           PICKED_MARK, Palette, Type, grade_badge,
                           ledger_line, left_rail_box, ov_font, retint_badge,
                           rule_row, stage_color, strip_emoji)

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
BASE_HEIGHT = 560      # 量不到内容时的首帧兜底上限（不再是常态下限）
FLOOR_HEIGHT = 150     # 贴内容下限 ≈ 4 行
SHRINK_EPS = 16        # 收缩迟滞：涨立即，缩需 ≥16px（见 fit_height）
SCREEN_MARGIN = 40
BADGE_GAP = 5

# 阶段后缀（沿用旧文案，仅从英雄行拆成独立标签便于分色）
STAGE_SUFFIX = {
    "idle": "", "champ_select": "·选人", "game_start": "·加载",
    "in_game": "·对局中",
}

# 提示语（沿用旧文案，去掉 emoji）
COACH_PREFIX = "军师"
AI_BUSY_TEXT = "AI 生成中…"
DETECT_TEXT = "识别中…（读取三张卡面）"


def ui_scale(screen_h: int) -> float:
    """按 1440p 基准的界面缩放比（2560x1440 → 1.0）"""
    return max(0.7, min(1.3, screen_h / DESIGN_H))


def fit_height(want: int, floor: int, screen_h: int, cur: int) -> int:
    """贴内容 + 抖动抑制（纯函数，可离线断言）。

    旧实现固定 `min(560*scale, 屏-40)`，军师/时刻块叠上后 1440p 最坏 ~604px
    超窗，底部"替换预告/敌方威胁"被挤到不可见；且内容只有 ~430px 时底部
    空出约 130px 死区。这里改为按内容所需高度取 min(max(内容, 下限), 屏-40)；
    军师/识别块进出会让内容忽长忽短，无迟滞则窗口每 500ms 跳一下，故收缩侧
    要求变化 ≥SHRINK_EPS 才生效（涨侧立即——内容多了要马上看得到）。
    """
    target = min(max(int(want), int(floor)), int(screen_h) - SCREEN_MARGIN)
    if cur and target < cur and (cur - target) < SHRINK_EPS:
        return cur
    return target


class Overlay:
    def __init__(self, root: tk.Tk, session, ui_queue: queue.Queue, visible: bool = True):
        self.session = session
        self.ui_queue = ui_queue
        self._moment_shown = False
        self._coach_shown = False
        self._ai_shown = False
        self._forecast_shown = False
        self._last_err = None         # 刷新异常签名（60s 限频去重）
        self._last_err_ts = 0.0
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self._setup_window()
        self._build()
        self._snap_to_content()
        if visible:
            self.win.deiconify()
            self.win.update_idletasks()   # 强制 realize，顶层 HWND 稳定后再设穿透
        self._apply_click_through()
        self.win.after(800, self._apply_click_through)   # 兜底重申（防映射期句柄重建）
        self.win.after(500, self._refresh)

    # ── 窗口技术（fork：无边框+置顶+完整穿透） ─────────────────────

    def _setup_window(self):
        scale = ui_scale(self.win.winfo_screenheight())
        self.scale = scale
        P = Palette
        self.w = max(160, round(BASE_WIDTH * scale))
        self.pad_x = max(6, round(8 * scale))
        self.pad_y = max(1, round(2 * scale))
        self.wrap = self.w - 2 * self.pad_x
        self.wrap_badge = self.wrap - (Metrics["badge"] + BADGE_GAP)

        self.win.title("HexaOverlay")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.config(bg=P["bg"])
        # 半透明卡片感：整体 alpha 0.88（用户口径「画面优先」——少占屏靠
        # 贴内容而非降 alpha，降 alpha 只会牺牲正文可读性）
        try:
            self.win.attributes("-alpha", 0.88)
        except Exception:
            pass
        # 右侧居中贴边（高度按内容自适应，见 _fit_height；此处为初始/下限值）
        self.win.update_idletasks()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        h = min(round(BASE_HEIGHT * scale), screen_h - SCREEN_MARGIN)
        self._floor_h = max(60, min(round(FLOOR_HEIGHT * scale),
                                    screen_h - SCREEN_MARGIN))
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

    # ── 构建（控件一次性建好，500ms 轮询只 config 不重建） ──────────

    def _build(self):
        P = Palette
        s = self.scale
        pad = {"padx": self.pad_x, "pady": self.pad_y, "anchor": "w"}

        # ① 英雄行：名字（主文本）+ 阶段后缀（三级文本），不再整行强调蓝
        hero = tk.Frame(self.win, bg=P["bg"])
        hero.pack(**pad)
        self.lbl_hero = tk.Label(hero, text="待机中…", font=ov_font("ov_hero", True, s),
                                 bg=P["bg"], fg=P["accent"])
        self.lbl_hero.pack(side=tk.LEFT)
        self.lbl_stage = tk.Label(hero, text="", font=ov_font("ov_body", False, s),
                                  bg=P["bg"], fg=P["text_mute"])
        self.lbl_stage.pack(side=tk.LEFT, padx=(4, 0))

        # ② 军师（条件）：三级前缀 + 正文；显示时插在英雄行与加点行之间
        coach = tk.Frame(self.win, bg=P["bg"])
        tk.Label(coach, text=COACH_PREFIX, font=ov_font("ov_meta", False, s),
                 bg=P["bg"], fg=P["text_mute"]).pack(side=tk.LEFT)
        self.lbl_coach = tk.Label(coach, text="", font=ov_font("ov_body", False, s),
                                  bg=P["bg"], fg=P["text"], justify=tk.LEFT,
                                  wraplength=self.wrap)
        self.lbl_coach.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        self._coach = coach

        # ③ 加点顺序
        self.lbl_skill = tk.Label(self.win, text="加点: -",
                                  font=ov_font("ov_meta", False, s),
                                  bg=P["bg"], fg=P["text_mute"], justify=tk.LEFT,
                                  wraplength=self.wrap)
        self.lbl_skill.pack(**pad)

        # ④ 中段容器：海克斯时刻 + AI 在途标识（固定容器 → 两者相对顺序不翻面）
        self._mid = tk.Frame(self.win, bg=P["bg"])
        self._mid.pack(**pad)
        self._mid.grid_columnconfigure(0, weight=1)

        # 海克斯时刻：状态点 + 标题 + 提示 + 三行档位（徽章/卡名/理由）
        moment = tk.Frame(self._mid, bg=P["bg"])
        moment.grid(row=0, column=0, sticky="ew")
        self._moment = moment
        self._moment_title = rule_row(moment, "海克斯时刻", bg=P["bg"],
                                      size=Type["ov_meta"], dot=P["warning"])
        self._moment_title.grid(row=0, column=0, sticky="ew")
        self._moment_body = tk.Label(moment, text="", font=ov_font("ov_body", False, s),
                                     bg=P["bg"], fg=P["text_dim"], justify=tk.LEFT,
                                     wraplength=self.wrap)
        self._moment_body.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._grade_rows = []
        for i in range(3):
            row = tk.Frame(moment, bg=P["bg"])
            badge = grade_badge(row, "")
            badge.pack(side=tk.LEFT, pady=(2, 0))
            col = tk.Frame(row, bg=P["bg"])
            col.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(BADGE_GAP, 0))
            top = tk.Frame(col, bg=P["bg"])
            top.pack(anchor=tk.W, fill=tk.X)
            num = tk.Label(top, text="", font=ov_font("ov_body", True, s),
                           bg=P["bg"], fg=P["text_mute"])
            num.pack(side=tk.LEFT, padx=(0, 3))
            name = tk.Label(top, text="", font=ov_font("ov_body", True, s),
                            bg=P["bg"], fg=P["text"], justify=tk.LEFT,
                            wraplength=self.wrap_badge)
            name.pack(side=tk.LEFT, fill=tk.X, expand=True)
            pick = tk.Label(top, text="", font=ov_font("ov_meta", False, s),
                            bg=P["bg"], fg=P["success"])
            pick.pack(side=tk.RIGHT, padx=(4, 0))
            reason = tk.Label(col, text="", font=ov_font("ov_meta", False, s),
                              bg=P["bg"], fg=P["text_mute"], justify=tk.LEFT,
                              wraplength=self.wrap_badge)
            reason.pack(anchor=tk.W, fill=tk.X)
            row.grid(row=2 + i, column=0, sticky="ew", pady=(3, 0))
            row.grid_remove()
            self._grade_rows.append({"row": row, "badge": badge, "num": num,
                                     "name": name, "pick": pick, "reason": reason})
        self._moment_shown = False

        # AI 在途标识（模型在途时显示，完成即消失）
        ai = tk.Frame(self._mid, bg=P["bg"])
        ai.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        tk.Frame(ai, width=5, height=5, bg=P["warning"]).pack(
            side=tk.LEFT, padx=(0, 6), pady=4)
        tk.Label(ai, text=AI_BUSY_TEXT, font=ov_font("ov_meta", False, s),
                 bg=P["bg"], fg=P["text_mute"]).pack(side=tk.LEFT)
        self._ai = ai
        self._ai.grid_remove()
        self._ai_shown = False

        # ⑤ 建议：2px 左轨（普通强调色 / 满装替换琥珀色），正文恒为主文本
        self._suggest = tk.Frame(self.win, bg=P["bg"])
        self._suggest.pack(**pad)
        rule_row(self._suggest, "建议", bg=P["bg"]).grid(
            row=0, column=0, sticky="ew")
        self._sug_outer, self._sug_box = left_rail_box(self._suggest, P["accent"])
        self._sug_outer.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.lbl_suggest = tk.Label(self._sug_box, text="对局内自动触发",
                                    font=ov_font("ov_body", False, s),
                                    bg=P["surface_inset"], fg=P["text"],
                                    justify=tk.LEFT, wraplength=self.wrap - 18)
        self.lbl_suggest.pack(anchor=tk.W)

        # ⑥ 海克斯台账
        ledger = tk.Frame(self.win, bg=P["bg"])
        ledger.pack(**pad)
        rule_row(ledger, "海克斯台账", bg=P["bg"]).grid(
            row=0, column=0, sticky="ew")
        self.lbl_augments = tk.Label(ledger, text="-", font=ov_font("ov_body", False, s),
                                     bg=P["bg"], fg=P["text_dim"], justify=tk.LEFT,
                                     wraplength=self.wrap)
        self.lbl_augments.grid(row=1, column=0, sticky="ew", pady=(3, 0))

        # ⑦ 敌方威胁（标题保持中性，语义色留给档位与真正的告警行）
        threat = tk.Frame(self.win, bg=P["bg"])
        threat.pack(**pad)
        rule_row(threat, "敌方威胁", bg=P["bg"]).grid(
            row=0, column=0, sticky="ew")
        self.lbl_threat = tk.Label(threat, text="-", font=ov_font("ov_body", False, s),
                                   bg=P["bg"], fg=P["text_dim"], justify=tk.LEFT,
                                   wraplength=self.wrap)
        self.lbl_threat.grid(row=1, column=0, sticky="ew", pady=(3, 0))

        # ⑧ 满装替换预告（告警级，条件显示在末尾）
        self.lbl_forecast = tk.Label(self.win, text="", font=ov_font("ov_meta", False, s),
                                     bg=P["bg"], fg=P["warning"], justify=tk.LEFT,
                                     wraplength=self.wrap)
        self._forecast_shown = False

    # ── 刷新（UI 线程 after 轮询 session 快照） ────────────────────

    def _refresh(self):
        # 异常不冒出（同主窗口 _poll_ui 的教训）：异常逃出会让本 after 链
        # 不再续约，浮窗内容永久冻结（F10 显隐走 ui_queue 仍活着，形成
        # "能开关但内容不更新"的隐性坏死）。持续异常按 60s 限频记日志。
        try:
            self._refresh_once()
        except Exception as e:
            sig = repr(e)
            now = time.monotonic()
            if sig != self._last_err or now - self._last_err_ts >= 60:
                self._last_err = sig
                self._last_err_ts = now
                print(f"[ERR] overlay 刷新异常（下一帧自动恢复）: "
                      f"{type(e).__name__}: {e}")
        finally:
            self.win.after(500, self._refresh)

    def _refresh_once(self):
        P = Palette
        state = self.session.ui_state()
        stage = state["stage"]
        hero = state["my_hero"]
        # 等级直出（快照免费事实）：英雄名后缀 Lv.N，无数据（0）省略
        level = state.get("my_level") or 0
        if hero:
            hero_text = f"{hero} Lv.{level}" if level else hero
            self.lbl_hero.config(text=hero_text, fg=P["text"])
            self.lbl_stage.config(text=STAGE_SUFFIX.get(stage, ""),
                                  fg=stage_color(stage))
        else:
            self.lbl_hero.config(text="待机中…", fg=P["accent"])
            self.lbl_stage.config(text=STAGE_SUFFIX.get(stage, ""),
                                  fg=stage_color(stage))
        self._render_coach(state.get("coach_latest") or "")
        self.lbl_skill.config(text=f"加点: {state.get('skill_order') or '-'}")
        # 台账带品质：『① 贪欲束缚（棱彩）』，tier 缺省省略括号
        self.lbl_augments.config(
            text="\n".join(
                ledger_line(i, a.get("name", "?"), a.get("tier") or "")
                for i, a in enumerate(state["augments"])) or "-")
        self.lbl_threat.config(text=strip_emoji(state["threat"]) or "-")
        # 满六神装替换建议：左轨转琥珀（正文恒为主文本，多行琥珀字可读性差）
        swap = state.get("suggestion_kind") == "swap_items"
        self._sug_outer.configure(bg=P["warning"] if swap else P["accent"])
        self.lbl_suggest.config(text=strip_emoji(state["suggestion"]) or "对局内自动触发")
        self._render_forecast(state.get("swap_forecast") or "")
        self._render_ai_busy(state.get("engine") or {})
        self._render_moment(state)
        self._fit_height()

    def _snap_to_content(self):
        """首帧贴内容：build 完直接量所需高度，避免先撑 560 再收缩的跳变。"""
        try:
            self.win.update_idletasks()
            want = self.win.winfo_reqheight()
        except Exception:
            return
        h = fit_height(want, self._floor_h, self._screen_h, 0)
        if h > 0 and h != self._h:
            self._h = h
            y = max(0, (self._screen_h - h) // 2)
            self.win.geometry(f"{self.w}x{h}+{self._x}+{y}")

    def _fit_height(self):
        """按内容动态调窗高（纯函数 fit_height：贴内容 + 收缩迟滞）"""
        try:
            self.win.update_idletasks()   # 刷新 label 文本/显隐后再量所需高度
            want = self.win.winfo_reqheight()
        except Exception:
            return
        h = fit_height(want, self._floor_h, self._screen_h, self._h)
        if h > 0 and h != self._h:
            self._h = h
            y = max(0, (self._screen_h - h) // 2)
            self.win.geometry(f"{self.w}x{h}+{self._x}+{y}")

    def _render_coach(self, text: str):
        """军师提示行：最新一条显示在顶部（英雄行下、加点行上），空则收起"""
        text = strip_emoji(text)
        if text:
            self.lbl_coach.config(text=text)
            if not self._coach_shown:
                # before= 固定插入位置：重新 pack 不会跑到内容区末尾
                self._coach.pack(before=self.lbl_skill,
                                 padx=self.pad_x, pady=self.pad_y, anchor="w")
                self._coach_shown = True
        elif self._coach_shown:
            self._coach.pack_forget()
            self._coach_shown = False

    def _render_forecast(self, text: str):
        """替换预告行：有错配提示时显示在威胁块下方，无则收起"""
        text = strip_emoji(text)
        if text:
            self.lbl_forecast.config(text="替换预告: " + text)
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
            self._ai.grid()
            self._ai_shown = True
        elif not busy and self._ai_shown:
            self._ai.grid_remove()
            self._ai_shown = False

    def _render_moment(self, state):
        """海克斯时刻：状态点 + 三行档位徽章（逐行分色）

        状态流转（多局零识别成功曾无从判断，全程可见）：
        界面在屏未识别 → "识别中…"；识别成功 → 标题 ✓、三卡上屏
        （AI 精修在途时只显卡名 + "AI 评级中…"，档位等精修回调后再放，
        2026-09-30：此前规则基准档顶着评级直接上屏，用户易当成 AI 结论）；
        点选卡按卡名标 ✓已选（AI 精修可能重排三卡，按下标会标错行）；
        reroll/新轮识别复位 → 上一轮旧评级直接丢弃并回"识别中"；
        界面关闭 → 保留上轮 成功✓/失败✗ 一行。

        2026-09-30：旧版把整个多行块涂成 `ranks[0]` 的档位色 → S/A/B 三行同色，
        档位信息在游戏里等于失效；现改为逐行按自身档位着色 + 15px 徽章。
        """
        ranks = state.get("card_ranks") or []
        pending = bool(state.get("card_ranks_pending"))
        active = bool(state.get("hex_active", False))
        last = state.get("hex_last", "") or ""
        picked = state.get("hex_picked")
        if active and not state.get("hex_recognized"):
            # reroll/新轮已把识别复位但 card_ranks 未清（game_state 保留旧值），
            # 此时在屏的评级必是上一轮旧卡名，直接丢弃防错位显示
            ranks = []
        if not ranks and not active and not last:
            self._hide_moment()
            return
        picked_name = picked[1] if picked and len(picked) > 1 else ""
        P = Palette
        if ranks:
            recognized = bool(state.get("hex_recognized"))
            if pending:
                self._show_moment("海克斯时刻 ✓已识别", P["accent"], PENDING_TEXT)
            else:
                self._show_moment(
                    "海克斯时刻 ✓已识别" if recognized else "海克斯时刻",
                    P["success"] if recognized else P["warning"], "")
            for i in range(3):
                self._render_grade_row(i, ranks[i] if i < len(ranks) else None,
                                       pending, picked_name)
        elif active:
            self._show_moment("海克斯时刻", P["warning"], DETECT_TEXT)
            self._clear_grade_rows()
        else:
            self._show_moment("海克斯时刻",
                              P["success"] if "✓" in last else P["warning"],
                              strip_emoji(last))
            self._clear_grade_rows()

    def _show_moment(self, title, dot, body):
        self._moment_title.rule_label.config(text=title)
        if self._moment_title.rule_dot is not None:
            self._moment_title.rule_dot.config(bg=dot)
        self._moment_body.config(text=body)
        if not self._moment_shown:
            self._moment.grid()
            self._moment_shown = True

    def _hide_moment(self):
        if self._moment_shown:
            self._moment.grid_remove()
            self._moment_shown = False

    def _clear_grade_rows(self):
        for w in self._grade_rows:
            w["row"].grid_remove()

    def _render_grade_row(self, index, entry, pending, picked_name):
        w = self._grade_rows[index]
        if entry is None:
            w["row"].grid_remove()
            return
        grade = "" if pending else (entry.get("grade") or "?")
        retint_badge(w["badge"], grade)
        w["num"].config(text=CIRCLED[index])
        w["name"].config(text=strip_emoji(entry.get("name", "?")),
                         fg=GRADES.get(grade) or Palette["text"])
        w["reason"].config(text=PENDING_WAIT if pending
                           else strip_emoji(entry.get("reason", "") or ""))
        # "✓已选"按稳定身份（卡名）匹配：hex_picked=(idx, name) 的 name
        # 与评级条目 name 同源于检测卡名，模型重排后仍能标对行
        w["pick"].config(text=PICKED_MARK
                         if picked_name and entry.get("name") == picked_name else "")
        w["row"].grid()

    # ── 显隐控制（计划：仅留一个可选 overlay 显隐键） ───────────────

    def toggle(self):
        if self.win.state() == "withdrawn":
            self.win.deiconify()
            self.win.update_idletasks()
            self._apply_click_through()   # 重新显示后确保穿透仍在
        else:
            self.win.withdraw()
