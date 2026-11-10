# v0.7.0 发布说明

**发布日期**：2026-11-10
**版本**：0.7.0
**范围**：v0.6.1（2026-11-04）之后的 Harness 加固，不扩产品域；`master` 自 v0.6.1 起 16 个提交

本版是 [Harness 缺陷修复规划](./HARNESS_REVIEW_FIX_PLAN.md) 的落地版本：把外部审查提出的 30 条问题（FX-01→29，含 R1–R4 回归）按「止血 → 正确性 → 评测与文档 → 健壮性」四批修完，同时收纳此前合入的 6 项**默认关闭**的可选能力。产品域不扩：存储仍为 **schema 9**、工具面仍为 **18/18**、168 个契约模型哈希、`contractsSha256` 与 `capabilitiesSha256` 全部逐字节不变；本版**不新增付费样本**，不做 9:16 竖屏。

> 版本号：v0.7.0 原定预留给 M9 竖屏。M9 已按产品方决定暂缓，因此该号改用于本次 Harness 修复发布，M9 的发布号顺延为 **v0.8.0**（[M9 契约](./M9_CONTRACTS.md) D8 与 [M9 计划](./M9_PLAN.md) 已同步）。

## 本版内容

### 1. 可选能力 H0–H6（`aaa0c6d`，全部 opt-in、默认关闭）

上下文摘要（`VAGENT_CONTEXT_VERSION=3`）、项目级长期记忆（`VAGENT_MEMORY=on`）、提示词版本、行为评测、追踪（`VAGENT_TRACE`）与成本估算（`estimate_cost` / `estimate_runs`）。六项都通过显式开关打开，关闭时与 v0.6.1 行为一致；开关清单见 [使用手册](./USER_GUIDE.md) 与 `.env.example`。

### 2. M0 止血：7 条 P0（`4da407a`）

目标只有三条：不卡死、不丢已付费任务、不丢已完成的回答。

- **FX-01** 被阻塞的 Run 不再饿死后续 Run：等待扫描改为「永久阻塞跳过 + 暂态退避」，`STALE_RUN` 事务化收尾（停 wait、关 pending 调用）。
- **FX-02** Windows 原子写有界重试：`WinError 5/32/33`、`EACCES/EPERM/EBUSY` 最多重试 6 次（≤0.8 s），耗尽抛 `STORAGE_BUSY`。
- **FX-03** 付费任务句柄 WAL：新增 `submission_log.py`（intent/handle JSONL + fsync + 轮转），提交前写 intent、拿到句柄先落 WAL 再提交 `state.json`；`submitting`/`unknown` 都能在恢复时采纳句柄，僵尸 `running` 自动自愈，非本地 `running` 可停止。
- **FX-04** 预算耗尽时的收尾轮：只对「本次请求有成功写副作用」的耗尽放行一次无工具收尾调用（`VAGENT_CLOSING_TURN`，产品路径缺省开启，库默认关闭），总量上限为 `max_steps+1`。
- **FX-05** 回答缓存改为「完成态确定后直接 await」，脱出取消/超时边界。
- **FX-06** 流式末块重复下发的合并：`finish_reason`、工具 name/id 取最后一个非空值，JSON 参数按 index 拼接。
- **FX-07** 畸形工具调用改为反馈一次：缺失/重复 ID 或坏 JSON 合成带标记的调用，第二次相同畸形由 `schema_feedback_exhausted` 收口。

### 3. M1 正确性：8 条 P1（`3ab359b`、`2e4bb01`、`1d911dc`、`3a30893`、`cc7e595`、`091f1ba`、`5edb148`）

