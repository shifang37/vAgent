# vagent Harness 缺陷修复规划

> 日期：2026-11-06
> 输入：`tmp/harness-review-report.md`（审查同日，基线 `aaa0c6d`，v0.6.1）
> 性质：**规划文件，不含代码改动**。每条修复给出根因、方案、改动面、测试与验收判据。
> 核对方式：逐条重读了报告点名的代码路径（行号以当前 `aaa0c6d` 为准），未重跑报告里的复现实验；基线只跑了两组相关用例，`1624` 项可收集、受影响四文件 `48 passed`。
> 不变式：`168` 个契约模型哈希、工具面/规则版本、`state.json` 形状（只允许追加可选字段或旁路文件）、v1/v2/v3 恢复路径。

## 0. 结论

- 报告共 **30 条**具体问题（新发现 26 条 + R1–R4），全部纳入本计划，另列 5 条能力缺口为独立专项。
- 先修 **7 条 P0**：其中 3 项针对"永久卡死 / 丢掉已付费任务"（高#1、中#3），4 项针对"丢掉已完成的回答或交付说明"（中#4、中#5、中#6、中#8）。
- 再修 **8 条 P1**：R1/R2/R3/R4 四条回归 + 上下文前缀缓存（中#10）+ 密钥识别（中#11）+ MCP ReDoS（中#9）+ 用量口径（中#7）。
- 评测可信度（高#2、中#14、低#26）与 5 处文档不实说法单列一批，**文档与代码同批交付**。
- 低优先级 12 条按"数据增长 → 旁路写安全 → 观测细节 → 加固"顺序进 v0.7.0，不与止血混在一起。
- 有一条需要你决策：单次模型调用 60 s 上限（低#20）是保留还是改成策略项；本计划默认"保留 + 写进文档"。

| 批次 | 目标 | 内容 | 工作量 | 版本 |
|---|---|---|---:|---|
| M0 | 止血 | 7 项 P0：不卡死、不丢已付费任务、不丢回答 | ~8 人日 | v0.6.2 |
| M1 | 正确性 | R1–R4 + 上下文/密钥/MCP/用量 | ~11 人日 | v0.6.3 |
| M2 | 评测与文档 | 行为评测可信化、只读脚本、文档补齐 | ~3 人日 | v0.6.3 |
| M3 | 健壮与性能 | 12 条低优先级 + 状态增长 | ~12 人日 | v0.7.0 |
| M4 | 能力专项 | 意图对账、数据保留、监督自检等 | 按需 | 待排 |

## 1. 修复原则

1. **先止血，再正确，最后整洁。** M0 只做"不丢钱、不卡死、不丢已完成的回复"，不做重构、不动布局。
2. **复现先行。** 每条先落一个离线失败用例（零费用、可重复），再改代码；报告里标"只追代码"的两处（中#5 TIMEOUT 分支、低#24）先补复现。
3. **契约只增不改。** 新字段一律带默认值；能放旁路文件就不进 `state.json`；`RunPolicy` 这类内存参数不落盘。
4. **失败必须降级。** 摘要、记忆、追踪、评测报告、回答缓存都是优化项：坏了就退，不允许让 Run 失败。
5. **默认离线零费用。** 所有验收离线完成。唯一需要真实调用的探针是上下文 v4 的尾消息角色（1 次低成本调用），须显式授权并记账。
6. **文档与代码同批。** 报告点名的 5 处不实说法（`CONTEXT_SUMMARY_ACCEPTANCE.md:10`、`H3:26`、`H0:24`、`H6:12`、`README.md:230`）随对应批次修正，不留尾巴。
7. **兼容优先于优雅。** 容易改变既有基线行为的能力（收尾轮、上下文 v4）用开关控制：库默认关，产品路径开；已有评估基线不因本轮修复而漂移。

## 2. 缺陷 → 修复项总表

严重度沿用报告（高/中/低）；"量"为 S（≤0.5 天）/ M（0.5–2 天）/ L（2–5 天）。

| ID | 报告 | 严重度 | 批次 | 量 | 一句话 |
|---|---|---|---|---|---|
| FX-01 | 高#1 | 高 | M0 | M | 等待扫描遇到永久阻塞的 Run 不再停下，后续 Run 正常自动恢复 |
| FX-02 | 中#3a | 中 | M0 | S | Windows 原子写按 WinError 5/32 有界重试，并给出可读错误码 |
| FX-03 | 中#3b | 中 | M0 | M | 提交句柄预写 WAL；僵尸 `running` 自愈；纯文本 Run 可停止 |
| FX-04 | 中#4 | 中 | M0 | M | 预算耗尽时保证一次不带工具的收尾轮，保留产物说明 |
| FX-05 | 中#5 | 中 | M0 | S | 回答缓存写入移出取消边界，即发即弃 |
| FX-06 | 中#6 | 中 | M0 | S | `finish_reason` / 工具名/id 取最后一个非空值，不靠字符串拼接 |
| FX-07 | 中#8 | 中 | M0 | M | 畸形工具调用回一条 `INVALID_TOOL_CALL` ToolMessage，反馈一次 |
| FX-08 | R1 | 高 | M1 | S | Schema 反馈上限按 Run 重置 `batches` |
| FX-09 | R2 | 高 | M1 | M | 摘要降级与 v2 逐字节等价；事件/span 只在真正采用时记 applied |
| FX-10 | R3 | 高 | M1 | M | 记忆取代改为"当前事实集差集"，不再误杀同类兄弟事实 |
| FX-11 | R4 | 中 | M1 | M | 淘汰按时间序、优先本项目/已取代；单步只解析一次侧车 |
| FX-12 | 中#10 | 中 | M1 | L | 上下文动态块（预算/记忆/摘要）移到历史之后，恢复前缀缓存 |
| FX-13 | 中#11 | 中 | M1 | S | `looks_secret` 用 `(?<![A-Za-z0-9])` 并补中文/ JWT / Bearer |
| FX-14 | 中#9、低#16 | 中 | M1 | M | MCP Schema 拒绝 `pattern`/`patternProperties`；结果 NaN/$ref 降级为工具错误 |
| FX-15 | 中#7 | 中 | M1 | S | 失败但已计费的调用补齐 span 用量与 `model.usage`；根 span 只记增量 |
| FX-16 | 高#2、中#14、低#26 | 高/中 | M2 | M | 行为评测：k≤样本数、按 pass@1 判定、评分器四条件负样本、二部匹配 |
| FX-17 | 中#12 | 中 | M2 | S | `compare_cost.py` 改真只读快照；目录不存在直接报错 |
| FX-18 | 中#13、低#26 | 中 | M2 | S | 7 个开关、3 个命令、已知限制写进 README/USER_GUIDE/.env.example |
| FX-19 | 低#26 | 低 | M2 | S | 文档门禁用例 + 开关的 Application 端到端用例 |
| FX-20 | 低#15 | 低 | M3 | L | `state.json` 增长：事件封顶、终态 Run 保留策略、`vagent gc` |
| FX-21 | 低#17、低#18 | 低 | M3 | M | 侧车用原子写且失败隔离；摘要缓存键含目标预算 |
| FX-22 | 低#19 | 低 | M3 | S | 有未跟踪模型步时成本标 `partial` 并提示 |
| FX-23 | 低#21 | 低 | M3 | S | 传输层流中断报 `STREAM_INTERRUPTED`，保住已收用量 |
| FX-24 | 低#22 | 低 | M3 | M | TTS 下载对齐视频策略：https、固定主机、443、公网 DNS |
| FX-25 | 低#23 | 低 | M3 | S | `Span.set()` 也按属性名脱敏 |
| FX-26 | 低#24 | 低 | M3 | M | 摘要移出 system 特权位置（随 FX-12）；预算紧张时丢弃记忆块而非失败 |
| FX-27 | 低#25 | 低 | M3 | S | `BibleSave` 字符串字段加长度上限 |
| FX-28 | 缺口/低 | 低 | M3 | L | 锁活性检查、最后正常快照、traces 轮转、同名重复调用提示等 |
| FX-29 | 低#20 | 低 | 待决策 | S | 单次模型 60 s 上限：保留并写文档，或改为策略项 |
| C1 | 缺口#1 | — | M4 | L | 付费副作用意图 + 供应商能力矩阵 + `job reconcile` |
| C2 | 缺口#2 | — | M4 | L | 数据保留与压缩（Run 消息旁路、fsck） |
| C3 | 缺口#3 | — | M4 | — | 收尾轮策略化（FX-04 已有基础版） |
| C4 | 缺口#4 | — | M4 | L | 真模型参与的行为评测模式（A/B、pass@k 置信区间） |
| C5 | 缺口#5 | — | M4 | M | 后台循环监督：退避重启 + `wait/checkpoint/operation` 自检 |

## 3. M0：止血（v0.6.2）

