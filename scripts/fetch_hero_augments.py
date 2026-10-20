#!/usr/bin/env python
"""海克斯先验抓取（N6 数据流水线补齐：data/hero_augments.csv 的上游断链修复）

上游调研结论（竞品报告 §3.3 三候选实测，2026-10-20）：
- apexlol.info/zh：403 反爬，放弃；
- hextech.dtodo.cn：301 → aramgg.com，两候选同源合并（站内 JSON-LD 的
  logo 也指向 cdn.dtodo.cn，实为同一项目改版）；
- aramgg.com：现为 Astro 静态站，robots.txt 明面 `Allow: /data/*.json`，
  站内 ChampionAugments.js 揭示全量静态 JSON 链——即报告所说 hex-helper
  「buildId 直取 JSON 干净路子」的当今形态。且与旧 CSV 逐行可对齐
  （暗裔剑魔 rank1 贪欲束缚 黄金/1、rank2 升级：死亡之舞 白银/1 与
  2026-09-25 资产完全一致，rank3+ 差异属版本演进）→ 选此路：
  纯 JSON 无 HTML 解析、CDN 静态文件、与既有资产同源同语义。

数据链（三级静态 JSON）：
  1) /data/champions-names-i18n.json          173 英雄 id/alias/多语名
  2) /data/aram-mayhem-augments.zh_cn.json    海克斯目录（中文名 + rarity）
  3) cdn.dtodo.cn/hextech/champion-details/{id}.json（每英雄全量统计）
     失败回退同源 /data/champion-details/{id}.json（站方自带的二级源，
     与站内 JS「CDN 优先/同源回退」取数策略同构）

输出 CSV schema 不变：中文名,英文名,等级,总排名,等级内序号,海克斯名称
  等级   = 海克斯稀有度（rarity 0白银/1黄金/2棱彩；站点 rarity-labels 原文）
  总排名 = detail rank（站方按胜率全局序，1..total 稠密）
  等级内序号 = 同稀有度内按总排名序的 1 基序号（与旧资产 t_rank 同义）

安全语义：先写临时文件 → 校验（行数量级/等级值域/英雄覆盖/占位名）→
原子替换；任一步失败保留旧 CSV 不动（宁旧勿坏）。当日缓存于
data/cache/augments/（已 gitignore），断点续爬。

用法:
    uv run python scripts/fetch_hero_augments.py              # 全量刷新
    uv run python scripts/fetch_hero_augments.py --force      # 忽略当日缓存
    uv run python scripts/fetch_hero_augments.py --dry-run    # 只验证不落盘
"""
import csv
import datetime
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hexa.config import DATA_DIR

SITE = "https://aramgg.com"
CDN = "https://cdn.dtodo.cn/hextech"
NAMES_URL = f"{SITE}/data/champions-names-i18n.json"
CATALOG_URL = f"{SITE}/data/aram-mayhem-augments.zh_cn.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}
TIMEOUT = 30
RETRIES = 3
DETAIL_SLEEP = 0.25      # CDN 静态文件，礼貌间隔即可（非 HTML 爬页面）

# 稀有度 → 等级（站点 champion 页 data-rarity-labels 原文：0白银/1黄金/2棱彩）
RARITY_TIER = {0: "白银", 1: "黄金", 2: "棱彩"}

# 校验门槛（N6：宁旧勿坏——不过门槛不落盘）
MIN_CHAMPIONS = 160      # 站点当前 173 英雄，留出上游临时缺英雄的余量
MIN_ROWS = 15000         # 173 英雄 × 每英雄 ~126 条（16.20 池）≈ 2.2 万行
MAX_ROWS = 60000         # 抓重复/翻倍即超
HEADER = ["中文名", "英文名", "等级", "总排名", "等级内序号", "海克斯名称"]


def normalize_en(name: str) -> str:
    """英文名归一（小写去空格撇号）：champions.json ↔ 站点 alias 的连接键"""
    return name.lower().replace(" ", "").replace("'", "").replace("’", "")


