# M6 E3 验收：转场、标题卡、整集渲染与 720P/1080P 导出

日期：**2026-10-23**。基线：`master` `70ddc2e`（M6 E2 音频，schema 7、tools/rules 11/11）；本包交付 commit 见 `git log`。范围见 [M6 契约](./M6_CONTRACTS.md) §5/§10 的 E3，冻结决策见 §1。**E3 全程视频 submit=0、文本模型调用=0、TTS 真实请求=0；所有媒体由包内 FFmpeg 与本地 `local-tts-v1` 生成，未读取或调用任何供应商。**

## 1. 已交付

| 项目 | 交付 |
|---|---|
| 存储 | schema **8**：新增 `renders` 渲染任务集合；`v7→v8` 只加一个空集合并逐字节备份，v1–v7 数据全部可读。E1 的 schema 7 语义不变 |
| 成片对象 | `Film` 现在恰好属于一个分镜或分集（`storyboardId`/`episodeId` 二选一）；新增 `ffmpeg-render-native-v1` 原生渲染装配，`FilmVersion` 记录 `storyboardVersion` 或 `episodeVersion`；逐场成片保留 |
| 渲染记录 | `FilmRender.sources` 的 storyboard/episode 版本与字幕轨改为可选组合；`FilmRender.subtitle.retimed` 记录 cue 是否按转场夹取；渲染成片仍强制 `render`，旧拼接成片仍不得携带 |
| 工具 | `film_render`（`write`、锁外执行、显式 `confirm`）与 `render_read`（只读）；tools/rules **13/13**，冻结 **11/11**（隐藏两个渲染工具）与 10/10…1/1 全部可按 `toolFeatures` 恢复 |
| 时间线 | 以已交付镜头与冻结字幕轨为唯一输入；`cut` 不缩短，`fade`/`crossfade` 重叠转场只裁掉出镜镜头尾部；转场 0.1–1.0 秒且不超过相邻任一镜头时长 1/3；跨场转场仅在 `scope=episode` 允许 |
| 字幕 | 源字幕轨必须与镜头时间线逐段相等（否则 `RENDER_TIMELINE_MISMATCH`）；转场后用同一重叠把 cue 夹入有效镜头窗口，`soft` 写 `mov_text` + 旁挂 SRT，`burn` 用包内字体烧录，`both` 两者都做；旁挂 SRT 由 `GET /api/films/{filmId}/versions/{version}/subtitles.srt` 重算，摘要写入 `render.subtitle.sidecarSha256` |
| 标题卡 | 最多 4 条，`startMs/durationMs` 必须落在有效时间线内且互不重叠；用包内 OFL 字体 `drawtext` 居中渲染，`styleProfile=title-zh-v1`，失败返回 `TITLE_CARD_LIMIT` |
| 视频 | 逐场归一化到 profile 基准（720P 1280×720/30fps 或 1080P 1920×1080/30fps，H.264 `yuv420p`），场内/跨场转场按 `xfade` 分组、`cut` 用 `concat`；1080P 标 `upscaled=true`，不承诺画质 |
| 音频 | 按字幕 cue 把同集 voice 资产放到有效时间线，BGM/SFX 默认 −18/−6 dB 并用 `mix-zh-v1` 混音；两遍 `loudnorm` 与 EBU R128 复测写入 `render.audio.loudness`。稀疏对白导致的交付漂移会追加一次动态 loudnorm 与按 EBU 实测的增益/limiter 修正，仍超差才返回 `LOUDNESS_OUT_OF_RANGE` 且不提交成片 |
| 校验 | MP4 结构、H.264/AAC、分辨率、30fps、时长 ≤1 帧偏差、`mov_text` 存在性、字幕时间线、响度与产物 SHA-256；任一失败保留 `RenderTask.scenes[].failureStage`，不提交成片版本 |
| 幂等与失败隔离 | 指纹 = 分镜/分集版本、镜头版本与哈希、字幕轨版本与哈希、音频资产版本/起点、profile、转场、标题卡、引擎与字体；同指纹重放原成片。整集逐场归一化，单场失败只标记该场并继续其他场，已交付逐场成片不删除 |
| 整集 | `scope=episode` 跨场连续时间线、跨场转场、连续字幕与整集字幕轨；8 场 19 镜头验收成片 **89.3 秒**（原 90.0 秒减去 0.4/0.3 秒转场） |
| CLI | `vagent film render --storyboard/--episode --track --profile --mode --transitions --title-cards --request-id` 与 `vagent film renders`；离线、无 Key、未登记 Job |
| HTTP / Web | `POST /api/sessions/{id}/films/render`、`GET /api/renders`、`GET /api/renders/{id}`、`GET /api/films/{filmId}/versions/{version}/subtitles.srt|ass`；成片面板新增「整片渲染」、逐场状态与旁挂 SRT 下载，`web/render-state.js` |
| 验收文档 | 本文件；[M6 契约](./M6_CONTRACTS.md) 的 E3 细化、[M5 计划](./M5_PLAN.md) 的下一步与 README 状态已同步 |

