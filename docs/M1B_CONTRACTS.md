# M1-B 契约与恢复协议

日期：2026-10-09。B0 提供契约/等待实验，B1 提供 Job/Mock Worker/schema v2，B2 接入四工具和模式，B3 已接入生产等待/停止/计时与跨存储恢复。新 mock Run 使用 execution v2；文本及历史 execution v1 保持原路径。Worker 应用生命周期与 Job 入口待 B4。

范围依据：[M1-B 任务规划](./M1B_PLAN.md)。验证证据见 [B0 验收](./M1B_B0_ACCEPTANCE.md)、[B1 验收](./M1B_B1_ACCEPTANCE.md)、[B2 验收](./M1B_B2_ACCEPTANCE.md) 与 [B3 验收](./M1B_B3_ACCEPTANCE.md)。

## 1. 模块与版本

| 模块 | 已实现内容 |
|---|---|
| `src/vagent/contracts.py` | 不依赖 Store 或模型 SDK 的严格 JSON 类型、UTC 时间、标识符、不可变数组 |
| `src/vagent/video/contracts.py` | VideoCapabilities、VideoRequest、产物引用、供应商协议/错误、Job/JobResult、查询策略与合法状态迁移关系 |
| `src/vagent/waiting.py` | 通用 ToolExecutionContext、延迟结果、WaitBinding、ResumeToken、成功/失败工具结果；不导入视频模块 |
| `src/vagent/video/jobs.py`、`worker.py`、`providers/mock.py` | B1：原子 Job 登记、两层去重、来源冻结、独立上游账本、串行 Worker、查询恢复 |
| `src/vagent/video/tools.py`、`tools.py` | B2：四工具、服务端上下文、原样返回已记账结果/延迟标记、能力配置和规则版本 |
| `src/vagent/wait_runtime.py`、`runner.py`、`checkpoints.py`、`journal.py` | B3：通用等待解析/领取/交付、同 Store 执行互斥、同步中断/节点结果补偿、原预算与独立等待时间 |
| `scripts/probe_m1b_wait.py` | 使用真实 FileStore、项目工具和 SQLite 的离线图实验；不向应用注册工具或启动后台服务 |

| 版本维度 | 当前值 | 后续规则 |
|---|---|---|
| 视频对象 `contractVersion` | 1 | B0 Job 的字段契约；与存储/执行版本分开 |
| JSON `schemaVersion` | 2 | B1 在实例锁下自动迁移 v1，先保存原始快照，再增加 jobs/waits |
| Run `executionVersion` | 1 或 2 | 未注册等待解析器时默认 1，新 mock 默认 2；恢复选择 Run 原版本，`CHECKPOINT_VERSION=1` 未全局升级 |
| Run `contextVersion` | 1 或 2 | 与 executionVersion 独立；两种旧布局都需要继续支持 |
| `toolFeatures.video.toolsVersion` / `rulesVersion` | 2 / 2；旧版为 1 / 1 | 当前规则支持挂起；历史 execution v1 选择 B2 原规则/工具描述，能力、Schema 与系统规则均校验签名 |

契约使用 Pydantic 严格校验、camelCase JSON 和 `extra=forbid`。请求及嵌套规格/来源不可变；JSON 数组在内存中转成 tuple。持久写入使用 `model_dump(mode="json", by_alias=True)`，读取使用 `model_validate`。不要用跳过校验的 `model_construct` 或 `model_copy(update=...)` 形成待提交状态。

## 2. 能力、请求和来源

每个适配器实例对应一个已配置的 `(provider, model)`，`capabilities()` 返回能力版本及完整合法规格组合。首版输入类型为 text；参考图、视频延长和真实媒体在后续契约版本扩展。

启用 mock 后先读取 `video_capabilities`，按实际返回的模型和规格构造请求。当前默认请求结构示例：

```json
{
  "provider": "mock",
  "model": "mock-t2v",
  "capabilitiesVersion": "v1",
  "prompt": "雨夜咖啡店的单镜头画面",
  "spec": {
    "durationSeconds": 5,
    "resolution": "720p",
    "aspectRatio": "16:9"
  },
  "sourceRefs": [{"artifactId": "source-id", "version": 1}]
}
```

