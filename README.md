# doubaowebapi

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker](https://img.shields.io/badge/docker-supported-blue?logo=docker)](https://github.com/mrz2333/doubaowebapi)

逆向豆包（Doubao）网页版客户端 API，对外暴露 OpenAI 兼容接口，让任何纯文本模型也能识图、读文件、生成图片/视频/音乐。

## 目录

- [这个项目能做什么](#这个项目能做什么)
- [原理](#原理)
- [部署教程](#部署教程)
  - [系统要求](#系统要求)
  - [Docker 部署（推荐）](#docker-部署推荐)
  - [pip 安装](#pip-安装)
  - [QR 扫码登录](#qr-扫码登录)
  - [从 Session 文件登录](#从-session-文件登录)
  - [从 Cookie 环境变量登录](#从-cookie-环境变量登录)
  - [CDP 外部浏览器模式](#cdp-外部浏览器模式)
  - [反向代理配置](#反向代理配置)
  - [第三方客户端集成](#第三方客户端集成)
  - [完整部署验证流程](#完整部署验证流程)
- [环境变量](#环境变量)
- [模型列表](#模型列表)
- [API 端点](#api-端点)
  - [聊天对话](#聊天对话)
  - [图片生成](#图片生成)
  - [视频生成](#视频生成)
  - [音乐生成](#音乐生成)
  - [文件上传](#文件上传)
  - [图片上传](#图片上传)
  - [Session 管理](#session-管理)
  - [Admin 管理面板](#admin-管理面板)
- [使用示例](#使用示例)
  - [OpenAI Python SDK](#openai-python-sdk)
  - [curl](#curl)
- [技术细节](#技术细节)
  - [认证流程](#认证流程)
  - [SSE 流式协议](#sse-流式协议)
  - [思考链提取](#思考链提取)
  - [风控与限流](#风控与限流)
  - [Tool Calling 模拟](#tool-calling-模拟)
- [项目结构](#项目结构)
- [常见问题与排障](#常见问题与排障)
- [注意事项](#注意事项)
- [致谢](#致谢)
- [License](#license)

## 这个项目能做什么

为通用 AI Agent 补全多模态能力。举个例子：你的 Agent 跑的是 DeepSeek V4——一个纯文本模型，看不了图、不能生图、更不会生成音乐。接入 doubaowebapi 之后：

| 能力 | 说明 |
|------|------|
| **多模态对话** | 多轮上下文、深度思考（思维链）、联网搜索 |
| **图片理解** | 上传图片让模型"看懂"，支持截图、照片、文档截图等 |
| **文件理解** | 上传 PDF/Word/Excel/代码等 60+ 种格式，模型直接读取内容 |
| **文生图** | 自然语言描述生成图片，支持多种尺寸和风格 |
| **文生视频** | 自然语言描述生成短视频，支持时长和比例选择 |
| **文生音乐** | 自然语言描述生成音乐，支持风格和歌词自定义 |
| **文件中转** | 上传文件获得永久 TOS URI，可跨机器下载（单文件最大 1GB） |
| **Admin 面板** | Web UI 管理 API Key、查看日志、在线对话测试 |
| **CDP 模式** | 连接 VNC 浏览器复用已登录会话，容器内无需装 Chromium |
| **反检测** | 内置 Patchright（Playwright 反检测 fork），比 stealth 更可靠 |

⚠️ **不适合编程 Agent**：豆包网页版模型不支持原生 Function Calling，本项目的 Tool Calling 通过 XML prompt injection 模拟，稳定性有限，不适合 Claude Code / Codex 等编程智能体。

## 原理

```
你的应用 / AI Agent
        │
        ▼ OpenAI 兼容 API
┌─────────────────────────────┐
│       doubaowebapi          │
│   (FastAPI + Patchright)    │
│                             │
│  ┌───────────────────────┐  │
│  │    BrowserClient      │  │
│  │  ┌─────────┐ ┌─────┐ │  │
│  │  │CDP 模式 │ │独立  │ │  │
│  │  │(外部VNC)│ │模式  │ │  │
│  │  └────┬────┘ └──┬──┘ │  │
│  └───────┼─────────┼────┘  │
└──────────┼─────────┼───────┘
           │         │
           ▼         ▼
      doubao.com  doubao.com
     (复用登录)  (Cookie注入)
```

**核心流程：**

1. 通过 QR 扫码获取 `sessionid` / `ttwid` / `passport_csrf_token` 等认证 Cookie
2. 调用豆包内部 SSE 流式端点完成对话、图片/视频/音乐生成
3. 浏览器内 `bdms.frontierSign()` 自动注入 `a_bogus` / `msToken` 签名，绕过前端风控
4. 将豆包的私有协议响应转换为 OpenAI 兼容格式

**豆包内部端点：**

| 端点 | 协议 | 思考链 | 说明 |
|------|------|--------|------|
| `/samantha/chat/completion` | JSON 明文 | ✅ `block_type=10040` | **推荐主用** |
| `/chat/completion` | JSON 明文 | ✅ | 备用 |
| `/alice/message/stream_call_bot` | base64 编码 | ❌ | 旧端点，已废弃 |

---

## 部署教程

### 系统要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| **操作系统** | Linux / macOS / Windows | Ubuntu 22.04+ / Debian 12+ |
| **Python** | 3.10+ | 3.12+ |
| **Docker** | 20.10+ | 24.0+（含 Docker Compose V2） |
| **内存** | 2 GB | 4 GB+（内置 Chromium 模式需要） |
| **磁盘** | 3 GB | 5 GB+（含 Chromium 依赖） |
| **网络** | 能访问 `doubao.com` | 稳定低延迟连接 |

> 💡 **内存说明**：内置 Chromium 模式下，浏览器进程常驻占用约 500 MB–1 GB 内存。使用 CDP 外部浏览器模式可大幅降低容器内存需求至 ~200 MB。

### Docker 部署（推荐）

最简单的部署方式。**首次构建需要 5–10 分钟**（下载 Chromium 依赖），请耐心等待。

#### 1. 克隆仓库

```bash
git clone https://github.com/mrz2333/doubaowebapi.git
cd doubaowebapi
```

#### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，**必须设置 `DOUBAO_API_KEY`**：

```bash
# === 必须配置 ===
DOUBAO_API_KEY=your-secret-key-here    # API 鉴权密钥，同时是 Admin 面板登录密码

# === 网络配置 ===
DOUBAO_HOST=0.0.0.0    # Docker 部署必须改为 0.0.0.0，否则外部无法访问
DOUBAO_PORT=8458        # 服务端口，默认 8458

# === 可选配置 ===
DOUBAO_RPM_LIMIT=50           # 每分钟请求限制
DOUBAO_TIMEOUT=180            # 单次请求超时（秒）
DOUBAO_KEEPALIVE_INTERVAL=7200  # Session 保活间隔（秒），0 禁用
```

> ⚠️ **重要**：
> - `DOUBAO_HOST` 默认为 `127.0.0.1`（仅本机访问），Docker 部署需改为 `0.0.0.0`
> - `DOUBAO_API_KEY` 留空则 API 端点不鉴权，**强烈建议设置**，否则任何人都能调用你的服务
> - `.env.example` 中部分变量可能使用 `DOLA_` 前缀（历史兼容），实际运行时以 `DOUBAO_` 前缀为准

#### 3. 构建并启动

```bash
docker compose up -d --build
```

> 💡 首次构建耗时较长（5–10 分钟），主要时间花在安装 Chromium 和 Python 依赖上。后续重建由于缓存存在会快很多。

#### 4. 检查服务状态

```bash
# 查看容器状态
docker ps | grep doubaowebapi

# 查看启动日志
docker compose logs -f --tail 50

# 健康检查（首次启动需 30–90 秒完成浏览器连接/Session 注入）
curl http://localhost:8458/health
# 返回 {"status":"ok","logged_in":false} 表示服务已启动但未登录
```

> **服务启动 ≠ 可用**：必须先完成登录（见下方 QR 扫码登录），`logged_in` 变为 `true` 后才能正常调用 API。

#### 5. 常用运维命令

```bash
# 停止服务
docker compose down

# 重启服务
docker compose restart

# 重建并启动（代码更新后）
docker compose up -d --build

# 查看实时日志
docker compose logs -f

# 进入容器调试
docker exec -it doubaowebapi bash
```

#### 网络模式详解

本项目默认使用 `network_mode: host`，服务直接监听宿主机网络，性能开销最小。

| `DOUBAO_HOST` 值 | 效果 | 适用场景 |
|------------------|------|----------|
| `127.0.0.1` | 仅本机可访问 | 本地开发、反向代理后端 |
| `0.0.0.0` | 所有网络接口可访问 | 需要外部直接访问 |

> 💡 host 网络模式下，容器和宿主机共享网络栈，`127.0.0.1:9222` 直接指向宿主机的 Chromium CDP 端口，无需额外映射。

**如需使用桥接网络模式**（不推荐，仅在有特殊需求时使用）：

```yaml
services:
  doubaowebapi:
    build: .
    container_name: doubaowebapi
    restart: always
    ports:
      - "8458:8458"
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
      - DOUBAO_HOST=0.0.0.0
      - DOUBAO_PORT=8458
    volumes:
      - ./data:/app/data
```

> ⚠️ 桥接模式下，`DOUBAO_CDP_URL` 必须使用宿主机 IP（如 `http://172.17.0.1:9222`），不能使用 `127.0.0.1`。

#### 完整 docker-compose.yaml 参考

```yaml
services:
  doubaowebapi:
    build: .
    container_name: doubaowebapi
    restart: always
    network_mode: host
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
      - DOUBAO_HOST=127.0.0.1
      - DOUBAO_PORT=8458
      - PYTHONDONTWRITEBYTECODE=1
    volumes:
      - ./data:/app/data             # Session 文件和浏览器数据持久化
      # - ./doubaowebapi:/app/doubaowebapi  # 开发时挂载源码，生产环境注释掉
```

> 💡 `./doubaowebapi:/app/doubaowebapi` 挂载源码仅用于开发调试，**生产环境请注释掉**，否则每次重启都会从宿主机覆盖容器内代码。

#### 数据持久化

`./data` 目录是关键数据卷，包含：

| 文件/目录 | 说明 |
|-----------|------|
| `data/.doubao_session.json` | 登录 Session/Cookie 持久化文件 |
| `data/.browser_data/` | 内置 Chromium 的用户数据目录（登录态、Cookie） |

> ⚠️ 删除 `data/.doubao_session.json` 会丢失登录态，需要重新扫码。删除 `data/.browser_data/` 会导致内置 Chromium 重置。

### pip 安装

不使用 Docker 时的安装方式，适合开发调试或轻量部署。

#### 前置条件

- Python 3.10+（推荐 3.12）
- 系统需安装 Chromium 运行时依赖（Debian/Ubuntu）：

```bash
sudo apt-get update && sudo apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    libxshmfence1 fonts-liberation
```

#### 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/mrz2333/doubaowebapi.git
cd doubaowebapi

# 2. （推荐）创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 3. 安装浏览器（Patchright，自带反检测补丁的 Playwright fork）
pip install patchright && patchright install chromium

# 4. 安装项目依赖
pip install -e .

# 5. 启动服务
DOUBAO_API_KEY=your-secret-key python -m doubaowebapi
```

> 💡 如果 Patchright 安装失败（网络问题等），会自动回退到 Playwright + playwright-stealth：
> ```bash
> pip install playwright==1.52.0 playwright-stealth
> ```

#### Systemd 服务化（可选）

创建 `/etc/systemd/system/doubaowebapi.service`：

```ini
[Unit]
Description=DouboWebAPI Service
After=network.target

[Service]
Type=simple
User=doubaowebapi
WorkingDirectory=/opt/doubaowebapi
Environment=DOUBAO_API_KEY=your-secret-key
Environment=DOUBAO_HOST=0.0.0.0
Environment=DOUBAO_PORT=8458
ExecStart=/opt/doubaowebapi/.venv/bin/python -m doubaowebapi
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now doubaowebapi
sudo systemctl status doubaowebapi
```

### QR 扫码登录

服务启动后，**必须登录豆包账号才能使用 API**。需要手机上的 [豆包 APP](https://www.doubao.com/download/) 扫码。

#### 方式一：网页扫码（推荐）

在浏览器打开 `http://<服务器IP>:8458/auth`，用豆包 APP 扫描页面上的二维码。

> 💡 `/auth` 页面是从宿主机或本地浏览器打开的，不是在容器内打开。

#### 方式二：API 扫码（无浏览器界面时，如纯服务器环境）

```bash
# 1. 触发 QR 登录，获取二维码 Base64 图片
curl -X POST http://localhost:8458/v1/session/qr-login

# 2. 轮询状态，获取 QR 图片（用手机豆包 APP 扫码）
#    返回的 base64 图片可复制到浏览器地址栏查看：data:image/png;base64,<base64_string>
curl http://localhost:8458/v1/session/qr-login

# 3. 扫码成功后验证
curl http://localhost:8458/health
# 应返回 {"status":"ok","logged_in":true}
```

> 💡 API 扫码返回的 `qr_base64` 字段可直接在浏览器地址栏输入 `data:image/png;base64,<值>` 查看，或使用在线 Base64 转图片工具。

#### 方式三：Admin 面板扫码

浏览器打开 `http://<服务器IP>:8458/admin`，输入 `DOUBAO_API_KEY` 登录后，在面板内点击扫码登录。

#### 登录后自动持久化

扫码成功后 Cookie 自动保存到 `data/.doubao_session.json`，容器重启后自动加载，**无需重复登录**。

建议开启 `DOUBAO_KEEPALIVE_INTERVAL=7200`（每 2 小时自动保活），防止 Session 过期。

### 从 Session 文件登录

如果你已经有 Cookie（比如从浏览器开发者工具提取），手动创建 `data/.doubao_session.json`：

```json
{
  "cookies": {
    "sessionid": "xxx",
    "ttwid": "xxx",
    "passport_csrf_token": "xxx"
  },
  "params": {
    "device_id": "xxx",
    "web_id": "xxx",
    "fp": "xxx",
    "fp_verified": false
  }
}
```

> 💡 **如何从浏览器提取 Cookie**：
> 1. 在已登录的豆包网页版打开开发者工具（F12）
> 2. 切到 Application → Cookies → `https://www.doubao.com`
> 3. 找到 `sessionid`、`ttwid`、`passport_csrf_token` 的值
> 4. 按上述 JSON 格式填入

### 从 Cookie 环境变量登录

也可以通过 `DOUBAO_COOKIE` 环境变量传入 Cookie（适合临时测试或 CI 环境）：

```bash
# Cookie header 格式，分号分隔
DOUBAO_COOKIE="sessionid=xxx; ttwid=xxx; passport_csrf_token=xxx" python -m doubaowebapi
```

> ⚠️ 环境变量方式的优先级高于 Session 文件。如果同时设置了 `DOUBAO_COOKIE` 和 Session 文件，优先使用环境变量。

### CDP 外部浏览器模式

生产环境**推荐使用 CDP 模式**：在宿主机（或 VNC 桌面）运行 Chromium 并登录豆包，doubaowebapi 通过 CDP 协议连接，直接复用已登录的浏览器会话。

**优势：**

| 优势 | 说明 |
|------|------|
| 更小的镜像 | 容器内无需安装 Chromium，镜像体积减少约 500 MB |
| 更快的启动 | 无需等待浏览器下载和安装，启动时间从 90 秒降至 10 秒内 |
| 风控验证 | 遇到验证码时可在 VNC 桌面手动操作 |
| 真实指纹 | 浏览器指纹完全真实，风控概率更低 |
| 登录持久 | 浏览器侧持久化登录状态，容器重建无需重新扫码 |
| 更低内存 | 容器内存占用从 ~1 GB 降至 ~200 MB |

#### 配置步骤

**1. 在宿主机启动 Chromium 并开启 CDP 远程调试：**

```bash
# 无头模式（服务器环境）
chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check --headless=new

# 有头模式（桌面/VNC 环境，可手动操作验证码）
chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check
```

> 💡 如果系统没有安装 Chromium，可以用 Google Chrome 替代：
> ```bash
> google-chrome --remote-debugging-port=9222 --no-first-run --no-default-browser-check
> ```

**2. 在 Chromium 中访问 `https://www.doubao.com/chat/` 并登录豆包**

> 💡 建议勾选「记住登录状态」，这样即使重启浏览器也不需要重新登录。

**3. 在 `.env` 中配置：**

```bash
DOUBAO_CDP_URL=http://127.0.0.1:9222        # 必填：Chromium CDP 地址
DOUBAO_NOVNC_URL=http://127.0.0.1:6080      # 可选：noVNC 地址，风控验证时跳转
```

> 💡 因为本项目使用 `network_mode: host`，`127.0.0.1:9222` 直接指向宿主机的 Chromium。桥接网络模式下需改为宿主机 IP。

**4. 重启 doubaowebapi 使配置生效：**

```bash
docker compose restart
```

> 💡 不设置 `DOUBAO_CDP_URL` 时，doubaowebapi 自动启动内置 Chromium（Patchright）并通过 Session 文件注入 Cookie。两种模式可随时切换。

#### VNC + noVNC 完整方案（推荐生产环境）

在服务器上部署 VNC 桌面 + Chromium + noVNC，实现浏览器可视化管理：

```yaml
# docker-compose.cdp.yaml — CDP 模式完整部署
services:
  chromium-vnc:
    image: linuxserver/chromium:latest
    container_name: chromium-vnc
    restart: always
    network_mode: host
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Asia/Shanghai
      - CHROME_CLI=--remote-debugging-port=9222 --no-first-run
    volumes:
      - ./vnc-config:/config
    # VNC 端口 3001 (Web), 5900 (VNC 客户端)
    # Chromium CDP 端口 9222

  doubaowebapi:
    build: .
    container_name: doubaowebapi
    restart: always
    network_mode: host
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
      - DOUBAO_HOST=0.0.0.0
      - DOUBAO_PORT=8458
      - DOUBAO_CDP_URL=http://127.0.0.1:9222
      - DOUBAO_NOVNC_URL=http://127.0.0.1:3001
    volumes:
      - ./data:/app/data
```

```bash
# 1. 先启动 VNC 浏览器
docker compose -f docker-compose.cdp.yaml up -d chromium-vnc

# 2. 浏览器打开 http://<服务器IP>:3001，在 VNC 桌面中登录豆包

# 3. 再启动 doubaowebapi
docker compose -f docker-compose.cdp.yaml up -d doubaowebapi
```

### 反向代理配置

如果需要通过域名或 HTTPS 访问，可在前面加一层反向代理。

#### Nginx 配置

```nginx
server {
    listen 443 ssl http2;
    server_name doubao-api.example.com;

    ssl_certificate     /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;

    # SSE 流式响应必须关闭缓冲
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;

    location / {
        proxy_pass http://127.0.0.1:8458;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 超时设置
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

> ⚠️ **关键**：SSE 流式响应必须设置 `proxy_buffering off`，否则客户端无法实时接收流式输出，会等到全部完成才一次性返回。

#### Caddy 配置

```
doubao-api.example.com {
    reverse_proxy localhost:8458
}
```

> 💡 Caddy 默认不缓冲响应，对 SSE 友好，配置更简洁。

### 第三方客户端集成

doubaowebapi 暴露 OpenAI 兼容接口，可直接接入支持自定义 API 端点的客户端。

#### LobeChat

在 LobeChat 的「语言模型」设置中添加自定义服务商：

| 配置项 | 值 |
|--------|---|
| API 地址 | `http://<服务器IP>:8458/v1` |
| API Key | 你设置的 `DOUBAO_API_KEY` |
| 模型 | `doubao` / `doubao-think` / `doubao-expert` |

#### NextChat (ChatGPT Next Web)

设置 → 自定义接口：

| 配置项 | 值 |
|--------|---|
| 接口地址 | `http://<服务器IP>:8458` |
| API Key | 你设置的 `DOUBAO_API_KEY` |

#### Open WebUI

Settings → Connections → OpenAI API：

| 配置项 | 值 |
|--------|---|
| Base URL | `http://<服务器IP>:8458/v1` |
| API Key | 你设置的 `DOUBAO_API_KEY` |

#### 通用集成参数

所有支持 OpenAI API 格式的客户端，统一使用以下参数：

```
Base URL:  http://<服务器IP>:8458/v1
API Key:   <你的 DOUBAO_API_KEY>
模型列表:  doubao, doubao-pro, doubao-think, doubao-expert, doubao-image, doubao-video, doubao-music
```

### 完整部署验证流程

以下是端到端部署验证的完整命令序列：

```bash
# ─────────────────────────────────────────
# 步骤 1：构建并启动（首次需 5–10 分钟）
# ─────────────────────────────────────────
docker compose up -d --build

# ─────────────────────────────────────────
# 步骤 2：等待容器 healthy
# ─────────────────────────────────────────
#    首次启动需要 30–90 秒连接浏览器/注入 Session
#    可用以下命令轮询等待：
until docker ps | grep doubaowebapi | grep -q healthy; do sleep 5; done
echo "✅ 容器已 healthy"

# ─────────────────────────────────────────
# 步骤 3：检查服务状态
# ─────────────────────────────────────────
curl http://localhost:8458/health
# 期望：{"status":"ok","logged_in":false}
#       logged_in=false 表示服务已启动但尚未登录

# ─────────────────────────────────────────
# 步骤 4：扫码登录（三选一，详见上方）
# ─────────────────────────────────────────
# 方式 A：浏览器打开 http://<服务器IP>:8458/auth 扫码
# 方式 B：API 扫码
curl -X POST http://localhost:8458/v1/session/qr-login
# 方式 C：Admin 面板 http://<服务器IP>:8458/admin

# ─────────────────────────────────────────
# 步骤 5：验证登录成功
# ─────────────────────────────────────────
curl http://localhost:8458/health
# 期望：{"status":"ok","logged_in":true}

# ─────────────────────────────────────────
# 步骤 6：测试对话（替换 YOUR_KEY 为 DOUBAO_API_KEY）
# ─────────────────────────────────────────
curl -s http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao","messages":[{"role":"user","content":"说一个字：好"}],"stream":false}'
# 期望：返回包含 "好" 的 JSON 响应

# ─────────────────────────────────────────
# 步骤 7：测试流式输出
# ─────────────────────────────────────────
curl -s http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao","messages":[{"role":"user","content":"1+1=?"}],"stream":true}'
# 期望：逐行输出 SSE 事件

# ─────────────────────────────────────────
# 步骤 8：测试图片生成
# ─────────────────────────────────────────
curl -s http://localhost:8458/v1/images/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-image","prompt":"一朵花","size":"512x512"}'
# 期望：返回图片 URL
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DOUBAO_HOST` | `127.0.0.1` | 服务监听地址，Docker 部署建议 `0.0.0.0` |
| `DOUBAO_PORT` | `8458` | 服务端口 |
| `DOUBAO_API_KEY` | *(空)* | API 密钥，保护 `/v1/*` 端点；同时是 Admin 面板登录密码。留空则不鉴权 |
| `DOUBAO_COOKIE` | *(空)* | 直接传入 Cookie header 格式字符串，优先级高于 Session 文件 |
| `DOUBAO_SESSION_FILE` | `.doubao_session.json` | Session Cookie 存储路径 |
| `DOUBAO_RPM_LIMIT` | `50` | 每分钟请求频率限制 |
| `DOUBAO_TIMEOUT` | `180` | 单次请求超时秒数 |
| `DOUBAO_KEEPALIVE_INTERVAL` | `7200` | Session 保活间隔秒数（0=禁用） |
| `DOUBAO_BOT_ID` | `7234781073513644036` | 默认 Bot ID，`7338286299411103781` 为扩展版 |
| `DOUBAO_MS_TOKEN` | *(空)* | msToken，留空最安全，伪造值会触发风控 |
| `DOUBAO_CDP_URL` | *(空)* | 外部 Chromium CDP 地址，如 `http://127.0.0.1:9222` |
| `DOUBAO_BROWSER_DATA` | `/app/data/.doubao_browser` | 浏览器持久化数据目录（独立模式） |
| `DOUBAO_HEADLESS` | `true` | Headless 模式（CDP 模式下忽略） |
| `DOUBAO_NOVNC_URL` | *(空)* | noVNC 远程桌面地址，用于风控验证时跳转 |

> ⚠️ **关于 `DOLA_` 前缀**：`.env.example` 中部分变量可能使用 `DOLA_` 前缀（历史兼容遗留），实际运行时请使用 `DOUBAO_` 前缀。两者在某些版本中可能都有效，但 `DOUBAO_` 是当前标准前缀。

## 模型列表

### 聊天模型

| 模型名 | deep_think | 说明 |
|--------|-----------|------|
| `doubao` | 0 | 快速模式，直接回答 |
| `doubao-pro` | 0 | 快速模式别名 |
| `doubao-think` | 1 | 思考模式，输出 `reasoning_content` 思维链 |
| `doubao-expert` | 3 | 专家模式，深度推理。有每日配额，耗尽后自动降级为 think |

### 多媒体模型

| 模型名 | 类型 | 说明 |
|--------|------|------|
| `doubao-image` | image | 图片生成，支持多种尺寸和风格 |
| `doubao-video` | video | 视频生成，支持时长和比例 |
| `doubao-music` | audio | 音乐生成，支持风格和歌词 |

### 底层路由

| 模式 | 参数 | 底层模型 |
|------|------|----------|
| 快速 | `deep_think=0` | Doubao-Pro（豆包 Pro） |
| 思考 | `deep_think=1` | Doubao-Thinking（带思维链） |
| 专家 | `deep_think=3` | Doubao-Expert（深度推理，配额制） |

## API 端点

### 聊天对话

```
POST /v1/chat/completions
```

OpenAI 兼容的聊天补全接口，支持流式和非流式。

**请求体：**

```json
{
  "model": "doubao-think",
  "messages": [
    {"role": "user", "content": "解释量子纠缠"}
  ],
  "stream": true
}
```

**多模态对话** — 在 messages 中引用图片 URL：

```json
{
  "model": "doubao",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "这张图里有什么？"},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
      ]
    }
  ]
}
```

**带文件对话** — 先通过 `/v1/files` 上传文件获取 key，再在 content 中引用：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "总结这个文件"},
    {"type": "file", "file": {"key": "tos-cn-i-xxx/document.pdf"}}
  ]
}
```

### 图片生成

```
POST /v1/images/generations
```

```json
{
  "model": "doubao-image",
  "prompt": "一只橘猫趴在键盘上睡觉，水彩风格",
  "n": 1,
  "size": "1024x1024"
}
```

支持的尺寸：`512x512`、`768x768`、`1024x1024`、`1024x768`、`768x1024`、`1792x1024`、`1024x1792`

可选参数：
- `style`：图片风格
- `model_params.seedream_model`：指定底层生图模型

### 视频生成

```
POST /v1/video/generations
```

```json
{
  "model": "doubao-video",
  "prompt": "海浪拍打沙滩，夕阳西下",
  "model_params": {
    "video_ratio": "16:9",
    "video_duration": "5"
  }
}
```

参数说明：
- `video_ratio`：视频比例，支持 `1:1`、`16:9`、`9:16`
- `video_duration`：时长秒数，如 `5`、`10`

视频生成是异步的，API 会轮询等待结果返回。

### 音乐生成

```
POST /v1/audio/generations
```

```json
{
  "model": "doubao-music",
  "prompt": "轻快的钢琴曲，适合下午茶"
}
```

可选参数：
- `model_params.lyrics`：自定义歌词
- `model_params.genre`：音乐风格

### 文件上传

```
POST /v1/files
```

上传文件（最大 1GB），返回永久 TOS URI。

```bash
curl -X POST http://localhost:8458/v1/files \
  -H "Authorization: Bearer ***" \
  -F "file=@document.pdf"
```

返回：
```json
{
  "key": "tos-cn-i-xxx/document.pdf",
  "uri": "https://tos-cn-i-xxx.ivolces.com/..."
}
```

### 图片上传

```
POST /v1/images/upload
```

上传图片用于多模态对话（非生成，是让模型"看"这张图）。

```bash
curl -X POST http://localhost:8458/v1/images/upload \
  -H "Authorization: Bearer ***" \
  -F "file=@photo.jpg"
```

### Session 管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/session/qr-login` | POST | 触发 QR 登录 |
| `/v1/session/qr-login` | GET | 获取 QR 状态和图片 |
| `/v1/session/update` | POST | 手动更新 Cookie |
| `/auth/status` | GET | 查看当前登录状态 |

### Admin 管理面板

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin` | GET | 管理面板 SPA |
| `/api/auth/login` | POST | 登录（密码 = `DOUBAO_API_KEY`） |
| `/api/auth/status` | GET | 登录状态 |
| `/api/auth/logout` | POST | 登出 |
| `/api/keys` | GET | 列出所有 API Key |
| `/api/keys` | POST | 创建新 API Key |
| `/api/keys/{id}` | DELETE | 删除 API Key |
| `/api/system` | GET | 系统信息 |
| `/api/logs` | GET | 请求日志 |
| `/api/cookies` | GET | 当前 Cookie 状态 |

### 其他

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查，返回 `{"status":"ok","logged_in":true/false}` |
| `/v1/models` | GET | 模型列表 |

## 使用示例

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8458/v1",
    api_key="your-api-key",
)

# 快速对话
resp = client.chat.completions.create(
    model="doubao",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 思考模式（带思维链）
resp = client.chat.completions.create(
    model="doubao-think",
    messages=[{"role": "user", "content": "解释量子纠缠"}],
)
# reasoning_content 包含思考过程
print(resp.choices[0].message.reasoning_content)
print(resp.choices[0].message.content)

# 流式输出
for chunk in client.chat.completions.create(
    model="doubao",
    messages=[{"role": "user", "content": "写一首关于代码的诗"}],
    stream=True,
):
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)

# 图片生成
resp = client.images.generate(
    model="doubao-image",
    prompt="一只戴着墨镜的猫",
    size="1024x1024",
    n=1,
)
print(resp.data[0].url)
```

### curl

```bash
# 流式聊天
curl -X POST http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-think","messages":[{"role":"user","content":"你好"}],"stream":true}'

# 非流式聊天
curl -X POST http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao","messages":[{"role":"user","content":"1+1=?"}],"stream":false}'

# 图片生成
curl -X POST http://localhost:8458/v1/images/generations \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-image","prompt":"日落下的灯塔","size":"1024x1024"}'

# 视频生成
curl -X POST http://localhost:8458/v1/video/generations \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-video","prompt":"雪山上日出延时"}'

# 音乐生成
curl -X POST http://localhost:8458/v1/audio/generations \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-music","prompt":"轻松的爵士乐"}'

# 上传文件
curl -X POST http://localhost:8458/v1/files \
  -H "Authorization: Bearer ***" \
  -F "file=@report.pdf"

# 带图片的聊天（base64 方式）
curl -X POST http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "这张图里有什么？"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}
      ]
    }]
  }'
```

## 技术细节

### 认证流程

```
1. GET doubao.com → 收集 ttwid + 基础 Cookie
2. GET /passport/safe/csrf_token → passport_csrf_token
3. GET /passport/web/get_qrcode → QR Token + Base64 PNG 图片
4. 轮询 /passport/web/check_qrconnect（每 1.5 秒）
   new → scanned → confirmed（返回 redirect_url）
5. 跟随 redirect_url → 提取 sessionid + 完整 Cookie
6. Cookie 写入 .doubao_session.json 持久化
```

认证完成后，所有 API 请求携带 Cookie 发送。浏览器端的 `bdms.frontierSign()` 会自动为请求附加 `a_bogus` 和 `msToken` 签名。

### SSE 流式协议

豆包的聊天响应通过 Server-Sent Events 流式返回。主要事件类型：

| 事件 | 说明 |
|------|------|
| `sentEvent` | 对话内容块（文本、思维链、搜索结果等） |
| `STREAM_ERROR` | 错误事件（风控、限流等） |
| `STREAM_END` | 流结束标记 |

`sentEvent` 内部的 `block_type` 决定内容类型：

| block_type | 说明 |
|-----------|------|
| 1 | 普通文本 |
| 10000 | 思维链内容（think/reasoning） |
| 10040 | 思考过程元数据 |

### 思考链提取

使用 `doubao-think` 或 `doubao-expert` 模型时，响应中包含 `reasoning_content` 字段：

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "最终回答...",
      "reasoning_content": "思考过程..."
    }
  }]
}
```

流式模式下，思维链通过 `delta.reasoning_content` 增量返回，在 `content` 之前输出。

### 风控与限流

豆包有服务端风控，触发时返回错误码：

| 错误码 | 说明 | 应对 |
|--------|------|------|
| `710022002` | 请求频率过高 | 降低请求频率，等待冷却 |
| `710022004` | 触发验证码 | 需要在浏览器中完成验证 |

**应对策略：**
- 使用 CDP 模式 + Patchright，浏览器指纹更真实
- 设置合理的 `DOUBAO_RPM_LIMIT`
- 不要设置伪造的 `DOUBAO_MS_TOKEN`
- 风控验证码可通过 Admin 面板或 noVNC 手动完成

### Tool Calling 模拟

豆包网页版不支持原生 Function Calling。本项目通过 XML prompt injection 模拟：

1. 将 OpenAI 格式的 `tools` 定义注入系统提示词
2. 模型输出的 XML 格式工具调用被解析并转换回 OpenAI 格式
3. 支持多工具并行调用、参数解析、历史上下文管理

⚠️ 模拟的 Tool Calling 稳定性有限，不推荐用于编程 Agent 等关键场景。

## 项目结构

```
doubaowebapi/
├── doubaowebapi/
│   ├── __init__.py          # 版本号 & 包导出
│   ├── __main__.py          # python -m doubaowebapi 入口
│   ├── unified_server.py    # FastAPI 服务端，所有 API 端点
│   ├── browser_client.py    # Playwright/Patchright 浏览器客户端
│   ├── client.py            # 豆包 Chat API 逆向客户端
│   ├── qr_login.py          # QR 扫码登录（字节 passport 体系）
│   ├── session.py           # Cookie/Session 文件管理
│   ├── tool_calling.py      # Tool Calling 模拟（XML prompt injection）
│   ├── token_counter.py     # Token 计数（tiktoken 近似）
│   ├── sse.py               # SSE 流解析器
│   ├── captcha_handler.py   # 验证码处理器
│   ├── captcha_server.py    # 验证码本地 Web 服务
│   ├── dropdown_debug.py    # 调试脚本：发现所有技能按钮
│   └── static/
│       ├── admin.html       # Vue3 Admin 管理面板 SPA
│       └── img/             # 豆包官方图标
│           ├── apple-touch-icon.png
│           ├── favicon-192.png
│           ├── favicon-64.png
│           └── logo.png
├── tests/
│   ├── test_context_window.py
│   └── test_tool_reliability.py
├── Dockerfile
├── docker-compose.yaml
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
├── CLAUDE.md
├── LICENSE
└── README.md
```

## 常见问题与排障

### 容器启动后 `logged_in` 一直是 `false`

**原因**：尚未完成扫码登录。

**解决**：按照 [QR 扫码登录](#qr-扫码登录) 完成登录。`logged_in=false` 只是表示服务已启动但未认证，不影响服务运行。

---

### 容器启动失败 / 健康检查超时

**可能原因和解决**：

1. **内存不足**：内置 Chromium 模式需要 2 GB+ 内存
   ```bash
   # 检查容器日志
   docker compose logs --tail 100
   # 切换到 CDP 模式降低内存需求
   ```

2. **Chromium 下载失败**：网络问题导致 `patchright install chromium` 失败
   ```bash
   # 重新构建，确保网络畅通
   docker compose build --no-cache
   ```

3. **端口冲突**：8458 端口被占用
   ```bash
   # 检查端口占用
   ss -tlnp | grep 8458
   # 在 .env 中修改 DOUBAO_PORT
   ```

---

### `{"status":"ok","logged_in":true}` 但 API 调用返回 401

**原因**：`Authorization` 请求头缺失或 `DOUBAO_API_KEY` 不匹配。

**解决**：
- 确认请求头格式为 `Authorization: Bearer <你的DOUBAO_API_KEY>`
- 如果 `.env` 中 `DOUBAO_API_KEY` 为空，则不需要鉴权头
- 修改 `.env` 后需重启容器：`docker compose restart`

---

### Session 过期 / API 返回认证错误

**原因**：豆包 Cookie 有有效期，长时间不活动会过期。

**解决**：
1. 开启自动保活：`DOUBAO_KEEPALIVE_INTERVAL=7200`（每 2 小时刷新一次）
2. 重新扫码登录：访问 `/auth` 或 POST `/v1/session/qr-login`
3. 使用 CDP 模式：浏览器侧登录态更持久

---

### 风控验证码 / 请求返回 710022004

**原因**：请求频率过高或被风控系统检测到异常。

**解决**：
1. 降低 `DOUBAO_RPM_LIMIT`（建议 20–30）
2. 切换到 CDP 模式，浏览器指纹更真实
3. 通过 Admin 面板（`/admin`）或 noVNC 手动完成验证码
4. 不要设置伪造的 `DOUBAO_MS_TOKEN`

---

### 反向代理后流式输出不工作

**原因**：Nginx 等代理默认缓冲响应，SSE 事件被缓存。

**解决**：在 Nginx 配置中添加：
```nginx
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 300s;
```

---

### Docker 构建时间过长

**原因**：首次构建需要下载 Chromium（约 300 MB）和 Python 依赖。

**解决**：
1. 使用国内 Docker 镜像加速
2. 使用 CDP 模式（Dockerfile 中可以去掉 Chromium 安装步骤）
3. 后续构建会利用缓存层，速度会快很多

---

### `doubao-expert` 返回质量下降

**原因**：专家模式有每日配额，耗尽后自动降级为 `doubao-think`。

**解决**：
- 查看 Admin 面板中的日志确认是否降级
- 等待配额重置（通常每天重置）
- 非深度推理场景使用 `doubao-think` 替代

---

## 注意事项

1. **仅供学习研究**：本项目逆向了豆包网页版 API，请遵守豆包用户协议，自行承担使用风险
2. **风控风险**：高频请求会触发风控，建议设置合理的 RPM 限制，使用 CDP 模式降低检测概率
3. **Session 过期**：Cookie 会过期，建议启用 `DOUBAO_KEEPALIVE_INTERVAL` 自动保活
4. **专家模式配额**：`doubao-expert` 每日有配额，耗尽后自动降级为 `doubao-think`
5. **msToken**：不要设置伪造的 msToken，空值最安全
6. **图片水印**：部分生图模型输出可能包含水印
7. **Tool Calling 限制**：XML prompt injection 模拟，非原生支持，复杂工具链场景不稳定
8. **文件上传**：单文件最大 1GB，TOS URI 永久有效，下载链接 7 天过期

## 致谢

- [dola2api](https://github.com/mrz2333/dola2api) — 本项目基于 dola2api 架构开发
- [doubao2api](https://github.com/wangchuxiaoji-oss/doubao2api) — 参考了豆包 API 参数和实现方案

## License

[Apache License 2.0](LICENSE)
