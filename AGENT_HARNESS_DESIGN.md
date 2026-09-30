# vagent Agent 选型与 Harness 设计

> 日期：2026-09-30
> 状态：已按求职项目目标选择 LangGraph 自主设计 Harness；第一部分实现与验证状态见 [README](./README.md)。本文其余能力为逐步实施的设计目标。  
> 产品定位：以 DeepSeek 为决策模型的视频创作 Agent。先建立可独立运行的 Agent，再为它接入视频生成能力。

当前原型已交付 Agent 循环、项目记忆、版本化产物、基础执行护栏、上下文裁剪、Skills，以及 SQLite 持久图检查点和显式恢复。上下文预算暂以 UTF-8 序列化字节数衡量；真实模型评测与视频 Job 仍属后续目标，不能把下文全部视为已实现。恢复行为及限制见 [持久恢复验收](./docs/RECOVERY_ACCEPTANCE.md)。

已补齐 [模型与缓存用量观测](./docs/USAGE_ACCEPTANCE.md) 和 [上下文 v2 优化](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)：固定规则、Skill 元信息位于动态项目事实之前，工具定义保持确定性顺序，JSON 去除冗余空白，并保留旧检查点的上下文格式。真实缓存命中收益待 API 联调验证。

## 1. 设计目标

只配置 DeepSeek API Key，就能让 Agent 理解创作需求，读取项目状态，自主选择工具，保存创作方案，根据真实工具结果继续行动，并在用户反馈后修改已有产物。

后续接入视频供应商时，应增加视频工具、适配器和后台任务实现，保持 Agent 循环、会话模型、工具执行规范和客户端协议稳定。

术语约定：

| 概念 | 在本项目中的职责 |
|---|---|
| 模型 | DeepSeek 根据输入和工具结果生成回复、提出工具调用 |
| Agent | 模型在目标驱动下多次选择行动、观察结果、调整行动的整体行为 |
| Harness | 包围模型的运行环境：上下文组装、工具执行、状态记录、预算、恢复和事件 |
| 工具 | 有 Schema、有权限边界、能返回真实结果的应用能力 |
| 工作流 | 必须由代码保证的确定性步骤，例如保存、幂等提交、轮询、下载 |

Agent 不需要每次都调用工具；用户只问概念时可以直接回答。复杂任务也不要求固定经过“脚本 → 分镜 → 生成”，应由实际需求和工具结果决定下一步。

## 2. 技术选型

### 2.1 候选方案

| 方案 | 适用点 | 对本项目的代价 | 决策 |
|---|---|---|---|
| 直接基于 DeepSeek REST 自建全部能力 | 协议控制最直接、依赖少 | 需自行维护流解析、消息序列化、工具调用增量与错误归一化 | 保留为 ModelAdapter 的替代实现 |
| AI SDK + 自有轻量 Harness | 有 DeepSeek 适配、文本流和工具调用支持；适合 TS 项目 | 项目仍需负责持久化、执行记录、上下文和异步任务 | 历史备选，与当前 Python 路线不一致，不采用 |
| LangGraph Python | 提供状态图、持久执行、流和中断等机制 | 自主定义状态、路由、工具策略和上下文，同时处理外部操作幂等 | **采用**：复用图执行底座，展示项目自身的 Harness 设计与评测 |
| 基于 DeepSeek Harness 扩展 | 已有 Web、工具、会话、配置及插件结构；开源 MIT | 官方仍是 developer preview，明确可能出现兼容性破坏；需适应 Cordis 和已有工作区产品结构 | 参考架构，暂不作为独立 vagent 包的运行时依赖 |

这是一项范围选择，而非框架性能排名。独立 Python 产品、DeepSeek 优先、视频专用工具和可控的初期复杂度，是本次选择的依据。

### 2.2 采用的组合

