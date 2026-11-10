# v0.6.1 发布说明

**发布日期**：2026-11-04
**版本**：0.6.1
**范围**：v0.6.0（2026-10-25）之后的补丁修复，不新增功能；`master` 自 v0.6.0 起 9 个提交

本版把 v0.6.0 发布后发现的问题修掉，并补上一套不联网、不产生费用的供应商契约回放校验。产品域不扩：存储仍为 **schema 9**、工具面仍为 **18/18**、168 个契约模型哈希、`contractsSha256` 与 `capabilitiesSha256` 全部逐字节不变；本版**不新增付费样本**，不做 9:16 竖屏。

## 本版内容

- **Kling 3.0 Turbo 适配器对齐**（`e1b45f6`）：响应解析改用官方字段——`create_time`/`update_time` 是 Unix 毫秒而不是 ISO 字符串、交付时长取 `outputs[].duration`（官方从不返回 `usage` 块）、失败原因读 `message`；业务错误码按官方表逐码重映射（`1201` 由「内容拒绝」改为「参数无效」、`1004` 由「权限拒绝」改为「鉴权失败」，补齐 `1000`/`1100`/`1103`/`1200`/`1202`/`1203`/`1300`）。**含一处崩溃级修复**：`safe_provider_code` 原先可能产出 `JobErrorV2.provider_code` 拒绝的值（数字开头或含冒号），使错误处理路径自身抛 `ValidationError`。过程与证据见 [Kling 对齐与契约校验](./KLING_ALIGNMENT.md)。
- **零费用契约回放校验器**（`8022f55`、`b8e5c74`、`b5386b4`）：`scripts/verify_provider_contract.py` 把官方参考的应答体经 `httpx.MockTransport` 灌进生产 Kling/fal 适配器，**37 项检查**，无凭据、无 DNS、无付费任务；`tests/test_provider_contract_replay.py`（2 项）把它接进套件，并断言每个官方服务码都有对应检查。此前 mock 供应商与 `evaluate_m3` 的夹具都按适配器的假设手写，字段漂移对两者都不可见。
- **混音静音快速失败**（`4529146`）：`mixer.py` 在 loudnorm 之前用 `volumedetect` 探测 premix，`max_volume ≤ -90 dBFS` 时立即返回 `LOUDNESS_OUT_OF_RANGE`「（混音为静音，未交付）」，不再空跑测量/应用/复测/校正四遍；`render_service.py` 给 `AudioPlan` 增加 `dropped` 诊断（`no-dialogue-line`/`no-cue`/`outside-edit`），并在时间线音频全部落空时返回 `RENDER_SOURCE_MISSING`。
- **文档与流程**（`bea0554`、`0986650`、`2bdab2a`、`9b6953c`）：M9 竖屏按产品方决定暂缓，E0 契约与基线保留；README 重写为项目首页，操作手册移入 `docs/USER_GUIDE.md`；`docs/M8_CONTRACTS.md` §5.1 按官方服务码逐码重写；`docs/M5_E4_ACCEPTANCE.md` 登记 Windows 上 M5 离线评估的等待层偶发，评估夹具的等待上限由 300 秒放宽到 600 秒。本次发布提交另补 `docs/M3_CONTRACTS.md` 与 `docs/VIDEO_API_NOTES.md` 的对齐补记、README 的版本与验证数字，以及本文件。

## 验证

