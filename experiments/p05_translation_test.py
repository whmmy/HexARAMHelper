#!/usr/bin/env python
"""P0.5 翻译层验证实验（计划 §8：先于一切 AI 功能的质量门）

目的：验证"数据出事实，模型出判断"链路 —— 给 qwen3.8-flash 当前版本真实数据
（hero_augments.csv 海克斯排名 + OP.GG 出装/加点），按 P2 真实流程生成
ChampionContext，然后自动判定三项定死标准：

  ① 零幻觉：输出中所有装备名命中 DD 装备库、所有海克斯名命中 229 名单
  ② 海克斯亲和 top10 中 ≥7 个与 OP.GG 当前版本排名（CSV）方向一致
  ③ 无过时装备：① 的另一侧面（不在当前 DD 版本 = 过时/幻觉）

通过 → P2 AI 双槽位可开工；不通过 → 退化为"英雄粒度为主 + 联动仅规则"。

用法:
    uv run python experiments/p05_translation_test.py          # 全部 3 英雄
    uv run python experiments/p05_translation_test.py aatrox   # 单英雄

API Key（二选一）:
    环境变量 DASHSCOPE_API_KEY=sk-xxx
    或项目根 .env 文件:  DASHSCOPE_API_KEY=sk-xxx
无 key 时打印指引并以 exit 2 退出（数据与脚本均已备好，key 到位一键跑）。
"""
import json
import os
import re
import sys
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "experiments" / "p05_data"
REPORT_PATH = ROOT / "experiments" / "p05_report.md"

HEROES = ["aatrox", "annie", "jinx"]

SYSTEM_PROMPT = """你是英雄联盟「海克斯大乱斗」模式的版本翻译器。铁律：数据出事实，你出判断。
用户会给你某英雄当前版本的真实统计数据（海克斯推荐排名、OP.GG 出装/加点）。
你的任务是把数据组织成结构化的 ChampionContext JSON，供局内小模型消费。

要求：
1. augment_affinity：从海克斯排名数据取 top15，标注 bias（ap/ad/tank/functional）与一句 note；
   "升级：X" 型海克斯单独标注 is_upgrade=true（决策语义是"升级哪件已购装备"）。
2. opening_items：给 3 组出门装（含"标准/对面poke多/配合特定海克斯"等 when 条件），
   装备名必须使用输入数据中出现过的名字，不得发明。
3. build_paths：按输入出装数据整理 ap/ad/tank 中实际存在的路线（每条 items+skill_order+note），
   不存在的路线不要编。
4. component_rules：从小件合成角度给 2~3 条（输入数据里出现的装备组件关系）。
5. augment_item_links：写出海克斯与出装的联动规则（如某海克斯→某装备/路线偏移+理由），
   只能引用输入数据中出现过的海克斯名和装备名。
6. skill_order：从加点数据提炼"主X副Y"。
7. 只输出 JSON（可被 json.loads 解析），不要输出任何其他文字。字段名与示例完全一致。

输出 JSON schema：
{
  "augment_affinity": [{"name":"...","bias":"ad","note":"...","is_upgrade":false}],
  "opening_items": [{"combo":["..."],"when":"..."}],
  "build_paths": {"ap":{"items":["..."],"skill_order":"主W副Q","note":"..."}},
  "component_rules": [{"item":"...","upgrade_to":"...","hold_when":"..."}],
  "augment_item_links": [{"augment":"...","shift":"...","reason":"..."}],
  "skill_order": "主X副Y"
}"""


def load_env_file():
    """读取项目根 .env（简单 KEY=VALUE，不引入额外依赖）"""
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def resolve_api_key() -> str:
    load_env_file()
    return (os.environ.get("DASHSCOPE_API_KEY") or "").strip()


def build_user_prompt(hero: str, kb, ground: dict) -> str:
    """构造与 P2 真实流程一致的数据输入（版本号显式进 prompt）"""
    hero_cn = ground["hero_cn"]
    patch = ground.get("patch") or kb.dd_version
    # 海克斯排名（CSV top30，含档位与排名 —— A 层统计事实）
    rows = []
    for name in kb.top_augments(hero_cn, 30):
        info = kb.augment_info(hero_cn, name)
        rows.append(f"{info['overall_rank']:>3}  {info['tier']}  {name}")
    aug_table = "\n".join(rows)
    builds = ground["core_builds"][:2]
    starters = ground["starter_items"][:2]
    boots = ground["boots"][:2]
    skills = ground.get("skill_orders", [])[:1]
    return f"""英雄：{hero_cn}
数据版本：{patch}（以下全部统计来自此版本，禁止使用你记忆中的旧版本知识，禁止出现数据外的装备/海克斯名）

【海克斯推荐排名 top30】（格式：总排名 稀有度 名称）
{aug_table}

【OP.GG 核心出装方案】
{json.dumps(builds, ensure_ascii=False)}

【出门装组合】
{json.dumps(starters, ensure_ascii=False)}

【鞋子选择】
{json.dumps(boots, ensure_ascii=False)}

【技能加点序列（1-18级）】
{skills}
"""


