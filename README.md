# vagent

面向视频创作的 Agent 实习项目，使用 **Python + DeepSeek + LangGraph**，自主设计上下文、项目记忆、Skills、工具执行与护栏，再接入视频生成模型。

当前已实现可运行的 **Python CLI Agent 原型**。DeepSeek 适配接口已完成，尚未配置真实 Key 联调；现有验证使用离线模拟模型和模拟 HTTP。当前交付文本创作材料，还未接入视频生成 API。

## 当前进度

| 部分 | 状态 | 已交付内容 |
|---|---|---|
| 01 Agent 核心 | 已实现 | 自定义 LangGraph model/tools 循环、DeepSeek 适配、CLI、项目与产物工具、版本化存储、执行限额 |
| 02 上下文与 Skills | 已实现 | 稳定前缀、确定性排序、紧凑 JSON、输入预算、完整轮次裁剪、项目事实注入、按需读取 SKILL.md、内容版本记录 |
| Python 迁移 | 已实现并通过本地验证 | Python 源码与 pytest 测试、pip 安装、wheel 打包、Python CI；替换原 TS/Node 工程 |
| 03 持久图恢复 | 已实现 | SQLite LangGraph 检查点、显式 resume、累计预算与工具重放保护 |
| 03 用量观测 | 已实现 | 单次模型调用记录、缓存命中/未命中 Token、加权命中率、未知用量标记 |
| 03 Redis 回答缓存 | 已实现 | 显式只读模式、最终文本精确匹配、TTL、故障回退、独立命中统计 |
| 03 任务评测 | 待实现 | 真实模型固定任务集与策略对比 |
| 04 Web 与视频工具 | 待实现 | 最小本地 Web、模拟视频 Job、真实供应商接入 |

每个独立完成的代码部分都同步更新本 README、提交并推送 GitHub。阶段目标见 [M1 计划](./M1_PLAN.md) 和 [Harness 设计](./AGENT_HARNESS_DESIGN.md)。

## 快速开始（Windows PowerShell）

要求 **Python 3.11 或更新版本**，本地验证使用 Python 3.12。当前 Agent 不需要 Node.js 或 npm。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .

