# C0：万相视频 API 核实记录

查阅日期：**2026-10-10（Asia/Shanghai）**。范围：M1-C 的单镜头文生视频。本文记录公开官方资料与本项目的取舍；实现契约见 [M1C_CONTRACTS.md](./M1C_CONTRACTS.md)。**本次未调用生成接口、查询账号任务或验证视频权限，视频 submit 次数为 0。**

## 1. 来源与证据等级

下列页面均于上述日期读取正文，不以搜索摘要或历史计划代替核实。“文档支持”不代表当前账号已经开通；“未确认”不能作为实现中的肯定承诺。

| 编号 | 官方资料 | 本次用途 |
|---|---|---|
| S1 | [万相2.7-文生视频 API 参考][S1] | 精确模型、输入、输出、HTTP、状态、期限、MP4 编码 |
| S2 | [模型价格：万相文生视频][S2] | 北京地域精确版本的单价、免费额度、视频计费规则 |
| S3 | [限流：万相系列][S3] | 创建任务 RPS 与并发任务数 |
| S4 | [获取与配置 API Key][S4] | 地域、业务空间、Key 权限与账号关系 |
| S5 | [管理异步任务][S5] | 查询限制、列表查询、取消的适用状态 |
| S6 | [错误信息][S6] | 鉴权、权限、参数、余额、限流与服务错误 |
| S7 | [地域与接入域名][S7] | 业务空间专属域名、旧域名的兼容边界 |
| S8 | [文生视频使用指南][S8] | 声音、单镜头描述、失败计费与文档差异 |

资料优先级：选定版本的 API 参数表 > 泛化使用指南；报价使用同地域、同精确模型版本的价格行。文档之间有差异时显式指定参数，无法消除的差异标为未确认。

## 2. 冻结的首个能力组合

| 项目 | 官方支持 / 本项目采用 | 证据 |
|---|---|---|
| 供应商与模式 | 本地标识 `provider=wan`、`mode=live`；继续保留 off/mock | 项目决策 |
| 模型 | **`wan2.7-t2v-2026-06-12`**；不使用会漂移的 `wan2.7-t2v` 别名 | 文档支持，S1/S2 |
| 地域 | **`cn-beijing`（华北2，北京）** | 文档支持，S1/S2/S7 |
| 时长 | API 支持 2–15 的整数秒，默认 5；首期只开放 **5 秒** | 文档支持，S1 |
| 画质 / 比例 | API 支持 720P/1080P 和五种比例；首期只开放 **720P、16:9、1280×720** | 文档支持，S1 |
| 视频数 / 容器 | 单任务一个视频；MP4、H.264 视频编码 | 文档支持，S1 |
| 声音 | 不传 `audio_url` 时自动生成匹配的背景音乐、音效或人声 | 文档支持，S1/S8；具体音频编码、采样率未确认 |
| 镜头 | 通过 prompt 明确要求单镜头；2.7 不支持 `shot_type` 控制 | 文档支持，S1；镜头遵循程度须在 C3 看实际样本 |
| 预计费用 | **CNY 0.60/输出秒 × 5 秒 = CNY 3.00**，不抵扣免费额度 | 文档支持，S2；实际扣费未取得 |

其余分辨率、比例、时长和模型不进入本期能力表。官方支持更广不意味着应用已开放；模型、地域或规格不匹配时返回明确错误，不能自动替换。

### 完整默认参数

| HTTP 字段 | S1 的限制 / 默认 | 本项目冻结值 |
|---|---|---|
| `input.prompt` | 中英文，最多 5000 字符，超出会被上游截断 | 后端前置 `生成单镜头视频。\n`；总长 ≤5000，用户正文 trim 后 ≤4991，超限本地拒绝 |
| `input.negative_prompt` | 可选，最多 500 字符 | 不传；工具不开放 |
| `input.audio_url` | 可选，支持参考音频 | 不传；不开放参考音频或 URL 输入 |
| `parameters.resolution` | `720P` / `1080P`，默认 `1080P` | **显式 `720P`**；本地规格名为 `720p` |
| `parameters.ratio` | 默认 `16:9` | 显式 `16:9` |
| `parameters.duration` | 整数 2–15，默认 5 | 显式整数 `5` |
| `parameters.prompt_extend` | 默认 true，可能增加耗时 | 显式 `false`，保留冻结的单镜头提示词 |
| `parameters.watermark` | S1 参数表默认 false | 显式 `true`，添加“AI生成”标识 |
| `parameters.seed` | 0–2147483647；不传则随机 | 显式整数 `0`；相同 seed 不保证结果完全相同 |

