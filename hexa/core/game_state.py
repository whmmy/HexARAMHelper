"""GameSession：单局游戏会话状态（跨线程回调汇聚点）

数据流：PhaseMachine(lcu) + LivePoller(live_api) 的回调 → 本类（内部加锁）
       → ui_state() 供 UI 线程轮询渲染。

承载：双方阵容 / 我的装备与死亡态 / 海克斯台账 / current_path / 时间线 /
     出门装两分支状态机（v4.1 §2）/ 触发器评估（死亡·敌方关键装·KDA）。
"""
import threading
import time
from dataclasses import dataclass, field

from hexa.core.rules_engine import Candidate


@dataclass
class Suggestion:
    kind: str                    # opening / next_items / augment / event_note
    candidates: list = field(default_factory=list)  # [Candidate]
    note: str = ""
    gold: float = 0.0            # 生成建议时的我方金币（next_items 展示用）
    ts: float = field(default_factory=time.time)

    def render(self) -> str:
        if not self.candidates:
            return self.note
        if self.kind == "next_items":
            head = "出装主线"
            if self.gold > 0:
                head += f"（金币 {self.gold:.0f}）"
            lines = [head]
            for i, c in enumerate(self.candidates[:3], 1):
                price = f"{c.price}G" if c.price else ""
                extra = f"，{c.afford_note}" if c.afford_note else ""
                tag = f"（{price}{extra}）" if price else ""
                lines.append(f"{'①②③'[i-1]} {c.name}{tag} —— {c.reason}")
            return "\n".join(lines)
        lines = []
        for i, c in enumerate(self.candidates[:3], 1):
            lines.append(f"[推荐{i}] {c.name} —— {c.reason}")
        if self.note:
            lines.append(f"({self.note})")
        return "\n".join(lines)


