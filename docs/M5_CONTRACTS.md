# M5 契约：剧本/小说 → 分集分场分镜

冻结日期：**2026-10-22（E0）**。基线 `master` `00d087f`（M4 自动时间线归一化已合入，schema 5，工具面 6/6）。范围见 [M5_PLAN](./M5_PLAN.md)，上游语义见 [M3 契约](./M3_CONTRACTS.md) 与 [M4 归一化](./M4_NORMALIZATION.md)，证据见 [M5 E0 验收](./M5_E0_ACCEPTANCE.md)。

本文规定 E1–E4 的实现要求。**E0 不注册新工具、不改 tools/rules、不调用任何模型或供应商；视频 submit=0，文本模型调用=0。** 本轮**不做 9:16 竖屏**：不新增画幅字段，不启用 mock 能力表里历史冻结的 `1080p/9:16` 条目，M5 的镜头规格仍是 16:9 720P。

## 1. 决策与版本

| 项目 | 冻结结果 |
|---|---|
| 存储 | E0 新建与迁移为 schema **6**；v5→v6 只加四个空集合 `episodes/scenes/characters/styles` |
| 工具面 | **E0 保持 6/6**；E1 追加改编工具后为 **7/7**；E2 追加工人评分工具 `consistency_review` 后为 **8/8**（mock 与 live 同步，已实现）；E3 追加 `episode_generate` 与 `shot_redo` 后为 **9/9**（冻结 8/8），并保留 7/7、6/6、5/5、4/4、3/3、2/2、1/1 冻结变体 |
| 恢复 | 继续按完整 `toolFeatures` 身份选择冻结注册表；新版本不得改写旧 prompt/规则字节，未知组合返回 `RESUME_CONFIG_CHANGED` |
| execution/context/checkpoint | 保持 **3 / 2 / 1**，不为分集引入第二套等待状态机 |
| Job/Request | 沿用 **v6** 通用供应商请求；M5 不新增 Job 契约版本，逐镜头登记仍走 `shot_generate` |
| 能力与价格 | M5 不新增能力条目、不改价格版本；Wan `3.00 CNY/5 秒` 仍是逐镜头估算口径 |
| 场景与分镜 | **一个场景至多绑定一个分镜（1:1）**，镜头、素材、Job、成片沿用现有按 `storyboardId` 的机制 |
| 每集上限 | 每集 **10–20 镜头**、**60–90 秒**、**20 个新 Job**、**60.00 CNY**；旧“每 Run 4 Job/12 CNY”只约束 6/6 及更早工具面的 Run |
| 角色一致性 | 参考图 + `i2v`/`flf2v`，不做 LoRA 或声音克隆；缺素材**整批拒绝**，不静默降级 |
| 工具集合 | E1 新增 5 个：`episode_plan`、`episode_save`、`episode_read`、`bible_save`、`bible_read`（角色与风格用 `kind` 区分） |

## 2. 数据对象

JSON 字段采用 camelCase；ID 由服务端生成（canonical UUID），用户/模型不能指定文件路径。四个新对象都**只增不改**：根记录保留 `currentVersion` 与连续 `versions`，历史版本、已冻结的预算与已绑定的分镜不可重写或删除。