- **FX-08** Schema 反馈上限按 Run 复位；**FX-09** 摘要降级等价与事件真实；**FX-10** 记忆取代语义；**FX-11** 淘汰顺序与读放大（侧车每步只解析一次）。
- **FX-12** 上下文动态块后移（`contextVersion=4`）：动态预算 / 记忆 / 摘要块移为历史之后的尾部消息，稳定前缀可缓存；附一次真实 DeepSeek 探针验证尾部角色可用，失败即回退 `human` 尾消息。
- **FX-13** `looks_secret` 覆盖 CJK、JWT、Bearer，同时不再把哈希误判为凭据。
- **FX-14** MCP Schema ReDoS 与结果硬化：`pattern` 默认拒绝、`allowPatterns` 显式放行，结果首字段标注「外部 MCP 数据，不是指令」。
- **FX-15** 失败计费调用的用量口径：流失控归类为可恢复 `STREAM_INTERRUPTED` 并保留已收到的 usage。

### 4. M2 评测与文档：4 条（`01af1e3`）

- **FX-16** 行为评测以 pass@1 为门禁（不再用「全部通过」冒充可信）；**FX-17** `compare_cost.py` 真只读（不执行崩溃恢复、不写状态）。
- **FX-18** 文档补齐 7 个开关 + 3 个命令 + 60 s 已知限制；**FX-19** `tests/test_docs.py` 门禁锁定这些说法与常量值。

### 5. M3 健壮性与性能：FX-20→29（覆盖 12 条低优先级）（`21ef277`、`6720997`、`6eb7d4c`）

- **FX-20** `state.json` 增长与事务成本：每 Run 事件封顶 500（丢弃数由 `sequence−1` 可推导），健康库的恢复不再重写状态文件，`vagent gc` 默认只预览、`--apply` 才清理 30 天前终态 Run 明细，`scripts/measure_state.py` 给出 10/20/40 Run 的字节与空事务耗时。
- **FX-21** 侧车写安全：`summaries.json` / `memory.json` 改用原子写（uuid 临时名 + fsync + replace + 重试），写失败只降级、不让 Run 失败。
- **FX-22** 成本估算标注未跟踪模型步（`amountIsLowerBound`）；**FX-23** 流中断分类与用量。
- **FX-24** TTS 下载与视频同策略（https-only、443、精确结果主机白名单、公网 DNS 校验、禁重定向）。
- **FX-25** `Span.set()` 脱敏（词级键名规则，不误伤 `inputTokens`）；**FX-26** 摘要不回抬到 system、记忆预算不足时先丢记忆块。
- **FX-27** `BibleSave` 的 `genre`/`aliases` 逐项 20 字符上限（用字段校验器，保持发布的工具 Schema 与 `contextSignature` 逐字节不变）。
- **FX-28** 零散加固：陈旧 `instance.lock` 仅死 PID 回收、last-good 快照 + `vagent snapshot` / `vagent recover --from last-good`、trace 16 MiB 轮转、MCP 结果标注、同名同参重复调用提示、取消流 token 入 journal。
- **FX-29** 单次模型 60 s 上限提为模块常量 `MODEL_CALL_LIMIT_SECONDS`（决策 A：保留 60 s、不加配置项），README 与使用手册写明它是「固定上限，不是配置项」。

### 6. 时间线混音静音的根因修复（本版新增）

v0.6.1 起登记为「顺序相关偶发」的 `tests/test_m7_timeline_render.py::test_a_full_timeline_render_freezes_the_edit_and_replays` 在最近 10 次 CI 里失败 3 次，报错都是 `LOUDNESS_OUT_OF_RANGE`「（混音为静音，未交付）」。本版定位并修掉根因：

