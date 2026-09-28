#!/bin/bash
# =============================================================================
# tdx-data-feed 容器入口脚本
# =============================================================================
# 职责：
#   1. 等待 ollama / postgres / redis 上线
#   2. 初始化数据库表结构
#   3. 拉取 Ollama 模型（首次启动）
#   4. 按 CMD 执行不同模式
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log() { echo -e "${GREEN}[tdx-entrypoint]${NC} $(date +'%Y-%m-%d %H:%M:%S') $*"; }
warn() { echo -e "${YELLOW}[tdx-entrypoint]${NC} $*"; }
err() { echo -e "${RED}[tdx-entrypoint ERROR]${NC} $*" >&2; }

# ---- 激活 venv ----
source /opt/venv/bin/activate
export PYTHONPATH="${TDX_HOME}:${PYTHONPATH}"

# ---- 等待服务上线 ----
wait_for() {
    local host=$1 port=$2 name=$3 timeout=${4:-120}
    local start=$(date +%s)
    log "等待 ${name} (${host}:${port}) 上线..."
    while ! nc -z "${host}" "${port}" 2>/dev/null; do
        if [ $(($(date +%s) - start)) -gt ${timeout} ]; then
            err "${name} 超时未上线"
            return 1
        fi
        sleep 2
    done
    log "✓ ${name} 已就绪"
}

# ---- 初始化目录 ----
mkdir -p "${TDX_DATA_DIR}/parquet/daily" \
         "${TDX_DATA_DIR}/adj_factor" \
         "${TDX_DATA_DIR}/double_up" \
         "${TDX_DATA_DIR}/predictions" \
         "${TDX_DATA_DIR}/plan_e" \
         "${TDX_DATA_DIR}/train" \
         "${TDX_DATA_DIR}/logs" \
         "${TDX_MODEL_DIR}"

# ---- 等待依赖服务 ----
wait_for "${POSTGRES_HOST}" "${POSTGRES_PORT}" "PostgreSQL"
wait_for "${REDIS_HOST}"    "${REDIS_PORT}"    "Redis"
wait_for "${OLLAMA_HOST}"   "${OLLAMA_PORT}"   "Ollama"

# ---- 数据库初始化 ----
log "初始化 PostgreSQL 表结构..."
python3 -c "
import psycopg2
conn = psycopg2.connect(
    host='${POSTGRES_HOST}',
    port=${POSTGRES_PORT},
    dbname='${POSTGRES_DB}',
    user='${POSTGRES_USER}',
    password='${POSTGRES_PASSWORD}'
)
cur = conn.cursor()
cur.execute('''
CREATE TABLE IF NOT EXISTS predictions (
    id BIGSERIAL PRIMARY KEY,
    run_date DATE NOT NULL,
    code TEXT NOT NULL,
    prob NUMERIC(6,4) NOT NULL,
    close NUMERIC(12,4),
    pct_t1 NUMERIC(8,4),
    ret_5d NUMERIC(8,4),
    vol_ratio_t1 NUMERIC(8,4),
    model_version TEXT DEFAULT 'v7',
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(run_date);
CREATE INDEX IF NOT EXISTS idx_predictions_code ON predictions(code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_predictions_uniq
    ON predictions(run_date, code, model_version);

CREATE TABLE IF NOT EXISTS backtest_results (
    id BIGSERIAL PRIMARY KEY,
    strategy TEXT NOT NULL,
    run_date DATE NOT NULL,
    total_signals INT,
    wins INT,
    losses INT,
    avg_return NUMERIC(8,4),
    win_rate NUMERIC(6,4),
    params JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_backtest_strategy ON backtest_results(strategy);

CREATE TABLE IF NOT EXISTS timer_runs (
    id BIGSERIAL PRIMARY KEY,
    timer_name TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT NOW(),
    finished_at TIMESTAMP,
    status TEXT DEFAULT 'running',
    output TEXT
);
''')
conn.commit()
conn.close()
print('✓ DB schema OK')
"

# ---- Ollama 模型拉取（首次启动）----
if [ "${SKIP_OLLAMA_PULL:-false}" != "true" ]; then
    log "拉取 Ollama 模型 ${TDX_OLLAMA_MODEL:-qwen3-14b-tdx-v7}..."
    if command -v ollama >/dev/null 2>&1; then
        ollama pull "${TDX_OLLAMA_MODEL:-qwen3-14b-tdx-v7}" || \
            warn "ollama pull 失败（可能需要手动 deploy.sh）"
    fi
fi

# ---- 执行模式 ----
MODE="${1:-scheduler}"
log "启动模式: ${MODE}"
case "${MODE}" in
    scheduler)
        log "运行定时任务调度器（cron 风格）..."
        exec python3 -m tdxfeed.scheduler
        ;;
    predict)
        log "执行每日全市场预测..."
        exec python3 scripts/predict.py --top "${TOP_N:-30}" --threshold "${THRESHOLD:-0.7}"
        ;;
    plan-e)
        log "执行方案 E 回测..."
        shift
        exec python3 scripts/plan_e_backtest.py "$@"
        ;;
    train)
        log "启动训练任务..."
        exec python3 -m tdxfeed.trainer
        ;;
    shell)
        log "进入交互式 shell..."
        exec /bin/bash
        ;;
    web)
        log "启动 Web UI..."
        exec python3 -m tdxfeed.web --host 0.0.0.0 --port 8080
        ;;
    *)
        log "自定义命令: $*"
        exec "$@"
        ;;
esac