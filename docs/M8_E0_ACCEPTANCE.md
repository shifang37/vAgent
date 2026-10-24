# M8 E0 验收：供应商路由、账单与发布契约冻结

日期：**2026-10-25**。基线 `master` `16f377b`（v0.5.0 发布后；schema 9、tools/rules 18/18、168 个契约模型）。范围见 [M8 计划](./M8_PLAN.md)，冻结契约见 [M8 契约](./M8_CONTRACTS.md)，机器可读基线见 `docs/M8_E0_BASELINE.json`。**E0 不改变产品代码（`src/`、`web/`、`tests/`）、不注册工具、不新增模型、不改存储版本：视频 submit=0、文本模型调用=0、TTS 请求=0、凭据读取=0。**

E0 的产出是「契约 + 基线 + 可行性证据」，实现从 E1 开始。**E1 的第一件事是按契约 §3 落地只读路由预览**；E0 基线 `docs/M8_E0_BASELINE.json` 保持冻结，不被后续阶段改写。

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 契约冻结 | `docs/M8_CONTRACTS.md` | D1–D8 决策、能力/价格/上限表、`provider-route-v1` 预览、`billing-v1` 侧车账本、错误映射与提交节流、4 个新错误码、确定性与基线保护 |
| 基线快照 | `docs/M8_E0_BASELINE.json` | **168** 个契约模型的 schema 哈希与聚合哈希、两套工具面/规则哈希、能力表与价格、记账上限、`routeProbe` 参考排序 |
| 基线脚本 | `scripts/capture_m8_baseline.py` | 可复现地重建上述快照；不联网、不登记 Job、不读凭据；只用工作区内的临时 Store（结束即删） |
| 验收文档 | 本文件 | 决策、证据、边界与 E1 入口 |
| 文档同步 | `docs/M8_PLAN.md`、`M1_PLAN.md`、`docs/M5_PLAN.md`、`README.md`、`AGENT_HARNESS_DESIGN.md` §8.9 | M8 状态、契约与基线的交叉引用 |

## 2. 已确认的决策

| 决策点 | 结论 |
|---|---|
| 路由粒度 | 只读建议 + 用户显式切换；不做镜头级 provider 冻结，因此不需要 schema 10 |
| 跨币种 | 不换汇；USD/CNY 分别记账与汇总；`scope=episode/batch` 只接受 CNY |
| 账单口径 | 手工录入 reported actual；未录入保持 `unknown`（不是 0，也不是错误）；不抓取供应商账单 API |
| 限流/并发 | 单 Worker 串行不变；逐 provider 提交间隔 2.0 秒（2–60）、429 冷却 60 秒（60–900）、`Retry-After` 上限 900 秒；本地设置只允许延长 |
| 提交重试 | **任何发送后的自动重投都不做**（含 429 与「明确拒绝」）；退避只作用于尚未发出的调度；连接失败仍归 `SUBMISSION_UNKNOWN`，不破例 |
| 错误码 | 新增 4 个：`ROUTE_UNAVAILABLE`、`PROVIDER_BACKOFF`、`BILL_RECORD_INVALID`、`BILL_CONFLICT` |
| 工具面 | **18/18 不变**，不新增模型工具；预览与账单都是 CLI/HTTP 的本地动作 |
| 存储 | 继续 schema 9；账单是 `home/billing.json` 侧车文件，`DatabaseV9` 与 168 个模型哈希逐字节不变 |
| 新类型落点 | 只进新模块 `routing.py`/`billing.py`/`providers/gate.py`，已哈希的 9 个契约模块不得改动 |
| 真实样本 | E0 全程 submit=0；E3 需单独授权；失败不重试、不换 provider、不改冻结表 |
| 发布 | v0.5.0 已发布（2026-10-24）；M8 成果作为 v0.6.0；PyPI 默认不公开 |

## 3. 验证结果

### 3.1 产品代码未变

```text
git status --short
 M AGENT_HARNESS_DESIGN.md
 M M1_PLAN.md
 M README.md
 M docs/M5_PLAN.md
 M docs/M8_PLAN.md
?? docs/M8_CONTRACTS.md
?? docs/M8_E0_ACCEPTANCE.md
?? docs/M8_E0_BASELINE.json
?? scripts/capture_m8_baseline.py

git diff --stat src web tests   # 空
```

`ruff check src/vagent tests scripts` 全部通过；`ruff format --check` **218** 个文件已格式化（M7 E4 为 217，本包新增基线脚本）。全量回归：**1409 passed / 3 skipped**（1412 collected；3 项 skip 为既有的 Windows 符号链接权限用例），耗时 **1607.62 秒（0:26:47）**，与 v0.5.0 发布时的 1409/3 一致。前端 8 个状态模块 **78 passed / 0 failed**（无前端改动，沿用 M7 E4 套件）。

