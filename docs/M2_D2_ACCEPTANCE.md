# M2 D2 验收：多镜头生成、重做与费用护栏

完成日期：**2026-10-17**。基线：D1 提交 `2cc86f6`（codex/m2-d1），实现分支 `codex/m2-d1`（D2 延续该分支）。契约与范围见 [M2 契约](./M2_CONTRACTS.md)（含 D2 实现补记）与 [M2 任务规划](./M2_PLAN.md)；官方证据见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 9 节。

**D2 全部验证离线：视频 submit=0，未发起真实视频调用，未使用任何真实凭证或素材；真实多镜头联调仍留待 D4。** 费用相关用例使用受控 HTTP（`httpx.MockTransport`）驱动冻结的万相协议，只有本地合成图片进入请求体。

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| `shot_generate` 批量原子登记（一次调用登记多个镜头 Job） | 完成 |
| 三重上限强制（每 Job 金额、每 Run 新真实 Job 数、每 Run 累计金额）与额度快照 | 完成 |
| 估算回显与显式确认（`confirm=true`），任一失败整批不登记 | 完成 |
| 按 ShotVersion 去重、同请求复用原 Job | 完成 |
| 逐镜头重做（`JobService.redo_shot`：新 ShotVersion + 新 Job，仅目标镜头） | 完成 |
| 集合等待（`await_job` v4，`jobIds[]`、`WaitBindingV3`、部分失败摘要、超时部分结果与重等） | 完成 |
| 部分失败隔离（单镜头失败不影响其他镜头推进与交付） | 完成 |
| `wan.py` 多模式协议（t2v / 首帧 i2v / 首尾帧 flf2v，Base64 参考图、无 `ratio` 图片模式） | 完成 |
| 参考图本地校验（格式、尺寸、大小、宽高比、PNG 透明通道、提交时重新取证） | 完成 |
| live 切换 4/4 与 `wan27-shot-beijing-v2`；`wan-http-v2` | 完成 |
| Job `contractVersion=3`（`shotRef`、逐镜头请求、额度快照） | 完成 |
| 受控 HTTP 故障矩阵与 mock 端到端 N 镜头含一次重做 | 完成 |

不在 D2（按计划进入后续工作包）：素材上传/导入接口与素材库页、用量统计、Web 分镜侧栏与连播、CLI `shots redo`/`assets`/`usage`（D3）；`scripts/evaluate_m2.py`、真实成本受控多镜头联调、wheel smoke 扩展与四组 CI 汇总（D4）。

## 2. 版本与契约落地

- Job 契约：mock 保持 **1**（历史与新 Mock Job 均不改写），M1-C live 保持 **2**，M2 live 新 Job 为 **3**（`JobV3`：`shotRef`、`VideoRequestV3`、`VideoCapabilitiesV3`、`CostRecordV3.limits`）。`parse_job` 按精确整数分派，3 为新增，4 与其它类型仍拒绝。
- 能力表：live 发布 **`wan27-shot-beijing-v2`** 两个条目（`wan2.7-t2v-2026-06-12` 与 `wan2.7-i2v-2026-04-25`）；冻结的 `wan27-t2v-beijing-v1` 仍注册在适配器上，使历史 Job 与新 `video_generate` 兼容路径继续可用，但不出现在当前能力表中。
- 适配器：`WanAdapter` 增加 `wan-http-v2` 请求体（`input.media`）与 `reference_loader` 注入；`adapterVersion=wan-http-v2`，端点、路径、请求头、状态映射与 24 小时窗口沿用 M1-C。
- 工具：mock 与 live 均为 **4/4**；live 工具集新增 `storyboard_read`/`storyboard_save`/`shot_generate`，`await_job` 改为集合 schema。`for_execution_version(1/2/3)` 的冻结变体同时恢复旧**工具定义**（`await_job` 的 `jobId` schema 与旧描述），因此 pre-M2 Run 的签名与工具集逐字节不变。
- 等待：新增 `WaitBindingV3`（`kind="jobset"`、成员 `jobIds` 稳定排序、2–16 个）与 `parse_wait` 分派；`waits` 集合在 schema v4 内接受两种绑定，schema 版本不变。
- 设置：新增 `videoMaxNewJobsPerRun`（默认 4，1–4 可下调）与 `videoMaxRunCost`（默认 `12.00`）；`videoMaxJobCost` 沿用 `3.00`。
- `schemaVersion` 仍为 **4**，`CHECKPOINT_VERSION` 仍为 **1**，`contextVersion` 仍为 **2**；本次不新增顶层集合。

