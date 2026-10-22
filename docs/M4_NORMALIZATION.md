# M4 时间线归一化 / 转码：设计与验收

状态：**已实现，待发布**（2026-10-22，`master` 开发版）。范围：把 M3 的“只允许参数一致的流复制拼接”扩展为 **流复制优先、自动回退转码**，让来自 i2v/flf2v、不同帧率/分辨率、有音轨/无音轨的镜头可以合成一条成片。本文记录冻结参数、实现位置、验证证据与边界。

## 1. 决策

| 项 | 结论 |
|---|---|
| 目标画幅 | **16:9，1280×720**（与当前万相/Kling/fal 冻结规格一致；9:16 竖屏不在本次范围） |
| 兼容策略 | **自动回退**：先尝试无损流复制；`mp4-copy-signature-v1` 拒绝时改为统一转码 |
| 视频 | H.264 High、yuv420p、**30fps**、SAR 1:1、CRF 20、preset veryfast |
| 音频 | 保留原声；无音轨补静音；统一 **AAC 48kHz 立体声、192kbps** |
| 时间线 | 每个输入按目标帧数裁剪；视频/音频都从 0 开始，再 `concat` |
| 转码引擎 | `ffmpeg-concat-normalize-v4`；profile `h264-1280x720-30fps-aac-48k-stereo-v1` |
| 记录 | `FilmAssembly.assembly_version` + 可选 `normalization` 元数据；`FilmVersion` 仍只增不改 |
| 旧成片 | `assembly_version` 默认 `ffmpeg-concat-copy-v1`、`normalization=null`；旧 v1–v3 normalize 记录仍可读 |

## 2. 实现

- `src/vagent/video/assembler.py`
  - `plan_normalization()`：接受时间线、帧率、分辨率、音频存在性差异，按目标 30fps 计算每个输入的帧数与时长。
  - `assemble_normalized()`：单次 FFmpeg `filter_complex`，每个输入先 `scale/pad/fps/trim/setpts`，音频 `aresample/aformat/apad/atrim` 或 `anullsrc`，最后 `concat=n=N:v=1:a=1` 一次性编码输出。
  - 输出再次经 `inspect_mp4` 与 `read_copy_signature` 校验：1280×720、30fps、H.264+AAC、有时长容差；提交前按实际视频时长校准时间线。
  - 流复制路径 `assemble()` 与其冻结命令保持不变。
- `src/vagent/video/film_service.py`
  - 先 `plan_assembly()`；仅当返回 `FILM_INPUT_INCOMPATIBLE` 时调用 `plan_normalization()` 并走 `assemble_normalized()`。
  - 指纹包含 `assemblyVersion` 与 `normalization`；纯 copy 成片的指纹与旧版逐字节一致，保证历史成片重放。
  - 仍在 Store 锁外转码，锁内复核分镜 revision、输入哈希与产物哈希后一次提交；提交使用实际编码后的时间线。
- `src/vagent/video/films.py`
  - 新增 `FilmNormalization` 与 `FilmAssembly.normalization`（可选）；校验 normalization 与 assembly version 一致。
  - `film_view` 暴露 `assemblyVersion` 与 `normalization`，CLI/Web 可显示实际使用的引擎。

## 3. 验证

- 新增/更新测试：`tests/test_m3_assembler.py`（normalize plan、无音轨补静音、copy 仍拒绝不兼容输入、30fps/时长校验、缩放/重采样）、`tests/test_m3_film_service.py`（copy 失败自动回退、提交 normalization 版本）。
- 电影相关套件：`tests/test_m3_assembler.py`、`test_m3_film_service.py`、`test_m3_film_api.py`、`test_m3_film_cli.py` 共 37 项通过。
- 完整 Python 3.12 回归：**1122 passed / 3 skipped**（453.83 秒）。
- **真实混合样本**：用 2026-10-22 的万相 i2v 5 秒与 flf2v 5 秒真实 MP4（见 [i2v/flf2v 验收](./M2_I2V_ACCEPTANCE.md)）执行 `vagent film assemble`：
  - 旧 copy-only 路径会返回 `FILM_INPUT_INCOMPATIBLE`（时间线相差超过一帧）；新路径自动回退转码。
  - 成片：`filmId=c1964628-3404-4039-a2fe-7fa9863a7298`，`outputMediaId=08bc25d9-9d78-4858-86d7-4ca3ffab2767`，**5,968,116 字节 / 10.000000 秒 / 1280×720 / H.264+AAC / 30fps**，SHA-256 `46a586ea6fb7217197b53c6e6cb133b9b36c0abdeffd0acd20f292bdd3f719af`。
  - 成片版本记录 `assemblyVersion=ffmpeg-concat-normalize-v4`、`profile=h264-1280x720-30fps-aac-48k-stereo-v1`；时间线为 5.0 + 5.0 秒。
  - 证据文件：`output/wan-modes-20261022/i2v-flf2v-film-10s.mp4`、`film-10s-thumb.png`。

## 4. 边界与风险

- **不是 9:16 竖屏**：本次只做 16:9 720P；竖屏短剧需要新的目标 profile 与裁切/补边策略，不能把 16:9 直接裁成 9:16 当作默认行为。
- **只做拼接，不做转场/字幕/配乐/导出参数定制**；这些仍属后续能力。
- **转码有损且更慢**：copy 路径仍优先；只有不兼容时才转码。CRF 20/veryfast 是当前默认，后续可按 profile 调整。
- 输入仍要求可解析的 AVC/AAC MP4；非 AVC、HDR、旋转元数据、可变帧率的复杂结构未全面验证。
- 无音轨片段会补静音；原声片段的音量/响度不做归一化（响度标准化另做）。
- 30fps 转换会在接缝处按帧对齐，实际时长与源素材可能有 1 帧级差异；成片版本以实际编码时间线为准。
- 实际耗时取决于镜头数量与 CPU；大合集尚未做压力测试。

## 5. 后续

1. 9:16 竖屏 profile（720×1280/1080×1920）与裁切/补边策略，配合供应商竖屏生成能力。
2. 转场、字幕、配音/配乐、响度归一化。
3. 按镜头/按集批量拼接与失败重试。
4. 发布版本与 tag（当前功能在 `master`，尚未打 tag）。

## 6. 证据索引

- [i2v/flf2v 真实样本](./M2_I2V_ACCEPTANCE.md)
- [M3 契约](./M3_CONTRACTS.md)（M3 冻结的 copy-only 语义，本文件为其 M4 扩展）
- `output/wan-modes-20261022/evidence.json`、`i2v-flf2v-film-10s.mp4`