### FX-01　被阻塞的 Run 不再饿死后续 Run
- **报告**：高#1，`src/vagent/wait_runtime.py:448-470`
- **根因**：`run_once` 命中的第一个满足条件的 Run 不论成败都 `return`；`block_auto` 只写 `waitResumeError`，Run 仍是 `interrupted`/`waiting_external`，下一轮扫描仍满足条件，于是永远卡在它身上。
- **方案**：
  1. 区分**永久阻塞**与**暂态阻塞**。永久集合初版 `{EXPLICIT_RESUME_REQUIRED, STALE_RUN}`；其余（`KEY_MISSING`、`RESUME_CONFIG_CHANGED`、`NO_CHECKPOINT`、`TIMEOUT`、`CONFIG_BUSY`）按指数退避重试。
  2. 扫描改为"逐个尝试，跳过永久阻塞与退避未到期的 Run"，不再在第一个阻塞处返回；`run_once` 返回值语义不变（本次推进的 Run，无推进返回 `None`）。
  3. 每个 Run 维护 `_retry_at[run_id]`：1→2→4→…→60 s 封顶；`notify()`、显式 resume、交付新结果时清零。
  4. `STALE_RUN` 终态化：停掉等待，Run → `cancelled`、`errorCode=STALE_RUN`、`resumable=False`，记 `run.superseded` 事件，不再重试。
- **改动面**：`wait_runtime.py`（`run_once`、`block_auto` 旁加分级表）；`tests/test_wait_runtime.py`、`tests/test_wait_recovery.py`。
- **兼容**：无 schema 变更；`waitResumeError` 仍是"最近一次阻塞原因"，只是不再驱动重复尝试。
- **测试/验收**：
  - `test_scan_skips_explicit_resume_blocked_run_and_advances_later_run`（跨会话 A 阻塞、B 就绪）
  - `test_scan_skips_blocked_run_and_advances_same_session_successor`（同会话，断言旧 Run 终态为 `cancelled/STALE_RUN`）
  - 改 `test_wait_recovery.py:75`：第二次扫描应为 `None`（现断言"每次都返回被阻塞的 Run"）
  - `test_transient_blocker_retries_after_backoff_window`（推进 61 s 后重试）
- **风险**：退避可能让"刚修好配置"的 Run 延迟最多 60 s；用 `notify()` 即时唤醒兜底。

### FX-02　Windows 原子写有界重试
- **报告**：中#3a，`src/vagent/storage.py:40-50`
- **根因**：`atomic_write_bytes` 只 `os.replace` 一次；索引器/杀毒/编辑器占用目标文件时抛 `PermissionError`（WinError 5/32），上层直接失败。
- **方案**：
  1. `os.replace` 对 `PermissionError` 且 `winerror ∈ {5,32,33}`（POSIX 下 `EACCES`/`EBUSY`）重试 6 次，指数退避 + 抖动，总耗时 ≤ 800 ms。
  2. 读取路径（`FileStore.open` 读 `state.json`）同样重试。
  3. 重试耗尽抛 `AppError("STORAGE_BUSY", ...)`，`__cause__` 保留原始异常；CLI/Web 显示"磁盘被占用，请重试"，不再裸露 traceback。
  4. 临时文件仍用 uuid 名；`finally` 清理语义不变。
- **改动面**：`storage.py`；新增 `tests/test_storage_atomic.py`。
- **测试/验收**：monkeypatch `os.replace` 先抛 3 次 `PermissionError(winerror=5)` 后成功 → 内容正确、无 `.tmp` 残留；一直失败 → `STORAGE_BUSY` 且原文件未损坏；`windows-latest` 矩阵必须绿。
- **风险**：同步重试会短暂占用事件循环 ≤ 0.8 s；只在写盘失败路径发生，可接受。

### FX-03　付费任务句柄 WAL、僵尸恢复、可停止
- **报告**：中#3b，`src/vagent/storage.py:415-476`、`video/worker.py:327-344`、`runner.py:1337-1358`、`application.py:1359-1369`
- **根因**：供应商已受理后，记录 `providerTaskId` 的那次 `state.json` 写失败 → Job 变 `unknown/SUBMISSION_UNKNOWN`，任务永久失联；收尾写盘失败 → Run 停在 `running` 但锁已释放，之后 `RUN_BUSY`；执行版本 1 的 Run 没有等待服务，`stop` 直接拒绝。
- **方案**：
  1. 新增旁路 WAL `<home>/submission-handles.jsonl`（追加写、每条 fsync、POSIX 0600）：提交前写 `{"phase":"intent","jobId","provider","requestId","at"}`；供应商返回后立刻写 `{"phase":"handle","jobId","taskId","requestId","at"}`，**再**做 `state.json` 提交。
  2. `FileStore.open` 的恢复阶段对账：Job 为 `SUBMISSION_UNKNOWN` 或 `providerTaskId` 为空但 WAL 有 handle → 采纳 `taskId`，转 `queued/polling`，`revision+1`，记 `job.handle_recovered`。只有 intent 的 Job 保持 unknown，但把已有的 `providerSubmissionRequestId` 暴露出来供人工对账（**不新增 Job 契约字段**）。
  3. WAL 打开时压缩：保留 7 天或到相关 Job 全部终态；超过 1 MiB 轮转 `submission-handles.1.jsonl`。
  4. 僵尸自愈：`_execute`/`Application` 启动时，`running` 但本进程无锁、无活动任务 → 事务改为 `interrupted`（`resumable=True`），记 `run.stale_recovered`。
  5. 停止：`runner.stop`/`application.stop` 对"非本进程持有"的 `running` Run 也能事务化转 `cancelled`（复用 `WaitService.stop_run` 的收尾逻辑），不再 `RUN_BUSY`。
- **改动面**：`storage.py`、`video/worker.py`、`runner.py`、`application.py`、`wait_runtime.py`；`tests/test_video_jobs.py`、`test_video_worker.py`、`test_resume.py`、新增 `tests/test_submission_wal.py`。
- **兼容**：`state.json` 不变；WAL 是新旁路文件，旧数据目录缺失时按空处理。
- **测试/验收**：
  - `test_handle_wal_survives_a_failed_state_write`：monkeypatch `atomic_write_json` 对 `state.json` 只失败一次 → 重启后 `providerTaskId` 已恢复、`submitCalls == 1`。
  - `test_intent_without_handle_stays_unknown_and_is_reconcilable`
  - `test_zombie_running_run_recovers_on_next_start`、`test_non_local_running_run_can_be_stopped`
  - WAL 压缩/轮转单测。
- **风险**：WAL 里的 taskId 是明文但无凭据，权限 0600；Windows 无 POSIX 权限位，靠目录 ACL（文档注明）。

### FX-04　预算耗尽时的收尾轮（guaranteed closing turn）
- **报告**：中#4 + 缺口#3，`src/vagent/runner.py:802-808`、`1018-1036`
- **根因**：剩余步数或工具额度为 0 时直接 `STEP_LIMIT`/`TOOL_LIMIT` 失败；模型即使刚提交了产物，用户也只看到一条通用错误，拿不到 `artifactId`。
- **方案**：
  1. 定义：`max_steps` 继续表示**可带工具的模型步**；收尾轮是额外的 1 次 **不带工具** 的模型调用。
  2. 触发条件（三者同时满足）：预算刚耗尽（`model_steps >= max_steps` 或 `tool_calls >= max_tool_calls`）；上一批工具里有**成功的写副作用**（`artifact_save`、`project_update`、已受理的 `video_generate` 等，用 `ToolDefinition.effect` 判定）；本次执行尚未用过收尾轮。
  3. "是否用过"记在**图状态**（`GraphState.closing_turn_used`，SQLite 检查点），不进 `state.json`；旧检查点缺该键按 `False` 处理。这样显式/自动恢复都不会重复收尾轮。
  4. 自动恢复的预算校验（`validate_auto_budget`，`runner.py:1243-1250`）要放行"步数刚好等于上限、但收尾轮还没用"的情况，否则外部结果在最后一步交付时会被 `STEP_LIMIT` 挡住。
  5. 收尾调用：`tools=[]` 并在尾部消息追加收尾指令（"预算已用尽，不要调用工具；说明已完成什么、产物 ID、还剩什么"）。返回值只取文本；即使模型仍返回 tool_calls 也不执行、不循环。
  6. 收尾轮计入 `modelSteps`（诚实计费），但允许 `max_steps + 1`；结果 `status=completed`，附加 `run.closing_turn` 事件（`reason=STEP_LIMIT|TOOL_LIMIT`）与 `run.budget_ended` 事件属性。
  7. 确定性兜底：从本次 Run 的 ToolMessage 中提取成功的 `artifactId`，若最终回答未包含，追加一行"已完成产物：…"，保证 ID 一定可见。
  8. 开关：`RunPolicy(..., closing_turn=False)` **库默认关**；`Application`/headless 新 Run 默认开（`VAGENT_CLOSING_TURN`，缺省 on）。不写入 `state.json` 的 `policy`，避免 schema 变动；显式 resume 时按当前配置执行。
