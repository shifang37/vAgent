# vagent Web 工作台

DeepSeek Harness 风格的本地创作工作台，已连接真实 Agent。

## 启动

在仓库根目录安装 `requirements-dev.lock`。启动后可在配置向导中填写 DeepSeek Key，也可使用 `.env`：

```powershell
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m vagent web --mcp-local --no-open
```

访问 http://127.0.0.1:3210 。可使用 `--port` 换端口。静态 `http.server` 已不能提供 Agent API；前端不会静默回退成模板。

## 交互与数据

- 创建、切换、搜索服务端会话；刷新恢复选中的会话。
- 设置页保存本地 Key/模型，显示配置来源；独立的验证按钮只发起一次短模型请求，保存本身不产生模型费用；保存视频 Key 时执行一次零费用只读探测（不创建任务），结果区分鉴权通过与未验证。
- 回复逐段显示为草稿；刷新或 SSE 重连补齐当前草稿，完成后显示正式回复，停止或失败清除草稿。
- 真正的 DeepSeek model/tools 循环，Skills 按需读取；分镜模式将输出要求加入需求。
- 只读模式在后端过滤写工具；模型不能通过改参数绕开。
- 文字附件在发送时作为明确标记的参考材料加入需求，合计最多 20,000 字符。
- 查看产物、选择历史版本、导出 Markdown、继续修改。
- 产物显示正文非空白字符数及保存时上限；编排观测显示项目正文上限。旧版缺少记录时明确标注未记录校验。
- 停止当前执行，显式从检查点恢复；沿用原执行的模型/工具/时间预算。
- 视频任务卡片区分模拟与真实，显示 jobId、模型/规格、来源版本、结果与错误；live 另显示云端/下载状态、估价/实际费用与配置阻塞。完整媒体可用时支持播放、拖动进度和下载，Run 结束后 Job 仍持续更新。
- 下载失败或媒体缺失时可重试原下载，沿用原 Job/mediaId；历史结果保持原样。视频设置页默认只留视频 Key 与模式开关（首次保存 Key 且模式为 off 时自动启用 live）；供应商、模型、地域、业务空间与金额上限收进「高级（可选）」折叠，填写业务空间即切换到专属域名、留空用共享域名、清空并保存即移除；显示来源、参考估价与只读探测状态，保存后免重启对新登记生效。
- 等待态可停止 Agent，Job 继续跟踪；查询暂停时可恢复原任务查询。刷新保留已保存的等待与任务状态。
- 素材库（左栏「素材库」）：列出整个数据目录的参考图（含其他项目，便于复用）与本地预览、按当前项目过滤、上传导入（PNG/JPEG/WebP，≤20 MB，按项目与哈希去重）；文件缺失或损坏时可重新导入同一张图片按原记录恢复，M2 不提供删除。
- 分镜面板（左栏「分镜与镜头」，会话已有分镜时自动打开；也可在对话里点「分镜」）：按 order 的镜头卡（景别、时长、模式、参考图缩略图、shotId）、逐镜头版本历史与状态、播放与下载本地 MP4、逐镜头重做、按镜头分组的 Job 卡；工具条提供全选、刷新、批量生成与序列连播。
- 批量生成确认对话框：先请求只读估算，逐镜头列出模式/时长/估算与合计，并显示本 Run 已用与剩余额度；确认后整批登记，超出上限时服务端整批拒绝。
- 序列连播：客户端按顺序播放各镜头本地 MP4（不做服务端拼接），可暂停、下一个或停止；尚无本地媒体的镜头在面板上明确标注并被跳过。
- 用量面板（左栏「用量统计」）：按项目聚合任务计数与状态、登记估算与账单状态（未取得账单保持未知）、分镜与镜头版本数、素材数、模型 Token 覆盖率与按日记录；只读本地数据，不做外部调用。
- 编排观测：上下文字节与裁剪数、项目事实与计划、Skills 版本、MCP 状态、工具目录和事件、已知/未知 Token 用量。
- 新版 API 可提供当前轮工具参数和结果明细；旧运行实例缺少该字段时隐藏明细区，工具事件仍可见。

服务端 `state.json` 与 `checkpoints.sqlite` 是数据来源。浏览器只在 sessionStorage 保存选中的会话 ID；旧 `vagent.ui-preview.v1` 演示数据不迁移、不读取、不删除。

## API

