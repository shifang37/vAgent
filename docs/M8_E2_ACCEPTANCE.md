# M8 E2 验收：提交节流、退避记录与手工账单

日期：**2026-10-25**。基线 `master` `16102dd`（M8 E1 路由预览已合入；schema 9、tools/rules 18/18、168 个契约模型）。契约见 [M8 契约](./M8_CONTRACTS.md) §4–§5、§7–§9，计划见 [M8 计划](./M8_PLAN.md) §3.3，机器可读基线见 `docs/M8_E0_BASELINE.json`。

E2 交付**供应商运维与账单**：`src/vagent/video/providers/gate.py`（逐 provider 提交间隔、429 冷却、`PROVIDER_BACKOFF` 只写 `runtimeBlock`、统一错误表常量与内存态观测）、`src/vagent/video/billing.py`（`billing-v1` 手工账本侧车文件）、`usage.billing` 汇总、`vagent usage bill record|show`、`POST/GET /api/usage/bill`、`GET /api/usage?runtime=1` 与 Web 用量面板账单区。**未新增 schema、未新增模型工具、未改 9 个已哈希契约模块**；全程视频 submit=0、文本模型调用=0、TTS 请求=0、凭据读取=0。

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 提交闸门 | `src/vagent/video/providers/gate.py` | `SubmitGate`（逐 provider 最小间隔 + 429 冷却，注入时钟）、`GateDecision`、`LatencyWindow`/`RuntimeMetrics`、每 Store 一份的 `gate_for`/`metrics_for` |
| 冻结常量与错误表 | 同上 | `SUBMIT_INTERVAL_SECONDS=2.0`（2.0–60.0）、`RATE_LIMIT_COOLDOWN_SECONDS=60.0`（60–900）、`RETRY_AFTER_CAP_SECONDS=900.0`、`GLOBAL_CALL_INTERVAL_SECONDS=1.0`；`SUBMIT_ERROR_TABLE`/`QUERY_ERROR_TABLE` 把 §5.1/§5.2 的映射写成可断言的数据 |
| Worker 接线 | `src/vagent/video/worker.py` | 提交前查闸门 → 挡住时 `service.block(..., "PROVIDER_BACKOFF", stage="submit")` 并继续扫描其他到期任务；真正提交时 `record_submit`；`PROVIDER_RATE_LIMITED` 时 `record_rate_limit`；submit/query 延迟进内存指标 |
| 新配置项 | `src/vagent/config.py` | `videoSubmitIntervalSeconds`（`VAGENT_VIDEO_SUBMIT_INTERVAL_SECONDS`，默认 2.0，**2.0–60.0**）与 `videoRateLimitCooldownSeconds`（`VAGENT_VIDEO_RATE_LIMIT_COOLDOWN_SECONDS`，默认 60.0，**60–900**）；超范围在保存/启动时拒绝，读入后再按区间收紧 |
| 账单账本 | `src/vagent/video/billing.py` | `BILLING_VERSION=1`、`BILLING_FILE="billing.json"`、`BillRecord`/`BillLedger`（`Contract` 子类，**不进 9 个已哈希模块**）、`record_bill`/`read_ledger`/`summarize_billing`/`ledger_view` |
| 用量汇总 | `src/vagent/video/usage.py` | `billing` 块追加在既有结构之后（`cost` 仍是 CNY 历史口径）；`runtime` 块只在显式请求时出现 |
| 本地动作 | `src/vagent/application.py` | `usage(project_id, runtime=…)`、`record_bill(request)`、`bills(project_id, currency)`；账本动作永不调用供应商 |
| CLI | `src/vagent/cli.py` | `vagent usage bill record --job … --amount … --currency … --source … [--invoiced-on …] [--note …] [--replace --expected-revision N] --confirm`、`vagent usage bill show [--project …] [--currency …]`；`vagent usage --project` 增加 `billing`/`runtime` 块 |
| HTTP | `src/vagent/web.py` | `POST /api/usage/bill`（`confirm=true` + CSRF，返回 201）、`GET /api/usage/bill?projectId=&currency=`、`GET /api/usage?runtime=1`；`BILL_CONFLICT` 映射 409 |
| Web | `web/bill-state.js` + `web/app.js` + `web/styles.css` | 用量面板新增「供应商账单」区：逐币种估算/已录入/差额、「未录入（未知，不是 0）」、已录入记录与录入表单；写入只走 `POST /api/usage/bill` |
| 测试 | `tests/test_m8_gate.py`（12）、`tests/test_m8_gate_worker.py`（5）、`tests/test_m8_billing.py`（10 + 1 skip）、`tests/test_m8_bill_api.py`（6）、`tests/test_m8_bill_cli.py`（4）、`tests/test_bill_state.mjs`（7） | 新增 **37 项后端**（+1 项 POSIX-only skip）与 **7 项前端**，全部离线 |

