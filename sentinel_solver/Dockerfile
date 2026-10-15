ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE}

# Build-time proxy for pip and the Chromium download (same convention as the app image).
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG http_proxy
ARG https_proxy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
# 只安装完整 Chromium（新版无头模式），headless shell 过不了 Cloudflare
RUN pip install --no-cache-dir -r requirements.txt

# Chromium 的系统依赖放进镜像层：容器重建/重启时不必重新 apt 安装，求解服务几秒内即可就绪
# （此前每次启动都执行 `playwright install --with-deps`，期间所有生图请求都会连接被拒）。
RUN playwright install-deps chromium && rm -rf /var/lib/apt/lists/*

COPY app.py vm.js entrypoint.sh ./

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
