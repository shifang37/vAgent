# M1-C C3.1 验收：离线评测与安装媒体流程

日期：2026-10-15。实现基线为 C2 `f74958c`，工作分支 `codex/m1c-c3`，代码提交 `a3cafba` 与 CI 修复 `206866f`。范围依据 [任务规划](./M1C_PLAN.md)、[冻结契约](./M1C_CONTRACTS.md)和 [C2 验收](./M1C_C2_ACCEPTANCE.md)。

**C3.1 已完成：专项离线评测 7/7 通过，仓库外安装的 wheel 通过含真实协议夹具与本地媒体的完整 smoke。** 本次真实视频提交数为 0、真实文本模型调用数为 0；所有供应商与媒体请求均由受控 HTTP 响应处理，未调用收费模型，未产生费用。**C3.2 真实联调尚未执行**：当前未配置 `VAGENT_DASHSCOPE_KEY` 与 `VAGENT_DASHSCOPE_WORKSPACE_ID`；账号权限、真实 CDN、真实输出编码/音频与账单继续保持未验证。

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

修复后 [运行 38696458231](https://github.com/shifang37/vAgent/actions/runs/38696458231)（`206866f`）**四组全部通过**。远端结果以 [C3 分支运行记录](https://github.com/shifang37/vAgent/actions?query=branch%3Acodex%2Fm1c-c3) 为准，本地通过不替代远端状态。

## 6. C3.2 真实联调（待执行）

未执行的原因：当前未配置视频凭证，不能发起真实提交。执行入口与预算已就绪：

```powershell
$env:VAGENT_DASHSCOPE_KEY = "<北京业务空间 Key>"
$env:VAGENT_DASHSCOPE_WORKSPACE_ID = "<业务空间 ID>"
.\.venv\Scripts\python.exe scripts/evaluate_m1c.py --live --max-model-calls 16 --output output/m1c-c3-live-<新名>.json
```

- 结构上只运行一个视频用例：**最多 1 个 Job、1 次 submit**；参考估算 3.00 CNY，实际账单未取得前保持 unknown。
- 计划从仓库外安装的 wheel 发起，使用独立数据目录；验收核对自然语言工具调用、原 task ID、完整文件、大小/哈希与浏览器实际元数据及播放下载。
- 仍然未验证（本次离线/安装证据不能替代）：账号视频权限与精确模型可用性、真实 CDN 主机与重定向、真实输出的编码/帧率/音频、浏览器解码与拖动、实际扣费与免费额度。

M1-C 的“自然语言需求 → 单条真实视频 → 本地交付”在 C3.2 完成后才算整体验收。
