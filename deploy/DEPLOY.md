# 从零上线 vagent：每一步是什么、为什么

这份文档假设你**没有部署经验**，目标是让你不仅是“照着命令敲完”，而是知道每条命令在干什么、每个配置文件里的每一行为什么这么写。

读完你应该能回答：镜像和容器什么关系、为什么端口要这样开、`docker compose up -d --build` 到底做了几件事、为什么 Key 不能放环境变量。

---

## 0. 先看懂整体

### 0.1 最终形态

```text
              HTTPS 443
浏览器 ──────────────────> Caddy（容器）────────> vagent app（容器）
                              │  证书/口令/反代        │  127.0.0.1:3210
                              │                       ▼
                              │                  /data（宿主机目录）
                              │                  state.json / checkpoints.sqlite
                              │                  media/ / config.yml
                              │
                         只发布 80/443；app 的 3210 不发布到宿主机
```

### 0.2 一次请求经历了什么

1. 浏览器访问 `https://你的域名`，DNS 把它解析到服务器 IP。
2. Caddy 监听 443，用自动申请的证书完成 TLS 握手。
3. Caddy 先检查 `basic_auth`，没带口令直接返回 **401**，请求根本到不了应用。
4. 通过后，Caddy 把请求转发给 Docker 网络里的 `app:3210`，**保留原始 Host 头**。
5. app 自己的中间件再校验 `Host` 和 `Origin`，不在白名单就返回 **403**，防止别人把域名指过来或做跨站请求。
6. 通过后进入 FastAPI 路由，读写 `/data` 下的状态文件。

### 0.3 为什么需要两层防护

Caddy 只认口令，但它不知道“这个域名该不该被这个应用接受”。app 的 Host 校验是**应用自己**的最后一道门：即使有人绕过 Caddy 直接打到 app（比如以后你把 3210 映射出去），域名不对照样拒绝。两层各管一件事，不是重复。

---

## 1. 买服务器

| 项 | 选择 | 为什么 |
|---|---|---|
| 规格 | 2 核 4G | 应用常驻约 150 MB；CPU 全花在 FFmpeg 渲染，2 核够跑 1 路 1080p |
| 磁盘 | 50 GiB 系统盘 | 一集成片 100–200 MB，扣掉系统镜像约能放 200 集 |
| 带宽 | 200M 峰值 | 720p 播放每路 2–4 Mbps，1–2 个人看绰绰有余 |
| 镜像 | Ubuntu 24.04 LTS | 官方软件源、Docker 文档最全；别选带宝塔的镜像，会和 Docker 抢端口 |
| 地域 | 华北 2（北京） | 万相走百炼北京，同区调用延迟最低 |
| 防火墙 | 放行 22 / 80 / 443 | 22 是 SSH；80 给证书续期和跳转；443 是 HTTPS |

> 轻量控制台里叫「防火墙」，ECS 叫「安全组」，是同一个东西：控制哪些端口能从公网进来。

### 1.1 登录并做三件小事

```bash
ssh root@你的服务器IP
```

解释：`ssh` 用密钥或密码登录到远程机器的 shell，之后所有命令都在服务器上执行。

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

解释：日志时间、备份文件名、数据库时间都取系统时区。不改的话默认是 UTC，你排查问题时会一直差 8 小时。

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

逐条解释：
- `fallocate -l 2G`：快速分配一个 2G 的文件当交换区（比 `dd` 快很多）。
- `chmod 600`：只有 root 能读写。权限过宽时内核会拒绝启用它。
- `mkswap`：把这个文件格式化成交换空间。
- `swapon`：本次开机立即启用。
- 写 `/etc/fstab`：让重启后自动挂载，否则重启就没了。
- `free -h`：确认 `Swap` 那一行不是 0。

为什么 4G 还要 swap：正常用不着，但 1080p 渲染时 ffmpeg 会短时冲高几百 MB，有 swap 当安全垫，宁可慢一点也不要被 OOM 杀掉。

```bash
sudo ss -lntp | grep -E ':80|:443' || echo "80/443 空闲"
```

解释：`ss -lntp` 列出所有监听中的 TCP 端口，`-p` 显示进程。确认没有预装的 nginx/apache 占着 80/443，否则 Caddy 会启动失败。

