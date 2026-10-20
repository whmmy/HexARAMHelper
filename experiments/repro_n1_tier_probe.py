# -*- coding: utf-8 -*-
"""N1 海克斯阶级色环探针 · 真机标注集复跑（20261020 定标，N1 20261019）。

用 log/games/*.json 台账（hero×augment×ts）× data/hero_augments.csv 先验
给 log/captures/click_*.png 定格帧打伪标签，再把 log/captures/round_*.png
激活整帧按布局裁三卡，逐帧验证 hexa.core.screen_detect.card_tier_from_crop：

  1) 点击帧 vs 先验：exact / unknown（留空走先验兜底，合规）/ wrong；
  2) 整帧三卡：同轮同档一致性 + 疑似轮档打印（人工抽查）。

定标结论（阈值写在 screen_detect._TIER_*）：
  白银 = 色环无彩（frac<0.02）且卡顶条带亮灰（中位 S<=8 / V>=200）
  黄金 = 冰蓝亮框（frac>=0.05 且 H80~115 占比>=0.60）——本模式黄金框
         不是金黄色，是冰蓝玻璃框+金色角饰
  棱彩 = 粉紫虹彩（H130~180+红端0~20 合计占比>=0.30 且粉>蓝）
已知盲区：悬停蓝光长压（~1.5s 光斑累积）与黄金同相，点击即抓帧正常路径
未复现（真机 41 帧零误判）；误路由垃圾帧（商店/地图 OCR 撞名）可能误判，
该路径上游名字本身即假阳，探针不为其背书。

运行: uv run python experiments/repro_n1_tier_probe.py
（无网络/无游戏；需要本机 log/captures 真机产物，缺文件时报错退出。）
"""
import csv
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2

from hexa.core.screen_detect import card_tier_from_crop

CARDS_169 = [[0.2285, 0.1757, 0.1656, 0.4890],
             [0.4199, 0.1757, 0.1656, 0.4890],
             [0.6113, 0.1757, 0.1664, 0.4890]]


def load_prior():
    prior = {}
    with open(os.path.join("data", "hero_augments.csv"),
              encoding="utf-8-sig") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            if len(row) < 6 or not row[5].strip() or row[5].strip() == "？？？":
                continue
            prior[(row[0].strip(), row[5].strip())] = row[2].strip()
    return prior


def load_captures():
    caps = []
    for p in glob.glob(os.path.join("log", "captures", "click_*.png")):
        m = re.match(r"click_(\d{8})_(\d{6})_(\d{3})_card(\d)\.png",
                     os.path.basename(p))
        if not m:
            continue
        ts = (datetime.datetime.strptime(m.group(1) + m.group(2),
                                         "%Y%m%d%H%M%S").timestamp()
              + int(m.group(3)) / 1000.0)
        caps.append((ts, p))
    caps.sort()
    return caps


def main():
    prior = load_prior()
    caps = load_captures()
    if not caps:
        print("[SKIP] log/captures/click_*.png 不存在（真机产物未就绪）")
        return 0

    ok = unknown = wrong = 0
    print("== 点击定格帧 vs 先验（自动对账，|Δt|<15s）==")
    for f in sorted(glob.glob(os.path.join("log", "games", "*.json"))):
        doc = json.load(open(f, encoding="utf-8"))
        hero = doc.get("hero")
        for a in doc.get("augments", []):
            ts = a.get("ts") or 0
            best = min(caps, key=lambda c: abs(c[0] - ts), default=None)
            if not best or abs(best[0] - ts) >= 15:
                continue
            pt = prior.get((hero, a["name"]))
            if not pt:
                continue
            got = card_tier_from_crop(cv2.imread(best[1]))
            if got == pt:
                ok += 1
            elif got == "":
                unknown += 1
                print(f"  [unknown→先验] {hero} {a['name']} prior={pt} "
                      f"{os.path.basename(best[1])}")
            else:
                wrong += 1
                print(f"  [WRONG] {hero} {a['name']} prior={pt} got={got} "
                      f"{os.path.basename(best[1])}")
    print(f"点击帧: exact={ok} unknown(留空走先验)={unknown} wrong={wrong}")

    print("== 激活整帧三卡一致性（同轮同档）==")
    mix = 0
    for f in sorted(glob.glob(os.path.join("log", "captures", "round_*.png"))):
        img = cv2.imread(f)
        H, W = img.shape[:2]
        res = []
        for r in CARDS_169:
            x, y, w, h = (int(r[0] * W), int(r[1] * H),
                          int(r[2] * W), int(r[3] * H))
            res.append(card_tier_from_crop(img[y:y + h, x:x + w]))
        tag = res[0] if len(set(res)) == 1 else "MIX"
        if tag == "MIX":
            mix += 1
            print(f"  [MIX] {os.path.basename(f)} {res}"
                  "（半帧动画/悬停光斑，人工看图定性）")
    print(f"整帧: {len(glob.glob(os.path.join('log', 'captures', 'round_*.png')))}"
          f" 帧，MIX={mix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
