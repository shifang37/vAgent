# M6 E4 验收：整集成片、浏览器字幕开关与真实镜头复用

日期：**2026-10-23**。基线：`master` `e95e4ba`（M6 E3，schema 8、tools/rules 13/13、冻结 11/11…1/1）；本包交付提交见 `git log`。范围见 [M6 契约](./M6_CONTRACTS.md) §5/§10 的 E4，冻结决策见 §1。**E4 未调用文本模型；视频 submit=0（真实镜头来自已批准的 M5 E4 样本）；真实 TTS 请求=0。唯一真实付费样本是 M5 E4 已记录的 18 次 Wan 文生视频提交，本包只复用其已下载镜头，未新增提交、未新增费用。**

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 离线端到端套件 | `scripts/evaluate_m6.py` | 默认离线、`--live` 明确拒绝；3 个用例走真实 Store、分集/分镜/字幕/配音工具、受控 Wan 传输、Job/Media Worker、渲染服务与包内 FFmpeg。Wan 由 `httpx.MockTransport` 提供，现场媒体为本地冻结 MP4。`--only` 仅用于开发调试 |
| 套件用例 | 同上 | `episode-both`（8 场 19 镜头、19 条本地配音、27 条字幕、2 个转场、2 张标题卡、89.3 秒、同指纹重放）；`episode-failure`（单场未交付只标记该场，其余 7 场完成、不提交整集）；`episode-refusals`（缺确认、末尾转场、缺字幕轨全部拒绝且不写任务） |
| 套件测试 | `tests/test_m6_evaluation.py` | 4 项：整套报告与 89.3 秒成片、`--live` 拒绝、不覆盖报告/不复用非空目录、套件身份与纯函数 |
| Web 整集成片面板 | `web/app.js`、`web/episode-state.js`、`web/styles.css` | 分集面板新增整集播放器、逐场渲染状态、旁挂 SRT 下载与**字幕开关**；字幕按旁挂 SRT 在客户端叠加，`mov_text` 原生轨保持隐藏；快照刷新时保留播放位置与播放/暂停状态 |
| 前端状态与测试 | `web/episode-state.js`、`tests/test_episode_state.mjs` | 新增 3 项纯函数测试（成片投影/摘要、SRT 解析、cue 查找与开关标签），总计 8 项；CI 的 `node --check` 与 `node --test tests/test_episode_state.mjs` 覆盖 |
| 时间线连续性修复 | `src/vagent/video/subtitles.py`、`src/vagent/video/render_service.py`、`src/vagent/video/subtitle_service.py` | 以镜头**时长**重建精确连续的起点，避免成片时间线把起点舍入到微秒、时长保留精确有理数时出现 1 µs 内的接缝；E1 旁挂导出按微秒基比较时间线。新增 2 项回归测试（字幕时间线与渲染时间线） |
| 真实镜头复用样本 | `tmp/m6-e4-real/run.py`、`output/m6-e4-real/` | 复制 M5 E4 的 `fridge-notes` 18 个真实 Wan 镜头，离线生成 11 条本地配音、19 条整集字幕，渲染 **89.1667 秒**整集成片（2 个 1.0 秒转场、2 张标题卡、soft 字幕）；新增视频 Job=0 |
| 受控浏览器 | `tmp/m6-e4-browser/check.mjs`、`output/m6-e4-browser.json` | Chrome headless 7 阶段：整集面板、89.2 秒/720P/−16.0 LUFS、19 条字幕、首条 cue 显示、字幕开/关/再开、成片 Range、零页面错误 |
| 安装态 wheel | `scripts/wheel_smoke.py` | 新增 `smoke_m6_evaluation`（安装态运行 3 用例）与 `smoke_m6_workbench`（CLI `film renders`、HTTP `/api/renders`、分集详情/会话快照、旁挂 SRT、成片 Range、静态资源） |
| 验收文档 | 本文件、[M6 契约](./M6_CONTRACTS.md)、[M5 计划](./M5_PLAN.md)、[E3 验收](./M6_E3_ACCEPTANCE.md)、`README.md` | 记录交付、边界、失败与复现 |

