# M8 E3 验收：真实 CosyVoice 配音与真实 DeepSeek 编排

日期：**2026-10-25**。基线 `master` `eddc5d8`（M8 E2 提交节流与账单已合入；schema 9、tools/rules 18/18、168 个契约模型）。契约见 [M8 契约](./M8_CONTRACTS.md) §9 的 E3，清单与金额见 [M8 计划](./M8_PLAN.md) §5，机器可读基线见 `docs/M8_E0_BASELINE.json`。

本轮按 **D6「清单与金额任一变化需重新授权」** 取得单独授权：**只执行现有凭据可跑的两项**——CosyVoice 1 集（≤0.40 CNY）与真实 DeepSeek 编排 1 Run（<2 CNY）；Kling 5 秒与 fal 5 秒两项**因缺少 `VAGENT_KLING_API_KEY` / `FAL_KEY` 未执行**，如实记录为凭据缺失，能力表继续标注「文档支持，未做真实样本」。**全程视频 submit=0、Kling/fal 请求=0。**

## 1. 结果摘要

| 样本 | 授权 | 结果 | 关键证据 |
|---|---|---|---|
| CosyVoice v2 配音 1 集 | ≤0.40 CNY | **成功**：19 条台词、170 字符、19 次请求 | 19 个 `request_id`、19 个 48 kHz 单声道 16 位 WAV（合计 3,148,676 B）、FFmpeg 解码通过、Range 206；估算 0.0340 CNY，实际账单 `unknown` |
| 真实 DeepSeek 编排 1 Run | <2 CNY | **成功**：4 次模型调用、3 次工具调用（`episode_plan`→`episode_save`→`episode_read`） | Run `526f8747-0a33-4901-a477-876f1cc08d77`，输入 96,617 / 输出 2,176 Token，保存 8 场/19 镜头/90 秒/19 条台词；Job=0 |
| Kling `kling-3.0-turbo` 5s 720P | 0.56 USD | **未执行**：`VAGENT_KLING_API_KEY` 未配置 | 未发送任何请求（0 次鉴权、0 次提交） |
| fal `kling-video/v3/standard` 5s | 0.42 USD | **未执行**：`FAL_KEY` 未配置 | 未发送任何请求（0 次鉴权、0 次提交） |

## 2. 零费用预检与凭据

预检脚本 `tmp/m8-e3/preflight.py` 只做只读检查（DeepSeek `GET /models` 不产生 Token，DashScope 只计算端点主机不发请求），证据 `output/m8-e3/preflight.json`：

| 项 | 值 |
|---|---|
| DeepSeek `GET /models` | HTTP 200；模型 `deepseek-flash`、`deepseek-v4-pro`；配置的 `deepseek-flash` 可用；**0 Token、0 计费** |
| DashScope / CosyVoice 端点 | `https://ws-qc9azaknvzlk95v8.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer`（业务空间域名；未发送请求） |
| Kling / fal | `configured=false`，本轮不提交 |
| 视频模式 | `off`（DeepSeek 样本用 `mock`，见 §4.3；任何模式下都没有登记或提交视频 Job） |

凭据只从本机 `.env` 读取，验收文档与证据文件只记录长度/前缀，不记录 Key 值。

## 3. CosyVoice 真实样本

### 3.1 路径

样本 `tmp/m8-e3/cosyvoice_sample.py` 走产品自身的链路，而不是直接调适配器：

1. `tests/fixtures/m5/texts/rainy-cafe.md`（`fridge-notes` 同源的 969 字中文短篇）经 `plan_episode`（`text-split-zh-v1`）切成 8 场/19 镜头/90 秒、19 条台词、170 字符；
2. `episode_save` 保存分集与冻结台词；
3. `audio_generate`（`confirm=true`、live 模式）对**整集 19 条台词**合成配音，音色映射为 `画外 → longxiaochun_v2`、`陈默 → longwanjun`、`林晚 → longyichen`；
4. 工具的媒体路径与包内 FFmpeg 校验产物。

**音色绑定**写入 `AudioAsset` 版本（`provider=cosyvoice`、`model=cosyvoice-v2`、`voiceId`），同一集同一 speaker 的绑定一旦写入即冻结。

### 3.2 结果

