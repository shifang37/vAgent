# KEYS_ONLY K1 验收：两 Key 直用的契约与服务端

完成日期：**2026-10-17**。基线：pre-K 代码提交 `cc3628e`、K0 冻结提交 `316a3a4`；实现分支 `codex/m3-e0`。方案与冻结决议见 [两 Key 直用方案](./KEYS_ONLY_PLAN.md)；共享域名鉴权与读取的零费用预研见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 10 节。

**K1 全程离线：视频 submit=0，未发起真实视频或模型调用；共享域名提交路径的真实验证留待 K3 单样本（冻结决议 4）。**

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| 请求契约 `VideoRequestV4`（单镜头）/ `VideoRequestV5`（镜头）：V2/V3 形状减 `workspace_id`，`endpoint_profile="beijing-shared-v1"` | 完成 |
| Job 契约 `JobV4`/`JobV5`（contractVersion=4/5）与 `parse_job`/`StoredJob` 分派 `{1..5}` | 完成 |
| `check_live_configuration` 去掉业务空间必填；`check_runtime` 共享形态仅按地域、模型与适配器恢复 | 完成 |
| `generate` / `_derive_shot` / 重做路径：无业务空间配置时冻结 V4/V5，配置业务空间时维持 V2/V3 | 完成 |
| 适配器按记录形态选主机（共享 `dashscope.aliyuncs.com` / 专属 `{workspace}.cn-beijing.maas.aliyuncs.com`）；`query` 增 `endpoint_profile` 参数 | 完成 |
| 错误映射与文案（鉴权、访问拒绝与恢复提示按两 Key 口径） | 完成 |
| 混合数据目录重启恢复（V2/V3 与 V4/V5 共存、互不影响） | 完成 |

不在 K1（按计划进入后续工作包）：设置页最小表单与高级折叠、保存时零费用探测、Key 与模式免重启生效的端到端用例（K2）；wheel smoke 扩展、四组 CI 与共享域真实样本（K3）。

## 2. 契约兼容（逐字节回归）

- pre-K 提交 `cc3628e` 上以 `tmp/keys-only-capture-pins.py` 采集 V2/V3 指纹（证据 `output/keys-only-prek-pins.json`）：`VideoRequestV2`/`VideoRequestV3` 的 `fingerprint`、`intent_fingerprint` 与 canonical 序列化 SHA-256 在 K1 后**逐字节不变**（固定于 `tests/test_keys_only.py`）。
- V2/V3 请求、校验器与恢复语义未改动；`JobDispatch` 仅新增 `{4, 5}`，历史版本校验不变。
- V4 沿 V2 的 intent 口径（intentVersion 2）；V5 沿 V3 的镜头口径（intentVersion 3，含提示词、规格与媒体清单）；V4/V5 落盘请求均不含 `workspaceId`（受控断言）。

## 3. 受控 HTTP 全链路（共享域）

`tests/test_keys_only.py`（5 项）以受控 Wan 上游（记录主机、路径与请求体）与本地测试媒体驱动真实应用路径：

- 单镜头文生视频：keys-only live 登记产生 `JobV4`；提交打向 `https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis`，请求体逐字段断言（与专属域同一 `wan-http-v1` 适配器体，仅主机不同）；查询、下载（MediaWorker 提交本地 MP4）直至 `succeeded`；关闭重开后按原记录恢复。
- 镜头批与重做：2 个镜头（t2v 与 i2v，含 Base64 `first_frame`）全部在共享域提交；显式重做只新增目标镜头的 ShotVersion 与一个 `JobV5`，重做登记本身不产生提交。
- 适配器形态校验：仅凭视频 Key 通过共享形态（无需业务空间）；专属形态缺业务空间仍 `VIDEO_WORKSPACE_REQUIRED`；缺 Key 仍 `VIDEO_KEY_MISSING`。

## 4. 混合数据目录与恢复

同一数据目录内两代 Job 共存、互不影响（用例三步完整覆盖）：

- 仅两 Key 配置：已登记的 V2 任务提交被阻断（`runtime_block=VIDEO_PROVIDER_UNAVAILABLE`、stage=submit），不静默换主机；V4 任务在共享域正常提交与交付。
- 恢复业务空间配置：V2 任务按原业务空间在专属域自行恢复提交并查询；已交付的 V4 任务不受影响、不再产生新调用。
- 受控断言精确到「该阶段只出现预期主机」。

## 5. 决议 1 可行性验证（模式切换热生效）

结论：**可行，无需退级**；实现与端到端用例按计划在 K2 落地。依据（当前代码路径）：

- 启动即为 off/mock/live 三个模式构建完整工具视图（`ApplicationService.open` 的 `tool_views`），live 视图在启动时已注册（含 WanAdapter，Key 可暂空），不存在「启动于 off 即无 live 工具」的结构性障碍。
- `runner(video_mode=...)` 已按模式逐次选择工具视图；跨模式恢复（`_waiting_runner` 按 Run 记录的模式选视图）本就在运行时走该路径。
- 现状：`save_configuration` 将视频设置保持在启动快照并以 `restart_fields` 如实报告；K2 只需让运行级登记读取已保存设置、并刷新 `JobService.config` 的视频字段与适配器 Key（`WanAdapter` 在每次调用读取当前 Key），模式切换即对新登记生效；在途与历史 Job 仍按各冻结形态恢复（第 4 节已证）。

## 6. 门禁与回归

- 离线全量：**947 passed / 2 skipped**（修复见第 7 节；2 项跳过仍为 Windows 账户无符号链接权限）。
- `ruff check` 与 `ruff format --check` 按 CI 同口径（`src/vagent tests scripts`）通过。
- 新用例：`tests/test_keys_only.py` 5 项。
- 视频 submit=0。
- 四组 CI（ubuntu/windows × 3.11/3.12）在最终提交 `573fe00` 上全部通过（check runs `117426325670` / `117426325971` / `117426325886` / `117426325914`；ubuntu 3.11/3.12 均 946 passed / 3 skipped，windows 3.11/3.12 均 948 passed / 1 skipped；安装态 wheel smoke 四组 PASS）；汇总证据 `output/keys-only-k1-ci-573fe00-summary.json`。

## 7. 失败与偏差记录

- 新增用例首轮 1 项失败（混合目录用例）：循环以 `video_clock.advance(5)` 推进，未跨过 live 任务 15 秒轮询间隔（`job.policy.interval_seconds=15`）即因「无事可做」结束，共享任务首次查询落入第三步、打向共享域——该行为正确（其冻结形态即共享域），失败的是「该阶段仅专属域」断言。属测试驱动缺陷：改为 `advance(20)` 并注明 15 秒间隔；产品代码未因该失败变更。
- 阻断任务不饿死其余任务依赖 `block()` 对相同错误的幂等返回（原 job 原样返回、revision 不变）；经调试复现实测确认，未改动该行为。
- 无其他失败与偏差；V2/V3 行为回归以全量套件覆盖。

## 8. 留给 K2 的入口

设置页最小表单（两 Key + 模式开关）与「高级（可选）」折叠（供应商、模型、地域、业务空间 ID、金额上限）；保存视频 Key 的零费用只读探测（区分「鉴权通过 / 视频权限未验证」，失败不阻塞保存、无任务与费用）；Key 与模式免重启生效（决议 1 结论：可行）与首次配置自动切 live 的限定触发（决议 2）；对应离线用例；全程 submit=0。
