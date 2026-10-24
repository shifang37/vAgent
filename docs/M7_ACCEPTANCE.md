# M7 验收：多集与工作台（Season、批量队列、时间线）

日期：**2026-10-23**。基线：`master` `71bc50a`（M7 E3 时间线工作台已合入；M7 E0 契约 = `dd3dfde`，M6 E4 = `5cfd754`）。范围见 [M7 契约](./M7_CONTRACTS.md) §9 的 E4，冻结决策见 §1，上游语义见 §2–§5 与 [E3 验收](./M7_E3_ACCEPTANCE.md)。**E4 未读取凭据、未访问供应商、未新增付费样本：真实视频 submit=0、文本模型调用=0、TTS 真实请求=0。** 离线套件的受控 Wan 传输只连 `httpx.MockTransport`，镜头是仓库内的本地测试 MP4，配音是 `local-tts-v1`；浏览器与安装态验收复用同一份本地媒体。

## 1. 交付内容

| 交付 | 实现位置 | 说明 |
|---|---|---|
| 离线端到端套件 | `scripts/evaluate_m7.py` | 默认离线、`--live` 明确拒绝；3 个用例走真实 Store、季/批次/时间线/字幕/音频工具、受控 Wan 传输、Job/Media Worker、本地渲染与包内 FFmpeg。`--only` 仅用于开发调试 |
| 套件用例 | 同上 | `season-batch`（3 集 36 Job、预览 36、重放不新增）、`season-failure`（单集 `BATCH_EMPTY` 隔离、显式恢复只重登记失败集、旧批次逐字节保留）、`timeline-reassemble`（14 镜头 67 秒时间线 v1→v2 重拼、字幕重映射、旧成片保留、过期拒绝与非法编辑拒绝） |
| 套件测试 | `tests/test_m7_evaluation.py` | 4 项：整套报告与关键数值、`--live` 拒绝、不覆盖报告/不复用非空目录、套件身份与纯函数 |
| 受控浏览器 | `tmp/m7-e4-browser/check.mjs`、`output/m7-e4-browser/` | Chrome 155 headless：时间线面板 v2/14 镜头/65.6 秒、客户端顺序预览、上移镜头并保存 v3、导出成片 v3、Range 206、零页面错误 |
| 安装态 wheel | `scripts/wheel_smoke.py` | 新增 `smoke_m7_evaluation`（安装态运行 3 用例）与 `smoke_m7_workbench`（季/批次/时间线 CLI、HTTP 读面、音频导入、成片 Range） |
| 验收文档 | 本文件、[M7 契约](./M7_CONTRACTS.md)、[M5 计划](./M5_PLAN.md)、[E3 验收](./M7_E3_ACCEPTANCE.md)、`README.md`、`M1_PLAN.md`、[Harness 设计](../AGENT_HARNESS_DESIGN.md)、[Web 说明](../web/README.md) | 记录交付、边界、失败与复现 |

## 2. 相对计划的实现决策

