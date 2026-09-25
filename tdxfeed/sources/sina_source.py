"""新浪行情数据源（白天稳定，第一主力）：quotes.sina.cn getKLineData。

- 日K: scale=240，最大 datalen=1023（约4年）
- 5分钟: scale=5，且含 amount(成交额) 字段
- 返回 JSONP: var _x=([{day,open,high,low,close,volume(股)[,amount(元)]}]);
- volume 单位=股，÷100 转手；日K 无 amount 时填 0
- 全类型可用：A股(sh600000/sz000001)/指数(sh000001/sz399001)/ETF(sh510300)/可转债(sh113001)
"""
import json
import re
import time
import urllib.request

from tdxfeed.sources.base import DataSource


class SinaSource(DataSource):
    def fetch_bars(self, symbol, start_date=None, end_date=None,
                   scale=240, datalen=1023):
        url = ('https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_s=/'
               f'CN_MarketDataService.getKLineData?symbol={symbol}&scale={scale}'
               f'&ma=no&datalen={datalen}')
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Mozilla/5.0'})
        last_err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    text = r.read().decode('utf-8')
                m = re.search(r'\((\[.*\])\)', text, re.S)
                if not m:
                    raise RuntimeError('Sina 返回格式异常')
                rows = json.loads(m.group(1))
                if not rows:
                    raise RuntimeError('Sina 返回空数据')
                bars = []
                for row in rows:
                    day = str(row['day']).replace('-', '').replace(' ', '').replace(':', '')[:8][:8]
                    bars.append([
                        self.normalize_date(day),
                        float(row['open']),
                        float(row['high']),
                        float(row['low']),
                        float(row['close']),
                        float(row['volume']) / 100.0,  # 股→手
                        float(row.get('amount') or 0),  # 5分钟有，日K无
                    ])
                return bars
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError('Sina源失败: ' + str(last_err))
