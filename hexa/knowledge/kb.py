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
import threading
import time
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
        # 海克斯卡面原文描述（20261013 描述自举）：名 -> 描述文本——
        # 对局中卡面 OCR 直出写回，随真实对局自增长（标签库是离线从名字
        # 猜的压缩结论，真机实锤 大法师 被标 ap/法穿/爆发 而原文是技能
        # 花费返还——原文才是事实源，评级 prompt 冲突时以描述为准）
        self.augment_descs = {}
        self._desc_lock = threading.Lock()
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
        self._load_augment_descs()
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

    def _load_augment_descs(self):
        """卡面原文描述库（20261013 描述自举；对局 OCR 写回，随版本自更新）"""
        path = self.know_dir / "augment_descs.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.augment_descs = {name: (entry.get("text") or "")
                                      for name, entry in
                                      (data.get("descs") or {}).items()}
            except Exception as e:
                print(f"[WARN] augment_descs.json 读取失败: {e}")

    def augment_desc(self, name: str) -> str:
        """卡面原文描述（未收录返回空串——调用方按空值跳过，勿编造）"""
        return self.augment_descs.get(name, "")

    def record_augment_desc(self, name: str, text: str) -> bool:
        """写回一条卡面描述（OCR 线程调用）：锁内合并 + 原子落盘。

        同名已有且文本一致 → 跳过写盘（每轮评级都会回调，不刷盘）；
        文本不同（版本改版/上轮 OCR 噪声）→ 覆盖取新。写盘失败只警告
        （资产是增速器，丢一轮下局再长）。"""
        if not name or not text:
            return False
        with self._desc_lock:
            if self.augment_descs.get(name) == text:
                return False
            self.augment_descs[name] = text
            path = self.know_dir / "augment_descs.json"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                data = {"version": 1, "descs": {
                    n: {"text": t, "ts": time.time()}
                    for n, t in self.augment_descs.items()}}
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                tmp.replace(path)
            except Exception as e:  # noqa: BLE001 —— 资产写盘失败不影响对局
                print(f"[WARN] augment_descs.json 写盘失败: {e}")
            return True

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
                if not name or name == "？？？":
                    # 上游榜单占位行：不入档也不进唯一名集合（防 OCR
                    # 幻觉校验被占位符污染；真名补齐需重抓 CSV，运维项）
                    continue
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
            try:
                self.cn_to_en = json.loads(path.read_text(encoding="utf-8"))
                self.en_to_cn = {en.lower(): cn for cn, en in self.cn_to_en.items()}
            except Exception as e:  # noqa: BLE001 —— 文件损坏不崩启动
                print(f"[WARN] champions.json 读取失败（中英映射降级）: {e}")

    def _load_pinyin(self):
        path = self.data_dir / "pinyin_map.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for cn, py in data.items():
                    if cn not in self.pinyin_map[py]:
                        self.pinyin_map[py].append(cn)
                    if cn not in self.pinyin_map[cn]:
                        self.pinyin_map[cn].append(cn)
            except Exception as e:  # noqa: BLE001 —— 文件损坏不崩启动
                print(f"[WARN] pinyin_map.json 读取失败（拼音兜底降级）: {e}")

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
        # 斗魂竞技场 44xxxx、KIWI 77xxxx，价格各异）；商店实测按峡谷价出售。
        # 去重优先级：峡谷可买 > 大乱斗可买 > 其它，同级内取最短 id——
        # 同长度不同 id 时小 id 未必是峡谷条目（放血者的诅咒 4010 乱斗
        # 2500 vs 8010 峡谷 2900），必须先按地图分层再比 id。
        best = {}
        buy_maps = {}   # 名字 -> 在 11/12 哪些图有可买条目
        from12 = {}     # 名字 -> map12 可买条目是否带 from 合成链
        for _iid, it in data.get("data", {}).items():
            name = (it.get("name") or "").strip()
            if not name:
                continue
            maps = it.get("maps") or {}
            non77 = not _iid.startswith("77")
            # KIWI 模式条目(77xxxx)不参与 11/12 可买性判定：DD 给它们标了
            # map12/453，但真机海克斯大乱斗商店没有（麦瑞德裂血手套案
            # 2026-09-27）；同名装备以峡谷/大乱斗正价条目为准
            if non77 and (it.get("gold") or {}).get("purchasable"):
                for m in ("11", "12"):
                    if maps.get(m):
                        buy_maps.setdefault(name, set()).add(m)
                if maps.get("12") and it.get("from"):
                    from12[name] = True
            # 入库看地图标记（魔切/炽天使之拥等商店不可直购的自动升级件
            # 保留在库里供台账/路线推断，可推荐性由 buy_maps/item_available 管）
            tier = 0 if (non77 and maps.get("11")) else (
                1 if (non77 and maps.get("12")) else 2)
            key = (tier, len(_iid), int(_iid or 0))
            cur = best.get(name)
            if cur is None or key < cur[0]:
                best[name] = (key, _iid, it)
        # 海斗"任务合成件"守卫（虚空献祭案 2026-09-29）：DD 把终极任务件
        # 标成 map12 可买（虚空献祭/终极九头蛇/沃格勒特的巫师帽，全 6000G），
        # 但商店常规货架根本没有——它们由任务海克斯（升级祭献/提亚马特
        # 任务等）合成/解锁，真机商店里搜不到（用户实证），直推"还差X金"
        # 等于推荐一件买不到的装。判据用结构不用名单：峡谷（map11）从无
        # 可买主张 + map12 可买条目无 from 合成链 → 任务/特殊件；大乱斗
        # 货架真卖的专属件（救生索/幽魂弯刀/毁坏仪式）带组件链，不受影响。
        # 仍入库（任务合成后会上身，台账/幻觉校验/卖价要用），仅从可买
        # 地图摘除——推荐性由 item_available 管，同魔切/炽天使之拥口径。
        for name, maps in list(buy_maps.items()):
            if maps == {"12"} and not from12.get(name):
                buy_maps[name] = set()
        # 峡谷+大乱斗都不可买 = 斗魂竞技场/KIWI 专属（飞升护符、黑洞护手、
        # 麦瑞德裂血手套等），本作商店里根本没有，推荐出去等于幻觉，
        # 直接不入库（幻觉校验随之收紧）；id→名全量映射保留供合成链换算。
        for name, (key, _iid, it) in best.items():
            if key[0] >= 2:
                continue
            self.items[name] = {
                "id": _iid,
                "tags": it.get("tags", []),
                "gold": (it.get("gold") or {}).get("total", 0),
                # 卖出价（DD gold.sell，实测全量条目都带）：满六神装后
                # "卖X换Y"的差价核算依据（v4.4 替换建议）
                "sell": (it.get("gold") or {}).get("sell", 0),
                "plaintext": it.get("plaintext", ""),
                "from": it.get("from", []) or [],   # 组件 item id 列表
                "into": it.get("into", []) or [],   # 升级目标 item id 列表
                # 常规货架可买地图：名字任一非 KIWI 条目在 11/12 可买即算
                # （任务合成件已摘除，见上方守卫），供 item_available 按
                # 当前对局地图过滤
                "maps": sorted(buy_maps.get(name, set())),
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

    def item_available(self, name: str, map_id: str = "") -> bool:
        """装备在指定地图商店是否可买（"11"=召唤师峡谷 / "12"=极地大乱斗）。

        map_id 缺失/未知时退化为"任一支持地图可买"（宽松兜底，行为同
        未按图过滤）；竞技场/KIWI 专属件不入库、任务合成件（虚空献祭案）
        可买地图已摘空，任一图都查不到。
        """
        info = self.items.get(name)
        if not info:
            return False
        maps = info.get("maps") or []
        if map_id in ("11", "12"):
            return map_id in maps
        return bool(maps)

    def valid_augment(self, name: str) -> bool:
        return name in self.augment_names

    def item_sell_price(self, name: str) -> int:
        """装备卖出价（DD gold.sell；未知装备 0）。替换建议差价核算用"""
        return self.items.get(name, {}).get("sell", 0)

    def permanent_items(self, names: list) -> list:
        """过滤消耗品/饰品后的占格装备名列表（满六神装判定用）。

        大乱斗虽无饰品位，药水等消耗品会临时占格，数格前须排除；
        gold=0 且无 tags 的占位条目（魄罗佳肴/防御工事/小兵去质器/
        银蛇币杂项/普朗克 占位，全库实测 7 条）同样不算正式件——
        它们占格但不构成战力，2026-09-28 炽炎雏龙局 1 传说+魄罗佳肴
        凑满 6 格被误判满装即此因。
        库外名（DD 版本滞后于游戏的新装备）按占格保留——宁可多判
        满装走替换流，也不漏判后继续推买不进的"下一件"。
        """
        out = []
        for n in names:
            info = self.items.get(n)
            if info is None or not ({"Consumable", "Trinket"} & set(info["tags"])):
                if info is not None and not info.get("gold") and not info.get("tags"):
                    continue
                out.append(n)
        return out

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

    def core_build_line(self, hero_cn: str, current_path: str = "") -> list:
        """英雄核心套装线（v4.3 主推逻辑，用户验收口径 2026-09-27：
        "先出自己流派的核心三件套，敌情是后续调整"）。

        按当前路线选最匹配的组合：小丑 ad→收集者线 / ap→黯炎火炬·面具线，
        剑魔 ad→锯齿短匕·狂妄·焚天·死亡之舞（2026-09-29 重抓）。匹配票数 = 组合内与路线同 bias 的
        件数，同票取 OP.GG 登场序靠前；无路线（对局早期）退回首组。
        返回展平装备名列表（组合内顺序），组合不合法（含库外名）整组跳过。
        """
        combos = self.builds.get(hero_cn, {}).get("core_builds") or []
        best, best_key = None, None
        for i, combo in enumerate(combos):
            if not combo or not all(self.valid_item(n) for n in combo):
                continue
            votes = sum(1 for n in combo
                        if current_path and self.item_path_bias(n) == current_path)
            key = (votes, -i)
            if best_key is None or key > best_key:
                best, best_key = list(combo), key
        return best or []

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

    # 『升级：』为升级型海克斯的官方命名前缀（升级：中娅/升级：收集者…，
    # 强化对应基础海克斯效果）；标签库另有不带前缀的强化型条目，以
    # is_upgrade 字段标注（升级联动案 20261013 起供局内联动判定消费）
    UPGRADE_PREFIX = "升级："

    def augment_is_upgrade(self, name: str) -> bool:
        """升级型海克斯判定（事实层：读标签库 is_upgrade 字段，不做任何
        出装语义判断——方案怎么适配是模型的判断工作）。

        双保险：名称以『升级：』前缀开头亦可判——标签库落后于游戏
        新出的升级型海克斯时不漏判。空名/两者皆否 → False。
        """
        if not name:
            return False
        if name.startswith(self.UPGRADE_PREFIX):
            return True
        return bool(self.augment_tags.get(name, {}).get("is_upgrade"))
