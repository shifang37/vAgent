# M2 D3 验收：素材库、用量统计与 CLI/Web 分镜工作台

完成日期：**2026-10-17**。基线：D2 提交 `2f53a69`（codex/m2-d1；D3 延续该分支）。契约与范围见 [M2 契约](./M2_CONTRACTS.md)（含 D3 实现补记）与 [M2 任务规划](./M2_PLAN.md)；官方接口证据见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 9 节。

**D3 全部验证离线：视频 submit=0，未发起真实视频或模型调用，未使用任何真实凭证或素材。** 浏览器验证使用受控上游（`httpx.MockTransport` 驱动已冻结的万相协议）、本地合成的参考图与测试 MP4；素材导入、批量登记、本地交付、连播与重做都走真实应用代码路径。

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| 素材库写入侧：受信本地导入（Web multipart、`vagent assets import` 路径）、魔数与发布边界校验、按 `(projectId, sha256)` 去重、缺失/损坏文件的恢复 | 完成 |
| 素材读取接口与只读工具：`GET/POST /api/assets`、`GET/HEAD /api/assets/{id}/content`（复用媒体 Range 交付）、`asset_read` | 完成 |
| 分镜读取接口与统一投影：会话快照 `storyboard`、`GET /api/storyboards[...]`、逐镜头版本与由 Job 派生的媒体链接 | 完成 |
| 估算预览与登记入口：`GET /api/storyboards/{id}/estimate`、`POST /api/sessions/{id}/shots/generate`、`POST /api/sessions/{id}/shots/{shotId}/redo` | 完成 |
| 客户端 Run：CLI/Web 显式动作各自的持久 Run、请求 ID 幂等、与对话 Run 的 latest 和额度隔离 | 完成 |
| 用量统计读取侧聚合：`GET /api/usage`、`vagent usage --project [PROJECT]` | 完成 |
| CLI：`storyboard show`、`shots redo`、`assets list|import`、`usage --project` | 完成 |
| Web：分镜面板、镜头卡、批量确认对话框、序列连播、素材库页、用量面板、按镜头分组的 Job 卡 | 完成 |
| 三个 M2 交付缺陷的修复：`JobResultV3`、`VideoRequestV3.public_arguments`、面板未跟随 SSE Job 事件 | 完成 |
| 前端状态测试与受控浏览器分镜/素材库验证 | 完成 |

不在 D3（按计划进入 D4）：`scripts/evaluate_m2.py` 与真实成本受控多镜头联调、wheel smoke 扩展与四组 CI 汇总、`docs/M2_ACCEPTANCE.md`、README 与计划的最终同步。

## 2. 版本与契约落地

- `schemaVersion` 仍为 **4**、`executionVersion` 仍为 **3**、`contextVersion` 仍为 **2**、`CHECKPOINT_VERSION` 仍为 **1**；M2 视图工具/规则仍为 **4/4**、能力版本 `wan27-shot-beijing-v2`、`adapterVersion=wan-http-v2`、`sourcePolicyVersion=wan-result-hosts-v2`、`validationVersion=mp4-avc-v1`。D3 未改动任何冻结数值，也未新增设置项或顶层集合。
- 新增模块 `src/vagent/video/clients.py`（显式本地动作的客户端 Run 与幂等重放）、`src/vagent/video/usage.py`（读取侧聚合）、`web/storyboard-state.js`（分镜面板的纯状态逻辑）；`src/vagent/video/views.py` 扩展为工具、CLI、Web 共用的分镜投影。
- 工具集：M2 视图新增只读工具 `asset_read`（追加进 `M2_TOOL_NAMES`），`for_execution_version(1/2/3)` 的冻结变体继续排除全部 M2 工具；`tests/test_m2_compatibility.py` 中 8 组 pre-M2 签名（mock/live × context v1/v2 × 只读）与两组 toolFeatures 继续逐字节相等。
- 版本边界：D3 在同一个 4/4 版本内新增工具与规则文本，因此 **D2 构建录制的 M2 Run 不承诺跨 D3 恢复**——显式恢复在签名比对处返回 `RESUME_CONFIG_CHANGED`，不会以不同工具集静默继续；D2 构建的签名哈希作为证据保存在 `tests/test_m2_compatibility.py`（`D2_M2_SIGNATURES`、`D2_M2_INSTRUCTIONS`），并有专门用例断言“被拒绝而不是被静默续跑”。这与 D1 记录 D1→D2 边界的处理一致；**pre-M2 保持可恢复**。

