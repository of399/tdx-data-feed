#!/usr/bin/env python
"""只读审计：量化 daban 标签里「主板 ST 的 5% 档被当成 10% 档」这一历史污染面。

零写入——只读 `data/parquet/daily` 与模型文件，产物就是 stdout。

    venv/bin/python scripts/audit_daban_st_tier_pollution.py model-audit
    venv/bin/python scripts/audit_daban_st_tier_pollution.py rule --limit 200
    venv/bin/python scripts/audit_daban_st_tier_pollution.py shape --sample 400
    venv/bin/python scripts/audit_daban_st_tier_pollution.py quantify --limit 300   # 抽样，先验形状
    venv/bin/python scripts/audit_daban_st_tier_pollution.py quantify --full        # 全市场（重活）
    venv/bin/python scripts/audit_daban_st_tier_pollution.py sensitivity --full
    venv/bin/python scripts/audit_daban_st_tier_pollution.py split --full

**全量扫描默认关**：`quantify/sensitivity/split` 不带 `--full` 时只跑 `--limit` 只票，
判据形状与逐票算术完全相同，但 16.8M 行那次要 5,237 个 parquet、峰值内存与几分钟墙钟都
要单独花——所以把它做成显式开关，并请在带内存上限的 scope 里跑：

    systemd-run --user --quiet --scope -p MemoryMax=12G -p MemorySwapMax=3G \\
        venv/bin/python scripts/audit_daban_st_tier_pollution.py quantify --full

问题本身
--------
现役 `data/double_up/d_signal_5d_model_daban.txt`（29 维二分类）的标签是
`close[t+1]/close[t] - 1 >= limit`（`scripts/train_daban_model.py:6`，判定 `:162-163`），
而 `limit` 取自 `get_limit(code)`——**不传 `is_st` 也不传 `on_date`**。主板 ST 在
`MAIN_ST_RULE_CHANGE_DATE` 之前是 5% 档，于是那些行按 10% 档打标签：涨幅 4.4%~9.4% 的
真涨停被记成 y=0。今天无影响（并轨后 ST 主板 = 普通主板，纯算术），缺陷只作用于
并轨之前的历史行。

样本枚举必须排除指数文件
----------------------------------------
`data/parquet/daily/` 里指数与个股同目录、同命名规则，去掉前缀后是同一个 6 位码
（`sh000001` 上证指数 / `sz000001` 平安银行），而 `market_of()` 只看数字段 ⇒ 不排除就会把
指数序列当成一只主板票进样本，行数、正样本、翻转数一起被灌水。本脚本按
`tdxfeed.symbols.INDICES` 现算排除清单（`_index_symbols`），并在末尾打印排除数。
首版本没有排除，全市场票数多 8 只——这一条与 `AGENTS.md` 无关，是脚本自己的坑。

判据为什么只能用「价格上限反推」
--------------------------------
**这一段的旧说法（"仓库没有日期化 ST 源"）已按 2026-11-05 通达信实测翻正。** 现状是分层的能力：
F10【特别处理】段落带日期（字段 `公告日期/实施日期/处理类型`），但**深度不均而且新在前**：
全市场 5,229 只的条目数实测分布是 `0:4739 / 1:182 / 2:233 / 3:60 / 4:11 / 5:4` ⇒
观测最大 **5** 条，**不存在「每票最多只有最近 3 条事件」这种硬上限**（3 条是众数，不是上限；
先前那句按 15 只小样本写，已被这轮全市场实测否证）。真正压制覆盖率的是
**4,739 / 5,229 = 90.63%** 的票这一栏「暂无数据」，而有这一栏的票只留得住最近几条
（F10 侧最早实施日期实测 2014-08-06，普遍晚于价格侧首次触档年份）；
`block_gn.dat` 一次拉全 1,379,375 行 / 1.86s，但字段里没有任何日期列 ⇒ 只能定「今天谁是 ST」；
akshare 是 `requirements.lock` 里已声明的生产依赖（`tdxfeed/finance.py:21`、`tdxfeed/corp.py:26`），
深市带日期改名面板本来就在依赖里，沪市无对应接口。
⇒ 现役票的近期段可权威判定，**台账盖不到的那一大片仍只能靠价格反推**——两类：整栏
   「暂无数据」的 90.63%，以及有栏票「最早一条之前」被截断的那段；本脚本给的就是这一段：
用价格上限反推 `regime5`（近 W 个交易日内出现过 K 次 [4.4%, CEIL] 且从未越过 CEIL），
它给出的是一条**推断**的污染面，不是权威计数——所以下面所有档位都三档并报，任何单档都不许
被引用成「污染量」。CEIL/W/K 是这支探针自己的推断参数（不是仓库契约，改它们就是改判据）；
阈值、容差、并轨日一律从 `tdxfeed.execution.limits` 派生，不在本文件手抄数字。
价格反推与台账的互证（41 只 / 2,974 票日）：ST 票日期间当日 `|pct|>5.8%` 为 0 例、票级零分歧，
但票日级有假阳性 ⇒ **精确率 ≈0.8 量级、不是 1**，详见快照 §3.3.6。

口径（与写侧同源，勿改）
------------------------
判定契约 = `limit_threshold` + `RET_TOLERANCE`（认板），**不是** `limit_ratio` +
`limit_up_price`（算价）——后者是 Decimal 取整到分的挂单价，两者不可互换（AGENTS.md
「判涨停用 limit_threshold，算挂单价用 limit_ratio」）。向量化点直接减 `RET_TOLERANCE`，
与 `tdxfeed/features_5d.py` 同一写法。
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tdxfeed import paths  # noqa: E402
from tdxfeed.execution.limits import (  # noqa: E402
    MAIN_ST_RULE_CHANGE_DATE,
    RET_TOLERANCE,
    limit_threshold,
    market_of,
    strip_exchange_prefix,
)
from tdxfeed.names import is_st_name, load_universe  # noqa: E402

# 吞错计数（SEM002）：被跳过的读盘/短帧不静默，按原因分桶，末尾统一打印
_ERROR_COUNT: Counter[str] = Counter()
# 主动排除计数：不是错误，但必须可见——否则「票少了 8 只」没人能解释
_EXCLUDE_COUNT: Counter[str] = Counter()

# —— 以下三个数全部派生自权威层，不在本文件写字面量 ——
RULE_DAY = pd.Timestamp(MAIN_ST_RULE_CHANGE_DATE)
RULE_DAY_64 = np.datetime64(RULE_DAY)
_PRE_DAY = MAIN_ST_RULE_CHANGE_DATE - timedelta(days=1)
MAIN_CUR = limit_threshold("600000")                                   # 现役：不传 is_st/on_date
GEM_CUR = limit_threshold("300000")                                    # 双创现役
MAIN_ST_PRE = limit_threshold("600000", is_st=True, on_date=_PRE_DAY)  # 并轨前主板 ST
# 认板判定值（阈值 − 容差），与 is_limit_up_ret 同口径
DEC_MAIN = MAIN_CUR - RET_TOLERANCE
DEC_GEM = GEM_CUR - RET_TOLERANCE
DEC_ST_PRE = MAIN_ST_PRE - RET_TOLERANCE
# 「摸到 5% 上限」的下沿：判定值再退一个容差，仍由权威层派生
TOUCH_LO = DEC_ST_PRE - RET_TOLERANCE

# 重建 5% 档的三档推断参数（CEIL/W/K 是判据本身，不是仓库契约）
TIERS: tuple[tuple[str, float, int, int], ...] = (
    ("loose", 0.060, 30, 2),
    ("medium", 0.058, 40, 3),
    ("strict", 0.055, 60, 6),
)
MEDIUM = dict((nm, (c, w, k)) for nm, c, w, k in TIERS)["medium"]
SIG_START = 4          # 样本空间起点：与训练侧一样要 4 根历史 + 1 根 T+1
WIN_A = 120            # rule 子命令的并轨前窗口（交易日）
MIN_ROWS = 30          # 少于这个行数的票整个跳过（与 P1 探针一致，可用 --min-rows 改）


def _read_close(fp: Path) -> pd.DataFrame | None:
    """读一只票的 date/close 两列；任何失败都在**本函数**里计数，不静默。

    键名 = 吞错所在的真函数（`_read_close`），这是 SEM002 两个守卫
    （`tests/test_error_counters.py::test_counter_keys_are_real_functions`、
    `tests/test_sem002_convergence.py::test_counter_labels_match_actual_functions`）的判据，
    防的是**拼错的键永远为 0、读起来像没有错误**。原先这里写
    `_ERROR_COUNT["read_parquet"] += 1`，键名对不上任何函数 ⇒ 判红；也不许再拆成
    「内层计数 + 外层 `except: return None`」两层——SEM002 是**逐 handler** 判的，
    外层那个不计数、不 raise 的 `except Exception` 本身就会被
    `scripts/ci_semantic_guard.py` 记一条（实测：拆两层后 `row=147 … 吞错未计数`）。
    一层写完既满足两个守卫，也保证一次失败只加一次。
    """
    try:
        return pd.read_parquet(fp, columns=["date", "close"])
    except Exception:
        _ERROR_COUNT["_read_close"] += 1
        return None


def _boards(code: str) -> str:
    """主板/双创的归类走权威层 market_of，不在这里重抄前缀表。"""
    m = market_of(code)
    if m == "main":
        return "main"
    if m == "gem":
        return "gem"
    return "other"


def _pct_today(close: np.ndarray) -> np.ndarray:
    """第 i 个元素 = 当日涨幅 close[i]/close[i-1] - 1（第 0 个为 NaN）。

    这条**只用于** regime 重建（「这一天有没有摸到 5% 上限」是当日事实）。
    标签用的是另一条 `_pct_forward`，两者差一根 K 线，混用会把标签整体错位一天。
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        step = np.where(close[:-1] > 0, close[1:] / close[:-1] - 1.0, np.nan)
    return np.concatenate(([np.nan], step))


