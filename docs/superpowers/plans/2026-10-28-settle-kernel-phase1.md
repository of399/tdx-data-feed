# 阶段 1：结算内核与 11 处收编 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把散落在 11 个文件里的出场判定/成交价/费用口径收进 `tdxfeed/execution/`，让每个调用点只剩「声明自己用哪套口径 + 把结果映射回本文件的输出形状」。

**Architecture:** 双内核。内核 A（`single.py`）算单笔 TP/SL 结算，拆成两层：`check_exit()` 只做单根 K 线的阶梯判定，`settle_single_trade()` 负责从入场根逐根跑到出场并回灌逐日收益。内核 B（`hold.py`）算打板持有期（开盘涨停买不进、跌停顺延、T+1/T+2 卖）。共用 `limits.py`（涨跌幅比例/阈值/涨跌停价）、`costs.py`（费用）、`bars.py`（列名归一）、`result.py`（`TradeResult`）。

**Tech Stack:** Python 3.12（仓库 venv）、pandas、pytest、decimal.Decimal（价格取整）。

**Spec:** `docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md`（第 3 章 + 第 6 章阶段 1）

## Global Constraints

- 命令一律在仓库根执行，Python 走 `venv/bin/python` / `venv/bin/pytest`，不用系统 `python3`。
- **共享工作树**：`git add` / `git commit` 必须带显式路径，禁止 `-A` 和 `.`。看到不认识的脏文件（当前为 `scripts/backfill_minutes.py`、`tdxfeed/kline.py`、`v5/audit/verification/*`）先判断是不是别的会话的，**不要提交、不要顺手修**。
- 出场口径不可协商（来自 `AGENTS.md`）：同根 K 线双触发结算亏损侧；跳空穿越触发价按当日**开盘**成交；出场价回灌逐日收益，`return_pct` 与资金曲线同源。
- `data/`、`logs/`、`models/` 在 gitignore 里，改写前做带日期备份。
- 本阶段**不改**任何策略参数（TP/SL/hold_days 数值、信号条件、标的池），只改结算实现。
- 本阶段**不动** `data/` 下的任何数据；标签重算是阶段 2 的事。

## 与规格正文的 5 处更正（实施时以此为准）

实施精读代码后发现规格 §3.5 有 5 行判断需要更正，其余按规格执行：

| 规格行 | 规格原写 | 实测更正 |
|---|---|---|
| #2 `plan_e_sim_live` | 数值不变 | **数值微变**：① `TIME` 窗口差一根——`simulate_trade` 判 `future[1..10]` 后在 `future[10].close` 出场，`plan_e_backtest` 判 `entry+1..entry+9` 后在 `entry+9.close` 出场，统一到 plan_e 后持有期短一根；② 成本口径从「减 `2*(fee+slip)` 的加法式」统一到 plan_e 的乘法式，差约 5e-6。逐笔 diff 入报告。 |
| #5 `mcp/servers/backtest.py` | 收编到 A，数值不变 | 只收编**阶梯**（`check_exit`）。`max_hold` 到期、`cooldown`、挂单/撤单状态机是策略差异，留在调用方。数值除一种 K 线形状外不变：**开盘已在止盈线之上、日内又跌破止损线**时，旧实现先判「开盘越线」记 `take_profit_gap@开盘`，内核按 `sl_first` 记 `stop_loss@止损价`。方向保守（盈利笔记成亏损笔），2% 日波动下 20000 次随机走行 0 例，已接受并钉死用例（`test_gap_up_beyond_take_profit_plus_intraday_stop_settles_loss_side`）。 |
| #9 `daban/execution_backtest.py` | 收编到 B | 它吃的是**行式事件表**（`next_open`/`next_close`/`next_lim_dn` 列），手里没有 bar 序列，无法构造 `settle_hold_trade` 的入参。实际收编范围＝`net_return` 的**卖出侧**算术（`CostModel.sell`）；`_exit_price` 的跌停判断读事件表预计算的 `next_lim_dn`（数据列，不是规则计算），不动；买入侧也不套 `round_trip`（限价单无买入滑点，双边滑点每笔差 5bp）。结算仍行式。`#11 daban_event_table.py` 是这张表的生产者，本轮 0 改动。 |
| #10 `daban/topk_pnl.py` | 收编到 B，数值不变 | 数值**微变**：成本改委托 `round_trip`，逐笔 `|Δ| ≤ 1e-7`（旧式买入佣金写成 `(1-fee)` 乘子，`round_trip` 写成 `1/(1+commission)`，差 `commission²`）。成交价/时点/复权/跳过原因不变；两处标签级变化：入场根停牌且出场根缺失时内核先报 `suspended`（旧实现先报 `no_exit`），日期字段改走 `date_str` 归一。另需给 `HoldPlan` 加 `sell_offset`（CLI `--exit-offset` 允许 >2）。 |
| §4.2 硬编码路径数 | 47 个文件 | 五张表（daily/min*/derived/adj_factor/hfq_factor）口径实测 **50** 个（`scripts` 41 / `tdxfeed` 9 / `mcp` 2），另 `v5` 19 个走独立 `_paths.py`。阶段 3+4 的守卫以实测清单为准。 |

---

## File Structure

新建：

- `tdxfeed/paths.py` — 数据根与五张表的路径定位（本阶段只建，供 `names.py` 用；全库迁移在阶段 3+4）。
- `tdxfeed/names.py` — 全 A 名单与 ST 判定唯一来源。
- `tdxfeed/execution/__init__.py` — 公开 API 出口。
- `tdxfeed/execution/limits.py` — 比例/阈值/涨跌停价/开盘涨停判定。
- `tdxfeed/execution/costs.py` — `CostModel`、`Fill`。
- `tdxfeed/execution/bars.py` — `as_bars()` 列名归一。
- `tdxfeed/execution/result.py` — `TradeResult`。
- `tdxfeed/execution/single.py` — 内核 A。
- `tdxfeed/execution/hold.py` — 内核 B。
- `tests/test_execution_limits.py`、`tests/test_execution_costs.py`、`tests/test_execution_single.py`、`tests/test_execution_hold.py`、`tests/test_settle_kernel_parity.py`。

修改（11 个调用点 + 3 个旧入口）：

- `tdxfeed/features_5d.py`（`get_limit` 委托）
- `tdxfeed/daban/limit_price.py`（`limit_ratio` / `market_of` / `exchange_prefix` / `limit_up_price` / `limit_down_price` 委托）
- `quant/multifactor/factors.py`（`get_limit` 委托）
- `scripts/plan_e_backtest.py`、`scripts/plan_e_sim_live.py`、`scripts/plan_e_journal.py`
- `scripts/daban_backtest.py`、`mcp/servers/backtest.py`
- `scripts/train_pnl_proxy.py`
- `scripts/v3_simulate.py`、`scripts/v3_simulate_rolling.py`
- `tdxfeed/daban/paper.py`、`tdxfeed/daban/execution_backtest.py`、`tdxfeed/daban/topk_pnl.py`
- `AGENTS.md`（口径段补一句内核位置）

---

### Task 1: 路径与名单两个前置模块

**Files:**
- Create: `tdxfeed/paths.py`
- Create: `tdxfeed/names.py`
- Test: `tests/test_paths_and_names.py`

**Interfaces:**
- Produces:
  - `paths.DATA_ROOT: Path`、`paths.daily_dir() -> Path`、`paths.daily_path(code: str) -> Path`、`paths.minute_path(code: str, period: str) -> Path`、`paths.adj_factor_path(code: str) -> Path`、`paths.hfq_factor_path(code: str) -> Path`、`paths.derived_path(code: str) -> Path`、`paths.universe_path() -> Path`
  - `names.load_universe() -> dict[str, str]`、`names.is_st_name(name: str) -> bool`、`names.name_of(code: str) -> str`

- [ ] **Step 1: 写失败测试**

`tests/test_paths_and_names.py`：

```python
"""路径只定位不 fallback；ST 判定只有一个来源。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tdxfeed import names, paths


def test_daily_path_uses_exchange_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    (tmp_path / "parquet" / "daily").mkdir(parents=True)
    p = paths.daily_path("600519")
    assert p.name == "sh600519.parquet"
    assert p.parent.name == "daily"


def test_bj_code_gets_bj_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.daily_path("920001").name == "bj920001.parquet"
    assert paths.daily_path("bj920001").name == "bj920001.parquet"


def test_adj_and_derived_naming_differ_by_table(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.adj_factor_path("600519").name == "600519.parquet"
    assert paths.hfq_factor_path("600519").name == "600519.parquet"
    assert paths.derived_path("sh600519").name == "sh600519_indicators.parquet"


def test_missing_file_raises_instead_of_falling_back(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        paths.read_path(paths.daily_path("600519"))


def test_is_st_name_covers_star_st_and_spaces():
    assert names.is_st_name("*ST美丽") is True
    assert names.is_st_name("ST 康佳") is True
    assert names.is_st_name("平安银行") is False
    assert names.is_st_name("") is False


def test_load_universe_reads_code_to_name(tmp_path, monkeypatch):
    fp = tmp_path / "all_a_stocks.json"
    fp.write_text(json.dumps({"000001": "平安银行", "000010": "*ST美丽"}), encoding="utf-8")
    monkeypatch.setattr(names, "universe_path", lambda: fp)
    monkeypatch.setattr(names, "_CACHE", None)
    uni = names.load_universe()
    assert uni["000001"] == "平安银行"
    assert names.name_of("sz000010") == "*ST美丽"
    assert names.name_of("999999") == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_paths_and_names.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tdxfeed.paths'`

- [ ] **Step 3: 实现**

`tdxfeed/paths.py`：

```python
"""五张行情表的路径定位：只定位，不 fallback，缺文件就报错。

各表命名不一致是历史事实，不要在这里抹平：
  daily / min*   → 带市场前缀 sh600519.parquet
  adj_factor / hfq_factor → 纯 6 位 600519.parquet
  derived        → 带前缀 + _indicators 后缀 sh600519_indicators.parquet
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("TDX_OUT_ROOT", ROOT / "data"))

_BARE_TABLES = {"adj_factor", "hfq_factor"}


def _six(code: str) -> str:
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return c.zfill(6)


def exchange_prefix(code: str) -> str:
    c = _six(code)
    if c.startswith(("920", "8", "4")):
        return "bj"
    return "sh" if c[0] in "69" else "sz"


def daily_dir() -> Path:
    return DATA_ROOT / "parquet" / "daily"


def daily_path(code: str) -> Path:
    return daily_dir() / f"{exchange_prefix(code)}{_six(code)}.parquet"


def minute_path(code: str, period: str) -> Path:
    p = str(period).strip().lower().lstrip("m") if not str(period).startswith("min") else str(period)
    p = p if p.startswith("min") else f"min{p}"
    return DATA_ROOT / "parquet" / p / f"{exchange_prefix(code)}{_six(code)}.parquet"


def adj_factor_path(code: str) -> Path:
    return DATA_ROOT / "adj_factor" / f"{_six(code)}.parquet"


def hfq_factor_path(code: str) -> Path:
    return DATA_ROOT / "hfq_factor" / f"{_six(code)}.parquet"


def derived_path(code: str) -> Path:
    return DATA_ROOT / "parquet" / "derived" / f"{exchange_prefix(code)}{_six(code)}_indicators.parquet"


def universe_path() -> Path:
    return DATA_ROOT / "all_a_stocks.json"


def read_path(path: Path) -> Path:
    """存在性守卫：把「路径算错」变成立刻可见的异常，而不是空表。"""
    if not Path(path).exists():
        raise FileNotFoundError(str(path))
    return Path(path)
```

`tdxfeed/names.py`：

```python
"""全 A 名单与 ST 判定：scan / predict / daban 的唯一来源。"""
from __future__ import annotations

import json

from .paths import universe_path

_CACHE: dict[str, str] | None = None


def load_universe() -> dict[str, str]:
    """code(6 位) -> name。文件缺失返回空字典，由调用方决定是否致命。"""
    global _CACHE
    if _CACHE is None:
        fp = universe_path()
        _CACHE = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
    return _CACHE


def is_st_name(name: str) -> bool:
    return "ST" in str(name).replace(" ", "").upper()


def name_of(code: str) -> str:
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return load_universe().get(c.zfill(6), "")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_paths_and_names.py -q`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/paths.py tdxfeed/names.py tests/test_paths_and_names.py
git commit -m "feat(paths): 五张表路径定位 + 全 A 名单作为 ST 唯一来源"
```

---

### Task 2: 涨跌幅规则收进 limits.py

**Files:**
- Create: `tdxfeed/execution/__init__.py`、`tdxfeed/execution/limits.py`
- Test: `tests/test_execution_limits.py`

**Interfaces:**
- Produces:
  - `limit_ratio(code: str, *, is_st: bool = False, on_date=None) -> Decimal`
  - `limit_threshold(code: str, *, is_st: bool = False, on_date=None, buffer: float = 0.005) -> float`
  - `limit_up_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float`
  - `limit_down_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float`
  - `is_sealed(price: float, limit_price: float) -> bool`
  - `is_limit_up_ret(ret: float, limit: float) -> bool`
  - `open_is_limit_up(open_price, prev_close, code, *, is_st=False, on_date=None) -> bool`
  - `market_of(code: str) -> str`、`as_date(value) -> date | None`
  - `LimitRule`（Task 6 用）

- [ ] **Step 1: 写失败测试**

`tests/test_execution_limits.py`：

```python
"""两套合同必须分开：判定阈值留 0.5% 缓冲，挂单价用精确比例取整到分。"""
from __future__ import annotations

from decimal import Decimal

from tdxfeed.execution.limits import (
    LimitRule,
    is_limit_up_ret,
    is_sealed,
    limit_down_price,
    limit_ratio,
    limit_threshold,
    limit_up_price,
    market_of,
    open_is_limit_up,
)


def test_market_classification():
    assert market_of("sh600519") == "main"
    assert market_of("sz000001") == "main"
    assert market_of("sz300750") == "gem"
    assert market_of("sh688981") == "gem"
    assert market_of("bj920001") == "bse"
    assert market_of("830799") == "bse"


def test_exact_ratio_is_decimal():
    assert limit_ratio("600519") == Decimal("0.10")
    assert limit_ratio("300750") == Decimal("0.20")
    assert limit_ratio("920001") == Decimal("0.30")


def test_main_st_ratio_follows_merger_date():
    assert limit_ratio("600519", is_st=True, on_date="2026-07-03") == Decimal("0.05")
    assert limit_ratio("600519", is_st=True, on_date="2026-07-06") == Decimal("0.10")
    assert limit_ratio("600519", is_st=True) == Decimal("0.10")


def test_gem_st_is_always_20pct():
    assert limit_ratio("300750", is_st=True, on_date="2020-01-02") == Decimal("0.20")


def test_threshold_equals_ratio_minus_buffer():
    assert limit_threshold("600519") == 0.095
    assert limit_threshold("300750") == 0.195
    assert limit_threshold("920001") == 0.295
    assert limit_threshold("600519", is_st=True, on_date="2026-07-03") == 0.045


def test_prices_round_to_cent_half_up():
    assert limit_up_price(10.005, Decimal("0.10")) == 11.01
    assert limit_down_price(10.00, Decimal("0.10")) == 9.0
    assert limit_up_price(0, Decimal("0.10")) == 0.0


def test_open_limit_up_uses_threshold_contract():
    assert open_is_limit_up(11.0, 10.0, "600519") is True
    assert open_is_limit_up(10.94, 10.0, "600519") is False   # 9.4% < 9.5%
    assert open_is_limit_up(11.94, 10.0, "300750") is False   # 19.4% < 19.5%
    assert open_is_limit_up(12.0, 10.0, "300750") is True     # 20% 板块 +20% 即涨停


def test_sealed_uses_exact_price_contract():
    assert is_sealed(10.995, 11.0) is True    # 正好卡在容差内（半分钱）
    assert is_sealed(10.99, 11.0) is False    # 差一分钱，超出半分钱容差


def test_limit_rule_bundles_code_and_name():
    rule = LimitRule(code="600519", is_st=True)
    assert rule.up_price(10.0) == 11.0
    assert rule.down_price(10.0, on_date="2026-07-03") == 9.5
    assert rule.is_open_limit_up(11.0, 10.0, on_date="2026-07-06") is True
    assert rule.is_sealed(10.995, 10.0, on_date="2026-07-06") is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_execution_limits.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tdxfeed.execution'`

- [ ] **Step 3: 实现**

`tdxfeed/execution/__init__.py`（本任务先只导出 limits，后面任务逐个补）：

```python
"""回测结算内核：两套口径（单笔 TP/SL、打板持有期）+ 共用组件。"""
from .limits import (
    LimitRule,
    is_limit_up_ret,
    is_sealed,
    limit_down_price,
    limit_ratio,
    limit_threshold,
    limit_up_price,
    market_of,
    open_is_limit_up,
)

__all__ = [
    "LimitRule",
    "is_limit_up_ret",
    "is_sealed",
    "limit_down_price",
    "limit_ratio",
    "limit_threshold",
    "limit_up_price",
    "market_of",
    "open_is_limit_up",
]
```

`tdxfeed/execution/limits.py`：

