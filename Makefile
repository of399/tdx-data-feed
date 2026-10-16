# Makefile · tdx-data-feed
# 统一开发入口 · 整合所有操作
# 用法:
#   make help           # 看所有目标
#   make install        # 装依赖
#   make data-sync      # 同步日 K
#   make predict        # 跑方案 D 预测
#   make plan-e         # 跑方案 E 回测
#   make deploy-v5      # 部署 v5
#   make deploy-all     # 部署所有模型
#   make status         # 看系统状态
#   make canary         # 健康检查
#   make timer-enable   # 启用 systemd 自动化
#
# 分类:
#   开发      install / lint / fix / format / test
#   数据      data-universe / data-sync / data-adj / data-bj
#   分析      analyze-double-up / analyze-signal-d / analyze-top10
#   训练      train-v5 / train-v6 / train-v7
#   部署      deploy-v5 / deploy-v6 / deploy-v7
#   应用      predict / plan-e / journal
#   自动化    timer-install / timer-enable / timer-disable / timer-status
#   监控      canary / status / logs
#   维护      clean / reset-data / backup / restore

.PHONY: help

# ============== 变量 ==============
VENV := /home/jiuben/tdx-data-feed/venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(PYTHON) -m ruff
OLLAMA := ollama
ROOT := $(shell pwd)
SCRIPTS := $(ROOT)/scripts
V5_SCRIPTS := $(ROOT)/v5/scripts
SYSTEMD_DIR := $(ROOT)/systemd
USER_SYSTEMD := $(HOME)/.config/systemd/user
LOGS := $(ROOT)/logs
DATA := $(ROOT)/data
MODELS := /home/jiuben/models

# Ollama 模型
MODEL_V5 := qwen3-14b-tdx-v5
MODEL_V6 := qwen3-14b-tdx-v6
MODEL_V7 := qwen3-14b-tdx-v7
MODEL_BASE := qwen3-14b-tdx-fast

