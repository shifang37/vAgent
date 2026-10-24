# M8 E1 验收：只读路由预览与报价对比

日期：**2026-10-25**。基线 `master` `16f377b`（v0.5.0 发布后）+ M8 E0 文档（schema 9、tools/rules 18/18、168 个契约模型）。契约见 [M8 契约](./M8_CONTRACTS.md) §3、§7–§9，计划见 [M8 计划](./M8_PLAN.md) §3.2，机器可读基线见 `docs/M8_E0_BASELINE.json`。

E1 交付**只读**路由预览：`src/vagent/video/routing.py`（`provider-route-v1`）、`vagent route preview`、`GET /api/route/preview` 与 Web 报价对比面板。**未新增 schema、未新增模型工具、未改 9 个已哈希契约模块**；全程视频 submit=0、文本模型调用=0、TTS 请求=0、凭据读取=0（只读「是否配置」的布尔值）。

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 纯函数预览 | `src/vagent/video/routing.py` | `ROUTE_CONTRACT = "provider-route-v1"`、`RouteRequest`/`RouteCandidate`/`RoutePreview`（`Contract` 子类）、`provider_verification`、`make_request`、`resolve`、`preview`、`preview_for`、`effective_job_ceilings`、`quote_fingerprint`、`preview_fingerprint` |
| 本地只读动作 | `src/vagent/application.py` 的 `route_preview` | 解析分镜镜头、读取「Key 是否配置」、按当前配置计算有效单 Job 上限，调用同一个纯函数 |
| CLI | `src/vagent/cli.py` 的 `vagent route preview` | 只读、打印 JSON；不建 provider 连接、不写状态 |
| HTTP | `src/vagent/web.py` 的 `GET /api/route/preview` | 与 `/api/usage` 同级；不需要会话、Run 或 CSRF；只读 |
| Web | `web/route-state.js` + `web/app.js` 报价对比对话框 | 三行候选、可用性与原因、`sourceUrl`/`checkedAt`、「不换汇」说明；「使用该供应商」只写既有 `PATCH /api/config` 的 `videoProvider` |
| 测试 | `tests/test_m8_routing.py`（55）、`tests/test_m8_route_api.py`（6）、`tests/test_m8_route_cli.py`（2）、`tests/test_route_state.mjs`（6） | 共 **63** 项后端 + **6** 项前端；全部离线 |

## 2. 复现 E0 的冻结事实

`test_preview_reproduces_the_frozen_route_probe` 取出 `docs/M8_E0_BASELINE.json` 的 `routeProbe.rows`，逐条构造预览，把输出投影回探针行的形状（不可用行按探针口径丢弃 quote），再对整个列表做 canonical SHA-256：

```text
rowsSha256 = 3817d19141f1e73b075722328b645be08e6a3ac2d4748caeea1e819c21f06413
```

与 E0 基线的 `routeProbe.rowsSha256` **逐字节相同**。因此同一 5 秒 t2v 请求的金额、单位与顺序（wan 3.00 CNY → fal 0.42 USD → kling 0.56 USD / 4 `VIDEO_UNIT`）、10 秒的 `VIDEO_COST_LIMIT` 与 `VIDEO_UNSUPPORTED_SPEC` 组合、i2v 的 `VIDEO_UNSUPPORTED_MODE`、集级的 `EPISODE_COST_LIMIT` 都与 E0 冻结结论一致。

基线快照本身也重新生成并比对：`docs/M8_E0_BASELINE.json` 的 SHA-256 仍为 `f3dc91c1c08db59ae75347d438e5fe44a6c0ac54b53da0ffaa12f4e8cbcb3706`，168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希与 `rowsSha256` 全部不变。

## 3. E1 的实现决定

