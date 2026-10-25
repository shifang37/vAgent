# vagent

面向视频创作的 Agent 实习项目，使用 **Python + DeepSeek + LangGraph**，自主设计上下文、项目记忆、Skills、工具执行与护栏，再接入视频生成模型。

当前已实现 **Python CLI + 本地 Web Agent**，前端通过同源 API/SSE 调用共享 LangGraph Runner。M1–**M8** 已交付并合入 master（M3 合并提交 `ef8bdda` 于 2026-10-21；M4/M5 于 2026-10-22；M6 成片后期与 M7 多集工作台于 2026-10-24，发布版本 **v0.5.0**；M8 供应商路由、账单与真实样本于 2026-10-25，发布版本 **v0.6.0**，[发布说明](./docs/RELEASE_0.6.0.md)），支持真实万相多镜头本地 MP4 交付、逐镜头重做、素材库、用量统计和客户端序列连播，以及本地拼接成片与自动归一化、Kling/fal 多供应商、报价对比与手工账单、headless 与用户 skills；两 Key 直用 K0–K3 已完成。**M6 E0–E4 已交付（2026-10-23，schema 8、tools/rules 13/13）**；**M7 E0 契约已冻结（2026-10-23，schema 9 方向、工具面 13/13→18/18）**；**M7 E1 季与 schema 9 已交付（2026-10-23，schema 9、tools/rules 15/15、离线 submit=0）**；**M7 E2 批量队列已交付（2026-10-23，Batch/BatchEpisode、`season_generate`、tools/rules 16/16、离线 submit=0）**；**M7 E3 时间线工作台已交付（2026-10-23，Timeline/TimelineVersion、`timeline_save`/`timeline_read`、`ffmpeg-render-timeline-v1`、tools/rules 18/18、客户端 BGM/SFX 导入、离线 submit=0）**。**M8 E0 契约已冻结（2026-10-25，供应商路由/账单/发布，schema 9、tools/rules 18/18、168 个契约模型哈希不变）**；**M8 E1 只读路由预览已交付（2026-10-25，`provider-route-v1`、`vagent route preview`、`GET /api/route/preview`、Web 报价对比，复现 E0 `routeProbe.rowsSha256`、离线 submit=0）**，详见 [E1 验收](./docs/M8_E1_ACCEPTANCE.md)。**M8 E2 供应商运维与账单已交付（2026-10-25，`providers/gate.py` 提交间隔/429 冷却与 `PROVIDER_BACKOFF`、`billing-v1` 侧车账本、`vagent usage bill record|show`、`POST/GET /api/usage/bill`、`usage.billing` 逐币种汇总、`GET /api/usage?runtime=1` 内存指标、Web 用量面板账单区，工具面 18/18 与 schema 9 不变、离线 submit=0）**，详见 [E2 验收](./docs/M8_E2_ACCEPTANCE.md)。**M8 E3 真实样本已交付（2026-10-25，CosyVoice 1 集 19 条台词 / 估算 0.0340 CNY，真实 DeepSeek 编排 1 Run 4 次模型调用；Kling/fal 因缺凭据未执行、0 请求；视频 submit=0、168 个模型哈希与 `routeProbe` 不变）**，详见 [E3 验收](./docs/M8_E3_ACCEPTANCE.md)。**M8 E4 验收与发布已完成（2026-10-25，离线 4 用例、受控浏览器报价对比 + 账单重放、安装态 wheel smoke、基线 168 个模型哈希与 `routeProbe.rowsSha256` 不变，未新增付费样本）**：`pyproject.toml` 与 `__version__` 升到 **0.6.0**，发布说明见 [v0.6.0](./docs/RELEASE_0.6.0.md)，完整证据与复现见 [M8 验收](./docs/M8_ACCEPTANCE.md) 与 `output/m8-e4-suite.json`、`output/m8-e4-browser/`。**M6 E1 字幕已交付（2026-10-22，schema 7、tools/rules 10/10、离线 submit=0）**：`subtitle-align-zh-v1`、`subtitle_save`、SRT/ASS、软字幕/旁挂/烧录、包内 OFL 字体与只增不改的成片渲染版本，详见 [E1 验收](./docs/M6_E1_ACCEPTANCE.md)。**M4 自动时间线归一化与 M5 剧本→分集分场分镜已随 v0.4.0 发布，M6 成片后期与 M7 多集工作台已随 v0.5.0 发布**：M5 E4 于 2026-10-22 完成离线套件、CLI/Web 分集流程、安装态、受控浏览器与获批真实样本（`fridge-notes` 18 镜头 / 7 场，18 次 Wan 提交、估算 54.00 CNY、7 条逐场成片）。**2026-10-21 完成 M3 E0 契约冻结与 E1 本地拼接成片**：schema v5 的 `films`、`mp4-copy-signature-v1` 流复制签名、包内 FFmpeg 拼接、`film_assemble`/`film_read`（tools/rules 5/5）、`vagent film`、成片媒体分支与 Web 成片面板均已交付，**E1 全程视频 submit=0**。**2026-10-21 完成 M3 E2 多供应商与中转**：Job/Request v6、`multi-provider-v3`（Kling Singapore 直连与 fal standard 静音中转）、逐 provider/currency 与 Kling VIDEO_UNIT 预算桶、认证任务结果来源绑定与公网连接固定、tools/rules 6/6（冻结 5/5 与 4/4 仍可恢复）以及设置页多供应商 Key 均已交付，**E2 全程视频 submit=0**。**2026-10-21 完成 M3 E3 headless 与用户 skills**：`vagent headless` 两阶段计划/授权/集合等待/本地拼接交付、结构化 `--json` 与冻结退出码、`VAGENT_HOME/skills` 合并发现与坏文件隔离均已交付，**E3 离线证据、视频 submit=0**。**2026-10-21 完成 M3 E4 端到端验收与打包**：离线「一句话→多镜头→本地拼接成片」套件 6/6、安装态 wheel 成片工作台与 6 用例 PASS、受控浏览器 7/7、四组 CI 通过，并用 M2 真实分镜与三个真实万相 MP4 以零新增费用完成成片（42 项检查）；新增付费样本未执行。首轮发现见 [编排测试报告](./docs/AGENT_ORCHESTRATION_ACCEPTANCE.md)，当前边界见 [M3 契约](./docs/M3_CONTRACTS.md)、[E0 验收](./docs/M3_E0_ACCEPTANCE.md)、[E1 验收](./docs/M3_E1_ACCEPTANCE.md)、[E2 验收](./docs/M3_E2_ACCEPTANCE.md)、[E3 验收](./docs/M3_E3_ACCEPTANCE.md) 与 [M3 验收](./docs/M3_ACCEPTANCE.md)。

任务 2 已补齐本地配置向导、CLI/Web 流式回复和 9 类固定评测。**2026-10-09 完成 A5：完整真实套件 9/9 通过，M1-A 已验收。** 最终套件包含 3 次显式恢复，累计 27 次模型调用、25 次工具调用；此前失败记录完整保留，详见 [M1-A 验收记录](./docs/M1A_ACCEPTANCE.md)。**2026-10-10 完成 B5，M1-B 的 B0–B5 已验收**：真实 DeepSeek + Mock 套件 5/5 通过，累计 15 次模型调用、14 次工具调用，3 个 Job 各提交一次。应用自动推进模拟任务，持久等待、停止、重启恢复及 CLI/Web 已通过回归和仓库外 wheel 验证。详见 [M1-B 验收](./docs/M1B_ACCEPTANCE.md)；真实视频接入属于后续 M1-C。

## 当前进度

M3 E0 已完成 Kling 3.0 Turbo 直连与 fal standard 静音中转的官方核实、拼接方案 A（固定包内 FFmpeg、仅流复制）、版本/预算/headless 两阶段授权与用户 skills 契约，以及 schema v4 合成兼容基线。**E1 已交付本地拼接成片**：schema v4→v5 机械迁移（`films`）、`mp4-copy-signature-v1`（逐字段校验 avcC/AAC 配置、轨道时间线、edit list 与 priming）、按样本表推导每段时长与接缝偏移、Store 锁外可取消的 FFmpeg 流复制、`film_assemble`/`film_read` 与 tools/rules 5/5（旧 4/4 Run 仍按冻结工具集恢复）、`FilmMedia` 联合分支与 Range/下载、`vagent film assemble|show` 与 Web 成片面板。双片段真实拼接输出的 SHA-256 与 E0 记录的夹具修正变体逐字节一致；全量回归 **1007 passed / 2 skipped** 与前端 **31 项**通过，视频 **submit=0**，详见 [E1 验收](./docs/M3_E1_ACCEPTANCE.md)。

**E2 已交付多供应商与中转**：`JobV6`/`VideoRequestV6` 通用请求（历史 1–5 精确分派不变）、`multi-provider-v3` 能力表（Kling `kling-3.0-turbo` 新加坡直连、fal `kling-video/v3/standard/text-to-video` 静音中转）、逐 provider/currency 与 Kling `VIDEO_UNIT` 预算桶（共享每 Run 4 个新 Job）、`kling-auth-result-origin-v1`/`fal-auth-result-origin-v1` 认证结果来源绑定（精确 origin + 整条 URL 指纹、禁重定向、每次连接重新解析并固定公网地址）、tools/rules 6/6 与冻结 5/5 真实检查点恢复、Kling/fal Key 热更新与未验证标注、设置页供应商选择与逐供应商报价/上限。全部供应商调用均为受控 HTTP，视频 **submit=0**；真实账号样本留待 E4，详见 [E2 验收](./docs/M3_E2_ACCEPTANCE.md)。

**E3 已交付 headless 与用户 skills**：`vagent headless PROMPT` 只用分镜工具产出 `planned` 计划并冻结有序请求、provider/model/profile、能力/价格版本与逐桶估算；`vagent headless --plan RUN_ID --plan-fingerprint SHA --confirm --max-cost ... [--max-units ...] [--max-jobs N]` 在任何登记前逐桶核对预算，登记事务内再核对计划指纹、分镜 revision 与请求集合，然后复用 Job/媒体 Worker 等待并本地拼接交付；`--resume RUN_ID` 复用已保存授权，不增额度、不重新提交。`--json` 输出固定字段，退出码 0/2/3/4/5/6/130。默认 skills 发现改为「内置根 → `VAGENT_HOME/skills`」，用户重名让位内置、坏文件只诊断，显式 `VAGENT_SKILLS_DIR` 仍整体替换。详见 [E3 验收](./docs/M3_E3_ACCEPTANCE.md)。

**E4 已交付端到端验收与打包**：`scripts/evaluate_m3.py` 默认离线、`--live` 明确拒绝；6 个用例走真实生产代码路径（一句话 Kling/fal/Wan 多镜头、本地成片、重做后 v2、拒绝矩阵、headless 计划与显式授权交付），合计 21 次脚本模型调用、11 次受控提交、11 个镜头交付、6 个成片版本、1 次重做，视频 submit=0。安装态 wheel smoke 新增成片工作台与 6 用例套件（PASS，成片 418,395 字节/10.021289 秒，HTTP/CLI 重放通过）；受控浏览器 7/7（成片 v1/v2、播放/拖动/下载、Kling/fal Key 未验证标注、用量分桶、零页面错误）。零新增费用复用 M2 真实分镜与三个真实万相 MP4，安装态完成 4,501,422 字节/10.096549 秒成片并通过 42 项检查（既有集合逐字段保留、v4→v5 备份、HTTP、真实浏览器解码）。详见 [M3 验收](./docs/M3_ACCEPTANCE.md)。

**2026-10-22 完成 M5 E4 分集工作流验收**：`scripts/evaluate_m5.py` 默认离线、`--live` 明确拒绝；5 个用例（`fridge-notes` 18 镜头/7 场/7 条逐场成片、`rainy-cafe` 19 镜头 i2v 角色一致性、拒绝矩阵、20 任务集级桶、失败隔离与 v1→v2 重做）走真实生产代码路径，42 次受控提交、41 个交付、18 个成片版本、真实供应商与模型调用 0。新增 CLI `episode plan/save/show/generate`、Web 只读分集面板（`/api/episodes`、逐场成片播放/下载、角色一致性证据）与 `episode-state.js`；安装态 wheel 新增 M5 工作台与 5 用例套件（PASS），受控浏览器 5/5。真实样本用 `fridge-notes` 全篇完成 **18 次 Wan 真实提交（估算 54.00 CNY，实际账单 unknown）** 与 7 条逐场成片；文生视频 submit=18，文本模型调用=0，未重提、未使用 Kling/fal 真实账号。期间修复媒体 Worker 版本冲突丢失 prepared 进度、Web 快照缺少逐场成片投影与套件数据目录隔离三个缺陷。详见 [E4 验收](./docs/M5_E4_ACCEPTANCE.md)。

**2026-10-22 完成 M6 E0 契约冻结**：M6（成片后期：字幕、配音、配乐、转场、片头尾、720P/1080P 导出）的 7 个决策点已确认——TTS 用阿里云百炼 CosyVoice 并复用视频 Key、音乐仅 CC0/用户导入、默认软字幕（`mov_text` + 旁挂 SRT）且烧录为可选档、720P 为验收口径且 1080P 仅参数化上采样、接受供应商水印、包内附 OFL 中文字体、整集成片纳入 E3。冻结 schema 7 方向（`audio`/`subtitleTracks` + media `kind=audio`）、`AudioAsset`/`SubtitleTrack`/`FilmRender` 三个域对象、`subtitle-align-zh-v1` 与 `mix-zh-v1` 算法、工具面 9/9→13/13 与 18 个错误码。本阶段不改产品代码：视频 submit=0、模型调用=0、TTS 请求=0；基线快照 `docs/M6_E0_BASELINE.json` 记录 126 个契约模型哈希与包内 FFmpeg 7.1 的字幕/转场/响度能力（`ffprobe` 不存在，测量走 FFmpeg 输出）。详见 [M6 契约](./docs/M6_CONTRACTS.md) 与 [E0 验收](./docs/M6_E0_ACCEPTANCE.md)。


**2026-10-22 完成 M6 E1 字幕**：schema **7**（`audio`/`subtitleTracks` 两个空集合 + media `kind=audio`/`contractVersion=3`）与 v6→v7 逐字节备份迁移；`subtitle-align-zh-v1` 确定性对齐（终止/次级标点断句、每条 ≤48 字符/每行 ≤24、≥0.5 秒、0.05 秒间隙、绝不跨镜头；整数微秒最大余数分配，最后一条 cue 精确落在冻结时间线末端）；`subtitle_save`（省略 cues 生成、显式 cues 人工修正、相同 cues 重放）与 tools/rules **10/10**（冻结 9/9）；SRT/ASS 确定性生成、包内 OFL 字体（`VAgentSubtitleSC-400.ttf`，1,213,160 字节 / SHA-256 `3cb33efa…5606cb`）、软字幕（`mov_text` + 旁挂 SRT）、烧录与 `both`，以 `FilmVersion.render`/`FilmMedia` 追加只增不改的成片版本；`vagent subtitle show|save|export`、`/api/subtitles*` 与 Web 字幕区。60.256 秒成片验收中字幕零漂移、烧录帧与内置 FFmpeg + 包内字体的固定 SHA 一致；E1 全程视频 submit=0、模型调用=0、TTS 请求=0。详见 [E1 验收](./docs/M6_E1_ACCEPTANCE.md)。

**2026-10-22 完成 M6 E2 音频**：先按官方文档核实百炼 CosyVoice v2 的 HTTP 端点、请求/返回字段、音色、2 元/万字符与 RPM 180（音频结果 URL 24 小时有效），再交付 `tts-providers-v1`、离线确定性 `local-tts-v1`、复用视频 Key 的 CosyVoice 适配器（受控 `httpx`、禁用重定向、仅信任 `*.aliyuncs.com` 结果地址）、`TtsRequestV1`/`TtsResultV1` 与 `audio_generate`——显式 `confirm`、单条 ≤500 字符/每集 ≤2000 字符/40 次请求、价格已核实时单条 ≤0.10 CNY/每集 ≤0.40 CNY，任一超限整批拒绝、不部分提交；同一集同一 speaker 首次生成即冻结 provider/model/voiceId/speed（`VOICE_CONFLICT`），相同台词与音色重放复用原资产。`AudioAsset`/`AudioMedia` 由服务端 UUID 落盘并可读（Range/下载沿用媒体面）；`mix-zh-v1` 实现 voice 0 dB、BGM −18 dB 侧链 ducking（−20 dB/8/20 ms/400 ms）、SFX −6 dB、`amix normalize=0`、两遍 `loudnorm`（−16 LUFS/−1 dBTP/LRA 11）与 EBU R128 成品复测（超差返回 `LOUDNESS_OUT_OF_RANGE` 不交付）。tools/rules **11/11**（冻结 10/10，9/9 仍可恢复），`episode_read.audio` 返回音色表、绑定、额度与资产。E2 全程 **TTS 真实请求=0、视频 submit=0、模型调用=0**；CosyVoice 仍标注「文档支持，未做真实样本」，CLI/Web 音频面板与整集成片渲染留待 E3/E4。详见 [E2 验收](./docs/M6_E2_ACCEPTANCE.md)。

**2026-10-23 完成 M6 E3 转场/片头尾/导出/整集**：schema **8** 新增 `renders` 渲染任务集合（v7→v8 只加空集合、逐字节备份），`Film` 可分镜或分集、原生渲染装配 `ffmpeg-render-native-v1`；`film_render`/`render_read` 与 tools/rules **13/13**（冻结 11/11），`cut/fade/crossfade` 以 `xfade` overlap 定义有效时间线（0.1–1.0 秒、≤ 相邻 1/3），支持跨场整集、720P/1080P、标题卡（`drawtext` + 包内字体）、soft/burn/both、旁挂 SRT、voice 按 cue 定位、BGM/SFX 混音与两遍 `loudnorm`；逐场归一化实现单场失败隔离，MP4 结构/时长 ≤1 帧/字幕轨/响度/SHA 校验与同指纹幂等；新增 `vagent film render|renders`、`/api/renders`、`/api/films/{id}/versions/{v}/subtitles.srt` 与 Web 渲染区。离线验收以 8 场 19 镜头、19 条本地配音完成 **89.3 秒**整集成片，`blackdetect` 无黑帧、EBU R128 达标；E3 全程视频 submit=0、TTS 请求=0、模型调用=0。详见 [E3 验收](./docs/M6_E3_ACCEPTANCE.md)。

