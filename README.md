# vagent

面向视频创作的 Agent 实习项目，使用 **Python + DeepSeek + LangGraph**，自主设计上下文、项目记忆、Skills、工具执行与护栏，再接入视频生成模型。

当前已实现 **Python CLI + 本地 Web Agent**，前端通过同源 API/SSE 调用共享 LangGraph Runner。2026-10-08 完成真实 DeepSeek、持久记忆、Skills、8 种工具（含 2 个本地 MCP 工具）的编排验收，随后补齐记忆冲突与正文长度的后端校验。当前交付文本创作材料，尚未接入视频生成 API；首轮发现见 [编排测试报告](./docs/AGENT_ORCHESTRATION_ACCEPTANCE.md)，修复与验证范围见 [质量校验验收](./docs/QUALITY_ACCEPTANCE.md)。

任务 2 已补齐本地配置向导、CLI/Web 流式回复和 9 类固定评测。**2026-10-09 完成 A5：完整真实套件 9/9 通过，M1-A 已验收。** 最终套件包含 3 次显式恢复，累计 27 次模型调用、25 次工具调用；此前失败记录完整保留，详见 [M1-A 验收记录](./docs/M1A_ACCEPTANCE.md)。同日完成 M1-B 的 **B0–B4**：契约、持久 Job/Mock Worker、四工具、持久等待与恢复，以及 CLI/Web 入口。应用自动推进 Job，CLI 持续等待，页面显示模拟任务、来源版本和恢复查询；停止 Agent 与正常退出保持不同语义。详见 [B4 验收](./docs/M1B_B4_ACCEPTANCE.md)，下一步按 [M1-B 任务规划](./docs/M1B_PLAN.md) 执行 B5 的完整验收与真实 DeepSeek + Mock 联调。

## 当前进度

| 部分 | 状态 | 已交付内容 |
|---|---|---|
| 01 Agent 核心 | 已实现 | 自定义 LangGraph model/tools 循环、DeepSeek 适配、CLI、项目与产物工具、版本化存储、执行限额 |
| 02 上下文与 Skills | 已实现 | 稳定前缀、确定性排序、紧凑 JSON、输入预算、完整轮次裁剪、项目事实注入、按需读取 SKILL.md、内容版本记录 |
| Python 迁移 | 已实现并通过本地验证 | Python 源码与 pytest 测试、pip 安装、wheel 打包、Python CI；替换原 TS/Node 工程 |
| 03 持久图恢复 | 已实现 | SQLite 检查点、显式 resume、外部等待自动继续、累计预算、停止竞争与工具重放保护 |
| 03 用量观测 | 已实现 | 单次模型调用记录、缓存命中/未命中 Token、加权命中率、未知用量标记 |
| 03 Redis 回答缓存 | 已实现 | 显式只读模式、最终文本精确匹配、TTL、故障回退、独立命中统计 |
| 03 任务评测 | A5 完整真实验收通过 | 9 类用例、独立状态评分、上下文版本/预算对比、用量覆盖率、显式检查点续跑 |
| 04 本地 Web | 已接入真实 Agent | 单 Key 配置向导、模型切换与验证、逐段文本流、断线补齐、停止/恢复、产物与编排观测 |
| MCP | 已实现并验证 stdio | 显式只读白名单、工具发现、Schema 校验、取消/超时、结果大小限制 |
| 内容质量校验 | 已通过回归与真实纠错验收 | 记忆字段职责、旧事实残留检查、持久字数上限、保存前计数、未纠正错误禁止报告完成 |
| M1-B 模拟视频 | B0–B4 已完成，B5 待实施 | 自动 Worker、四工具、持久等待/停止/恢复、CLI Job 命令与非阻塞输入、Job API/SSE/任务卡片；详见 [B4 验收](./docs/M1B_B4_ACCEPTANCE.md) |
| M1-C 真实视频 | 待实施 | 真实供应商接入、媒体下载/播放与真实视频验收 |

