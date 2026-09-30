"""UI 设计 token 与控件工厂（Quiet Dark v3）

**全仓唯一色值来源**：任何 `#rrggbb` 只允许出现在本文件的 `Palette` 里，
`tests/smoke_offline.py` 的「调色板单源」断言把这条锁死——UI 层要加颜色
必须先加 token，不允许就地写死（2026-09-30 UI 重构：overlay.py 曾自带一套
重复色值，与本文件分叉）。

分层：
- `Palette` 色板：两级表面（surface / surface_inset）+ 三级文字（text /
  text_dim / text_mute）+ 单一强调色 accent + 语义色（仅档位与告警用）
- `Type` 字号：桌面（display/title/body/small/mono）+ overlay（hero/badge/
  body/meta），overlay 一律再乘 `overlay.ui_scale()`
- `Metrics` 间距：标尺 4/6/8/12/16，圆角恒 0（直角 + 1px 发丝线，故不需要
  圆角贴图/色键透明，overlay 的 alpha 与分层窗口机制完全不用碰）
- `GRADES` 档位色：键与引擎 `grade` 字段同源（S/A/B/C/?）

三个工厂：
- `card_frame()`  1px 发丝边卡片 + 可选 2px 左轨（层级靠左轨不靠颜色泛滥）
- `rule_row()`    段落标题 + 右侧自适应 1px 细线（替代「── 建议 ──」字符画）
- `grade_badge()` 15px 实色档位徽章 + 深底色字母（面积小=干扰低，扫视收益高）
"""
import datetime
import io
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from hexa import __version__

# ══════════════════════════════════════════════════════════════
# Palette —— 唯一色值来源
# ══════════════════════════════════════════════════════════════
Palette = {
    "bg":            "#0B0F13",   # 页面底色
    "surface":       "#131A21",   # 卡片
    "surface_inset": "#0E141A",   # 卡内凹陷区（日志/输入框/字段网格）
    "line":          "#1E2831",   # 1px 发丝边框
    "line_soft":     "#171F27",   # 卡内分隔线 / overlay 段落线
    "text":          "#E4EAF0",   # 主文本
    "text_dim":      "#8B98A6",   # 次级文本
    "text_mute":     "#64717F",   # 三级（段落标题/序号/footer）
    "accent":        "#58A6FF",   # 唯一强调色
    "accent_hvr":    "#79C0FF",
    "accent_dim":    "#2C4257",   # 强调色淡底（chip 底）
    "success":       "#3FB950",
    "warning":       "#D29922",
    "error":         "#F85149",
    "grade_b":       "#B7C3CE",   # 档位 B 由「正文白」降为次级灰，拉开 S>A>B>C
    "btn_hover":     "#1A222B",
    "scroll_thumb":  "#26313C",
}

# 档位色：键与引擎 grade 字段同源。模型可能给出表外值 → 一律 .get 兜底。
GRADES = {
    "S": Palette["success"],
    "A": Palette["accent"],
    "B": Palette["grade_b"],
    "C": Palette["warning"],
    "?": Palette["warning"],
}

# 阶段色（主窗口状态 pill / overlay 英雄行状态后缀）
STAGE_COLOR = {
    "idle":         "text_mute",
    "champ_select": "accent",
    "game_start":   "accent",
    "in_game":      "success",
}

# 海克斯时刻共享文案（overlay 与主窗口同口径，避免两处各写一份漂移）
PENDING_TEXT = "AI 评级中…"      # 块级：识别成功但 AI 精修在途
PENDING_WAIT = "等待 AI 评级"    # 行级：三行各自的占位理由
PICKED_MARK = "✓已选"
CIRCLED = "①②③"

# ══════════════════════════════════════════════════════════════
# Type / Metrics
# ══════════════════════════════════════════════════════════════
Type = {
    "display":  13,   # 桌面主输出标题
    "title":    10,   # 卡片标题
    "body":     10,   # 正文
    "small":     9,   # meta / footer
    "mono":      9,   # Consolas 数字与日志
    "ov_hero":  12,   # overlay 英雄行
    "ov_badge":  8,   # overlay 档位徽章字母
    "ov_body":   9,   # overlay 正文
    "ov_meta":   8,   # overlay 段落标题
}

