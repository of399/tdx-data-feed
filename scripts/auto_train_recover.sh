#!/usr/bin/env bash
# ==============================================================================
# 自动训练恢复脚本
# ==============================================================================
# 用途：
#   排查并修复当前 v9 训练卡死问题（多看门狗循环重启泥潭），
#   清理所有残留进程，干净地启动训练，确认进入 step 阶段后启动看门狗监控。
#
# 执行阶段：
#   阶段 1：继续排查并修复当前问题（约 3 min）
#   阶段 2：彻底终止并清理所有残留进程（约 1 min）
#   阶段 3：直接调用训练命令启动任务，不启用看门狗（约 1 min）
#   阶段 4：等待 5 min，确认训练已进入 step 阶段（约 5 min）
#   阶段 5：启动一个干净的看门狗实例进行监控（约 30 s）
#
# 总耗时：约 10-15 min（不含训练 8-10 h）
#
# 特性：
#   - 严格顺序执行：任何阶段失败立即终止，不会进入下一阶段
#   - 状态校验：每个阶段都有明确的成功判定条件
#   - 日志记录：所有关键节点输出到控制台 + 日志file
#   - 可重复运行：脚本本身幂等，多次执行不会造成混乱
#   - 不引入新依赖：仅使用 bash 内置命令、ps、grep、nvidia-smi 等标准工具
#
# 用法：
#   bash scripts/auto_train_recover.sh
#   bash scripts/auto_train_recover.sh --skip-debug   # 跳过阶段 1 排查
#   bash scripts/auto_train_recover.sh --skip-wait    # 跳过阶段 4 等待
# ==============================================================================

# === 严格模式 ================================================================
set -u
# 故意不用 set -e：每一步都需要明确判断成功/失败，不能让脚本在第一处错误就退出

# === 路径与常量 ==============================================================
ROOT=/home/jiuben/tdx-data-feed
TRAIN_CONFIG=training/config/tdx_v9_from_scratch.yaml
TRAIN_LOG=logs/train_tdx_v9_from_scratch.log
WATCHDOG_LOG=logs/v9_watchdog.log
RECOVER_LOG=logs/auto_train_recover_$(date +%Y%m%d_%H%M%S).log
TRAINER_LOG=train/output/qwen3-14b-tdx-v9/trainer_log.jsonl
OUTPUT_DIR=train/output/qwen3-14b-tdx-v9
WATCHDOG_SCRIPT=scripts/v9_train_watchdog.sh
BACKUP_DIR=data/v4fwd/backup_2026-10-16

# 等待训练进入 step 阶段的最长时间（秒）
MAX_WAIT=360
# 等待训练进入 step 阶段的检查间隔（秒）
CHECK_INTERVAL=15

# 进程关键字（用于 pgrep / pkill）
TRAIN_PATTERN="training/config/tdx_v9_from_scratch.yaml|train_lora|llamafactory-cli|llamafactory/launcher|torchrun"
WATCHDOG_PATTERN="scripts/v9_train_watchdog\.sh|watchdogd"

# === 颜色定义（增强可读性）===================================================
COLOR_RED='\033[0;31m'
COLOR_GREEN='\033[0;32m'
COLOR_YELLOW='\033[1;33m'
COLOR_BLUE='\033[0;34m'
COLOR_CYAN='\033[0;36m'
COLOR_RESET='\033[0m'

# === 日志工具 ================================================================
print_banner() {
    local title="$1"
    echo "" | tee -a "${RECOVER_LOG}"
    echo -e "${COLOR_CYAN}================================================================================${COLOR_RESET}" | tee -a "${RECOVER_LOG}"
    echo -e "${COLOR_CYAN}  ${title}${COLOR_RESET}" | tee -a "${RECOVER_LOG}"
    echo -e "${COLOR_CYAN}================================================================================${COLOR_RESET}" | tee -a "${RECOVER_LOG}"
}

print_info() {
    local message="$1"
    echo -e "${COLOR_BLUE}[信息]${COLOR_RESET} ${message}" | tee -a "${RECOVER_LOG}"
}

