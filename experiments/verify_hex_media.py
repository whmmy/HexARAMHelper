"""门禁验证：真实素材上的海克斯识别全管线（离线，无需游戏/标定文件）。

对 media/image.png 与 media/analysis/frames/ 关键帧（≤10 帧）逐帧跑：
模板预筛分 → 标题条 OCR 命中 → 卡片矩形 OCR（含名行优选）→ 分数打印；
并验证 检测状态机 → on_cards 发射门 → GameSession 评级/建议 链路。

核心断言（不满足 exit 1）：
1. image.png 三卡全部命中有效库名（分数 ≥80）；
2. 评级与建议可生成（rank_augment_cards → 3 条评级；record_augment → 建议）；
3. 布局几何正确（cards 覆盖卡名行 0.398~0.427H；rerolls 3 个且全部在屏内）；
4. DetectorCore 稳定 2 帧 → on_cards 载荷过 ≥2 有效名发射门；
5. 参考三卡帧（窗口 A/B 各一）三卡全中。

运行: uv run python experiments/verify_hex_media.py
退出码：0=全部通过；1=断言失败；2=素材缺失跳过（media/ 为 gitignore 的
本地素材，干净检出上无物可验，按约定显式跳过而非空洞通过）。
标定文件缺失时自动用内置默认布局（2560x1440 真机实测值），同样必须全过。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2

from hexa.core.game_state import GameSession
from hexa.core.rules_engine import RulesEngine
from hexa.core.screen_detect import (CALIB_DIR, DetectorCore, Layout,
                                     ScreenDetector, load_layout)
from hexa.core.ocr_engine import OcrEngine
from hexa.knowledge.kb import KnowledgeBase

W, H = 2560, 1440
FAILED = []


def check(name, cond, detail=""):
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main():
    t0 = time.time()
    # 素材前置检查：缺失则显式跳过（exit 2），不做空洞验证
    image_path = ROOT / "media" / "image.png"
    frames_dir = ROOT / "media" / "analysis" / "frames"
    if not image_path.exists() or not frames_dir.is_dir():
        print(f"[SKIP] 素材缺失（media/ 为本地 gitignore 数据）: {image_path}")
        print("[SKIP] 本脚本只在有真实素材的工作区运行；缺素材时无物可验")
        sys.exit(2)
    FRAMES = sorted(frames_dir.glob("*.png"))[:10]
    if not FRAMES:
        print(f"[SKIP] 关键帧目录为空: {frames_dir}")
        sys.exit(2)

    kb = KnowledgeBase()
    ocr = OcrEngine(kb)
    layout = load_layout(W, H)
    det = ScreenDetector(kb, ocr, layout=layout)
    print(f"== 布局来源: {layout.source} ==")
    px = layout.to_pixels(W, H)
    print(f"cards={px['cards']}\nrerolls={px['rerolls']}")

    # ── 断言 3：布局几何（本次修复的根因位） ─────────────────────
    print("\n[布局几何]")
    check("cards 覆盖卡名行(0.398~0.427H)",
          all(r[1] <= 0.396 * H and r[1] + r[3] >= 0.43 * H
              for r in px["cards"]), str(px["cards"]))
    rr = px["rerolls"]
    check("rerolls 三个且全部在屏内(x/y)",
          len(rr) == 3 and all(0 <= r[0] and r[0] + r[2] <= W
                               and 0 <= r[1] and r[1] + r[3] <= H
                               and r[2] > 0 for r in rr), str(rr))
    check("rerolls 位于卡下方(0.68~0.73H)",
          all(0.66 * H <= r[1] and r[1] + r[3] <= 0.76 * H for r in rr), str(rr))

    # 模板预筛（npz 缺失时跳过，不阻断——新装环境无模板）
    tpl = None
    tp = CALIB_DIR / f"{W}x{H}_tpl.npz"
    if tp.exists():
        import numpy
        data = numpy.load(tp)
        tpl = [data[f"t{i}"] for i in range(3)]
        det._tpl = tpl
    else:
        print("  （无模板 npz，跳过预筛分——非阻断）")

    def recognize_frame(frame, tag):
        """完整管线一步：模板分 + 标题命中 + 卡片矩形 OCR（名行优选+分数）"""
        sh, sw = frame.shape[:2]
        regions = layout.to_pixels(sw, sh)
        strips = [det._crop(frame, b) for b in regions["title_bands"]]
        tscore = det._template_score(strips) if tpl else float("nan")
        hits, titles = det._ocr_title_strips(strips)
        cards, scores = [], []
        for rect in regions["cards"]:
            crop = det._crop(frame, rect)
            found = ocr.match_augments_in(ocr.recognize(crop))
            if not found:
                cards.append(None)
                scores.append(0.0)
                continue
            ch = crop.shape[0] or 1
            zone = [h for h in found
                    if det.NAME_ZONE[0] * ch <= h[2][1] + h[2][3] / 2
                    <= det.NAME_ZONE[1] * ch]
            name, mscore, _ = (zone or found)[0]
            cards.append(name)
            scores.append(mscore)
        print(f"{tag}: tpl={tscore:.3f} hits={hits} titles={titles}")
        print(f"    cards={cards} scores={[f'{s:.0f}' for s in scores]}")
        return tscore, hits, cards, scores

    # ── 断言 1/5：逐帧识别（image.png + ≤10 关键帧） ─────────────
    print("\n[逐帧识别]")
    image_path = ROOT / "media" / "image.png"
    img = cv2.imread(str(image_path))
    if img is None:
        check("素材存在 media/image.png", False, str(image_path))
        print(f"\n失败: {FAILED}")
        sys.exit(1)
    _ts, _h, img_cards, img_scores = recognize_frame(img, "image.png      ")
    check("image.png 三卡全中有效库名",
          all(n and kb.valid_augment(n) for n in img_cards), str(img_cards))
    check("image.png 命中分 ≥80", all(s >= 80 for s in img_scores),
          str(img_scores))

    frame_cards = {}
    for f in FRAMES:
        fr = cv2.imread(str(f))
        if fr is None:
            print(f"  （跳过缺失帧 {f.name}）")
            continue
        _ts, _h, cards, _s = recognize_frame(fr, f"{f.name}: "[:29].ljust(29))
        frame_cards[f.name] = cards

    n_good = sum(1 for c in frame_cards.values()
                 if sum(1 for n in c if n and kb.valid_augment(n)) >= 2)
    check(f"关键帧 ≥8/10 至少 2 卡有效（实测 {n_good}/{len(frame_cards)}）",
          n_good >= 8, str(frame_cards))
    # 参考帧必须存在且三卡全中（缺帧=失败，不允许空洞通过）
    offer_a = [c for k, c in frame_cards.items() if "sec041" in k]
    offer_b = [c for k, c in frame_cards.items() if "sec262" in k]
    check("窗口A参考帧(sec041) 存在且三卡全中",
          bool(offer_a) and all(n and kb.valid_augment(n) for n in offer_a[0]),
          str(offer_a))
    check("窗口B参考帧(sec262) 存在且三卡全中",
          bool(offer_b) and all(n and kb.valid_augment(n) for n in offer_b[0]),
          str(offer_b))

    # ── 断言 4：DetectorCore 稳定 → 发射门 ────────────────────────
    print("\n[状态机 → 发射门]")
    core = DetectorCore()
    core.feed_titles(2)
    core.feed_titles(3)                      # 连续 2 帧命中 → active
    core.feed_cards(img_cards)
    check("单帧不稳定", core.stable_cards is None)
    core.feed_cards(img_cards)
    stable = core.consume_cards()
    check("稳定 2 帧产出 on_cards 载荷", stable == img_cards, str(stable))
    check("载荷过 ≥2 有效名发射门",
              bool(stable) and det.count_valid_cards(stable) >= 2, str(stable))
    reemitted = 0
    for _ in range(12):                      # 闩锁：同内容不重发
        core.feed_cards(img_cards)
        if core.consume_cards() is not None:
            reemitted += 1
    check("闩锁：同内容 12 帧不重发", reemitted == 0, f"reemitted={reemitted}")

    # ── 断言 2：评级与建议生成（检测 → game_state → 建议 链路） ──
    print("\n[评级/建议链路]")
    session = GameSession(kb, RulesEngine(kb))
    session.on_champ_select({"ally": ["暗裔剑魔"], "enemy": [],
                             "my_hero": "暗裔剑魔", "source": "verify"})
    session.on_game_start({"ally": ["暗裔剑魔", "九尾妖狐", "皎月女神", "盲僧",
                                    "众星之子"],
                           "enemy": ["黑暗之女", "狂暴之心", "放逐之刃",
                                     "诺克萨斯之手", "琴瑟仙女"],
                           "my_hero": "暗裔剑魔", "source": "verify"})
    session.on_in_progress()
    ranks = session.rank_augment_cards(img_cards)
    check("rank_augment_cards 出 3 条评级",
          len(ranks) == 3 and all(r.get("grade") in "SABC?" for r in ranks),
          str(ranks))
    check("overlay 时刻块数据就绪(card_ranks)",
          len(session.ui_state().get("card_ranks") or []) == 3)
    first_valid = next(n for n in img_cards if n and kb.valid_augment(n))
    check("record_augment 记账成功", session.record_augment(first_valid))
    check("记账后生成建议(opening)",
          session.suggestion is not None and session.suggestion.kind == "opening",
          str(session.suggestion))
    st = session.ui_state()
    check("建议文本可渲染进悬浮窗", bool(st.get("suggestion")))

    dt = time.time() - t0
    print(f"\n{'='*40}\n耗时 {dt:.0f}s | 失败 {len(FAILED)}")
    if FAILED:
        print("失败项:", FAILED)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
