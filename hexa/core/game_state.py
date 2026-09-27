"""GameSession：单局游戏会话状态（跨线程回调汇聚点）

数据流：PhaseMachine(lcu) + LivePoller(live_api) 的回调 → 本类（内部加锁）
       → ui_state() 供 UI 线程轮询渲染。

承载：双方阵容 / 我的装备与死亡态 / 海克斯台账 / current_path / 时间线 /
     出门装两分支状态机（v4.1 §2）/ 触发器评估（死亡·敌方关键装·KDA）/
     DecisionEngine 编排（同步规则底线 + AI 异步精修，serial 防旧覆新）/
     建议历史与复盘日志（P3）。
"""
import json
import threading
import time
from dataclasses import dataclass, field

from hexa.ai import engine as engine_mod
from hexa.ai.engine import KIND_CTX, KIND_NEXT, KIND_OPENING, KIND_RANKS
from hexa.config import BASE_DIR
from hexa.core.rules_engine import Candidate


@dataclass
class Suggestion:
    kind: str                    # opening / next_items / augment / event_note
    candidates: list = field(default_factory=list)  # [Candidate]
    note: str = ""
    gold: float = 0.0            # 生成建议时的我方金币（next_items 展示用）
    source: str = "rule"         # rule / ai（AI 精修覆盖后标记）
    serial: int = 0              # 建议序号（AI 精修防旧覆新）
    ts: float = field(default_factory=time.time)

    def render(self) -> str:
        if not self.candidates:
            return self.note
        tag = " ·AI" if self.source == "ai" else ""
        if self.kind == "next_items":
            head = "出装主线" + tag
            if self.gold > 0:
                head += f"（金币 {self.gold:.0f}）"
            lines = [head]
            for i, c in enumerate(self.candidates[:3], 1):
                price = f"{c.price}G" if c.price else ""
                extra = f"，{c.afford_note}" if c.afford_note else ""
                tag_i = f"（{price}{extra}）" if price else ""
                lines.append(f"{'①②③'[i-1]} {c.name}{tag_i} —— {c.reason}")
            return "\n".join(lines)
        lines = []
        for i, c in enumerate(self.candidates[:3], 1):
            lines.append(f"[推荐{i}] {c.name} —— {c.reason}")
        if self.note:
            lines.append(f"({self.note})")
        return "\n".join(lines)