**2026-10-23 完成 M6 E4 整集成片验收**：`scripts/evaluate_m6.py` 默认离线、`--live` 拒绝；3 个用例（89.3 秒 8 场 19 镜头整集、单场失败隔离、拒绝矩阵）走真实 Store、字幕/配音工具、受控 Wan、Worker、渲染服务与包内 FFmpeg，56 次受控提交、56 个交付、1 个成片版本、1 次重放、27 条字幕，真实供应商/模型/TTS 调用均为 0。Web 分集面板新增整集播放器、逐场渲染状态、旁挂 SRT 下载与**字幕开关**（旁挂 SRT 客户端 overlay，原生 `mov_text` 隐藏），并在快照刷新时保留播放位置。复用 M5 E4 已批准的 `fridge-notes` 18 个真实镜头，离线生成 11 条配音与 19 条字幕，渲染 **89.1667 秒**整集成片（2 个转场、2 张标题卡、soft 字幕、−16.0 LUFS / −14.2 dBTP、无黑帧），新增视频 submit=0、费用 0。受控 Chrome 7/7（整集播放、字幕开/关/再开、Range 206、零页面错误）；安装态 wheel 新增 M6 3 用例与工作台。期间修复真实 AAC 时长导致的字幕/渲染时间线 1 µs 接缝、快照刷新重置整集播放器、Linux `imageio-ffmpeg` 无 `drawtext` 时标题卡不可渲染三个缺陷（标题卡改用同一包内字体的 ASS 烧录）；四组 CI 在 `2aac7a8` 上全部 success（[运行 39912715486](https://github.com/shifang37/vAgent/actions/runs/39912715486)）。详见 [E4 验收](./docs/M6_E4_ACCEPTANCE.md)。

**2026-10-23 完成 M7 E0 契约冻结**：M7（多集与工作台）的 9 个决策点已确认——季只保存有序 Episode 成员与标题、一次批量覆盖 1–3 集且逐集原子/失败隔离/显式恢复、每集一条只增不改的时间线（重排/裁剪/转场/字幕轨/音轨/标题卡）、时间线渲染复用 M4 归一化 + `ffmpeg-render-timeline-v1`、Web 优先且预览为客户端顺序播放、每季至多一个 running 批次、批量上限 60 Job/180.00 CNY、BGM/SFX 由客户端受控上传、E0–E3 submit=0。冻结 schema 9 方向（`seasons`/`batches`/`timelines` 三个空集合）、`Season`/`Batch`/`Timeline` 域对象、`timeline-zh-v1` 与 `timeline-subtitle-remap-v1`、工具面 13/13→18/18 与 13 个新错误码。本阶段不改产品代码：视频 submit=0、模型调用=0、TTS 请求=0；基线 `docs/M7_E0_BASELINE.json` 记录 165 个契约模型哈希与包内 FFmpeg 7.1 的裁剪探针（0.5 秒起裁 1.0 秒，实测 1.0 秒/30fps）。详见 [M7 契约](./docs/M7_CONTRACTS.md) 与 [E0 验收](./docs/M7_E0_ACCEPTANCE.md)。

**2026-10-23 完成 M7 E1 季与 schema 9**：schema **9** 新增 `seasons`/`batches`/`timelines` 三个空集合，v8→v9 逐字节备份迁移，v1–v8 全部可读；新增 `Season`/`SeasonVersion` 只增不改契约与 Store 提交校验（每项目 ≤8 季、每季 1–4 集有序且唯一、一集至多属于一个季的当前版本、版本连续）。`season_save` 新建或追加版本、相同内容重放、`expectedRevision` 乐观并发；`season_read` 返回季、成员集摘要与季列表。工具面 **15/15**（冻结 13/13，旧 prompt/规则字节不变），季保存全程不登记 Job、不调用模型或供应商。新增 `vagent season show|save`、`/api/seasons*` 与 Web 季面板（`season-state.js`，视频模式 off 也可用），文档、CI 与前端状态测试同步。详见 [E1 验收](./docs/M7_E1_ACCEPTANCE.md)。

**2026-10-23 完成 M7 E2 批量队列**：新增 `Batch`/`BatchEpisode`/`BatchEstimate` 与 `season_generate`（tools/rules **16/16**，冻结 15/15）。一次批次覆盖 1–3 集，逐集使用独立 Store 事务调用既有整集登记（键 `batch:<batchId>:<episodeId>`），单集失败只写该集错误码、其他集继续；预检 3 集 / 60 新 Job / 180.00 CNY，超限整批拒绝；每季同时至多一个 `running` 批次，每集仍受冻结的 20 Job / 60.00 CNY 约束。`resumeBatchId` 与 `episodeIds` 互斥，恢复只重登记旧批次中非 `succeeded` 的集并按当前版本创建 `resumeOf` 新批次，旧批次逐字节保留。Store 打开时把残留 `running` 批次收为 `partial`/`failed`，未完成成员写入 `BATCH_INTERRUPTED`；批次 ID 由调用身份确定性生成，重放同一调用或相同内容不重复登记。`season_read` 返回最近/指定批次的逐集登记状态、Job ID、估算、错误码与 `deliveredShots/failedShots/pendingShots`。新增 `GET /api/seasons/{id}/estimate`（只读预览）、`POST /api/sessions/{id}/seasons/generate`、CLI `vagent season generate` 与 Web 批量确认/失败隔离/恢复面板。全程视频 submit=0、模型调用=0、TTS 请求=0，详见 [E2 验收](./docs/M7_E2_ACCEPTANCE.md)。安装态 wheel 与 `scripts/wheel_smoke.py` 同步通过；修复 M6/M7 规则增长后 wheel smoke 的 live/M1-C 夹具上下文预算（产品默认 `VAGENT_CONTEXT_BYTES` 不变）；四组 CI 在 `e7ddb8c` 上全部 success（[运行 39954582630](https://github.com/shifang37/vAgent/actions/runs/39954582630)）。

**2026-10-23 完成 M7 E3 时间线工作台**：新增每集一条只增不改的 `Timeline`/`TimelineVersion`（`timeline-zh-v1`：有序 `clips` 含镜头内 `inMs/outMs`、`transitions`、整集字幕轨、`audio[]` 与标题卡；≤64 版本、≤20 镜头、≤19 转场、有效时长 1–90 秒、确定性指纹）与 `timeline_save`/`timeline_read`（tools/rules **18/18**，冻结 16/16）。`film_render` 增加 `scope=timeline`，输出装配字面量 `ffmpeg-render-timeline-v1` 与 `RenderSources.timeline`，按 clip 窗口本地 `-ss/-t` 裁剪、重排、`cut/fade/crossfade`、标题卡、720P/1080P、soft/burn/both；`timeline-subtitle-remap-v1` 先把整集字幕 cue 换算为镜头内偏移再按入点重映射（时间线外丢弃、窗口外夹取），旁挂 SRT 与成片同源重算；音轨以冻结 `audio[]` 为准，voice 按 cue 定位，BGM/SFX 按 `startMs/gainDb` 放置。BGM/SFX 由**客户端受控导入**（`POST /api/audio/import`、`vagent audio import`，WAV/MP4、≤25 MB、≤60 秒、强制许可与来源、按 SHA-256 去重），不是模型工具；`GET /api/audio`、`vagent audio list`。新增 `GET /api/episodes/{id}/timeline`、`GET /api/timelines/{id}`、`POST /api/sessions/{id}/timelines/save`、CLI `vagent timeline show|sources|save`、`vagent film render --timeline` 与 Web 时间线工作台（排序/裁剪/转场/字幕/音轨/标题卡、客户端顺序预览、保存与导出，视频模式 off 可用）。全程视频 submit=0、模型调用=0、TTS 请求=0，详见 [E3 验收](./docs/M7_E3_ACCEPTANCE.md)。回归中同时修复两处稳定性问题：未改动的 `running` 批次不再被 Store 误判为推进（避免并发 Job 轮询/渲染提交触发 `BATCH_CONFLICT`），`mix-zh-v1` 对近静音第一遍 `loudnorm` 的非有限测量值退回动态模式（避免 “Result too large”）。

**2026-10-23 完成 M7 E4 验收**：`scripts/evaluate_m7.py` 默认离线、`--live` 拒绝，3 个用例走真实 Store、季/批次/时间线/字幕/音频工具、受控 Wan、Worker、本地渲染与包内 FFmpeg：3 集批次登记 36 个 mock Job 并同调用重放；单集 `BATCH_EMPTY` 隔离后 `resumeBatchId` 只重登记失败集（新增 12 Job、旧批次逐字节保留）；14 镜头 67 秒时间线 v1→v2（交换前两镜、裁 1 秒、400 ms 叠化 → 65.6 秒）重拼，22 条字幕 cue 精确前移 1.4 秒，旧成片 SHA 与旧时间线版本不变，过期渲染在副本上返回 `TIMELINE_SOURCE_MISSING` 且不写成片。全程 14 次受控提交、14 个本地交付、96 个 mock Job、4 条批次、2 个时间线版本、2 个成片版本、−16.0 LUFS / −13.9 dBTP；真实视频 submit=0、文本模型调用=0、TTS 真实请求=0。受控 Chrome 155 在时间线面板完成「v2/14 镜头/65.6 秒 → 顺序预览 → 上移镜头保存 v3 → 导出成片 v3」闭环（Range 206、零页面错误）；安装态 wheel 新增 M7 3 用例与季/时间线工作台，并把安装态 M1-b 评估的测试预算提高到 96 KiB（修复 E3 推送后四组 CI 的 wheel smoke `CONTEXT_LIMIT`；产品默认 `VAGENT_CONTEXT_BYTES` 不变）；CI 补上 `render-state`/`timeline-state` 的 `node --check` 与 Node 测试。四组 CI 在 `760fed9` 上全部 success（[运行 40063039486](https://github.com/shifang37/vAgent/actions/runs/40063039486)，attempt 2；attempt 1 的 Windows 3.12 为 runner 通信中断后重跑）。详见 [M7 验收](./docs/M7_ACCEPTANCE.md) 与 `output/m7-e4-suite.json`。

| 部分 | 状态 | 已交付内容 |
|---|---|---|
| 01 Agent 核心 | 已实现 | 自定义 LangGraph model/tools 循环、DeepSeek 适配、CLI、项目与产物工具、版本化存储、执行限额 |
| 02 上下文与 Skills | 已实现 | 稳定前缀、确定性排序、紧凑 JSON、输入预算、完整轮次裁剪、项目事实注入、按需读取 SKILL.md、内容版本记录 |
| Python 迁移 | 已实现并通过本地验证 | Python 源码与 pytest 测试、pip 安装、wheel 打包、Python CI；替换原 TS/Node 工程 |
| 03 持久图恢复 | 已实现 | SQLite 检查点、显式 resume、外部等待自动继续、累计预算、停止竞争与工具重放保护 |
| 03 用量观测 | 已实现 | 单次模型调用记录、缓存命中/未命中 Token、加权命中率、未知用量标记 |
| 03 Redis 回答缓存 | 已实现 | 显式只读模式、最终文本精确匹配、TTL、故障回退、独立命中统计 |
| 03 任务评测 | A5 完整真实验收通过 | 9 类用例、独立状态评分、上下文版本/预算对比、用量覆盖率、显式检查点续跑 |
| 04 本地 Web | 已接入真实 Agent | 独立文本/视频 Key 设置、模型切换与验证、逐段文本流、断线补齐、停止/恢复、产物与视频播放下载 |
| MCP | 已实现并验证 stdio | 显式只读白名单、工具发现、Schema 校验、取消/超时、结果大小限制 |
| 内容质量校验 | 已通过回归与真实纠错验收 | 记忆字段职责、旧事实残留检查、持久字数上限、保存前计数、未纠正错误禁止报告完成 |
| M1-B 模拟视频 | B0–B5 已验收 | 自动 Worker、四工具、持久等待/停止/恢复、CLI/Web；离线与真实 DeepSeek + Mock 均 5/5 通过，详见 [M1-B 验收](./docs/M1B_ACCEPTANCE.md) |
| M1-C 真实视频 | C0–C3.2 已完成；真实单镜头已交付本地 MP4 | schema v3 兼容、Wan HTTP、双 Key 配置、本地媒体校验/恢复、Range 与播放器、离线评测与安装媒体流程、真实联调（查询协议修复与暂停恢复）；详见 [C2 验收](./docs/M1C_C2_ACCEPTANCE.md)与 [M1-C 验收](./docs/M1C_ACCEPTANCE.md) |
| M2 分镜多镜头 | D0–D4 已验收并合入 master（`8776482`，含真实成本受控多镜头联调） | D0 契约冻结（i2v/首尾帧能力与价格、图片 Base64 输入、费用三重上限、集合等待语义）；D1 schema v4、结构化分镜域与 storyboard 工具、execution v3 与 v3→v4 迁移；D2 多镜头批量登记、三重上限、集合等待、逐镜头重做与 live 能力表 v2（`wan27-shot-beijing-v2`，受控 HTTP，submit=0）；D3 素材库导入/去重/恢复与只读读取、用量聚合、CLI 命令与 Web 分镜工作台（分镜面板、批量确认、序列连播、素材库页、用量面板）；详见 [M2 任务规划](./docs/M2_PLAN.md)、[D1 验收](./docs/M2_D1_ACCEPTANCE.md)、[D2 验收](./docs/M2_D2_ACCEPTANCE.md) 与 [D3 验收](./docs/M2_D3_ACCEPTANCE.md)；D4 新增 `scripts/evaluate_m2.py`（默认离线的 M2 端到端套件：live 模式 + 受控上游 + 本地测试媒体 + 加速夹具时钟，7 用例覆盖分镜草案、批量登记与去重、集合等待与逐镜头交付、部分失败隔离、逐镜头重做、三重上限与未确认拒绝，10 次提交、9 个镜头本地交付、2 次重做、submit=0）与 `tests/test_m2_evaluation.py` 26 项，安装态 wheel smoke 增加 M2 工作台与离线套件（PASS），受控浏览器 7/7 阶段 0 失败；真实成本受控多镜头联调完成（2 镜头 + 1 次单镜头重做，submit=3、估算 9.00 CNY，三个真实 MP4 本地交付并经浏览器播放、连播与下载哈希核验），详见 [D4 验收](./docs/M2_ACCEPTANCE.md) |
| M3 E1 本地拼接成片 | 已交付（离线、submit=0） | schema v5 与 v4→v5 机械迁移（精确字节备份，E0 冻结的 v5 预期哈希由真实迁移复现）；`mp4-copy-signature-v1`（轨道/codec 配置/时间线/edit list/priming 逐字段校验，未知结构拒绝）；按样本表推导接缝时长与偏移（禁止 FFmpeg 静默修正 DTS）；包内 `imageio-ffmpeg==0.6.0` 流复制、120 秒上限、可取消、锁外执行与锁内复核后一次提交；`Film`/`FilmVersion`/`FilmMedia` 只增不改；`film_assemble`/`film_read` 与 tools/rules 5/5（按 toolFeatures 恢复 4/4）；`vagent film assemble|show`；Web 成片面板与 Range/下载；详见 [E1 验收](./docs/M3_E1_ACCEPTANCE.md) |
| M3 E2 多供应商与中转 | 已交付（受控 HTTP、submit=0） | Job/Request v6 与 `multi-provider-v3`（Kling 直连、fal 中转，各一条 t2v/5 秒/720p/16:9）；逐 provider/currency 与 Kling `VIDEO_UNIT` 预算桶、共享 4 个新 Job；认证结果来源绑定、精确 origin/URL 指纹、禁重定向与公网连接固定；tools/rules 6/6 与冻结 5/5 恢复；多供应商 Key 热更新、未验证标注、设置页与用量分桶；详见 [E2 验收](./docs/M3_E2_ACCEPTANCE.md) |
| M3 E3 headless 与用户 skills | 已交付（离线、submit=0） | `vagent headless` 两阶段计划/授权/集合等待/本地拼接交付；计划冻结有序规范请求、provider/model/profile、能力/价格版本与逐桶估算，凭 Run ID/计划指纹/`--confirm`/`--max-cost`/`--max-units`/`--max-jobs` 授权固定集合；登记事务内原子核对计划、分镜 revision、请求与所有预算桶；`--resume` 复用原授权且不重新提交；`--json` 固定字段与退出码 0/2/3/4/5/6/130；`VAGENT_HOME/skills` 合并发现、内置名优先、坏文件隔离、显式根整体替换；详见 [E3 验收](./docs/M3_E3_ACCEPTANCE.md) |
| M3 E4 端到端验收与打包 | 已交付（离线/安装态/浏览器/真实数据均 submit=0；新增付费样本未执行） | `scripts/evaluate_m3.py` 6 用例（Kling/fal/Wan 一句话成片、重做后 v2、拒绝矩阵、headless 计划与显式授权交付；21 次脚本模型调用、11 次受控提交、11 个镜头交付、6 个成片版本、1 次重做）；`tests/test_m3_evaluation.py` 31 项含 22 类评分器反例；wheel smoke 成片工作台（418,395 字节/10.021289 秒、HTTP/CLI 重放、多供应商设置）与安装态 6 用例 PASS；受控浏览器 7/7；零新增费用用 M2 真实分镜与三个真实万相 MP4 完成 4,501,422 字节/10.096549 秒成片，42 项检查含 v4→v5 备份、既有集合逐字段保留、HTTP Range 与真实浏览器解码；修复 `film_assemble` 执行上下文、`film-state.js` 静态资源、设置页供应商切换与 AAC `esds` 码率归一化四个缺陷；四组 CI 通过；详见 [M3 验收](./docs/M3_ACCEPTANCE.md) |
| M4 时间线归一化/转码 | 已实现（`master` 开发版，未打 tag） | 流复制优先；`mp4-copy-signature-v1` 不兼容时自动回退到 `ffmpeg-concat-normalize-v4`：1280×720/30fps/H.264 + AAC 48k 立体声，无音轨补静音；成片版本记录 `normalization` profile；真实 i2v+flf2v 两段合成 **5,968,116 字节 / 10.0 秒** 成片；详见 [M4 归一化](./docs/M4_NORMALIZATION.md) |
| M5 E0 契约冻结 | 已交付（schema v6 外壳 + 域契约，submit=0、模型调用=0） | `Episode`/`Scene`/`Character`/`Style` 严格契约与 Store 提交校验器；场景至多绑定一个分镜（1:1）；v5→v6 只加四个空集合、逐字节备份；每集 10–20 镜头/60–90 秒/20 Job/60.00 CNY（旧每 Run 4 Job/12 CNY 只约束 6/6 及更早工具面）；`text-split-zh-v1` 确定性切分契约与错误码；**不含 9:16 竖屏**；v5 合成基线覆盖 M4 归一化成片 + 两代 4/4 与 6/6 工具签名；详见 [M5 契约](./docs/M5_CONTRACTS.md) 与 [E0 验收](./docs/M5_E0_ACCEPTANCE.md) |
| M5 E1 确定性文本改编 | 已交付（离线，模型调用=0、submit=0） | `text-split-zh-v1`：章节/场景切分、对白零丢失抽取（条数与正文引号逐一相等）、角色识别、镜头草案与逐场/逐镜头预算（60–90 秒、10–20 镜头、每场≤6）；台词冻结进 `SceneVersion.dialogue[]`；5 个工具 `episode_plan`/`episode_save`/`episode_read`/`bible_save`/`bible_read` 与 tools/rules **7/7**（冻结 6/6 变体供旧 Run 恢复）；3 篇固定中文样例 969–1017 字、18–20 镜头、11–22 条台词可复现；详见 [E1 验收](./docs/M5_E1_ACCEPTANCE.md) |
| M5 E2 角色/风格一致性 | 已交付（离线，模型调用=0、submit=0） | `storyboard_save` 用 `sceneId` 一次绑定场景（1:1）；镜头有序 `characterIds`（i2v 1 个、flf2v 2 个）由服务端映射到角色最新版本的第一个参考图，并冻结 `{characterId, characterVersion}`；缺图/跨项目/非图像整批拒绝（`CHARACTER_ASSET_MISSING`），不降级 t2v；`style-prompt-zh-v1` 固定风格模板 + `【出场】` 角色名进入请求提示词；`episode_read` 返回逐角色跨镜头证据与 `consistent`；`consistency_review` 只记录用户给出的 1–5 分（`CONSISTENCY_EVIDENCE_MISSING` 拒绝无镜头证据），评分只增不改；工具面 **8/8**（冻结 7/7）；14 项定向用例覆盖素材顺序、篡改拒绝、版本混用与评分重放；详见 [E2 验收](./docs/M5_E2_ACCEPTANCE.md) |
| M5 E3 生成与重做 | 已交付（受控 HTTP + 本地测试媒体，视频 submit=0） | `episode_generate` 整集批量登记：事务内重算分集→场记→分镜→镜头，任一场记未绑定分镜（`SCENE_CONFLICT`）或分镜镜头数/时长与冻结场记不一致（`EPISODE_SHOT_BUDGET`/`EPISODE_DURATION_LIMIT`）即整批拒绝；集级额度按冻结 `shotBudget` **跨 Run 累计**（`jobCap` 20 / `episodeCapAmount` 60.00 CNY，逐镜头 0.60 CNY/秒），失败与 `unknown` 仍占额度、不换汇（Kling/fal 的 USD 报价整批拒绝）；`RunLimitsSnapshot` 新增可选 `scope=episode`，Store 每次提交重算集级任务数与估算并核对 `shotRef` 归属、币种与单价，篡改返回 `JOB_CONFLICT`/`EPISODE_COST_LIMIT`；`shot_redo` 只追加目标镜头的新 ShotVersion 与新 Job；`await_job` 集合等待上限 16→20；`episode_read.generation` 给出逐镜头 `unregistered/pending/delivered/failed` 与集级余量；工具面 **9/9**（冻结 8/8）；12 项定向用例覆盖 12 镜头/60 秒全链路交付、单镜头失败隔离与重做修复、跨 Run 额度、submit 与预算一致；详见 [E3 验收](./docs/M5_E3_ACCEPTANCE.md) |
| M5 E4 分集工作流与验收 | 已交付（离线/安装态/浏览器，真实样本 18 次 Wan 提交、估算 54.00 CNY、实际账单 unknown，文本模型调用 0） | `scripts/evaluate_m5.py` 5 用例（`fridge-notes` 18 镜头/7 场/7 条逐场成片、`rainy-cafe` 19 镜头含 i2v 与一致性评分、超长/未绑定/确认/镜头数/时长/缺素材拒绝矩阵、20 任务集级桶与重做拒绝、上游失败隔离与 v1→v2）；`tests/test_m5_evaluation.py` 10 项；CLI `episode plan/save/show/generate` 与 `clients.generate_episode`；Web `/api/episodes`、会话快照逐场成片投影、只读分集面板、`episode-state.js` 5 项测试；安装态 wheel M5 工作台 + 5 用例 PASS（42 次受控提交、41 个交付、18 个成片版本、2 次重做）；受控浏览器 5/5（7 场、18/18、54.00/60.00 CNY、成片 1280×720 解码与 Range）；真实样本 7 条逐场成片与浏览器证据；修复媒体 Worker 版本冲突丢失 prepared 进度、Web 快照缺少成片投影与套件数据目录隔离三个缺陷；详见 [E4 验收](./docs/M5_E4_ACCEPTANCE.md) |
| M6 E0 契约冻结 | 已交付（文档 + 基线，submit=0、模型调用=0、TTS 请求=0） | 7 个决策点冻结（百炼 CosyVoice 复用视频 Key、音乐仅 CC0/用户导入、默认软字幕 + 可选烧录、720P 验收且 1080P 仅上采样、接受水印、包内 OFL 字体、整集纳入 E3）；schema 7 方向（`audio`/`subtitleTracks` + media `kind=audio`，`contractVersion=3`）；`AudioAsset`/`AudioAssetVersion`/`SubtitleTrack`/`SubtitleTrackVersion`/`FilmRender` 契约；`subtitle-align-zh-v1`（台词→cue、≤24 字符/行、0.5 秒下限、0.05 秒间隙、不跨镜头）与 `mix-zh-v1`（−16 LUFS/−1 dBTP、ducking；两遍 `loudnorm`）；工具面 9/9→10/10→11/11→13/13 且保留全部冻结变体；TTS 每集 2000 字符、单次 500 字符、40 次请求；18 个错误码；基线 126 个契约模型哈希 + FFmpeg 7.1 能力探针（中文烧录、`mov_text` 软字幕、响度测量实测通过，`ffprobe` 不存在）。见 [M6 契约](./docs/M6_CONTRACTS.md) 与 [E0 验收](./docs/M6_E0_ACCEPTANCE.md) |
| M6 E1 字幕 | 已交付（离线，submit=0、模型调用=0、TTS 请求=0） | schema 7（`audio`/`subtitleTracks` + media `kind=audio`）与 v6→v7 逐字节备份迁移；`SubtitleTrack`/`SubtitleTrackVersion`/`SubtitleCue` 只增不改、目标唯一、cue 校验（不重叠/不倒序/不跨镜头/≤48 字符/每轨 ≤200/每场 ≤40）；`subtitle-align-zh-v1`（整数微秒、规范镜头窗口、字符比例分配、≥0.5 秒 + 0.05 秒间隙）；`subtitle_save` 与 tools/rules **10/10**（冻结 9/9）；SRT/ASS 与 `srtSha256`/`assSha256`；包内 OFL 字体（子集 3,876 字符 / SHA-256 `3cb33efa…5606cb`，缺失返回 `FONT_MISSING`）；`subtitle_service` 的 soft/burn/both 导出（`ffmpeg-render-v1`、`FilmVersion.render`、720P、音频复制、指纹幂等、锁外可停止）；`mp4-avc-v1` 接受 `mov_text` 轨；`vagent subtitle show|save|export`、`/api/subtitles*`、Web 字幕区与 `subtitle-state.js`；60.256 秒样例字幕零漂移、烧录帧固定 SHA、人工修正重渲染、重叠/越界/缺轨拒绝矩阵；详见 [E1 验收](./docs/M6_E1_ACCEPTANCE.md) |
| M6 E2 音频 | 已交付（离线，TTS 真实请求=0、视频 submit=0、模型调用=0） | 官方核实 CosyVoice v2（共享/业务空间域名、`format=wav`/`sample_rate=48000`、`usage.characters`、2 元/万字符、RPM 180、结果 URL 24 小时）；`tts-providers-v1`（live 为 `cosyvoice-v2` 8 个已核实音色，mock 为 3 个本地音色）、`local-tts-v1` 确定性合成与 CosyVoice 适配器（受控 HTTP、禁重定向、仅信任 `*.aliyuncs.com`）；`audio_generate` 显式确认、500/2000/40 限额、0.10/0.40 CNY 金额上限、同集同 speaker 音色绑定（`VOICE_CONFLICT`）、相同请求复用资产、整批拒绝；`AudioAsset`/`AudioMedia` 真实落盘与读取；`mix-zh-v1`（voice 0 dB、BGM −18 dB ducking、SFX −6 dB、`amix normalize=0`、两遍 `loudnorm` + EBU R128 复测、超差 `LOUDNESS_OUT_OF_RANGE`）；tools/rules **11/11**（冻结 10/10）、`episode_read.audio`；36 项定向用例与离线 `episode→TTS→mix→MP4` 验收；CosyVoice 保持「文档支持，未做真实样本」，CLI/Web 与整集渲染留待 E3/E4；详见 [E2 验收](./docs/M6_E2_ACCEPTANCE.md) |
| M6 E3 转场/片头尾/导出/整集 | 已交付（本地 FFmpeg，submit=0、TTS 请求=0、模型调用=0） | schema **8**（`renders` 任务集合，v7→v8 逐字节备份）；`Film` 分镜/分集二选一、`ffmpeg-render-native-v1` 与 `episodeVersion`；`film_render`/`render_read`（tools/rules **13/13**，冻结 11/11）；`cut/fade/crossfade` 有效时间线（只裁出镜尾部、0.1–1.0 秒且 ≤ 相邻 1/3）、跨场整集、cue 夹取与旁挂 SRT；标题卡 `drawtext`（≤4，包内字体）；720P/1080P（1080P `upscaled=true`）、逐场归一化 + 单场失败隔离；voice 按字幕 cue 定位、BGM/SFX 混音与两遍 `loudnorm` 写入 `render.audio.loudness`；MP4 结构/时长 ≤1 帧/字幕轨/响度/SHA 校验与同指纹幂等；CLI `film render|renders`、`/api/sessions/{id}/films/render`、`/api/renders`、`/api/films/{id}/versions/{v}/subtitles.srt` 与 Web 渲染区；89.3 秒整集验收无黑帧、19 条配音与 EBU R128 达标；详见 [E3 验收](./docs/M6_E3_ACCEPTANCE.md) |
| M6 E4 整集成片验收 | 已交付（复用 M5 真实镜头，新增 submit=0、TTS 请求=0、模型调用=0） | `scripts/evaluate_m6.py` 3 用例（89.3 秒 8 场 19 镜头整集、单场失败隔离、拒绝矩阵）与 `tests/test_m6_evaluation.py` 4 项；Web 分集面板整集播放器、逐场渲染状态、旁挂 SRT 与字幕开关（客户端 overlay、快照刷新保留播放位置），`episode-state.js` 新增 3 项前端测试；真实镜头复用 `fridge-notes` 18 镜头 → 11 条本地配音/19 条字幕 → **89.1667 秒**整集（2 转场、2 标题卡、soft、−16.0 LUFS / −14.2 dBTP、无黑帧、20,600,359 字节）；受控 Chrome 7/7（播放、字幕开/关/再开、Range 206、零页面错误）；安装态 wheel M6 3 用例与工作台；修复 AAC 时间线 1 µs 接缝、快照刷新重置播放器与 Linux 无 `drawtext` 的标题卡实现；详见 [E4 验收](./docs/M6_E4_ACCEPTANCE.md) |
| M7 E0 契约冻结 | 已交付（文档 + 基线，submit=0、模型调用=0、TTS 请求=0） | 9 个决策点冻结（季只保存有序成员、批量 1–3 集且逐集原子/失败隔离/显式恢复、时间线只增不改、`ffmpeg-render-timeline-v1`、客户端预览、每季一个 running 批次、60 Job/180.00 CNY、客户端素材导入、E0–E3 submit=0）；schema 9 方向（`seasons`/`batches`/`timelines` 三个空集合）；`Season`/`SeasonVersion`、`Batch`/`BatchEpisode`、`Timeline`/`TimelineVersion`/`TimelineClip` 契约；`timeline-zh-v1`、`timeline-subtitle-remap-v1`、30 fps 网格与 ±1 帧容差；工具面 13/13→15/15→16/16→18/18 且保留全部冻结变体；13 个新错误码；基线 165 个契约模型哈希 + 包内 FFmpeg 7.1 裁剪探针（1.0 秒/30fps、无告警）。见 [M7 契约](./docs/M7_CONTRACTS.md) 与 [E0 验收](./docs/M7_E0_ACCEPTANCE.md) |
| M7 E1 季与 schema 9 | 已交付（离线，submit=0、模型调用=0、TTS 请求=0） | schema **9** 的 `seasons`/`batches`/`timelines` 三个空集合与 v8→v9 逐字节备份迁移；`Season`/`SeasonVersion` 只增不改与 Store 提交校验（每项目 ≤8 季、每季 1–4 集有序唯一、一集至多属于一个季的当前版本）；`season_save`（新建/追加/相同内容重放/`expectedRevision` 乐观并发）与 `season_read`（季、成员集摘要、季列表、`batch=null`）；tools/rules **15/15**（冻结 13/13，旧规则字节不变）；`vagent season show|save`、`/api/seasons*`、Web 季面板与 `season-state.js`（off 模式可用）；详见 [E1 验收](./docs/M7_E1_ACCEPTANCE.md) |
| M7 E2 批量队列 | 已交付（离线，submit=0、模型调用=0、TTS 请求=0） | `Batch`/`BatchEpisode`/`BatchEstimate` 与 Store 校验（目标/成员/指纹冻结、终态不可改写、≤64 条/项目、revision 只 +1、成员状态机）；`season_generate` 1–3 集、逐集独立事务与失败隔离、3 集/60 Job/180.00 CNY 预检、每季一个 `running` 批次、显式 `resumeBatchId` 恢复 `resumeOf` 新批次；Store 打开崩溃恢复 `BATCH_INTERRUPTED`；确定性批次 ID 与同内容重放；`season_read` 批次交付计数；CLI `season generate|show --batch`、`/api/seasons/{id}/estimate`、`/api/sessions/{id}/seasons/generate`、Web 批量确认/预览/恢复与 `season-state.js` 7 项新测试；tools/rules **16/16**（冻结 15/15）；详见 [E2 验收](./docs/M7_E2_ACCEPTANCE.md) |
| M7 E3 时间线工作台 | 已交付（离线，submit=0、模型调用=0、TTS 请求=0） | `Timeline`/`TimelineVersion`/`TimelineClip`/`TimelineTransition`/`TimelineSubtitleRef`/`TimelineAudioTrack`/`TimelineTitleCard`（`timeline-zh-v1`，≤64 版本、≤20 镜头、≤19 转场、1–90 秒）与 Store 校验（每集唯一、指纹、只增不改；打开不重解析镜头来源）；`timeline_save`/`timeline_read`；`film_render` 的 `scope=timeline`、`ffmpeg-render-timeline-v1`、`RenderSources.timeline`、`RenderTask.timelineId/timelineVersion`、`timeline-subtitle-remap-v1`、按 `inMs/outMs` 裁剪/重排/转场/标题卡/720P/1080P；冻结 `audio[]` 音轨（voice 按 cue、BGM/SFX 按 startMs/gainDb）；客户端 BGM/SFX 导入（WAV/MP4、≤25 MB、≤60 秒、许可与来源、SHA-256 去重）；CLI `timeline show|sources|save`、`audio list|import`、`film render --timeline`；`GET /api/episodes/{id}/timeline`、`/api/timelines/{id}`、`POST /api/sessions/{id}/timelines/save`、`/api/audio*`、Web 时间线工作台与 `timeline-state.js` 13 项测试；tools/rules **18/18**（冻结 16/16）；详见 [E3 验收](./docs/M7_E3_ACCEPTANCE.md) |
| M7 E4 验收 | 已交付（离线 + 受控浏览器 + 安装态，复用本地媒体；真实 submit=0、模型调用=0、TTS 请求=0） | `scripts/evaluate_m7.py` 3 用例（3 集 36 Job 批量/重放、单集 `BATCH_EMPTY` 隔离与显式恢复、14 镜头 67→65.6 秒时间线 v1→v2 重拼与字幕 1.4 秒精确重映射、过期/非法编辑拒绝）与 `tests/test_m7_evaluation.py` 4 项；`output/m7-e4-suite.json` 记录 96 个 mock Job、4 条批次、1 次恢复、14 次受控提交、2 个时间线版本、2 个成片版本、22 条 cue、−16.0 LUFS；受控 Chrome 时间线面板编辑/预览/导出 v3（Range 206、零页面错误）；wheel smoke 新增安装态 M7 3 用例与季/时间线工作台；详见 [M7 验收](./docs/M7_ACCEPTANCE.md) |
| M8 E0 契约冻结 | 已交付（文档 + 基线，submit=0、模型调用=0、TTS 请求=0、凭据读取=0） | D1–D8 冻结：只读建议 + 用户显式切换、不换汇（USD/CNY 分账）、手工录入账单、逐 provider 间隔 2.0 秒 + 429 冷却 60 秒且**任何发送后自动重投都不做**、4 个新错误码（`ROUTE_UNAVAILABLE`/`PROVIDER_BACKOFF`/`BILL_RECORD_INVALID`/`BILL_CONFLICT`）、账单为 `home/billing.json` 侧车（schema 仍 9）、v0.6.0 发布、明确不做 9:16；基线 168 个契约模型哈希 + `capabilitiesSha256` + 两套工具/规则哈希 + `routeProbe`（默认上限下 wan 5 秒可用、10 秒 `VIDEO_COST_LIMIT`、CNY→USD 分组、wan→kling→fal 并列）；详见 [M8 契约](./docs/M8_CONTRACTS.md) 与 [E0 验收](./docs/M8_E0_ACCEPTANCE.md) |
| M8 E1 路由预览 | 已交付（离线，视频 submit=0、模型调用=0、TTS 请求=0、凭据读取=0） | `provider-route-v1` 纯函数（每供应商一行、8 级 `reason` 优先级、CNY→USD→金额→wan→kling→fal 排序、`quoteFingerprint`/`fingerprint` 无时钟）；按分镜解析当前版本并逐镜头判定/求和（跨条目时明确注明行内取第一个镜头的 model/priceVersion）；有效单 Job 上限按当前配置计算（wan 可调高、kling/fal 只能收紧）；`vagent route preview`、`GET /api/route/preview`、Web 报价对比对话框（分镜面板「报价对比」、分集面板「集级报价对比」，只读卡片 + `sourceUrl`/`checkedAt` + 「不换汇」，切换只写既有 `PATCH /api/config`）；真实预览复现 E0 `routeProbe.rowsSha256 = 3817d191…`；63 项后端与 6 项前端用例；**工具面 18/18、schema 9、168 个模型哈希不变**；详见 [E1 验收](./docs/M8_E1_ACCEPTANCE.md) |
| M8 E2 运维与账单 | 已交付（离线，视频 submit=0、模型调用=0、TTS 请求=0、凭据读取=0） | `providers/gate.py`：`SubmitGate` 逐 provider 提交最小间隔 **2.0 秒**（2.0–60.0）与 429 冷却 **60 秒**（60–900，`Retry-After` 只延长且 900 秒上限、截断时写入诊断），`PROVIDER_BACKOFF` 只写 `runtimeBlock`（`pending_submit` + `submitAttempts=0` + 不发请求），**任何发送后的自动重投都不做**；`SUBMIT_ERROR_TABLE`/`QUERY_ERROR_TABLE` 把 §5.1/§5.2 映射写成常量；`billing.py`：`billing-v1` 侧车账本（`home/billing.json`、0600、一条记录一次原子替换、幂等/`BILL_CONFLICT`/`BILL_RECORD_INVALID`/损坏 `INVALID_STORE` 且保留原文件、≤4096 条/项目、≤4 MB）；`usage.billing` 逐币种估算/已录入/差额（`unknown` 不计 0、不换汇、无跨币种合计）；`GET /api/usage?runtime=1` 暴露内存态延迟/退避/限流计数（`durable:false`）；`vagent usage bill record\|show`、`POST/GET /api/usage/bill`、Web 用量面板账单区与 `bill-state.js`；37 项后端（+1 POSIX skip）与 7 项前端用例；**工具面 18/18、schema 9、168 个模型哈希不变**；详见 [E2 验收](./docs/M8_E2_ACCEPTANCE.md) |
| M8 E3 真实样本 | 已交付（单独授权的真实请求；视频 submit=0、Kling/fal 0 请求） | CosyVoice v2 对整集 19 条台词（170 字符）配音 19 次请求，19 个 48 kHz 单声道 WAV（3,148,676 B）、FFmpeg 解码与 Range 206 通过，估算 0.0340 CNY、实际账单 `unknown`；真实 DeepSeek 1 Run、4 次模型调用（`episode_plan`→`episode_save`→`episode_read`）、输入 96,617 / 输出 2,176 Token；Kling/fal 缺凭据未执行；修复 CosyVoice 流式 WAV 占位长度与默认 64 KiB 预算装不下 18/18 工具面两个缺陷；详见 [E3 验收](./docs/M8_E3_ACCEPTANCE.md) |
| M8 E4 验收与发布 | 已完成（离线 + 受控浏览器 + 安装态；真实视频 submit=0、模型调用=0、TTS=0，未新增付费样本） | `scripts/evaluate_m8.py` 4 用例（18 行路由矩阵 + 4 行冻结探针、拒绝矩阵、账单与错误映射、重放不新增）与 `tests/test_m8_evaluation.py` 4 项；`output/m8-e4-suite.json` 记录 38 次预览、6 次受控提交、7 个 Job、3 次账单写入、3 次重放、1 条账本记录；受控 Chrome 155 报价对比 + 账单重放（6 步、3 截图、零页面错误）；wheel smoke 新增 M8 评估与 CLI/HTTP 三面；`pyproject.toml`/`__version__` 升到 **0.6.0**，发布说明见 [v0.6.0](./docs/RELEASE_0.6.0.md)；详见 [M8 验收](./docs/M8_ACCEPTANCE.md) |

2026-10-10 完成 C0–C2：固定北京 `wan2.7-t2v-2026-06-12`、5 秒 720P 16:9，官方参考生成费为 3.00 元，采用业务空间专属域名。应用支持 off/mock/live，任务只提交一次，按原 ID 持久查询；云端成功后由独立媒体 Worker 下载，完整文件与索引提交后才进入 `succeeded`。下载重试、重启和媒体修复沿用原 Job/mediaId；Web 可播放、拖动进度和下载。账号视频权限、实际上游媒体与账单未验证，本次未调用收费模型。接口依据及完整阶段范围见 [官方核实](./docs/VIDEO_API_NOTES.md)、[冻结契约](./docs/M1C_CONTRACTS.md)和[任务规划](./docs/M1C_PLAN.md)。2026-10-15 完成 C3.1：`scripts/evaluate_m1c.py` 离线套件 7/7 通过，仓库外安装 wheel 通过含 live 协议夹具、下载中重启、CLI 与媒体 API 证据的完整 smoke；真实提交数保持 0。2026-10-16 完成 C3.2 真实联调：真实 DeepSeek 提交 1 个万相任务（北京 5 秒 720P，生成约 140 秒），应用下载并交付本地 MP4（3,183,756 字节、1280×720/H.264/AAC，浏览器播放、拖动与下载校验通过）；期间修复真实响应的 `usage.SR` 数字形态（`9a49d71`），查询暂停后经 `vagent jobs retry-query` 复用原任务完成交付，全程 submit=1；估算 3.00 元、实际账单未知。C3.1/C3.2 证据见 [M1-C 验收](./docs/M1C_ACCEPTANCE.md)。2026-10-16：M1 全部完成并合入 master（`cc3628e`）；M2（分镜多镜头工作流）进入实施：D0 完成接口核实与契约冻结（[M2 契约](./docs/M2_CONTRACTS.md)；submit=0），D1 完成 schema v4 与结构化分镜域——新增 `storyboards`/`assets` 集合的机械迁移（历史对象逐字节保留）、稳定 Shot ID 与只增不改的分镜版本、`storyboard_read`/`storyboard_save`（mock，工具与规则 4/4）、execution v3 与 pre-M2 Run 原签名恢复；离线全量回归 839 passed / 2 skipped，submit=0。证据见 [D1 验收](./docs/M2_D1_ACCEPTANCE.md)，任务全貌见 [M2 任务规划](./docs/M2_PLAN.md)。2026-10-17：D2 完成多镜头批量登记、三重上限、集合等待与逐镜头重做，并把 live 切换到 4/4 与 `wan27-shot-beijing-v2`（受控 HTTP 驱动冻结协议，视频 submit=0；离线全量 868 passed / 2 skipped），证据见 [D2 验收](./docs/M2_D2_ACCEPTANCE.md)；D3 完成素材库（导入、去重、文件恢复与只读读取）、用量统计读取侧、CLI 命令与 Web 分镜工作台（分镜面板、批量确认对话框、客户端序列连播、素材库页、用量面板），并修复 D2 遗留的两个交付缺陷（`JobResultV3` 与 M2 真实 Job 视图 500），离线全量 917 passed / 2 skipped、受控浏览器 7/7 阶段 0 失败、安装态 wheel smoke PASS，证据见 [D3 验收](./docs/M2_D3_ACCEPTANCE.md)。2026-10-17 完成 D4 的离线与打包部分：`scripts/evaluate_m2.py` 离线端到端套件 7/7（受控上游驱动冻结的 `wan-http-v2`，覆盖批量原子登记与去重、集合等待与逐镜头本地交付、部分失败隔离与逐镜头重做、三重上限与未确认拒绝；10 次提交、9 个镜头交付、2 次重做，视频 submit=0），`tests/test_m2_evaluation.py` 26 项（含评分器反例、显式续跑与预算边界），安装态 wheel smoke 扩展到 M2 工作台（分镜/素材 CLI 与 HTTP 面）与离线套件并 PASS，受控浏览器 7/7 阶段 0 失败，离线全量 **943 passed / 2 skipped**；证据见 [M2 验收](./docs/M2_ACCEPTANCE.md) 与 `output/m2-d4-*.json`。真实双镜头加一次单镜头重做的受控联调（submit≤3、估算≤9.00 CNY）已于同日完成：真实 DeepSeek 走 `video_capabilities → project_read → storyboard_save → shot_generate → await_job`（5 次调用、38,958/1,333 Token、覆盖率完整），一次登记两个镜头（合计估算 6.00），两镜头与一次重做共 **3 次提交全部一次即成**，三个真实 MP4（1280×720 / 5.03 秒 / H.264+AAC，1.93 / 2.54 / 1.96 MB）本地交付；重做只新增目标镜头的版本与任务，其他镜头的 Job 与已交付媒体哈希不变；浏览器播放、序列连播与逐媒体下载哈希校验通过；真实结果主机 `dashscope-a717.oss-accelerate.aliyuncs.com` 仍在 `wan-result-hosts-v2` 允许集内，估算合计 9.00 元、**实际账单未知**。2026-10-17：M2 全部完成并合入 master（`8776482`）；M3 于当日进入规划，并于 2026-10-21 完成 E0：本地拼接、Kling/fal 最小接入、headless 两阶段授权与用户 skills 契约已冻结，E1–E4 已实施，见 [M3 任务规划](./docs/M3_PLAN.md) 与 [E0 验收](./docs/M3_E0_ACCEPTANCE.md)。两 Key 直用的独立小轨道（K0–K3）K0 已于 2026-10-17 冻结，见 [两 Key 直用方案](./docs/KEYS_ONLY_PLAN.md)；K1（契约与服务端）同日完成：V4/V5 契约与分派、适配器按记录形态选主机（共享域名与业务空间专属域名）、keys-only live 校验与恢复、受控 HTTP 全链路与混合目录重启用例，离线全量 947 passed / 2 skipped，视频 submit=0，见 [K1 验收](./docs/KEYS_ONLY_K1_ACCEPTANCE.md)。K2（Web 与探测）于 2026-10-18 完成：设置页最小表单（两个 Key 与模式开关）与「高级（可选）」折叠、保存视频 Key / 业务空间的零费用只读探测（鉴权通过与未验证分列）、首次配置自动切 live 的限定触发、视频配置免重启对新登记生效（含业务空间后补与移除的端点热切换）；受控浏览器 6 阶段 0 失败（探测 3 次 GET、生成前零提交零任务，唯一一次提交带新 Key 打向共享域名并本地交付），离线全量 958 passed / 2 skipped，视频 submit=0，见 [K2 验收](./docs/KEYS_ONLY_K2_ACCEPTANCE.md)。K3（验收与打包）于 2026-10-18 完成：wheel smoke 增加 keys-only 共享域夹具（仅两 Key 保存、零费用探测、免重启自动 live、共享域提交与本地交付，覆盖媒体 API 与 CLI 核验），本地安装态 smoke PASS（含十三项检查）、离线全量 958 passed / 2 skipped、四组 CI 全部通过；并以安装在仓库外独立环境的 wheel 完成共享域真实单镜头样本（`wan2.7-t2v-2026-06-12`，5 秒/720P，submit=1、估算 3.00 CNY，任务 `ad3a4a68-6849-44a8-9b68-1cca1c6877be`，结果主机 `dashscope-a717.oss-accelerate.aliyuncs.com` 仍在 `wan-result-hosts-v2` 允许集内，实际账单 unknown），见 [K3 验收](./docs/KEYS_ONLY_K3_ACCEPTANCE.md)。

**2026-10-22 补做万相 i2v/flf2v 真实样本**：`wan2.7-i2v-2026-04-25` 在北京 5 秒 720P 下，首帧图生视频（task `0520a9b5…`）与首尾帧生视频（task `6add91d2…`）各一条真实任务成功并交付本地 MP4（6,109,743 B / 5,991,195 B，均 1280×720 H.264+AAC 5.033 秒）；结果主机在允许集内，估算各 3.00 CNY、实际账单 unknown。两条输出编码与分辨率相同，但时间线相差超过一帧，**copy-only** 的 `film assemble` 当时返回 `FILM_INPUT_INCOMPATIBLE`；随后 M4 加入自动转码回退，当前 `film assemble` 已能把两条合成 **5,968,116 字节 / 10.000 秒 / 30fps** 的成片，并记录 `ffmpeg-concat-normalize-v4` 与 profile。详见 [i2v/flf2v 验收](./docs/M2_I2V_ACCEPTANCE.md) 与 [M4 归一化](./docs/M4_NORMALIZATION.md)。

每个独立完成的代码部分都同步更新本 README、提交并推送 GitHub。阶段目标见 [M1 计划](./M1_PLAN.md)、[M2 任务规划](./docs/M2_PLAN.md)、[M3 任务规划](./docs/M3_PLAN.md)、[M4 归一化](./docs/M4_NORMALIZATION.md)、[M5 短剧流水线规划](./docs/M5_PLAN.md)、[M6 契约](./docs/M6_CONTRACTS.md)、[M7 契约](./docs/M7_CONTRACTS.md)、[M8 计划](./docs/M8_PLAN.md)、[两 Key 直用方案](./docs/KEYS_ONLY_PLAN.md) 和 [Harness 设计](./AGENT_HARNESS_DESIGN.md)。

## 本地 Web 与真实编排

保留 DeepSeek Harness 风格，已移除浏览器中的模板回复。会话、项目记忆和产物由 Python 服务保存；侧栏「编排观测」查看上下文字节预算、裁剪数、记忆 revision、Skills、MCP 与实际调用用量。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
# 可在启动后的设置页填写 Key，也可使用本地 .env。
$env:VAGENT_HOME = Join-Path $PWD '.vagent/web'
.\.venv\Scripts\python.exe -m vagent web --mcp-local --no-open
```

打开 [本地工作台](http://127.0.0.1:3210)。缺少 Key 时自动打开配置向导；保存不调用模型，「保存并验证」最多发送一次简短请求，可能产生少量费用。发送创作需求会调用真实 DeepSeek。`--mcp-local` 启用镜头时长和帧数计算服务；外部 stdio 配置见 [MCP 说明](./docs/MCP.md)。

服务只监听 `127.0.0.1`，校验 Host/Origin 和写请求 CSRF Token；静态资源使用白名单，API 不返回 Key。同一数据目录只允许一个 CLI/Web 写进程。文本流实时显示为草稿，完整回复通过检查后才持久保存；断线重连补齐当前文本，停止或失败时丢弃草稿。完整边界见 [Web 说明](./web/README.md)。

在工作台运行时，可显式执行固定任务验收（会产生模型费用）：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --live
# 仅重新检查已有产物，不调用模型：
.\.venv\Scripts\python.exe scripts/evaluate_agent.py --review-existing
```

此前首组真实调用共 13 个模型步骤、17 次工具调用；输入 71,645、输出 3,498 Token。原始报告保存在被忽略的 `output/agent-live-acceptance.json`，可复现步骤和发现见 [验收记录](./docs/AGENT_ORCHESTRATION_ACCEPTANCE.md)。

新的评测无需启动 Web，使用同一个 ApplicationService/Runner 和内置 MCP。默认离线夹具只验证工程链路，不代表模型质量：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --output output/m1a-offline.json
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --context-version 1 --compare output/m1a-offline.json --output output/m1a-v1.json
# 显式真实请求；全套最多32次模型调用，任一用例失败立即停止：
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --live --max-model-calls 32 --output output/m1a-live.json
# 仅在最近失败仍可恢复时显式继续，原报告保留，预算不增加：
.\.venv\Scripts\python.exe scripts/evaluate_m1a.py --live --continue-from output/m1a-live.json --output output/m1a-resumed.json
```

每次新评测使用独立数据目录，报告记录路径。已有输出文件不会覆盖。可用 `--context-bytes` 比较预算；报告列出状态正确性、耗时、首段文字时间、模型/工具次数、已知 Token、缓存命中及统计覆盖率。离线或用量缺失时不计算 Token 差值，不将单组结果外推为节费收益。

本次通过的完整报告为 `output/m1a-a5-20261009-v2-r4-resumed-3.json`。最终 brief 三个版本为 286、261、113 字，storyboard 为 241 字，旧版保持不变；只读任务未修改项目或产物。最终套件 27 次模型调用中 24 次有完整用量，另外记录 8 次请求发送前的连接重试；四轮复验合计 55 次模型调用，失败与未知用量均纳入记录。

## 记忆一致性与字数上限

`goal` 只保存创作目的和核心信息，`audience` / `style` 分别保存受众和风格。更新时检查目标中的字段重复，以及目标/约束中残留的旧受众、旧风格；操作日志中的历史项目快照也用于检查旧数据。发现冲突返回 `MEMORY_CONFLICT`，整次更新不落盘、revision 不变。模型需要在同一次更新中修正相关字段；历史产物正文不会自动改写。

正文的“字数”统一采用**非空白 Unicode 字符数**：汉字、英文、数字、标点、Markdown 标记均计入，空格和换行不计，标题不计。支持明确的数字上限，例如：

```text
保存一份 brief，正文300字以内。
方案不超过100字，分镜不超过300字。
brief 改为500字以内。
brief 取消字数限制。
```

后端在接收写作请求时，把上限写入项目 `contentLimits` 并更新 revision；同一请求 ID 不会重复修改，只读请求不修改它。上限跨轮次、跨重启保留，普通工具不能放宽或删除；未指定种类的正文上限是项目默认值。标题、纯回复要求、代码块、引用行和 Web 标记的附件参考材料不用于设置正文上限。

`artifact_save` 在写入版本前计数。超限返回实际计数和 `CONTENT_LENGTH`，不创建产物或新版本；成功返回并保存 `contentCheck`。Web 展示上限、实际计数和保存时的校验记录。旧版本缺少记录时明确显示“未记录字数校验”，不会补造通过结果。

校验错误需由模型在原执行预算内纠正；若没有纠正便直接结束，Run 返回 `QUALITY_UNRESOLVED`，不采用模型的成功声明。固定任务评测重新读取正文计数，有质量问题时返回失败退出码。当前检查针对明确数字上限和字段原值的文本重复/残留，不承诺任意自然语言约束或同义改写的完整语义判断。详见 [质量校验验收](./docs/QUALITY_ACCEPTANCE.md)。

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

也可在 Web 设置中填写 Key 和模型。页面配置原子保存到 `VAGENT_HOME/config.yml`，优先级为**内置默认值 < 本地 config.yml < 环境变量/.env**，CLI 也读取同一文件。设置页显示来源；由启动配置或环境变量提供的字段不能在页面覆盖。页面保存对下一次请求生效；修改环境变量或 `.env` 后仍需重启服务。

密钥输入框始终留空，留空保持已存值；移除密钥需要单独操作。凭证不进入状态、检查点、事件或浏览器存储。本地配置是明文文件，POSIX 写入权限为 0600，Windows 使用所在目录的访问控制。运行或验证期间不能修改配置，验证期间不能启动新 Run；验证成功状态只在当前服务进程内保留。

```powershell
.\.venv\Scripts\python.exe -m vagent config show
.\.venv\Scripts\python.exe -m vagent run '准备咖啡店短视频方案，面向上班族，暖色调，并保存。' --session coffee
.\.venv\Scripts\python.exe -m vagent run '改成雨夜氛围，保留受众设定和原版。' --session coffee
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
.\.venv\Scripts\python.exe -m vagent chat --session coffee
```

`chat` 中输入 `/exit` 退出，Ctrl+C 停止当前进程的 Agent。`run` 和 `chat` 会发起真实请求并产生 API 费用。候选模型 `deepseek-flash` 可配置，账户权限与线上兼容性仍需真实 Key 验证；首版关闭 thinking。

`run` 支持 `--request-id`：同一会话相同 ID、相同需求返回已有运行记录；相同 ID 对应不同需求会报错。失败与中断任务也不自动重跑；继续原执行使用 `resume`，发起独立的新尝试才使用新 ID。

## 模拟视频工具与持久等待（M1-B）

默认关闭，启动前设置 `VAGENT_VIDEO_MODE=mock` 可注册模拟视频工具。模式和来源可在 CLI 配置输出、Web 设置页和配置 API 查看。C1 起也可将视频配置保存到 `config.yml`；K2 起视频配置保存后免重启、对新登记立即生效（在途与历史任务按各自冻结配置恢复），设置页默认只留两个 Key 与模式开关，高级字段折叠；off/mock/live 各有独立工具视图。

```powershell
$env:VAGENT_VIDEO_MODE = 'mock'
.\.venv\Scripts\python.exe -m vagent config show
```

| 工具 | 当前行为 |
|---|---|
| `video_capabilities` | 读取适配器提供的模型、能力版本和完整合法规格组合 |
| `video_generate` | 只在本地登记 Job，返回 jobId 与登记状态；每个 Run 最多一个新 Job，不在工具中提交上游 |
| `job_get` | 读取当前项目的本地 Job 快照；同一调用重放原结果，新调用读取最新状态 |
| `await_job` | 终态立即返回；未结束则保存持久等待，由执行图挂起（execution v2/v3），结果就绪后回填原工具调用 |
| `storyboard_read` | M2 D1 起（mock）：读取当前项目的结构化分镜、当前版本有序镜头与各镜头版本摘要；只读，跨项目拒绝 |
| `storyboard_save` | M2 D1 起（mock）：新建或修改结构化分镜，校验来源/素材引用与上限，产生新版本并保留旧版本，返回服务端稳定 Shot ID |
| `shot_generate` | M2 D2 起（mock/live）：为已保存分镜的指定镜头批量登记任务，必须显式确认，回显逐镜头与合计估算、已用与剩余额度；任一镜头校验失败整批不登记，同一分镜版本同一镜头的相同请求复用原任务 |
| `asset_read` | M2 D3 起：只读列出素材库参考图元数据（assetId、来源项目、格式、尺寸、导入时间）；不含本地路径、私有 URL 或指纹，也不能导入或删除 |
| `film_assemble` | M3 E1 起（tools/rules 5/5）：按当前分镜版本把每个镜头的最新已交付 MP4 本地流复制拼接为单一成片；必须提供 `expectedRevision`，任一镜头未交付或编码不兼容整批拒绝；同输入复用原版本，锁外执行、可停止，不登记 Job、不产生费用 |
| `film_read` | M3 E1 起：只读返回当前成片、历史版本、逐输入镜头与有理数时间线偏移，以及受控媒体 URL；不拼接、不创建任务 |
| `subtitle_save` | M6 E1 起（tools/rules 10/10）：按冻结台词与时间线确定性对齐或保存人工 cue；只增不改、无 Job、无费用 |
| `season_save` | M7 E1 起（tools/rules 15/15）：保存季标题与 1–4 个有序分集 ID；每次有效修改追加 `SeasonVersion`，旧版本逐字节保留；相同内容重放同一版本；不登记 Job、不调用模型、不产生费用 |
| `season_read` | M7 E1 起（E2 扩展）：只读季、成员集摘要（版本、场数、镜头数、时长）、全部季列表与最近/指定批次的逐集登记状态、Job ID、交付计数与错误码；不登记、不提交 |
| `season_generate` | M7 E2 起（tools/rules 16/16）：为季的 1–3 集逐集原子登记，必须显式 `confirm`；单集失败只标记该集；预检 3 集/60 Job/180.00 CNY；每季一个进行中批次；显式 `resumeBatchId` 恢复；只登记，不提交、不等待 |
| `timeline_save` | M7 E3 起（tools/rules 18/18）：保存每集一条只增不改的 `TimelineVersion`——有序 `clips`（镜头内 `inMs/outMs` 裁剪）、`transitions`、整集字幕轨、`audio[]`、标题卡与 `expectedRevision`；有效时长 1–90 秒，相同内容重放、修改追加版本；只引用已登记 `ShotVersion`，不登记 Job、不调用模型、不产生费用 |
| `timeline_read` | M7 E3 起：只读当前/指定版本、逐条裁剪与源时长、镜头交付状态、转场、字幕/音轨/标题卡、指纹、有效时长与 `stale`；不登记、不提交 |
| `audio_generate` | M6 E2 起（11/11）：按集生成 voice 资产，显式确认、字符/次数/金额限额、音色绑定与复用；不登记视频 Job |
| `film_render` | M6 E3 起（13/13）；M7 E3 扩展 `scope=timeline`（18/18）：本地渲染整片，支持 `cut/fade/crossfade`、标题卡、720P/1080P、soft/burn/both、整集与时间线；`scope=timeline` 的剪辑/转场/字幕/音轨全部取自冻结版本，`ffmpeg-render-timeline-v1`、`timeline-subtitle-remap-v1`；显式确认、锁外执行、无 Job、无供应商费用 |
| `render_read` | M6 E3 起：只读渲染任务逐场状态、失败阶段、测量与预估耗时；不重新渲染 |

新 mock Run 自 M2 D1 起使用 execution v3；v1/v2 运行仍按原路径恢复。`AgentRunner.run()` 在持久中断保存后返回 `waiting_external`，释放图执行任务；已完成工具保留，未完成调用不伪造成功。`WaitCoordinator` 扫描持久结果并续接原调用，`ApplicationService` 已管理协调器启动、关闭及启动补偿。等待仍占用唯一未结束 Run 名额，新消息返回 `RUN_BUSY`。

用户停止会先持久关闭自动继续，并补齐可见的终止工具结果；Job 继续独立跟踪。正常退出保留等待意图，重启只自动继续有效等待。结果之后已经开始过模型尝试的中断要求显式 `resume`；缺少原配置、检查点或预算时保留结果，原因记录在 `waitResumeError`。

`ApplicationService` 统一管理 JobWorker、媒体 Worker 和等待协调器。`run/chat/resume` 等待原 Run 完成，图挂起期间 Worker 仍独立推进；`chat` 输入不会阻塞 Worker，也不留下阻止 Windows 退出的输入线程。缺少恢复配置时显示原因并保存结果。旧 B2 execution v1 仍按 `EXTERNAL_WAIT_UNAVAILABLE` 结束未完成等待，其 preparing 记录不会被后台唤醒。

以下 Job 命令不需要 DeepSeek Key：

```powershell
vagent jobs list
vagent jobs list --session coffee
vagent jobs get JOB_ID
vagent jobs retry-query JOB_ID
vagent jobs retry-download JOB_ID
vagent jobs work
```

分镜、素材与用量命令同样不需要 DeepSeek Key，也不启动模型、Worker 或 MCP：

```powershell
vagent storyboard show [-s PROJECT] [--storyboard STORYBOARD_ID]
vagent shots redo SHOT_ID [-s PROJECT] [--request-id REQUEST_ID]
vagent assets list [-s PROJECT]
vagent assets import PATH [PATH ...] [-s PROJECT]
vagent usage --project [PROJECT]
vagent usage bill record --job JOB_ID --amount 0.56 --currency USD --source "Kling 控制台 2026-10-25 账单" --confirm
vagent usage bill show [--project PROJECT] [--currency CNY|USD]
vagent route preview --mode t2v --duration 5
vagent route preview --storyboard STORYBOARD_ID [--version N] [--shot SHOT_ID ...] [--scope shot|episode|batch]
```

`storyboard show`、`assets list`、`usage --project` 只读本地状态。`shots redo SHOT_ID` 由镜头 ID 解析所属分镜与项目，为该镜头新增一个 `ShotVersion` 并登记一个新 Job，其他镜头的版本、Job 与媒体不动；它只登记、不提交上游，由 Web 或 `jobs work` 继续推进。省去 `--request-id` 时每次调用都是一次新的重做；带上该参数即可让重试幂等（同一请求 ID 重放原结果，不重复登记）。`assets import` 校验魔数/尺寸/宽高比/大小后按 `(项目, SHA-256)` 去重，命中即复用原素材；同一张图片重新导入还能恢复被删除或损坏的素材文件。`usage --project` 汇总整个数据目录（按项目聚合则给出项目 ID），只读取 Jobs、估算、账单状态、分镜、素材与模型 Token 覆盖率，不做外部调用。`route preview` 只读比较冻结能力表：每个供应商一行，给出金额与币种、可用性与原因、Key 是否配置、价格来源与核对日期；CNY 与 USD 只并列展示、**不换汇**，集级/批量只接受 CNY，`--require-available` 在没有可用候选时返回 `ROUTE_UNAVAILABLE`；它不登记 Job、不调用供应商、不调用模型、不读取 Key 内容。

`usage bill` 只写 `VAGENT_HOME/billing.json` 侧车账本（不进 `state.json`，因此 schema 仍是 9）：只接受已终态 Job 的手工 reported actual，币种必须等于该 Job 冻结报价的币种，`--confirm` 缺失时返回 `CONFIRMATION_REQUIRED` 且不写盘；相同内容重放返回 `replayed=true`，金额/币种/日期/来源/备注任一不同返回 `BILL_CONFLICT`，更正需要 `--replace --expected-revision N`；Job 不存在或在途、字段非法、条数/大小超限为 `BILL_RECORD_INVALID`；账本损坏时报 `INVALID_STORE` 且保留原文件。`usage --project` 与 `GET /api/usage` 追加 `billing` 块（逐币种估算/已录入/差额，未录入为 `null` 而不是 0，跨币种不合计），`GET /api/usage?runtime=1` 追加内存态 `runtime` 块（提交/查询延迟 p50/p95、退避与限流计数，固定 `durable:false`）。

`list/get` 读取本地状态并核对媒体可用性，发现文件缺失或损坏时保存新的可用性 revision；不发起上游 HTTP。`retry-query` 只恢复原任务查询窗口，`retry-download` 只登记原媒体的恢复窗口；这四个命令均不启动 Worker、模型或 MCP。`jobs work` 或 Web 持续推进查询、下载与有效等待；缺少原模型配置时继续推进 Job，保留等待结果。启动模式控制新 Run 的工具，已登记 mock Job 即使在 off 模式启动也继续跟踪。

CLI 退出后没有后台守护进程；重新运行 Web 或 `jobs work` 才继续推进队列。`run/chat/resume` 的 Ctrl+C 停止当前 Agent，`jobs work` 的 Ctrl+C 只关闭本地循环并保留等待意图。同一数据目录被 Web 占用时，CLI 提示 `STORE_LOCKED`。

Web 提供 `GET /api/jobs`（可选 `sessionId`）、`GET /api/jobs/:id`、`POST /api/jobs/:id/retry-query` 和 `POST /api/jobs/:id/retry-download`。会话快照含当前项目 Job；`job.updated` 绑定创建 Job 的会话/Run，客户端按 jobId 与 revision 去重，溢出和重连通过快照补齐。任务卡片显示模拟标记、模型/参数、来源版本、生成/查询/下载状态；Run 结束后仍更新，等待时可停止 Agent，查询暂停或下载失败时可恢复原任务。

只读任务隐藏并拒绝 `video_generate`，允许读取已有 Job 和保存等待记账。视频工具可见时，整次 Run 跳过 Redis 回答缓存，包括第一次模型调用之前；供应商前缀缓存 Token 统计保持。新 Run 保存模式与工具/能力配置，恢复时核对；旧 off Run 即使在 mock 启动配置下恢复，也使用原系统规则与工具集合。

## 本地成片与 headless（M3）

成片与 headless 都是本地动作，不调用供应商、不登记生成额度。`vagent film assemble` 用包内 FFmpeg 把当前分镜版本每个镜头的最新已交付 MP4 流复制拼接为单一成片；`vagent film show` 只读当前成片、历史版本和受控媒体地址。`vagent headless` 面向脚本化/无浏览器场景，依次完成「计划 → 显式授权 → 集合等待 → 本地拼接交付」。

```powershell
# 1) 只规划：调用编排模型保存结构化分镜（可能产生文本费用），不登记、不提交任何视频任务
vagent headless "做一支 15 秒的雨夜咖啡店短片" -s coffee --json

# 输出含 runId、planFingerprint、storyboardId/version/revision、estimates[]（逐桶金额与 Kling VIDEO_UNIT）与 limits。
# 2) 核对镜头、供应商、能力/价格版本和逐桶估算后，显式授权并执行：
vagent headless --plan RUN_ID --plan-fingerprint SHA --confirm `
  --max-cost kling:USD=1.12 --max-units kling=8 --max-jobs 2 --json

# 3) 等待超时（退出码 5）或进程中断后继续；沿用原授权，不增额度、不重新提交：
vagent headless --resume RUN_ID --json
```

- **两阶段授权**：`--plan` 只接受已经产出的计划；`--plan-fingerprint` 必须匹配，`--confirm` 必须显式给出，`--max-cost` 必须覆盖计划中的每个金额桶（Kling 还要 `--max-units`），可用 `--max-jobs` 收紧共享的 4 个新任务上限。缺项、未知桶、低于计划估算、超过服务端上限或计划后分镜 revision 变化都会在任何登记前拒绝。
- **原子核对**：授权指纹和预算覆盖先持久化；登记事务内再次核对计划指纹、当前分镜 revision、逐镜头请求指纹、能力/价格版本与所有预算桶，一项不符则整批不登记。授权后、登记前崩溃时，`--resume` 使用原授权补齐登记。
- **等待与交付**：登记后复用 `JobWorker`/`MediaWorker` 与既有终态规则等待全部镜头；任意镜头失败返回退出码 4，等待到期返回 5 并保留 jobIds；全部交付后复用本地成片服务拼接，输出经过 resolver 校验的绝对路径、SHA-256 与大小。`--resume` 不重新提交，分镜或镜头版本在登记后变化会返回 6。
- **结构化输出**：`--json` 时 stdout 只输出一个对象，字段固定为 `formatVersion,status,projectId,sessionId,runId,planFingerprint,storyboardId,storyboardVersion,storyboardRevision,jobIds,filmId,filmVersion,output{mediaId,path,sha256,sizeBytes},estimates,limits,error`；过程日志在 stderr。成功用 `status` 区分 `planned`/`delivered`。
- **退出码**：`0` 计划成功或已交付，`2` 参数/领域校验，`3` 授权/预算拒绝，`4` 上游/下载/拼接失败，`5` 等待超时，`6` 恢复条件变化，`130` 用户中断。无 TTY 不交互、不默认同意。
- headless 规划 Run 使用收窄的分镜工具集（不能登记 Job 或拼接），其普通 `vagent resume` 会因工具集不同而被拒绝；继续 headless 流程请使用 `vagent headless --resume`。真实编排模型仍可能产生文本费用；计划阶段与既有 CLI 回归均保持视频 submit=0。

## 本地字幕（M6 E1）

字幕保存与导出同样是本地动作：不调用供应商、不登记视频 Job、不产生费用。`subtitle save` 省略 `--cues` 时按该场已冻结的 `SceneVersion.dialogue[]` 与冻结时间线做 `subtitle-align-zh-v1` 确定性对齐，显式 `--cues` 时按 JSON 数组做人工修正（整数毫秒，只校验不重排）；`subtitle export` 把某一确定字幕版本软封装（`mov_text` + 旁挂 SRT）或烧录到当前成片，并追加一个只增不改的成片版本（`render` 记录 profile、字体指纹、测量与渲染指纹）。

```powershell
vagent subtitle show  -s coffee --storyboard STORYBOARD_ID
vagent subtitle save  -s coffee --storyboard STORYBOARD_ID --request-id sub-1
vagent subtitle save  -s coffee --storyboard STORYBOARD_ID --track TRACK_ID `
  --expected-revision 0 --cues cues.json --request-id sub-2
vagent subtitle export -s coffee --track TRACK_ID --mode soft --request-id export-1
vagent subtitle export -s coffee --track TRACK_ID --mode burn --request-id export-2
```

- `show/save` 返回字幕轨、版本、逐条 cue、时间线来源（`film`/`storyboard`）与受控 SRT/ASS 地址；`subtitle_save` 工具与 CLI 共用同一领域写入，相同 cues 重放同一版本，修正追加新版本。
- `export` 只接受与源成片版本逐段相等的字幕时间线（否则 `RENDER_TIMELINE_MISMATCH`）；E1 只支持 720P 与分镜作用域，1080P/整集导出返回 `RENDER_PROFILE_UNSUPPORTED`/`RENDER_SOURCE_MISSING`。
- 烧录只使用包内 OFL 字体（`VAgentSubtitleSC-400.ttf`，SHA-256 固定）；字体缺失或被替换返回 `FONT_MISSING`，不会退回系统字体。重叠/倒序/越界/超限的 cue 分别返回 `SUBTITLE_CUE_CONFLICT`/`SUBTITLE_OVERFLOW`，整次拒绝且旧版本保留。

## 本地季与批量（M7 E1/E2）

季只是成员关系：它保存标题与 1–4 个有序分集 ID，不复制角色/风格，也不拥有场记、分镜、预算、Job 或成片。`season save` 每次有效修改追加一个 `SeasonVersion`，旧版本逐字节保留；相同标题与相同顺序重放同一版本。一个项目最多 8 个季，一集至多属于一个季的当前版本；换季要先在原季的新版本中移除，再加入目标季。季操作不登记任务、不调用模型或供应商，视频模式为 `off` 也可用。批量登记只接受该季当前版本的 1–3 集，逐集独立事务、单集失败隔离；每季同时至多一个进行中批次。

```powershell
vagent season show -s coffee
vagent season show -s coffee --season SEASON_ID
vagent season show -s coffee --season SEASON_ID --batch BATCH_ID
vagent season save -s coffee --title "第一季" `
  --episode EPISODE_ID_1 --episode EPISODE_ID_2 --request-id season-1
vagent season save -s coffee --season SEASON_ID --title "第一季" `
  --episode EPISODE_ID_2 --episode EPISODE_ID_1 `
  --expected-revision 0 --request-id season-2
vagent season generate -s coffee --season SEASON_ID --confirm --request-id batch-1
vagent season generate -s coffee --season SEASON_ID --episode EPISODE_ID_1 --episode EPISODE_ID_2 --confirm
vagent season generate -s coffee --season SEASON_ID --resume BATCH_ID --confirm
```

- `show` 返回季、当前版本、`revision`、成员集摘要（版本、场数、镜头数、目标时长）、最近批次与全部季/批次列表；省略 `--season` 时读取本项目最新创建的季，`--batch` 指定要读取的批次。
- `save` 修改时必须带 `seasonId` 与 `season show` 返回的 `revision`；缺失或过期返回 `SEASON_VERSION_CONFLICT`。标题为空、超过 4 集或超过 8 个季返回 `SEASON_LIMIT`；重复成员、跨项目分集或已属于另一季返回 `SEASON_EPISODE_CONFLICT`；未知季返回 `SEASON_NOT_FOUND`。
- `generate` 必须显式 `--confirm`；`--episode` 省略时用全部成员（季有 4 集时会因超过批次上限返回 `BATCH_LIMIT`）。`--resume BATCH_ID` 与 `--episode` 互斥，只重登记旧批次中非 `succeeded` 的集并创建 `resumeOf` 新批次；每季已有进行中批次返回 `BATCH_CONFLICT`。批次只登记，不提交、不等待。
- Web 季面板（侧栏「季」）提供季选择、新建、标题编辑、有序成员勾选（点击顺序即季内顺序，最多 4 集）与 `批量生成` 区块：勾选 1–3 集后先预览逐集镜头数/新任务/估算，再确认登记；批次卡片显示逐集状态、Job ID、交付计数与错误码，`partial/failed` 时可显式「恢复失败集」。预览走 `GET /api/seasons/{id}/estimate`，登记走 `POST /api/sessions/{id}/seasons/generate`，季详情为 `/api/seasons/{id}?projectId=`。
- E2 已实现批次，`season_read.batch` 为最近批次（或 `batchId` 指定），`batches` 为摘要列表；时间线工作台属 E3，见下一节。

## 本地时间线工作台（M7 E3）

时间线把已保存镜头重新排序、裁剪，并绑定转场、整集字幕轨、音轨与标题卡。每集恰好一条时间线，版本只增不改；`timeline save` 修改时必须带读到的 `revision`，相同内容重放同一版本。时间线只引用已登记的 `ShotVersion`（未交付也可保存排期），不复制媒体、预算或分镜描述；分集版本变化后 `stale=true`，渲染拒绝并返回 `TIMELINE_SOURCE_MISSING`。保存与读取不登记 Job、不调用模型或供应商，视频模式 `off` 也可用。

```powershell
vagent timeline sources -s coffee --episode EPISODE_ID
vagent timeline save -s coffee --episode EPISODE_ID --clips clips.json `
  --transitions transitions.json --subtitle-track TRACK_ID --subtitle-version 1 `
  --audio audio.json --title-cards titles.json --request-id timeline-1
vagent timeline save -s coffee --episode EPISODE_ID --timeline TIMELINE_ID `
  --clips clips2.json --subtitle-track TRACK_ID --subtitle-version 1 `
  --expected-revision 0 --request-id timeline-2
vagent timeline show -s coffee --episode EPISODE_ID
vagent timeline show -s coffee --timeline TIMELINE_ID --version 2
vagent audio import rain.wav -s coffee --kind bgm --title "雨声" `
  --source-url "https://example.com/rain" --license "CC0" --request-id audio-1
vagent audio list -s coffee
vagent film render -s coffee --timeline TIMELINE_ID --confirm --request-id render-1
```

- `clips` 为有序 JSON 数组：`shotId`、`shotVersionId`、`storyboardId`、可选 `sceneId`、`inMs`、`outMs`（镜头内整数毫秒；单条 ≥1 秒、不超过镜头时长）。`transitions` 为 `afterShotId`/`type`/`durationMs`；转场 0.1–1.0 秒且不超过相邻任一裁剪后镜头时长的 1/3，最后一个镜头之后不能有转场。有效时长 = 裁剪时长和 − 转场和，必须 1–90 秒。
- 字幕只能引用本集 `scope=episode` 的字幕轨与版本；有 voice 音轨时必须同时提供字幕轨。音轨的 `voice` 起点固定 0（按重映射 cue 定位），BGM/SFX 的 `startMs` 必须在有效时间线内，`gainDb` 范围 −60..+12。标题卡最多 4 条，不重叠、不越界。
- BGM/SFX 只能经 `vagent audio import` 或 Web 上传导入（WAV/MP4、≤25 MB、≤60 秒，必须填 `sourceUrl`/`license`，按 SHA-256 去重）；模型只看到 `audioId`，没有音频导入工具。
- `film render --timeline` 本地导出时间线版本（可选 `--timeline-version`），输出 `ffmpeg-render-timeline-v1` 与 `render.sources.timeline`；字幕按 `timeline-subtitle-remap-v1` 重映射，旁挂 SRT 由同一逻辑重算；时间线外的 cue 丢弃，裁剪窗口外夹取。全程不登记 Job、不调用供应商；同指纹返回原成片，失败不提交任何版本。
- Web 时间线工作台（侧栏「时间线」或分集面板「打开时间线工作台」）提供排序/裁剪/转场/字幕/音轨/标题卡编辑、客户端顺序预览（按裁剪窗口播放源镜头，不合成）、保存与导出，以及 BGM/SFX 上传；`timeline-state.js` 的纯函数负责预览与保存前的本地校验。

## 万相真实协议与本地媒体（M1-C C1/C2）

C1/C2 已实现任务登记、一次提交、原任务查询和本地媒体交付，普通回归使用受控 HTTP 响应与本地生成的测试视频。运行 live 会发出收费生成请求；云端成功先进入 `downloading`，文件校验与媒体索引提交完成后才返回本地结果。真实账号与付费验收在 C3。

视频 Key 独立于 DeepSeek Key。在本机 `.env` 或 `VAGENT_HOME/config.yml` 中配置，环境变量优先。以下为视频字段及默认值：

| 环境变量 | 本地配置 / PATCH 字段 | 取值 |
|---|---|---|
| `VAGENT_VIDEO_MODE` | `videoMode` | `off`（默认）、`mock`、`live` |
| `VAGENT_DASHSCOPE_KEY` | `videoApiKey` | 北京地域的视频 Key；只在本机填写 |
| `VAGENT_DASHSCOPE_WORKSPACE_ID` | `videoWorkspaceId` | 与 Key 匹配的业务空间 ID，小写 DNS 标签 |
| `VAGENT_VIDEO_PROVIDER` | `videoProvider` | `wan` |
| `VAGENT_VIDEO_MODEL` | `videoModel` | `wan2.7-t2v-2026-06-12` |
| `VAGENT_VIDEO_REGION` | `videoRegion` | `cn-beijing` |
| `VAGENT_VIDEO_MAX_JOB_COST` | `videoMaxJobCost` | 十进制人民币字符串，默认 `"3.00"` |

固定规格为 5 秒、720p、16:9，公开提示词最多 4991 字符。后端加上单镜头前缀，显式发送 `prompt_extend=false`、`watermark=true`、`seed=0`。每个 Run 最多一个新 Job；重复调用先复用原任务，再检查新配置。估价以 Decimal 字符串冻结，提交前复核原报价、当前价格和金额上限；实际费用缺少账单时保持未知。

保存或启动配置均不发送测试生成，现有验证按钮只验证 DeepSeek。配置 API 返回 `videoApiKeyConfigured`、`videoPermissionStatus=unverified`、只读探测结果 `videoProbe`（鉴权通过 / 未通过 / 未完成，含主机与时间）、参考估价 `videoEstimate`、`videoMode` 与当前 `activeVideoMode`（保存后一致）及 `restartRequired/restartFields`（K2 起恒为空），不返回 Key。保存视频 Key 或业务空间时执行一次零费用只读探测（不存在任务 ID 的查询，不创建任务、不产生费用；探测失败不阻塞保存）；首次保存视频 Key 且模式为 off 时自动切换到 live，用户显式选择后不再自动翻转。业务空间可留空、后补或以空串移除（回到共享域名），已保存字段对新登记即时生效、无需重启。Web 设置页默认只保留两个 Key 与模式开关，供应商、模型、地域、业务空间与金额上限收进「高级（可选）」。

提交歧义进入 `unknown`，不重提；查询按原 ID 每 15 秒推进，失败后按 15/30/60 秒有限退避，并保留更晚的 `Retry-After`。从提交意图起满 24 小时暂停查询。缺配置或空间不匹配时保存阻塞原因，不增加 HTTP 次数；`jobs retry-query` 先校验原配置和期限，仅恢复查询窗口。查询/下载阶段不因后来调低金额上限而丢弃已受理结果。

媒体下载限定服务端批准的 HTTPS 主机；每次跳转校验来源，解析到公网地址后固定连接地址并验证原 TLS 主机名。使用独立无鉴权客户端，最多 256 MiB，连接/读取/总超时为 10/30/180 秒，一个自动窗口最多 3 次尝试，重启不刷新额度。文件先写入 `.part`，校验自包含 MP4、H.264、1280×720 和 5 秒规格，计算 SHA-256，再原子发布文件并提交结果；不依赖运行时 FFmpeg。

媒体位于数据目录的 `media/<projectId>/<mediaId>.mp4`，CLI 返回相对位置。`GET /api/media/{mediaId}` 返回元数据与当前可用性；`GET/HEAD /api/media/{mediaId}/content` 支持单 Range，`?download=1` 提供附件下载。播放器只在当前媒体可用时出现，SSE 与快照更新保留同一播放器节点。缺失或损坏的文件返回 410，历史结果保持原样；修复复用原 ID 且必须匹配原 SHA-256。冲突文件需备份移走后再重试；过期 URL 不刷新，也不触发重新生成。

旧 off/mock Run 按原工具、规则与检查点签名恢复；live 使用 tools/rules v3。原 live 配置不可用时返回 `RESUME_CONFIG_CHANGED`，保留等待与累计预算。公开 Job、SSE、模型消息和 CLI 查询不包含业务空间、签名 URL 或私有供应商诊断。`await_job` 在 `downloading` 继续等待，本地提交后才交付；下载失败、等待到期或停止后，后续修复只更新 Job，不复活已结束 Run 或改写历史工具结果。

Mock 始终标记 `simulated: true`、`mediaAvailable: false`，不产生 MP4 或播放/下载链接。调用真实 Agent 仍需要 DeepSeek Key 并消耗文本 Token；B0–B4 的工程验证全部使用离线模型，没有调用真实模型或视频 API。

## 从检查点继续

每次执行现在会将 LangGraph 检查点保存到数据目录的 `checkpoints.sqlite`。CLI 在开始执行时输出 Run ID，`inspect` 也会列出运行 ID、状态、`resumable`、原始预算和已用活动时间。

```powershell
.\.venv\Scripts\python.exe -m vagent inspect --session coffee
# 将下面的 RUN_ID 替换为 inspect 或执行输出中的实际 ID。
.\.venv\Scripts\python.exe -m vagent resume RUN_ID
```

- 普通进程中断、用户取消，以及网络、鉴权、限流等可恢复失败，只能显式继续。有效的外部等待可由协调器自动继续；不会自动重发已经尝试过的后续模型请求。显式恢复真实模型请求仍可能产生 API 费用。
- 继续沿用同一 Run ID、request ID、模型步数、工具次数和活动时间预算；即使新 Runner 设置了更高限额，原 Run 也不会获得新预算。预算耗尽和非法协议等不可恢复错误会结束 Run。
- 图以 Run ID 作为 `thread_id`，每个节点的检查点提交完成后才进入下一个节点。工具操作 ID 由 Run ID、模型步骤和工具调用 ID 组成；重复进入工具节点时复用已经提交的结果，未完成的工具才实际执行。
- 已完成的 Run 返回原结果；图已完成但会话快照尚未提交时，从检查点补齐会话，不再次调用模型。
- 只允许恢复当前会话最近的 Run；如果已提交更新的需求，旧 Run 会被拒绝。模型、工具 Schema、系统规则、Skill 版本或上下文预算变化时，也会要求恢复原配置。可以修正 API Key，Key 不属于检查点或配置指纹。
- 旧版 Run 没有图检查点，不能追溯恢复；原有成功对话、项目和产物仍可继续使用。检查点缺失或损坏时会报告错误，保留文件，不退回从头执行。

活动时间在正常停止时按实际耗时累计，离线时间不计入。模型请求发出前预留最多 60 秒（不足时使用剩余预算）；如果请求中途进程被强制终止，重启时按这份预留额保守计时，因为无法确定实际执行了多久。该扣除只发生一次。未取得响应的请求可能已被供应商计费，但无法凭空补出其 Token usage。

外部等待不消耗活动时间，也不新增模型/工具额度；`externalWaitSeconds` 累计已结算等待，`externalWaitStartedAt` 保存当前区间起点，包含等待期间的进程离线时间。单次默认上限 10 分钟，超时交付 `JOB_WAIT_TIMEOUT`，Job 继续独立跟踪。停止后显式恢复未获结果的原等待可开启新代次；已保存结果保持稳定，原 Run 的 8/12/180 限额不重置。

CLI/Web 共用的 HTTP 客户端仅在 `ConnectError` / `ConnectTimeout`（尚未发送模型 HTTP 请求）时最多重连两次。使用原代理和证书校验，所有等待仍受原时间预算限制；写入/读取中断、429/5xx、流式失败、重定向或额外认证流程均不自动重试。配置页的单次验证显式关闭连接重试。评测中的 `connectionRetries` 与 `modelCalls` 分开统计。

断电或强退可能留下 `instance.lock`，仍需确认原进程已经退出后手动移除该锁，再执行 `resume`。恢复验证记录见 [持久恢复验收](./docs/RECOVERY_ACCEPTANCE.md)。

## 查看模型与缓存用量

执行 `run`、`chat`、`resume` 后会显示用量摘要。`inspect` 中的每个 Run 也增加 `usage` 汇总。查看单次调用明细可使用以下命令，不需要 Key，也不会调用模型：

```powershell
.\.venv\Scripts\python.exe -m vagent usage --session coffee
.\.venv\Scripts\python.exe -m vagent usage --run RUN_ID
```

`usage` 只显示运行标识、模型、状态及用量，不输出对话和产物正文。原始调用明细保存在 `state.json` 的 `runs[runId].modelCalls` 中，汇总从这些明细计算。

| 字段 | 含义 |
|---|---|
| `modelCallCount` / `recordedCallCount` | 模型适配器调用尝试数 / 已有明细的调用数，包含失败与中断；存在旧版未跟踪步骤时前者为 null |
| `observedInputTokens` / `observedOutputTokens` | 已取得响应中可确认的输入 / 输出 Token 累计值，保留原有历史总量 |
| `cacheHitTokens` / `cacheMissTokens` | 已知缓存命中 / 未命中输入 Token 总量；未取得该项数据时为 null |
| `cacheHitRate` | 有完整缓存数据的调用中，命中 Token 总量 ÷（命中 + 未命中 Token 总量）；不是各次命中率的简单平均 |
| `callsWithTokenUsage` / `callsWithCacheUsage` | 有完整 Token / 缓存数据的调用数，用于判断统计覆盖率 |
| `tokenUsageComplete` / `cacheUsageComplete` | 所有调用是否都有对应的完整统计；失败、中断或旧版缺失明细会使相应值为 false |
| `untrackedModelSteps` | 旧版没有单次调用明细的模型步数，不能当作已确认的实际请求数 |

每条调用记录包含所属模型步骤、开始/结束时间、耗时、`started/responded/failed/cancelled/interrupted` 状态、上述 Token 数及错误码。`responded` 仅表示适配器返回了响应；即使工具协议随后校验失败，该响应已报告的 Token 仍计入统计。

DeepSeek 原始 `prompt_cache_hit_tokens`、`prompt_cache_miss_tokens` 优先使用；只有标准 `cache_read` 等单边数据且输入总数已知时，由总数减去已知项推导另一项，标记 `cacheUsageSource=derived`。缺失为 `missing`，负数或相互矛盾的缓存数据为 `invalid`，不伪装成 0 命中。总缓存输入为 0 时命中率为 null。

未知用量不会被当成免费调用；当前总量只反映已知部分。强退前尚未返回的请求会标为 interrupted，用量和准确结束时间保留未知。重复 request ID、重复恢复已完成 Run 或工具节点重放不会重复计入已有调用；重新请求模型会新增调用记录。旧 Run 不回填虚构缓存数据，恢复后从新的模型步骤开始记录。

这里统计的是服务商报告的上下文缓存用量，不是本地 Redis 命中数，也不代表减少了上下文窗口占用。文本模型尚未加入价格表或人民币费用估算；C1 的视频估价单独保存在 Job 中。验证范围见 [用量观测验收](./docs/USAGE_ACCEPTANCE.md)。

## Redis 回答缓存（任务四）

默认关闭。需要 Redis 服务，并在 `.env` 中设置连接地址；连接凭据不会显示在 `config show` 或保存到 Run。使用前先更新依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
```

```dotenv
VAGENT_REDIS_URL=redis://localhost:6379/0
VAGENT_CACHE_TTL=3600
```

```powershell
.\.venv\Scripts\python.exe -m vagent run '阅读现有方案，解释镜头节奏。' --session coffee --read-only
.\.venv\Scripts\python.exe -m vagent chat --session coffee --read-only
.\.venv\Scripts\python.exe -m vagent usage --session coffee
```

`--read-only` 仅提供 `project_read`、`artifact_read`、`skill_read`；后端同样拒绝写工具，即使模型尝试调用也不能修改项目、计划或产物。会话与执行记录仍正常持久化。`resume` 自动沿用原 Run 的模式。没有 Redis 配置时只读模式也能使用；普通写作 Run 和离线 demo 不使用回答缓存。

缓存以**单个模型步的最终纯文本响应**为单位；工具调用响应、空回复、非法协议及明确截断的响应不缓存。命中跳过该步模型请求，仍正常完成图检查点与会话提交，不复用旧 Token usage。之前已经发生的模型调用与读取工具仍保留。缓存不是整个 Agent 工作流或工具操作的重放机制。

键以 SHA-256 摘要覆盖数据目录、项目身份及完整事实、全部项目产物版本与内容、实际发送的消息及工具定义、系统与 Skill 版本、上下文格式和模型配置。不同数据目录或项目隔离；任一依赖变化都会生成不同键。Redis 仅保存版本号和回答文本，默认 1 小时过期，TTL 可设为 1～604800 秒；单条回答限制 256 KiB。

这是严格精确匹配，没有语义相似度匹配。**重复输入同一句话不保证命中**：正常连续对话会增加历史，工具调用 ID 和消息元数据变化也会影响键。实际收益需要真实重复请求数据验证；未声称已测得生产 Token 节省。

每次 Redis 读写最多等待 0.5 秒；连接失败、超时或损坏条目会回退到模型请求，写缓存失败不丢弃已经取得的回答。原 Run 的取消与总时间预算继续生效。Redis 是可丢弃缓存，不替代 SQLite 检查点和 JSON 项目记忆，也不提供并发请求合并。

`usage.answerCache` 单独累计 `hit`、`miss`、`invalid`、`error`、`stored`、`skipped`，未发生的项省略；`error` 包含读失败和写失败，并非模型失败数。命中不会新增 `modelCalls`、`modelSteps` 或输入/输出 Token，不能将旧响应 Token 当作本次服务商缓存用量。执行时也显示 Redis 事件。验收证据见 [Redis 缓存验收](./docs/REDIS_CACHE_ACCEPTANCE.md)。

## 实现与代码入口

```text
src/vagent/
  cli.py       argparse 命令行与配置展示
  application.py CLI/Web 共用的生命周期与执行服务
  web.py       回环 HTTP API、CSRF、SSE 与打包页面
  headless.py  E3：headless 两阶段计划/授权/集合等待与本地拼接交付
  mcp_bridge.py MCP stdio 发现、显式只读白名单与异步工具桥
  mcp_server.py 内置镜头时长与帧数计算服务
  config.py    配置来源、原子保存与 Key 校验
  models.py    DeepSeek 单步/流式适配、完整参数校验与离线模拟模型
  http.py      请求发送前的有限连接重试，共享客户端生命周期
  runner.py    自定义 LangGraph 图、预算、取消与事件
  checkpoints.py SQLite 异步检查点与连接生命周期
  journal.py   跨恢复保留的执行预算、使用量与会话提交
  usage.py     Token 与缓存用量归一化、覆盖率与 Run 汇总
  cache.py     Redis 精确回答缓存、TTL、超时与故障回退
  context.py   上下文组装与完整轮次裁剪
  quality.py   记忆冲突、用户字数要求与正文计数校验
  tools.py     工具注册、Pydantic 校验、项目与产物操作
  storage.py   单写锁、事务、原子替换、操作日志、schema 迁移与逐字节备份
  skills.py    技能发现、元信息与按需正文读取
  contracts.py B0：不依赖 Store/SDK 的严格 JSON 类型
  waiting.py   通用执行上下文、延迟结果和版本化等待指针
  wait_runtime.py B3：持久等待、结果领取/交付、停止、计时与恢复协调
  video/contracts.py v1 Mock / v2 live 请求、Job、费用、媒体与供应商契约
  video/jobs.py B1：原子登记、请求冻结、去重、版本检查和启动恢复
  video/tools.py off/mock/live 独立视图、分镜/成片/分集工具、等待解析与历史兼容
  video/worker.py 串行提交/查询、超时、持久重试、配置阻塞与停止
  video/media.py C2：安全文件访问、完整性、原子提交、可用性与幂等修复
  video/media_worker.py C2：独立串行下载、持久额度/期限与强退恢复
  video/media_http.py C2：HTTPS 来源策略、公网 DNS 固定连接与流量上限
  video/media_response.py C2：已核验文件的 GET/HEAD、Range 与下载响应
  video/mp4.py C2：有界 MP4 结构/样本校验与实测元数据
  video/copy_signature.py E1：mp4-copy-signature-v1 流复制签名（codec 配置、时间线、edit list 与 priming）
  video/assembler.py E1：包内 FFmpeg 定位/指纹、按样本推导的接缝时间线、concat 流复制与输出校验；M4 在签名不兼容时回退 ffmpeg-concat-normalize-v4（1280×720/30fps + AAC 48k）
  video/films.py E1：Film/FilmVersion/FilmMedia 契约、只增不改校验、输入选择与启动清理
  video/film_service.py E1：锁内冻结、锁外拼接、重获锁复核后一次提交与指纹幂等
  video/subtitles.py M6 E1：SubtitleTrack/SubtitleTrackVersion 契约、subtitle-align-zh-v1、cue 校验与 SRT/ASS 序列化
  video/subtitle_service.py M6 E1：锁内冻结、锁外字幕软封装/烧录、复核后一次提交与渲染指纹幂等
  video/subtitle_font.py M6 E1：包内 OFL 字体定位、SHA-256 校验与 FONT_MISSING
  video/audio.py M6 E2：schema 7 的 audio 媒体分支、AudioAsset 域、TTS 额度常量与逐集/只增不改校验
  video/tts.py M6 E2：tts-providers-v1 能力表、local-tts-v1 确定性 WAV、CosyVoice 受控适配器与错误映射
  video/audio_service.py M6 E2：audio_generate 的锁外 TTS、额度/音色绑定/幂等复用与 AudioMedia+AudioAsset 一次提交
  video/mixer.py M6 E2：mix-zh-v1 混音、侧链 ducking、两遍 loudnorm 与 EBU R128 成品复测
  video/renders.py M6 E3：有效时间线、fade/crossfade 校验、cue 夹取、标题卡与 RenderTask 只增不改进度
  video/render_service.py M6 E3：film_render/render_read、逐场归一化、跨场组装、音频放置、1080P、校验与幂等提交
  video/timelines.py M7 E3：timeline-zh-v1 契约、只增不改时间线、裁剪/转场/音轨/标题卡校验与指纹
  video/audio_import.py M7 E3：客户端 WAV/MP4 受控导入、许可与来源校验、SHA-256 去重与音频媒体落盘
  video/seasons.py M7 E1：Season/SeasonVersion 契约与季成员只增不改校验
  video/batches.py M7 E2：Batch/BatchEpisode 契约、逐集登记、恢复与 Store 崩溃收尾
  video/routing.py M8 E1：provider-route-v1 只读路由预览（每供应商一行、原因优先级、跨币种并列、无时钟指纹）
  video/billing.py M8 E2：billing-v1 手工账单侧车账本（幂等/冲突/替换、逐币种汇总、原子替换与损坏保护）
  video/providers/gate.py M8 E2：逐 provider 提交间隔与 429 冷却、PROVIDER_BACKOFF 诊断、统一错误表常量与内存指标
  web/bill-state.js M8 E2：账单币种行、差额符号、记录行与写入体的纯状态逻辑
  web/subtitle-state.js M6 E1：字幕面板的纯状态逻辑（版本、cue、导出请求与受控下载）
  web/render-state.js M6 E3：整片渲染请求、逐场进度摘要与旁挂 SRT 的纯状态逻辑
  web/timeline-state.js M7 E3：时间线草稿、裁剪/转场/预览/保存体与本地校验的纯状态逻辑
  web/season-state.js M7 E1/E2：季成员、批次确认/恢复与状态文本的纯状态逻辑
  web/film-state.js E1：成片面板的纯状态逻辑（版本列表、播放/下载、显式拼接请求）
  video/episodes.py M5 E0：Episode/Scene/Character/Style 契约、只增不改版本与集级预算不变量
  video/adaptation.py M5 E1：text-split-zh-v1 确定性章节/场景/对白切分与镜头草案
  video/consistency.py M5 E2：角色/风格提示模板与跨镜头一致性证据
  web/episode-state.js M5 E4 / M6 E4：分集面板的纯状态逻辑（逐场汇总、集级预算、成片标签、整集成片投影、SRT 解析与 cue 查找）
  video/storyboards.py D1：分镜/镜头/素材域契约、只增不改版本与提交级不变量
  video/views.py D1/D3：工具与本地客户端共用的 Job/分镜有界投影（含镜头媒体派生）
  video/assets.py D3：素材库导入（魔数/尺寸/哈希校验、按项目去重、文件恢复）与只读读取
  video/clients.py D3：显式本地动作（逐镜头重做、批量登记）的客户端 Run 与幂等重放
  video/usage.py D3：Jobs、估算/账单状态、分镜与素材、模型 Token 覆盖率的读取侧聚合
  web/storyboard-state.js D3：分镜面板的纯状态逻辑（分组、缩略图、连播、估算汇总）
  video/providers/mock.py B1：独立持久上游账本与服务端模拟轨迹
  video/providers/wan.py C1：固定北京协议、独立 HTTP 客户端与提交歧义处理
skills/        内置 SKILL.md，随 wheel 分发
tests/         pytest 行为与协议测试
scripts/probe_m1b_wait.py B0：独立临时目录中的持久等待实验
scripts/evaluate_m6.py M6 E4：离线整集验收套件（字幕/配音/转场/标题卡/失败隔离/拒绝矩阵）
```

LangGraph 提供图执行底座，项目自己定义状态、路由、上下文策略、工具边界、版本控制和运行记录。`ChatDeepSeek.bind_tools(...).astream(...)` 只执行单个模型步；保留非流式 `generate` 接口以兼容测试和其他调用方。流式工具参数必须通过严格 JSON 解码和完成标记检查后才执行，模型不会直接执行工具。

- **工具白名单**：默认 `project_read`、`project_update`、`plan_update`、`artifact_save`、`artifact_read`、`skill_read`；mock/live 模式增加各自版本的四个视频工具。不开放 shell 或任意文件路径。
- **Pydantic Schema**：工具声明与后端输入校验共用定义，拒绝未知字段和错误参数类型；工具错误会返回模型。
- **项目记忆**：保存目标、受众、风格、约束和计划；产物以稳定 ID 保存，每次修改增加版本并保留原文。
- **写入保护**：单写进程锁、串行事务、同目录原子替换；业务修改和操作结果一起提交。相同操作 ID 复用结果，参数冲突则拒绝。
- **运行限制**：默认最多 8 个模型步、12 次工具调用、180 秒，显式恢复沿用累计预算；超时取消异步模型请求，停止后不启动新工具。已完成的本地写入保留。
- **预算提示**：上下文 v2 在稳定前缀和项目事实之后提供实际剩余模型/工具次数，模型剩余次数包含本次调用；重启后继续扣除已用额度，提示不写入对话历史。v1 布局保持原状。
- **日志边界**：不直接输出供应商异常或参数校验中的原始输入值。配置展示只返回 Key 是否存在。

文本工具去重针对同一请求/操作 ID。B1 JobService 另限制每个 Run 最多一个视频 Job：不同调用 ID 的同一规范化视频请求返回原 jobId，不同请求返回冲突；这不是语义去重。JSON 存储面向单用户小规模使用，同步本地文件写入不是可抢占的异步任务。

## 上下文与 Skills

新 Run 默认使用上下文 v2。每个模型步按以下顺序组装系统消息，再追加近期对话：

1. 固定系统规则。
2. 按名称排序的 Skill 名称、描述和内容版本，以及固定的读取指引。
3. 最新项目事实。

工具定义作为独立的 `tools` 参数传给模型，按函数名排序，并固定对象键顺序。项目更新不会改动前面的规则与 Skill 元信息，有利于复用服务商的相同输入前缀；最终缓存行为由服务商决定。

项目事实和 Skill 元信息使用紧凑、键顺序稳定的 JSON。发送给模型的应用工具结果只去掉 JSON 字符串外的冗余空白，保留字符串正文、转义、数值原文及工具调用 ID；存储中的原始对话和产物不改写。

`VAGENT_CONTEXT_BYTES` 默认 98304（M8 E3 起；18/18 工具面在旧 65536 下第一轮即 `CONTEXT_LIMIT`），按消息和工具定义的 UTF-8 序列化字节数衡量，**不是精确 Token 数或供应商请求体大小**。供应商返回的 Token usage 另行记入 Run。

超限时裁剪最旧的完整用户轮次，保留工具调用与结果配对；原始历史不删除。当前需求、当前轮工具结果和项目事实必须完整保留，仍超限则在调用模型前报 `CONTEXT_LIMIT`。长产物通过 `artifact_read` 的 offset/limit 分段读取。

Run 保存 `contextVersion`，`inspect` 和 `usage` 可查看。已有检查点 Run 缺少该字段时按 v1 恢复，沿用原来的布局、空白和配置指纹；后续新 Run 使用 v2。未知格式版本会在恢复前报错。无检查点的早期 Run 仍不能恢复。对比数据和验证范围见 [上下文优化验收](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)。

内置 [video-brief](./skills/video-brief/SKILL.md) 和 [shot-description](./skills/shot-description/SKILL.md)。启动时校验并保留内容快照，初始上下文仅包含名称、描述和内容版本；`skill_read` 后才加入正文。

自定义技能目录结构为 `skills/your-skill/SKILL.md`：

```markdown
---
name: your-skill
description: 说明哪些任务需要这个技能。
---

使用现有工具完成任务的操作指引。
```

名称必须与目录一致，每个文件最多 16 KiB。默认按「内置根 → `VAGENT_HOME/skills/`」发现，每个根内按目录名排序；内置名称优先，用户重名会被忽略并在 `vagent skills list` 的 `diagnostics` 中以 `SKILL_NAME_CONFLICT` 报告。用户目录不存在视为空；坏用户文件（非法 frontmatter、超长、符号链接/目录联接）只记录 `INVALID_SKILL`/`INVALID_SKILL_FILE` 诊断，不使应用启动失败。内置加用户的有效条目合计最多 32 个：先保留内置，再按名称取前，其余逐条报告 `SKILL_LIMIT`。`VAGENT_SKILLS_DIR` 仍指定可信本地目录并**整体替换**内置根（保持原有严格报错语义），不再合并用户根。修改后重启加载；不支持自动执行脚本、联网安装或热更新，Skill 不能增加工具权限或跳过代码校验。新增或修改 skill 会改变新 Run 的签名；恢复旧 Run 时若当前 catalog 与记录不一致，会返回 `RESUME_CONFIG_CHANGED`，不会把新用户文件悄悄注入旧 Run。

`vagent skills list` 输出 `{"skills": [...], "diagnostics": [...]}`；诊断只含代码、目录名和固定说明，不含正文或私有绝对路径。Web 的「编排观测」面板同样显示未加载的用户 skill 诊断。

## 数据兼容与限制

默认数据目录为 `~/.vagent/`，可用 `VAGENT_HOME` 覆盖。`state.json` 保存会话、项目、产物、Run、操作结果、jobs/waits、media 索引与 M2 的 `storyboards`/`assets` 集合、M5 的 `episodes`/`scenes`/`characters`/`styles` 集合；E3 的 headless 计划、授权指纹、登记结果与交付摘要保存在 Run 记录的可选 `headless` 字段（未使用时不写该字段，不改变存储 schema，也不迁移旧记录）；`checkpoints.sqlite` 保存图执行位置和消息。B1 的 `mock-video.json` 独立保存模拟上游受理记录、请求、轨迹和调用计数。应在程序退出后备份完整数据目录。配置 Key 不写入状态；真实 Job 的私有上游 URL 仅保存在本地状态中，不经公开接口返回。`inspect` 会显示创作正文。

当前存储为 schema v6。打开 v1–v5 数据时，在实例锁下使用原版本校验器验证状态与消息，先将原始字节保存为同目录的 `state-v1-<UUID>.json` 等备份，再原子迁移。迁移只增加缺少的顶层集合（v4 新增 `storyboards`/`assets`，v5 新增 `films`，v6 新增 `episodes`/`scenes`/`characters`/`styles`）并升级版本，历史 Job、Run、Operation、来源版本和指纹不重编码、不补写默认字段，也不修改 SQLite；运行恢复单独提交。`media` 索引在 v5 成为联合类型：旧的生成媒体记录保持原字段按原校验器解析，`contractVersion=2/kind=film` 才走成片校验器，旧媒体不补 `kind`、不改写。旧 Mock Job 保持 contract v1，新 live Job 使用 v2。缺少视频字段的旧 Run 按 off 解释，execution v1/v2 与上下文 v1/v2 按原签名恢复；execution/context/checkpoint 仍为 3/2/1，工具面自 1/1 起逐代追加（M5 为 9/9），恢复旧 Run 时按 `toolFeatures` 身份重建冻结注册表并核对签名，签名逐字节不变。迁移写入失败、损坏数据和未知版本保留原状态文件。v6/v5/v4/v3 不能交给更旧的应用直接读取；回退需在服务退出后恢复完整数据目录，单份状态备份不能替代它。

- 成功会话可跨重启继续；执行中的普通 Run 重启后标记为 `interrupted`，已提交产物保留，不自动重放。B3 保留有效的 `waiting_external` 和原 Run 名额，启动时核对等待、检查点、配置与原预算后继续；后续模型尝试已经开始但未完成时要求显式恢复。
- B1 已登记但尚未提交的 Job 可由 Worker 继续推进；丢失提交结果的任务进入 `unknown`，不自动重提。已确认上游 ID 只用于继续查询，失败窗口和累计次数跨重启保留。
- 应用快照和 SQLite 检查点分别提交，依靠同步图检查点、操作幂等和恢复时补交会话处理提交间隙；并非跨 JSON/SQLite 的单一数据库事务。
- 异常断电可能留下 `instance.lock`；确认没有进程使用该数据目录后才手动移除，程序不会自动抢锁。
- CLI/Web 已支持流式草稿，完成后以持久回复替换；草稿不写入检查点，中断后不作为下一次模型输入。自动摘要和跨项目偏好记忆尚未实现。

B1 的 JobService、MockVideoAdapter 和 JobWorker 可通过 Python API 独立使用和测试，不需要模型或 Key；应用统一管理 Worker 和客户端的启动与退出。Mock 只保存带 `simulated: true`、`mediaAvailable: false` 的描述，不生成媒体文件。文本 Agent 只需要 DeepSeek Key；live 生成另外需要视频 Key 和对应业务空间。缺少视频配置时仍可启动应用并使用文本功能。

## 验证与打包

任务四新增验证：真实 Redis 重连命中、TTL 失效与服务中断回退；独立 wheel 环境中的仓库外 CLI 与依赖检查通过。详见 [Redis 缓存验收](./docs/REDIS_CACHE_ACCEPTANCE.md)。

2026-10-24 **v0.5.0 发布**（M6 成片后期 + M7 多集工作台）：版本号 `0.5.0`、构建 wheel/sdist 并通过安装态 wheel smoke；发布前在 `760fed9` 上复跑全量回归 **1409 passed / 3 skipped**、Ruff（217 文件）、前端 8 模块 **78 项**；四组 CI 在 `760fed9` 上 success。详见 [v0.5.0 发布说明](./docs/RELEASE_0.5.0.md) 与下一阶段 [M8 计划](./docs/M8_PLAN.md)。

2026-10-25 **M8 E0 契约冻结**（供应商路由、账单与 v0.6.0 发布）：[M8 契约](./docs/M8_CONTRACTS.md) 定下 `provider-route-v1` 只读路由预览（每个供应商一行、CNY→USD 分组排序、`reason` 复用既有错误码）、`billing-v1` 手工账单侧车账本（`home/billing.json`，不进 `state.json`、不抓取供应商账单）、逐 provider 提交间隔 2.0 秒与 429 冷却 60 秒，以及 4 个新错误码（`ROUTE_UNAVAILABLE`、`PROVIDER_BACKOFF`、`BILL_RECORD_INVALID`、`BILL_CONFLICT`）；**工具面保持 18/18、schema 保持 9、168 个契约模型哈希与 `capabilitiesSha256` 冻结**，E0 未改产品代码（submit=0、模型调用=0、TTS=0、凭据读取=0）。机器可读基线见 `docs/M8_E0_BASELINE.json`，证据与复现命令见 [E0 验收](./docs/M8_E0_ACCEPTANCE.md)。

2026-10-25 **M8 E1 只读路由预览**（`provider-route-v1`）：新增 `src/vagent/video/routing.py`，按冻结能力表为每个供应商给出**恰好一行**候选（`provider/model/capabilitiesVersion/priceVersion/currency/unitPrice/quantity/amount/resourceUnits/available/reason/sourceUrl/checkedAt/quoteFingerprint`），按「环境 → 凭据 → 能力 → 价格 → 记账」8 级优先级取第一个原因，按「可用 → CNY→USD → 金额 → wan→kling→fal」排序并给出 `recommended`；有效单 Job 上限按当前配置计算（wan 的 `videoMaxJobCost` 调高后 10 秒才可用，kling/fal 只能收紧）。按分镜预览读取当前版本、逐镜头判定与求和（任一镜头失败即该供应商不可用，`reason` 取第一个失败镜头；跨 t2v/i2v 条目时金额为逐镜头之和并在 `notes` 注明行内取第一个镜头的 `model/priceVersion`）；`notes` 固定写明**不换汇**、集级/批量只接受 CNY、预览不预测 Run 余量与限流冷却。三个面共用同一纯函数：`vagent route preview [--storyboard …|--mode …] [--scope …] [--provider …] [--require-available]`、`GET /api/route/preview`、Web 分镜面板「报价对比」与分集面板「集级报价对比」对话框（三行候选、原因、`sourceUrl`/`checkedAt`、「使用该供应商」只写既有 `PATCH /api/config` 的 `videoProvider`，不改写已登记 Job）。真实预览逐项复现 E0 `routeProbe.rowsSha256 = 3817d191…`；新增 63 项后端与 6 项前端用例，全量回归 **1472 passed / 3 skipped**、Ruff 222 文件、前端 9 模块 84 项；**工具面 18/18、schema 9、168 个模型哈希与 `capabilitiesSha256` 不变**，全程 submit=0、模型调用=0、TTS=0、凭据读取=0。详见 [E1 验收](./docs/M8_E1_ACCEPTANCE.md)。

2026-10-25 **M8 E2 供应商运维与账单**：新增 `src/vagent/video/providers/gate.py` 与 `src/vagent/video/billing.py`，并把两者接到既有 Worker、用量、CLI、HTTP 与 Web 用量面板。**提交节流**：逐 provider 提交最小间隔默认 2.0 秒（新配置 `videoSubmitIntervalSeconds`/`VAGENT_VIDEO_SUBMIT_INTERVAL_SECONDS`，2.0–60.0）、429 冷却默认 60 秒（`videoRateLimitCooldownSeconds`/`VAGENT_VIDEO_RATE_LIMIT_COOLDOWN_SECONDS`，60–900），`Retry-After` 只延长不缩短且超过 900 秒按上限截断；被挡住时**保持 `pending_submit`、不写 `submitAttempts`、不发请求**，只把 `PROVIDER_BACKOFF` 写进既有 `RuntimeBlock`（`stage=submit`，下一次真正提交时清空），因此 M1-C 的「每 Job 至多一次提交、未知永不重投」逐字保留——包括 `not_accepted`、`SUBMISSION_UNKNOWN` 与连接失败都不自动重投。§5.1/§5.2 的映射落成常量 `SUBMIT_ERROR_TABLE`/`QUERY_ERROR_TABLE`，`GET /api/usage?runtime=1` 暴露每 Store 的提交/查询延迟 p50/p95、退避与限流计数（`durable:false`，重启归零）。**账单**：`billing-v1` 侧车账本 `VAGENT_HOME/billing.json`（与 `state.json` 同级、0600、一条记录一次临时文件 + `os.replace`），不进 `DatabaseV9`，因此 schema 仍 9、168 个契约模型哈希不变、`JobV6.cost.actual` 仍是冻结的 `unknown`；幂等只看业务字段（金额/币种/日期/来源/备注），不同即 `BILL_CONFLICT` 需 `--replace --expected-revision`，非法/在途/币种不符/超限为 `BILL_RECORD_INVALID`，账本损坏为 `INVALID_STORE` 且保留原文件。`usage.billing` 逐币种聚合估算/已录入/差额，**未录入是 `null` 而不是 0、跨币种无合计、不换汇**。三个面：`vagent usage bill record|show`、`POST/GET /api/usage/bill`、Web 用量面板「供应商账单」区（币种行、已录入记录与录入表单，写入只走 `POST /api/usage/bill`，带 CSRF 与 `confirm`）。新增 37 项后端（+1 项 POSIX-only skip）与 7 项前端用例，全量回归 **1509 passed / 4 skipped**（E1 为 1472/3）、Ruff 229 文件、前端 10 模块 91 项；**工具面 18/18、schema 9、168 个模型哈希与 `capabilitiesSha256` 不变**，全程 submit=0、模型调用=0、TTS=0、凭据读取=0。详见 [E2 验收](./docs/M8_E2_ACCEPTANCE.md)。
2026-10-25 **M8 E3 真实样本**：按单独授权只执行现有凭据可跑的两项——CosyVoice v2 对 `rainy-cafe` 整集 19 条台词（170 字符）配音 19 次请求，产物为 19 个 48 kHz 单声道 16 位 WAV（合计 3,148,676 字节），包内 FFmpeg 解码通过、`Range: bytes=0-99` 返回 206，产品口径估算 **0.0340 CNY**、实际账单 `unknown`；真实 DeepSeek（`deepseek-flash`）跑 1 个 Run，**4 次模型调用、3 次工具调用**（`episode_plan`→`episode_save`→`episode_read`）、输入 96,617 / 输出 2,176 Token、有效 12.4 秒，保存 8 场 / 19 镜头 / 90 秒分集，`jobs=0`。Kling 5 秒与 fal 5 秒因缺 `VAGENT_KLING_API_KEY` / `FAL_KEY` **未执行（0 请求）**，能力表保持「文档支持，未做真实样本」。两条真实样本各暴露并修复一个产品缺陷：①CosyVoice 返回 `RIFF`/`data` 占位长度（`0x7FFFFFxx`）的流式 WAV，`inspect_wav` 以「块长度越界」拒绝——新增 `normalize_streaming_wav()` 只改写这两个占位长度，截断的真实长度仍被拒绝；②18/18 工具面（工具定义 45,956 B + 规则 14,053 B）在默认 64 KiB 预算下第一轮即 `CONTEXT_LIMIT`——产品默认 `VAGENT_CONTEXT_BYTES` 升到 **98304**（96 KiB，与 M6 E1 起的验收夹具一致；升级后若要恢复用 64 KiB 记录的旧 Run，需临时设回 `VAGENT_CONTEXT_BYTES=65536`，否则按既有 `RESUME_CONFIG_CHANGED` 拒绝）。全程视频 submit=0；`docs/M8_E0_BASELINE.json` 重新生成后 SHA-256 仍为 `f3dc91c1…b3706`，168 个模型哈希、`contractsSha256`、`capabilitiesSha256`、工具/规则哈希与 `routeProbe.rowsSha256` 全部不变。详见 [E3 验收](./docs/M8_E3_ACCEPTANCE.md)。

2026-10-25 **M8 E4 验收与 v0.6.0**：离线套件 `scripts/evaluate_m8.py` 4 用例（18 行三供应商矩阵与 4 行冻结 `routeProbe` 逐字节复现、10 秒/图生/集级批量/缺 Key/过期价/未发布供应商/非法参数/`--require-available` 拒绝矩阵、受控 401/超时/429 + 节流与账单录入/重放/冲突/替换/损坏账本、预览与登记与账单三层重放不新增）与 `tests/test_m8_evaluation.py` 4 项；`output/m8-e4-suite.json` 记录 38 次预览、6 次受控提交、7 个 Job、3 次账单写入、3 次重放、1 条账本记录（`suiteId=88c283bb329a8e20`、真实供应商/文本/TTS 调用均为 0）。受控 Chrome 155 在报价对比对话框看到 3 个可用候选（9.00 CNY / 1.26 USD / 1.68 USD·12 units、推荐 wan、不换汇、未做真实样本标签）与账单面板（USD 已录入 0.60、CNY 未录入不计 0、第 2 版），并以同一账单重放确认「已复用」且记录数不变；6 步、3 张截图、零页面/控制台错误。安装态 wheel smoke 新增 M8 4 用例与 `route preview`/`usage bill show`/HTTP 三面。`pyproject.toml` 与 `src/vagent/__init__.py` 升到 **0.6.0**（修正自 v0.4.0 起未更新的 `__version__`），发布说明见 [v0.6.0](./docs/RELEASE_0.6.0.md)、验收与复现见 [M8 验收](./docs/M8_ACCEPTANCE.md)。全程真实视频 submit=0、文本模型调用=0、TTS=0，未新增付费样本；168 个模型哈希与 `routeProbe.rowsSha256` 不变。四组 CI（ubuntu/windows × Python 3.11/3.12）在 `588e1ea` 上全部 success（[运行 40171887390](https://github.com/shifang37/vAgent/actions/runs/40171887390)）。

```powershell
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
node --check web/job-state.js
node --check web/storyboard-state.js
node --check web/film-state.js
node --check web/subtitle-state.js
node --test tests/test_job_state.mjs
node --test tests/test_storyboard_state.mjs
node --test tests/test_film_state.mjs
node --test tests/test_subtitle_state.mjs
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/python
```

2026-10-23 M7 E2 本地完整回归：**1385 passed / 3 skipped**（1498.66 秒，0:24:59；3 项 skip 为既有的 Windows 符号链接权限用例），前端七个状态文件 **65 项** 通过（`season-state` 14 项），Ruff check/format（208 个文件）与 `node --check` 通过。E2 新增/更新：`Batch`/`BatchEpisode`/`BatchEstimate`、`season_generate`、逐集独立事务与失败隔离、显式恢复、Store 崩溃恢复、`season_read` 批次交付计数、CLI `season generate`/`show --batch`、`/api/seasons/{id}/estimate`、`/api/sessions/{id}/seasons/generate`、Web 批量确认/失败隔离/恢复面板与 `season-state.js` 7 项新测试；后端 43 项定向用例；受控 Chrome 在视频模式 mock 下完成「打开季面板→预览 2 集 24 个新任务→确认生成→批次全部成功/逐集 12-12 登记」验证（控制台 0 错误、24 个 mock Job、1 条 succeeded 批次、submit=0）。详见 [E2 验收](./docs/M7_E2_ACCEPTANCE.md)。

2026-10-23 M7 E3 本地完整回归：**1405 passed / 3 skipped**（1253.07 秒，0:20:53；3 项 skip 为既有的 Windows 符号链接权限用例），前端八个状态文件 **78 项** 通过（`timeline-state` 13 项），Ruff check/format（215 个文件）与 `node --check` 通过。E3 新增/更新：`Timeline`/`TimelineVersion`/`TimelineClip`/`TimelineTransition`/`TimelineSubtitleRef`/`TimelineAudioTrack`/`TimelineTitleCard`、`timeline_save`/`timeline_read`、`film_render` 的 `scope=timeline` 与 `ffmpeg-render-timeline-v1`/`RenderSources.timeline`/`RenderTask.timeline*`、`timeline-subtitle-remap-v1`、客户端 BGM/SFX 导入、CLI `timeline show|sources|save`/`audio list|import`/`film render --timeline`、`/api/episodes/{id}/timeline`、`/api/timelines/{id}`、`/api/sessions/{id}/timelines/save`、`/api/audio*`、Web 时间线工作台与 `timeline-state.js` 13 项新测试；后端定向用例 19 项；规则增长后的夹具预算调整（产品默认 `VAGENT_CONTEXT_BYTES` 不变）；回归附带修复未改动 running 批次的误判与近静音 `loudnorm` 哨兵值。详见 [E3 验收](./docs/M7_E3_ACCEPTANCE.md)。
2026-10-23 M7 E4 本地完整回归：**1409 passed / 3 skipped**（1289.29 秒，0:21:29；3 项 skip 为既有的 Windows 符号链接权限用例），前端八个状态文件 **78 项** 通过，Ruff check/format（217 个文件）与 `node --check` 通过。E4 新增/更新：`scripts/evaluate_m7.py` 3 用例与 `tests/test_m7_evaluation.py` 4 项（96 个 mock Job、4 条批次、1 次恢复、14 次受控提交、14 个交付、2 个时间线版本、2 个成片版本、22 条字幕 cue、真实 submit/模型/TTS=0）；受控 Chrome 时间线编辑/预览/导出 v3（Range 206、零页面错误）；wheel smoke 新增安装态 M7 3 用例与季/时间线工作台，并把安装态 M1-b 评估预算提到 96 KiB（修复 E3 推送后四组 CI 的 `CONTEXT_LIMIT`）；CI 补上 `render-state`/`timeline-state` 的 Node 检查与测试。四组 CI 在 `760fed9` 上全部 success（[运行 40063039486](https://github.com/shifang37/vAgent/actions/runs/40063039486)，attempt 2）。详见 [M7 验收](./docs/M7_ACCEPTANCE.md)。
2026-10-23 M7 E1 本地完整回归：**1371 passed / 3 skipped**（1198.81 秒，0:19:58；3 项 skip 为既有的 Windows 符号链接权限用例），前端七个状态文件 **58 项** 通过，Ruff check/format（204 个文件）与 `node --check` 通过。E1 新增/更新：schema 9 与 `seasons`/`batches`/`timelines`、`Season`/`SeasonVersion`、`season_save`/`season_read`、`vagent season show|save`、`/api/seasons*`、Web 季面板与 `season-state.js`；后端 29 项定向用例 + 前端 7 项；受控 Chrome 155 在视频模式 off 下完成「打开季面板→改名→加入第三集→保存为 v2」验证（控制台 0 错误、Jobs=0）。详见 [E1 验收](./docs/M7_E1_ACCEPTANCE.md)。
2026-10-23 M6 E4 本地完整回归：**1342 passed / 3 skipped**（1080.30 秒，0:18:00；3 项 skip 为既有的 Windows 符号链接权限用例），前端状态测试 **51 项** 通过，Ruff check/format（197 个文件）与 `node --check` 通过。E4 新增/更新：`scripts/evaluate_m6.py` 3 用例与 `tests/test_m6_evaluation.py` 4 项、时间线连续性与标题卡回归 3 项、`episode-state.js` 3 项前端测试；安装态 wheel 3/3 PASS、受控 Chrome 7/7、真实镜头复用 89.1667 秒整集（新增 submit=0）。详见 [E4 验收](./docs/M6_E4_ACCEPTANCE.md)。

2026-10-23 M6 E3 本地完整回归：**1335 passed / 3 skipped**（1266.19 秒，0:21:06；3 项 skip 为既有的 Windows 符号链接权限用例），前端状态测试 **48 项** 通过，Ruff check/format（195 个文件）与 `node --check` 通过。E3 新增 30 项定向用例（后端 25 + 前端 5），工具面 13/13（冻结 11/11），schema 8（v7→v8 只加 `renders`）。详见 [E3 验收](./docs/M6_E3_ACCEPTANCE.md)。

2026-10-22 M5 E3 本地 Python 3.12.14 完整回归：**1215 passed，3 skipped**（677.10 秒；E2 基线 1203/3），前端状态测试 **31 项** 通过，Ruff check/format 通过（161 个文件）。E3 新增 `episode_generate` 整集批量登记与 `shot_redo` 逐镜头重做（tools/rules **9/9**，冻结 8/8）、集级额度跨 Run 累计与 Store 重算、集级 `RunLimitsSnapshot`（`scope=episode`）、20 成员集合等待与 `episode_read.generation`；`tests/test_m5_generation.py` 12 项覆盖 12 镜头/60 秒全链路、单镜头失败隔离与重做修复、跨 Run 额度与 submit=0。详见 [E3 验收](./docs/M5_E3_ACCEPTANCE.md)。

2026-10-21 M3 E3 本地 Python 3.12.14 完整回归：**1083 passed，3 skipped**（跳过项为当前 Windows 账户无符号链接创建权限的既有边界与本包新增的同类用例），前端状态测试 **31 项**通过，Ruff check/format 与前端语法检查通过。E3 新增 headless 两阶段计划/授权/集合等待/本地拼接交付、`--json` 固定字段与退出码 0/2/3/4/5/6/130、Run 内可选 `headless` 状态与 `--resume` 原授权续接（含工具/skills/上下文签名复核），以及 `VAGENT_HOME/skills` 合并发现、内置名优先、坏文件隔离与显式根整体替换。全部供应商路径为受控 HTTP、媒体为本地测试文件，视频 submit=0；范围、逐项证据与保留的边界见 [E3 验收](./docs/M3_E3_ACCEPTANCE.md)。

2026-10-21 M3 E4 本地 Python 3.12.14 完整回归：**1117 passed，3 skipped**（471.47 秒；E3 基线 1083/3），前端状态测试 **31 项**通过，Ruff check/format 通过（148 个文件）。E4 新增 `scripts/evaluate_m3.py` 6 用例离线套件与 `tests/test_m3_evaluation.py` 31 项（含 22 类评分器反例）、安装态 wheel smoke 的成片工作台与 6 用例段、受控浏览器 7/7，以及零新增费用复用 M2 真实三个万相 MP4 的安装态成片（4,501,422 字节/10.096549 秒、42 项检查）。E4 期间修复 `film_assemble` 缺少执行上下文、`film-state.js` 未提供、设置页切换供应商后 Key 输入仍禁用、AAC `esds` 码率描述被误当拼接配置四个缺陷。新增付费样本未执行，视频 submit=0；详见 [M3 验收](./docs/M3_ACCEPTANCE.md)。

2026-10-21 M3 E1 本地 Python 3.12.14 完整回归：**1007 passed，2 skipped**，前端状态测试 **31 项**通过，Ruff check/format 与前端语法检查通过。E1 新增 schema v5 与 v4→v5 迁移、`mp4-copy-signature-v1`、拼接引擎与时序算法、成片域与服务、工具 5/5 与 4/4 恢复、CLI/Web/HTTP 面；双片段真实拼接输出 SHA-256 与 E0 记录的修正变体逐字节一致，视频 submit=0。范围、逐项证据与保留的边界见 [E1 验收](./docs/M3_E1_ACCEPTANCE.md)。

2026-10-10 C2 本地 Python 3.12.14 完整回归：**762 passed，2 skipped**，另有 **11 项前端状态测试通过**，Ruff 与前端语法检查通过。两项跳过因当前 Windows 账户无符号链接创建权限；junction 路径保护测试通过。新增媒体来源/传输/MP4/原子提交、6 个媒体强退点、持久下载/修复、Range、设置与等待验证，M1-A/B/C1 的兼容套件保持通过。Chrome 155 已验证测试视频播放、拖动、下载、刷新与同 ID 修复；范围见 [C2 验收](./docs/M1C_C2_ACCEPTANCE.md)，此前 C1 证据保留在 [C1 验收](./docs/M1C_C1_ACCEPTANCE.md)。Node 仅用于开发检查，运行 Agent 无需安装。

C2 收尾修正了粗粒度单调时钟下的恢复预算边界：先扣除已用时间，再计算截止时间，避免预算恰好耗尽时因浮点余差多登记模型步骤。固定时钟用例覆盖 execution v1/v2，恢复、Runner 和媒体应用定向回归 42 项通过；首轮远端失败及后续结果见 [C2 验收](./docs/M1C_C2_ACCEPTANCE.md)。

C2 的 wheel/sdist 位于 `dist/m1c-c2/`，新建独立环境安装锁定依赖与 wheel 后，仓库外 `scripts/wheel_smoke.py` 和 `pip check` 均通过；预算修复后的新包也已重新安装并通过。记录保存在被忽略的 `output/m1c-c2-wheel-smoke.json` 和 `output/m1c-c2-wheel-smoke-budget-fix.json`，覆盖已有文本、Mock、配置、MCP、持久等待和退出；包内媒体模块/Web 资源与源码一致，sdist 保留视频夹具原 SHA-256。专项安装媒体评测与真实联调仍属于 C3。远端检查按 [C2 分支 Actions](https://github.com/shifang37/vAgent/actions?query=branch%3Acodex%2Fm1c-c2) 的实际运行结果查看。

M1-B 固定评测默认离线，通过完整应用入口执行五类用例；`--live` 才调用真实 DeepSeek，视频始终使用 Mock。套件最多 40 次模型尝试，每个 Run 沿用 8/12/180 预算。首个失败即停止，显式续跑校验原数据与检查点，保留原 Run、累计预算和旧报告；不能通过续跑提高上限。

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_m1b.py
.\.venv\Scripts\python.exe scripts/evaluate_m1b.py --live --output output/m1b-live.json
.\.venv\Scripts\python.exe scripts/evaluate_m1b.py --live --continue-from output/m1b-live.json --output output/m1b-resumed.json
```

B5 的源码包和 wheel 位于 `dist/m1b-b5/`。独立环境在仓库外验证 CLI、Web、MCP、完整五类离线套件和等待跨服务重启后续接原 Run；`scripts/wheel_smoke.py --output <新报告路径>` 保存安装证据。报告、真实套件的 Token/调用次数及故障矩阵见 [M1-B 验收](./docs/M1B_ACCEPTANCE.md)。

B3 的源码包和 wheel 已构建到 `dist/m1b-b3/`。独立虚拟环境在仓库外通过 10 项安装检查，包括持久等待跨服务重启后续接原 Run，以及安装版 SQLite 节点 pending writes 强退恢复；Worker 由脚本显式推进。完整范围与证据见 [B3 验收](./docs/M1B_B3_ACCEPTANCE.md)。

已验证离线 CLI，以及 wheel 安装到独立虚拟环境后在仓库目录之外运行 `skills list`、`demo`、`inspect`。本次新增验证：新建独立虚拟环境安装 wheel，保存产物后取消，再从仓库外通过 CLI `resume` 完成原 Run，仍仅有一个产物；`pip check` 通过。源码包与 wheel 仅本地构建，未发布 PyPI。GitHub Actions 配置 Ubuntu/Windows、Python 3.11/3.12 检查，远端结果见 [Actions](https://github.com/shifang37/vAgent/actions)。

用量观测版本的 wheel 也通过独立虚拟环境验证：从仓库外恢复离线 Run 后执行 `usage --run`，显示累计 4 次适配器调用，并将未报告的 Token 和缓存明细保留为未知。

上下文 v2 的 wheel 已在同一独立环境重新安装验证：仓库外执行 `skills list`、取消后 `resume`、`inspect`、`usage --run` 均通过，格式版本保持 2、最终仍仅有一个产物，`pip check` 通过。v1 检查点兼容由自动化测试覆盖，详细记录见 [上下文优化验收](./docs/CONTEXT_OPTIMIZATION_ACCEPTANCE.md)。

自动化测试验证工程行为。M1-A 的完整真实套件 9/9 通过，保留了此前连接故障、步数耗尽和超长拒绝的全部证据；M1-B 的真实 DeepSeek + Mock 套件 5/5 通过，共观测输入 55,036、输出 3,115 Token，用量完整。两个阶段均已验收；单套通过不代表生产成功率或创作质量保证，也不代表真实视频交付。证据见 [M1-A 验收](./docs/M1A_ACCEPTANCE.md) 和 [M1-B 验收](./docs/M1B_ACCEPTANCE.md)。
