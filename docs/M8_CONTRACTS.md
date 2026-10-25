# M8 契约：供应商路由、账单与真实样本发布

冻结日期：**2026-10-25（E0）**。基线 `master`（v0.5.0 发布后；schema 9、tools/rules 18/18、168 个契约模型）。范围见 [M8 计划](./M8_PLAN.md)，上游语义见 [M3 契约](./M3_CONTRACTS.md)、[M5 契约](./M5_CONTRACTS.md)、[M6 契约](./M6_CONTRACTS.md)、[M7 契约](./M7_CONTRACTS.md)，基线证据见 [E0 验收](./M8_E0_ACCEPTANCE.md) 与 `docs/M8_E0_BASELINE.json`。

本文规定 E1–E4 的实现要求。**E0 不改变产品代码（`src/`、`web/`、`tests/`）、不注册工具、不新增模型、不改存储版本：视频 submit=0、文本模型调用=0、TTS 请求=0、凭据读取=0。** M8 只在供应商侧加**只读路由预览**、**手工账单账本**与**提交节流**，不扩产品域：**不新增 schema、不新增写工具、不改已冻结的成片/时间线/分集语义**，也不做 9:16 竖屏与汇率换算。

> **E0 硬约束的落地方式**：M8 的新类型一律放**新模块**（`src/vagent/video/routing.py`、`billing.py`、`providers/gate.py`），不得改动 E0 基线覆盖的 9 个契约模块（`vagent.storage`、`vagent.video.contracts`、`films`、`episodes`、`storyboards`、`renders`、`audio`、`subtitles`、`tts`）。基线里的 **168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256`、工具面与规则哈希**在 E1–E4 全程必须逐字节不变；这也是「不新增 schema」的可核对形式。

## 1. 决策与冻结结果

| 编号 | 决策 | 冻结结果 |
|---|---|---|
| D1 | 路由粒度 | **只读建议 + 用户显式切换**；不做镜头级 provider 冻结、不改任何请求的冻结字段，因此不需要 schema 10 |
| D2 | 跨币种 | **不换汇**：USD/CNY 各自记账、各自汇总、各自展示；分集/批量继续只接受 CNY（USD 供应商在 `scope=episode/batch` 下不可用） |
| D3 | 账单口径 | **手工录入 reported actual**；未录入保持 `unknown`（`unknown` 不是错误码、也绝不计为 0）；**不抓取**供应商账单 API |
| D4 | 限流/并发 | 单 Worker 串行不变；新增**逐 provider 提交最小间隔**与**429 冷却**；**任何发送后的自动重投都不做**（见 §5.3，比计划更严） |
| D5 | 错误码 | 新增 **4** 个：`ROUTE_UNAVAILABLE`、`PROVIDER_BACKOFF`、`BILL_RECORD_INVALID`、`BILL_CONFLICT`；其余复用既有码（计划建议 3 个，E0 把账单侧拆成写入校验与冲突两个码，`unknown` 仍只是状态） |
| D6 | 真实样本授权 | 清单与金额沿用 [M8 计划 §5](./M8_PLAN.md)；模型、次数、金额任一变化需重新授权；E3 失败不重试、不换 provider |
| D7 | 发布 | v0.5.0 已发布（2026-10-24）；M8 成果作为 **v0.6.0** 发布；PyPI 默认不公开，公开与否单列决定 |
| D8 | 竖屏/混合分集 | **明确不做**：9:16、混合供应商分集、跨集超级成片、镜头级 provider 冻结 |

| 项目 | 冻结结果 |
|---|---|
| 存储 | **继续 schema 9**；账单是 `home/billing.json` 侧车文件（`billing-v1`），不进入 `state.json`，`DatabaseV9` 逐字节不变 |
| 工具面 | **18/18 不变**：不新增模型工具（路由预览、账单录入都是 CLI/HTTP 的本地动作），`VIDEO_RULES_V18`/`LIVE_VIDEO_RULES_V18` 与 32 个已注册工具（5 个核心 + 27 个视频工具）字节不变 |
| 门禁 | 契约模型 168、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希、`routeProbe.rowsSha256` 全部不变；变化必须解释并升级版本号 |
| 费用 | 与 M3–M7 相同：视频与 TTS 分桶，不换汇、不互相抵扣，任何付费请求都要显式 `confirm` 并回显估算 |
| 真实样本 | E0 全程 submit=0；E3 的请求由单独授权触发，失败只留证 |

## 2. 冻结事实：能力表、价格与上限

### 2.1 视频能力表（live，4 条已发布条目）

| 供应商 | 模型 | 能力版本 | 价格版本 | 币种 | 单价 | 5 秒估算 | 模式 | 音频 |
|---|---|---|---|---|---|---|---|---|
| wan | `wan2.7-t2v-2026-06-12` | `wan27-shot-beijing-v2` | `wan27-beijing-720p-20261010` | CNY | 0.60/秒 | **3.00** | t2v 2–15 秒 | auto |
| wan | `wan2.7-i2v-2026-04-25` | `wan27-shot-beijing-v2` | `wan27-i2v-beijing-720p-20261016` | CNY | 0.60/秒 | **3.00** | i2v/flf2v 2–15 秒 | auto |
| kling | `kling-3.0-turbo` | `multi-provider-v3` | `kling30-turbo-720p-20261021` | USD | 0.112/秒 + 0.8 `VIDEO_UNIT`/秒 | **0.56**（4 units） | t2v 5 秒 | native |
| fal | `fal-ai/kling-video/v3/standard/text-to-video` | `multi-provider-v3` | `fal-kling30-standard-silent-20261021` | USD | 0.084/秒 | **0.42** | t2v 5 秒 | silent |

- kling/fal 只开放 **720P / 16:9 / t2v 5 秒**，`shotModes=("t2v",)`；wan 保留 M2 已验证的 2–15 秒与 i2v/flf2v。三家的 `supportsCancel`/`supportsIdempotentSubmit`/`supportsResultUrlRefresh` 全为 `false`。
- 价格来源与核对日期：wan `2026-10-16`、kling/fal `2026-10-21`、CosyVoice `2026-10-22`；`sourceUrl` 必须保持 `https://` 且随价格版本冻结。

