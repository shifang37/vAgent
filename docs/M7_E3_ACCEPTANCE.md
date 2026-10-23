# M7 E3 验收：时间线工作台

日期：**2026-10-23**。基线：`master` `9913de8`（M7 E2 批量队列与文档已合入；M7 E0 契约 = `dd3dfde`，M6 E4 = `5cfd754`）。范围见 [M7 契约](./M7_CONTRACTS.md) §2.3、§2.4、§4、§5 与 §9 的 E3，冻结决策见 §1。**E3 未读取凭据、未访问供应商、未提交任何视频或 TTS 请求：视频 submit=0、模型调用=0、TTS 请求=0。** 所有媒体来自本地测试 MP4、`local-tts-v1` 与包内 FFmpeg 7.1；时间线渲染全程本地执行。

## 1. 交付范围

| 交付 | 实现 |
|---|---|
| 时间线域 | `Timeline`/`TimelineVersion`/`TimelineClip`/`TimelineTransition`/`TimelineSubtitleRef`/`TimelineAudioTrack`/`TimelineTitleCard`；每集恰好一条、版本只增不改（≤64）、`timeline-zh-v1`、整数毫秒裁剪、确定性指纹 |
| 保存与读取 | `timeline_save`（episodeId + 有序 clips + transitions + subtitle + audio + titleCards + `expectedRevision`；相同内容重放、修改追加版本）、`timeline_read`（episodeId/timelineId + 可选版本；逐条裁剪、源时长、交付状态、转场、字幕/音轨、标题卡、指纹、`stale`） |
| 工具面 | tools/rules **18/18**（新增 `timeline_save`/`timeline_read`），冻结 **16/16** 视图；13/13、15/15 继续隐藏季/时间线工具；旧 prompt/规则字节保留 |
| 时间线渲染 | `film_render` 的 `scope=timeline`、`ffmpeg-render-timeline-v1`、`RenderSources.timeline`、`RenderTask.timelineId/timelineVersion`、`render_read` 按 `timelineId` 读取；按 `inMs/outMs` 本地裁剪、跨镜头/跨场重排、`cut/fade/crossfade`、标题卡、720P/1080P、soft/burn/both、同指纹幂等 |
| 字幕重映射 | `timeline-subtitle-remap-v1`：cue 先换算为镜头内偏移，再按 clip 入点与新有效起点重映射；时间线外的 cue 丢弃，裁剪窗口外夹取，旁挂 SRT/ASS 与 `render.subtitle.retimed=true` 同源重算 |
| 音频 | voice 按重映射 cue 定位（被完全裁掉的 cue 不参与混音）、BGM/SFX 按 `startMs/gainDb` 放置；时间线的 `audio[]` 是唯一混音清单；沿用 `mix-zh-v1` ducking/两遍 `loudnorm`/EBU R128 复测 |
| 客户端素材导入 | `POST /api/audio/import`（multipart）与 `vagent audio import`：WAV/MP4、≤25 MB、≤60 秒、强制 `license`/`sourceUrl`、按 SHA-256 去重、写 `AudioMedia`/`AudioAsset`，模型只看到 `audioId`；**不是模型工具** |
| CLI | `vagent timeline show|sources|save`、`vagent audio list|import`、`vagent film render --timeline [--timeline-version]` |
| HTTP | `GET /api/episodes/{id}/timeline`、`GET /api/timelines/{id}`、`POST /api/sessions/{id}/timelines/save`、`GET /api/audio`、`POST /api/audio/import`；`/api/renders` 与 `film_render` 支持 `timelineId/timelineVersion` |
| Web | 时间线工作台（左栏「时间线」或分集面板入口）：镜头排序/裁剪/移除、边界转场、字幕轨选择、音轨勾选与增益/起点、标题卡、客户端顺序预览、保存与导出；视频模式 off 下仍可用 |
| 文档 | 本文件、[M7 契约](./M7_CONTRACTS.md) 的 E3 实施细化、`README.md`、`M1_PLAN.md`、[M5 计划](./M5_PLAN.md)、[Harness 设计](../AGENT_HARNESS_DESIGN.md)、[Web 说明](../web/README.md) |

## 2. 契约实施细化（不改冻结语义）