1. **每个供应商恰好一行**：按契约 §3.3 为每个请求规格选一条冻结条目（优先精确匹配 `(mode, durationSeconds, aspectRatio)`，其次同模式，最后该供应商第一条），再按「环境 → 凭据 → 能力 → 价格 → 记账」取第一条命中的 `reason`。
2. **按分镜的请求回声**：`storyboardId` 与 `mode` 二选一。按分镜预览时 `request.mode/durationSeconds/aspectRatio` 为 `null`，`storyboardId/storyboardVersion/shotIds` 为解析后的值；每个镜头各自判定，任一镜头失败即该供应商整行不可用，`reason` 取**第一个失败镜头**的原因（能力类先于价格类，与单镜头优先级同构）。
3. **金额是逐镜头报价之和**：`quantity` 为总秒数，`amount` 为各镜头冻结单价 × 各自秒数之和（十进制、两位小数），`resourceUnits` 为各镜头整数单位之和。已发布条目同一供应商单价一致，因此 `amount = unitPrice × quantity` 仍然精确成立。若所选镜头跨多个条目（wan t2v + i2v 的 `priceVersion` 不同），行内 `model/priceVersion/sourceUrl/checkedAt` 取第一个镜头，`notes` 明确写出这一点——契约要求「每个供应商恰好一行」，单行无法同时列出两个价格版本，这里选择**如实注明**而不是静默合并。
4. **quote 全有或全无**：只有当请求落在该条目的已核实规格内、且价格版本等于当前冻结版本时才给出 `priceVersion/currency/unitPrice/quantity/amount/resourceUnits/quoteFingerprint`。`VIDEO_MODE_OFF`、`VIDEO_KEY_MISSING`、`EPISODE_COST_LIMIT`、`VIDEO_COST_LIMIT` 这些「环境/记账」原因仍保留报价（比价面板即便在 off/mock 下也可读）；`VIDEO_UNSUPPORTED_MODE`、`VIDEO_UNSUPPORTED_SPEC`、`VIDEO_PRICE_UNKNOWN` 没有可展示的冻结报价，字段为 `null`。
5. **单 Job 上限纳入判定**：有效上限取当前配置——wan 用 `videoMaxJobCost`，kling/fal 取 `min(存储值, 冻结常量)`；`test_effective_ceilings_match_the_registration_path` 与登记路径的 `budget_ceiling` 逐 provider 对齐。默认 3.00 CNY 下 10 秒 wan 报 `VIDEO_COST_LIMIT`；调高 `videoMaxJobCost` 后同一请求变为可用（与 E0 探针的说明一致）。
6. **显式指定供应商**：`RouteRequest.provider` 是可选的「核对某一个供应商」输入。被点名的供应商若没有已发布条目，会得到一行 `VIDEO_PROVIDER_UNAVAILABLE`（`model` 为 `null`），其余供应商照常评估——这是契约 §3.3 第 2 行的可执行形式。该字段只出现在请求回声里，不改变任何设置。
7. **错误码边界**：参数形式冲突（`storyboardId` 与 `mode` 同时给出、两者都缺、`shotIds` 缺少分镜）返回 `INVALID_ARGUMENTS`；请求本身不可回答（未知分镜/版本/镜头、非法 scope/mode/duration、`--require-available` 无候选）返回 `ROUTE_UNAVAILABLE`。两者都不登记任何对象。
8. **确定性**：输出没有 `generatedAt`/`now`，`checkedAt` 来自冻结价格版本；同样的输入重复调用、把能力表倒序传入，`model_dump_json()` 与两个指纹都逐字节相同。`quoteFingerprint` 覆盖 provider/model/能力版本/价格版本/币种/单价/数量/金额/资源单位；`fingerprint` 再覆盖 scope、请求回声、候选顺序与 `recommended`。
9. **不换汇**：`notes` 固定写明 CNY 与 USD 分别计账、不做汇率比较或跨币种合计；集级/批量只接受 CNY。CLI/HTTP/Web 三个面都原样显示这条说明，Web 端 `route-state.js` 不提供任何合计字段。
10. **工具面零变化**：预览是用户决策而不是模型决策，因此没有 `provider_route` 工具；`VIDEO_TOOLS_VERSION`/`VIDEO_RULES_VERSION` 与 32 个已注册工具逐字节不变。

## 4. 三个面

```powershell
# 按规格：5 秒 t2v，输出三行候选与推荐
vagent route preview --mode t2v --duration 5

# 按分镜：读取当前版本（或 --version），只比较已选镜头（可重复 --shot）
vagent route preview --storyboard STORYBOARD_ID [--version N] [--shot SHOT_ID ...]

# 集级/批量口径；没有任何可用候选时以 ROUTE_UNAVAILABLE 失败
vagent route preview --storyboard STORYBOARD_ID --scope episode --require-available
```

```text
GET /api/route/preview?storyboardId=&version=&shotIds=&mode=&durationSeconds=&aspectRatio=&scope=&provider=&requireAvailable=
```

