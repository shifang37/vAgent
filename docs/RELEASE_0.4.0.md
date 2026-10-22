# v0.4.0 发布说明

**发布日期**：2026-10-22  
**版本**：0.4.0  
**M5 E4 代码提交**：`6c2a336`（文档同步 `2ca05fe`）  
**范围**：M1-A/B/C、M2 D0–D4、M3 E0–E4、M4、M5 E0–E4、K0–K3

本版把 **M4 自动时间线归一化**与 **M5 剧本/小说 → 分集分场分镜**合入 `master`，形成覆盖 M1–M5 的发布：从一句话创作、结构化分镜、多镜头生成、逐镜头重做，到自动归一化拼接成片、剧本改编为分集分场分镜、整集批量登记与逐镜头重做，以及 CLI/Web/headless 与分集工作台。

## 本版内容

- **Agent 底座（沿用 0.3.0）**：Python CLI + 本地 Web，共用 LangGraph Runner；上下文 v2、项目记忆、Skills、严格工具 Schema、检查点恢复、预算/停止、质量校验、MCP、Redis 回答缓存与用量观测。
- **M4 自动时间线归一化**：`film assemble` 仍以 `mp4-copy-signature-v1` 流复制优先；逐字段签名不兼容时自动回退 `ffmpeg-concat-normalize-v4`（1280×720/30fps/H.264 + AAC 48k 立体声，无音轨补静音），成片版本记录 `normalization` profile。真实 i2v + flf2v 两段合成 **5,968,116 字节 / 10.000 秒 / 30fps**。
- **M5 分集分场分镜**：schema **v6** 的 `episodes`/`scenes`/`characters`/`styles`；`text-split-zh-v1` 确定性章节/场景/对白切分（对白零丢失）与镜头草案；角色与风格圣经、`style-prompt-zh-v1` 风格模板、镜头 `characterIds` 冻结到角色版本与参考图；`episode_generate` 整集批量登记与 `shot_redo` 逐镜头重做，集级额度**跨 Run 累计**（每集 20 Job / 60.00 CNY，逐镜头 0.60 CNY/秒）；`episode_read` 给出逐镜头进度与逐角色一致性证据；工具面 **9/9**（冻结 8/8 至 1/1 变体）。
- **CLI/Web**：`vagent episode plan/save/show/generate`；Web 只读分集面板（`/api/episodes`、逐场成片播放/下载、角色一致性证据与 1–5 分人工评分），会话快照复用同一投影。
- **既有能力不变**：万相北京 t2v/i2v/flf2v 真实样本、Kling/fal 受控接入与逐供应商预算桶、headless 两阶段授权、用户 skills 合并发现、成片 Range/下载、两 Key 直用 K0–K3。

## 验证

