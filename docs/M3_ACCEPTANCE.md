# M3 端到端验收：离线套件、安装态、浏览器与打包（E4）

完成日期：**2026-10-21**。基线：E3 提交 `a4ae2b3`（M3 schema 5、tools/rules 6/6、headless 与用户 skills 已交付）；最终代码提交 **`482d05d`**（文档同步另计）。范围与门槛见 [M3 任务规划](./M3_PLAN.md) 的 E4，冻结契约见 [M3 契约](./M3_CONTRACTS.md)。**E4 未执行任何新增付费样本，视频 submit=0、真实供应商请求=0；Kling/fal 真实账号与账单仍保持「未做真实样本」。** 唯一真实媒体证据是**零新增费用**复用 M2 已交付的三个真实万相 MP4 做本地拼接。

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| `scripts/evaluate_m3.py`：默认离线的「一句话→多镜头→本地拼接成片」套件（受控 Kling/fal/Wan + 本地测试 MP4 + 包内 FFmpeg + headless） | 完成，6/6 |
| `tests/test_m3_evaluation.py`：套件回归、评分器反例、预算/续跑边界与 `film_assemble` 执行上下文回归 | 完成，31 项 |
| wheel smoke 的 M3 扩展：安装态成片工作台（HTTP/CLI/重放/多供应商设置）与安装态 6 用例套件 | 完成，PASS |
| 受控浏览器验收：成片拼接、播放/拖动/下载、重做后 v2、历史版本、Kling/fal 设置、用量分桶 | 完成，7/7 阶段 0 失败 |
| 零新增费用真实数据验收：M2 真实分镜 + 三个真实万相镜头，安装态 `vagent film assemble` | 完成，42 项检查全通过 |
| 四组 CI（ubuntu/windows × Python 3.11/3.12） | 完成（第 6 节） |
| `docs/M3_ACCEPTANCE.md`、README、M1_PLAN、M3_PLAN、Harness 设计与 E1–E3 验收同步 | 完成 |
| 新增付费真实样本（Kling/fal 真实账号、真实新提交） | **未执行**：未获得模型/次数/金额的显式授权（决策点 3）；相关供应商保持未验证标注 |

## 2. 离线端到端套件（`scripts/evaluate_m3.py`）

### 2.1 设计

应用以 `video_mode="live"` 打开，`KlingAdapter`、`FalAdapter`、`WanAdapter` 与 `MediaHttpClient` 全部由 `httpx.MockTransport` 接管；每个用例使用加速的夹具时钟，只跳到已持久化的截止时间与 Worker 的一秒限流门，因此登记、提交、查询、下载、集合等待、FFmpeg 拼接、逐镜头重做、headless 计划/授权/执行都走真实生产代码路径。视频来自提交的 `tests/fixtures/m1c/sample-720p.mp4`，无 DNS/TLS、无账号、无真实凭证、无真实提交。

**`--live` 在本包明确拒绝执行**：`EVAL_LIVE_UNAVAILABLE`，未发起任何请求；新增付费样本必须另行批准。

### 2.2 六个用例

