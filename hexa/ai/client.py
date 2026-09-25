"""双槽位 LLM 客户端（计划 §4 模型双槽位）

- System 2：ChampionContext 生成（每局 1 次，智能档）
- System 1：快照建议器（每局 8~11 次，性价比档）
OpenAI 兼容协议（openai SDK），任意平台可换（百炼/deepseek/kimi/GLM…）。
qwen3 系必须 enable_thinking=False（P0.5 实测：长输入非流式默认深度思考会超时）。
"""
import json
import threading
import time


class LLMError(Exception):
    pass


class UsageStats:
    """线程安全的用量/费用累计（本局 + 累计，单价可配 每百万 token）"""

    def __init__(self):
        self._lock = threading.Lock()
        self.game_calls = 0
        self.game_tokens_in = 0
        self.game_tokens_out = 0
        self.total_calls = 0
        self.total_tokens_in = 0
        self.total_tokens_out = 0

    def add(self, tokens_in: int, tokens_out: int):
        with self._lock:
            self.game_calls += 1
            self.game_tokens_in += tokens_in
            self.game_tokens_out += tokens_out
            self.total_calls += 1
            self.total_tokens_in += tokens_in
            self.total_tokens_out += tokens_out

    def new_game(self):
        with self._lock:
            self.game_calls = 0
            self.game_tokens_in = 0
            self.game_tokens_out = 0

    def cost(self, price_in: float, price_out: float, cumulative: bool = False) -> float:
        """费用（元）。price_* = 每百万 token 单价"""
        with self._lock:
            if cumulative:
                tin, tout = self.total_tokens_in, self.total_tokens_out
            else:
                tin, tout = self.game_tokens_in, self.game_tokens_out
        return tin / 1e6 * price_in + tout / 1e6 * price_out


class LLMClient:
    """单槽位客户端：超时/重试/降级模型/用量统计

    settings 槽位字段：model / base_url / api_key / api_key_env /
    temperature / max_tokens / timeout / fallback_model / price_in / price_out
    """

    def __init__(self, settings, slot: str, usage: UsageStats = None):
        self.settings = settings
        self.slot = slot
        self.usage = usage or UsageStats()
        self.last_error = ""
        self._client = None
        self._client_key = None  # (base_url, api_key) 变化时重建

    # ── 配置读取 ──────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self.settings.get(self.slot, "model", default="")

    @property
    def configured(self) -> bool:
        """模型名 + key 均就绪才视为已配置"""
        return bool(self.model) and bool(self._resolve_key())

    def _resolve_key(self) -> str:
        return self.settings.resolve_api_key(self.slot)

    def _sdk_client(self):
        import openai
        base_url = self.settings.get(
            self.slot, "base_url",
            default="https://dashscope.aliyuncs.com/compatible-mode/v1")
        key = self._resolve_key()
        if not key:
            raise LLMError(f"{self.slot} 未配置 API Key")
        if self._client is None or self._client_key != (base_url, key):
            self._client = openai.OpenAI(base_url=base_url, api_key=key,
                                         timeout=self._timeout())
            self._client_key = (base_url, key)
        return self._client

    def _timeout(self) -> float:
        return float(self.settings.get(self.slot, "timeout", default=60))

    # ── 调用 ──────────────────────────────────────────────────────

    def chat(self, system: str, user: str, max_tokens: int = None,
             json_mode: bool = True) -> tuple[str, dict]:
        """返回 (文本, meta)。失败重试 1 次（超时则降级 fallback_model 再试）；
        全部失败 raise LLMError。meta = {model, latency, tokens_in, tokens_out}"""
        max_tokens = max_tokens or int(
            self.settings.get(self.slot, "max_tokens", default=800))
        temperature = float(
            self.settings.get(self.slot, "temperature", default=0.3))
        attempts = [self.model]
        fb = self.settings.get(self.slot, "fallback_model", default="")
        if fb and fb != self.model:
            attempts.append(fb)
        last_err = None
        for i, model in enumerate(attempts):
            try:
                return self._call_once(model, system, user, max_tokens,
                                       temperature, json_mode)
            except Exception as e:  # noqa: BLE001 —— 任何 SDK/网络异常都走降级链
                last_err = e
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                # 超时类错误才值得换降级模型；其余首试失败直接二试同模型意义不大，
                # 但保留统一重试语义（间隔 0.5s）。
                time.sleep(0.5)
        raise LLMError(f"{self.slot} 调用失败（{len(attempts)} 次）: {last_err}")

    def _call_once(self, model, system, user, max_tokens, temperature,
                   json_mode) -> tuple[str, dict]:
        client = self._sdk_client()
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": self._timeout(),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # qwen3 系（qwen3.x）默认深度思考，长输入非流式必超时（P0.5 实测）
        if model.lower().startswith("qwen3"):
            kwargs["extra_body"] = {"enable_thinking": False}
        t0 = time.time()
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "prompt_tokens", 0) or 0
        tout = getattr(usage, "completion_tokens", 0) or 0
        self.usage.add(tin, tout)
        meta = {"model": model, "latency": round(time.time() - t0, 2),
                "tokens_in": tin, "tokens_out": tout}
        return content, meta

    # ── 工具 ──────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """设置界面"测试连接"：极小请求测延迟。返回 {ok, latency, model|error}"""
        if not self.model:
            return {"ok": False, "error": "未配置模型名"}
        if not self._resolve_key():
            return {"ok": False, "error": "未配置 API Key"}
        try:
            _, meta = self._call_once(
                self.model, "你是连通性探测器。", "回复 JSON: {\"ok\": true}",
                max_tokens=16, temperature=0.0, json_mode=True)
            return {"ok": True, "latency": meta["latency"], "model": meta["model"]}
        except Exception as e:  # noqa: BLE001 —— 测试连接需把失败原因带给 UI
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160]}


def parse_json(text: str) -> dict:
    """模型输出 → dict（剥代码围栏/前后杂讯；解析失败 raise LLMError）"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"输出不含 JSON 对象: {text[:80]!r}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败: {e}") from e
