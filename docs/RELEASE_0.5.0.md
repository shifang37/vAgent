# v0.5.0 发布说明

**发布日期**：2026-10-24  
**版本**：0.5.0  
**M7 E4 代码提交**：`760fed9`（文档同步 `b1ae8b4`，本发布文档另计）  
**范围**：M1–M7；本版新增 **M6 E1–E4（成片后期）** 与 **M7 E0–E4（多集与工作台）**

本版把**成片后期**（字幕、配音、混音、转场、标题卡、整集渲染、720P/1080P 导出）与**多集工作台**（季、批量队列、时间线编辑）合入 `master`，形成覆盖 M1–M7 的发布：从一句话/剧本改编、分集分场分镜、真实多镜头生成，到字幕/配音/混音/转场/整集成片，再到季、批量与时间线导出。v0.4.0（2026-10-22）之后的 17 个提交全部纳入本版。

## 本版内容

- **M6 E1 字幕**：schema **7**（`audio`/`subtitleTracks` + media `kind=audio`）与 v6→v7 逐字节备份迁移；`subtitle-align-zh-v1` 确定性对齐（终止/次级标点断句、每条 ≤48 字符/每行 ≤24、≥0.5 秒、0.05 秒间隙、绝不跨镜头）；`subtitle_save` 与工具面 **10/10**（冻结 9/9）；SRT/ASS、包内 OFL 字体（`VAgentSubtitleSC-400.ttf`）、软字幕/旁挂 SRT/烧录/both 与只增不改的成片版本；`vagent subtitle show|save|export`、`/api/subtitles*` 与 Web 字幕区。
- **M6 E2 音频**：官方核实的 CosyVoice v2 适配（复用视频 Key、受控 HTTP、禁重定向、仅信任 `*.aliyuncs.com`）、`tts-providers-v1` 与离线 `local-tts-v1`、`audio_generate`（显式确认、单条 ≤500 字符/每集 ≤2000 字符/40 次请求、0.10/0.40 CNY 上限、同集同 speaker 音色冻结）；`mix-zh-v1`（voice 0 dB、BGM −18 dB ducking、SFX −6 dB、两遍 `loudnorm`、EBU R128 复测）；工具面 **11/11**；全程 TTS 真实请求=0。
- **M6 E3 转场/标题卡/整集/导出**：schema **8**（`renders`）、`ffmpeg-render-native-v1`、`film_render`/`render_read`（**13/13**）；`cut/fade/crossfade` 有效时间线、跨场整集、720P/1080P、标题卡（包内字体 ASS）、soft/burn/both、voice 按 cue、BGM/SFX、响度写入渲染记录；逐场归一化失败隔离与同指纹幂等；CLI `film render|renders`、`/api/renders` 与 Web 渲染区。
- **M6 E4 整集成片验收**：`scripts/evaluate_m6.py` 3 用例离线走真实 Store、字幕/配音工具、受控 Wan、Worker、渲染服务与包内 FFmpeg；89.3 秒 8 场 19 镜头整集、单场失败隔离、拒绝矩阵；Web 整集播放器、逐场渲染状态、旁挂 SRT 下载与字幕开关（客户端 overlay）。
- **M7 E1 季与 schema 9**：`seasons`/`batches`/`timelines` 三个空集合与 v8→v9 逐字节备份迁移、v1–v8 全部可读；`Season`/`SeasonVersion` 只增不改、每项目 ≤8 季、每季 1–4 集有序唯一；`season_save`/`season_read`（**15/15**）；CLI/Web 季面板。
- **M7 E2 批量队列**：`Batch`/`BatchEpisode`/`BatchEstimate` 与 `season_generate`（**16/16**）；1–3 集、逐集独立事务与失败隔离、每集 20 Job/60.00 CNY、批次 60 Job/180.00 CNY、每季一个 `running` 批次、显式 `resumeBatchId` 恢复与 Store 崩溃恢复（`BATCH_INTERRUPTED`）、确定性批次 ID 与同内容重放；`/api/seasons/{id}/estimate`、`/api/sessions/{id}/seasons/generate` 与 Web 批量确认/恢复。
- **M7 E3 时间线工作台**：每集一条只增不改的 `Timeline`/`TimelineVersion`（`timeline-zh-v1`：有序 clip 含镜头内裁剪、转场、整集字幕轨、音轨、标题卡；≤64 版本）、`timeline_save`/`timeline_read`（**18/18**）；`film_render` 的 `scope=timeline` 与 `ffmpeg-render-timeline-v1`、`timeline-subtitle-remap-v1`；客户端受控导入 BGM/SFX（WAV/MP4、≤25 MB、≤60 秒、许可/来源必填、SHA-256 去重）；CLI `timeline show|sources|save`、`audio list|import`、`film render --timeline` 与 Web 时间线工作台。
- **M7 E4 验收**：`scripts/evaluate_m7.py` 3 用例（3 集 36 Job 批量与重放、`BATCH_EMPTY` 隔离与显式恢复、14 镜头 67→65.6 秒时间线 v1→v2 重拼与 22 条字幕精确重映射、过期/非法编辑拒绝）；受控 Chrome 完成编辑/预览/导出 v3 闭环；安装态 wheel 新增 M7 两段。
- **既有能力不变**：真实 Wan t2v/i2v/flf2v 样本、Kling/fal 受控接入与逐供应商预算桶、headless 两阶段授权、用户 skills、成片/字幕/渲染媒体面、两 Key 直用 K0–K3。