# 颜色
GREEN := \033[0;32m
YELLOW := \033[0;33m
CYAN := \033[0;36m
RED := \033[0;31m
BOLD := \033[1m
NC := \033[0m

.DEFAULT_GOAL := help

# ============== 帮助 ==============
help:  ## 显示所有目标（默认）
	@echo "$(CYAN)$(BOLD)╔══════════════════════════════════════════════╗$(NC)"
	@echo "$(CYAN)$(BOLD)║      tdx-data-feed Makefile (完整版)         ║$(NC)"
	@echo "$(CYAN)$(BOLD)╚══════════════════════════════════════════════╝$(NC)"
	@echo ""
	@echo "$(YELLOW)$(BOLD)📦 安装与环境:$(NC)"
	@grep -E "^(install|venv|setup|update):.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🔍 代码质量:$(NC)"
	@grep -E "^(lint|fix|fix-unsafe|format|typecheck):.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🧪 测试:$(NC)"
	@grep -E "^(test|test-cov|test-quick|sop-quick|sop-full|audit):.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)📊 数据采集:$(NC)"
	@grep -E "^data-.*:.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)📈 量化分析:$(NC)"
	@grep -E "^analyze-.*:.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🎓 模型训练:$(NC)"
	@grep -E "^train-.*:.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🚀 模型部署:$(NC)"
	@grep -E "^deploy-.*:.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)💼 应用层:$(NC)"
	@grep -E "^(predict|plan-e|journal|simulate|status):.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)⏰ 自动化调度:$(NC)"
	@grep -E "^timer-.*:.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🏥 监控:$(NC)"
	@grep -E "^(canary|logs|health):.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🧹 维护:$(NC)"
	@grep -E "^(clean|backup|restore|reset-data):.*?## .*$$" $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)💡 示例:$(NC)"
	@echo "  make install && make data-sync && make predict"
	@echo "  make deploy-all && make timer-enable"
	@echo "  make canary && make status"

# ============== 安装与环境 ==============
install:  ## 装 Python 依赖 + 初始化
	@echo "$(CYAN)▶ 创建虚拟环境$(NC)"
	test -d $(VENV) || python3 -m venv $(VENV)
	@echo "$(CYAN)▶ 升级 pip$(NC)"
	$(PIP) install --upgrade pip
	@echo "$(CYAN)▶ 安装 requirements$(NC)"
	$(PIP) install -r requirements.txt
	@echo "$(CYAN)▶ 安装额外依赖（lightgbm + 训练）$(NC)"
	$(PIP) install lightgbm llamafactory peft
	@echo "$(CYAN)▶ 创建日志目录$(NC)"
	mkdir -p $(LOGS)
	@echo "$(GREEN)✓ 安装完成$(NC)"
	@echo ""
	@echo "$(YELLOW)下一步:$(NC)"
	@echo "  make data-universe  # 下载股票清单"
	@echo "  make data-sync      # 同步日 K"

venv:  ## 仅创建虚拟环境
	python3 -m venv $(VENV)
	@echo "$(GREEN)✓ 虚拟环境创建完成: $(VENV)$(NC)"

setup: install data-universe data-sync  ## 完整初始化（环境 + 数据）

update:  ## 更新依赖到最新版
	$(PIP) install --upgrade pip
	$(PIP) install --upgrade -r requirements.txt
	$(PIP) install --upgrade lightgbm llamafactory peft gguf
	@echo "$(GREEN)✓ 依赖已更新$(NC)"

# ============== 代码质量 ==============
lint:  ## ruff check（不修改）
	$(RUFF) check v5/ --statistics
	@echo "$(GREEN)✓ lint 完成$(NC)"

lint-all:  ## ruff check 全部代码（含 scripts）
	$(RUFF) check . --statistics --ignore=E501 || true

lint-pipeline:  ## 检查 shell 脚本中未被 pipefail 保护的管道（防 `cmd|tail` 静默吞错）
	@echo "$(CYAN)▶ 扫描未被 pipefail 保护的 | tail/head/grep/awk/sed 管道$(NC)"
	@missing=0; for f in $$(find scripts v5/scripts systemd -name '*.sh' 2>/dev/null); do \
	  if ! grep -qE 'pipefail' "$$f"; then \
	    if grep -qE '\|\s*(tail|head|grep|awk|sed)\b' "$$f"; then \
	      echo "  ⚠ 未受 pipefail 保护: $$f"; missing=1; fi; fi; \
	done; \
	if [ "$$missing" -eq 0 ]; then echo "$(GREEN)✓ 所有含管道的脚本均已启用 pipefail$(NC)"; else echo "$(YELLOW)⚠ 存在未保护脚本，请补齐 set -uo pipefail$(NC)"; fi

fix:  ## ruff 自动 fix（safe）
	$(RUFF) check v5/ --fix
	@echo "$(GREEN)✓ safe auto-fix 完成$(NC)"

fix-unsafe:  ## ruff 自动 fix（含 unsafe，需 review）
	@echo "$(YELLOW)⚠ unsafe fix 会修改语义，请 review diff$(NC)"
	$(RUFF) check v5/ --fix --unsafe-fixes
	@echo "$(YELLOW)⚠ 请检查: git diff$(NC)"

format:  ## ruff format（与 black 兼容）
	$(RUFF) format v5/
	@echo "$(GREEN)✓ format 完成$(NC)"

format-all:  ## format 全部
	$(RUFF) format .

typecheck:  ## mypy 类型检查
	$(PYTHON) -m mypy v5/ --ignore-missing-imports || echo "mypy not installed"

# ============== 测试 ==============
test:  ## 跑所有 pytest
	cd $(ROOT) && $(PYTEST) v5/tests/ -v

test-cov:  ## pytest + coverage 报告
	cd $(ROOT) && $(PYTEST) v5/tests/ --cov=v5 --cov-report=term-missing --cov-report=html
	@echo "$(GREEN)✓ 覆盖率报告: htmlcov/index.html$(NC)"

test-quick:  ## 快速冒烟测试
	cd $(ROOT) && $(PYTEST) v5/tests/ -x --timeout=10

sop-quick:  ## 告警链路 quick 验证（5s，非侵入）
	bash v5/tests/test_alerting_pipeline.sh --report-json /tmp/sop-quick-report.json

sop-full:  ## 告警链路 full 验证（140s，侵入）
	bash v5/tests/test_alerting_pipeline.sh --mode full --auto-sink --report-json /tmp/sop-full-report.json

audit: sop-quick  ## 跑 quick + 显示最近报告
	@echo ""
	@echo "$(CYAN)=== v5/audit/ 最近 5 份 ===$(NC)"
	@ls -lt v5/audit/*.md 2>/dev/null | head -5 | awk '{print "  ", $$NF}'

# ============== 数据采集 ==============
data-universe:  ## 下载全 A 股代码清单
	$(PYTHON) $(SCRIPTS)/download_universe.py
	@echo "$(GREEN)✓ 股票清单: $(DATA)/all_a_stocks.json$(NC)"

data-sync:  ## 同步全市场日 K（tdxfeed）
	$(PYTHON) -m tdxfeed.cli sync
	@echo "$(GREEN)✓ 日 K 同步完成$(NC)"

data-sync-symbol:  ## 同步单股日 K（参数 SYM=sh600000）
	$(PYTHON) -m tdxfeed.cli sync $(SYM)

data-adj:  ## 下载沪深复权因子
	$(PYTHON) $(SCRIPTS)/download_adj_factor_b.py
	@echo "$(GREEN)✓ 复权因子下载完成$(NC)"

data-bj:  ## 下载北交所日 K + 复权因子
	$(PYTHON) $(SCRIPTS)/download_bj_daily.py
	$(PYTHON) $(SCRIPTS)/download_adj_factor_bj.py
	@echo "$(GREEN)✓ 北交所数据下载完成$(NC)"

data-watch:  ## 启动 watchdog 自动重启挂死进程
	$(PYTHON) $(SCRIPTS)/download_adj_factor_watch.py &

data-status:  ## 查看数据同步状态
	$(PYTHON) -m tdxfeed.cli status
	@echo "$(CYAN)股票清单: $(shell jq 'length' $(DATA)/all_a_stocks.json 2>/dev/null || echo 'N/A') 只$(NC)"
	@echo "$(CYAN)日 K: $(shell ls $(DATA)/parquet/daily/ 2>/dev/null | wc -l) 文件$(NC)"
	@echo "$(CYAN)复权因子: $(shell ls $(DATA)/adj_factor/ 2>/dev/null | wc -l) 文件$(NC)"

# ============== 量化分析 ==============
analyze-double-up:  ## 方案 C：识别翻倍样本
	$(PYTHON) $(SCRIPTS)/find_double_up_c.py
	@echo "$(GREEN)✓ 翻倍样本: $(DATA)/double_up/double_up_2020_2026_detail.csv$(NC)"

analyze-double-up-v2:  ## 方案 C v2：前复权对比
	$(PYTHON) $(SCRIPTS)/find_double_up_c_v2.py
	@echo "$(GREEN)✓ v2 报告: $(DATA)/double_up/double_up_v2_report_*.md$(NC)"

analyze-signal-d:  ## 方案 D：训练 LightGBM 信号模型
	$(PYTHON) $(SCRIPTS)/find_d_signal_5d.py
	@echo "$(GREEN)✓ LightGBM 模型: $(DATA)/double_up/d_signal_5d_model.txt (AUC 0.8144)$(NC)"

analyze-top10:  ## Top 10 妖股深度复盘
	$(PYTHON) $(SCRIPTS)/top10_deep_dive.py
	@echo "$(GREEN)✓ 报告: $(DATA)/double_up/top10_deep_dive_*.md$(NC)"

analyze-dashboard:  ## 生成综合 HTML 仪表盘
	$(PYTHON) $(SCRIPTS)/gen_dashboard.py
	@echo "$(GREEN)✓ Dashboard: $(DATA)/double_up/training_dashboard.html$(NC)"

analyze-all: analyze-double-up analyze-signal-d analyze-top10  ## 跑所有分析

# ============== 模型训练 ==============
train-v5:  ## 训练 v5（12656 样本，rank=16）
	@echo "$(CYAN)▶ 训练 v5（GPU 占用）$(NC)"
	$(PYTHON) training/scripts/train_lora.py training/config/tdx_v5_lora.yaml
	@echo "$(GREEN)✓ v5 训练完成$(NC)"

train-v6:  ## 训练 v6（5238 样本，rank=16）
	@echo "$(CYAN)▶ 训练 v6（GPU 占用）$(NC)"
	$(PYTHON) training/scripts/train_lora.py training/config/tdx_v6_lora.yaml
	@echo "$(GREEN)✓ v6 训练完成$(NC)"

train-v7:  ## 训练 v7（v5 续训，8000 样本）
	@echo "$(CYAN)▶ 训练 v7（GPU 占用）$(NC)"
	$(PYTHON) training/scripts/train_lora.py training/config/tdx_v7_resume.yaml
	@echo "$(GREEN)✓ v7 训练完成（eval_loss 最低 0.1914）$(NC)"

train-all: train-v5 train-v6 train-v7  ## 顺序训练所有模型

train-plot:  ## 画 loss 曲线
	$(PYTHON) $(SCRIPTS)/plot_loss.py --all

train-monitor:  ## 启动 GPU 温度监控
	bash $(SCRIPTS)/monitor_gpu_temp.sh &

# ============== 模型部署 ==============
deploy-v5:  ## 部署 v5 到 Ollama（4 步：merge → GGUF → Modelfile → create）
	bash $(V5_SCRIPTS)/deploy_v5.sh
	@echo "$(GREEN)✓ v5 已部署: $(MODEL_V5):latest$(NC)"

deploy-v6:  ## 部署 v6 到 Ollama
	bash $(V5_SCRIPTS)/deploy_v6.sh
	@echo "$(GREEN)✓ v6 已部署: $(MODEL_V6):latest$(NC)"

deploy-v7:  ## 部署 v7 到 Ollama
	@echo "$(CYAN)▶ 部署 v7（需先训练）$(NC)"
	bash $(V5_SCRIPTS)/deploy_v7.sh
	@echo "$(GREEN)✓ v7 已部署: $(MODEL_V7):latest$(NC)"

deploy-all: deploy-v5 deploy-v6 deploy-v7  ## 部署所有模型

deploy-list:  ## 列出已部署模型
	$(OLLAMA) list
	@echo ""
	@echo "$(CYAN)模型详情:$(NC)"
	@echo "  $(MODEL_V5): eval_loss=0.1958, rank=8,  29GB"
	@echo "  $(MODEL_V6): eval_loss=0.2883, rank=16, 29GB"
	@echo "  $(MODEL_V7): eval_loss=0.1914, rank=8,  29GB (最优)"
	@echo "  $(MODEL_BASE): eval_loss=0.2300, baseline"

deploy-test:  ## 测试所有模型推理（茅台）
	@for m in $(MODEL_V5) $(MODEL_V6) $(MODEL_V7); do \
		echo "$(CYAN)▶ 测试 $$m$(NC)"; \
		curl -s http://localhost:11434/api/generate -d "{ \
			\"model\": \"$$m\", \"prompt\": \"分析茅台 600519\", \
			\"stream\": false, \"options\": {\"stop\": [], \"repeat_penalty\": 1.0} \
		}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(' ',d.get('response','')[:200])"; \
		echo; \
	done

deploy-rm:  ## 删除指定模型（MODEL=name）
	$(OLLAMA) rm $(MODEL)
	@echo "$(GREEN)✓ 已删除 $(MODEL)$(NC)"

# ============== 应用层 ==============
predict:  ## 跑方案 D 全市场预测（默认 Top 20）
	$(PYTHON) $(SCRIPTS)/predict.py --top 30 --threshold 0.7
	@echo "$(GREEN)✓ 预测报告: $(DATA)/predictions/predict_latest.md$(NC)"

predict-top:  ## 跑方案 D Top 50 + 阈值 0.6
	$(PYTHON) $(SCRIPTS)/predict.py --top 50 --threshold 0.6

predict-demo:  ## 跑 5 只 demo 预测
	$(PYTHON) $(SCRIPTS)/predict.py --codes 600519,000001,300750,688981,920047 --top 3

predict-codes:  ## 跑指定股票（参数 CODES=600519,000001）
	$(PYTHON) $(SCRIPTS)/predict.py --codes $(CODES) --top 5

plan-e:  ## 跑方案 E 完整回测（v2 增强）
	$(PYTHON) $(SCRIPTS)/plan_e_backtest.py

plan-e-v1:  ## 跑方案 E v1 旧规则
	$(PYTHON) $(SCRIPTS)/plan_e_backtest.py --no-filter

plan-e-compare:  ## 方案 E v1 vs v2 对比
	$(PYTHON) $(SCRIPTS)/plan_e_backtest.py --compare

plan-e-today:  ## 方案 E 当日信号
	$(PYTHON) $(SCRIPTS)/plan_e_backtest.py --today

journal:  ## 实盘跟踪 daily 模式
	$(PYTHON) $(SCRIPTS)/plan_e_journal.py --mode daily

journal-status:  ## 实盘跟踪 status 模式
	$(PYTHON) $(SCRIPTS)/plan_e_journal.py --mode status

journal-weekly:  ## 实盘跟踪 weekly 模式
	$(PYTHON) $(SCRIPTS)/plan_e_journal.py --mode weekly

journal-buy:  ## 实盘买入（CODE=600519 PRICE=10.50 QTY=1000）
	$(PYTHON) $(SCRIPTS)/plan_e_journal.py --mode buy --code $(CODE) --price $(PRICE) --qty $(QTY)

journal-sell:  ## 实盘卖出（CODE=600519 PRICE=13.65 REASON=TP）
	$(PYTHON) $(SCRIPTS)/plan_e_journal.py --mode sell --code $(CODE) --price $(PRICE) --reason $(REASON)

simulate:  ## 模拟交易（START=2026-08-27 END=2026-09-25 CAPITAL=100000）
	$(PYTHON) $(SCRIPTS)/plan_e_journal.py --mode simulate \
		--start $(START) --end $(END) --capital $(CAPITAL)

# ============== 自动化调度 ==============
# ⚠️ 部署铁律：凡修改 systemd/ 目录下任何 *.service / *.timer / *.path 文件，提交后
#    必须执行 `make timer-install`（将文件复制到 ~/.config/systemd/user/，即 systemd 实际加载目录），
#    再 `systemctl --user daemon-reload`；对 *.path 还需 `restart <单元>.path` 重建 inotify 监视，
#    对处于 start-limit-hit 的单元还需 `reset-failed`。否则 systemd 加载的仍是旧文件。
#    漂移与限流会被 `make status` / `make canary` 的 obsidian 监控持续检测并告警。
timer-install:  ## 安装 systemd timer/path（复制到 user systemd，凡改 systemd/ 必跑）
	@echo "$(CYAN)▶ 复制 timer/service/path 到 user systemd$(NC)"
	@mkdir -p $(USER_SYSTEMD)
	@cp --remove-destination $(SYSTEMD_DIR)/*.service $(SYSTEMD_DIR)/*.timer $(SYSTEMD_DIR)/*.path $(USER_SYSTEMD)/
	@echo "$(GREEN)✓ timer/path 已复制（记得 daemon-reload + 必要时 restart/reset-failed）$(NC)"

timer-enable: timer-install  ## 启用所有 timer 与 path 单元
	@echo "$(CYAN)▶ 重载 systemd$(NC)"
	systemctl --user daemon-reload
	@echo "$(CYAN)▶ 启用 timer$(NC)"
	@for t in tdxfeed-sync predict-daily plan-e-journal plan-e-weekly canary-health; do \
		systemctl --user enable $$t.timer 2>&1 | head -1; \
		systemctl --user start $$t.timer; \
	done
	@echo "$(CYAN)▶ 启用 path 单元（Obsidian 同步）$(NC)"
	systemctl --user enable obsidian-sync.path 2>&1 | head -1
	systemctl --user start obsidian-sync.path
	@echo "$(GREEN)✓ 所有 timer/path 已启用$(NC)"
	@echo ""
	@$(MAKE) timer-status

timer-disable:  ## 禁用所有 timer 与 path 单元
	@for t in tdxfeed-sync predict-daily plan-e-journal plan-e-weekly canary-health; do \
		systemctl --user disable $$t.timer; \
		systemctl --user stop $$t.timer 2>/dev/null || true; \
	done
	systemctl --user disable obsidian-sync.path 2>/dev/null || true
	systemctl --user stop obsidian-sync.path 2>/dev/null || true
	@echo "$(GREEN)✓ 所有 timer/path 已禁用$(NC)"

timer-status:  ## 查看所有 timer/path 状态
	@echo "$(CYAN)$(BOLD)=== tdx-data-feed Timers ===$(NC)"
	systemctl --user list-timers --all | grep -E "tdxfeed|predict-daily|plan-e|canary" || echo "  (无 timer 运行)"
	@echo "$(CYAN)$(BOLD)--- path 单元 ---$(NC)"
	systemctl --user list-units --all --type=path | grep -E "tdxfeed|obsidian|plan-e" || echo "  (无 path 运行)"

timer-logs:  ## 查看 timer 日志
	@echo "$(CYAN)$(BOLD)=== 最近日志 ===$(NC)"
	@for f in $(LOGS)/*.log; do \
		echo ""; \
		echo "$(YELLOW)$$f:$(NC)"; \
		tail -5 $$f; \
	done

# ============== 监控 ==============
canary:  ## 跑一次健康检查（详细输出）
	$(PYTHON) $(SCRIPTS)/canary_health_check.py
	@echo ""
	@echo "$(CYAN)$(BOLD)=== 历史最近 5 次 ===$(NC)"
	@tail -5 $(LOGS)/canary.log 2>/dev/null || echo "  (无历史)"

canary-quiet:  ## 静默检查（仅异常时输出，systemd 用）
	$(PYTHON) $(SCRIPTS)/canary_health_check.py --quiet

status:  ## 系统全状态（数据/模型/timer/canary）
	@echo "$(CYAN)$(BOLD)╔══════════════════════════════════════════════╗$(NC)"
	@echo "$(CYAN)$(BOLD)║          tdx-data-feed 系统状态               ║$(NC)"
	@echo "$(CYAN)$(BOLD)╚══════════════════════════════════════════════╝$(NC)"
	@echo ""
	@echo "$(YELLOW)$(BOLD)📊 数据:$(NC)"
	@echo "  股票清单: $(shell jq 'length' $(DATA)/all_a_stocks.json 2>/dev/null || echo 'N/A') 只"
	@echo "  日 K:     $(shell ls $(DATA)/parquet/daily/ 2>/dev/null | wc -l) 文件"
	@echo "  复权因子: $(shell ls $(DATA)/adj_factor/ 2>/dev/null | wc -l) 文件"
	@echo ""
	@echo "$(YELLOW)$(BOLD)🤖 模型 (Ollama):$(NC)"
	@$(OLLAMA) list 2>/dev/null | grep tdx | awk '{printf "  %-30s %s\n", $$1, $$3}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)⏰ Timer:$(NC)"
	@systemctl --user list-timers --all 2>/dev/null | grep -E "tdxfeed|predict-daily|plan-e|canary|obsidian" | awk '{printf "  %-30s %s %s\n", $$NF, $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)$(BOLD)🏥 Canary:$(NC)"
	@tail -1 $(LOGS)/canary.log 2>/dev/null || echo "  (无 canary 日志)"
	@echo ""
	@echo "$(YELLOW)$(BOLD)🔄 Obsidian 同步监控 (obsidian-sync):$(NC)"
	@bash $(SCRIPTS)/verify_obsidian_systemd.sh --readonly || \
		echo "$(RED)$(BOLD)⚠ obsidian-sync 监控异常：存在 FAIL（详见上方；漂移需 make timer-install，限流需 reset-failed）$(NC)"
	@echo ""
	@echo "$(YELLOW)$(BOLD)💾 磁盘:$(NC)"
	@df -h /home | tail -1 | awk '{printf "  %s 已用 %s (%s)\n", $$2, $$3, $$5}'

verify-obsidian:  ## 验证 obsidian-sync systemd 重载/部署（V0–V8，含触发测试）
	@bash $(SCRIPTS)/verify_obsidian_systemd.sh

verify-obsidian-quick:  ## 仅只读静态检查（V0–V4），用于日常
	@bash $(SCRIPTS)/verify_obsidian_systemd.sh --readonly

health: canary  ## 别名

verify-math:  ## 兼容旧名（v1 数学闭环），等价 verify-obsidian-templates
	@$(MAKE) verify-obsidian-templates

verify-obsidian-templates:  ## 校验全 vault 源 ↔ 部署零漂移（v2.1：共享+分 vault 配置 JSON）；需 VAULT_KIND
	@bash $(SCRIPTS)/verify-obsidian-templates.sh --vault $${VAULT:-/home/jiuben/StockVault} --vault-kind $${VAULT_KIND:-stockvault} --prefix "$${PREFIX:-05-知识库/数学}" --math-tpl-dir "$${MATH_TPL_DIR:-06-模板/math}" --nonmath-tpl-dir "$${NONMATH_TPL_DIR:-06-模板}"

precommit-verify:  ## commit 前跑 verify-obsidian-templates（git 钩子未启用时的手动门）；需 VAULT_KIND
	@bash $(SCRIPTS)/verify-obsidian-templates.sh --vault $${VAULT:-/home/jiuben/StockVault} --vault-kind $${VAULT_KIND:-stockvault} --prefix "$${PREFIX:-05-知识库/数学}" --math-tpl-dir "$${MATH_TPL_DIR:-06-模板/math}" --nonmath-tpl-dir "$${NONMATH_TPL_DIR:-06-模板}"

install-obsidian-templates:  ## 部署源 → vault（共享+分 vault JSON），备份 .bak-<ts>；需 VAULT_KIND
	@bash $(SCRIPTS)/install-obsidian-templates.sh --vault $${VAULT:-/home/jiuben/StockVault} --vault-kind $${VAULT_KIND:-stockvault}

verify-obsidian-data:  ## Phase 2 数据完整性：元数据字段 + 链接完整性（断链/循环依赖）
	@python3 $(SCRIPTS)/verify_obsidian_metadata.py --vault $${VAULT:-/home/jiuben/StockVault}
	@python3 $(SCRIPTS)/verify_obsidian_links.py --vault $${VAULT:-/home/jiuben/StockVault}

verify-obsidian-metadata:  ## 仅元数据字段完整性校验
	@python3 $(SCRIPTS)/verify_obsidian_metadata.py --vault $${VAULT:-/home/jiuben/StockVault}

verify-obsidian-links:  ## 仅链接完整性 + 循环依赖校验
	@python3 $(SCRIPTS)/verify_obsidian_links.py --vault $${VAULT:-/home/jiuben/StockVault}

logs:  ## 查看所有日志（最近 5 行）
	@echo "$(CYAN)$(BOLD)=== 日志汇总 ===$(NC)"
	@for f in $(LOGS)/*.log; do \
		test -f $$f || continue; \
		echo ""; \
		echo "$(YELLOW)$(BOLD)$$f:$(NC)"; \
		tail -3 $$f; \
	done

logs-follow:  ## 实时跟踪所有日志
	tail -F $(LOGS)/*.log

# ============== 维护 ==============
clean:  ## 清 .pyc / __pycache__ / 测试缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage .mypy_cache
	@echo "$(GREEN)✓ 清理完成$(NC)"

clean-predictions:  ## 清预测结果（保留 latest）
	find $(DATA)/predictions -type f ! -name "predict_latest.md" -delete
	@echo "$(GREEN)✓ 预测结果已清理（保留 latest）$(NC)"

clean-logs:  ## 清日志（保留最近 7 天）
	find $(LOGS) -type f -mtime +7 -delete
	@echo "$(GREEN)✓ 旧日志已清理$(NC)"

backup:  ## 备份关键数据（不含 GGUF 大文件）
	@echo "$(CYAN)▶ 备份到 /home/jiuben/backups/$(NC)"
	@mkdir -p /home/jiuben/backups
	@tar czf /home/jiuben/backups/tdx-data-feed-$(shell date +%Y%m%d).tar.gz \
		--exclude='data/parquet-vol-backup' \
		--exclude='*.gguf' \
		--exclude='venv' \
		--exclude='__pycache__' \
		--exclude='.git' \
		--exclude='htmlcov' \
		.
	@ls -la /home/jiuben/backups/tdx-data-feed-*.tar.gz | tail -1
	@echo "$(GREEN)✓ 备份完成$(NC)"

restore:  ## 恢复备份（参数 FILE=/path/to/backup.tar.gz）
	@echo "$(YELLOW)⚠ 这将覆盖当前数据$(NC)"
	@test -f $(FILE) || (echo "$(RED)❌ 文件不存在: $(FILE)$(NC)" && exit 1)
	tar xzf $(FILE)
	@echo "$(GREEN)✓ 恢复完成$(NC)"

reset-data:  ## 重置数据（删 parquet + adj_factor + predictions，需重新下载）
	@echo "$(YELLOW)⚠ 这将删除所有数据，确认请输入 yes$(NC)"
	@read confirm && test "$$confirm" = "yes" || (echo "已取消" && exit 1)
	rm -rf $(DATA)/parquet/daily/* $(DATA)/adj_factor/*
	rm -rf $(DATA)/predictions/*
	@echo "$(GREEN)✓ 数据已重置$(NC)"
	@echo "$(YELLOW)下一步: make data-universe && make data-sync && make data-adj$(NC)"

# ============== 完整流程 ==============
pipeline: install data-universe data-sync data-adj analyze-all deploy-all timer-enable  ## 完整流程（从零到全自动化）

quickstart: install predict  ## 5 分钟快速上手

ci: lint format test canary  ## CI 检查（lint + test + health）

# ============== 帮助分类 ==============
help-install:  ## 安装类目标
	@grep -E "^(install|venv|setup|update):" $(MAKEFILE_LIST) | awk '{print $$1}'

help-data:  ## 数据类目标
	@grep -E "^data-" $(MAKEFILE_LIST) | awk '{print $$1}'

help-analyze:  ## 分析类目标
	@grep -E "^analyze-" $(MAKEFILE_LIST) | awk '{print $$1}'

help-train:  ## 训练类目标
	@grep -E "^train-" $(MAKEFILE_LIST) | awk '{print $$1}'

help-deploy:  ## 部署类目标
	@grep -E "^deploy-" $(MAKEFILE_LIST) | awk '{print $$1}'

help-timer:  ## 自动化类目标
	@grep -E "^timer-" $(MAKEFILE_LIST) | awk '{print $$1}'

help-monitor:  ## 监控类目标
	@grep -E "^(canary|status|logs|health)" $(MAKEFILE_LIST) | awk '{print $$1}'
