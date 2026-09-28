"""DecisionEngine：建议生成统一接口（计划 §7 ai/engine.py）

三实现：
- RuleDecisionEngine   纯规则（P0 行为，离线兜底，永远可用）
- AIDecisionEngine     System2(加载期) + System1(局内)；同步出规则底线建议，
                       异步 AI 精修经 on_result 回调覆盖（带 serial 防串局/防旧覆盖新）
- JevEngine            结构化评估模型占位（P3+，接口先留）

守门（计划 §4 / v4.2 触发模型）：
- budget_per_game  System1 每局硬上限（超限自动纯规则）
- min_interval     periodic 类精修最小间隔；event 类（死亡/首海克斯/海克斯时刻）不受限
- 任何异常 → 保留规则建议，绝不阻塞 UI 线程（AI 全部跑后台线程）
"""
import threading
import time
from abc import ABC, abstractmethod

from hexa.core.rules_engine import Candidate

KIND_CTX = "ctx"                    # System2 上下文就绪（UI 刷新提示）
KIND_OPENING = "opening"
KIND_NEXT = "next_items"
KIND_RANKS = "augment_ranks"


class DecisionEngine(ABC):
    mode = "rule"

    def bind(self, on_result):
        """on_result(kind, payload, serial) —— AI 结果回调（UI/会话线程安全由调用方保证）"""
        self.on_result = on_result

    @abstractmethod
    def start_game(self, hero: str, ally: list, enemy: list,
                   game_map: str = ""):
        """新局开始：重置预算/缓存；AI 实现此后台生成 ChampionContext。

        game_map: "11"=召唤师峡谷 / "12"=极地大乱斗（装备按图可买性过滤）
        """

    @abstractmethod
    def opening_candidates(self, first_augment, threat, serial: int) -> list:
        """出门装候选（同步规则底线；AI 实现可再发异步精修）"""

    @abstractmethod
    def next_candidates(self, my_items, gold, current_path, threat,
                        serial: int, reason: str = "event",
                        augments: list = None) -> list:
        """出装主线候选（同步规则底线；reason: event/periodic 影响守门）"""

    @abstractmethod
    def rank_cards(self, hero, card_names, taken, my_items, threat,
                   serial: int) -> list:
        """海克斯三卡片评级 [{name, grade, reason}]（同步基准；AI 异步精修）"""

    @abstractmethod
    def skill_order(self, hero: str) -> str:
        """静态加点顺序一行（overlay 常驻）"""

    @abstractmethod
    def stats(self) -> dict:
        """费用仪表/状态（ui settings 面板消费）"""


class RuleDecisionEngine(DecisionEngine):
    mode = "rule"

    def __init__(self, kb, rules):
        self.kb = kb
        self.rules = rules
        self.on_result = None
        self._hero = ""  # start_game 前（如中途启动）快照回调仍会读取
        self.game_map = ""  # 当前对局地图（"11"/"12"），按图过滤可买装备

    def start_game(self, hero, ally, enemy, game_map=""):
        self._hero = hero
        self.game_map = game_map or ""

    def opening_candidates(self, first_augment, threat, serial):
        return self.rules.opening_candidates(self._hero, first_augment,
                                             threat, game_map=self.game_map)

    def next_candidates(self, my_items, gold, current_path, threat,
                        serial, reason="event", augments=None):
        return self.rules.build_sequence(my_items, current_path, threat,
                                         hero=self._hero,
                                         game_map=self.game_map)

    def rank_cards(self, hero, card_names, taken, my_items, threat, serial):
        return self.rules.rank_augments(hero, card_names, taken)

    def skill_order(self, hero):
        return self.kb.skill_advice(hero)["priority"]

    def stats(self):
        return {"mode": self.mode, "calls": 0, "budget": 0,
                "cost_game": 0.0, "cost_total": 0.0, "ctx_source": "-",
                "inflight": 0}


