"""腾讯行情数据源（凌晨稳定，回补用）：proxy.finance.qq.com 的 newfqkline 日K接口。

域名/路径选这台而不是 gtimg：2026-10-26 审计并发把 ifzq 与 web.ifzq 两台 gtimg 都打进
腾讯 WAF 的 HTTP 501（server: stgw），而 proxy.finance.qq.com/ifzqgtimg 同 param 回 200、
首行末行与 count 语义完全一致（akshare 的腾讯封装也走这台）。

注意：腾讯接口不含成交额(amount)字段，amount 列填 0 并在写盘时标记 source=tencent。
这台每行末尾还多几列（{},'换手','成交额(万元)'…），列 0-5 与 gtimg 相同，本适配器只取 0-5。
源列序 [date, open, close, high, low, volume]。主板/创业板 volume 是手，直取；
科创板（sh688/sh689）三台入口都回股，按代码折算（tdxfeed/volume_units.py）。
Sina 吐股是另一回事（sina_source.py:45），别把两条规则混在一起。
【v2 修复】OHLC 解析改为按列索引取值（原版本误用 float(row) 整行）。
"""
import json
import time
import urllib.request

from tdxfeed.sources.base import DataSource
from tdxfeed.volume_units import to_hands


class TencentSource(DataSource):
    def fetch_bars(self, symbol, start_date=None, end_date=None):
        url = ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
               '?param={},day,{},{},320,qfq').format(
                   symbol, start_date or '', end_date or '')
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0'})
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode('utf-8'))
                d = data.get('data', {})
                node = d.get(symbol, {})
                rows = node.get('qfqday') or node.get('day') or []
                if not rows:
                    for v in d.values():
                        rows = v.get('qfqday') or v.get('day') or []
                        if rows:
                            break
                if not rows:
                    raise RuntimeError('腾讯返回空数据')
                bars = []
                for row in rows:
                    # 腾讯日K 列序: [date, open, close, high, low, volume]
                    bars.append([
                        self.normalize_date(str(row[0]).replace('-', '')),
                        float(row[1]),   # open
                        float(row[3]),   # high
                        float(row[4]),   # low
                        float(row[2]),   # close
                        to_hands(symbol, float(row[5])),   # 科创板源=股，其余=手
                        0.0,             # 腾讯无成交额，本适配器一律填 0，不猜
                    ])
                return bars
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError('腾讯源失败: ' + str(last_err))
