# syntax=docker/dockerfile:1
# 单进程镜像：App 自己带 FFmpeg（imageio-ffmpeg）和字幕字体，系统层只补 CA。
# 国内拉不动 Docker Hub 时，把 PYTHON_IMAGE 指到镜像源（见 deploy/.env.example）。
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VAGENT_HOME=/data

# 官方 slim 镜像通常已带 CA 证书；缺了才装，避免在国内网络里卡在 apt。
RUN if [ ! -f /etc/ssl/certs/ca-certificates.crt ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends ca-certificates \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY web ./web
COPY skills ./skills
# 国内构建慢时把 PIP_INDEX_URL 换成镜像源（见 deploy/.env.example）。
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --index-url "$PIP_INDEX_URL" .

# 非 root 运行；/data 由 compose 以同一个 UID 挂载，见 DEPLOY.md。
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin vagent \
    && mkdir -p /data \
    && chown -R vagent:vagent /data /app
USER 10001

EXPOSE 3210
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3210/api/health', timeout=3).status == 200 else 1)"

# 容器内监听 0.0.0.0，否则 Caddy 从 compose 网络连不进容器的回环地址；
# 端口不发布到宿主机，对外唯一的入口仍是 Caddy（Host / Origin 边界照旧生效）。
CMD ["vagent", "web", "--no-open", "--host", "0.0.0.0", "--port", "3210"]