def parse_catalog(catalog: dict, min_entries: int = 100) -> dict:
    """海克斯目录 → {augment_id(str): (中文名, 等级)}；缺失字段即抛错（schema 漂移要响）
    min_entries：目录规模下限（当前 247 条；烟测传小值单测纯变换）"""
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError("海克斯目录结构异常（非 dict 或为空）")
    out = {}
    for aid, ent in catalog.items():
        if not isinstance(ent, dict):
            continue
        name = ent.get("displayName")
        rarity = ent.get("rarity")
        if not isinstance(name, str) or not name.strip() or rarity not in RARITY_TIER:
            continue
        out[str(aid)] = (name.strip(), RARITY_TIER[rarity])
    if len(out) < min_entries:   # 目录瘸腿说明改版，硬失败保旧文件
        raise ValueError(f"海克斯目录仅解析出 {len(out)} 条（预期 {min_entries}+），疑似改版")
    return out


def parse_champion_detail(detail: dict, catalog: dict) -> list:
    """单个 champion-details JSON → [(总排名, 等级, 海克斯名)]（按总排名升序）。

    - 内层 championAugments 为 [id, JSON字符串] 二元组，与站内 JS 同解法；
    - rank 必须稠密 1..total（上游自报 total 自检，防半截数据入库）；
    - 目录缺名条目跳过并告警（kb 侧本就不入未知名），超 1% 即抛错。
    """
    ca = (detail or {}).get("championAugments")
    # 条目 = [championId, augments_json_str, game_patch, 生成日期]（后两元素可缺）
    if not (isinstance(ca, list) and ca and isinstance(ca[0], list)
            and len(ca[0]) >= 2 and isinstance(ca[0][1], str)):
        raise ValueError("championAugments 结构异常（预期 [[id, json_str, …]]）")
    inner = json.loads(ca[0][1])
    augs = inner.get("augments")
    if not isinstance(augs, dict) or not augs:
        raise ValueError("augments 为空/缺失")
    rows, skipped = [], 0
    for aid, st in augs.items():
        cent = catalog.get(str(aid))
        if cent is None:
            skipped += 1
            continue
        try:
            rank = int(st["rank"])
            total = int(st.get("total", 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"rank/total 字段异常: {aid} -> {st}")
        rows.append((rank, cent[1], cent[0]))
    if skipped * 100 > len(augs):   # 目录缺名 >1%：catalog 与 detail 脱钩，硬失败
        raise ValueError(f"目录缺名 {skipped}/{len(augs)} 条，catalog 与 detail 脱钩")
    rows.sort(key=lambda r: r[0])
    total_self = int(next(iter(augs.values())).get("total", 0))
    if total_self and [r for r, _, _ in rows] != list(range(1, total_self + 1)):
        raise ValueError(f"rank 非 1..{total_self} 稠密（半截数据，宁旧勿坏）")
    return rows


def rows_for_champion(hero_cn: str, hero_en: str, parsed: list) -> list:
    """解析结果 → CSV 行（等级内序号 = 同稀有度内按总排名序的 1 基序号）"""
    out, counters = [], {}
    for rank, tier, name in parsed:
        counters[tier] = counters.get(tier, 0) + 1
        out.append([hero_cn, hero_en, tier, rank, counters[tier], name])
    return out


def validate_rows(rows: list, champs: int):
    """全局校验门槛（宁旧勿坏）：不过即抛 ValueError，调用方不落盘。"""
    if champs < MIN_CHAMPIONS:
        raise ValueError(f"英雄覆盖 {champs} < {MIN_CHAMPIONS}，疑似抓取半途而废")
    if not (MIN_ROWS <= len(rows) <= MAX_ROWS):
        raise ValueError(f"行数 {len(rows)} 越界（[{MIN_ROWS}, {MAX_ROWS}]）")
    tiers = {r[2] for r in rows}
    if tiers != set(RARITY_TIER.values()):
        raise ValueError(f"等级值域异常: {tiers}（预期 棱彩/黄金/白银 三档齐）")
    bad = [r for r in rows if not r[5] or "？？？" in r[5]]
    if bad:
        raise ValueError(f"占位/空名 {len(bad)} 行混入（如 {bad[0][5]!r}）")


def atomic_write_csv(rows: list, out_path: Path):
    """先写临时文件再原子替换（BOM + CRLF，与既有资产字节风格一致）"""
    tmp = out_path.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    tmp.replace(out_path)


def get_json(sess: requests.Session, url: str, cache_path: Path, force: bool):
    """当日缓存优先；无缓存则联网（重试+退避）并落缓存"""
    if not force and cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")), True
        except Exception:  # noqa: BLE001 —— 缓存坏了当无缓存重抓
            pass
    last = None
    for attempt in range(RETRIES):
        try:
            r = sess.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(r.text, encoding="utf-8")
            return data, False
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def fetch_detail(sess: requests.Session, cid: str, cache_dir: Path, force: bool):
    """每英雄统计：CDN 优先，同源 /data/ 回退（站方自带的二级源）"""
    cache_path = cache_dir / f"{cid}.json"
    if not force and cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8")), True
        except Exception:  # noqa: BLE001
            pass
    last = None
    for url in (f"{CDN}/champion-details/{cid}.json",
                f"{SITE}/data/champion-details/{cid}.json"):
        try:
            r = sess.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(r.text, encoding="utf-8")
            return data, False
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"champion-details/{cid} 两级源均失败: {last}")