### 2.2 TTS

| 供应商 | 模型 | 价格版本 | 币种 | 单价 | RPM | 上限 |
|---|---|---|---|---|---|---|
| cosyvoice | `cosyvoice-v2` | `cosyvoice-v2-beijing-20261022` | CNY | 0.20/千字符 | 180 | 单条 ≤500 字符、单集 ≤2000 字符 / ≤40 请求 / ≤0.40 CNY |

`local-tts-v1` 仍是 mock 模式的唯一 TTS，价格未知且不计费。

### 2.3 记账上限

| 层 | 上限 |
|---|---|
| 单 Job | wan 3.00 CNY（`videoMaxJobCost`，默认 3.00，可调大）；kling 0.60 USD / 4 units、fal 0.50 USD（与冻结常量取小，**只能收紧**） |
| 单 Run | wan 12.00 CNY、kling 2.40 USD / 16 units、fal 2.00 USD；每 Run 最多 4 个新真实 Job |
| 集级 | 20 Job / 60.00 CNY；每镜头估算 3.00 CNY；最多 20 镜头 |
| 批量 | 3 集 / 60 Job / 180.00 CNY；每集 ≤20 Job；每项目 ≤64 批次 |
| 等待 | 集合 20 成员 / 600 秒（不变） |

### 2.4 价格变更规则

任何单价、计费维度、模型或端点的变化都**必须**升 `capabilitiesVersion` 与 `priceVersion`（必要时升 `adapterVersion`/`sourcePolicyVersion`），并重跑 `scripts/capture_m8_baseline.py` 生成新基线。历史 Job 保留冻结快照，不受影响；路由预览拒绝旧价条目（`available=false, reason=VIDEO_PRICE_UNKNOWN`），**不做静默调价**。

### 2.5 「未做真实样本」的口径

能力与价格目前**只有文档依据**，没有真实扣费样本。预览返回的 `verification` 固定为 `documented-no-real-sample`；它是 `routing.py` 里的静态标签，**不是能力表字段**，因此 E3 成功与否都不改 `capabilitiesSha256`。E3 的样本证据写入 `docs/M8_ACCEPTANCE.md`（E4 交付时建立链接），标签是否升级由后续版本决定。

## 3. 路由预览（`provider-route-v1`）

预览回答「这个镜头/这一集现在能用哪些供应商、各要多少钱、为什么不能用」，**只读**：不登记 Job、不写存储、不发供应商请求、不读凭据内容（只读「是否配置」的布尔值）。

### 3.1 输入

| 形式 | 字段 | 说明 |
|---|---|---|
| 按分镜 | `storyboardId` + 可选 `version` + 可选 `shotIds[]` | 服务端取该分镜版本的镜头，**逐镜头判定**；任一镜头不被某供应商支持，则该供应商在整次预览中不可用，`reason` 取第一个失败镜头的原因 |
| 按规格 | `mode` + `durationSeconds` + 可选 `aspectRatio` | 不引用任何存储对象，供「先比价再写分镜」使用 |
| 公共 | `scope ∈ {shot, episode, batch}`，缺省 `shot` | `episode/batch` 会按 CNY-only 口径过滤 USD 供应商 |

`storyboardId` 与 `mode` 二选一，同时提供返回 `INVALID_ARGUMENTS`；`durationSeconds` 必须在 2–15，`mode ∈ {t2v, i2v, flf2v}`，`aspectRatio` 仅 `16:9` 或缺省。

### 3.2 输出（`provider-route-v1`）

```json
{
  "contract": "provider-route-v1",
  "scope": "shot",
  "request": {"mode": "t2v", "durationSeconds": 5, "aspectRatio": "16:9", "storyboardId": null, "storyboardVersion": null, "shotIds": []},
  "videoMode": "live",
  "candidates": [
    {
      "provider": "wan",
      "model": "wan2.7-t2v-2026-06-12",
      "capabilitiesVersion": "wan27-shot-beijing-v2",
      "priceVersion": "wan27-beijing-720p-20261010",
      "currency": "CNY",
      "unit": "output_second",
      "unitPrice": "0.60",
      "quantity": "5",
      "amount": "3.00",
      "resourceUnits": null,
      "available": true,
      "reason": null,
      "keyConfigured": true,
      "verification": "documented-no-real-sample",
      "sourceUrl": "https://help.aliyun.com/zh/model-studio/model-pricing#ba6f7744d5e0o",
      "checkedAt": "2026-10-16",
      "quoteFingerprint": "…"
    }
  ],
  "recommended": "wan",
  "notes": ["USD 不计入集级/批量额度"],
  "fingerprint": "…"
}
```

