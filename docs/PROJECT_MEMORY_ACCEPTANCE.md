# 项目记忆与跨会话延续验收（P0–P4）

日期：2026-11-27。范围：`docs/PROJECT_MEMORY_DESIGN.md` 全部阶段（P0 数据层、P1 项目/会话解耦、P2 记忆能力与上下文、P3 会话绑定入口、P4 代码维护进展与两会话联调）。

## 交付总览

| 阶段 | 范围 | 关键落点 | 自动化验收 |
|---|---|---|---|
| P0 数据层 | schema 10、`memories` 集合、`SessionV10.projectId`、v9→v10 迁移、`ensure_session(project_id)` | `src/vagent/storage.py` | `tests/test_project_memory.py`（P-01） |
| P1 解耦 | 消除"项目 ID = 会话 ID"硬编码，统一经 `project_of_session` / `FileStore.project_of()` 解析 | `tools.py`、`runner.py`、`application.py`、`headless.py`、`cli.py`、`video/jobs.py`、`video/audio_service.py`、`video/clients.py`、`video/usage.py` | `tests/test_project_binding.py`（P-13） |
| P2 记忆与上下文 | `memory_read` / `memory_update`、上限与乐观锁、`ContextBuilder` 注入与先丢项目记忆降级、工具变体冻结（20/18 与 ≤18 冻结） | `src/vagent/project_memory.py`、`context.py`、`tools.py`、`video/tools.py`、`runner.py`、`config.py` | `tests/test_project_memory_tools.py`、`tests/test_tool_versions.py`（P-02–P-10、P-12） |
| P3 绑定入口 | `create_session(project_id)`、`start(project_id)`、`projects()`、CLI `--project`、`POST /api/sessions`、`GET /api/projects`、Web 侧边栏项目绑定与记忆台账展示 | `application.py`、`cli.py`、`web.py`、`storage.py`、`runner.py`、`web/index.html`、`web/app.js`、`web/job-state.js`、`web/styles.css` | `tests/test_project_binding.py`、`tests/test_web.py`、`tests/test_job_state.mjs`（P-11） |
| P4 代码维护进展 | `episode_save` 成功后在同事务内 upsert `ep-<episodeId>`（`kind="progress"`），重放幂等，台账满时不阻断分集保存 | `src/vagent/project_memory.py`、`src/vagent/video/episodes.py` | `tests/test_project_memory_tools.py`（P4 单元与离线双会话）+ 真实两会话联调 |

## 核心决策与不变量

- **项目与会话分离，不改写历史数据（D1/D7）**：`SessionV10` 独立于冻结的 `Session`，旧会话迁移不补 `projectId` 字段（缺失按自身 ID 解释），九个历史 `DatabaseV1…V9` 模型哈希与基线逐字节不变。
- **记忆是独立有界台账（D2/D8）**：`memories[projectId]` 拥有独立 `revision`，不撞 `Project.revision`。硬上限为 64 条、单条 400 字符、整份紧凑 JSON 8 KiB；模型写入超限返回 `MEMORY_LIMIT` 且不落盘。
- **不改 `contextVersion` 与 `contextSignature`（D3）**：项目记忆作为项目事实之后的运行时数据块渲染（v1 不插、v2/v3 在预算块之前、v4 在系统前缀内）。预算不足时优先丢弃项目记忆并发 `context.project_memory_dropped` 事件。工具挂在 `projectMemory` 特性下，旧 Run 恢复复现不含该特性的冻结表面与原布局。
- **`progress` 归代码所有（D5/P4）**：模型写入或删除 `kind="progress"` 均报 `MEMORY_KIND_FORBIDDEN`。`save_episode()` 写入分集后在**同一个 Store 事务**中调用 `upsert_progress()`，以 `ep-<episodeId>` 为键记录标题、版本、场数、镜头数与目标时长：
  - 同一 `operation_key` 重放由 `FileStore.operation` 直接返回已记录结果，不重复升记忆 `revision`。
  - `episode_save` 校验失败时整笔事务回滚，不产生幽灵进度。
  - 若台账已达 64 条或 8 KiB 硬上限，`upsert_progress()` 放弃追加并返回 `False`，**绝不阻断分集保存本身**（结构化 `episodes` 仍是唯一真源）。
