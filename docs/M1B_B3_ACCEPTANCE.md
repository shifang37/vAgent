# M1-B B3 验收：持久等待与恢复

日期：2026-10-09。基于 B2 提交 `41be04a`；范围依据 [M1-B 计划](./M1B_PLAN.md)，数据与恢复约束见 [契约协议](./M1B_CONTRACTS.md)。

**B3 已实现生产等待、停止、独立计时和跨存储恢复。** 新 mock Run 通过 LangGraph 持久中断释放执行，结果就绪后回填原工具调用并继续；原预算、Job、Operation 和模型尝试记录保持。验证全部使用确定性模型和 Mock，没有请求 DeepSeek 或真实视频 API。

## 实现范围

| 位置 | 行为 |
|---|---|
| `waiting.py` | 原等待指针增加可缺省的 batchIndex、领域 timeoutError；恢复指针仍只接受 waitId/generation |
| `wait_runtime.py` | WaitService 负责准备、arm、稳定结果、版本领取、原 Operation 交付、停止和显式重等；WaitCoordinator 提供持久扫描、通知加速、启动/关闭 |
| `runner.py` | 注册等待解析器的新 Run 默认 execution v2；工具异常捕获之外 interrupt，同批重放保留原中断位置，Command 仅传服务端指针 |
| `checkpoints.py` | 同时核对 interrupts、next 与有效终态；识别旧 interrupt 和已完成节点 pending writes 共存的提交间隙 |
| `journal.py`、`storage.py` | 活动时间挂起/重启；外部时间包含等待期间离线区间；启动保留有效等待名额，不刷新已用模型/工具额度 |
| `tools.py`、`video/tools.py` | 通用资源解析器；当前视频规则/工具说明 v2，历史 B2 视图 v1；job 解析只读本地状态，不调用供应商 |
| `application.py` | 管理等待协调器生命周期，启动扫描、正常退出保留意图、用户停止先落盘，后台事件归属原 Run |

没有等待解析器的文本 Run 仍走 execution v1，原 CHECKPOINT_VERSION 保持 1。新 mock Run 保存 execution v2、视频 toolsVersion/rulesVersion 2；已存在的 v1 Run 使用原规则、工具说明与能力配置恢复。MCP 的只读限制、视频回答缓存绕过和原上下文布局保持。

## 提交与恢复规则

1. 工具先保存 preparing 与原上下文/指纹；Runner 随后保存批次位置、Run 等待意图和已用活动时间。这两个 JSON 提交之间退出也可从原 tools 节点重建中断。
2. SQLite 同步保存真实 interrupt 后才能 arm，随后再次读取 Job，补偿资源已先完成的竞态。等待时无模型连接或图执行任务；无变化扫描不改写 Run。
3. 稳定结果进入 ready，再比较版本与原 Run/配置/预算领取恢复权。结果按原 operationKey 保存；ToolMessage 使用原 toolCallId。同批尚未执行的工具完成后才请求模型。
4. SQLite 有时同时保留旧 interrupt、resume 值和已完成 tools 节点的 pending writes。只有对应 waitId/generation、Operation 与 ToolMessage 一致，才识别为已解决；通过原图推进已保存节点结果。
5. 后续模型尝试开始前确认交付。若 JSON 的 modelSteps 已超过 claimedModelSteps，重启只保留结果和用量，记录 EXPLICIT_RESUME_REQUIRED，要求显式恢复；不自动重发模型请求。已保存终态只补齐会话。

同一 Store 的 Runner 和多个协调器共用执行互斥。waiting_external 占用唯一未结束 Run 名额，用户停止后图尚未退出时也不能创建孤立的新 Run。

## 停止、退出与预算

- 用户停止先关闭未交付等待的 autoResume，保存 cancelled 和会话可见的终止工具结果，再取消正在继续的图。已经保存的 Operation 不改写；Job 可以继续完成，但不会唤醒已停止 Run。
- 正常服务退出保留等待意图，包含 preparing 阶段及 claimed 后尚未开始模型的情况。启动扫描核对两份存储后继续；缺失配置/Key/检查点或预算不足只保存 waitResumeError，领域结果保留。
- 外部等待使用 externalWaitStartedAt 与 externalWaitSeconds，活动时间仅计实际 Agent 执行。400 秒离线等待不消耗原 20 秒活动限额；重启提供更高策略也不增加原预算。
- 单次默认等待 600 秒。尚未准备好结果的等待到期后交付 JOB_WAIT_TIMEOUT；Job 不停止。已 ready/已提交结果保持稳定。
- 显式恢复尚无结果的停止等待会增加 generation 并创建新的截止时间，原工具只记一次额度。有稳定结果时复用原指针/结果；模型另发 await 调用则正常累计工具次数。较新会话请求使旧 Run 恢复返回 STALE_RUN。

