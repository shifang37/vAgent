# 行为评测（成功、工具选择精度、pass@1 门禁、匹配与 judge）验收

日期：2026-11-06（初版 2026-11-04；M2 / FX-16 可信化修订）。范围：Agent Harness 落差收口计划的 H4 轨道——会话级行为评测与打分：任务成功率、工具选择精度（惩罚多余调用）、pass@1 门禁、最大二分匹配与可选离线 judge。不改动任何已冻结契约；默认离线、零费用、进 CI。

## 决策

- **老实标注，不冒充真实模型行为。** 每个样本仍由 `ReplayModel` 逐步回放脚本；它**不读取**提示词与工具列表。`mode="offline-replay"`、`LIMITATIONS` 与本文都明确：离线回放只证明评分器、pass@1 门禁与报告口径，不证明真实模型质量，也不覆盖提示词/上下文回归。
- **`k` 不得超过样本数。** `1 <= k <= min(len(case.samples))`，否则 `EVAL_K_TOO_LARGE`（`k < 1` 为 `EVAL_K_INVALID`）；不再重复最后一个脚本补样本，因此 `pass@1` 不会随 `k` 漂移。
- **门禁改用 pass@1。** 用例分 `gate`（每个被评估样本都必须通过）与 `measure`（只报告指标）两组。`status="passed"` 仅当存在 gate 用例且全部 gate 的 `pass@1 == 1.0`；measure 用例的 50% 不会把门禁抬过去，measure-only 运行也不会被判为通过。
- **`pass@k` 与方差降为描述性指标。** 继续报告，但不参与门禁；真实模型的 pass@k 估计与置信区间属于 M4/C4，不在离线套件里假装。
- **调用匹配改最大二分匹配。** `score_calls` 用增广路（Kuhn）求最大一对一匹配；贪心会把通用期望消耗在具体调用上、漏掉后续可匹配的调用，导致 recall/precision 少算。
- **`suite_id` 纳入完整脚本。** 每个 sample 的文本、工具名、参数与调用 ID 全部进入哈希；改脚本必改 ID。提示词变化也会改 ID，但离线结果不会变——这正是「脚本不依赖输入」的证据。
- **评分器变异测试。** 4 个负样本各自只违反 `completed` / `artifacts` / `project` / `tools` 中的一个条件；删掉任一条件，对应负样本会变成通过，测试立即失败。
- **`ReplayModel` 增加结构断言。** 若一次调用没有工具列表，而脚本要调工具，明确抛 `REPLAY_SCRIPT_NEEDS_TOOLS`，暴露「脚本不依赖输入」的事实，而不是静默回放。
- **judge 只记录、不判定。** 默认离线 `RubricJudge`（命中 rubric 短语的比例），可替换；judge 分数不影响样本是否通过。
- **报告不覆盖历史。** 输出文件已存在即 `EVAL_OUTPUT_EXISTS`。
- **预算上限不提升。** 样本用产品默认 `RunPolicy`（8 步 / 12 次工具 / 180 秒）。

## 指标定义

| 指标 | 定义 |
|---|---|
| `passed`（样本） | `completed` 且期望工具全部命中（recall=1）且多余调用 ≤ `allowed_redundant` 且产物/项目断言成立 |
| `pass@1` | 该用例被评估样本的通过率；gate 用例必须为 1.0 |
| `pass@k` | 该用例至少一个样本通过则为 1；仅描述，不门禁 |
| `variance` | 该用例样本通过指示量的总体方差；仅描述 |
| `precision` | 最大匹配命中数 / 实际调用数；多余调用拉低精度 |
| `recall` | 最大匹配命中数 / 期望调用数；漏调用拉低召回 |
| 调用匹配 | 最大一对一匹配：工具名相同且期望参数是被调用参数的子集 |
| `redundantCalls` / `missingCalls` | 最大匹配后仍未匹配的多余 / 缺失调用总数 |

`allowed_redundant` 允许容忍「参数纠错重试」这类合理多余调用——一次 Schema 错误重试不算过度调用。

## 内置黄金集与结果

5 个用例 × 2 样本：3 个 `measure` 保留原有的 50% 样本，2 个 `gate` 要求两个样本都通过。

| 用例 | 组 | 期望工具 | 样本通过 | pass@1 |
|---|---|---|---:|---:|
| `brief-and-memory` | measure | project_update, artifact_save(kind=brief) | [✓, ✗] | 0.5 |
| `answer-without-tools` | measure | （无） | [✓, ✗] | 0.5 |
| `recover-from-bad-arguments` | measure | project_update(audience), artifact_save(brief) | [✗, ✓] | 0.5 |
| `golden-brief-and-report` | gate | project_read, artifact_save(kind=brief) | [✓, ✓] | 1.0 |
| `golden-project-memory` | gate | project_update(goal, audience) | [✓, ✓] | 1.0 |

