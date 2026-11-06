# 编排与成本：Token→金额估算验收（H6 第 1 项）

日期：2026-11-05。范围：Agent Harness 落差收口计划的 H6 轨道——计划写明「实验性、择优纳入」，本文件只覆盖**已纳入的一项**：把已记录的 Token 用量换算成金额，并给出「缓存命中到底省了多少」的可比数字。其余候选（并行工具调用 / thinking 模式 / Redis 收益量化 / plan-and-execute）**未做**，理由见文末「未纳入的候选」。

## 决策

- **只算不存，钱不进 `state.json`。** 价格与金额都是**读时投影**：`usage --cost` 从既有 `modelCalls` 的 token 计数算出金额，不写任何字段。因此 `state.json` schema、168 个模型哈希、恢复语义全部不动——这与 H1/H2 把新状态放旁路是同一套纪律。
- **默认关闭，不开就是原样。** 不加 `--cost` 时 `usage` 的输出与加这个功能之前逐字节相同（用例锁住了 key 集合）；成本只在显式要求时出现。
- **价目表在本地、带版本与日期、手工维护。** `src/vagent/pricing.py` 里是 `PRICING_VERSION = "deepseek-reference-v1"`、`PRICE_AS_OF = "2026-11-05"`、按模型别名的「每百万 token」三档价（缓存命中输入 / 缓存未命中输入 / 输出）与币种。**不联网、不猜**：模型不在表内就返回 `unpriced` 并说明原因，绝不按「大致这个价」估价——错的价格比没有价格更有害。
- **用 `Decimal` 记账，不用浮点。** 金额以 6 位小数的**字符串**输出（`"0.011750"`），汇率不换算、跨币种不合并（沿用账单台账的「不换汇」规则）。三次一 token 的调用相加必须是 `0.000006`，而不是 `5.999999e-06`（有用例）。
- **缓存分档计价，缺数据时降级并标注。** 报告了命中/未命中就按报告计价；只报告了一侧就推导另一侧（`cacheUsage="derived"`）；两者都不可信（如命中数 > 输入数）就按**全部未命中**估上界并标 `assumed-miss`——宁可乐观标价，也不假装精确。
- **不调用供应商、不写文件、不动预算。** 估算只读 `store.snapshot()`；`usage --cost` 与脚本都不产生任何写入，也不影响 Run 的费用/步数护栏。

## 工作机制

- `estimate_tokens(model, *, input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens, cache_blind)` → 单次调用的金额与分档 token 数；模型或 token 数未知则返回 `None`。
- `estimate_run(run)` → 逐条 `modelCalls` 计价后求和；返回 `status`（`priced` / `partial` / `unpriced` / `mixed-currency`）、`pricedCalls` / `unpricedCalls`、`cacheAssumedMissCalls` 与金额。
- `estimate_runs(runs)` → 按币种汇总，报告 `pricedRuns` / `partialRuns` / `unpricedRuns`。
- `savings(runs)` → 同一批 Run 分别按「缓存感知」与「全部按未命中」（`cache_blind`）计价，给出 `saved` 与 `savedPercent`。
- CLI：`vagent usage --cost`（可叠加 `-s/--run/--project`）；加 `--project` 时把 `textCost` 挂进项目聚合结果。
- 脚本：`scripts/compare_cost.py [--home .vagent] [--project coffee] [--json] [--verbose]` 输出两种口径的对照表。

## 可比数字（离线，零费用）

**实测样本**：一个真实跑出来的 Run（`ScriptedModel` 上报了 token 与缓存明细），2 次模型调用：

| 调用 | 输入 | 缓存命中 | 缓存未命中 | 输出 |
|---|---:|---:|---:|---:|
| 第 1 次 | 1200 | 800 | 400 | 300 |
| 第 2 次 | 2000 | 1500 | 500 | 800 |
| 合计 | 3200 | 2300 | 900 | 1100 |

`python scripts/compare_cost.py --home <该数据目录>`：

| 币种 | 缓存感知 | 全部按未命中 | 节省 | 节省比例 |
|---|---:|---:|---:|---:|
| CNY | 0.011750 | 0.015200 | 0.003450 | **22.70%** |

**黄金样例**（固定输入，可手算校验）：100 万输入（40% 命中 / 60% 未命中）+ 100 万输出 → `9.400000` CNY（0.6×2 + 0.4×0.5 + 1×8）；把同一批输入全部按未命中计价 → `10.000000` CNY，差 `0.600000`（**6.00%**）。这两组数字同时是 `tests/test_cost_estimate.py` 的断言。