## 2. 关键实现决策（相对计划的细化）

1. **真实镜头复用，不新增付费样本。** M5 E4 已批准的 `fridge-notes` 18 个 Wan 镜头与 7 条逐场成片是本地不可变证据；E4 复制该数据目录后只调用本地 `local-tts-v1` 与包内 FFmpeg。真实 CosyVoice、真实 i2v 参考图和任何新 Wan 提交仍需单独授权，能力表继续标注「文档支持，未做真实样本」。
2. **整集成片是逐场镜头在集级时间线上的重组。** 复用 M5 的 18 个镜头，按场序拼接为一条 89.1667 秒整集；不重拼、不重下、不重提，保留逐场成片与旧版本。
3. **字幕开关由旁挂 SRT 驱动。** `soft` 成片内嵌 `mov_text` 并写旁挂 SRT；浏览器对 `mov_text` 的支持不一致，因此面板从受控 `sidecarUrl` 读取同一份 SRT，在播放器上叠加/隐藏字幕。原生轨被置为 `hidden`，避免支持 `mov_text` 的浏览器重复显示；开关是纯前端只读动作，不写 Store。
4. **快照刷新不打断播放。** 分集面板会在会话快照更新时重建；E4 在重建前记录同一成片的 `src/currentTime/paused`，重建后恢复，避免字幕开关或状态更新把播放器重置到 0 秒。
5. **时间线以精确连续的起点为准。** 真实 AAC 镜头的成片时间线把段起点记为微秒、时长保留精确有理数；字幕轨要求严格连续，直接使用舍入起点会在 1 µs 内失败。修复后字幕、渲染与 E1 旁挂导出共享同一套「按精确时长累加起点」的规则；不改 schema、不改工具版本、不改冻结语义。
6. **失败保留而不是回滚。** 单场未交付时渲染任务标记该场 `failed`、其余场继续，不提交整集；已交付镜头、逐场成片与媒体保留。拒绝矩阵在任何编码前返回，不创建任务或成片。
7. **E4 不新增模型/供应商费用。** 离线套件与安装态均使用受控 HTTP 和本地 TTS；真实样本只复用已有镜头。报告显式记录 `videoSubmit=0`、`textModelCalls=0`、`realTtsRequests=0`。

## 3. 验证结果

### 3.1 离线套件

```text
.\.venv\Scripts\python.exe scripts/evaluate_m6.py --home <new> --output <new.json>
3/3 passed；89.3 秒整集；56 次受控提交、56 个交付、1 个成片版本、1 次同指纹重放、27 条字幕；
真实供应商调用 0、文本模型调用 0、真实 TTS 请求 0。
```

- `episode-both`：8 场 19 镜头全部交付；`both` 字幕；2 个转场（0.4 秒 crossfade + 0.3 秒 fade）与 2 张标题卡；输出 **89.3 秒**、1280×720/H.264 + AAC 48k 立体声 + `mov_text`；19 条 voice 轨；响度 **−16.0 LUFS / true peak −14.4 dBTP**；`blackdetect` 无黑帧；同指纹第二次调用 `replay=true` 且版本数仍为 1。
- `episode-failure`：第 3 场未交付，`RENDER_SOURCE_MISSING` 只标记该场 `failed`，其余 7 场 `succeeded`；不提交整集，18 个已交付镜头与逐场成片保留。
- `episode-refusals`：`CONFIRMATION_REQUIRED`、`TRANSITION_INVALID`、`SUBTITLE_TRACK_MISSING` 全部拒绝；无成片、无渲染任务。

报告：`output/m6-e4-suite.json`（`suiteId=8cde4238f3632b4b`）。

### 3.2 受控浏览器

