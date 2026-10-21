# M3 契约：成片、多供应商与 headless

冻结日期：**2026-10-21（E0）**。基线为 `d06dc5f`（M2 与 K0–K3 已完成，schema 4）。范围见 [M3_PLAN](./M3_PLAN.md)，官方核实见 [VIDEO_API_NOTES](./VIDEO_API_NOTES.md) 第 11–12 节，证据见 [E0 验收](./M3_E0_ACCEPTANCE.md)。

本文规定 E1–E4 的实现要求。**E0 不升级生产存储、不注册新工具、不调用账号接口；视频 submit=0。** 文档支持、合成本地实验、受控 HTTP 与真实账号证据分别记录，不相互替代。M1-C、M2、K 轨的单次提交、提交不确定不重试、原任务查询、媒体落盘才成功及历史恢复规则继续有效。

## 1. 决策与版本

| 项目 | 冻结结果 |
|---|---|
| 拼接 | **A：`imageio-ffmpeg==0.6.0` 随安装提供的 FFmpeg，concat demuxer + stream copy**。不使用系统 FFmpeg 自动回退，不调用运行时下载器，不转码 |
| 新直连 | `kling`，新 API Key 协议，`kling-3.0-turbo`，新加坡端点；首期只开放 t2v / 720p / 16:9 / 5 秒 / 原生音频 |
| 中转 | `fal`，`fal-ai/kling-video/v3/standard/text-to-video`；首期只开放 t2v / standard 档 / 16:9 / 5 秒，显式 `generate_audio=false` |
| 其他候选 | Replicate、Sora、Veo、Seedance 本阶段不接入，原因是先收敛为一个直连、一个中转；不据此宣称其不可用。Kling/fal 的图生、首尾帧、多镜头、声音控制也不进入首期能力表 |
| 存储 | E1 新建与迁移为 schema **5**；v4→v5 只改外壳版本并加入 `films={}` |
| 工具版本 | E1 新 Run 为 **tools/rules 5/5**（成片工具）；E2 新 Run 为 **6/6**（通用供应商请求）。不得在同一版本中改变已发布的工具集合、参数或规则 |
| 生成契约 | 历史 Job/Request 1–5 原样保留；E2 新增通用 **6**，不把 Wan 旧对象重编码成 6 |
| 能力 | E1 保留现有 Wan 能力；E2 新能力汇总版本 **`multi-provider-v3`**，逐供应商、模型、模式发布，不自动选模型 |
| Harness | execution **3**、context **2**、checkpoint **1** 保持；等待绑定继续用现有版本，成片不引入第二套等待状态机 |
| skills | 默认在内置根之外增加 `VAGENT_HOME/skills/`；显式 `VAGENT_SKILLS_DIR` 仍替换整个发现根 |
| 真实调用 | E0–E3 submit=0；E4 优先复用已有本地视频，任何新增付费样本另需明确模型、次数和金额授权 |

版本递增分两步是对规划中「M3 一律 5/5」提案的细化：E1 完成后仍须能恢复其 Run，E2 不得再重复 M2 在 4/4 内追加工具造成的恢复边界。旧 Run 使用原版本工具注册表、规则、能力和 skills 元信息形状。

恢复选择必须同时依据已保存的 execution/context 版本与完整 `toolFeatures` 身份（tools/rules、能力及已有特性快照），不能只按 executionVersion 分派。即使 execution=3 未变，也须重建匹配的冻结注册表、规则与能力后核对原签名；未知组合返回 `RESUME_CONFIG_CHANGED`。E1 保存真实 SQLite 检查点验证旧 4/4 与新 5/5 恢复，E2 再验证 5/5 与 6/6 共存；不得用纯领域 JSON 夹具替代这些恢复证据。

## 2. 成片对象与媒体

JSON 字段采用 camelCase；ID 由服务端生成（canonical UUID），用户/模型不能指定文件路径。

| 对象 | 必填内容 |
|---|---|
| `Film` | `id, projectId, storyboardId, currentVersion, versions[], revision, createdAt`；同一项目同一分镜至多一个 Film |
| `FilmVersion` | `version, storyboardVersion, storyboardRevision, inputs[], outputMediaId, assembly, sizeBytes, sha256, metadata, createdAt` |
| `inputs[]` | 按分镜顺序的 `shotId, shotVersionId, jobId, mediaId, sha256, sizeBytes`；不能为空，最多 64 条 |
| `assembly` | `assemblyVersion="ffmpeg-concat-copy-v1"`、`compatibilityVersion="mp4-copy-signature-v1"`、`engine{packageVersion,ffmpegVersion,binarySha256}`、`compatibilitySha256`、逐输入有理数时间线偏移/时长及 `fingerprint` |
| 新 `FilmMedia` | `contractVersion=2, kind="film", id, projectId, filmId, filmVersion, relativePath, mimeType="video/mp4", sizeBytes, sha256, metadata, validationVersion="mp4-avc-v1", createdAt` |

