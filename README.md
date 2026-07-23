# doubaowebapi

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker](https://img.shields.io/badge/docker-supported-blue?logo=docker)](https://github.com/mrz2333/doubaowebapi#docker-部署)

逆向豆包（Doubao）客户端 API，为 AI 智能体提供免费的多模态能力。通过 OpenAI 兼容接口，让任何纯文本模型也能识图、读文件、生成图片/音乐/视频。

基于 [dola2api](https://github.com/mrz2333/dola2api) 架构，适配豆包（doubao.com）。

## 这个项目能做什么

- **多模态对话**：多轮对话、深度思考（思维链）、联网搜索，完整的 ChatCompletion 能力
- **多模态理解**：识图、读 PDF/Word/Excel/代码等 60+ 种文件格式
- **多媒体生成**：免费生成图片（文生图）、视频、音乐
- **文件中转**：通过 `/v1/files` 上传文件获得永久 TOS URI，可做跨机器文件传输
- **Admin 管理面板**：Web UI 管理 API Key、查看日志、在线测试
- **CDP 外部浏览器**：连接 VNC 浏览器复用已登录会话，无需容器内装 Chromium
- **Patchright 反检测**：内置 Patchright（反检测 Playwright fork），比 playwright-stealth 更可靠

⚠️ **不适合编程智能体**：豆包客户端模型不支持 Function Calling / Tool Use（XML prompt injection 模拟的工具调用仅供实验），不适合作为编程智能体的后端模型。

## 原理

通过 QR 扫码登录获取 `sessionid` 等认证 Cookie，然后调用豆包内部 SSE 流式端点实现对话、图片/视频/音乐生成。

| 端点 | 协议 | 思考链 | 状态 |
|------|------|--------|------|
| `POST /samantha/chat/completion` | JSON 明文 sentEvent | **有** — `block_type=10040` + `10000` | ✅ 推荐主用 |
| `POST /chat/completion` | JSON 明文 | **有** | 备用 |
| `POST /alice/message/stream_call_bot` | base64 编码 payload | **无** | 旧端点，已废弃 |

- 认证: Cookie (`sessionid`, `ttwid`, `passport_csrf_token`)
- 响应: Server-Sent Events 流
- 签名: 浏览器内 `bdms.frontierSign()` 自动注入 `a_bogus` / `msToken`

## 快速开始

### Docker 部署（推荐）

```bash
git clone https://github.com/mrz2333/doubaowebapi.git
cd doubaowebapi
cp .env.example .env
# 编辑 .env 设置 DOUBAO_API_KEY
docker compose up -d --build
```

### QR 扫码登录

启动后访问 `http://<host>:8458/auth`，用豆包 APP 扫码登录。

也可以通过 API 触发：

```bash
curl -X POST http://localhost:8458/v1/session/qr-login
```

### 从 Session 文件创建客户端

如果已有 Cookie，直接创建 `.doubao_session.json`：

```json
{
  "cookies": {
    "sessionid": "your_sessionid",
    "ttwid": "your_ttwid",
    "passport_csrf_token": "your_csrf_token"
  },
  "params": {
    "device_id": "...",
    "web_id": "...",
    "fp": "..."
  }
}
```

### CDP 外部浏览器模式（推荐生产环境）

设置 `DOUBAO_CDP_URL` 环境变量后，doubaowebapi 会连接外部 Chromium 浏览器（如 VNC 浏览器栈），复用已登录的豆包会话。无需在容器内安装 Chromium。

```yaml
# docker-compose.yaml
environment:
  - DOUBAO_CDP_URL=http://127.0.0.1:9222
  - DOUBAO_NOVNC_URL=http://127.0.0.1:6080
```

不设置 `DOUBAO_CDP_URL` 时，doubaowebapi 会自动启动内置 Chromium（通过 Patchright）并注入 Session Cookie。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DOUBAO_HOST` | `127.0.0.1` | 监听地址 |
| `DOUBAO_PORT` | `8458` | 服务端口 |
| `DOUBAO_API_KEY` | *(空)* | API 密钥（保护 `/v1/*` 端点 + Admin 登录密码） |
| `DOUBAO_SESSION_FILE` | `.doubao_session.json` | Session 文件路径 |
| `DOUBAO_RPM_LIMIT` | `50` | 每分钟请求限制 |
| `DOUBAO_TIMEOUT` | `180` | 请求超时（秒） |
| `DOUBAO_KEEPALIVE_INTERVAL` | `7200` | Session 保活间隔（秒） |
| `DOUBAO_BOT_ID` | `7234781073513644036` | 默认 Bot ID |
| `DOUBAO_MS_TOKEN` | *(空)* | msToken（留空最安全，假值触发风控） |
| `DOUBAO_CDP_URL` | *(空)* | 外部 Chromium CDP 地址 |
| `DOUBAO_BROWSER_DATA` | `/app/data/.doubao_browser` | 浏览器持久化目录 |
| `DOUBAO_HEADLESS` | `true` | Headless 模式（CDP 模式忽略） |
| `DOUBAO_NOVNC_URL` | *(空)* | noVNC 地址（风控验证用） |

## API 端点

### GET /health

健康检查，返回登录状态。

```json
{"status": "ok", "logged_in": true}
```

### GET /v1/models

列出所有可用模型。

### POST /v1/chat/completions

OpenAI 兼容的聊天接口，支持流式和非流式。

```bash
curl -X POST http://localhost:8458/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao","messages":[{"role":"user","content":"你好"}]}'
```

### POST /v1/images/generations

文生图接口。

```bash
curl -X POST http://localhost:8458/v1/images/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-image","prompt":"一只可爱的猫咪","n":1,"size":"1024x1024"}'
```

### POST /v1/video/generations

文生视频接口。

```bash
curl -X POST http://localhost:8458/v1/video/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-video","prompt":"海浪拍打沙滩"}'
```

### POST /v1/audio/generations

文生音乐接口。

```bash
curl -X POST http://localhost:8458/v1/audio/generations \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-music","prompt":"轻快的钢琴曲"}'
```

### POST /v1/files

文件上传（最大 1GB），返回永久 TOS URI。

### GET /v1/files/download

通过 TOS URI 获取 7 天有效下载链接。

### POST /v1/images/upload

图片上传，用于多模态对话。

### GET /auth

QR 扫码登录页面。

### GET /auth/status

查看当前登录状态。

### POST /v1/session/qr-login

API 方式触发 QR 登录。

### GET /admin

Admin 管理面板（密码 = `DOUBAO_API_KEY`）。

## 模型列表

| 模型名 | 类型 | 说明 |
|--------|------|------|
| `doubao` | chat | 快速模式（默认） |
| `doubao-pro` | chat | 快速模式别名 |
| `doubao-think` | chat | 思考模式（带思维链 `reasoning_content`） |
| `doubao-expert` | chat | 专家模式（深度推理，有配额限制） |
| `doubao-image` | image | 图片生成 |
| `doubao-video` | video | 视频生成 |
| `doubao-music` | audio | 音乐生成 |

### 三模式对话

| 模式 | 参数 | 特点 |
|------|------|------|
| 快速 | `model: "doubao"` | 直接回答，速度快 |
| 思考 | `model: "doubao-think"` | 带思维链，输出 `reasoning_content` |
| 专家 | `model: "doubao-expert"` | 深度推理，有配额限制，耗尽后自动降级为 think |

## Admin Dashboard

访问 `http://<host>:8458/admin`，输入 `DOUBAO_API_KEY` 登录。

功能：
- 📊 系统概览（模型列表、登录状态、运行时间）
- 🔑 API Key 管理（创建/吊销/过期管理）
- 💬 在线对话测试（支持所有模型）
- 📋 请求日志（带统计图表）
- 📱 移动端适配（底部 Tab Bar + safe-area）

## 使用 OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8458/v1",
    api_key="your-api-key",
)

# 普通对话
response = client.chat.completions.create(
    model="doubao",
    messages=[{"role": "user", "content": "你好"}],
)

# 思考模式
response = client.chat.completions.create(
    model="doubao-think",
    messages=[{"role": "user", "content": "解释量子纠缠"}],
)

# 流式输出
for chunk in client.chat.completions.create(
    model="doubao-think",
    messages=[{"role": "user", "content": "写一首诗"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

## 注意事项

1. **风控**：豆包有请求频率限制（710022002 / 710022004 错误码），触发后需要验证码。CDP 模式 + Patchright 是最佳组合
2. **Session 过期**：Cookie 会过期，需要定期重新扫码登录。通过 `DOUBAO_KEEPALIVE_INTERVAL` 自动保活
3. **专家模式配额**：`doubao-expert` 有每日配额，耗尽后自动降级为 `doubao-think`
4. **msToken**：留空即可，伪造的 msToken 反而会触发风控
5. **图片生成**：部分生图模型可能有水印
6. **Tool Calling**：通过 XML prompt injection 模拟，非原生支持，稳定性有限

## Docker Compose 完整示例

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

## 架构

```
                    doubaowebapi
┌─────────────────────────────────────────────────────┐
│  FastAPI (OpenAI-compatible REST)                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ /v1/*    │  │ /admin   │  │ /auth (QR login) │  │
│  └────┬─────┘  └──────────┘  └──────────────────┘  │
│       │                                              │
│  ┌────▼─────────────────────────────────────────┐   │
│  │  BrowserClient (Playwright/Patchright)       │   │
│  │  ┌────────────────┐  ┌────────────────────┐  │   │
│  │  │ CDP Mode       │  │ Standalone Mode     │  │   │
│  │  │ (external VNC) │  │ (built-in Chromium) │  │   │
│  │  └───────┬────────┘  └─────────┬──────────┘  │   │
│  └──────────┼──────────────────────┼─────────────┘   │
└─────────────┼──────────────────────┼─────────────────┘
              │                      │
              ▼                      ▼
        doubao.com             doubao.com
      (已登录浏览器)        (Cookie 注入)
```

## 项目结构

```
doubaowebapi/
├── doubaowebapi/
│   ├── __init__.py          # 版本号
│   ├── __main__.py          # 入口
│   ├── unified_server.py    # FastAPI 服务端
│   ├── browser_client.py    # Playwright 浏览器客户端
│   ├── client.py            # 豆包 Chat API 客户端
│   ├── qr_login.py          # QR 扫码登录
│   ├── session.py           # Cookie/Session 管理
│   ├── tool_calling.py      # Tool Calling (XML prompt injection)
│   ├── token_counter.py     # Token 计数
│   ├── sse.py               # SSE 流解析
│   ├── captcha_handler.py   # 验证码处理
│   ├── captcha_server.py    # 验证码本地服务
│   ├── dropdown_debug.py    # 调试脚本
│   └── static/
│       ├── admin.html       # Vue3 管理面板 SPA
│       └── img/             # 豆包官方图标
├── tests/
├── docs/
├── Dockerfile
├── docker-compose.yaml
├── pyproject.toml
├── requirements.txt
├── .env.example
└── README.md
```

## 致谢

- [dola2api](https://github.com/mrz2333/dola2api) — 本项目基于 dola2api 架构开发
- [doubao2api](https://github.com/wangchuxiaoji-oss/doubao2api) — 参考了豆包 API 参数和实现方案

## License

[Apache License 2.0](LICENSE)
