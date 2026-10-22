# M6 E0 验收：成片后期契约冻结

日期：**2026-10-22**。基线 `master` `b8df786`（**v0.4.0**：schema 6、tools/rules 9/9、冻结 8/8…1/1）。范围见 [M5 计划](./M5_PLAN.md) §4 的 M6，冻结契约见 [M6 契约](./M6_CONTRACTS.md)。**E0 不改变产品代码（`src/`、`web/`、`tests/`）、不注册工具、不调用模型或供应商：视频 submit=0、文本模型调用=0、TTS 请求=0。**

E0 的产出是「契约 + 基线 + 可行性证据」，实现从 E1 开始。 **E1 已按 §5 的入口实施**（schema 7、`subtitle-align-zh-v1`、`subtitle_save`、SRT/ASS、软字幕/旁挂/烧录、包内 OFL 字体、tools/rules 10/10），证据见 [E1 验收](./M6_E1_ACCEPTANCE.md)；E0 基线 `docs/M6_E0_BASELINE.json` 保持冻结，未被改写。

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 契约冻结 | `docs/M6_CONTRACTS.md` | 7 个决策点、schema 7 方向、`AudioAsset`/`SubtitleTrack`/`FilmRender` 三个域对象、`subtitle-align-zh-v1` 与 `mix-zh-v1` 算法、工具面 9/9→13/13、预算、18 个错误码、确定性与基线保护 |
| 基线快照 | `docs/M6_E0_BASELINE.json` | 126 个契约模型的 schema 哈希（`vagent.storage` 23、`video.contracts` 74、`video.films` 9、`video.episodes` 13、`video.storyboards` 7）、冻结上限、工具名分组、包内 FFmpeg 事实与本地探针 |
| 基线脚本 | `scripts/capture_m6_baseline.py` | 可复现地重建上述快照；不联网、不登记 Job、不调用模型，只对合成输入跑本地 FFmpeg |
| 验收文档 | 本文件 | 决策、证据、边界与 E1 入口 |

## 2. 已确认的决策

| 决策点 | 结论 |
|---|---|
| TTS 供应商 | 阿里云百炼 CosyVoice，复用 `VAGENT_DASHSCOPE_KEY`；端点/音色/计价在 E2 前按官方文档核实 |
| 音乐/音效 | 仅 CC0/公有领域与用户导入，逐项记录来源与许可 |
| 字幕形态 | 默认软字幕（`mov_text` + 旁挂 `.srt`），烧录为可选导出档 |
| 分辨率 | 验收 720P；1080P 仅参数化上采样 profile，标 `upscaled=true` |
| 水印 | 接受 Wan「AI 生成」水印，记录进 `render.watermark`，不裁切 |
| 中文字体 | 包内附 OFL 子集（Noto Sans SC），`fontRef` 记录 SHA-256 |
| 整集成片 | 纳入 E3：跨场整集导出，含跨场转场与连续字幕时间轴 |

## 3. 验证结果

### 3.1 产品代码未变

```text
git status --short        # 仅 docs/M6_CONTRACTS.md、docs/M6_E0_BASELINE.json、scripts/capture_m6_baseline.py
git diff --stat src web tests   # 空
```

`ruff check src/vagent tests scripts` 全部通过；`ruff format --check` 165 个文件已格式化（较 v0.4.0 的 164 个新增基线脚本）。v0.4.0 的全量回归 **1233 passed / 3 skipped** 继续有效，E0 未触碰被测代码。

### 3.2 基线快照

| 项目 | 值 |
|---|---|
| 契约模型数 | **126**（`vagent.storage` 23、`video.contracts` 74、`video.films` 9、`video.episodes` 13、`video.storyboards` 7） |
| `Database`（schema 6） | `188d2ea12bdbc695e5723207c5abed5ef5bf4c24f15b820e12d3aaa81979105c` |
| `FilmVersion` | `dbcb0e1b8e2c84e4812ef4fb560233dd39233bab652dbfcd2c8b905b4a214455` |
| `Episode` | `115f9efb3e7f8906bf75dabb793a663e87c3cc31e4a167130ceaf76246129b00` |
| 成片上限 | 输入 64、版本 64、单次拼接超时 120 秒、`mp4-copy-signature-v1`、归一化 profile `h264-1280x720-30fps-aac-48k-stereo-v1` |
| 集级上限 | 60–90 秒、10–20 镜头、每场 ≤ 6 镜头、12 场、20 Job、60.00 CNY、镜头 2–15 秒、tools 版本 9 |
| 等待 | 集合成员 20、超时 600 秒 |

