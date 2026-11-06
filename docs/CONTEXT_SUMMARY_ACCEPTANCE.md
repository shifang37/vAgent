# 上下文压缩（摘要）验收

日期：2026-11-04。范围：Agent Harness 落差收口计划的 H1 轨道——用摘要压缩替代纯截断，在保留完整工具协议的前提下，让被裁掉的旧轮次以压缩摘要的形式继续可达。不改动任何已冻结契约；默认关闭，显式开关。

## 决策

- **新增 `contextVersion=3`，但新 Run 默认仍是 v2。** 依据：M2/M3/M5 基线与多个已交付用例把产品 Run 的 `contextVersion` 钉在 2（`docs/M3_E0_BASELINE.json`、`test_m2_clients.py`、`test_context_optimization.py`）。把默认改成 3 会改动这些基线，违反「契约基线不变」。因此 v3 为**显式开关**：`VAGENT_CONTEXT_VERSION=3` 或直接构造 `ContextBuilder(format_version=3)`；不加该变量时行为与现状逐字节相同。
- **摘要缓存是侧车文件，不进 `state.json`。** `<home>/summaries.json`（`summaries-v1`），内容寻址：键 = SHA-256（提示词版本 + 摘要器指纹 + 被摘要消息的规范 JSON）。因此不新增 schema 集合、不动 `DatabaseV9` 与 168 个模型哈希。
- **默认摘要器离线、确定性、零费用。** `ExtractiveSummarizer` 不调用模型，因此「同一段历史只摘要一次」天然成立；模型版摘要器可注入，同一 span 也只调用一次（侧车命中即复用）。
- **摘要失败或超预算一律降级回截断。** 摘要只是优化，不是正确性要求；降级路径**重新构建**普通截断，消息与字节同 v2 逐字节相同。（v0.6.3 修复：此前失败时保留的是「为摘要预留窗口」的更少消息；超预算时事件也已提前发出。）
- **稳定前缀不变。** v3 只在 v2 前缀之后追加摘要块；固定规则、Skill 元信息、项目事实前缀逐字节不变（`test_v3_without_summarize_keeps_the_exact_v2_layout`）。
- **签名覆盖摘要指引。** v3 的 `contextSignature` 额外包含 `historySummaryGuidance`（`SUMMARY_HEADER` 文本），改文案即改签名；v2/v1 签名不受影响（`test_v3_signature_covers_the_summary_guidance`）。

## 工作机制

1. `ContextBuilder.build(..., summarize=True)` 先**预留**一段摘要预算（`summary_target_bytes(budget)+64`），据此选出「放得下摘要时仍能保留的最新若干整轮」窗口；窗口之外的消息即待摘要 span。
2. Runner 用 `span_key` 查侧车：命中则复用，未命中才调用摘要器一次并写回；`_summarize` 本身不发事件，由调用点按**最终是否采用**决定发什么。
3. 第二次 `build(..., history_summary=...)` 用真实摘要重建；只有放进预算才采用（`report.summary is not None`）。采用时发一次 `context.summarized`（`applied=true`、`reused`、`summaryBytes`）并记 `context.summarize` span `applied=true`；未采用时发 `context.summary_rejected`（`reason="budget"`），span `applied=false`，且**重建**普通截断，与 v2 逐字节相同。
4. span 是**整轮**，且摘要覆盖**整个** span（先详细、放不下则每轮一行、再放不下则等距抽样），不会只记住最早几条而丢掉临近窗口的轮次。

`summary_target_bytes(budget) = min(4096, max(256, budget // 16))`。

## 离线可比数字（对「截断」）

固定样本：40 轮（每轮 user+assistant+tool_result，均含唯一事实 `F0..F39`），预算 12,000 字节，内置 `project_read` 工具 Schema，`ExtractiveSummarizer`。

| 方案 | 上下文字节 | 保留消息数 | 裁掉消息数 | 事实可召回 |
|---|---:|---:|---:|---:|
| 不截断（超预算，不可用） | 30,368 | 121 | 0 | 40 / 40 |
| v2 纯截断 | 11,903 | 45 | 75 | **17 / 40** |
| v3 降级（摘要器失败 / 摘要超预算） | 11,903 | 45 | 75 | **17 / 40** |
| v3 摘要（本次） | 11,151 | 39 | 81 | **23 / 40** |

摘要块 599 字节 / 7 行。v3 在预算内多召回 6 条事实，且上下文反而更小：它用「更小的保留窗口 + 覆盖全 span 的摘要」换取对最早历史的记忆，而不是把它们丢给截断。降级样本与 v2 完全同字节（11,903 / 45 / 75），不再残留「为摘要预留窗口」的更少消息。

复现：`python tmp/measure_h1.py`（离线，无网络、无模型调用）。

## 自动化验证

`tests/test_context_summary.py`（12 项）：

| 场景 | 断言 |
|---|---|
| v3 不开摘要 | 与 v2 逐字节相同（消息、字节、裁掉数） |
| v3 摘要 | 预算内、含 `SUMMARY_HEADER`、可召回 v2 已丢弃的 `F0`、工具配对完整 |
| 摘要超预算 | 回退纯截断，`summary is None`，与 v2 布局一致 |
| 摘要器失败 | 与 v2 **逐字节相同**（消息与 `contextBytes` 全等），不发 `context.summarized` |
| 摘要被预算拒绝 | 不发 `context.summarized`；发一条 `context.summary_rejected`；span `applied=false`；消息与 v2 全等 |
| 摘要器 | 确定性、字节受限、保留工具结果 |
| `span_key` | 相同内容同键；换内容或换指纹则换键 |
| 侧车 | 往返；损坏文件视为未命中并可重建；超限按时间淘汰最旧（不是按键序） |
| Runner（v3） | 应用摘要一次、发 `context.summarized`；关进程重开后同一 span 命中侧车、**不再调用摘要器**、发 `context.summary_reused` |
| 开关 | 默认 2；`VAGENT_CONTEXT_VERSION=3` 生效；非法值报 `INVALID_CONTEXT_VERSION` |
| 签名 | v3 与 v2 签名不同；同版本稳定 |

## 兼容与边界

- v1/v2/v3 均在 `CONTEXT_VERSIONS` 内，`for_version` 与恢复路径按记录版本重建；未识别的版本仍拒绝恢复。
- 无摘要器、`summarize=False` 或摘要超预算时，v3 与 v2 等价，故打开 v3 不会比现状更差。
- 模型版摘要器若注入，其调用**不计入** Run 的模型步/工具预算，也不并入 Run 的 usage 总数（属维护调用），只发 `context.summarized` 事件并在侧车留痕；本仓库默认不注入模型版，因此默认零费用。
- 摘要侧车按 `createdAt` 时间淘汰（`MAX_ENTRIES=512`，不是按 JSON 键序），读取按 `(st_mtime_ns, st_size)` 缓存；恢复与连续步内同一 span 只解析一次。
- 不做向量记忆（H2）与提示词工程（H3）。