```python
"""涨跌幅规则：精确比例（算价）与判定阈值（认板）是两套合同，不可混用。

| 用途 | 接口 | 值 |
|---|---|---|
| 挂单/涨跌停价 | limit_ratio + limit_up_price | 0.10 / 0.20 / 0.30，Decimal 取整到分 |
| 「是否涨停」判定 | limit_threshold | 比例 − 0.5% 缓冲，0.095 / 0.195 / 0.295 |

主板 ST 在 2026-07-06 与普通票并轨（±5% → ±10%），因此比例必须能按交易日判；
不传 on_date 按现行规则。创业板/科创板/北交所的 ST 不单独设档。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

MARKET_MAIN = "main"
MARKET_GEM = "gem"
MARKET_BSE = "bse"

LIMIT_RATIO: dict[str, Decimal] = {
    MARKET_MAIN: Decimal("0.10"),
    MARKET_GEM: Decimal("0.20"),
    MARKET_BSE: Decimal("0.30"),
}
LIMIT_RATIO_ST_MAIN_OLD = Decimal("0.05")
LIMIT_RATIO_ST_MAIN = Decimal("0.10")
MAIN_ST_RULE_CHANGE_DATE = date(2026, 7, 6)
DEFAULT_THRESHOLD_BUFFER = 0.005
RET_TOLERANCE = 0.001

_CENT = Decimal("0.01")
_ONE = Decimal("1")


def normalize_code(code: str) -> str:
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return c.zfill(6)


def market_of(code: str) -> str:
    c = normalize_code(code)
    if c.startswith(("920", "8", "4")):
        return MARKET_BSE
    if c.startswith(("300", "301", "688", "689")):
        return MARKET_GEM
    return MARKET_MAIN


def as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("-", "")[:8]
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    return None


def limit_ratio(code: str, *, is_st: bool = False, on_date=None) -> Decimal:
    market = market_of(code)
    if market == MARKET_MAIN and is_st:
        d = as_date(on_date)
        if d is not None and d < MAIN_ST_RULE_CHANGE_DATE:
            return LIMIT_RATIO_ST_MAIN_OLD
        return LIMIT_RATIO_ST_MAIN
    return LIMIT_RATIO[market]


def limit_threshold(code: str, *, is_st: bool = False, on_date=None,
                    buffer: float = DEFAULT_THRESHOLD_BUFFER) -> float:
    return float(limit_ratio(code, is_st=is_st, on_date=on_date) - Decimal(str(buffer)))


def _quantize_cents(raw: Decimal) -> float:
    return float(raw.quantize(_CENT, rounding=ROUND_HALF_UP))


def _as_decimal(ratio: Decimal | float) -> Decimal:
    return ratio if isinstance(ratio, Decimal) else Decimal(str(ratio))


def limit_up_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    if prev_close is None or prev_close <= 0:
        return 0.0
    return _quantize_cents(Decimal(str(prev_close)) * (_ONE + _as_decimal(ratio)))


def limit_down_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    if prev_close is None or prev_close <= 0:
        return 0.0
    return _quantize_cents(Decimal(str(prev_close)) * (_ONE - _as_decimal(ratio)))


def is_sealed(price: float, limit_price: float) -> bool:
    """已封住涨停价（容差半分钱，规避浮点误差）。"""
    if limit_price <= 0 or price <= 0:
        return False
    return price >= limit_price - 0.005


def is_limit_up_ret(ret: float, limit: float) -> bool:
    return ret >= (limit - RET_TOLERANCE)


def open_is_limit_up(open_price: float, prev_close: float, code: str, *,
                     is_st: bool = False, on_date=None) -> bool:
    """开盘即涨停（阈值合同）：判定是否「买不进」，不是挂单价。"""
    if prev_close is None or prev_close <= 0:
        return False
    limit = limit_threshold(code, is_st=is_st, on_date=on_date)
    return is_limit_up_ret(open_price / prev_close - 1.0, limit)


@dataclass(frozen=True)
class LimitRule:
    """把「哪只票、是不是 ST、容差多少」绑在一起，按日供给比例。"""
    code: str
    is_st: bool = False
    tol: float = 0.005

    def ratio(self, on_date=None) -> Decimal:
        return limit_ratio(self.code, is_st=self.is_st, on_date=on_date)

    def up_price(self, prev_close: float, on_date=None) -> float:
        return limit_up_price(prev_close, self.ratio(on_date))

    def down_price(self, prev_close: float, on_date=None) -> float:
        return limit_down_price(prev_close, self.ratio(on_date))

    def is_open_limit_up(self, open_price: float, prev_close: float, on_date=None) -> bool:
        return open_is_limit_up(open_price, prev_close, self.code,
                               is_st=self.is_st, on_date=on_date)

    def is_sealed(self, price: float, prev_close: float, on_date=None) -> bool:
        return is_sealed(price, self.up_price(prev_close, on_date))

    def is_sealed_at(self, price: float, limit_price: float) -> bool:
        return is_sealed(price, limit_price)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_execution_limits.py -q`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/execution/__init__.py tdxfeed/execution/limits.py tests/test_execution_limits.py
git commit -m "feat(execution): 涨跌幅两套合同收进 limits.py（比例取整 vs 判定阈值）"
```

---

### Task 3: CostModel 与 Fill

**Files:**
- Create: `tdxfeed/execution/costs.py`
- Test: `tests/test_execution_costs.py`

**Interfaces:**
- Produces:
  - `CostModel(commission=0.0003, min_commission=0.0, stamp_sell=0.0, slippage=0.0010)`
  - `.buy(price, shares) -> Fill`、`.sell(price, shares) -> Fill`
  - `.round_trip(entry, exit_price) -> float`（比例式，要求 `min_commission == 0`）
  - `.cash_pnl(entry, exit_price, shares, *, base=None) -> float`（绝对金额式，支持最低佣金）
  - `Fill(price, shares, amount, fee, tax)` 带 `cash_out` / `cash_in` 属性
  - `PLAN_E_COSTS`（`commission=3e-4, slippage=1e-3`）、`DABAN_COSTS`（`commission=2.5e-4, stamp_sell=5e-4, slippage=5e-4`）

- [ ] **Step 1: 写失败测试**

`tests/test_execution_costs.py`：

```python
"""费用口径只有两个合同：比例式（无最低佣金）与金额式（有最低佣金）。"""
from __future__ import annotations

import pytest

from tdxfeed.execution.costs import DABAN_COSTS, PLAN_E_COSTS, CostModel


def test_round_trip_matches_plan_e_formula_exactly():
    entry, exit_price = 10.0, 13.0
    fee, slip = 0.0003, 0.0010
    expected = (exit_price * (1 - slip) * (1 - fee)
                / (entry * (1 + slip) * (1 + fee)) - 1)
    assert CostModel(commission=fee, slippage=slip).round_trip(entry, exit_price) == expected


def test_round_trip_without_cost_is_gross():
    assert CostModel(commission=0.0, slippage=0.0).round_trip(10.0, 11.0) == pytest.approx(0.1)


def test_round_trip_refuses_min_commission():
    with pytest.raises(ValueError):
        CostModel(min_commission=5.0).round_trip(10.0, 11.0)


def test_sell_applies_stamp_only_on_sell_side():
    cm = CostModel(commission=0.0, stamp_sell=0.001, slippage=0.0)
    assert cm.buy(10.0, 1000).fee == 0.0
    assert cm.sell(10.0, 1000).tax == 10.0


def test_cash_pnl_reproduces_paper_net_actual():
    # paper._net_actual 的公式：base=买入金额（未含滑点），佣金双边各有最低值
    cm = CostModel(commission=2.5e-4, min_commission=5.0, stamp_sell=5e-4, slippage=0.0)
    fill, sell, shares = 10.0, 11.0, 1000
    buy_amt, sell_amt = fill * shares, sell * shares
    commission = max(5.0, buy_amt * 2.5e-4) + max(5.0, sell_amt * 2.5e-4)
    expected = (sell_amt - buy_amt - commission - sell_amt * 5e-4) / buy_amt
    assert cm.cash_pnl(fill, sell, shares) == pytest.approx(expected, abs=1e-12)


def test_cash_pnl_uses_fill_not_slipped_price_as_base():
    cm = CostModel(commission=0.0, slippage=0.001)
    pnl = cm.cash_pnl(10.0, 11.0, 1000)
    assert pnl == pytest.approx((11.0 * 0.999 - 10.0 * 1.001) / 10.0)


def test_presets():
    assert PLAN_E_COSTS.round_trip(10.0, 10.0) < 0
    assert DABAN_COSTS.stamp_sell == 5e-4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_execution_costs.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tdxfeed.execution.costs'`

- [ ] **Step 3: 实现**

`tdxfeed/execution/costs.py`：

```python
"""交易成本：佣金/印花税/滑点。

两个合同不能合并：
  round_trip —— 比例式，用在「以收益率为输出」的回测（plan_e / sim_live / topk / daban）。
                它没有最低佣金概念：一笔收益率的 5 元下限取决于本金，本金不在入参里。
  cash_pnl   —— 金额式，用在有持仓手数与最低佣金的账本（daban/paper）。
                分母是买入金额（未含滑点），与 paper._net_actual 的 base 定义一致。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    price: float      # 含滑点的成交价
    shares: int
    amount: float     # price * shares
    fee: float
    tax: float = 0.0

    @property
    def cash_out(self) -> float:
        return self.amount + self.fee

    @property
    def cash_in(self) -> float:
        return self.amount - self.fee - self.tax


@dataclass(frozen=True)
class CostModel:
    commission: float = 0.0003
    min_commission: float = 0.0
    stamp_sell: float = 0.0
    slippage: float = 0.0010

    def buy(self, price: float, shares: int) -> Fill:
        px = float(price) * (1.0 + self.slippage)
        amount = px * int(shares)
        fee = max(self.min_commission, amount * self.commission)
        return Fill(price=px, shares=int(shares), amount=amount, fee=fee)

    def sell(self, price: float, shares: int) -> Fill:
        px = float(price) * (1.0 - self.slippage)
        amount = px * int(shares)
        fee = max(self.min_commission, amount * self.commission)
        return Fill(price=px, shares=int(shares), amount=amount, fee=fee,
                    tax=amount * self.stamp_sell)

    def round_trip(self, entry: float, exit_price: float) -> float:
        if self.min_commission > 0:
            raise ValueError("round_trip 不支持最低佣金：本金不在入参里，改用 cash_pnl")
        entry_eff = float(entry) * (1.0 + self.slippage)
        exit_eff = float(exit_price) * (1.0 - self.slippage)
        return (exit_eff * (1.0 - self.commission - self.stamp_sell)
                / (entry_eff * (1.0 + self.commission)) - 1.0)

    def cash_pnl(self, entry: float, exit_price: float, shares: int,
                 *, base: float | None = None) -> float:
        b = self.buy(entry, shares)
        s = self.sell(exit_price, shares)
        denom = base if base is not None else float(entry) * int(shares)
        if denom <= 0:
            return float("nan")
        return (s.cash_in - b.cash_out) / denom


# 方案 E 全链路：佣金 0.03% + 滑点 0.10%，双边，无印花税（口径见 AGENTS.md）
PLAN_E_COSTS = CostModel(commission=0.0003, slippage=0.0010)

# 打板研究：佣金 2.5bp + 印花税 5bp（仅卖出）+ 滑点 5bp
DABAN_COSTS = CostModel(commission=0.00025, stamp_sell=0.0005, slippage=0.0005)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_execution_costs.py -q`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/execution/costs.py tests/test_execution_costs.py
git commit -m "feat(execution): CostModel 两个费用合同（比例式 / 含最低佣金金额式）"
```

---

### Task 4: TradeResult 与 bars 归一

**Files:**
- Create: `tdxfeed/execution/result.py`、`tdxfeed/execution/bars.py`
- Test: `tests/test_execution_bars.py`

**Interfaces:**
- Produces:
  - `TradeResult(entry_idx, entry_date, entry_price, exit_idx, exit_date, exit_price, reason, gross_ret, net_ret, max_gain=0.0, max_loss=0.0, daily=(), status="ok", deferred=0, notes=())`（**15 个字段**，`notes` 用于记录降级/顺延等说明）
  - `bars.as_bars(df, *, suffix="") -> pd.DataFrame`：返回只含 `date/open/high/low/close/volume` **六列**的归一帧（`reset_index` 到 0，`date` 原样透传、不新增列）；带 `suffix` 时五个 OHLCV 列**必须全有后缀**，缺一列即 `KeyError`（不允许静默回退到裸列，否则复权/不复权会混用）
  - `bars.date_str(value) -> str`：日期字符串化的唯一入口（`Timestamp` / `date32` / `"20260105"` → `YYYY-MM-DD`）；内核逐行调用它，不依赖 `as_bars` 产出日期列

- [ ] **Step 1: 写失败测试**

`tests/test_execution_bars.py`：

```python
"""内核只认 open/high/low/close/volume；_adj 后缀与 date 类型在入口归一。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.execution.bars import as_bars, date_str
from tdxfeed.execution.result import TradeResult


def test_as_bars_renames_adj_suffix():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=2),
        "open_adj": [1.0, 2.0], "high_adj": [1.1, 2.1],
        "low_adj": [0.9, 1.9], "close_adj": [1.0, 2.0],
        "volume": [100, 200],
    })
    out = as_bars(df, suffix="_adj")
    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert out["close"].tolist() == [1.0, 2.0]


def test_as_bars_rejects_missing_column():
    df = pd.DataFrame({"date": [1], "open": [1.0], "high": [1.0], "low": [1.0]})
    with pytest.raises(KeyError):
        as_bars(df)


def test_as_bars_keeps_row_order_and_index_reset():
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=3),
        "open": [3.0, 1.0, 2.0], "high": [3.0, 1.0, 2.0],
        "low": [3.0, 1.0, 2.0], "close": [3.0, 1.0, 2.0], "volume": [1, 1, 1],
    }, index=[7, 8, 9])
    out = as_bars(df)
    assert list(out.index) == [0, 1, 2]
    assert out["open"].tolist() == [3.0, 1.0, 2.0]


def test_date_str_handles_date32_and_timestamp():
    assert date_str(pd.Timestamp("2026-01-05")) == "2026-01-05"
    assert date_str("20260105") == "2026-01-05"


def test_trade_result_defaults():
    r = TradeResult(entry_idx=1, entry_date="2026-01-02", entry_price=10.0,
                    exit_idx=3, exit_date="2026-01-06", exit_price=13.0,
                    reason="TP", gross_ret=0.3, net_ret=0.29)
    assert r.status == "ok"
    assert r.deferred == 0
    assert r.daily == ()
    assert r.max_gain == 0.0 and r.max_loss == 0.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_execution_bars.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tdxfeed.execution.bars'`

- [ ] **Step 3: 实现**

`tdxfeed/execution/result.py`：

```python
"""结算结果：两个内核共用同一形状，避免各调用点再拼 dict。"""
from __future__ import annotations

from dataclasses import dataclass, field

OK = "ok"


@dataclass(frozen=True)
class TradeResult:
    entry_idx: int
    entry_date: str
    entry_price: float
    exit_idx: int
    exit_date: str
    exit_price: float
    reason: str                     # SL | TP | TIME | END | <卖出时点>
    gross_ret: float
    net_ret: float
    max_gain: float = 0.0
    max_loss: float = 0.0
    daily: tuple[tuple[str, float], ...] = ()
    status: str = OK                # ok | unbuyable_open_limit | suspended | no_exit | deferred_unsold
    deferred: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
```

`tdxfeed/execution/bars.py`：

```python
"""把各调用点的行情帧归一成内核认识的五列，只做形状转换，不做筛选。"""
from __future__ import annotations

import pandas as pd

OHLCV = ("open", "high", "low", "close", "volume")


def date_str(value) -> str:
    s = str(value).replace("-", "")[:8]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return str(value)[:10]


def as_bars(df: pd.DataFrame, *, suffix: str = "") -> pd.DataFrame:
    """返回固定列序、从 0 开始的副本。缺列抛 KeyError，不静默补零。"""
    cols = {name: (f"{name}{suffix}" if f"{name}{suffix}" in df.columns else name)
            for name in OHLCV}
    missing = [name for name, src in cols.items() if src not in df.columns]
    if missing:
        raise KeyError(f"缺列 {missing}（suffix={suffix!r}）")
    out = pd.DataFrame({
        "date": df["date"].to_numpy(),
        **{name: df[src].to_numpy(dtype=float) for name, src in cols.items()},
    })
    return out.reset_index(drop=True)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_execution_bars.py -q`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/execution/result.py tdxfeed/execution/bars.py tests/test_execution_bars.py
git commit -m "feat(execution): TradeResult 与 bars 列名归一"
```

---

### Task 5: 内核 A — check_exit 与 settle_single_trade

**Files:**
- Create: `tdxfeed/execution/single.py`
- Test: `tests/test_execution_single.py`
- Modify: `tdxfeed/execution/__init__.py`（追加导出）

**Interfaces:**
- Consumes: `limits`（无）、`costs.CostModel`、`bars.date_str`、`result.TradeResult`
- Produces:
  - `SingleTradePlan(judge="intrabar", tp=0.30, sl=-0.10, hold_days=10, sl_first=True)`
  - `ExitHit(price: float, reason: str)`
  - `exit_levels(entry_price, plan) -> tuple[float | None, float | None]`
  - `check_exit(entry_price, *, open_, high, low, close, plan) -> ExitHit | None`
  - `entry_index(bars, signal_idx, lag) -> int | None`
  - `settle_single_trade(bars, entry_idx, plan, costs, *, buyable=None, insufficient="skip") -> TradeResult | None`

**语义（不可配）**：`sl_first=True` 时同根双触发结算亏损侧；跳空穿越按 `min(sl_lvl, open)` / `max(tp_lvl, open)`；超时按第 `hold_days` 根（含入场根计数）收盘成交；`daily` 在入场根与出场根各扣一次 `commission + slippage`。

- [ ] **Step 1: 写失败测试**

`tests/test_execution_single.py`：

```python
"""内核 A 的三条硬口径：亏损侧优先、跳空按开盘、超时收官。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.single import (
    ExitHit,
    SingleTradePlan,
    check_exit,
    entry_index,
    exit_levels,
    settle_single_trade,
)

FREE = CostModel(commission=0.0, slippage=0.0)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows)),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [1_000_000] * len(rows),
    })


def test_exit_levels_from_entry_price():
    tp_lvl, sl_lvl = exit_levels(10.0, SingleTradePlan(tp=0.30, sl=-0.10))
    assert tp_lvl == pytest.approx(13.0)
    assert sl_lvl == pytest.approx(9.0)


def test_exit_levels_allow_disabled_side():
    assert exit_levels(10.0, SingleTradePlan(tp=None, sl=-0.10))[0] is None


def test_double_touch_settles_loss_side():
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0,
                     plan=SingleTradePlan())
    assert hit == ExitHit(price=9.0, reason="SL")


def test_double_touch_tp_first_keeps_profit_side():
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0,
                     plan=SingleTradePlan(sl_first=False))
    assert hit.reason == "TP"


def test_gap_down_through_stop_fills_at_open():
    hit = check_exit(10.0, open_=8.0, high=8.5, low=7.5, close=8.0, plan=SingleTradePlan())
    assert hit == ExitHit(price=8.0, reason="SL")


