# M7 E1 验收：季与 schema 9

日期：**2026-10-23**。基线：`master` `dd3dfde`（M7 E0 契约与 `docs/M7_E0_BASELINE.json` 已合入；M6 E4 = `5cfd754`）。范围见 [M7 契约](./M7_CONTRACTS.md) §9 的 E1，冻结决策见 §1，基线证据见 [E0 验收](./M7_E0_ACCEPTANCE.md) 与 `docs/M7_E0_BASELINE.json`。**E1 未读取凭据、未访问供应商、未登记 Job、未调用文本模型或 TTS：视频 submit=0、模型调用=0、TTS 请求=0。** 本轮不做 9:16 竖屏，也不实现批次与时间线（E2/E3）。

## 1. 已交付

| 项目 | 交付 |
|---|---|
| 存储 | schema **9**：新增 `DatabaseV9` 与 `seasons`/`batches`/`timelines` 三个集合；`v8→v9` 只加三个空集合，v1–v8 全部可读；迁移前逐字节备份 `state-v8-*.json`，不重编码历史对象 |
| 季域 | `src/vagent/video/seasons.py`：`Season`/`SeasonVersion` 严格契约（版本连续、`currentVersion` 指向最新版），只保存标题与 1–4 个有序 Episode ID；不复制角色/风格，不拥有场记、分镜、预算、Job 或成片 |
| 提交校验 | `validate_season_changes` 在 Store 每次打开与提交时复核：每项目 ≤8 季、每季 1–4 集、成员有序唯一、同一集至多属于一个季的当前版本；历史版本逐字节保留，删除或就地改写在提交前拒绝 |
| 保存语义 | `season_save`：新建或追加 `SeasonVersion`；相同标题与相同顺序返回当前版本并标记 `replayed=true`，不追加空版本；内容变化必须带 `expectedRevision`，缺失或过期返回 `SEASON_VERSION_CONFLICT` |
| 读取语义 | `season_read`：季、当前版本、`revision`、成员集摘要（版本、场数、镜头数、目标时长）与全部季列表；省略 `seasonId` 时读取最新创建的季；E1 恒返回 `batch=null` |
| 工具与规则 | tools/rules **13/13 → 15/15**（mock 与 live 同步）；`season_save`/`season_read` 追加到末尾，13/13 与更早变体按 `toolFeatures` 冻结，旧 prompt/规则字节不改 |
| CLI | `vagent season show|save`（离线、无 Key、无 Job；`--expected-revision` 做乐观并发，`--request-id` 重放） |
| HTTP / Web | `GET /api/seasons?projectId=`、`GET /api/seasons/{id}?projectId=`、`POST /api/sessions/{id}/seasons/save`；侧栏「季」面板支持季选择、新建、标题编辑、按点击顺序选择 1–4 集与保存，`web/season-state.js` 为纯投影并有 7 项 Node 测试 |
| 迁移与兼容 | 打开旧库仍按 v1–v8 逐级机械升级；`DatabaseV9` 的 `batches`/`timelines` 为 JSON 对象集合，E1 保持为空，E2/E3 再各自注册域校验器，避免 v9 信封再次改形 |
| 验收文档 | 本文件；[M7 契约](./M7_CONTRACTS.md)、[M5 计划](./M5_PLAN.md)、[M1 计划](../M1_PLAN.md)、[README](../README.md) 与 [Harness 设计](../AGENT_HARNESS_DESIGN.md) 已同步 |

## 2. schema 9 与迁移证据

- `tests/test_m7_schema.py::test_fresh_store_is_schema_nine_with_three_empty_collections`：新数据目录直接是 `schemaVersion=9`，`seasons={}`、`batches={}`、`timelines={}`，`DatabaseV9` 校验通过。
- `test_v8_state_migrates_to_v9_with_an_exact_backup`：用真实产品路径（`episode_save` + `storyboard_save`）生成 v9 状态，再机械构造 v8 信封打开；迁移后 `schemaVersion=9`、三个集合为空、**其他每个集合逐字段相等**；`state-v8-*.json` 备份与原始字节逐字节一致；`checkpoints.sqlite` 哨兵不变；再次打开不再产生备份。
- 历史链：v1（`test_b0_compatibility`、`test_storage_migration`）、v1/v2（`test_m1c_migration`）、v3（`test_m2_migration`）、v4（`test_m3_baseline`）、v5（`test_m5_baseline`）、v6/v7（`test_m6_schema`）全部继续迁移到 v9，原 v3/v4/v5/v6 信封投影哈希仍逐字节复现。
- 契约基线比对：`test_every_e0_baseline_schema_hash_is_unchanged` 重算 E0 的 **165** 个模型哈希（9 个模块），**全部逐字节不变**；新增模型只有 `DatabaseV9`、`Season`、`SeasonVersion`。`test_frozen_stage_limits_and_tool_groups` 复核 M2/M3/M5/M5E2/M5E3/M6E1/M6E2/M6E3 工具名分组、等待 20/600 秒与全部冻结上限不变。
- 上限与版本：`storageSchemaVersion` 8→9 与 `videoToolsVersion`/`videoRulesVersion` 13→15 是本阶段仅有的声明式变化；季上限冻结为每项目 8 季、每季 4 集、标题 120 字符。

