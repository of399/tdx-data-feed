# 全市场日线扩至 2000-01-01 实施方案

**Goal:** 把 `data/parquet/daily/` 的沪深个股日线补到 2000-01-01 之前（按源能给的最早一日），并让抓取层不再被单次上限静默截断。

**Architecture:** 深取只有一条可用通道（腾讯 `web.ifzq.gtimg.cn` 的 `fqkline`），它按 `end` 日期向前翻页，因此新增一个分页适配器 + 一个只读覆盖度审计 + 一个幂等续跑的回填 runner；写盘全部复用既有 `Writers.merge_write`（它已带「同日 amount 取 max」和「mergesort 去重」两条保护）。复权层不进这条链路，另建独立目录，避免与现存 2020 起的因子互相污染。

**Tech Stack:** Python 3.14（主 `venv/bin/python`，数学栈无关）、`urllib.request`、pandas + pyarrow（date32 v2 schema）、pytest（根 `tests/`）。

**Spec:** 本方案自带 §0 实测前置，无独立设计文档——所有结论都是 2026-10-25 在本机跑出来的数字，执行时若与 §0 冲突，以重跑为准。

## Global constraints

- 解释器：本条链路全部跑主 `venv/bin/python`（只碰 json/pandas/pyarrow/urllib，不碰 sympy，与 venv-math 无关）。
- `data/` 在 gitignore 里：任何就地改写前先做带日期备份（AGENTS.md「风险边界」），1 号任务之后、4 号任务之前必须做一次。
- 提交必须带显式路径，不用 `-A` / `.`；这是共享工作树。
- 门禁：`make lint` / `make typecheck` 只覆盖 `v5/`，本方案代码在 `tdxfeed/` + `scripts/` + `tests/`，所以**测试要显式跑** `venv/bin/pytest tests/<file> -q`；`make lint-all` 退出码恒 0，不能当通过证据。
- 单位口径：`daily` parquet 的 `volume` 列**单位是手**，`amount` 单位是元；写盘必须走 `Writers.merge_write`，不得用 `write_parquet` 覆盖全文件。
- 静默空返回是本方案要防的失败类：源在「请求条数超上限」时返回**空列表而不是报错**，任何一次分页拿到空都必须抛错，不能计成「这只没有更早历史」。
- 成交额补缺口径沿用现有 `tdxfeed/amount_fix.py:13` 的 `amount = close × volume(手) × 100`，只补 `amount<=0 且 volume>0`，停牌日（volume=0）保持 0。

## 非目标（明确不做，需要另立项）

1. **退市/历史上市名单**：`data/all_a_stocks.json` 只有 5221 只现存代码，日线目录里不在册的仅 7 只。不补历史名单，2000 段就是「活到今天的股票回头看 26 年」，幸存者偏差不会因数据变长而消失。
2. **涨跌停规则分段**：`tdxfeed/daban/limit_price.py:67` 只有一个常量 `MAIN_ST_RULE_CHANGE_DATE = date(2026, 7, 6)`。把打板特征推到 2000 之前，必须先补 1996-12-16 涨跌停引入、ST ±5%、2019 科创 ±20%、2020 创业板 ±20%、2021 北交所 ±30% 的分段，否则是拿今天的规则判历史。
3. 指数/ETF/可转债的深取（本环境通达信公共服务器不吐数据，见 §0 第 4 条）。

## §0 实测前置（2026-10-25 本机）

| # | 结论 | 复跑命令 |
|---|---|---|
| 1 | 现状：有效个股文件 5236，另含 5232 个 `.v1.bak.parquet` 迁移备份混在同目录；总行数 ~33.4M。起始 ≤2000 的文件 1718 个，起始年直方图紧贴真实 IPO 波峰（1990-93:272，1996-99:696，2000:228） | `venv/bin/python -c "import glob,os;fs=[f for f in glob.glob('data/parquet/daily/*.parquet')];print(len(fs),sum('.bak' in f for f in fs))"` |
| 2 | 截断指纹：`sh600000` 本地只有 1033 行（2022-07-19 起），但同一只向腾讯要 1996-2000 段返回 **1999-11-10 → 2000-12-29 共 273 行**（正是它的上市日）。按「老代码段 + 行数 ≤1100 + 末行在 2025 后」粗筛 112 只候选，其中 `sh600925/600930/600938/601022` 是真的晚上市（源 1996-2000 返回 0 行）⇒ **必须逐只向源确认，不能按行数猜** | 见任务 3 的审计脚本 |
| 3 | 截断根因在接口，不在代码参数：Sina `getKLineData` 只有 `datalen`，**`datalen=3000/6000` 直接返回 0 行**；`tdxfeed/full_sync.py:228` 走的就是它，`tdxfeed/sources/sina_source.py:3` 自己写着「最大 datalen=1023（约4年）」。⇒ 日K 深取不可能从 Sina 拿 | `venv/bin/python -c` 见任务 2 的探针，或 `curl -s 'https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_s=/CN_MarketDataService.getKLineData?symbol=sz000021&scale=240&ma=no&datalen=3000'` |
| 4 | 通达信通道在本机死：`pytdx` 连 `218.6.170.47:7709` TCP 成功（0.1s）但 `get_security_bars(9,0,'000021',0,800)` 返回 **None**；`full_sync.py:99` 早已注明「本环境实测 TDX 公共服务器不吐指数」。东财 `push2his` 连测 3 次 `ConnectionError`（域名解析正常、`/` 回 404，API 侧拒绝），而 akshare 的 `stock_zh_a_hist` 正建在它上面 ⇒ 现存深历史是当年东财可用时灌进来的，现在复制不了 | 方案文档执行时重跑一次确认边界没变 |
| 5 | 唯一可用深取通道 = 腾讯：`param=sz000021,day,1996-01-01,2005-12-31,{count},` 在 count=320/640/1023/2000 时分别返回 320/640/1023/2000 行，**末行恒为 2005-12-30，首行随 count 增大而左移** ⇒ 语义是「区间内最近 N 根」，翻页要靠移动 `end`。`count=5000` 返回 **0 行** ⇒ 单次上限在 2000 与 5000 之间，超限是静默空返回 | 任务 2 Step 1 的探针 |
| 6 | **现存 bug（本方案最重要的一条）**：腾讯日K 的 volume **已经是手**，`2026-10-23 sz000021` 源给 `665445.000`，库里同日 `volume=665444.67`（同为手）。而 `tdxfeed/sources/tencent_source.py:44` 写着 `float(row[5]) / 100.0,  # 股→手` ——按它跑，回填进去的每一根体积都小 100 倍，只会触发 `writers.py:143` 那条「相差 100 倍」的单位告警（不阻断）。`sina_source.py:45` 的 ÷100 是对的（Sina 吐股），两源单位不同被抄混了 | `venv/bin/python -c` 见任务 1 |
| 7 | 复权层两条都能拿到：腾讯 `hfq` 在 1997 年有数据（`1997-08-01` hfq 收 98.370 / 未复权收 23.050 ⇒ 比值 4.27），且 `qfq` 序列天然以最新日为锚（2026-10 的 qfq 与未复权逐行相同），所以**不能拿 qfq 当因子存**，得用 `hfq_close/raw_close` | 任务 5 |
| 8 | 返回键按复权类型不同：`''` → `day`，`qfq` → `qfqday`，`hfq` → `hfqday`。既有 `tencent_source.py:26` 取 `node.get('qfqday') or node.get('day')`，在最新一段会「请求复权却拿到未复权」而无人报错——分页新代码必须按类型取键 | 任务 2 |

