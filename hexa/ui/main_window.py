"""主窗口：状态/阵容/建议/成型度对比/台账(可纠错)/上局回顾/数据新鲜度/AI 仪表/日志

线程模型：后台线程（PhaseMachine/LivePoller/检测器）→ ui_queue → after 轮询。
ui_queue 既承载事件字符串也承载可调用对象（跨线程 UI 调度的统一出口）。

2026-09-30 UI 重构（Quiet Dark v3）——**只换皮肤，骨架不动**：
- 卡片统一发丝边 + 标题轨（`theme.card_frame`）。旧版卡内标题用页面底色
  `#0d1117` 画在 `#161b22` 卡片上，实机截图里是一块块深色矩形补丁 → 补
  `Card.TLabel` / `CardDim.TLabel` 卡内专用样式
- 8 张卡片用「2px 左轨」分层而非颜色泛滥：只有"当前建议"（主输出）挂
  强调色左轨，其余保持中性
- `wraplength` 由写死 500/250 改为跟随窗口宽（旧值在缩放后换行错乱，底部
  状态行被截断成「上下文 O」）
- ttk 控件补全样式（Entry/Combobox/Checkbutton/Scrollbar/Progressbar 原为
  clam 默认外观：白边输入框、亮灰滚动条、灰按钮），配置上收 theme.py
- 删掉「路线: AD」（与 overlay 一致：路线不是 ad/ap/坦克三键而是不同装备
  玩法，具名方案未进 UI，这行既没用又与系统自身的方案制矛盾）
- 去 emoji，逻辑层文案里的彩色 emoji 在渲染边界统一剥离（`strip_emoji`）
- 卡片/段落顺序、轮询频率、`ui_state()` 消费方式一字未改
- 20261013 增『上局回顾』卡：只读渲染 log/games/ 最近一份复盘 JSON
  （落盘早已存在但玩家看不到；数据准备纯函数在 review.py，见冒烟 [16]）
- 20261019 N4 复盘结果维度案：复盘卡升级『上局账本』一屏——format_review
  摘要 + format_ledger 采纳对账（出门装听劝/海克斯轮次/出装采纳/胜负
  KDA+分均，纯事实对账）同卡渲染；落盘侧新增 result/kda/summoner_name/
  item_events/total_gold 由 game_state/live_api/lcu 采集
"""
import queue
import time
import tkinter as tk
from tkinter import ttk

from hexa.ui.review import (format_ledger, format_review,
                            latest_review_path, load_review)
from hexa.ui.theme import (GRADES, Metrics, PENDING_WAIT, Palette, Theme, Type,
                           card_frame, grade_badge, ledger_line, retint_badge,
                           stage_color, strip_emoji, version_label)

STAGE_CN = {
    "idle": "待机", "champ_select": "选人中", "game_start": "加载中", "in_game": "对局中",
}

# 窗口几何：默认高度按内容请求校准，下限 1150（原 760 装得下满载内容）；
# 再夹到屏幕内，1080p 机器不至于开出超屏窗口。
MIN_H = 1150


