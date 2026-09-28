-- tdx-data-feed 初始化 schema
-- 由 docker-entrypoint.sh 幂等执行（IF NOT EXISTS）

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

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

CREATE TABLE IF NOT EXISTS timer_runs (
    id BIGSERIAL PRIMARY KEY,
    timer_name TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT NOW(),
    finished_at TIMESTAMP,
    status TEXT DEFAULT 'running',
    output TEXT
);

CREATE TABLE IF NOT EXISTS model_registry (
    id BIGSERIAL PRIMARY KEY,
    model_name TEXT NOT NULL UNIQUE,
    base_model TEXT,
    lora_path TEXT,
    gguf_path TEXT,
    eval_loss NUMERIC(8,4),
    created_at TIMESTAMP DEFAULT NOW()
);

INSERT INTO model_registry (model_name, base_model, eval_loss) VALUES
    ('qwen3-14b-tdx-v5', 'Qwen3-14B-Base', 0.1958),
    ('qwen3-14b-tdx-v6', 'Qwen3-14B-Base', 0.2883),
    ('qwen3-14b-tdx-v7', 'Qwen3-14B-Base', 0.1914)
ON CONFLICT (model_name) DO NOTHING;