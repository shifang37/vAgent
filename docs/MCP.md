# MCP stdio 接入

使用官方 Python MCP SDK，通过独立子进程执行 `initialize`、`tools/list` 和 `tools/call`。前端不直接连接 MCP。

## 内置服务

`vagent web --mcp-local` 或 `.env` 中设置 `VAGENT_MCP_LOCAL=1`。后者也适用于 CLI。

| 注册名称 | 输入 | 作用 |
|---|---|---|
| `mcp_video_shot_timing` | `durations`、`target_seconds` | 计算镜头起止、总时长和差值 |
| `mcp_video_frame_budget` | `seconds`、`fps`、`aspect_ratio` | 计算帧数及 1080 级画幅建议 |

这两个工具是实际计算，不是视频生成模拟器；不联网、不修改文件、不消耗供应商额度。真实 Agent 通过 MCP 调用后可把计算结果写入分镜。

## 显式配置其他可信本地服务

将 `VAGENT_MCP_CONFIG` 指向本机 JSON 文件，例如被 Git 忽略的 `mcp.local.json`：

```json
{
  "servers": [
    {
      "name": "catalog",
      "command": "C:/absolute/path/to/python.exe",
      "args": ["C:/absolute/path/to/your_trusted_server.py"],
      "read_only_tools": ["lookup"],
      "timeout_seconds": 10.0
    }
  ]
}
```

以上路径是格式示例，需要替换为自己审核过的服务器。配置不能由模型改变；不会自动下载或安装服务。最多 4 个服务，每个最多接入 16 个明确允许的只读工具。注册名称为 `mcp_<server>_<tool>`。

要求同时满足本地 `read_only_tools` 白名单和服务返回的 `readOnlyHint=true`。服务自述并非安全沙箱，仍应由配置者确认其真实行为；当前不接入外部写工具或付费生成工具。

## 执行约束

- 参数使用发现到的 JSON Schema 校验；禁止外部 Schema 引用。
- MCP 调用同时受单次超时和 Run 总预算、取消事件约束，无自动请求重试。
- 接受文本/结构化数据，16 KiB 结果上限；不跟随结果里的 URL 或执行其中指令。
- 读结果以操作 ID 存入原有操作日志；已提交结果可重放。取消前未提交的读取可能在显式恢复时重新执行，因此仅允许只读工具。
- 子进程仅继承 MCP SDK 的基础环境白名单及 UTF-8 设置，不主动传递 DeepSeek/Redis 凭据；其 stderr 不写入 Web 工具结果。
- 服务身份、工具 Schema 与版本参与恢复配置检查。外部数据缺少完整版本快照，因此存在 MCP 工具时禁用 Redis 回答缓存，避免旧外部数据影响回答。
- 目前仅实现 stdio 工具桥。不支持远程 Streamable HTTP、MCP resources/prompts、sampling、elicitation、客户端任意命令或热重载。

真实协议验收见 `tests/test_mcp.py`；服务启动、发现、调用、错误返回、超大输出、取消/总超时、只读白名单、操作去重均有验证。