| 用例 | 形态 | 关键断言 |
|---|---|---|
| `kling-film` | 一句话 Agent Run | 工具链 `video_capabilities→storyboard_save→shot_generate→await_job→storyboard_read→film_assemble`；2 个 Kling JobV6 各提交一次；请求体仅 `prompt/settings/options`、`settings={duration:5,resolution:720p,aspect_ratio:16:9}`、`external_task_id` 以 `vag-` 开头；USD 0.56/镜头、合计 1.12、8 VIDEO_UNIT；成片 v1 两个输入按镜头顺序；v1 媒体分支、时间线、HTTP 与 `film_read` 全通过；随后用户显式重做镜头一，重拼得到 v2，v1 文件与媒体不变、其他镜头 Job/媒体不变、同请求重放幂等、过期 revision 返回 `VERSION_CONFLICT` |
| `fal-film` | 一句话 Agent Run | fal `queue.fal.run` 收 `duration:"5"`、`generate_audio:false`、`shot_type:"customize"`、`enable_safety_checker:true`、`cfg_scale:0.5`、固定负向提示；USD 0.42/镜头、合计 0.84；结果从 app-root 路径读取、下载主机 `v3.fal.media`；成片 v1 与同输入重放 |
| `wan-film` | 一句话 Agent Run | 既有 Wan 路径仍产出成片：CNY 3.00/镜头、合计 6.00、共享 Job 上限 2；`720P/16:9/5/prompt_extend=false/watermark=true/seed=0`；成片 v1 与同输入重放 |
| `film-refusals` | 程序化生产路径 | 先交付两镜头并成片；同输入重放不新增版本/文件；过期 revision→`VERSION_CONFLICT`；截断一个输入文件→`FILM_MEDIA_INVALID`；修改分镜新增未交付镜头→`FILM_INPUT_MISSING`；全程成片仍为 v1、无 `.part`、无额外提交 |
| `headless-plan` | `vagent headless` 计划阶段 | 仅保存分镜并产生 `planned`；`jobIds=[]`、`output=null`；计划指纹 64 位；估算 Kling 1.12 USD/8 units；规划工具集不含 `shot_generate`/`video_generate`/`film_assemble`；Job=0、媒体=0、提交=0 |
| `headless-deliver` | 显式授权执行 | 授权后登记 2 个 Job、集合等待并本地拼接为 v1；输出路径/大小/SHA-256 与文件一致；重复 `execute` 返回同一 output/film 且不重新提交；结束后 2 Job、3 媒体（2 镜头 + 1 成片） |

### 2.3 结果与复现

一次通过：**6/6 passed**，21 次脚本模型调用、18 次工具调用、11 次受控提交、11 次受控下载、11 个镜头本地交付、6 个成片版本、1 次逐镜头重做；`realProviderCalls=0`，脚本模型 Token 用量全部未知（不把未知记作零）。报告：`output/m3-e4-offline.json`。

```bash
python scripts/evaluate_m3.py --home .vagent/evaluations/m3/<new> --output output/m3-e4-offline.json
```

评分器对 22 类伪造/不一致证据逐项反例测试（声明成片但回复无版本、少记提交、Kling 请求体被改、fal 漏音频开关、Wan 参数漂移、成片哈希/HTTP Range 造假、文本侧被改、重做影响其他 Job、旧成片被覆盖、过期 revision 被接受、拒绝码被替换、计划阶段登记 Job、headless 重复提交等），任一被评分器放行即测试失败。

## 3. 安装态 wheel smoke

`scripts/wheel_smoke.py` 新增两段（安装态、仓库外临时目录、`-I`、无供应商请求）：

- `smoke_m3_film`：安装版应用保存两镜头分镜、以客户端动作批量登记、受控 Wan 下载本地 MP4，再以 `expectedRevision` 本地拼接；断言成片 v1 两输入、1280×720/H.264/AAC、`10.021289` 秒、418,395 字节、SHA-256 `41be1ed9…fd8a`（与 E0/E1 夹具修正变体一致）；成片媒体详情/GET/HEAD/Range/416/附件名；`vagent film show` 与同请求 `film assemble` 重放；Kling/fal Key 保存后 `keyConfigured=true` 且 `permissionStatus="unverified"`，Key 不出现在配置投影；实例锁释放。2 次受控提交、2 次下载。
- `smoke_m3_evaluation`：以 `-I` 在仓库外运行安装态 `scripts/evaluate_m3.py`，断言 `passed`、6/6、11 次提交、6 个成片版本。

完整 smoke：**PASS**（`output/m3-e4-wheel-smoke.json`；`m3Film` 与 `m3Evaluation` 均含明细）：

