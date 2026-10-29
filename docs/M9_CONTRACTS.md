# M9 契约：竖屏 9:16

状态：**E0 已冻结（2026-10-25），方向暂缓（2026-10-29）**——产品方已确认本轮只用横屏 720p，竖屏不排期；本契约作为「将来做竖屏时直接续做」的冻结依据保留，代码仍只有 v3/v6/16:9。基线 `master` `6e45e92`（v0.6.0：schema 9、tools/rules 18/18、168 个契约模型、`1516 passed / 4 skipped`）。本文件是 M9 的契约，`docs/M9_PLAN.md` 是计划；两者冲突时以本文件为准。机器可读基线：`docs/M9_E0_BASELINE.json`（SHA-256 `afd1b38f…59b694b`），由 `scripts/capture_m9_baseline.py` 生成。E0 未改产品代码：视频 submit=0、模型调用=0、TTS=0、凭据读取=0。

## 1. 决策与冻结结果

| 编号 | 决策 | 冻结值 |
|---|---|---|
| D1 | 画幅来源 | **配置级** `videoFrame`（`16:9` 默认 / `9:16`），一个项目一个画幅；登记时把画幅**冻结进 Job**；分镜工具不加字段、不加工具 |
| D2 | 契约代次 | 新增 `ShotSpecV4`/`VideoRequestV7`/`JobV7` 只承载竖屏；`ShotSpecV3`/`VideoRequestV6`/`JobV6` 与 `multi-provider-v3` **逐字节保留可读** |
| D3 | 竖屏几何 | 只做 **1080×1920**（30 fps、H.264、AAC 48 kHz 立体声）；**不做** 720×1280、1:1、5:4 |
| D4 | 供应商覆盖 | 能力表**按画幅发布**：`16:9` 表与 M8 逐字节一致（`capabilitiesSha256` = `da6aa7b7…`），`9:16` 表只有在官方价格核实并升 `priceVersion` 后才发布该供应商的竖屏条目；未核实返回 `ROUTE_UNAVAILABLE`，**不退回 16:9** |
| D5 | 字幕与标题卡 | 新增渲染档 `subtitle-zh-v2`（字号 = round(高度×0.035) → 67@1920、底部边距 = 高度×0.12 → 230、左右边距 = 宽度×0.06 → 65、每行 ≤14 字符）；`subtitle-zh-v1` 与 16:9 输出逐字节不变；不做竖排 |
| D6 | 渲染与时间线 | 复用 `film_render` 与 `scope=timeline`；新增 `ExportProfile` 值 `1080p-vertical-h264-aac-v1` 与归一化 profile `h264-1080x1920-30fps-aac-48k-stereo-v1`；时间线画幅与已登记 Job 不一致 → `TIMELINE_FRAME_MISMATCH` |
| D7 | 价格与预算 | 竖屏单价必须单独核实（Kling `0.112 USD/s` 只适用 720p）；逐集 20 Job / 60.00 CNY、批量 3 集 / 60 Job / 180.00 CNY 口径不变；**v7 不得绕过集级上限**、不换汇 |
| D8 | 发布与边界 | 发布 **v0.7.0**；不做混合画幅分集、跨集横竖混剪、移动端专属页面、PyPI 公开 |

## 2. 冻结事实

### 2.1 16:9 基线（M9 全程必须逐字节不变）

| 项 | 冻结值 |
|---|---|
| `contractsSha256` | `ba9d8d7b4888f4c21ec58b269836f813bf7cbf850ada9881a7f77751301b2e27` |
| 契约模型数 | 168 |
| `capabilitiesSha256`（`16:9` 表） | `da6aa7b7889a616bc944d965739deacab91288b02b8d07c892c260301a52d1bb` |
| live 工具 `specsSha256` / `instructionsSha256` | `1efcb3f9…bbebe4` / `e81a2ef7…03e060` |
| `routeProbe.rowsSha256` | `3817d19141f1e73b075722328b645be08e6a3ac2d4748caeea1e819c21f06413` |
| t2v 画幅 | `ShotSpecV3` 只 `16:9`；`VideoRequestV6.parameters.ratio` 只 `16:9` |
| 已发布能力条目 | 4 条（wan t2v / wan i2v+flf2v / kling t2v / fal t2v），均为 `5 s / 720P / 16:9` 或图生 |
| 导出 profile | `720p-h264-aac-v1` = 1280×720（`upscaled=false`）、`1080p-h264-aac-v1` = 1920×1080（`upscaled=true`） |
| 归一化 profile | `h264-1280x720-30fps-aac-48k-stereo-v1` |
| 媒体几何校验 | `src/vagent/video/media.py:295` 要求 1280×720 |
| 字幕档 | `subtitle-zh-v1`：每行 ≤24 字符、`ASS_STYLE = {font_size 52, margin_v 54, margin_lr 40, outline 2.5, shadow 1.0}` 按 720 反推 |