| 项 | 值 |
|---|---|
| 分集 | `episodeId=8afa5ba3-7046-494c-abc5-b2222126220a`，8 场 / 19 镜头 / 90 秒 / 19 条台词 / 170 字符 |
| 供应商 / 模型 | `cosyvoice` / `cosyvoice-v2`（`tts-providers-v1`、`cosyvoice-v2-beijing-20261022` 价格版本） |
| 端点主机 | `ws-qc9azaknvzlk95v8.cn-beijing.maas.aliyuncs.com` |
| 结果主机 | `dashscope-result-bj.oss-cn-beijing.aliyuncs.com`（探针实测；适配器对每次下载仍只信任 `*.aliyuncs.com`） |
| 请求数 / 字符 | **19 次请求 / 170 字符**（单条 ≤500、每集 ≤2000、≤40 次、≤0.40 CNY 全部满足） |
| 产物 | 19 个 `audio/wav`，合计 **3,148,676 字节**；48 kHz、单声道、16 位 PCM；时长 0.85–3.01 秒（逐条见证据） |
| 解码 | 包内 FFmpeg 7.1 `-f null -` 解码 **exit 0**、无 stderr |
| 播放/Range | 首个媒体 `Range: bytes=0-99` → **206**，`Content-Range: bytes 0-99/146924`，`Accept-Ranges: bytes` |
| 成本 | 产品口径估算 **0.0340 CNY**（170 字符 × 0.20/千字符）；供应商 `usage.characters` 合计 **314**；实际账单 `unknown` |
| 视频 | `jobs=0`、视频 submit=0、文本模型调用=0 |
| 耗时 | 单条 0.77–2.08 秒（19 条合计约 25 秒） |

逐条 `request_id`、`textSha256`、`sha256`、字节数与时长见 `output/m8-e3/evidence.json` 的 `cosyvoice.providerCalls` / `cosyvoice.assets`；报告本身是 `output/m8-e3/cosyvoice.json`。

前 4 条请求 ID：`58959249-5bbf-91fb-bc2c-dba3d246a069`、`c254c60a-66a6-9b77-8633-3214fa4d277d`、`75141d29-9d75-9fc2-9df8-a202c8571f9d`、`b897f15b-d512-9df9-91b0-a8e666d32a88`。

### 3.3 缺陷 1（真实样本发现）：CosyVoice 返回「流式 WAV」头，产品的严格校验拒绝它

首轮真实请求在第一条台词就以 `AUDIO_FORMAT_UNSUPPORTED（块长度越界）` 失败：DashScope 返回的 OSS 对象带**流式占位长度**——`RIFF` 长度 `2147483583`、`data` 块长度 `2147483547`（均为 `0x7FFFFFxx`），而对象本身已完整（62,444 字节）。`inspect_wav` 的「块长度必须落在文件内」检查因此拒绝，真实 TTS 链路完全不可用；此前所有用例都用 `local-tts-v1` 的规范 WAV，所以离线套件看不到。

证据：`output/m8-e3/cosyvoice-probe.json`（1 次 2 字符请求，记录原始响应、`headHex` 与逐块 `declaredSize/fitsInFile`）与 `output/m8-e3/cosyvoice-probe.wav`。

修复（`src/vagent/video/tts.py`）：新增 `normalize_streaming_wav()`，在下载完成后**只**把 `0x7F000000` 以上的 RIFF/data 占位长度改写为真实长度，其余头部字节逐字节保留；占位特征不成立（例如真的截断）时原样返回，`inspect_wav` 继续拒绝。新增 2 项测试：占位头被规范化且与原始规范 WAV 逐字节相等；截断的真实长度仍被拒绝；适配器下载流式头后返回规范化产物（sha/时长/字节数以规范化后为准）。

## 4. 真实 DeepSeek 编排

### 4.1 目标

M5 E4 的真实样本 `文本模型调用=0`，「剧本 → 分集」的真实链路一直未验证。本轮用**真实 DeepSeek**跑 1 个 Run：`episode_plan`（离线切分）→ `episode_save`（保存分集）→ `episode_read`（读回核对），不调用任何视频生成、配音或渲染工具。

### 4.2 结果