---

## 2. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
```

解释：`curl` 下载官方安装脚本，`-fsSL` 表示失败不输出、跟随重定向、静默；`| sudo sh` 把脚本内容直接交给 shell 执行。它装的是 Docker Engine + Buildx + Compose 插件。

```bash
sudo systemctl enable --now docker
```

解释：`enable` 设置开机自启，`--now` 立即启动。两个动作一条命令完成。

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

解释：
- `usermod -aG docker` 把当前用户加入 `docker` 组。Docker 的守护进程通过 `/var/run/docker.sock` 通信，这个 socket 属主是 docker 组，不加组就每次都要 `sudo`。
- `newgrp docker` 让当前终端立即获得新组身份；不执行就重新登录一次。

```bash
docker version
docker compose version
```

解释：前者确认客户端和服务端都通；后者确认是 **Compose v2 插件**（输出类似 `v2.x.x`）。老的 `docker-compose` v1 命令语法不同，本文全部使用 `docker compose`。

### 2.1 国内网络的两个加速

**镜像加速**（拉基础镜像慢）：阿里云控制台 → 容器镜像服务 → 镜像管理 → 镜像加速器，按页面给的地址写入：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://你的加速器地址.mirror.aliyuncs.com"] }
EOF
sudo systemctl restart docker
```

解释：`daemon.json` 是 Docker 守护进程的配置；`registry-mirrors` 让拉取先走国内缓存；改完必须重启守护进程才生效。

**单个镜像拉不动时**（实测 Docker Hub 会限速到几十 KB/s）：先从镜像源拉，再打回官方名字，compose 就不用改。

```bash
docker pull docker.1ms.run/library/caddy:2-alpine
docker tag docker.1ms.run/library/caddy:2-alpine caddy:2-alpine
```

解释：`pull` 从镜像源下载；`tag` 给同一个镜像再起一个名字。镜像内容不变，只是本地多了一个叫 `caddy:2-alpine` 的标签，compose 按这个名字就能找到。

---

## 3. 传代码

在**你自己电脑**上（Windows PowerShell）执行，先进入仓库根目录：

```powershell
cd D:\Code\video
```

目标目录结构：

```text
~/vagent/
├── src/ web/ skills/          ← 运行所需
├── pyproject.toml README.md LICENSE
├── .dockerignore
└── deploy/                    ← 部署套件
    ├── Dockerfile
    ├── docker-compose.yml
    ├── Caddyfile
    └── .env.example
```

```powershell
ssh root@你的IP "mkdir -p ~/vagent"

scp -r .\src .\web .\skills .\deploy `
       .\pyproject.toml .\README.md .\LICENSE .\.dockerignore `
       root@你的IP:~/vagent/
```

解释：
- `scp -r` 是走 SSH 的复制，`-r` 递归复制目录。反引号 `` ` `` 在 PowerShell 里是续行，让一条命令分多行写。
- 为什么只传这些：`tests/`、`docs/`、`tmp/`、`output/`、`.venv/`、`.git/` 都不是运行必需的。`.venv` 是 Windows 的虚拟环境，Linux 里反而有害。
- `.dockerignore` 必须一起传：它决定构建时哪些文件不会被打进镜像（下一节会讲）。

---

## 4. 配置 .env

```bash
cd ~/vagent/deploy
cp .env.example .env
chmod 600 .env
nano .env
```

解释：
- `cp` 复制模板。`.env` 里会有域名，以后还会放密码类配置，所以从不提交进仓库。
- `chmod 600` 只允许属主读写，其他用户和组都看不到。
- `nano` 是简单文本编辑器：`Ctrl+O` 保存、回车确认、`Ctrl+X` 退出。

`.env.example` 里现在只有两项是真正要改的：

```dotenv
VAGENT_DOMAIN=vagent.example.com
ACME_EMAIL=
```

**为什么域名要写在这里**：compose 启动时会读取同目录的 `.env`，把 `${VAGENT_DOMAIN}` 替换成实际值。这种替换叫「变量插值」，发生在 compose 解析配置的阶段，不是在容器里运行的阶段。

这一点很重要，区分三件事：

| 位置 | 谁读它 | 用途 |
|---|---|---|
| `deploy/.env` | **docker compose 自己** | 替换 `compose.yaml` 里的 `${...}` |
| `services.*.environment` | 容器里的进程 | 变成容器内的环境变量 |
| 网页设置页 | vagent 应用 | 写进 `data/config.yml` |

也就是说：`.env` 里的变量**不会自动进容器**，必须在 compose 的 `environment:` 里显式列出来才会传进去。我们的 compose 里就是这么做的：

```yaml
environment:
  VAGENT_HOME: /data
  VAGENT_WEB_ALLOWED_HOSTS: ${VAGENT_DOMAIN}
