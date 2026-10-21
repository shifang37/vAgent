# M3 E3 验收：headless 与用户 skills

日期：**2026-10-21**。基线 `e32bb9b`（E2，Job/Request v6、tools/rules 6/6、schema 5）；本包交付 commit 见 `git log`。范围见 [M3 任务规划](./M3_PLAN.md) 的 E3，冻结契约见 [M3 契约](./M3_CONTRACTS.md) §7、§8。**E3 全程视频 submit=0：规划使用脚本编排模型，Kling/fal/Wan 只走受控 `httpx.MockTransport`，媒体来自本地测试 MP4；未读取真实供应商 Key，未创建云端任务，未产生视频费用。**

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 规划阶段 | `src/vagent/headless.py`、`src/vagent/application.py` | `vagent headless PROMPT` 以收窄的分镜工具视图运行普通 Agent Runner：保留 `storyboard_save`/`storyboard_read`/`asset_read`/`video_capabilities` 等，移除 `shot_generate`、`video_generate`、`film_assemble`。规划 Run 仍是普通 completed Agent Run，记录 tools/rules 6/6、模型、上下文签名与事件 |
| 计划对象 | `JobService.plan_shots`（`src/vagent/video/jobs.py`） | 只读推导有序规范请求：`storyboardId/version/revision`、每镜头的 provider/model/mode/时长/请求体/fingerprint、能力/价格/profile 版本、逐镜头估算、逐 `(provider,currency)` 桶金额与 Kling `VIDEO_UNIT`、服务端上限快照；不写 Job/Operation/revision |
| 计划指纹 | `headless_plan_fingerprint` | canonical JSON SHA-256，只含模式、供应商、分镜身份与有序 `{shotId,order,request}`；不含时间、路径、调用 ID |
| 两阶段授权 | `cli.py`、`headless.authorize` | `--plan RUN_ID --plan-fingerprint SHA --confirm --max-cost PROVIDER:CURRENCY=AMOUNT`、`--max-units`、`--max-jobs`；授权只覆盖计划中的桶，必须在任何登记前通过 |
| 原子登记 | `_check_headless_authorization`、`_commit_generation` | 登记事务内重新推导请求并核对计划指纹、当前分镜 revision、逐镜头 request fingerprint、能力/价格版本、Job 数与所有预算桶；任一不符整批不写。登记结果（`shotVersionId/jobId/revision`）与 Job 在同一事务写入 Run 的 `headless.registration` |
| 等待与交付 | `headless.execute` | 登记后复用 `JobWorker`/`MediaWorker` 与 `job_settled`/`jobset_entries` 终态规则等待全部镜头；全部可交付后调用既有 `clients.assemble_film` 与 `FilmService` 本地拼接；输出经 resolver 校验的绝对路径、SHA-256、大小 |
| 恢复 | `headless.execute`、`--resume` | 计划、授权、登记与交付都从 Run 的可选 `headless` 字段恢复；`--resume` 不增额度、不重新授权、不重新提交；授权后登记前崩溃时使用已冻结授权补齐登记 |
| 结构化输出 | `cli.headless_command` | `--json` stdout 只输出一个对象，字段冻结为 `formatVersion,status,projectId,sessionId,runId,planFingerprint,storyboardId,storyboardVersion,storyboardRevision,jobIds,filmId,filmVersion,output{mediaId,path,sha256,sizeBytes},estimates,limits,error`；过程日志写 stderr |
| 退出码 | `headless.EXIT_CODES` | `0` 计划/交付成功，`2` 参数或领域校验，`3` 授权/预算拒绝，`4` 上游/下载/拼接失败，`5` 等待超时，`6` 恢复条件变化，`130` 用户中断 |
| 用户 skills | `src/vagent/skills.py` | 默认「内置根 → `VAGENT_HOME/skills/`」，每根按目录名排序；内置名优先、用户重名 `SKILL_NAME_CONFLICT`；坏用户文件隔离为 `INVALID_SKILL`/`INVALID_SKILL_FILE`；内置+用户合计 ≤32，超出逐条 `SKILL_LIMIT`；显式 `VAGENT_SKILLS_DIR` 保持全量替换与严格报错；raw SHA-256 前 12 位版本、按需读取、metadata 参与 Run 签名不变 |
| CLI/Web 同步 | `src/vagent/cli.py`、`web/app.js`、README | `vagent headless`、`vagent skills list`（`skills` + `diagnostics`）；观测面板显示未加载的用户 skill 诊断与默认发现规则；README 新增 headless 用法、JSON 字段与退出码 |

