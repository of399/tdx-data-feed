"""命令行入口：sync / status / vault-doc 子命令。

用法：
  python -m tdxfeed.cli sync [symbol]   拉取并双写该标的（默认 sh600000）
  python -m tdxfeed.cli status          查看已同步标的与末日期
  python -m tdxfeed.cli vault-doc       生成 StockVault 数据字典与同步状态
"""
import os
import sys
from datetime import datetime

import yaml

from tdxfeed.sources.akshare_source import AkshareSource
from tdxfeed.sources.sina_source import SinaSource
from tdxfeed.sources.tencent_source import TencentSource
from tdxfeed.state import State
from tdxfeed.writers import Writers

CONFIG_PATH = '/home/jiuben/tdx-data-feed/config.yaml'


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f)


def get_out_root():
    cfg = load_config()
    return os.environ.get(
        'TDX_OUT',
        cfg.get('output_root', '/home/jiuben/tdx-data-feed/data'))


def cmd_sync(symbol):
    out_root = get_out_root()
    state = State(os.path.join(out_root, '.state', 'manifest.json'))
    w = Writers(out_root)
    last_err = None
    for SourceCls, name in [(SinaSource, 'sina'), (AkshareSource, 'akshare'), (TencentSource, 'tencent')]:
        try:
            inst = SourceCls()
            bars = inst.fetch_bars(symbol)
            if bars:
                w.write_day(symbol, bars)
                w.write_parquet(symbol, bars)
                last = str(bars[-1][0]).replace('-', '')[:8]
                state.update(symbol, last, len(bars))
                print(f'sync ok [{name}]', symbol, len(bars), '根, 末日期:', last)
                return
        except Exception as e:
            last_err = e
            continue
    print('sync failed:', symbol, '| all sources down:', str(last_err)[:80])


def cmd_status():
    out_root = get_out_root()
    state = State(os.path.join(out_root, '.state', 'manifest.json'))
    print('已同步标的数:', len(state.data))
    for k, v in list(state.data.items()):
        print(' ', k, '末日期:', v.get('last_date'), '条数:', v.get('count'))



def cmd_full():
    """Clear manifest for full resync. Plan section 7."""
    out_root = get_out_root()
    state_path = os.path.join(out_root, '.state', 'manifest.json')
    if os.path.exists(state_path):
        os.remove(state_path)
        print(f'cleared manifest: {state_path}')
    else:
        print('manifest not found (nothing to clear)')
    print('--full mode: main path = python tdxfeed/full_sync.py (v2.1)')



def cmd_verify(symbol=None):
    """Reconcile .day vs parquet. Plan section 10.2."""
    out_root = get_out_root()
    vipdoc_sh = os.path.join(out_root, 'sh', 'lday')
    vipdoc_sz = os.path.join(out_root, 'sz', 'lday')
    parquet_dir = os.path.join(out_root, 'parquet', 'daily')

    def list_day_files():
        files = []
        for d in [vipdoc_sh, vipdoc_sz]:
            if os.path.isdir(d):
                files.extend([os.path.join(d, f) for f in os.listdir(d) if f.endswith('.day')])
        return files

    def list_parquet_files():
        if os.path.isdir(parquet_dir):
            return [os.path.join(parquet_dir, f) for f in os.listdir(parquet_dir) if f.endswith('.parquet')]
        return []

    if symbol:
        m = symbol.lower()
        day_file = os.path.join(vipdoc_sh if m.startswith('sh') else vipdoc_sz, f'{m}.day')
        pq_file = os.path.join(parquet_dir, f'{m}.parquet')
        day_ok = os.path.exists(day_file)
        pq_ok = os.path.exists(pq_file)
        day_size = os.path.getsize(day_file) // 32 if day_ok else 0
        print(f'{symbol}: .day={"OK "+str(day_size)+" bars" if day_ok else "MISSING"} | parquet={"OK" if pq_ok else "MISSING"}')
        return

    days = list_day_files()
    pqs = list_parquet_files()
    day_codes = set(os.path.basename(f).replace('.day', '') for f in days)
    pq_codes = set(os.path.basename(f).replace('.parquet', '') for f in pqs)
    only_day = day_codes - pq_codes
    only_pq = pq_codes - day_codes
    both = day_codes & pq_codes
    print('Reconciliation:')
    print(f'  .day files:   {len(day_codes)}')
    print(f'  parquet:      {len(pq_codes)}')
    print(f'  intersection: {len(both)}')
    if only_day:
        print(f'  WARN only .day no parquet: {len(only_day)} (eg: {sorted(only_day)[:5]})')
    if only_pq:
        print(f'  WARN only parquet no .day: {len(only_pq)} (eg: {sorted(only_pq)[:5]})')
    if not only_day and not only_pq:
        print('  OK: .day and parquet fully reconciled')