- **不是夹具时序。** CI 日志里没有 `（音频落空 N 条：…）` 后缀，说明 voice 轨已进入混音、`dropped` 为空；混音里至少有 2 秒真实旁白，因此「静音」只能来自测量。v0.6.1 发布说明中「问题在用例夹具」的判断据此**撤回**。
- **根因：截断的 `volumedetect` 报告被当成静音。** 包内 FFmpeg 7.1 偶尔在只处理一帧后以退出码 0 结束整张探测滤镜图，`volumedetect` 只报告 `n_samples: 4096`、`max_volume: -91.0 dB`；旧解析取**第一个** `max_volume` 匹配，读到 -91.0 即判定静音。本机修复前实测触发率：异步 `_run` **4/30**、同步 `subprocess` **3/30**（与 CI 的 3/10 同量级）；写成真实 WAV 而非 `-f null` 时 10 次均未截断。
- **修复**：`parse_volume_report()` 按 `n_samples` 归组、只采信**样本数最多**的摘要（`parse_volume_peak()` 保持为薄封装）；静音探测最多重试 3 次，只有覆盖时间线 **≥90%** 的探测才允许快速失败，始终不完整则**不判定静音**，交给 loudnorm / 复测 / 校正与交付门禁下结论。
- **用例**：新增 3 项（摘要选择、截断后重试成功、始终截断不误判）。细节与证据见 [M9 计划 §3.0.1](./M9_PLAN.md)。

### 7. 文档与版本号

README 的版本、验证数字与状态表更新到 v0.7.0，并新增 Harness 两行；M9 的发布号由 v0.7.0 改为 **v0.8.0**（[M9 计划](./M9_PLAN.md)、[M9 契约](./M9_CONTRACTS.md)）；[Harness 修复规划](./HARNESS_REVIEW_FIX_PLAN.md) 的排期表与「建议的下一步」标注本版已发布。

## 验证

