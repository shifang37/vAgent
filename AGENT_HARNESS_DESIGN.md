# vagent Agent 选型与 Harness 设计

> 日期：2026-09-30
> 状态：已按求职项目目标选择 LangGraph 自主设计 Harness；第一部分实现与验证状态见 [README](./README.md)。本文其余能力为逐步实施的设计目标。  
> 产品定位：以 DeepSeek 为决策模型的视频创作 Agent。先建立可独立运行的 Agent，再为它接入视频生成能力。

当前原型已交付 Agent 循环、项目记忆、版本化产物、基础执行护栏、上下文裁剪、Skills、SQLite 持久图检查点和显式恢复，以及 M1 验收完成的模拟与真实单镜头视频链路（含本地媒体交付）。上下文预算暂以 UTF-8 序列化字节数衡量；2026-10-17 已完成 M2 的 D0–D4（结构化分镜、多镜头生成与逐镜头重做、素材库、用量统计、分镜工作台、离线端到端套件与真实成本受控双镜头联调，见 [M2 任务规划](./docs/M2_PLAN.md) 与 [M2 验收](./docs/M2_ACCEPTANCE.md)）；其余能力仍属逐步实施的设计目标，不能把下文全部视为已实现。恢复行为及限制见 [持久恢复验收](./docs/RECOVERY_ACCEPTANCE.md)。

已补齐 [模型与缓存用量观测](./docs/USAGE_ACCEPTANCE.md) 和 [上下文 v2 优化](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)：固定规则、Skill 元信息位于动态项目事实之前，工具定义保持确定性顺序，JSON 去除冗余空白，并保留旧检查点的上下文格式。真实缓存命中收益待 API 联调验证。

## 1. 设计目标

任务四已交付可选 [Redis 回答缓存](./docs/REDIS_CACHE_ACCEPTANCE.md)：仅在显式只读模式缓存精确匹配的最终文本，命中不请求模型；工具写权限由代码移除，状态变化自然失效，故障回退并保留现有检查点恢复机制。

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

2026-10-10 已完成 M1-B 的 B0–B5：契约、持久 Job/Mock Worker、视频工具、生产等待/恢复、CLI/Web 与阶段验收。Runner 只处理通用延迟结果与原调用交付，领域解析器提供具体结果；SQLite 中断、JSON 等待与模型尝试边界均有重启补偿，见 [契约协议](./docs/M1B_CONTRACTS.md) 和 [B3 验收](./docs/M1B_B3_ACCEPTANCE.md)。应用生命周期、完整离线矩阵、5/5 真实 DeepSeek + Mock 套件及独立 wheel 已通过 [M1-B 验收](./docs/M1B_ACCEPTANCE.md)；当前状态以 README 为准。

### 8.1 先设计并模拟的接口