### 3.2 基线快照

| 项目 | 值 |
|---|---|
| 契约模型数 | **168**：`vagent.storage` 26、`video.contracts` 75、`video.films` 26、`video.episodes` 13、`video.storyboards` 7、`video.renders` 6、`video.audio` 8、`video.subtitles` 4、`video.tts` 3（M7 E0 为 165；M7 E1–E3 的 schema 9 与季/批次/时间线把 storage/contracts/films 各加 1） |
| `contractsSha256` | `ba9d8d7b4888f4c21ec58b269836f813bf7cbf850ada9881a7f77751301b2e27` |
| `capabilitiesSha256` | `da6aa7b7889a616bc944d965739deacab91288b02b8d07c892c260301a52d1bb` |
| 基线文件 SHA-256 | `f3dc91c1c08db59ae75347d438e5fe44a6c0ac54b53da0ffaa12f4e8cbcb3706`（47,137 字节） |
| 存储版本 | **9**；M8 不新建、不迁移 |
| 工具面 | **18/18**（mock 与 live 同值）；已注册 **32** 个工具（5 核心 + 27 视频），M2–M7 分组共 **23** 个命名工具 |
| mock 工具面 | `specsSha256=1e222b2195d8150b2eaec06c7fd31d4b93f37988f1611a95e436bd0a95a51039`，规则 `9e66254b…`（6,723 字符） |
| live 工具面 | `specsSha256=1efcb3f955f83d98e9d556a66a382531513bca79bc174b2ec917a0e539bbebe4`，规则 `e81a2ef7…`（7,263 字符） |
| 能力表 | live **4** 条：wan t2v、wan i2v、kling、fal；mock 1 条 |
| 价格 | wan 0.60 CNY/秒（`wan27-beijing-720p-20261010` / `wan27-i2v-beijing-720p-20261016`，2026-10-16）；kling 0.112 USD/秒 + 0.8 `VIDEO_UNIT`/秒（`kling30-turbo-720p-20261021`，2026-10-21）；fal 0.084 USD/秒（`fal-kling30-standard-silent-20261021`，2026-10-21）；cosyvoice 0.20 CNY/千字符（`cosyvoice-v2-beijing-20261022`，2026-10-22） |
| 上限 | 单 Job 3.00 CNY（`videoMaxJobCost` 可调）/ 0.60 USD / 0.50 USD；单 Run 12.00 CNY / 2.40 USD / 2.00 USD；每 Run 4 个新真实 Job；集级 20 Job / 60.00 CNY；批量 3 集 / 60 Job / 180.00 CNY；TTS 单集 2000 字符 / 40 请求 / 0.40 CNY；等待 20 成员 / 600 秒 |

E1–E4 必须保持 168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希与 `routeProbe.rowsSha256` **逐字节不变**；只允许新增模块与非哈希文件（契约 §8）。任何其他差异都要在对应阶段验收中解释，否则视为回归。

> **M8 基线不是 M7 基线的父集。** 三个新模型是 `DatabaseV9`、`SeasonGenerationArgs`、`RenderTimelineSource`；M7 E1–E3 在 M7 E0 之后还声明过传递 `$defs` 漂移，使 **12** 个既有模型（`Database`/`DatabaseV5`/`DatabaseV7`/`DatabaseV8`、`Film`/`FilmAssembly`/`FilmRender`/`FilmVersion`/`RenderSources`、`FilmRenderArgs`/`RenderReadArgs`/`RenderTask`）的哈希已与 M7 E0 基线不同。因此 M8 的比对基准是本文这份快照，而不是 `docs/M7_E0_BASELINE.json`；M7 基线继续作为 M6→M7 迁移的黄金证据。

### 3.3 路由探针（参考排序）

`routeProbe` 把契约 §3.4 的排序/拒绝规则直接作用在**冻结的能力表**上（不联网、无时钟、无存储），作为 E1 实现的对照：

| 请求 | 候选顺序与金额 | 推荐 |
|---|---|---|
| `scope=shot`、t2v、5 秒、16:9 | wan **3.00 CNY** → fal **0.42 USD** → kling **0.56 USD**（4 `VIDEO_UNIT`），全部可用 | wan |
| `scope=shot`、t2v、10 秒、16:9 | wan `VIDEO_COST_LIMIT`（6.00 CNY 超过默认 3.00 单 Job 上限）；kling/fal `VIDEO_UNSUPPORTED_SPEC` | 无 |
| `scope=shot`、i2v、5 秒 | wan **3.00 CNY** 可用；kling/fal `VIDEO_UNSUPPORTED_MODE` | wan |
| `scope=episode`、t2v、5 秒 | wan **3.00 CNY** 可用；kling/fal `EPISODE_COST_LIMIT` | wan |

