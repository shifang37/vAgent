# M8 验收：供应商路由、账单与发布

日期：**2026-10-25**。基线：`master` `3afb9da`（M8 E3 真实样本已合入；schema 9、tools/rules 18/18、168 个契约模型）。范围见 [M8 契约](./M8_CONTRACTS.md) §9 的 E4，冻结决策见 §1，上游证据见 [E0](./M8_E0_ACCEPTANCE.md)、[E1](./M8_E1_ACCEPTANCE.md)、[E2](./M8_E2_ACCEPTANCE.md) 与 [E3](./M8_E3_ACCEPTANCE.md)。**E4 未读取凭据、未访问真实供应商、未新增付费样本：真实视频 submit=0、文本模型调用=0、TTS 真实请求=0。** 离线套件的受控上游只连 `httpx.MockTransport`；浏览器与安装态复用同一份本地数据目录。

## 1. 交付内容

| 交付 | 实现位置 | 说明 |
|---|---|---|
| 离线端到端套件 | `scripts/evaluate_m8.py` | 默认离线、`--live` 明确拒绝；4 个用例走真实 Store、`provider-route-v1`、`billing-v1`、提交闸门与 Job Worker；外部 HTTP 只连受控传输。`--only` 仅用于开发调试 |
| 套件用例 | 同上 | `route-matrix`（18 行三供应商矩阵 + 4 行冻结探针逐字节复现、缺 Key、分镜求和、确定性、不写存储）、`refusal-matrix`（10 秒/图生/集级批量/过期价/未发布供应商/非法参数/`--require-available`）、`billing-errors`（受控 401/超时/429 与节流、统一错误表、录入/重放/冲突/替换/损坏账本、逐币种汇总）、`replay-no-new`（预览逐字节相同、同 operationKey 只登记 1 个 Job、账单重放不新增） |
| 套件测试 | `tests/test_m8_evaluation.py` | 4 项：整套报告与关键数值、`--live` 拒绝、不覆盖报告/不复用非空目录、套件身份与纯函数 |
| 受控浏览器 | `tmp/m8-e4-browser/seed.py`、`check.mjs`、`output/m8-e4-browser/` | Chrome 155 headless：报价对比三行候选（9.00 CNY / 1.26 USD / 1.68 USD·12 units、推荐 wan、不换汇、未做真实样本标签）、账单面板（USD 已录入 0.60、CNY 未录入不计 0、第 2 版）、同一账单重放提示「已复用」且记录数不变；3 张截图、零页面/控制台错误 |
| 安装态 wheel | `scripts/wheel_smoke.py` | 新增 `smoke_m8_evaluation`（安装态运行 4 用例）与 `smoke_m8_surface`（`route preview` CLI/HTTP、`usage bill show` CLI/HTTP、账单重放、`usage.billing`、`runtime` 非持久块） |
| 验收文档与发布 | 本文件、[M8 契约](./M8_CONTRACTS.md)、[M8 计划](./M8_PLAN.md)、[v0.6.0 发布说明](./RELEASE_0.6.0.md)、`README.md`、`M1_PLAN.md`、[M5 计划](./M5_PLAN.md)、[Harness 设计](../AGENT_HARNESS_DESIGN.md)、[Web 说明](../web/README.md) | 记录交付、边界、失败与复现；E3 的真实样本证据见 [E3 验收](./M8_E3_ACCEPTANCE.md) |
| 版本 | `pyproject.toml`、`src/vagent/__init__.py` | **0.5.0 → 0.6.0**；同时修正 `__version__` 自 v0.4.0 起未随 v0.5.0 更新的遗漏（`/api/health` 曾显示 0.4.0） |

## 2. 相对计划的实现决策