## 验证

- **本地完整回归**：Python 3.12.14 在发布工作树上复跑 **1409 passed / 3 skipped**（1647.09 秒，0:27:27；`src/`/`tests/` 相对 `760fed9` 未变，耗时偏长是本机同期负载较高）。3 项 skip 为既有的「当前 Windows 账户无符号链接权限」用例，未新增 skip；日志 `tmp/release-0.5.0-pytest.log`。
- **静态检查**：`ruff check` 通过；`ruff format --check` **217 文件**已格式化；`node --check` 与 8 个前端状态模块 **78 项** Node 测试通过。
- **CI**：四组矩阵（ubuntu/windows × Python 3.11/3.12）在 `760fed9` 上全部 **success**（[运行 40063039486](https://github.com/shifang37/vAgent/actions/runs/40063039486)，attempt 2；attempt 1 的 Windows 3.12 为 runner 通信中断，非测试失败）。
- **受控浏览器**：M7 E4 时间线面板「v2/14 镜头/65.6 秒 → 顺序预览 → 上移镜头保存 v3 → 导出成片 v3」闭环，Range 206、页面错误 0；M6 E4 整集播放、字幕开/关/再开 7/7。
- **安装态 wheel smoke**：**PASS**（`output/release-0.5.0-wheel-smoke.json`）。从 `site-packages` 导入，覆盖文本/质量/MCP、Mock 视频工具、持久等待与重启续接、M1-B 五用例、M1-C 七用例、M2 工作台与七用例、M3 成片拼接与六用例、M5 分集工作台与五用例、M6 渲染工作台与三用例、M7 季/时间线工作台与三用例、两 Key 零费用探测与热更新，并清理临时数据。
- **真实付费样本**：本版**未新增**真实付费样本；M6/M7 的成片验收复用 M5 E4 已批准的 `fridge-notes` 18 个真实 Wan 镜头（`新增 submit=0`、新增费用 0）。M6/M7 全程视频 submit=0、文本模型调用=0、TTS 真实请求=0（M7 的批量/时间线用例只走受控传输与本地媒体）。

## 构建产物

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `shifang37_vagent-0.5.0-py3-none-any.whl` | 1,309,619 字节 | `f38fe684cb508c3eef380776dae7f3114adaea2a332ff7b3ca6c4dd52abb482e` |
| `shifang37_vagent-0.5.0.tar.gz` | 2,334,843 字节 | `76b9b7970d331e70d83ffaa68156ce4963af65a028c413bfc66b09979209ee36` |

哈希为本次发布实测值；从同一提交重新构建不保证逐字节一致，请以 `git show v0.5.0` 与本节记录为准。sdist 打包的是本文件定稿前的副本（其中 sdist 行标注为「构建后补记」），wheel 不受文档影响。上一版产物保留在 `dist/python/archive-0.4.0/`。

## 安装

要求 Python 3.11+。从本仓库构建并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# 或安装已构建的 wheel：
.\.venv\Scripts\python.exe -m pip install dist\python\shifang37_vagent-0.5.0-py3-none-any.whl
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\vagent web --mcp-local
```

打开 `http://127.0.0.1:3210`，在设置页填写编排模型与视频供应商 Key。`vagent episode plan "剧本.txt"` 可离线改编为分集分场分镜；`vagent season save|show|generate` 管理季与批量；`vagent timeline save` 与 `vagent film render --timeline` 导出时间线成片。`dist\python\archive-0.4.0\` 保留上一版构建产物，`dist\python` 根目录只放当前版本，避免安装态脚本误取旧 wheel。

## 兼容与升级

- 存储升为 **schema v9**（`seasons`/`batches`/`timelines`）。打开 v1–v8 数据时在实例锁下先用原版本校验器验证，再把原始字节备份为 `state-v1-<UUID>.json` 并原子迁移；v8→v9 只增加三个空集合，不重编码历史记录。
- v9 不能被 0.4.0 及更早版本直接读取；回退需在服务退出后恢复完整数据目录，单份状态备份不能替代。
- 工具面 **18/18**；旧 Run 按 `toolFeatures` 身份重建冻结注册表（18/18 … 1/1），签名与 prompt/规则字节保持不变（新增版本只追加，不改旧版本）。
- 成片、字幕轨、渲染任务与时间线版本**只增不改**：旧成片按原校验器读取，不补写新字段；旧字面量 `ffmpeg-render-v1`/`ffmpeg-render-native-v1` 不变，时间线渲染只使用 `ffmpeg-render-timeline-v1`。
- 配置与 Key 的存放方式不变；Kling/fal Key 只在对应供应商请求中使用。
- 3 项 skip 仍为 Windows 符号链接权限的既有边界。

## 已知限制

- **真实 Kling/fal 账号未做样本**：能力表继续标注「文档支持，未做真实样本」，真实主机、编码与账单未验证（属 M8）。
- **真实 CosyVoice 未提交**：M6 只使用 `local-tts-v1`；真实音色、编码与账单需单独授权（属 M8）。
- **真实 DeepSeek 分集编排未执行**：M5 E4 的文本模型调用=0，属 M8 的补齐项。
- 实际账单仍为 `unknown`；估算与供应商 `usage` 均不当作扣费凭据。
- 不支持 9:16 竖屏；未接入 Sora/Veo/Seedance/Replicate；可执行插件/插件市场、后台批处理守护进程未实现。
- 时间线预览是客户端按 clip 顺序播放并应用裁剪偏移，**不是交付物**；导出成片才是唯一交付口径。
- BGM/SFX 只支持客户端受控导入，未接入第三方音乐 API；M4A 只按容器头与包内 FFmpeg 探测，未做真实样本。
- 公共 PyPI 发布未做；本版只提供仓库内构建的 wheel/sdist。

## 证据索引

- M6：[契约](./M6_CONTRACTS.md)、[E0](./M6_E0_ACCEPTANCE.md)、[E1](./M6_E1_ACCEPTANCE.md)、[E2](./M6_E2_ACCEPTANCE.md)、[E3](./M6_E3_ACCEPTANCE.md)、[E4](./M6_E4_ACCEPTANCE.md)
- M7：[契约](./M7_CONTRACTS.md)、[E0](./M7_E0_ACCEPTANCE.md)、[E1](./M7_E1_ACCEPTANCE.md)、[E2](./M7_E2_ACCEPTANCE.md)、[E3](./M7_E3_ACCEPTANCE.md)、[M7 验收](./M7_ACCEPTANCE.md)
- 上游：[M5 计划](./M5_PLAN.md)、[M5 契约](./M5_CONTRACTS.md)、[M5 E4](./M5_E4_ACCEPTANCE.md)、[M4 归一化](./M4_NORMALIZATION.md)、[M3 验收](./M3_ACCEPTANCE.md)、[M2 i2v/flf2v](./M2_I2V_ACCEPTANCE.md)、[官方核实](./VIDEO_API_NOTES.md)
- 下一阶段：[M8 计划](./M8_PLAN.md)
- 上一版：[v0.4.0 发布说明](./RELEASE_0.4.0.md)