- `candidates` **每个供应商恰好一行**（wan 的两条条目按 §3.3 选一条）；顺序见 §3.4。
- `amount` 是冻结单价 × 秒数的十进制字符串（两位小数，`ResourceUnits` 按 `unitsPerSecond × 秒数` 取整），**不做四舍五入之外的任何换算**。
- `quoteFingerprint` = `sha256`(规范化 JSON：provider、model、capabilitiesVersion、priceVersion、currency、unitPrice、quantity、amount、resourceUnits)；`fingerprint` 再覆盖 `scope`、`request`、候选顺序与 `recommended`。**两个指纹都不含时间**。
- `available` 的语义是「按下表规则**在当前模式下可以登记**」，不是「立刻提交一定不被限流」：预览不读取冷却状态（§5.4），因此没有时钟依赖。
- 预览**始终评估 live 能力表**（真实供应商与真实价格），`videoMode` 只是回显：`off`/`mock` 下所有候选都因 `VIDEO_MODE_OFF` 不可用、`recommended=null`，但价格对比仍然可读——这正是比价面板的用途。

### 3.3 可用性与 `reason` 优先级

每个供应商取**一条**冻结条目：优先 `shotModes` 含请求模式且 `specs` 含 `(mode, durationSeconds, aspectRatio)` 的条目；否则取 `shotModes` 含该模式的条目；否则取该供应商的第一条。随后按固定优先级判定，**取第一条命中的原因**：

| 序 | `reason` | 触发 | 对应登记错误码 |
|---|---|---|---|
| 1 | `VIDEO_MODE_OFF` | `videoMode ∈ {off, mock}`：预览始终评估 **live** 能力表，但当前模式无法登记真实任务 | `VIDEO_MODE_OFF` / `VIDEO_UNSUPPORTED_MODE` |
| 2 | `VIDEO_PROVIDER_UNAVAILABLE` | 该供应商没有已发布条目，请求却显式指定了它 | `VIDEO_PROVIDER_UNAVAILABLE` |
| 3 | `VIDEO_KEY_MISSING` | 该供应商 Key 未配置 | `VIDEO_KEY_MISSING` |
| 4 | `VIDEO_UNSUPPORTED_MODE` | 该供应商没有支持此 `mode` 的条目 | `VIDEO_UNSUPPORTED_MODE` |
| 5 | `VIDEO_UNSUPPORTED_SPEC` | 有该模式，但时长/画幅不在已核实规格内 | `VIDEO_UNSUPPORTED_SPEC` |
| 6 | `VIDEO_PRICE_UNKNOWN` | 价格缺失或价格版本不是当前冻结版本 | `VIDEO_PRICE_UNKNOWN` / `VIDEO_PRICE_CHANGED` |
| 7 | `EPISODE_COST_LIMIT` | `scope ∈ {episode, batch}` 且报价不是 CNY | `EPISODE_COST_LIMIT` |
| 8 | `VIDEO_COST_LIMIT` | 估算金额或资源单位超过**有效单 Job 上限**（默认 wan 3.00 CNY、kling 0.60 USD/4 units、fal 0.50 USD） | `VIDEO_COST_LIMIT` |

优先级是「环境 → 凭据 → 能力 → 价格 → 记账」：先回答「能不能做这个镜头」，再回答「按当前口径能不能记账」。**单 Job 上限必须核对**，否则预览会推荐一个登记时被 `VIDEO_COST_LIMIT` 拒绝的报价（例如默认 3.00 CNY 下 wan 的 10 秒报价 6.00 CNY）。预览用**当前配置下的有效上限**（wan 取当前 `videoMaxJobCost`；kling/fal 取 `min(存储值, 冻结常量)`），所以同一个 10 秒请求在调高 `videoMaxJobCost` 后可以变为可用——这一点与登记路径一致。

`reason` 只用**已冻结的错误码**，不新增词表；预览不会因为某个候选不可用而整体失败。整体失败只有请求本身不可回答时才发生：未知分镜/版本、未知 `shotIds`、非法 `scope`/`mode`/`durationSeconds`，或调用方显式要求「至少一个可用」（`--require-available`）时返回 **`ROUTE_UNAVAILABLE`**。此时不登记任何东西。

预览**只核对静态能力与单 Job 上限**：不预测 Run 剩余额度、已占用的 Job 名额、集级/批量余量，也不看 429 冷却。这些由登记路径（`VIDEO_COST_LIMIT`/`JOB_CONFLICT`/`EPISODE_COST_LIMIT`）与提交路径（`PROVIDER_BACKOFF`）各自兜底，预览不重复也不取代它们。

### 3.4 排序与推荐

排序键（升序，`available` 在前）：

1. `available`（可用优先）；
2. `currencyOrder`：**CNY → USD**（项目记账币种优先，**不是汇率比较**）；
3. `amount`（同币种内按估算金额升序）；
4. `providerOrder`：**wan → kling → fal**（固定并列顺序，保证可复现）。

`recommended` = 排序后第一个可用候选，全部不可用时为 `null`。