结论口径：缓存对**本样本**省了 22.70%，对 40/60 分档的黄金样例省 6.00%——差异来自命中率（71.9% vs 40%），不是价格表。这个数字只说明「已记录用量在该表下的费用」，不冒充账单：真实扣费仍以 `usage bill` 的手工台账为准。

## 与计划对照（H6 验收判据）

| 判据 | 落地 | 证据 |
|---|---|---|
| 每项以离线对照实验给出可比数字 | 缓存感知 vs 缓存盲两种口径的对照表（22.70% / 6.00%） | `test_cache_aware_costing_beats_pricing_every_input_at_miss_rate`、`scripts/compare_cost.py` |
| 默认关闭 | 无 `--cost` 时输出 key 集合不变 | `test_the_usage_payload_is_unchanged_unless_cost_is_asked_for`、`test_the_cli_flag_is_opt_in` |
| 不破坏预算与恢复语义 | 纯读投影：不写 `state.json`、不改签名、不碰护栏 | `test_estimating_does_not_mutate_the_run` |
| 价格来源可审计 | 版本 + 日期 + 币种 + 每档单价常量 | `test_the_price_table_carries_its_provenance` |
| 诚实处理未知 | 未知模型 / 未知 token / 不可信缓存拆分都不估价或显式降级 | `test_an_unknown_model_is_never_guessed`、`test_a_call_without_token_usage_is_reported_not_estimated`、`test_a_half_reported_split_is_derived_and_an_impossible_one_falls_back_to_miss` |
| 精确性 | `Decimal` + 6 位小数 + 字符串金额，微额累加精确 | `test_amounts_stay_exact_at_micro_scales`、`test_one_million_tokens_of_each_kind_prices_to_the_table` |
| 离线 | 模块内无 httpx / requests / socket / urllib / 文件打开 | `test_pricing_module_is_offline_and_dependency_free` |

## 未纳入的候选（如实记录）

| 候选 | 状态 | 为什么先不做 |
|---|---|---|
| 并行工具调用 | 未做 | 当前 `tools_node` 串行执行（`runner.py` 的 `for call in ...`）。并行会改动工具预算计数、检查点恢复与挂起等待的语义，属于「会动冻结语义」的改动，必须单独一次 E0 冻结 + 失败回退实验，不能顺手塞进成本这条轨道 |
| thinking 模式 | 未做 | 需要协议字段完整往返 + 恢复后一致 + 与预算/用量口径对齐三件事一起验证；当前 `models.py` 仍是 `thinking: disabled` |
| Redis 回答缓存收益量化 | 未做 | 需要真实命中样本；离线可造，但「命中率」这个数字脱离真实流量没有意义，计划本身也要求「可比数字」 |
| plan-and-execute / 反思 / 子 Agent | 未做 | 属于编排模式研究，成本远大于本轨道，且与单 Agent 主路径耦合风险高（计划明写「不与主路径耦合」） |

## 兼容与边界

- 新增 CLI 参数 `usage --cost`（默认关闭）；无新增环境变量、无新增持久字段。
- 价目表是**参考价**，不是账单：币种固定、不换汇、不折算；真实扣费仍走 `usage bill` 手工台账。
- 金额 6 位小数：单次调用的微额（如 1 token = `0.000002`）能表示，但小于 1e-6 的差异会被量化掉。
- 不覆盖视频供应商费用（那部分已有 `billing-v1` 台账与估算，本轨道只管文本模型 token）。

## 自动化验证

`tests/test_cost_estimate.py`（新增，14 项）：精确计价 4 项、诚实未知 3 项、对照与不可变性 3 项、接线与纪律 4 项。

复现（离线，无网络、无模型调用）：

```
python -m pytest tests/test_cost_estimate.py -q
python -m vagent usage -s coffee --cost        # 金额只在显式要求时出现
python scripts/compare_cost.py --home .vagent  # 缓存感知 vs 缓存盲
```

回归：全量 `pytest tests/` **1620 passed, 4 skipped**（24 分 13 秒；跳过项为本机无符号链接权限的既有用例），退出码 0。本轨道未调用任何供应商、未联网。

顺带一条跨轨道事实：H5 那一轮全量出现过一次 `test_m7_timeline_render.py` 的顺序偶发（README「已知限制」已记录），本次全量未复现——与「偶发、单跑必过」的判断一致。
