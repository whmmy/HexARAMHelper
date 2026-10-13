"""runes 探针汇总：log/runes_probe/runes_probe.jsonl → §6 复核结论

用法：
    uv run python experiments/summarize_runes_probe.py [JSONL路径]
    （路径缺省取 <项目根>/log/runes_probe/runes_probe.jsonl）

对 docs/真机验证清单.md §6 的判定输入：
- runes_field present/absent 计数 → 字段到底存不存在（playerlist vs
  allgamedata 两条路分开看）
- augs id 样例（去重前 20）→ id>100000 启发式成立与否（有无低 id
  海克斯 / 高 id 普通符文混入）
- 每玩家 augs 数量随帧 min→max → 是否 0→3 随选择递增

纯 stdlib，无网络。文件不存在时友好提示并 exit 1。
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_PATH = (Path(__file__).resolve().parent.parent / "log"
                / "runes_probe" / "runes_probe.jsonl")


def load_rows(path: Path) -> list:
    """逐行解析 JSONL；半行（进程崩溃截断）跳过不影响其余证据"""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def summarize(rows: list) -> dict:
    """按 src 分组统计：行数 / present·absent 玩家·帧 / augs id 样例 /
    每玩家 (session, champion, team) 的 augs 数随帧序列"""
    stats = {}
    for row in rows:
        src = str(row.get("src") or "?")
        st = stats.setdefault(src, {
            "rows": 0, "present": 0, "absent": 0,
            "aug_ids": set(), "series": defaultdict(list),
        })
        st["rows"] += 1
        players = row.get("players")
        if not isinstance(players, list):
            continue
        for p in players:
            if not isinstance(p, dict):
                continue
            if p.get("runes_field") == "present":
                st["present"] += 1
            else:
                st["absent"] += 1
            augs = p.get("augs")
            if isinstance(augs, list):
                st["aug_ids"].update(
                    a for a in augs if isinstance(a, (int, float)))
            key = (row.get("session"), str(p.get("champion") or "?"),
                   str(p.get("team") or "?"))
            st["series"][key].append(len(augs) if isinstance(augs, list) else 0)
    return stats


def print_report(stats: dict):
    for src in sorted(stats):
        st = stats[src]
        print(f"\n== {src} ==")
        print(f"样本行数: {st['rows']}")
        print(f"runes 字段: present 玩家·帧 {st['present']} / "
              f"absent 玩家·帧 {st['absent']}")
        ids = sorted(st["aug_ids"])
        if ids:
            print(f"augs id 样例（去重前 20，共 {len(ids)} 个）: {ids[:20]}")
        else:
            print("augs id 样例: 无（未观测到 id>100000 条目）")
        print("每玩家 augs 数量随时间（min→max）:")
        if not st["series"]:
            print("  （无玩家·帧样本）")
        for (session, champ, team) in sorted(
                st["series"], key=lambda k: str(k)):
            seq = st["series"][(session, champ, team)]
            lo, hi = min(seq), max(seq)
            verdict = "，递增" if hi > lo else ""
            print(f"  session {session}  {champ}({team}): "
                  f"{lo} -> {hi}（{len(seq)} 帧{verdict}）")


def main(argv) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    if not path.exists():
        print(f"[WARN] 找不到探针落库文件: {path}")
        print("探针随 app.py 对局中自动落库（默认开，settings "
              "fixtures.runes_probe 可关）；打完一局后再跑本脚本。")
        return 1
    rows = load_rows(path)
    if not rows:
        print(f"[WARN] 落库文件为空（无有效行）: {path}")
        return 1
    print(f"探针落库: {path}（共 {len(rows)} 行）")
    print_report(summarize(rows))
    print("\n判定输入齐了：字段存在性看 present/absent，启发式看 id 样例，"
          "0→3 递增看每玩家 min→max（对照 docs/真机验证清单.md §6 勾选）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
