"""海克斯界面检测（计划 §3 / §7 core/screen_detect.py）

架构：DetectorCore（纯逻辑状态机，可离线测试）+ ScreenDetector（截屏线程）。

主信号：标题条小区域 OCR + 229 海克斯库模糊匹配（每 2s）——
≥2 个标题命中且连续 2 帧 = 界面出现；连续 3 帧无命中 = 消失（迟滞防误触）。
界面活跃期间持续比对卡片内容：内容变化（reroll/新轮）即重启识别，
稳定 2 帧才回调 on_cards 计入推荐/台账。
OCR 连续失配 = UI 漂移/改版 → on_calibrate_hint 提示重标定，不默默推错。
模板匹配（cv2.matchTemplate）退为预热加速与 OCR 异常兜底。

游戏窗口定位（窗口化/副屏适配）：ctypes FindWindow 查游戏窗口（类名
"Riot Window" + 标题 "League of Legends (TM) Client"，EnumWindows 模糊
兜底）→ GetClientRect + ClientToScreen 取客户区屏幕矩形。客户区 ≠ 主屏
整屏覆盖时：截屏改 mss 区域抓取（left/top/width/height 绝对坐标，副屏
可为负），Layout 按客户区尺寸换算并绑定原点偏移（to_pixels 默认输出
屏幕坐标供鼠标记账直读；canvas=True 输出画布坐标供截屏帧裁剪）；查不
到窗口（独占全屏等）回退 monitors[1] 整屏行为，打一行 [INFO]。

区域坐标（Layout）：默认层 = 16:9 百分比常量（2560x1440 真机实测值，
media/analysis/ 可复现）；加载时把横向偏移换算为「高度基准 + 画布中心
锚定」单位（H 相对量），16:10/21:9/32:9 兜得住；标定结果按画布分辨率存
calibrations/{W}x{H}.json（整屏=屏幕分辨率，窗口化=客户区尺寸；gitignore
排除，用户屏幕专属）；自动标定 =
中央横带全量 OCR 找三组海克斯标题聚类 → 几何校验 → 按实测锚点推导
（cards 覆盖含卡名的完整卡框；刷新按钮每卡一个共 3 个）。
界面活跃期还会自愈：未标定直接落标定，已标定检测到名行漂移自动重标。
"""
import ctypes
import ctypes.wintypes   # Win32 窗口定位用（本项目 Windows 专属，同 mouse_ledger）
import json
import threading
import time
from pathlib import Path

from hexa.config import BASE_DIR

# 标定文件放应用根下可写目录（PyInstaller 冻结后包目录只读；
# gitignore 排除 *.json，用户屏幕专属不入库）
CALIB_DIR = BASE_DIR / "calibrations"

# ── 默认布局（16:9 比例；2026-09-25 由 2560x1440 真机海克斯界面整帧
# OCR + 金框扫描线实测，10 个录像关键帧几何一致，见 media/analysis/） ──
# title_bands: 三个卡名文本带 (x, y, w, h)；cards: 三张卡片全框（OCR 区 +
# 点击热区，严禁交叠）；rerolls: 每卡下方各一个刷新按钮（共 3 个）。
# 均为 16:9 屏幕比例；卡名行真值 y 0.398~0.427，卡框 y 0.176~0.665，
# 刷新按钮 y 0.685~0.727（中心 x 与三卡中心一一对齐）。
DEFAULT_LAYOUT_169 = {
    "title_bands": [[0.2285, 0.3910, 0.1656, 0.0445],
                    [0.4199, 0.3910, 0.1656, 0.0445],
                    [0.6113, 0.3910, 0.1664, 0.0445]],
    "cards":       [[0.2285, 0.1757, 0.1656, 0.4890],
                    [0.4199, 0.1757, 0.1656, 0.4890],
                    [0.6113, 0.1757, 0.1664, 0.4890]],
    "rerolls":     [[0.2915, 0.6770, 0.0440, 0.0550],
                    [0.4831, 0.6770, 0.0440, 0.0550],
                    [0.6739, 0.6770, 0.0440, 0.0550]],
}
ASPECT_169 = 16 / 9

# 标定/模板落盘互斥（写者：检测线程自愈标定 + 手动标定线程；
# save_layout 与 _save_templates 共用，防两个线程交错写坏同一文件）
_SAVE_LOCK = threading.Lock()


