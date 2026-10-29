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


def test_star_volume_shares_converted_to_hands(fake_urlopen):
    """科创板是例外：三台入口都回股（688981 当日 28098839），档案口径是手。

    证据除与本地 248298 手对不上 100 倍外，行内“成交额(万元)”列也自证：
    vol×close/1e4 = 286608 万元 ≈ 源列 285079 万元（股口径才对得上；
    按手口径会差 100 倍）。折算不留在这里，整段深取就会在同一只票里造出断层。
    """
    fake_urlopen({"data": {"sh688981": {"day": DAY_ROWS_STAR}}})
    bars = TencentSource().fetch_bars("sh688981", "20261023", "20261023")
    assert bars[0][5] == pytest.approx(280988.39, rel=1e-6)  # 股 ÷ 100 → 手
    assert Writers._date_int(bars[0][0]) == 20261023


DAY_ROWS_STAR = [
    ["2026-10-23", "102.000", "101.000", "102.500", "100.500", "28098839.000"],
]


def test_merge_write_unit_warning_fires_on_100x(tmp_path, capsys):
    """绊线自证：旧/新中位相差 100 倍必须告警。

    原实现的 ratio 写成 `nm/om if om > nm else om/nm`，两分支都 ≤1，永远触发不了；
    科创板深取把 100 倍断层写进 689009/688511 时它一声没响。
    """
    w = Writers(str(tmp_path))
    w.merge_write("sh600000", [
        [20260101 + i, 10, 11, 9, 10.5, 100000.0, 1e8] for i in range(1, 4)])
    capsys.readouterr()  # 第一笔没有旧文件，不该有告警
    w.merge_write("sh600000", [
        [20260101 + i, 10, 11, 9, 10.5, 10000000.0, 1e8] for i in range(1, 4)])
    out = capsys.readouterr().out
    assert "单位告警" in out, "100 倍单位差必须触发告警"
