# M6 契约：成片后期（字幕、配音、配乐、转场、导出）

冻结日期：**2026-10-22（E0）**。基线 `master` `b8df786`（**v0.4.0**：schema 6、tools/rules 9/9、冻结 8/8…1/1）。范围见 [M5 计划](./M5_PLAN.md) §4 的 M6，上游语义见 [M5 契约](./M5_CONTRACTS.md)、[M3 契约](./M3_CONTRACTS.md) 与 [M4 归一化](./M4_NORMALIZATION.md)，基线证据见 [E0 验收](./M6_E0_ACCEPTANCE.md) 与 `docs/M6_E0_BASELINE.json`。

本文规定 E1–E4 的实现要求。**E0 不改变产品代码（`src/`、`web/`、`tests/`）、不注册工具、不调用模型或供应商：视频 submit=0、文本模型调用=0、TTS 请求=0。** 本轮**不做 9:16 竖屏**，画幅仍为 16:9。

> **E1 已实施（2026-10-22）**：schema 7、`subtitle-align-zh-v1`、`subtitle_save`、SRT/ASS、软字幕/旁挂/烧录、包内 OFL 字体、tools/rules 10/10（冻结 9/9）、CLI/Web/HTTP 与验收见 [E1 验收](./M6_E1_ACCEPTANCE.md)。实现中对本文件的细化（不改冻结语义）：
>
> - **时间基**：`SubtitleCue.start/end` 是精确有理数（分母 1 000 000 的微秒），`subtitle_save` 的人工修正输入用整数毫秒；对齐与校验都以“每个镜头窗口的边界只舍入一次”的规范微秒窗口为准，最后一 cue 精确落在冻结时间线末端。
> - **字幕轨冻结输入**：`SubtitleTrackVersion` 追加 `timeline[]`、`timelineSource`、`filmVersion`、`timelineSha256`、`sourceSha256`；`storyboardVersion` 在分镜作用域记录，`episodeVersion` 在整集作用域记录。分镜已有成片时按成片实际（有理数）时间线对齐，否则按分镜镜头时长；导出只接受与成片时间线逐段相等的字幕轨，否则 `RENDER_TIMELINE_MISMATCH`。
> - **导出范围（E1）**：`ffmpeg-render-v1` 只实现“对已拼接成片的字幕封装/烧录”子集（`scope=storyboard`，720P，音频原样复制；软字幕写 `mov_text` + 旁挂 SRT，烧录用 `subtitles` 滤镜 + 包内字体，`both` 两者都做）。整集导出、音频/响度、转场、标题卡与 1080P 上采样仍属 E2/E3；请求 1080P 或整集导出返回 `RENDER_PROFILE_UNSUPPORTED` / `RENDER_SOURCE_MISSING`，不静默降级。
> - **音频测量**：E1 复制源音频、不做混音，因此 `render.audio` 为默认空轨道/空测量，`render.measurements.audio` 记录实测编码/采样率/声道（无声源为 `null`）；响度目标在 E2/E3 生效。
> - **字体**：包内 `VAgentSubtitleSC-400.ttf`（Noto Sans SC 子集，改名以符合 OFL 对修改版保留名的要求），`fontRef` 固定名称与 SHA-256；文件缺失或哈希不符返回 `FONT_MISSING`。
> - **错误码**：E1 额外使用 `RENDER_ENCODE_FAILED` 与 `RENDER_VALIDATION_FAILED` 表示本地编码/输出校验失败（§7 的 18 个码保持不变，仅为新增本地导出失败细分）。
> - **媒体联合**：soft 成片含 `mov_text` 轨，`mp4-avc-v1` 解析接受 `sbtl/text/subt` 字幕轨（仍拒绝外部数据引用、重叠样本与未知轨道）；音频 `kind=audio`（`contractVersion=3`）在 E1 只冻结记录形状，读取/写入由 E2 实施。
>
> **E2 已实施（2026-10-22）**：`tts-providers-v1`、`local-tts-v1`、CosyVoice 适配器、`audio_generate`、音频资产/媒体读写、`mix-zh-v1`、tools/rules 11/11（冻结 10/10）与验收见 [E2 验收](./M6_E2_ACCEPTANCE.md)。实现中对本文件的细化（不改冻结语义）：
>
> - **官方事实**：CosyVoice 非实时 HTTP API 为 `POST {base}/api/v1/services/audio/tts/SpeechSynthesizer`，`Authorization: Bearer <Key>`；请求 `{model, input:{text, voice, format:"wav", sample_rate:48000, rate, volume}}`；返回 `request_id`、`output.audio.url`（24 小时有效）、`usage.characters`。共享域名 `dashscope.aliyuncs.com` 与业务空间域名 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` 均可；`cosyvoice-v2` 北京 2 元/万字符（0.20 元/千字符）、RPM 180。未经真实样本前能力表固定标 `documented-no-real-sample`。
> - **音色绑定键**：冻结的 `SceneVersion.dialogue[].speaker` 可能为空或未保存为角色，因此绑定键取「speaker（空则 `画外`）+ 集」，能唯一解析到角色时同时记录 `characterId`；`ProviderAudioSource` 追加可选 `episodeId`/`speaker`（E1 从未写入音频资产，历史记录不受影响），使额度、绑定与幂等复用均可从 Store 重算。`VoiceProfile` 仍只描述声音，不承载供应商 voice id。
> - **预算与价格**：单条 ≤500 字符、每集 ≤2000 字符、≤40 次请求；价格已核实时单条 ≤0.10 CNY、每集 ≤0.40 CNY。金额以 4 位小数记录，只按 CNY；`unknown` 价格（本地夹具）只按字符与次数限制并标注。额度在锁内从 `audio` 集合重算，额度不足或音色冲突整批拒绝，不部分提交。
> - **本地夹具**：`local-tts-v1` 严格按 §8 公式生成 48 kHz、单声道、16 位 PCM WAV（正弦 + 静音、10 ms 淡入淡出、无时钟/随机）；CosyVoice 请求固定 `format=wav`、`sample_rate=48000`，下载只接受 HTTP(S) `*.aliyuncs.com`、禁用重定向、上限 25 MB。
> - **混音与响度**：`mix-zh-v1` 每轨 `volume=<gain>dB` + `adelay` + `apad/atrim`，voice 0 dB、BGM −18 dB、SFX −6 dB；BGM 用 `sidechaincompress`（阈值线性 0.1 = −20 dB、ratio 8、attack 20 ms、release 400 ms），最终 `amix normalize=0`。两遍 `loudnorm`（先测量后应用）后，用 `ebur128=peak=true` 对交付文件复测；成品响度超出 −16±0.5 LU、true peak > −1 dBTP 或时长相距时间线超过 1 帧时返回 `LOUDNESS_OUT_OF_RANGE`/`RENDER_VALIDATION_FAILED` 并删除产物。两遍 `loudnorm` JSON 与 EBU R128 复测值都随 `MixResult` 返回，E3 写入 `FilmVersion.render.audio.loudness`。
> - **错误码**：§7 的 18 个码保持不变；E2 新增 `AUDIO_FORMAT_UNSUPPORTED`、`TTS_KEY_MISSING`、`TTS_EMPTY_EPISODE`、`PROVIDER_RESULT_UNTRUSTED` 与供应商传输码（`PROVIDER_*`），用于本地上传校验与受控 TTS 错误映射。
> - **工具面**：新 Run 发布 tools/rules **11/11**，`audio_generate` 为 `write`、无 Job；冻结 10/10（隐藏 `audio_generate`、保留 E1 规则字节）与 9/9…1/1 继续按 `toolFeatures` 恢复。`episode_read` 只追加 `audio` 投影（音色表、额度、绑定、资产），不改变既有字段。
> - **范围**：E2 只做配音生成、资产/媒体与本地混音服务；整集渲染、转场、标题卡、1080P、CLI/Web 音频面板与浏览器验收仍属 E3/E4，`film_render`/`render_read` 未注册。
>
> **E3 已实施（2026-10-23）**：`film_render`/`render_read`、`cut/fade/crossfade`、标题卡、720P/1080P、跨场整集渲染、旁挂 SRT、CLI/HTTP/Web 与验收见 [E3 验收](./M6_E3_ACCEPTANCE.md)。实现中对本文件的细化（不改冻结语义）：
>
> - **存储**：E1 的 schema 7 保持不变；E3 新增 schema **8**，只加一个 `renders` 渲染任务集合（`RenderTask`/`RenderSceneProgress`），v7→v8 逐字节备份、v1–v7 全部可读。`Film` 现在恰好属于一个分镜或分集，`FilmVersion` 用 `storyboardVersion` 或 `episodeVersion` 记录来源；逐场成片继续保留。
> - **成片装配**：E3 从已交付镜头直接渲染，新增 `ffmpeg-render-native-v1` 装配记录；E1 的 `subtitle_service` 仍只在已拼接成片上追加字幕版本。`RenderSources.subtitleTrack` 在 `soft` 无字幕时为可选；`RenderSubtitle.retimed` 标记 cue 是否按转场夹取。
> - **有效时间线**：`fade`/`crossfade` 以 `xfade` overlap 只裁掉出镜镜头尾部；总时长 = 镜头时长之和 − 转场时长之和。跨场转场只在 `scope=episode` 允许，场边界必须在时间线中。源字幕轨仍冻结在源时间线；渲染把 cue 夹入有效镜头窗口并写旁挂 SRT，`GET /api/films/{filmId}/versions/{version}/subtitles.srt` 重算同样的 cue。E1 的无转场导出保持原严格“时间线逐段相等”。
> - **标题卡**：最多 4 条，`start/duration` 以毫秒输入、以有理数记录，必须落在有效时间线内且互不重叠；用包内 OFL 字体 `drawtext` 居中渲染，`styleProfile=title-zh-v1`。
> - **逐场与失败隔离**：每场先归一化为 profile 基准的无声中间文件，再跨场组装；单场失败只标记该场并继续其他场，最终不提交整集成片，已交付的逐场成片、媒体与 Job 不删除、不降级。
> - **音频放置**：voice 资产用该集冻结台词在字幕 cue 流中的字符位置定位起点（不重读原文、不猜台词）；BGM/SFX 从 0 开始；`mix-zh-v1` 的两遍 `loudnorm` 与 EBU R128 复测写入 `render.audio.loudness`。稀疏对白的交付漂移允许一次动态 loudnorm 重试与按 EBU 实测的增益/limiter 修正，仍超差返回 `LOUDNESS_OUT_OF_RANGE`。
> - **1080P**：作为参数化上采样 profile，逐场直接归一到 1920×1080/30fps，记录 `upscaled=true`；不做超分或画质承诺。
> - **工具面**：新 Run tools/rules **13/13**；`film_render` 为 `write`、锁外执行、无 Job、无供应商费用；`render_read` 只读。冻结 **11/11**（隐藏 `film_render`/`render_read`）与 10/10…1/1 继续按 `toolFeatures` 恢复。`episode_read` 只追加 `render` 投影。
> - **错误码**：§7 的 18 个码保持不变；E3 额外使用 `RENDER_ENCODE_FAILED`/`RENDER_VALIDATION_FAILED`（E1 已用于本地导出）与 `CONFIRMATION_REQUIRED`，并新增 `RENDER_NOT_FOUND`/`RENDER_CONFLICT` 表示任务读取与任务历史。
>
> **E4 已实施（2026-10-23）**：整集成片、浏览器字幕开关、真实镜头复用与验收见 [E4 验收](./M6_E4_ACCEPTANCE.md)。实现中对本文件的细化（不改冻结语义）：
>
> - **时间线连续性**：真实 AAC 镜头的成片时间线把段起点舍入到微秒，但时长保留精确有理数；字幕与渲染都改为按精确时长累加起点，E1 旁挂导出在微秒基比较时间线。schema、工具面与错误码不变。
> - **浏览器字幕开关**：分集面板从受控 `sidecarUrl` 读取旁挂 SRT，在播放器上叠加/隐藏；内嵌 `mov_text` 保留并置为 `hidden`，避免支持 `mov_text` 的浏览器重复显示。开关是只读前端动作，不写 Store。
> - **标题卡实现**：Linux 的 `imageio-ffmpeg==0.6.0` 静态构建带 libass 但没有 `drawtext`；E4 把标题卡改为同一包内字体的 ASS 事件，经 `subtitles` 滤镜烧录，`title-zh-v1` 的文字、时间与居中语义不变，Windows/Linux 共用一条路径。
> - **真实样本**：E4 复用 M5 E4 已批准的 `fridge-notes` 18 个 Wan 镜头，新增视频 submit=0、真实 TTS=0；真实 CosyVoice 与新付费样本仍需单独授权。

## 1. 决策与版本

E0 前的 7 个决策点已确认，冻结如下：

| 决策 | 冻结结果 |
|---|---|
| TTS 供应商 | **阿里云百炼 CosyVoice**，复用现有 `VAGENT_DASHSCOPE_KEY`；端点、参数、音色表与计价必须在 E2 按官方文档核实后才可提交真实请求，未做真实样本前能力表标「文档支持，未做真实样本」 |
| 音乐/音效 | **仅 CC0/公有领域素材与用户导入**；每个音频版本必填 `sourceUrl` 与 `license`；测试使用 FFmpeg 合成音，不引入第三方音乐 API |
| 字幕形态 | **默认软字幕**（MP4 内嵌 `mov_text` + 旁挂 `.srt`），**烧录为可选导出档**；两种模式都要实现 |
| 分辨率 | 验收口径 **720P**；允许参数化 1080P profile，但必须标 `upscaled=true`，不承诺画质提升 |
| 水印 | **接受** Wan 固定「AI 生成」水印，写入 `render.watermark`，不裁切、不做去水印 |
| 中文字体 | **包内附 OFL 授权子集**（Noto Sans SC 子集），`fontRef` 记录名称与 SHA-256；缺失或哈希不符返回 `FONT_MISSING` |
| 整集成片 | **纳入 E3**：跨场整集导出，含跨场转场与连续字幕时间轴；逐场成片保留 |

| 项目 | 冻结结果 |
|---|---|
| 存储 | E1 新建与迁移为 schema **7**；v6→v7 只加两个空集合 `audio`/`subtitleTracks`，并让 media 联合新增 `kind=audio`（`contractVersion=3`）；逐字节备份，v1–v6 数据可读 |
| 工具面 | **E0 保持 9/9**；E1 追加 `subtitle_save` → **10/10**；E2 追加 `audio_generate` → **11/11**；E3 追加 `film_render`、`render_read` → **13/13**；全部冻结变体 9/9、8/8、7/7、6/6、5/5、4/4、3/3、2/2、1/1 保留且可按 `toolFeatures` 恢复 |
| 恢复 | 继续按完整 `toolFeatures` 身份选择冻结注册表；新版本不得改写旧 prompt/规则字节，未知组合返回 `RESUME_CONFIG_CHANGED` |
| execution/context/checkpoint | 保持 **3 / 2 / 1**；渲染不引入第二套等待状态机，复用现有 wait 运行时（成员上限 20、超时 600 秒） |
| 视频 Job | 沿用 **v6** 通用供应商请求；M6 不新增视频 Job 契约版本，也不改变视频预算桶 |
| TTS 请求 | 独立 `TtsRequestV1` / `TtsResultV1`，**不进**视频 Job；一次请求对应一条台词（或一个人工分组的台词块） |
| 能力与价格 | `multi-provider-v3` 不变；新增 `tts-providers-v1`（供应商、模型、音色、每千字符计价或 `unknown`） |
| 预算 | 视频每集 20 Job / 60.00 CNY 不变；**TTS 每集 ≤ 2000 字符、单次请求 ≤ 500 字符、每集 ≤ 40 次请求**；金额上限仅在价格已核实（非 `unknown`）时生效，`unknown` 只按字符数与次数约束并明确标注 |
| 渲染 | 纯本地 FFmpeg，零供应商费用；单次 FFmpeg 调用超时仍为 **120 秒**，整次渲染作业上限 **600 秒**；成片时长 ≤ **90.000 秒**且必须与冻结时间线一致 |
| 成片对象 | `FilmVersion.render` 为**可选**；`ffmpeg-concat-copy-v1` / `ffmpeg-concat-normalize-*` 的旧成片不得携带 `render`，渲染成片不得缺少 `render` |

## 2. 数据对象

新增对象只增不改：根记录保留 `currentVersion` 与连续 `versions[]`，历史版本、已冻结的预算与已绑定的引用不可重写或删除。ID 由服务端生成（canonical UUID），用户与模型不能指定文件路径。

| 对象 | 必填内容 |
|---|---|
| `AudioAsset` | `id, projectId, kind, currentVersion, versions[], revision, createdAt`；`kind ∈ {voice, bgm, sfx}` |
| `AudioAssetVersion` | `version, kind, mediaId, source, duration, sampleRate, channels, sha256, sizeBytes, createdAt`；`source=provider` 时含 `provider, model, voiceId, text, speed, characterId`；`source=import` 时含 `title, sourceUrl, license` |
| `SubtitleTrack` | `id, projectId, scope, storyboardId?, episodeId?, currentVersion, versions[], revision, createdAt`；`scope ∈ {storyboard, episode}` |
| `SubtitleTrackVersion` | `version, scope, storyboardVersion?, cues[], styleProfile, fontRef, createdAt` |
| `FilmRender` | 见下；作为 `FilmVersion.render` 的可选字段 |

`SubtitleTrackVersion.cues[]` 每条为 `{start, end, text, speaker?, sceneId?, shotId?, sourceSha256}`；`styleProfile` 固定 `subtitle-zh-v1`；`fontRef` 为 `{name, sha256}`。

`FilmRender`：

| 字段 | 内容 |
|---|---|
| `renderVersion` | 固定 `ffmpeg-render-v1`（后续渲染引擎变更才追加新字面量，不改写旧值） |
| `exportProfile` | `{name, width, height, fps, videoCodec, crf?, videoBitrate?, pixFmt, audioCodec, sampleRate, channels, audioBitrate, subtitleMode, upscaled}`；`name ∈ {720p-h264-aac-v1, 1080p-h264-aac-v1}`；`subtitleMode ∈ {soft, burn, both}` |
| `engine` | `{version, configurationSha256}` |
| `subtitle` | `{trackId, trackVersion, trackSha256, mode, fontRef, sidecarSha256?}` |
| `audio` | `{tracks[{audioId, audioVersion, kind, gainDb, start, ducking, voiceBinding?}], ducking{thresholdDb, ratio, attackMs, releaseMs}, loudness{targetLufs, targetTruePeakDb, input{}, output{}}}` |
| `transitions` | `[{afterShotId, type, duration}]`；`type ∈ {cut, fade, crossfade}`；`cut` 不记录（默认） |
| `titleCards` | `[{text, start, duration, styleProfile}]`，最多 4 条 |
| `watermark` | `{required: true, source: "provider"}` |
| `measurements` | `{duration, sizeBytes, sha256, video{codec,width,height,fps}, audio{codec,sampleRate,channels}}` |
| `sources` | `{episodeId?, storyboardVersion, shotVersionIds[], audioAssets[], subtitleTrack, font}` |
| `failureStage` | `{null, source, subtitle, audio, mix, encode, validate}` 之一；成功时为 `null` |
| `fingerprint` | 渲染指纹，见 §5 |

上限：每集音频资产 ≤ 40 条 voice + 8 条 bgm + 16 条 sfx；单条音频 ≤ 60 秒、≤ 25 MB；每条字幕轨 ≤ 200 cue；每条 cue ≤ 48 字符（最多 2 行、每行 ≤ 24 字符）；每条字幕轨每场 ≤ 40 cue。

## 3. 字幕时间轴（`subtitle-align-zh-v1`）

输入只有两样：绑定到分镜的 `SceneVersion.dialogue[]`（`text/speaker/paragraph`）与冻结的镜头时间线（顺序与时长）。**不重新读取原文、不重新猜台词。**

1. 切分：按终止标点（`。！？；…`）断句；单句超过 24 字符时再按次级标点（`，、：`）切到 ≤ 24 字符；单条 cue 最多 2 行、合计 ≤ 48 字符。
2. 归属：每条 cue 绑定其所属镜头（`shotId`）与场（`sceneId`）；一句台词跨越镜头边界时按边界切分，两侧保留同一 `speaker`。
3. 分配：在场内按 cue 字符数比例分配该镜头时长；每条 cue 最短 0.5 秒，相邻 cue 间隙 0.05 秒；镜头时长不足时在同一镜头内合并相邻 cue，**绝不越界到下一镜头**。
4. 全局起点：场内起点 = 前序镜头时长累加；整集起点 = 前序场时长按场序累加。
5. 确定性：同一输入字节 → 同一 cue 字节（无随机、无时钟、无浮点顺序依赖）；排序键固定为（场序、镜头序、段落序、行内序号）。
6. 人工修正：`subtitle_save` 追加新的 `SubtitleTrackVersion`，历史版本保留；渲染只引用某一确定版本，指纹随之改变。
7. 校验：cue 不得重叠、不得倒序、不得超出成片时长；违反分别返回 `SUBTITLE_CUE_CONFLICT` 与 `SUBTITLE_OVERFLOW`；无字幕轨而 `subtitleMode != soft` 时返回 `SUBTITLE_TRACK_MISSING`。

## 4. 音频与响度（`tts-voice-binding-v1` / `mix-zh-v1`）

- **音色绑定**：同一集内某角色第一次 `audio_generate` 时冻结 `{provider, model, voiceId, speed}`，后续同角色必须一致，否则 `VOICE_CONFLICT`；绑定回显在 `render.audio.tracks[].voiceBinding` 与音频资产版本上。`VoiceProfile` 仍是描述性字段（`description/timbre/pace`），**不承载供应商 voice id**。
- **TTS 文本来源**：只接受该集冻结的台词；服务端按字符数校验（单次 ≤ 500、每集 ≤ 2000、每集 ≤ 40 次），超限返回 `TTS_TEXT_LIMIT` / `TTS_BUDGET_LIMIT`，整批拒绝、不部分提交。
- **混音**：人声 0 dB；BGM 默认 −18 dB，侧链压缩 ducking（`thresholdDb=-20, ratio=8, attackMs=20, releaseMs=400`）；SFX 默认 −6 dB；各轨增益范围 −60…+12 dB；无音轨镜头补静音（沿用 M4 归一化）。
- **响度**：两遍 `loudnorm`（先测量后应用），目标 `-16 LUFS`、true peak `-1 dBTP`、`LRA=11`；两遍测量都写入 `render.audio.loudness`。成品测量超出容差（响度 ±0.5 LU 或 true peak > −1 dBTP）返回 `LOUDNESS_OUT_OF_RANGE`，不提交成片版本。
- **来源可追溯**：每条音频版本必须有 `license` 与 `sourceUrl`（导入）或 provider/model/voiceId（生成）；缺失返回 `AUDIO_LICENSE_MISSING` / `AUDIO_ASSET_MISSING`。

## 5. 渲染与导出（`ffmpeg-render-v1`）

渲染把「已交付镜头 + 字幕轨 + 音频资产 + 导出 profile」变成一条成片，全程本地执行，不产生供应商费用。

1. **归一化前置**：按 profile 基准（`1280×720/30fps/H.264 + AAC 48k 立体声`，或 1080P 的对应参数）复用 M4 归一化路径，保证拼接与转场处于同一时基。
2. **拼接与转场**：无转场边界走 `cut`；`fade` 用 `fade`/`afade`，`crossfade` 用 `xfade` + 音频交叉淡化；转场时长 0.1–1.0 秒，不得超过相邻任一镜头时长的 1/3；跨场边界在整集成片中允许转场，场内边界沿用分镜顺序。
3. **字幕**：`soft` 只写 `mov_text` 轨并旁挂 `.srt`；`burn` 用 `subtitles` 滤镜配合包内字体；`both` 两者都做。
4. **音频**：按 §4 混音并两遍响度归一。
5. **编码与封装**：MP4、H.264（`yuv420p`）+ AAC；`faststart`；文件名与媒体记录由服务端生成。
6. **校验**：时长与冻结时间线一致（≤ 1 帧偏差）、音画首帧对齐、字幕轨存在性（soft/both）、响度测量在容差内、解析 MP4 结构与编码参数、记录 `sizeBytes`/`sha256`。任一失败返回对应 `RENDER_*` 错误并保留 `failureStage`，不提交成片版本。
7. **幂等**：渲染指纹 = `sha256(分镜/分集版本, 镜头版本集合, 字幕轨版本, 音频资产版本集合, exportProfile, 引擎指纹, 字体指纹)`。同指纹重复请求返回已成片版本，不重复渲染；任一输入变化都产生新版本，旧版本保留。
8. **失败隔离**：整集渲染中单场失败只标记该场 `failed`，其他场继续；已交付的逐场成片不删除、不降级。

## 6. 工具面、规则与费用

| 工具 | 引入 | 输入要点 | 输出 | 幂等 | 费用 |
|---|---|---|---|---|---|
| `subtitle_save` | E1 | `scope`（storyboard/episode）+ 目标 ID + `cues[]` | 新 `SubtitleTrackVersion` 与确定性校验结果 | 相同 cues 返回同版本 | 无 |
| `audio_generate` | E2 | 集 ID + 台词范围 + 角色→音色 + **显式 `confirm`** | 逐条音频资产、字符数、估算或 `unknown`、集级余量 | 相同请求复用资产 | TTS 供应商费用 |
| `film_render` | E3 | 分镜/分集 + 字幕轨版本 + profile + 模式 + 转场 + 标题卡 + **显式 `confirm`** | 渲染任务与预估耗时上界 | 同指纹返回原成片 | 无（本地） |
| `render_read` | E3 | 渲染/分集 ID | 逐场进度、测量、失败阶段 | 只读 | 无 |

规则文本冻结为 `m6-rules-v1`；`M6_TOOL_NAMES = (subtitle_save, audio_generate, film_render, render_read)`，按 E1/E2/E3 分批启用，旧变体通过 `toolFeatures` 隐藏。

**费用口径**：视频与 TTS 分桶，不换汇、不互相抵扣；`unknown` 价格只报字符数与次数并明确标注；任何付费请求都必须在登记前回显估算、在同一次登记事务内复核额度；不自动重提。

## 7. 错误码

| 错误码 | 触发 |
|---|---|
| `SUBTITLE_CUE_CONFLICT` | cue 重叠、倒序或与镜头时间轴冲突 |
| `SUBTITLE_OVERFLOW` | cue 超出成片时长或超过字符上限 |
| `SUBTITLE_TRACK_MISSING` | 需要字幕轨但未提供或版本不存在 |
| `AUDIO_ASSET_MISSING` | 引用的音频资产或媒体文件缺失 |
| `AUDIO_LICENSE_MISSING` | 导入音频缺少 `license`/`sourceUrl` |
| `TTS_TEXT_LIMIT` | 单次 > 500 字符或每集 > 2000 字符 |
| `TTS_BUDGET_LIMIT` | 每集请求次数或金额额度不足 |
| `TTS_PROVIDER_UNKNOWN` | 供应商/模型/音色未在 `tts-providers-v1` 中核实 |
| `VOICE_CONFLICT` | 同集同角色使用不同音色绑定 |
| `RENDER_PROFILE_UNSUPPORTED` | profile 未在能力表内或与源参数不兼容 |
| `RENDER_SOURCE_MISSING` | 镜头未交付、场记未绑定或音频/字幕缺失 |
| `RENDER_TIMELINE_MISMATCH` | 输出时长/顺序与冻结时间线不符 |
| `RENDER_TIMEOUT` | 单次 FFmpeg 120 秒或整次作业 600 秒超时 |
| `LOUDNESS_OUT_OF_RANGE` | 响度或 true peak 超出容差 |
| `FONT_MISSING` | 包内字体缺失或 SHA-256 不符 |
| `TRANSITION_INVALID` | 转场类型/时长非法或跨边界越界 |
| `TITLE_CARD_LIMIT` | 标题卡超过 4 条或时间轴冲突 |
| `CONFIRMATION_REQUIRED` | 付费登记未显式确认 |

## 8. 确定性与离线

- **`local-tts-v1` 本地夹具**：不联网，按文本确定性合成音频（时长 = `clamp(ceil(字符数 × 0.18), 1, 8)` 秒，正弦 + 静音段，采样率 48 kHz、单声道）；离线套件、wheel 与 CI 全部使用它。
- **真实 TTS** 只在 E2/E4 的显式授权下提交，必须记录字符数、估算与实际账单状态（无凭据保持 `unknown`）。
- **字体**随包分发（OFL），CI 与本地使用同一文件；不允许依赖系统字体作为验收路径。
- **测量容差**：同一 `imageio-ffmpeg==0.6.0` 构建下响度测量允许 ±0.5 LU、时长允许 ±1 帧；不承诺跨构建逐字节一致，但必须记录引擎版本与配置指纹。
- **离线可复现**：渲染的输入、profile、字体、引擎指纹全部写入成片版本；同一数据目录重放应得到相同指纹（新版本或幂等命中）。

## 9. 基线保护

`docs/M6_E0_BASELINE.json` 记录 schema 6 的契约哈希、冻结上限与 FFmpeg 事实。E1 允许的变化只有三类，且必须在 E1 验收中逐项列出：

1. 新增 `DatabaseV7` 与 `audio`/`subtitleTracks` 域模型；
2. 既有模型**追加可选字段**（默认 `None`，例如 `FilmVersion.render`）；
3. media 联合新增 `kind=audio`（`contractVersion=3`）。

除此之外所有 `schemaSha256`、上限与工具名分组必须逐字节不变；`vagent.video.tools` 的既有工具名与规则字节不得改写。

## 10. 工作包与验收

| 包 | 交付 | 验收 |
|---|---|---|
| E1 字幕 | **已交付（2026-10-22）**：schema v7（两个空集合 + media kind=audio）、`subtitle_save`、`subtitle-align-zh-v1`、SRT/ASS 生成、软字幕与旁挂、烧录 profile 与包内字体；tools/rules **10/10**（冻结 9/9）。见 [E1 验收](./M6_E1_ACCEPTANCE.md) | 60 秒样例字幕零漂移；中文渲染正确；人工修正可重渲染；重叠/越界/缺轨全部拒绝；submit=0、模型调用=0 |
| E2 音频 | **已交付（2026-10-22）**：`audio_generate`、`tts-providers-v1`、`local-tts-v1` 与 CosyVoice 适配器、音色绑定、音频资产/媒体读写、`mix-zh-v1` 混音/ducking、两遍响度测量；tools/rules **11/11**（冻结 10/10）。见 [E2 验收](./M6_E2_ACCEPTANCE.md) | 人声清晰无削波；响度测量在容差内；来源与许可可追溯；字符/次数/金额额度超限整批拒绝；离线全链路零真实请求 |
| E3 转场/片头尾/导出/整集 | **已交付（2026-10-23）**：`film_render`/`render_read`、`cut/fade/crossfade`、标题卡、跨场整集渲染、`720p`/`1080p` profile、旁挂 SRT、schema 8 渲染任务；tools/rules **13/13**（冻结 11/11）。见 [E3 验收](./M6_E3_ACCEPTANCE.md) | 转场无黑帧、音画同步；时长与时间线一致；整集 60–90 秒成片（验收 89.3 秒）；单场失败隔离；同指纹幂等 |
| E4 验收 | **已交付（2026-10-23）**：离线套件 3 用例（89.3 秒整集、单场失败隔离、拒绝矩阵）、浏览器整集播放与字幕开关、M5 真实镜头复用（89.1667 秒、新增 submit=0）、安装态与 CI。见 [E4 验收](./M6_E4_ACCEPTANCE.md) | 一条完整短剧成片可播放、字幕可开关、音频测量达标；失败保留；CI 通过；真实付费样本需单独授权 |

## 11. 明确不做

- 9:16 竖屏与竖屏供应商能力（单列后续）。
- 声音克隆、明星音色、任何未授权的声音复刻。
- 专业多轨混音工作站、复杂动效、时间线交互编辑（属 M7）。
- 去水印、裁切水印区域。
- 1080P 画质增强/超分；1080P 仅作为参数化上采样 profile。
- Sora/Veo/Seedance/Replicate、可执行插件、PyPI 公开发布。

## 12. E2 前须核实（2026-10-22 已核实，不得先写实现再补文档）

已核实（2026-10-22，官方页面见下），结果写入 [E2 验收](./M6_E2_ACCEPTANCE.md) §2：

1. CosyVoice 的官方端点、请求/响应字段、音色 ID 列表、并发与限流：非实时 HTTP API `POST {base}/api/v1/services/audio/tts/SpeechSynthesizer`（[HTTP API 参考](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api)）；模型 `cosyvoice-v2`，`input.text/voice/format/sample_rate/rate`；返回 `output.audio.url`（24 小时）、`usage.characters`；北京 RPM 180。音色见 [CosyVoice 音色列表](https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list)。
2. 计价口径、货币、是否与 Wan 共用 Key：[cosyvoice-v2 模型页](https://help.aliyun.com/zh/model-studio/cosyvoice-v2) 为 **2 元/万字符**（CNY、按字符）；复用 `VAGENT_DASHSCOPE_KEY`，可不填业务空间（共享域名），填写后走业务空间专属域名。
3. 返回格式/采样率/时长/URL 有效期：请求 `format=wav`、`sample_rate=48000` 后返回 16 位 PCM WAV；结果 URL 24 小时；下载后由 `inspect_wav` 校验 RIFF/PCM 与时长，仅信任 `*.aliyuncs.com` 的 HTTP(S) 地址。
4. 字体已在 E1 交付（包内 OFL 子集与许可证），E2 未新增字体。
5. CC0 素材清单与许可格式已由 `ImportedAudioSource`（`title/sourceUrl/license` 必填）冻结；E2 未接入素材导入工具，导入路径留待 E4/M7。
