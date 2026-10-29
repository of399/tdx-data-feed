"""按 end 日期向前翻腾讯日K，绕开单次条数上限——分页截断的正解。

背景（docs/superpowers/plans/2026-10-25-daily-history-2000-backfill.md §0，2026-10-26 复核）：
- 腾讯 fqkline 没有 offset 参数，count 语义是「区间内最近 N 根」：末行恒为区间末日，
  首行随 count 增大而左移，所以只能靠回移 end 翻页；
- count=2000 可用、2100 起服务端回 `msg='param error'` 且 `data` 退化成 []；
- **复权序列另有更小的顶**：hfq/qfq 请求 1024 或 2000 都只回 640 行（见
  PAGE_CAP_BY_ADJUST），满页判据必须分档，否则复权序列会被截在最近 640 天；
- Sina 只有 datalen，>1023 返回空；东财 push2his 与通达信公共服务器在本机不可用。

「没有更早」和「请求被拒」必须分开判：前者是 `msg=''` + `day=[]`，后者是
`data` 不是 dict。把后者当成历史尽头，就是静默截断的成因本身。
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

from tdxfeed.volume_units import to_hands

# 上限由服务端参数校验决定，不是区间里的自然日数：2000 可用、2100 被拒，
# 取 1024 留一半余量（服务端哪天收紧阈值也不至于立刻变成静默空返回）。
PAGE_CAP = 1024
# 满页行数按复权类型分档：2026-10-26 实测 proxy 域，未复权 count=1024 回满 1024 行，
# 而 hfq/qfq 无论 count=1024 还是 2000 都只回 640 行（count=500 回 500，说明 640 是
# 服务端对复权序列的硬顶）。混用一档的后果是复权序列被截到最近 640 天还自认翻完。
PAGE_CAP_BY_ADJUST = {"": PAGE_CAP, "qfq": 640, "hfq": 640}
# 返回键随复权类型不同（§0 第 8 条）。既有适配器取 `qfqday or day`，在最新一段会
# 「请求复权却拿到未复权」而无人报错，所以这里未知类型走 KeyError，不做静默回落。
TENCENT_KEYS = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}
FIRST_DAY = "1990-01-01"  # 深取固定左端：翻页只动 end
# 三台同载荷的入口，按「实测当下可用」排序，不是按名气排序：
#   2026-10-26 4 并发跑到 ~3400 请求（≈6.4 req/s）后 ifzq 与 web.ifzq 双双被腾讯 WAF
#   按 IP 拦成 HTTP 501（server: stgw，body 跳 waf.tencent.com/501page.html），
#   而 akshare 用的 proxy.finance.qq.com/newfqkline 同 param 回 200，首行同样是
#   1999-11-10、末行同样是区间末日 ⇒ count/end 语义一致，可直接换。
# 健康的那台放前面：被封时每台要付 30s 退避，把两台已死的排前面等于每只白扔一分钟。
ENDPOINTS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    "?param={sym},day,{start},{end},{count},{adjust}",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={sym},day,{start},{end},{count},{adjust}",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={sym},day,{start},{end},{count},{adjust}",
)
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

    每台入口各试一次：撞风控（WAF_CODES）退避 WAF_COOLDOWN 后换下一台，
    普通网络抖动仍按秒级重试。
    """
    key = TENCENT_KEYS[adjust]
    last = None
    for attempt in range(3):
        for tpl in ENDPOINTS:
            url = tpl.format(sym=sym, start=FIRST_DAY,
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


def _to_bar(row: list, sym: str = "") -> list:
    """源行 [date, open, close, high, low, volume] → bar 契约（date 为 int YYYYMMDD, volume 手）。

    科创板源 volume 是股（见 tdxfeed/volume_units.py），必须按代码折算。
    """
    return [int(str(row[0]).replace("-", "")),
            float(row[1]), float(row[3]), float(row[4]), float(row[2]),
            to_hands(sym, float(row[5])), 0.0]


def _default_page(sym: str, end: str, count: int, adjust: str = ""):
    raw = http_page(sym, end, count, adjust)
    return [_to_bar(r, sym) for r in raw], raw


def fetch_history(symbol: str, want_from: str, *, fetch_page=None,
                  page_cap: int | None = None, adjust: str = "",
                  sleep: float = 0.15) -> list[list]:
    """翻到 `want_from`（'YYYY-MM-DD'）之前或源尽头为止，返回升序去重 bars。

    行形状同 DataSource：[date:int YYYYMMDD, open, high, low, close, volume(手), amount(元)]；
    amount 恒为 0（腾讯日K 不吐成交额）。写盘层要不要给它补估算值，不归本模块管，
    也不要在本文件里假设那条链路存在。

    page_cap 缺省时按复权类型取各自的满页行数：实测复权序列 count 给到 1024/2000 也只回
    640 行，用未复权的 1024 当判据会把复权序列截在最近 640 天却报「翻完了」。
    """
    fetch = fetch_page if fetch_page is not None else _default_page
    cap = PAGE_CAP_BY_ADJUST[adjust] if page_cap is None else page_cap
    want = int(str(want_from).replace("-", ""))
    out: dict[int, list] = {}
    end = dt.date.today()
    page_no = 0
    prev_first = None
    while True:
        page, _raw = fetch(symbol, end.isoformat(), cap, adjust)
        page_no += 1
        if not page:
            if page_no == 1:
                raise EmptyPageError(
                    f"{symbol} 首页即空 end={end} count={cap}："
                    "1990 至今整段无行，多半是代码无效")
            break                       # 后续页空 = 源已到尽头，正常结束
        for bar in page:
            out[bar[0]] = bar
        first = min(b[0] for b in page)
        if prev_first is not None and first >= prev_first:
            break                       # 翻页不再左移 = 源不给更多，别再要同一批行
        prev_first = first
        if len(page) < cap:             # 不足一页 = 区间已取完 = 源尽头
            break
        if first < want:                # 已翻到目标起点之前
            break
        end = dt.datetime.strptime(str(first), "%Y%m%d").date() - dt.timedelta(days=1)
        time.sleep(sleep)
    return sorted(out.values(), key=lambda b: b[0])
