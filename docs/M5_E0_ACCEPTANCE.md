# M5 E0 验收：契约冻结与 v5 基线

日期：**2026-10-22**。基线 `master` `00d087f`（M4 归一化已合入，schema 5，工具面 6/6）。**E0 完成。** 本次未调用任何模型或供应商，视频 submit=0，文本模型调用=0，未提交付费任务，未读取凭据或真实数据目录。

## 1. 已冻结交付

| 项目 | 交付与边界 |
|---|---|
| 实施契约 | [M5_CONTRACTS](./M5_CONTRACTS.md)：schema v6、Episode/Scene/Character/Style、迁移、`text-split-zh-v1`、镜头/时长/预算、错误码、排除项、版本号消歧 |
| 数据对象 | `src/vagent/video/episodes.py`：9 个严格契约 + 4 个 Store 提交校验器；不含工具、CLI、Web、供应商或切分算法 |
| 存储 | `src/vagent/storage.py`：`DatabaseV5`（历史）与 `Database`（v6），v5→v6 只加四个空集合；打开时对 9 个集合重跑领域校验器 |
| 场景与分镜 | **场景至多绑定一个分镜（1:1）**，绑定一次不可换；镜头/素材/Job/成片继续按 `storyboardId` 走 M2–M4 机制 |
| 每集额度 | 10–20 镜头、60–90 秒、20 个新 Job、60.00 CNY；旧“每 Run 4 Job/12 CNY”只约束 6/6 及更早工具面 |
| 工具版本 | **E0 保持 6/6**；E1 追加 5 个改编工具时升 7/7，并保留 6/6 及更早冻结变体 |
| 排除 | 9:16 竖屏、转场/字幕/TTS（M6）、多集队列与工作台（M7）、LoRA 与声音克隆 |
| 版本号消歧 | M5 独占 `schemaVersion 6`；M6 若需新存储形状必须用 v7，“成片版本 v6”是剪辑输出序号 |

## 2. schema v5 合成基线

[tests/fixtures/m5/manifest.json](../tests/fixtures/m5/manifest.json) 与四份 v5 快照由 [capture_m5_baseline.py](../scripts/capture_m5_baseline.py) 固定 UUID/时间生成。历史夹具直接沿用 M3 基线；当前成片夹具用冻结 live 管线（HTTP transport 对任何请求报错）加一份**本地真实归一化成片**构成。

| 快照 | 覆盖 | 关键事实 |
|---|---|---|
| `legacy-v5-mock.json` | M3 legacy v4 → v5 外壳 | 2 Job（契约 1）、2 Run、7 Operation，全部原文不变 |
| `legacy-v5-live.json` | M3 legacy v4 → v5 外壳 | 1 Job（契约 2）与 1 条已交付媒体 |
| `current-v5-shots.json` | M3 当前分镜语料 → v5 外壳 | 6 Job（契约 3/4/5）、两版分镜、5 条 ShotVersion、1 条集合等待，Run 全部 **4/4** |
| `current-v5-films.json` | M4 成片表面 | 2 条已交付片段 + 1 条 `FilmMedia`（union）、1 个 Film、3 个 **6/6** Run |

- 四份快照的原字节、canonical、集合哈希、Job 请求/意图指纹、Operation 指纹、媒体/素材哈希、分镜历史、成片版本与 Run 签名全部固定在 manifest；`expectedV6CanonicalSha256` 只计算“v5 + 四个空集合 + `schemaVersion=6`”的机械预期。
- **M4 表面被完整保留**：`current-v5-films.json` 的成片版本为 `assemblyVersion=ffmpeg-concat-normalize-v4`、`profile=h264-1280x720-30fps-aac-48k-stereo-v1`、`1,261,138 字节 / 9.866667 秒 / 1280×720`，媒体索引同时含有 2 条 `MediaAsset` 与 1 条 `FilmMedia`；引擎记录为包内 `imageio-ffmpeg 0.6.0` + FFmpeg 7.1 essentials，二进制 SHA-256 `2ce797a0…71a3`（与 M3 E0 记录一致）。
- **两代工具签名共存**：`current-v5-shots.json` 的 Run 保持 4/4（`toolFeatures` 哈希固定），`current-v5-films.json` 的 Run 为 6/6（`contextSignature` 与 `toolFeaturesSha256` 由当前真实注册表、内置 skills 元信息与合成模型身份算出，例如 `film-run-1` 为 `8560da2b…`）。E1 升到 7/7 后必须仍能重建这两个身份。
- 这是领域 JSON 基线：**没有像素文件、没有凭据、没有可恢复 SQLite 检查点**。成片记录的字节哈希来自本次本地归一化，重复生成会不同；按基线约定**不得为迁就后续实现而重新生成**。