```text
PASS: installed wheel, Web assets, config, streaming, text/quality, MCP, mock video tools, durable wait, original Run restart/automatic continuation, readonly delivery, unchanged budgets, CLI list/get/usage, five M1-B cases, live protocol with local media restart and CLI media evidence, seven M1-C cases, M2 storyboard/asset CLI and HTTP surface, seven M2 cases, M3 local film assembly with film media HTTP/CLI replay and provider settings, six M3 cases, keys-only live shared-host fixture with zero-cost save probe and hot registration, cleanup.
```

## 4. 受控浏览器验收

夹具 `tmp/m3-e4-browser/launcher.py` 以 live 模式、受控 Wan 传输、本地样例 MP4 与加速时钟提供两镜头工作台；`check.mjs` 用 Playwright 驱动 7 个阶段（Chrome 155.0.8059.85，headless，1440×1000）：

1. 分镜面板与两镜头；
2. 批量确认（合计 6.00、剩余额度）与本地交付；
3. 显式「拼接成片」→ `<video>` 读取 1280×720 / 10.02 秒、播放、拖动、受控媒体 URL、下载链接、v1 当前；
4. 重做镜头一后「重新拼接成片」→ v2 当前、v1 历史、播放器仍可解码；
5. 设置页选择 Kling 保存 Key → 「鉴权/模型权限未验证」；切换 fal 保存 → 同样标注；两个 Key 均不回显到页面；
6. 用量面板：三个任务合计 9.00 CNY、`wan` 分桶、未取得账单、分镜统计；
7. 零页面错误/控制台错误/失败请求/4xx-5xx。

结果：**7/7 阶段通过，0 失败**。证据：`output/m3-e4-browser.json`、截图 `output/m3-e4-browser-{storyboard,delivered,film,film-v2,settings-kling,settings-fal,usage}.png`。

## 5. 零新增费用真实数据验收

### 5.1 数据与方式

复制 M2 真实数据目录 `tmp/m2-d4-live/home-live-r1`（schema 4，项目 `live-multi-shot`，分镜 `16331187-40eb-4405-b8f3-f3ec16443f21`，revision 2）到 `tmp/m3-e4-real/home`，删除实例锁；使用**安装态 wheel**、`VAGENT_VIDEO_MODE=off`、真实项目与分镜 ID 执行 `vagent film assemble`。该分镜的三个真实万相镜头：原始两镜头 + 镜头一的一次真实重做；最新镜头版本选中重做镜头 `69998ede…` 与镜头二 `0b35a607…`。全程无供应商调用、无新提交、无新增费用。

### 5.2 结果（42 项检查全部通过）

- **迁移与保留**：schema 4→5，原始字节备份 `state-v4-96662069-6178-436e-acda-24bc543f8065.json` 与迁移前一致；`jobs`、`storyboards`、`waits`、`projects`、`artifacts`、`assets` 逐字节不变；三个真实媒体记录与文件哈希不变；旧 Run/Operation 保留；新增一个 `local-client` 成片 Run 与成片 Operation。
- **成片 v1**：`filmId` 记录于 `output/m3-e4-real-verify.json`；4,501,422 字节、SHA-256 `b3bd32ce8b5210b2878b5ae8bf4dfaf8d42b2e946c3b079eacfa84af03207df2`、1280×720/H.264/AAC、10.096549 秒；两个输入按镜头顺序引用最新 ShotVersion；时间线 5.06322 + 5.06322 秒、接缝连续；引擎 `imageio-ffmpeg 0.6.0`、Windows 二进制 SHA-256 `2ce797a0…71a3`。
- **媒体分支**：`kind="film"`、`contractVersion=2`、`validationVersion="mp4-avc-v1"`，`filmId/filmVersion` 与版本一致，文件大小/哈希与版本一致。
- **HTTP**：`/api/films/{storyboard}?projectId=…` 返回该成片；媒体详情、GET、HEAD、单 Range 206、不可满足 416、附件名、ETag 全部通过。
- **真实重放**：同一 `--request-id` 再次执行返回同一 `runId` 与同一 `outputMediaId`（`output/m3-e4-real-replay.json`），不新增版本或文件。
- **浏览器解码**：Chrome headless 通过 `/api/media/{id}/content` 解码真实成片，`videoWidth/Height=1280/720`、媒体时长 10.12644 秒、播放至 7.50 秒，零页面错误；证据 `output/m3-e4-real-browser.json` 与截图 `output/m3-e4-real-browser.png`。