Metrics = {
    "gap_xs":    4,
    "gap_s":     6,
    "gap_m":     8,
    "gap_l":    12,
    "pad_card": 10,   # 卡片内距
    "rail_w":    2,   # 左轨宽
    "badge":    15,   # 档位徽章边长
    "radius":    0,   # 圆角恒 0（见模块 docstring）
}

UI_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
MONO_FAMILY = "Consolas"
_FAMILY_CACHE = ""


def resolve_family(root=None) -> str:
    """按本机实际可用族取第一个候选（YaHei UI 缺失时退回 YaHei，再退 Segoe UI）。

    字体族缺失会让 Tk 静默换字体、与设计稿观感偏离，故启动时校准一次。
    """
    global _FAMILY_CACHE
    if _FAMILY_CACHE:
        return _FAMILY_CACHE
    try:
        avail = {f.lower() for f in tkfont.families(root)}
    except Exception:
        avail = set()
    for fam in UI_FAMILIES:
        if fam.lower() in avail:
            _FAMILY_CACHE = fam
            break
    else:
        _FAMILY_CACHE = UI_FAMILIES[1]
    return _FAMILY_CACHE


class Theme:
    """Palette/Type 的类属性入口（main_window / settings_window 沿用旧名调用）。"""

    BG          = Palette["bg"]
    BG_CARD     = Palette["surface"]          # 旧名 → 新 token
    BG_INPUT    = Palette["surface_inset"]
    SURFACE     = Palette["surface"]
    SURFACE_INSET = Palette["surface_inset"]
    LINE        = Palette["line"]
    LINE_SOFT   = Palette["line_soft"]
    ACCENT      = Palette["accent"]
    ACCENT_HVR  = Palette["accent_hvr"]
    ACCENT_DIM  = Palette["accent_dim"]
    SUCCESS     = Palette["success"]
    WARNING     = Palette["warning"]
    ERROR       = Palette["error"]
    TEXT        = Palette["text"]
    TEXT_DIM    = Palette["text_dim"]
    TEXT_MUTE   = Palette["text_mute"]
    BORDER      = Palette["line"]             # 旧名 → 新 token

    FAMILY      = UI_FAMILIES[0]
    FONT        = (UI_FAMILIES[0], Type["body"])
    FONT_BOLD   = (UI_FAMILIES[0], Type["body"], "bold")
    FONT_MONO   = (MONO_FAMILY, Type["mono"])
    MONO_FAMILY = MONO_FAMILY   # 同名上收：class 体右值先落到模块全局

    @classmethod
    def style_root(cls, root: tk.Misc):
        """注册全套 ttk 样式。

        2026-09-30：原来只配到 TEntry/TScrollbar，Combobox/Checkbutton/
        Progressbar 走 clam 默认外观（白边输入框、亮灰滚动条、灰按钮），
        本次补全并统一为「发丝边 + 凹陷底」。
        """
        fam = resolve_family(root)
        Theme.FAMILY = fam
        Theme.FONT = (fam, Type["body"])
        Theme.FONT_BOLD = (fam, Type["body"], "bold")
        cls.FONT, cls.FONT_BOLD = Theme.FONT, Theme.FONT_BOLD
        P = Palette
        root.configure(bg=P["bg"])
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=P["bg"], foreground=P["text"],
                        font=Theme.FONT, bordercolor=P["line"],
                        lightcolor=P["line"], darkcolor=P["line"])

        # 容器
        style.configure("TFrame", background=P["bg"])
        style.configure("Card.TFrame", background=P["surface"])
        style.configure("Inset.TFrame", background=P["surface_inset"])

        # 文字：bg / card / inset / mute 四套背景各一，避免「页面底色画在
        # 卡片上」形成深色矩形补丁（2026-09-30 实测：card #131A21 上曾画
        # #0B0F13 的标题，截图里是明显的深色方块）
        style.configure("TLabel", background=P["bg"], foreground=P["text"])
        style.configure("Dim.TLabel", background=P["bg"], foreground=P["text_dim"])
        style.configure("Mute.TLabel", background=P["bg"], foreground=P["text_mute"])
        style.configure("Accent.TLabel", background=P["bg"], foreground=P["accent"])
        style.configure("Warn.TLabel", background=P["bg"], foreground=P["warning"])
        style.configure("Card.TLabel", background=P["surface"], foreground=P["text"])
        style.configure("CardDim.TLabel", background=P["surface"],
                        foreground=P["text_dim"])
        style.configure("CardMute.TLabel", background=P["surface"],
                        foreground=P["text_mute"])
        style.configure("CardWarn.TLabel", background=P["surface"],
                        foreground=P["warning"])
        style.configure("Field.TLabel", background=P["surface_inset"],
                        foreground=P["text_mute"])
        style.configure("MuteInset.TLabel", background=P["surface_inset"],
                        foreground=P["text_mute"])
        style.configure("DimInset.TLabel", background=P["surface_inset"],
                        foreground=P["text_dim"])
        style.configure("Badge.TLabel", background=P["bg"], foreground=P["bg"])

        # 按钮：平 + 发丝边，无 clam 3D 浮雕
        style.configure("TButton", background=P["surface"], foreground=P["text"],
                        bordercolor=P["line"], lightcolor=P["line"],
                        darkcolor=P["line"], borderwidth=1, relief="flat",
                        focusthickness=0, focuscolor=P["accent"],
                        padding=(10, 3), font=Theme.FONT)
        style.map("TButton",
                  background=[("pressed", P["surface_inset"]),
                              ("active", P["btn_hover"]),
                              ("disabled", P["surface"])],
                  foreground=[("disabled", P["text_mute"])])
        style.configure("Accent.TButton", background=P["bg"],
                        foreground=P["accent"], bordercolor=P["accent_dim"],
                        lightcolor=P["accent_dim"], darkcolor=P["accent_dim"])
        style.map("Accent.TButton",
                  background=[("active", P["surface_inset"])])

        # 输入类
        style.configure("TEntry", fieldbackground=P["surface_inset"],
                        foreground=P["text"], bordercolor=P["line"],
                        lightcolor=P["line"], darkcolor=P["line"],
                        insertcolor=P["accent"], borderwidth=1, relief="flat",
                        padding=3)
        style.map("TEntry", bordercolor=[("focus", P["accent"])],
                  lightcolor=[("focus", P["accent"])])
        style.configure("TCombobox", fieldbackground=P["surface_inset"],
                        background=P["surface"], foreground=P["text"],
                        bordercolor=P["line"], lightcolor=P["line"],
                        darkcolor=P["line"], arrowcolor=P["text_mute"],
                        selectbackground=P["surface_inset"],
                        selectforeground=P["text"], borderwidth=1,
                        relief="flat", padding=3)
        style.map("TCombobox", fieldbackground=[("readonly", P["surface_inset"])],
                  bordercolor=[("focus", P["accent"])],
                  arrowcolor=[("active", P["text_dim"])])

        # 勾选
        style.configure("TCheckbutton", background=P["bg"], foreground=P["text"],
                        indicatorcolor=P["surface_inset"],
                        indicatorbackground=P["surface"],
                        indicatorforeground=P["accent"], focuscolor=P["accent"],
                        bordercolor=P["bg"], font=Theme.FONT)
        style.map("TCheckbutton",
                  background=[("active", P["bg"])],
                  indicatorbackground=[("selected", P["accent"]),
                                      ("active", P["btn_hover"])],
                  foreground=[("disabled", P["text_mute"])])

        # 滚动条：深色细条（原来只有 trough 被配，thumb 是亮灰）
        style.configure("TScrollbar", background=P["scroll_thumb"],
                        troughcolor=P["bg"], bordercolor=P["bg"],
                        lightcolor=P["bg"], darkcolor=P["bg"],
                        arrowcolor=P["line"], arrowsize=10, borderwidth=0)
        style.map("TScrollbar", background=[("active", P["line"])])

        # 进度条：4px 细条（成型度对比；配置从 main_window 就地样式上收）
        style.configure("BP.Horizontal.TProgressbar", background=P["success"],
                        troughcolor=P["surface_inset"], bordercolor=P["surface_inset"],
                        lightcolor=P["success"], darkcolor=P["success"],
                        thickness=4, borderwidth=0)
        style.configure("Horizontal.TProgressbar", background=P["accent"],
                        troughcolor=P["surface_inset"], bordercolor=P["surface_inset"],
                        thickness=4, borderwidth=0)