**跨币种不比较、不合并、不标注「更便宜」**：CNY 与 USD 金额只是并列展示，`notes` 明确写出「不换汇」。固定顺序（含币种分组）是**展示规则**，不是汇率推断；这一点在 Web 面板与 CLI 文案里都必须可见。

### 3.5 拒绝矩阵（预览即预告）

| 请求 | 预览结果 | 登记时 |
|---|---|---|
| `scope=shot`、t2v、5 秒 | wan 3.00 CNY、fal 0.42 USD、kling 0.56 USD 全部可用，推荐 wan | 任一被选供应商都可登记 |
| `scope=shot`、t2v、10 秒 | wan `VIDEO_COST_LIMIT`（6.00 CNY > 默认 3.00 单 Job 上限）；kling/fal `VIDEO_UNSUPPORTED_SPEC`；无推荐 | 选 wan 会 `VIDEO_COST_LIMIT`（调高 `videoMaxJobCost` 后才会可用）；选 kling/fal 会 `VIDEO_UNSUPPORTED_SPEC` |
| `scope=shot`、i2v/flf2v、5 秒 | wan 可用；kling/fal `VIDEO_UNSUPPORTED_MODE` | 选 kling/fal 会 `VIDEO_UNSUPPORTED_MODE` |
| `scope=episode/batch`、任意 | 仅 wan 可用；kling/fal `EPISODE_COST_LIMIT` | 整批登记被拒，不写任何 Job |
| 任一供应商 Key 未配置 | 该供应商 `VIDEO_KEY_MISSING`，其余照常 | 登记报 `VIDEO_KEY_MISSING` |
| 显式 `--require-available` 且无可用 | 返回 `ROUTE_UNAVAILABLE` | — |

### 3.6 面（不新增工具）

| 面 | 形态 | 约束 |
|---|---|---|
| CLI | `vagent route preview [--storyboard ID] [--version N] [--shot ID…] [--mode t2v] [--duration 5] [--aspect 16:9] [--scope shot\|episode\|batch] [--require-available]` | 只读，打印 JSON（与 `usage`/`jobs` 一致）；不读凭据内容、不建 provider 连接、不写存储 |
| HTTP | `GET /api/route/preview?storyboardId=&version=&mode=&durationSeconds=&aspectRatio=&scope=&requireAvailable=` | 与 `/api/usage` 同级的本地只读动作，不要求会话或 Run |
| Web | 报价对比面板（分镜/分集面板内的只读卡片） | 显示三行候选、可用性与原因、`sourceUrl`、`checkedAt` 与「不换汇」说明；「使用该供应商」只是把用户选择写进既有设置路径 |

**不新增模型工具**：路由预览若做成工具，会把 18/18 变成 19/19，并牵动 prompt/规则变体与冻结注册表；预览是**用户决策**而不是模型决策，因此固定在 CLI/HTTP/Web。工具面、规则字节与 `toolFeatures` 在 M8 全程不变。

### 3.7 切换语义（不静默替换）

- 预览只给建议，**从不**改写 `videoProvider`、`videoModel` 或任何已登记 Job。
- 用户通过**现有设置页**（`PATCH /api/config` 的 `videoProvider`）显式切换；按 K2 语义**对新登记立即生效**，在途与历史 Job 保留冻结的 provider/模型/价格版本。CLI 目前只有 `config show`，本阶段**不新增** CLI 设置命令（如将来要加，属新的本地动作，须在对应阶段验收说明）。
- 切换后登记仍按冻结能力表执行：kling/fal 只接受 t2v 5 秒 720P，集级/批量仍拒 USD，超上限仍是 `VIDEO_COST_LIMIT`/`EPISODE_COST_LIMIT`。
- 不做「按价格自动换供应商」、不做「失败后换 provider 重试」。

### 3.8 确定性与测试矩阵

- **无时钟**：输出不含 `generatedAt`/`now`；`checkedAt` 来自冻结价格版本。同一输入在同一份基线代码上必须得到**逐字节相同**的 JSON（`fingerprint` 相同）。
- **无随机**：不生成 ID、不读文件系统顺序。
- 测试（E1 落地、E4 复核）：`wan/kling/fal × t2v/i2v/flf2v × 5/10 秒` 矩阵、`scope=shot/episode/batch`、Key 有/无、价格版本缺失、显式指定不可用 provider、`--require-available`、排序稳定性（重复调用与乱序输入同结果）、`submit=0` 断言。
- E1 的实现输出必须逐项复现基线 `routeProbe.rows` 的金额、单位与顺序（`rowsSha256` 一致），这是「预览与冻结事实一致」的机器可验证形式。

### 3.9 新模块

`src/vagent/video/routing.py`：`ROUTE_CONTRACT = "provider-route-v1"`、`RouteRequest`/`RouteCandidate`/`RoutePreview`（`Contract` 子类）、`provider_verification`、纯函数 `preview(...)`。**不得**修改 §0 列出的 9 个模块。

## 4. 账单账本（`billing-v1`）

### 4.1 口径

- 只记**供应商后台核对过的实际扣费**（手工录入），一条记录对应一个已登记的 Job；不把供应商 `usage` 字段当扣费，不做自动抓取。
- 币种必须等于该 Job **冻结报价**的币种（wan/CNY、kling/USD、fal/USD），**永不换算**。
- 未录入即 `unknown`：`unknown` 不是 0，也不是错误。
- 账本只影响汇总与展示，**不改写任何 Job**（`JobV6.cost.actual` 仍冻结为 `unknown`；`ProviderActualCost` 的 `status="unknown"` 不变，因此 Job 契约与 168 个模型哈希都不动）。