样本 0/1 的真实差异：`brief-and-memory` 与 `answer-without-tools` 都是样本 0 通过、样本 1 因多余 `project_read` 失败；`recover-from-bad-arguments` 是样本 0 连续两次相同 Schema 错误（同时未完成、无产物），样本 1 正确。两个 gate 用例的两个样本都通过。

汇总（`--k 2`）：

| 指标 | 值 |
|---|---:|
| 用例 / 样本 | 5 / 10（gate 2 / measure 3） |
| 通过样本 | 7（successRate 0.70） |
| pass@1 / pass@k | 0.70 / 1.00 |
| gate pass@1 / measure pass@1 | 1.00 / 0.50 |
| failedGates | `[]` |
| 方差 | 0.15 |
| 工具选择 precision / recall | 0.72 / 0.90 |
| 多余 / 缺失调用 | 6 / 2 |
| 真实供应商 / 文本模型调用 | 0 / 0 |
| judge（离线 rubric） | 0.80 |
| status | `passed` |

复现：`python scripts/evaluate_behavior.py --home <空目录> --output <新路径> --k 2`。

## 自动化验证

`tests/test_behavior_evaluation.py`（18 项）：

| 场景 | 断言 |
|---|---|
| 全套离线运行 | status passed、5 用例 / 10 样本、gate 2 / measure 3、pass@1=0.70、gatePass@1=1.00、pass@k=1.00、方差=0.15、真实调用 0、报告=内存 |
| 报告不覆盖 | 二次运行 `EVAL_OUTPUT_EXISTS`，原文件不变 |
| `--live` | 拒绝且不写文件、不发请求 |
| 确定性 | 同输入两次报告（含 judge）逐字节相同 |
| 评分器反例 | 漏调用 → recall<1；多余调用 → precision<1；参数不符不算命中；空期望+空调用=1.0 |
| 最大二分匹配 | 通用 + 具体两条期望、具体 + 通用两条调用 → 2/2，不再少算 |
| `suite_id` | 改提示词、改脚本文本、改工具参数、改分组都会改变 ID |
| `k` 上限 | `k=3 > 2` → `EVAL_K_TOO_LARGE`；`k=0` → `EVAL_K_INVALID`；不创建数据目录 |
| pass@1 门禁 | 50% 的 measure 不影响状态；50% 的 gate 失败并出现在 `failedGates`；通过的 gate 在 k=1 与 k=2 都通过；measure-only 不判通过 |
| 评分器四条件变异 | `completed` / `artifacts` / `project` / `tools` 四个负样本各自只有该条件为 false，`passed=false`；删掉任一条件测试即失败 |
| ReplayModel 结构断言 | 无工具 + 脚本要调工具 → `REPLAY_SCRIPT_NEEDS_TOOLS` |
| pass@k / 方差 | 单用例样本 [✗,✓] → pass@1=0.5、pass@k=1.0、方差 0.25 |
| judge | 确定性、rubric 比例为分、空 rubric=1.0、不影响通过 |
| 离线性 | 引擎源码不含 httpx/requests/socket/DeepSeekModel/random |

`tests/test_docs.py` 同时锁定「3 个命令 + 7 个开关 + 60 秒限制」的文档，以及 Application 层把 `VAGENT_MEMORY` / `VAGENT_CONTEXT_VERSION` / `VAGENT_PROMPT_VERSION` 真正接到 runner 的端到端用例。

## CI 与边界

- 离线用例进既有四组 CI；`scripts/evaluate_behavior.py` 默认离线，live 明确拒绝。
- 不追求大样本生产成功率：证明评分口径与可复现优先；真实质量需另立授权样本。
- 离线套件**不能**证明提示词回归：提示词改了，`suite_id` 会变，但回放结果不会变；H3 文档已删除「一并覆盖提示词回归」的说法。
- `--k` 只运行前 `k` 个脚本，且必须 `k <= 样本数`；需要更多样本时应新增脚本，而不是重复最后一个。gate 用例的脚本数为 2，发布门禁使用默认 `k=2` 时即跑全样本。
- `measure` 用例只报告指标，不参与发布门禁；只有 gate 用例决定退出码。
- 不做自动调优、不接外部评测平台，也不在离线套件里伪造真实模型的 pass@k 置信区间（留给 M4/C4）。