```

### 4.1 为什么 Key 不能写进 .env

vagent 的配置优先级是「内置默认 < `config.yml` < 环境变量」。一旦环境变量里有 `DEEPSEEK_API_KEY`，这个字段的来源就变成「由环境提供」，网页设置页会直接拒绝修改（返回 `CONFIG_OVERRIDE`），而且**所有访问者共用同一个 Key**。

所以 Key 的正确位置是网页设置页 → 落盘到 `data/config.yml`。`.env.example` 里专门写了这条提醒。

---

## 5. 生成口令哈希

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext '你的口令'
```

解释：
- `docker run` 启动一个容器；`--rm` 表示退出后自动删除，不留垃圾。
- `caddy:2-alpine` 是镜像名；后面 `caddy hash-password` 是容器里要执行的命令。
- `--plaintext` 后面是明文口令，工具会输出一段 `$2a$14$...` 开头的 60 字符字符串。

为什么存哈希不存明文：

1. Caddyfile 会出现在 `docker inspect`、备份、截图、误提交里。存哈希，泄露了也推不回口令。
2. 这是 **bcrypt** 哈希，设计上就慢（约 100ms/次），暴力破解成本高；`$2a$14$` 里的 14 是计算强度。
3. Caddy 校验时把用户输入同样做一次 bcrypt，比对结果，全程不需要知道原口令。

把输出整段复制进 `Caddyfile`，替换占位串：

JDJhJDE0JDcyZ0gwTUR0UXBkNlB1TnF5eHV2ZE9RLlpVTlhGZEV5Ni95M05uQUhHVU00NGdqRXlwOWFP

```bash
nano Caddyfile
```

```caddyfile
{$VAGENT_DOMAIN} {
	basic_auth {
		me $2a$14$这里是刚才生成的60个字符
	}
	...
}
```

---

## 6. 构建镜像

```bash
cd ~/vagent/deploy
docker compose build
```

解释：`docker compose build` 只处理 compose 里带 `build:` 的服务（这里是 `app`），其余服务（`caddy`）直接拉现成镜像。

### 6.1 这一步内部发生了什么

1. **确定构建上下文**：`context: ..` 表示把 `~/vagent` 整个目录（排除 `.dockerignore` 里的文件）作为构建输入。
2. **按 Dockerfile 逐层执行**：每一条 `COPY`/`RUN` 生成一个只读层。
3. **缓存**：如果某一层的内容和上次完全一样，直接用缓存。所以第二次构建常常只要几秒。
4. **打包**：最后一层 + 元数据打成本地镜像 `vagent:local`。

### 6.2 首次慢是正常的

首次要下载 Python 依赖（`langgraph`、`fastapi` 等几十个包），国内网络大约 2–4 分钟；如果卡在下载，在 `.env` 里开镜像源：

```dotenv
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PYTHON_IMAGE=docker.1ms.run/library/python:3.12-slim
```

解释：第一条让容器里的 pip 走清华源；第二条让 `FROM` 用镜像源里的基础镜像（内容与官方一致，只是下载地址不同）。

### 6.3 构建完清缓存

```bash
docker builder prune -af
docker system df
```

解释：BuildKit 会把中间层留在本地，一次构建可能占 2–3 GB。`builder prune -af` 删掉所有未使用的构建缓存（`-a` 全部，`-f` 不询问）；`system df` 看镜像、容器、卷、缓存各占多少。

---

## 7. 启动

