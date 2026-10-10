# M1-C C2 验收：本地媒体交付、恢复与播放

日期：2026-10-10。实现基线为 C1 `30ca949`，工作分支 `codex/m1c-c2`。范围依据 [任务规划](./M1C_PLAN.md)和 [C0 冻结契约](./M1C_CONTRACTS.md)，C1 历史证据保留在 [C1 验收](./M1C_C1_ACCEPTANCE.md)。

**C2.1/C2.2 已完成。** 独立媒体 Worker 从原任务输出下载 MP4，完整文件与媒体索引提交后才交付成功；CLI/Web 支持原下载重试、播放、拖动和下载。受控 HTTP、本地测试媒体与实际 Chrome 验证均通过。本次真实视频提交数为 0、真实文本模型调用数为 0；测试中的供应商请求均由受控响应处理，未产生模型费用。

## 1. 已交付范围

| 工作 | 实现与边界 |
|---|---|
| 独立媒体 Worker | 由应用管理串行下载，持续推进原 Job；下载不占用供应商轮询步骤，不在 Store 事务中执行网络传输或文件校验 |
| HTTP 来源和资源限制 | 固定 HTTPS 主机白名单，每次跳转复核；DNS 仅接受公网地址并固定连接 IP，TLS 验证原主机；无 API 鉴权、Cookie 或环境代理，签名地址不进入公开响应/HTTP 日志 |
| 下载校验 | 最多 256 MiB，连接/读取/总超时 10/30/180 秒；每个自动窗口最多 3 次尝试，次数、下次时间和期限持久保存；从头重下 `.part` |
| MP4 与元数据 | 有界解析 box、轨道与样本表，核对样本位于文件内，要求自包含 H.264、1280×720、时长 5 秒（允许 0.1 秒误差），记录音频/帧率和 SHA-256；拒绝外部引用、分片 MP4 与不支持的结构 |
| 文件和索引提交 | 刷盘/关闭后保存准备记录，原子发布最终文件，再以一次 Store 事务提交 MediaAsset、JobResult 与 succeeded；没有完整文件不能交付成功 |
| 下载恢复与修复 | 重启、取消和显式重试复用原 Job、上游 task ID、mediaId；完整性匹配的准备文件可补交索引；修复必须匹配原大小、SHA-256 和元数据 |
| 当前可用性 | 启动及读取核验本地文件；缺失/损坏更新可用性和 revision，已交付结果与工具历史保持不变；冲突文件保留，先备份移走再重试 |
| 应用、CLI/Web | 配置/退出管理、媒体 API、单 Range、幂等下载重试、完整视频设置表单与同源播放器；沿用原工具与检查点签名，Mock 仍无 MP4 |

文件位于数据目录的 `media/<projectId>/<mediaId>.mp4`。Windows 使用不覆盖目标的原子 rename，POSIX 使用同目录 link/unlink 发布并同步目录；已有冲突最终文件不会被覆盖。路径通过服务端 ID 构造，目录/文件访问拒绝符号链接和 Windows reparse point，并保护核验与打开之间的路径替换边界。

MP4 检查是结构、样本边界和规格校验，不包含运行时转码或完整解码。浏览器解码证据来自下方本地夹具；真实供应商可能返回的其他结构、音频编码和内容质量仍需 C3 核实。

## 2. 对外行为

| 入口 | 行为 |
|---|---|
| `GET /api/media/{mediaId}` | 返回元数据、大小/哈希、来源与当前可用性，不返回绝对路径或签名地址 |
| `GET/HEAD /api/media/{mediaId}/content` | 已核验文件句柄，`video/mp4`、长度与 ETag；GET 支持单 Range 206、不可满足范围 416；非法/多范围回退完整 200；HEAD 忽略 Range 且无正文 |
| `?download=1` | 以服务端 mediaId 命名附件；默认 inline，断开时关闭句柄 |
| `POST /api/jobs/{jobId}/retry-download` | CSRF + `{clientRequestId, expectedRevision}`，202 登记原下载；同请求重放、执行中去重，参数冲突/过旧 revision 返回 409，非法字段和保留 ID 返回 422 |
| `vagent jobs retry-download JOB_ID` | 共用媒体服务登记恢复窗口，不直接发出 HTTP；随后 `jobs work` 或 Web 推进 |
| CLI `jobs list/get` | 显示媒体 ID、数据目录下相对路径、下载/可用性与费用；本地核验可保存新 revision，不调用上游 |
| 视频设置 | 独立视频 Key、空间、off/mock/live、固定供应商/模型/地域、参考估价与单 Job 金额上限，展示来源与重启提示；保存不创建生成任务 |

文件缺失或损坏时内容接口返回 410，页面移除播放入口；未知媒体 ID 返回 404。`If-Range` 只接受当前强 ETag，其他值返回完整内容。快照、SSE 与重试响应按 jobId/revision 合并，同一可用媒体保留原视频 DOM 节点，避免快照更新中断播放。

`await_job` 在下载期间继续等待，本地提交后回填原调用。下载耗尽返回明确失败；等待已超时、被停止或已结束时，后续下载/修复只更新 Job，不自动恢复 Run、不改写历史工具结果。无 URL 刷新、自动重新生成或后台守护进程；CLI 退出后由下次 Web/`jobs work` 继续。

## 3. 自动化验证

