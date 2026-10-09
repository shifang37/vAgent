# M1-B B1 验收：持久 Job 与 Mock Worker

日期：2026-10-09。范围：schema v2 迁移、原子 Job 登记、两层去重、独立 Mock 账本、串行 Worker、查询重试与重启恢复。实现基于 B0 提交 `fdb661a`；任务边界见 [M1-B 计划](./M1B_PLAN.md)，数据约束见 [契约协议](./M1B_CONTRACTS.md)。

**B1 已完成。** 不调用模型也能登记并推进模拟 Job；已确认的提交不重提，无法确定受理结果的提交保留 unknown。CLI/Web 尚未注册视频工具或自动启动 Worker，生产等待、模式配置和页面属于 B2–B4。全部验证使用临时目录，没有调用 DeepSeek 或真实视频 API。

## 交付内容

| 位置 | 行为 |
|---|---|
| `storage.py` | 严格区分 v1/v2；在原实例锁下验证旧数据、原子保存原始字节快照，再迁移至 jobs/waits 根结构；保留旧消息格式、指纹、产物和用量 |
| `video/jobs.py` | Job 与生成 Operation 同事务；服务端上下文与来源校验、不可变请求/能力/策略快照、每 Run 一个 Job、项目内查询、revision 和状态迁移检查、恢复暂停查询 |
| `video/providers/mock.py` | 独立 `mock-video.json` 保存受理时间、请求、场景、轨迹位置及 submit/query 计数；支持排队、成功、失败、拒绝受理、查询中断和已受理但响应丢失 |
| `video/worker.py` | 从持久队列每次选择一个到期任务；同一 Store 串行执行；事务外 submit/query、有界超时、查询重试、start/stop 与取消收尾 |
| `video/contracts.py` | 增加可缺省 queryStartedAt，保证正在进行的查询也有持久记账与恢复依据 |

JobService 的登记结果始终区分本地登记与生成完成。原 operationKey 重放复用原结果；更换调用 ID 后，同一规范化请求返回原 jobId，不同请求返回 JOB_ALREADY_EXISTS，错误消息包含已有 ID。原 FileStore Operation 指纹编码未改动。

Mock 结果始终为 `simulated: true`、`mediaAvailable: false`、空 artifactRefs；本阶段不创建 MP4 或可播放的文本产物。Worker 不访问 Mock 私有账本来自动判断 unknown，也没有重复提交或替换上游任务的路径。

## 持久化与恢复证据

新增 `tests/test_storage_migration.py`、`tests/test_video_jobs.py`、`tests/test_video_worker.py`，共 **61 项**；同时保留 B0 和原 M1-A 回归。

| 场景 | 已断言的结果 |
|---|---|
| v1→v2 迁移 | 原字节快照包含原换行与消息；旧项目、多个产物版本、Operation、上下文签名、Token 和调用记录不变；SQLite 文件不改写 |
| 迁移失败 | 快照写入失败、最终替换失败均保留原 state.json，释放本次取得的锁并清理临时文件；已有实例锁不抢占 |
| 损坏和未知版本 | 状态版本、旧消息和 Mock 账本异常不会重建空库；不接受布尔值冒充 schema 版本 |
| 原调用与同 Run 去重 | 相同调用复用 Operation，不同参数冲突；8 个并发调用只登记一个 Job；新的 Run 可独立登记 |
| 来源与规格 | 不存在、跨项目、版本缺失和非法组合均在登记前拒绝；事务前来源变化被重新检查；后续文本更新不改已存请求/版本引用 |
| 正常与快速终态 | 排队→运行→成功、提交时直接成功/生成失败均可复现；成功和匹配的结果在同次本地提交 |
| 错误分类 | 明确拒绝受理为 submit 失败，生成失败保留原 ID，响应丢失/未归类提交异常为 unknown；错误中不保留原始供应商异常正文 |
| 查询重试 | 正常间隔 2 秒，错误后 1/2/4 秒重试；四次连续失败后仅暂停查询，保留最近生成状态与上游 ID；重启不重置窗口 |
| 恢复查询 | 显式 retry_query 清零当前失败窗口，累计 queryAttempts 和 submitAttempts 不变；只查询原上游 ID |
| 关闭和并发 | Worker 停止中断请求并保存状态，不提交剩余 Job；同 Store 的多个 Worker 实例仍串行；其他到期任务不会被某个 Job 的等待/重试阻塞 |
| 落盘失败 | 上游确认后本地提交失败保留 submitting 意图；结果提交失败不出现缺失结果的 succeeded；重启仍按原 ID 或 unknown 恢复 |
| 无模型依赖 | Worker 推进不更改 Run 的模型步数、工具次数、Token 或会话，不要求任何 Key |

