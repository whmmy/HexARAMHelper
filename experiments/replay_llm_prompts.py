"""重放实验：用历史对局日志 + 真机海克斯卡片，按项目原路径实调双槽位大模型。

数据源：
- log/games/ 最近一局完整 5v5 日志（英雄/双方阵容/已拿海克斯/装备/路线/威胁）
- media/image.png OCR 出的真机三张海克斯卡片（海克斯符文选项）
- 知识库 OP.GG builds + 海克斯先验 top20（与局内一致）

流程与 AIDecisionEngine 完全一致：
1. System2（qwen3.8-max）生成 ChampionContext（原 system2.generate）
2. System1（deepseek-v4.1-flash）三连，各跑两个分支：
   - 现状分支：项目原样（deepseek 默认深度思考 → max_tokens 被烧光 → 降级规则）
   - 关思考分支：enable_thinking=False（仅本实验注入，展示 AI 实际能力）
每一步打印：完整 system/user 提示词 → 模型原始输出 → 玩家可见渲染文本。

运行: uv run python experiments/replay_llm_prompts.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hexa.config import load_dotenv  # noqa: E402
load_dotenv()

import cv2  # noqa: E402

from hexa.ai import system1, system2  # noqa: E402
from hexa.ai.client import LLMClient, UsageStats  # noqa: E402
from hexa.config import Settings  # noqa: E402
from hexa.core.game_state import Candidate, Suggestion  # noqa: E402
from hexa.core.rules_engine import RulesEngine, ThreatProfile  # noqa: E402
from hexa.core.screen_detect import ScreenDetector, load_layout  # noqa: E402
from hexa.core.ocr_engine import OcrEngine  # noqa: E402
from hexa.knowledge.kb import KnowledgeBase  # noqa: E402

W, H = 2560, 1440
REAL_CARDS = ["藏身草丛", "术士果汁盒", "虚幻武器"]  # media/image.png 真机 OCR 兜底值


class CapturingClient:
    """包装 LLMClient：原样转发 chat()，同时截留每一发的完整提示词与原始输出"""

    def __init__(self, inner: LLMClient, log: list):
        self.inner = inner
        self.log = log
        self.usage = inner.usage
        self.last_raw = ""

    @property
    def configured(self):
        return self.inner.configured

    def chat(self, system, user, max_tokens=None, json_mode=True):
        self.log.append({"slot": self.inner.slot, "system": system, "user": user,
                         "max_tokens": max_tokens})
        text, meta = self.inner.chat(system, user, max_tokens=max_tokens,
                                     json_mode=json_mode)
        self.last_raw = text
        print(f"  ({self.inner.slot} → {meta['model']} "
              f"latency={meta['latency']}s tokens={meta['tokens_in']}/{meta['tokens_out']})")
        return text, meta


class NoThinkLLM(LLMClient):
    """实验对照：所有模型强制 enable_thinking=False。

    依据：deepseek-v4.1-flash 在 dashscope 上默认深度思考，长输入下思考
    token 烧光 System1 的小 max_tokens 预算（260~400）→ content 为空 →
    生产路径全部静默降级规则版。qwen3 系已有同款处理（client.py），此处
    仅在实验中把同等处理扩展到 deepseek，验证 AI 实际能力。
    """

    def _call_once(self, model, system, user, max_tokens, temperature,
                   json_mode) -> tuple[str, dict]:
        client = self._sdk_client()
        kwargs = {"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "temperature": temperature, "max_tokens": max_tokens,
                  "timeout": self._timeout()}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        kwargs["extra_body"] = {"enable_thinking": False}
        t0 = time.time()
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        usage = getattr(resp, "usage", None)
        tin = getattr(usage, "prompt_tokens", 0) or 0
        tout = getattr(usage, "completion_tokens", 0) or 0
        self.usage.add(tin, tout)
        return content, {"model": model, "latency": round(time.time() - t0, 2),
                         "tokens_in": tin, "tokens_out": tout}


def load_latest_log() -> dict:
    """最近一局完整 5v5 日志（中途启动的残缺局阵容只有 1 人，不适合重放）"""
    logs = sorted((ROOT / "log" / "games").glob("*.json"), key=lambda p: p.name)
    if not logs:
        sys.exit("log/games/ 无历史日志")
    full = [p for p in logs
            if len(json.loads(p.read_text(encoding="utf-8")).get("enemy") or []) >= 5]
    pick = full[-1] if full else logs[-1]
    return json.loads(pick.read_text(encoding="utf-8"))


def ocr_real_cards(kb, det_layout) -> list:
    """media/image.png 真机三卡片识别（缺素材时回退内置值）"""
    path = ROOT / "media" / "image.png"
    if not path.exists():
        return REAL_CARDS
    ocr = OcrEngine(kb)
    det = ScreenDetector(kb, ocr, layout=det_layout)
    img = cv2.imread(str(path))
    if img is None:
        return REAL_CARDS
    px = det_layout.to_pixels(img.shape[1], img.shape[0])
    cards = []
    for rect in px["cards"]:
        crop = det._crop(img, rect)
        found = ocr.match_augments_in(ocr.recognize(crop))
        if not found:
            cards.append(None)
            continue
        ch = crop.shape[0] or 1
        zone = [h for h in found
                if det.NAME_ZONE[0] * ch <= h[2][1] + h[2][3] / 2
                <= det.NAME_ZONE[1] * ch]
        cards.append((zone or found)[0][0])
    valid = [c for c in cards if c and kb.valid_augment(c)]
    return valid if len(valid) == 3 else REAL_CARDS


def threat_from_log(d: dict) -> ThreatProfile:
    """日志 threat 串（法伤×3 | 物伤×2 | 坦克×1 | 治疗:琴瑟仙女）→ 结构化"""
    t = ThreatProfile()
    for part in (d.get("threat") or "").split("|"):
        part = part.strip()
        if "×" in part:
            head, cnt = part.split("×", 1)
            n = int(cnt or 0)
            if head.startswith("法伤"):
                t.ap_count = n
            elif head.startswith("物伤"):
                t.ad_count = n
            elif head.startswith("坦克"):
                t.tank_count = n
        elif part.startswith("治疗:"):
            t.healers = [x for x in part[3:].split("/") if x]
    return t


def show_prompts(log, idx):
    e = log[idx]
    bar = "─" * 72
    print(f"\n{bar}\n>>> 实际发送 [{e['slot']}] max_tokens={e['max_tokens']}\n{bar}")
    print(f"--- system ---\n{e['system']}")
    print(f"--- user ---\n{e['user']}\n{bar}")


def render_opening(picks, first_augment):
    s = Suggestion(kind="opening", source="ai")
    s.candidates = [Candidate(p["name"], p["reason"]) for p in picks]
    s.note = f"首个海克斯【{first_augment}】已联动" if first_augment else ""
    return s.render()


def render_next(picks, kb, gold):
    s = Suggestion(kind="next_items", source="ai", gold=gold)
    s.candidates = [Candidate(p["name"], p["reason"],
                              price=kb.items.get(p["name"], {}).get("gold", 0))
                    for p in picks]
    return s.render()


def main():
    t0 = time.time()
    d = load_latest_log()
    hero = d["hero"]
    print(f"== 历史日志: {d.get('started_at')} 英雄={hero} ==")
    print(f"   我方: {'、'.join(d.get('ally') or [])}")
    print(f"   敌方: {'、'.join(d.get('enemy') or [])}")
    taken = [a["name"] for a in d.get("augments") or []]
    print(f"   已拿海克斯: {'、'.join(taken) or '无'}")
    print(f"   装备/金币/路线: {d.get('final_items')} / 4000G / {d.get('current_path')}")
    print(f"   威胁: {d.get('threat')}")

    kb = KnowledgeBase()
    rules = RulesEngine(kb)
    settings = Settings()
    usage = UsageStats()
    c2 = CapturingClient(LLMClient(settings, "system2", usage=usage), [])
    c1 = CapturingClient(LLMClient(settings, "system1", usage=usage), [])   # 项目现状
    c1nt = CapturingClient(NoThinkLLM(settings, "system1", usage=usage), [])  # 关思考对照
    if not (c1.configured and c2.configured):
        sys.exit("AI 槽位未配置（模型名/Key），无法实调")
    print(f"== 槽位: system2={c2.inner.model} | system1={c1.inner.model} ==")

    threat = threat_from_log(d)
    cards = ocr_real_cards(kb, load_layout(W, H))
    print(f"== 海克斯符文选项（真机 OCR）: {cards} ==")

    # ── 1. System2：ChampionContext（与 engine.start_game 同路径） ──
    ally, enemy = d.get("ally") or [], d.get("enemy") or []
    ctx = system2.generate(kb, rules, c2, hero, ally, enemy,
                           data_version=kb.builds_patch)
    show_prompts(c2.log, 0)
    print(f"[System2 raw output]\n{c2.last_raw}\n")
    print(f"[System2 解析] source={ctx.source} 亲和{len(ctx.augment_affinity)}"
          f" 出门{len(ctx.opening_items)} 路线{list(ctx.build_paths)}"
          f" 小件规则{len(ctx.component_rules)} 联动{len(ctx.augment_item_links)}")
    for a in ctx.augment_affinity[:8]:
        print(f"   - {a['name']}（bias={a['bias']}）{a['note']}")

    my_items = d.get("final_items") or []
    gold, path = 4000.0, d.get("current_path") or "ad"
    first = taken[0] if taken else ""

    # ── 2a. System1：海克斯三卡片评级（真机选项） ──────────────────
    base_ranks = rules.rank_augments(hero, cards, taken)
    print(f"\n== System1 海克斯评级（规则基准档）==")
    for r in base_ranks:
        print(f"   [{r['grade']}] {r['name']} —— {r['reason']}")
    ranks = system1.rank_augments(c1, ctx, hero, cards, taken, my_items,
                                  threat.summary(), base_ranks)
    show_prompts(c1.log, 0)
    print(f"[现状分支输出] content={c1.last_raw!r} → 全部降级规则版" if not c1.last_raw
          else f"[现状分支输出]\n{c1.last_raw}")
    ranks_ai = system1.rank_augments(c1nt, ctx, hero, cards, taken, my_items,
                                     threat.summary(), base_ranks)
    print(f"[关思考分支 raw output]\n{c1nt.last_raw}\n")
    print("[玩家可见 · 海克斯时刻块]")
    print("── 海克斯时刻 ──")
    for i, r in enumerate(ranks_ai[:3], 1):
        print(f"{'①②③'[i-1]} [{r.get('grade', '?')}] {r.get('name', '?')}"
              f"  {r.get('reason', '')}")

    # ── 2b. System1：出门装三选 ───────────────────────────────────
    base_open = rules.opening_candidates(hero, first, threat)
    print(f"\n== System1 出门装（规则基准序）==")
    for c in base_open:
        print(f"   {c.name} —— {c.reason}")
    picks = system1.suggest_opening(c1, ctx, hero, first, threat.summary(),
                                    base_open)
    print(f"[现状分支输出] content={c1.last_raw!r} → 降级规则序" if not c1.last_raw
          else f"[现状分支输出]\n{c1.last_raw}")
    picks_ai = system1.suggest_opening(c1nt, ctx, hero, first, threat.summary(),
                                       base_open)
    show_prompts(c1nt.log, 1)
    print(f"[关思考分支 raw output]\n{c1nt.last_raw}\n")
    print("[玩家可见 · 出门装建议]")
    print(render_opening(picks_ai, first))

    # ── 2c. System1：下一件三选（历史快照） ───────────────────────
    base_next = rules.build_sequence(my_items, path, threat, hero=hero)
    print(f"\n== System1 下一件（规则基准序）==")
    for c in base_next:
        print(f"   {c.name}（{c.price}G） —— {c.reason}")
    picks = system1.suggest_next(c1, ctx, my_items, gold, path,
                                 threat.summary(), taken, base_next)
    print(f"[现状分支输出] content={c1.last_raw!r} → 降级规则序" if not c1.last_raw
          else f"[现状分支输出]\n{c1.last_raw}")
    picks_ai = system1.suggest_next(c1nt, ctx, my_items, gold, path,
                                    threat.summary(), taken, base_next)
    show_prompts(c1nt.log, 2)
    print(f"[关思考分支 raw output]\n{c1nt.last_raw}\n")
    print("[玩家可见 · 出装主线]")
    print(render_next(picks_ai, kb, gold))

    # ── 用量 ──────────────────────────────────────────────────────
    p1i = settings.get_float("system1", "price_in", default=0.8)
    p1o = settings.get_float("system1", "price_out", default=2.7)
    p2i = settings.get_float("system2", "price_in", default=2.4)
    p2o = settings.get_float("system2", "price_out", default=9.6)
    u = c2.inner.usage
    cost = (u.total_tokens_in / 1e6 * p2i + u.total_tokens_out / 1e6 * p2o
            + u.total_tokens_in / 1e6 * p1i + u.total_tokens_out / 1e6 * p1o)
    print(f"\n== 用量: {usage.total_calls} 次调用, "
          f"in={usage.total_tokens_in} out={usage.total_tokens_out}, "
          f"费用≈¥{cost:.4f} | 耗时 {time.time() - t0:.0f}s ==")


if __name__ == "__main__":
    main()