def test_gap_up_through_target_fills_at_open():
    hit = check_exit(10.0, open_=14.0, high=14.2, low=13.8, close=14.0, plan=SingleTradePlan())
    assert hit == ExitHit(price=14.0, reason="TP")


def test_close_only_judges_on_close():
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0,
                     plan=SingleTradePlan(judge="close_only"))
    assert hit is None
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=13.0,
                     plan=SingleTradePlan(judge="close_only"))
    assert hit == ExitHit(price=13.0, reason="TP")


def test_entry_index_respects_lag_and_bounds():
    df = _bars([(10.0, 10.0, 10.0, 10.0)] * 5)
    assert entry_index(df, 0, lag=1) == 1
    assert entry_index(df, 3, lag=1) == 4
    assert entry_index(df, 4, lag=1) is None


def test_settle_enters_next_bar_and_times_out_at_close_cost_free():
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),     # 0 信号日
        (10.0, 10.0, 10.0, 10.0),     # 1 入场日 open=10
        (10.5, 10.6, 10.4, 10.5),     # 2
        (11.0, 11.1, 10.9, 11.0),     # 3 超时（hold_days=3 ⇒ 第 3 根收盘）
    ])
    r = settle_single_trade(df, 1, SingleTradePlan(tp=0.30, sl=-0.10, hold_days=3), FREE)
    assert r.reason == "TIME"
    assert r.exit_idx == 3
    assert r.exit_price == 11.0
    assert r.net_ret == pytest.approx(0.1)


def test_daily_series_has_entry_and_exit_cost_bars():
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.0, 10.0, 10.0),
        (13.0, 13.2, 12.9, 13.0),
    ])
    cm = CostModel(commission=0.0003, slippage=0.0010)
    r = settle_single_trade(df, 1, SingleTradePlan(tp=0.30, sl=-0.10, hold_days=5), cm)
    assert [d for d, _ in r.daily] == ["2026-01-02", "2026-01-03"]
    # 入场日只扣一次 side cost，出场日再扣一次
    assert r.daily[0][1] == pytest.approx(-(0.0003 + 0.0010))
    assert r.daily[1][1] == pytest.approx(13.0 / 10.0 - 1 - (0.0003 + 0.0010))


def test_insufficient_window_skips_by_default():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0)])
    assert settle_single_trade(df, 1, SingleTradePlan(), FREE) is None


def test_insufficient_window_can_close_at_last_bar():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.5, 10.6, 10.4, 10.5)])
    r = settle_single_trade(df, 1, SingleTradePlan(hold_days=10), FREE,
                            insufficient="last_close")
    assert r.reason == "END"
    assert r.exit_idx == 2


def test_buyable_callback_can_block_entry():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.5, 10.6, 10.4, 10.5)])
    assert settle_single_trade(df, 1, SingleTradePlan(), FREE,
                               buyable=lambda i: False) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_execution_single.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tdxfeed.execution.single'`

- [ ] **Step 3: 实现**

`tdxfeed/execution/single.py`：

```python
"""内核 A：单笔 TP/SL 结算。

入场时点不进内核——它是策略差异（plan_e 在 T+1 确认后于 T+2 开盘入场，
sim_live 在信号日次日开盘入场）。调用方算好 entry_idx，内核只从该根往后跑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import pandas as pd

from .bars import date_str
from .costs import CostModel
from .result import TradeResult


@dataclass(frozen=True)
class SingleTradePlan:
    judge: Literal["intrabar", "close_only"] = "intrabar"
    tp: float | None = 0.30
    sl: float | None = -0.10
    hold_days: int = 10
    sl_first: bool = True


@dataclass(frozen=True)
class ExitHit:
    price: float
    reason: str


def exit_levels(entry_price: float, plan: SingleTradePlan) -> tuple[float | None, float | None]:
    tp_lvl = entry_price * (1 + plan.tp) if plan.tp is not None else None
    sl_lvl = entry_price * (1 + plan.sl) if plan.sl is not None else None
    return tp_lvl, sl_lvl


def check_exit(entry_price: float, *, open_: float, high: float, low: float,
               close: float, plan: SingleTradePlan) -> ExitHit | None:
    """单根 K 线的出场判定。日内高低点证明不了先后，双触发取 plan.sl_first 指定侧。"""
    tp_lvl, sl_lvl = exit_levels(entry_price, plan)
    if plan.judge == "close_only":
        ret = close / entry_price - 1.0 if entry_price > 0 else 0.0
        if plan.sl_first:
            if sl_lvl is not None and ret <= plan.sl:
                return ExitHit(price=close, reason="SL")
            if tp_lvl is not None and ret >= plan.tp:
                return ExitHit(price=close, reason="TP")
        else:
            if tp_lvl is not None and ret >= plan.tp:
                return ExitHit(price=close, reason="TP")
            if sl_lvl is not None and ret <= plan.sl:
                return ExitHit(price=close, reason="SL")
        return None

    hit_sl = sl_lvl is not None and low <= sl_lvl
    hit_tp = tp_lvl is not None and high >= tp_lvl
    if not (hit_sl or hit_tp):
        return None
    open_j = open_ if open_ > 0 else None
    if hit_sl and (plan.sl_first or not hit_tp):
        price = min(sl_lvl, open_j) if open_j is not None else sl_lvl
        return ExitHit(price=price, reason="SL")
    price = max(tp_lvl, open_j) if open_j is not None else tp_lvl
    return ExitHit(price=price, reason="TP")


def entry_index(bars: pd.DataFrame, signal_idx: int, lag: int) -> int | None:
    idx = signal_idx + lag
    return idx if 0 <= idx < len(bars) else None


def settle_single_trade(
    bars: pd.DataFrame,
    entry_idx: int,
    plan: SingleTradePlan,
    costs: CostModel,
    *,
    buyable: Callable[[int], bool] | None = None,
    insufficient: Literal["skip", "last_close"] = "skip",
) -> TradeResult | None:
    n = len(bars)
    if entry_idx < 0 or entry_idx >= n:
        return None
    entry = float(bars["open"].iloc[entry_idx])
    if entry <= 0:
        return None
    if buyable is not None and not buyable(entry_idx):
        return None

    start = entry_idx + 1
    end = min(entry_idx + plan.hold_days, n)
    if end <= start:
        return None
    if end - start < plan.hold_days and insufficient == "skip":
        return None

    exit_idx, reason, exit_price = end - 1, "TIME", None
    for j in range(start, end):
        hit = check_exit(entry, open_=float(bars["open"].iloc[j]),
                         high=float(bars["high"].iloc[j]),
                         low=float(bars["low"].iloc[j]),
                         close=float(bars["close"].iloc[j]), plan=plan)
        if hit is not None:
            exit_idx, reason, exit_price = j, hit.reason, hit.price
            break

    high_max = float(bars["high"].iloc[entry_idx:exit_idx + 1].max())
    low_min = float(bars["low"].iloc[entry_idx:exit_idx + 1].min())
    if reason == "TIME":
        exit_price = float(bars["close"].iloc[exit_idx])
    if exit_price is None or exit_price <= 0:
        return None

    gross = exit_price / entry - 1.0
    net = costs.round_trip(entry, exit_price)

    side_cost = costs.commission + costs.slippage + costs.stamp_sell
    daily: list[tuple[str, float]] = []
    prev_close = entry
    for k in range(entry_idx, exit_idx + 1):
        close_k = exit_price if k == exit_idx else float(bars["close"].iloc[k])
        r = close_k / prev_close - 1.0 if prev_close > 0 else 0.0
        if k == entry_idx:
            r -= costs.commission + costs.slippage
        if k == exit_idx:
            r -= side_cost
        daily.append((date_str(bars["date"].iloc[k]), r))
        prev_close = close_k

    return TradeResult(
        entry_idx=entry_idx,
        entry_date=date_str(bars["date"].iloc[entry_idx]),
        entry_price=entry,
        exit_idx=exit_idx,
        exit_date=date_str(bars["date"].iloc[exit_idx]),
        exit_price=exit_price,
        reason=reason,
        gross_ret=gross,
        net_ret=net,
        max_gain=(high_max - entry) / entry,
        max_loss=(low_min - entry) / entry,
        daily=tuple(daily),
    )
```

注意两处必须与 plan_e 逐位对齐，否则 Task 8 的 md5 闸门会红：
1. 入场根扣 `commission + slippage`（**不含** stamp），出场根扣三者合计 —— plan_e 的 `side_cost = fee + eff_slip`，印花税为 0。
2. `insufficient="skip"` 的判据是「入场后可判定的根数不足 `hold_days`」，与 plan_e 的 `_has_exit_window`（要求 ≥3 根）不同：plan_e 是「至少 3 根」，不是「满 hold_days」。**Task 8 必须让 plan_e 自己保留 `_has_exit_window` 预检**，`insufficient` 保持 `"skip"` 只在 0 根时触发。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_execution_single.py -q`
Expected: 15 passed

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/execution/single.py tdxfeed/execution/__init__.py tests/test_execution_single.py
git commit -m "feat(execution): 内核 A（check_exit / settle_single_trade）"
```

---

### Task 6: 内核 B — 打板持有期

**Files:**
- Create: `tdxfeed/execution/hold.py`
- Test: `tests/test_execution_hold.py`
- Modify: `tdxfeed/execution/__init__.py`（追加导出）

**Interfaces:**
- Consumes: `limits.LimitRule`、`costs.CostModel`、`bars.date_str`、`result.TradeResult`
- Produces:
  - `HoldPlan(buy="next_open", sell="next_close", max_defer=3)`
  - `settle_hold_trade(bars, signal_idx, plan, costs, rule) -> TradeResult`

**语义**：买入日开盘已是涨停价（`rule.is_open_limit_up`）→ `status="unbuyable_open_limit"`；买入日 `open/volume<=0` → `suspended`；卖出日收盘跌停 → 顺延，每个顺延日先试开盘（未跌停则以开盘卖），再试收盘，最后一日强制按收盘卖；`deferred` 记顺延天数；`max_defer=0` 表示不顺延，跌停也按当日收盘强制成交（`reason` 带 `_forced`）。

**不做**：`HoldPlan` 不带止损字段（打板持有期不设止损；单笔 TP/SL 是内核 A 的合同）；不做金额口径（`shares`/现金盈亏走 `CostModel.cash_pnl`，调用方自己算）。开盘卖模式（`next_open`/`t_plus_2_open`）按开盘无条件成交——既有 daban `topk` 就是这样建模的，迁移必须保数，跌停不可卖只对收盘卖模式生效（在 docstring 里写明这是已知建模简化）。

- [ ] **Step 1: 写失败测试**

`tests/test_execution_hold.py`：

```python
"""内核 B：买不进要显式说出来，跌停卖不出要顺延而不是当没发生。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.hold import HoldPlan, settle_hold_trade
from tdxfeed.execution.limits import LimitRule

FREE = CostModel(commission=0.0, slippage=0.0)
RULE = LimitRule(code="600519")


def _bars(rows):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(rows)),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [r[4] if len(r) > 4 else 1_000_000 for r in rows],
    })


def test_buy_next_open_sell_next_close():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4), (10.4, 10.6, 10.2, 10.5)])
    r = settle_hold_trade(df, 0, HoldPlan(), FREE, RULE)
    assert r.entry_idx == 1 and r.entry_price == 10.0
    assert r.exit_idx == 1 and r.exit_price == 10.4
    assert r.reason == "t1_close"


def test_open_limit_up_is_unbuyable():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (11.0, 11.0, 11.0, 11.0), (11.1, 11.2, 11.0, 11.1)])
    r = settle_hold_trade(df, 0, HoldPlan(), FREE, RULE)
    assert r is not None and r.status == "unbuyable_open_limit"
    assert r.entry_idx == 1


def test_suspended_entry():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0, 0), (10.1, 10.2, 10.0, 10.1)])
    r = settle_hold_trade(df, 0, HoldPlan(), FREE, RULE)
    assert r.status == "suspended"


def test_limit_down_close_defers_and_limit_down_open_blocks_open_sell():
    # 买入 10.0；T+1 收盘跌停 9.0；T+2 前收 9.0 → 跌停价 8.1，开盘恰在 8.1（卖不出），
    # 收盘拉回 9.2 → 按 T+2 收盘卖
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.1, 9.0, 9.0),
        (8.1, 9.3, 8.1, 9.2),
        (9.2, 9.3, 9.1, 9.2),
    ])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=2), FREE, RULE)
    assert r.exit_idx == 2
    assert r.exit_price == 9.2
    assert r.reason == "t2_close"
    assert r.deferred == 1


def test_defer_day_can_sell_at_open_when_open_not_limit_down():
    # T+1 收盘跌停；T+2 开盘 9.1 未跌停 → 按 T+2 开盘卖
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.1, 9.0, 9.0),
        (9.1, 9.3, 9.0, 9.2),
    ])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=2), FREE, RULE)
    assert r.exit_idx == 2
    assert r.reason == "t2_open"
    assert r.exit_price == 9.1


def test_last_defer_day_forces_close_even_at_limit_down():
    df = _bars([
        (10.0, 10.0, 10.0, 10.0),
        (10.0, 10.1, 9.0, 9.0),
        (8.1, 8.2, 8.1, 8.1),
    ])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=1), FREE, RULE)
    assert r.exit_idx == 2
    assert r.reason == "t2_close_forced"
    assert r.exit_price == 8.1


def test_t_plus_2_open_sell():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4), (10.6, 10.8, 10.5, 10.7)])
    r = settle_hold_trade(df, 0, HoldPlan(sell="t_plus_2_open"), FREE, RULE)
    assert r.exit_idx == 2 and r.exit_price == 10.6
    assert r.reason == "t2_open"


def test_signal_close_buy_at_first_bar_uses_entry_price_as_prev_close():
    """signal_idx=0 且卖点也在第 0 根时，`exit_idx-1` 为负——不得绕到最后一根取前收。"""
    # 若误取最后一根收盘（100）当前收，跌停价变 90，第 0 根的 10.0 会被当成跌停而顺延
    df = _bars([(10.0, 10.0, 10.0, 10.0), (100.0, 100.0, 100.0, 100.0)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_close"), FREE, RULE)
    assert r.status == "ok" and r.reason == "t1_close"
    assert r.exit_idx == 0 and r.exit_price == 10.0


def test_missing_exit_bar_returns_no_exit():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4)])
    r = settle_hold_trade(df, 0, HoldPlan(sell="t_plus_2_open"), FREE, RULE)
    assert r.status == "no_exit"


def test_zero_defer_forces_close_on_limit_down():
    # max_defer=0 = 到期就走：收盘跌停也按当日收盘成交
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.0, 9.0), (9.0, 9.1, 8.0, 8.9)])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=0), FREE, RULE)
    assert r.exit_idx == 1 and r.exit_price == 9.0
    assert r.reason == "t1_close_forced" and r.deferred == 0


def test_unsold_when_data_ends_during_defer():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.0, 9.0)])
    r = settle_hold_trade(df, 0, HoldPlan(max_defer=2), FREE, RULE)
    assert r.status == "deferred_unsold" and r.reason == "deferred_unsold"
    assert r.deferred == 0 and r.entry_idx == 1


def test_signal_limit_up_buy_uses_exact_ratio():
    # 信号日收盘 10.0 → 主板涨停价 11.0（精确比例，不是阈值合同的 9.5%）
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.1, 9.9, 10.2), (10.3, 10.4, 10.1, 10.2)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_limit_up", sell="next_open"), FREE, RULE)
    assert r.entry_idx == 0 and r.entry_price == 11.0
    assert r.exit_idx == 1 and r.exit_price == 10.0


def test_next_open_sell_is_relative_to_entry_bar():
    # buy=signal_close 时 next_open 卖在信号次根，不是 signal+2
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.5, 10.6, 10.4, 10.5), (11.0, 11.1, 10.9, 11.0)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_close", sell="next_open"), FREE, RULE)
    assert r.entry_idx == 0 and r.exit_idx == 1 and r.exit_price == 10.5


def test_signal_buy_no_exit_reports_true_entry_index():
    # buy=signal_close 时 entry_idx=signal_idx；旧实现固定 signal_idx+1，会差一根
    df = _bars([(10.0, 10.0, 10.0, 10.0)])
    r = settle_hold_trade(df, 0, HoldPlan(buy="signal_close", sell="next_open"), FREE, RULE)
    assert r.status == "no_exit" and r.entry_idx == 0


def test_signal_buy_with_non_positive_entry_price_is_suspended():
    # 收盘价 0 的信号帧：两个 signal 买入模式都不得走到除法，必须显式报 suspended
    # 第二根有可成交开盘——守卫失效时会真的落到 `_finish` 的除法上（ZeroDivisionError）
    df = _bars([(0.0, 0.0, 0.0, 0.0), (10.0, 10.0, 10.0, 10.0)])
    for buy in ("signal_close", "signal_limit_up"):
        r = settle_hold_trade(df, 0, HoldPlan(buy=buy, sell="next_open"), FREE, RULE)
        assert r.status == "suspended" and r.entry_idx == 0


def test_net_uses_cost_model():
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4), (10.4, 10.6, 10.2, 10.5)])
    cm = CostModel(commission=0.00025, stamp_sell=0.0005, slippage=0.0005)
    r = settle_hold_trade(df, 0, HoldPlan(), cm, RULE)
    assert r.net_ret == pytest.approx(cm.round_trip(10.0, 10.4))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_execution_hold.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tdxfeed.execution.hold'`

- [ ] **Step 3: 实现**

`tdxfeed/execution/hold.py`：

