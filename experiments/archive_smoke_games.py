"""一次性清理：把 log/games/ 下的冒烟测试残留归档到 _smoke_archive/（移动，不删除）

背景：tests/smoke_offline.py 早期版本未隔离 GameSession 的局终落盘
（现已 patch _game_state.BASE_DIR 到临时目录，见 smoke_offline.py 顶部），
历史运行在 log/games/ 留下 ~30 份 0 时长假对局。

判定必须看内容而非文件名（真实对局也可能同名英雄）：
  1) duration_s 为 0 或缺失 —— 真实对局从 game_start 到 EndOfGame 至少几分钟，
     round(time.time() - t0, 1) 不会得到 0.0；smoke 里 on_end_of_game 紧跟
     on_game_start，间隔 <0.1s 才会落 0.0；
  2) 且阵容与 smoke 剧本一致 —— 两套剧本状态（依据 tests/smoke_offline.py）：
     - 5v5 完整局（smoke_offline.py on_game_start 的 teams）
     - 选人"换人跟随"中间态（旧版 smoke 在选人阶段即触发落盘：ally=[暗裔剑魔],
       enemy=[黑暗之女]，且 augments 全是 REAL_AUG=sorted(augment_names)[0]
       ="万用瞄准镜"）。1v1 真实对局不可能出现在大乱斗，双保险。

用法：uv run python experiments/archive_smoke_games.py [--dry]
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAMES_DIR = ROOT / "log" / "games"
ARCHIVE_DIR = GAMES_DIR / "_smoke_archive"

# smoke 剧本阵容（与 tests/smoke_offline.py 的 on_champ_select/on_game_start 对应）
SMOKE_LINEUPS = [
    # 5v5 完整局
    ({"暗裔剑魔", "九尾妖狐", "皎月女神", "盲僧", "众星之子"},
     {"黑暗之女", "狂暴之心", "放逐之刃", "诺克萨斯之手", "琴瑟仙女"}),
    # 选人"换人跟随"中间态（旧版 smoke 残留）
    ({"暗裔剑魔"}, {"黑暗之女"}),
]


def is_smoke_residue(doc: dict) -> bool:
    if doc.get("duration_s"):          # 0 / 0.0 / 缺失(None) 均算 0 时长
        return False
    ally, enemy = set(doc.get("ally") or []), set(doc.get("enemy") or [])
    return any(ally == a and enemy == e for a, e in SMOKE_LINEUPS)


def main(dry: bool = False):
    if not GAMES_DIR.is_dir():
        print(f"[INFO] {GAMES_DIR} 不存在，无事可做")
        return
    moved, kept, broken = [], [], []
    for f in sorted(GAMES_DIR.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:         # 解析失败的保守起见留在原地
            broken.append((f.name, str(e)))
            continue
        if is_smoke_residue(doc):
            moved.append(f.name)
            if not dry:
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(ARCHIVE_DIR / f.name))
        else:
            kept.append(f.name)

    print(f"[INFO] 归档 {len(moved)} 份 → {ARCHIVE_DIR.relative_to(ROOT)}"
          if not dry else f"[DRY] 将归档 {len(moved)} 份")
    for n in moved:
        print(f"  [MOVE] {n}")
    print(f"[INFO] 保留 {len(kept)} 份（疑似真实对局/无法判定）")
    for n in kept:
        print(f"  [KEEP] {n}")
    for n, why in broken:
        print(f"  [WARN] {n} 解析失败留在原地: {why}")


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