## 3. 素材库写入侧与读取接口

- 导入校验按契约第 7 节：先由 `inspect_image` 从字节本身判定 PNG/JPEG/WebP 并读取尺寸与透明通道，再按提交前同一 `ImageInputRule` 校验边像素、宽高比、单图大小与 alpha；任一不满足即拒绝且不落盘、不建记录（用例断言 `assets/` 目录不出现）。
- 去重与恢复：`(projectId, sha256)` 命中即复用原 `assetId` 且不重写文件（用例断言文件 mtime 不变）；记录存在但文件缺失或损坏时，重新导入**同一张图片**会按原记录路径恢复文件并返回同一 `assetId` 与 `repaired=true`。跨项目复用按 `(projectId, sha256)` 各自建记录（素材库目的所在）。
- 路径安全：路径只由服务端 ID 构造（`assets/<projectId>/<assetId>.<ext>`），沿用媒体模块的 no-follow/reparse point 原语；`assets/coffee/../state.json`、非规范 UUID、越界扩展名、反斜杠路径等一律 `ASSET_SOURCE_UNSAFE`；写入用 `O_EXCL`，重复写同一路径为 `ASSET_FILE_CONFLICT`；删除或改写素材记录被 `ASSET_IMMUTABLE`/`ASSET_CONFLICT` 拒绝。
- 读取接口：`GET /api/assets`（可 `projectId` 过滤，默认全库）、`POST /api/assets`（multipart，CSRF + `projectId` + `file`，正文上限 `IMAGE_MAX_BYTES + 64 KiB`，超限 413，非 multipart 415）、`GET /api/assets/{id}`（元数据 + `available` + `contentUrl`）、`GET/HEAD /api/assets/{id}/content`（单 Range 206、416、`?download=1` 附件，MIME 与扩展名取自记录）。不存在 404 `ASSET_NOT_FOUND`；文件缺失或损坏 410 `ASSET_UNAVAILABLE`。响应不含 `relativePath`、本地绝对路径或私有 URL。
- 工具 `asset_read`：只读，省略 `assetId` 时按导入时间倒序列出最近 50 条（返回 `total`/`truncated`），可按 `projectId` 过滤；字段为 assetId、projectId、kind、mimeType、sizeBytes、dimensions、createdAt、source、assetFormatVersion，**不含 sha256、路径或私有 URL**；未知 ID 为 `ASSET_NOT_FOUND`；只读视图仍暴露该工具但不允许导入/删除。
- CLI：`vagent assets list [-s PROJECT]`（只读）与 `vagent assets import PATH... [-s PROJECT]`（逐文件报告成功/失败，任一失败退出码 1；重复导入报告 `deduplicated`）。
- 测试：`tests/test_asset_library.py`（13 项）、`tests/test_asset_api.py`（4 项）。

## 4. 分镜读取、估算预览与客户端动作

