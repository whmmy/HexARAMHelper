"""HexARAMHelper 入口：主窗口 + overlay + 服务编排

生命周期全自动（LCU 阶段状态机驱动），无操作热键；
仅保留两个可选键：F10 浮窗显隐（全局）、F9 撤销最近一条记账
（带前台窗口校验——仅本工具窗口在前台时生效，对局中请用主窗口「撤销」）。

P1 起：画面检测线程（标题条 OCR + 229 库匹配，迟滞）+ 只读鼠标记账
（海克斯界面活跃窗口内点击卡片 → 台账；点 reroll → 主动重扫）。
P2 起：AI 双槽位引擎（settings 开关；无 Key 自动纯规则）。
"""
import ctypes
import ctypes.wintypes  # Win32 前台校验用（本项目 Windows 专属，同 screen_detect）
import os
import queue
import sys
import threading
import time

import tkinter as tk


# ── 启动延迟自动数据刷新（P3 杂项①） ─────────────────────────────
AUTO_REFRESH_DELAY_S = 60.0   # 启动后延迟抓取 OP.GG 的秒数（避开启动瞬间的网络/CPU 抢占）


class DelayedAutoRefresh:
    """延迟触发、可取消的一次性任务（启动 60s 后自动抓 OP.GG 用）。

    用 threading.Timer 而非 root.after：不依赖 Tk 对象——应用退出路径
    （mainloop 退出后的 finally / 托盘退出）与手动按钮入口都能直接
    cancel，且离线冒烟不开窗即可断言"挂起→取消→不执行 / 到点→仅执行
    一次"（20261013 P3 杂项案）。
    """

    def __init__(self, delay_s: float = AUTO_REFRESH_DELAY_S):
        self._delay_s = delay_s
        self._lock = threading.Lock()
        self._timer = None

    @property
    def pending(self) -> bool:
        with self._lock:
            return self._timer is not None

    def schedule(self, fn) -> bool:
        """挂起延迟触发；已有挂起时不重复排（返回 False，防到点抓两遍）。"""
        with self._lock:
            if self._timer is not None:
                return False
            timer = threading.Timer(self._delay_s, self._fire, args=(fn,))
            timer.daemon = True
            timer.name = "AutoDataRefresh"
            self._timer = timer
        timer.start()
        return True

    def _fire(self, fn):
        # cancel 与到点并发的窄窗：锁内谁先拿走 _timer 谁赢——cancel 先
        # 清走则 fn 不执行（这正是"取消挂起的刷新"的语义）
        with self._lock:
            if self._timer is None:
                return
            self._timer = None
        fn()

    def cancel(self):
        """取消挂起的触发（幂等；未挂起/已到点后再调是无害空转）。"""
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()


