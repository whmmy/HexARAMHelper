"""AI 调用台账（JSONL 逐条落盘，复盘用）

每个 AI 环节（system2 ctx / system1 opening·next·swap·ranks /
desc_check 描述校验 / test_connection 连通性测试）每次 chat 调用
落一条：purpose、slot、model、tokens、latency、成败与错误摘要，
外加局上下文（hero）。复盘口径：与 log/games/ 复盘日志按 ts 对齐，
即可还原"这一局每个 AI 环节调了几次、花了多少、哪次失败"。

先例同 runes_probe（JSONL 追加写 + 运行时读 BASE_DIR + 限频 WARN）。
红线：台账任何异常只 WARN 不外抛——绝不影响调用主链；写盘失败丢的
只是复盘证据，不丢对局。
"""
import json
import threading
import time

import hexa.config as _config


class UsageLedger:
    """AI 调用逐条落库（线程安全；LLM 调用来自多个 daemon 线程）"""

    LOG_NAME = "ai_calls.jsonl"

    def __init__(self, log_dir=None):
        # 运行时读 _config.BASE_DIR 属性（非 import 期绑定）——冒烟测试
        # 把 BASE_DIR 指到临时目录后，未显式传 log_dir 的构造也自动隔离
        self.log_dir = log_dir or (_config.BASE_DIR / "log" / "ai_calls")
        self._write_lock = threading.Lock()   # 追加行原子性（多线程并发写）
        self._ctx = {}                        # 局上下文（set_context 写）
        self._warn_ts = 0.0                   # 写盘异常限频（60s 一条）

    def set_context(self, **ctx):
        """更新局上下文（game_start 时由 app 层喂 hero 等），随每行落盘"""
        with self._write_lock:
            self._ctx = {k: v for k, v in ctx.items() if v}

    def record(self, purpose, slot, ok, model="", latency=None,
               tokens_in=0, tokens_out=0, error=""):
        """一次 chat 调用 → 一行 JSONL。purpose=业务环节
        （ctx/opening/next/swap/ranks/desc_check），slot=计价槽位。"""
        with self._write_lock:
            row = {"ts": round(time.time(), 3), **self._ctx,
                   "purpose": purpose, "slot": slot, "ok": bool(ok),
                   "model": model,
                   "latency": round(latency, 2) if latency else None,
                   "tokens_in": int(tokens_in or 0),
                   "tokens_out": int(tokens_out or 0),
                   "error": str(error)[:160]}
            path = self.log_dir / self.LOG_NAME
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception as e:  # noqa: BLE001 —— 台账绝不外抛，主链无感
                now = time.monotonic()
                if now - self._warn_ts >= 60.0:
                    self._warn_ts = now
                    print(f"[WARN] AI 台账落盘失败（60s 限频一条）: {e}")


_default = UsageLedger()


def get() -> UsageLedger:
    return _default


def set_default(ledger: UsageLedger):
    """替换默认实例（冒烟测试隔离落盘目录用）"""
    global _default
    _default = ledger
