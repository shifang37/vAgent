# M3 E1 验收：本地拼接与成片域

日期：**2026-10-21**。基线 `1614788`（E0 契约冻结，schema 4），本包交付 commit 见 `git log`。范围见 [M3 任务规划](./M3_PLAN.md) 的 E1，冻结契约见 [M3 契约](./M3_CONTRACTS.md) §1–§4。**E1 全程视频 submit=0，未调用任何视频供应商，未产生视频费用。**

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| schema v5 | `src/vagent/storage.py` | `DatabaseV4` 保留为历史外壳，`Database` 升为 v5（`media` 联合类型 + `films`）；v1–v4 全部可迁移，v4→v5 只加空 `films` 并改版本 |
| v4→v5 迁移 | `storage.migrated_state` / `FileStore.open` | 迁移前保存 `state-v4-<UUID>.json` 精确字节；校验新外壳后原子替换；失败保留原文件并释放锁；重开不二次迁移 |
| 成片域 | `src/vagent/video/films.py` | `Film`/`FilmVersion`/`FilmSegment`/`FilmAssembly`/`FilmMedia`、只增不改校验（`validate_film_changes`）、`select_inputs` 输入选择、`film_view` 有界投影 |
| 流复制签名 | `src/vagent/video/copy_signature.py` | `mp4-copy-signature-v1`：轨道/顺序/handler、`avcC` 与 AAC `esds` 逐字节、宽高/`pasp`/`colr`/`clap`、`tkhd` 矩阵、timescale、`stts` 规律、`ctts` 形式、`stss`、`sgpd`/`sbgp`、单条（或一条前置空 edit + 一条真实 edit）edit list、AAC priming；未知结构拒绝 |
| 拼接引擎 | `src/vagent/video/assembler.py` | 只允许包内 `imageio-ffmpeg` 二进制（符号链接/reparse/包外路径拒绝并返回 `ASSEMBLER_UNAVAILABLE`）；`-nostdin -protocol_whitelist file -auto_convert 0 -c copy -movflags +faststart`；64 输入、单输入 256 MiB、总输入/输出各 2 GiB、120 秒；取消终止子进程并清理工作目录 |
| 时间线算法 | 同上 `plan_assembly` | 每段时长 = 该输入最长原始轨道结束时间（含 AAC priming），接缝起点为累计时长；输出校验样本数求和、`avcC`/`esds` 一致、视频/音频结束时间与模型一致（容差 = 一帧 + 一个 tick）、空编辑偏移上限、视频合成偏移等于首个输入；非零返回码或非单调 DTS 等时间戳告警一律拒绝 |
| 服务编排 | `src/vagent/video/film_service.py` | 锁内冻结输入 → 锁外校验/拼接 → 重获锁复核 revision 与输入哈希 → 原子改名 + 单事务追加 `FilmVersion`、`FilmMedia` 与操作结果；指纹命中复用原版本；旧产物缺失/损坏返回 `FILM_MEDIA_INVALID`；启动清理 `.part` |
| 媒体联合 | `src/vagent/video/media.py`、`jobs.validate_media_changes` | `media` 索引新增 `kind="film"` 分支，不伪造生成 Job、不改旧记录；`view`/`open_content`、Range/ETag/下载、`media_response` 共用 |
| 工具与版本 | `src/vagent/video/tools.py` | `film_assemble`（可取消的锁外本地写）、`film_read`（只读）；mock/live 均升为 **tools/rules 5/5**；注册 **toolsVersion 4** 的冻结视图，恢复按 `toolFeatures` 身份而非仅 executionVersion 选择；execution/context/checkpoint 仍为 **3/2/1** |
| 客户端动作 | `src/vagent/video/clients.py` | `vagent film assemble` 与 Web 按钮共用确定性 “local-client” Run、`clientRequestId` 幂等重放，不更新会话 latest Run、不占用对话额度、不要求视频 Key |
| CLI | `src/vagent/cli.py` | `vagent film assemble --storyboard ID --expected-revision N [--request-id ID]`、`vagent film show [--storyboard ID]`；不启动 Worker/模型/供应商 |
| Web | `src/vagent/web.py`、`web/app.js`、`web/film-state.js` | `GET /api/films/{storyboardId}?projectId=`、`POST /api/sessions/{id}/films/assemble`（CSRF + 严格 JSON）；分镜面板新增成片区（播放、版本列表、下载、显式拼接/刷新、错误说明） |
| 依赖与分发 | `pyproject.toml`、`requirements-dev.lock`、[第三方清单](./THIRD_PARTY_ASSEMBLY.md) | 精确固定 `imageio-ffmpeg==0.6.0`；wrapper BSD-2-Clause 与包内 FFmpeg GPL v3-or-later 分开记录，附 wheel/二进制 SHA-256 与来源 |

