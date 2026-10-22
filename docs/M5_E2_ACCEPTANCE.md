# M5 E2 验收：角色/风格一致性

日期：**2026-10-22**。基线 `master` `9283658`（M5 E1，schema 6，tools/rules 7/7）；本包交付 commit 见 `git log`。范围见 [M5 计划](./M5_PLAN.md) §3.2 E2，冻结契约见 [M5 契约](./M5_CONTRACTS.md) §2、§6、§7。**E2 全程离线：模型调用=0，视频 submit=0，未读取真实 Key、未登记付费任务、未产生费用。**

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 场景绑定 | `src/vagent/video/storyboards.py` | `storyboard_save` 新增可选 `sceneId`：与保存分镜同一 Store 事务写入 `Scene.storyboardId` 并递增 `revision`；一个场记只能绑定一个分镜、已绑定不可换绑（`SCENE_CONFLICT`），同一分镜也不能被两个场记绑定 |
| 角色引用与冻结 | 同上 + `consistency.py` | 镜头新增有序 `characterIds`（i2v 1 个、flf2v 按首帧/尾帧 2 个、t2v 0 个）；服务端取每个角色最新版本的第一个 `referenceAssetIds`，按同一顺序写入 `Shot.referenceAssetIds`，并冻结 `Shot.characterRefs[{characterId, characterVersion}]`；同一镜头不能同时给 `characterIds` 与 `referenceAssetIds` |
| 缺素材拒绝 | 同上 | 角色没有参考图、参考图不在本项目素材库、不是图像，或与镜头冻结的引用不一致时返回 `CHARACTER_ASSET_MISSING`，整次保存不写入任何场记/分镜，不降级为 `t2v` |
| 跨镜头一致性证据 | `src/vagent/video/consistency.py`、`episodes.py` | `episode_read` 对分集每个被引用角色重算全部已绑定分镜的镜头证据（`shotId`、`storyboardId`、角色版本、参考素材），给出 `assetIds`、`characterVersions`、`shotCount` 与 `consistent`；角色追加新版本不会改写旧镜头，新旧版本混用会如实报告 `consistent=false` |
| 风格提示模板 | 同上 | 新增冻结模板 `style-prompt-zh-v1`：`【风格】名称｜题材：…｜色调：…｜镜头语言：…`，随后 `【出场】` 有序角色名，最后是镜头描述；同一分集/角色版本/描述产生逐字节相同的提示词，preview、headless plan 与登记路径共用同一拼装函数 |
| 人工一致性评分 | `src/vagent/video/episodes.py` | `Episode.consistencyReviews[]`（E2 追加的兼容字段，默认空）；`consistency_review` 工具只记录用户明确给出的 1–5 分，同一事务内重算证据并写入角色/分集版本、镜头、分镜、素材、`consistent`、评分人；没有镜头证据返回 `CONSISTENCY_EVIDENCE_MISSING`；只增不改（`EPISODE_IMMUTABLE`），不改变镜头/任务/预算/费用 |
| 工具面 | `src/vagent/video/tools.py` | 新增 `consistency_review`，tools/rules **7/7 → 8/8**（mock 与 live 同步），冻结 **7/7** 视图（隐藏该工具、保留 `VIDEO_RULES_V7` 指令）；6/6、5/5、4/4 等更早视图继续隐藏 M5 工具，5/5 与 4/4 的 `video_generate` 仍为 `VideoIntentV2`，6/6 与 7/7 恢复为其真实六参数 schema |
| 文档 | 本文件、`docs/M5_CONTRACTS.md`、`docs/M5_PLAN.md`、`README.md` | 契约补 E2 字段/不变量/错误码/模板语义；计划与 README 标注 E2 完成 |

## 2. 关键实现决策（相对契约的细化）

