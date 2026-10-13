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


def load_dotenv(path: Path = None):
    """读取项目根 .env（简单 KEY=VALUE；不覆盖已有环境变量；无额外依赖）"""
    path = path or (BASE_DIR / ".env")
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except Exception as e:
        print(f"[WARN] .env 读取失败: {e}")


load_dotenv()

# ── settings.json 默认值 ─────────────────────────────────────────

DEFAULTS = {
    # AI 总开关（关 = 纯规则模式，P0 验收要求无 key 可用）
    "ai": {
        "enabled": False,
        "budget_per_game": 0,         # 每局 System1 调用上限（<=0 = 不限，
                                      # 2026-09-30 用户拍板：分级单价下成本可忽略；
                                      # 要限就填正数，超限自动纯规则）
        "min_interval": 20,           # periodic 类精修最小间隔秒（AGENTS 成本
                                      # 守门条款：20s 不可松）；event 类不受限
    },
    # System 2：ChampionContext 生成（每局 1 次）
    "system2": {
        "model": "qwen3.8-max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",                # 留空则回退环境变量
        "api_key_env": "DASHSCOPE_API_KEY",
        "temperature": 0.3,
        "max_tokens": 1500,
        "thinking": "off",            # off/low/medium/high/auto（auto=跟随服务商默认）
        "timeout": 60,
        "fallback_model": "",         # 超时降级模型（空=不降级，同模型重试）
        "price_in": 2.4,              # ¥/百万 token（费用仪表）
        "price_out": 9.6,
    },
    # System 1：快照建议器（每局 8~11 次）
    "system1": {
        "model": "qwen3.8-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "api_key_env": "DASHSCOPE_API_KEY",
        "temperature": 0.3,
        "max_tokens": 800,
        "thinking": "off",            # deepseek 系默认深度思考，显式关掉才快（也不烧穿配额）
        "timeout": 30,
        "fallback_model": "",
        "price_in": 0.8,
        "price_out": 2.7,
    },
    # 局内触发器开关（计划 §5 触发器可配置）
    "triggers": {
        "death_next_item": True,      # 我死亡 → 立即重算出装主线（事件触发）
        "periodic_recheck": True,     # 快照 diff 定时重算（v4.2：装备/金币/敌方装备变化即刷）
        "enemy_key_item": True,       # 敌方关键装备完成 → 克制提示
        "enemy_kda": True,            # 敌方 KDA 起飞（每局限 2 次）
        "kda_threshold": 3.0,
    },
    # 真机调测 dump（P0 字段实测用）
    "fixtures": {
        "dump": False,
        "runes_probe": True,   # 海克斯字段落库探针（log/runes_probe/ 下
                               # JSONL，§6 复核用；纯落库不进推荐链）
    },
    # 诊断开关（app.py 消费；补进默认值防"settings 全删重建后配置面漂移"）
    "diagnostics": {
        "elevate_auto": True,         # 未提权时自动弹 UAC 重启（游戏内点击记账前提）
        "click_capture": True,        # 点击定格截图落 log/captures/（排障用）
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
                # 损坏文件先备份再回退默认：api_key/预算等全在里面，
                # 静默覆盖等于配置蒸发，留着 .bak 人工可救
                bak = self.path.with_suffix(".json.bak")
                try:
                    os.replace(self.path, bak)
                    print(f"[WARN] settings.json 读取失败（已备份到 "
                          f"{bak.name}），使用默认值: {e}")
                except OSError:
                    print(f"[WARN] settings.json 读取失败，使用默认值: {e}")

    @staticmethod
    def _merge(base: dict, override: dict):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                Settings._merge(base[k], v)
            else:
                base[k] = v

    def save(self):
        # 原子写（tmp→replace）：直写中途崩溃/断电会截断 JSON，下次启动
        # 读取失败静默回退全默认——api_key/预算全部丢失且仅一条 WARN
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[WARN] settings.json 写入失败: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

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

    def get_float(self, *keys, default: float) -> float:
        """数值读取：空串/非法值回退 default（历史 settings.json 可能存 ""）"""
        try:
            return float(self.get(*keys, default=default))
        except (TypeError, ValueError):
            return default

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