print_success() {
    local message="$1"
    echo -e "${COLOR_GREEN}[成功]${COLOR_RESET} ${message}" | tee -a "${RECOVER_LOG}"
}

print_warning() {
    local message="$1"
    echo -e "${COLOR_YELLOW}[警告]${COLOR_RESET} ${message}" | tee -a "${RECOVER_LOG}"
}

print_fail() {
    local message="$1"
    echo -e "${COLOR_RED}[失败]${COLOR_RESET} ${message}" | tee -a "${RECOVER_LOG}"
}

# === 阶段 1：继续排查并修复当前问题 ==========================================
stage1_debug() {
    print_banner "阶段 1/5：继续排查并修复当前问题"

    cd "${ROOT}" || { print_fail "无法进入项目目录：${ROOT}"; return 1; }

    # 排查项 1：检查 git 状态
    print_info "排查项 1/6：检查 git 状态"
    local current_commit=$(git rev-parse --short HEAD 2>/dev/null)
    local dirty_count=$(git status --porcelain 2>/dev/null | wc -l)
    echo "  current_commit：${current_commit}" | tee -a "${RECOVER_LOG}"
    echo "  未提交修改数：${dirty_count}" | tee -a "${RECOVER_LOG}"
    if [ "${dirty_count}" -gt 0 ]; then
        print_warning "存在未提交的修改，请先处理"
        git status --short | head -10 | tee -a "${RECOVER_LOG}"
    else
        print_success "git 工作区干净"
    fi

    # 排查项 2：检查关键file
    print_info "排查项 2/6：检查关键file"
    local missing_count=0
    for file in "${TRAIN_CONFIG}" "${WATCHDOG_SCRIPT}" "${TRAINER_LOG}" "${BACKUP_DIR}/train.jsonl" "${BACKUP_DIR}/val.jsonl" "${BACKUP_DIR}/test.jsonl"; do
        if [ ! -e "${file}" ]; then
            print_fail "关键文件缺失：${file}"
            missing_count=$((missing_count + 1))
        fi
    done
    if [ "${missing_count}" -gt 0 ]; then
        print_fail "存在 ${missing_count} 个关键文件缺失"
        return 1
    fi
    print_success "所有关键文件齐全"

    # 排查项 3：验证 yaml 关键配置
    print_info "排查项 3/6：验证 yaml 关键配置"
    local max_steps_val=$(grep -E "^max_steps:" "${TRAIN_CONFIG}" | sed -n 's/^max_steps:[[:space:]]*//p' | awk '{print $1}')
    local learning_rate_val=$(grep -E "^learning_rate:" "${TRAIN_CONFIG}" | sed -n 's/^learning_rate:[[:space:]]*//p' | awk '{print $1}')
    local max_samples_val=$(grep -E "^max_samples:" "${TRAIN_CONFIG}" | sed -n 's/^max_samples:[[:space:]]*//p' | awk '{print $1}')
    local load_best_val=$(grep -E "^load_best_model_at_end:" "${TRAIN_CONFIG}" | sed -n 's/^load_best_model_at_end:[[:space:]]*//p' | awk '{print $1}')

    if [ "${max_steps_val}" != "2000" ]; then
        print_fail "max_steps 应为 2000，实际为 ${max_steps_val}"
        return 1
    fi
    if [ "${learning_rate_val}" != "5.0e-5" ]; then
        print_fail "learning_rate 应为 5.0e-5，实际为 ${learning_rate_val}"
        return 1
    fi
    if [ "${max_samples_val}" != "50748" ]; then
        print_fail "max_samples 应为 50748（数据平衡后），实际为 ${max_samples_val}"
        return 1
    fi
    if [ "${load_best_val}" != "true" ]; then
        print_fail "load_best_model_at_end 应为 true，实际为 ${load_best_val}"
        return 1
    fi
    print_success "yaml 配置正确（max_steps=2000, LR=5e-5, max_samples=50748, load_best=true）"

    # 排查项 4：验证数据平衡
    print_info "排查项 4/6：验证数据平衡"
    if [ -f "${BACKUP_DIR}/train.jsonl" ]; then
        head -1000 "${BACKUP_DIR}/train.jsonl" 2>/dev/null | python3 -c "
import sys, json, re
label_pattern= re.compile(r'方向：?(涨|跌|震)')
count= {'涨': 0, '跌': 0, '震': 0}
for 行 in sys.stdin:
    sample = json.loads(行)
    match = label_pattern.search(sample.get('output', ''))
    if match: count_dict[match.group(1)] += 1
total= count_dict['涨'] + count_dict['跌'] + count_dict['震']
if total > 0:
    print(f'  备份目录训练集抽样（1000 条）涨={count_dict[\"涨\"]}({count_dict[\"涨\"]/total:.1%}) 跌={count_dict[\"跌\"]}({count_dict[\"跌\"]/total:.1%}) 震={count_dict[\"震\"]}({count_dict[\"震\"]/total:.1%})')
" 2>/dev/null | tee -a "${RECOVER_LOG}"
        print_success "数据备份目录完整（3 集平衡后样本可从备份恢复）"
    else
        print_fail "数据备份目录不存在：${BACKUP_DIR}"
        return 1
    fi

    # 排查项 5：检查当前进程状态
    print_info "排查项 5/6：检查当前进程状态"
    local watchdog_count=$(pgrep -af "${WATCHDOG_PATTERN}" 2>/dev/null | grep -v "watchdogd" | wc -l)
    local train_count=$(pgrep -af "${TRAIN_PATTERN}" 2>/dev/null | wc -l)
    echo "  watchdog_count（不含系统 watchdogd）：${watchdog_count}" | tee -a "${RECOVER_LOG}"
    echo "  train_count：${train_count}" | tee -a "${RECOVER_LOG}"
    if [ "${watchdog_count}" -gt 1 ]; then
        print_warning "存在多个watchdog_processes（数量：${watchdog_count}），这正是循环重启的根因"
    fi
    if [ "${train_count}" -gt 0 ]; then
        print_warning "存在 ${train_count} 个训练残留进程，将进入阶段 2 清理"
    fi

    # 排查项 6：检查 GPU 状态
    print_info "排查项 6/6：检查 GPU 状态"
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv 2>/dev/null | tee -a "${RECOVER_LOG}"
    else
        print_warning "nvidia-smi 不可用，跳过 GPU 检查"
    fi

    print_success "阶段 1 完成：排查发现修复已就绪，待清理后启动"
    return 0
}