- 统一投影：会话快照新增 `storyboard`，另有 `GET /api/storyboards?projectId=`、`GET /api/storyboards/{id}?projectId=`；三者与 `storyboard_read` 共用 `video/views.py` 的同一次投影（按 order 排序、逐镜头版本历史、`current`、`latest`）。镜头媒体**由 Job 派生**：M2 不写 `ShotVersion.mediaId`（保持 null），`mediaUrl` 只在对应 Job 当前可用性为 available 时出现。
- 估算预览：`GET /api/storyboards/{id}/estimate` 只读返回逐镜头模式/时长/估算、合计与（新的）Run 额度快照，不登记、不提交、不写状态；同一逻辑在真正登记时再次执行并由服务端强制三重上限（超出整批不登记）。
- 客户端 Run：`video/clients.py` 为一次显式用户动作创建一条持久 Run（`model="local-client"`、`status=completed`、0 模型步、记录当前 `videoMode` 与 4/4 `toolFeatures`），**不更新会话的 `latestRunId`**，因此对话 Run 的恢复路径不受影响；额度按该 Run 计算，每次显式请求都是新的额度。Run ID 由 `(sessionId, clientRequestId)` 确定性派生，同一请求重放原结果、不重复登记；同一请求 ID 用于另一个镜头返回 `OPERATION_CONFLICT`；过期 `expectedRevision` 返回 `VERSION_CONFLICT`，且不会以更新的分镜静默继续；注定失败的动作（未知镜头、越界项目、版本过期）在创建 Run 之前就被拒绝。
- CLI `vagent shots redo SHOT_ID`：由镜头 ID 解析所属分镜与项目（可用 `-s/--storyboard` 核对），为该镜头追加一个新 `ShotVersion` 并登记一个新 Job，其他镜头的版本、Job 与媒体不动；只登记、不提交上游。省略 `--request-id` 即一次新的重做，提供同一 ID 时重放原结果（CLI 不重述 revision，因此重试仍然幂等）。
- 测试：`tests/test_storyboard_views.py`（6 项）、`tests/test_m2_clients.py`（7 项）、`tests/test_shot_api.py`（3 项）、`tests/test_m2_cli.py`（4 项），以及 `tests/test_m2_compatibility.py` 新增的 3 项边界用例。

## 5. 用量统计

- `video/usage.py` 与 `GET /api/usage?projectId=`、`vagent usage --project [PROJECT]`（省略取值时汇总整个数据目录）输出同一对象：Jobs 计数/状态/按模式、未结束数、登记估算合计与按报价版本、`actualStatus`（未取得账单保持 unknown 且不与估算混同）、分镜数与镜头版本数、本项目素材数与库总量、模型的已记录调用数/Token/加权缓存命中率与覆盖率、按项目 `runs` 汇总，以及最近 31 天的按日记录。
- 语义：只读本地记录，不调用模型或供应商，不写状态（用例断言聚合前后 `state.json` 全等）；空范围不会声称用量完整；未知用量不计作零。
- 测试：`tests/test_video_usage.py`（6 项）。

## 6. 失败与偏差记录

开发过程中由新用例、受控浏览器与既有回归暴露并修正的缺陷：