# ══════════════════════════════════════════════════════════════
# 三个工厂（层级靠留白/左轨/字重，不靠颜色泛滥）
# ══════════════════════════════════════════════════════════════
def card_frame(parent, rail=None, pad=None, bg=None, title=None, note=None,
               tick="line", pack=None):
    """1px 发丝边卡片，**返回内容容器**（把子控件加到返回值上）。

    外层底=line、内层底=surface 且内缩 1px → 边框即外层露出的那 1px，
    纯 tk 实现、不用贴图，缩放窗口不重绘不闪烁。
    rail: 左轨色 token 名（如 "accent"），给出即 2px 强调条。
    title/note: 标题行（短轨 + 标题 + 右侧注记）；tick 指定短轨色 token。
    pack: 外层的 pack 参数（如 dict(fill=tk.X, pady=(8, 0))）。

    **必须由本函数自己 pack 外层、只返回内层**：早期版本返回内层让调用方去
    pack，等于把已 pack 的控件再塞进另一个父容器，Tk 会把它从原父容器摘走，
    整张卡片塌成 0 高度（2026-09-30 实机出图时踩到：主窗口只剩状态行与
    footer，所有卡片消失）。
    """
    P = Palette
    pad = Metrics["pad_card"] if pad is None else pad
    surface = bg or P["surface"]
    outer = tk.Frame(parent, bg=P["line"])
    if rail:
        tk.Frame(outer, bg=P[rail], width=Metrics["rail_w"]).pack(
            side=tk.LEFT, fill=tk.Y)
    inner = tk.Frame(outer, bg=surface, padx=pad, pady=pad)
    inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
               padx=(0, 1), pady=1)
    if title or note:
        row = tk.Frame(inner, bg=surface)
        row.pack(anchor=tk.W, fill=tk.X)
        if tick:
            tk.Frame(row, bg=P[tick], width=Metrics["rail_w"],
                     height=Type["title"] + 2).pack(side=tk.LEFT, fill=tk.Y)
        if title:
            tk.Label(row, text=title, font=(Theme.FAMILY, Type["title"], "bold"),
                     bg=surface, fg=P["text_dim"]).pack(
                         side=tk.LEFT, padx=(Metrics["gap_s"], 0))
        if note:
            tk.Label(row, text=note, font=(Theme.FAMILY, Type["small"]),
                     bg=surface, fg=P["text_mute"]).pack(side=tk.RIGHT)
    if pack:
        outer.pack(**pack)
    return inner