1. **4 个用例对应契约的 4 类事实。** 路由矩阵同时覆盖「三供应商 × 三种模式 × 5/10 秒 × 三种 scope」的 18 行策略与 E0 的 4 行 `routeProbe`；后者用与 `tests/test_m8_routing.py` 相同的投影和 `canonical_fingerprint` 计算 `rowsSha256`，因此「预览与冻结事实一致」在套件里也是机器可验证的。
2. **账单用例用受控终态 Job，不用真实提交。** 受控 Kling 依次返回 401、连接超时、429（`Retry-After: 120`），受控 Wan 返回 401；四次 Kling 提交与一次 Wan 提交都只连 `httpx.MockTransport`。401 与超时各证明「终态不重投 / 未知不重投」，429 证明「冷却按 provider 生效、被挡住时 `pending_submit` + `submitAttempts=0` + `PROVIDER_BACKOFF`、冷却结束后只提交一次」。
3. **损坏账本在副本上验证。** `billing-errors` 主目录保留可读账本（USD 0.60、revision 2），损坏用例写到同级的 `billing-errors-damaged/billing.json`，因此浏览器与安装态可以继续复用主目录；`record_bill` 对该副本报 `INVALID_STORE` 且原字节保留、`show` 报 `ledgerStatus=invalid`。
4. **重放不新增分三层证明。** 同一预览调用三次逐字节相同且 `state.json` 不变；同一 Run + operationKey 的第二次登记返回同一个 Job（Job 数 +1 而不是 +2，且不提交）；同一账单业务字段重放两次 `replayed=true`、账本字节不变，改成不同金额返回 `BILL_CONFLICT` 且仍只有一条记录。登记重放用离线 mock 适配器，避免留下真实待提交 Job。
5. **浏览器复用验收数据目录。** 复制 `billing-errors` 主目录为浏览器 home，再用产品 `storyboard_save` 工具加 3 个 t2v 5 秒镜头（不登记 Job、不提交）；报价对比因此显示逐镜头求和后的 9.00 CNY / 1.26 USD / 1.68 USD·12 units，账单面板显示同一条 USD 0.60 记录。服务以 live 模式 + 占位 Key 启动，但目录中 5 个 Job 全部是终态，全程无提交。
6. **安装态复用同一套件。** wheel 环境用 `-I` 运行 `evaluate_m8.py`，再读取 `billing-errors` 数据目录：CLI `route preview`（含 `--require-available` 拒绝）、`usage bill show`、HTTP `/api/route/preview`、`/api/usage/bill`、`/api/usage` 与 `?runtime=1`、同一账单重放全部通过，且 Job 数与账本记录数不变。

## 3. 验证结果

### 3.1 离线套件

```text
.\.venv\Scripts\python.exe scripts/evaluate_m8.py --home <new> --output <new.json>
4/4 passed；38 次预览、4 行冻结探针、18 行矩阵、6 次受控提交、7 个 Job、
3 次账单写入、3 次重放、1 条账本记录；0.25 秒。
```

- `route-matrix`：`frozenRouteProbeReproduced`（`rowsSha256 = 3817d19141f1e73b075722328b645be08e6a3ac2d4748caeea1e819c21f06413`）、`referenceShotComparesThreeProviders`（wan 3.00 CNY → fal 0.42 USD → kling 0.56 USD，推荐 wan）、18 行策略矩阵、集级/批量仅 CNY、缺 Key 只挡该供应商、3 镜头分镜求和（15 秒 → 9.00 CNY / 1.26 USD / 1.68 USD、12 units）、重复预览逐字节相同、`state.json` 不变、`jobs=0`。
- `refusal-matrix`：10 秒 t2v → wan `VIDEO_COST_LIMIT`、kling/fal `VIDEO_UNSUPPORTED_SPEC`；i2v/flf2v → kling/fal `VIDEO_UNSUPPORTED_MODE`；episode/batch → kling/fal `EPISODE_COST_LIMIT` 且保留报价；过期价 → `VIDEO_PRICE_UNKNOWN`；显式未发布供应商 → `VIDEO_PROVIDER_UNAVAILABLE`；9 组非法参数与未知分镜、`--require-available` 分别返回约定的 `INVALID_ARGUMENTS`/`ROUTE_UNAVAILABLE`；全程不登记、不写存储。
- `billing-errors`：`SUBMIT_ERROR_TABLE` 9 行与 `QUERY_ERROR_TABLE` 5 行逐项等于契约；401 → `PROVIDER_AUTH_FAILED`（1 次提交、不重投）、超时 → `SUBMISSION_UNKNOWN`（1 次提交、不重投）、429 → `PROVIDER_RATE_LIMITED` + 冷却，被挡住的 Job 保持 `pending_submit`/`submitAttempts=0`/`PROVIDER_BACKOFF`，冷却结束后只提交一次；账单首次写入 revision 1，相同业务字段重放 `replayed=true` 且账本字节不变，改金额 `BILL_CONFLICT`，显式替换 revision 2；汇总 CNY 估算 3.00 / 已录入 `null` / 1 个未录入，USD 估算 2.24 / 已录入 0.60 / 差额 −1.64 / 3 个未录入，无跨币种合计；损坏副本 `INVALID_STORE` 且原字节保留。
- `replay-no-new`：预览三次逐字节相同；同 operationKey 登记两次只新增 1 个 Job、0 次提交；账单重放两次 `replayed=true`、1 条记录、账本字节不变；不同金额 `BILL_CONFLICT` 且仍 1 条。
- 报告：`output/m8-e4-suite.json`（`suiteId=88c283bb329a8e20`、`mode=offline-fixture`、`realProviderCalls=0`、`textModelCalls=0`、`ttsRealRequests=0`）。