- **回滚开关与默认上下文版本**：`VAGENT_PROJECT_MEMORY=off` 隐藏 `memory_*` 工具与记忆块且保留 schema 10 数据；`VAGENT_CONTEXT_VERSION` 保持默认 `2`（设计 §11 注明翻到 `4` 与本设计解耦、单独评审，本次未变更）。

## 验收判据对照（P-01–P-13 + P4）

| 编号 | 场景 | 证据（自动化用例） | 结果 |
|---|---|---|---|
| P-01 | v9→v10 迁移、备份与历史信封哈希 | `tests/test_project_memory.py`、`tests/test_m5_baseline.py` | 通过 |
| P-02 | 旧 Run 恢复（签名与布局不变） | `tests/test_project_memory_tools.py::test_old_run_resumes_unchanged`、`tests/test_b0_compatibility.py` | 通过 |
| P-03 | v1/v2/v3/v4 布局兼容 | `tests/test_context_layout_v4.py`、`tests/test_context_optimization.py`、`tests/test_project_memory_tools.py::test_v1_layout_never_renders_project_memory` | 通过 |
| P-04 | 新会话绑定旧项目读取记忆与产物 | `tests/test_project_memory_tools.py::test_new_session_sees_project_memory` | 通过 |
| P-05 | 64 条 / 400 字 / 8 KiB 硬上限 | `tests/test_project_memory_tools.py::test_memory_limits_are_enforced` | 通过 |
| P-06 | 乐观锁冲突 `MEMORY_REVISION_CONFLICT` | `tests/test_project_memory_tools.py::test_memory_revision_conflict` | 通过 |
| P-07 | 只读 Run 禁止写记忆 | `tests/test_project_memory_tools.py::test_read_only_cannot_update_memory`、`test_read_only_run_cannot_update_memory` | 通过 |
| P-08 | 跨项目隔离 | `tests/test_project_memory_tools.py::test_memory_is_project_scoped` | 通过 |
| P-09 | 预算挤压优先丢弃项目记忆并上报事件 | `tests/test_project_memory_tools.py::test_memory_dropped_before_context_limit`、`test_run_reports_the_project_memory_drop` | 通过 |
| P-10 | 同记忆连续两步前缀逐字节稳定 | `tests/test_project_memory_tools.py::test_memory_keeps_prefix_stable` | 通过 |
| P-11 | CLI `--project`、D6 优先级与 Web/HTTP 入口 | `tests/test_project_binding.py::test_service_create_session_start_and_projects_entry_points`、`test_cli_parser_and_project_binding_precedence_d6`、`test_cli_chat_forwards_project_flag`、`tests/test_web.py::test_web_projects_and_session_project_binding`、`tests/test_job_state.mjs` | 通过 |
| P-12 | 工具版本冻结（≤18 不含记忆工具，20/18 含） | `tests/test_tool_versions.py` | 通过 |
| P-13 | 视频域跨会话访问（分集/分镜/Job/成片/音频校验） | `tests/test_project_binding.py::test_new_session_sees_existing_video_state`、`test_new_session_sees_existing_film`、`test_run_started_in_a_bound_session_works_in_its_project` | 通过 |
| P4-1 | `episode_save` 自动维护 `ep-<episodeId>` 进展（新建/修订/重放/失败回滚/满账不阻断/禁止模型改删） | `tests/test_project_memory_tools.py::test_episode_save_maintains_code_owned_progress_idempotently` | 通过 |
| P4-2 | 双会话接力（离线确定性） | `tests/test_project_memory_tools.py::test_two_sessions_handoff_episode_progress_and_todo` | 通过 |

## 真实两会话联调记录（2026-11-27）

环境：独立临时目录 `tmp/p4-live-handoff`、`VAGENT_VIDEO_MODE=mock`（未调用任何视频生成供应商，零视频费用）、`VAGENT_PROJECT_MEMORY=on`、真实文本模型 `deepseek-flash`。输入素材为仓库既有测试剧本 `tests/fixtures/m5/texts/rainy-cafe.md`。

