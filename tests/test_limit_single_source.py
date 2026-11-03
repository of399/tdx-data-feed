"""三份涨跌幅实现必须同源：改规则只改 tdxfeed/execution/limits.py 一处。"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

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


def test_daban_is_sealed_follows_kernel(monkeypatch):
    """内核实现下 1.0 < 2.0-0.005 应判 False；哨兵 True 能抓住本地实现回退。"""
    monkeypatch.setattr(limits, "is_sealed", lambda *a, **k: True)
    assert limit_price.is_sealed(1.0, 2.0) is True


def test_daban_as_date_follows_kernel(monkeypatch):
    """正常解析得 2026-01-01，哨兵 2030-01-01 与本地实现结果不同。"""
    from datetime import date
    monkeypatch.setattr(limits, "as_date", lambda *a, **k: date(2030, 1, 1))
    assert limit_price.as_date("20260101") == date(2030, 1, 1)


def test_quant_zt_density_uses_market_threshold():
    """689 是科创板：+10% 不算涨停（委托前 quant 按主板 9.5% 误判）。"""
    import numpy as np
    import pandas as pd

    from quant.multifactor.factors import compute_stock_raw

    n = 30
    close = np.full(n, 10.0)
    close[-1] = 11.0
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=n),
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1e6, "amount": 1e7,
    })
    assert float(compute_stock_raw(df, "sh600519")["zt_density_20d"].iloc[-1]) == pytest.approx(0.05)
    assert float(compute_stock_raw(df, "sh689009")["zt_density_20d"].iloc[-1]) == pytest.approx(0.0)
    assert float(compute_stock_raw(df, "sz300750")["zt_density_20d"].iloc[-1]) == pytest.approx(0.0)


def test_threshold_change_is_confined_to_689():
    """委托前后阈值只在 689 段变化：范围变大就等于 mf 报表出现无法归因的差异。"""
    import re

    from quant.multifactor.factors import get_limit
    from tdxfeed.paths import daily_files

    def legacy(code: str) -> float:
        s = re.sub(r"^(sh|sz|bj)", "", str(code))
        if s.startswith("920") or s.startswith("8"):
            return 0.295
        if s.startswith("688") or s.startswith("300") or s.startswith("301") or s.startswith("200"):
            return 0.195
        return 0.095

    files = daily_files()
    if not files:
        pytest.skip("无行情目录，跳过全代码分类扫描")
    changed = [fp.stem for fp in files if abs(legacy(fp.stem) - get_limit(fp.stem)) > 1e-12]
    assert "sh689009" in changed
    assert [c for c in changed if c != "sh689009"] == []


SCRIPT_GET_LIMIT = (
    "scripts/daban_event_table.py",
    "scripts/factor_engine.py",
    "scripts/top10_deep_dive.py",
    "scripts/find_double_up_c.py",
    "scripts/find_double_up_c_v2.py",
    "scripts/v3_premarket_top.py",
)
ROOT = Path(__file__).resolve().parents[1]


def test_daban_exchange_prefix_delegates_to_paths():
    from tdxfeed import paths
    assert limit_price.exchange_prefix is paths.exchange_prefix


def test_daban_exchange_prefix_keeps_explicit_prefix():
    """sh000001 是上证指数；重推会指到 sz000001 平安银行。"""
    assert limit_price.exchange_prefix("sh000001") == "sh"
    assert limit_price.exchange_prefix("sz000001") == "sz"
    assert limit_price.exchange_prefix("000001") == "sz"


def test_scripts_have_no_local_limit_table():
    """六个脚本不得再持有阈值字面量或本地 get_limit 实现。"""
    offenders = []
    for rel in SCRIPT_GET_LIMIT:
        text = (ROOT / rel).read_text(encoding="utf-8")
        if any(v in text for v in ("0.095", "0.195", "0.295")):
            offenders.append(f"{rel}: 阈值字面量")
        if "def get_limit" in text:
            offenders.append(f"{rel}: 本地 get_limit")
    assert offenders == []


# --------------------------------------------------------------------------- #
# P1-12 Phase 3（台账 KI-SINGLE-004）：容差与阈值常量单源，**零口径变化**去重
# --------------------------------------------------------------------------- #

# 台账 detect 的口径。原规格只认 `limit - 0.001`，漏掉同契约的 limits / limit_val / lim /
# df['limit'] 等写法（9 处），故按变量形态放宽；价格级 EPS（ztj/limit_price - EPS，
# 含 min60 的 EPS=0.005）是**另一契约**，不在此列、也不得并入 RET_TOLERANCE。
RET_TOL_RE = re.compile(r"""(limit[A-Za-z_]*|lim|\[["']limit["']\])\s*-\s*0\.001""")
TOL_DOMAINS = ("tdxfeed", "scripts", "quant", "mcp")
TOL_SKIP = {"venv", "venv-math", "_archive", ".git", "node_modules", "__pycache__"}

# 收编后必须引用权威容差的站点（反作弊用，见下）
RET_TOL_SITES = (
    "tdxfeed/features_5d.py",
    "scripts/predict.py",
    "scripts/daban_label_diagnose.py",
    "scripts/v3_premarket_top.py",
    "scripts/find_double_up_c.py",
    "scripts/find_double_up_hfq.py",
    "scripts/factor_engine.py",
    "scripts/top10_deep_dive.py",
    "scripts/find_double_up_c_v2.py",
    "quant/multifactor/factors.py",
)


def test_no_inline_ret_tolerance_literal():
    """全仓不得再出现「阈值 − 字面量 0.001」的涨停判定（含 docstring 里的示例）。"""
    offenders = []
    for base in TOL_DOMAINS:
        for fp in sorted((ROOT / base).rglob("*.py")):
            if any(part in TOL_SKIP for part in fp.relative_to(ROOT).parts):
                continue
            for i, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
                if RET_TOL_RE.search(line):
                    offenders.append(f"{fp.relative_to(ROOT).as_posix()}:{i}")
    assert offenders == [], "容差走 limits.RET_TOLERANCE / is_limit_up_ret，不要手抄字面量"


def test_sites_reference_authoritative_tolerance():
    """把容差整段删掉也能让上一条变绿——但那是**改口径**（判定变严），不是去重。"""
    for rel in RET_TOL_SITES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "RET_TOLERANCE" in text or "is_limit_up_ret" in text, f"{rel} 未引用权威容差"


def test_ret_tolerance_value_pinned():
    """去重不许顺手改值：0.001 是浮点容忍（Phase 0② 结论），改了就是收窄判定。"""
    assert limits.RET_TOLERANCE == 0.001


def test_features_is_limit_up_ret_delegates(monkeypatch):
    """哨兵替换内核后 features 入口必须跟着变，否则只是「值碰巧相等」的假委托。"""
    monkeypatch.setattr(limits, "is_limit_up_ret", lambda ret, limit: ret > 999)
    assert features_5d.is_limit_up_ret(0.10, 0.095) is False
    assert features_5d.is_limit_up_ret(1000.0, 0.095) is True


def test_limit_map_is_derived_not_hardcoded():
    src = (ROOT / "tdxfeed" / "features_5d.py").read_text(encoding="utf-8")
    for lit in ("0.095", "0.195", "0.295", "0.045"):
        assert lit not in src, f"features_5d 不得硬编码阈值 {lit}（应从 limits 派生）"
    buf = Decimal(str(limits.DEFAULT_THRESHOLD_BUFFER))
    assert {m: float(r - buf) for m, r in limits.LIMIT_RATIO.items()} == features_5d.LIMIT_MAP
    assert float(limits.LIMIT_RATIO_ST_MAIN_OLD - buf) == features_5d.LIMIT_ST_MAIN_OLD


@pytest.mark.parametrize("limit", [0.095, 0.195, 0.295, 0.045])
def test_is_limit_up_ret_grid_parity(limit):
    """逐点等价证明：与旧表达式（字面量容差）在 0.0001 步长网格上完全一致。"""
    import numpy as np

    rets = np.arange(-0.05, 0.35, 0.0001)
    got = [features_5d.is_limit_up_ret(float(r), limit) for r in rets]
    want = [bool(r >= limit - 0.001) for r in rets]
    assert got == want