---

### Task 1: 修腾讯日K 体积单位（先出 RED）

**Files:**
- Modify: `tdxfeed/sources/tencent_source.py:37-46`
- Test: `tests/test_tencent_daily_volume_unit.py`（新建）

**Interfaces:**
- Consumes: `TencentSource.fetch_bars(symbol, start_date=None, end_date=None)`（现有签名不变）
- Produces: `bars` 行形状 `[date:int YYYYMMDD, open, high, low, close, volume(手), amount(元)]`，volume 不做 ÷100

- [ ] **Step 1: 跑一次真实探针确认单位，把结论钉在提交信息里**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python - <<'PY'
import json,urllib.request,pandas as pd
u='https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz000021,day,2026-10-20,2026-10-24,10,'
with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=20) as r:
    rows=json.loads(r.read().decode())['data']['sz000021']['day']
df=pd.read_parquet('data/parquet/daily/sz000021.parquet')
print("源末行", rows[-1])
print("库同日", df.tail(1).to_dict('records'))
PY
```
Expected: 源 volume `665445.000` 与库 `volume≈665444.67` **同数量级**（比值 ~1.0，不是 ~100）⇒ 源单位=手。

- [ ] **Step 2: 写失败测试（用假 urlopen，不打网）**

创建 `tests/test_tencent_daily_volume_unit.py`：

```python
"""腾讯日K 的 volume 单位=手，适配器不能再 ÷100（Sina 才吐股）。

真实依据（2026-10-25 实测）：sz000021 在 2026-10-23 源返回 665445.000，
库里同日 volume=665444.67（同为手）。按旧实现会写成 6654.45，
只有 writers.py 那条 >50× 的单位告警会响，且不阻断。
"""
import json

import pytest

from tdxfeed.sources.tencent_source import TencentSource

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
    assert bars[0][0] == 20261023
    # 列序 [date, open, close, high, low, volume]
    assert bars[0][1] == pytest.approx(27.78)   # open
    assert bars[0][4] == pytest.approx(29.19)   # close
    assert bars[0][2] == pytest.approx(29.28)   # high
    assert bars[0][3] == pytest.approx(26.85)   # low
    assert bars[0][5] == pytest.approx(665445.0, rel=1e-6)  # 手，不 ÷100
```

- [ ] **Step 3: 跑测试确认它按预期失败**

Run: `venv/bin/pytest tests/test_tencent_daily_volume_unit.py -q`
Expected: FAIL，`assert 6654.45 == 665445.0`（旧实现正好差 100 倍）——这一步的失败输出要留进提交信息。

- [ ] **Step 4: 改实现**

`tdxfeed/sources/tencent_source.py` 把行内 `float(row[5]) / 100.0` 换成直取，并同步改文档字符串里那行注释：

```python
                for row in rows:
                    # 腾讯日K 列序: [date, open, close, high, low, volume(手)]
                    # 单位=手，与库里 daily parquet 同口径，不能再 ÷100
                    # （÷100 只对 Sina 成立，Sina 吐的是股：sina_source.py:45）
                    bars.append([
                        self.normalize_date(str(row[0]).replace('-', '')),
                        float(row[1]),   # open
                        float(row[3]),   # high
                        float(row[4]),   # low
                        float(row[2]),   # close
                        float(row[5]),   # 手
                        0.0,             # 腾讯无成交额，由 amount_fix 口径补
                    ])
```

- [ ] **Step 5: 跑测试确认通过，并确认 Sina 侧没被顺手改坏**

Run: `venv/bin/pytest tests/test_tencent_daily_volume_unit.py tests/ -q -k "sina or volume or amount"`
Expected: 全绿（`tests/test_daban_*` 里若有依赖旧腾讯口径的断言，说明那个断言本身是照 bug 写的，逐条核对后再改，不许直接放宽）。

- [ ] **Step 6: 提交（显式路径）**

```bash
cd /home/jiuben/tdx-data-feed && git add -- tests/test_tencent_daily_volume_unit.py tdxfeed/sources/tencent_source.py \
 && git commit -F /tmp/tencent_unit.txt -- tests/test_tencent_daily_volume_unit.py tdxfeed/sources/tencent_source.py
```

---

### Task 2: 腾讯分页深取适配器

**Files:**
- Create: `tdxfeed/history_paging.py`
- Test: `tests/test_tencent_history_paging.py`

**Interfaces:**
- Consumes: `TencentSource.fetch_bars`（Task 1 后的形状）
- Produces:
  - `PAGE_CAP: int = 1024`、`TENCENT_KEYS: dict[str, str]`
  - `fetch_history(symbol: str, want_from: str, *, fetch_page=None, page_cap: int = PAGE_CAP) -> list[list]`
    返回按日期升序、去重后的 bars（同 `fetch_bars` 行形状），最早一日 ≤ `want_from` 或源已到尽头。
  - `EmptyPageError`：任何一页返回空即抛出。

- [ ] **Step 1: 先把单次上限钉成常量（实测边界）**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python - <<'PY'
import json,urllib.request
def n(count):
    u=f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz000021,day,1990-01-01,2005-12-31,{count},'
    with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=20) as r:
        d=json.loads(r.read().decode())['data']['sz000021']
    rows=d.get('day') or []
    return len(rows), (rows[0][0] if rows else None)
for c in (1024, 2000, 2500, 3000, 4000):
    print(c, n(c))
PY
```
Expected: 找到「仍返回 N 行」的最大 count 与「返回 0 行」的最小 count，`PAGE_CAP` 取前者的一半并注释写明实测数字（已知 2000 可用、5000 空）。

- [ ] **Step 2: 写失败测试**

