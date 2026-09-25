"""海克斯界面检测（计划 §3 / §7 core/screen_detect.py）

架构：DetectorCore（纯逻辑状态机，可离线测试）+ ScreenDetector（截屏线程）。

主信号：标题条小区域 OCR + 229 海克斯库模糊匹配（每 2s）——
≥2 个标题命中且连续 2 帧 = 界面出现；连续 3 帧无命中 = 消失（迟滞防误触）。
界面活跃期间持续比对卡片内容：内容变化（reroll/新轮）即重启识别，
稳定 2 帧才回调 on_cards 计入推荐/台账。
OCR 连续失配 = UI 漂移/改版 → on_calibrate_hint 提示重标定，不默默推错。
模板匹配（cv2.matchTemplate）退为预热加速与 OCR 异常兜底。

区域坐标（Layout）：默认层 = 16:9 百分比常量（占位，真机标定校准）；
加载时把横向偏移换算为「高度基准 + 屏幕中心锚定」单位（H 相对量），
16:10/21:9/32:9 兜得住；标定结果按分辨率存 calibrations/{W}x{H}.json
（gitignore 排除，用户屏幕专属）；自动标定 = 中央横带全量 OCR 找
三组海克斯标题聚类 + reroll 特征文本。
"""
import json
import threading
import time
from pathlib import Path

from hexa.config import BASE_DIR

# 标定文件放应用根下可写目录（PyInstaller 冻结后包目录只读；
# gitignore 排除 *.json，用户屏幕专属不入库）
CALIB_DIR = BASE_DIR / "calibrations"

# ── 默认布局（16:9 百分比占位；auto_calibrate 一次后即按真机生效） ──
# title_bands: 三个卡片标题条 (x, y, w, h)；cards: 三张卡片矩形（点击热区，
# 严禁交叠）；reroll: 刷新按钮矩形。均为屏幕比例。
DEFAULT_LAYOUT_169 = {
    "title_bands": [[0.245, 0.418, 0.170, 0.055],
                    [0.415, 0.418, 0.170, 0.055],
                    [0.585, 0.418, 0.170, 0.055]],
    "cards":       [[0.225, 0.455, 0.170, 0.420],
                    [0.415, 0.455, 0.170, 0.420],
                    [0.605, 0.455, 0.170, 0.420]],
    "reroll":      [0.445, 0.900, 0.110, 0.055],
}
ASPECT_169 = 16 / 9


class Layout:
    """区域布局：内部以「高度相对量 + 中心锚定」存储，适配任意宽高比。

    存储单元：x 用相对屏幕中心的 H 偏移（xu = (fx-0.5)*16/9），
    y/w/h 用 H 相对量（w 预乘 16/9）。to_pixels 时：
      px_x = W/2 + xu*H ; px_y = y*H ; px_w = wu*H ; px_h = h*H
    """

    def __init__(self, data: dict = None, source: str = "default"):
        d = data or DEFAULT_LAYOUT_169
        self.source = source
        if d.get("units") == "h":
            # 自动标定按真实分辨率直接换算的 H 单位（任意宽高比精确）
            self.title_bands = [list(r) for r in d["title_bands"]]
            self.cards = [list(r) for r in d["cards"]]
            self.reroll = list(d["reroll"])
        else:
            self.title_bands = [self._to_h_units(r) for r in d["title_bands"]]
            self.cards = [self._to_h_units(r) for r in d["cards"]]
            self.reroll = self._to_h_units(d["reroll"])

    @staticmethod
    def _to_h_units(r):
        x, y, w, h = r
        return [(x - 0.5) * ASPECT_169, y, w * ASPECT_169, h]

    @staticmethod
    def _from_h_units(r):
        xu, y, wu, h = r
        return [xu / ASPECT_169 + 0.5, y, wu / ASPECT_169, h]

    def to_pixels(self, screen_w: int, screen_h: int):
        def conv(r):
            xu, y, wu, h = r
            x = int(screen_w / 2 + xu * screen_h)
            yy = int(y * screen_h)
            return [x, yy, int(wu * screen_h), int(h * screen_h)]
        return {"title_bands": [conv(r) for r in self.title_bands],
                "cards": [conv(r) for r in self.cards],
                "reroll": conv(self.reroll)}

    def as_169(self) -> dict:
        return {"title_bands": [self._from_h_units(r) for r in self.title_bands],
                "cards": [self._from_h_units(r) for r in self.cards],
                "reroll": self._from_h_units(self.reroll)}


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
    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    path = CALIB_DIR / f"{screen_w}x{screen_h}.json"
    doc = {"screen": [screen_w, screen_h], "confidence": confidence,
           "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "regions": regions}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[CALIB] 已保存标定 {path.name}（置信度 {confidence:.2f}）")