`FilmVersion.storyboardVersion` 是来源快照；Film 根不保存会过期的单一来源版本。每次修改或重做后显式重拼，可以为同一 Film 追加新版本。`currentVersion` 指向末版，版本号从 1 连续增长；`revision` 初值 1，每次追加增加 1。已有版本和文件不能被覆盖、重编码或删除。

现有 `MediaAsset` 强制 `jobId` 且要求关联成功生成 Job，不能用于成片。`media` 集合新增上述独立分支，**不得伪造生成 Job、给旧媒体补 `kind`，或修改旧记录**。历史分支按原校验器解析；`contractVersion=2/kind=film` 才走新校验器。媒体 resolver 校验两分支各自的引用不变量，HTTP/Range、下载、缓存、路径保护和播放器共用。

所有媒体仍位于 `media/{projectId}/{mediaId}.mp4`。成片工具和 Web 视图只返回受控媒体 URL 与 ID；headless/CLI 的本机交付摘要可以返回经过 resolver 校验的绝对路径。`FilmMedia` 与对应 `FilmVersion` 中的文件大小、哈希、元数据必须一致。

## 3. 输入选择、拼接与事务

### 3.1 选择与兼容

一次拼接固定当前 `StoryboardVersion` 与 `revision`，按其镜头顺序选择**该分镜版本、该 shot 的最新 ShotVersion**。最新版本未成功或未交付时整体拒绝，不回退到更早的成功版本。核对 ShotVersion、Job、媒体的项目与相互引用；mock 和旧 Job 沿各自既有引用规则验证。来源文件重新检查大小、SHA-256、路径与 `mp4-avc-v1`，不只信数据库索引。

`mp4-avc-v1` 继续表示既有结构校验，不能当作可拼接签名。新 `mp4-copy-signature-v1` 至少包含：

- 轨道数量、顺序与类型（一个 AVC 视频、零或一个 AAC 音频），视频 sample-entry 和 `avcC` 编码配置；宽高、像素宽高比、色彩/旋转信息及相关 sample-description 扩展。
- 视频/音频 timescale、视频逐帧时长规律（首期只接受恒定帧时长）、composition offset 形式、同步帧起点；AAC AudioSpecificConfig、采样率、声道与 sample-entry 配置。
- 每文件的 DTS/PTS 范围、edit list/预卷、样本结束时间和可证明安全的衔接偏移。文件时长、sample 数、字节偏移和创建时间是逐文件时间线数据，不作为要求相等的编码字段。

参与流复制的编码配置必须逐字段一致；未知或无法解释的相关 box/时间线结构拒绝。初版只接受已覆盖的 edit-list/音频预卷形式；扩充解析范围另增兼容版本。只有宽高、平均帧率、`hasAudio` 相同仍应拒绝未证明兼容的输入。音频有/无、声道或编码配置不同整体拒绝；不同供应商的输出也不承诺兼容。

### 3.2 执行和时间戳

使用包内二进制，确认路径位于安装包的 `binaries` 普通目录，拒绝 symlink/reparse 与环境变量/PATH 注入；记录版本和可执行文件 SHA-256。不支持的平台或缺失二进制返回 `ASSEMBLER_UNAVAILABLE`。E1 锁定依赖及平台 wheel 哈希，安装态验收覆盖 Windows 与 CI 所用 Unix 平台。

服务生成私有工作目录和 concat 清单，输入只引用校验后的本地文件；用参数数组运行 subprocess（无 shell）、`-nostdin`、`-protocol_whitelist file`、`-auto_convert 0`、`-map 0:v:0 -map 0:a:0? -c copy -movflags +faststart`。工作文件名由服务生成，不把提示词/用户路径拼入清单。限制 64 个输入、单输入沿用媒体大小上限、总输入/输出各不超过 **2 GiB**、单次进程 **120 秒**，停止时终止子进程并清理未提交文件。

