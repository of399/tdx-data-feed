#!/usr/bin/env python3
"""
分钟线补齐脚本（min15 / min60）

设计目标：
  - 从 data/parquet/daily/*.parquet 派生全市场正股 symbols（不依赖 vipdoc 目录）
  - 支持 --period 15 / 60
  - 支持 --codes-file 指定缺失清单（精确补齐指定 code）
  - 复用 min_sync2.py 已有的 fetch_sina / save_rows_parquet
  - fail-fast：连续 N 段失败 或 总时间超 M 分钟立即退出
  - 断点续跑：文件存在且 size > 0 自动跳过
  - 缺口台账：写入 /tmp/min_backfill_<period>_<ts>.jsonl
  - 实时进度 + ETA + 失败分类统计

用法示例：
  python scripts/backfill_minutes.py --period 15 --codes-file data/missing_min15.txt
  python scripts/backfill_minutes.py --period 60 --codes-file data/missing_min60.txt
  python scripts/backfill_minutes.py --period 15 --max-total-minutes 60
  python scripts/backfill_minutes.py --period 60 --all-stocks        # 全市场正股
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, '/home/jiuben/tdx-data-feed')
sys.path.insert(0, '/home/jiuben/tdx-data-feed/tdxfeed')

from kline import fetch_sina, save_rows_parquet

ROOT = Path('/home/jiuben/tdx-data-feed')
OUT = ROOT / 'data' / 'parquet'

# 6位代码 → 加 sh/sz/bj 前缀（用 daily 实际文件名前缀推断）
PREFIX_BY_START = {
    '60': 'sh', '68': 'sh', '90': 'bj', '11': 'sh', '13': 'sh',
    '00': 'sz', '20': 'sz', '30': 'sz', '12': 'sz', '15': 'sz',
}


def daily_prefix_map():
    """从 data/parquet/daily/ 实际文件名推断每个 6 位代码的市场前缀"""
    m = {}
    for p in (x for x in (OUT / 'daily').glob('*.parquet') if '.bak' not in x.name):
        s = p.stem
        m_pat = re.match(r'^(sh|sz|bj)(\d{6})$', s)
        if m_pat:
            m[m_pat.group(2)] = m_pat.group(1)
    return m


def load_codes_from_file(fp: Path):
    """从一行一个 6 位 code 的文件读 symbols"""
    codes = [ln.strip() for ln in fp.read_text().splitlines() if ln.strip() and re.match(r'^\d{6}$', ln.strip())]
    return codes


def list_all_daily_codes():
    """从 data/parquet/daily/ 列全市场 6 位代码"""
    m = daily_prefix_map()
    return list(m.keys())


def already_have(period: int, code: str):
    """检查 file 是否已存在且非空"""
    fp = OUT / f'min{period}' / f'{code}.parquet'
    if fp.exists() and fp.stat().st_size > 0:
        return True
    return False


def append_journal(journal_fp: Path, row: dict):
    journal_fp.parent.mkdir(parents=True, exist_ok=True)
    with journal_fp.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', required=True, choices=['15', '60'])
    ap.add_argument('--codes-file', type=str, default=None, help='一行一个 6 位 code 的清单')
    ap.add_argument('--all-stocks', action='store_true', help='从 daily parquet 取全市场正股')
    ap.add_argument('--max-total-minutes', type=int, default=30)
    ap.add_argument('--consecutive-fail-max', type=int, default=10)
    ap.add_argument('--sleep', type=float, default=0.3)
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()

    period = int(args.period)

    # 1) 准备代码列表
    if args.codes_file:
        codes = load_codes_from_file(Path(args.codes_file))
        src = f'codes-file: {args.codes_file}'
    elif args.all_stocks:
        codes = list_all_daily_codes()
        src = 'all-stocks (daily parquet)'
    else:
        print('ERROR: 必须指定 --codes-file 或 --all-stocks', file=sys.stderr)
        sys.exit(2)

    if args.limit:
        codes = codes[:args.limit]

    print(f'[backfill-min{period}] 源: {src}, 总数: {len(codes)}', flush=True)
    print(f'[backfill-min{period}] fail-fast: consecutive_fail_max={args.consecutive_fail_max} max_total_minutes={args.max_total_minutes}', flush=True)

    # 2) 准备符号（加 sh/sz/bj 前缀）
    pref_map = daily_prefix_map()
    symbols = []
    for c in codes:
        pref = pref_map.get(c)
        if pref is None:
            # 推断兜底
            pref = PREFIX_BY_START.get(c[:2])
        if pref:
            symbols.append(f'{pref}{c}')
        else:
            print(f'  WARN: 无法推断 {c} 的市场前缀，跳过', flush=True)

    # 3) 过滤已有文件
    final = []
    skipped = 0
    for s in symbols:
        code = s[2:]
        if already_have(period, code):
            skipped += 1
            continue
        final.append(s)
    print(f'[backfill-min{period}] 待下载: {len(final)} 只, 已跳过(已有): {skipped}', flush=True)
    if not final:
        print('[backfill-min{period}] 无需下载，退出', flush=True)
        return

    # 4) 缺口台账
    journal = Path(f'/tmp/min_backfill_{period}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.jsonl')

    # 5) 主循环
    t0 = time.time()
    ok = fail = consec_fail = 0
    exit_status = 'success'
    fail_reasons = {}
    final.sort()
    for i, sym in enumerate(final):
        # 全局时间检查
        el_min = (time.time() - t0) / 60
        if el_min > args.max_total_minutes:
            print(f'[backfill-min{period}] ⚠ 超过 {args.max_total_minutes} 分钟（{el_min:.1f}min），退出', flush=True)
            exit_status = 'fail-fast-time'
            break

        code = sym[2:]
        prefix = sym[:2]
        out_fp = OUT / f'min{period}' / f'{code}.parquet'
        t1 = time.time()
        err = None
        try:
            rows = fetch_sina(sym, scale=period)
            if rows:
                save_rows_parquet(sym, rows, period)
                ok += 1
                consec_fail = 0
            else:
                fail += 1
                consec_fail += 1
                err = 'empty'
        except Exception as e:
            fail += 1
            consec_fail += 1
            err = str(e)[:120]
        dur = time.time() - t1

        # 写台账
        append_journal(journal, {
            'ts': datetime.now().isoformat(),
            'sym': sym,
            'period': period,
            'ok': err is None,
            'err': err,
            'rows': len(rows) if rows and err is None else 0,
            'dur_s': round(dur, 2),
        })

        if err:
            fail_reasons[err] = fail_reasons.get(err, 0) + 1

        # 进度
        if (i + 1) % 50 == 0 or i == len(final) - 1:
            el = time.time() - t0
            eta = el / (i + 1) * (len(final) - i - 1)
            print(f'  [{i+1}/{len(final)}] ok={ok} fail={fail} skip={skipped} 已用 {el:.0f}s ETA {eta:.0f}s consec_fail={consec_fail}', flush=True)

        # 连续失败退出
        if consec_fail >= args.consecutive_fail_max:
            print(f'[backfill-min{period}] ⚠ 连续失败 {consec_fail} 段（>={args.consecutive_fail_max}），exit_status=fail-fast', flush=True)
            exit_status = 'fail-fast-consecutive'
            break

        time.sleep(args.sleep)

    el = time.time() - t0
    print(f'[backfill-min{period}] 完成: ok={ok} fail={fail} skip={skipped} exit_status={exit_status} 总耗时 {el:.0f}s', flush=True)
    if fail_reasons:
        print(f'[backfill-min{period}] 失败原因 Top 10:', flush=True)
        for r, n in sorted(fail_reasons.items(), key=lambda x: -x[1])[:10]:
            print(f'  {n} 次: {r[:100]}', flush=True)
    print(f'[backfill-min{period}] 台账: {journal}', flush=True)
    if exit_status != 'success':
        sys.exit(2)


if __name__ == '__main__':
    main()
