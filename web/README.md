# vagent Web 工作台

DeepSeek Harness 风格的本地创作工作台，已连接真实 Agent。

## 启动

在仓库根目录安装 `requirements-dev.lock`，在 `.env` 中配置 DeepSeek Key：

```powershell
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m vagent web --mcp-local --no-open
```

访问 http://127.0.0.1:3210 。可使用 `--port` 换端口。静态 `http.server` 已不能提供 Agent API；前端不会静默回退成模板。

## 交互与数据

- 创建、切换、搜索服务端会话；刷新恢复选中的会话。
- 真正的 DeepSeek model/tools 循环，Skills 按需读取；分镜模式将输出要求加入需求。
- 只读模式在后端过滤写工具；模型不能通过改参数绕开。
- 文字附件在发送时作为明确标记的参考材料加入需求，合计最多 20,000 字符。
- 查看产物、选择历史版本、导出 Markdown、继续修改。
- 停止当前执行，显式从检查点恢复；沿用原执行的模型/工具/时间预算。
- 编排观测：上下文字节与裁剪数、项目事实与计划、Skills 版本、MCP 状态、工具目录和事件、已知/未知 Token 用量。
- 新版 API 可提供当前轮工具参数和结果明细；旧运行实例缺少该字段时隐藏明细区，工具事件仍可见。

服务端 `state.json` 与 `checkpoints.sqlite` 是数据来源。浏览器只在 sessionStorage 保存选中的会话 ID；旧 `vagent.ui-preview.v1` 演示数据不迁移、不读取、不删除。

## API

实现 `/api/health`、`/api/session-token`、会话列表/创建/快照、消息提交、Run 状态/停止/恢复、产物列表/历史版本，以及 `/api/events?sessionId=...`。

发送消息使用 `clientRequestId`。响应丢失时前端复用该 ID，后端返回原 Run，避免重复付费执行。同一 ID 对应不同需求或只读模式会冲突。

SSE 首先发送快照；每次相关状态变化重新发送持久快照，15 秒心跳。断线重连恢复最新快照，不承诺永久事件流重放；事件编号保存在各 Run 内。当前模型回复仍整段返回，工具步骤和状态实时更新。

## 本地访问边界

仅监听 127.0.0.1，Host 限定 localhost/127.0.0.1 与指定端口；Origin 必须精确同源，拒绝 cross-site/same-site Fetch Metadata。写请求需要 CSRF Token 和 JSON，请求体最多 128 KiB。静态文件只提供 4 个允许的资源，不提供 `.env` 或数据目录。

API Key 仅来自服务端环境或本地 `.env`，不返回前端。受信任的本机进程仍可调用本地接口；本服务不是多用户鉴权系统，不支持局域网公开部署。CLI 与 Web 同一目录互斥，服务退出前等待当前 Run 取消完成。

当前没有在线 Key 设置、模型切换、配置向导、逐 Token 流或视频生成。环境配置变化后重新启动服务。