## 2. 关键实现决策（相对 E0 的细化）

1. **接缝时长来自样本表，而非夹具常量。** `segment_duration = max(视频原始结束, 音频原始结束)`。对已提交夹具：视频 `51200/10240 = 5.0`，音频 `241024/48000 = 5.021333…`，因此每段 `5.021333333`，与 E0 手写的夹具专用常量在数值上一致，但此处由样本/预卷推导。
2. **输出预期按轨道分别建模。** concat 只保留首个输入 edit list 的呈现偏移，因此视频结束 = 累计段时长 − 末段自身音视频差，音频结束 = 累计音频原始时长。实测 `10.0212890625` 与模型 `10.021333…` 的差为 2/48000 秒（≤ 一帧容差）。
3. **FFmpeg 的接缝补帧是被接受的封装偏移。** 输出视频 `stts` 出现 `[(49,1024),(1,1242),(50,1024)]`：接缝处一帧被拉长 218 ticks（21.3 ms），即一次 AAC priming 的偏移，落在 `max(视频一帧, AAC 一帧)` 内并逐条写入 `assembly.timeline`；E0 的默认变体则触发 `Non-monotonic DTS` 告警，本实现直接拒绝。
4. **工具版本恢复改为按身份匹配。** 新增 `register_tools_variant`/`for_tools_version`/`for_record_features`，`AgentRunner.resume` 在 execution=3 但 `toolFeatures` 不同时只委托一次 `for_record`，无法复现时以 `RESUME_CONFIG_CHANGED` 拒绝（原有 D2 时代的拒绝用例继续通过）。
5. **锁外写入是显式的新执行类别。** `ToolDefinition.async_write_execute` 与 MCP 的只读异步工具分开注册：写工具必须 `effect="write"`，校验上下文与只读权限后释放锁执行，绝不冒充 read。

## 3. 验证结果

