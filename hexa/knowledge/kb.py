"""知识库加载器（B 层事实层 + A 层先验的内存表示）

职责（fork 自参考项目 main.py DataManager 并扩展）：
- hero_augments.csv        → 英雄×海克斯先验排名（A 层）
- champions.json           → 中文名 ↔ 英文名映射
- pinyin_map.json          → 拼音模糊匹配（LCU 断连时手动输英雄兜底）
- augment_names.json       → 229 海克斯唯一名（OCR 校验集 / 幻觉检测）
- champ_damage_profile.json→ 英雄伤害类型粗分类（敌方威胁画像）
- items_raw.json           → DD 装备元数据（幻觉校验 + current_path 标签）
"""
import csv
import json
from collections import defaultdict
from pathlib import Path

from hexa.config import DATA_DIR, KNOW_DIR

# DD item tags → 伤害/路线语义（current_path 推断用，rules_engine 消费）
# 注：DD 中魔抗 tag 为 SpellBlock（无 MagicResist 这个 tag）
ITEM_TAG_PATH = {
    "SpellDamage": "ap",
    "Damage": "ad",
    "CriticalStrike": "ad",
    "AttackSpeed": "ad",
    "LifeSteal": "ad",
    "ArmorPenetration": "ad",
    "Armor": "tank",
    "SpellBlock": "tank",
    "Health": "tank",
    "HealthRegen": "tank",
}


