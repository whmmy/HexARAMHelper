"""HexARAMHelper —— 海克斯大乱斗 AI 助手（版本翻译器）。

`__version__` 是**应用版本的唯一来源**：主窗口/设置窗口标题栏与界面角标都读它，
`pyproject.toml` 的 version 同步维护，两者一致性由 tests/smoke_offline.py 的
"应用版本与 pyproject 对齐"断言锁住（package = false，uv 不读包元信息，
所以两边必须手工同步 + 断言防漂移）。

版本号约定：0.x.y = 个人自用阶段（x 递增=有功能变更，y 递增=修复/微调）。
2026-09-30 0.1.0 → 0.2.0：UI 视觉重构（Quiet Dark v3，见 docs/ui/preview.html）。
"""
__version__ = "0.2.0"
