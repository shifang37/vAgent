# M1-B 任务规划：模拟视频 Job 与持久等待

更新日期：2026-10-09。状态：**B0–B2 完成，B3–B5 待实施**。B1 提供 schema v2、Job/Mock Worker；B2 已注册四工具、off/mock 启动模式、服务端执行上下文、延迟结果、只读与缓存边界。CLI/Web 尚未管理 Worker 或挂起生产 Run，未执行真实模型联调。证据见 [B0 验收](./M1B_B0_ACCEPTANCE.md)、[B1 验收](./M1B_B1_ACCEPTANCE.md) 与 [B2 验收](./M1B_B2_ACCEPTANCE.md)。

依据：[M1 总计划](../M1_PLAN.md)、[Harness 设计](../AGENT_HARNESS_DESIGN.md)、[M1-A 验收](./M1A_ACCEPTANCE.md)。规划基线为 `a03f249`；当前能力以 [README](../README.md) 为准。数据、Job/Worker、模式配置和 Agent 工具已按 [M1-B 契约](./M1B_CONTRACTS.md) 落地，生产等待协调、Job 命令和页面仍为拟实施项。

M1-B 的交付目标是：用户通过 Agent 登记一个模拟视频任务，Worker 独立推进任务；Agent 可以先回复任务 ID，也可以持久等待结果。刷新页面或重启进程后，任务、原工具调用和预算保持一致，已确认的提交不重复执行。

## 1. 范围与实施约束

- 保留现有模型 → 工具 → 观察结果的循环，增加通用挂起与继续能力。Runner 只认识延迟工具结果，具体视频供应商由视频模块处理。
- 增加视频能力表、请求校验、JobService、MockVideoAdapter、进程内 Worker、四个 Agent 工具、CLI/Web 状态展示和验收入口。
- Mock 只交付持久化的模拟结果描述，始终带 `simulated: true`、`mediaAvailable: false`。本阶段不创建占位 MP4，不扩展现有文本 Artifact 为可播放视频。
- 默认 `videoMode=off`，显式启用 `mock` 后提供视频工具。未启用时保留 M1-A 的文本创作行为；模拟模式也不能宣称交付了真实视频。
- 离线开发和回归不需要任何 Key；真实 Agent 验收需要 DeepSeek Key，会消耗文本 Token。整个 M1-B 不需要视频 Key，也不调用收费视频 API。
- 沿用同一数据目录单写进程、至多一个未结束 Agent Run 的边界，`waiting_external` 也占用该 Run 名额。新消息返回 `RUN_BUSY`，不新增消息队列或多 Agent 调度。Worker 可继续处理此前已登记的 Job。
- 不包含真实供应商、媒体下载/播放、视频计费、图生视频、多镜头拆分、拼接、自动换供应商或云端取消。上述能力继续属于 M1-C 或后续阶段。

## 2. 现有代码与需要补齐的部分