- **改动面**：`runner.py`（`model_node`、`tools_node`、`RunPolicy`）、`application.py`、`config.py`；`tests/test_runner.py`、`test_resume.py`。
- **兼容**：默认关时行为与今日一致，`test_m1b/m2/m3` 的 `STEP_LIMIT` 基线不动；产品路径的新 Run 才有收尾轮。`docs/AGENT_HARNESS_DESIGN.md §4.2` 与 `USER_GUIDE` 同步写清"总数可为 maxSteps+1"。
- **测试/验收**：
  - `test_last_step_artifact_save_returns_a_closing_answer_with_artifact_id`（`max_steps=1`）
  - `test_tool_limit_uses_a_closing_turn_instead_of_failing`
  - `test_closing_turn_is_not_repeated_after_resume`
  - `test_last_step_external_result_still_gets_a_closing_turn`（自动恢复不再被 `validate_auto_budget` 挡住）
  - `test_no_closing_turn_when_the_run_only_read`（保持 `test_step_limit_prevents_unbounded_loop` 原断言不变）
- **风险**：模型在收尾轮仍可能输出工具意图；只取文本、忽略调用，不会产生副作用。

### FX-05　回答缓存写入即发即弃
- **报告**：中#5，`src/vagent/runner.py:957-965`
- **根因**：`answer_cache.put` 包在 `bounded_call` 里，且位于成功分支内；写入其实已成功时，用户停止会让 `bounded_call` 抛 `CANCELLED`，整个 Run 变 `cancelled`，恢复要多付一次模型调用；deadline 同时到期则丢回答。
- **方案**：
  1. 先确定 `completed` 并 `publish`，再异步写缓存：`asyncio.create_task` + 完成回调吞异常，登记在 runner 的 `_background` 集合。
  2. `_execute` 收尾时 `asyncio.wait(..., timeout=0.5)` 尽力排空，不因缓存失败改变 Run 状态；取消/超时只记 `cache.put_skipped` 事件。
  3. `record_cache` 的失败同样吞掉，只在 telemetry 里标注。
- **改动面**：`runner.py`；`tests/test_answer_cache.py`。
- **测试/验收**：`test_cancelled_during_cache_put_keeps_completed_answer`（停止时机在 put 开始后）→ `status=completed`、`modelCalls=1`；`test_cache_put_timeout_does_not_fail_the_run`；`test_cache_put_failure_is_ignored`。
- **风险**：进程立刻退出时缓存可能不落盘——缓存本就可丢，可接受。

### FX-06　末块重复下发的合并
- **报告**：中#6，`src/vagent/models.py:96-124`
- **根因**：`add_ai_message_chunks` 合并 `response_metadata` 时把字符串**拼接**，网关重复发最终块 → `finish_reason="stopstop"` 被判 `RESPONSE_TRUNCATED`；工具名/id 每个增量重复出现 → `skill_readskill_read`。
- **方案**：
  1. 汇总阶段自己合并：保留 `chunks` 列表；`finish_reason` 取最后一个非空值。
  2. `tool_call_chunks` 按 `index` 分组：`name`/`id` 取该组最后一个非空值，`args` 仍按顺序拼接（JSON 分片）；合并后再 `json.loads`。
  3. 对 `content` 的重复整块下发**不做**去重（可能是合法重复文本），只在文档标注为已知边界。
- **改动面**：`models.py`；`tests/test_streaming.py`。
- **测试/验收**：双 `finish_reason=stop` → `completed`；增量重复 name/id → 单次正确调用；交错 index / 迟到 id / 缺 `[DONE]` 的原有用例保持绿。
- **风险**：不同网关"重复块"语义不同；解析失败仍走 `INVALID_TOOL_CALL` 反馈（FX-07）。

### FX-07　畸形工具调用改为反馈一次
- **报告**：中#8，`src/vagent/runner.py:939-946`、`models.py:120-123`
- **根因**：`INVALID_TOOL_CALL` 不在 `RECOVERABLE_ERRORS`，也从不转成 ToolMessage；模型一次坏 JSON，Run 直接不可恢复失败。`H0:24` 却把它列为"反馈一次"的错误码。
- **方案**：
  1. `models.py` 不再直接抛：坏参数的调用放进 `invalid_tool_calls` 并返回 `AIMessage`（保留已解析出的合法调用与 usage 元数据，计费不丢）。
  2. `runner` 把每个畸形调用转成**唯一 id 的合成调用**（缺 id 用 `invalid-{step}-{i}`，重复 id 重新分配），在 `additional_kwargs["invalidToolCalls"]` 里记 `{id: {name, error}}`，再进 tools 节点。
  3. tools 节点按标记直接返回 `{"ok":false,"error":{"code":"INVALID_TOOL_CALL","message":"参数不是合法 JSON，未执行"}}` 的 ToolMessage，绝不执行工具。
  4. 现有 `schema_feedback_exhausted`（修好 R1 后）在第二次相同畸形时以 `SCHEMA_FEEDBACK_LIMIT` 结束，避免死循环。
- **改动面**：`models.py`、`runner.py`；`tests/test_runner.py`、`tests/test_streaming.py`、`tests/test_usage.py`、`tests/test_harness_gaps.py`。
- **测试/验收**：`test_malformed_call_is_fed_back_once_then_retried`（模型第二次改正 → `completed`）；`test_repeated_malformed_call_ends_with_schema_feedback_limit`；`test_malformed_call_never_executes_a_tool`（`operations == {}`）。
- **风险**：错误信息回吐的参数片段要截断（≤200 字符），避免把坏 JSON 原样塞回上下文。

## 4. M1：正确性（v0.6.3）

### FX-08　R1：Schema 反馈上限按 Run 复位
- **报告**：R1，`runner.py:226-248`
- **根因**：遇到 `HumanMessage` 只把 `current = None`，`batches` 不清空，于是上一个 Run 的末批签名留在列表里；新 Run 第一次出现相同错误就被判"第二次"，直接 `SCHEMA_FEEDBACK_LIMIT`。
- **方案**：`HumanMessage` 分支同时 `batches.clear()`；判定仍完全由消息历史推导，不新增持久字段。
- **测试/验收**：`test_schema_feedback_limit_resets_between_runs_in_one_session`：Run1 以一次 Schema 错误结束；Run2 第一次相同错误 → 不失败、`model.calls == 2`；Run2 第二次 → 仍正确拦截。补一个同进程双 Run 的用例（报告指出当前没有多 Run 覆盖）。
- **量**：S。**风险**：低。

### FX-09　R2：摘要降级等价与事件真实
- **报告**：R2，`runner.py:854-870`、`context.py:209-236`、`summary.py:98-103`
- **根因**：
  - 摘要不可用时，runner 保留的是"为摘要预留窗口"的那次裁剪结果（更少消息），而不是 v2 的普通截断 → 20 条对 17 条、11903 对 10421 字节。
  - 摘要生成后因超预算被拒时，`context.summarized` 事件已经发出，span `applied=bool(summary)` 仍为真。
- **方案**：
  1. 摘要生成后**重新构建**并检查 `report.summary is not None`；未采用则用 `build_context(summarize=False)` 重建，保证与 v2 逐字节相同。
  2. `_summarize` 只负责"生成/复用"，不再发事件；由调用点根据最终 `applied` 发一次 `context.summarized`（带 `applied`、`reused`、`summaryBytes`）；span 只在真正采用时 `applied=True`。
  3. 摘要被预算拒绝时记 `summary_rejected` 计数，便于观测反复拒绝。
- **测试/验收**：`test_summary_failure_matches_v2_byte_for_byte`（同输入两种路径消息/字节相等）；`test_rejected_summary_does_not_emit_applied_event`；修正 `docs/CONTEXT_SUMMARY_ACCEPTANCE.md:10` 的说法。
- **量**：M。**风险**：摘要在长历史下多一次 `build`（纯计算，无模型调用）。

### FX-10　R3：记忆取代语义
- **报告**：R3，`memory.py:176-211`
- **根因**：`sync` 对每个当前事实，把同 `(projectId, kind)` 的**所有**其他记录都标成 `supersededBy=本键`。带多条约束的项目每加一条就杀掉其余；且"先标记、后判断无新增不写盘"导致标记潜伏，下一次有新增时一起写盘 → `constraints` 全部召回不到。删除的事实则永远不被取代。
- **方案**：
  1. 改为"当前事实集差集"：本轮 `project_facts` 的指纹集合 `F`；同项目记录中，`id ∈ F` 且未取代的保持活跃；`id ∉ F` 且活跃的记录标 `supersededBy="removed"`（删除）或指向同 kind 的新记录（文本变更时可选，不做也能表达）。
  2. 只有"新增"或"新取代"实际发生时才写盘，且一次写最终集合。
  3. 多值字段（`constraints`、`plan`）之间**不再互相取代**；同文本重复项按指纹天然去重。
  4. 侧车版本升到 `memory-v2`：读取时兼容 v1（`supersededBy` 为任意真值即视为失效），写入用 v2；不需要 `state.json` 变更。
- **测试/验收**：`test_adding_a_constraint_keeps_sibling_constraints_active`；`test_removing_a_constraint_supersedes_it`；`test_unsynced_supersede_is_not_persisted_without_a_change`；`test_scalar_change_supersedes_only_the_old_value`；`test_v1_sidecar_is_readable`。
- **量**：M。**风险**：`"removed"` 语义要写进文档；检索只过滤真值，无需改读取逻辑。