- VideoSpec 不硬编码供应商时长/分辨率；数值必须有限且为正。校验的是整个组合，不能把不同组合中的合法单项拼成未经支持的规格。
- VideoRequest 的提示词有界且非空，只清理首尾空白；sourceRefs 可为空，提供时必须包含确切版本，不接受重复引用。
- `validate_video_request` 检查服务端选定的能力、所属项目和真实产物版本。跨项目来源与不存在来源均返回 `SOURCE_NOT_FOUND`；版本不存在返回 `SOURCE_VERSION_NOT_FOUND`。
- 模型不能通过参数传入 projectId、runId、operationKey、mode、API Key 或 baseUrl。provider/model 名称仍须匹配服务端注册表，不构成任意供应商调用权限。
- B1 在创建 Job 的同一 Store 事务中复核来源，避免校验与登记之间的竞态。旧版文本产物历史保持不变，因此来源 ID/版本能稳定定位原内容。

请求指纹是确定性 JSON 的 SHA-256：包括 provider、model、能力版本、清理首尾空白后的提示词、规格和有序来源；按对象键排序，拒绝 NaN/Infinity。来源顺序和提示词内部空白保留，不宣称语义等价去重。

请求指纹用于同 Run 的单次生成槽位。既有 `FileStore.operation_fingerprint` 仍负责原工具调用的参数冲突检测，其编码算法不变；两种指纹不能互相替代。

B1 的 `JobService.generate(request, context=...)` 返回已记账的 `ok/data` 或 `ok/error`。B2 的上下文执行器直接返回它，避免二次包装；保留原始 JSON 参数用于原调用指纹。不同调用 ID 的同一规范化请求复用原 Job，不同请求返回 `JOB_ALREADY_EXISTS`，错误消息携带已有 jobId。登记结果是调用当时的状态，实时状态需用新的 `job_get` 调用读取。模型输入仅接受已公布的 camelCase JSON 字段，不能借 Python 字段名传入隐藏控制参数。

## 3. 供应商提交、查询和结果

```python
class VideoProviderAdapter(Protocol):
    def capabilities(self) -> VideoCapabilities: ...
    async def submit(self, request: VideoRequest, operation_key: str) -> ProviderTaskHandle: ...
    async def query(self, task_id: str) -> ProviderTaskSnapshot: ...
```

`ProviderTaskHandle` 必须有上游 ID，可附初始快照；附带快照必须属于同一个 ID，因此可表达立即完成的提交。`ProviderTaskSnapshot` 只表示已确认的 queued/running/succeeded/failed，生成失败必须携带 `stage=generate` 的错误。取消是独立的 `CancellableProvider` 协议；能力标记不能替代实际实现。

| 供应商调用结果 | 统一表达 | Worker 动作（B1 已实现） |
|---|---|---|
| 提交已确认受理 | ProviderTaskHandle | 保存原上游 ID，后续只 query |
| 明确未受理 | ProviderCallError，stage=submit，submission_outcome=not_accepted | Job failed，保留提交错误，不创建替代任务 |
| 受理结果不确定 | ProviderCallError，stage=submit，submission_outcome=unknown | Job unknown，停止自动 submit |
| 未归类的提交异常、超时或进程丢失 | 不得推定未受理 | 保守归为 unknown；重启不增加提交尝试 |
| 查询中断 | ProviderCallError，stage=query，不允许 submission_outcome | 保留原生成状态，仅按查询策略重试 |
| 已确认生成失败 | ProviderTaskSnapshot failed，stage=generate | Job failed，记录原上游 ID |

供应商错误先在适配器边界转换为有界的 code/message，不将原始 HTTP 正文、请求头或凭证写入模型结果、事件或状态。M1-B 还没有真实适配器，错误类型本身不能代替适配器脱敏实现。

JobResult 只允许 `simulated=true`、`mediaAvailable=false`、空的 artifactRefs。结果的请求指纹、规格和 sourceRefs 必须与 Job 请求匹配。M1-C 再增加真实媒体结果和下载阶段，不能用此模拟结果判据宣布真实交付成功。

## 4. Job 的持久约束

Job 保存 `id`、`context`、`operationKey`、revision、request/指纹、完整能力快照、状态、原上游 ID、提交/查询计数、查询策略、UTC 时间和结果/错误。项目、会话、创建 Run 与模型步位于服务端 `context` 中；operationKey 必须等于该上下文计算出的原调用键。

```text
pending_submit → submitting → queued → running → succeeded / failed
                       ├── running / succeeded / failed
                       └── unknown
```

