"""fixtures dump：真机调测时按局落盘 LCU / Live API 原始响应

用途（计划 v4.1 §7）：盲写阶段无法提前 dump，真机联调时打开开关自动落盘，
后续开发用落盘数据 mock 回放（tests/smoke_offline.py 即消费此格式）。

目录结构: fixtures/{YYYYmmdd-HHMMSS}-{英雄|unknown}/
    nnn-{kind}.json        按时间序编号的原始响应
    _fields.md             字段清单（每 endpoint 顶层键 + playerlist 元素键汇总）
"""
import json
import time
from pathlib import Path

from hexa.config import FIXTURES_DIR


class FixtureDumper:
    """enabled=False 时所有 dump 静默跳过（默认关闭，settings.fixtures.dump 控制）"""

    def __init__(self, enabled: bool = False, root: Path = FIXTURES_DIR):
        self.enabled = enabled
        self.root = root
        self._dir: Path | None = None
        self._seq = 0
        self._seen_fields: dict = {}   # kind -> 顶层键集合（playerlist 特殊记元素键）

    # ── 会话管理 ──────────────────────────────────────────────────

    def start_session(self, label: str = "unknown"):
        """新开局（GameStart 时调用），创建局目录并重置序号"""
        if not self.enabled:
            return
        self._dir = self.root / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._seen_fields = {}
        print(f"[OK] fixtures 会话目录: {self._dir}")

    def end_session(self):
        """对局结束：写字段清单汇总"""
        if not self.enabled or not self._dir:
            return
        try:
            self._write_field_inventory()
        except Exception as e:
            print(f"[WARN] 字段清单写入失败: {e}")
        self._dir = None

    # ── dump ──────────────────────────────────────────────────────

    def dump(self, kind: str, payload):
        if not self.enabled or self._dir is None:
            return
        self._seq += 1
        path = self._dir / f"{self._seq:04d}-{kind}.json"
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except Exception as e:
            print(f"[WARN] fixtures dump 失败 ({kind}): {e}")
        self._record_fields(kind, payload)

    def dump_lcu(self, endpoint: str, payload):
        """LCU 响应（endpoint 转 kind：/a/b -> a_b）"""
        self.dump("lcu_" + endpoint.strip("/").replace("/", "_"), payload)

    # ── 字段清单（P0 交付物：实测字段清单模板） ────────────────────

    def _record_fields(self, kind: str, payload):
        if isinstance(payload, list):
            keys = set()
            for item in payload[:20]:
                if isinstance(item, dict):
                    keys |= set(item.keys())
        elif isinstance(payload, dict):
            keys = set(payload.keys())
        else:
            return
        self._seen_fields.setdefault(kind, set()).update(keys)

    def _write_field_inventory(self):
        lines = ["# 字段清单（自动汇总，真机实测）", ""]
        for kind in sorted(self._seen_fields):
            keys = sorted(self._seen_fields[kind])
            lines.append(f"## {kind}")
            lines.append("- " + " / ".join(keys) if keys else "- (空)")
            lines.append("")
        (self._dir / "_fields.md").write_text("\n".join(lines), encoding="utf-8")