```bash
sudo mkdir -p data && sudo chown -R 10001:10001 data
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

逐条解释：

- `chown 10001:10001`：镜像里应用以 UID 10001 运行（非 root）。挂载进来的宿主目录如果属主是 root，应用就写不了锁文件，启动会报 `Permission denied`。
- `up` 创建并启动所有服务；`-d` 后台运行；`--build` 启动前先重新构建镜像（等价于先 `build` 再 `up`）。
- `ps` 看状态。**`healthy` 表示健康检查通过**，不是“进程还活着”。
- `logs -f` 实时跟随某个服务的日志；`Ctrl+C` 只退出跟随，不影响容器。

### 7.1 启动顺序是怎么保证的

```yaml
depends_on:
  app:
    condition: service_healthy
```

Caddy 会等 app 的健康检查通过才启动。健康检查本身是容器里每 30 秒跑一次：

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3210/api/health', timeout=3).status == 200 else 1)"]
  start_period: 20s
```

- `test` 返回 0 算健康，非 0 算不健康。
- `start_period: 20s` 给应用 20 秒启动宽限，这期间失败不算数。
- 连续 `retries: 3` 次失败才标记 unhealthy。

没有这层，Caddy 可能在 app 还没就绪时启动，用户会先看到 502。

### 7.2 成功的样子

```text
$ docker compose ps
NAME           IMAGE          STATUS
vagent-app-1   vagent:local   Up (healthy)
vagent-caddy-1 caddy:2-alpine Up

$ docker compose logs app
vagent Web: http://0.0.0.0:3210
允许的对外域名: 你的域名
```

第二行是本次改动加的输出，用来确认“应用到底认哪个域名”。

---

## 8. 验收

```bash
curl -sI https://你的域名 | head -n 1
```

解释：`-I` 只要响应头，`-s` 静默去掉进度条，`head -n 1` 只看第一行。**期望 `HTTP/2 401`**——说明 Caddy 的口令生效，请求没进应用。

```bash
curl -s -u 'me:你的口令' https://你的域名/api/health
```

解释：`-u` 带上 Basic Auth（用户名 `me` 对应 Caddyfile 里那一行）。**期望 `{"status":"ready","version":"0.7.1",...}`**。

```bash
TOKEN=$(curl -s -u 'me:你的口令' https://你的域名/api/session-token \
  | sed 's/.*"token":"\([^"]*\)".*/\1/')

curl -s -o /dev/null -w '%{http_code}\n' -u 'me:你的口令' \
  -H 'Origin: https://evil.example.com' -H "X-CSRF-Token: $TOKEN" \
  -H 'Content-Type: application/json' -d '{}' https://你的域名/api/sessions
# 期望 403
```

解释：先拿到 CSRF token（应用要求所有写请求都带它），再伪造一个跨站 `Origin` 发 POST。**期望 403 `ORIGIN_DENIED`**。这是应用自己的同源校验在拦，不是 Caddy。

至于 Host 白名单：3210 没有发布到宿主机，外部的伪造 Host 根本到不了应用，所以这一层在正式部署里由 Caddy 先挡住（Caddy 只按配置的域名匹配）。只有当你在附录 B 那样把端口直接发布出去时，Host 校验才是唯一防线。

> 这套链路已在 Docker Desktop 上实测：无口令 401、带口令 health 200、首页 200、建会话 201、伪造 Origin 403、SSE 收到事件、容器 healthy。

---

## 9. 在网页里填 Key

打开 `https://你的域名`，输入 `me` 和口令，进「设置」：

1. **DeepSeek Key**：必填。保存本身不产生费用，只有真正对话才计 token。
2. **万相 Key（百炼）**：想生成真实视频才填。供应商选 `wan`，模式切 `live`，保存。页面提示需要重启时执行：

   ```bash
   docker compose restart app
   ```

   解释：`restart` 只重启一个容器，不重建、不丢数据；`config.yml` 在挂载卷里，重启后重新读取。
3. **想零费用体验**：模式保持 `off` 或 `mock`，不填视频 Key。

---

## 10. 日常运维

### 10.1 备份

```bash
sudo tar czf ~/vagent-backup-$(date +%F).tgz -C ~/vagent/deploy data
```

解释：`tar` 打包，`c` 创建、`z` gzip 压缩、`f` 指定输出文件；`$(date +%F)` 展开成 `2026-11-19`；`-C` 先切到 deploy 目录，这样压缩包里是相对路径 `data/...`。