## 2. 关键实现决策（相对契约的细化）

1. **规划工具集收窄而不是更换工具版本。** 计划 Run 继续记录发布中的 tools/rules 6/6 与真实能力表，只把三个会写生成/成片状态的工具从该 Run 的可调用集合中移除。这样不需要新增工具版本或改写历史签名；代价是该 Run 的完整上下文签名与普通 6/6 Run 不同，普通 `vagent resume RUNID` 会以 `RESUME_CONFIG_CHANGED` 拒绝，headless 继续流程必须使用 `vagent headless --resume RUNID`（已记录在 README 与 CLI 帮助中）。
2. **计划状态保存在 Run 的可选 `headless` 字段。** 不新增顶层集合、不升级 schema v5、不迁移历史记录；未使用 headless 的 Run 不写该字段。字段包含 `status/plan/planFingerprint/authorization/registration/delivery` 与时间戳，所有写入都与同一 `state.json` 事务提交。
3. **授权与登记分两个事务。** 授权事务先核对计划指纹、分镜 revision、当前请求/价格/能力快照与预算覆盖，并把授权指纹写入 Run；登记事务再完整核对一次，并把 Job、ShotVersion、`registration` 与操作结果原子提交。这样授权后崩溃可以由 `--resume` 用原授权补齐，而不是要求用户重新给出限额；仍满足“任何视频登记前拒绝不符项、登记事务内原子核对”的要求。
4. **等待复用既有终态原语，不保存第二个中断等待。** headless 没有需要唤醒的 Agent 图，因此不登记 `WaitBinding`；它复用同一 `JobWorker`/`MediaWorker`、`job_settled` 与 `jobset_entries` 判定，并复用 `ApplicationService.subscribe` 的通知。Job 与媒体仍由既有持久 Worker 推进，进程退出后保留；恢复时只重新等待尚未完成的已登记 Job。
5. **预算只覆盖计划桶。** 金额经 `Money` 规范化为两位十进制字符串；计划未使用的桶、未知供应商/币种、负数/非有限值、低于计划估算、超过当前服务上限、缺少 Kling units 或 `--max-jobs` 低于计划新任务数均拒绝。`--max-jobs` 默认取服务端共享上限（4），只能收紧。
6. **交付前再次核对来源。** 登记后要求分镜 revision 等于登记时 revision、每个镜头最新 `ShotVersion` 等于登记时版本；否则返回 6，不等待、不拼接。交付使用确定性 `clientRequestId`（按计划 Run 派生），拼接结果与成片媒体在同一次 Film 事务中只增不改。
7. **Wan 与 Kling/fal 共用同一流程。** Kling/fal 走 v6 的逐 provider/currency 与 units 桶；Wan 仍使用冻结的 CNY `RunLimitsSnapshot`，headless 在同一登记事务内按它的 `usedAmount/usedNewJobs` 核对，不把 Wan 重编码为 v6。
8. **skills 合并只影响新 Run。** 用户根按需扫描；内置根保持原有严格报错，用户根不存在的视为空，坏文件只产生诊断。新增/修改 skill 改变 catalog 元信息，因此旧 Run 恢复返回 `RESUME_CONFIG_CHANGED`，不会把新文件注入旧 Run 的上下文。headless 的 `authorize` 与 `--resume` 也会用记录的工具视图、skills 与上下文重建签名；变化时返回 6，不做登记、不继续等待。

## 3. 验证结果

### 3.1 新增测试

| 文件 | 项 | 覆盖 |
|---|---|---|
| `tests/test_m3_headless.py` | 21 | 规划只保存分镜、冻结请求/估算/指纹、Job=0、供应商请求=0；规划工具集不含三个写工具；无分镜与 off 模式拒绝；9 组预算拒绝（缺桶、低于估算、超 Run 上限、Kling units 少/多、未知桶、负金额、max-jobs 过小/过大）；指纹不匹配与未确认拒绝且不登记；Kling 2 镜头端到端（授权→登记→集合等待→本地拼接交付，输出路径/哈希/大小，重复 execute 只读重放，重复授权拒绝）；等待超时保留 jobIds 后 `--resume` 只查询不重提；授权后修改分镜返回 6；退出码分类；Wan CNY 桶端到端；取消等待保留已登记 Job 且不强制提交；mock 模式本地登记无付费桶、无媒体时拒绝拼接；新增用户 skill 后授权以 `RESUME_CONFIG_CHANGED` 拒绝 |
| `tests/test_m3_headless_cli.py` | 3 | 真实 CLI 两次调用：规划 JSON（6 个固定字段集）→ 授权执行交付，`--resume` 重放同一 output；未确认/错指纹/预算不足/预授权/未知计划/非法 max-jobs/off 模式的退出码 2/3；等待 0.3 秒超时返回 5 并保留 2 个 jobIds，恢复后交付且提交数仍为 2 |
| `tests/test_m3_skills.py` | 9（1 项跳过） | 默认合并顺序、缺失用户根、内置名冲突诊断、坏文件隔离（非法 frontmatter/超长/符号链接）、32 合计上限与逐条 `SKILL_LIMIT`、显式根全量替换与严格报错、新增 skill 改变签名并拒绝旧 Run、按需读取仍只从合并 catalog 返回正文。跳过项仍是当前 Windows 账户无符号链接权限的既有边界 |

