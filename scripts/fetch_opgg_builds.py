#!/usr/bin/env python
"""OP.GG aram-mayhem 出装/加点抓取（P0.5 ground truth + P1 爬虫前哨）

技术路线（实测验证）：OP.GG 是 Next.js App Router，SSR HTML 的 RSC flight
流（self.__next_f.push）内含全部表格数据 —— 纯 HTTP 可拿，无需 Selenium/浏览器。
build 页含 starter_items/boots/core_items 表格行（alt=装备名序列），
skills 页含 QWER 加点序列（1-18 级顺序，第一个为主流方案）。

用法: uv run python scripts/fetch_opgg_builds.py [slug ...]
默认抓 p05 三英雄: aatrox annie jinx
输出: experiments/p05_data/{slug}.json
"""
import json
import re
import sys
import datetime
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))
from hexa.config import DATA_DIR

OUT_DIR = ROOT / "experiments" / "p05_data"
BASE = "https://op.gg/zh-cn/lol/modes/aram-mayhem"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}


def fetch_flight(url: str) -> str:
    """抓页面并拼接 RSC flight 流（JSON 兼容反转义）"""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', html, re.S)
    if not chunks:
        raise RuntimeError(f"页面无 RSC flight 数据（改版或被反爬）: {url}")
    return "".join(json.loads('"' + c + '"') for c in chunks)


def table_rows(flight: str, prefix: str) -> dict:
    """提取表格行 {row_id: [装备名...]}；行边界 = 下一个 ["$","tr" 出现处"""
    rows = {}
    for m in re.finditer(re.escape('["$","tr","') + prefix, flight):
        rid_end = flight.find('"', m.end())
        rid = prefix + flight[m.end():rid_end]
        seg_end = flight.find('["$","tr"', m.end())
        seg = flight[m.end():seg_end if seg_end > 0 else m.end() + 6000]
        alts = re.findall(r'alt":"([^"]+)"', seg)
        rows.setdefault(rid, alts)
    return rows


def parse_build_page(flight: str) -> dict:
    out = {}
    out["starter_items"] = [v for k, v in sorted(table_rows(flight, "starter_items").items())]
    out["boots"] = [v for k, v in sorted(table_rows(flight, "boots").items())]
    out["core_builds"] = [v for k, v in sorted(table_rows(flight, "core_items").items())]
    return out


def parse_skills_page(flight: str) -> dict:
    # QWER 加点序列：连续的技能字母（格子渲染顺序即加点顺序）
    seqs = re.findall(r'(?:["\'?]*[QWER]["\'?,\]\[]+){10,}', flight)
    orders = []
    for s in seqs:
        letters = re.findall(r'[QWER]', s)
        if len(letters) >= 15:  # 18 级完整序列（R 占 6 级位时字母数>=15）
            orders.append("".join(letters))
    # 技能名映射（data 里 key->name）
    skill_names = dict(re.findall(
        r'\{"key":"([QWER])","name":"([^"]+)"', flight))
    return {"skill_orders": orders, "skill_names": skill_names}


def skill_priority(order: str) -> str:
    """从加点序列推导 主X副Y（谁先到 5 级谁主）"""
    def fifth_at(skill):
        count = 0
        for i, ch in enumerate(order):
            if ch == skill:
                count += 1
                if count == 5:
                    return i
        return 999
    ranks = sorted("QWE", key=lambda s: fifth_at(s))
    return f"主{ranks[0]}副{ranks[1]}"


def extract_patch(flight: str) -> str:
    m = re.search(r'(\d{2}\.\d{1,2})\s*版本', flight)
    return m.group(1) if m else ""


def fetch_hero(slug: str, cn_map: dict) -> dict:
    print(f"--- 抓取 {slug} ---")
    build_flight = fetch_flight(f"{BASE}/{slug}/build")
    time.sleep(1.2)
    skills_flight = fetch_flight(f"{BASE}/{slug}/skills")

    data = {
        "slug": slug,
        "fetched_at": datetime.date.today().isoformat(),
        "url_build": f"{BASE}/{slug}/build",
    }
    data["patch"] = extract_patch(build_flight) or extract_patch(skills_flight)
    data.update(parse_build_page(build_flight))
    skills = parse_skills_page(skills_flight)
    data["skill_orders"] = skills["skill_orders"][:3]
    data["skill_names"] = skills["skill_names"]
    if data["skill_orders"]:
        data["skill_priority"] = skill_priority(data["skill_orders"][0])
    # 英雄中文名：slug（小写英文名）反查 champions.json，最可靠
    hero_cn = next((cn for cn, en in cn_map.items()
                    if en.lower().replace(" ", "") == slug.lower()), None)
    data["hero_cn"] = hero_cn or slug
    return data


def main():
    slugs = sys.argv[1:] or ["aatrox", "annie", "jinx"]
    cn_to_en = json.loads((DATA_DIR / "champions.json").read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for slug in slugs:
        try:
            data = fetch_hero(slug, cn_to_en)
            path = OUT_DIR / f"{slug}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            ok += 1
            print(f"[OK] {slug} -> {path.name} | patch={data['patch']} "
                  f"| starter={data['starter_items'][:1]} "
                  f"| core0={data['core_builds'][0] if data['core_builds'] else []} "
                  f"| skill={data.get('skill_priority', '?')}")
            time.sleep(1.5)
        except Exception as e:
            print(f"[FAIL] {slug}: {e}")
    sys.exit(0 if ok == len(slugs) else 1)


if __name__ == "__main__":
    main()
