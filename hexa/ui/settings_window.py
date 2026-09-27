"""设置窗口（计划 §4 模型双槽位 + §7 settings_window.py）

布局：AI 总开关 → System2/System1 两组槽位（模型下拉+自定义 / Key / base_url /
温度 / max_tokens / 思考强度 / 价格 / 测试连接）→ 全局（预算 / 精修间隔）→ 触发器开关 →
数据面板（DD 版本 / builds 版本与抓取日期，>7 天黄标）。
保存即写 settings.json 并回调 rebuild_engine（热切换规则/AI 模式）。
"""
import tkinter as tk
from tkinter import ttk

from hexa.config import DEFAULTS
from hexa.ui.theme import Theme

MODEL_PRESETS = ["qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash",
                 "deepseek-v4-pro", "deepseek-v4.1-flash", "kimi-k3", "glm-5.3"]

# (界面标签, settings 值)；off=显式关闭（deepseek 默认思考烧配额，速度优先）
THINKING_OPTIONS = [("关闭", "off"), ("低", "low"), ("中", "medium"),
                    ("高", "high"), ("跟随模型默认", "auto")]
THINKING_LABELS = [label for label, _v in THINKING_OPTIONS]
_THINKING_BY_LABEL = dict(THINKING_OPTIONS)