def point_in_rect(x: int, y: int, rect) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


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
        """active 状态下每帧喂卡片识别结果（list[str|None]，长度 3）"""
        if self.state != "active":
            return
        content = tuple(card_texts)
        if content != self._last_content:
            self._last_content = content
            self._content_run = 1
            self.stable_cards = None
            if any(card_texts):
                print(f"[DETECT] 卡片内容变化: {card_texts}")
        else:
            self._content_run += 1
            # 发射后闩锁：稳定 2 帧且该内容未发射过才置位（防每帧重发烧预算）
            if (self._content_run >= self.STABLE_FRAMES and any(card_texts)
                    and self.stable_cards is None
                    and self._emitted_content != content):
                self.stable_cards = list(card_texts)
                self.stable_round = self.round_id
                self._emitted_content = content
                print(f"[DETECT] 卡片内容稳定: {card_texts}")

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

    POLL_INTERVAL = 2.0
    MISMATCH_LIMIT = 5     # 连续 N 轮"标题命中但卡片全失配" → 提示重标定
    BLIND_OCR_EVERY = 3    # 非活跃期每 N 帧强制 OCR 一次（防模板漏检）
    TPL_HIT = 0.60         # matchTemplate 命中阈值（卡框美术恒定，文字占比小）

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
        self._tick_count = 0
        self._tpl = None                  # [灰度标题条 ndarray ×3] 模板预筛
        self._tpl_loaded = False          # 懒加载一次

    # ── 控制 ──────────────────────────────────────────────────────

    def set_layout(self, layout: Layout):
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

    # ── 截屏 ──────────────────────────────────────────────────────

    def _grab(self, screen=None):
        """主屏全帧 → BGR ndarray。screen 可传独立 mss 实例（标定线程用，
        mss 非线程安全，检测线程实例加锁自用）。"""
        import mss
        import numpy
        if screen is not None:
            shot = screen.grab(screen.monitors[1])
        else:
            with self._grab_lock:
                if self._screen is None:
                    self._screen = mss.mss()
                shot = self._screen.grab(self._screen.monitors[1])
        img = numpy.asarray(shot)
        return img[:, :, :3][:, :, ::-1].copy()  # BGRA→BGR

    def _screen_size(self):
        with self._grab_lock:
            if self._screen is None:
                import mss
                self._screen = mss.mss()
            mon = self._screen.monitors[1]
        return mon["width"], mon["height"]

    def _grab_bands(self, band_rects) -> list:
        """只截 3 个标题条小图（非活跃期省掉全屏 14MB 拷贝）→ BGR ndarray 列表"""
        import mss
        import numpy
        strips = []
        with self._grab_lock:
            if self._screen is None:
                self._screen = mss.mss()
            for band in band_rects:
                x, y, w, h = band
                shot = self._screen.grab({
                    "top": max(0, int(y)), "left": max(0, int(x)),
                    "width": max(1, int(w)), "height": max(1, int(h)),
                    "mon": 1})
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
            data = numpy.load(path)
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
                numpy.savez_compressed(path, t0=tpls[0], t1=tpls[1], t2=tpls[2])
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
            time.sleep(self.POLL_INTERVAL)

    def _tick(self):
        if self.layout is None or not self.ocr.available:
            return
        self._tick_count += 1
        regions = self.layout.to_pixels(*self._screen_size())

        # ── 非活跃期：小图模板预筛，命中/盲扫帧才 OCR ──────────────
        if self.core.state != "active":
            self._load_templates()
            strips = self._grab_bands(regions["title_bands"])
            do_ocr = self._tick_count % self.BLIND_OCR_EVERY == 0
            if not do_ocr and self._tpl is not None:
                do_ocr = self._template_score(strips) >= self.TPL_HIT
            hits, _titles = self._ocr_title_strips(strips) if do_ocr else (0, [])
            self.core.feed_titles(hits)
            self._fire_state_change()
            return

        # ── 活跃期：全帧一次，标题 + 卡片共用 ─────────────────────
        frame = self._grab()
        sh, sw = frame.shape[:2]

        hits, _titles = self._ocr_title_strips(
            [self._crop(frame, band) for band in regions["title_bands"]])
        self.core.feed_titles(hits)
        self._fire_state_change()

        card_texts, any_text = self._ocr_cards(frame, regions["cards"])
        if not any_text:
            self._mismatch_run += 1
            if self._mismatch_run == self.MISMATCH_LIMIT and self.on_calibrate_hint:
                self.on_calibrate_hint()
        else:
            self._mismatch_run = 0
        self.core.feed_cards(card_texts)
        stable = self.core.consume_cards()
        if stable and self.on_cards:
            self.on_cards(stable, self.core.round_id)

    def _crop(self, frame, rect):
        x, y, w, h = rect
        return frame[max(0, int(y)):int(y + h), max(0, int(x)):int(x + w)]

    def _fire_state_change(self):
        active = self.core.state == "active"
        if active != self.last_active:
            self.last_active = active
            if active:
                # 首次激活：截当前帧存模板（后续非活跃期走预筛）
                try:
                    frame = self._grab()
                    regions = self.layout.to_pixels(*self._screen_size())
                    self._save_templates(frame, regions)
                except Exception:
                    pass
            else:
                self._mismatch_run = 0  # 跨活跃期不累计失配
            if self.on_state:
                self.on_state(active, self.core.round_id)

    def _ocr_cards(self, frame, card_rects) -> tuple:
        card_texts, any_text = [], False
        for rect in card_rects:
            found = self.ocr.match_augments_in(
                self.ocr.recognize(self._crop(frame, rect)))
            if found:
                any_text = True
                card_texts.append(found[0][0])
            else:
                card_texts.append(None)
        return card_texts, any_text

    # ── 自动标定（主窗口"开始标定"触发，阻塞 ~2s） ────────────────

    def auto_calibrate(self, attempts: int = 3) -> bool:
        """中央横带全量 OCR 找三组海克斯标题聚类 → 推导区域 → 保存。

        需要海克斯界面正在屏幕上。成功返回 True（layout 已更新）。
        截屏用独立 mss 实例（与检测线程隔离，mss 非线程安全）。
        """
        import mss
        for i in range(attempts):
            frame = self._grab(screen=mss.mss())
            sh, sw = frame.shape[:2]
            bands = self._find_title_clusters(frame)
            if bands and len(bands) == 3:
                regions = self._regions_from_bands(bands, sw, sh)
                save_layout(sw, sh, regions, confidence=0.9)
                self.set_layout(Layout(regions, source="auto_calib"))
                self._save_templates(frame, self.layout.to_pixels(sw, sh))
                return True
            if i < attempts - 1:
                time.sleep(1.5)
        print("[CALIB] 自动标定失败：未找到三组海克斯标题（确认界面在屏后重试/手动标定）")
        return False

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

    def _regions_from_bands(self, clusters, sw, sh):
        """由标题聚类推导区域，直接输出 H 单位（units:"h"）——
        按真实分辨率换算，任意宽高比回读零漂移。"""
        bands, cards = [], []
        for cl in clusters:
            cx_px = sum(h[1] for h in cl) / len(cl)
            w_px = max(h[3] for h in cl) * 2.2
            h_px = max(h[4] for h in cl) * 1.9
            y_px = min(h[2] for h in cl)
            bands.append([(cx_px - sw / 2) / sh, y_px / sh, w_px / sh, h_px / sh])
            cw_px = w_px * 1.25
            cards.append([(cx_px - cw_px / 2 - sw / 2) / sh,
                          (y_px + h_px * 0.9) / sh, cw_px / sh, 0.40])
        reroll_y = (min(b[1] + b[3] for b in bands)
                    + max(c[1] + c[3] for c in cards) - 0.02)
        return {"units": "h",
                "title_bands": bands, "cards": cards,
                "reroll": [(0 - sw * 0.06) / sh, reroll_y,
                           sw * 0.12 / sh, 0.05]}
