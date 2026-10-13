"""GameSession：单局游戏会话状态（跨线程回调汇聚点）

数据流：PhaseMachine(lcu) + LivePoller(live_api) 的回调 → 本类（内部加锁）
       → ui_state() 供 UI 线程轮询渲染。

承载：双方阵容 / 我的装备与死亡态 / 海克斯台账 / current_path / 时间线 /
     出门装两分支状态机（v4.1 §2）/ 触发器评估（死亡·敌方关键装·KDA）/
     DecisionEngine 编排（同步规则底线 + AI 异步精修，serial 防旧覆新）/
     建议历史与复盘日志（P3）/ 己方阵容缺口 gap + 军师提示滚动列表
     （v4.5：阵容缺口/敌方缺口反向/顺逆风/攒钱与替换提醒，去重节流）。
"""
import json
import threading
import time
from dataclasses import dataclass, field

from hexa.ai import engine as engine_mod
from hexa.ai.engine import KIND_CTX, KIND_NEXT, KIND_OPENING, KIND_RANKS
from hexa.config import BASE_DIR
from hexa.core.rules_engine import (LEGENDARY_GOLD, Candidate, SwapCandidate,
                                    is_full_build)

# ── 军师提示（v4.5）口径常量 ─────────────────────────────────────
COACH_MAX_NOTES = 3            # 滚动保留最近 3 条（任务口径）
COACH_REPEAT_COOLDOWN = 60.0   # 同 key 提示最小重复间隔：5s 轮询下防状态回摆刷屏
WIND_EDGE = 6.0                # 顺/逆风判定分界（动量差 + 权重×人均传说件差）
WIND_ITEM_WEIGHT = 4.0         # 装备成型度差权重：一件传说件差 ≈ 4 分动量差


@dataclass
class Suggestion:
    kind: str                    # opening / next_items / swap_items / augment / event_note
    candidates: list = field(default_factory=list)  # [Candidate]（swap_items 为 [SwapCandidate]）
    note: str = ""
    gold: float = 0.0            # 生成建议时的我方金币（next_items/swap_items 展示用）
    source: str = "rule"         # rule / ai（AI 精修覆盖后标记）
    serial: int = 0              # 建议序号（AI 精修防旧覆新）
    ts: float = field(default_factory=time.time)

    def render(self) -> str:
        if not self.candidates:
            return self.note
        tag = " ·AI" if self.source == "ai" else ""
        if self.kind == "swap_items":
            # 满六神装替换建议（v4.4）：🔄 卖X → 买Y（差价N金），与
            # "下一件"视觉区分（UI 层另以琥珀色标识）
            head = "满六神装 · 替换建议" + tag
            if self.gold > 0:
                head += f"（金币 {self.gold:.0f}）"
            lines = [head]
            for i, c in enumerate(self.candidates[:3], 1):
                if not isinstance(c, SwapCandidate):
                    continue
                diff = c.price - c.sell_price
                # net_cost=None（金币未知，_apply_afford 同口径）不标注可买性，
                # 防"还差X金"假信息
                if c.net_cost is None:
                    aff = ""
                else:
                    aff = "现金足够" if c.net_cost <= 0 else f"还差{c.net_cost}金"
                tail = f"，{aff}" if aff else ""
                lines.append(f"{'①②③'[i-1]} 🔄 卖{c.sell} → 买{c.buy}"
                             f"（差价{diff}G{tail}）—— {c.reason}")
            if self.note:
                lines.append(f"({self.note})")
            return "\n".join(lines)
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
            # 攒钱决策附注（v4.5）：这波回城攒大件 vs 先买小件
            if self.note:
                lines.append(self.note)
            return "\n".join(lines)
        lines = []
        for i, c in enumerate(self.candidates[:3], 1):
            lines.append(f"[推荐{i}] {c.name} —— {c.reason}")
        if self.note:
            lines.append(f"({self.note})")
        return "\n".join(lines)