def detail_meta(detail: dict) -> tuple:
    """条目级元信息 → (patch, 数据日期)；缺省空串"""
    ca = (detail or {}).get("championAugments") or []
    if ca and isinstance(ca[0], list) and len(ca[0]) >= 4:
        return str(ca[0][2] or ""), str(ca[0][3] or "")
    return "", ""


def main():
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    out = DATA_DIR / "hero_augments.csv"
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    today = datetime.date.today().strftime("%Y%m%d")
    cache_dir = DATA_DIR / "cache" / "augments" / today

    cn_to_en = json.loads((DATA_DIR / "champions.json").read_text(encoding="utf-8"))
    en_to_cn = {normalize_en(en): cn for cn, en in cn_to_en.items()}

    sess = requests.Session()
    sess.headers.update(HEADERS)

    names, _ = get_json(sess, NAMES_URL, cache_dir / "names-i18n.json", force)
    catalog, _ = get_json(sess, CATALOG_URL, cache_dir / "catalog.json", force)
    catalog = parse_catalog(catalog)

    # 站点英雄 → champions.json 规范中文名（join 不上的站点英雄跳过并告警）
    site_champs = []
    for cid, ent in names.items():
        alias = (ent or {}).get("alias") or ""
        cn = en_to_cn.get(normalize_en(alias))
        if cn is None:
            print(f"[WARN] 站点英雄 {alias}({cid}) 不在 champions.json，跳过")
            continue
        site_champs.append((int(ent["id"]), cid, cn, cn_to_en[cn]))
    site_champs.sort(key=lambda t: t[3])   # 按英文名字母序，与旧资产排序一致

    all_rows, covered, failed, stamps, patches = [], 0, [], set(), set()
    for i, (_id, cid, cn, en) in enumerate(site_champs, 1):
        try:
            detail, hit = fetch_detail(sess, cid, cache_dir / "champion-details", force)
            parsed = parse_champion_detail(detail, catalog)
        except Exception as e:  # noqa: BLE001 —— 单英雄失败即整体失败（宁旧勿坏）
            failed.append((cn, str(e)))
            print(f"[{i}/{len(site_champs)}] [FAIL] {cn}: {e}", flush=True)
            continue
        if not hit:
            time.sleep(DETAIL_SLEEP)
        all_rows.extend(rows_for_champion(cn, en, parsed))
        covered += 1
        patch, gen_date = detail_meta(detail)
        if patch:
            patches.add(patch)
        inner = json.loads(detail["championAugments"][0][1])
        if inner.get("augments"):
            any_st = next(iter(inner["augments"].values()))
            if any_st.get("win_rate_date"):
                stamps.add(any_st["win_rate_date"])
        if i % 20 == 0 or i == len(site_champs):
            print(f"[{i}/{len(site_champs)}] 已解析 {covered} 英雄 / {len(all_rows)} 行",
                  flush=True)

    if failed:
        print(f"\n[ABORT] {len(failed)} 英雄抓取失败（首列: "
              f"{[c for c, _ in failed[:5]]}）——保留旧 CSV 不动，稍后重跑续爬")
        sys.exit(1)

    validate_rows(all_rows, covered)

    print(f"\n校验通过: {covered} 英雄 / {len(all_rows)} 行 / 等级值域 "
          f"棱彩·黄金·白银 / 上游 patch {sorted(patches) or '-'} "
          f"/ 数据日期 {sorted(stamps) or '-'}")
    if dry:
        print("[DRY-RUN] 不落盘")
        return
    atomic_write_csv(all_rows, out)
    print(f"[OK] 已原子替换 {out}")


if __name__ == "__main__":
    main()
