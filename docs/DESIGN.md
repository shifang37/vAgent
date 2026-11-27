# 视频 LLM Agent（vagent）设计方案

> 形态对齐 DeepSeek Harness：Python 包安装 → 本地运行 → 填入自己的 API key → 打开本地 Web GUI 使用。
> 定位：**专门适配视频生成 LLM 的 agent**，用户不写代码，用自然语言指挥 agent 完成脚本 → 分镜 → 生成 → 交付。

> **实施补充（2026-09-29）**：先完成只依赖 DeepSeek API Key 的 Agent 基础，再用模拟视频任务验证扩展接口，最后接入真实视频模型。当前实施顺序、选型和验收以 [M1 实施计划](./M1_PLAN.md) 和 [Agent 选型与 Harness 设计](./AGENT_HARNESS_DESIGN.md) 为准；实现语言已统一为 Python；下文保留产品远期目标供参考，未实现能力以 README 为准。

---

## 1. 产品形态（复刻 DSH 体验）

| 环节 | 做法 |
|---|---|
| 安装 | 从本地仓库执行 `python -m pip install .`（尚未发布 PyPI） |
| 启动 | `vagent web` → 自动打开浏览器 `http://localhost:3210` |
| 首次使用 | Web 向导引导填写各家视频 API key（可跳过，之后在设置里补） |
| 使用 | 对话式：「生成一条 15 秒赛博朋克城市夜景宣传片」→ agent 自动写脚本、拆镜头、调视频 API、展示结果 |
| 命令行 | `vagent headless "prompt"` 直接生成（脚本化/无头场景） |
| 配置 | `~/.vagent/config.yml` + 环境变量，key 保存在本机，仅用于对应供应商 HTTPS 认证 |
| 扩展 | 插件目录 `~/.vagent/plugins`、skills 目录，高级用户可扩展（借鉴 DSH） |

### 与 DSH 的关系（关键决策）
- **独立 Python 包分发**，不要求用户先装 DSH（开箱即用，`vagent` 一个命令搞定）。
- **架构借鉴 DSH**：本地 CLI + 内置 Web GUI + 「默认配置 + 用户覆盖」的配置层 + 插件/skills 扩展点。
- 未来可选：做成 DSH 兼容的 bundle，让已装 DSH 的用户通过 `dsh --profile video` 复用同一套能力（二期）。

---

## 2. 总体架构

```
vagent CLI (bin: vagent)
├── web        启动本地服务器 + Web GUI
├── headless   命令行直接生成（无头模式）
├── config     查看 / 编辑配置与 key
└── plugins    管理插件（借鉴 dsh plugin）

┌────────────────────────────────────────────┐
│ 本地 Web GUI（后续 FastAPI + 页面模板）│
│  首次向导 · 对话界面 · 任务进度 · 素材库 · 用量 │
└───────────────┬────────────────────────────┘
                │ HTTP / SSE（进度推送）
┌───────────────▼────────────────────────────┐
│ 核心服务（Python 本地进程）                  │
│  认证(本地单用户) · Key 管理 · 任务系统 · 资源 │
└───────────────┬────────────────────────────┘
┌───────────────▼────────────────────────────┐
│ Agent 编排层                                │
│  编排 LLM（默认 DeepSeek，可换）              │
│  工具集(function calling)：                  │
│    write_script / storyboard /              │
│    text_to_video / image_to_video /         │
│    first_last_frame / extend_video / assemble│
│  工作流状态机 + 项目级对话记忆                 │
└───────────────┬────────────────────────────┘
┌───────────────▼────────────────────────────┐
│ 视频适配器层（统一接口 + 各家 Adapter）        │
│  Veo(Gemini) · Seedance(火山) · 万相(DashScope)│
│  Sora(OpenAI) · Kling(快手) · fal/Replicate  │
└───────────────┬────────────────────────────┘
     用户自己的 key → 各视频云 API（HTTPS 直连）
```

---

## 3. 核心模块设计

### 3.1 CLI 与配置（对齐 DSH）
- `pyproject.toml` 的 `[project.scripts]` 注册 `vagent`。
- 配置根：`~/.vagent/`（跨平台用 `pathlib.Path.home()`），含 `config.yml`、`state/`（任务与素材持久化）、`plugins/`、`skills/`。
- 配置分层（借鉴 DSH 的 bundle + patch 思想）：
  1. 内置默认配置（能力差异表、默认参数、模型优先级）
  2. `~/.vagent/config.yml` 用户覆盖
  3. 环境变量（`VAGENT_VEO_KEY`、`VAGENT_SEEDANCE_KEY`…）优先级最高
- `vagent config` 子命令：查看生效配置、校验 key（试 ping 一次）、导出脱敏信息。

### 3.2 Key 管理与安全（本地单用户）
- **Key 由本地后端持有**：浏览器与本地服务交互，由本地服务通过 HTTPS 向对应供应商认证；不将 Key 注入提示词或返回浏览器。
- 写入 `config.yml`（建议 `chmod 600`），或环境变量注入。
- 支持多供应商并行绑定：配了哪个 key，agent 就能用哪个模型；未配置的模型在对话中自动跳过并提示引导。
- 用量统计：按官方单价估算每次调用费用，存本地 `state/usage.json`，GUI 用量面板展示。