- pending_submit 的 submitAttempts 为 0；进入 submitting 之前持久记录第一次尝试，之后最多为 1。submitting/unknown 尚无已确认的上游 ID；queued/running/succeeded 必须有原 ID。
- 成功必须同时有匹配的 JobResult；失败必须有正确阶段的错误。unknown 表示提交不确定，不能写成 generation failure。
- `job_transition_allowed` 拒绝 running 回退到 queued/submitting，以及 unknown/终态自动重提；允许同状态更新查询元信息。B1 在 Store 事务中比较旧状态、revision、累计次数和不可变字段，并检查每个 Run 只有一个 Job、原登记 Operation 同时存在。供应商查询偶尔回报 queued 时，不回退本地已经确认的 running。
- 正常每 2 秒查询。提交/单次查询超时初始均为 15 秒；查询错误后的重试间隔为 1/2/4 秒，三次重试仍失败则暂停。参数通过已保存的 PollingPolicy 决定，不在 Worker 中重新生成预算。
- queryAttempts 累计，consecutiveQueryErrors 表示当前失败窗口；retrying 的计数处于窗口内，paused 表示初次查询及重试均已失败。恢复查询由用户发起，清零失败窗口并继续累计总查询数，不改变提交次数或上游 ID。
- queryState=retrying/paused 时，Job 仍为最近一次确认的 queued/running。只有 polling/retrying 有 nextPollAt，暂停与终态不自动安排下一次查询。
- B1 增加可缺省的 `queryStartedAt`。发起 query 前先累计 queryAttempts、保存开始时间，并将 nextPollAt 设为本次超时截止。进程在查询中退出后，这次已记账的查询消费一个失败窗口；重试时间锚定原截止，不因再次重启而重置。正常响应、异常和 Worker 停止都会清除此字段。

这些类型约束、持久队列、查询调用、原子去重及 Worker start/stop 已由 B1 实现，Agent 等待协调已由 B3 接入；JobWorker 应用生命周期待 B4。Mock 的 `mock-video.json` 与 `state.json` 分别原子提交；Worker 只使用 capabilities/submit/query，不能读取私有账本来消除 unknown。

## 5. 通用等待与恢复指针

ToolExecutionContext 由执行端构造，包含 projectId/sessionId/runId/modelStep/toolCallId，operationKey 沿用 `runId:modelStep:toolCallId`。等待只保存 `resource.kind/id`，Harness 无需导入具体视频供应商。

B2 新建 WaitBinding 同时保存 `operationFingerprint`，覆盖原工具名和原 JSON 参数。最终 Operation 尚不存在时，原键也不能改为另一个本地或 MCP 操作。该字段为 B0 实验数据保留可缺省兼容；生产四工具总是填写。上下文执行器在重放前核对项目、会话、Run、调用键与 Store，领域写操作再次检查 Run 的只读状态。

| WaitBinding 状态 | 必需事实 |
|---|---|
| preparing | 原调用、资源、开始/截止时间已经登记；暂不允许自动交付 |
| armed | 已核对 SQLite 中断，checkpointId 和 interruptId 同时保存 |
| ready | armed 的基础上有稳定的成功/失败工具结果 |
| claimed | 领取恢复权，保存 claimedModelSteps，用于识别之后是否已经开始模型尝试 |
| delivered | SQLite 已包含原工具结果，记录对应 deliveredCheckpointId |
| stopped | autoResume=false；即使保存了结果也不能通过普通唤醒交付 |

`revision` 用于状态变更比较；`generation` 用于拒绝已失效的等待指针。B3 显式重新等待时由服务端管理代次，并先复用已提交的原 Operation，不能覆盖其结果或重复加工具额度。

B3 增加可缺省的 `batchIndex` 与 `timeoutError`。原等待停止且尚无稳定结果时，显式恢复增加 generation 并重新保存截止时间；有稳定结果/Operation 时保留原指针和结果，兼容 SQLite 已记录的恢复值。模型另发新的 await 调用仍独立占用一次工具额度。

恢复入口只传指针，不接受调用方声称的生成结果：

```json
{"waitId": "wait-id", "generation": 1}
```

`ResumeToken` 拒绝额外 result 字段。先根据服务端持久记录验证指针和状态，再传 `Command(resume={interruptId: token})`；图从 WaitBinding 取最终结果。ToolSuccess/ToolFailure 保持原 `ok/data` 或 `ok/error` 外形，严格要求 JSON 布尔值和可持久 JSON 数据。

## 6. 图执行中的三个已验证要求