按样本时序推导每段的安全起点/结束时间，为 concat 明确指定 duration；处理 AAC priming，不能让 FFmpeg 静默修复非单调 DTS。每个接缝允许的封装偏移至多 `max(视频一帧时长, AAC一个帧时长)`，总时长偏差至多该值乘接缝数再加一个输出 timescale tick，偏移逐条落入 `assembly`。偏差超限、时间戳回退警告、丢包/截断/编码错误或返回码非零均拒绝交付。输出再跑结构校验，并核对轨道配置、样本数量、时间线和哈希；浏览器连续解码另在 E1/E4 验收。

E0 的双份测试媒体实验：默认 concat 虽返回 0，出现 AAC `Non-monotonic DTS`；夹具专用 duration `5.021333333` 后无此警告，输出约 10.021289 秒并通过结构校验。这只证明选择 A 可行；**该常量不能成为生产通用算法**，E1 必须从每个输入的样本表/预卷计算。

### 3.3 幂等与文件提交

`film_assemble(storyboardId, expectedRevision)` 使用服务端 operationKey，CLI/Web 为同一次动作提供稳定 `clientRequestId`；`expectedRevision` 指分镜 revision。首次检查后固定所有输入；提交前再次检查 revision 与文件哈希。发生修改返回 `VERSION_CONFLICT`，不混用新旧输入。

`film_assemble` 是可取消的本地异步写工具：短 Store 事务内取得来源快照并登记操作意图，释放锁后执行文件读取/子进程，最后重获锁校验 revision、引用与文件哈希并原子提交。不能在现有同步 `context_execute` 的 Store 锁内运行 FFmpeg，也不能把工具冒充 read 以绕过写入规则。停止/截止信号贯穿锁外执行并终止子进程；取消后不得再进入提交，已完成的重复操作按原结果重放。

指纹为 canonical JSON 的 SHA-256：项目、分镜 ID/版本、有序输入 ID/哈希、assembly/compatibility 版本、引擎二进制哈希及所有影响输出的参数；不含时间、绝对路径、调用 ID。先查指纹，命中有效历史产物则返回原 FilmVersion（不增加 revision/版本/文件）；旧产物缺失或损坏返回 `FILM_MEDIA_INVALID`，不覆盖修复。源版本、哈希、参数或引擎改变才形成新版本。

先生成随机 `.part`，完成校验和 fsync 后原子改名为最终文件，再以一个 Store 事务追加 FilmVersion、FilmMedia 和操作完成结果；媒体与版本不能分两次提交。事务失败可能留下无引用最终文件：启动补偿按受控操作意图处理或清理，不能根据孤立文件推造成功。崩溃遗留 `.part` 一律不索引；已提交旧文件不清理。停止前已经提交的结果保留，未提交的结果不返回成功。全程不登记 generation Job、不启动供应商、不占生成额度。

`film_read(storyboardId)` 只读返回当前 Film 与历史版本摘要；无成片返回空值。错误至少区分 `FILM_INPUT_MISSING`、`FILM_INPUT_NOT_READY`、`FILM_INPUT_INCOMPATIBLE`、`FILM_MEDIA_INVALID`、`ASSEMBLER_UNAVAILABLE`、`FILM_ASSEMBLY_FAILED`、`VERSION_CONFLICT`；拒绝时列出 shot ID 和差异字段，不泄漏本地路径或上游私有 URL。

CLI 冻结为 `vagent film assemble --storyboard ID --expected-revision N [--request-id ID]` 与 `vagent film show --storyboard ID`，沿用项目/session 参数风格。Web 增加显式拼接、成片播放/拖动/下载与版本列表；不自动在镜头重做后拼接。

## 4. v4→v5 与历史恢复

迁移对已验证的 v4 状态做深拷贝，仅加入空 `films` 并改 `schemaVersion=5`。已有 media/Job/Run/Operation/Wait/Storyboard/Asset 的 JSON 值全部不变，不补字段、不重算指纹。迁移前保留原始字节备份 `state-v4-UUID.json`，验证新外壳后原子替换；失败原文件不动，重开不二次迁移，SQLite 检查点、锁与旁车文件不动。

E0 提交 [schema-v4 合成基线](../tests/fixtures/m3/manifest.json)，覆盖历史与当前结构，记录 raw/canonical/collection 哈希、请求与操作指纹及 Run 签名；它不包含可恢复 SQLite 检查点。E1 用它实施真实 v5 迁移测试，并补充当前签名 Run 的检查点恢复测试，不能把 E0 的机械预期计算称为已实现迁移。schema 1–4 校验器保留；未知版本拒绝。