字符限制按 Python Unicode 码点计数；前缀为 9 个码点，内部空白保留。不依赖上游自动截断。`audioMode=auto` 与 `shotMode=single` 是应用能力描述，不发送为供应商字段。不得发送旧版 `size`、`shot_type` 或未经 2.7 参数表确认的 `audio=false`。

S8 的通用 FAQ 写水印默认开启，与 S1 的 2.7 参数表不同；本项目始终发送 `watermark=true`，不依赖该默认值。S8 的快速开始已介绍更新系列，不能据此替换本次固定的 2.7 版本。

## 3. 账户、地域与凭证

S4/S6 要求先开通百炼服务；开通时如提示实名认证须完成相应流程。Key 有归属账号、业务空间与地域，自定义权限还可能限制模型和来源 IP。非默认空间需有该模型的调用授权；精确版本是否在当前账号可见、余额和免费额度均需单独核验。

采用 S1/S7 推荐的**业务空间专属域名**：`https://{workspaceId}.cn-beijing.maas.aliyuncs.com`。`workspaceId` 从控制台的业务空间/API Host 获取，Key 必须能访问该空间。域名由后端以经过校验的单个 DNS 标签拼接；不接受用户或模型提供任意 URL。

S1 说明旧 `dashscope.aliyuncs.com` 仍可用，S7 同时说明它从 2026-09-30 起不再支持新特性。因此首期只冻结专属域名，不自动回退旧域名、试用域名或新加坡。所需新增配置为 `VAGENT_DASHSCOPE_KEY` 和 `VAGENT_DASHSCOPE_WORKSPACE_ID`，详见契约。

| 当前账号事项 | 本次状态 | 何时验证 |
|---|---|---|
| 百炼开通、实名认证、精确模型授权 | **未验证** | C3 前通过账号控制台确认 |
| 北京 Key / 业务空间匹配、IP 权限 | **未验证**；本次未读取凭证 | C3 前配置；真实调用保留错误证据 |
| 免费额度、余额、节省计划及实际单价 | **未知** | 账号费用页及后续账单 |
| 真实视频生成、下载、浏览器播放 | **未执行** | C3 使用最多一个收费 Job 验收 |

本次查阅未找到能无生成副作用地证明“该 Key 可调用该精确视频模型”的接口。配置保存只做本地校验，展示“已配置，视频权限未验证”；通用模型列表或其他模型调用成功不能代替视频权限验证。

## 4. HTTP 协议

| 操作 | 固定方法与路径 | 请求头 |
|---|---|---|
| 提交 | `POST /api/v1/services/aigc/video-generation/video-synthesis` | `Authorization: Bearer <key>`、`Content-Type: application/json`、`X-DashScope-Async: enable` |
| 查询 | `GET /api/v1/tasks/{task_id}` | `Authorization: Bearer <key>` |

主机使用上节专属域名。鉴权客户端不跟随重定向、不隐式重试提交；直连 HTTP 实现使用现有 httpx。`operationKey` 只用于本地幂等，不作为未经证实的上游幂等头发送。

最小请求示例（**合成示例，未发送**）：

```json
{
  "model": "wan2.7-t2v-2026-06-12",
  "input": {"prompt": "生成单镜头视频。\n雨夜咖啡店，镜头缓慢推近窗边的一杯热咖啡。"},
  "parameters": {
    "resolution": "720P", "ratio": "16:9", "duration": 5,
    "prompt_extend": false, "watermark": true, "seed": 0
  }
}
```

S1 的提交成功响应包含 `output.task_id`、`output.task_status` 与 `request_id`；失败示例为顶层 `code/message/request_id`。`task_id` 是后续查询身份，`request_id` 是**每一次 HTTP 请求**的追踪标识，两者不能互换。保存原提交 request ID 和最近查询 request ID，不用查询 ID 覆盖提交 ID。

查询成功的关键字段：

