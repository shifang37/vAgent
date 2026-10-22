# M5 E3 验收：生成与重做

日期：**2026-10-22**。基线 `master` `569c53b`（M5 E2，schema 6，tools/rules 8/8）；本包交付 commit 见 `git log`。范围见 [M5 计划](./M5_PLAN.md) §3.2 的 E3，冻结契约见 [M5 契约](./M5_CONTRACTS.md) §5、§10。**E3 全程视频 submit=0：所有供应商调用走受控 `httpx.MockTransport`，媒体来自本地测试 MP4（`tests/fixtures/m1c/sample-720p.mp4`）；未读取真实 Key、未创建云端任务、未产生费用。E3 也不调用文本模型：离线用例直接驱动工具与服务。**

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 分集批量登记 | `src/vagent/video/jobs.py`（`episode_generate`）、`episodes.py`（`episode_shot_plan`）、`tools.py` | 新工具 `episode_generate(episodeId, confirm)`：一次为整集所有已绑定分镜的全部镜头登记任务；必须 `confirm=true`；返回逐镜头 `jobId`/`shotVersionId`/估算、逐场分镜与集级已用/余量；同一镜头相同冻结请求复用原任务 |
| 计划即权威 | `episodes.py` | 登记事务内重算分集 → 场记 → 分镜 → 镜头：任一场记未绑定分镜返回 `SCENE_CONFLICT`；已绑定分镜的镜头数或时长与冻结场记不一致分别返回 `EPISODE_SHOT_BUDGET` / `EPISODE_DURATION_LIMIT`；分镜合计必须等于集冻结的镜头数与时长，全部整批拒绝、不写任何 Job 或 ShotVersion |
| 集级额度 | `jobs.py`（`_episode_limits`、`episode_budget_usage`、`preview_shots`）、`episodes.py` | 按 Job 的 `shotRef.storyboardId` → 场记 → 分集归属，统计项目内**所有 Run** 的已登记真实 Job（含重做、`unknown` 与失败，失败不释放）；`jobCap`（默认 20）与 `episodeCapAmount`（默认 60.00 CNY）同时生效，越界返回 `EPISODE_COST_LIMIT` 并列出超限镜头与桶；只按冻结 CNY 记账、不换汇，逐镜头每秒单价必须等于 `perShotAmount/5`（0.60 CNY/秒）；Web/CLI 的 `preview_shots` 对已绑定分集的分镜返回同一集级额度（含已用量），确认对话框与实际登记不再显示两套上限 |
| 额度快照 | `src/vagent/video/contracts.py`（`RunLimitsSnapshot`） | 同一快照类型新增可选 `scope`（`run` 或 `episode`）与 `episodeId`；集级 Job 的 `cost.limits` 记录该集冻结的 `jobCap`/`episodeCapAmount`/已用与余量。默认 `run`，旧记录不补字段、不迁移 |
| Store 不变量 | `jobs.py`（`validate_job_changes`、`_validate_episode_buckets`） | 每次提交按整库重算集级任务数与估算 ≤ 冻结额度；集级 Job 必须引用该集已绑定分镜的镜头，币种与单价必须与冻结预算一致，快照上界必须等于冻结 `jobCap`/`episodeCapAmount`；篡改返回 `JOB_CONFLICT`/`EPISODE_COST_LIMIT`。9/9 Run 可承载整批集级 Job，其余真实 Job 仍受每 Run 4 个限制 |
| 逐镜头重做 | `jobs.py`（`redo_shot_tool`）、`tools.py` | 新工具 `shot_redo(storyboardId, storyboardVersion, shotId, expectedRevision)`：只为目标镜头追加新 `ShotVersion` 与新 Job，其他镜头的版本、任务与媒体不动；过期 revision 返回 `VERSION_CONFLICT`；同一调用键重放原结果；属于分集时同样占用集级额度 |
| 集合等待 | `src/vagent/waiting.py`、`tools.py` | `WaitBindingV3.job_ids` 上限 16→20、`WAIT_SET_LIMIT` 同步调整，因此一集最多 20 个 Job 可在一次 `await_job` 中交付；等待到期返回已完成部分，失败镜头不隐藏已交付镜头，也不自动重提 |
| 读取视图 | `episodes.py`（`episode_generation`、`read_episode`） | `episode_read.generation` 返回 `ready/problems`、逐镜头 `unregistered/pending/delivered/failed`（含 `jobId`、`shotVersionId`、`mediaId`、`estimate`、`error`）、逐场分镜与集级余量；未绑定/不一致的分集只报告问题，不失败 |
| 工具面 | `tools.py` | 新增 `episode_generate` 与 `shot_redo`，mock/live 同步 **8/8 → 9/9**（`M5_E3_TOOL_NAMES`）；冻结 **8/8** 视图（隐藏两个新工具、保留 E2 规则），7/7、6/6、5/5、4/4 继续隐藏 E3 工具；headless 规划工具集排除两个新写工具 |
| 文档 | 本文件、`docs/M5_CONTRACTS.md`、`docs/M5_PLAN.md`、`README.md` | 契约补 E3 细化、`limits.scope` 语义、错误码归属与交付检查；计划与 README 标注 E3 交付 |