1. **复用本地媒体，不新增付费样本。** 套件的 14 个镜头全部由受控 Wan 传输提交、由 `MediaHttpClient` 下载仓库内的 `tests/fixtures/m1c/sample-720p.mp4`；没有真实账号、CDN 或账单调用。BGM 通过客户端 `import_audio` 导入本地合成 WAV，配音走 `local-tts-v1`。报告显式记录 `realProviderCalls=0`、`textModelCalls=0`、`ttsRealRequests=0`。
2. **批量用例只证明登记与隔离。** `season-batch` 的 3 集各 12 镜头在 mock 模式下登记 36 个 Job，预览与登记都回显 12/12/12；同一调用重放返回原批次且不新增 Job。批量层不提交、不等待，与 E2 冻结语义一致。
3. **恢复只重登记失败集。** `season-failure` 先让第二集没有绑定分镜，批次为 `partial`（24 个 Job，第二集 `BATCH_EMPTY`）；随后把第二集改成 EpisodeVersion 2 并补齐分镜，`resumeBatchId` 只为它创建 `resumeOf` 新批次、冻结 v2、新增 12 个 Job；旧批次逐字节不变，成功集不重复登记，已全部成功的批次再次恢复被 `BATCH_CONFLICT` 拒绝。
4. **时间线 v1→v2 是“只增不改”的重拼。** 6 场 14 镜头约 67 秒的时间线保存为 v1（22 条整集字幕 cue、1 条 BGM、15 条 voice、2 张标题卡）；v2 交换前两镜、把新首镜裁掉 1 秒并加 400 ms 叠化，有效时长 65.6 秒。两次渲染都只追加 `FilmVersion`：v1 的 SHA 与装配字面量不变，v2 指向 `timelineVersion=2`；再次渲染 v1 命中 v1 成片。
5. **字幕重映射以冻结的时间线为基准。** cue 先按整集字幕轨换算成镜头内偏移，再按 clip 入点与新有效起点重映射；v2 中第一个未裁剪场的第一条 cue 精确前移 1.4 秒（1 秒裁剪 + 0.4 秒转场），22 条 cue 全部单调、不重叠、落在 65.6 秒内，`render.subtitle.retimed=true`。
6. **过期校验放在字节副本上。** 分集改版后旧时间线必须 `stale=true` 且渲染在编码前返回 `TIMELINE_SOURCE_MISSING`。为让同一份工作台数据仍可编辑/预览/导出，套件把工作台数据目录复制为 `timeline-reassemble-stale` 后在副本上做这项校验；工作台主目录保持未过期。复制件里的 `films`/`media`/`renders` 在失败渲染前后逐字节不变。
7. **浏览器编辑、预览、导出闭环。** 浏览器从 `timeline-reassemble` 的副本打开时间线面板：识别当前 v2 与 14 个镜头，顺序预览读取 `/api/media/...` 的本地镜头；把第二镜上移后保存为 v3（revision 2），再导出成片 v3（65.6 秒，Range 206）。页面错误与控制台错误均为 0。
8. **安装态验收复用同一套件。** wheel 环境用 `-I` 运行 `evaluate_m7.py`，再读取 `timeline-reassemble` 数据目录：CLI `season show`/`timeline show|sources`/`audio list`/`film renders`、HTTP 季详情/估算/时间线编辑/音频导入/渲染读取/会话快照/旁挂 SRT/成片 Range 全部通过。

## 3. 验证结果

### 3.1 离线套件

```text
.\.venv\Scripts\python.exe scripts/evaluate_m7.py --home <new> --output <new.json>
3/3 passed；96 个 mock Job、4 条批次、1 次恢复、14 次受控提交、14 个交付、
2 个时间线版本、2 个成片版本、1 次旧版本重放、22 条字幕 cue；
真实供应商调用 0、文本模型调用 0、真实 TTS 请求 0；26.5 秒。
```

- `season-batch`：预览与登记都是 3 集 / 每集 12 个新任务 / 合计 36；批次 `succeeded`、revision 6；逐集 `jobCount/shotCount` 12/12/12；`season_read` 回显 12 个待定镜头/集；同调用重放 `replayed=true` 且 Job 数仍为 36；全程 `submitCalls=0`。
- `season-failure`：原批次 `partial`，成员状态 `[succeeded, failed, succeeded]`，错误码 `[null, BATCH_EMPTY, null]`，Job 24 个；恢复批次 `resumeOf` 指向原批次、只含失败集、`episodeVersion=2`、新增 12 个 Job（合计 36）；原批次逐字节保留；恢复已成功批次返回 `BATCH_CONFLICT`。
- `timeline-reassemble`：14 个已交付镜头、67 秒 v1、65.6 秒 v2；22 条 cue；字幕首条前移 1400 ms；重复/裁剪不足 1 秒/末尾转场分别返回 `TIMELINE_INVALID`/`TIMELINE_TRIM_INVALID`/`TIMELINE_INVALID`；副本上的过期渲染返回 `TIMELINE_SOURCE_MISSING` 且不写 `films`/`media`/`renders`。
- 成片 v2：65.6 秒、8,060,907 字节、SHA-256 `f6dd3e3c…e7355a`、装配 `ffmpeg-render-timeline-v1`、`timelineVersion=2`、16 条音轨（1 BGM + 15 voice）、−16.0 LUFS / true peak −13.9 dBTP、旁挂 SRT SHA-256 `2c9bdec5…d5bb9a`。
- 报告：`output/m7-e4-suite.json`（`suiteId=0e4c17edfd7313bc`）。

