# M5 E4 验收：分集工作流、安装态、浏览器与真实样本

日期：**2026-10-22**。基线 `master` `d98ddc2`（M5 E3，schema 6、tools/rules 9/9、冻结 8/8）；最终代码提交见 `git log`。范围见 [M5 计划](./M5_PLAN.md) §3.2 的 E4，冻结契约见 [M5 契约](./M5_CONTRACTS.md) §10。**E4 未调用文本模型（模型调用=0）；离线套件与安装态验收的所有供应商调用均为受控 HTTP，真实视频 submit=0。** 唯一真实付费样本是经单独批准的 **18 次 Wan 文生视频提交（估算 54.00 CNY，实际账单 unknown）**，用于 `fridge-notes` 全篇 18 镜头 / 90 秒 / 7 个场记的逐场成片；未使用 Kling/fal 真实账号，未执行其他付费样本。

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 离线端到端套件 | `scripts/evaluate_m5.py` | 默认离线、`--live` 明确拒绝；5 个用例走真实生产代码路径（真实 `ApplicationService`、Agent Runner、episode/storyboard/film 工具、Job/Media Worker、包内 FFmpeg）。Wan 由 `httpx.MockTransport` 提供，媒体按每个镜头的冻结时长由本地夹具转码生成。`--only` 仅用于开发调试 |
| 套件用例 | 同上 | `wan-episode`（`fridge-notes` 18 镜头 / 7 场 / 7 条逐场成片）、`wan-i2v`（`rainy-cafe` 19 镜头 / 8 场 / 2 个角色 / i2v / 一致性证据与人工评分）、`wan-refusals`（超长原文、未绑定场记、确认、镜头数/时长不一致、缺角色素材）、`wan-cap`（20 个任务占满集级桶、下一次重做整批拒绝、零提交）、`wan-redo`（1 个上游失败隔离、两次重做、v1→v2 成片） |
| 套件评分器测试 | `tests/test_m5_evaluation.py` | 10 项：整套报告断言、按秒单价、伪造成片拒绝、拒绝矩阵、集级桶、`--live` 拒绝、预算上限、报告与数据目录不覆盖、套件身份与纯函数 |
| CLI 分集流程 | `src/vagent/cli.py`、`src/vagent/video/clients.py` | `vagent episode plan/save/show/generate`：离线改编、原子保存场记与分集、读取生成进度与逐场成片、显式 `--confirm` 整集登记（只登记、不提交）。`clients.generate_episode` 与 Web/CLI 共用同一条客户端 Run |
| Web 分集面板 | `src/vagent/application.py`、`src/vagent/web.py`、`web/app.js`、`web/index.html`、`web/episode-state.js`、`web/styles.css` | 只读面板：分集标题/版本、场景/镜头/时长、集级任务与金额、逐镜头交付进度、逐场成片播放/下载、角色一致性证据与人工评分；点击场记直接打开该场分镜面板并复用现有成片播放器。新增 `GET /api/episodes`、`GET /api/episodes/{id}`，会话快照新增 `episode` |
| 前端状态与测试 | `web/episode-state.js`、`tests/test_episode_state.mjs` | 5 项纯函数测试（逐场汇总、集级预算、空结果、场记摘要/成片标签、路径编码）；CI 新增 `node --check web/episode-state.js`、`web/film-state.js` 与 `node --test tests/test_film_state.mjs`、`tests/test_episode_state.mjs` |
| 安装态 wheel | `scripts/wheel_smoke.py` | 新增 `m5EpisodeWorkbench`（安装态 CLI `episode show`、HTTP `/api/episodes`、会话快照、面板静态资源）与 `fiveCaseM5Suite`（安装态运行 5 用例，5/5 PASS、42 次受控提交、41 个交付、18 个成片版本、2 次重做） |
| 真实样本 | 本文第 4 节、`output/m5-e4-real/` | `fridge-notes` 全篇：18 镜头 / 90 秒 / 7 场，Wan 北京 720P 5 秒；7 条逐场成片，浏览器解码与截图 |
| 验收文档 | 本文件、[M5 计划](./M5_PLAN.md)、[M5 契约](./M5_CONTRACTS.md)、[E3 验收](./M5_E3_ACCEPTANCE.md)、`README.md`、`M1_PLAN.md` | 记录交付、边界、失败与复现 |