### 4.2 文件与形状

`<home>/billing.json`（与 `state.json` 同级，权限 0600）：

```json
{
  "billingVersion": 1,
  "records": {
    "<jobId>": {
      "jobId": "…",
      "projectId": "…",
      "provider": "kling",
      "currency": "USD",
      "amount": "0.56",
      "source": "Kling 控制台 2026-10-25 账单",
      "invoicedOn": "2026-10-25",
      "note": "含税",
      "recordedAt": "2026-10-25T09:00:00+00:00",
      "revision": 1
    }
  }
}
```

- `amount`：`Money`（0.00–99999.99，两位小数）；`currency` 只能是冻结报价的币种；`source` 1–1000 字符且必填（无来源的金额一律拒收）；`invoicedOn` 为 `YYYY-MM-DD`；`note` ≤500 字符；`revision` 从 1 起，替换时递增。
- 只接受**已终态**的 Job（`succeeded`/`failed`/`unknown`/`download_failed`）；在途 Job 的账单会被供应商继续调整，录入即拒。
- 每项目记录数 ≤**4096**（超出返回 `BILL_RECORD_INVALID`），账本总大小 ≤**4 MB**。

### 4.3 校验、幂等与冲突

| 情况 | 结果 |
|---|---|
| 首次录入合法记录 | 写入，`replayed=false`，`revision=1` |
| 与现有记录**完全相同**（金额/币种/来源/日期/备注） | 不写盘，返回现有记录，`replayed=true` |
| 金额/币种/日期/来源任一不同 | **`BILL_CONFLICT`**，保留原记录 |
| 显式更正（`--replace --expected-revision N`） | `revision=N+1` 覆盖；`N` 不是当前 revision 时 `BILL_CONFLICT` |
| 字段非法、Job 不存在/在途、币种不符、超上限 | **`BILL_RECORD_INVALID`**，不写盘 |
| 账本 JSON 损坏 | 录入 **`INVALID_STORE`**（复用），`show`/`usage` 报 `ledgerStatus=invalid` 且全部按 `unknown` 处理，原文件保留不覆盖 |

写入方式：**一次记录 = 一次临时文件 + `os.replace` 原子替换**（与 `config.yml` 相同），并在 FileStore 实例锁内进行；账本不与 `state.json` 混写，因此不存在跨文件事务。`recordedAt` 用注入时钟（测试固定时钟）。

幂等判等只看**业务字段**（`jobId`、`currency`、`amount`、`source`、`invoicedOn`、`note`），**不含** `recordedAt` 与 `revision`：同一份账单重复录入不会因为时间戳不同而变成冲突。

### 4.4 汇总

`GET /api/usage` 与 `vagent usage` 在现有结构上**追加** `billing` 块，既有字段语义不变（`cost` 仍是 CNY 历史口径，`costByProvider` 仍按供应商/币种分列）：

```json
"billing": {
  "ledgerVersion": 1,
  "ledgerStatus": "ok",
  "currencies": [
    {"currency": "CNY", "estimate": "3.00", "reported": "3.00", "reportedJobs": 1, "unknownJobs": 0, "delta": "0.00"},
    {"currency": "USD", "estimate": "0.98", "reported": null, "reportedJobs": 0, "unknownJobs": 2, "delta": null}
  ],
  "reportedJobs": 1,
  "unknownJobs": 2
}
```

- 每个币种独立聚合：`estimate` 来自冻结报价，`reported` 来自账本，`delta = reported − estimate`；**没有 USD 记录时 `reported`/`delta` 为 `null`，不是 0**。
- 跨币种不生成「合计」；`notes` 明确「不换汇」。
- 账单录入/替换后 `usage` 立即可见（同进程读盘即可，不缓存）。

### 4.5 面

| 面 | 形态 | 约束 |
|---|---|---|
| CLI | `vagent usage bill record --job ID --amount 0.56 --currency USD --source "…" [--invoiced-on YYYY-MM-DD] [--note …] [--replace --expected-revision N] --confirm`；`vagent usage bill show [--project ID] [--currency USD]` | 本地写 / 只读；不调用供应商、不登记 Job |
| HTTP | `POST /api/usage/bill`（客户端动作，body 带 `confirm=true`）、`GET /api/usage/bill?projectId=` | 与 `/api/audio/import` 同级的本地受控写；不是模型工具 |

### 4.6 新模块

`src/vagent/video/billing.py`：`BILLING_VERSION = 1`、`BILLING_FILE = "billing.json"`、`BillRecord`/`BillLedger`（`Contract` 子类）、`record_bill`/`read_ledger`/`summarize_billing`。**不得**把模型放进已哈希的 9 个模块。

## 5. 错误映射、提交退避与并发

### 5.1 提交（`stage=submit`）

