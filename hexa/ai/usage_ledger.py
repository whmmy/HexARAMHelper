"""AI 调用台账（按对局分文件 JSONL：完整提示词 + 返回原文，复盘用）

每个 AI 环节（system2 ctx / system1 opening·next·swap·ranks /
desc_check 描述校验 / test_connection 连通性测试）每次 chat 调用落
一行：发出去的 system/user 提示词全文、模型返回原文、purpose、slot、
model、tokens、费用、latency、成败与错误摘要。**按对局组织**：
一局一个文件 log/ai_calls/{YYYYMMDD_HHMMSS}_{英雄}.jsonl——与
log/games/ 复盘日志同款命名规则（同源于 game_start 时刻），逐局
对照"这局 AI 看到了什么、答了什么、哪个模型表现如何、花了多少"，
评估要不要换模型时按文件读即可。

选人期预生成（champ select 阶段的 System2 ctx）发生在开局前：先
缓冲，开局轮转时随本局文件落盘——ctx 调用不丢归属。

保留策略：每局轮转时按 diagnostics.ai_ledger_keep_days（默认 14 天）
清理过期文件，无常驻定时器。

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

    UNFILED_CAP = 100   # 无对局文件时的缓冲上限（防"从不开局"无限堆积）

    def __init__(self, log_dir=None, keep_days=14):
        # 运行时读 _config.BASE_DIR 属性（非 import 期绑定）——冒烟测试
        # 把 BASE_DIR 指到临时目录后，未显式传 log_dir 的构造也自动隔离
        self.log_dir = log_dir or (_config.BASE_DIR / "log" / "ai_calls")
        self.keep_days = keep_days
        self._write_lock = threading.Lock()   # 局文件状态与追加行原子性
        self._game_stem = None                # 本局文件主干（无扩展名）
        self._hero = ""
        self._pending = []                    # 开局前的调用（选人期预生成）
        self._warn_ts = 0.0                   # 写盘异常限频（60s 一条）

    def new_game(self, game_id: str, hero: str = ""):
        """轮转到新对局：game_id 形如 20261016_183000_暗裔剑魔（与复盘
        日志同名规则），此后 record 落本局文件；开局前缓冲的调用（选人
        期 ctx 预生成）随本文件落盘；顺带清理过期台账（每局至多扫一次）。"""
        with self._write_lock:
            self._game_stem = str(game_id or "").strip() or None
            self._hero = str(hero or "")
            rows, self._pending = self._pending, []
            if self._game_stem:
                for row in rows:
                    self._append(row)
            self.purge_expired()   # 定时清除：每局轮转时扫一次（锁内，快）

    def record(self, purpose, slot, ok, model="", latency=None,
               tokens_in=0, tokens_out=0, cost=None, error="",
               system="", user="", response=""):
        """一次 chat 调用 → 一行 JSONL。purpose=业务环节
        （ctx/opening/next/swap/ranks/desc_check），slot=计价槽位；
        system/user=发出去的提示词全文，response=模型返回原文
        （失败的尝试 response 为空、error 带原因）。"""
        row = {"ts": round(time.time(), 3), "hero": self._hero,
               "purpose": purpose, "slot": slot, "ok": bool(ok),
               "model": model,
               "latency": round(latency, 2) if latency else None,
               "tokens_in": int(tokens_in or 0),
               "tokens_out": int(tokens_out or 0),
               "cost": round(cost, 6) if cost else None,
               "error": str(error)[:160],
               "system": str(system or ""),
               "user": str(user or ""),
               "response": str(response or "")}
        with self._write_lock:
            if self._game_stem:
                self._append(row)
            else:
                self._pending.append(row)
                self._pending = self._pending[-self.UNFILED_CAP:]

    def _append(self, row):
        """锁内调用：追加一行到本局文件"""
        path = self.log_dir / f"{self._game_stem}.jsonl"
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 —— 台账绝不外抛，主链无感
            self._warn(e)

    def purge_expired(self):
        """删除 keep_days 天前的台账文件（new_game 时调用）"""
        try:
            cutoff = time.time() - max(0, self.keep_days) * 86400.0
            for f in self.log_dir.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001 —— 清理失败不影响主链
            self._warn(e)

    def _warn(self, e):
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