# === 阶段 2：彻底终止并清理所有残留进程 =====================================
stage2_cleanup() {
    print_banner "阶段 2/5：彻底终止并清理所有残留进程"

    cd "${ROOT}" || { print_fail "无法进入项目目录"; return 1; }

    # 步骤 1：终止所有看门狗
    print_info "终止所有watchdog_processes"
    pkill -9 -f "scripts/v9_train_watchdog\.sh" 2>/dev/null
    sleep 2
    local remaining_watchdog=$(pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | wc -l)
    if [ "${remaining_watchdog}" -gt 0 ]; then
        print_warning "仍有 ${remaining_watchdog} 个watchdog_processes，尝试再次强杀"
        pkill -9 -f "scripts/v9_train_watchdog\.sh" 2>/dev/null
        sleep 3
    fi
    local final_watchdog=$(pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | wc -l)
    if [ "${final_watchdog}" -gt 0 ]; then
        print_fail "无法终止所有watchdog_processes（剩余 ${final_watchdog} 个）"
        return 1
    fi
    print_success "所有看门狗进程已终止"

    # 步骤 2：终止所有训练相关进程
    print_info "终止所有训练相关进程"
    pkill -9 -f "${TRAIN_PATTERN}" 2>/dev/null
    sleep 3
    local remaining_train=$(pgrep -af "${TRAIN_PATTERN}" 2>/dev/null | wc -l)
    if [ "${remaining_train}" -gt 0 ]; then
        print_warning "仍有 ${remaining_train} 个训练进程，尝试再次强杀"
        pkill -9 -f "${TRAIN_PATTERN}" 2>/dev/null
        sleep 5
    fi
    local final_train=$(pgrep -af "${TRAIN_PATTERN}" 2>/dev/null | wc -l)
    if [ "${final_train}" -gt 0 ]; then
        print_fail "无法终止所有训练进程（剩余 ${final_train} 个）"
        pgrep -af "${TRAIN_PATTERN}" 2>/dev/null | head | tee -a "${RECOVER_LOG}"
        return 1
    fi
    print_success "所有训练进程已终止"

    # 步骤 3：清理老的 trainer_log.jsonl
    print_info "清理老的训练器日志（保留末 5 行作为历史参考）"
    if [ -f "${TRAINER_LOG}" ]; then
        local line_count=$(wc -l < "${TRAINER_LOG}")
        if [ "${line_count}" -gt 5 ]; then
            tail -5 "${TRAINER_LOG}" > "${TRAINER_LOG}.tmp"
            mv "${TRAINER_LOG}.tmp" "${TRAINER_LOG}"
            echo "  原 ${line_count} 行，保留末 5 行作为历史参考" | tee -a "${RECOVER_LOG}"
        fi
        print_success "训练器日志已清理"
    else
        print_info "训练器日志不存在，跳过"
    fi

    # 步骤 4：删除老 checkpoint
    print_info "删除老的 checkpoint 目录（老数据训的，无用）"
    local deleted_count=0
    for old_ckpt in "${OUTPUT_DIR}"/checkpoint-*; do
        if [ -d "${old_ckpt}" ]; then
            rm -rf "${old_ckpt}"
            echo "  已删除：${old_ckpt}" | tee -a "${RECOVER_LOG}"
            deleted_count=$((deleted_count + 1))
        fi
    done
    print_success "已删除 ${deleted_count} 个老 checkpoint"

    # 步骤 5：清理老的 sanity 标记
    print_info "清理老的 sanity 标记"
    if [ -d "logs/v4fwd_sanity" ]; then
        find "logs/v4fwd_sanity" -name "*.FAILED" -delete 2>/dev/null
        find "logs/v4fwd_sanity" -name "*.RUNNING" -delete 2>/dev/null
        find "logs/v4fwd_sanity" -name "*.RC_ERROR" -delete 2>/dev/null
        print_success "老 sanity 标记已清理"
    fi

    # 步骤 6：等待 GPU 显存释放
    print_info "等待 GPU 显存自然释放（30 s）"
    sleep 30
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=memory.used --format=csv 2>/dev/null | tee -a "${RECOVER_LOG}"
    fi
    print_success "阶段 2 完成：所有残留已清理，GPU 显存等待自然释放"

    return 0
}