每个独立完成的代码部分都同步更新本 README、提交并推送 GitHub。阶段目标见 [M1 计划](./M1_PLAN.md) 和 [Harness 设计](./AGENT_HARNESS_DESIGN.md)。

## 本地 Web 与真实编排

保留 DeepSeek Harness 风格，已移除浏览器中的模板回复。会话、项目记忆和产物由 Python 服务保存；侧栏「编排观测」查看上下文字节预算、裁剪数、记忆 revision、Skills、MCP 与实际调用用量。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
# 可在启动后的设置页填写 Key，也可使用本地 .env。
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m vagent web --mcp-local --no-open
```

打开 [本地工作台](http://127.0.0.1:3210)。缺少 Key 时自动打开配置向导；保存不调用模型，「保存并验证」最多发送一次简短请求，可能产生少量费用。发送创作需求会调用真实 DeepSeek。`--mcp-local` 启用镜头时长和帧数计算服务；外部 stdio 配置见 [MCP 说明](./docs/MCP.md)。

服务只监听 `127.0.0.1`，校验 Host/Origin 和写请求 CSRF Token；静态资源使用白名单，API 不返回 Key。同一数据目录只允许一个 CLI/Web 写进程。文本流实时显示为草稿，完整回复通过检查后才持久保存；断线重连补齐当前文本，停止或失败时丢弃草稿。完整边界见 [Web 说明](./web/README.md)。

在工作台运行时，可显式执行固定任务验收（会产生模型费用）：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --live
# 仅重新检查已有产物，不调用模型：
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --review-existing
```

此前首组真实调用共 13 个模型步骤、17 次工具调用；输入 71,645、输出 3,498 Token。原始报告保存在被忽略的 `output/agent-live-acceptance.json`，可复现步骤和发现见 [验收记录](./docs/AGENT_ORCHESTRATION_ACCEPTANCE.md)。

