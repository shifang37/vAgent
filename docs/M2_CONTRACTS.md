# M2 契约、迁移与生成护栏

冻结日期：**2026-10-16，D0**。实现基线：M1 完成提交 `cc3628e`（master）。官方证据见 [VIDEO_API_NOTES.md](./VIDEO_API_NOTES.md) 第 9 节；范围见 [M2_PLAN.md](./M2_PLAN.md)。

**本文冻结 M2 的数据对象、迁移、能力表、费用护栏、确认与等待语义。D0 不实现、不启用 live、不升级当前存储、不调用收费模型：本次视频 submit 次数为 0，未查询账号任务，未上传或生成任何素材。** “必须”表示 D1–D4 的实现与验收要求；旧版行为以 [M1-C 契约](./M1C_CONTRACTS.md)、[M1-B 契约](./M1B_CONTRACTS.md) 及当前兼容测试为准。任何新协议都不得通过改写旧指纹或旧签名让恢复检查通过。

M1-C 已冻结语义继续有效且不被改写：单次提交、按原 task ID 查询、完整媒体落盘才 succeeded、来源允许集与下载策略、错误码与脱敏边界、`supportsIdempotentSubmit=false`、`supportsResultUrlRefresh=false`、`supportsCancel=false`。M2 只在其上扩展。

## 1. 版本冻结

| 维度 | 冻结规则 |
|---|---|
| `schemaVersion` | M2 新建/迁移为 **4**；1/2/3 的只读校验器与逐字节备份流程保留 |
| Job `contractVersion` | 历史 mock 仍为 1，M1-C live 仍为 2；**M2 live 新 Job 为 3**（引入 `shotRef` 与多模式请求）。按精确整数分派校验器，缺省只按历史解释 |
| Run `executionVersion` | 保留 1/2；**集合等待使用 3**（`WAIT_EXECUTION_VERSION=3`）。不改写 1/2 的校验器与恢复路径 |
| Run `contextVersion` | 保留历史 1/2；M2 新 Run 仍使用 **2**（`CONTEXT_VERSION` 不变） |
| `CHECKPOINT_VERSION` | 保持 **1**；集合等待作为新执行图版本接入，不改动检查点载荷格式 |
| `toolFeatures.video.toolsVersion/rulesVersion` | mock/legacy 的 1/1、2/2 与 M1-C live 的 3/3 字节语义不变；**M2 live 为 4/4** |
| `capabilitiesVersion` | M1-C 的 **`wan27-t2v-beijing-v1`** 继续对历史 Job 有效；**M2 为 `wan27-shot-beijing-v2`** |
| `adapterVersion` / `endpointProfile` | M1-C `wan-http-v1` 保留；**M2 为 `wan-http-v2`** / `beijing-workspace-v1`（端点形态与 M1-C 相同，仅协议体新增 `media` 与模式字段） |
| `priceVersion` | t2v 沿用 **`wan27-beijing-720p-20261010`**；**i2v/首尾帧为 `wan27-i2v-beijing-720p-20261016`** |
| `sourcePolicyVersion` | 沿用 **`wan-result-hosts-v2`**；真实样本若出现新主机，在 D4 追加 v3，不改写 v2，不泛放行 |
| `validationVersion` | 沿用 **`mp4-avc-v1`**；素材库不复制、不重编码既有媒体 |
| 素材库 `assetFormatVersion` | **`asset-image-v1`**（导入校验与元数据口径） |

`contracts.py` 保持严格 camelCase JSON 契约与版本分派；域对象模型（`Storyboard`/`Shot`/`Asset`）不进入通用 Agent 图。`video/jobs.py` 负责同事务登记与不变量；`video/providers/wan.py` 负责固定协议与错误映射；`worker.py` 只处理提交/查询；媒体下载与文件提交沿用 M1-C 模块。M2 的领域读写工具与 M1-A 文本工具并列注册，不修改文本工具的字节签名。

## 2. 数据对象

新增两个顶层集合 `storyboards`、`assets`；`jobs` 与 `media` 只新增只读链接字段，不改变既有生命周期与状态机。

