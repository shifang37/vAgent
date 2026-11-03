# Kling 3.0 Turbo 适配器对齐与零成本契约校验

状态：**已实现并验证**（2026-11-03，`master`）。范围：把 Kling 直连适配器的响应解析与业务错误码表对齐官方 3.0 Turbo 参考，并新增一套不联网、不产生费用的契约回放校验器，补齐「mock 通过 ≠ 真实接口可用」这段证据空白。**Kling 与 fal 的真实付费样本仍未做**，见 §6。

## 1. 为什么要做

`VAGENT_VIDEO_MODE=mock` 注册的是独立的模拟供应商，`scripts/evaluate_m3.py` 的 `MockTransport` 应答体也是手写的。两者都能证明 Job / Worker / 媒体 / 成片流水线，但**都不能证明适配器读的字段名与厂商规范一致**——因为夹具是按适配器的假设写的，不是按厂商参考写的。

典型证据：修复前 `evaluate_m3.py` 的 Kling 夹具里写着 `"usage": {"duration": 5}`，而官方查询接口从不返回 `usage`（时长在 `outputs[].duration`）。这套夹具让离线套件永远绿，却把缺陷固化了下来。

## 2. 发现的问题（修复前实测）

用官方应答体直接喂进生产 `KlingAdapter`，结果如下：

| # | 现象 | 官方字段 | 影响 |
|---|---|---|---|
| 1 | `submit_time` 恒为 `null` | `create_time`（Unix 毫秒） | 提交时间丢失 |
| 2 | `end_time` 恒为 `null` | `update_time`（Unix 毫秒） | 结束时间丢失 |
| 3 | `usage` 恒为 `null` | `outputs[].duration`（字符串） | 交付时长丢失 |
| 4 | 失败原因为空 | `message`（适配器读的是不存在的 `error`） | 厂商原因不可见，`provider_code` 恒 `null` |
| 5 | `1201` 判为内容拒绝 | **Invalid parameters**（参数错） | 配置写错被误报成"内容被拒"，引导去改提示词 |
| 6 | `1004` 判为权限拒绝 | **Authorization has expired**（鉴权过期） | Key 过期被误报成权限不足 |
| 7 | `safe_provider_code` 可能产出契约拒绝的值 | `JobErrorV2.provider_code` 要求字母开头 | 厂商以字符串返码时，**错误处理路径本身**抛 `ValidationError` |

第 7 项是崩溃级缺陷，且发生在最不该出错的位置。另外错误码表缺 `1000`、`1100`、`1103`、`1200`、`1202`、`1203`、`1300`。

## 3. 修复

### 3.1 响应解析（`src/vagent/video/providers/kling.py`）

| 项 | 修复 |
|---|---|
| 时间 | `create_time` / `update_time` 按 Unix 毫秒解释，输出 ISO 8601（毫秒精度，UTC） |
| 时长 | 从 `outputs[].duration` 的十进制字符串解析为 `float`，含 `isfinite` 与非负校验 |
| 失败原因 | 读 `message`，拼进用户可见文案（截断 400 字符）；空值回退为固定文案 |
| `provider_code` | 只接受码形 token；自由文本一律不进该字段（正则 `^[A-Za-z][A-Za-z0-9_.-]*$`） |

### 3.2 业务错误码（对齐 [官方错误码](https://kling.ai/document-api/api/get-started/error-codes)）

| 官方码 | HTTP | 含义 | 本地错误码 |
|---|---|---|---|
| `1000`–`1004` | 401 | 鉴权失败 / 为空 / 无效 / 未生效 / 已过期 | `PROVIDER_AUTH_FAILED` |
| `1100`–`1102` | 429 | 账号异常 / 欠费 / 资源包耗尽 | `PROVIDER_BILLING_BLOCKED` |
| `1103` | 403 | 无权访问该 API 或模型 | `PROVIDER_ACCESS_DENIED` |
| `1200`、`1201` | 400 | 请求参数无效 | `PROVIDER_INVALID_REQUEST` |
| `1202` | 404 | 请求方法无效 | `PROVIDER_INVALID_REQUEST` |
| `1203` | 404 | 模型或资源不存在 | `PROVIDER_MODEL_UNAVAILABLE` |
| `1300`、`1301` | 400 | 平台策略 / 内容安全 | `PROVIDER_CONTENT_REJECTED` |
| `1302`、`1303` | 429 | 限流 / 并发超资源包 | `PROVIDER_RATE_LIMITED` |
| `1304` | 429 | IP 白名单策略 | `PROVIDER_ACCESS_DENIED` |

