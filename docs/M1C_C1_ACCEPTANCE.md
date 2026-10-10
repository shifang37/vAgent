# M1-C C1 验收：真实协议、数据兼容与应用接入

日期：2026-10-10。实现基线为 C0 `e879976`，工作分支 `codex/m1c-c1`。范围依据 [任务规划](./M1C_PLAN.md)、[冻结契约](./M1C_CONTRACTS.md)和[官方接口核实](./VIDEO_API_NOTES.md)。

**C1.1–C1.3 已完成离线验证。** 应用可登记真实万相任务、提交一次并持久查询原 ID。云端成功只保存为 `downloading`，带私有上游输出和稳定下载意图，`result=null`、`mediaAvailable=false`。本次真实视频提交数为 0、真实文本模型调用数为 0；测试中的 HTTP 调用均为受控响应。

## 1. 已交付范围

| 工作包 | 实现与边界 |
|---|---|
| C1.1 数据与兼容 | schema v3；独立 Job contract v2、费用、上游输出、下载记录、媒体索引与结果约束；保留 v1/v2 源版本校验器及旧 Mock Job v1 |
| C1.2 Wan HTTP | 固定北京业务空间端点、独立 httpx 客户端、提交歧义白名单、原 ID 查询、有限持久重试和期限；成功只登记待下载意图 |
| C1.3 应用接入 | 独立文本/视频 Key、保存后重启、off/mock/live 工具视图、live tools/rules v3、旧检查点恢复、CLI/Web 真实状态与费用投影 |

只开放 `wan2.7-t2v-2026-06-12`、`cn-beijing`、5 秒、720p、16:9。公共提示词最多 4991 字符，后端附加精确单镜头前缀；HTTP 显式发送 `resolution=720P`、`ratio=16:9`、`duration=5`、`prompt_extend=false`、`watermark=true`、`seed=0`。

估价按冻结的 0.60 元/秒 × 5 秒保存为 CNY `"3.00"`，金额使用 Decimal 字符串。实际费用始终保留 unknown，供应商 usage 不冒充账单。登记及提交前均检查配置与额度；报价变化或金额超限保留原任务，不能自动改价、换模型或重新生成。

## 2. 身份、恢复与隐私边界

- 迁移先验证源文件，再将原始字节备份为 `state-v1-<UUID>.json` 或 `state-v2-<UUID>.json`，最后原子替换。v1 只增加 jobs/waits/media，v2 只增加 media。历史对象、Operation/请求指纹及原始默认字段保持；运行态恢复另行提交。
- C0 的两份固定夹具及 manifest 哈希未改；真实 SQLite 检查点沿用历史签名实验验证。迁移不重建图、不修改 SQLite/WAL/SHM，也不把夹具内的字符串哨兵当成可恢复图证据。
- Job 登记先复用原 Operation/同 Run 意图，再解析新配置；每 Run 最多一个新 Job。提交意图与次数先持久化，终生最多一次 submit。合法受理 handle 单独提交后再应用初始快照；快照故障保留原 ID。
- 仅精确 HTTP/code 白名单且无受理数据时判定明确拒绝；矛盾响应、超时、断开、取消、非法 JSON 或提交中断均保持 unknown，不自动重提。查询未知状态或失败保留已确认的生成事实。
- 正常查询间隔 15 秒；失败后按 15/30/60 秒有限退避，合法 Retry-After 与本地退避取更晚时间。从提交意图起 24 小时后暂停；原 task ID、失败窗口与累计次数跨重启保留。串行 live 调用间隔至少 1 秒。
- 缺 Key、原业务空间不匹配、价格变化或额度不足保存 `runtimeBlock`，不增加未发送的 HTTP 次数，也不重复发布同一阻塞事件。已受理任务的查询不因后续调低金额上限而丢弃结果。
- off/mock/live 分别构造工具视图；原 off/mock Run 在 live 启动下仍按原签名恢复。live 的 Key/空间缺失返回 `RESUME_CONFIG_CHANGED`，不刷新累计执行或等待预算。MCP 连接与处理器复用，保持历史工具顺序。
- 配置保存与启动零生成。视频字段保存后重启生效，当前客户端、能力和队列配置保持启动快照；文本字段保留原有即时生效行为。配置接口显示保存值、当前模式、来源与重启字段，Key 只返回是否配置。
- 私有 URL、业务空间和供应商诊断不进入 Job 公共投影、工具结果、模型消息、SSE 或 CLI 查询；文本/视频 Key 不进入 Job、Operation 或检查点。签名 URL 仅保存在本地私有 Job 输出中。
- `await_job` 在 downloading 继续挂起，直到等待期限或停止；停止 Agent 不代表取消云端任务。没有本地媒体时不交付成功结果，不产生第二个 mediaId 或第二次生成。