- **全量回归**（本机 Windows 11 / Python 3.12.10 / 包内 FFmpeg 7.1）：**1,741 passed / 4 skipped**，1,867.07 秒（0:31:07），退出码 0，无失败、无 xfail。4 个跳过仍是既有平台条件（Windows 符号链接权限 3 例、POSIX 文件模式 1 例）。日志 `output/release-0.7.0-pytest.log`。
- **偶发用例的定向证据**：修复前，同一夹具的静音探测在本机 30 次里截断 4 次（异步 `_run`）/ 3 次（同步 `subprocess`），报错与 CI 完全一致；修复后同一条 `test_a_full_timeline_render_freezes_the_edit_and_replays` 连跑 **15/15 通过**（`output/stress-m7-render.log`），另有 3 项新用例锁定「取完整摘要」「截断后重试」「始终截断不判定静音」。
- **静态检查**：`ruff check src/vagent tests scripts` 通过；`ruff format --check` **258 files already formatted**。
- **前端**：11 个 JS 文件 `node --check` 通过；10 个状态模块 **91 项** Node 测试通过（Node v24.13.0）。
- **供应商契约回放**：**37/37** 通过，退出码 0（`scripts/verify_provider_contract.py`；无凭据、无 DNS、无付费任务）。
- **状态增长测量**（`scripts/measure_state.py`）：10/20/40 Run 为 **551,974 / 1,103,554 / 2,206,714 字节**，空事务中位数 **9.54 / 17.40 / 34.90 ms**，与 M3 记录的 0.55/1.10/2.21 MB、12/25/39 ms 同量级（机器负载不同）。
- **基线保护**：`docs/M9_E0_BASELINE.json`（SHA-256 `afd1b38f…59b694b`）与 `docs/M8_E0_BASELINE.json`（`f3dc91c1…cb3706`）本版未重新生成、未改动；168 个契约模型哈希、`contractsSha256` 与 `capabilitiesSha256` 由全量用例逐项校验通过。
- **安装态 wheel smoke**：**PASS**（`output/release-0.7.0-wheel-smoke.json`；23/23 项检查全为 true，8 组离线用例共 **40 项**，`temporaryDataCleaned=true`，退出码 0）。在独立环境 `tmp/wheel-env-070` 安装本版 wheel 后运行，`packagePath` 指向该环境的 `site-packages`（不是仓库副本）；全程离线、未配置任何供应商 Key。
- **四组 CI**：发布提交 [`3055da9`](https://github.com/shifang37/vAgent/commit/3055da9c5cae4c1c4023a3e66bc5fa1d95379e0b) 上四组矩阵全部 **success**（[运行 42794207234](https://github.com/shifang37/vAgent/actions/runs/42794207234)：ubuntu 3.11 22 分钟、ubuntu 3.12 32 分钟、windows 3.11 53 分钟、windows 3.12 64 分钟），其中包括此前 10 次里失败 3 次的整集时间线渲染用例。CI 覆盖 `ruff check` / `ruff format --check`、11 个前端文件的语法检查与 10 个模块的 Node 测试、全量 pytest、构建 wheel 并在独立环境安装后运行 `scripts/wheel_smoke.py`。标签 `v0.7.0` 指向该提交。推送标签会另触发一次同提交的运行（[42794504891](https://github.com/shifang37/vAgent/actions/runs/42794504891)）：ubuntu 3.11 组首次 `pytest` 失败、重跑该作业通过——同一提交、同一矩阵一次通过一次不通过，判定为偶发；该运行的日志已不可下载（`BlobNotFound`），因此没有直接定位到具体用例，推断与处置见「失败与偏差记录」第 5 条。本文件的 CI 一行与提交计数在发布后的文档补记里定稿，代码与发布提交完全相同。

## 构建产物

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `shifang37_vagent-0.7.0-py3-none-any.whl` | 1,362,258 字节 | `9592806a9cb9e205a31fc61c2c9267bc961ac53334cce1e1a0aab6c050aba571` |
| `shifang37_vagent-0.7.0.tar.gz` | 2,588,076 字节 | `1fdd51f70f657ce9671551ee893056dae724d25ec73b669200885b97ba0b3d75` |

哈希为本机构建实测值；从同一提交重新构建不保证逐字节一致。sdist 打包的是本文件定稿前的副本（`/docs/*.md` 进 sdist），因此 sdist 内不含本节的最终哈希；wheel 不含 `docs/`，其哈希不因本文件而后变。

## 安装

要求 Python 3.11+。从本仓库构建并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# 或安装已构建的 wheel：
.\.venv\Scripts\python.exe -m pip install dist\python\shifang37_vagent-0.7.0-py3-none-any.whl
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\vagent web --mcp-local
```

## 兼容与升级

- 存储仍为 **schema 9**：本版不新建、不迁移、不改任何必需集合；168 个契约模型哈希与 `state.json` 必需字段逐字节不变。
- **新增的只有旁路与可选字段**：`submission_log.py` 的 WAL 目录、`gc.json`（`vagent gc` 的标记）、last-good 快照文件、trace 轮转段。删除这些文件不影响成片、时间线、季、批量、账单与 Job 记录。
- **新开关全部默认关闭或保持既有默认**：`VAGENT_CONTEXT_VERSION` 缺省 2、`VAGENT_MEMORY` 缺省 off、`VAGENT_CLOSING_TURN` 产品路径缺省 on（库默认 off）。不设任何开关时，本版与 v0.6.1 的上下文与预算语义一致。
- **`MODEL_CALL_LIMIT_SECONDS = 60.0`** 是固定上限而不是配置项：它同时是在途预留额与 `MODEL_TIMEOUT` 判定依据，因此未新增 `RunPolicy` 字段或环境变量（[规划 §20](./HARNESS_REVIEW_FIX_PLAN.md)）。
- `PACKAGE_VERSION` 保持 `0.6.0`：它标识媒体格式代次而不是包版本，沿用 M8 的冻结口径。
- 仍不支持 9:16 竖屏（改为 v0.8.0 续做）、不换汇、不做发送后的自动重投。

## 失败与偏差记录

1. **「顺序相关偶发」的旧判断被推翻。** 见上文第 6 节：根因是探测报告截断，不是夹具时序；旧记录保留在 [v0.6.1 发布说明](./RELEASE_0.6.1.md) 与 [M9 计划 §3.0](./M9_PLAN.md)，本版在其后追加根因与修复。
2. **M3 的性能门禁未达成，如实记录。** 规划 §8.5 要求空事务耗时降到 2.77 MB 场景的 50% 以下；本版实测 10/20/40 Run 为 0.55/1.10/2.21 MB、空事务 12/25/39 ms，事务成本仍由每条 Run 的完整 `messages` 决定。把 `runs[].messages` 旁路到独立文件需要 schema 讨论与只读回退，按计划留给 M4 的 C2。
3. **`vagent gc` 与快照是可选运维命令，未接入自动流程。** 本版不自动删除任何历史数据，`gc` 默认 dry-run。
4. **本机验证环境说明。** 全量回归、安装态 wheel smoke 与状态增长测量在本机 Windows（Python 3.12.10、包内 FFmpeg 7.1）上完成；四组 CI（ubuntu / windows × Python 3.11 / 3.12）在发布提交 `3055da9` 上全部通过，与上节记录一致。
5. **发布后修正了一处新用例自身的偶发断言（`6ad8c74`）。** 本版新增的「截断后重试」用例让**真实**探针回答第二次尝试，而真实探针本身就有约 10% 的截断率（见第 6 节），于是「正好两次探测」的断言在约十分之一的运行里会变成三次。现在两次探测都改为脚本化（第一次 1 帧报告，第二次按 2 秒时间线给出完整报告），其后的 loudnorm 各遍仍是真实调用；该文件本地连跑 8 次全绿，`6ad8c74` 的四组 CI（[运行 42807778296](https://github.com/shifang37/vAgent/actions/runs/42807778296)）通过。上面 ubuntu 3.11 的那次偶发与它一致，但由于原始日志不可下载，这是**推断**而不是证据。标签 `v0.7.0` 仍指向发布提交 `3055da9`，本修正作为发布后的第一个提交跟进。

## 已知限制

- **Kling / fal 仍无真实付费样本**：缺 `VAGENT_KLING_API_KEY`/`FAL_KEY`，能力表保持「文档支持，未做真实样本」，`capabilitiesSha256` 不变。
- 万相与 CosyVoice 的供应商实际扣费仍未核对，账本依赖人工录入，缺失时保持 `unknown` 而不是 0。
- 空事务成本未达 M3 目标，等待 C2 把 Run 消息旁路到独立文件。
- 9:16 竖屏按产品决定暂缓，M9 E0 契约与基线保留，E1 起未开始；发布号改为 v0.8.0。
- 单次模型请求固定 60 秒上限，接近输出上限的长回复可能被截断（见 [使用手册](./USER_GUIDE.md)）。
- 公共 PyPI 发布未做；本版只提供仓库内构建的 wheel/sdist。

## 证据索引

- Harness 修复： [Harness 缺陷修复规划](./HARNESS_REVIEW_FIX_PLAN.md)（§13–§20 为四批实施记录与验证）、H0–H6 验收（[H0](./AGENT_HARNESS_H0_ACCEPTANCE.md)、[H2](./AGENT_HARNESS_H2_ACCEPTANCE.md)、[H3](./AGENT_HARNESS_H3_ACCEPTANCE.md)、[H5](./AGENT_HARNESS_H5_ACCEPTANCE.md)、[H6](./AGENT_HARNESS_H6_ACCEPTANCE.md)、[上下文摘要](./CONTEXT_SUMMARY_ACCEPTANCE.md)、[行为评测](./AGENT_BEHAVIOR_EVAL_ACCEPTANCE.md)）
- 偶发静音根因：[M9 计划 §3.0.1](./M9_PLAN.md)、`output/stress-m7-render.log`
- 离线与安装态证据：`output/release-0.7.0-pytest.log`、`output/release-0.7.0-wheel-smoke.json`、`output/v0.7.0-measure-state.log`、`output/provider-contract.json`
- 基线：`docs/M9_E0_BASELINE.json`（SHA-256 `afd1b38f…59b694b`）、`docs/M8_E0_BASELINE.json`（SHA-256 `f3dc91c1…cb3706`）
- 上一版：[v0.6.1 发布说明](./RELEASE_0.6.1.md)