新的评测无需启动 Web，使用同一个 ApplicationService/Runner 和内置 MCP。默认离线夹具只验证工程链路，不代表模型质量：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --output output/m1a-offline.json
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --context-version 1 --compare output/m1a-offline.json --output output/m1a-v1.json
# 显式真实请求；全套最多32次模型调用，任一用例失败立即停止：
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --live --max-model-calls 32 --output output/m1a-live.json
# 仅在最近失败仍可恢复时显式继续，原报告保留，预算不增加：
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --live --continue-from output/m1a-live.json --output output/m1a-resumed.json
```

每次新评测使用独立数据目录，报告记录路径。已有输出文件不会覆盖。可用 `--context-bytes` 比较预算；报告列出状态正确性、耗时、首段文字时间、模型/工具次数、已知 Token、缓存命中及统计覆盖率。离线或用量缺失时不计算 Token 差值，不将单组结果外推为节费收益。

本次通过的完整报告为 `output/m1a-a5-20261009-v2-r4-resumed-3.json`。最终 brief 三个版本为 286、261、113 字，storyboard 为 241 字，旧版保持不变；只读任务未修改项目或产物。最终套件 27 次模型调用中 24 次有完整用量，另外记录 8 次请求发送前的连接重试；四轮复验合计 55 次模型调用，失败与未知用量均纳入记录。

## 记忆一致性与字数上限

`goal` 只保存创作目的和核心信息，`audience` / `style` 分别保存受众和风格。更新时检查目标中的字段重复，以及目标/约束中残留的旧受众、旧风格；操作日志中的历史项目快照也用于检查旧数据。发现冲突返回 `MEMORY_CONFLICT`，整次更新不落盘、revision 不变。模型需要在同一次更新中修正相关字段；历史产物正文不会自动改写。

正文的“字数”统一采用**非空白 Unicode 字符数**：汉字、英文、数字、标点、Markdown 标记均计入，空格和换行不计，标题不计。支持明确的数字上限，例如：

```text
保存一份 brief，正文300字以内。
方案不超过100字，分镜不超过300字。
brief 改为500字以内。
brief 取消字数限制。
```

后端在接收写作请求时，把上限写入项目 `contentLimits` 并更新 revision；同一请求 ID 不会重复修改，只读请求不修改它。上限跨轮次、跨重启保留，普通工具不能放宽或删除；未指定种类的正文上限是项目默认值。标题、纯回复要求、代码块、引用行和 Web 标记的附件参考材料不用于设置正文上限。

`artifact_save` 在写入版本前计数。超限返回实际计数和 `CONTENT_LENGTH`，不创建产物或新版本；成功返回并保存 `contentCheck`。Web 展示上限、实际计数和保存时的校验记录。旧版本缺少记录时明确显示“未记录字数校验”，不会补造通过结果。

校验错误需由模型在原执行预算内纠正；若没有纠正便直接结束，Run 返回 `QUALITY_UNRESOLVED`，不采用模型的成功声明。固定任务评测重新读取正文计数，有质量问题时返回失败退出码。当前检查针对明确数字上限和字段原值的文本重复/残留，不承诺任意自然语言约束或同义改写的完整语义判断。详见 [质量校验验收](./docs/QUALITY_ACCEPTANCE.md)。

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

也可在 Web 设置中填写 Key 和模型。页面配置原子保存到 `VAGENT_HOME/config.yml`，优先级为**内置默认值 < 本地 config.yml < 环境变量/.env**，CLI 也读取同一文件。设置页显示来源；由启动配置或环境变量提供的字段不能在页面覆盖。页面保存对下一次请求生效；修改环境变量或 `.env` 后仍需重启服务。

密钥输入框始终留空，留空保持已存值；移除密钥需要单独操作。凭证不进入状态、检查点、事件或浏览器存储。本地配置是明文文件，POSIX 写入权限为 0600，Windows 使用所在目录的访问控制。运行或验证期间不能修改配置，验证期间不能启动新 Run；验证成功状态只在当前服务进程内保留。

```powershell
.\.venv\Scripts\python.exe -m vagent config show
.\.venv\Scripts\python.exe -m vagent run '准备咖啡店短视频方案，面向上班族，暖色调，并保存。' --session coffee
.\.venv\Scripts\python.exe -m vagent run '改成雨夜氛围，保留受众设定和原版。' --session coffee
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
.\.venv\Scripts\python.exe -m vagent chat --session coffee
```

`chat` 中输入 `/exit` 退出，Ctrl+C 停止当前进程的 Agent。`run` 和 `chat` 会发起真实请求并产生 API 费用。候选模型 `deepseek-flash` 可配置，账户权限与线上兼容性仍需真实 Key 验证；首版关闭 thinking。

`run` 支持 `--request-id`：同一会话相同 ID、相同需求返回已有运行记录；相同 ID 对应不同需求会报错。失败与中断任务也不自动重跑；继续原执行使用 `resume`，发起独立的新尝试才使用新 ID。

## 模拟视频工具与持久等待（B3）

默认关闭，启动前设置 `VAGENT_VIDEO_MODE=mock` 才注册视频工具。模式和来源可在 CLI 配置输出、Web 设置页和配置 API 查看；模式不写入 `config.yml`，不能在页面热切换，修改环境变量后需重启。

```powershell
$env:VAGENT_VIDEO_MODE = 'mock'
.\.venv\Scripts\python.exe -m vagent config show
```

| 工具 | 当前行为 |
|---|---|
| `video_capabilities` | 读取适配器提供的模型、能力版本和完整合法规格组合 |
| `video_generate` | 只在本地登记 Job，返回 jobId 与登记状态；每个 Run 最多一个新 Job，不在工具中提交上游 |
| `job_get` | 读取当前项目的本地 Job 快照；同一调用重放原结果，新调用读取最新状态 |
| `await_job` | 终态立即返回；未结束则保存持久等待，由 execution v2 挂起，结果就绪后回填原工具调用 |

新 mock Run 使用 execution v2。`AgentRunner.run()` 在持久中断保存后返回 `waiting_external`，释放图执行任务；已完成工具保留，未完成调用不伪造成功。`WaitCoordinator` 扫描持久结果并续接原调用，`ApplicationService` 已管理协调器启动、关闭及启动补偿。等待仍占用唯一未结束 Run 名额，新消息返回 `RUN_BUSY`。

用户停止会先持久关闭自动继续，并补齐可见的终止工具结果；Job 继续独立跟踪。正常退出保留等待意图，重启只自动继续有效等待。结果之后已经开始过模型尝试的中断要求显式 `resume`；缺少原配置、检查点或预算时保留结果，原因记录在 `waitResumeError`。

`ApplicationService` 统一管理 JobWorker 和等待协调器。`run/chat/resume` 等待原 Run 完成，图挂起期间 Worker 仍独立推进；`chat` 输入不会阻塞 Worker，也不留下阻止 Windows 退出的输入线程。缺少恢复配置时显示原因并保存结果。旧 B2 execution v1 仍按 `EXTERNAL_WAIT_UNAVAILABLE` 结束未完成等待，其 preparing 记录不会被后台唤醒。

以下 Job 命令不需要 DeepSeek Key：

```powershell
vagent jobs list
vagent jobs list --session coffee
vagent jobs get JOB_ID
vagent jobs retry-query JOB_ID
vagent jobs work
```

`list/get` 只读本地状态，`retry-query` 只为已暂停且有上游 ID 的任务恢复查询窗口，不重新 submit；三者不启动 Worker、模型或 MCP。`jobs work` 持续推进持久队列及有效等待；缺少原模型配置时继续推进 Job，保留等待结果。启动模式控制新 Run 的工具，已登记 mock Job 即使在 off 模式启动也继续跟踪。

CLI 退出后没有后台守护进程；重新运行 Web 或 `jobs work` 才继续推进队列。`run/chat/resume` 的 Ctrl+C 停止当前 Agent，`jobs work` 的 Ctrl+C 只关闭本地循环并保留等待意图。同一数据目录被 Web 占用时，CLI 提示 `STORE_LOCKED`。

Web 提供 `GET /api/jobs`（可选 `sessionId`）、`GET /api/jobs/:id` 和 `POST /api/jobs/:id/retry-query`。会话快照含当前项目 Job；`job.updated` 绑定创建 Job 的会话/Run，客户端按 jobId 与 revision 去重，溢出和重连通过快照补齐。任务卡片显示模拟标记、模型/参数、来源版本、生成与查询状态；Run 结束后仍更新，等待时可停止 Agent，查询暂停时可恢复查询。

只读任务隐藏并拒绝 `video_generate`，允许读取已有 Job 和保存等待记账。视频工具可见时，整次 Run 跳过 Redis 回答缓存，包括第一次模型调用之前；供应商前缀缓存 Token 统计保持。新 Run 保存模式与工具/能力配置，恢复时核对；旧 off Run 即使在 mock 启动配置下恢复，也使用原系统规则与工具集合。

Mock 始终标记 `simulated: true`、`mediaAvailable: false`，不产生 MP4 或播放/下载链接。调用真实 Agent 仍需要 DeepSeek Key 并消耗文本 Token；B0–B4 的工程验证全部使用离线模型，没有调用真实模型或视频 API。

## 从检查点继续

每次执行现在会将 LangGraph 检查点保存到数据目录的 `checkpoints.sqlite`。CLI 在开始执行时输出 Run ID，`inspect` 也会列出运行 ID、状态、`resumable`、原始预算和已用活动时间。

```powershell
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
# 将下面的 RUN_ID 替换为 inspect 或执行输出中的实际 ID。
.\.venv\Scripts\python.exe -m vagent resume RUN_ID
```

- 普通进程中断、用户取消，以及网络、鉴权、限流等可恢复失败，只能显式继续。有效的外部等待可由协调器自动继续；不会自动重发已经尝试过的后续模型请求。显式恢复真实模型请求仍可能产生 API 费用。
- 继续沿用同一 Run ID、request ID、模型步数、工具次数和活动时间预算；即使新 Runner 设置了更高限额，原 Run 也不会获得新预算。预算耗尽和非法协议等不可恢复错误会结束 Run。
- 图以 Run ID 作为 `thread_id`，每个节点的检查点提交完成后才进入下一个节点。工具操作 ID 由 Run ID、模型步骤和工具调用 ID 组成；重复进入工具节点时复用已经提交的结果，未完成的工具才实际执行。
- 已完成的 Run 返回原结果；图已完成但会话快照尚未提交时，从检查点补齐会话，不再次调用模型。
- 只允许恢复当前会话最近的 Run；如果已提交更新的需求，旧 Run 会被拒绝。模型、工具 Schema、系统规则、Skill 版本或上下文预算变化时，也会要求恢复原配置。可以修正 API Key，Key 不属于检查点或配置指纹。
- 旧版 Run 没有图检查点，不能追溯恢复；原有成功对话、项目和产物仍可继续使用。检查点缺失或损坏时会报告错误，保留文件，不退回从头执行。

活动时间在正常停止时按实际耗时累计，离线时间不计入。模型请求发出前预留最多 60 秒（不足时使用剩余预算）；如果请求中途进程被强制终止，重启时按这份预留额保守计时，因为无法确定实际执行了多久。该扣除只发生一次。未取得响应的请求可能已被供应商计费，但无法凭空补出其 Token usage。

外部等待不消耗活动时间，也不新增模型/工具额度；`externalWaitSeconds` 累计已结算等待，`externalWaitStartedAt` 保存当前区间起点，包含等待期间的进程离线时间。单次默认上限 10 分钟，超时交付 `JOB_WAIT_TIMEOUT`，Job 继续独立跟踪。停止后显式恢复未获结果的原等待可开启新代次；已保存结果保持稳定，原 Run 的 8/12/180 限额不重置。

CLI/Web 共用的 HTTP 客户端仅在 `ConnectError` / `ConnectTimeout`（尚未发送模型 HTTP 请求）时最多重连两次。使用原代理和证书校验，所有等待仍受原时间预算限制；写入/读取中断、429/5xx、流式失败、重定向或额外认证流程均不自动重试。配置页的单次验证显式关闭连接重试。评测中的 `connectionRetries` 与 `modelCalls` 分开统计。

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
  application.py CLI/Web 共用的生命周期与执行服务
  web.py       回环 HTTP API、CSRF、SSE 与打包页面
  mcp_bridge.py MCP stdio 发现、显式只读白名单与异步工具桥
  mcp_server.py 内置镜头时长与帧数计算服务
  config.py    配置来源、原子保存与 Key 校验
  models.py    DeepSeek 单步/流式适配、完整参数校验与离线模拟模型
  http.py      请求发送前的有限连接重试，共享客户端生命周期
  runner.py    自定义 LangGraph 图、预算、取消与事件
  checkpoints.py SQLite 异步检查点与连接生命周期
  journal.py   跨恢复保留的执行预算、使用量与会话提交
  usage.py     Token 与缓存用量归一化、覆盖率与 Run 汇总
  cache.py     Redis 精确回答缓存、TTL、超时与故障回退
  context.py   上下文组装与完整轮次裁剪
  quality.py   记忆冲突、用户字数要求与正文计数校验
  tools.py     工具注册、Pydantic 校验、项目与产物操作
  storage.py   单写锁、事务、原子替换、操作日志、schema v1→v2 迁移
  skills.py    技能发现、元信息与按需正文读取
  contracts.py B0：不依赖 Store/SDK 的严格 JSON 类型
  waiting.py   通用执行上下文、延迟结果和版本化等待指针
  wait_runtime.py B3：持久等待、结果领取/交付、停止、计时与恢复协调
  video/contracts.py B0：视频能力、请求、Job、供应商协议与状态约束
  video/jobs.py B1：原子登记、请求冻结、去重、版本检查和启动恢复
  video/tools.py B2/B3：四工具、能力规则、等待解析器与历史执行视图
  video/worker.py B1：串行提交/查询、超时、持久重试和停止
  video/providers/mock.py B1：独立持久上游账本与服务端模拟轨迹
skills/        内置 SKILL.md，随 wheel 分发
tests/         pytest 行为与协议测试
scripts/probe_m1b_wait.py B0：独立临时目录中的持久等待实验
```