## 2. 退避：只延后「还没发出去的调度」

契约 §5.3 的「任何发送后的自动重投都不做」逐字保留。E2 只做一件事：**提交前**若逐 provider 间隔或 429 冷却未到期，就保持 `pending_submit`、不写 `submitAttempts`、不发请求，并把这次等待写成一条诊断。

`test_a_proven_429_cools_down_the_provider_and_never_resubmits`（受控 Kling 429 + `Retry-After: 120`）与 `test_the_submit_interval_delays_the_second_job_of_a_provider`（受控 401）逐项核对：

| 事实 | 断言 |
|---|---|
| 429 后 Job 是终态 `failed`、`submitAttempts=1`、无上游 ID | `PROVIDER_RATE_LIMITED`、`httpStatus=429`、`providerTaskId=null`、请求数 1 |
| 冷却由「已证明的限流」触发，且按 provider 生效 | 同一时刻 `kling` 冷却中、`wan`/`fal` 仍可提交 |
| `Retry-After` 只延长、不缩短，超过 900 秒按上限截断 | 120 秒窗口覆盖 60 秒默认值；5000 秒被截断为 900 秒且 `message` 注明 |
| 被挡住时不消耗唯一的提交 | 状态仍 `pending_submit`、`submitAttempts=0`、`submissionStartedAt=null`、`providerTaskId=null`；`state.json` 只有 `runtimeBlock`/`revision`/`updatedAt` 变化 |
| 诊断可持久读取，下一次真正提交时清空 | `runtimeBlock={stage:"submit", code:"PROVIDER_BACKOFF"}` → 提交后 `runtimeBlock=null` |
| 间隔按「上一次真正提交」计算，且逐 provider | 1.5 秒时挡住（诊断写明「间隔 2 秒」与最早可提交时刻，`remainingSeconds≈0.5`），2.0 秒后放行；全局 1 秒间隔仍由既有 `_store_next_calls` 负责 |
| 查询侧 429 不打开提交冷却 | `test_a_query_side_429_never_opens_a_submit_cooldown`：`retry_after_at` 只进 `nextPollAt`，闸门仍允许提交 |
| 终态 Job 不会被重投 | `test_a_failed_job_still_consumes_the_single_recorded_submission`：300 秒后 `run_once()` 返回 `None`、请求数仍为 1 |

**冷却与间隔是内存态**（每 Store 一份，与既有 `_store_next_calls` 同构）：重启只丢「再等一会」，不影响「未知永不重投」。因此 M1-C 的「每个 Job 至多一次提交」没有变化。

## 3. 账单：侧车文件、原子替换、不换汇

`billing.json` 与 `state.json` 同级，权限 0600（POSIX），**一条记录 = 一次临时文件 + `os.replace`**，并在 FileStore 实例锁内执行。它不进 `DatabaseV9`，所以 schema 仍是 9、168 个模型哈希不变；`JobV6.cost.actual` 也继续是冻结的 `unknown`。

| 场景 | 结果 |
|---|---|
| 首次录入合法记录 | 写入，`recorded=true`、`replayed=false`、`revision=1` |
| 完全相同（业务字段）重放 | 不写盘，`replayed=true`；`recordedAt` 不同不构成冲突 |
| 金额/币种/日期/来源/备注任一不同 | `BILL_CONFLICT`，原记录保留 |
| `--replace --expected-revision N` | `revision=N+1`；`N` 过期 → `BILL_CONFLICT`；缺少 `--expected-revision` → `BILL_RECORD_INVALID` |
| Job 不存在、在途、无冻结报价（模拟任务）、币种与冻结报价不符、字段非法、条数/大小超限 | `BILL_RECORD_INVALID`，不写盘 |
| 账本 JSON 损坏 | 录入 `INVALID_STORE` 且原文件逐字节保留；`show`/`usage` 报 `ledgerStatus="invalid"` 并按 `unknown` 处理 |

汇总（`usage.billing`）逐币种独立：`estimate` 来自冻结报价，`reported` 来自账本，`delta = reported − estimate`；**没有记录时 `reported`/`delta` 是 `null` 而不是 0**，`unknownJobs` 照实计数，跨币种不生成合计，`notes` 固定写明「不换汇」。`test_summary_keeps_currencies_separate_and_unknown_is_not_zero` 用一条 kling 0.56 USD 与一条 wan 3.00 CNY 核对这一形状，并断言 `cost.actual` 仍是 `unknown`、Job 记录里没有任何 `billing` 字段。

## 4. 观测

