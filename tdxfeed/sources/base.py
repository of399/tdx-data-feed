from abc import ABC, abstractmethod
from datetime import datetime


class DataSource(ABC):
    """数据源统一接口：返回归一化 bars。

    每根 bar 为列表 [date, open, high, low, close, volume(手), amount(元)]。
    """

    @abstractmethod
    def fetch_bars(self, symbol, start_date=None, end_date=None):
        pass

    @staticmethod
    def normalize_date(d):
        if isinstance(d, str):
            return datetime.strptime(d, "%Y%m%d")
        return d