def call_model(api_key: str, user_prompt: str) -> str:
    from openai import OpenAI
    client = OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout=120,
        max_retries=1,
    )
    resp = client.chat.completions.create(
        model="qwen3.8-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=2000,
        # qwen3 系列默认可能启用深度思考，长输入下非流式等待会超时；
        # 翻译层任务不需要思考链，显式关闭（未知参数平台会忽略，无害）
        extra_body={"enable_thinking": False},
    )
    return resp.choices[0].message.content or ""


def parse_model_json(text: str) -> dict:
    """容错解析（剥 markdown 围栏 / 前后杂文）"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ── 判定（三项定死标准） ──────────────────────────────────────────

def iter_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from iter_strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from iter_strings(v)


def structured_item_names(ctx: dict) -> list:
    """结构化装备名字段（直接进推荐输出的部分，零幻觉硬标准）：
    opening_items[].combo[] / build_paths.*.items[] / component_rules[].item+upgrade_to"""
    names = []
    for entry in ctx.get("opening_items") or []:
        names.extend(entry.get("combo") or [])
    for _path, p in (ctx.get("build_paths") or {}).items():
        if isinstance(p, dict):
            names.extend(p.get("items") or [])
    for rule in ctx.get("component_rules") or []:
        names.append(rule.get("item", ""))
        names.append(rule.get("upgrade_to", ""))
    return [n for n in names if n]


def free_text_suspects(ctx: dict, kb) -> list:
    """自由文本（when/note/shift/reason）中的疑似未命中装备词 —— 仅观测不硬卡。
    先剔除文本中真实存在的 DD 装备名（子串方向）与海克斯名，再看是否残留
    含装备关键字且形似名词（≤7 字）的片段。"""
    import re as _re
    suspects = []
    for field in ("opening_items", "build_paths", "component_rules",
                  "augment_item_links"):
        for s in iter_strings(ctx.get(field)):
            if not isinstance(s, str):
                continue
            rest = s
            # 剔除真实装备名/海克斯名（长名优先，防部分匹配残留）
            for known in sorted(kb.items, key=len, reverse=True):
                if known in rest:
                    rest = rest.replace(known, " ")
            for aug in kb.augment_names:
                if aug in rest:
                    rest = rest.replace(aug, " ")
            for token in _re.split(r"[、/，,。\s；;：:（）()【】\"']+", rest):
                if 2 <= len(token) <= 7 and _re.search(
                        r"(靴|剑|刀|杖|弓|甲|帽|典|刃|枪|盾|锤|爪|面具|破败|饮魔|眼泪|章节|药水|腰带)$", token):
                    suspects.append(token)
    return suspects


def judge(hero_cn: str, kb, ctx: dict, ground: dict) -> dict:
    """返回 {pass, checks: [(名, ok, detail)]}"""
    checks = []

    # ① 零幻觉装备 + ③ 无过时装备（硬标准：结构化装备名字段 100% 命中当前 DD 库）
    names = structured_item_names(ctx)
    bad_items = [n for n in names if not kb.valid_item(n)]
    checks.append(("①零幻觉：推荐字段装备名全部命中 DD 库",
                   not bad_items,
                   f"幻觉: {bad_items[:5]}" if bad_items
                   else f"命中 {len(names)} 个装备名"))

    # ①' 海克斯名零幻觉（affinity 名 + links.augment 字段）
    bad_augs = []
    for entry in ctx.get("augment_affinity") or []:
        name = str(entry.get("name", ""))
        if name and not kb.valid_augment(name):
            bad_augs.append(name)
    for link in ctx.get("augment_item_links") or []:
        aug = str(link.get("augment", ""))
        if aug and not kb.valid_augment(aug):
            bad_augs.append(aug)
    checks.append(("①零幻觉：海克斯名全部命中 229 名单", not bad_augs,
                   f"幻觉: {bad_augs[:5]}" if bad_augs else "全部命中"))

    # ② 海克斯亲和 top10 与 CSV 排名方向一致（集合交集 ≥7）
    model_top10 = [str(e.get("name", "")) for e in
                   (ctx.get("augment_affinity") or [])[:10]]
    csv_top10 = kb.top_augments(hero_cn, 10)
    overlap = len(set(model_top10) & set(csv_top10))
    checks.append((f"②海克斯亲和 top10 方向一致（交集 {overlap}/10 ≥7）",
                   overlap >= 7,
                   f"model: {model_top10[:5]}... csv: {csv_top10[:5]}..."))

    # 附加观测1：自由文本装备引用（不计通过线，供 prompt 调优参考）
    suspects = free_text_suspects(ctx, kb)
    checks.append(("观测：理由文字无未登记装备词（不计入通过线）",
                   not suspects, f"待人工复核: {suspects[:6]}" if suspects else "干净"))

    # 附加观测2：加点顺序与 OP.GG 一致性
    skill_note = ""
    expected = ground.get("skill_priority", "")
    got = ctx.get("skill_order", "")
    if expected and got:
        skill_note = f"模型: {got} / OP.GG: {expected} / {'一致' if got == expected else '不一致'}"
    checks.append(("观测：加点顺序与 OP.GG 一致（不计入通过线）",
                   got == expected or not expected, skill_note))

    return {"pass": all(ok for _, ok, _ in checks[:3]), "checks": checks}


# ── 主流程 ───────────────────────────────────────────────────────

def main():
    slugs = [a for a in sys.argv[1:] if not a.startswith("-")] or HEROES
    from hexa.knowledge.kb import KnowledgeBase
    kb = KnowledgeBase()

    missing = [s for s in slugs if not (DATA_DIR / f"{s}.json").exists()]
    if missing:
        print(f"[!] 缺少 ground truth 数据: {missing}，先运行 "
              f"`uv run python scripts/fetch_opgg_builds.py {' '.join(missing)}`")
        sys.exit(2)

    api_key = resolve_api_key()
    if not api_key:
        print("=" * 56)
        print("未检测到 API Key —— P0.5 数据与脚本已全部备好，只差 key。")
        print("两种方式任选其一后重新运行本脚本：")
        print("  1) 环境变量:  set DASHSCOPE_API_KEY=sk-你的百炼key")
        print("     (Git Bash:  export DASHSCOPE_API_KEY=sk-你的百炼key)")
        print("  2) 项目根建 .env 文件，内容一行:")
        print("     DASHSCOPE_API_KEY=sk-你的百炼key")
        print("百炼 key 获取: https://bailian.console.aliyun.com/")
        print("=" * 56)
        sys.exit(2)

    print(f"[OK] API Key 已配置（{'*' * 6}{api_key[-4:]}），开始 P0.5 翻译层验证")
    results = []
    for slug in slugs:
        ground = json.loads((DATA_DIR / f"{slug}.json").read_text(encoding="utf-8"))
        hero_cn = ground["hero_cn"]
        print(f"\n=== {hero_cn} ({slug}) ===")
        user_prompt = build_user_prompt(slug, kb, ground)
        try:
            raw = call_model(api_key, user_prompt)
            ctx = parse_model_json(raw)
        except Exception as e:
            print(f"[FAIL] 模型调用/解析失败: {e}")
            results.append((hero_cn, {"pass": False,
                                      "checks": [("模型调用/解析", False, str(e))]},
                            ctx := None, user_prompt))
            continue
        verdict = judge(hero_cn, kb, ctx, ground)
        for name, ok, detail in verdict["checks"]:
            print(f"  {'✅' if ok else '❌'} {name}  {detail}")
        results.append((hero_cn, verdict, ctx, user_prompt))

    write_report(results)
    n_pass = sum(1 for _, v, _, _ in results if v["pass"])
    print(f"\n{'=' * 56}\nP0.5 结果: {n_pass}/{len(results)} 英雄通过")
    if n_pass == len(results):
        print("✅ 通过 —— P2 AI 双槽位可开工")
        sys.exit(0)
    print("❌ 未全部通过 —— 按计划退化为「英雄粒度为主 + 联动仅规则」并复核 prompt")
    sys.exit(1)


def write_report(results):
    lines = [f"# P0.5 翻译层验证报告",
             f"\n生成时间: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
             f"模型: qwen3.8-flash | 判定标准: 零幻觉 + top10≥7 一致 + 无过时装备\n"]
    for hero_cn, verdict, ctx, prompt in results:
        lines.append(f"\n## {hero_cn} —— {'✅ 通过' if verdict['pass'] else '❌ 未通过'}\n")
        for name, ok, detail in verdict["checks"]:
            lines.append(f"- {'✅' if ok else '❌'} {name}　{detail}")
        if ctx:
            lines.append("\n<details><summary>模型输出 JSON</summary>\n\n```json")
            lines.append(json.dumps(ctx, ensure_ascii=False, indent=1))
            lines.append("```\n</details>")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] 报告已写入 {REPORT_PATH}")


if __name__ == "__main__":
    main()
