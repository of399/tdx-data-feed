"""腾讯 fqkline 没有偏移量参数，只能按 end 日期向前翻页；超限不是静默空行。

2026-10-26 实测边界：count=1024/2000 正常返回（首行随 count 左移、末行恒为区间末日），
count=2100 起回 `msg='param error'` 且 `data` 退化成 []；而区间早于上市时是
`code=0 / msg='' / day=[]`。「请求被拒」和「这段确实没数据」在响应里是可分的，
分页器必须按这个区分判停——把超限当成历史尽头，就是静默截断的成因本身。
"""
import json

import pytest

from tdxfeed.history_paging import (
    PAGE_CAP,
    EmptyPageError,
    PageRequestError,
    fetch_history,
)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _bar(iso, vol="100"):
    """腾讯源行 → 适配器行 [date:int, open, high, low, close, volume(手), amount]。"""
    return [int(iso.replace("-", "")), 10.0, 12.0, 9.0, 11.0, float(vol), 0.0]


def page_factory(pages):
    """按请求顺序吐页；pages 元素是 iso 日期列表。返回 (fetch_page, 请求记录)。"""
    seen = []

    def fetch_page(symbol, end_date, count, adjust=""):
        seen.append((end_date, count))
        if len(seen) > len(pages):
            return [], []
        return [_bar(d) for d in pages[len(seen) - 1]], []
    return fetch_page, seen


def test_pages_backwards_by_end_date_until_start_reached():
    p1 = ["2003-01-02", "2003-06-30"]
    p2 = ["1999-11-10", "2002-12-31"]
    fetch_page, seen = page_factory([p1, p2])
    bars = fetch_history("sz000021", "2000-01-01", fetch_page=fetch_page, page_cap=2)
    dates = [b[0] for b in bars]
    assert dates == sorted(dates), "必须升序"
    assert min(dates) == 19991110, "第二页翻到 want_from 之前才停"
    assert len(seen) == 2, "两页够了就该停：不该无限往前翻"
    assert seen[1][0] < seen[0][0], "下一页的 end 必须比上一页更早"


def test_empty_first_page_raises_instead_of_claiming_full_history():
    """首页就空 = 这只票在 1990→今天整段无数据，不是「历史已翻完」。"""
    fetch_page, _ = page_factory([[]])
    with pytest.raises(EmptyPageError):
        fetch_history("sz000021", "2000-01-01", fetch_page=fetch_page, page_cap=2)


def test_later_empty_page_means_source_exhausted_not_error():
    fetch_page, seen = page_factory([["2003-01-02", "2003-06-30"], []])
    bars = fetch_history("sz000021", "1995-01-01", fetch_page=fetch_page, page_cap=2)
    assert [b[0] for b in bars] == [20030102, 20030630]
    assert len(seen) == 2, "后续页空应停，不该抛错"


def test_dedupes_overlapping_rows_across_pages():
    fetch_page, _ = page_factory([["2003-01-02"], ["2003-01-02"], []])
    bars = fetch_history("sz000021", "1995-01-01", fetch_page=fetch_page, page_cap=1)
    assert [b[0] for b in bars].count(20030102) == 1


def test_request_rejected_page_is_not_history_end():
    """count 超限的 param error 必须抛出来，不能被当成「翻到头了」。"""
    def boom(symbol, end_date, count, adjust=""):
        raise PageRequestError("param error")

    with pytest.raises(PageRequestError):
        fetch_history("sz000021", "2000-01-01", fetch_page=boom, page_cap=99999)


def test_caps_page_size_below_source_limit():
    assert PAGE_CAP <= 2000, "实测 count=2000 可用、2100 起 param error"


def test_adjust_selects_the_matching_response_key(monkeypatch):
    """§0 第 8 条：'' → day，qfq → qfqday，hfq → hfqday；混键会「请求复权却拿到未复权」。"""
    import tdxfeed.history_paging as hp

    payload = {"data": {"sz000021": {
        "day": [["2000-01-04", "1", "1", "1", "1", "1"]],
        "qfqday": [["2000-01-04", "2", "2", "2", "2", "2"]],
        "hfqday": [["2000-01-04", "9", "9", "9", "9", "9"]],
    }}}
    monkeypatch.setattr(hp.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(payload))
    assert hp.http_page("sz000021", "2005-12-31", 100, "hfq")[0][1] == "9"
    assert hp.http_page("sz000021", "2005-12-31", 100, "qfq")[0][1] == "2"
    assert hp.http_page("sz000021", "2005-12-31", 100, "")[0][1] == "1"
    with pytest.raises(KeyError):
        hp.http_page("sz000021", "2005-12-31", 100, "nonsense")


def test_param_error_response_raises_request_error(monkeypatch):
    """实测超限形状：data 退化成 []，msg='param error'。"""
    import tdxfeed.history_paging as hp

    monkeypatch.setattr(hp.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(
                            {"code": 0, "msg": "param error", "data": []}))
    with pytest.raises(PageRequestError):
        hp.http_page("sz000021", "2005-12-31", 2100, "")


def test_empty_range_is_not_a_request_error(monkeypatch):
    """区间早于上市：code=0/msg=''/day=[] 是合法空页，原样返回空列表。"""
    import tdxfeed.history_paging as hp

    monkeypatch.setattr(hp.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(
                            {"code": 0, "msg": "", "data": {"sz000021": {"day": []}}}))
    assert hp.http_page("sz000021", "1990-12-31", 300, "") == []


def test_blocked_host_falls_back_to_the_next_mirror(monkeypatch):
    """2026-10-26 实测：4 并发打满后 web.ifzq 被腾讯 WAF 按 IP 拦成 HTTP 501
    （`server: stgw`，body 是跳 waf.tencent.com/501page.html 的 JS），
    而镜像域 ifzq.gtimg.cn 同路径同载荷仍然可用——换域，不是放弃深取。"""
    import tdxfeed.history_paging as hp

    tried = []

    def fake_urlopen(req, timeout=None):
        tried.append(req.full_url)
        if len(tried) == 1:
            raise _http_error(501, req.full_url)
        return _Resp({"code": 0, "msg": "", "data": {
            "sh600000": {"day": [["1999-11-10", "29.5", "27.75", "29.8", "27.0", "1740850.5"]]}}})

    slept = []
    monkeypatch.setattr(hp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(hp.time, "sleep", lambda s: slept.append(s))
    rows = hp.http_page("sh600000", "2000-12-31", 1024, "")
    assert len(rows) == 1, "第二台域取到了就该正常返回"
    assert len(tried) == 2 and hp.HOSTS[0] in tried[0] and hp.HOSTS[1] in tried[1]
    assert max(slept) >= hp.WAF_COOLDOWN, "撞风控那一下必须退避，不能秒级重试继续砸"


def test_waf_rejection_backs_off_instead_of_hammering(monkeypatch):
    """全部域都被拒时，重试间隔必须整体上移到分钟级：秒级砸 IP 只会延长封禁。"""
    import tdxfeed.history_paging as hp

    monkeypatch.setattr(hp.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(
                            _http_error(501, req.full_url)))
    slept = []
    monkeypatch.setattr(hp.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(RuntimeError):
        hp.http_page("sh600000", "2000-12-31", 1024, "")
    assert slept and min(slept) >= hp.WAF_COOLDOWN


def _http_error(code, url):
    import urllib.error
    return urllib.error.HTTPError(url, code, "Not Implemented", {}, None)