`docs/M9_E0_BASELINE.json` 的 `frames.horizontal` 逐项记录上述值；E1–E4 每次验收都比对这一节。

### 2.2 画幅模型

- 画幅**不是**项目字段、不是 schema 集合、不是工具参数：它是配置项 `videoFrame`（`VAGENT_VIDEO_FRAME`，取值 `16:9`/`9:16`，默认 `16:9`）。
- 登记时读取一次并写进 Job 的 `spec.aspectRatio`（`ShotSpecV4`），此后该 Job 的画幅不可变；改配置只影响之后登记的任务。
- `videoFrame` **不进** `Run.toolFeatures`、**不进** 上下文签名：因此 `16:9` 下 features 与 M8 逐字节一致，pre-M9 的 live Run 仍可恢复。切到 `9:16` 后能力表变化，features 不再匹配任何已冻结变体，旧 Run 恢复返回 `RESUME_CONFIG_CHANGED`（与「能力版本变化」的既有语义一致，不伪造兼容）。
- 一个项目一个画幅：同一项目的分镜、分集、季、批量、时间线都按当前配置登记；混画幅分集**不支持**。

### 2.3 竖屏几何

| 项 | 冻结值 |
|---|---|
| 像素 | 1080×1920，30 fps |
| 视频 | H.264、`crf=20`、`pix_fmt=yuv420p`（沿用 M6 导出参数） |
| 音频 | AAC 48 kHz、立体声、192 kbps |
| 导出 profile | `1080p-vertical-h264-aac-v1` |
| 归一化 profile | `h264-1080x1920-30fps-aac-48k-stereo-v1` |
| `upscaled` | 任一源镜头短于 1080×1920 时为 `true`；全部达标为 `false` |
| 时长上限 | 沿用 90 秒、≤20 镜头、≤19 转场 |

`assembler.py`、`films.py`、`media.py`、`render_service.py` 的 1280/720 硬编码在 E2 改为 profile 驱动；16:9 输出必须用固定 SHA 回归证明未变。

### 2.4 字幕与标题卡

- `subtitle-zh-v2` 是**渲染档**，写在 `SubtitleTrackVersion.styleProfile`；新建字幕轨按登记时的 `videoFrame` 选择：`16:9` → `subtitle-zh-v1`，`9:16` → `subtitle-zh-v2`。
- v2 参数（按 1080×1920 计算，均为整数）：`fontSize = round(height × 0.035) = 67`、`marginV = round(height × 0.12) = 230`、`marginLR = round(width × 0.06) = 65`、每行 ≤14 字符、`outline = 2.5`、`shadow = 1.0`、颜色沿用 v1。
- cue 文本与 48 字符/条、≤200 条/轨、0.5 秒下限、0.05 秒间隙、不跨镜头等 **v1 对齐语义不变**；SRT 旁挂仍按 24 字符/行（外部播放器口径），不随画幅变化。
- 标题卡：竖屏版式按 width/height 参数化（`render_title_ass` 已接受 width/height），字号与安全区按 v2 比例；最多 4 条不变。
- 渲染时字幕档与画幅不一致 → `SUBTITLE_STYLE_FRAME_MISMATCH`（例如把 `subtitle-zh-v1` 轨烧到 1080×1920，或 v2 轨烧到 16:9）。

## 3. 契约代次（v7 家族）

### 3.1 形状

