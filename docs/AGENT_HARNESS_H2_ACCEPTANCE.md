# 长期记忆检索验收（H2）

日期：2026-11-05。范围：Agent Harness 落差收口计划的 H2 轨道——把「项目事实」升级为可检索的长期记忆，跨 Run（可选跨项目）召回。不改动任何已冻结契约：`state.json` schema 不动，记忆落在旁路文件；默认关闭，关闭时上下文与 `contextSignature` 与今日逐字节相同。

## 决策

- **记忆是派生索引，不是事实源。** 记录只从**结构化项目字段**（goal / audience / style / constraints / plan）投影，从不吸收模型自由文本。这样即便模型在上一次 Run 里「记错」了什么，也不会污染下一次的检索结果——项目事实仍由 `quality.py` 的确定性字段与 `MEMORY_CONFLICT` 兜底。
- **旁路文件，不碰 `state.json`。** 索引落在 `<home>/memory.json`（`MEMORY_VERSION=2`，读取兼容 v1），因此 `state.json` 的冻结 schema、168 个契约模型哈希与 `routeProbe.rowsSha256` 都不动。
- **离线、确定性、零费用的嵌入。** 默认 embedder 是 hashing trick（blake2b 定点散列 + L2 归一化），同一文本在任何平台、任何进程都得到同一向量；无网络、无模型调用、无训练。用例断言的正是「模块内不出现 httpx / requests / socket / random / urlopen」。
- **默认关闭；开启即改变签名。** `VAGENT_MEMORY` 缺省为 off，此时 `AgentRunner.memory is None`，检索块不生成，签名 payload 与今日完全一致。开启后签名 payload 增加 `memoryGuidance` 与 `memory{embedding,limit}`，所以「关→开」或「换 embedder」都会让旧 Run 拒绝恢复——这是显式契约变更，不是静默漂移。
- **检索块只追加在稳定前缀之后。** `MEMORY_HEADER + 召回行` 拼在规则 / Skills / 项目事实 / 预算块之后，稳定前缀逐字节不变；用一张离线用例把「有记忆」的 system 文本按 `MEMORY_HEADER` 切开，前半段必须与「无记忆」构建的结果**全等**。
- **取代按「当前事实集差集」，不是「同 kind 互斥」。** 记录在项目当前事实集里就活跃，不在就标 `supersededBy` 而非删除：检索只返回当前真相，历史仍可审计（与产物「保留旧版本」同一纪律）。多条 `constraints` / `plan` 是并存的同级事实，互不取代；单值字段（goal/audience/style）的旧值指向新值，其余标 `"removed"`。
- **不做用户画像 / 任意文档 RAG。** 与计划边界一致，留作 H6 延伸。

## 工作机制

- `tokens(text)`：NFKC 归一 + casefold；ASCII 词串 + 中文单字与二元组（中文无空格，bigram 承担英文里 word 的词法信号）。
- `HashingEmbedder`：`EMBEDDING_DIM=256`、`EMBEDDING_VERSION="hashing-bigram-v1"`；每个 token 取 blake2b 前 4 字节定位桶、第 5 字节决定 ±1，末了 L2 归一化。空文本返回零向量。
- `MemoryIndex.sync(project)`：把本轮 `project_facts` 的指纹集合 `F` 当作该项目的**当前事实集** → 跳过疑似凭据 → 记录 `id ∈ F` 且已取代的复活、`id ∉ F` 且活跃的标 `supersededBy`（单值 kind 指向新值，其余 `"removed"`）→ 新增 `id ∉ 记录` 的事实 → 只有真的发生新增/取代/复活才落盘一次最终集合。同事实重复 sync 是 no-op；落盘按 `(createdAt, id)` 从旧到新排序（字节稳定），超限时**先淘汰已取代、再本项目最旧、最后全局最旧**（`MAX_RECORDS_PER_PROJECT=512`、`MAX_RECORDS=4096`）。`MemoryIndex` 按 `(st_mtime_ns, st_size)` 缓存解析结果：一步里的 `sync` + `render` 只解析一次。
- `recall(projectId, query, limit, cross_project=False)`：余弦 > 0 才算命中，按 `(-score, kind, id)` 稳定排序；跨项目必须显式 `cross_project=True`。
- `render(...)`：每行 `- (kind) text`，按 UTF-8 字节裁剪到 `budget_bytes`（默认 1024），保证注入块不挤占预算。
- 运行时接线：`runner._run` 在每次构建上下文前 `memory.sync(project)`（幂等）→ `memory.render(project.id, last_user_text(), limit=memory_limit)` → `context.build(memory_block=...)`。召回按**当次用户请求**检索，空命中就不加块。
- 配置：`VAGENT_MEMORY`（1/on/true/yes 开启，默认 off）、`VAGENT_MEMORY_LIMIT`（1–16，默认 `DEFAULT_LIMIT=4`，越界抛 `INVALID_MEMORY_LIMIT`）。`Application.memory_index()` 在关闭时返回 `None`，headless 路径复用同一开关。