1. **挂起必须越过普通错误处理。** GraphInterrupt 继承 Exception，不能在工具执行器内部调用 interrupt 后再经过当前 `except Exception`。工具先返回 DeferredToolResult；通用图节点在工具异常捕获范围之外调用 interrupt。B2/B3 接入时保持这一边界。
2. **中断优先于终态判断。** 当前验证版本中，同一节点第二次中断可以出现 `snapshot.next == ()`，但 `snapshot.tasks[].interrupts` 仍非空。必须先检查中断，只有没有待处理中断、没有后续节点且最终状态有效，才能补交终态。现有 execution v1 不产生这类中断，保持原路径。
3. **同批重放保持中断顺序。** 已经建立等待的调用在节点重放时仍经过原 interrupt 位置，哪怕它的本地 Operation 已完成。随后复用原结果；不能因为前一个 Job 已完成而跳过其 interrupt，导致第二个等待取到前一个恢复指针。未曾建立等待且资源已经完成的调用可以直接返回结果。

B0 离线实验仍独立保留；以上行为已由 B3 的生产 AgentRunner 回归验证。B3 还覆盖 SQLite 同时保留旧 interrupt 和已完成节点 pending writes 的情况：只有原 waitId/generation 的 `wait_deliveries`、ToolMessage 和 Operation 相互匹配，才将旧 interrupt 视为已解决。没有有效终态时用原图补交已保存节点结果，不能仅凭 `next == ()` 完成 Run。

### B2 的执行边界

`await_job` 在同一个 Store 锁范围内选择即时结果或 preparing 记录。未建立等待且 Job 已成功时保存成功 Operation；失败、提交不确定、查询暂停分别返回 `JOB_FAILED`、`JOB_SUBMISSION_UNKNOWN`、`JOB_QUERY_PAUSED`。待完成时仅保存绑定、原调用指纹与固定 10 分钟截止时间，不保存虚假的成功 Operation。已建立绑定的原调用始终重放同一延迟标记，即使 Job 或 Operation 已结束。

execution v1 仍在普通工具异常捕获之外识别延迟标记，以 `EXTERNAL_WAIT_UNAVAILABLE`、`resumable=false` 结束；B3 扫描忽略这些已结束 Run 的 preparing 记录。execution v2 则在同步中断后保存 waiting_external，后续由协调器续接原调用。

### B3 的生产边界

`WaitCoordinator.run_once()` 扫描持久记录，`start()/stop()` 管理本地循环，通知只加速扫描。ApplicationService 已管理协调器并将事件归属原 Run；JobWorker 仍需显式运行。配置、Key、原工具/能力、最新会话、检查点和剩余预算均在自动执行前检查；失败原因保存在 `waitResumeError`，已经准备好的领域结果不丢失。

领取后发生过新的模型尝试时，自动恢复返回 `EXPLICIT_RESUME_REQUIRED`，不重发请求；普通 failed/cancelled Run 不被后台继续。已完整落盘的图终态可直接补齐会话。活动时间在图挂起时停止；外部等待用 `externalWaitStartedAt` 和累计 `externalWaitSeconds` 结算。无变化的扫描不更新 Run，也不消费模型、工具或活动时间预算。

用户停止先在 JSON 事务中关闭自动继续、保存 cancelled 与可见的终止工具消息，再取消当前图。Job 不变。正常服务退出只停止协调循环；等待准备和已领取但尚未开始模型尝试的中断保留自动意图。Run 停止后若已有更新请求，显式恢复仍拒绝覆盖新会话。

## 7. 提交顺序与启动补偿（B1/B3 已实施）

| 阶段 | 先持久保存 | 重启后的处理 |
|---|---|---|
| 登记生成 | Job + video_generate 的成功 Operation 同一 JSON 事务 | 重放返回原 jobId，不重新登记 |
| 提交意图 | submitting + submitAttempts=1；随后在事务外 submit | 无已确认 ID 归 unknown，不能猜测未发送 |
| 上游受理/完成 | 原 ID、标准化状态；成功时同时保存结果描述 | 继续 query 原 ID，或直接提供已保存结果 |
| 查询意图 | queryAttempts 累计、queryStartedAt、当前请求的超时截止 | 保留原 ID；中断查询占用原失败窗口，再按保存的策略重试或暂停 |
| 准备等待 | 工具先保存 preparing 与原调用；Runner 再保存 batchIndex、Run 意图并结算活动时间 | 两次 JSON 提交之间退出也保留原等待名额；核对原 tools 检查点后重建中断，复用已完成操作，不先调用模型 |
| 等待就绪 | SQLite 同步中断后再把绑定设为 armed | 若中断存在但 JSON 未更新，核对原调用后补 arm；再检查已经完成的 Job |
| 结果就绪 | armed → ready，保存真实最终工具结果 | 不依赖内存事件；扫描即可发现可交付结果 |
| 领取恢复 | 比较 revision、Run 最新性/状态、autoResume、配置和预算，记录 claimedModelSteps | 未开始新模型调用可补交；已发生模型尝试则走原显式恢复规则 |
| 回填结果 | 原 Operation 保存稳定结果，再向图写原 toolCallId 的 ToolMessage | 结果已提交、图未提交时复用同一结果，不再做副作用 |
| 节点结果写入 | SQLite pending writes 可能先保存已完成 tools 节点，旧 interrupt 尚在 | 核对交付代次、原结果与工具消息；通过原图提交缓存的节点结果，不重新执行工具 |
| 完成交付 | 图确认原工具结果后保存 delivered | 只有交付标记缺失时补标记；重复唤醒不重开图 |

