"""pytdx 数据源（best-effort：TDX 服务器不可达时由 akshare 兜底）。"""
from tdxfeed.client import TdxClient
from tdxfeed.sources.base import DataSource


class PytdxSource(DataSource):
    def __init__(self):
        self.client = TdxClient()

    def fetch_bars(self, symbol, start_date=None, end_date=None):
        code = symbol[-6:]
        market = 1 if code.startswith(("60", "68")) else 0
        self.client.connect()
        bars = []
        start = 0
        while True:
            chunk = self.client.fetch_with_retry(
                self.client.api.get_security_bars,
                4, market, code, start, 800,
            )
            if not chunk or not chunk.get("bars"):
                break
            for b in chunk["bars"]:
                dt = b.get("datetime", "")
                if isinstance(dt, str):
                    dt = dt[:8]
                bars.append([
                    self.normalize_date(dt),
                    b["open"], b["high"], b["low"], b["close"],
                    b["vol"], b["amount"],
                ])
            if len(chunk["bars"]) < 800:
                break
            start += 800
        return bars