class KnowledgeBase:
    def __init__(self, data_dir: Path = DATA_DIR, know_dir: Path = KNOW_DIR):
        self.data_dir = data_dir
        self.know_dir = know_dir
        # 英雄 × 海克斯先验: hero -> {augment_name: {tier, overall_rank, t_rank}}
        self.hero_augments = {}
        # 海克斯唯一名集合
        self.augment_names = set()
        # 中文名 -> 英文名 / 英文名(小写) -> 中文名
        self.cn_to_en = {}
        self.en_to_cn = {}
        # 拼音/中文 -> 候选英雄列表
        self.pinyin_map = defaultdict(list)
        # 英雄伤害画像: 中文名 -> {damage, is_tank, tags}
        self.champ_profile = {}
        # DD 装备: 中文名 -> {tags, gold, plaintext}
        self.items = {}
        self.dd_version = ""
        # OP.GG builds: 中文名 -> {core_builds, starter_items, boots, skill_orders, ...}
        self.builds = {}
        self.builds_patch = ""
        self.builds_fetched = ""
        # 海克斯语义标签: 名 -> {path_bias, tags, is_upgrade}
        self.augment_tags = {}
        self.augment_tags_version = ""
        self._load_all()

    # ── 加载 ──────────────────────────────────────────────────────

    def _load_all(self):
        self._load_hero_augments()
        self._load_champions()
        self._load_pinyin()
        self._load_knowledge()
        self._load_items()
        self._load_builds()
        self._load_augment_tags()
        print(f"[OK] 知识库加载: {len(self.hero_augments)} 英雄先验 / "
              f"{len(self.augment_names)} 海克斯 / {len(self.items)} 装备 / "
              f"{len(self.champ_profile)} 英雄画像 / {len(self.builds)} 英雄 builds")

    def _load_builds(self):
        """A 层统计层：data/builds/{slug}.json × 173（OP.GG 出装/出门/鞋子/加点）"""
        builds_dir = self.data_dir / "builds"
        if not builds_dir.is_dir():
            print(f"[WARN] 缺少 {builds_dir}（OP.GG builds，出装候选池将退化为全库扫描）")
            return
        patches = set()
        for path in sorted(builds_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            hero = (data.get("hero_cn") or "").strip()
            if not hero:
                continue
            self.builds[hero] = data
            if data.get("patch"):
                patches.add(data["patch"])
            if data.get("fetched_at") and data["fetched_at"] > self.builds_fetched:
                self.builds_fetched = data["fetched_at"]
        if patches:
            # 数值化取最大（字符串比较会把 "16.9" 排在 "16.19" 后）
            def vkey(v):
                return tuple(int(x) if x.isdigit() else 0
                             for x in str(v).split("."))
            self.builds_patch = sorted(patches, key=vkey)[-1]

    def _load_augment_tags(self):
        """B 层语义标签库（System2 离线打标；缺失时 rules_engine 用内置小表兜底）"""
        path = self.know_dir / "augments.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.augment_tags = data.get("augments", {})
                if data.get("version"):
                    self.augment_tags_version = data["version"]
            except Exception as e:
                print(f"[WARN] augments.json 读取失败: {e}")

    def _load_hero_augments(self):
        path = self.data_dir / "hero_augments.csv"
        if not path.exists():
            print(f"[WARN] 缺少 {path}")
            return
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header or "总排名" not in header:
                print("[WARN] hero_augments.csv 表头不符预期")
            for row in reader:
                if len(row) < 6:
                    continue
                hero = row[0].strip()
                try:
                    overall_rank = int(row[3])
                except ValueError:
                    overall_rank = 999
                try:
                    t_rank = int(row[4])
                except ValueError:
                    t_rank = 999
                name = row[5].strip()
                if hero not in self.hero_augments:
                    self.hero_augments[hero] = {}
                self.hero_augments[hero][name] = {
                    "tier": row[2].strip(),
                    "overall_rank": overall_rank,
                    "t_rank": t_rank,
                }
                self.augment_names.add(name)

    def _load_champions(self):
        path = self.data_dir / "champions.json"
        if path.exists():
            self.cn_to_en = json.loads(path.read_text(encoding="utf-8"))
            self.en_to_cn = {en.lower(): cn for cn, en in self.cn_to_en.items()}

    def _load_pinyin(self):
        path = self.data_dir / "pinyin_map.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for cn, py in data.items():
                if cn not in self.pinyin_map[py]:
                    self.pinyin_map[py].append(cn)
                if cn not in self.pinyin_map[cn]:
                    self.pinyin_map[cn].append(cn)

    def _load_knowledge(self):
        path = self.know_dir / "champ_damage_profile.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.champ_profile = data.get("champions", {})
            self.dd_version = data.get("version", "")
        # augment_names.json 是 CSV 的派生文件，仅当 CSV 缺失时兜底
        if not self.augment_names:
            path = self.know_dir / "augment_names.json"
            if path.exists():
                self.augment_names = set(
                    json.loads(path.read_text(encoding="utf-8")).get("names", []))

    def _load_items(self):
        path = self.know_dir / "items_raw.json"
        if not path.exists():
            print(f"[WARN] 缺少 {path}（DD 装备库，幻觉校验将失效）")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.dd_version = data.get("version", self.dd_version)
        # 同名装备在 DD 里有多份模式条目（峡谷 3xxx/8xxx、大乱斗 22xxxx、
        # KIWI 模式 77xxxx，价格各异）；商店实测按峡谷价出售，故同名去重
        # 优先保留 id 最短（峡谷正价）条目，避免价格随机错+合成链指向
        # 模式专属名导致建议与商店对不上。仅模式存在的名字照常保留。
        best = {}
        for _iid, it in data.get("data", {}).items():
            name = (it.get("name") or "").strip()
            if not name:
                continue
            cur = best.get(name)
            if cur is None or len(_iid) < len(cur[0]) or (
                    len(_iid) == len(cur[0]) and int(_iid or 0) < int(cur[0] or 0)):
                best[name] = (_iid, it)
        for name, (_iid, it) in best.items():
            self.items[name] = {
                "id": _iid,
                "tags": it.get("tags", []),
                "gold": (it.get("gold") or {}).get("total", 0),
                "plaintext": it.get("plaintext", ""),
                "from": it.get("from", []) or [],   # 组件 item id 列表
                "into": it.get("into", []) or [],   # 升级目标 item id 列表
            }
        # id -> name 索引（组件/升级链换算用）：全量 id 都映射到规范名，
        # 任一模式条目的 from/into 引用都能落到同一装备
        self.item_id_to_name = {
            iid: (it.get("name") or "").strip()
            for iid, it in data.get("data", {}).items()
            if (it.get("name") or "").strip()}

    # ── 英雄查询（fork 参考项目 DataManager 接口） ─────────────────

    def validate_hero(self, name: str, threshold: int = 80):
        """英雄名校验 + 模糊映射，返回规范中文名或 None"""
        if not name:
            return None
        if name in self.hero_augments:
            return name
        try:
            from thefuzz import process
            result = process.extractOne(name, list(self.hero_augments.keys()))
            if result and result[1] > threshold:
                return result[0]
        except ImportError:
            pass
        return None

    def search_hero(self, query: str):
        """拼音/中文搜索，返回 (候选列表, 是否精确)"""
        query = query.strip().lower()
        if query in self.pinyin_map:
            return self.pinyin_map[query], True
        validated = self.validate_hero(query, threshold=60)
        if validated:
            return [validated], False
        return [], False

    def champion_cn(self, en_name: str):
        """英文英雄名（LCU/Live API 返回）→ 中文名"""
        if not en_name:
            return None
        return (self.en_to_cn.get(en_name.lower())
                or self.en_to_cn.get(en_name.replace(" ", "").lower()))

    # ── 海克斯先验查询 ────────────────────────────────────────────

    TIER_PRIORITY = {"棱彩": 0, "黄金": 1, "白银": 2, "未知": 3}

    def top_augments(self, hero_cn: str, n: int = 20):
        """该英雄的海克斯亲和 top N（按 总排名 → 稀有度 → 档内序）"""
        augments = self.hero_augments.get(hero_cn)
        if not augments:
            return []
        ranked = sorted(
            augments.items(),
            key=lambda kv: (kv[1]["overall_rank"],
                            self.TIER_PRIORITY.get(kv[1]["tier"], 3),
                            kv[1]["t_rank"]))
        return [name for name, _ in ranked[:n]]

    def augment_info(self, hero_cn: str, augment_name: str):
        return self.hero_augments.get(hero_cn, {}).get(augment_name)

    # ── 装备查询（rules_engine / 幻觉校验用） ──────────────────────

    def item_path_bias(self, item_name: str):
        """装备名 → 路线倾向（ap/ad/tank/None），按 DD tags 投票"""
        info = self.items.get(item_name)
        if not info:
            return None
        votes = {"ap": 0, "ad": 0, "tank": 0}
        for tag in info["tags"]:
            path = ITEM_TAG_PATH.get(tag)
            if path:
                votes[path] += 1
        best = max(votes, key=votes.get)
        return best if votes[best] > 0 else None

    def valid_item(self, name: str) -> bool:
        return name in self.items

    def valid_augment(self, name: str) -> bool:
        return name in self.augment_names

    # ── OP.GG builds 查询（P1：候选池升级为高频装） ─────────────────

    def build_for(self, hero_cn: str) -> dict:
        """英雄 OP.GG builds 原始数据（无数据返回 {}）"""
        return self.builds.get(hero_cn, {})

    def core_item_pool(self, hero_cn: str) -> list:
        """该英雄 OP.GG 高频核心装（按登场序展平去重，越靠前越高频）。

        仅保留 DD 库内条目（OP.GG 与 DD 中文命名一致，实测命中率 100%，
        此处过滤只作防御）。
        """
        build = self.builds.get(hero_cn, {})
        pool, seen = [], set()
        for combo in build.get("core_builds", []):
            for name in combo:
                if name not in seen and self.valid_item(name):
                    seen.add(name)
                    pool.append(name)
        return pool

    def starter_combos(self, hero_cn: str) -> list:
        """出门装方案（[[...], ...]，全部组件过 DD 校验）"""
        build = self.builds.get(hero_cn, {})
        out = []
        for combo in build.get("starter_items", []):
            if combo and all(self.valid_item(n) for n in combo):
                out.append(list(combo))
        return out

    def boots_options(self, hero_cn: str) -> list:
        build = self.builds.get(hero_cn, {})
        return [list(c) for c in build.get("boots", [])
                if c and all(self.valid_item(n) for n in c)]

    def skill_advice(self, hero_cn: str) -> dict:
        """静态加点建议（v4.1 收窄：只给大概顺序，不做实时技能点计算）"""
        build = self.builds.get(hero_cn, {})
        orders = build.get("skill_orders") or []
        return {
            "priority": build.get("skill_priority") or "",
            "order": orders[0] if orders else [],
            "names": build.get("skill_names") or {},
        }

    # ── 海克斯语义标签（B 层，System2 离线打标产物） ────────────────

    def augment_tag_info(self, name: str) -> dict:
        return self.augment_tags.get(name, {})
