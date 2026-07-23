FROM python:3.12-slim

WORKDIR /app

# 安装 Chromium 运行时依赖（连接外部 CDP 时非必需，但 launch_persistent_context 回退路径需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget gnupg ca-certificates fonts-liberation libnss3 libnspr4 \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir patchright python-multipart httpx \
    && patchright install chromium \
    || pip install --no-cache-dir playwright==1.52.0 playwright-stealth python-multipart httpx

COPY doubaowebapi/ doubaowebapi/

ENV DOUBAO_HOST=127.0.0.1
ENV DOUBAO_PORT=8458
ENV DOUBAO_HEADLESS=true
ENV DOUBAO_BROWSER_DATA=/app/data/.browser_data
ENV DOUBAO_SESSION_FILE=/app/data/.doubao_session.json

EXPOSE 8458

# 健康检查：/health 返回 logged_in 状态。启动需要连 CDP+注入会话，给 90s 宽限。
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8458/health || exit 1

CMD ["python", "-m", "doubaowebapi"]