```python
"""内核 B：打板持有期结算（次日开盘买、指定时点卖、跌停顺延）。

与内核 A 的区别不是参数，是口径：这里「买不进」和「卖不出」都是必须显式
输出的状态，不能当没发生过，也不能回填一个理想成交价。

已知建模简化：开盘卖模式（`next_open` / `t_plus_2_open`）按开盘无条件成交，
不判开盘是否跌停——既有 daban `topk` 就是这个口径，迁移要保数；跌停不可卖
只在收盘卖模式（`next_close`）上判定。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

import pandas as pd

from .bars import date_str
from .costs import CostModel
from .limits import LimitRule
from .result import TradeResult

DEFAULT_DEFER = 3


@dataclass(frozen=True)
class HoldPlan:
    buy: Literal["next_open", "signal_close", "signal_limit_up"] = "next_open"
    sell: Literal["next_open", "next_close", "t_plus_2_open"] = "next_close"
    max_defer: int = DEFAULT_DEFER


def _row(bars: pd.DataFrame, idx: int) -> dict:
    r = bars.iloc[idx]
    return {"date": date_str(r["date"]), "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])}


def _gap_result(entry_idx: int, bar: dict, entry_price: float, status: str) -> TradeResult:
    """没有成交的一笔：entry_idx 由调用方给（signal 买入模式下它不等于 signal_idx+1）。"""
    return TradeResult(entry_idx=entry_idx, entry_date=bar["date"],
                       entry_price=entry_price, exit_idx=entry_idx,
                       exit_date=bar["date"], exit_price=0.0, reason=status,
                       gross_ret=0.0, net_ret=0.0, status=status)


def settle_hold_trade(bars: pd.DataFrame, signal_idx: int, plan: HoldPlan,
                      costs: CostModel, rule: LimitRule) -> TradeResult:
    n = len(bars)
    if signal_idx < 0 or signal_idx >= n:
        raise IndexError(f"signal_idx={signal_idx} 超出 bars 长度 {n}")

    sig = _row(bars, signal_idx)
    entry_idx = signal_idx + 1
    if plan.buy == "signal_close":
        entry_idx = signal_idx
        entry_price = sig["close"]
    elif plan.buy == "signal_limit_up":
        entry_price = rule.up_price(sig["close"], sig["date"])
        entry_idx = signal_idx
    else:
        if entry_idx >= n:
            return _gap_result(entry_idx, sig, 0.0, "no_entry")
        eb = _row(bars, entry_idx)
        if eb["open"] <= 0 or eb["volume"] <= 0:
            return _gap_result(entry_idx, eb, 0.0, "suspended")
        if rule.is_open_limit_up(eb["open"], sig["close"], eb["date"]):
            return _gap_result(entry_idx, eb, eb["open"], "unbuyable_open_limit")
        entry_price = eb["open"]

    # 信号帧收盘价为 0/负（停牌脏数据）时入场价无效：不是算出来的 0 元成交，是买不进
    if entry_price <= 0:
        return _gap_result(entry_idx, sig, 0.0, "suspended")

    if plan.sell == "t_plus_2_open":
        exit_idx = signal_idx + 2
        sell_reason = "t2_open"
        sell_at = "open"
    elif plan.sell == "next_open":
        exit_idx = entry_idx + 1
        sell_reason = "t2_open"
        sell_at = "open"
    else:
        exit_idx = entry_idx
        sell_reason = "t1_close"
        sell_at = "close"

    if exit_idx >= n:
        return _gap_result(entry_idx, _row(bars, entry_idx), entry_price, "no_exit")

    bar = _row(bars, exit_idx)
    # exit_idx 可能等于 0（signal_close 买在第 0 根），此时前收取入场价而非绕到末根
    prev_close = float(bars["close"].iloc[exit_idx - 1]) if exit_idx > 0 else entry_price
    if sell_at == "open":
        if bar["open"] <= 0:
            return _gap_result(entry_idx, bar, entry_price, "no_exit")
        return _finish(entry_idx, entry_price, exit_idx, bar["date"], bar["open"],
                       sell_reason, costs, bars)

    if bar["close"] <= 0:
        return _gap_result(entry_idx, bar, entry_price, "no_exit")
    lim_dn = rule.down_price(prev_close, bar["date"])
    if lim_dn > 0 and bar["close"] > lim_dn + rule.tol:
        return _finish(entry_idx, entry_price, exit_idx, bar["date"], bar["close"],
                       sell_reason, costs, bars)

    # max_defer=0：不顺延，跌停也按当日收盘强制成交（daban topk 的「到期就走」）
    if plan.max_defer <= 0:
        return _finish(entry_idx, entry_price, exit_idx, bar["date"], bar["close"],
                       f"{sell_reason}_forced", costs, bars)

    # 收盘跌停 → 顺延：每个顺延日先试开盘、再试收盘，最后一日强制按收盘
    max_days = int(plan.max_defer)
    for k in range(1, max_days + 1):
        j = exit_idx + k
        if j >= n:
            r = _gap_result(entry_idx, bar, entry_price, "deferred_unsold")
            return replace(r, deferred=k - 1)
        b = _row(bars, j)
        pc = float(bars["close"].iloc[j - 1])
        dn = rule.down_price(pc, b["date"])
        is_last = k >= max_days
        if b["open"] > 0 and (dn <= 0 or b["open"] > dn + rule.tol):
            return _finish(entry_idx, entry_price, j, b["date"], b["open"],
                           f"t{j - entry_idx + 1}_open", costs, bars,
                           deferred=k)
        if b["close"] > 0 and (dn <= 0 or b["close"] > dn + rule.tol or is_last):
            px = b["close"]
            reason = f"t{j - entry_idx + 1}_close" + ("_forced" if is_last else "")
            return _finish(entry_idx, entry_price, j, b["date"], px, reason,
                           costs, bars, deferred=k)
    r = _gap_result(entry_idx, bar, entry_price, "deferred_unsold")
    return replace(r, deferred=max_days)


def _finish(entry_idx: int, entry_price: float, exit_idx: int, exit_date: str,
            exit_price: float, reason: str, costs: CostModel,
            bars: pd.DataFrame, *, deferred: int = 0) -> TradeResult:
    gross = exit_price / entry_price - 1.0
    net = costs.round_trip(entry_price, exit_price)
    return TradeResult(
        entry_idx=entry_idx,
        entry_date=date_str(bars["date"].iloc[entry_idx]),
        entry_price=entry_price,
        exit_idx=exit_idx,
        exit_date=exit_date,
        exit_price=exit_price,
        reason=reason,
        gross_ret=gross,
        net_ret=net,
        deferred=deferred,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_execution_hold.py -q`
Expected: 16 passed

- [ ] **Step 5: 导出并提交**

`tdxfeed/execution/__init__.py` 用**并入**方式扩展（保留既有 `__all__` 顺序，不要重排）：
`from .hold import HoldPlan, settle_hold_trade`，并把 `"HoldPlan"`、`"settle_hold_trade"` 追加到 `__all__` 末尾。

```bash
git add tdxfeed/execution/hold.py tdxfeed/execution/__init__.py tests/test_execution_hold.py
git commit -m "feat(execution): 内核 B（打板持有期：买不进/跌停顺延显式化）"
```

---

### Task 7: 旧入口改为委托（函数名全部保留）

**Files:**
- Modify: `tdxfeed/features_5d.py:46-96`（`LIMIT_MAP`、`get_limit`）
- Modify: `tdxfeed/daban/limit_price.py:54-164`（常量与四个函数）
- Modify: `quant/multifactor/factors.py:58-64`（`get_limit`）
- Modify: `tests/test_features_5d.py`（只改两句已变假的 docstring，见 Step 3 末）
- Test: `tests/test_limit_single_source.py`

**Interfaces:**
- Consumes: `tdxfeed.execution.limits`
- Produces: 三个模块的公开函数签名**不变**，行为不变（除下面两处刻意的口径修正）。

**已知口径变化（逐条写进提交信息）**：
- `features_5d.get_limit` / `quant.factors.get_limit` 对**无前缀**的北交所代码（`8xxxxx` / `4xxxxx`）从主板 0.095 变 0.295。这是修正（旧实现只认 `bj`/`920` 前缀），不是回归。
- 两者对 `689xxx`（科创板 CDR，如 `689009 九号公司`，`data/parquet/daily/sh689009.parquet` 真实存在）从 0.095 变 0.195：旧实现只列了 `688`，内核 `market_of` 的 gem 组是 `300/301/688/689`。
- 前缀大小写不再敏感（`SZ300750` 现按创业板 0.195、`bj600519` 现按主板 0.095）：内核先 `lower()` 再判板块；旧实现是精确字符串前缀匹配。仓库内代码源均为小写。
- `quant.factors.get_limit` 对 `200xxx`（B 股）原按创业板 0.195，委托后按主板 0.095；本仓库 `quant` 只跑 A 股，现有测试无 200 开头样本。

**本轮不动**：`compute_window_features` 的 `is_bj`/`is_gem` 两列仍用各自的旧前缀规则，因此 `689xxx` 的 `limit` 变为 0.195 而 `is_gem` 仍为 0（`8xxxxx`/`4xxxxx` 同理 `is_bj` 仍为 0）。这是既有缺陷，不在本任务范围；阶段 2 重训前若要对齐，单独改这两列并记录。

- [ ] **Step 1: 写失败测试**

`tests/test_limit_single_source.py`：

```python
"""三份涨跌幅实现必须同源：改规则只改 tdxfeed/execution/limits.py 一处。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tdxfeed import features_5d
from tdxfeed.daban import limit_price
from tdxfeed.execution import limits


def test_features_get_limit_delegates():
    for code in ("600519", "sz300750", "sh688981", "bj920001", "830799"):
        assert features_5d.get_limit(code) == limits.limit_threshold(code)


def test_features_get_limit_keeps_st_branch():
    assert features_5d.get_limit("600519", "*ST某某", "2026-07-03") == 0.045
    assert features_5d.get_limit("600519", "*ST某某", "2026-07-06") == 0.095


def test_daban_limit_ratio_delegates():
    assert limit_price.limit_ratio("600519") == limits.limit_ratio("600519")
    assert limit_price.limit_ratio("600519", "*ST某某", "2026-07-03") == Decimal("0.05")


def test_daban_price_helpers_delegate():
    assert limit_price.limit_up_price(10.0, Decimal("0.10")) == limits.limit_up_price(10.0, Decimal("0.10"))
    assert limit_price.limit_down_price(10.0, Decimal("0.10")) == limits.limit_down_price(10.0, Decimal("0.10"))


def test_quant_get_limit_delegates():
    from quant.multifactor import factors
    assert factors.get_limit("300750") == pytest.approx(0.195)
    assert factors.get_limit("600519") == pytest.approx(0.095)


def test_features_get_limit_bse_without_prefix():
    """无前缀的北交所代码也按 30% 档——旧实现只有 bj/920 前缀才判北交所。"""
    for code in ("830799", "430047", "871981"):
        assert features_5d.get_limit(code) == pytest.approx(0.295)


def test_features_get_limit_689_is_gem():
    """689 是科创板 CDR（689009 九号公司）；旧实现只认 688，落到主板 0.095。"""
    assert features_5d.get_limit("689009") == pytest.approx(0.195)
    assert features_5d.get_limit("sh689009") == pytest.approx(0.195)


def test_quant_get_limit_689_is_gem():
    from quant.multifactor import factors
    assert factors.get_limit("689009") == pytest.approx(0.195)


def test_market_of_and_prefix_agree():
    for code in ("600519", "000001", "300750", "688981", "920001"):
        assert limit_price.market_of(code) == limits.market_of(code)
    assert limit_price.exchange_prefix("830799") == "bj"
    assert limit_price.exchange_prefix("sh688981") == "sh"
    assert limit_price.exchange_prefix("300750") == "sz"


def test_features_and_quant_follow_kernel_threshold(monkeypatch):
    """替换内核后入口必须跟着变——否则只是「值碰巧相等」的假委托。"""
    monkeypatch.setattr(limits, "limit_threshold", lambda *a, **k: 0.4242)
    assert features_5d.get_limit("600519") == 0.4242
    from quant.multifactor import factors
    assert factors.get_limit("600519") == 0.4242


def test_daban_helpers_follow_kernel(monkeypatch):
    monkeypatch.setattr(limits, "limit_ratio", lambda *a, **k: Decimal("0.66"))
    assert limit_price.limit_ratio("600519") == Decimal("0.66")
    monkeypatch.setattr(limits, "limit_up_price", lambda *a, **k: 12.34)
    assert limit_price.limit_up_price(10.0) == 12.34
    monkeypatch.setattr(limits, "limit_down_price", lambda *a, **k: 8.88)
    assert limit_price.limit_down_price(10.0) == 8.88
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_limit_single_source.py -q`
Expected: FAIL 6 条 —— `test_features_get_limit_delegates`（`830799` 旧值 0.095 ≠ 0.295）、`test_features_get_limit_bse_without_prefix`、`test_features_get_limit_689_is_gem`、`test_quant_get_limit_689_is_gem`、`test_features_and_quant_follow_kernel_threshold`、`test_daban_helpers_follow_kernel`；其余 5 条本来就应该绿（同值守卫，不是 RED 项；本文件共 11 个用例）。若 `quant` 不可导入则先确认 `quant/__init__.py` 存在。

- [ ] **Step 3: 实现（三处委托）**

`tdxfeed/features_5d.py` —— 保留 `LIMIT_MAP` 常量与 `get_limit` 名字，函数体改委托：

```python
from .execution import limits as _limits
from .names import is_st_name as _is_st_name

LIMIT_MAP: dict[str, float] = {"main": 0.095, "gem": 0.195, "bse": 0.295}
LIMIT_ST_MAIN_OLD = 0.045
MAIN_ST_RULE_CHANGE_DATE = _limits.MAIN_ST_RULE_CHANGE_DATE


def get_limit(code: str, name: str = "", on_date=None) -> float:
    """涨停判定阈值。委托 `tdxfeed.execution.limits.limit_threshold`（唯一实现）。"""
    return _limits.limit_threshold(code, is_st=_is_st_name(name), on_date=on_date)
```

（必须写成模块属性调用 `_limits.limit_threshold(...)`，不要 `from ... import limit_threshold as _limit_threshold`——后者把函数对象绑死在 import 时，`tests/test_limit_single_source.py` 的 monkeypatch 委托守卫就测不到「真的走内核」。同时删掉本文件的 `from datetime import date, datetime`（已无使用者）与 `_is_main_st_before_merger` 函数；`MAIN_ST_RULE_CHANGE_DATE` 由上面的赋值继续可访问，`__all__` 条目保持不变。`LIMIT_ST_MAIN_OLD` 保留常量化说明，无人引用但是规则名的一部分。）

`tdxfeed/daban/limit_price.py` —— **只改下面列出的名字**：`normalize_code`/`market_of`/`exchange_prefix`/`is_st`/`as_date`/`limit_ratio`/`limit_up_price`/`limit_down_price`/`is_sealed`；模块 docstring、`__all__`、`pct_change`、`board_label` 原样保留（`board_label` 仍用 `is_st` 与 `_as_date`，`_as_date = as_date` 别名保留）。`_CENT`/`_ONE`/`_quantize_cents`/`_PREFIXES`/`ROUND_HALF_UP` 在本文件已无使用者，随实现一起删。`LIMIT_RATIO` 用 `dict(...)` 拷贝而不是直接别名——别名会留下一个可跨模块原地修改的共享可变对象，日后任一侧写回就串了两份口径。

```python
from datetime import date
from decimal import Decimal

from tdxfeed.execution import limits as _limits
from tdxfeed.names import is_st_name as _is_st_name

MARKET_MAIN, MARKET_GEM, MARKET_BSE = "main", "gem", "bse"
LIMIT_RATIO = dict(_limits.LIMIT_RATIO)
LIMIT_RATIO_ST_MAIN_OLD = _limits.LIMIT_RATIO_ST_MAIN_OLD
LIMIT_RATIO_ST_MAIN = _limits.LIMIT_RATIO_ST_MAIN
MAIN_ST_RULE_CHANGE_DATE = _limits.MAIN_ST_RULE_CHANGE_DATE


def normalize_code(code: str) -> str:
    return _limits.normalize_code(code)


def market_of(code: str) -> str:
    return _limits.market_of(code)


def exchange_prefix(code: str) -> str:
    c = _limits.normalize_code(code)
    if _limits.market_of(c) == MARKET_BSE:
        return "bj"
    return "sh" if c[0] in "69" else "sz"


def is_st(name: str) -> bool:
    return _is_st_name(name)


def as_date(value) -> date | None:
    return _limits.as_date(value)


_as_date = as_date


def limit_ratio(code: str, name: str = "", on_date=None) -> Decimal:
    return _limits.limit_ratio(code, is_st=_is_st_name(name), on_date=on_date)


def limit_up_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    return _limits.limit_up_price(prev_close, ratio)


def limit_down_price(prev_close: float, ratio: Decimal | float = Decimal("0.10")) -> float:
    return _limits.limit_down_price(prev_close, ratio)


def is_sealed(price: float, limit_price: float) -> bool:
    return _limits.is_sealed(price, limit_price)
```

`quant/multifactor/factors.py`：

```python
def get_limit(code: str) -> float:
    """涨停判定阈值。委托 tdxfeed.execution.limits（唯一实现）。"""
    from tdxfeed.execution.limits import limit_threshold
    return limit_threshold(code)
```

（`200` 开头的 B 股原本被这批 0.195 覆盖，委托后按主板 0.095 算；本仓库的 `quant` 只跑 A 股，且现有测试没有 200 开头样本。若 `grep -rn "200[0-9][0-9][0-9]" tests/` 有断言，改断言而不是改实现。）

`tests/test_features_5d.py` —— 只改两句会变假的 docstring（断言一律不动）：
- `test_get_limit_threshold_equals_ratio_minus_buffer`：末尾「比例独立重述一遍，改动须两处同时」→「比例独立重述一遍；实现已单一来源，这里钉的是规则值本身」。
- `test_get_limit_agrees_with_daban_limit_ratio_across_the_merger`：「同一规则的两处实现不许各自漂」→「两处入口现均委托同一实现，这里钉的是跨日期的口径覆盖」。

- [ ] **Step 4: 跑测试与受影响回归**

Run: `venv/bin/pytest tests/test_limit_single_source.py tests/test_features_5d.py tests/test_limit_price_st_rule.py tests/test_daban_paper.py tests/test_daban_topk_pnl.py tests/test_daban_stop_lines.py -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/features_5d.py tdxfeed/daban/limit_price.py quant/multifactor/factors.py tests/test_limit_single_source.py tests/test_features_5d.py
git commit -m "refactor(limits): 三份涨跌幅实现收编为委托，函数名保留"
```

---

### Task 8: 迁移 plan_e_backtest（md5 必须不变）

