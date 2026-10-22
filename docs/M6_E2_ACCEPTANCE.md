# M6 E2 验收：音频（CosyVoice、音色绑定、混音与两遍响度）

日期：**2026-10-22**。基线：`master` `4ec3ee6`（M6 E1 字幕，schema 7、tools/rules 10/10）；本包交付 commit 见 `git log`。范围见 [M6 契约](./M6_CONTRACTS.md) §4/§10 的 E2，冻结决策见 §1，官方核实记录见 §12。**E2 全程 TTS 真实请求=0、视频 submit=0、模型调用=0；所有供应商调用均为受控 `httpx.MockTransport`，媒体由本地 `local-tts-v1` 与包内 FFmpeg 生成。**

## 1. 已交付

| 项目 | 交付 |
|---|---|
| TTS 能力表 | `tts-providers-v1`：live 为阿里云百炼 `cosyvoice-v2`（8 个官方核实音色，0.20 CNY/千字符，CNY，RPM 180，`documented-no-real-sample`）；mock 为 `local-tts-v1`（3 个本地音色，价格 `unknown`、仅按字符/次数限额） |
| 本地夹具 | `local-tts-v1`：严格按契约 §8 生成 48 kHz、单声道、16 位 PCM WAV，时长 `clamp(ceil(字符数×0.18),1,8)` 秒，正弦 + 静音、10 ms 淡入淡出；无时钟、无随机、同文本同音色逐字节一致 |
| CosyVoice 适配器 | `CosyVoiceAdapter`：`POST {base}/api/v1/services/audio/tts/SpeechSynthesizer`，`Bearer` Key；请求 `model/input.text/voice/format=wav/sample_rate=48000/rate/volume`；解析 `output.audio.url` 与 `usage.characters`；结果 URL 仅接受 HTTP(S) `*.aliyuncs.com`、禁重定向、上限 25 MB；官方 401/403/404/429/400 错误映射为 `PROVIDER_*` |
| 请求/结果契约 | `TtsRequestV1`（单条台词、不进视频 Job）、`TtsResultV1`（字符、时长、采样率、声道、SHA-256、大小、上游 requestId/URL 过期）；`VoiceBinding` 描述性模型 |
| 工具 | `audio_generate`（`write`、无 Job、锁外 TTS、锁内一次提交）：`episodeId` + 可选台词范围（`sceneId`+`paragraph`+可选 `text`）+ `speaker→voiceId` + 显式 `confirm`；返回逐条资产、字符/次数/金额、绑定与资产视图 |
| 额度与绑定 | 单条 ≤500 字符、每集 ≤2000 字符/40 次请求；价格已核实时单条 ≤0.10 CNY、每集 ≤0.40 CNY；同一集同一 speaker 首次生成即冻结 provider/model/voiceId/speed，变更返回 `VOICE_CONFLICT`；任一超限整批拒绝、不部分提交 |
| 幂等 | 同一台词 + 相同音色（含 provider/model/speed）复用原 `AudioAsset`，不重复计费；同一工具调用按 Store Operation 指纹重放；相同请求 ID 的变更返回 `OPERATION_CONFLICT` |
| 音频域与媒体 | `ProviderAudioSource` 追加可选 `episodeId`/`speaker`；`AudioAsset`/`AudioAssetVersion` 真实写入；`AudioMedia`（`kind=audio`）由服务端 UUID 落盘、SHA-256/大小/采样率/声道/时长与版本互相校验；`MediaFiles` 接受 `.wav`/`.m4a`，`MediaService.view/open_content` 开放音频读取，`/api/media/{id}/content` 返回音频 MIME 并沿用 Range/ETag/下载 |
| 混音 | `mix-zh-v1`：voice 0 dB、BGM −18 dB、SFX −6 dB，每轨 `aresample/volume/adelay/apad/atrim`；BGM 用 `sidechaincompress`（线性阈值 0.1 = −20 dB、ratio 8、attack 20 ms、release 400 ms）；`amix normalize=0` 保留冻结增益；输出 48 kHz 立体声 16 位 PCM WAV |
| 响度 | 两遍 `loudnorm`（先测量后应用，目标 −16 LUFS/true peak −1 dBTP/LRA 11），再用 `ebur128=peak=true` 对交付文件复测；成品响度超 −16±0.5 LU、true peak > −1 dBTP 或时长相距时间线超过 1 帧时返回 `LOUDNESS_OUT_OF_RANGE`/`RENDER_VALIDATION_FAILED` 并删除产物；两遍 JSON 与复测值都由 `MixResult` 返回 |
| 工具面与恢复 | `audio_generate` 追加后 tools/rules **10/10 → 11/11**（mock 与 live 同步），按 `toolFeatures` 冻结 **10/10** 变体（隐藏 `audio_generate`、保留 E1 规则字节）；9/9…1/1 继续按原冻结注册表恢复；`episode_read` 只追加 `audio` 投影（音色表、额度、绑定、资产） |
| 校验与限制 | `validate_audio_changes` 每次提交复核只增不改、逐集 voice 上限（40）、项目 bgm/sfx 上限（8/16）、单条 ≤60 秒、媒体测量一致；新增 `AUDIO_FORMAT_UNSUPPORTED`/`TTS_KEY_MISSING`/`TTS_EMPTY_EPISODE`/`PROVIDER_RESULT_UNTRUSTED` 与 `PROVIDER_*` 传输码 |
| 验收文档 | 本文件；[M6 契约](./M6_CONTRACTS.md) 的 E2 细化、§10 状态与 §12 核实结论已同步，README 与 M5 计划的下一步已更新 |