| 项 | 值 |
|---|---|
| Run | `526f8747-0a33-4901-a477-876f1cc08d77`，`model=deepseek-flash`，`status=completed` |
| 模型调用 | **4 次**（`step 1–4`，均为 `responded`，无错误），输入 **96,617** / 输出 **2,176** Token，有效耗时 **12.375 秒** |
| 工具调用 | **3 次**：`episode_plan`、`episode_save`、`episode_read`（操作键 `526f8747…:1|2|3:<callId>`） |
| 上下文 | `contextBytes=97,404`（96 KiB 预算内），未裁剪历史（`droppedMessages=0`） |
| 结果 | 1 集：8 场 / 19 镜头 / 90 秒 / 19 条台词；`shotBudget` 19 镜头、20 Job、60.00 CNY（`wan27-beijing-720p-20261010`） |
| 视频 | `jobs=0`；视频 submit=0；TTS 请求=0 |
| 成本 | 文本 Token 已记录；实际账单 `unknown`（仓库没有文本计价表，不按估算冒充账单） |
| 提示词 | 手稿 sha256 与提示词 sha256 记录在证据里；模型未改写原文（分集台词与 `episode_plan` 一致） |

证据：`output/m8-e3/deepseek.json`（含 `modelCalls`、`toolCallKeys`、`answer`）与 `output/m8-e3/evidence.json`。

### 4.3 事实（非缺陷）：`videoMode=off` 下没有视频工具，模型无法完成分集编排

第一次 Run 用 `VAGENT_VIDEO_MODE=off`（试图「零工具」跑文本编排）：`ApplicationService.open` 只为 `mock`/`live` 注册视频工具，`off` 视图只有 5 个核心工具（`project_read/update`、`plan_update`、`artifact_save/read`、`skill_read`），模型如实回答「当前工具清单里没有 `episode_plan`/`episode_save`/`episode_read`，无法按要求完成」，Run 以 `completed` 结束、**0 次视频提交**（6,758 输入 / 517 输出 Token）。

这是既有设计（off = 无视频/语音能力），不是缺陷；但它决定了真实编排样本必须在 `mock` 下跑：`mock` 只把**视频供应商**替换为确定性模拟，**文本模型仍是真实 DeepSeek**，且不会产生任何供应商费用。验收因此把「`off` 无视频工具」作为事实记录，并改用 `mock` 重跑。

### 4.4 缺陷 2（真实样本发现）：18/18 工具面在默认 64 KiB 预算下第一轮就 `CONTEXT_LIMIT`

`mock` 模式的 Run 在 `modelSteps=0` 时以 `CONTEXT_LIMIT` 失败：首轮上下文 = 32 个工具定义（**45,956 字节** compact JSON）+ 规则（**14,053 字节**）+ Skills/项目事实/预算块 + 需求，实测约 **64.5 KiB**，超过 `VAGENT_CONTEXT_BYTES` 的 64 KiB 默认值。也就是说：**任何新用户用默认配置发一个普通需求都会在第一步失败**，而此前所有里程碑都只把测试夹具提到 96 KiB、把产品默认留在 65536，因此从未被真实 Run 暴露。

修复（`src/vagent/config.py`、`src/vagent/context.py`、`.env.example`、本机 `.env`、README）：

- 产品默认 `VAGENT_CONTEXT_BYTES` 由 **65536 → 98304**（96 KiB，与 M6 E1 起所有验收夹具一致）；`Config` 与 `load_config()` 两处默认同步，产品两条构建路径（`ApplicationService.runner`/`headless_runner`）都显式传 `ContextBuilder(config.context_bytes)`。`ContextBuilder()` 自身的默认仍是 65536：它只作为直接构造的兜底，而 v1/v2 持久 Run 的 `contextSignature` 正是用 65536 签的，改动兜底会让 `test_b0_compatibility`/`test_m2_compatibility`/`test_wait_compatibility` 的冻结签名全部失效。
- 96 KiB 相对 64.5 KiB 的第一轮实测留出约 33 KiB 给需求正文与首轮工具结果；`ContextBuilder` 仍按原规则在超预算时裁剪旧轮次，不截断当前需求。
- README 的预算说明同步为 98304。
- **兼容性**：`contextSignature` 含预算值，所以用旧预算记录的同工具面 Run 需要 `VAGENT_CONTEXT_BYTES=65536` 才能恢复；跨里程碑的旧 Run 本来就因工具面/规则变化返回 `RESUME_CONFIG_CHANGED`，本次不新增这一类拒绝。`tests/test_context_optimization.py` 的旧检查点用例显式固定 65536，验证旧预算的签名与恢复路径仍然工作。