**Files:**
- Modify: `scripts/plan_e_backtest.py:265-344`（`_settle_from_entry`）
- Test: `tests/test_plan_e_backtest_exit_fills.py`（既有，必须继续绿）

**Interfaces:**
- Consumes: `settle_single_trade`、`SingleTradePlan`、`CostModel`
- Produces: `_settle_from_entry(df, entry_idx, hold_days, tp, sl, fee, eff_slip) -> dict | None` 签名**不变**，内部改为调内核；返回 dict 的键与数值口径不变。

- [ ] **Step 1: 先取 md5 基线（改动前）**

```bash
cp -p data/plan_e/plan_e_backtest_v2_latest.md data/plan_e/plan_e_backtest_v2_latest.md.bak-$(date +%Y%m%d)-task8
venv/bin/python scripts/plan_e_backtest.py 2>&1 | tail -5
grep -v -e '^生成时间：' -e '^# 方案 E 回测报告 (' \
  data/plan_e/plan_e_backtest_v2_latest.md | md5sum | tee /tmp/plan_e_md5_before.txt
```

Expected: 打印一份报告路径与一行归一化 md5（**必须剔除两行时间戳**：首行标题 `# 方案 E 回测报告 (YYYY-MM-DD HH:MM)` 带分钟、末尾 `生成时间：…` 带秒——只剔除后者时，跨分钟重跑仍对不上，实测四份产物 17:36/17:37/17:39/17:41 归一后全为 `645fff8d…`）。**必须真的跑一次**：报告随当日数据变（现网数据比磁盘上那份新），闸门只对「同一次数据快照的前后两次运行」有意义。若脚本参数与预期不符（例如需要 `--mode`），用 `venv/bin/python scripts/plan_e_backtest.py --help` 读出真实入口后再跑，并把命令记在 `/tmp/plan_e_cmd.txt`。跑一次约 20 秒（含 300 次随机择时基线，seed 固定）；不要用 `--random-timing-trials 0` 图快，那会改报告内容、闸门失效。

- [ ] **Step 2: 写失败测试（内核接入点）**

在 `tests/test_plan_e_backtest_exit_fills.py` 末尾追加：

```python
def test_settle_from_entry_delegates_to_kernel():
    """迁移后内核是唯一实现：改内核行为必须能穿透到 plan_e。"""
    from tdxfeed.execution import single

    # 5 根帧：entry_idx=1 时 start=2、end=min(11,5)=5，end-start=3 才过 `_has_exit_window`
    _seed([(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
           (10.0, 13.5, 8.5, 12.0), (12.0, 12.5, 11.8, 12.0),
           (12.0, 12.2, 11.9, 12.1)])
    base = pe._settle_from_entry(pe._PARQUET_CACHE[CODE], 1, 10, 0.30, -0.10, 0.0, 0.0)
    assert base["exit_reason"] == "SL"

    calls = []
    original = single.check_exit

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    from unittest.mock import patch
    with patch.object(single, "check_exit", side_effect=spy):
        pe._settle_from_entry(pe._PARQUET_CACHE[CODE], 1, 10, 0.30, -0.10, 0.0, 0.0)
    assert calls, "plan_e 的出场判定没有走内核 check_exit"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_plan_e_backtest_exit_fills.py::test_settle_from_entry_delegates_to_kernel -q`
Expected: FAIL，`AssertionError: plan_e 的出场判定没有走内核 check_exit`

- [ ] **Step 4: 实现**

`scripts/plan_e_backtest.py` —— 替换 `_settle_from_entry` 函数体（保留函数名、签名、返回键）：

```python
from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.single import SingleTradePlan, settle_single_trade


def _settle_from_entry(df: pd.DataFrame, entry_idx: int, hold_days: int,
                       tp: float, sl: float, fee: float,
                       eff_slip: float) -> dict | None:
    """从给定入场日按出场阶梯结算一笔——委托 tdxfeed.execution.single（唯一实现）。

    `_has_exit_window` 的「至少 3 天前向窗口」是本报告的数据可用性预检，
    不属于结算口径，留在调用侧。

    `insufficient="skip"`（内核默认）：被数据端点截断的笔历史上就报 `TIME`，
    改成 `"last_close"` 会把这些笔标成 `END`、动报告内容、md5 闸门必红。
    """
    if not _has_exit_window(len(df), entry_idx, hold_days):
        return None
    plan = SingleTradePlan(judge="intrabar", tp=tp, sl=sl,
                           hold_days=hold_days, sl_first=True)
    costs = CostModel(commission=fee, slippage=eff_slip)
    r = settle_single_trade(df, entry_idx, plan, costs, insufficient="skip")
    if r is None:
        return None
    return {
        'entry_date': r.entry_date,
        'entry_price': round(r.entry_price, 2),
        'exit_date': r.exit_date,
        'exit_price': round(r.exit_price, 2),
        'return_pct': round(r.net_ret, 4),
        'return_pct_gross': round(r.gross_ret, 4),
        'max_gain_pct': round(r.max_gain, 4),
        'max_loss_pct': round(r.max_loss, 4),
        'exit_reason': r.reason,
        'hold': f"T+{r.exit_idx - r.entry_idx + 1}",
        'daily': list(r.daily),
    }
```

- [ ] **Step 5: 跑测试与 md5 闸门**

```bash
venv/bin/pytest tests/test_plan_e_backtest_exit_fills.py tests/test_plan_e_random_timing.py tests/test_plan_e_significance.py -q
venv/bin/python scripts/plan_e_backtest.py 2>&1 | tail -3
grep -v -e '^生成时间：' -e '^# 方案 E 回测报告 (' \
  data/plan_e/plan_e_backtest_v2_latest.md | md5sum
```

Expected: 测试全绿；归一化 md5 与 `/tmp/plan_e_md5_before.txt` **逐字符相同**。不同就停下——按这个顺序查：(1) `return_pct_gross` 的公式是 `exit/entry - 1`（内核）还是 `(exit-entry)/entry`（旧实现），两者差 1 ulp，可能让某笔的 4 位小数翻边；(2) `insufficient` 是否被改成了 `last_close`（截断笔的 reason 会变 `END`）；(3) `_has_exit_window` 是否被重复施加或删除。定位到具体行后停下来报告，不要靠改小数位/四舍五入把 md5 凑回去。

- [ ] **Step 6: 提交**

```bash
git add scripts/plan_e_backtest.py tests/test_plan_e_backtest_exit_fills.py
git commit -m "refactor(plan_e): _settle_from_entry 委托结算内核，报告 md5 不变"
```

---

### Task 9: 迁移 plan_e_sim_live（TIME 窗口差一根 + 成本乘法口径）

**Files:**
- Modify: `scripts/plan_e_sim_live.py:130-179`（`simulate_trade`）
- Test: `tests/test_plan_e_sim_live_exit.py`（既有 + 新增窗口用例）

**Interfaces:**
- Consumes: `settle_single_trade`、`SingleTradePlan`、`CostModel`
- Produces: `simulate_trade(df_full, signal_date, code) -> dict | None` 签名不变；返回键不变（`code/signal_date/entry_date/entry_price/exit_price/pnl_pct/reason`）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_plan_e_sim_live_exit.py` 末尾追加：

```python
def test_time_exit_after_nine_bars_matches_backtest_window():
    """入场后第 9 根收盘超时（与 plan_e 的 hold_days=10 含入场根一致）。"""
    rows = [(10.0, 10.1, 9.9, 10.0)] * 9           # 入场 bar 之后的 9 根，都不触发
    df = _bars(*rows, (10.0, 10.1, 9.9, 10.0), (99.0, 99.0, 99.0, 99.0))
    res = sim.simulate_trade(df, SIGNAL_DATE, "600000")
    assert res["reason"] == "TIME"
    assert res["exit_price"] == 10.0


def test_end_reason_when_window_truncated():
    rows = [(10.0, 10.1, 9.9, 10.0)] * 2
    df = _bars(*rows)
    res = sim.simulate_trade(df, SIGNAL_DATE, "600000")
    assert res["reason"] == "END"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_plan_e_sim_live_exit.py -q`
Expected: `test_end_reason_when_window_truncated` 可能已通过；`test_time_exit_after_nine_bars_matches_backtest_window` FAIL（旧实现按第 10 根收盘出场，`exit_price` 取到 99.0）。

- [ ] **Step 3: 实现**

```python
from tdxfeed.execution.bars import as_bars
from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.single import SingleTradePlan, settle_single_trade

TP_PCT, SL_PCT, HOLD_DAYS = 0.30, -0.10, 10


def simulate_trade(df_full: pd.DataFrame, signal_date: pd.Timestamp, code: str) -> dict | None:
    """信号日次日开盘入场，委托结算内核；与 plan_e_backtest 同一出场阶梯与同一时间窗。"""
    future = df_full[df_full["date"] > signal_date].reset_index(drop=True)
    if len(future) < 2:
        return None
    bars = as_bars(future)
    if float(bars["open"].iloc[0]) <= 0 or np.isnan(float(bars["open"].iloc[0])):
        return None
    plan = SingleTradePlan(judge="intrabar", tp=TP_PCT, sl=SL_PCT,
                           hold_days=HOLD_DAYS, sl_first=True)
    # 常量名叫 _BPS，值其实是**百分数**（0.03 = 0.03%、0.10 = 0.10%），与旧式
    # `(fee+slip)*2/100` 一致；除以 1e4 会把它缩小 100 倍。
    costs = CostModel(commission=TRADE_FEE_BPS / 100,
                      slippage=TRADE_SLIPPAGE_BPS / 100, stamp_sell=0.0)
    r = settle_single_trade(bars, 0, plan, costs, insufficient="last_close")
    if r is None:
        return None
    return {
        "code": code,
        "signal_date": signal_date.strftime("%Y-%m-%d"),
        "entry_date": r.entry_date,
        "entry_price": r.entry_price,
        "exit_price": r.exit_price,
        "pnl_pct": r.net_ret,
        "reason": r.reason,
    }
```

（若 `TRADE_FEE_BPS` / `TRADE_SLIPPAGE_BPS` 在文件里是别的名字，用 `grep -n "TRADE_FEE\|SLIPPAGE" scripts/plan_e_sim_live.py` 取真实常量名，不要新造。现有值是 `0.03` / `0.10`——按百分数理解，除以 100；`stamp_sell=0.0` 与 plan_e 一致，显式钉住。）

**已知行为变化（写进提交信息）**：旧实现的判定循环是 `range(1, min(HOLD_DAYS+1, len))`，即入场后判 **10** 根、TIME 落在 `entry+10` 收盘；内核（与 plan_e 同口径）判 **9** 根、TIME 落在 `entry+9` 收盘。所以 **TIME 笔的 `exit_date`/`exit_price` 会提前一根**，这是本任务要修的口径，不是回归；TP/SL 笔的价格与日期不变。`pnl_pct` 同时从「加法扣 2×(fee+slip)」改为内核的乘法式（同一费率下差 ~1e-6 量级）。

- [ ] **Step 4: 跑测试**

Run: `venv/bin/pytest tests/test_plan_e_sim_live_exit.py -q`
Expected: 全绿（原有三条口径用例 + 新增两条）

- [ ] **Step 5: 逐笔 diff 记录（写进提交信息）**

```bash
venv/bin/python - <<'PY'
import importlib.util, json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
spec = importlib.util.spec_from_file_location("sim_old", "scripts/plan_e_sim_live.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
# 用同一个人工构造的 30 根随机走行，打印新实现结果供人工核对时间窗
rows, px = [], 10.0
for i in range(30):
    o = px; c = o * (1.0 + 0.01 * ((i % 5) - 2)); h = max(o, c) * 1.005; l = min(o, c) * 0.995
    rows.append((o, h, l, c)); px = c
df = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=30), "open": [r[0] for r in rows],
                   "high": [r[1] for r in rows], "low": [r[2] for r in rows],
                   "close": [r[3] for r in rows], "volume": [1e6] * 30, "amount": [1e7] * 30})
print(json.dumps(mod.simulate_trade(df, pd.Timestamp("2025-01-01"), "600000"), ensure_ascii=False))
PY
```

把输出里的 `reason` 与 `exit_date` 与 **plan_e 的新实现**对同一帧的结果对照（任务 8 已把 plan_e 接同一内核，二者应逐笔一致：同一行 `_settle_from_entry` 与 `settle_single_trade(bars, 0, ...)` 的差别只在「入场时点由调用方给定」）。与**旧 sim_live** 比，TIME 笔的 `exit_date`/`exit_price` 会早一根（见上面「已知行为变化」），TP/SL 笔一致。差异说明写进提交信息正文。

- [ ] **Step 6: 提交**

```bash
git add scripts/plan_e_sim_live.py tests/test_plan_e_sim_live_exit.py
git commit -m "refactor(sim_live): 委托结算内核；TIME 窗口与 plan_e 对齐，成本改乘法口径"
```

---

### Task 10: 迁移 mcp/servers/backtest.py（只收编阶梯，数值不变）

**Files:**
- Modify: `mcp/servers/backtest.py:375-389`
- Test: 新建 `tests/test_mcp_backtest_exit_ladder.py`；回归面另跑 `tests/test_daban_mcp.py`

**Interfaces:**
- Consumes: `check_exit`、`SingleTradePlan`
- Produces: `_simulate(...)` 签名与返回结构不变；`exit_reason` 取值集合不变（`stop_gap/stop_loss/take_profit_gap/take_profit/max_hold/signal/end`）。

- [ ] **Step 1: 写测试**

`tests/test_mcp_backtest_exit_ladder.py`：

```python
"""MCP 回测的阶梯必须与内核同源，但 max_hold/cooldown 仍归它自己。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("mcp_backtest_under_test",
                                                  ROOT / "mcp" / "servers" / "backtest.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _frame(opens=None, highs=None, lows=None, closes=None, n=6):
    opens = opens or [10.0] * n
    highs = highs or [10.1] * n
    lows = lows or [9.9] * n
    closes = closes or [10.0] * n
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n),
        "open_adj": opens, "high_adj": highs, "low_adj": lows,
        "close_adj": closes, "volume": [1e6] * n,
    })


def _run(mod, df, *, exits=None, max_hold=None):
    n = len(df)
    entries = np.zeros(n, dtype=bool)
    entries[0] = True
    exits = exits if exits is not None else np.zeros(n, dtype=bool)
    # 本模块的 stop_loss 是正数（0.10，校验区间 0.001..0.95），止损线 = entry*(1-stop_loss)
    return mod._simulate(df, entries, exits, 0, n - 1, fee_bps=0.0, stamp_bps=0.0,
                         slip_bps=0.0, take_profit=0.30, stop_loss=0.10,
                         max_hold=max_hold, cooldown=0)


def test_exit_ladder_delegates_to_kernel(monkeypatch):
    mod = _load()
    import tdxfeed.execution.single as single

    seen = []
    original = single.check_exit

    def spy(*args, **kwargs):
        seen.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(single, "check_exit", spy)

    df = _frame(lows=[9.9, 9.9, 8.5, 9.9, 9.9, 9.9],
                highs=[10.1, 10.1, 10.5, 10.1, 10.1, 10.1])
    sim = _run(mod, df)
    assert seen, "MCP 回测的止盈止损没有走内核 check_exit"
    assert sim["trades"][0]["exit_reason"] == "stop_loss"
    assert sim["trades"][0]["exit_price"] == 9.0


@pytest.mark.parametrize("case,opens,highs,lows,expect_reason,expect_price", [
    # 入场 i=1 @10.0；止损 9.0 / 止盈 13.0
    ("stop_gap", [10, 10, 8.5, 10, 10, 10], [10.1, 10.1, 8.8, 10.1, 10.1, 10.1],
     [9.9, 9.9, 8.0, 9.9, 9.9, 9.9], "stop_gap", 8.5),
    ("take_profit_gap", [10, 10, 14.0, 10, 10, 10], [10.1, 10.1, 14.2, 10.1, 10.1, 10.1],
     [9.9, 9.9, 13.8, 9.9, 9.9, 9.9], "take_profit_gap", 14.0),
    ("stop_loss", [10] * 6, [10.1] * 6, [9.9, 9.9, 8.5, 9.9, 9.9, 9.9], "stop_loss", 9.0),
    ("take_profit", [10] * 6, [10.1, 10.1, 13.5, 10.1, 10.1, 10.1], [9.9] * 6,
     "take_profit", 13.0),
])
def test_price_ladder_reasons_unchanged(case, opens, highs, lows,
                                        expect_reason, expect_price):
    mod = _load()
    sim = _run(mod, _frame(opens=opens, highs=highs, lows=lows))
    t = sim["trades"][0]
    assert t["exit_reason"] == expect_reason, case
    assert t["exit_price"] == expect_price, case


def test_max_hold_stays_outside_kernel_ladder():
    mod = _load()
    sim = _run(mod, _frame(), max_hold=2)
    t = sim["trades"][0]
    assert t["exit_reason"] == "max_hold"
    assert t["hold_days"] == 2


def test_signal_exit_and_end_exit_reasons_unchanged():
    mod = _load()
    exits = np.zeros(6, dtype=bool)
    exits[2] = True
    assert _run(mod, _frame(), exits=exits)["trades"][0]["exit_reason"] == "signal"
    assert _run(mod, _frame())["trades"][0]["exit_reason"] == "end"
```

- [ ] **Step 2: 跑测试**

Run: `venv/bin/pytest tests/test_mcp_backtest_exit_ladder.py -q`
Expected: 只有 `test_exit_ladder_delegates_to_kernel` FAIL（`MCP 回测的止盈止损没有走内核 check_exit`）；其余 6 条（4 条阶梯参数化 + max_hold + signal/end）是**既有行为特征化**用例——迁移前就该绿，迁移后必须仍绿，它们是「数值不变」的证据。共 7 个用例。

- [ ] **Step 3: 实现**

把 `_simulate` 里第 2 步（原 375-389 行）替换为：

```python
        # 2) 日内止盈止损（T+1：入场当日不检查）——阶梯委托内核，max_hold/cooldown 留在本函数
        if in_pos and i > entry_idx:
            # 符号约定不同：本模块 stop_loss 为正数（0.10），内核 sl 为带符号收益（-0.10）
            # check_exit 不读 hold_days（时间窗归调用方），故不传；传了会让人误以为 max_hold 也搬进内核
            plan = _single.SingleTradePlan(
                judge="intrabar", tp=take_profit,
                sl=-stop_loss if stop_loss is not None else None, sl_first=True)
            hit = _single.check_exit(entry_gross, open_=open_p,
                                     high=float(df.at[i, "high_adj"]),
                                     low=float(df.at[i, "low_adj"]),
                                     close=close_p, plan=plan)
            if hit is not None:
                if hit.reason == "SL":
                    reason = "stop_gap" if open_p <= entry_gross * (1 - stop_loss) else "stop_loss"
                else:
                    reason = "take_profit_gap" if open_p >= entry_gross * (1 + take_profit) else "take_profit"
                sell(i, hit.price, reason)
```

文件顶部、`sys.path.insert` 之后（与既有 `from tdxfeed... import ...  # noqa: E402` 同段）加：

```python
from tdxfeed.execution import single as _single  # noqa: E402
```

**必须是模块属性调用** `_single.check_exit(...)`，不能 `from tdxfeed.execution.single import check_exit`：后者在 import 期把函数对象绑死，Step 1 的 monkeypatch 守卫就测不到真委托（Task 7 的 R16 同一坑，`features_5d` 已按此写法改过）。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_mcp_backtest_exit_ladder.py tests/test_daban_mcp.py -q`
Expected: 全绿（`test_daban_mcp.py` 只覆盖 `daban_top_picks` 工具面、不含 `_simulate`，跑它是证明 MCP 工具层没被这次改动带到）

- [ ] **Step 5: 提交**

```bash
git add mcp/servers/backtest.py tests/test_mcp_backtest_exit_ladder.py
git commit -m "refactor(mcp-backtest): 止盈止损阶梯委托内核，max_hold/cooldown 留在原处"
```

---

### Task 11: 迁移 v3_simulate / v3_simulate_rolling（close_only 原口径）

**Files:**
- Modify: `scripts/v3_simulate.py:127-147`
- Modify: `scripts/v3_simulate_rolling.py`（同结构的持仓检查段）
- Test: `tests/test_v3_simulate_close_only.py`

**Interfaces:**
- Consumes: `check_exit`、`SingleTradePlan(judge="close_only", sl_first=False)`
- Produces: 两个脚本的 `exit_type` 取值不变（`tp/sl/time/force_close`），数值不变。

- [ ] **Step 1: 写失败测试**

`tests/test_v3_simulate_close_only.py`：

```python
"""v3 是收盘价判定的老口径，迁移时把 judge 显式写出来，别顺手改成日内。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_v3_declares_close_only_plan():
    plan = _load("v3_simulate").EXIT_PLAN
    assert plan.judge == "close_only"
    assert plan.sl_first is False
    assert (plan.tp, plan.sl, plan.hold_days) == (0.30, -0.10, 30)


