# M3 E0 验收：研究、契约与兼容基线

日期：**2026-10-21**。基线提交 `d06dc5f`，schema v4；M1/M2 已合入 master，K0–K3 已完成。**E0 完成；E1–E4 已按后续验收实施（视频 submit=0，见 [M3 验收](./M3_ACCEPTANCE.md)）。** 本次未读取凭证或真实数据目录、查询账号、上传或调用生成模型，未修改生产代码、依赖锁或存储版本。

## 1. 已冻结交付

| 项目 | 交付与边界 |
|---|---|
| 官方核实 | [VIDEO_API_NOTES 第 11–12 节](./VIDEO_API_NOTES.md)：Kling 新 API Key / Singapore / 3.0 Turbo t2v，以及 fal Kling v3 standard t2v 的精确参数、异步查询、公开单价、来源与未确认项 |
| 实施契约 | [M3_CONTRACTS](./M3_CONTRACTS.md)：schema v5、Film/FilmVersion/FilmMedia、事务与幂等、可取消本地拼接、版本恢复、多供应商与预算、headless、skills |
| 拼接选型 | A：固定 `imageio-ffmpeg==0.6.0` 包内 FFmpeg，concat + stream copy；系统安装不作后备，纯 Python mux 延后。运行依赖的正式引入、平台哈希与第三方分发材料由 E1 完成 |
| 版本 | E1 tools/rules 5/5，E2 6/6；execution/context/checkpoint 3/2/1；新通用 Job/Request v6，历史 1–5 不变；恢复须按完整 toolFeatures 身份还原冻结工具表 |
| 成片域 | 独立 FilmMedia union 分支，不伪造生成 Job；输入取当前分镜版本的最新已交付镜头，锁外拼接，锁内复核并原子登记；新流签名校验不能由旧 MP4 结构检查替代 |
| 最小 provider | Kling t2v / 720p / 16:9 / 5秒 / 原生音频；fal standard t2v / 16:9 / 5秒 / 显式静音，无不存在的 resolution 参数。其余组合顺延 |
| 价格与上限 | Kling 5秒估算 0.560 USD 与 4 VIDEO_UNIT，默认每 Job 0.60 USD 且4 units、每 Run 2.40 USD 且16 units；fal 0.420 USD，每 Job 0.50 USD、每 Run 2.00 USD；Wan 3/12 CNY 不变，全 Run 新 Job 共享最多4个。仅冻结估算，实际账单 unknown，默认上限不构成消费授权 |
| 来源与配置 | 新 provider 绑定认证任务完整 URL（含 path/query 指纹）、精确 origin 与策略，公网 DNS 校验并固定连接、禁重定向；Wan 静态允许集不变。新 Key 未有已核实免费鉴权接口时标注未验证，不套用 Wan 保存探测 |
| headless / skills | 先 planned，再以 Run ID、计划指纹与逐桶预算确认固定集合，resume 复用原授权；默认只规划仍可能产生文本编排费用。用户 skills 合并内置根，显式替换根语义与 raw 哈希保留，不执行代码 |
| 文档同步 | [M3_PLAN](./M3_PLAN.md)、[Harness §8.6](../AGENT_HARNESS_DESIGN.md)、[README](../README.md)、[M1_PLAN](../M1_PLAN.md) 已同步，未宣称生产成片或新 provider 已交付 |

证据分层：官方页面仅证明文档支持；合成实验仅证明固定夹具的本地行为；历史 K3/M2 真实证据不外推到新 provider。当前账号权限、地域可达性、真实编码、结果 CDN、计费与通用成片解码仍未验证。

## 2. schema v4 合成基线

[manifest.json](../tests/fixtures/m3/manifest.json) 和三份 JSON 快照由 [capture_m3_baseline.py](../scripts/capture_m3_baseline.py) 固定 UUID/时间、使用仓库测试构造器生成；所有 live Job 仅本地登记，HTTP transport 对任何请求报错。

| 快照 | 覆盖 |
|---|---|
| `legacy-v4-mock.json` | 既有 M2 v3 mock 夹具机械包入 v4，含等待/完成 Run、历史操作和 Job v1 |
| `legacy-v4-live.json` | 既有 M2 v3 live 合成夹具机械包入 v4，含 Job v2 成功媒体及引用 |
| `current-v4-shots.json` | 非空素材记录、两版分镜、t2v/i2v、5个 ShotVersion（含定向重做）、Job v3/v4/v5、两域 profile 与集合等待 |

manifest 固定原字节/canonical/集合哈希、Job 请求与意图指纹、Operation 指纹、媒体/素材哈希、分镜历史与 Run 签名。当前构造器还记录按真实注册表、内置 skills 元信息、合成模型身份计算的 Runner 签名及 tool/system prompt 哈希。`expectedV5CanonicalSha256` 仅计算 `{**v4, schemaVersion:5, films:{}}` 的预期，不调用或实现生产 v5 迁移。