加进 cron 每天跑：

```bash
sudo crontab -e
# 每天 3 点（把 $HOME 换成实际路径，cron 里路径必须写全）
0 3 * * * cd $HOME/vagent/deploy && tar czf $HOME/vagent-$(date +\%F).tgz data
```

注意 cron 里 `%` 要写成 `\%`，否则会被当成换行。

**备份内容是 `data/`，其中 `config.yml` 有明文 Key**，备份文件本身要当机密对待。

### 10.2 恢复

```bash
docker compose down
sudo tar xzf ~/vagent-backup-2026-11-19.tgz -C ~/vagent/deploy
docker compose up -d
```

解释：`x` 解包。先停服务再覆盖，避免应用正在写时被替换。

### 10.3 升级

```bash
cd ~/vagent && git pull          # 或重新 scp
cd deploy
docker compose up -d --build
docker builder prune -af
```

解释：`--build` 会用新代码重建镜像，`up -d` 发现镜像变了就替换容器；`data/` 是挂载卷，不受影响。

### 10.4 磁盘

```bash
df -h /
docker system df
docker compose exec app vagent gc --apply
```

解释：`exec` 在运行中的容器里执行一条命令；`vagent gc --apply` 清理旧终态 Run 的明细，避免 `state.json` 无限膨胀。

---

## 11. 排错

| 现象 | 真正的原因 | 怎么查 |
|---|---|---|
| 403 `HOST_DENIED` | 访问域名 ≠ `VAGENT_WEB_ALLOWED_HOSTS` | `docker compose logs app` 看第二行；改 `.env` 后 `up -d` |
| 502 | app 没起来或没 healthy | `docker compose ps` + `logs app` |
| 401 一直过不去 | 哈希没替换，或粘错 | 重新 `hash-password`，整段 60 字符都要 |
| 证书申请失败 | 80 端口不通 / DNS 没生效 / 未备案 | `docker compose logs caddy`；`dig 你的域名` |
| 聊天很久才一次性出结果 | SSE 被代理缓冲 | 确认 Caddyfile 有 `flush_interval -1`，`restart caddy` |
| 启动报 `Permission denied` | `data/` 属主不是 10001 | `sudo chown -R 10001:10001 data` |
| 磁盘满 | 构建缓存或媒体堆积 | `docker system df`；清缓存、清旧媒体 |

---

## 12. 安全与边界

- **单用户部署**：`data/config.yml` 只有一份 Key。你填，朋友就用你的；朋友填，你就花他的。这是当前代码的单实例设计。
- **口令是唯一门禁**：Caddy 的 `basic_auth` 保护全部页面和 API。口令泄露 = 别人能用你的 Key。
- **没有每日总额上限**：应用有单 Job（默认 3 元）、单 Run、集级三重上限，但没有跨天熔断。别把网址发到公开渠道。
- **单实例**：进程内 worker + 本地磁盘 + SSE，不能多副本，也别上 serverless。

---

## 附录 A：Dockerfile 逐行

```dockerfile
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
```

