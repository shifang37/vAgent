# M1-C 契约、迁移与媒体提交协议

冻结日期：**2026-10-10，C0**。实现基线：B5 `2cc764d`，规划提交 `b940c82`。官方证据及未确认项见 [VIDEO_API_NOTES.md](./VIDEO_API_NOTES.md)，范围见 [M1C_PLAN.md](./M1C_PLAN.md)。

**本文是 C1/C2 的实现规格，C0 不启用 live，不升级当前存储，不调用收费模型。** “必须”表示后续实现与验收的要求；旧版行为以 [M1-B 契约](./M1B_CONTRACTS.md)及当前兼容测试为准。新协议不得通过改写旧指纹使恢复检查通过。

**C1 实现状态补记（2026-10-10）**：C1.1–C1.3 已完成，schema v3、Wan 协议和配置/工具接入通过离线验证，云端成功停在 `downloading`。验证结果见 [C1 验收](./M1C_C1_ACCEPTANCE.md)。下方 C0 记录、固定夹具和原始哈希保持不变；下载与真实验收仍按 C2/C3 实施。

## 1. 版本与模块边界

| 维度 | 冻结规则 |
|---|---|
| `schemaVersion` | C1 新建/迁移为 3；保留 1、2 的只读校验器和原始备份流程 |
| Job `contractVersion` | 旧及新 Mock Job 继续为 **1**；新 live Job 为 **2**；按精确整数分派校验器，缺省只按历史 v1 解释 |
| Run `executionVersion` | 保留 1/2；live 使用已有等待图 **2**，不全局升级 `CHECKPOINT_VERSION=1` |
| Run `contextVersion` | 保留历史 1/2；新 Run 使用当前 2，独立于视频版本 |
| `toolFeatures.video.toolsVersion/rulesVersion` | 旧 Mock 的 1/1、2/2 字节语义不变；live 为 **3/3** |
| `capabilitiesVersion` | 首期 live 为 **`wan27-t2v-beijing-v1`** |
| `adapterVersion` / `endpointProfile` | **`wan-http-v1`** / **`beijing-workspace-v1`** |
| `priceVersion` | **`wan27-beijing-720p-20261010`** |
| `sourcePolicyVersion` / `validationVersion` | **`wan-result-hosts-v1`** / **`mp4-avc-v1`** |

`contracts.py` 提供严格 camelCase JSON 契约、不可变请求及版本分派；`jobs.py` 负责同事务登记与不变量；`providers/wan.py` 负责固定协议、错误映射；`worker.py` 只处理提交/查询；`media.py`、`media_worker.py` 处理下载/文件提交。`storage.py` 管理 schema v3、媒体索引和恢复记录。供应商参数、媒体 HTTP 和费用规则不进入通用 Agent 图。

持久对象沿用 `extra=forbid`、有限数值、UTC 时间与严格布尔值；嵌套不可变集合使用 tuple。解析外部供应商 JSON 时先提取已知字段再构造契约，允许忽略未知非关键字段，不将原始响应整体持久化。

## 2. 配置与新旧 Run

| 环境变量 | `config.yml` / 配置写接口字段 | 默认 / 限制 |
|---|---|---|
| `VAGENT_VIDEO_MODE` | `videoMode` | `off`；仅 off/mock/live |
| `VAGENT_DASHSCOPE_KEY` | `videoApiKey` | 无；repr、读接口、日志均隐藏；删除使用 `clearVideoApiKey` |
| `VAGENT_DASHSCOPE_WORKSPACE_ID` | `videoWorkspaceId` | 无；live 必需，取控制台 API Host 中的空间标签 |
| `VAGENT_VIDEO_PROVIDER` | `videoProvider` | `wan`；首期 live 只允许此值 |
| `VAGENT_VIDEO_MODEL` | `videoModel` | `wan2.7-t2v-2026-06-12`；只允许已冻结模型 |
| `VAGENT_VIDEO_REGION` | `videoRegion` | `cn-beijing`；首期只允许此值 |
| `VAGENT_VIDEO_MAX_JOB_COST` | `videoMaxJobCost` | 十进制字符串 `3.00` CNY；有限且非负，0 表示拒绝有费用的新提交 |

优先级为默认值 < 本地配置 < 环境变量/显式启动配置，沿用现有 `CONFIG_OVERRIDE`、本地原子写入和来源展示。模式与真实供应商分开：off 不开放视频工具，mock 继续使用 provider=mock/model=mock-t2v，不能被 live 配置改名或收费。

空间 ID 校验为一个 1–63 字符的小写 ASCII DNS 标签（字母/数字/内部连字符，不含点、斜杠、端口、空白）；后端拼接唯一 HTTPS 主机。旧 Job 保存自己的地域、空间和端点版本；凭证按该范围匹配，Key 本身不写 Job、Run、Operation 或检查点。轮换同一范围的 Key 不改变任务身份；只配置另一个空间时不得拿其凭证盲查旧任务。

读取配置返回 `videoApiKeyConfigured`、`videoPermissionStatus=unverified`、各字段来源与 `restartRequired`，不返回 Key 或签名地址。保存和启动不发送测试生成；现有 `/api/config/validate` 的 DeepSeek 验证不扩展成视频生成。C3 证据只证明指定账号范围在当时可用，不是永久权限证明。

