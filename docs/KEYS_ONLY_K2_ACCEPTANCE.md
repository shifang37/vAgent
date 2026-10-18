# KEYS_ONLY K2 验收：设置页最小表单、零费用探测与免重启生效

完成日期：**2026-10-18**。基线：K1 提交 `fed9e57`；实现分支 `codex/m3-e0`。方案与冻结决议见 [两 Key 直用方案](./KEYS_ONLY_PLAN.md)；零费用探测法与共享域名预研见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 10 节。

**K2 全程离线：视频 submit=0，未发起真实视频或模型调用，未使用任何真实凭证。** 浏览器验证使用受控上游（`httpx.MockTransport` 驱动已冻结的万相协议，探测应答与真实服务同形：不存在的任务返回 200 / `task_status=UNKNOWN`）与本地测试 MP4；设置保存、探测、登记、提交与本地交付都走真实应用代码路径。共享域名提交路径的真实验证仍留给 K3 单样本（冻结决议 4）。

## 1. 交付范围

| 交付 | 状态 |
|---|---|
| 设置页最小表单：DeepSeek Key（+ 模型）与万相 API Key、视频模式开关（真实视频 / 关闭），保存后立即生效的文案 | 完成 |
| 「高级（可选）」折叠：视频模式（含 mock）、供应商、模型、地域（固定值展示）、业务空间 ID、单任务金额上限；填写业务空间即切专属域名，清空保存即移除 | 完成 |
| 保存视频 Key / 业务空间的零费用只读探测（不存在任务 ID 的查询）：`videoProbe` = 鉴权通过 / 鉴权未通过 / 未完成（含主机、时间、上游 requestId）；失败不阻塞保存 | 完成 |
| 免重启生效（决议 1）：Key、业务空间与模式的保存对新登记即时生效；`JobService.config` 与适配器凭证同步刷新；在途与历史 Job 按各自冻结形态恢复（K1 已证） | 完成 |
| 首次配置自动切 live（决议 2，限定触发）：仅当视频 Key 从「未配置」变为「已配置」、模式为 off 且该模式可编辑（非环境提供）时翻转一次；显式选择（含同一次保存内）优先；`autoEnabledLive` 仅在 PATCH 响应中返回 | 完成 |
| 业务空间可留空 / 后补 / 以空串移除（`videoWorkspaceId: ""`），提交主机随记录形态切换 | 完成 |
| 离线用例：`tests/test_keys_only_web.py`（11 项）与既有 live 保存用例改写（`tests/test_live_application.py`） | 完成 |
| 受控浏览器核对：`tmp/k2-browser/launcher.py` + `check.mjs`（6 阶段 0 失败，5 张截图） | 完成 |

不在 K2（按计划进入 K3）：wheel smoke 的 key-only live 夹具、四组 CI 汇总、共享域名真实样本（submit=1、估算 3.00 CNY，执行前显式批准）。

## 2. 设置页最小表单与高级折叠

- 默认表单只保留 DeepSeek Key、模型、万相 API Key 与「启用真实视频生成（live）」开关；开关与高级区的「视频模式」三选（off / mock / live）双向同步，mock 仍在高级区可达（对应决议 2 的「选择 mock 后不再自动翻转」）。
- 「高级（可选）」使用原生 `<details>` 折叠：视频模式、供应商、视频模型、地域（北京 · cn-beijing 固定值）、业务空间 ID（可选）与单任务金额上限；提示说明「留空用共享域名 `dashscope.aliyuncs.com`、填写后切换到专属域名、清空并保存即可移除」；金额上限保留原校验（十进制 CNY）。
- 保存提示：「配置已保存。」+（自动切 live 时）「已自动启用真实视频生成（可在设置中关闭）。」+（探测有结果时）鉴权通过 / 未通过 / 未完成之一；重启提示仅在 `restartFields` 非空时回退显示（K2 起为空）。
- 顶部「真实视频 · 本地 MP4」标记在保存后立即刷新（`renderVideoMode` 依据活动模式渲染），无需刷新页面。

