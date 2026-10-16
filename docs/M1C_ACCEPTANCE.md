# M1-C 验收：离线评测、安装媒体流程与真实单镜头交付

更新日期：2026-10-16。实现基线为 C2 `f74958c`，工作分支 `codex/m1c-c3`；C3.1 代码提交 `a3cafba`（含 CI 修复 `206866f`、`b268828`），C3.2 修复提交 `9a49d71`。范围依据 [任务规划](./M1C_PLAN.md)、[冻结契约](./M1C_CONTRACTS.md)和 [C2 验收](./M1C_C2_ACCEPTANCE.md)。

**C3.1 已完成**：专项离线评测 7/7 通过，仓库外安装的 wheel 通过含真实协议夹具与本地媒体的完整 smoke（第 1–5 节，真实提交数为 0）。**C3.2 真实联调已完成（2026-10-16）**：已安装应用经真实 DeepSeek 提交 1 个万相任务并交付本地可播放 MP4（第 6 节）；期间发现并修复真实响应 `usage.SR` 数字形态导致查询暂停的问题，恢复沿用原任务，全程 submit=1、无二次提交；估算 3.00 元，实际账单保持未知。

## 1. 已交付范围

| 工作 | 实现与边界 |
|---|---|
| `scripts/evaluate_m1c.py` | 默认离线；`--live` 显式启用真实 DeepSeek 与万相。离线模式在真实 ApplicationService、live 工具、独立 Worker、持久等待与本地媒体管线上注入受控 Wan HTTP 与受控媒体响应；7 类用例覆盖立即完成、排队完成、生成失败、提交被拒、提交不确定、下载窗口耗尽后显式恢复、不支持规格 |
| 评测预算与续跑 | 每 Run 8 步 / 12 工具 / 180 秒活动时间；套件上限离线默认 40、真实默认 16，首个失败即停止；`--continue-from` 仅续接原可恢复失败 Run，校验原数据/检查点/受控账本哈希，保留此前报告与全部尝试；live 续跑不能创建新 Job（同 Run 去重加证据哈希校验） |
| 评测证据 | 每用例保存原 Run/Job、工具结果、等待交付、Operation、Job revision 事件，以及受控上游账本（提交体、鉴权/异步头、查询任务 ID、下载主机）或 live 的持久计数；媒体保存实测 SHA-256、大小、1280×720/5 秒/H.264 元数据与媒体 API 探测结果；报告不写入 Key、workspaceId 或签名 URL |
| `scripts/wheel_smoke.py` | 新增 `smoke_live_media`：安装版 live 协议与本地媒体流程，在下载进行中重启服务，核对原 Job/mediaId/providerTaskId 复用、submit=1、CLI 与媒体 API 证据；新增在安装环境运行 `evaluate_m1c.py` 离线套件 |
| `tests/test_m1c_evaluation.py` | 24 项回归：评分反例（伪造提交次数、外部任务 ID、媒体哈希、签名泄漏、等待结果等 15 类）、离线禁用付费模型与外部服务、失败即停、累计预算、原 Run 续跑与预算保持、旧报告/证据变化拒绝、上限校验与输出保护 |

真实模式（`--live`）结构上只包含一个用例（来源版本 + await 交付），因此一次验收最多一个视频 Job、一次提交；金额上限沿用 C0 冻结的 3.00 CNY 估算。

## 2. 离线验收

正式离线报告：`output/m1c-c3-offline-20261015.json`（开发过程报告 `m1c-c3-offline-dev-01…04` 保留）。Windows / Python 3.11.15。

