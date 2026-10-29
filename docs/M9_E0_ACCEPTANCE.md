# M9 E0 验收：竖屏 9:16 契约冻结

状态：**已完成（2026-10-25）；方向暂缓（2026-10-29）**——E0 的证据（基线与哈希对照）仍然有效，但竖屏 E1–E4 不排期，产品维持横屏 720p。基线 `master` `6e45e92`（v0.6.0：schema 9、tools/rules 18/18、168 个契约模型）。本阶段只做**文档 + 基线脚本 + 基线快照**与 §3.0 的前置加固，**不改 schema、不改工具面、不改能力表、不改价格**：视频 submit=0、模型调用=0、TTS 请求=0、凭据读取=0。

契约见 [M9 契约](./M9_CONTRACTS.md)，计划见 [M9 计划](./M9_PLAN.md)，机器可读基线见 `docs/M9_E0_BASELINE.json`。

## 1. 交付内容

| 产物 | 说明 |
|---|---|
| `docs/M9_CONTRACTS.md` | 冻结 D1–D8：配置级画幅、v7 契约代次、1080×1920 几何、按画幅发布能力表、`subtitle-zh-v2`、`TIMELINE_FRAME_MISMATCH` 等 4 个新错误码、预算不变量、16:9 基线清单 |
| `docs/M9_E0_BASELINE.json` | 49,229 字节，SHA-256 `afd1b38fa42aa8c3275548995b7cb7227feaadff153cfe79cf4e4a906059694b`；含 168 个契约模型哈希、两套工具面、能力表、价格与上限、`routeProbe`、`frames.horizontal`（不许变的 16:9 事实）与 `frames.verticalTargets`（要变成什么） |
| `scripts/capture_m9_baseline.py` | 生成上述基线；复用 `scripts/capture_m8_baseline.py` 的哈希、工具面与路由探针实现（M8 已冻结该脚本，避免第二份排序实现漂移） |
| §3.0 混音加固 | `mixer.py` 静音快速失败 + `render_service.py` 音频落空诊断；新增 4 项用例（见 §3.2） |

## 2. 相对计划的实现决策

1. **画幅按配置而不是按项目字段**：`videoFrame` 不进 schema、不进 `toolFeatures`。这样 `16:9` 下 features 与 M8 逐字节一致，pre-M9 的 live Run 仍能恢复；代价是切到 `9:16` 后旧 live Run 恢复会得到 `RESUME_CONFIG_CHANGED`，已在契约 §2.2 明确记录，不伪造兼容。
2. **能力表按画幅各发布一份**：`16:9` 表 == M8 的 `multi-provider-v3` 逐字节（`capabilitiesSha256` 不变），`9:16` 表是新表。这样 M8 基线的能力哈希继续有效，竖屏增量单独记账。
3. **竖屏只做 1080×1920**：不收 720×1280，避免字幕/标题卡再维护第三套比例。
4. **基线脚本复用 M8 脚本的纯函数**：M8 的 `capture_m8_baseline.py` 与它的 JSON 一同冻结，导入它是「一份实现」而不是「一个可能漂移的副本」；E1 若需要独立实现必须先在契约里说明。
5. **§3.0 加固放在 E0 之前**：它属于 M8 遗留的稳定性问题，不是竖屏功能；因此它先落地并单独验收，基线快照记录的是加固后的树。

## 3. 验证结果

### 3.1 基线复现与 M8 哈希对照

重跑 `scripts/capture_m9_baseline.py` 后 `docs/M9_E0_BASELINE.json` 的 SHA-256 仍是 `afd1b38f…59b694b`（两次运行字节一致）。与 `docs/M8_E0_BASELINE.json` 对照，全部**逐字节相同**：

| 项 | M8 | M9 | 结论 |
|---|---|---|---|
| `contracts.contractsSha256` | `ba9d8d7b…2e27` | `ba9d8d7b…2e27` | 不变 |
| 契约模型数 | 168 | 168 | 不变 |
| `capabilities.capabilitiesSha256` | `da6aa7b7…d1bb` | `da6aa7b7…d1bb` | 不变（仍是 16:9 的 4 条） |
| live `specsSha256` / `instructionsSha256` | `1efcb3f9…` / `e81a2ef7…` | 同左 | 工具面 18/18 不变 |
| mock `specsSha256` / `instructionsSha256` | `1e222b21…` / `9e66254b…` | 同左 | 不变 |
| `routeProbe.rowsSha256` | `3817d191…6413` | `3817d191…6413` | 不变 |
| `limits` / `prices` | v0.6.0 值 | 同左 | 不变 |
| `releaseVersion` | `0.5.0`（冻结时） | `0.6.0`（冻结时） | 各记自己的发布 |

`frames.horizontal` 记录的是：`ShotSpecV3` 的 t2v 只有 `16:9`、`VideoRequestV6` 的 `ratio` 只有 `16:9`、两个导出 profile 的宽高与 `upscaled`、归一化 profile、`media.py:295` 的 1280×720 校验、`subtitle-zh-v1` 的 24 字符/行与 `ASS_STYLE`；`frames.verticalTargets` 记录 D1–D8 的冻结值（含 `SILENCE_PEAK_DB = -90.0` 与三个 `dropped` 原因）。

