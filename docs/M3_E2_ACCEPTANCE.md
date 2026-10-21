# M3 E2 验收：更多供应商与中转

日期：**2026-10-21**。基线 `9813f4c`（E1，schema 5，tools/rules 5/5）；本包交付 commit 见 `git log`。范围见 [M3 任务规划](./M3_PLAN.md) 的 E2，冻结契约见 [M3 契约](./M3_CONTRACTS.md) §1、§5、§6。**E2 全程视频 submit=0：所有供应商响应均为受控 httpx 响应，未读取真实供应商 Key、未创建云端任务、未产生视频费用。**

## 1. 交付内容

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 通用请求 v6 | `src/vagent/video/contracts.py` | `VideoRequestV6`/`JobV6`/`JobCostV6`/`ResultSource`；冻结 `provider, model, mode, region, endpointProfile, adapterVersion, capabilitiesVersion, priceVersion, sourcePolicyVersion`、规范化请求体参数与可空 `shotRef`；历史 Job/Request 1–5 原类与分派不变，Wan 不被重编码为 6 |
| 能力表 v3 | 同上 `kling_capabilities`/`fal_capabilities` | 每个供应商一条 t2v/5 秒/720p/16:9；Kling 原生音频 + `VIDEO_UNIT`，fal standard 显式静音；`supportsCancel/IdempotentSubmit/ResultUrlRefresh=false`，提示词上限 2500 |
| Kling 直连 | `src/vagent/video/providers/kling.py` | 固定 `https://api-singapore.klingai.com`、`Authorization: Bearer`、`POST /text-to-video/kling-3.0-turbo`、`GET /tasks?task_ids=`；状态/结果/错误分列；成功要求唯一 video 输出 |
| fal 中转 | `src/vagent/video/providers/fal.py` | 固定 `https://queue.fal.run`、`Authorization: Key`、endpoint 提交与 app 根查询（status 后必须再取 result）；不跟随响应中的 URL，COMPLETED 不等于交付 |
| 价格与预算 | `src/vagent/video/jobs.py` | Kling 0.112 USD/输出秒（5 秒 0.56 USD）+ 0.8 `VIDEO_UNIT`/秒（4 units）；fal 0.084 USD/秒（0.42 USD）；按 provider/currency 与 Kling units 分桶，Run 共享至多 4 个新 Job；用户设置只能收紧冻结上限 |
| 来源策略 | `src/vagent/video/media_http.py` | `kling-auth-result-origin-v1`/`fal-auth-result-origin-v1`：认证成功结果生成 `resultSource{taskId, origin, urlFingerprint}`；下载逐次核对 provider/task/policy/精确 origin/整条 URL 指纹，禁止重定向；传输层每次连接重新解析并固定公网地址；Wan 静态允许集不变 |
| 工具与恢复 | `src/vagent/video/tools.py`、`src/vagent/tools.py` | mock/live 均升为 **tools/rules 6/6**；冻结 4/4、5/5 视图（旧 `video_generate` 参数 schema 与仅 Wan 的能力快照），恢复按完整 `toolFeatures` 身份选择；execution/context/checkpoint 仍为 3/2/1 |
| 配置 | `src/vagent/config.py`、`src/vagent/application.py` | `VAGENT_KLING_API_KEY`/`FAL_KEY`（本地字段 `klingApiKey`/`falApiKey`）、供应商选择 `videoProvider=wan|kling|fal`、逐供应商上限；保存即热更新到新登记，在途/历史 Job 用冻结身份；Kling/fal 无已核实免费探测，只标注「已保存，鉴权/模型权限未验证」 |
| 设置页与用量 | `web/app.js` | 供应商选择 + 所选供应商 Key、逐供应商报价/上限、Kling 单位提示、按供应商分桶用量；高级覆盖默认折叠 |
| CLI | `src/vagent/cli.py` | `jobs retry-query`、`shots redo` 与 `config` 支持三个供应商；`vagent config` 只输出 Key 是否配置，不打印任何 Key |

## 2. 关键实现决策（相对契约的细化）