| 位置 | 当前实现 | M1-B 工作 |
|---|---|---|
| `src/vagent/video/contracts.py`、`waiting.py` | B0 已定义视频/Job 与通用等待契约，并有独立离线验证 | B2/B3 接入工具执行与等待协调器 |
| `src/vagent/storage.py` | B1 已迁移 schema v2，保存 jobs/waits；Job 与登记 Operation 同事务，保留 v1 快照与旧指纹 | B3 接入等待记录和 Run 的跨存储协调 |
| `src/vagent/video/jobs.py`、`worker.py`、`providers/mock.py` | B1 已实现原子登记、两层去重、独立账本、串行 Worker 与持久查询重试 | B2–B4 接入工具、等待协调和应用生命周期 |
| `src/vagent/tools.py`、`video/tools.py` | B2 已支持服务端上下文、延迟结果与四工具；保留旧原始数据执行器和 MCP 只读限制 | B3 使用通用延迟结果挂起，不在工具内部触发图中断 |
| `src/vagent/runner.py` | B2 按工具模式生成规则、保存能力配置、绕过动态状态回答缓存；仍是 execution v1 | B3 增加持久挂起和原调用继续，保留旧版本路径 |
| `src/vagent/journal.py`、`checkpoints.py` | 活动时间按单次执行累计；JSON 与 SQLite 分别提交 | 暂停等待计时、同步中断检查点、处理两份存储的提交间隙 |
| `src/vagent/application.py` | 单活动任务；退出时停止运行；事件依附当前 Run | 管理 Worker/等待协调器生命周期；独立发布 Job 变化；支持停止等待 |
| `src/vagent/cli.py` | 直接调用 Runner；交互输入使用同步 `input()` | 接入共享等待协调；避免输入阻塞 Worker；明确退出后的 Job 行为 |
| `src/vagent/web.py`、`web/app.js` | SSE 只分发快照与文本增量；页面只识别现有 Run 状态 | 增加 Job 快照、Job 事件、等待态和查询恢复入口 |
| `src/vagent/cache.py` | 回答缓存键含项目/产物，不含 Job 与能力状态 | 视频工具可见时跳过应用回答缓存，防止复用过时的任务状态 |

B0 已验证等待机制，B1 提供 Job/Worker 和 v2 存储，B2 完成工具接入。B2 的未完成 `await_job` 保存 preparing 记录后返回延迟标记，execution v1 明确以 `EXTERNAL_WAIT_UNAVAILABLE` 结束 Run，不把它当作工具成功。B3 接入生产挂起与恢复；B4 接入 Worker 生命周期，仅注册工具不能视为整个 M1-B 已完成。

## 3. 契约设计

### 3.1 数据对象

| 对象 | 必需内容与规则 |
|---|---|
| `VideoCapabilities` | `provider`、`model`、能力版本、模式、合法规格组合、输入类型、取消/提交幂等支持标记。按模型校验时长、分辨率和画幅的组合，不把 Mock 示例值写进 Runner |
| `VideoRequest` | 提示词、选定模型和规格、可选 `sourceRefs[{artifactId, version}]`。来源为空时可直接以提示词生成；提供来源时必须指定存在且属于当前项目的版本 |
| `ProviderTaskHandle` / `ProviderTaskSnapshot` | 上游 ID、标准化状态、结果或有界错误。适配器保留 `capabilities`、`submit`、`query` 三个基本接口；取消仍为可选协议 |
| `Job` | ID、project/session/创建 Run ID、revision、模式、供应商/模型/能力版本、不可变请求快照、operationKey、requestFingerprint、状态、上游 ID、提交/查询记录、时间、结果和错误阶段 |
| `JobResult` | 模拟结果描述、来源版本和实际采用的参数；`simulated: true`、`mediaAvailable: false`、空的输出产物引用。结果与 Job 成功状态同一次本地提交 |
| `WaitBinding` | 服务端 operationKey、runId、modelStep、toolCallId、jobId、等待阶段/版本、开始与截止时间、自动恢复意图、最终工具结果及交付状态 |
| Run 扩展 | `waiting_external`、等待引用、独立的 `externalWaitSeconds`、保存的能力模式/执行版本；继续保留原预算、累计工具额度和模型调用记录 |

`projectId`、`runId`、`toolCallId` 和 operationKey 由执行上下文注入，模型不能自行指定所属项目、凭证、上游地址或提交幂等键。请求快照在 Job 创建时固定；之后修改源文本或配置不改变已登记请求。

### 3.2 四个 Agent 工具

| 工具 | 输入/返回 | 执行边界 |
|---|---|---|
| `video_capabilities` | 返回可用模型、合法规格和模拟标记 | 只读；能力由适配器提供 |
| `video_generate` | 校验 VideoRequest，返回本地 jobId、登记状态和模拟标记 | 本地写工具；Job 与成功 Operation 结果同事务提交，不在工具内提交上游 |
| `job_get` | 按 jobId 返回当前项目内的有界 Job 快照 | 只读本地状态，不发网络请求、不触发重提 |
| `await_job` | jobId；已结束则立即返回结果，否则登记持久等待 | 只读领域数据并保存执行记录；返回通用延迟结果，由 Harness 挂起，不循环请求模型 |