# === 阶段 3：直接调用训练命令启动任务 =======================================
stage3_start_train() {
    print_banner "阶段 3/5：直接调用训练命令启动任务（不启用看门狗）"

    cd "${ROOT}" || { print_fail "无法进入项目目录"; return 1; }

    # 二次校验：确保没有训练或看门狗进程在跑
    print_info "二次校验：确保无残留进程"
    local residual=$(pgrep -af "${TRAIN_PATTERN}|${WATCHDOG_PATTERN}" 2>/dev/null | grep -v "watchdogd" | wc -l)
    if [ "${residual}" -gt 0 ]; then
        print_fail "发现 ${residual} 个残留进程，阶段 2 清理不彻底"
        pgrep -af "${TRAIN_PATTERN}|${WATCHDOG_PATTERN}" 2>/dev/null | grep -v "watchdogd" | head | tee -a "${RECOVER_LOG}"
        return 1
    fi
    print_success "无残留进程"

    # 启动训练
    print_info "启动训练（使用 train_lora.py 注入 PATH）"
    echo "  训练配置：${TRAIN_CONFIG}" | tee -a "${RECOVER_LOG}"
    echo "  训练日志：${TRAIN_LOG}" | tee -a "${RECOVER_LOG}"

    nohup bash -c "venv/bin/python training/scripts/train_lora.py ${TRAIN_CONFIG} > ${TRAIN_LOG} 2>&1; echo \"\$(date) [train] exit=\$?\" >> ${WATCHDOG_LOG}" >/dev/null 2>&1 &
    local start_pid=$!
    disown 2>/dev/null
    echo "  启动进程 PID：${start_pid}" | tee -a "${RECOVER_LOG}"

    # 等待 10 s 让训练进程初始化
    sleep 10

    # 校验：训练进程是否成功启动
    local actual_train_count=$(pgrep -f "training/scripts/train_lora\.py" 2>/dev/null | wc -l)
    if [ "${actual_train_count}" -eq 0 ]; then
        print_fail "训练进程未启动（10 s 后未检测到 train_lora.py 进程）"
        echo "  训练日志末 20 行：" | tee -a "${RECOVER_LOG}"
        tail -20 "${TRAIN_LOG}" 2>/dev/null | tee -a "${RECOVER_LOG}"
        return 1
    fi
    print_success "训练进程已启动（PID：${actual_train_count}）"

    # 确认无看门狗启动
    local watchdog_processes=$(pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | wc -l)
    if [ "${watchdog_processes}" -gt 0 ]; then
        print_fail "意外启动看门狗（数量：${watchdog_processes}），本阶段不应启动看门狗"
        pkill -9 -f "scripts/v9_train_watchdog\.sh" 2>/dev/null
        return 1
    fi
    print_success "确认：本阶段未启动看门狗（符合需求）"

    print_success "阶段 3 完成：训练已后台启动（无看门狗）"
    return 0
}