| 上游事实 | 错误码 | `submission_outcome` | Job 状态 | 冷却 |
|---|---|---|---|---|
| 2xx + 已确认 ID（Kling `code="0"`+`data.id`；fal `request_id`） | —（受理） | accepted | `queued` | 无 |
| 401 / Kling 1001–1003 | `PROVIDER_AUTH_FAILED` | not_accepted | `failed` | 无（需人工改 Key） |
| 403 / Kling 1004、1304 | `PROVIDER_ACCESS_DENIED` | not_accepted | `failed` | 无 |
| 404 | `PROVIDER_MODEL_UNAVAILABLE` | not_accepted | `failed` | 无 |
| 429 / Kling 1302、1303 | `PROVIDER_RATE_LIMITED` | not_accepted | `failed` | **触发**（§5.4） |
| Kling 1101、1102 | `PROVIDER_BILLING_BLOCKED` | not_accepted | `failed` | 无（需人工充值） |
| Kling 1201、1301 | `PROVIDER_CONTENT_REJECTED` | not_accepted | `failed` | 无 |
| 400/422（fal `detail` 含 safety 时为内容拒绝） | `PROVIDER_INVALID_REQUEST` / `PROVIDER_CONTENT_REJECTED` | not_accepted | `failed` | 无 |
| 其他非 2xx、无法解析、超时、网络、协议错误 | `SUBMISSION_UNKNOWN` | unknown | `unknown` | 无 |

**`not_accepted` 也只把该 Job 置终态，不自动重投。** 恢复方式是既有的 `shot_redo`/`episode_generate` 显式路径，占用新的 Job 名额与估算。

### 5.2 查询（`stage=query`）

| 上游事实 | 错误码 | 处理 |
|---|---|---|
| 2xx 状态机（Kling submitted/processing/succeeded/failed；fal IN_QUEUE/IN_PROGRESS/COMPLETED） | — | 推进；终态失败为 `PROVIDER_GENERATION_FAILED` |
| 401/403 | `PROVIDER_AUTH_FAILED`/`PROVIDER_ACCESS_DENIED` | 暂停查询（`queryPauseReason=configuration`）+ `runtimeBlock`，任务保留 |
| 404 | `PROVIDER_TASK_UNAVAILABLE` | 暂停查询（`task_unavailable`），不重新生成 |
| 429 | `PROVIDER_RATE_LIMITED` | 按 `nextPollAt = max(退避, Retry-After)` 重试 |
| 5xx | `QUERY_HTTP_ERROR` | 同上 |
| 超时/网络/协议 | `QUERY_TIMEOUT`/`QUERY_NETWORK`/`QUERY_PROTOCOL_ERROR` | 按 `retry_delays_seconds=(15,30,60)` 重试，第 4 次转 `retry_exhausted` 暂停 |
| 超过 24 小时查询期限 | `PROVIDER_TASK_UNAVAILABLE` | 暂停，等待 `jobs retry-query` 显式恢复 |

`Retry-After` 同时接受 delta-seconds 与 HTTP-date，**只延长、不缩短**本地退避窗口。

### 5.3 提交退避：只有「还没发出去的调度」会被延后

允许：
- 提交前检查逐 provider 最小间隔与冷却；未到时间就**保持 `pending_submit`**，不写 `submitAttempts`、不改变任何冻结字段、不发请求。
- 把这次等待作为**诊断**记进 `runtimeBlock`（`stage=submit`、`code=PROVIDER_BACKOFF`、`blockedAt`；message 里写提供商与最早可提交时间），下一次真正开始提交时清空（既有 `_submit` 已置 `runtimeBlock=null`）。

禁止：
- **任何发送后的自动重投**：`SUBMISSION_UNKNOWN` 不重投；`not_accepted`（429/4xx/业务拒绝）也不自动重投。
- 不为「连接失败」破例：现有适配器在单次请求内**无法证明**「一个字节都没发出」，把连接错误归入 `SUBMISSION_UNKNOWN` 是 M3 冻结的安全属性，M8 不放宽。计划里「连接失败可重试」的括号说明**被本决策取代**。
- 不在批次层重试、不换 provider、不降规格、不改写提示词。

这样 M1-C 的「每个 Job 至多一次提交、未知永不重投」在 M8 里逐字保留，退避只是**节流**，不是重试机制。

> **E2 已实施（2026-10-25）**，实现中对本节的细化（不改冻结语义，证据见 [E2 验收](./M8_E2_ACCEPTANCE.md)）：
>
> - **配置项名称**：逐 provider 提交间隔为 `videoSubmitIntervalSeconds`（`VAGENT_VIDEO_SUBMIT_INTERVAL_SECONDS`，默认 2.0），429 冷却为 `videoRateLimitCooldownSeconds`（`VAGENT_VIDEO_RATE_LIMIT_COOLDOWN_SECONDS`，默认 60.0）；两者在保存/启动时按 §5.4 的区间校验，读入后再按同区间收紧，因此环境变量也无法把间隔压到 2 秒以下。
> - **`Retry-After` 的来源**：Kling/fal 适配器原先在 submit 分支丢弃了这个响应头（只用于查询）。E2 在 submit 拒绝路径上把它一并交给 `ProviderCallError`，冷却据此「只延长、不缩短」并受 900 秒上限约束；错误码、`submission_outcome` 与状态机不变。
> - **统一错误表常量**：§5.1/§5.2 的映射落在 `providers/gate.py` 的 `SUBMIT_ERROR_TABLE`/`QUERY_ERROR_TABLE`，供验收用例与实现对照，不新增错误码。
> - **观测**：`GET /api/usage?runtime=1` 暴露每 Store 的提交/查询延迟 p50/p95 与退避/限流计数，块内固定 `"durable": false`；默认 `/api/usage` 不含该块。