## 3. 批量登记与费用护栏

- 登记原子性：`shot_generate`/`redo_shot` 在**一次 Store 事务**内完成逐镜头请求推导、参考图取证、上限校验、Job 与 `ShotVersion` 写入；任一镜头失败整批不写入（`tests/test_m2_generation.py` 的确认/未知镜头/过期版本用例断言无 Job、无 ShotVersion、无 upstream 调用）。
- 三重上限：每 Job 估算 > `videoMaxJobCost` → `VIDEO_COST_LIMIT`；本 Run 新真实 Job 数超 `min(videoMaxNewJobsPerRun, 4)` → `VIDEO_RUN_JOB_LIMIT`；本 Run 累计估算超 `videoMaxRunCost` → `VIDEO_RUN_COST_LIMIT`。三者都在登记前校验，拒绝时 submit=0。
- 额度快照：每次调用返回逐镜头估算、合计估算与 `limits{maxJobCost,maxNewJobsPerRun,maxRunCost,usedNewJobs,usedAmount,remainingAmount}`，并冻结进每个 Job 的 `cost.limits`。已用量与剩余量**每次登记都从持久化 Job 重算**，重启不刷新（重启后同 Run 的第二次批量仍按既有用量拒绝）。
- Store 层不变量：`video` 工具版本 < 4 的 Run 仍只允许一个 Job；M2 Run 的新真实 Job 数上限为 4（常量 `LIVE_JOBS_PER_RUN`），超限或非 M2 Run 多 Job 一律 `JOB_CONFLICT`。
- 金额上限的默认值使 5 秒以外的镜头自然被单 Job 上限拒绝（6 秒 = 3.60 > 3.00）；上调 `videoMaxJobCost` 后 2–15 秒整数时长均可用。

## 4. 集合等待与部分失败

- `await_job` v4 接受非空 `jobIds`（1–16，去重、稳定排序）；**单元素列表走 M1-C 原路径**：`kind="job"` 绑定与同形返回体（`test_single_element_wait_uses_the_frozen_job_binding`）。
- 多元素：一次登记 `WaitBindingV3`；全部成员到达可交付终态（成功/失败/unknown/下载失败/查询暂停/运行期阻断）后返回逐镜头结果与失败摘要；等待不增加模型调用、不改变 Job 状态（用例断言 upstream 提交数不变）。
- 部分失败：单镜头失败不影响其他镜头推进与交付；摘要按 `ok` 计数并保留 `JOB_FAILED` 等具体错误（`test_live_set_wait_reports_per_shot_results_and_partial_failures`）。
- 超时：到达 600 秒等待期限时返回**已完成部分**（未完成项带 `JOB_NOT_READY`），摘要标注已到期；随后可用新的调用标识显式重等（`test_live_set_wait_timeout_returns_partial_results_and_can_be_repeated`）。运行时只在解析器未产生结果时套用已保存的 `JOB_WAIT_TIMEOUT`。
- 重启与停止：集合绑定跨重启逐字节保留并可解析；停止 Run 只把绑定置为 `stopped`（`autoResume=false`），两个 Job 继续独立推进（`test_set_wait_survives_restart_and_stop_keeps_the_jobs`）。
- 运行器级：三镜头 mock 轨迹从保存分镜到批量登记、挂起、Worker 交付、协调器自动续接直至最终回复（`test_mock_runner_generates_three_shots_and_waits_as_one_set`）。

## 5. 重做语义