创建 `tests/test_tencent_history_paging.py`：

```python
"""腾讯 fqkline 没有偏移量参数，只能按 end 日期向前翻页；超限是静默空返回。

§0 实测：count=320/640/1023/2000 时末行恒为区间末日、首行随 count 左移，
count=5000 返回 0 行。Sina 的 datalen>1023 同样是 0 行——「要太多」在这两个源
里都不报错，所以分页器必须把空页当异常，而不是当「没有更早数据」。
"""
import pytest

from tdxfeed.history_paging import EmptyPageError, fetch_history


def page_factory(pages):
    """pages: 按请求顺序吐出的行列表，行格式 [date, open, close, high, low, volume]。"""
    seen = []

    def fetch_page(symbol, end_date, count, adjust=""):
        seen.append((end_date, count))
        rows = pages[len(seen) - 1] if len(seen) <= len(pages) else []
        return [[int(r[0].replace("-", "")), float(r[1]), float(r[3]), float(r[4]),
                 float(r[2]), float(r[5]), 0.0] for r in rows], rows
    return fetch_page, seen


def test_pages_backwards_by_end_date_until_start_reached():
    p1 = [["2003-01-02", "10", "11", "12", "9", "100"], ["2003-06-30", "10", "11", "12", "9", "100"]]
    p2 = [["1999-11-10", "10", "11", "12", "9", "100"], ["2002-12-31", "10", "11", "12", "9", "100"]]
    fetch_page, seen = page_factory([p1, p2])
    bars = fetch_history("sz000021", "2000-01-01",
                         fetch_page=fetch_page, page_cap=2)
    dates = [b[0] for b in bars]
    assert dates == sorted(dates), "必须升序"
    assert 20000101 <= min(dates), "第二页把 1999-11-10 带进来后才停"
    assert len(seen) == 2, "两页够了就该停：第二页行数 < page_cap"


def test_empty_page_raises_instead_of_silently_stopping():
    fetch_page, _ = page_factory([[]])
    with pytest.raises(EmptyPageError):
        fetch_history("sz000021", "2000-01-01", fetch_page=fetch_page, page_cap=2)


def test_dedupes_overlapping_rows_across_pages():
    same = [["2003-01-02", "10", "11", "12", "9", "100"]]
    fetch_page, _ = page_factory([same, same, []])
    bars = fetch_history("sz000021", "1995-01-01",
                         fetch_page=fetch_page, page_cap=1)
    assert [b[0] for b in bars].count(20030102) == 1


def test_caps_page_size_below_source_limit():
    from tdxfeed.history_paging import PAGE_CAP
    assert PAGE_CAP <= 2000, "§0 实测 count=2000 是已知可用上界，5000 直接空返回"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_tencent_history_paging.py -q`
Expected: `ModuleNotFoundError: No module named 'tdxfeed.history_paging'`

- [ ] **Step 4: 写最小实现**

创建 `tdxfeed/history_paging.py`：

```python
"""按 end 日期向前翻腾讯日K，绕开源接口的单次条数上限（分页截断的正解）。

见本仓 docs/superpowers/plans/2026-10-25-daily-history-2000-backfill.md §0：
- 腾讯 fqkline 无 offset，count 语义是「区间内最近 N 根」，要太多直接返回空；
- Sina 只有 datalen，>1023 返回空；
- 东财 push2his 与通达信公共服务器在本机均不可用。
所以「空页」有两种含义（到尽头 / 请求超限），分页器必须把它们分开——
本模块的做法是：第一页就空 = 异常，后续页空 = 到尽头。
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

# §0 实测 count=2000 可用、count=5000 空返回；取已知安全值的一半留余量。
PAGE_CAP = 1024
# 返回键随复权类型不同（§0 第 8 条）：混用会「请求复权却拿到未复权」。
TENCENT_KEYS = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}
BASE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        "?param={sym},day,{start},{end},{count},{adjust}")


class EmptyPageError(RuntimeError):
    """第一页就空：请求超限或代码不存在。不能当成「没有更早历史」。"""


def _http_page(sym: str, end: str, count: int, adjust: str) -> list[list]:
    url = BASE.format(sym=sym, start="1990-01-01", end=end,
                      count=count, adjust=adjust)
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                    timeout=20) as r:
                payload = json.loads(r.read().decode("utf-8"))
            node = (payload.get("data") or {}).get(sym)
            if isinstance(node, dict):
                return node.get(TENCENT_KEYS.get(adjust, "day")) or []
            return node or []
        except Exception as e:  # 网络抖动重试；空返回不抛，交给调用方判语义
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"腾讯页请求失败 {sym} end={end}: {last!s}")


def _to_bar(row: list) -> list:
    """腾讯行 [date, open, close, high, low, volume(手)] → daily parquet 行形状。"""
    d = int(str(row[0]).replace("-", ""))
    return [d, float(row[1]), float(row[3]), float(row[4]), float(row[2]),
            float(row[5]), 0.0]


def fetch_history(symbol: str, want_from: str, *, fetch_page=None,
                  page_cap: int = PAGE_CAP, adjust: str = "",
                  sleep: float = 0.15) -> list[list]:
    """翻到 `want_from`（'YYYY-MM-DD'）之前或源尽头为止，返回升序去重 bars。"""
    fetch = fetch_page or (lambda s, e, c, a: [(_to_bar(r), r)
                           for r in []] and (_http_page(s, e, c, a), None)[0:1])
    if fetch_page is None:
        def fetch(sym, end, count, adjust=""):
            raw = _http_page(sym, end, count, adjust)
            return ([_to_bar(r) for r in raw], raw)
    want = int(want_from.replace("-", ""))
    out: dict[int, list] = {}
    end = dt.date.today()
    page_no = 0
    while True:
        page, raw = fetch(symbol, end.isoformat(), page_cap, adjust)
        page_no += 1
        if not page:
            if page_no == 1:
                raise EmptyPageError(
                    f"{symbol} 首页即空 end={end} count={page_cap}："
                    "多半是请求超限或代码无效，不是历史尽头")
            break
        for bar in page:
            out[bar[0]] = bar
        first = min(b[0] for b in page)
        if len(page) < page_cap:      # 不足一页 = 源已到尽头
            break
        if first < want:              # 已翻到目标起点之前
            break
        end = dt.datetime.strptime(str(first), "%Y%m%d").date() - dt.timedelta(days=1)
        time.sleep(sleep)
    return sorted(out.values(), key=lambda b: b[0])
```

注意：上面 `fetch = fetch_page or (...)` 一行的默认值写法可读性差，实现时直接写成

