"""方案 B 下载包装：socket timeout + 超时重启 watchdog
- socket.setdefaulttimeout(20) 让 baostock 不会无限 hang
- 每 5 分钟检查文件数，无进展就 kill 重启
- 最多 3 次重启，全部 hang 就退出
"""
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

ADJ_DIR = Path('/home/jiuben/tdx-data-feed/data/adj_factor')
SCRIPT = '/home/jiuben/tdx-data-feed/scripts/download_adj_factor_b.py'
LOG = Path('/tmp/adj_factor_watch.log')


def now():
    return datetime.now().strftime('%H:%M:%S')


def log(msg):
    line = f'[{now()}] {msg}'
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def file_count():
    return len(list(ADJ_DIR.glob('*.parquet')))


def run_once(restart_n):
    log(f'启动 download_adj_factor_b.py (restart #{restart_n})')
    # 关键：socket timeout
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    log_path = f'/tmp/adj_factor_b_r{restart_n}.log'
    proc = subprocess.Popen(
        ['venv/bin/python', SCRIPT],
        cwd='/home/jiuben/tdx-data-feed',
        env=env,
        stdout=open(log_path, 'w'),  # noqa: SIM115  # Popen 内部管理 file obj 生命周期
        stderr=subprocess.STDOUT,
    )

    last_count = file_count()
    last_change = time.time()
    idle_min = 0

    while True:
        time.sleep(30)
        if proc.poll() is not None:
            log(f'脚本退出 RC={proc.returncode}')
            return 'exit'

        cur = file_count()
        if cur > last_count:
            log(f'  进度: {cur} (新增 {cur - last_count})')
            last_count = cur
            last_change = time.time()
            idle_min = 0
            continue

        idle_min = (time.time() - last_change) / 60
        if idle_min >= 4:  # 4 分钟无新增 → hang
            log(f'⚠ hang {idle_min:.1f}min 无新增，kill 重启')
            proc.kill()
            proc.wait(timeout=10)
            return 'hang'


def main():
    LOG.unlink(missing_ok=True)
    initial = file_count()
    log(f'起始文件数: {initial}/5565')

    for restart_n in range(3):
        result = run_once(restart_n)
        cur = file_count()
        log(f'本轮 {result}: 现 {cur}/5565 (新增 {cur - initial})')
        if cur >= 5550:  # 剩余基本是 920XXX
            log(f'基本完成 (剩余 {5565 - cur} 只预计 fail)，退出')
            break
        if result == 'exit' and cur == initial:
            log('脚本自己退出且无新增，不再重启')
            break

    final = file_count()
    log(f'\n最终: {final}/5565 (缺 {5565 - final} 只)')


if __name__ == '__main__':
    main()
