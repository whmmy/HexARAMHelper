"""HexARAMHelper 入口：主窗口 + overlay + 服务编排

生命周期全自动（LCU 阶段状态机驱动），无操作热键；
仅保留一个可选 overlay 显隐键（F10）。

P1 起：画面检测线程（标题条 OCR + 229 库匹配，迟滞）+ 只读鼠标记账
（海克斯界面活跃窗口内点击卡片 → 台账；点 reroll → 主动重扫）。
P2 起：AI 双槽位引擎（settings 开关；无 Key 自动纯规则）。
"""
import ctypes
import os
import queue
import sys
import threading
import time

import tkinter as tk


def main():
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
    from hexa.core.screen_detect import ScreenDetector, load_layout
    from hexa.knowledge.kb import KnowledgeBase
    from hexa.ui.main_window import MainWindow
    from hexa.ui.overlay import Overlay
    from hexa.ui.settings_window import SettingsWindow
    from hexa.ui.theme import LogRedirector

    settings = Settings()
    kb = KnowledgeBase()
    rules = RulesEngine(kb)
    dumper = FixtureDumper(enabled=bool(settings.get("fixtures", "dump", default=False)))

    # UI 队列（后台线程 → UI 线程；可承载事件串与可调用对象）
    ui_queue = queue.Queue()
    log_queue = queue.Queue()

    root = tk.Tk()
    root.withdraw()  # 主窗口在初始化完成后再显示

    # stdout/stderr → 日志面板
    sys.stdout = LogRedirector(log_queue, sys.stdout)
    sys.stderr = LogRedirector(log_queue, sys.stderr)

    # UI 线程调度 helper（任意线程 → mainloop）
    def ui(fn):
        ui_queue.put(fn)

    # ── 决策引擎（规则 / AI 双槽位） ──────────────────────────────
    engine = engine_mod.build_engine(kb, rules, settings)
    live = LivePoller(kb, fixture_dumper=dumper)
    session = GameSession(kb, rules, settings=settings,
                          fixture_dumper=dumper, live_poller=live,
                          engine=engine)
    live.on_snapshot = session.on_snapshot

    lcu = LCUConnector(kb)
    phase = PhaseMachine(lcu, callbacks={
        "on_champ_select": session.on_champ_select,
        "on_game_start": session.on_game_start,
        "on_in_progress": session.on_in_progress,
        "on_end_of_game": session.on_end_of_game,
    })

    # ── P1 交互闭环：画面检测 + 鼠标记账 ──────────────────────────
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    ocr = OcrEngine(kb)
    detector = ScreenDetector(kb, ocr, layout=load_layout(screen_w, screen_h))
    detector.last_cards = None          # 最近一轮稳定卡片名（记账映射用）

    def _on_state(active, round_id):
        if active:
            ui(lambda: print(f"[DETECT] 海克斯界面活跃（第 {round_id} 轮）"))
        else:
            ui(lambda: print("[DETECT] 海克斯界面结束"))
            detector.last_cards = None
            session.clear_card_ranks()   # 时刻块随界面消失（台账仍在）

    def _on_cards(stable_cards, round_id):
        detector.last_cards = list(stable_cards)  # 含 None 占位（保证卡片序对齐）
        names = [n for n in stable_cards if n]
        print(f"[DETECT] 本轮卡片: {names}")
        ranks = session.rank_augment_cards(stable_cards)  # 同步基准 + AI 异步精修
        ui(lambda: print("[AI] 海克斯评级: " +
                         ", ".join(f"{r['name']}[{r['grade']}]" for r in ranks)))

    def _on_calibrate_hint():
        ui(lambda: print("[WARN] 海克斯识别连续失配——界面可能改版/漂移，"
                         "请到设置重新标定（海克斯界面出现时点击主窗口\"开始标定\"）"))

    detector.on_state = _on_state
    detector.on_cards = _on_cards
    detector.on_calibrate_hint = _on_calibrate_hint

    card_ledger = CardLedger(rects_provider=lambda: (
        detector.layout.to_pixels(screen_w, screen_h)["cards"]
        if detector.layout else None))

    def _on_click(x, y):
        idx = card_ledger.card_at(x, y)
        if idx is None or not detector.last_cards or idx >= len(detector.last_cards):
            return
        name = detector.last_cards[idx]
        if not name or not kb.valid_augment(name):
            return
        if session.record_augment(name):
            ui(lambda n=name: print(f"[LEDGER] 记账: 卡片{idx + 1} {n}"))

    def _on_reroll():
        detector.force_rescan()
        detector.last_cards = None  # 重扫稳定前点击不记账（防按旧卡片名记录）
        ui(lambda: print("[LEDGER] reroll → 已触发重扫"))

    ledger = MouseLedger(
        on_click=_on_click,
        gate=lambda: detector.core.state == "active",
        on_reroll=_on_reroll,
        reroll_rect_provider=lambda: (
            detector.layout.to_pixels(screen_w, screen_h)["reroll"]
            if detector.layout else None),
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
        def run():
            try:
                ok = detector.auto_calibrate()
            except Exception as e:  # noqa: BLE001 —— 标定失败要反馈而非静默
                ok = False
                print(f"[CALIB] 标定异常: {e}")
            ui(lambda: print("[CALIB] 标定成功，区域已按本机分辨率保存"
                             if ok else "[CALIB] 标定失败（确认海克斯界面在屏幕上后重试）"))
        ui(lambda: print("[CALIB] 标定中…（保持海克斯界面在屏幕上）"))
        threading.Thread(target=run, daemon=True, name="Calibrate").start()

    main_win = MainWindow(root, session, ui_queue, log_queue,
                          on_open_settings=_open_settings,
                          on_calibrate=_start_calibrate)
    overlay = Overlay(root, session, ui_queue,
                      visible=bool(settings.get("ui", "overlay_visible", default=True)))

    def _rebuild_engine():
        new_engine = engine_mod.build_engine(kb, rules, settings)
        session.replace_engine(new_engine)

    # overlay 显隐键（唯一热键，可选项；Tk 调度统一走 ui 队列）
    def hotkey_loop():
        import keyboard
        last = 0.0
        while True:
            try:
                if keyboard.is_pressed("f10") and time.time() - last > 0.8:
                    last = time.time()
                    ui_queue.put(overlay.toggle)
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
        phase.stop()
        live.stop()
        detector.stop()
        ledger.stop()
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