### 3.2 受控浏览器

夹具为套件工作台数据目录的副本（`tmp/m7-e4-browser/home`），Chrome 155（`channel=chrome`，headless，1440×1100）：

| 阶段 | 结果 |
|---|---|
| 载入工作台与会话 | 通过（项目 `m7-e4`） |
| 打开时间线面板 | 14 个镜头、当前 v2、revision 1、有效 65.6 秒 |
| 客户端顺序预览 | `#timeline-preview-video` 读取 `/api/media/…/content`，标签「预览第 1/14 段」 |
| 编辑并保存 | 第二镜上移，保存为 v3（revision 2、14 个镜头、65.6 秒） |
| 导出成片 | 导出完成：成片 v3（65.6 秒），`timelineVersion=3` |
| 成片媒体 HTTP | 受控 Range 返回 206 与 1024 字节 |
| 页面错误 | 0（page/console） |

浏览器数据目录中的成片历史为 `[v1 67.0 秒, v2 65.6 秒, v3 65.6 秒]`；v1/v2 的 SHA 未被改写，v3 SHA-256 `e4a537d0…36826ce`、7,817,325 字节。证据：`output/m7-e4-browser/check.json` 与 `timeline-panel.png`、`timeline-preview.png`、`timeline-saved-v3.png`、`timeline-exported-v3.png`。

### 3.3 安装态 wheel

`scripts/wheel_smoke.py` **PASS**（`output/m7-e4-wheel-smoke.json`）：

- 安装态运行 M7 3 用例：3/3 PASS、96 个 mock Job、4 条批次、1 次恢复、14 次受控提交、14 个交付、2 个时间线版本、2 个成片版本、22 条字幕 cue。
- 安装态 M7 工作台：`seasonId=f448c9d1-4bf8-45b0-8df3-9361ca59789e`、2 集 / 每集 12 个 Job、批次 `e16858ce-0bf6-5c5c-8e19-1f307e770531`（`succeeded`）；`timelineId=c9ad2d4a-2c50-4e3c-b9fe-f5c8de0bfa79`、v1/v2、65.6 秒；`filmId=8c302759-4a7c-4102-b3ee-c01eb9c5dfe3`、成片 v2、65.6 秒、SHA-256 `04758bc4…689fcf37`、旁挂 SRT SHA-256 `2c9bdec5…d5bb9a`；CLI `season show`、`timeline show|sources`、`audio list`、`film renders` 与本地 `film render` 重放 v2；HTTP 季详情/估算、时间线编辑、音频导入（201）、`/api/renders`、会话快照、旁挂 SRT 与成片 Range 与源状态一致。
- 既有 M1–M6 与 keys-only 段全部保持通过（含 5 个 M1-B、7 个 M1-C、7 个 M2、6 个 M3、5 个 M5、3 个 M6 用例）。

### 3.4 完整回归与门禁

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1409 passed, 3 skipped in 1289.29s (0:21:29)

.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
All checks passed; 217 files already formatted

node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs \
  tests/test_episode_state.mjs tests/test_subtitle_state.mjs tests/test_render_state.mjs \
  tests/test_season_state.mjs tests/test_timeline_state.mjs