## 2. schema 8 与迁移证据

- 新数据目录直接是 `schemaVersion=8`，`audio`/`subtitleTracks`/`renders` 均为 `{}`，`DatabaseV8` 校验通过。
- `v7→v8` 只加 `renders={}`；迁移前写 `state-v7-*.json` 逐字节备份，其他集合逐字段保留；再次打开不再迁移。
- v1–v6 的历史链继续机械迁移到 v8；`test_b0_compatibility`、`test_m1c_migration`、`test_m2_migration`、`test_m3_baseline`、`test_m5_baseline`、`test_storage_migration`、`test_storage_tools` 全部更新并通过。
- `test_m6_schema.py` 对比 E0 的 126 个模型哈希：E3 声明内的变化只有 `Film`、`FilmVersion`、`FilmAssembly`、`RenderSources`、`RenderSubtitle`、`DatabaseV5`、`Database`、`DatabaseV7`；其余字节不变。新增 `DatabaseV8`、`RenderTask`/`RenderSceneProgress` 与 E3 参数模型。
- 工具名分组新增 `M6_E3_TOOL_NAMES=("film_render","render_read")`；`M6_TOOL_NAMES` 仍是契约冻结的四个名字。

## 3. 有效时间线与转场

1. 源时间线：分镜有未渲染成片时用成片的逐段有理数时间线，否则用镜头计划时长；分集把每场按场序累加，场记必须绑定分镜（`RENDER_SOURCE_MISSING`）。
2. 转场：`afterShotId` 必须是时间线中非末尾的镜头；`fade`/`crossfade` 0.1–1.0 秒且 ≤ 相邻两镜头时长的 1/3；`cut` 不记录、不缩短；`episode` 允许跨场边界，分镜内仍沿用分镜顺序。
3. 有效时间线：每个转场只裁掉出镜镜头的尾部，入镜镜头保留全长的头部（与 `xfade` 的 overlap 语义一致），因此总时长 = 各镜头时长之和 − 转场时长之和。`test_m6_renders.py` 对 5/5/5 秒三镜头与 0.5 秒 crossfade 断言总长 14.5 秒、第二镜起点 4.5 秒。
4. cue 夹取：源字幕轨保持逐段不变；渲染时把 cue 起点/终点映射到有效镜头窗口，落在被裁尾部内的 cue 被丢弃或夹到边界，再保证不与前一条重叠。`soft/both` 的旁挂 SRT 与烧录 ASS 都使用夹取后的 cue；E1 的无转场导出仍走原严格路径。
5. 确定性：全部使用 `Fraction` 与整数微秒，不读时钟、不依赖浮点顺序；同一输入字节得到同一时间线、同一 SRT 与同一指纹。

## 4. 渲染流水线与失败矩阵

