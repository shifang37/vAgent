# v0.7.1 发布说明

**发布日期**：2026-11-13
**版本**：0.7.1
**范围**：v0.7.0（2026-11-10）之后的分发改动；运行时行为不变

本版只解决一件事：**让别人能下载并装上**。v0.7.0 及之前的所有里程碑都把「PyPI 公开发布」写在排除项里，仓库因此没有许可证、没有发布资产，也没有一条不 clone 源码的安装路径。本版补齐这些，并把发布流程固化成流水线。

运行时与 v0.7.0 等价：存储仍为 **schema 9**、工具面仍为 **18/18**、168 个契约模型哈希与 `contractsSha256` / `capabilitiesSha256` 不变；未新增开关或付费样本，未改任何 CLI 参数、Web 接口或数据字段。

## 本版内容

### 1. 许可证与包元数据

- 新增 `LICENSE`（MIT）。此前仓库没有许可证，等于「保留所有权利」，与「别人可以下载使用」的意图直接冲突。
- `pyproject.toml` 增加 `license = "MIT"`、`license-files`、`authors`、`keywords`、`classifiers` 与 `[project.urls]`（Homepage / Repository / Issues / Documentation / Install）。
- 产物侧效果：wheel 的 `METADATA` 产出 `License-Expression: MIT` 与 `License-File: LICENSE`，`dist-info/licenses/LICENSE` 随包分发；sdist 顶层带 `LICENSE`。

### 2. 版本一致性闸门

新增 `scripts/check_release_version.py`：tag、`pyproject.toml` 的 `version`、`src/vagent/__init__.py` 的 `__version__` 三者必须一致，否则发布失败；`docs/RELEASE_<版本>.md` 缺失时给出提示（不阻断）。目的是杜绝「tag 是 v0.7.1、内容是 0.7.0」的资产。

### 3. GitHub Release 流水线

新增 `.github/workflows/release.yml`：推送 `v*` tag（或手动指定 tag）→ 版本校验 → 构建 sdist/wheel → 在安装态跑 `scripts/wheel_smoke.py` → 生成 `SHA256SUMS` → 创建/更新 Release 并上传三个资产。已存在的 Release 改为覆盖上传（`--clobber`），因此重跑是安全的。这条通路不需要任何 PyPI 账号。

### 4. PyPI 发布工作流

新增 `.github/workflows/publish.yml`：Trusted Publishing（OIDC，仓库不存 token），绑定 `environment: pypi`，默认手动触发——避免仓库刚公开时每次 Release 都因为 PyPI 未配置而变红。一次性配置步骤见 [分发说明](./DISTRIBUTION.md)。

### 5. 安装文档与 README

新增 [分发说明](./DISTRIBUTION.md)：三条渠道的命令与前置条件、发版步骤、PyPI 一次性配置、国内镜像、干净环境验证脚本、每次发版的检查清单、平台边界。README 的快速开始改为「不 clone 仓库」，源码构建单独降为开发小节。

## 构建产物

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `shifang37_vagent-0.7.1-py3-none-any.whl` | 1,363,897 字节 | `a625ae6b506c35a9a9ba7297d791d1961bd60e439e699160e597a3aa6a761c72` |
| `shifang37_vagent-0.7.1.tar.gz` | 2,593,899 字节 | `df039150bccaef25871b0ff15ca6640e1ddb66d6add26d7c67fb88bb932928e8` |

哈希为本机构建实测值；同一提交重新构建不保证逐字节一致。wheel 不含 `docs/`，sdist 含 `docs/*.md`。

**下载后请以 GitHub Release 页的 `SHA256SUMS` 为准。** Release 资产由 CI 在 Ubuntu 上重建，与上表不逐字节一致——v0.7.1 的 CI 实测值是 `e54e953efb1a26431aa71f5d20ca8f1db0d91600542d5a7f52ba0e858c97a115`（wheel，1,363,328 字节）与 `64eab1f42c46e56437ccca662168e0f8a9bafb6824333511a8c3d3b6ed04e7e5`（sdist，2,593,750 字节）。从 Release 下载 wheel 后重算 SHA-256，与 `SHA256SUMS` 逐位一致。

## 安装

```powershell
# 1. 从仓库标签（当前可用，需要 git）
python -m pip install "git+https://github.com/shifang37/vAgent@v0.7.1"

# 2. 从 GitHub Release 资产（打 tag 后自动产出）
python -m pip install https://github.com/shifang37/vAgent/releases/download/v0.7.1/shifang37_vagent-0.7.1-py3-none-any.whl

# 3. PyPI 配置完成后的一行式（uv 会自动准备 Python 3.11+）
uvx --from shifang37-vagent vagent web --mcp-local
```