实现 `/api/health`、`/api/session-token`、`GET/PATCH /api/config`、`POST /api/config/validate`、会话列表/创建/快照、消息提交、Run 状态/停止/恢复、产物列表/历史版本，以及 `/api/events?sessionId=...`。Job 入口为 `GET /api/jobs`（可选 `sessionId`）、`GET /api/jobs/:id`、`POST /api/jobs/:id/retry-query`。读取会核对本地媒体并可能保存可用性 revision，不触发供应商请求；恢复查询不重新提交。

PATCH 配置与完整设置表单支持 `videoMode`、`videoApiKey`、`clearVideoApiKey`、`videoWorkspaceId`、`videoProvider`、`videoModel`、`videoRegion`、`videoMaxJobCost`；省略保留原值。视频金额为十进制人民币字符串，模型和地域限定 C0 冻结值。读取返回 Key 是否配置、权限 `unverified`、参考估价 `videoEstimate`、各字段来源、待生效 `videoMode`、当前 `activeVideoMode`、`restartRequired/restartFields`。保存配置不产生生成请求。

live Job 视图包含 `lastProviderStatus`、`queryPauseReason`、冻结费用、下载阶段/次数、`runtimeBlock`、当前 `mediaAvailability` 与 `canRetryDownload`；不返回业务空间、私有供应商诊断或签名 URL。云端成功先返回 `downloading`、`result=null`、`mediaAvailable=false`，完整文件和索引提交后才 `succeeded`。历史 `result` 与当前可用性分开：文件丢失或损坏时保留结果、增加 revision 并移除播放器。

| 媒体接口 | 行为 |
|---|---|
| `GET /api/media/{mediaId}` | 本地元数据、大小、SHA-256、来源与当前可用性；可用时返回同源 `contentUrl` |
| `GET/HEAD /api/media/{mediaId}/content` | 完整 MP4；GET 支持单 Range 206、不可满足范围 416 与强 ETag 的 If-Range；HEAD 无正文 |
| `GET /api/media/{mediaId}/content?download=1` | 以服务端 mediaId 命名的附件下载；文件缺失或损坏返回 410 |
| `POST /api/jobs/{jobId}/retry-download` | CSRF + JSON `{clientRequestId, expectedRevision}`，202 登记恢复窗口；同 ID 同参数重放，旧 revision/参数冲突返回 409，非法字段返回 422 |

同一重试请求或正在执行的下载不会登记第二个执行；冲突文件保留，需备份移走后重试。媒体修复必须匹配原 SHA-256，过期链接不会通过新生成替换。CLI 的对应入口为 `vagent jobs retry-download JOB_ID`，随后由 `jobs work` 或 Web 推进。

分镜、素材与用量入口：

| 接口 | 行为 |
|---|---|
| `GET /api/storyboards?projectId=`、`GET /api/storyboards/{id}?projectId=` | 分镜摘要与完整视图（有序镜头、逐镜头版本、`status`、`mediaAvailable`、`mediaUrl`、`latest`）；跨项目 404 |
| `GET /api/storyboards/{id}/estimate?projectId=&storyboardVersion=&shotIds=` | 只读逐镜头估算、合计与新 Run 的额度快照，供确认对话框使用；不登记、不提交 |
| `POST /api/sessions/{id}/shots/generate` | CSRF + JSON `{clientRequestId, storyboardId, storyboardVersion, shotIds, confirm}`，202 登记；同一请求 ID 重放原结果，同一 ID 换镜头返回 409 |
| `POST /api/sessions/{id}/shots/{shotId}/redo` | CSRF + JSON `{clientRequestId, storyboardId, storyboardVersion, expectedRevision}`，202 重做；过期 revision 返回 409 `VERSION_CONFLICT` |
| `GET /api/assets?projectId=`、`POST /api/assets` | 素材元数据列表 / multipart 上传（CSRF、`projectId` + `file`，正文上限 20 MB + 64 KiB） |
| `GET /api/assets/{id}`、`GET/HEAD /api/assets/{id}/content` | 元数据与可用性 / 单 Range 图片内容（`?download=1` 附件）；不存在 404、文件缺失或损坏 410 |
| `GET /api/usage?projectId=` | 读取侧聚合（任务、估算与账单状态、分镜、素材、Token 覆盖率、按日）；不调用模型或供应商 |

CLI/Web 的显式重做与批量登记各自创建一条“客户端 Run”（`model="local-client"`，不更新会话的 latest Run，也不占用对话 Run 的额度），因此对话 Run 的恢复路径不受影响；同一 `clientRequestId` 重放原结果，不会重复登记。素材只经本地受信入口写入，读取接口不返回本地路径或私有 URL，工具 `asset_read` 同样只返回元数据。