### 3.3 校验器自身的防崩溃约束（`src/vagent/video/providers/protocol.py`）

`safe_provider_code` 的正则收窄为 `JobErrorV2.provider_code` 的**严格子集**（`[A-Za-z][A-Za-z0-9_.-]{0,127}`）。数字开头或含冒号的码会被丢弃为 `None`，与整型码的既有行为一致，但不会再产出契约验证阶段才炸的值。

## 4. 零成本契约校验器

`scripts/verify_provider_contract.py` 把官方参考里的应答体灌进生产适配器，经 `httpx.MockTransport` 拦截，**无凭据、无 DNS、无付费任务**。

| 分组 | 检查数 | 覆盖 |
|---|---|---|
| `kling.success.*` | 6 | 状态、视频 URL、结果 origin、两个时间戳、交付时长 |
| `kling.failure.*` | 4 | 失败状态、失败码、`provider_code` 码形、厂商原因回填 |
| `kling.status_map.*` | 4 | `submitted` / `processing` / `succeeded` / `failed` |
| `kling.service_code.*` | 18 | 上表全部官方服务码 |
| `fal.success.*` / `fal.status_map.*` / `fal.failure.*` | 5 | 队列 `IN_QUEUE` → `IN_PROGRESS` → `COMPLETED`、app-root 结果、`error` 失败 |

```powershell
.\.venv\Scripts\python.exe scripts/verify_provider_contract.py
# {"status": "passed", "totals": {"checks": 37, "passed": 37, "failed": 0}, "failedChecks": []}
```

退出码 0/1；`--output` 可写完整 JSON 报告。规范侧（`OFFICIAL_SERVICE_CODES`）与适配器侧（`_REJECTIONS`）**刻意各写一份**，改一边会被另一边抓住：

```text
# 反向对照：把规范侧 1201 改回旧的错误期望
status       : failed
failedChecks : ['kling.service_code.1201']
```

`tests/test_provider_contract_replay.py` 把校验器接进套件，并断言每个官方码都有对应检查，避免它悄悄腐化。

## 5. 验证证据

| 检查 | 结果 |
|---|---|
| `ruff check` + `ruff format --check` | 234 个文件通过 |
| `pytest tests/test_m3_providers.py` | 11 passed（含新增失败原因用例） |
| 全量 pytest | 1,522 passed / 4 skipped，另有 1 例顺序相关 flake，见 §6 |
| `scripts/evaluate_m3.py`（离线） | 6/6 passed，`realProviderCalls: 0` |
| 契约回放 | 37/37 passed |

## 6. 边界与未验证项

- **Kling 与 fal 仍无真实付费样本**。能力表继续标注「文档支持，未做真实样本」；协议层已对齐并受回归保护，但网络、账户、配额这一层未验证。
- 真实采样的最小闭环成本：Kling 直连 **0.56 USD**（5 秒 / 720P / 含原生音频，4 VIDEO_UNIT），fal 中转 **0.42 USD**（静音）。
- fal 侧只校验队列骨架与 `video.url` 读取；fal 结果体若新增必填字段，需要追加对应检查。
- `provider_code` 不放厂商自由文本，属刻意取舍：该字段是码形 token，人的原因走 `message`。
- 全量套件存在 1 例与顺序相关的偶发失败（`test_m7_timeline_render.py` 的整集时间线渲染，混音测得 `integratedLufs=-70.0`），单跑该文件 6/6 通过；根因未修，记录见 [M9 计划 §3.0](./M9_PLAN.md)。

## 7. 相关文档

- 供应商路由与提交/查询错误映射：[M8 契约 §5.1](./M8_CONTRACTS.md)
- 官方参考：[Kling 3.0 Turbo 文生视频](https://kling.ai/document-api/api/video/3-0-turbo/text-to-video) · [鉴权](https://kling.ai/document-api/api/get-started/authentication) · [错误码](https://kling.ai/document-api/api/get-started/error-codes) · [价格](https://kling.ai/document-api/pricing/base/video)