| 位置 | 含义 / 处理 |
|---|---|
| `output.task_id` | 必须与原确认 task ID 一致 |
| `output.task_status` | 按下表解释，不接受未知枚举为成功 |
| `output.video_url` | SUCCEEDED 时的视频地址，私有保存后下载；不直接交给页面或模型 |
| `output.submit_time/scheduled_time/end_time` | 文档格式不含时区；保留有界原值，不擅自解释为 UTC |
| `output.orig_prompt` | 可选回显；不能覆盖原冻结请求 |
| `output.code/message` | FAILED 的错误；经过固定映射与脱敏后记录 |
| `usage.duration/output_video_duration` | 成功输出的计费时长，示例单位秒；不是金额 |
| `usage.input_video_duration/video_count/SR/ratio` | 文生视频输入时长为 0，数量为 1；记录可取得的规格 |
| `request_id` | 本次查询追踪 ID |

S1 有一处将输出时长描述为等同 `input.duration`，但参数表和请求示例把时长放在 `parameters.duration`；采用后者。成功缺 URL、字段类型错误、task ID 不符等属于协议错误，不能产生本地 succeeded。

| 上游状态 | 本地解释 |
|---|---|
| `PENDING` | queued；已 running 的 Job 不倒退 |
| `RUNNING` | running |
| `SUCCEEDED` | 上游生成成功，登记下载；完整媒体提交后才是本地 succeeded |
| `FAILED` | 已确认生成失败，保留 task ID |
| `CANCELED` | 已确认上游取消，归生成失败 `PROVIDER_CANCELED`；不创建替代任务 |
| `UNKNOWN` | 查询对象不存在或状态未知/过期；保留原 task ID 与生成状态，暂停查询 |
| 其他值 / 缺状态 | 协议错误，按有限查询策略处理；不是生成失败 |

## 5. 提交歧义、查询与生命周期

### 提交边界

S1 没有公开提交幂等参数；S5 的列表查询可按 task ID、时间、模型、状态筛选，但未提供客户端幂等键，也不能从丢失的响应中可靠反查唯一原请求。因此 **`supportsIdempotentSubmit=false`**。提示词、时间窗口和列表里的近似任务都不能用来自动消除 unknown。

- 返回可核验的成功受理 ID：先持久保存原 ID；附带状态异常时仍只查询这个 ID。
- 与 S6 对应的完整、无任务 ID 的明确鉴权/权限/参数/余额/配额拒绝响应：`submissionOutcome=not_accepted`，本地 failed。
- 超时、连接中断、取消、5xx、408、未知错误体、非 JSON、成功响应丢失或缺合法 ID：`submissionOutcome=unknown`，停止自动提交。不能仅凭“4xx”或异常名称认定未受理。
- 崩溃发生在提交意图记录后但 ID 落盘前，同样保留 unknown。供应商文档中的“重试”建议不改变每 Run 一次提交的项目约束。