LangGraph 提供图执行底座，项目自己定义状态、路由、上下文策略、工具边界、版本控制和运行记录。`ChatDeepSeek.bind_tools(...).astream(...)` 只执行单个模型步；保留非流式 `generate` 接口以兼容测试和其他调用方。流式工具参数必须通过严格 JSON 解码和完成标记检查后才执行，模型不会直接执行工具。

- **工具白名单**：默认 `project_read`、`project_update`、`plan_update`、`artifact_save`、`artifact_read`、`skill_read`；mock 模式增加上述四个视频工具。不开放 shell 或任意文件路径。
- **Pydantic Schema**：工具声明与后端输入校验共用定义，拒绝未知字段和错误参数类型；工具错误会返回模型。
- **项目记忆**：保存目标、受众、风格、约束和计划；产物以稳定 ID 保存，每次修改增加版本并保留原文。
- **写入保护**：单写进程锁、串行事务、同目录原子替换；业务修改和操作结果一起提交。相同操作 ID 复用结果，参数冲突则拒绝。
- **运行限制**：默认最多 8 个模型步、12 次工具调用、180 秒，显式恢复沿用累计预算；超时取消异步模型请求，停止后不启动新工具。已完成的本地写入保留。
- **预算提示**：上下文 v2 在稳定前缀和项目事实之后提供实际剩余模型/工具次数，模型剩余次数包含本次调用；重启后继续扣除已用额度，提示不写入对话历史。v1 布局保持原状。
- **日志边界**：不直接输出供应商异常或参数校验中的原始输入值。配置展示只返回 Key 是否存在。