`--read-only` 隐藏并拒绝 `video_generate`，仍允许读取及等待已有 Job。等待记录属于执行记账，不能修改生成参数或新建 Job。工具返回的“已登记”与“已完成”必须区分。

去重分两层：

1. 原 `runId:modelStep:toolCallId` 重放复用已有 Operation 结果，参数变化仍返回冲突。
2. M1-B 每个 Run 最多新建一个视频 Job，提前验证 M1-C 的单次生成边界。模型换一个调用 ID 重复提交同一规范化请求时返回原 jobId；请求不同则返回 `JOB_ALREADY_EXISTS` 和已有 jobId。用户明确重做应另发新请求，不能靠自动重试创建第二个 Job。

来源越界、版本不存在、参数超出能力或重复请求冲突均在 Job 创建前拒绝，不能自动降低规格或拆成多个任务。

### 3.3 Job 状态与重试

主路径为 `pending_submit → submitting → queued/running → succeeded/failed`，允许上游直接返回终态。`unknown` 表示提交结果不确定；查询健康度另用 `queryState` 表示。

| 情况 | 持久状态与后续动作 |
|---|---|
| 已登记，尚未开始提交 | `pending_submit`；Worker 可在重启后提交 |
| 准备向上游提交 | 先记录 `submitting` 与提交尝试，再在 Store 事务外调用适配器 |
| 已取得上游 ID | 保存 ID 和状态；后续只查询该 ID |
| 明确未受理或生成失败 | `failed`，分别标记 `error.stage=submit/generate`；不创建替代 Job |
| 提交超时，或停在 `submitting` 且没有已确认 ID | `unknown`、需要核实；不自动再次 submit，也不伪称生成失败/已取消 |
| 查询暂时失败 | 保留最近一次已确认的生成状态；`queryState=retrying`，只重试 query |
| 查询重试耗尽 | `queryState=paused`、需要处理；不把查询失败改成生成失败。用户可对原 ID 恢复查询 |
| Mock 成功 | 结果描述与 `succeeded` 同事务提交；不存在真实媒体文件 |

初始查询策略：正常每 2 秒查询；连续查询错误后按 1/2/4 秒最多重试 3 次，成功后清零，耗尽后暂停。策略和 `nextPollAt`、重试次数保存在 Job 中，重启不刷新额度；测试使用可注入时钟，不真实等待这些间隔。

单次等待初始上限为 10 分钟，创建等待时保存截止时间。到期返回 `JOB_WAIT_TIMEOUT` 并停止该次自动等待，Job 继续独立跟踪。`unknown` 或查询暂停也及时返回明确工具错误；不能让 Run 永久挂起。再次等待由新的工具调用明确发起，仍计入原 Run 工具预算。

M1-C 可以在上游完成后增加下载阶段；届时本地 Job 的 `succeeded` 必须以真实媒体完整落盘为条件，不能直接沿用 Mock 的完成判据。

## 4. 等待、停止与恢复

### 4.1 持久等待路径