- **决策模型**：DeepSeek 官方 API，初始候选模型 ID 为 `deepseek-flash`；模型可配置，M1-A0 记录实际可用型号与行为。
- **模型接入**：`langchain-deepseek` 的 `ChatDeepSeek`，直连 DeepSeek；不依赖网关或第三个平台 Key。
- **运行时**：Python 3.11+（本地验证使用 Python 3.12）。
- **Harness**：项目自有 `AgentRunner`，显式使用 LangGraph StateGraph 定义 model/tools 节点及路由；逐步补齐上下文、skills 和恢复。
- **Schema**：Pydantic，生成提供给模型的 JSON Schema，执行服务端参数校验；持久状态也通过模型校验。
- **持久化**：版本化 JSON Store + 单进程串行写入；通过接口隔离，后续可以替换数据库。
- **外壳**：CLI 先验收，FastAPI 本地 Web 随后接同一套 Runner 与事件。

模型适配器只执行一个模型步，不自动执行工具。AgentRunner 的 LangGraph 图拥有唯一的循环与路由控制权，避免多个框架重复执行工具或独立重试。

第一部分通过 ChatDeepSeek.bind_tools(...).ainvoke(...) 完成单步请求，依赖由 requirements-dev.lock 锁定。模型协议映射已用模拟 HTTP 验证，真实 Key 联调尚未完成；逐 Token 流和图级持久恢复继续分模块实施。

### 2.3 已查明的 DeepSeek 协议约束

2026-09-29 查阅的官方文档使用 `deepseek-flash` 演示工具调用；不能从旧示例直接沿用已经变化的模型别名。

- 模型只提出工具调用；工具的实际执行由应用实现。
- 开启 thinking 且携带 tools 时，官方要求后续请求保留并传回相关历史 `reasoning_content`。模型适配器必须支持必要的协议元数据，不能只保存显示给用户的文本。
- Chat Completions 不支持任意在历史中插入模型未发出的工具调用；异步恢复应回答原有的工具调用，不能伪造调用记录。
- strict 工具 Schema 属于 Beta 且有类型限制；首版先采用稳定接口和本地强校验，不依赖 Beta 自动纠正参数。

初始最小闭环显式关闭 thinking，先验证工具循环。启用 thinking 是兼容性扩展项：验证 SDK 往返保留字段、上下文处理和恢复后，再开放配置。模型协议元数据不作为界面中的“执行日志”输出；用户看到的是操作摘要、工具状态和产物。

## 3. 架构与依赖方向

```text
CLI / Web
    │ 用户输入、停止/继续请求；消费事件
    ▼
ApplicationService
    ▼
AgentRunner ── ContextBuilder ── RunPolicy
    │              │
    │              └── SessionStore / ProjectStore / ArtifactStore
    ├── ModelAdapter ── DeepSeek API
    ├── ToolRegistry ── ToolExecutor ── 本地创作工具
    │                                   └── 视频工具（M1-B 起）
    │                                        ▼
    │                                    JobService
    │                                        ▼
    │                               VideoProviderAdapter
    │                                Mock → Wan / 其他
    └── CheckpointStore / EventStore
```

依赖约束：

1. `agent/` 不导入万相 SDK、百炼鉴权、分辨率列表或供应商状态码。
2. Web/CLI 不直接调用 DeepSeek 或视频服务，所有入口共享应用服务。
3. 视频工具面向 `JobService`；供应商协议由适配器完成。
4. 用户凭证通过服务端依赖注入提供，不进入模型上下文、工具参数或工具返回值。
5. 视频能力按配置查询；没有视频 Key 时，本地创作工具依然可用。

## 4. Agent 循环

```text
接受用户消息并建立 Run
  → 组装系统约束、项目状态、相关产物和有效消息
  → DeepSeek 返回文本 / 工具调用
  → 完整收集工具调用，校验、登记并执行
  → 持久化真实工具结果，关联原 toolCallId
  → DeepSeek 根据结果决定继续、追问或结束
```

### 4.1 具体执行规则

1. 使用 `clientRequestId` 接收消息；同一请求重复到达时返回已有 Run。
2. 每个会话最多一个正在执行的 Run。其他消息明确排队，或由用户停止旧 Run 后发起新 Run。
3. 调用模型前保存步骤起点；流式文本作为草稿事件发送，完整 assistant 消息形成后才能执行其中的工具。
4. 按调用顺序串行执行首版工具。持久化调用记录后再开始执行；依赖前一个工具结果的下一步必须经过模型再次观察。
5. 每个模型提出的调用都获得匹配的结果或明确拒绝/错误结果，不留下无法配对的工具消息。
6. 工具结果回传模型后继续循环。只有最终回复、追问、等待外部任务、用户停止、预算耗尽或明确失败才结束/暂停。
7. 明确区分普通答复和有事实依据的完成：保存类操作必须有成功结果及产物 ID，不能凭模型文字宣称文件已保存。