def cmd_vault_doc():
    cfg = load_config()
    vault = cfg.get('obsidian_vault', '/home/jiuben/StockVault')
    out_root = cfg.get('output_root', '/home/jiuben/tdx-data-feed/data')
    state = State(os.path.join(out_root, '.state', 'manifest.json'))
    data_dir = os.path.join(vault, '03-数据')
    os.makedirs(data_dir, exist_ok=True)

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = ''.join(
        '| {} | {} | {} |\n'.format(k, v.get('last_date', '-'),
                                       v.get('count', '-'))
        for k, v in state.data.items())
    dict_doc = f'''# 数据字典（自动生成）

> 生成时间：{now} ｜ 来源：tdx-data-feed 数据管道 ｜ 勿手改，由 `cli vault-doc` 覆盖

## 字段口径

| 字段 | 单位 | 说明 |
|---|---|---|
| date | YYYYMMDD | 交易日 |
| open/high/low/close | 元 | 前复权价（qfq），后复权（hfq）阶段二补充 |
| volume | 手 | 1 手 = 100 股（.day 标准单位） |
| amount | 元 | 成交额（腾讯源为 0，待 akshare 补齐） |
| source | - | pytdx / akshare / tencent |

## 目录布局

- {out_root}/sh/lday/*.day 通达信兼容日K（32字节/条）
- {out_root}/sz/lday/*.day
- {out_root}/parquet/daily/*.parquet 通用表（v4 原料）
- {out_root}/.state/manifest.json 增量状态

## 更新状态

- 已同步标的：{len(state.data)}
- 复权口径：前复权 qfq（当前）；后复权 hfq（阶段二）
'''
    st_doc = f'''# 同步状态（自动生成）

> 生成时间：{now}

## 最近同步摘要

| 标的 | 末日期 | 条数 |
|---|---|---|
{rows}
## 说明

- 缺失/失败标的将在此列出（当前为空）
- 数据源占比与字段口径见数据字典
'''
    p1 = os.path.join(data_dir, '数据字典.md')
    p2 = os.path.join(data_dir, '同步状态.md')
    with open(p1, 'w', encoding='utf-8') as f:
        f.write(dict_doc)
    with open(p2, 'w', encoding='utf-8') as f:
        f.write(st_doc)
    print('vault-doc 已生成:')
    print(' ', p1)
    print(' ', p2)


if __name__ == '__main__':
    args = sys.argv[1:]
    cmd = args[0] if args else 'status'
    if cmd == 'sync':
        cmd_sync(args[1] if len(args) > 1 else 'sh600000')
    elif cmd == 'status':
        cmd_status()
    elif cmd == 'vault-doc':
        cmd_vault_doc()
    elif cmd == 'full':
        cmd_full()
    elif cmd == 'verify':
        cmd_verify(args[1] if len(args) > 1 else None)
    else:
        print('未知命令:', cmd, '（支持 sync / full / status / verify / vault-doc）')