- **本地完整回归**：**1,522 passed / 4 skipped，另 1 例已登记偶发失败**（1,201.77 秒，0:20:01；skip 为平台条件：Windows 符号链接权限 3 例、POSIX 文件模式 1 例）。唯一失败是 `tests/test_m7_timeline_render.py::test_a_full_timeline_render_freezes_the_edit_and_replays`，与本版之前的记录一致，同一条用例单跑该文件 6/6 通过。
- **该偶发的新观察（本版未改代码）**：用例的 BGM 夹具是 1 秒数字静音，混音是否为「非静音」因此完全取决于 voice 轨是否落进剪辑窗口，voice 落空时混音按构造就是静音，断言必然失败。据此判断问题出在**用例夹具**的时序，而不是混音产品路径；修法（把 BGM 夹具换成非静音素材）留待后续单独验证，不在本版改动产品代码。
  > **2026-11-10 补记（v0.7.0）**：这条判断已撤回。CI 失败日志里没有 `（音频落空 N 条：…）` 后缀，说明 voice 轨并未落空；真正原因是 FFmpeg 7.1 偶尔只处理一帧就结束探测滤镜图，`volumedetect` 的 4096 样本 -91 dB 报告被误读为静音。根因、复现与修复见 [v0.7.0 发布说明](./RELEASE_0.7.0.md) 与 [M9 计划 §3.0.1](./M9_PLAN.md)。
