# M1-B B2 验收：视频工具与能力接入

日期：2026-10-09。基于 B1 提交 `87de167`；范围依据 [M1-B 计划](./M1B_PLAN.md)，数据和重放约束见 [契约协议](./M1B_CONTRACTS.md)。

**B2 已完成工具、模式、只读与缓存边界。** 确定性模型可以先读取能力、登记唯一模拟 Job，再读取登记状态并引用真实 jobId。没有调用 DeepSeek 或真实视频 API。生产图挂起/自动恢复仍属于 B3，Worker 生命周期、Job 命令和页面属于 B4。

## 实现

| 位置 | 当前行为 |
|---|---|
| `video/tools.py` | 注册 video_capabilities、video_generate、job_get、await_job；能力来自适配器；公开快照有界，不暴露 operationKey/执行上下文/私有账本 |
| `tools.py` | 增加服务端上下文执行器，直接返回已记账结果或 DeferredToolResult；旧执行器仍返回原始数据；外部异步 MCP 仍只读 |
| `config.py`、`application.py` | 启动读取 off/mock，默认 off；配置/API/设置页显示模式和来源；不支持本地配置写入或热切换模式 |
| `runner.py` | 按模式生成规则、保存工具/能力配置；为旧 off Run 选择原工具和规则；延迟标记不会序列化为成功结果 |
| `storage.py`、`waiting.py` | 新 Run 保存 videoMode/toolFeatures；等待保存原操作指纹，最终 Operation 尚不存在时也拒绝复用调用键执行其他操作 |
| `cli.py`、`web/app.js` | CLI 恢复选择已保存的模式，inspect 显示模式；Web 设置页仅展示模式/来源 |

模型不能指定 projectId、sessionId、runId、modelStep、toolCallId、operationKey、mode、Key、baseUrl 或模拟轨迹。作用域由执行端注入并在重放前核对。`video_generate` 沿用 JobService 的同事务登记与两层去重，不在工具内部提交上游，也不把已记账的 `{ok,data/error}` 再包装一层。

只读隐藏并拒绝生成，仍允许当前项目内读取和等待记账。视频工具可见时，整次 Run 在构建缓存键之前绕过 Redis，包括第一次模型调用；供应商报告的缓存 Token 和用量统计保持。MCP identities 仍只描述外部工具。

## 等待的阶段边界

- 无已有绑定、Job 已成功：立即保存并返回成功结果；失败、提交不确定、查询暂停分别返回 `JOB_FAILED`、`JOB_SUBMISSION_UNKNOWN`、`JOB_QUERY_PAUSED`。
- Job 未完成：同一 Store 锁内保存 preparing 绑定、原上下文/调用指纹与固定 10 分钟截止时间，只返回 DeferredToolResult，不创建成功 Operation。
- 已有绑定：始终返回同一 waitId/generation/resource，即使 Job 已成功或最终 Operation 已提交；重启不会重新计时，改参数返回冲突。
- 当前 execution v1：在工具异常捕获之外识别延迟结果，Run 以 `EXTERNAL_WAIT_UNAVAILABLE`、`resumable=false` 结束；此前工具结果保留，同批后续工具和下一次模型请求不执行。未实现 waiting_external、中断 arm、领取、结果交付、停止竞争或等待计时。

B2 的 preparing 记录不是可自动恢复的生产等待。B3 扫描必须先核对执行版本及 Run 状态；不能唤醒这些已经结束的 Run。当前 CLI/Web 不自动推进 Job，安装验证显式调用 B1 Worker；不能把这次验证描述为 B3/B4 的生命周期闭环。

## 离线证据

相对 B1 增加 **58 项**回归：新增 `test_video_tools.py`、`test_video_integration.py` 共 56 项，旧图兼容矩阵增加 2 项；原故障/恢复测试继续通过。