1. `await_job` 用原 operationKey 登记等待，保存原模型步、工具调用 ID 和当前批次位置。已经完成的同批工具继续通过原 Operation 日志复用。
2. B0 已验证采用 LangGraph `interrupt` / `Command(resume=...)` 与同步 SQLite 检查点。工具返回延迟标记后，在工具异常捕获范围之外挂起；终态必须同时核对 pending interrupts 与 next，不能只凭 next 为空判断结束。
3. 中断时记录已用活动时间，暂停活动计时；释放模型连接和图执行任务。当前批次尚未完成的调用不能进入下一次模型请求，也不能伪造为已成功的工具结果。
4. 等待先标记为准备中；确认图中断检查点保存后才能启用自动恢复。检查点就绪后重新读取 Job，处理“Job 先完成、等待后登记”的竞态。持久状态扫描负责补偿，内存通知只用于加速。
5. Worker 更新 Job 后，协调器将稳定的最终工具结果写入等待记录。只有仍允许自动继续、仍为最新 Run、预算和配置有效的等待才能领取恢复权；用等待版本检查保证同一时刻只执行一次。
6. 恢复使用原 toolCallId 回填 `ToolMessage`，继续完成同批剩余调用，再进入下一次模型决策。同批节点重放保持原 interrupt 位置，再复用已完成的 Operation 结果；重复通知不得重复加工具额度或追加结果。
7. 成功交付后清理等待状态。模型继续失败时保留原结果及累计用量，沿用 M1-A 的显式 `resume`，不让 Worker 重发已经尝试的模型请求。

`activeSeconds` 仅累计实际 Agent 执行；外部等待包含进程离线期间的等待时长，另行记录。等待及 Worker 查询不会增加模型调用数或 Token；恢复不重置 8 步、12 次工具、180 秒的原 Run 上限。

### 4.2 停止与应用退出

- 用户停止等待 Run 时，先持久关闭该等待的自动恢复意图，再补齐会话可见的终止工具结果。Job 继续被 Worker 跟踪；后续成功也不会自动唤醒已停止的 Agent。
- 停止与结果完成竞争时，以持久领取/停止状态串行裁决；若图已经继续执行，则触发现有取消机制，阻止后续模型或工具启动。停止不撤销已完成操作。
- 用户之后显式恢复原 Run，可以在原预算内读取同一 Job 或重新等待；已有较新会话请求时仍拒绝恢复旧 Run。
- 进程正常退出时保存仍在等待的意图，不能把退出等同于用户按下“停止”。Worker 停止调度；未取得确定结果的提交按 `unknown` 处理，查询保留原 ID 和进度。
- 重启仅自动恢复仍有效的外部等待。普通 `failed/cancelled/interrupted` Run 不因此被自动继续。若在结果交付后已经开始一次模型请求再崩溃，保留该次尝试，要求显式恢复。

### 4.3 跨存储一致性与兼容

JSON 领域状态与 SQLite 图检查点仍是两份存储，不承诺跨文件事务或网络调用恰好一次。需要保证可重复读取/交付、避免重复副作用，并保留无法判断的提交状态。

- B0 已在 [契约文档](./M1B_CONTRACTS.md) 明确等待准备、图检查点就绪、结果就绪、领取恢复、结果已交付各阶段的写入顺序。启动时核对两份存储；领取后但尚未调用模型的中断可补交，已经尝试模型请求的中断不得自动重发。
- schema v1 → v2 迁移在实例锁下进行：验证旧数据、保留迁移前快照、原子写入新状态。迁移失败保留原文件；未知版本拒绝打开，不重置为空库。
- 旧项目、文本产物版本、会话、Operation 指纹和模型用量保持。旧 Run 缺少视频字段时按 `off` 解释。
- 新等待使用新的执行版本；保留 execution v1 的原图路径、系统规则和工具签名，继续支持其上下文 v1/v2 检查点。不能因统一改写系统提示或全局增加工具，导致原 M1-A Run 无法恢复。
- 新版 Run 保存模式、工具/能力版本并纳入恢复校验。缺少适配器、配置不匹配、缺少检查点或剩余预算不足时，Job 状态仍可查看，自动恢复暂停并说明原因。
- 备份/回退以进程退出后的完整数据目录为单位；不只回退 `state.json` 而保留较新的 SQLite 检查点。保留现有实例锁处理规则，不自动抢占锁。

## 5. Worker、CLI/Web 与缓存

B1 的 Worker 已支持单步推进及 start/stop，同一 Store 的执行互斥保证串行处理到期任务；提交和查询均在 Store 事务外执行，有界超时后再持久化结果。任务队列来自 Store，每轮重新选择到期 Job，不能在单个 Job 上循环等待而阻塞其他任务。后续由 `ApplicationService` 管理其生命周期；无需 Celery、消息中间件或独立写进程。

