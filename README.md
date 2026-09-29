# vagent

面向视频创作的 Agent 实习项目：使用 **DeepSeek + LangGraph**，自主设计上下文、项目记忆、skills、工具执行与护栏，再接入视频生成模型。

当前是可运行的 **CLI Agent 原型**。真实 DeepSeek 调用接口已实现，但尚未配置 Key 做真实服务联调；现有验证使用确定性模拟模型和模拟 HTTP 响应。没有接入视频 API，也没有生成真实视频。

## 当前进度

| 部分 | 状态 | 已交付内容 |
|---|---|---|
| 01 Agent 核心 | 已实现，本地测试通过 | LangGraph model/tools 循环、DeepSeek 适配、CLI、项目与产物工具、版本化持久存储、执行限额 |
| 02 上下文与 Skills | 待实现 | 上下文预算、完整工具消息裁剪、按需加载 SKILL.md、领域约束注入 |
| 03 持久图恢复与评测 | 待实现 | LangGraph 持久检查点、显式继续、固定任务集与策略对比 |
| 04 Web 与视频工具 | 待实现 | Web、模拟视频长任务、真实供应商接入 |

每个独立完成的代码部分都会同步更新本 README、提交并推送 GitHub。详见 [M1 计划](./M1_PLAN.md) 和 [Harness 设计](./AGENT_HARNESS_DESIGN.md)。

## 快速开始

要求 Node.js **24 或更新版本**、npm。

```powershell
npm ci
npm run check

# 可选：把演示数据放在当前项目的被忽略目录，避免混入正式项目数据。
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
npm run dev -- demo
npm run dev -- inspect --session demo
```

`demo` 无需 Key、不会联网，也不会消耗模型费用。它使用固定测试逻辑调用真实项目工具，并在本地保存一个带“模拟”标记的创作方案；不代表 DeepSeek 的生成效果。

## 连接真实 DeepSeek

复制 `.env.example` 为 `.env`，填入自己的 Key：

```dotenv
DEEPSEEK_API_KEY=你的真实Key
VAGENT_DEEPSEEK_MODEL=deepseek-flash
```

`.env` 已被 Git 忽略。也可以直接设置环境变量；`VAGENT_DEEPSEEK_KEY` 的优先级高于 `DEEPSEEK_API_KEY`。现有环境变量不会被 `.env` 覆盖。

```powershell
npm run dev -- config show
npm run dev -- run '准备咖啡店短视频方案，面向上班族，暖色调，并保存。' --session coffee
npm run dev -- run '改成雨夜氛围，保留受众设定和原版。' --session coffee
npm run dev -- inspect --session coffee

# 或进入持续对话，/exit 退出；Ctrl+C 停止当前进程的 Agent。
npm run dev -- chat --session coffee
```

`run` 和 `chat` 会向 DeepSeek 发起真实请求并产生费用；默认模型是可配置的候选型号，需要账户具备对应权限。首版显式关闭 thinking。仅凭本地模拟测试不能保证账户权限和线上模型兼容性。

`run` 可提供 `--request-id`：同一会话重复提交相同请求 ID 时返回已有记录，不重跑模型或工具；相同 ID 对应不同需求会报错。失败请求也不自动重跑，修改任务后应使用新的请求 ID。

## 已实现的机制

```text
CLI → AgentRunner（自定义 LangGraph 图）
        ├─ model 节点 → ChatDeepSeek
        └─ tools 节点 → 注册表、Schema 校验、操作记录
                           └─ 当前项目与版本化文本产物
```

- **模型与执行分离**：DeepSeek 提出调用，服务端校验执行，并把真实结果返回模型继续决策。
- **5 个创作工具**：`project_read`、`project_update`、`plan_update`、`artifact_save`、`artifact_read`。未开放 shell、任意文件路径或视频生成权限。
- **结构化项目记忆**：保存目标、受众、风格、约束和计划；产物使用稳定 ID，修改保留旧版本。
- **并发与写入保护**：单写进程锁、串行事务、同目录原子替换；本地修改和工具成功结果一起提交。
- **去重边界**：相同执行记录不会再次写入；不承诺不同请求 ID 或不同工具调用 ID 之间的语义去重。
- **运行限额**：最多 8 个模型步、12 次工具调用、180 秒运行时间；取消后不启动新的工具。
- **错误处理**：工具名与参数校验失败可反馈给模型；原始供应商错误不会直接打印，以防回显请求凭证。

LangGraph 提供图执行能力；状态定义、路由、工具权限、版本控制、执行记录和产品行为由项目实现。没有采用 Deep Agents 成品 harness，也没有把通用 Agent 循环交给另一个 SDK 自动运行。

## 数据与当前限制

默认数据目录为 `~/.vagent/`，可用 `VAGENT_HOME` 覆盖。`state.json` 保存项目、完整成功会话、产物正文、Run 记录和工具结果；Key 不写入这个文件。`inspect` 会打印所选项目的内容，请勿把包含私密创作资料的输出公开上传。

- 重启后可继续**已完成会话**，并读取已保存的产物；尚未实现 LangGraph 图级自动续跑。
- 中断 Run 会被标记为 `interrupted`。工具已经提交的本地写入会保留；不会自动重新执行中断任务。
- 异常断电可能留下 `instance.lock`。确认没有 vagent 进程使用该目录后，才手动删除该锁；程序不会擅自抢占。
- 当前模型回复整段显示，工具事件实时输出；逐 Token 流、Web、自动摘要、跨项目偏好记忆、skills、视频任务仍在计划中。
- JSON 适合当前单用户小规模原型，尚未实现历史清理、状态备份和大规模并发。

## 验证与打包

```powershell
npm run typecheck
npm test
npm run build
node dist/cli.js --help
npm pack
```

第一部分已有 **16 项自动化测试**：真实工具结果驱动后续模型步、工具失败、执行预算、取消、请求去重、版本冲突、跨项目访问、持久化重开、写操作回滚、DeepSeek HTTP 协议映射等。

当前验证环境：Windows、Node.js 24。真实 API 联调和 Unix 安装验证尚未完成。npm 包标记为 private，`npm pack` 仅生成本地安装包，不发布公共 npm。

## 后续顺序

1. 上下文预算、按需读取与 Skills 加载。
2. 持久图检查点、显式恢复及独立评测任务集。
3. 最小 Web。
4. 模拟视频 Job，再接真实视频模型。