### 3.2 全量与门禁

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js && node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs
```

- 全量：**1083 passed / 3 skipped**（约 7 分 45 秒；3 项 skip 中 1 项为本包 `tests/test_m3_skills.py` 的符号链接用例，另 2 项为既有 Windows 账户无符号链接权限的用例）。E3 新增 3 个测试文件共 **33 项**（21 + 3 + 9），其中 32 项通过、1 项按环境跳过。
- Ruff check/format：通过（146 个文件已格式化）；前端 `node --check web/app.js` 与 `node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs` **31 项**通过，0 失败。

### 3.3 零费用与受控边界

- 规划阶段的脚本模型只调用 `storyboard_save`；测试断言 `jobs`、`media` 与 `operations` 中没有生成操作，供应商 transport 的 POST 计数为 0。
- 授权本身不登记、不提交；只有授权后的首次 `execute` 才登记 Job，提交仍由既有 `JobWorker` 对受控 Kling/Wan transport 完成。
- 交付只用受控媒体（`tests/fixtures/m1c/sample-720p.mp4` 或本地导出的媒体）和包内 FFmpeg；拼接不产生供应商请求。
- 所有供应商响应均为 `httpx.MockTransport`；没有真实 DNS/TLS 连接，没有读取真实 Key，也没有创建云端任务。

## 4. 未完成与后续（E4）

- 真实编排模型（DeepSeek）下的 headless 计划未做真实样本；E3 只验证脚本编排与受控供应商。真实计划可能产生文本费用，E4 的离线/安装态验收仍需显式授权。
- 真实 Kling/fal/Wan 账号的鉴权、地域可达性、编码兼容、CDN 下载与账单仍未验证；headless 的真实付费路径留给 E4 的显式额度样本。
- 受控浏览器中的 headless 交互（计划核对界面）不在 M3 范围；Web 只同步了用户 skill 诊断说明。
- `scripts/evaluate_m3.py`、wheel smoke 扩展、四组 CI、`docs/M3_ACCEPTANCE.md` 与 README 全量验收数据属于 **E4**。
- **E4 已完成**：离线套件的 headless 计划（submit=0）与显式授权交付（2 Job→本地成片、重放不重提）均通过，安装态 6 用例套件 PASS；真实编排模型与真实付费授权仍未执行。见 [M3 验收](./M3_ACCEPTANCE.md)。

## 5. 失败与偏差记录

- 开发开始时，本机 `pytest` 临时目录与 `.pytest_cache` 出现 `WinError 5`。用随附的 Windows 权限诊断脚本检查 `pytest-of-*` 与所有祖先：`WRITE_DAC`/`WRITE_OWNER` 均可用、没有包允许项，诊断为 `NOT_THIS_CLASS`（未做任何修改）。随后本会话的文件策略切换为完全访问，完整测试正常执行；该问题属于宿主环境权限，不是本包代码失败。
- 一处实现取舍需要明确：授权事务与登记事务分开（见 §2.3）。这样做是为了让“授权后、登记前崩溃”可用原授权恢复；登记事务仍逐项重核对，不存在“先登记后补授权”的窗口。
- headless 不在等待时创建 `WaitBinding`（见 §2.4）。验收以 Job/媒体 Worker、持久 Job 与 Run 内 `registration` 为恢复证据；没有新增第二套外部等待状态机。
- headless 规划 Run 的普通 `vagent resume` 会因收窄工具集改变签名而返回 `RESUME_CONFIG_CHANGED`（见 §2.1）。这是有意的：headless 流程只用 `vagent headless --resume` 续接，普通恢复不会把计划 Run 当成对话 Run 重放。
- 本包未使用真实供应商样本，因此所有结论都只是受控 HTTP 与本地媒体证据，不称为账号已验证；真实账单与实际编码兼容性仍为未知。
