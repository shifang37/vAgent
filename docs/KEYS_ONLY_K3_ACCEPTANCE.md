# KEYS_ONLY K3 验收：wheel smoke 扩展、四组 CI 与共享域名真实样本

完成日期：**2026-10-18**。基线：K2 提交 `86a44f3`；实现分支 `codex/m3-e0`（K3 变更提交 `b953886`）。方案与冻结决议见 [两 Key 直用方案](./KEYS_ONLY_PLAN.md)；零费用探测法与共享域名预研见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 10 节。

**K3 含本轨唯一付费步骤**：零费用预检（不存在任务查询 + DeepSeek 模型列表）后，经安装在仓库外独立环境的 wheel 以仅视频 Key（共享域名）提交 1 个 5 秒 720P 单镜头任务（submit=1、估算 3.00 CNY），未调用文本模型；其余全部离线。执行前额度已经显式批准（冻结决议 4）。

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| wheel smoke 扩展：keys-only live 夹具（仅两 Key 保存 → 零费用探测 → 免重启自动 live → 共享域提交 → 本地交付 → CLI 核验） | 完成 |
| 共享域真实样本（单镜头 5 秒 720P；submit=1、估算 3.00 CNY；结果主机与账单如实记录） | 完成 |
| 四组 CI（ubuntu/windows × 3.11/3.12）与汇总证据 | 完成（明细见第 5 节） |
| README、M3_PLAN、KEYS_ONLY_PLAN 与 VIDEO_API_NOTES 同步 | 完成 |

## 2. wheel smoke 的 keys-only live 夹具

- `scripts/wheel_smoke.py` 新增 `smoke_keys_only_live`（含 `seed_keys_only_storyboard`），随既有检查一起在 CI 的安装态步骤运行；报告新增 `checks.keysOnlyLiveSharedHost` 与 `keysOnlyLive` 段。
- 夹具起点：全新数据目录、模式 off、无任何视频 Key；受控上游记录每次调用的方法 / 主机 / 路径 / 凭证匹配。
- 步骤与断言（全部走安装包真实代码路径）：
  1. **设置页保存两个 Key**（编排 Key + 视频 Key，均占位值）→ 恰好一次 GET 探测（共享 `dashscope.aliyuncs.com`、`/api/v1/tasks/{随机 UUID}`、携带新 Key），服务对不存在任务应答 `UNKNOWN` → `videoProbe.status=authenticated`、`code=PROVIDER_TASK_UNAVAILABLE`；`autoEnabledLive=true`、`videoMode=activeVideoMode=live`、`restartFields=[]`、`videoWorkspaceId=null`；保存路径零任务、零提交；两个 Key 均不出现在响应。
  2. **免重启登记**：同进程经客户端批量登记 1 个镜头 → `JobV5`（contractVersion=5、`endpointProfile=beijing-shared-v1`、落盘请求无 `workspaceId`）。
  3. **提交与查询**：应用自身 Worker 提交——唯一一次 POST 打向共享域 `/api/v1/services/aigc/video-generation/video-synthesis`、携带已保存 Key、请求体逐字段断言（`wan2.7-t2v-2026-06-12`、提示词前缀、`{resolution 720P, ratio 16:9, duration 5, prompt_extend=false, watermark=true, seed=0}`）；查询恰好一次、同为主机；全部上游调用主机集合 = {共享域}。
  4. **本地交付**：受控 MP4 下载经真实媒体管线校验入索引，文件字节与 SHA-256 核对；媒体 API 全量读取通过；CLI `jobs get` 报 `succeeded` / `mediaAvailable`、`submitAttempts=1`、无签名泄漏；实例锁释放。
- 本地证据：`output/keys-only-k3-wheel-smoke.json`（`keysOnlyLive` 段：jobId / providerTaskId / mediaId / sha256 / 契约版本 / 端点形态 / 提交与查询计数）。
- 覆盖边界：探测与生成前的任何路径零提交；真实共享域提交路径由第 4 节样本验证（夹具拦截全部主机，不能替代真实样本）。

## 3. 本地门禁（K3 变更提交 `b953886`）

- 离线全量：958 passed / 2 skipped（与 K2 基线一致；wheel smoke 不属于 pytest 套件）。
- `ruff check` 与 `ruff format --check`（`src/vagent tests scripts`）通过；`node --check` 三个前端文件与 `node --test` 两个状态文件通过。
- 安装态 wheel smoke：本地构建 wheel（`tmp/k3-wheel`）安装进独立环境（`tmp/m2-d1-wheel-env`）后以 `-I` 运行，**PASS**（共十三项检查，含新增 keys-only 夹具；首次运行即通过）。

## 4. 共享域名真实样本（唯一付费步骤）

**预检（零费用，先于付费执行）**：`output/keys-only-k3-preflight.json` —— DeepSeek 模型列表 200（`deepseek-flash` 在列）；共享域对不存在任务 ID 的查询返回合法 payload（`task_status=UNKNOWN`，适配器映射 `PROVIDER_TASK_UNAVAILABLE`）——Key、共享域名主机与鉴权有效；未创建任务、未计费。