### FX-11　R4：淘汰顺序与读放大
- **报告**：R4，`memory.py:155`、`summary.py:204`
- **根因**：`sort_keys=True` 让 JSON 键按哈希序落盘，读取后 dict 顺序即哈希序，`list(records)[:n]` 淘汰的是"字典序最小"，不是最旧；淘汰全局跨项目，也不优先淘汰已取代；每步 `sync` + `render` 各解析一次 `memory.json`。
- **方案**：
  1. 落盘仍确定：先按 `(createdAt, id)` 排序再序列化，保持字节稳定。
  2. 淘汰顺序：已取代 → 本项目最旧 → 全局最旧；新增 `MAX_RECORDS_PER_PROJECT`（如 512）。
  3. `MemoryIndex` 增加按 `(st_mtime_ns, st_size)` 失效的读缓存；`sync` 返回记录集，`render` 复用，单步只解析一次。
  4. `SummaryStore` 同样加 `createdAt`、按时间淘汰、读缓存。
- **测试/验收**：`test_eviction_keeps_newest_and_prefers_superseded`；`test_eviction_does_not_cross_project_while_local_capacity_remains`；`test_one_memory_parse_per_step`（monkeypatch `_read` 计数 == 1）。
- **量**：M。**风险**：缓存要与单写者锁一致；跨进程只读安全。

### FX-12　上下文动态块后移（contextVersion 4）
- **报告**：中#10，`context.py:162-177`、`runner.py:803-806`
- **根因**：每步变化的剩余预算、记忆块、摘要都拼在 `messages[0]` 的系统消息里；相邻两步在第一个消息就分叉，之后越来越长的历史完全无法命中供应商前缀缓存（实测 3105 字节只有前 583 字节相同）。
- **方案**：
  1. 新增 `contextVersion=4`（v1/v2/v3 布局与签名不动，`CONTEXT_VERSIONS=(1,2,3,4)`）。
  2. v4 布局：`system(规则 + Skills + 项目事实)` → 历史 → **尾部一条动态消息**（摘要 + 记忆块 + 剩余预算），尾部消息由 `build()` 每次合成，**不写入历史/检查点**。
  3. 尾部角色先按 `SystemMessage` 实现（DeepSeek 兼容多 system 消息则最优），提供 `VAGENT_CONTEXT_TAIL=system|human` 兜底；默认值在 1 次真实探针通过后确定。
  4. 签名：v4 的 `contextSignature` 纳入布局版本与尾消息模板；v2/v3 签名不变，旧 Run 恢复路径不变。
  5. 记忆块变为"预算不足可丢弃"：见 FX-26。
- **改动面**：`context.py`、`runner.py`、`config.py`、`prompts.py`（尾模板）；`tests/test_context_optimization.py`、新增 `tests/test_context_layout_v4.py`。
- **测试/验收**：
  - `test_adjacent_steps_share_the_same_history_prefix`：模拟两步，断言 `messages[0..k]` 字节完全相同，只有最后一条不同。
  - `test_v4_tail_is_not_persisted_in_history`
  - `test_v1_v2_v3_layouts_are_byte_identical_to_today`
  - 真实探针（显式授权、1 次调用）：断言模型能正确读到预算/记忆；失败则把默认尾部角色切到 `human` 并记录。
- **量**：L。**风险**：尾部 `SystemMessage` 的供应商兼容性未知，必须先探针；v4 默认仍关闭，等探针 OK 再考虑翻转。

### FX-13　`looks_secret` 漏报/误报
- **报告**：中#11，`memory.py:35-40`
- **根因**：Python `re` 把中文当 `\w`，`\bsk-` 在中文后不成立；缺 JWT/Bearer/密码类模式；`api key` 纯字样和 32 位 hex 又误报。
- **方案**：
  1. 边界统一用 `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])`。
  2. 补模式：`sk-[A-Za-z0-9_-]{8,}`、JWT（三段 base64url）、`Bearer\s+\S+`、中文"密码/密钥/令牌/口令"后跟分隔符与值、`api[_-]?key\s*[:=：]`。
  3. 十六进制/base64 只在"键名上下文"下判定，降低 commit hash 误报；"不要出现 API key 字样"这类否定句不命中。
  4. 保持"命中即不入库"（只影响 `memory.json` 侧车，不影响项目 JSON 进模型——文档写清）。
- **测试/验收**：参数化正向（中文前缀、JWT、Bearer、`后台密码：`）+ 负向（否定句、commit hash、普通 32 位编号）；`test_secret_detection_*`。
- **量**：S。**风险**：模式收紧可能漏掉新密钥格式；用"宁可多报"的保守方向。

### FX-14　MCP Schema ReDoS 与结果硬化
- **报告**：中#9、低#16，`tools.py:354-357`、`mcp_bridge.py:63-94`
- **根因**：已白名单的 MCP 服务可发布 `"pattern": "^(a+)+$"`；校验同步跑在事件循环上，`bounded_call` 的超时永远轮不到，Web 与"停止"一起冻结；结果含 NaN 或不可解析 `$ref` 时整 Run `failed/EXECUTION_ERROR`。
- **方案**：
  1. 注册/调用前拒绝 `pattern`、`patternProperties`、外部 `$ref`（默认 `MCP_SCHEMA_UNSUPPORTED`）；给一个显式 `allowPatterns` 配置项供自建可信服务开启，默认关。理由：Python `re` 无法在线程里安全超时（GIL），线性时间引擎引入新依赖不划算。
  2. 参数大小上限（如 32 KiB）与 Schema 深度/体积上限。
  3. 结果侧：递归清洗 NaN/Infinity/非 JSON 类型，`$ref` 构造失败 → 返回 `{"ok":false,"error":{"code":"MCP_SCHEMA"}}` 的工具错误，不再让 Run 失败；保留 16 KiB 限制。
  4. 结果打上 `source="mcp"`、"外部数据"标记（见 FX-28）。
- **测试/验收**：`test_pattern_schema_is_rejected_at_registration`；`test_malicious_pattern_cannot_block_the_event_loop`（0.2 s 心跳探针，改造报告的 7 s 复现）；`test_nan_result_returns_a_bounded_tool_error`；`test_unresolvable_ref_returns_a_tool_error`。
- **量**：M。**风险**：拒绝 `pattern` 会挡住部分真实服务，需在 `docs/MCP.md` 写明开关与风险。

### FX-15　失败计费调用的用量口径
- **报告**：中#7，`runner.py:764-794`、`620-644`
- **根因**：抛 `StreamResponseError` 的调用 token 进了 journal 与成本估算，但 span 没有用量、`model.usage` 没发；根 span 记的是跨恢复累计 token，恢复后重复相加。
- **方案**：
  1. `finish_attempt` 返回 `journal.finish_model_call` 的记录；异常分支据此 `emit model.usage`，并把 `error.usage` 写入 `model.call` span 的 `inputTokens/outputTokens`。
  2. 根 span 改为"本次执行增量"：执行前取 `record` 的 token 基线，结束时相减。
  3. `estimate_run` 与 span 口径对齐（见 FX-22）。
- **测试/验收**：`test_truncated_stream_records_usage_in_span_and_event`；`test_run_span_tokens_are_per_execution_after_resume`。
- **量**：S。**风险**：低；注意别把已取消调用的 token 记两次。

## 5. M2：评测与文档（v0.6.3）

### FX-16　行为评测可信化
- **报告**：高#2、中#14、低#26，`behavior.py:60-87/106-119/141-156/244-252`、`docs/AGENT_HARNESS_H3_ACCEPTANCE.md:26`
- **根因**：`ReplayModel` 不看输入，提示词换成 `"ignore"` 结果也不变；k 大于脚本数时重复最后一个脚本，pass@k 由脚本排列决定；门槛只看 pass@k，每个用例 50% 通过时 k=2 必过；评分器 4 个条件里 3 个删掉都没测试抓；贪心匹配会少算命中；`suite_id` 不含脚本内容。
- **方案**：
  1. **老实标注**：`mode="offline-replay"`、`LIMITATIONS` 明说"不回放真实模型行为"，删除 H3:26 与 H4 文档中"覆盖提示词回归"的说法。
  2. `k` 必须 `1 <= k <= min(len(case.samples))`，否则 `EVAL_K_TOO_LARGE`；**判定改用 pass@1**。
  3. 用例分 `gate`（全样本必须过）与 `measure`（只报告指标）两组；现有三条 50% 用例进 `measure`，新增 2 条 golden 进 `gate`。
  4. `suite_id` 纳入每个 sample 的完整脚本（工具名/参数/文本），提示词改了 ID 必变。
  5. `score_calls` 改最大二分匹配（Hopcroft–Karp 或增广路），补报告里的 2/2 例子。
  6. **评分器变异测试**：4 个负样本各自只违反一个条件（completed / artifacts / project / tools），任一条件被删都必须被抓。
  7. `ReplayModel` 增加可选断言：若传入 `tools` 为空却脚本要调工具，明确报错，暴露"脚本不依赖输入"的事实。