```python
from typing import Protocol

# B0 定义领域协议，B1–B3 已实现 Job/工具/等待；Worker 应用生命周期由 B4 接入。
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
- M2 起，一次生成调用可登记**多个**镜头 Job，替代 M1-C 的“每个 Run 最多一个新视频 Job”，由“每 Job 金额、每 Run 新 Job 数、每 Run 累计估算”三重上限约束；M1-C 的单镜头流程作为 N=1 特例继续可用。
- 分镜与镜头使用服务端生成的稳定 ID 与只增不改的版本；“重做第 3 个镜头”定位该 Shot ID，产生新 ShotVersion 与新 Job，其他镜头的产物、版本与 Job 不动。
- 素材库向工具只读暴露参考图元数据，不开放任意文件系统路径；参考图以 Base64 data URL 送达上游，不经公网暴露、不经第三方图床、不经非生产上传通道。
- 集合等待（`await_job` v4）登记一次等待并在终态或到期后返回逐镜头结果与失败摘要；等待不增加模型调用，单 Job 阻塞不扩散。
- 未核实的模式（视频续写）不进入能力表；越界参数返回明确错误，不自动替换模型或规格。

### 8.5 M2 多镜头工作流（D0 冻结）

M2 在 M1 之上加入结构化分镜、多镜头生成、逐镜头重做、图生视频与素材库。工具与版本冻结为 `toolsVersion/rulesVersion=4/4`、能力版本 `wan27-shot-beijing-v2`、`adapterVersion=wan-http-v2`；数据对象、迁移、费用护栏、确认流程与集合等待语义见 [M2 契约](./docs/M2_CONTRACTS.md)。

| 工具 | 行为 |
|---|---|
| `storyboard_read` | 只读返回当前分镜、版本与有序镜头及各镜头版本；跨项目拒绝 |
| `storyboard_save` | 校验来源与镜头上限，产生新 StoryboardVersion（保留原版），返回分镜版本与稳定 Shot ID |
| `shot_generate` | 一次调用原子登记多个镜头 Job，回显逐镜头与合计估算、已用与剩余额度；任一校验失败整体不登记 |
| `video_generate` / `job_get` | 保留 M1-C 单镜头与只读语义不变 |
| `asset_read` | 只读列出素材库参考图元数据（assetId、来源项目、格式、尺寸、导入时间）；不含路径、私有 URL 与指纹，也不能导入或删除 |
| `await_job` | 接受 `jobIds` 集合，一次登记等待，返回逐镜头结果与失败摘要；单元素等价 M1-C |

**D1 实现状态（2026-10-16）**：schema v4 与 `storyboards`/`assets` 集合、分镜域模型（稳定 Shot ID、只增不改版本、素材引用校验）与 `storyboard_read`/`storyboard_save` 已在 mock 视图按 4/4 工具与规则版本实现；`executionVersion=3` 引入，1/2 保持原恢复路径，pre-M2 Run 按记录的 toolFeatures 还原原工具集并保持签名逐字节不变。`shot_generate`、集合等待与 live 的 4/4 协议按 D2 实施（live 暂保持 3/3）。迁移夹具、签名与回归证据见 [D1 验收](./docs/M2_D1_ACCEPTANCE.md)。

**D2/D3 实现状态（2026-10-17）**：D2 交付多镜头批量原子登记、三重上限与额度快照、集合等待（execution v3 的 `jobset` 绑定）、部分失败隔离、逐镜头重做，并把 live 切换到 4/4 与 `wan27-shot-beijing-v2`（`wan-http-v2`，受控 HTTP）. D3 在同一 4/4 版本内新增只读素材工具 `asset_read`、素材库导入/读取接口与文件恢复、用量聚合读取侧、以及 CLI（`storyboard show`、`shots redo`、`assets list|import`、`usage --project`）与 Web 分镜界面（分镜面板、批量确认、序列连播、素材库页、用量面板）；显式用户动作使用独立的“客户端 Run”，不占用对话 Run 的 latest 与额度。由于 M2 视图在 4/4 内新增工具，D2 构建录制的 M2 Run 不承诺跨 D3 恢复（显式恢复返回 `RESUME_CONFIG_CHANGED`，与 D1 记录的边界一致；D2 签名哈希保存在 `tests/test_m2_compatibility.py`），`for_execution_version(1/2/3)` 继续排除全部 M2 工具，pre-M2 签名逐字节不变。证据见 [D2 验收](./docs/M2_D2_ACCEPTANCE.md)与 [D3 验收](./docs/M2_D3_ACCEPTANCE.md)。
**D4 实现状态（2026-10-17）**：新增 `scripts/evaluate_m2.py`（默认离线的 M2 端到端套件：live 模式 + 受控上游 + 本地测试媒体 + 加速夹具时钟，7 用例）与 `tests/test_m2_evaluation.py`；`scripts/wheel_smoke.py` 扩展到安装态 M2 工作台（分镜/素材 CLI 与 HTTP 面）与离线套件。D4 未改动任何工具签名、规则文本、执行/上下文版本、能力版本、价格、来源策略或校验版本——只新增脚本与测试，因此 D3 记录的恢复边界与 pre-M2 逐字节签名证据保持不变。证据见 [M2 验收](./docs/M2_ACCEPTANCE.md)。D4 亦完成真实成本受控多镜头联调：一次登记两个镜头、集合等待后逐镜头本地交付，随后一次显式单镜头重做，共 3 次提交、9.00 CNY 估算，结果主机仍在 `wan-result-hosts-v2` 内；i2v/首尾帧与镜头延长未做真实样本。

### 8.6 M3 成片、多供应商与 headless（E0 冻结，E1–E4 已实施）

2026-10-21 完成 E0，实施契约见 [M3 契约](./docs/M3_CONTRACTS.md)，研究和合成证据见 [E0 验收](./docs/M3_E0_ACCEPTANCE.md)。E0 仅冻结要求与基线；**E1–E4 已按下列规则实施**（见 [E1 验收](./docs/M3_E1_ACCEPTANCE.md)、[E2 验收](./docs/M3_E2_ACCEPTANCE.md)、[E3 验收](./docs/M3_E3_ACCEPTANCE.md) 与 [M3 验收](./docs/M3_ACCEPTANCE.md)），视频 submit=0。E4 的端到端验收修复了 `film_assemble` 的 Runner 执行上下文、`film-state.js` 静态资源、设置页供应商切换与 AAC `esds` 码率归一化四个缺陷；新增付费样本未执行。

E1 新增 `Film`/只增不改的 `FilmVersion`，以分镜当前版本与最新已交付 ShotVersion 的有序快照生成本地成片；缺失或未完成镜头整体拒绝。`film_assemble` 为可取消的本地异步写工具：锁内取快照与登记意图，锁外执行受限子进程，锁内复核来源 revision/哈希并原子登记版本、FilmMedia 与完成操作。不能在现有同步工具锁内运行 FFmpeg。它不创建 generation Job、不增加 submit；`film_read` 只读。schema v5 仅新增空 `films` 与外壳版本，旧记录不补字段；`media` 使用独立 FilmMedia union 分支，禁止伪造生成 Job。

拼接采用固定 `imageio-ffmpeg==0.6.0` 包内二进制 concat + stream copy。新 `mp4-copy-signature-v1` 检查编码配置和样本时间线，旧结构检查不能证明兼容；E0 已保留 AAC 非单调 DTS 反例。E1 须完成样本推导、停止/崩溃补偿、浏览器解码及平台二进制/许可证验收。

工具与规则在 E1 为 **5/5**，E2 为 **6/6**；execution/context/checkpoint 保持 **3/2/1**。恢复以保存的完整 toolFeatures 身份选择冻结工具表、规则与能力，即使 execution 未变也须还原后核对签名；旧 4/4 与 E1 5/5 的真实检查点恢复均为后续门槛，未知组合拒绝。E2 新 Job/Request v6 与 `multi-provider-v3` 增加 Kling Singapore t2v 直连和 fal Kling standard 静音中转，历史 1–5 精确分派不变。各 provider/currency 及 Kling units 独立预算、Run 共享至多 4 个新 Job；新来源策略绑定认证任务的完整 URL，并校验/固定公网连接地址，Wan 旧允许集不变。

**E2 实现状态（2026-10-21）**：Job/Request v6 只用于新供应商，Wan 1–5 原类与精确分派不变；Kling/fal 各发布一条 t2v/5 秒/720p/16:9 能力（Kling 原生音频 + `VIDEO_UNIT`，fal standard 显式静音），tools/rules 6/6 并保留 5/5 冻结变体（含旧 `video_generate` 参数 schema），真实 SQLite 检查点验证 5/5 Run 在 6/6 构建下恢复。预算按 provider/currency 分桶并附 Kling units，Run 共享 4 个新 Job；写工具只登记，不提交。新来源策略在认证成功的任务结果上生成 `resultSource{taskId,origin,urlFingerprint}`，下载逐次核对精确 origin 与整条 URL 指纹、禁止重定向，并由固定 DNS 答案的传输层连接公网地址；Wan 静态允许集不变。配置新增 `VAGENT_KLING_API_KEY`/`FAL_KEY` 与逐供应商上限（只能收紧），保存即热更新；Kling/fal 无已核实免费探测，只标注未验证。

E1 实施的三个实现要点：①接缝时长由每输入的样本表/预卷推导（`max(视频原始结束, 音频原始结束)`），不接受夹具常量；②FFmpeg 在接缝处拉长一帧属于允许的封装偏移，须落在 `max(一帧视频, 一帧 AAC)` 内并逐条写入 `assembly.timeline`，出现 DTS 回退/损坏告警即拒绝；③新增 `async_write_execute` 执行类别承载锁外本地写，工具恢复改为按 `toolFeatures` 身份匹配（`register_tools_variant`/`for_record_features`），不再只按 executionVersion 分派。

E3 headless 复用 ApplicationService/Runner/Store 与原等待/恢复机制：默认生成 planned 产物，后以 Run ID、计划指纹、明确确认及各预算授权固定请求集合，resume 复用原授权。默认用户 skills 目录合并到内置根之后，保留显式环境变量全量替换语义、raw 哈希版本与按需读取；用户坏文件隔离、内置名保留，旧 Run 不注入新增 skills。它不开放可执行插件。

**E3 实现状态（2026-10-21）**：`vagent headless PROMPT` 以收窄后的分镜工具视图运行普通 Agent Runner（保留 `storyboard_save`/读取工具，移除 `shot_generate`、`video_generate`、`film_assemble`），计划冻结有序规范请求、provider/model/profile、能力/价格版本与逐桶估算并作为 Run 的可选 `headless` 字段持久化，不新增 schema。第二次命令以 Run ID、计划指纹、`--confirm` 与逐桶 `--max-cost`/`--max-units`/`--max-jobs` 授权固定集合；授权本身不登记，登记事务内再核对计划、当前分镜 revision、请求指纹与所有预算桶，任一不符整批不写。等待复用 Job/Media Worker 与既有终态判定，交付复用 `FilmService` 与本地拼接；`--resume` 只从持久状态继续，不增额度、不重新提交。`--json` 固定字段与退出码 0/2/3/4/5/6/130，无 TTY 不交互。用户 skills 默认按「内置根 → `VAGENT_HOME/skills`」合并，内置名优先、坏文件只诊断；显式 `VAGENT_SKILLS_DIR` 仍整体替换，新增/修改 skill 改变签名并以 `RESUME_CONFIG_CHANGED` 拒绝旧 Run。全部证据为受控 HTTP 与本地测试媒体，视频 submit=0。

### 8.7 M6 成片后期（E0 冻结，E1–E4 已实施）

M6 契约见 [M6 契约](./docs/M6_CONTRACTS.md)，E0 基线与 FFmpeg 能力见 [E0 验收](./docs/M6_E0_ACCEPTANCE.md)；**E1（字幕）与 E2（音频）已于 2026-10-22 实施，E3（转场/标题卡/整集/导出）与 E4（整集成片验收）已于 2026-10-23 实施**，证据见 [E1 验收](./docs/M6_E1_ACCEPTANCE.md)、[E2 验收](./docs/M6_E2_ACCEPTANCE.md)、[E3 验收](./docs/M6_E3_ACCEPTANCE.md) 与 [E4 验收](./docs/M6_E4_ACCEPTANCE.md)，视频 submit=0、模型调用=0、TTS 真实请求=0。

E1 的边界选择：

- **存储**：schema 7 只加 `audio`/`subtitleTracks` 两个空集合，并让 media 联合新增 `kind=audio`（`contractVersion=3`）；`v6→v7` 仍是机械迁移 + 逐字节备份。`FilmVersion` 只追加可选 `render`，因此 `Film` 与嵌入它的 `DatabaseV5`/`Database` 的 schema 哈希随之内联变化；其余 E0 模型逐字节不变。
- **字幕时间轴**：`subtitle-align-zh-v1` 只读冻结的 `SceneVersion.dialogue[]` 与时间线；所有时间在内部是整数微秒，镜头窗口边界只舍入一次，人工修正只校验不重排。分镜已有成片时按成片有理数时间线对齐，否则按分镜镜头时长；导出要求逐段相等，否则 `RENDER_TIMELINE_MISMATCH`。
- **工具面**：`subtitle_save` 是普通同步写工具（无 Job、无费用、走 `store.operation` 重放），tools/rules 10/10，并按 `toolFeatures` 冻结 9/9 及其之前的全部变体；`storyboard_read`/`episode_read` 只追加 `subtitle` 投影。
- **导出**：`ffmpeg-render-v1` 在 E1 只实现字幕子集——锁内冻结源成片与字幕版本，锁外 FFmpeg 软封装/烧录，锁内复核源哈希与产物后把文件、`FilmVersion(render)`、`FilmMedia` 与 Operation 一次提交；音频原样复制，1080P/整集/响度留给 E2/E3。
- **字体**：烧录只使用包内 OFL 子集（固定名称 + SHA-256），缺失即 `FONT_MISSING`，不回退系统字体；`mp4-avc-v1` 结构校验接受 `mov_text` 字幕轨，但仍拒绝外部数据引用、重叠样本和未知轨道。

E2（音频）的边界选择：

- **TTS 与额度**：`tts-providers-v1` 按模式发布 live（CosyVoice v2）或 mock（`local-tts-v1`）；`audio_generate` 必须显式 `confirm`，在锁内从 `audio` 集合重算单条 500 字符/每集 2000 字符/40 次请求与 0.10/0.40 CNY 上限，超限整批拒绝；TTS 调用在锁外，全部成功后才把 `AudioMedia` + `AudioAsset` + Operation 一次提交。同集同 speaker 首次生成冻结 provider/model/voiceId/speed（`VOICE_CONFLICT`），相同台词与音色重放复用原资产。
- **工具面**：`audio_generate` 是 `async_write_execute`（无 Job、无等待），tools/rules 11/11，冻结 10/10 与更早变体；`episode_read` 只追加 `audio` 投影。headless 规划排除该付费写工具。
- **混音**：`mix-zh-v1` 以 `amix normalize=0` 保留 voice 0 dB/BGM −18 dB/SFX −6 dB，BGM 用 `sidechaincompress`（−20 dB 线性 0.1、8、20 ms、400 ms）ducking；两遍 `loudnorm`（−16 LUFS/−1 dBTP/LRA 11）后用 EBU R128 复测成品，超差即 `LOUDNESS_OUT_OF_RANGE` 并删除产物。E2 只交付本地混音服务，`film_render` 集成留给 E3。

### 8.8 M7 多集与工作台（E0 冻结，E1 已交付）

M7 契约见 [M7 契约](./docs/M7_CONTRACTS.md)，E0 基线与本地裁剪探针见 [E0 验收](./docs/M7_E0_ACCEPTANCE.md) 与 `docs/M7_E0_BASELINE.json`。**E0 只冻结要求与基线，不改产品代码：视频 submit=0、模型调用=0、TTS 请求=0。** E1 已交付（见 [E1 验收](./docs/M7_E1_ACCEPTANCE.md)）：schema 9 三个空集合与 v8→v9 逐字节备份、`Season`/`SeasonVersion`、`season_save`/`season_read`、tools/rules 15/15（冻结 13/13）、CLI/Web 季面板；既有 165 个契约模型哈希不变，视频 submit=0、模型调用=0、TTS 请求=0。

E1 的实现边界：

- **存储**：`DatabaseV9` 只加 `seasons`/`batches`/`timelines` 三个集合；E1 只写 `seasons`，另两个保持空映射，交由 E2/E3 的域校验器填充；`v8→v9` 机械迁移与逐字节备份，v1–v8 全部可读，打开与每次提交都重跑季校验。
- **季校验**：Store 级 `validate_season_changes` 复核版本连续、`currentVersion` 指向最新版、每项目 ≤8 季、每季 1–4 集、成员有序唯一、同一集至多属于一个季的当前版本；历史版本逐字节保留，删除或改写即 `SEASON_VERSION_CONFLICT`/`SEASON_EPISODE_CONFLICT`。
- **幂等与并发**：`season_save` 以完整内容（标题 + 有序成员）判等，相同内容返回当前版本并标记 `replayed=true`，不追加空版本；内容变化必须带 `expectedRevision`，否则 `SEASON_VERSION_CONFLICT`。
- **零费用路径**：季保存不登记 Job、不调用模型或供应商；CLI/Web 的本地保存走 metadata-only 客户端 Run（`videoMode=off`、`toolFeatures={}`），因此文本项目在视频模式 off 下也能组织季。
- **工具面**：E1 追加 `season_save`/`season_read`（15/15），13/13 与更早变体按 `toolFeatures` 冻结；headless 仍只编排分镜，不加入季批量或时间线渲染。

后续阶段的边界选择：

- **存储**：schema 9 只加 `seasons`/`batches`/`timelines` 三个空集合；`v8→v9` 机械迁移 + 逐字节备份；E1 只新增 v9 模型，既有 165 个契约哈希与冻结上限逐字节不变。E3 才允许给 `FilmAssembly` 追加 `ffmpeg-render-timeline-v1` 字面量、给 `RenderSources`/`RenderTask` 追加可选时间线字段、给 `film_render`/`render_read` 追加时间线输入 schema，并在 E3 验收中逐项列出嵌套哈希变化。
- **季**：`Season`/`SeasonVersion` 只保存标题与有序 Episode ID；角色/风格仍是项目级共享圣经；一集至多属于一个季的当前版本，历史版本保留但不作为当前归属依据。
- **批量**：每次 `season_generate` 落一条 `Batch`，逐集原子登记（复用 `episode_generate` 语义），单集失败只标记该集；恢复必须显式 `resumeBatchId` 并创建新批次（`resumeOf`），只重试未成功集；不确定提交不重投。每季至多一个 `running` 批次，批量上限 1–3 集/60 Job/180.00 CNY。批次不新增等待绑定，调用方按集分段 `await_job`（每段 ≤20 个 Job，沿用 20 成员/600 秒上限）。
- **时间线**：每集一条 `Timeline`，版本只增不改；一条版本冻结有序 clip（`shotId/shotVersionId/storyboardId/sceneId/inMs/outMs`）、转场点、整集字幕轨、音轨与标题卡；`timeline-zh-v1` 指纹不含版本/时间。分集版本变化后旧时间线 `stale=true`，渲染返回 `TIMELINE_SOURCE_MISSING`。
- **渲染**：`film_render` 增加 `scope=timeline`；复用 M4 归一化与 M6 `ffmpeg-render-v1` 管线，新增装配字面量 `ffmpeg-render-timeline-v1`；裁剪吸附 30 fps，输出时长与有效时间线允许 ±1 帧；字幕按 `timeline-subtitle-remap-v1` 夹取/平移，voice 跟随重映射 cue，BGM/SFX 按 start；同指纹幂等，旧成片版本保留。
- **工具面**：`season_save`/`season_read`（15/15）→ `season_generate`（16/16）→ `timeline_save`/`timeline_read`（18/18）；13/13 与更早变体、15/15、16/16 全部按 `toolFeatures` 冻结。headless 仍只编排分镜，不加入季批量或时间线渲染。
- **工作台**：Web 优先的季面板与时间线编辑器；预览是客户端按 clip 顺序播放并应用裁剪偏移，不做服务端合成；导出才是交付口径。BGM/SFX 由客户端受控上传（许可/来源必填），不作为模型工具；不实现多轨混音工作站、关键帧/变速/特效或后台批处理守护进程。

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
