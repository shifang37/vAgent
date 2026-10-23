# M7 E2 验收：批量队列

日期：**2026-10-23**。基线：`master` `2fc192b`（M7 E1 季与 schema 9 已合入；M7 E0 契约 = `dd3dfde`）。范围见 [M7 契约](./M7_CONTRACTS.md) §3 与 §9 的 E2，冻结决策见 §1，基线与本地裁剪证据见 [E0 验收](./M7_E0_ACCEPTANCE.md) 与 `docs/M7_E0_BASELINE.json`。**E2 未读取凭据、未访问供应商、未提交任何视频或 TTS 请求：视频 submit=0、模型调用=0、TTS 请求=0。** 本轮不实现时间线（E3），也不做 9:16 竖屏。

## 1. 已交付

| 项目 | 交付 |
|---|---|
| 批次域 | `src/vagent/video/batches.py`：`Batch`/`BatchEpisode`/`BatchEstimate` 严格契约；`Batch.status` 只由成员推导；成员、目标、指纹与 `createdAt` 冻结；终态不可改写；每项目 ≤64 条 |
| 登记语义 | `JobService.season_generate`：一次 1–3 集、逐集独立的 Store 事务（键 `batch:<batchId>:<episodeId>`）走既有整集登记；单集失败只写该集 `errorCode`，其他集继续；每集仍受冻结的 20 Job / 60.00 CNY 约束 |
| 批量上限 | 预检 3 集 / 60 个新 Job / 180.00 CNY；超限整批拒绝（`BATCH_LIMIT`），不写批次、不登记任何集。每季同时至多一个 `running` 批次（`BATCH_CONFLICT`） |
| 进度与交付 | `season_read` 返回最近批次或指定 `batchId`：逐集登记状态、`shotCount`、`jobIds`、复用数、估算、错误码，以及从 Job/媒体计算的 `deliveredShots/failedShots/pendingShots`；交付失败不反写批次 |
| 显式恢复 | `resumeBatchId` 与 `episodeIds` 互斥；只为旧批次中非 `succeeded` 的集创建 `resumeOf` 新批次，按恢复时各集**当前版本**重新冻结；旧批次逐字节保留，不自动重跑、不重投已进入不确定窗口的提交 |
| 崩溃恢复 | 打开 Store 时把 `running` 批次改为 `partial`/`failed`，把 `pending/running` 成员改为 `failed`（`BATCH_INTERRUPTED`）并推进 revision；恢复只走显式 `resumeBatchId` |
| 幂等与重放 | 批次 ID 由调用身份确定性生成（uuid5）；重放同一调用返回原批次。普通生成若与已终态批次指纹完全一致则重放该批次（`replayed=true`），不重复登记 Job |
| 工具与规则 | tools/rules **15/15 → 16/16**（mock 与 live 同步）；`season_generate` 追加到末尾；15/15、13/13 与更早变体按 `toolFeatures` 冻结，旧 prompt/规则字节不改 |
| CLI | `vagent season generate --season ... [--episode ...|--resume ...] --confirm`；`season show --batch` 读取指定批次。离线、无真实 Key、无提交 |
| HTTP / Web | `POST /api/sessions/{id}/seasons/generate`、`GET /api/seasons/{id}/estimate`、`GET /api/seasons/{id}?batchId=`；季面板加入批量确认对话框、逐集隔离状态与显式「恢复失败集」，`web/season-state.js` 纯投影新增 7 项 Node 测试 |
| 验收文档 | 本文件；[M7 契约](./M7_CONTRACTS.md)、[M5 计划](./M5_PLAN.md)、[M1 计划](../M1_PLAN.md)、[README](../README.md)、[Web 说明](../web/README.md) 与 [Harness 设计](../AGENT_HARNESS_DESIGN.md) 已同步 |

## 2. 批次不变量

| 不变量 | 拒绝结果 |
|---|---|
| 一季 1–3 集、成员唯一且属于该季当前版本 | `BATCH_LIMIT`（4 集缺省）/ `BATCH_CONFLICT`（非成员）/ schema 先拒绝空或 4 个显式 ID |
| 每项目 ≤64 条批次 | `BATCH_CONFLICT` |
| 批次目标/成员/指纹/`createdAt` 冻结 | `BATCH_CONFLICT`（提交前拒绝） |
| 终态批次不可改写、不可删除 | `BATCH_CONFLICT` |
| revision 每次推进只 +1，更新时间不回退 | `BATCH_CONFLICT` |
| 成员状态只能 `pending → running → succeeded/failed`；终态成员逐字节保留 | `BATCH_CONFLICT` |
| 批次上限 60 新 Job / 180.00 CNY | `BATCH_LIMIT`，整批未登记 |
| 每季只允许一个 `running` 批次 | `BATCH_CONFLICT` |
| 恢复必须指向同一项目已结束且含失败集的旧批次 | `BATCH_CONFLICT` |
| 登记时成员分集版本必须仍等于冻结版本 | 该集 `BATCH_CONFLICT`，其他集继续 |

