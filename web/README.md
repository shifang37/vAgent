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
- 设置页保存本地 Key/模型，显示配置来源；独立的验证按钮只发起一次短模型请求，保存本身不产生模型费用。
- 回复逐段显示为草稿；刷新或 SSE 重连补齐当前草稿，完成后显示正式回复，停止或失败清除草稿。
- 真正的 DeepSeek model/tools 循环，Skills 按需读取；分镜模式将输出要求加入需求。
- 只读模式在后端过滤写工具；模型不能通过改参数绕开。
- 文字附件在发送时作为明确标记的参考材料加入需求，合计最多 20,000 字符。
- 查看产物、选择历史版本、导出 Markdown、继续修改。
- 产物显示正文非空白字符数及保存时上限；编排观测显示项目正文上限。旧版缺少记录时明确标注未记录校验。
- 停止当前执行，显式从检查点恢复；沿用原执行的模型/工具/时间预算。
- 编排观测：上下文字节与裁剪数、项目事实与计划、Skills 版本、MCP 状态、工具目录和事件、已知/未知 Token 用量。
- 新版 API 可提供当前轮工具参数和结果明细；旧运行实例缺少该字段时隐藏明细区，工具事件仍可见。

服务端 `state.json` 与 `checkpoints.sqlite` 是数据来源。浏览器只在 sessionStorage 保存选中的会话 ID；旧 `vagent.ui-preview.v1` 演示数据不迁移、不读取、不删除。

## API

实现 `/api/health`、`/api/session-token`、`GET/PATCH /api/config`、`POST /api/config/validate`、会话列表/创建/快照、消息提交、Run 状态/停止/恢复、产物列表/历史版本，以及 `/api/events?sessionId=...`。

发送消息使用 `clientRequestId`。响应丢失时前端复用该 ID，后端返回原 Run，避免重复付费执行。同一 ID 对应不同需求或只读模式会冲突。

SSE 首先发送 `snapshot`，包含持久状态和可选的内存 `draft`；正文通过 `assistant.delta` 推送，携带 Run ID、模型步、序号和文字。客户端拒绝旧步和重复序号，发现缺口时重新连接获取快照。每个订阅队列最多 64 项，慢客户端溢出时回退到当前快照；15 秒心跳，不承诺永久事件重放。

Token 草稿不写入状态文件或事件日志。模型响应通过完成标记、完整 JSON 和质量检查后，`model.completed` 触发正式消息快照；中断、取消或截断丢弃草稿。工具参数只在整步响应完成后执行，不执行半截 JSON。流式 usage 和缓存字段按完整调用统计，不逐块累加累计值；缺失保持未知。

## 本地访问边界

仅监听 127.0.0.1，Host 限定 localhost/127.0.0.1 与指定端口；Origin 必须精确同源，拒绝 cross-site/same-site Fetch Metadata。写请求需要 CSRF Token 和 JSON，请求体最多 128 KiB。静态文件只提供 4 个允许的资源，不提供 `.env` 或数据目录。

API Key 来自数据目录 `config.yml`、服务端环境或本地 `.env`，读取接口只返回是否已配置。密码框提交后清空，关闭设置时也清空，不写入 localStorage/sessionStorage。环境和启动配置优先且在页面锁定；本地保存立即对下一次运行生效。运行或验证期间拒绝配置修改，验证期间拒绝新执行。受信任的本机进程仍可调用接口；本服务不是多用户鉴权系统，不支持局域网公开部署。

验证使用固定 DeepSeek 端点、最多 8 个输出 Token、15 秒超时、不重试；错误内容不回显供应商原文。验证状态在服务重启后重置，保存相同配置不清除已验证状态。环境配置变化后需重启服务。

B2 支持启动配置 `VAGENT_VIDEO_MODE=off|mock`，默认 off。设置页显示模式与来源，仅供查看；PATCH 配置仍只接受 Key/模型。mock 注册四个模拟视频工具，但当前 Web 不自动启动 Worker，也不挂起等待未完成 Job；待完成 `await_job` 以 `EXTERNAL_WAIT_UNAVAILABLE` 结束 Run。生产等待、Job API/SSE 和任务卡片分别由 B3/B4 接入。没有真实媒体或播放/下载入口，详见 [B2 验收](../docs/M1B_B2_ACCEPTANCE.md)。

质量规则见 [质量校验验收](../docs/QUALITY_ACCEPTANCE.md)。例如发送“brief 正文300字以内”会设置持久上限；只有用户明确修改/取消才能放宽。后端超限或记忆冲突会返回工具错误，模型未纠正就结束时 Run 失败。源码更新后需正常重启已有服务再刷新页面，以加载新的校验逻辑。