def test_v3_rolling_declares_close_only_plan():
    plan = _load("v3_simulate_rolling").EXIT_PLAN
    assert plan.judge == "close_only"
    assert plan.sl_first is False
    assert (plan.tp, plan.sl, plan.hold_days) == (0.30, -0.10, 30)


@pytest.mark.parametrize("name", ["v3_simulate", "v3_simulate_rolling"])
def test_effective_plan_honours_runtime_params(name):
    """模块级常量只是默认口径；CLI / 函数参数覆盖必须真的生效。"""
    plan = _load(name).exit_plan(0.5, -0.2, 7)
    assert (plan.tp, plan.sl, plan.hold_days) == (0.5, -0.2, 7)
    assert plan.judge == "close_only"
    assert plan.sl_first is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_v3_simulate_close_only.py -q`
Expected: 4 failed，`AttributeError: module ... has no attribute 'EXIT_PLAN'`（`exit_plan` 用例同样先红在 `EXIT_PLAN`，因为 `replace` 以它为底）。

- [ ] **Step 3: 实现**

`scripts/v3_simulate.py` 顶部（`ROOT` 定义之后、`import pandas as pd` 段之后）：

```python
from dataclasses import replace

if str(ROOT) not in sys.path:      # scripts/ 直接运行时 sys.path[0] 是 scripts/
    sys.path.insert(0, str(ROOT))

from tdxfeed.execution.single import SingleTradePlan, check_exit  # noqa: E402

# v3 的历史口径：只看收盘价，止盈先于止损。这里只把口径声明出来，数值不变；
# 运行期参数（--take-profit / --stop-loss / --hold-days）经 exit_plan() 覆盖。
EXIT_PLAN = SingleTradePlan(judge="close_only", tp=0.30, sl=-0.10,
                            hold_days=30, sl_first=False)


def exit_plan(take_profit: float, stop_loss: float, hold_days: int) -> SingleTradePlan:
    return replace(EXIT_PLAN, tp=take_profit, sl=stop_loss, hold_days=hold_days)
```

（两个脚本目前都没有 `import sys`，要补；`ROOT` 两处都已存在——那行硬编码路径归阶段 3+4 处理，本轮不动。）

持仓检查段（原 133-147 行）里**只替换 if/elif 判定链**，`ret`、`ret_max`、`h['max_price']`、`h['days_held']` 四行原样保留：

```python
            hit = check_exit(h['entry_price'], open_=float(day_data.iloc[0]['open']),
                             high=high, low=float(day_data.iloc[0]['low']),
                             close=close,
                             plan=exit_plan(args.take_profit, args.stop_loss,
                                            args.hold_days))
            if hit is not None:
                to_close.append((code, close, hit.reason.lower(), ret))
            elif h['days_held'] >= args.hold_days:
                to_close.append((code, close, 'time', ret))
```

**`time` 分支必须留着**：内核只判 TP/SL，持有期到期仍归调用方（与 Task 10 的 `max_hold` 同一分工）。`hit.reason.lower()` 给出 `'tp'`/`'sl'`，`exit_type` 取值集合不变（`tp/sl/time/force_close`）。

`scripts/v3_simulate_rolling.py` 同样加 `EXIT_PLAN` 与 `exit_plan()`（模块级），判定链换成 `plan=exit_plan(take_profit, stop_loss, hold_days)`（该脚本这三个是 `simulate_window` 的函数参数，没有对应的 CLI 开关），`elif h['days_held'] >= hold_days:` 照旧保留。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_v3_simulate_close_only.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add scripts/v3_simulate.py scripts/v3_simulate_rolling.py tests/test_v3_simulate_close_only.py
git commit -m "refactor(v3): 收盘判定口径显式声明并委托内核，数值不变"
```

---

### Task 12: 迁移 daban_backtest（数值会变：跳空按开盘）

**Files:**
- Modify: `scripts/daban_backtest.py:263-297`
- Test: `tests/test_daban_backtest_gap_fill.py`

**Interfaces:**
- Consumes: `check_exit`、`settle_single_trade`、`SingleTradePlan`、`CostModel`
- Produces: `Trade` 字段不变（`exit_date` 仍是 `YYYYMMDD`）；`ret_net = gross - cost` 的原有成本处理保留（`cost` 是调用方传进来的单边合计，内核按零成本跑）。

**本任务的三处已知数值变化（都要写进提交正文）**
1. 跳空穿过触发价按当日开盘成交（旧实现抹回 `entry*(1±sl|tp)`）——这是任务目的。
2. 合理性闸门下界从 `-limit-0.01` 放宽到 `-2*limit+(-0.01)`：入场价可以是 T+1 开盘（可能高于前收），此后连跌两根就能超过一个 limit。不放宽的话，真跳空亏损笔会被当「除权未补偿」丢掉——方向是把最差的样本删掉，比不给跳空更危险。
3. `hold_n == 1`（信号在倒数第二根、入场日就是数据末根）时内核返回 None（窗口里没有可判根），旧实现按当日收盘超时出场。调用侧补这一支，保住旧行为。

- [ ] **Step 1: 写失败测试**

`tests/test_daban_backtest_gap_fill.py`：

```python
"""跳空穿过止损价时按开盘成交；入场当日仍要判；数据到头仍报超时。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    name = "daban_bt_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "daban_backtest.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Trade 是 @dataclass(slots=True)，3.14 下 dataclasses 会回查 sys.modules[cls.__module__]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _frame(closes, opens, highs, lows):
    n = len(closes)
    return pd.DataFrame({
        "date": [f"2026010{i}" for i in range(1, n + 1)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1e6] * n,
    })


# d0 收 10.00，d1 收 11.00 = 涨停（limit_up_price(10.0)=11.0），d2 = T+1 入场 @11.00
# 止损线 9.90 / 止盈线 12.10（tp=sl=0.10，便于手算）
_GAP = dict(
    closes=[10.0, 11.0, 11.0, 9.0, 9.0],
    opens=[10.0, 11.0, 11.0, 9.0, 9.0],     # d3 低开 9.0，穿过 9.90
    highs=[10.2, 11.0, 11.2, 9.2, 9.2],
    lows=[9.9, 11.0, 10.9, 8.8, 8.8],
)


def test_gap_down_settles_at_open_not_at_stop_level():
    mod = _load()
    trades, skipped = mod._backtest_chase(
        "600000", "浦发银行", _frame(**_GAP), tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert (len(trades), skipped) == (1, 0)
    t = trades[0]
    assert t.reason == "止损"
    assert t.exit == 9.0                 # 旧实现给 9.9（抹回止损价）
    assert t.exit_date == "20260104"


def test_entry_bar_is_still_judged(monkeypatch):
    """T+1 开盘买入当日就可能被止损——旧实现从 k=j 起判，迁移不许丢这一根。"""
    mod = _load()
    df = _frame(closes=[10.0, 11.0, 10.9],
                opens=[10.0, 11.0, 11.0],
                highs=[10.2, 11.0, 11.1],
                lows=[9.9, 11.0, 9.5])          # 入场当日最低 9.5 ≤ 9.90
    trades, skipped = mod._backtest_chase(
        "600000", "浦发银行", df, tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert (len(trades), skipped) == (1, 0)
    t = trades[0]
    assert t.reason == "止损"
    assert t.exit == 9.9
    assert t.exit_date == t.entry_date        # 同日出场


def test_data_ends_on_entry_day_still_reports_timeout():
    mod = _load()
    df = _frame(closes=[10.0, 11.0, 10.5],
                opens=[10.0, 11.0, 11.0],
                highs=[10.2, 11.0, 11.2],
                lows=[9.9, 11.0, 10.0])
    trades, _ = mod._backtest_chase(
        "600000", "浦发银行", df, tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert len(trades) == 1
    assert trades[0].reason == "超时"
    assert trades[0].exit == 10.5


def test_ladder_delegates_to_kernel(monkeypatch):
    mod = _load()
    import tdxfeed.execution.single as single

    seen = []
    original = single.settle_single_trade

    def spy(*args, **kwargs):
        seen.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(single, "settle_single_trade", spy)
    trades, _ = mod._backtest_chase(
        "600000", "浦发银行", _frame(**_GAP), tp=0.10, sl=0.10, hold=3, cost=0.0)
    assert seen, "daban 回测的止盈止损没有走内核"
    assert trades[0].exit == 9.0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_daban_backtest_gap_fill.py -q`
Expected: **2 failed / 2 passed**（控制者已在未改动的代码上实测过这个分布）：
- `test_gap_down_settles_at_open_not_at_stop_level` 红于 `assert 9.9 == 9.0`（旧实现抹回止损价，`ret_gross=-0.1` 还刚好过闸）。
- `test_ladder_delegates_to_kernel` 红于 `assert seen`。
- `test_entry_bar_is_still_judged`、`test_data_ends_on_entry_day_still_reports_timeout` 是**旧行为的特征化**用例，迁移前就绿，迁移后必须仍绿——它们保证迁移没顺手丢掉入场日判定与数据到头那一支。

**闸门必须同步放宽**，否则 gap 用例会以 `(0, 1) != (1, 0)` 红：新出场价 9.0 的 `gross = -18.2%` 会落进旧界 `-limit-0.01 = -11%`，被当「除权未补偿」删掉。

- [ ] **Step 3: 实现**

文件顶部、既有 `# noqa: E402` 导入段里加：

```python
from tdxfeed.execution import single as _single  # noqa: E402
from tdxfeed.execution.costs import CostModel  # noqa: E402
```

模块级（`Trade` 之后、`load_adj` 之前）：

```python
# 成交成本由调用方以 `cost`（单边合计）另扣，内核按零成本跑，避免扣两遍。
_ZERO_COSTS = CostModel(commission=0.0, slippage=0.0)
```

`_backtest_chase` 的出场段（原 `exit_price = 0.0` 到 `reason = "超时"` 那 18 行）替换为：

```python
        f_j = float(factors[j])
        entry = entry_raw * f_j  # 复权口径买入价
        if entry <= 0:
            continue

        n_win = min(hold, len(df) - j)
        win = pd.DataFrame({
            "date": dates[j:j + n_win],
            "open": opens[j:j + n_win] * factors[j:j + n_win],
            "high": highs[j:j + n_win] * factors[j:j + n_win],
            "low": lows[j:j + n_win] * factors[j:j + n_win],
            "close": closes[j:j + n_win] * factors[j:j + n_win],
            "volume": vols[j:j + n_win],
        })
        plan = _single.SingleTradePlan(tp=tp, sl=-sl, hold_days=hold, sl_first=True)

        # 入场当日也要判（旧实现从 k=j 起）：内核的时间窗固定从 entry+1 起（plan_e 口径），
        # 所以这一根在调用侧用同一阶梯判。开盘价即入场价，跳空条款在这一根不会生效。
        hit = _single.check_exit(entry, open_=entry,
                                 high=float(win["high"].iloc[0]),
                                 low=float(win["low"].iloc[0]),
                                 close=float(win["close"].iloc[0]), plan=plan)
        if hit is not None:
            k_rel = 0
            reason = "止损" if hit.reason == "SL" else "止盈"
            exit_price = hit.price
        elif n_win == 1:
            # 数据在入场日就断了：内核窗口没有可判根会返回 None，这里按当日收盘超时
            k_rel, reason, exit_price = 0, "超时", float(win["close"].iloc[0])
        else:
            r = _single.settle_single_trade(win, 0, plan, _ZERO_COSTS,
                                            insufficient="last_close")
            if r is None:
                continue
            k_rel = r.exit_idx
            reason = {"SL": "止损", "TP": "止盈"}.get(r.reason, "超时")
            exit_price = r.exit_price
        exit_date = dates[j + k_rel]
```

同时在 `opens/highs/lows/closes` 那几行旁边加 `vols = df["volume"].to_numpy()`（`win` 要用）。

闸门那行改成对称界：

```python
        # 合理性闸门：买入价 = T+1 开盘（可能高于前收），此后连跌两根可超过一个 limit；
        # 下界跟随跳空放开，否则真跳空亏损笔会被当「除权未补偿」删掉。
        if gross < -2 * ratio_f - 0.01 or gross > 2 * ratio_f + 0.01:
```

**不要**把 `exit_date` 写成 `r.exit_date`：内核返回 `YYYY-MM-DD`，`Trade.exit_date` 是 `YYYYMMDD`，两者混进同一个 CSV 会让下游按日期分组全错。

- [ ] **Step 4: 跑测试**

Run: `venv/bin/pytest tests/test_daban_backtest_gap_fill.py -q`
Expected: 4 passed

- [ ] **Step 5: 记录数值变化（A/B 用旧文件，不脏工作树）**

```bash
# 旧实现：放到 data/（gitignore）下，_ROOT = parents[1] 仍指向仓库根
git show HEAD:scripts/daban_backtest.py > data/_old_daban_backtest.py
venv/bin/python data/_old_daban_backtest.py --mode chase --start 20260901 --end 20260930 \
    --out /tmp/daban_old >/tmp/daban_old.log 2>&1
venv/bin/python scripts/daban_backtest.py --mode chase --start 20260901 --end 20260930 \
    --out /tmp/daban_new >/tmp/daban_new.log 2>&1
rm data/_old_daban_backtest.py
```

拿两份 summary 的「样本数 / 胜率 / 均收益 / 止损数 / 止盈数 / 超时数」对照，写进提交信息。差异只应来自上面三条；若样本数变化幅度大得解释不了，先查闸门与入场日这两支，不要直接接受。

- [ ] **Step 6: 提交**

```bash
git add scripts/daban_backtest.py tests/test_daban_backtest_gap_fill.py
git commit -m "refactor(daban-backtest): 出场委托内核，跳空改按开盘成交"
```

---

### Task 13: 迁移 plan_e_journal（组合账本保留，判定委托）

**Files:**
- Modify: `scripts/plan_e_journal.py:346-411`
- Test: `tests/test_plan_e_journal_from_entry.py`

**Interfaces:**
- Consumes: `check_exit`、`SingleTradePlan`
- Produces: journal 记录键不变（`date/code/action/qty/price/amount/pnl/pnl_pct/reason/hold_days/entry_date/signal_date`）；`hold_days` 改为**交易日计数**。

- [ ] **Step 1: 写失败测试**

`tests/test_plan_e_journal_from_entry.py`：

```python
"""journal 的出场判定与回测同源；持有期按交易日而不是自然日。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("journal_under_test",
                                                  ROOT / "scripts" / "plan_e_journal.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_journal_exit_plan_is_canonical():
    mod = _load()
    assert mod.EXIT_PLAN.judge == "intrabar"
    assert mod.EXIT_PLAN.sl_first is True


def test_journal_gap_down_uses_open():
    mod = _load()
    from tdxfeed.execution.single import check_exit
    hit = check_exit(10.0, open_=8.0, high=8.4, low=7.8, close=8.0, plan=mod.EXIT_PLAN)
    assert hit.reason == "SL" and hit.price == 8.0


def test_journal_counts_trading_days():
    mod = _load()
    assert mod._bars_held(pd.Timestamp("2026-01-09"), pd.Timestamp("2026-01-12")) == 1


def test_journal_double_trigger_settles_loss_side():
    """同一根 K 线双触发结算亏损侧——旧实现先判止盈，这是本轮的口径变化。"""
    mod = _load()
    from tdxfeed.execution.single import check_exit
    hit = check_exit(10.0, open_=10.0, high=13.5, low=8.5, close=12.0, plan=mod.EXIT_PLAN)
    assert hit.reason == "SL" and hit.price == 9.0


def test_time_due_matches_kernel_window():
    """TIME 边界与内核一致：hold_days=10 含入场根 → 入场后第 9 根到期。"""
    mod = _load()
    assert mod._time_due(8) is False
    assert mod._time_due(9) is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_plan_e_journal_from_entry.py -q`