### 4.2 初始运行限额

| 限额 | 初始值与行为 |
|---|---|
| 模型步数 | 每个 Run 最多 8 步，参数纠错也计入 |
| 工具调用数 | 每个 Run 最多 12 次，超限返回受控结果并停止继续调用 |
| 模型超时 | 单次 60 秒；连接、流中断与完整响应分别处理 |
| 活动执行时间 | 每个 Run 累计 180 秒；等待外部 Job 的时间单独记录 |
| 参数纠错 | 连续相同 Schema 错误最多向模型反馈 1 次后停止 |
| 工具重试 | 已确认无副作用的只读操作可有限重试；写操作依据执行记录恢复 |
| 上下文预算 | 初始软上限 32K Token，其中预留输出及后续工具结果空间；不代表供应商上下文上限 |

限额由配置决定。停止时保留已完成产物并说明未完成部分；用户主动继续可以创建关联的新 Run，不能自动重置预算无限执行。

## 5. 首批工具与真实 Agent 验收任务

### 5.1 M1-A 的内置工具

| 工具 | 实际作用 | 边界 |
|---|---|---|
| `project_read` | 读取当前项目创作目标、受众、风格、约束及产物索引 | 只访问服务端绑定的 projectId |
| `project_update` | 更新结构化创作需求，返回新 revision | 字段白名单、版本检查，避免覆盖更新后的需求 |
| `plan_update` | 保存用户可见的简短任务清单和状态 | 面向复杂任务，不强迫每个简单问题先列计划 |
| `artifact_save` | 保存脚本、创作方案或镜头描述为版本化产物 | 文件位置由 Store 决定；用户材料作为数据处理 |
| `artifact_read` | 按产物 ID 读取指定版本或范围 | 返回有界正文与版本，不接受任意磁盘路径 |

工具执行的是确定性的读写，内容由当前 Agent 生成。不把 `write_script` 再包装成一层偷偷调用 DeepSeek 的工具，以免形成难以计费、跟踪和恢复的嵌套模型循环。

M1-A 可以创作脚本、文本分镜和修改方案，但不承担多镜头视频生成、拼接或独立 Studio 的交付要求。

### 5.2 示例验收

用户：“帮我准备一个咖啡店短视频创作方案，面向上班族，暖色调，把方案保存下来。”

预期能力：读取项目 → 补充必要信息或使用明确默认值 → 保存项目需求 → 保存方案 → 根据工具返回的 ID 和版本告知用户产物位置。动作顺序不是固定模板，验收关注最终状态和事实依据。

用户继续：“改成雨夜氛围，保留受众设定和原版。”

预期能力：读取已有项目和方案 → 修改相关内容 → 新增产物版本 → 保留原版 → 告知改动。关闭进程后重新启动，仍能读取这些已保存的状态。

这两个任务只使用 DeepSeek Key；真实读写结果与多轮工具观察是验收主体。

## 6. 数据与上下文

### 6.1 分离的数据对象

| 对象 | 用途 |
|---|---|
| Session | 用户可见的持续对话，关联 projectId |
| Run | 一次用户请求及其模型步、预算、停止原因和恢复点 |
| ToolInvocation | 某一步工具调用、输入摘要、状态、结果、operationKey |
| Project | 持久创作需求和约束，可跨 Run 读取 |
| Artifact | 有版本的脚本、镜头说明、参考素材或输出视频 |
| Job | 外部耗时操作；包含 providerTaskId，与 Run 生命周期独立 |

初期可把这些对象存入同一个版本化 JSON Store，保证本地写操作与其工具完成记录一起提交。原始对话记录和模型上下文视图分离；SDK 对象不会直接成为领域数据结构。

### 6.2 上下文组装