## 3. 季的不变量

| 不变量 | 拒绝结果 |
|---|---|
| 一季 1–4 集，标题 1–120 字符 | `SEASON_LIMIT`（工具 schema 先拒绝空数组/5 个成员/超长标题时为 `INVALID_ARGUMENTS`） |
| 每项目 ≤8 季 | `SEASON_LIMIT` |
| 成员必须是本项目已保存的分集 | `SEASON_EPISODE_CONFLICT` |
| 同一季内不能重复同一集 | `SEASON_EPISODE_CONFLICT` |
| 一集至多属于一个季的当前版本 | `SEASON_EPISODE_CONFLICT`；换季需先在原季追加移出新版本 |
| 修改必须匹配 `revision` | `SEASON_VERSION_CONFLICT` |
| 季不存在或跨项目 | `SEASON_NOT_FOUND` |
| 历史版本只增不改、季记录不可删除 | `SEASON_VERSION_CONFLICT`（提交前拒绝，原状态保留） |
| 相同内容重放 | 返回当前版本，`replayed=true`，不追加版本 |

`tests/test_m7_seasons.py` 覆盖：有序版本与旧版本逐字节保留、相同内容重放（带/不带 `seasonId`）、过期 `revision`、未知季、重复成员、换季先移除再加入、1–4 与 8 季上限、空项目读取、提交期篡改拒绝；并断言两次保存后 `state["jobs"] == {}`。

## 4. 工具面与冻结变体

- `tests/test_m7_tools.py::test_tool_versions_are_fifteen_and_thirteen_is_frozen`：当前 mock/live 均为 **15/15**，`M7_TOOL_NAMES=("season_save","season_read")`，此前所有工具名分组的并集等于 `PRE_M7_E1_TOOL_NAMES`。
- `test_new_runs_publish_fifteen_and_frozen_thirteen_hides_the_season_tools`：新 Run 发布 15/15 且规则含季说明；用当前 features 改 `toolsVersion/rulesVersion=13` 精确命中冻结 13/13 视图——季工具与季规则消失，`film_render`/`subtitle_save` 与 M6 规则字节仍在。因 13/13 是首个带 `tts` 能力的默认版本，冻结变体保留完整 configuration（含 `tts`），真实的 M6 E3 记录可直接恢复。
- `test_season_tools_are_declared_with_the_right_effects`：`season_save` 为 `write`、`season_read` 为 `read`，输入 schema 分别含 `title/episodeIds/seasonId/expectedRevision` 与 `seasonId`。
- `test_read_only_view_keeps_season_read_and_hides_season_save`：只读视图保留 `season_read`。

## 5. CLI、HTTP 与 Web

- CLI `tests/test_m7_season_cli.py`（2 项）：在 `VAGENT_VIDEO_MODE=off` 的子进程里 `season show` 读取季与成员；`season save` 新建第二季、同一 `--request-id` 重放同版本、以 `--expected-revision 0` 追加 v2 并重排成员；未知季与已归属集返回非零，stderr 含冻结错误码。写入的 metadata-only 客户端 Run 为 `videoMode=off`、`toolFeatures={}`，且 `state["jobs"] == {}`。
- HTTP `tests/test_m7_season_api.py`（2 项）：视频模式 off 下 `GET /api/seasons*`、`POST /api/sessions/{id}/seasons/save` 可用；列表、详情、会话快照 `season` 投影一致；相同内容 201 + `replayed=true`；过期 `revision` 返回 409 `SEASON_VERSION_CONFLICT`；缺 CSRF 403、未知字段 422、超过 4 集 422、未知季 404 `SEASON_NOT_FOUND`。
- Web `tests/test_season_state.mjs`（7 项）：季元数据、成员顺序、季选择器、跨季归属禁用、失效成员提示、标题/1–4 集校验、保存体只在其一时携带 `expectedRevision`。`node --check web/season-state.js` 与 `node --check web/app.js` 通过；CI 已加入该检查与测试。
- 受控浏览器（headless Chrome 155，CDP）：在视频模式 `off` 的本地服务上打开界面，选择 `coffee` 项目并打开「季」面板；面板显示既有季「浏览器第一季」v1 / revision 0 / 2 集。改为「浏览器第一季·改名」、按点击顺序加入第三集并保存后，面板与 `/api/seasons` 都变为 v2 / revision 1 / 3 集，提示「季已保存为 v2」；控制台无错误，`/api/jobs` 为 0。截图与摘要见 `output/m7-e1-browser/season-panel.png` 与 `season-panel.json`（`output/` 为本地证据目录，不入库）。
- 面板行为：侧栏「季」打开 `#season-panel`；选择季时按需读取 `/api/seasons/{id}`；勾选顺序即成员顺序；属于其他季当前版本的集禁用并提示先在原季移除；保存成功后刷新会话快照并重新读取该季。面板只调用季端点，不触发 `season_generate`（E2）或渲染。

