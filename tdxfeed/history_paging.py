"""按 end 日期向前翻腾讯日K，绕开单次条数上限——分页截断的正解。

背景（docs/superpowers/plans/2026-10-25-daily-history-2000-backfill.md §0，2026-10-26 复核）：
- 腾讯 fqkline 没有 offset 参数，count 语义是「区间内最近 N 根」：末行恒为区间末日，
  首行随 count 增大而左移，所以只能靠回移 end 翻页；
- count=2000 可用、2100 起服务端回 `msg='param error'` 且 `data` 退化成 []；
- Sina 只有 datalen，>1023 返回空；东财 push2his 与通达信公共服务器在本机不可用。

「没有更早」和「请求被拒」必须分开判：前者是 `msg=''` + `day=[]`，后者是
`data` 不是 dict。把后者当成历史尽头，就是静默截断的成因本身。
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

# 上限由服务端参数校验决定，不是区间里的自然日数：2000 可用、2100 被拒，
# 取 1024 留一半余量（服务端哪天收紧阈值也不至于立刻变成静默空返回）。
PAGE_CAP = 1024
# 返回键随复权类型不同（§0 第 8 条）。既有适配器取 `qfqday or day`，在最新一段会
# 「请求复权却拿到未复权」而无人报错，所以这里未知类型走 KeyError，不做静默回落。
TENCENT_KEYS = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}
FIRST_DAY = "1990-01-01"  # 深取固定左端：翻页只动 end
# 同路径同载荷的两个域。首选镜像：2026-10-26 4 并发跑到 ~3400 请求时
# web.ifzq 被腾讯 WAF 按 IP 拦成 HTTP 501（server: stgw，body 跳 waf.tencent.com），
# 当时 ifzq 同参数仍返回 1999-11-10 起的全深度行。
HOSTS = ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn")
BASE = ("https://{host}/appstock/app/fqkline/get"
        "?param={sym},day,{start},{end},{count},{adjust}")
# 501/403/429 是 IP 级风控信号，不是抖动：秒级重试等于继续砸，会把临时拦截拖成
# 长时间封禁。退避到分钟级，让整轮审计的失败面收敛在少数代码上。
WAF_CODES = (401, 403, 429, 501, 503)
WAF_COOLDOWN = 30.0


class EmptyPageError(RuntimeError):
    """首页即空：该代码在 1990→今天整段无行，多半代码无效，不是历史尽头。"""


class PageRequestError(RuntimeError):
    """请求被服务端拒（count 超限 / 代码形状非法）：`msg='param error'`、`data` 不是 dict。"""


def http_page(sym: str, end: str, count: int, adjust: str = "") -> list:
    """取一页原始行；只有网络/解析异常重试，param error 立刻抛出。

    每台域各试一次：撞风控（WAF_CODES）退避 WAF_COOLDOWN 后换下一台，
    普通网络抖动仍按秒级重试。
    """
    key = TENCENT_KEYS[adjust]
    last = None
    for attempt in range(3):
        for host in HOSTS:
            url = BASE.format(host=host, sym=sym, start=FIRST_DAY,
                              end=end, count=count, adjust=adjust)
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                        timeout=20) as r:
                    payload = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
                time.sleep(WAF_COOLDOWN if getattr(e, "code", None) in WAF_CODES
                           else 2 * (attempt + 1))
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                raise PageRequestError(
                    f"{sym} 请求被拒 end={end} count={count} msg={payload.get('msg')!r}"
                    "（count 超限是 param error，不是历史尽头）")
            node = data.get(sym) or {}
            if not isinstance(node, dict):
                raise PageRequestError(
                    f"{sym} 响应节点形状异常 end={end}: {type(node).__name__}")
            return node.get(key) or []
    raise RuntimeError(f"腾讯页请求失败 {sym} end={end}: {last!s}")


def _to_bar(row: list) -> list:
    """源行 [date, open, close, high, low, volume(手)] → bar 契约（date 为 int YYYYMMDD）。"""
    return [int(str(row[0]).replace("-", "")),
            float(row[1]), float(row[3]), float(row[4]), float(row[2]),
            float(row[5]), 0.0]


def _default_page(sym: str, end: str, count: int, adjust: str = ""):
    raw = http_page(sym, end, count, adjust)
    return [_to_bar(r) for r in raw], raw


def fetch_history(symbol: str, want_from: str, *, fetch_page=None,
                  page_cap: int = PAGE_CAP, adjust: str = "",
                  sleep: float = 0.15) -> list[list]:
    """翻到 `want_from`（'YYYY-MM-DD'）之前或源尽头为止，返回升序去重 bars。

    行形状同 DataSource：[date:int YYYYMMDD, open, high, low, close, volume(手), amount(元)]；
    amount 恒为 0（腾讯日K 不吐成交额），落盘后由 tdxfeed/amount_fix.py 口径补。
    """
    fetch = fetch_page if fetch_page is not None else _default_page
    want = int(str(want_from).replace("-", ""))
    out: dict[int, list] = {}
    end = dt.date.today()
    page_no = 0
    while True:
        page, _raw = fetch(symbol, end.isoformat(), page_cap, adjust)
        page_no += 1
        if not page:
            if page_no == 1:
                raise EmptyPageError(
                    f"{symbol} 首页即空 end={end} count={page_cap}："
                    "1990 至今整段无行，多半是代码无效")
            break                       # 后续页空 = 源已到尽头，正常结束
        for bar in page:
            out[bar[0]] = bar
        first = min(b[0] for b in page)
        if len(page) < page_cap:        # 不足一页 = 区间已取完 = 源尽头
            break
        if first < want:                # 已翻到目标起点之前
            break
        end = dt.datetime.strptime(str(first), "%Y%m%d").date() - dt.timedelta(days=1)
        time.sleep(sleep)
    return sorted(out.values(), key=lambda b: b[0])