1. **逐场归一化**：每场先按 profile 缩放/补边/30fps/`yuv420p`，按镜头时长 `tpad+trim`，再按该场内的转场组成 `xfade` 组；组间 `cut` 走 `concat`。中间文件只含视频，单场失败只标记该场。
2. **音频**：voice 资产按该场/整集冻结台词的字符位置映射到字幕 cue 起点；BGM/SFX 从 0 开始。`mix-zh-v1` 输出 48 kHz 立体声 WAV 并完成两遍 `loudnorm` 与 EBU R128 复测。
3. **合成**：场与场之间按跨场转场再次 `xfade`/`concat`，随后叠加标题卡、烧录字幕，编码为 H.264 + AAC、`faststart`，输出时长精确裁剪到有效时间线。
4. **校验**：`inspect_mp4` 解析结构（子表/样本范围/外部引用）、探测流参数、比较时长 ≤1 帧、检查 `mov_text` 存在性与旁挂摘要、复核响度；失败删除 `.part` 并保留任务逐场状态。

| 场景 | 结果 |
|---|---|
| 分镜/分集、字幕轨或音频缺失 | `RENDER_SOURCE_MISSING`；分集未交付的场标记 `failed`，其他场继续 |
| 1080P | `1080p-h264-aac-v1`，`upscaled=true`，分辨率/尺寸校验通过 |
| 未知 profile / 超过 90 秒 | `RENDER_PROFILE_UNSUPPORTED` |
| 转场越界/末尾/超过 1/3 | `TRANSITION_INVALID`，不写任何成片 |
| 标题卡 >4/重叠/越界 | `TITLE_CARD_LIMIT` |
| 字幕轨时间线与镜头不一致 | `RENDER_TIMELINE_MISMATCH` |
| 需要字幕但无轨/无 cue | `SUBTITLE_TRACK_MISSING` |
| 字体缺失/哈希不符 | `FONT_MISSING` |
| 响度/true peak 超差 | `LOUDNESS_OUT_OF_RANGE` |
| 编码/结构/时长校验失败 | `RENDER_ENCODE_FAILED` / `RENDER_VALIDATION_FAILED` |
| 未显式确认 | `CONFIRMATION_REQUIRED` |

## 5. 整集与转场验收证据

- `tests/test_m6_render_episode.py`：8 场 19 镜头、19 条本地配音、89.6 秒整集（1 次 0.4 秒跨场转场），逐场全部 `succeeded`；`episodeVersion`、`episodeId`、19 条 `RenderAudio.tracks` 与多个不同起点、响度 |I+16|≤0.5、true peak ≤ −1 dBTP；同指纹第二次调用 `replay=true` 且版本数不增。
- `tests/test_m6_render_acceptance.py`：90.0 秒原时间线 + 0.4 秒跨场 crossfade + 0.3 秒场内 fade，输出 **89.3 秒**；`blackdetect=d=0.04:pix_th=0.08` 全片无黑帧；MP4 为 1280×720/H.264 + AAC 48k 立体声 + `mov_text`；旁挂 SRT 时间戳单调且不越过成片末端；第二条用例验证首场转场让后续场统一提前 0.4 秒。
- `tests/test_m6_render_service.py`：soft/burn/both、720P/1080P、crossfade+fade、标题卡、同指纹重放、`render_read`、无效转场不写文件、损坏输入标记场失败、`film_render` 缺确认被拒。
- `tests/test_m6_renders.py`：有效时间线、转场边界/1/3 限制、cue 夹取与丢弃、标题卡上限、参数与任务模型校验。

## 6. 工具面、CLI/Web 与恢复

- 新 Run：`toolFeatures.video.toolsVersion=13`、`rulesVersion=13`；规则文本 = `VIDEO_RULES_V11`/`LIVE_VIDEO_RULES_V11` + `RENDER_RULES`，旧 V11 字节未改写。
- `film_render`：`effect=write`、`feature=video`、`async_write_execute`（锁外 FFmpeg、锁内提交）；无 Job、无供应商、无费用。
- 冻结变体：新增 `register_tools_variant(11, ...)`（mock 与 live），配置严格为 `mode/toolsVersion=11/rulesVersion=11/capabilities`、规则为 V11、`exclude=("film_render","render_read")`；10/10 与更早变体在同一次改名中同时隐藏 `film_render`/`render_read`，历史 10/10、9/9…1/1 的 `toolFeatures` 仍逐字段可匹配。
- `episode_read` 追加 `render`（最新任务状态、逐场进度、成片与失败阶段）；既有字段不变。
- headless 规划排除 `film_render`（本地写工具）；渲染只在显式 CLI/Web/工具调用中执行。
- CLI：`vagent film render|renders`；HTTP：`POST /api/sessions/{id}/films/render`、`GET /api/renders`、`GET /api/films/{id}/versions/{v}/subtitles.srt|ass`；Web 成片面板与 `web/render-state.js`。