### 3.2 受控浏览器

数据目录为 `billing-errors` 主目录的副本（`tmp/m8-e4-browser/home`）。Chrome 155.0.8059.85（`channel=chrome`，headless，1440×1100）：

| 阶段 | 结果 |
|---|---|
| 载入与会话 | 通过（项目 `m8-e4`） |
| 分镜面板 | 3 个镜头，工具栏「报价对比」可用 |
| 报价对比 | 3 个可用候选：wan 9.00 CNY、fal 1.26 USD、kling 1.68 USD · 12 VIDEO_UNIT；推荐 wan；`sourceUrl`/`checkedAt`、「不换汇」「仅有文档依据，未做真实样本」可见 |
| 账单面板 | 「供应商账单」区：CNY 未录入（未知，不是 0）、USD 已录入 0.60、第 2 版记录与来源可见 |
| 账单重放 | 同 Job/金额/币种/来源/日期提交 → 「已复用」，记录数仍为 1 |
| 页面错误 | 0（page/console） |

证据：`output/m8-e4-browser/check.json` 与 `route-preview.png`、`billing-panel.png`、`billing-replayed.png`。

### 3.3 安装态 wheel

`scripts/wheel_smoke.py` **PASS**（`output/m8-e4-wheel-smoke.json`，4,276,330 字节；安装路径 `tmp/release-0.6.0-wheel-env/Lib/site-packages/vagent/__init__.py`，`temporaryDataCleaned=true`）：

- 安装态运行 M8 4 用例：4/4 PASS、38 次预览、4 行冻结探针、18 行矩阵、6 次受控提交、7 个 Job、3 次账单写入、3 次重放、1 条账本记录；真实供应商/文本/TTS 调用=0。
- 安装态 M8 三面：`vagent route preview --mode t2v --duration 5` 返回 wan 3.00 CNY / fal 0.42 USD / kling 0.56 USD、推荐 wan、不换汇；`--duration 10 --require-available` 以 `ROUTE_UNAVAILABLE` 拒绝；`vagent usage bill show --project m8-e4` 读到 USD 0.60（revision 2）且 CNY `reported=null`；HTTP `/api/route/preview`、`/api/usage/bill`、`/api/usage`（含 `billing` 与非持久 `runtime`）与同一账单 `POST /api/usage/bill` 重放（201、`replayed=true`、revision 仍 2）全部通过；Job 数与账本记录数不变。
- 既有 M1–M7 与 keys-only 段全部保持通过（含 5 个 M1-B、7 个 M1-C、7 个 M2、6 个 M3、5 个 M5、3 个 M6、3 个 M7 用例）。

### 3.4 完整回归与门禁

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1516 passed, 4 skipped in 1481.17s (0:24:41)