1. **Store 打开不重解析镜头来源。** `validate_timeline_changes` 只校验模型、所有权、每集唯一、内容指纹与只增不改历史；不在 Store 校验中重读分集/场记/分镜。否则一次分集编辑或分镜改尺寸会让旧时间线把整库变为不可打开。读取仍返回 `stale=true`，渲染重新解析并返回 `TIMELINE_SOURCE_MISSING`。
2. **clip 引用已注册 `ShotVersion`。** 契约允许“镜头尚未交付也可以保存时间线（用于排期）”；因此时间线引用某镜头已登记的 `ShotVersion`（Job 未完成也可），源时长取该 `ShotVersion` 所属分镜版本的镜头时长。完全未登记的镜头无法进入时间线（`TIMELINE_INVALID`）。渲染要求对应媒体已交付。
3. **字幕源时间基准。** 契约公式把 `cue.start` 当作镜头内时间；实际冻结字幕 cue 使用整集时间线。实现先用 `SubtitleTrackVersion.timeline` 把 cue 换算为镜头内偏移（`cue.start − trackEntry.start`），再按 clip 入点映射，等价于“有效起点 + (局部 − inMs)”，第二镜之后不累积漂移。保存与渲染都要求字幕版本的 `episodeVersion` 与时间线一致；不一致时保存返回 `SUBTITLE_TRACK_MISSING`，渲染返回 `RENDER_TIMELINE_MISMATCH`。
4. **音轨以冻结清单为准。** `scope=timeline` 只混音 `TimelineVersion.audio` 中列出的资产；M6 的“全部已导入 BGM/SFX 自动混入”不适用于时间线。voice 的 `startMs` 固定 0（由 cue 反推），BGM/SFX 的 `startMs/gainDb` 按冻结值。
5. **渲染版本共享成片历史。** 同一集的时间线渲染与整集渲染写同一条 episode film 历史，每个时间线版本产生新的 `FilmVersion`；旧版本与旧媒体逐字节保留。装配字面量 `ffmpeg-render-timeline-v1` 与 `render.sources.timeline` 一一对应（Store 校验强制）。
6. **错误码边界。** 参数结构错误（字段缺失、`outMs ≤ inMs`、voice `startMs ≠ 0`、未知 kind、越界数值）在 schema 层返回 `INVALID_ARGUMENTS`；语义错误返回契约码（`TIMELINE_INVALID`/`TIMELINE_TRIM_INVALID`/`TIMELINE_SOURCE_MISSING`/`TIMELINE_VERSION_CONFLICT`/`TIMELINE_NOT_FOUND`、`SUBTITLE_TRACK_MISSING`、`AUDIO_*`）。
7. **本地渲染不要求视频模式。** 时间线保存/音频导入使用 `videoMode=off` 的 metadata-only 客户端 Run；`film_render` 客户端动作改为 metadata-only（本地 FFmpeg，不依赖供应商配置），因此 off 模式也能导出时间线/整片。它仍不登记 Job、不产生供应商费用。
8. **附带修复（回归发现）**：`validate_batch_changes` 过去把“未改动但仍在 running 的批次”也当作一次推进，要求 `revision + 1`；任何并发/无关事务（Job 轮询、分集保存、渲染提交）都会因此返回 `BATCH_CONFLICT`。E3 全量回归捕获到该竞态后，改为 `record == old` 直接跳过，未改动的批次不再被误判；新增 `test_an_unrelated_commit_while_a_batch_runs_does_not_advance_it` 覆盖，旧有状态机与终态不可改写规则不变。
9. **附带修复（回归发现）**：`mix-zh-v1` 的第二遍 `loudnorm` 直接拼入第一遍的 `input_i/input_tp/input_lra/input_thresh/target_offset`。近静音时间线会得到 FFmpeg 的 `-3.402823e+38` 或 `-inf` 哨兵值，第二遍因此以 “Error parsing global options: Result too large” 失败（时间线只有少量对白时偶发）。E3 增加有限的测量值校验（-100..0 LUFS、-100..20 dBTP、0..100 LU、-120..0 dB、偏移 -100..100），任一非法就退回未测量的动态 `loudnorm`，由后续 EBU R128 复测/修正继续把关；新增 `test_loudnorm_second_pass_ignores_silence_sentinels` 覆盖，M6 混音测试全绿。

## 3. 数据与基线