视频配置保存后重启生效。运行中的请求及队列使用已保存身份，不能改用新默认值；金额上限在提交前再次检查。应用可在缺视频配置时启动并提供设置/文本功能；live 的 `video_generate` 明确报缺配置，不能静默转 mock。

新 Run 只看到启动模式的能力。恢复旧 Run 时，按原 mode、tools/rules/execution/context 版本构建独立工具视图并验证原签名；不能让“为恢复旧 Job 装配的适配器”出现在新 Run 能力表中。缺原适配器或配置时保留 Job/等待结果并报告 `VIDEO_PROVIDER_UNAVAILABLE` 或 `RESUME_CONFIG_CHANGED`，不修改工具身份。视频工具可见时继续跳过 Redis 回答缓存；只读模式继续禁止新 Job。恢复不刷新 8 步/12 工具/180 秒活动时间及既有等待预算。

## 3. 能力、冻结请求、去重与费用

四工具名称保持 `video_capabilities`、`video_generate`、`job_get`、`await_job`。查询和等待只读本地事实，不直接发起供应商 HTTP。生成工具只登记，不同步等待云端或下载。

### 公开能力与参数

live 能力只有一个条目：provider=wan，model 为上述精确版本，mode=live，region=cn-beijing，`inputTypes=["text"]`，`specs=[{durationSeconds:5,resolution:"720p",aspectRatio:"16:9"}]`。公开 `maxPromptCharacters=4991`、`shotMode=single`、`audioMode=auto`、`promptExtend=false`、`watermark=true`、`seed=0`，以及固定的价格版本和 3.00 CNY 估算。`supportsCancel`、`supportsIdempotentSubmit`、`supportsResultUrlRefresh` 均为 false。运行时配置是否齐全单独展示，不把动态状态写进能力签名。

`video_generate` 仍只接受 `provider/model/capabilitiesVersion/prompt/spec/sourceRefs`；所有字段使用 camelCase，拒绝隐藏别名和多余字段。live 的 `durationSeconds` 为整数 5，不能接受布尔值、字符串或其他组合。来源最多 16 个，保留有序的 artifactId/确切 version；同事务复核所属项目和版本，不接受重复来源。来源可为空，但不能捏造或使用“最新版”代替冻结版本。

模型不能传入 Key、workspaceId、region、baseUrl、端点/适配器版本、operationKey、预算、seed、音频 URL 或下载位置。原来的 `VIDEO_MODEL_UNAVAILABLE`、`VIDEO_CAPABILITIES_CHANGED`、`VIDEO_UNSUPPORTED_SPEC`、`SOURCE_NOT_FOUND`、`SOURCE_VERSION_NOT_FOUND` 继续有效。

### 私有 `VideoRequestV2`

| 字段 | 值 / 不变量 |
|---|---|
| `provider/model/capabilitiesVersion/prompt/spec/sourceRefs` | 校验后的公开参数；prompt 仅 trim 首尾，不折叠内部空白 |
| `region/workspaceId/endpointProfile/adapterVersion` | 登记时从服务端选定，全部冻结 |
| `providerPrompt` | 精确等于 `生成单镜头视频。\n` + prompt；总长 ≤5000，超限 `VIDEO_PROMPT_TOO_LONG` |
| `parameters` | 精确 HTTP 对象：`resolution=720P, ratio=16:9, duration=5, prompt_extend=false, watermark=true, seed=0` |

请求体只能由上述冻结对象生成，不再次应用供应商默认参数。`negative_prompt`、`audio_url`、`size`、`shot_type` 不发送。单镜头前缀是模型控制意图，不构成输出一定单镜头的保证。

