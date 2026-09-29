# vagent

面向视频创作的 Agent 实习项目，使用 **Python + DeepSeek + LangGraph**，自主设计上下文、项目记忆、Skills、工具执行与护栏，再接入视频生成模型。

当前已实现可运行的 **Python CLI Agent 原型**。DeepSeek 适配接口已完成，尚未配置真实 Key 联调；现有验证使用离线模拟模型和模拟 HTTP。当前交付文本创作材料，还未接入视频生成 API。

## 当前进度

| 部分 | 状态 | 已交付内容 |
|---|---|---|
| 01 Agent 核心 | 已实现 | 自定义 LangGraph model/tools 循环、DeepSeek 适配、CLI、项目与产物工具、版本化存储、执行限额 |
| 02 上下文与 Skills | 已实现 | 输入预算、完整轮次裁剪、项目事实注入、按需读取 SKILL.md、内容版本记录 |
| Python 迁移 | 已实现并通过本地验证 | Python 源码与 pytest 测试、pip 安装、wheel 打包、Python CI；替换原 TS/Node 工程 |
| 03 持久图恢复与评测 | 待实现 | LangGraph 持久检查点、显式继续、固定任务集与策略对比 |
| 04 Web 与视频工具 | 待实现 | 最小本地 Web、模拟视频 Job、真实供应商接入 |

每个独立完成的代码部分都同步更新本 README、提交并推送 GitHub。阶段目标见 [M1 计划](./M1_PLAN.md) 和 [Harness 设计](./AGENT_HARNESS_DESIGN.md)。

## 快速开始（Windows PowerShell）

要求 **Python 3.11 或更新版本**，本地验证使用 Python 3.12。当前 Agent 不需要 Node.js 或 npm。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .

# 将演示数据放在当前项目的被忽略目录中。
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\python.exe -m vagent demo
.\.venv\Scripts\python.exe -m vagent inspect --session demo
.\.venv\Scripts\python.exe -m vagent skills list
```

这些命令无需激活虚拟环境，也无需修改 PowerShell 执行策略。若 `python` 打开 Microsoft Store，请安装 Python 3.11+，或用已有解释器的完整路径执行第一条命令。

macOS/Linux 使用 `python3 -m venv .venv`，再将 `.\.venv\Scripts\python.exe` 换为 `.venv/bin/python`。也可激活虚拟环境后直接使用 `vagent` 命令。只安装运行依赖可用 `python -m pip install .`；开发与 CI 使用 `requirements-dev.lock` 固定依赖版本。

`demo` 无需 Key、不会联网，也不消耗模型费用。它按固定测试逻辑读取 skill、读取项目并保存带“模拟”标记的方案，不能用于衡量 DeepSeek 的生成效果。

## 连接真实 DeepSeek

复制 `.env.example` 为 `.env`，在本机填入自己的 Key：

```dotenv
DEEPSEEK_API_KEY=你的真实Key
VAGENT_DEEPSEEK_MODEL=deepseek-flash
```

`.env` 已被 Git 忽略。`VAGENT_DEEPSEEK_KEY` 优先于 `DEEPSEEK_API_KEY`，已有环境变量不会被 `.env` 覆盖。

```powershell
.\.venv\Scripts\python.exe -m vagent config show
.\.venv\Scripts\python.exe -m vagent run '准备咖啡店短视频方案，面向上班族，暖色调，并保存。' --session coffee
.\.venv\Scripts\python.exe -m vagent run '改成雨夜氛围，保留受众设定和原版。' --session coffee
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
.\.venv\Scripts\python.exe -m vagent chat --session coffee
```

`chat` 中输入 `/exit` 退出，Ctrl+C 停止当前进程的 Agent。`run` 和 `chat` 会发起真实请求并产生 API 费用。候选模型 `deepseek-flash` 可配置，账户权限与线上兼容性仍需真实 Key 验证；首版关闭 thinking。

`run` 支持 `--request-id`：同一会话相同 ID、相同需求返回已有运行记录；相同 ID 对应不同需求会报错。失败与中断任务也不自动重跑，新的尝试应使用新 ID。

## 实现与代码入口

```text
src/vagent/
  cli.py       argparse 命令行与配置展示
  config.py    环境变量与 Key 校验
  models.py    DeepSeek 单步适配、离线模拟模型
  runner.py    自定义 LangGraph 图、预算、取消与事件
  context.py   上下文组装与完整轮次裁剪
  tools.py     工具注册、Pydantic 校验、项目与产物操作
  storage.py   单写锁、事务、原子替换、操作日志
  skills.py    技能发现、元信息与按需正文读取