## 6. 零费用与零模型证据

- 季保存走 `clients.save_season` 的 metadata-only 路径：`client_context(..., metadata_only=True)` 以 `videoMode=off`、`toolFeatures={}` 记录一个终态本地 Run，不调用 `service.generation_mode()`，因此 off/mock/live 三种模式都不需要视频 Key。
- 领域层不触碰 Job、媒体、供应商或 TTS；测试在保存后断言 `state["jobs"] == {}`，CLI 测试额外断言 `state["schemaVersion"] == 9` 与客户端 Run 的 `videoMode=off`。
- 本阶段没有读取 `.env` 或供应商凭据，没有发起任何 HTTP 请求；离线套件只使用本地夹具与真实 Store 代码路径。

## 7. 回归与复现

```powershell
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
node --check web/season-state.js
node --test tests/test_season_state.mjs
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

本机结果（2026-10-23）：

- 全量回归：**1371 passed / 3 skipped**（1198.81 秒，0:19:58；3 项 skip 为既有的 Windows 符号链接权限用例）；Ruff check/format（204 个文件）通过；前端七个状态文件（含 `season-state.js`）**58 passed**。
- 两个测试夹具的上下文预算（不是产品默认值或上限）因规则文本增长而上调：`tests/test_m1c_evaluation.py` 64 KiB→72 KiB、`test_live_application` 的脚本化四工具调用场景 64 KiB→72 KiB（M6 E1 已把 M2 evaluation 提到 96 KiB）。产品的 `VAGENT_CONTEXT_BYTES` 默认值未改变。
- E1 定向用例：`tests/test_m7_schema.py`、`tests/test_m7_tools.py`、`tests/test_m7_seasons.py`、`tests/test_m7_season_cli.py`、`tests/test_m7_season_api.py` 共 **29 passed**。

## 8. 边界与明确未做

- **批次未实现**：`season_generate`、`Batch`/`BatchEpisode`、失败隔离与显式恢复属 E2；`batches` 集合保持为空。
- **时间线未实现**：`timeline_save`/`timeline_read`、`Timeline`/`TimelineVersion`、`film_render` 的 timeline scope 与 `ffmpeg-render-timeline-v1` 属 E3；`timelines` 集合保持为空。
- **季不拥有成片**：季只保存成员关系与标题；场记、镜头、预算、Job 与成片仍按集独立，跨集「超级成片」不在 M7。
- **Web 预览不是交付物**：E1 面板只编辑成员与标题，不提供时间线预览；时间线编辑与客户端顺序播放属 E3。
- **9:16 竖屏、多轨混音工作站、关键帧/变速/特效、后台批处理守护进程**：不在 M7。
- **真实付费样本**：E1 未提交任何任务；E4 如需新增样本必须单独批准模型、次数与金额。

## 9. E2 入口

1. 按 [M7 契约](./M7_CONTRACTS.md) §2.2/§3 新增 `Batch`/`BatchEpisode` 与 `season_generate`；`DatabaseV9.batches` 的 JSON 对象集合直接承接记录，Store 增加 `validate_batch_changes`，不改 v9 信封。
2. 批次 1–3 集、逐集原子登记、单集失败隔离、`resumeOf` 新建恢复批次、每季至多一个 `running` 批次、60 Job/180.00 CNY 预检；崩溃恢复把 `running` 批次转为 `partial`/`failed`，成员集 `BATCH_INTERRUPTED`。
3. 工具面升到 **16/16**（冻结 15/15），Web 批量确认/失败隔离/恢复；`season_read` 开始返回最近批次与逐集交付计数。
4. E2 仍不提交供应商：离线套件使用 mock/受控 HTTP 与本地测试媒体，视频 submit=0；真实付费样本需单独授权。