## 2. 关键实现决策（相对契约的细化）

1. **集级额度是“每集”的，不是“每批”的。** 契约把上限写成“每集新 Job ≤ 20 / 每集成本 ≤ 60.00 CNY”，E3 按字面实现：同一分集跨 Run、跨重做累计，失败与 `unknown` 仍占额度。代价是 20 镜集在默认额度下没有重做余量（20 job 已用满）；12 镜 60 秒样例花费 36.00 CNY，留 8 次重做额度。这是验收中显式固定的行为，不是遗漏。
2. **计划即权威。** 集冻结的 `shotCount`/`targetDurationSeconds` 与分镜实际镜头数/时长必须一致，否则登记前就整批拒绝。这样“登记了哪些镜头”与“预算里的镜头”可逐条对应，`submit` 数、估算与集级额度三者一致可证明；代价是分镜必须先按草案保存，而不是登记时临时改镜头数。
3. **不换汇。** 集级额度按 E0 冻结的 Wan CNY 口径记账；Kling/fal 报价是 USD，进入集级批量会直接 `EPISODE_COST_LIMIT`。它们的镜头仍走 `shot_generate` 的逐 Run 额度（M3 语义不变）。同一检查还要求逐镜头每秒单价等于 `perShotAmount/5`，价格表变化不会把集预算悄悄花超。
4. **快照是同一类型的加法扩展。** 没有新增 Job/Request 契约版本：`RunLimitsSnapshot` 增加默认 `run` 的 `scope` 与可选 `episodeId`，`scope=run` 仍强制 `max/used ≤ LIVE_JOBS_PER_RUN`。集级 Job 用 `scope=episode` 记录真实的集级上限与累计用量，旧记录字节不变。
5. **9/9 Run 与旧额度共存。** 9/9 Run 可以把一集的整批 Job 放在同一 Run（每 Run 4 个不再约束集级 Job），同一 Run 中其他非集级真实 Job 仍受 4 个限制；6/6 及更早工具面的 Run 完全不变，Store 以 Run 记录的 `toolsVersion` 选择规则。
6. **`shot_generate` 与预览也计入集级额度。** 9/9 下对已绑定分集的分镜调用 `shot_generate`（Web/CLI 路径）同样走集级额度，关闭“绕过 `episode_generate` 花超预算”的旁路；`preview_shots` 对这类分镜返回同一集级额度（含已用量），确认对话框不会显示与实际登记不同的上限。没有绑定分集的 M2/M3 分镜保持逐 Run 语义。
7. **重做是工具，但仍只由用户显式发起。** 工具说明与 V9 规则都要求“只有用户明确要求时才调用 `shot_redo`”，`expectedRevision` 防止在已变化的记录上重做；CLI/Web 的 `redo_shot` 不变，但它们的客户端 Run 现在记录 9/9，因此同样受集级额度约束。
8. **集合等待一次覆盖一集。** 上限 16→20 是让 20 镜集不需要拆分等待；存储的旧 wait 记录（≤16 成员）继续有效，`WaitBindingV3` 的排序与唯一性约束不变。
9. **读取视图容忍不完整计划。** `episode_read` 是读取工具，不能因为场记未绑定就失败；`generation.ready=false` + `problems[]` 让 Agent 先修计划再登记，而不是把读取错误当成登记失败。
10. **失败隔离沿用 M2。** 集合等待逐 Job 返回 `jobs[]`、`succeeded`、`failed`，一个失败不隐藏其他已交付镜头；修复只通过目标镜头的 `shot_redo`，不重新提交其他镜头。

## 3. 验证结果

### 3.1 新增测试

`tests/test_m5_generation.py` **12 项**：