| 指标 | 来源 | 持久性 |
|---|---|---|
| 逐 provider Job 数/状态分布、429 次数（`job.error.httpStatus==429`） | Job 记录 | 持久（既有） |
| 提交/查询延迟（最近 64 个样本 p50/p95）、`backoffCount`、`rateLimitHits` | `RuntimeMetrics`（每 Store） | **内存**，重启归零；`GET /api/usage?runtime=1` 暴露，块内固定 `"durable": false` |

`test_usage_reports_runtime_metrics_as_non_durable` 与 HTTP 用例核对：默认 `/api/usage` 没有 `runtime` 键；`?runtime=1` 才有，且样本数、`backoffCount`、`rateLimitHits` 与受控时序一致、`note` 明确「重启归零，不进入账单，也不作为验收结论」。

## 5. 三个面与配置

```powershell
# 录入一条已核对的供应商扣费（必须显式确认）
vagent usage bill record --job JOB_ID --amount 0.56 --currency USD `
  --source "Kling 控制台 2026-10-25 账单" --invoiced-on 2026-10-25 --confirm

# 更正：显式替换并核对当前版本
vagent usage bill record --job JOB_ID --amount 0.60 --currency USD `
  --source "Kling 控制台 2026-10-26 更正" --replace --expected-revision 1 --confirm

# 只读：账本记录与逐币种汇总（不换汇）
vagent usage bill show [--project coffee] [--currency USD]
vagent usage --project coffee        # 追加 billing 与 runtime 块
```

```text
POST /api/usage/bill      {"jobId":"…","amount":"0.56","currency":"USD","source":"…","invoicedOn":"2026-10-25","confirm":true}
GET  /api/usage/bill?projectId=&currency=
GET  /api/usage?runtime=1
```

新增配置项（本地设置只能延长，超出区间在保存/启动时拒绝）：

| 配置 | 环境变量 | 默认 | 区间 |
|---|---|---|---|
| `videoSubmitIntervalSeconds` | `VAGENT_VIDEO_SUBMIT_INTERVAL_SECONDS` | 2.0 | 2.0–60.0 |
| `videoRateLimitCooldownSeconds` | `VAGENT_VIDEO_RATE_LIMIT_COOLDOWN_SECONDS` | 60.0 | 60–900 |

Web：用量面板底部新增「供应商账单」。

- 逐币种一行：估算、已录入、差额、`n 条已录入 · m 个未录入`；未录入显式显示「未录入（未知，不是 0）」。
- 已录入记录列出日期、供应商、金额与币种、Job ID、来源；第 2 版以上标注「第 N 版」。
- 录入表单（Job ID/金额/币种/来源/日期/备注）先做本地校验，再 `POST /api/usage/bill`；失败按错误码提示，`BILL_CONFLICT` 明确要求显式替换。
- 面板只读 `/api/usage`、`/api/usage/bill` 并写账本；不登记 Job、不改 `state.json`、不调用供应商。

任务卡（§5.5）：被闸门挡住的 Job 显示为**「等待节流」**并用中性样式（不再归入「需要处理」的红色错误），正文直接给出 `runtimeBlock.message` 里的最早可提交时刻；Job 仍是 `pending_submit`、提交次数仍为 0，因此不会与「配置错误」混淆。`tests/test_m8_bill_api.py` 对服务的 `app.js` 断言了「等待节流」「PROVIDER_BACKOFF」与账单动作，防止这条渲染规则被无意删掉。