def rule_row(parent, text, bg=None, size=None, gap=Metrics["gap_s"], dot=None):
    """段落标题 + 右侧自适应 1px 细线，替代「── 建议 ──」字符画。

    标题标签与状态点以 `row.rule_label` / `row.rule_dot` 暴露，供 500ms 重绘
    路径只改 config（destroy/recreate 会闪烁，见 main_window 的去重建惯例）。
    """
    P = Palette
    surface = bg or P["surface"]
    row = tk.Frame(parent, bg=surface)
    dot_frame = None
    if dot:
        dot_frame = tk.Frame(row, width=5, height=5, bg=dot)
        dot_frame.pack_propagate(False)
        dot_frame.pack(side=tk.LEFT, padx=(0, gap), pady=4)
    label = tk.Label(row, text=text,
                     font=(Theme.FAMILY, size or Type["ov_meta"]),
                     bg=surface, fg=P["text_mute"], anchor="w")
    label.pack(side=tk.LEFT)
    tk.Frame(row, height=1, bg=P["line_soft"]).pack(
        side=tk.LEFT, fill=tk.X, expand=True, padx=(gap, 0), pady=5)
    row.rule_label = label
    row.rule_dot = dot_frame
    return row


def grade_badge(parent, grade, size=None, fg=None):
    """15px 实色档位徽章。表外档位不抛异常（模型可能给出 S/A/B/C 之外的值），
    退回描边空心块——UI 异常绝不能冒泡冻结整帧（见 main_window._poll_ui 注释）。

    grade 传空串 = pending 占位（空心无字）；传 None = 未知（空心 + 问号）。
    字母标签挂在 `box.grade_label` 上，配 retint_badge 复用。
    """
    P = Palette
    size = size or Metrics["badge"]
    color = GRADES.get(grade)
    box = tk.Frame(parent, width=size, height=size, bg=color or P["line"])
    box.pack_propagate(False)
    label = tk.Label(box, text=(grade or "?"),
                     font=(Theme.FAMILY, Type["ov_badge"], "bold"),
                     bg=color or P["surface_inset"],
                     fg=fg or (P["bg"] if color else P["text_mute"]))
    label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
    box.grade_label = label
    return box


