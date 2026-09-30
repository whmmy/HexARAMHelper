"""主窗口：状态/阵容/建议/成型度对比/台账(可纠错)/数据新鲜度/AI 仪表/日志

线程模型：后台线程（PhaseMachine/LivePoller/检测器）→ ui_queue → after 轮询。
ui_queue 既承载事件字符串也承载可调用对象（跨线程 UI 调度的统一出口）。
"""
import queue
import time
import tkinter as tk
from tkinter import ttk, scrolledtext

from hexa.ui.theme import Theme

STAGE_CN = {
    "idle": "待机", "champ_select": "选人中", "game_start": "加载中", "in_game": "对局中",
}


class MainWindow:
    def __init__(self, root: tk.Tk, session, ui_queue: queue.Queue,
                 log_queue: queue.Queue, on_open_settings=None,
                 on_calibrate=None):
        self.root = root
        self.session = session
        self.ui_queue = ui_queue
        self.log_queue = log_queue
        self.on_open_settings = on_open_settings
        self.on_calibrate = on_calibrate
        self._ledger_cache = None      # 台账按钮去重重建
        self._bp_cache = None          # 成型度面板去重重建（同台账惯例）
        self._last_poll_err = None     # 轮询异常签名（限频去重，防 500ms 刷屏）
        self._last_poll_err_ts = 0.0
        self._build()
        self.root.after(100, self._poll_ui)
        self.root.after(200, self._poll_log)

    # ── 构建 ──────────────────────────────────────────────────────

    def _build(self):
        self.root.title("HexARAMHelper 海克斯大乱斗助手")
        # 默认高度兜底 1150：成型度/预告/军师面板就位后满载内容实测约 1121px，
        # 旧值 760 只装得下空面板，日志会被 expand 挤到 1px（构建完按请求高度再校准）
        self.root.geometry("560x1150")
        self.root.minsize(480, 560)
        Theme.style_root(self.root)
        # 成型度进度条配色（就地配置：theme.py 属公共层，随面板扩展再上收）
        ttk.Style(self.root).configure(
            "BP.Horizontal.TProgressbar", background=Theme.SUCCESS,
            troughcolor=Theme.BG, bordercolor=Theme.BORDER,
            lightcolor=Theme.SUCCESS, darkcolor=Theme.SUCCESS)

        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 日志（排障唯一出口）：side=BOTTOM 最先入包 → pack 先到先得，空间分配
        # 优先级最高；窗口高度不足时宁可裁剪上方卡片，日志也保有 8 行请求高度
        logcard = ttk.Frame(main)
        logcard.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, pady=(8, 0))
        ttk.Label(logcard, text="日志", style="Dim.TLabel").pack(anchor=tk.NW)
        self.txt_log = scrolledtext.ScrolledText(
            logcard, height=8, bg=Theme.BG_CARD, fg=Theme.TEXT_DIM,
            font=Theme.FONT_MONO, relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # 状态行
        status = ttk.Frame(main)
        status.pack(fill=tk.X)
        self.lbl_stage = ttk.Label(status, text="阶段: 待机", style="Accent.TLabel",
                                   font=Theme.FONT_BOLD)
        self.lbl_stage.pack(side=tk.LEFT)
        self.lbl_hero = ttk.Label(status, text="英雄: -")
        self.lbl_hero.pack(side=tk.LEFT, padx=(16, 0))
        self.lbl_path = ttk.Label(status, text="路线: -")
        self.lbl_path.pack(side=tk.LEFT, padx=(16, 0))
        if self.on_open_settings:
            ttk.Button(status, text="⚙ 设置", command=self.on_open_settings
                       ).pack(side=tk.RIGHT)
        if self.on_calibrate:
            ttk.Button(status, text="开始标定", command=self.on_calibrate
                       ).pack(side=tk.RIGHT, padx=(0, 8))

        # 手动输英雄兜底行
        manual = ttk.Frame(main)
        manual.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(manual, text="手动设英雄(兜底):", style="Dim.TLabel").pack(side=tk.LEFT)
        self.ent_hero = ttk.Entry(manual, width=16)
        self.ent_hero.pack(side=tk.LEFT, padx=6)
        ttk.Button(manual, text="设置", command=self._on_manual_hero).pack(side=tk.LEFT)
        self.lbl_manual_hint = ttk.Label(manual, text="", style="Dim.TLabel")
        self.lbl_manual_hint.pack(side=tk.LEFT, padx=8)

        # 阵容卡片
        teams = ttk.Frame(main)
        teams.pack(fill=tk.X, pady=(10, 0))
        self.lbl_ally = ttk.Label(teams, text="己方: -", justify=tk.LEFT, wraplength=250)
        self.lbl_ally.grid(row=0, column=0, sticky=tk.NW, padx=(0, 8))
        self.lbl_enemy = ttk.Label(teams, text="敌方: -", justify=tk.LEFT, wraplength=250)
        self.lbl_enemy.grid(row=0, column=1, sticky=tk.NW)
        teams.columnconfigure(0, weight=1)
        teams.columnconfigure(1, weight=1)

        # 威胁 + 建议 + 替换预告 + 军师滚动列表 + 成型度对比 + 台账(纠错)卡片
        self.card_threat = self._card(main, "敌方威胁画像")
        self.card_suggest = self._card(main, "当前建议")
        self.card_forecast = self._card(main, "满装替换预告")
        self.card_coach = self._card(main, "军师提示（最近 3 条）")
        # 成型度对比（v4.6）：敌我十人六神装进度（传说件 x/6 + 装备估值）
        bp_frame = ttk.Frame(main, style="Card.TFrame", padding=8)
        bp_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(bp_frame, text="成型度对比（传说件 x/6 · 装备估值）",
                  style="Dim.TLabel").pack(anchor=tk.NW)
        self.bp_box = ttk.Frame(bp_frame, style="Card.TFrame")
        self.bp_box.pack(anchor=tk.NW, pady=(4, 0), fill=tk.X)
        ledger_frame = ttk.Frame(main, style="Card.TFrame", padding=8)
        ledger_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(ledger_frame, text="海克斯台账（点名字撤销纠错）",
                  style="Dim.TLabel").pack(anchor=tk.NW)
        self.ledger_box = ttk.Frame(ledger_frame, style="Card.TFrame")
        self.ledger_box.pack(anchor=tk.NW, pady=(4, 0), fill=tk.X)
        self.card_ranks = self._card(main, "海克斯时刻评级")

        # 数据新鲜度（版本翻译器底线：抓取日期常显 + >7 天黄标）
        fresh = ttk.Frame(main)
        fresh.pack(fill=tk.X, pady=(8, 0))
        self.lbl_fresh = ttk.Label(fresh, text="数据版本: -", style="Dim.TLabel")
        self.lbl_fresh.pack(side=tk.LEFT)
        self.lbl_engine = ttk.Label(fresh, text="AI: -", style="Dim.TLabel")
        self.lbl_engine.pack(side=tk.RIGHT)

        # 构建完按实际内容请求高度校准默认窗口（后续面板扩展自动适配，不低于兜底值）
        self.root.update_idletasks()
        self.root.geometry(f"560x{max(1150, self.root.winfo_reqheight())}")

    def _card(self, parent, title) -> ttk.Label:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=8)
        frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(frame, text=title, style="Dim.TLabel").pack(anchor=tk.NW)
        body = ttk.Label(frame, text="-", justify=tk.LEFT, wraplength=500)
        body.pack(anchor=tk.NW, pady=(4, 0))
        return body

    # ── 交互 ──────────────────────────────────────────────────────

    def _on_manual_hero(self):
        query = self.ent_hero.get().strip()
        if not query:
            return
        result = self.session.set_hero_manual(query)
        if result:
            self.lbl_manual_hint.config(text=f"✅ {result}", foreground=Theme.SUCCESS)
            self.ent_hero.delete(0, tk.END)
        else:
            self.lbl_manual_hint.config(text="❌ 未匹配到英雄", foreground=Theme.ERROR)

    # ── 轮询渲染 ──────────────────────────────────────────────────

    def _poll_ui(self):
        try:
            try:
                while True:
                    item = self.ui_queue.get_nowait()
                    if callable(item):
                        try:
                            item()
                        except Exception as e:
                            print(f"[WARN] UI 调度异常: {e}")
            except queue.Empty:
                pass
            state = self.session.ui_state()
            self.lbl_stage.config(text=f"阶段: {STAGE_CN.get(state['stage'], state['stage'])}")
            self.lbl_hero.config(text=f"英雄: {state['my_hero'] or '-'}")
            path_txt = state['current_path'].upper() or "-"
            if state['path_drift']:
                path_txt += " ⚠装备偏离，已按实际出装重定"
            self.lbl_path.config(text=f"路线: {path_txt}")
            self.lbl_ally.config(text="己方: " + (" ".join(state['ally']) or "-"))
            self.lbl_enemy.config(text="敌方: " + (" ".join(state['enemy']) or "-"))
            self.card_threat.config(text=state['threat'] or "-")
            # 满六神装替换建议视觉区分：琥珀色（普通"下一件"为默认前景色）
            self.card_suggest.config(
                text=state['suggestion'] or "-（对局内触发时给出）",
                foreground=(Theme.WARNING
                            if state.get('suggestion_kind') == 'swap_items'
                            else Theme.TEXT))
            # 替换预告（提示级不改主建议）：威胁与已有装备错配时给出方向
            self.card_forecast.config(
                text=state.get('swap_forecast')
                or "-（威胁与出装明显错配时提示）",
                foreground=Theme.WARNING if state.get('swap_forecast')
                else Theme.TEXT_DIM)
            # 军师提示滚动列表（v4.5，最多 3 条，最新在下）
            coach = state.get('coach') or []
            self.card_coach.config(
                text="\n".join(
                    f"{time.strftime('%H:%M:%S', time.localtime(c['ts']))}"
                    f" 🧠 {c['text']}" for c in coach)
                or "-（对局内自动生成：阵容缺口/顺逆风/攒钱与替换提醒）")
            self._render_ledger(state['augments'])
            self._render_build_progress(state.get('build_progress'))
            self._render_ranks(state.get('card_ranks') or [],
                               bool(state.get('card_ranks_pending')))
            self._render_freshness(state)
            self._render_engine(state.get('engine') or {})
        except Exception as e:
            # 渲染异常只记日志不冒出：异常冒出会让本 after 链不再续约，
            # 状态卡/评级/仪表全冻结且 ui_queue 无人消费（F10 显隐、托盘退出失效）。
            # 同一异常持续发生按 60s 限频，防每帧刷屏淹没排障信息。
            sig = repr(e)
            now = time.monotonic()
            if sig != self._last_poll_err or now - self._last_poll_err_ts >= 60:
                self._last_poll_err = sig
                self._last_poll_err_ts = now
                print(f"[ERR] 主窗口轮询异常（下一帧自动恢复）: "
                      f"{type(e).__name__}: {e}")
        finally:
            self.root.after(500, self._poll_ui)

    def _render_ledger(self, augments):
        """台账纠错：点名字撤销（列表变化才重建，防每 500ms 重建闪烁）"""
        key = tuple(augments)
        if key == self._ledger_cache:
            return
        self._ledger_cache = key
        for w in self.ledger_box.winfo_children():
            w.destroy()
        if not augments:
            ttk.Label(self.ledger_box, text="-（游戏内海克斯界面点击后自动记录）",
                      style="Dim.TLabel").pack(anchor=tk.NW)
            return
        for name in augments:
            row = ttk.Frame(self.ledger_box, style="Card.TFrame")
            row.pack(anchor=tk.NW, fill=tk.X, pady=1)
            ttk.Label(row, text=f"· {name}").pack(side=tk.LEFT)
            ttk.Button(row, text="撤销", width=5,
                       command=lambda n=name: self.session.revoke_augment(n)
                       ).pack(side=tk.RIGHT)

    def _render_build_progress(self, bp):
        """成型度对比（v4.6）：敌我两列，每行 英雄名 + 进度条 + x/6 + 估值。

        行由 game_state 按成型度降序排好；数据变化才重建（台账同款缓存
        惯例，防 500ms 轮询重建闪烁）。bp=None（快照未到/装备数据缺失）
        → 占位行不报错。
        """
        key = ((tuple((r["name"], r["legendary"], r["gold"])
                      for r in bp["ally"])),
               (tuple((r["name"], r["legendary"], r["gold"])
                      for r in bp["enemy"]))) if bp else None
        if key == self._bp_cache:
            return
        self._bp_cache = key
        for w in self.bp_box.winfo_children():
            w.destroy()
        if bp is None:
            ttk.Label(self.bp_box, text="-（对局内快照同步后显示双方成型度）",
                      style="Dim.TLabel").pack(anchor=tk.NW)
            return
        for col, (tag, rows) in enumerate((("己方", bp["ally"]),
                                           ("敌方", bp["enemy"]))):
            col_frame = ttk.Frame(self.bp_box, style="Card.TFrame")
            col_frame.grid(row=0, column=col, sticky=tk.NW,
                           padx=(0, 8) if col == 0 else 0)
            ttk.Label(col_frame, text=tag, style="Dim.TLabel").pack(anchor=tk.NW)
            if not rows:
                # 阵容/分组缺失（快照 playerlist 不全）时占位，不报错
                ttk.Label(col_frame, text="-", style="Dim.TLabel"
                          ).pack(anchor=tk.NW)
                continue
            for r in rows:
                row = ttk.Frame(col_frame, style="Card.TFrame")
                row.pack(anchor=tk.NW, fill=tk.X, pady=1)
                ttk.Label(row, text=r["name"], width=8, anchor=tk.W
                          ).pack(side=tk.LEFT)
                ttk.Progressbar(row, style="BP.Horizontal.TProgressbar",
                                maximum=6, value=min(r["legendary"], 6),
                                length=56).pack(side=tk.LEFT, padx=(2, 4))
                ttk.Label(row, text=f"{min(r['legendary'], 6)}/6", width=4,
                          anchor=tk.E).pack(side=tk.LEFT)
                ttk.Label(row, text=f"{r['gold']}G", style="Dim.TLabel",
                          width=7, anchor=tk.E).pack(side=tk.RIGHT)
        self.bp_box.columnconfigure(0, weight=1)
        self.bp_box.columnconfigure(1, weight=1)

    def _render_ranks(self, ranks, pending=False):
        if not ranks:
            self.card_ranks.config(text="-（海克斯界面出现时显示）")
            return
        lines = []
        for i, r in enumerate(ranks[:3], 1):
            if pending:
                # AI 评级精修在途：先显卡名，档位等回调后再放（与 overlay 同口径）
                lines.append(f"{'①②③'[i-1]} {r.get('name', '?')}  ⏳ AI 评级中…")
            else:
                grade = r.get("grade", "?")
                lines.append(f"{'①②③'[i-1]} {r.get('name', '?')}  [{grade}]  "
                             f"{r.get('reason', '')}")
        self.card_ranks.config(text="\n".join(lines))

    def _render_engine(self, st):
        if not st:
            # stats 异常被上游吞掉时不误报"关"（AI 可能实际在跑）
            self.lbl_engine.config(text="AI: -", foreground=Theme.TEXT_DIM)
            return
        mode = st.get("mode", "rule")
        if mode == "rule":
            txt, color = "AI: 关（规则模式）", Theme.TEXT_DIM
        else:
            budget = st.get('budget', 0)
            budget_txt = "∞" if budget <= 0 else budget   # <=0 = 不限次
            txt = (f"AI: {mode} | 本局 {st.get('calls', 0)}/{budget_txt} 次"
                   f" | ¥{st.get('cost_game', 0):.4f} | 上下文 {st.get('ctx_source', '-')}")
            color = Theme.SUCCESS
        self.lbl_engine.config(text=txt, foreground=color)

    def _render_freshness(self, state):
        info = self.session.data_freshness()
        age = info["age_days"]
        if age is None:
            txt, color = f"数据版本: {info['dd_version'] or '-'} | 抓取日期: 未知", Theme.WARNING
        else:
            txt = (f"数据版本: {info['dd_version'] or '-'} | "
                   f"抓取日期: {info['date']}（{age} 天前）")
            color = Theme.WARNING if age > 7 else Theme.TEXT_DIM
        self.lbl_fresh.config(text=txt, foreground=color)

    def _poll_log(self):
        try:
            try:
                while True:
                    line = self.log_queue.get_nowait()
                    self.txt_log.config(state=tk.NORMAL)
                    self.txt_log.insert(tk.END, line + "\n")
                    self.txt_log.see(tk.END)
                    if int(self.txt_log.index('end-1c').split('.')[0]) > 800:
                        self.txt_log.delete("1.0", "400.0")
                    self.txt_log.config(state=tk.DISABLED)
            except queue.Empty:
                pass
        except Exception as e:
            # 异常冒出会让本 after 链不再续约，日志面板从此冻结；
            # 记一行不冒出，下一帧自动恢复（同 _poll_ui 的处理）。
            print(f"[WARN] 日志轮询异常（下一帧自动恢复）: {type(e).__name__}: {e}")
        finally:
            self.root.after(200, self._poll_log)
