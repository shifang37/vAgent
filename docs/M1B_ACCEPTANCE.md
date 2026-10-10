# M1-B 验收：模拟视频 Job 与持久等待

日期：2026-10-10。初次验收基于 B4 提交 `dab5064`，提交收尾包含 CLI 取消修复 `8abe21e`；范围依据 [M1-B 任务规划](./M1B_PLAN.md) 与 [契约协议](./M1B_CONTRACTS.md)。

**B0–B5 已完成，M1-B 已验收。** 完整离线用例和真实 DeepSeek + Mock 套件均为 **5/5 通过**；收尾全量回归 **474 passed, 1 skipped**，另有 **7 项前端状态测试通过**。独立 wheel 已在仓库外通过 CLI、Web、自动 Worker、持久等待与原 Run 重启流程。视频侧全部为 Mock，成功仅代表模拟结果描述，没有生成真实媒体。

## B5 交付

| 位置 | 交付行为 |
|---|---|
| `scripts/evaluate_m1b.py` | 默认离线，`--live` 才调用 DeepSeek；五类用例均通过完整 ApplicationService、真实工具、Worker 和等待协调器执行 |
| 评测与报告 | 独立核对持久 Job、来源版本、原 toolCallId、Operation、等待交付和 Mock 上游账本；记录模型尝试、submit/query、显式/自动恢复次数、活动/等待时间及未知用量 |
| 评测续跑 | 首个失败即停止；`--continue-from` 只继续原可恢复失败 Run，校验原数据/检查点摘要和配置，保留此前报告及全部模型尝试 |
| `tests/test_m1b_evaluation.py` | 20 项新增回归：评分反例、失败即停、默认离线、累计预算、原 Run 续跑、旧报告拒绝和证据保护 |
| `scripts/wheel_smoke.py` | 增加仓库外安装版五类套件、console CLI 的 Job list/get/usage、可保存的安装报告；Windows 包来源校验统一规范路径 |

每个 Run 保持最多 **8 次模型调用、12 次工具调用、180 秒活动时间**。套件上限硬限制为 **40 次模型尝试**，剩余额度不足时缩减新 Run 的上限；续跑不能提高已保存的 Run 或套件额度。外部等待不增加模型调用。Mock 故障轨迹由评测服务端选择，不进入模型的生成参数。

评测只使用内置 Skills，关闭 Redis、用户 MCP 和外部 MCP；离线模式移除模型 Key，固定夹具仍通过真实工具及持久化路径。自然语言检查使用固定字面规则，真实答复另经人工式逐条复核；单套通过不代表生产成功率或创作质量保证。

## 完整离线矩阵

Windows / Python 3.12.14：初次 B5 全量 **472 passed, 1 skipped**，116.56 秒。报告：`output/m1b-b5-tests-final-20261010.xml`；此前 138.46 秒的同数量回归报告也保留。收尾纳入 `8abe21e` 的两项取消回归后，**474 passed, 1 skipped**，122.23 秒，报告为 `output/m1b-b5-close-tests-20261010.xml`。跳过的是当前 Windows 账户无符号链接创建权限的既有测试。Ruff lint/format、两个 JS 模块语法、7 项 Node 状态测试及 `pip check` 均通过。

| 验收场景 | 独立证据 |
|---|---|
| off/mock、非法规格与来源、冻结请求 | `test_video_contracts.py`、`test_video_jobs.py`、`test_video_tools.py`、`test_video_integration.py` |
| 原调用重放、同 Run 去重、重复 clientRequestId | Job/工具测试及 `test_job_application.py`、`test_web.py`；不靠模型答复判定幂等 |
| 排队/成功、生成失败、提交 unknown、查询暂停与恢复 | `test_video_worker.py`、`test_wait_runtime.py`；查询故障不会变成生成失败，不重新 submit |
| 七处 Job 提交/查询强退 | `test_video_worker.py` 的真实子进程与独立 Mock 账本；无确定上游 ID 保留 unknown，有 ID 只 query |
| 等待准备、同步中断、结果、领取、Operation/pending writes、模型响应丢失 | `test_wait_recovery.py` 的 13 个单等待点与 3 个同批第二等待点；保留原 ID、结果数和预算 |
| 等待零模型调用、同批工具、快速完成、重复唤醒 | `test_wait_runtime.py`、`test_waiting_probe.py`；图释放执行权，原结果只交付一次 |
| 停止/完成竞争、新会话、超时和预算 | `test_wait_runtime.py`、`test_job_application.py`；停止后 Job 可完成但不唤醒旧 Run，外部时间与活动时间分开 |
| 旧数据/检查点、只读和回答缓存 | `test_storage_migration.py`、`test_b0_compatibility.py`、`test_wait_compatibility.py`、`test_video_tools.py`；原上下文指纹和操作结果保持 |
| Job API/SSE、乱序、重连/溢出、CLI 输入与退出 | `test_job_application.py`、`test_job_cli.py`、`test_job_state.mjs`；含 Windows SIGINT 子进程与输入不阻塞 Worker |
| 既有文本、质量、流式、MCP、缓存与恢复 | 全量回归继续覆盖，没有改写生产 Runner、工具协议或模型配置指纹 |