## 5. 多供应商协议与下载

### 5.1 请求快照

Job/Request v6 冻结 `provider, model, mode, region, endpointProfile, adapterVersion, capabilitiesVersion, priceVersion, sourcePolicyVersion`，并保存规范化请求、估算、额度、可空 `shotRef`；沿用 Job 生命周期、媒体意图、持久退避和敏感字段隔离。密钥仍仅从配置读取，不进入 Job、指纹、日志、工具结果或检查点。v6 可用于无 shotRef 的单镜头及有 shotRef 的分镜请求。旧 Wan 1–5 保留原类与精确分派，新 Wan 请求也不强制转换为 v6。

| 供应商 | profile / adapter / price / source policy |
|---|---|
| Kling | `kling-singapore-v1` / `kling-http-v1` / `kling30-turbo-720p-20261021` / `kling-auth-result-origin-v1` |
| fal | `fal-queue-v1` / `fal-http-v1` / `fal-kling30-standard-silent-20261021` / `fal-auth-result-origin-v1` |

Kling 基址固定 `https://api-singapore.klingai.com`；`Authorization: Bearer <API_KEY>`；创建 `POST /text-to-video/kling-3.0-turbo`，请求仅 `prompt`、`settings{duration:5,resolution:"720p",aspect_ratio:"16:9"}` 与服务端关联 `options.external_task_id`。`prompt` 非空且最多 2500 字符（本项目裁剪）。查询 `GET /tasks?task_ids=<原ID>`，只处理对应 ID；`submitted/processing/succeeded/failed` 映射本地状态，成功取唯一 video output。外部 ID 的唯一性不等于幂等保证，`supportsIdempotentSubmit=false`。

fal 使用 `Authorization: Key <FAL_KEY>`、基址 `https://queue.fal.run` 与上述 endpoint；请求固定 `duration:"5", aspect_ratio:"16:9", generate_audio:false, shot_type:"customize", enable_safety_checker:true`，只提供单个 `prompt`（1..2500），`cfg_scale=0.5`、`negative_prompt="blur, distort, and low quality"` 显式固定。不得遗漏音频开关而触发默认收费档。持久化 `request_id`，按官方 SDK 的 app 根路径 `GET /fal-ai/kling-video/requests/{id}/status` 与 `GET /fal-ai/kling-video/requests/{id}` 查询（生成路径含 `/v3/standard/text-to-video`，查询路径去除该子路径，不盲跟响应中的 URL）；`IN_QUEUE/IN_PROGRESS` 为未完成，`COMPLETED` 后仍需 GET result 并检查错误/唯一 `video.url`。

两者采用轮询，不开放本地公网回调、不上传参考图、不执行云端取消。查询初始 15 秒，退避沿用既有持久策略；每 provider 本地至多一个未完成提交任务作为保守调度上限，不把它称为账号额度。Kling 限额按账号/模型/资源包、查询不占并发；fal 额度以账号为准，E0 不读取账号。HTTP 401/403、参数拒绝、内容拒绝、429、上游 5xx/协议错误分列；提交一旦进入不确定窗口一律不盲重投，查询可退避恢复，result 下载重试复用原 task ID。E2 用受控响应测试全部映射。

### 5.2 结果来源规则

官方协议给出结果 URL，但未承诺完整稳定的生产 CDN 主机清单。**不从示例 URL 推导生产静态允许集。** 新策略明确采用逐 Job 的认证结果来源绑定：

1. 只有从固定 API 主机、经过 TLS 校验并携带该 provider 凭证获得的原 task ID 成功结果，才可产生 `resultSource{taskId,origin,urlFingerprint,sourcePolicyVersion}`；Kling/fal 分开校验，绑定 exact HTTPS origin（规范主机、443）。Agent、用户或其他供应商不能提供这个授权。
2. URL 必须是 HTTPS、无 userinfo/fragment/IP 字面量/非 443 端口；主机解析的所有地址必须是公网地址。每次下载/重试重新校验 DNS，实际连接固定到本次验证地址并校验原主机的 TLS/SNI，防止 DNS rebinding；不能只校验后再交给独立 resolver 重解析。
3. 每次同时核对原 provider/task/profile/sourcePolicy、精确 origin 与完整 `urlFingerprint`。后者为认证响应中原 URL 字符串 UTF-8 字节的 SHA-256，不解码百分号、不重排/删除 query（包括签名参数）、不替换 path；origin 另按 IDNA 小写主机与 443 规范化。URL 禁止空白/控制字符，实际发送的 path/query 必须保持原值；同 origin 下换 path/query 也暂停。只使用绑定原 URL，不采用通配域名或共享允许集；禁止所有重定向、代理环境变量、cookie 和媒体请求上的 API Authorization。签名 URL 不暴露在日志/工具/API 摘要；必须受现有下载大小、超时、MIME、哈希及 MP4 校验约束。
4. 绑定后来源改变、过期或策略无法验证时暂停下载，保留任务与原结果；不自动生成新任务、不静默切换主机。`supportsResultUrlRefresh=false`。Kling 文档的 30 天保存期不代表签名 URL 也保证 30 天，fal 的 CDN 生命周期也不当作永久承诺。

