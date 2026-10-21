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
- 20261020 卡片叠改挂滚动画布：卡片叠内容高无上界（账本轮数/成型度行数
  都随对局增长，N4 账本段一屏运行期 +数百 px）而窗口高固定（构建期一次
  校准 + minsize + 屏幕工作区夹取），腔体不足时放不下的后 pack 卡片被
  packer 整张 unmap（评级卡/AI 仪表行/账本本体不可见且够不着，1080p
  必现）。画布作受压方：pack 顺位最后、被截短只是少显示内容不 unmap，
  滚动条常驻兜底。卡片/段落顺序、_render_* 消费方式不变
- 20261021 双栏仪表盘案（用户口径「主页面不需要长条，不然都有滚动条，
  很多东西一眼显示不全」）：单列长条改成 **双栏 + 满宽行** 的仪表盘
  ——顶栏（阶段/英雄/按钮）→ 阵容（满宽）→ 双栏主体（左：当前建议/
  敌方威胁/替换预告/军师提示；右：海克斯时刻评级/海克斯台账/成型度
  对比）→ 上局账本（满宽行）→ 日志（窗口底部固定）→ 状态底栏。
  滚动画布保留为**溢出兜底**（窗口被手动缩到很小/内容暴涨时不丢控件），
  但滚动条改为**按需显隐**：内容装得下就藏起来，且藏在固定宽度的右侧
  槽里（显隐不改画布宽度 → 不引发 wraplength 连锁重排抖动）。
  wraplength 改为**按容器宽**计算（单列时按窗口宽算，两栏后各列宽不
  同，按窗口宽算会错行）；渲染链、ui_state 消费方式、字段一字未改。
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