## 与计划对照（H2 验收判据）

| 判据 | 落地 | 证据 |
|---|---|---|
| 离线嵌入 stub → 命中判据 | 余弦召回，零向量/无关查询返回空 | `test_recall_is_relevance_ordered_and_budget_clipped`、`test_cosine_ranks_related_text_above_unrelated` |
| 去重 | 内容寻址 id，同事实重复 sync 是 no-op | `test_sync_is_idempotent_and_content_addressed` |
| 取代（当前事实集） | 单值字段变更只取代旧值；删除即取代；回归即复活；同级约束互不取代 | `test_changing_a_fact_supersedes_the_previous_record`、`test_scalar_change_supersedes_only_the_old_value`、`test_adding_a_constraint_keeps_sibling_constraints_active`、`test_removing_a_constraint_supersedes_only_that_record`、`test_a_returning_fact_is_reactivated` |
| 取代立即落盘 | 只有取代、没有新增时也写盘；无变化不写 | `test_a_removal_is_persisted_immediately_and_a_no_op_sync_does_not_write` |
| 淘汰与读放大 | 先淘汰已取代，再本项目最旧、最后全局最旧；一步只解析一次侧车 | `test_eviction_keeps_the_newest_and_prefers_superseded`、`test_eviction_stays_local_while_the_project_cap_is_the_only_pressure`、`test_one_memory_parse_serves_sync_and_recall_across_steps` |
| v1 侧车兼容 | `version=1` 可读（任意真值即失效），下次写入升为 v2 | `test_v1_sidecar_is_readable_and_rewritten_as_v2` |
| 检索注入不破坏稳定前缀 | 前缀按 `MEMORY_HEADER` 切开后与无记忆构建全等；块在 `PROJECT_HEADER` 之后 | `test_memory_on_recalls_the_fact_after_the_frozen_prefix` |
| 默认无网络 | 模块无网络 / 随机依赖；embedder 逐位确定 | `test_memory_module_is_offline_and_free_of_io`、`test_embedding_is_deterministic_and_normalized` |
| 恢复兼容 | 关闭时签名不变；开启后签名变化 → 旧 Run 拒绝恢复 | `test_memory_off_keeps_the_context_and_signature_untouched`、`test_memory_on_recalls_...`（签名断言） |
| 隐私（Key / 密文不入库） | 凭据模式命中即不索引 | `test_secrets_are_never_indexed` |
| 默认可关闭 | `VAGENT_MEMORY` 默认 off，越界参数拒绝 | `test_config_memory_defaults_off_and_parses_the_switch`、`test_config_rejects_an_invalid_memory_limit` |

## 检索质量实测（离线，固定事实集）

五条事实（goal / audience / style / constraint / plan）对两组查询各 5 条：**词法组**与事实共享词，**改写组**只表达同一意图、不共享词。三个指标：top-1 命中、**top-4 命中**（默认 `DEFAULT_LIMIT=4`，即注入块里实际会出现的内容）、top-5 全量召回。

