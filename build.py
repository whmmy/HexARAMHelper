"""PyInstaller 打包脚本（计划 P4，fork 自参考项目 build 思路）

用法：uv run python build.py
产物：dist/HexARAMHelper/HexARAMHelper.exe（onedir，启动更快、杀软误报更少）

说明：
- 数据资产（data/、hexa/knowledge/、界面默认模板）随包分发
- settings.json / .env / calibrations 不打包（用户本地生成）
- mss 截图 / psutil 进程扫描在杀软可能误报 → 见 README「杀软误报预案」一节
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "HexARAMHelper"

DATA_ARGS = [
    "--add-data", "data;data",
    "--add-data", "hexa/knowledge;hexa/knowledge",
    # builds 单文件太大时可不带入（缺它退化为全库扫描），默认带上
    "--add-data", "data/builds;data/builds",
]

HIDDEN_IMPORTS = [
    "psutil", "mss", "pystray", "PIL",
    "rapidocr_onnxruntime", "opencv-python-headless:cv2",
    "thefuzz", "pypinyin", "openai",
]


def main():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--noconsole",
        "--name", APP_NAME,
        # 数据与隐藏导入
        *[f"--add-data={a}" if False else a for a in DATA_ARGS],
    ]
    for h in HIDDEN_IMPORTS:
        mod = h.split(":")[-1] if ":" in h else h
        cmd += ["--hidden-import", mod]
    cmd += ["--collect-all", "rapidocr_onnxruntime"]
    cmd += [str(ROOT / "app.py")]

    print("[BUILD]", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)
    print(f"[BUILD] 完成：dist/{APP_NAME}/{APP_NAME}.exe")
    print("[BUILD] 首次运行会生成 settings.json；API Key 在设置界面填写")


if __name__ == "__main__":
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller：uv add --dev pyinstaller 或 uv pip install pyinstaller")
        sys.exit(2)
    main()
    shutil.rmtree(ROOT / "build", ignore_errors=True)