# 将演示数据放在当前项目的被忽略目录中。
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\python.exe -m vagent demo
.\.venv\Scripts\python.exe -m vagent inspect --session demo
.\.venv\Scripts\python.exe -m vagent skills list
```

这些命令无需激活虚拟环境，也无需修改 PowerShell 执行策略。若 `python` 打开 Microsoft Store，请安装 Python 3.11+，或用已有解释器的完整路径执行第一条命令。

macOS/Linux 使用 `python3 -m venv .venv`，再将 `.\.venv\Scripts\python.exe` 换为 `.venv/bin/python`。也可激活虚拟环境后直接使用 `vagent` 命令。只安装运行依赖可用 `python -m pip install .`；开发与 CI 使用 `requirements-dev.lock` 固定依赖版本。

`demo` 无需 Key、不会联网，也不消耗模型费用。它按固定测试逻辑读取 skill、读取项目并保存带“模拟”标记的方案，不能用于衡量 DeepSeek 的生成效果。

## 连接真实 DeepSeek

复制 `.env.example` 为 `.env`，在本机填入自己的 Key：

```dotenv
DEEPSEEK_API_KEY=你的真实Key
VAGENT_DEEPSEEK_MODEL=deepseek-flash
```

`.env` 已被 Git 忽略。`VAGENT_DEEPSEEK_KEY` 优先于 `DEEPSEEK_API_KEY`，已有环境变量不会被 `.env` 覆盖。

```powershell
.\.venv\Scripts\python.exe -m vagent config show
.\.venv\Scripts\python.exe -m vagent run '准备咖啡店短视频方案，面向上班族，暖色调，并保存。' --session coffee
.\.venv\Scripts\python.exe -m vagent run '改成雨夜氛围，保留受众设定和原版。' --session coffee
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
.\.venv\Scripts\python.exe -m vagent chat --session coffee
```

`chat` 中输入 `/exit` 退出，Ctrl+C 停止当前进程的 Agent。`run` 和 `chat` 会发起真实请求并产生 API 费用。候选模型 `deepseek-flash` 可配置，账户权限与线上兼容性仍需真实 Key 验证；首版关闭 thinking。

`run` 支持 `--request-id`：同一会话相同 ID、相同需求返回已有运行记录；相同 ID 对应不同需求会报错。失败与中断任务也不自动重跑；继续原执行使用 `resume`，发起独立的新尝试才使用新 ID。

## 从检查点继续

每次执行现在会将 LangGraph 检查点保存到数据目录的 `checkpoints.sqlite`。CLI 在开始执行时输出 Run ID，`inspect` 也会列出运行 ID、状态、`resumable`、原始预算和已用活动时间。

```powershell
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
# 将下面的 RUN_ID 替换为 inspect 或执行输出中的实际 ID。
.\.venv\Scripts\python.exe -m vagent resume RUN_ID
```

- 进程中断、用户取消，以及网络、鉴权、限流等可恢复失败，可以显式继续。恢复真实模型请求仍可能产生 API 费用；没有自动重试或后台续跑。
- 继续沿用同一 Run ID、request ID、模型步数、工具次数和活动时间预算；即使新 Runner 设置了更高限额，原 Run 也不会获得新预算。预算耗尽和非法协议等不可恢复错误会结束 Run。
- 图以 Run ID 作为 `thread_id`，每个节点的检查点提交完成后才进入下一个节点。工具操作 ID 由 Run ID、模型步骤和工具调用 ID 组成；重复进入工具节点时复用已经提交的结果，未完成的工具才实际执行。
- 已完成的 Run 返回原结果；图已完成但会话快照尚未提交时，从检查点补齐会话，不再次调用模型。
- 只允许恢复当前会话最近的 Run；如果已提交更新的需求，旧 Run 会被拒绝。模型、工具 Schema、系统规则、Skill 版本或上下文预算变化时，也会要求恢复原配置。可以修正 API Key，Key 不属于检查点或配置指纹。
- 旧版 Run 没有图检查点，不能追溯恢复；原有成功对话、项目和产物仍可继续使用。检查点缺失或损坏时会报告错误，保留文件，不退回从头执行。

活动时间在正常停止时按实际耗时累计，离线时间不计入。模型请求发出前预留最多 60 秒（不足时使用剩余预算）；如果请求中途进程被强制终止，重启时按这份预留额保守计时，因为无法确定实际执行了多久。该扣除只发生一次。未取得响应的请求可能已被供应商计费，但无法凭空补出其 Token usage。

断电或强退可能留下 `instance.lock`，仍需确认原进程已经退出后手动移除该锁，再执行 `resume`。恢复验证记录见 [持久恢复验收](./docs/RECOVERY_ACCEPTANCE.md)。

## 查看模型与缓存用量

执行 `run`、`chat`、`resume` 后会显示用量摘要。`inspect` 中的每个 Run 也增加 `usage` 汇总。查看单次调用明细可使用以下命令，不需要 Key，也不会调用模型：

```powershell
.\.venv\Scripts\python.exe -m vagent usage --session coffee
.\.venv\Scripts\python.exe -m vagent usage --run RUN_ID
```

`usage` 只显示运行标识、模型、状态及用量，不输出对话和产物正文。原始调用明细保存在 `state.json` 的 `runs[runId].modelCalls` 中，汇总从这些明细计算。

| 字段 | 含义 |
|---|---|
| `modelCallCount` / `recordedCallCount` | 模型适配器调用尝试数 / 已有明细的调用数，包含失败与中断；存在旧版未跟踪步骤时前者为 null |
| `observedInputTokens` / `observedOutputTokens` | 已取得响应中可确认的输入 / 输出 Token 累计值，保留原有历史总量 |
| `cacheHitTokens` / `cacheMissTokens` | 已知缓存命中 / 未命中输入 Token 总量；未取得该项数据时为 null |
| `cacheHitRate` | 有完整缓存数据的调用中，命中 Token 总量 ÷（命中 + 未命中 Token 总量）；不是各次命中率的简单平均 |
| `callsWithTokenUsage` / `callsWithCacheUsage` | 有完整 Token / 缓存数据的调用数，用于判断统计覆盖率 |
| `tokenUsageComplete` / `cacheUsageComplete` | 所有调用是否都有对应的完整统计；失败、中断或旧版缺失明细会使相应值为 false |
| `untrackedModelSteps` | 旧版没有单次调用明细的模型步数，不能当作已确认的实际请求数 |

每条调用记录包含所属模型步骤、开始/结束时间、耗时、`started/responded/failed/cancelled/interrupted` 状态、上述 Token 数及错误码。`responded` 仅表示适配器返回了响应；即使工具协议随后校验失败，该响应已报告的 Token 仍计入统计。

DeepSeek 原始 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens` 优先使用；只有标准 `cache_read` 等单边数据且输入总数已知时，由总数减去已知项推导另一项，标记 `cacheUsageSource=derived`。缺失为 `missing`，负数或相互矛盾的缓存数据为 `invalid`，不伪装成 0 命中。总缓存输入为 0 时命中率为 null。