装好后 `vagent --version` 应为 `0.7.1`。数据默认落在 `~/.vagent`，可用 `VAGENT_HOME` 改到别处。

## 兼容与升级

- **无数据迁移**：schema 9 未变，v0.7.0 的 `state.json`、检查点、媒体、账单可直接继续使用。
- **无接口变化**：CLI 子命令、参数、错误码与 Web API 与 v0.7.0 相同。
- `PACKAGE_VERSION` 保持 `0.6.0`：它标识媒体格式代次，与包版本无关。
- 依赖声明未改（仍是 `imageio-ffmpeg==0.6.0`），因此包内 FFmpeg 版本不变。

## 验证记录

本机 Windows（Python 3.12）、完全权限模式下完成，命令与结果如下。

| 项目 | 命令 | 结果 |
|---|---|---|
| 版本闸门 | `python scripts/check_release_version.py v0.7.1` / `v0.7.0` / 无参数 | `0` / `1` / `2`，拒绝逻辑符合预期 |
| Ruff | `ruff check` + `ruff format --check`（`src/vagent`、`scripts`、`tests`） | 259 个文件通过，无需格式化 |
| 构建 | `python -m build --no-isolation --outdir dist/python` | 产出 wheel 与 sdist，见上表 |
| sdist 可构建性 | `pip wheel --no-deps --no-build-isolation dist/python/*.tar.gz` | 从 sdist 重建 wheel 成功，`vagent/web/` 15 个文件、`vagent/builtin_skills/` 2 个、`LICENSE` 均在 |
| 干净环境安装 | 新建 venv，`pip install dist/python/*.whl`（含依赖） | 成功；`vagent.__version__` 与 `importlib.metadata` 均为 `0.7.1`，`vagent.__file__` 指向该 venv 的 `site-packages` |
| 安装后 CLI | `vagent --version` / `demo` / `inspect --session demo` / `skills list` | `0.7.1`；离线 demo 完成 4 步模型、3 次工具并保存产物；两个内置 Skill 可见 |
| 安装态冒烟 | `python scripts/wheel_smoke.py --output output/verify-0.7.1-wheel-smoke.json` | **PASS**：23/23 检查为 true；M1-B 5、M1-C 7、M2 7、M3 6、M5 5、M6 3、M7 3、M8 4 共 40 例全部 `passed`；`temporaryDataCleaned=true` |
| 全量回归 | `pytest -q` | **1,741 passed / 4 skipped**，2,074 秒，无失败（skip 为平台条件：Windows 符号链接权限 3 例、POSIX 文件模式 1 例） |

wheel 的 `METADATA` 另行逐项核对：`Version: 0.7.1`、`License-Expression: MIT`、`License-File: LICENSE`、5 条 `Project-URL`、`Requires-Python: >=3.11`，以及 `vagent/web/` 15 个文件与 2 个内置 Skill。

未验证项：PyPI 与 GitHub Release 两条流水线只做了本地 YAML 解析与权限检查，尚未在真实 runner 上跑过——它们需要先提交、打 tag 才能触发。Windows 之外的平台由 CI 矩阵覆盖。

## 失败与偏差记录

1. **本机构建首次失败，原因是沙箱而非仓库。** 会话最初为受限文件权限模式，`python -m build` 在临时目录上 `PermissionError`；切到完全权限后同一条命令成功，产物与前次修改无关。
2. **pip 直连 PyPI 在本网络下 25 分钟无进展**，改用清华镜像完成安装与冒烟；这是网络路径差异，不是产物问题。文档里已给出镜像命令。
3. **PyPI 尚未真正发布**：本版只交付了可直接发布的产物与流水线，公开发布仍需一次性的人工配置（[分发说明](./DISTRIBUTION.md) 第三节）。

## 已知限制

- Alpine/musl 与 Windows ARM64 没有 `imageio-ffmpeg` 的二进制轮子，会退化为源码构建并需要编译工具链。
- 仍需 Python 3.11+；`pip`/`pipx` 用户要自己装，`uv` 会自动准备。
- 不做 npm/npx 分发：包装层要自己引导 Python 运行时，成本高于收益（结论见讨论记录）。
- 包名 `shifang37-vagent` 带账号前缀，PyPI 发布后不可改名。

## 证据索引

- 安装态冒烟：`output/verify-0.7.1-wheel-smoke.json`
- 全量回归：`output/verify-0.7.1-pytest.log`
- 产物校验和：`dist/python/SHA256SUMS`
- 上一版：[v0.7.0](./RELEASE_0.7.0.md)
