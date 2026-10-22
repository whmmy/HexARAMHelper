# HexARAMHelper —— 海克斯大乱斗 AI 军师

隔段时间回归跟不上版本？本工具把当前版本数据（OP.GG 天级统计 + CommunityDragon
装备库）交给**双层 AI**，实时翻译成"这局怎么出装、怎么选海克斯"，并随战局演化
自动精修——加载期定战略，局内出参谋，全程只读观察、绝不代你操作。

**铁律：数据出事实，模型出判断。** 版本答案只锚 OP.GG 天级统计，敌方威胁以
Live API 原文直出喂给模型；模型输出的一切装备/海克斯名逐字过装备库校验，
幻觉条目直接丢弃。只显示推荐档位（S/A/B）不显示胜率数字（Riot 展示政策）。

## 核心架构：双层 AI 军师

| 层 | 实现 | 频次 | 职责 |
|---|---|---|---|
| **System 2 战略层** | 大模型（默认 qwen3.8-max） | 每局 1 次，选人期阵容锁定即后台预生成 | 观察双方 10 人阵容 → ChampionContext：多流派完整出装方案（每套带路线标注与适配 note）+ 海克斯亲和 + 出门装 |
| **System 1 战术层** | 小模型（默认 qwen3.8-flash） | 局内 8~11 次 | 结合对局快照、已选海克斯、敌方威胁实证，在 System2 全方案池内权衡，输出"3 选项 + 理由" |
| **规则引擎（守门员）** | 纯代码，无 AI | 常驻 | 防幻觉校验、地图/金币可行性过滤、评分排序；AI 全链失败时无缝兜底 |

分层逻辑：加载期用大模型做一次重的"阵容阅读"（对局开始即就绪，首个海克斯
零等待），局内用小模型做高频轻量的"战局精修"——每次推荐都在全方案池内
重新权衡，而不是加载期定稿。

### AI 接入能力

- **方案制而非伤害类型三键**：莫甘娜"火炬面具冰杖 / 火炬面具沙漏 / 火炬面具
  影焰"是三套独立命名方案，同伤害类型多方案分叉；局内海克斯全偏 AP 时
  AD 英雄能顺滑切到 AP 方案池
- **双槽位独立配置**：System2/System1 各自选模型、Key、价格档；超时自动
  降级模型、失败自动回退规则建议
- **任意 OpenAI 兼容平台**：DashScope / DeepSeek / 本地 vLLM 均可换用
- **成本守门**：每局调用上限（默认不限）、局内精修最小间隔 20s、费用仪表
  实时显示本局/累计花费（约 ¥0.07~0.12/局）
- **同源同参兜底**：AI 失败降级与 AI 精修看到同一份事实；无 Key 时全自动
  纯规则模式，功能独立成立

### 玩家主权

流派选择权在你（"这把就是想玩 AP 卡莎"），工具只参谋：当前出装路线由你的
实际购买行为投票推断，模型倾向只作为 note 展示；没有"先选流派"之类的强制
步骤，overlay 建议自动跟随实际状态演化。

## 游戏内外你会看到

- 游戏内 overlay：右侧窄条常驻（F10 显隐）；海克斯界面出现时顶部高亮
  三卡片档位（S/A/B）+ 理由
- 出装主线带金币感知；满六神装自动切"卖X换Y"替换推荐（差价/现金核算）；
  军师提示滚动给出阵容缺口、顺逆风、攒钱与替换提醒
- 敌方画像直出（如 `牛头酋长(ap,Tank/Support)`）：敌方实际出装/KDA/死亡
  来源经 Live API 实证，喂给两层模型做威胁判断
- 海克斯记账：界面活跃时点击卡片自动记录（只读鼠标观察），F9 撤销最近一条
  （F9 仅本工具窗口在前台时生效，对局中请切回主窗口按 F9 或点「撤销」）；
  reroll 点击自动重扫
- 成型度对比面板（主窗口）：敌我十人六神装进度（传说件 x/6 + 装备估值）
  实时对比
- 上局回顾（主窗口）：自动加载 `log/games/` 最近一份复盘——英雄/时长/
  海克斯台账/建议次数/终局出装×主线，只读渲染
- 标定：第一次进海克斯界面后点主窗口「开始标定」（约 2s，之后永久全自动）
- 设置：主窗口「⚙ 设置」——AI 双槽位（模型/Key/价格/测试连接）、触发器、
  预算、费用仪表、数据新鲜度面板
- 手动兜底：主窗口「手动设英雄」输入拼音/中文（LCU 断连时）
- 真机调测 dump：`settings.json` 开 `"fixtures": {"dump": true}`，见
  [docs/真机验证清单.md](docs/真机验证清单.md)

## 快速上手

```bash
# 需要 uv（自动管理 Python 3.12）
uv sync

# 离线冒烟测试（无 LCU / 无游戏 / 无网络 / 无 Key）
uv run python tests/smoke_offline.py

# 启动（自动弹 UAC 请求管理员——为什么需要见下节）
uv run python app.py

# 无游戏客户端视觉验证（mock 驱动一整局）
HEXA_MOCK=1 uv run python -u app.py
```

