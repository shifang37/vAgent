# v0.6.0 发布说明

**发布日期**：2026-10-25  
**版本**：0.6.0  
**M8 E4 代码提交**：`df9eb74`（本发布文档的同步提交另计；tag 与推送单独确认）  
**范围**：M1–M8；本版新增 **M8 E0–E4（供应商路由、手工账单、提交节流、真实样本与发布）**

本版把**供应商侧**补齐：只读的 `provider-route-v1` 报价对比、`billing-v1` 手工实际扣费账本、逐 provider 提交间隔与 429 冷却，以及 **CosyVoice 整集配音**与**真实 DeepSeek 分集编排**两条真实样本。产品域不扩：存储仍为 **schema 9**、工具面仍为 **18/18**、168 个契约模型哈希与 `capabilitiesSha256` 全部不变；不换汇、不做 9:16 竖屏、不做发送后的自动重投。v0.5.0（2026-10-24）之后的提交全部纳入本版。

## 本版内容

- **M8 E0 契约与基线**：冻结 `provider-route-v1`（每供应商一行、8 级原因优先级、CNY→USD→金额→wan→kling→fal 排序、无时钟指纹）、`billing-v1` 侧车账本（`home/billing.json`，不进 `state.json`）、逐 provider 提交间隔 2.0 秒与 429 冷却 60 秒，以及 4 个新错误码（`ROUTE_UNAVAILABLE`、`PROVIDER_BACKOFF`、`BILL_RECORD_INVALID`、`BILL_CONFLICT`）；`docs/M8_E0_BASELINE.json` 记录 **168** 个契约模型哈希、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希、能力/价格表与 `routeProbe.rowsSha256`。
- **M8 E1 只读路由预览**：`src/vagent/video/routing.py`；`vagent route preview`、`GET /api/route/preview`、Web 分镜/分集面板「报价对比」。按分镜逐镜头判定并求和、按规格直接比价，有效单 Job 上限按当前配置计算；「使用该供应商」只写既有 `PATCH /api/config`，不改写已登记 Job。
- **M8 E2 供应商运维与账单**：`providers/gate.py`（逐 provider 2.0 秒提交间隔、60 秒 429 冷却、`Retry-After` 只延长且 900 秒上限、`PROVIDER_BACKOFF` 只写 `runtimeBlock`、`pending_submit` + `submitAttempts=0`、统一错误表常量、内存态延迟/退避/限流计数）；`billing.py`（幂等/冲突/显式替换、原子替换、损坏保留原文件、逐币种汇总且 `unknown` 不计 0）；`vagent usage bill record|show`、`POST/GET /api/usage/bill`、Web 用量面板「供应商账单」。
- **M8 E3 真实样本**：CosyVoice v2 对整集 19 条台词（170 字符）配音 19 次请求，19 个 48 kHz 单声道 WAV、FFmpeg 解码与 Range 206 通过，产品口径估算 **0.0340 CNY**；真实 DeepSeek（`deepseek-flash`）1 Run、4 次模型调用、`episode_plan`→`episode_save`→`episode_read`、输入 96,617 / 输出 2,176 Token。Kling/fal 因缺 `VAGENT_KLING_API_KEY`/`FAL_KEY` **未执行（0 请求）**，能力表继续标注「文档支持，未做真实样本」。两条样本暴露并修复两个缺陷：CosyVoice 流式 WAV 占位长度（`normalize_streaming_wav()`）与默认 64 KiB 预算装不下 18/18 工具面（`VAGENT_CONTEXT_BYTES` 默认升到 **98304**）。
- **M8 E4 验收与发布**：`scripts/evaluate_m8.py` 4 个离线用例（路由矩阵、拒绝矩阵、账单与错误映射、重放不新增）与 `tests/test_m8_evaluation.py` 4 项；受控 Chrome 155 报价对比 + 账单重放；安装态 wheel smoke 新增 M8 评估与 CLI/HTTP 三面；`docs/M8_ACCEPTANCE.md` 汇总复现命令。
- **既有能力不变**：真实 Wan t2v/i2v/flf2v 样本、Kling/fal 受控接入、字幕/配音/混音/转场/整集渲染、季/批量/时间线工作台、headless 与用户 skills、两 Key 直用 K0–K3。

## 验证

- **本地完整回归**：**1516 passed / 4 skipped**（1481.17 秒，0:24:41；4 项 skip 是既有的 Windows 符号链接/文件权限用例；相对 E3 的 1512/4 新增 4 项 `test_m8_evaluation.py`，日志 `output/m8-e4-pytest.log`）。
- **静态检查**：`ruff check` 通过；`ruff format --check` **231 文件**已格式化；10 个前端状态模块 **91 项** Node 测试通过；`.github/workflows/ci.yml` 补上 `route-state.js`/`bill-state.js` 的检查与测试。
- **离线验收套件**：4/4 通过、0.25 秒；38 次预览、**18** 行路由矩阵、**4** 行冻结探针、**6** 次受控提交、**7** 个 Job、3 次账单写入、3 次重放、1 条账本记录；真实供应商调用=0、文本模型调用=0、TTS 请求=0（`output/m8-e4-suite.json`，`suiteId=88c283bb329a8e20`）。
- **受控浏览器**：Chrome 155.0.8059.85 headless，6 步、3 张截图、零页面/控制台错误；报价对比显示 3 个可用候选（9.00 CNY / 1.26 USD / 1.68 USD·12 units，推荐 wan，含「不换汇」「仅有文档依据」），账单面板显示 USD 已录入 0.60、CNY 未录入（未知，不是 0）、第 2 版记录，并验证同一账单重放不新增记录（`output/m8-e4-browser/`）。
- **安装态 wheel smoke**：**PASS**（`output/m8-e4-wheel-smoke.json`）。从 `site-packages` 导入（0.6.0），覆盖文本/质量/MCP、Mock 视频工具、持久等待与重启续接、M1-B 五用例、M1-C 七用例、M2 工作台与七用例、M3 成片拼接与六用例、M5 分集工作台与五用例、M6 渲染工作台与三用例、M7 季/时间线工作台与三用例、**M8 4 用例与 `route preview`/`usage bill show`/HTTP 三面（含账单重放）**、两 Key 零费用探测与热更新，并清理临时数据。
- **四组 CI**：**TBD**（推送后运行；tag 与推送单独确认）。
- **真实付费样本**：本版**未新增**付费样本；E3 的两条真实样本按单独授权执行，Kling/fal 未执行、0 请求。