这是对旧 Wan 静态主机允许集的**显式新增版本**，不会放宽 `wan-result-hosts-v2`。E2 必须覆盖私网/混合解析/重绑定/重定向/跨供应商来源/错 task ID/端口/同源换 path 或签名 query/凭证泄漏反例，未实现这些条件时该适配器不可下载真实结果。实际账号地域可达性、授权、真实编码和账单仍待 E4；能力表只能称「文档支持，未做真实样本」。

## 6. 费用、确认与配置

金额使用 Decimal 十进制字符串，不用浮点；保留原计费单位，不折算汇率，不套用 Wan 的 CNY 字面量。

| provider / 模式 | 冻结公开估算 | 默认每 Job 上限 | 默认每 Run 上限 |
|---|---|---|---|
| Wan | 沿用既有冻结价格 | 3.00 CNY | 12.00 CNY |
| Kling Turbo / native audio | 0.112 USD/输出秒 × 5 = **0.560 USD**；另记录 0.8 VIDEO_UNIT/秒 × 5 = **4 VIDEO_UNIT** | **0.60 USD 且 4 VIDEO_UNIT** | **2.40 USD 且 16 VIDEO_UNIT** |
| fal standard / silent | 0.084 USD/输出秒 × 5 = **0.420 USD** | **0.50 USD** | **2.00 USD** |

Kling 公开美元价格是估算口径，不承诺用户资源包/折扣/本币实际扣款；并列 unit 预留不可用美元互换。`estimate` 保存 `amount,currency,unit,quantity,unitPrice,priceVersion`；Kling 另保存 `resourceEstimate`。`actual` 没有账单证据始终 unknown。不同 provider/currency 分桶显示并逐桶校验，即使同为 USD 也不把一个供应商的剩余额度挪给另一个。

全 Run 新 Job 数仍**共享硬上限 4**，不是每 provider 各 4；重做计新 Job，去重复用不重复扣额度。每次批量登记在一个事务内校验所有请求、权限、来源和所有预算桶；一项失败全批拒绝。已登记、失败或 unknown 的尝试保守占用预留额，不因报错释放额度重新提交；旧 Run 仍使用其原冻结上限。用户给出的限额可收紧，不能突破服务硬上限；若未来改价/上限/单位，新增版本后重新确认。

确认包含具体 provider/model/mode/规格、每镜头与合计估算、各预算桶已用/剩余及共享 Job 数。`confirm=true` 或已保存的 headless 授权只对该冻结请求集合有效，模型不能扩大授权。配置保存不调用生成接口；Wan K 轨已完成的 Key-only 端点/探测与高级覆盖不重复实施。Kling/fal 新字段为 `VAGENT_KLING_API_KEY` / `FAL_KEY`（配置中 provider 名固定），由用户明确选择供应商，不从 Key 格式猜测。

Kling/fal 若无已核实的免费只读鉴权接口，保存只做本地格式检查并显示「已保存，鉴权/模型权限未验证」，不拿随机收费任务验证，也不把格式通过称鉴权通过；E0 不新增账号探测。各 Key 缺失只禁用对应 provider，不阻塞已有 Wan/mock/文本模式。配置热更新仅用于新登记；在途 Job 继续使用冻结 profile/version，换 Key 仍须校验其 provider，旧恢复签名不改。

## 7. headless（E3）

`vagent headless PROMPT` 与 Web 共用 ApplicationService/Runner/Store、项目/会话、单写锁、等待协调器与恢复机制。默认只完成草案、保存分镜和估算，`status="planned"`，视频 submit=0；**真实编排模型可能产生文本费用**。离线验证使用 mock 编排和受控供应商，不能把默认路径称为完全免费。