| 覆盖 | 断言 |
|---|---|
| mock 整批登记 | 12 镜头按“场记顺序 → 镜头序号”登记；每场分镜 revision +1、4 个 ShotVersion、12 个 mock Job；同调用重放同一结果，新调用全部 `reused=true` 且不新增 Job；`episode_read.generation` 的 `counts` 为 planned 12 / registered 12 / pending 12；mock 提交数 0 |
| 确认与不完整计划 | `confirm=false` → `CONFIRMATION_REQUIRED`；未绑定分集 → `SCENE_CONFLICT`；分镜 3 镜头 → `EPISODE_SHOT_BUDGET`；4 镜头但合计 16 秒 → `EPISODE_DURATION_LIMIT`；四类拒绝均不写 Job，读取视图 `ready=false` |
| 只读与跨项目 | 只读 Run → `READ_ONLY`；不存在的分集 → `EPISODE_NOT_FOUND`；均不登记 |
| 集级快照 | 12 镜头/60 秒 live 批次：预览先显示该场 4 个镜头/12.00 的集级额度，登记后逐镜头 3.00 CNY、合计 36.00；`limits` 为 `scope=episode`、`maxNewJobsPerRun=20`、`maxRunCost=60.00`、`usedNewJobs=12`、`usedAmount=36.00`、`remainingAmount=24.00`；登记后 12 个 `pending_submit`、上游提交 0；再次预览显示 12 个已用且全部 `reused`；`episode_read` 余量 8 个/24.00 |
| jobCap 跨 Run | 20 镜/80 秒集（5 场 × 4 镜头 × 4 秒，48.00）登记满 20 个；另一 Run 的 `shot_redo` → `EPISODE_COST_LIMIT`（消息含 `jobCap`），Job 仍 20 个、分镜 revision 与上游提交均不变；再次 `episode_generate` 全部 `reused` |
| 金额/旁路 | 12 镜集（36.00）后把一场分镜改成 6 个 15 秒镜头，再 `shot_generate` 这 6 个：36.00 + 54.00 > 60.00 → `EPISODE_COST_LIMIT`，Job 仍 12 个、提交 0 |
| 重做隔离 | 只追加目标镜头的新 ShotVersion/Job；其余分镜 revision 不变、其他 ShotVersion 与 Job 记录逐字段不变；集级用量 13 个 / 39.00；旧 revision 重做 → `VERSION_CONFLICT` 且不写 |
| 集合等待与失败隔离 | 12 镜头批次中 1 个上游 `FAILED`：12 次提交各一次，`resolve_jobset_wait` 与 `await_job` 均返回 succeeded 11 / failed 1，失败项 `JOB_FAILED`、其余 `mediaAvailable=true`；`episode_read` delivered 11 / failed 1、用量 36.00；只重做失败镜头后交付 12/12、提交 13、用量 39.00 |
| 重开恢复 | 关闭并重开后集级 Job 的 `cost.limits` 仍为 `scope=episode`、`episodeId` 与 60.00 上限 |
| 篡改拒绝 | 把某个集级 Job 的快照 `maxNewJobsPerRun` 改成 19（同时递增 revision 以绕过 revision 检查）→ `JOB_CONFLICT`，状态逐字节不变 |
| 等待上限 | `WaitBindingV3` 接受 20 个成员、拒绝第 21 个 |
| 不写分集 | `episode_generate` 前后 `episodes` 集合逐字节相同（集级进度由分镜与 Job 推导） |

### 3.2 既有用例的机械更新

- 工具面版本 8/8 → 9/9：`test_m5_tools.py`（并新增 8/8 冻结视图断言）、`test_m2_clients.py`、`test_m2_compatibility.py`、`test_m2_integration.py`、`test_m3_recovery.py`、`test_live_application.py`、`test_video_integration.py`（并把 rules 变更用例从 `VIDEO_RULES_V8` 改到 `VIDEO_RULES_V9`）、`test_wait_compatibility.py`、`test_m2_generation.py`。
- `RunLimitsSnapshot` 新增 `scope`/`episodeId`：更新 `test_m2_generation.py` 的精确 `limits` 断言与 `scripts/evaluate_m2.py` 的 `batch_echoed` 期望（增加 `scope=run`、`episodeId=null`），行为未变。
- `episode_read` 空结果新增 `generation: null`：更新 `test_m5_tools.py` 的精确字典断言。