```python
    fetch = fetch_page if fetch_page is not None else _default_page
```

并把 `_default_page` 定义为模块级函数：

```python
def _default_page(sym: str, end: str, count: int, adjust: str = ""):
    raw = _http_page(sym, end, count, adjust)
    return [_to_bar(r) for r in raw], raw
```

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_tencent_history_paging.py -q`
Expected: 4 passed

- [ ] **Step 6: 真网单只验证（只读，不落盘）**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python -c "
from tdxfeed.history_paging import fetch_history
b=fetch_history('sh600000','1999-01-01')
print('页数外的总行数',len(b),'最早',b[0][0],'最晚',b[-1][0])
assert b[0][0]<=19991231, '没翻到 2000 年前'
"
```
Expected: 最早 ≤ 1999-12-31（浦发 1999-11-10 上市），行数 > 6000（1999→2026）。

- [ ] **Step 7: 提交**

```bash
cd /home/jiuben/tdx-data-feed && git add -- tdxfeed/history_paging.py tests/test_tencent_history_paging.py \
 && git commit -F /tmp/paging.txt -- tdxfeed/history_paging.py tests/test_tencent_history_paging.py
```

---

### Task 3: 只读覆盖度审计（先量化，再回填）

**Files:**
- Create: `scripts/audit_daily_history_depth.py`
- Test: `tests/test_history_depth_gap_plan.py`
- Output: `data/audit/history_depth_<YYYYMMDD>.json`（gitignore，不进库）

**Interfaces:**
- Consumes: `fetch_history`（Task 2）、`data/all_a_stocks.json`、`data/parquet/daily/*.parquet`
- Produces:
  - `plan_gaps(local_min: int | None, local_max: int | None, source_first: int | None, target: int) -> dict`
    纯函数，返回 `{"code": ..., "state": "ok|extend|unknown|listed_after_target", "missing_days_est": int}`。
  - `main()` 退出码：0 成功，1 有超过 10% 代码请求失败（防「审计自己也静默截断」）。

- [ ] **Step 1: 写失败测试（纯函数，不打网）**

创建 `tests/test_history_depth_gap_plan.py`：

```python
"""审计的分类逻辑：不能按「本地行数 ≤1100」猜谁被截断了。

§0 第 2 条实测：112 只「行数像被钉住」的老代码段候选里，
sh600925/600930/600938/601022 是真的 2022 后上市，源 1996-2000 返回 0 行；
只有 sh600000 这类才是真缺口。分类必须同时看「本地起始日」和「源首行」。
"""
import pytest

from scripts.audit_daily_history_depth import plan_gaps

TARGET = 20000101


def test_local_start_after_target_but_source_has_earlier_is_extend():
    r = plan_gaps(20220719, 20261023, 19991110, TARGET)
    assert r["state"] == "extend"
    assert r["missing_days_est"] > 5000


def test_source_has_nothing_before_target_is_a_real_late_listing():
    r = plan_gaps(20230329, 20261023, None, TARGET)
    assert r["state"] == "listed_after_target"
    assert r["missing_days_est"] == 0


def test_already_deep_enough_is_ok():
    assert plan_gaps(19940202, 20261023, 19940202, TARGET)["state"] == "ok"


def test_request_failure_is_unknown_not_ok():
    """源请求失败（None 来自异常）绝不能被归成「不用补」。"""
    assert plan_gaps(20220719, 20261023, -1, TARGET)["state"] == "unknown"


def test_missing_local_file_counts_as_full_extend():
    r = plan_gaps(None, None, 19930104, TARGET)
    assert r["state"] == "extend"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_history_depth_gap_plan.py -q`
Expected: `ModuleNotFoundError` 或 `ImportError: cannot import name 'plan_gaps'`

- [ ] **Step 3: 写实现**

创建 `scripts/audit_daily_history_depth.py`：

```python
#!/usr/bin/env python
"""只读审计：每只沪深个股「本地起始日 vs 源能给的最早日」，产出待回填清单。

零写入（只往 data/audit/ 写一份自己的产物）。用法：
    venv/bin/python scripts/audit_daily_history_depth.py --workers 4
    venv/bin/python scripts/audit_daily_history_depth.py --limit 20   # 先小批量
产物 data/audit/history_depth_YYYYMMDD.json 是 gitignore 的，任务 6 的绊线测试读它。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from tdxfeed.history_paging import EmptyPageError, fetch_history  # noqa: E402

DAILY = ROOT / "data" / "parquet" / "daily"
TARGET_DEFAULT = 20000101


def plan_gaps(local_min, local_max, source_first, target: int) -> dict:
    """分类：ok / extend / listed_after_target / unknown。source_first=-1 表示请求失败。"""
    if source_first == -1:
        return {"state": "unknown", "missing_days_est": 0}
    if source_first is None:
        return {"state": "listed_after_target", "missing_days_est": 0}
    if local_min is not None and local_min <= target:
        return {"state": "ok", "missing_days_est": 0}
    if source_first >= target:
        return {"state": "listed_after_target", "missing_days_est": 0}
    end = local_min if local_min is not None else int(dt.date.today().strftime("%Y%m%d"))
    days = (pd.Timestamp(str(end)) - pd.Timestamp(str(source_first))).days
    return {"state": "extend", "missing_days_est": max(0, int(days * 240 / 365))}


def local_span(sym: str):
    p = DAILY / f"{sym}.parquet"
    if not p.exists():
        return None, None
    df = pd.read_parquet(p, columns=["date"])
    d = pd.to_datetime(df["date"].astype("int64").astype(str), format="%Y%m%d")
    return int(d.min().strftime("%Y%m%d")), int(d.max().strftime("%Y%m%d"))


def audit_one(sym: str, target: int) -> dict:
    lo, hi = local_span(sym)
    try:
        bars = fetch_history(sym, str(target)[:4] + "-" + str(target)[4:6] + "-" + str(target)[6:],
                             page_cap=256)
        first = bars[0][0] if bars else None
        err = None
    except EmptyPageError as e:
        first, err = None, str(e)[:120]
    except Exception as e:  # 网络/解析异常一律计 unknown，不掩盖
        first, err = None, f"{type(e).__name__}:{str(e)[:100]}"
    r = plan_gaps(lo, hi, -1 if err else first, target)
    r.update({"symbol": sym, "local_min": lo, "local_max": hi,
              "source_first": first, "error": err})
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=int, default=TARGET_DEFAULT)
    args = ap.parse_args()

    uni = json.load(open(ROOT / "data" / "all_a_stocks.json"))
    syms = [("sh" + c if c.startswith("6") else "sz" + c)
            for c in uni if c[:2] in ("00", "60")]
    if args.limit:
        syms = syms[: args.limit]
    print(f"待审计 {len(syms)} 只 target={args.target}", flush=True)

    results, done = [], 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(lambda s: audit_one(s, args.target), syms):
            results.append(r)
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(syms)}", flush=True)
    out = ROOT / "data" / "audit" / f"history_depth_{dt.date.today():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "target": args.target,
        "total": len(results),
        "by_state": {},
        "missing_days_total": 0,
        "rows": results,
    }
    for r in results:
        summary["by_state"][r["state"]] = summary["by_state"].get(r["state"], 0) + 1
        summary["missing_days_total"] += r["missing_days_est"]
    summary["request_failure_ratio"] = round(
        summary["by_state"].get("unknown", 0) / max(len(results), 1), 4)
    json.dump(summary, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"},
                     ensure_ascii=False), flush=True)
    print(f"产物 → {out}", flush=True)
    return 1 if summary["request_failure_ratio"] > 0.1 else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_history_depth_gap_plan.py -q`