- Schema 仍是 **9**；`timelines` 集合由 E3 开始写入，v9 信封不变，`DatabaseV9` 仍是三个新集合。E3 新增 `Timeline` 等 7 个模型与 `RenderTimelineSource`。
- 对 `docs/M7_E0_BASELINE.json` 的 **165** 个契约模型哈希逐项比对，E3 声明的漂移只有：`storage.Database/DatabaseV5/DatabaseV7/DatabaseV8`（嵌入 films 的传递 `$defs`）、`films.Film/FilmAssembly/FilmRender/FilmVersion/RenderSources`（新增 timeline 字段与装配字面量）、`renders.FilmRenderArgs/RenderReadArgs/RenderTask`（timeline scope 与字段）。其余逐字节不变；`tests/test_m7_schema.py` 显式列出该集合。
- 旧 v1–v8 数据仍可打开；时间线记录与镜头/媒体引用分离，删除或改写旧版本会被 Store 拒绝（`TIMELINE_VERSION_CONFLICT`/`TIMELINE_INVALID`）。

## 4. 工具面与规则

- mock 与 live 同步 **18/18**：`timeline_save`（write，同步本地事务）与 `timeline_read`（read）；`M7_E3_TOOL_NAMES=("timeline_save","timeline_read")`、`M7_TOOL_NAMES` 为 5 个季/时间线工具。
- 冻结 **16/16** 视图：使用当前 features 改 `toolsVersion/rulesVersion=16` 精确命中，时间线工具与规则消失，`season_generate`/`film_render`/`subtitle_save` 与 M7 E2 规则字节仍在；15/15 仍隐藏 `season_generate`，13/13 隐藏全部季/时间线工具。
- 规则 `m7-rules-v1` 追加 `timeline-zh-v1`、裁剪/转场/字幕/音轨/标题卡约束、`film_render` 的 `scope=timeline` 与 `ffmpeg-render-timeline-v1`；旧变体规则字节未改写。
- 只读 Run 仍可 `timeline_read`，不能 `timeline_save`。`episode_read` 追加只读 `timeline` 投影（当前版本摘要或 `null`）。

## 5. 时间线与渲染验收

- **保存/重放**：19 镜头 90 秒整集时间线保存为 v1，`replayed=false`；相同内容重放返回同一版本且不新增记录、不登记 Job；修改首镜入点后追加 v2，v1 逐字节保留，revision 从 0→1。
- **校验矩阵**：重复镜头、越界/倒序/不足 1 秒裁剪、最后镜头后的转场、重复边界、超过相邻 1/3 的转场、越界场记/分镜/镜头版本、非整集字幕轨、不存在的字幕版本、voice 无字幕轨、voice 非零起点、kind 与资产不一致、标题卡重叠/越界、有效时长越界均被拒绝，失败不写 `timelines`。
- **过期**：保存时间线后修改分集标题（episode v2），`timeline_read.stale=true`；渲染在编码前返回 `TIMELINE_SOURCE_MISSING`，`films`/`media`/`renders` 完全不变。
- **整集渲染**：90 秒时间线（2 条标题卡、1 条 BGM、1 条 voice）输出 `assembly_version=ffmpeg-render-timeline-v1`、`render.sources.timeline`（ID/版本/指纹）、`subtitle.retimed=true`、`metadata.duration_seconds` 与有效时长相差 ≤1 帧；BGM 的 `gainDb=-12`、`startMs=500` 写入 `render.audio.tracks`；voice 由 cue 定位。第二次同版本渲染 `replay=true` 且返回同一 `filmVersion`。
- **重排/裁剪/转场后再渲染**：交换前两镜、首镜裁掉 1 秒、加 400ms 叠化，v2 有效时长 88.7 秒；成片历史为 [v1, v2]，v1 SHA 与装配版本不变，v2 的 `render.sources.timeline.timelineVersion=2`、时长与 88.7 秒相差 ≤1 帧；显式渲染 v1 仍重放 v1。
- **字幕重映射**：纯函数用例覆盖“裁剪窗口外 cue 丢弃、窗口内夹取、第二镜起点按 clip 入点平移”；渲染版本的 `rendered_cues` 由冻结字幕轨 + 时间线版本重算，cue 单调不重叠且全部落在有效时长内。
- **音频导入**：WAV 导入成功（`replayed=false`）；相同字节换标题再次导入 `replayed=true` 且复用同一 `audioId`；空许可 `AUDIO_LICENSE_MISSING`；非音频字节 `AUDIO_FORMAT_UNSUPPORTED`。