七个故障点使用真实子进程 `os._exit(73)`，不依赖正常清理。测试在确认子进程退出后才移除其遗留 instance.lock；服务自身不自动抢锁。

| 强退点 | 重启状态与动作 |
|---|---|
| Job + Operation 登记后 | pending_submit；随后仅提交一次 |
| submitting 意图落盘、尚未调用上游 | unknown；即使 Mock 未受理也不猜测未发送、不重提 |
| Mock 已受理、本地尚未拿到 handle | unknown；独立账本仍只有一次受理 |
| 原上游 ID 已落盘 | queued；后续只 query 该 ID |
| query 意图落盘、上游查询尚未执行 | 计数保留，消费一个查询失败窗口，再按原截止/策略调度 |
| Mock query 已完成、本地尚未提交 | 保留原已确认状态和 ID，再次 query 可取到已推进的结果 |
| succeeded 与结果已落盘 | 直接读取结果，Worker 不再执行 |

查询进行中的 nextPollAt 保存该次超时截止。启动补偿清理 queryStartedAt，并以原截止计算下一次重试，因此重复重启不会刷新等待时间或失败额度。正常的排队/运行/暂停/终态记录不会被启动扫描重置。

## 旧执行兼容

`tests/test_b0_compatibility.py` 继续固定 B0 之前的两个历史 contextSignature。测试先保存原 execution v1 图与工具结果，再恢复为 v1 JSON 根结构，经过真正的 v2 迁移后继续原 Run。两种 contextVersion 均保持原产物、Operation、累计额度和完成结果，重复 resume 不增加模型调用。

当前 Runner、系统规则、原工具签名和 CHECKPOINT_VERSION 均未改写。v2 新增的 waiting_external/等待字段只有存储能力，不能视为 B3 自动恢复已经实现。

## 本地验证

环境：Windows、Python 3.12.14。执行结果：

```text
python -m ruff check src/vagent tests scripts             PASS
python -m ruff format --check src/vagent tests scripts    PASS
node --check web/app.js                                  PASS
python -m pytest -q                                     315 passed, 1 skipped (44.71s)
```

跳过项为 Windows 符号链接权限限制。B1 的故障注入使用可注入时钟；仅适配器超时测试使用 0.01 秒限额，未真实等待 2 秒轮询或 1/2/4 秒重试间隔。

## 打包与独立安装

执行 `python -m build --no-isolation --outdir dist/m1b-b1` 生成 sdist 和 wheel；开发环境及独立安装环境的 `pip check` 均通过。本次产物独立保存，不覆盖 B0 的打包目录。

新建虚拟环境并按锁定依赖安装本次 wheel，在仓库之外验证：

- vagent 从新环境的 site-packages 导入；CLI 的 skills list、demo、inspect 和 usage 正常。
- 旧领域状态迁移至 v2 后登记唯一 Job，关闭 Store、重新打开，再沿用同一上游 ID 完成；submitCalls=1、queryCalls=2，来源版本保持 1，原文本产物和 Run 用量不变。
- 安装后的 Web 静态资源、配置、流式 Runner、质量拒绝和真实本地 MCP 测试通过；应用视频开关仍未启用。
- 临时虚拟环境和工作目录已清理；没有对用户数据目录启动服务，没有发布 PyPI。

安装证据为 `output/m1b-b1-wheel-20261009.json`，过程日志为同名 `.log`，均被 Git 忽略。已验证 wheel：`dist/m1b-b1/shifang37_vagent-0.2.0-py3-none-any.whl`，SHA-256：`791e4052c24a763792e73c7f5a5af9af850132bca075fb0fcdf06f96cff6218e`。这次验证覆盖 B1 库和现有文本应用，B5 的视频工具/等待/页面完整安装验收仍待实施。

## 下一工作包

B2 接入 video_capabilities、video_generate、job_get、await_job 的工具定义、服务端上下文、off/mock 模式、只读边界及回答缓存策略。B3 再接入生产图等待/停止/预算/恢复，B4 管理应用生命周期并交付 CLI/Web；B5 的真实 DeepSeek + Mock 联调与完整应用安装验收仍单独执行。