def main():
    # pythonw 下 stdout/stderr 为 None——任何 print 都 AttributeError
    # （20261004 提权重启静默死亡案：UAC 点"是"后无窗即此故，知识库
    # 加载 [OK] 行在 tee 之前打印直接炸进程），先垫安全流
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    # 高 DPI（fork 参考项目 gui_launcher）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    from hexa.config import BASE_DIR, Settings
    os.chdir(BASE_DIR)

    from hexa.ai import engine as engine_mod
    from hexa.core.fixtures import FixtureDumper
    from hexa.core.game_state import GameSession
    from hexa.core.live_api import LivePoller
    from hexa.core.lcu import LCUConnector, PhaseMachine
    from hexa.core.mouse_ledger import CardLedger, MouseLedger
    from hexa.core.ocr_engine import OcrEngine
    from hexa.core.rules_engine import RulesEngine
    from hexa.core.runes_probe import RunesProbe
    from hexa.core.screen_detect import ScreenDetector, load_layout, point_in_rect
    from hexa.knowledge.kb import KnowledgeBase
    from hexa.ui.main_window import MainWindow
    from hexa.ui.overlay import Overlay
    from hexa.ui.settings_window import SettingsWindow
    from hexa.ui.theme import LogRedirector

    # UI 队列（后台线程 → UI 线程；可承载事件串与可调用对象）
    ui_queue = queue.Queue()
    log_queue = queue.Queue()

    # stdout/stderr → 日志面板 + 文件留档——提前到一切打印之前：
    # 知识库加载等早期输出也要留档，pythonw 垫流之后的早期 print 才有
    # 归处（控制台窗口一关输出全丢：2026-09-29 台账复发案起 tee 落盘）
    _tee_path = BASE_DIR / "log" / f"app_{time.strftime('%Y%m%d_%H%M%S')}.log"
    _tee_path.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = LogRedirector(log_queue, sys.stdout, tee_path=_tee_path)
    sys.stderr = LogRedirector(log_queue, sys.stderr, tee_path=_tee_path)

    settings = Settings()
    kb = KnowledgeBase()
    rules = RulesEngine(kb)

    # ── 自动提权（20261003/04 皮城女警案定案） ────────────────────
    # 游戏窗口提权(High)运行，本工具 Medium 时游戏画面焦点期的输入对
    # 全部观察通道（LL 钩子/捕获子进程/GetAsyncKeyState）完全不可见
    # （事件计数归零、连鼠标移动都没有）——选卡点击永远进不了台账。
    # 提权到与游戏同级后观察恢复。启动时检测：未提权 → ShellExecute
    # runas 重启自身（UAC 弹窗一次），拒绝则带降级 WARN 继续跑。
    from hexa.core.mouse_ledger import is_elevated
    _relaunch_mark = "--elevated-relaunch" in sys.argv
    if _relaunch_mark and is_elevated():
        print("[OK] 管理员权限生效（IL=High）——游戏画面焦点期的输入观察"
              "就绪")
    if _relaunch_mark and not is_elevated():
        # 递归保险（20261004 提权递归案）：重启标记在身仍非提权 → 不再
        # 二次请求（否则无限递归，8 分钟 1500 个实例），带降级 WARN 运行
        print("[WARN] 提权重启未生效（仍为普通权限）——游戏内选卡点击"
              "无法记账；请右键本程序选择\"以管理员身份运行\"后重试")
    if (not is_elevated() and not _relaunch_mark
            and bool(settings.get("diagnostics", "elevate_auto",
                                  default=True))):
        # 重启目标必须绕开 uv 的 pythonw trampoline（.venv\Scripts 下的
        # 是跳板，提权令牌在它 spawn 真解释器时丢失 → 子进程仍 Medium →
        # 再次请求 → 无限递归）：从 pyvenv.cfg 解析基础解释器直启，
        # PYTHONPATH 指向 venv site-packages 保住虚拟环境依赖
        target = None
        try:
            for line in (BASE_DIR / ".venv" / "pyvenv.cfg").read_text(
                    encoding="utf-8").splitlines():
                if line.lower().startswith("home"):
                    base = os.path.dirname(line.split("=", 1)[1].strip())
                    cand = os.path.join(base, "pythonw.exe")
                    if os.path.exists(cand):
                        target = cand
                    break
        except Exception:  # noqa: BLE001 —— 解析失败走跳板兜底
            pass
        if target is None:
            target = str(BASE_DIR / ".venv" / "Scripts" / "pythonw.exe")
        site = str(BASE_DIR / ".venv" / "Lib" / "site-packages")
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [p for p in (site, os.environ.get("PYTHONPATH", "")) if p])
        script = os.path.abspath(sys.argv[0])
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", target, f'"{script}" --elevated-relaunch',
            str(BASE_DIR), 1)
        if ret > 32:
            print("[OK] 已请求管理员权限重启（UAC 确认后生效）——游戏画面"
                  "焦点期的输入观察需要与游戏同级权限")
            return
        print("[WARN] 提权未确认（UAC 拒绝或失败）——以普通权限运行："
              "游戏内的选卡点击将无法记账（输入被提权游戏窗口屏蔽）")

    # 降进程优先级：本工具任何突发计算（OCR/截图）都不与游戏抢核
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass

    dumper = FixtureDumper(enabled=bool(settings.get("fixtures", "dump", default=False)))

    root = tk.Tk()
    root.withdraw()  # 主窗口在初始化完成后再显示

    # UI 线程调度 helper（任意线程 → mainloop）
    def ui(fn):
        ui_queue.put(fn)

    # ── 决策引擎（规则 / AI 双槽位） ──────────────────────────────
    engine = engine_mod.build_engine(kb, rules, settings)
    lcu = LCUConnector(kb)
    # ── 海克斯落库探针（20261013，诊断专用）────────────────────────
    # 竞品实证 allgamedata.allPlayers[].runes.generalRunes[] 中 id>100000
    # 疑似已选海克斯，逐局落盘 log/runes_probe/runes_probe.jsonl 供
    # experiments/summarize_runes_probe.py 复核 §6 旧结论；探针炸不进主链
    # （live_api/game_state 侧 try/except 隔离），settings
    # fixtures.runes_probe=false 可关。
    runes_probe = (RunesProbe()
                   if bool(settings.get("fixtures", "runes_probe",
                                        default=True)) else None)
    # 国服 activeplayer 缺英雄名/装备：注入召唤师名给 LivePoller 做玩家锚定
    live = LivePoller(kb, fixture_dumper=dumper,
                      summoner_name_fn=lcu.summoner_name,
                      runes_probe=runes_probe)
    session = GameSession(kb, rules, settings=settings,
                          fixture_dumper=dumper, live_poller=live,
                          engine=engine,
                          # 鼠标双通道统计窗随复盘 JSON 落盘（台账排障局级
                          # 证据）。ledger 在下方才构造——复盘写盘时刻必已
                          # 存在，此处晚绑定即可
                          mouse_stats_fn=lambda: ledger.stats_history())
    live.on_snapshot = session.on_snapshot

    phase = PhaseMachine(lcu, callbacks={
        "on_champ_select": session.on_champ_select,
        "on_game_start": session.on_game_start,
        # 20261002 寒冰案：局内相位切换 MouseLedger 轮询通道开关——LL 钩子
        # 在游戏窗口焦点期收不到事件（客户端窗口期正常）。回调在相位机
        # 首次触发时才解引用，ledger 必已构造（phase.start() 在其后）。
        "on_in_progress": lambda *a: (ledger.set_game_mode(True),
                                      session.on_in_progress()),
        "on_end_of_game": lambda *a: (ledger.set_game_mode(False),
                                      session.on_end_of_game(*a)),
        "on_phase": session.on_phase,          # phase 轨迹进复盘 timeline
        "check_stall": session.report_data_stall,   # 数据流停摆→终局兜底
    })

    # ── P1 交互闭环：画面检测 + 鼠标记账 ──────────────────────────
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    ocr = OcrEngine(kb)
    detector = ScreenDetector(kb, ocr, layout=load_layout(screen_w, screen_h))
    detector.last_cards = None          # 最近一轮稳定卡片名（记账映射用）

    # ── 点击证据落盘（台账排障黑匣子的图像侧） ────────────────────
    # 每次卡片点击的定格帧写 log/captures/ PNG，文件名进 clicks 记录
    # （复盘日志）——OCR 读错/矩形错位/界面异常，事后看图即知。
    # 排障默认开；定位后 settings [diagnostics] click_capture = false 关。
    _capture_on = bool(settings.get("diagnostics", "click_capture",
                                    default=True))
    _cap_dir = BASE_DIR / "log" / "captures"

    def _capture_sink(idx, crop, tag):
        """capture_sink 钩子：crop → PNG 文件名（失败返回 None 不抛）。"""
        if not _capture_on:
            return None
        import cv2
        _cap_dir.mkdir(parents=True, exist_ok=True)
        ts = (time.strftime("%Y%m%d_%H%M%S")
              + f"_{int(time.time() * 1000) % 1000:03d}")
        path = _cap_dir / f"{tag}_{ts}_card{idx + 1}.png"
        ok, buf = cv2.imencode(".png", crop)   # imencode 走内存，绕开
        if not ok:                             # imwrite 的 ANSI 路径限制
            return None
        path.write_bytes(buf.tobytes())
        return path.name

    detector.capture_sink = _capture_sink

    def _frame_sink(frame):
        """每轮海克斯界面激活帧存档（20261003 皮城女警案：用户点击落点
        与标定矩形错位嫌疑——留档看卡片真实位置，坐标争议一锤定音）。"""
        if not _capture_on:
            return
        import cv2
        _cap_dir.mkdir(parents=True, exist_ok=True)
        path = _cap_dir / (f"round_{time.strftime('%Y%m%d_%H%M%S')}.png")
        ok, buf = cv2.imencode(".png", frame)
        if ok:
            path.write_bytes(buf.tobytes())

    detector.frame_sink = _frame_sink

    def _capture_full_drop(x, y):
        """丢弃点击的全屏定格（5s 限频）：rect_miss/no_hex_evidence 的
        点击没有卡面可截，但"用户点的那一刻屏幕上是什么"是排障铁证。"""
        if not _capture_on:
            return None
        now = time.monotonic()
        if now - _click_diag_ts.get("_full_drop", 0.0) < 5.0:
            return None
        _click_diag_ts["_full_drop"] = now
        try:
            frame = detector._grab()
            import cv2
            _cap_dir.mkdir(parents=True, exist_ok=True)
            path = _cap_dir / (f"drop_{time.strftime('%Y%m%d_%H%M%S')}"
                               f"_{int(x)}_{int(y)}.png")
            ok, buf = cv2.imencode(".png", frame)
            if ok:
                path.write_bytes(buf.tobytes())
                return path.name
        except Exception as e:  # noqa: BLE001 —— 排障辅助不拖垮主链路
            print(f"[WARN] 丢弃点击截屏异常: {e}")
        return None

    def _on_state(active, round_id):
        if active:
            session.on_hex_ui(True)
            ui(lambda: print(f"[DETECT] 海克斯界面活跃（第 {round_id} 轮）"))
        else:
            ui(lambda: print("[DETECT] 海克斯界面结束"))
            detector.last_cards = None
            detector.last_cards_raw = None
            session.clear_card_ranks()   # 时刻块随界面消失（台账仍在）
            session.on_hex_ui(False)     # 识别成败摘要留在浮窗

    def _on_cards(stable_cards, round_id):
        detector.last_cards = list(stable_cards)  # 含 None 占位（保证卡片序对齐）
        names = [n for n in stable_cards if n]
        print(f"[DETECT] 本轮卡片: {names}")
        # 描述自举（20261013）：卡面原文是海克斯能力的事实源——标签库是
        # 离线从名字猜的压缩结论（真机实锤 大法师 被标 ap/法穿/爆发，原文
        # 是技能花费返还）。现在抓帧定格（界面在屏），OCR 在后台线程跑；
        # 评级 AI 精修线程经 fetcher 等待结果进 prompt 并写回知识库——
        # 调用频次不增，规则基准照旧即时上屏。
        _desc_box = {}
        _desc_done = threading.Event()

        def _on_desc(descs):
            _desc_box["descs"] = descs
            _desc_done.set()

        detector.async_ocr_descriptions(_on_desc, round_id=round_id)

        def _desc_fetcher():
            if not _desc_done.wait(10.0):
                print("[WARN] 卡面描述 OCR 超时（10s），本轮评级不带描述")
                return None
            descs = _desc_box.get("descs")
            if not descs:
                return None
            out = {}
            for i, n in enumerate(stable_cards):
                if not n:
                    continue
                text = (descs.get(i) or "").strip()
                if len(text) >= 8:
                    out[n] = text[:120]
                    kb.record_augment_desc(n, out[n])   # 自增长资产：随对局自更新
                elif kb.augment_desc(n):
                    out[n] = kb.augment_desc(n)   # 本轮该卡没读到 → 库缓存兜底
            return out or None

        ranks = session.rank_augment_cards(stable_cards,
                                           desc_fetcher=_desc_fetcher)
        # 打印标注 ctx 来源：ctx 未就绪时这行只是规则基准，此前顶着 [AI]
        # 前缀被当成 AI 返回，对账时无从分辨（20260929 曙光女神案）
        try:
            ctx_tag = session.engine.stats().get("ctx_source") or "-"
        except Exception:
            ctx_tag = "?"
        ui(lambda: print(f"[AI] 海克斯评级[ctx:{ctx_tag}]: " +
                         ", ".join(f"{r['name']}[{r['grade']}]" for r in ranks)))

    def _on_calibrate_hint():
        ui(lambda: print("[WARN] 海克斯卡片识别连续失配——下次海克斯界面出现时"
                         "保持不动 5 秒会自动完成标定；或趁界面在屏时到主窗口"
                         "点\"开始标定\"（有 10 秒倒计时切窗口）"))

    detector.on_state = _on_state
    detector.on_cards = _on_cards
    detector.on_calibrate_hint = _on_calibrate_hint

    card_ledger = CardLedger(rects_provider=lambda: (
        detector.layout.to_pixels(screen_w, screen_h)["cards"]
        if detector.layout else None))

    def _reroll_rects():
        px = detector.layout.to_pixels(screen_w, screen_h) if detector.layout \
            else None
        return px["rerolls"] if px else None

    _click_diag_ts = {}   # 点击链诊断限频（key → 上次打印时刻）
    _silent_clicks = {"n": 0, "ts": time.monotonic()}
    # 常规点击未路由计数（idx=None+非活跃，5min 一条汇总）：2026-09-30
    # 牛头案——两局选卡点击零记账且 py-spy 实证线程存活，本计数与
    # MouseLedger [MOUSE] 按下沿汇总对账可定案"点击有没有进 _on_click"

    def _click_diag(key, detail, ev=None):
        """点击链丢弃诊断：3s 限频 print（落日志面板+tee 文件），并写
        session timeline 供复盘日志事后追溯（stdout 无留档时唯一现场）。

        2026-09-29 复发案：海克斯识别成功但台账空——旧版 _on_click 三个
        静默 return（reroll 防御/矩形未命中/无海克斯证据）无线索可查，
        本轮全部上诊断。只诊断异常丢弃：正常"点卡片矩形外"的移动/攻击
        点击（无海克斯证据时不进到这里）不刷屏。
        ev：点击上下文（x/y/card），进 clicks 黑匣子（打印限频不限记录）。"""
        now = time.monotonic()
        first = now - _click_diag_ts.get(key, 0.0) >= 3.0
        if first:
            _click_diag_ts[key] = now
            print(f"[LEDGER] 点击未记账({key}): {detail}")
        try:
            session._log("click_dropped", f"{key} {detail}")
        except Exception:
            pass
        if ev:
            session.log_click({**ev, "branch": "drop",
                               "result": f"dropped_{key}", "name": None,
                               "raw": None, "image": None})

    def _on_click(x, y, ts=None):
        # 双保险：MouseLedger 已把刷新按钮命中独占路由给 on_reroll，正常
        # 走不到这里；留着防御路由/矩形来源将来变更时误把刷新记账成选卡。
        # ts=点击时刻（v4 透传）：门控/记账一律按点击时刻判定，处理线程
        # 被拖 late 时不再按处理时刻误丢（20261002 虚空之女案）
        t_click = ts if ts is not None else time.time()
        for rr in _reroll_rects() or []:
            if point_in_rect(x, y, rr):
                _click_diag("reroll_overlap",
                            f"({x},{y}) 命中刷新按钮矩形（路由异常?）",
                            ev={"x": x, "y": y, "card": None, "ts": t_click})
                return
        idx = card_ledger.card_at(x, y)
        active = detector.recent_hex_activity(ts=t_click)
        if idx is None:
            if detector.core.state == "active":
                rects = card_ledger.rects_provider()
                shot = _capture_full_drop(x, y)
                _click_diag("rect_miss",
                            f"({x},{y}) 海克斯界面在屏但未命中任何卡片矩形 "
                            f"{rects}（标定错位嫌疑）",
                            ev={"x": x, "y": y, "card": None, "ts": t_click,
                                "image": shot})
            else:
                st = _silent_clicks
                st["n"] += 1
                now = time.monotonic()
                if now - st["ts"] >= 300.0:
                    print(f"[MOUSE] 近5min 常规点击未路由（卡矩形外+非活跃）"
                          f" {st['n']} 次")
                    st["n"] = 0
                    st["ts"] = now
            return   # 常规对局点击（移动/攻击）占绝大多数：静默防刷屏
        if not active:
            shot = _capture_full_drop(x, y)
            _click_diag("no_hex_evidence",
                        f"({x},{y}) 卡{idx + 1} 矩形命中但无海克斯证据"
                        "（界面刚关 >10s？）",
                        ev={"x": x, "y": y, "card": idx + 1, "ts": t_click,
                            "image": shot})
            return   # 卡片矩形命中但无海克斯证据 = 误入
        # 本轮已记账 → 界面早已随选卡关闭，这是选完后的游玩点击（商店推荐
        # 面板/移动落进旧卡矩形）——20261012 赏金猎人案幻影记账的闸门：
        # 检测器消失迟滞 5~9s + 证据窗 10s 内残留卡名仍有效，真机一局被
        # 多记 2 条。放在抓帧之前：游玩点击不再产生定格截图。
        rid = detector.core.round_id
        if session.round_recorded(rid):
            _click_diag("round_recorded",
                        f"({x},{y}) 卡{idx + 1} 第{rid}轮已记账，后续点击"
                        "按游玩点击丢弃",
                        ev={"x": x, "y": y, "card": idx + 1, "ts": t_click})
            return
        # 点击时刻抓帧定格+落盘（毫秒级）；直记/补扫消费同一张 crop，
        # 保证截图与识别看到的是同一像素（台账对账三方：clicks/截图/日志）
        crop, img = detector.capture_click_evidence(idx)
        # 稳定结果优先；识别尚未稳定时用最近一帧读数兜底（历史缺陷：
        # OCR 逐帧抖动导致永不稳定 → last_cards 空 → 点击被静默丢弃）
        cards = detector.last_cards or detector.last_cards_raw
        name = cards[idx] if cards and idx < len(cards) else None
        if name and kb.valid_augment(name):
            _record_click(idx, name, None,
                          ev=(t_click, x, y, idx, "direct", img), rid=rid,
                          crop=crop)
            return
        # 识别挪后台线程（迟滞窗口/OCR 迟滞期的点击也要能记账；历史缺陷：
        # 这里的点击被静默丢弃 → 台账缺记录；2026-09-29 起不再同步阻塞
        # 记账线程 ~2s，连点两张卡不漏）
        detector.async_ocr_card_raw(
            idx, lambda n, r, i=idx, rid=rid, c=crop: _record_click(
                i, n, r, ev=(t_click, x, y, i, "rescan", img), rid=rid,
                crop=c),
            crop=crop)

    def _record_click(idx, name, raw, ev=None, rid=None, crop=None):
        """点击记账落位（稳定名直记 / 后台补扫回调共用）。

        库外名留痕：timeline 记下 OCR 原文随复盘日志落盘（知识库
        落后于游戏版本时才有线索可查）；不进台账、不参与联动推装。
        ev=(点击时刻, x, y, 卡号, 分支, 截图文件名)：终点组装 clicks
        黑匣子记录——direct=稳定名直记，rescan=后台补扫，
        callback=无上下文兜底（不该出现，出现即上下文丢失线索）。
        rid：点击时刻的检测器轮次——成功记账即登记本轮（session 侧
        闸门数据源）并立即作废残留卡名（同 reroll 分支）：20261012
        赏金猎人案，选卡后窗口期内商店/地面的 OCR 读数不得再入台账。
        crop：点击定格帧的单卡裁剪——记账成功后后台 OCR 描述带入
        描述自举库（升级：无尽之刃案 20261014：快选时 on_cards 稳定
        门未及触发，该轮描述 OCR 从未跑过，已选海克斯从此只有名字
        没有效果进 prompt）。"""
        if ev is None:
            ev = (time.time(), None, None, idx, "callback", None)
        t0, x, y, card, branch, img = ev

        def _blackbox(result, n=None):
            session.log_click({"ts": t0, "x": x, "y": y,
                               "card": card + 1 if card is not None else None,
                               "branch": branch, "result": result,
                               "name": n, "raw": raw, "image": img})

        if not name or not kb.valid_augment(name):
            session._log("augment_unverified", f"卡{idx + 1} raw={raw}")
            _blackbox("unverified")
            now = time.time()
            if now - _click_diag_ts.get("unverified", 0.0) > 5.0:
                _click_diag_ts["unverified"] = now
                ui(lambda i=idx: print(f"[LEDGER] 卡片{i + 1} 点击未记账："
                                       "海克斯名未识别出来（界面刚出现"
                                       "或已关闭），已忽略；若卡面确有名字，"
                                       "可能是知识库落后于游戏版本，"
                                       "请刷新 augments.json"))
            return
        ok = session.record_augment(name, card_idx=idx, round_id=rid)
        _blackbox("recorded" if ok else "duplicate", name)
        if ok:
            detector.last_cards = None   # 残留卡名作废（选卡完成界面即关，
            detector.last_cards_raw = None   # 窗口期内读数只该走向丢弃分支）
            if crop is not None:
                detector.async_ocr_card_desc(
                    crop, lambda text, n=name: (
                        kb.record_augment_desc(n, text[:120])
                        if text and len(text) >= 8 else None))
            ui(lambda n=name: print(f"[LEDGER] 记账: 卡片{idx + 1} {n}"))
        else:
            ui(lambda n=name: print(f"[LEDGER] {n} 已在台账，忽略重复记录"))

    def _on_reroll(x, y, ts=None):
        t_click = ts if ts is not None else time.time()
        if not detector.recent_hex_activity(ts=t_click):
            _click_diag("reroll_no_evidence",
                        f"({x},{y}) 刷新按钮命中但无海克斯证据",
                        ev={"x": x, "y": y, "card": None, "ts": t_click})
            return   # 界面关闭 >10s 后的对局点击不再触发重扫/刷日志（同 _on_click 门）
        if session.round_recorded(detector.core.round_id):
            _click_diag("reroll_round_recorded",
                        f"({x},{y}) 刷新按钮命中但本轮已记账（选卡后游玩"
                        "点击，防重扫/浮窗状态被误触发）",
                        ev={"x": x, "y": y, "card": None, "ts": t_click})
            return
        detector.force_rescan()
        detector.last_cards = None  # 重扫稳定前点击不记账（防按旧卡片名记录）
        detector.last_cards_raw = None
        session.on_hex_reroll()     # 浮窗回"识别中"，选中标记作废
        session.log_click({"x": x, "y": y, "card": None, "branch": "reroll",
                           "result": "reroll_rescan", "name": None,
                           "raw": None, "image": None, "ts": t_click})
        ui(lambda: print("[LEDGER] reroll → 已触发重扫"))

    ledger = MouseLedger(
        on_click=_on_click,
        on_reroll=_on_reroll,
        reroll_rect_provider=_reroll_rects,
    )

    # ── UI ────────────────────────────────────────────────────────
    def _open_settings():
        """设置窗口单例：已开则置前，防多实例同时写 settings"""
        global _settings_win
        try:
            if _settings_win is not None and _settings_win.winfo_exists():
                _settings_win.deiconify()
                _settings_win.lift()
                _settings_win.focus_force()
                return
        except Exception:
            pass
        _settings_win = SettingsWindow(
            root, settings, kb,
            engine_stats_fn=lambda: session.engine.stats(),
            rebuild_engine_fn=_rebuild_engine)

    _settings_win = None

    def _start_calibrate():
        """倒计时标定：给用户 10 秒把海克斯三选一界面切到前台（历史问题：
        点按钮的瞬间界面往往已切回战斗画面，立即截图必失败）。"""
        def run():
            try:
                ok = detector.auto_calibrate(delay=10.0, attempts=5, interval=2.0)
            except Exception as e:  # noqa: BLE001 —— 标定失败要反馈而非静默
                ok = False
                print(f"[CALIB] 标定异常: {e}")
            ui(lambda: print("[CALIB] 标定成功，区域已按本机分辨率保存"
                             if ok else
                             "[CALIB] 标定失败（倒计时内需让海克斯三选一界面"
                             "在前台且卡面稳定；也可等界面出现时保持不动自动标定）"))
        ui(lambda: print("[CALIB] 标定中…（10 秒内把海克斯三选一界面切到前台）"))
        threading.Thread(target=run, daemon=True, name="Calibrate").start()

    # ── 数据刷新：手动按钮 + 启动延迟自动抓取（P3 杂项①） ──────────
    # 任务级在途闸：按钮侧本有 _refresh_busy 防自重入，但自动路径绕过
    # 按钮——手动全量抓取要几分钟，60s 自动抓取到点完全可能撞上在途的
    # 手动刷新，两路并发写 data/ 会半新半旧，故共用任务体统一持闸
    _refresh_mutex = threading.Lock()
    _refresh_state = {"running": False}

    def _run_data_refresh(on_done=None):
        """刷新任务体（手动按钮与启动延迟自动抓取共用，同源同参）：后台
        线程顺序跑 OP.GG 重抓 + 知识派生重生成，输出逐行 print 进日志
        面板。脚本产物落 data/，知识库启动时一次性加载——完成后提示
        重启生效（不做运行时热替换 kb，防锁内状态半新半旧）。打包版无
        scripts/ 目录，直接提示不支持。on_done 为空即自动路径（无按钮
        要恢复）。"""
        with _refresh_mutex:
            if _refresh_state["running"]:
                print("[DATA] 数据刷新已在进行，本次跳过（防两路并发重抓写坏 data/）")
                if on_done:
                    ui(on_done)
                return
            _refresh_state["running"] = True

        scripts_dir = BASE_DIR / "scripts"
        jobs = [["fetch_opgg_builds.py", "--all"],
                ["gen_champ_profile.py"]]
        if not scripts_dir.exists():
            with _refresh_mutex:
                _refresh_state["running"] = False
            ui(lambda: print("[DATA] 打包版无 scripts/ 目录，请在源码目录运行刷新"))
            if on_done:
                ui(on_done)   # 恢复按钮的回调必须回 UI 线程（Tk 跨线程唯一通道）
            return

        def run():
            import subprocess
            ok = True
            try:
                for job in jobs:
                    script = scripts_dir / job[0]
                    if not script.exists():
                        print(f"[DATA] 缺少 {job[0]}，跳过")
                        continue
                    print(f"[DATA] 运行 {job[0]} {' '.join(job[1:])} …")
                    try:
                        proc = subprocess.Popen(
                            [sys.executable, str(script), *job[1:]],
                            cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
                        for line in proc.stdout or []:
                            print(f"[DATA] {line.rstrip()}")
                        code = proc.wait()
                        if code != 0:
                            ok = False
                            print(f"[WARN] {job[0]} 退出码 {code}")
                    except Exception as e:  # noqa: BLE001 —— 单脚本失败不断链
                        ok = False
                        print(f"[WARN] {job[0]} 运行异常: {e}")
            finally:
                with _refresh_mutex:
                    _refresh_state["running"] = False
            ui(lambda: print(
                "[OK] 数据已刷新，重启应用后生效（知识库启动时加载）"
                if ok else
                "[WARN] 数据刷新部分失败，详见上方 [DATA] 输出"))
            if on_done:
                ui(on_done)   # DataRefresh 线程内直调会跨线程摸 Tk 控件（Tkinter 非线程安全）

        ui(lambda: print("[DATA] 开始刷新（OP.GG 全量抓取需几分钟，可继续游戏）"))
        threading.Thread(target=run, daemon=True, name="DataRefresh").start()

    def _start_data_refresh(on_done):
        """手动『刷新数据』按钮入口（行为不变）。顺带取消挂起的启动延迟
        自动抓取——用户已手动触发，到点就不必再后台抓一遍。"""
        auto_refresh.cancel()
        _run_data_refresh(on_done)

    def _auto_refresh_fire():
        """启动延迟自动抓取到点：走与手动按钮同一个任务体（同源同参），
        无按钮回调。"""
        print(f"[DATA] 启动已 {AUTO_REFRESH_DELAY_S:.0f}s，自动刷新开始"
              "（OP.GG 全量抓取在后台跑，可继续游戏）")
        _run_data_refresh(None)

    auto_refresh = DelayedAutoRefresh(AUTO_REFRESH_DELAY_S)

    main_win = MainWindow(root, session, ui_queue, log_queue,
                          on_open_settings=_open_settings,
                          on_calibrate=_start_calibrate,
                          on_refresh_data=_start_data_refresh)
    overlay = Overlay(root, session, ui_queue,
                      visible=bool(settings.get("ui", "overlay_visible", default=True)))

    def _rebuild_engine():
        new_engine = engine_mod.build_engine(kb, rules, settings)
        session.replace_engine(new_engine)

    # 热键（F10 overlay 显隐 + F9 撤销记账；Tk 调度统一走 ui 队列）
    # GetAsyncKeyState 轮询而非 keyboard 库——keyboard 在 import 时就装全局
    # 键盘低级钩子，钩子回调依赖本进程线程调度，进程有 CPU 突发时全系统
    # 按键都会延迟（游戏内"动不了"的元凶）。本工具只需 F10/F9，无需钩子。
    # F10 保持全局（游戏内也要能开关浮窗）；F9 改台账，只在本工具窗口
    # 处于前台时生效（见 _foreground_is_own_window）。
    def _foreground_is_own_window():
        """F9 前台校验：前台窗口是否属于本进程。

        EnumWindows 枚举本进程全部顶层窗口（主窗/浮窗/设置窗任一在前台
        即算"自己"），与 GetForegroundWindow 句柄比对——没有这层校验时，
        在浏览器/IDE 里按 F9 也会静默撤销台账且不可逆。句柄集合每次现查
        不缓存（设置窗可能后开、主窗可能缩托盘），仅在按键命中时调用，
        开销可忽略。
        """
        user32 = ctypes.windll.user32
        # 默认 restype 是 c_int，64 位下高位句柄带符号，会与枚举集合比对失配
        user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        hwnds = set()
        pid_self = os.getpid()

        # 回调引用必须存活到 EnumWindows 返回（ctypes 不保管局部 CFUNCTYPE）
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                            ctypes.wintypes.LPARAM)
        def _on_enum(hwnd, _lparam):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == pid_self:
                hwnds.add(hwnd)
            return True

        user32.EnumWindows(_on_enum, 0)
        return user32.GetForegroundWindow() in hwnds

    def _revoke_latest_augment():
        """F9：撤销最近一条海克斯记账（误点快速纠错）。

        仅在本工具窗口处于前台时生效（hotkey_loop 先经
        _foreground_is_own_window 校验；游戏内误点需切回主窗口再按 F9，
        或点台账行「撤销」按钮）。末条读取与撤销在 session RLock 内原子
        完成（revoke_latest_augment 封装），在本热键线程直接执行——只做
        规则级重算，无重活，不进 Tk 主线程；主窗口台账由 500ms ui_state
        轮询自动刷新（≤0.5s 可见）。
        """
        name = session.revoke_latest_augment()
        if name:
            print(f"[INFO] 已撤销最近海克斯记账：{name}")
        else:
            print("[INFO] F9 撤销：台账为空，无可撤销")

    def hotkey_loop():
        import ctypes
        VK_F9 = 0x78
        VK_F10 = 0x79
        last_f9 = 0.0
        last_f10 = 0.0
        while True:
            try:
                now = time.time()
                if (ctypes.windll.user32.GetAsyncKeyState(VK_F10) & 0x8000
                        and now - last_f10 > 0.8):
                    last_f10 = now
                    ui_queue.put(overlay.toggle)
                if (ctypes.windll.user32.GetAsyncKeyState(VK_F9) & 0x8000
                        and now - last_f9 > 0.8):
                    last_f9 = now
                    if _foreground_is_own_window():
                        _revoke_latest_augment()
                    else:
                        print("[INFO] F9 忽略：本工具窗口不在前台"
                              "（防其他应用误按）；撤销请切回主窗口再按 F9"
                              "或点台账行「撤销」按钮")
            except Exception:
                pass
            time.sleep(0.05)
    threading.Thread(target=hotkey_loop, daemon=True, name="Hotkeys").start()

    # 系统托盘（fork 参考项目；回调同样经 ui 队列进 Tk 主线程）
    tray = start_tray(root, ui_queue)
    root.protocol("WM_DELETE_WINDOW", lambda: (hide_to_tray(root)))

    # 对局阶段 → 检测器暂停/恢复（非对局期不截屏不 OCR）
    def phase_watch():
        try:
            if session.stage in ("game_start", "in_game"):
                detector.resume()
            else:
                detector.pause()
        except Exception:
            pass
        root.after(2000, phase_watch)

    # 启动服务
    print(f"[OK] HexARAMHelper 启动（决策引擎: {engine.mode}）")
    print(f"[OK] 数据版本: {kb.dd_version} | builds: {kb.builds_patch or '-'} | "
          f"AI: {'开' if settings.get('ai','enabled') else '关（规则模式）'}")
    live.start()
    phase.start()
    detector.pause()   # 初始待机，phase_watch 按对局阶段恢复
    detector.start()
    ledger.start()
    phase_watch()
    # 启动延迟自动抓取（P3 杂项①）：启动满 60s 才后台抓 OP.GG，避开
    # 启动瞬间的网络/CPU 抢占；挂起中的抓取在退出路径 finally 与手动
    # 按钮入口都会被取消，手动按钮行为不变
    auto_refresh.schedule(_auto_refresh_fire)
    root.deiconify()
    if os.environ.get("HEXA_SMOKE"):
        # UI 冒烟模式：3 秒后自动退出（验证 GUI 可启动，无人工交互）
        root.after(3000, root.destroy)
    if os.environ.get("HEXA_MOCK"):
        # mock 驱动一局（无需游戏客户端）：选人→加载→海克斯时刻→记账→出门装
        # →出装主线（金币感知）→死亡→敌方关键装
        def mock_drive():
            import time as _t
            from hexa.core.live_api import LiveSnapshot, PlayerInfo
            _t.sleep(2.5)
            session.on_champ_select({"ally": ["暗裔剑魔"], "enemy": [],
                                     "my_hero": "暗裔剑魔", "source": "mock"})
            _t.sleep(1.0)
            session.on_game_start({
                "ally": ["暗裔剑魔", "九尾妖狐", "皎月女神", "盲僧", "众星之子"],
                "enemy": ["黑暗之女", "狂暴之心", "放逐之刃", "诺克萨斯之手", "琴瑟仙女"],
                "my_hero": "暗裔剑魔", "source": "mock"})
            session.on_in_progress()
            _t.sleep(1.0)
            # 海克斯时刻：三卡片评级（AI 引擎会异步精修，规则基准即时出）
            top = kb.top_augments("暗裔剑魔", 3)
            cards = (["贪欲束缚"] + [n for n in top if n != "贪欲束缚"])[:3]
            session.rank_augment_cards(cards)
            _t.sleep(2.0)
            session.record_augment("贪欲束缚", tier="棱彩")

            def snap(items, dead=False, enemy_items=None):
                s = LiveSnapshot(ts=_t.time(), ok=True)
                s.my_champion = "暗裔剑魔"
                s.my_is_dead = dead
                s.my_current_gold = 4000   # 演示金币感知：够买第 1 件 + 攒第 2 件
                s.my_items = [{"name": n, "count": 1} for n in items]
                s.players.append(PlayerInfo(champion="暗裔剑魔", champion_en="Aatrox",
                                            team="ORDER", items=s.my_items,
                                            kills=2, deaths=dead and 1 or 0, assists=3))
                s.players.append(PlayerInfo(champion="诺克萨斯之手", champion_en="Darius",
                                            team="CHAOS",
                                            items=[{"name": n, "count": 1}
                                                   for n in (enemy_items or ["多兰之刃"])],
                                            kills=5, deaths=1, assists=3))
                for cn in ("黑暗之女", "狂暴之心", "琴瑟仙女"):
                    s.players.append(PlayerInfo(champion=cn, champion_en="", team="CHAOS"))
                session.on_snapshot(s)

            snap(["多兰之刃"])
            _t.sleep(1.2)
            snap(["多兰之刃", "吸血鬼节杖"])
            _t.sleep(0.8)
            snap(["多兰之刃", "吸血鬼节杖"], dead=True)            # 死亡 → 下一件三选
            _t.sleep(10.0)
            snap(["多兰之刃", "吸血鬼节杖"], enemy_items=["荆棘之甲"])  # 敌方关键装提示
        threading.Thread(target=mock_drive, daemon=True, name="MockDrive").start()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        auto_refresh.cancel()   # 挂起的启动延迟刷新随应用退出作废（P3 杂项①）
        phase.stop()
        live.stop()
        detector.stop()
        ledger.stop()
        session.flush_pending_review()   # 复盘日志兜底②：退出前补写未落盘的局
        if tray:
            tray.stop()