1. **会话 A（新建项目）**
   - 项目 / 会话 ID：`666ac9b0-d8ed-4297-b907-b22a91b26182`
   - Run：`d7240542-aa77-4b40-aefe-ece35d2285df`，`status=completed`，`modelSteps=6`，`toolCalls=6`，`inputTokens=147920`，`outputTokens=2555`
   - 过程：模型先调 `episode_plan` 切分剧本（8 场 19 镜、目标 90 秒），再调 `episode_save` 保存《第 3 集 雨夜咖啡店》（`aef71382-155b-489f-a5d2-a17da3b2c408`，v1）。代码在同事务中自动写入 `progress` 条目 `ep-aef71382-155b-489f-a5d2-a17da3b2c408` 并将记忆 `revision` 推进到 `1`。随后模型写入 `style-rain`（decision）与 `ep04-opening`（todo），过程中首次按旧 revision 写入触发 `MEMORY_REVISION_CONFLICT`，重读后以 `expectedRevision=1` 成功落盘，台账进入 `revision=2`。
   - 落盘记忆（3 条，按 `progress → decision → todo` 排序）：
     - `ep-aef71382-155b-489f-a5d2-a17da3b2c408`（`progress`）：`分集《第 3 集 雨夜咖啡店》(aef71382-155b-489f-a5d2-a17da3b2c408) 已保存 v1：8 场 19 镜，目标 90 秒`
     - `style-rain`（`decision`）：`全片保持雨夜冷蓝调，店招禁用暖黄`
     - `ep04-opening`（`todo`）：`第 4 集补拍雨夜后门追逐开场`
2. **会话 B（绑定同一项目）**
   - 会话 ID：`5ac74bc1-274f-43b5-9be8-7a26411e7440`（`projectId = 666ac9b0-d8ed-4297-b907-b22a91b26182`，未新建项目）
   - Run：`0e1fa859-c7ca-43cb-8ac4-f6e5c5a19801`，`status=completed`，`modelSteps=2`，`toolCalls=2`，`inputTokens=41505`，`outputTokens=314`
   - 结果：会话 B 在零历史对话的前提下准确回答出第 3 集标题/ID/v1/8 场 19 镜/90 秒进度、冷蓝调且禁用暖黄的风格决定，以及第 4 集补拍雨夜后门追逐开场的待办，并明确区分记忆进度摘要与正式项目字段。

## 自动化验证与环境说明

离线复现命令（无需网络、无 API 费用）：

```powershell
python -m pytest tests/test_project_memory.py tests/test_project_memory_tools.py tests/test_project_binding.py tests/test_tool_versions.py tests/test_web.py tests/test_docs.py tests/test_context_layout_v4.py tests/test_context_optimization.py tests/test_b0_compatibility.py tests/test_m5_tools.py tests/test_m5_episodes.py tests/test_m5_consistency.py tests/test_m5_generation.py tests/test_m5_baseline.py -q
node --test tests/test_job_state.mjs
ruff check src/vagent tests scripts
ruff format --check src/vagent tests scripts
```

本次实测结果：
- 定向 Python 测试集（项目记忆、绑定、工具版本、Web、文档门禁、上下文布局、B0 兼容与 M5 分集回归）：**164 passed, 1 skipped**（跳过项为当前沙箱环境不允许 FFmpeg 管道子进程的既有条件 `test_new_session_sees_existing_film`）。
- 前端状态单测 `node --test tests/test_job_state.mjs`：**12 passed, 0 failed**。
- `ruff check` 与 `ruff format --check`：零告警通过。
- 环境备注：本机 `.venv\Scripts\python.exe` 启动器存在 Windows `_ssl` DLL 加载失败（`C:\Program Files\Python312\python.exe` 直接加载 OpenSSL 3.0.16 正常）；上述定向回归通过基础 Python 3.12 + `.venv` 依赖路径执行，`tests/test_m5_cli.py` 因通过 `sys.executable` 派生子进程未透传 `PYTHONPATH` 未能在该变通环境下跑通，待修复本机 `.venv` 启动器后可再补跑全量。
