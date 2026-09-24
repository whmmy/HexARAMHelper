"""HexARAMHelper 入口：主窗口 + overlay + 服务编排

生命周期全自动（LCU 阶段状态机驱动），无操作热键；
仅保留一个可选 overlay 显隐键（F10）。
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

    from hexa.core.fixtures import FixtureDumper
    from hexa.core.game_state import GameSession
    from hexa.core.live_api import LivePoller
    from hexa.core.lcu import LCUConnector, PhaseMachine
    from hexa.core.rules_engine import RulesEngine
    from hexa.knowledge.kb import KnowledgeBase
    from hexa.ui.main_window import MainWindow
    from hexa.ui.overlay import Overlay
    from hexa.ui.theme import LogRedirector

    settings = Settings()
    kb = KnowledgeBase()
    rules = RulesEngine(kb)
    dumper = FixtureDumper(enabled=bool(settings.get("fixtures", "dump", default=False)))

    # UI 队列（后台线程 → UI 线程）
    ui_queue = queue.Queue()
    log_queue = queue.Queue()

    root = tk.Tk()
    root.withdraw()  # 主窗口在初始化完成后再显示

    # stdout/stderr → 日志面板
    sys.stdout = LogRedirector(log_queue, sys.stdout)
    sys.stderr = LogRedirector(log_queue, sys.stderr)

    # 服务编排
    live = LivePoller(kb, fixture_dumper=dumper)
    session = GameSession(kb, rules, settings=settings,
                          fixture_dumper=dumper, live_poller=live)
    live.on_snapshot = session.on_snapshot

    lcu = LCUConnector(kb)
    phase = PhaseMachine(lcu, callbacks={
        "on_champ_select": session.on_champ_select,
        "on_game_start": session.on_game_start,
        "on_in_progress": session.on_in_progress,
        "on_end_of_game": session.on_end_of_game,
    })

    # UI
    main_win = MainWindow(root, session, ui_queue, log_queue)
    overlay = Overlay(root, session, ui_queue,
                      visible=bool(settings.get("ui", "overlay_visible", default=True)))

    # overlay 显隐键（唯一热键，可选项）
    def hotkey_loop():
        import keyboard
        last = 0.0
        while True:
            try:
                if keyboard.is_pressed("f10") and time.time() - last > 0.8:
                    last = time.time()
                    ui_queue.put("overlay_toggle")
                    root.after(0, overlay.toggle)
            except Exception:
                pass
            time.sleep(0.05)
    threading.Thread(target=hotkey_loop, daemon=True, name="Hotkeys").start()

    # 系统托盘（fork 参考项目）
    tray = start_tray(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (hide_to_tray(root)))

    # 启动服务
    print("[OK] HexARAMHelper 启动（P0 骨架·规则模式）")
    print(f"[OK] 数据版本: {kb.dd_version} | AI: {'开' if settings.get('ai','enabled') else '关（规则模式）'}")
    live.start()
    phase.start()
    root.deiconify()
    if os.environ.get("HEXA_SMOKE"):
        # UI 冒烟模式：3 秒后自动退出（验证 GUI 可启动，无人工交互）
        root.after(3000, root.destroy)
    if os.environ.get("HEXA_MOCK"):
        # mock 驱动一局（无需游戏客户端）：选人→加载→对局→海克斯→死亡→敌方关键装
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
        if tray:
            tray.stop()


def hide_to_tray(root: tk.Tk):
    root.withdraw()


def start_tray(root: tk.Tk):
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
                pystray.MenuItem("显示主窗口", lambda: root.after(0, root.deiconify)),
                pystray.MenuItem("退出", lambda: root.after(0, root.destroy)),
            ))
        threading.Thread(target=icon.run, daemon=True, name="Tray").start()
        return icon
    except Exception as e:
        print(f"[WARN] 托盘启动失败（不影响使用）: {e}")
        return None


if __name__ == "__main__":
    main()
