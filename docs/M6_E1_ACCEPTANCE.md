# M6 E1 验收：字幕（schema 7、确定性对齐、软字幕与烧录）

日期：**2026-10-22**。基线：`master` `2c0e7e7`（M6 E0 契约与 `schema 6` 基线已合入；v0.4.0 = `b8df786`）。范围见 [M6 契约](./M6_CONTRACTS.md) §10 的 E1，冻结决策见 §1，基线证据见 [E0 验收](./M6_E0_ACCEPTANCE.md) 与 `docs/M6_E0_BASELINE.json`。**E1 全程视频 submit=0、文本模型调用=0、TTS 请求=0；未读取或调用任何供应商。**

## 1. 已交付

| 项目 | 交付 |
|---|---|
| 存储 | schema **7**：新增 `DatabaseV7`、`audio`/`subtitleTracks` 两个集合与 media 联合的 `kind=audio`（`contractVersion=3`）；`v6→v7` 只加两个空集合，v1–v6 全部可读；迁移前逐字节备份，不重编码历史对象 |
| 字幕域 | `src/vagent/video/subtitles.py`：`SubtitleTrack`/`SubtitleTrackVersion`/`SubtitleCue`/`SubtitleTimelineEntry`，只增不改的版本历史、目标唯一、cue 不重叠/不倒序/不越界、单条 ≤48 字符、每轨 ≤200 条、每场 ≤40 条 |
| 对齐 | `subtitle-align-zh-v1`：终止标点断句、次级标点切到 ≤24 字符、按场/镜头冻结时间线做字符比例分配、每条 ≥0.5 秒、间隙 0.05 秒、绝不跨镜头；整数微秒 + 最大余数分配，同一输入总是同一 cue 字节 |
| 工具与规则 | 新增 `subtitle_save`（省略 `cues` 时确定性生成，显式 `cues` 时人工修正），`storyboard_read`/`episode_read` 返回字幕轨与逐条 cue；tools/rules **9/9 → 10/10**（mock 与 live 同步），并按 `toolFeatures` 冻结 **9/9** 变体 |
| 序列化 | SRT（毫秒、两行、UTF-8）与 ASS（`subtitle-zh-v1` 样式、`PlayResX/Y` 随档位、内置字体名）确定性生成；`srtSha256`/`assSha256` 与受控下载地址 |
| 字体 | 包内 `VAgentSubtitleSC-400.ttf`（Noto Sans SC 源码实例化到 400 并改名，OFL-1.1 允许修改版分发）；子集 3,876 字符 / 4,315 字形 / 1,213,160 字节 / SHA-256 `3cb33efa…5606cb`；缺失或哈希不符返回 `FONT_MISSING`；随包附 `LICENSE-OFL.txt` 与 `manifest.json` |
| 导出 | `subtitle_service.py`：把已拼接成片 + 某一确定字幕版本渲染为新的成片版本（`FilmVersion.render`，`ffmpeg-render-v1`）；`soft` 写 `mov_text` + 旁挂 SRT，`burn` 用 `subtitles` 滤镜 + 包内字体，`both` 两者都做；音频原样复制、E1 不重编码；锁外执行、可停止、单次 FFmpeg ≤120 秒、整次作业 ≤600 秒；同指纹幂等，任一失败保留旧版本 |
| 媒体 | soft 成片含 `mov_text` 轨后仍通过 `mp4-avc-v1` 结构校验（接受 `sbtl/text/subt` 字幕轨，仍拒绝外部数据引用、重叠样本与未知轨道）；`FilmMedia` 与 Range/下载沿用 M3 面 |
| CLI | `vagent subtitle show|save|export`（离线、无 Key、未登记任何 Job；`--cues` 提交人工修正，`--request-id` 重放） |
| HTTP / Web | `GET /api/subtitles`、`POST /api/sessions/{id}/subtitles/save|export`、`GET /api/subtitles/{track}/{version}/srt|ass`；分镜面板新增字幕区（版本、cue 数、软/烧录导出、SRT/ASS 下载）与 `web/subtitle-state.js` |
| 验收文档 | 本文件；[M6 契约](./M6_CONTRACTS.md) 的 E1 细化和 [第三方组件](./THIRD_PARTY_ASSEMBLY.md) 的字体分发材料已同步 |

