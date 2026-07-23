## 项目

DouboWebAPI — 豆包逆向 API，OpenAI 兼容接口。Docker 部署，`network_mode: host`。

## 工作流程

每次完成代码修改后，按以下流程执行，不要跳过：

1. **语法检查**: `python3 -m py_compile` 检查所有改动的 `.py` 文件
2. **更新版本号**: `doubaowebapi/__init__.py` 里的 `__version__`，以及 `pyproject.toml`（如适用）
3. **Docker 部署**: `docker compose up -d --build`
4. **等待健康**: `until curl -s http://127.0.0.1:8458/health | grep -q '"ok"'; do sleep 3; done`
5. **功能测试**: 测试修改涉及的所有端点
6. **提交 commit**: 写清楚改动内容

## 关键文件

| 文件 | 用途 |
|------|------|
| `doubaowebapi/__init__.py` | 版本号 `__version__` |
| `doubaowebapi/unified_server.py` | FastAPI 服务端，所有 `/v1/*` 和 `/admin/*` 端点 |
| `doubaowebapi/browser_client.py` | Playwright 浏览器客户端，豆包交互核心 |
| `doubaowebapi/static/admin.html` | Vue3 管理后台 SPA |
| `docker-compose.yaml` | Docker 部署配置 |
| `Dockerfile` | 容器构建 |

## 版本号

版本号在 `doubaowebapi/__init__.py` 第 1 行，`unified_server.py` 通过 `__import__("doubaowebapi").__version__` 读取。两个地方要一致。

## 部署

```bash
cd /opt/dpanel/dpanel/compose/doubaowebapi
docker compose up -d --build
```