## 3. 离线验证

Windows / Python 3.12 完整回归结果：**647 passed，1 skipped，132.72 秒**。跳过的是 `tests/test_context_skills.py` 中当前 Windows 账户没有符号链接权限的测试。Ruff lint/format、两份前端脚本语法检查及 **7 项前端状态测试**通过。

| 证据 | 覆盖内容 |
|---|---|
| [test_m1c_migration.py](../tests/test_m1c_migration.py) | C0 原字节与目标 envelope 哈希、备份/替换失败、未知/损坏版本、原指纹与消息、重复打开、SQLite 文件不变 |
| [test_video_contracts_v2.py](../tests/test_video_contracts_v2.py) | v1/v2 严格分派、请求/估价冻结、私有输出、状态约束、媒体与本地结果同事务、历史结果不可改 |
| [test_wan_provider.py](../tests/test_wan_provider.py) | 请求头/参数/端点、受理与无效附带快照、精确拒绝/矛盾响应、无隐式重试/跳转、响应上限、失败/UNKNOWN/CANCELED、Retry-After |
| [test_live_jobs.py](../tests/test_live_jobs.py) | 配置/来源/规格/价格边界、重放去重、受理后保存故障、提交取消、查询中断/重启/期限/间隔、配置暂停、稳定下载意图、混合 Job 与等待 |
| [test_live_application.py](../tests/test_live_application.py) | 双 Key 与来源优先级、保存零 HTTP、重启生效、三模式视图、原 Run 预算/签名、缺配置文本可用、下载等待/停止、HTTP/SSE/检查点脱敏、客户端关闭 |
| [test_live_cli.py](../tests/test_live_cli.py) | list/get 不启动服务且隐藏私有输出；retry-query 校验原空间和期限，只恢复窗口，不发 HTTP |
| 历史兼容与全量套件 | B0 固定签名、B2/B3 旧视图、真实 SQLite 恢复/等待领取/停止、M1-A/B、质量、只读/MCP/缓存、CLI 取消与前端 revision 回归 |

复现命令：

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
node --check web/job-state.js
node --test tests/test_job_state.mjs
.\.venv\Scripts\python.exe -m pytest -q -ra
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/m1c-c1
```

安装版继续使用 `scripts/wheel_smoke.py --output <新报告路径>` 在仓库外验证已有文本、Mock、配置、MCP、持久等待与退出流程。该脚本的媒体安装验收扩展仍属于 C3；C1 的协议集成证据来自上表受控 HTTP 测试。

## 4. 打包与安装

`python -m build --no-isolation --outdir dist/m1c-c1` 已成功生成 `shifang37_vagent-0.2.0-py3-none-any.whl` 和 `shifang37_vagent-0.2.0.tar.gz`。独立 wheel 环境安装该包后，仓库外 smoke 与 `pip check` 均通过，开发环境的 editable 安装保持可用。

安装证据保存在被忽略的 `output/m1c-c1-wheel-smoke.json`：确认导入自独立环境的 site-packages、Web 资源/配置/流式文本/质量/MCP、Mock 四工具、等待跨服务重启、原 Run/ID/预算、CLI 查询/用量、M1-B 五类离线套件，以及临时目录和实例锁清理。未把这些结果记为真实媒体安装通过。

仓库既有 Actions 继续对 Ubuntu/Windows × Python 3.11/3.12 执行 lint、前端检查、pytest、wheel/sdist 和仓库外 smoke；远端状态见 [C1 分支运行记录](https://github.com/shifang37/vAgent/actions?query=branch%3Acodex%2Fm1c-c1)。

## 5. 未完成项与下一步

下一项为 **C2.1**：独立媒体 Worker、有界流式下载、来源/重定向/路径校验、MP4 校验与文件提交强退恢复。C2.2 再提供媒体 API、Range、下载重试、播放器和完整视频设置表单。

本次未验证真实账户权限、实际扣费、媒体 CDN、真实 MP4 编码或浏览器播放；未安装视频插件或转码工具，未发布 PyPI。C3 在媒体和安装离线检查完成后，才进行最多一个真实 Job 的独立付费验收。因此本记录确认 C1 完成，不代表 M1-C 的视频交付阶段完成。