文本工具去重针对同一请求/操作 ID。B1 JobService 另限制每个 Run 最多一个视频 Job：不同调用 ID 的同一规范化视频请求返回原 jobId，不同请求返回冲突；这不是语义去重。JSON 存储面向单用户小规模使用，同步本地文件写入不是可抢占的异步任务。

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

默认数据目录为 `~/.vagent/`，可用 `VAGENT_HOME` 覆盖。`state.json` 保存会话、项目、产物、Run、操作结果以及 B1 的 jobs/waits；`checkpoints.sqlite` 保存图执行位置和消息。B1 的 `mock-video.json` 独立保存模拟上游受理记录、请求、轨迹和调用计数。应在程序退出后备份完整数据目录。配置 Key 不写入状态。`inspect` 会显示创作正文。

当前存储为 schema v2。打开 v1 数据时，在实例锁下验证旧状态与消息，先将原始字节保存为同目录的 `state-v1-<UUID>.json`，再原子迁移，保留原项目、产物版本、Operation 指纹、用量和检查点。缺少视频字段的旧 Run 按 off 解释，execution v1 与上下文 v1/v2 继续按原配置恢复。迁移写入失败、损坏数据和未知版本均保留原状态文件；单独的 v1 快照不能代替完整目录备份。

- 成功会话可跨重启继续；执行中的普通 Run 重启后标记为 `interrupted`，已提交产物保留，不自动重放。B3 保留有效的 `waiting_external` 和原 Run 名额，启动时核对等待、检查点、配置与原预算后继续；后续模型尝试已经开始但未完成时要求显式恢复。
- B1 已登记但尚未提交的 Job 可由 Worker 继续推进；丢失提交结果的任务进入 `unknown`，不自动重提。已确认上游 ID 只用于继续查询，失败窗口和累计次数跨重启保留。
- 应用快照和 SQLite 检查点分别提交，依靠同步图检查点、操作幂等和恢复时补交会话处理提交间隙；并非跨 JSON/SQLite 的单一数据库事务。
- 异常断电可能留下 `instance.lock`；确认没有进程使用该数据目录后才手动移除，程序不会自动抢锁。
- CLI/Web 已支持流式草稿，完成后以持久回复替换；草稿不写入检查点，中断后不作为下一次模型输入。自动摘要和跨项目偏好记忆尚未实现。