**执行环境与命令**：

```bash
. tmp/keys-only-k3-live/runenv.sh   # .env 去 CRLF；移除 VAGENT_DASHSCOPE_WORKSPACE_ID；VAGENT_VIDEO_MODE=live；独立 VAGENT_HOME
tmp/m2-d1-wheel-env/Scripts/python.exe -I tmp/keys-only-k3-live/sample.py --output output/keys-only-k3-live.json
```

样本由安装在仓库外独立环境（`tmp/m2-d1-wheel-env`，wheel 构建于 `tmp/k3-wheel`）的 K3 应用执行，注册路径与 Web 分镜面板的客户端批量登记相同。

**登记与提交**：

- 1 个 t2v 镜头（5 秒 / 720P / 16:9，提示词「雨夜咖啡店，镜头缓慢推近窗边的热咖啡」）→ `JobV5`（contractVersion=5）、`endpointProfile=beijing-shared-v1`、落盘请求不含 `workspaceId`；回显估算 3.00 CNY。
- 唯一一次提交成功受理（`submitAttempts=1`），上游任务 ID **`ad3a4a68-6849-44a8-9b68-1cca1c6877be`**；提交后按冻结的 15 秒策略轮询 **10 次、0 次错误**（查询请求 ID `d1e15ec0-538f-984f-b202-5f8fb8872669`）；总耗时约 2 分 40 秒（04:33:16Z → 04:35:56Z）。
- 客户端 Run `1e1cb1ef-40a3-5db2-aa4b-a0b814e60e5b` 为 `completed`（executionVersion 3）；**模型调用 0 次**（冻结预算只含 1 次视频提交；文本编排路径的真实性由 C3.2 与 M2 D4 的真实联调覆盖）。

**交付与结果主机**：

- `SUCCEEDED` → 下载一次即成（attempts=1、windowAttempts=1、来源策略 `wan-result-hosts-v2`）→ 本地交付 MP4：**3,417,261 字节**、1280×720、5.033 秒、H.264 + AAC、30fps、校验 `mp4-avc-v1`；媒体 SHA-256 **`2e7974c3f587555734af08a0f1f9ed63f4a845b594ded9bcc4b5c0d794e715a9`**（独立 `sha256sum` 复核与证据一致）。
- 结果主机 **`dashscope-a717.oss-accelerate.aliyuncs.com`**，仍在 `wan-result-hosts-v2` 允许集内（无需追加 v3 允许集）；本地文件与索引提交完成、实例锁释放。
- 共享域名**提交**路径由此首个真实样本验证：进程内不存在业务空间配置，请求形态为 `beijing-shared-v1`，提交被受理且任务走完查询—下载—交付全链路。

**成本（如实记录）**：估算 3.00 CNY（单价 0.60 CNY/输出秒 × 5 秒，价格版本 `wan27-beijing-720p-20261010`，单 Job 上限 3.00）；上游 `usage` 回显 `outputVideoDuration=5`、`videoCount=1`、720P、16:9。**实际账单保持未知**（`actualCost.status=unknown`、金额 null）；是否被免费额度抵扣未知。

**证据**：`output/keys-only-k3-live.json`、`output/keys-only-k3-preflight.json`；数据目录 `tmp/keys-only-k3-live/home-live`（含交付 MP4 与状态；本地私有 URL 仅存于该状态，不进入公开记录）。

## 5. 四组 CI

- 四组 CI（ubuntu/windows × 3.11/3.12）在最终提交 `29b9162` 上全部通过（check runs `117589703143` / `117589703002` / `117589702784` / `117589703007`；ubuntu 3.11/3.12 均 957 passed / 3 skipped，windows 3.11/3.12 均 959 passed / 1 skipped；安装态 wheel smoke 四组 PASS 且含 keys-only 夹具）；汇总证据 `output/keys-only-k3-ci-29b9162-summary.json`。

## 6. 失败与偏差记录

- K3 执行无失败、无重试：wheel smoke 扩展首次本地运行即 PASS；样本一次提交即成、下载一次即成；四文档同步补丁逐条计数校验通过。
- 覆盖说明（非偏差）：样本未调用文本模型，注册经客户端批量路径（与 Web 面板一致）；`video_generate` 单镜头工具路径与模型编排路径此前已由受控用例（K1/K2）与真实联调（C3.2、M2 D4）分别覆盖，K3 冻结预算只安排 1 次视频提交。
- 无其他偏差；V2/V3 逐字节回归与 pre-K 兼容套件保持通过（离线全量与四组 CI 口径同 K2）。

## 7. 与 M3 的衔接

- 两 Key 直用（K0–K3）全部完成：共享域名提交路径已由真实样本验证；M3 E2 承接剩余「多供应商与中转」范围，E4 的新增付费样本按 M3 决策点 3 另行批准执行。