| 查询组 | top-1 | top-4（注入块内） | top-5 召回 |
|---|---|---|---|
| 词法（lexical） | 5/5 | 5/5 | 5/5 |
| 改写（paraphrase） | **2/5** | **3/5** | 5/5 |

改写组逐条（期望 → 实际 top-1，期望所在名次）：受众 → plan（第 3）、风格 → plan（第 5）、目标 → audience（第 5）、字数上限 → constraint（第 1）、下一步 → plan（第 1）。

**如实记录的失败**：离线 hashing 向量本质是词法的，「目标受众是谁」这类短改写查询压不住共享了「做 / 什么」等高频 bigram 的事实，top-1 只有 2/5；按默认注入 4 条算，改写组有 **2/5 的查询压根没把正确事实放进块**（名次 5 的两条被 `limit` 截掉）。top-5 召回 5/5 只说明「排序靠后但仍在前五」，不能说成「块内命中」。要提升 top-1 需要语义嵌入（需模型 / 网络），与「默认零费用、离线」的边界冲突，故不做，只把数字留在这里。

**一条被删掉的复杂度**：中途曾实现「词法重叠加权项」（`OVERLAP_WEIGHT=0.6` + `relevance()`），动机是「短中文查询压不住共享 token 的事实」。实测对照——词法组 5/5→5/5、改写组 2/5→2/5，**两组都没变**。既然没有可测量的收益，就删掉它，代码保留纯余弦：`recall()` 直接 `cosine(vector, stored)`。这条留下的不是代码，而是「先量再留」的判断。

## 隐私与恢复

- **凭据不入库**：`sk-…`、`api_key` 字样、32+ 位十六进制、40+ 位 base64 形态一律跳过，用例把 `sk-secret…` 塞进 `constraints` 后断言落盘 JSON 中不含该串。
- **损坏即降级**：`memory.json` 读失败或 JSON 坏掉时按空索引处理（`records()` 返回空、`sync()` 可重建），记忆是派生索引，丢了不丢真相。
- **恢复语义**：记忆不写 `state.json`，恢复只依赖 `state.json`；签名 payload 里的 `memory{embedding,limit}` 保证「换了 embedder 或改了条数上限」不会被静默复用。副作用是：开启记忆后旧 Run 不能 resume，需要新 Run——与 H1 / H3 的开关同一处理方式。

## 兼容与边界

- `MEMORY_VERSION=2`（读取兼容 v1）、`EMBEDDING_VERSION="hashing-bigram-v1"` 写入旁路文件包头；升级 embedding 版本等于换锚点，旧向量会被 `len(stored) != len(vector)` 或包头指纹挡掉，需重建（派生索引可安全重建）。
- 淘汰会真正删掉记录（包括仍活跃的）；被淘汰的当前事实会在下一次 `sync` 重新投影出来。容量上限是「防止无界增长」的兜底，不参与正确性判断。
- 跨项目检索需显式 `cross_project=True`，运行时默认关闭；不做跨项目偏好记忆（计划明列为未做）。
- 不做用户画像、不做任意文档 RAG、不做摘要（H1）与 tracing（H5）。

## 自动化验证

`tests/test_long_term_memory.py`（23 项）：embedder 3 项、记录投影 / 去重 / 取代 / 复活 / 淘汰 / 隐私 / 旁路容错 13 项、检索 2 项、配置开关 2 项、runner 接线 3 项。

复现（离线，无网络、无模型调用）：

```
python -m pytest tests/test_long_term_memory.py -q
python tmp/measure_h2.py          # top-1 词法 5/5、改写 2/5；块内(top-4) 5/5、3/5；top-5 均 5/5
```

回归：全量 `pytest tests/` **1588 passed, 4 skipped**（跳过项为本机无符号链接权限的既有用例）；`ruff check` 与 `ruff format --check src/vagent tests scripts`（CI 口径）全绿。本轨道未调用任何模型，未联网。
