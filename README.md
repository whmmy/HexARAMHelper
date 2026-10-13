# HexARAMHelper 海克斯大乱斗助手

游戏加载时读双方阵容 → 规则引擎 + AI（可选）翻译当前版本数据 → 游戏内 overlay
自动给出：出门装三选、每轮海克斯推荐（S/A/B 档）、出装主线序列（金币感知）、
小件合成判断、建议加点顺序——全部带理由，跟着已选海克斯和实际出装自动调整。

**铁律：数据出事实，模型出判断。** 版本答案只来自 OP.GG 天级刷新统计（英雄粒度），
模型只做重排/适配/写理由，输出经 DD 装备库校验防幻觉。只显示档位不显示胜率数字
（合规），绝不向游戏注入任何输入（只读观察）。

## 快速上手（开发）

```bash
# 需要 uv（自动管理 Python 3.12）
uv sync

# 离线冒烟测试（无 LCU / 无游戏 / 无网络 / 无 Key）
uv run python tests/smoke_offline.py

# 启动（建议管理员，LCU 进程扫描需要）
uv run python app.py

# 无游戏客户端视觉验证（mock 驱动一整局）
HEXA_MOCK=1 uv run python -u app.py
```

- 游戏内 overlay：右侧窄条常驻，F10 显隐；海克斯界面出现时顶部高亮三卡片档位
- 海克斯记账：界面活跃时点击卡片自动记录（只读鼠标观察），F9 一键撤销最近一条
  （F9 仅本工具窗口在前台时生效；对局中请切回主窗口按 F9 或点「撤销」纠错）
- 出装主线带金币感知；满六神装后自动切「卖X换Y」替换推荐（差价/现金核算）；
  军师提示滚动给出阵容缺口、顺逆风、攒钱与替换提醒
- 成型度对比面板（主窗口）：敌我十人六神装进度（传说件 x/6 + 装备估值）实时对比
- reroll：点刷新按钮自动重扫
- 标定：第一次进海克斯界面后点主窗口「开始标定」（约 2s，之后永久全自动）
- 设置：主窗口「⚙ 设置」——AI 双槽位（模型/Key/价格/测试连接）、触发器、
  预算、费用仪表、数据新鲜度面板
- 手动兜底：主窗口「手动设英雄」输入拼音/中文（LCU 断连时）
- 真机调测 dump：`settings.json` 开 `"fixtures": {"dump": true}`，见
  [docs/真机验证清单.md](docs/真机验证清单.md)

## AI 双槽位（可选，无 Key 自动纯规则）

设置界面开启 AI 后：System 2（默认 qwen3.8-max）在选人期阵容锁定即后台
预生成 ChampionContext（海克斯亲和/出门装方案/多路线/海克斯→出装联动），
开局直接复用（首个海克斯零等待；未预生成则开局照旧派发），System 1
（默认 qwen3.8-flash）在局内按快照给三选+理由。守门：每局 AI 调用上限
budget_per_game（默认 0=不限；选人期预生成与局内精修共用计数）、periodic
精修最小间隔、超时自动降级模型、失败自动回退规则建议。费用仪表实时显示
本局/累计花费（~¥0.07~0.12/局）。

Key 配置：设置界面填入（仅存本地 settings.json）或环境变量 `DASHSCOPE_API_KEY` /
项目根 `.env`（见 `.env.example`）。任意 OpenAI 兼容平台均可换用。

## 数据更新

```bash
uv run python scripts/gen_champ_profile.py      # 从 DD + CSV 重新生成知识派生文件
uv run python scripts/fetch_opgg_builds.py      # 重抓 OP.GG 出装/加点（天级刷新）
uv run python scripts/build_kb_tagger.py        # 海克斯语义标签增量打标（System2）
```

## 打包

```bash
uv pip install pyinstaller
uv run python build.py        # → dist/HexARAMHelper/HexARAMHelper.exe
```

杀软误报预案：热键为纯 ctypes 实现（GetAsyncKeyState 50ms 轮询），不装 keyboard
全局键盘钩子，正常不会被杀软当钩子误报；F9 撤销记账另带前台窗口校验——仅本
工具窗口在前台时生效，不会在其他应用里静默改台账。个别杀软仍可能对 mss 截图 /
psutil 进程扫描误报，发布时引导用户将程序目录加入信任（个人自用无此问题——
直接源码运行）。

## 结构

```
app.py                # 入口：主窗口+托盘+overlay+服务编排（含检测/记账线程）
build.py              # PyInstaller 打包
hexa/
  config.py           # settings.json 读写（AI 双槽位/触发器/预算）+ .env 加载
  ai/                 # client(双槽位 LLM) system2(ChampionContext) system1(建议器)
                      # engine(DecisionEngine：规则/AI/Jev 占位 + 预算间隔守门)
  core/               # lcu(阶段状态机) live_api fixtures game_state rules_engine
                      # ocr_engine(RapidOCR+库匹配) screen_detect(迟滞检测+标定)
                      # mouse_ledger(只读点击记账)
  knowledge/          # DD 装备库/海克斯名单/语义标签库 + kb.py 加载器
  ui/                 # theme main_window(台账纠错/标定) settings_window overlay
data/                 # 英雄×海克斯先验 / OP.GG builds ×173 / 拼音映射
experiments/          # P0.5 翻译层验证
scripts/              # 数据抓取与生成
docs/                 # 可行性研究 / 实施计划 v4.2 / 真机验证清单
```

详细设计见 [docs/实施计划.md](docs/实施计划.md)。