Expected: FAIL，4 条全红——前三条 `AttributeError: module ... has no attribute 'EXIT_PLAN'`，`test_journal_counts_trading_days` 报 `'_bars_held'`。

- [ ] **Step 3: 实现**

顶部：

```python
from tdxfeed.execution.single import SingleTradePlan, check_exit

# 与回测同一口径：日内判定、双触发结算亏损侧、跳空按开盘
EXIT_PLAN = SingleTradePlan(judge="intrabar", tp=TP_PCT, sl=SL_PCT,
                            hold_days=HOLD_DAYS, sl_first=True)


def _bars_held(entry_day: pd.Timestamp, today: pd.Timestamp) -> int:
    """交易根数：用工作日近似交易日（journal 的日期循环本身就是 freq='B'）。"""
    return int(len(pd.bdate_range(entry_day, today)) - 1)


def _time_due(bars_held: int) -> bool:
    """内核口径：hold_days 含入场根，故入场后第 hold_days-1 根收盘即到期。"""
    return bars_held >= max(1, EXIT_PLAN.hold_days - 1)
```

持仓检查段（原 346-408 行的判定与记账）改为：

```python
                t = today_data.iloc[0]
                entry = p['entry_price']
                bars_held = _bars_held(pd.to_datetime(p['entry_date']), trade_date)
                hit = check_exit(entry, open_=float(t['open']), high=float(t['high']),
                                 low=float(t['low']), close=float(t['close']), plan=EXIT_PLAN)
                reason, exit_price = None, None
                if hit is not None:
                    reason = hit.reason          # 内核返回 "TP"/"SL"，与 journal 既有取值一致
                    exit_price = hit.price
                elif _time_due(bars_held):
                    reason = 'TIME'
                    exit_price = float(t['close'])
                if reason:
                    qty = p['qty']
                    proceeds = exit_price * qty
                    pnl = proceeds - entry * qty
                    pnl_pct = (exit_price - entry) / entry
                    pos['cash'] += proceeds
                    closed.append(code)
                    journal_records.append({
                        'date': date_str,
                        'code': code,
                        'action': 'SELL',
                        'qty': qty,
                        'price': round(exit_price, 2),
                        'amount': round(proceeds, 2),
                        'pnl': round(pnl, 2),
                        'pnl_pct': round(pnl_pct, 4),
                        'reason': reason,
                        'hold_days': bars_held,
                        'entry_date': p['entry_date'],
                        'signal_date': p['signal_date'],
                    })
                    print(f"SELL {code} {reason} {pnl_pct*100:+.1f}% ", end='')
```

三处配套调整：

1. **不要 `df_win`**：`check_exit` 只吃当日四个价，任何窗口裁切都是死代码（计划初稿那行 `tail(EXIT_PLAN.hold_days + 2)` 已删）。
2. **不要 `p['bars_held'] = bars_held`**：`bars_held` 只进当次记录，写回持仓字典是无消费者的状态污染。
3. **末尾平仓的 `hold_days` 同一单位**（原 529 行）：END 记录若仍按自然日算，`hold_days` 列就成了两种单位混排。改为
   `'hold_days': _bars_held(pd.to_datetime(p['entry_date']), dates[-1]),`。

TIME 边界必须走 `_time_due`，不能写 `bars_held >= EXIT_PLAN.hold_days`：内核窗口是 `range(entry_idx+1, entry_idx+hold_days)`，TIME 落在 `entry_idx + hold_days - 1`（**入场根计入**，`tdxfeed/execution/single.py:112`；`plan_e_backtest._has_exit_window` 同约定）。差一根就是阶梯分叉，正是本阶段要消灭的东西。

`EXIT_PLAN` 不需要 `replace()` 工厂：本文件的 `TP_PCT/SL_PCT/HOLD_DAYS` 是模块常量、没有 CLI 覆盖（与 Task 11 的 v3 脚本不同），`grep` 已确认无 `add_argument` 写这三个值。

同时确认过的口径事实（无需改动）：本文件只有这一处出场阶梯（`mode_sell` 是人工记账，`reason='MANUAL'`）；持仓在 Step 1 判定、Step 2 才开仓，故判定永远不会落在入场根上，内核「从 `entry_idx+1` 起判」不丢任何一日。

- [ ] **Step 4: 跑测试与回归**

Run: `venv/bin/pytest tests/test_plan_e_journal_from_entry.py tests/test_plan_e_journal_daban_entry.py tests/test_plan_e_live_retirement.py -q`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add scripts/plan_e_journal.py tests/test_plan_e_journal_from_entry.py
git commit -m "refactor(journal): 出场判定委托内核；持有期改交易日计数，止损优先+跳空按开盘"
```

---

### Task 14: 迁移 train_pnl_proxy（标签口径变，重算留到阶段 2）

**Files:**
- Modify: `scripts/train_pnl_proxy.py:60-95`（`compute_hypothetical_pnl`）
- Test: `tests/test_train_pnl_proxy_label.py`

**Interfaces:**
- Consumes: `settle_single_trade`、`SingleTradePlan`、`CostModel`
- Produces: `compute_hypothetical_pnl(code, sig_date) -> float | None` 签名不变；返回值口径改为内核 A（入场次根开始判定、亏损侧优先、跳空按开盘）。

- [ ] **Step 1: 写失败测试**

`tests/test_train_pnl_proxy_label.py`：

```python
"""训练标签必须与回测同一口径，否则模型学的是「乐观成交」。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("train_pnl_under_test",
                                                  ROOT / "scripts" / "train_pnl_proxy.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _seed(mod, bars):
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(bars)),
        "open": [b[0] for b in bars], "high": [b[1] for b in bars],
        "low": [b[2] for b in bars], "close": [b[3] for b in bars],
        "volume": [1e6] * len(bars),
    })
    mod._load_one = lambda code: df


def test_label_double_touch_is_loss_side():
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0), (10.0, 10.0, 10.0, 10.0),
                (10.0, 13.5, 8.5, 12.0)])
    pnl = mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01"))
    # 内核成本是乘性的（CostModel.round_trip），不是 ret - 0.0026 的加法：
    # 加法口径在这里会给 -0.1026，差 2.6e-4，足以让这条用例红。
    from tdxfeed.execution.costs import CostModel
    expected = CostModel(commission=0.0003, slippage=0.0010).round_trip(10.0, 9.0)
    assert pnl == pytest.approx(expected, abs=1e-12)
    assert pnl < -0.10


def test_label_ignores_entry_bar_high_low():
    """入场根自己的 high/low 不算触发（旧实现从入场根就开始判）。"""
    mod = _load()
    _seed(mod, [(10.0, 10.0, 10.0, 10.0), (10.0, 14.0, 6.0, 10.0),
                (10.0, 10.2, 9.9, 10.0)])
    pnl = mod.compute_hypothetical_pnl("600000", pd.Timestamp("2026-01-01"))
    assert pnl is not None and pnl > -0.01
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_train_pnl_proxy_label.py -q`
Expected: 两条都 FAIL（旧实现给 `-0.0026` 的 TP 结果 / 入场根就触发）

- [ ] **Step 3: 实现**

```python
# 直跑脚本时 sys.path[0] 是 scripts/，`tdxfeed` 不在其下（与 plan_e_journal:100-106 同一处理）
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.execution.bars import as_bars  # noqa: E402
from tdxfeed.execution.costs import CostModel  # noqa: E402
from tdxfeed.execution.single import SingleTradePlan, settle_single_trade  # noqa: E402


def compute_hypothetical_pnl(code: str, sig_date: pd.Timestamp) -> float | None:
    """与 plan_e 同一结算口径的 PnL 代理标签。返回 None 表示数据不足。"""
    df = _load_one(code)
    if df.empty:
        return None
    sig_match = df.index[df["date"] == sig_date]
    if len(sig_match) == 0:
        return None
    sig_i = int(sig_match[0])
    if sig_i + 1 >= len(df):
        return None
    future = df.iloc[sig_i + 1: sig_i + 1 + HOLD_DAYS].reset_index(drop=True)
    bars = as_bars(future)
    entry = float(bars["open"].iloc[0])
    if not np.isfinite(entry) or entry <= 0:
        return None
    plan = SingleTradePlan(judge="intrabar", tp=TP, sl=SL, hold_days=HOLD_DAYS, sl_first=True)
    costs = CostModel(commission=FEE / 2, slippage=SLIP / 2)
    r = settle_single_trade(bars, 0, plan, costs, insufficient="last_close")
    return None if r is None else float(r.net_ret)
```

（`FEE` / `SLIP` 在本文件里已是「双边合计」，`CostModel` 要单边值，所以除 2。返回 `r.net_ret` 而不是 `gross - FEE - SLIP`，因为内核已含成本。）

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_train_pnl_proxy_label.py -q`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add scripts/train_pnl_proxy.py tests/test_train_pnl_proxy_label.py
git commit -m "refactor(train_pnl_proxy): PnL 标签改用结算内核口径（标签将随之重算）"
```

---

### Task 15: 迁移 daban 三处（topk / paper / execution_backtest）

**Files:**
- Modify: `tdxfeed/execution/hold.py`（`HoldPlan.sell_offset`；`_row` 去掉内核不读的高低价）
- Modify: `tdxfeed/daban/topk_pnl.py:19-31,41-90`（`net_return`、`simulate_trade`）
- Modify: `tdxfeed/daban/paper.py:339-372`（`_net_actual`、`_actual_pnl`，只换成本）
- Modify: `tdxfeed/daban/execution_backtest.py:54-69`（`net_return`，只换卖出侧算术）
- Test: `tests/test_daban_hold_kernel.py`（新增）、`tests/test_execution_hold.py`、`tests/test_daban_paper.py`、`tests/test_daban_topk_pnl.py`（更新旧成本断言）

**Interfaces:**
- Consumes: `settle_hold_trade`、`HoldPlan`、`LimitRule`、`CostModel`
- Produces: `HoldPlan.sell_offset: int = 2`（仅 `t_plus_2_open` 用：卖在 `signal_idx + sell_offset` 的开盘，`reason=f"t{n}_open"`）；三个模块的公开函数签名不变；`topk_pnl.simulate_trade` 的返回键不变；`paper` 的 journal 字段不变。

**数值声明（规格 §3.5 的「否」逐条落实，实现者按此写报告）：**

- **#10 `topk`**：净收益改委托 `CostModel.round_trip`，逐笔 `|Δ| ≤ 1e-7`——旧式把买入佣金写成 `(1-fee)` 乘子，`round_trip` 写成 `1/(1+commission)`，差 `commission² ≈ 6.25e-8`；成交价、时点、复权、跳过原因不变。另有两处**标签级**变化需在报告里点名：入场根停牌且出场根缺失时内核先报 `suspended`（旧实现先报 `no_exit`，判定顺序不同）；日期字段改走 `date_str`（`YYYYMMDD` 归一成 `YYYY-MM-DD`，现有调用方本就传带横线的串）。
- **#9 `execution_backtest`**：只把卖出侧费用算术换到 `CostModel.sell`，数值差 ≤1 ulp。**不套 `round_trip`**——它是触板价成交的限价买入、买入侧无滑点，双边滑点会让每笔凭空少 5bp；`_exit_price` 的跌停判断读事件表预计算的 `next_lim_dn` 列（数据列，不是规则计算），不动。
- **#8 `paper`**：`shares>=100` 走 `CostModel.cash_pnl`（`costs.py` 的 docstring 本来就写明 `cash_pnl` 的分母与 `_net_actual` 的 `base` 同定义），`shares<100` 的「名义本金」分支保留算术（整数股合同表达不了 fractional shares），费率改从 `_costs_for(config)` 取。`slip_bps=0`（默认）时与旧算术只差浮点结合顺序，`slip_bps>0` 时佣金基数变成含滑点名义额（相对差 ≈1e-7）。**涨跌幅/涨停判定一行不改**：`paper` 用的 `daban.limit_price.{limit_ratio,limit_up_price,limit_down_price,is_st}` 在 Task 2 之后已整体委托 `execution.limits`，`execute_signal`/`settle_deferred` 的分支、`defer_count`、`notes`、`status` 全部保留。

- [ ] **Step 1: 写失败测试 / 更新旧成本断言**

`tests/test_daban_hold_kernel.py`（新增）：

```python
"""daban 三处结算同源：买不进、卖出时点、成本公式只有一套。"""
from __future__ import annotations

import pandas as pd
import pytest

from tdxfeed.daban import topk_pnl
from tdxfeed.daban.execution_backtest import ExecutionConfig
from tdxfeed.daban.execution_backtest import net_return as eb_net_return
from tdxfeed.execution.costs import CostModel


def _bars(n: int = 5) -> pd.DataFrame:
    rows = [(10.0, 10.2, 9.8, 10.0), (10.0, 10.5, 9.9, 10.4),
            (10.6, 10.8, 10.5, 10.7), (10.7, 10.9, 10.6, 10.8),
            (10.8, 11.0, 10.7, 10.9)][:n]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n),
        "open": [r[0] for r in rows], "close": [r[3] for r in rows],
        "volume": [1e6] * n,
    })


def test_topk_ret_net_uses_round_trip():
    """#10 的成本公式与内核同一份：旧式 (1-fee)^2 与 round_trip 差约 6.3e-8。"""
    out = topk_pnl.simulate_trade(_bars(), 0, "600000")
    assert out["status"] == "ok"
    cm = CostModel(commission=2.5e-4, stamp_sell=5e-4, slippage=5e-4)
    assert out["ret_net"] == pytest.approx(cm.round_trip(10.0, 10.6), abs=1e-12)


def test_topk_exit_offset_beyond_t_plus_2():
    """--exit-offset 允许 >2：内核用 sell_offset 表达，不能退化成固定 T+2。"""
    out = topk_pnl.simulate_trade(_bars(), 0, "600000", exit_offset=3)
    assert out["status"] == "ok"
    assert out["exit_date"] == "2026-01-04"
    assert out["exit_price"] == pytest.approx(10.7)


def test_topk_delegates_to_hold_kernel(monkeypatch):
    """把判定改回内联时本用例红：它钉的是调用点，不是参数对象。"""
    seen = {}
    real = topk_pnl.settle_hold_trade

    def spy(bars, signal_idx, plan, costs, rule):
        seen["plan"] = plan
        return real(bars, signal_idx, plan, costs, rule)

    monkeypatch.setattr(topk_pnl, "settle_hold_trade", spy)
    topk_pnl.simulate_trade(_bars(), 0, "600000", exit_offset=3)
    assert seen["plan"].buy == "next_open"
    assert seen["plan"].sell == "t_plus_2_open"
    assert seen["plan"].sell_offset == 3


def test_execution_backtest_keeps_limit_buy_contract():
    """#9 数值不变：限价买入不打滑点，只有卖出侧吃 slip。"""
    cfg = ExecutionConfig(fee_bps=2.5, stamp_bps=5.0, slip_bps=5.0)
    fee, stamp, slip = 2.5 / 1e4, 5.0 / 1e4, 5.0 / 1e4
    expected = (1 - fee) * (1 - fee - stamp) * (10.6 * (1 - slip)) / 10.0 - 1.0
    assert eb_net_return(10.0, 10.6, cfg) == pytest.approx(expected, rel=0, abs=1e-15)
```

`tests/test_execution_hold.py` 追加（内核 B 的偏移必须是参数，不是写死的 2）：

```python
def test_sell_offset_generalizes_t_plus_n_open():
    # daban topk 的 --exit-offset 允许 >2：卖点在 signal_idx + sell_offset 的开盘
    df = _bars([(10.0, 10.0, 10.0, 10.0), (10.0, 10.5, 9.9, 10.4),
                (10.6, 10.8, 10.5, 10.7), (10.7, 10.9, 10.6, 10.8)])
    r = settle_hold_trade(df, 0, HoldPlan(sell="t_plus_2_open", sell_offset=3), FREE, RULE)
    assert r.exit_idx == 3 and r.reason == "t3_open" and r.exit_price == 10.7
```

`tests/test_daban_paper.py` 追加（顶部补 `from tdxfeed.execution.costs import CostModel`）：

```python
def test_net_actual_delegates_to_cost_model():
    """shares>=100 走 CostModel.cash_pnl（costs.py 声明的「分母同定义」合同）。"""
    from tdxfeed.daban.paper import _net_actual
    cfg = _cfg()
    cm = CostModel(commission=cfg.fee_bps / 1e4, min_commission=cfg.min_commission,
                   stamp_sell=cfg.stamp_bps / 1e4, slippage=cfg.slip_bps / 1e4)
    assert _net_actual(11.2, 11.6, 8900, cfg) == pytest.approx(
        cm.cash_pnl(11.2, 11.6, 8900), rel=0, abs=1e-15)


def test_net_actual_nominal_branch_stays_notional():
    """shares<100（未入选行）按名义本金记账；换成整数股 cash_pnl 会差约 100 倍。"""
    from tdxfeed.daban.paper import _net_actual
    cfg = _cfg()
    notional = cfg.position_pct * cfg.capital
    buy, sell = notional, notional * 10.6 / 10.0
    cost = (max(cfg.min_commission, buy * cfg.fee_bps / 1e4)
            + max(cfg.min_commission, sell * cfg.fee_bps / 1e4)
            + sell * cfg.stamp_bps / 1e4)
    assert _net_actual(10.0, 10.6, 0, cfg) == pytest.approx(
        (sell - buy - cost) / notional, rel=0, abs=1e-15)
```

`tests/test_daban_topk_pnl.py` 的两处旧成本期望改成 `round_trip` 口径（这正是本任务声明的那 6.3e-8 变化，别用放宽容差蒙过去；顶部补 `from tdxfeed.execution.costs import CostModel`）：

```python
def test_net_return_matches_cost_formula():
    cm = CostModel(commission=2.5e-4, stamp_sell=5e-4, slippage=5e-4)
    got = net_return(10.0, 10.0, fee_bps=2.5, stamp_bps=5.0, slip_bps=5.0)
    assert got == pytest.approx(cm.round_trip(10.0, 10.0), abs=1e-12)