### 3.2 §3.0 前置加固：混音静音的确定性失败

**问题**：2026-10-25 首次全量回归时 `tests/test_m7_timeline_render.py::test_a_full_timeline_render_freezes_the_edit_and_replays` 失败，`RenderError` 携带 `integratedLufs=-70.0`、`truePeakDb=-1.1`、`lra=0.0`，即交付文件几乎全静音却带一个峰值——它在 `mix_tracks` 的四遍（测量、应用、复测、重试/校正）里被放大成 `LOUDNESS_OUT_OF_RANGE`。该用例单独重跑通过，第二次全量也通过，因此是顺序/时序相关而非确定性回归。

**改动**（两处，均为「让失败说清原因」，不放宽任何门禁）：

| 文件 | 改动 |
|---|---|
| `src/vagent/video/mixer.py` | 新增 `SILENCE_PEAK_DB = -90.0`、`_SILENCE_PEAK` 与 `parse_volume_peak()`；在 loudnorm 之前跑一次 `volumedetect` 探测 premix，`max_volume ≤ -90 dBFS` 时立即返回 `LOUDNESS_OUT_OF_RANGE`「（混音为静音，未交付）」，不再空跑四遍编码；`MixError` 增加 `.detail`，避免调用方包装时重复冻结文案（见 §6.5） |
| `src/vagent/video/render_service.py` | `AudioPlan` 增加 `dropped` 诊断（原因 `no-dialogue-line`/`no-cue`/`outside-edit`）；`_timeline_cue_start` 对「落在 clip 末尾或之后」的落点返回 `None`（按 M7 契约「不足 1 ms 的 cue 丢弃」，不再夹取成 0 长度点击）；时间线声明了音频但全部落空时返回 `RENDER_SOURCE_MISSING` 并给出条数与原因；混音失败时把 `dropped` 摘要拼进 `RenderError` 详情 |

**新增用例**（4 项）：`test_digital_silence_is_refused_before_the_loudnorm_passes`、`test_a_very_quiet_but_audible_premix_still_reaches_the_target`（证明探测阈值只挡数字静音，−60 dB 的真实信号仍被归一化到 −16 LUFS）、`test_volume_peak_parser_reads_the_silence_floor`、`test_a_timeline_whose_only_audio_falls_outside_the_edit_names_the_cause`。

**结果**：`tests/test_m6_audio_mix.py` 11 passed（2.85 s）、`tests/test_m7_timeline_render.py` 6 passed（76.65 s）；M6/M7 渲染相关 8 个文件 46 passed（292.16 s）。原有用例 `test_silent_mix_is_rejected_by_the_loudness_gate` 仍然通过（拒绝语义未变）。

**2026-10-29 复现与二次加固**：同一条命令（`tests/test_m6_audio_mix.py` + `tests/test_m7_timeline_render.py`）在 7 次本地运行中失败 1 次，报错已从「−70 LUFS 超差」变成**「混音为静音，未交付」**，说明 voice 轨在该次运行被丢弃、只剩数字静音 BGM。随后同一命令连续 5 次全通过（17 passed，59.7–65.1 s）。据此把 `dropped` 摘要拼进 `RenderError` 详情（下次复现即可看到是 `outside-edit` 还是 `no-cue`），并修掉「（…（…））」的双重包装。**该偶发仍未根除**，判定为「约 1/7 次的本地顺序相关 flake」，E4 门禁保留两次全量。

### 3.3 定向回归与静态检查

```text
.\.venv\Scripts\python.exe -m pytest -q tests/test_m6_audio_mix.py tests/test_m6_audio_acceptance.py \
  tests/test_m6_subtitle_service.py tests/test_m6_render_service.py tests/test_m6_render_episode.py \
  tests/test_m6_render_acceptance.py tests/test_m7_timeline_render.py tests/test_m7_timelines.py
46 passed in 292.16s

.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
All checks passed!
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
232 files already formatted
```

