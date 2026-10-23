# M7 E0 验收：多集与工作台契约冻结

日期：**2026-10-23**。基线 `master` `5cfd754`（M6 E4 已合入；schema 8、tools/rules 13/13、冻结 11/11…1/1）。范围见 [M5 计划](./M5_PLAN.md) §5 的 M7，冻结契约见 [M7 契约](./M7_CONTRACTS.md)。**E0 不改变产品代码（`src/`、`web/`、`tests/`）、不注册工具、不调用模型或供应商：视频 submit=0、文本模型调用=0、TTS 请求=0。**

E0 的产出是「契约 + 基线 + 可行性证据」，实现从 E1 开始。**E1 的第一件事是按契约 §9 建立 schema 9 与 Season 域**；E0 基线 `docs/M7_E0_BASELINE.json` 保持冻结，不被后续阶段改写。

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 契约冻结 | `docs/M7_CONTRACTS.md` | 9 个决策点、schema 9 方向、`Season`/`Batch`/`Timeline` 三个域对象、`timeline-zh-v1` 与 `timeline-subtitle-remap-v1`、批量队列/失败隔离/恢复、工具面 13/13→18/18、13 个新错误码、确定性与基线保护 |
| 基线快照 | `docs/M7_E0_BASELINE.json` | **165** 个契约模型的 schema 哈希（`vagent.storage` 25、`video.contracts` 74、`video.films` 25、`video.episodes` 13、`video.storyboards` 7、`video.renders` 6、`video.audio` 8、`video.subtitles` 4、`video.tts` 3）、冻结上限、工具名分组、包内 FFmpeg 事实与本地裁剪探针 |
| 基线脚本 | `scripts/capture_m7_baseline.py` | 可复现地重建上述快照；不联网、不登记 Job、不调用模型，只对合成输入跑本地 FFmpeg |
| 验收文档 | 本文件 | 决策、证据、边界与 E1 入口 |
| 文档同步 | `M1_PLAN.md`、`docs/M5_PLAN.md` §5/§7/§8/§9、`README.md`、`AGENT_HARNESS_DESIGN.md` §8.7/§8.8、`docs/M6_CONTRACTS.md` §12 | M7 计划、决策点、工具面与导入路径的交叉引用 |

## 2. 已确认的决策

| 决策点 | 结论 |
|---|---|
| Season 形状 | 一项目多季；季只保存标题与有序 Episode ID，不复制角色/风格，不拥有成片或预算；一集至多属于一个季的当前版本 |
| 批量队列 | 一次 `season_generate` 覆盖 1–3 集，逐集原子登记；单集失败隔离；恢复必须显式 `resumeBatchId` 并创建新批次；不自动重跑 |
| 批量上限 | 1–3 集、60 Job、180.00 CNY；逐集 20 Job / 60.00 CNY 不变；不换汇；失败与 unknown 继续占额度 |
| 时间线 | 每集一条只增不改的 `Timeline`；版本冻结有序 clip（含 in/out 裁剪）、转场点、整集字幕轨、音轨、标题卡 |
| 时间线渲染 | 复用 M4 归一化 + M6 本地管线，新增 `ffmpeg-render-timeline-v1`；30 fps 网格、±1 帧容差、同指纹幂等 |
| 工作台 | Web 优先；季面板、批量确认/恢复、时间线编辑、预览与导出；预览是客户端顺序播放，导出才是交付口径 |
| 并发/限额 | 每季至多一个 `running` 批次；供应商提交仍由既有 Job Worker 串行约束；批次等待按集分段（每段 ≤20 个 Job） |
| 素材导入 | BGM/SFX 由客户端受控上传（Web/CLI），必须 `license`/`sourceUrl`；不是模型工具，不引入第三方音乐 API |
| 付费样本 | E0–E3 submit=0、模型调用=0、TTS 请求=0；E4 只复用已有本地媒体，新增样本须单独授权 |

## 3. 验证结果

### 3.1 产品代码未变

```text
git status --short
 M AGENT_HARNESS_DESIGN.md
 M README.md
 M docs/M5_PLAN.md
 M docs/M6_CONTRACTS.md
?? docs/M7_CONTRACTS.md
?? docs/M7_E0_BASELINE.json
?? scripts/capture_m7_baseline.py

git diff --stat src web tests   # 空
```