`tests/test_m7_batches.py`（10 项）覆盖：3 集批次 36 Job、逐集 `shotCount/jobIds` 与 revision 6；重放同调用不新增 Job；无分镜的成员单独 `BATCH_EMPTY` 且其余集照常登记；4 集季缺省 `BATCH_LIMIT` 与显式 3 集成功；预算上限预检整批拒绝；恢复只重登记失败集并冻结新版本；`running` 批次阻止第二个批次；崩溃恢复把 `running` 改为 `partial`、未完成成员写入 `BATCH_INTERRUPTED`；终态批次不可改写/删除；预览不写存储且返回逐镜头估算；批次完成后的 `shot_redo` 只新增一个 Job，不改写终态批次。

## 3. 崩溃恢复证据

- `test_store_recovers_a_running_batch_as_interrupted_on_open`：先真实登记一集得到成功成员与其 Job，再构造一个 `running` 批次（成功成员 + 待登记成员）；关闭并重新打开 Store 后，顶部状态为 `partial`、revision 从 0 推进到 1，成功成员逐字节保留，待登记成员变为 `failed` + `BATCH_INTERRUPTED`。
- 打开时的 `validate_batch_changes(initial=True)` 只做结构、成员归属与 Job 引用校验；创建规则（`running`/`revision 0`）与逐次推进规则只在提交事务内执行，因此恢复本身也经过同一校验器。
- 恢复不修改任何 Job、不重投 `submitting`/unknown 提交：批次层没有提交路径，供应商提交仍只发生在既有 Job Worker。

## 4. 工具面与冻结变体

- `tests/test_m7_tools.py::test_tool_versions_are_sixteen_and_fifteen_is_frozen`：当前 mock/live 均为 **16/16**，`M7_E2_TOOL_NAMES=("season_generate",)`，`M7_TOOL_NAMES` 为三件套，`PRE_M7_E2_TOOL_NAMES` 等于此前全部工具名分组的并集。
- `test_new_runs_publish_sixteen_and_frozen_fifteen_hides_the_batch_tool`：新 Run 发布 16/16 且规则含 `resumeBatchId`；用当前 features 改 `toolsVersion/rulesVersion=15` 精确命中冻结 15/15 视图——`season_generate` 与批次规则消失，`season_save`/`season_read` 与 M7 E1 规则字节仍在；13/13 变体同时隐藏三个季工具。
- `test_season_tools_are_declared_with_the_right_effects`：`season_generate` 是 `write` 且只有 `context_execute`；输入 schema 含 `seasonId/confirm/episodeIds/resumeBatchId`；`season_read` 增加 `batchId`。
- `test_read_only_view_keeps_season_read_and_hides_both_season_writers`：只读视图保留 `season_read`，隐藏 `season_save` 与 `season_generate`。

## 5. CLI、HTTP 与 Web

- CLI `tests/test_m7_batch_cli.py`（2 项）：在 `VAGENT_VIDEO_MODE=mock` 的子进程里 `season generate` 登记 2 集 / 24 个 mock Job；同一 `--request-id` 重放同一批次并标记 `replayed=true`；`season show --batch` 与 `batches` 摘要一致；缺 `--confirm` 时非零退出、`CONFIRMATION_REQUIRED`、不写 Job。
- HTTP `tests/test_m7_batch_api.py`（2 项）：`GET /api/seasons/{id}/estimate` 回显 24 个新任务、逐集 12 条逐镜头计划且不写批次/Job；`POST /api/sessions/{id}/seasons/generate` 返回 202 与逐集状态，季详情、会话快照与 `batches` 列表一致；相同 `clientRequestId` 与“新请求但内容相同”都重放同一批次；缺 CSRF 403、`episodeIds+resumeBatchId` 422、未知字段 422、未知季 404、恢复已全部成功的批次 409 `BATCH_CONFLICT`。
- Web `tests/test_season_state.mjs`（14 项，其中 7 项为 E2 新增）：批次元数据与状态文案、逐集交付计数、恢复目标只含非成功集、4 集季不默认选中、1–3 集校验、批量请求体在 `episodeIds` 与 `resumeBatchId` 之间二选一且恒带 `confirm=true`。
- 面板行为：保存季后 `批量生成` 区块列出已保存成员（≤3 集，4 集时默认不选并要求勾选）；点击「预览并生成」先调 estimate 再弹出确认对话框（逐集镜头数/新任务/估算/不可登记原因/上限/进行中批次），确认后提交；批次卡片显示状态、revision、逐集登记与交付计数、错误码；`partial/failed` 时出现「恢复失败集（n 集）」，恢复同样先预览再显式确认。面板只调用季与批次端点，不提交供应商。
- 受控浏览器（headless Chrome，CDP）：在 `VAGENT_VIDEO_MODE=mock` 的本地服务上打开 `coffee` 项目与「季」面板。面板显示「浏览器第一季」v1 / 2 集；点击「预览并生成 2 集」后对话框逐集列出 12 个镜头 / 12 个新任务 / 估算「—」，按钮为「确认生成 24 个新任务」；确认后面板显示批次 `30cd90e9 · 全部成功 · 2 集 · 24 个新任务 · revision 4`，两行成员分别为「第1集 / 第2集 · 全部成功 · 登记 12/12 个任务 · 交付 0 / 失败 0 / 待定 12」。控制台 **0 错误**，页面无异常；数据目录中 24 个 Job 全部为 `mode=mock`、1 条 `succeeded` 批次。截图与摘要见 `output/m7-e2-browser/batch-panel.png` 与 `batch-panel.json`（`output/` 为本地证据目录，不入库）。