class GameSession:
    """单局生命周期：ChampSelect → GameStart → InProgress → EndOfGame/秒退重置"""

    def __init__(self, kb, rules, settings=None, fixture_dumper=None, live_poller=None):
        self.kb = kb
        self.rules = rules
        self.settings = settings
        self.dumper = fixture_dumper
        self.live = live_poller
        self._lock = threading.RLock()
        self.reset()

    # ── 会话重置 ──────────────────────────────────────────────────

    def reset(self):
        with self._lock:
            self.stage = "idle"           # idle/champ_select/game_start/in_game
            self.my_hero = None
            self.ally = []
            self.enemy = []
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
            self._log("game_start",
                      f"me={self.my_hero} ally={len(self.ally)} enemy={len(self.enemy)}")
            # 敌方威胁画像（英雄粒度，装备实时部分随快照补）
            self.threat = self.rules.threat_profile(self.enemy)
            if self.dumper:
                self.dumper.start_session(self.my_hero or "unknown")
                self.dumper.dump_lcu("/lol-gameflow/v1/session", teams)
            # P2 在此触发 System2 ChampionContext 生成（当前仅规则模式）

    def on_in_progress(self):
        with self._lock:
            self.stage = "in_game"
            self._log("in_progress")
            if self.live:
                self.live.enter_game()

    def on_end_of_game(self, reason):
        with self._lock:
            self._log("end_of_game", reason)
            if self.live:
                self.live.leave_game()
            if self.dumper:
                self.dumper.end_session()
            self.reset()

    # ── LivePoller 回调（轮询线程） ────────────────────────────────

    def on_snapshot(self, snap):
        with self._lock:
            if self.stage not in ("in_game", "game_start"):
                return
            # 我的英雄兜底（LCU 断连时 live 仍可跟随）
            if not self.my_hero and snap.my_champion:
                validated = self.kb.validate_hero(snap.my_champion)
                if validated:
                    self.my_hero = validated
            self.my_items = [it["name"] for it in snap.my_items if it.get("name")]
            self._my_gold = float(snap.my_current_gold or 0)
            # 敌方威胁画像实时补全（含敌方装备扫描）
            enemies = snap.enemies()
            self.threat = self.rules.threat_profile(self.enemy, enemies)
            self._evaluate_triggers(snap, enemies)
            self._recompute_path()
            self._periodic_recheck(enemies, snap)

    def _periodic_recheck(self, enemies, snap):
        """定时快照 diff（v4.2）：状态签名变化即重算建议，不再只靠死亡触发。

        签名 = (我方装备, 金币档 250G 粒度, 敌方装备, 死亡态)。
        P0 规则层重算零成本，签名一变即刷；P2 AI 模式在此基础上加
        调用间隔与预算守门（triggers.periodic_recheck 可关）。
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
            self._suggest_next_items()

    # ── 海克斯记账（P1 mouse_ledger 调用入口；冒烟测试直接调用） ───

    def record_augment(self, name: str, tier: str = ""):
        with self._lock:
            if not name:
                return False
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
        """台账纠错（P3 UI 点击区调用）"""
        with self._lock:
            before = len(self.augments)
            self.augments = [a for a in self.augments if a["name"] != name]
            if len(self.augments) != before:
                self._log("augment_revoked", name)
                self._recompute_path()
                return True
            return False

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

    # ── 出门装两分支状态机（v4.1 §2：先海克斯后出门装） ────────────

    def _decide_opening_or_next(self):
        """首个海克斯记账后：未购买任何装备 → 出门装三选（结合刚选海克斯）；
        已购买 → 不再推出门装，转下一件。"""
        if self.opening_state != "wait_first_augment" or not self.first_augment:
            return
        if self.my_items:
            self.opening_state = "bypassed"
            self._log("opening_bypassed", "已购装备，直接进入下一件模式")
            self._suggest_next_items()
        else:
            self.opening_state = "offered"
            cands = self.rules.opening_candidates(self.my_hero, self.first_augment)
            self.suggestion = Suggestion(
                "opening", cands, note=f"首个海克斯【{self.first_augment}】已联动")
            self._log("opening_offered", self.first_augment)

    def _suggest_next_items(self):
        """出装主线序列（v4.2）：金币按序累计——够买第 1 件后剩余继续判第 2 件，
        兑现"钱够就连买两件"的场景；小件合成线有目标时置顶。"""
        threat = self.threat or self.rules.threat_profile(self.enemy)
        seq = self.rules.build_sequence(self.my_items, self.current_path, threat)
        target, note = self.rules.component_advice(self.my_items, self.current_path)
        if target and (not seq or seq[0].name != target):
            tprice = self.kb.items.get(target, {}).get("gold", 0)
            seq = [Candidate(target, note, price=tprice)] + [
                c for c in seq if c.name != target][:2]
        # 金币感知：序列定型后按顺序累计（先组装再算，避免插入项漏标）
        remaining = self._my_gold
        for c in seq:
            if self._my_gold > 0 and c.price:
                if remaining >= c.price:
                    c.afford_note = "可购买"
                    remaining -= c.price
                else:
                    c.afford_note = f"还差{c.price - remaining:.0f}"
        self.suggestion = Suggestion("next_items", seq, gold=self._my_gold)

    # ── 触发器评估（死亡 / 敌方关键装备 / KDA 起飞） ───────────────

    def _evaluate_triggers(self, snap, enemies):
        triggers = (self.settings.get("triggers") if self.settings else None) or {}

        # 1. 我死亡（边沿）→ 立即重算出装主线（事件触发保留）
        if triggers.get("death_next_item", True) and snap.my_is_dead and not self._prev_dead:
            self._log("my_death", f"respawn={snap.my_respawn_timer:.0f}s")
            if self.first_augment or self.my_items:
                self._suggest_next_items()
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
                    self._log("enemy_kda_alert", f"{p.champion} {p.kills}/{p.deaths}")
                    break

    # ── current_path 重估（层 0 确定性） ──────────────────────────

    def _recompute_path(self):
        aug_names = [a["name"] for a in self.augments]
        new_path = self.rules.infer_current_path(self.my_items, aug_names)
        if new_path and new_path != self.current_path:
            self.current_path = new_path
            self._log("current_path", new_path)

    # ── UI 消费 ───────────────────────────────────────────────────

    def data_freshness(self) -> dict:
        """数据新鲜度（版本翻译器底线：抓取日期常显，>7 天黄标）"""
        import datetime
        info = {"dd_version": self.kb.dd_version, "date": "-", "age_days": None}
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
                "dd_version": self.kb.dd_version,
            }