### 5.4 并发、间隔与冷却

| 项 | 冻结值 | 范围 | 说明 |
|---|---|---|---|
| Worker 并发 | **1** | 不变 | 单 Worker 串行；每 provider 至多 1 个在途提交 |
| 全局调用间隔 | **1.0 秒** | 不变 | Worker 对同一 Store 的任意实时供应商调用之间的最小间隔 |
| 逐 provider 提交间隔 | **2.0 秒**（wan/kling/fal 同值） | 2.0–60.0 | 新配置项；与既有额度上限同一口径，**本地设置只允许延长**，不能把间隔调到 2 秒以下 |
| 429 冷却 | **60 秒** | 60–900 | 冷却是**逐 provider**的，不是逐 Job；本地设置同样只允许延长 |
| `Retry-After` 上限（仅用于冷却计算） | **900 秒** | 固定 | 超过上限按上限计，并在 `runtimeBlock.message` 注明被截断；查询侧的 `Retry-After` 语义不变（只延长 `nextPollAt`，不加新上限） |

- 冷却由「已证明的限流」触发（§5.1 的 429 行）；认证/欠费/参数错误**不**触发冷却（它们需要人工处理，且每 Job 已各自留痕）。
- 冷却期间新登记仍可进行（登记不调用供应商），提交等到冷却结束；预览不显示冷却（无时钟）。
- 冷却与间隔是**内存态**（每 Store 一份，类似现有 `_store_next_calls`）：进程重启后归零。这不影响正确性，因为失效的只是「再等一会」，而不是「可以重投」。

### 5.5 记录位置（不改 Job 契约）

- `PROVIDER_BACKOFF` 只写 `JobV2.runtime_block`（`RuntimeBlock`：`stage/code/message/blockedAt`）。`pending_submit` + `submitAttempts=0` + `runtimeBlock.stage="submit"` 是**已冻结校验器允许的组合**，因此不需要新字段、不需要 Job v7。
- Web/CLI 看到 `code=PROVIDER_BACKOFF` 时必须显示为「等待节流」而非「配置错误」；其它 `runtimeBlock` 仍按配置阻塞展示。
- 计数不落盘：`usage` 只报**可持久推导**的观测（见 §5.6）。

### 5.6 观测

| 指标 | 来源 | 持久性 |
|---|---|---|
| 逐 provider Job 数/状态分布 | Job 记录 | 持久 |
| 429 次数 | `job.error.httpStatus == 429` 或 `provider_code ∈ {1302,1303}` | 持久（Job 终态留痕） |
| `unknown` 提交数 | Job 状态 | 持久 |
| 提交/查询延迟、退避次数、限流命中 | 进程内计数器（每 Store，最近 64 个样本的 p50/p95） | **内存**，重启归零；`GET /api/usage?runtime=1` 暴露 |

内存指标必须标注 `"durable": false`，不得进入账单或验收结论。

## 6. 工具面、规则与费用

| 项 | 冻结结果 |
|---|---|
| 工具面 | **18/18 不变**（`VIDEO_TOOLS_VERSION=18`、`VIDEO_RULES_VERSION=18`，live 同值）；已注册 32 个工具（5 核心 + 27 视频）名字与 schema 逐字节不变 |
| 规则 | `VIDEO_RULES_V18`/`LIVE_VIDEO_RULES_V18` 字节不变（mock 6723 字符、live 7263 字符，见基线） |
| 新增工具 | **0 个**；路由预览与账单录入是 CLI/HTTP 客户端动作 |
| 费用口径 | 视频与 TTS 分桶；不换汇；`unknown` 不当 0；每次付费登记仍要显式 `confirm` 并回显估算 |

如果 E1–E4 中有人想加 `provider_route` 工具，必须先改本文并接受 18/18 → 19/19 的 prompt/规则/冻结注册表代价——**当前决策是不加**。

## 7. 错误码

M7 的 31 个错误码全部保留；M8 新增 **4** 个：

| 错误码 | 触发 | 副作用 |
|---|---|---|
| `ROUTE_UNAVAILABLE` | 预览请求本身不可回答（未知分镜/版本/镜头、非法 scope/mode/duration、显式指定未发布 provider），或调用方要求「至少一个可用」而没有候选 | 只读失败；不登记、不写存储 |
| `PROVIDER_BACKOFF` | 提交被逐 provider 最小间隔或 429 冷却挡住 | 未发请求、未消耗提交次数；任务保持 `pending_submit`，写 `runtimeBlock` |
| `BILL_RECORD_INVALID` | 账单字段非法、Job 不存在或在途、币种与冻结报价不符、超出条数/大小上限 | 不写盘；账本保持原样 |
| `BILL_CONFLICT` | 同一 Job 的已报告账单不一致，或替换时 `expectedRevision` 过期 | 保留原记录；需显式 `--replace` |

复用：`VIDEO_MODE_OFF`、`VIDEO_KEY_MISSING`、`VIDEO_PRICE_UNKNOWN`、`VIDEO_PRICE_CHANGED`、`VIDEO_PROVIDER_UNAVAILABLE`、`VIDEO_UNSUPPORTED_MODE`、`VIDEO_UNSUPPORTED_SPEC`、`VIDEO_COST_LIMIT`、`EPISODE_COST_LIMIT`、`PROVIDER_*` 全家、`INVALID_STORE`、`CONFIRMATION_REQUIRED`、`INVALID_ARGUMENTS`。预览的 `reason` 字段复用同一批码，不引入新词表。

