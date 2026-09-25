# Makefile · tdx-data-feed
# 统一开发入口
# 用法:
#   make help     # 看所有目标
#   make lint     # ruff check（不修改）
#   make fix      # ruff check --fix（自动修安全 fix）
#   make fix-unsafe # ruff check --fix --unsafe-fixes（含 unsafe fix, 需 review）
#   make format   # ruff format（与 black 兼容）
#   make test     # 跑 v5/tests/ 下所有 pytest
#   make test-cov # pytest + coverage 报告
#   make sop-quick # 告警链路 quick 验证（5s, 不侵入）
#   make sop-full  # 告警链路 full 验证（140s, 侵入, 冻结 8002 进程）
#   make audit    # 跑 quick 验证 + 显示 audit 目录报告
#   make clean    # 清 .pyc / .pytest_cache

.PHONY: help lint fix fix-unsafe format test test-cov sop-quick sop-full audit clean

VENV := /home/jiuben/tdx-data-feed/venv
PYTHON := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF := $(PYTHON) -m ruff
SOP := bash v5/tests/test_alerting_pipeline.sh

# --------- 颜色 ---------
GREEN := \033[0;32m
YELLOW := \033[0;33m
CYAN := \033[0;36m
NC := \033[0m

help:  ## 显示帮助
	@echo "$(CYAN)tdx-data-feed Makefile$(NC)"
	@echo ""
	@echo "$(YELLOW)代码质量:$(NC)"
	@grep -E '^(lint|fix|format):.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-12s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)测试:$(NC)"
	@grep -E '^(test|test-cov):.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-12s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)告警链路验证:$(NC)"
	@grep -E '^(sop-quick|sop-full):.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-12s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)其他:$(NC)"
	@grep -E '^(audit|clean):.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  $(GREEN)%-12s$(NC) %s\n", $$1, $$2}'

lint:  ## ruff check (不修改文件)
	$(RUFF) check v5/ --statistics

fix:  ## ruff auto-fix (安全 fix, 自动修)
	$(RUFF) check v5/ --fix
	@echo "$(GREEN)✓ safe auto-fix 完成$(NC)"

fix-unsafe:  ## ruff unsafe-fix (含 BREAKING 变更, 需 review)
	$(RUFF) check v5/ --fix --unsafe-fixes
	@echo "$(YELLOW)⚠ unsafe auto-fix 已应用, 请 review diff: git diff$(NC)"

format:  ## ruff format (与 black 兼容)
	$(RUFF) format v5/
	@echo "$(GREEN)✓ format 完成$(NC)"

test:  ## 跑 v5/tests/ 所有 pytest
	cd $(dir $(VENV)) && $(PYTEST) v5/tests/ -v

test-cov:  ## pytest + coverage (HTML 报告)
	cd $(dir $(VENV)) && $(PYTEST) v5/tests/ --cov=v5 --cov-report=term-missing --cov-report=html
	@echo "$(GREEN)✓ 覆盖率报告: htmlcov/index.html$(NC)"

sop-quick:  ## 告警链路 quick 验证 (5s, 非侵入)
	$(SOP) --report-json /tmp/sop-quick-report.json

sop-full:  ## 告警链路 full 验证 (140s, 侵入, 冻结 8002)
	$(SOP) --mode full --auto-sink --report-json /tmp/sop-full-report.json

audit:  ## 跑 quick + 显示 audit 目录最近报告
	$(MAKE) sop-quick
	@echo ""
	@echo "$(CYAN)=== v5/audit/ 最近 5 份报告 ===$(NC)"
	@ls -lt v5/audit/*.md 2>/dev/null | head -5 | awk '{print "  ", $$NF}'

clean:  ## 清 .pyc / .pytest_cache / __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
	@echo "$(GREEN)✓ 清理完成$(NC)"

.DEFAULT_GOAL := help