- `ARG` 是**构建参数**：只在构建时存在，不进最终镜像。用它可以不改进程就换基础镜像。
- `FROM` 决定起点。`slim` 是精简 Debian，比完整版小几百 MB。

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 VAGENT_HOME=/data
```

- 不写 `.pyc` 文件：容器里不需要，浪费层。
- 不缓冲 stdout：日志能实时出现在 `docker compose logs`。
- pip 不留缓存：镜像少几十 MB。
- `VAGENT_HOME=/data`：应用数据目录固定到挂载点，重启不丢。

```dockerfile
RUN if [ ! -f /etc/ssl/certs/ca-certificates.crt ]; then apt-get update ... fi
```

- 只在缺 CA 证书时才装。官方 slim 镜像通常自带，这一步一般 0.3 秒跳过。
- 为什么要在意：国内网络里 `apt-get update` 可能卡几分钟，能跳就跳。

```dockerfile
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY web ./web
COPY skills ./skills
RUN python -m pip install --index-url "$PIP_INDEX_URL" .
```

- **顺序是有讲究的**：先放依赖声明和源码，最后安装。源码改动只会让最后两层失效，前面装依赖的层还能用缓存。
- `pip install .` 用 hatchling 打成 wheel 再安装；`web/` 和 `skills/` 通过 `pyproject.toml` 的 `force-include` 打进包里，所以运行时不需要仓库目录。

```dockerfile
RUN useradd --uid 10001 ... && chown -R vagent:vagent /data /app
USER 10001
```

- **非 root 运行**：容器被攻破时拿不到宿主机 root。
- 固定 UID 10001 是为了让宿主机 `chown` 有确定的数字可写；`USER` 之后的进程都以它身份运行。

```dockerfile
EXPOSE 3210
HEALTHCHECK ... CMD ["vagent", "web", "--no-open", "--host", "0.0.0.0", "--port", "3210"]
```

- `EXPOSE` 只是**声明**端口，方便阅读，不发布到宿主机。
- 必须监听 `0.0.0.0`：监听 `127.0.0.1` 时只有容器自己能连，Caddy 从其他容器过不来（这个坑我们实测踩过）。
- 不发布端口 + Host 白名单 = 即使容器在 0.0.0.0 上监听，也只能经 Caddy 访问。

---

## 附录 B：compose 逐段

```yaml
build:
  context: ..
  dockerfile: deploy/Dockerfile
  args: { PIP_INDEX_URL: ..., PYTHON_IMAGE: ... }
```

- `context: ..` 把仓库根目录作为构建输入（Dockerfile 在 `deploy/` 下，但源码在根目录）。
- `dockerfile` 相对 context 解析。
- `args` 对上 Dockerfile 里的 `ARG`。

```yaml
image: vagent:local
```

构建结果的本地标签。`up` 时如果镜像已存在就直接用，改了代码才重建。

```yaml
volumes:
  - ./data:/data
```

冒号左边是宿主机路径（相对 compose 文件），右边是容器内路径。**这是数据不丢的关键**：容器删了，`./data` 还在。

```yaml
expose: ["3210"]
```

只在 Docker 网络内暴露，宿主机看不到。对比 `ports: ["3210:3210"]` 会把端口发布到宿主机所有网卡。

```yaml
logging:
  driver: json-file
  options: { max-size: "10m", max-file: "3" }
```

每个容器最多 3 个 10MB 的日志文件，超过就滚掉。没有这行，长期运行会把磁盘写满。

```yaml
restart: unless-stopped
```

进程崩了自动重启；你手动 `docker compose stop` 的除外（否则会停不下来）。

```yaml
name: vagent
```

Compose 项目名，决定容器名前缀（`vagent-app-1`）和卷名前缀（`vagent_caddy_data`）。同一台机器跑多个项目时要区分开。

```yaml
depends_on:
  app:
    condition: service_healthy
```

等依赖服务健康再启动。`service_started` 只等它启动，不保证能服务。

---

## 附录 C：命令速查

| 命令 | 作用 |
|---|---|
| `docker compose build` | 只构建镜像，不启动 |
| `docker compose up -d` | 创建并后台启动 |
| `docker compose up -d --build` | 先重建再启动（升级常用） |
| `docker compose ps` | 看容器状态和健康 |
| `docker compose logs -f app` | 实时跟随某个服务日志 |
| `docker compose restart app` | 重启单个容器，不重建 |
| `docker compose down` | 停止并删除容器（保留卷和 data） |
| `docker compose down -v` | 连卷一起删（**会丢数据**） |
| `docker compose exec app sh` | 进容器 shell |
| `docker compose config` | 渲染出插值后的完整配置，排查变量问题 |
| `docker builder prune -af` | 清构建缓存 |
| `docker system df` | 看磁盘占用分布 |
| `docker stats --no-stream` | 看实时内存/CPU |

---

## 附录 D：本机先排练

Windows 上装并启动 Docker Desktop 后：

```powershell
cd D:\Code\video\deploy
Copy-Item .env.example .env
# 把 .env 里的 VAGENT_DOMAIN 改成 localhost
docker compose up -d --build
```

打开 <https://localhost>（Caddy 用内部 CA 自签，浏览器提示不受信任时继续）。这一步能验证镜像构建、反代、口令、SSE，不用买服务器。

注意：本机演练时 Caddyfile 里的口令哈希同样要替换，否则 Caddy 会启动失败。