- **测试/验收**：`test_k_larger_than_samples_is_rejected`；`test_pass_at_1_decides_the_gate`；`test_suite_id_changes_when_a_script_changes`；`test_maximum_matching_counts_both_expected_calls`；四条 `test_scorer_check_<name>_is_required`（期望失败）。
- **量**：M。**风险**：golden 用例需要设计得更"整"，避免又变成 50% 通过。

### FX-17　`compare_cost.py` 真只读
- **报告**：中#12，`scripts/compare_cost.py:60`、`H6:12`
- **根因**：`FileStore.open` 会执行崩溃恢复，把 `running` 改成 `interrupted` 并写回；`--home` 打错会静默建目录并报 0 个 Run。
- **方案**：
  1. `storage.load_snapshot(home)`：只读 JSON + `DatabaseV9.model_validate`，不迁移、不恢复、不创建目录；目录/文件不存在 → `STORE_NOT_FOUND`。
  2. `compare_cost.py` 改用该函数；`--home` 错误直接报错退出。
  3. 其它只读脚本（`evaluate_*` 中仅统计的路径）一并排查。
- **测试/验收**：`test_compare_cost_never_writes_the_store`（monkeypatch 写函数为失败 + 比较文件字节/mtime）；`test_missing_home_fails_without_creating_it`。
- **量**：S。**风险**：低。

### FX-18　文档补齐（7 开关 + 3 命令 + 已知限制）
- **报告**：中#13、低#26，`README.md`、`docs/USER_GUIDE.md`、`.env.example`
- **内容**：
  - 新增"高级开关（默认关闭）"一节：`VAGENT_CONTEXT_VERSION`、`VAGENT_MEMORY`、`VAGENT_MEMORY_LIMIT`、`VAGENT_PROMPT_VERSION`、`VAGENT_TRACING`、`VAGENT_TRACE_SAMPLE`、`VAGENT_TRACE_CONTENT`（含义、默认值、对签名/恢复的影响）。
  - 新增命令说明：`vagent usage --cost`、`scripts/evaluate_behavior.py`、`scripts/compare_cost.py`。
  - 改 `README.md:230`："没有自动摘要" → "自动摘要为 v3 显式开关，默认关闭；记忆为 opt-in"。
  - `.env.example` 补注释条目（不全开，保持默认关闭）。
  - 60 s 单次模型上限写进"已知限制/行为"（见 FX-29）。
- **验收**：FX-19 的文档门禁用例覆盖上述字符串。

### FX-19　文档门禁与开关端到端用例
- **报告**：低#26
- **方案**：
  1. `tests/test_docs.py`：断言 7 个环境变量与 3 个命令在 README 或 USER_GUIDE 中出现；断言 `.env.example` 提到 7 个开关。
  2. Application 级用例：设置 `VAGENT_MEMORY=on` / `VAGENT_CONTEXT_VERSION=3` / `VAGENT_PROMPT_VERSION=v2` → 构造 `Application` → 断言 `runner.memory_index()`、`context.format_version`、`prompt_version` 真的透传（现在是配置层测了、装配层没测）。
  3. H4 文档样本 0/1 与表格对齐；H6 第 37 行的 22.70% 改注"来自 `ScriptedModel`，非真实调用"。
- **量**：S。

## 6. M3：健壮性与性能（v0.7.0）

### FX-20　`state.json` 增长与事务成本
- **报告**：低#15，`storage.py:497-530`、`application.py:1307-1313`、`journal.py:100-119`
- **根因**：每个 Run 存完整会话历史；每条事件单独写整文件；`operations`/`waits`/检查点从不清理；实测 10/20/40 Run 为 237 KB / 777 KB / 2.77 MB，40 Run 时一次空事务 37 ms。
- **方案（分三步，先低风险）**：
  1. 事件封顶：每 Run 保留最后 `MAX_EVENTS_PER_RUN=500` 条，记 `droppedEvents` 计数；`assistant.delta` 已不入库，维持。
  2. 恢复阶段"无变化不写盘"：`recover` 返回是否真的改了数据，`transaction` 支持 `write_if_changed`；打开健康库不重写 `state.json`（现在每次都写）。
  3. 保留策略 + `vagent gc`（默认 `--dry-run`）：终态且超过 `RETENTION_DAYS=30` 的 `operations`/`waits`/`modelCalls`/`events` 可清理；`--apply` 才写盘，并在 `state.json` 记 `lastGcAt`（可选字段）。
  4. 若仍偏大，再进 C2：`runs[].messages` 移到 `runs/<id>.json` 旁路（需 schema v10 或只读回退，单独 E0）。
- **测试/验收**：`test_open_does_not_rewrite_a_healthy_state`（mtime 不变）；`test_events_are_capped`；`test_gc_dry_run_is_read_only`、`test_gc_apply_keeps_recent_and_active`；`scripts/measure_state.py` 输出 10/20/40 Run 的字节与事务耗时（离线、可复现）。
- **量**：L。**风险**：清理是不可逆的，默认 dry-run + 显式 `--apply`；被清理的 Run 仍可 `inspect` 摘要。

### FX-21　侧车写安全与摘要缓存键
- **报告**：低#17、低#18，`summary.py:156-164/193-211`、`memory.py:165-174`
- **根因**：固定 `.tmp` 名、不 fsync、不在锁内写；写失败会让 Run 失败；摘要缓存键不含预算，大预算摘要被小预算反复拒绝且永不重建。
- **方案**：
  1. 两个侧车都改用 `storage.atomic_write_bytes`（uuid 临时名 + fsync + replace + FX-02 重试）。
  2. 写失败只降级：摘要 → 跳过缓存、继续用截断；记忆 → 记事件、本次不用记忆块。绝不失败 Run。
  3. `span_key` 加入 `targetBytes`（以及 summarizer 的版本/指纹；无指纹时用类名 + `promptVersion`）。
- **测试/验收**：`test_summary_write_failure_degrades_to_truncation`；`test_memory_write_failure_does_not_fail_the_run`；`test_summary_cache_key_includes_the_budget`；`test_sidecar_never_leaves_a_temp_file`。

### FX-22　成本估算的未跟踪模型步
- **报告**：低#19，`pricing.py:117-164`
- **方案**：`estimate_run` 读取 `untrackedModelSteps`，非 0 时 `status` 至少为 `partial`，输出 `untrackedModelSteps` 与"金额为下限"的说明；`usage --cost` 同步展示。
- **测试/验收**：`test_untracked_model_steps_make_the_estimate_partial`；`test_fully_tracked_run_stays_priced`。

### FX-23　流中断分类与用量
- **报告**：低#21，`models.py:80-95`
- **方案**：`astream` 迭代包 try/except，识别 `httpx`/底层传输异常 → `StreamResponseError("STREAM_INTERRUPTED", ..., partial_reply)`（可恢复），保留已收到的 usage；未知异常仍是 `EXECUTION_ERROR`。
- **测试/验收**：伪 transport 在首个增量后抛 `httpx.ReadError` → `STREAM_INTERRUPTED`、`resumable=True`、journal 有 token。

### FX-24　TTS 下载策略对齐
- **报告**：低#22，`video/tts.py:421-428/492-505`
- **方案**：复用 `video/media_http.py` 的策略骨架：https-only、端口 443、固定 `*.aliyuncs.com` 精确主机白名单（沿用现有测试主机）、`PinnedPublicBackend` 做 DNS 公网校验、禁重定向；http URL 明确拒绝并给出可读错误（官方示例的 http 不代表可接受）。
- **测试/验收**：任意 `*.aliyuncs.com`、非 443 端口、明文 http、解析到私网 → 全部 `PROVIDER_RESULT_UNTRUSTED`；现有成功路径不变。

### FX-25　`Span.set()` 脱敏
- **报告**：低#23，`tracing.py:101-110`
- **方案**：`set()` 走 `redact_attributes` 同一套规则（键名像凭据则整条丢弃，值按模式替换），`status`/`errorCode` 仍作特殊字段。
- **测试/验收**：`test_span_set_redacts_secret_keys_and_values`；现有 span 断言不回归。

### FX-26　摘要特权位置与记忆预算降级
- **报告**：低#24，`summary.py:106-114`、`context.py:177-207`
- **方案**：
  1. 摘要随 FX-12 移到尾部动态消息，不再抬高进 system；补一条注入用例（摘要里写"忽略规则"不改变工具集）。
  2. 记忆块可丢弃：`fit` 超预算时先尝试去掉记忆块重建（发 `context.memory_dropped`），仍超才报 `CONTEXT_LIMIT`；避免"开了记忆反而跑不动"。
- **测试/验收**：`test_memory_block_is_dropped_before_context_limit`；`test_summary_injection_cannot_change_the_tool_set`。