skills/        内置 SKILL.md，随 wheel 分发
tests/         pytest 行为与协议测试
```

LangGraph 提供图执行底座，项目自己定义状态、路由、上下文策略、工具边界、版本控制和运行记录。`ChatDeepSeek.bind_tools(...).ainvoke(...)` 只执行单个模型步，模型不会直接执行工具。

- **工具白名单**：`project_read`、`project_update`、`plan_update`、`artifact_save`、`artifact_read`、`skill_read`。不开放 shell 或任意文件路径。
- **Pydantic Schema**：工具声明与后端输入校验共用定义，拒绝未知字段和错误参数类型；工具错误会返回模型。
- **项目记忆**：保存目标、受众、风格、约束和计划；产物以稳定 ID 保存，每次修改增加版本并保留原文。
- **写入保护**：单写进程锁、串行事务、同目录原子替换；业务修改和操作结果一起提交。相同操作 ID 复用结果，参数冲突则拒绝。
- **运行限制**：默认最多 8 个模型步、12 次工具调用、180 秒；超时取消异步模型请求，停止后不启动新工具。已完成的本地写入保留。
- **日志边界**：不直接输出供应商异常或参数校验中的原始输入值。配置展示只返回 Key 是否存在。

去重仅针对同一请求/操作 ID，不承诺不同 ID 之间的语义去重。JSON 存储面向单用户小规模使用，同步本地文件写入不是可抢占的异步任务。

## 上下文与 Skills

每个模型步重新注入系统规则、最新项目事实、skill 元信息和近期对话。`VAGENT_CONTEXT_BYTES` 默认 65536，按消息和工具定义的 UTF-8 序列化字节数衡量，**不是精确 Token 数或供应商请求体大小**。供应商返回的 Token usage 另行记入 Run。

超限时裁剪最旧的完整用户轮次，保留工具调用与结果配对；原始历史不删除。当前需求、当前轮工具结果和项目事实必须完整保留，仍超限则在调用模型前报 `CONTEXT_LIMIT`。长产物通过 `artifact_read` 的 offset/limit 分段读取。

内置 [video-brief](./skills/video-brief/SKILL.md) 和 [shot-description](./skills/shot-description/SKILL.md)。启动时校验并保留内容快照，初始上下文仅包含名称、描述和内容版本；`skill_read` 后才加入正文。

自定义技能目录结构为 `skills/your-skill/SKILL.md`：

```markdown
---
name: your-skill
description: 说明哪些任务需要这个技能。
---

使用现有工具完成任务的操作指引。
```

名称必须与目录一致，最多加载 32 个技能，每个文件最多 16 KiB。`VAGENT_SKILLS_DIR` 可指定可信本地目录，替代内置目录；修改后重启加载。不支持自动执行脚本、联网安装或热更新。Skill 不能增加工具权限或跳过代码校验。

## 数据兼容与限制

默认数据目录为 `~/.vagent/`，可用 `VAGENT_HOME` 覆盖。`state.json` 保存成功会话、项目、产物、Run 与操作结果；配置 Key 不写入状态。`inspect` 会显示创作正文。

Python 版保留 schema v1 的领域数据结构和工具 JSON 字段（如 `artifactId`、`expectedVersion`），并验证了读取原 TS 消息格式的兼容行为。已有项目可以继续使用原数据目录；无效或不支持的状态文件会报错并保留原文件。

- 成功会话可跨重启继续；未完成 Run 重启后标记为 `interrupted`，已提交产物保留，不自动重放。
- 当前保存的是应用运行快照，尚未接入 LangGraph 持久检查点和图级续跑。
- 异常断电可能留下 `instance.lock`；确认没有进程使用该数据目录后才手动移除，程序不会自动抢锁。
- 回复整段显示，工具事件实时输出；逐 Token 流、自动摘要、跨项目偏好记忆、Web 和视频任务尚未实现。

后续视频服务通过注册工具接入独立 Job 服务和供应商适配器。Agent Run 与视频 Job 分开记录；长任务状态、付费提交与恢复策略独立实现，结果不确定的付费提交不能盲目重试。当前只需要 DeepSeek Key，视频服务 Key 在真实视频阶段单独配置。

## 验证与打包

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/python
```

本地 Python 3.12 测试结果：**44 passed，1 skipped**。跳过的是当前 Windows 账户无符号链接创建权限的测试。测试覆盖工具闭环、请求去重、错误与超时、取消、并发拒绝、版本/项目隔离、写入回滚、旧状态读取、上下文预算、Skills 和 DeepSeek HTTP 协议。

已验证离线 CLI，以及 wheel 安装到独立虚拟环境后在仓库目录之外运行 `skills list`、`demo`、`inspect`。源码包与 wheel 仅本地构建，未发布 PyPI。GitHub Actions 配置 Ubuntu/Windows、Python 3.11/3.12 检查，远端结果见 [Actions](https://github.com/shifang37/vAgent/actions)。

这些测试验证工程行为，不代表真实模型任务成功率。后续按顺序完成真实 DeepSeek 联调、持久图恢复与任务评测、最小 Web、模拟视频 Job、真实视频模型接入。
