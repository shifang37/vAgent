# v0.3.0 发布说明

**发布日期**：2026-10-21  
**版本**：0.3.0  
**M3 合并提交**：`ef8bdda`  
**范围**：M1-A/B/C、M2 D0–D4、M3 E0–E4、K0–K3

本版把 M3 合入 `master`，形成首个覆盖 M1–M3 的完整发布：从一句话创作、结构化分镜、多镜头生成、逐镜头重做，到本地拼接成片、CLI/Web 播放下载，以及 headless 与用户 skills。

## 本版内容

- **Agent 底座**：Python CLI + 本地 Web，共用 LangGraph Runner；上下文 v2、项目记忆、Skills、严格工具 Schema、检查点恢复、预算/停止、质量校验、MCP、Redis 回答缓存与用量观测。
- **真实视频链路**：万相北京 `wan2.7-t2v`（单镜头）与 M2 多镜头、逐镜头重做、素材库、序列连播；真实样本与本地 MP4 交付已验收。
- **M3 本地成片**：schema v5 的 `films`、`mp4-copy-signature-v1` 流复制签名、包内 `imageio-ffmpeg` 拼接、`film_assemble`/`film_read`、`vagent film assemble|show`、Web 成片面板与 Range/下载。
- **多供应商**：Job/Request v6 与 `multi-provider-v3`，Kling Singapore 直连、fal standard 静音中转；逐 provider/currency 与 Kling `VIDEO_UNIT` 预算桶、结果来源绑定。
- **两 Key 直用 K0–K3**：设置页只填编排模型 Key + 视频供应商 Key；Wan 共享域名解析与零费用探测；Kling/fal 未验证标注；Key 热更新。
- **headless 与扩展点**：`vagent headless` 两阶段计划/授权/集合等待/本地拼接交付，固定 `--json` 与退出码；`VAGENT_HOME/skills` 合并发现、内置名优先、坏文件隔离。

## 验证

- 本地 Python 3.12.14 全量回归：**1117 passed / 3 skipped**（451.28 秒）；3 项跳过为 Windows 无符号链接权限与平台专用夹具的既有边界。
- 前端状态测试：**31/31**；`ruff check` 与 `ruff format --check` 通过（148 个文件）。
- 四组 CI（ubuntu/windows × Python 3.11/3.12）在 `ef8bdda` 上通过（[run #71](https://github.com/shifang37/vAgent/actions/runs/39623192395)）。
- 安装态 wheel smoke：PASS（含 M3 成片工作台、6 用例离线套件、两 Key 共享域夹具、仓库外 CLI）。
- 本版 wheel：`shifang37_vagent-0.3.0-py3-none-any.whl`，315,892 字节，SHA-256 `eb596fdbeaeedc093366826f72c1de0ceb0e76aaaea8da40370f3d7ef7d53d24`。
- 受控浏览器：**7/7** 阶段、0 失败；真实数据成片 42 项检查通过（4,501,422 字节 / 10.096549 秒）。

## 安装

要求 Python 3.11+。从本仓库构建并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
# 或安装已构建的 wheel：
.\.venv\Scripts\python.exe -m pip install dist\python\shifang37_vagent-0.3.0-py3-none-any.whl
$env:VAGENT_HOME = Join-Path $PWD '.vagent'
.\.venv\Scripts\vagent web --mcp-local
```

打开 `http://127.0.0.1:3210`，在设置页填写编排模型与视频供应商 Key。`vagent headless "..."` 可在无 Web 环境只规划；显式授权后才登记付费任务。

## 兼容与升级

- 存储为 schema v5。打开 v1–v4 数据时在实例锁下先备份原始字节（`state-v1-<UUID>.json` 等），再原子迁移；迁移只增加集合与版本，不重编码历史记录。
- v5 不能被更旧的应用直接读取；回退需在服务退出后恢复完整数据目录。
- 从 0.2.0 升级建议先备份 `VAGENT_HOME` 整个目录，再启动新版本。
- 配置与 Key 的存放方式不变；Kling/fal Key 只在对应供应商请求中使用。

## 已知限制

- Kling `kling-3.0-turbo` 与 fal `kling-video/v3/standard/text-to-video` 未做真实账号样本，真实结果主机、编码与账单未验证。
- headless 的真实 DeepSeek 计划与真实付费授权未执行；离线与受控供应商证据继续有效。
- Wan 图生视频/首尾帧已实现并有离线和受控 HTTP 验证，但未执行真实付费样本。
- 成片只做同参数镜头间的流复制拼接，不做转场、字幕、配乐、转码或导出参数定制；镜头延长因上游通道限制未实现。
- 未接入 Sora、Veo、Seedance、Replicate；可执行插件/插件市场、Studio 时间线编辑、素材删除/检索、按能力表自动选模型、跨项目记忆与自动摘要仍未实现。
- 公共 PyPI 发布未做；本版只提供仓库内构建的 wheel/sdist。
- 实际账单仍为 `unknown`，首次真实使用前请自行核对供应商价格与账号权限。

## 证据索引

- [M1-A 验收](./M1A_ACCEPTANCE.md)、[M1-B 验收](./M1B_ACCEPTANCE.md)、[M1-C 验收](./M1C_ACCEPTANCE.md)
- [M2 验收](./M2_ACCEPTANCE.md)、[M2 D1](./M2_D1_ACCEPTANCE.md)、[M2 D2](./M2_D2_ACCEPTANCE.md)、[M2 D3](./M2_D3_ACCEPTANCE.md)
- [M3 验收](./M3_ACCEPTANCE.md)、[E0](./M3_E0_ACCEPTANCE.md)、[E1](./M3_E1_ACCEPTANCE.md)、[E2](./M3_E2_ACCEPTANCE.md)、[E3](./M3_E3_ACCEPTANCE.md)
- [两 Key 直用 K1](./KEYS_ONLY_K1_ACCEPTANCE.md)、[K2](./KEYS_ONLY_K2_ACCEPTANCE.md)、[K3](./KEYS_ONLY_K3_ACCEPTANCE.md)
- 契约与边界：[M3 契约](./M3_CONTRACTS.md)、[M3 任务规划](./M3_PLAN.md)、[M3 验收第 9 节](./M3_ACCEPTANCE.md#L122-L127)