Mock 通过服务端测试夹具选择排队、成功、生成失败、查询中断、已受理但响应丢失等轨迹；轨迹选择不暴露为模型参数。模拟上游账本独立于本地 Job 提交事务持久化，保存任务 ID、受理时间和调用计数，重启不重置轨迹。不能利用 Mock 私有账本自动消除统一接口本来无法判定的 `unknown`。

| 入口 | 拟定行为 |
|---|---|
| `VAGENT_VIDEO_MODE=off\|mock` | 启动时读取，默认 off；配置展示包含模式和来源。本阶段不做模式热切换 |
| `vagent web` | 在应用生命周期内推进 Job 和有效等待；浏览器关闭不停止服务中的任务 |
| `vagent run/chat/resume` | 通过共享服务执行；遇到 `await_job` 时 CLI 等待持久结果通知，图本身处于挂起状态。若 Run 先结束则返回 jobId |
| `vagent jobs list [--session ID]` / `vagent jobs get JOB_ID` | 查看本地状态，不要求 DeepSeek Key；list 可按会话过滤 |
| `vagent jobs work` | 持续推进已登记 Job 与有效等待，供没有 Web 的场景使用；不新建 Run。缺少原模型配置时只推进 Job，保留结果供显式恢复 |
| `vagent jobs retry-query JOB_ID` | 仅恢复已知上游 ID、查询已暂停的 Job；重置查询重试窗口，不重新 submit |
| `GET /api/jobs/:id` | 返回本地 Job 快照，不触发执行 |
| `POST /api/jobs/:id/retry-query` | 恢复原任务查询，沿用本地服务的 Host/Origin/CSRF 校验 |
| 会话快照与 SSE | 快照增加当前项目的 Job 索引；增加 `job.updated`、`run.waiting` 和恢复通知，文本增量维持现有协议 |

CLI 进程退出后没有后台守护进程继续运行；重新启动 Web 或 `jobs work` 才继续处理持久队列。同一数据目录被 Web 占用时，独立 CLI 仍提示占用，不能绕过实例锁。`chat` 的等待输入需要可取消的非阻塞实现，并覆盖 Windows 退出清理，避免同步 `input()` 冻结 Worker。

`run/chat/resume` 中的 Ctrl+C 沿用“停止当前 Agent”的语义；`jobs work` 中的 Ctrl+C 仅关闭本地工作循环并保存待处理状态。启动模式控制新 Run 的工具集合；Worker 仍识别已登记 Job 保存的模式，恢复 Agent 时另行校验其原配置。

Web 在任务卡片显示模拟标记、jobId、模型/参数、来源版本、排队/运行/完成或错误状态；等待时可停止 Agent，查询暂停时可恢复查询。Run 已结束后 Job 卡片仍更新。无真实媒体时没有播放/下载入口，不展示伪造进度百分比。

Job 事件绑定 Job 自己的 session/run 信息，不能写到当前活动的其他 Run。状态先落盘再通知；客户端按 jobId + revision 忽略重复/旧事件，重连和队列溢出用包含 Job 的完整快照恢复。SSE 服务端需要显式分发新增事件类型，不能把所有非快照事件都当作 `assistant.delta`。

视频工具可见时，整次 Run 跳过 Redis 最终回答缓存，包括只读请求，避免在模型读取 Job 之前命中过时文本。`off` 模式保持原缓存行为；不改变 DeepSeek 自身的前缀缓存与 Token 统计。恢复与能力配置指纹必须覆盖新的规则版本。

## 6. 工作包、依赖与完成门槛

保留总计划的 B0–B3 编号，新增 B4 入口交付、B5 验收，避免把界面和打包留成隐含工作。下表的 `video/` 指 `src/vagent/video/`，其余未带目录的 Python 文件位于 `src/vagent/`。

