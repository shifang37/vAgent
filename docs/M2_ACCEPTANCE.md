# M2 端到端验收：离线套件、受控联调与打包（D4）

完成日期：**2026-10-17**。基线：D3 提交 `8213366`（codex/m2-d1；D4 延续该分支）。契约与范围见 [M2 契约](./M2_CONTRACTS.md) 与 [M2 任务规划](./M2_PLAN.md)；官方接口证据见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 9 节；前序验收见 [D1](./M2_D1_ACCEPTANCE.md)、[D2](./M2_D2_ACCEPTANCE.md) 与 [D3](./M2_D3_ACCEPTANCE.md) 验收。

**离线部分全程零费用：视频 submit=0（上游为受控 HTTP），未发起真实模型或视频调用，未使用真实凭证或素材。** 受控上游由本地合成参考图与提交的测试 MP4 驱动，登记、集合等待、本地交付、逐镜头重做、费用护栏与拒绝路径都走真实应用代码路径。真实成本受控联调见第 7 节。

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| `scripts/evaluate_m2.py`：默认离线端到端套件；`--live` 走受控额度的真实双镜头加一次重做 | 完成 |
| `tests/test_m2_evaluation.py`：套件回归、评分器反例、显式续跑与预算边界 | 完成 |
| wheel smoke 的 M2 扩展：离线套件、分镜/素材 CLI 与 HTTP 面 | 完成 |
| 受控浏览器证据复跑（输出前缀 `m2-d4-browser`，不覆盖 D3 证据） | 完成 |
| 四组 CI | 完成（`fb3b6d4`，[运行 39015717826](https://github.com/shifang37/vAgent/actions/runs/39015717826)） |
| `docs/M2_ACCEPTANCE.md`、README、M1_PLAN、Harness 设计与各验收文档同步 | 完成 |
| 真实成本受控多镜头联调（2 镜头 + 1 次单镜头重做，submit≤3、估算≤9.00 CNY） | 完成（submit=3、估算 9.00 CNY；第 7 节） |

## 2. 离线端到端套件（`scripts/evaluate_m2.py`）

设计：**live 模式 + 受控上游 + 本地测试媒体 + 加速的夹具时钟**。应用以 `video_mode="live"` 打开，`WanAdapter` 与 `MediaHttpClient` 由 `httpx.MockTransport` 接管（冻结的 `wan-http-v2` 协议：提交体、`input.media`、查询状态映射、`usage`），下发的 MP4 为提交的本地夹具；夹具时钟只跳到已持久化的截止时间，因此不改任何冻结策略值（15 秒查询、600 秒等待、每秒一次的 Worker 调用限流）。脚本模型从对话记录推导决策，显式续跑可重复。

七个用例（每个独立断言，任一失败即停止并保留报告）：

| 用例 | 期望 | 关键断言 |
|---|---|---|
| `storyboard-draft` | 只保存分镜，不登记 | 3 个有序镜头、稳定 shotId、i2v/flf2v 参考图来自素材库、`sourceRefs` 保留 brief v1、逐镜头与合计估算回显、无 Job 无提交 |
| `batch-delivered` | 一次登记 N 镜头并逐镜头本地交付 | 3 个 JobV3（`shotRef` 对齐）、每 Job 估算 3.00、额度快照（4 / 12.00 / 已用 3 / 剩余 3.00）、同参数重放复用原 Job、集合等待一次登记、3 个 MP4 与夹具哈希一致、媒体接口 GET/HEAD/206/416/附件名/ETag 全通过、i2v 提交体为 Base64 `first_frame` 且不发送 `ratio`、无签名或工作空间标识泄漏 |
| `partial-failure` | 单镜头失败不扩散 | 上游第 2 个任务 FAILED（`PROVIDER_GENERATION_FAILED`），另外 2 个交付；集合等待摘要 `failed=1/succeeded=2`；回复引用失败 jobId 与错误码；该镜头由用户重做后交付 |
| `redo-target` | 重做只影响目标镜头 | 目标镜头 +1 版本 +1 Job，其他镜头的版本、Job 身份（revision、任务 ID、提交次数、媒体 ID）与已交付媒体的文件哈希逐一不变；同请求重放返回同一结果且不新增 Job；过期 revision 返回 `VERSION_CONFLICT` 且状态不变 |
| `over-limit` | 三重上限整批拒绝 | 5 镜头 → `VIDEO_RUN_JOB_LIMIT`；6 秒镜头 → `VIDEO_COST_LIMIT`；两次都未登记任何 Job、未提交、未新增 ShotVersion；回复说明 3.00/4/12.00 上限 |
| `unsupported` | 未核实能力明确拒绝 | 30 秒、4K、1:1 与镜头延长均被指出不可用并请求确认；无登记、无提交 |
| `needs-confirmation` | 未确认不登记 | `confirm=false` 返回 `CONFIRMATION_REQUIRED`，无 Job、无提交、无 ShotVersion |

一次通过的套件合计：31 次模型调用、27 次工具调用、10 次提交、10 次查询、9 次下载；9 个镜头本地交付、2 次逐镜头重做；登记估算合计 30.00 CNY（全部离线，真实花费 0 元）；模型用量由脚本模型产生，全部为未知 Token（离线夹具不衡量模型用量）。复现：

```bash
python scripts/evaluate_m2.py --output output/m2-d4-offline.json
```

## 3. 安装态 wheel smoke 扩展

`scripts/wheel_smoke.py` 新增两段安装态检查（仓库外、`-I`、无供应商请求）：

- `smoke_m2_workbench`：安装版应用保存分镜、导入素材、以客户端动作批量登记两个镜头，再核对 HTTP 面（`/api/storyboards`、`/api/storyboards/{id}`、`/estimate`、`/api/assets`、素材内容与 Range、404）与 CLI 面（`storyboard show`、`shots redo` 含同请求重放、`assets list|import` 含同哈希去重、`usage --project`），并断言实例锁释放。
- `smoke_m2_evaluation`：以 `-I` 在仓库外运行安装态的 `scripts/evaluate_m2.py`，断言 `passed`、7/7 用例与提交/交付计数。

安装态完整 smoke 结果：**PASS** —— `PASS: installed wheel, Web assets, config, streaming, text/quality, MCP, mock video tools, durable wait, original Run restart/automatic continuation, readonly delivery, unchanged budgets, CLI list/get/usage, five M1-B cases, live protocol with local media restart and CLI media evidence, seven M1-C cases, M2 storyboard/asset CLI and HTTP surface, seven M2 cases, cleanup.` 证据：`output/m2-d4-wheel-smoke.json`（含 `m2Workbench` 与 `m2Evaluation` 明细：分镜 ID、镜头 ID、素材 ID、批量与重做的 jobId、各项布尔检查）。

## 4. 测试与门禁

- 离线全量：在本机（Windows 11、工作环境 Python 3.11）运行完整 pytest 套件 **943 passed / 2 skipped**（405 秒；D3 基线 917 passed / 2 skipped；D4 新增 26 项，全部来自 `tests/test_m2_evaluation.py`）。两项跳过仍为 Windows 账户无符号链接权限的既有跳过。
- `ruff check` 与 `ruff format --check` 覆盖 `src/vagent tests scripts`；`node --check` 三个前端脚本；`node --test tests/test_job_state.mjs`（10 项）与 `tests/test_storyboard_state.mjs`（11 项）通过。
- 兼容证据（D1–D3 冻结）：`tests/test_m2_compatibility.py` 的 pre-M2 签名逐字节用例、等待与恢复套件、媒体与评测套件全部通过；D4 未改动任何工具签名、版本号或冻结数值（仅新增脚本与测试）。

## 5. 受控浏览器证据

D4 复跑 D3 的受控浏览器夹具，输出前缀改为 `m2-d4-browser`，因此 D3 证据（`output/m2-d3-browser-*`）保持不变。夹具 `tmp/m2-d4-browser/launcher.py` 以 live 模式、受控 Wan 传输、本地样例 MP4、加速时钟与注入的离线模型提供三个镜头的分镜工作台；`check.mjs` 用 Playwright 驱动七个阶段：分镜面板与镜头卡、批量估算与显式确认、交付与播放、序列连播、逐镜头重做、素材库导入与预览、用量面板，最后核对零页面错误。

结果：**7/7 阶段通过，0 失败**（Chrome 155.0.8059.84，headless，1440×1000），接口无 4xx/5xx、无页面错误、无控制台错误、无失败请求。确认对话框回执「已登记 3 个镜头任务」；重做回执说明目标镜头已重做且「其他镜头未受影响」；连播读取到本地 MP4 的 1280×720 / 5 秒。证据：`output/m2-d4-browser.json`（阶段、空失败列表、浏览器版本、夹具与素材 ID、连播媒体尺寸）与 7 张截图 `output/m2-d4-browser-{storyboard,confirm,delivered,sequence,redo,assets,usage}.png`。

## 6. 四组 CI

四组 CI（ubuntu/windows × Python 3.11/3.12，`.github/workflows/ci.yml`「Agent checks」）在 D4 收尾提交 **`fb3b6d4`** 上全部完成，结论均为 **success**（[运行 39015717826](https://github.com/shifang37/vAgent/actions/runs/39015717826)）：check runs `117347306230`（ubuntu 3.11）、`117347306335`（ubuntu 3.12）、`117347306428`（windows 3.11）、`117347306487`（windows 3.12）；汇总证据保存在 `output/m2-d4-ci-fb3b6d4-summary.json`。该提交的树包含全部 D4 代码与文档（`4cd8ddb` 新增套件与安装态面、`1079438` 文档、`fb3b6d4` 重做证据收紧）。

该轮 CI 依次执行 ruff check、node 语法检查与 `node --test`（两个前端状态测试文件均为 0 跳过）、pytest、wheel 构建与安装态 `scripts/wheel_smoke.py`。安装态 smoke 输出：

```text
PASS: installed wheel, Web assets, config, streaming, text/quality, MCP, mock video tools, durable wait, original Run restart/automatic continuation, readonly delivery, unchanged budgets, CLI list/get/usage, five M1-B cases, live protocol with local media restart and CLI media evidence, seven M1-C cases, M2 storyboard/asset CLI and HTTP surface, seven M2 cases, cleanup.
```

套件结果：ubuntu 3.11 **942 passed / 3 skipped**（266.61 秒）、ubuntu 3.12 **942 passed / 3 skipped**（329.22 秒）、windows 3.11 **944 passed / 1 skipped**（851.29 秒）、windows 3.12 **944 passed / 1 skipped**（775.91 秒）。平台间差异（本机 Windows 2 跳过、CI Windows 1 跳过、ubuntu 3 跳过）与 D1–D3 同因，仍为既有的符号链接权限类跳过与 Windows 专用控制台夹具（`skipif(sys.platform != "win32")`），不是 D4 引入。

D4 没有使用 CI 试错：离线全量套件、ruff、前端检查、安装态 wheel smoke 与受控浏览器验证都在推送前于本机完成（见第 4、5 节），首轮推送 `1079438` 的四组 CI 同样全部通过（ubuntu 941/3、windows 943/1，安装态 smoke PASS）。随后为「重做证据不再包含恒空字段」追加 `fb3b6d4`（第 8 节第 6 条），本节记录的是该提交上的最终四组结果。其后的文档提交（本条记录与 README、M1_PLAN、M2_PLAN、Harness 设计的同步）不改动代码。

## 7. 真实成本受控多镜头联调（`--live`）

完成日期：**2026-10-17**。形态与额度按契约第 5 节冻结执行：两镜头分镜、**一次**批量登记、**一次**集合等待、逐镜头本地交付，随后由用户显式发起**一次单镜头重做**；实际上限 submit=**3**、估算 **9.00 CNY**（每 Job 3.00）。i2v、首尾帧与镜头延长未做真实样本（见第 9 节）。

### 7.1 零费用预检（paidCalls=0）

- DeepSeek `GET /models` → **200**，返回 `deepseek-flash`、`deepseek-v4-pro`，配置模型可用。
- 对不存在的任务 ID 查询北京业务空间：**HTTP 200**，原文 `{"output":{"task_id":"…","task_status":"UNKNOWN"}}`（原始响应存 `output/m2-d4-live-probe.json`）；适配器按冻结口径把 `UNKNOWN` 归类为 `PROVIDER_TASK_UNAVAILABLE` 并暂停查询（不会重试到永久）。两者共同证明 Key、地域、业务空间专属域名与鉴权有效，且未创建任务、未计费。
- 证据：`output/m2-d4-live-preflight.json`、`output/m2-d4-live-probe.json`。

### 7.2 真实联调结果（1 个用例，37 项检查全部通过）

- 发起方式：仓库外安装环境（`tmp/m2-d1-wheel-env/`，`-I` 保证导入 site-packages），独立数据目录 `tmp/m2-d4-live/home-live-r1`，会话模型为真实 `deepseek-flash`；套件上限 16 次模型调用。
- 模型链：`video_capabilities → project_read → storyboard_save → shot_generate → await_job`；**5 次真实模型调用**，38,958 输入 / 1,333 输出 Token，缓存命中 3,584 / 未命中 35,374，用量覆盖完整；活动 8.47 秒、外部等待 140.75 秒，均在原预算内。
- 登记：一次调用登记 **2 个镜头 Job**（`usedNewJobs=2`、合计估算 **6.00**、`remainingAmount=6.00`，上限仍为每 Job 3.00 / 每 Run 4 个 / 每 Run 12.00）；三个 Job 请求的冻结参数逐字段一致（`720P`、`ratio=16:9`、`duration=5`、`prompt_extend=false`、`watermark=true`、`seed=0`），工作空间作用域已记录。
- 云端与交付：**3 次提交**（两镜头 + 一次重做，全部一次即成）、27 次查询、3 次下载（各 1 次尝试即提交）。真实 task ID：`e971a41c-ea5c-41bd-9fd3-b4ef3815df0e`、`d2f6c842-9c26-4907-b9a1-761d8ab0e9f2`、`ee2bf289-703a-498c-9de7-12845718109a`；结果主机均为 `dashscope-a717.oss-accelerate.aliyuncs.com`，落在 `wan-result-hosts-v2` 允许集内，**不需要追加策略版本**。
- 逐镜头交付：三个 MP4 全部 1280×720 / 5.033 秒 / H.264 + AAC / 30fps，大小 1,926,942 / 2,544,082 / 1,960,856 字节，SHA-256 `37e3ff76d48cde8e54807fc58b3ec3093ac44ca7a56daf10a1845c8501612d4d`、`1c2d84f03a429f68f83229c68f5933d6a27b1fede2fa5f36066e499fe8239062`、`031bc03570393293f33f7d465b99ffa66184c8ee88c12310797e6db9365d8991`；本地文件与记录哈希一致，媒体接口（详情 / GET / HEAD / 单 Range 206 / 不可满足 416 / 附件名 / ETag）全部通过。
- 上游 usage：`duration=5.0`、`output_video_duration=5.0`、`input_video_duration=0.0`、`video_count=1`、`resolution=720P`、`aspectRatio=16:9`，与冻结口径一致。
- 逐镜头重做：目标镜头新增 1 个 ShotVersion 与 1 个 Job；其他镜头的版本增量 0、Job 身份（revision / 任务 ID / 提交次数 / 媒体 ID）与已交付媒体文件哈希逐一不变；同请求重放返回同一结果且不新增 Job。
- 浏览器（Chrome headless，安装版应用以 live 模式打开该数据目录）：分镜面板显示版本数 [2, 1]（重做镜头两个版本）；序列连播读取到真实交付的媒体（1280×720 / 5.04 秒）并实际播放；每个交付媒体经内容接口返回 200 `video/mp4`、字节与记录哈希一致、`attachment; filename="<mediaId>.mp4"`；零页面错误。证据：`output/m2-d4-live-browser.json`、`output/m2-d4-live-browser.png`。
- 费用：登记估算合计 **9.00 CNY**（3 × 3.00，价格版本 `wan27-beijing-720p-20261010`）；**实际账单未取得，保持 unknown**，免费额度是否抵扣未知。
- 完整报告：`output/m2-d4-live.json`（37 项检查全部通过，含 `redo_left_other_media_untouched`、`media_matches_local_file`、`submit_accounted`、`cost_recorded`、`quota_snapshot`、`set_wait_delivered`）。

## 8. 失败与偏差记录

开发过程中由新用例与本机门禁暴露并修正的缺陷：

1. **夹具时钟与 Worker 调用限流冲突（评测脚本缺陷）**：live Worker 对每个 Store 每秒只允许一次供应商调用（上游 RPS 保护）。只跳到 Job 截止时间的夹具时钟在第二个镜头提交前停住（时钟已等于最早截止时间，无法再前进），表现为批次永远停留在「已提交 2 个、查询 0 次」。修正 `advance_clock`：仅在仍有在途 Job 时，若没有可跳的截止时间或最早截止时间不晚于当前值，就前进 1 秒越过限流门。
2. **评分器混用运行期与重做期证据（评测脚本缺陷）**：重做发生在 Run 结束之后，若把重做 Job 计入批次断言，则「镜头数」「提交数」与「回复引用 jobId」都会误判。修正为在 Run 结束点冻结 `runJobs`/`runUpstream`，批次与额度断言只用它们，交付与重做断言用最终状态。
3. **i2v 与 t2v 的报价版本不同（评测脚本缺陷）**：逐镜头断言曾固定 t2v 的 `wan27-beijing-720p-20261010`，图片模式应为 `wan27-i2v-beijing-720p-20261016`。
4. **确认用例未真正不确认（夹具缺陷）**：`needs-confirmation` 用例漏设 `confirm=false`，脚本模型在「未确认却被接受」时会因缺少 `error` 字段抛错，掩盖真实结论。已修正并用例断言 `confirm` 从未为真。
5. **无分镜项目的读取（评测脚本缺陷）**：`capture_state` 曾以「整个数据目录存在分镜」为条件读取当前项目分镜，在拒绝类用例上抛 `NOT_FOUND`。改为按项目捕获并对 `NOT_FOUND` 记 `None`。
6. **重做证据中的一个恒空字段（评测脚本缺陷，交付前自查发现）**：重做的「其他镜头不动」比较曾包含 `MediaRef.sha256`，但 `MediaRef` 只携带 `mediaId`，该键恒为 `None`，使这项比较看起来比实际更强。修正为只保留真实身份字段，并新增独立断言「重做前后已交付媒体文件哈希逐一相等」（`redo_left_other_media_untouched`），同时补一个对应的评分器反例用例。该修正以独立提交 `fb3b6d4` 追加，最终 CI 记录在该提交上。

## 9. 未做真实样本的模式与限制

- **i2v、首尾帧与镜头延长未做真实样本**：能力条目已按 D0 核实冻结，离线与受控 HTTP 已覆盖请求体、参考图校验与错误路径；真实授权、真实结果主机与画幅表现仍未验证。镜头延长在 M2 裁剪（上游仅接受 URL 且上传通道「勿用于生产环境」）。
- **实际账单保持 unknown**：估算来自冻结价格表，未取得扣费凭据；免费额度是否抵扣未知。
- **M2 不做拼接**：交付形态为逐镜头本地 MP4 加客户端序列连播，最终单文件合成属于 M3。
- 浏览器以外的播放设备、上游断点续传、多区域 CDN 未覆盖。
