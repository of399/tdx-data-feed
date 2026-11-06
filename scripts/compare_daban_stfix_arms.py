"""B2 四臂对照评估：同一冻结行集上，把 ST 档供给的净效应量出来（2026-11-05）。

═══════════════════════════════════════════════════════════════════════════════
【这批在判什么】KI-DATA-008 说「并轨前的主板 ST 按 10% 档打标签 ⇒ 正样本系统性漏记」，
B1 把日期与真实 ST 状态接进了训练/特征侧（管道），本批把**产物**跑出来对照：

    ctl  ST 档强制关（归因基线，逐值等于改动前的旧口径）
    A    权威层只读 f10_ledger + block_gn
    A2   权威三层并读（+ akshare_sz）——B1 之后的默认生产口径
    I    A2 + price_inferred（价格反推，只在权威判不出时生效）

**主判据 = `ctl` vs `A2` 这一对；四臂全部进表**（2026-11-05 21:21:41 曾收到一条「只跑两臂」的
改派，送达时四臂 20:30 前已全部跑完、对照评估 20:36 跑完、结论快照 21:20 定稿 ⇒ **改派未按期
生效，本批按四臂验收**；`A`/`I` 是**已产出的灵敏度读数**，可引用方向但**不替代主判据**，也不得
因为它们与 `A2` 接近就合并成「修好了」）。**本脚本仍然要求四臂齐全**——比较集是预登记的一部分，缩小它要由人显式授权并留痕，
不能在命令行上临时改（`_parse_models` 的缺臂守卫，钉为
`tests/test_daban_stfix_arms.py::test_reduced_arm_set_is_refused_not_rescaled`；改派期间那次
两臂实跑在 21:22:59 退出码 1、未产文件，所以「只看两臂」的读数只能从那次四臂预登记运行里
按臂取行——而 CI/p 属于「三臂 vs ctl」自助族的 rng 流位置，不是两臂独立重算的自助）。

四臂**只许差在 dim1(limit) 与 dim15(is_st) 的供给上**：超参、切分、采样、特征管线
逐字相同（`train_daban_model.invariant_lines()` 每次实跑都打印，并写进各臂训练报告）。

判据预登记（先定再看数，不得事后挑）：

  · 主判据 = 逐日 Top-5 命中率的**配对差**：抽样单位=交易日，按日整块自助，
    `n_boot=2000`、`seed=42`，与 union47 那轮**同一个引擎**
    （`stage1_union_compare.cluster_bootstrap_delta`，含 `1/(n_boot+1)` 的经验 p 下限）
  · 次判据 = Top-5 位移（逐日名单里换了几个槽位 / K）与 lift
  · AUC 只当参考，不当判据
  · **切流必要条件**：Δ vs ctl 的 95% CI 下界 > 0（方向为正且不跳 0）；
    否则结论写「证据不足，不切」，**不写「证伪」**
  · q 值按仓库现有 BH（`quant.multifactor.cv_utils.bh_fdr_correct`），
    比较集 = 全部非基线臂；每条都报 q，不得只报最小 p 的那一条

【为什么行集必须冻结】四臂若各自重扫样本集，A2 多出来的正样本会同时改变**标签**与
**样本**，Δ 里就混进两个成分。所以本脚本只在
`data/double_up/daban_pattern_eval_20261022.parquet` 那 1,729,388 行 `(code, date)` 键上
打分：行集不动，动的是每行的特征值与标签。列名/行数/唯一键任一不符 ⇒
`SnapshotContractError` 停下，不默默重装配。

【三个已知坑，都在这里当场处理，不靠注释】
1. **快照里的撞码指数行**。`train_daban_model.iter_stock_files` 按裸 6 位码记账，
   `sh000001`（上证指数）等 5 只指数与同码个股撞车（实测 1,643 个重复键 / 3,286 行，
   占 0.190%；取证 `docs/research/2026-11-05-daban-label-caliber-and-displacement.md` §5.2）。
   本脚本**按行**用 `prev_close` 反查它来自哪个日线文件（不是按票选一个），并把来源文件名
   带进产物，让「这一行是指数」可查、可做敏感性。**行数不因之改变**——披露，不是过滤。
2. **快照的数值是 10-22 的**。之后日线补了 `amount`（快照里 2026-09/10 有 78,087 行
   `avg_amount_5d=0`），`48dcc6a`（2026-10-28）又把 `689` 归进科创板。所以重算的 ctl
   特征与快照存值**本来就不该逐格相同**。脚本逐列把差数打印出来当披露项，同时单独
   断言「旧标签」的复现率（标签只看 close 序列，不吃 amount）。
3. **主判据只能用统一标签**。四臂各用自己的标签算命中率，A2 多出来的正样本会同时抬高
   自己的分子——所以主表统一用 A2 标签（`PRIMARY_LABEL_SOURCE`），另附一张旧标签表，
   两套都写分母（行数 / 正样本数 / 天数）。

用法（实跑；只写对照产物，绝不碰现役模型）：
    venv/bin/python scripts/compare_daban_stfix_arms.py \\
        --model ctl=data/double_up/d_signal_5d_model_daban_stfix_20261105_ctl.txt \\
        --model A2=data/double_up/d_signal_5d_model_daban_stfix_20261105_A2.txt \\
        --out-md data/double_up/daban_stfix_arms_20261105.md
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tdxfeed")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lightgbm as lgb  # noqa: E402
from report_daban_topk import _auc  # noqa: E402  # 全仓唯一的 daban AUC 实现

# 复用而不是复制：同一自助引擎、同一 p 值下限与 CI 口径，才能和 union47 那轮并排读
from stage1_union_compare import cluster_bootstrap_delta  # noqa: E402

from tdxfeed import (  # noqa: E402
    paths,
    st_supply as sup,
    symbols as _sym,
)
from tdxfeed.atomic import atomic_write_text  # noqa: E402  # SEM001 原子写
from tdxfeed.features_5d import (  # noqa: E402
    compute_window_features_extended,
    get_limit,
    is_limit_up_ret,
)

# 指数文件的 symbol（`000001` 既是上证指数也是平安银行 ⇒ 撞码就是这几只）。
# 数「这一行来自指数文件」必须用**名单**，不能用「文件名是不是 sh<code>」——
# 对 600000 来说 `sh600000` 就是个股，那种写法会把个股数成指数、把指数数成个股。
_INDEX_STEMS = frozenset(_sym.sym_of_index(c) for c in _sym.INDICES)

SNAPSHOT = ROOT / "data" / "double_up" / "daban_pattern_eval_20261022.parquet"
LIVE_MODEL = ROOT / "data" / "double_up" / "d_signal_5d_model_daban.txt"
OLD_LABEL = "label"                    # 快照里的旧口径标签列（不传日期的 get_limit 算出来的）
MERGED_OLD_LABEL = "label_old"         # 重算表里带过来的同一列（改名以免与臂标签混淆）
PRIMARY_ARM = "A2"                      # 主判据统一标签来自哪一臂（预登记，不得看完数改）
PRIMARY_LABEL_SOURCE = f"label_{PRIMARY_ARM}"   # 四臂共用的那一列（模块头第 3 条）
UNIFIED_KEY = f"unified_{PRIMARY_ARM.lower()}"  # 报告里那套统一标签表的名字
KEY_COLS = ("date", "code")
METRIC = "hit_rate"

# 预登记的判据数字。改它们=改判据，必须在交付里单独说明，不许看完数之后动。
PROTOCOL: dict[str, Any] = {
    "primary": "top5_hit_rate_day_mean",
    "k": 5,
    "n_boot": 2000,
    "seed": 42,
    "baseline": "ctl",
    "sampling_unit": "trading_day",
    "bh": "per_family_over_non_baseline_arms",
}

SNAP_ROWS_MEASURED = 1_729_388     # 2026-11-05 实读；行集契约的登记值
SNAP_KEYS_MEASURED = 1_727_745     # 唯一 (code,date) 键数（差值 = 撞码指数行）
DUP_CODES_REGISTERED = ("000001", "000016", "000688", "000852", "000905")

# 阈值渗进这几维（`tdxfeed/features_5d.py:181,183,274`），所以逐臂必须重算整份特征
LIMIT_LEAK_COLS = ("limit", "limit_up_5d", "limit_up_t1", "is_limit_up_today",
                   "consecutive_boards", "board_position", "is_st")

_ERROR_COUNT: Counter[str] = Counter()      # SEM002：被交代的错误按函数名分桶


class SnapshotContractError(RuntimeError):
    """冻结快照的行集/列契约不符 ⇒ 停下报错，不默默重装配（任务书第 4 条）。"""


# --------------------------------------------------------------------------- #
# 一、行集与列契约
# --------------------------------------------------------------------------- #
def check_snapshot_contract(snap: pd.DataFrame, *, feature_cols: list[str],
                            n_rows_expected: int | None = SNAP_ROWS_MEASURED,
                            allow_duplicate_keys: bool = True) -> int:
    """断言「四臂就在这一份行集上、用这一套列打分」，并把已知撞码如实打印。

    返回行数（原样，**不过滤**）。任何一条不符都抛：缺列 = 装配时少算了一维；
    行数变 = 有人顺手加了过滤；唯一键数变 = 快照被重排或部分重写。
    """
    missing = [c for c in feature_cols if c not in snap.columns]
    if missing:
        raise SnapshotContractError(
            f"模型要 {len(missing)} 列不在冻结快照里：{missing[:8]} ⇒ 特征管线与快照口径"
            "不一致。**不许默默重装配**（重装配=换行集，四臂就不可比了）")
    if n_rows_expected is not None and len(snap) != n_rows_expected:
        raise SnapshotContractError(
            f"快照实读 {len(snap):,} 行，登记的是 {n_rows_expected:,} 行 ⇒ 这份快照不是"
            "被引用的那一份（或被人重写过）。行集是四臂可比的前提，差一行也要停下来问")
    for col in (*KEY_COLS, OLD_LABEL, "prev_close"):
        if col not in snap.columns:
            raise SnapshotContractError(f"快照缺列 {col!r} ⇒ 无法按行定位/无法给附表分母")
    dup = int(snap.duplicated(list(KEY_COLS)).sum())
    if dup and not allow_duplicate_keys:
        raise SnapshotContractError(
            f"快照有 {dup} 行重复 (code,date) 键，而调用方要求键唯一 ⇒ 按 (code,date) "
            "取特征会把 1 行放大成 2 行（那就是默默重装配）")
    if dup:
        codes = sorted({str(c) for c in
                        snap.loc[snap.duplicated(list(KEY_COLS), keep=False), "code"]})
        print(f"[行集披露] 重复 (code,date) 键 {dup:,} 行 / 撞码裸码 {len(codes)} 个："
              f"{codes}（同码指数与个股撞车）。本脚本按**行**反查来源文件，"
              f"行数保持 {len(snap):,} 不变")
        if tuple(codes) != DUP_CODES_REGISTERED:
            print(f"           ⚠ 撞码清单与登记值不一致：登记 {list(DUP_CODES_REGISTERED)}")
    uniq = int(snap.drop_duplicates(list(KEY_COLS)).shape[0])
    if n_rows_expected == SNAP_ROWS_MEASURED and uniq != SNAP_KEYS_MEASURED:
        # 只对**那份登记过的冻结快照**查唯一键数：合成用例没有登记值可对
        raise SnapshotContractError(
            f"唯一 (code,date) 键 {uniq:,} 与登记值 {SNAP_KEYS_MEASURED:,} 不符 ⇒ "
            "快照被重排/部分重写，不能再当冻结行集用")
    return len(snap)


def build_daily_index() -> dict[str, list[Path]]:
    """裸码 → 日线文件清单（**含指数文件**——撞码就是在这里撞的，先过滤就查不回来）。"""
    out: dict[str, list[Path]] = {}
    for fp in sorted(Path(paths.daily_dir()).glob("*.parquet")):
        if ".bak" in fp.stem:
            continue
        code = fp.stem[2:] if fp.stem[:2] in ("sh", "sz", "bj") else fp.stem
        out.setdefault(code, []).append(fp)
    return out


def select_payload_cols(snap_columns, *, feature_union: list[str]) -> list[str]:
    """载荷列 = 元列 + 模型列并集，**去重**且保序，只留快照里真有的列。

    `prev_close` 同时是元列（按行反查来源要用）和 29 维之一——不去重时 `rows["prev_close"]`
    返回的是 DataFrame，报错信息会伪装成「来源不唯一」，而漂移比对会把它当成 30 维。
    """
    have = set(snap_columns)
    seen: set[str] = set()
    out: list[str] = []
    for c in (*KEY_COLS, OLD_LABEL, "prev_close", *feature_union):
        if c in seen or c not in have:
            continue
        seen.add(c)
        out.append(c)
    return out


def read_daily(fp: Path) -> pd.DataFrame:
    """读日线帧。

    ⚠ **不许往 `.attrs` 里塞东西**（这里曾经塞过日期→行号索引）：pandas 的
    `NDFrame.__finalize__` 会 `deepcopy(attrs)`，而 `features_5d` 每次调用要做几十次
    pandas 运算 ⇒ 一个 8,522 条的索引被复制几十次。单票 672 行 × 4 臂实测从 ~3s 变成
    54.7s（222s 里 210s 是 deepcopy）。索引请放调用方的 `positions` 字典里。
    """
    df = pd.read_parquet(fp).sort_values("date").reset_index(drop=True)
    df["_d10"] = df["date"].astype(str).str.slice(0, 10)
    return df


def date_positions(f: pd.DataFrame) -> dict[str, int]:
    """日期(ISO 前 10 位) → 行号。每只票每个文件算一次，缓存在任务局部字典里。"""
    return {d: i for i, d in enumerate(f["_d10"].tolist())}


def pick_source(code: str, cands: list[Path], frames: dict[Path, pd.DataFrame],
                positions: dict[Path, dict[str, int]], date: str,
                prev_close: float) -> tuple[Path, int]:
    """按**行**反查来源文件与行号：`prev_close` 是快照这一行的 T 日 raw 收盘。

    撞码的 5 只票里指数行与个股行同键不同价，靠它分得开。对不上或同时命中两家 ⇒
    `SnapshotContractError`：宁可停下，不要猜，更不要静默丢行。
    """
    tol = 1e-3 * max(1.0, abs(float(prev_close)))
    good: list[tuple[Path, int]] = []
    for fp in cands:
        f = frames.get(fp)
        if f is None:
            f = read_daily(fp)
            frames[fp] = f
            positions[fp] = date_positions(f)
        i = positions[fp].get(date)
        if i is None:
            continue
        if abs(float(f["close"].iat[i]) - float(prev_close)) <= tol:
            good.append((fp, i))
    if not good:
        raise SnapshotContractError(
            f"{code}@{date} 在任何候选日线文件里都对不上 prev_close={prev_close}"
            f"（候选 {[p.stem for p in cands]}）⇒ 这一行无法重算，不许静默丢行")
    if len(good) > 1:
        raise SnapshotContractError(
            f"{code}@{date} 的 prev_close={prev_close} 同时命中 "
            f"{[p.stem for p, _ in good]} ⇒ 来源不唯一，停下来人工判")
    return good[0]


# --------------------------------------------------------------------------- #
# 二、逐臂重算特征 + 打分（worker 侧；`_W` 由 `init_worker` 建，测试可直接注入）
# --------------------------------------------------------------------------- #
_W: dict[str, Any] = {}


def init_worker(model_paths: dict[str, Path], panel_path: Path | None = None,
                live_model: Path | None = None) -> None:
    """每个 worker 建一次：模型、按臂供给、日线索引。Python 3.14 默认 forkserver，
    不能指望从父进程继承这些对象——initializer 是唯一的建法（所以参数必须是位置参数，
    `ProcessPoolExecutor` 的 `initargs` 是位置元组）。"""
    arms = list(model_paths)
    _W["arms"] = arms
    _W["models"] = {a: lgb.Booster(model_file=str(model_paths[a])) for a in arms}
    _W["feat_cols"] = {a: list(_W["models"][a].feature_name()) for a in arms}
    _W["supplies"] = {a: sup.build_supply(a, panel_path=panel_path) for a in arms}
    _W["index"] = build_daily_index()
    _W["live"] = lgb.Booster(model_file=str(live_model)) if live_model else None
    _W["live_cols"] = list(_W["live"].feature_name()) if _W["live"] else []


def label_at(close: np.ndarray, i: int, limit_val: float) -> int:
    """与 `scripts/train_daban_model.py` 同一条标签判定：T+1 收盘涨幅 vs **T 日**阈值。"""
    if close[i] <= 0:
        return 0
    return int(is_limit_up_ret(close[i + 1] / close[i] - 1.0, limit_val))


def arm_feature_row(f: pd.DataFrame, code: str, i: int, st_flag: bool | None,
                    ctl_row: dict | None, *, limit_val: float,
                    ctl_limit: float | None) -> dict | None:
    """这一行这一臂的 29 维；供给与 ctl **完全同值**时复用 ctl 那份，否则整份重算。

    `compute_window_features_extended` 是 (帧, 代码, sig_idx, is_st) 的纯函数，而阈值
    由 (代码, 日期, is_st) 决定 ⇒ 只有当**阈值和 ST 布尔都**与 ctl 一样时，两份特征才
    可能逐值相同。两个条件都比，是为了让「只比一个」这类写错的版本当场被
    `tests/test_daban_stfix_arms.py::test_arm_row_reuse_is_exact_and_only_when_supply_matches`
    抓住（只比 is_st 会让某一臂悄悄吃到别的臂的 limit）。
    """
    if (ctl_row is not None and bool(st_flag) is False and ctl_limit is not None
            and abs(limit_val - ctl_limit) <= 1e-12):
        return ctl_row
    return compute_window_features_extended(f, code, sig_idx=i, is_st=st_flag)


def score_group(task: tuple[str, pd.DataFrame]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """一只票：按行取来源文件 → 逐臂算 (limit,is_st) → 逐臂**整份**重算 29 维 → 逐臂打分。

    每臂各调一次 `compute_window_features_extended`，不是「只换两列」：阈值渗进
    `limit_up_5d/limit_up_t1/is_limit_up_today/consecutive_boards/board_position`
    （`tdxfeed/features_5d.py:181,183,274`），叠列就是错的对照。
    """
    code, rows = task
    arms = _W["arms"]
    if "ctl" not in arms:
        raise SnapshotContractError(
            "载荷里没有 ctl 臂 ⇒ 行级披露（旧标签复现、指数行数）没有归属的那一臂，"
            "其余臂按定义就该与旧标签不同，不能替 ctl 记这个数")
    dup_cols = sorted({c for c in rows.columns.tolist()
                       if list(rows.columns).count(c) > 1})
    if dup_cols:
        raise SnapshotContractError(
            f"{code} 的载荷有重复列 {dup_cols} ⇒ `rows[col]` 会取出 DataFrame 而不是 Series，"
            "逐行来源反查与漂移比对都会错位。载荷必须走 `select_payload_cols`（去重保序）")
    cands = _W["index"].get(code, [])
    if not cands:
        raise SnapshotContractError(f"裸码 {code} 在日线目录里没有文件 ⇒ 无法重算")
    # 帧缓存的作用域是**这一只票**（一个任务最多 2 个候选文件）。曾经放在 worker 全局，
    # 5,211 只票跑下来每个 worker 会缓存几百个文件、连 `_pos` 字典一起留着，
    # 外层 12G 的 scope 会在最后几十只票上被顶掉——任务级缓存才是对的粒度。
    frames: dict[Path, pd.DataFrame] = {}
    positions: dict[Path, dict[str, int]] = {}
    dates = rows["date"].tolist()
    prevs = rows["prev_close"].astype(float).tolist()
    labs_old = rows[OLD_LABEL].astype(int).tolist()
    out: dict[str, Any] = {"date": dates, "code": code,
                           MERGED_OLD_LABEL: labs_old}
    # 行级披露只在 ctl 臂上记一次：其余臂**本该**与旧标签不同（那正是要量的翻牌），
    # 在它们身上累加「不符数」会把同一个形状记成 4 倍，读起来像缺陷比实际大。
    meta: dict[str, Any] = {"rows": len(rows), "label_mismatch_ctl": 0,
                            "drift_by_col": {}, "index_rows": 0,
                            "is_st_rows": {a: 0 for a in arms},
                            "tiers": {a: {} for a in arms},
                            "axis_mismatch_arm_rows": 0}
    close_cache: dict[Path, np.ndarray] = {}
    feat_ctl: list[list[float]] | None = None
    ctl_dicts: list[dict] | None = None          # ctl 那一遍的整份特征，供其余臂复用
    ctl_limits: list[float] | None = None
    n_reused = 0
    for arm in arms:
        supply = _W["supplies"][arm]
        cols = _W["feat_cols"][arm]
        feat_rows: list[list[float]] = []
        labels: list[int] = []
        src_stem: list[str] = []
        tiers: Counter[str] = Counter()
        arm_dicts: list[dict] = []        # 只在 ctl 臂上累积（供现役参考行复用）
        for j, (d, prev_close, lab_old) in enumerate(
                zip(dates, prevs, labs_old, strict=True)):
            fp, i = pick_source(code, cands, frames, positions, d, prev_close)
            f = frames[fp]
            got = None if supply is None else supply.is_st(code, d)
            st_flag = None if got is None else got[0]
            if got is not None:
                tiers[got[1]] += 1
            # 标签轴的自检：判定阈值必须与 dim1 同一个数，否则标签与特征分了两个轴
            axis = get_limit(code, on_date=d,
                             is_st=(False if st_flag is None else st_flag))
            feat = arm_feature_row(
                f, code, i, st_flag, ctl_dicts[j] if ctl_dicts is not None else None,
                limit_val=axis,
                ctl_limit=ctl_limits[j] if ctl_limits is not None else None)
            if feat is None:
                raise SnapshotContractError(
                    f"{code}@{d} 特征算不出（sig_idx={i}，帧长 {len(f)}）⇒ 快照行无法重算")
            if arm != "ctl" and ctl_dicts is not None and feat is ctl_dicts[j]:
                n_reused += 1
            if abs(axis - float(feat["limit"])) > 1e-12:
                meta["axis_mismatch_arm_rows"] += 1      # 按臂×行计，必须恒为 0
            cs = close_cache.get(fp)
            if cs is None:
                cs = f["close"].to_numpy(dtype=float)
                close_cache[fp] = cs
            lab = label_at(cs, i, axis)
            labels.append(lab)
            if arm == "ctl" and lab != lab_old:
                meta["label_mismatch_ctl"] += 1
            feat_rows.append([feat[c] for c in cols])
            if arm == "ctl":
                arm_dicts.append(feat)
            src_stem.append(fp.stem)
            if arm == "ctl" and fp.stem in _INDEX_STEMS:
                meta["index_rows"] += 1     # 这一行来自指数文件（撞码的另一侧）
            if st_flag:
                meta["is_st_rows"][arm] += 1
        X = pd.DataFrame(feat_rows, columns=cols)
        out[f"label_{arm}"] = labels
        out[f"score_{arm}"] = _W["models"][arm].predict(X.fillna(0)).tolist()
        meta["tiers"][arm] = dict(tiers)
        if arm == "ctl":      # 存值漂移只在 ctl（=旧口径）上有意义
            ctl_dicts = arm_dicts
            ctl_limits = [float(dd["limit"]) for dd in arm_dicts]
            out["src_stem"] = src_stem
            feat_ctl = feat_rows
            arr = np.asarray(feat_rows, dtype=float)
            sv = rows[cols].to_numpy(dtype=float)
            bad = np.abs(arr - sv) > 1e-4 * np.maximum(1.0, np.abs(sv))
            for j, c in enumerate(cols):
                n_bad = int(bad[:, j].sum())
                if n_bad:
                    meta["drift_by_col"][c] = n_bad
    meta["reused_rows"] = n_reused       # 复用次数：证明「四臂只差供给」没有退化成「四臂共用一份特征」
    if _W.get("live") is not None and feat_ctl is not None:
        # 现役模型只当**参考行**：喂它 ctl 的重算特征 ⇒ live−ctl 含「重训 + 数据漂移」
        # 两个成分，仍不是判据；判据永远是 臂 vs ctl。
        live_cols = _W["live_cols"]
        Xc = pd.DataFrame(feat_ctl, columns=_W["feat_cols"]["ctl"])
        need = [c for c in live_cols if c not in Xc.columns]
        if need:
            raise SnapshotContractError(
                f"现役模型要 {len(need)} 列不在重算特征里：{need[:8]} ⇒ 参考行算不出")
        out["score_live"] = _W["live"].predict(Xc[live_cols].fillna(0)).tolist()
    return pd.DataFrame(out), meta


def merge_group_results(parts: list[tuple[pd.DataFrame, dict[str, Any]]]) -> tuple[
        pd.DataFrame, dict[str, Any]]:
    """拼成一张对照表，并把披露项（漂移/撞码/标签复现）汇总出来。"""
    frame = pd.concat([p for p, _ in parts], ignore_index=True)
    agg: dict[str, Any] = {"rows": len(frame), "label_mismatch_ctl": 0,
                           "index_rows": 0, "axis_mismatch_arm_rows": 0,
                           "reused_rows": 0,
                           "drift_by_col": {}, "is_st_rows": {}, "tiers": {}}
    for _, m in parts:
        agg["label_mismatch_ctl"] += int(m["label_mismatch_ctl"])
        agg["index_rows"] += int(m["index_rows"])
        agg["axis_mismatch_arm_rows"] += int(m["axis_mismatch_arm_rows"])
        agg["reused_rows"] += int(m.get("reused_rows", 0))
        for c, n in m["drift_by_col"].items():
            agg["drift_by_col"][c] = agg["drift_by_col"].get(c, 0) + int(n)
        for a, n in m["is_st_rows"].items():
            agg["is_st_rows"][a] = agg["is_st_rows"].get(a, 0) + int(n)
        for a, t in m["tiers"].items():
            d = agg["tiers"].setdefault(a, {})
            for k, v in t.items():
                d[k] = d.get(k, 0) + int(v)
    return frame, agg


# --------------------------------------------------------------------------- #
# 三、逐日判据（Top-K 命中率 / 位移 / lift / AUC 参考）
# --------------------------------------------------------------------------- #
def per_day_top_table(frame: pd.DataFrame, *, k: int, label_col: str,
                      score_col: str) -> pd.DataFrame:
    """逐日截面按分排序取前 K，给每天的 hits/slots/hit_rate/base_rate/名单。

    与 `scripts/report_daban_topk.py::_topk_stats` 同口径（那条是池化写法，这条把
    每天摊开，好按天自助）。两者等值性由
    `tests/test_daban_stfix_arms.py::test_per_day_top5_matches_the_existing_report_protocol` 钉。
    """
    need = {*(KEY_COLS), label_col, score_col}
    missing = sorted(c for c in need if c not in frame.columns)
    if missing:
        raise ValueError(f"逐日 Top-K 缺列 {missing}")
    ordered = frame.sort_values(["date", score_col], ascending=[True, False],
                                kind="mergesort")
    pos = ordered.groupby("date", sort=False).cumcount()
    picks = ordered[pos.to_numpy() < k]
    if not len(picks):
        raise ValueError("逐日 Top-K 一行都没取到 ⇒ 分数列或日期列是空的")
    day = picks.groupby("date", sort=True).agg(
        hits=(label_col, "sum"), slots=(label_col, "size"))
    day["slots"] = np.minimum(day["slots"].to_numpy(), k)
    day["hit_rate"] = day["hits"].to_numpy() / day["slots"].to_numpy()
    sizes = frame.groupby("date", sort=True).size()
    bases = frame.groupby("date", sort=True)[label_col].mean()
    day["n_rows"] = sizes.reindex(day.index).to_numpy()
    day["base_rate"] = bases.reindex(day.index).to_numpy()
    day["top"] = [tuple(g["code"].tolist()) for _, g in picks.groupby("date", sort=True)]
    if (day["n_rows"] < k).any():
        bad = int((day["n_rows"] < k).sum())
        raise ValueError(
            f"{bad} 天的横截面不足 {k} 笔，slots 会缩档 ⇒ 逐日等权与池化不再等价，"
            "这一天该不该进分母要显式决定，不能让它悄悄摊薄")
    return day.reset_index()


def displacement_per_day(a: pd.DataFrame, b: pd.DataFrame) -> np.ndarray:
    """逐日「换了几个槽位 / K」——集合差，不是行差（与 11-05 位移数同定义）。"""
    if list(a["date"]) != list(b["date"]):
        raise ValueError("两张逐日表的日期不一致 ⇒ 不是一回事，不能算位移")
    k = int(a["slots"].iloc[0])
    out = []
    for ta, tb in zip(a["top"].tolist(), b["top"].tolist(), strict=True):
        out.append(len(set(ta) - set(tb)) / max(k, 1))
    return np.asarray(out, dtype=float)


def displacement(a: pd.DataFrame, b: pd.DataFrame) -> float:
    return float(displacement_per_day(a, b).mean())


def day_metric_frame(tables: dict[str, pd.DataFrame], metric: str) -> pd.DataFrame:
    """把每臂的逐日表压成「一天一行、每臂一列」——抽样单位就是这张表的行。"""
    out = pd.DataFrame({"date": tables[next(iter(tables))]["date"].tolist()})
    for arm, t in tables.items():
        if list(t["date"]) != out["date"].tolist():
            raise ValueError(f"臂 {arm} 的日期序列与基线不是一回事 ⇒ 不能配对自助")
        out[f"{metric}_{arm}"] = t[metric].to_numpy(dtype=float)
    return out


def _mean_metric(metric: str):
    def fn(sample: pd.DataFrame, arm: str) -> float:
        col = sample[f"{metric}_{arm}"].to_numpy(dtype=float)
        return float(col.mean()) if col.size else float("nan")
    return fn


def bootstrap_delta(tables: dict[str, pd.DataFrame], *, metric: str = METRIC,
                    baseline: str, n_boot: int, seed: int) -> dict[str, Any]:
    """按交易日整块自助的配对差，直接走 union47 那个引擎（不另写一套自助）。"""
    if baseline not in tables:
        raise ValueError(f"基线 {baseline!r} 不在臂里：{sorted(tables)}")
    frame = day_metric_frame(tables, metric)
    res = cluster_bootstrap_delta(frame, list(tables), baseline,
                                  {metric: _mean_metric(metric)},
                                  n_boot=n_boot, seed=seed)
    out: dict[str, Any] = {}
    for key, entry in res.items():
        boot = entry["boot"][metric]
        flat = dict(entry)
        flat.pop("boot")
        flat.pop("point_delta")
        out[key] = {"point_delta": float(entry["point_delta"][metric]),
                    "boot": boot, **flat,
                    **{k: boot[k] for k in ("mean_delta", "ci_lo", "ci_hi",
                                             "p_boot", "n_boot")},
                    "n_days": int(entry["n_days_resampled"])}
    return out


def attach_bh_q(res: dict[str, Any], *, alpha: float = 0.05) -> dict[str, Any]:
    """给每个「臂−基线」补 BH q；比较集 = 全部非基线臂（不是只挑显著的那几条）。

    返回**新字典**（不改入参）：报告里 `delta` 还要按原样再渲染一次，就地改写会让
    「q 是哪一批 p 校正出来的」这件事在内存里变得不可追。
    """
    from quant.multifactor.cv_utils import bh_fdr_correct

    keys = [k for k, v in res.items()
            if isinstance(v, dict) and "p_boot" in v]
    if not keys:
        raise ValueError("res 里没有任何带 p_boot 的条目 ⇒ q 值无从算起")
    ps = [float(res[k]["p_boot"]) for k in keys]
    _, qs = bh_fdr_correct(ps, alpha=alpha)
    out: dict[str, Any] = {}
    for k in res:
        out[k] = dict(res[k]) if isinstance(res[k], dict) else res[k]
    for k, q in zip(keys, qs.tolist(), strict=True):
        out[k]["q_value"] = float(q)
        out[k]["reject_h0"] = bool(q < alpha)
    out["comparison_set"] = len(keys)
    return out


def necessary_condition(entry: dict[str, Any]) -> bool:
    """切流的必要条件：Δ 点估计为正 **且** 95% CI 下界 > 0（不跳 0）。"""
    return bool(float(entry["point_delta"]) > 0 and float(entry["ci_lo"]) > 0)


def verdict_text(entry: dict[str, Any]) -> str:
    """裁定文案。CI 跳 0 只写「证据不足」——**不许**写成「证伪」（那是超额结论：
    本轮功效只够说量不出来，不够说效应不存在）。"""
    if necessary_condition(entry):
        return ("满足切流必要条件（Δ>0 且 95% CI 不跳 0）——但切不切流是用户的决定，"
                "本批不切")
    return "证据不足，不切（Δ 的 95% CI 跳 0 或方向不为正）"


def label_tables(snap: pd.DataFrame, *, unified_label_col: str,
                 old_label_col: str) -> dict[str, dict[str, Any]]:
    """两套标签各自的分母。主表用统一标签（A2），附表用旧标签；分母不同就必须都写出来。"""
    out: dict[str, dict[str, Any]] = {}
    for name, col in ((UNIFIED_KEY, unified_label_col), ("old", old_label_col)):
        if col not in snap.columns:
            raise ValueError(f"标签表缺列 {col!r}（{name}）")
        y = snap[col].to_numpy(dtype=float)
        out[name] = {"label_col": col, "n_rows": len(y),
                     "n_positive": int(y.sum()),
                     "base_rate": float(y.mean()) if len(y) else float("nan"),
                     "n_days": int(snap["date"].nunique())}
    return out


# --------------------------------------------------------------------------- #
# 四、装配与输出
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="B2 四臂对照评估（ST 档供给的净效应）")
    ap.add_argument("--snapshot", default=str(SNAPSHOT), help="冻结评估快照 parquet")
    ap.add_argument("--model", action="append", default=[],
                    help="臂=路径，可重复。四个臂都要给齐，缺臂直接拒跑（判据按三臂对照登记）")
    ap.add_argument("--live-model", default=str(LIVE_MODEL),
                    help="现役模型，**只当参考行**（含重训与数据漂移两个成分，不是判据）；"
                         "传空字符串就不算")
    ap.add_argument("--panel", default="", help="ST 事件面板路径（默认走 paths.st_event_path()）")
    ap.add_argument("--k", type=int, default=PROTOCOL["k"])
    ap.add_argument("--n-boot", type=int, default=PROTOCOL["n_boot"])
    ap.add_argument("--seed", type=int, default=PROTOCOL["seed"])
    ap.add_argument("--baseline", default=PROTOCOL["baseline"], choices=list(sup.ARMS))
    ap.add_argument("--workers", type=int, default=8,
                    help="进程数（每 worker 各建一次模型与面板；内存上限由外层 scope 管）")
    ap.add_argument("--out-md", default="")
    ap.add_argument("--out-json", default="")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做行集/列契约检查并打印，不重算不打分（用来先确认契约）")
    return ap


def _parse_models(items: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for item in items:
        arm, _, path = item.partition("=")
        if not path:
            raise SystemExit(f"--model 要写成 臂=路径，收到 {item!r}")
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            raise SystemExit(f"臂 {arm} 的模型不存在：{p}")
        out[arm] = p
    missing = [a for a in sup.ARMS if a not in out]
    if missing:
        raise SystemExit(
            f"缺臂 {missing} ⇒ 判据按「三臂 vs ctl」预登记，缺臂就凑不齐比较集，"
            "不许临时改成两臂")
    extra = [a for a in out if a not in sup.ARMS]
    if extra:
        raise SystemExit(f"未知臂 {extra} ⇒ 词表 = {list(sup.ARMS)}")
    return out


def evaluate_arms(frame: pd.DataFrame, *, arms: list[str], baseline: str, k: int,
                  n_boot: int, seed: int, unified_label_col: str,
                  old_label_col: str) -> dict[str, Any]:
    """两套标签各跑一遍主/次判据；返回可直接写报告的字典。"""
    tables = {name: label_tables(frame, unified_label_col=unified_label_col,
                                 old_label_col=old_label_col)[name]
              for name in (UNIFIED_KEY, "old")}
    out: dict[str, Any] = {"denominators": tables, "label_sets": {}}
    for label_name, label_col in ((UNIFIED_KEY, unified_label_col),
                                  ("old", old_label_col)):
        if label_col not in frame.columns:
            raise ValueError(f"对照表缺标签列 {label_col!r}（{label_name}）")
        per = {a: per_day_top_table(frame, k=k, label_col=label_col,
                                    score_col=f"score_{a}") for a in arms}
        boot = attach_bh_q(bootstrap_delta(per, metric=METRIC, baseline=baseline,
                                           n_boot=n_boot, seed=seed))
        base_rate = float(tables[label_name]["base_rate"])
        rows: dict[str, Any] = {"per_arm": {}, "delta": boot}
        ref = per[baseline]
        for a in arms:
            t = per[a]
            pooled_hits = float(t["hits"].sum())
            pooled_slots = float(t["slots"].sum())
            prec = pooled_hits / pooled_slots if pooled_slots else float("nan")
            sub = frame[["date", "code", label_col, f"score_{a}"]].rename(
                columns={f"score_{a}": "score"})
            rows["per_arm"][a] = {
                "hit_rate_day_mean": float(t[METRIC].mean()),
                "precision_pooled": prec,
                "lift": prec / base_rate if base_rate > 0 else float("nan"),
                "hit_days_share": float((t["hits"] > 0).mean()),
                "n_days": len(t),
                "displacement_vs_baseline": displacement(ref, t),
                "auc": float(_auc(sub[label_col].to_numpy(dtype=float),
                                  sub["score"].to_numpy(dtype=float))),
                "mean_daily_base_rate": float(t["base_rate"].mean()),
            }
        out["label_sets"][label_name] = rows
    out["comparison_set"] = boot.get("comparison_set")
    return out



def pct_or_na(x: float, *, signed: bool = False) -> str:
    """算不出就说算不出：0 分母时给「不可用 + 原因」，不给 nan% 让人读成 0%。"""
    import math

    if x is None or not math.isfinite(x):
        return "不可用：旧正样本为 0，变化率没有分母"
    return f"{x:+.2%}" if signed else f"{x:.2%}"


def x_or_na(x: float) -> str:
    import math

    return "不可用：基准率 0" if x is None or not math.isfinite(x) else f"{x:.2f}x"


def f4_or_na(x: float) -> str:
    """AUC 之类在单类样本上算不出——写「不可用」而不是 nan（nan 会被读成 0）。"""
    import math

    return "不可用" if x is None or not math.isfinite(x) else f"{x:.4f}"


def write_report_md(res: dict[str, Any], path: Path) -> None:
    p = res["protocol"]
    e = res.get("protocol_effective", p)
    lines = [f"# daban ST 档四臂对照（{datetime.now():%Y-%m-%d %H:%M}）", "",
             "## 协议（预登记）", "",
             f"- 主判据 `{p['primary']}`，抽样单位={p['sampling_unit']}，基线={p['baseline']}",
             f"- **实跑参数**：K={e['k']}，n_boot={e['n_boot']}，seed={e['seed']}，"
             f"baseline={e['baseline']}"
             + ("" if e == p else "　⚠ 与预登记不一致，本轮结论只对实跑参数负责"),
             f"- 预登记协议：{p}",
             f"- 行集：`{res['snapshot']}` 共 {res['n_rows']:,} 行 / {res['n_days']} 个交易日",
             f"- 统一标签列：`{res['unified_label_col']}`（主表）；附表用重算表里的 `{MERGED_OLD_LABEL}`（=快照的 `{OLD_LABEL}`）",
             f"- 切流必要条件：Δ vs {p['baseline']} 的 95% CI 下界 > 0", "",
             "## 标签分母", ""]
    for name, t in res["denominators"].items():
        lines.append(f"- {name}：`{t['label_col']}` 正样本 {t['n_positive']:,} / "
                     f"{t['n_rows']:,} 行 = 基准率 {t['base_rate']:.4%}，"
                     f"{t['n_days']} 个交易日")
    lines += ["", "## 主表（统一标签）", "",
              "| 臂 | 逐日 Top-5 命中率 | Δ vs ctl | CI95 | p | q | 位移 | lift | AUC(参考) |",
              "|---|---:|---:|---|---:|---:|---:|---:|---:|"]
    for label_name in (UNIFIED_KEY, "old"):
        blk = res["label_sets"][label_name]
        if label_name == "old":
            lines += ["", "## 附表（旧标签）", "",
                      "| 臂 | 逐日 Top-5 命中率 | Δ vs ctl | CI95 | p | q | 位移 | lift | AUC(参考) |",
                      "|---|---:|---:|---|---:|---:|---:|---:|---:|"]
        for arm, s in blk["per_arm"].items():
            key = f"{arm}_minus_{p['baseline']}"
            d = blk["delta"].get(key)
            delta = "—（基线）" if d is None else f"{d['point_delta']:+.4%}"
            ci = "—" if d is None else f"[{d['ci_lo']:+.4%}, {d['ci_hi']:+.4%}]"
            pv = "—" if d is None else f"{d['p_boot']:.4f}"
            qv = "—" if d is None else f"{d['q_value']:.4f}"
            lines.append(f"| {arm} | {s['hit_rate_day_mean']:.4%} | {delta} | {ci} | "
                         f"{pv} | {qv} | {s['displacement_vs_baseline']:.1%} | "
                         f"{x_or_na(s['lift'])} | {f4_or_na(s['auc'])} |")
    lines += ["", "## 裁定", ""]
    for label_name in (UNIFIED_KEY, "old"):
        blk = res["label_sets"][label_name]
        for arm, s in blk["per_arm"].items():
            if arm == p["baseline"]:
                continue
            d = blk["delta"][f"{arm}_minus_{p['baseline']}"]
            lines.append(f"- **{arm} vs {p['baseline']}（{label_name}）**："
                         f"{verdict_text(d)}；Δ={d['point_delta']:+.4%}，"
                         f"q={d['q_value']:.4f}，位移 {s['displacement_vs_baseline']:.1%}")
    lines += ["", "## 统一标签相对旧标签翻了几行（OOS 窗口口径）", "",
              f"- 窗口：{res['labels_vs_old']['window']}，{res['labels_vs_old']['n_rows']:,} 行",
              f"- 旧正样本 {res['labels_vs_old']['old_positive']:,} → "
              f"统一正样本 {res['labels_vs_old']['unified_positive']:,}"
              f"（{pct_or_na(res['labels_vs_old']['positive_change_pct'], signed=True)}）",
              f"- 0→1 {res['labels_vs_old']['flip_0_to_1']:,} 行 / "
              f"1→0 {res['labels_vs_old']['flip_1_to_0']:,} 行",
              f"- 按年：{res['labels_vs_old']['by_year']}",
              "", "## 供给分布（每臂真正给了多少行 ST）", ""]
    for arm, d in res.get("supply_distribution", {}).items():
        lines.append(f"- {arm}: is_st=1 的样本行 {d['is_st_rows']:,}，"
                     f"档位分布 {d['tiers'] or '（未供给档，不记档）'}")
    lines += ["", "## 披露项（不是判据，但必须让人看见）", "",
              f"- 撞码指数行：{res['agg']['index_rows']:,} 行（按行反查来源，未过滤；"
              "只在 ctl 臂上数一次）",
              f"- 旧标签复现不符（只在 ctl 臂上查，行级）：{res['agg']['label_mismatch_ctl']:,} 行 / "
              f"{res['n_rows']:,} 行",
              f"- 标签轴与 dim1 不一致（按臂×行计）：{res['agg']['axis_mismatch_arm_rows']:,} "
              "（必须为 0）",
              f"- 跨臂复用特征行数：{res['agg'].get('reused_rows', 0):,}（非 ctl 臂里供给值"
              "与 ctl 逐值相同、因此没有重算的行；0 就意味着四臂真的每行都算了四遍）",
              f"- 重算 ctl 特征与快照存值逐列差：{res['agg']['drift_by_col']}",
              "- 现役模型参考行（含重训与数据漂移两个成分，**不是判据**）："]
    ref = res.get("live_ref")
    lines.append(f"  - {ref}" if ref else "  - （未给 --live-model）")
    lines += ["", "_生成：`scripts/compare_daban_stfix_arms.py`；"
                  "协议原文见文件头与 `tests/test_daban_stfix_arms.py`。_"]
    atomic_write_text(path, "\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    models = _parse_models(args.model)
    arms = list(sup.ARMS)
    if args.baseline not in arms:
        raise SystemExit(f"--baseline {args.baseline} 不在臂词表里")
    panel = Path(args.panel) if args.panel else None

    print(f"[快照] 读 {args.snapshot}")
    snap = pd.read_parquet(Path(args.snapshot))
    feat_union = sorted({c for a in arms
                         for c in lgb.Booster(model_file=str(models[a])).feature_name()})
    check_snapshot_contract(snap, feature_cols=feat_union,
                            n_rows_expected=(SNAP_ROWS_MEASURED
                                             if Path(args.snapshot) == SNAPSHOT
                                             else None))
    n_rows = len(snap)
    if args.dry_run:
        print(f"[dry-run] 行集/列契约通过：{n_rows:,} 行 / {snap['date'].nunique()} 天 / "
              f"模型列并集 {len(feat_union)} 列")
        return 0

    payload_cols = select_payload_cols(snap.columns.tolist(), feature_union=feat_union)
    tasks = [(str(code), grp[payload_cols].reset_index(drop=True))
             for code, grp in snap.groupby("code", sort=True)]
    live = Path(args.live_model) if args.live_model else None
    print(f"[重算] {len(tasks):,} 个裸码 × {n_rows:,} 行 × {len(arms)} 臂，"
          f"workers={args.workers}")
    parts: list[tuple[pd.DataFrame, dict[str, Any]]] = []
    with ProcessPoolExecutor(max_workers=args.workers, initializer=init_worker,
                             initargs=(models, panel, live)) as ex:
        for i, (frame_part, meta) in enumerate(ex.map(score_group, tasks,
                                                      chunksize=16), 1):
            parts.append((frame_part, meta))
            if i % 500 == 0:
                print(f"  {i}/{len(tasks)} 只票 …")

    frame, agg = merge_group_results(parts)
    if len(frame) != n_rows:
        raise SnapshotContractError(
            f"重算后 {len(frame):,} 行 != 快照 {n_rows:,} 行 ⇒ 行集被动过，四臂不可比")

    res: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": str(args.snapshot), "n_rows": int(n_rows),
        "n_days": int(frame["date"].nunique()),
        "protocol": dict(PROTOCOL),
        "protocol_effective": {"primary": PROTOCOL["primary"], "k": int(args.k),
                               "n_boot": int(args.n_boot), "seed": int(args.seed),
                               "baseline": args.baseline,
                               "sampling_unit": PROTOCOL["sampling_unit"],
                               "bh": PROTOCOL["bh"]},
        "arms": arms, "baseline": args.baseline,
        "unified_label_col": PRIMARY_LABEL_SOURCE,
        "agg": agg,
    }
    res.update(evaluate_arms(frame, arms=arms, baseline=args.baseline, k=args.k,
                             n_boot=args.n_boot, seed=args.seed,
                             unified_label_col=PRIMARY_LABEL_SOURCE,
                             old_label_col=MERGED_OLD_LABEL))
    res["labels_vs_old"] = label_flip_summary(frame)
    res["supply_distribution"] = {
        a: {"is_st_rows": agg["is_st_rows"].get(a, 0), "tiers": agg["tiers"].get(a, {})}
        for a in arms}
    for line in main_verdict_lines(res):
        print(line)
    if "score_live" in frame.columns:
        res["live_ref"] = live_reference(
            frame, k=args.k, unified_label_col=PRIMARY_LABEL_SOURCE,
            old_label_col=MERGED_OLD_LABEL, baseline=args.baseline)
        print(f"[参考行] {res['live_ref']}")
    if args.out_md:
        write_report_md(res, Path(args.out_md))
        print(f"[报告] {args.out_md}")
    if args.out_json:
        atomic_write_text(Path(args.out_json),
                          json.dumps(res, ensure_ascii=False, indent=1, default=float),
                          encoding="utf-8")
        print(f"[数据] {args.out_json}")
    return 0


def main_verdict_lines(res: dict[str, Any]) -> list[str]:
    """把主表每条「臂−基线」打成一行可读判定（比较集与 q 一起出来）。"""
    p = res["protocol"]
    blk = res["label_sets"][UNIFIED_KEY]
    lines = [f"[比较集] 非基线臂 {blk['delta']['comparison_set']} 条，"
             f"BH q 按全族算（不挑最小 p 的那一条）"]
    for key, d in blk["delta"].items():
        if key == "comparison_set" or not isinstance(d, dict):
            continue
        arm = key.split("_minus_")[0]
        lines.append(
            f"[主判据] {arm} vs {p['baseline']} Δ={d['point_delta']:+.4%} "
            f"CI[{d['ci_lo']:+.4%}, {d['ci_hi']:+.4%}] p={d['p_boot']:.4f} "
            f"q={d['q_value']:.4f} ⇒ {verdict_text(d)}")
    return lines


def label_flip_summary(frame: pd.DataFrame) -> dict[str, Any]:
    """统一标签（A2）相对旧标签翻了几行、集中在哪一年——OOS 窗口口径，不是全历史。

    必须与 `docs/research/2026-11-05-daban-st-tier-label-pollution.md` 的全历史读数
    （52,919 行 / +24.42%）分开读：这里的分母只有快照那 336 个交易日。
    """
    if PRIMARY_LABEL_SOURCE not in frame.columns:
        raise ValueError(f"缺 {PRIMARY_LABEL_SOURCE}：无法核对统一标签")
    if MERGED_OLD_LABEL not in frame.columns:
        raise ValueError(f"缺 {MERGED_OLD_LABEL}：无法核对旧标签")
    old = frame[MERGED_OLD_LABEL].to_numpy(dtype=int)
    new = frame[PRIMARY_LABEL_SOURCE].to_numpy(dtype=int)
    flips = int((new != old).sum())
    flip = pd.Series(new != old)
    years = frame["date"].astype(str).str.slice(0, 4).to_numpy()
    by_year = {str(k): int(v) for k, v in flip.groupby(years).sum().items()}
    return {"n_rows": len(frame), "old_positive": int(old.sum()),
            "unified_positive": int(new.sum()),
            "flip_0_to_1": int(((new == 1) & (old == 0)).sum()),
            "flip_1_to_0": int(((new == 0) & (old == 1)).sum()),
            "flips_total": flips,
            "positive_change_pct": (float((new.sum() - old.sum()) / old.sum())
                                    if old.sum() else float("nan")),
            "by_year": by_year,
            "window": f"{frame['date'].min()} ~ {frame['date'].max()}"}


def live_reference(frame: pd.DataFrame, *, k: int, unified_label_col: str,
                   old_label_col: str, baseline: str) -> dict[str, Any]:
    """现役模型喂 ctl 重算特征：只给水平数，不进 BH、不进裁定。

    它回答的是「这次重训 + 数据漂移把数字挪了多少」，与「ST 档供给值不值」是两件事；
    把它写进报告是因为不写就会被后来人当成对照读。
    """
    out: dict[str, Any] = {}
    for name, col in ((UNIFIED_KEY, unified_label_col), ("old", old_label_col)):
        t = per_day_top_table(frame, k=k, label_col=col, score_col="score_live")
        b = per_day_top_table(frame, k=k, label_col=col, score_col=f"score_{baseline}")
        base_rate = float(frame[col].mean())
        prec = float(t["hits"].sum()) / float(t["slots"].sum())
        out[name] = {"hit_rate_day_mean": float(t[METRIC].mean()),
                     "lift": prec / base_rate if base_rate > 0 else float("nan"),
                     "displacement_vs_baseline": displacement(b, t)}
    return out


if __name__ == "__main__":
    raise SystemExit(main())