1. **只加字段，不动 schema 版本。** E2 只在 `Shot` 上追加默认空的 `characterRefs`，在 `Episode` 根上追加默认空的 `consistencyReviews`；E0/E1 记录、M2–M4 分镜、v5 合成基线与检查点都不需要重新编码，`schemaVersion` 保持 6。
2. **一个角色一个帧，取第一张参考图。** `i2v` 天然只有一帧，`flf2v` 有两个角色各占首/尾帧；角色圣经允许多张参考图，但镜头构图只使用第一张，规则固定且可复现。需要首尾帧来自同一角色多张图时仍可走 M2 的显式 `referenceAssetIds`（不冻结角色版本）。
3. **素材绑定可证明。** Store 每次提交都从镜头冻结的 `characterVersion` 重算该版本的第一张参考图，与 `Shot.referenceAssetIds` 不一致即拒绝；因此直接改 JSON 的素材或角色版本无法通过，不需要第二份绑定表。
4. **证据按场记×分镜重算。** 一致性只统计分集冻结场记所绑定分镜的当前版本，未绑定场记不猜分镜；评分与 `episode_read` 用同一函数，避免“评分通过但读取不一致”。`consistent` 只在全部镜头指向同一素材时为真，零镜头不算“一致”，评分会被拒绝。
5. **风格模板是请求的一部分。** 绑定场记后，`_derive_shot` 在 preview/plan/register 三条路径上使用同一 `compose_shot_prompt`；风格或角色版本变化会改变请求指纹，从而不会错误复用旧 Job。未绑定场记的分镜行为与 M2 完全一致。
6. **评分是人工事实，不是模型输出。** 工具说明与规则文本都要求“只有用户明确给出分数时才调用”；评分只追加到分集根记录并递增 `revision`，与 M2 把 `ShotVersion` 追加到分镜根记录同构，不产生新的分集版本。
7. **修复 E1 的冻结 schema 偏差。** E1 冻结 6/6 时给 live `video_generate` 误用了 `VideoIntentV2`，并漏掉 5/5 的同一覆盖；E2 按 M3 契约恢复：4/4、5/5 用 `VideoIntentV2`，6/6、7/7 用当前 `VideoIntentV6`（允许 fal 模型名中的 `/`）。同时给 5/5、6/6、7/7 补上应隐藏的更新工具，冻结视图不再泄漏新工具。

## 3. 验证结果

### 3.1 新增测试

`tests/test_m5_consistency.py` **14 项**：

| 覆盖 | 断言 |
|---|---|
| 场景绑定与素材冻结 | `sceneId` 绑定后场记 `storyboardId`/`revision` 正确；i2v 只取 1 张；`characterRefs` 记录角色版本；`storyboard_read` 回显 `characterIds` 与 `referenceAssetIds`；重复绑定/换绑均 `SCENE_CONFLICT` |
| 首尾帧顺序 | 两个角色按 `characterIds` 顺序得到 `[首帧素材, 尾帧素材]` |
| 缺素材 | 角色无参考图时整批 `CHARACTER_ASSET_MISSING`，Store 无分镜、无场记 |
| 引用形状 | i2v 重复角色、t2v 带角色、flf2v 少角色、同时给两种引用均 `INVALID_ARGUMENTS` |
| 篡改拒绝 | 改素材或角色版本后 Store 提交分别返回 `CHARACTER_ASSET_MISSING`/`CHARACTER_NOT_FOUND`，状态逐字节不变 |
| 跨项目 | 引用其他项目角色返回 `CHARACTER_CONFLICT` |
| 证据与模板 | `episode_read` 返回 `stylePrompt` 与逐角色证据；同一分镜两次 `plan_shots` 的请求逐字段相同且提示词以风格模板开头 |
| 角色名顺序 | `cast_names` 按首尾帧顺序返回冻结姓名 |
| 模板纯函数 | `style_prompt`/`compose_shot_prompt` 固定字节与可选风格/出场 |
| 版本混用 | 角色 v2 换图后新增镜头使证据变为 `consistent=false`、`assetIds` 两项、版本 `[1,2]` |
| 评分记录 | 3 个镜头评分 5：证据含 3 个 `shotId`、同一素材、`consistent=true`；分集 `revision` 递增；同 operation key 重放不重复；改写/删除评分 `EPISODE_IMMUTABLE` |
| 无证据评分 | 没有镜头时 `CONSISTENCY_EVIDENCE_MISSING`，分集 revision 不变 |
| 重开恢复 | 关闭并以 `FileStore.open` 重开后评分仍在 |
| 无图角色 | 角色圣经本身仍是合法记录（只在镜头引用时拒绝） |