1. **`JobResultV3` 缺失（D2 遗留，D3 首次交付真实镜头时暴露）**：交付路径只为单镜头结果建模，`JobV3.result` 沿用 `JobResultV2` 时其 `spec`（`VideoSpecV2`）无法等于镜头的 `ShotSpecV3`（pydantic 严格模式报 `model_type`），M2 真实镜头因此永远无法进入 `succeeded`。改为新增 `JobResultV3(JobResultV2)`（`spec: ShotSpecV3`）、`JobV3.result` 指向它，并在媒体提交时按 Job 类型选择结果模型；`JobV2`/M1-C 结果与校验不变。
2. **`VideoRequestV3.public_arguments` 缺失（D2 遗留）**：`live_job_snapshot` 对每个 live Job 调用 `job.request.public_arguments()`，该方法只定义在 `VideoRequestV2` 上，因此任何 M2 真实 Job 的视图（`/api/jobs`、会话快照、`job_get`、SSE）都会 500。补上 V3 的公开字段投影（provider/model/capabilitiesVersion/prompt/spec/sourceRefs），并断言视图不含 `workspaceId`。
3. **分镜面板不跟随 SSE Job 事件（D3 缺陷，受控浏览器暴露）**：`job.updated` 只刷新了对话内的 Job 列表，新面板仍显示旧状态，交付完成后既看不到“已完成”，也没有媒体可播放；同时镜头状态与媒体链接只取自分镜快照，交付后仍是旧值。现在 Job 事件也会刷新面板（连播期间保留播放器，停止后整体重绘），并且镜头状态与媒体链接在分镜快照未回流时**从 Job 派生**（`jobMediaUrl`/`shotState(shot, jobs)`），与 Job 卡同一数据源。
4. **测试与夹具侧适配**：mock Job 保持 contract v1 且不携带 `shotRef`，因此面板与用例的“按镜头分组”以 `ShotVersion.jobId` 为主键（`shotRef` 仅作 live 的补充提示）；CLI 子进程测试的环境改为系统变量白名单（宿主环境整块可能超过 Windows 的 32K 限制）；浏览器检查脚本修正了 Playwright `waitForFunction` 的参数位（options 必须是第三个参数，否则静默使用默认超时）；浏览器夹具需要注入离线模型以跳过首次运行的设置对话框，上游媒体主机必须落在 `wan-result-hosts-v2` 允许集内，并且真实 15 秒轮询策略需要夹具加速时钟才能在可接受时长内看到交付（不改任何冻结策略值）。
5. **一次误写**：在 `tests/` 下误建了一个临时占位文件，立即删除，未进入任何提交。

## 7. 门禁与回归

- 离线全量：**917 passed / 2 skipped**（D2 基线 868 passed / 2 skipped；D3 新增 49 项：素材库 13、素材接口 4、用量 6、分镜投影 6、客户端动作 7、分镜接口 3、CLI 4、兼容边界 3）。另有 11 项前端状态测试由 `node --test` 运行，不计入 pytest 计数。两项跳过仍为 Windows 账户无符号链接权限的既有跳过。
- `ruff check` 与 `ruff format --check` 全库通过；`node --check` 覆盖 `web/app.js`、`web/job-state.js`、`web/storyboard-state.js`，`node --test tests/test_job_state.mjs`（10 项）与 `node --test tests/test_storyboard_state.mjs`（11 项）通过；`.github/workflows/ci.yml` 已加入新的 node 检查步骤。
- 兼容证据：pre-M2 签名与 M1-C/M1-B/B0/B2/B5 兼容套件、等待与恢复套件、媒体与评测套件全部通过（`tests/test_m2_compatibility.py`、`tests/test_wait_compatibility.py`、`tests/test_b0_compatibility.py`、`tests/test_m1c_evaluation.py`、`tests/test_m1b_evaluation.py` 等）。
- 安装态：以工作环境构建 wheel，在独立环境（`tmp/m2-d1-wheel-env`，仅替换 vagent 包）安装后运行 `scripts/wheel_smoke.py --output output/m2-d3-wheel-smoke.json` → **PASS**（含 Web 资源、配置、流式、文本/质量、MCP、mock 视频工具、持久等待、原 Run 重启续接、只读交付、预算不变、CLI list/get/usage、M1-B 五例、live 协议与本地媒体重启、M1-C 七例）。

## 8. 受控浏览器验证