未知用量不会被当成免费调用；当前总量只反映已知部分。强退前尚未返回的请求会标为 interrupted，用量和准确结束时间保留未知。重复 request ID、重复恢复已完成 Run 或工具节点重放不会重复计入已有调用；重新请求模型会新增调用记录。旧 Run 不回填虚构缓存数据，恢复后从新的模型步骤开始记录。

这里统计的是服务商报告的上下文缓存用量，不是本地 Redis 命中数，也不代表减少了上下文窗口占用。当前未加入价格表或人民币费用估算。验证范围见 [用量观测验收](./docs/USAGE_ACCEPTANCE.md)。

## Redis 回答缓存（任务四）

默认关闭。需要 Redis 服务，并在 `.env` 中设置连接地址；连接凭据不会显示在 `config show` 或保存到 Run。使用前先更新依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
```

```dotenv
VAGENT_REDIS_URL=redis://localhost:6379/0
VAGENT_CACHE_TTL=3600
```

```powershell
.\.venv\Scripts\python.exe -m vagent run '阅读现有方案，解释镜头节奏。' --session coffee --read-only
.\.venv\Scripts\python.exe -m vagent chat --session coffee --read-only
.\.venv\Scripts\python.exe -m vagent usage --session coffee
```

`--read-only` 仅提供 `project_read`、`artifact_read`、`skill_read`；后端同样拒绝写工具，即使模型尝试调用也不能修改项目、计划或产物。会话与执行记录仍正常持久化。`resume` 自动沿用原 Run 的模式。没有 Redis 配置时只读模式也能使用；普通写作 Run 和离线 demo 不使用回答缓存。

缓存以**单个模型步的最终纯文本响应**为单位；工具调用响应、空回复、非法协议及明确截断的响应不缓存。命中跳过该步模型请求，仍正常完成图检查点与会话提交，不复用旧 Token usage。之前已经发生的模型调用与读取工具仍保留。缓存不是整个 Agent 工作流或工具操作的重放机制。

键以 SHA-256 摘要覆盖数据目录、项目身份及完整事实、全部项目产物版本与内容、实际发送的消息及工具定义、系统与 Skill 版本、上下文格式和模型配置。不同数据目录或项目隔离；任一依赖变化都会生成不同键。Redis 仅保存版本号和回答文本，默认 1 小时过期，TTL 可设为 1～604800 秒；单条回答限制 256 KiB。

这是严格精确匹配，没有语义相似度匹配。**重复输入同一句话不保证命中**：正常连续对话会增加历史，工具调用 ID 和消息元数据变化也会影响键。实际收益需要真实重复请求数据验证；未声称已测得生产 Token 节省。

每次 Redis 读写最多等待 0.5 秒；连接失败、超时或损坏条目会回退到模型请求，写缓存失败不丢弃已经取得的回答。原 Run 的取消与总时间预算继续生效。Redis 是可丢弃缓存，不替代 SQLite 检查点和 JSON 项目记忆，也不提供并发请求合并。

`usage.answerCache` 单独累计 `hit`、`miss`、`invalid`、`error`、`stored`、`skipped`，未发生的项省略；`error` 包含读失败和写失败，并非模型失败数。命中不会新增 `modelCalls`、`modelSteps` 或输入/输出 Token，不能将旧响应 Token 当作本次服务商缓存用量。执行时也显示 Redis 事件。验收证据见 [Redis 缓存验收](./docs/REDIS_CACHE_ACCEPTANCE.md)。

## 实现与代码入口

```text
src/vagent/
  cli.py       argparse 命令行与配置展示
  config.py    环境变量与 Key 校验
  models.py    DeepSeek 单步适配、离线模拟模型
  runner.py    自定义 LangGraph 图、预算、取消与事件
  checkpoints.py SQLite 异步检查点与连接生命周期
  journal.py   跨恢复保留的执行预算、使用量与会话提交
  usage.py     Token 与缓存用量归一化、覆盖率与 Run 汇总
  cache.py     Redis 精确回答缓存、TTL、超时与故障回退
  context.py   上下文组装与完整轮次裁剪
  tools.py     工具注册、Pydantic 校验、项目与产物操作
  storage.py   单写锁、事务、原子替换、操作日志
  skills.py    技能发现、元信息与按需正文读取