## 离线回归

相对 B2 新增 **54 项**：`test_wait_runtime.py` 33 项、`test_wait_recovery.py` 16 项、`test_wait_compatibility.py` 5 项。既有 B0/B1 故障、迁移和文本/流式/MCP/缓存验证继续纳入全量回归。

| 场景 | 独立断言 |
|---|---|
| 正常等待 | 中断期间模型计数/Token/活动时间保持；图任务已释放，原工具预算不重记；成功后只追加原结果一次 |
| 同批两个等待 | 中间与之后的写工具各执行一次，结果 jobId 分别正确；第二次 next 为空也不会误判终态 |
| 快速完成 | 已完成资源直接返回；SQLite interrupt 已保存、JSON 尚未 arm 时完成 Job，也不丢失唤醒 |
| 稳定结果/错误 | 成功、生成/提交失败、提交 unknown、查询暂停、等待到期分别交付；没有新 submit 或替代 Job |
| 停止竞争 | ready、claimed、Operation 已提交时停止，后续显式恢复仍复用稳定结果；模型已开始时取消，后台不再次请求 |
| 再次等待 | 原调用停止后显式重等保持工具额度；新的模型步可再次 await，同名 callId 按模型步区分，两次结果不混用 |
| 配置/预算 | Key、模式、检查点缺失及模型/活动额度不足保留 ready 结果，无模型请求；配置验证期间延后继续，更高新策略不扩充原预算 |
| 生命周期 | Application 正常退出、准备中退出、协调器领取后关闭均保存意图；重开服务自动续接原 Run |
| 通用性/只读 | 非视频 report 资源走同一生产路径；伪造且无持久记录的延迟标记拒绝；只读能等待而不能生成或修改 Job |
| 并发/旧会话 | 多协调器与显式 resume 不并行执行图；停止清理期间不留下孤立 Run；旧结果不覆盖新请求 |

生产图强退使用真实子进程 `os._exit(73)`，覆盖 **13 个单等待点**：绑定保存、Run 准备、SQLite 中断后尚未 arm、armed、Job 终态、ready、claimed、resume 写入、节点 pending writes、Operation 写入、交付检查点、后续模型开始、最终图提交。另有 **3 个同批第二等待点**：prepared、armed、Operation 写入。

每个子进程退出后只手动移除该测试已确认结束进程的 instance.lock；生产代码不抢占锁。重新打开后核对原 Job/上游 ID、submit 次数、工具消息顺序、产物单版本、原预算与最终会话。模型请求开始后强退的场景保留未知用量与一次预留超时扣除，只能显式恢复。

兼容验证继续固定 M1-A 两种原上下文指纹；另从 B2 `41be04a` 源码捕获四个 mock 指纹，覆盖 context v1/v2 和普通/只读，当前 v2 Runner 实际恢复同一个 v1 检查点。旧 B2 已结束等待保持 preparing，后台不会自动复活。

## 基础验证与安装

Windows、Python 3.12.14：全量 **427 passed, 1 skipped**（95.82 秒），结果保存在被忽略的 `output/m1b-b3-tests-20261009.xml`。跳过项是当前 Windows 账户缺少符号链接权限。Ruff lint/format、`node --check web/app.js` 和开发环境 `pip check` 均通过。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1b-b3
```

`scripts/wheel_smoke.py` 使用独立 wheel 环境，继续验证文本/Web/流式/质量/MCP；视频部分验证唯一 Job、只读持久等待、关闭后重开服务、原 Run 自动继续、原预算与单次结果交付。Worker 由验证脚本显式推进，submit=1、query=2；不是 B4 自动 Worker 或 Job 页面验收。

已在独立虚拟环境安装锁定依赖与本地 wheel，在仓库外通过 10 项检查：安装、依赖完整性、模块实际安装位置、CLI skills/demo/inspect/usage、mock 配置、完整 wheel smoke，以及安装版的节点 pending writes 强退恢复。临时应用数据和实例锁均已清理；源码包与 wheel 位于 `dist/m1b-b3/`，未发布到 PyPI。最终安装报告与日志分别为 `output/m1b-b3-wheel-20261009.json`、同名 `.log`。

已验证的 `shifang37_vagent-0.2.0-py3-none-any.whl` 的 SHA-256：`06d4cba44ca0630528ff2382dacfdd8473ff96773cc3a0cecb35480086ccc27a`。

## 剩余边界

B4 仍需管理 JobWorker、CLI 持续等待/非阻塞输入/Job 命令、Job API/SSE/快照与页面卡片。CLI 目前在图挂起后返回 waiting_external；页面尚未提供完整等待与独立 Job 操作。B5 再执行完整应用矩阵及真实 DeepSeek + Mock 联调。此次没有真实视频媒体、真实模型质量评测、远端 CI 结果或 PyPI 发布。