## 8. 确定性与基线保护

- **零模型/零供应商**：E0–E2 与 E4 的离线套件、安装态与 CI 全部使用受控 HTTP 与本地夹具；真实供应商调用数与文本模型调用数为 0。唯一的真实请求只发生在 **E3**，且必须先取得单独授权。
- **预览确定性**：无时钟、无随机、无外部 I/O；同一输入在同一基线代码上输出逐字节相同，`fingerprint` 与 `rowsSha256` 可复核。
- **账单可追溯**：每条记录必须带 `source`、`invoicedOn` 与 `recordedAt`；替换保留 `revision` 递增，不删除历史（更正覆盖当前记录，`revision` 说明被改过）。
- **基线保护**：`docs/M8_E0_BASELINE.json` 的 168 个模型哈希、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希与 `routeProbe.rowsSha256` 是 E1–E4 的比对基准。允许的变化**只有**新增模块（`routing.py`、`billing.py`、`providers/gate.py`）与非哈希文件（`application.py`、`cli.py`、`web.py`、`jobs.py`、`worker.py`、`video/usage.py`、Web 前端与文档）。任何其他差异都必须在验收中解释，否则视为回归。

## 9. 工作包与验收

| 包 | 交付 | 验收 |
|---|---|---|
| E1 路由预览 | **已完成（2026-10-25）**：`routing.py`、`vagent route preview`、`GET /api/route/preview`、Web 报价对比面板；无 schema、无工具变化 | 预览逐项复现 `routeProbe`（金额/单位/顺序/`rowsSha256`）；拒绝矩阵与 Key 缺失正确；`submit=0`、存储不变。证据见 [E1 验收](./M8_E1_ACCEPTANCE.md) |
| E2 供应商运维与账单 | **已完成（2026-10-25）**：`providers/gate.py`（逐 provider 间隔 2.0 秒与 429 冷却 60 秒、`Retry-After` 只延长且 900 秒上限）、`RuntimeBlock` 退避记录、`billing.py` + `vagent usage bill record\|show` + `POST/GET /api/usage/bill`、`usage.billing` 汇总、`SUBMIT_ERROR_TABLE`/`QUERY_ERROR_TABLE` 统一错误表常量、`GET /api/usage?runtime=1` 内存指标；无 schema、无工具变化 | 错误映射反例矩阵、固定时钟下的间隔/冷却时序、`PROVIDER_BACKOFF` 不消耗提交次数、未知不重投、账单幂等/冲突/汇总（USD 不并入 CNY）——证据见 [E2 验收](./M8_E2_ACCEPTANCE.md) |
| E3 真实样本 | **已完成（2026-10-25，按单独授权只执行可跑的两项）**：CosyVoice v2 配音 1 集（19 条台词 / 170 字符 / 19 次请求 / 估算 0.0340 CNY；19 个 48 kHz 单声道 WAV、FFmpeg 解码通过、Range 206）、真实 DeepSeek 编排 1 Run（4 次模型调用、`episode_plan`→`episode_save`→`episode_read`、输入 96,617 / 输出 2,176 Token、保存 8 场 / 19 镜头 / 90 秒）；Kling/fal 因缺 `VAGENT_KLING_API_KEY`/`FAL_KEY` 未执行（0 请求）；两条真实样本各触发一个产品缺陷并已修复（CosyVoice 流式 WAV 占位长度、默认 64 KiB 预算装不下 18/18 工具面）；视频 submit=0、基线 168 个模型哈希与 `routeProbe.rowsSha256` 不变。证据见 [E3 验收](./M8_E3_ACCEPTANCE.md) | 每条留证（任务 ID、结果主机、字节数、SHA-256、时长/编码、播放与 Range、账单或 `unknown`）；失败不重试、不换 provider、不改冻结表 |
| E4 验收与发布 | **已完成（2026-10-25）**：`scripts/evaluate_m8.py` 离线 4 用例（路由矩阵/拒绝矩阵/账单与错误映射/重放不新增）、`tests/test_m8_evaluation.py` 4 项、受控浏览器（报价对比 + 账单重放）、安装态 wheel smoke 的 M8 两段、`docs/M8_ACCEPTANCE.md` 与 v0.6.0 发布产物；`--live` 明确拒绝，未新增付费样本 | 全量回归、Ruff、前端 10 模块、四组 CI；168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256` 与 `routeProbe.rowsSha256` 不变；证据见 [M8 验收](./M8_ACCEPTANCE.md) |

## 10. 明确不做

- 9:16 竖屏、竖屏供应商能力、画幅自适应。
- 汇率换算、跨币种合计、按价格自动换供应商、失败后换 provider 重试。
- 账单自动抓取（无公开 API 时不伪造）、把供应商 `usage` 当扣费、把 `unknown` 计为 0。
- 镜头级 provider 冻结（schema 10）、混合供应商分集、跨集超级成片。
- 新增模型工具/插件、后台批处理守护进程、无人值守的付费重试。
- PyPI 公开发布（除非单独批准）。
