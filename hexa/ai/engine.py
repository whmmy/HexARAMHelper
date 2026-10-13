"""DecisionEngine：建议生成统一接口（计划 §7 ai/engine.py）

三实现：
- RuleDecisionEngine   纯规则（P0 行为，离线兜底，永远可用）
- AIDecisionEngine     System2(加载期) + System1(局内)；同步出规则底线建议，
                       异步 AI 精修经 on_result 回调覆盖（带 serial 防串局/防旧覆盖新）
- JevEngine            结构化评估模型占位（P3+，接口先留）

守门（计划 §4 / v4.2 触发模型）：
- budget_per_game  每局 AI 调用上限；<=0 = 不限（默认，2026-09-30
                   用户拍板），正数硬限超限自动纯规则。选人期 System2
                   预生成（prepare_game）计入本局预算（20261013 ctx 提前
                   派发案）；开局旧路径直派 System2 不计入（与旧行为一致）
- min_interval     periodic 类精修最小间隔；event 类（死亡/首海克斯/海克斯时刻）不受限
- 任何异常 → 保留规则建议，绝不阻塞 UI 线程（AI 全部跑后台线程）
"""
import threading
import time
from abc import ABC, abstractmethod

from hexa.ai.system2 import ContextCache
from hexa.core.rules_engine import Candidate, _is_fullstat_item

KIND_CTX = "ctx"                    # System2 上下文就绪（UI 刷新提示）
KIND_OPENING = "opening"
KIND_NEXT = "next_items"
KIND_RANKS = "augment_ranks"


