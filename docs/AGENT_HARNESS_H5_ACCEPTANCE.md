# 可观测性（tracing）验收（H5）

日期：2026-11-05。范围：Agent Harness 落差收口计划的 H5 轨道——给 Run 生命周期补 span 观测，贯穿 context / model / tool / wait，并保留现有事件流。零新增依赖、默认关闭；关闭时构造不出 span、不写文件、行为与今日一致。

## 决策

- **零依赖，自建 span 模型，导出到本地 JSONL 旁路文件。** 计划原文许可「接 OTel **或等价 span 导出**」。引入 `opentelemetry-sdk` 会为一门默认关闭的可选能力给全部读者和 CI 增加依赖，而真正需要的是字段语义与落点。因此字段直接采用 OTel 命名（`traceId` / `spanId` / `parentSpanId` / `startTimeUnixNano` / `endTimeUnixNano` / `attributes` / `status`），将来要接 collector 只换 exporter，不用改埋点。
- **观测不参与控制流。** 每个 span 都在被观测的工作**已提交其真实效果之后**才写出；导出异常一律吞掉（有用例断言「导出必炸的 exporter 也不影响 Run 成功」）。这是「默认关闭不改变行为」的更强版本：即使打全开且磁盘满，Run 结果也不变。
- **关闭即惰性。** `AgentRunner.tracing is None` 时所有埋点走 `NullSpan`，不创建 span 对象、不读文件、不写文件；用例断言关闭时不出现 `traces/` 目录，且「关」与「开」两次同脚本运行的**事件类型序列完全一致**、`contextSignature` 相同。
- **traceId 由 Run id 派生，恢复不换 trace。** `traceId = sha256("run:"+runId)[:32]`，因此显式恢复仍在同一条 trace 同一个文件里；恢复执行的根 span 以**上一次执行的根 span 为父**，形成 `run₀ ← run₁ ← run₂` 的链，而不是一条任务被劈成两条 trace。
- **内容默认不采集，采集则必脱敏。** 默认只记结构（谁、多久、成功与否、token 数）；`VAGENT_TRACE_CONTENT=on` 才记 prompt / 模型回复 / 工具参数。无论开不开，属性都过同一套脱敏：键名像凭据（`api_key` / `token` / `authorization`…）整条丢弃，值像凭据（`sk-…`、`Bearer …`、32+ 位十六进制、40+ 位 base64）就地替换为 `[redacted]`，超长文本截断并标注原长度。
- **采样按 trace 决策，不按 span。** `VAGENT_TRACE_SAMPLE=0..100`，用 traceId 前 8 位取模，一条 Run 要么整条被采、要么一条都不写，避免出现残缺调用链。
- **时长用单调高分辨率时钟，时间戳用墙钟。** 第一次实测就撞上：Windows 的墙钟粒度粗到「两次取样相同」，`context.build` 一律读成 `0 ns`。改为 `startTimeUnixNano`/`endTimeUnixNano` 仍取墙钟、`durationNano` 取 `perf_counter_ns` 差值后，同一段构建读到 **0.3–0.7 ms**。这是「先量、发现数字不对、再修」的又一次应用：假 0 比没有数字更有害。
- **采样/内容/开关全部走环境变量，签名不变。** tracing 不进入 `contextSignature`：它不改变模型输入，若进签名会让「开个观测」拒绝旧 Run 恢复。

## span 结构

