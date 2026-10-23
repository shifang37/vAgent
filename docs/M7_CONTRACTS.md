# M7 契约：多集与工作台（Season、批量队列、时间线）

冻结日期：**2026-10-23（E0）**。基线 `master` `5cfd754`（M6 E4 已合入；schema 8、tools/rules 13/13、冻结 11/11…1/1）。范围见 [M5 计划](./M5_PLAN.md) §5 的 M7，上游语义见 [M5 契约](./M5_CONTRACTS.md)、[M6 契约](./M6_CONTRACTS.md) 与 [M4 归一化](./M4_NORMALIZATION.md)，基线证据见 [E0 验收](./M7_E0_ACCEPTANCE.md) 与 `docs/M7_E0_BASELINE.json`。

本文规定 E1–E4 的实现要求。**E0 不改变产品代码（`src/`、`web/`、`tests/`）、不注册工具、不调用模型或供应商：视频 submit=0、文本模型调用=0、TTS 请求=0。** 本轮**不做 9:16 竖屏**，画幅仍为 16:9。

## 1. 决策与版本

E0 前的 9 个决策点已确认，冻结如下：

| 决策 | 冻结结果 |
|---|---|
| Season 形状 | **一个项目可有多个季**；季只保存有序成员关系（Episode ID）与标题，不拥有成片、不复制角色/风格；角色/风格仍是项目级共享圣经 |
| 集与季 | 每集仍是独立的版本、场记、分镜、预算与成片；**一集至多属于一个季的当前版本**；旧季版本保留成员历史，但同一集的当前归属唯一 |
| 批量队列 | 一次 `season_generate` 覆盖 **1–3 集**，逐集原子登记；单集失败只标记该集，其他集继续；恢复必须显式 `resumeBatchId`，不自动重跑、不重复提交 |
| 时间线 | 每集一条 `Timeline`，版本只增不改；一条版本冻结有序镜头（含裁剪 in/out）、转场点、字幕轨、音轨、标题卡；渲染只读某一确定版本 |
| 渲染 | 时间线复用 **M4 归一化 + M6 `ffmpeg-render-v1`** 本地管线；新增装配字面量 `ffmpeg-render-timeline-v1`，不回写旧字面量；同指纹幂等 |
| 工作台 | **Web 优先**：季面板、批量确认/失败隔离/恢复、时间线编辑、预览与导出；预览是客户端顺序播放，**不承诺帧级准确**；导出才是交付物 |
| 并发/限额 | 每季同时至多一个未完成批次；供应商提交仍由既有 Job Worker 串行约束（每 provider 至多一个未完成提交）；批量上限 **60 Job / 180.00 CNY**，逐集 20 Job / 60.00 CNY 不变 |
| 素材导入 | BGM/SFX 由**客户端动作**（Web 上传/CLI 路径）导入，必须提供 `license`/`sourceUrl`；不做模型工具、不引入第三方音乐 API；模型只引用 `audioId` |
| 付费样本 | E0–E3 全程 submit=0、模型调用=0、TTS 请求=0；E4 只复用已有本地媒体，任何新增付费样本须单独授权 |

| 项目 | 冻结结果 |
|---|---|
| 存储 | E1 新建与迁移为 schema **9**；v8→v9 只加三个空集合 `seasons`/`batches`/`timelines`；逐字节备份，v1–v8 全部可读 |
| 工具面 | **E0 保持 13/13**；E1 追加 `season_save`/`season_read` → **15/15**；E2 追加 `season_generate` → **16/16**；E3 追加 `timeline_save`/`timeline_read` → **18/18**；13/13、11/11…1/1 与 15/15、16/16 冻结变体全部保留，按 `toolFeatures` 恢复 |
| 恢复 | 继续按完整 `toolFeatures` 身份选择冻结注册表；新版本不得改写旧 prompt/规则字节，未知组合返回 `RESUME_CONFIG_CHANGED` |
| execution/context/checkpoint | 保持 **3 / 2 / 1**；批次不引入第二套等待状态机，复用现有集合等待 |
| 集合等待 | 保持 **20 成员 / 600 秒** 不变；整季批次按集分段等待（每个 `await_job` ≤20 个 Job），不改变任何等待 schema |
| 视频 Job | 沿用 **v6** 通用供应商请求；M7 不新增视频 Job 契约版本，不改变视频预算桶 |
| 预算 | 集级 20 Job / 60.00 CNY 不变；批量级 **60 Job / 180.00 CNY**；只按 CNY 记账、不换汇；失败、`unknown` 与已登记未完成的 Job 继续占额度 |
| 批量记录 | 每次 `season_generate` 创建一条 `Batch`；恢复创建新 `Batch` 并以 `resumeOf` 指向旧批次；批次终态后不可改写 |
| 时间线预览 | 客户端顺序播放 + 裁剪偏移，只用于编辑预览；服务端不生成预览成片，不承诺音画帧级同步 |
| 明确排除 | 9:16 竖屏、多轨混音工作站、关键帧/变速/特效、自动批处理守护进程、可执行插件与 PyPI 发布 |