- `JobService.redo_shot`：只由显式调用触发；为当前分镜版本的目标镜头追加**一个新的 `ShotVersion`** 并登记**一个新 Job**；其他镜头的版本、Job、产物一律不动（用例逐项断言 ShotVersion 前缀不变、其他镜头仍只有原版本）。
- 幂等：同一调用上下文（`operation_key`）重复触发返回原结果，不再登记；`expectedRevision` 过期返回 `VERSION_CONFLICT`。
- 对话重做：Agent 通过 `storyboard_save` 产生新分镜版本 + `shot_generate` 只提交目标镜头完成；CLI/Web（D3）走 `redo_shot`，需为该次用户操作提供一个 Run 上下文。
- 去重与重做的边界：`shot_generate` 按 `(storyboardId, storyboardVersion, shotId)` + 冻结请求指纹复用最近一次匹配的 ShotVersion 的 Job；请求不同（含分镜改版）则新建。

## 6. 图片输入与多模式协议

- 请求体按模式精确生成：t2v 发送 `ratio=16:9` 且无 `media`；i2v/flf2v 发送 `media[].type = first_frame[, last_frame]` 的 Base64 data URL 且**不发送 `ratio`**；`resolution=720P`、`prompt_extend=false`、`watermark=true`、`seed=0` 三种模式一致（参数化用例逐模式比对请求体）。
- 参考图取证：登记时校验（存在、图像、记录与请求一致、格式、尺寸、大小、宽高比、PNG 透明通道），提交时通过注入的 `reference_loader` **重新读取并复核哈希**——文件在登记后被改动会在提交前以 `ASSET_HASH_MISMATCH` 拒绝，且不发出任何 HTTP 请求。
- 越界参数一律明确拒绝，不替换模型或规格：未核实模式 `VIDEO_UNSUPPORTED_MODE`、时长不在规格表 `VIDEO_UNSUPPORTED_SPEC`、含 alpha 的 PNG `IMAGE_ALPHA_UNSUPPORTED`、边或比例越界、`IMAGE_FORMAT_UNSUPPORTED`、`IMAGE_TOO_LARGE`。
- 镜头延长仍裁剪：`extendSource` 恒为 null，请求里不出现 `first_clip`。

## 7. 门禁与回归

- 离线全量：**868 passed / 2 skipped**（D1 基线 839；新增 29 项：`tests/test_m2_generation.py` 19 项、`tests/test_wan_shot_provider.py` 10 项）。
- 新增覆盖：同一调用标识下的**并发**批量登记与并发重做各一例（四个线程只登记一次，验证 Store 的按操作重放）；以及 **mock 端到端 N 镜头含一次重做**的 Agent 级轨迹（两镜头生成后，第二个 Run 只改并只重做其中一个镜头，另一镜头的 ShotVersion、Job 与描述不动，全程无供应商调用）。
- `ruff check` 与 `ruff format --check` 全库通过。
- 兼容证据：pre-M2 mock/live 签名与工具集恢复用例（`tests/test_m2_compatibility.py`）继续通过；`tests/test_video_jobs.py`、`tests/test_video_tools.py`、`tests/test_live_jobs.py`、`tests/test_live_application.py`、`tests/test_m1c_evaluation.py`、`tests/test_wait_runtime.py`、`tests/test_wait_compatibility.py` 全部通过。
- 四组 CI：见第 9 节。

## 8. 失败与偏差记录

开发过程中由新用例与既有回归暴露并修正的缺陷（均由用例固定在提交中）：