| 模型 | 冻结形状 |
|---|---|
| `ShotSpecV4` | `mode: t2v\|i2v\|flf2v`；`durationSeconds: 2–15`；`aspectRatio: "16:9"\|"9:16"\|null`；t2v 必须给画幅，i2v/flf2v 为 `null`（由参考图决定） |
| `VideoRequestV7` | `parameters.aspectRatio: "16:9"\|"9:16"`（t2v）；`ratio: "16:9"\|"9:16"\|null`；`capabilitiesVersion = "multi-provider-v4"` |
| `JobV7` | 沿用 `JobV6` 的字段集，`request` 为 `VideoRequestV7`、`spec` 为 `ShotSpecV4`；`cost.actual` 仍为冻结的 `unknown` |
| 能力表 `multi-provider-v4` | 在 v3 的 4 条 16:9 条目之外，按 §2.2 的画幅**分别**发布两条表：`16:9` 表 == v3 逐字节，`9:16` 表只含已核实条目 |

### 3.2 历史兼容

- v1–v6 的 Job/Request/Spec 与 `multi-provider-v3` 表逐字节保留，旧记录按自己的版本解析与重放。
- 竖屏 Job 只由 v7 承载；**不允许**把 9:16 写进 v6 记录，也不允许把 v6 记录改写成 v7。
- pre-M9 的 live Run 在 `16:9` 配置下 features 与 M8 一致，可恢复；切到 `9:16` 后不可恢复（`RESUME_CONFIG_CHANGED`），需按新会话重做。

### 3.3 校验与拒绝

| 场景 | 结果 |
|---|---|
| `videoFrame` 取值不是 `16:9`/`9:16` | `FRAME_UNSUPPORTED`（配置写入即拒绝） |
| 请求画幅与配置不一致（伪造 v7 请求） | `FRAME_UNSUPPORTED` |
| `9:16` + `720P` 或非 1080×1920 目标 | `FRAME_UNSUPPORTED` |
| `9:16` 的 i2v/flf2v 参考图不是 1080×1920 | `CHARACTER_ASSET_FRAME_MISMATCH`（不缩放、不裁切、不降级 t2v） |
| `9:16` 渲染时字幕档是 `subtitle-zh-v1`（或反之） | `SUBTITLE_STYLE_FRAME_MISMATCH` |
| 时间线内 clip 的 Job 画幅与当前渲染画幅不一致 | `TIMELINE_FRAME_MISMATCH` |
| `9:16` 供应商条目未核实价格 | `ROUTE_UNAVAILABLE`（不退回 16:9、不报价） |

## 4. 登记与预算不变量

- 竖屏 Job 的额度口径与 16:9 **完全相同**：逐镜头金额、每 Run 4 个新 Job、逐集 20 Job / 60.00 CNY、批量 3 集 / 60 Job / 180.00 CNY。
- v7 的集级校验必须与 v6 走同一套重算：从 Store 重算任务数与估算，核对 `shotRef` 归属、币种与单价；**v7 不得绕过 `EPISODE_COST_LIMIT`/`JOB_CONFLICT`**。
- USD 供应商 + 集级/批量继续整批拒绝（`EPISODE_COST_LIMIT`），竖屏不改变这条。
- 预算金额按新 `priceVersion` 的竖屏单价计算；未知价格不登记。

## 5. 渲染与后期

1. `ExportProfile` 增加 `1080p-vertical-h264-aac-v1`，`matches_the_frozen_geometry` 同步增加 `(1080, 1920, upscaled)` 分支。
2. `profile_for` 按 name 给出宽高；`assembler`/`films`/`media`/`render_service` 的几何全部改为 profile 驱动，禁止再写 1280/720 字面量。
3. 字幕：`render_ass`/`render_title_ass` 用当前 profile 的宽高与 v2 比例；16:9 仍走 v1 常量，输出 SHA 不变。
4. 时间线：`timeline` scope 复用 M7 语义（重排/裁剪/转场/标题卡/音轨），画幅由冻结 Job 推导；混用画幅拒绝。
5. 混音门（E0 前置已实现，作为冻结事实）：全静音 premix（`max_volume ≤ -90 dBFS`）在 loudnorm 之前直接返回 `LOUDNESS_OUT_OF_RANGE`「（混音为静音，未交付）」；时间线音频条目全部落在剪辑窗口外时返回 `RENDER_SOURCE_MISSING` 并列出条数与原因（`no-dialogue-line`/`no-cue`/`outside-edit`）。映射见 `src/vagent/video/mixer.py`（`SILENCE_PEAK_DB`、`parse_volume_peak`）与 `src/vagent/video/render_service.py`（`AudioPlan.dropped`、`_timeline_cue_start`）。

## 6. 面与配置