## 管理员权限说明（重要）

**为什么需要**：游戏客户端以管理员（高完整性级别）运行；本工具以普通权限
运行时，游戏画面焦点期内所有只读输入观察通道（底层鼠标钩子等）都收不到
事件——海克斯选卡点击完全无法记账（真机实测事件计数归零，连鼠标移动都
看不到）。提权到与游戏同级后观察即恢复。

- 启动时自动弹一次 UAC 提权重启（默认开启，设置 `diagnostics.elevate_auto`
  可关；也可手动右键"以管理员身份运行"）
- 拒绝 UAC → 降级运行并警告：海克斯记账不可用；其余功能（AI/规则驱动的
  出装推荐、敌方画像、复盘）不受影响
- 权限只用于**观察**：绝不向游戏注入任何输入——不自动买装、不自动选海克斯、
  不模拟按键；也不装全局键盘钩子（热键是 GetAsyncKeyState 轮询实现）

不放心可直接审代码：鼠标观察在 `hexa/core/mouse_ledger.py`，全部只读实现。

## AI 配置（可选，无 Key 自动纯规则）

Key 三种来源任选：设置界面填入（仅存本地 settings.json）/ 环境变量
`DASHSCOPE_API_KEY` / 项目根 `.env`（复制 `.env.example` 填入）。

## 隐私与数据流向

- 全程本地运行，无遥测无上报；网络访问仅限本机 LCU / Live Client API
  （只读）与你配置的模型端点；OP.GG 抓取只发生在手动执行的数据脚本里
- AI 开启时会把本局阵容与出装快照发送到你配置的模型端点（DashScope 或
  任意 OpenAI 兼容平台）；不开启 AI 则零数据外发
- 每次海克斯卡片点击会在 `log/captures/` 落一张定格截图（排障用，默认开启，
  设置 `click_capture` 可关）——**分享日志目录前注意其中含游戏画面截图**
- 屏幕截图用于海克斯 OCR 与标定；标定数据（`calibrations/`）按屏幕分辨率
  生成，仅存本地

## 合规与免责声明

- 本项目与 Riot Games、OP.GG 无关且未经其认可；League of Legends 相关
  名称与数据版权归 Riot Games，出装统计来自 OP.GG，装备知识来自
  CommunityDragon
- 代码以 [MIT](LICENSE) 许可证开源；仅供个人学习与娱乐使用，按"原样"提供，
  不作任何担保；因使用本工具产生的一切后果（包括账号风险）由使用者自行承担

## 数据更新（OP.GG 天级刷新）

```bash
uv run python scripts/gen_champ_profile.py      # 从 DD + CSV 重新生成知识派生文件
uv run python scripts/fetch_opgg_builds.py      # 重抓 OP.GG 出装/加点
uv run python scripts/build_kb_tagger.py        # 海克斯语义标签增量打标（System2）
```

## 打包

> ⚠️ 已知问题（修复排在真机稳定之后）：PyInstaller 6.x 把 `--add-data` 放进
> `_internal/` 子目录而代码取 exe 目录 → 数据资产落空；提权重启依赖
> `.venv/pyvenv.cfg`，打包后不存在 → 永远普通权限。**exe 打包当前不可用，
> 请以源码方式运行。**

```bash
uv pip install pyinstaller
uv run python build.py        # → dist/HexARAMHelper/HexARAMHelper.exe
```

杀软误报预案：热键为纯 ctypes 实现（GetAsyncKeyState 50ms 轮询），不装全局
键盘钩子；F9 撤销记账另带前台窗口校验——仅本工具窗口在前台时生效。个别杀软
仍可能对 mss 截图 / psutil 进程扫描误报，可将程序目录加入信任。

## 结构

```
app.py                # 入口：主窗口+托盘+overlay+服务编排（含检测/记账/提权）
build.py              # PyInstaller 打包（见上「已知问题」）
hexa/
  config.py           # settings.json 读写（AI 双槽位/触发器/预算）+ .env 加载
  ai/                 # client(双槽位 LLM) system2(ChampionContext) system1(建议器)
                      # engine(DecisionEngine：规则/AI/Jev 占位 + 预算间隔守门)
  core/               # lcu(阶段状态机) live_api fixtures game_state rules_engine
                      # ocr_engine(RapidOCR+库匹配) screen_detect(迟滞检测+标定)
                      # mouse_ledger(只读点击记账)
  knowledge/          # DD 装备库/海克斯名单/语义标签库 + kb.py 加载器
  ui/                 # theme main_window(台账纠错/标定) settings_window overlay
data/                 # 英雄×海克斯先验 / OP.GG builds / 拼音映射
scripts/              # 数据抓取与生成
tests/                # 离线冒烟（smoke_offline.py，交付门禁）
experiments/          # P0.5 翻译层验证
docs/                 # 可行性研究 / 真机验证清单 / 竞品对比
```