### 3.3 门禁

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_m5_generation.py tests/test_m5_tools.py tests/test_m5_episodes.py tests/test_m5_adaptation.py tests/test_m5_consistency.py tests/test_storyboards.py tests/test_storyboard_views.py tests/test_m2_generation.py
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs
```

- 定向：E3 新增 12 项全部通过；连同 M5 工具/域/改编/一致性、分镜与 M2 批量登记共 **128 passed**。
- Ruff：check 与 format 全仓库通过（**161** 个文件）。
- 前端未改；`node --check` 四个脚本与三个状态文件 `node --test` **31 项** 通过。

### 3.4 完整回归

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1215 passed, 3 skipped in 677.10s (0:11:17)
```

- E2 基线为 1203 passed / 3 skipped，本包新增 12 项即为 1215；3 项 skip 仍是既有的“当前 Windows 账户无符号链接权限”用例，未新增 skip。
- 视频 submit=0、模型调用=0：E3 用例的供应商全部是 `httpx.MockTransport` 受控上游，媒体是本地测试 MP4；未读取真实 Key。
- 真实 Wan 账号、真实 DeepSeek 计划与真实账单仍未执行（见第 4 节）。

## 4. 明确未做

- **真实付费生成**：E3 只登记与交付受控上游任务；真实 Wan 集级批次、真实账单与真实失败隔离需要 E4 的单独授权（明确模型、次数与金额）后才能执行。真实 DeepSeek 分集编排同样未执行（模型调用=0）。
- **CLI/Web 分集流程**：E4。CLI/Web 的逐镜头 `redo_shot` 已存在，并因客户端 Run 记录 9/9 而自动受集级额度约束；但还没有 `vagent episode generate` 之类的新命令或 Web 分集面板。
- **整集成片**：M6。M5 仍按 `storyboardId` 单分镜成片，一场一版本；跨场组合成片、字幕、配音与转场不在 E3。
- **Kling/fal 集级批量**：不进入集级额度（不换汇）；它们的镜头仍可走 `shot_generate` 的逐 Run 4 个/12 USD 上限。
- **9:16 竖屏、LoRA/角色微调、声音克隆**：仍不在 M5。

## 5. 失败与偏差记录

- 首轮用自造错误码 `SCENE_UNBOUND` 表示“场记未绑定分镜”，与冻结错误码表冲突；最终改回 `SCENE_CONFLICT`（消息保留“还没有绑定分镜”），不新增错误码。
- `RunLimitsSnapshot` 的字段上界从 `LIVE_JOBS_PER_RUN` 放宽到 `EPISODE_JOB_CAP`，但只对 `scope=episode` 生效；默认 `scope=run` 的旧记录仍校验 ≤4，避免“追溯放宽”。这是让 20 镜集可在单 Run 登记、同时不改变历史语义的最小改动。
- `limits` 结果字典新增两个字段，属加法变化；已机械更新两处精确断言。Web/CLI 的多余字段不影响既有解析。
- 失败隔离测试首轮暴露的是测试夹具问题而非实现缺陷：受控上游最初把未脚本化任务保持为 `PENDING`，Worker 的“每秒一次 live 调用”节流使 12 个任务无法在预期轮次内完成；夹具改为每个任务返回 `SUCCEEDED`（第 1 个返回 `FAILED`）后，12 次提交、11 次交付 + 1 次失败、1 次重做修复全部通过。
- 20 镜集在默认 20 job / 60 CNY 下没有重做余量：这是“跨 Run 累计”的必然结果，已固定为 `EPISODE_COST_LIMIT` 用例；需要重做余量时应先按 12–16 镜生成。
- 本机 workspace-write 沙箱曾拒绝子进程写工作区（连 Ruff 缓存与临时文件都无法创建）。`diagnose-windows-sandbox-acl` 的诊断结果为 `NOT_THIS_CLASS`（无软件包权限项，`WRITE_DAC`/`WRITE_OWNER` 均可用），随后本会话切换到完全权限，Ruff 与完整回归在无沙箱限制下运行；这不是代码缺陷，也没有改动任何文件权限。
- `episode_generate` 的 mock 路径不校验金额（模拟任务没有费用），只校验真实 Job 的集级额度；与 `shot_generate` 的 mock 语义一致，已由 mock 用例覆盖“提交 0”。

## 6. 复现命令

```powershell
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_m5_generation.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
```

E3 的所有用例都不需要 Key、网络或付费任务；`VAGENT_HOME` 只影响本地状态目录。