`ruff check src/vagent tests scripts` 全部通过；`ruff format --check` **198** 个文件已格式化（M6 E4 为 197，本包新增基线脚本）。前端六个状态文件 **51 passed**（无前端改动）。全量回归：**1342 passed / 3 skipped**（1345 collected；3 项 skip 为既有的 Windows 符号链接权限用例），耗时 **1064.12 秒（0:17:44）**。

### 3.2 基线快照

| 项目 | 值 |
|---|---|
| 契约模型数 | **165**（`vagent.storage` 25、`video.contracts` 74、`video.films` 25、`video.episodes` 13、`video.storyboards` 7、`video.renders` 6、`video.audio` 8、`video.subtitles` 4、`video.tts` 3） |
| `DatabaseV8`（schema 8） | `0cdb3175299a4bdbaeb176bbf4c4aa1276570960e93ad2d27dfcf2b9083a40cd` |
| `Episode` | `115f9efb3e7f8906bf75dabb793a663e87c3cc31e4a167130ceaf76246129b00` |
| `RenderTask` | `29a1c5353b43e961b58bc11bf4cdad1f0a02b67ce9b1c90d7403507e70d07074` |
| `FilmAssembly` | `5635fcbdfad4a890864d675ba38baace26d842e5c5690d9ef8792a700bcfc6f0` |
| `RenderSources` | `3c881d7a0664bc8821a33d6f902482900a3e0197e001f01aacf4cae0e8310b5c` |
| 存储版本 | **8**；E1 新建/迁移为 **9**（只加 `seasons`/`batches`/`timelines`） |
| 工具面 | **13/13**（mock 与 live）；M2/M3/M5/M5E2/M5E3/M6E1/M6E2/M6E3 分组字节固定 |
| 集级上限 | 60–90 秒、10–20 镜头、每场 ≤6 镜头、12 场、20 Job、60.00 CNY、镜头 2–15 秒 |
| 成片/渲染上限 | 输入 64、版本 64、拼接超时 120 秒、渲染单次 120 秒/整次 600 秒、成片 ≤90 秒、标题卡 ≤4、转场 0.1–1.0 秒、音轨 ≤64 |
| 等待 | 集合成员 **20**、超时 600 秒（M7 不修改；批次按集分段等待） |

E1 只允许新增 v9 模型与三个空集合；既有 165 个 `schemaSha256`、全部上限、工具名分组与规则字节必须逐字节不变。E3 才允许契约 §2.4 的四类追加（`ffmpeg-render-timeline-v1`、`RenderSources.timeline`、`RenderTask.timeline*`、工具输入 schema），并须在 E3 验收中逐项列出嵌套哈希变化。

### 3.3 本地工具链事实

包内 `imageio-ffmpeg==0.6.0` 提供 **FFmpeg 7.1-essentials**（gyan.dev 构建）：

| 项目 | 值 |
|---|---|
| 版本行 | `ffmpeg version 7.1-essentials_build-www.gyan.dev` |
| 二进制 SHA-256 | `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3` |
| 编译配置 SHA-256 | `df7f1233a38e9bb70a77ab7d4b38e27c4eaea073ac04147471810442a99ff44c` |
| `ffprobe` | **不存在**（测量走 FFmpeg 输出 + 现有 `mp4.py` 解析） |

裁剪探针（合成输入，不涉及真实素材）：

| 探针 | 结果 |
|---|---|
| 合成源 | 320×180、30 fps、2.0 秒、H.264 + AAC、81,276 字节 |
| 起裁 0.5 秒、裁 1.0 秒并重编码 | 返回 0；40,863 字节；SHA-256 `935ac9628237a0687d427bc0e61a9bd152816d7e36ff1de262f49102819a3609`；解析时长 **1.0 秒**、30 fps、H.264 + AAC |
| 时长误差 | **0.0 秒**（1.0 − 1.0） |
| 告警/错误 | 无（stderr 中无 error/warning/non-monotonic） |

结论：M7 的时间线裁剪**不需要新增二进制依赖**；`-ss/-t` + 重编码能落在 30 fps 网格，M4 归一化与 M6 渲染管线可直接复用。探针只证明本机包内构建的裁剪行为，不证明所有平台、浏览器或真实素材的帧级一致。

### 3.4 可复现

```powershell
.\.venv\Scripts\python.exe scripts/capture_m7_baseline.py
# 再运行一次，docs/M7_E0_BASELINE.json 的 SHA-256 与首次相同
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
```