Expected: 5 passed

- [ ] **Step 5: 小批量真跑，确认形状再放开**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python scripts/audit_daily_history_depth.py --limit 20
```
Expected: `by_state` 至少分出 `ok` 与 `listed_after_target`；`sh600000` 若在 20 只里应为 `extend`。人工看一眼 `rows` 里的 `source_first`，确认不是被 count 上限钉出来的假象（这正是用户「先便宜地证伪前提」的口径）。

- [ ] **Step 6: 全量审计（约 5200 次请求，4 并发 ≈ 30-50 分钟）**

```bash
cd /home/jiuben/tdx-data-feed && nohup venv/bin/python scripts/audit_daily_history_depth.py --workers 4 >> logs/history-audit.log 2>&1 &
```
Expected: 退出码 0；`missing_days_total` 落在几百万数量级。若 `unknown > 10%`，先修通道再进任务 4——审计自身不可信时回填范围就是错的。

- [ ] **Step 7: 提交**

```bash
cd /home/jiuben/tdx-data-feed && git add -- scripts/audit_daily_history_depth.py tests/test_history_depth_gap_plan.py \
 && git commit -F /tmp/audit.txt -- scripts/audit_daily_history_depth.py tests/test_history_depth_gap_plan.py
```

---

### Task 4: 回填 runner（幂等、可续跑、先备份）

**Files:**
- Create: `tdxfeed/backfill_history.py`
- Create: `scripts/backfill_daily_history.py`（薄 CLI）
- Test: `tests/test_backfill_history_runner.py`

**Interfaces:**
- Consumes: `fetch_history`（Task 2）、`Writers.merge_write`（现有）、审计 JSON（Task 3）
- Produces:
  - `backfill_symbol(sym: str, target: int, *, fetch=fetch_history, writers=None, state=None) -> dict`
    返回 `{"symbol", "rows_added", "first_date", "skipped": bool, "reason": str | None}`。
  - `resume_key(sym: str) -> str`；续跑状态落 `data/.state/history_backfill.json`。

- [ ] **Step 1: 先做带日期备份（AGENTS.md 硬要求，`data/` gitignore 救不回来）**

```bash
cd /home/jiuben/tdx-data-feed && mkdir -p ~/backups && tar -czf ~/backups/daily-parquet-$(date +%Y%m%d).tar.gz -C data parquet/daily && ls -l ~/backups/daily-parquet-$(date +%Y%m%d).tar.gz
```
Expected: 生成 ~1.1 GB 级归档；`tar -tzf` 能列出文件才算备份成立。备份 ref 路径写进提交信息。

- [ ] **Step 2: 写失败测试（假 fetch，零网络、写临时目录）**

创建 `tests/test_backfill_history_runner.py`：

```python
"""回填 runner 的两条不可让渡性质：幂等、且不许把已有真 amount 覆盖成 0。

merge_write 内部已有「同日 amount 取 max」保护（writers.py:155），
本测试钉的是 runner 这层：重复跑同一只必须不再增行；
深取段（amount=0）不得改变已有段的 amount。
"""
import pandas as pd
import pytest

from tdxfeed.backfill_history import backfill_symbol
from tdxfeed.writers import Writers
from tdxfeed.state import State


def _mk(tmp_path):
    w = Writers(str(tmp_path))
    st = State(str(tmp_path / ".state" / "m.json"))
    return w, st


def bars(start, n, amount):
    d = pd.date_range(start, periods=n, freq="B")
    return [[int(x.strftime("%Y%m%d")), 1.0, 2.0, 0.5, 1.5, 100.0, amount] for x in d]


def test_second_run_adds_nothing(tmp_path):
    w, st = _mk(tmp_path)
    deep = bars("1996-01-02", 40, 0.0)
    recent = bars("2026-01-02", 10, 5e8)
    r1 = backfill_symbol("sz000021", 20000101,
                         fetch=lambda s, t: deep + recent, writers=w, state=st)
    before = pd.read_parquet(tmp_path / "parquet/daily/sz000021.parquet")
    r2 = backfill_symbol("sz000021", 20000101,
                         fetch=lambda s, t: deep + recent, writers=w, state=st)
    after = pd.read_parquet(tmp_path / "parquet/daily/sz000021.parquet")
    assert r2["rows_added"] == 0, "幂等：第二次不该再增行"
    assert len(after) == len(before)
    assert r1["rows_added"] == len(deep) + len(recent)


def test_existing_amount_survives_deep_segment(tmp_path):
    w, st = _mk(tmp_path)
    backfill_symbol("sz000021", 20000101,
                    fetch=lambda s, t: bars("2026-01-02", 5, 5e8), writers=w, state=st)
    p = tmp_path / "parquet/daily/sz000021.parquet"
    before = pd.read_parquet(p)
    backfill_symbol("sz000021", 20000101,
                    fetch=lambda s, t: bars("1996-01-02", 30, 0.0), writers=w, state=st)
    after = pd.read_parquet(p)
    m = after[after["date"].isin(before["date"])]
    assert (m["amount"] > 0).all(), "已有真实成交额不能被深取段的 0 抹掉"


def test_empty_fetch_raises_not_silently_ok(tmp_path):
    w, st = _mk(tmp_path)
    with pytest.raises(RuntimeError):
        backfill_symbol("sz000021", 20000101, fetch=lambda s, t: [], writers=w, state=st)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_backfill_history_runner.py -q`
Expected: `ModuleNotFoundError: No module named 'tdxfeed.backfill_history'`

- [ ] **Step 4: 写实现**

创建 `tdxfeed/backfill_history.py`：

```python
"""把一只票的日线向历史方向补到 target 之前，写盘只走 merge_write。

为什么不直接 write_parquet：merge_write 已带三条保护——
mergesort + keep='last' 去重、同日 amount 取 max（防深取段 0 抹掉真值）、
重建 .day 保证两格式同源（writers.py:128-175）。
"""
from __future__ import annotations