class DecisionEngine(ABC):
    mode = "rule"

    # ── Live 免费事实挂载点（GameSession.on_snapshot 整体替换写入）──
    # 类属性仅作缺省（规则/占位引擎无 prompt 不消费）；GameSession 用
    # setattr 写实例属性，轮询线程写 / AI 后台线程读，整引用赋值原子。
    # 数据均为 Live API 原文直出（判断归模型，代码不做预消化结论）。
    enemy_spells: dict = {}   # 敌方英雄名 → [召唤师技能 displayName]
    ally_builds: list = []    # 队友出装摘要行 "英雄: 装备1、装备2"

    def bind(self, on_result):
        """on_result(kind, payload, serial) —— AI 结果回调（UI/会话线程安全由调用方保证）"""
        self.on_result = on_result

    @abstractmethod
    def start_game(self, hero: str, ally: list, enemy: list,
                   game_map: str = ""):
        """新局开始：重置预算/缓存；AI 实现此后台生成 ChampionContext。

        game_map: "11"=召唤师峡谷 / "12"=极地大乱斗（装备按图可买性过滤）
        """

    def prepare_game(self, hero: str, ally: list, enemy: list):
        """选人期预生成钩子（阵容锁定即后台派发，开局 start_game 复用）。

        默认无操作：规则引擎无 System2 上下文（纯规则模式行为零变化）。
        AIDecisionEngine 覆盖：按 (hero, ally, enemy) 键控去重/重派，
        派发计入本局预算。game_state.on_champ_select 锁外调用。"""

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
                   serial: int, desc_fetcher=None) -> tuple:
        """海克斯三卡片评级，返回 (基准评级, AI精修是否在途)。

        基准评级 [{name, grade, reason}] 同步可立即上屏；AI 在途=True 时
        UI 先显示卡名等评级（"先显卡名后放评级"），精修结果经
        on_result(KIND_RANKS) 回调覆盖（失败也回调 None 通知清等待态，
        规则基准成为最终展示）。desc_fetcher：卡面原文描述获取器
        （20261013 描述自举）——AI 精修线程内调用，None=无描述可用。"""

    @abstractmethod
    def skill_order(self, hero: str) -> str:
        """静态加点顺序一行（overlay 常驻）"""

    def pool_for(self, current_path: str):
        """当前路线的 AI 适配出装池（None=无，规则层回 OP.GG 池）。

        base 类默认 None：规则引擎无 System2 上下文。AIDecisionEngine 覆盖。
        GameSession 规则底线预演与本类内部派发各调一次，同源同参保一致。
        """
        return None

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
                        serial, reason="event", augments=None, pool=None):
        return self.rules.build_sequence(my_items, current_path, threat,
                                         hero=self._hero,
                                         game_map=self.game_map, pool=pool)

    def rank_cards(self, hero, card_names, taken, my_items, threat, serial,
                   desc_fetcher=None):
        return self.rules.rank_augments(hero, card_names, taken), False

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
        self._pending_since = 0.0  # System2 生成起点（挂死看门狗计时）
        self._ctx_failed = False   # 看门狗落位后置真（stats 显示"生成失败"）
        self._prepare_key = None   # 选人期预生成的阵容键（None=无预生成在途/就绪）

    # ── 守门 ──────────────────────────────────────────────────────

    def _try_reserve(self) -> bool:
        """原子占位：预算内则计数 +1 并返回 True（检查与计数同锁，防竞态超限）。

        budget_per_game <= 0 = 不限次（默认，2026-09-30 用户拍板：分级
        单价下单局成本可忽略，不设硬上限）；正数仍硬限，超限自动纯规则。
        消费方：局内 System1 三类精修 + 选人期 System2 预生成（开局复用
        不重复计入，开局旧路径直派的 System2 不计入）。"""
        budget = int(self.settings.get_float("ai", "budget_per_game", default=0))
        with self._lock:
            if budget > 0 and self._calls >= budget:
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
        精修失败/空结果也回调 on_result(kind, None, serial)：等待态消费方
        （如海克斯评级的"AI 评级中"）借此清等待、回落规则底线展示，
        否则失败时等待态永久滞留（2026-09-30 评级等待态案）。"""
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
                if not payload:
                    payload = None   # 空结果按失败口径：通知等待方回落规则底线
                if self.on_result:
                    if payload:
                        self._last_refine[kind] = time.time()
                    self.on_result(kind, payload, serial)
            except Exception as e:  # noqa: BLE001 —— 精修失败保留规则建议
                print(f"[WARN] AI 精修 {kind} 失败（保留规则建议）: {e}")
                if self.on_result:
                    self.on_result(kind, None, serial)
            finally:
                with self._lock:
                    self._inflight -= 1
        t = threading.Thread(target=run, daemon=True, name=f"AI-{kind}")
        t.start()
        self._tasks.append(t)
        self._tasks = [x for x in self._tasks if x.is_alive()][-8:]

    # ── 生命周期 ──────────────────────────────────────────────────

    def _enemy_detail(self) -> list:
        """敌方明细行 ["名(伤害,Role)[ 召唤师技能:X+Y]"]：start_game 名单 +
        champ_damage_profile + 快照召唤师技能（Live 免费事实直出，缺数据
        的英雄不带技能段）。

        _fmt_snapshot 的 enemy 管道此前恒为空（死参数），威胁只有计数摘要；
        补上名字+画像后 System1 才能做"全 AD 局早护甲"级别的语境判断。
        Role 标签（Tank/Support/Mage…）数据直出，治疗系/坦克识别交给模型；
        召唤师技能同样只给事实（displayName 原文），"这人是 XX 流"的结论
        由模型自己下。
        """
        out = []
        for name in self._enemy[:5]:
            prof = self.delegate.kb.champ_profile.get(name, {})
            dmg = prof.get("damage") or "?"
            tags = prof.get("tags") or []
            role = "/".join(tags[:2]) if tags else ""
            line = f"{name}({dmg}" + (f",{role})" if role else ")")
            spells = (self.enemy_spells or {}).get(name) or []
            if spells:
                line += f" 召唤师技能:{'+'.join(str(s) for s in spells)}"
            out.append(line)
        return out

    def _ally_detail(self) -> list:
        """队友出装摘要行（GameSession 快照直出，本处只透传+截断 5 人）。

        行形如 "牛头酋长: 圣物之盾、生命药水"（含我，全员直出）。规则/
        占位引擎该字段为类默认空列表——无 prompt 不消费，零开销。"""
        return [str(x) for x in (self.ally_builds or [])][:5]

    def _spawn_system2(self, hero: str, ally: list, enemy: list,
                       stale_check, tag: str):
        """后台生成 ChampionContext → cache.set(键控) → KIND_CTX 回调。

        stale_check 在结果落位前于锁内求值：开局路径按游戏代际（旧局结果
        整包丢弃）、选人期路径按阵容键（骰子换人/阵容变化即弃）。生成/
        解析异常在此兜底回调 KIND_CTX(source=error)，防线程静默死亡、
        pending 永挂（system2.generate 内部已兜 LLM/解析层，此处兜 kb 等
        意外异常）。"""
        kb = self.delegate.kb
        rules = self.delegate.rules

        def gen():
            with self._lock:
                self._inflight += 1
            try:
                ctx = self.s2.generate(kb, rules, self.client_s2, hero, ally,
                                       enemy, data_version=kb.builds_patch)
                with self._lock:
                    stale = bool(stale_check())
                if stale:
                    print(f"[AI] 丢弃{tag} ChampionContext"
                          f"（{ctx.hero}，代际/阵容已过期）")
                    return
                if ctx.hero != hero:
                    print(f"[AI] Context 英雄不符（{ctx.hero} != {hero}），丢弃")
                    return
                self.cache.set(ctx,
                               ContextCache.make_key(hero, ally, enemy))
                src = "AI 上下文已生成" if ctx.source == "ai" else "AI 失败，规则版上下文"
                print(f"[AI] System2 完成（{tag}，{ctx.source}）："
                      f"亲和{len(ctx.augment_affinity)}"
                      f" 出门{len(ctx.opening_items)} 方案{len(ctx.builds)} — {src}")
                if self.on_result:
                    self.on_result(KIND_CTX, {"source": ctx.source}, 0)
            except Exception as e:  # noqa: BLE001 —— generate 内部已兜
                # LLM/解析异常，此处兜 kb/规则层意外异常：漏掉即线程静默
                # 死亡、pending 永挂（直到看门狗落位），必须可见
                print(f"[WARN] System2 生成异常: {type(e).__name__}: {e}")
                if self.on_result:
                    self.on_result(KIND_CTX, {"source": "error"}, 0)
            finally:
                with self._lock:
                    self._inflight -= 1
        t = threading.Thread(target=gen, daemon=True, name="AI-System2")
        t.start()
        self._tasks.append(t)

    def prepare_game(self, hero: str, ally: list, enemy: list):
        """选人期预生成 ChampionContext（20261013 ctx 迟到案）。

        海克斯大乱斗第一个海克斯出现很早，原 GameStart 才派发的 ctx 迟到
        最伤。阵容锁定（my_hero 就位）即后台派发，开局 start_game 同键
        命中复用（零新增 LLM 调用、零等待）。

        - 按 (hero, ally, enemy) 键控：同键已就绪/在途 → 幂等跳过（选人
          期回调每 ~2s 刷新一次，靠这里去重）；键变化（骰子换人/阵容变化）
          → 旧 ctx 立即失效并重派，旧在途结果按键丢弃。
        - 预算守门：派发计入本局预算（budget_per_game>0 占 1 个名额，
          开局复用不重复计入；<=0 不限）。预算窗自选人期起算。
        - 英雄缺失/槽位未配置 → 静默跳过（开局路径照旧兜底，行为同旧）。
        """
        hero = str(hero or "")
        if not hero:
            return
        if not (self.client_s2.configured and self.client_s1.configured):
            return
        key = ContextCache.make_key(hero, ally, enemy)
        with self._lock:
            if (self._prepare_key == key
                    and (self.cache.ready_for(key)
                         or self.cache.pending_for(key))):
                return   # 同键已就绪/在途：幂等（选人期 2s 轮询去重）
            self._prepare_key = key
            self._calls = 0          # 新一局预算窗自选人期起算
            self._ctx_failed = False
        # 骰子换人/阵容变化：旧 ctx 立即失效（开局绝不复用错阵容的方案池）
        self.cache.invalidate()
        if not self._try_reserve():
            # 结构上仅在预算窗被并发占满时可达；守门语义保留（占不到名额
            # 就不派发，开局按旧路径兜底）
            print("[AI] 预算不足，跳过选人期 System2 预生成（开局照旧兜底）")
            return
        self.cache.start_pending(key)
        self._pending_since = time.time()
        self._spawn_system2(
            hero, list(ally or []), list(enemy or []),
            stale_check=lambda: self._prepare_key != key, tag="选人期")

    def start_game(self, hero, ally, enemy, game_map=""):
        self._hero = hero
        self._enemy = [str(x) for x in (enemy or [])]
        self.game_map = game_map or ""
        # 新局清空 Live 免费事实挂载（上一局快照残留；新局首帧快照即回填，
        # 空窗期 _enemy_detail 不带技能段/队友行为空——宁缺勿错局）
        self.enemy_spells = {}
        self.ally_builds = []
        self.delegate._hero = hero
        self.delegate.game_map = self.game_map
        self._last_refine = {"opening": 0.0, "next_items": 0.0,
                             "augment_ranks": 0.0}
        key = ContextCache.make_key(hero, ally, enemy)
        with self._lock:
            self._epoch += 1
            epoch = self._epoch
            reuse_ready = self.cache.ready_for(key)      # 选人期已生成同键 ctx
            reuse_pending = (not reuse_ready
                             and self.cache.pending_for(key))  # 同键生成在途
            if not reuse_ready and not reuse_pending:
                # 全新派发（未预生成/键失配）：旧语义整窗重置，费用窗口
                # 自开局起算（本路径 System2 派发不入预算，与旧行为一致）
                self._calls = 0
                self._prepare_key = None
                for _c in (self.client_s1, self.client_s2):
                    _u = getattr(_c, "usage", None)
                    if _u:
                        _u.new_game()
        if reuse_ready or reuse_pending:
            # 复用/等待选人期结果：开局零新增 LLM 调用。预算窗延续选人期
            # （预生成已计入）；费用窗口延续（预生成花费属本局）。就绪分支
            # 补发 KIND_CTX（复盘时间线留"开局复用"痕；session 此刻必为
            # 刚 reset 的空会话，回调只落日志无刷新副作用）；在途分支由
            # gen 线程落位时自会发，此处不重复。
            print(f"[AI] System2 开局"
                  f"{'复用选人期上下文' if reuse_ready else '等待选人期在途生成'}"
                  f"（零新增调用）")
            if reuse_ready and self.on_result:
                ctx = self.cache.get(key)
                if ctx is not None:
                    self.on_result(KIND_CTX, {"source": ctx.source}, 0)
            return
        # 新局开始必须无条件使上一局上下文失效（清缓存 + 置 pending），
        # 早退路径（hero 缺失 / 槽位未配置）也不例外：否则 LCU 迟到时
        # 缓存仍是上一局 ChampionContext，本局沿用上一局英雄的出门/路线/
        # 加点生成 prompt，直到 late_start 补开局才自愈
        self.cache.start_pending()
        self._pending_since = time.time()
        self._ctx_failed = False
        if not hero:
            print("[AI] 英雄未知，跳过 System2 上下文生成（可手动设英雄后重开局）")
            return
        if not (self.client_s2.configured and self.client_s1.configured):
            print("[AI] 槽位未配置完整，本局纯规则模式")
            return
        self._spawn_system2(hero, list(ally or []), list(enemy or []),
                            stale_check=lambda: epoch != self._epoch,
                            tag="开局")

    # ── AI 适配出装池（System2 产物 → 规则底线/System1 白名单） ────

    def pool_for(self, current_path: str):
        """ctx 就绪时返回当前路线适配方案池（pool_for 并集）；否则 None。

        供规则底线 build_sequence 作 pool 数据源（GameSession 预演与本类
        派发各调一次，同源同参）；ctx 未就绪/路线无匹配方案 → None 回落
        OP.GG 池。
        """
        ctx = self.cache.get()
        if ctx is None or self.cache.pending:
            return None
        return ctx.pool_for(current_path) or None

    def _extra_allowed(self) -> list:
        """System1 跨方案权衡白名单：全方案池并集（过滤地图不可买/杂项）。

        模型可把其他方案里的件（如另一流派的保命件/破甲件）提到 picks——
        名字仍全部来自 System2 方案（过 kb.valid_item），防幻觉边界不破。
        """
        ctx = self.cache.get()
        if ctx is None or self.cache.pending:
            return []
        kb = self.delegate.kb
        gm = self.game_map
        out = []
        for n in ctx.all_pools():
            if (n not in out and kb.item_available(n, gm)
                    and not _is_fullstat_item(kb, n)):
                out.append(n)
        return out

    # ── 三类建议（同步规则底线 + 异步 AI 精修） ────────────────────

    def _ctx_ready(self) -> bool:
        ctx = self.cache.get()
        if ctx is not None and not self.cache.pending:
            return True
        # System2 挂死看门狗：pending 超 180s 视为生成失败并落位——
        # LLM 心跳/渐进输出可绕过 read timeout（2026-09-30 牛头案：
        # qwen3 思考挂死 12 分钟，期间 System1 全瘫、浮窗永久"生成中"）。
        # 落位后规则底线照常兜底；真 ctx 若迟到 cache.set 仍会正常生效。
        if self.cache.pending and self._pending_since \
                and time.time() - self._pending_since > 180:
            self.cache.fail()
            self._pending_since = 0.0
            self._ctx_failed = True
            print("[WARN] System2 生成超 180s 未返回，按失败落位放行规则底线"
                  "（本轮无方案池/跨流派适配）")
        return False

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
        pool = self.pool_for(current_path)
        base = self.delegate.next_candidates(my_items, gold, current_path,
                                             threat, serial, reason,
                                             pool=pool)
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
                taken, base, enemy=self._enemy_detail(),
                allies=self._ally_detail(),
                extra_allowed=self._extra_allowed())
            return [Candidate(p["name"], p["reason"],
                              price=kb.items.get(p["name"], {}).get("gold", 0))
                    for p in picks
                    if kb.item_available(p["name"], gm)]
        self._submit(KIND_NEXT, serial, fn)
        return base

    def rank_cards(self, hero, card_names, taken, my_items, threat, serial,
                   desc_fetcher=None):
        base = self.delegate.rank_cards(hero, card_names, taken, my_items,
                                        threat, serial)[0]
        if not (self._ctx_ready() and self._try_reserve()):
            return base, False
        ctx = self.cache.get()
        summary = threat.summary() if threat else ""

        def fn():
            # 描述自举（20261013）：AI 线程内等描述 OCR（fetcher 自带超时
            # 放行）——派发时序/频次不变，只多等 ~1s 事实进 prompt
            descs = None
            if desc_fetcher is not None:
                try:
                    descs = desc_fetcher()
                except Exception as e:  # noqa: BLE001 —— 描述失败只降级
                    print(f"[WARN] 卡面描述获取失败，评级退回无描述: {e}")
            return self.s1.rank_augments(
                self.client_s1, ctx, hero, card_names, taken or [], my_items,
                summary, base, enemy=self._enemy_detail(),
                allies=self._ally_detail(), descriptions=descs)
        self._submit(KIND_RANKS, serial, fn)
        return base, True

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
                    "ai", "budget_per_game", default=0)),
                "cost_game": round(cost_game, 4),
                "cost_total": round(cost_total, 4),
                "inflight": self._inflight,
                "ctx_source": (ctx.source if ctx else
                               ("生成失败" if self._ctx_failed else
                                ("生成中" if self.cache.pending else "-")))}


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
                        serial, reason="event", augments=None, pool=None):
        return self.delegate.next_candidates(my_items, gold, current_path,
                                             threat, serial, reason, augments,
                                             pool=pool)

    def rank_cards(self, hero, card_names, taken, my_items, threat, serial,
                   desc_fetcher=None):
        return self.delegate.rank_cards(hero, card_names, taken, my_items,
                                        threat, serial, desc_fetcher)

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