## 7. 测试证据

命令：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_m6_renders.py tests/test_m6_render_service.py tests/test_m6_render_episode.py tests/test_m6_render_acceptance.py tests/test_m6_render_api.py tests/test_m6_render_cli.py tests/test_m6_tools.py tests/test_m6_schema.py
node --test tests/test_render_state.mjs
.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
```

- 新增 **30 项**：后端 25 项 = `test_m6_renders.py` 9（时间线/转场/cue/标题卡/参数/任务）+ `test_m6_render_service.py` 8（soft/burn/both、1080P、转场、重放、进度、失败隔离、工具确认）+ `test_m6_render_episode.py` 3（整集、重放、单场隔离）+ `test_m6_render_acceptance.py` 2（89.3 秒成片与旁挂时间戳）+ `test_m6_render_api.py` 2 + `test_m6_render_cli.py` 1；前端 `test_render_state.mjs` 5。
- 另更新既有断言：schema 7→8（迁移/存储/CLI）、tools/rules 11→13（M2/M3/M5/M6 工具面与恢复）、`episode_read` 空视图补 `render: None`、全部旧冻结变体的 exclude 集合加入 E3 工具名。
- 定向结果：E3 套件 **43 passed**（含既有 M6 schema/tools 用例），`node --test tests/test_*state.mjs` **48 passed**（E1/E2/E3 全部前端状态测试）。
- 完整回归：**1335 passed / 3 skipped（1266.19 秒，0:21:06）**；三项 skip 仍是既有的 Windows 符号链接权限用例。

## 8. 明确未做与边界

- **真实浏览器播放/下载与字幕开关**：E4；E3 只交付 HTTP/Web 入口与离线 API 用例。
- **真实付费样本**：E3 全程 submit=0、TTS 请求=0；真实 Wan 片段复用与真实 CosyVoice 仍留待 E4/单独授权。
- **9:16 竖屏、1080P 超分/画质增强、去水印、多轨时间线编辑、复杂动效**：不做。
- **BGM/SFX 导入工具**：仍无；`mix-zh-v1` 只混用户已存在的 `bgm`/`sfx` 资产（start=0），CC0 素材导入留待 E4/M7。
- **逐条 cue 可视化编辑**：M7；E3 仅支持 `subtitle_save` 的人工 cue 与渲染夹取。
- **跨进程渲染队列**：渲染仍在单次工具调用内完成（单次 FFmpeg ≤120 秒、整次 ≤600 秒）；不做后台多任务队列。

## 9. 检查结果

```text
ruff check src/vagent tests scripts          # 通过
ruff format --check src/vagent tests scripts # 通过
node --check web/app.js web/render-state.js  # 通过
前端状态测试全部通过（新增 5 项）
```

完整回归：`pytest -q -p no:cacheprovider` → **1335 passed / 3 skipped，1266.19 秒（0:21:06）**。三项 skip 为当前 Windows 账户无符号链接创建权限的既有边界，与本包无关；本包新增 30 项全部通过。`git diff --check`、`ruff check`、`ruff format --check`（195 个文件）、`node --check web/app.js web/render-state.js` 与全部前端状态测试（48 项）通过。

## 10. 复现说明

1. 全新数据目录：`VAGENT_HOME=<dir> vagent film render --storyboard <id> --track <id> --mode both --confirm`。
2. 同一命令带相同 `--request-id` 重放；不同输入产生新版本，旧版本与文件保留。
3. 1080P 只需 `--profile 1080p-h264-aac-v1`，记录会标 `upscaled=true`。
4. 转场/标题卡从 JSON 或 JSON 文件传入；越界在编码前拒绝。