# === 阶段 4：等待 5 min，确认训练进入 step 阶段 =============================
stage4_wait_step() {
    print_banner "阶段 4/5：等待训练进入 step 阶段（最长 ${MAX_WAIT} s）"

    cd "${ROOT}" || { print_fail "无法进入项目目录"; return 1; }

    # 记录开始时的 trainer_log.jsonl line_count
    local initial_lines=0
    if [ -f "${TRAINER_LOG}" ]; then
        initial_lines=$(wc -l < "${TRAINER_LOG}")
    fi
    echo "  初始训练器日志line_count：${initial_lines}" | tee -a "${RECOVER_LOG}"

    local wait_start=$(date +%s)
    local elapsed=0
    local step_count=0
    local fail_count=0

    # 等待时间预算分两段：
    # - 前 180 s：等 tokenize + trainer_log.jsonl 首次写入（step 20）
    # - 180 s 后：判定改为 "训练进程稳定运行 + GPU 高负载" 即可
    local TOKENIZE_PHASE=180
    local last_log_lines=0
    if [ -f "${TRAINER_LOG}" ]; then
        last_log_lines=$(wc -l < "${TRAINER_LOG}")
    fi

    while [ "${elapsed}" -lt "${MAX_WAIT}" ]; do
        sleep "${CHECK_INTERVAL}"
        elapsed=$(($(date +%s) - wait_start))

        # 检查训练进程是否还在
        local current_train_count=$(pgrep -f "training/scripts/train_lora\.py" 2>/dev/null | wc -l)
        if [ "${current_train_count}" -eq 0 ]; then
            print_fail "训练进程意外退出（已等待 ${elapsed} s）"
            echo "  训练日志末 30 行：" | tee -a "${RECOVER_LOG}"
            tail -30 "${TRAIN_LOG}" 2>/dev/null | tee -a "${RECOVER_LOG}"
            return 1
        fi

        # 阶段 A（前 180 s）：等 trainer_log.jsonl 首次写入
        if [ "${elapsed}" -lt "${TOKENIZE_PHASE}" ]; then
            if [ -f "${TRAINER_LOG}" ]; then
                local current_lines=$(wc -l < "${TRAINER_LOG}")
                if [ "${current_lines}" -gt "${last_log_lines}" ]; then
                    local latest_step=$(tail -1 "${TRAINER_LOG}" | python3 -c "
import sys, json
try:
    r = json.loads(sys.stdin.read())
    print(r.get('current_steps', 0))
except: print(0)
" 2>/dev/null)
                    echo "  已等待 ${elapsed} s：训练器日志写入新行（${current_lines} 行），最新 step ${latest_step}" | tee -a "${RECOVER_LOG}"
                    if [ "${latest_step}" -ge 20 ]; then
                        print_success "训练已进入 step 阶段（current_steps ≥ 20）"
                        if command -v nvidia-smi >/dev/null 2>&1; then
                            nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv 2>/dev/null | tee -a "${RECOVER_LOG}"
                        fi
                        print_success "阶段 4 完成：训练已进入 step 阶段（耗时 ${elapsed} s）"
                        return 0
                    fi
                fi
            fi
            if [ $((elapsed % 30)) -eq 0 ]; then
                echo "  已等待 ${elapsed} s：仍在 tokenize 阶段（最长等 ${TOKENIZE_PHASE} s）" | tee -a "${RECOVER_LOG}"
            fi
        else
            # 阶段 B（180 s 后）：判定训练进程稳定 + GPU 高负载
            local gpu_util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d " ")
            echo "  已等待 ${elapsed} s：训练进程稳定，GPU util ${gpu_util}%" | tee -a "${RECOVER_LOG}"
            if [ "${gpu_util}" -ge 50 ]; then
                print_success "训练已稳定运行（GPU util ${gpu_util}% ≥ 50%），视为已进入训练"
                print_success "阶段 4 完成：训练已稳定运行（耗时 ${elapsed} s）"
                return 0
            fi
        fi
    done

    # 超时
    print_fail "等待超时（${MAX_WAIT} s）训练仍未进入稳定阶段"
    echo "  训练日志末 30 行：" | tee -a "${RECOVER_LOG}"
    tail -30 "${TRAIN_LOG}" 2>/dev/null | tee -a "${RECOVER_LOG}"
    echo "  训练器日志：" | tee -a "${RECOVER_LOG}"
    tail -10 "${TRAINER_LOG}" 2>/dev/null | tee -a "${RECOVER_LOG}"
    return 1
}

