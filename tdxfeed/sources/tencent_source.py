"""腾讯行情数据源（凌晨稳定，回补用）：web.ifzq.gtimg.cn 日K接口。

注意：腾讯接口不含成交额(amount)字段，amount 列填 0 并在写盘时标记 source=tencent。
返回行格式: [date, open, close, high, low, volume(股)]，volume 需 ÷100 转手。
【v2 修复】OHLC 解析改为按列索引取值（原版本误用 float(row) 整行）。
"""
import json
import time
import urllib.request

from tdxfeed.sources.base import DataSource


class TencentSource(DataSource):
    def fetch_bars(self, symbol, start_date=None, end_date=None):
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
               '{},day,{},{},320,qfq').format(
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
                    # 腾讯列序: [date, open, close, high, low, volume(股)]
                    bars.append([
                        self.normalize_date(str(row[0]).replace('-', '')),
                        float(row[1]),   # open
                        float(row[3]),   # high
                        float(row[4]),   # low
                        float(row[2]),   # close
                        float(row[5]) / 100.0,  # 股→手
                        0.0,             # 腾讯无成交额
                    ])
                return bars
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError('腾讯源失败: ' + str(last_err))