## 2. schema 7 与迁移证据

- `tests/test_m6_schema.py::test_fresh_store_is_schema_seven_with_two_empty_collections`：新数据目录直接是 `schemaVersion=7`，`audio={}`、`subtitleTracks={}`，`DatabaseV7` 校验通过。
- `test_v6_state_migrates_to_v7_with_an_exact_backup`：用真实产品路径生成分集、场记、绑定分镜、字幕轨与运行记录，再机械构造 v6 信封打开；迁移后 `schemaVersion=7`、两个集合为空、**其他每个集合逐字段相等**；`state-v6-*.json` 备份与原始字节逐字节一致；`checkpoints.sqlite` 哨兵不变；再次打开不再产生备份。
- 历史链：v1（`test_b0_compatibility`、`test_storage_migration`）、v1/v2（`test_m1c_migration`）、v3（`test_m2_migration`）、v4（`test_m3_baseline`）、v5（`test_m5_baseline`）全部继续迁移到 v7，原 v3/v4/v5/v6 信封投影哈希仍逐字节复现。
- 契约基线比对：`test_e1_keeps_every_baseline_schema_hash_except_the_declared_change` 重算 126 个 E0 模型哈希。**只有以下四项变化**（都源于 `FilmVersion.render` 这一个新增可选字段及其被嵌入）：
  - `vagent.video.films.FilmVersion`（新增 `render`）
  - `vagent.video.films.Film`（嵌入 `FilmVersion`）
  - `vagent.storage.DatabaseV5`、`vagent.storage.Database`（`films` 集合嵌入 `FilmVersion`）
  - 其余 122 个 E0 模型哈希**逐字节不变**；新增模型全部是 schema 7 的新增对象（`DatabaseV7`、16 个 render 模型、`SubtitleTrack*`/`SubtitleCue`、`AudioAsset*`/`AudioMedia`）。
- 上限与工具名分组：`storageSchemaVersion` 6→7 是唯一变化；镜头/集/等待/拼接上限与 M2/M3/M5 工具名分组逐项不变，新增 `M6_TOOL_NAMES=("subtitle_save","audio_generate","film_render","render_read")`。

## 3. `subtitle-align-zh-v1` 与“零漂移”

算法输入只有两样：绑定到分镜的 `SceneVersion.dialogue[]` 与冻结的镜头时间线（分镜已有成片时用成片逐段有理数时间线，否则用分镜镜头时长）。实现要点：

1. **断句**：`。！？；…!?;` 终止并按需吞掉连续终止符与收尾引号；句子 >24 字符时在 `，、：,;—` 处切到 ≤24；无法再切且 ≤48 时保留为一条两行 cue；>48 硬切。切分不丢字符（测试逐字符回拼）。
2. **归属**：场对话流按字符位置等比例映射到该场总时长，镜头边界处的 cue 被切开、两侧保留同一 `speaker`；逐 shot 再按字符数比例分配该镜头时长，先给每条 0.5 秒，再把余量按最大余数分配，间隙固定 0.05 秒，**绝不越界到下一镜头**。
3. **时间基**：内部全部是整数微秒；每个镜头窗口的终点只舍入一次（`timeline_windows_us`），最后一 cue 精确落在冻结时间线末端，不产生浮点累积。
4. **冻结与可复现**：字幕版本记录 `timeline[]`、`timelineSource`、`filmVersion`、`timelineSha256`、`sourceSha256`；相同输入字节 → 相同 cue、SRT、ASS 与 `trackSha256`。人工修正只校验、不重排。
5. **确定性**：无随机、无时钟、无文件系统依赖；SRT 时间戳 = cue 微秒向下取整到毫秒，ASS 到厘秒。

验收证据：

- `tests/test_m6_acceptance.py`：用 12 个真实交付片段拼出 **60.256 秒**成片（`latest.durationSeconds ≈ 60.2347`，呈现时间线 60.256000），保存字幕后**最后一条 cue 的微秒终点 == 该成片版本时间线的规范终点**，每条 cue 都落在自己的镜头窗口内；导出的旁挂 SRT 时间戳与 cue 毫秒逐一相等；soft 导出含 `mov_text`，时长偏差 ≤1 帧。
- `tests/test_m6_subtitles.py`：60.000 秒合成时间线（6×10 秒）上最后一条 cue 精确为 `60_000_000` 微秒；逐镜头“分配+间隙”精确填满；两次对齐逐字段相同；cue 不跨镜头；SRT/ASS 字节可复现。
- 分组与上限：`test_alignment_is_deterministic_and_keeps_the_dialogue`、`test_split_keeps_every_character_and_respects_the_limits`、`test_unbreakable_run_becomes_one_two_line_cue`。

