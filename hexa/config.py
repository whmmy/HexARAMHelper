"""HexARAMHelper 全局配置：路径常量 + settings.json 读写

settings.json 为本地运行时配置（已被 .gitignore 排除，绝不入库）。
API key 双通道：settings.json 的 api_key 字段（本地明文）或环境变量（默认 DASHSCOPE_API_KEY）。
"""
import json
import os
import sys
from pathlib import Path


def get_base_dir() -> Path:
    """应用根目录（兼容 PyInstaller frozen 与源码运行）"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
DATA_DIR = BASE_DIR / "data"
KNOW_DIR = BASE_DIR / "hexa" / "knowledge"
FIXTURES_DIR = BASE_DIR / "fixtures"
SETTINGS_PATH = BASE_DIR / "settings.json"

# ── settings.json 默认值 ─────────────────────────────────────────

DEFAULTS = {
    # AI 总开关（关 = 纯规则模式，P0 验收要求无 key 可用）
    "ai": {
        "enabled": False,
        "budget_per_game": 15,        # 每局 System1 调用硬上限
    },
    # System 2：ChampionContext 生成（每局 1 次）
    "system2": {
        "model": "qwen3.8-max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",                # 留空则回退环境变量
        "api_key_env": "DASHSCOPE_API_KEY",
        "temperature": 0.3,
        "max_tokens": 1500,
        "timeout": 60,
    },
    # System 1：快照建议器（每局 8~11 次）
    "system1": {
        "model": "qwen3.8-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "api_key_env": "DASHSCOPE_API_KEY",
        "temperature": 0.3,
        "max_tokens": 800,
        "timeout": 30,
    },
    # 局内触发器开关（计划 §5 触发器可配置）
    "triggers": {
        "death_next_item": True,      # 我死亡 → 下一件三选
        "enemy_key_item": True,       # 敌方关键装备完成 → 克制提示
        "enemy_kda": True,            # 敌方 KDA 起飞（每局限 2 次）
        "kda_threshold": 3.0,
    },
    # 真机调测 dump（P0 字段实测用）
    "fixtures": {
        "dump": False,
    },
    "ui": {
        "overlay_visible": True,
    },
}


class Settings:
    """settings.json 读写（深层默认值合并，未知键保留）"""

    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self._data = json.loads(json.dumps(DEFAULTS))  # 深拷贝
        self.load()

    def load(self):
        if self.path.exists():
            try:
                user = json.loads(self.path.read_text(encoding="utf-8"))
                self._merge(self._data, user)
            except Exception as e:
                print(f"[WARN] settings.json 读取失败，使用默认值: {e}")

    @staticmethod
    def _merge(base: dict, override: dict):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                Settings._merge(base[k], v)
            else:
                base[k] = v

    def save(self):
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception as e:
            print(f"[WARN] settings.json 写入失败: {e}")

    # dict 风格访问: settings["ai"]["enabled"]
    def __getitem__(self, key):
        return self._data[key]

    def get(self, *keys, default=None):
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def set(self, *keys_and_value):
        *keys, value = keys_and_value
        node = self._data
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        self.save()

    def resolve_api_key(self, slot: str) -> str:
        """槽位（system1/system2）的 key：settings 明文优先，环境变量回退"""
        key = self.get(slot, "api_key", default="") or ""
        if key.strip():
            return key.strip()
        env_name = self.get(slot, "api_key_env", default="DASHSCOPE_API_KEY")
        return (os.environ.get(env_name) or "").strip()