## 3. 零费用只读探测

- 触发：保存请求涉及 `videoApiKey` 或 `videoWorkspaceId` 且保存后视频 Key 已配置；清空 Key、只改其他字段不探测；探测在 `config_busy` 窗口内执行（15 秒上限），结果写入响应、不阻塞已保存配置。
- 语义：向当前形态的主机（共享 `dashscope.aliyuncs.com` / 专属 `{workspace}.cn-beijing.maas.aliyuncs.com`）发起一次 `GET /api/v1/tasks/{随机 UUID}`。真实服务对不存在的任务返回 200 / `task_status=UNKNOWN`（K0 预研实测，见 VIDEO_API_NOTES 第 10 节），适配器把该应答映射为 `PROVIDER_TASK_UNAVAILABLE`，探测记录 `status=authenticated`（鉴权通过；视频权限仍未验证）。401 / 403 映射 `rejected`（附固定文案与错误码）；网络、超时或协议异常映射 `inconclusive`。
- 契约：`GET /api/config` 与 `PATCH /api/config` 响应新增 `videoProbe`（null = 未探测或因 Key / 业务空间变化而重置；否则 `{status, checkedAt, host, taskId, code, message, requestId}`）；`videoPermissionStatus` 保持 `unverified`（权限需真实生成才能验证）；进程重启后重置（与 DeepSeek 验证一致）。PATCH 响应额外携带 `autoEnabledLive`。
- 零费用：探测不创建任务、不提交；离线用例与浏览器证据断言探测仅 GET、保存路径零 POST、数据目录无 Job。

## 4. 免重启生效（决议 1 落地）

- `save_configuration` 改为 async：保存后把有效配置同时应用到 `ApplicationService.config`、`JobService.config` 与 `WanAdapter` 凭证（`update_credentials`）；新登记（对话 Run、批量登记、重做）即时使用新 Key / 业务空间 / 模式；`runner()` 按新配置选择启动时已注册的工具视图（三模式视图启动即注册，K1 第 5 节已证可行）。
- 在途与历史语义不变：等待中的 Run 按记录的模式恢复（`_waiting_runner`）；已登记未提交的 Job 按其冻结形态做 `check_runtime`（K1 混合目录用例覆盖共享 / 专属两代）；`restartFields` 恒为空。
- 业务空间热切换：后补 → 新任务冻结 V2 / V3（专属域名、`beijing-workspace-v1`）；移除 → 新任务冻结 V4 / V5（共享域名、`beijing-shared-v1`）。受控用例断言提交主机随形态切换（POST 专属 → 共享）。
- 自动切 live（决议 2）：用例覆盖「首次保存 Key → 自动 live」「同一次保存显式 off / mock 优先」「显式选择后普通保存与 Key 轮换不再翻转」「清空后重新配置再次触发」「环境提供的模式不自动翻转」。
- 回归：既有 live 保存用例改写为免重启语义（保存即生效、`restartFields` 为空、清空 Key 热生效且不探测）；pre-K 契约、V2/V3 指纹与兼容套件全部保持通过。

## 5. 受控浏览器核对

