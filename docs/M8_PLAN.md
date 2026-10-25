# M8 计划：供应商路由、真实样本与发布

状态：**E0 契约已冻结、E1 路由预览已交付、E2 供应商运维与账单已交付（2026-10-25）；E3–E4 待实施**。基线 `master` `16102dd`（v0.5.0 发布后；schema 9、tools/rules 18/18、168 个契约模型）。v0.5.0 检查点已发布（2026-10-24，[发布说明](./RELEASE_0.5.0.md)），M8 交付后发布 **v0.6.0**。冻结结果见 [M8 契约](./M8_CONTRACTS.md) 与 [E0 验收](./M8_E0_ACCEPTANCE.md)，机器可读基线见 `docs/M8_E0_BASELINE.json`。本文件是计划，不是契约：数字与建议值以 E0 冻结的 `docs/M8_CONTRACTS.md` 为准；[M5 计划 §6](./M5_PLAN.md) 保留为概要，本文件是其展开。

## 1. 为什么是 M8

M1–M7 已完成产品域闭环：一句话/剧本 → 分集分场分镜 → 真实 Wan 多镜头 → 逐场成片 → 字幕/配音/混音/转场/标题卡/整集渲染 → 季/批量/时间线。当前缺口不在产品域，而在**供应商真实性**与**发布**：

| 差距 | 现状证据 | 影响 |
|---|---|---|
| 发布断层 | v0.4.0 tag 后 17 个提交（M6 E1–E4、M7 E0–E4）未发版；`pyproject.toml` 仍 `0.4.0` | 没有 M6/M7 的稳定检查点与回滚点 |
| Kling/fal 零真实样本 | 能力表标注「文档支持，未做真实样本」；[M3 验收 §9](./M3_ACCEPTANCE.md) | 真实结果主机、编码、账单未验证 |
| CosyVoice 零真实请求 | M6 E2/E4 `realSample=false`；[M6 E4 §4](./M6_E4_ACCEPTANCE.md) | 音色、时长、费用只有文档依据 |
| 真实编排未跑 | M5 E4 真实样本 `文本模型调用=0`；[M5 E4](./M5_E4_ACCEPTANCE.md) | 「剧本 → 分集」真实链路未验证 |
| 无能力路由 | 全局单值 `videoProvider`（`src/vagent/config.py`）；分集/批量直接拒绝 Kling/fal（`src/vagent/video/jobs.py` 的 `EPISODE_COST_LIMIT`）；USD 不换汇 | 不能按模式/价格推荐，也不能混合供应商 |
| 供应商运维与账单薄 | 429/欠费映射只在适配器层；submit 无退避、并发=1；`actual` 始终 unknown | 限流/欠费只能人工兜底，账单不可对账 |

结论：**M8 只做「供应商侧 + 发布」，不扩产品域**——不新增 schema、不新增写工具、不改已冻结的成片/时间线语义。

## 2. 发布检查点 v0.5.0（已完成，2026-10-24）

> **已完成**：v0.5.0 于 2026-10-24 发布（[发布说明](./RELEASE_0.5.0.md)），本节保留为当时的门禁记录；M8 的成果将作为 v0.6.0 发布。

