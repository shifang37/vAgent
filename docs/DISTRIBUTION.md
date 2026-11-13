# 分发与安装

本文是「别人怎么拿到这个工具」的唯一事实来源。三条通路按落地顺序排列：第一条今天就能用，后两条各需要一次配置。

| 通路 | 用户命令 | 前置条件 | 状态 |
|---|---|---|---|
| 仓库标签 | `pip install "git+https://github.com/shifang37/vAgent@v0.7.1"` | 有 git | **可用** |
| GitHub Release 资产 | `pip install https://github.com/shifang37/vAgent/releases/download/v0.7.1/shifang37_vagent-0.7.1-py3-none-any.whl` | 只需网络 | 打 tag 后自动产出 |
| PyPI + `uvx` | `uvx --from shifang37-vagent vagent` | PyPI 一次性配置 | 待配置 |

## 一、从仓库标签安装（立刻可用）

```bash
python -m pip install "git+https://github.com/shifang37/vAgent@v0.7.1"
VAGENT_HOME=./.vagent vagent demo
```

`pip` 会拉取 `hatchling` 在隔离环境里把源码构建成 wheel；`web/` 与 `skills/` 通过 `tool.hatch.build.targets.wheel.force-include` 打进包里，所以安装后不需要仓库目录。Windows 下把 `./.vagent` 换成 `.\.vagent`。

缺点：需要 git，且每次安装都要现场构建（几十秒）。国内网络可用镜像：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "git+https://github.com/shifang37/vAgent@v0.7.1"
```

## 二、打 tag → GitHub Release（自动）

`.github/workflows/release.yml` 在推送 `v*` tag 时执行：版本一致性校验 → 构建 sdist/wheel → 安装态冒烟（离线，不配置任何 Key）→ 生成 `SHA256SUMS` → 创建 Release 并上传资产。

发版步骤：

1. 改 `pyproject.toml` 的 `version` 与 `src/vagent/__init__.py` 的 `__version__`（两者必须相同，脚本会校验）；
2. 写 `docs/RELEASE_<版本>.md`——它会成为 Release 正文，缺失时退回自动生成的说明；
3. 提交并推送，确认 CI 四组矩阵为绿；
4. 打 tag 并推送：

```bash
git tag v0.7.1
git push origin v0.7.1
```

5. 到 Actions 的 `Release` 运行里取安装命令（会写在 run summary 中），或直接下载资产。

Tag 与包版本不一致时 workflow 直接失败，不会产出「tag 是 v0.7.1、内容是 0.7.0」的资产。Release 已存在时改为覆盖上传（`--clobber`），因此重跑是安全的。

## 三、PyPI（一次性配置，之后才有 `uvx`）

PyPI 用 Trusted Publishing（OIDC），不需要在仓库里存 token。

1. 注册并登录 <https://pypi.org>，开启 2FA；
2. 打开 <https://pypi.org/manage/account/publishing/>，添加 **pending publisher**：

   | 字段 | 值 |
   |---|---|
   | PyPI Project Name | `shifang37-vagent` |
   | Owner | `shifang37` |
   | Repository name | `vAgent` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. GitHub 仓库 → Settings → Environments → 新建 `pypi`（可加 required reviewers，发布前人工确认）；
4. Actions → `Publish to PyPI` → Run workflow，填要发布的 tag；
5. 首次成功后项目即占位，后续版本重复第 4 步。

配置完成后，可以把 `publish.yml` 的触发条件从手动改成随 Release 自动发布：

```yaml
on:
  release:
    types: [published]
```

用户侧的命令（`uv` 会自动下载 Python 3.11+，因此用户不需要预装 Python）：

```bash
uvx --from shifang37-vagent vagent demo
uv tool install shifang37-vagent      # 常驻使用：之后直接 vagent web
```

国内网络：

```bash
uv tool install --index-url https://pypi.tuna.tsinghua.edu.cn/simple shifang37-vagent
```

## 四、验证「别人真的能装上」

在干净环境里跑一遍，任何一步失败都说明分发有问题：

```bash
python -m venv /tmp/vagent-check
/tmp/vagent-check/bin/pip install <上面任一安装命令里的地址>
VAGENT_HOME=/tmp/vagent-data /tmp/vagent-check/bin/vagent demo
VAGENT_HOME=/tmp/vagent-data /tmp/vagent-check/bin/vagent inspect --session demo
```

Windows：

```powershell
python -m venv .\tmp\vagent-check
.\tmp\vagent-check\Scripts\pip.exe install <地址>
$env:VAGENT_HOME = ".\tmp\vagent-data"
.\tmp\vagent-check\Scripts\vagent.exe demo
```

## 五、每次发版的检查清单

- [ ] `pyproject.toml` 的 `version`、`__version__`、tag 三者一致
- [ ] `docs/RELEASE_<版本>.md` 已写（含产物 SHA-256）
- [ ] CI 四组矩阵（ubuntu/windows × 3.11/3.12）为绿
- [ ] Release 运行里的安装态冒烟 PASS
- [ ] 用第四节在干净环境实装一次
- [ ] 把 `SHA256SUMS` 贴进发布说明

## 六、已知边界

- wheel 是 `py3-none-any`，但 `imageio-ffmpeg==0.6.0` 只提供 6 个平台的二进制轮子：`win32`、`win_amd64`、`macosx_x86_64`、`macosx_arm64`、`manylinux_x86_64`、`manylinux_aarch64`。**Alpine/musl 与 Windows ARM64 没有轮子**，会退化为源码构建，需要编译工具链。
- 需要 Python 3.11+。`pip`/`pipx` 用户要自己装；`uv` 会自动准备。
- 包名 `shifang37-vagent` 带账号前缀，PyPI 发布后不能改名。若将来要 `vagent` 这个名字，需要另建项目并同步迁移。
- 包内 FFmpeg 是 `imageio-ffmpeg` 的随包二进制。不要把它单独再分发——那会引入额外的 GPL 义务判断；保持「随 Python 包安装」的现状。
- 前端是原生 JS，随 wheel 分发，没有独立的 npm 产物；本项目不做 npm/npx 分发（理由见讨论记录，核心是 Python 运行时的引导成本）。
