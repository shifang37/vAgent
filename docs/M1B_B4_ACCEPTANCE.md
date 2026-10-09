# M1-B B4 验收：CLI/Web 与 Job 生命周期

日期：2026-10-09。基于 B3 提交 `ce8eb8c`；范围依据 [M1-B 计划](./M1B_PLAN.md)，持久等待和存储约束沿用 [契约协议](./M1B_CONTRACTS.md)。

**B4 已交付自动 Worker、CLI 持续等待/Job 命令/非阻塞输入，以及 Job API/SSE/模拟任务卡片。** 所有验证使用确定性模型、临时数据和 Mock，没有请求 DeepSeek 或真实视频 API。B5 的完整真实模型套件仍待执行。

## 实现范围

| 位置 | 行为 |
|---|---|
| `application.py` | 同时管理 Worker 与等待协调器；关闭前先禁止自动继续，再停止 Worker、协调器和图执行；保留等待意图 |
| `video/jobs.py`、`video/views.py` | Job 提交落盘后通知；CLI/Web 共用有界视图，事件携带 Job 自己的项目、会话和创建 Run |
| `cli.py`、`console.py` | run/chat/resume 经共享服务执行，通知与持久状态扫描等待原 Run；Windows 轮询控制台，POSIX 使用可移除的输入监听，不创建阻塞输入线程 |
| `web.py` | Job list/get/retry-query；会话快照包含 jobs/wait；显式分发 Job、等待和恢复事件，沿用 Host/Origin/CSRF 边界 |
| `web/app.js`、`job-state.js`、样式/页面 | 模拟卡片、状态和错误、来源版本、等待停止及恢复查询；按 Job revision 合并事件、快照与 HTTP 响应 |
| `wait_runtime.py` | 自动恢复受阻时只在原因变化后发布通知，结果继续持久保存 |

新 Run 默认视频模式仍为 off。Worker 按已保存的 Job 模式工作，off 启动也可推进原 mock Job；恢复 Agent 另行校验原模式、模型与能力配置。版本化工具结果保持原字段，execution v1/v2、规则/工具版本和原上下文指纹不变。

## 入口与退出规则

- `vagent jobs list [--session ID]`、`get JOB_ID`、`retry-query JOB_ID` 不要求 Key，不启动 Worker、模型连接或 MCP。恢复查询只重开已确认上游 ID 的查询窗口，累计 submit/query 次数保持。
- `vagent jobs work` 持续推进队列和有效等待，不新建 Run。缺少原模型配置时仍推进 Job，保存结果与恢复原因。
- `run/chat/resume` 中的 Ctrl+C 持久停止当前 Agent；`jobs work` 的 Ctrl+C 仅关闭本地循环，保留等待意图。CLI 退出后没有后台守护进程，可重新启动 Web 或 jobs work。
- 浏览器关闭不影响服务 Worker。等待占用唯一未结束 Run 名额；停止后 Job 可以继续完成，但不会唤醒原 Run 或覆盖新会话结果。
- 同一数据目录继续实行实例锁。关闭中的未确认提交为 unknown，不重提；已确认上游 ID 保持。Job 无真实媒体，页面没有视频播放、下载或虚构百分比。

## 离线回归

相对 B3 新增 **25 项 Python 测试**：`test_job_application.py` 11 项、`test_job_cli.py` 14 项。另有 `test_job_state.mjs` **7 项前端状态测试**。

| 场景 | 独立断言 |
|---|---|
| 自动 Worker / 原模式 | 应用自动 submit/query；正常退出后在 off/mock 中继续原 ID；submit=1、query=2，无新模型调用 |
| 独立事件 | 创建 Run 完成后 Job 继续更新；另一个 Run 运行时不改变其 ID/事件；创建 Run 日志不因 Job 查询增加 |
| 等待与恢复 | CLI 和共享服务保持等待，图执行已释放；原 toolCallId 交付一次，工具额度和预算不重置 |
| 停止和配置 | 停止关闭 autoResume，Job 继续成功且旧 Run 不唤醒；缺 Key 保留结果，补回模型后显式恢复同一 Run |
| Job API | 读取不会查询上游；缺失 ID 为 404；查询暂停与生成状态分别显示；越界 Host/Origin/CSRF 拒绝 |
| 查询重试 | 暂停后 query=4，恢复沿用原 ID，成功时 query=6、submit=1；CLI 恢复命令本身不调用适配器 |
| SSE / 客户端 | Job 与 Run 事件不伪装成文本增量；溢出/重连附带当前 Job；重复、乱序、旧 HTTP 响应和其他会话事件不回退状态 |
| 控制台和退出 | 输入等待期间 Worker 完成 Job；Unicode/退格/EOF/Ctrl+C 可处理；真实子进程的 asyncio SIGINT 路径返回 130、释放锁，无遗留输入线程 |