| 用例 | 覆盖 | 模型调用 / 工具调用 | 结果 |
|---|---|---|---|
| register-and-reply | 提交即 SUCCEEDED（立即完成）；相同参数重放去重、不同提示词 `JOB_ALREADY_EXISTS`；Worker 独立完成 | 4 / 4 | 通过 |
| await-success | 来源 brief v1；PENDING→RUNNING→SUCCEEDED；持久等待交付；媒体 API 200/HEAD/206/416/下载附件 | 4 / 4 | 通过 |
| generation-failure | 查询 FAILED → `generate/PROVIDER_GENERATION_FAILED`，原 task ID 保留 | 4 / 3 | 通过 |
| submit-rejected | 400 InvalidParameter 明确拒绝 → `submit/PROVIDER_INVALID_REQUEST`，无 task ID | 4 / 3 | 通过 |
| submit-unknown | 提交超时 → unknown，不自动重提 | 4 / 3 | 通过 |
| download-recovery | 3 次下载失败 → download_failed；显式重试新窗口 generation=2 恢复同一媒体 | 4 / 3 | 通过 |
| unsupported-spec | 60 秒/4K/1:1 不可用；无 Job、submit=0、请求确认 | 2 / 1 | 通过 |

合计：**26 次夹具模型调用、21 次工具调用、6 次 submit、5 次 query、6 次下载、3 个媒体交付、3 次持久等待自动恢复**；Token 用量全部未知（离线夹具不产生真实用量），不把离线零观测量解释为免费调用。

受控账本核对（每用例）：提交体与冻结请求逐字段一致（模型、单镜头前缀提示词、`resolution=720P`、`ratio=16:9`、`duration=5`、`prompt_extend=false`、`watermark=true`、`seed=0`，无多余字段）；提交带 Bearer 鉴权与 `X-DashScope-Async: enable`；查询仅作用于原 task ID；下载仅访问允许集主机；Job 的 `providerTaskId` 与受控回执一致。媒体为已提交夹具 `tests/fixtures/m1c/sample-720p.mp4`（209909 字节，SHA-256 `b179cca0ebb91e2b49a1e212df743037a8e292a3b754ee040c2124b31a3b6a54`），交付文件哈希、大小与 1280×720/5 秒/H.264/AAC 元数据同索引一致；媒体读取探测 GET/HEAD/单 Range 206/不可满足 416/下载附件名均按冻结语义通过。

## 3. 安装验收

`python -m build --no-isolation --outdir dist/m1c-c3` 生成 `shifang37_vagent-0.2.0-py3-none-any.whl`（SHA-256 `4417df8d7e9ff3337f1a441169900db7473c91fdddd6223d725cf76ef042b9f1`）与 `shifang37_vagent-0.2.0.tar.gz`（SHA-256 `c9ba7d3a48b1dcc19ce3c118357d06d2b7e68ef95257d2f3bbc0e75a5f36ac67`）。仓库外新建独立虚拟环境 `tmp/m1c-c3-wheel-env/`，安装锁定依赖与该 wheel，`pip check` 通过；从仓库外以 `-I` 运行 `scripts/wheel_smoke.py`，10 项检查全部通过，证据为 `output/m1c-c3-wheel-smoke.json`：

- 安装包来源为该环境 site-packages；Web 资源、配置脱敏与流式、文本与质量、两个本地 MCP 工具。
- Mock 视频：持久等待跨服务重启续接原 Run，submit=1/query=2，CLI list/get/usage。
- **Live 协议与媒体**：受控提交 1 次、查询 1 次；下载在写入中被重启打断，恢复后沿用原 mediaId（`3d26da8b-a284-4276-8c04-d86b0921174d`）与原 providerTaskId（`wheel-task`），累计尝试 2 次且窗口未重置，submit 仍为 1；交付文件与夹具哈希一致；媒体 API GET/HEAD/206 通过；CLI `jobs get` 显示 succeeded 与受控相对路径 `media/wheel/<mediaId>.mp4`；运行中与结束后实例锁均释放。
- 安装环境运行 M1-B 五类与 M1-C 七类离线套件：5/5 与 7/7 通过；临时数据清理完成。

## 4. 自动化回归

