"""设置窗口（计划 §4 模型双槽位 + §7 settings_window.py）

布局：AI 总开关 → System2/System1 两组槽位（模型下拉+自定义 / Key / base_url /
温度 / max_tokens / 思考强度 / 价格 / 测试连接）→ 全局（预算 / 精修间隔）→ 触发器开关 →
数据面板（DD 版本 / builds 版本与抓取日期 / 英雄先验抓取日期，任一 >7 天黄标）。
保存即写 settings.json 并回调 rebuild_engine（热切换规则/AI 模式）。

2026-09-30 UI 重构（Quiet Dark v3）——**只换皮肤，骨架不动**：
- `ttk.LabelFrame` 全部换成 `theme.card_frame` + 标题轨。clam 下 LabelFrame 的
  标题压在边框上、背景与卡片不同源，是主窗口「卡内标题深色补丁」同源问题
- 字段区放进 surface_inset 凹陷底；base_url / api_key 用等宽字体（便于核对
  key，掩码行为不变）
- 「测试连接」结果字符串与后台线程逻辑一行未动，只换呈现（绿/红/灰）
- 「保存并生效」用 accent 主按钮
- 控件、字段、保存/读取/测试逻辑、`_ProbeSettings` 语义全部原样
"""
import tkinter as tk
from tkinter import ttk

from hexa.config import DEFAULTS
from hexa.ui.theme import (Metrics, Palette, Theme, Type, card_frame,
                           strip_emoji, version_label)

MODEL_PRESETS = ["qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash",
                 "deepseek-v4-pro", "deepseek-v4.1-flash", "kimi-k3", "glm-5.3"]

# (界面标签, settings 值)；off=显式关闭（deepseek 默认思考烧配额，速度优先）
THINKING_OPTIONS = [("关闭", "off"), ("低", "low"), ("中", "medium"),
                    ("高", "high"), ("跟随模型默认", "auto")]
THINKING_LABELS = [label for label, _v in THINKING_OPTIONS]
_THINKING_BY_LABEL = dict(THINKING_OPTIONS)

# (key, label, width, is_number)  数值字段空/非法保存时回落 DEFAULTS
SLOT_FIELDS = [
    ("base_url", "Base URL", 46, False),
    ("api_key", "API Key（留空读环境变量）", 46, False),
    ("api_key_env", "Key 环境变量名", 14, False),
    ("temperature", "温度", 6, True),
    ("max_tokens", "max_tokens", 8, True),
    ("timeout", "超时(秒)", 6, True),
    ("price_in", "输入价 ¥/M tok", 8, True),
    ("price_out", "输出价 ¥/M tok", 8, True),
]

# 这些字段用等宽字体（核对 base_url / key 串更易，已是掩码的 api_key 除外）
MONO_KEYS = ("base_url", "api_key", "api_key_env")

# 卡片统一 pack 参数（card_frame 自己 pack 外层，见 theme.card_frame 注释）
SECTION_PACK = dict(fill=tk.X, pady=(10, 0))


class _ProbeSettings:
    """探针 settings：读穿透到真实配置，写只落内存（测试连接不污染 settings.json）。

    语义：先查本窗口表单值 → 再查真实 settings → resolve_api_key 同源环境变量。
    """

    def __init__(self, real):
        self.real = real
        self._tmp = {}

    def get(self, *keys, default=None):
        node = self._tmp
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return self.real.get(*keys, default=default)
            node = node[k]
        return node

    def get_float(self, *keys, default: float) -> float:
        """与 Settings.get_float 同语义（表单值穿透）"""
        try:
            return float(self.get(*keys, default=default))
        except (TypeError, ValueError):
            return default

    def set(self, *keys_and_value):
        *keys, value = keys_and_value
        node = self._tmp
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    def resolve_api_key(self, slot: str):
        """与 Settings.resolve_api_key 同语义，但读探针值（表单优先）"""
        import os
        key = self.get(slot, "api_key", default="") or ""
        if key.strip():
            return key.strip()
        env_name = self.get(slot, "api_key_env", default="DASHSCOPE_API_KEY")
        return (os.environ.get(env_name) or "").strip()