## 6. 零费用与零模型证据

- `season_generate` 走客户端 Run（`videoMode=mock/live`、完整 `toolFeatures`），只调用既有 `episode_generate` 登记语义；不调用模型、不调用供应商提交、不创建等待绑定。mock 下 `totalEstimate=null`、`simulated=true`。
- 领域层与 Store 校验器不触碰媒体下载、TTS 或供应商客户端；测试断言 `submitCalls == 0`（M5 的 adapter ledger）与全部 Job `mode == "mock"`。
- 本阶段没有读取 `.env` 或供应商凭据；离线套件使用真实 Store 代码路径与本地夹具。

## 7. 回归与复现

```powershell
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
node --check web/season-state.js
node --check web/app.js
node --test tests/test_season_state.mjs
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

本机结果（2026-10-23）：

- 全量回归：**1385 passed / 3 skipped**（1498.66 秒，0:24:59；3 项 skip 为既有的 Windows 符号链接权限用例）；Ruff check/format（208 个文件）通过；前端七个状态文件 **65 项** 通过（`season-state` 14 项，其中 7 项为 E2 新增），`node --check web/app.js` 与 `web/season-state.js` 通过。
- E2 定向用例：`tests/test_m7_batches.py`（10）、`tests/test_m7_batch_api.py`（2）、`tests/test_m7_batch_cli.py`（2）、`tests/test_m7_tools.py`（5）、`tests/test_m7_schema.py`、`tests/test_m7_seasons.py`、`tests/test_m7_season_api.py`、`tests/test_m7_season_cli.py` 共 **43 passed**；前端 `season-state` 14 项通过。
- 版本敏感子集：`test_m2_compatibility`、`test_m2_clients`、`test_m2_generation`、`test_m2_integration`、`test_m3_recovery`、`test_m5_tools`、`test_m6_tools`、`test_video_integration`、`test_live_application` 共 **135 passed**；`test_m1c_evaluation` 与 `test_m5_episodes` 共 **35 passed**（未上调任何上下文预算）。
- 契约基线：`test_every_e0_baseline_schema_hash_is_unchanged` 复核 E0 的 165 个既有模型哈希全部不变；新增模型只有 `Batch`/`BatchEpisode`/`BatchEstimate` 与工具输入 `SeasonGenerationArgs`。

## 8. 边界与明确未做

- **时间线未实现**：`timeline_save`/`timeline_read`、`Timeline`/`TimelineVersion`、`film_render` 的 timeline scope 与 `ffmpeg-render-timeline-v1` 属 E3；`timelines` 集合保持为空。
- **批次不提交、不等待**：`season_generate` 只登记；提交、查询、下载与限流仍由既有 Job/媒体 Worker 负责。调用方需按集分段 `await_job`（每段 ≤20）。
- **交付失败不通过批次修复**：批次只记录登记结果；交付失败用既有 `shot_redo`/`episode_generate` 修复，不从批次重投。
- **真实付费样本**：E2 未提交任何任务；E4 如需新增样本必须单独批准模型、次数与金额。
- **9:16 竖屏、跨集超级成片、多轨混音工作站、后台批处理守护进程**：不在 M7。

## 9. E3 入口

1. 按 [M7 契约](./M7_CONTRACTS.md) §2.3/§4 新增 `Timeline`/`TimelineVersion`/`TimelineClip` 与 `timeline_save`/`timeline_read`；`DatabaseV9.timelines` 直接承接记录，Store 增加 `validate_timeline_changes`，不改 v9 信封。
2. `film_render` 增加 `scope=timeline` 与 `timelineId/timelineVersion`；新增装配字面量 `ffmpeg-render-timeline-v1`、`RenderSources.timeline`、`ffmpeg-render-timeline-v1` 的字幕/音频重映射（`timeline-subtitle-remap-v1`）。
3. 工具面升到 **18/18**（冻结 16/16），Web 时间线裁剪/排序/转场/预览/导出与客户端 BGM/SFX 受控导入。
4. E3 仍不提交供应商：渲染全程本地 FFmpeg，离线套件使用本地测试媒体；真实付费样本需单独授权。