等待截止时间在创建时固定，默认 10 分钟。已 ready 的结果保持稳定；仍未准备好结果的等待超过截止时间后交付 `JOB_WAIT_TIMEOUT`，Job 继续跟踪。unknown、查询暂停和生成失败分别交付对应错误，由后续模型根据事实答复；这些工具错误不自行重提 Job。等待、停止、恢复领取和模型请求丢失均由 B3 故障测试独立验证。

## 8. schema v1 → v2 迁移协议（B1）

B1 已实现的根结构为原 projects/sessions/artifacts/runs/operations，加 `jobs: {}` 和 `waits: {}`，schemaVersion 为 2。v2 Run 增加 waiting_external、videoMode、activeWaitId、externalWaitSeconds 等可缺省字段；缺失视频字段的旧 Run 按 off 解释。

实施顺序：取得原实例锁 → 依据 v1 模型及原消息解析器验证 → 同目录原子保存 `state-v1-<UUID>.json` 原始字节快照 → 深拷贝并只增加 v2 根字段 → 校验 v2 → 沿用启动恢复并原子替换 state.json。迁移失败、损坏数据或未知版本保留原文件并报告错误，不自动重建空库。已经是 v2 的目录不再生成迁移快照。

迁移不重新计算 Operation 指纹，不重排其原参数含义，不重写原 contextSignature、执行版本、产物历史或用量。默认字段可以在读取时解释；避免为迁移而把整份旧数据重新转成另一种消息/SDK 序列化格式。

SQLite 检查点保持在原文件中，不做盲目版本升级。备份或回退应用必须在进程退出后处理完整数据目录；单独还原 state.json 无法保证与 SQLite 一致。仍不自动抢占遗留实例锁。

## 9. execution v1 的兼容门槛

新增等待图使用单独的 execution v2 路径。`CHECKPOINT_VERSION` 仍为 1；读取旧 Run 时通过工具注册表的历史版本视图选择原规则、描述及配置，保留模型、工具、Skills 和上下文校验。

B2 为新 Run 保存 `videoMode` 和 `toolFeatures`。off 没有新能力签名项；缺失字段的旧 Run 解释为 off/空配置。启动为 mock 时，应用/CLI 和 Runner 恢复入口为旧 Run 移除视频工具与附加规则，两个旧签名保持原值。mock Run 必须匹配保存的完整能力、工具 Schema、规则及其版本；关闭模式、缺少适配器或改变配置均在新模型调用前拒绝。只读过滤保留能力配置和缓存绕过策略，不把视频身份混入 MCP 的 identities。

`tests/test_b0_compatibility.py` 固定了在 `a03f249` 上、模型名 `b0-legacy-fixture`、原项目工具集合、无额外 Skill 的两个配置指纹，并实际保存后关闭 Store，再恢复同一个 Run：

| contextVersion | executionVersion | 原 contextSignature |
|---|---|---|
| 1 | 1 | `ca3d165c443da216c26394eb3d95d5400fc9633636e05ef90fd008388d41d3c7` |
| 2 | 1 | `349273b9b922fca6637f83651e760dad4303343352ade27da2b30ca74ddcc7ae` |

测试固定历史指纹，不从当前提示词重新推导期望值。B1 将同一旧执行检查点与 v1 JSON 状态一起重新打开，实际经过 schema v2 迁移后继续完成原 Run，保留原产物、Operation 和累计预算；后续接入仍需满足此门槛。

B3 另从原提交 `41be04a` 的源码捕获四个 B2 mock 指纹，覆盖 context v1/v2 与普通/只读模式，固定在 `tests/test_wait_compatibility.py`。当前默认 v2 Runner 可恢复同一 v1 Run，保持原指纹、原 Operation 和累计预算；B2 已结束的等待不自动复活。