| 工作包 | 依赖 | 任务与主要落点 | 完成门槛 |
|---|---|---|---|
| **B0 契约与等待验证（已完成）** | M1-A 已验收 | 数据、状态、工具返回、错误与迁移协议；LangGraph 同批中断/继续；提交顺序和 execution v1/v2 兼容路径；新增契约类型、实验与回归 | 离线验证通过，同批多等待与五处强退按原调用恢复，旧图指纹和恢复结果保持；见 B0 验收记录 |
| **B1 持久 Job 与 Mock Worker（已完成）** | B0 | 新增 `video/jobs.py`、`video/worker.py`、`video/providers/mock.py`；存储迁移、请求冻结、两层去重、持久 Mock 轨迹、查询重试与恢复查询 | 不接模型也能登记/完成/失败；61 项新增回归、七处强退及旧检查点迁移恢复通过，见 B1 验收 |
| **B2 视频工具与能力接入（已完成）** | B1 | 四工具、通用上下文/延迟结果、启动模式/来源、能力配置指纹、按模式规则、只读和缓存边界 | 58 项新增回归；确定性模型创建唯一 Job，非法参数/来源拒绝，off/MCP/旧检查点兼容，详见 B2 验收 |
| **B3 持久等待与恢复** | B2、B0 验证结论 | 扩展 `runner.py`、`journal.py`、`checkpoints.py`；新增通用等待协调模块；完成自动继续、停止、计时、退出/启动扫描和故障间隙处理 | 等待零新增模型调用；原调用结果交付一次；停止后不唤醒；提交/等待/恢复各强退点通过，原预算不增加 |
| **B4 CLI/Web 交付** | B3 | 共享服务管理 Worker；CLI 查询/工作循环/非阻塞交互；Job API、快照和 SSE；`web/app.js`、`index.html`、`styles.css` 增加状态卡片和模拟标记 | CLI 与 Web 使用同一状态；页面刷新补齐；Run 完成后仍见 Job 更新；等待、停止、查询暂停/恢复可操作 |
| **B5 验收与打包** | B4 | 新增 `scripts/evaluate_m1b.py` 和 `docs/M1B_ACCEPTANCE.md`；扩展 wheel smoke；更新 README 和总计划 | 离线故障矩阵、真实 DeepSeek + Mock 套件、仓库外 wheel 流程有独立证据；未验证部分明确保留，不提前勾选完成 |

实施顺序为 **B0 → B1 → B2 → B3 → B4 → B5**。每包交付后记录代码、验证结果和剩余限制；B0 的等待验证与 B3 的跨存储恢复是主要风险，不能用内存回调成功代替持久恢复验收。

## 7. 验收矩阵

按职责增加测试，并复用已有恢复、只读、流式和缓存夹具。B0/B1 已有契约、Job、Worker、迁移测试；B2 增加 `test_video_tools.py`、`test_video_integration.py`，扩展旧图兼容测试。下表保留整个 M1-B 的验收矩阵；生产等待、页面和真实模型联调仍待 B3–B5，不能由当前服务与工具测试代替。

