"""akshare 数据源（可靠主线）：Sina 主源，Eastmoney 回退，带退避重试。"""
import time

import akshare as ak

from tdxfeed.sources.base import DataSource


class AkshareSource(DataSource):
    def _fetch_sina(self, code):
        return ak.stock_zh_a_daily(symbol=code, adjust="qfq")

    def _fetch_em(self, code):
        df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
        return df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount",
        })

    def fetch_bars(self, symbol, start_date=None, end_date=None):
        code = symbol[-6:]
        df = None
        last_err = None
        # Sina 主源：最多 3 轮，退避间隔 2/4/6 秒
        for attempt in range(3):
            try:
                df = self._fetch_sina(code)
                if df is not None and not df.empty and "date" in df.columns:
                    break
                raise RuntimeError("Sina 返回空数据")
            except Exception as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        # Eastmoney 回退：最多 2 轮，退避间隔 3/6 秒
        if df is None or df.empty or "date" not in df.columns:
            df = None
            for attempt in range(2):
                try:
                    df = self._fetch_em(code)
                    if df is not None and not df.empty:
                        break
                except Exception as e:
                    last_err = e
                    time.sleep(3 * (attempt + 1))
        if df is None or df.empty or "date" not in df.columns:
            raise RuntimeError("两个数据源均失败: " + str(last_err)[:120])
        # Sina 源 volume 单位为股，转手（÷100）
        if "volume" in df.columns:
            df["volume"] = df["volume"] / 100.0
        bars = []
        for _, r in df.iterrows():
            date = str(r["date"]).replace("-", "")[:8]
            bars.append([
                self.normalize_date(date),
                float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]),
                float(r["volume"]), float(r["amount"]),
            ])
        return bars