规范化 JSON 算法为 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)` 后 UTF-8 SHA-256：

- `intentFingerprint`：对 `{"intentVersion":2,"arguments":<规范化公开六字段>}` 求哈希；缺省 sourceRefs 规范化为 `[]`。
- `requestFingerprint`：对 `{"requestVersion":2,"request":<完整 VideoRequestV2>}` 求哈希，包含实际端点身份、prompt 与完整 HTTP 参数。
- 原 Operation 指纹仍对**原工具名及原始 JSON 参数**使用既有算法，不能替换为以上任一个。

查找顺序必须先复用原 Operation/WaitBinding，再检查同 Run 已登记 Job。不同调用 ID 但相同 intent 返回原 Job，沿用其请求和报价；不同 intent 返回 `JOB_ALREADY_EXISTS` 及原 jobId。只有没有原 Job 时才解析当前配置与价格。重复操作不得因今天的默认值改变而创建另一任务。

### `CostRecord`

| 字段 | 首期内容 |
|---|---|
| `estimate`（不可变） | `currency=CNY, unit=output_second, unitPrice="0.60", quantity="5", amount="3.00", priceVersion, checkedAt="2026-10-10", sourceUrl, maxJobCost="3.00"`；最后一项保存登记时实际配置值 |
| `providerUsage` | 初始 null；只接收原 task 的合法 duration/inputVideoDuration/outputVideoDuration/videoCount/resolution/aspectRatio，缺失项为 null |
| `actual` | 初始 `{status:"unknown",currency:"CNY",amount:null,source:null}`；只有可追溯实际账单才允许 status=reported 和金额，HTTP usage 不能代替账单 |

金额使用 Decimal，持久化为规范十进制字符串，拒绝 NaN/Infinity、负数和指数表示；首期 CNY 金额保留两位小数。免费额度不参与预算扣减，DeepSeek 费用独立记录。

缺 Key、空间、能力、有效报价或超预算时在登记前返回 `VIDEO_KEY_MISSING`、`VIDEO_WORKSPACE_REQUIRED`、`VIDEO_CONFIG_INVALID`、`VIDEO_PRICE_UNKNOWN`、`VIDEO_COST_LIMIT`，submit=0。对尚未提交的旧 Job，提交前再核对完整请求、当前价格版本与当前金额上限：价格变更为 `VIDEO_PRICE_CHANGED`，超额为 `VIDEO_COST_LIMIT`；保留原报价和 `pending_submit`，保存 `runtimeBlock`，不得提高或重新计算原额度后自动发送。原报价和当前上限都必须满足。已提交任务的查询/下载不重新收取预算，也不能因后来调低上限而丢弃结果。

## 4. 供应商结果、错误与轮询

### 适配器协议与结果分层

沿用 `capabilities()`、`submit(request, operation_key)`、`query(task_id)`；live 使用版本化 handle/snapshot，不能把成功 URL 填进旧 `JobResult`。`operation_key` 不发送给供应商。客户端独立于 DeepSeek 管理，关闭时释放连接；鉴权请求不跟随 3xx，transport retries=0，响应正文上限 1 MiB。

`ProviderTaskHandleV2` 保存 `taskId`、可空 `requestId`、可选同 ID 的 snapshot。2xx 响应没有顶层拒绝 code 且存在合法 task ID 时先保存 handle，附带快照不合法则留待原 ID 查询，不能退回“未提交”；同时携带顶层拒绝与受理字段的矛盾响应按下文 unknown 处理，该 ID 尚不能视为已确认。缺少状态时本地 queued 表示已受理待查询，`lastProviderStatus=null`，页面不声称已知云端排队状态。新 task ID 限制为单个 1–256 字符 ASCII 字母/数字/下划线/连字符，作为路径段编码；不接受 URL。

`ProviderTaskSnapshotV2` 保存 `taskId/requestId/status`，其中 status 为 queued/running/succeeded/failed；succeeded 必须携带 `ProviderOutput`，failed 必须携带 stage=generate 的错误。`ProviderOutput` 是**私有上游结果**：

| 字段 | 约束 |
|---|---|
| `taskId`、`receivedAt` | 原 ID、本地 UTC 接收时间 |
| `videoUrl` | 有界（≤8192 字符）HTTPS 字符串；保存时校验语法，不向公开投影传递 |
| `urlExpiresAt`、`expirySource` | 仅可解析的签名 Expires 绝对时间可填入，source=signature；其余为 null/unknown，不伪造新 24 小时期限 |
| `providerTimes` | 有界原始时间串，可空；不参与 UTC 调度 |
| `usage` | 上述合法时长/规格，值缺失保留未知；没有真实费用字段 |

URL 主机或媒体校验失败属于下载阶段，保留已经确认的云端成功事实；SUCCEEDED 缺 URL、task ID 不符、字段类型错误则属于查询协议错误，不能登记可交付结果。终态重复响应不得分配第二个 mediaId。

### 提交错误白名单

`JobErrorV2` 为 `stage=submit|query|generate|download`、有界大写 code 和固定中文 message。私有诊断只增加已校验的 `httpStatus/providerCode/requestId`；禁止原始响应、URL、头部或异常 repr。`ProviderCallError` 的提交分支必须有 `submissionOutcome=not_accepted|unknown`，查询分支不能设置它。

仅完整 JSON、**无任何 task ID/受理数据且 HTTP 与 code 同时命中下表**，才能判定 not_accepted；这不是“所有 4xx 都安全”的规则。

| HTTP + 顶层 provider code（大小写精确匹配） | 本地 code |
|---|---|
| 400 + `InvalidParameter` / `InvalidInputLength` / `BadRequest.EmptyModel` | `PROVIDER_INVALID_REQUEST` |
| 401 + `InvalidApiKey` / `invalid_api_key` | `PROVIDER_AUTH_FAILED` |
| 403 + `AccessDenied` / `access_denied` / `AccessDenied.Unpurchased` / `Model.AccessDenied` / `Workspace.AccessDenied` / `Endpoint.AccessDenied` | `PROVIDER_ACCESS_DENIED` |
| 404 + `ModelNotFound` / `model_not_found` / `WorkSpaceNotFound` | `PROVIDER_MODEL_UNAVAILABLE` |
| 400 + `Arrearage`，或 403 + `AllocationQuota.FreeTierOnly`，或 429 + `BudgetLimitExceeded` | `PROVIDER_BILLING_BLOCKED` |
| 429 + `Throttling` / `Throttling.RateQuota` / `Throttling.BurstRate` / `Throttling.AllocationQuota` / `Throttling.Concurrency` / `LimitRequests` / `limit_requests` | `PROVIDER_RATE_LIMITED` |

即使 not_accepted 也不自动重试提交。408、5xx、3xx、未知错误、HTTP/code 不匹配、非 JSON、缺 ID 的成功响应、受理与拒绝相矛盾的响应均为 `SUBMISSION_UNKNOWN`。所有网络/取消/协议异常和提交中崩溃同样保守处理；不能把 `DataInspectionFailed`、`InternalError.Algo.*` 等可能涉及处理阶段的错误泛化为未受理。上游安全检查失败仍可在原 task 的 FAILED 快照中明确归 `PROVIDER_GENERATION_FAILED`，不自动修改内容重提。

### 查询状态与策略

S1 的 PENDING/RUNNING/SUCCEEDED/FAILED 按核实记录映射；CANCELED → stage=generate、`PROVIDER_CANCELED`；UNKNOWN → stage=query、`PROVIDER_TASK_UNAVAILABLE`，立即暂停，保留原 ID/状态。其他未知枚举、缺字段或响应 ID 不一致为 `QUERY_PROTOCOL_ERROR`，不能假定失败或成功。queued 不覆盖已经确认的 running。

live 的 `PollingPolicy` 固定正常 15 秒、submit/query 单次总超时各 30 秒、连接 10 秒，查询失败后间隔 **15/30/60 秒**（首次失败后最多 3 次重试）。Mock 的旧 policy 原样保留。queryAttempts、consecutiveQueryErrors、queryStartedAt、nextPollAt 均先持久化再发请求；重启按原截止时间消费被中断的一次，不清零额度。单 Worker 串行调度，调用之间至少 1 秒，避免多个旧 Job 到期时突发请求；不是整个账号的限流保证。

429、短时 5xx、网络故障和协议异常可以有限查询重试；若有合法 Retry-After（秒数或 HTTP 日期），取它与本地退避中更晚的时间并持久保存。不是承诺上游必定返回该头。超过从提交意图起 24 小时的保守查询期限则暂停，不缩短 Retry-After 再请求。

v2 的 `queryPauseReason` 为 `retry_exhausted|configuration|task_unavailable` 或 null；paused 不一律要求耗尽错误窗口。配置/鉴权错误立即暂停，计数不伪增；恢复查询只针对原 ID，通过配置和期限检查后清零失败窗口，保留累计次数。未知状态按原有限窗口耗尽后归 retry_exhausted。已确认云端成功后 queryState=idle，下载不占查询循环。

`runtimeBlock={stage,code,message,blockedAt}` 仅表示当前缺配置、价格变化等执行条件；不覆盖生成事实。pending_submit 被阻塞时 submitAttempts=0；accepted Job 缺配置时保存查询暂停；下载独立处理。后台不反复空转请求或重复发布同一阻塞事件。解除条件后只继续原 Job，新的配置不得改变原身份。

## 5. Job、媒体索引与等待结果

`JobV2` 保留 v1 的 id/context/operationKey/revision/request/requestFingerprint/capabilities/status、提交和查询计数、policy、UTC 时间、error/result；新增以下字段。context 与 operationKey 仍由服务端生成，每 Run 至多一个 Job，所有更新经过同一 Store revision/CAS 校验。

| 新增字段 | 冻结含义 |
|---|---|
| `mode`、`intentFingerprint` | mode 只能 live；去重哈希见第 3 节 |
| `submissionStartedAt`、`queryDeadlineAt` | 提交意图时一起保存；后者为前者+24小时；提交之前均为 null |
| `providerSubmissionRequestId`、`lastQueryRequestId` | 分别保存提交及最近查询追踪 ID，可空 |
| `lastProviderStatus` | 最后一次合法上游枚举，可空；不等同本地 Job.status |
| `queryPauseReason`、`runtimeBlock` | 第 4 节的暂停/配置诊断，初始 null |
| `cost` | 原报价、可取得 usage、实际费用状态 |
| `providerOutput` | 私有上游输出，初始 null；云端成功后保留 |
| `downloadPolicy` | 登记时冻结的资源上限、超时与退避，见第 6 节 |
| `download` | 一个持久 DownloadRecord；云端成功前为 null |
| `mediaAvailability` | 最近核验的 status（available 或 unavailable）、reason、checkedAt；初始 unavailable/not_delivered/null；实际可用性变化增加 Job revision |

合法状态迁移（同状态只能更新允许的进度元数据）：

```text
pending_submit -> submitting -> queued/running -> downloading -> succeeded
                      |              |                |
                      +-> unknown    +-> failed       +-> download_failed
                      +-> failed                          |
                      +-> downloading                     +-> downloading
