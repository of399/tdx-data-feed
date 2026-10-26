"""腾讯日K 的 volume 单位=手，适配器不能再 ÷100（Sina 才吐股）。

真实依据（2026-10-26 实测）：sz000021 在 2026-10-23 源返回 665445.000，
库里同日 volume=665444.67（同为手）。按旧实现会写成 6654.45，
只有 writers.py:147 那条 >50× 的单位告警会响，且不阻断。
"""
import json

import pytest

from tdxfeed.sources.tencent_source import TencentSource
from tdxfeed.writers import Writers

DAY_ROWS = [
    ["2026-10-23", "27.780", "29.190", "29.280", "26.850", "665445.000"],
]


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_urlopen(monkeypatch):
    import tdxfeed.sources.tencent_source as mod

    def _install(payload):
        monkeypatch.setattr(
            mod.urllib.request, "urlopen",
            lambda req, timeout=None: _Resp(payload))
    return _install


def test_daily_volume_is_taken_as_hands_not_shares(fake_urlopen):
    fake_urlopen({"data": {"sz000021": {"day": DAY_ROWS}}})
    bars = TencentSource().fetch_bars("sz000021", "20261023", "20261023")
    # normalize_date 交回 datetime，落盘由 Writers._date_int 归一
    assert Writers._date_int(bars[0][0]) == 20261023
    # 源列序 [date, open, close, high, low, volume]，适配器重排成 bar 契约
    assert bars[0][1] == pytest.approx(27.78)    # open
    assert bars[0][2] == pytest.approx(29.28)    # high
    assert bars[0][3] == pytest.approx(26.85)    # low
    assert bars[0][4] == pytest.approx(29.19)    # close
    assert bars[0][5] == pytest.approx(665445.0, rel=1e-6)  # 手，不 ÷100