## 2. 数据对象

新增对象只增不改：根记录保留 `currentVersion` 与连续 `versions[]`，历史版本、已冻结的预算与已绑定的引用不可重写或删除。ID 由服务端生成（canonical UUID），用户与模型不能指定文件路径。

### 2.1 Season

| 对象 | 必填内容 |
|---|---|
| `Season` | `id, projectId, currentVersion, versions[], revision, createdAt` |
| `SeasonVersion` | `version, title, episodeIds[], createdAt`；`episodeIds` 为 1–4 个有序唯一 Episode ID |

- 季成员关系只存在于 `SeasonVersion.episodeIds`；**Episode 不新增反向 `seasonId`**，避免双重事实源。`season_read` 从季版本反查。
- 新增/改名/重排/增删集都追加新的 `SeasonVersion`；旧版本逐字节保留。
- 一集至多属于一个季的**当前**版本；换季需要先在原季的新版本中移除，再加入目标季，旧历史仍可读。
- 每项目季数 ≤ **8**，每季 1–4 集；`season_save` 不登记 Job、不调用模型。

### 2.2 Batch

| 对象 | 必填内容 |
|---|---|
| `Batch` | `id, projectId, seasonId, seasonVersion, episodeIds[], status, episodes[], resumeOf?, fingerprint, createdAt, updatedAt, revision` |
| `BatchEpisode` | `episodeId, episodeVersion, status, shotCount, jobIds[], reusedCount, estimate?, errorCode?, startedAt?, finishedAt?` |

- `Batch.status ∈ {running, succeeded, partial, failed}`；`BatchEpisode.status ∈ {pending, running, succeeded, failed}`。
- `Batch.episodeIds` 为 1–3 个该季当前版本的 Episode ID；`episodes[]` 与之一一对应且顺序相同。
- `episodeVersion` 在批次创建时冻结；若登记/恢复时该集当前版本已变化，该集失败（`BATCH_CONFLICT`），其他集继续。
- 顶层状态由成员推导：任一 `pending/running` → `running`；全部 `succeeded` → `succeeded`；有成功也有失败 → `partial`；全部失败 → `failed`。
- `jobIds` 最多 20 个；`reusedCount` 为复用的 ShotVersion 数；`estimate` 只记录本集新增任务的估算合计。
- 批次指纹 = `sha256`（`projectId`、`seasonId`、`seasonVersion`、有序 `episodeIds`、每集冻结的 `episodeVersion`、模式与请求参数），不含时间、调用 ID 或 `resumeOf`；用于调用重放与审计。
- 批次记录是可变进度（类似 `RenderTask`）：目标、成员、`createdAt` 不可改，只能推进状态与 `revision`；终态后不可改写。
- **登记状态与交付状态分开**：`BatchEpisode.status` 只表示登记结果；`season_read` 另从 Job/ShotVersion 计算 `deliveredShots/failedShots/pendingShots`，不改写批次。交付失败用既有 `shot_redo`/`episode_generate` 修复，不通过批次恢复重投。
- 每项目批次 ≤ **64** 条；恢复只创建新批次，不删除旧批次。

### 2.3 Timeline

| 对象 | 必填内容 |
|---|---|
| `Timeline` | `id, projectId, episodeId, currentVersion, versions[], revision, createdAt`；每集恰好一条 |
| `TimelineVersion` | `version, episodeVersion, contract, clips[], transitions[], subtitle?, audio[], titleCards[], fingerprint, createdAt` |
| `TimelineClip` | `shotId, shotVersionId, storyboardId, sceneId, inMs, outMs` |
| `TimelineTransition` | `afterShotId, type ∈ {cut, fade, crossfade}, durationMs` |
| `TimelineSubtitle` | `trackId, trackVersion` |
| `TimelineAudioTrack` | `audioId, audioVersion, kind ∈ {voice, bgm, sfx}, gainDb, startMs` |
| `TimelineTitleCard` | `text, startMs, durationMs, styleProfile="title-zh-v1"` |

