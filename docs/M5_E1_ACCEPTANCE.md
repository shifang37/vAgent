# M5 E1 验收：确定性文本改编

日期：**2026-10-22**。基线：`master` `8fe1db5`（M5 E0 契约与 schema v6 已合入）。范围见 [M5 计划](./M5_PLAN.md) §3.2 E1，契约见 [M5 契约](./M5_CONTRACTS.md) §4。**E1 全程离线：模型调用=0，视频 submit=0，未产生任何费用。**

## 1. 已交付

| 项目 | 交付 |
|---|---|
| 切分算法 | `src/vagent/video/adaptation.py`：`text-split-zh-v1` 的章节切分、场景切分、对白抽取、角色识别、镜头草案与预算分配；全部为纯函数，无随机/时钟/ID/文件系统依赖 |
| 台词持久化 | `SceneVersion.dialogue[]`（**E1 追加的兼容字段**，默认空；E0 记录不带该字段仍可读），台词按场冻结，后续镜头/字幕不必再猜 |
| 领域写入 | `episodes.py`：`save_episode`/`read_episode`/`save_bible`/`read_bible`，沿用 storyboard 的日记式 `store.operation`（同 operation key 重放原结果，不同参数返回 `OPERATION_CONFLICT`） |
| 工具面 | 新增 5 个工具：`episode_plan`、`episode_save`、`episode_read`、`bible_save`、`bible_read`；tools/rules **6/6 → 7/7**（mock 与 live 同步），并冻结 **6/6** 变体供旧 Run 恢复 |
| 样例语料 | 3 篇固定中文文稿（[texts](../tests/fixtures/m5/texts/)）：`rainy-cafe.md`、`last-station.md`、`fridge-notes.md` |
| 验收文档 | 本文件；[M5 契约](./M5_CONTRACTS.md) 的 §2/§4 已同步 E1 的实现细节 |

## 2. 算法与冻结规则

`plan_episode(text, title)` 的实现严格对应契约 §4：

1. **归一化**：去 BOM、CRLF→LF、全角空白、压缩连续空行。
2. **章节**：Markdown 标题（`#`…）与 `第N章/节/回`；连续标题不会产生空章节（无正文的标题重命名当前章节）。
3. **场景**：显式标记（`内景/外景/场景`、`第N场`、`INT./EXT.`）、段落数 ≥6、字符数 ≥900、或时间标记发生切换时切分；非法则整篇报错，不静默合并。
4. **对白**：`「…」` 与 `“…”` 两种引号按出现顺序抽取，归属取最近的 `名字：` 前缀或 `说/问/道/答` 动词；抽取条数与正文引号数量**必须相等**，否则 `TEXT_SPLIT_FAILED`。
5. **角色**：只认对白归属出的说话人（代词与单字被过滤），按首次归属排序，返回台词数与首次出现的场次。
6. **镜头**：每场 1–6 个镜头，先按 beat 给 1–3 个草案，再按顺序补齐/压回 10–20 的总量；镜头描述只使用该场的标题、地点、时间、摘要与台词，不编造未出现的角色。
7. **预算**：目标时长 = 镜头数 × 5 秒并被夹在 60–90；逐场时长按镜头数比例分配且各自落在 `[2×镜头, 15×镜头]`，合计必须精确等于目标；逐镜头时长再按场分配，均落在 2–15 秒。

## 3. 三篇样例的实测结果

| 样例 | 正文字符 | 场景 | 镜头 | 台词 | 目标时长 | 逐镜头估算 | 识别角色 |
|---|---|---|---|---|---|---|---|
| `rainy-cafe.md` | 1,017 | 8 | 19 | 19 | 90 秒 | 57.00 CNY | 陈默、林晚 |
| `last-station.md` | 981 | 9 | 20 | 22 | 90 秒 | 60.00 CNY | 老人、女孩、苏禾 |
| `fridge-notes.md` | 969 | 7 | 18 | 11 | 90 秒 | 54.00 CNY | 母亲 |

- 三篇均落在“1 集 10–20 镜头 / 60–90 秒”的冻结区间内，且逐场镜头数、逐场时长、逐镜头时长同时满足各自硬边界。
- 台词零丢失：三篇的抽取条数与正文引号数逐一相等（19/22/11），测试按文本内容逐条比对。
- 可复现：同一原文重复调用返回**逐字段相同**的草案；草案 JSON 不含时间、ID 或路径。
- 镜头草案可从已保存场景**重新推导**且与规划结果完全一致（`draft_stored_scene`），因此不需要在 Store 里存第二份草案。

## 4. 检查结果

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_m5_adaptation.py tests/test_m5_tools.py tests/test_m5_episodes.py tests/test_m5_baseline.py
.venv\Scripts\python.exe -m ruff check --no-cache .
git diff --check
```

- 新增定向用例：改编 23 项、工具与版本 21 项、E0 域契约 10 项、E0 基线 13 项，共 **67 passed**。
- 版本迁移：新增 Run 记录 7/7；6/6 快照通过 `for_record_features` 还原为不含 5 个新工具的冻结视图（`tests/test_m5_tools.py::test_new_runs_publish_seven_and_old_snapshots_restore`）。既有断言中 14 处“当前版本 6”按机械方式更新为 7，冻结的 3/3、4/4、5/5、6/6 变体断言保留；`test_video_integration.py` 中模拟“规则被改动则拒绝恢复”的补丁目标同步改为当前 `VIDEO_RULES_V7`。
- ruff 与 `git diff --check` 通过。

## 5. 完整回归

```text
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
1189 passed, 3 skipped in 591.95s (0:09:51)
```

判定标准：全部通过、无新增 skip（3 项 skip 仍是既有的“当前 Windows 账号无 symlink 权限”skills 用例）；M3/M4 既有断言只按工具版本号机械更新，未放宽任何行为。

## 6. 明确未做

- **真实生成**：E1 不登记 Job、不提交供应商；摄像机镜头仍需 E2/E3 的 `shot_generate`（按集批量与逐镜头重做属 E3）。
- **场景↔分镜绑定**：`Scene.storyboardId` 的显式绑定留给 E2/E3 的镜头生成流程；E1 不创建分镜。
- **角色外观的创作性补全**：角色识别只给姓名与台词统计，`appearance` 必须由用户或后续阶段补写，模型未参与。
- 9:16 竖屏、字幕/TTS（M6）、多集队列与工作台（M7）不在本轮。
