"""名单 ↔ 日线目录的双向差守卫（数据依赖：无 `data/` 时整模块收集期跳过）。

判据来自 2026-11-06 首轮 `scripts/update_universe.py` 刷新后的实测：**名单 5,579 条 vs
日线规范名 5,232 个文件（裸码 5,229 只）⇒ 双向差实测 8（日线有名单无）/ 355（名单有日线无）**。
钉的是**那一天**的读数，语义分两条：

- **名单有、日线无**：只允许北交所 `920` 段（355 只）——`data/parquet/daily` 里没有它们的日线
  （北交所日线 2026-10-21 下线，见 `scripts/INDEX.md`），这是这条链的已知形状
  （KI-DATA-003「日线无 bj」），不是名单错。**这一侧按「段」给豁免而不是按码登记**，
  所以以后新上市的 920 票不会让它变红（否则每次 IPO 都要来改表）。
- **日线有、名单无**：只允许下面登记的 8 只，且每只都要写清为什么不在当日挂牌清单里。
  这一侧是**闭合判据（双向相等）**，所以它会随市场变红——那是刻意的：多出一只就意味着
  「有新标的进了日线而名单没刷」，修法是先 `make data-universe` 再重跑本判据；
  刷新之后**仍然**剩下的那只才需要登记（登记 = 承认它是例外，并写下理由）。
  已登记的条目若哪天不再出现在差集里（例如退市票被补进名单、或日线文件被清掉），
  同一条判据会以 `daily_only_stale_registration` 变红 ⇒ 这张表不允许腐化。

换句话说：本表**不是**「钉住 2026-11-06 现状不许动」，而是「920 段按段放行 + 其余逐条登记理由」，
红了先看名单有没有刷，再决定登记还是修数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

NEEDS_DATA = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdxfeed import names, paths  # noqa: E402

# 日线里有、当日挂牌名单里没有的 8 只（2026-11-06 实测，逐条给理由）
DAILY_ONLY_ALLOWED = {
    "000300": "沪深300 指数文件（daily 里 000300 只有 sh000300，个股同名文件不存在）",
    "399001": "深证成指（指数，不是股票）",
    "399006": "创业板指（指数，不是股票）",
    "600001": "已退市个股 sh600001：1,023 行 2005-04-19→2009-12-15，不在 INDICES 里",
    "000016": "旧名单里的 *ST康佳A，2026-11-06 交易所当日挂牌清单已无此码（退市/换号）",
    "301139": "旧名单里的 *ST元道，当日挂牌清单已无此码",
    "601059": "信达证券：国泰海通合并后退市，日线仍是历史段",
    "601198": "东兴证券：同上（合并退市）",
}
UNIVERSE_ONLY_PREFIX = "920"


def bare_codes_in_daily() -> set[str]:
    """日线目录里的裸 6 位码（`paths.daily_files()` 只认规范名，备份与残留不算）。"""
    out = set()
    for fp in paths.daily_files():
        stem = fp.stem
        out.add(stem[2:] if stem[:2] in ("sh", "sz", "bj") else stem)
    return out


def parity_violations(*, daily: set[str], universe: set[str],
                      daily_only_allowed: set[str],
                      universe_only_prefix: str) -> dict[str, set[str]]:
    """把两个集合的差拆成「已登记 / 未登记」两类读数。

    `universe_only_prefix` 是**唯一**允许「名单有、日线无」的段（北交所 920）；
    其它段出现这种差就说明名单收了日线根本没有的东西——那是下一轮读表的坑。
    """
    daily_only = daily - universe
    universe_only = universe - daily
    return {
        "daily_only_unregistered": daily_only - daily_only_allowed,
        "daily_only_stale_registration": daily_only_allowed - daily_only,
        "universe_only_unexpected": {c for c in universe_only
                                     if not c.startswith(universe_only_prefix)},
    }


def test_parity_probe_catches_unregistered_drift() -> None:
    """变异对照：未登记的差、腐化的登记、非 920 段的「名单有日线无」，三类都要判出来。"""
    daily = {"600000", "000300", "920999"}
    universe = {"600000", "000300", "688981", "920001"}
    bad = parity_violations(daily=daily, universe=universe,
                            daily_only_allowed={"920999", "600111"},
                            universe_only_prefix="920")
    assert bad["daily_only_unregistered"] == set(), bad          # 920999 已登记
    assert bad["daily_only_stale_registration"] == {"600111"}, bad  # 登记了但已不在差集里
    assert bad["universe_only_unexpected"] == {"688981"}, bad    # 920001 属允许段，688981 不是

    # 登记表清空 ⇒ 920999 立刻变成未登记漂移（这条防止「登记当免检」）
    assert parity_violations(daily=daily, universe=universe, daily_only_allowed=set(),
                             universe_only_prefix="920")["daily_only_unregistered"] == {"920999"}


def test_registration_rows_carry_a_verifiable_reason() -> None:
    """登记 = 承认例外并写下可核验的理由；空理由或"未知"式占位不允许进表。

    哪个生产改动会让它红：有人图省事把新出现的差集直接登记成 `""`/「待查」，
    那等于把闭合判据降级成免检清单（判据还在，内容已空）。
    """
    for code, reason in DAILY_ONLY_ALLOWED.items():
        assert code.isdigit() and len(code) == 6, f"登记键必须是 6 位数字裸码：{code!r}"
        text = str(reason).strip()
        assert len(text) >= 12, f"{code} 的理由太短，读不出为什么是例外：{text!r}"
        assert not any(bad in text for bad in ("待查", "未知", "TODO", "占位")), \
            f"{code} 的理由是占位文字：{text!r}"


def test_prefix_exemption_does_not_license_a_non_920_gap() -> None:
    """`920` 段的豁免只吃 920：非 920 段的「名单有、日线无」照样判红（防豁免扩成免检）。"""
    daily, universe = {"600000"}, {"600000", "920001", "688999"}
    bad = parity_violations(daily=daily, universe=universe,
                            daily_only_allowed=set(), universe_only_prefix="920")
    assert bad["universe_only_unexpected"] == {"688999"}, bad


def test_segment_exemption_is_a_real_segment_not_a_vacuous_one() -> None:
    """段豁免必须是**真段**：它在真数据上得真的在吃数，且不得匹配名单里的每一个码。

    哪个生产改动会让它红：有人把 `UNIVERSE_ONLY_PREFIX` 改成 `""` 想「收紧判据」——
    实测 `str.startswith("")` 恒真，空前缀把 355 只全放行，判据还在但内容已被清空。
    2026-11-06 的三个读数：`920` ⇒ 0 命中、不匹配的段 ⇒ 355 命中、`""` ⇒ 0 命中。
    """
    universe = set(names.load_universe())
    daily = bare_codes_in_daily()
    gap = universe - daily
    assert gap, "「名单有、日线无」为空 ⇒ 段豁免没有内容，这一格是空话"
    assert not all(c.startswith(UNIVERSE_ONLY_PREFIX) for c in universe), \
        f"前缀 {UNIVERSE_ONLY_PREFIX!r} 匹配了名单里每一个码 ⇒ 等于全免检（空前缀就是这个形状）"
    assert all(c.startswith(UNIVERSE_ONLY_PREFIX) for c in gap), \
        f"差集里混进了别的段：{sorted(gap)[:5]}"
    bad = parity_violations(daily=daily, universe=universe,
                            daily_only_allowed=set(DAILY_ONLY_ALLOWED),
                            universe_only_prefix="没有这种段")
    assert bad["universe_only_unexpected"] == gap, \
        "换成不匹配的段必须逐只判红；不匹配却仍为 0 ⇒ 豁免其实没在吃数"


def test_universe_and_daily_differ_exactly_as_registered() -> None:
    """真数据判据：双向差必须**逐只**落在登记表里（多一只是漂移，少一只是登记表腐化）。

    哪个生产改动会让它红：新标的进了日线而名单没刷（修法是 `make data-universe`），
    或名单收了日线里根本没有的非 920 段代码。
    """
    universe = set(names.load_universe())
    daily = bare_codes_in_daily()
    bad = parity_violations(daily=daily, universe=universe,
                            daily_only_allowed=set(DAILY_ONLY_ALLOWED),
                            universe_only_prefix=UNIVERSE_ONLY_PREFIX)
    assert not any(bad.values()), (
        f"名单 {len(universe):,} 条 vs 日线 {len(daily):,} 条，差集越界："
        f"未登记的「日线有名单无」{sorted(bad['daily_only_unregistered'])}"
        f"（修法：make data-universe）；已失效的登记条目 "
        f"{sorted(bad['daily_only_stale_registration'])}（删掉或换新码）；"
        f"非 {UNIVERSE_ONLY_PREFIX} 段的「名单有日线无」{sorted(bad['universe_only_unexpected'])}")