```

`test_simulate_trade_buys_t1_open_and_sells_t2_open` 里的 `expected` 同样换成
`CostModel(commission=2.5e-4, stamp_sell=5e-4, slippage=5e-4).round_trip(10.2, 10.5)`。

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_daban_hold_kernel.py tests/test_execution_hold.py tests/test_daban_topk_pnl.py -q`
Expected: **5 failed / 其余全绿**，失败项恰好是：
1. `test_topk_ret_net_uses_round_trip`（旧式与 `round_trip` 差 6.3e-8，`abs=1e-12` 必红）
2. `test_topk_delegates_to_hold_kernel`（模块里还没有 `settle_hold_trade`，`monkeypatch.setattr` 抛 AttributeError）
3. `test_sell_offset_generalizes_t_plus_n_open`（`HoldPlan` 还没有 `sell_offset`，TypeError）
4. `test_net_return_matches_cost_formula`（已改成 `round_trip` 期望）
5. `test_simulate_trade_buys_t1_open_and_sells_t2_open`（同上）

`test_topk_exit_offset_beyond_t_plus_2`、`test_execution_backtest_keeps_limit_buy_contract`、`tests/test_execution_hold.py` 既有 16 条、`tests/test_daban_paper.py` 两条新用例在改动前**就是绿的**——它们是迁移护栏（尤其 nominal 分支与「限价买入无滑点」），不是 RED。

- [ ] **Step 3: 实现**

`tdxfeed/execution/hold.py` —— 两处加法式扩展，默认值保持既有行为：

```python
@dataclass(frozen=True)
class HoldPlan:
    buy: Literal["next_open", "signal_close", "signal_limit_up"] = "next_open"
    sell: Literal["next_open", "next_close", "t_plus_2_open"] = "next_close"
    max_defer: int = DEFAULT_DEFER
    sell_offset: int = 2   # 仅 t_plus_2_open 用：卖在 signal_idx + sell_offset 的开盘


def _row(bars: pd.DataFrame, idx: int) -> dict:
    """只取内核真正读的四列：开盘卖出与跌停顺延都不看 high/low。"""
    r = bars.iloc[idx]
    return {"date": date_str(r["date"]), "open": float(r["open"]),
            "close": float(r["close"]), "volume": float(r["volume"])}
```

`t_plus_2_open` 分支改为：

```python
    if plan.sell == "t_plus_2_open":
        exit_idx = signal_idx + int(plan.sell_offset)
        sell_reason = f"t{int(plan.sell_offset)}_open"
        sell_at = "open"
```

（`sell_offset=2` 时 `reason` 仍是 `"t2_open"`，既有 16 条内核用例不动。`_row` 去掉 high/low 是因为 `settle_hold_trade` 从不读它们，而 daban topk 的行情帧（`load_prices` 只取 `date/open/close/volume`）本来就没有这两列——留着就是 KeyError。）

`tdxfeed/daban/topk_pnl.py` —— `net_return` 委托 `CostModel.round_trip`，`simulate_trade` 委托内核 B：

```python
from tdxfeed.execution.bars import date_str
from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.hold import HoldPlan, settle_hold_trade
from tdxfeed.execution.limits import LimitRule


def net_return(entry_price: float, exit_price: float, *, fee_bps: float = 2.5,
               stamp_bps: float = 5.0, slip_bps: float = 5.0) -> float:
    """双边滑点 + 双边佣金 + 卖出印花税，委托 `CostModel.round_trip`（唯一费用公式）。"""
    if entry_price <= 0:
        return float("nan")
    cm = CostModel(commission=fee_bps / 1e4, stamp_sell=stamp_bps / 1e4,
                   slippage=slip_bps / 1e4)
    return cm.round_trip(entry_price, exit_price)


def simulate_trade(df: pd.DataFrame, signal_idx: int, code: str, *,
                   exit_offset: int = 2, fee_bps: float = 2.5,
                   stamp_bps: float = 5.0, slip_bps: float = 5.0) -> dict[str, Any]:
    """信号日 T 收盘 → T+1 开盘买入 → T+exit_offset 开盘卖出（判定委托内核 B）。"""
    signal_date = date_str(df["date"].iloc[signal_idx])
    if exit_offset < 2:
        return _skip(code, signal_date, "t_plus_1")
    plan = HoldPlan(buy="next_open", sell="t_plus_2_open", max_defer=0,
                    sell_offset=int(exit_offset))
    costs = CostModel(commission=fee_bps / 1e4, stamp_sell=stamp_bps / 1e4,
                      slippage=slip_bps / 1e4)
    r = settle_hold_trade(df, signal_idx, plan, costs, LimitRule(code))
    if r.status == "unbuyable_open_limit":
        return _skip(code, signal_date, "open_limit")
    if r.status != "ok":
        return _skip(code, signal_date, r.status)

    # 净收益按复权价重算：内核按原始价结算（涨停判定必须用真实价），复权是调用方口径
    entry_adj = r.entry_price * _factor(df, r.entry_idx)
    exit_adj = r.exit_price * _factor(df, r.exit_idx)
    return {
        "status": "ok",
        "code": code,
        "sym": exchange_prefix(code) + code.zfill(6),
        "signal_date": signal_date,
        "entry_date": r.entry_date,
        "exit_date": r.exit_date,
        "entry_price": round(r.entry_price, 4),
        "exit_price": round(r.exit_price, 4),
        "ret_gross": exit_adj / entry_adj - 1.0,
        "ret_net": net_return(entry_adj, exit_adj, fee_bps=fee_bps,
                              stamp_bps=stamp_bps, slip_bps=slip_bps),
        "hold_days": r.exit_idx - r.entry_idx,
    }
```

要点（不要自己再调和）：
- `from tdxfeed.features_5d import get_limit, is_open_limit_up` 两个导入删除——开盘涨停由 `LimitRule` 判，两者数值等价（`get_limit` 已委托 `limit_threshold`，`features_5d.is_open_limit_up` 与 `limits.is_limit_up_ret` 都是 `ret >= limit - 0.001`）。
- 状态映射只改一个名字：内核 `unbuyable_open_limit` → 旧输出的 `open_limit`；`no_entry` / `suspended` / `no_exit` 同名直传。
- `bad_price` 分支删除：`_factor` 已把 ≤0/非有限归一到 1.0，内核只在 `entry_price > 0` 才返回 `ok`。
- `entry_price`/`exit_price` 保持**原始价**取整到 4 位、`sym`、`hold_days` 都不变；复权只进 `ret_gross`/`ret_net`（与旧实现一致）。
- 索引口径从 `.at`（标签）变 `.iloc`（位置）：`run_picks` 已 `reset_index(drop=True)`，旧代码在未 reset 的帧上本来就会错位。

`tdxfeed/daban/paper.py` —— 只换成本算术，判定与账本结构一行不动：

```python
from tdxfeed.execution.costs import CostModel


def _costs_for(config: PaperConfig) -> CostModel:
    return CostModel(commission=config.fee_bps / 1e4,
                     min_commission=config.min_commission,
                     stamp_sell=config.stamp_bps / 1e4,
                     slippage=config.slip_bps / 1e4)


def _net_actual(fill: float, sell: float, shares: int, config: PaperConfig) -> float:
    """按固定 10% 名义本金估算实际成本净收益（纸盘成交价即官方价）。"""
    notional = config.position_pct * config.capital
    if fill <= 0 or sell <= 0 or notional <= 0:
        return float("nan")
    cm = _costs_for(config)
    if shares >= 100:
        return cm.cash_pnl(fill, sell, shares)
    # 未入选行进不了 100 股：按名义本金记账（整数股合同表达不了 fractional shares）
    buy_amt = notional
    sell_amt = notional * sell / fill
    commission = max(cm.min_commission, buy_amt * cm.commission) + max(
        cm.min_commission, sell_amt * cm.commission)
    tax = sell_amt * cm.stamp_sell
    slippage = buy_amt * cm.slippage + sell_amt * cm.slippage
    return (sell_amt - buy_amt - commission - tax - slippage) / notional


def _actual_pnl(fill: float, sell: float, shares: int, config: PaperConfig) -> float:
    """book A 的绝对盈亏：同一现金合同的比率 × 买入金额。"""
    return _costs_for(config).cash_pnl(fill, sell, shares) * (fill * shares)
```

> **注意（原计划此处有误，已更正）**：`paper.py` 的涨跌幅/涨跌停价判定**已经共用**——`from tdxfeed.daban.limit_price import is_st, limit_ratio, limit_up_price, limit_down_price`，而这个模块在 Task 2 之后整体委托 `execution.limits`。所以 `execute_signal` / `settle_deferred` 的分支、`defer_count`、`notes`、`status` 全部保留；原稿那版 `_kernel_ratio(code, is_st=...)` 不存在（`limit_ratio` 只吃 `(code, name, on_date)`，硬换签名会丢掉按名称判 ST），`cash_pnl(fill, sell, 100, base=notional)` 也会把名义本金分支算成 100 股的名义额（差约 100 倍）。规格 §3.5 #8 的「数值不变」因此只剩成本这一处。

`tdxfeed/daban/execution_backtest.py` —— 卖出侧交给 `CostModel.sell`，买入侧保留「限价单无滑点」：

```python
from tdxfeed.execution.costs import CostModel


def net_return(entry: float, exit_price: float, cfg: ExecutionConfig) -> float:
    """限价买入（无买入滑点）+ 卖出滑点/双边佣金/印花税。

    不套 `round_trip`：它把双边滑点都算上，而这里是触板价成交的限价单，
    买入侧加 5bp 会让每笔收益凭空少 0.05%；`_exit_price` 的跌停判断读事件表
    预计算的 `next_lim_dn` 列（数据，不是规则计算），同样不动。
    """
    try:
        entry = float(entry)
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        return float("nan")
    if not np.isfinite(entry) or not np.isfinite(exit_price) or entry <= 0 or exit_price <= 0:
        return float("nan")
    cm = CostModel(commission=cfg.fee_bps / 1e4, stamp_sell=cfg.stamp_bps / 1e4,
                   slippage=cfg.slip_bps / 1e4)
    sell = cm.sell(exit_price, 1)
    return (1.0 - cm.commission) * sell.cash_in / entry - 1.0
```

（与旧式 `(1-fee)*(1-fee-stamp)*exit_exec/entry - 1` 数学等价，只差乘法结合顺序（≤1 ulp），`test_net_return_applies_sell_costs_only` 的 `pytest.approx` 默认容差足够；`EPS` 与 `_exit_price` 一并保留。）

- [ ] **Step 4: 跑三处回归**

Run: `venv/bin/pytest tests/test_daban_hold_kernel.py tests/test_execution_hold.py tests/test_daban_topk_pnl.py tests/test_daban_paper.py tests/test_daban_execution_backtest.py tests/test_daban_fill_ledger_net.py -q`
Expected: 全绿。任何一处数值断言失败 → 先对照 Step 1「数值声明」的三条，确认是不是已声明的差异（`topk` 6.3e-8、`paper` 滑点>0 时的 1e-7、`execution_backtest` ≤1 ulp）；**不要**为了绿把判定改回内联，也不要把 `execution_backtest` 换成双边滑点的 `round_trip`。

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/execution/hold.py tdxfeed/daban/topk_pnl.py tdxfeed/daban/paper.py \
        tdxfeed/daban/execution_backtest.py tests/test_daban_hold_kernel.py \
        tests/test_execution_hold.py tests/test_daban_topk_pnl.py tests/test_daban_paper.py
git commit -m "refactor(daban): topk 委托内核 B；paper/execution 共用成本模型（数值差异已声明）"
```

---

### Task 16: 等价回归与口径出口收尾

**Files:**
- Create: `tests/test_settle_kernel_parity.py`
- Modify: `tdxfeed/execution/__init__.py`（补齐全部导出）
- Modify: `AGENTS.md`（「回测成交口径」段）
- Modify: `docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md`（追加「实施期更正」小节，内容＝本计划开头那张表）

**Interfaces:**
- Produces: 一个随机化逐笔对照测试，任何一处内核改动导致口径漂移都会红。

- [ ] **Step 1: 写对照测试**

`tests/test_settle_kernel_parity.py`：

```python
"""随机行情上，内核 A 必须同时满足：亏损侧优先、跳空按开盘、逐日同源。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tdxfeed.execution.costs import CostModel
from tdxfeed.execution.single import SingleTradePlan, settle_single_trade


def _random_bars(seed: int, n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10.0 * np.cumprod(1 + rng.normal(0, 0.03, n))
    open_ = close * (1 + rng.normal(0, 0.01, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=n),
                         "open": open_, "high": high, "low": low,
                         "close": close, "volume": np.full(n, 1e6)})


def test_parity_ladder_is_sl_first_and_gap_at_open():
    costs = CostModel()
    plan = SingleTradePlan(tp=0.05, sl=-0.05, hold_days=5)
    for seed in range(50):
        bars = _random_bars(seed)
        r = settle_single_trade(bars, 3, plan, costs)
        if r is None or r.reason not in {"SL", "TP"}:
            continue
        k = r.exit_idx
        tp_lvl = r.entry_price * 1.05
        sl_lvl = r.entry_price * 0.95
        o = float(bars["open"].iloc[k])
        if r.reason == "SL":
            assert float(bars["low"].iloc[k]) <= sl_lvl
            assert r.exit_price == min(sl_lvl, o) if o > 0 else r.exit_price == sl_lvl
        else:
            assert float(bars["high"].iloc[k]) >= tp_lvl
            assert r.exit_price == max(tp_lvl, o) if o > 0 else r.exit_price == tp_lvl


def test_parity_daily_curve_compounds_to_net_return():
    costs = CostModel(commission=0.0003, slippage=0.0010)
    plan = SingleTradePlan(tp=0.30, sl=-0.10, hold_days=10)
    for seed in range(30):
        bars = _random_bars(seed, n=60)
        r = settle_single_trade(bars, 5, plan, costs)
        if r is None:
            continue
        curve = 1.0
        for _, inc in r.daily:
            curve *= 1.0 + inc
        # 逐日复利与单笔净收益同源，但**不是**恒等：`daily` 按日线性减成本、
        # `net_ret` 用 `round_trip` 乘成本因子，差值 ≈ |收益| × 单边成本（≤0.23%）。
        # 实测 5000 帧最大 5.3e-4。用「1% × |net_ret| + 1e-5」卡量级，别写 1e-9。
        assert abs((curve - 1.0) - r.net_ret) <= 0.01 * abs(r.net_ret) + 1e-5
        if abs(r.net_ret) > 1e-3:            # 收益接近 0 时符号无意义，不判
            assert (curve > 1.0) == (r.net_ret > 0.0)
```

（`pytest` 已在文件里 `import pytest`；上面的断言写法 `assert x == a if c else x == b` 是三元表达式，落地时拆成 `if/else` 两句，避免优先级坑。）

- [ ] **Step 2: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_settle_kernel_parity.py -q`
Expected: 2 passed

- [ ] **Step 3: 补导出与文档**

`tdxfeed/execution/__init__.py` 追加：

```python
from .bars import as_bars
from .costs import DABAN_COSTS, PLAN_E_COSTS, CostModel, Fill
from .hold import HoldPlan, settle_hold_trade
from .result import TradeResult
from .single import ExitHit, SingleTradePlan, check_exit, entry_index, exit_levels, settle_single_trade
```

并把这些名字加入 `__all__`。

`AGENTS.md`「回测成交口径（不可协商）」段追加一行：

```markdown
- 唯一实现已收进 `tdxfeed/execution/`：内核 A（`single.py`，单笔 TP/SL）与内核 B（`hold.py`，打板持有期），共用 `limits.py` / `costs.py`。新脚本从内核取口径，不要另写判定顺序；`plan_e_backtest._settle_from_entry` 现在是它的薄封装（守卫 `tests/test_settle_kernel_parity.py` + 报告 md5 闸门）。
```

规格文档末尾追加：

```markdown
## 10. 实施期更正（2026-10-28）

见 `docs/superpowers/plans/2026-10-28-settle-kernel-phase1.md` 开头的更正表：
#2 数值微变（TIME 窗口差一根 + 成本乘法口径）、#5 只收编阶梯、#8/#9 只收编 costs/limits（`paper.py` 账本与行式事件表结构保留）、§4.2 实测 50 个硬编码文件。
```

- [ ] **Step 4: 全量回归**

```bash
make test
```

Expected: `tests/` + `v5/tests/` 全绿，通过数不低于改动前（改动前为 691 + 19）。若有模块在 `data/` 缺失时被跳过，先确认 `ls data/parquet/daily | head` 非空再重跑。

- [ ] **Step 5: 提交**

```bash
git add tests/test_settle_kernel_parity.py tdxfeed/execution/__init__.py AGENTS.md docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md
git commit -m "test(execution): 内核等价回归 + 口径唯一出口收尾（含实施期更正）"
```

---

## 阶段 1 完成判据

1. `venv/bin/pytest tests/ -q` 全绿，通过数 ≥ 改动前。
2. `grep -v -e '^生成时间：' -e '^# 方案 E 回测报告 (' data/plan_e/plan_e_backtest_v2_latest.md | md5sum` 与 `/tmp/plan_e_md5_before.txt` 相同（整文件 md5 不可能相同：标题与页脚各带一个时间戳）。
3. `grep -rn "min(sl_lvl\|max(tp_lvl" --include=*.py scripts tdxfeed mcp | grep -v tdxfeed/execution` 无命中（阶梯算术只在核心里）。注意**不要**用 `sl_first` 当闸门字面：8 处命中全是 `sl_first=` 参数传递（`v3_simulate.py`、`v3_simulate_rolling.py`、`train_pnl_proxy.py`、`plan_e_backtest.py`、`plan_e_sim_live.py`、`plan_e_journal.py`、`daban_backtest.py`、`mcp/servers/backtest.py`），判定唯一在 `tdxfeed/execution/single.py`。
4. 11 个调用点都已改为调用内核/共用组件，各自的 `AGENTS.md` 声明行写在对应测试的 docstring 里。
5. 提交历史能逐条对应 Task 1–16，每个提交只含一个调用点或一组同源改动。
