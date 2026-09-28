# =============================================================================
# tdx-data-feed 应用镜像
# =============================================================================
# 基础镜像: Ubuntu 22.04 + Python 3.14 + CUDA 12.4 + llama.cpp master
# 构建:    docker build -t tdx-app:latest .
# 运行:    docker run --gpus all -v $PWD/data:/opt/tdx-data-feed/data tdx-app
# =============================================================================

# ---- 阶段 1: 基础工具与依赖 ----
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04 AS base

LABEL maintainer="jiuben <jiuben@tdx.local>"
LABEL description="tdx-data-feed: A股量化研究平台（数据采集/训练/预测/回测）"
LABEL version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- 系统依赖 ----
# python3.14 用 deadsnakes PPA; git/curl/wget/ca-certificates/openssl 为通用基础;
# build-essential 为编译 llama.cpp; libgomp 为 lightgbm 并行; tzdata 设时区.
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common \
        build-essential \
        ca-certificates \
        curl \
        wget \
        git \
        gnupg \
        libgomp1 \
        libssl-dev \
        libffi-dev \
        libbz2-dev \
        libreadline-dev \
        libsqlite3-dev \
        libncursesw5-dev \
        xz-utils \
        tk-dev \
        libxml2-dev \
        libxmlsec1-dev \
        liblzma-dev \
        tzdata \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.14 \
        python3.14-venv \
        python3.14-dev \
        python3-pip \
    && ln -sf /usr/bin/python3.14 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.14 1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ---- Ollama 客户端（用于调用远端 ollama 服务）----
RUN curl -fsSL https://ollama.com/install.sh | sh \
    && cp /usr/local/bin/ollama /tmp/ollama-binary \
    && rm -f /usr/local/bin/ollama \
    || echo "ollama 客户端跳过（将由独立 service 提供）"

# ---- 工作目录 ----
WORKDIR /opt/tdx-data-feed

# ---- Python 依赖（先装依赖再 COPY 代码，利用缓存层）----
COPY requirements.txt requirements-extra.txt* ./
RUN python3.14 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel setuptools \
    && /opt/venv/bin/pip install -r requirements.txt \
    && /opt/venv/bin/pip install -r requirements-extra.txt || true

# ---- llama.cpp（master 版，含 DFLASH 修复）----
ARG LLAMA_CPP_REF=master
RUN git clone --depth=1 --branch ${LLAMA_CPP_REF} https://github.com/ggerganov/llama.cpp.git /tmp/llama.cpp \
    && cp -r /tmp/llama.cpp/gguf-py/gguf /opt/gguf-py-master \
    && /opt/venv/bin/pip install --force-reinstall --no-deps /tmp/llama.cpp/gguf-py \
    && rm -rf /tmp/llama.cpp

# ---- 应用代码 ----
COPY scripts/         ./scripts/
COPY tdxfeed/         ./tdxfeed/
COPY v5/              ./v5/
COPY v6/              ./v6/
COPY v7/              ./v7/
COPY train/           ./train/
COPY systemd/         ./systemd/
COPY Makefile         ./Makefile
COPY *.md             ./
COPY config*.yaml     ./
COPY .github/         ./.github/

# ---- 入口脚本 ----
COPY deploy/scripts/docker-entrypoint.sh /usr/local/bin/tdx-entrypoint
RUN chmod +x /usr/local/bin/tdx-entrypoint

# ---- 健康检查 ----
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

# ---- 端口暴露 ----
# 8080: tdx-app HTTP（预测/报告 web）
# 11434: 仅文档用（实际跑在独立 ollama service 上）
EXPOSE 8080

# ---- 环境变量默认值 ----
ENV TDX_HOME=/opt/tdx-data-feed \
    TDX_DATA_DIR=/opt/tdx-data-feed/data \
    TDX_MODEL_DIR=/opt/tdx-data-feed/models \
    OLLAMA_HOST=ollama \
    OLLAMA_PORT=11434 \
    OLLAMA_URL=http://ollama:11434 \
    REDIS_HOST=redis \
    REDIS_PORT=6379 \
    POSTGRES_HOST=postgres \
    POSTGRES_PORT=5432 \
    POSTGRES_DB=tdx \
    POSTGRES_USER=tdx \
    PYTHONPATH=/opt/tdx-data-feed

# ---- 启动入口 ----
ENTRYPOINT ["/usr/local/bin/tdx-entrypoint"]
CMD ["scheduler"]