| span | kind | 父 | 关键属性 | 何时写 |
|---|---|---|---|---|
| `run` | internal | 上次执行的根（首次为空） | runId、sessionId、model、contextVersion、executionVersion、resume、maxSteps、maxToolCalls、runStatus、modelSteps、toolCalls、inputTokens、outputTokens、droppedMessages | 每次执行结束（含失败/挂起） |
| `context.build` | internal | `run` | contextVersion、summarize、historySummary、memoryRecords、inputBytes、droppedMessages | 每次构建上下文 |
| `context.summarize` | internal | `run` | droppedMessages、contextVersion、applied、summaryBytes | 真正触发摘要时 |
| `model.call` | client | `run` | model、step、streamed、inputTokens、outputTokens、durationSeconds（内容开启时 prompt/reply） | 每次真实模型调用 |
| `tool.call` | internal | `run` | tool、callId、step、ok、errorCode、jobId、deferred、waitId（内容开启时 arguments） | 每次工具执行 |
| `wait.suspend` | internal | `run` | waitId、resourceKind、autoResume | 挂起外部等待时 |
| `wait.external` | internal | 恢复执行的 `run` | waitId、resumed（explicit/automatic）、waitedSeconds | 恢复时按持久化的开始时间**回溯**写入 |

`model.call` 的 `inputTokens` / `outputTokens` 与 `model.usage` 事件取自同一条 `modelCalls` 记录，用例逐项比对——trace 与既有用量口径不会各说各话。

`wait.external` 是唯一不能用「开-关 span」测量的边界：等待跨进程、可能持续数小时，进程会退出。因此它在恢复时用持久化的 `externalWaitStartedAt` 作为起点、用与持久化同一时钟的当前时间作为终点回溯写入；两端同源，注入时钟的夹具看到的时长与 `externalWaitSeconds` 一致（用例断言 ≥ 90s）。

## 实测（离线 demo，零费用）

`VAGENT_TRACING=on vagent demo` 跑一遍内置离线演示（`DemoModel`，不联网、不调付费模型），产出**一个 trace 文件、12 条 span**：

| span | 条数 | 时长（单条） |
|---|---|---|
| `run` | 1 | 257 ms（整条 Run） |
| `context.build` | 4 | 0.30–0.70 ms |
| `model.call` | 4 | 2.8–3.7 ms |
| `tool.call` | 3 | 3.6–3.9 ms |

四步模型 + 三次工具的全部工作都落在 257 ms 内，span 之和与根 span 同量级，父子关系为「全部挂在 `run` 下」。文件形如：

```
{"attributes":{"contextVersion":2,"inputBytes":7977,...},"durationNano":304000,
 "endTimeUnixNano":…,"kind":"internal","name":"context.build","parentSpanId":"737c8859468a4463",
 "spanId":"e8c1691b56d14e20","startTimeUnixNano":…,"status":"ok",
 "traceId":"b9544aaba46e2d4dbbcfe9117e5a9348"}
```

## 与计划对照（H5 验收判据）

| 判据 | 落地 | 证据 |
|---|---|---|
| 离线 collector 捕获 span | `InMemoryExporter`（测试/自评）与 `JsonlExporter`（`<home>/traces/<traceId>.jsonl`） | `test_a_real_run_writes_a_jsonl_sidecar`、`test_jsonl_exporter_writes_one_file_per_trace` |
| 父子关系正确 | 七类 span 的父子关系；非根 span 逐级回溯必达 `run` | `test_spans_share_one_trace_and_nest_under_the_run` |
| 脱敏 | 键名整条丢弃 + 值就地替换 + 长度截断；内容采集为显式开关 | `test_secret_shaped_values_and_keys_are_redacted`、`test_failed_run_records_an_error_span`、`test_content_capture_is_opt_in` |
| 默认关闭不改变行为 | 关闭不建目录、不建 span；事件类型序列与开启时一致；签名相同 | `test_tracing_off_writes_nothing_and_keeps_events_identical` |
| 恢复后 span 连续 | 两次执行同一 traceId，`run₁.parentSpanId == run₀.spanId` | `test_resume_continues_the_same_trace`、`test_tracing_records_the_wait_lifecycle` |
| 与 usage 事件对齐 | span 的 token 数 == `model.usage` 事件里的 token 数 | `test_model_span_usage_matches_the_usage_events` |
| 采样 | 0 全关、100 全开、按 trace 确定性 | `test_sampling_is_deterministic_and_can_disable_tracing` |
| 观测不改变结果 | 导出必炸的 exporter 下 Run 仍 `completed` | `test_an_export_failure_never_breaks_the_run` |
| 默认关闭（配置） | `VAGENT_TRACING` 缺省 off；非法采样百分比拒绝 | `test_config_tracing_defaults_off_and_parses_the_switch`、`test_config_rejects_an_invalid_sample_percent` |
| 应用接线 | 关闭时 `runner.tracing is None`；开启时按配置构造 | `test_application_wires_tracing_only_when_enabled` |

