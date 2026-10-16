# M2 D1 验收：schema v4、分镜域模型与工具

完成日期：**2026-10-16**。基线：D0 提交 `0b4eb0c`（codex/m2-d0），实现分支 `codex/m2-d1`。契约与范围见 [M2 契约](./M2_CONTRACTS.md)与 [M2 任务规划](./M2_PLAN.md)；官方证据见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 9 节。

**D1 全程离线：视频 submit=0，未发起真实视频或模型调用，未上传素材；真实多镜头联调留待 D4。**

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| schema v4 与 v3→v4 迁移（只新增 `storyboards`、`assets`） | 完成 |
| Storyboard/Shot/ShotVersion/Asset 域契约与提交级不变量 | 完成 |
| `storyboard_read` / `storyboard_save`（mock 视图，工具与规则 4/4） | 完成 |
| `executionVersion=3` 与 pre-M2 Run 原配置恢复 | 完成 |
| Mock 多镜头分镜轨迹（Runner 级与应用级） | 完成 |

不在 D1（按计划进入后续工作包）：`shot_generate` 批量登记、三重费用上限、集合等待（`await_job` v4）、live 的 4/4 协议与能力表 v2、素材导入与素材库页。

## 2. 版本冻结落地

- `schemaVersion`：新建与迁移为 **4**；1/2/3 的只读校验器（`DatabaseV1`/`DatabaseV2`/`DatabaseV3`）与逐字节备份流程保留。
- `executionVersion`：`WAIT_EXECUTION_VERSION=3`；1/2 的校验器与恢复路径不改写，2/3 统一进入现有等待机制（集合语义待 D2）。`CHECKPOINT_VERSION` 保持 1，`contextVersion` 保持 2。
- mock 视图：`toolsVersion/rulesVersion=4`（新增分镜工具）；pre-M2 的 1/1、2/2 变体按执行版本注册并**排除**新增工具名，恢复时字节等于历史配置。
- live 视图：D1 保持 **3/3** 与 `wan27-t2v-beijing-v1` 不变；4/4、`wan27-shot-beijing-v2`、`wan-http-v2` 随 D2 协议一并实施。
- 价格、`sourcePolicyVersion`、`validationVersion` 与既有 Job 生命周期均未改动。

## 3. 迁移证据（夹具与逐字节备份）

固定夹具 `tests/fixtures/m2/legacy-v3-{mock,live}.json`（D0 生成，manifest 固定哈希）：

- 打开前逐字节校验 `rawSha256`，并按 v3 语义通过原校验器。
- 迁移后：`schemaVersion=4`、`storyboards={}`、`assets={}`，全量 canonical 指纹等于 `expectedV4CanonicalSha256`；每个历史集合的独立指纹等于 `preservedCollectionsSha256` 且与原始 JSON 逐项相等；Job `requestFingerprint`、Operation 指纹与 mediaId 保持。
- 备份：`state-v3-<UUID>.json` 逐字节等于原文件；重开不再迁移、不产生第二份备份；目录内的 checkpoint 哨兵文件不被触碰。
- 失败注入：备份阶段与替换阶段失败均保留原 `state.json`、释放 `instance.lock`、无残留临时文件。
- 未知版本（0/5/布尔/字符串/null）与不完整 v4 信封均拒绝打开，不产生备份。
- v1/v2 旧夹具继续迁移到 v4：M1-C 迁移测试更新为 v4 终态，同时以“去掉两个新集合后的 v3 形态”复核冻结的 `expectedV3CanonicalSha256`——历史证据语义不变。

测试：`tests/test_m2_migration.py`（7 项）、更新后的 `tests/test_m1c_migration.py` 与 `tests/test_storage_migration.py`。

## 4. 分镜域与工具行为