skills/        内置 SKILL.md，随 wheel 分发
tests/         pytest 行为与协议测试
```

LangGraph 提供图执行底座，项目自己定义状态、路由、上下文策略、工具边界、版本控制和运行记录。`ChatDeepSeek.bind_tools(...).ainvoke(...)` 只执行单个模型步，模型不会直接执行工具。

- **工具白名单**：`project_read`、`project_update`、`plan_update`、`artifact_save`、`artifact_read`、`skill_read`。不开放 shell 或任意文件路径。
- **Pydantic Schema**：工具声明与后端输入校验共用定义，拒绝未知字段和错误参数类型；工具错误会返回模型。
- **项目记忆**：保存目标、受众、风格、约束和计划；产物以稳定 ID 保存，每次修改增加版本并保留原文。
- **写入保护**：单写进程锁、串行事务、同目录原子替换；业务修改和操作结果一起提交。相同操作 ID 复用结果，参数冲突则拒绝。
- **运行限制**：默认最多 8 个模型步、12 次工具调用、180 秒，显式恢复沿用累计预算；超时取消异步模型请求，停止后不启动新工具。已完成的本地写入保留。
- **日志边界**：不直接输出供应商异常或参数校验中的原始输入值。配置展示只返回 Key 是否存在。

去重仅针对同一请求/操作 ID，不承诺不同 ID 之间的语义去重。JSON 存储面向单用户小规模使用，同步本地文件写入不是可抢占的异步任务。

## 上下文与 Skills

新 Run 默认使用上下文 v2。每个模型步按以下顺序组装系统消息，再追加近期对话：

1. 固定系统规则。
2. 按名称排序的 Skill 名称、描述和内容版本，以及固定的读取指引。
3. 最新项目事实。

工具定义作为独立的 `tools` 参数传给模型，按函数名排序，并固定对象键顺序。项目更新不会改动前面的规则与 Skill 元信息，有利于复用服务商的相同输入前缀；最终缓存行为由服务商决定。

项目事实和 Skill 元信息使用紧凑、键顺序稳定的 JSON。发送给模型的应用工具结果只去掉 JSON 字符串外的冗余空白，保留字符串正文、转义、数值原文及工具调用 ID；存储中的原始对话和产物不改写。

`VAGENT_CONTEXT_BYTES` 默认 65536，按消息和工具定义的 UTF-8 序列化字节数衡量，**不是精确 Token 数或供应商请求体大小**。供应商返回的 Token usage 另行记入 Run。

超限时裁剪最旧的完整用户轮次，保留工具调用与结果配对；原始历史不删除。当前需求、当前轮工具结果和项目事实必须完整保留，仍超限则在调用模型前报 `CONTEXT_LIMIT`。长产物通过 `artifact_read` 的 offset/limit 分段读取。

Run 保存 `contextVersion`，`inspect` 和 `usage` 可查看。已有检查点 Run 缺少该字段时按 v1 恢复，沿用原来的布局、空白和配置指纹；后续新 Run 使用 v2。未知格式版本会在恢复前报错。无检查点的早期 Run 仍不能恢复。对比数据和验证范围见 [上下文优化验收](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)。

内置 [video-brief](./skills/video-brief/SKILL.md) 和 [shot-description](./skills/shot-description/SKILL.md)。启动时校验并保留内容快照，初始上下文仅包含名称、描述和内容版本；`skill_read` 后才加入正文。

自定义技能目录结构为 `skills/your-skill/SKILL.md`：

```markdown
---
name: your-skill
description: 说明哪些任务需要这个技能。
---