| 场景 | 独立断言 |
|---|---|
| 能力→登记→查询 | 应用共享 Runner 的确定性模型执行 5 次模型调用、5 次工具调用；同一 Run 只有一个 Job，登记时 submitAttempts=0 |
| 两层去重 | 原调用重放同一个 Operation；规范化参数变化在原键下冲突；新调用 ID 的相同请求复用 Job，不同请求携带原 jobId 拒绝 |
| 输入和来源 | 非法组合、未知模型/能力版本、隐藏字段、跨项目来源、缺失版本在登记前拒绝；合法源文本后续更新不改变冻结版本 |
| 上下文 | 缺失、伪造项目/会话、错误调用键和不同 Store 在访问结果或修改前拒绝 |
| 读取 | 同项目的新调用读取新 revision；原调用返回原快照；不存在和跨项目 Job 返回相同错误，不调用供应商 |
| 只读 | 模型看不到生成工具，强行调用拒绝；完整注册表也按 Run 的只读字段拒绝；等待只改变执行记录 |
| 待完成等待 | 保存一次绑定，截止时间固定；重启、Job 终态和 Operation 已提交后仍重放原标记；提交失败不留下部分记录 |
| 调用键保护 | 待完成等待的原键不能改成项目写入或 MCP 调用；不产生外部调用或副作用 |
| 即时等待结果 | 成功、提交失败、生成失败、unknown、查询暂停分别有稳定结果；恢复查询也不会改写原 Operation |
| 图边界 | 延迟结果不报告 tool.completed；同批之前的写入保留、之后的写入不执行；模型计数不增加；用户快照协议配对完整 |
| 配置与恢复 | off/mock 和来源展示，拒绝热切换；模式、能力版本、规格、规则和工具变化在新模型调用前拒绝 |
| 原批次恢复 | 视频登记后取消，在原模型步/调用 ID 下恢复，Job 和原预算保持，已记账工具不重复计数 |
| 旧图/旧数据 | 固定的 context v1/v2 签名保持；实际 v1→v2 迁移后，在 off 与 mock 启动条件下均能完成旧 Run |
| 缓存/用量/MCP | 可命中过时数据的缓存替身未被访问；正常与只读模式均保留模型缓存 Token；真实本地 stdio MCP 与四工具共存 |

基础验证：Python 3.12.14，`pytest -q` 为 **373 passed, 1 skipped**（43.57 秒）；跳过项为 Windows 账户缺少符号链接权限。Ruff lint/format、`node --check web/app.js` 与 `pip check` 通过。既有 B0/B1 强退与迁移、文本质量、流式、CLI 和评测回归包含在全量测试内。

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_video_tools.py tests/test_video_integration.py tests/test_b0_compatibility.py
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1b-b2
```

## 独立安装

`scripts/wheel_smoke.py` 已增加 mock 启动、确定性模型能力查询/登记、只读等待边界、重启后的原标记重放、显式 Worker 推进和新调用读取终态；继续覆盖旧文本/Web/流式/质量/MCP。必须用独立安装 wheel 的 Python 执行，脚本会验证模块来自该 Python 环境，数据全部位于临时目录。

本次已构建 `dist/m1b-b2/shifang37_vagent-0.2.0.tar.gz` 与同目录 wheel。新建独立 Python 虚拟环境，安装锁定依赖及 wheel 后，在仓库外完成：

- CLI `skills list`、`demo`、`inspect --session demo`、`usage --session demo`；mock 模式的 `config show` 正确显示环境来源且未配置 Key。
- 旧 Web 静态资源、配置保存、流式文本、产物质量拒绝及真实本地 stdio MCP。
- mock 模式下 3 次确定性模型调用完成能力查询和唯一 Job 登记；未完成只读 await 仅调用模型一次，按 B2 边界结束。
- 关闭并重开服务后，用虚拟时钟显式推进 Worker：submit=1、query=2，模拟结果落盘；原等待仍返回同一标记，新只读 Run 取得终态。
- 独立环境 `pip check` 通过；临时数据、环境和实例锁均已清理。

验证报告为被忽略的 `output/m1b-b2-wheel-20261009.json`，完整日志为同名 `.log`。已安装验证的 wheel SHA-256：

```text
ac0a0fc3c2c808daa6b432933298d0675a692c740621024b669e5eb809439edc
```

以上仅验证 B2 工具边界及 B1 的显式 Worker 推进，不包含生产图自动等待、后台生命周期或 Job 页面。源码包/wheel 仅本地构建，未发布 PyPI。

## 后续

B3 接入 execution v2 的持久挂起、停止、计时、领取/交付和故障补偿；B4 管理 Worker、CLI 交互与 Job API/SSE/卡片；B5 执行完整离线矩阵和真实 DeepSeek + Mock 套件。此次不包含真实视频、真实模型质量验收、远端 CI 结果或 PyPI 发布。
