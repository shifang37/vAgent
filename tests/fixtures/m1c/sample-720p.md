# C2 本地 MP4 测试夹具

`sample-720p.mp4` 于 2026-10-10 为本项目测试生成。画面来自 FFmpeg `testsrc2` 滤镜，声音来自 `sine` 滤镜；没有下载或使用外部视频、图片、录音，也没有调用生成模型。

| 属性 | 已保存文件 |
|---|---|
| 容器 / 视频 / 音频 | MP4 / H.264（libx264、yuv420p）/ AAC |
| 规格 | 1280×720、16:9、10 fps、5 秒 |
| 字节数 | 209909 |
| SHA-256 | `b179cca0ebb91e2b49a1e212df743037a8e292a3b754ee040c2124b31a3b6a54` |

使用 FFmpeg 7.1 生成，命令如下（在本目录运行）：

```text
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=10:duration=5 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=5 -c:v libx264 -preset veryfast -crf 40 -pix_fmt yuv420p -threads 1 -c:a aac -b:a 32k -movflags +faststart -shortest sample-720p.mp4
```

普通测试直接读取已提交的二进制文件，不需要 FFmpeg。不同 FFmpeg/编码库版本重建的字节可能不同；若有意替换夹具，应同步核对大小、哈希、媒体/Range 测试和浏览器证据。此文件只证明受控媒体链路可用，不作为真实万相输出的证据；C0 的 JSON 迁移夹具与哈希保持独立。