- `contract` 固定 `timeline-zh-v1`；`episodeVersion` 冻结编辑所依据的分集版本；每集时间线版本 ≤ **64**。
- `clips` 1–20 条；同一 `shotId` 在一条版本内至多出现一次；`0 ≤ inMs < outMs ≤ 镜头时长 × 1000`，且 `outMs - inMs ≥ 1000`；单条裁剪时长不超过镜头原时长。
- `transitions` 至多 19 条；`afterShotId` 必须是当前版本中的镜头且不能是最后一条；`cut` 不记录；`fade/crossfade` 为 100–1000 ms，且不超过相邻任一裁剪后镜头时长的 1/3；总转场时长小于总时长。
- `subtitle` 可选；提供时必须是本集的 `scope=episode` 字幕轨版本（分镜字幕轨不能直接用于重排后的整集时间线）。**提供 `voice` 音轨时必须同时提供字幕轨**，否则 `SUBTITLE_TRACK_MISSING`。
- `audio` 至多 64 条，按 `kind` 受 M6 上限（voice 40 / bgm 8 / sfx 16）约束；voice 的 `startMs` 固定为 0（由字幕 cue 反推），BGM/SFX 的 `startMs` 落在有效时间线内；`gainDb ∈ [-60, +12]`。
- **素材导入是客户端动作，不是模型工具**：Web 上传或 CLI 导入本地 WAV/M4A（≤25 MB、≤60 秒），必须填 `license` 与 `sourceUrl`；服务端校验 RIFF/PCM 或 MP4 音频、按 SHA-256 去重、写 `AudioMedia`/`AudioAsset`，只把 `audioId` 返回模型，不暴露本地路径。第三方音乐 API 不接入。
- `titleCards` 至多 4 条，按有效时间线校验不越界、不重叠；文字与风格沿用 M6 `title-zh-v1`。
- 有效时长 = Σ 裁剪时长 − Σ 转场时长，必须 **1.000–90.000 秒**；这也是 `film_render` 的成片上限。
- 指纹 = `sha256`（规范化 JSON：`contract`、`episodeId`、`episodeVersion`、有序 `clips`、`transitions`、`subtitle`、`audio`、`titleCards`），不含版本号、时间与调用 ID；同一内容重放返回同一版本。

**时间线跟随时集版本，而不是替代它。** 时间线版本不复制镜头描述、预算或媒体；它只引用 `(storyboardId, shotVersionId, shotId, sceneId)`。分集版本变化后，旧时间线被标记为 `stale=true`，渲染返回 `TIMELINE_SOURCE_MISSING`，必须先保存新版本。

### 2.4 E3 允许追加的渲染字段（届时逐项列出）

时间线渲染需要把来源写入成片版本，E3 才追加以下**可选**字段；E1 不得提前改动既有模型：

| 模型 | 追加字段 | 说明 |
|---|---|---|
| `FilmAssembly.assembly_version` | 新增字面量 `ffmpeg-render-timeline-v1` | 旧 `ffmpeg-render-v1`/`ffmpeg-concat-*` 不变 |
| `RenderSources` | `timeline?: {timelineId, timelineVersion, timelineSha256}` | 逐场/整集渲染保持 `null` |
| `RenderTask` | `timelineId?`、`timelineVersion?` | `scope=timeline` 时两者必填，`episodeId` 同时指向该集 |
| `FilmRenderArgs`/`RenderReadArgs` | `scope` 增加 `timeline`、`timelineId/timelineVersion` | 工具输入 schema；旧 13/13 变体保留旧 schema |

## 3. 批量队列（`season_generate`）

`season_generate` 不是后台守护进程；它是一次**显式确认的批量登记调用**，把选中集逐集交给现有 `episode_generate` 语义。真正的提交、查询、下载与限额仍由既有 Job/媒体 Worker 负责。