| 对象 | 必填内容 |
|---|---|
| `Style` | `id, projectId, currentVersion, versions[], revision, createdAt`（项目级风格圣经） |
| `StyleVersion` | `version, name, genre[], palette, cameraLanguage, createdAt` |
| `Character` | `id, projectId, currentVersion, versions[], revision, createdAt`（项目级角色圣经） |
| `CharacterVersion` | `version, name, aliases[], appearance, wardrobe, voice, relationships[], referenceAssetIds[], createdAt` |
| `Scene` | `id, projectId, episodeId, storyboardId(nullable), currentVersion, versions[], revision, createdAt` |
| `SceneVersion` | `version, heading, location, timeOfDay, synopsis, characterIds[], **dialogue[]**(E1 追加，默认空), shotCount, durationSeconds, createdAt` |
| `SceneDialogue` | `text, speaker(对白归属的姓名，可空), paragraph(该对白所在段落序号)` |
| `Episode` | `id, projectId, title, styleId(nullable), currentVersion, versions[], **consistencyReviews[]**(E2 追加，默认空), revision, createdAt` |
| `EpisodeVersion` | `version, targetDurationSeconds, shotBudget, scenes[{sceneId, sceneVersion}], sources[], textSplitContract, createdAt` |
| `ConsistencyReview` | `id, characterId, characterVersion, episodeVersion, score(1–5), note(nullable), reviewer, shotIds[], storyboardIds[], assetIds[], consistent, createdAt`（E2 追加） |
| `ShotBudget` | `shotCount, targetDurationSeconds, sceneMaxShots, jobCap, perShotAmount, currency, episodeCapAmount, priceVersion` |

不变量（Store 每次提交都重新校验，不只校验新写入）：

1. `versions` 必须是 1..N 连续序列，`currentVersion == len(versions)`；`revision` 单调不减。
2. `Scene.episodeId` 与 `Episode` 的 `projectId` 必须一致；`EpisodeVersion.scenes` 不能重复引用同一场景，且只能引用**同一分集**的场景。
3. `sum(scene.shotCount) == shotBudget.shotCount`，`sum(scene.durationSeconds) == targetDurationSeconds`；任一不满足分别返回 `EPISODE_SHOT_BUDGET` / `EPISODE_DURATION_LIMIT`。
4. 单场景 `shotCount ≤ sceneMaxShots`（默认 6），且 `durationSeconds` 落在 `[2×shotCount, 15×shotCount]`（沿用镜头 2–15 秒硬边界）。
5. `shotBudget.jobCap ≥ shotCount` 且 `perShotAmount × shotCount ≤ episodeCapAmount`，否则该预算不可保存。
6. `Scene.storyboardId` 只能从 `null` 绑定一次；一个分镜只能属于一个场景（`SCENE_CONFLICT`）。
7. `Character.referenceAssetIds` 必须指向**同项目素材库中的图像**（`CHARACTER_ASSET_MISSING`）；关系双方必须同项目且不得自指（`CHARACTER_CONFLICT`）。
8. `Episode.styleId` 已设置后不可更换；`sources[]` 沿用既有 `ArtifactRef` 版本引用校验。
9. `StyleVersion` 不含字幕样式、音轨、转场或画幅字段；这些属于 M6，不得提前塞进 v6。
10. **E1 追加**：`SceneVersion.dialogue[]` 把台词冻结在场景版本里，`speaker` 是对白归属的姓名（还不是 `characterId`）；不带该字段的 E0 记录继续有效，默认空数组。
11. **E2 追加**：`Episode.consistencyReviews[]` 只追加不改写。每条评分绑定当时的角色版本、分集版本、镜头、分镜与参考素材，并记录 `consistent` 证据；角色必须同项目，引用的分集/角色版本必须已存在。E0/E1 记录不带该字段仍有效，默认空数组。
12. **E2 追加**：`Shot.characterRefs[]` 冻结每个参考帧来自哪个角色版本；`Shot.referenceAssetIds` 与 `characterRefs` 一一对应。Store 每次提交都重算该角色版本的第一个参考图，与镜头记录不一致即 `CHARACTER_ASSET_MISSING`，因此不能手工改写素材绑定。

E1 的 `episode_save` 必须在**一个 Store 事务**内写入场景、分集与引用它的圣经记录：场景校验要求其分集存在，分集预算校验要求其场景存在。E1 的 `episode_plan`/`episode_save`（`adaptation.py` + `episodes.py`）已按此实现：同一 operation key 重放原结果，未变化的场景不产生新版本，草稿不落盘而是在读取时按 `text-split-zh-v1` 重新推导。

### 2.1 场景与分镜