## 6. CLI 与 HTTP

- CLI：`timeline sources`（源镜头/字幕/音频）、`timeline save --clips`、`timeline show`、`audio import|list`、`film render --timeline` 全部通过；过期 `expectedRevision` 返回 `TIMELINE_VERSION_CONFLICT`；渲染输出 `assemblyVersion=ffmpeg-render-timeline-v1`；全程视频模式 off，Job 数不变。
- HTTP：时间线源视图、保存（201/重放/409）、详情、404；音频导入（201/重放/400）、列表；`film_render` 时间线（202）与 `/api/renders?timelineId=`；`episode_read`/会话快照包含 `timeline` 与最新渲染；`/timeline-state.js`、`/app.js` 与 `index.html` 的 `timeline-panel`/`audio-input` 可访问；CSRF 缺失 403、未知字段 422。
- Web 状态逻辑：`timeline-state.js` 纯函数覆盖草稿、摘要、校验、移动/裁剪、转场、保存体、音轨/字幕投影、顺序预览与错误文案。

## 7. 测试与回归

- 新增后端定向用例 **19** 项：`tests/test_m7_timelines.py` 10、`tests/test_m7_timeline_render.py` 5、`tests/test_m7_timeline_api.py` 2、`tests/test_m7_timeline_cli.py` 2；新增前端 `tests/test_timeline_state.mjs` **13** 项；回归修复附带 2 项（批次并发、静音 loudnorm）。
- 更新 `tests/test_m7_schema.py`（18/18、E3 声明漂移集合、时间线限制）、`tests/test_m7_tools.py`（18/18 与冻结 16/16）、其余工具版本断言（16→18）。
- 规则增长后的夹具预算：M1-B/M1-C 评估与 live 应用夹具的 `context_bytes` 由 64/72 KiB 提到 **96 KiB**（`tests/test_m1b_evaluation.py`、`tests/test_m1c_evaluation.py`、`tests/wan_support.py`、`test_live_application.py` 的一条脚本历史，以及 `test_m2_generation`/`test_m2_integration`/`test_m3_recovery`/`test_video_integration`/`test_wait_*` 的 `agent_runner` 夹具）；这是测试参数，产品默认 `VAGENT_CONTEXT_BYTES` 仍为 65536，`ContextBuilder()` 的显式调用与产品运行时预算不变。`scripts/wheel_smoke.py` 的 M1-C/M2 夹具预算同步保持 96 KiB。
- 全量回归：**1405 passed / 3 skipped**（1253.07 秒，0:20:53；3 项 skip 为既有的 Windows 符号链接权限用例）。前端八个状态文件 **78 项** 通过（`timeline-state` 13 项）；Ruff check/format（215 个 Python 文件）与 `node --check`（`app.js`、`timeline-state.js`）通过。

## 8. 保留的边界

- **预览不是交付物**：Web 预览按 clip 顺序播放源镜头并应用裁剪偏移，不做服务端合成，不保证字幕/混音帧级准确；导出才是交付物。
- **真实付费样本**：E3 全程 submit=0、TTS 请求=0；真实歌曲/音效只通过客户端导入的本地测试 WAV 验证，未接入第三方音乐 API，也未验证真实 M4A 文件（M4A 只按容器头 + 包内 FFmpeg 探测实现，未做真实样本）。
- **帧级精度**：以 30 fps 编码栅格和 ±1 帧容差验收，不承诺跨构建逐字节一致。
- **未实现**：9:16 竖屏、关键帧/变速/特效、多轨混音工作站、跨集“超级成片”、去水印、1080P 超分；`timeline_save` 不接受分镜字幕轨或跨集镜头。
- **真实浏览器验收**：E3 只交付 HTTP/Web 入口、纯状态测试与手动可复现的页面结构；受控浏览器全流程与安装态 wheel 属 E4。

## 9. E4 入口

1. `scripts/evaluate_m7.py`：默认离线，覆盖 3 集批次/失败隔离/恢复、时间线 v1→v2 重拼、字幕重映射、浏览器工作台与导出，复用已有本地媒体。
2. 安装态 wheel 与 `scripts/wheel_smoke.py` 追加时间线工作台与音频导入的受控用例。
3. 四组 CI 与 `docs/M7_ACCEPTANCE.md`；真实付费样本仍需单独授权。