1. **确认与目标**：必须 `confirm=true`；`seasonId` 必填；可选 `episodeIds[]`（缺省为季当前版本的全部集；若季有 4 集，缺省值直接 `BATCH_LIMIT`，必须显式选择至多 3 集）。`resumeBatchId` 可选，用于恢复，**与 `episodeIds` 互斥**。调用前回显逐集镜头数、逐镜头/合计估算、集级与批量级余量。
2. **批次创建**：在锁内创建 `Batch(status=running, revision=0)`，冻结每集当前 `episodeVersion` 与请求指纹；若该季已有 `running` 批次，返回 `BATCH_CONFLICT`，不创建第二个。
3. **逐集登记**：按 `episodeIds` 顺序，每集使用独立的 Store 操作（键 `batch:<batchId>:<episodeId>`）调用现有整集登记逻辑；该集的所有 Job 与 ShotVersion 在同一事务内全有或全无。任一集失败只把该集标为 `failed` 并写入错误码，继续下一集。
4. **复用与不重提**：同一分镜版本同一镜头的相同请求复用已有 ShotVersion/Job；已进入不确定窗口的提交不会因批次失败或恢复而重投。恢复批次只用 `episode_generate` 的复用路径，不创建 `shot_redo`。
5. **批量上限**：一次批次最多 3 集、60 个新 Job、180.00 CNY；预检超限整批拒绝（`BATCH_LIMIT`），不登记任何集。逐集提交时仍按该集冻结的 20 Job / 60.00 CNY 复核；某集超限只失败该集。
6. **失败隔离与恢复**：`Batch` 终态为 `succeeded`/`partial`/`failed`；`partial` 表示至少一集成功、至少一集失败。恢复必须显式传 `resumeBatchId`，只选择旧批次中非 `succeeded` 的集，按恢复时各集**当前版本**重新冻结并创建 `resumeOf` 新批次，重新确认；不复用旧批次的 `running` 状态，也不改写旧批次。进程崩溃后，打开 Store 时把 `running` 批次改为 `partial`（若已有成功集）或 `failed`，把所有 `pending/running` 成员集改为 `failed`（`BATCH_INTERRUPTED`），等显式恢复；不自动重跑。
7. **并发与限流**：每季至多一个 `running` 批次；供应商提交仍受既有「每 provider 至多一个未完成提交」约束；429、5xx 与不确定提交沿用 Job 状态机，不在批次层重试或并行绕过。
8. **读取**：`season_read` 返回季、当前成员集摘要、最近批次（或指定 `batchId`）、逐集登记状态、逐集交付计数（`deliveredShots/failedShots/pendingShots`）、Job ID、估算与错误码；只读，不登记、不提交。
9. **等待**：批次不创建新的等待绑定；调用方按集分段调用 `await_job`（每个集合 ≤20 个 Job，沿用 M5 E3 的 20 成员上限与 600 秒超时），某集等待到期不影响其他集或已登记 Job。

## 4. 时间线（`timeline-zh-v1`）

时间线把已保存的镜头版本重新排序、裁剪，并绑定字幕/音轨/转场/标题卡；它不重新生成镜头。

1. **来源校验**：每条 clip 的 `shotId` 必须出现在该集当前版本某个场记绑定的分镜中；`storyboardId/sceneId` 与引用一致；`shotVersionId` 必须是该分镜已存在的 ShotVersion 且 `shotId` 匹配。镜头尚未交付也可以保存时间线（用于排期），但渲染会拒绝。
2. **顺序与裁剪**：`clips` 顺序即时间线顺序；可跨场重排，但不得重复同一镜头；裁剪以整数毫秒输入，服务端以 30 fps 网格在渲染时对齐（见 §5），保存时只做范围与最小时长校验。
3. **转场**：与 M6 相同，只裁掉出镜镜头尾部；`cut` 不占时长；转场不得跨出时间线或吃掉整个镜头。
4. **字幕**：引用整集字幕轨；渲染时按 `timeline-subtitle-remap-v1` 重映射（§5.3）。保存时不改写字幕轨；旧字幕版本保留。
5. **音轨**：voice 轨按重映射后的 cue 定位；BGM/SFX 按 `startMs` 放置；响度与 ducking 沿用 `mix-zh-v1`。
6. **追加与并发**：`timeline_save` 用 `expectedRevision` 做乐观并发（创建时可省略，更新时必填；缺失或过期返回 `TIMELINE_VERSION_CONFLICT`）；版本号连续递增，`currentVersion` 只前进。相同指纹返回当前版本并标记 `replayed=true`，不追加空版本。
7. **读取**：`timeline_read` 返回当前/指定版本、每条 clip 的源时长、裁剪、交付状态、字幕/音轨/标题卡引用与指纹；`stale` 表示 `episodeVersion` 已不是当前分集版本。