class SettingsWindow(tk.Toplevel):
    def __init__(self, root: tk.Tk, settings, kb, engine_stats_fn,
                 rebuild_engine_fn):
        super().__init__(root)
        self.settings = settings
        self.kb = kb
        self.engine_stats_fn = engine_stats_fn      # fn() -> dict（费用仪表）
        self.rebuild_engine_fn = rebuild_engine_fn  # fn() 保存后热切换引擎
        self.vars = {}
        self._test_results = {}
        self._test_labels = {}     # slot -> 结果标签（两个槽位各一个，不能共用）
        self.title(f"设置 — HexARAMHelper {version_label()}")
        self.geometry("720x860")
        self.configure(bg=Palette["bg"])
        Theme.style_root(self)
        self._build()
        # 卡片内距让内容比旧 LabelFrame 版略高，按内容请求高度自适应（下限 860），
        # 否则数据面板与保存行会被挤出窗口（固定 860 时底部两卡在 1080p 屏外）
        self.update_idletasks()
        self.geometry(f"720x{max(860, self.winfo_reqheight())}")
        self._load_values()
        self._poll_stats()

    # ── 控件构建 ──────────────────────────────────────────────────

    def _section(self, parent, title, note=None, pack=None):
        """统一卡片：标题轨（+右侧注记）+ 内容容器（替掉 ttk.LabelFrame）"""
        return card_frame(parent, title=title, note=note, pack=pack)

    def _build(self):
        P = Palette
        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        # AI 总开关（视觉权重最高，单独成卡）
        top = card_frame(outer, title="AI 总开关", pack=dict(fill=tk.X))
        self.vars["ai.enabled"] = tk.BooleanVar()
        ttk.Checkbutton(top, text="启用 AI（关闭 = 纯规则模式，无 Key 可用）",
                        variable=self.vars["ai.enabled"]).pack(anchor=tk.W,
                                                              pady=(4, 0))

        # 双槽位
        self._slot_frame(outer, "system2", "System 2 — ChampionContext 生成",
                         "每局 1 次")
        self._slot_frame(outer, "system1", "System 1 — 快照建议器", "每局 8~11 次")

        # 全局预算/间隔
        gb = self._section(outer, "全局守门", pack=SECTION_PACK)
        row = tk.Frame(gb, bg=P["surface"])
        row.pack(anchor=tk.W, pady=(4, 0))
        self.vars["ai.budget_per_game"] = tk.StringVar()
        self.vars["ai.min_interval"] = tk.StringVar()
        self._field_label(row, "每局 System1 上限(0=不限):")
        ttk.Entry(row, textvariable=self.vars["ai.budget_per_game"],
                  width=6).pack(side=tk.LEFT, padx=(4, 16))
        self._field_label(row, "periodic 精修最小间隔(秒):")
        ttk.Entry(row, textvariable=self.vars["ai.min_interval"],
                  width=6).pack(side=tk.LEFT, padx=(4, 0))

        # 触发器
        tg = self._section(outer, "局内触发器", pack=SECTION_PACK)
        row = tk.Frame(tg, bg=P["surface"])
        row.pack(anchor=tk.W, pady=(4, 0))
        for key, label in [("death_next_item", "死亡刷下一件"),
                           ("periodic_recheck", "快照 diff 重算"),
                           ("enemy_key_item", "敌方关键装提示"),
                           ("enemy_kda", "敌方起飞提示")]:
            self.vars[f"triggers.{key}"] = tk.BooleanVar()
            ttk.Checkbutton(row, text=label,
                            variable=self.vars[f"triggers.{key}"]).pack(
                                side=tk.LEFT, padx=(0, 12))
        self._field_label(row, "KDA 阈值:")
        self.vars["triggers.kda_threshold"] = tk.StringVar()
        ttk.Entry(row, textvariable=self.vars["triggers.kda_threshold"],
                  width=5).pack(side=tk.LEFT, padx=4)

        # 费用仪表
        cg = self._section(outer, "费用仪表", "本局 / 累计", pack=SECTION_PACK)
        self.lbl_cost = tk.Label(cg, text="-", font=Theme.FONT,
                                 bg=P["surface"], fg=P["text_dim"],
                                 anchor=tk.W, justify=tk.LEFT)
        self.lbl_cost.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

        # 数据面板
        dg = self._section(outer, "数据面板", "版本翻译器底线：新鲜度常显",
                          pack=SECTION_PACK)
        self.lbl_data = tk.Label(dg, text="-", font=Theme.FONT,
                                 bg=P["surface"], fg=P["text_dim"],
                                 anchor=tk.W, justify=tk.LEFT)
        self.lbl_data.pack(anchor=tk.W, fill=tk.X, pady=(4, 0))

        # 保存
        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="保存并生效", style="Accent.TButton",
                   command=self._save).pack(side=tk.RIGHT)
        tk.Label(btns, text="Key 仅存本地 settings.json（gitignore 排除）",
                 font=(Theme.FAMILY, Type["small"]), bg=P["bg"],
                 fg=P["text_mute"]).pack(side=tk.LEFT)
        tk.Label(btns, text=version_label(),
                 font=(Theme.FAMILY, Type["small"]), bg=P["bg"],
                 fg=P["text_mute"]).pack(side=tk.LEFT, padx=(10, 0))

    def _field_label(self, parent, text):
        """字段名：三级文本（与 design 稿一致，字段本身才是主信息）"""
        tk.Label(parent, text=text, font=(Theme.FAMILY, Type["small"]),
                 bg=Palette["surface"], fg=Palette["text_mute"]).pack(side=tk.LEFT)

    def _slot_frame(self, parent, slot, title, note=None):
        P = Palette
        box = card_frame(parent, title=title, note=note,
                         pack=dict(fill=tk.X, pady=(10, 0)))

        # 首行：模型 / 降级模型 / 测试连接
        row0 = tk.Frame(box, bg=P["surface"])
        row0.pack(fill=tk.X, pady=(6, 0))
        self.vars[f"{slot}.model"] = tk.StringVar()
        cb = ttk.Combobox(row0, textvariable=self.vars[f"{slot}.model"],
                          values=MODEL_PRESETS, width=20)
        cb.pack(side=tk.LEFT)
        self.vars[f"{slot}.fallback_model"] = tk.StringVar()
        self._field_label(row0, "降级模型:")
        ttk.Entry(row0, textvariable=self.vars[f"{slot}.fallback_model"],
                  width=16).pack(side=tk.LEFT, padx=4)
        btn = ttk.Button(row0, text="测试连接", command=lambda s=slot: self._test(s))
        btn.pack(side=tk.RIGHT)
        self.vars[f"{slot}._test"] = tk.StringVar(value="")
        lbl = tk.Label(row0, textvariable=self.vars[f"{slot}._test"],
                       font=(Theme.FAMILY, Type["small"]),
                       bg=P["surface"], fg=P["text_mute"])
        lbl.pack(side=tk.RIGHT, padx=8)
        self._test_labels[slot] = lbl

        # 字段网格：底色下沉到 inset，与卡片面分层
        grid_holder = tk.Frame(box, bg=P["surface_inset"], padx=Metrics["gap_m"],
                               pady=Metrics["gap_s"])
        grid_holder.pack(fill=tk.X, pady=(6, 0))
        grid = tk.Frame(grid_holder, bg=P["surface_inset"])
        grid.pack(fill=tk.X)
        for i, (key, label, width, is_num) in enumerate(SLOT_FIELDS):
            r, c = divmod(i, 2)
            tk.Label(grid, text=label + ":", font=(Theme.FAMILY, Type["small"]),
                     bg=P["surface_inset"], fg=P["text_mute"]).grid(
                         row=r, column=c * 2, sticky=tk.W, pady=2)
            self.vars[f"{slot}.{key}"] = tk.StringVar()
            ent = ttk.Entry(grid, textvariable=self.vars[f"{slot}.{key}"],
                            width=width)
            if key == "api_key":
                ent.config(show="*")
            if key in MONO_KEYS:
                ent.config(font=(Theme.MONO_FAMILY, Type["small"]))
            ent.grid(row=r, column=c * 2 + 1, sticky=tk.W, padx=(0, 18), pady=2)
        # 思考强度（紧跟字段网格下一格；off=关闭 low/medium/high=思考档 auto=跟默认）
        r, c = divmod(len(SLOT_FIELDS), 2)
        tk.Label(grid, text="思考强度:", font=(Theme.FAMILY, Type["small"]),
                 bg=P["surface_inset"], fg=P["text_mute"]).grid(
                     row=r, column=c * 2, sticky=tk.W, pady=2)
        self.vars[f"{slot}.thinking"] = tk.StringVar()
        ttk.Combobox(grid, textvariable=self.vars[f"{slot}.thinking"],
                     values=THINKING_LABELS, width=14,
                     state="readonly").grid(row=r, column=c * 2 + 1,
                                            sticky=tk.W, padx=(0, 18), pady=2)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

    # ── 读写 ──────────────────────────────────────────────────────

    def _load_values(self):
        s = self.settings
        self.vars["ai.enabled"].set(bool(s.get("ai", "enabled", default=False)))
        self.vars["ai.budget_per_game"].set(
            str(s.get("ai", "budget_per_game", default=0)))
        self.vars["ai.min_interval"].set(
            str(s.get("ai", "min_interval", default=20)))
        for key in ("death_next_item", "periodic_recheck", "enemy_key_item",
                    "enemy_kda"):
            self.vars[f"triggers.{key}"].set(
                bool(s.get("triggers", key, default=True)))
        self.vars["triggers.kda_threshold"].set(
            str(s.get("triggers", "kda_threshold", default=3.0)))
        for slot in ("system1", "system2"):
            for key, _label, _w, _n in SLOT_FIELDS:
                default = DEFAULTS.get(slot, {}).get(key, "")
                self.vars[f"{slot}.{key}"].set(
                    str(s.get(slot, key, default=default) or default))
            self.vars[f"{slot}.model"].set(str(s.get(slot, "model", default="")))
            self.vars[f"{slot}.fallback_model"].set(
                str(s.get(slot, "fallback_model", default="")))
            thinking = (s.get(slot, "thinking", default="off") or "off").strip().lower()
            label = dict((v, k) for k, v in THINKING_OPTIONS).get(thinking, "关闭")
            self.vars[f"{slot}.thinking"].set(label)
        self._render_data_panel()

    def _save(self):
        s = self.settings
        s.set("ai", "enabled", bool(self.vars["ai.enabled"].get()))
        s.set("ai", "budget_per_game", self._num("ai.budget_per_game", 0, int))
        s.set("ai", "min_interval", self._num("ai.min_interval", 20, float))
        for key in ("death_next_item", "periodic_recheck", "enemy_key_item",
                    "enemy_kda"):
            s.set("triggers", key, bool(self.vars[f"triggers.{key}"].get()))
        s.set("triggers", "kda_threshold",
              self._num("triggers.kda_threshold", 3.0, float))
        for slot in ("system1", "system2"):
            s.set(slot, "model", self.vars[f"{slot}.model"].get().strip())
            s.set(slot, "fallback_model",
                  self.vars[f"{slot}.fallback_model"].get().strip())
            s.set(slot, "thinking",
                  _THINKING_BY_LABEL.get(self.vars[f"{slot}.thinking"].get(),
                                         "off"))
            for key, _label, _w, is_num in SLOT_FIELDS:
                if is_num:
                    s.set(slot, key, self._num(
                        f"{slot}.{key}", DEFAULTS.get(slot, {}).get(key, 0),
                        float))
                else:
                    s.set(slot, key, self.vars[f"{slot}.{key}"].get().strip())
        print("[OK] 设置已保存")
        self.rebuild_engine_fn()

    def _num(self, var_key, default, typ):
        try:
            return typ(self.vars[var_key].get())
        except (TypeError, ValueError):
            return default

    # ── 测试连接（后台线程，绝不阻塞 Tk 主线程） ──────────────────

    def _test(self, slot):
        self.vars[f"{slot}._test"].set("测试中…")
        lbl = self._test_labels.get(slot)
        if lbl is not None:
            lbl.config(fg=Palette["text_mute"])
        import threading
        threading.Thread(target=self._test_bg, args=(slot,),
                         daemon=True, name=f"Test-{slot}").start()

    def _test_bg(self, slot):
        """表单当前值写入探针 settings（不落盘），后台发起真实请求"""
        probe = _ProbeSettings(self.settings)
        self._apply_slot_to(probe, slot)
        from hexa.ai.client import LLMClient
        result = LLMClient(probe, slot).test_connection()
        def done():
            try:
                lbl = self._test_labels.get(slot)
                color = Palette["text_mute"]
                # ✓/✗ 单色字形（原为彩色 emoji，与全链去 emoji 口径一致）；
                # 成功/失败由文字色承担
                if result.get("ok"):
                    self.vars[f"{slot}._test"].set(
                        f"✓ {result['latency']}s ({result['model']})")
                    color = Palette["success"]
                else:
                    self.vars[f"{slot}._test"].set(
                        f"✗ {result.get('error', '失败')[:40]}")
                    color = Palette["error"]
                if lbl is not None:
                    lbl.config(fg=color)
            except tk.TclError:
                pass  # 窗口已关闭
        try:
            self.after(0, done)
        except tk.TclError:
            pass

    def _apply_slot_to(self, target, slot):
        for key in ("model", "base_url", "api_key", "api_key_env"):
            target.set(slot, key, self.vars[f"{slot}.{key}"].get().strip())
        target.set(slot, "thinking",
                   _THINKING_BY_LABEL.get(self.vars[f"{slot}.thinking"].get(),
                                          "off"))

    # ── 仪表轮询 ──────────────────────────────────────────────────

    def _poll_stats(self):
        try:
            st = self.engine_stats_fn() or {}
            self.lbl_cost.config(text=(
                f"本局: {st.get('calls', 0)} 次调用 / ¥{st.get('cost_game', 0):.4f}   "
                f"累计: ¥{st.get('cost_total', 0):.4f}   "
                f"上下文: {st.get('ctx_source', '-')}   模式: {st.get('mode', '-')}"))
        except tk.TclError:
            return  # 窗口已销毁，终止轮询链
        except Exception:
            pass
        try:
            self.after(2000, self._poll_stats)
        except tk.TclError:
            pass

    def _render_data_panel(self):
        """数据面板：版本/新鲜度常显，>7 天黄标。

        N6 断链修复后英雄先验有了上游脚本，纳入与 builds 同款的当天监控
        （此前只盯 builds 目录 mtime，hero_augments.csv 是全项目最旧资产
        却无人报警）。任一资产缺文件/超 7 天 → 整卡黄标。
        """
        kb = self.kb
        import datetime

        def _fresh(rel: str):
            """(日期串, 天数)；缺文件 → (None, None)"""
            try:
                mtime = kb.data_dir.joinpath(rel).stat().st_mtime
                d = datetime.date.fromtimestamp(mtime)
                return d.isoformat(), (datetime.date.today() - d).days
            except OSError:
                return None, None

        builds_date, builds_age = _fresh("builds")
        prior_date, prior_age = _fresh("hero_augments.csv")
        stale = any(a is None or a > 7 for a in (builds_age, prior_age))
        color = Palette["warning"] if stale else Palette["text_dim"]
        prior_txt = ("未知" if prior_date is None
                     else f"{prior_date}，{prior_age} 天前")
        self.lbl_data.config(text=(
            f"DD 装备库: {kb.dd_version or '-'}   OP.GG builds: "
            f"{kb.builds_patch or '-'}（抓取 {kb.builds_fetched or builds_date or '未知'}）\n"
            f"英雄先验: {len(kb.hero_augments)} 名 / {len(kb.augment_names)} 海克斯"
            f"（抓取 {prior_txt}）   语义标签: {len(kb.augment_tags)}"
            f"   装备: {len(kb.items)}"),
            fg=color)
