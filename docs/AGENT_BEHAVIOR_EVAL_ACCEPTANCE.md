# 行为评测（成功、工具选择精度、pass@k、judge）验收

日期：2026-11-04。范围：Agent Harness 落差收口计划的 H4 轨道——补上「会话级行为评测与打分」：任务成功率、工具选择精度（惩罚多余调用）、pass@k 与方差、可选离线 judge。不改动任何已冻结契约；默认离线、零费用、进 CI。

## 决策

- **评测引擎放 `src/vagent/behavior.py`，黄金集与 CLI 放 `scripts/evaluate_behavior.py`。** 引擎可导入、可单测；脚本沿用既有 `run_suite(args)` + 原子写报告 + 拒绝覆盖的约定（与 `scripts/evaluate_m8.py` 一致）。
- **完全离线、确定性。** 每个样本用脚本模型逐步回放；因此 `textModelCalls=0`、`realProviderCalls=0`，同一输入两次运行报告逐字节相同（`test_identical_inputs_give_an_identical_report`）。`--live` 直接拒绝（无付费样本授权）。
- **评分器与用例分离**，故可做变异测试。
- **预算上限不提升。** 样本用产品默认 `RunPolicy`（8 步 / 12 次工具 / 180 秒），评测不放大预算。
- **judge 只记录、不判定。** 默认离线 `RubricJudge`（命中 rubric 短语的比例），可替换；judge 分数不影响样本是否通过（`test_judge_scores_are_recorded_but_do_not_change_pass`）。
- **报告不覆盖历史。** 输出文件已存在即 `EVAL_OUTPUT_EXISTS`。

## 指标定义

| 指标 | 定义 |
|---|---|
| `passed`（样本） | `completed` 且期望工具全部命中（recall=1）且多余调用 ≤ `allowed_redundant` 且产物/项目断言成立 |
| `precision` | 命中数 / 实际调用数；多余调用拉低精度 |
| `recall` | 命中数 / 期望调用数；漏调用拉低召回 |
| 调用匹配 | 一对一并贪心：工具名相同且期望参数是被调用参数的子集 |
| `pass@1` | 样本通过率 |
| `pass@k` | 该用例至少一个样本通过则为 1 |
| `variance` | 该用例样本通过指示量的总体方差 |
| `redundantCalls` / `missingCalls` | 多余 / 缺失调用总数（过度调用与欠调用的量化） |

`allowed_redundant` 允许容忍「参数纠错重试」这类合理多余调用——一次 Schema 错误重试不算过度调用。

## 内置黄金集与结果

3 个用例 × 2 样本（样本 0 故意用「多余调用 / 不保存 / 连续相同参数错误」制造失败，样本 1 正确）：

| 用例 | 期望工具 | 样本通过 | pass@1 | pass@k |
|---|---|---:|---:|---:|
| `brief-and-memory` | project_update, artifact_save(kind=brief) | [✓, ✗] | 0.5 | 1.0 |
| `answer-without-tools` | （无） | [✓, ✗] | 0.5 | 1.0 |
| `recover-from-bad-arguments` | project_update(audience), artifact_save(brief) | [✗, ✓] | 0.5 | 1.0 |

汇总（`--k 2`）：

| 指标 | 值 |
|---|---:|
| 用例 / 样本 | 3 / 6 |
| 通过样本 | 3（successRate 0.50） |
| pass@1 / pass@k | 0.50 / 1.00 |
| 方差 | 0.25 |
| 工具选择 precision / recall | 0.53 / 0.83 |
| 多余 / 缺失调用 | 6 / 2 |
| 真实供应商 / 文本模型调用 | 0 / 0 |
| judge（离线 rubric） | 0.67 |

第三例的第 0 个样本同时验证了 H0 的参数纠错上限（连续两次相同 Schema 错误 → `SCHEMA_FEEDBACK_LIMIT` → 样本失败）。

复现：`python scripts/evaluate_behavior.py --home <空目录> --output <新路径> --k 2`。

## 自动化验证

`tests/test_behavior_evaluation.py`（新增，9 项）：

| 场景 | 断言 |
|---|---|
| 全套离线运行 | status passed、3 用例 / 6 样本、pass@1=0.5、pass@k=1.0、方差=0.25、真实调用 0、报告=内存 |
| 报告不覆盖 | 二次运行 `EVAL_OUTPUT_EXISTS`，原文件不变 |
| `--live` | 拒绝且不写文件、不发请求 |
| 确定性 | 同输入两次报告（含 judge）逐字节相同 |
| 评分器变异测试 | 漏调用 → recall<1；多余调用 → precision<1；参数不符不算命中；空期望+空调用=1.0 |
| pass@k / 方差 | 单用例样本 [✗,✓] → pass@1=0.5、pass@k=1.0、方差 0.25 |
| judge | 确定性、rubric 比例为分、空 rubric=1.0、不影响通过 |
| 离线性 | 引擎源码不含 httpx/requests/socket/DeepSeekModel/random |

## CI 与边界

- 离线用例进既有四组 CI；`scripts/evaluate_behavior.py` 默认离线，live 明确拒绝。
- 不追求大样本生产成功率：证明评分口径与可复现优先；真实质量需另立授权样本。
- 不做自动调优、不接外部评测平台。