import json
import os

from tdxfeed.history_paging import fetch_history
from tdxfeed.state import State
from tdxfeed.writers import Writers

STATE_NAME = os.path.join(".state", "history_backfill.json")


def resume_key(symbol: str) -> str:
    return symbol


def _done_symbols(state_path: str) -> set[str]:
    if not os.path.exists(state_path):
        return set()
    return set(json.load(open(state_path, encoding="utf-8")).get("done", []))


def backfill_symbol(symbol: str, target: int, *, fetch=fetch_history,
                    writers: Writers | None = None,
                    state: State | None = None) -> dict:
    w = writers or Writers(os.environ.get("TDX_OUT_ROOT", "data"))
    want_from = str(target)
    want_from = f"{want_from[:4]}-{want_from[4:6]}-{want_from[6:]}"
    bars = fetch(symbol, want_from)
    if not bars:
        raise RuntimeError(f"{symbol} 深取返回 0 行：这不是历史尽头，是通道故障")
    path = os.path.join(w.out_root, "parquet", "daily", f"{symbol}.parquet")
    before = 0
    if os.path.exists(path):
        import pandas as pd
        before = len(pd.read_parquet(path, columns=["date"]))
    n = w.merge_write(symbol, bars, category="daily")
    if state is not None:
        state.update(resume_key(symbol), str(bars[-1][0]), n)
    return {"symbol": symbol, "rows_added": max(0, n - before),
            "first_date": bars[0][0], "skipped": False, "reason": None}


def mark_done(state_path: str, symbol: str) -> None:
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    data = {"done": sorted(_done_symbols(state_path) | {symbol})}
    json.dump(data, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)
```

创建 `scripts/backfill_daily_history.py`：

```python
#!/usr/bin/env python
"""按审计产物回填历史日线。用法：
    venv/bin/python scripts/backfill_daily_history.py --dry-run --limit 5
    venv/bin/python scripts/backfill_daily_history.py --workers 3
退出码：0 全绿；1 = 失败/待补比例超阈值（与 full_sync.sync_exit_code 同思路，
空数据不能报成完美）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.backfill_history import backfill_symbol, mark_done  # noqa: E402
from tdxfeed.state import State  # noqa: E402
from tdxfeed.writers import Writers  # noqa: E402

BAD_RATIO_MAX = 0.05


def latest_audit() -> Path:
    fs = sorted(glob.glob(str(ROOT / "data" / "audit" / "history_depth_*.json")))
    if not fs:
        raise SystemExit("先跑 scripts/audit_daily_history_depth.py（任务 3）")
    return Path(fs[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--target", type=int, default=20000101)
    args = ap.parse_args()

    audit = json.load(open(latest_audit(), encoding="utf-8"))
    todo = [r["symbol"] for r in audit["rows"] if r["state"] == "extend"]
    state_path = str(ROOT / "data" / ".state" / "history_backfill.json")
    done = set()
    if Path(state_path).exists():
        done = set(json.load(open(state_path, encoding="utf-8")).get("done", []))
    todo = [s for s in todo if s not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"待回填 {len(todo)} 只（审计 extend 总数 "
          f"{sum(1 for r in audit['rows'] if r['state'] == 'extend')}）", flush=True)
    if args.dry_run:
        print("dry-run：不写盘", flush=True)
        return 0

    w, st = Writers(str(ROOT / "data")), State(state_path)
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(lambda s: _run(s, args.target, w, st, state_path), todo):
            if r.get("error"):
                fail += 1
                print(f"FAIL {r['symbol']}: {r['error']}", flush=True)
            else:
                ok += 1
    total = ok + fail
    print(f"完成 成功{ok} 失败{fail}", flush=True)
    return 1 if total and fail / total > BAD_RATIO_MAX else 0


def _run(sym, target, w, st, state_path) -> dict:
    try:
        r = backfill_symbol(sym, target, writers=w, state=st)
        mark_done(state_path, sym)
        return r
    except Exception as e:
        return {"symbol": sym, "error": f"{type(e).__name__}:{str(e)[:120]}"}


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_backfill_history_runner.py -q`
Expected: 3 passed

- [ ] **Step 6: dry-run + 5 只真跑 + 单位回归复核**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python scripts/backfill_daily_history.py --dry-run
venv/bin/python scripts/backfill_daily_history.py --limit 5
venv/bin/python - <<'PY'
import pandas as pd
df=pd.read_parquet('data/parquet/daily/sh600000.parquet')
d=pd.to_datetime(df['date'].astype('int64').astype(str),format='%Y%m%d')
old=df[d<pd.Timestamp('2000-01-01')]
print("行数",len(df),"2000 前", len(old))
if len(old):
    print("2000 前 volume 中位", float(old['volume'].median()),
          "近 5 年 volume 中位", float(df[d>pd.Timestamp('2021-01-01')]['volume'].median()))
PY
```
Expected: 两个中位数**同数量级**（差 >50× 说明单位又混了，必须停下重查 Task 1）。

- [ ] **Step 7: 全量回填（`extend` 清单，3 并发）**

```bash
cd /home/jiuben/tdx-data-feed && nohup venv/bin/python scripts/backfill_daily_history.py --workers 3 >> logs/history-backfill.log 2>&1 &
```
中途可 Ctrl-C：续跑靠 `data/.state/history_backfill.json` 的 `done` 集，不在清单里的下一次自然跳过。

- [ ] **Step 8: 提交**

```bash
cd /home/jiuben/tdx-data-feed && git add -- tdxfeed/backfill_history.py scripts/backfill_daily_history.py tests/test_backfill_history_runner.py \
 && git commit -F /tmp/backfill.txt -- tdxfeed/backfill_history.py scripts/backfill_daily_history.py tests/test_backfill_history_runner.py
```

---

### Task 5: 复权因子扩到 2000（另建目录，不碰现存因子）

**Files:**
- Create: `tdxfeed/adj_factor_history.py`
- Test: `tests/test_adj_factor_history.py`

**Interfaces:**
- Consumes: `fetch_history` 的 `adjust='hfq'` 与 `adjust=''` 两条序列（Task 2）
- Produces:
  - `derive_factor(raw_rows: list[list], hfq_rows: list[list]) -> list[list]` → `[[date:int, raw_close, hfq_close, factor:float], ...]`
  - 新存储 `data/adj_factor_ext/{code}.parquet`；**现有 `data/hfq_factor/`、`data/adj_factor/` 一律不改**（它们从 2020-01-02 起，锚定口径未知，覆盖会让已发布报告失真）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_adj_factor_history.py`：

```python
"""因子只能用 hfq/raw 推，不能用 qfq：qfq 以最新日为锚，历史会随每次分红整体重算。