### FX-27　`BibleSave` 字符串长度上限
- **报告**：低#25，`video/tools.py`
- **方案**：`BibleSave` 的 `genre/style/title` 等字段加 `max_length`（与各镜头提示词预算一致），超限返回 `INVALID_ARGUMENTS` 而不是之后每个镜头 `VIDEO_PROMPT_TOO_LONG`。
- **测试/验收**：`test_bible_genre_length_is_bounded`。

### FX-28　零散加固
- **报告**：能力缺口"其他区域提到的缺口"
- **清单**：
  1. `instance.lock` 记 PID 却从不检查活性：打开时解析 PID，进程不存在 → 视为陈旧锁并恢复（Windows 用 `OpenProcess`/`os.kill(pid,0)` 等价判断），仍不为活进程抢锁。
  2. `INVALID_STORE` 无"最后正常"快照：每次成功事务后按低频（如每 20 次或显式 `vagent snapshot`）保留 `state.last-good.json`；提供 `vagent recover --from last-good`（显式）。
  3. `traces/*.jsonl` 无轮转：按 16 MiB 轮转，POSIX 0600；文档写明权限。
  4. MCP 结果标注"不可信外部数据"（ToolMessage 头部注释），`inputSchema` 体积上限。
  5. 同名同参重复调用：连续重复时返回提示性结果（附"该调用已执行过"），并在事件里记 `repeatCalls`，便于发现"步数被悄悄耗尽"；不直接拒绝，避免误伤合法重读。
  6. 用户取消的流：把已产生的 token 记入 journal（与 FX-15 同一处）。
  7. 模型连接重试次数：记录到 `model.call` span 属性（若底层无重试则标注 0）。
- **验收**：逐条一个小用例；不改变成功路径。

### FX-29　单次模型 60 s 上限（待决策）
- **报告**：低#20，`runner.py:904-933`
- **现状**：`reservation = min(60.0, ...)`，长回复在供应商已计费后被杀。
- **选项**：
  - A（推荐，最小改动）：保留 60 s，写进 USER_GUIDE"已知限制"，并把 `reservation` 常量提为模块常量便于调整。
  - B：改为 `RunPolicy.max_model_seconds`（默认 60，可调），需要策略字段与恢复兼容说明。
- **建议**：先做 A；等有真实长回复被截断的证据再做 B。

## 7. 能力缺口专项（M4，按需排期）

| 项 | 内容 | 依赖 | 验收 |
|---|---|---|---|
| C1 付费意图与对账 | 供应商能力矩阵（Kling/Wan/fal 是否支持幂等键/客户端 token 查询）；支持者传 token 并在恢复时按 token 查询；`vagent job reconcile <id>` 显式对账 | FX-03 WAL | mock 供应商按 token 找回；不支持者明确标注"只能人工对账" |
| C2 数据保留与压缩 | `runs[].messages` 旁路到 `runs/<id>.json`、检查点清理、`vagent fsck` 对齐 waits/operations/Run 状态 | FX-20 | fsck 能发现僵尸 Run 与孤儿 wait；旁路后 `state.json` 线性下降 |
| C3 收尾轮策略化 | 收尾轮触发条件可配置（写副作用/任意/关闭）、多种收尾提示模板 | FX-04 | 三种策略各一条用例；预算不变 |
| C4 真模型行为评测 | 显式授权的 live 模式、无偏 pass@k + 置信区间、提示词/上下文/记忆的 A/B | FX-16 | 每次运行有成本上限与授权开关；离线 CI 不受影响 |
| C5 后台循环监督 | Worker/协调器异常退避重启；`wait/checkpoint/operation` 一致性自检 | FX-01、FX-20 | 注入异常后自动恢复；自检报告可读 |

## 8. 验证与验收门禁

1. **离线全绿**：`python -m pytest -q`（当前可收集 1624 项）；四平台矩阵（ubuntu/windows × 3.11/3.12）与 wheel 安装态冒烟不变。
2. **每条先红后绿**：新增用例必须在修复前失败，修复后通过；报告里的复现路径尽量转成回归用例（FX-01/02/03/04/05/06/07/09/14）。
3. **契约门禁**：`tests/test_video_contracts.py`、`test_video_contracts_v2.py`、`test_storage_migration.py`、`test_resume.py`、`test_provider_contract_replay.py` 必须绿；168 个模型哈希与工具面不变。
4. **降级门禁**：摘要/记忆/追踪/缓存/侧车的所有失败注入用例必须证明"Run 不失败"。
5. **性能门禁**：`scripts/measure_state.py` 输出对比表（10/20/40 Run 的字节与空事务耗时），M3 后事务耗时不得超过 2.77 MB 场景的 50%。
6. **唯一 live 项**：FX-12 的尾部角色探针，1 次低成本 DeepSeek 调用，需显式授权、记录到验收文档；失败即回退到 `human` 尾消息。
7. **文档一致性**：`tests/test_docs.py` 覆盖 7 开关 + 3 命令 + 60 s 限制；报告点名的 5 处说法逐条有修正记录。

## 9. 排期与依赖

| 周 | 内容 | 可并行 |
|---|---|---|
| 第 1 周 | M0 全部（FX-01→07） | FX-05/06/07 可与 FX-01/02/03 并行 |
| 第 2 周 | M1 上半（FX-08→11、FX-13、FX-15） | FX-12 需先做探针，可与其它并行 |
| 第 2–3 周 | M1 下半（FX-12）+ M2（FX-16→19） | FX-12 与 M2 独立 |
| 第 3–4 周 | M3（FX-20→28） | FX-24/25/27 可并行；FX-20 单独占一条泳道 |
| 之后 | M4（C1→C5） | 按风险优先级 |

- **关键路径**：FX-01（卡死）→ FX-03（WAL，依赖 FX-02 的写可靠性）→ FX-12（v4，需探针）→ FX-20（增长）。
- **版本**：M0 → v0.6.2；M1+M2 → v0.6.3；M3 → v0.7.0；M4 随能力走。
- 同一批次一条独立 PR；每条 PR 自带"复现用例 + 修复 + 文档/验收表格更新"。

## 10. 风险、兼容与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| 收尾轮改变预算语义 | 模型调用数可 `max_steps+1` | 库默认关、产品开；文档写清；`test_step_limit_prevents_unbounded_loop` 保持原行为（只读不触发） |
| 上下文 v4 尾部角色不被网关接受 | 新 Run 请求失败 | 默认仍 v2；v4 显式开关 + 1 次探针；`tail=human` 兜底；失败立即回滚到 v3 |
| `state.json` 追加字段/清理 | 旧版本程序读不了新文件；清理不可逆 | 能放旁路就放旁路；`gc` 默认 dry-run；迁移前保留原始字节（沿用现有 `state-vN-*.json` 纪律） |
| MCP 拒绝 `pattern` | 部分服务注册失败 | 显式 `allowPatterns` 开关 + `MCP.md` 风险说明 |
| 记忆 v2 语义变化 | 旧侧车记录被重新解释 | 版本号区分；v1 只读兼容；召回逻辑只认真值 |
| 评测门禁变严 | 现有报告/基线需要重跑 | 现有 50% 用例转 `measure`；golden 单独加；历史报告不覆盖 |
| 写重试掩盖真实磁盘故障 | 延迟失败 | 重试有上限并抛 `STORAGE_BUSY`；trace/事件记录重试次数 |

## 11. 需要同步修改的既有测试（预期变化，不是回归）

| 文件 | 现状 | 修复后 |
|---|---|---|
| `tests/test_wait_recovery.py:75` | 断言"每次扫描都返回被阻塞的 Run" | 第二次扫描 `None`；同会话后继可推进 |
| `tests/test_wait_runtime.py:332-333` | 阻塞后仍返回该 Run | 首次记录阻塞，后续跳过；退避用例单独断言 |
| `tests/test_runner.py:180-186` | 畸形调用直接 `INVALID_TOOL_CALL` 失败 | 第一次反馈，第二次 `SCHEMA_FEEDBACK_LIMIT` |
| `tests/test_streaming.py:226-249` | 流式畸形参数直接 `INVALID_TOOL_CALL` 失败 | 第一次反馈，第二次 `SCHEMA_FEEDBACK_LIMIT`；截断仍失败 |
| `tests/test_usage.py:170-177` | 单次被拒调用的用量 | 两次调用（反馈 + 上限）的累计用量 |
| `tests/test_runner.py:92-99` | `max_steps=2` 纯读循环 → `STEP_LIMIT` | 不变（只读不触发收尾轮）；新增写副作用收尾轮用例 |
| `tests/test_behavior_evaluation.py:44-52` | 依赖 `pass@k` 与 `k=2` 门槛 | 改 `pass@1`、k≤样本数、gate/measure 分组 |
| `tests/test_context_summary.py:117` | 超预算摘要"降级到普通截断" | 断言与 v2 逐字节相同、事件/span `applied=false` |
| `tests/test_long_term_memory.py` | 同类事实互相取代 | 多条约束并存、删除才取代 |
| `tests/test_cost_estimate.py` | 有未跟踪步仍 `priced` | `partial` + 提示 |
| `tests/test_mcp.py` | 任意 Schema 都接受 | `pattern` 默认拒绝、开关放行 |
| `tests/test_harness_gaps.py:27` | 断言第二次相同错误被拦截 | 保留该断言；补"第一次只反馈、不失败"的用例 |