- 排序键 =（可用、币种 CNY→USD、金额、wan→kling→fal）；**跨币种只并列展示，不比较、不合计**，所以 wan 与 fal/kling 之间没有「更便宜」的结论。
- `routeProbe.rowsSha256 = 3817d19141f1e73b075722328b645be08e6a3ac2d4748caeea1e819c21f06413`；E1 的真实预览必须复现同一金额、单位与顺序。
- 探针固定 `jobCeilings` 为**默认本地设置**（wan 3.00 CNY、kling 0.60 USD/4 units、fal 0.50 USD），与基线 `limits.providerCeilings` 一致；真实预览用实际配置计算，所以调高 `videoMaxJobCost` 会让 10 秒的 wan 变为可用——探针记录的是默认口径。
- 10 秒这一行是 E0 的一个**实测结论**：默认 3.00 CNY 的单 Job 上限让 wan 实际可用时长停在 5 秒，虽然能力表声明 2–15 秒。预览若不核对单 Job 上限，就会推荐一个登记必被 `VIDEO_COST_LIMIT` 拒绝的报价。
- 探针只证明**冻结表 + 冻结排序/拒绝规则**能给出预期结果；它不证明真实主机、编码、账单或限流（那是 E3 的边界）。

### 3.4 可复现

```powershell
.\.venv\Scripts\python.exe scripts/capture_m8_baseline.py
# 再运行一次，docs/M8_E0_BASELINE.json 的 SHA-256 与首次相同
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
```

本次连续运行两次，输出 JSON 的 SHA-256 一致（`f3dc91c1…`，47,137 字节）；脚本用工作区内临时 Store（`tmp/m8-e0-baseline-store`，结束即删）重建两套工具面，不触碰应用数据目录。

### 3.5 无付费、无模型

- 基线脚本只做本地 schema 哈希、注册表重建与纯函数报价，写入 `submit=0`、`modelCalls=0`、`providerRequests=0`、`credentialsRead=false`、`jobsRegistered=0`。
- 本包未读取任何 Key 内容（只取「是否配置」的布尔值，且全部为未配置），未访问供应商，未提交 Job，未调用文本模型或 TTS；没有新增费用，实际账单仍为 `unknown`。
- 全量回归与前端测试均使用本地夹具/受控路径。

## 4. 设计边界与决定

1. **账单用侧车文件，不碰 `state.json`。** 把账单写进 `DatabaseV9` 会强制 schema 10，从而让 168 个模型哈希与 M8 基线一起失效；侧车 `billing.json` 只加一个文件、一次原子替换，`ProviderActualCost(status="unknown")` 也继续原样冻结。代价是账本与状态文件没有跨文件事务——因此账本只做「一条记录 = 一次原子替换」，损坏时拒绝写入并保留原文件。
2. **M8 不放开任何发送后的重投。** 计划 §3.3 的括号「连接失败、429、明确拒绝可重试」在 E0 被收窄为「只有尚未发出的调度会被延后」：现有适配器在单次请求内无法证明「一个字节都没发出」，429 与 4xx 虽然是 `not_accepted`，自动重投仍会削弱 M1-C 的单次提交不变量。恢复一律走显式 `shot_redo`/`episode_generate`。
3. **退避记录用既有 `runtimeBlock`，不新增 Job 字段。** `pending_submit` + `submitAttempts=0` + `runtimeBlock.stage="submit"` 是已冻结校验器允许的组合，因此 `PROVIDER_BACKOFF` 不需要 Job v7；Web/CLI 按错误码把它显示为「等待节流」。
4. **冷却与间隔是内存态。** 与现有「每 Store 1 秒调用间隔」同构；重启后归零只会让下一次提交早一点，不会让任何未知提交被重投。因此不需要新的持久字段。
5. **预览无时钟、无随机。** 输出不含 `generatedAt`，`checkedAt` 来自冻结价格版本；同一输入逐字节相同，`fingerprint` 可作为回归断言。冷却状态不进预览，避免把一次性运行态写进「可复现」的契约。
6. **排序里的 CNY 优先是展示规则，不是汇率。** 契约把币种分组与 `wan→kling→fal` 并列顺序都写死，并在 CLI/Web 文案里显式写「不换汇」，避免把「wan 排第一」误读成「wan 更便宜」。
7. **`reason` 复用错误码。** 预览的不可用原因直接用已冻结错误码（`VIDEO_KEY_MISSING`、`EPISODE_COST_LIMIT`…），不新造词表；只有「请求本身不可回答」才用新码 `ROUTE_UNAVAILABLE`。
8. **新类型只进新模块。** `routing.py`/`billing.py`/`providers/gate.py` 让 M8 能在不改 9 个已哈希模块的前提下增加行为；这是「不新增 schema」在工程上可核对的形式。
9. **「未做真实样本」是标签，不是能力表字段。** 它放在 `routing.py` 的静态表里，E3 成功与否都不改 `capabilitiesSha256`；样本证据写进验收文档，避免基线在阶段中途失效。
10. **已知版本漂移（本包不修）**：`src/vagent/__init__.py` 仍写 `0.4.0`，而 `pyproject.toml` 是 `0.5.0`（v0.5.0 发布时的遗漏）。E0 冻结产品代码，因此本包只记录不改；由 E4 发布 v0.6.0 时一并校正，并纳入安装态 wheel smoke 的版本断言。
11. **预览必须核对单 Job 上限。** 探针把默认上限（wan 3.00 CNY、kling 0.60 USD、fal 0.50 USD）纳入判定后，10 秒 t2v 从「wan 6.00 CNY 可用」变成「wan `VIDEO_COST_LIMIT`」——这是 E0 用真实报价矩阵发现的坑：能力表声明 wan 2–15 秒，但默认预算实际只放行 5 秒。契约因此把「记账上限」放在优先级末尾并强制核对，避免预览推荐一个登记必被拒的报价；同时区分「wan 的 `videoMaxJobCost` 可调大」与「kling/fal 只能收紧」两种语义。