```

submitting/queued 可直接进入 downloading；本地 running 不回到 queued。unknown/failed 不自动重新提交；succeeded 不回退生成状态。pending_submit 的 submitAttempts=0，其余为 1，终生不增加到 2。

| 状态 / 事实 | 必须满足 |
|---|---|
| pending_submit/submitting/unknown | 无确认 providerTaskId；unknown 必须是 stage=submit 的不确定错误 |
| queued/running | 有原 providerTaskId，无本地 result；queryState 为 polling/retrying/paused |
| failed | stage=submit 时无确认 ID；stage=generate 时有原 ID；无 result |
| downloading/download_failed | 原 ID、ProviderOutput 与稳定 DownloadRecord 均存在；queryState=idle，无 result |
| download_failed | error.stage=download，download.phase=failed，无自动 nextAttemptAt |
| succeeded | 有且只有一个已提交 MediaAsset 和匹配 JobResultV2；无生成/初次交付错误；历史交付事实保持 |

### `MediaAsset` 与 `JobResultV2`

schema v3 仅新增一个顶层 `media` 字典，以 mediaId 为键；下载恢复记录放在 Job 内，不混入文本 Artifact。

`MediaAsset` 字段：`id/projectId/jobId/sourceRefs/relativePath/mimeType/sizeBytes/sha256/createdAt/metadata/validationVersion`。id 为服务端 UUID；relativePath 固定为 `media/<projectId>/<mediaId>.mp4`；mimeType=video/mp4；sizeBytes 为实际正整数；SHA-256 为小写 64 位十六进制。metadata 包含实测的 `width/height/durationSeconds/videoCodec/hasAudio/audioCodec/frameRate`：尺寸为正整数、时长为有限正数、videoCodec=h264、hasAudio 为布尔值、audioCodec 为可空的有界编码名、frameRate 为可空的正整数 numerator/denominator 对象。不能把请求规格冒充实测值。来源与所属项目必须和 Job 一致。

`JobResultV2`：`simulated=false`、`mediaAvailable=true`、`requestFingerprint`、请求 `spec/sourceRefs`、空 `artifactRefs`、恰好一个 `mediaRefs=[{mediaId}]`、有界 summary。该结果只能与 MediaAsset 和 Job.succeeded 在同一次 Store 事务中提交。v1 的模拟 result 完全保持原结构，不添加 mediaRefs 或费用字段。

MediaAsset 的身份、来源、路径、哈希和规格提交后不可修改。成功结果中的 mediaAvailable 表示**交付时**可用；当前可用性必须由文件校验后的视图确定。文件后来丢失/损坏时，GET 媒体或 Job 视图显示不可用，移除播放器，不能继续报告“文件可播放”，也不改写已交付 Operation/ToolMessage。

### 等待与公开投影

live 的 `await_job` 在 queued/running/downloading 继续沿用固定 600 秒等待；生成阶段暂停/失败/unknown 分别返回原 `JOB_QUERY_PAUSED`、`JOB_FAILED`、`JOB_SUBMISSION_UNKNOWN`。初次下载耗尽交付 `JOB_DOWNLOAD_FAILED`；等待截止则 `JOB_WAIT_TIMEOUT`，Job 可继续。只有本地完整文件和索引已提交且当前可用时才提供成功结果。

后续重试下载成功只更新卡片，不复活已 completed/failed/cancelled/stopped 的 Run，也不覆盖旧 await 的已交付结果。新的 await 调用仍占原有工具额度；文件缺失且没有修复执行时返回 `JOB_MEDIA_UNAVAILABLE`，正在修复时可按同一等待规则等待。准备、领取、停止与补交沿用 WaitBinding 原指针，不引入供应商回调直接唤醒模型。

live `job_snapshot` 明确投影：jobId、revision、mode、simulated=false、当前 mediaAvailable、status、lastProviderStatus、queryState/queryPauseReason、公开请求与实际默认参数、来源、cost（已脱敏）、下载阶段/计数、mediaRefs、error、result、时间。CLI/Web 另加 projectId/sessionId/runId/providerTaskId、可重试标志；不返回 workspaceId、磁盘绝对路径、私有 ProviderOutput 或签名 URL。旧 tools/rules 1/2 继续走旧投影，当前 HTTP/页面可使用版本分支显示新字段。

## 6. 下载、原子提交与恢复

### `DownloadRecord` 与策略

| 字段 | 约束 |
|---|---|
| `mediaId`、`relativePath` | 第一次上游成功时一次分配；所有尝试、重启、手动重试都复用 |
| `phase` | pending / writing / prepared / committed / failed |
| `generation`、`attempts`、`windowAttempts` | 初始 1/0/0；attempts 终生累计；windowAttempts 每窗口最多 3 |
| `nextAttemptAt`、`startedAt`、`deadlineAt` | 持久调度/在途意图；每次网络尝试之前计数并保存；无自动执行时 nextAttemptAt=null |
| `prepared` | 初始 null；完整验证后保存 sizeBytes/sha256/metadata/validatedAt，作为重命名后的恢复凭据 |
| `error` | 可空、有界 stage=download 错误；修复历史成功文件时也保存到此处 |
| `repair` | 初次交付 false；已成功文件丢失后的显式取回为 true |
| `sourcePolicyVersion` | 本窗口采用的服务端允许集版本；窗口内固定，不能由工具/HTTP 参数指定主机 |

`downloadPolicy`：`maxBytes=268435456`（256 MiB）、connectTimeoutSeconds=10、readTimeoutSeconds=30、totalTimeoutSeconds=180、retryDelaysSeconds=[5,30]、maxAttemptsPerWindow=3、maxRedirects=3、concurrency=1。总时长覆盖解析、跳转、传输及完成检查；自动重启不新开窗口。

独立串行 MediaWorker 由 ApplicationService 启停和监管；与 JobWorker、WaitCoordinator 各有独立循环和任务异常处理。流式 I/O 不能在 Store 事务锁内执行，不阻塞模型停止/SSE/查询。关闭时取消并等待媒体任务释放响应和文件，再关闭 HTTP 客户端及 Store；Windows/Python 3.11 的取消信号仍需回归。

### 传输和内容校验

1. 只使用原 ProviderOutput.videoUrl。HTTPS、443、精确主机允许集和每次重定向地址均验证；拒绝 userinfo、IP 字面量、内网/环回/链路本地/保留地址及跨策略跳转。解析 DNS 后还需确保连接使用已验证的公网地址，不能仅做一次预查询后让客户端重新解析。允许集更新须有新的官方/真实样本证据和策略版本，显式重试新窗口才能采用。
2. 独立无鉴权客户端，`trust_env=false`、不带 Cookie，手动处理至多 3 次跳转；不转发 API 请求头。请求 Accept-Encoding: identity，拒绝压缩 Content-Encoding。只接受最终 200 的完整媒体；首期不上游 Range 续传。
3. 分块累计**实际写入字节数**，即使无 Content-Length 或长度伪报也执行上限；存在合法 Content-Length 时完成后必须相等。允许 video/mp4 或 application/octet-stream，缺失类型也必须通过完整 MP4 校验；明确 HTML/JSON 等类型立即拒绝。
4. `mp4-avc-v1` 至少校验 ftyp/moov/mdat、盒子长度/嵌套边界（含 64 位 size 与末盒 size=0）、H.264 avc1/avc3 视频轨道、非空样本表、所有视频/音频样本实际落在 mdat 文件范围内、正时长；拒绝外部数据引用、截断或仅有容器头的文件。首期只支持自包含非碎片 MP4，若返回 fMP4 则保留错误，不能将未支持结构当成完成。
5. 实测视频尺寸须为 1280×720；时长与 5 秒相差不超过 0.1 秒（本地容差，不是官方保证），无转码或悄悄修改规格。记录音轨和编码信息，计算完整 SHA-256；容器/样本边界校验不等于已经完成浏览器解码测试，后者由 C3 验收。

网络中断、读取/总超时、HTTP 429/短时 5xx 可在原窗口内重试；完整性长度不符作为传输中断处理。退避和合法 Retry-After 取较晚时间并保存。磁盘满、权限、未授权主机、非法类型、结构/规格错误、大小超限立即停止自动重试。已过可知签名期限为 `MEDIA_URL_EXPIRED`；403 或 404/410 只表示下载不可访问/结果不可用，不能无依据声称一定过期。

错误码冻结：`DOWNLOAD_NETWORK`、`DOWNLOAD_TIMEOUT`、`DOWNLOAD_INCOMPLETE`、`MEDIA_HTTP_ERROR`、`MEDIA_RESULT_UNAVAILABLE`、`MEDIA_URL_EXPIRED`、`MEDIA_SOURCE_UNAPPROVED`、`MEDIA_SOURCE_UNSAFE`、`MEDIA_TOO_LARGE`、`MEDIA_INVALID_TYPE`、`MEDIA_INVALID_MP4`、`MEDIA_SPEC_MISMATCH`、`MEDIA_DISK_FULL`、`MEDIA_PERMISSION_DENIED`、`MEDIA_IO_ERROR`、`MEDIA_FILE_CONFLICT`、`MEDIA_HASH_MISMATCH`。消息使用固定可操作描述，不回显签名或远端错误正文。下载失败不清除 providerOutput/cost/providerTaskId。

本期 `supportsResultUrlRefresh=false`：不轮询尝试延长链接，不增加 submit。网络故障重试仍使用保存的 URL；永久失效时说明原结果已无法取回，保留 Job 与费用未知状态。

### 文件与索引提交顺序

媒体目录和文件名仅由服务端 ID 决定。目录解析及打开时检查真实路径、符号链接/Windows reparse point，禁止逃逸媒体根；临时文件与最终文件同目录，不从 URL 提取文件名。状态与路径不能由客户端直接构造。

| 阶段 | 持久顺序 | 强退后的补偿 |
|---|---|---|
| 登记意图 | 同一事务保存 ProviderOutput、稳定 mediaId/path、phase=pending、Job.downloading | 继续同一个媒体记录；重复 SUCCEEDED 不再分配 ID |
| 开始尝试 | 先增加 attempts/windowAttempts，phase=writing，保存 startedAt/deadlineAt | 本次已消费；剩余窗口内按原截止安排重试，不能重启免费重试 |
| 写入 | 流式写 `<mediaId>.mp4.part`，验证并计算哈希，flush/fsync/关闭 | 无 prepared 记录的 part 不可交付；从头下载，不能拼接不明剩余字节 |
| 准备提交 | 把 size/hash/metadata 写入 DownloadRecord.prepared，phase=prepared | 重启核对 part 或最终文件与 prepared 的哈希/大小，再继续提交；不另发下载 |
| 重命名 | 同目录原子重命名 part → mp4；可用平台上同步目录 | 最终文件完整但索引缺失时，依原 prepared 补提交；不分配新 ID |
| 提交索引 | 一次 Store 事务保存 MediaAsset、JobResultV2、Job.succeeded、phase=committed | 只有完成此步才发布成功事件并允许等待交付 |
| 等待交付 | 沿用原 WaitBinding/Operation/SQLite 提交协议 | 重复扫描、丢失通知、pending writes 均复用原 toolCallId 和结果 |

若已存在最终文件且无对应 prepared/MediaAsset，或哈希不符，返回 `MEDIA_FILE_CONFLICT`/`MEDIA_HASH_MISMATCH` 并保留证据；不覆盖来历不明的文件。part/最终文件均缺失时按原剩余额度重新下载，额度耗尽则 download_failed。文件提交和 JSON 并非跨资源事务，上表补偿保障进程强退；不能承诺所有文件系统在突然断电时的物理持久性。

初次交付失败后手动重试仍复用原 mediaId、来源与原任务；同一个原窗口已在执行时重复请求不增加窗口。只有已停止的失败窗口可经用户显式请求开始 generation+1、windowAttempts=0；attempts 不清零。

已 succeeded 的文件后来丢失可通过同一入口显式修复：Job.status/result 和 MediaAsset 的哈希/来源不变，download.repair=true，单独推进下载阶段；取回的字节必须匹配原 SHA-256，才能恢复可用。保留同一媒体 ID，不将不同内容冒充原文件；无法取回时继续显示当前不可用。

## 7. CLI/Web 交付接口

接口沿用单数据目录、本地 Host/Origin 校验及写请求 CSRF 保护；模型工具按服务端 projectId 限定资源。HTTP 按当前应用数据目录的 mediaId 查索引，再核对 media/project/job/sourceRefs 关系，不接受文件路径参数。这里不增加多用户鉴权语义。

| 接口 | 冻结行为 |
|---|---|
| `GET /api/media/{mediaId}` | 返回 mediaId/projectId/jobId/sourceRefs/mimeType/sizeBytes/sha256/metadata、当前 available/unavailable 与原因、同源 contentUrl；无磁盘绝对路径或远端 URL |
| `GET /api/media/{mediaId}/content` | 完整 200 或单字节区间 206；媒体不存在 404，已索引但文件丢失/损坏 410；失败用固定错误，不自动重新生成/下载 |
| `HEAD /api/media/{mediaId}/content` | 与完整 GET 相同状态/长度/类型/ETag，无正文；按 HTTP 语义忽略 Range |
| `GET .../content?download=1` | 相同字节与 Range 语义，Content-Disposition=attachment；默认 inline，下载名仅为服务端 mediaId.mp4 |
| `POST /api/jobs/{jobId}/retry-download` | body 为 `{clientRequestId,expectedRevision}`；仅初次下载失败或历史文件不可用时可新开恢复窗口；进行中的重试返回当前执行，不另开窗口 |
| `vagent jobs retry-download JOB_ID` | 使用同一应用服务；CLI 生成请求 ID/读取 revision，不另建 Run；`jobs work`/Web 服务推进下载 |

媒体内容响应包括 Content-Type: video/mp4、Accept-Ranges: bytes、强 ETag（SHA-256）、准确 Content-Length、X-Content-Type-Options: nosniff。GET 支持 `bytes=a-b`、`bytes=a-`、`bytes=-n`；末端超过长度时截到末字节，合法但不可满足的区间返回 **416 + Content-Range: bytes */L**，206 返回 **Content-Range: bytes a-b/L** 和区间长度。非法单位/语法及多 Range 请求统一忽略 Range，返回完整 200，首期不生成 multipart。

If-Range 只有匹配当前强 ETag 时才应用 Range；不匹配、弱标记或不支持的日期值返回完整 200，不把旧文件字节拼入新响应。条件/HEAD 的判断先于流式发送，连接断开及时关闭文件。内容由稳定的本地已验证文件句柄提供，不能把 part 当作成品。

下载重试以 `download-retry:<jobId>:<clientRequestId>` 记录 Operation，并原子检查 revision 与下载状态；相同参数重放返回原结果，相同 ID 改参数返回 OPERATION_CONFLICT，陈旧 revision 返回 JOB_REVISION_CONFLICT。新窗口登记返回 202，资源不存在 404，不允许的状态 409（JOB_DOWNLOAD_RETRY_UNAVAILABLE）。同一正在执行的窗口不因不同 clientRequestId 重置计数。手动修复后的下载失败仍使用原 Job，不自动请求模型。

CLI 可以显示受控相对位置 `media/<projectId>/<mediaId>.mp4`；浏览器只使用同源 contentUrl。Job 卡片显示模拟/真实、实际采用规格、估算与实际费用未知状态，以及云端生成、下载、暂停或失败；只在当前 available 时显示播放器。CSP 增加同源 media-src，播放器支持 loadedmetadata、播放、拖动和下载。

启动、Job/媒体读取及打开内容时核验文件当前可用性；发现状态变化时保存 mediaAvailability 并增加 Job revision，再发布 SSE。无变化核验不反复写状态。即使原 Run 已结束，页面仍按 Job revision 合并更新；旧 Mock 卡片和已交付工具结果保持原样。

## 8. schema v1/v2 → v3 迁移

1. 在原单写实例锁内读取 state.json **原始字节**；只接受精确整数 1/2/3。按源版本严格校验结构、Job/Wait、消息及引用，损坏或未知版本报 INVALID_STORE，原文件不重置。
2. 对 1/2 先原子保存 `state-v1-<UUID>.json` 或 `state-v2-<UUID>.json`，内容逐字节相同。备份失败则不替换状态。
3. 内存深拷贝构造 v3：v1 只新增空 jobs/waits/media 并改 schemaVersion；v2 只新增空 media 并改 schemaVersion。原 jobs/waits/runs/operations/artifacts 等对象不经 model_dump 重新编码，不补写历史默认字段，不重算指纹。
4. 使用 v3 混合版本校验器再次检查，再原子替换 state.json。备份或替换出错时释放实例锁、保留原状态和已有备份；再次打开允许重试迁移。已经是 v3 不重复备份。
5. 迁移之后才按原恢复规则处理被中断的 Run/提交/查询及新增下载记录。恢复不是迁移，可以合法更新状态/revision，但不能改变身份或刷新预算；测试必须分开验证这两个阶段。

旧 Run 缺 videoMode 只在读取语义上视为 off，execution/context、toolFeatures、contextSignature、原 Operation JSON、modelSteps/toolCalls/activeSeconds/externalWaitSeconds 均保留。旧 Mock Job 仍由 v1 类型解释，不增加 request 字段、不转成真实结果。schema 升级不修改 `checkpoints.sqlite` 及其相关 WAL/SHM 文件，不打开图来重新生成 checkpointId/interruptId。

继续支持旧 B0 execution v1 + context v1/v2 和 B2 tools/rules v1，以及 B3/B4 的原等待指针、停止/领取/交付与 pending writes 恢复；`RESUME_CONFIG_CHANGED` 不能通过重写 contextSignature 绕过。新 schema 写入后不支持旧版应用直接读取；退回旧程序需在应用退出后恢复**完整数据目录**备份，单份 state 备份不包含完整检查点和媒体。

### 固定迁移夹具

以下文件由上述基线的现有存储/Mock/工具 API 在隔离目录生成，内容全部为合成数据。C0 已用源版本校验器读取；**尚未实现或声称通过 v3 迁移**。

| 夹具 | 覆盖事实 |
|---|---|
| [legacy-v1.json](../tests/fixtures/m1c/legacy-v1.json) | CRLF 原始字节、两版文本产物、旧消息/用量/Operation、缺省视频字段 |
| [legacy-v2-mock.json](../tests/fixtures/m1c/legacy-v2-mock.json) | running 与 succeeded Mock Job、armed 与 delivered Wait、原请求/操作指纹、来源固定在 v1 而文本已到 v2 |
| [manifest.json](../tests/fixtures/m1c/manifest.json) | 源文件 SHA-256、各原集合哈希、Job/Operation 指纹及目标 v3 envelope 的规范 JSON 哈希 |

manifest 的规范 JSON 使用第 3 节同一排序/编码算法。目标 envelope 只执行第 3 步的机械增量；运行态恢复须另外断言，不能更新期望哈希来掩盖身份变化。`.gitattributes` 对 legacy-v1 关闭文本换行转换，保证备份字节断言跨平台有效。

夹具内的 checkpoint/contextSignature 字符串是**领域数据哨兵**，没有随附真实 SQLite，不能用于证明图可恢复。真实检查点兼容继续使用 [test_b0_compatibility.py](../tests/test_b0_compatibility.py)、[test_wait_compatibility.py](../tests/test_wait_compatibility.py)、[test_wait_recovery.py](../tests/test_wait_recovery.py) 的固定历史签名和离线图实验。C1 必须新增 v3 断言，不能替换这些旧证据。

## 9. C1/C2 实现与验收顺序

| 实施步骤 | 必须补齐的验证 |
|---|---|
| C1.1 类型/存储 | v1/v2 固定夹具→v3、逐字节备份、迁移失败、未知版本、混合 Job 版本、原指纹与真实检查点兼容 |
| C1.2 Wan HTTP | 请求精确值、无重试提交、受理 ID 先保存、立即终态、明确拒绝/矛盾响应/超时/断开、UNKNOWN/CANCELED/非法状态、限流及持久查询窗口 |
| C1.3 配置/工具 | 三种模式、只读、缺 Key/空间/价格/额度、原 Run 能力隔离、配置保存零生成、费用快照与公开字段脱敏；云端成功只进入 downloading |
| C2.1 媒体 | 原 URL 有界流下载、重定向/DNS/路径校验、真实有效 MP4 与截断/超限、磁盘故障、每个提交强退点、重复成功/重试、原 mediaId 与一次 submit |
| C2.2 CLI/Web | GET/HEAD/206/416/410、同源播放/拖动/下载、刷新恢复、SSE revision、文件后来丢失及按原哈希修复、Run 停止/已结束后不意外唤醒 |
| C3 | 全量离线/安装/四组 CI 后，以最多一个真实 Job 验证真实模型权限、CDN、媒体编码和浏览器实际播放；费用/失败/未覆盖项目如实记录 |

HTTP 和媒体测试使用受控响应/本地测试服务器及有效小型 MP4，普通 CI 不访问收费服务或公共视频站。C0 只检查文档、夹具与已有兼容基线；完整真实交付依然属于 C3。

## 10. C0 验证记录

2026-10-10 本地验证：

- 两份夹具分别通过当前 DatabaseV1/Database 校验；原始字节、原集合、Job/Operation 指纹及预期 v3 envelope 哈希与 manifest 一致，CRLF 夹具换行保留。
- 两份新增文档及 README/计划的相对链接、章节锚点、JSON 示例和 Markdown 表格已检查；提示词前缀为 9 个码点，5 秒估算为 CNY 3.00。
- 下列已有契约/迁移/检查点兼容回归 **49 passed**。没有增加 live 实现，也没有把目标 envelope 哈希检查算作 v3 迁移通过。

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_storage_migration.py tests/test_b0_compatibility.py tests/test_wait_compatibility.py tests/test_video_contracts.py
```

本次视频 submit=0、真实模型调用=0；账号权限和真实媒体仍待 C3。仓库常规 CI 与打包检查按实际提交查看，C0 不宣称已完成 C3 的媒体安装验收。