使用现有工具完成任务的操作指引。
```

名称必须与目录一致，最多加载 32 个技能，每个文件最多 16 KiB。`VAGENT_SKILLS_DIR` 可指定可信本地目录，替代内置目录；修改后重启加载。不支持自动执行脚本、联网安装或热更新。Skill 不能增加工具权限或跳过代码校验。

## 数据兼容与限制

默认数据目录为 `~/.vagent/`，可用 `VAGENT_HOME` 覆盖。`state.json` 保存成功会话、项目、产物、Run 与操作结果；`checkpoints.sqlite` 保存图执行位置和消息。两者共同用于恢复，应在程序退出后一起备份和迁移。配置 Key 不写入状态。`inspect` 会显示创作正文。

Python 版保留 schema v1 的领域数据结构和工具 JSON 字段（如 `artifactId`、`expectedVersion`），并验证了读取原 TS 消息格式的兼容行为。已有项目可以继续使用原数据目录；无效或不支持的状态文件会报错并保留原文件。

- 成功会话可跨重启继续；未完成 Run 重启后标记为 `interrupted`，已提交产物保留，不自动重放。
- 应用快照和 SQLite 检查点分别提交，依靠同步图检查点、操作幂等和恢复时补交会话处理提交间隙；并非跨 JSON/SQLite 的单一数据库事务。
- 异常断电可能留下 `instance.lock`；确认没有进程使用该数据目录后才手动移除，程序不会自动抢锁。
- 回复整段显示，工具事件实时输出；逐 Token 流、自动摘要、跨项目偏好记忆、Web 和视频任务尚未实现。

后续视频服务通过注册工具接入独立 Job 服务和供应商适配器。Agent Run 与视频 Job 分开记录；长任务状态、付费提交与恢复策略独立实现，结果不确定的付费提交不能盲目重试。当前只需要 DeepSeek Key，视频服务 Key 在真实视频阶段单独配置。

## 验证与打包

任务四新增验证：真实 Redis 重连命中、TTL 失效与服务中断回退；独立 wheel 环境中的仓库外 CLI 与依赖检查通过。详见 [Redis 缓存验收](./docs/REDIS_CACHE_ACCEPTANCE.md)。

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/python
```

本地 Python 3.12 测试结果：**109 passed，1 skipped**。跳过的是当前 Windows 账户无符号链接创建权限的测试。测试覆盖工具闭环、请求去重、错误与超时、取消、并发拒绝、版本/项目隔离、写入回滚、旧状态读取、上下文预算、Skills、DeepSeek HTTP 协议，以及真实子进程强退后的恢复、部分工具提交、累计预算、终态补交、缓存 Token 字段、缺失用量与加权命中率。上下文 v2 另验证了稳定前缀、确定性排序、无损 JSON 空白压缩、完整协议配对、分页读取及旧版上下文恢复兼容。

已验证离线 CLI，以及 wheel 安装到独立虚拟环境后在仓库目录之外运行 `skills list`、`demo`、`inspect`。本次新增验证：新建独立虚拟环境安装 wheel，保存产物后取消，再从仓库外通过 CLI `resume` 完成原 Run，仍仅有一个产物；`pip check` 通过。源码包与 wheel 仅本地构建，未发布 PyPI。GitHub Actions 配置 Ubuntu/Windows、Python 3.11/3.12 检查，远端结果见 [Actions](https://github.com/shifang37/vAgent/actions)。

用量观测版本的 wheel 也通过独立虚拟环境验证：从仓库外恢复离线 Run 后执行 `usage --run`，显示累计 4 次适配器调用，并将未报告的 Token 和缓存明细保留为未知。

上下文 v2 的 wheel 已在同一独立环境重新安装验证：仓库外执行 `skills list`、取消后 `resume`、`inspect`、`usage --run` 均通过，格式版本保持 2、最终仍仅有一个产物，`pip check` 通过。v1 检查点兼容由自动化测试覆盖，详细记录见 [上下文优化验收](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)。

这些测试验证工程行为，不代表真实模型任务成功率。后续按顺序完成真实 DeepSeek 联调与任务评测、最小 Web、模拟视频 Job、真实视频模型接入。