§0 实测：sz000021 的 qfq 序列在 2026-10 与未复权逐行相同（锚在最新），
而 hfq 在 1997-08-01 是 98.370、未复权收 23.050 ⇒ factor≈4.27 且稳定可存。
"""
import math

import pytest

from tdxfeed.adj_factor_history import derive_factor


def row(d, close):
    return [int(d), close, close, close, close, 100.0, 0.0]


def test_factor_is_hfq_over_raw_per_date():
    raw = [row("19970801", 23.05), row("19970804", 20.0)]
    hfq = [row("19970801", 98.37), row("19970804", 80.0)]
    out = derive_factor(raw, hfq)
    assert out[0][0] == 19970801
    assert out[0][3] == pytest.approx(98.37 / 23.05, rel=1e-6)


def test_dates_must_line_up_or_it_raises():
    with pytest.raises(ValueError):
        derive_factor([row("19970801", 23.05)], [row("19970804", 80.0)])


def test_zero_raw_close_is_rejected_not_divided():
    with pytest.raises(ZeroDivisionError):
        derive_factor([row("19970801", 0.0)], [row("19970801", 1.0)])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_adj_factor_history.py -q`
Expected: `ModuleNotFoundError: No module named 'tdxfeed.adj_factor_history'`

- [ ] **Step 3: 写实现**

创建 `tdxfeed/adj_factor_history.py`：

```python
"""从腾讯 hfq/未复权两条序列反推后复权因子，扩到 2000 年以前。

为什么另建目录：data/adj_factor/ 与 data/hfq_factor/ 都从 2020-01-02 起
（各 5221 只 / 1633 行），锚定口径不同就是两套数字；已发布报告引用的是旧目录，
就地改写会让历史结论失效。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

COLS = ["date", "raw_close", "hfq_close", "factor"]


def derive_factor(raw_rows, hfq_rows):
    raw = {r[0]: float(r[4]) for r in raw_rows}
    adj = {r[0]: float(r[4]) for r in hfq_rows}
    if set(raw) != set(adj):
        raise ValueError(
            f"两条序列日期不齐：only_raw={sorted(set(raw) - set(adj))[:3]} "
            f"only_hfq={sorted(set(adj) - set(raw))[:3]}")
    out = []
    for d in sorted(raw):
        rc = raw[d]
        if rc <= 0:
            raise ZeroDivisionError(f"{d} 未复权收盘 {rc} ≤0，因子无法反推")
        out.append([d, rc, adj[d], adj[d] / rc])
    return out


def write_ext(out_root: str, code: str, factor_rows) -> Path:
    d = Path(out_root) / "adj_factor_ext"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{code}.parquet"
    df = pd.DataFrame(factor_rows, columns=COLS)
    df["date"] = df["date"].astype("int64")
    tmp = p.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, p)   # 半写不可见：中断不留脏文件
    return p
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_adj_factor_history.py -q`
Expected: 3 passed

- [ ] **Step 5: 单只真跑并核对口径一致性（关键：与现存 2020+ 因子比对）**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python - <<'PY'
import pandas as pd
from tdxfeed.history_paging import fetch_history
from tdxfeed.adj_factor_history import derive_factor, write_ext
sym, code = 'sz000021', '000021'
raw = fetch_history(sym, '2000-01-01', adjust='')
hfq = fetch_history(sym, '2000-01-01', adjust='hfq')
rows = derive_factor(raw, hfq)
new = pd.DataFrame(rows, columns=['date','raw_close','hfq_close','factor'])
old = pd.read_parquet('data/hfq_factor/000021.parquet')
print("新因子最早", new.date.min(), "行数", len(new))
print("现存 hfq 列名", list(old.columns))
m = new.merge(old, on='date', how='inner')
if len(m):
    col = 'adj_factor' if 'adj_factor' in old.columns else [c for c in old.columns if 'factor' in c][0]
    ratio = (m['factor'] / m[col]).dropna()
    print("重叠", len(m), "行；本目录/现存 比值中位", float(ratio.median()),
          "std", float(ratio.std()))
PY
```
Expected: 比值 std ≈ 0（同一常数倍）⇒ 是锚定差异，可换算；std 大 ⇒ 两条序列不可混用，`adj_factor_ext` 就只能自成一套，任何下游引用必须显式选目录，不能假装兼容。把实测数字写进提交信息。

- [ ] **Step 6: 全量出因子 + 提交**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/python - <<'PY'
import json, sys; sys.path.insert(0,'.')
from concurrent.futures import ThreadPoolExecutor
from tdxfeed.history_paging import fetch_history
from tdxfeed.adj_factor_history import derive_factor, write_ext
uni=json.load(open('data/all_a_stocks.json'))
codes=[c for c in uni if c[:2] in ('00','60')]
def one(c):
    s=('sh' if c.startswith('6') else 'sz')+c
    try:
        write_ext('data', c, derive_factor(fetch_history(s,'1990-01-01',adjust=''),
                                          fetch_history(s,'1990-01-01',adjust='hfq')))
        return (c,0,None)
    except Exception as e:
        return (c,1,f"{type(e).__name__}:{str(e)[:80]}")
with ThreadPoolExecutor(max_workers=3) as ex:
    res=list(ex.map(one,codes))
print("失败", sum(r[1] for r in res), "共", len(res))
for r in res:
    if r[1]: print("  ", r)
PY
git add -- tdxfeed/adj_factor_history.py tests/test_adj_factor_history.py \
 && git commit -F /tmp/factor.txt -- tdxfeed/adj_factor_history.py tests/test_adj_factor_history.py