class Layout:
    """区域布局：内部以「高度相对量 + 中心锚定」存储，适配任意宽高比。

    存储单元：x 用相对画布中心（整屏=屏幕 / 窗口化=客户区）的 H 偏移
    （xu = (fx-0.5)*16/9），y/w/h 用 H 相对量（w 预乘 16/9）。to_pixels 时：
      px_x = W/2 + xu*H ; px_y = y*H ; px_w = wu*H ; px_h = h*H
    （W/H = 画布尺寸；已绑定窗口时再加客户区原点偏移成屏幕坐标）
    """

    def __init__(self, data: dict = None, source: str = "default"):
        d = data or DEFAULT_LAYOUT_169
        self.source = source
        # 游戏窗口几何绑定（窗口化/副屏适配）：ScreenDetector 每 tick 刷新；
        # 绑定后 to_pixels 以客户区尺寸换算并加原点偏移输出屏幕坐标
        self.window = None   # (origin_x, origin_y, w, h) | None
        rerolls = self._rerolls_of(d)
        if d.get("units") == "h":
            # 自动标定按真实分辨率直接换算的 H 单位（任意宽高比精确）
            self.title_bands = [list(r) for r in d["title_bands"]]
            self.cards = [list(r) for r in d["cards"]]
            self.rerolls = [list(r) for r in rerolls]
        else:
            self.title_bands = [self._to_h_units(r) for r in d["title_bands"]]
            self.cards = [self._to_h_units(r) for r in d["cards"]]
            self.rerolls = [self._to_h_units(r) for r in rerolls]

    @staticmethod
    def _rerolls_of(d: dict) -> list:
        """刷新按钮矩形 ×3；旧标定只存单 reroll → 复制三份兜底读取
        （旧公式推导的矩形本身已坏、常在屏幕外，复制仅为兼容不崩）。"""
        rr = d.get("rerolls")
        if not rr and "reroll" in d:
            rr = [d["reroll"]]
        if not rr:
            rr = DEFAULT_LAYOUT_169["rerolls"]
        if len(rr) != 3:
            rr = (list(rr) * 3)[:3]
        return rr

    @staticmethod
    def _to_h_units(r):
        x, y, w, h = r
        return [(x - 0.5) * ASPECT_169, y, w * ASPECT_169, h]

    @staticmethod
    def _from_h_units(r):
        xu, y, wu, h = r
        return [xu / ASPECT_169 + 0.5, y, wu / ASPECT_169, h]

    def bind_window(self, geom):
        """绑定游戏窗口客户区几何 (origin_x, origin_y, w, h)；None 解绑。

        绑定后 to_pixels 默认输出屏幕坐标（原点加到 x/y）——MouseLedger 的
        屏幕落点/卡片矩形映射直读；canvas=True 输出画布坐标（客户区左上
        为原点）——检测线程对截屏帧裁剪用。"""
        self.window = tuple(int(v) for v in geom) if geom else None

    def to_pixels(self, w: int = None, h: int = None, *, canvas: bool = False):
        """比例矩形 → 像素矩形（title_bands/cards/rerolls 三组）。

        未绑定窗口（整屏模式）：w/h = 屏幕分辨率，输出屏幕坐标（原点 0,0）。
        已绑定窗口：画布 = 客户区尺寸，绑定值优先于入参——旧调用方
        （app.py 闭包）捕获的是启动时屏幕分辨率，窗口化后已过期，被绑定
        值覆盖才能保证卡片矩形与鼠标落点不错位。
        canvas=True：返回画布坐标（不加原点偏移），供截屏帧裁剪。"""
        win = self.window   # 一次快照：bind_window 会被检测线程并发刷新
        if win:
            ox, oy, w, h = (0, 0, win[2], win[3]) if canvas else win
        else:
            ox, oy = 0, 0
        if not w or not h:
            raise ValueError("to_pixels 缺画布尺寸：未绑定窗口且未传 w/h")

        def conv(r):
            xu, ry, wu, rh = r
            x = int(w / 2 + xu * h) + ox
            yy = int(ry * h) + oy
            return [x, yy, int(wu * h), int(rh * h)]
        return {"title_bands": [conv(r) for r in self.title_bands],
                "cards": [conv(r) for r in self.cards],
                "rerolls": [conv(r) for r in self.rerolls]}

    def as_169(self) -> dict:
        return {"title_bands": [self._from_h_units(r) for r in self.title_bands],
                "cards": [self._from_h_units(r) for r in self.cards],
                "rerolls": [self._from_h_units(r) for r in self.rerolls]}


def load_layout(screen_w: int, screen_h: int) -> Layout:
    """分辨率专属标定 → 默认布局（返回 Layout 与是否已标定）"""
    path = CALIB_DIR / f"{screen_w}x{screen_h}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Layout(data.get("regions"), source=f"calib:{path.name}")
        except Exception as e:
            print(f"[WARN] 标定文件损坏，用默认布局: {e}")
    return Layout()


def save_layout(screen_w: int, screen_h: int, regions: dict, confidence: float):
    """标定落盘。写者可能有两个（检测线程自愈标定 + 手动标定线程），
    全部经 _SAVE_LOCK 串行 + tmp→replace 原子替换，防止交错写损坏 JSON
    （读侧 load_layout 借 replace 原子性永远读到完整旧版或完整新版）。"""
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    path = CALIB_DIR / f"{screen_w}x{screen_h}.json"
    doc = {"screen": [screen_w, screen_h], "confidence": confidence,
           "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "regions": regions}
    with _SAVE_LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    print(f"[CALIB] 已保存标定 {path.name}（置信度 {confidence:.2f}）")


def point_in_rect(x: int, y: int, rect) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


# ── 游戏窗口定位（窗口化/副屏适配；纯 Win32 只读查询，不注入不拦截） ──

GAME_WINDOW_CLASS = "Riot Window"                    # LOL 游戏内主窗口类名
GAME_WINDOW_TITLE = "League of Legends (TM) Client"  # 窗口化时的标题栏标题
GAME_MIN_CLIENT = (640, 480)   # 客户区最小可信尺寸（游戏内最小 1024x576，
                               # 低于此 = 异常状态，按未找到窗口回退整屏）


def find_game_window():
    """查找游戏窗口 → hwnd | None。FindWindow 类名+标题精确匹配优先；
    查不到再 EnumWindows 按「类名 Riot Window + 标题含 League of
    Legends」模糊兜底（标题被录屏/挂件工具改写过的场景）。"""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(GAME_WINDOW_CLASS, GAME_WINDOW_TITLE)
    if hwnd:
        return hwnd
    found = []

    # 回调引用必须存活到 EnumWindows 返回（ctypes 不保管局部 CFUNCTYPE）
    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND,
                        ctypes.wintypes.LPARAM)
    def _on_enum(h, _lparam):
        if not user32.IsWindowVisible(h):   # 只看可见顶层窗口
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cls, 64)
        if cls.value != GAME_WINDOW_CLASS:
            return True
        title = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(h, title, 128)
        if "League of Legends" in title.value:
            found.append(h)
            return False                    # 找到即停
        return True

    user32.EnumWindows(_on_enum, 0)
    return found[0] if found else None


def game_window_geometry():
    """游戏窗口客户区的屏幕矩形 (origin_x, origin_y, w, h) | None。

    用客户区而非 GetWindowRect 全窗矩形：窗口化的标题栏/边框不属于游戏
    画面，截屏画布与卡片坐标都必须以客户区为基准（GetClientRect 的
    left/top 恒 0，原点用 ClientToScreen(0,0) 换算成屏幕坐标）。进程已
    SetProcessDpiAwareness(1)（app.py），Win32 返回物理像素，与 mss 抓
    屏/GetCursorPos 同一坐标系。最小化（IsIconic）或客户区过小 = 不可
    用，按未找到处理（调用方回退整屏）。"""
    hwnd = find_game_window()
    if not hwnd:
        return None
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        return None
    rc = ctypes.wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    cw, ch = rc.right - rc.left, rc.bottom - rc.top
    if cw < GAME_MIN_CLIENT[0] or ch < GAME_MIN_CLIENT[1]:
        return None
    pt = ctypes.wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return (pt.x, pt.y, cw, ch)


def capture_region(origin_x: int, origin_y: int, w: int, h: int) -> dict:
    """画布原点+尺寸 → mss 区域抓屏参数。绝对屏幕坐标可为负（副屏在
    主屏左侧时窗口原点为负——mss 按虚拟桌面坐标处理，负值合法）。"""
    return {"left": int(origin_x), "top": int(origin_y),
            "width": int(w), "height": int(h)}


def is_fullscreen_cover(geom, screen_w: int, screen_h: int) -> bool:
    """客户区是否恰好整屏覆盖主屏（无边框全屏 → 等价独占，走 monitors[1]）"""
    return tuple(geom) == (0, 0, int(screen_w), int(screen_h))