### 3.3 视频适配器层（统一接口）
所有视频 API 都是异步长任务，抽象为统一接口：

```python
from typing import Protocol

# 后续视频模块协议草案，具体领域类型和适配器尚未实现。
class VideoAdapter(Protocol):
    capabilities: Capabilities  # 时长、分辨率、图生视频等能力

    async def generate(self, req: GenerateRequest) -> TaskHandle: ...
    async def poll(self, task_id: str) -> TaskStatus: ...

# 取消是可选能力，不能假定每个供应商都支持。
# GenerateRequest 包含 prompt、参考图、duration、aspect_ratio 等字段。
```

- 每个供应商一个 adapter（Veo→Gemini API、Seedance→火山引擎、万相→DashScope、Sora→OpenAI、Kling→Kling API、fal/Replicate→聚合中转）。
- **能力差异表**内置在默认配置中；agent 根据已配置的 key + 能力差异表自动选模型，参数越界自动修正（如请求 30s 但模型只支持 10s → 自动拆 3 段或提示）。

### 3.4 Agent 编排层
- **编排 LLM**：默认 DeepSeek（用户填自己的 DeepSeek/OpenAI key，或内置免费配额可选），通过 function calling 驱动工具链。
- **工具集**（每个工具都有 JSON Schema，LLM 可调用）：
  - `write_script(需求) → 脚本`
  - `storyboard(脚本) → 分镜 JSON`（镜头列表：画面描述、时长、景别、运镜、参考图）
  - `text_to_video` / `image_to_video` / `first_last_frame` / `extend_video`
  - `assemble(镜头列表) → 拼接产物`
- **工作流状态机**：需求理解 → 脚本 → 分镜 → 逐镜头生成（按并发限制批量）→ 审查（可选重做单镜头）→ 拼接交付。
- **项目级对话记忆**：一次会话 = 一个项目；「重做第 3 个镜头」只重做第 3 个，不重来。

### 3.5 任务系统
- 任务表（SQLite 或 JSON 文件持久化）：`task_id / project_id / provider / model / status / progress / cost / error / result_urls`。
- 提交后本地进程轮询上游（无 webhook 的供应商）或收 webhook（支持的）。
- SSE 向前端推进度；查询失败可有界重试。付费提交结果不确定时先对账，不盲目重试或自动换供应商重复生成。
- 结果文件：下载到 `~/.vagent/media/` 本地缓存，避免第三方 URL 过期。

### 3.6 本地 Web GUI（对齐 DSH web）
- **首次向导**：欢迎页 → 选择要用的供应商 → 填 key → 校验通过即激活。
- **对话界面**：主聊天流 + 侧栏当前分镜表 + 生成结果卡片（播放/下载/重做该镜头）。
- **Studio 模式（二期）**：分镜表可视化编辑，逐镜头改提示词/参数后批量生成。
- **素材库**：历史项目与产物，可复用参考图。
- **用量面板**：各 key 消耗、费用估算、成功率、本月统计。

---

## 4. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 运行时 | Python 3.11+ | Python Agent 生态与视频供应商 SDK 易于结合 |
| Web GUI | FastAPI + 页面模板（后续） | 通过 HTTP/SSE 复用 Python Runner |
| CLI | 标准库 argparse | 命令少，手写即可对齐 DSH 的 flag 风格 |
| 配置 | 环境变量 + python-dotenv；技能元信息用 PyYAML | 配置与模型上下文分开管理 |
| 状态存储 | JSON；后续可换 Python SQLite | 单机单用户足够 |
| 视频 API | 各家 REST 直连（httpx） | 统一 Python 适配器封装请求与异步任务 |
| 编排 | LangGraph Python + ChatDeepSeek + 自有 Harness | 复用图执行，自主实现上下文、工具策略与运行记录 |

---

## 5. MVP 分期

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M1** | CLI 骨架 + `vagent web` 启动 + 首次向导 + 配置/Key 管理 + **1 个视频模型**（Seedance 或 Veo）+ 对话式单镜头生成 + SSE 进度 + 预览/下载 | 用户装包 → 填一个 key → 一句话生成一段视频 |
| **M2** | 分镜多镜头工作流 + 图生视频/首尾帧 + 镜头延长 + 素材库 + 用量统计 | 一句话生成多镜头成片，可逐镜头重做 |
| **M3** | 拼接 assemble + 插件/skills 扩展点 + 更多供应商（Sora/Kling/万相）+ fal/Replicate 中转 + headless 模式完善 | 完整创作工作台，可扩展新模型 |

---

## 6. 关键风险与对策

| 风险 | 对策 |
|---|---|
| 各家 API 差异大（时长/参数/认证/异步方式） | 统一 adapter 接口 + 能力差异表，把差异收口在适配层 |
| 视频生成慢（分钟级） | 异步任务 + SSE 进度 + 多镜头并发（受供应商限流约束） |
| 用户 key 泄露 | 本地运行、进程内持有、配置文件权限收紧、日志脱敏 |
| 国内网络访问 Veo/Sora 受限 | 支持 fal/OfoxAI 等中转（用户自备中转 key） |
| 编排 LLM 幻觉参数 | JSON Schema 严格校验工具参数 + 能力差异表自动纠偏 |