def hide_to_tray(root: tk.Tk):
    root.withdraw()


def start_tray(root: tk.Tk, ui_queue: queue.Queue):
    try:
        import pystray
        from PIL import Image, ImageDraw
        import math

        def _icon():
            size = 64
            img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            cx = cy = size // 2
            r = size // 2 - 4
            pts = [(cx + r * math.cos(math.radians(60 * i - 30)),
                    cy + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
            draw.polygon(pts, fill=(13, 17, 23, 255), outline=(88, 166, 255, 255))
            r2 = r * 0.55
            inner = [(cx + r2 * math.cos(math.radians(60 * i - 30)),
                      cy + r2 * math.sin(math.radians(60 * i - 30))) for i in range(6)]
            draw.polygon(inner, fill=(88, 166, 255, 200))
            return img

        icon = pystray.Icon(
            "HexARAMHelper", _icon(), "海克斯大乱斗助手",
            pystray.Menu(
                pystray.MenuItem("显示主窗口",
                                 lambda: ui_queue.put(root.deiconify)),
                pystray.MenuItem("退出",
                                 lambda: ui_queue.put(root.destroy)),
            ))
        threading.Thread(target=icon.run, daemon=True, name="Tray").start()
        return icon
    except Exception as e:
        print(f"[WARN] 托盘启动失败（不影响使用）: {e}")
        return None


if __name__ == "__main__":
    main()