## 2. 关键实现决策（相对计划的细化）

1. **成片口径是逐场成片，不是整集成片。** M5 契约 §2.1 明确「一个场景至多绑定一个分镜，成片仍按 `storyboardId` 拼接；一集多场的成片由 M6 负责」。E4 因此验收 7 条 10–15 秒的逐场成片，全部可播放、可下载；不新增跨场组合成片对象，也不把它说成一条 90 秒短剧。
2. **离线套件按用例隔离数据目录。** 每个用例使用 `--home/<case>` 独立数据目录。`wan-cap` 只登记 20 个任务、不推进交付（先停两个 Worker），因此不会把未提交任务留给下一个用例的真实 Worker；这也让「20 个注册 + 零提交 + 重做拒绝」成为确定性证据。早期把 5 个用例放在同一数据目录时，后一个用例的 Worker 会继续提交前一个用例遗留的 `pending_submit` 任务，导致等待超时，这是套件夹具缺陷而不是产品缺陷。
3. **真实样本用 t2v，不伪造角色参考图。** 仓库没有真实角色素材；`fridge-notes` 只有 1 个识别角色，若用 i2v 需要先准备真实参考图。E4 的真实样本只证明「真实付费提交 → 查询 → 下载 → 逐场成片 → 浏览器解码」，角色一致性由离线 `wan-i2v` 用例和 E2 证据覆盖；真实 i2v 参考图一致性仍标为未做。
4. **真实样本一次登记、零模型调用。** 分集/场记/分镜由服务端 `episode_save`/`storyboard_save` 直接写入，18 个任务通过 `ApplicationService.generate_episode` 一次显式确认登记；没有调用 DeepSeek，也没有自动重提。预算在登记前校验为 18 个任务 / 54.00 CNY。
5. **Web 面板只读。** 按 E4 决策，面板展示分集、预算、进度、成片与一致性证据；计划/保存/整集登记仍由 Agent 会话（9/9 工具）或 CLI 的显式动作完成，面板不新增写接口。
6. **`episode_read` 仍是工具的唯一读取视图。** Web 的 `ApplicationService.episode` 在工具视图上追加逐场成片的受控媒体投影（`film`），不改变工具返回结构；会话快照复用同一投影，避免面板与 API 出现两套上限。

## 3. 验证结果

### 3.1 离线套件

```text
.\.venv\Scripts\python.exe scripts/evaluate_m5.py --home <new> --output <new.json>
5/5 passed；42 次受控提交、41 个交付、62 个注册、18 个成片版本、2 次重做；真实供应商调用 0、真实模型调用 0。
```

- `wan-episode`：18 镜头全部交付，7 条逐场成片（2–3 镜头/条），集级 `usedNewJobs=18`、`usedAmount=54.00`、`remainingAmount=6.00`。
- `wan-i2v`：19 镜头，混合 t2v/i2v；角色引用冻结、一致性证据 `consistent=true`、人工评分 5 分写入；8 条逐场成片。
- `wan-refusals`：`EPISODE_SOURCE_TOO_LONG`、`SCENE_CONFLICT`、`CONFIRMATION_REQUIRED`、`EPISODE_SHOT_BUDGET`、`EPISODE_DURATION_LIMIT`、`CHARACTER_ASSET_MISSING` 全部按契约拒绝；无 Job、无提交。
- `wan-cap`：20 个任务占满 `jobCap` 与金额桶，重做返回 `EPISODE_COST_LIMIT`，Job 数保持 20、提交 0。
- `wan-redo`：首个上游任务 `FAILED` 被隔离，重做只追加目标镜头；v1 与 v2 成片均 3 个输入，v2 使用最新镜头版本；提交数 5。

### 3.2 定向测试与前端

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_m5_evaluation.py
10 passed in 232.34s

.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_m5_cli.py
5 passed

.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_web.py tests/test_m5_cli.py tests/test_m5_tools.py
35 passed