E1 只允许三类变化（新增 v7 模型、既有模型追加可选字段、media 新增 `kind=audio`），其余条目必须逐字节不变；差异要在 E1 验收中逐项列出。

### 3.3 本地工具链事实

包内 `imageio-ffmpeg==0.6.0` 提供 **FFmpeg 7.1-essentials**（gyan.dev 构建）：

| 项目 | 值 |
|---|---|
| 版本行 | `ffmpeg version 7.1-essentials_build-www.gyan.dev` |
| 二进制 SHA-256 | `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3` |
| 编译配置 SHA-256 | `df7f1233a38e9bb70a77ab7d4b38e27c4eaea073ac04147471810442a99ff44c` |
| `ffprobe` | **不存在**（所有测量走 FFmpeg 输出 + 现有 `mp4.py` 解析） |
| 滤镜 | `subtitles`、`ass`、`xfade`、`loudnorm`、`afade`、`drawtext`、`amix`、`sidechaincompress`、`aresample`、`scale`、`fps` **全部具备** |
| 编码器 | `mov_text`、`ass`、`subrip`、`aac`、`libx264` **全部具备** |
| 封装器 | `srt`、`ass`、`mp4`、`matroska` **全部具备** |

结论：M6 的字幕、转场、响度、混音与导出**不需要新增二进制依赖**，仅需包内字体（E1 引入）。

### 3.4 本地探针（合成输入）

| 探针 | 结果 |
|---|---|
| 中文烧录 | 成功；输出 2,779 字节，抽帧 SHA-256 `9a7c03bc8bcaaf67dea0184be8fbb00dad8ab5c7210b53f9202fe49bd637659f`（画面实测渲染出「冰箱上的便条」） |
| 软字幕封装 | 成功；`-c:s mov_text` 输出 2,978 字节 |
| 响度测量 | 成功；1 秒 440 Hz 正弦测得 `input_i=-21.75`、`input_tp=-18.06`、`input_lra=0.00`、`input_thresh=-31.75` |
| 系统中文字体 | `C:\Windows\Fonts\msyh.ttc` 存在（仅作本地探针，验收路径改用包内字体） |

探针全程本地：视频 submit=0、TTS 请求=0、模型调用=0。

### 3.5 复现

```powershell
.\.venv\Scripts\python.exe scripts/capture_m6_baseline.py
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
```

## 4. 明确未做与边界

- **schema 7 未实现**：`audio`/`subtitleTracks` 集合与 media `kind=audio` 属 E1。
- **字体未随包**：E1 引入 OFL 子集并附许可证；在此之前验收不依赖系统字体。
- **渲染管线未实现**：`ffmpeg-render-v1` 是契约，不是现有能力；当前成片仍是流复制或归一化拼接。
- **真实 TTS 未提交**：E0 不联网；CosyVoice 的端点、音色、计价与返回格式仍待 E2 按官方文档核实，能力表在真实样本前保持「文档支持，未做真实样本」。
- **1080P 未验证**：只冻结为参数化上采样 profile，不作为画质承诺。
- **整集成片未实现**：属 E3；当前交付仍是逐场成片。
- 探针只证明本地 FFmpeg 具备相关能力，不证明真实 TTS 音质、真实音乐的授权链路或平台兼容性。

## 5. E1 入口

E1 的第一件事是按 §9 建立 schema 7：新增 `DatabaseV7`、`audio`/`subtitleTracks` 两个集合与 media `kind=audio`，在同一提交内带上 v6→v7 的逐字节备份迁移与基线比对（只允许三类差异）；随后实现 `subtitle-align-zh-v1`、`subtitle_save` 与软/烧录两条导出路径，工具面升到 10/10（冻结 9/9）。**E1 不得调用任何供应商，也不得提交付费任务。**