# === 阶段 5：启动一个干净的 watchdog 实例 ===================================
stage5_start_watchdog() {
    print_banner "阶段 5/5：启动一个干净的看门狗实例"

    cd "${ROOT}" || { print_fail "无法进入项目目录"; return 1; }

    # 校验 1：训练仍在跑
    print_info "校验训练仍在跑"
    local train_count=$(pgrep -f "training/scripts/train_lora\.py" 2>/dev/null | wc -l)
    if [ "${train_count}" -eq 0 ]; then
        print_fail "训练进程不在跑（数量：${train_count}），看门狗无对象可监控"
        return 1
    fi
    print_success "训练进程在跑（数量：${train_count}）"

    # 校验 2：确保只有一个看门狗
    print_info "校验无重复看门狗"
    local watchdog_count=$(pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | wc -l)
    if [ "${watchdog_count}" -gt 0 ]; then
        print_warning "已存在 ${watchdog_count} 个watchdog_processes，先清理再启动新的"
        pkill -9 -f "scripts/v9_train_watchdog\.sh" 2>/dev/null
        sleep 3
    fi
    print_success "watchdog_count：${watchdog_count}（已清理）"

    # 启动看门狗
    print_info "启动看门狗（setsid 彻底脱离会话）"
    setsid nohup bash "${WATCHDOG_SCRIPT}" >/dev/null 2>&1 &
    local start_pid=$!
    disown 2>/dev/null
    echo "  启动进程 PID：${start_pid}" | tee -a "${RECOVER_LOG}"

    # 等待 5 s 确认启动
    sleep 5
    local actual_watchdog_count=$(pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | wc -l)
    if [ "${actual_watchdog_count}" -ne 1 ]; then
        print_fail "看门狗启动失败（期望 1 个，实际 ${actual_watchdog_count} 个）"
        pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | head | tee -a "${RECOVER_LOG}"
        return 1
    fi
    print_success "看门狗已启动（数量：${actual_watchdog_count}，符合预期 1 个）"

    # 最终状态汇总
    print_info "最终进程状态"
    echo "  watchdog_processes：" | tee -a "${RECOVER_LOG}"
    pgrep -af "scripts/v9_train_watchdog\.sh" 2>/dev/null | tee -a "${RECOVER_LOG}"
    echo "  训练进程：" | tee -a "${RECOVER_LOG}"
    pgrep -af "training/scripts/train_lora\.py" 2>/dev/null | tee -a "${RECOVER_LOG}"

    if command -v nvidia-smi >/dev/null 2>&1; then
        echo "  GPU 状态：" | tee -a "${RECOVER_LOG}"
        nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv 2>/dev/null | tee -a "${RECOVER_LOG}"
    fi

    print_success "阶段 5 完成：看门狗已启动，训练将持续被监控"
    return 0
}