Windows / Python 3.11.15 全量回归：**786 passed，2 skipped，215.24 秒**。跳过为当前 Windows 账户无符号链接创建权限的既有测试。Ruff lint/format 与前端语法检查保持通过。

| 证据 | 覆盖 |
|---|---|
| [test_m1c_evaluation.py](../tests/test_m1c_evaluation.py) | 24 项评测回归（见第 1 节） |
| 既有全量套件 | M1-A/B/C1/C2 的协议、迁移、媒体、恢复、等待、质量、只读/MCP/缓存与前端状态测试继续运行 |

复现命令：

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
.\.venv\Scripts\python.exe -m pytest -q -ra
.\.venv\Scripts\python.exe scripts/evaluate_m1c.py --output output/m1c-c3-offline-<新名>.json
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1c-c3
# 使用独立安装环境的 Python，从仓库外调用：
# <installed-python> -I <repo>/scripts/wheel_smoke.py --output <新报告.json>
```

## 5. 远端 CI

仓库 Actions 对 Ubuntu/Windows × Python 3.11/3.12 执行 lint、前端检查、pytest、构建与仓库外 smoke。

首轮 [运行 38692797642](https://github.com/shifang37/vAgent/actions/runs/38692797642)（`a3cafba`）保留：Ubuntu 3.11/3.12 与 Windows 3.12 通过；Windows/Python 3.11 在既有 `smoke_video_tools` 的等待断言处失败（`wheel_smoke.py` 第 120 行 `pending["status"] == "waiting_external"`，失败日志已取回核对）。该 runner 的同一 smoke 步骤耗时 56 秒（C2 通过时为 27 秒）；慢速环境中排队中的 mock Job 可能在等待 Run 的 `await_job` 执行前完成，使 Run 直接以结果结束。修复 `206866f` 在首个服务实例中冻结 Job 时钟：提交仍立即发生，排队查询改为等待显式重启；并为两处等待断言补充状态与错误码消息，便于后续失败自证。本机在独立安装环境复跑 smoke 通过。

修复后 [运行 38696458231](https://github.com/shifang37/vAgent/actions/runs/38696458231)（`206866f`）**四组全部通过**。

文档提交 `afbb6cb` 的 [运行 38698120292](https://github.com/shifang37/vAgent/actions/runs/38698120292) 在 Ubuntu/Python 3.11 暴露另一处既有竞态（与文档变更无关）：停止等待 Run 的同时，协调器已通过的扫描在 `advance_wait` 再次校验自动恢复资格前撞上停止事务，向已取消的 Run 写入 `run.resume_blocked (WAIT_NOT_READY)`，触发 `test_media_application.py::test_download_can_finish_after_stopped_or_timed_out_wait[stop]` 的“停止后 Run 记录不变”断言（1 failed、784 passed、3 skipped）。修复 `b268828` 把资格校验移入 `block_auto` 的同一事务：停止或交付抢先完成的扫描不再记录阻塞事件与 `waitResumeError`。定向套件 71 项、该用例连续 25 次与全量回归 786 项通过。

随后文档提交 `da5de7c`（纯文档变更）在 Windows/Python 3.12 的 `wheel_smoke.py::smoke_live_media` 收尾等待处超时（[作业 116845557461](https://github.com/shifang37/vAgent/actions/runs/38861808016/job/116845557461)）：重启后 `wait_for_run(..., 5)` 的 5 秒活性界限在慢 runner 上不足，相同代码在前两个提交的同一平台均通过。修复把该函数的四个活性界限放宽到 30 秒并注明其不是速度断言；本机以安装版 wheel 从仓库外复跑完整 smoke 通过（`output/m1c-c3-wheel-smoke-r2.json`）。

远端结果以 [C3 分支运行记录](https://github.com/shifang37/vAgent/actions?query=branch%3Acodex%2Fm1c-c3) 为准，本地通过不替代远端状态。

## 6. C3.2 真实联调（2026-10-16 完成）

### 6.1 执行方式、预算与预检

- 从仓库外安装环境发起：`tmp/m1c-c3-wheel-env/` 内以 `-I` 运行 `scripts/evaluate_m1c.py` 与 CLI/Web；独立数据目录 `tmp/m1c-c3-live/home-live-r1`。
- 预算：套件上限 16 次模型调用；真实样本最多 1 个视频 Job、1 次 submit（实际全程 1 次）；金额上限沿用 C0 冻结的 3.00 CNY 估算。
- 零费用预检：DeepSeek `GET /models` 返回 200（`deepseek-flash` 在线）；对不存在的任务 ID 发起一次 DashScope 查询返回 200 / `task_status=UNKNOWN`，证明 Key、业务空间主机与鉴权有效（查询不扣费、不生成）；安装版离线套件两轮 7/7（`output/m1c-c3-live-preflight-20261016.json`、`output/m1c-c3-live-preflight-r2-20261016.json`）。

### 6.2 首轮真实运行与发现的协议偏差

首轮（安装 wheel `6e020257…`，提交 `09a0a37`）报告保留为失败记录：`output/m1c-c3-live-20261016-r1.json`（状态 error）。

- 真实 DeepSeek（`deepseek-flash`）完成 5 次模型调用（21,379 输入 / 919 输出 Token，覆盖率完整）：`project_read → artifact_read(brief v1) → video_capabilities → video_generate → await_job`；登记请求的冻结参数与来源版本逐字段一致。
- 云端任务 `cb6a8290-f6f1-40f6-b745-fc1c452aeab1` 仅提交 1 次（13:46:20 本地提交），13:48:40 生成成功（约 140 秒）。
- 生成成功后的应用查询被判为 `QUERY_PROTOCOL_ERROR`：12 次查询（含 15/30/60 秒退避）后按策略暂停（`retry_exhausted`）。等待在期限（600 秒）前收到可操作错误 `JOB_QUERY_PAUSED` 并自动恢复一次；模型如实报告查询已暂停、无法确认状态；Run 正常完成（活动 7.36 秒，外部等待 245.9 秒，未超原预算）。
- 评测器 `grade()` 在 Job 无下载记录时以 `TypeError` 崩溃，报告状态为 error——两个缺陷都由本次真实样本暴露：离线夹具此前把 `usage.SR` 定为字符串，且评分路径未覆盖“成功用例未交付”。

根因（用保存的真实响应原文离线复现）：真实 `usage.SR` 为数字 `720`，与冻结契约的字面量 `720P` 不符，快照校验失败。真实响应（脱敏）存于 `output/m1c-c3-live-probe-task-20261016.json`。

### 6.3 修复与验证

- `9a49d71`：适配器将数字 `720/1080` 归一化为契约字面量（未知值仍拒绝）；评测器对无下载记录或无媒体的成功用例优雅判 False；离线夹具改钉真实数字形态。新增/调整 6 项回归。
- 验证：真实响应原文离线复现通过（usage 5.0 秒 / 720P / 16:9，签名到期 2026-10-17T05:48:38Z，主机 `dashscope-a717.oss-accelerate.aliyuncs.com`）；全量回归 **794 passed / 2 skipped**；安装版离线套件 7/7（r2）；对原失败证据重评不再崩溃（29 项检查：23 项通过，6 项交付类如实为 False）。

### 6.4 恢复与真实交付（零新增费用）

- `vagent jobs retry-query`（安装版 CLI）恢复原任务查询（revision 28，error 清空，submit 保持 1）→ Web 服务启动后第 13 次查询摄取 SUCCEEDED，随即下载 1 次即成并原子提交媒体。
- 结果：Job `d9e25ca5-ca57-40b8-a0d2-52566708dfaf` `succeeded`、媒体 `65b0e452-39f1-4eb7-ae00-bc7264d5d22c` 可用；`submitAttempts=1`、`queryAttempts=13`、`download.attempts=1`（generation 1，策略 `wan-result-hosts-v2`）。
- 媒体：3,183,756 字节，SHA-256 `9d87d7fa17d3edcc0f6fb1cb2ea3b9fd27e1dc2d4f4f7104913f7a5d81fe8a8c`；1280×720、5.033 秒、H.264 + AAC、30fps、`mp4-avc-v1` 校验；真实结果主机为 v1 允许集内的 `dashscope-a717.oss-accelerate.aliyuncs.com`，签名 24 小时，未观察到重定向。
- 媒体接口（`output/m1c-c3-live-recovery-20261016.json`、`tmp/m1c-c3-live/media-api-curl.txt`）：详情/GET/HEAD/单 Range 206/不可满足 416/下载附件名/ETag 与哈希一致，全部通过。
- 浏览器（Chrome 155 headless，真实页面）：元数据 1280×720 / 5.04 秒、实际播放、拖动到 3.0 秒、`<video>` 三次 206 Range 请求、页面下载按钮保存文件与资产哈希一致、零页面错误；证据 `output/m1c-c3-live-browser-20261016.json`、截图 `output/m1c-c3-live-browser.png`。
- CLI：`vagent jobs get` 显示 succeeded、媒体可用、受控相对路径与费用记录（`tmp/m1c-c3-live/cli-jobs-get.json`）。

### 6.5 重启不重复提交

服务以新进程再次打开同一数据目录：15 秒内两次快照完全一致（零写入），`revision 33`、submit=1、query=13、download=1、媒体可用与 task ID 均不变（`tmp/m1c-c3-live/state-web2-t0.json`、`state-web2-t1.json`）。恢复过程本身即为跨进程续接：评测进程退出后，由 CLI/Web 新进程完成查询、下载与交付，submit 始终为 1。强杀进程后实例锁按设计保留，确认无 vagent 进程后手动移除（运维观察；优雅退出自动释放）。

### 6.6 费用与用量

- 估算 3.00 CNY（0.60 × 5 秒，价格版本 `wan27-beijing-720p-20261010`）；上游 usage（5 秒 / 720P / 16:9）已记录；**实际扣费未取得，保持 unknown**，免费额度是否抵扣未知。
- 真实模型用量：首轮 5 次调用 21,379 输入 / 919 输出 Token，覆盖率完整；恢复不新增模型调用。

### 6.7 安装与远端

- 修复后 wheel `8c4af34f…`（提交 `9a49d71`）在仓库外安装环境通过完整 smoke（安装包来源、Web 资源、配置、流式、文本/质量、MCP、Mock 视频、持久等待与重启续接、预算保持、CLI、M1-B 五类与 M1-C 七类离线套件、live 协议夹具与本地媒体、清理）：`output/m1c-c3-final-wheel-smoke.json`。
- 分支 CI：`55bf5a8`、`09a0a37`、`9a49d71`（修复提交，[运行 38859024527](https://github.com/shifang37/vAgent/actions/runs/38859024527)）与 `26689b2`（文档提交，[运行 38860636066](https://github.com/shifang37/vAgent/actions/runs/38860636066)）均四组全部通过；此后文档提交的远端结果以 [C3 分支运行记录](https://github.com/shifang37/vAgent/actions?query=branch%3Acodex%2Fm1c-c3) 为准。

### 6.8 仍未验证 / 与计划的差异

- 实际扣费与免费额度抵扣；真实结果链接过期后的行为（本次在 24 小时窗口内完成，本地缓存不受影响）。
- 生成进行中的真实服务重启未覆盖（云端 140 秒内完成）；由恢复流程的跨进程续接与 C3.1 离线强退矩阵共同覆盖。
- 上游断点续传、多区域 CDN、浏览器以外的播放设备。
- live 套件报告状态为 error（诚实保留）；修复后未重新发起收费样本（预算仅 1 次提交），交付证据来自恢复流程与独立探测。