## 5. 渲染与导出（`ffmpeg-render-v1` + `ffmpeg-render-timeline-v1`）

时间线渲染只读冻结版本，全程本地执行，不产生供应商费用。

### 5.1 入口

`film_render` 追加 `scope=timeline`：

| 参数 | 规则 |
|---|---|
| `scope=timeline` | 必须提供 `timelineId`，可选 `timelineVersion`（缺省为当前版本）；不得同时提供 `episodeId`/`storyboardId`/`trackId`/`trackVersion`/`transitions`/`titleCards` |
| `profile` | 沿用 `720p-h264-aac-v1` / `1080p-h264-aac-v1`；1080P 仍只是参数化上采样 |
| `subtitleMode` | 沿用 `soft/burn/both`；时间线无字幕轨时只允许 `soft`，否则 `SUBTITLE_TRACK_MISSING` |
| `confirm` | 必填；本地渲染同样写盘 |
| `render_read` | 可选 `timelineId`；也继续支持 `renderId`/`filmId`/`episodeId`/`storyboardId` |

### 5.2 冻结与执行

1. 锁内取快照：分集、时间线版本、每条 clip 的 ShotVersion、媒体、字幕轨、音轨与标题卡；复核 `timeline.episodeVersion == episode.currentVersion`、clip 归属、媒体已交付与哈希。
2. 锁外逐 clip 归一化到 profile 基准；按 `inMs/outMs` 裁剪（`-ss` + `-t`），并吸附到 30 fps 帧网格；接缝与转场按 M6 `xfade`/`fade` 规则组装，总时长为有效时间线。
3. 字幕按 §5.3 重映射；voice 按重映射 cue 定位，BGM/SFX 按时间线 start 放置；两遍 `loudnorm` 与 EBU R128 复测沿用 `mix-zh-v1`。
4. 编码 MP4/H.264/AAC、`faststart`；校验时长与有效时间线相差 ≤ 1 帧、音画首帧对齐、字幕轨存在性、响度容差、MP4 结构与编码参数、SHA/大小。
5. 锁内复核源哈希后一次提交：新 `FilmVersion`（`episodeVersion=timeline.episodeVersion`、`assemblyVersion=ffmpeg-render-timeline-v1`、`render.sources.timeline`）与 `FilmMedia`、`RenderTask`、Operation。旧成片版本与旧时间线版本不删除、不降级。
6. 任一 clip 的源缺失/未交付/哈希不符，整次渲染在编码前返回 `TIMELINE_SOURCE_MISSING`/`RENDER_SOURCE_MISSING`，不提交成片；不做“跳过缺失镜头”的静默降级。

### 5.3 字幕与音频重映射（`timeline-subtitle-remap-v1`）

- 每条 cue 必须带 `shotId`；缺失 `shotId` 返回 `SUBTITLE_CUE_CONFLICT`。cue 的镜头不在时间线中时，该 cue 被丢弃（不报错），对应的 voice 轨不参与混音；旁挂 SRT/ASS 只包含时间线内的 cue。
- cue 的源区间按所属 clip 的 `[inMs, outMs]` 夹取；新起点 = clip 的有效起点 + (`cue.start` − `inMs`)；超出裁剪窗口的部分截断；不足 1 ms 的 cue 丢弃。
- 同一镜头内的相对时序保持不变；跨镜头顺序随 clip 顺序；旁挂 SRT/ASS 与 `render.subtitle` 记录重映射后的 cue 与 `retimed=true`。
- voice 轨按重映射后的 cue 字符位置放置；被完全裁掉的 cue 对应的 voice 轨不参与混音（不报错）；BGM/SFX 仍按 `startMs` 放置。
- 标题卡按有效时间线校验；最多 4 条，不重叠、不越界。

### 5.4 幂等与版本

- 渲染指纹 = `sha256`（时间线指纹、`timelineVersion`、`profile`、`subtitleMode`、引擎指纹、字体指纹、音频/字幕解析结果）；同一指纹重复请求返回已成片版本，不重复编码。
- 时间线任何编辑都追加新 `TimelineVersion`；再次渲染产生新 `FilmVersion`，旧版本保留。时间线编辑**不会**改写任何既有 `FilmVersion`。
- 失败任务记录 `failureStage`，不提交成片；`render_read` 可读逐 storyboard 进度、失败阶段与错误码。