## 5. 明确未做与边界

- **路由预览未实现**：`routing.py`、`vagent route preview`、`GET /api/route/preview`、Web 报价对比面板属 E1；当前只有 `provider_statuses()` 与设置页的逐 provider 摘要。
- **账单未实现**：`billing.json`、`vagent usage bill`、`POST/GET /api/usage/bill`、`usage.billing` 汇总属 E2。
- **提交节流未实现**：逐 provider 间隔、429 冷却、`PROVIDER_BACKOFF` 记录属 E2；当前只有每 Store 1 秒的全局调用间隔与既有查询退避。
- **真实样本未提交**：Kling/fal/CosyVoice/真实 DeepSeek 编排都仍标注「文档支持，未做真实样本」；E3 需单独授权模型、次数与金额。
- **数据库/工具面/规则未动**：schema 仍 9，工具面仍 18/18，规则字节不变；没有新增模型工具。
- **9:16 竖屏、汇率换算、混合供应商分集、账单自动抓取、PyPI 公开发布**：均不在 M8。
- `routeProbe` 只证明冻结表与排序规则；它不证明真实主机可达、供应商限流额度、真实账单金额或浏览器播放。

## 6. E1 入口

> **E1 已完成（2026-10-25）**：`routing.py`、`vagent route preview`、`GET /api/route/preview` 与 Web 报价对比面板已交付，真实预览复现本基线的 `routeProbe.rowsSha256`；证据见 [E1 验收](./M8_E1_ACCEPTANCE.md)。本节保留为当时的入口要求。

E1 的第一件事是按 [M8 契约](./M8_CONTRACTS.md) §3 落地**只读路由预览**：

1. 新建 `src/vagent/video/routing.py`：`ROUTE_CONTRACT = "provider-route-v1"`、`RouteRequest`/`RouteCandidate`/`RoutePreview`（`Contract` 子类）、`provider_verification`、纯函数 `preview(...)`；**不得**修改 `vagent.storage` 与 9 个已哈希契约模块。
2. 预览输入支持「按分镜（`storyboardId`/`version`/`shotIds`）」与「按规格（`mode`/`durationSeconds`/`aspectRatio`）」两种形式，`scope ∈ {shot, episode, batch}`；输出每个供应商恰好一行，字段按契约 §3.2。
3. 可用性与 `reason` 按契约 §3.3 的 **8** 级优先级（环境 → 凭据 → 能力 → 价格 → 记账，末级含单 Job 上限 `VIDEO_COST_LIMIT`）；排序按 §3.4；`recommended` 取第一个可用候选；`--require-available` 无候选时返回 `ROUTE_UNAVAILABLE`。
4. 接三个面：`vagent route preview`、`GET /api/route/preview`、Web 报价对比面板（只读卡片 + 「不换汇」说明）。三者共用同一个纯函数，**不加模型工具**。
5. 测试：`wan/kling/fal × t2v/i2v/flf2v × 5/10 秒`矩阵、`scope` 三种、Key 有/无、价格版本缺失、显式不可用 provider、排序稳定性、`submit=0`；并断言输出逐项复现基线 `routeProbe.rows`（金额、单位、顺序、`rowsSha256`）。
6. 基线比对：168 个模型哈希、`contractsSha256`、`capabilitiesSha256`、工具/规则哈希、`rowsSha256` 全部不变；任何差异必须在 E1 验收中解释。
7. **E1 不发视频、不调模型、不读凭据内容。**

## 7. 复现命令

```powershell
.\.venv\Scripts\python.exe scripts/capture_m8_baseline.py
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs tests/test_episode_state.mjs tests/test_subtitle_state.mjs tests/test_render_state.mjs tests/test_season_state.mjs tests/test_timeline_state.mjs
```