| 对象 | 字段（camelCase） |
|---|---|
| `Storyboard` | `id`、`projectId`、`title`、`sourceRefs`（文本产物版本引用，最多 16）、`currentVersion`（int ≥1）、`versions`（有序，只增不改）、`revision`、`createdAt` |
| `StoryboardVersion` | `version`、`shots`（有序快照，1..64）、`createdAt` |
| `Shot`（快照内） | 服务端生成、跨版本稳定的 `id`；`order`（1..N，连续）；`description`（1..2000 字符）；`framing`（枚举，见下）；`cameraMovement`（可选，≤200 字符）；`durationSeconds`（int）；`mode`（`t2v`/`i2v`/`flf2v`）；`referenceAssetIds`（`i2v`/`flf2v` 必填，1 或 2 个，顺序语义见第 4 节）；`extendSource`（M2 恒为 null，见第 4 节） |
| `ShotVersion` | `id`、`shotId`、`storyboardVersion`、`jobId`、请求与估算快照、`mediaId`（交付后写入，否则 null）、`createdAt` |
| `Asset` | `id`（canonical UUID）、`projectId`、`kind="image"`（预留 `video`）、`relativePath`（`assets/<projectId>/<assetId>.<ext>`）、`mimeType`（`image/png`/`image/jpeg`/`image/webp`）、`sizeBytes`、`sha256`、`dimensions`（`width`/`height`，int ≥1）、`createdAt`、`source="user_import"`、`assetFormatVersion` |
| Job 扩展（`contractVersion=3`） | 新增只读链接 `shotRef{storyboardId,storyboardVersion,shotId,shotVersionId}`，创建时固定，之后不可变；其余字段与状态机沿用 M1-C `JobV2` |
| `CostRecord` 扩展 | 新增逐镜头估算明细与额度快照：每次调用返回 `shots[]`（`shotId`、`jobId`、`estimate`）与 `limits{maxJobCost,maxNewJobsPerRun,maxRunCost,usedNewJobs,usedAmount,remainingAmount}`；`actual` 仍默认 `unknown` |

固定语义：

- 版本只增不改：修改分镜产生新的 `StoryboardVersion` 与新的 `ShotVersion`，旧版本、旧 Job、旧媒体一律不动。
- 镜头 `id` 由服务端生成并在所有版本中稳定；“重做第 3 个镜头”定位该 `shotId`，不依赖聊天中的文字顺序。
- `framing` 枚举冻结为 `{wide, medium, close, extreme_close, over_shoulder, aerial}`；`cameraMovement` 为可选自由文本，不参与能力校验。
- `durationSeconds` 只允许能力表已核实的值（第 4 节）；`mode` 未核实的能力不得出现在请求中。
- `referenceAssetIds` 引用素材库 `Asset`：允许引用**同一数据目录内其他项目**的素材（素材库目的所在）；引用不存在、已越界或非图像的资产一律拒绝。`sourceRefs` 的来源 artifact 仍限定当前项目。
- 素材路径由服务端 ID 构造，沿用媒体目录的符号链接与 reparse point 保护；`Asset` 不进入 `media` 索引，生成视频仍在 `media` 索引，素材库在读取侧合并展示。

## 3. 迁移 v3 → v4

- **只新增 `storyboards`、`assets` 两个顶层集合（初始 `{}`）**，`schemaVersion` 置 4；历史对象不重编码、不补默认字段、不改写任何指纹。
- 沿用 M1-C 流程：迁移前逐字节备份、失败保留原状态、未知版本拒绝打开、`instance.lock` 单写保护。
- 固定 v3 基线夹具与 manifest 在 D0 生成并提交（`tests/fixtures/m2/`），记录每个快照的 `rawSha256`、`expectedV4CanonicalSha256` 与 `preservedCollectionsSha256`；夹具为**合成域迁移用**，不含可恢复的 SQLite 检查点证据。
- 旧 Run 按其原 `tools/rules/execution/context` 版本与签名恢复；新 Run 使用 M2 工具与规则版本 4/4 及能力版本 `wan27-shot-beijing-v2`。历史 Job/媒体/播放器/只读/缓存语义不变。

## 4. 生成模式与能力表 v2（`wan27-shot-beijing-v2`）