发送消息使用 `clientRequestId`。响应丢失时前端复用该 ID，后端返回原 Run，避免重复付费执行。同一 ID 对应不同需求或只读模式会冲突。

SSE 首先发送 `snapshot`，包含持久状态和可选的内存 `draft`；正文通过 `assistant.delta` 推送，携带 Run ID、模型步、序号和文字。客户端拒绝旧步和重复序号，发现缺口时重新连接获取快照。每个订阅队列最多 64 项，慢客户端溢出时回退到当前快照；15 秒心跳，不承诺永久事件重放。

快照还包含当前项目的 `jobs` 和原 Run 的 `wait` 摘要。独立 `job.updated` 携带 Job 自己的 sessionId/runId 与有界视图；客户端按 jobId + revision 丢弃重复或旧版本，即使另一条 Run 已开始也不会把旧 Job 事件写入新 Run。`run.waiting`、`run.resumed`、`run.resume_blocked`、`run.completed` 在 SSE 交付时附带当前会话快照；未知事件退回快照，不混入文本增量。HTTP 恢复查询/下载响应晚于新 SSE 时也不能回退 Job 状态；更新快照或任务卡时复用同一视频节点，保留播放进度。

Token 草稿不写入状态文件或事件日志。模型响应通过完成标记、完整 JSON 和质量检查后，`model.completed` 触发正式消息快照；中断、取消或截断丢弃草稿。工具参数只在整步响应完成后执行，不执行半截 JSON。流式 usage 和缓存字段按完整调用统计，不逐块累加累计值；缺失保持未知。

## 本地访问边界

仅监听 127.0.0.1，Host 限定 localhost/127.0.0.1 与指定端口；Origin 必须精确同源，拒绝 cross-site/same-site Fetch Metadata。写请求需要 CSRF Token；JSON 请求体最多 128 KiB，素材上传是 multipart 且正文最多 20 MB + 64 KiB。静态文件仅提供 `index.html`、`app.js`、`job-state.js`、`storyboard-state.js`、`styles.css`、`mark.svg`，不提供 `.env` 或数据目录。Job 提示词、结果和来源标题均作为转义后的文本显示。

DeepSeek 与视频 Key 分开配置，来自数据目录 `config.yml`、服务端环境或本地 `.env`，读取接口只返回是否已配置。密码框提交后清空，关闭设置时也清空，不写入 localStorage/sessionStorage。环境和启动配置优先且不可被页面覆盖；文本与视频配置保存后立即生效、无需重启——视频配置用于新登记任务，在途与历史任务按各自冻结配置恢复。运行或验证期间拒绝配置修改，验证期间拒绝新执行。受信任的本机进程仍可调用接口；本服务不是多用户鉴权系统，不支持局域网公开部署。

验证使用固定 DeepSeek 端点、最多 8 个输出 Token、15 秒超时、不重试；错误内容不回显供应商原文。验证状态在服务重启后重置，保存相同配置不清除已验证状态。环境配置变化后需重启服务。

支持 `VAGENT_VIDEO_MODE=off|mock|live`，默认 off。启动模式控制新 Run 的工具，旧 off/mock Run 使用原工具/规则及签名，旧 Job 按原供应商恢复；live 缺少原 Key/业务空间时保存配置阻塞并停止上游任务 HTTP 尝试。已保存的媒体地址由独立无鉴权客户端下载。Mock 不需要视频 Key，也不提供占位 MP4。关闭浏览器不停止 Worker，退出服务才停止本地推进并保存等待意图。`await_job` 在 `downloading` 时继续等待，本地提交后才交付；后续修复不复活已结束 Run。C2 已通过受控视频与浏览器验证，真实账号、实际媒体和账单仍待 C3，见 [C2 验收](../docs/M1C_C2_ACCEPTANCE.md)；原 Mock 证据见 [B4 验收](../docs/M1B_B4_ACCEPTANCE.md)。

质量规则见 [质量校验验收](../docs/QUALITY_ACCEPTANCE.md)。例如发送“brief 正文300字以内”会设置持久上限；只有用户明确修改/取消才能放宽。后端超限或记忆冲突会返回工具错误，模型未纠正就结束时 Run 失败。源码更新后需正常重启已有服务再刷新页面，以加载新的校验逻辑。