输入包括产品规则、已注册工具描述、当前项目约束、近期完整对话和按需读取的产物。完整脚本、素材和历史日志不无条件塞入每次请求。

- 原始历史留存，ContextBuilder 决定本次模型看到的内容。
- 截断以完整的 assistant 工具调用与对应结果为单位，不切断消息配对。
- 已完成的旧轮次可裁剪，项目事实通过结构化 Store 保留，必要时让模型调用读取工具。
- 首版不依赖额外模型做自动摘要；若保留必要上下文后仍超过预算，应提示缩小本次任务，而非截断正在使用的工具协议。
- 推理模式启用后，保留轮次的协议元数据必须一并保留；压缩与重新开上下文的行为单独验证。
- 工具输出、用户材料和脚本文字是数据，不能提升为系统指令。

## 7. 停止、恢复与可观察性

### 7.1 Run 状态

```text
created → running → completed
             ├── waiting_user     # 有明确追问，下一条回答建立关联 Run
             ├── waiting_external # M1-B：存在尚未返回的 await_job 工具结果
             ├── interrupted      # 进程中断，可从检查点继续
             ├── cancelled        # 用户停止 Agent
             └── failed           # 错误或预算耗尽，带明确原因
```

“继续”针对可恢复检查点；新需求或回复追问建立新 Run。用户停止后，不能因 Job 回调而自动重启被停止的 Agent。

### 7.2 工具执行记录

- `operationKey` 由服务端依据 Run、步骤及调用 ID 生成，模型不能自定义。
- 工具状态包括 `prepared`、`running`、`succeeded`、`failed`、`unknown`。
- 恢复前先查看执行记录：有完成结果则复用；本地写工具通过同一 Store 提交领域更新和完成结果。
- 外部提交无法与本地状态原子提交；没有可靠结果时进入 `unknown`，不自动重新扣费。
- 工具已完成但下一次模型调用未完成时，从已保存的工具结果继续；不再次执行工具。
- 流中断时丢弃未完成的 assistant 草稿，保留已提交步骤；再次请求模型可能产生文本调用费用，计入 Run 记录。
- 停止后继续会话前，为尚未结束的工具调用保存匹配的终止结果；对 `await_job` 表示停止等待，对不确定的外部提交保留 `unknown`，不能写成“云端已取消”。ContextBuilder 不向下一次模型请求提供悬空的工具调用。

### 7.3 事件

统一输出 `run.started`、`assistant.delta`、`tool.started`、`tool.completed`、`artifact.updated`、`run.waiting`、`run.completed`、`run.failed` 等事件。M1-B 起补充 `job.updated`。

事件具有递增序号、Run/Session 标识和 revision。状态先落盘，再通知客户端；Web 重连获取快照并去重。文本 delta 是临时草稿，刷新后以最终消息或中断状态为准。

日志记录模型名、调用步数、Token 用量、耗时、错误码及工具摘要；不记录 Key，不默认上传到第三方观测服务。

当前已实现每次模型适配器调用的持久用量记录与 `vagent usage` 查询，包含 DeepSeek 缓存命中/未命中 Token、耗时、调用状态和按 Token 加权的命中率。缺失用量保持未知，恢复保留原记录与总量；这是本地用量观测，不包含服务商价格换算。字段和验证口径见 [用量观测验收](./docs/USAGE_ACCEPTANCE.md)。

## 8. 与视频生成结合

### 8.1 先设计并模拟的接口

```python
from typing import Protocol

# 后续视频模块的协议草案；领域类型尚未实现。
class VideoProviderAdapter(Protocol):
    def capabilities(self) -> VideoCapabilities: ...
    async def submit(self, req: VideoRequest, operation_key: str) -> ProviderTaskHandle: ...
    async def query(self, task_id: str) -> ProviderTaskSnapshot: ...

# 仅支持取消的供应商实现此能力。
class CancellableProvider(Protocol):
    async def cancel(self, task_id: str) -> None: ...
```

`operation_key` 用于本地关联和供应商支持时的幂等键，不能假设所有 API 都提供幂等提交保证。

