# vagent 使用手册

本手册收录 CLI、Web、headless、恢复、用量、缓存、Skills 与数据兼容的操作细节与边界说明，内容来自历次里程碑交付时写入 README 的参考段落。安装与总览见仓库根目录的 [README](../README.md)；逐阶段的范围、复现命令与证据见同目录下的 `M*_ACCEPTANCE.md`。

命令示例以 Windows PowerShell 和仓库内虚拟环境 `.\.venv\Scripts\python.exe` 为准；macOS / Linux 换成 `.venv/bin/python`，或激活虚拟环境后直接使用 `vagent`。

## 目录

- [本地 Web 与真实编排](#本地-web-与真实编排)
- [记忆一致性与字数上限](#记忆一致性与字数上限)
- [连接真实 DeepSeek](#连接真实-deepseek)
- [视频工具与持久等待](#模拟视频工具与持久等待m1-b)
- [本地成片与 headless](#本地成片与-headlessm3)
- [本地字幕](#本地字幕m6-e1)
- [本地季与批量](#本地季与批量m7-e1e2)
- [本地时间线工作台](#本地时间线工作台m7-e3)
- [万相真实协议与本地媒体](#万相真实协议与本地媒体m1-c-c1c2)
- [从检查点继续](#从检查点继续)
- [查看模型与缓存用量](#查看模型与缓存用量)
- [Redis 回答缓存](#redis-回答缓存任务四)
- [实现与代码入口](#实现与代码入口)
- [上下文与 Skills](#上下文与-skills)
- [数据兼容与限制](#数据兼容与限制)

## 本地 Web 与真实编排

保留 DeepSeek Harness 风格，已移除浏览器中的模板回复。会话、项目记忆和产物由 Python 服务保存；侧栏「编排观测」查看上下文字节预算、裁剪数、记忆 revision、Skills、MCP 与实际调用用量。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
# 可在启动后的设置页填写 Key，也可使用本地 .env。
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m vagent web --mcp-local --no-open
```

打开 [本地工作台](http://127.0.0.1:3210)。缺少 Key 时自动打开配置向导；保存不调用模型，「保存并验证」最多发送一次简短请求，可能产生少量费用。发送创作需求会调用真实 DeepSeek。`--mcp-local` 启用镜头时长和帧数计算服务；外部 stdio 配置见 [MCP 说明](./MCP.md)。

服务只监听 `127.0.0.1`，校验 Host/Origin 和写请求 CSRF Token；静态资源使用白名单，API 不返回 Key。同一数据目录只允许一个 CLI/Web 写进程。文本流实时显示为草稿，完整回复通过检查后才持久保存；断线重连补齐当前文本，停止或失败时丢弃草稿。完整边界见 [Web 说明](../web/README.md)。

在工作台运行时，可显式执行固定任务验收（会产生模型费用）：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --live
# 仅重新检查已有产物，不调用模型：
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --review-existing
```

此前首组真实调用共 13 个模型步骤、17 次工具调用；输入 71,645、输出 3,498 Token。原始报告保存在被忽略的 `output/agent-live-acceptance.json`，可复现步骤和发现见 [验收记录](./AGENT_ORCHESTRATION_ACCEPTANCE.md)。

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

校验错误需由模型在原执行预算内纠正；若没有纠正便直接结束，Run 返回 `QUALITY_UNRESOLVED`，不采用模型的成功声明。固定任务评测重新读取正文计数，有质量问题时返回失败退出码。当前检查针对明确数字上限和字段原值的文本重复/残留，不承诺任意自然语言约束或同义改写的完整语义判断。详见 [质量校验验收](./QUALITY_ACCEPTANCE.md)。

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

## 模拟视频工具与持久等待（M1-B）

默认关闭，启动前设置 `VAGENT_VIDEO_MODE=mock` 可注册模拟视频工具。模式和来源可在 CLI 配置输出、Web 设置页和配置 API 查看。C1 起也可将视频配置保存到 `config.yml`；K2 起视频配置保存后免重启、对新登记立即生效（在途与历史任务按各自冻结配置恢复），设置页默认只留两个 Key 与模式开关，高级字段折叠；off/mock/live 各有独立工具视图。

```powershell
$env:VAGENT_VIDEO_MODE = 'mock'
.\.venv\Scripts\python.exe -m vagent config show
```

| 工具 | 当前行为 |
|---|---|
| `video_capabilities` | 读取适配器提供的模型、能力版本和完整合法规格组合 |
| `video_generate` | 只在本地登记 Job，返回 jobId 与登记状态；每个 Run 最多一个新 Job，不在工具中提交上游 |
| `job_get` | 读取当前项目的本地 Job 快照；同一调用重放原结果，新调用读取最新状态 |
| `await_job` | 终态立即返回；未结束则保存持久等待，由执行图挂起（execution v2/v3），结果就绪后回填原工具调用 |
| `storyboard_read` | M2 D1 起（mock）：读取当前项目的结构化分镜、当前版本有序镜头与各镜头版本摘要；只读，跨项目拒绝 |
| `storyboard_save` | M2 D1 起（mock）：新建或修改结构化分镜，校验来源/素材引用与上限，产生新版本并保留旧版本，返回服务端稳定 Shot ID |
| `shot_generate` | M2 D2 起（mock/live）：为已保存分镜的指定镜头批量登记任务，必须显式确认，回显逐镜头与合计估算、已用与剩余额度；任一镜头校验失败整批不登记，同一分镜版本同一镜头的相同请求复用原任务 |
| `asset_read` | M2 D3 起：只读列出素材库参考图元数据（assetId、来源项目、格式、尺寸、导入时间）；不含本地路径、私有 URL 或指纹，也不能导入或删除 |
| `film_assemble` | M3 E1 起（tools/rules 5/5）：按当前分镜版本把每个镜头的最新已交付 MP4 本地流复制拼接为单一成片；必须提供 `expectedRevision`，任一镜头未交付或编码不兼容整批拒绝；同输入复用原版本，锁外执行、可停止，不登记 Job、不产生费用 |
| `film_read` | M3 E1 起：只读返回当前成片、历史版本、逐输入镜头与有理数时间线偏移，以及受控媒体 URL；不拼接、不创建任务 |
| `subtitle_save` | M6 E1 起（tools/rules 10/10）：按冻结台词与时间线确定性对齐或保存人工 cue；只增不改、无 Job、无费用 |
| `season_save` | M7 E1 起（tools/rules 15/15）：保存季标题与 1–4 个有序分集 ID；每次有效修改追加 `SeasonVersion`，旧版本逐字节保留；相同内容重放同一版本；不登记 Job、不调用模型、不产生费用 |
| `season_read` | M7 E1 起（E2 扩展）：只读季、成员集摘要（版本、场数、镜头数、时长）、全部季列表与最近/指定批次的逐集登记状态、Job ID、交付计数与错误码；不登记、不提交 |
| `season_generate` | M7 E2 起（tools/rules 16/16）：为季的 1–3 集逐集原子登记，必须显式 `confirm`；单集失败只标记该集；预检 3 集/60 Job/180.00 CNY；每季一个进行中批次；显式 `resumeBatchId` 恢复；只登记，不提交、不等待 |
| `timeline_save` | M7 E3 起（tools/rules 18/18）：保存每集一条只增不改的 `TimelineVersion`——有序 `clips`（镜头内 `inMs/outMs` 裁剪）、`transitions`、整集字幕轨、`audio[]`、标题卡与 `expectedRevision`；有效时长 1–90 秒，相同内容重放、修改追加版本；只引用已登记 `ShotVersion`，不登记 Job、不调用模型、不产生费用 |
| `timeline_read` | M7 E3 起：只读当前/指定版本、逐条裁剪与源时长、镜头交付状态、转场、字幕/音轨/标题卡、指纹、有效时长与 `stale`；不登记、不提交 |
| `audio_generate` | M6 E2 起（11/11）：按集生成 voice 资产，显式确认、字符/次数/金额限额、音色绑定与复用；不登记视频 Job |
| `film_render` | M6 E3 起（13/13）；M7 E3 扩展 `scope=timeline`（18/18）：本地渲染整片，支持 `cut/fade/crossfade`、标题卡、720P/1080P、soft/burn/both、整集与时间线；`scope=timeline` 的剪辑/转场/字幕/音轨全部取自冻结版本，`ffmpeg-render-timeline-v1`、`timeline-subtitle-remap-v1`；显式确认、锁外执行、无 Job、无供应商费用 |
| `render_read` | M6 E3 起：只读渲染任务逐场状态、失败阶段、测量与预估耗时；不重新渲染 |

新 mock Run 自 M2 D1 起使用 execution v3；v1/v2 运行仍按原路径恢复。`AgentRunner.run()` 在持久中断保存后返回 `waiting_external`，释放图执行任务；已完成工具保留，未完成调用不伪造成功。`WaitCoordinator` 扫描持久结果并续接原调用，`ApplicationService` 已管理协调器启动、关闭及启动补偿。等待仍占用唯一未结束 Run 名额，新消息返回 `RUN_BUSY`。

用户停止会先持久关闭自动继续，并补齐可见的终止工具结果；Job 继续独立跟踪。正常退出保留等待意图，重启只自动继续有效等待。结果之后已经开始过模型尝试的中断要求显式 `resume`；缺少原配置、检查点或预算时保留结果，原因记录在 `waitResumeError`。

预算耗尽（模型步数或工具额度）且本次请求已成功写入产物时，Run 会额外执行一次**不带工具**的收尾调用，用来说明已完成/未完成部分并给出 `artifactId`；总数最多 `maxSteps+1`，只触发一次，恢复后不重复。该行为默认开启，可用 `VAGENT_CLOSING_TURN=off` 关闭；只读步骤不触发收尾轮，仍按原错误码结束。每次收尾调用照常计入用量与成本。

`ApplicationService` 统一管理 JobWorker、媒体 Worker 和等待协调器。`run/chat/resume` 等待原 Run 完成，图挂起期间 Worker 仍独立推进；`chat` 输入不会阻塞 Worker，也不留下阻止 Windows 退出的输入线程。缺少恢复配置时显示原因并保存结果。旧 B2 execution v1 仍按 `EXTERNAL_WAIT_UNAVAILABLE` 结束未完成等待，其 preparing 记录不会被后台唤醒。

以下 Job 命令不需要 DeepSeek Key：

```powershell
vagent jobs list
vagent jobs list --session coffee
vagent jobs get JOB_ID
vagent jobs retry-query JOB_ID
vagent jobs retry-download JOB_ID
vagent jobs work
```

分镜、素材与用量命令同样不需要 DeepSeek Key，也不启动模型、Worker 或 MCP：

```powershell
vagent storyboard show [-s PROJECT] [--storyboard STORYBOARD_ID]
vagent shots redo SHOT_ID [-s PROJECT] [--request-id REQUEST_ID]
vagent assets list [-s PROJECT]
vagent assets import PATH [PATH ...] [-s PROJECT]
vagent usage --project [PROJECT]
vagent usage bill record --job JOB_ID --amount 0.56 --currency USD --source "Kling 控制台 2026-10-25 账单" --confirm
vagent usage bill show [--project PROJECT] [--currency CNY|USD]
vagent route preview --mode t2v --duration 5
vagent route preview --storyboard STORYBOARD_ID [--version N] [--shot SHOT_ID ...] [--scope shot|episode|batch]
```

`storyboard show`、`assets list`、`usage --project` 只读本地状态。`shots redo SHOT_ID` 由镜头 ID 解析所属分镜与项目，为该镜头新增一个 `ShotVersion` 并登记一个新 Job，其他镜头的版本、Job 与媒体不动；它只登记、不提交上游，由 Web 或 `jobs work` 继续推进。省去 `--request-id` 时每次调用都是一次新的重做；带上该参数即可让重试幂等（同一请求 ID 重放原结果，不重复登记）。`assets import` 校验魔数/尺寸/宽高比/大小后按 `(项目, SHA-256)` 去重，命中即复用原素材；同一张图片重新导入还能恢复被删除或损坏的素材文件。`usage --project` 汇总整个数据目录（按项目聚合则给出项目 ID），只读取 Jobs、估算、账单状态、分镜、素材与模型 Token 覆盖率，不做外部调用。`route preview` 只读比较冻结能力表：每个供应商一行，给出金额与币种、可用性与原因、Key 是否配置、价格来源与核对日期；CNY 与 USD 只并列展示、**不换汇**，集级/批量只接受 CNY，`--require-available` 在没有可用候选时返回 `ROUTE_UNAVAILABLE`；它不登记 Job、不调用供应商、不调用模型、不读取 Key 内容。

`usage bill` 只写 `VAGENT_HOME/billing.json` 侧车账本（不进 `state.json`，因此 schema 仍是 9）：只接受已终态 Job 的手工 reported actual，币种必须等于该 Job 冻结报价的币种，`--confirm` 缺失时返回 `CONFIRMATION_REQUIRED` 且不写盘；相同内容重放返回 `replayed=true`，金额/币种/日期/来源/备注任一不同返回 `BILL_CONFLICT`，更正需要 `--replace --expected-revision N`；Job 不存在或在途、字段非法、条数/大小超限为 `BILL_RECORD_INVALID`；账本损坏时报 `INVALID_STORE` 且保留原文件。`usage --project` 与 `GET /api/usage` 追加 `billing` 块（逐币种估算/已录入/差额，未录入为 `null` 而不是 0，跨币种不合计），`GET /api/usage?runtime=1` 追加内存态 `runtime` 块（提交/查询延迟 p50/p95、退避与限流计数，固定 `durable:false`）。

`list/get` 读取本地状态并核对媒体可用性，发现文件缺失或损坏时保存新的可用性 revision；不发起上游 HTTP。`retry-query` 只恢复原任务查询窗口，`retry-download` 只登记原媒体的恢复窗口；这四个命令均不启动 Worker、模型或 MCP。`jobs work` 或 Web 持续推进查询、下载与有效等待；缺少原模型配置时继续推进 Job，保留等待结果。启动模式控制新 Run 的工具，已登记 mock Job 即使在 off 模式启动也继续跟踪。

CLI 退出后没有后台守护进程；重新运行 Web 或 `jobs work` 才继续推进队列。`run/chat/resume` 的 Ctrl+C 停止当前 Agent，`jobs work` 的 Ctrl+C 只关闭本地循环并保留等待意图。同一数据目录被 Web 占用时，CLI 提示 `STORE_LOCKED`。

Web 提供 `GET /api/jobs`（可选 `sessionId`）、`GET /api/jobs/:id`、`POST /api/jobs/:id/retry-query` 和 `POST /api/jobs/:id/retry-download`。会话快照含当前项目 Job；`job.updated` 绑定创建 Job 的会话/Run，客户端按 jobId 与 revision 去重，溢出和重连通过快照补齐。任务卡片显示模拟标记、模型/参数、来源版本、生成/查询/下载状态；Run 结束后仍更新，等待时可停止 Agent，查询暂停或下载失败时可恢复原任务。

只读任务隐藏并拒绝 `video_generate`，允许读取已有 Job 和保存等待记账。视频工具可见时，整次 Run 跳过 Redis 回答缓存，包括第一次模型调用之前；供应商前缀缓存 Token 统计保持。新 Run 保存模式与工具/能力配置，恢复时核对；旧 off Run 即使在 mock 启动配置下恢复，也使用原系统规则与工具集合。

## 本地成片与 headless（M3）

成片与 headless 都是本地动作，不调用供应商、不登记生成额度。`vagent film assemble` 用包内 FFmpeg 把当前分镜版本每个镜头的最新已交付 MP4 流复制拼接为单一成片；`vagent film show` 只读当前成片、历史版本和受控媒体地址。`vagent headless` 面向脚本化/无浏览器场景，依次完成「计划 → 显式授权 → 集合等待 → 本地拼接交付」。

```powershell
# 1) 只规划：调用编排模型保存结构化分镜（可能产生文本费用），不登记、不提交任何视频任务
vagent headless "做一支 15 秒的雨夜咖啡店短片" -s coffee --json

# 输出含 runId、planFingerprint、storyboardId/version/revision、estimates[]（逐桶金额与 Kling VIDEO_UNIT）与 limits。
# 2) 核对镜头、供应商、能力/价格版本和逐桶估算后，显式授权并执行：
vagent headless --plan RUN_ID --plan-fingerprint SHA --confirm `
  --max-cost kling:USD=1.12 --max-units kling=8 --max-jobs 2 --json

# 3) 等待超时（退出码 5）或进程中断后继续；沿用原授权，不增额度、不重新提交：
vagent headless --resume RUN_ID --json
```

- **两阶段授权**：`--plan` 只接受已经产出的计划；`--plan-fingerprint` 必须匹配，`--confirm` 必须显式给出，`--max-cost` 必须覆盖计划中的每个金额桶（Kling 还要 `--max-units`），可用 `--max-jobs` 收紧共享的 4 个新任务上限。缺项、未知桶、低于计划估算、超过服务端上限或计划后分镜 revision 变化都会在任何登记前拒绝。
- **原子核对**：授权指纹和预算覆盖先持久化；登记事务内再次核对计划指纹、当前分镜 revision、逐镜头请求指纹、能力/价格版本与所有预算桶，一项不符则整批不登记。授权后、登记前崩溃时，`--resume` 使用原授权补齐登记。
- **等待与交付**：登记后复用 `JobWorker`/`MediaWorker` 与既有终态规则等待全部镜头；任意镜头失败返回退出码 4，等待到期返回 5 并保留 jobIds；全部交付后复用本地成片服务拼接，输出经过 resolver 校验的绝对路径、SHA-256 与大小。`--resume` 不重新提交，分镜或镜头版本在登记后变化会返回 6。
- **结构化输出**：`--json` 时 stdout 只输出一个对象，字段固定为 `formatVersion,status,projectId,sessionId,runId,planFingerprint,storyboardId,storyboardVersion,storyboardRevision,jobIds,filmId,filmVersion,output{mediaId,path,sha256,sizeBytes},estimates,limits,error`；过程日志在 stderr。成功用 `status` 区分 `planned`/`delivered`。
- **退出码**：`0` 计划成功或已交付，`2` 参数/领域校验，`3` 授权/预算拒绝，`4` 上游/下载/拼接失败，`5` 等待超时，`6` 恢复条件变化，`130` 用户中断。无 TTY 不交互、不默认同意。
- headless 规划 Run 使用收窄的分镜工具集（不能登记 Job 或拼接），其普通 `vagent resume` 会因工具集不同而被拒绝；继续 headless 流程请使用 `vagent headless --resume`。真实编排模型仍可能产生文本费用；计划阶段与既有 CLI 回归均保持视频 submit=0。

## 本地字幕（M6 E1）

字幕保存与导出同样是本地动作：不调用供应商、不登记视频 Job、不产生费用。`subtitle save` 省略 `--cues` 时按该场已冻结的 `SceneVersion.dialogue[]` 与冻结时间线做 `subtitle-align-zh-v1` 确定性对齐，显式 `--cues` 时按 JSON 数组做人工修正（整数毫秒，只校验不重排）；`subtitle export` 把某一确定字幕版本软封装（`mov_text` + 旁挂 SRT）或烧录到当前成片，并追加一个只增不改的成片版本（`render` 记录 profile、字体指纹、测量与渲染指纹）。

```powershell
vagent subtitle show  -s coffee --storyboard STORYBOARD_ID
vagent subtitle save  -s coffee --storyboard STORYBOARD_ID --request-id sub-1
vagent subtitle save  -s coffee --storyboard STORYBOARD_ID --track TRACK_ID `
  --expected-revision 0 --cues cues.json --request-id sub-2
vagent subtitle export -s coffee --track TRACK_ID --mode soft --request-id export-1
vagent subtitle export -s coffee --track TRACK_ID --mode burn --request-id export-2
```

- `show/save` 返回字幕轨、版本、逐条 cue、时间线来源（`film`/`storyboard`）与受控 SRT/ASS 地址；`subtitle_save` 工具与 CLI 共用同一领域写入，相同 cues 重放同一版本，修正追加新版本。
- `export` 只接受与源成片版本逐段相等的字幕时间线（否则 `RENDER_TIMELINE_MISMATCH`）；E1 只支持 720P 与分镜作用域，1080P/整集导出返回 `RENDER_PROFILE_UNSUPPORTED`/`RENDER_SOURCE_MISSING`。
- 烧录只使用包内 OFL 字体（`VAgentSubtitleSC-400.ttf`，SHA-256 固定）；字体缺失或被替换返回 `FONT_MISSING`，不会退回系统字体。重叠/倒序/越界/超限的 cue 分别返回 `SUBTITLE_CUE_CONFLICT`/`SUBTITLE_OVERFLOW`，整次拒绝且旧版本保留。

## 本地季与批量（M7 E1/E2）

季只是成员关系：它保存标题与 1–4 个有序分集 ID，不复制角色/风格，也不拥有场记、分镜、预算、Job 或成片。`season save` 每次有效修改追加一个 `SeasonVersion`，旧版本逐字节保留；相同标题与相同顺序重放同一版本。一个项目最多 8 个季，一集至多属于一个季的当前版本；换季要先在原季的新版本中移除，再加入目标季。季操作不登记任务、不调用模型或供应商，视频模式为 `off` 也可用。批量登记只接受该季当前版本的 1–3 集，逐集独立事务、单集失败隔离；每季同时至多一个进行中批次。

```powershell
vagent season show -s coffee
vagent season show -s coffee --season SEASON_ID
vagent season show -s coffee --season SEASON_ID --batch BATCH_ID
vagent season save -s coffee --title "第一季" `
  --episode EPISODE_ID_1 --episode EPISODE_ID_2 --request-id season-1
vagent season save -s coffee --season SEASON_ID --title "第一季" `
  --episode EPISODE_ID_2 --episode EPISODE_ID_1 `
  --expected-revision 0 --request-id season-2
vagent season generate -s coffee --season SEASON_ID --confirm --request-id batch-1
vagent season generate -s coffee --season SEASON_ID --episode EPISODE_ID_1 --episode EPISODE_ID_2 --confirm
vagent season generate -s coffee --season SEASON_ID --resume BATCH_ID --confirm
```

- `show` 返回季、当前版本、`revision`、成员集摘要（版本、场数、镜头数、目标时长）、最近批次与全部季/批次列表；省略 `--season` 时读取本项目最新创建的季，`--batch` 指定要读取的批次。
- `save` 修改时必须带 `seasonId` 与 `season show` 返回的 `revision`；缺失或过期返回 `SEASON_VERSION_CONFLICT`。标题为空、超过 4 集或超过 8 个季返回 `SEASON_LIMIT`；重复成员、跨项目分集或已属于另一季返回 `SEASON_EPISODE_CONFLICT`；未知季返回 `SEASON_NOT_FOUND`。
- `generate` 必须显式 `--confirm`；`--episode` 省略时用全部成员（季有 4 集时会因超过批次上限返回 `BATCH_LIMIT`）。`--resume BATCH_ID` 与 `--episode` 互斥，只重登记旧批次中非 `succeeded` 的集并创建 `resumeOf` 新批次；每季已有进行中批次返回 `BATCH_CONFLICT`。批次只登记，不提交、不等待。
- Web 季面板（侧栏「季」）提供季选择、新建、标题编辑、有序成员勾选（点击顺序即季内顺序，最多 4 集）与 `批量生成` 区块：勾选 1–3 集后先预览逐集镜头数/新任务/估算，再确认登记；批次卡片显示逐集状态、Job ID、交付计数与错误码，`partial/failed` 时可显式「恢复失败集」。预览走 `GET /api/seasons/{id}/estimate`，登记走 `POST /api/sessions/{id}/seasons/generate`，季详情为 `/api/seasons/{id}?projectId=`。
- E2 已实现批次，`season_read.batch` 为最近批次（或 `batchId` 指定），`batches` 为摘要列表；时间线工作台属 E3，见下一节。

## 本地时间线工作台（M7 E3）

时间线把已保存镜头重新排序、裁剪，并绑定转场、整集字幕轨、音轨与标题卡。每集恰好一条时间线，版本只增不改；`timeline save` 修改时必须带读到的 `revision`，相同内容重放同一版本。时间线只引用已登记的 `ShotVersion`（未交付也可保存排期），不复制媒体、预算或分镜描述；分集版本变化后 `stale=true`，渲染拒绝并返回 `TIMELINE_SOURCE_MISSING`。保存与读取不登记 Job、不调用模型或供应商，视频模式 `off` 也可用。

```powershell
vagent timeline sources -s coffee --episode EPISODE_ID
vagent timeline save -s coffee --episode EPISODE_ID --clips clips.json `
  --transitions transitions.json --subtitle-track TRACK_ID --subtitle-version 1 `
  --audio audio.json --title-cards titles.json --request-id timeline-1
vagent timeline save -s coffee --episode EPISODE_ID --timeline TIMELINE_ID `
  --clips clips2.json --subtitle-track TRACK_ID --subtitle-version 1 `
  --expected-revision 0 --request-id timeline-2
vagent timeline show -s coffee --episode EPISODE_ID
vagent timeline show -s coffee --timeline TIMELINE_ID --version 2
vagent audio import rain.wav -s coffee --kind bgm --title "雨声" `
  --source-url "https://example.com/rain" --license "CC0" --request-id audio-1
vagent audio list -s coffee
vagent film render -s coffee --timeline TIMELINE_ID --confirm --request-id render-1
```

- `clips` 为有序 JSON 数组：`shotId`、`shotVersionId`、`storyboardId`、可选 `sceneId`、`inMs`、`outMs`（镜头内整数毫秒；单条 ≥1 秒、不超过镜头时长）。`transitions` 为 `afterShotId`/`type`/`durationMs`；转场 0.1–1.0 秒且不超过相邻任一裁剪后镜头时长的 1/3，最后一个镜头之后不能有转场。有效时长 = 裁剪时长和 − 转场和，必须 1–90 秒。
- 字幕只能引用本集 `scope=episode` 的字幕轨与版本；有 voice 音轨时必须同时提供字幕轨。音轨的 `voice` 起点固定 0（按重映射 cue 定位），BGM/SFX 的 `startMs` 必须在有效时间线内，`gainDb` 范围 −60..+12。标题卡最多 4 条，不重叠、不越界。
- BGM/SFX 只能经 `vagent audio import` 或 Web 上传导入（WAV/MP4、≤25 MB、≤60 秒，必须填 `sourceUrl`/`license`，按 SHA-256 去重）；模型只看到 `audioId`，没有音频导入工具。
- `film render --timeline` 本地导出时间线版本（可选 `--timeline-version`），输出 `ffmpeg-render-timeline-v1` 与 `render.sources.timeline`；字幕按 `timeline-subtitle-remap-v1` 重映射，旁挂 SRT 由同一逻辑重算；时间线外的 cue 丢弃，裁剪窗口外夹取。全程不登记 Job、不调用供应商；同指纹返回原成片，失败不提交任何版本。
- Web 时间线工作台（侧栏「时间线」或分集面板「打开时间线工作台」）提供排序/裁剪/转场/字幕/音轨/标题卡编辑、客户端顺序预览（按裁剪窗口播放源镜头，不合成）、保存与导出，以及 BGM/SFX 上传；`timeline-state.js` 的纯函数负责预览与保存前的本地校验。

## 万相真实协议与本地媒体（M1-C C1/C2）

C1/C2 已实现任务登记、一次提交、原任务查询和本地媒体交付，普通回归使用受控 HTTP 响应与本地生成的测试视频。运行 live 会发出收费生成请求；云端成功先进入 `downloading`，文件校验与媒体索引提交完成后才返回本地结果。真实账号与付费样本记录见 [M1-C 验收](./M1C_ACCEPTANCE.md)。

视频 Key 独立于 DeepSeek Key。在本机 `.env` 或 `VAGENT_HOME/config.yml` 中配置，环境变量优先。以下为视频字段及默认值：

| 环境变量 | 本地配置 / PATCH 字段 | 取值 |
|---|---|---|
| `VAGENT_VIDEO_MODE` | `videoMode` | `off`（默认）、`mock`、`live` |
| `VAGENT_DASHSCOPE_KEY` | `videoApiKey` | 北京地域的视频 Key；只在本机填写 |
| `VAGENT_DASHSCOPE_WORKSPACE_ID` | `videoWorkspaceId` | 与 Key 匹配的业务空间 ID，小写 DNS 标签 |
| `VAGENT_VIDEO_PROVIDER` | `videoProvider` | `wan` |
| `VAGENT_VIDEO_MODEL` | `videoModel` | `wan2.7-t2v-2026-06-12` |
| `VAGENT_VIDEO_REGION` | `videoRegion` | `cn-beijing` |
| `VAGENT_VIDEO_MAX_JOB_COST` | `videoMaxJobCost` | 十进制人民币字符串，默认 `"3.00"` |

固定规格为 5 秒、720p、16:9，公开提示词最多 4991 字符。后端加上单镜头前缀，显式发送 `prompt_extend=false`、`watermark=true`、`seed=0`。每个 Run 最多一个新 Job；重复调用先复用原任务，再检查新配置。估价以 Decimal 字符串冻结，提交前复核原报价、当前价格和金额上限；实际费用缺少账单时保持未知。

保存或启动配置均不发送测试生成，现有验证按钮只验证 DeepSeek。配置 API 返回 `videoApiKeyConfigured`、`videoPermissionStatus=unverified`、只读探测结果 `videoProbe`（鉴权通过 / 未通过 / 未完成，含主机与时间）、参考估价 `videoEstimate`、`videoMode` 与当前 `activeVideoMode`（保存后一致）及 `restartRequired/restartFields`（K2 起恒为空），不返回 Key。保存视频 Key 或业务空间时执行一次零费用只读探测（不存在任务 ID 的查询，不创建任务、不产生费用；探测失败不阻塞保存）；首次保存视频 Key 且模式为 off 时自动切换到 live，用户显式选择后不再自动翻转。业务空间可留空、后补或以空串移除（回到共享域名），已保存字段对新登记即时生效、无需重启。Web 设置页默认只保留两个 Key 与模式开关，供应商、模型、地域、业务空间与金额上限收进「高级（可选）」。

提交歧义进入 `unknown`，不重提；查询按原 ID 每 15 秒推进，失败后按 15/30/60 秒有限退避，并保留更晚的 `Retry-After`。从提交意图起满 24 小时暂停查询。缺配置或空间不匹配时保存阻塞原因，不增加 HTTP 次数；`jobs retry-query` 先校验原配置和期限，仅恢复查询窗口。查询/下载阶段不因后来调低金额上限而丢弃已受理结果。

媒体下载限定服务端批准的 HTTPS 主机；每次跳转校验来源，解析到公网地址后固定连接地址并验证原 TLS 主机名。使用独立无鉴权客户端，最多 256 MiB，连接/读取/总超时为 10/30/180 秒，一个自动窗口最多 3 次尝试，重启不刷新额度。文件先写入 `.part`，校验自包含 MP4、H.264、1280×720 和 5 秒规格，计算 SHA-256，再原子发布文件并提交结果；不依赖运行时 FFmpeg。

媒体位于数据目录的 `media/<projectId>/<mediaId>.mp4`，CLI 返回相对位置。`GET /api/media/{mediaId}` 返回元数据与当前可用性；`GET/HEAD /api/media/{mediaId}/content` 支持单 Range，`?download=1` 提供附件下载。播放器只在当前媒体可用时出现，SSE 与快照更新保留同一播放器节点。缺失或损坏的文件返回 410，历史结果保持原样；修复复用原 ID 且必须匹配原 SHA-256。冲突文件需备份移走后再重试；过期 URL 不刷新，也不触发重新生成。

旧 off/mock Run 按原工具、规则与检查点签名恢复；live 使用 tools/rules v3。原 live 配置不可用时返回 `RESUME_CONFIG_CHANGED`，保留等待与累计预算。公开 Job、SSE、模型消息和 CLI 查询不包含业务空间、签名 URL 或私有供应商诊断。`await_job` 在 `downloading` 继续等待，本地提交后才交付；下载失败、等待到期或停止后，后续修复只更新 Job，不复活已结束 Run 或改写历史工具结果。

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

断电或强退可能留下 `instance.lock`，仍需确认原进程已经退出后手动移除该锁，再执行 `resume`。恢复验证记录见 [持久恢复验收](./RECOVERY_ACCEPTANCE.md)。

## 查看模型与缓存用量

执行 `run`、`chat`、`resume` 后会显示用量摘要。`inspect` 中的每个 Run 也增加 `usage` 汇总。查看单次调用明细可使用以下命令，不需要 Key，也不会调用模型：

```powershell
.\.venv\Scripts\python.exe -m vagent usage --session coffee
.\.venv\Scripts\python.exe -m vagent usage --run RUN_ID
```

`usage` 只显示运行标识、模型、状态及用量，不输出对话和产物正文。原始调用明细保存在 `state.json` 的 `runs[runId].modelCalls` 中，汇总从这些明细计算。

`vagent usage --cost` 在既有输出上追加按参考价目表计算的金额（默认不显示，不写状态）。两个配套离线脚本：

```powershell
.\.venv\Scripts\python.exe scripts\compare_cost.py --home <已有数据目录>   # 缓存感知 vs 缓存盲
.\.venv\Scripts\python.exe scripts\evaluate_behavior.py --home <空目录> --output <新路径> --k 2
```

`scripts/compare_cost.py` 使用只读快照：不迁移、不执行崩溃恢复、不创建目录，`--home` 打错直接报 `STORE_NOT_FOUND`。`scripts/evaluate_behavior.py` 默认离线、零费用；`gate` 用例必须每个样本都通过，`measure` 用例只报告指标，`--k` 不能超过样本数，`--live` 会被拒绝。

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

这里统计的是服务商报告的上下文缓存用量，不是本地 Redis 命中数，也不代表减少了上下文窗口占用。文本模型尚未加入价格表或人民币费用估算；C1 的视频估价单独保存在 Job 中。验证范围见 [用量观测验收](./USAGE_ACCEPTANCE.md)。

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

`usage.answerCache` 单独累计 `hit`、`miss`、`invalid`、`error`、`stored`、`skipped`，未发生的项省略；`error` 包含读失败和写失败，并非模型失败数。命中不会新增 `modelCalls`、`modelSteps` 或输入/输出 Token，不能将旧响应 Token 当作本次服务商缓存用量。执行时也显示 Redis 事件。验收证据见 [Redis 缓存验收](./REDIS_CACHE_ACCEPTANCE.md)。

## 实现与代码入口

```text
src/vagent/
  cli.py       argparse 命令行与配置展示
  application.py CLI/Web 共用的生命周期与执行服务
  web.py       回环 HTTP API、CSRF、SSE 与打包页面
  headless.py  E3：headless 两阶段计划/授权/集合等待与本地拼接交付
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
  storage.py   单写锁、事务、原子替换、操作日志、schema 迁移与逐字节备份
  skills.py    技能发现、元信息与按需正文读取
  contracts.py B0：不依赖 Store/SDK 的严格 JSON 类型
  waiting.py   通用执行上下文、延迟结果和版本化等待指针
  wait_runtime.py B3：持久等待、结果领取/交付、停止、计时与恢复协调
  video/contracts.py v1 Mock / v2 live 请求、Job、费用、媒体与供应商契约
  video/jobs.py B1：原子登记、请求冻结、去重、版本检查和启动恢复
  video/tools.py off/mock/live 独立视图、分镜/成片/分集工具、等待解析与历史兼容
  video/worker.py 串行提交/查询、超时、持久重试、配置阻塞与停止
  video/media.py C2：安全文件访问、完整性、原子提交、可用性与幂等修复
  video/media_worker.py C2：独立串行下载、持久额度/期限与强退恢复
  video/media_http.py C2：HTTPS 来源策略、公网 DNS 固定连接与流量上限
  video/media_response.py C2：已核验文件的 GET/HEAD、Range 与下载响应
  video/mp4.py C2：有界 MP4 结构/样本校验与实测元数据
  video/copy_signature.py E1：mp4-copy-signature-v1 流复制签名（codec 配置、时间线、edit list 与 priming）
  video/assembler.py E1：包内 FFmpeg 定位/指纹、按样本推导的接缝时间线、concat 流复制与输出校验；M4 在签名不兼容时回退 ffmpeg-concat-normalize-v4（1280×720/30fps + AAC 48k）
  video/films.py E1：Film/FilmVersion/FilmMedia 契约、只增不改校验、输入选择与启动清理
  video/film_service.py E1：锁内冻结、锁外拼接、重获锁复核后一次提交与指纹幂等
  video/subtitles.py M6 E1：SubtitleTrack/SubtitleTrackVersion 契约、subtitle-align-zh-v1、cue 校验与 SRT/ASS 序列化
  video/subtitle_service.py M6 E1：锁内冻结、锁外字幕软封装/烧录、复核后一次提交与渲染指纹幂等
  video/subtitle_font.py M6 E1：包内 OFL 字体定位、SHA-256 校验与 FONT_MISSING
  video/audio.py M6 E2：schema 7 的 audio 媒体分支、AudioAsset 域、TTS 额度常量与逐集/只增不改校验
  video/tts.py M6 E2：tts-providers-v1 能力表、local-tts-v1 确定性 WAV、CosyVoice 受控适配器与错误映射
  video/audio_service.py M6 E2：audio_generate 的锁外 TTS、额度/音色绑定/幂等复用与 AudioMedia+AudioAsset 一次提交
  video/mixer.py M6 E2：mix-zh-v1 混音、侧链 ducking、两遍 loudnorm 与 EBU R128 成品复测
  video/renders.py M6 E3：有效时间线、fade/crossfade 校验、cue 夹取、标题卡与 RenderTask 只增不改进度
  video/render_service.py M6 E3：film_render/render_read、逐场归一化、跨场组装、音频放置、1080P、校验与幂等提交
  video/timelines.py M7 E3：timeline-zh-v1 契约、只增不改时间线、裁剪/转场/音轨/标题卡校验与指纹
  video/audio_import.py M7 E3：客户端 WAV/MP4 受控导入、许可与来源校验、SHA-256 去重与音频媒体落盘
  video/seasons.py M7 E1：Season/SeasonVersion 契约与季成员只增不改校验
  video/batches.py M7 E2：Batch/BatchEpisode 契约、逐集登记、恢复与 Store 崩溃收尾
  video/routing.py M8 E1：provider-route-v1 只读路由预览（每供应商一行、原因优先级、跨币种并列、无时钟指纹）
  video/billing.py M8 E2：billing-v1 手工账单侧车账本（幂等/冲突/替换、逐币种汇总、原子替换与损坏保护）
  video/providers/gate.py M8 E2：逐 provider 提交间隔与 429 冷却、PROVIDER_BACKOFF 诊断、统一错误表常量与内存指标
  web/bill-state.js M8 E2：账单币种行、差额符号、记录行与写入体的纯状态逻辑
  web/subtitle-state.js M6 E1：字幕面板的纯状态逻辑（版本、cue、导出请求与受控下载）
  web/render-state.js M6 E3：整片渲染请求、逐场进度摘要与旁挂 SRT 的纯状态逻辑
  web/timeline-state.js M7 E3：时间线草稿、裁剪/转场/预览/保存体与本地校验的纯状态逻辑
  web/season-state.js M7 E1/E2：季成员、批次确认/恢复与状态文本的纯状态逻辑
  web/film-state.js E1：成片面板的纯状态逻辑（版本列表、播放/下载、显式拼接请求）
  video/episodes.py M5 E0：Episode/Scene/Character/Style 契约、只增不改版本与集级预算不变量
  video/adaptation.py M5 E1：text-split-zh-v1 确定性章节/场景/对白切分与镜头草案
  video/consistency.py M5 E2：角色/风格提示模板与跨镜头一致性证据
  web/episode-state.js M5 E4 / M6 E4：分集面板的纯状态逻辑（逐场汇总、集级预算、成片标签、整集成片投影、SRT 解析与 cue 查找）
  video/storyboards.py D1：分镜/镜头/素材域契约、只增不改版本与提交级不变量
  video/views.py D1/D3：工具与本地客户端共用的 Job/分镜有界投影（含镜头媒体派生）
  video/assets.py D3：素材库导入（魔数/尺寸/哈希校验、按项目去重、文件恢复）与只读读取
  video/clients.py D3：显式本地动作（逐镜头重做、批量登记）的客户端 Run 与幂等重放
  video/usage.py D3：Jobs、估算/账单状态、分镜与素材、模型 Token 覆盖率的读取侧聚合
  web/storyboard-state.js D3：分镜面板的纯状态逻辑（分组、缩略图、连播、估算汇总）
  video/providers/mock.py B1：独立持久上游账本与服务端模拟轨迹
  video/providers/wan.py C1：固定北京协议、独立 HTTP 客户端与提交歧义处理
skills/        内置 SKILL.md，随 wheel 分发
tests/         pytest 行为与协议测试
scripts/probe_m1b_wait.py B0：独立临时目录中的持久等待实验
scripts/evaluate_m6.py M6 E4：离线整集验收套件（字幕/配音/转场/标题卡/失败隔离/拒绝矩阵）
```

LangGraph 提供图执行底座，项目自己定义状态、路由、上下文策略、工具边界、版本控制和运行记录。`ChatDeepSeek.bind_tools(...).astream(...)` 只执行单个模型步；保留非流式 `generate` 接口以兼容测试和其他调用方。流式工具参数必须通过严格 JSON 解码和完成标记检查后才执行，模型不会直接执行工具。

- **工具白名单**：默认 `project_read`、`project_update`、`plan_update`、`artifact_save`、`artifact_read`、`skill_read`；mock/live 模式增加各自版本的视频工具（当前工具面 18/18，见上文工具表）。不开放 shell 或任意文件路径。
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

`VAGENT_CONTEXT_VERSION=3` 时，被裁掉的旧轮次会换成一条压缩摘要；`VAGENT_CONTEXT_VERSION=4` 时，系统消息只保留规则、Skills 与项目事实，摘要、记忆块和剩余预算移到历史之后的一条**尾部动态消息**（每步重新合成，不写入历史/检查点）。尾部角色默认 `system`，可用 `VAGENT_CONTEXT_TAIL=human` 回退；切换版本或角色都会改变 `contextSignature`，旧 Run 需要新请求。v2 仍是新 Run 的默认值，v1/v2/v3 的布局与签名不变。v4 的尾部角色已用 1 次真实 DeepSeek 调用验证（模型正确读到了尾部预算）。

项目事实和 Skill 元信息使用紧凑、键顺序稳定的 JSON。发送给模型的应用工具结果只去掉 JSON 字符串外的冗余空白，保留字符串正文、转义、数值原文及工具调用 ID；存储中的原始对话和产物不改写。

`VAGENT_CONTEXT_BYTES` 默认 98304（M8 E3 起；18/18 工具面在旧 65536 下第一轮即 `CONTEXT_LIMIT`），按消息和工具定义的 UTF-8 序列化字节数衡量，**不是精确 Token 数或供应商请求体大小**。供应商返回的 Token usage 另行记入 Run。

超限时裁剪最旧的完整用户轮次，保留工具调用与结果配对；原始历史不删除。当前需求、当前轮工具结果和项目事实必须完整保留，仍超限则在调用模型前报 `CONTEXT_LIMIT`。长产物通过 `artifact_read` 的 offset/limit 分段读取。

Run 保存 `contextVersion`，`inspect` 和 `usage` 可查看。已有检查点 Run 缺少该字段时按 v1 恢复，沿用原来的布局、空白和配置指纹；后续新 Run 使用 v2。未知格式版本会在恢复前报错。无检查点的早期 Run 仍不能恢复。对比数据和验证范围见 [上下文优化验收](./CONTEXT_OPTIMIZATION_ACCEPTANCE.md)。

内置 [video-brief](../skills/video-brief/SKILL.md) 和 [shot-description](../skills/shot-description/SKILL.md)。启动时校验并保留内容快照，初始上下文仅包含名称、描述和内容版本；`skill_read` 后才加入正文。

自定义技能目录结构为 `skills/your-skill/SKILL.md`：

```markdown
---
name: your-skill
description: 说明哪些任务需要这个技能。
---

使用现有工具完成任务的操作指引。
```

名称必须与目录一致，每个文件最多 16 KiB。默认按「内置根 → `VAGENT_HOME/skills/`」发现，每个根内按目录名排序；内置名称优先，用户重名会被忽略并在 `vagent skills list` 的 `diagnostics` 中以 `SKILL_NAME_CONFLICT` 报告。用户目录不存在视为空；坏用户文件（非法 frontmatter、超长、符号链接/目录联接）只记录 `INVALID_SKILL`/`INVALID_SKILL_FILE` 诊断，不使应用启动失败。内置加用户的有效条目合计最多 32 个：先保留内置，再按名称取前，其余逐条报告 `SKILL_LIMIT`。`VAGENT_SKILLS_DIR` 仍指定可信本地目录并**整体替换**内置根（保持原有严格报错语义），不再合并用户根。修改后重启加载；不支持自动执行脚本、联网安装或热更新，Skill 不能增加工具权限或跳过代码校验。新增或修改 skill 会改变新 Run 的签名；恢复旧 Run 时若当前 catalog 与记录不一致，会返回 `RESUME_CONFIG_CHANGED`，不会把新用户文件悄悄注入旧 Run。

`vagent skills list` 输出 `{"skills": [...], "diagnostics": [...]}`；诊断只含代码、目录名和固定说明，不含正文或私有绝对路径。Web 的「编排观测」面板同样显示未加载的用户 skill 诊断。

## 高级开关（默认关闭）

以下开关用于 Agent Harness 的可选能力（上下文摘要、长期记忆、提示词版本与追踪），默认关闭，在 `.env` 或环境变量中设置后重启生效。除 tracing 外，改动通常进入 Run 的 `contextSignature`，恢复旧 Run 需要原配置；关闭后回到 v2 基线。

| 开关 | 默认 | 取值 / 作用 | 对签名 / 恢复的影响 |
|---|---|---|---|
| `VAGENT_CONTEXT_VERSION` | `2` | `2` 稳定前缀；`3` 历史摘要；`4` 尾部动态消息（配合 `VAGENT_CONTEXT_TAIL=system\|human`） | 版本改变签名；旧 Run 需新请求 |
| `VAGENT_MEMORY` | `off` | 开启 `memory.json` 项目级长期记忆，召回已确认事实 | 开启后记忆块进入上下文并改变签名 |
| `VAGENT_MEMORY_LIMIT` | `4` | 每个项目最多渲染的记忆条数（1–16） | 改变上下文内容与签名 |
| `VAGENT_PROMPT_VERSION` | `v1` | `v1` 冻结；`v2` 追加数据通道硬化子句 | 模板文本进入签名，切换即拒绝恢复旧 Run |
| `VAGENT_TRACING` | `off` | 开启 `traces/*.jsonl` span 导出 | 不改变签名；默认不写文件 |
| `VAGENT_TRACE_SAMPLE` | `100` | 采样百分比（0–100） | 不改变签名 |
| `VAGENT_TRACE_CONTENT` | `off` | 是否把提示词 / 回答正文写入 span | 不改变签名；开启后 trace 含正文 |

`VAGENT_CLOSING_TURN`（缺省 `on`）控制预算耗尽后的无工具收尾轮；`VAGENT_CONTEXT_TAIL` 只对 v4 有效。两者已在前文说明。真实费用仍以 `usage bill` 手工台账为准，`usage --cost` 只是参考价目表下的估算。

## 数据保留、快照与恢复（M3）

`state.json` 的增长有明确上限，所有清理默认只预览、不写盘：

- **事件封顶**：每个 Run 只保留最近 500 条事件；丢弃数由首个保留事件的 `sequence−1` 推出（不新增存储字段），裁剪后 `sequence` 仍单调。流式草稿 `assistant.delta` 从不入库。
- **健康库不重写**：打开数据目录时的崩溃恢复只在真正改动数据时写 `state.json`；健康库的 mtime 不变。
- **`vagent gc`**：缺省 `--dry-run` 语义（只输出将删除的数量与字节），`--apply` 才清理。保留窗口 `--days` 默认 30：只处理 `completed`/`failed`/`cancelled` 且 `updatedAt` 早于窗口的 Run，清除其 `events`、`modelCalls`（答案、状态、token 总数与产物保留），以及指向这些 Run 的 `operations` 与 `waits`；`running`/`waiting_external`/`interrupted` 永不清理。清理时间记在同目录旁路 `gc.json`（`lastGcAt`），`state.json` 形状不变。
- **last-good 快照**：每 20 次成功事务按低频刷新 `state.last-good.json`，也可用 `vagent snapshot --confirm` 显式刷新。损坏时用 `vagent recover --from last-good --confirm` 恢复；当前文件先留存为 `state-invalid-<UUID>.json`，不会无痕覆盖。
- **陈旧实例锁**：`instance.lock` 记录 PID；只有该 PID 已不存在才回收（Windows 用 `OpenProcess`/`GetExitCodeProcess` 判断，绝不向进程发信号）。无法解析的锁按“可能仍在使用”处理，仍需人工确认后删除。
- **trace 轮转**：`traces/<traceId>.jsonl` 达到 16 MiB 时轮转为 `<traceId>.1.jsonl`（只保留一段），POSIX 权限 0600；Windows 依赖数据目录 ACL。恢复时 `previous_root` 会同时读取轮转段，trace 树仍连续。
- **成本口径**：有未跟踪模型步（`usageStartStep` 之前没有调用明细）的 Run，`usage --cost` 的 `status` 为 `partial`，带 `untrackedModelSteps`、`amountIsLowerBound` 与“金额是下限”的说明；最外层汇总同样标 `amountIsLowerBound`。

`scripts/measure_state.py` 离线合成 10/20/40 个 Run，输出 `state.json` 字节数与一次空事务的中位耗时（本机 Windows 参考：10/20/40 Run ≈ 0.55/1.10/2.21 MB，空事务 ≈ 12/25/39 ms）。事务耗时仍主要由 `runs[].messages` 决定，把消息搬到旁路文件属于后续的数据保留专项（C2），不在 M3 范围。

## 数据兼容与限制

默认数据目录为 `~/.vagent/`，可用 `VAGENT_HOME` 覆盖。`state.json` 保存会话、项目、产物、Run、操作结果、jobs/waits、media 索引与 M2 的 `storyboards`/`assets` 集合、M5 的 `episodes`/`scenes`/`characters`/`styles`、M6 的 `audio`/`subtitleTracks`/`renders` 与 M7 的 `seasons`/`batches`/`timelines` 集合；E3 的 headless 计划、授权指纹、登记结果与交付摘要保存在 Run 记录的可选 `headless` 字段（未使用时不写该字段，不改变存储 schema，也不迁移旧记录）；`checkpoints.sqlite` 保存图执行位置和消息。B1 的 `mock-video.json` 独立保存模拟上游受理记录、请求、轨迹和调用计数。应在程序退出后备份完整数据目录。配置 Key 不写入状态；真实 Job 的私有上游 URL 仅保存在本地状态中，不经公开接口返回。`inspect` 会显示创作正文。

当前存储为 schema 9。打开 v1–v8 数据时，在实例锁下使用原版本校验器验证状态与消息，先将原始字节保存为同目录的 `state-v1-<UUID>.json` 等备份，再原子迁移。迁移只增加缺少的顶层集合（v4 新增 `storyboards`/`assets`，v5 新增 `films`，v6 新增 `episodes`/`scenes`/`characters`/`styles`，v7 新增 `audio`/`subtitleTracks`，v8 新增 `renders`，v9 新增 `seasons`/`batches`/`timelines`）并升级版本，历史 Job、Run、Operation、来源版本和指纹不重编码、不补写默认字段，也不修改 SQLite；运行恢复单独提交。`media` 索引在 v5 成为联合类型：旧的生成媒体记录保持原字段按原校验器解析，`contractVersion=2/kind=film` 才走成片校验器，旧媒体不补 `kind`、不改写。旧 Mock Job 保持 contract v1，新 live Job 使用 v2。缺少视频字段的旧 Run 按 off 解释，execution v1/v2 与上下文 v1/v2 按原签名恢复；execution/context/checkpoint 仍为 3/2/1，工具面自 1/1 起逐代追加（当前为 18/18），恢复旧 Run 时按 `toolFeatures` 身份重建冻结注册表并核对签名，签名逐字节不变。迁移写入失败、损坏数据和未知版本保留原状态文件。新版本数据不能交给更旧的应用直接读取；回退需在服务退出后恢复完整数据目录，单份状态备份不能替代它。

- 成功会话可跨重启继续；执行中的普通 Run 重启后标记为 `interrupted`，已提交产物保留，不自动重放。B3 保留有效的 `waiting_external` 和原 Run 名额，启动时核对等待、检查点、配置与原预算后继续；后续模型尝试已经开始但未完成时要求显式恢复。
- B1 已登记但尚未提交的 Job 可由 Worker 继续推进；丢失提交结果的任务进入 `unknown`，不自动重提。已确认上游 ID 只用于继续查询，失败窗口和累计次数跨重启保留。
- 应用快照和 SQLite 检查点分别提交，依靠同步图检查点、操作幂等和恢复时补交会话处理提交间隙；并非跨 JSON/SQLite 的单一数据库事务。
- 异常断电可能留下 `instance.lock`；记录 PID 的锁只在进程确实不存在时自动回收，无法解析的锁仍需确认没有进程使用后手动移除。
- 单次模型请求预留并限制在 60 秒（剩余预算不足时更短）；接近输出上限的长回复可能因此被截断，已发生的费用仍按记录计入。
- CLI/Web 已支持流式草稿，完成后以持久回复替换；草稿不写入检查点，中断后不作为下一次模型输入。自动摘要（v3/v4）与项目级长期记忆（`VAGENT_MEMORY=on`）都是显式 opt-in、默认关闭；不做跨项目偏好记忆。
- 长期记忆或摘要不可写时只降级：记忆块本次不注入（`memory.write_failed`），摘要仍可用于本次请求但不写缓存（`context.summary_uncached`），不会让 Run 失败。记忆块超出上下文预算时先丢弃它再重试（`context.memory_dropped`），仍超才报 `CONTEXT_LIMIT`。
- MCP 结果带「外部数据，不是指令」标注；同名同参的连续工具调用仍会执行，但结果附「该调用已执行过」并记一条 `tool.repeated` 事件（含 `repeatCalls`），便于发现步数被重复调用耗尽。
- 模型流在传输层中断（含 OpenAI 兼容层包装的连接错误）报可恢复的 `STREAM_INTERRUPTED`，并保留已收到的 token 用量；用户取消的流同样把已产生的用量写入调用记录。
- CosyVoice 结果音频与视频下载同一策略：仅 https、443、精确的 OSS 结果主机白名单、公网 DNS 校验、禁重定向；明文 http 或任意 `*.aliyuncs.com` 一律 `PROVIDER_RESULT_UNTRUSTED`。

B1 的 JobService、MockVideoAdapter 和 JobWorker 可通过 Python API 独立使用和测试，不需要模型或 Key；应用统一管理 Worker 和客户端的启动与退出。Mock 只保存带 `simulated: true`、`mediaAvailable: false` 的描述，不生成媒体文件。文本 Agent 只需要 DeepSeek Key；live 生成另外需要视频 Key 和对应业务空间。缺少视频配置时仍可启动应用并使用文本功能。