`Scene → Storyboard` 是 E1 的显式动作，不是保存场景的副作用：

- 保存分集只写 `Episode/Scene`，不创建分镜、不登记任何 Job；
- E2 起，用户或 Agent 在 `storyboard_save` 传 `sceneId`，与保存分镜同一事务把 `storyboardId` 绑定到场景；绑定后场景的后续版本仍可改 `shotCount`/`synopsis`，但不能换绑到另一个分镜，同一分镜也不能再绑定第二个场景；
- 成片仍按 `storyboardId` 拼接（M3/M4 语义不变）；一集多场的成片由 M6 的组合成片负责，M5 不引入“整集成片”对象。

## 3. v5→v6 迁移与历史恢复

迁移对已验证的 v5 状态做深拷贝，仅加入四个空集合并改 `schemaVersion=6`。已有 media（含 `FilmMedia` union）、Film/FilmVersion（含 `normalization`）、Job、Run、Operation、Wait、Storyboard、Asset 的 JSON 值全部不变，不补字段、不重算指纹。迁移前保留原始字节备份 `state-v5-UUID.json`，验证新外壳后原子替换；失败原文件不动，重开不二次迁移，SQLite 检查点、锁与旁车文件不动。

schema 1–5 校验器全部保留（`DatabaseV1..V5`），当前 `Database` 指 v6；未知版本拒绝打开。打开时对 `jobs/media/storyboards/assets/films/episodes/scenes/characters/styles` 全部重跑领域校验器，任何损坏的跨对象引用都返回 `INVALID_STORE` 且不写备份。

E0 提交 [v5 合成基线](../tests/fixtures/m5/manifest.json)：两份历史夹具机械包入 v5、一份 M3 当前分镜夹具、一份 M4 成片夹具（含真实归一化成片记录与两份已交付片段）。它不包含像素文件、凭据或可恢复 SQLite 检查点。E1 必须补当前签名 Run 的检查点恢复证据，不能把 E0 的机械哈希预期称为已实现恢复。

## 4. 文本改编契约 `text-split-zh-v1`

输入先限定为纯文本 `txt`/`markdown` 字符串与它的 `Artifact` 版本引用；单篇上限 **20000 字符**（`EPISODE_SOURCE_TOO_LONG`）。切分必须**确定性**：同一输入与同一 `textSplitContract` 产生逐字节相同的草案，无随机、无模型、无时间戳参与指纹。

1. **归一化**：去 BOM、统一 CRLF/LF、全角空白转半角、压缩连续空行。归一化结果参与草案指纹。
2. **章节切分**：Markdown 标题（`#`…`######`）、`第N章/节/回`、整行分隔线 `---`/`***`。无标记时整篇为一章。
3. **场景切分**：按空行分段聚合成 beats，地点/时间标记（`日/夜/清晨/黄昏`、`内景/外景`）与对白密度决定边界；单场景镜头数不超过 `sceneMaxShots`。
4. **对白抽取**：中文引号（`“”`/`「」`）与 `角色：台词` 两种形式；对白必须**不丢**，抽取失败返回 `TEXT_SPLIT_FAILED`，不得静默丢弃。
5. **镜头草案**：每个场景按 beat 切 2–4 个镜头；镜头描述只能由场景 synopsis、地点、时间与角色外观拼装，不得编造未出现的角色或地点。
6. **预算分配**：先按场景分配镜头数使合计落在 10–20，再按镜头数分配时长使合计落在 60–90 秒且每镜头 2–15 秒；无法同时满足时整体失败（`EPISODE_DURATION_LIMIT` / `EPISODE_SHOT_BUDGET`），不自动放宽。

草案是只读输出：`episode_plan` 返回结构、逐镜头明细与估算，**不写 Store、不登记 Job**。只有 `episode_save`（显式确认）才持久化。

## 5. 镜头、时长与预算