## 5. 费用与「零新增」边界

| 项 | 数量 | 估算 | 实际 |
|---|---|---|---|
| CosyVoice 成功样本 | 19 次请求 / 170 字符 | 0.0340 CNY（产品口径） | `unknown` |
| CosyVoice 首轮失败（第 1 条台词） | 1 次请求 / 8 字符 | ≈0.0016 CNY | `unknown` |
| CosyVoice 诊断探针 | 1 次请求 / 2 字符 | ≈0.0008 CNY | `unknown` |
| 若按供应商 `usage.characters` 计 | 333 字符 | ≈0.0666 CNY | `unknown` |
| 真实 DeepSeek 编排 | 1 Run / 4 次调用 | — | `unknown`（输入 96,617 / 输出 2,176 Token） |
| 真实 DeepSeek `off` 试跑 | 1 Run / 2 次调用 | — | `unknown`（输入 6,758 / 输出 517 Token） |
| Kling / fal | **0** | — | 未提交 |

**「实际账单 unknown」不是 0**：本机未取得 DashScope 与 DeepSeek 的后台账单，E2 的账单账本只接受人工录入的实际扣费，因此证据里保持 `unknown`，不计入估算。实际支出仍在授权上限（0.40 CNY + 2 CNY 文本）之内。

## 6. 基线保护

重新运行 `scripts/capture_m8_baseline.py` 后 `docs/M8_E0_BASELINE.json` 的 SHA-256 仍为 `f3dc91c1c08db59ae75347d438e5fe44a6c0ac54b53da0ffaa12f4e8cbcb3706`，与 E0/E1/E2 记录一致：**168 个 `schemaSha256`、`contractsSha256`、`capabilitiesSha256`、两套工具/规则哈希与 `routeProbe.rowsSha256` 全部逐字节不变**。本轮改动的 `tts.py`（函数级规范化）、`config.py`/`context.py`（默认预算）都不在 9 个已哈希契约模块的模型定义内，因此基线不动。

工具面仍是 **18/18**、schema 仍是 **9**、新增模型工具 **0 个**、新增 schema 集合 **0 个**。

## 7. 代码、测试与门禁

| 文件 | 变更 |
|---|---|
| `src/vagent/video/tts.py` | `normalize_streaming_wav()` + `STREAMING_SIZE_SENTINEL`；适配器下载后规范化再校验 |
| `src/vagent/config.py` | `context_bytes` 默认 98304（`Config` 与 `load_config`） |
| `src/vagent/context.py` | `ContextBuilder` 默认 98304 |
| `tests/test_m6_audio.py` | 新增 2 项（占位头规范化、适配器规范化下载产物），共 16 项 |
| `tests/test_context_optimization.py` | 旧检查点用例显式固定 65536（签名与恢复使用旧预算） |
| `tests/test_resume.py` | `test_cli_resume_offline_run_without_key_or_source_cwd` 改为用产品配置的预算创建 Run（原来依赖 `ContextBuilder()` 兜底值，默认改为 96 KiB 后与 CLI 的恢复路径不匹配） |
| `tests/test_configuration.py` | 新增 1 项：默认预算 98304，且 18/18 工具面 + 规则仍留出 ≥8 KiB 余量（64 KiB 下该断言会失败） |
| `.env.example`、`README.md` | 预算默认值同步为 98304 |
| `tmp/m8-e3/*` | 预检、CosyVoice 样本、探针、DeepSeek 运行、证据汇总脚本（本地证据，不进入 wheel） |

定向回归（含 M8 E1/E2 全部套件、M6 音频、上下文与配置）：