夹具为真实镜头复用样本（`tmp/m6-e4-real/home`），Chrome 155（`channel=chrome`，headless，1440×1000）：

| 阶段 | 结果 |
|---|---|
| 载入工作台与会话 | 通过 |
| 打开分集面板 | 7 个场记、`18/18` 交付、`54.00 / 60.00 CNY`、整集成片 89.2 秒 / 1280×720 / −16.0 LUFS / 软字幕 |
| 整集播放器 | `videoWidth/Height=1280/720`、时长 89.17 秒、播放推进 |
| 字幕开 | 首条 cue `粥在锅里`（00:09.126–00:14.189）在 overlay 中可见，按钮 `字幕：开` |
| 字幕关 | overlay 隐藏，按钮 `字幕：关` |
| 字幕再开 | overlay 恢复同一条 cue，按钮 `字幕：开` |
| 成片媒体 HTTP | 受控 Range 返回 206 与 1024 字节 |
| 页面错误 | 0（page/console） |

证据：`output/m6-e4-browser.json`、`output/m6-e4-episode-panel.png`、`output/m6-e4-subtitles-on.png`、`output/m6-e4-subtitles-off.png`。

### 3.3 真实镜头复用样本

`fridge-notes`（M5 E4 已交付 18 镜头 / 7 场 / 90 秒）→ 离线 11 条配音 / 19 条字幕 → **89.1667 秒**整集成片。

| 项 | 值 |
|---|---|
| 新增视频提交 | **0**（源 Job 18 个，全部 `succeeded`；渲染后仍 18 个） |
| 新增费用 | **0.00 CNY**（只复用 M5 已下载镜头） |
| 文本模型调用 / 真实 TTS | 0 / 0（`local-tts-v1`） |
| 分集 | `episodeId=de84c2ed-ddec-44c9-9cc0-25110e753d6a`，7 场、18 镜头 |
| 成片 | 89.1667 秒、20,600,359 字节、SHA-256 `ad34ebb9…feb7a4b8`、1280×720、H.264+AAC、`mov_text` |
| 转场/标题卡 | 2 个 1.0 秒转场（crossfade + fade）、2 张标题卡（「冰箱上的便条」「未完待续」） |
| 字幕 | 19 条 cue，旁挂 SRT SHA-256 `c2928327…7776fc4e15`；首条 `粥在锅里`、末条 `嗯。` |
| 配音 | 11 条 voice 轨（画外/母亲，`local-narrator`/`local-female`），不调用 CosyVoice |
| 响度 | −16.0 LUFS、true peak −14.2 dBTP、LRA 8.5（EBU R128 复测） |
| 黑帧 | 0（`blackdetect=d=0.04:pix_th=0.08`） |
| 逐场成片 | 7 条 M5 逐场成片保留、未删除、未降级 |

逐镜头来源、字幕、响度、视频流参数与成片 SHA 见 `output/m6-e4-real/evidence.json`；成片副本与缩略图见 `output/m6-e4-real/episode-film.mp4`、`episode-thumb.png`。

### 3.4 安装态 wheel

`scripts/wheel_smoke.py` **PASS**（`output/m6-e4-wheel-smoke.json`）：

- 安装态运行 M6 3 用例：3/3 PASS、56 次受控提交、56 个交付、1 个成片版本、1 次重放、27 条字幕（45.0 秒）。
- 安装态 M6 工作台：`episodeId=a5ef65ed-1845-4c62-adf4-a1325b32f7dc`、`filmId=a34e8a92-f372-4827-ac0b-f72c2c64fd18`、89.3 秒；CLI `vagent film renders`、HTTP `/api/renders`、`/api/episodes/{id}`、会话快照、旁挂 SRT 与成片 Range 与源状态一致；`app.js`、`episode-state.js`、`subtitle-state.js`、`render-state.js`、`styles.css` 均可服务。
- 既有 M1–M5 与 keys-only 段全部保持通过（含 7 个 M2、6 个 M3、5 个 M5 用例）。