保留 M1-C 冻结条目：`wan2.7-t2v-2026-06-12`，北京，5 秒/720P/16:9，单任务估算 3.00 元。M2 新增下列**已核实**条目；未核实的模式不出现。

| 模式 | 本地标识 | 模型 | 规格 | 估算 |
|---|---|---|---|---|
| 文生视频 | `t2v` | `wan2.7-t2v-2026-06-12` | 北京、2–15 整数秒（M2 仍默认 5）、720P、16:9 | 0.60 元/秒 |
| 首帧图生视频 | `i2v` | `wan2.7-i2v-2026-04-25` | 北京、2–15 整数秒、720P、**画幅由输入图决定** | 0.60 元/秒 |
| 首尾帧生视频 | `flf2v` | `wan2.7-i2v-2026-04-25` | 同上，`referenceAssetIds` 依次为 `first_frame`、`last_frame` | 0.60 元/秒 |

- **视频续写（`first_clip`）在 M2 裁剪**：官方仅接受 URL（公网或 `oss://` 临时 URL），不接受 Base64；把本地媒体送到上游需要 [上传文件获取临时 URL][D4] 通道。该通道官方标注“请勿用于生产环境”、上传接口位于已弃用的旧域名 `dashscope.aliyuncs.com`，与本项目“只冻结业务空间专属域名、不私搭公网暴露、不引入第三方图床”的边界冲突。故 M2 不实现镜头延长，能力表不出现该条目，`extendSource` 恒为 null。重新纳入的条件：上游提供与专属域名一致、且非“勿用于生产环境”的文件通道。
- **图片提供方式为 Base64 data URL**：官方文档明确 `media[].url` 接受 `data:{MIME};base64,{data}`（图片专用）。本项目采用该方式，避免公网暴露、第三方图床与上传通道依赖；音频/视频类输入只接受 URL，故 M2 不开放参考音频。
- 图片输入约束（与上游一致，导入与提交前均校验）：`PNG/JPEG/WebP`；单边像素 `[240, 8000]`；宽高比 `1:8～8:1`；单图 ≤20 MB；PNG 不支持透明通道（含 alpha 的 PNG 在提交前本地拒绝并给出明确错误）。BMP 上游支持但本项目不纳入。
- 画幅：`i2v`/`flf2v` 不发送 `ratio`；输出画幅尽量与输入图一致，边长为 16 的倍数，故能力表**不固定 16:9**，也不承诺精确比例。`t2v` 继续固定 16:9。
- 请求体（合成示例，D0 未发送）：

```json
{
  "model": "wan2.7-i2v-2026-04-25",
  "input": {
    "prompt": "生成单镜头视频。\n雨夜咖啡店，镜头缓慢推近窗边的一杯热咖啡。",
    "media": [
      {"type": "first_frame", "url": "data:image/png;base64,<bounded>"},
      {"type": "last_frame",  "url": "data:image/png;base64,<bounded>"}
    ]
  },
  "parameters": {"resolution": "720P", "duration": 5, "prompt_extend": false, "watermark": true, "seed": 0}
}
```

- `media` 每个 `type` 最多出现一次；组合只允许 `first_frame`、`first_frame+last_frame`。不发送 `driving_audio`、`first_clip`。`resolution` 显式 `720P`、`prompt_extend=false`、`watermark=true`、`seed=0` 与 M1-C 口径一致。
- 提交与查询沿用 M1-C 的固定路径、请求头、状态映射与 24 小时窗口；`usage` 新增读取 `input_video_duration`（i2v 为 0）。成功缺 `video_url`、字段类型错误、task ID 不符一律为协议错误，不产生本地 succeeded。
- 越界参数（未核实模式、不合法时长、非法素材）返回明确错误，**不自动替换模型、规格或模式**。
- 限流沿用文档值（每模型 5 RPS、5 个处理中任务，按主账号维度）；登记串行，**同时在途真实 Job ≤ min(4, 5−1)=4**。查询沿用 15 秒与持久退避。

## 5. 费用与确认

