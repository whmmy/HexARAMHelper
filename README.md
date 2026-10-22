# HexARAMHelper

海克斯大乱斗（极地大乱斗 + 海克斯强化）的局内 AI 助手。程序运行后自动识别
当前对局：选人期根据双方阵容生成多套出装方案，游戏中结合敌方实际出装、经济
和已选海克斯实时调整建议；海克斯选卡界面弹出时直接给出三个档位（S/A/B）的
推荐和理由。

全程只读观察游戏（Live Client API / 屏幕识别 / 鼠标位置观察），不自动买装、
不自动选海克斯、不模拟按键。不配置 API Key 时自动切纯规则模式，基础推荐
功能仍然可用。

## 功能

- **出装推荐**：出装数据来自 OP.GG 天级统计与 CommunityDragon 装备库。加载期
  按双方阵容生成多套命名方案（同一伤害类型可有多种流派分叉），局内随金币、
  顺逆风、敌方堆甲/威胁实时刷新；满六神装后自动给"卖 X 换 Y"替换建议
- **海克斯推荐**：海克斯界面出现时，游戏内 overlay 显示三个卡片的档位与理由；
  点击卡片自动记录你的选择（reroll 后自动重扫）
- **敌方画像**：敌方实际出装、KDA、死亡来源经 Live API 读取后喂给模型做威胁
  判断，如 `牛头酋长(ap,Tank/Support)`
- **成型度对比**：敌我十人六神装进度（传说件 x/6 + 装备估值）实时对比
- **上局回顾**：自动加载最近一局的复盘（英雄/时长/海克斯台账/终局出装）
- **费用统计**：本局与累计的模型调用费用实时显示（实测约 ¥0.07~0.12/局）

## 工作方式

| 层 | 实现 | 频次 | 职责 |
|---|---|---|---|
| 战略层（大模型） | 默认 qwen3.8-max | 每局 1 次，选人期后台生成 | 读双方阵容，产出多流派出装方案 + 海克斯亲和 + 出门装 |
| 战术层（小模型） | 默认 qwen3.8-flash | 局内 8~11 次 | 结合对局快照与已选海克斯，在全部方案池内权衡，给出 3 选项 + 理由 |
| 规则引擎 | 纯代码 | 常驻 | 模型输出过装备库白名单校验（幻觉条目丢弃）、地图/金币可行性过滤；AI 失败时回退到 OP.GG 规则推荐 |

两层模型各自独立配置（模型、Key、平台地址、价格档均可分开设）。推荐只显示
档位不显示胜率数字（Riot 展示政策）。

## 使用步骤