1. **v6 只用于新供应商。** `JobV6` 覆盖 `JobV2` 的请求/成本/结果字段，`JobDispatch[6]` 与 `StoredJob` 联合保留 1–5 原类；Wan 新请求仍冻结为 v4/v5。这样历史签名、检查点与媒体提交规则不需要重编码。
2. **金额精度分层。** 能力表保存 4 位小数的单价（`0.112`/`0.084`），Job 报价按输出秒计算后保存两位小数金额（`0.56`/`0.42`）；金额始终是十进制字符串，不换汇。Kling 另存 `resourceEstimate{unit="VIDEO_UNIT", unitsPerSecond=0.8, quantity=5, units=4}`。
3. **`external_task_id` 只作服务端关联。** Kling 请求的 `options.external_task_id` 取 `"vag-" + SHA-256(operationKey)[:32]`，确定性且长度固定；契约明确不据此承诺幂等，提交仍是一次、未知不重投。
4. **预算快照含三个桶。** `RunLimitsSnapshotV2.buckets` 固定给出 wan/CNY、kling/USD、fal/USD 三个 `AmountLimit`（含各自 used/remaining 与 Kling units），注册事务内逐桶校验，任一超限整批拒绝；单位超限使用新增错误码 `VIDEO_RUN_UNIT_LIMIT`。共享 Job 上限仍是 4，而不是每供应商 4。
5. **来源绑定的语义。** `bound_result_source` 规范 origin 为 `https://<IDNA 小写主机>:443`，指纹是认证响应中原始 URL 字符串 UTF-8 字节的 SHA-256；因此同源换 path/query、百分号解码差异、端口、userinfo、fragment、IP 字面量全部拒绝。下载端 `check_source(url, policy, binding)` 只在 auth-result-origin 策略下要求绑定；`PinnedPublicBackend` 每次连接重新解析，任何私网/混合答案拒绝，并把已解析的数值地址交给 httpcore（Host/SNI 仍是原主机）；auth 策略下任何 3xx 直接拒绝。
6. **5/5 → 6/6 恢复。** 6/6 的 `video_generate` 参数 schema 是 `VideoIntentV6`（允许 fal 模型名中的 `/`），冻结视图 4/4、5/5 用 `VideoIntentV2` 与旧描述；live 的冻结能力快照只含 Wan 两条。真实 SQLite 检查点验证：5/5 录制的 Run 在 6/6 构建下恢复后仍为 5/5 且签名不变，新 Run 使用 6/6。
7. **新供应商不做鉴权探测。** 保存 Kling/fal Key 只做本地格式校验；首个视频 Key 保存仍自动把 off 切到 live（K2 语义泛化到三个 provider），但不发送任何请求。Wan 的共享域/业务空间探测与热更新回归保持原状。
8. **真实交付衔接修正。** M2 把交付媒体记录在 Job（`result.mediaRefs`），`ShotVersion.mediaId` 保持空且只增不改；E1 的成片选择只读 `ShotVersion.mediaId`，真实生成的镜头因此无法拼接。E2 在 `ShotVersion.mediaId` 为空时改用 Job 已提交媒体引用（仍要求 Job succeeded、媒体索引与哈希一致），使「多供应商 N 镜头 → 本地成片」在本包端到端成立；E1 既有测试与夹具不受影响。

## 3. 验证结果

### 3.1 新增测试

| 文件 | 项 | 覆盖 |
|---|---|---|
| `tests/test_m3_providers.py` | 10 | 能力表/报价/单位；Kling 提交-查询-结果绑定与逐字段请求体；401/403/400 内容拒绝/429 计费与限流/422 参数拒绝分列；500/缺 ID/坏 JSON 的 `SUBMISSION_UNKNOWN` 且只发一次；查询 401 暂停、404 任务不可用、429 退避、坏结果协议错误；唯一 video 输出；fal 提交/状态/结果路径与静音参数；COMPLETED 后错误与缺失 video；跨供应商请求拒绝 |
| `tests/test_m3_source_policy.py` | 16 | 精确 URL/origin 通过；百分号/query/path/http/端口/userinfo/fragment 拒绝；跨供应商或错 policy 的绑定拒绝；IP 字面量不可绑定；Wan 允许集不变；固定后端只连绑定主机与 443；私网/混合 DNS 拒绝；每次连接重新解析，重绑定后的私网答案拒绝 |
| `tests/test_m3_multi_provider.py` | 12 | 3 镜头 Kling 批量登记与桶快照；共享 4 Job 上限与 provider 桶不互挪；缺 Key 只禁用该供应商；超长提示词/超规格/i2v 拒绝；**受控上游 N 镜头端到端**（提交→查询→本地 MP4 交付→`film_assemble`）；重启恢复与篡改桶拒绝；单镜头 v6 登记与重放；预览币种；能力工具逐供应商状态；429 提交不重投；查询 429 退避后暂停；下载遇到 3xx 直接拒绝（`MEDIA_SOURCE_UNAPPROVED`） |
| `tests/test_m3_recovery.py` | 2 | 当前 6/6 与冻结 5/5 的 schema/工具集；真实 SQLite 检查点 5/5 Run 在 6/6 下恢复 |
| `tests/test_m3_config.py` | 4 | Kling Key 保存即 live、零探测、零 Job；坏 Key 拒绝；冻结环境变量名；逐供应商上限与非法单位拒绝 |