| 项 | 冻结值 |
|---|---|
| 每集时长 | 目标 60–90 秒（`targetDurationSeconds`），硬边界同值 |
| 每集镜头 | 10–20（硬上限 20） |
| 单场景镜头 | ≤ 6 |
| 单镜头时长 | 2–15 秒（沿用 `SHOT_DURATION_MIN/MAX`） |
| 场景数 | ≤ 12 |
| 每集新 Job | ≤ **20** |
| 每集成本 | ≤ **60.00 CNY**（20 × 3.00） |
| 逐镜头估算 | Wan `0.60 CNY/秒 × 5 秒 = 3.00 CNY`，`priceVersion` 沿用 `wan27-beijing-720p-20261010` |

**与既有护栏的关系**：`LIVE_JOBS_PER_RUN=4`、Wan 每 Job 3.00 / 每 Run 12.00 CNY 继续约束 6/6 及更早工具面的 Run，不得被追溯放宽。E3 为 7/7 引入集级额度时，两套上限同时生效：同一 Run 内仍是逐条 Job 上限，而按集批量登记以 `jobCap` 与 `episodeCapAmount` 为整批拒绝边界。

- 超限时**整批拒绝**，逐条列出超限镜头与桶，不部分登记；
- `unknown`/失败尝试仍保守占用预留额，不因报错释放额度后重提；
- 实际账单在取得凭据前始终 `unknown`，估算不得称为扣费；
- 重做只由用户显式发起，且只影响目标镜头（沿用 M2 语义）。

**E3 实现细化（2026-10-22，9/9）**：E3 把上述集级额度落到 9/9 工具面，证据见 [E3 验收](./M5_E3_ACCEPTANCE.md)。

- **两个新工具**：`episode_generate`（整集批量登记，`confirm=true` 必填）与 `shot_redo`（只由用户显式发起的逐镜头重做，带分镜 `expectedRevision`）。`await_job` 的集合等待上限由 16 提到 **20**，因此一集最多 20 个任务可在一次等待里交付。
- **计划即权威**：登记事务内重算分集 → 场记 → 分镜 → 镜头；任一场记未绑定分镜用 `SCENE_CONFLICT`，已绑定分镜的镜头数或时长与冻结场记不一致分别用 `EPISODE_SHOT_BUDGET` / `EPISODE_DURATION_LIMIT`，全部整批拒绝。分镜镜头数、镜头时长与分集总数必须与冻结 `ShotBudget` 一致，因此“登记的就是预算里的”可证明。
- **集级额度跨调用累计**：按 Job 的 `shotRef.storyboardId` → 场记 → 分集归属，统计该项目内**所有 Run** 的已登记真实 Job（含重做、`unknown` 与失败任务，失败不释放）。`jobCap`、`episodeCapAmount` 同时生效；任一越界返回 `EPISODE_COST_LIMIT` 并列出超限镜头与桶。集级额度只按冻结的 CNY 记账，**不换汇**：`Kling`/`fal` 的 USD 报价、或每秒单价与集冻结 `perShotAmount/5` 不一致时同样整批拒绝（`EPISODE_COST_LIMIT`）。
- **额度快照落在 Job 上**：`RunLimitsSnapshot` 增加可选 `scope=run|episode` 与 `episodeId`（默认 `run`，旧记录逐字节不变）。集级 Job 的 `cost.limits` 记录该集冻结的 `jobCap`/`episodeCapAmount`/已用与余量；Store 每次提交都按整库重算集级任务数与估算，并核对 `shotRef` 必须指向该集已绑定分镜、币种与单价必须与冻结预算一致，篡改返回 `JOB_CONFLICT` / `EPISODE_COST_LIMIT`。该字段是同一快照类型的加法扩展，不新增 Job 契约版本。
- **9/9 的 Run 额度**：9/9 Run 可以把一集的整批 Job 放在同一 Run；这些 Job 不再受每 Run 4 个限制，改由集级 `jobCap` 约束。同一 Run 内其余非集级真实 Job 仍受冻结的每 Run 4 个限制，6/6 及更早工具面的 Run 完全不变。
- **`episode_read` 的 `generation`**：返回 `ready/problems`、逐镜头 `unregistered/pending/delivered/failed` 状态（含 `jobId`、`shotVersionId`、`mediaId`、`estimate`、`error`）、逐场分镜与集级余量。读取路径不因场记未绑定或分镜不一致而失败，只如实报告问题。