SLOT_FIELDS = [
    # (key, label, width, is_number)  数值字段空/非法保存时回落 DEFAULTS
    ("base_url", "Base URL", 46, False),
    ("api_key", "API Key（留空读环境变量）", 46, False),
    ("api_key_env", "Key 环境变量名", 14, False),
    ("temperature", "温度", 6, True),
    ("max_tokens", "max_tokens", 8, True),
    ("timeout", "超时(秒)", 6, True),
    ("price_in", "输入价 ¥/M tok", 8, True),
    ("price_out", "输出价 ¥/M tok", 8, True),
]


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
        self.title("设置 — HexARAMHelper")
        self.geometry("720x860")
        self.configure(bg=Theme.BG)
        Theme.style_root(self)
        self._build()
        self._load_values()
        self._poll_stats()

    # ── 控件构建 ──────────────────────────────────────────────────

    def _build(self):
        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        # AI 总开关
        top = ttk.Frame(outer)
        top.pack(fill=tk.X)
        self.vars["ai.enabled"] = tk.BooleanVar()
        ttk.Checkbutton(top, text="启用 AI（关闭 = 纯规则模式，无 Key 可用）",
                        variable=self.vars["ai.enabled"]).pack(side=tk.LEFT)

        # 双槽位
        self._slot_frame(outer, "system2", "System 2 — ChampionContext 生成（每局 1 次）")
        self._slot_frame(outer, "system1", "System 1 — 快照建议器（每局 8~11 次）")

        # 全局预算/间隔
        gb = ttk.LabelFrame(outer, text="全局守门", style="Card.TFrame")
        gb.pack(fill=tk.X, pady=(10, 0))
        row = ttk.Frame(gb)
        row.pack(fill=tk.X, padx=8, pady=6)
        self.vars["ai.budget_per_game"] = tk.StringVar()
        self.vars["ai.min_interval"] = tk.StringVar()
        ttk.Label(row, text="每局 System1 硬上限:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.vars["ai.budget_per_game"],
                  width=6).pack(side=tk.LEFT, padx=(4, 16))
        ttk.Label(row, text="periodic 精修最小间隔(秒):").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self.vars["ai.min_interval"],
                  width=6).pack(side=tk.LEFT, padx=4)

        # 触发器
        tg = ttk.LabelFrame(outer, text="局内触发器", style="Card.TFrame")
        tg.pack(fill=tk.X, pady=(10, 0))
        row = ttk.Frame(tg)
        row.pack(fill=tk.X, padx=8, pady=6)
        for key, label in [("death_next_item", "死亡刷下一件"),
                           ("periodic_recheck", "快照 diff 重算"),
                           ("enemy_key_item", "敌方关键装提示"),
                           ("enemy_kda", "敌方起飞提示")]:
            self.vars[f"triggers.{key}"] = tk.BooleanVar()
            ttk.Checkbutton(row, text=label,
                            variable=self.vars[f"triggers.{key}"]).pack(side=tk.LEFT,
                                                                        padx=(0, 12))
        ttk.Label(row, text="KDA 阈值:").pack(side=tk.LEFT)
        self.vars["triggers.kda_threshold"] = tk.StringVar()
        ttk.Entry(row, textvariable=self.vars["triggers.kda_threshold"],
                  width=5).pack(side=tk.LEFT, padx=4)

        # 费用仪表
        cg = ttk.LabelFrame(outer, text="费用仪表（本局 / 累计）", style="Card.TFrame")
        cg.pack(fill=tk.X, pady=(10, 0))
        self.lbl_cost = ttk.Label(cg, text="-", style="Dim.TLabel")
        self.lbl_cost.pack(anchor=tk.W, padx=8, pady=6)

        # 数据面板
        dg = ttk.LabelFrame(outer, text="数据面板（版本翻译器底线：新鲜度常显）",
                            style="Card.TFrame")
        dg.pack(fill=tk.X, pady=(10, 0))
        self.lbl_data = ttk.Label(dg, text="-", style="Dim.TLabel", justify=tk.LEFT)
        self.lbl_data.pack(anchor=tk.W, padx=8, pady=6)

        # 保存
        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btns, text="保存并生效", command=self._save).pack(side=tk.RIGHT)
        ttk.Label(btns, text="Key 仅存本地 settings.json（gitignore 排除）",
                  style="Dim.TLabel").pack(side=tk.LEFT)

    def _slot_frame(self, parent, slot, title):
        g = ttk.LabelFrame(parent, text=title, style="Card.TFrame")
        g.pack(fill=tk.X, pady=(10, 0))
        row0 = ttk.Frame(g)
        row0.pack(fill=tk.X, padx=8, pady=(8, 2))
        self.vars[f"{slot}.model"] = tk.StringVar()
        cb = ttk.Combobox(row0, textvariable=self.vars[f"{slot}.model"],
                          values=MODEL_PRESETS, width=20)
        cb.pack(side=tk.LEFT)
        self.vars[f"{slot}.fallback_model"] = tk.StringVar()
        ttk.Label(row0, text="降级模型:").pack(side=tk.LEFT, padx=(14, 0))
        ttk.Entry(row0, textvariable=self.vars[f"{slot}.fallback_model"],
                  width=16).pack(side=tk.LEFT, padx=4)
        btn = ttk.Button(row0, text="测试连接", command=lambda s=slot: self._test(s))
        btn.pack(side=tk.RIGHT)
        self.vars[f"{slot}._test"] = tk.StringVar(value="")
        ttk.Label(row0, textvariable=self.vars[f"{slot}._test"],
                  style="Dim.TLabel").pack(side=tk.RIGHT, padx=8)
        grid = ttk.Frame(g)
        grid.pack(fill=tk.X, padx=8, pady=(2, 8))
        for i, (key, label, width, is_num) in enumerate(SLOT_FIELDS):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label + ":").grid(row=r, column=c * 2,
                                                   sticky=tk.W, padx=(0, 4),
                                                   pady=2)
            self.vars[f"{slot}.{key}"] = tk.StringVar()
            ent = ttk.Entry(grid, textvariable=self.vars[f"{slot}.{key}"],
                            width=width)
            if key == "api_key":
                ent.config(show="*")
            ent.grid(row=r, column=c * 2 + 1, sticky=tk.W, padx=(0, 18), pady=2)
        # 思考强度（紧跟字段网格下一格；off=关闭 low/medium/high=思考档 auto=跟默认）
        r, c = divmod(len(SLOT_FIELDS), 2)
        ttk.Label(grid, text="思考强度:").grid(row=r, column=c * 2,
                                               sticky=tk.W, padx=(0, 4), pady=2)
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
            str(s.get("ai", "budget_per_game", default=15)))
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
        s.set("ai", "budget_per_game", self._num("ai.budget_per_game", 15, int))
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
                if result.get("ok"):
                    self.vars[f"{slot}._test"].set(
                        f"✅ {result['latency']}s ({result['model']})")
                else:
                    self.vars[f"{slot}._test"].set(
                        f"❌ {result.get('error', '失败')[:40]}")
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
        kb = self.kb
        import datetime
        try:
            mtime = kb.data_dir.joinpath("builds").stat().st_mtime
            date = datetime.date.fromtimestamp(mtime).isoformat()
            age = (datetime.date.today() - datetime.date.fromtimestamp(mtime)).days
            color = Theme.WARNING if age > 7 else Theme.TEXT_DIM
        except OSError:
            date, age, color = "未知", None, Theme.WARNING
        self.lbl_data.config(text=(
            f"DD 装备库: {kb.dd_version or '-'}   OP.GG builds: "
            f"{kb.builds_patch or '-'}（抓取 {kb.builds_fetched or date}）\n"
            f"英雄先验: {len(kb.hero_augments)}   海克斯名单: {len(kb.augment_names)}"
            f"   语义标签: {len(kb.augment_tags)}   装备: {len(kb.items)}"),
            foreground=color)
