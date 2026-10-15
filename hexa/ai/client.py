"""双槽位 LLM 客户端（计划 §4 模型双槽位）

- System 2：ChampionContext 生成（每局 1 次，智能档）
- System 1：快照建议器（每局 8~11 次，性价比档）
OpenAI 兼容协议（openai SDK），任意平台可换（百炼/deepseek/kimi/GLM…）。
思考强度 per-slot 可配：off/low/medium/high/auto（auto=跟随服务商默认；
qwen3 系例外——auto 也强制关，非流式长输入默认深度思考必超时，P0.5 实测）。
"""
import json
import threading
import time

from hexa.ai import usage_ledger as _ul

# 档位 → 思考 token 预算（dashscope thinking_budget）
THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 16384}


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
        return self.settings.get_float(self.slot, "timeout", default=60)

    # ── 调用 ──────────────────────────────────────────────────────

    def chat(self, system: str, user: str, max_tokens: int = None,
             json_mode: bool = True, purpose: str = "") -> tuple[str, dict]:
        """返回 (文本, meta)。失败重试 1 次（超时则降级 fallback_model 再试）；
        全部失败 raise LLMError。meta = {model, latency, tokens_in, tokens_out}

        purpose=业务环节标签（ctx/opening/next/swap/ranks/desc_check…），
        缺省回退槽位名；每次尝试（含降级重试）各落一行 AI 台账（20261015
        各环节账单案），复盘按 ts 对齐复盘日志。"""
        max_tokens = max_tokens or int(
            self.settings.get_float(self.slot, "max_tokens", default=800))
        temperature = self.settings.get_float(self.slot, "temperature", default=0.3)
        model = self.model
        fb = self.settings.get(self.slot, "fallback_model", default="")
        attempts = [model, fb] if (fb and fb != model) else [model, model]
        last_err = None
        for i, model in enumerate(attempts):
            t0 = time.time()
            try:
                out, meta = self._call_once(model, system, user, max_tokens,
                                            temperature, json_mode)
                _ul.get().record(purpose or self.slot, self.slot, ok=True,
                                 model=meta.get("model", model),
                                 latency=meta.get("latency"),
                                 tokens_in=meta.get("tokens_in", 0),
                                 tokens_out=meta.get("tokens_out", 0))
                return out, meta
            except Exception as e:  # noqa: BLE001 —— 任何 SDK/网络异常都走降级链
                last_err = e
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                _ul.get().record(purpose or self.slot, self.slot, ok=False,
                                 model=model,
                                 latency=time.time() - t0, error=e)
                # 超时类错误才值得换降级模型；其余首试失败直接二试同模型意义不大，
                # 但保留统一重试语义（间隔 0.5s）。
                time.sleep(0.5)
        raise LLMError(f"{self.slot} 调用失败（{len(attempts)} 次）: {last_err}")

    def _thinking_params(self, model: str, max_tokens: int):
        """思考强度设置 → (extra_body 或 None, 生效 max_tokens, 是否开启思考)

        off=显式下发关闭；low/medium/high=开启+预算档；auto=不发参数跟随服务商
        默认（仅 qwen3 系例外，见模块注释）。思考 token 计入输出配额，故开启时
        max_tokens 需加上预算，防思考把正文配额烧穿（deepseek 默认思考的教训）。

        qwen3 系不分档位一律强制关：非流式长输入开思考必超时（P0.5 实测），
        且服务商对思考请求的渐进输出会绕过 read timeout——2026-09-30 牛头案
        thinking=high 挂死 System2 12 分钟才报 Request timed out，期间 ctx
        永远 pending、System1 全瘫、浮窗永久"生成中"。qwen3 要开思考需先
        改流式实现。"""
        effort = (self.settings.get(self.slot, "thinking", default="off")
                  or "off").strip().lower()
        if model.lower().startswith("qwen3"):
            if effort in THINKING_BUDGET:
                print(f"[WARN] {model} 思考档位 '{effort}' 不生效：qwen3 系"
                      "非流式开思考必挂死，已强制关闭（要开思考请换 "
                      "deepseek/glm 系或改流式实现）")
            return {"enable_thinking": False}, max_tokens, False
        if effort == "auto":
            return None, max_tokens, False
        on = effort in THINKING_BUDGET
        budget = THINKING_BUDGET.get(effort, 0)
        if model.lower().startswith("glm"):
            body = {"thinking": {"type": "enabled" if on else "disabled"}}
        else:
            body = {"enable_thinking": on}
            if on and budget:
                body["thinking_budget"] = budget
        return body, max_tokens + budget, on

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
        body, max_tokens, thinking_on = self._thinking_params(model, max_tokens)
        kwargs["max_tokens"] = max_tokens
        if thinking_on:
            # 思考档位下部分平台禁自定义温度，交服务商默认
            kwargs.pop("temperature", None)
        if body:
            kwargs["extra_body"] = body
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
        t0 = time.time()
        try:
            _, meta = self._call_once(
                self.model, "你是连通性探测器。", "回复 JSON: {\"ok\": true}",
                max_tokens=16, temperature=0.0, json_mode=True)
            _ul.get().record("test_connection", self.slot, ok=True,
                             model=meta.get("model", self.model),
                             latency=meta.get("latency"),
                             tokens_in=meta.get("tokens_in", 0),
                             tokens_out=meta.get("tokens_out", 0))
            return {"ok": True, "latency": meta["latency"], "model": meta["model"]}
        except Exception as e:  # noqa: BLE001 —— 测试连接需把失败原因带给 UI
            _ul.get().record("test_connection", self.slot, ok=False,
                             model=self.model, latency=time.time() - t0, error=e)
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