.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
All checks passed; 231 files already formatted

node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs \
  tests/test_episode_state.mjs tests/test_subtitle_state.mjs tests/test_render_state.mjs \
  tests/test_season_state.mjs tests/test_timeline_state.mjs tests/test_route_state.mjs \
  tests/test_bill_state.mjs
91 passed
```

- 4 项 skip 仍是既有的 Windows 符号链接/文件权限用例，未新增 skip；相对 E3 的 1512/4，本包新增 4 项 `test_m8_evaluation.py`。
- 本包新增：`scripts/evaluate_m8.py`、`tests/test_m8_evaluation.py` 4 项、wheel smoke 的 M8 两段、浏览器脚本与种子；`pyproject.toml`/`__version__` 升到 0.6.0；`.github/workflows/ci.yml` 补上 E1/E2 遗漏的 `route-state.js`/`bill-state.js` 的 `node --check` 与 Node 测试，CI 前端覆盖达到 10 个状态模块 91 项。
- 四组 CI：**TBD**（tag 与推送单独确认后运行）。

### 3.5 基线保护

重新运行 `scripts/capture_m8_baseline.py` 后 `docs/M8_E0_BASELINE.json` 的 SHA-256 仍为 `f3dc91c1c08db59ae75347d438e5fe44a6c0ac54b53da0ffaa12f4e8cbcb3706`：**168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希与 `routeProbe.rowsSha256` 全部逐字节不变**。E4 只新增 `evaluate_m8.py`、测试、浏览器脚本与文档，并修改 `pyproject.toml`、`src/vagent/__init__.py`（版本号）、`scripts/wheel_smoke.py` 与 `scripts/capture_m8_baseline.py`（把 `releaseVersion` 固定为冻结时的 0.5.0，见 §6.6），均不在 9 个已哈希契约模块内。

工具面仍是 **18/18**、schema 仍是 **9**、新增模型工具 **0 个**、新增 schema 集合 **0 个**。

## 4. 费用与「零新增」边界

| 项 | 数量 | 说明 |
|---|---|---|
| 真实视频提交 | **0** | 受控 Kling/Wan 传输只连 `httpx.MockTransport` |
| 文本模型调用 | **0** | 套件、浏览器与安装态都不调用模型 |
| TTS 真实请求 | **0** | 不使用真实 TTS；证据复用 E3 |
| 新增付费样本 | **0** | E3 的两条样本按单独授权执行，见 [E3 验收](./M8_E3_ACCEPTANCE.md) |
| 账单 | 不产生 | 全部金额都是本地记录/受控 Job 的冻结估算；实际扣费仍为 `unknown` |

## 5. 明确未做与边界

1. **Kling / fal 真实样本仍未做**：缺 `VAGENT_KLING_API_KEY`/`FAL_KEY`，E3 与本轮均为 0 请求；能力表继续 `documented-no-real-sample`，`capabilitiesSha256` 不变。
2. **不换汇、不合计**：CNY 与 USD 只并列展示；没有汇率、没有跨币种合计、没有「更便宜」结论。
3. **不自动重投**：`not_accepted`、`SUBMISSION_UNKNOWN` 与连接失败都不重投；恢复走显式 `shot_redo`/`episode_generate`。
4. **不抓取账单**：只接受人工录入的实际扣费；`unknown` 不是 0，也不是错误。
5. **不做 9:16 竖屏、混合供应商分集、跨集超级成片、镜头级 provider 冻结**（schema 10）。
6. **浏览器证据只证明展示与重放**：报价对比不登记 Job、不调用供应商；账单面板只写侧车账本，不改 `state.json`、不改任何 Job。
7. **费用口径偏差仍记录在案**：E3 的 CosyVoice 供应商 `usage.characters`（314）约为台词字符（170）的 1.85 倍；产品估算按台词字符、上限校验按供应商字符，二者都在 0.40 CNY 内，本轮未改冻结计价语义。

## 6. 失败与偏差记录

1. **套件首轮 `SuiteClock.advance` 用整数加 datetime 抛 `TypeError`。** 改为 `timedelta(seconds=...)`；这是套件脚本缺陷，不涉及产品代码。
2. **拒绝矩阵最初用 flf2v 10 秒断言 wan 可用。** 默认单 Job 上限 3.00 CNY 下 10 秒是 6.00 CNY，正确结果是 `VIDEO_COST_LIMIT`；改用 flf2v 5 秒验证「图生可用、kling/fal 模式不支持」，10 秒上限另由路由矩阵覆盖。
3. **节流断言最初读实时计数。** `kling_calls["count"]` 在「冷却结束后再提交」后继续增长，导致「被挡住时未发请求」误判；改为在挡住时快照 `calls_at_block` 再比较。
4. **重放用例最初把「登记重放」放在账单 Job 之前。** `JobWorker.run_once()` 会先提交更早的待提交 Job，导致账单 Job 仍在途、`record_bill` 返回 `BILL_RECORD_INVALID`；改为先提交并记账，再用离线 mock 适配器做登记重放（不留真实待提交 Job）。
5. **浏览器最初按单镜头金额断言，实际分镜面板按 3 个镜头求和。** 改为断言 9.00 CNY / 1.26 USD / 1.68 USD·12 units，并保留单镜头的 3.00/0.42/0.56 由套件与 CLI/HTTP 检查覆盖。
6. **`src/vagent/__init__.py` 的 `__version__` 自 v0.4.0 起未更新。** v0.5.0 发布漏改，导致 `/api/health` 显示 0.4.0；本版一并升到 0.6.0，并记录在发布说明。
7. **版本升级会让 E0 基线重新生成时改动 `releaseVersion`。** `capture_m8_baseline.py` 原先从 `pyproject.toml` 读取版本；升到 0.6.0 后重跑会把 `releaseVersion` 从 `0.5.0` 改为 `0.6.0`，SHA-256 随之变化（`21630008…`），违反「E0 基线冻结」契约。修复：脚本固定 `FROZEN_RELEASE_VERSION = "0.5.0"`（该字段记录冻结时的发布，而不是重新生成它的代码版本），重跑后 SHA-256 恢复为 `f3dc91c1…`，文件逐字节未变。E4 的基线复核以恢复后的 SHA 为准。
8. **Windows 工作区权限**：本轮开始时 `scripts/src/tests/docs` 的写入被 Windows 文件权限挡住，按诊断脚本结论（无 package ACE、`WRITE_DAC`/`WRITE_OWNER` 可得）判断为会话早期受限策略所致；文件策略切换为完全权限后 Ruff、pytest、构建与浏览器均正常，未手工修改任何 ACL。

## 7. 复现说明

1. 全新数据目录运行离线套件：`VAGENT_HOME=<new> .\.venv\Scripts\python.exe scripts/evaluate_m8.py --home <new> --output <new.json>`；本机还需 `PYTHONPATH=tests`（复用既有测试夹具导入约定）。
2. 浏览器：运行 `scripts/evaluate_m8.py` 后执行 `tmp/m8-e4-browser/seed.py` 复制 `billing-errors` 数据目录并添加分镜；以 `VAGENT_VIDEO_MODE=live` 与占位 `VAGENT_DASHSCOPE_KEY`/`VAGENT_KLING_API_KEY`/`FAL_KEY` 启动 `vagent web --port 3315 --no-open`，再运行 `node tmp/m8-e4-browser/check.mjs`；报告与截图写入 `output/m8-e4-browser/`。
3. 安装态：构建 wheel、在独立环境安装后运行 `scripts/wheel_smoke.py --output output/m8-e4-wheel-smoke.json`。
4. 基线：`.\.venv\Scripts\python.exe scripts/capture_m8_baseline.py`，比较 `docs/M8_E0_BASELINE.json` 的 SHA-256 与 §3.5。
5. 发布：`pyproject.toml` 0.6.0、`docs/RELEASE_0.6.0.md`、`dist/python/` 的 wheel/sdist；tag 与推送单独确认。