1. **安装 [uv](https://docs.astral.sh/uv/)**（自动管理 Python 3.12），
   然后在本项目目录执行：

   ```bash
   uv sync
   ```

2. **（可选）配置 LLM**：不配也能用（纯规则模式）。见下节
   [LLM 配置](#llm-配置)。

3. **启动**：

   ```bash
   uv run python app.py
   ```

   启动时会弹一次 UAC 请求管理员权限（原因见
   [管理员权限说明](#管理员权限说明)），同意后自动重启。拒绝也能跑，但海克斯
   记账不可用。

4. **进对局后**：
   - 选人期阵容锁定即后台生成出装方案，无需操作
   - 标定全自动：第一次进入海克斯选卡界面时按当前分辨率自动完成（保持界面
     不动几秒即可），标定文件按分辨率存 `calibrations/`，换分辨率后首次出现
     界面会重新自动标定，平时无需任何操作
   - 游戏内右侧出现常驻 overlay（窄条，出装主线 + 提示）；海克斯界面弹出时
     顶部显示推荐档位
   - 点海克斯卡片会自动记录你的选择；F9 撤销最近一条（需本工具窗口在前台）；
     F10 切换 overlay 显隐
   - 若出现"卡片识别连续失配"提示（UI 改版/布局异常才需要），点主窗口
     「开始标定」手动兜底：点击后 10 秒内把海克斯界面切到前台
   - LCU 断连时可用主窗口「手动设英雄」输入拼音/中文兜底

5. **可选设置**：主窗口「⚙ 设置」可调 AI 双槽位（模型/Key/平台地址/价格/
   测试连接）、触发器、预算等；`settings.json` 开 `"fixtures": {"dump": true}`
   可抓真机调测数据，见
   [docs/真机验证清单.md](docs/真机验证清单.md)。

不想开游戏可以先跑 mock 验证界面：

```bash
HEXA_MOCK=1 uv run python -u app.py
```

## LLM 配置

协议为 OpenAI 兼容（openai SDK），**任意兼容平台都能接**：DashScope（百炼）、
DeepSeek、Kimi/Moonshot、智谱 GLM、本地 vLLM 等。战略层/战术层两个槽位各自
独立配置。

推荐在主窗口「⚙ 设置」里直接填：每个槽位有模型名、Base URL、API Key、单价、
「测试连接」按钮，改完即生效。也可编辑 `settings.json`（`system2`/`system1`
两组字段：`model` / `base_url` / `api_key` / `api_key_env` / `thinking` /
`fallback_model` / `price_in` / `price_out` 等）。

API Key 三种来源任选其一：

1. 设置界面填入（存本地 `settings.json`）
2. 环境变量（默认读 `DASHSCOPE_API_KEY`，槽位的 `api_key_env` 可改成任意变量名）
3. 项目根 `.env` 文件（复制 `.env.example` 填入）

几个注意事项：

- 默认 Base URL 指向 DashScope，换其他平台时把 Base URL 和模型名一起改掉
  （如 DeepSeek 填 `https://api.deepseek.com/v1` + `deepseek-chat`）
- `thinking` 档位（off/low/medium/high/auto）控制思考强度；qwen3 系在
  非流式下会强制关思考（开必超时），deepseek/glm 系可正常开
- 超时会自动用 `fallback_model` 重试；全部失败则回退规则推荐
- 每局调用次数有上限（默认不限），局内推荐最小间隔 20 秒

## 管理员权限说明

游戏客户端以管理员（高完整性级别）运行，本工具以普通权限运行时，游戏画面
焦点期内所有只读输入观察通道（底层鼠标钩子等）都收不到事件，海克斯选卡点击
完全无法记账（真机实测事件计数归零）。提权到与游戏同级后观察即恢复。

- 启动时自动弹一次 UAC 提权重启（默认开启，设置 `diagnostics.elevate_auto`
  可关；也可手动右键"以管理员身份运行"）
- 权限只用于观察，绝不向游戏注入任何输入——不自动买装、不自动选海克斯、
  不模拟按键；也不装全局键盘钩子（热键是 GetAsyncKeyState 轮询实现）。
  鼠标观察代码在 `hexa/core/mouse_ledger.py`，全部只读实现，可直接审

## 隐私与数据流向

- 全程本地运行，无遥测无上报；网络访问仅限本机 LCU / Live Client API
  （只读）与你配置的模型端点；OP.GG 抓取只发生在手动执行的数据脚本里
- AI 开启时会把本局阵容与出装快照发送到你配置的模型端点；不开启 AI 则零数据
  外发
- 每次海克斯卡片点击会在 `log/captures/` 落一张定格截图（排障用，默认开启，
  设置 `click_capture` 可关）——分享日志目录前注意其中含游戏画面截图
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
uv run python scripts/build_kb_tagger.py        # 海克斯语义标签增量打标
```

## 打包

> 已知问题（修复排在真机稳定之后）：PyInstaller 6.x 把 `--add-data` 放进
> `_internal/` 子目录而代码取 exe 目录 → 数据资产落空；提权重启依赖
> `.venv/pyvenv.cfg`，打包后不存在 → 永远普通权限。**exe 打包当前不可用，
> 请以源码方式运行。**

```bash
uv pip install pyinstaller
uv run python build.py        # → dist/HexARAMHelper/HexARAMHelper.exe
```

杀软误报预案：热键为纯 ctypes 实现（GetAsyncKeyState 50ms 轮询），不装全局
键盘钩子；F9 撤销记账另带前台窗口校验。个别杀软仍可能对 mss 截图 / psutil
进程扫描误报，可将程序目录加入信任。

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
