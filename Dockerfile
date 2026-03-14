FROM python:3.11-slim

# 系统依赖（curl_cffi 需要 libcurl）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libcurl4-openssl-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷贝依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY codex/ ./codex/

# 数据目录（挂载为 volume）
RUN mkdir -p /data/codex_tokens

ENV DATA_DIR=/data
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:5000/api/health || exit 1

WORKDIR /app/codex
CMD ["python", "app.py"]