## 4. 导出与失败矩阵

渲染指纹 = `sha256(filmId, 源成片版本+sha, storyboardVersion, shotVersionIds, 字幕轨 ID/版本/`trackSha256`, profile, 引擎版本+二进制 SHA, 字体指纹)`；同指纹直接返回原成片版本，不重复编码。导出前在锁内冻结输入，FFmpeg 在锁外执行，提交前复核源文件哈希与产物哈希，再把文件、`FilmVersion`（含 `render`）、`FilmMedia` 与 Operation 一次提交。

| 场景 | 结果 |
|---|---|
| soft | `mov_text` 轨存在、有 `zho`、默认关闭；旁挂 SRT 的 SHA-256 写入 `render.subtitle.sidecarSha256`；视频/音频复制、时长与源一致 |
| burn | 无字幕轨；画面与源不同；1.0 秒抽帧 SHA-256 `7dd49572…c52bd6c` 与内置 FFmpeg 7.1 + 包内字体逐字节一致（其它构建下跳过固定值断言）；日志无时间戳告警 |
| both | 既有烧录画面，又有 `mov_text` 轨与旁挂 SRT |
| 同输入重放 | 版本数不变、`outputMediaId` 不变 |
| 1080P / 未知模式 | `RENDER_PROFILE_UNSUPPORTED` |
| 字体缺失/被替换 | `FONT_MISSING`（即使 `fontRef` 常量正确也不渲染） |
| 用分镜计划时间线对齐的轨道导出到成片 | `RENDER_TIMELINE_MISMATCH`，不猜测重排 |
| 成片文件缺失/损坏 | `RENDER_SOURCE_MISSING` / `RENDER_VALIDATION_FAILED` |
| 人工 cue 重叠/倒序/跨镜头/超 48 字符/超 200 条/每场 >40 | `SUBTITLE_CUE_CONFLICT` / `SUBTITLE_OVERFLOW`，整次拒绝且旧版本保留 |
| 没有字幕轨却请求导出 | `SUBTITLE_TRACK_MISSING` |
| 编码失败/输出结构或时长校验失败 | `RENDER_ENCODE_FAILED` / `RENDER_VALIDATION_FAILED` |

定向用例：`tests/test_m6_subtitle_export.py`（7 项）覆盖 soft/burn/both、幂等、1080P/未知模式、字体缺失、计划时间线与成片不一致、`mov_text` 校验与固定帧哈希；`tests/test_m6_subtitle_api.py`（3 项）覆盖 CSRF/严格字段、保存/读取/导出、SRT/ASS 下载 SHA 与 Range 无关的确定性、请求 ID 冲突、1080P 拒绝、未知字幕版本；`tests/test_m6_subtitle_cli.py`（1 项）覆盖 show/save/export、人工修正重渲染、同请求重放与过期 revision。

## 5. 工具面与恢复

- 新 Run 的 `toolFeatures.video` 为 `toolsVersion=10`、`rulesVersion=10`，规则文本在 9/9 之后追加 `SUBTITLE_RULES`；`subtitle_save` 是 `write`、无 Job、无费用。
- `register_tools_variant(9, ...)` 冻结 M5 E3 的 9/9 工具集合并隐藏 `subtitle_save`；8/8、7/7、6/6、5/5、4/4、3/3、2/2、1/1 变体继续按 `toolFeatures` 恢复（`test_m6_tools.py`、`test_m3_recovery.py`、`test_m2_compatibility.py`）。
- `storyboard_read`/`episode_read` 只加字段（`subtitle`），旧字段与冻结签名不受影响；只读视图不含 `subtitle_save`。

