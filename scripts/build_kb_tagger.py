"""229 海克斯语义标签库生成（计划 §5 B 层：System2 离线打标，增量更新）

用法：
  uv run python scripts/build_kb_tagger.py           # 全量缺项打标（断点续跑）
  uv run python scripts/build_kb_tagger.py --force   # 全部重打（覆盖旧标签）
  uv run python scripts/build_kb_tagger.py --dry-run # 只看缺多少，不调用

产物：hexa/knowledge/augments.json
  {"version": "tags-<patch>-<date>", "augments": {名字: {
     "path_bias": "ad|ap|tank|none",
     "tags": ["重伤","护甲",...]（受控词表）,
     "is_upgrade": bool,          # "升级：X"型海克斯
     "opening_shift": "装备名|",  # 出门装联动（如 心之刚→巨人腰带）
     "opening_shift_reason": "<=16字"}}}

守门：输出名字必须 ⊆ 输入分片；path_bias/词表受控；opening_shift 必须过
DD 装备库校验。失败分片自动重试 1 次，再失败保留待续跑（下次补）。
费用参考：229 条 × qwen3.8-flash ≈ 几分钱。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexa.config import KNOW_DIR, Settings  # noqa: E402
from hexa.knowledge.kb import KnowledgeBase  # noqa: E402

OUT_PATH = KNOW_DIR / "augments.json"
CHUNK = 24
TAG_VOCAB = [
    "爆发", "poke", "持续输出", "治疗", "重伤", "护甲", "魔抗", "生命",
    "攻速", "暴击", "穿甲", "法穿", "护盾", "位移", "控制", "成长",
    "经济", "复活", "升级", "真伤", "减速", "吸血", "召唤物", "游走",
]
BIASES = {"ad", "ap", "tank", "none"}

SYSTEM_PROMPT = """你是英雄联盟「海克斯大乱斗」数据打标员。对给定的每个海克斯（大乱斗 \
模式特有强化符文），输出语义标签 JSON。

要求：
1. path_bias：该海克斯最利于的出装方向——ad（物理）/ap（法术）/tank（坦度）/none（通用）。
2. tags：从受控词表里选 0~4 个：%(vocab)s。
3. "升级：X"型海克斯（名字带"升级："前缀）is_upgrade=true，其效果依赖已购装备。
4. opening_shift：仅当该海克斯强烈改变第一件出门装选择时填具体装备名（用简中通用名，\
如 巨人腰带/多兰之刃/红水晶），否则留空字符串。
5. 只输出 JSON：{"tags": [{"name": "原名", "path_bias": "...", "tags": [...], \
"is_upgrade": false, "opening_shift": "", "opening_shift_reason": ""}]}
名字必须与输入完全一致，不得增删改。""" % {"vocab": "、".join(TAG_VOCAB)}


def load_existing() -> dict:
    if OUT_PATH.exists():
        try:
            return json.loads(OUT_PATH.read_text(encoding="utf-8")).get("augments", {})
        except Exception:
            pass
    return {}


def validate_chunk(chunk_names: list, out_tags: list, kb: KnowledgeBase) -> dict:
    """输出守门：名字 ⊆ 分片；枚举受控；opening_shift 过 DD 库。返回 name→tag"""
    out = {}
    for t in out_tags or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if name not in chunk_names or name in out:
            continue
        bias = str(t.get("path_bias") or "none").lower()
        if bias not in BIASES:
            bias = "none"
        tags = [x for x in (t.get("tags") or []) if x in TAG_VOCAB][:4]
        is_up = bool(t.get("is_upgrade"))
        shift = str(t.get("opening_shift") or "").strip()
        reason = str(t.get("opening_shift_reason") or "").strip()[:16]
        if shift and not kb.valid_item(shift):
            shift, reason = "", ""
        if name.startswith("升级："):
            is_up = True
        out[name] = {"path_bias": bias, "tags": tags, "is_upgrade": is_up,
                     "opening_shift": shift, "opening_shift_reason": reason}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已有标签全部重打")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    kb = KnowledgeBase()
    names = sorted(kb.augment_names)
    existing = {} if args.force else load_existing()
    todo = [n for n in names if n not in existing]
    print(f"[TAG] 共 {len(names)} 海克斯，已有标签 {len(existing)}，待打 {len(todo)}")
    if args.dry_run or not todo:
        return

    settings = Settings()
    settings.set("ai", "enabled", True)  # 仅进程内：让 build_engine 逻辑可复用
    from hexa.ai.client import LLMClient, LLMError, parse_json
    client = LLMClient(settings, "system2")
    if not client.configured:
        print("[TAG] 未配置 System2 模型/Key（settings.json 或 DASHSCOPE_API_KEY）")
        sys.exit(2)

    merged = dict(existing)
    failed = []
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        user = "海克斯名单：\n" + "\n".join(f"- {n}" for n in chunk)
        got = None
        for _ in range(2):
            try:
                text, _meta = client.chat(SYSTEM_PROMPT, user, max_tokens=2400)
                got = validate_chunk(chunk, parse_json(text).get("tags"), kb)
                if got:
                    break
            except (LLMError, Exception) as e:  # noqa: BLE001
                print(f"[WARN] 分片失败重试: {e}")
                time.sleep(1.0)
        if got:
            merged.update(got)
            print(f"[TAG] 进度 {min(i + CHUNK, len(todo))}/{len(todo)}"
                  f"（本片 {len(got)}/{len(chunk)}）")
        else:
            failed += chunk
        # 每片落盘（断点续跑）
        doc = {"version": f"tags-{kb.dd_version or 'unknown'}-"
                          f"{time.strftime('%Y%m%d')}",
               "tag_vocab": TAG_VOCAB, "augments": merged}
        OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"[TAG] 完成：成功 {len(merged)} / 失败 {len(failed)} {failed[:5]}")
    if failed:
        print("[TAG] 失败分片下次运行自动补打")


if __name__ == "__main__":
    main()
