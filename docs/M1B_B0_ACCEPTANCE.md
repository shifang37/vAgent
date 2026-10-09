# M1-B B0 契约与等待验证

日期：2026-10-09。范围：B0 的数据/错误/迁移协议、LangGraph 持久等待实验与旧检查点兼容。协议见 [M1-B 契约](./M1B_CONTRACTS.md)，后续任务见 [M1-B 计划](./M1B_PLAN.md)。

**B0 已完成并通过本地验收。** 完整回归为 **254 passed、1 skipped**，其中新增 B0 用例 44 项；独立 wheel 安装与仓库外验证通过。本次没有真实模型或视频供应商请求。

## 交付内容

- 严格、不可变的视频请求/规格/来源、能力表、Job/JobResult、查询策略与供应商协议；来源范围和版本校验、稳定请求指纹、合法状态迁移关系。
- 通用执行上下文、延迟结果、等待阶段、恢复指针与成功/失败工具结果。等待模块不导入视频适配器。
- 独立离线实验 `scripts/probe_m1b_wait.py`，使用真实 FileStore 项目工具、Operation 日志和 SQLite 同步图检查点；重建图后恢复原工具调用。
- 在 a03f249 上事先取得的两种上下文配置指纹；旧 execution v1 的实际保存/中断/关闭/恢复回归。
- schema v2 迁移顺序、JSON/SQLite 提交间隙处理及 execution v1/v2 分流规则已写入契约文档。迁移和应用调度尚未实施。

当前 CLI/Web 的工具注册、系统规则、Store schema 和 AgentRunner 执行图保持原行为，仍不提供视频 Job。实验的 `b0-probe.json` 只存在于其临时目录，不能作为后续应用的正式等待存储格式。

## 运行环境

本机 Windows、Python 3.12；LangGraph 1.2.12、langgraph-checkpoint 4.2.0、SQLite saver 3.1.1、Pydantic 2.13.5。没有变更依赖版本。Windows 现有符号链接权限用例跳过，未将本地结果当作其他系统或远端 CI 结果。

## 验证结果

| 场景 | 独立验证的结果 |
|---|---|
| 能力与输入边界 | 两种适配器规格可变化；非法规格组合、非有限/非正数、字符串数字、越权字段、旧能力版本均被拒绝 |
| 来源与请求冻结 | 真实 FileStore 产物检查项目和确切版本；源产物更新后旧版本仍在；请求嵌套字段不可变，指纹跨 JSON 读取不变 |
| 状态与错误 | unknown 不能自动重提；查询暂停仍保留 running 和原上游 ID；重试窗口遵循保存的策略；错误阶段与终态载荷不一致时拒绝持久化 |
| 模拟结果 | 不允许模拟结果声明真实媒体、输出占位产物或引用另一请求的规格/指纹 |
| 单次等待重开存储 | 暂停时 1 次离线模型调用、1 个产物；重启继续后累计 2 次调用、3 个工具结果、2 个产物，均只有一个版本 |
| 等待期间重复检查 | 不增加模型调用；资源未就绪时拒绝交付；最终重复唤醒不重开图 |
| 同批两个等待 | 原中断顺序保持，wait-0/wait-1 分别得到自己的 resourceId；累计 2 次模型调用、5 次工具额度、3 个产物，无重复版本 |
| 完成竞态与失败结果 | 资源先完成可直接返回；失败先于等待 arm 时仍保留原错误，按原 toolCallId 回填一次，没有被改成成功 |
| 恢复输入 | 旧代次、未就绪/已停止绑定、额外注入结果和不可持久 JSON 被拒绝 |
| 旧 execution v1 | 两种上下文格式均保持基线指纹；原 Run/预算继续，累计模型调用 2 → 3、工具次数仍为 1，原产物和 Operation 未变 |

强退测试由真实子进程执行 `os._exit(73)`，不是正常关闭模拟。仅确认该测试子进程已退出后，清除其临时目录中的遗留锁：