## 6. 角色与风格一致性

E2 已按下列规则实现，证据见 [E2 验收](./M5_E2_ACCEPTANCE.md)。

- **场景绑定**：`storyboard_save` 可用 `sceneId` 在保存分镜的同一事务里绑定该场记（1:1）；一个场记只能绑定一个分镜，已绑定后不可换绑（`SCENE_CONFLICT`）。未绑定的场记不参与风格与一致性计算。
- **角色引用**：镜头通过有序 `characterIds` 引用角色，数量必须等于该模式的参考帧数（`i2v` 1 个、`flf2v` 2 个，`t2v` 0 个）；服务端取每个角色最新版本的第一个 `referenceAssetIds`，按同一顺序产出 `Shot.referenceAssetIds`，并把 `{characterId, characterVersion}` 冻结进 `Shot.characterRefs`。同一镜头不能同时给 `characterIds` 与 `referenceAssetIds`。
- **缺素材整批拒绝**：角色没有参考图、参考图不在本项目素材库或不是图像时返回 `CHARACTER_ASSET_MISSING`，整次保存不写入任何场记/分镜；不降级为 `t2v`，不借用其他项目素材。
- **同一组素材**：`episode_read` 逐角色重算该分集所有已绑定分镜的镜头证据（镜头 ID、分镜 ID、角色版本、参考素材），`consistent` 仅在全部镜头指向同一素材时为真。角色圣经追加新版本不会改写已冻结的镜头；新旧版本混用会如实报告 `consistent=false`。
- **风格模板**：绑定场记后，镜头请求提示词按 `style-prompt-zh-v1` 固定顺序拼装：`【风格】名称｜题材：…｜色调：…｜镜头语言：…`，随后是 `【出场】` 有序角色名，最后是镜头描述。同一分集、同一角色版本与同一描述产生逐字节相同的提示词；仍受供应商提示词长度上限约束。
- **人工评分**：`consistency_review` 只在用户明确给出 1–5 分时记录；它在同一事务内重算证据，追加 `ConsistencyReview`（含角色/分集版本、镜头、分镜、素材与 `consistent`），没有引用该角色的镜头时返回 `CONSISTENCY_EVIDENCE_MISSING`。评分不改变镜头、任务、预算或费用，历史评分不可改写（`EPISODE_IMMUTABLE`）。
- 不做 LoRA/角色微调、不做声音克隆；`VoiceProfile` 只是描述性字段，不含 TTS 供应商、音色 ID 或授权。

## 7. 错误码

新增（不与既有码同义重复）：

| 错误码 | 触发 |
|---|---|
| `EPISODE_NOT_FOUND` / `EPISODE_CONFLICT` / `EPISODE_IMMUTABLE` | 分集不存在、ID/项目不匹配、历史被改写或删除 |
| `EPISODE_SOURCE_TOO_LONG` | 源文本超过 20000 字符 |
| `EPISODE_SHOT_BUDGET` | 镜头数或逐场镜头之和越界 |
| `EPISODE_DURATION_LIMIT` | 时长或逐场时长之和越界 |
| `EPISODE_COST_LIMIT` | 集级成本上限不足（登记阶段整体拒绝） |
| `SCENE_NOT_FOUND` / `SCENE_CONFLICT` / `SCENE_IMMUTABLE` | 场景缺失、分镜重复绑定、历史被改写 |
| `CHARACTER_NOT_FOUND` / `CHARACTER_CONFLICT` / `CHARACTER_IMMUTABLE` | 角色缺失、关系非法、历史被改写 |
| `CHARACTER_ASSET_MISSING` | 角色参考素材缺失、不是本项目的图像，或与镜头冻结的引用不一致 |
| `STYLE_NOT_FOUND` / `STYLE_CONFLICT` / `STYLE_IMMUTABLE` | 风格缺失、跨项目、历史被改写 |
| `TEXT_SPLIT_FAILED` | 切分无法产出完整场景/对白 |
| `CONSISTENCY_EVIDENCE_MISSING` | 记录评分时该角色在分集已绑定分镜中没有任何镜头 |