class MainWindow:
    def __init__(self, root: tk.Tk, session, ui_queue: queue.Queue,
                 log_queue: queue.Queue, on_open_settings=None,
                 on_calibrate=None, on_refresh_data=None):
        self.root = root
        self.session = session
        self.ui_queue = ui_queue
        self.log_queue = log_queue
        self.on_open_settings = on_open_settings
        self.on_calibrate = on_calibrate
        self.on_refresh_data = on_refresh_data
        self._refresh_busy = False     # 数据刷新按钮在途标记
        self._ledger_cache = None      # 台账按钮去重重建
        self._bp_cache = None          # 成型度面板去重重建（同台账惯例）
        self._review_cache = None      # 上局回顾 (路径, mtime) 变化才重建
        self._review_checked = 0.0     # 上局回顾目录扫描时刻（15s 限频）
        self._last_poll_err = None     # 轮询异常签名（限频去重，防 500ms 刷屏）
        self._last_poll_err_ts = 0.0
        self._wraps = []               # 需随窗口宽度重排的标签（wraplength）
        self._build()
        self.root.after(100, self._poll_ui)
        self.root.after(200, self._poll_log)

    # ── 构建 ──────────────────────────────────────────────────────

    def _build(self):
        P = Palette
        ver = version_label()
        self.root.title(f"HexARAMHelper 海克斯大乱斗助手 {ver}")
        # 默认高度兜底 1150：成型度/预告/军师面板就位后满载内容实测约 1121px，
        # 旧值 760 只装得下空面板，日志会被 expand 挤到 1px（构建完按请求高度再校准）
        self.root.minsize(480, 560)
        Theme.style_root(self.root)

        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        main.bind("<Configure>", self._on_resize)

        # 日志：自建 Text + ttk 滚动条，不用 scrolledtext.ScrolledText。
        # 原因（本 Python 3.12 的 ScrolledText 是 **Text 子类**：Text 挂在内层
        # frame 上却没被 pack，frame 保持 Tk 默认浅底 → 日志区右侧/下方露白，
        # 实机出图才看出来（2026-09-30）；且它的滚动条是经典 tk.Scrollbar，
        # 吃不到 theme 注册的深色 TScrollbar（旧版那个亮灰滚动条就是这么来的）。
        logcard = card_frame(main, title="日志",
                             pack=dict(side=tk.BOTTOM, fill=tk.BOTH,
                                       expand=True, pady=(8, 0)))
        log_body = tk.Frame(logcard, bg=P["surface_inset"])
        log_body.pack(fill=tk.BOTH, expand=True)
        self.txt_log = tk.Text(
            log_body, height=8, bg=P["surface_inset"], fg=P["text_dim"],
            font=Theme.FONT_MONO, relief=tk.FLAT, highlightthickness=0, bd=0,
            insertbackground=P["accent"], state=tk.DISABLED, wrap=tk.WORD,
            padx=7, pady=5)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_bar = ttk.Scrollbar(log_body, orient=tk.VERTICAL,
                                command=self.txt_log.yview)
        log_bar.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log.config(yscrollcommand=log_bar.set)

        # 状态行：阶段色片 + 英雄（路线已删，见模块 docstring）
        status = tk.Frame(main, bg=P["bg"])
        status.pack(fill=tk.X, pady=(0, 6))
        self.lbl_stage = tk.Label(status, text="待机", font=Theme.FONT_BOLD,
                                  bg=P["surface_inset"], fg=P["text_mute"],
                                  padx=7, pady=2)
        self.lbl_stage.pack(side=tk.LEFT)
        self.lbl_hero = tk.Label(status, text="英雄: -", font=Theme.FONT,
                                 bg=P["bg"], fg=P["text_dim"])
        self.lbl_hero.pack(side=tk.LEFT, padx=(10, 0))
        # 版本角标：先 pack(RIGHT) 才会落在最右，按钮随后 pack(RIGHT) 排它左侧
        tk.Label(status, text=ver, font=(Theme.FAMILY, Type["small"]),
                 bg=P["bg"], fg=P["text_mute"]).pack(side=tk.RIGHT)
        if self.on_open_settings:
            ttk.Button(status, text="⚙ 设置", command=self.on_open_settings
                       ).pack(side=tk.RIGHT)
        if self.on_calibrate:
            ttk.Button(status, text="开始标定", command=self.on_calibrate
                       ).pack(side=tk.RIGHT, padx=(0, 8))
        tk.Frame(main, height=1, bg=P["line"]).pack(fill=tk.X)

        # 手动输英雄兜底行（视觉降权：三级文本 + 小字）
        manual = tk.Frame(main, bg=P["bg"])
        manual.pack(fill=tk.X, pady=(6, 0))
        tk.Label(manual, text="手动设英雄(兜底):", font=(Theme.FAMILY, Type["small"]),
                 bg=P["bg"], fg=P["text_mute"]).pack(side=tk.LEFT)
        self.ent_hero = ttk.Entry(manual, width=16)
        self.ent_hero.pack(side=tk.LEFT, padx=6)
        ttk.Button(manual, text="设置", command=self._on_manual_hero).pack(side=tk.LEFT)
        self.lbl_manual_hint = tk.Label(manual, text="", font=(Theme.FAMILY, Type["small"]),
                                        bg=P["bg"], fg=P["text_mute"])
        self.lbl_manual_hint.pack(side=tk.LEFT, padx=8)

        # 阵容卡片（两列 + 中缝 1px 竖线）
        teams = card_frame(main, pack=dict(fill=tk.X, pady=(8, 0)))
        col_a = tk.Frame(teams, bg=P["surface"])
        col_a.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        col_b = tk.Frame(teams, bg=P["surface"])
        col_b.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                   padx=(Metrics["gap_m"], 0))
        tk.Frame(teams, width=1, bg=P["line_soft"]).pack(side=tk.LEFT,
                                                           fill=tk.Y, padx=Metrics["gap_m"])
        for col, tag in ((col_a, "己方"), (col_b, "敌方")):
            tk.Label(col, text=tag, font=(Theme.FAMILY, Type["small"]),
                     bg=P["surface"], fg=P["text_mute"]).pack(anchor=tk.W)
        self.lbl_ally = tk.Label(col_a, text="-", font=Theme.FONT, bg=P["surface"],
                                 fg=P["text"], justify=tk.LEFT)
        self.lbl_ally.pack(anchor=tk.W)
        self.lbl_enemy = tk.Label(col_b, text="-", font=Theme.FONT, bg=P["surface"],
                                  fg=P["text"], justify=tk.LEFT)
        self.lbl_enemy.pack(anchor=tk.W)
        self._wraps.append((self.lbl_ally, 0.5))
        self._wraps.append((self.lbl_enemy, 0.5))

        # 威胁 + 建议 + 替换预告 + 军师滚动列表 + 成型度对比 + 台账(纠错)卡片
        # （顺序沿用旧版，不重排）
        self.card_threat = self._card(main, "敌方威胁画像")
        self.card_suggest = self._card(main, "当前建议", rail="accent",
                                       note="主输出")
        self.card_forecast = self._card(main, "满装替换预告")
        self.card_coach = self._card(main, "军师提示（最近 3 条）")

        # 成型度对比（v4.6）：敌我十人六神装进度（传说件 x/6 + 装备估值）
        bp = card_frame(main, title="成型度对比", note="传说件 x/6 · 装备估值",
                        pack=dict(fill=tk.X, pady=(8, 0)))
        self.bp_box = tk.Frame(bp, bg=P["surface"])
        self.bp_box.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

        ledger = card_frame(main, title="海克斯台账", note="点名字撤销纠错",
                            pack=dict(fill=tk.X, pady=(8, 0)))
        self.ledger_box = tk.Frame(ledger, bg=P["surface"])
        self.ledger_box.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

        # 上局账本（20261013 回顾面板案；N4 20261019 升级本局账本一屏）：
        # 复盘 JSON 局终落盘早已存在（game_state._write_review_log →
        # log/games/），此前玩家看不到。只读渲染最近一份：format_review
        # 摘要 + format_ledger 采纳对账（出门装听劝/海克斯轮次/出装采纳/
        # 胜负 KDA，纯事实对账）。缺文件/缺字段一律占位不炸（渲染链断链
        # 会让全部卡片冻结，见 _poll_ui 注释）。内容静态：目录扫描 15s
        # 限频、(路径, mtime) 变化才重建（台账同款缓存惯例）。
        self.card_review = self._card(main, "上局账本",
                                      note="log/games 最近一份 · 采纳对账")
        self.card_review.config(
            text="-（完成一局后自动显示：结果/听劝/采纳/海克斯台账/终局出装）",
            fg=P["text_mute"])

        self._build_ranks_card(main)

        # 数据新鲜度（版本翻译器底线：抓取日期常显 + >7 天黄标）
        fresh = tk.Frame(main, bg=P["bg"])
        fresh.pack(fill=tk.X, pady=(8, 0))
        self.lbl_fresh = tk.Label(fresh, text="数据版本: -", bg=P["bg"],
                                  fg=P["text_mute"], justify=tk.LEFT,
                                  anchor=tk.W, font=(Theme.FAMILY, Type["small"]))
        self.lbl_fresh.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.lbl_engine = tk.Label(fresh, text="AI: -", bg=P["bg"],
                                   fg=P["text_mute"], justify=tk.RIGHT,
                                   anchor=tk.E, font=(Theme.FAMILY, Type["small"]))
        self.lbl_engine.pack(side=tk.RIGHT)
        if self.on_refresh_data:
            self._refresh_btn = ttk.Button(fresh, text="刷新数据", width=8,
                                           command=self._on_refresh_data)
            self._refresh_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._wraps.append((self.lbl_fresh, 0.5))
        self._wraps.append((self.lbl_engine, 0.5))

        # 构建完按实际内容请求高度校准默认窗口（后续面板扩展自动适配，不低于兜底值）
        #
        # 2026-09-30：一度把高度夹到「屏高-80」，结果在 1080p 上更糟——内容
        # 需 ~1120px、窗口 1000px 时 pack 会把**最后 pack 的卡片压成 0 高度**
        # （海克斯台账/评级整张卡不可见且够不着）。旧公式让窗口略微超屏，
        # 代价只是日志底部几十像素在屏外（可拖动/缩放），明显更优，故保留原样。
        self.root.update_idletasks()
        self.root.geometry(f"560x{max(MIN_H, self.root.winfo_reqheight())}")

    def _card(self, parent, title, rail=None, note=None):
        """带标题轨的卡片，返回正文标签（调用点仍只 config(text=...)）"""
        P = Palette
        box = card_frame(parent, rail=rail, title=title, note=note,
                         tick=(rail or "line"),
                         pack=dict(fill=tk.X, pady=(8, 0)))
        body = tk.Label(box, text="-", font=Theme.FONT, bg=P["surface"],
                        fg=P["text"], justify=tk.LEFT, anchor=tk.W)
        body.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        self._wraps.append((body, 1.0))
        return body

    def _on_resize(self, ev):
        """wraplength 跟随窗口宽（旧值写死 500/250，缩放后换行错乱/底部截断）"""
        width = max(160, ev.width - Metrics["pad_card"] * 2)
        for lbl, frac in self._wraps:
            try:
                lbl.config(wraplength=max(120, int(width * frac) - 6))
            except tk.TclError:
                pass

    def _build_ranks_card(self, parent):
        """海克斯时刻评级卡：三行真徽章（单标签塞不进徽章，同 overlay 口径）"""
        P = Palette
        box = card_frame(parent, title="海克斯时刻评级",
                         pack=dict(fill=tk.X, pady=(8, 0)))
        self.ranks_hint = tk.Label(box, text="-（海克斯界面出现时显示）",
                                   font=(Theme.FAMILY, Type["small"]),
                                   bg=P["surface"], fg=P["text_mute"], anchor=tk.W)
        self.ranks_hint.pack(anchor=tk.W)
        self.ranks_rows = []
        for i in range(3):
            row = tk.Frame(box, bg=P["surface"])
            badge = grade_badge(row, "")
            badge.pack(side=tk.LEFT, pady=(1, 0))
            num = tk.Label(row, text="①②③"[i], font=(Theme.FAMILY, Type["small"], "bold"),
                           bg=P["surface"], fg=P["text_mute"])
            num.pack(side=tk.LEFT, padx=(5, 3))
            name = tk.Label(row, text="", font=Theme.FONT, bg=P["surface"],
                            fg=P["text"], anchor=tk.W)
            name.pack(side=tk.LEFT)
            reason = tk.Label(row, text="", font=(Theme.FAMILY, Type["small"]),
                              bg=P["surface"], fg=P["text_mute"], anchor=tk.W)
            reason.pack(side=tk.LEFT, padx=(6, 0))
            self.ranks_rows.append({"row": row, "badge": badge, "name": name,
                                    "reason": reason, "shown": False})

    # ── 交互 ──────────────────────────────────────────────────────

    def _on_manual_hero(self):
        query = self.ent_hero.get().strip()
        if not query:
            return
        result = self.session.set_hero_manual(query)
        if result:
            self.lbl_manual_hint.config(text=strip_emoji(result), fg=Palette["success"])
            self.ent_hero.delete(0, tk.END)
        else:
            self.lbl_manual_hint.config(text="未匹配到英雄", fg=Palette["error"])

    def _on_refresh_data(self):
        """数据刷新按钮：在途防重入，完成回调恢复按钮（回调由 app 侧经
        ui() 调度回 UI 线程）。刷新脚本在后台线程跑，输出经 print 进日志面板。"""
        if self._refresh_busy:
            return
        self._refresh_busy = True
        self._refresh_btn.config(state=tk.DISABLED, text="刷新中…")

        def done():
            self._refresh_busy = False
            try:
                self._refresh_btn.config(state=tk.NORMAL, text="刷新数据")
            except tk.TclError:
                pass
        self.on_refresh_data(done)

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
            P = Palette
            stage = state["stage"]
            self.lbl_stage.config(text=STAGE_CN.get(stage, stage),
                                  fg=stage_color(stage))
            self.lbl_hero.config(text=f"英雄: {state['my_hero'] or '-'}")
            self.lbl_ally.config(text=" ".join(state["ally"]) or "-")
            self.lbl_enemy.config(text=" ".join(state["enemy"]) or "-")
            self.card_threat.config(text=strip_emoji(state["threat"]) or "-")
            # 满六神装替换建议：正文保持主文本（多行琥珀字可读性差），
            # 与 overlay 同口径由卡片左轨/语义色承担区分
            self.card_suggest.config(
                text=strip_emoji(state["suggestion"]) or "-（对局内触发时给出）")
            # 替换预告（提示级不改主建议）：威胁与已有装备错配时给出方向
            forecast = strip_emoji(state.get("swap_forecast") or "")
            self.card_forecast.config(
                text=forecast or "-（威胁与出装明显错配时提示）",
                fg=P["warning"] if forecast else P["text_mute"])
            # 军师提示滚动列表（v4.5，最多 3 条，最新在下）
            coach = state.get("coach") or []
            self.card_coach.config(
                text="\n".join(
                    f"{time.strftime('%H:%M:%S', time.localtime(c['ts']))}"
                    f"  {strip_emoji(c['text'])}" for c in coach)
                or "-（对局内自动生成：阵容缺口/顺逆风/攒钱与替换提醒）")
            self._render_ledger(state["augments"])
            self._render_build_progress(state.get('build_progress'))
            self._render_ranks(state.get('card_ranks') or [],
                               bool(state.get('card_ranks_pending')))
            self._render_freshness(state)
            self._render_engine(state.get('engine') or {},
                                state.get('kda_alerts_used'),
                                state.get('kda_alerts_limit'))
            self._render_review()
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
        """台账纠错：点名字撤销（列表变化才重建，防每 500ms 重建闪烁）。

        条目为 ui_state 直出的 {"name","tier","tier_src","round_id"}；行文
        与 overlay 同口径走 theme.ledger_line——『① 贪欲束缚（棱彩）』，
        tier 缺省省略括号；轮序号用 round_id+1、先验档位标「先验」
        （N1 20261019），旧快照缺字段回退下标。"""
        P = Palette
        key = tuple((a.get("name", ""), a.get("tier", ""),
                     a.get("tier_src", ""), a.get("round_id"))
                    for a in augments)
        if key == self._ledger_cache:
            return
        self._ledger_cache = key
        for w in self.ledger_box.winfo_children():
            w.destroy()
        if not augments:
            tk.Label(self.ledger_box, text="-（游戏内海克斯界面点击后自动记录）",
                     font=(Theme.FAMILY, Type["small"]), bg=P["surface"],
                     fg=P["text_mute"], anchor=tk.W).pack(anchor=tk.W)
            return
        for i, a in enumerate(augments):
            name = a.get("name", "?")
            rid = a.get("round_id")
            row = tk.Frame(self.ledger_box, bg=P["surface"])
            row.pack(anchor=tk.W, fill=tk.X, pady=2)
            tk.Label(row, text=ledger_line(
                         i, name, a.get("tier") or "",
                         round_no=None if rid is None else rid + 1,
                         tier_src=a.get("tier_src") or ""),
                     font=Theme.FONT, bg=P["surface"],
                     fg=P["text"]).pack(side=tk.LEFT)
            ttk.Button(row, text="撤销", width=5,
                       command=lambda n=name: self.session.revoke_augment(n)
                       ).pack(side=tk.RIGHT)

    def _render_build_progress(self, bp):
        """成型度对比（v4.6）：敌我两列，每行 英雄名 + 进度条 + x/6 + 估值。

        行由 game_state 按成型度降序排好；数据变化才重建（台账同款缓存
        惯例，防 500ms 轮询重建闪烁）。bp=None（快照未到/装备数据缺失）
        → 占位行不报错。
        """
        P = Palette
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
            tk.Label(self.bp_box, text="-（对局内快照同步后显示双方成型度）",
                     font=(Theme.FAMILY, Type["small"]), bg=P["surface"],
                     fg=P["text_mute"], anchor=tk.W).pack(anchor=tk.W)
            return
        for col, (tag, rows) in enumerate((("己方", bp["ally"]),
                                           ("敌方", bp["enemy"]))):
            col_frame = tk.Frame(self.bp_box, bg=P["surface"])
            col_frame.grid(row=0, column=col, sticky=tk.NW,
                           padx=(0, Metrics["gap_l"]) if col == 0 else 0)
            tk.Label(col_frame, text=tag, font=(Theme.FAMILY, Type["small"]),
                     bg=P["surface"], fg=P["text_mute"]).pack(anchor=tk.W)
            if not rows:
                # 阵容/分组缺失（快照 playerlist 不全）时占位，不报错
                tk.Label(col_frame, text="-", font=(Theme.FAMILY, Type["small"]),
                         bg=P["surface"], fg=P["text_mute"]).pack(anchor=tk.W)
                continue
            for r in rows:
                row = tk.Frame(col_frame, bg=P["surface"])
                row.pack(anchor=tk.W, fill=tk.X, pady=2)
                tk.Label(row, text=r["name"], font=(Theme.FAMILY, Type["small"]),
                         bg=P["surface"], fg=P["text"], width=7,
                         anchor=tk.W).pack(side=tk.LEFT)
                ttk.Progressbar(row, style="BP.Horizontal.TProgressbar",
                                maximum=6, value=min(r["legendary"], 6),
                                length=52).pack(side=tk.LEFT, padx=(2, 4))
                tk.Label(row, text=f"{min(r['legendary'], 6)}/6",
                         font=(Theme.MONO_FAMILY, Type["small"]), bg=P["surface"],
                         fg=P["text_dim"], width=4, anchor=tk.E).pack(side=tk.LEFT)
                # 估值破万走琥珀（对方成型更快时一眼可见）
                hot = r["gold"] >= 10000
                tk.Label(row, text=f"{r['gold']}G",
                         font=(Theme.MONO_FAMILY, Type["small"]), bg=P["surface"],
                         fg=P["warning"] if hot else P["text_mute"],
                         width=7, anchor=tk.E).pack(side=tk.RIGHT)
        self.bp_box.grid_columnconfigure(0, weight=1)
        self.bp_box.grid_columnconfigure(1, weight=1)

    def _render_ranks(self, ranks, pending=False):
        """海克斯时刻评级：15px 档位徽章 + 卡名 + 理由（逐行独立着色，同 overlay）

        行控件一次性建好、每帧只 config（destroy/recreate 会闪烁，同台账惯例）。
        可见行恒为前缀 0..n-1，按索引顺序 pack → 顺序永不翻面。
        """
        P = Palette
        for i, w in enumerate(self.ranks_rows):
            entry = ranks[i] if i < len(ranks) else None
            if entry is None:
                if w["shown"]:
                    w["row"].pack_forget()
                    w["shown"] = False
                continue
            grade = "" if pending else (entry.get("grade") or "?")
            retint_badge(w["badge"], grade)
            w["name"].config(text=strip_emoji(entry.get("name", "?")),
                             fg=GRADES.get(grade) or P["text"])
            w["reason"].config(text=PENDING_WAIT if pending
                               else strip_emoji(entry.get("reason", "") or ""))
            if not w["shown"]:
                w["row"].pack(anchor=tk.W, fill=tk.X, pady=2)
                w["shown"] = True
        if ranks and not self.ranks_hint.winfo_ismapped():
            self.ranks_hint.pack_forget()
        elif not ranks and not self.ranks_hint.winfo_ismapped():
            self.ranks_hint.pack(anchor=tk.W)

    def _render_engine(self, st, kda_used=None, kda_limit=None):
        """AI 仪表行：模式/调用次数/费用/上下文 + 起飞提示计数（P3 杂项：
        enemy_kda 触发每局限次的已用/上限直出 ui_state，这里原样拼接，
        规则/AI 两种模式都显示——该触发不依赖 AI 是否开启）"""
        P = Palette
        kda_txt = (f" | 起飞提示 {kda_used}/{kda_limit}"
                   if kda_used is not None and kda_limit is not None else "")
        if not st:
            # stats 异常被上游吞掉时不误报"关"（AI 可能实际在跑）
            self.lbl_engine.config(text=f"AI: -{kda_txt}", fg=P["text_mute"])
            return
        mode = st.get("mode", "rule")
        if mode == "rule":
            txt, color = f"AI: 关（规则模式）{kda_txt}", P["text_mute"]
        else:
            budget = st.get('budget', 0)
            budget_txt = "∞" if budget <= 0 else budget   # <=0 = 不限次
            txt = (f"AI: {mode} | 本局 {st.get('calls', 0)}/{budget_txt} 次"
                   f" | ¥{st.get('cost_game', 0):.4f} | 上下文 {st.get('ctx_source', '-')}"
                   f"{kda_txt}")
            color = P["success"]
        self.lbl_engine.config(text=txt, fg=color)

    def _render_review(self):
        """上局账本：最近一份复盘 JSON 只读渲染（format_review 摘要 +
        format_ledger 采纳对账，N4 本局账本一屏）。

        目录扫描 15s 限频（500ms 轮询里 glob 太重）；(路径, mtime) 未
        变化不重建（台账同款缓存惯例）。读失败/缺字段一律占位行不冒泡
        ——异常冒出会断开 after 轮询链，全部卡片随之冻结（同 _poll_ui）。
        建议文本可能带彩色 emoji，渲染边界统一剥离（同其余卡片口径）。
        """
        now = time.monotonic()
        if now - self._review_checked < 15:
            return
        self._review_checked = now
        path = latest_review_path()
        try:
            key = (str(path), path.stat().st_mtime) if path else None
        except OSError:
            key = None
        if key == self._review_cache:
            return
        self._review_cache = key
        if path is None:
            # 复盘文件消失（清理/换目录）：回占位，不留上一份的残影
            self.card_review.config(
                text="-（完成一局后自动显示：结果/听劝/采纳/海克斯台账/终局出装）",
                fg=Palette["text_mute"])
            return
        doc = load_review(path)
        if doc is None:
            self.card_review.config(
                text=f"复盘读取失败: {path.name}（只读跳过，不影响本局）",
                fg=Palette["warning"])
            return
        self.card_review.config(
            text=strip_emoji("\n".join([*format_review(doc),
                                        *format_ledger(doc)])),
            fg=Palette["text"])

    def _render_freshness(self, state):
        P = Palette
        info = self.session.data_freshness()
        age = info["age_days"]
        if age is None:
            txt, color = f"数据版本: {info['dd_version'] or '-'} | 抓取日期: 未知", P["warning"]
        else:
            txt = (f"数据版本: {info['dd_version'] or '-'} | "
                   f"抓取日期: {info['date']}（{age} 天前）")
            color = P["warning"] if age > 7 else P["text_mute"]
        self.lbl_fresh.config(text=txt, fg=color)

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