| 强退位置 | 重启结果 |
|---|---|
| 等待准备已落盘、尚未形成中断 | 重建原 tools 节点到中断；复用已完成写入，不增加模型调用 |
| SQLite 中断已保存、尚未 arm | 核对中断后补 arm，再读取完成结果 |
| arm 已落盘 | 通过原调用继续一次 |
| 原工具结果已保存、图节点尚未提交 | 复用原 Operation 结果，不重复副作用 |
| 图终态已保存、交付标记尚未更新 | 只补 delivered，不新增模型调用 |

全部强退用例最终保留原产物 ID，每个产物仅一个版本；工具调用/结果配对完整。

## 本轮发现与固定的规则

- 初次聚焦测试为 35 项通过、1 项失败；失败暴露出同一节点第二次中断时 `snapshot.next` 可能为空。恢复入口现先检查 pending interrupts，再判断图结束，多等待及强退复验通过。
- GraphInterrupt 是 Exception，直接穿过当前工具 `except Exception` 会被转换成普通错误。验证采用先返回 DeferredToolResult、再由图节点在错误处理之外 interrupt 的路径；生产接入留给 B2/B3。
- 节点重放必须保留原 interrupt 的顺序；已完成本地结果只避免重复副作用，不能随意跳过框架的恢复位置。
- JSON 与 SQLite 没有跨文件事务。实验验证原结果可重复读取/补交，不声称外部请求恰好一次，也未验证生产协调器的并发领取或完整 Run 停止/预算逻辑。

## 可复现入口与证据

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_video_contracts.py tests/test_waiting_probe.py tests/test_b0_compatibility.py
.\.venv\Scripts\python.exe scripts/probe_m1b_wait.py --output output/m1b-b0-wait-new.json
.\.venv\Scripts\python.exe -m ruff check src/vagent tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src/vagent tests scripts
node --check web/app.js
.\.venv\Scripts\python.exe -m pytest -q
```

脚本默认使用自动清理的独立临时数据目录，不读取用户配置、不调用供应商；输出文件已存在时拒绝覆盖。初次成功报告保留在被 Git 忽略的 `output/m1b-b0-wait-20261009.json`，之后增加的失败竞态、JSON 边界与查询窗口测试由完整回归覆盖。

完整回归用时 51.64 秒：254 passed、1 skipped。Ruff lint/format、前端语法和 `pip check` 通过。

sdist/wheel 构建通过。新建独立虚拟环境，按锁定依赖安装本次 wheel，并在仓库目录之外完成：

- 确认 vagent 从独立环境的 site-packages 导入；CLI 的 config show、skills list、demo、inspect 和 usage 通过。
- 安装后的 Web 页面、配置、流式 Runner、真实本地 MCP、产物及质量拒绝验证通过；新增视频契约与恢复指针可导入，应用 videoGeneration 仍为 false。
- 使用安装后的库运行 B0 SQLite 等待实验，仍为 2 次离线模型调用、3 次工具结果和 2 个单版本产物，重复唤醒没有新增调用。
- `pip check` 通过，临时安装环境和工作目录均已清理；没有启动用户数据目录的服务。

安装证据为 `output/m1b-b0-wheel-20261009.json`，过程日志为同名 `.log`；二者均被 Git 忽略。报告 SHA-256：`862a3c140024bf44b9a40d16876c6d7d06db35039e734effab0a2a419af48558`。已验证 wheel 为 `dist/python/shifang37_vagent-0.2.0-py3-none-any.whl`，SHA-256：`05c45bf872f7eecc2f80fbb23984dffcedd0dbb6976605d8aadcb2a84517fa86`；未发布 PyPI。

## 下一工作包

B1 按已固定的契约实现 schema v2 迁移、原子 Job 登记、Mock 账本、Worker 提交/查询和不确定状态处理。B2 再注册四个 Agent 工具，B3 接入生产 Run 等待/停止/预算与自动恢复；B0 不代替这些验收。