具体 HTTP/code 白名单、协议优先级与错误名在 [契约第 4 节](./M1C_CONTRACTS.md#4-供应商结果错误与轮询)冻结。

### 限流和时效

| 项目 | 官方资料结论 | 项目策略 / 未确认项 |
|---|---|---|
| 生成耗时 | S1 通常 1–5 分钟 | 不承诺固定完成时间或进度百分比 |
| 轮询间隔 | S1 建议例如 15 秒 | live 正常间隔 15 秒；Mock 原 2 秒不变 |
| 创建限流 | S3 北京精确模型为 5 RPS、5 个处理中任务 | 本地串行发起，控制速率；账号也可能被其他客户端占用 |
| 查询限流 | S1/S5：20 RPS/QPS；S5 按主账号及其子账号计 | 持久退避；不把配额当成独占配额 |
| task ID 期限 | S1：24 小时；超期可返回 UNKNOWN | S5 泛化描述为完成后通常 24 小时，起算点表述不同；本地从提交意图起保守计 24 小时，不承诺额外查询窗口 |
| 视频 URL 期限 | S1：24 小时，随后结果可能清理 | 尽快下载；接收时间+24小时不能当作新的有效期 |
| 查询原任务刷新 URL | **未找到延长签名或结果保留期的承诺** | 本期 `supportsResultUrlRefresh=false`；过期停止下载，不靠反复查询刷新或重新生成 |
| 云端取消 | S5 只支持仍处于 PENDING 的任务；RUNNING 不支持 | 本期不实现取消，`supportsCancel=false`；停止 Agent 不代表云端停止 |

S5 当前正文和 cURL 中存在 `/tasks{task_id}` 缺分隔斜杠的示例；查询路径以 S1 的 `/tasks/{task_id}` 为准。取消未纳入本期，不据该示例实现调用。

## 6. 下载来源与媒体验证

S1 的 HTTP/SDK 示例出现以下 HTTPS 主机：

- `dashscope-result-sh.oss-accelerate.aliyuncs.com`
- `dashscope-a717.oss-accelerate.aliyuncs.com`

它们是**文档示例来源，非完整 CDN 清单或永久域名保证**。本期以这两个精确主机作为最小允许集；每次跳转均验证主机与实际连接地址，最多 3 次跳转。未知主机停止并保留原 Job，待核实后更新服务端策略；不能泛放行所有 `aliyuncs.com`、OSS 桶或用户 URL。C3 必须记录真实样本的主机和重定向情况。

下载客户端不携带 API Key、Cookie 或 API 客户端的默认鉴权头。仅 HTTPS/443，拒绝 userinfo、IP 字面量、本机/内网/保留地址和不受控路径。签名 URL 只在本地私有 Job 中保存，不进入日志、异常正文、工具结果、SSE、HTML 或公开配置。

确认的媒体保证为 MP4/H.264；**帧率、码率、H.264 profile/level、音频编码、moov 位置及服务端文件校验和未确认**。文件名和 Content-Type 均不能证明完整视频。C2 校验容器结构、视频轨道、样本范围及大小并计算 SHA-256；实际浏览器解码、音轨和拖动在 C3 验证。不引入转码作为隐式补救。

下载保护值冻结为 256 MiB、连接 10 秒、读取 30 秒、单次总时长 180 秒、并发 1、每窗口总共 3 次尝试。这是针对首期规格的**本地资源策略**，不是官方大小/耗时上限；超限须报错，不能报生成失败或重新付费生成。

## 7. 价格与实际费用

S2 北京 `wan2.7-t2v-2026-06-12` 行：720P **0.6 元/秒**，1080P **1 元/秒**。本期只采用 720P。S2/S8 说明文生视频输入不计费，按成功生成的输出秒数计费；模型调用失败或处理错误不产生费用、不消耗免费额度。

S2 列出该模型北京免费额度 **50 秒**，有效期自开通百炼/模型发布/申请通过之日起 90 天内（以较晚者为准）。这是文档中的额度规则，当前账号是否有、剩余多少均未知，不能从预估金额扣除。

- 价格版本：`wan27-beijing-720p-20261010`；币种 `CNY`，十进制字符串单价 `0.60`，5 秒估算 `3.00`，默认单 Job **估算金额**上限 `3.00`。
- 本次选择的输出声音包含在该文生视频规格中；表中未单列默认声音、水印费用。没有据此开放额外参数，也没有承诺所有其他组合同价。
- `usage` 只有时长/规格，未提供人民币实际扣费或优惠抵扣字段。`actualAmount=null`、`actualStatus=unknown`，留待账单核对；不能把 `usage.duration × 单价` 写成实际账单。
- 上游成功而本地查询/下载失败仍可能已经计费。提交 unknown 也不能宣称未计费；仅已确认的上游调用/处理失败适用官方失败计费规则。
- 该估算只覆盖视频生成，不包含 DeepSeek、存储/流量或其他云产品费用；服务端估算上限不能保证供应商最终账单。价格变更应更新来源与版本并重新核实，旧 Job 保留原快照。

## 8. C0 结论与后续验证

**公开接口与实现策略已冻结，可进入 C1 离线实现。** 当前账号权限、精确模型实际可用性、真实 CDN、音频编码与浏览器表现保留为 C3 验证项；不要求现在生成视频来补齐这些事实。C0 没有把“已配置”升级为“已验证”，也没有宣称 C1/C2 已实现。

[S1]: https://help.aliyun.com/zh/model-studio/text-to-video-api-reference
[S2]: https://help.aliyun.com/zh/model-studio/model-pricing#ba6f7744d5e0o
[S3]: https://help.aliyun.com/zh/model-studio/rate-limit
[S4]: https://help.aliyun.com/zh/model-studio/get-api-key
[S5]: https://help.aliyun.com/zh/model-studio/manage-asynchronous-tasks
[S6]: https://help.aliyun.com/zh/model-studio/error-code
[S7]: https://help.aliyun.com/zh/model-studio/regions#h2_migrate_domain
[S8]: https://help.aliyun.com/zh/model-studio/text-to-video-guide
