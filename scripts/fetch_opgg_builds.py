#!/usr/bin/env python
"""OP.GG aram-mayhem 出装/加点抓取（P0.5 ground truth + P1 builds 数据资产）

技术路线（实测验证）：OP.GG 是 Next.js App Router，SSR HTML 的 RSC flight
流（self.__next_f.push）内含全部表格数据 —— 纯 HTTP 可拿，无需 Selenium/浏览器。
build 页含 starter_items/boots/core_items 表格行（alt=装备名序列），
skills 页含 QWER 加点序列（1-18 级顺序，第一个为主流方案）。

用法:
    uv run python scripts/fetch_opgg_builds.py                # P0.5 三英雄
    uv run python scripts/fetch_opgg_builds.py --all          # 全量 173 英雄
    uv run python scripts/fetch_opgg_builds.py --all --force  # 强制重抓（忽略当日缓存）
    uv run python scripts/fetch_opgg_builds.py --hero amumu --force  # 单英雄定向刷新

特性：断点续爬（当日已有即跳过）、失败重试+退避、连续失败 5 次中止防限流、
全量完成后汇总 data/builds/builds.csv。
输出: experiments/p05_data/{slug}.json（三英雄） 或 data/builds/{slug}.json（全量）
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


def extract_item_alts(seg: str) -> list:
    """按 metaType 过滤：只保留 metaType="item" 的图标 alt（排除技能/海克斯图标）"""
    out = []
    for part in seg.split('"metaType":"')[1:]:
        mtype = part[:part.find('"')]
        m = re.search(r'alt":"([^"]+)"', part)
        if m and mtype == "item":
            out.append(m.group(1))
    return out


def table_rows(flight: str, prefix: str) -> dict:
    """提取表格行 {row_id: [装备名...]}；行边界 = 下一个 ["$","tr" 出现处"""
    rows = {}
    for m in re.finditer(re.escape('["$","tr","') + prefix, flight):
        rid_end = flight.find('"', m.end())
        rid = prefix + flight[m.end():rid_end]
        seg_end = flight.find('["$","tr"', m.end())
        seg = flight[m.end():seg_end if seg_end > 0 else m.end() + 6000]
        rows.setdefault(rid, extract_item_alts(seg))
    return rows


def parse_build_page(flight: str) -> dict:
    out = {}
    out["starter_items"] = [v for k, v in sorted(table_rows(flight, "starter_items").items())]
    out["boots"] = [v for k, v in sorted(table_rows(flight, "boots").items())]
    out["core_builds"] = [v for k, v in sorted(table_rows(flight, "core_items").items())]
    return out


def parse_skills_page(flight: str) -> dict:
    """加点序列提取。两种渲染形态：
    ① JSON 数组（含进化型英雄的 "R-E" 复合标记，如卡兹奇/螳螂系）
    ② 连续字母序列（旧渲染兜底）
    统一存列表形式：["Q","W","E","Q","Q","R-E",...]"""
    orders = []
    for m in re.finditer(r'\["Q"(?:,"[QWER](?:-[A-Z])?")+\]', flight):
        try:
            arr = json.loads(m.group())
            if len(arr) >= 15:
                orders.append(arr)
        except Exception:
            pass
    if not orders:
        for s in re.findall(r'(?:["\'?]*[QWER]["\'?,\]\[]+){10,}', flight):
            letters = re.findall(r'[QWER]', s)
            if len(letters) >= 15:
                orders.append(list("".join(letters)))
    # 技能名映射（data 里 key->name）
    skill_names = dict(re.findall(
        r'\{"key":"([QWER])","name":"([^"]+)"', flight))
    return {"skill_orders": orders, "skill_names": skill_names}


def skill_priority(order) -> str:
    """从加点序列推导 主X副Y（谁先到 5 级谁主）；"R-E" 复合标记按 base 技能计"""
    def fifth_at(skill):
        count = 0
        for i, ch in enumerate(order):
            base = str(ch).split("-")[0]
            if base == skill:
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
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    fetch_all = "--all" in sys.argv
    force = "--force" in sys.argv
    hero = None
    if "--hero" in sys.argv:
        _i = sys.argv.index("--hero")
        hero = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None
        args = [a for a in args if a != hero]   # 防误当 P0.5 三英雄 slug 抓错目录
    cn_to_en = json.loads((DATA_DIR / "champions.json").read_text(encoding="utf-8"))
    if fetch_all:
        slugs = sorted({en.lower().replace(" ", "") for en in cn_to_en.values()})
        out_dir = DATA_DIR / "builds"      # 全量正式资产：data/builds/{slug}.json
    elif hero:
        # 单英雄定向刷新：复用全量正式资产目录（版本检查/抓取日期随单文件更新）
        slugs = [hero.strip().lower().replace(" ", "")]
        out_dir = DATA_DIR / "builds"
    else:
        slugs = args or ["aatrox", "annie", "jinx"]
        out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    ok, skip, failed = 0, 0, []
    for i, slug in enumerate(slugs, 1):
        path = out_dir / f"{slug}.json"
        if path.exists() and not force:
            # 断点续爬：当日已有数据则跳过
            try:
                if json.loads(path.read_text(encoding="utf-8")).get(
                        "fetched_at") == datetime.date.today().isoformat():
                    skip += 1
                    continue
            except Exception:
                pass
        print(f"[{i}/{len(slugs)}] {slug} ...", flush=True)
        last_err = None
        for attempt in range(3):  # 失败重试 + 退避
            try:
                data = fetch_hero(slug, cn_to_en)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")
                ok += 1
                core0 = data["core_builds"][0] if data["core_builds"] else []
                print(f"[OK] {slug} | patch={data['patch']} | "
                      f"starter={data['starter_items'][:1]} | core0={core0} "
                      f"| skill={data.get('skill_priority', '?')}", flush=True)
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(3 * (attempt + 1))
        if last_err is not None:
            failed.append((slug, str(last_err)))
            print(f"[FAIL] {slug}: {last_err}", flush=True)
            if len(failed) >= 5:
                print("[ABORT] 连续失败过多，疑似被限流 —— 稍后重跑续爬", flush=True)
                break
        else:
            failed.clear()  # 成功即重置连续失败计数
        time.sleep(1.5)

    if fetch_all:
        write_summary_csv(out_dir)
    print(f"\n完成: ok={ok} skip={skip} fail={len(failed)} / {len(slugs)}")
    if failed:
        print("失败清单:", [s for s, _ in failed])
    sys.exit(0 if not failed else 1)


def write_summary_csv(out_dir: Path):
    """全量汇总 builds.csv：每英雄一行（首套方案 + 加点），规则引擎 P1 消费"""
    import csv
    cn_to_en = json.loads((DATA_DIR / "champions.json").read_text(encoding="utf-8"))
    rows = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append({
            "中文名": d.get("hero_cn", ""),
            "英文名": cn_to_en.get(d.get("hero_cn", ""), d.get("slug", "")),
            "版本": d.get("patch", ""),
            "抓取日期": d.get("fetched_at", ""),
            "加点顺序": d.get("skill_priority", ""),
            "加点序列": "|".join(".".join(x) for x in d.get("skill_orders", [])[:2]),
            "出门装1": "+".join(d["starter_items"][0]) if d.get("starter_items") else "",
            "出门装2": "+".join(d["starter_items"][1]) if len(d.get("starter_items", [])) > 1 else "",
            "鞋子1": "+".join(d["boots"][0]) if d.get("boots") else "",
            "鞋子2": "+".join(d["boots"][1]) if len(d.get("boots", [])) > 1 else "",
            "核心装1": "+".join(d["core_builds"][0]) if d.get("core_builds") else "",
            "核心装2": "+".join(d["core_builds"][1]) if len(d.get("core_builds", [])) > 1 else "",
            "方案总数": len(d.get("core_builds", [])),
        })
    csv_path = out_dir / "builds.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["中文名"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"[OK] 汇总 {len(rows)} 英雄 -> {csv_path}")


if __name__ == "__main__":
    main()