## 6. 检查结果

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_m6_schema.py tests/test_m6_subtitles.py tests/test_m6_subtitle_service.py tests/test_m6_tools.py tests/test_m6_subtitle_export.py tests/test_m6_subtitle_api.py tests/test_m6_subtitle_cli.py tests/test_m6_acceptance.py
node --test tests/test_subtitle_state.mjs
.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
```

- M6 定向用例 **39 passed**（schema 9 项、对齐 6 项、服务 5 项、工具 7 项、导出 7 项、API 3 项、CLI 1 项、60 秒验收 1 项），`node --test tests/test_subtitle_state.mjs` **7 passed**（全部前端状态测试 43 项通过）。
- 机械更新：既有断言中“当前 9/9”改为 10/10（`test_live_application`、`test_m2_clients`、`test_m2_compatibility`、`test_m2_generation`、`test_m2_integration`、`test_m3_recovery`、`test_video_integration`、`test_wait_compatibility`、`test_m5_tools`），“schemaVersion=6”改为 7（`test_b0_compatibility`、`test_m1c_migration`、`test_m2_migration`、`test_m3_baseline`、`test_m3_film_cli`、`test_m5_baseline`、`test_storage_migration`、`test_storage_tools`）；`test_video_integration` 的“规则被改动则拒绝恢复”补丁目标改为当前 `VIDEO_RULES_V10`。**没有放宽任何行为断言**。
- 离线 M2/M3 验收夹具的上下文预算由 64 KiB 提高到 96 KiB（`tests/test_m2_evaluation.py` 的 `options(context_bytes=...)` 与 `scripts/evaluate_m3.py` 的 `base_config`）：10/10 的规则与 `subtitle_save` schema 使原来的 64 KiB 夹具在最后一个模型轮次触发 `CONTEXT_LIMIT`；模型行为、工具语义与失败判定均未变。产品默认仍是 `VAGENT_CONTEXT_BYTES=65536`。
- 安装态证据：`pip wheel . --no-deps --no-build-isolation` 构建的 `shifang37_vagent-0.4.0-py3-none-any.whl` 含 `vagent/video/fonts/VAgentSubtitleSC-400.ttf`、`LICENSE-OFL.txt`、`manifest.json` 与 `vagent/web/subtitle-state.js`；字体文件的运行时 SHA-256 与 `subtitle_font.FONT_SHA256` 相同。
- 完整回归与 ruff 见第 7 节。

## 7. 完整回归

```text
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1272 passed, 3 skipped in 890.35s (0:14:50)
```

三项 skip 仍是既有的“当前 Windows 账号无 symlink 权限”skills 用例，与本包无关；新增 39 项 M6 用例全部通过。`ruff check`、`ruff format --check`（179 个文件）、`git diff --check`、`node --check web/app.js web/subtitle-state.js` 与全部前端状态测试（43 项）通过。基线对照：M6 E0 记录的 v0.4.0 回归为 1233 passed / 3 skipped；本包新增 39 项后端用例与 7 项字幕前端用例，并把既有断言按上文机械更新。

## 8. 明确未做与边界

- **整集字幕导出**：E1 生成并保存 `scope=episode` 的连续字幕轨（跨场时间轴已实现），但把整集轨道烧录/封装到整集成片属于 E3；E1 导出只接受 `scope=storyboard`。
- **音频、TTS、BGM 与响度**：E2 交付 `audio_generate`、`tts-providers-v1`、`local-tts-v1`、音色绑定、ducking 与两遍响度测量；E1 只冻结 `AudioAsset`/`AudioMedia` 记录并可读校验，音频媒体读取返回 `MEDIA_NOT_FOUND`。E1 导出复制源音频，不做增益/归一化，`render.audio` 为空轨道、`render.measurements.audio` 记录实测编码/采样率/声道。
- **转场、标题卡、1080P 与整集渲染**：E3 的 `film_render`/`render_read`；E1 的 `RENDER_VERSION` 只覆盖字幕子集，1080P 返回 `RENDER_PROFILE_UNSUPPORTED`。
- **Web 逐条 cue 编辑**：E1 的 Web 只做生成/重生成、软/烧录导出与 SRT/ASS 下载；人工逐条修正走 `vagent subtitle save --cues` 或 `subtitle_save` 的 `cues` 参数（M7 的时间线编辑再做可视化）。
- **真实浏览器与真实供应商**：E1 只做离线与单元/集成验收；受控浏览器播放与字幕开关、真实样本成片属 E4，且需单独授权。
- **9:16 竖屏**仍未实现，画幅保持 16:9。