node --test tests/test_episode_state.mjs tests/test_film_state.mjs
14 passed
```

### 3.3 安装态 wheel

`scripts/wheel_smoke.py` **PASS**（`output/m5-e4-wheel-smoke.json`）：

- 安装态 M5 工作台：`episodeId=137ec545-16ba-453e-958c-cd136bfd4ccc`、7 场、18 镜头；CLI `vagent episode show` 与 HTTP `/api/episodes`、`/api/episodes/{id}`、会话快照一致；`app.js` / `episode-state.js` / `styles.css` 全部可服务。
- 安装态运行 5 用例：5/5 PASS、42 次受控提交、41 个交付、18 个成片版本、2 次重做，用时 231.9 秒。
- 既有 M1–M3 与 keys-only 段全部保持通过。

### 3.4 受控浏览器

夹具为离线套件 `wan-episode` 的真实状态（`tmp/m5-e4-suite-28/wan-episode`），Chrome（`channel=chrome`，headless，1440×1000）：

| 阶段 | 结果 |
|---|---|
| 载入工作台与会话 | 通过 |
| 打开分集面板 | 7 个场记、`18/18` 交付、`54.00 / 60.00 CNY`、7 个分镜入口、7 个成片播放与下载链接 |
| 点击场记打开分镜 | 两镜头分镜与现有成片播放器正确渲染 |
| 浏览器解码成片 | `videoWidth/Height=1280/720`、时长 10.01 秒、播放推进到 0.7 秒以上 |
| 成片媒体 HTTP | 受控 Range 请求返回 200/206 与字节 |
| 页面错误 | 0 |

证据：`output/m5-e4-browser.json`、`output/m5-e4-episode-panel.png`、`output/m5-e4-episode-storyboard.png`。

### 3.5 真实样本

`fridge-notes`（969 字符）→ 1 集 18 镜头 / 90 秒 / 7 场；Wan 北京 `wan2.7-t2v-2026-06-12`、720P、5 秒、`watermark=true`、`prompt_extend=false`。逐镜头请求含 `style-prompt-zh-v1` 风格模板。

| 项 | 值 |
|---|---|
| 登记与提交 | 18 个任务，`submit=18`，未重提 |
| 估算 | 18 × 3.00 = **54.00 CNY** |
| 实际账单 | `unknown`（未取得账单；免费额度是否抵扣未知） |
| 文本模型调用 | 0 |
| 分集 | `episodeId=de84c2ed-ddec-44c9-9cc0-25110e753d6a`，7 场、18 镜头、90 秒 |
| 成片 | 7 条逐场成片，合计 **32,666,071 字节**；4 条 10.096549 秒（2 镜头/场）、3 条 15.159766 秒（3 镜头/场），全部 1280×720/H.264+AAC |
| 拼接引擎 | 7 条均为 `ffmpeg-concat-copy-v1`（真实输出彼此兼容，未触发 M4 归一化回退） |
| 浏览器 | 分集面板 18/18、7 条成片可播放；成片解码 1280×720、首条 10.0965 秒；截图 `output/m5-e4-real-episode-panel.png`、`output/m5-e4-real-episode-storyboard.png` |

逐任务与逐成片的 ID、大小、SHA-256、时长、编码与 assembly 记录见 `output/m5-e4-real/evidence.json`；首条成片副本与缩略图见 `output/m5-e4-real/scene-1-film.mp4`、`scene-1-thumb.png`。真实样本验收脚本与日志在 `tmp/m5-e4-real/`（本地证据，不进入 wheel）。

### 3.6 完整回归与门禁

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1233 passed, 3 skipped in 813.17s (0:13:33)

.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs tests/test_episode_state.mjs
36 passed
```

- E3 基线为 1215 passed / 3 skipped；本包新增 18 项（`test_m5_evaluation.py` 10、`test_m5_cli.py` 5、`test_web.py` 1、`test_media_worker.py` 2）。3 项 skip 仍是既有的“当前 Windows 账户无符号链接权限”用例，未新增 skip。
- Ruff check/format 全仓库通过（164 个文件）；前端四个状态文件 36 项 Node 测试通过。

四组 CI（ubuntu/windows × Python 3.11/3.12）在最终代码提交上的运行与结论见第 6 节。

## 4. 真实样本证据（明细）

见 `output/m5-e4-real/evidence.json`。关键结论：

