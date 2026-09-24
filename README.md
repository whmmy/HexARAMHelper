# HexARAMHelper 海克斯大乱斗助手

游戏加载时读双方阵容 → 规则引擎 + AI（可选）翻译当前版本数据 → 游戏内 overlay
自动给出：出门装三选、每轮海克斯推荐、下一件三选、小件合成判断、建议加点顺序——
全部带理由，跟着已选海克斯和实际出装自动调整。

**铁律：数据出事实，模型出判断。** 版本答案只来自 OP.GG 天级刷新统计（英雄粒度），
模型只做重排/适配/写理由，输出经 DD 装备库校验防幻觉。

## 快速上手（开发）

```bash
# 需要 uv（自动管理 Python 3.12）
uv sync

# 离线冒烟测试（无 LCU / 无游戏 / 无网络）
uv run python tests/smoke_offline.py

# 启动（建议管理员，LCU 进程扫描需要）
uv run python app.py
```

- 游戏内 overlay：右侧窄条常驻，F10 显隐
- 手动兜底：主窗口「手动设英雄」输入拼音/中文（LCU 断连时）
- 真机调测 dump：`settings.json` 开 `"fixtures": {"dump": true}`，见
  [docs/真机验证清单.md](docs/真机验证清单.md)

## P0.5 翻译层验证（一键）

```bash
# 数据已备好（experiments/p05_data/*.json），配置 key 后：
uv run python experiments/p05_translation_test.py
```

API Key 二选一：环境变量 `DASHSCOPE_API_KEY` 或项目根 `.env`（见 `.env.example`）。
无 key 时脚本打印指引并以 exit 2 退出。

## 数据更新

```bash
uv run python scripts/gen_champ_profile.py    # 从 DD + CSV 重新生成知识派生文件
uv run python scripts/fetch_opgg_builds.py    # 重抓 OP.GG 出装/加点（天级刷新用）
```

## 结构

```
app.py                # 入口：主窗口+托盘+overlay+服务编排
hexa/
  config.py           # settings.json 读写（AI 双槽位/触发器/预算）
  core/               # lcu(阶段状态机) live_api fixtures game_state rules_engine
  knowledge/          # DD 装备库/海克斯名单/英雄画像 + kb.py 加载器
  ui/                 # theme main_window overlay
experiments/          # P0.5 翻译层验证
scripts/              # 数据抓取与生成
docs/                 # 可行性研究 / 实施计划 v4.1 / 真机验证清单
```

详细设计见 [docs/实施计划.md](docs/实施计划.md)。