这是领域 JSON 基线，**没有真实数据、像素文件或可恢复 SQLite 检查点**；历史占位签名有明确标注。测试核对完整性、跨对象引用、重复打开不迁移、检查点旁车哨兵不变和坏引用拒绝。E1 必须继续验证真实迁移的逐字节备份与 SQLite 恢复，不能把本基线当作这些能力已完成。

四份 JSON 重复生成后逐字节一致，补入 v5 预期哈希时三份 v4 原字节保持不变。快照从此作为 golden evidence 保留，不为迁就后续实现而重新生成。

## 3. 本地拼接实验与保留的反例

环境为 Windows amd64，本机 PATH 无 ffmpeg/ffprobe。实验依赖仅安装在被忽略的 `output/m3-e0-deps`，未写运行依赖或提交二进制。Windows wheel 为31,246,824字节，PyPI SHA-256 `02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a`；Linux x86_64 wheel 为29,498,237字节，SHA-256 `c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282`。包内本机 FFmpeg 版本为7.1 essentials gyan，二进制 SHA-256 `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`。来源见 [选型记录](./VIDEO_API_NOTES.md)。

输入为已提交的 `tests/fixtures/m1c/sample-720p.mp4` 两份：仓库生成的 testsrc2 与正弦声音，209,909字节、5秒、1280×720、10fps、H.264/AAC；SHA-256 `b179cca0ebb91e2b49a1e212df743037a8e292a3b754ee040c2124b31a3b6a54`。复现脚本为 [probe_m3_assembly.py](../scripts/probe_m3_assembly.py)，机器可读证据为 [M3_E0_ASSEMBLY_PROBE.json](./M3_E0_ASSEMBLY_PROBE.json)。

| concat 变体 | 返回码 / AAC DTS 警告 | 输出大小 / 时长 | SHA-256 |
|---|---|---|---|
| 默认时长 | 0 / 有 | 418,435字节 / 10.0秒 | `aff98d14483d6fd78df76a47a206d488cb742e2ea6f435a16156769ab6702012` |
| 每输入显式 duration `5.021333333`（仅本夹具） | 0 / 无 | 418,395字节 / 10.0212890625秒 | `41be1ed90fa7abfcdb3b3d46775d96695ca3e441f92adb15363be400ec21fd8a` |

两者通过既有 `inspect_mp4` 结构检查，证明结构通过不足以证明接缝正确。首个实验 stderr 保留的关键值为 `Non-monotonic DTS; previous: 240640, current: 240000; changing to 240641`；FFmpeg 自动调整并返回0，按冻结契约不能交付。夹具修正后的平均帧率为512000/51309，说明封装补时也会改变平均值，不能仅比较平均帧率作为编码兼容判断。

这个夹具专用常量**不是生产拼接算法**。E1 要从样本/预卷推导时序、记录偏移并验证上限，还须完成连续浏览器解码、停止/崩溃、异规格拒绝与其他平台验收。本次没有这些证据，也未以已有真实媒体做实验。

Python wrapper 许可证为 BSD-2-Clause，本机随包 FFmpeg `-L` 为 GPL v3-or-later；二者不可混同。E1 须按各平台实际构建提供对应许可证、构建来源与源码获取/必要分发材料，并完成安装态验证。

复现命令（在仓库根执行，输出目录须尚不存在）：

```powershell
.venv\Scripts\python.exe -m pip install --only-binary=:all: --no-deps --target output/m3-e0-deps imageio-ffmpeg==0.6.0
.venv\Scripts\python.exe scripts/probe_m3_assembly.py --ffmpeg output/m3-e0-deps/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe --output-dir output/m3-e0-probe-record
```

本次本地原始 `version.txt`、`license.txt`、两组 stderr/concat 清单/输出媒体保留在被忽略的 `output/m3-e0-probe-record/`；提交的报告不含本机路径与第三方二进制。

## 4. 检查结果

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_m3_baseline.py tests/test_m2_migration.py tests/test_keys_only.py tests/test_m2_compatibility.py tests/test_context_skills.py
.venv\Scripts\python.exe -m ruff check scripts/capture_m3_baseline.py scripts/probe_m3_assembly.py tests/test_m3_baseline.py
git diff --check
```

- 定向回归：**50 passed / 1 skipped**，其中新增基线11项；跳过为当前 Windows 账号无 symlink 权限的既有 skills 测试。
- Ruff：通过。拼接实验：两变体均已执行，默认警告如实保留，夹具修正变体通过本地结构检查。
- 文档链接与 diff 空白检查：通过。未重跑全量套件、wheel、CI 或受控浏览器；历史 K3 的958 passed / 2 skipped不是本次结果。
- 视频 submit=0；未新增生成任务或产生视频供应商费用，生产存储仍为 schema v4。

## 5. 下一工作包 E1

先以冻结基线实现 schema v5 机械迁移、Film/FilmVersion 和 FilmMedia union，再完成精确工具版本恢复及可取消异步写路径；随后实现样本/编码配置解析、流复制拼接和原子文件/Store 提交，最后接入 CLI/Web 并完成故障、安装态与浏览器验收。E1 保持 submit=0，Kling/fal 的生产接入由 E2 实施。