1. **Store 的“原调用成功结果”校验只认单 Job**：批量登记后第二个 Job 触发 `JOB_OPERATION_MISSING`。改为 `_operation_registers`：结果体含 `jobId` 或 `shots[].jobId` 任一即通过。
2. **`_batch_limits` 对 `VideoPrice` 直接求和**：`TypeError` 被 `store.operation` 掩码为 `TOOL_ERROR`。改为按 `Decimal(estimate.amount)` 求和；同时说明：服务层异常必须保持 `AppError`，否则会以通用错误码出现在模型可见结果里。
3. **重做的 journal key 与 Job 的 `operationKey` 不一致**：触发同一校验失败。改为以调用上下文（`operation_key`）为日志键，重复触发即重放原结果。
4. **集合等待解析器返回裸摘要**：`WaitService.resolve` 需要 `ToolResult` 信封，缺失时 `ValidationError` 被上层掩码成 `EXECUTION_ERROR`。改为返回 `{"ok": True, "data": …}`。
5. **`stored_request_fingerprint` 用了错误的算法**：mock（`VideoRequest`）指纹必须与其 `fingerprint()` 一致，否则同一请求不会被复用而会重复登记。
6. **`WaitService.claim` 与 `commit_result` 仍用 `WaitBinding.model_validate`**：集合绑定（含 `jobIds`）被 `extra=forbid` 拒绝，自动续接失败。改用 `parse_wait`。
7. **t2v 镜头误走图片分支**：`_shot_media` 在无参考帧的模式下仍要求 `image_input`，导致文本镜头报 `VIDEO_UNSUPPORTED_MODE`。
8. **M2 提交前价格校验只认同一报价**：`JobV3` 的报价按自身能力条目的单价 × 时长冻结，因此提交前改为与该冻结报价比较（费率变化会以适配器/能力不可用呈现）。
9. **测试侧适配**：`_adapters`/`_capabilities` 私有字典的键由 `(provider, model)` 变为三元组（新增公开 `default_adapter`）；`await_job` 的冻结调用形状（`jobId`）保留在 pre-M2 用例中，当前工具用例改用 `jobIds`；live 应用级用例的 `toolsVersion/rulesVersion` 断言更新为 4/4；`evaluate_m1b.py`/`evaluate_m1c.py` 的 `await_job` 调用与评分断言同步为集合形状。

10. **安装态 wheel smoke 仍用旧形状**：`scripts/wheel_smoke.py` 的 mock 等待演示仍以 `jobId` 调用 `await_job`，live 演示又从 `video_capabilities` 返回的第一个条目构造 `video_generate`（M2 下该条目已是 shot 条目，`video_generate` 无法消费）。三处调用改为 `jobIds` 形状，live 演示显式钉住冻结的单镜头版本与规格。首轮 `1e28242` 与次轮 `247964e` 的四组 CI 因此都在安装态 smoke 上失败（`smoke_video_tools` 的等待断言），修复后自 `93784f4` 起全部通过；本次先在独立环境（`tmp/m2-d1-wheel-env`）安装自建轮子跑通 smoke 再推送，避免继续用 CI 试错。

## 9. 四组 CI

四组 CI（ubuntu/windows × 3.11/3.12，`.github/workflows/ci.yml`「Agent checks」）在最终代码提交 **`34cf1aa`** 上全部通过（check runs `117304190739` / `117304190715` / `117304190659` / `117304190547`，结论均为 success）：该轮 ruff 与 node 检查通过，套件 ubuntu 3.11 为 **867 passed / 3 skipped**、windows 3.11 为 **869 passed / 1 skipped**，安装态 `scripts/wheel_smoke.py` 输出 PASS（含持久等待、原 Run 重启续接、M1-B 五例与 M1-C 七例）。平台间的跳过数差异（本机 Windows 2、ubuntu 3、CI windows 1）与 D1 同因，仍为既有的符号链接权限类跳过，不是 D2 引入。

中间轮次：首轮 `1e28242` 与次轮 `247964e` 均在安装态 wheel smoke 上失败（原因与修复见第 8 节第 10 项）；`93784f4` 起四组全绿（`117303646485` / `117303646434` / `117303646417` / `117303646346`），该提交与 `34cf1aa` 的差别仅为新增的并发用例与端到端重做用例。本次文档提交不改动代码。

## 10. 留给 D3 的入口

素材上传/导入接口与素材库页（写入侧）、用量统计读取侧聚合、Web 分镜侧栏/镜头卡/批量确认对话框/序列连播、按镜头分组的 Job 卡；CLI `vagent storyboard show`、`vagent shots redo SHOT_ID`（幂等：clientRequestId 映射到一次调用上下文）、`vagent assets list|import`、`vagent usage --project`；D3 需要为 CLI/Web 的显式重做创建一个 Run 上下文（D2 只提供 `redo_shot` 服务路径与语义保证）。