- 夹具与脚本：`tmp/k2-browser/launcher.py`（初始 off、无任何视频 Key；受控上游记录每次调用的方法 / 主机 / 路径 / 凭证匹配；20× 加速时钟；注入离线模型）与 `tmp/k2-browser/check.mjs`；Chrome 155.0.8059.84（headless）。
- 结果：**6/6 阶段通过，0 失败**，接口无 4xx/5xx、无页面错误、无控制台错误、无失败请求。
  1. 设置页最小表单与高级折叠：默认表单含两个 Key、模型与模式开关（默认关）；高级区折叠，标题「高级（可选）」，内含视频模式 / 业务空间 / 金额上限等；未配置时显示「未配置视频密钥」。
  2. 保存视频 Key：零费用探测与自动启用 —— 探测恰好一次（GET、共享域名、携带新 Key），提示「配置已保存。已自动启用真实视频生成（可在设置中关闭）。视频鉴权通过（视频权限未验证）。」；开关切到真实视频；顶部标记即时刷新；`/api/config` 显示 `videoMode=live` 且 `videoProbe.status=authenticated`；任务数 0、提交数 0。
  3. 高级区：填写业务空间 `k2-space` → 探测专属域名通过；清空并保存 → 业务空间移除（`null`），回到共享域名探测。
  4. 免重启登记与本地交付：三次保存后仍零任务、零提交；关闭设置对话框后在分镜面板批量生成 1 个镜头（估算 3.00 CNY），唯一一次提交（POST、共享域名、携带设置页新 Key）一次即成；加速时钟下本地交付 MP4（1280 宽播放验证），任务 `succeeded`。
  5. 开关关闭真实视频：`videoMode=off` 热生效，顶部标记隐藏，切换模式不触发探测。
  6. 页面无错误：四项计数全部为 0。
- 全程 5 次上游调用：3 次探测 GET（共享 / 专属 / 共享，均凭证匹配）+ 1 次生成提交 POST（共享）+ 1 次查询 GET（`k2-task-1`）——生成前的零费用路径没有任何任务创建。
- 证据：`output/keys-only-k2-browser.json`（阶段、空失败列表、浏览器版本、5 次调用明细与夹具 ID）与 5 张截图 `output/keys-only-k2-browser-{settings-default,settings-saved,settings-advanced,delivered,settings-off}.png`。

## 6. 门禁与回归

- 离线全量：**958 passed / 2 skipped**（K1 基线 947 passed / 2 skipped；K2 新增 `tests/test_keys_only_web.py` 11 项，改写 `tests/test_live_application.py` 的保存用例为免重启语义）。
- `ruff check` 与 `ruff format --check`（`src/vagent tests scripts`）通过；`node --check` 覆盖 `web/app.js` 等，`node --test` 两个前端状态文件（22 项）通过。
- 视频 submit=0：全部用例与浏览器证据断言，探测与保存路径无任务创建。
- 四组 CI（ubuntu/windows × 3.11/3.12）在最终提交 `b2b834b` 上全部通过（check runs `117578243305` / `117578243410` / `117578243420` / `117578243445`；ubuntu 3.11/3.12 均 957 passed / 3 skipped，windows 3.11/3.12 均 959 passed / 1 skipped；安装态 wheel smoke 四组 PASS）；汇总证据 `output/keys-only-k2-ci-b2b834b-summary.json`。

## 7. 失败与偏差记录

1. 新用例首轮 1 项失败：共享域批量登记的断言误写为 `JobV4` —— 镜头批量在共享形态冻结的是 **JobV5**（单镜头才是 V4）；产品行为正确，夹具断言修正。
2. 受控浏览器首轮 3 项失败（同源）：保存后对话框重渲染会把「高级（可选）」折叠重新收起，清空业务空间的输入落在不可见元素上；该阶段中止使后续断言级联失败（提交落在当时仍配置的专属域名、探测计数少 1）。属检查脚本缺陷：新增 `expandAdvanced()` 在每次保存后按需重新展开；产品代码未因该失败变更。
3. 无其他失败与偏差；V2/V3 逐字节回归与 pre-K 兼容套件保持通过。

## 8. 留给 K3 的入口

- wheel smoke 扩展（key-only live 夹具：仅两 Key、共享域名、保存探测与免重启生效路径）；四组 CI 汇总证据；共享域名真实样本（单镜头、5 秒 720P、submit=1、估算 3.00 CNY，执行前显式批准额度；结果主机核对 `wan-result-hosts-v2`，出现新主机按既有规则追加 v3、不改写 v2）。
- 可直接复用的 K2 证据：受控浏览器夹具与检查脚本（覆盖除真实 Key 与真实上游之外的全部保存 / 探测 / 登记路径）、`videoProbe` 契约与 `tests/test_keys_only_web.py` 的受控断言模式。