| 面 | 冻结行为 |
|---|---|
| 配置 | `videoFrame`（`VAGENT_VIDEO_FRAME`，默认 `16:9`）；`PATCH /api/config` 允许切换；切换只影响新登记 |
| CLI | `vagent route preview --frame 16:9\|9:16`（只读预览）；登记/渲染**不**接受画幅参数，一律读配置 |
| HTTP | `GET /api/route/preview?frame=`、`PATCH /api/config`、会话快照暴露 `videoFrame` |
| Web | 设置页画幅选择；报价对比显示行内画幅；9:16 播放器与字幕 overlay 使用 v2 安全区 |
| 工具面 | **18/18 不变**，不新增工具、不改 prompt/规则字节（`instructionsSha256` 不变） |

## 7. 错误码

新增 4 个（其余复用既有家族）：

| 错误码 | 含义 |
|---|---|
| `FRAME_UNSUPPORTED` | 画幅不受支持：配置值非法、请求画幅与配置不符、或请求了未开放的竖屏分辨率 |
| `CHARACTER_ASSET_FRAME_MISMATCH` | 竖屏 i2v/flf2v 的参考图不是 1080×1920 |
| `TIMELINE_FRAME_MISMATCH` | 时间线 clip 的冻结画幅与当前渲染画幅不一致 |
| `SUBTITLE_STYLE_FRAME_MISMATCH` | 字幕渲染档与画幅不匹配 |

复用：`ROUTE_UNAVAILABLE`（竖屏价格未核实）、`VIDEO_UNSUPPORTED_MODE`/`VIDEO_UNSUPPORTED_SPEC`（供应商无该竖屏组合）、`LOUDNESS_OUT_OF_RANGE`（静音/超差）、`RENDER_SOURCE_MISSING`（时间线音频全丢）、`RESUME_CONFIG_CHANGED`（画幅切换后旧 Run 不可恢复）。

## 8. 确定性与基线保护

- E0 基线 `docs/M9_E0_BASELINE.json` 的 `contracts`/`tools`/`capabilities`/`routeProbe`/`prices`/`limits` 是 E1–E4 的比对基准；`frames.horizontal` 是「16:9 不许变」的清单，`frames.verticalTargets` 是「竖屏要变成什么」的清单。
- 重跑 `scripts/capture_m9_baseline.py` 后文件 SHA-256 必须仍是 `afd1b38f…59b694b`；E1 起允许变化的只有 `contracts`（新增 v7 模型）与 `capabilities`（新增 9:16 表），且必须在 E1 验收里逐项列出。
- 所有新逻辑必须有固定输入→固定输出：字幕 ASS 文本、`exportProfile` JSON、`RenderEngine.configurationSha256`、成片 SHA 都要可复现；16:9 的既有固定 SHA 必须不变。
- 受控 HTTP 只连 `httpx.MockTransport`；真实样本按 §9 单独授权。

## 9. 工作包与验收门禁

| 包 | 门禁 |
|---|---|
| E1 | 画幅矩阵（wan/kling/fal × t2v/i2v/flf2v × 16:9/9:16）、竖屏缺参考图拒绝、未核实价格拒绝、v6→v7 并存与恢复、集级额度不被绕过；**16:9 的 `capabilitiesSha256` 与 `routeProbe.rowsSha256` 不变** |
| E2 | 1080×1920 出片（时长 ≤1 帧）、字幕 v2 零漂移与安全区、标题卡固定 SHA、16:9 固定 SHA 不变、混画幅拒绝、单场失败隔离 |
| E3 | 受控浏览器：设置页切 9:16 → 报价对比 → 登记 → 渲染 → 播放/Range 206 → 字幕开关，零页面错误 |
| E4 | 离线套件 + 安装态 wheel smoke + 全量回归（两次：`-x` 与完整）+ Ruff + 四组 CI + v0.7.0 发布说明；真实视频 submit=0、模型调用=0、TTS=0 |

## 10. 明确不做

- 720×1280 竖屏、1:1/5:4、同一集混画幅、跨集横竖混剪。
- 竖排文字、逐字动画、竖屏专属剪辑工作站、移动端专属页面。
- 汇率换算、跨币种合计、混合供应商分集、镜头级 provider 冻结（schema 10）。
- 后台批处理守护进程、账单自动抓取、PyPI 公开、可执行插件。
- 9:16 的 Kling/fal 真实样本（无 Key 时不做，能力表保持 `documented-no-real-sample`）。