## 覆盖边界（如实记录）

- **Job 不产生独立 span。** 视频 Job 在 worker 进程里跑，跨进程/跨 Run，本次只把 `jobId` 记在 `tool.call` 上，让 trace 能指向 Job；Job 侧 span 需要 worker 级 tracer，留作后续。
- **未完成的 span 不落盘。** span 在结束时写出（OTel 也是这个语义），进程崩溃时正在进行的 span 会丢；不补写「猜测的」结束时间。这是刻意的：宁可少一条 span，也不写假数据。
- **没有 OTLP / collector 传输。** 只写本地 JSONL；字段即 OTel 形状，接 collector 时只需新增一个 exporter 类（`export(span)` + `previous_root(traceId)`）。
- **不采样 span 内部事件。** 只记 span 与属性，不做 span events / links / baggage。
- **`context.summarize` 的 cached 标记未记。** 摘要是否命中侧车缓存在事件流里有（`context.summary_reused`），span 只记 `applied` 与 `summaryBytes`，避免两处口径不一致。

## 兼容与边界

- 新增环境变量：`VAGENT_TRACING=on`、`VAGENT_TRACE_SAMPLE=0..100`（默认 100）、`VAGENT_TRACE_CONTENT=on`（默认 off）。非法采样值抛 `INVALID_TRACE_SAMPLE`。
- 旁路文件在 `<home>/traces/`，不写 `state.json`，不改 schema、不改 168 个模型哈希、不进 `contextSignature`。
- 关闭时零额外文件与零额外事件；打开后事件流**不变**（tracing 是并列通道，不替换 `on_event`）。
- 读取已有 trace 找「上一次根 span」是唯一的新读操作，发生在 `for_run` 且仅当开启 tracing；文件损坏时降级为「无父」，用例覆盖。

## 自动化验证

`tests/test_tracing.py`（新增，17 项）+ `tests/test_wait_runtime.py::test_tracing_records_the_wait_lifecycle`（1 项，复用既有 durable wait 夹具）：

- 默认关闭 1 项、span 树/侧车 3 项、用量对齐 1 项、失败与脱敏 3 项、exporter 3 项、采样 1 项、接线 2 项、配置 2 项、模块纪律 1 项、等待生命周期 1 项。

复现（离线，无网络、无付费模型调用）：

```
python -m pytest tests/test_tracing.py tests/test_wait_runtime.py -q

VAGENT_HOME=tmp/h5-demo VAGENT_TRACING=on python -m vagent demo
# 产物：tmp/h5-demo/traces/<traceId>.jsonl（一次 demo = 一个文件 12 条 span）
```

回归：全量 `pytest tests/` **1605 passed, 4 skipped, 1 failed**（30 分 30 秒；跳过项为本机无符号链接权限的既有用例）。唯一失败是 README「已知限制」里早已记录的 M7 顺序偶发——`tests/test_m7_timeline_render.py::test_a_full_timeline_render_freezes_the_edit_and_replays`，混音被判定为静音而未交付；单跑该用例 **1 passed**、单跑整个文件 **6 passed**，与本轨道代码无关（本轨道只碰 runner / tracing / config / application，未触及混音路径），按项目纪律记录而不掩盖。除该偶发外的 1605 项全部通过，其中本轨道新增 **18 项**（`tests/test_tracing.py` 17 + `tests/test_wait_runtime.py` 1）。`ruff check` / `ruff format --check src/vagent tests scripts`（CI 口径）全绿。本轨道未调用任何付费模型、未联网（demo 用内置离线 `DemoModel`）。