沿用：`VERSION_CONFLICT`（分集/场景 revision 竞争）、`VIDEO_RUN_COST_LIMIT`、`VIDEO_COST_LIMIT`、`SOURCE_NOT_FOUND`、`SOURCE_VERSION_NOT_FOUND`、`CONFIRM` 语义走工具参数而非新码。

E3 未新增错误码：集级任务数（`jobCap`）或金额（`episodeCapAmount`）耗尽、币种或单价与冻结预算不一致，统一返回 `EPISODE_COST_LIMIT`（“集级额度不足，整批拒绝”），消息中列出超限镜头与桶；未绑定分镜的场记沿用 `SCENE_CONFLICT`，分镜与冻结场记的镜头数/时长不一致沿用 `EPISODE_SHOT_BUDGET` / `EPISODE_DURATION_LIMIT`。

## 8. 明确排除

- **9:16 竖屏与竖屏供应商能力**：不新增画幅/裁切字段，不选 mock 的 `1080p/9:16` 条目；竖屏是后续独立话题。
- 转场、字幕、配音、配乐、响度、片头片尾、1080P 导出：属 M6。
- Episode/Season 多集队列、时间线工作台、批量重试：属 M7。
- Kling/fal 的 i2v/首尾帧、Sora/Veo/Seedance/Replicate：不在 M5。
- LoRA/角色微调、声音克隆、明星音色。
- PDF/网页导入、跨语言改编。

## 9. 版本号消歧（与 M5_PLAN 的差异）

`M5_PLAN` §4.2/§7 把 M6 的“成片版本 v6”与 M5 的 `schemaVersion v6` 写成同一个 v6。本文冻结如下解释：

- **M5 独占 `schemaVersion 6`**；
- M6 若需要新的存储形状，必须用 `schemaVersion 7`，不能复用 v6；
- M6 的“成片版本 v6”指**成片/剪辑输出序号**（cut/timeline 版本），与 `schemaVersion` 不是同一个编号空间，不得互相代替。

## 10. 交付检查

- **E1**：`text-split-zh-v1` 确定性、对白零丢失、预算合成、`episode_plan`/`episode_save`/`episode_read` 工具与 7/7 冻结变体、6/6 与 7/7 共存恢复、v5→v6 真实迁移与字节备份、3 篇固定样例离线可复现（模型调用=0）。
- **E2**：角色圣经与参考图导入、跨镜头一致性引用、缺素材拒绝、人工一致性评分记录。已交付：场景绑定、`characterIds`→冻结素材、`style-prompt-zh-v1`、`episode_read` 证据与 `consistency_review`（tools/rules 8/8，冻结 7/7），见 [E2 验收](./M5_E2_ACCEPTANCE.md)。
- **E3**：集级批量登记与预算原子性、逐镜头重做隔离、集合等待、失败隔离、submit 与预算一致（真实样本需单独授权）。已交付：`episode_generate`/`shot_redo` 与 tools/rules **9/9**（冻结 8/8）、集级额度跨 Run 累计与 Store 重算、20 成员集合等待、`episode_read.generation`，见 [E3 验收](./M5_E3_ACCEPTANCE.md)。
- **E4**：离线端到端套件、经批准的最小真实样本成片、CLI/Web 流程与验收文档；未做真实样本的能力保持标注。