独立离线评测报告为 `output/m1b-offline-20261010.json`，数据目录 `.vagent/evaluations/m1b-offline-20261010/`。五类合计 **14 次夹具模型调用、10 次工具调用、3 次 submit、6 次 query、2 次自动恢复**；全部 14 次 Token 用量保持未知，不将离线零观测量解释为真实免费调用。

初始离线开发报告 `output/m1b-b5-dev-01.json` 保留评分器字段名错误的记录，修正后的 `m1b-b5-dev-02.json` 与正式离线报告均通过。故障注入回归另验证：等待结果已交付后丢失一次模型响应，显式继续原 Run，套件累计从 7 次变为 15 次；原 Job、结果、预算和失败日志保持，submit=1/query=2。

## 真实 DeepSeek + Mock 套件

模型：`deepseek-flash`，thinking disabled，上下文预算 65,536 字节。使用原有本地配置获取 Key，报告不写入凭证。首次完整套件即 **5/5 通过**，没有额外真实重试或续跑。

| 用例 | 模型调用 / 工具调用 | 实际结果 |
|---|---|---|
| 登记后回复 | 3 / 3 | 返回原 jobId 与 pending_submit 登记事实；不调用 await，Worker 独立成功 |
| 等待成功后引用结果 | 4 / 5 | 持久挂起后原 await 调用交付成功；引用真实 Job、来源 ID 与 v1，无真实媒体 |
| 生成失败后如实说明 | 4 / 4 | 持久挂起后交付 JOB_FAILED，说明 generate/MOCK_GENERATION_FAILED，不新建替代 Job |
| 能力不满足时澄清 | 2 / 1 | 核对 60 秒 / 4K / 1:1 均不支持，列出合法组合并请求确认；没有 Job 或 submit |
| 视频未启用 | 2 / 1 | 无视频工具，说明只能准备文本、无法交付真实视频；没有 Job 或文本写入 |

合计：**15 次模型调用、14 次工具调用、3 次 submit、6 次 query**，无连接重试，2 次持久等待自动恢复，0 次显式恢复。已知输入 **55,036 Token**、输出 **3,115 Token**，15/15 次用量完整；活动时间合计 **36.032 秒**，外部等待 **5.543 秒**。

原始报告：`output/m1b-live-20261010-initial.json`；完整可恢复数据：`.vagent/evaluations/m1b-live-20261010/`。报告保存 state.json、SQLite 与 Mock 账本摘要。

| 用例 | 原 Run ID | 原 Job ID |
|---|---|---|
| 登记后回复 | `e07459cf-a243-4ba0-aa25-bbed33842ff8` | `3e59363a-809e-448b-9fc7-5cc4ec594701` |
| 等待成功 | `0b65711a-245d-4c1c-a5c2-74da7655a108` | `586e1795-71cf-4825-91d4-a28dbbd554cf` |
| 生成失败 | `b7cf6317-6239-4ecb-8569-11c041c51915` | `c7dadbeb-cbc6-4463-9759-ee091be49173` |

成功等待引用的来源为 `99b5c714-7067-4384-be4f-f8f0ca9ca5c7` v1，正文 38 个非空白字符。请求和结果中的 sourceRefs 相同，来源未被修改。另两类没有 Job。

## 页面与独立安装

页面核验使用已完成真实套件的完整数据副本，未新增模型调用、未改动原始报告或数据：

1. 成功 Job 显示“模拟完成”、原 jobId、5 秒 / 720p / 16:9 与“无真实媒体”。刷新后仍为原 Run，模型 4 步、工具 5 次。
2. 点击 Job 来源按钮打开原 brief v1，正文与 38 字计数一致。
3. 失败 Job 显示“任务失败”、generate/MOCK_GENERATION_FAILED；Run 显示“已完成”，表示 Agent 已如实答复失败，未误标 Job 成功。
4. 默认 494×570 视口无横向溢出；页面没有 video/audio 元素或下载链接，浏览器 error/warn 日志为空。

记录：`output/m1b-b5-browser-20261010.json`；来源、成功与失败截图为同目录 `m1b-b5-browser-{source,success,failure}-20261010.jpg`。B4 的等待中刷新/停止、恢复查询及原来源版本测试继续由原浏览器证据和本次自动化回归覆盖。

