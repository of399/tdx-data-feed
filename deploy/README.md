# tdx-data-feed 容器化部署指南

> **一键启动整套 A 股量化研究平台**：含 GPU 推理（Ollama）+ 元数据库（Postgres）+ 缓存队列（Redis）+ 监控告警（Prometheus + Grafana）。

## 📋 前置条件

| 组件 | 版本要求 | 验证命令 |
|---|---|---|
| Docker | 24.0+ | `docker --version` |
| Docker Compose | v2.20+ | `docker compose version` |
| NVIDIA 驱动 | 580+ | `nvidia-smi` |
| NVIDIA Container Toolkit | 最新 | `docker run --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` |
| 磁盘空间 | ≥ 250 GB | `df -h` |
| 内存 | ≥ 32 GB（含 GPU 加载 14B 模型） | `free -h` |

### 1 行验证脚本

```bash
docker --version && docker compose version && \
    nvidia-smi | head -10 && \
    docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

## 🚀 快速启动（4 步）

### 第 1 步：克隆代码

```bash
git clone https://github.com/jiuben/tdx-data-feed.git
cd tdx-data-feed
```

### 第 2 步：配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少修改三个密码：
#   POSTGRES_PASSWORD
#   REDIS_PASSWORD
#   GRAFANA_PASSWORD
nano .env   # 或 vim .env
```

### 第 3 步：构建镜像

```bash
docker compose build
# 首次约 10-15 分钟（拉基础镜像 + 装 50+ Python 包 + 编译 llama.cpp）
```

### 第 4 步：启动服务

```bash
docker compose up -d
# 1-2 分钟启动 6 个容器
```

## ✅ 验证部署

```bash
# 1. 容器状态
docker compose ps
# 应输出 6 个 service 全 healthy

# 2. 查看主应用日志
docker compose logs -f tdx-app

# 3. 进入应用容器
docker compose exec tdx-app shell

# 4. 验证 Ollama 模型
curl http://localhost:11434/api/tags | jq '.models[].name'

# 5. 验证 PostgreSQL
docker compose exec postgres psql -U tdx -d tdx -c "\dt"

# 6. 验证 Redis
docker compose exec redis redis-cli -a $REDIS_PASSWORD ping
```

### Web 访问

| 服务 | URL | 凭据 |
|---|---|---|
| tdx-app Web UI | http://localhost:8080 | - |
| Ollama API | http://localhost:11434 | - |
| Prometheus | http://localhost:9090 | - |
| Grafana | http://localhost:3000 | admin / 见 .env |

## 🔧 常用操作

```bash
# 启动指定服务
docker compose up -d ollama postgres redis

# 停止（保留数据）
docker compose stop

# 重启单个服务
docker compose restart tdx-app

# 查看实时日志
docker compose logs -f --tail=100 tdx-app

# 进入容器 shell
docker compose exec tdx-app shell

# 在容器内跑预测
docker compose exec tdx-app predict

# 在容器内跑方案 E 回测
docker compose exec tdx-app plan-e --hold 10 --tp 0.30 --sl -0.10

# 在容器内拉新模型
docker compose exec ollama ollama pull qwen3-14b-tdx-v7

# 销毁（保留数据卷）
docker compose down

# 完全清理（含数据卷）
docker compose down -v
```

## 📦 数据持久化

| 卷名 | 内容 | 大小估算 |
|---|---|---|
| `tdx_data` | parquet, adj_factor, double_up, predictions, plan_e | ~3 GB |
| `tdx_models` | GGUF, merged 模型 | ~30 GB |
| `tdx_logs` | 应用日志 | ~1 GB |
| `ollama_data` | Ollama 模型缓存 | ~120 GB |
| `postgres_data` | 元数据 | ~1 GB |
| `redis_data` | 缓存 | ~2 GB |
| `prometheus_data` | 指标 | ~5 GB |
| `grafana_data` | 仪表板 | ~500 MB |

## 🔨 自定义构建

### 修改 llama.cpp 版本

```bash
# .env 中：
LLAMA_CPP_REF=master  # 或指定 tag 如 b3500
docker compose build --no-cache tdx-app
```

### 跳过 Ollama 模型拉取（自部署 Modelfile）

```bash
# .env 中：
SKIP_OLLAMA_PULL=true
# 然后手动跑：
docker compose exec tdx-app bash /opt/tdx-data-feed/v5/scripts/deploy_v7.sh
```

### 使用远程 Ollama（GPU 在别的机器）

```bash
# .env 中：
OLLAMA_URL=http://192.168.1.100:11434
# 然后只跑其他 5 个服务：
docker compose up -d tdx-app postgres redis prometheus grafana
```

## 🚨 故障排查

### 容器无法启动 GPU

```bash
# 1. 验证 NVIDIA Container Toolkit
nvidia-ctk --version

# 2. 测试 GPU passthrough
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi

# 3. 查看启动日志
docker compose logs tdx-app | grep -iE "cuda|gpu|nvidia"
```

### Ollama 模型未自动拉取

```bash
# 手动拉取
docker compose exec ollama ollama pull qwen3-14b-tdx-v7

# 或挂载已有的模型
# 1. 停止：docker compose down ollama
# 2. 复制：cp -r ~/.ollama/models /var/lib/docker/volumes/ollama_data/_data/
# 3. 重启：docker compose up -d ollama
```

### 数据库连接失败

```bash
# 查看 postgres 日志
docker compose logs postgres | tail -20

# 手动连接验证
docker compose exec postgres psql -U tdx -d tdx -c "SELECT 1"
```

### 磁盘空间不足

```bash
# 查看各卷占用
docker system df -v

# 清理未使用镜像/容器/卷
docker system prune -a --volumes
# ⚠ 危险！会清掉所有 tdx_data
```

### 网络问题

```bash
# 重建网络
docker compose down
docker network rm tdx-net
docker compose up -d
```

## 🔄 升级流程

```bash
# 1. 拉新代码
git pull origin main

# 2. 重新构建
docker compose build --pull

# 3. 滚动重启（数据卷不丢）
docker compose up -d

# 4. 清理旧镜像
docker image prune -f
```

## 🏗 架构图

```
┌──────────────────────────────────────────────────────────────┐
│                         Host Machine                          │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │  tdx-app    │  │   ollama    │  │  postgres   │          │
│  │  Python 3.14│←→│  GPU 推理   │  │  元数据     │          │
│  │  :8080      │  │  :11434     │  │  :5432      │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
│         │                │              │                    │
│         └────────────────┼──────────────┘                    │
│                          ↓                                   │
│                  ┌─────────────┐                             │
│                  │    redis    │                             │
│                  │  缓存+队列  │                             │
│                  │   :6379     │                             │
│                  └─────────────┘                             │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐                            │
│  │ prometheus  │  │   grafana   │                            │
│  │  监控采集   │→│  仪表板     │                            │
│  │   :9090     │  │   :3000     │                            │
│  └─────────────┘  └─────────────┘                            │
│                                                              │
│  Volumes: tdx_data / tdx_models / ollama_data / ...         │
└──────────────────────────────────────────────────────────────┘
```

## 📚 进一步阅读

- [README.md](../README.md) - 项目总览
- [docs/WORKFLOW.md](../docs/WORKFLOW.md) - 工作流概览
- [docs/PLAN_E.md](../docs/PLAN_E.md) - 方案 E 策略
- [systemd/](../systemd/) - 非容器化调度（开发机备选）