## 3. 领域契约与校验证据

`tests/test_m5_episodes.py`（9 项）覆盖：分集/场景/圣经原子提交与重开、场景分镜绑定唯一性、镜头数与时长合计必须等于冻结场景（`EPISODE_SHOT_BUDGET`/`EPISODE_DURATION_LIMIT`）、分集历史只增不改、角色素材必须存在（`CHARACTER_ASSET_MISSING`）、关系必须同项目（`CHARACTER_NOT_FOUND`）、圣经不可删除（`CHARACTER_IMMUTABLE`/`STYLE_IMMUTABLE`）、`ShotBudget` 成本上限与 20 镜头硬上限、场景时长必须容得下自身镜头数。

`tests/test_m5_baseline.py`（13 项）覆盖：四份快照的完整性与固定身份（含 6/6 工具签名断言）、v5→v6 迁移逐字节等价与 `state-v5-*.json` 备份、检查点旁车哨兵不变、重复打开不再迁移、成片输入与已交付媒体/ShotVersion 的交叉引用、media union 三类记录、冻结常量（60–90 秒、10–20 镜头、场景 6、20 Job、60.00 CNY、`text-split-zh-v1`）、缺成片媒体或缺分镜时拒绝打开且保留原字节。

因 schema 升到 v6，同步把历史迁移断言的终态从 v5 更新为 v6（`tests/test_m2_migration.py`、`tests/test_storage_migration.py`、`tests/test_m3_baseline.py`）：M3 基线仍先核对**冻结的 v5 外壳哈希**，再核对继续机械升级到 v6 的结果，历史域值逐键比对未变。

## 4. 检查结果

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_m5_baseline.py tests/test_m5_episodes.py tests/test_m2_migration.py tests/test_storage_migration.py tests/test_m3_baseline.py
.venv\Scripts\python.exe -m ruff check --no-cache .
git diff --check
```

- 定向回归：**52 passed**（M5 基线 13、M5 分集域 9、M2 迁移 12、存储迁移 5、M3 基线 13）。
- 完整 Python 3.12 回归（见第 5 节）：**1145 passed / 3 skipped，594.84 秒**。3 项 skip 是既有的“当前 Windows 账号无 symlink 权限”skills 用例，未新增 skip。
- Ruff：全仓库通过（`--no-cache`，本机 `.ruff_cache` 不可写）。`git diff --check` 通过。
- submit=0：未登记生成任务、未产生视频或文本费用；生产存储仍为 v5，只有新建数据目录从 v6 开始。
- 因 schema 升到 v6，历史用例里 7 处“终态 schemaVersion == 5”断言同步改为 6，并让 v1/v3/v4 投影显式排除四个新集合；冻结的 v3/v4/v5 外壳哈希断言全部保留且仍然通过。
- 环境备注：本机的 `output/` 与 `tmp/` 两个既有目录当前不可写（与本轮代码无关），E0 的捕获与测试全部使用系统临时目录。

## 5. 完整回归

```text
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1145 passed, 3 skipped in 594.84s (0:09:54)
```

判定标准是**全部通过、无新增 skip**：M3/M4 的既有断言只按版本号机械更新，没有任何用例放宽或跳过。

## 6. 下一工作包 E1

先实现 `text-split-zh-v1` 与离线镜头草案（模型调用=0），再接入 `episode_plan`/`episode_save`/`episode_read`/`bible_save`/`bible_read` 并把工具面升到 7/7（保留 6/6 变体），最后用固定 3 篇中文故事验证“对白不丢、结构完整、预算可复现”。E1 仍保持 submit=0；真实生成从 E3 起、需单独授权。