Windows 子进程覆盖 run 等待、jobs work、chat 空闲输入及 jobs work 保留已有等待。SIGINT 由子进程自身的标准 signal 接口触发，走实际 asyncio 处理器；chat 的空键盘通过夹具模拟，不把它当作人工物理键盘验收。B3 的精确完成边界测试显式停用应用 Worker，继续由原夹具控制完成；B4 新测试独立验证应用自动调度。

Windows / Python 3.12：全量 **452 passed, 1 skipped**，117.59 秒；跳过项是当前账户缺少符号链接创建权限。报告：`output/m1b-b4-tests-20261009.xml`。Ruff lint/format、两个 JS 模块语法、7 项 Node 测试及开发环境 pip check 通过。

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
node --check web/job-state.js
node --test tests/test_job_state.mjs
.\.venv\Scripts\python.exe -m pytest -q --junitxml=output/m1b-b4-tests-20261009.xml
.\.venv\Scripts\python.exe -m pip check
```

## 浏览器验证

在独立本地测试服务、注入模型和模拟账本上，通过应用内浏览器操作真实页面：

1. 发送等待已有 Job 的请求，页面进入等待态；刷新后仍是原 Job/Run，模型 1 步、工具 1 次，停止按钮可用。
2. 点击停止后显示 cancelled，Job 卡片继续排队和更新；提供显式恢复入口。
3. 已暂停查询显示最近确认的生成状态、查询错误和恢复按钮；点击恢复后同一 jobId 完成模拟。
4. 已更新到 v2 的文本产物，其 Job 仍标明并打开原来源 v1。
5. 提示词中的 `<img ... onerror=...>` 按文字显示，卡片没有注入图片、视频或音频元素，页面无横向溢出，浏览器无 error/warn 日志。
6. Agent 登记后先回复并完成 Run；卡片随后从已登记推进到模拟完成，模型步数保持 2。

默认视口 1280×720。状态与调用计数记录于 `output/m1b-b4-browser-20261009.json`；证据截图保存为 `output/m1b-b4-waiting.jpg`、`m1b-b4-query-paused.jpg`、`m1b-b4-query-restored.jpg`、`m1b-b4-source-version.jpg`、`m1b-b4-completed.jpg`。浏览器恢复查询用例 submit=1、query=6，登记后回复用例 submit=1、query=2；没有执行真实模型或视频请求。

## 安装与剩余范围

已构建 wheel/sdist，并在仓库外新建独立虚拟环境，安装锁定依赖与本地 wheel；**14 项检查通过**，包括包来源、pip check、CLI skills/demo/inspect/usage/mock 配置、Job 登记/list/get、安装版 Worker SIGINT 退出，以及完整 wheel smoke。临时虚拟环境与数据目录已清理。报告和日志：`output/m1b-b4-wheel-20261009.json`、同名 `.log`。

`scripts/wheel_smoke.py` 使用应用管理的 Worker 和原 Run 的共享等待，验证关闭后重开、原工具结果与预算、submit=1/query=2，并检查 Job API、快照与打包的前端状态模块；测试只推进虚拟时钟，不手动调度 Worker。文本、流式、质量与 MCP 安装验证同时通过。

构建命令：`.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1b-b4`。已验证 wheel `shifang37_vagent-0.2.0-py3-none-any.whl` 的 SHA-256：`9ec8f999e5935912d2ceff1ad160402beee42faded9c86cda1f0938dbf789996`。未发布到 PyPI。

B5 仍需新增完整 M1-B 评测入口、真实 DeepSeek + Mock 五类套件与阶段验收文档。此次不包含真实媒体、真实模型质量评测、远端 CI 成功结论或 PyPI 发布。
