# doubaowebapi

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker](https://img.shields.io/badge/docker-supported-blue?logo=docker)](https://github.com/mrz2333/doubaowebapi)

逆向豆包（Doubao）网页版客户端 API，对外暴露 OpenAI 兼容接口，让任何纯文本模型也能识图、读文件、生成图片/视频/音乐。

## 目录

- [这个项目能做什么](#这个项目能做什么)
- [原理](#原理)
- [快速开始](#快速开始)
  - [Docker 部署](#docker-部署)
  - [pip 安装](#pip-安装)
  - [QR 扫码登录](#qr-扫码登录)
  - [从 Session 文件登录](#从-session-文件登录)
  - [CDP 外部浏览器模式](#cdp-外部浏览器模式)
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

## 快速开始

### Docker 部署（推荐）

最简单的部署方式。**首次构建需要 5-10 分钟**（下载 Chromium 依赖），请耐心等待。

```bash
git clone https://github.com/mrz2333/doubaowebapi.git
cd doubaowebapi
cp .env.example .env

# 编辑 .env，必须设置 DOUBAO_API_KEY（API 鉴权密钥 + Admin 面板登录密码）
# 如果需要从外部访问服务，将 DOUBAO_HOST 改为 0.0.0.0
vim .env

docker compose up -d --build
```

> ⚠️ **重要**：本项目默认使用 `network_mode: host`，服务直接监听宿主机网络。
> - `DOUBAO_HOST=127.0.0.1`：只允许本机访问（默认）
> - `DOUBAO_HOST=0.0.0.0`：允许外部访问
> - CDP 模式下，`DOUBAO_CDP_URL` 应指向宿主机上实际的 Chromium CDP 地址（如 `http://127.0.0.1:9222`），因为 host 网络模式下容器和宿主机共享网络

**完整 docker-compose.yaml 示例：**

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
    volumes:
      - ./data:/app/data
```

**启动后验证：**

```bash
# 等待容器变为 healthy（首次启动需 30-90 秒连接浏览器/注入 Session）
docker ps | grep doubaowebapi

# 检查健康状态
curl http://localhost:8458/health
# 返回 {"status":"ok","logged_in":false} 表示服务已启动但未登录
```

> **服务启动 ≠ 可用**：必须先完成登录（见下方 QR 扫码登录），`logged_in` 变为 `true` 后才能正常调用 API。

### pip 安装

不使用 Docker 时的安装方式：

```bash
# 1. 克隆仓库
git clone https://github.com/mrz2333/doubaowebapi.git
cd doubaowebapi

# 2. 安装浏览器（Patchright，自带反检测补丁的 Playwright fork）
pip install patchright && patchright install chromium

# 3. 安装项目依赖
pip install -e .

# 4. 启动服务
DOUBAO_API_KEY=your-secret-key python -m doubaowebapi
```

### QR 扫码登录

服务启动后，**必须登录豆包账号才能使用 API**。需要手机上的 [豆包 APP](https://www.doubao.com/download/) 扫码。

**方式一：网页扫码**（推荐，有浏览器界面时）

在宿主机浏览器打开 `http://localhost:8458/auth`，用豆包 APP 扫描页面上的二维码。

> 💡 `/auth` 页面是从宿主机浏览器打开的，不是在容器内打开。

**方式二：API 扫码**（无浏览器界面时，如纯服务器环境）

```bash
# 1. 触发 QR 登录，获取二维码 Base64 图片
curl -X POST http://localhost:8458/v1/session/qr-login

# 2. 轮询状态，获取 QR 图片（用手机豆包 APP 扫码）
curl http://localhost:8458/v1/session/qr-login

# 3. 扫码成功后验证
curl http://localhost:8458/health
# 应返回 {"status":"ok","logged_in":true}
```

扫码成功后 Cookie 自动保存到 `data/.doubao_session.json`，容器重启后自动加载，无需重复登录。

**方式三：Admin 面板扫码**

浏览器打开 `http://localhost:8458/admin`，输入 `DOUBAO_API_KEY` 登录后，在面板内点击扫码登录。

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

### CDP 外部浏览器模式

生产环境推荐使用 CDP 模式：在宿主机（或 VNC 桌面）运行 Chromium 并登录豆包，doubaowebapi 通过 CDP 协议连接，直接复用已登录的浏览器会话。

**优势：**
- 容器内无需安装 Chromium，镜像更小、启动更快
- 风控验证时可在 VNC 桌面手动操作
- 浏览器指纹完全真实，风控概率更低
- 登录状态在浏览器侧持久化，容器重建无需重新扫码

**配置步骤：**

1. 在宿主机启动 Chromium 并开启 CDP 远程调试：
   ```bash
   chromium --remote-debugging-port=9222 --no-first-run --no-default-browser-check
   ```

2. 在 Chromium 中访问 `https://www.doubao.com/chat/` 并登录豆包

3. 在 `.env` 中配置：
   ```bash
   DOUBAO_CDP_URL=http://127.0.0.1:9222
   DOUBAO_NOVNC_URL=http://127.0.0.1:6080  # 可选，风控验证时跳转
   ```

> 💡 因为本项目使用 `network_mode: host`，`127.0.0.1:9222` 直接指向宿主机的 Chromium。

不设置 `DOUBAO_CDP_URL` 时，doubaowebapi 自动启动内置 Chromium（Patchright）并通过 Session 文件注入 Cookie。

### 完整部署验证流程

```bash
# 1. 构建并启动（首次需 5-10 分钟）
docker compose up -d --build

# 2. 等待容器 healthy
until docker ps | grep doubaowebapi | grep -q healthy; do sleep 5; done

# 3. 检查服务状态
curl http://localhost:8458/health
# 期望：{"status":"ok","logged_in":false}

# 4. 扫码登录（三选一，详见上方）
curl -X POST http://localhost:8458/v1/session/qr-login

# 5. 验证登录成功
curl http://localhost:8458/health
# 期望：{"status":"ok","logged_in":true}

# 6. 测试对话（替换 YOUR_KEY 为你设置的 DOUBAO_API_KEY）
curl -s http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao","messages":[{"role":"user","content":"说一个字：好"}],"stream":false}'
# 期望：返回包含 "好" 的 JSON 响应
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DOUBAO_HOST` | `127.0.0.1` | 服务监听地址，Docker 部署建议 `0.0.0.0` |
| `DOUBAO_PORT` | `8458` | 服务端口 |
| `DOUBAO_API_KEY` | *(空)* | API 密钥，保护 `/v1/*` 端点；同时是 Admin 面板登录密码。留空则不鉴权 |
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
  -H "Authorization: Bearer YOUR_KEY" \
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
  -H "Authorization: Bearer YOUR_KEY" \
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
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-think","messages":[{"role":"user","content":"你好"}],"stream":true}'

# 非流式聊天
curl -X POST http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao","messages":[{"role":"user","content":"1+1=?"}],"stream":false}'

# 图片生成
curl -X POST http://localhost:8458/v1/images/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-image","prompt":"日落下的灯塔","size":"1024x1024"}'

# 视频生成
curl -X POST http://localhost:8458/v1/video/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-video","prompt":"雪山上日出延时"}'

# 音乐生成
curl -X POST http://localhost:8458/v1/audio/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-music","prompt":"轻松的爵士乐"}'

# 上传文件
curl -X POST http://localhost:8458/v1/files \
  -H "Authorization: Bearer YOUR_KEY" \
  -F "file=@report.pdf"

# 带图片的聊天（base64 方式）
curl -X POST http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
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
