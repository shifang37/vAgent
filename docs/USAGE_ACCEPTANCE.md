# 模型与缓存 Token 用量观测验收

日期：2026-09-29。范围：原任务二——记录缓存命中、未命中 Token 和模型调用次数，保留未知项，为后续成本分析提供数据。

## 实现与查询

```text
vagent usage --session coffee
vagent usage --run RUN_ID
```

查询无需 Key，不调用模型，不输出用户提示词或产物正文。`run/chat/resume` 输出摘要，`inspect` 包含 Run 的 `usage` 汇总；需要详细数据时使用 `usage`。

- `state.json` 的 `runs[runId].modelCalls` 保存每次调用的模型步骤、状态、开始/结束时间、耗时、输入/输出 Token、缓存命中/未命中 Token 和脱敏错误码。
- 模型适配器即将调用时登记记录。模型步骤已预留但在适配器调用前取消时，不计入模型调用数。这一计数表示应用调用尝试，不保证供应商已收到或计费。
- 响应的用量先于工具协议验证记录；即使响应中的工具调用不合法，已报告的 Token 也不会丢失。调用明细与原有输入/输出累计量在同一 JSON 事务中更新。
- DeepSeek 原始 usage 字段优先使用，避免 SDK 将缺失字段归一化为 0 后误报。标准 `input_token_details.cache_read` 可用于推导未命中输入量，`cache_creation` 不作为 DeepSeek 缓存未命中量。
- 单次缓存数据来源为 `reported/derived/missing/invalid`。字段缺失为 null；非法类型、负数或命中/未命中与总输入矛盾的缓存数据不计入有效统计。
- Run 汇总保留完整性标志与覆盖调用数。命中率只使用同时具有命中和未命中数据的调用，按 Token 数加权；分母为零时返回 null。只有单边缓存数据时仍保留已知的单边总量，但该调用不参与命中率。
- 失败、中断请求的缺失用量不会填成 0；重启将尚在 started 状态的记录标记 interrupted，准确结束时间和耗时保持未知。
- 恢复继续累计，重复提交原 request ID、恢复已完成 Run 和重放工具不重复记录既有模型调用。若恢复需要再次调用模型，新调用单独计数。
- 旧 Run 保留原有输入/输出总量，不回填历史调用和缓存明细。`untrackedModelSteps` 标记缺口；存在缺口时 `modelCallCount` 为 null，`recordedCallCount` 仍可查看新增明细数。

## 自动化验证

执行 `python -m pytest -q`：77 passed、1 skipped（本机 Windows 无符号链接权限）。主要新增测试位于 `tests/test_usage.py`，并扩展了 `tests/test_resume.py` 的真实子进程强退验证。

Ruff lint/format、`git diff --check`、sdist/wheel 构建通过。将本次 wheel 安装到独立虚拟环境后，在仓库外完成离线 Run 的恢复与 `usage --run` 查询：累计调用 4 次、最终一个产物，未报告的缓存信息仍为 null；`pip check` 通过。上述检查在本机 Windows / Python 3.12 执行，未宣称远端 CI 已通过。

| 场景 | 结果 |
|---|---|
| 模拟 HTTP 返回 DeepSeek 原始缓存字段 | 经实际 ChatDeepSeek 适配器进入 Run 明细和汇总，重启后保持 |
| 第一次输入 100、命中 90；第二次输入 10、命中 0 | 汇总命中率为 90/110，约 81.82%，而非单次比例均值 45% |
| 缓存字段缺失、usage 为空对象或 null | 未知字段保持 null，完整性标志为 false |
| 标准 cache_read、明确 0 命中、0 输入 | 正确推导未命中量；零分母不产生假命中率 |
| 负数、字符串、布尔值、计数矛盾 | 缓存数据标记 invalid，不宣称有效节省 |
| 只有单边缓存字段 | 保留已知值，不推测未知值或将其用于不完整的比率 |
| 响应包含非法工具调用 | Run 失败，但该次响应已报告的 Token 仍保留 |
| 模型失败、取消或强退后 resume | 旧用量保持，新调用新增；未知记录不混成零消耗 |
| 重复 request ID、再次恢复已完成 Run | 调用数和 Token 不增加 |
| 旧版无调用明细的 Run 继续执行 | 原总量保留，新明细追加，旧步骤缺口明确展示 |
| 无 Key、仓库目录外运行 usage | 可按会话或 Run 查询，不展示对话内容 |
| 离线 Demo | 记录适配器调用次数，Token 与缓存信息保持未报告 |

这些结果来自离线模型、模拟 HTTP 和本地子进程，不代表真实 DeepSeek 的缓存命中率、模型质量或实际账单。未启用 Redis、回答缓存、上下文重排或价格估算。