既有用例只按版本号机械更新（7→8）：`test_m5_tools.py`、`test_m3_recovery.py`、`test_m2_compatibility.py`、`test_m2_generation.py`、`test_m2_clients.py`、`test_m2_integration.py`、`test_video_integration.py`、`test_live_application.py`、`test_wait_compatibility.py`；未放宽任何行为断言。

### 3.2 门禁

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_m5_consistency.py tests/test_m5_tools.py tests/test_m5_episodes.py tests/test_m5_adaptation.py tests/test_storyboards.py tests/test_storyboard_views.py
.\.venv\Scripts\python.exe -m ruff check --no-cache src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src/vagent tests scripts
```

- 定向：`test_m5_consistency.py` 14 项；连同 M5 工具/域/改编与分镜相关用例共 **97 passed**。
- Ruff：check 与 format 全仓库通过（160 个文件）。
- 前端未改；`node --check web/app.js` 与三个状态文件 `node --test` **31 项** 全部通过。

### 3.3 完整回归

```text
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1203 passed, 3 skipped in 680.08s (0:11:20)
```

- 3 项 skip 仍是既有的“当前 Windows 账户无 symlink 权限” skills 用例，未新增 skip。
- 全量在无沙箱限制的本机进程中运行；受限沙箱下会拒绝 FFmpeg/MCP/redis 与 CLI 子进程，详见第 5 节。
- `submit=0`、模型调用=0：所有 Job/媒体/成片用例都使用 mock 或受控 HTTP，没有真实 Key、真实任务或费用。

## 4. 明确未做

- **真实生成与真实样本**：E2 只做引用、模板与证据，不登记/提交任何视频 Job；真实角色一致性外观评分需要 E3/E4 的生成与单独授权。
- **CLI/Web 新流程**：E2 复用 M2/D3 的 `vagent assets import` 导入参考图，未新增 `episode`/`bible` CLI 或 Web 面板；浏览器与安装态证据留待 E4。
- **多参考图构图**：每个角色只用第一张参考图；多角度选图、LoRA、声音克隆不在 M5。
- **9:16 竖屏**：仍未进入 M5。

## 5. 失败与偏差记录

- 本机受限沙箱（workspace-write）下首次全量为 **1147 passed / 32 failed / 24 errors / 3 skipped**：56 项非通过全部落在子进程类用例（FFmpeg 拼接 `ASSEMBLER_UNAVAILABLE`、MCP stdio `MCP_CONNECT`、CLI 子进程与 redis-server 启动失败），没有一项在 M5/E2 域或工具断言上；同一命令在无文件沙箱限制的本机进程中为 **1203 passed / 3 skipped**。另：沙箱下 `pytest` 以 `mkdir(mode=0o700)` 创建的临时目录无法被受限令牌枚举，首次定向运行用仅本地的目录模式补丁绕过（不进入仓库），无沙箱运行时不需要该补丁。
- 上述 56 项并非“已知失败被忽略”：完整回归以干净环境的 1203 通过为准，受限环境只作为边界证据记录。
- 发现并修正 E1 遗留的冻结视图偏差（6/6 误用 `VideoIntentV2`、5/5 漏掉该覆盖、5/5/6/6 未隐藏 M5 工具），修正后 4/4、5/5、6/6、7/7 的 schema 与工具集合分别符合 M3/M5 契约；详见第 2 节第 7 条。
- 首轮实现把 `style_for_storyboard` 放在 `consistency.py` 顶层并直接导入 `episodes`，会形成循环导入；最终改为函数内延迟导入，模块导入与工具注册均无循环。
- 未做真实账号/付费样本，因此所有结论都是受控本地证据；评分是人工判断的记录，不代表模型自动评估质量。