## 12. 文档同步清单

| 文档 | 改动 | 随批次 |
|---|---|---|
| `docs/CONTEXT_SUMMARY_ACCEPTANCE.md:10` | "降级后与 v2 逐字节相同"改为修复后的事实（并在表格注明降级样本） | FX-09 |
| `docs/AGENT_HARNESS_H3_ACCEPTANCE.md:26` | 删除"`evaluate_behavior.py` 一并覆盖提示词回归"，改为"行为回归由 H4 的回放评分器承担，不代表真实模型行为" | FX-16 |
| `docs/AGENT_HARNESS_H0_ACCEPTANCE.md:24` | `INVALID_TOOL_CALL` 改为"反馈一次后失败"的真实描述 | FX-07 |
| `docs/AGENT_HARNESS_H6_ACCEPTANCE.md:12` | "脚本不产生任何写入"改为"真只读快照，不执行崩溃恢复" | FX-17 |
| `docs/AGENT_HARNESS_H6_ACCEPTANCE.md:37`、`:41` | 22.70% 标注来自 `ScriptedModel`，非真实调用 | FX-19 |
| `README.md:230`、`docs/USER_GUIDE.md` | 7 开关、3 命令、v3 摘要/记忆的 opt-in 说明 | FX-18 |
| `AGENT_HARNESS_DESIGN.md §4.2` | 写清收尾轮与 `max_steps+1` 的预算语义 | FX-04 |
| `docs/MCP.md` | `pattern` 策略、`allowPatterns` 风险、结果不可信标注 | FX-14 |
| `docs/RECOVERY_ACCEPTANCE.md` | WAL 对账、僵尸自愈、可停止语义 | FX-03 |
| `.env.example` | 7 个开关的注释条目 | FX-18 |
| 新增 `docs/HARNESS_REVIEW_FIX_PLAN.md` | 本文件 | — |

## 13. 实施记录（M0）

> 2026-11-06，在 `aaa0c6d` 工作区落地；每条先在本地复现失败，再修复。

| 项 | 状态 | 落地要点 | 新增/调整用例 |
|---|---|---|---|
| FX-01 | 完成 | `WaitCoordinator` 按"永久阻塞跳过 + 暂态退避"扫描；`STALE_RUN` 事务化终态（停 wait、关 pending 调用）；`run_once` 仍返回"本次尝试过的 Run"，纯跳过返回 `None` | `tests/test_wait_runtime.py` 新增 3 条；`test_wait_recovery.py:75` 改为第二次扫描 `None` |
| FX-02 | 完成 | `atomic_write_bytes` 对 WinError 5/32/33 与 `EACCES/EPERM/EBUSY` 重试 6 次（≤0.8 s）；耗尽抛 `STORAGE_BUSY`；`FileStore.open` 读 `state.json` 同策略 | 新增 `tests/test_storage_atomic.py`（4 条） |
| FX-03 | 完成 | 新增 `src/vagent/submission_log.py`（intent/handle JSONL、fsync、轮转/压缩）；提交前写 intent、拿到句柄后先写 WAL 再提交 `state.json`；恢复时 `submitting`/`unknown` 均可采纳句柄；校验器只放行"unknown+无 ID → queued+WAL ID"这一种恢复；`run/resume` 前自愈僵尸 `running`；非本地 `running` 可停止 | 新增 `tests/test_submission_wal.py`（6 条）；`test_runner.py` 新增僵尸自愈用例 |
| FX-04 | 完成 | `RunPolicy.closing_turn`（库默认关）；产品路径由 `VAGENT_CLOSING_TURN`（缺省 on）开启；只对"本请求有成功写副作用"的预算耗尽放行一次无工具收尾调用；`closing_turn_used` 存图状态（不落 `state.json`）；回答补齐缺失的 `artifactId`；自动恢复不再被 `validate_auto_budget` 挡 | `test_runner.py` 新增 5 条；`test_wait_runtime.py` 新增"最后一步外部结果仍收尾"用例 |
| FX-05 | 完成 | 缓存写入改到"回答已判定完成"之后直接 await，不再经过 `bounded_call`；停止/到期不会把完成态变成取消/超时；写入异常只记 `error` 状态 | `test_answer_cache.py` 新增 2 条（停止、deadline） |
| FX-06 | 完成 | 新增 `_last_finish_reason` / `_merge_tool_call_chunks` / `_decode_tool_calls`：`finish_reason` 与工具 name/id 取最后一个非空值，JSON 参数按 index 拼接后解码 | `test_streaming.py` 新增 2 条（重复末块、重复工具名） |
| FX-07 | 完成 | 流式畸形参数不再直接抛错；runner 把缺失/重复 ID 或坏 JSON 转成带标记的合成调用，工具节点回 `INVALID_TOOL_CALL` ToolMessage 且整批不执行；第二次相同畸形由既有 `schema_feedback_exhausted` 收口 | `test_runner.py` 新增"反馈后可修正"、更新参数化用例；`test_streaming.py`、`test_usage.py` 同步期望 |

### 与规划的偏差

- **FX-05**：规划写"即发即弃"，实现改为"完成态确定后直接 await、脱离取消边界"。原因：现有答案缓存用例在 `run()` 返回后立即断言 `answerCache` 计数；直接 await 既满足"停止/超时不丢回答"，也保留可观测性。缓存自身有 0.5 s 超时与 `error` 状态。
- **FX-04**：`closing_turn_used` 放图检查点而不是 `state.json`，避免 schema 变更；`max_steps` 仍表示可带工具的模型步，收尾轮是额外一次，总量最多 `max_steps+1`。
- **FX-03**：恢复对账在 `FileStore.open` 内完成；WAL 压缩只保留非终态/`unknown` 的 Job，终态记录在下次打开时清掉。

### 验证

- 逐项用例先红后绿；`python -m pytest -q` 全量 **1645 passed, 4 skipped**（22 分钟，本机 Windows）。
- `python -m ruff check src/vagent tests scripts` 与 `ruff format --check` 全绿。
- 新增开关 `VAGENT_CLOSING_TURN` 已写入 `.env.example`、`AGENT_HARNESS_DESIGN.md §4.2` 与 `USER_GUIDE`；FX-18 的"高级开关"一节仍应把它与其余 7 个开关统一补齐。
- 本机运行需清掉 `HTTP(S)_PROXY/ALL_PROXY/NO_PROXY`：当前环境 `NO_PROXY` 里的 `[::1]` 会让 httpx 解析代理时抛 `InvalidURL`（与本次改动无关）。

## 14. 实施记录（M1 上半：FX-08 / FX-09 / FX-10）

> 2026-11-06，接着 M0 的同一工作区落地；每条先让新增用例在旧代码上失败，再修复转绿。

| 项 | 状态 | 落地要点 | 新增/调整用例 |
|---|---|---|---|
| FX-08 | 完成 | `schema_feedback_exhausted` 遇 `HumanMessage` 时同时 `batches.clear()`：上一 Run 的末批不再参与「连续两次相同 Schema 错误」的判定；判定仍完全由消息历史推导，无持久字段 | `tests/test_harness_gaps.py`：新增会话内跨 Run 用例，并在历史级用例补「两个 Run 各自一次相同错误 → 不拦截」断言 |
| FX-09 | 完成 | `_summarize` 只负责生成/复用并返回 `(summary, reused)`，不发事件；调用点在第二次 build 后用 `report.summary is not None` 判定采用：采用才发 `context.summarized`（`applied=true`、`reused`、`summaryBytes`）并让 span `applied=true`，未采用发 `context.summary_rejected(reason=budget)`、span `applied=false`；只要未采用就 `build_context(summarize=False)` 重建，与 v2 逐字节相同 | `tests/test_context_summary.py`：新增 2 条（摘要器失败逐字节等价、超预算不发 applied），既有用例不变 |
| FX-10 | 完成 | `sync` 改为「当前事实集差集」：`id ∈ F` 活跃（曾被取代的复活），`id ∉ F` 的活跃记录按 kind 处理——单值 kind 指向新值，`constraint`/`plan` 一律 `"removed"`；同级约束/计划项不再互相取代；新增、取代、复活任一真正发生才一次性写盘；侧车升 `memory-v2`，`version=1` 可读 | `tests/test_long_term_memory.py`：新增 6 条（并存、删除、立即落盘、单值指向、复活、v1 兼容） |

### 与规划的偏差

- **FX-10 的取代目标**：规划写「`"removed"` 或指向同 kind 的新记录（可选）」。实现按 kind 是否单值决定：`goal/audience/style` 指向新值，`constraint/plan` 一律 `"removed"`——多值 kind 里指向某个兄弟事实会重新引入「误杀」语义。另补「回归即复活」：内容寻址的记录再次出现在当前事实集时清空 `supersededBy`，否则删掉再加回的事实将永久不可召回，属同一类缺陷。
- **FX-09 的事件口径**：保留 `context.summary_reused` 作为独立的缓存命中事件；「一次事件带 applied/reused」由 `context.summarized`（仅在采用时）与 `context.summary_reused`（仅命中缓存时）共同表达，既满足「未采用不得声称 applied」，也不破坏既有观测口径。
- **FX-09 的重建成本**：降级路径多一次纯计算的 `build`（长历史下 O(消息数×轮数)），不产生模型调用；换来的是「失败即等于 v2」这一可断言的强不变量。