```text
tests/test_b0_compatibility.py tests/test_m2_compatibility.py tests/test_wait_compatibility.py
tests/test_m6_audio.py tests/test_m6_audio_generate.py
tests/test_m8_billing.py tests/test_m8_bill_api.py tests/test_m8_bill_cli.py
tests/test_m8_gate.py tests/test_m8_gate_worker.py
tests/test_m8_route_api.py tests/test_m8_route_cli.py tests/test_m8_routing.py
tests/test_configuration.py tests/test_context_optimization.py tests/test_context_skills.py tests/test_web.py
215 passed, 2 skipped in 50.07s
```

- `ruff check`：全仓库通过；`ruff format --check`：229 个文件已格式化。
- 本地全量回归（`pytest -q`）：**1512 passed / 4 skipped**（1744.21 秒，0:29:04；4 项 skip 是既有的 POSIX-only 与 Windows 符号链接用例）。首轮全量回归暴露一处夹具假设：`test_resume.py::test_cli_resume_offline_run_without_key_or_source_cwd` 用库级兜底预算（65536）创建 Run，再用 CLI（产品预算 98304）恢复，导致 `RESUME_CONFIG_CHANGED`；已改为用产品配置的预算创建，恢复语义不变。
- 前端八个状态文件、受控浏览器、安装态 wheel 与四组 CI 仍属 **E4**（本轮不改前端、不改工具面）。
- 本机 `NO_PROXY` 含 `[::1]`，会让 `langchain`/`openai` 构造同步客户端时抛 `httpx.InvalidURL`；产品在 `ModelHttpClient` 启动时已用 `repair_proxy_environment()` 修复（真实 Run 未受影响）。`tests/test_context_optimization.py` 的单文件运行会先于任何 `ModelHttpClient` 构造，因此在该环境里需要先修复代理环境再跑——这是既有环境问题，不是本轮改动引入的回归。

## 8. 未做与限制

1. **Kling / fal 真实样本未做**：本轮没有 `VAGENT_KLING_API_KEY` / `FAL_KEY`，未发送任何请求。能力表继续标注 `documented-no-real-sample`，`capabilitiesSha256` 不变；样本成败都不改冻结能力表。
2. **仅 3 个音色**：真实配音只用了 `longxiaochun_v2`、`longwanjun`、`longyichen`；其余 5 个已核实音色仍未做真实样本。
3. **字符口径差异**：供应商 `usage.characters`（314）约为台词字符（170）的 1.85 倍。产品 `usage.characters`/`estimatedCostCny` 按**台词字符**计算，而 `audio_generate` 的逐条与每集成本上限校验用的是**供应商上报字符**。两者都在 0.40 CNY 以内，但估算口径需要后续版本决定（建议 E4 记录为已知偏差，不擅自改冻结的计价语义）。
4. **无浏览器播放**：音频用 Range 206 + 包内 FFmpeg 解码代替；整集成片浏览器播放已在 M6/M7 验收覆盖，本轮不重复。
5. **文本计价不存在于仓库**：DeepSeek 的实际扣费不进入 `usage.billing`（E2 账本只收视频/TTS 的人工实际值），保持 `unknown`。
6. **「未做真实样本」标签按契约 §2.5 保持不变**：CosyVoice 已经跑通真实样本，但 `tts-providers-v1` 的 `realSample=false` / `verification="documented-no-real-sample"` 属于冻结能力表，篡改会让 `capabilitiesSha256` 变化；契约规定标签升级由后续版本单独决定，本轮只留证据。
7. **没有真实视频提交**：本轮两项样本都不登记视频 Job；`EPISODE`/`season` 批量的真实提交仍属后续单独授权。

## 9. E3 入口（已完成）与 E4 入口

E3 已按授权执行完毕，`docs/M8_CONTRACTS.md` §9 的 E3 行已标记完成。下一步 **E4**：

1. `scripts/evaluate_m8.py` 离线 3–4 用例与 `tests/test_m8_evaluation.py`；
2. 全量回归、Ruff、前端八个状态文件、受控浏览器（报价对比 + 账单）；
3. 安装态 wheel smoke（M8 段）与四组 CI；
4. `docs/M8_ACCEPTANCE.md`（链接本文件与 `docs/M8_E0_BASELINE.json`）与 **v0.6.0** 发布；
5. E4 不新增付费样本；Kling/fal 若届时仍无 Key，保持「未做真实样本」标注。