## 2. 官方核实（E2 前完成，未先写实现）

| 项目 | 核实结果 | 来源 |
|---|---|---|
| 端点 | `POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer`；共享域名 `https://dashscope.aliyuncs.com/api/v1/...` 仍可用 | [HTTP API 参考](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api)、[Base URL 总览](https://www.alibabacloud.com/help/zh/model-studio/base-url) |
| 鉴权 | `Authorization: Bearer <API Key>`、`Content-Type: application/json`；复用 `VAGENT_DASHSCOPE_KEY`，业务空间 ID 可选 | 同上 |
| 请求 | `{"model":"cosyvoice-v2","input":{"text","voice","format":"wav","sample_rate":48000,"rate","volume":50}}` | 同上 |
| 返回 | `request_id`、`output.finish_reason`、`output.audio.url`（24 小时）、`output.audio.id/expires_at`、`usage.characters` | 同上 |
| 模型/价格 | `cosyvoice-v2`，华北2（北京）**2 元/万字符**（0.20 元/千字符），RPM **180** | [cosyvoice-v2 模型页](https://help.aliyun.com/zh/model-studio/cosyvoice-v2) |
| 音色 | 北京 v2 列表中的 `longxiaochun_v2`、`longxiaoxia_v2`、`longyichen`、`longwanjun`、`longlaobo`、`longlaoyi`、`longsanshu`、`longxiaocheng_v2`（E2 只发布该 8 个已核实子集） | [CosyVoice 音色列表](https://help.aliyun.com/zh/model-studio/cosyvoice-voice-list) |
| 错误 | 400 `InvalidParameter/CLIENT_ERROR`、401 `InvalidApiKey`、403 `AccessDenied`、404 模型不存在、429 `Throttling.*`、400 `Arrearage`、400 `DataInspectionFailed`、5xx | [HTTP API 参考](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api) |
| 结果下载 | 只接受 HTTP(S)、`*.aliyuncs.com`、无 userinfo、禁重定向、≤25 MB；下载后用 `inspect_wav` 校验 RIFF/16 位 PCM、采样率、声道与时长 | 实现约束，见 §5 |

**未做真实样本**：未读取真实 Key、未向 DashScope 发送任何请求；`realSample=false`、`verification="documented-no-real-sample"` 保持不变。共享域名与业务空间域名两条 URL 都只由受控 `MockTransport` 覆盖。

## 3. 数据与契约

schema 保持 **7**，不新增迁移；E2 只做两类兼容扩展：

1. `ProviderAudioSource` 追加可选 `episodeId`、`speaker`（默认 `None`）。E1 只冻结音频记录形状、从未写入音频资产，因此历史记录不需要重编码；`AudioAssetVersion`/`AudioAsset`/`DatabaseV7` 的 JSON Schema 因嵌套而机械变化，已由现有基线用例覆盖。
2. 新增 `TtsRequestV1`/`TtsResultV1`/`VoiceBinding`/`AudioLineRef`/`AudioVoice`/`AudioGenerateArgs` 与 `tts.py` 能力表；均不嵌入 Store，不改变已有集合。

额度、绑定与幂等全部从 `state["audio"]` 的 `ProviderAudioSource.text/episodeId/speaker/provider/model/voiceId/speed` 重算，不依赖内存或上一次响应；`AudioAsset` 仍只增不改，`AudioMedia` 与版本在同一次 `store.operation` 事务内提交。

## 4. 预算、确认与幂等

价格口径：`cost = characters / 1000 × 0.20 CNY`，4 位小数；本地夹具价格为 `unknown`，只按字符与次数约束并明确标注。

| 约束 | 值 | 越界 |
|---|---|---|
| 单条台词 | ≤500 字符 | `TTS_TEXT_LIMIT` |
| 每集累计 | ≤2000 字符 | `TTS_TEXT_LIMIT` |
| 每集请求 | ≤40 次 | `TTS_BUDGET_LIMIT` |
| 单条金额（已核实价格） | ≤0.10 CNY | `TTS_BUDGET_LIMIT` |
| 每集金额（已核实价格） | ≤0.40 CNY | `TTS_BUDGET_LIMIT` |
| 确认 | `confirm=true` | `CONFIRMATION_REQUIRED` |
| 音色绑定 | 同集同 speaker 一致 | `VOICE_CONFLICT` |

- **先校验后调用**：`_plan` 在锁内完成台词解析、音色核实、绑定检查与额度检查；任一失败不会发起 TTS。供应商调用在锁外逐条执行，全部成功后才在锁内提交；中途失败会删除全部 `.part` 文件，不留下部分资产。
- **复用**：相同 `(episodeId, speaker, text, provider, model, voiceId, speed)` 直接复用既有资产，`reused=true`，不新增请求与媒体；新工具调用只增加 Operation 记录。
- **并发**：提交时在锁内再次按 draft 复核绑定与额度；若另一调用已写入同一条台词，则丢弃本次 `.part` 并复用对方资产。
- **只读 Run**：`READ_ONLY`，不写资产、不调用 TTS。
- **重放**：同一 Operation 键 + 同一参数直接返回原结果；同键不同参数返回 `OPERATION_CONFLICT`。

## 5. 混音、ducking 与响度

`mix-zh-v1` 的公开入口是 `vagent.video.mixer.mix_tracks(...)`，由 E3 的 `film_render` 复用，E2 不注册渲染工具。

1. **轨道**：每条输入先 `aresample=48000` + `aformat=fltp/stereo`，再 `volume=<gain>dB`、`adelay=delays=<ms>:all=1`、`apad`、`atrim=duration=<时间线>`、`asetpts=PTS-STARTPTS`。默认 voice 0 dB、BGM −18 dB、SFX −6 dB，增益范围 −60…+12 dB。
2. **ducking**：voice 与 bgm 同时存在且至少一条 BGM 标记 ducking 时，voice 经 `asplit` 作为 sidechain，BGM 经 `sidechaincompress=threshold=0.1:ratio=8:attack=20:release=400:makeup=1`；阈值按线性幅度传入（−20 dB → 0.1）。
3. **混合**：各 bus 用 `amix=inputs=N:duration=longest:dropout_transition=0:normalize=0`，避免默认 `normalize=true` 把冻结增益按输入数除；最终补齐/裁剪到时间线并 `asetpts`。
4. **两遍响度**：第一遍同一图 + `loudnorm=I=-16:TP=-1:LRA=11:print_format=json` 输出到 `-f null -` 取得 `input_*`/`output_*`；第二遍把 `measured_I/TP/LRA/thresh/offset` 与 `linear=true` 喂回 `loudnorm`，编码 48 kHz 立体声 16 位 PCM WAV。两遍 JSON 都保存在 `MixResult`。
5. **成品复测**：对交付 WAV 运行 `ebur128=peak=true`，解析 Integrated loudness、LRA 与 True peak；要求 |I − (−16)| ≤ 0.5 LU 且 true peak ≤ −1 dBTP。`loudnorm` 对稀疏/高 LRA 素材的二次测量会有偏差，因此以 EBU R128 复测作为交付门槛；超差返回 `LOUDNESS_OUT_OF_RANGE` 并删除产物。
6. **时长/结构**：`inspect_wav` 校验 48 kHz/2 声道/16 位 PCM；采样数与时长相距时间线不超过 1 帧；失败删除产物。

失败矩阵（`MixError`，`stage` 对应 `source/mix/encode/validate`）：缺文件/空时间线 `RENDER_SOURCE_MISSING`；FFmpeg 启动或编码失败 `RENDER_ENCODE_FAILED`；超时 `RENDER_TIMEOUT`；静音/不可达响度、成品超差 `LOUDNESS_OUT_OF_RANGE`；结构/时长不符 `RENDER_VALIDATION_FAILED`。

## 6. 工具面、规则与恢复

- 新 Run：`toolFeatures.video.toolsVersion=11`、`rulesVersion=11`，配置新增 `tts`（`tts-providers-v1` 当前模式表）；规则文本 = `VIDEO_RULES_V10`/`LIVE_VIDEO_RULES_V10` + `AUDIO_RULES`，旧 V10 字节未改写。
- `audio_generate`：`effect=write`、`feature=video`、`async_write_execute`（锁外 TTS/写 `.part`，锁内提交）；无 Job、无等待、无计费 Job。
- 冻结变体：新增 `register_tools_variant(10, ...)`（mock 与 live），配置严格为 `mode/toolsVersion=10/rulesVersion=10/capabilities` 四键、规则为 V10、`exclude=("audio_generate",)`；9/9 与更早变体在同一次改名中同时隐藏 `subtitle_save` 与 `audio_generate`，因此历史 10/10、9/9…1/1 的 `toolFeatures` 仍逐字段可匹配。
- `episode_read` 追加 `audio`：当前模式的 `provider`（含音色表、价格、验证状态）、`usage`（字符/次数/金额与上限）、`bindings`、`assets`；既有字段不变，旧 Run 恢复时读取视图多一个键不影响签名。
- headless 规划排除 `audio_generate`（付费写工具），`TTS_*`/`VOICE_CONFLICT`/`AUDIO_*` 已映射冻结退出码。

## 7. 测试证据

命令：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_m6_audio.py tests/test_m6_audio_generate.py tests/test_m6_audio_mix.py tests/test_m6_audio_acceptance.py tests/test_m6_tools.py tests/test_m6_schema.py
.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --test tests/test_*state.mjs
```

- 新增 **36 项**：`test_m6_audio.py` 14（能力表/价格、公式、确定性、WAV 校验、适配器请求与下载、共享/业务空间端点、5 类官方错误、HTTP(S) 结果地址、不可信 URL、本地适配器）；`test_m6_audio_generate.py` 14（确认、资产/媒体/额度/只读、部分台词、重放、复用、同键改参冲突、音色冲突、未知音色/缺映射、字符与请求上限、Range/MIME、live CosyVoice、无 Key 的缓存重放）；`test_m6_audio_mix.py` 7（48k 立体声与两遍测量、ducking 总线、静音拒绝、缺文件/空轨、图参数、指纹、解析器）；`test_m6_audio_acceptance.py` 1（`episode → local TTS → 混音 → AAC/MP4` 可解码，全程零真实请求）。
- 关键断言：本地 WAV 同输入逐字节一致；`tts_cost(500)=0.1000`、`tts_cost(2000)=0.4000`；字符/请求超限时 `state["audio"]` 与 `media` 均为空；音色冲突不改资产；live 模式 `provider=cosyvoice`、`estimatedCostCny` 与字符数精确对应、`jobs` 仍为空；混音成品 EBU R128 `|I+16|≤0.5`、true peak ≤ −1，ducking 总线在台词窗内至少低 1 dB；验收 MP4 探测到 `h264` + `aac 48000 Hz stereo`。
- 机械更新：既有断言中的 10/10 改为 11/11（`test_m6_tools`、`test_m5_tools`、`test_m2_clients`、`test_m2_compatibility`、`test_m2_generation`、`test_m2_integration`、`test_m3_recovery`、`test_live_application`、`test_video_integration`、`test_wait_compatibility`）；冻结变体查找剥离新增 `tts` 键；`episode_read` 空视图补 `audio: None`；`test_video_integration` 的规则漂移补丁目标改为 `VIDEO_RULES_V11`；E1 的“音频媒体读取返回 `MEDIA_NOT_FOUND`”断言改为音频视图。
- 完整回归：**1309 passed / 3 skipped（836.25 秒，0:13:56）**。

## 8. 明确未做与边界

- **真实 TTS**：E2 未读取真实 Key、未提交 CosyVoice；能力表保持「文档支持，未做真实样本」。真实音色、编码、账单与并发需在 E4/单独授权下验证。
- **CLI/Web**：E2 未新增 `vagent audio`、`/api/audio*` 或 Web 音频面板；配音通过 Agent 工具、Python 服务与媒体面（`/api/media/{id}`）可用，界面与真实浏览器证据留待 E4。M5 E2 同样以离线后端验收为界。
- **渲染集成**：`film_render`/`render_read`、转场、标题卡、1080P、整集 60–90 秒成片与字幕/音频合并属 E3；E2 只交付混音服务与测量，`subtitle_service` 仍复制源音频。
- **素材导入**：`ImportedAudioSource` 的 `license/sourceUrl` 记录形状已冻结，但 E2 未提供 BGM/SFX 导入工具；CC0 素材清单与来源导入留待 E4/M7。
- **音色选择**：只发布官方 v2 的 8 个北京系统音色；声音复刻、声音设计、方言音色与跨地域音色未接入。`longxiaochun_v2` 等 ID 以官方列表为准，真实可用性未抽样。
- **未做**：9:16 竖屏、1080P 画质增强、去水印、多轨时间线编辑、可执行插件、PyPI 公开发布。

## 9. 检查结果

```text
ruff check src/vagent tests scripts          # 通过
ruff format --check src/vagent tests scripts # 通过
全部前端状态测试（43 项）通过（本包未改前端）
```

完整回归与耗时见 §7 的 1309 passed / 3 skipped；三项 skip 仍是既有的 Windows 符号链接权限用例，与 E2 无关。