| 场景 | 必须独立断言的结果 | 证据方式 |
|---|---|---|
| 默认关闭 / 显式模拟 | off 维持无视频能力；mock 的能力、工具结果、界面与答复均明确模拟 | 单元测试、真实 Agent 样本 |
| 正常生成 | 能力查询 → 唯一 Job → 排队/运行 → 模拟成功；提交次数为 1、无真实媒体声明 | 离线端到端、真实 Agent 样本 |
| 非法规格 / 产物来源 | 非法组合、跨项目 ID、不存在版本均失败，新增 Job 与提交次数为 0；合法来源之后被修改也不改变请求快照 | 契约/工具测试 |
| 请求和调用重放 | 重复 clientRequestId、原调用重放、同 Run 换调用 ID 均不重复新建或 submit；不同参数得到冲突 | 存储/Runner 测试 |
| 等待 / 批次 / 快速完成 | 等待期间模型计数不变；同批前后工具各执行一次；Job 先完成也不丢失结果；等待调用只占一次额度 | 确定性模型与时钟 |
| 提交崩溃 | Job 落盘后尚未提交可继续；提交标记落盘后无确定结果进入 unknown；已保存上游 ID 只 query | 子进程强退 + 独立 Mock 账本 |
| 等待崩溃 | 覆盖等待准备后、同步图中断后、Job 终态后、领取恢复后、结果已提交但图未提交后重启 | 子进程强退；核对原 ID、工具结果数与提交数 |
| 重复唤醒 / 模型响应丢失 | 重复完成事件或并发 resume 不并行执行图；恢复中模型请求已开始后崩溃不自动重发 | 协调器/模型调用记录 |
| 停止与新会话 | 等待中停止后 Job 可成功，模型计数不增加；停止后新请求不能被旧结果覆盖；显式恢复遵守最新 Run 校验 | Runner/API 竞争测试 |
| 查询 / 失败 / 等待到期 | 生成失败、提交不确定、查询错误、等待超时各有独立结果；仅查询错误可恢复查询，submit 次数不增加 | 故障注入、虚拟时钟 |
| 预算与配置 | 长等待及离线时间不耗活动预算；模型/工具额度累计；配置缺失或变化保留结果而不自动调用模型 | Journal/恢复测试 |
| 旧数据与只读缓存 | schema v1、execution v1、上下文 v1/v2 可按原条件恢复；旧操作不重放；只读不能创建 Job；视频可见时不命中旧回答缓存 | 既有夹具与新增回归 |
| 页面与生命周期 | Job 在 Run 完成后继续更新；事件重复/乱序、溢出与重连均恢复一致；CLI 输入和退出不挂住 Worker/锁 | API/SSE、浏览器、CLI 子进程 |
| 安装与旧功能 | wheel 在仓库外运行完整离线 Job/等待/重启流程；文本保存、质量校验、MCP、流式和原恢复流程继续通过 | 独立安装与回归结果 |

真实 DeepSeek 验收使用 Mock 视频适配器，覆盖五类用例：登记后回复、等待成功后引用结果、生成失败后如实说明、能力不满足时纠错/澄清、未启用视频时说明能力边界。跨重启与严格调用次数主要由确定性夹具验证，不把概率性自然语言输出当作幂等证据。

拟定评测入口默认离线，`--live` 才发起真实文本模型请求；每个 Run 沿用 8/12/180 限额，完整五类套件最多 40 次模型调用，任一用例失败立即停下。显式续跑沿用原 Run 与套件累计预算，保留此前报告；不能用重发新 Run 替代失败证据。报告记录 Job/源版本/原 toolCallId、submit/query 次数、恢复次数、等待与活动时间、已知 Token 和未知用量。

实施后的基础验证沿用仓库入口：

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/python
```

随后执行新增的离线评测及独立 wheel 验证；真实调用只在明确执行 B5 联调时进行。发布 PyPI、真实视频验证和生产成功率评估不属于上述结果。

## 8. 阶段完成清单

- [x] B0：契约、迁移协议及持久等待验证完成；实际 schema 迁移见 B1。
- [x] B1：Mock Job、Worker、两层去重与故障状态完成，见 [B1 验收](./M1B_B1_ACCEPTANCE.md)。
- [x] B2：四工具、模式、只读与缓存边界完成，见 [B2 验收](./M1B_B2_ACCEPTANCE.md)。
- [ ] B3：等待、停止、计时和跨重启恢复完成。
- [ ] B4：CLI/Web 状态与操作闭环完成。
- [ ] B5：离线/真实证据分开记录，独立安装通过，验收文档完成。

下一项可执行任务为 **B2：接入四个视频工具与服务端执行上下文，增加 off/mock 模式、只读边界、按模式的规则和回答缓存策略；继续保持 execution v1 兼容。**
