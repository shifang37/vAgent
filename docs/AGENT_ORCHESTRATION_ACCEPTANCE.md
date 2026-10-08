# 真实 Agent 编排验收

验收日期：2026-10-08。结论：真实编排、工具闭环与持久化检查通过；同时发现两项内容质量问题，不能把 Run 的 completed 当成全部需求合格。

## 实际接入

前端 → 同源 FastAPI / SSE → ApplicationService → 原有 LangGraph AgentRunner → DeepSeek / 内置工具 / MCP stdio。CLI 复用同一应用服务与工具配置。未接入视频生成供应商。

本机服务：http://127.0.0.1:3210 。数据在 `.vagent/web/`；不是浏览器演示数据。真实验收会话为 `413dd4c2-0948-49da-a001-0c325f3a96ab`。输入均是本次构造的虚构咖啡店项目，Key 只由服务端读取。

## 真实模型结果

模型：`deepseek-flash`；thinking 关闭。没有自动付费重试，全部请求成功完成。

| 用例 | 模型调用 | 工具调用 | 输入 Token | 输出 Token | 结果 |
|---|---:|---:|---:|---:|---|
| 创建方案、持久记忆、读取 video-brief、执行计划 | 5 | 6 | 15,673 | 1,161 | brief v1 与项目事实实际落盘 |
| 延续记忆、读取 shot-description、修改版本、MCP 计算 | 5 | 8 | 30,256 | 1,757 | 同一 brief 更新 v2，新增 storyboard v1 |
| 只读核查记忆和 brief 历史版本 | 2 | 3 | 15,899 | 566 | 受众/约束保留；项目与产物未修改 |
| 浏览器直接发送：不用工具复述当前记忆 | 1 | 0 | 9,817 | 14 | 回复“受众是城市上班族，当前风格为雨夜青蓝色。” |
| 合计 | **13** | **17** | **71,645** | **3,498** | 工具链与真实 UI 请求均完成 |

供应商报告缓存命中 42,496 Token，加权命中率约 59.3%；全部 13 次请求均取得 Token 和缓存明细。这里不是 Redis 命中率，不含金额估算，也不能推断生产节费比例。本次未配置 Redis。

8 种工具都实际成功调用：`project_read`、`project_update`、`plan_update`、`artifact_read`、`artifact_save`、`skill_read`、`mcp_video_shot_timing`、`mcp_video_frame_budget`。两份 Skill 正文均由真实模型按工具调用读取。

MCP 工具由独立 Python 进程提供，通过官方 SDK 协商协议 `2025-11-25`。镜头 5 / 7 / 11 / 7 秒合计 30 秒；30 秒 × 24fps = 720 帧，9:16 建议画幅 1080×1920。工具返回值被模型用于保存分镜。

真实产物：

- brief：`2b868988-742e-4f62-bded-e2a3d58b3b9a`，v1 暖色、v2 雨夜青蓝；相同 ID，旧版仍可读取。
- storyboard：`57b0bd67-09f7-42ee-a313-c326ff50ba38`，v1 包含 MCP 校验结果。

首三轮最后模型输入分别为 18,126 / 35,968 / 42,395 UTF-8 序列化字节，均未达到 65,536 字节预算，因此真实样例没有触发裁剪。长历史裁剪与保护完整工具协议的行为由下述自动化测试覆盖，不声称真实样例已验证长对话质量。

## 发现的问题

1. **记忆字段存在冗余冲突**：`project.style` 已更新为“雨夜青蓝色”，但 `project.goal` 仍有“暖色自然光”。模型的只读核查也指出了它。持久化本身正常，跨字段语义一致性没有自动保障。后续应收紧字段职责，并在需求更新后检查相关事实是否互相矛盾。
2. **字数约束没有被严格遵守**：模型称 brief v2 在 300 字内，但实际包含 309 个汉字、468 个总字符。brief v1 为 293 个汉字/447 字符，storyboard 为 194 个汉字/533 字符。验收不能使用模型自报字数；需定义统计口径并在保存/交付前校验。

原始报告的 `checks` 是编排机制检查，`qualityFindings` 单独记录上述问题，整体标为 `passed-with-quality-findings`。没有修改真实产物来掩盖问题。

## 自动化与打包验证

完整 pytest：**120 passed，1 skipped**。跳过项是当前 Windows 账户缺少符号链接创建权限。已有 109 项保持通过，新增 11 项覆盖 Web 与 MCP 集成。最后一次新增工具明细断言后的 Web/MCP 定向复测为 11 passed。

| 范围 | 验证内容 |
|---|---|
| 上下文 | 稳定前缀、确定性排序、完整轮次裁剪、JSON 无损压缩、当前轮保护、超限前拒绝请求 |
| 记忆/产物 | 跨轮与重开保留、项目隔离、revision/version 冲突、旧版保留、分页读取、事务回滚 |
| Skills | 元信息与正文分离、按需读取、内容版本、无效配置与路径限制 |
| 编排保护 | 重复 requestId、并发拒绝、非法工具/参数、步数/工具/时间限额、错误脱敏 |
| 恢复 | 停止后继续、SQLite 检查点、累计预算、部分工具提交重放、真实子进程强退场景 |
| 用量/缓存 | 逐调用统计、加权缓存命中率、未知用量、Redis 故障回退/TTL 等原有用例 |
| MCP | 真 stdio 发现/调用、Schema 校验、只读白名单、操作去重、错误/超大结果、取消和总超时 |
| Web | 同源/Host/CSRF、体积限制、敏感静态路径拒绝、无 Key 明确失败、版本读取、运行与事件持久化 |

Ruff 与 JS 语法检查通过；sdist/wheel 构建通过。独立虚拟环境安装 wheel 后，在仓库外通过真实应用生命周期和 ASGI HTTP 验证页面资源、API、Runner、产物保存、工具明细以及 MCP 子进程发现，退出后锁正常释放；`pip check` 通过。该冒烟验证不监听额外端口，不调用付费模型。

浏览器已验证真实请求→完成、SSE 快照更新、刷新恢复会话、记忆观测、brief v1/v2 切换。修复了窄屏固定输入区遮住产物入口的问题，改为独立滚动的对话区。

当前 3210 进程运行主接入版本，后补的原始工具参数/结果明细字段需下次正常重启后加载；原有工具事件、上下文、记忆和用量已在当前页面验证。自动审批拒绝了本次额外的服务重启和另启服务验证操作，仅返回 `blocked by policy`，未提供具体原因；原服务保持运行。最终源代码的生命周期与工具明细已通过独立 wheel/ASGI 验证。

## 复现

```powershell
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m vagent web --mcp-local --no-open
# 在另一个终端执行；产生真实模型费用：
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --live
# 只检查已有结果，不联网调用模型：
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --review-existing
.\.venv\Scripts\python.exe -m pytest -q
```

原始运行报告位于被 Git 忽略的 `output/agent-live-acceptance.json`。更改代码或配置后，先正常停止占用同一数据目录的服务再重启；不要在仍运行时删除锁。

本次不包含：远程/写入型 MCP、MCP resources/prompts/sampling、真实视频 Job、自动摘要/向量记忆、跨项目偏好记忆、生产并发负载或大样本模型成功率评测。