# ── 纯逻辑状态机（离线可测） ─────────────────────────────────────────

class DetectorCore:
    """海克斯界面出现/消失迟滞 + 卡片内容稳定判定。

    状态机只有 inactive/active 两态：连续 2 帧标题命中 → active；
    active 下连续 3 帧无命中 → inactive（迟滞防误触）。
    喂入：feed_titles(hits: int) 每帧标题命中数；
         feed_cards(card_texts: list[str|None]) 界面活跃期的每帧卡片识别结果。
    输出：state、stable_cards（同内容稳定 2 帧后置位一次，消费后调
         consume_cards()；内容不变不会重复置位——发射后闩锁，防每帧重发）。
    """

    APPEAR_FRAMES = 2     # 连续 2 帧命中 → 出现
    LEAVE_FRAMES = 3      # 连续 3 帧无命中 → 消失（迟滞）
    STABLE_FRAMES = 2     # 内容稳定 2 帧 → 允许计入台账
    MIN_TITLE_HITS = 2    # ≥2 个标题命中 = 界面出现

    def __init__(self):
        self.state = "inactive"
        self.round_id = 0            # 本局第几轮海克斯（内容变化即 +1）
        self._hit_run = 0
        self._miss_run = 0
        self._last_content = None
        self._content_run = 0
        self._emitted_content = None  # 发射后闩锁：同内容只发一次 on_cards
        self.stable_cards = None
        self.stable_round = 0

    def feed_titles(self, hits: int):
        if self.state == "inactive":
            if hits >= self.MIN_TITLE_HITS:
                self._hit_run += 1
                self._miss_run = 0
                if self._hit_run >= self.APPEAR_FRAMES:
                    self.state = "active"
                    self._hit_run = 0
                    self._content_run = 0
                    self._last_content = None
                    self._emitted_content = None
                    self.round_id += 1
                    print(f"[DETECT] 海克斯界面出现（第 {self.round_id} 轮）")
            else:
                self._hit_run = 0
        elif self.state == "active":
            if hits < self.MIN_TITLE_HITS:
                self._miss_run += 1
                if self._miss_run >= self.LEAVE_FRAMES:
                    self.state = "inactive"
                    self._miss_run = 0
                    self.stable_cards = None
                    self._emitted_content = None
                    print("[DETECT] 海克斯界面消失")
            else:
                self._miss_run = 0

    def feed_cards(self, card_texts: list):
        """active 状态下每帧喂卡片识别结果（list[str|None]，长度 3）。

        卡面流光动画下 OCR 逐帧抖动（真机 2026-09-27 复盘：整帧漏读与
        部分漏读交替 → 内容永不稳定 → last_cards 空 → 点击记账被静默
        丢弃）。容错：本帧读数是上帧的"子集"（None 槽 + 其余槽位同名）
        → 视为上帧内容的噪声重复继续计数；任一槽位读出不同的名字则按
        本帧原样处理（真实变化不被旧值污染）。"""
        if self.state != "active":
            return
        content = tuple(card_texts)
        if self._last_content is not None and all(
                n is None or n == p
                for n, p in zip(content, self._last_content)):
            content = self._last_content
        if content != self._last_content:
            self._last_content = content
            self._content_run = 1
            self.stable_cards = None
            if any(content):
                print(f"[DETECT] 卡片内容变化: {list(content)}")
        else:
            self._content_run += 1
            # 发射后闩锁：稳定 2 帧且该内容未发射过才置位（防每帧重发烧预算）
            if (self._content_run >= self.STABLE_FRAMES and any(content)
                    and self.stable_cards is None
                    and self._emitted_content != content):
                self.stable_cards = list(content)
                self.stable_round = self.round_id
                self._emitted_content = content
                print(f"[DETECT] 卡片内容稳定: {list(content)}")

    @property
    def last_content(self):
        """最近一帧（漏读补齐后）的卡片读数 tuple——OCR 迟滞与点击兜底用"""
        return self._last_content

    def consume_cards(self):
        out = self.stable_cards
        self.stable_cards = None
        return out

    def reset_round(self):
        """reroll/强制重扫：内容状态清零，等下一帧稳定（闩锁一并清）"""
        self._last_content = None
        self._content_run = 0
        self.stable_cards = None
        self._emitted_content = None


# ── 截屏检测线程 ─────────────────────────────────────────────────────