### 3.2 全量与门禁

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js && node --test tests/test_job_state.mjs tests/test_storyboard_state.mjs tests/test_film_state.mjs
```

- 全量：**1051 passed / 2 skipped**（约 6 分 22 秒；2 项 skip 仍为当前 Windows 账户无符号链接权限的既有用例）。E2 新增 5 个测试文件共 **44 项**（10 + 16 + 12 + 2 + 4）。

前端 **31 项**通过（`node --test` 三个状态文件）；Ruff check/format 通过。

### 3.3 零费用与受控边界

- 所有新增供应商测试只用 `httpx.MockTransport`：Kling/fal 适配器、Worker、媒体下载、来源策略均走真实代码路径，但没有任何 DNS/TLS 连接。
- 受控上游记录每次调用的主机、路径、方法与请求体；断言只出现 `api-singapore.klingai.com`、`queue.fal.run`，结果为测试注入的 URL。
- 没有真实 Key：测试使用占位值；`vagent config` 回归断言 Key 不出现在输出中。
- 成片拼接沿用 E1 的包内 FFmpeg；E2 端到端用同一受控媒体拼接 3 个镜头，**不产生供应商调用**。

## 4. 未完成与后续（E3–E4）

- 真实 Kling/fal 账号鉴权、地域可达性、真实编码、结果 CDN 下载表现与账单均未验证；按契约留待 **E4** 的显式授权样本，能力表只能称「文档支持，未做真实样本」。
- `VAGENT_HOME/skills/` 用户扩展点与 `vagent headless` 属 **E3**；离线端到端脚本、wheel smoke、四组 CI 与 `docs/M3_ACCEPTANCE.md` 属 **E4**。
- 跨供应商镜头的真实编码兼容性未做真实样本；`mp4-copy-signature-v1` 已在 E1 拒绝不兼容输入，但「Kling 与 fal 的实际输出可拼接」不作承诺。
- 未在受控浏览器中重跑设置页多供应商交互；Web 端只做了 Node 状态测试与 HTTP 层回归，浏览器证据留待 E4。
- **E4 已完成**：受控浏览器已验证 Kling/fal Key 保存与「鉴权/模型权限未验证」标注（并修复切换供应商后输入框仍禁用的问题），安装态 wheel 也验证了多供应商设置投影；真实 Kling/fal 账号与账单仍未做样本。见 [M3 验收](./M3_ACCEPTANCE.md)。

## 5. 失败与偏差记录

- 开始实现前，工作区被 Windows 文件权限挡住（DSH 沙箱无法为 `D:\Code\video` 授予写入）；用官方诊断脚本为当前用户补上完全控制并保留备份/回滚命令，随后继续。
- 开发过程中，本机 `tests/test_usage.py` 的 3 项模型缓存用例曾因宿主机环境存在大小写重复的代理变量（`NO_PROXY`/`no_proxy`）而在构造 langchain HTTP 客户端时抛 `InvalidURL`；该失败在**基线 `9813f4c` 上同样可复现**，与本包改动无关。最终一次干净运行（环境变量已规整）全量为 **1051 passed / 2 skipped**，上述用例通过；本包未修改该路径，也未用重试掩盖。
- 一处实现偏差已修正：E2 最初把每 Job 的 Kling units 与整批求和比较，导致 3 镜头批次被误判超限；改为逐镜头校验后，批次与端到端用例通过（该错误未进入提交）。
- 未做真实供应商样本，因此本包所有「鉴权/下载/限流」结论都只是受控 HTTP 证据，不称为账号已验证。
