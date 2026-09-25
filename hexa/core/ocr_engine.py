"""OCR 引擎（计划 §7 core/ocr_engine.py）：RapidOCR 封装 + 海克斯库模糊匹配

主用途：海克斯界面标题条/卡片区域识别 → 文本 → 229 海克斯库模糊匹配。
自检即验收（计划 §3）：OCR 结果必须能和 229 海克斯库模糊匹配，
连续失配 = UI 漂移/改版 → 上层提示重标定而非默默推错。
RapidOCR 懒加载（首次识别 ~2s 模型初始化，之后单帧几十 ms）。
"""
import threading
import time

from thefuzz import process as fuzz_process


class OcrEngine:
    def __init__(self, kb):
        self.kb = kb
        self._ocr = None
        self._lock = threading.Lock()
        self.available = True   # import 失败时置 False（上层走模板兜底/提示）

    def _get_ocr(self):
        if self._ocr is None:
            with self._lock:
                if self._ocr is None:
                    from rapidocr_onnxruntime import RapidOCR
                    self._ocr = RapidOCR()
        return self._ocr

    # ── 识别 ──────────────────────────────────────────────────────

    def recognize(self, image) -> list:
        """BGR ndarray → [(text, [x,y,w,h]框, score)]；不可用/失败返回 []"""
        if not self.available or image is None:
            return []
        try:
            ocr = self._get_ocr()
        except Exception as e:  # noqa: BLE001 —— OCR 环境损坏不致命
            print(f"[WARN] RapidOCR 初始化失败，画面检测降级模板兜底: {e}")
            self.available = False
            return []
        try:
            result, _ = ocr(image)
        except Exception as e:
            print(f"[WARN] OCR 识别异常: {e}")
            return []
        out = []
        for box, text, score in (result or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            rect = [int(min(xs)), int(min(ys)),
                    int(max(xs) - min(xs)), int(max(ys) - min(ys))]
            out.append((str(text).strip(), rect, float(score)))
        return out

    # ── 海克斯名匹配（OCR 噪声 → 229 库） ─────────────────────────

    def match_augment(self, text: str, threshold: int = 72):
        """单条文本 → (海克斯名, 相似度) | None

        预清洗：去空白/标点/常见装饰字符（·、「」等）后再匹配。
        """
        if not text:
            return None
        cleaned = "".join(
            ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
        if len(cleaned) < 2:
            return None
        name, score = fuzz_process.extractOne(
            cleaned, list(self.kb.augment_names)) or (None, 0)
        if name and score >= threshold:
            return name, int(score)
        return None

    def match_augments_in(self, ocr_items: list, threshold: int = 72) -> list:
        """整帧 OCR 结果 → 命中的海克斯 [(name, score, rect)]（同名去重取最高分）"""
        hits = {}
        for text, rect, score in ocr_items:
            m = self.match_augment(text, threshold)
            if not m:
                continue
            name, mscore = m
            if name not in hits or mscore > hits[name][1]:
                hits[name] = (name, mscore, rect)
        return sorted(hits.values(), key=lambda h: -h[1])
