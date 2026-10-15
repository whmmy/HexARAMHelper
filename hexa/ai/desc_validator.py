"""卡面描述通顺校验器（LLM 判定，可插拔——20261015 描述共识案）

管线位置：OCR 健康门（screen_detect.desc_healthy，字符级）→ 本校验
（通顺性判断——判断归模型）→ 共识入库（kb.record_augment_desc）。

只判"是不是通顺的自然语句/短语、无乱码错字"；判"描述内容对不对、
和海克斯匹不匹配"是语义裁判，越界不做（也不把海克斯名喂给校验器，
防它滑向语义比对）。只 pass/fail 不改写，进库文本仍是 OCR 直出原文
（数据直出不破）。

设计边界（20261015 用户拍板）：
- 结论缓存（文本 → verdict）：同一文本重复读零调用；
- 每局调用上限 CALLS_PER_GAME：病态 OCR 抖动时账单封顶，超限放行
  （退化为纯共识模式）；
- 任何异常/未配 key/输出不可解析 → 返回 None=放行（校验不可用≠
  校验不合格，永远有兜底）；只有明确判坏（False）才拒收；
- 槽位 settings["desc_check"]：换小模型改 model 一行；彻底离线换
  本地实现接口不变（Jev 同款可插拔先例）。未配置即整体禁用。
"""
import threading

from hexa.ai.client import LLMClient, LLMError, parse_json

CALLS_PER_GAME = 10   # 每局校验调用上限（verdict 缓存外的净新文本）

SYSTEM_PROMPT = (
    "你是 OCR 文本质检器。判断给定文本是否为通顺的自然中文语句或短语"
    "（游戏卡面描述），无明显乱码、错别字堆砌或不可读噪声。"
    "文本结尾截断不算失败。只输出 JSON：{\"ok\": true} 或 {\"ok\": false}")


class DescValidator:
    """LLM 通顺判定：check() → True 放行 / False 拒收 / None 放行（兜底）"""

    def __init__(self, settings):
        self._settings = settings
        self._client = None       # 懒建（首次 check 时）；构造不抛
        self._disabled = False    # 未配 key 永久禁用（重启进程生效）
        self._verdicts = {}       # 文本 → bool（结论缓存，进程内有效）
        self._calls = 0           # 本局净新文本调用数（new_game 归零）
        self._lock = threading.Lock()

    def new_game(self):
        """每局重置调用计数（app 层 on_game_start 喂）"""
        with self._lock:
            self._calls = 0

    def check(self, text) -> object:
        """→ True=通顺放行 / False=判坏拒收 / None=校验不可用放行。

        判坏结论也进缓存：同一条垃圾文本反复出现不再花调用。"""
        text = (text or "").strip()
        if not text:
            return None
        with self._lock:
            if self._disabled:
                return None
            if text in self._verdicts:
                return self._verdicts[text]
            if self._calls >= CALLS_PER_GAME:
                return None
        try:
            if self._client is None:
                self._client = LLMClient(self._settings, "desc_check")
            if not self._client.configured:
                with self._lock:
                    self._disabled = True
                print("[INFO] desc_check 槽位未配置模型/Key，描述校验禁用"
                      "（纯共识模式）")
                return None
            out, _meta = self._client.chat(
                SYSTEM_PROMPT, text[:200],
                max_tokens=20, json_mode=True, purpose="desc_check")
            verdict = self._parse(out)
        except LLMError as e:
            print(f"[WARN] 描述通顺校验不可用（放行，纯共识模式）: {e}")
            return None
        except Exception as e:  # noqa: BLE001 —— 校验器绝不外抛
            print(f"[WARN] 描述通顺校验异常（放行）: {type(e).__name__}: {e}")
            return None
        with self._lock:
            self._calls += 1
            self._verdicts[text] = verdict
        return verdict

    @staticmethod
    def _parse(out) -> object:
        """模型输出 → True/False；输出不可解析 → None（放行，不误杀）"""
        try:
            return bool(parse_json(out).get("ok"))
        except LLMError:
            return None