- **内容**：M6 E1–E4（字幕、CosyVoice 适配、混音、转场/标题卡/整集渲染、720P/1080P）+ M7 E0–E4（季、批量、时间线、验收）。
- **改动**：`pyproject.toml` 0.4.0 → 0.5.0；新增 `docs/RELEASE_0.5.0.md`；README/M1_PLAN/M5_PLAN 状态同步；`dist/python` 归档 0.4.0 产物并构建 0.5.0；保存安装态证据。
- **门禁**：全量回归（M7 E4 基线 1409 passed / 3 skipped）、Ruff check/format（217 文件）、前端 8 模块 78 项、四组 CI（`760fed9` 已 success，[运行 40063039486](https://github.com/shifang37/vAgent/actions/runs/40063039486)）、安装态 wheel smoke PASS、受控浏览器证据沿用 M7 E4。
- **打 tag 与推送单独确认**；失败先修，不把 0.5.0 并入 M8 范围。
- 0.5.0 之后，M8 的成果作为 **v0.6.0** 发布，发布说明只写增量。

## 3. M8 工作包（E0–E4）

### 3.1 E0 契约冻结（约 0.5 天；submit=0、模型调用=0、TTS=0）

决策点（建议值，E0 冻结）：

| 编号 | 决策 | 建议 |
|---|---|---|
| D1 | 路由粒度 | **只读建议 + 用户显式切换**；不做镜头级 provider 冻结（避免 schema 10 与预算语义重写） |
| D2 | 跨币种 | **不换汇**；USD/CNY 各自记账；分集/批量继续只接受 CNY |
| D3 | 账单口径 | **手工录入 reported actual**；未录入保持 `unknown`；不把供应商 `usage` 当扣费 |
| D4 | 限流/并发 | 沿用单 Worker 串行提交；新增逐 provider 最小提交间隔、429 冷却与 Retry-After 上限 |
| D5 | 错误码 | 复用供应商错误家族；新增码以 E0 为准（建议：账单未知、退避中、路由不可用各 1 个） |
| D6 | 真实样本授权 | 清单见 §5；模型/次数/金额任一变化需重新授权 |
| D7 | 发布 | v0.5.0 检查点 + v0.6.0；PyPI **默认不公开**，是否公开单列决定 |
| D8 | 竖屏/混合分集 | **明确不做**（9:16、混合供应商分集、跨集超级成片） |

官方复核清单（只查文档与公开页面，不发请求）：

- Kling `kling-3.0-turbo` Singapore：计费维度（`VIDEO_UNIT` 0.8/秒、USD 0.112/秒）、429/欠费错误体、`Retry-After` 语义、结果主机。
- fal `kling-video/v3/standard/text-to-video`：USD 0.084/秒、静音输出、队列/并发限制、错误体。
- Wan 北京：3.00 CNY/5 秒、`BudgetLimitExceeded` 映射、结果主机允许集。
- CosyVoice v2：0.20 CNY/千字符、RPM 180、结果 URL 24 小时、错误码（`Throttling` 等）。
- 账单可见性：各供应商后台是否提供可核对的扣费明细；无 API 时不伪造抓取。

产出：`docs/M8_CONTRACTS.md`（路由预览、账单录入、错误码、退避与并发）、`docs/M8_E0_BASELINE.json`（能力表/价格版本/工具面 18/18 哈希）、`scripts/capture_m8_baseline.py`、`docs/M8_E0_ACCEPTANCE.md`。硬约束：不改产品代码、不改 schema、submit=0。

### 3.2 E1 路由预览与报价对比（2–3 天；submit=0）

> **已完成（2026-10-25）**：`src/vagent/video/routing.py`（`provider-route-v1`）、`vagent route preview`、`GET /api/route/preview`、Web 报价对比面板；真实预览逐项复现 E0 `routeProbe.rowsSha256`，工具面、schema 与 168 个模型哈希不变。证据见 [E1 验收](./M8_E1_ACCEPTANCE.md)。

- **只读预览** `provider_route`：输入分镜（`storyboardId`/版本或 `(mode, durationSeconds, aspectRatio)`），输出每个候选供应商的 `provider/model/capabilitiesVersion/priceVersion/currency/unitPrice/amount/resourceUnits/available/reason/sourceUrl/checkedAt`。
- **排序**：先「已核实可用」，再按估算金额；并列按固定顺序（wan → kling → fal），保证可复现。
- **不静默替换**：预览只给建议；用户显式切换 `videoProvider`（设置页/CLI）后，既有登记路径按冻结能力表执行，请求里冻结的 provider/价格版本不变。
- **拒绝矩阵**：USD 供应商 + 分集/批量 → `EPISODE_COST_LIMIT`；Kling/fal + 非 t2v → `VIDEO_UNSUPPORTED_MODE`；Key 未配置 → 不可用；价格未知 → 不可推荐。
- **面**：`vagent route preview`、`GET /api/route/preview`、Web 报价对比面板。**无 schema 变更**；是否新增只读工具由 E0 定（建议不加，避免工具面 18/18 → 19/19 带来的 prompt/冻结变体成本）。
- **测试**：路由矩阵（wan/kling/fal × t2v/i2v/flf2v × 5s/10s）、排序稳定性、拒绝矩阵、submit=0。

### 3.3 E2 供应商运维与账单（2 天；submit=0）

> **已完成（2026-10-25）**：`src/vagent/video/providers/gate.py`（逐 provider 提交间隔 2.0 秒、429 冷却 60 秒、`Retry-After` 只延长且 900 秒上限、`PROVIDER_BACKOFF` 只写 `runtimeBlock`、统一错误表常量、内存态延迟/退避/限流计数）、`src/vagent/video/billing.py`（`billing-v1` 侧车账本）、`vagent usage bill record|show`、`POST/GET /api/usage/bill`、`usage.billing` 汇总与 Web 用量面板账单区；工具面、schema 与 168 个模型哈希不变。证据见 [E2 验收](./M8_E2_ACCEPTANCE.md)。

- **错误映射统一表**：429 / 欠费 / 401 / 403 / 5xx / 超时 → 固定错误码 + 是否可重试 + Retry-After。
- **提交退避**：仅「提交前安全失败」（连接失败、429、明确拒绝）可重试；**未知提交状态一律不重投**（沿用 M1-C 语义），退避与重试写入 Job 事件。
- **并发/限流**：单 Worker 串行不变；新增 per-provider 最小提交间隔与 429 冷却（配置项 + 默认值 + 上限）。
- **账单**：`vagent usage bill record|show`（金额/币种/来源/日期/备注）与 `GET /api/usage/bill`；usage 汇总 estimate vs actual；缺账单保持 `unknown`。
- **观测**：逐 provider 提交/查询延迟、429 次数、退避次数，`vagent usage` 输出。
- **测试**：错误映射反例矩阵、退避时序（固定时钟）、不重投断言、账单幂等与汇总。

### 3.4 E3 真实样本（0.5–1 天；需显式授权）

按 §5 清单执行，每条留证：请求/任务 ID、结果主机、字节数、SHA-256、分辨率/编码/时长、浏览器播放与 Range、账单（或 `unknown`）。失败不重试、不换 provider、不改写既有证据；能力表维持「未做真实样本」标注直到样本成功。

### 3.5 E4 验收与发布（2 天）

- `scripts/evaluate_m8.py` 离线 3–4 用例（路由矩阵、账单与错误映射、拒绝矩阵、重放不新增）与 `tests/test_m8_evaluation.py`。
- 门禁：全量回归、Ruff、前端 8 模块、受控浏览器（报价对比 + 账单）、安装态 wheel smoke（M8 段）、四组 CI。
- 发布 **v0.6.0**：版本号、`docs/RELEASE_0.6.0.md`、tag、wheel/sdist、安装文档、PyPI 决策记录。
- 文档：`docs/M8_ACCEPTANCE.md`、README、M1_PLAN、M5_PLAN、AGENT_HARNESS_DESIGN、web/README。

## 4. 验收样例（草案）

1. 同一 5 秒 720P t2v 镜头：预览列出 wan 3.00 CNY、kling 0.56 USD、fal 0.42 USD 三行；选择 kling 后 Job 冻结 kling 能力表与价格版本；USD 镜头进入分集 → 明确拒绝且不登记。
2. 受控 429 + 欠费 + 超时：固定错误码、退避记录、不重复提交；未知状态不重投。
3. 账单：录入 0.56 USD → usage 汇总 estimate 0.56 / actual 0.56；未录入保持 `unknown`。
4. 真实样本：Kling/fal 各 1 条 5 秒 MP4 交付本地并可浏览器播放；CosyVoice 1 集配音；真实 DeepSeek 编排 1 个 Run。

## 5. 真实样本与费用（需显式授权）

| 样本 | 数量 | 估算 | 说明 |
|---|---|---|---|
| Kling `kling-3.0-turbo` 5s 720P | 1 | 0.56 USD（0.112 USD/s，4 VIDEO_UNIT） | 新加坡直连，需 Kling Key |
| fal `kling-video/v3/standard` 5s | 1 | 0.42 USD（0.084 USD/s） | 中转、静音输出 |
| CosyVoice v2 配音 | 1 集 ≤2000 字符 | ≤0.40 CNY（0.20 元/千字符） | 单条 ≤500 字符、≤40 次请求 |
| 真实 DeepSeek 编排 | 1 个 Run | 文本费 < 2 CNY（按现有 Run 限额） | 补 M5 E4 `模型调用=0` 的缺口 |
| 可选：i2v 角色一致性 1 场 | ≤6 镜头 | ≈18 CNY（3.00/镜头） | 另批授权，可不做 |

合计推荐授权上限：**0.98 USD + 2.40 CNY + 文本费 2 CNY**（不含可选 i2v 场）。实际账单以供应商后台为准；估算不当扣费凭据。

## 6. 工时与顺序

| 包 | 估时 | 备注 |
|---|---|---|
| v0.5.0 发布检查点 | 0.5 天 | 不含 M8 代码 |
| M8 E0 | 0.5 天 | 文档 + 基线，submit=0 |
| M8 E1 | 2–3 天 | 只读路由与报价对比 |
| M8 E2 | 2 天 | 错误映射、退避、账单 |
| M8 E3 | 0.5–1 天 | 真实样本，需授权 |
| M8 E4 | 2 天 | 离线套件、门禁、v0.6.0 |

建议顺序：**v0.5.0 → E0 → E1 与 E2 并行 → E3 → E4**；E3 失败不阻塞 E4 的发布判断，但必须在验收文档中如实记录。

## 7. 边界（本轮不做）

- 9:16 竖屏与竖屏供应商能力（单列后续话题）。
- 汇率换算、混合供应商分集、跨集「超级成片」、镜头级 provider 冻结（schema 10）。
- Sora/Veo/Seedance/Replicate、可执行插件/插件市场、后台批处理守护进程。
- PyPI 公开发布（除非单独批准；默认只发布仓库内 wheel/sdist）。
- 账单自动抓取：供应商无公开账单 API 时不做，手工录入为唯一口径。

## 8. 风险

1. **Kling/fal 账号不可用或地域受限**：样本失败只留证、不重试；能力表维持「未验证」，不阻塞 v0.6.0 发布。
2. **供应商调价**：必须升 `capabilitiesVersion`/`priceVersion`，历史 Job 冻结快照不受影响；路由拒绝旧价条目。
3. **USD/CNY 并列被误读为已换汇**：界面与 CLI 明确「不换汇、分别记账」，汇总不合并币种。
4. **退避引入重复提交**：只允许提交前安全失败重试；未知状态不重投，用固定时钟测试覆盖。
5. **发布期 CI runner 中断**（有历史先例）：重跑，不视为失败。
6. **真实样本失败**：不重跑掩盖、不用其它 provider 结果替代。

## 9. 新对话启动方式

> 基于 `master`（v0.5.0 已发布；schema 9、tools/rules 18/18、168 个契约模型），**M8 E1 路由预览（2026-10-25）与 E2 供应商运维与账单（2026-10-25）均已交付**，现在从 **E3 真实样本**开始（[E1 验收](./M8_E1_ACCEPTANCE.md)、[E2 验收](./M8_E2_ACCEPTANCE.md)）：E3 必须先取得**单独授权**，按 §5 清单执行 Kling 5 秒、fal 5 秒、CosyVoice 1 集与真实 DeepSeek 编排 1 Run，每条留证任务 ID、结果主机、字节数、SHA-256、时长/编码、播放与 Range、账单或 `unknown`，失败不重试、不换 provider；随后 E4 完成离线套件、门禁与 **v0.6.0** 发布。E0 基线 `docs/M8_E0_BASELINE.json` 是全程比对基准：168 个模型哈希、`contractsSha256`、`capabilitiesSha256`、工具/规则哈希与 `routeProbe.rowsSha256` 必须不变。不做 9:16 竖屏；Kling/fal/CosyVoice 在样本成功前保持「未做真实样本」标注。