### 3.1 定向与全量

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js && node --check web/film-state.js
node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs
```

- 全量：**1007 passed / 2 skipped**（skip 仍为当前 Windows 账户无符号链接权限的既有用例），耗时约 7 分钟；其中新增 `tests/test_m3_assembler.py` 17 项、`tests/test_m3_film_service.py` 10 项、`tests/test_m3_film_api.py` 2 项、`tests/test_m3_film_cli.py` 1 项，并改写 `tests/test_m3_baseline.py` 的 v4→v5 迁移断言（用 E0 冻结的 `expectedV5CanonicalSha256` 校验真实迁移）。
- 前端：**31 项**通过（`test_film_state.mjs` 9 项新增）。
- Ruff check/format：通过（含对 E0 遗留三个未格式化脚本的修正）。

### 3.2 逐字节证据

用已提交夹具 `tests/fixtures/m1c/sample-720p.mp4`（SHA-256 `b179cca0…6a54`）拼接两份，在 Windows amd64 + `imageio-ffmpeg==0.6.0`（包内 FFmpeg 7.1 essentials，SHA-256 `2ce797a0…71a3`）下得到：

| 指标 | 实测 | 说明 |
|---|---|---|
| 大小 | **418,395 字节** | 与 E0「夹具修正变体」一致 |
| SHA-256 | **`41be1ed90fa7abfcdb3b3d46775d96695ca3e441f92adb15363be400ec21fd8a`** | **与 E0 记录逐字节相同**，说明通用算法复现了实验变体 |
| 结构 | 1280×720、H.264/AAC、10.0212890625 秒 | `inspect_mp4` 通过 |
| 样本 | 视频 100、音频 472（各为输入的 2 倍） | 无丢帧、无截断 |
| stderr | 空 | 无 DTS/损坏告警 |

该哈希断言只在 `sys.platform == "win32"` 且二进制 SHA-256 匹配时启用；其他平台断言结构、样本数与时间线不变（Linux wheel 已由 E0 记录但未在本机安装）。

### 3.3 迁移与恢复

- v4→v5：三份 E0 基线快照（`tests/fixtures/m3/*.json`）逐一迁移，`store.snapshot()` 等于 `{**v4, schemaVersion: 5, films: {}}`，canonical SHA-256 等于清单里的 `expectedV5CanonicalSha256`；备份 `state-v4-*.json` 与原文逐字节相同；检查点旁车文件不变；重开不再迁移。
- v3→v5：`tests/test_m2_migration.py` 断言 v4 投影的 canonical 哈希仍等于清单里的 `expectedV4CanonicalSha256`，其余集合逐字节保留。
- v1/v2→v5：`tests/test_storage_migration.py`、`tests/test_m1c_migration.py` 更新为最终 v5 外壳，历史域数据与操作指纹不变。
- 4/4 → 5/5 恢复：`test_e1_restores_a_four_four_run_with_its_frozen_tools_and_starts_five_five` 用真实 SQLite 检查点记录一个 4/4 Run，重启后由 5/5 Runner 恢复，断言恢复后的工具集不含成片工具、`toolFeatures` 仍为 4/4，且新 Run 使用 5/5。
- D2 时代不可复现的签名仍拒绝：`test_a_d2_era_m2_run_is_refused_instead_of_resuming_differently` 通过。

### 3.4 行为边界

- **零供应商调用**：`film_assemble` 不注册 Job、不读 Key、不建 HTTP 客户端；测试断言 `jobs` 数量在拼接前后不变。
- **幂等**：同一组输入（相同指纹）换 `clientRequestId` 再拼接，返回原版本、不新增文件；分镜 revision 变化后显式重拼产生 v2，旧版本与文件保留。
- **拒绝矩阵**：缺少镜头记录 `FILM_INPUT_MISSING`；最新版本未交付 `FILM_INPUT_NOT_READY`；编码/时间线不兼容 `FILM_INPUT_INCOMPATIBLE`；输入文件缺失或哈希不符 `FILM_MEDIA_INVALID`；过期 revision `VERSION_CONFLICT`；拼接产物与校验值不符 `FILM_MEDIA_INVALID`；取消或时间戳告警 `FILM_ASSEMBLY_FAILED`。
- **取消**：预置取消标志的用例断言进程被终止、无 `.part`、无工作目录残留。
- **媒体**：`GET /api/media/{id}` 返回 `kind="film"`、`filmVersion`、`availability`；`content` 支持 HEAD/单 Range/`?download=1`，ETag 为成片 SHA-256；文件删除后 `mediaAvailable=false` 且重新拼接报 `FILM_MEDIA_INVALID`。

## 4. 未完成与后续（E2–E4）

- 未做真实浏览器成片播放验收（E0/E1 未运行受控浏览器套件）；`scripts/evaluate_m3.py`、wheel smoke 扩展、四组 CI 与真实样本留给 **E4**。
- Kling/fal 供应商接入、`multi-provider-v3` 能力表与 `Job/Request v6` 属于 **E2**；headless 与用户 skills 目录属于 **E3**。
- Ubuntu 与 macOS 的安装态拼接只用 Linux wheel 哈希记录，尚未在本机执行；Linux/Windows CI 会在 E4 覆盖。
- 未引用过的遗留最终文件（事务失败后 rename 成功的孤儿）不自动删除：启动只清理 `.part`，避免从孤立文件推造成功；如需人工清理，须在服务退出后按 `media/<project>/<uuid>.mp4` 核对 `state.json` 的 `media` 索引。
- 本机 `.vagent/web` 与 `output/` 中的历史真实媒体未被本次改动迁移或重写；E4 计划用真实数据目录做零新增费用的成片验收。