class GameSession:
    """单局生命周期：ChampSelect → GameStart → InProgress → EndOfGame/秒退重置"""

    DRAFT_INTERVAL = 20.0   # 复盘草稿落盘周期（进程死亡最多丢这么多秒）
    # 对局数据流停摆判定：快照断流超此时长即视为局已结束（终局监听兜底）
    STALL_TIMEOUT = 300.0

    def __init__(self, kb, rules, settings=None, fixture_dumper=None,
                 live_poller=None, engine=None, mouse_stats_fn=None):
        self.kb = kb
        self.rules = rules
        self.settings = settings
        self.dumper = fixture_dumper
        self.live = live_poller
        self.engine = engine or engine_mod.RuleDecisionEngine(kb, rules)
        self.mouse_stats_fn = mouse_stats_fn   # fn() -> [统计窗dict]（复盘用）
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
            self.hex_recorded_rounds = set()   # 已记账轮次（检测器 round_id，
                                               # 见 record_augment / round_recorded）
            # 已触发升级联动的轮次（升级联动案 20261013：同一轮次防重复
            # 触发建议刷新，见 record_augment）
            self._upgrade_fired_rounds = set()
            # 我的实时态（live 快照维护）
            self.my_items = []
            self.my_level = 0             # 我的英雄等级（快照直出，UI 英雄行）
            self._prev_my_items = []
            self._prev_dead = False
            self._enemy_items_seen = set()
            self._kda_alerts = 0
            self.current_path = ""
            self.timeline = []
            self.suggestion = None        # 当前 overlay 展示的建议
            self.threat = None            # ThreatProfile
            self.gap = None               # AllyGap 己方阵容缺口（军师/流派倾向）
            self.build_progress = None    # 成型度对比 {"ally":[行],"enemy":[行]}
                                           # （快照 playerlist 六神装进度，UI 面板数据源）
            self.coach_notes = []         # 军师提示滚动列表 [{"ts","key","text"}]
            self._coach_keys = {}         # 军师 key → 上次推送时间（冷却节流）
            self._coach_ctx_key = ""      # 最近一次实际发给模型的军师 key（AI 精修定向回填）
            self.swap_forecast = ""       # 替换预告（威胁 vs 已有装备错配，提示级）
            self.opening_state = "wait_first_augment"  # wait/offered/bypassed
            self.first_augment = None
            self._my_gold = 0.0
            self._last_sig = None         # 快照签名（定时 diff 用）
            self._suppress_recheck = False  # 事件帧刚发过建议，本次 diff 跳过
            self._sug_serial = 0          # 建议序号（AI 精修防旧覆新）
            self._card_serial = 0         # 海克斯时刻评级序号
            self.card_ranks = []          # 当前海克斯三卡片评级 [{name,grade,reason}]
            self.card_ranks_pending = False  # AI 评级精修在途（UI 先显卡名后放评级）
            # 海克斯识别状态（overlay 状态标识数据源：多局零识别成功曾无从判断）
            self.hex_active = False       # 三选一界面在屏（detector 回调）
            self.hex_recognized = False   # 本轮三卡已稳定识别（on_cards 已发射）
            self.hex_picked = None        # 本轮点击选中的卡 (idx, name)
            self.hex_last = ""            # 上一轮识别结果（界面关闭后保留展示）
            self.history = []             # 建议历史（复盘日志）
            self._review_flushed = False  # 本局复盘已落盘标记（双兜底防丢）
            self._engine_started = False  # 引擎本局已 start_game（快照兜底判据）
            self.clicks = []              # 点击链黑匣子（台账排障，见 log_click）
            self._game_t0 = time.time()
            # 复盘草稿周期落盘状态（进程死亡护身符，见 _maybe_flush_review_draft）
            self._review_path = None      # 本局复盘文件名（首写后整局稳定覆盖）
            self._draft_ts = 0.0          # 上次草稿写入时间（限频）
            # 数据流停摆兜底（终局监听不依赖 phase 查询，见 report_data_stall）
            self._last_data_ts = time.time()
            self._log("session_reset")

    def _log(self, event, detail=""):
        self.timeline.append((time.time(), event, detail))
        print(f"[SESSION] {event} {detail}".rstrip())

    # ── PhaseMachine 回调（状态机线程） ────────────────────────────

    def on_champ_select(self, teams):
        prep = None
        with self._lock:
            my_hero = self.kb.validate_hero(teams.get("my_hero") or "")
            # 上一局终局事件丢失（中途启动/秒退）时补写复盘（flush 幂等，
            # 正常路径已写过则此处空转）
            self.flush_pending_review()
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
            # System2 选人期预派发（20261013 ctx 迟到案：海克斯大乱斗第一个
            # 海克斯出现很早，GameStart 才派发的 ctx 迟到最伤）。阵容锁定
            # （my_hero 就位）即后台预生成，开局 start_game 同键命中复用；
            # 骰子换人/阵容变化由引擎按 (hero, ally, enemy) 键比对自动失效
            # 重派。纯规则引擎 prepare 为空操作，行为零变化。
            # 锁内只提取实参，锁外派发（同 on_game_start 规矩：不持锁起线程）
            if my_hero:
                prep = (self.engine, my_hero,
                        [c for c in teams.get("ally", [])
                         if c and self.kb.validate_hero(c)],
                        [c for c in teams.get("enemy", [])
                         if c and self.kb.validate_hero(c)])
        if prep:
            engine, hero, ally, enemy = prep
            engine.prepare_game(hero, ally, enemy)

    def on_game_start(self, teams):
        with self._lock:
            # 上一局终局事件丢失（直接开局/秒退跳过 EndOfGame）时补写复盘
            self.flush_pending_review()
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
            # 己方阵容缺口（v4.5 军师/流派倾向数据源；快照阶段每帧重算补全）
            self.gap = self.rules.ally_gap(self._ally_five(),
                                           self.my_hero or "")
            # 旧局军师上下文清空（System1 模块态桥接，防跨局残留）
            self._set_coach_context(clear=True)
            if self.dumper:
                self.dumper.start_session(self.my_hero or "unknown")
                self.dumper.dump_lcu("/lol-gameflow/v1/session", teams)
            # System2 ChampionContext（AI 引擎后台生成；规则引擎空操作）
            hero, ally, enemy = self.my_hero, list(self.ally), list(self.enemy)
            game_map = self.game_map

        # 引擎回调在锁外启动（内部再起线程，避免持锁起线程）
        self.engine.start_game(hero or "", ally, enemy, game_map=game_map)
        self._engine_started = True

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
            self._last_data_ts = time.time()   # 数据流停摆兜底的起点
            self._log("in_progress")
            if self.live:
                self.live.enter_game()
                # 海克斯落库探针与 enter_game 同点开新局（局号 +1）：
                # 20261013 §6 复核接线——探针属诊断件，new_game 炸了也
                # 不能影响 enter_game 主链，这里只防调用本身
                probe = getattr(self.live, "runes_probe", None)
                if probe is not None:
                    try:
                        probe.new_game()
                    except Exception as e:  # noqa: BLE001
                        print(f"[WARN] runes 探针 new_game 异常: {e}")

    def on_end_of_game(self, reason):
        with self._lock:
            # 幂等门：会话已收尾（data_stall 兜底先触发过/重复终局事件）
            # 就不拿空会话再写一份零内容文件、不重置已清场的新局状态
            if self.stage not in ("game_start", "in_game"):
                return
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
            self._last_data_ts = time.time()   # 快照到位 = 数据流活着
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
            # 我的等级（Live 免费事实直出；activeplayer 缺失时已由 poller
            # 用 playerlist 匹配条兜底，无数据保持 0，UI 省略 Lv.）
            self.my_level = int(getattr(snap, "my_level", 0) or 0)
            enemies = snap.enemies()
            # ── Live 免费事实喂模型（20261013：API 已返回但没进上下文的
            #    三件直出）——全部原文直出、不做代码侧预消化结论 ──
            # ①敌方召唤师技能：英雄名 → [displayName]，整体替换（轮询线程写
            #   /AI 线程读，GIL 下整引用赋值原子）；引擎按英雄名自取
            self.engine.enemy_spells = {
                (p.champion or p.champion_en): list(p.spells)
                for p in enemies
                if (p.champion or p.champion_en) and p.spells}
            # ③队友出装摘要：allies 逐人"英雄: 装备名列表"行（含我，直出
            # 全员；无装备标"无"），喂 System1 next/rank 上下文
            self.engine.ally_builds = [
                f"{p.champion or p.champion_en or '?'}: "
                + ("、".join(it["name"] for it in p.items if it.get("name"))
                   or "无")
                for p in snap.allies() if (p.champion or p.champion_en)]
            # LCU 阵容缺失兜底（国服偶发/中途启动）：快照名单直接回填，
            # 否则威胁画像按空名单计算恒为"暂无数据"
            if not self.enemy and enemies:
                self.enemy = [p.champion for p in enemies if p.champion]
                self.ally = [p.champion for p in snap.allies()
                             if p.champion and p.champion != self.my_hero]
            # 引擎开局兜底②：上面的中途启动兜底只救"快照时英雄未知"——
            # 选人期已锁定英雄的局（my_hero 早有值）错过 GameStart 转换后
            # 永远轮不到它，引擎整局空转（20260929 曙光女神案：复盘
            # ctx_source="-"、calls=0、费用 ¥0.0000）。按"本局引擎未启动"
            # 判据补 start_game，英雄已知也救；_engine_started 幂等防重入。
            if (self.stage == "in_game" and not self._engine_started
                    and self.my_hero):
                late_start = (self.engine, self.my_hero, list(self.ally),
                              list(self.enemy), self.game_map)
                self._engine_started = True
            # 成型度对比（v4.6）：playerlist 全员六神装进度（传说件 x/6 +
            # 装备总价值），敌我分组供主窗口面板；放阵容回填之后保证分组完整
            self.build_progress = {
                "ally": self._build_progress_rows(snap.allies()),
                "enemy": self._build_progress_rows(enemies),
            }
            # 敌方威胁画像实时补全（含敌方装备扫描）
            self.threat = self.rules.threat_profile(self.enemy, enemies)
            # 死亡实证挂载（/eventdata 谁在杀我 → resist_advice 破平局；
            # champ_damage 已由 threat_profile 本帧逐敌登记）
            self.threat.killer_counts = dict(
                getattr(snap, "killers_of_me", {}) or {})
            # 己方缺口重算（上方已回填阵容时随之更新），并挂到威胁对象上：
            # engine 层接口不透传 gap，借 threat 穿透到 build_sequence
            # （"缺前排 → tank 加分"链路，见 rules_engine.tank_lean）
            self.gap = self.rules.ally_gap(self._ally_five(),
                                           self.my_hero or "")
            self.threat.ally_gap = self.gap
            self._evaluate_triggers(snap, enemies)
            self._recompute_path()
            # 替换预告（v4.4，提示级不改主建议）：威胁画像每次快照全量
            # 重算，装备明显错配（敌 AP 成型而我无魔抗等）时提前预告；
            # 放 _recompute_path 之后，保证用的是本帧路线
            forecast = self.rules.swap_forecast(self.my_items,
                                                self.current_path, self.threat)
            if forecast != self.swap_forecast:
                self.swap_forecast = forecast
                if forecast:
                    self._log("swap_forecast", forecast)
            # 军师提示滚动维护（阵容缺口/敌方缺口反向/顺逆风；按 key 去重，
            # 5s 轮询下不重复刷）
            self._update_coach(snap)
            self._periodic_recheck(enemies, snap)
            # 复盘草稿周期落盘（进程死亡护身符；限频 20s，零内容不写）
            self._maybe_flush_review_draft()
        # 锁外补 start_game（同 on_game_start 规矩：不持锁起线程）
        if late_start:
            engine, hero, ally, enemy, game_map = late_start
            engine.start_game(hero, ally, enemy, game_map=game_map)

    def _build_progress_rows(self, players: list) -> list:
        """一组玩家的成型度行：传说件计数（x/6）+ 装备总价值（DD gold）。

        传说件沿用 LEGENDARY_GOLD 粗分口径（与顺逆风/满装判定同一把尺）；
        先过 permanent_items 剔除药水等消耗品——临时占格不该计入成型，
        估值也只算占格件（喝掉的药水不是战力）。行按传说件数 → 装备
        价值降序，面板上一眼看出谁先成型。
        """
        rows = []
        for p in players:
            names = [it.get("name", "") for it in p.items if it.get("name")]
            perms = self.kb.permanent_items(names)
            legendary = sum(1 for n in perms
                            if self.kb.items.get(n, {}).get("gold", 0)
                            >= LEGENDARY_GOLD)
            gold = sum(self.kb.items.get(n, {}).get("gold", 0) for n in perms)
            rows.append({"name": p.champion or p.champion_en or "?",
                         "legendary": legendary, "gold": gold})
        rows.sort(key=lambda r: (r["legendary"], r["gold"]), reverse=True)
        return rows

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

    def log_click(self, record: dict):
        """点击链黑匣子（2026-09-29 台账复发案）：每次卡片点击/丢弃/reroll
        的完整终点记录（坐标/卡号/分支/结果/OCR原文/定格截图文件名），
        随复盘日志落盘——台账缺记录时与截图、诊断日志三方对账。

        环形上限 300 条防失控（正常一局 <10 条，异常刷屏时保尾部）；
        调用方分布在 MouseLedger/补扫回调线程，锁内追加。"""
        with self._lock:
            record = dict(record)
            record.setdefault("ts", time.time())
            self.clicks.append(record)
            if len(self.clicks) > 300:
                del self.clicks[:len(self.clicks) - 300]

    def record_augment(self, name: str, tier: str = "", card_idx=None,
                       round_id=None):
        """记账入口。card_idx：点击的卡片序号（0-2），供 overlay"✓已选"标记。
        round_id：点击时刻的检测器界面轮次（20261012 赏金猎人案）——成功
        记账即登记本轮已选：选卡完成游戏内界面立即关闭，同轮之后命中卡
        矩形的点击都是商店推荐面板/移动等游玩点击，但检测器消失迟滞
        （5~9s）+ 10s 证据窗内残留卡名仍有效，真机一局被幻影多记 2 条；
        app 按 round_recorded() 把同轮后续点击闸在台账外。

        同名防重复记账：拒绝时不动 hex_picked——被拒的点击不是一条新
        台账（同名已在档），把"✓已选"挪到新卡会让浮窗谎报已选而台账
        缺条（2026-09-29 核验实锤的错位）。模式是否允许同名强化跨轮
        重复选取无仓库数据可证（离线不可判），留 augment_duplicate_click
        轨迹事件供真机对局定性后再决定是否改按 (name, 轮次) 记账。

        升级型海克斯联动（升级联动案 20261013）：记账成功且为升级型
        （kb.augment_is_upgrade，事实层判定）→ ①军师推一条纯事实提示
        （不带出装结论，方案适配是模型的判断工作；先于刷新派发，
        _suggest_next_items 的 _set_coach_context 会把它喂给 System1）；
        ②事件式触发一次建议刷新——与死亡边沿同类派发（reason="event"，
        引擎侧不受 periodic 20s min_interval 守门），同一轮次
        （round_id）防重复触发，撤销后同轮重记不再刷（换轮照常）；
        出门装建议在屏时不覆盖（同 KIND_CTX 派发口径：opening 本就
        结合首海克斯生成）。刷新在锁外派发（同 _on_engine_result
        规矩：持锁中不调要拿锁的方法，AI 模式内部还要起线程）。
        """
        upgrade_refresh = False
        with self._lock:
            if not name:
                return False
            if any(a["name"] == name for a in self.augments):
                if card_idx is not None:
                    self._log("augment_duplicate_click",
                              f"卡{card_idx + 1} {name}（台账已有同名，"
                              "未重复记账，✓已选保持原卡）")
                return False
            if card_idx is not None:
                self.hex_picked = (card_idx, name)
            self.augments.append({"name": name, "tier": tier, "ts": time.time()})
            if round_id is not None:
                self.hex_recorded_rounds.add(round_id)
            self._log("augment_recorded", f"{tier} {name}")
            if self.first_augment is None:
                self.first_augment = name
            self._recompute_path()
            fired_by_opening = self._decide_opening_or_next()
            if self.kb.augment_is_upgrade(name):
                self._push_coach(f"upgrade:{name}",
                                 f"已选升级型海克斯 {name}：基础效果强化")
                if (round_id is None
                        or round_id not in self._upgrade_fired_rounds):
                    if round_id is not None:
                        self._upgrade_fired_rounds.add(round_id)
                    if self.opening_state == "offered":
                        if not self.my_items:
                            self._log("upgrade_refresh_skip",
                                      f"{name}（出门装建议在屏，不覆盖）")
                        else:
                            self.opening_state = "bypassed"
                            self._log("opening_bypassed",
                                      "已购装备，转入下一件模式")
                            upgrade_refresh = True
                    elif not fired_by_opening:
                        upgrade_refresh = True
            if upgrade_refresh:
                self._log("upgrade_refresh", name)
        if upgrade_refresh:
            self._suggest_next_items(reason="event")
        return True

    def round_recorded(self, round_id) -> bool:
        """本轮（检测器 round_id）是否已成功记账过海克斯。

        app 点击闸门的数据源（_on_click/_on_reroll）：True = 本轮界面已
        随选卡关闭，后续命中卡矩形/刷新按钮的点击按游玩点击丢弃。"""
        return round_id in self.hex_recorded_rounds

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

    def revoke_latest_augment(self):
        """撤销最近一条海克斯记账（app.py F9 热键入口）。

        末条读取与撤销同在锁内完成（RLock 可重入），防"读到的末条"
        与点击记账线程并发错位；只做规则级重算（路径/出门装回退），
        无重活，可在任意线程直接调用。返回被撤销的名字，台账为空
        或撤销失败返回 None。
        """
        with self._lock:
            if not self.augments:
                return None
            name = self.augments[-1]["name"]
            return name if self.revoke_augment(name) else None

    def set_hero_manual(self, query: str):
        """LCU 断连兜底：拼音/中文模糊匹配手动设英雄（main_window 输入框调用）"""
        matches, is_exact = self.kb.search_hero(query)
        if not matches:
            return None
        with self._lock:
            if self.my_hero != matches[0] and self.stage in ("game_start", "in_game"):
                # 局中换英雄视为极端兜底，重置对局数据
                augments = list(self.augments)
                rounds = set(self.hex_recorded_rounds)
                up_rounds = set(self._upgrade_fired_rounds)
                self.reset()
                self.augments = augments
                self.hex_recorded_rounds = rounds
                self._upgrade_fired_rounds = up_rounds
                self.stage = "in_game"
            elif self.stage == "idle":
                self.stage = "champ_select"
            self.my_hero = matches[0]
            self._log("hero_manual", matches[0])
        return matches[0]

    # ── 海克斯时刻（screen_detect 检测到三卡片 → 评级；记账入口 record_augment） ──

    def rank_augment_cards(self, card_names: list, desc_fetcher=None) -> list:
        """三张卡片基准评级（同步）+ AI 精修（异步经 _on_engine_result 覆盖）。

        desc_fetcher：卡面原文描述获取器（20261013 描述自举）——在 AI
        精修线程内调用（内部等待后台描述 OCR，自带超时放行），描述作为
        事实层进评级 prompt 并写回知识库；规则基准的上屏时序不受影响。

        AI 精修在途时 card_ranks_pending=True——UI 先显示卡名（"AI 评级中"），
        精修回调后放评级；未提交精修（规则模式/ctx 未就绪/预算耗尽）时
        False，基准评级即时上屏即最终展示。"""
        with self._lock:
            self._card_serial += 1
            serial = self._card_serial
            hero = self.my_hero or ""
            taken = [a["name"] for a in self.augments]
            items = list(self.my_items)
            threat = self.threat
        ranks, ai_pending = self.engine.rank_cards(hero, card_names, taken,
                                                   items, threat, serial,
                                                   desc_fetcher=desc_fetcher)
        with self._lock:
            if serial == self._card_serial:
                self.card_ranks = ranks
                self.card_ranks_pending = ai_pending
                # 能到这里的调用方（detector on_cards）已保证 ≥2 张有效库名
                self.hex_active = True
                self.hex_recognized = True
                self.hex_last = ""
        return ranks

    def clear_card_ranks(self):
        """海克斯界面消失后清除时刻评级（overlay 时刻块随之隐藏）"""
        with self._lock:
            self.card_ranks = []
            self.card_ranks_pending = False

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
                self.card_ranks_pending = False   # 新一轮：等待态随轮复位
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
                self.card_ranks_pending = False   # 旧轮等待态一并作废

    # ── 出门装两分支状态机（v4.1 §2：先海克斯后出门装） ────────────

    def _decide_opening_or_next(self) -> bool:
        """首个海克斯记账后：未购买任何装备 → 出门装三选（结合刚选海克斯）；
        已购买 → 不再推出门装，转下一件。

        返回本次是否已自行派发过一次 next_items 刷新（bypassed 分支）——
        调用方（record_augment 升级联动）据此避免同一记账事件双派发。"""
        if self.opening_state != "wait_first_augment" or not self.first_augment:
            return False
        if self.my_items:
            self.opening_state = "bypassed"
            self._log("opening_bypassed", "已购装备，直接进入下一件模式")
            self._suggest_next_items(reason="event")
            return True
        self.opening_state = "offered"
        serial = self._new_serial()
        threat = self.threat
        first = self.first_augment
        cands = self.engine.opening_candidates(first, threat, serial)
        note = f"首个海克斯【{first}】已联动"
        self._set_suggestion(Suggestion("opening", cands, note=note,
                                        serial=serial))
        self._log("opening_offered", first)
        return False

    def _suggest_next_items(self, reason: str = "event"):
        """出装主线序列（v4.2）：金币按序累计——够买第 1 件后剩余继续判第 2 件，
        兑现"钱够就连买两件"的场景；小件合成线有目标时置顶。

        reason: event（死亡/首海克斯后，不受 AI 间隔守门）| periodic（快照 diff）
        军师提示先行（v4.5 时序修正）：本帧攒钱/满装替换提示先推送、写
        上下文，再派发精修——后台线程组装 prompt 即读取军师模块态，顺序
        反了模型永远看不到它本该"修正"的那条。UI/日志推送行为不变，仅
        时序提前。规则底线取自引擎的规则实现（同一 delegate 同一组实参，
        与派发内部重算确定性一致）。
        """
        threat = self.threat or self.rules.threat_profile(self.enemy)
        serial = self._new_serial()
        # 规则底线预演：与引擎派发走同一规则实现，序列确定性一致；
        # pool 同源自引擎（AI 适配方案池，ctx 未就绪为 None 回 OP.GG）
        rule_engine = getattr(self.engine, "delegate", self.engine)
        seq = rule_engine.next_candidates(self.my_items, self._my_gold,
                                          self.current_path, threat, serial,
                                          reason=reason,
                                          augments=list(self.augments),
                                          pool=self.engine.pool_for(
                                              self.current_path))
        full_build = self._is_full_build()
        save_note = ""
        if full_build:
            if not (seq and isinstance(seq[0], SwapCandidate)):
                # 引擎底线非替换对（sell 池空等罕见情形）→ 直接规则版兜底
                seq = self.rules.replacement_candidates(
                    self.my_items, int(self._my_gold), self.current_path,
                    threat, augments=[a["name"] for a in self.augments],
                    hero=self.my_hero or "", game_map=self.game_map)
            if seq:
                # 净差补记 + 台账联动重排（与 _suggest_swap 同一套，先备好
                # 供军师文案取首对）；金币未知不按 0 计（同 _apply_afford 口径）
                for sw in seq:
                    sw.net_cost = (None if self._my_gold <= 0
                                   else sw.price - sw.sell_price
                                   - int(self._my_gold))
                seq = self.rules.swap_augment_rank(
                    seq, [a["name"] for a in self.augments])
                c0 = seq[0]
                if c0.net_cost is None:
                    aff_txt = ""     # 金币未知：不标注可买性
                else:
                    aff = ("现金足够" if c0.net_cost <= 0
                           else f"还差{c0.net_cost}金")
                    aff_txt = f"（{aff}）"
                # 满装替换提醒（v4.5 军师）：复用替换建议产出，key 按买件名
                # ——局势演化推出更优换装对时允许再提醒一次
                self._push_coach(f"fullswap:{c0.buy}",
                                 f"满六神装：可卖{c0.sell}换{c0.buy}{aff_txt}")
        else:
            target, note = self.rules.component_advice(
                self.my_items, self.current_path, game_map=self.game_map,
                prefer=[c.name for c in seq], hero=self.my_hero or "",
                threat=self.threat)
            synth_hint = ""
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
            elif note:
                # 防御合成让位核心（component_advice 返回 None+提示语，
                # 20260929 小火龙案）：不置顶覆盖主线，降级为附注行
                synth_hint = note
            # 攒钱决策附注（v4.5）：这波回城攒大件直购 vs 先买小件；
            # 同时入军师滚动列表（攒钱提醒），key 按目标件名去重防刷
            save_note = self.rules.saving_advice(self.my_items, self._my_gold,
                                                 seq, game_map=self.game_map)
            if synth_hint:
                save_note = (f"{synth_hint}；{save_note}" if save_note
                             else synth_hint)
            if save_note and seq:
                self._push_coach(f"save:{seq[0].name}", save_note)
        # 满六神装（v4.4）：格满后"下一件"买不进格，切到替换建议流；
        # 未满装行为完全不变（build_sequence 内按占格装备数分流）
        if full_build:
            if not seq:
                self._set_suggestion(Suggestion(
                    "swap_items", [], note="六神装成型，暂无更优替换（保持现装）",
                    gold=self._my_gold, serial=serial))
                self._log("swap_mode", "满装无替换候选")
            else:
                self._suggest_swap(seq, serial, reason)
        else:
            # 金币感知：序列定型后按顺序累计（先组装再算，避免插入项漏标）
            self._apply_afford(seq)
            self._set_suggestion(Suggestion("next_items", seq, note=save_note,
                                            gold=self._my_gold, serial=serial))
        # 精修派发必须在建议落位之后：AI 回包按 serial 认领"当前建议"——
        # 派发先于落位时，瞬时回包会写进即将被 _set_suggestion 整体替换的
        # 旧建议对象，AI 精修无声丢失（升级联动案 20261013 冒烟实测命中：
        # _suggest_next_items 经升级联动/ctx 刷新存在锁外调用路径，回包
        # 可在落位前插入）。军师上下文仍先于派发写入（后台线程组装
        # prompt 时读取）；内部重算的规则底线与上方预演同源同参。
        self._set_coach_context()
        if self.engine.mode != "rule":
            self.engine.next_candidates(self.my_items, self._my_gold,
                                        self.current_path, threat, serial,
                                        reason=reason,
                                        augments=list(self.augments))

    # ── 满六神装替换建议（v4.4） ─────────────────────────────────

    def _is_full_build(self) -> bool:
        """满六神装：6 占格且其中传说档非靴件 ≥5（is_full_build 单一口径，
        与 rules_engine.build_sequence 分流同尺；鞋/组件/消耗品占格不再
        单独构成满装——2026-09-28 真机 3 传说+2 组件+鞋误判案）。"""
        return is_full_build(self.kb, self.my_items)

    def _suggest_swap(self, swaps: list, serial: int, reason: str = "event"):
        """替换建议落位：补记金币净差 + 台账联动重排。

        引擎接口不透传 gold/augments（规则层 net_cost 先按价差计），
        这里补减身上金币；海克斯联动度补排（只定序不减员，AI 精修按
        买件名映射不受影响）。满装但身上还有小件时，合成不占格、比
        卖装更优先，作为 note 附在替换建议下。满装替换军师提醒改在
        _suggest_next_items 军师先行段推送（先于 prompt 组装，模型可见）。
        """
        for sw in swaps:
            # 金币未知（0）不按 0 计净差：置 None 不标注，与 _apply_afford
            # 口径一致，防 UI/军师报"还差X金"假信息
            sw.net_cost = (None if self._my_gold <= 0
                           else sw.price - sw.sell_price - int(self._my_gold))
        aug_names = [a["name"] for a in self.augments]
        swaps = self.rules.swap_augment_rank(swaps, aug_names)
        note = ""
        target, cnote = self.rules.component_advice(
            self.my_items, self.current_path, game_map=self.game_map,
            prefer=[c.buy for c in swaps], hero=self.my_hero or "",
            threat=self.threat)
        if target:
            note = f"先合成：{cnote}"
        self._set_suggestion(Suggestion("swap_items", swaps, note=note,
                                        gold=self._my_gold, serial=serial))
        self._log("swap_mode",
                  f"{len(swaps)} 对（{reason}）"
                  f"{'; '.join(f'卖{c.sell}换{c.buy}' for c in swaps[:3])}")

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
        ctx_refresh = False
        with self._lock:
            if kind == KIND_CTX:
                self._log("engine_ctx", str(payload.get("source")))
                # 适配方案就绪 → 刷新下一件建议，规则底线从 OP.GG 兜底切到
                # 方案池。仅下一件模式刷新：出门装态（opening 建议在展示，
                # 不消费方案池）与满装替换态（replacement_candidates 不吃
                # pool）刷新只会打乱流程/白烧预算。必须锁外执行——
                # _suggest_next_items 内部 _new_serial 要拿同一把锁
                # （threading.Lock 不可重入，锁内直调死锁）。
                if self.opening_state == "offered" and self.my_items:
                    # 出门装期间已购装：与 periodic 同款翻转
                    self.opening_state = "bypassed"
                    self._log("opening_bypassed", "已购装备，转入下一件模式")
                in_next_mode = ((self.first_augment or self.my_items)
                                and self.opening_state != "offered")
                ctx_refresh = bool(in_next_mode
                                   and not self._is_full_build())
            elif kind == KIND_RANKS:
                if serial == self._card_serial:
                    if payload:
                        self.card_ranks = payload
                        self._log("card_ranks_ai", str(len(payload)))
                    # None（精修失败/空结果信号）同样清等待态：规则基准
                    # 即为最终展示，"AI 评级中"不能永久滞留
                    self.card_ranks_pending = False
            elif serial == self._sug_serial and self.suggestion is not None:
                # opening / next_items：serial 匹配当前建议且类型一致才覆盖
                cur = self.suggestion
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
                        # System1 可选 coach 字段 → 覆盖最新一条军师文案
                        self._apply_ai_coach()
                        self._log("next_items_ai", f"{len(payload)} 项")
                elif kind == KIND_NEXT and cur.kind == "swap_items":
                    # 替换模式 AI 精修：payload 是 Candidate(买件名, AI理由)，
                    # 映射回当前替换对（按 buy 名，集合内），重排 + 换理由；
                    # 差价/净差等事实字段保持规则版（数据出事实，模型出判断）
                    if payload:
                        by_buy = {c.buy: c for c in cur.candidates
                                  if isinstance(c, SwapCandidate)}
                        merged, seen = [], set()
                        for pc in payload:
                            m = by_buy.get(pc.name)
                            if m is not None and m.buy not in seen:
                                if pc.reason:
                                    m.reason = pc.reason
                                merged.append(m)
                                seen.add(m.buy)
                        merged += [m for b, m in by_buy.items()
                                   if b not in seen]
                        if merged:
                            cur.candidates = merged[:3]
                            cur.source = "ai"
                            cur.ts = time.time()
                            # System1 可选 coach 字段 → 覆盖最新一条军师文案
                            self._apply_ai_coach()
                            self._log("swap_ai", f"{len(merged)} 对")
        if ctx_refresh:
            self._suggest_next_items(reason="event")

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
                    # 克制提示同步入军师滚动列表（军师格式，v4.5）
                    self._push_coach(f"ekey:{champ}:{item}",
                                     f"敌方{champ}出了{item}：{note}")
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
                    # 敌方威胁变化入军师（v4.5）
                    self._push_coach(f"kda:{p.champion}",
                                     f"敌方{p.champion}已起飞"
                                     f"（{p.kills}/{p.deaths}/{p.assists}），"
                                     "考虑针对性装备")
                    self._log("enemy_kda_alert", f"{p.champion} {p.kills}/{p.deaths}")
                    break

    # ── 军师提示（v4.5：滚动列表 + 去重节流 + System1 上下文桥接） ──

    def _ally_five(self) -> list:
        """己方五人名单。self.ally 来源不一（LCU teams 含我 / Live 回填不含
        我），统一合并去重，保证缺口统计按 5 人编制。"""
        names = [c for c in self.ally if c]
        if self.my_hero and self.my_hero not in names:
            names.append(self.my_hero)
        return names

    def _push_coach(self, key: str, text: str):
        """入列一条军师提示（带时间戳；key 去重 + 冷却节流；最多保留
        COACH_MAX_NOTES 条）。5s 轮询下同一提示只进一次列表；被挤出后
        60s 内不回灌（状态回摆不重复轰炸）。"""
        text = str(text).strip()
        if not text:
            return
        now = time.time()
        if any(c["key"] == key for c in self.coach_notes):
            return
        if now - self._coach_keys.get(key, 0.0) < COACH_REPEAT_COOLDOWN:
            return
        self._coach_keys[key] = now
        self.coach_notes.append({"ts": now, "key": key, "text": text})
        self.coach_notes = self.coach_notes[-COACH_MAX_NOTES:]
        self._log("coach", text)

    def _update_coach(self, snap):
        """每次快照重估三类状态型军师提示（事件型在触发器/建议流内推送）：
        ①己方阵容缺口（缺口标签变化 = 新 key）；②敌方缺口反向利用
        （敌方缺 AP → 你可偏护甲）；③顺逆风（数据不足不出声）。"""
        # ① 己方阵容缺口
        if self.gap and self.gap.gaps:
            tag = "、".join(self.gap.gaps)
            tail = ""
            if self.gap.need_tank and self.rules.tank_lean(
                    self.my_hero or "", self.gap):
                tail = "，你的坦装价值放大"
            self._push_coach(f"gap:{tag}", f"己方阵容{tag}{tail}")
        # ② 敌方阵容缺口反向利用：敌方无法伤 → 护甲更值；无物伤 → 魔抗更值
        egap = self.rules.ally_gap(self.enemy)
        if egap.need_ap:
            self._push_coach("egap:ap", "敌方缺法伤（全AD），护甲收益更高")
        if egap.need_ad:
            self._push_coach("egap:ad", "敌方缺物伤（全AP），魔抗收益更高")
        # ③ 顺逆风
        wind = self._wind_state(snap)
        if wind:
            self._push_coach(f"wind:{wind[0]}", wind[1])
        # ④ 死亡来源点名：同一英雄已杀你 ≥2 次 → 直接给抗性方向
        # （key 含次数，每多死一次升级提醒一次；60s 冷却防轰炸）
        top_kill = self.threat.top_killer() if self.threat else ()
        if top_kill:
            champ, n = top_kill
            side = (getattr(self.threat, "champ_damage", {}) or {}).get(champ, "")
            what = {"ap": "魔抗", "ad": "护甲"}.get(side, "针对抗性")
            self._push_coach(
                f"killer:{champ}:{n}",
                f"{champ}已{n}次击杀你，优先补{what}针对它")

    def _wind_state(self, snap):
        """顺逆风估算：playerlist 双方 KDA 动量差 + 装备成型度差。

        Live API 无 per-player 金币，经济差用 KDA 近似：动量 =
        kills*2 + assists − deaths*1.5（人均）；成型度 = 人均传说件数
        （≥LEGENDARY_GOLD 占格件）。合计击杀+死亡 < 6（开局噪音）不出声。
        返回 (状态, 文案) 或 None。
        """
        allies, foes = snap.allies(), snap.enemies()
        if not allies or not foes:
            return None
        if sum(p.kills + p.deaths for p in allies + foes) < 6:
            return None

        def momentum(players):
            return sum(p.kills * 2 + p.assists - p.deaths * 1.5
                       for p in players) / len(players)

        def build_avg(players):
            n = sum(1 for p in players for it in p.items
                    if self.kb.items.get(it.get("name", ""),
                                         {}).get("gold", 0) >= LEGENDARY_GOLD)
            return n / len(players)

        score = ((momentum(allies) - momentum(foes))
                 + WIND_ITEM_WEIGHT * (build_avg(allies) - build_avg(foes)))
        if score >= WIND_EDGE:
            return ("顺风", "顺风：经济领先，可提前补输出扩大优势")
        if score <= -WIND_EDGE:
            return ("逆风", "逆风优先保命装，放弃第四件输出")
        return ("均势", "均势：按主线补装，别单带过深")

    def _set_coach_context(self, clear: bool = False):
        """AI 模式：把军师提示写入 System1 模块上下文。

        engine 层接口（next_candidates）不透传军师数据（本期未改 engine），
        以 system1 模块态桥接：写入发生在精修派发前（锁内同步，军师提示
        已先行推送，见 _suggest_next_items），读取在后台线程 prompt 组装
        时。规则模式零开销跳过。
        AI 精修文案（src="ai"）不回注 prompt——否则模型把自己的旧话当
        规则层事实，形成自反馈回路。
        同时记录本次实际发给模型的最新一条提示 key（_coach_ctx_key）：
        AI 精修回填只定向覆盖这条，窗口期内新推的、模型没见过的提示不动。
        """
        if self.engine.mode == "rule":
            return
        notes = [] if clear else [c for c in self.coach_notes
                                  if c.get("src") != "ai"]
        self._coach_ctx_key = notes[-1]["key"] if notes else ""
        try:
            from hexa.ai import system1
            system1.set_coach_context([c["text"] for c in notes])
        except Exception as e:  # noqa: BLE001 —— 上下文缺失不阻断建议生成
            self._coach_ctx_key = ""   # 没发出去就不允许回填覆盖
            print(f"[WARN] 军师上下文写入失败: {e}")

    def _apply_ai_coach(self):
        """System1 可选输出字段 coach（≤30字）→ 定向覆盖发给模型的那条军师文案。

        规则版文案独立完整，AI 版只是判断层面的精修；读取 system1 模块态
        （suggest_next/suggest_swap 解析时写入），消费后清空防陈值复用。
        打 src="ai" 标记：UI 照常展示，但 _set_coach_context 不再回注 prompt。
        只覆盖 _set_coach_context 记录的、确实发给模型的最新一条（按 key
        定位）——上下文写入与 AI 结果返回之间新推的提示模型没见过，不动
        它们（原"覆盖最新一条"会在 60s 冷却内错杀正确文案）；那条已被
        挤出或本次没发过提示时，不覆盖任何现有提示。
        """
        if self.engine.mode == "rule":
            return
        try:
            from hexa.ai import system1
            text = system1.last_coach()
            system1.clear_coach()
        except Exception:
            return
        if not text:
            return
        key = self._coach_ctx_key
        self._coach_ctx_key = ""
        for i, c in enumerate(self.coach_notes):
            if c["key"] == key:
                entry = dict(c)
                entry["text"] = text
                entry["src"] = "ai"
                self.coach_notes[i] = entry
                self._log("coach_ai", text)
                return
        if not self.coach_notes:
            # 上下文阶段就没有规则提示可发：AI 文案自行入列展示（原行为保留）
            self.coach_notes.append({"ts": time.time(), "key": "ai",
                                     "src": "ai", "text": text})
            self.coach_notes = self.coach_notes[-COACH_MAX_NOTES:]
            self._log("coach_ai", text)
            return
        # 发给模型的那条已被挤出/本次没发过：不覆盖任何现有提示（宁缺毋滥）
        self._log("coach_ai_skip", text)

    # ── current_path 重估（层 0 确定性） ──────────────────────────

    def _recompute_path(self):
        aug_names = [a["name"] for a in self.augments]
        # hero/gap：缺前排且我的英雄可扛时 tank 路线加分（v4.5）
        new_path = self.rules.infer_current_path(
            self.my_items, aug_names, hero=self.my_hero or "", gap=self.gap)
        if new_path and new_path != self.current_path:
            self.current_path = new_path
            self._log("current_path", new_path)

    # ── 复盘日志（P3：局终落盘，供赛后回顾） ──────────────────────

    def _write_review_log(self, final=True):
        """复盘落盘。final=True 局终写（置幂等位，on_end_of_game /
        flush_pending_review 调）；final=False 为对局期草稿（不置位，
        局终仍会覆盖写终态），文件名首写即缓存——草稿与终态覆盖同一
        份文件，不因 hero 迟到等产生第二份。"""
        try:
            out_dir = BASE_DIR / "log" / "games"
            out_dir.mkdir(parents=True, exist_ok=True)
            if self._review_path is None:
                ts = time.strftime("%Y%m%d_%H%M%S",
                                   time.localtime(self._game_t0))
                safe_hero = "".join(
                    ch for ch in (self.my_hero or "unknown")
                    if ch.isalnum() or "\u4e00" <= ch <= "\u9fff") or "unknown"
                self._review_path = f"{ts}_{safe_hero}.json"
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
                # 点击链黑匣子（台账排障对账：与 log/captures/ 截图、
                # [LEDGER] 诊断日志三方互证）
                "clicks": [dict(c, ts=round(c["ts"], 3))
                           for c in self.clicks[-200:]],
                # 鼠标双通道统计窗历史（20261002 寒冰案：局内 LL 钩子/轮询
                # 各收到多少按下沿、有无拦截告警——台账空的局级铁证）
                "mouse_stats": list(self.mouse_stats_fn())
                               if self.mouse_stats_fn else [],
            }
            path = out_dir / self._review_path
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            if final:
                self._review_flushed = True   # 写成功才置位；异常保持 False 可重试
            print(f"[SESSION] 复盘{'日志' if final else '草稿'}已写入 "
                  f"{self._review_path}")
        except Exception as e:
            print(f"[WARN] 复盘日志写入失败: {e}")

    def _maybe_flush_review_draft(self):
        """复盘草稿周期落盘（进程死亡护身符）。

        复盘数据此前全程在内存、局终才落盘——App 进程被强杀/终端关闭/
        关机时，"下一局开局"与"退出 finally"两条兜底都来不及执行，整局
        随进程蒸发（20260929 人马案：日女局 21:07 正常落盘后同进程再打
        人马局，进程无声消失，复盘零记录且无 tee 无 WER 无从查证）。
        改为对局期每 DRAFT_INTERVAL 秒覆盖写同一份草稿，局终 final 写
        覆盖为终态——进程死亡最多丢一个周期。零内容局不产文件（与
        flush_pending_review 同判据）；hero 未定不写（文件名会被钉死为
        unknown）。仅在快照路径调用，锁内直写（RLock 同线程可重入，
        与 on_end_of_game 持锁写同规矩）。
        """
        now = time.time()
        if now - self._draft_ts < self.DRAFT_INTERVAL:
            return
        has_unverified = any(e[1] == "augment_unverified"
                             for e in self.timeline)
        if not (self.augments or self.history or has_unverified
                or self.clicks):
            return
        if not self.my_hero:
            return
        self._draft_ts = now
        self._write_review_log(final=False)

    def report_data_stall(self):
        """对局数据流停摆判定（PhaseMachine 每轮询问；终局监听兜底）。

        终局落盘此前完全押注"phase 查询成功"一条链——InProgress→IDLE
        转换只在 get_gameflow_phase 正常返回时判定（20260929 人马案：
        局终退回桌面后本应 2s 内触发落盘，却因客户端局后离场/重启换口/
        接口持续失败，prev 永远卡 InProgress，on_end_of_game 永不触发，
        直到关机数据随进程蒸发）。本兜底不依赖 LCU 可用性：in_game 且
        快照断流超 STALL_TIMEOUT（游戏进程退了/接口死了/客户端不在，
        全部表现为 on_snapshot 不再来）即判定局已结束，返回 True 让
        PhaseMachine fire on_end_of_game("data_stall")。正常对局快照
        1-2s 一帧、死亡重生最长 78s，远小于阈值，不会误判；加载期
        （game_start）不判定。幂等由 on_end_of_game 的 stage 门保证。
        """
        with self._lock:
            if self.stage != "in_game":
                return False
            return time.time() - self._last_data_ts > self.STALL_TIMEOUT

    def on_phase(self, phase):
        """原始 phase 变化留痕（复盘 timeline 可见 phase 轨迹，排障用）"""
        with self._lock:
            self._log("phase", phase)

    def flush_pending_review(self):
        """复盘日志兜底②（P1）：终局事件丢失时补写上一局的复盘。

        触发点：下一局 on_champ_select / on_game_start 的 reset 前、
        App 退出 finally（app.py ledger.stop 后）。条件：本局尚未落盘、
        仍处于对局阶段（idle 下 reset 已清场，无可写内容）、且台账或
        建议历史非空，或 timeline 有库外名点击留痕（augment_unverified
        ——纯留痕局也要落盘，否则知识库落后案的线索随退出丢失）；
        零内容的空转局不产文件。_write_review_log 幂等——同一局重复
        调用只在首次成功时产出文件（后写会覆盖同名文件，置位后不再
        重写）。
        """
        with self._lock:
            if self._review_flushed:
                return
            if self.stage not in ("game_start", "in_game"):
                return
            has_unverified = any(e[1] == "augment_unverified"
                                 for e in self.timeline)
            # 纯点击留痕局（点击全被丢/全 unverified，台账建议双空）也要
            # 落盘——clicks 黑匣子正是这类局的唯一现场（2026-09-29 台账案）
            if not (self.augments or self.history or has_unverified
                    or self.clicks):
                return
            self._log("review_flush", "终局事件丢失，兜底补写复盘日志")
            self._write_review_log(final=True)

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
                # 我的等级（快照直出；0=无数据，UI 英雄行省略 Lv.）
                "my_level": self.my_level,
                "ally": list(self.ally),
                "enemy": list(self.enemy),
                # 台账带品质（tier 快照缺省为空串，UI 省略括号）
                "augments": [{"name": a["name"], "tier": a.get("tier") or ""}
                             for a in self.augments],
                "my_items": list(self.my_items),
                "current_path": self.current_path,
                "path_drift": drift,
                "threat": threat,
                "suggestion": self.suggestion.render() if self.suggestion else "",
                "suggestion_kind": self.suggestion.kind if self.suggestion else "",
                "suggestion_source": self.suggestion.source if self.suggestion else "",
                "swap_forecast": self.swap_forecast,
                # 成型度对比（v4.6）：敌我两组六神装进度（锁内深拷贝；
                # None=快照未到/装备数据缺失，UI 按占位渲染不报错）
                "build_progress": (
                    {"ally": [dict(r) for r in self.build_progress["ally"]],
                     "enemy": [dict(r) for r in self.build_progress["enemy"]]}
                    if self.build_progress else None),
                # 军师提示（v4.5）：滚动列表（≤3 条）+ 最新一条（overlay 顶部）
                "coach": [{"ts": c["ts"], "text": c["text"]}
                          for c in self.coach_notes],
                "coach_latest": (self.coach_notes[-1]["text"]
                                 if self.coach_notes else ""),
                "card_ranks": [dict(r) for r in self.card_ranks],
                "card_ranks_pending": self.card_ranks_pending,
                "hex_active": self.hex_active,
                "hex_recognized": self.hex_recognized,
                "hex_picked": (list(self.hex_picked)
                               if self.hex_picked else None),
                "hex_last": self.hex_last,
                "skill_order": skill,
                "dd_version": self.kb.dd_version,
                "engine": engine_stats,
            }