### 验证

- 定向：`tests/test_harness_gaps.py` **5 passed**、`tests/test_context_summary.py` **11 passed**、`tests/test_long_term_memory.py` **20 passed**；连同 `test_runner` / `test_resume` / `test_context_optimization` / `test_prompt_versions` / `test_tracing` / `test_usage` / `test_wait_runtime` / `test_wait_recovery` 一起跑 **212 passed**（71 秒）。
- 先红后绿：同上三组新增断言在旧实现上分别 **2 failed / 2 failed / 4 failed**，修复后全绿（FX-10 的「单值指向」用例在旧实现上恰好同键，故仍绿，属预期）。
- `ruff check src/vagent tests scripts` 与 `ruff format --check` 全绿。
- 全量：`pytest -q` **1557 passed, 4 skipped, 69 failed, 28 errors**（29 分 29 秒，本机 Windows）。失败/错误**全部**落在「子进程 + 管道」路径：ffmpeg 的 `asyncio.create_subprocess_exec` 抛 `PermissionError`（`ASSEMBLER_UNAVAILABLE`）、MCP stdio 连接失败（`MCP_CONNECT`）、`redis-server` 起不来、CLI 子进程超时或报本地目录权限——这是本次 DSH Windows 沙箱的已知边界（命名管道/受限令牌），不是本批改动。抽样复跑 `test_live_application`、`test_m3_headless`、`test_m3_multi_provider`、`test_m5_evaluation`、`test_m6_subtitle_api` 均复现为同一个 ffmpeg `PermissionError`；`test_m2_cli` 复现为 CLI 子进程的本地目录权限。失败清单里没有 `test_runner` / `test_resume` / `test_storage*` / `test_context_*` / `test_long_term_memory` / `test_usage` / `test_wait_*` / `test_job*` / `test_quality` / `test_tracing` 任何一项。
- 因此「全绿门禁」需在非沙箱终端复跑（M0 的 1645 passed 就是这样得到的）；本机沙箱里只把定向与核心用例作为判据，未把环境失败算作回归。
- 本机沙箱还有一个 `0o700` → Windows DACL 的问题（pytest 的 `tmp_path` 创建后本进程也不可读写）：用 `tmp/testshim/sitecustomize.py`（git-ignored，仅放进 `PYTHONPATH`）把该模式放宽到 `0o755` 再跑测试；仓库文件未改。

## 15. 实施记录（M1 剩余：FX-11 / FX-12 / FX-13 / FX-14 / FX-15）

| 项 | 状态 | 落地要点 | 新增/调整用例 |
|---|---|---|---|
| FX-11 | 完成 | 侧车按 `(createdAt, id)` 从旧到新落盘；淘汰顺序改为「已取代 → 本项目最旧 → 全局最旧」，新增 `MAX_RECORDS_PER_PROJECT=512`；`MemoryIndex` / `SummaryStore` 都按 `(st_mtime_ns, st_size)` 缓存解析结果（写后刷新），一步的 `sync`+`render` 只解析一次；`SummaryStore` 条目加 `createdAt`、按时间淘汰 | `tests/test_long_term_memory.py` 新增 3 条（淘汰优先已取代、本地容量不跨项目、一步一次解析）；`tests/test_context_summary.py` 新增 1 条（摘要按时间淘汰） |
| FX-13 | 完成 | 边界统一为 `(?<![A-Za-z0-9])`（中文前缀可命中）；补 JWT、`Bearer`、`api_key: 值`、中文键名（密码/密钥/口令/令牌）+ 分隔符；十六进制/base64 只在键名上下文里判为凭据，裸 hash 与「不要出现 API key 字样」不再误报 | `tests/test_long_term_memory.py` 新增 13 组参数化（8 正 + 5 负） |
| FX-15 | 完成 | 失败的模型调用在 `model.call` span 关闭前结算：`finish_model_call` 带 `error.usage`，span 记 `inputTokens/outputTokens`，有 token 时补发 `model.usage`；`finish_attempt` 用 `attempt_finished` 防重复结算；根 span 改为「本次执行增量」（减去进入时的累计 token） | `tests/test_tracing.py` 新增 2 条（截断流补 span/事件、恢复后根 span 每段增量） |
| FX-14 | 完成 | Schema 关卡统一到 `tools.check_schema`：拒绝外部 `$ref`、默认拒绝 `pattern`/`patternProperties`（`MCP_SCHEMA_UNSUPPORTED`，`ServerConfig.allow_patterns` 显式放行）、深度 ≤ 16、体积 ≤ 16 KiB；参数 ≤ 32 KiB 且校验异常一律降级为工具错误（`MCP_SCHEMA`）；结果递归清洗 NaN/Infinity 并标 `source="mcp"`，仍受 16 KiB 上限 | `tests/test_mcp.py` 新增 5 条（NaN 清洗、pattern 拒绝/放行、深度、恶意 pattern 不阻塞、坏 `$ref`/超大参数降级） |
| FX-12 | 完成 | 新增 `contextVersion=4`：系统消息只留规则 + Skills + 项目事实，摘要/记忆/剩余预算放进历史之后的**尾部动态消息**（每步合成、不入历史）；`VAGENT_CONTEXT_TAIL=system|human`（默认 `system`，已探针）；v4 签名纳入尾部角色与模板；v2/v3 布局与签名不变 | 新增 `tests/test_context_layout_v4.py`（8 条：前缀一致、tail 不含预算/记忆、v2/v3 逐字节、角色校验、端到端摘要、签名、开关） |

### 与规划的偏差

- **FX-11 的 `sync` 返回值**：规划写「`sync` 返回记录集」，实际保留 `list[str]`（本次新增的 id）以免破坏既有调用与用例；「只解析一次」由 `_read` 的 `(mtime, size)` 缓存达成，`render` 复用同一份解析结果。
- **淘汰会删除活跃记录**：容量是兜底而非正确性约束；被淘汰的当前事实会在下一次 `sync` 重新投影。已写进 H2 验收的兼容与边界。

### 验证

- `tests/test_long_term_memory.py` **36 passed**、`tests/test_context_summary.py` **12 passed**、`tests/test_tracing.py` **19 passed**、`tests/test_usage.py` **18 passed**、`tests/test_mcp.py` **10 passed**、`tests/test_context_layout_v4.py` **8 passed**；布局相关四文件一起 **60 passed**。
- 先红后绿：FX-11 的 4 条新用例在旧代码上全失败（3 条淘汰/解析 + 1 条摘要淘汰）；FX-13 的 13 组参数化在旧模式上有 **8 组失败**（5 个漏报：中文前缀、Bearer、JWT、中文键名 ×2；3 个误报：否定句、commit hash、32 位编号）；FX-15 的 2 条在旧代码上全失败（span 无 token、恢复后根 span 记 12 而非 5）；FX-14 的 3 条可导入断言在旧代码上全失败（pattern 被接受、深度无上限、坏 `$ref` 直接抛异常而不是返回工具错误），第 4 条因旧模块没有 `result_payload` 而无法收集（功能缺失）。FX-12 是新增布局，旧代码没有 v4；用「v2/v3 仍逐字节相同」与「相邻步共享前缀」两条覆盖，不把「旧代码没有该功能」算回归。
- **FX-12 真实探针（已授权）**：2026-11-06，`deepseek-flash`，1 次调用。v4 + `tail=system` 的 Run 问「本 Run 的 modelCallsRemaining 是多少」，模型答 `8`（= `maxSteps`），状态 `completed`、`contextVersion=4`、`modelCalls=1` —— 供应商接受历史之后的尾部 `SystemMessage`，默认 `tail_role=system` 成立；`human` 仅作未验证时的回退。
- `tests/test_context_layout_v4.py` 的 8 条：相邻步前缀一致、tail 不含预算/记忆、v2/v3 逐字节不变、两类尾部角色与非法值、v4 端到端摘要、签名覆盖 tail 角色、配置开关。



## 建议的下一步

1. **M0 / M1 已全部落地**（见 §13–§15）：FX-08→15 完成，M1 只剩发布版本号与全量门禁。
2. **M2（FX-16→19）** 可接着做：行为评测可信化、`compare_cost.py` 真只读、文档补齐（7 开关 + 3 命令 + 60 s 限制）、文档门禁用例。
3. **M3（FX-20→28）** 按「数据增长 → 旁路写安全 → 观测细节 → 加固」推进；FX-20 需单独一条泳道。
4. **M1 的 v4 仍是 opt-in**：默认 `VAGENT_CONTEXT_VERSION=2`。把默认翻到 v4 之前，建议先积累一段真实运行的缓存命中数据，再决定是否默认开启。