## 6. 工具面、规则与费用

| 工具 | 引入 | 输入要点 | 输出 | 幂等 | 费用 |
|---|---|---|---|---|---|
| `season_save` | E1 | 季 ID（可选）+ 标题 + 有序 `episodeIds` + `expectedRevision` | 新 `SeasonVersion` | 相同内容返回同版本 | 无 |
| `season_read` | E1 | 季 ID/项目；可选 `batchId` | 季、成员集摘要、最近批次、逐集状态 | 只读 | 无 |
| `season_generate` | E2 | 季 ID + 可选 `episodeIds` + `resumeBatchId` + **显式 `confirm`** | 新 `Batch` 与逐集 Job/估算 | 同请求复用 Job；恢复不重投 | 视频供应商费用 |
| `timeline_save` | E3 | 集 ID + clips/transitions/subtitle/audio/titleCards + `expectedRevision` | 新 `TimelineVersion` 与校验结果 | 相同指纹返回同版本 | 无 |
| `timeline_read` | E3 | `timelineId` 或 `episodeId`；可选 `version` | 版本、clip 交付状态、指纹、`stale` | 只读 | 无 |
| `film_render` | E3 扩展 | `scope=timeline` + 时间线版本 + profile/mode + **显式 `confirm`** | 渲染任务与预估耗时 | 同指纹返回原成片 | 无（本地） |

BGM/SFX 导入不注册为模型工具：它是 Web/CLI 的受控上传动作，模型只看到 `audioId`、时长、来源与许可；M6 已冻结的 `ImportedAudioSource` 字段（`title/sourceUrl/license`）直接复用。这样既补齐时间线音轨，又不把本地文件系统暴露给模型。

规则文本冻结为 `m7-rules-v1`，在各阶段只追加新工具说明；旧变体的 prompt/规则字节不得改写。工具面与规则版本：

| 阶段 | tools/rules | 新增/变化 |
|---|---|---|
| E0 | 13/13（不变） | 仅契约与基线 |
| E1 | **15/15** | `season_save`、`season_read`；schema 9 |
| E2 | **16/16** | `season_generate` |
| E3 | **18/18** | `timeline_save`、`timeline_read`；`film_render` 增加 `timeline` scope 与时间线字段 |
| E4 | 18/18（不变） | 只做验收与修复 |

**费用口径**：视频与 TTS 分桶，不换汇、不互相抵扣；批次与逐集都在登记事务内复核额度；`unknown` 价格只报字符数/次数并明确标注；任何付费请求都必须显式确认、回显估算，不自动重提。headless 仍只编排分镜，不加入 `season_generate` 或时间线渲染。

## 7. 错误码

M6 的 18 个错误码全部保留；M7 新增：

| 错误码 | 触发 |
|---|---|
| `SEASON_LIMIT` | 季数超过 8、成员为空或超过 4 集 |
| `SEASON_NOT_FOUND` | 季不存在或不属于当前项目 |
| `SEASON_EPISODE_CONFLICT` | 集不存在/跨项目、重复、或已属于另一个季的当前版本 |
| `SEASON_VERSION_CONFLICT` | `expectedRevision` 过期或季版本不连续 |
| `BATCH_EMPTY` | 未选中任何集；或某目标集没有可登记镜头（只该集失败，其他集继续） |
| `BATCH_LIMIT` | 批次超过 3 集、60 Job 或 180.00 CNY |
| `BATCH_CONFLICT` | 该季已有 `running` 批次、批次不存在/跨项目/不可恢复、或成员集版本已变化 |
| `BATCH_INTERRUPTED` | 进程中断后批次被恢复为终态；成员集需显式恢复，不自动重跑 |
| `TIMELINE_INVALID` | clip 重复/缺失/顺序或引用非法、转场非法、字幕轨不匹配、标题卡冲突 |
| `TIMELINE_NOT_FOUND` | 时间线或指定版本不存在、或不属于当前项目 |
| `TIMELINE_TRIM_INVALID` | `inMs/outMs` 越界、倒序或裁剪不足 1 秒 |
| `TIMELINE_SOURCE_MISSING` | 时间线过期、镜头/媒体/字幕/音轨未交付或已删除 |
| `TIMELINE_VERSION_CONFLICT` | 时间线 `expectedRevision` 过期、版本不存在或目标不唯一 |