### 3.5 完整回归与门禁

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1342 passed, 3 skipped in 1080.30s (0:18:00)

.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
197 files already formatted

node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs tests/test_episode_state.mjs tests/test_subtitle_state.mjs tests/test_render_state.mjs
51 passed
```

- E3 基线为 1335 passed / 3 skipped；本包新增/更新：`test_m6_evaluation.py` 4 项、时间线与标题卡回归 3 项、前端 `test_episode_state.mjs` 3 项（含既有共 8 项）。
- 3 项 skip 仍是既有的“当前 Windows 账户无符号链接权限”用例，未新增 skip。
- Ruff check/format 全仓库通过（197 个文件）；前端六个状态文件 51 项 Node 测试通过。

四组 CI（ubuntu/windows × Python 3.11/3.12）在最终代码提交上的运行与结论见第 6 节。

## 4. 真实样本证据（明细）

见 `output/m6-e4-real/evidence.json`。关键结论：

- 18 个源镜头均来自 M5 E4 已批准样本，全部 `succeeded`；本包渲染前后 Job 数都是 18，`newVideoSubmits=0`、`newVideoJobs=0`。
- 整集成片由 7 个场记、18 个已交付镜头与 2 个跨场转场组成；逐场成片保留。
- 字幕轨从整集冻结台词确定性对齐，渲染时按转场夹取；旁挂 SRT 19 条 cue，首条 9.126 秒、末条 89.137 秒，均在 89.1667 秒成片内。
- 浏览器对分集面板与整集成片的实测：7 个场记、18/18、54.00/60.00 CNY、89.17 秒、字幕开/关、Range 206；页面错误 0。

## 5. 明确未做与边界

- **真实 CosyVoice 声音**：E4 只使用 `local-tts-v1`；真实 TTS 需要单独授权与字符/金额预算，能力表继续标注「文档支持，未做真实样本」。
- **真实 i2v 参考图与角色外观评分**：E4 真实样本是 t2v 镜头复用；离线 `wan-i2v` 已在 M5/M6 E2 覆盖引用与评分机制。
- **新增 Wan/Kling/fal 提交**：E4 submit=0；新付费样本需单独批准模型、次数与金额。
- **9:16 竖屏、1080P 超分/画质增强、去水印、字幕逐条可视化编辑、多集队列、时间线交互编辑、LoRA/声音克隆**：不在 M6；1080P 只作为参数化上采样 profile（E3 已验收）。
- **真实 BGM/SFX 素材导入**：E4 未引入第三方音乐；真实样本只有本地配音，无 BGM/SFX。
- 离线套件的受控上游只证明应用路径、字幕/音频/渲染边界与本地成片，不证明真实账号、CDN、编码与账单。

## 6. CI 与提交

- 最终代码提交：`5c888b7`（2026-10-23）。
- 四组 CI（`ubuntu/windows × Python 3.11/3.12`，`.github/workflows/ci.yml`「Agent checks」）在该提交上的运行与 check runs：（待填）。
- 汇总证据保存为 `output/m6-e4-ci-<sha>-summary.json`；（待填）。

## 7. 失败与偏差记录

1. **真实 AAC 时长导致字幕时间线不连续（产品缺陷）**：M5 已交付镜头的成片时间线把每段起点舍入到微秒，但时长保留精确有理数（如 `55822/11025` 秒）；`SubtitleTrackVersion` 要求严格连续，保存整集字幕时报 “Timeline entries must be contiguous from zero”，渲染的轨道精确比较也会在 1 µs 内失配。修复：`_film_entries`、`_assign_timeline` 与 `_plan_episode` 都按精确时长累加起点；E1 旁挂导出改在微秒基比较时间线。回归见 `test_m6_subtitles.py`、`test_m6_renders.py` 新增 2 项。
2. **快照刷新重置整集播放器（产品缺陷）**：分集面板在会话快照更新时重建 DOM，整集播放器回到 0 秒，字幕开关“再开”时找不到当前 cue。修复：重建前记录同一成片的 `src/currentTime/paused`，重建后恢复；受控浏览器复测开/关/再开全部通过。
3. **初始字幕面板没有整集播放器**：E3 只在分镜面板提供成片播放器；E4 在分集面板新增整集播放器、逐场渲染状态与旁挂 SRT，仍复用现有成片播放器语义。
4. **`mov_text` 浏览器支持不一致**：E4 不假设浏览器能显示内嵌字幕，改用同一份旁挂 SRT 的客户端 overlay；原生轨置为 `hidden`，避免重复显示。sidecar 与内嵌轨均保留，供下载与兼容播放器使用。
5. **安装态 M2 段仍用 64 KiB 测试预算（套件缺陷）**：E1 已把 pytest 夹具提高到 96 KiB（`tests/test_m2_evaluation.py`），但 wheel smoke 调用 `evaluate_m2.py` 时仍用默认 64 KiB；M6 的 13/13 工具规则使 `batch-delivered` 实测 **76,869 字节**，触发 `CONTEXT_LIMIT`。修复：wheel smoke 显式传 `--context-bytes 98304`，与 pytest 夹具一致；产品默认仍是 `VAGENT_CONTEXT_BYTES=65536`，未改动。前两轮失败保留在运行记录中，第三轮通过（见 §3.4）。
6. **Linux 的 imageio-ffmpeg 没有 `drawtext`（产品缺陷）**：E4 首次在 Ubuntu CI 上运行 E3 渲染用例时，所有带标题卡的成片都报 `RENDER_ENCODE_FAILED`（`Filter not found`）。`imageio-ffmpeg==0.6.0` 的 Linux 静态构建带 libass 与 `subtitles`，但没有 libfreetype 的 `drawtext`；Windows 构建有。修复：标题卡改为同一包内字体的 ASS 事件，经 `subtitles=titles.ass:fontsdir=fonts` 烧录，`title-zh-v1` 文字、时间与居中语义不变；新增 ASS 标题卡单测，并在 Ubuntu 容器复跑渲染/API/整集/评测用例（11 passed）。
7. **Windows CI 的烧录帧固定 SHA 不可跨 runner 复用（套件脆弱性）**：E1 的 `test_burn_export...` 固定了烧录帧 PNG 的 SHA-256；GitHub Windows runner 的系统字体栈与本地 Windows 11 不同，同一 FFmpeg/字体二进制输出 `9e705833…` 而不是本地 `7dd49572…`，导致 Windows CI 两个矩阵失败。修复：测试接受这两个已验证 hash，并保留“烧录帧 != 源帧”与包内字体指纹断言；产品与字体未变。这是固定像素测试的可移植性修正，不是放宽字体校验。

## 8. 复现说明

1. 全新数据目录运行离线套件：`VAGENT_HOME=<new> .\.venv\Scripts\python.exe scripts/evaluate_m6.py --home <new> --output <new.json>`；重放同指纹使用相同参数。
2. 真实镜头复用：先确认 `tmp/m5-e4-real/home` 存在且 18 个 Job 均为 `succeeded`，再运行 `tmp/m6-e4-real/run.py --confirm`；脚本复制数据目录，不写源目录、不提交新 Job。
3. 浏览器：`VAGENT_HOME=tmp/m6-e4-real/home VAGENT_VIDEO_MODE=mock .\.venv\Scripts\python.exe -m vagent web --port 3311 --no-open`，再运行 `node tmp/m6-e4-browser/check.mjs`；报告与三张截图写入 `output/`。
4. 安装态：构建 wheel、在独立环境安装后运行 `scripts/wheel_smoke.py --output output/m6-e4-wheel-smoke.json`。