class GameSession:
    """单局生命周期：ChampSelect → GameStart → InProgress → EndOfGame/秒退重置"""

    def __init__(self, kb, rules, settings=None, fixture_dumper=None,
                 live_poller=None, engine=None):
        self.kb = kb
        self.rules = rules
        self.settings = settings
        self.dumper = fixture_dumper
        self.live = live_poller
        self.engine = engine or engine_mod.RuleDecisionEngine(kb, rules)
        self.engine.bind(self._on_engine_result)
        self._lock = threading.RLock()
        self.reset()

    # ── 会话重置 ──────────────────────────────────────────────────

    def reset(self):
        with self._lock:
            self.stage = "idle"           # idle/champ_select/game_start/in_game
            self.my_hero = None
            self.ally = []
            self.enemy = []
            self.game_map = ""            # 当前地图（"11"=峡谷/"12"=大乱斗）
            # 海克斯台账（记账 + 纠错的数据源）
            self.augments = []            # [{"name","tier","ts"}]
            # 我的实时态（live 快照维护）
            self.my_items = []
            self._prev_my_items = []
            self._prev_dead = False
            self._enemy_items_seen = set()
            self._kda_alerts = 0
            self.current_path = ""
            self.timeline = []
            self.suggestion = None        # 当前 overlay 展示的建议
            self.threat = None            # ThreatProfile
            self.opening_state = "wait_first_augment"  # wait/offered/bypassed
            self.first_augment = None
            self._my_gold = 0.0
            self._last_sig = None         # 快照签名（定时 diff 用）
            self._suppress_recheck = False  # 事件帧刚发过建议，本次 diff 跳过
            self._sug_serial = 0          # 建议序号（AI 精修防旧覆新）
            self._card_serial = 0         # 海克斯时刻评级序号
            self.card_ranks = []          # 当前海克斯三卡片评级 [{name,grade,reason}]
            # 海克斯识别状态（overlay 状态标识数据源：多局零识别成功曾无从判断）
            self.hex_active = False       # 三选一界面在屏（detector 回调）
            self.hex_recognized = False   # 本轮三卡已稳定识别（on_cards 已发射）
            self.hex_picked = None        # 本轮点击选中的卡 (idx, name)
            self.hex_last = ""            # 上一轮识别结果（界面关闭后保留展示）
            self.history = []             # 建议历史（复盘日志）
            self._game_t0 = time.time()
            self._log("session_reset")

    def _log(self, event, detail=""):
        self.timeline.append((time.time(), event, detail))
        print(f"[SESSION] {event} {detail}".rstrip())

    # ── PhaseMachine 回调（状态机线程） ────────────────────────────

    def on_champ_select(self, teams):
        with self._lock:
            my_hero = self.kb.validate_hero(teams.get("my_hero") or "")
            if self.stage in ("game_start", "in_game") or (
                    my_hero and my_hero != self.my_hero and self.stage == "champ_select"):
                # 骰子换人 / 异常回退：重置会话重新跟随
                self.reset()
            self.stage = "champ_select"
            # gameflow session 缺地图字段时保留旧值（拿得到为准）
            self.game_map = str(teams.get("map_id") or self.game_map)
            if my_hero and my_hero != self.my_hero:
                self.my_hero = my_hero
                self._log("hero_selected", my_hero)
            if self.dumper:
                self.dumper.dump_lcu("/lol-champ-select/v1/session", teams)

    def on_game_start(self, teams):
        with self._lock:
            self.reset()
            self.stage = "game_start"
            self.my_hero = self.kb.validate_hero(teams.get("my_hero") or "") or self.my_hero
            self.ally = [c for c in teams.get("ally", []) if self.kb.validate_hero(c)]
            self.enemy = [c for c in teams.get("enemy", []) if self.kb.validate_hero(c)]
            self.game_map = str(teams.get("map_id") or self.game_map)
            self._log("game_start",
                      f"me={self.my_hero} ally={len(self.ally)} "
                      f"enemy={len(self.enemy)} map={self.game_map or '?'}")
            # 敌方威胁画像（英雄粒度，装备实时部分随快照补）
            self.threat = self.rules.threat_profile(self.enemy)
            if self.dumper:
                self.dumper.start_session(self.my_hero or "unknown")
                self.dumper.dump_lcu("/lol-gameflow/v1/session", teams)
            # System2 ChampionContext（AI 引擎后台生成；规则引擎空操作）
            hero, ally, enemy = self.my_hero, list(self.ally), list(self.enemy)
            game_map = self.game_map

        # 引擎回调在锁外启动（内部再起线程，避免持锁起线程）
        self.engine.start_game(hero or "", ally, enemy, game_map=game_map)

    def replace_engine(self, new_engine):
        """设置保存后热切换引擎（重建 AI 客户端/预算态；规则↔AI 即时生效）"""
        with self._lock:
            self.engine = new_engine
            new_engine.game_map = self.game_map  # 换引擎不丢当前地图上下文
            self.engine.bind(self._on_engine_result)
        print(f"[SESSION] 引擎已切换: {new_engine.mode}")

    def on_in_progress(self):
        with self._lock:
            self.stage = "in_game"
            self._log("in_progress")
            if self.live:
                self.live.enter_game()

    def on_end_of_game(self, reason):
        with self._lock:
            self._log("end_of_game", reason)
            self._write_review_log()
            if self.live:
                self.live.leave_game()
            if self.dumper:
                self.dumper.end_session()
            self.reset()

    # ── LivePoller 回调（轮询线程） ────────────────────────────────

    def on_snapshot(self, snap):
        late_start = None
        with self._lock:
            if self.stage not in ("in_game", "game_start"):
                return
            # 我的英雄兜底（LCU 断连时 live 仍可跟随）
            if not self.my_hero and snap.my_champion:
                validated = self.kb.validate_hero(snap.my_champion)
                if validated:
                    self.my_hero = validated
                    # 中途启动（重启/断连恢复）未经历 game_start：
                    # 快照补齐阵容并补 start_game，否则引擎空转建议为空
                    self.ally = [p.champion for p in snap.allies()
                                 if p.champion and p.champion != validated]
                    self.enemy = [p.champion for p in snap.enemies()
                                  if p.champion]
                    self._log("hero_late_detected",
                              f"me={validated} ally={len(self.ally)} "
                              f"enemy={len(self.enemy)}")
                    late_start = (self.engine, validated,
                                  list(self.ally), list(self.enemy),
                                  self.game_map)
            self.my_items = [it["name"] for it in snap.my_items if it.get("name")]
            self._my_gold = float(snap.my_current_gold or 0)
            enemies = snap.enemies()
            # LCU 阵容缺失兜底（国服偶发/中途启动）：快照名单直接回填，
            # 否则威胁画像按空名单计算恒为"暂无数据"
            if not self.enemy and enemies:
                self.enemy = [p.champion for p in enemies if p.champion]
                self.ally = [p.champion for p in snap.allies()
                             if p.champion and p.champion != self.my_hero]
            # 敌方威胁画像实时补全（含敌方装备扫描）
            self.threat = self.rules.threat_profile(self.enemy, enemies)
            self._evaluate_triggers(snap, enemies)
            self._recompute_path()
            self._periodic_recheck(enemies, snap)
        # 锁外补 start_game（同 on_game_start 规矩：不持锁起线程）
        if late_start:
            engine, hero, ally, enemy, game_map = late_start
            engine.start_game(hero, ally, enemy, game_map=game_map)

    def _periodic_recheck(self, enemies, snap):
        """定时快照 diff（v4.2）：状态签名变化即重算建议，不再只靠死亡触发。

        签名 = (我方装备, 金币档 250G 粒度, 敌方装备, 死亡态)。
        规则层重算零成本，签名一变即刷；AI 模式下 periodic 类精修由
        引擎守门（min_interval + budget），事件类不受间隔限制。
        """
        if self.settings and not self.settings.get(
                "triggers", "periodic_recheck", default=True):
            return
        sig = (
            tuple(self.my_items),
            int(self._my_gold // 250),
            tuple(sorted(it.get("name", "") for p in enemies
                         for it in p.items if it.get("name"))),
            bool(snap.my_is_dead),
        )
        if sig == self._last_sig:
            return
        self._last_sig = sig
        # 死亡等事件帧刚发过建议（同签名内 event 分支已刷），本次例行跳过，
        # 防同帧 event+periodic 双发白烧 AI 预算
        if self._suppress_recheck:
            self._suppress_recheck = False
            return
        # 出门装等待期（首海克斯已记、未购装备）不推下一件
        if self.opening_state == "offered":
            if not self.my_items:
                return
            self.opening_state = "bypassed"
            self._log("opening_bypassed", "已购装备，转入下一件模式")
        # 事件提示 8s 内不被例行刷新覆盖
        if (self.suggestion and self.suggestion.kind == "event_note"
                and time.time() - self.suggestion.ts < 8):
            return
        if self.first_augment or self.my_items:
            self._suggest_next_items(reason="periodic")

    # ── 海克斯记账（P1 mouse_ledger 调用入口；冒烟测试直接调用） ───

    def record_augment(self, name: str, tier: str = "", card_idx=None):
        """记账入口。card_idx：点击的卡片序号（0-2），供 overlay"✓已选"标记。"""
        with self._lock:
            if not name:
                return False
            if card_idx is not None:
                self.hex_picked = (card_idx, name)
            if any(a["name"] == name for a in self.augments):
                return False  # 防重复记账
            self.augments.append({"name": name, "tier": tier, "ts": time.time()})
            self._log("augment_recorded", f"{tier} {name}")
            if self.first_augment is None:
                self.first_augment = name
            self._recompute_path()
            self._decide_opening_or_next()
            return True

    def revoke_augment(self, name: str):
        """台账纠错（主窗口点击区调用）；撤销首海克斯时联动状态一并回退"""
        with self._lock:
            before = len(self.augments)
            self.augments = [a for a in self.augments if a["name"] != name]
            if len(self.augments) == before:
                return False
            self._log("augment_revoked", name)
            if self.first_augment == name:
                self.first_augment = (self.augments[0]["name"]
                                      if self.augments else None)
                if self.opening_state == "offered":
                    # 出门装建议基于已撤销的海克斯，回退等待重新决策
                    self.opening_state = ("wait_first_augment"
                                          if not self.my_items else "bypassed")
                    self.suggestion = None
                    self._log("opening_rollback", "首海克斯被撤销")
            self._recompute_path()
            return True

    def set_hero_manual(self, query: str):
        """LCU 断连兜底：拼音/中文模糊匹配手动设英雄（main_window 输入框调用）"""
        matches, is_exact = self.kb.search_hero(query)
        if not matches:
            return None
        with self._lock:
            if self.my_hero != matches[0] and self.stage in ("game_start", "in_game"):
                # 局中换英雄视为极端兜底，重置对局数据
                augments = list(self.augments)
                self.reset()
                self.augments = augments
                self.stage = "in_game"
            elif self.stage == "idle":
                self.stage = "champ_select"
            self.my_hero = matches[0]
            self._log("hero_manual", matches[0])
        return matches[0]

    # ── 海克斯时刻（screen_detect 检测到三卡片 → 评级；记账入口 record_augment） ──

    def rank_augment_cards(self, card_names: list) -> list:
        """三张卡片基准评级（同步）+ AI 精修（异步经 _on_engine_result 覆盖）"""
        with self._lock:
            self._card_serial += 1
            serial = self._card_serial
            hero = self.my_hero or ""
            taken = [a["name"] for a in self.augments]
            items = list(self.my_items)
            threat = self.threat
        ranks = self.engine.rank_cards(hero, card_names, taken, items,
                                       threat, serial)
        with self._lock:
            if serial == self._card_serial:
                self.card_ranks = ranks
                # 能到这里的调用方（detector on_cards）已保证 ≥2 张有效库名
                self.hex_active = True
                self.hex_recognized = True
                self.hex_last = ""
        return ranks

    def clear_card_ranks(self):
        """海克斯界面消失后清除时刻评级（overlay 时刻块随之隐藏）"""
        with self._lock:
            self.card_ranks = []

    # ── 海克斯界面识别状态（detector on_state / reroll 回调） ──────

    def on_hex_ui(self, active: bool):
        """三选一界面出现/消失（检测线程调用）。消失时把本轮识别成败
        摘要留在 hex_last，供 overlay 在界面关闭后仍展示结果。"""
        with self._lock:
            if active:
                self.hex_active = True
                self.hex_recognized = False
                self.hex_picked = None
                self.hex_last = ""
            else:
                self.hex_active = False
                self.hex_last = ("上轮: 识别成功 ✓" if self.hex_recognized
                                 else "上轮: 未识别成功 ✗")

    def on_hex_reroll(self):
        """点击刷新按钮：本轮内容作废，回识别中状态（新内容稳定后重标记）"""
        with self._lock:
            if self.hex_active:
                self.hex_recognized = False
                self.hex_picked = None

    # ── 出门装两分支状态机（v4.1 §2：先海克斯后出门装） ────────────

    def _decide_opening_or_next(self):
        """首个海克斯记账后：未购买任何装备 → 出门装三选（结合刚选海克斯）；
        已购买 → 不再推出门装，转下一件。"""
        if self.opening_state != "wait_first_augment" or not self.first_augment:
            return
        if self.my_items:
            self.opening_state = "bypassed"
            self._log("opening_bypassed", "已购装备，直接进入下一件模式")
            self._suggest_next_items(reason="event")
        else:
            self.opening_state = "offered"
            serial = self._new_serial()
            threat = self.threat
            first = self.first_augment
            cands = self.engine.opening_candidates(first, threat, serial)
            note = f"首个海克斯【{first}】已联动"
            self._set_suggestion(Suggestion("opening", cands, note=note,
                                            serial=serial))
            self._log("opening_offered", first)

    def _suggest_next_items(self, reason: str = "event"):
        """出装主线序列（v4.2）：金币按序累计——够买第 1 件后剩余继续判第 2 件，
        兑现"钱够就连买两件"的场景；小件合成线有目标时置顶。

        reason: event（死亡/首海克斯后，不受 AI 间隔守门）| periodic（快照 diff）
        """
        threat = self.threat or self.rules.threat_profile(self.enemy)
        serial = self._new_serial()
        seq = self.engine.next_candidates(self.my_items, self._my_gold,
                                          self.current_path, threat, serial,
                                          reason=reason,
                                          augments=list(self.augments))
        target, note = self.rules.component_advice(self.my_items,
                                                   self.current_path,
                                                   game_map=self.game_map,
                                                   prefer=[c.name for c in seq])
        if target:
            if seq and seq[0].name == target:
                # 目标本就是主线第一件：合成提示并入理由，不重排不重复占位
                # （旧写法直接跳过，提示整个丢失）
                if note and note not in seq[0].reason:
                    seq[0].reason = f"{note}；{seq[0].reason}"
            else:
                tprice = self.kb.items.get(target, {}).get("gold", 0)
                seq = [Candidate(target, note, price=tprice)] + [
                    c for c in seq if c.name != target][:2]
        # 金币感知：序列定型后按顺序累计（先组装再算，避免插入项漏标）
        self._apply_afford(seq)
        self._set_suggestion(Suggestion("next_items", seq, gold=self._my_gold,
                                        serial=serial))

    # ── 建议落位（serial 管理 + 历史） ─────────────────────────────

    def _new_serial(self) -> int:
        with self._lock:
            self._sug_serial += 1
            return self._sug_serial

    def _apply_afford(self, seq):
        """金币按序累计填 afford_note；身上组件能合成该件时按价抵扣
        （腰带 1000G 抵千变者贾修），已被前面候选消耗的组件不重复抵。
        金币未知（0）时不标注。"""
        if self._my_gold <= 0:
            return
        remaining = self._my_gold
        pool = {n: self.kb.items.get(n, {}).get("gold", 0)
                for n in self.my_items}
        pool = {n: g for n, g in pool.items() if g}
        for c in seq:
            if not c.price:
                continue
            frm = {self.kb.item_id_to_name.get(f)
                   for f in self.kb.items.get(c.name, {}).get("from", [])}
            discount = 0
            for comp in list(pool):
                if comp in frm:
                    discount += pool.pop(comp)
            if remaining + discount >= c.price:
                c.afford_note = "可购买"
                # 商店不透支：组件价值只在合成时兑现，剩余按实际现金计
                remaining = max(0.0, remaining + discount - c.price)
            else:
                c.afford_note = f"还差{c.price - remaining - discount:.0f}"

    def _set_suggestion(self, sugg: Suggestion, serial: int = None):
        with self._lock:
            sugg.serial = serial if serial is not None else self._sug_serial
            self.suggestion = sugg
            self.history.append((time.time(), sugg.kind, sugg.render()))

    # ── AI 精修结果（引擎后台线程回调） ────────────────────────────

    def _on_engine_result(self, kind, payload, serial):
        with self._lock:
            if kind == KIND_CTX:
                self._log("engine_ctx", str(payload.get("source")))
                return
            if kind == KIND_RANKS:
                if serial == self._card_serial and payload:
                    self.card_ranks = payload
                    self._log("card_ranks_ai", str(len(payload)))
                return
            # opening / next_items：serial 匹配当前建议且类型一致才覆盖
            cur = self.suggestion
            if serial != self._sug_serial or cur is None:
                return
            if kind == KIND_OPENING and cur.kind == "opening":
                if payload:
                    cur.candidates = payload
                    cur.source = "ai"
                    cur.ts = time.time()
                    self._log("opening_ai", f"{len(payload)} 项")
            elif kind == KIND_NEXT and cur.kind == "next_items":
                if payload:
                    cur.candidates = payload
                    cur.source = "ai"
                    cur.ts = time.time()
                    # AI 重排后需按当前金币重算可买性（保序累计）
                    self._apply_afford(cur.candidates)
                    self._log("next_items_ai", f"{len(payload)} 项")

    # ── 触发器评估（死亡 / 敌方关键装备 / KDA 起飞） ───────────────

    def _evaluate_triggers(self, snap, enemies):
        triggers = (self.settings.get("triggers") if self.settings else None) or {}

        # 1. 我死亡（边沿）→ 立即重算出装主线（事件触发保留）
        if triggers.get("death_next_item", True) and snap.my_is_dead and not self._prev_dead:
            self._log("my_death", f"respawn={snap.my_respawn_timer:.0f}s")
            if self.first_augment or self.my_items:
                self._suggest_next_items(reason="event")
                self._suppress_recheck = True  # 同帧签名 diff 不再重复发
        self._prev_dead = snap.my_is_dead

        # 2. 敌方关键装备完成（集合 diff）→ 克制提示
        if triggers.get("enemy_key_item", True):
            now_items = {(p.champion or p.champion_en, it["name"])
                         for p in enemies for it in p.items if it.get("name")}
            fresh = now_items - self._enemy_items_seen
            self._enemy_items_seen |= now_items
            from hexa.core.rules_engine import KEY_ENEMY_ITEMS
            for champ, item in sorted(fresh):
                if item in KEY_ENEMY_ITEMS:
                    note = KEY_ENEMY_ITEMS[item]
                    self.suggestion = Suggestion(
                        "event_note", [Candidate(f"敌方 {champ} 出了 {item}", note)])
                    self.history.append(
                        (time.time(), "event_note", self.suggestion.render()))
                    self._log("enemy_key_item", f"{champ} {item}")

        # 3. 敌方 KDA 起飞（每局限 2 次）
        if triggers.get("enemy_kda", True) and self._kda_alerts < 2:
            threshold = float(triggers.get("kda_threshold", 3.0))
            for p in enemies:
                if p.kills >= 4 and p.deaths <= 1 and (p.kills / (p.deaths + 1)) >= threshold:
                    self._kda_alerts += 1
                    self.suggestion = Suggestion(
                        "event_note",
                        [Candidate(f"{p.champion} 已起飞 {p.kills}/{p.deaths}/{p.assists}",
                                   "考虑针对性装备（抗性/重伤/金身类）")])
                    self.history.append(
                        (time.time(), "event_note", self.suggestion.render()))
                    self._log("enemy_kda_alert", f"{p.champion} {p.kills}/{p.deaths}")
                    break

    # ── current_path 重估（层 0 确定性） ──────────────────────────

    def _recompute_path(self):
        aug_names = [a["name"] for a in self.augments]
        new_path = self.rules.infer_current_path(self.my_items, aug_names)
        if new_path and new_path != self.current_path:
            self.current_path = new_path
            self._log("current_path", new_path)

    # ── 复盘日志（P3：局终落盘，供赛后回顾） ──────────────────────

    def _write_review_log(self):
        try:
            out_dir = BASE_DIR / "log" / "games"
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(self._game_t0))
            safe_hero = "".join(
                ch for ch in (self.my_hero or "unknown")
                if ch.isalnum() or "\u4e00" <= ch <= "\u9fff") or "unknown"
            stats = {}
            try:
                stats = self.engine.stats()
            except Exception:
                pass
            doc = {
                "hero": self.my_hero,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(self._game_t0)),
                "duration_s": round(time.time() - self._game_t0, 1),
                "ally": list(self.ally), "enemy": list(self.enemy),
                "augments": list(self.augments),
                "final_items": list(self.my_items),
                "current_path": self.current_path,
                "threat": self.threat.summary() if self.threat else "",
                "engine": stats,
                "suggestions": [
                    {"ts": round(t, 1), "kind": k, "text": x}
                    for t, k, x in self.history[-60:]],
                "timeline": [
                    {"ts": round(t, 1), "event": e, "detail": d}
                    for t, e, d in self.timeline[-200:]],
            }
            path = out_dir / f"{ts}_{safe_hero}.json"
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            print(f"[SESSION] 复盘日志已写入 {path.name}")
        except Exception as e:
            print(f"[WARN] 复盘日志写入失败: {e}")

    # ── UI 消费 ───────────────────────────────────────────────────

    def data_freshness(self) -> dict:
        """数据新鲜度（版本翻译器底线：抓取日期常显，>7 天黄标）"""
        import datetime
        info = {"dd_version": self.kb.dd_version, "date": "-",
                "age_days": None, "builds_patch": self.kb.builds_patch,
                "builds_date": self.kb.builds_fetched}
        try:
            mtime = self.kb.data_dir.joinpath("hero_augments.csv").stat().st_mtime
            d = datetime.date.fromtimestamp(mtime)
            info["date"] = d.isoformat()
            info["age_days"] = (datetime.date.today() - d).days
        except OSError:
            pass
        return info

    def ui_state(self) -> dict:
        with self._lock:
            threat = self.threat.summary() if self.threat else ""
            drift = self.rules.path_drift(self.current_path, self.my_items)
            try:
                engine_stats = self.engine.stats()
            except Exception:
                engine_stats = {}
            skill = self.engine.skill_order(self.my_hero or "") if self.my_hero else ""
            return {
                "stage": self.stage,
                "my_hero": self.my_hero,
                "ally": list(self.ally),
                "enemy": list(self.enemy),
                "augments": [a["name"] for a in self.augments],
                "my_items": list(self.my_items),
                "current_path": self.current_path,
                "path_drift": drift,
                "threat": threat,
                "suggestion": self.suggestion.render() if self.suggestion else "",
                "suggestion_kind": self.suggestion.kind if self.suggestion else "",
                "suggestion_source": self.suggestion.source if self.suggestion else "",
                "card_ranks": [dict(r) for r in self.card_ranks],
                "hex_active": self.hex_active,
                "hex_recognized": self.hex_recognized,
                "hex_picked": (list(self.hex_picked)
                               if self.hex_picked else None),
                "hex_last": self.hex_last,
                "skill_order": skill,
                "dd_version": self.kb.dd_version,
                "engine": engine_stats,
            }