- 三重上限（冻结值）：每 Job 估算上限 `videoMaxJobCost = 3.00` CNY（既有）；每 Run 新真实 Job 数 `videoMaxNewJobsPerRun = 4`；每 Run 累计估算上限 `videoMaxRunCost = 12.00` CNY。设置页展示并允许下调；提交前校验剩余额度。
- 确认流程（冻结）：沿用 M1-C 的**单阶段登记 + 强制估算回显**。`shot_generate` 结果固定包含逐镜头估算、合计估算、已用与剩余额度；服务端强制数量与金额上限。验收断言“回复包含合计估算，且提交数不超过用户确认的镜头集合”。
- **批量登记原子性**：一次调用先校验全部镜头（来源、模式、素材存在性、上限、去重），任一失败整体不登记；通过后在同一次 Store 事务登记全部 Job 与 `ShotVersion`。
- **去重按 `ShotVersion` 作用域**：同一 `ShotVersion` 的重复登记复用原 Job（指纹 = 规范化请求 + `shotRef`）；工具重放与重复用户请求不重复登记。
- **重做**仅由用户显式发起（对话、Web 重做按钮、`vagent shots redo SHOT_ID`）：产生新 `ShotVersion` 与一个新 Job，**不受去重复用限制**；仅目标镜头受影响，其他镜头的产物、版本、Job 全不动。失败与 `unknown` 沿用 M1-C：不自动重提、不自动换参数重试。
- 真实样本额度（D4）：首个真实多镜头验收为 **2 镜头加 1 次单镜头重做，submit ≤ 3、累计估算 ≤ 9.00 CNY**，须显式批准后执行。i2v/首尾帧默认不做真实样本，另行批准。零费用预检沿用 C3.2 方式（DeepSeek `GET /models`、对不存在任务 ID 的一次查询）。

## 6. 集合等待（工具 v4）

- `await_job`（`toolsVersion=4`）接受非空 `jobIds` 列表（1..16，去重、稳定排序），登记一次等待。
- 全部 Job 到达可交付终态（成功、失败、`unknown`、查询暂停、下载失败）或达到等待期限（沿用 **600 秒**）后，返回**逐镜头结果与失败摘要**；单元素列表等价 M1-C 的 `jobId` 行为（返回体与 M1-C 同形）。
- 超时返回已完成部分并允许显式重等；已交付结果不被后续事件改写。等待本身不增加模型调用，也不改变 Job 状态。
- 绑定：`ExternalResourceRef(kind="jobset", id=<服务端生成的集合 ID>)`，成员 `jobIds` 存于 `WaitBindingV3`；`executionVersion=3`。已交付的单 Job 绑定（`kind="job"`，v2）继续按原语义恢复。
- 推荐对话流程：分镜草案 Run（读项目、保存分镜、回显估算后结束）与生成 Run（批量登记、集合等待、逐镜头汇总）分离，确认点位于两个 Run 之间，沿用 8 步/12 工具/180 秒预算；同 Run 内完成 N=4 的登记加等待加汇总也在预算内。
- 停止、退出、强退与重启复用 B1–C2 持久机制；新增“等待集合部分交付后重启”“重做期间原 Job 与媒体不受影响”“停止不唤醒”用例。

## 7. 素材库

- **导入**：Web 上传（multipart、CSRF、大小与类型上限 ≤20 MiB、PNG/JPEG/WebP）与 `vagent assets import PATH`。魔数校验、尺寸读取、SHA-256；`(projectId, sha256)` 命中则复用既有 `assetId`，不重复落盘。文件路径由服务端 ID 构造（`assets/<projectId>/<assetId>.<ext>`），沿用媒体目录的符号链接与 reparse point 保护。
- **读取**：素材元数据与内容接口复用媒体 HTTP 语义与同源边界；素材库页列表与预览。不存在返回 404，已删除/不可读返回 410 语义与媒体接口一致。
- **引用**：镜头以 `assetId` 引用；允许复用同一数据目录内其他项目的素材；来源 `sourceRefs` 仍限定当前项目；不存在或越界资产拒绝。工具只读列出素材，不开放任意文件系统路径，上传只经本地受信入口。
- **删除**：M2 不提供素材删除（避免引用悬空），留待后续阶段。
- 已有媒体不迁移、不重编码；素材库不改写任何历史媒体记录。