- **静态检查**：`ruff check` 通过；`ruff format --check` **234 文件**；10 个前端状态模块 **91 项** Node 测试通过。
- **契约回放**：37/37 通过，退出码 0（`scripts/verify_provider_contract.py`）。
- **基线保护**：升到 0.6.1 后重新生成 `docs/M9_E0_BASELINE.json`，SHA-256 仍为 `afd1b38f…59b694b`，逐字节不变；`docs/M8_E0_BASELINE.json` 保持 `f3dc91c1…cb3706`。`src/vagent/video/assembler.py` 的 `PACKAGE_VERSION` 是冻结的装配引擎标识（写进每份成片的 `engine.packageVersion`），**不随包版本变化**，本版未改动。
- **安装态 wheel smoke**：**PASS**（`output/release-0.6.1-wheel-smoke.json`，23/23 检查、退出码 0、`temporaryDataCleaned=true`）。在独立环境 `tmp/wheel-env-061` 安装本版 wheel 后运行，`packagePath` 指向该环境的 `site-packages`（不是仓库副本）。覆盖文本/质量/MCP、Mock 视频工具、持久等待与重启续接、M1-B 五用例、M1-C 七用例、M2 工作台与七用例、M3 六用例（`submitCalls=11`、成片 6）与成片媒体 HTTP/CLI、M5 五用例（`submitCalls=42`、成片 18）、M6 三用例、M7 三用例、M8 四用例与 `route preview`/`usage bill`，以及两 Key 零费用探测与免重启登记；各段 `realProviderCalls=0`。
- **四组 CI**：发布提交 `e571bc9` 上四组矩阵（ubuntu / windows × Python 3.11 / 3.12）全部 **success**（[运行 41786694393](https://github.com/shifang37/vAgent/actions/runs/41786694393)；ubuntu 组约 19 分钟，windows 组约 62 分钟）。CI 覆盖 `ruff check` / `ruff format --check`、前端 10 个模块的语法检查与 Node 测试、全量 pytest、构建 wheel 并在独立环境安装后运行 `scripts/wheel_smoke.py`。

## 构建产物

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `shifang37_vagent-0.6.1-py3-none-any.whl` | 1,308,535 字节 | `defcdf8b44d2fa41e9d5220411ae613f5a0e3cbe2b2763561152c30d7d529f06` |
| `shifang37_vagent-0.6.1.tar.gz` | 2,426,899 字节 | `f1ff2e7e24f2c1866b555cbd5c503f31a1a9b250fd4de330d4dceb89a85f6e0b` |

哈希为本次构建实测值；从同一提交重新构建不保证逐字节一致。sdist 打包的是本文件定稿前的副本（`/docs/*.md` 进 sdist），因此 sdist 内不含本节的最终哈希；wheel 不含 `docs/`，其哈希不因本文件而后变。

## 安装

要求 Python 3.11+。从本仓库构建并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# 或安装已构建的 wheel：
.\.venv\Scripts\python.exe -m pip install dist\python\shifang37_vagent-0.6.1-py3-none-any.whl
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\vagent web --mcp-local
```

## 兼容与升级

- 存储仍为 **schema 9**：本版不新建、不迁移、不改任何集合；`state.json` 与 168 个契约模型哈希逐字节不变。
- 已冻结的成片、时间线、季、批量、账单与 Job 记录不受影响；历史 Kling 失败记录里的 `provider_code` 是自由文本 token，不参与任何冻结哈希，本版不改写历史。
- `PACKAGE_VERSION` 保持 `0.6.0`：它标识媒体格式代次而不是包版本，沿用 M8 的冻结口径。
- v0.6.0 之后的两处语义变化：Kling 业务码 `1201` 现在映射为 `PROVIDER_INVALID_REQUEST`、`1004` 映射为 `PROVIDER_AUTH_FAILED`；只有新产生的失败记录会带上新映射。
- 仍不支持 9:16 竖屏、不换汇、不做发送后的自动重投。

## 失败与偏差记录

1. **首轮安装态 smoke 的 M3 段失败一次，判为环境争用，非回归。** 该轮 `wan-film` 用例的 `run_completed`、`tool_chain`、`film_*` 等 8 项为 false，套件在第 3 个用例后中止（`finishedCases=3`）；同时在本机并行执行的还有 `pip install -r requirements-dev.lock` 与两次 `python -m build`。随后在同一个 wheel 环境、同样的 `-I` 与隔离变量下单独复跑 `scripts/evaluate_m3.py`，**6/6 通过**；独占重跑完整 smoke **PASS**。据此不改产品代码，按环境时序记录。失败那轮的临时证据目录（因异常落在仓库根而非系统临时目录）未被清理，已删除。
2. **升级到 0.6.1 未改动任何冻结基线。** 重新生成 `docs/M9_E0_BASELINE.json` 与 `docs/M8_E0_BASELINE.json` 后与前值逐字节一致；`capture_m9_baseline.py` 的 `FROZEN_RELEASE_VERSION` 仍固定记录冻结时的 0.6.0。
3. **本机 `output/` 在 v0.6.0 之后被清空**，历史证据（`output/m8-e4-*` 等）不在仓库内（`output/` 被 `.gitignore` 忽略），只能按各验收文档的复现命令重建。本版证据已重新生成：`output/release-0.6.1-wheel-smoke.json` 与 `output/provider-contract.json`（37/37）。

## 已知限制

- **Kling / fal 仍无真实付费样本**：缺 `VAGENT_KLING_API_KEY`/`FAL_KEY`，能力表保持「文档支持，未做真实样本」，`capabilitiesSha256` 不变。协议层已对齐并受回归保护，网络与账号这一层未验证；最小闭环约 0.98 USD（Kling 0.56、fal 0.42）。
- 万相与 CosyVoice 的供应商实际扣费仍未核对，账本依赖人工录入，缺失时保持 `unknown` 而不是 0。
- 时间线整集渲染的偶发失败未根除，诊断见上节（**v0.7.0 已定位并修复**，见 [v0.7.0 发布说明](./RELEASE_0.7.0.md)）。
- 9:16 竖屏按产品决定暂缓，M9 E0 契约与基线保留，E1 起未开始。
- 公共 PyPI 发布未做；本版只提供仓库内构建的 wheel/sdist。

## 证据索引

- 对齐与校验：[Kling 对齐与契约校验](./KLING_ALIGNMENT.md) · [M8 契约 §5.1](./M8_CONTRACTS.md) · [M3 契约](./M3_CONTRACTS.md) · [Kling/fal 官方核实](./VIDEO_API_NOTES.md)
- 离线与安装态证据：`output/release-0.6.1-wheel-smoke.json`、`output/provider-contract.json`
- 基线：`docs/M9_E0_BASELINE.json`（SHA-256 `afd1b38f…59b694b`）、`docs/M8_E0_BASELINE.json`（SHA-256 `f3dc91c1…cb3706`）
- 上一版：[v0.6.0 发布说明](./RELEASE_0.6.0.md)