def retint_badge(box, grade):
    """复用徽章控件、只换底色与字母（500ms 重绘路径禁止 destroy/recreate）。"""
    P = Palette
    color = GRADES.get(grade)
    box.configure(bg=color or P["line"])
    label = getattr(box, "grade_label", None)
    if label is not None:
        label.configure(text=(grade or "?"), bg=color or P["surface_inset"],
                        fg=P["bg"] if color else P["text_mute"])


def left_rail_box(parent, color, bg=None, pad=(6, 5)):
    """左轨强调块：返回 (外层, 内容容器)。外层底=强调色、内容内缩 2px →
    露出的 2px 即左轨。改色只需 outer.configure(bg=...)。"""
    P = Palette
    outer = tk.Frame(parent, bg=color)
    inner = tk.Frame(outer, bg=bg or P["surface_inset"],
                     padx=pad[0], pady=pad[1])
    inner.pack(fill=tk.BOTH, expand=True, padx=(Metrics["rail_w"], 0))
    return outer, inner



def ov_font(kind="ov_body", bold=False, scale=1.0):
    """overlay 字体（字号乘分辨率缩放比，见 overlay.ui_scale）"""
    size = max(7, round(Type[kind] * scale))
    return (Theme.FAMILY, size, "bold") if bold else (Theme.FAMILY, size)


def stage_color(stage: str) -> str:
    """阶段 → 色 token 名（表外阶段退 text_dim）"""
    return Palette[STAGE_COLOR.get(stage, "text_dim")]


def version_label() -> str:
    """应用版本角标文案（唯一来源 `hexa.__version__`）。

    主窗口标题栏 + 状态行右端角标、设置窗口标题栏 + 底部注记都读这里；
    改动版本号只改 hexa/__init__.py 一处（pyproject 同步，见冒烟断言）。
    """
    return f"v{__version__}"


# ── emoji 清洗（UI 渲染边界）────────────────────────────────────────
# 逻辑层的文案自带彩色 emoji：game_state.py 满装替换建议用 🔄、
# rules_engine.py 攒钱提示用 💰、军师/评级用 🧠/⏳。core 属只读区（本次 UI
# 重构不碰决策逻辑），故在渲染边界统一剥离——全链路去 emoji 而零 core 改动。
# 黑白几何/符号字形保留：①②③ 卡序、✓✗ 识别结果、∞ 预算、⚙ 设置、→ 换装箭头。
_EMOJI_BMP = set("⚠⟳⏱⏲⏰⏳✅❌⚡🔔📌")


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F000 <= cp <= 0x1FAFF      # 绝大多数彩色 emoji 在星形平面
            or 0x2600 <= cp <= 0x27BF and ch in _EMOJI_BMP)


def strip_emoji(text: str) -> str:
    """剥掉彩色 emoji 并顺手收掉残留的行首尾空格。

    徽章里的档位字母、①②③ 卡序、✓/✗ 识别标记不受影响（不在剥离集合内）。
    """
    if not text:
        return ""
    out = "".join(ch for ch in text if not _is_emoji(ch))
    return "\n".join(line.strip() for line in out.splitlines()).strip()


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