78 passed
```

- 本包新增：`scripts/evaluate_m7.py`、`tests/test_m7_evaluation.py` 4 项、wheel smoke 的 M7 两段；CI 同时补上 `render-state`/`timeline-state` 的 `node --check` 与 Node 测试（E3 的 CI 只跑到 `season-state`）。
- 3 项 skip 仍是既有的「当前 Windows 账户无符号链接权限」用例，未新增 skip。
- 四组 CI（ubuntu/windows × Python 3.11/3.12）在最终代码提交上的运行与结论见第 6 节。

## 4. 明确未做与边界

- **真实付费样本**：E4 全程真实视频 submit=0、真实 TTS 请求=0；真实 Kling/fal/CosyVoice、真实 BGM/SFX 导入与真实账号账单仍需单独授权。
- **预览不是交付物**：浏览器顺序预览只按 clip 顺序播放源镜头并应用裁剪偏移，不做字幕叠加与混音；导出才是交付物。
- **过期校验的副本**：套件在字节副本上验证「分集改版 → 时间线过期 → 渲染拒绝」，工作台主目录保持可编辑；生产环境仍按同一冻结语义拒绝过期渲染。
- **帧级精度**：以 30 fps 编码栅格与 ±1 帧容差验收，不承诺跨构建逐字节一致；同一数据目录重放同一时间线版本命中同一成片。
- **9:16 竖屏、跨集「超级成片」、多轨混音工作站、关键帧/变速/特效、自动后台批处理守护进程**：不在 M7。
- 离线套件的受控上游只证明应用路径、批量隔离、时间线重拼与本地渲染，不证明真实账号、CDN、编码与账单。

## 5. 失败与偏差记录

1. **工作台数据被“过期”挡住会导致浏览器/安装态无法编辑预览。** 早期套件直接在 `timeline-reassemble` 主目录上把分集改成 v2，使时间线 `stale=true`，`timeline sources` 的镜头列表因此为空，浏览器与 wheel 工作台无法编辑或导出。修复：过期校验改为在同一数据目录的字节副本 `timeline-reassemble-stale` 上完成，主目录保留 v2 与可编辑镜头；生产语义不变（过期仍拒绝渲染）。
2. **本机 workspace-write 沙箱的子进程写入不可用（环境问题，不是产品缺陷）。** 开发开始时 Ruff/pytest 在 `scripts/`、`src/`、`tests/`、`docs/` 无法创建临时文件；本会话文件策略随后切换为完全访问，Ruff、pytest、构建与浏览器验收均正常执行，未改动任何文件权限。与 M3/M5 记录的宿主环境问题同类。
3. **浏览器面板首帧早于会话快照。** 时间线面板打开时依赖 `snapshot.episode.episodes`；会话切换后快照尚未到达时面板显示空态。受控脚本在面板出现后重试打开直至分集选择器出现（`panelAttempts=2`），页面本身无需修改；这是验收脚本的等待策略，不是产品缺陷。
4. **E3 推送后的四组 CI 全部失败于安装态 M1-b 用例。** E3 把工具/规则增长到 18/18 后，`scripts/wheel_smoke.py` 的 `smoke_evaluation` 仍用 `evaluate_m1b.py` 的默认 64 KiB，安装态 `await-success` 触发 `CONTEXT_LIMIT`；pytest 夹具此前已提高到 96 KiB。修复：wheel smoke 显式传 `--context-bytes 98304`，产品默认 `VAGENT_CONTEXT_BYTES` 仍为 65536。四组 CI 在 E4 最终提交上复跑通过。

## 6. 复现说明

1. 全新数据目录运行离线套件：`VAGENT_HOME=<new> .\.venv\Scripts\python.exe scripts/evaluate_m7.py --home <new> --output <new.json>`；重放同一调用使用相同参数与同一数据目录之外的新目录。
2. 浏览器：复制 `tmp/m7-e4-suite-final/timeline-reassemble` 为 `tmp/m7-e4-browser/home`，`VAGENT_HOME=tmp/m7-e4-browser/home VAGENT_VIDEO_MODE=mock .\.venv\Scripts\python.exe -m vagent web --port 3314 --no-open`，再运行 `node tmp/m7-e4-browser/check.mjs`；报告与截图写入 `output/m7-e4-browser/`。
3. 安装态：构建 wheel、在独立环境安装后运行 `scripts/wheel_smoke.py --output output/m7-e4-wheel-smoke.json`。
4. CI：四组 `Agent checks`（Ubuntu/Windows × Python 3.11/3.12）在最终代码提交上的运行与结论见 `output/m7-e4-ci-*-summary.json`；运行链接与任务 ID 在本文件后续更新中回填。