`VideoRequest` 引用版本化脚本/镜头/素材 ID，包含提示词与输出规格；`VideoCapabilities` 按具体模型声明时长、画幅、参考图等能力，Agent 核心不硬编码万相的能力上限。

### 8.2 M1-B 注册的视频工具

| 工具 | 行为 |
|---|---|
| `video_capabilities` | 返回已配置供应商、模型及可用能力；模拟模式明确标注 |
| `video_generate` | 校验需求和源产物版本，持久化 Job，返回稳定的本地 jobId |
| `job_get` | 读取本地 Job 快照，供用户查询；不要求模型持续轮询 |
| `await_job` | 挂起当前工具调用与 Run，等待 Job 终态，不占用模型连接 |

后台 Worker 根据持久化待处理 Job 调用适配器、轮询并保存产物；恢复时检查上游 ID 与提交不确定状态。工具返回 jobId 只代表任务已登记，不等于云端已经受理。

### 8.3 异步结果如何回到 Agent

1. `video_generate` 迅速返回 jobId，模型观察到“任务已登记”。
2. 模型可直接告知用户已提交并结束当前 Run，Job 继续后台执行；界面直接展示其进度。
3. 如果用户目标需要完成后再处理，模型调用 `await_job`。Harness 保存原调用 ID，进入 `waiting_external` 并释放执行资源。
4. Job 完成后，后台协调器按原调用 ID 写入工具结果，通过状态版本检查只恢复一次，然后调用 DeepSeek 继续处理。
5. 如果 Run 已停止，不自动恢复；若仍在等待，进程重启后根据 Job 终态恢复。结果不写成模型未发出过的工具调用。

等待中的同一会话收到新消息时可以排队；用户也可先停止当前 Run 再发送新需求。已经创建的云端 Job 继续被单独跟踪。

### 8.4 为后续视频工作流保留的约束

- 脚本/镜头使用稳定 ID 与版本。以后“重做第三个镜头”能定位原对象，而不依赖聊天中的第三段文字。
- 产物类型支持 text/image/video，关系记录源产物版本与生成参数；M1-A 只实现文本。
- 生成任务记录估算费用与实际用量来源；收费发生在执行边界，不能因为模型循环或网络重试重复提交。
- M1-C 每条用户生成请求最多创建一个真实视频 Job；Agent 的多个本地读写工具不受“一次工具调用”限制。
- 停止 Agent、停止本地查询与取消云端生成是三个独立动作。上游没有取消能力时，明确说明生成可能继续。
- 供应商未配置时，工具提供能力不可用的明确结果，Agent 仍可完成创作、保存和修改。
- 不把模型写出的“视频已完成”当作完成依据；成功来自 Job 状态及真实落盘的视频文件。

## 9. 首版规模边界与演进条件

首版做单 Agent、少量受限创作工具、持久会话、检查点与一个后台任务类型。无需先实现通用 shell、任意文件系统、子 Agent、插件市场、向量数据库或复杂工作流编辑器。

当出现跨会话多分支调度、长时间多阶段恢复或人工审阅节点，且自有状态机维护成本明显升高时，重新比较 LangGraph 等运行时；领域工具、JobService 和存储接口保持可复用。

## 10. 选型验证与资料

M1-A0 必须验证：DeepSeek Key 直连、文本流、工具参数增量组装、工具结果回传后的第二次决策、工具错误反馈、中断后恢复；验证通过后固定 SDK 版本。没有真实 Key 时可以完成模拟测试，但不能宣告兼容实验通过。

本次只查阅公开文档，未运行 SDK 实验，也未发起收费模型请求。资料核实日期为 2026-09-29。

- [DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls)
- [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)
- [DeepSeek API 入门与当前模型](https://api-docs.deepseek.com/)
- [AI SDK DeepSeek Provider](https://ai-sdk.dev/providers/ai-sdk-providers/deepseek)
- [AI SDK Agent 概念与循环](https://ai-sdk.dev/docs/agents/overview)
- [LangGraph Python 概览](https://docs.langchain.com/oss/python/langgraph/overview)
- [DeepSeek Harness 官方仓库](https://github.com/deepseek-ai/deepseek-harness)
- [M1 实施计划](./M1_PLAN.md)