```

---

### Task 6: 截断绊线 + 带日期文档（防「下个月又红还不知道为什么」）

**Files:**
- Create: `tests/test_daily_history_depth_no_cap_truncation.py`
- Create: `docs/daily_history_2000_20261025.md`

**Interfaces:**
- Consumes: 审计 JSON（Task 3 产物）、`data/parquet/daily/`
- Produces: 一条会随通道退化而变红的守卫；一份记录字段质量差异的快照文档。

- [ ] **Step 1: 写绊线测试（默认跳过，审计产物在才生效）**

创建 `tests/test_daily_history_depth_no_cap_truncation.py`：

```python
"""绊线：回填过之后，不该再有「源有更早历史、本地却从近 4 年起」的票。

为什么必须有这条：2026-10-25 之前的整月里，`tdx-verification` 天天红而两套门禁
全绿（`v5/tests/test_math_sympy.py` 的 132 条断言 `make test` 一条也收不到）。
截断是静默的，Sina datalen>1023 与腾讯 count>2000 都返回空而不报错，
所以「源退化回 1023 根」这件事只能靠一条主动比对源的守卫来发现。
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = 20000101


def _audit() -> dict | None:
    fs = sorted(glob.glob(str(ROOT / "data" / "audit" / "history_depth_*.json")))
    return json.load(open(fs[-1], encoding="utf-8")) if fs else None


def test_audit_product_exists_when_claiming_backfilled():
    """文档说回填过，就必须拿得出审计产物——否则这条声明不可证伪。"""
    doc = ROOT / "docs" / "daily_history_2000_20261025.md"
    if not doc.exists():
        pytest.skip("任务 6 未执行")
    assert _audit() is not None, (
        "文档已声明回填，但没有 data/audit/history_depth_*.json：重跑 "
        "scripts/audit_daily_history_depth.py")


def test_no_extend_left_after_backfill():
    a = _audit()
    if a is None:
        pytest.skip("没有审计产物（先跑任务 3）")
    leftover = [r["symbol"] for r in a["rows"] if r["state"] == "extend"]
    assert not leftover, f"仍有 {len(leftover)} 只应回填未回填，例：{leftover[:5]}"


def test_deep_segment_amount_follows_estimate_convention():
    """2000 前那段的 amount 允许是 close×vol×100 的估算值，但不能是 0。"""
    import pandas as pd
    p = ROOT / "data" / "parquet" / "daily" / "sh600000.parquet"
    if not p.exists():
        pytest.skip("本机无该文件")
    df = pd.read_parquet(p)
    df["d"] = pd.to_datetime(df["date"].astype("int64").astype(str), format="%Y%m%d")
    old = df[df.d < pd.Timestamp("2000-01-01")]
    if not len(old):
        pytest.skip("sh600000 尚无 2000 前段")
    zero = int(((old.amount <= 0) & (old.volume > 0)).sum())
    assert zero == 0, f"{zero} 行有量无额，fill_missing_amount 没跟上"
```

- [ ] **Step 2: 跑测试，确认它在回填前是「红/跳过」的真实形状**

Run: `venv/bin/pytest tests/test_daily_history_depth_no_cap_truncation.py -q`
Expected: 任务 3 之后、任务 4 全量之前 → `test_no_extend_left_after_backfill` FAIL 并打印剩余数量；这就是回填范围的度量。

- [ ] **Step 3: 全量跑一遍两套门禁**

```bash
cd /home/jiuben/tdx-data-feed && venv/bin/pytest tests/ -q && make test
```
Expected: 根测试集全绿；`make test` 绿（它只覆盖 `v5/tests/`，与本链路无关，跑它是为了证明没连带打坏别的东西）。

- [ ] **Step 4: 写带日期文档（一次性快照，不是现状规格）**

创建 `docs/daily_history_2000_20261025.md`，必须包含以下五段实测字段（数字来自任务 3/5 的产物，不许照抄本方案）：

```markdown
# 全市场日线扩至 2000（2026-10-25 快照）

## 一、做了什么
- 通道：腾讯 fqkline 按 end 向前分页，PAGE_CAP=<实测值>；Sina 日K 受 datalen≤1023 限制、
  东财 push2his 与通达信公共服务器在本机不可用（各自实测症状）。
- 修掉的现存 bug：腾讯日K volume 单位=手，旧适配器 ÷100（`tdxfeed/sources/tencent_source.py:44`）。
- 范围：审计 extend 清单 N=<数字> 只，回填后全市场日线最早一日=<YYYYMMDD>。

## 二、字段质量在 2020 年前后不等（引用前必读）
| 字段 | 2020-01-02 起 | 2000-2019 | 源能给的最早 |
|---|---|---|---|
| OHLC/volume | 真值 | 真值（腾讯） | <日期> |
| amount | 真值 | **close×volume×100 估算**（`tdxfeed/amount_fix.py:13` 口径） | 无真值来源 |
| 复权因子 | `data/hfq_factor/` | `data/adj_factor_ext/`（本方案新增，比值 std=<数字>） | <日期> |

## 三、没解决的两条（不是工作量问题）
- 幸存者偏差：universe 仍是 5221 只现存代码，退市股没有来源。
- 涨跌停规则分段：`tdxfeed/daban/limit_price.py:67` 只有 `MAIN_ST_RULE_CHANGE_DATE`，
  所以 2000-2005 的打板标签不能直接和现规则混训。

## 四、复跑命令
（任务 3/4/6 的三条命令原样贴回）

## 五、备份
回填前归档：~/backups/daily-parquet-YYYYMMDD.tar.gz（tar -tzf 已验证）
```

- [ ] **Step 5: 提交**

```bash
cd /home/jiuben/tdx-data-feed && git add -- tests/test_daily_history_depth_no_cap_truncation.py docs/daily_history_2000_20261025.md \
 && git commit -F /tmp/tripwire.txt -- tests/test_daily_history_depth_no_cap_truncation.py docs/daily_history_2000_20261025.md
```

---

## Self-review 结论

1. **覆盖度**：「扩到 2000」= 任务 2（通道）+ 任务 3（范围）+ 任务 4（回填）+ 任务 5（配套因子）+ 任务 6（绊线与记录）；「解决分页截断」= 任务 2 的分页语义 + 空页异常 + 任务 1 的单位前置修复 + 任务 6 的静默退化绊线。§0 第 3/4 条记录的「不可用通道」没有对应任务，因为是边界事实不是缺陷。
2. **占位符**：任务 6 Step 4 文档里的 `<实测值>` / `<数字>` 是**要求执行者回填的测量槽**，每处都写清了从哪个产物取；其余步骤均为完整代码。
3. **类型一致性**：bars 行形状在任务 1/2/4 统一为 `[date:int YYYYMMDD, open, high, low, close, volume(手), amount(元)]`；`fetch_history(symbol, want_from, *, fetch_page, page_cap, adjust, sleep)` 在任务 4 以 `fetch=` 注入、任务 5 以 `adjust=` 复用，签名一致；`derive_factor(raw_rows, hfq_rows) -> list[list]` 与 `write_ext(out_root, code, factor_rows)` 只在任务 5 出现。