独立安装在仓库外新建虚拟环境，安装锁定依赖及本地 wheel，以 `-I` 检查包来自该环境；CLI 验证 version、skills、config、demo、inspect、usage。扩展 wheel smoke 同时验证：

- 打包的 Web 资源、配置脱敏、流式、文本保存、质量拒绝和两个本地只读 MCP 工具。
- 应用关闭后重开，等待续接原 Run/原 toolCallId；只读等待、原预算不变，原上游 ID 保持，submit=1/query=2。
- Job API/会话快照、安装版 console 的 Job list/get/usage、5/5 离线 M1-B 套件及临时数据/实例锁清理。

首轮安装探针因 Windows 8.3 短路径与规范路径比较不一致而退出，记录保存在 `output/m1b-b5-wheel-preflight-20261010.{json,log}`；统一解析路径后，`m1b-b5-wheel-preflight2-20261010.json` 的 12 项检查和完整 smoke 通过。安装后的包代码与生产行为无需修改。最终构建和检查脚本验证报告为 `output/m1b-b5-wheel-verified-20261010.json`、同名 `.log` 及 `output/m1b-b5-wheel-smoke-verified-20261010.json`；此前包含最终 README 的 `m1b-b5-wheel-20261010.json` 也通过并保留。临时虚拟环境与测试数据已清理。

已验证 wheel：`dist/m1b-b5/shifang37_vagent-0.2.0-py3-none-any.whl`；SHA-256：`7f36853e3eab0f613a355692a1069298ada68fd2a9e69de7d18852592d4d67d1`。原始真实报告的 SHA-256 为 `e9f21de500d48ef0e7688a953447467ed808761fbece4ab07971643415a96041`，页面核验和后续回归未改变该报告或原始数据。

## 提交收尾复验

2026-10-10：在 `8abe21e` 基线上复验 B5 的完整代码，包含 Python 3.11 通知与取消同时到达时保留 Ctrl+C 的修复。全量 Python 回归为 474 通过、1 跳过；7 项前端状态测试、Ruff lint/format、JS 语法与依赖检查均通过。

原始真实报告的 SHA-256 与上文一致，报告关联的 state.json、SQLite 检查点和 Mock 账本三个摘要均匹配；沿用已通过的 5/5 真实 DeepSeek + Mock 证据，本次收尾没有新增真实模型调用。

重新构建的 wheel 为 `dist/m1b-b5-close-20261010/shifang37_vagent-0.2.0-py3-none-any.whl`，SHA-256：`0ebeddd1308f0622fb98b15e5e33c4cfb9e67382a4ffd1273b5c8563cf2baec1`。在仓库外新建虚拟环境并安装锁定依赖后，12 项安装检查全部通过；完整 smoke 再次覆盖 CLI/Web、MCP、持久等待、原 Run 恢复、预算保持和 5/5 离线评测。

收尾安装报告为 `output/m1b-b5-close-wheel-20261010.json`、同名 `.log` 和 `output/m1b-b5-close-wheel-smoke-20261010.json`。smoke 自身的临时数据已清理；自动审批拒绝删除独立安装虚拟环境，返回 `blocked by policy`，因此 `%TEMP%/vagent-b5-close-20261010` 保留，报告的 `temporaryEnvironmentCleaned` 为 false。此限制不改变安装验证结果。

## 复现入口与边界

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_m1b.py
# 明确调用真实文本模型，视频始终为 Mock；默认套件上限 40 次。
.\.venv\Scripts\python.exe scripts/evaluate_m1b.py --live --output output/m1b-live.json
# 仅在该报告最后一个 Run 可恢复失败时使用；保留原 Run 和全部历史证据。
.\.venv\Scripts\python.exe scripts/evaluate_m1b.py --live --continue-from output/m1b-live.json --output output/m1b-resumed.json
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1b-b5
# 使用独立安装环境的 Python，从仓库外调用脚本：
# <installed-python> -I <repo>/scripts/wheel_smoke.py --output <new-report.json>
```

证据文件与评测数据位于 Git 忽略目录；阶段结论和复现步骤保存在本文。远端 CI 按 [B5 分支的 Agent checks](https://github.com/shifang37/vAgent/actions/workflows/ci.yml?query=branch%3Acodex%2Fm1b-b5) 的实际提交核对，本地验收不代替远端结果。本阶段没有真实视频媒体、视频供应商 API 调用或 PyPI 发布。下一阶段为 M1-C 的真实视频接入，仍需单独确定供应商、凭证、计费与媒体落盘验收。