# 窗口几何（20261021 双栏案）：默认宽度给到两栏并排的舒适区（1080p 下
# 横向放得下），高度按内容校准后夹进屏幕工作区——滚动画布兜底，装不下
# 时滚动条自动出现，不再靠"超屏窗口"硬扛。
WIN_W = 1040
MIN_H = 720
SCROLL_GUTTER = 12      # 右侧滚动条槽宽（常驻占位，画布宽度恒定）


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
        self._wrap_groups = {}         # 容器 → [(标签, 占比, 内缩)]（列内换行）
        self._wrap_bound = set()        # 已绑 <Configure> 的容器（避免重复绑）
        self._scroll_shown = False      # 滚动条是否已显形（按需显隐）
        self._win_w = WIN_W             # 目标窗宽（构建期算好，供 _fit_window）
        self._build()
        self.root.after(100, self._poll_ui)
        self.root.after(200, self._poll_log)

    # ── 构建 ──────────────────────────────────────────────────────

    def _build(self):
        P = Palette
        ver = version_label()
        self.root.title(f"HexARAMHelper 海克斯大乱斗助手 {ver}")
        # 默认尺寸按内容校准（双栏案：横向 1040 / 纵向 ~780 装得下满载
        # 内容），minsize 只防窗口被缩到两栏都放不下的程度
        self.root.minsize(880, 620)
        Theme.style_root(self.root)

        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # ── 底部固定区：状态底栏 + 日志卡 ────────────────────────────
        # pack 顺序 = 腔体分配顺序：先 pack(BOTTOM) 占住底边，剩下的腔体
        # 留给上方内容区（expand 归它）。日志在画布外、自带滚动，不与画布
        # 抢滚轮（祖籍过滤见 _on_wheel）。
        bottom = tk.Frame(outer, bg=P["bg"])
        bottom.pack(side=tk.BOTTOM, fill=tk.X)

        # 状态底栏（薄一行）：手动英雄兜底 | 数据新鲜度 | 刷新数据 | AI 仪表
        # 20261021 双栏案：手动设英雄从独立行挪进底栏——它是兜底功能，
        # 视觉降权更合理，且腾出一行纵向（1080p 屏只有 ~990px 可用，
        # 满载内容要压进这个数才能一屏看全、滚动条不出现）。
        footer = tk.Frame(bottom, bg=P["bg"])
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

        manual = tk.Frame(footer, bg=P["bg"])
        manual.pack(side=tk.LEFT, padx=(0, Metrics["gap_l"]))
        tk.Label(manual, text="手动英雄", font=(Theme.FAMILY, Type["small"]),
                 bg=P["bg"], fg=P["text_mute"]).pack(side=tk.LEFT)
        self.ent_hero = ttk.Entry(manual, width=12)
        self.ent_hero.pack(side=tk.LEFT, padx=4)
        ttk.Button(manual, text="设置", width=4,
                   command=self._on_manual_hero).pack(side=tk.LEFT)
        self.lbl_manual_hint = tk.Label(manual, text="",
                                        font=(Theme.FAMILY, Type["small"]),
                                        bg=P["bg"], fg=P["text_mute"])
        self.lbl_manual_hint.pack(side=tk.LEFT, padx=6)

        self.lbl_fresh = tk.Label(footer, text="数据版本: -", bg=P["bg"],
                                  fg=P["text_mute"], justify=tk.LEFT,
                                  anchor=tk.W, font=(Theme.FAMILY, Type["small"]),
                                  wraplength=260)
        self.lbl_fresh.pack(side=tk.LEFT, fill=tk.X, expand=True,
                            padx=(Metrics["gap_l"], 0))
        self.lbl_engine = tk.Label(footer, text="AI: -", bg=P["bg"],
                                   fg=P["text_mute"], justify=tk.RIGHT,
                                   anchor=tk.E, font=(Theme.FAMILY, Type["small"]),
                                   wraplength=300)
        self.lbl_engine.pack(side=tk.RIGHT)
        if self.on_refresh_data:
            self._refresh_btn = ttk.Button(footer, text="刷新数据", width=8,
                                           command=self._on_refresh_data)
            self._refresh_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._wrap_in(footer, self.lbl_fresh, 1.0)
        self._wrap_in(footer, self.lbl_engine, 1.0)

        # 日志：自建 Text + ttk 滚动条，不用 scrolledtext.ScrolledText。
        # 原因（本 Python 3.12 的 ScrolledText 是 **Text 子类**：Text 挂在内层
        # frame 上却没被 pack，frame 保持 Tk 默认浅底 → 日志区右侧/下方露白，
        # 实机出图才看出来（2026-09-30）；且它的滚动条是经典 tk.Scrollbar，
        # 吃不到 theme 注册的深色 TScrollbar（旧版那个亮灰滚动条就是这么来的）。
        logcard = card_frame(bottom, title="日志",
                             pack=dict(side=tk.TOP, fill=tk.X, pady=(8, 0)))
        log_body = tk.Frame(logcard, bg=P["surface_inset"])
        log_body.pack(fill=tk.BOTH, expand=True)
        self.txt_log = tk.Text(
            log_body, height=3, bg=P["surface_inset"], fg=P["text_dim"],
            font=Theme.FONT_MONO, relief=tk.FLAT, highlightthickness=0, bd=0,
            insertbackground=P["accent"], state=tk.DISABLED, wrap=tk.WORD,
            padx=7, pady=5)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_bar = ttk.Scrollbar(log_body, orient=tk.VERTICAL,
                                command=self.txt_log.yview)
        log_bar.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log.config(yscrollcommand=log_bar.set)

        # ── 内容区：画布 + 右侧滚动条槽（按需显隐） ──────────────────
        # 画布保留为「溢出兜底」：窗口被手动缩到很小、或账本/成型度行数暴涨
        # 时，内容不被裁掉而是可滚到（老版把后 pack 的卡片整张 unmap，
        # 够不着也看不见）。滚动条不再常驻：装得下就藏起来（用户口径：主
        # 页面不要滚动条），且它住在**固定宽度的右侧槽**里 → 显隐不改画布
        # 宽度，不会引发 wraplength 连锁重排抖动。
        gutter = tk.Frame(outer, bg=P["bg"], width=SCROLL_GUTTER)
        gutter.pack_propagate(False)
        gutter.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll = ttk.Scrollbar(gutter, orient=tk.VERTICAL,
                                     style="Slim.Vertical.TScrollbar")

        area = tk.Frame(outer, bg=P["bg"])
        area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(area, bg=P["bg"], highlightthickness=0, bd=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas = canvas
        self.scroll.config(command=canvas.yview)
        canvas.config(yscrollcommand=self.scroll.set)
        # 卡片叠容器沿用 main 命名：下方全部卡片/行的父容器引用逐字不动，
        # 滚动外壳只是包在它外面的新增一层
        main = tk.Frame(canvas, bg=P["bg"])
        self.main = main            # 供 _fit_window 量内容高
        self.bottom = bottom        # 供 _fit_window 算窗口目标高
        self._canvas_item = canvas.create_window((0, 0), window=main,
                                                 anchor="nw")
        # 画布宽变（拉窗）→ 内层 frame 跟宽，卡片只纵滚不横滚
        canvas.bind("<Configure>", self._on_canvas_resize)
        # 内层 frame 宽高变（wraplength 重排/卡片文本增删）→ 刷 scrollregion
        main.bind("<Configure>", self._on_resize)
        # 滚轮事件递给指针下的具体控件（多为卡内 label/frame），不会沿几何
        # 父链冒泡到画布，只能全局绑定后按祖籍过滤；日志卡在画布外有自己的
        # 滚动（Text 类绑定），祖籍不是画布时不抢
        canvas.bind_all("<MouseWheel>", self._on_wheel)

        # 状态行：阶段色片 + 英雄（路线已删，见模块 docstring）
        status = tk.Frame(main, bg=P["bg"])
        status.pack(fill=tk.X)
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
        tk.Frame(status, height=1, bg=P["line"]).pack(fill=tk.X, pady=(6, 0))

        # 手动输英雄兜底行已挪进底部状态栏（见底栏构建处）——20261021 双栏
        # 案为纵向空间让位；功能与接线一字未改。

        # 阵容卡片（满宽，单行：己方 | 敌方）——双栏案压缩纵向：两支队伍
        # 挤在一行（1007px 内 5 个名字放得下），省下的高度给内容更密的
        # 主体双栏（1080p 屏只有 ~990px 可用，满载内容必须压进这个数）
        teams = card_frame(main, pack=dict(fill=tk.X, pady=(8, 0)))
        team_row = tk.Frame(teams, bg=P["surface"])
        team_row.pack(anchor=tk.W, fill=tk.X)
        tk.Label(team_row, text="己方", font=(Theme.FAMILY, Type["small"]),
                 bg=P["surface"], fg=P["text_mute"]).pack(side=tk.LEFT)
        self.lbl_ally = tk.Label(team_row, text="-", font=Theme.FONT,
                                 bg=P["surface"], fg=P["text"], justify=tk.LEFT)
        self.lbl_ally.pack(side=tk.LEFT, padx=(6, 0))
        tk.Frame(team_row, width=1, bg=P["line_soft"]).pack(
            side=tk.LEFT, fill=tk.Y, padx=Metrics["gap_l"])
        tk.Label(team_row, text="敌方", font=(Theme.FAMILY, Type["small"]),
                 bg=P["surface"], fg=P["text_mute"]).pack(
                     side=tk.LEFT, padx=(Metrics["gap_l"], 0))
        self.lbl_enemy = tk.Label(team_row, text="-", font=Theme.FONT,
                                  bg=P["surface"], fg=P["text"], justify=tk.LEFT)
        self.lbl_enemy.pack(side=tk.LEFT, padx=(6, 0))
        self._wrap_in(team_row, self.lbl_ally, 0.45)
        self._wrap_in(team_row, self.lbl_enemy, 0.45)

        # 上局账本（20261013 回顾面板案；N4 20261019 升级本局账本一屏）：
        # 复盘 JSON 局终落盘早已存在（game_state._write_review_log →
        # log/games/），此前玩家看不到。只读渲染最近一份：format_review
        # 摘要 + format_ledger 采纳对账（出门装听劝/海克斯轮次/出装采纳/
        # 胜负 KDA，纯事实对账）。缺文件/缺字段一律占位不炸（渲染链断链
        # 会让全部卡片冻结，见 _poll_ui 注释）。内容静态：目录扫描 15s
        # 限频、(路径, mtime) 变化才重建（台账同款缓存惯例）。
        #
        # 20261021 双栏案：改成**满宽行**并 pack 在双栏主体之前 → 落在腔体
        # 底边（账本是对账信息，铺满一行读起来不折行、不占纵向空间）。
        self.card_review = self._card(main, "上局账本",
                                      note="log/games 最近一份 · 采纳对账",
                                      pack=dict(fill=tk.X, side=tk.BOTTOM,
                                                pady=(8, 0)),
                                      # 账本是对账明细（8~12 行），用小字排：
                                      # 1080p 屏只有 ~990px 可用，正文密度
                                      # 直接决定主页面能不能一屏装下
                                      font=(Theme.FAMILY, Type["small"]))
        self.card_review.config(
            text="-（完成一局后自动显示：结果/听劝/采纳/海克斯台账/终局出装）",
            fg=P["text_mute"])

        # ── 主体双栏（20261021 双栏仪表盘案）────────────────────────
        # 左栏 = 局内要盯的（主输出 → 威胁 → 替换预告 → 军师）；
        # 右栏 = 盘点类（海克斯时刻评级 → 台账 → 成型度对比）。
        # 两栏等宽（uniform）——左栏文字多、右栏是表，宽度差会让表挤成两行。
        body = tk.Frame(main, bg=P["bg"])
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        body.grid_columnconfigure(0, weight=1, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)
        col_l = tk.Frame(body, bg=P["bg"])
        col_l.grid(row=0, column=0, sticky="nsew")
        col_r = tk.Frame(body, bg=P["bg"])
        col_r.grid(row=0, column=1, sticky="nsew", padx=(Metrics["gap_m"], 0))

        # 左栏：当前建议挂强调左轨（主输出），其余保持中性
        self.card_suggest = self._card(col_l, "当前建议", rail="accent",
                                       note="主输出")
        self.card_threat = self._card(col_l, "敌方威胁画像")
        self.card_forecast = self._card(col_l, "满装替换预告")
        self.card_coach = self._card(col_l, "军师提示（最近 3 条）")

        # 右栏：海克斯时刻评级 → 海克斯台账(纠错) → 成型度对比
        self._build_ranks_card(col_r)

        ledger = card_frame(col_r, title="海克斯台账", note="点名字撤销纠错",
                            pack=dict(fill=tk.X, pady=(8, 0)))
        self.ledger_box = tk.Frame(ledger, bg=P["surface"])
        self.ledger_box.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

        # 成型度对比（v4.6）：敌我十人六神装进度（传说件 x/6 + 装备估值）
        bp = card_frame(col_r, title="成型度对比", note="传说件 x/6 · 装备估值",
                        pack=dict(fill=tk.X, pady=(8, 0)))
        self.bp_box = tk.Frame(bp, bg=P["surface"])
        self.bp_box.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

        # 构建完按实际内容请求高度校准默认窗口（后续面板扩展自动适配，不低于兜底值）
        #
        # 2026-09-30：一度把高度夹到「屏高-80」，结果在 1080p 上更糟——内容
        # 需 ~1120px、窗口 1000px 时 pack 会把**最后 pack 的卡片压成 0 高度**
        # （海克斯台账/评级整张卡不可见且够不着）。旧公式让窗口略微超屏，
        # 代价只是日志底部几十像素在屏外（可拖动/缩放），明显更优，故保留原样。
        #
        # 画布不向 packer 上报卡片叠的内容高（超出部分被裁进滚动区），校准前
        # 先把画布请求高顶到卡片叠所需高：初始窗口仍按整卡叠+日志校准；滚动
        # 只在窗口被屏幕夹小/手动缩小或运行期内容长高（上局账本一屏整段
        # +数百 px，本窗高构建后不再跟随）时接管。
        self.root.update_idletasks()
        self.canvas.config(height=main.winfo_reqheight())
        self.root.update_idletasks()
        # 20261021 双栏案：横向固定 1040（两栏并排的舒适区，1080p 放得下），
        # 纵向按内容校准后夹进屏幕工作区——**不再超屏**（旧版靠"窗口略超
        # 屏"硬扛内容，代价是日志底部落到屏外）。装不下的部分由画布 +
        # 按需滚动条兜底（老版会把后 pack 的卡片整张 unmap，够不着）。
        w = min(WIN_W, max(880, self.root.winfo_screenwidth() - 60))
        h = max(MIN_H, self.root.winfo_reqheight())
        h = min(h, max(MIN_H, self.root.winfo_screenheight() - 90))
        # 目标宽度存一份：窗口未映射时 winfo_width() 返回 1，直接拿它改
        # 几何会把宽度写成 1（被 minsize 兜成 880，两栏被压扁）
        self._win_w = w
        self.root.geometry(f"{w}x{h}")
        self._sync_scroll()

    def _card(self, parent, title, rail=None, note=None, pack=None, font=None):
        """带标题轨的卡片，返回正文标签（调用点仍只 config(text=...)）"""
        P = Palette
        box = card_frame(parent, rail=rail, title=title, note=note,
                         tick=(rail or "line"),
                         pack=pack or dict(fill=tk.X, pady=(8, 0)))
        body = tk.Label(box, text="-", font=font or Theme.FONT, bg=P["surface"],
                        fg=P["text"], justify=tk.LEFT, anchor=tk.W,
                        wraplength=240)
        body.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))
        # 正文换行宽度跟随**所在卡片内宽**（双栏后两列宽不同，按窗口宽算
        # 会错行）；inset = 卡片左右内距。初始 wraplength=240 是为了不让
        # 长文本在首帧把整列撑宽（grid 列最小宽 = 内容请求宽）。
        self._wrap_in(box, body, 1.0, inset=Metrics["pad_card"] * 2)
        return body

    # ── 换行与滚动（20261021 双栏案）────────────────────────────────

    def _wrap_in(self, container, label, frac=1.0, inset=0):
        """登记「随容器宽度换行」的标签：容器 <Configure> 时按**容器宽**
        （双栏后 = 列宽，不是窗口宽）算 wraplength。"""
        self._wrap_groups.setdefault(container, []).append((label, frac, inset))
        if container not in self._wrap_bound:
            self._wrap_bound.add(container)
            container.bind("<Configure>",
                           lambda ev, c=container: self._resize_group(c))

    def _resize_group(self, container):
        """按容器实际宽重排该容器内的换行标签（列宽变化 → 文本重排）。"""
        width = container.winfo_width()
        if width <= 1:
            return
        for label, frac, inset in self._wrap_groups.get(container, []):
            try:
                label.config(wraplength=max(120, int((width - inset) * frac) - 6))
            except tk.TclError:
                pass

    def _on_canvas_resize(self, ev):
        """画布宽变（拉窗）→ 内层 frame 跟宽，卡片只纵滚不横滚；顺带按需
        显隐滚动条（内容装得下就不留滚动条）。"""
        self.canvas.itemconfigure(self._canvas_item, width=ev.width)
        self._sync_scroll()

    def _on_resize(self, ev):
        """内层 frame 宽高变（wraplength 重排/卡片文本增删）→ 刷 scrollregion
        并按需显隐滚动条。"""
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass
        self._sync_scroll()

    def _sync_scroll(self):
        """滚动条按需显隐：内容装得下就藏起来（用户口径「主页面不需要长条，
        不然都有滚动条」），装不下才出现。滚动条住在**固定宽度的右侧槽**
        里 → 显隐不改画布宽度，不会引发 wraplength 连锁重排抖动。"""
        try:
            bbox = self.canvas.bbox("all")
            need = bool(bbox) and (bbox[3] - bbox[1]) > self.canvas.winfo_height() + 2
        except tk.TclError:
            need = False
        if need and not self._scroll_shown:
            self.scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self._scroll_shown = True
        elif not need and self._scroll_shown:
            self.scroll.pack_forget()
            self._scroll_shown = False

    def _fit_window(self):
        """窗口随内容长高（夹屏幕内）——「一眼看全」的落地动作。

        20261021 双栏案实测坑：构建期各卡片还是空的（评级行/台账行/成型度
        行都是运行期才填），按空内容校准的窗口高只有 ~744px；满载后真实
        内容高 ~830px —— 上局账本整卡与成型度卡下半被裁掉，正是用户说的
        「一眼显示不全」。这里在轮询里按**实际内容高**补足窗口高度，
        超屏则停住（剩下的交给滚动条兜底，不让窗口开到屏外）。

        只长不缩：内容回缩（切回小内容）时窗口保持大尺寸——收窗是用户
        的动作，程序擅自缩会让人以为界面在闪。
        """
        try:
            self.root.update_idletasks()
            content_h = self.main.winfo_reqheight()
            bottom_h = self.bottom.winfo_reqheight()
            want = content_h + bottom_h + 24     # 24 = outer pady(10×2) + 余量
            cur = self.root.winfo_height()
            limit = self.root.winfo_screenheight() - 90
            if want > cur + 4 and want <= limit:
                self.root.geometry(f"{self._win_w}x{want}")
        except tk.TclError:
            pass

    def _on_wheel(self, ev):
        """滚轮跟随指针：仅指针落在卡片叠（画布或其内层后代）时滚动画布。
        事件递给指针下的具体控件、不沿几何父链冒泡，故全局绑定 + 祖籍过滤；
        日志卡祖籍不是画布，不抢它 Text 自带的滚动。"""
        w = getattr(ev, "widget", None)
        canvas = self.canvas
        while w is not None and w is not canvas:
            w = getattr(w, "master", None)
        if w is canvas:
            try:
                canvas.yview_scroll(int(-ev.delta / 120), "units")
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
            # 理由给 wraplength 封顶：双栏后列宽固定，长理由若不限宽会把
            # 整列撑宽、把左栏挤扁（grid 列最小宽 = 内容请求宽）
            reason = tk.Label(row, text="", font=(Theme.FAMILY, Type["small"]),
                              bg=P["surface"], fg=P["text_mute"], anchor=tk.W,
                              wraplength=230)
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
            # 英雄行带等级（快照直出的免费事实，同 overlay 口径）
            hero = state["my_hero"]
            level = state.get("my_level") or 0
            hero_txt = f"英雄: {hero or '-'}"
            if hero and level:
                hero_txt += f" Lv.{level}"
            self.lbl_hero.config(text=hero_txt)
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
            # 窗口随内容长高（夹屏内）——满载内容不被裁掉，用户一眼看全
            self._fit_window()
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
        tier 缺省省略括号；轮序号用 round_id 直出（检测器界面首次出现即
        第 1 轮，与游戏轮次/日志同口径，N1 20261019）、先验档位标「先验」，
        旧快照缺字段回退下标。"""
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
                         round_no=rid,
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