def _pct_forward(close: np.ndarray) -> np.ndarray:
    """第 i 个元素 = 次日涨幅 close[i+1]/close[i] - 1（最后一个为 NaN）。

    与 `scripts/train_daban_model.py:162` 的 `ret_next = close[sig_idx+1]/cur_close - 1`
    同轴：标签挂在信号日 t 上，值取 t→t+1。
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        fwd = np.where(close[:-1] > 0, close[1:] / close[:-1] - 1.0, np.nan)
    return np.concatenate((fwd, [np.nan]))


def _regime5(abs_pct: np.ndarray, ok: np.ndarray, ceil_: float, w: int, k: int) -> np.ndarray:
    """近 w 个交易日（含当日）全程未越过 ceil_ 且至少 k 次落在 [TOUCH_LO, ceil_] ⇒ 视作 5% 档。"""
    le = (ok & (abs_pct <= ceil_)).astype(float)
    touch = (ok & (abs_pct >= TOUCH_LO) & (abs_pct <= ceil_)).astype(float)
    all_le = pd.Series(le).rolling(w, min_periods=w).sum().to_numpy()
    n_touch = pd.Series(touch).rolling(w, min_periods=w).sum().to_numpy()
    return (all_le >= w) & (n_touch >= k)


def _index_symbols() -> frozenset[str]:
    """日线目录里的指数文件名，取自仓库内置指数表（`tdxfeed.symbols.INDICES`）。

    这是一条**必须排除**的坑，不是可选清理：`data/parquet/daily/` 里指数与个股同目录同名规则，
    `sh000001`（上证指数）与 `sz000001`（平安银行）去掉前缀后是同一个 6 位码，而
    `market_of("000001")` 只看数字段 ⇒ 指数序列会被当成一只主板票进样本。仓库在
    `tdxfeed/freshness.py:270`、`tdxfeed/adjusted.py:7`、`tdxfeed/daban/limit_price.py:38`
    都把「同码双文件」记成已知危害。首版本脚本没排除，全市场票数因此多 6 个文件。
    """
    from tdxfeed.symbols import INDICES, sym_of_index

    return frozenset(sym_of_index(c) for c in INDICES)


def _daily_iter(boards: tuple[str, ...]) -> list[tuple[str, Path]]:
    skip = _index_symbols()
    out: list[tuple[str, Path]] = []
    for fp in paths.daily_files():
        if fp.stem in skip:
            _EXCLUDE_COUNT["index_symbol"] += 1
            continue
        code = str(strip_exchange_prefix(fp.stem))
        if _boards(code) in boards:
            out.append((code, fp))
    return out


def short_history(n: int, min_rows: int) -> bool:
    """行数不足 ⇒ **在本函数里计数**并返回 True（键名 = 函数名，SEM002 判据，见 `_read_close`）。

    两处调用点是同一件事的两道闸：读进来的帧长度、以及 `astype` 之后实际可用的长度
    （`_series` 里 `n` 与 `len(df)` 本应相等，留两道是因为日线帧可能被上游改写）。
    合并成一处会丢掉「哪一道闸拦下的」这一格，所以两处都计数、键名相同（Counter 自然累加，
    历史读数 `{'short_history': N}` 的语义不变）。
    """
    if n < min_rows:
        _ERROR_COUNT["short_history"] += 1
        return True
    return False


def _series(code: str, fp: Path, min_rows: int = MIN_ROWS) -> dict | None:
    """一只票的公共预处理：日期、涨幅、样本区段、并轨前标记。"""
    df = _read_close(fp)
    if df is None:
        return None
    if short_history(len(df), min_rows):
        return None
    df = df.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(df["date"]).to_numpy()
    close = df["close"].astype(float).to_numpy()
    n = len(close)
    if short_history(n, min_rows):
        return None
    today = _pct_today(close)
    fwd = _pct_forward(close)
    idx = np.arange(SIG_START, n - 1)
    return {
        "code": code, "n": n, "dates": dates, "close": close,
        "abs_pct": np.abs(today), "ok": np.isfinite(today),
        "idx": idx, "ret_next": fwd[idx], "date_at": dates[idx],
    }


# ────────────────────────────── model-audit ──────────────────────────────

def model_header(mp: Path) -> tuple[str | None, str | None]:
    """取 LightGBM 模型文本里的 `feature_names` / `feature_infos` 两行头。

    缺任一头就在**本函数**里计数（键名 = 函数名，SEM002 判据，见 `_read_close`）；
    调用方拿到 None 自己决定退出码，这里不改变判定，只把「统计」放在吞错的那一层。
    """
    names = infos = None
    with mp.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("feature_names="):
                names = line[len("feature_names="):].rstrip("\n")
            elif line.startswith("feature_infos="):
                infos = line[len("feature_infos="):].rstrip("\n")
                break
    if not names or not infos:
        _ERROR_COUNT["model_header"] += 1
    return names, infos


def cmd_model_audit(args: argparse.Namespace) -> int:
    """现役模型的 feature_names / feature_infos：5% 档进没进过训练矩阵。"""
    mp = paths.model_path(args.model)
    if not mp.exists():
        print(f"❌ 模型不在场: {mp}")
        return 2
    names, infos = model_header(mp)
    if not names or not infos:
        print(f"❌ {mp.name} 没有 feature_names/feature_infos 头")
        return 2
    toks = re.findall(r"\[[^\]]*\]|none", infos)
    nm = names.split()
    print(f"模型 = {mp.name}")
    print(f"feature_names={len(nm)}  feature_infos 解析出 {len(toks)} 段  "
          f"对齐={'OK' if len(nm) == len(toks) else '不等（勿引用本段的逐维结论）'}")
    if len(nm) != len(toks):
        return 2
    print(f"权威层阈值：主板现役={MAIN_CUR:g}  双创现役={GEM_CUR:g}  "
          f"并轨前主板ST={MAIN_ST_PRE:g}（判定值再减容差 {RET_TOLERANCE:g}）")
    for key in args.features.split(","):
        if key not in nm:
            print(f"  {key:20s} 不在这只模型的 29 维里")
            continue
        blk = toks[nm.index(key)]
        print(f"  {key:20s} dim={nm.index(key):>2d}  feature_infos={blk}")
        if blk != "none" and ":" in blk:
            lo, hi = blk.strip("[]").split(":")
            in_range = float(lo) <= DEC_ST_PRE <= float(hi)
            print(f"  {'':20s} ⇒ 区间 [{float(lo):g}, {float(hi):g}]；"
                  f"5% 档的判定值 {DEC_ST_PRE:g} 落在该区间内？{in_range}")
    print("旁证：`none` 段 = 该维在训练矩阵里是常数列（`is_bj` 也应是 none，北交所按设计被剔除）。")
    print("DONE_MODEL_AUDIT")
    return 0


# ────────────────────────────── rule ──────────────────────────────

def no_daily_file(code: str) -> Path | None:
    """按裸码定位日线文件；不在场就在**本函数**里计数并返回 None。

    键名 = 函数名（SEM002 判据，见 `_read_close`）。返回 None 而不是抛：这一支探针的语义是
    「跳过并让跳过可见」，`cmd_rule` 末尾还会把总数打印出来，所以计数不会被静默吃掉。
    """
    fp = paths.daily_path(code)
    if not fp.exists():
        _ERROR_COUNT["no_daily_file"] += 1
        return None
    return fp


def cmd_rule(args: argparse.Namespace) -> int:
    """并轨是否真在数据里留下痕迹：当前 ST 组 vs 对照组，窄窗 + 前后对照。"""
    uni = load_universe()
    main_st = sorted(c for c, n in uni.items() if is_st_name(n) and _boards(c) == "main")
    main_non = sorted(c for c, n in uni.items() if not is_st_name(n) and _boards(c) == "main")
    random.seed(args.seed)
    ctl = random.sample(main_non, min(len(main_st), len(main_non)))
    st_seen = main_st[:args.limit] if args.limit else main_st
    ctl_seen = ctl[:args.limit] if args.limit else ctl
    print(f"名单总数={len(uni)}  主板 ST={len(main_st)}（本轮看 {len(st_seen)}）  "
          f"主板非 ST={len(main_non)}（对照组 {len(ctl_seen)}）")

    rows = []
    for grp, codes in (("ST", st_seen), ("CONTROL", ctl_seen)):
        for code in codes:
            fp = no_daily_file(code)
            if fp is None:
                continue
            s = _series(code, fp, args.min_rows)
            if s is None:
                continue
            pre_i = np.where(s["date_at"] < RULE_DAY_64)[0]
            post_i = np.where(s["date_at"] >= RULE_DAY_64)[0]
            for side, sel in (("pre", pre_i[-WIN_A:]), ("post", post_i)):
                if sel.size < 20:
                    continue
                v = s["ret_next"][sel]
                v = v[np.isfinite(v)]
                if v.size < 20:
                    continue
                rows.append({"grp": grp, "side": side, "code": code, "n": int(v.size),
                             "p995": float(np.quantile(v, 0.995)),
                             "max": float(v.max()),
                             "frac_gt_st": float((v > MAIN_ST_PRE).mean()),
                             "n_in_band": int(((v >= DEC_ST_PRE) & (v < DEC_MAIN)).sum()),
                             "n_ge_main": int((v >= DEC_MAIN).sum())})
    r = pd.DataFrame(rows)
    if r.empty:
        print("没有可用样本")
        return 2
    print(f"有效逐票条数={len(r)}  缺失日线文件={_ERROR_COUNT['no_daily_file']}")
    for side in ("pre", "post"):
        for grp in ("ST", "CONTROL"):
            g = r[(r["side"] == side) & (r["grp"] == grp)]
            if g.empty:
                continue
            print(f"[{side:4s}/{grp:7s}] 票数={len(g):>4d}  交易日={int(g['n'].sum()):>8,}  "
                  f"正收益 p99.5 中位={g['p995'].median():.4f}  单票最大涨幅中位={g['max'].median():.4f}")
            print(f"          涨幅>{MAIN_ST_PRE:g} 的天数占比中位={g['frac_gt_st'].median() * 100:6.2f}%  "
                  f"[{DEC_ST_PRE:g},{DEC_MAIN:g}) 带内合计={int(g['n_in_band'].sum()):,}  "
                  f">={DEC_MAIN:g} 合计={int(g['n_ge_main'].sum()):,}")
    piv = r[r["side"] == "pre"].pivot_table(index="code", values="max")
    post = r[r["side"] == "post"].set_index("code")["max"]
    both = pd.concat([piv["max"], post], axis=1, keys=["pre", "post"]).dropna()
    if len(both):
        print(f"\n=== 逐票前后对照（{len(both)} 只两段都有数据；pre=并轨前最后 {WIN_A} 个交易日）===")
        n_never = int((both["pre"] < DEC_MAIN).sum())
        print(f"  pre 段从未达到 10% 档判定值 {DEC_MAIN:g} 的票数 = {n_never} / {len(both)}"
              f"   ← 与「那半年上限其实是 5%」一致；对照组同一比例见下表")
        n_post = int((both["post"] >= DEC_MAIN).sum())
        print(f"  post 段至少一天达到 {DEC_MAIN:g} 的票数 = {n_post} / {len(both)}")
        print(f"  pre 从未够着 10% 档 且 post 够着了（并轨解锁的形状）= "
              f"{int(((both['pre'] < DEC_MAIN) & (both['post'] >= DEC_MAIN)).sum())} 只")
        print(f"  pre 段就已越过 {DEC_MAIN:g} 的票数 = {int((both['pre'] >= DEC_MAIN).sum())}"
              f"   ← 这些是「5% 档不成立」的反例，逐条都要能解释（名单滞后/退市整理/停牌复牌）")
    print("DONE_RULE")
    return 0


# ────────────────────────────── quantify / sensitivity / split 的公共扫描 ──────────────────────────────

def _scan(codes_files: list[tuple[str, Path]], tier: tuple[float, int, int],
          min_rows: int = MIN_ROWS) -> dict:
    """逐票累加：样本行、现役 y=1（按板块各自阈值）、regime5 天数、0→1 翻转。"""
    ceil_, w_, k_ = tier
    acc = {
        "rows_main": 0, "rows_gem": 0, "pos_main": 0, "pos_gem": 0,
        "pos_gem_if_main_thr": 0, "reg_days": 0, "reg_pre": 0, "flip": 0,
        "by_year_flip": Counter(), "by_year_pos": Counter(), "by_year_rows": Counter(),
        "by_exch": {}, "release_st": [0, 0], "release_ctl": [0, 0], "n_codes": 0,
    }
    uni = load_universe()
    st_now = {c for c, n in uni.items() if is_st_name(n)}
    for code, fp in codes_files:
        b = _boards(code)
        s = _series(code, fp, min_rows)
        if s is None:
            continue
        acc["n_codes"] += 1
        r = s["ret_next"]
        valid = np.isfinite(r)
        thr = DEC_MAIN if b == "main" else DEC_GEM
        pos = valid & (r >= thr)
        if b == "main":
            acc["rows_main"] += int(valid.sum())
            acc["pos_main"] += int(pos.sum())
        else:
            acc["rows_gem"] += int(valid.sum())
            acc["pos_gem"] += int(pos.sum())
            # 前一支探针的口径缺陷对照：它把主板阈值也套在双创上
            acc["pos_gem_if_main_thr"] += int((valid & (r >= DEC_MAIN)).sum())
        if b != "main":
            continue
        ap, ok = s["abs_pct"], s["ok"]
        reg = _regime5(ap, ok, ceil_, w_, k_) if s["n"] >= w_ else np.zeros(s["n"], dtype=bool)
        sel_all = reg[s["idx"]] & valid
        pre = s["date_at"] < RULE_DAY_64
        sel = sel_all & pre
        acc["reg_days"] += int(sel_all.sum())
        acc["reg_pre"] += int(sel.sum())
        flip = sel & (r >= DEC_ST_PRE) & (r < DEC_MAIN)
        acc["flip"] += int(flip.sum())
        yr = pd.DatetimeIndex(s["date_at"]).year.to_numpy()
        for y, fv in zip(yr, flip, strict=True):
            acc["by_year_flip"][int(y)] += int(fv)
        for y, pv in zip(yr, pos, strict=True):
            acc["by_year_pos"][int(y)] += int(pv)
        for y in yr[valid]:
            acc["by_year_rows"][int(y)] += 1
        key = (str(fp.stem)[:2], code in st_now)
        t = acc["by_exch"].setdefault(key, [0, 0, 0, 0])
        t[0] += int(valid.sum())
        t[1] += int(pos.sum())
        t[2] += int(sel.sum())
        t[3] += int(flip.sum())
        # 同步解锁自校验：并轨前 W 日全程 regime5，且并轨后 10 日内不再出现
        pre_i = np.where(pre)[0]
        post_i = np.where(~pre)[0]
        if pre_i.size >= w_ and post_i.size >= 10:
            slot = "release_st" if code in st_now else "release_ctl"
            acc[slot][1] += 1
            # pre_i/post_i 在样本区段空间，回指全长度的 reg 必须先经 s["idx"] 换算
            at = s["idx"]
            if bool(reg[at[pre_i[-w_]]:at[pre_i[-1]] + 1].all()) \
                    and not bool(reg[at[post_i[0]]:at[post_i[min(w_, post_i.size) - 1]] + 1].any()):
                acc[slot][0] += 1
    return acc


def _pick(args: argparse.Namespace, boards: tuple[str, ...] = ("main", "gem")) -> list[tuple[str, Path]]:
    pool = _daily_iter(boards)
    if args.full or not args.limit or len(pool) <= args.limit:
        return pool
    random.seed(args.seed)
    return random.sample(pool, args.limit)


def _print_acc(acc: dict, label: str) -> None:
    rows = acc["rows_main"] + acc["rows_gem"]
    pos = acc["pos_main"] + acc["pos_gem"]
    flip = acc["flip"]
    print(f"\n=== {label} ===")
    print(f"票数={acc['n_codes']:,}  样本行={rows:,}"
          f"（主板 {acc['rows_main']:,} / 双创 {acc['rows_gem']:,}）")
    print(f"现役 y=1={pos:,}（主板 {acc['pos_main']:,} 基准率 {100 * acc['pos_main'] / max(acc['rows_main'], 1):.4f}%"
          f" / 双创 {acc['pos_gem']:,} 基准率 {100 * acc['pos_gem'] / max(acc['rows_gem'], 1):.4f}%）"
          f"  合计基准率={100 * pos / max(rows, 1):.4f}%")
    print(f"主板 5% 档天数={acc['reg_days']:,}（并轨前 {acc['reg_pre']:,}）  "
          f"标签 0→1 应翻转={flip:,}")
    if pos:
        print(f"正样本 {pos:,} → {pos + flip:,}（+{100 * flip / pos:.2f}%）  "
              f"基准率 → {100 * (pos + flip) / max(rows, 1):.4f}%")
    print(f"口径差对照：若按前一支探针把 {DEC_MAIN:g} 也套在双创上，"
          f"现役 y=1 会多算 {acc['pos_gem_if_main_thr'] - acc['pos_gem']:,} 行")


def _print_years(acc: dict) -> None:
    print("\n=== 翻转按年份（并标注该年现役正样本）===")
    for y in sorted(acc["by_year_flip"]):
        fb, pv, rw = acc["by_year_flip"][y], acc["by_year_pos"][y], acc["by_year_rows"][y]
        print(f"  {y}: 翻转={fb:>7,}  现役正={pv:>7,}  行数={rw:>9,}  "
              f"占该年正样本={100 * fb / max(pv, 1):7.1f}%  占该年行={100 * fb / max(rw, 1):.4f}%")


def _print_selfcheck(acc: dict) -> None:
    print("\n=== 判据自校验：并轨日是否出现「同步解锁」（只有规则变更能解释）===")
    for slot, lab in (("release_st", "当前名 ST 的主板票"), ("release_ctl", "当前非 ST 的主板票")):
        a, b = acc[slot]
        print(f"  {lab}: 观察 {b:,} 只，其中并轨前 {MEDIUM[1]} 日全程 regime5 且并轨后解除 = "
              f"{a:,} 只（{100 * a / max(b, 1):.1f}%）")


def _print_counters() -> None:
    if _ERROR_COUNT:
        print(f"\n计数（未静默）: {dict(_ERROR_COUNT)}")
    else:
        print("\n计数（未静默）: 无读盘失败、无短帧跳过")
    if _EXCLUDE_COUNT:
        print(f"主动排除（不参与任何计数）: {dict(_EXCLUDE_COUNT)}")


def cmd_quantify(args: argparse.Namespace) -> int:
    pool = _pick(args)
    scope = "全市场" if args.full else f"抽样 {len(pool)} 只"
    print(f"扫描范围={scope}  判据=medium {MEDIUM}  判定值 主板={DEC_MAIN:g} "
          f"双创={DEC_GEM:g} 并轨前ST={DEC_ST_PRE:g}  并轨日={RULE_DAY.date()}")
    acc = _scan(pool, MEDIUM, args.min_rows)
    _print_acc(acc, f"污染面量化（{scope}）")
    _print_years(acc)
    _print_selfcheck(acc)
    _print_counters()
    print("DONE_QUANTIFY")
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    pool = _pick(args)
    scope = "全市场" if args.full else f"抽样 {len(pool)} 只"
    print(f"扫描范围={scope}（灵敏度带：三档并报，任何单档都不许被引成「污染量」）")
    rows = []
    medium_acc: dict = {}
    for nm, ceil_, w_, k_ in TIERS:
        acc = _scan(pool, (ceil_, w_, k_), args.min_rows)
        if nm == "medium":
            medium_acc = acc
        pos = acc["pos_main"] + acc["pos_gem"]
        all_rows = acc["rows_main"] + acc["rows_gem"]
        rows.append((nm, ceil_, w_, k_, acc["reg_pre"], acc["flip"], pos, all_rows))
    print(f"\n{'档':8s} {'CEIL':>6s} {'W':>4s} {'K':>3s} {'5%档天数':>12s} {'翻转0→1':>10s} "
          f"{'现役正样本':>11s} {'放大%':>8s} {'新基准率':>9s}")
    for nm, ceil_, w_, k_, rg, fl, pos, ar in rows:
        print(f"{nm:8s} {ceil_:>6.3f} {w_:>4d} {k_:>3d} {rg:>12,} {fl:>10,} {pos:>11,} "
              f"{100 * fl / max(pos, 1):>7.2f}% {100 * (pos + fl) / max(ar, 1):>8.4f}%")
    lo = min(100 * r[5] / max(r[6], 1) for r in rows)
    hi = max(100 * r[5] / max(r[6], 1) for r in rows)
    print(f"\n实测带 = [{lo:.2f}%, {hi:.2f}%]，中心（medium）= "
          f"{100 * rows[1][5] / max(rows[1][6], 1):.2f}%")
    if args.detail:
        _print_acc(medium_acc, "medium 档明细（与 quantify --full 同一趟扫描）")
        _print_years(medium_acc)
        _print_selfcheck(medium_acc)
        _print_exchanges(medium_acc)
    _print_counters()
    print("DONE_SENSITIVITY")
    return 0


def _print_exchanges(acc: dict) -> None:
    print("\n=== 翻转按交易所前缀拆分（决定「有带日期名单的那一半」占多少）===")
    tot: dict[str, list[int]] = {}
    for (pref, isst), v in sorted(acc["by_exch"].items()):
        print(f"  {pref} {'ST' if isst else '非ST':4s} 行={v[0]:>10,} 现役正={v[1]:>9,} "
              f"5%档天数={v[2]:>10,} 翻转={v[3]:>9,}")
        t = tot.setdefault(pref, [0, 0, 0, 0])
        for i in range(4):
            t[i] += v[i]
    for pref, v in sorted(tot.items()):
        print(f"  [{pref}] 小计 行={v[0]:,} 现役正={v[1]:,} 5%档天数={v[2]:,} 翻转={v[3]:,} "
              f"翻转占该所现役正={100 * v[3] / max(v[1], 1):.2f}%")
    sh, sz = tot.get("sh", [0] * 4), tot.get("sz", [0] * 4)
    sfl = sh[3] + sz[3]
    print(f"\n沪(60) 占总翻转 = {100 * sh[3] / max(sfl, 1):.1f}%   深(00) = {100 * sz[3] / max(sfl, 1):.1f}%"
          "  （akshare 只有深市带日期改名面板 ⇒ 这就是「有名单的那一半」）")


def cmd_split(args: argparse.Namespace) -> int:
    pool = _pick(args, ("main",))
    acc = _scan(pool, MEDIUM, args.min_rows)
    _print_exchanges(acc)
    _print_counters()
    print("DONE_SPLIT")
    return 0


def cmd_shape(args: argparse.Namespace) -> int:
    """翻转行的 T+1 涨幅形状：真 5% 涨停应聚在上限的「钉子」上，低波动误判则平滑衰减。"""
    pool = _daily_iter(("main",))
    random.seed(args.seed)
    sample = random.sample(pool, min(args.sample, len(pool)))
    print(f"主板日线文件={len(pool)}  抽样={len(sample)}  判据=medium {MEDIUM}")
    ceil_, w_, k_ = MEDIUM
    fv, pv = [], []
    for code, fp in sample:
        s = _series(code, fp, args.min_rows)
        if s is None or s["n"] < w_:
            continue
        reg = _regime5(s["abs_pct"], s["ok"], ceil_, w_, k_)
        r = s["ret_next"]
        valid = np.isfinite(r)
        sel = reg[s["idx"]] & valid & (s["date_at"] < RULE_DAY_64)
        fv.append(r[sel & (r >= DEC_ST_PRE) & (r < DEC_MAIN)])
        pv.append(r[valid & (r >= DEC_MAIN)])
    fva = np.concatenate(fv) if fv else np.array([])
    pva = np.concatenate(pv) if pv else np.array([])
    print(f"flagged 翻转行={len(fva):,}  现役正样本行={len(pva):,}")
    if fva.size:
        bins = np.arange(TOUCH_LO, 0.060, 0.002)
        h, e = np.histogram(fva, bins=bins)
        print("\n=== 翻转行 T+1 涨幅直方图（0.2pp 一格）===")
        for i, cnt in enumerate(h):
            print(f"  [{e[i]:.3f},{e[i + 1]:.3f})  {cnt:>6,}  {'#' * int(60 * cnt / max(h.max(), 1))}")
        pin_lo, pin_hi = MAIN_ST_PRE - 0.002, MAIN_ST_PRE + 0.002
        n_pin = int(((fva >= pin_lo) & (fva <= pin_hi)).sum())
        print(f"\n落在 [{pin_lo:g},{pin_hi:g}]「钉子」内 = {n_pin:,}/{len(fva):,} = {100 * n_pin / len(fva):.1f}%")
        print(f"中位={np.median(fva):.4f} p25={np.quantile(fva, .25):.4f} "
              f"p75={np.quantile(fva, .75):.4f} p95={np.quantile(fva, .95):.4f} max={fva.max():.4f}")
    if pva.size:
        lo, hi = DEC_MAIN, MAIN_CUR + 0.005
        n2 = int(((pva >= lo) & (pva <= hi)).sum())
        print(f"\n对照：现役正样本落在 [{lo:g},{hi:g}] 钉子内 = {100 * n2 / len(pva):.1f}%  "
              f"（中位={np.median(pva):.4f} p95={np.quantile(pva, .95):.4f}）")
    _print_counters()
    print("DONE_SHAPE")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, limit: int | None) -> None:
        sp.add_argument("--full", action="store_true", help="全市场扫描（默认关）")
        sp.add_argument("--min-rows", type=int, default=MIN_ROWS,
                        help=f"行数下限，低于则整只跳过（默认 {MIN_ROWS}）")
        sp.add_argument("--limit", type=int, default=limit,
                        help=f"抽样只数（默认 {limit}；与 --full 同用则忽略）")
        sp.add_argument("--seed", type=int, default=42, help="抽样种子")

    p = sub.add_parser("model-audit", help="现役模型的 feature_infos：低档阈值进没进过训练矩阵")
    p.add_argument("--model", default="d_signal_5d_model_daban.txt")
    p.add_argument("--features", default="limit,is_st,is_bj")
    p.set_defaults(func=cmd_model_audit)

    p = sub.add_parser("rule", help="并轨日在价格侧留没留痕迹（ST 组 vs 对照组）")
    common(p, 200)
    p.set_defaults(func=cmd_rule)

    p = sub.add_parser("quantify", help="污染面量化（medium 档）")
    common(p, 300)
    p.set_defaults(func=cmd_quantify)

    p = sub.add_parser("sensitivity", help="三档判据并报，量出灵敏度带")
    common(p, 150)
    p.add_argument("--detail", action="store_true",
                   help="medium 档顺带打印年份/自校验/交易所明细（省一次全市场重扫）")
    p.set_defaults(func=cmd_sensitivity)

    p = sub.add_parser("split", help="翻转按交易所（沪/深）拆分")
    common(p, 300)
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("shape", help="翻转行的涨幅形状（钉子上有没有尖峰）")
    p.add_argument("--sample", type=int, default=400)
    p.add_argument("--min-rows", type=int, default=MIN_ROWS)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=cmd_shape)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