B1 的 JobService、MockVideoAdapter 和 JobWorker 可通过 Python API 独立使用和测试，不需要模型或 Key；B4 已管理应用启动与退出。B2 的视频工具/模式和 B3 的等待/恢复通过同一服务接入 CLI/Web。Mock 只保存带 `simulated: true`、`mediaAvailable: false` 的描述，不生成媒体文件。当前真实 Agent 只需要 DeepSeek Key，视频服务 Key 在真实视频阶段单独配置。

## 验证与打包

任务四新增验证：真实 Redis 重连命中、TTL 失效与服务中断回退；独立 wheel 环境中的仓库外 CLI 与依赖检查通过。详见 [Redis 缓存验收](./docs/REDIS_CACHE_ACCEPTANCE.md)。

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
node --check web/job-state.js
node --test tests/test_job_state.mjs
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/python
```

本地 Python 3.12 测试结果：**452 passed，1 skipped**，另有 **7 项前端状态测试通过**。跳过的是当前 Windows 账户无符号链接创建权限的测试。B4 新增 25 项 Python 测试，覆盖自动 Worker、独立 Job 事件、API/SSE、CLI 持续等待/恢复和 Windows SIGINT 退出；B0–B3 的迁移、原图指纹、16 项生产强退、预算与工具去重继续通过。原工具闭环、质量、MCP、流式、缓存与评测回归保持。Node 仅用于开发时检查前端语法与状态逻辑，运行 Agent 无需安装。

B3 的源码包和 wheel 已构建到 `dist/m1b-b3/`。独立虚拟环境在仓库外通过 10 项安装检查，包括持久等待跨服务重启后续接原 Run，以及安装版 SQLite 节点 pending writes 强退恢复；Worker 由脚本显式推进。完整范围与证据见 [B3 验收](./docs/M1B_B3_ACCEPTANCE.md)。

已验证离线 CLI，以及 wheel 安装到独立虚拟环境后在仓库目录之外运行 `skills list`、`demo`、`inspect`。本次新增验证：新建独立虚拟环境安装 wheel，保存产物后取消，再从仓库外通过 CLI `resume` 完成原 Run，仍仅有一个产物；`pip check` 通过。源码包与 wheel 仅本地构建，未发布 PyPI。GitHub Actions 配置 Ubuntu/Windows、Python 3.11/3.12 检查，远端结果见 [Actions](https://github.com/shifang37/vAgent/actions)。

用量观测版本的 wheel 也通过独立虚拟环境验证：从仓库外恢复离线 Run 后执行 `usage --run`，显示累计 4 次适配器调用，并将未报告的 Token 和缓存明细保留为未知。

上下文 v2 的 wheel 已在同一独立环境重新安装验证：仓库外执行 `skills list`、取消后 `resume`、`inspect`、`usage --run` 均通过，格式版本保持 2、最终仍仅有一个产物，`pip check` 通过。v1 检查点兼容由自动化测试覆盖，详细记录见 [上下文优化验收](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)。

自动化测试验证工程行为。2026-10-09 的完整真实套件 9/9 通过，保留了此前连接故障、步数耗尽和超长拒绝的全部证据；单套通过不代表生产成功率或创作质量保证。M1-A 已验收，M1-B 已完成 B0–B4，下一步为 B5。B0–B4 没有新增真实模型或视频 API 调用；证据见 [M1-A 验收](./docs/M1A_ACCEPTANCE.md) 和 [B4 验收](./docs/M1B_B4_ACCEPTANCE.md)。