class AIDecisionEngine(DecisionEngine):
    """System2 + System1 双槽位编排（守门后调用，异常一律降级规则）"""

    mode = "ai"

    def __init__(self, delegate: RuleDecisionEngine, settings,
                 client_s2, client_s1, system2_mod, system1_mod):
        self.delegate = delegate
        self.settings = settings
        self.s2 = system2_mod
        self.s1 = system1_mod
        self.client_s2 = client_s2
        self.client_s1 = client_s1
        self.cache = system2_mod.ContextCache()
        self.on_result = None
        self._lock = threading.Lock()
        self._epoch = 0            # 游戏代际（换局 +1；旧局在途结果整包丢弃）
        self._calls = 0            # 本局 System1 调用计数（预算守门）
        self._last_refine = {"opening": 0.0, "next_items": 0.0,
                             "augment_ranks": 0.0}
        self._hero = ""
        self._enemy = []           # 敌方名单（System1 prompt 敌方明细数据源）
        self.game_map = ""         # 当前对局地图（"11"/"12"）
        self._tasks = []           # 后台线程引用（防 GC）
        self._inflight = 0         # 在途 AI 生成数（浮窗"生成中"标识）

    # ── 守门 ──────────────────────────────────────────────────────

    def _try_reserve(self) -> bool:
        """原子占位：预算内则计数 +1 并返回 True（检查与计数同锁，防竞态超限）"""
        budget = int(self.settings.get_float("ai", "budget_per_game", default=15))
        with self._lock:
            if self._calls >= budget:
                return False
            self._calls += 1
            return True

    def _interval_ok(self, kind: str, reason: str) -> bool:
        if reason != "periodic":
            return True
        min_iv = self.settings.get_float("ai", "min_interval", default=20)
        return time.time() - self._last_refine.get(kind, 0) >= min_iv

    def _submit(self, kind, serial, fn):
        """后台执行 AI 精修；结果经 on_result(kind, payload, serial) 回传。

        提交时捕获游戏代际——旧局迟到结果在回调前整包丢弃（防串局）。
        """
        with self._lock:
            epoch = self._epoch
            self._inflight += 1

        def run():
            try:
                payload = fn()
                with self._lock:
                    stale = epoch != self._epoch
                if stale:
                    print(f"[AI] 丢弃旧局精修结果 {kind}（代际过期）")
                    return
                if payload is not None and self.on_result:
                    self._last_refine[kind] = time.time()
                    self.on_result(kind, payload, serial)
            except Exception as e:  # noqa: BLE001 —— 精修失败保留规则底线
                print(f"[WARN] AI 精修 {kind} 失败（保留规则建议）: {e}")
            finally:
                with self._lock:
                    self._inflight -= 1
        t = threading.Thread(target=run, daemon=True, name=f"AI-{kind}")
        t.start()
        self._tasks.append(t)
        self._tasks = [x for x in self._tasks if x.is_alive()][-8:]

    # ── 生命周期 ──────────────────────────────────────────────────

    def _enemy_detail(self) -> list:
        """敌方明细行 ["名(伤害画像)"]：start_game 名单 + champ_damage_profile。

        _fmt_snapshot 的 enemy 管道此前恒为空（死参数），威胁只有计数摘要；
        补上名字+画像后 System1 才能做"全 AD 局早护甲"级别的语境判断。
        """
        out = []
        for name in self._enemy[:5]:
            dmg = self.delegate.kb.champ_profile.get(name, {}).get("damage") or "?"
            out.append(f"{name}({dmg})")
        return out

    def start_game(self, hero, ally, enemy, game_map=""):
        self._hero = hero
        self._enemy = [str(x) for x in (enemy or [])]
        self.game_map = game_map or ""
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
            self._calls = 0
        self._last_refine = {"opening": 0.0, "next_items": 0.0,
                             "augment_ranks": 0.0}
        # 两槽位用量各自独立计量：System1/2 单价不同，若共享实例，
        # stats() 会把同一份 tokens 按两套单价各算一遍（费用虚高近一倍）
        for _c in (self.client_s1, self.client_s2):
            _u = getattr(_c, "usage", None)
            if _u:
                _u.new_game()
        self.delegate._hero = hero
        self.delegate.game_map = self.game_map
        # 新局开始必须无条件使上一局上下文失效（清缓存 + 置 pending），
        # 早退路径（hero 缺失 / 槽位未配置）也不例外：否则 LCU 迟到时
        # 缓存仍是上一局 ChampionContext，本局沿用上一局英雄的出门/路线/
        # 加点生成 prompt，直到 late_start 补开局才自愈
        self.cache.start_pending()
        if not hero:
            print("[AI] 英雄未知，跳过 System2 上下文生成（可手动设英雄后重开局）")
            return
        if not (self.client_s2.configured and self.client_s1.configured):
            print("[AI] 槽位未配置完整，本局纯规则模式")
            return
        kb = self.delegate.kb
        rules = self.delegate.rules

        def gen():
            with self._lock:
                self._inflight += 1
            try:
                ctx = self.s2.generate(kb, rules, self.client_s2, hero, ally,
                                       enemy, data_version=kb.builds_patch)
                with self._lock:
                    stale = epoch != self._epoch
                if stale:
                    print(f"[AI] 丢弃旧局 ChampionContext（{ctx.hero}，代际过期）")
                    return
                if ctx.hero != hero:
                    print(f"[AI] Context 英雄不符（{ctx.hero} != {hero}），丢弃")
                    return
                self.cache.set(ctx)
                src = "AI 上下文已生成" if ctx.source == "ai" else "AI 失败，规则版上下文"
                print(f"[AI] System2 完成（{ctx.source}）：亲和{len(ctx.augment_affinity)}"
                      f" 出门{len(ctx.opening_items)} 路线{len(ctx.build_paths)} — {src}")
                if self.on_result:
                    self.on_result(KIND_CTX, {"source": ctx.source}, 0)
            finally:
                with self._lock:
                    self._inflight -= 1
        t = threading.Thread(target=gen, daemon=True, name="AI-System2")
        t.start()
        self._tasks.append(t)

    # ── 三类建议（同步规则底线 + 异步 AI 精修） ────────────────────

    def _ctx_ready(self) -> bool:
        return self.cache.get() is not None and not self.cache.pending

    def opening_candidates(self, first_augment, threat, serial):
        base = self.delegate.opening_candidates(first_augment, threat, serial)
        if not (self._ctx_ready() and self._try_reserve()):
            return base
        ctx = self.cache.get()
        kb = self.delegate.kb
        gm = self.game_map
        summary = threat.summary() if threat else ""
        taken = [first_augment] if first_augment else []

        def fn():
            picks = self.s1.suggest_opening(
                self.client_s1, ctx, self._hero, first_augment, summary, base,
                enemy=self._enemy_detail())
            out = []
            for p in picks:
                if not kb.item_available(p["name"], gm):
                    continue
                # combo 原样保留：Candidate 无 combo 字段，与规则版口径一致
                # （rules_engine.opening_candidates：组合构成入理由、价格按
                # 组合总价），此前丢字段且价格退化为单件价
                combo = p.get("combo") or [p["name"]]
                out.append(Candidate(
                    p["name"], f"{p['reason']}（{'+'.join(combo)}）",
                    price=sum(kb.items.get(n, {}).get("gold", 0)
                              for n in combo)))
            return out
        self._submit(KIND_OPENING, serial, fn)
        return base

    def next_candidates(self, my_items, gold, current_path, threat,
                        serial, reason="event", augments=None):
        base = self.delegate.next_candidates(my_items, gold, current_path,
                                             threat, serial, reason)
        if not (self._ctx_ready() and self._interval_ok(KIND_NEXT, reason)
                and self._try_reserve()):
            return base
        ctx = self.cache.get()
        kb = self.delegate.kb
        gm = self.game_map
        summary = threat.summary() if threat else ""
        taken = [a["name"] for a in (augments or [])]

        def fn():
            picks = self.s1.suggest_next(
                self.client_s1, ctx, my_items, gold, current_path, summary,
                taken, base, enemy=self._enemy_detail())
            return [Candidate(p["name"], p["reason"],
                              price=kb.items.get(p["name"], {}).get("gold", 0))
                    for p in picks
                    if kb.item_available(p["name"], gm)]
        self._submit(KIND_NEXT, serial, fn)
        return base

    def rank_cards(self, hero, card_names, taken, my_items, threat, serial):
        base = self.delegate.rank_cards(hero, card_names, taken, my_items,
                                        threat, serial)
        if not (self._ctx_ready() and self._try_reserve()):
            return base
        ctx = self.cache.get()
        summary = threat.summary() if threat else ""

        def fn():
            return self.s1.rank_augments(
                self.client_s1, ctx, hero, card_names, taken or [], my_items,
                summary, base, enemy=self._enemy_detail())
        self._submit(KIND_RANKS, serial, fn)
        return base

    # ── 查询 ──────────────────────────────────────────────────────

    def skill_order(self, hero):
        ctx = self.cache.get()
        if ctx and ctx.source == "ai":
            text = ctx.skill_order_text()
            if text:
                return text
        return self.delegate.skill_order(hero)

    def stats(self):
        u1 = getattr(self.client_s1, "usage", None)
        u2 = getattr(self.client_s2, "usage", None)
        p1i = self.settings.get_float("system1", "price_in", default=0.8)
        p1o = self.settings.get_float("system1", "price_out", default=2.7)
        p2i = self.settings.get_float("system2", "price_in", default=2.4)
        p2o = self.settings.get_float("system2", "price_out", default=9.6)
        cost_game = cost_total = 0.0
        calls = self._calls
        if u1:
            cost_game += u1.cost(p1i, p1o)
            cost_total += u1.cost(p1i, p1o, cumulative=True)
        if u2:
            cost_game += u2.cost(p2i, p2o)
            cost_total += u2.cost(p2i, p2o, cumulative=True)
        ctx = self.cache.get()
        return {"mode": self.mode, "calls": calls,
                "budget": int(self.settings.get_float(
                    "ai", "budget_per_game", default=15)),
                "cost_game": round(cost_game, 4),
                "cost_total": round(cost_total, 4),
                "inflight": self._inflight,
                "ctx_source": (ctx.source if ctx else
                               ("生成中" if self.cache.pending else "-"))}