- 夹具与脚本：`tmp/m2-d3-browser/launcher.py`（live 模式、受控上游与本地测试 MP4、加速时钟、注入离线模型）与 `tmp/m2-d3-browser/check.mjs`；Chrome 155.0.8059.84（headless）。
- 结果：**7/7 阶段通过，0 失败**，且接口无 4xx/5xx、无页面错误、无控制台错误、无失败请求。
  1. 分镜面板与镜头卡：3 个镜头按顺序渲染，未登记时无版本记录，i2v 镜头的缩略图来自素材库（`/api/assets/{id}/content`，自然宽度 1280）。
  2. 批量估算与显式确认：确认框逐镜头列出模式/时长/估算与合计 `9.00 CNY` 及剩余额度，确认后 3 个任务登记并全部交付（每个镜头的版本记录与任务卡播放器各 3 个）。
  3. 序列连播：读取到本地 MP4 的 `1280×720 / 5 秒`，开始播放、切到第 2 个镜头、停止后播放器移除。
  4. 逐镜头重做：仅目标镜头新增一个版本与一个新任务，其他镜头版本数不变，新任务同样交付。
  5. 素材库：已有素材预览为真实本地图片（1280 宽）；上传新增一张；同哈希再次导入复用原素材（不新增记录）。
  6. 用量面板：任务计数、登记估算合计 `12.00`（3 + 1 次重做）、账单保持未知、分镜与素材统计、Token 覆盖率、按日记录齐备。
  7. 页面无错误：上述四项计数全部为 0。
- 证据：`output/m2-d3-browser.json`（阶段、空失败列表、浏览器版本、连播媒体尺寸、夹具与素材 ID）与 7 张截图 `output/m2-d3-browser-{storyboard,confirm,delivered,sequence,redo,assets,usage}.png`。

## 9. 四组 CI

四组 CI（ubuntu/windows × 3.11/3.12，`.github/workflows/ci.yml`「Agent checks」）在 D3 收尾提交 **`39e6977`** 上全部完成，结论均为 **success**：check runs `117327535036`（ubuntu 3.11）、`117327535155`（ubuntu 3.12）、`117327534768`（windows 3.11）、`117327535018`（windows 3.12）；汇总证据保存在 `output/m2-d3-ci-39e6977-summary.json`。该提交的树包含全部 D3 代码（`e5d1e90` 之后只有文档提交 `d262605` 与本验收记录）。该轮 CI 依次执行 ruff check、node 语法检查与 `node --test`（两个前端状态测试文件均为 0 跳过）、pytest、wheel 构建与安装态 `scripts/wheel_smoke.py`；安装态 smoke 输出 `PASS: installed wheel, Web assets, config, streaming, text/quality, MCP, mock video tools, durable wait, original Run restart/automatic continuation, readonly delivery, unchanged budgets, CLI …`，完整输出见该 job 的日志。

套件结果：ubuntu 3.11 **916 passed / 3 skipped**（238.37 秒）、ubuntu 3.12 **916 passed / 3 skipped**（171.25 秒）、windows 3.11 **918 passed / 1 skipped**（600.02 秒）、windows 3.12 **918 passed / 1 skipped**（582.78 秒）。平台间差异（本机 Windows 2 跳过、CI Windows 1 跳过、ubuntu 3 跳过）与 D1/D2 同因，仍为既有的符号链接权限类跳过与 Windows 专用控制台夹具（`skipif(sys.platform != "win32")`），不是 D3 引入。

D3 未使用 CI 试错：离线全量套件、ruff、前端检查、安装态 wheel smoke 与受控浏览器验证都在推送前于本机完成（见第 7、8 节），因此四组 CI 在首次推送即通过，没有需要记录的失败轮次或重跑。其后的文档提交（本条记录与 README、`M1_PLAN`、Web README 的同步）不改动代码。

## 10. 留给 D4 的入口

- `scripts/evaluate_m2.py`（默认离线）与真实成本受控多镜头联调（2 镜头 + 1 次重做，submit ≤ 3、累计估算 ≤ 9.00 CNY，须显式批准额度）；wheel smoke 的 M2 扩展（`storyboard show`、`shots redo`、`assets`、`usage --project` 与素材接口）；`docs/M2_ACCEPTANCE.md`；README 与计划的最终同步。
- 可直接复用的 D3 证据：受控浏览器夹具与检查脚本（覆盖除真实 Key 与真实上游之外的全部路径）、用量聚合（`M2_ACCEPTANCE.md` 的费用与 Token 段落）、以及 [M2 契约](./M2_CONTRACTS.md) 第 12 节的实现补记。