class ScreenDetector(threading.Thread):
    """每 2s 检测：非活跃期小区域模板预筛（几 ms），命中才 OCR；活跃期全帧识别。

    性能设计（对局 CPU 优化）：
    - 非活跃期（一局 95% 时间）只截 3 个标题条小图（非全屏），先 cv2.matchTemplate
      预筛——模板=上次界面激活时自动保存的标题条灰度图；未命中不跑 OCR。
    - 盲扫兜底：每 BLIND_OCR_EVERY 帧强制 OCR 一次（模板失效/未生成时也能发现
      界面，最迟 ~6s）；模板从第一次真实激活起就位，此后非活跃期几乎零 OCR。
    - 活跃期全帧截屏一次，标题 + 卡片共用（界面几十秒，成本可忽略）。

    回调（检测线程内执行，UI 消费请自行 queue/after 转发）：
      on_state(active: bool, round_id: int)
      on_cards(stable_cards: list[str], round_id: int)   # 稳定 2 帧后
      on_calibrate_hint()                                # OCR 连续失配
    """

    POLL_INTERVAL = 2.0    # 纯待机轮询间隔（非活跃且无模板证据）
    ACTIVE_POLL = 1.0      # 活跃期间隔（识别窗口就几十秒，抢稳定速度）
    WAKE_POLL = 0.5        # 模板命中但未确认活跃：短轮询加速迟滞确认
    MISMATCH_LIMIT = 5     # 连续 N 轮"标题命中但卡片全失配" → 提示重标定
    BLIND_OCR_EVERY = 3    # 非活跃期每 N 帧强制 OCR 一次（防模板漏检）
    TPL_HIT = 0.60         # matchTemplate 命中阈值（卡框美术恒定，文字占比小）
    AUTO_CALIB_TRIES = 3   # 每活跃期自愈标定最大尝试次数
    AUTO_CALIB_DRIFT = 40  # 已标定者：聚类中心均偏超过该像素数才重建（防抖动）

    def __init__(self, kb, ocr, layout: Layout = None):
        super().__init__(daemon=True, name="ScreenDetector")
        self.kb = kb
        self.ocr = ocr
        self.core = DetectorCore()
        self.layout = layout
        self.on_state = None
        self.on_cards = None
        self.on_calibrate_hint = None
        self._running = False
        self._paused = threading.Event()   # set = 暂停检测（非对局期）
        self._grab_lock = threading.Lock()  # mss 实例非线程安全
        self._mismatch_run = 0
        self._screen = None
        self.last_active = False
        self.last_cards_raw = None        # 最近一帧卡片读数（app 记账兜底）
        self._tick_count = 0
        self._tpl = None                  # [灰度标题条 ndarray ×3] 模板预筛
        self._tpl_loaded = False          # 懒加载一次
        self._auto_calib_tries = 0        # 本活跃期自愈标定尝试次数（≤3）
        self._auto_calib_done = False     # 本活跃期自愈标定已成功
        self._window = None               # 游戏窗口客户区几何（本 tick 生效）
        self._mode_logged = None          # 抓屏模式日志去重（window/screen）
        self._canvas_loaded = None        # 当前画布尺寸（变更时重读标定/模板）
        self._last_tpl_score = 0.0        # 最近一帧模板预筛分（0=无模板）
        self._last_hex_evidence = 0.0     # 最近海克斯证据时刻（点击记账门）
        # 点击证据落盘钩子（fn(idx, crop, tag) -> filename|None；None=不落）。
        # IO 策略在 app 侧（写 log/captures/ PNG），核心层只回调——
        # 2026-09-29 台账案：点击时刻定格帧落库，与 clicks 记录对账用
        self.capture_sink = None

    # ── 控制 ──────────────────────────────────────────────────────

    def set_layout(self, layout: Layout):
        # 窗口几何绑定跨 set_layout 保持：自动标定会换新 Layout 对象，
        # 不继承绑定的话 app.py 侧 to_pixels 立刻退回整屏坐标 → 记账错位
        if layout is not None and self.layout is not None \
                and self.layout.window and layout is not self.layout:
            layout.window = self.layout.window
        self.layout = layout

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def force_rescan(self):
        """reroll 按钮点击后调用：立即重启内容识别"""
        self.core.reset_round()

    def stop(self):
        self._running = False

    # ── 游戏窗口几何（窗口化/副屏适配） ──────────────────────────

    def _refresh_geometry(self):
        """每 tick 重查游戏窗口 → 绑定客户区几何；查不到回退整屏。

        - 找到窗口且客户区 ≠ 主屏整屏覆盖（窗口化/副屏/异尺寸）→ 绑定
          几何，截屏走 mss 区域抓取，坐标按客户区换算；
        - 恰好整屏覆盖（无边框全屏）或查不到窗口（独占全屏/已退出/查询
          异常）→ 回退 monitors[1] 整屏行为，模式切换时打一行 [INFO]
          （不每 tick 刷屏）。
        - 画布尺寸变化（窗口化↔全屏/改窗口大小）→ 按画布分辨率重读标定
          与模板（{W}x{H}.json 机制不变），自愈标定预算重置（换画布 = 全新
          标定问题，旧尝试次数不作数）。
        """
        geom, err = None, None
        try:
            geom = game_window_geometry()
        except Exception as e:  # noqa: BLE001 —— Win32 查询异常按未找到处理
            err = e
        mode = "screen"
        if geom:
            pw, ph = self._primary_size()
            if not is_fullscreen_cover(geom, pw, ph):
                mode = "window"
            else:
                geom = None   # 整屏覆盖：与独占全屏等价，走 monitors[1]
        self._window = geom
        if self.layout is not None:
            self.layout.bind_window(geom)
            canvas = self._screen_size()
            if canvas != self._canvas_loaded:
                self.set_layout(load_layout(*canvas))
                self._tpl_loaded = False   # 模板按画布分辨率存档，缓存作废
                self._tpl = None
                self._auto_calib_tries = 0
                self._auto_calib_done = False
                self._canvas_loaded = canvas
        if mode != self._mode_logged:
            self._mode_logged = mode
            if mode == "window":
                ox, oy, w, h = geom
                print(f"[INFO] 游戏窗口模式：客户区 {w}x{h} @({ox},{oy})，"
                      "截屏/坐标按窗口客户区换算")
            else:
                print("[INFO] 未找到独立游戏窗口（独占全屏或已退出），"
                      f"按主屏整屏识别{('：' + str(err)) if err else ''}")

    # ── 截屏 ──────────────────────────────────────────────────────

    def _grab(self, screen=None):
        """当前画布全帧（窗口模式 = 客户区区域抓取；整屏 = monitors[1]）
        → BGR ndarray。screen 可传独立 mss 实例（标定线程用，
        mss 非线程安全，检测线程实例加锁自用）。"""
        import mss
        import numpy
        region = capture_region(*self._window) if self._window else None
        if screen is not None:
            shot = screen.grab(region or screen.monitors[1])
        else:
            with self._grab_lock:
                if self._screen is None:
                    self._screen = mss.mss()
                shot = self._screen.grab(region or self._screen.monitors[1])
        img = numpy.asarray(shot)
        return img[:, :, :3][:, :, ::-1].copy()  # BGRA→BGR

    def _primary_size(self):
        """主屏物理分辨率（整屏回退的画布；全屏覆盖判定基准）"""
        with self._grab_lock:
            if self._screen is None:
                import mss
                self._screen = mss.mss()
            mon = self._screen.monitors[1]
        return mon["width"], mon["height"]

    def _screen_size(self):
        """当前画布尺寸：窗口模式 = 客户区尺寸，整屏模式 = 主屏分辨率。
        标定/模板文件按画布分辨率存档（机制不变，窗口化自动换档）。"""
        if self._window:
            return self._window[2], self._window[3]
        return self._primary_size()

    def _grab_bands(self, band_rects) -> list:
        """只截 3 个标题条小图（非活跃期省掉全屏 14MB 拷贝）→ BGR ndarray 列表。

        band_rects 是画布坐标；窗口模式加客户区原点 → 屏幕绝对坐标区域抓
        取（原点可为负——副屏在主屏左侧，不能往 0 截，截 0 会错位到主屏）。"""
        import mss
        import numpy
        ox, oy = (self._window[0], self._window[1]) if self._window else (0, 0)
        strips = []
        with self._grab_lock:
            if self._screen is None:
                self._screen = mss.mss()
            for band in band_rects:
                x, y, w, h = band
                shot = self._screen.grab({
                    "top": int(y + oy), "left": int(x + ox),
                    "width": max(1, int(w)), "height": max(1, int(h))})
                strips.append(numpy.asarray(shot)[:, :, :3][:, :, ::-1].copy())
        return strips

    # ── 模板预筛 ──────────────────────────────────────────────────

    def _template_path(self):
        if self.layout is None:
            return None
        try:
            w, h = self._screen_size()
        except Exception:
            return None
        return CALIB_DIR / f"{w}x{h}_tpl.npz"

    def _load_templates(self):
        if self._tpl_loaded:
            return
        self._tpl_loaded = True
        path = self._template_path()
        if not path or not path.exists():
            return
        try:
            import numpy
            with numpy.load(path) as data:   # 及时关句柄，不妨碍原子替换
                tpls = [data[f"t{i}"] for i in range(3)]
            if len(tpls) == 3 and all(t.size for t in tpls):
                self._tpl = tpls
                print(f"[DETECT] 模板预筛已加载: {path.name}")
        except Exception as e:
            print(f"[WARN] 模板加载失败（走盲扫）: {e}")

    def _save_templates(self, frame, regions):
        """界面激活帧的标题条灰度图存为模板（每次激活滚动刷新，保持不漂移）"""
        try:
            import cv2
            import numpy
            path = self._template_path()
            if not path:
                return
            tpls = []
            for band in regions["title_bands"]:
                x, y, w, h = band
                crop = frame[max(0, int(y)):int(y + h), max(0, int(x)):int(x + w)]
                if crop.size:
                    tpls.append(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
            if len(tpls) == 3:
                CALIB_DIR.mkdir(parents=True, exist_ok=True)
                with _SAVE_LOCK:
                    # tmp 名必须以 .npz 结尾——numpy.savez_compressed 会对
                    # 非 .npz 文件名自动补 .npz，导致 replace 找不到文件
                    tmp = path.with_name(path.stem + ".tmp.npz")
                    numpy.savez_compressed(tmp, t0=tpls[0], t1=tpls[1],
                                           t2=tpls[2])
                    tmp.replace(path)
                self._tpl = tpls
        except Exception as e:
            print(f"[WARN] 模板保存失败（不影响检测）: {e}")

    def _template_score(self, strips) -> float:
        """3 条标题条与模板的最大 matchTemplate 相似度（结构变化时返回 0）"""
        import cv2
        best = 0.0
        try:
            for strip, tpl in zip(strips, self._tpl):
                if strip.shape[0] < tpl.shape[0] or strip.shape[1] < tpl.shape[1]:
                    return 0.0
                gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
                res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
                best = max(best, float(res.max()))
        except Exception:
            return 0.0
        return best

    # ── 标题识别 ──────────────────────────────────────────────────

    def _ocr_title_strips(self, strips) -> tuple:
        """标题条小图列表 → (命中数, 命中名列表)"""
        hits, titles = 0, []
        for crop in strips:
            best = None
            for text, rect, score in self.ocr.recognize(crop):
                m = self.ocr.match_augment(text)
                if m and (best is None or m[1] > best[1]):
                    best = m
            if best:
                hits += 1
                titles.append(best[0])
        return hits, titles

    # ── 主循环 ────────────────────────────────────────────────────

    def run(self):
        self._running = True
        while self._running:
            if self._paused.wait(timeout=1.0):
                time.sleep(0.2)
                continue
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 —— 单帧失败不影响循环
                print(f"[WARN] 检测帧异常: {e}")
            # 自适应间隔：OCR 是大头，间隔只影响响应延迟——活跃期/
            # 模板命中期短轮询抢识别窗口（海克斯选择就几十秒）；纯待机 2s
            if self.core.state == "active":
                time.sleep(self.ACTIVE_POLL)
            elif self._last_tpl_score >= self.TPL_HIT:
                time.sleep(self.WAKE_POLL)
            else:
                time.sleep(self.POLL_INTERVAL)

    def _tick(self):
        if self.layout is None or not self.ocr.available:
            return
        self._tick_count += 1
        self._refresh_geometry()
        # 画布坐标（窗口模式相对客户区左上）——截屏帧裁剪用；屏幕坐标
        # （含原点偏移）由 to_pixels 默认输出给 app.py 的记账矩形
        regions = self.layout.to_pixels(*self._screen_size(), canvas=True)

        # ── 非活跃期：小图模板预筛，命中/盲扫帧才 OCR ──────────────
        if self.core.state != "active":
            self._load_templates()
            strips = self._grab_bands(regions["title_bands"])
            # 模板分每帧都算（matchTemplate ~ms 级）：命中=版式未漂移，
            # 既当 OCR 门也当短轮询/点击记账的证据
            tpl_score = (self._template_score(strips)
                         if self._tpl is not None else 0.0)
            self._last_tpl_score = tpl_score
            if tpl_score >= self.TPL_HIT:
                self._last_hex_evidence = time.time()
            do_ocr = (self._tick_count % self.BLIND_OCR_EVERY == 0
                      or tpl_score >= self.TPL_HIT)
            hits, _titles = self._ocr_title_strips(strips) if do_ocr else (0, [])
            if hits >= self.core.MIN_TITLE_HITS:
                self._last_hex_evidence = time.time()
            self.core.feed_titles(hits)
            self._fire_state_change()
            if self.core.state != "active":
                return
            # 刚激活：不等下一轮轮询，本 tick 直接落全帧卡识别（首读快 ~2s）

        # ── 活跃期：全帧一次，标题 + 卡片共用 ─────────────────────
        frame = self._grab()
        sh, sw = frame.shape[:2]

        title_strips = [self._crop(frame, band)
                        for band in regions["title_bands"]]
        hits, _titles = self._ocr_title_strips(title_strips)
        # 模板分（matchTemplate ~ms 级）：高分 = 版式未漂移
        self._last_tpl_score = (self._template_score(title_strips)
                                if self._tpl is not None else 0.0)
        if hits >= self.core.MIN_TITLE_HITS:
            self._last_hex_evidence = time.time()
        self.core.feed_titles(hits)
        self._fire_state_change()

        # 活跃期自愈标定（未标定/标定漂移；每活跃期最多 3 次尝试）——
        # 成功后区域可能更新，卡片 OCR 用刷新后的区域
        if hits >= self.core.MIN_TITLE_HITS and not self._auto_calib_done \
                and self._auto_calib_tries < self.AUTO_CALIB_TRIES \
                and self._last_tpl_score < self.TPL_HIT:
            self._auto_calib_tries += 1
            self._auto_calibrate_frame(frame)
            regions = self.layout.to_pixels(sw, sh, canvas=True)

        card_texts, any_text = self._ocr_cards(frame, regions["cards"],
                                               prev_texts=self.core.last_content)
        if not any_text:
            self._mismatch_run += 1
            if self._mismatch_run == self.MISMATCH_LIMIT and self.on_calibrate_hint:
                self.on_calibrate_hint()
        else:
            self._mismatch_run = 0
        self.core.feed_cards(card_texts)
        # 最近一帧读数（含 None 占位）：识别未及稳定时的点击记账兜底
        self.last_cards_raw = (list(self.core.last_content)
                               if self.core.last_content else None)
        stable = self.core.consume_cards()
        if stable and self.on_cards:
            # 误发射防护：≥2 张为有效库名才上报（防 tooltip 漂入邻卡矩形
            # 的伪命中触发错误建议）；台账序对齐仍保留 None 占位
            if self.count_valid_cards(stable) >= 2:
                self.on_cards(stable, self.core.round_id)
            else:
                print(f"[DETECT] 忽略可疑卡片内容（有效名 <2）: {stable}")

    def _crop(self, frame, rect):
        x, y, w, h = rect
        return frame[max(0, int(y)):int(y + h), max(0, int(x)):int(x + w)]

    def _fire_state_change(self):
        active = self.core.state == "active"
        if active != self.last_active:
            self.last_active = active
            if active:
                # 首次激活：截当前帧存模板（后续非活跃期走预筛）
                self._auto_calib_tries = 0
                self._auto_calib_done = False
                try:
                    frame = self._grab()
                    regions = self.layout.to_pixels(*self._screen_size(),
                                                    canvas=True)
                    self._save_templates(frame, regions)
                except Exception:
                    pass
            else:
                self._mismatch_run = 0  # 跨活跃期不累计失配
            if self.on_state:
                self.on_state(active, self.core.round_id)

    # 卡名行在卡片全框内的纵向位置带（真值：名行 0.398~0.427H / 卡框
    # 0.176~0.665H → 相对 0.45~0.51；带留边后取 0.30~0.62，用于排除卡面
    # 描述文字里提及的其他海克斯名干扰）
    NAME_ZONE = (0.30, 0.62)

    # ── 点击时刻记账支撑（活跃迟滞窗口内的点击也要能记账） ────────

    def recent_hex_activity(self, within: float = 10.0) -> bool:
        """最近 within 秒内有过海克斯证据（界面活跃/标题命中/模板高分）。

        点击记账的门：检测状态机有 2 帧出现/3 帧消失迟滞——界面刚出现
        或刚关闭的窗口期内点击若按 state 门控会被静默丢弃（2026-09-28
        真机复盘：台账空白的根因之一），改按证据时间窗判定。"""
        return (self.core.state == "active"
                or time.time() - self._last_hex_evidence < within)

    def _grab_card_crop(self, idx: int):
        """点击时刻定格第 idx 张卡画面 → 裁剪图 | None（毫秒级，不 OCR）。

        抓帧经 _grab()（内部自持 _grab_lock 原子完成）——调用方不得再持
        同一把锁进入：threading.Lock 不可重入，外层 with + _grab() 内层
        with 同线程二次获取 = 永久死锁（2026-09-30 真机审查发现，点击
        链一旦踩中 MouseLedger 线程即挂死、后续点击全部零痕迹）。定格
        语义不变：grab 在锁内一次完成，返回帧后界面变化不影响已取像素。"""
        if self.layout is None or not self.ocr.available:
            return None
        try:
            regions = self.layout.to_pixels(*self._screen_size(), canvas=True)
            cards = regions["cards"]
            if idx < 0 or idx >= len(cards):
                return None
            frame = self._grab()
            return self._crop(frame, cards[idx])
        except Exception as e:  # noqa: BLE001 —— 抓帧失败按"未抓到"处理
            print(f"[WARN] 点击抓帧异常: {e}")
        return None

    def capture_click_evidence(self, idx: int, tag: str = "click") -> tuple:
        """点击时刻定格第 idx 张卡 → (crop|None, 截图文件名|None)。

        抓帧（毫秒级定格）+ 经 capture_sink 落盘一气呵成——台账排障
        黑匣子的图像侧：每次卡片矩形命中的点击都留一张点击瞬间的卡面
        （OCR 读错/矩形错位/界面状态异常，事后看图即知）。落盘失败只
        警告不抛（截图是排障辅助，不能拖垮记账主链路）。"""
        crop = self._grab_card_crop(idx)
        filename = None
        if crop is not None and self.capture_sink is not None:
            try:
                filename = self.capture_sink(idx, crop, tag)
            except Exception as e:  # noqa: BLE001 —— 落盘失败不影响记账
                print(f"[WARN] 点击截图落盘异常: {e}")
        return crop, filename

    def _analyze_card_crop(self, crop) -> tuple:
        """卡面裁剪 → (库名|None, 原始OCR文本|None)（sync/async 补扫共用）。

        直接消费 recognize() 的原始文本，不走 _ocr_cards 的
        match_augments_in@72 预过滤——否则库外名/低分读数在进入本函数
        前就被置 None，留痕恒为 raw=None，事后无从追溯（2026-09-29 核验
        实锤）。命中时 raw=命中框的 OCR 原文（含「」·等装饰字符）；
        未命中时 raw=名字带内相似度最高一条的原文（知识库落后于游戏
        版本时，复盘日志里能看到卡面真名，人工补录词表的依据）。名字带
        过滤同 _ocr_cards（防卡面描述文字/tooltip 干扰），但未命中留痕
        也只取带内文本。"""
        ch = crop.shape[0] or 1
        hits = []                     # (name, score, in_zone, raw_text)
        best_raw, best_score = None, -1.0
        for t, rect, _conf in self.ocr.recognize(crop):
            if not t:
                continue
            in_zone = (self.NAME_ZONE[0] * ch
                       <= rect[1] + rect[3] / 2 <= self.NAME_ZONE[1] * ch)
            m = self.ocr.match_augment(t, threshold=80)
            if m:
                hits.append((m[0], m[1], in_zone, t))
                continue
            # 未命中：留相似度最高的带内原文（库外名线索）
            if not in_zone:
                continue
            near = self.ocr.match_augment(t, threshold=0)
            if near and near[1] > best_score:
                best_raw, best_score = t, near[1]
        if hits:
            # 名字带内优先，同带取最高分（同 _ocr_cards 口径）
            zone_hits = [h for h in hits if h[2]] or hits
            name = max(zone_hits, key=lambda h: h[1])
            return name[0], name[3]
        return None, best_raw

    def async_ocr_card_raw(self, idx: int, on_done, crop=None) -> None:
        """点击时刻抓帧定格 → 后台线程识别 → on_done(库名|None, raw|None)。

        用户案 2026-09-29：系统已知点击的卡位（坐标→card_at），点击只需
        截图，识别（最长 ~2s）挪后台 daemon 线程——记账线程不再被同步
        补扫阻塞，连续快速点两张卡不再漏第二张。抓帧失败/越界当场回调
        (None, None)；OCR 经 recognize 内部锁与检测线程串行，不并发读数。
        crop 可传外部已定格的帧（点击链先 capture_click_evidence 抓帧+
        落盘，补扫复用同一张，保证截图与识别消费的是同一像素）。"""
        if crop is None:
            crop = self._grab_card_crop(idx)
        if crop is None:
            on_done(None, None)
            return

        def _work():
            try:
                name, raw = self._analyze_card_crop(crop)
            except Exception as e:  # noqa: BLE001 —— 补扫失败不影响主流程
                print(f"[WARN] 点击补扫异常: {e}")
                name, raw = None, None
            on_done(name, raw)

        threading.Thread(target=_work, daemon=True,
                         name=f"click-ocr-{idx}").start()

    def sync_ocr_card_raw(self, idx: int, crop=None) -> tuple:
        """点击时刻同步补扫第 idx 张卡 → (库名|None, 原始OCR文本|None)。

        async_ocr_card_raw 的同步形态（同一分析体，阻塞至识别完成），
        供合成测试/离线断言直调；产品路径走 async 版。crop 语义同 async
        （None=内部抓帧）。"""
        if crop is None:
            crop = self._grab_card_crop(idx)
        if crop is None:
            return None, None
        try:
            return self._analyze_card_crop(crop)
        except Exception as e:  # noqa: BLE001 —— 补扫失败不影响主流程
            print(f"[WARN] 点击补扫异常: {e}")
        return None, None

    def sync_ocr_card(self, idx: int):
        """同步补扫薄壳（保持原返回值：库名|None）。

        识别未稳定（界面刚出现/流光抖动）时的记账兜底——OCR 未及 2 帧
        稳定就点击时，稳定读数/最近一帧读数都可能还没有有效卡名。检测
        线程可能正在 OCR，OcrEngine 内部有锁串行；同步形态阻塞至多
        ~2s（产品路径已改走 async_ocr_card_raw，本壳留给测试/直调）。
        匹配阈值提到 80（补扫只此一帧，误记账的代价比漏记高）。失败
        返回 None。"""
        name, _raw = self.sync_ocr_card_raw(idx)
        return name

    def count_valid_cards(self, cards: list) -> int:
        """三卡中有效库名的个数（on_cards 发射门：≥2 才上报）"""
        return sum(1 for n in cards if n and self.kb.valid_augment(n))

    def _ocr_cards(self, frame, card_rects, prev_texts=None) -> tuple:
        card_texts, any_text = [], False
        for i, rect in enumerate(card_rects):
            crop = self._crop(frame, rect)
            found = self.ocr.match_augments_in(self.ocr.recognize(crop))
            if not found:
                card_texts.append(None)
                continue
            any_text = True
            # 单卡迟滞：上帧读数在本帧命中里仍存在 → 保持（同一张卡的两个
            # 竞争命中随流光动画逐帧翻转，会让内容永不稳定）
            prev = prev_texts[i] if prev_texts and i < len(prev_texts) else None
            if prev and any(h[0] == prev for h in found):
                card_texts.append(prev)
                continue
            # 优先取名行纵向带内的命中（描述文字提及的库名得分通常更低，
            # 但悬浮 tooltip 漂入时可能反超——按位置带过滤最稳）
            ch = crop.shape[0] or 1
            in_zone = [h for h in found
                       if self.NAME_ZONE[0] * ch <= h[2][1] + h[2][3] / 2
                       <= self.NAME_ZONE[1] * ch]
            card_texts.append((in_zone or found)[0][0])
        return card_texts, any_text

    # ── 自动标定（主窗口"开始标定"触发；活跃期也会自愈式触发） ─────

    def auto_calibrate(self, delay: float = 0.0, attempts: int = 5,
                       interval: float = 2.0) -> bool:
        """延迟 delay 秒（给用户时间把海克斯界面切到前台）后，中央横带
        全量 OCR 找三组海克斯标题聚类 → 推导区域 → 几何校验 → 保存。

        需要海克斯界面正在屏幕上（翻牌/刷新动画中名行被遮挡会失败，
        多次尝试兜底）。成功返回 True（layout 已更新）。
        截屏用独立 mss 实例（与检测线程隔离，mss 非线程安全）。
        """
        import mss
        if delay > 0:
            print(f"[CALIB] {delay:.0f} 秒后开始识别——请立刻把海克斯三选一"
                  "界面切到前台并保持不动（等卡面翻好，勿点刷新）")
            time.sleep(delay)
        # 倒计时期间窗口可能移动/切窗口化：标定前重查几何（窗口模式也
        # 抓客户区，否则窗口化标定的是整屏 → 区域推导全部错位）
        self._refresh_geometry()
        for i in range(attempts):
            frame = self._grab(screen=mss.mss())
            if self._calibrate_from_frame(frame):
                return True
            if i < attempts - 1:
                time.sleep(interval)
        print("[CALIB] 自动标定失败：未找到三组海克斯标题"
              "（需要当前屏幕正是海克斯三选一界面且卡面稳定；"
              "也可等下次界面出现时保持不动 5 秒自动完成）")
        return False

    def _calibrate_from_frame(self, frame) -> bool:
        """单帧推导 + 几何校验 + 保存（失败返回 False，不落盘）"""
        sh, sw = frame.shape[:2]
        clusters = self._find_title_clusters(frame)
        if not clusters or len(clusters) != 3:
            return False
        if not self._clusters_plausible(clusters, sw, sh):
            print("[CALIB] 标题聚类几何异常（三卡应均布等距、同一水平线），拒绝保存")
            return False
        regions = self._regions_from_bands(clusters, sw, sh)
        if not self._regions_valid(regions):
            print("[CALIB] 推导区域越界（cards/rerolls 超出屏幕），拒绝保存")
            return False
        save_layout(sw, sh, regions, confidence=0.9)
        self.set_layout(Layout(regions, source="auto_calib"))
        # canvas=True：模板/裁剪基于截屏帧（画布坐标），不带原点偏移
        self._save_templates(frame, self.layout.to_pixels(sw, sh, canvas=True))
        print("[CALIB] 几何校验通过，标定已保存")
        return True

    def _auto_calibrate_frame(self, frame):
        """活跃期自愈标定（免手动，_tick 调用，每活跃期 ≤AUTO_CALIB_TRIES 次）：
        - 未标定（source=default，如首次使用/换分辨率）→ 直接按本帧落标定；
        - 已标定 → 名行聚类中心与现标定均偏 >AUTO_CALIB_DRIFT 像素
          （UI 漂移/坏标定滞留）才重建，防好标定被反复重写。"""
        sh, sw = frame.shape[:2]
        clusters = self._find_title_clusters(frame)
        if not clusters or len(clusters) != 3 \
                or not self._clusters_plausible(clusters, sw, sh):
            return
        if self.layout.source != "default":
            cur = sorted(sw / 2 + (b[0] + b[2] / 2) * sh
                         for b in self.layout.title_bands)
            got = sorted(sum(h[1] for h in cl) / len(cl) for cl in clusters)
            drift = sum(abs(a - b) for a, b in zip(got, cur)) / 3
            if drift <= self.AUTO_CALIB_DRIFT:
                # 标定没漂移：本活跃期自愈标定视为已完成——否则
                # _auto_calib_done 恒 False，活跃期每个 tick 都重跑
                # 一次全宽带聚类 OCR（~2-6s，最多 3 次），识别稳定
                # 被白拖十几秒（2026-09-28 真机复盘：界面出现→卡片
                # 稳定 ~15s 的主因）
                self._auto_calib_done = True
                return
            print(f"[CALIB] 检测到标题条漂移 ~{drift:.0f}px，自动重标定")
        self._auto_calib_done = self._calibrate_from_frame(frame)

    def _find_title_clusters(self, frame):
        """中央横带 OCR → 海克斯命中文本按 x 聚类为 ≤3 组，返回屏幕像素 bbox"""
        sh, sw = frame.shape[:2]
        y0, y1 = int(sh * 0.30), int(sh * 0.62)
        band = frame[y0:y1, :]
        items = self.ocr.recognize(band)
        hits = []
        for text, rect, score in items:
            m = self.ocr.match_augment(text)
            if m and m[1] >= 78:
                hits.append((m[0], rect[0] + rect[2] / 2, y0 + rect[1], rect[2],
                             rect[3], m[1]))
        hits.sort(key=lambda h: h[1])
        # 按 x 间距聚类（间距 > 带宽 18% 分新组）
        clusters = []
        for h in hits:
            if clusters and h[1] - clusters[-1][-1][1] < sw * 0.18:
                clusters[-1].append(h)
            else:
                clusters.append([h])
        clusters = [c for c in clusters if len(c) >= 1][:3]
        return clusters if len(clusters) == 3 else None

    @staticmethod
    def _clusters_plausible(clusters, sw, sh) -> bool:
        """保存前几何校验：三簇均布等距、同一水平线、左右对称于屏幕中心。
        （历史教训：标定保存于战斗画面时聚类出垃圾也照存 confidence=0.9，
        坏标定长期滞留 → 必须校验后才落盘。）"""
        cxs = [sum(h[1] for h in cl) / len(cl) for cl in clusters]
        tops = [min(h[2] for h in cl) for cl in clusters]
        gaps = [cxs[1] - cxs[0], cxs[2] - cxs[1]]
        # 真值：卡距 ≈0.19 屏宽（491px @2560），容差放得下非 16:9 居中布局
        if not all(0.14 * sw < g < 0.24 * sw for g in gaps):
            return False
        if abs(gaps[0] - gaps[1]) > 0.03 * sw:
            return False
        if max(tops) - min(tops) > 0.03 * sh:
            return False
        if abs((cxs[0] + cxs[2]) / 2 - sw / 2) > 0.08 * sw:
            return False
        return True

    @staticmethod
    def _regions_valid(regions: dict) -> bool:
        """推导区域整体合法性：全部矩形在屏内、卡片有序且互不交叠"""
        rects = regions["title_bands"] + regions["cards"] + regions["rerolls"]
        for xu, y, wu, h in rects:
            if not (0 <= y < 1 and 0 < h and y + h <= 1.0):
                return False
            if abs(xu) > ASPECT_169:   # x 偏移出屏（H 单位 |xu| ≤ 16/9）
                return False
        cx = [r[0] + r[2] / 2 for r in regions["cards"]]
        if not cx[0] < cx[1] < cx[2]:
            return False
        # 不交叠：按 x 排序后相邻卡左缘不得越过前一卡右缘
        # （容差 0.01H≈14px@1440 吸收换算舍入；真值卡间隙 ≈0.046H）
        cs = sorted(regions["cards"], key=lambda r: r[0])
        return all(a[0] + a[2] - b[0] <= 0.01 for a, b in zip(cs, cs[1:]))

    def _regions_from_bands(self, clusters, sw, sh):
        """由标题聚类推导区域，直接输出 H 单位（units:"h"）——
        按真实分辨率换算，任意宽高比回读零漂移。

        几何锚点取自 2560x1440 真机实测（10 关键帧一致）：
        卡框顶 ≈ 名行顶 - 0.222H、卡框高 0.489H、卡宽 0.2951H（=0.166 屏宽）；
        刷新按钮在名行下方 0.279H 处、每卡中心各一个（共 3 个）。
        旧公式 cards 从名行下方开始（永远框不到卡名）、
        reroll_y=min+max-0.02 恒 >1 落屏幕外——均已废弃。
        """
        bands, cards, rerolls = [], [], []
        for cl in clusters:
            cx = sum(h[1] for h in cl) / len(cl)
            top = min(h[2] for h in cl)
            name_h = max(h[4] for h in cl)
            # 卡片全框（OCR 区 + 点击热区）
            cw = sh * 0.2951
            cy = top - sh * 0.222
            ch = sh * 0.489
            cards.append([(cx - cw / 2 - sw / 2) / sh, cy / sh, cw / sh, ch / sh])
            # 标题条：与卡同宽，名行上下各留少量余量
            by = top - sh * 0.007
            bh = max(name_h * 1.5, sh * 0.040)
            bands.append([(cx - cw / 2 - sw / 2) / sh, by / sh, cw / sh, bh / sh])
            # 刷新按钮 ×3：中心对齐卡片中心
            rw = sh * 0.072
            ry = top + sh * 0.279
            rh = sh * 0.056
            rerolls.append([(cx - rw / 2 - sw / 2) / sh, ry / sh, rw / sh, rh / sh])
        return {"units": "h",
                "title_bands": bands, "cards": cards, "rerolls": rerolls}