### 3.4 全量回归

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1520 passed, 4 skipped in 1460.88s (0:24:20)
```

- 相对 M8 E4 的 `1516 passed / 4 skipped`（1481.17 s）**多 4 项**，正好是 §3.2 的新增用例；skip 仍是既有的 Windows 符号链接/权限用例，未新增。
- 耗时 +45 s（约 3%）：静音探测给每次混音多跑一遍 `volumedetect`（`-f null`，不编码）。这是用一点本地时间换「静音混音确定性失败」的代价，已在契约 §5 记为冻结行为。
- 这是加固后的**第一次**全量；E4 门禁要求**两次**（`-x` 与完整）都通过（见 §6.1）。

## 4. 费用与「零新增」边界

| 项 | 数量 | 说明 |
|---|---|---|
| 真实视频提交 | **0** | 基线脚本只读本地表，不构造上游请求 |
| 文本模型调用 | **0** | E0 无模型调用 |
| TTS 真实请求 | **0** | 未触碰 TTS |
| 供应商请求 | **0** | `published_surface()` 只重建注册表与能力表 |
| 凭据读取 | **0** | 不读任何 Key，`zeroSpend.credentialsRead=false` |
| 新登记 Job | **0** | 不写 `state.json`（脚本只在 `tmp/` 里建临时 Store） |

## 5. 明确未做与边界

1. **未改任何产品契约**：`ShotSpecV4`/`VideoRequestV7`/`JobV7`/`multi-provider-v4`/`subtitle-zh-v2`/`1080p-vertical-h264-aac-v1` 都只写在合约里，代码仍只有 v3/v6/`multi-provider-v3`/`subtitle-zh-v1`。
2. **未核实竖屏价格**：Wan 1080P、Kling 1080p/9:16、fal 9:16 的官方单价需要 E1 开工时按 §7 清单核实；未核实前 `9:16` 表为空，任何竖屏登记都会被 `ROUTE_UNAVAILABLE` 拒绝。
3. **未做竖屏渲染**：`profile_for` 仍只有横屏两支，`1080p-vertical-h264-aac-v1` 是目标值不是现值。
4. **未改 schema**：仍 9，未新增集合；`seasons`/`batches`/`timelines` 与 M7 一致。
5. **未改工具面**：仍 18/18、32 个工具、`instructionsSha256` 不变；`9:16` 不作为工具参数暴露。

## 6. 失败与偏差记录

1. **顺序相关失败（已加固，未根除；2026-10-29 复现 1 次）**：`test_m7_timeline_render.py` 的时间线渲染用例首轮全量失败（`1 failed, 808 passed, 2 skipped`），错误是 `LOUDNESS_OUT_OF_RANGE` 且测得 −70 LUFS / −1.1 dBTP；单跑该用例、该文件、第二次全量（1516 passed / 4 skipped）都通过。加固后全量 **1520 passed / 4 skipped** 通过；2026-10-29 同一条两文件命令在 7 次里失败 1 次（`1 failed, 16 passed`），报错已变成「混音为静音，未交付」，随后连续 5 次通过。加固把这条路径改成确定性且带原因（§3.2），但**触发条件仍未复现**，因此记为「约 1/7 次的本地顺序相关 flake，已加诊断与快速失败」，不声称已根除；E4 门禁要求两次全量（`-x` 与完整）都通过。
2. **`ConvertFrom-Json` 误报**：PowerShell 用默认编码读 `docs/M9_E0_BASELINE.json` 时中文被按 GBK 解码，报告「Invalid object passed in」。改用 Python `json.loads` 校验：文件 49,229 字节、合法 UTF-8、SHA-256 稳定。这是核验方式的缺陷，不是文件缺陷。
3. **`capture_m9_baseline.py` 首次 ruff 不通过**：未使用的 `canonical_sha256` 导入与一处 `publishedSpec` 单行超长；修掉后 `ruff check`/`format` 全过（232 文件）。
4. **`media.py` 没有宽高常量**：脚本原打算读 `MEDIA_WIDTH/HEIGHT`，实际它在 `media.py:295` 内联比较；改为在基线里记录 `{"width": 1280, "height": 720, "checkedIn": "src/vagent/video/media.py:295"}`。
5. **错误文案双重包装**：`render_service` 原先用 `f"（{error}）"` 包装 `MixError`，而 `MixError` 的文案已含冻结说明，复现时出现「（混音响度或 true peak 超出容差，未交付。（混音为静音，未交付））」。改为 `MixError.detail`（不含冻结文案）后，详情只剩真实原因，并把音频落空摘要一并拼上。

## 7. E1 开工前的官方核实清单（只查文档与公开页面，不发请求）

1. Wan `wan2.7-t2v` 的 `parameters.ratio` 是否接受 `9:16`，以及 1080P 竖屏的单价与价格页 URL（现价 0.60 CNY/秒是 720P）。
2. Kling `kling-3.0-turbo` 的 1080p/9:16 单价与 `VIDEO_UNIT` 换算（`0.112 USD/s`、`0.8 unit/s` 只适用 720p）。
3. fal `kling-video/v3/standard/text-to-video` 是否支持 9:16、是否仍为静音输出、单价是否变化。
4. Wan i2v/flf2v 对首帧/尾帧的边长与比例约束（决定 `CHARACTER_ASSET_FRAME_MISMATCH` 的判定阈值与错误文案）。

任一项未核实：该供应商的 `9:16` 条目不发布，`ROUTE_UNAVAILABLE`；不影响 `16:9` 与其它供应商。

## 8. 复现说明

1. 基线：`.\.venv\Scripts\python.exe scripts/capture_m9_baseline.py`，然后核对 `docs/M9_E0_BASELINE.json` 的 SHA-256 仍为 `afd1b38f…59b694b`。
2. 哈希对照：读 `docs/M8_E0_BASELINE.json`，逐项比较 §3.1 的 8 个值。
3. 加固用例：`.\.venv\Scripts\python.exe -m pytest -q tests/test_m6_audio_mix.py tests/test_m7_timeline_render.py`。
4. 全量：`.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`（本机约 23–25 分钟）。