- 18 个任务均从 Wan 真实提交、查询并下载；18/18 `succeeded`，无失败、无 `unknown`、无重提；结果主机在 `wan-result-hosts-v2` 允许集内。
- 7 条成片均由当前分镜版本的最新已交付镜头生成；4 条 2 输入 / 10.096549 秒、3 条 3 输入 / 15.159766 秒，全部 1280×720、H.264+AAC；拷贝签名彼此兼容，7 条均走 `ffmpeg-concat-copy-v1`。
- 浏览器对分集面板与首条成片的实测：7 个场记、18/18 交付、`54.00 / 60.00 CNY`、成片解码 1280×720 且播放推进；Range 请求返回 200/206；页面错误 0。
- 实际扣费与账单未取得，因此只记录估算（54.00 CNY）；不把 `usage.duration` 写成实际扣费。

| 场 | 镜头 | 成片字节 | 时长（秒） | SHA-256 前缀 |
|---|---|---|---|---|
| 1 内景 厨房 清晨 | 2 | 8,357,269 | 10.096549 | `7b4a215a2407` |
| 2 冰箱上的便条 2 | 3 | 3,413,499 | 15.159766 | `1a086ee8bb1e` |
| 3 冰箱上的便条 3 | 3 | 3,997,046 | 15.159766 | `4fef8ff375e3` |
| 4 冰箱上的便条 4 | 2 | 2,895,964 | 10.096549 | `728e0b318d72` |
| 5 内景 厨房 夜 | 2 | 5,948,199 | 10.096549 | `d88bcd88ca77` |
| 6 冰箱上的便条 6 | 3 | 3,391,653 | 15.159766 | `b7797837e190` |
| 7 冰箱上的便条 7 | 3 | 4,662,441 | 15.159766 | `e95830e59b31` |

## 5. 明确未做与边界

- **整集成片（跨场组合）**：M6；E4 只做逐场成片。
- **真实角色一致性样本**：真实样本为 t2v；真实 i2v 参考图、角色外观人工评分仍待单独的素材与授权。离线 `wan-i2v` 已覆盖引用与评分机制。
- **Kling/fal 真实账号**：E4 未提交；能力表继续标注「文档支持，未做真实样本」。
- **真实 DeepSeek 分集编排**：E4 不调用文本模型；Agent 驱动的真实计划仍待需要文本费用的单独授权。
- **9:16 竖屏、字幕/TTS/BGM/转场、多集队列、时间线编辑、LoRA/声音克隆**：不在 M5。
- 离线套件的受控上游只证明应用路径、拒绝边界与本地成片，不证明真实账号、CDN、编码与账单。

## 6. CI 与提交

- 最终代码提交：`<TBD>`。
- 四组 CI：`<TBD>`。

## 7. 失败与偏差记录

1. **媒体 Worker 在等待刷新下丢失 prepared 进度（产品缺陷）**：18 镜集的集合等待会持续 `refresh` 每个成功 Job 的媒体可用性，导致与下载 Worker 的 `expected_revision` 冲突；旧的 `_save` 在 `prepared` 保存冲突时直接向上抛出 `JOB_REVISION_CONFLICT`，Job 停在 `writing`，下一次循环按网络失败重下整条片段，20 镜集实际耗时被放大到 15 分钟以上。修复：`MediaWorker._save/_start_download/_fail` 在版本冲突时重读最新 Job 合并写入；`media.refresh` 对 `writing/prepared` 阶段直接跳过（下载进行中不写可用性）。回归见 `tests/test_media_worker.py` 新增 2 项。
2. **Web 会话快照缺少逐场成片投影（产品缺陷）**：`/api/episodes/{id}` 有 `film`，但会话快照直接调用 `read_episode`，导致分集面板显示「尚未拼接」、没有播放/下载链接。浏览器验收首次暴露；修复为快照复用 `ApplicationService.episode` 的同一投影。
3. **用例共享数据目录导致跨界任务（套件缺陷）**：见第 2 节第 2 条；修复为每用例独立数据目录，`wan-cap` 停止 Worker 后只验证注册与拒绝。
4. **`--live` 与真实样本的边界**：离线套件明确拒绝 `--live`；真实样本通过独立的一次性脚本执行，脚本在提交前校验 18 镜头/全部 5 秒/54.00 CNY，失败保留证据且不重提。
5. **既有 `episode_read` 的读取视图不变**：Web 的 `film` 投影只加在应用层，工具返回仍按 E2/E3 契约；未新增工具版本（保持 9/9）。
