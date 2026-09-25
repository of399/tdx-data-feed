"""方案 B：用 baostock 下载全 A 股复权因子
输出: data/adj_factor/{code}.parquet
  - date, adj_factor, qfq_close, raw_close
"""
import json
import socket
import sys
import time
import traceback
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

# baostock 网络调用全局 30s 超时（避免某只 hang 阻塞整个流程）
socket.setdefaulttimeout(30)

OUT_DIR = Path('/home/jiuben/tdx-data-feed/data/adj_factor')
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG = Path('/tmp/adj_factor_baostock.log')
ALL_STOCKS = Path('/home/jiuben/tdx-data-feed/data/all_a_stocks.json')

START_DATE = '2020-01-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')


def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def to_baostock_code(short: str) -> str:
    """000001 → sz.000001, 600519 → sh.600519, 830xxx → bj.830xxx"""
    if short.startswith('60') or short.startswith('68') or short.startswith('90') or short.startswith('11') or short.startswith('13'):
        return f'sh.{short}'
    if short.startswith('83') or short.startswith('87') or short.startswith('43'):
        return f'bj.{short}'
    return f'sz.{short}'


def fetch_one(bs, code_short: str, max_retries: int = 3):
    """下载单只股复权因子（adjustflag=2 前复权 vs 3 不复权）"""
    cache_fp = OUT_DIR / f'{code_short}.parquet'
    if cache_fp.exists():
        return 'cached'

    bs_code = to_baostock_code(code_short)
    last_err = None
    for attempt in range(max_retries):
        try:
            # 前复权
            rs_q = bs.query_history_k_data_plus(
                bs_code, "date,close",
                start_date=START_DATE, end_date=END_DATE,
                frequency="d", adjustflag="2"
            )
            data_q = []
            while rs_q.error_code == '0' and rs_q.next():
                data_q.append(rs_q.get_row_data())
            # 不复权
            rs_r = bs.query_history_k_data_plus(
                bs_code, "date,close",
                start_date=START_DATE, end_date=END_DATE,
                frequency="d", adjustflag="3"
            )
            data_r = []
            while rs_r.error_code == '0' and rs_r.next():
                data_r.append(rs_r.get_row_data())

            if not data_q or not data_r:
                last_err = "empty"
                time.sleep(2 ** attempt)
                continue

            import pandas as pd
            df_q = pd.DataFrame(data_q, columns=['date', 'qfq_close'])
            df_r = pd.DataFrame(data_r, columns=['date', 'raw_close'])
            merged = df_q.merge(df_r, on='date')
            if len(merged) == 0:
                last_err = "no overlap"
                time.sleep(2 ** attempt)
                continue
            # 过滤 '-' / 无效值
            merged['qfq_close'] = pd.to_numeric(merged['qfq_close'], errors='coerce')
            merged['raw_close'] = pd.to_numeric(merged['raw_close'], errors='coerce')
            merged = merged.dropna(subset=['qfq_close', 'raw_close'])
            merged = merged[merged['raw_close'] > 0]
            if len(merged) == 0:
                last_err = "all invalid"
                time.sleep(2 ** attempt)
                continue

            merged['adj_factor'] = (merged['qfq_close'] / merged['raw_close']).round(6)
            merged['code'] = code_short
            merged = merged[['code', 'date', 'adj_factor', 'qfq_close', 'raw_close']]
            merged.to_parquet(cache_fp, index=False)
            return 'ok'
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(2 ** attempt + 1)
    return f'fail:{last_err}'


def main():
    LOG.unlink(missing_ok=True)
    log("=== 方案 B 启动 (baostock 版) ===")

    import baostock as bs
    lg = bs.login()
    log(f"baostock 登录: {lg.error_msg}")
    if lg.error_code != '0':
        log("❌ 登录失败")
        sys.exit(1)

    with open(ALL_STOCKS) as _f:
        name_map = json.load(_f)
    codes = sorted(name_map.keys())
    log(f"A 股总数: {len(codes)}, 时间窗口: {START_DATE} ~ {END_DATE}")

    cached_n = sum(1 for c in codes if (OUT_DIR / f'{c}.parquet').exists())
    log(f"已缓存: {cached_n} / {len(codes)}")

    ok, fail, cached_n = 0, 0, 0
    t0 = time.time()
    fail_codes = []
    for i, code in enumerate(codes):
        cache_fp = OUT_DIR / f'{code}.parquet'
        if cache_fp.exists():
            cached_n += 1
            continue

        # 跳过北交所 920XXX（baostock 无数据，会卡 retry）
        if code.startswith('920'):
            log(f'  [{i+1}/{len(codes)}] skip {code} (北交所 920XXX baostock 无数据)')
            continue

        print(f'  [{i+1}/{len(codes)}] trying {code}...', flush=True)
        result = fetch_one(bs, code)
        if result == 'ok':
            ok += 1
        elif result == 'cached':
            cached_n += 1
        else:
            fail += 1
            fail_codes.append((code, result))

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            speed = (i + 1 - cached_n) / max(elapsed, 1)
            remaining = len(codes) - i - 1
            eta_min = remaining / max(speed, 0.01) / 60
            log(f"  [{i+1}/{len(codes)}] ok={ok} fail={fail} cached={cached_n} "
                f"speed={speed:.2f}只/s ETA={eta_min:.0f}min")

        # 限速：每只 0.05s（baostock 比较快）
        time.sleep(0.05)

    log(f"\n=== 完成 === ok={ok} fail={fail} cached={cached_n}  耗时={(time.time()-t0)/60:.1f}min")
    if fail_codes:
        log("失败列表（前 30）:")
        for code, err in fail_codes[:30]:
            log(f"  {code}: {err}")

    bs.logout()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        with LOG.open('a', encoding='utf-8') as f:
            f.write(f"[FATAL] {traceback.format_exc()}\n")
        raise