本次已连续运行两次，输出 JSON 的 SHA-256 一致（确定性）；裁剪探针的 SHA、字节数与时长在两次运行中一致。

### 3.5 无付费、无模型

- 基线脚本只做本地 schema 哈希与合成 FFmpeg 探针，写入 `submit=0`、`modelCalls=0`、`providerRequests=0`。
- 本包未读取凭据、未访问供应商、未提交 Job、未调用文本模型或 TTS；没有新增费用，实际账单仍为 `unknown`。
- 全量回归与前端测试均使用本地夹具/受控路径。

## 4. 设计边界与决定

1. **等待上限保持 20，不随批量扩大到 60。** 赛季批次最多 60 个 Job，但 `WaitBindingV3` 的成员上限是已冻结的 20；M7 选择按集分段 `await_job`（每段 ≤20），避免修改等待 schema 与历史绑定。批次本身不创建等待绑定。
2. **BGM/SFX 导入不是模型工具。** 时间线音轨需要素材，但把本地路径暴露给模型不安全；M7 冻结为 Web/CLI 受控上传，复用 M6 已冻结的 `ImportedAudioSource`（`title/sourceUrl/license`），模型只看到 `audioId`。因此工具面仍是 18/18，不新增 `audio_import`。
3. **批次登记与交付分开。** `BatchEpisode.status` 只记录登记结果；交付失败由 `season_read` 从 Job/ShotVersion 计算，修复走既有 `shot_redo`，不通过批次恢复重投。这样批次终态稳定，不会因异步交付反复改写。
4. **时间线只增不改。** 编辑产生新 `TimelineVersion`，渲染产生新 `FilmVersion`；旧版本、旧成片与旧媒体都不删除、不降级；同指纹重复渲染命中旧成片。
5. **时间线过期必须重存。** 时间线版本冻结 `episodeVersion`；分集版本变化后 `stale=true`，渲染返回 `TIMELINE_SOURCE_MISSING`，不做静默重映射。
6. **预览不是交付物。** Web 预览按 clip 顺序播放源镜头并应用裁剪偏移，不做服务端合成、不承诺字幕/混音帧级同步；导出成片才是验收口径。
7. **schema 9 只是方向。** E0 不实现 `seasons`/`batches`/`timelines`，不注册工具；E1 才建立 schema 9 与 Season 域。

## 5. 明确未做与边界

- **schema 9 未实现**：三个集合、`Season`/`Batch`/`Timeline` 模型、迁移与校验器属 E1–E3。
- **批量队列未实现**：`season_generate`、批次记录、失败隔离与恢复属 E2；当前只有单集 `episode_generate`。
- **时间线未实现**：`timeline_save`/`timeline_read`、`film_render` 的 timeline scope、裁剪/重排/字幕重映射属 E3。
- **Web 工作台未实现**：季面板、时间线编辑器与预览属 E1–E3；M6 的分集面板保持现状。
- **9:16 竖屏、跨集超级成片、多轨混音工作站、关键帧/变速/特效、自动后台批处理守护进程**：不在 M7。
- **真实付费样本**：E0 未提交；E4 如需新增样本，必须单独批准模型、次数与金额。
- 裁剪探针只证明本地 FFmpeg 行为，不证明真实素材、浏览器解码或平台兼容性。

## 6. E1 入口

E1 的第一件事是按 [M7 契约](./M7_CONTRACTS.md) §9 建立 **schema 9**：

1. 新增 `DatabaseV9` 与 `seasons`/`batches`/`timelines` 三个空集合；`v8→v9` 逐字节备份，v1–v8 全部可读；Store 打开与每次提交校验新集合。
2. 新增 `Season`/`SeasonVersion` 与 `season_save`/`season_read`；季只保存标题与有序 Episode ID，一集至多属于一个季的当前版本；不登记 Job、不调用模型。
3. 工具面升到 **15/15**（冻结 13/13）；规则字节与 13/13 变体逐字节保留。
4. 基线比对：165 个既有模型哈希、全部上限、工具名分组逐字节不变；只新增 v9 模型/集合。任何其他差异必须在 E1 验收中解释，否则视为回归。
5. CLI/Web 季面板只读写季元数据；批量与时间线仍不可用。**E1 不发视频、不调模型。**

## 7. 复现命令

```powershell
.\.venv\Scripts\python.exe scripts/capture_m7_baseline.py
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs tests/test_episode_state.mjs tests/test_subtitle_state.mjs tests/test_render_state.mjs
```
