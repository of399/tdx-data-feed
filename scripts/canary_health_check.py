#!/usr/bin/env python3
"""
Canary 健康检查脚本
====================

每 5 分钟跑一次，检查：
  1. Ollama 服务在线 + 模型可访问
  2. 数据采集（tdxfeed-sync）健康
  3. 关键数据文件存在性
  4. GPU 状态
  5. systemd timer 状态
  6. obsidian-sync 部署/重载状态（防仓库-部署漂移与 start-limit-hit）

输出：
  - logs/canary.log（追加）
  - logs/canary_history.jsonl（结构化时序）

异常时写 stderr → systemd 会发邮件或触发 alert

【运行】
  venv/bin/python scripts/canary_health_check.py            # 一次性
  venv/bin/python scripts/canary_health_check.py --quiet    # 静默模式（仅异常时输出）
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
LOG_FILE = ROOT / 'logs' / 'canary.log'
HISTORY_FILE = ROOT / 'logs' / 'canary_history.jsonl'

# 阈值
OLLAMA_TIMEOUT = 5           # 秒
PARQUET_MIN_COUNT = 5000     # 最少日 K 文件数
ADJ_MIN_COUNT = 5000         # 最少复权因子文件数
DISK_MIN_FREE_GB = 20        # 最小剩余磁盘 GB


def check_ollama() -> dict:
    """检查 Ollama 服务"""
    result = {'name': 'ollama', 'status': 'ok', 'details': {}}
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=OLLAMA_TIMEOUT)
        if r.status_code == 200:
            models = r.json().get('models', [])
            tdx_models = [m['name'] for m in models if 'tdx' in m['name']]
            result['details'] = {
                'total_models': len(models),
                'tdx_models': tdx_models,
            }
            if not tdx_models:
                result['status'] = 'warn'
                result['message'] = 'no tdx models loaded'
        else:
            result['status'] = 'error'
            result['message'] = f'HTTP {r.status_code}'
    except requests.exceptions.ConnectionError:
        result['status'] = 'error'
        result['message'] = 'connection refused (ollama not running)'
    except Exception as e:
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_tdxfeed_sync() -> dict:
    """检查最后一次 tdxfeed-sync 时间（应该 ≤ 24 小时前）"""
    result = {'name': 'tdxfeed_sync', 'status': 'ok', 'details': {}}
    try:
        # 查 systemd 上次运行时间
        out = subprocess.run(
            ['systemctl', '--user', 'show', 'tdxfeed-sync.service',
             '--property=ActiveEnterTimestamp,Result,SubState'],
            capture_output=True, text=True, timeout=5
        )
        lines = out.stdout.strip().split('\n')
        details = {}
        for line in lines:
            if '=' in line:
                k, v = line.split('=', 1)
                details[k] = v

        result['details'] = details

        # 检查是否是成功（exit code 0）
        if 'Result' in details and details['Result'] != 'success':
            result['status'] = 'warn'
            result['message'] = f"last run result: {details['Result']}"

        # 检查时间
        timestamp_str = details.get('ActiveEnterTimestamp', '')
        if timestamp_str:
            # 格式：Sun 2026-09-27 18:00:12 CST
            try:
                ts_str = timestamp_str.replace('CST', '').strip()
                # 取后两段
                parts = ts_str.split()
                if len(parts) >= 3:
                    ts = datetime.strptime(' '.join(parts[-3:]), '%Y-%m-%d %H:%M:%S')
                    age = datetime.now() - ts
                    result['details']['age_hours'] = round(age.total_seconds() / 3600, 2)
                    if age > timedelta(days=2):
                        result['status'] = 'warn'
                        result['message'] = f'sync too old: {age}'
            except Exception:
                pass
    except Exception as e:
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_data_files() -> dict:
    """检查关键数据文件数量"""
    result = {'name': 'data_files', 'status': 'ok', 'details': {}}

    pq_dir = ROOT / 'data' / 'parquet' / 'daily'
    adj_dir = ROOT / 'data' / 'adj_factor'

    n_pq = len([p for p in pq_dir.glob('*.parquet') if '.bak' not in p.name]) if pq_dir.exists() else 0
    n_adj = len([p for p in adj_dir.glob('*.parquet') if '.bak' not in p.name]) if adj_dir.exists() else 0

    result['details'] = {
        'parquet_daily': n_pq,
        'adj_factor': n_adj,
    }

    if n_pq < PARQUET_MIN_COUNT:
        result['status'] = 'warn'
        result['message'] = f'parquet/daily only {n_pq} (< {PARQUET_MIN_COUNT})'
    if n_adj < ADJ_MIN_COUNT:
        result['status'] = 'warn'
        result['message'] = f'adj_factor only {n_adj} (< {ADJ_MIN_COUNT})'

    return result


def check_disk() -> dict:
    """检查磁盘空间"""
    result = {'name': 'disk', 'status': 'ok', 'details': {}}
    try:
        st = os.statvfs('/home')
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
        used_pct = (1 - st.f_bavail / st.f_blocks) * 100

        result['details'] = {
            'free_gb': round(free_gb, 2),
            'total_gb': round(total_gb, 2),
            'used_pct': round(used_pct, 2),
        }

        if free_gb < DISK_MIN_FREE_GB:
            result['status'] = 'error'
            result['message'] = f'only {free_gb:.1f}GB free (< {DISK_MIN_FREE_GB}GB)'
    except Exception as e:
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_timers() -> dict:
    """检查关键 systemd timer 是否激活"""
    result = {'name': 'timers', 'status': 'ok', 'details': {}}
    timers = ['predict-daily.timer', 'plan-e-journal.timer', 'tdxfeed-sync.timer']
    inactive = []
    for t in timers:
        try:
            out = subprocess.run(
                ['systemctl', '--user', 'is-active', t],
                capture_output=True, text=True, timeout=3
            )
            if out.stdout.strip() != 'active':
                inactive.append(t)
        except Exception:
            inactive.append(t)
    result['details'] = {'checked': timers, 'inactive': inactive}
    if inactive:
        result['status'] = 'warn'
        result['message'] = f'inactive timers: {inactive}'
    return result


def check_gpu() -> dict:
    """检查 GPU 状态"""
    result = {'name': 'gpu', 'status': 'ok', 'details': {}}
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.used,utilization.gpu',
             '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in out.stdout.strip().split('\n') if l.strip()]
        result['details']['gpus'] = lines
        # 如果有 GPU 利用率持续 > 95% 5分钟以上，警告（防止卡住）
        for line in lines:
            parts = line.split(',')
            if len(parts) >= 3:
                util = parts[2].strip().replace('%', '')
                if util and int(util) > 95:
                    result['status'] = 'warn'
                    result['message'] = f'GPU {parts[0]} util {util}% (likely stuck)'
                    break
    except FileNotFoundError:
        result['status'] = 'skip'
        result['message'] = 'nvidia-smi not found'
    except Exception as e:
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_obsidian_sync() -> dict:
    """检查 obsidian-sync 的 systemd 单元部署/重载状态（防漂移与 start-limit-hit）

    直接调用 scripts/verify_obsidian_systemd.sh --readonly（只读静态检查 V0–V4），
    由其作为单一事实来源，避免逻辑重复。失败时返回 'error' 以参与 canary 的非零退出告警。
    """
    result = {'name': 'obsidian_sync', 'status': 'ok', 'details': {}}
    script = ROOT / 'scripts' / 'verify_obsidian_systemd.sh'
    if not script.exists():
        result['status'] = 'error'
        result['message'] = f'verify script missing: {script}'
        return result
    try:
        proc = subprocess.run(
            ['bash', str(script), '--readonly'],
            capture_output=True, text=True, timeout=30
        )
        out = proc.stdout.strip()
        fail_lines = [l for l in out.splitlines() if '[FAIL]' in l]
        summary = [l for l in out.splitlines() if l.startswith('OBSIDIAN_SYSTEMD:')]
        result['details'] = {'failures': fail_lines, 'summary': summary}
        if proc.returncode != 0:
            result['status'] = 'error'
            result['message'] = '; '.join(fail_lines)[:300] or 'obsidian systemd check failed'
        else:
            result['status'] = 'ok'
            result['message'] = summary[0] if summary else 'ok'
    except subprocess.TimeoutExpired:
        result['status'] = 'error'
        result['message'] = 'verify script timeout'
    except Exception as e:
        result['status'] = 'error'
        result['message'] = f'check error: {str(e)[:200]}'
    return result


def run_all_checks() -> dict:
    """跑所有检查"""
    return {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'checks': [
            check_ollama(),
            check_tdxfeed_sync(),
            check_data_files(),
            check_disk(),
            check_timers(),
            check_gpu(),
            check_obsidian_sync(),
        ]
    }


def main():
    parser = argparse.ArgumentParser(description='Canary 健康检查')
    parser.add_argument('--quiet', action='store_true', help='仅异常时输出')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    report = run_all_checks()
    all_checks = report['checks']
    n_err = sum(1 for c in all_checks if c['status'] == 'error')
    n_warn = sum(1 for c in all_checks if c['status'] == 'warn')

    # 写历史
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(report, ensure_ascii=False) + '\n')

    # 决定输出
    is_problem = n_err > 0 or n_warn > 0
    if args.quiet and not is_problem:
        sys.exit(0)

    # 简洁摘要
    status_emoji = {'ok': '✅', 'warn': '⚠️', 'error': '❌', 'skip': '⏭️'}
    line = f"[{report['ts']}] "
    for c in all_checks:
        line += f"{status_emoji.get(c['status'], '?')}{c['name']} "
    line += f"| err={n_err} warn={n_warn}"

    print(line)

    # 详情（异常时）
    if is_problem:
        print()
        for c in all_checks:
            if c['status'] in ('error', 'warn'):
                print(f"  ⚠ {c['name']}: {c['message']}")
                print(f"    details: {json.dumps(c['details'], ensure_ascii=False)[:200]}")

    # 写日志
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

    # 异常时退出非零（让 systemd alert）
    if n_err > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