复用：`CONFIRMATION_REQUIRED`、`EPISODE_COST_LIMIT`、`VERSION_CONFLICT`、`SCENE_CONFLICT`、`RENDER_*`、`SUBTITLE_*`、`AUDIO_*`、`FONT_MISSING`、`TRANSITION_INVALID`、`TITLE_CARD_LIMIT`、`LOUDNESS_OUT_OF_RANGE`。批次内单集失败时，错误码写入该 `BatchEpisode.errorCode`，不把整批伪装成成功。

## 8. 确定性与离线

- **零模型/零供应商**：E0–E3 的离线套件、安装态与 CI 全部使用 mock/受控 HTTP、本地测试媒体、`local-tts-v1` 与包内 FFmpeg；真实供应商调用数为 0。
- **时间线确定性**：`timeline-zh-v1` 只依赖输入字节与有理数毫秒；无时钟、随机、文件系统顺序或浮点累加依赖；相同输入得到相同指纹与版本。
- **裁剪容差**：渲染时按 30 fps 吸附，输出时长与有效时间线允许 **±1 帧**；成片记录 `ffmpeg-render-timeline-v1`、引擎指纹与 profile，不承诺跨构建逐字节一致。
- **预览不是交付物**：Web 预览按 clip 顺序播放源镜头并应用裁剪偏移，不做服务端合成；字幕叠加与音轨混音以导出为准。
- **离线可复现**：同一数据目录重放相同时间线版本与参数必须命中同一成片版本（幂等），不重新编码。
- **基线保护**：`docs/M7_E0_BASELINE.json` 的 165 个模型哈希与冻结上限是 E1 的比对基准；E1 只允许新增 v9 模型/集合，E3 只允许 §2.4 的四类追加，其余逐字节不变。

## 9. 工作包与验收

| 包 | 交付 | 验收 |
|---|---|---|
| E1 季与 schema 9 | **待实施**：schema v9（`seasons`/`batches`/`timelines` 三个空集合）、`Season`/`SeasonVersion`、`season_save`/`season_read`、v8→v9 逐字节备份、CLI/Web 季面板；tools/rules **15/15**（冻结 13/13） | 旧 v1–v8 数据可读；季成员唯一、顺序只增不改；无 Job、无模型、submit=0 |
| E2 批量队列 | **待实施**：`Batch`/`BatchEpisode`、`season_generate`（1–3 集、逐集原子、失败隔离、显式恢复、60 Job/180 CNY）、Store 崩溃恢复、Web 批量确认与恢复；tools/rules **16/16**（冻结 15/15） | 3 集批次中单集失败不阻塞其他集；单集交付失败可由 `shot_redo` 修复且不影响其他集；恢复不重复提交；不确定提交不重投；受控/离线 submit=0 |
| E3 时间线工作台 | **待实施**：`Timeline`/`TimelineVersion`、`timeline_save`/`timeline_read`、`film_render` 的 `timeline` scope、`ffmpeg-render-timeline-v1`、字幕/音频重映射、客户端 BGM/SFX 导入、Web 裁剪/排序/转场/预览/导出；tools/rules **18/18**（冻结 16/16） | 重排/裁剪/转场后成片可播放且时长正确；字幕不漂移；旧版本保留；同指纹幂等；失败不提交 |
| E4 验收 | **待实施**：`scripts/evaluate_m7.py` 默认离线、3 集批次/失败隔离/恢复、时间线 v1→v2 重拼、浏览器工作台、安装态与 CI；复用已有本地媒体 | 3 集批量中单集失败不阻塞；恢复不重复提交；时间线编辑后重拼版本只增不改；浏览器可编辑/预览/导出；真实付费样本需单独授权 |

## 10. 明确不做

- 9:16 竖屏与竖屏供应商能力（单列后续）。
- 多轨混音工作站、关键帧、变速、复杂动效、专业 NLE 交互。
- 自动后台批处理守护进程、无人值守的付费重试、自动换供应商或降规格。
- 跨项目季、跨剧集时间线、跨集拼接为“超级成片”。
- 去水印、1080P 超分/画质增强、声音克隆。
- 可执行插件、插件市场、PyPI 公开发布。
