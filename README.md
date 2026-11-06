# vagent

[![CI](https://github.com/shifang37/vAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/shifang37/vAgent/actions/workflows/ci.yml)

**vagent** 是一个面向视频创作的本地 Agent：用自然语言指挥它完成「剧本 → 分集 / 分场 / 分镜 → 调用视频模型生成 → 字幕 / 配音 / 混音 / 转场 → 整集成片」。它用 **Python + DeepSeek + LangGraph** 自主实现上下文、项目记忆、Skills、工具执行与费用护栏，再接入真实的视频生成与语音合成服务。

当前版本 **v0.6.1**（2026-11-04）。安装为 Python 包后以 `vagent` 命令运行，提供 CLI、本地 Web 工作台、无头两阶段授权流程和只读 MCP。所有数据和 Key 都留在本机。

> 一条 16:9 横屏 720p 的 AI 短剧流水线：从契约冻结到可复现的成片交付，每一步的证据都写进 `docs/`。

## 它能做什么

```text
一句话 / 剧本
   │  DeepSeek 编排（LangGraph 工具循环、项目记忆、Skills、预算）
   ▼
分集 → 分场 → 分镜 → 镜头            episode_plan / storyboard_save / shot_generate
   │  显式确认 + 三重费用上限 + 每 Job 一次提交
   ▼
视频生成（万相 t2v / i2v / 首尾帧；Kling、fal 受控接入）
   │  独立 Worker 查询、下载、本地校验、Range 播放
   ▼
字幕对齐 · CosyVoice 配音 · 混音响度 · 转场 / 标题卡 · 720P/1080P 渲染
   │  包内 FFmpeg，全部本地完成，不产生供应商费用
   ▼
整集成片 · 季 / 批量 · 只增不改的时间线工作台 · 报价对比与手工账单
```

- **Agent 核心**：自定义 LangGraph 状态图，Pydantic 工具 Schema，固定的模型步 / 工具次数 / 活动时间预算，SQLite 检查点与显式 `resume`，上下文 v2 稳定前缀与完整轮次裁剪，可选 Redis 精确回答缓存。
- **创作记忆与质量**：项目目标 / 受众 / 风格 / 约束分字段保存，产物带版本；检查记忆字段冲突与旧事实残留，正文字数上限持久化并在保存前实际计数。
- **视频生成**：统一的 Job 模型，`off` / `mock` / `live` 三种模式；万相 2.7 文生、图生与首尾帧视频真实跑通；Kling 直连与 fal 中转按官方文档接入（受控 HTTP 验证，尚无真实付费样本）。
- **多镜头与分集**：结构化分镜、素材库参考图、角色 / 风格一致性、确定性剧本改编（`text-split-zh-v1`）、整集批量登记、逐镜头重做、集级额度跨 Run 累计。
- **成片后期**：本地流复制拼接与自动归一化、`subtitle-align-zh-v1` 字幕对齐与软 / 烧录导出、百炼 CosyVoice v2 配音与音色绑定、`mix-zh-v1` 混音与两遍 `loudnorm`、`cut/fade/crossfade` 转场、标题卡、720P/1080P 导出。
- **多集与工作台**：季（有序分集成员）、1–3 集批量队列（逐集原子、失败隔离、显式恢复）、每集一条只增不改的时间线（重排、裁剪、转场、字幕、音轨、标题卡）与客户端预览。
- **供应商运维**：只读报价对比（`provider-route-v1`，不换汇）、逐供应商提交间隔与 429 冷却、手工录入实际账单的侧车账本（`billing-v1`）。
- **交付形态**：`vagent` CLI、本地 Web 工作台（原生 JS，同源 API + SSE）、`vagent headless` 两阶段授权、只读 MCP stdio 服务、随 wheel 分发的内置 Skills。

## 项目状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | Agent 基础、模拟视频工具与持久等待、万相真实单镜头 | 完成 |
| M2 | 结构化分镜、多镜头批量、费用三重上限、素材库、逐镜头重做 | 完成 |
| M3 | 本地拼接成片、Kling / fal 多供应商、headless、用户 Skills | 完成，v0.3.0 |
| M4 / M5 | 时间线归一化转码；剧本改编、角色一致性、分集工作流 | 完成，v0.4.0 |
| M6 | 字幕、配音、混音、转场 / 标题卡、整集渲染 | 完成 |
| M7 | 季、批量队列、时间线工作台 | 完成，v0.5.0 |
| M8 | 报价对比、提交节流、手工账单、真实样本、发布 | 完成，v0.6.0 |
| K0–K3 | 两 Key 直用：设置页最小表单、零费用探测、免重启生效 | 完成 |
| M9 | 9:16 竖屏 | **暂缓**。E0 契约与基线已冻结保留，E1 起未开始 |

**v0.6.1 是 v0.6.0 之后的补丁版本**：收纳 Kling 3.0 Turbo 适配器的字段与错误码对齐（含错误路径上的崩溃级修复）、零费用契约回放校验器，以及混音静音快速失败。存储、工具面与全部契约基线逐字节不变，不新增付费样本。

每个里程碑按固定节奏推进：**E0 冻结契约与机器可读基线 → E1–E3 实现 → E4 离线评估套件 + 安装态 wheel smoke + 受控浏览器 + 四组 CI**。契约、基线快照与验收证据全部在 [`docs/`](./docs)。

横屏 720p 的产品域已在 M1–M8 闭环，当前不新增功能，剩余工作属于证据与体验（真实样本、账单核对）。2026-10-29 产品方确认本轮只用横屏，竖屏方向保留 [M9 契约](./docs/M9_CONTRACTS.md) 供将来续做。

## 快速开始

要求 **Python 3.11+**（本地验证使用 3.12）。运行 Agent 不需要 Node.js。

```powershell
git clone https://github.com/shifang37/vAgent.git
cd vAgent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .

# 把数据放在当前目录的被忽略目录中，而不是 ~/.vagent
$env:VAGENT_HOME = Join-Path $PWD '.vagent'

# 无需 Key、不联网的离线演示
.\.venv\Scripts\python.exe -m vagent demo
.\.venv\Scripts\python.exe -m vagent inspect --session demo
```

macOS / Linux 把 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`。只需要运行时依赖可以直接 `python -m pip install .`。

### 连接 DeepSeek

复制 `.env.example` 为 `.env`，填入自己的 Key（`.env` 已被 Git 忽略）：

```dotenv
DEEPSEEK_API_KEY=你的Key
VAGENT_DEEPSEEK_MODEL=deepseek-flash
```

也可以在 Web 设置页填写。配置优先级为 **内置默认值 < `VAGENT_HOME/config.yml` < 环境变量 / `.env`**。Key 只由本地后端持有，不进入状态文件、检查点、事件或浏览器。

```powershell
.\.venv\Scripts\python.exe -m vagent run '准备咖啡店短视频方案，面向上班族，暖色调，并保存。' --session coffee
.\.venv\Scripts\python.exe -m vagent chat --session coffee
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
```

`run` 和 `chat` 会发起真实请求并产生 API 费用。

### 启动 Web 工作台

```powershell
.\.venv\Scripts\python.exe -m vagent web --mcp-local
```

打开 <http://127.0.0.1:3210>。服务只监听回环地址，校验 Host / Origin 与 CSRF Token，API 不返回 Key。缺少 Key 时自动打开配置向导。工作台包含会话对话、产物版本、分镜与镜头、成片与渲染、字幕、季、时间线、素材库、用量与账单等面板，详见 [Web 说明](./web/README.md)。

### 接入视频生成

视频工具默认关闭。`VAGENT_VIDEO_MODE=mock` 注册不联网的模拟供应商；`live` 需要对应供应商的 Key，并会产生真实费用。

| 供应商 | 接入方式 | 规格 | 计价 | 真实样本 |
|---|---|---|---|---|
| 万相 Wan 2.7（阿里云百炼，北京） | `VAGENT_DASHSCOPE_KEY`，可选业务空间 | 5 秒 / 720P / 16:9，t2v、i2v、首尾帧 | CNY，参考 3.00 元/条 | 已完成多镜头、分集与首尾帧样本 |
| Kling 3.0 Turbo（新加坡直连） | `VAGENT_KLING_API_KEY` | 5 秒 / 720P / 16:9 | USD + VIDEO_UNIT | 仅受控 HTTP，未做真实样本 |
| fal（Kling 中转，静音） | `FAL_KEY` | 5 秒 / 720P / 16:9 | USD | 仅受控 HTTP，未做真实样本 |
| CosyVoice v2（百炼 TTS） | 复用 `VAGENT_DASHSCOPE_KEY` | 48 kHz WAV，8 个已核实音色 | CNY，2 元/万字符 | 已完成 1 集 19 条台词 |

费用护栏在所有模式下一致：生成必须显式 `confirm`；每个 Job 冻结报价并受单 Job / 单 Run / 集级三重上限约束；每个 Job 至多提交一次，提交结果未知时不重投；CNY 与 USD 只并列展示、不换汇；实际账单只能手工录入，缺失时保持 `unknown` 而不是 0。完整环境变量见 [`.env.example`](./.env.example)，字段含义见 [使用手册](./docs/USER_GUIDE.md)。

## 命令行一览

| 命令 | 用途 | 是否调用模型 / 供应商 |
|---|---|---|
| `run` / `chat` / `resume` | 执行、对话、从检查点继续 | 调用 DeepSeek |
| `demo` / `inspect` / `usage` / `config` / `skills` | 离线演示、查看项目与运行、用量与账单、配置、Skills | 否 |
| `jobs` | 查看视频任务、恢复查询 / 下载、运行持久队列 | `jobs work` 推进已登记任务 |
| `storyboard` / `shots` / `assets` | 查看分镜、逐镜头重做、导入参考图 | 只登记本地任务 |
| `episode` / `season` | 剧本改编与分集、季成员与批量登记 | 只登记本地任务 |
| `film` / `subtitle` / `audio` / `timeline` | 拼接成片、字幕、BGM/SFX 导入、时间线保存与渲染 | 否，本地 FFmpeg |
| `route` | 只读报价对比 | 否 |
| `headless` | 计划 → 显式授权 → 等待 → 本地交付 | 规划阶段调用 DeepSeek |
| `web` | 启动本地 Web 工作台 | 按用户操作 |

每个子命令都支持 `--help`。逐命令的参数、错误码与边界见 [使用手册](./docs/USER_GUIDE.md)。

## 高级开关（默认关闭）

以下开关用于 Agent Harness 的可选能力（上下文摘要、长期记忆、提示词版本与追踪），默认关闭；在 `.env` 或环境变量中设置后重启生效。多数改动会进入 Run 的 `contextSignature`，因此恢复旧 Run 需要原配置；关掉即回到 v2 基线。

| 开关 | 默认 | 作用 | 对签名 / 恢复的影响 |
|---|---|---|---|
| `VAGENT_CONTEXT_VERSION` | `2` | `2` 稳定前缀；`3` 历史摘要；`4` 尾部动态消息（配合 `VAGENT_CONTEXT_TAIL=system\|human`） | 版本改变签名；旧 Run 需新请求 |
| `VAGENT_MEMORY` | `off` | 开启 `memory.json` 项目级长期记忆，召回已确认事实 | 开启后记忆块进入上下文并改变签名 |
| `VAGENT_MEMORY_LIMIT` | `4` | 每个项目最多渲染的记忆条数（1–16） | 改变上下文内容与签名 |
| `VAGENT_PROMPT_VERSION` | `v1` | `v1` 冻结；`v2` 追加数据通道硬化子句 | 模板文本进入签名，切换即拒绝恢复旧 Run |
| `VAGENT_TRACING` | `off` | 开启 `traces/*.jsonl` span 导出 | 不改变签名；默认不写文件 |
| `VAGENT_TRACE_SAMPLE` | `100` | 采样百分比（0–100） | 不改变签名 |
| `VAGENT_TRACE_CONTENT` | `off` | 是否把提示词 / 回答正文写入 span | 不改变签名；开启后 trace 含正文 |

`VAGENT_CLOSING_TURN`（缺省 `on`）控制预算耗尽后的无工具收尾轮，详见[使用手册](./docs/USER_GUIDE.md#高级开关默认关闭)。

### 离线评测与成本命令

- `vagent usage --cost`：在既有用量输出上追加参考价目表金额（默认不显示，不写状态）。
- `scripts/evaluate_behavior.py --home <空目录> --output <新路径> --k 2`：离线行为评测。`gate` 用例必须全样本通过，`measure` 只报告指标；`k` 不能超过样本数，`--live` 明确拒绝。
- `scripts/compare_cost.py --home <已有数据目录>`：只读快照对比缓存感知与缓存盲口径的文本模型费用；目录不存在直接报错。

## 架构概览

```text
vagent CLI ───┐                        ┌── Web 工作台（原生 JS，同源 API + SSE）
headless ─────┤                        │
MCP stdio ────┤                        │
              ▼                        ▼
       ApplicationService（CLI / Web 共用的生命周期与执行服务）
              │
   ┌──────────┼──────────────┬──────────────────┐
   ▼          ▼              ▼                  ▼
AgentRunner  JobWorker    MediaWorker     WaitCoordinator
LangGraph 图  串行提交/查询  下载/校验/落盘    持久等待续接
   │
   ▼
Store：state.json（schema 9，单写锁、事务、原子替换、逐字节备份迁移）
       checkpoints.sqlite（LangGraph 检查点） · media/ · billing.json
```

设计上的几条硬约束：

- **模型不直接执行任何东西**。工具参数经严格 JSON 解码与 Pydantic 校验后由后端执行；工具白名单固定，没有 shell 或任意文件路径。
- **只增不改**。产物、分镜、镜头、成片、字幕、时间线、季都以版本追加，旧版本与文件逐字节保留；历史 Job 与冻结报价不改写。
- **契约先行**。每个里程碑先冻结 Pydantic 契约、能力 / 价格表与错误码，并用基线 JSON 记录 168 个契约模型哈希；后续改动必须复现基线。
- **可恢复**。检查点、持久等待、Job 查询窗口与媒体下载额度都跨重启保留；恢复沿用原预算，不自动重发已尝试过的模型请求。
- **本地优先**。拼接、字幕、混音、渲染全部用包内 FFmpeg 在本机完成；Web 只监听 `127.0.0.1`。

主要代码入口：

```text
src/vagent/
  cli.py · web.py · headless.py · mcp_server.py · mcp_bridge.py   入口与接口层
  application.py                                               CLI/Web 共用的生命周期与执行服务
  runner.py · checkpoints.py · journal.py · context.py · tools.py  LangGraph 图、检查点、预算、上下文、工具
  models.py · http.py · cache.py · usage.py · quality.py          DeepSeek 适配、重试、Redis 缓存、用量、质量校验
  storage.py · skills.py · config.py · contracts.py               存储与迁移、Skills、配置、严格 JSON 类型
  waiting.py · wait_runtime.py                                    持久等待与恢复协调
  video/
    contracts.py · jobs.py · worker.py · providers/               Job 模型、登记、提交/查询、wan/kling/fal/mock 与提交闸门
    media*.py · mp4.py                                            下载、校验、落盘、Range 响应
    storyboards.py · assets.py · episodes.py · adaptation.py · consistency.py   分镜、素材、分集、改编、一致性
    films.py · film_service.py · assembler.py · copy_signature.py  拼接成片与归一化
    subtitles.py · subtitle_service.py · subtitle_font.py          字幕对齐、导出、包内 OFL 字体
    audio.py · tts.py · audio_service.py · mixer.py · audio_import.py   TTS、混音、BGM/SFX 导入
    renders.py · render_service.py · timelines.py                  转场/标题卡渲染、时间线
    seasons.py · batches.py · routing.py · billing.py · usage.py   季、批量、报价对比、账单、用量聚合
web/        工作台页面与 10 个纯函数状态模块（*-state.js）
skills/     内置 SKILL.md（video-brief、shot-description），随 wheel 分发
scripts/    evaluate_m*.py 离线验收套件、capture_m*_baseline.py 基线快照、verify_provider_contract.py 供应商契约回放、wheel_smoke.py 安装态检查
tests/      139 个 pytest 文件与 10 个 Node 状态测试
```

设计依据见 [产品设计](./DESIGN.md)、[Agent 选型与 Harness 设计](./AGENT_HARNESS_DESIGN.md) 与 [M1 实施计划](./M1_PLAN.md)。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/test_job_state.mjs          # 其余 9 个 *-state 测试同理，见 .github/workflows/ci.yml
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/python
.\.venv\Scripts\python.exe scripts/wheel_smoke.py   # 在临时环境安装 wheel 并跑安装态套件
```

| 检查 | 最近结果（`master`，v0.6.1 发布前） |
|---|---|
| 全量 pytest | 1,522 passed / 4 skipped，另有 1 例已登记的偶发失败（skip 为平台条件：Windows 符号链接权限 3 例、POSIX 文件模式 1 例） |
| Ruff check / format | 234 个文件通过 |
| 前端 Node 测试 | 10 个状态模块 91 项 |
| CI 矩阵 | ubuntu / windows × Python 3.11 / 3.12 四组，实时状态见页首 badge |

离线验收套件 `scripts/evaluate_m2.py` … `evaluate_m8.py` 默认不联网，用受控上游与本地测试媒体走真实生产代码路径；`--live` 会被明确拒绝或需要单独授权。全量回归约 21–25 分钟。已知两类偶发，均按「记录而不掩盖」处理：一是与顺序相关的时间线混音静音（`tests/test_m7_timeline_render.py`，单跑必过），已加固为确定性诊断，见 [M9 计划 §3.0](./docs/M9_PLAN.md)；二是 Windows 上 M5 离线评估偶发超出夹具等待上限，评估侧的 300 秒已放宽到 600 秒，见 [M5 验收](./docs/M5_E4_ACCEPTANCE.md)。

## 文档索引

| 类型 | 文档 |
|---|---|
| 使用手册 | [USER_GUIDE](./docs/USER_GUIDE.md)（CLI 细节、headless、恢复、用量、Redis、Skills、数据兼容）· [Web 说明](./web/README.md) · [MCP 接入](./docs/MCP.md) |
| 设计 | [DESIGN](./DESIGN.md) · [AGENT_HARNESS_DESIGN](./AGENT_HARNESS_DESIGN.md) · [M1_PLAN](./M1_PLAN.md) |
| 计划 | [M1-B](./docs/M1B_PLAN.md) · [M1-C](./docs/M1C_PLAN.md) · [M2](./docs/M2_PLAN.md) · [M3](./docs/M3_PLAN.md) · [M5](./docs/M5_PLAN.md) · [M8](./docs/M8_PLAN.md) · [M9](./docs/M9_PLAN.md) · [两 Key 直用](./docs/KEYS_ONLY_PLAN.md) |
| 契约 | [M1-B](./docs/M1B_CONTRACTS.md) · [M1-C](./docs/M1C_CONTRACTS.md) · [M2](./docs/M2_CONTRACTS.md) · [M3](./docs/M3_CONTRACTS.md) · [M5](./docs/M5_CONTRACTS.md) · [M6](./docs/M6_CONTRACTS.md) · [M7](./docs/M7_CONTRACTS.md) · [M8](./docs/M8_CONTRACTS.md) · [M9](./docs/M9_CONTRACTS.md) |
| 阶段验收 | [M1-A](./docs/M1A_ACCEPTANCE.md) · [M1-B](./docs/M1B_ACCEPTANCE.md) · [M1-C](./docs/M1C_ACCEPTANCE.md) · [M2](./docs/M2_ACCEPTANCE.md) · [M3](./docs/M3_ACCEPTANCE.md) · [M4](./docs/M4_NORMALIZATION.md) · [M5](./docs/M5_E4_ACCEPTANCE.md) · [M6](./docs/M6_E4_ACCEPTANCE.md) · [M7](./docs/M7_ACCEPTANCE.md) · [M8](./docs/M8_ACCEPTANCE.md) · [K3](./docs/KEYS_ONLY_K3_ACCEPTANCE.md) |
| 专项验收 | [编排](./docs/AGENT_ORCHESTRATION_ACCEPTANCE.md) · [质量校验](./docs/QUALITY_ACCEPTANCE.md) · [持久恢复](./docs/RECOVERY_ACCEPTANCE.md) · [用量观测](./docs/USAGE_ACCEPTANCE.md) · [上下文优化](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md) · [Redis 缓存](./docs/REDIS_CACHE_ACCEPTANCE.md) · [i2v / flf2v 样本](./docs/M2_I2V_ACCEPTANCE.md) · [Kling 对齐与契约校验](./docs/KLING_ALIGNMENT.md) |
| 发布说明 | [v0.6.1](./docs/RELEASE_0.6.1.md) · [v0.6.0](./docs/RELEASE_0.6.0.md) · [v0.5.0](./docs/RELEASE_0.5.0.md) · [v0.4.0](./docs/RELEASE_0.4.0.md) · [v0.3.0](./docs/RELEASE_0.3.0.md) |
| 外部依据 | [万相 API 核实](./docs/VIDEO_API_NOTES.md) · [第三方组件与分发材料](./docs/THIRD_PARTY_ASSEMBLY.md) |

每个 `docs/M*_E*_ACCEPTANCE.md` 记录对应子阶段的范围、复现命令、回归数字与保留的边界；`docs/M*_E0_BASELINE.json` 是机器可读的契约基线。

## 已知限制

- 只支持 **16:9 横屏 720p**（1080P 为参数化上采样）。9:16 竖屏已冻结契约但未实现。
- Kling 与 fal 只有按官方文档的受控 HTTP 接入，没有真实付费样本；能力表标注「文档支持，未做真实样本」。
- 万相与 CosyVoice 的真实样本给出的是产品口径估算，供应商实际账单未核对，账本依赖手工录入。
- 单用户、单写进程的本地工具：同一数据目录同时只允许一个 CLI / Web 写进程；JSON 存储面向小规模使用。
- 自动摘要（`VAGENT_CONTEXT_VERSION=3/4`）与项目级长期记忆（`VAGENT_MEMORY=on`）都是显式 opt-in、默认关闭；不做跨项目偏好记忆、发送后的自动重投或换汇。
- 单次模型请求预留并限制在 60 秒（剩余预算不足时更短）；接近输出上限的长回复可能因此被截断，见 [使用手册](./docs/USER_GUIDE.md)。
- 未发布到 PyPI，需从仓库构建安装；文本模型有参考价目表与按已记录用量的估算，不是供应商账单。