## 6. 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_m8_gate.py tests/test_m8_gate_worker.py tests/test_m8_billing.py tests/test_m8_bill_api.py tests/test_m8_bill_cli.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/test_bill_state.mjs
.\.venv\Scripts\python.exe scripts/capture_m8_baseline.py
```

| 项目 | 结果 |
|---|---|
| E2 定向后端用例 | **37 passed / 1 skipped**（`test_m8_billing.py` 的 0600 权限用例在 Windows 上跳过），其中 `test_m8_gate.py` 12、`test_m8_gate_worker.py` 5、`test_m8_billing.py` 10、`test_m8_bill_api.py` 6、`test_m8_bill_cli.py` 4 |
| M8 全部后端套件（E1+E2） | **99 passed / 1 skipped**（E1 的 63 项 + E2 的 37 项 + 1 项 skip） |
| 新增前端用例 | **7 passed**（`bill-state.js` 的币种行、差额符号、摘要、记录行、写入体、校验与路径） |
| 前端套件 | **10 模块 91 项**全部通过（E1 为 9 模块 84 项） |
| Ruff | `ruff check src/vagent tests scripts` 通过；`ruff format --check` **229** 个文件已格式化（E1 为 222，本包新增 `gate.py`、`billing.py`、`bill-state.js` 与 5 个测试文件） |
| 全量回归 | **1509 passed / 4 skipped**（1372.59 秒，0:22:52；E1 为 1472/3，本包新增 37 项后端用例与 1 项 POSIX-only skip），4 项 skip 均为既有的 Windows 符号链接/文件权限用例 |
| 基线 | `docs/M8_E0_BASELINE.json` 重新生成后 SHA-256 仍为 `f3dc91c1…b3706`；168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希与 `routeProbe.rowsSha256` 全部不变 |
| 零花费 | 全部供应商调用均为受控 `httpx.MockTransport`；账单动作不读凭据、不建 provider 连接、不登记 Job；CLI/HTTP 用例核对 `state.json` 字节未变 |

> 取证环境说明：全量回归在本机 Windows 上以 `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rf --basetemp=tmp\pt-full3`（`PYTHONPATH=tests`）复跑，**1509 passed / 4 skipped、退出码 0**，没有忽略或用例筛选。同一套用例在受限的 workspace-write 沙箱下分区运行时，出现过一批**子进程类**失败（CLI 子进程、FFmpeg 拼接与媒体 Worker 无法创建自己的临时目录），与产品代码无关；放开的进程内一次全绿。

## 7. 与 M8 契约的差异说明

契约 §8 允许改动的文件是「新增模块 + `application.py`/`cli.py`/`web.py`/`jobs.py`/`worker.py`/`video/usage.py`/Web 前端与文档」。E2 另有四处必须说明的改动，均**不涉及**9 个已哈希契约模块、schema、工具面、规则字节或任何基线哈希：

1. **`src/vagent/config.py`**：契约 §5.4 明确要求两个**新配置项**（逐 provider 提交间隔与 429 冷却），实现必须落在 `Config`/`LocalSettings` 里；两个字段只在区间内可调，`Config.__post_init__` 的既有校验路径会把越界值拒绝为 `VIDEO_CONFIG_INVALID`。
2. **`src/vagent/video/providers/kling.py`、`fal.py`**：两个适配器原先在 submit 分支丢弃了已解析的 `Retry-After`（`status, payload, _ = …`）。E2 把它一并交给 `ProviderCallError`，使冷却可以「只延长、不缩短」并受 900 秒上限约束。改动只有 **1 处解包 + 1 个参数**：错误码、`submission_outcome`、状态机与重投语义逐字不变。
3. **`tests/test_live_jobs.py::test_live_worker_serial_spacing_is_shared_by_owners`**：该用例原先断言「同一 Store 的两个 live 提交相隔 1.0 秒即可开始」，这正是 §5.4 用 **2.0 秒逐 provider 间隔**取代的旧口径。E2 把它改成：1.0 秒时全局间隔已满足、逐 provider 闸门仍挡住并写出 `PROVIDER_BACKOFF`（不消耗提交次数），2.02 秒后第二个 Job 才提交，并断言两次请求的时间差 ≥2 秒。这是**契约要求的行为变化**，不是放宽断言。
4. **`tests/…` 其余改动与 Web 前端**属于验收与展示层（新增 5 个测试文件、`bill-state.js`、`app.js` 账单区、`styles.css`、`film_support.py` 的注释），不影响任何冻结字节。

## 8. 明确未做

- **不重投**：`not_accepted`（429/4xx/业务拒绝）、`SUBMISSION_UNKNOWN` 与连接失败都不自动重投；恢复仍走显式 `shot_redo`/`episode_generate`（新 Job、新估算）。
- **不抓取账单**：没有供应商账单 API 的自动拉取，也不把 `usage` 字段当扣费；`unknown` 绝不计 0。
- **不换汇**：没有汇率、没有跨币种合计，也不做「按价格自动换供应商」或「失败后换 provider 重试」。
- **不改冻结面**：schema 9、tools/rules 18/18、168 个模型哈希、`capabilitiesSha256`、`routeProbe.rowsSha256` 全部不变；E2 没有新增模型工具（账单与节流都是本地动作）。
- **无真实样本**：E2 全程没有读取真实 Key、没有真实供应商请求；Kling/fal/CosyVoice 仍标注「文档支持，未做真实样本」，E3 的真实样本需单独授权。
- **不预测额度**：闸门只管「现在能不能开始提交」，不预测 Run 剩余额度、集级/批量余量或并发配额。

## 9. E3 入口

按 [M8 契约](./M8_CONTRACTS.md) §9 的 E3 与 [M8 计划](./M8_PLAN.md) §5 的清单执行**需单独授权**的真实样本：Kling 5 秒 720P 1 条（0.56 USD）、fal 5 秒 1 条（0.42 USD）、CosyVoice 1 集（≤0.40 CNY）、真实 DeepSeek 编排 1 Run（<2 CNY）。每条留证任务 ID、结果主机、字节数、SHA-256、时长/编码、播放与 Range、账单或 `unknown`；失败不重试、不换 provider、不改冻结表。E2 交付后 submit 仍为 0，`docs/M8_E0_BASELINE.json` 继续作为 E3/E4 的比对基准。