- 本地 Python 3.12.14 全量回归：**1233 passed / 3 skipped**（786.38 秒）；3 项跳过为 Windows 账户无符号链接创建权限的既有边界。
- `ruff check` 通过；`ruff format --check` 通过（164 个文件）。
- 前端：`node --check` 5 个模块通过；`node --test` **36/36** 通过（job/storyboard/film/episode 四组纯状态测试）。
- 四组 CI（ubuntu/windows × Python 3.11/3.12）在 `6c2a336` 上全部 success（[运行 39766146694](https://github.com/shifang37/vAgent/actions/runs/39766146694)）。
- 安装态 wheel smoke：**PASS**（`output/release-0.4.0-wheel-smoke.json`，从 `site-packages` 导入并清理临时数据）。覆盖文本/质量/MCP、Mock 视频工具、持久等待与重启续接、M1-B 五用例、M1-C 七用例（含 live 协议与本地媒体重启）、M2 工作台与七用例、M3 成片拼接 + 媒体 HTTP/CLI 重放 + 多供应商设置与六用例、**M5 分集工作台与五用例（5/5、42 次受控提交、41 个交付、18 个成片版本、2 次重做、真实供应商与模型调用 0）**、两 Key 共享域零费用探测与热更新。
- 受控浏览器：**5/5** 阶段（分集面板 7 场、18/18 交付、`54.00 / 60.00 CNY`、成片 1280×720 解码与 Range）；M3 阶段此前 7/7 的证据继续有效。
- 唯一新增真实付费样本：`fridge-notes` 全篇 **18 次 Wan 文生视频提交**（估算 **54.00 CNY**，实际账单 `unknown`），交付 **7 条逐场成片**（10.096549 / 15.159766 秒，1280×720 H.264+AAC）；文本模型调用 = 0，未使用 Kling/fal 真实账号。

## 构建产物

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `shifang37_vagent-0.4.0-py3-none-any.whl` | 362,209 字节 | `56b38e77d16df1c1780c5922a78384a4c0f7fa58750d52b05ad2e50bff046586` |
| `shifang37_vagent-0.4.0.tar.gz` | 1,203,617 字节 | `aca54a24e30f8f9b74ee4cf483537b04a99cac165321a73de452481ba6b2c7cd` |

哈希为本次发布实测值；从同一提交重新构建不保证逐字节一致，请以 `git show v0.4.0` 与本节记录为准。

## 安装

要求 Python 3.11+。从本仓库构建并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# 或安装已构建的 wheel：
.\.venv\Scripts\python.exe -m pip install dist\python\shifang37_vagent-0.4.0-py3-none-any.whl
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\vagent web --mcp-local
```

打开 `http://127.0.0.1:3210`，在设置页填写编排模型与视频供应商 Key。`vagent episode plan "剧本.txt"` 可离线改编为分集分场分镜；`vagent headless "..."` 在无 Web 环境只规划，显式授权后才登记付费任务。`dist\python\archive-0.3.0\` 保留上一版构建产物，`dist\python` 根目录只放当前版本，避免安装态脚本误取旧 wheel。

## 兼容与升级

- 存储升为 **schema v6**。打开 v1–v5 数据时在实例锁下先用原版本校验器验证，再把原始字节备份为 `state-v1-<UUID>.json` 并原子迁移；v5→v6 只增加四个空集合（`episodes`/`scenes`/`characters`/`styles`），不重编码历史记录。
- v6 不能被 0.3.0 及更早版本直接读取；回退需在服务退出后恢复完整数据目录，单份状态备份不能替代。
- 从 0.3.0 升级：先备份整个 `VAGENT_HOME` 再启动。旧 Run 按 `toolFeatures` 身份重建冻结注册表（9/9 … 1/1），签名逐字节不变；execution/context/checkpoint 仍为 3/2/1。
- 旧成片不含 `normalization` 字段时仍按原校验器读取，不补写；新成片记录实际使用的拼接 profile。
- 配置与 Key 的存放方式不变；Kling/fal Key 只在对应供应商请求中使用。

## 已知限制

- **整集成片（跨场组合）未做**：本版成片口径是逐场成片（一个场景至多绑定一个分镜），一条 90 秒整集短剧属 M6。
- **无字幕、配音（TTS）、配乐/音效、转场、响度归一化、片头片尾与 1080P 导出**：均属 M6。
- Kling `kling-3.0-turbo` 与 fal `kling-video/v3/standard/text-to-video` 未做真实账号样本，真实结果主机、编码与账单未验证。
- 真实 DeepSeek 分集编排未执行（M5 E4 模型调用 = 0）；真实 i2v 角色一致性样本未做（真实样本为 t2v）。
- 实际账单仍为 `unknown`，估算不当作扣费凭据。
- 不支持 9:16 竖屏；未接入 Sora/Veo/Seedance/Replicate；可执行插件/插件市场与 Studio 时间线编辑未实现。
- 公共 PyPI 发布未做；本版只提供仓库内构建的 wheel/sdist。

## 证据索引

- 里程碑：[M1-A](./M1A_ACCEPTANCE.md)、[M1-B](./M1B_ACCEPTANCE.md)、[M1-C](./M1C_ACCEPTANCE.md)、[M2](./M2_ACCEPTANCE.md)、[M3](./M3_ACCEPTANCE.md)、[M4 归一化](./M4_NORMALIZATION.md)、[M5 E4](./M5_E4_ACCEPTANCE.md)
- M5 全链：[M5 计划](./M5_PLAN.md)、[M5 契约](./M5_CONTRACTS.md)、[E0](./M5_E0_ACCEPTANCE.md)、[E1](./M5_E1_ACCEPTANCE.md)、[E2](./M5_E2_ACCEPTANCE.md)、[E3](./M5_E3_ACCEPTANCE.md)、[E4](./M5_E4_ACCEPTANCE.md)
- 相关：[i2v/flf2v 真实样本](./M2_I2V_ACCEPTANCE.md)、[两 Key 直用 K3](./KEYS_ONLY_K3_ACCEPTANCE.md)、[官方核实](./VIDEO_API_NOTES.md)
- 上一版：[v0.3.0 发布说明](./RELEASE_0.3.0.md)