## 构建产物

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `shifang37_vagent-0.6.0-py3-none-any.whl` | 1,351,606 字节 | `2b11de75d23b5aff7c92cc13dda8ecad70d574d0b2fb90c24c221ad1e9231e2e` |
| `shifang37_vagent-0.6.0.tar.gz` | 2,467,929 字节 | `48133147c973f7f427e730fd0a62bc77c74e380c4a8f3b51e48b35f74fad9ef1` |

哈希为本次发布实测值；从同一提交重新构建不保证逐字节一致，请以本节记录为准。上一版产物已移到 `dist/python/archive-0.5.0/`。sdist 打包的是本文件定稿前的副本（打包后仅文档/本节哈希变动，sdist 内仍显示上一轮的 sdist 哈希；wheel 哈希与安装态 smoke 所用产物一致，仍为 `2b11de75…`）。

## 安装

要求 Python 3.11+。从本仓库构建并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# 或安装已构建的 wheel：
.\.venv\Scripts\python.exe -m pip install dist\python\shifang37_vagent-0.6.0-py3-none-any.whl
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\vagent web --mcp-local
```

打开 `http://127.0.0.1:3210`。报价对比：分镜面板「报价对比」或 `vagent route preview --mode t2v --duration 5`；账单：用量面板「供应商账单」或 `vagent usage bill record|show`。

## 兼容与升级

- 存储仍为 **schema 9**：M8 不新建、不迁移；账单是 `home/billing.json` 侧车文件（`billing-v1`），`state.json` 与 168 个契约模型哈希逐字节不变。
- 工具面仍为 **18/18**、规则字节不变；报价对比与账单录入是 CLI/HTTP 的本地动作，不是模型工具。
- 升级不会改写历史 Job：`JobV6.cost.actual` 仍是冻结的 `unknown`；账本只影响 `usage.billing` 的展示与汇总。
- `VAGENT_CONTEXT_BYTES` 默认 **98304**（M8 E3）；用旧 64 KiB 预算记录的 Run 若需恢复，临时设回 `VAGENT_CONTEXT_BYTES=65536`，否则按既有 `RESUME_CONFIG_CHANGED` 拒绝。
- `vagent` 包版本号同步为 **0.6.0**（此前 `src/vagent/__init__.py` 自 v0.4.0 起未随 0.5.0 更新，`/api/health` 会显示 0.4.0；本版修正）。
- Kling/fal Key 只在对应供应商请求中使用；账单动作不读凭据、不建 provider 连接、不登记 Job。

## 已知限制

- **真实 Kling/fal 样本仍未做**：缺 `VAGENT_KLING_API_KEY`/`FAL_KEY`，本轮 0 请求；能力表保持「文档支持，未做真实样本」，`capabilitiesSha256` 不变。
- **不换汇**：USD 与 CNY 分别记账、分别汇总，没有汇率、没有跨币种合计，也不做「按价格自动换供应商」。
- **不自动重投**：`not_accepted`（429/4xx/业务拒绝）、`SUBMISSION_UNKNOWN` 与连接失败都不自动重投；恢复走显式 `shot_redo`/`episode_generate`。
- **不抓取账单**：只接受人工录入的供应商实际扣费；余额/可用性不在预览中预测。
- **不新增付费样本**：E4 全程受控 HTTP 与本地状态，真实视频 submit=0。
- 不支持 9:16 竖屏、混合供应商分集、跨集「超级成片」、镜头级 provider 冻结。
- 公共 PyPI 发布未做；本版只提供仓库内构建的 wheel/sdist。

## 证据索引

- M8：[契约](./M8_CONTRACTS.md)、[计划](./M8_PLAN.md)、[E0](./M8_E0_ACCEPTANCE.md)、[E1](./M8_E1_ACCEPTANCE.md)、[E2](./M8_E2_ACCEPTANCE.md)、[E3](./M8_E3_ACCEPTANCE.md)、[M8 验收](./M8_ACCEPTANCE.md)
- 离线与浏览器证据：`output/m8-e4-suite.json`、`output/m8-e4-browser/`
- 基线：`docs/M8_E0_BASELINE.json`（SHA-256 `f3dc91c1c08db59ae75347d438e5fe44a6c0ac54b53da0ffaa12f4e8cbcb3706`）
- 上一版：[v0.5.0 发布说明](./RELEASE_0.5.0.md)
