# H0 设计与代码落差收口验收

日期：2026-11-04。范围：Agent Harness 落差收口计划的 H0 轨道——把 `AGENT_HARNESS_DESIGN.md` 的承诺逐条同代码核对，能低成本落地的实现，其余在文档标注理由。本轨道不改动任何已冻结契约、工具面或 `state.json` schema；168 个模型哈希、`routeProbe.rowsSha256` 与 `DatabaseV9` 不变。

## 逐项决策

| 设计承诺 | 位置 | 代码现状（核对前） | 决策 | 证据 |
|---|---|---|---|---|
| `waiting_user` 状态 | §7.1 | 不存在该状态；追问以 `completed` Run 的 `answer` 返回 | **文档标注**：追问即一次正常完成的 Run，下一条回答建立新 Run | 见下「决策理由」 |
| 参数纠错上限 | §4.2 | 无计数器，相同 Schema 错误不封顶 | **已实现**：连续两次相同 Schema 错误即停止 | `SCHEMA_FEEDBACK_LIMIT`、`tests/test_harness_gaps.py` |
| 运行级排队 | §4.1.2 | 第二个请求直接 `RUN_BUSY` | **文档标注**：采用「用户停止旧 Run 后发起新 Run」分支 | 见下 |
| `video_generate` / `shot_redo` 的 `confirm` | §5.1、§8 | 二者无 `confirm`；批量工具均有 | **文档标注**：保留为低层工具，由次数/费用上限约束 | 见下 |

## 决策理由

- **`waiting_user`**：把它做成独立 Run 状态需要扩展 `RunRecord` 的 `status` 字面量集合，直接改动冻结的 `state.json` schema 与模型哈希。设计 §7.1 自身的说明已写明「新需求或回复追问建立新 Run」，当前「追问以完成态回复、追问后的回答另起一个 Run」完全满足该语义。因此只更新设计文档，不新增状态。
- **运行级排队**：设计 §4.1.2 明确给出两种可选行为——「明确排队」或「用户停止旧 Run 后发起新 Run」。产品选择后者：`RUN_BUSY` 是稳定错误码，Web/CLI 在运行中禁用发送并提示先停止。持久化排队需要新增待执行 Run 记录与新状态机语义，属于改 schema 的范围，不在本次收口内。
- **`confirm`**：`video_generate` 是单 Job 的低层登记工具，`shot_redo` 本就要求「只由用户显式发起」。二者已受 `video_mode` 开关、每 Run 新建 Job 上限（默认 4，模拟 1）与逐镜头单价/费用上限约束；批量入口（`shot_generate`/`episode_generate`/`season_generate`/`audio_generate`/`film_render`）才要求显式 `confirm`。给低层工具加 `confirm` 会改动已公布的 JSON Schema，从而改动每个 Run 的 `contextSignature` 并拒绝旧 Run 恢复——与「不改工具面」冲突，故不改。

## 已实现：参数纠错上限

设计 §4.2 承诺「连续相同 Schema 错误最多向模型反馈 1 次后停止」。实现方式：

- 新增纯函数 `schema_feedback_exhausted(messages)`（`src/vagent/runner.py`）：从已提交的消息历史推导「每一批工具调用」的 Schema 错误签名（`（工具名, 错误码）`，错误码限于 `INVALID_ARGUMENTS` 与 `INVALID_TOOL_CALL`），当最后两批的签名相同且非空时返回 `True`。
- `tools_node` 在结果落盘后判定：命中即抛 `SCHEMA_FEEDBACK_LIMIT`，Run 以 `failed` 结束，本批工具结果与已提交产物保留，工具调用配对完整。
- 判定只依赖消息历史，**不新增持久字段**，因此跨显式恢复也能正确判定，且不动任何 schema。

第一次 Schema 错误照常作为 `INVALID_ARGUMENTS` 工具结果反馈给模型（现有行为）；第二次相同错误不再反馈，直接停止。

## 额外落差（本轨道新发现，计划未列出）

| 落差 | 位置 | 现状 |
|---|---|---|
| `run.failed` 事件从未发出 | §7.3 | 失败只表现为 `run.completed` 且 `status:"failed"`；无独立 `run.failed` |
| `artifact.updated` 事件从未发出 | §7.3 | 产物变更无广播事件，客户端靠快照 diff |
| 「只读操作可有限重试」未实现 | §4.2 | MCP 只读工具显式「未自动重试」；本地只读无自动重试，回退为显式恢复 |
| 推理模式协议元数据保留未验证 | §6.2 | 尚未接入 thinking 模式（属 H6），无对应测试 |

以上四项记为**已知落差**：不改行为，登记在此供后续轨道（H3/H5/H6）取用。

## 自动化验证

`tests/test_harness_gaps.py`（新增，4 项）：

| 场景 | 断言 |
|---|---|
| 连续两次相同 `INVALID_ARGUMENTS` | Run `failed`、`errorCode=SCHEMA_FEEDBACK_LIMIT`、`model.calls==2`、协议完整 |
| 交替出现的两种不同 Schema 错误 | 不封顶，Run 正常 `completed` |
| 一次 Schema 错误后模型修正 | Run `completed`，无错误码 |
| 判定函数对（相同/中间成功/非 Schema 质量错误/单批）四类历史 | 返回符合预期 |

回归：`tests/test_harness_gaps.py tests/test_runner.py tests/test_context_optimization.py tests/test_context_skills.py` 共 **49 passed, 1 skipped**（跳过项为本机无符号链接权限的既有用例）。Ruff lint/format 通过。本轨道未调用任何模型，未联网。