class JevEngine(DecisionEngine):
    """Jev 结构化评估后端占位（P3+）：Choice/Score 原语 + 校准概率，零幻觉。

    接口与 RuleDecisionEngine 对齐，行为暂委托规则实现——替换点已收敛于此。
    """

    mode = "jev"

    def __init__(self, delegate: RuleDecisionEngine):
        self.delegate = delegate
        self.on_result = None
        self.game_map = ""

    def start_game(self, hero, ally, enemy, game_map=""):
        print("[Jev] 占位后端：行为等同规则模式（P3 接入）")
        self.game_map = game_map or ""
        self.delegate.start_game(hero, ally, enemy, game_map)

    def opening_candidates(self, first_augment, threat, serial):
        return self.delegate.opening_candidates(first_augment, threat, serial)

    def next_candidates(self, my_items, gold, current_path, threat,
                        serial, reason="event", augments=None):
        return self.delegate.next_candidates(my_items, gold, current_path,
                                             threat, serial, reason, augments)

    def rank_cards(self, hero, card_names, taken, my_items, threat, serial):
        return self.delegate.rank_cards(hero, card_names, taken, my_items,
                                        threat, serial)

    def skill_order(self, hero):
        return self.delegate.skill_order(hero)

    def stats(self):
        st = dict(self.delegate.stats())
        st["mode"] = self.mode
        return st


def build_engine(kb, rules, settings) -> DecisionEngine:
    """按 settings 决定规则/AI 引擎（app.py 启动与设置保存后调用）"""
    delegate = RuleDecisionEngine(kb, rules)
    if not settings.get("ai", "enabled", default=False):
        return delegate
    from hexa.ai import client as client_mod
    from hexa.ai import system1, system2
    # 不共享 UsageStats：两槽位单价不同，混在一起无法按槽位计价，
    # 且 stats() 会对同一份用量重复计费；LLMClient 缺省即自建独立实例
    c2 = client_mod.LLMClient(settings, "system2")
    c1 = client_mod.LLMClient(settings, "system1")
    if not (c1.configured and c2.configured):
        print("[AI] 已开启但槽位缺模型名/Key（settings 或环境变量），纯规则模式")
        return delegate
    return AIDecisionEngine(delegate, settings, c2, c1, system2, system1)