## 8. 工具与 Schema（D0 冻结命名）

| 工具 | 关键参数 | 行为 |
|---|---|---|
| `storyboard_read` | `storyboardId?`（省略取当前项目最新） | 只读返回分镜、当前版本、有序镜头与各镜头版本摘要；跨项目拒绝 |
| `storyboard_save` | `storyboardId?`、`expectedVersion?`、`title`、`shots[]`、`sourceRefs[]` | 校验 `sourceRefs`（文本产物版本、限当前项目）与 `shots` 上限；产生新 `StoryboardVersion`（保留原版）；返回 `storyboardId`/`version`/`shotIds` |
| `shot_generate` | `storyboardId`、`storyboardVersion`、`shotIds[]`、`confirm=true` | 批量登记，原子性见第 5 节；返回逐镜头 `jobId`、估算与额度快照 |
| `video_generate` | 同 M1-C | 保留单镜头兼容路径（N=1），语义不变 |
| `await_job` | `jobIds[]` | 集合等待，见第 6 节 |
| `job_get` | `jobId` | 同 M1-C，只读本地事实 |
| 素材读取 | `assetId?`/项目范围 | 只读列出素材元数据，不含路径与私有 URL |

mock/legacy 变体保留原工具名与描述；M2 live 变体的 `toolsVersion/rulesVersion=4/4`。规则文本明确：一次调用可登记多个镜头 Job，受三重上限约束；重做只影响目标镜头；不得索取或展示 Key、签名 URL 或素材私有路径。

## 9. CLI / Web 与用量统计

- CLI：`vagent storyboard show`（只读）、`vagent shots redo SHOT_ID`（幂等：`clientRequestId + revision`）、`vagent assets list|import`、`vagent usage --project`。
- Web：分镜侧栏（顺序、状态、缩略图、版本数）、镜头卡（参数、来源版本、版本列表、播放、重做、下载）、批量生成确认对话框（逐镜头与合计估算）、**序列连播**（客户端按顺序播放各镜头 MP4，非服务端拼接）、素材库页、用量面板、按镜头分组的 Job 卡；revision/SSE 合并与播放器节点保留语义沿用 C2。
- 用量统计：读取侧聚合既有记录（Jobs 计数与状态、估算与 `actualStatus`、模型 Token 与覆盖率，按项目与按日）；不引入计费真相，**实际账单仍未知的保持 `unknown`**；不做外部调用。

## 10. D0 决策冻结（对应 M2_PLAN 第 6 节）

1. **拼接归属**：归 M3。M2 交付逐镜头本地 MP4 与客户端序列连播，不做服务端拼接。
2. **确认流程形态**：单阶段登记 + 上限强制 + 估算回显。
3. **真实样本预算**：2 镜头 + 1 次重做，submit ≤ 3、累计估算 ≤ 9.00 CNY，显式批准后执行（D4）。
4. **默认上限数值**：每 Run 4 个新真实 Job、累计 12.00 CNY、每 Job 3.00 CNY，可在设置中下调。
5. **新模式的真实样本**：i2v、首尾帧默认不做真实调用；首帧与首尾帧纳入能力表但仅离线/受控 HTTP 验证。
6. **素材删除**：M2 不提供。

## 11. 证据与未确认项

已核实（文档正文，见 [VIDEO_API_NOTES.md](./VIDEO_API_NOTES.md) 第 9 节）：`wan2.7-i2v-2026-04-25` 的 `media` 组合与字段、图片 Base64 方式、素材约束、时长/画幅规则、提交与查询路径、限流（5 RPS / 5 并发，主账号维度）、北京 720P 0.60 元/秒与 50 秒免费额度。

未确认（保留为 D4 验证项，不作肯定承诺）：当前账号对 i2v 的模型授权与 IP 权限；i2v 真实结果主机与重定向（沿用 `wan-result-hosts-v2` 允许集，若不符则在 D4 追加策略版本）；`first_frame` 真实输出画幅与 audio 表现；实际账单（保持 `unknown`）；免费额度是否抵扣。

[D4]: https://help.aliyun.com/zh/model-studio/get-temporary-file-url