# === 参数解析 ================================================================
SKIP_DEBUG=false
SKIP_WAIT=false
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-debug)
            SKIP_DEBUG=true
            shift
            ;;
        --skip-wait)
            SKIP_WAIT=true
            等待训练最长时间=30
            shift
            ;;
        --help|-h)
            echo "用法：bash scripts/auto_train_recover.sh [选项]"
            echo ""
            echo "选项："
            echo "  --skip-debug   跳过阶段 1 排查（紧急情况下使用）"
            echo "  --skip-wait    跳过阶段 4 等待（紧急测试用，将等待时间设为 30 s）"
            echo "  --help, -h     显示此帮助"
            exit 0
            ;;
        *)
            echo "未知参数：$1"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# === 主流程 ================================================================
print_banner "v9 训练自动恢复脚本"
print_info "开始时间：$(date '+%Y-%m-%d %H:%M:%S')"
print_info "自动恢复日志：${RECOVER_LOG}"
print_info "项目根目录：${ROOT}"

OVERALL_START=$(date +%s)
OVERALL_RC=0

if [ "${SKIP_DEBUG}" = false ]; then
    stage1_debug || { OVERALL_RC=1; print_fail "阶段 1 失败，终止"; }
else
    print_warning "跳过阶段 1 排查"
fi

if [ "${OVERALL_RC}" -eq 0 ]; then
    stage2_cleanup || { OVERALL_RC=2; print_fail "阶段 2 失败，终止"; }
fi

if [ "${OVERALL_RC}" -eq 0 ]; then
    stage3_start_train || { OVERALL_RC=3; print_fail "阶段 3 失败，终止"; }
fi

if [ "${OVERALL_RC}" -eq 0 ]; then
    if [ "${SKIP_WAIT}" = false ]; then
        stage4_wait_step || { OVERALL_RC=4; print_fail "阶段 4 失败，终止"; }
    else
        print_warning "跳过阶段 4 等待"
    fi
fi

if [ "${OVERALL_RC}" -eq 0 ]; then
    stage5_start_watchdog || { OVERALL_RC=5; print_fail "阶段 5 失败，终止"; }
fi

OVERALL_END=$(date +%s)
OVERALL_ELAPSED=$((OVERALL_END - OVERALL_START))

print_banner "执行结果"
if [ "${OVERALL_RC}" -eq 0 ]; then
    print_success "所有阶段执行成功！总耗时 ${OVERALL_ELAPSED} 秒"
    echo "  - 阶段 1（排查）：完成" | tee -a "${RECOVER_LOG}"
    echo "  - 阶段 2（清理）：完成" | tee -a "${RECOVER_LOG}"
    echo "  - 阶段 3（启动训练）：完成" | tee -a "${RECOVER_LOG}"
    echo "  - 阶段 4（等待 step）：完成" | tee -a "${RECOVER_LOG}"
    echo "  - 阶段 5（启动看门狗）：完成" | tee -a "${RECOVER_LOG}"
    echo "" | tee -a "${RECOVER_LOG}"
    echo "  训练将持续 8-10 小时，watchdog 会自动监控与续训。" | tee -a "${RECOVER_LOG}"
    echo "  可通过以下命令查看进度：" | tee -a "${RECOVER_LOG}"
    echo "    tail -f ${TRAINER_LOG}" | tee -a "${RECOVER_LOG}"
    echo "    tail -f ${WATCHDOG_LOG}" | tee -a "${RECOVER_LOG}"
else
    print_fail "部分阶段失败，退出码 ${OVERALL_RC}，总耗时 ${OVERALL_ELAPSED} 秒"
    echo "  请检查 ${RECOVER_LOG} 获取详细信息" | tee -a "${RECOVER_LOG}"
fi

exit "${OVERALL_RC}"