最终 Windows / Python 3.12.14 全量回归：**759 passed，2 skipped，181.71 秒**。两项跳过均因当前 Windows 账号无符号链接创建权限；Windows junction 拒绝测试通过。Ruff lint/format、前端语法检查及 **11 项前端状态测试**通过。输入校验及 Web 文件线程调整后的 API/应用/CLI 定向回归为 **34 passed**，随后已纳入上述全量结果。

| 测试 | 核验内容 |
|---|---|
| [test_media_http.py](../tests/test_media_http.py) | 来源、重定向、DNS 固定 IP/TLS 身份、长度/类型/超时/限流、过期和私有地址脱敏 |
| [test_mp4.py](../tests/test_mp4.py) | 测试媒体实测规格、容器/轨道/样本边界、截断与不支持结构 |
| [test_media_worker.py](../tests/test_media_worker.py) | 独立下载、完整提交、有限重试、窗口/期限持久化、状态不变量与规格故障 |
| [test_media_recovery.py](../tests/test_media_recovery.py) | 6 个实际进程退出点、索引写失败、磁盘/权限/路径/冲突、取消关闭、同哈希修复和不可变历史 |
| [test_media_api.py](../tests/test_media_api.py) | GET/HEAD/Range/If-Range、下载、410 与 revision、严格重试/CSRF/幂等、断开清理与同源边界 |
| [test_media_application.py](../tests/test_media_application.py)、[test_media_cli.py](../tests/test_media_cli.py) | 原 Run 等待交付、停止/超时/重启、设置零生成、CLI 原下载恢复与客户端退出 |
| [test_job_state.mjs](../tests/test_job_state.mjs) 与历史套件 | 当前可用性控制播放入口、修复与旧响应保护，M1-A/B/C1 历史指纹、迁移、等待/恢复与文本功能兼容 |

6 个媒体强退点为：下载意图后、写入中、文件已校验但准备记录未保存、准备记录已保存、最终文件已发布、索引已提交。均用实际子进程退出验证；重启保持原 ID 与一次提交。已有等待领取/交付强退测试继续随全量回归运行。

复现命令：

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
node --check web/job-state.js
node --test tests/test_job_state.mjs
.\.venv\Scripts\python.exe -m pytest -q -ra
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1c-c2
```

共享媒体应用测试使用 50 ms 的轮询间隔，避免低于 Windows 计时精度的轮询挤占文件线程；产品默认仍为 250 ms。普通 CI 读取已提交的夹具，不需要 FFmpeg，也不调用收费服务。

## 4. 浏览器与测试媒体证据

实际 Chrome **155.0.8059.39** 验证了以下结果：

- 浏览器读取 1280×720、5 秒元数据，成功播放并拖动到约 3.005 秒，媒体响应为 206。
- 附件下载 SHA-256 与原文件一致；快照更新保持播放，刷新后保留同一 mediaId。
- 删除测试文件后播放器消失；原下载修复成功后恢复同一 mediaId，保存视频设置没有增加生成次数。
- 页面错误为 0；受控供应商提交 1 次、媒体下载 2 次（含修复），均非收费网络调用。

本地证据在被忽略的 `output/m1c-c2-browser/`：`report.json`、截图、下载 MP4 和隔离数据目录的 `fixture.json`。结果记录的是测试环境，没有将本地夹具称为真实生成视频。

已提交夹具为 [sample-720p.mp4](../tests/fixtures/m1c/sample-720p.mp4)，209909 字节，SHA-256 为 `b179cca0ebb91e2b49a1e212df743037a8e292a3b754ee040c2124b31a3b6a54`。仅使用本地 FFmpeg `testsrc2`/`sine` 滤镜生成，H.264/AAC、10 fps；生成命令和来源见 [夹具说明](../tests/fixtures/m1c/sample-720p.md)。应用与测试运行时均无需 FFmpeg。

## 5. 打包与后续边界

`python -m build --no-isolation --outdir dist/m1c-c2` 已成功生成 `shifang37_vagent-0.2.0-py3-none-any.whl` 与 `shifang37_vagent-0.2.0.tar.gz`。核对 wheel 中的 5 个新媒体模块、Web 资源与工作区源码一致，sdist 中 MP4 夹具的 SHA-256 保持原样。

新建独立虚拟环境 `tmp/m1c-c2-wheel-env/`，安装 `requirements-dev.lock` 和上述 wheel；仓库外 `scripts/wheel_smoke.py` 与 `pip check` 均通过，开发环境的 editable 安装未替换。证据保存为被忽略的 `output/m1c-c2-wheel-smoke.json`，确认导入来自独立 site-packages，覆盖文本/质量/流式、Web 资源、配置脱敏、MCP、Mock 工具、持久等待跨重启续接原 Run、CLI 查询/用量、五类 M1-B 离线套件与清理。C3 专门的安装媒体评测仍待实施。

仓库既有 Actions 对 Ubuntu/Windows × Python 3.11/3.12 执行 lint、前端检查、pytest、构建和仓库外 smoke；远端结果以 [C2 分支运行记录](https://github.com/shifang37/vAgent/actions?query=branch%3Acodex%2Fm1c-c2) 为准，本地通过不替代远端状态。

下一项为 **C3.1**：`scripts/evaluate_m1c.py`、安装版媒体流程和专项验收报告；再进入 **C3.2** 的单个真实 Job 联调。真实账号权限、实际上游 CDN/MP4、浏览器兼容与实际扣费仍未验证；免费额度和估价均不充当账单证据。C2 完成不代表整个 M1-C 已验收，也未发布 PyPI。