Web：分镜面板工具栏「报价对比」（scope=shot，优先使用已勾选镜头）、分集面板每场「集级报价对比」（scope=episode）。对话框显示三行候选的金额与币种、不可用原因、Key 是否配置、`sourceUrl` 与 `checkedAt`，以及「不换汇」和「切换只影响之后登记的任务」说明；「使用该供应商」调用既有 `PATCH /api/config` 的 `videoProvider`，遇到环境/启动配置覆盖时按既有 `CONFIG_OVERRIDE` 报错，不静默改写。

## 5. 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_m8_routing.py tests/test_m8_route_api.py tests/test_m8_route_cli.py
node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs tests/test_episode_state.mjs tests/test_subtitle_state.mjs tests/test_render_state.mjs tests/test_season_state.mjs tests/test_timeline_state.mjs tests/test_route_state.mjs
.\.venv\Scripts\python.exe scripts/capture_m8_baseline.py
```

| 项目 | 结果 |
|---|---|
| 新增后端用例 | **63 passed**（wan×kling×fal × t2v/i2v/flf2v × 5/10 秒全矩阵、拒绝矩阵、Key 有/无、价格版本缺失、显式不可用 provider、`--require-available`、排序稳定性、分镜求和、只读断言、CLI、HTTP） |
| 新增前端用例 | **6 passed**（`route-state.js` 的路径、行、摘要、切换与文案） |
| 前端套件 | **9 模块 84 项**全部通过（M7 为 8 模块 78 项，本包新增 `route-state.js` 与 6 项） |
| Ruff | `ruff check src/vagent tests scripts` 通过；`ruff format --check` **222** 个文件已格式化（E0 为 218，本包新增 `routing.py` 与 3 个测试文件） |
| 全量回归 | **1472 passed / 3 skipped**（E0 为 1409/3，本包新增 63 项后端用例；3 项 skip 仍是既有的 Windows 符号链接权限用例），耗时 **1548.02 秒（0:25:48）** |
| 基线 | `docs/M8_E0_BASELINE.json` 重新生成后 SHA-256 不变；`routeProbe.rowsSha256` 由真实预览复现 |
| 零花费 | 预览不登记 Job、不调用供应商、不调用模型、不读凭据内容；CLI/HTTP 用例在断言中核对 `state.json` 的 `jobs` 未变、`state.json` 字节未变 |

> 取证环境说明：本次运行所在的 Windows 沙箱对「运行期以 0700 创建的新目录」和带方括号的 `no_proxy` 取值有额外限制，因此证据命令是在一个**未入库**的本地 pytest 插件（`tmp/pytest_acl_plugin.py`，仅调整 mkdir 模式与代理旁路解析）下执行的；产品代码、测试与命令本身不依赖它，正常环境直接运行上面的命令即可。

## 6. 明确未做与边界

- **不新增工具、不改 schema**：没有 `provider_route` 工具，没有 schema 10，`DatabaseV9` 与 168 个模型哈希不变。
- **不做自动切换**：预览只给建议；不做「按价格自动换供应商」，不做「失败后换 provider 重试」，不改写任何已登记 Job 的冻结字段。
- **不预测运行态额度**：不预测 Run 剩余额度、已占用 Job 名额、集级/批量余量，也不读取 429 冷却；这些仍由登记路径与 E2 的提交节流兜底。
- **不换汇**：没有汇率、没有跨币种合计、没有「更便宜」结论；9:16 竖屏不在 M8。
- **未做真实样本**：三个供应商的 `verification` 仍是 `documented-no-real-sample`；E3 的真实请求需单独授权，且不改 `capabilitiesSha256`。
- **不抓取账单**：账单账本属 E2；本次没有新增任何账单文件或字段。

## 7. E2 入口

按 [M8 契约](./M8_CONTRACTS.md) §4–§5 开始 E2：`src/vagent/video/providers/gate.py`（逐 provider 最小间隔与 429 冷却、`PROVIDER_BACKOFF` 只写 `runtimeBlock`）、`src/vagent/video/billing.py`（`billing-v1` 手工账本、`vagent usage bill record|show`、`POST/GET /api/usage/bill`、`usage.billing` 汇总）。E2 仍为 submit=0、模型调用=0，并且必须保持本包与 E0 的全部哈希不变。
