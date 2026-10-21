# 本地拼接的第三方组件与分发材料（E1）

更新日期：2026-10-21。适用范围：M3 E1 的 `ffmpeg-concat-copy-v1`。本文件记录**实际安装到本机**的组件版本、来源与哈希，供安装态验收和再分发时引用；不下载、不转存任何第三方二进制到本仓库。

## 1. 组件

| 组件 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| `imageio-ffmpeg`（Python wrapper） | `0.6.0` | BSD-2-Clause | 只提供定位与调用，不含 FFmpeg 代码 |
| 随包 FFmpeg（`binaries/ffmpeg-<platform>`） | 7.1-essentials（Windows 为 gyan.dev 构建） | `-L` 报告为 GPL v3-or-later | 由 `imageio-ffmpeg` wheel 携带，运行时只调用、不分发副本 |

wrapper 与二进制分属不同许可证，不能把 wrapper 的 BSD-2-Clause 当作 FFmpeg 的许可证，反之亦然。`vagent` 不把 FFmpeg 复制到数据目录、不下载 `ffmpeg-downloader` 运行时、不回退到系统 PATH 或环境变量；`ASSEMBLER_UNAVAILABLE` 表示找不到包内二进制。

## 2. 已核实的 wheel 与二进制（E0 采集，E1 复用）

| 目标 | wheel 大小 | wheel SHA-256 | 包内 FFmpeg SHA-256 |
|---|---|---|---|
| Windows amd64（`imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl`） | 31,246,824 字节 | `02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a` | `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3` |
| Linux x86_64（`imageio_ffmpeg-0.6.0-py3-none-manylinux2014_x86_64.whl`） | 29,498,237 字节 | `c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282` | 未在本机安装 |

本机（Windows amd64、Python 3.12.14）安装后的实测值：

```text
package: imageio-ffmpeg 0.6.0
binary:  .../site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe
version: ffmpeg version 7.1-essentials_build-www.gyan.dev
sha256:  2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3
```

`vagent` 每次拼接都会重新计算二进制 SHA-256 并写入成片版本的 `assembly.engine.binarySha256`，因此换用其他构建（不同平台、其他 FFmpeg 版本）会形成新的成片指纹，不会复用旧版本。

## 3. 来源

- PyPI 项目页：<https://pypi.org/project/imageio-ffmpeg/0.6.0/>
- wrapper 源码与许可证：<https://github.com/imageio/imageio-ffmpeg>
- Windows 构建来源：<https://www.gyan.dev/ffmpeg/builds/>（essentials）
- FFmpeg 源码（GPL v3-or-later，若再分发二进制需随附对应源码获取方式）：<https://ffmpeg.org/download.html>

## 4. 分发与再分发边界

- 只从 PyPI 安装 wheel，不在运行时下载构建；`pyproject.toml` 与 `requirements-dev.lock` 都精确固定 `0.6.0`。
- 本仓库不提交任何第三方二进制；`dist/` 中若包含 wheel，其中的 FFmpeg 二进制即随 wheel 分发，需要同时提供上面第 3 节的许可证与源码获取信息。
- 安装态验收应记录安装平台、包版本、包内二进制路径与 SHA-256，见 `docs/M3_E1_ACCEPTANCE.md`。