- 契约要点：`Shot.order` 连续 1..N、`description` ≤2000 字符、`framing` 六枚举、`durationSeconds∈[2,15]`、`mode∈{t2v,i2v,flf2v}` 且参考图数量 0/1/2 一致；`StoryboardVersion` 版本连续、镜头 ID 唯一；`Storyboard` 版本只增不改、`currentVersion` 指向最新；`ShotVersion` 必须引用本分镜既有镜头；`Asset` 路径由服务端 ID 构造、同项目同哈希唯一。
- `storyboard_save`：新建产生版本 1；修改需 `storyboardId+expectedVersion`，过期即 `VERSION_CONFLICT` 且无副作用；显式传入既有 shotId 保持身份，未知/重复 ID 拒绝（`SHOT_NOT_FOUND`/`SHOT_DUPLICATE`）；`sourceRefs` 限当前项目冻结版本，省略则保留原引用；引用素材允许同一数据目录内跨项目复用，但必须存在且为图像；任一校验失败整次不写入。
- `storyboard_read`：省略 `storyboardId` 取当前项目最新；跨项目与不存在返回 `NOT_FOUND`；输出当前版本有序镜头与各镜头版本摘要（`versions`）。
- 工具重放：同一 operationKey+参数返回原结果且不追加版本；同 Key 改参数返回 `OPERATION_CONFLICT`；只读视图隐藏写工具。
- Store 提交级不变量：删除分镜、改写既有版本或改写素材一律拒绝（`STORYBOARD_IMMUTABLE`/`ASSET_IMMUTABLE`/`ASSET_CONFLICT`），与 Job/媒体不变量一同在每次事务校验。

测试：`tests/test_storyboards.py`（23 项）。

## 5. 旧 Run 与签名恢复

进入实现前（`92a4a87`）捕获 8 组 contextSignature（mock/live × context v1/v2 × 只读）与两组 toolFeatures JSON，固定于 `tests/test_m2_compatibility.py`：

- `for_execution_version(2)` 恢复的 mock 工具集与签名**逐字节**等于捕获值（新分镜工具被排除）；live 3/3 同样保持。
- 完整回归：以 execution v2 录制 mock Run → 关闭重开 → 默认（v3）Runner 经 `for_record` 恢复，原签名、原 toolFeatures、原规则文本不变，模型步数与工具计数符合预期。
- 既有 B0/B2/B5 兼容套件（文本工具 v1、mock 1/1、离线检查点）与等待兼容测试保持通过。

测试：`tests/test_m2_compatibility.py`（10 项）。

## 6. 门禁与回归

- 离线全量：**839 passed / 2 skipped**（M1 基线 794；2 项跳过仍为 Windows 账户无符号链接权限）。
- `ruff check` 与 `ruff format --check` 全库通过；wheel 构建成功，独立 wheel 环境的 `scripts/wheel_smoke.py` 完整通过（含 M1-B/M1-C 兼容、live 协议夹具与媒体证据）。
- Mock 轨迹：`tests/test_m2_integration.py` 三镜头“保存→读取→改版保留身份”的 Runner 级与应用级轨迹通过，`toolFeatures.video=4/4`、`executionVersion=3`；off 模式不出现分镜工具。
- 四组 CI（ubuntu/windows × 3.11/3.12）在实现补提交 `5eff0f3` 上全部通过（check runs `116955320579` / `116955320160` / `116955320530` / `116955320660`）；D0 分支提交 `0b4eb0c` 四组同样全绿。验收级汇总留 D4。

## 7. 失败与偏差记录

- 首轮新增用例 18 项失败，均为测试侧适配问题（缺 `ensure_session`、camelCase 键名、计数断言、只读工具集合预期），逐项修正后全绿；实现代码未因这些失败变更语义。
- `VIDEO_RULES_V4` 文本为 D1 定稿的过渡版本（含分镜指引，尚未含批量生成/重做条款）；D2 将在同一版本号下扩展并以 D2 验收为准。因此 D1 开发构建录制的 4/4 运行不承诺跨 D2 恢复（预发布工件边界）；**pre-M2（execution v2）运行按冻结契约保持可恢复**。
- `scripts/wheel_smoke.py` 需安装在独立环境中的 wheel 才能运行（仓库内可编辑安装被显式拒绝）；本地已按此方式验证通过，CI 矩阵与 D4 安装流程另行覆盖。
- 首轮 D1 CI（`9542d5e`/`f7b0b33`）3/4 通过；windows-latest 3.12 在既有计时测试 `test_timeout_cancels_hanging_model` 上失败：0.3 秒策略超时在慢 runner 上于挂起模型调用开始前到期，取消路径未运行、`stopped` 未置位。按仓库 liveness 惯例修复（`5eff0f3`：放宽为 5 秒守卫，并断言模型确已启动，使同类停顿在失败时给出明确信号），复跑四组全绿。

## 8. 留给 D2 的入口

`shot_generate` 批量原子登记、三重上限强制与估算回显、集合等待（execution v3 的 jobset 绑定与部分失败摘要）、部分失败隔离、逐镜头重做语义、`wan.py` 多模式协议与受控 HTTP 矩阵、mock 端到端 N 镜头含一次重做；live 切换 4/4 与 `wan27-shot-beijing-v2`。
