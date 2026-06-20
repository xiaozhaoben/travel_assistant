ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ARG INSTALL_NODE=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

RUN if [ "$INSTALL_NODE" = "true" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY constraints.txt .
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY app ./app
RUN mkdir -p logs runtime

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8000\")}/api/health', timeout=5).read()" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
