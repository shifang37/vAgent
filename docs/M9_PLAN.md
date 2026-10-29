# M9 计划：竖屏 9:16 短剧

状态：**暂缓（2026-10-29）**——产品方明确本轮「只用横屏 720p」，竖屏 E1–E4 不排期；E0 冻结结果与基线保留，将来要做竖屏时从 E1 续起（先按其 §7 核实价格）。**E0 契约已冻结（2026-10-25），E1 未开始、未改产品代码**。冻结结果见 [M9 契约](./M9_CONTRACTS.md) 与 [E0 验收](./M9_E0_ACCEPTANCE.md)，机器可读基线见 `docs/M9_E0_BASELINE.json`（SHA-256 `afd1b38f…59b694b`）。基线 `master` `6e45e92`（**v0.6.0** 发布后；schema **9**、tools/rules **18/18**、168 个契约模型、Ruff 232 文件、前端 10 模块 91 项、四组 CI [运行 40171887390](https://github.com/shifang37/vAgent/actions/runs/40171887390) success）。⚠️ 复跑期间出现过 **1 次顺序相关的偶发失败**（`test_m7_timeline_render.py` 的整集时间线渲染用例），已加固为确定性失败并单列，见 **§3.0**。本文件是**计划，不是契约**：数字以 [M9 契约](./M9_CONTRACTS.md) 为准。上一阶段收尾见 [M8 验收](./M8_ACCEPTANCE.md) 与 [v0.6.0 发布说明](./RELEASE_0.6.0.md)。

## 1. 为什么是 M9

M1–M8 已经把「一句话/剧本 → 分集分场分镜 → 真实多镜头 → 字幕/配音/混音/转场/整集渲染 → 季/批量/时间线 → 供应商路由与账单」跑通，并把 16:9 这一条画幅从登记冻结到了成片。**唯一被连续三次推迟、且直接决定产品形态的缺口是竖屏**：

| 差距 | 现状证据 | 影响 |
|---|---|---|
| 只有 16:9 | `ShotSpecV3` 的 t2v 只能 `aspect_ratio="16:9"`（`src/vagent/video/contracts.py:583`、`:906`）；`VideoRequestV6` 的 `ratio` 只有 `"16:9"`（`:589`、`:757`）；能力条目断言只发布 `5 s / 720p / 16:9`（`:1790`） | 短剧的主投放画幅是 9:16；当前成片无法直接投竖屏平台 |
| 渲染只有横屏几何 | 归一化 profile 固定 `h264-1280x720-30fps-aac-48k-stereo-v1`（`src/vagent/video/assembler.py:44`）；`ExportProfile` 只认 `(1280,720,False)` 与 `(1920,1080,True)`（`src/vagent/video/films.py:152`） | 720P/1080P 都是横屏；竖屏要么裁剪要么留黑边 |
| 字幕/标题卡按横屏排 | `subtitle-zh-v1` 每行 ≤24 字符、`ASS_STYLE` 的边距按 1280×720 反推（`src/vagent/video/subtitles.py:31`、`:51`、`:900`） | 1080 宽的竖屏放 24 字会挤成两行并越出安全区 |
| 三次明确延后 | [M5 计划 §9](./M5_PLAN.md)「9:16 竖屏与竖屏供应商能力（单列后续）」；[M8 计划 §7](./M8_PLAN.md)；[M8 验收 §5.5](./M8_ACCEPTANCE.md) | 每一轮都以「本轮不做」收尾，欠账持续累积 |
| 面不一致 | `src/vagent/mcp_server.py:40` 的时长/帧数计算已接受 `9:16`，但 live 管线不接受 | MCP 面能算、登记面会拒，语义自相矛盾 |
| 供应商侧的竖屏依据已在手 | Wan t2v 文档支持五种比例、目前只开放 16:9（[官方核实](./VIDEO_API_NOTES.md) 第 34 行）；Kling 官方范围含 `16:9/9:16/1:1`（第 303 行）；Wan i2v/flf2v **输出画幅由输入图决定、不设 `ratio`**（第 211 行） | 竖屏不是「要不要做」的问题，而是「按哪条路径冻结」的问题 |

结论：**M9 只做「画幅」这一条纵切**——不新增 schema 集合、不新增模型工具、不改 16:9 的既有语义与已冻结历史对象；把 9:16 从配置、登记、能力表、渲染、后期一直贯通到 Web 与发布。

## 2. 里程碑总览

| 包 | 内容 | 估时 | 花费门禁 |
|---|---|---|---|
| E0 | 契约冻结（画幅来源、契约代次、几何、字幕/标题卡、价格核实口径） | 0.5–1 天 | submit=0、模型调用=0、TTS=0、凭据读取=0 |
| E1 | 画幅、能力表与登记（配置、契约 v7、`multi-provider-v4`、路由与预算） | 2–3 天 | submit=0（受控 HTTP） |
| E2 | 竖屏渲染与后期（1080×1920 profile、归一化、字幕 v2、标题卡、成片校验） | 2–3 天 | submit=0（本地 FFmpeg） |
| E3 | Web 竖屏工作台（9:16 预览、设置页画幅、竖屏时间线/字幕） | 1.5–2 天 | 受控浏览器 |
| E4 | 验收与 **v0.7.0**（离线套件、安装态、全量回归、四组 CI、发布说明） | 2 天 | 未新增付费样本 |

顺序：**E0 → E1 → E2（可与 E3 前半并行）→ E3 → E4**。E1 与 E2 的分界是「能不能登记」与「能不能出片」：E1 结束时竖屏 Job 可被冻结、路由可算价、但成片仍走 16:9；E2 结束时 1080×1920 能出片。

## 3. 工作包

### 3.0 E0 前置：全量回归基线复核与 flake 加固（P0，先于 E0，不计入竖屏范围）——**已加固（2026-10-25）**

2026-10-25 在本机复跑 `pytest -q -x` 时，`tests/test_m7_timeline_render.py::test_a_full_timeline_render_freezes_the_edit_and_replays` 在全量顺序下失败（`1 failed, 808 passed, 2 skipped`），错误是 `RenderError`：混音测得 `integratedLufs=-70.0`、`truePeakDb=-1.1`，超出容差返回 `LOUDNESS_OUT_OF_RANGE`。随后**单独重跑该用例 1 passed、整个文件 5 passed**，再一次全量 `pytest -q -p no:cacheprovider` 得到 **1516 passed / 4 skipped**，与 M8 验收记录一致。结论：**这不是确定性回归，而是顺序/时序相关的偶发失败**；v0.6.0 的「全量通过」仍然成立，按 flake 记录，不作为回滚依据。

加固（已落地，证据见 [E0 验收 §3.2](./M9_E0_ACCEPTANCE.md)）：`mixer.py` 在 loudnorm 之前用 `volumedetect` 探测 premix，`max_volume ≤ -90 dBFS` 时立即返回 `LOUDNESS_OUT_OF_RANGE`「（混音为静音，未交付）」，不再空跑测量/应用/复测/校正四遍；`render_service.py` 给 `AudioPlan` 增加 `dropped` 诊断（`no-dialogue-line`/`no-cue`/`outside-edit`）、按 M7 契约丢弃「落在 clip 末尾或之后」的落点、并在时间线音频全落空时返回 `RENDER_SOURCE_MISSING` 并列出条数与原因。新增 4 项用例；首轮那次失败的**触发条件仍未复现**，因此记为「已知偶发 + 已加确定性诊断与快速失败」，E4 门禁保留两次全量（`-x` 与完整）都通过的要求。

### 3.1 E0 契约冻结（0.5–1 天；submit=0、模型调用=0、TTS=0、凭据读取=0）——**已完成（2026-10-25）**

> 冻结值已写入 [M9 契约](./M9_CONTRACTS.md) §1–§7，证据见 [E0 验收](./M9_E0_ACCEPTANCE.md)。下表保留为当时的建议值，冲突时以契约为准。

决策点（建议值，E0 冻结）：

| 编号 | 决策 | 建议 |
|---|---|---|
| D1 | 画幅来源 | **配置级**新增 `videoFrame`（`16:9` 默认 / `9:16`），登记时冻结进每个 Job；**不做**逐镜头混画幅、不做分镜工具新字段——保住工具面 18/18 与旧 prompt 字节不变 |
| D2 | 契约代次 | 新增 `ShotSpecV4`/`VideoRequestV7`/`JobV7` 只承载竖屏；v1–v6 与 `multi-provider-v3` 逐字节保留可读；**schema 仍 9**（不新增集合） |
| D3 | 竖屏几何 | 只做 **1080×1920**（1080P 级，30 fps、H.264、AAC 48 kHz 立体声）；**不做** 720×1280 竖屏、不做 1:1 |
| D4 | 供应商覆盖 | Wan t2v 9:16 与 Kling 9:16 只在**官方价格核实并按 `priceVersion` 升版**后才进能力表；fal 9:16 待核实，未核实不进；i2v/flf2v 竖屏要求参考图 1080×1920，否则 `CHARACTER_ASSET_FRAME_MISMATCH`（**不缩放、不裁切、不降级**） |
| D5 | 字幕与标题卡 | 新增 `subtitle-zh-v2`（竖屏每行 ≤14 字符、底部安全区 ≥12% 高、字号按 1080×1920 重算）；标题卡用竖屏版式；**不做**竖排文字、不做逐字动画 |
| D6 | 渲染与时间线 | 复用 `film_render` 与 `timeline` scope；装配字面量不变，画幅写进 `ExportProfile`/`RenderEngine.configuration_sha256`；时间线内画幅不一致 → `TIMELINE_FRAME_MISMATCH` |
| D7 | 价格与预算 | 竖屏单价必须单独核实（Kling 的 0.112 USD/s 是 720p，1080p 不适用）；未核实即 `ROUTE_UNAVAILABLE`，沿用「不可推荐」语义；集级/批量上限口径不变 |
| D8 | 发布与边界 | 发布 **v0.7.0**；不做混合画幅分集、不做跨集竖横混剪、不做移动端专属页面、不做 PyPI 公开 |

官方复核清单（只查文档与公开页面，不发请求）：Wan t2v 的 `parameters.ratio` 是否接受 `9:16`、竖屏是否仍在同一价格桶；Kling `kling-3.0-turbo` 的 9:16/1080p 单价与 `VIDEO_UNIT` 换算；fal `kling-video/v3/standard` 是否支持 9:16 与静音输出；Wan i2v/flf2v 对首帧/尾帧的边长与比例约束（决定 `CHARACTER_ASSET_FRAME_MISMATCH` 的判定阈值）。

产出：`docs/M9_CONTRACTS.md`、`docs/M9_E0_BASELINE.json`（在 M8 基线上新增画幅相关哈希，**旧 168 个模型哈希与 `routeProbe.rowsSha256` 必须不变**）、`scripts/capture_m9_baseline.py`、`docs/M9_E0_ACCEPTANCE.md`。硬约束：不改产品代码、不改 schema、submit=0。

### 3.2 E1 画幅、能力表与登记（2–3 天；submit=0）

- **配置**：`videoFrame`（`VAGENT_VIDEO_FRAME`，`16:9`/`9:16`，默认 `16:9`）；设置页只读展示当前画幅与影响范围（沿用 `PATCH /api/config` 的显式切换语义，不做静默替换）。
- **契约**：`ShotSpecV4`（t2v 允许 `16:9`/`9:16`；i2v/flf2v 的 `aspectRatio` 仍由参考图决定但必须与 `videoFrame` 一致）、`VideoRequestV7`、`JobV7`；`multi-provider-v4` 能力表把竖屏条目与 `priceVersion` 一起冻结；v6 及更早的 Job 仍按原契约恢复与重放。
- **路由**：`provider-route-v1` 的输入增加画幅，行内给出 `frame` 与对应单价；`reason` 优先级、CNY→USD→金额→wan→kling→fal 排序与「不换汇」保持不变；无竖屏条目的供应商返回 `ROUTE_UNAVAILABLE` 而不是退回 16:9。
- **预算**：竖屏 Job 的单价/数量按新价格版本重算，逐集 20 Job / 60.00 CNY 与批量 3 集 / 60 Job / 180.00 CNY 口径不变；集级校验必须同时接受 v6 与 v7，**v7 不能绕过集级上限**。
- **面**：`vagent route preview --frame`、`GET /api/route/preview?frame=`、CLI/HTTP 的登记面沿用现有 `shot_generate`/`episode_generate`（画幅来自配置，不进工具参数）。
- **测试**：画幅矩阵（wan/kling/fal × t2v/i2v/flf2v × 16:9/9:16）、竖屏缺参考图拒绝、价格未核实拒绝、v6→v7 恢复、集级额度不变量。

### 3.3 E2 竖屏渲染与后期（2–3 天；submit=0）

- **几何**：新增 `ExportProfile` 值 `1080p-vertical-h264-aac-v1`（1080×1920，`upscaled` 语义沿用：源为 1080×1920 才算原生）；归一化 profile 增加 `h264-1080x1920-30fps-aac-48k-stereo-v1`，`films.py` 的 `profile_for` 与 `matches_the_frozen_geometry` 同步。
- **字幕**：`subtitle-zh-v2` 的换行宽度、字号、边距与安全区按竖屏重算；`render_ass`/`render_title_ass` 已按 `width/height` 参数化（`subtitles.py:900`、`:933`），E2 只改样式常量与竖屏判定；SRT 仍 24 字符/行的旁挂口径是否调整由 D5 决定（建议 SRT 保持稳定、ASS 按竖屏重排）。
- **渲染**：`render_service` 按 profile 的 `width/height` 缩放（现有 `scale=...:force_original_aspect_ratio=decrease` 已参数化，`render_service.py:1498`）；逐场归一化、单场失败隔离、`blackdetect`、EBU R128、MP4 结构/时长 ≤1 帧与同指纹幂等全部沿用。
- **一致性校验**：成片结构校验从「1280×720」改为「按 profile 的 width/height」（`assembler.py:603`、`media.py:295` 的硬编码几何要改成 profile 驱动），16:9 输出必须逐字节不变（固定 SHA 回归）。
- **测试**：16:9 既有成片 SHA 不变、竖屏 1080×1920 出片、字幕零漂移、标题卡烧录帧固定 SHA、混合画幅拒绝、单场失败隔离。

### 3.4 E3 Web 竖屏工作台（1.5–2 天；受控浏览器）

- 设置页画幅选择（只写 `PATCH /api/config`）与「当前画幅影响已登记 Job 之后的新任务」提示。
- 9:16 播放器与时间线预览（客户端顺序预览沿用 M7 语义）；竖屏字幕 overlay 的行宽与安全区与烧录一致。
- 分镜/分集/报价对比面板显示画幅与竖屏单价；`route-state.js`、`timeline-state.js`、`episode-state.js`、`render-state.js` 增补画幅断言。
- 受控浏览器用例：切到 9:16 → 报价对比三行 → 登记 → 渲染 → 播放/Range 206 → 字幕开关，零页面/控制台错误。

### 3.5 E4 验收与 v0.7.0（2 天）

- `scripts/evaluate_m9.py` 3–4 用例（竖屏全链路、拒绝矩阵、16:9 回归不变、重放不新增）与 `tests/test_m9_evaluation.py`。
- 门禁：全量回归（E0 之后的基线 `1520 passed / 4 skipped`）、Ruff、前端 10 模块 + 新增画幅用例、受控浏览器、安装态 wheel smoke（M9 段）、四组 CI。
- 发布 **v0.7.0**：`pyproject.toml` 与 `src/vagent/__init__.py` 同步、`docs/RELEASE_0.7.0.md`、`dist/python/` 归档 0.6.0 产物并构建 0.7.0、tag 与推送单独确认。
- 文档：`docs/M9_ACCEPTANCE.md`、README、M1_PLAN、M5_PLAN、AGENT_HARNESS_DESIGN §8.9 之后新增 §8.10、web/README。

## 4. 验收样例（草案）

1. **竖屏全链路**：`videoFrame=9:16` 下同一分镜，路由预览给出画幅与竖屏单价；登记后 Job 冻结 `multi-provider-v4` 与 `ShotSpecV4(9:16)`；渲染输出 1080×1920 / 30 fps / H.264 + AAC，时长误差 ≤1 帧，`ffprobe` 缺失时以 FFmpeg 输出为准。
2. **字幕与标题卡**：竖屏 19 条 cue 每行 ≤14 字符、底部安全区不越界；烧录帧与标题卡帧固定 SHA；`blackdetect` 无黑帧。
3. **16:9 不回归**：同一批 16:9 夹具的成片 SHA、`FilmVersion.render` 与 `RenderEngine.configuration_sha256` 逐字节不变。
4. **拒绝矩阵**：`videoFrame=9:16` + 16:9 参考图 → `CHARACTER_ASSET_FRAME_MISMATCH`；竖屏供应商价格未核实 → `ROUTE_UNAVAILABLE`；时间线内画幅混用 → `TIMELINE_FRAME_MISMATCH`；未确认 → 不登记。
5. **重放不新增**：同一登记请求与同一账单重放，Job 数、渲染版本数、账本记录数均不变。

## 5. 真实样本与费用（需单独授权，默认不执行）

| 样本 | 数量 | 估算 | 说明 |
|---|---|---|---|
| Wan 竖屏 9:16 1080×1920 t2v 5 秒 | 1 | 价格待 E0 核实（16:9 720P 为 3.00 CNY，1080P 另计） | 竖屏真实链路 |
| Kling 9:16 5 秒 | 1 | 待核实（0.112 USD/s 仅适用 720p） | 缺 Key 时不执行，保持 `documented-no-real-sample` |
| 长期欠账：Kling/fal 16:9 各 1 条 | 2 | 0.56 USD + 0.42 USD | M8 E3 因缺凭据未做，见 [E3 验收](./M8_E3_ACCEPTANCE.md) |
| CosyVoice / DeepSeek | 0 | 0 | 复用 M8 E3 证据，本轮不新增 |

合计建议上限：**待 E0 核实竖屏单价后填写**；未获授权时 M9 全程真实视频 submit=0、TTS=0、模型调用=0，与 M6–M8 的离线门禁一致。

## 6. 工时与顺序

| 包 | 估时 | 备注 |
|---|---|---|
| E0 | 0.5–1 天 | 文档 + 基线，submit=0；价格核实决定竖屏供应商能否进能力表 |
| E1 | 2–3 天 | 契约 v7、能力表 v4、路由与预算 |
| E2 | 2–3 天 | 1080×1920 profile、字幕 v2、几何去硬编码 |
| E3 | 1.5–2 天 | Web 竖屏面板与设置页 |
| E4 | 2 天 | 离线套件、门禁、v0.7.0 |
| 合计 | **8–11 天** | 不含真实样本等待与授权 |

## 7. 边界（本轮不做）

- 1:1、5:4 等其它画幅；同一集内横竖混排；跨集「超级成片」。
- 竖屏专属剪辑工作站、动效/关键帧/变速、竖排文字与逐字字幕动画。
- 9:16 的 Kling/fal **真实**样本（无 Key 时不做，能力表继续标注）。
- 后台批处理守护进程、无人值守付费重试、账单自动抓取。
- PDF/网页导入、自动摘要与跨项目偏好记忆（README §数据兼容与限制 已记的 Harness 欠账，另列）。
- PyPI 公开、可执行插件/插件市场。

## 8. 风险

1. **竖屏价格未知导致估算失真**：必须 E0 核实并按 `priceVersion` 升版；未核实不进能力表，宁可 `ROUTE_UNAVAILABLE` 也不报价。
2. **i2v/flf2v 画幅由输入图决定**：旧 16:9 角色素材不能直接复用到竖屏；必须明确拒绝而不是缩放裁剪，否则角色一致性与画幅两套语义会互相污染。
3. **几何硬编码分散**：`assembler.py`、`films.py`、`media.py`、`render_service.py`、`subtitles.py` 都有 1280/720 假设；E2 必须改成 profile 驱动，并用 16:9 固定 SHA 回归证明没有回归。
4. **契约双代次**：v6/v7 并存会让预算与集级额度校验出现两条路径；必须逐条对照 M5/M7 的集级语义，并用回归覆盖「v7 不能绕过 20 Job / 60.00 CNY」。
5. **工具面不变是硬约束**：为画幅新增工具或 prompt 字段会同时改动 wheel smoke 的上下文预算与 18/18 工具哈希，E3 前的任何工具面变化都必须回到 E0 重新冻结。
6. **回归时长**：全量约 25 分钟、四组 CI；发布期 runner 中断按历史先例重跑，不视为失败。
7. **全量回归有顺序相关 flake**（见 §3.0）：`test_m7_timeline_render.py` 的数字静音夹具踩在「近静音 loudnorm 退回动态模式」边界，首轮全量失败一次、单独重跑与后续全量均通过。已加固为确定性失败（静音快速失败 + 音频落空诊断），但触发条件未复现，E4 仍需两次全量都通过。

## 9. 新对话启动方式

> 基于 `master` `6e45e92`（v0.6.0 发布后：schema 9、tools/rules 18/18、168 个契约模型；E0 加固后全量 `1520 passed / 4 skipped`）。M1–M8 已全部交付并发布，**M9 E0 契约已冻结（2026-10-25）**，E1 尚未开始。开工顺序：先按 [M9 契约 §7 的官方核实清单](./M9_CONTRACTS.md) 核实竖屏单价，再写 `ShotSpecV4`/`VideoRequestV7`/`JobV7` 与 `multi-provider-v4` 的 `9:16` 表，最后接路由与预算。比对基准是 `docs/M9_E0_BASELINE.json`（SHA-256 `afd1b38f…59b694b`）：**M8 的 168 个模型哈希、`contractsSha256`、`capabilitiesSha256`、工具/规则哈希与 `routeProbe.rowsSha256` 必须逐字节不变**，竖屏新增项单独记账。真实付费样本（§5）单独授权，不得在离线包内默认执行。