### 5.3 真实数据发现并修正的兼容性缺陷

三个真实万相 MP4 的 AAC `esds` 在 `maxBitrate/avgBitrate` 描述字段上不同（`f42b`/`f4b9`/`f2e7`），但 `AudioSpecificConfig`（`12 10 56 e5 00`）、采样率、声道与 `avcC` 完全一致。E1 的 `mp4-copy-signature-v1` 原本对整段 `esds` 逐字节比较，导致真实成片被错误拒绝为 `FILM_INPUT_INCOMPATIBLE`。E4 按契约字段把签名归一为 `objectTypeIndication`、`streamType` 与 `AudioSpecificConfig`（不含码率/SL 描述），保留采样率、声道、`avcC`、时间线与 priming 的严格比较；版本名仍为 `mp4-copy-signature-v1`，因为它从未在仓库外发布，且契约字段清单本就不含码率描述。修复后三个真实镜头兼容并可拼接；本地样例（48 kHz，`1188…`）与真实镜头（44.1 kHz，`1210…`）仍被正确拒绝。

## 6. 四组 CI

四组 CI（ubuntu/windows × Python 3.11/3.12，`.github/workflows/ci.yml`「Agent checks」）在最终代码提交 **`482d05d`** 上全部完成，结论均为 **success**（[运行 39614222544](https://github.com/shifang37/vAgent/actions/runs/39614222544)）：check runs `119270813839`（ubuntu 3.11）、`119270813896`（ubuntu 3.12）、`119270813715`（windows 3.11）、`119270813159`（windows 3.12）；汇总证据保存为 `output/m3-e4-ci-482d05d-summary.json`。该提交树包含全部 E4 代码、测试与 wheel smoke 扩展；文档提交在其后，不改动代码。

该轮 CI 依次执行 ruff check/format、四个前端脚本语法检查与三组 `node --test`（31 项，0 跳过）、pytest、wheel 构建、仓库外安装校验与 `scripts/wheel_smoke.py`。结果：

- ubuntu 3.11 / 3.12：**1117 passed / 3 skipped**，安装态 wheel smoke **PASS**（含 M3 成片工作台与 6 用例）。
- windows 3.11 / 3.12：**1119 passed / 1 skipped**，安装态 wheel smoke **PASS**（含 M3 成片工作台与 6 用例）。
- 平台差异与 M1–M3 历史一致，来自 Windows 专用控制台夹具与符号链接权限类跳过，不是 E4 引入。

## 7. 测试与门禁

- 离线全量：本机 Python 3.12.14 运行 `pytest -q`，**1117 passed / 3 skipped**（471.47 秒；E3 基线 1083/3；E4 新增 34 项：`tests/test_m3_evaluation.py` 31 项、`tests/test_web.py` 1 项、`tests/test_m3_assembler.py` 2 项）。3 项跳过仍为 Windows 账户无符号链接权限与平台专用夹具的既有边界。
- `ruff check src/vagent tests scripts` 与 `ruff format --check`：通过（148 个文件已格式化）。
- 前端：`node --check` 四个脚本；`node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs` **31 项**通过、0 失败。
- 安装态 wheel：构建后 `scripts/wheel_smoke.py` PASS（第 3 节）；受控浏览器 7/7（第 4 节）；真实数据 42 项检查（第 5 节）。
- 本轮未用真实付费模型；脚本模型与受控上游的 submit 计数只用于代码路径证据。

## 8. 失败与偏差记录

1. **`film_assemble` 无法被 Agent 调用（产品缺陷，E4 端到端首次暴露）**：`ToolRegistry.requires_context()` 只检查 `context_execute`，而 `film_assemble` 是 `async_write_execute`，Runner 因此传入 `None` 上下文，工具返回 `TOOL_CONTEXT_INVALID`。修复为两类执行器都需要服务端上下文；新增回归测试，`kling-film` 的完整 Agent Run 现已通过。无版本号变化（仅运行时上下文判定）。
2. **Web 未提供 `film-state.js`（产品缺陷）**：`web.py` 静态资源允许集漏掉 E1 新增的 `film-state.js`，`app.js` 的模块导入 404，整个工作台无法启动。修复允许集；新增测试逐个请求 `app.js` 实际导入的模块；wheel smoke 的资源检查扩展到全部前端文件。受控浏览器因此从 9 个失败恢复到 7/7。
3. **设置页切换供应商后 Key 输入仍禁用（产品缺陷）**：输入框按启动供应商（wan，来源 `provided`）渲染为禁用，切换 Kling/fal 时只更新占位符而不重新计算 `disabled`。修复切换处理，按目标供应商的 `editable` 与 busy 重新设置；浏览器验证 Kling 与 fal 均能保存并显示「未验证」。
4. **AAC `esds` 码率描述被当作拼接配置（产品缺陷，真实数据验收暴露）**：见第 5.3 节；按契约归一化后真实数据成片成功，并新增“码率不同仍兼容、AudioSpecificConfig 不同仍拒绝”的测试。
5. **旧沙箱 ACL 阻止读取 M2 数据目录（环境问题）**：`tmp/m2-d4-live/home-live-r1` 与 `.vagent/` 曾由旧沙箱账户写入，当前受限进程无权读取；按随附 Windows 权限诊断脚本检查后判定 `NOT_THIS_CLASS` 并只补了 `tmp`/`tmp/m2-d4-live` 的写权限，未能解除读取限制。会话随后切到完全访问，复制数据后完成验收；未修改产品代码或真实数据。
6. **新增付费样本未执行**：`--live` 明确拒绝；这不是失败完成，而是按决策点 3 保留的范围裁剪。Kling/fal 真实账号鉴权、地域可达性、真实编码、CDN 与账单仍为未知；headless 的真实编排模型计划也未执行（只验证脚本编排与受控供应商）。
7. **真实成片的实际账单仍未知**：本次拼接只使用 M2 已支付生成的真实文件，M3 未产生新费用；既有三个镜头的估算各 3.00 CNY、实际扣费与资源包抵扣仍未知。
8. **首轮 CI 的 Windows 超时（打包缺陷）**：首轮代码提交 `b960ebc` 的 ubuntu 两组通过、windows 两组失败；日志显示安装态 wheel smoke 里 `evaluate_m3.py` 子进程被 `run_command` 的 120 秒上限终止（该 Windows 运行器执行 6 用例明显超过 120 秒）。修复为 M3 安装态套件单独 `timeout=600`，`482d05d` 四组全部通过；未改动产品行为。

## 9. 未做真实样本与后续限制

- Kling `kling-3.0-turbo` 与 fal `kling-video/v3/standard/text-to-video` 未做真实账号样本，能力表只能称「文档支持，未做真实样本」；其真实结果主机、编码与账单未验证。
- headless 的真实 DeepSeek 计划、真实付费授权路径未执行；M3 E3 的受控供应商与脚本编排证据继续有效，但不等于账号已验证。
- 真实成片只覆盖万相真实输出、本地流复制与浏览器解码；Kling/fal 输出之间的兼容性未验证，继续由 `mp4-copy-signature-v1` 逐字段拒绝不同配置。
- 公共 PyPI 发布仍独立于本地 wheel 验收；可执行插件与插件市场继续顺延。