采用两阶段授权：第一次 `vagent headless PROMPT` 只产出 planned 结果，持久化分镜 ID/version/revision、有序规范请求、provider/model/profile、能力/价格版本、逐桶估算与限额，并返回 `runId` 与其 canonical JSON SHA-256 `planFingerprint`。用户核对后运行 `vagent headless --plan RUN_ID --plan-fingerprint SHA --confirm ...`；不接受在尚未产出计划的 PROMPT 命令上预先授权。

付费授权还必须提供可重复的 `--max-cost PROVIDER:CURRENCY=AMOUNT`，每个拟用金额桶都要覆盖；Kling 还需 `--max-units kling=AMOUNT`，可用 `--max-jobs N` 收紧共享 4。例：`--plan RUN_ID --plan-fingerprint SHA --confirm --max-cost kling:USD=0.60 --max-units kling=4 --max-jobs 1`。服务保存绑定计划与此次限额的授权指纹；登记事务中原子核对计划、当前分镜 revision、请求及价格/能力快照与各预算。缺项、指纹变化、未知桶、负数/非有限值或超服务硬上限在任何视频登记前拒绝；无 TTY 不交互、不默认同意。授权只覆盖该固定集合，不允许模型在预算内改动目标或加做；修改计划须重新规划与确认。

显式授权路径完成登记→集合等待→本地拼接，成功为 `delivered`。等待超时保留原 Run/Job，输出 ID；`--resume RUN_ID` 续接原执行与已冻结授权，不增额度、不重新提交。持久 waiting Run 在显式重启生命周期中沿既有规则继续；被用户停止的 Run 不自动复活。配置/skills 签名变化拒绝恢复并保留证据。

`--json` stdout 只输出一个 JSON 对象，过程日志在 stderr。字段冻结为 `formatVersion=1,status,projectId,sessionId,runId,planFingerprint,storyboardId,storyboardVersion,storyboardRevision,jobIds,filmId,filmVersion,output{mediaId,path,sha256,sizeBytes},estimates[],limits,error`；未到达字段为 null/空数组，不伪造结果，`error` 含 code/message。退出码：**0** 草案成功或已交付，**2** 参数/领域校验，**3** 授权/预算拒绝，**4** 上游/下载/拼接失败，**5** 等待超时，**6** 恢复条件变化，**130** 用户中断。成功应读取 `status` 区分 planned/delivered。

## 8. 用户 skills（E3）

默认按「内置根 → `VAGENT_HOME/skills/`」发现，每根按目录名排序；不存在的用户根视为空，不自动从网络安装。内置名保留，用户重名忽略并返回 `SKILL_NAME_CONFLICT` 诊断。显式 `VAGENT_SKILLS_DIR` 延续现有**全量替换**语义，不再合并内置/用户根，避免改变既有配置含义。

继续使用严格 name/description frontmatter、目录与 name 一致、UTF-8、非空正文、单文件 ≤16 KiB、禁止 symlink/reparse。有效条目总计 ≤32：先保留内置，剩余用户条目按名取前，超限逐条诊断；坏用户文件隔离并报告 `INVALID_SKILL`，不使应用启动失败。内置或显式替换根的既有错误语义继续保留。诊断不包含正文或私有绝对路径。

版本仍为原始 SKILL.md 字节 SHA-256 前 12 位，元信息参与 Run 签名；正文由 `skill_read` 按需读取且核对版本。新目录发现/文件修改只影响新 Run，旧 Run 以其原 catalog 元信息签名恢复，不能把新用户文件自动注入旧 Run。删除/修改恢复所需的 skill 则明确 `RESUME_CONFIG_CHANGED`，不悄悄忽略。此扩展只读取说明，不执行插件、脚本、任意文件或网络引用。

## 9. 交付检查

E1 必须验证机械迁移、旧 Run 恢复、新媒体 union、流签名拒绝矩阵、时间戳/音画衔接、零提交、幂等与重做后版本、停止/崩溃补偿、HTTP Range/下载哈希、浏览器解码和安装态 binary/许可证；E2 必须验证受控 HTTP 协议、来源绑定反例、预算多桶原子性与全部旧签名；E3 必须验证 headless 的两条路径、退出码与恢复、用户 skills 隔离。E4 才执行完整离线/wheel/CI 与经明确授权的真实样本，未做真实样本的供应商保持标注。
