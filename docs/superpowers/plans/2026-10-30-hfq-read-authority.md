# hfq 读侧权威：数据回填、读侧咽喉与双跑重训实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让价格特征与收益率标签读 `data/hfq_factor`（在役后复权因子），涨跌停/封板/委托价继续读 raw；用同一样本集上的双跑把这次口径切换量出来。

**Architecture:** 四层收敛——① `scripts/backfill_hfq_history.py` 把因子表补到上市日（三道对账门禁）；② `tdxfeed/adjusted.py` 成为唯一的复权读侧咽喉（raw 原样、hfq 逐日乘因子、strict 四条门禁含 raw_close 错配）；③ `features_5d` / `factors_polars` 按维度分派（13 hfq / 8 raw / 4 量纲不变 / 4 枚举），标签 `hypothetical_pnl` 切 hfq、daban 次日涨停标签留 raw；④ daban 与 stage1 各出 raw/hfq 两份产物，现役模型与 `DEFAULT_MODEL` 不动。

**Tech Stack:** Python 3.14（`venv/bin/python`）、pandas / polars / pyarrow、LightGBM、baostock、pytest。

**Spec:** `docs/superpowers/specs/2026-10-30-hfq-read-authority-design.md`

## Global Constraints

- Python 一律 `venv/bin/python` / `venv/bin/pytest`；系统 `python3` 没装本仓库依赖。
- **共享工作树**：`git add` / `git commit` 必须带显式路径，不用 `-A` / `.`；陌生脏文件与红测试先判断是否他人会话的改动，不顺手修、不顺手提交。
- `data/`、`logs/`、`models/` 在 gitignore 里：就地改写前先做带日期备份（`cp -a`），git 救不回来。
- 不提交 `.env*` 或任何凭据；不真发手机推送、不真推远端来「证明」通道。
- 不改 `tdxfeed/execution/`（成交/结算/涨跌停语义一字不动）；不改 `data/adj_factor`；不改 `DEFAULT_MODEL` / `FALLBACK_MODEL` / `POSITION_MODE_MODEL`；不建派生价格落盘表；不复活 `adj_factor_ext`。
- 因子表**只有一张**：`data/hfq_factor`。Task 8 名单里的 8 个价格派生模块不得出现 `adj_factor` / `hfq_factor` 字面量（守卫 Task 8）；名单外的既有豁免是 `scripts/predict.py` 的 `qfq_close` 展示字段与 `scripts/find_double_up_c_v2.py` 的翻倍筛选——这两处本轮不动，但也不许新增别的读点。
- 回填只在 **18:00 之后**跑（避开 16:30 `tdx-sync.service` 的第三条 `ExecStartPost=-`），并发 **3 个进程**。
- 测试一律不联网：baostock 用假客户端注入，HTTP 端点用自起自停的 fixture。
- 提交信息用中文、conventional 前缀；每个 Task 的最后一步是一次显式路径提交（Task 9/10 的产物在 `data/`，不产生提交）。

**五条落地裁决**（与 spec 正文的细微差异，执行时照这里）：

1. **spec §2.2 门禁 3 用不变量而不是拒绝分支实现**：`combine()` 保留旧表全部行，合并不可能缩表；写成 `assert len(merged) >= len(old)`，不做不可达的 `if reject`。测试锁「新段比旧表短时旧行全保留」。
2. **spec §4.4 的 `find_d_signal_5d.py` / `feature_engineering.py` 不加 `--price-basis`**：它们生产 `d_signal_5d_features_v9.csv`，而该文件已不存在（`scripts/recompute_pnl_labels.py:4-6` 有记录），是退役生产者；只在 Task 8 的扫描名单里保留，防止它们私读因子表。
3. **咽喉加第四条 strict 门禁：因子表 `raw_close` 与日线 `close` 的一致性（`FACTOR_RAW_TOL=0.01`，中位相对偏差）**。原因：`hfq_factor_path` 只认裸 6 位，`sh000001` 会读到 `000001.parquet`（平安银行）——实测重叠 1037 行 `raw_close` 中位偏差 **99.65%**，正常个股 **0.0**；同码双文件共 5 个（`000001/000016/000688/000852/000905`）。已同步进 spec §3.3 的四条门禁表；不对上的票按「缺因子」处理，绝不乘别只票的因子。
4. **Task 2 白名单只断言 `date >= 2020-01-01`**：回填会把 2020 年前的历史接进来，那一段没有基线可比，只能在 Task 9 的审计 JSON 里**记录不断言**；拿新段去套旧白名单会把正常的历史除权误报成源漂移。
5. **daban 双跑的样本对齐按 symbol（不是裸 6 位）**：`train_daban_model` 的日线文件带市场前缀，排除集也必须用 `sh600000` 这种写法，否则 `000001` 会把 `sh000001` 指数一起排掉。`--exclude-symbols-file` 写 JSON 的 `symbols` 字段，理由同 `hfq_factor_path` 的 6 位冲突。

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `scripts/backfill_hfq_history.py` | 新建 | 全历史回填：拉源、三道门禁、备份、原子写、单状态文件、熔断 |
| `tests/test_hfq_backfill_gate.py` | 新建 | 门禁与幂等的离线测试（假 baostock，零网络） |
| `tests/test_hfq_factor_monotone.py` | 新建 | 全表下跳事件白名单（11 只 / 12 次），源漂移绊线 |
| `tdxfeed/adjusted.py` | 新建 | 复权读侧咽喉：`frame(symbol, kind, strict)` |
| `tests/test_adjusted_frame.py` | 新建 | raw 逐值同盘、hfq=close×因子、四条 strict 门禁、raw_close 错配不乘、不读 adj |
| `tdxfeed/features_5d.py` | 改 | 两个特征函数加 `frame_hfq` 关键字并逐维分派 |
| `tdxfeed/factors_polars.py` | 改 | polars 复刻同步分派（与 pandas 逐值一致） |
| `tests/test_features_5d_price_basis.py` | 新建 | 合成除权消融：13 维不变、8 维随 raw、双引擎一致 |
| `scripts/train_pnl_proxy.py` | 改 | `hypothetical_pnl` 支持 `price_basis` |
| `scripts/recompute_pnl_labels.py` | 改 | `--price-basis` / `--src` / `--out`，产物分离 |
| `tests/test_label_price_basis.py` | 新建 | 同一除权窗口：raw 标签假止损、hfq 标签连续 |
| `tdxfeed/stage1_wide_table.py` | 改 | `--price-basis`，hfq 宽表用 hfq 特征 + hfq 标签 |
| `tests/test_stage1_wide_table_basis.py` | 新建 | 宽表按口径取特征与标签，缺失因子跳过 |
| `scripts/predict.py` | 改 | 注册表二元组→三元组（带 price_basis），2 条 hfq 条目 |
| `tests/test_predict_price_basis.py` | 新建 | 10 条现有条目零变化；hfq 缺因子走告警不 raise |
| `tests/test_single_factor_authority.py` | 新建 | 八个模块不得出现因子表字面量（AST 扫描） |
| `scripts/train_daban_model.py` | 改 | `--price-basis` / `--exclude-symbols-file` / `--hfq-skipped-out` |
| `docs/research/2026-10-30-hfq-read-authority-dual-run.md` | 新建 | 双跑报告（非口径漂移 / 口径效应两个差） |
| `AGENTS.md` | 改 | 数据口径段补一行 hfq 读侧权威 |

---

### Task 1: hfq 全历史回填器（门禁内核 + 运行器）

**Files:**
- Create: `scripts/backfill_hfq_history.py`
- Test: `tests/test_hfq_backfill_gate.py`

**Interfaces:**
- Consumes: `tdxfeed.hfq_factor.COLUMNS/FAIL_ABORT/bs_code/merge_rows/write_atomic/_query_retry/exit_code`、`tdxfeed.backfill_history.acquire_run_lock`、`tdxfeed.paths.hfq_factor_dir`
- Produces: `decide(old, new) -> (action, detail)`、`combine(old, new) -> DataFrame`、`backfill_one(bs, code, out_dir, today, backup_dir) -> str`、`plan_codes(out_dir, state, *, symbols, limit, force) -> list[str]`；状态文件 `data/.state/hfq_backfill.json`（`done / rejected / not_in_source / rebased`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_hfq_backfill_gate.py`：

```python
"""回填器的三道门禁与幂等：零网络，baostock 用假客户端注入。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_hfq_history as bh  # noqa: E402


class _RS:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0

    def next(self):
        if self._i >= len(self._rows):
            return False
        self._i += 1
        return True

    def get_row_data(self):
        return self._rows[self._i - 1]


class FakeBS:
    """只实现回填器用到的两个调用：历史查询与代码是否存在。"""

    def __init__(self, raw, hfq, in_source=True):
        self.rows = {"1": hfq, "3": raw}
        self.in_source = in_source

    def query_history_k_data_plus(self, code, fields, start_date, end_date,
                                  frequency, adjustflag):
        return _RS(self.rows[adjustflag])

    def query_stock_basic(self, code=""):
        return _RS([["sh.600000"]] if self.in_source else [])


def _old_frame(dates, raw, hfq):
    return pd.DataFrame({
        "code": ["600000"] * len(dates),
        "date": [pd.Timestamp(d).date() for d in dates],
        "hfq_factor": [h / r for h, r in zip(hfq, raw, strict=True)],
        "raw_close": raw,
        "hfq_close": hfq,
    })


def _write(tmp_path, dates, raw, hfq):
    out = tmp_path / "hfq"
    out.mkdir(exist_ok=True)
    _old_frame(dates, raw, hfq).to_parquet(out / "600000.parquet", index=False)
    return out


def test_raw_overlap_error_rejects_with_zero_writes(tmp_path):
    out = _write(tmp_path, ["2020-01-02", "2020-01-03"], [10.0, 10.0], [10.0, 10.0])
    before = (out / "600000.parquet").read_bytes()
    bs = FakeBS(raw=[["2020-01-02", "10.0"], ["2020-01-03", "10.2"]],
                hfq=[["2020-01-02", "10.0"], ["2020-01-03", "10.2"]])
    r = bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert r.startswith("reject:raw-overlap-err"), r
    assert (out / "600000.parquet").read_bytes() == before
    assert not (tmp_path / "bak" / "600000.parquet").exists()


def test_hfq_overlap_error_rebases_and_takes_new_segment(tmp_path):
    out = _write(tmp_path, ["2020-01-02", "2020-01-03"], [10.0, 10.0], [10.0, 10.0])
    bs = FakeBS(raw=[["2020-01-02", "10.0"], ["2020-01-03", "10.0"]],
                hfq=[["2020-01-02", "10.0"], ["2020-01-03", "10.5"]])
    r = bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert r.startswith("rebase:"), r
    df = pd.read_parquet(out / "600000.parquet")
    assert float(df["hfq_close"].iloc[-1]) == 10.5
    assert (tmp_path / "bak" / "600000.parquet").exists()


def test_shorter_source_segment_never_drops_rows(tmp_path):
    out = _write(tmp_path, ["2020-01-02", "2020-01-03", "2020-01-06"],
                 [10.0, 10.0, 10.0], [10.0, 10.0, 10.0])
    bs = FakeBS(raw=[["2020-01-06", "10.0"]], hfq=[["2020-01-06", "10.0"]])
    r = bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert r.startswith("ok:"), r
    assert len(pd.read_parquet(out / "600000.parquet")) == 3


def test_empty_source_is_classified_by_basic_info(tmp_path):
    out = _write(tmp_path, ["2020-01-02"], [10.0], [10.0])
    assert bh.backfill_one(FakeBS([], [], in_source=False), "600000", out,
                           "2026-10-30", tmp_path / "bak") == "not-in-source"
    assert bh.backfill_one(FakeBS([], [], in_source=True), "600000", out,
                           "2026-10-30", tmp_path / "bak") == "fail:empty-source"


def test_written_date_column_is_date32(tmp_path):
    out = _write(tmp_path, ["2020-01-02"], [10.0], [10.0])
    bs = FakeBS(raw=[["2020-01-02", "10.0"], ["2020-01-03", "10.0"]],
                hfq=[["2020-01-02", "10.0"], ["2020-01-03", "10.0"]])
    bh.backfill_one(bs, "600000", out, "2026-10-30", tmp_path / "bak")
    assert str(pq.read_schema(out / "600000.parquet").field("date").type) == "date32[day]"


def test_plan_codes_skips_done_and_not_in_source(tmp_path):
    out = _write(tmp_path, ["2020-01-02"], [10.0], [10.0])
    state = {"done": ["600000"], "rejected": {}, "not_in_source": ["600001"], "rebased": {}}
    assert bh.plan_codes(out, state) == []


def test_state_roundtrip_keeps_four_fields(tmp_path):
    p = tmp_path / "state.json"
    st = {"done": [], "rejected": {}, "not_in_source": [], "rebased": {}}
    st["done"].append("600000")
    bh.save_state(st, p)
    assert json.loads(p.read_text(encoding="utf-8"))["done"] == ["600000"]
    assert set(bh.load_state(p)) == {"done", "rejected", "not_in_source", "rebased"}


def test_mark_state_is_idempotent():
    st = {"done": [], "rejected": {}, "not_in_source": [], "rebased": {}}
    bh._mark(st, "done", "600000")
    bh._mark(st, "done", "600000")
    bh._mark(st, "rejected", "600001", "raw-overlap-err=1.0000%")
    assert st["done"] == ["600000"]
    assert st["rejected"] == {"600001": "raw-overlap-err=1.0000%"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_hfq_backfill_gate.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'backfill_hfq_history'`）

- [ ] **Step 3: 实现回填器**

创建 `scripts/backfill_hfq_history.py`：

```python
#!/usr/bin/env python
"""把 data/hfq_factor 回填到上市日：baostock 全历史（adjustflag=1 / 3）。

    venv/bin/python scripts/backfill_hfq_history.py --symbols 600000,000001 --allow-anytime
    venv/bin/python scripts/backfill_hfq_history.py --workers 3     # 18:00 后全量

三道门禁：
  1 重叠区 raw_close 最大相对误差 > 0.5% → 整只 reject，零写入（raw 是交易所价格权威）
  2 重叠区 hfq_close 最大相对误差 > 0.5% → 采用新段并记 rebased（因子可在同源内重算）
  3 合并不缩表：旧表行全保留，行数只增不减

并发用进程而不是线程：baostock 的连接挂在模块级 context 单例上
（baostock/util/socketutil.py 的 SocketUtil），线程会交错收发同一条 socket。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdxfeed.backfill_history import acquire_run_lock  # noqa: E402
from tdxfeed.hfq_factor import (  # noqa: E402
    COLUMNS, FAIL_ABORT, _query_retry, bs_code, exit_code, merge_rows, write_atomic)
from tdxfeed.paths import hfq_factor_dir  # noqa: E402

GATE_TOL = 0.005
START = "1990-01-01"
STATE_PATH = ROOT / "data" / ".state" / "hfq_backfill.json"
BACKUP_DIR = ROOT / "data" / "hfq_factor.bak-20261030"


def max_rel_err(old: pd.Series, new: pd.Series) -> float:
    """逐行相对误差的绝对最大值；任一侧非有限值的行不参与比较。"""
    a = pd.to_numeric(old, errors="coerce").to_numpy(dtype=float)
    b = pd.to_numeric(new, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(a) & np.isfinite(b)
    if not mask.any():
        return float("inf")
    return float(np.max(np.abs(b[mask] - a[mask]) / np.maximum(np.abs(a[mask]), 1e-9)))


def decide(old: pd.DataFrame, new: pd.DataFrame) -> tuple[str, str]:
    """返回 (action, detail)，action ∈ {ok, rebase, reject}。"""
    if old.empty:
        return "ok", "bootstrap"
    ov = old[["date", "raw_close", "hfq_close"]].merge(
        new[["date", "raw_close", "hfq_close"]], on="date", suffixes=("_old", "_new"))
    if ov.empty:
        return "reject", "no-overlap"
    raw_err = max_rel_err(ov["raw_close_old"], ov["raw_close_new"])
    if raw_err > GATE_TOL:
        return "reject", f"raw-overlap-err={raw_err:.4%}"
    hfq_err = max_rel_err(ov["hfq_close_old"], ov["hfq_close_new"])
    if hfq_err > GATE_TOL:
        return "rebase", f"hfq-overlap-err={hfq_err:.4%}"
    return "ok", f"raw-err={raw_err:.4%} hfq-err={hfq_err:.4%}"


def combine(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """旧表全保留、重叠日用新值；行数只增不减是这里的不变量。"""
    merged = new[COLUMNS].copy() if old.empty else pd.concat(
        [old[COLUMNS], new[COLUMNS]], ignore_index=True)
    merged["date"] = pd.to_datetime(merged["date"]).dt.date
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    merged = merged.reset_index(drop=True)[COLUMNS]
    assert len(merged) >= len(old), "combine 丢了旧行"
    return merged


def _backup_once(path: Path, backup_dir: Path) -> None:
    if not path.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / path.name
    if not dst.exists():          # 续跑不覆盖首份备份：那才是回填前的原样
        shutil.copy2(path, dst)


def load_state(path: Path = STATE_PATH) -> dict:
    if not Path(path).exists():
        return {"done": [], "rejected": {}, "not_in_source": [], "rebased": {}}
    with open(path, encoding="utf-8") as fh:
        st = json.load(fh)
    for k, v in (("done", []), ("rejected", {}), ("not_in_source", []), ("rebased", {})):
        st.setdefault(k, v)
    return st


def save_state(st: dict, path: Path = STATE_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def plan_codes(out_dir: Path, state: dict, *, symbols: str = "", limit: int = 0,
               force: bool = False) -> list[str]:
    if symbols:
        codes = [s.strip()[-6:] for s in symbols.split(",") if s.strip()]
    else:
        codes = [p.stem for p in sorted(Path(out_dir).glob("*.parquet"))
                 if len(p.stem) == 6 and p.stem.isdigit()]
    done = set(state["done"]) | set(state["not_in_source"])
    if force:
        done = set(state["not_in_source"])
    codes = [c for c in codes if c not in done]
    return codes[:limit] if limit else codes


def _drain(rs) -> bool:
    hit = False
    while rs.next():
        if rs.get_row_data()[0]:
            hit = True
    return hit


def _in_source(bs, code: str) -> bool:
    """源里根本没有这个代码（退市/指数段）才算 not-in-source，接口异常必须抛。"""
    rs = bs.query_stock_basic(code=bs_code(code))
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {rs.error_code}: {rs.error_msg}")
    return _drain(rs)


def _mark(state: dict, key: str, code: str, value=None) -> None:
    """状态集只增不重：--force 重跑同一只时不能再 append 一次。"""
    if key in ("done", "not_in_source"):
        if code not in state[key]:
            state[key].append(code)
    else:
        state[key][code] = value


def backfill_one(bs, code: str, out_dir: Path, today: str, backup_dir: Path) -> str:
    """单只回填。返回 ok:+N / rebase:+N:<detail> / reject:<原因> / not-in-source / fail:<原因>。"""
    path = Path(out_dir) / f"{code}.parquet"
    try:
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
        hfq = _query_retry(bs, bs_code(code), START, today, "1")
        raw = _query_retry(bs, bs_code(code), START, today, "3")
        if not hfq and not raw:
            return "fail:empty-source" if _in_source(bs, code) else "not-in-source"
        if not hfq or not raw:
            return "fail:partial-empty"
        new = merge_rows(pd.DataFrame(columns=COLUMNS), hfq, raw, code=code)
        if new is None:
            return "fail:no-valid-rows"
        action, detail = decide(old, new)
        if action == "reject":
            return f"reject:{detail}"
        merged = combine(old, new)
        _backup_once(path, Path(backup_dir))
        write_atomic(path, merged)
        added = len(merged) - len(old)
    except Exception as e:                 # 逐只隔离：一只坏不中断整轮
        return f"fail:{type(e).__name__}:{str(e)[:120]}"
    if action == "rebase":
        return f"rebase:+{added}:{detail}"
    return f"ok:+{added}"


_BS = None


def _get_bs():
    global _BS
    if _BS is None:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        _BS = bs
    return _BS


def _worker(code: str, out_dir: str, today: str, backup_dir: str):
    try:
        return code, backfill_one(_get_bs(), code, Path(out_dir), today, Path(backup_dir))
    except Exception as e:
        return code, f"fail:{type(e).__name__}:{str(e)[:120]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hfq 因子回填到上市日（baostock 全历史）")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--symbols", default="", help="逗号分隔，彩排用，如 600000,000001")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略 done 集重跑")
    ap.add_argument("--allow-anytime", action="store_true",
                    help="跳过 18:00 时窗检查（只用于彩排）")
    args = ap.parse_args(argv)

    if not args.allow_anytime and datetime.now().hour < 18:
        print("❌ 18:00 前不跑：16:30 的 tdx-sync 后置任务会写同一批 hfq 文件；"
              "彩排请显式加 --allow-anytime", file=sys.stderr)
        return 1

    out_dir = hfq_factor_dir()
    lock = acquire_run_lock(str(STATE_PATH) + ".lock")
    if lock is None:
        print(f"❌ 已有回填进程持有 {STATE_PATH}.lock", file=sys.stderr)
        return 1
    state = load_state()
    todo = plan_codes(out_dir, state, symbols=args.symbols, limit=args.limit,
                      force=args.force)
    print(f"待回填 {len(todo)} 只（done {len(state['done'])} / "
          f"rejected {len(state['rejected'])} / not_in_source {len(state['not_in_source'])}）")
    if not todo:
        return 0
    if args.dry_run:
        print(f"dry-run 前 10 只 {todo[:10]}")
        return 0

    ok = skip = fail = 0
    streak = 0
    tripped = False
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_worker, c, str(out_dir), args.today, str(BACKUP_DIR)): c
                for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                code, r = fut.result()
            except Exception as e:
                code, r = futs[fut], f"fail:{type(e).__name__}"
            if r.startswith("ok:") or r.startswith("rebase:"):
                ok += 1
                streak = 0
                _mark(state, "done", code)
                state["rejected"].pop(code, None)   # 修好后不再留在拒绝集里
                if r.startswith("rebase:"):
                    state["rebased"][code] = r.split(":", 2)[-1]
            elif r == "not-in-source":
                skip += 1
                streak = 0
                _mark(state, "not_in_source", code)
            else:
                fail += 1
                streak += 1
                if r.startswith("reject:"):
                    _mark(state, "rejected", code, r.split(":", 1)[1])
                print(f"  ✗ {code}: {r}", file=sys.stderr)
                if streak >= FAIL_ABORT:
                    pending = sum(1 for f in futs if f.cancel())
                    tripped = True
                    print(f"❌ 连续 {streak} 只失败，撤销 {pending} 个未启动任务",
                          file=sys.stderr)
                    break
            save_state(state)          # 单写者：只有主线程改 state
            if i % 100 == 0:
                print(f"  [{i}/{len(todo)}] ok={ok} skip={skip} fail={fail}", flush=True)

    print(f"完成：ok={ok} not_in_source={skip} fail={fail} 计划={len(todo)}")
    if tripped:
        return 1
    return exit_code(ok, skip, fail)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_hfq_backfill_gate.py -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 彩排 2 只（不写状态集之外的东西，允许白天跑）**

Run: `venv/bin/python scripts/backfill_hfq_history.py --symbols 600000,688048 --allow-anytime --force`
Expected: 两行都应命中 `ok:`——688048 与在役表**同源同参**（都是 baostock adjustflag=1/3），重叠区 `hfq_close` 应逐值相同，所以第一次也不该出现 `rebase`；若出现 `rebase`，先查是不是源改了后复权序列，别直接接受。新增行数按实测基线：

| 代码 | 在役因子首日 | 日线首日 | 首次预期 |
|---|---|---|---|
| 600000 | 2020-01-02 | 1999-11-10 | `ok:+4766` 左右（1999-11 至 2019-12 整段 + 2026-10-29） |
| 688048 | 2022-04-01 | 2022-04-01 | `ok:+0` 或 `ok:+1`（已回填到上市日，只可能补 10-29） |

再跑一次同一条命令：全部 `ok:+0`（幂等，且状态集不重复 append）。

- [ ] **Step 6: 提交**

```bash
git add scripts/backfill_hfq_history.py tests/test_hfq_backfill_gate.py
git commit -m "feat(hfq): 回填器与三道对账门禁（进程并发、单状态文件、熔断）"
```

---

### Task 2: 下跳事件白名单守卫

**Files:**
- Test: `tests/test_hfq_factor_monotone.py`

**Interfaces:**
- Consumes: `data/hfq_factor/*.parquet` 的 `date, hfq_factor`
- Produces: 无代码接口；全表扫描函数 `scan() -> list[tuple[str, str, float]]`

- [ ] **Step 1: 写守卫（数据依赖，`data/` 缺失时根 conftest 自动跳过）**

创建 `tests/test_hfq_factor_monotone.py`：

```python
"""data/hfq_factor 的下跳事件白名单：源漂移的绊线，不是修复器。

spec §1.2：这 12 次下跳不修（000002 是一日凹坑，整段缩放会把回补变成新的假跳），
只钉住清单——新增一个即红，先人眼复核是源修订还是增量写坏了。

范围：断言只覆盖 `date >= 2020-01-01`（在役表原有起点，白名单就是在这一段量的）。
回填会把 2020 年前的历史接进来，那一段的下跳没有基线可比，**只记录不断言**——
清单落在 Task 9 的 `data/hfq_backfill_coverage_20261030.json` 的 `pre2020_jumps` 里。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
HFQ = ROOT / "data" / "hfq_factor"

# (code, date) -> 相对跌幅；2026-10-30 全表实测，11 只 / 12 次
KNOWN_JUMPS = {
    ("000001", "2020-12-31"): -0.16816364,
    ("000002", "2020-11-19"): -0.02555526,
    ("000002", "2025-01-09"): -0.01178885,
    ("000517", "2020-11-17"): -0.01036253,
    ("600372", "2020-11-19"): -0.00387751,
    ("600654", "2020-11-25"): -0.13839379,
    ("600655", "2020-11-25"): -0.09114693,
    ("600681", "2020-11-25"): -0.03255809,
    ("600727", "2020-11-25"): -0.00081152,
    ("600771", "2020-11-25"): -0.00101321,
    ("600817", "2020-11-25"): -0.00271948,
    ("601607", "2020-11-27"): -0.00777702,
}
TOL = 1e-7
SCOPE = "2020-01-01"


def scan(root: Path = HFQ, *,
         since: str | None = SCOPE) -> list[tuple[str, str, float]]:
    """`since=None` 扫全历史（Task 9 审计用）；默认只扫在役段，白名单只对这一段负责。"""
    out: list[tuple[str, str, float]] = []
    for p in sorted(root.glob("*.parquet")):
        code = p.stem
        if not (len(code) == 6 and code.isdigit()):
            continue
        df = pd.read_parquet(p, columns=["date", "hfq_factor"]).sort_values("date")
        f = df["hfq_factor"].astype(float).to_numpy()
        d = df["date"].astype(str).to_numpy()
        for i in range(1, len(f)):
            if f[i - 1] > 0:
                r = f[i] / f[i - 1] - 1.0
                if r < -1e-6 and (since is None or d[i] >= since):
                    out.append((code, d[i], float(r)))
    return out


def test_downward_jumps_match_whitelist():
    found = {(c, d): r for c, d, r in scan()}
    assert set(found) == set(KNOWN_JUMPS), (
        f"新增下跳 {sorted(set(found) - set(KNOWN_JUMPS))}；"
        f"消失 {sorted(set(KNOWN_JUMPS) - set(found))}")
    for key, want in KNOWN_JUMPS.items():
        assert abs(found[key] - want) < TOL, (key, found[key], want)
```

- [ ] **Step 2: 跑守卫确认全绿（`>=2020-01-01` 段就是这 12 次）**

Run: `venv/bin/pytest tests/test_hfq_factor_monotone.py -q`
Expected: PASS（1 passed）。若失败，说明表已变：先查是回填/增量写坏还是源修订，**不要**改白名单蒙过去。回填后 2020 年前若出现下跳不在这里报，去 Task 9 的 `pre2020_jumps` 看清单。

- [ ] **Step 3: 提交**

```bash
git add tests/test_hfq_factor_monotone.py
git commit -m "test(hfq): 钉住全表 11 只 12 次因子下跳"
```

---

### Task 3: 读侧咽喉 `tdxfeed/adjusted.py`

**Files:**
- Create: `tdxfeed/adjusted.py`
- Test: `tests/test_adjusted_frame.py`

**Interfaces:**
- Consumes: `tdxfeed.paths.daily_path` / `tdxfeed.paths.hfq_factor_path`
- Produces: `frame(symbol, *, kind="raw"|"hfq", strict=False) -> pd.DataFrame`；`AdjustmentError(RuntimeError)`；`MIN_FACTOR_COV = 0.999`、`FACTOR_RAW_TOL = 0.01`；attrs 含 `kind/factor_cov/factor_first/factor_last/factor_missing/factor_head_lag/factor_tail_lag/factor_mismatch/factor_raw_dev`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_adjusted_frame.py`：

```python
"""读侧咽喉：raw 逐值同盘、hfq=OHLC×当日因子、三条 strict 门禁。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdxfeed import adjusted
from tdxfeed.adjusted import AdjustmentError

CODE = "600000"


def _daily(n: int, start=date(2020, 1, 2)) -> pd.DataFrame:
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(n)],
        "open": np.linspace(10.0, 20.0, n),
        "high": np.linspace(10.5, 20.5, n),
        "low": np.linspace(9.5, 19.5, n),
        "close": np.linspace(10.0, 20.0, n),
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })


def _factor(daily: pd.DataFrame, values, start: int = 0, stop: int | None = None) -> pd.DataFrame:
    """按日线切片造因子表：raw_close 必须等于日线 close，否则会触发同码双文件门禁。"""
    sub = daily.iloc[start:stop]
    v = np.asarray(values, dtype=float)
    assert len(v) == len(sub)
    return pd.DataFrame({"code": CODE, "date": list(sub["date"]), "hfq_factor": v,
                         "raw_close": sub["close"].to_numpy(dtype=float),
                         "hfq_close": sub["close"].to_numpy(dtype=float) * v})


def _wire(monkeypatch, tmp_path, daily: pd.DataFrame, factor: pd.DataFrame | None):
    d = tmp_path / "daily"; d.mkdir()
    dfp = d / f"sh{CODE}.parquet"
    daily.to_parquet(dfp, index=False)
    fdir = tmp_path / "hfq_factor"; fdir.mkdir()
    fp = fdir / f"{CODE}.parquet"
    if factor is not None:
        factor.to_parquet(fp, index=False)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: dfp)
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: fp)
    return dfp, fp


def test_raw_is_identical_to_disk(monkeypatch, tmp_path):
    daily = _daily(40)
    dfp, _ = _wire(monkeypatch, tmp_path, daily, None)
    got = adjusted.frame(CODE, kind="raw")
    pd.testing.assert_frame_equal(got, pd.read_parquet(dfp))


def test_hfq_multiplies_ohlc_only(monkeypatch, tmp_path):
    daily = _daily(40)
    fac = _factor(daily, np.where(np.arange(40) < 20, 1.0, 1.5))
    _wire(monkeypatch, tmp_path, daily, fac)
    got = adjusted.frame(CODE, kind="hfq")
    assert got.attrs["kind"] == "hfq"
    assert got.attrs["factor_cov"] == 1.0
    np.testing.assert_allclose(got["close"], daily["close"] * np.where(np.arange(40) < 20, 1.0, 1.5))
    np.testing.assert_allclose(got["open"], daily["open"] * np.where(np.arange(40) < 20, 1.0, 1.5))
    np.testing.assert_allclose(got["volume"], daily["volume"])
    np.testing.assert_allclose(got["amount"], daily["amount"])
    assert list(got["date"]) == list(daily["date"])


def test_missing_factor_file_is_factor_one_and_flagged(monkeypatch, tmp_path):
    daily = _daily(40)
    _wire(monkeypatch, tmp_path, daily, None)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    np.testing.assert_allclose(got["close"], daily["close"])
    assert got.attrs["factor_missing"] is True
    assert got.attrs["factor_cov"] == 0.0
    with pytest.raises(AdjustmentError, match="缺"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_strict_coverage_gate(monkeypatch, tmp_path):
    daily = _daily(1000)
    fac = _factor(daily, np.full(997, 1.2), start=3)
    _wire(monkeypatch, tmp_path, daily, fac)
    assert adjusted.frame(CODE, kind="hfq", strict=False).attrs["factor_cov"] < adjusted.MIN_FACTOR_COV
    with pytest.raises(AdjustmentError, match="factor_cov"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_strict_head_lag_gate_on_long_history(monkeypatch, tmp_path):
    daily = _daily(10000)
    fac = _factor(daily, np.full(9994, 1.2), start=6)
    _wire(monkeypatch, tmp_path, daily, fac)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    assert got.attrs["factor_cov"] >= adjusted.MIN_FACTOR_COV   # 覆盖率过关
    assert got.attrs["factor_head_lag"] == 6
    with pytest.raises(AdjustmentError, match="factor_first"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_strict_tail_lag_gate(monkeypatch, tmp_path):
    daily = _daily(100)
    fac = _factor(daily, np.full(98, 1.2), stop=-2)
    _wire(monkeypatch, tmp_path, daily, fac)
    with pytest.raises(AdjustmentError, match="factor_last"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_factor_raw_close_mismatch_is_treated_as_missing(monkeypatch, tmp_path):
    """sh000001 的裸 6 位因子文件其实是 000001 平安银行：raw_close 对不上就绝不能乘。"""
    daily = _daily(40)
    fac = _factor(daily, np.full(40, 1.2))
    fac["raw_close"] = fac["raw_close"] * 0.5      # 中位偏差 50%，远超 1%
    _wire(monkeypatch, tmp_path, daily, fac)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    np.testing.assert_allclose(got["close"], daily["close"])   # 没有乘 1.2
    assert got.attrs["factor_mismatch"] is True
    assert got.attrs["factor_missing"] is True
    assert got.attrs["factor_raw_dev"] > adjusted.FACTOR_RAW_TOL
    with pytest.raises(AdjustmentError, match="raw_close"):
        adjusted.frame(CODE, kind="hfq", strict=True)


def test_never_falls_back_to_adj_factor(monkeypatch, tmp_path):
    daily = _daily(40)
    _wire(monkeypatch, tmp_path, daily, None)
    adj_dir = tmp_path / "adj_factor"; adj_dir.mkdir()
    pd.DataFrame({"date": daily["date"], "adj_factor": np.full(40, 2.0)}).to_parquet(
        adj_dir / f"{CODE}.parquet", index=False)
    got = adjusted.frame(CODE, kind="hfq", strict=False)
    np.testing.assert_allclose(got["close"], daily["close"])   # 没有乘 2.0


def test_unknown_kind_raises(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _daily(40), None)
    with pytest.raises(ValueError):
        adjusted.frame(CODE, kind="qfq")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_adjusted_frame.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'tdxfeed.adjusted'`）

- [ ] **Step 3: 实现咽喉**

创建 `tdxfeed/adjusted.py`：

```python
"""复权读侧咽喉：价格派生只从这里取，因子表只有 data/hfq_factor 一张。

kind="raw"  原样返回日线（不排序、不改列，比照盘上逐值一致）；
kind="hfq"  按日左连 hfq_factor 后 ffill，只乘 OHLC，volume / amount / date 不动。

strict=True（训练 / 建表）在四处 raise：
  - 因子表 raw_close 与日线 close 的中位相对偏差 > 1%（同码双文件：sh000001 指
    数与 000001 平安银行共用 000001.parquet，实测中位偏差 99.65%）；
  - 覆盖率 factor_cov < 0.999；
  - 因子首行晚于日线首行超过 5 个交易日；
  - 因子末日早于日线末日超过 1 个交易日。
strict=False（预测）只记 attrs 与日志——新股 / 缺因子的票不能让整轮预测打挂，
但必须在 attrs 里看得见，不能静默变 raw；raw_close 对不上时**不乘因子**，
宁可让这只票按 raw 走（与它原本的现状一致），也不能乘上别只票的因子。
"""
from __future__ import annotations

import logging

import pandas as pd

from .paths import daily_path, hfq_factor_path

log = logging.getLogger("adjusted")

MIN_FACTOR_COV = 0.999
FACTOR_RAW_TOL = 0.01
HEAD_LAG_MAX = 5
TAIL_LAG_MAX = 1
PRICE_COLS = ("open", "high", "low", "close")


class AdjustmentError(RuntimeError):
    """复权口径门禁未通过。"""


def _raw_close_dev(daily: pd.DataFrame, dates: pd.Series,
                   fac: pd.DataFrame) -> float | None:
    """重叠区 |raw_close / close - 1| 的中位数。

    正常个股是 0.0（同源同参）；sh000001 读 000001.parquet 时实测 99.65%。
    用中位数而不是最大值：个别行的源修订不该把整只票判成错配。
    """
    left = pd.DataFrame({"date": dates, "close": daily["close"].astype(float).to_numpy()})
    ov = left.merge(fac[["date", "raw_close"]], on="date")
    ov = ov[(ov["close"] > 0) & (ov["raw_close"] > 0)]
    if ov.empty:
        return None
    return float((ov["raw_close"] / ov["close"] - 1.0).abs().median())


def frame(symbol: str, *, kind: str = "raw", strict: bool = False) -> pd.DataFrame:
    if kind not in ("raw", "hfq"):
        raise ValueError(f"kind 只接受 raw / hfq：{kind!r}")
    daily = pd.read_parquet(daily_path(symbol))
    if kind == "raw":
        daily.attrs["kind"] = "raw"
        return daily

    daily = daily.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(daily["date"])
    fac_path = hfq_factor_path(symbol)
    fac = None
    if fac_path.exists():
        fac = pd.read_parquet(fac_path, columns=["date", "hfq_factor", "raw_close"])
        fac = fac.assign(date=pd.to_datetime(fac["date"]))
        fac = fac.dropna(subset=["hfq_factor"]).sort_values("date")

    attrs = {"kind": "hfq", "factor_cov": 0.0, "factor_first": None,
             "factor_last": None, "factor_missing": True,
             "factor_head_lag": None, "factor_tail_lag": None,
             "factor_mismatch": False, "factor_raw_dev": None}
    if fac is not None and not fac.empty:
        first, last = fac["date"].iloc[0], fac["date"].iloc[-1]
        head_lag = int((dates < first).sum())
        tail_lag = int((dates > last).sum())
        cov = float(len(daily) - head_lag) / len(daily) if len(daily) else 0.0
        raw_dev = _raw_close_dev(daily, dates, fac)
        attrs.update(factor_cov=cov, factor_first=str(first.date()),
                     factor_last=str(last.date()),
                     factor_missing=cov < MIN_FACTOR_COV,
                     factor_head_lag=head_lag, factor_tail_lag=tail_lag,
                     factor_raw_dev=raw_dev)
        if raw_dev is not None and raw_dev > FACTOR_RAW_TOL:
            attrs.update(factor_cov=0.0, factor_missing=True, factor_mismatch=True)
            msg = (f"因子表 raw_close 与日线 close 中位偏差 {raw_dev:.2%}"
                   f"（> {FACTOR_RAW_TOL:.0%}）：疑似同码双文件，拒绝乘因子")
            if strict:
                raise AdjustmentError(f"{symbol}: {msg}")
            log.warning("%s: %s（按 raw 返回）", symbol, msg)
            out = daily.copy()
            out.attrs.update(attrs)
            return out
        problem = None
        if cov < MIN_FACTOR_COV:
            problem = f"factor_cov={cov:.4f} < {MIN_FACTOR_COV}"
        elif head_lag > HEAD_LAG_MAX:
            problem = f"factor_first={first.date()} 晚于日线首行 {head_lag} 个交易日"
        elif tail_lag > TAIL_LAG_MAX:
            problem = f"factor_last={last.date()} 早于日线末日 {tail_lag} 个交易日"
        if problem:
            if strict:
                raise AdjustmentError(f"{symbol}: {problem}")
            log.warning("%s: %s（按缺失处理，不回落 adj_factor）", symbol, problem)
        aligned = fac.set_index("date")["hfq_factor"].reindex(dates).ffill()
        factor = pd.Series(aligned.fillna(1.0).to_numpy(), index=daily.index)
    elif strict:
        raise AdjustmentError(f"{symbol}: 缺 {fac_path.name}，不能按 hfq 口径建帧")
    else:
        factor = pd.Series(1.0, index=daily.index)
        log.warning("%s: 缺后复权因子文件 %s，按 factor=1.0 处理", symbol, fac_path)

    out = daily.copy()
    for c in PRICE_COLS:
        if c in out.columns:
            out[c] = out[c].astype(float) * factor
    out.attrs.update(attrs)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_adjusted_frame.py -q`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/adjusted.py tests/test_adjusted_frame.py
git commit -m "feat(adjusted): 复权读侧咽喉与三条 strict 门禁"
```

---

### Task 4: 29 维特征逐维分派（pandas + polars）

**Files:**
- Modify: `tdxfeed/features_5d.py:94`、`:172`
- Modify: `tdxfeed/factors_polars.py:50`、`:102`
- Test: `tests/test_features_5d_price_basis.py`

**Interfaces:**
- Consumes: 无（帧由调用方经 `adjusted.frame` 取得）
- Produces: `compute_window_features(df, code, sig_idx=-1, frame_hfq=None)`、`compute_window_features_extended(df, code, sig_idx=-1, frame_hfq=None)`；polars 版同签名

- [ ] **Step 1: 写失败测试**

创建 `tests/test_features_5d_price_basis.py`：

```python
"""合成除权消融：13 个 hfq 维与无除权序列逐值相同，8 个 raw 维随 raw 变。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest

from tdxfeed.factors_polars import compute_window_features_extended_polars
from tdxfeed.features_5d import compute_window_features_extended

HFQ_DIMS = [
    "ret_5d", "ret_3d", "max_5d_pct", "pct_t1", "volatility_5d", "close_pos_5d",
    "max_drawdown_5d", "acceleration", "range_efficiency", "vol_norm_ret",
    "up_down_ratio", "dist_to_5d_high", "dist_to_5d_low",
]
RAW_DIMS = ["limit", "prev_close", "limit_up_5d", "limit_up_t1", "is_limit_up_today",
            "consecutive_boards", "board_position", "amt_per_price"]
AMOUNT_DIMS = ["avg_amount_5d", "vol_ratio_t1", "turnover_proxy", "amount_surge_3d"]
ENUM_DIMS = ["is_st", "is_new", "is_bj", "is_gem"]


def _frames(closes, factor):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    raw = pd.DataFrame({
        "date": [date(2020, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": closes * 0.995,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })
    hfq = raw.copy()
    for c in ("open", "high", "low", "close"):
        hfq[c] = raw[c] * np.asarray(factor, dtype=float)
    return raw, hfq


# 第 5 行是 10 转 3 的除权日：raw −30%，后复权因子 1 → 10/7
CLOSES = [10.0] * 5 + [7.0, 7.1, 7.8, 8.0, 8.2]
FACTOR = [1.0] * 5 + [10 / 7] * 5


def test_hfq_dims_equal_the_no_jump_series():
    raw, hfq = _frames(CLOSES, FACTOR)
    got = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq)
    ref = compute_window_features_extended(hfq, "600000", sig_idx=8)
    for k in HFQ_DIMS:
        assert got[k] == pytest.approx(ref[k], rel=1e-12, abs=1e-12), k


def test_raw_and_amount_dims_follow_raw():
    raw, hfq = _frames(CLOSES, FACTOR)
    got = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq)
    ref = compute_window_features_extended(raw, "600000", sig_idx=8)
    for k in RAW_DIMS + AMOUNT_DIMS + ENUM_DIMS:
        assert got[k] == pytest.approx(ref[k], rel=1e-12, abs=1e-12), k


def test_limit_branch_uses_raw_when_factor_moves_on_a_limit_day():
    # 第 5 行 raw +10%（封板），同日后复权因子 1 → 0.7：hfq 口径下这行是 −23%
    closes = [10.0] * 5 + [11.0, 11.0, 11.0, 11.0, 11.0]
    factor = [1.0] * 5 + [0.7] * 5
    raw, hfq = _frames(closes, factor)
    got = compute_window_features_extended(raw, "600000", sig_idx=5, frame_hfq=hfq)
    assert got["is_limit_up_today"] == 1                  # 涨停必须按 raw 判
    assert got["consecutive_boards"] == 1
    # 价格维按 hfq：窗口 1..5 的 pct 前四项为 0，最后一项 7.7/10−1 = −23%，
    # 落在 max_drawdown_5d 上（max_5d_pct 取最大，仍是首元素 0）。
    assert got["max_drawdown_5d"] == pytest.approx(0.7 * 11.0 / 10.0 - 1.0, rel=1e-9)
    assert got["max_5d_pct"] == pytest.approx(0.0, abs=1e-12)


def test_passing_raw_as_hfq_frame_changes_nothing():
    raw, _ = _frames(CLOSES, FACTOR)
    a = compute_window_features_extended(raw, "600000", sig_idx=8)
    b = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=raw.copy())
    assert a == pytest.approx(b, rel=1e-12, abs=1e-12)


def test_misaligned_frames_raise():
    raw, hfq = _frames(CLOSES, FACTOR)
    with pytest.raises(ValueError):
        compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq.iloc[1:])


def test_polars_matches_pandas_on_both_bases():
    raw, hfq = _frames(CLOSES, FACTOR)
    p = compute_window_features_extended(raw, "600000", sig_idx=8, frame_hfq=hfq)
    q = compute_window_features_extended_polars(
        pl.from_pandas(raw), "600000", sig_idx=8, frame_hfq=pl.from_pandas(hfq))
    for k in HFQ_DIMS + RAW_DIMS + AMOUNT_DIMS + ENUM_DIMS:
        assert q[k] == pytest.approx(p[k], rel=1e-9, abs=1e-12), k
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_features_5d_price_basis.py -q`
Expected: FAIL（`TypeError: ... unexpected keyword argument 'frame_hfq'`）

- [ ] **Step 3: 改 `tdxfeed/features_5d.py`**

在 `get_limit` 与 `compute_window_features` 之间插入：

```python
def _px_frame(df: pd.DataFrame, frame_hfq: pd.DataFrame | None) -> pd.DataFrame:
    """价格口径帧：None 时就是 raw 帧。两帧必须同日同长，错位直接 raise。"""
    if frame_hfq is None:
        return df
    if len(frame_hfq) != len(df):
        raise ValueError(f"hfq 帧 {len(frame_hfq)} 行 != raw 帧 {len(df)} 行")
    a = pd.to_datetime(df["date"]).reset_index(drop=True)
    b = pd.to_datetime(frame_hfq["date"]).reset_index(drop=True)
    if not a.equals(b):
        raise ValueError("hfq 帧与 raw 帧的 date 列不逐值相同")
    return frame_hfq
```

把 `compute_window_features` 的函数体替换为（签名加 `frame_hfq=None`）：

```python
def compute_window_features(
    df: pd.DataFrame,
    code: str,
    sig_idx: int = -1,
    frame_hfq: pd.DataFrame | None = None,
) -> "dict | None":
    if df is None or len(df) < 5:
        return None

    if sig_idx < 0:
        sig_idx = len(df) + sig_idx
    if sig_idx < 4 or sig_idx >= len(df):
        return None
    px = _px_frame(df, frame_hfq)

    prev_5 = df.iloc[sig_idx - 4 : sig_idx + 1]        # raw：涨停 / 成交额
    px_5 = px.iloc[sig_idx - 4 : sig_idx + 1]          # 价格口径：比率 / 位置
    sig_day = df.iloc[sig_idx]
    px_day = px.iloc[sig_idx]

    limit = get_limit(code)
    pct_raw = prev_5["close"].pct_change().fillna(0)
    pct_px = px_5["close"].pct_change().fillna(0)
    hi_max, lo_min = px_5["high"].max(), px_5["low"].min()

    feats = {
        "limit": limit,
        "prev_close": float(sig_day["close"]),
        "ret_5d": float(px_5["close"].iloc[-1] / px_5["close"].iloc[0] - 1),
        "ret_3d": float(px_5["close"].iloc[-1] / px_5["close"].iloc[-4] - 1) if len(px_5) >= 4 else 0,
        "max_5d_pct": float(pct_px.max()),
        "limit_up_5d": int((pct_raw >= limit - 0.001).sum()),
        "limit_up_t1": int(sig_day["close"] / sig_day["open"] - 1 >= limit - 0.001) if sig_day["open"] > 0 else 0,
        "pct_t1": float(px_day["close"] / px_day["open"] - 1) if px_day["open"] > 0 else 0,
        "avg_amount_5d": float(prev_5["amount"].mean()),
        "vol_ratio_t1": float(sig_day["amount"] / prev_5["amount"].mean()) if prev_5["amount"].mean() > 0 else 1.0,
        "volatility_5d": float(pct_px.std()),
        "close_pos_5d": float((px_day["close"] - lo_min) / (hi_max - lo_min)) if hi_max > lo_min else 0.5,
        "max_drawdown_5d": float(pct_px.min()),
        "turnover_proxy": float(sig_day["amount"] / sig_day["volume"]) if sig_day["volume"] > 0 else 0,
        "is_st": 0,
        "is_new": int(sig_idx < 250),
        "is_bj": int(code.startswith("bj") or code.startswith("920")),
        "is_gem": int(
            code.startswith("300")
            or code.startswith("301")
            or code.startswith("688")
            or code.startswith("sz300")
            or code.startswith("sz301")
            or code.startswith("sh688")
        ),
    }
    return feats
```

把 `compute_window_features_extended` 的函数体替换为（签名加 `frame_hfq=None`）：

```python
def compute_window_features_extended(
    df: pd.DataFrame,
    code: str,
    sig_idx: int = -1,
    frame_hfq: pd.DataFrame | None = None,
) -> "dict | None":
    base = compute_window_features(df, code, sig_idx=sig_idx, frame_hfq=frame_hfq)
    if base is None:
        return None

    if sig_idx < 0:
        sig_idx = len(df) + sig_idx
    px = _px_frame(df, frame_hfq)
    px_5 = px.iloc[sig_idx - 4 : sig_idx + 1]
    raw_5 = df.iloc[sig_idx - 4 : sig_idx + 1]
    close = px_5["close"]
    pct = close.pct_change().fillna(0)
    amount = raw_5["amount"]
    high = px_5["high"]
    low = px_5["low"]
    open_ = px_5["open"]

    accel = float(base["ret_5d"] - base["ret_3d"])
    daily_range = (high - low).replace(0, np.nan)
    efficiency = float(((close - open_) / daily_range).mean())
    if np.isnan(efficiency):
        efficiency = 0.0
    vol_norm = float(base["ret_5d"] / base["volatility_5d"]) if base["volatility_5d"] > 0 else 0.0
    amount_3d_mean = float(amount.iloc[-3:].mean())
    surge_3d = float(amount.iloc[-1] / amount_3d_mean) if amount_3d_mean > 0 else 1.0
    up_days = int((pct > 0).sum())
    down_days = int((pct < 0).sum())
    up_down = float(up_days / max(down_days, 1))
    high_max = float(high.max())
    dist_high = float((high_max - close.iloc[-1]) / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0
    low_min = float(low.min())
    dist_low = float((close.iloc[-1] - low_min) / close.iloc[-1]) if close.iloc[-1] > 0 else 0.0
    amt_per_price = float(base["avg_amount_5d"] / base["prev_close"]) if base["prev_close"] > 0 else 0.0

    # 封板 / 连板只认 raw：涨停是交易所绝对价规则，复权序列上的涨幅没有意义。
    close_full = df["close"]
    if sig_idx >= 1:
        limit_up_mask = (close_full.pct_change() >= base["limit"] - 0.001)
        is_limit_today = int(limit_up_mask.iloc[sig_idx])
        consec = 0
        i = sig_idx
        while i >= 1 and limit_up_mask.iloc[i]:
            consec += 1
            i -= 1
    else:
        is_limit_today, consec = 0, 0
    board_position = 1 if consec == 1 else (2 if consec == 2 else (3 if consec >= 3 else 0))

    return {
        **base,
        "acceleration": accel,
        "range_efficiency": efficiency,
        "vol_norm_ret": vol_norm,
        "amount_surge_3d": surge_3d,
        "up_down_ratio": up_down,
        "dist_to_5d_high": dist_high,
        "dist_to_5d_low": dist_low,
        "amt_per_price": amt_per_price,
        "is_limit_up_today": is_limit_today,
        "consecutive_boards": consec,
        "board_position": board_position,
    }
```

同时把模块 docstring 顶部「所有价格类特征必须使用 raw close」改成：

```
价格类特征分两口径（2026-10-30 起）：
  hfq（13 维）：ret_5d/ret_3d/max_5d_pct/pct_t1/volatility_5d/close_pos_5d/
    max_drawdown_5d/acceleration/range_efficiency/vol_norm_ret/up_down_ratio/
    dist_to_5d_high/dist_to_5d_low —— 除权日必须连续，取 frame_hfq；
  raw（8 维）：limit/prev_close/limit_up_5d/limit_up_t1/is_limit_up_today/
    consecutive_boards/board_position/amt_per_price —— 交易所绝对价规则。
```

- [ ] **Step 4: 改 `tdxfeed/factors_polars.py`（与 pandas 同分派）**

`compute_window_features_polars` 签名加 `frame_hfq: pl.DataFrame | None = None`，函数体替换为：

```python
def compute_window_features_polars(
    df: pl.DataFrame, code: str, sig_idx: int = -1,
    frame_hfq: pl.DataFrame | None = None,
) -> dict | None:
    n = df.height
    if n < 5:
        return None
    if sig_idx < 0:
        sig_idx = n + sig_idx
    if sig_idx < 4 or sig_idx >= n:
        return None
    w = _window_scalars(df, sig_idx)
    wx = _window_scalars(frame_hfq, sig_idx) if frame_hfq is not None else w
    if w is None or wx is None:
        return None
    c, h, l, a = w["c"], w["h"], w["l"], w["a"]
    cx, hx, lx, ox = wx["c"], wx["h"], wx["l"], wx["o"]
    sig_close, sig_open = w["sig_close"], w["sig_open"]
    sig_vol, sig_amt = w["sig_vol"], w["sig_amt"]

    def _pct(xs):
        return [0.0] + [(xs[i] / xs[i - 1] - 1.0) if xs[i - 1] else 0.0 for i in range(1, 5)]

    pct_raw = _pct(c)
    pct_px = _pct(cx)
    limit = float(get_limit(code))
    amt_mean = sum(a) / len(a)
    hi_max, lo_min = max(hx), min(lx)

    feats = {
        "limit": limit,
        "prev_close": float(sig_close),
        "ret_5d": float(cx[-1] / cx[0] - 1.0),
        "ret_3d": float(cx[-1] / cx[-4] - 1.0) if len(cx) >= 4 else 0.0,
        "max_5d_pct": float(max(pct_px)),
        "limit_up_5d": int(sum(1 for x in pct_raw if is_limit_up_ret(x, limit))),
        "limit_up_t1": (int(is_limit_up_ret(sig_close / sig_open - 1.0, limit))
                        if sig_open > 0 else 0),
        "pct_t1": float(wx["sig_close"] / wx["sig_open"] - 1.0) if wx["sig_open"] > 0 else 0.0,
        "avg_amount_5d": float(amt_mean),
        "vol_ratio_t1": float(a[-1] / amt_mean) if amt_mean > 0 else 1.0,
        "volatility_5d": float(np.std(pct_px, ddof=1)) if len(pct_px) > 1 else 0.0,
        "close_pos_5d": float((wx["sig_close"] - lo_min) / (hi_max - lo_min)) if hi_max > lo_min else 0.5,
        "max_drawdown_5d": float(min(pct_px)),
        "turnover_proxy": float(sig_amt / sig_vol) if sig_vol > 0 else 0.0,
        "is_st": 0,
        "is_new": int(sig_idx < 250),
        "is_bj": int(str(code).startswith("bj") or str(code).startswith("920")),
        "is_gem": int(
            str(code).startswith("300") or str(code).startswith("301")
            or str(code).startswith("688") or str(code).startswith("sz300")
            or str(code).startswith("sz301") or str(code).startswith("sh688")
        ),
    }
    return feats
```

`compute_window_features_extended_polars` 签名加 `frame_hfq: pl.DataFrame | None = None`，函数体替换为：

```python
def compute_window_features_extended_polars(
    df: pl.DataFrame, code: str, sig_idx: int = -1,
    frame_hfq: pl.DataFrame | None = None,
) -> dict | None:
    base = compute_window_features_polars(df, code, sig_idx=sig_idx, frame_hfq=frame_hfq)
    if base is None:
        return None
    n = df.height
    if sig_idx < 0:
        sig_idx = n + sig_idx
    px = frame_hfq if frame_hfq is not None else df
    p5 = px.slice(sig_idx - 4, 5)
    a = df.slice(sig_idx - 4, 5)["amount"].to_list()
    c = p5["close"].to_list()
    o = p5["open"].to_list()
    h = p5["high"].to_list()
    l = p5["low"].to_list()
    close = c
    pct = [0.0] + [(c[i] / c[i - 1] - 1.0) if c[i - 1] else 0.0 for i in range(1, 5)]
    daily_range = [(h[i] - l[i]) if (h[i] - l[i]) != 0 else np.nan for i in range(len(h))]
    eff_vals = [(close[i] - o[i]) / r for i, r in enumerate(daily_range)]
    efficiency = float(np.nanmean(eff_vals)) if eff_vals else 0.0
    if np.isnan(efficiency):
        efficiency = 0.0
    vol_norm = float(base["ret_5d"] / base["volatility_5d"]) if base["volatility_5d"] > 0 else 0.0
    amt_3d_mean = float(sum(a[-3:]) / 3) if len(a) >= 3 else float(sum(a) / len(a))
    surge_3d = float(a[-1] / amt_3d_mean) if amt_3d_mean > 0 else 1.0
    up_days = int(sum(1 for x in pct if x > 0))
    down_days = int(sum(1 for x in pct if x < 0))
    up_down = float(up_days / max(down_days, 1))
    hi_max = float(max(h))
    lo_min = float(min(l))
    dist_high = float((hi_max - close[-1]) / close[-1]) if close[-1] > 0 else 0.0
    dist_low = float((close[-1] - lo_min) / close[-1]) if close[-1] > 0 else 0.0
    amt_per_price = float(base["avg_amount_5d"] / base["prev_close"]) if base["prev_close"] > 0 else 0.0

    # 封板 / 连板只认 raw（与 features_5d 同一条判据）
    close_full = df["close"].to_list()
    limit_val = float(base["limit"])
    if sig_idx >= 1:
        limit_up_mask = [
            i >= 1
            and is_limit_up_ret(close_full[i] / close_full[i - 1] - 1.0, limit_val)
            if close_full[i - 1] else False
            for i in range(len(close_full))
        ]
        is_limit_today = int(limit_up_mask[sig_idx]) if sig_idx < len(close_full) else 0
        consec = 0
        i = sig_idx
        while i >= 1 and i < len(limit_up_mask) and limit_up_mask[i]:
            consec += 1
            i -= 1
    else:
        is_limit_today, consec = 0, 0
    board_position = 1 if consec == 1 else (2 if consec == 2 else (3 if consec >= 3 else 0))

    return {
        **base,
        "acceleration": float(base["ret_5d"] - base["ret_3d"]),
        "range_efficiency": efficiency,
        "vol_norm_ret": vol_norm,
        "amount_surge_3d": surge_3d,
        "up_down_ratio": up_down,
        "dist_to_5d_high": dist_high,
        "dist_to_5d_low": dist_low,
        "amt_per_price": amt_per_price,
        "is_limit_up_today": is_limit_today,
        "consecutive_boards": consec,
        "board_position": board_position,
    }
```

同时在 `stage1_wide_table` 之外还要保证两帧同日：`compute_window_features_extended_polars` 的调用方负责（Task 6 里用 `adjusted.frame` 取帧）。

- [ ] **Step 5: 跑测试确认通过，并跑全量既有特征测试**

Run: `venv/bin/pytest tests/test_features_5d_price_basis.py -q`
Expected: PASS（6 passed）

Run: `venv/bin/pytest tests/ -q -k "feature or factors or stage1"`
Expected: 全绿（若红，先判断是否他人会话的改动）

- [ ] **Step 6: 提交**

```bash
git add tdxfeed/features_5d.py tdxfeed/factors_polars.py tests/test_features_5d_price_basis.py
git commit -m "feat(features): 29 维逐维价格口径分派（13 hfq / 8 raw / 4 量纲 / 4 枚举）"
```

---

### Task 5: `hypothetical_pnl` 切 hfq

**Files:**
- Modify: `scripts/train_pnl_proxy.py:69`、`:93`
- Modify: `scripts/recompute_pnl_labels.py:21`
- Test: `tests/test_label_price_basis.py`

**Interfaces:**
- Consumes: `tdxfeed.adjusted.frame` / `MIN_FACTOR_COV`
- Produces: `compute_hypothetical_pnl(code, sig_date, price_basis="raw")`、`attach_pnl(df, max_workers=8, price_basis="raw")`、`recompute_pnl_labels.main(["--price-basis", ...])`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_label_price_basis.py`：

```python
"""同一除权窗口：raw 标签吃到假止损，hfq 标签连续。"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import train_pnl_proxy as tpp  # noqa: E402


def _bars(closes, factor):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    raw = pd.DataFrame({
        "date": pd.to_datetime([date(2020, 1, 2) + timedelta(days=i) for i in range(n)]),
        "open": closes * 1.0,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })
    hfq = raw.copy()
    for c in ("open", "high", "low", "close"):
        hfq[c] = raw[c] * np.asarray(factor, dtype=float)
    return raw, hfq


def _with_attrs(df: pd.DataFrame, **kw) -> pd.DataFrame:
    """DataFrame.assign(attrs=...) 建的是列；frame attrs 只能这样挂。"""
    out = df.copy()
    out.attrs.update(kw)
    return out


def test_raw_label_takes_fake_stop_hfq_label_does_not(monkeypatch):
    # 第 2 行是除权日：raw 从 10 跌到 6.8（−32%），hfq 连续
    closes = [10.0, 10.0, 6.8, 6.9, 7.0, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7]
    factor = [1.0, 1.0] + [10 / 6.8] * 10
    raw, hfq = _bars(closes, factor)
    monkeypatch.setattr(tpp, "_load_one", lambda code: raw)
    monkeypatch.setattr(tpp, "adj_frame",
                        lambda code, kind, strict: _with_attrs(hfq, factor_cov=1.0))
    pnl_raw = tpp.compute_hypothetical_pnl("600000", pd.Timestamp("2020-01-02"), "raw")
    pnl_hfq = tpp.compute_hypothetical_pnl("600000", pd.Timestamp("2020-01-02"), "hfq")
    assert pnl_raw < -0.09          # 假止损
    assert pnl_hfq > pnl_raw + 0.2  # 连续路径不触发止损


def test_hfq_label_missing_factor_returns_none(monkeypatch):
    raw, _ = _bars([10.0] * 12, [1.0] * 12)
    monkeypatch.setattr(tpp, "_load_one", lambda code: raw)
    monkeypatch.setattr(tpp, "adj_frame",
                        lambda code, kind, strict: _with_attrs(raw, factor_cov=0.5))
    assert tpp.compute_hypothetical_pnl("600000", pd.Timestamp("2020-01-02"), "hfq") is None


def test_recompute_writes_separate_file_for_hfq(monkeypatch, tmp_path):
    import recompute_pnl_labels as rpl
    src = tmp_path / "in.csv"
    pd.DataFrame({
        "code": ["600000", "600001"], "signal_date": ["20200102", "20200103"],
        "f": [1.0, 2.0], "hypothetical_pnl": [0.1, 0.2],
    }).to_csv(src, index=False)
    out = tmp_path / "out.csv"
    monkeypatch.setattr(rpl, "daily_files", lambda: [tmp_path / "600000.parquet",
                                                     tmp_path / "600001.parquet"])
    monkeypatch.setattr(rpl, "attach_pnl",
                        lambda df, max_workers=8, price_basis="raw": df.assign(
                            hypothetical_pnl=lambda d: d["code"].map(
                                {"600000": 0.05, "600001": 0.06})))
    assert rpl.main(["--price-basis", "hfq", "--src", str(src), "--out", str(out)]) == 0
    got = pd.read_csv(out)
    assert list(got["hypothetical_pnl"]) == pytest.approx([0.05, 0.06])
    assert "hypothetical_pnl" in got.columns
    assert str(out).endswith("out.csv")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_label_price_basis.py -q`
Expected: FAIL（`AttributeError: module 'train_pnl_proxy' has no attribute 'adj_frame'`）

- [ ] **Step 3: 改 `scripts/train_pnl_proxy.py`**

在 imports 之后加：

```python
from tdxfeed.adjusted import MIN_FACTOR_COV, frame as adj_frame  # noqa: E402
```

在 `_load_one` 之后插入：

```python
def _load_one_basis(code: str, price_basis: str) -> pd.DataFrame:
    """raw 走原加载器（零改动）；hfq 走咽喉，覆盖率不足返回空表交给上层记 None。"""
    if price_basis == "raw":
        return _load_one(code)
    f = adj_frame(code, kind="hfq", strict=False)
    if f.attrs.get("factor_cov", 0.0) < MIN_FACTOR_COV:
        return pd.DataFrame()
    f = f.copy()
    f["date"] = pd.to_datetime(f["date"])
    f["code"] = code
    return f.sort_values("date").reset_index(drop=True)
```

把 `compute_hypothetical_pnl` 与 `attach_pnl` 替换为：

```python
def compute_hypothetical_pnl(code: str, sig_date: pd.Timestamp,
                             price_basis: str = "raw") -> float | None:
    """与 plan_e 同一结算口径的 PnL 代理标签。返回 None 表示数据不足。"""
    df = _load_one_basis(code, price_basis)
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
    costs = CostModel(commission=FEE / 2, slippage=SLIP / 2, stamp_sell=0.0)
    r = settle_single_trade(bars, 0, plan, costs, insufficient="last_close")
    return None if r is None else float(r.net_ret)


def attach_pnl(df: pd.DataFrame, max_workers: int = 8,
               price_basis: str = "raw") -> pd.DataFrame:
    """给每个样本算 hypothetical_pnl(并行)。"""
    from concurrent.futures import ProcessPoolExecutor, as_completed

    rows = [(row["code"], pd.Timestamp(row["signal_date"])) for _, row in df.iterrows()]
    results: dict[int, float] = {}

    if max_workers > 1 and len(rows) > 100:
        with ProcessPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(compute_hypothetical_pnl, code, sd, price_basis): i
                    for i, (code, sd) in enumerate(rows)}
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception:
                    results[i] = None
                done += 1
                if done % 500 == 0 or done == len(rows):
                    valid = sum(1 for v in results.values() if v is not None)
                    print(f"    [{done}/{len(rows)}] valid_pnl={valid}", flush=True)
    else:
        for i, (code, sd) in enumerate(rows, 1):
            results[i - 1] = compute_hypothetical_pnl(code, sd, price_basis)
            if i % 500 == 0 or i == len(rows):
                valid = sum(1 for v in results.values() if v is not None)
                print(f"    [{i}/{len(rows)}] valid_pnl={valid}", flush=True)

    pnl_series = pd.Series([results.get(i) for i in range(len(df))], index=df.index)
    df = df.copy()
    df["hypothetical_pnl"] = pnl_series
    return df
```

- [ ] **Step 4: 改 `scripts/recompute_pnl_labels.py`**

在 imports 后加：

```python
OUT_HFQ = OUT_DATA.with_name("d_signal_5d_features_vpnl_hfq.csv")
```

把 `main` 替换为：

```python
def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--price-basis", choices=["raw", "hfq"], default="raw")
    ap.add_argument("--src", default=str(OUT_DATA))
    ap.add_argument("--out", default="")
    # hfq 口径下缺因子的票会合法地少标签；差值必须显式申报并写进报告，
    # 不能靠悄悄放宽闸门（0 是默认，任何非零都要人眼确认是哪些票）。
    ap.add_argument("--expect-drop", type=int, default=0)
    args = ap.parse_args(argv)
    src = Path(args.src)
    out = Path(args.out) if args.out else (
        OUT_HFQ if args.price_basis == "hfq" else OUT_DATA)

    # round_trip 解析：默认解析器会让 ~100 个浮点单元格读偏 1 ULP
    df = pd.read_csv(src, float_precision="round_trip")
    if "hypothetical_pnl" not in df.columns:
        print(f"❌ 缺 hypothetical_pnl 列: {src}", file=sys.stderr)
        return 1
    have = {p.stem for p in daily_files()}
    expected = int(df.loc[df["code"].isin(have), "hypothetical_pnl"].notna().sum())
    features = df.drop(columns=["hypothetical_pnl"])
    renamed = attach_pnl(features, max_workers=8, price_basis=args.price_basis)
    valid = int(renamed["hypothetical_pnl"].notna().sum())
    if valid < expected - args.expect_drop:
        print(f"❌ 重算后非空 {valid} < 上一版有日线行的 {expected}"
              f"（已申报允许减少 {args.expect_drop}），拒写 {out}", file=sys.stderr)
        return 1
    if valid != expected:
        print(f"⚠ 非空 {valid} 比上一版 {expected} 少 {expected - valid}"
              f"（已申报 {args.expect_drop}）：确认都是缺因子的票", file=sys.stderr)
    tmp = out.with_name(out.name + ".tmp")
    try:
        renamed.to_csv(tmp, index=False)
        tmp.replace(out)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    print(f"RECOMPUTE_DONE rows={len(renamed)} valid={valid} basis={args.price_basis} out={out}")
    return 0
```

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_label_price_basis.py -q`
Expected: PASS（3 passed）

- [ ] **Step 6: 提交**

```bash
git add scripts/train_pnl_proxy.py scripts/recompute_pnl_labels.py tests/test_label_price_basis.py
git commit -m "feat(labels): hypothetical_pnl 支持 hfq 口径并分离产物"
```

---

### Task 6: stage1 宽表按口径装配

**Files:**
- Modify: `tdxfeed/stage1_wide_table.py:57`、`:184`
- Test: `tests/test_stage1_wide_table_basis.py`

**Interfaces:**
- Consumes: `tdxfeed.adjusted.frame` / `MIN_FACTOR_COV`、`tdxfeed.factors_polars.compute_window_features_extended_polars(..., frame_hfq=)`
- Produces: `build_wide_table(..., price_basis="raw")`；CLI `--price-basis {raw,hfq}`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_stage1_wide_table_basis.py`：

```python
"""宽表按口径取特征：hfq 宽表用 hfq 帧 + hfq 标签，缺因子的票整只跳过。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdxfeed import adjusted, stage1_wide_table as swt


def _daily(path: Path, closes, factor=1.0):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    df = pd.DataFrame({
        "date": [date(2020, 1, 2) + timedelta(days=i) for i in range(n)],
        "open": closes * 0.995,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "amount": np.full(n, 1.0e7),
    })
    df.to_parquet(path, index=False)
    return df


def test_hfq_wide_table_uses_hfq_features_and_labels(monkeypatch, tmp_path):
    d = tmp_path / "daily"; d.mkdir()
    f = tmp_path / "hfq"; f.mkdir()
    closes = [10.0] * 5 + [7.0, 7.1, 7.8, 8.0, 8.2]
    raw = _daily(d / "sh600000.parquet", closes)
    fac = pd.DataFrame({
        "code": "600000", "date": raw["date"],
        "hfq_factor": [1.0] * 5 + [10 / 7] * 5,
        "raw_close": closes, "hfq_close": [c * k for c, k in
                                           zip(closes, [1.0] * 5 + [10 / 7] * 5, strict=True)],
    })
    fac.to_parquet(f / "600000.parquet", index=False)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: d / "sh600000.parquet")
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: f / "600000.parquet")
    monkeypatch.setattr(swt, "DAILY", d)

    src = tmp_path / "src.csv"
    # code 必须带市场前缀：装配按 DAILY / f"{code}.parquet" 定位；
    # signal_date 必须带连字符：_str_to_date 按 s[:4]/s[5:7]/s[8:10] 切。
    pd.DataFrame({
        "code": ["sh600000"], "signal_date": ["2020-01-10"],
        "is_positive": [1], "hypothetical_pnl": [0.42],
        **{k: [0.0] for k in swt.FEAT26_ORDER},
    }).to_csv(src, index=False)
    out = tmp_path / "wide.parquet"
    swt.build_wide_table(src_csv=src, out_parquet=out, sample_codes=None,
                         verify_parity=True, price_basis="hfq")
    got = pd.read_parquet(out)
    assert len(got) == 1
    assert float(got["hypothetical_pnl"].iloc[0]) == 0.42
    # 除权日若按 raw 算，ret_5d 会是 −30% 附近；hfq 下应为正
    assert float(got["ret_5d"].iloc[0]) > 0


def test_missing_factor_drops_the_symbol(monkeypatch, tmp_path):
    d = tmp_path / "daily"; d.mkdir()
    f = tmp_path / "hfq"; f.mkdir()
    raw = _daily(d / "sh600000.parquet", [10.0] * 10)
    monkeypatch.setattr(adjusted, "daily_path", lambda s: d / "sh600000.parquet")
    monkeypatch.setattr(adjusted, "hfq_factor_path", lambda s: f / "600000.parquet")
    monkeypatch.setattr(swt, "DAILY", d)
    src = tmp_path / "src.csv"
    pd.DataFrame({"code": ["sh600000"], "signal_date": ["2020-01-10"],
                  "is_positive": [1], "hypothetical_pnl": [0.42],
                  **{k: [0.0] for k in swt.FEAT26_ORDER}}).to_csv(src, index=False)
    out = tmp_path / "wide.parquet"
    stats = swt.build_wide_table(src_csv=src, out_parquet=out, sample_codes=None,
                                 verify_parity=True, price_basis="hfq")
    assert stats["n_hfq_skip"] == 1
    assert len(pd.read_parquet(out)) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_stage1_wide_table_basis.py -q`
Expected: FAIL（`TypeError: build_wide_table() got an unexpected keyword argument 'price_basis'`）

- [ ] **Step 3: 改宽表装配**

`tdxfeed/stage1_wide_table.py` 顶部 import：

```python
from .adjusted import MIN_FACTOR_COV, frame as adj_frame
```

`build_wide_table` 签名改为：

```python
def build_wide_table(
    src_csv: Path = SRC_CSV,
    out_parquet: Path = OUT_PARQUET,
    sample_codes: int | None = None,
    verify_parity: bool = True,
    price_basis: str = "raw",
) -> dict:
```

函数体里把 `fp = DAILY / f"{code}.parquet"` 一段替换为：

```python
        fp = DAILY / f"{code}.parquet"
        if not fp.exists():
            n_missing_file += int((base.get_column("code") == code).sum())
            continue
        # raw 路径一字不动：仍按 fp 直读，不绕咽喉（保持与既有宽表逐值一致）
        df = _norm_date(pl.read_parquet(fp))
        frame_hfq = None
        if price_basis == "hfq":
            f_hfq = adj_frame(code, kind="hfq", strict=False)
            if f_hfq.attrs.get("factor_cov", 0.0) < MIN_FACTOR_COV:
                n_hfq_skip += int((base.get_column("code") == code).sum())
                continue
            frame_hfq = _norm_date(pl.from_pandas(f_hfq))
            # 同长还不够：两帧必须同日同序，否则 sig_idx 会错位
            if (frame_hfq.height != df.height
                    or frame_hfq["date"].to_list() != df["date"].to_list()):
                n_hfq_skip += int((base.get_column("code") == code).sum())
                continue
        dates = df["date"].to_list()
```

调用点改为：

```python
                f26 = compute_window_features_extended_polars(
                    df, code, sig_idx=anchor, frame_hfq=frame_hfq)
                ...
                cands = compute_candidate_features(
                    frame_hfq if frame_hfq is not None else df,
                    code, sig_idx=anchor, lookback=60)
```

`verify_parity` 在 hfq 口径下没有对照列（CSV 是 raw 特征），进入函数时关掉并打一行：

```python
    if price_basis != "raw" and verify_parity:
        log.info("price_basis=%s：CSV 里是 raw 特征，parity 检查自动关闭", price_basis)
        verify_parity = False
```

累计器加 `n_hfq_skip = 0`，返回 dict 加 `"n_hfq_skip": n_hfq_skip`。

`__main__` 加参数并透传：

```python
    ap.add_argument("--price-basis", choices=["raw", "hfq"], default="raw")
    ...
    build_wide_table(
        src_csv=Path(a.src), out_parquet=Path(a.out),
        sample_codes=a.sample, verify_parity=not a.no_parity,
        price_basis=a.price_basis,
    )
```

同时把模块 docstring 里「复权口径：day parquet 的 close 已审计为未复权（raw）……」那段改成：

```
复权口径：price_basis=raw（默认）时特征与标签都是 raw；price_basis=hfq 时
26 维按 features_5d 的逐维分派（13 hfq / 8 raw / 4 量纲 / 4 枚举）从 hfq 帧算，
标签来自 src CSV（即 d_signal_5d_features_vpnl_hfq.csv 的 hfq 标签）。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_stage1_wide_table_basis.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/stage1_wide_table.py tests/test_stage1_wide_table_basis.py
git commit -m "feat(stage1): 宽表支持 hfq 特征口径与标签来源分离"
```

---

### Task 7: predict 注册表带价格口径

**Files:**
- Modify: `scripts/predict.py:100-136`、`:345`、`:463-476`、`:733-760`
- Test: `tests/test_predict_price_basis.py`

**Interfaces:**
- Consumes: `tdxfeed.adjusted.frame` / `MIN_FACTOR_COV`
- Produces: `MODEL_REGISTRY[name] = (path, "cls"|"reg", "raw"|"hfq")`；`ACTIVE_PRICE_BASIS`；`compute_features(code, price_basis=None)`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_predict_price_basis.py`：

```python
"""注册表三元组：现有 10 条零变化；raw 口径不碰因子表；hfq 缺因子告警不 raise。"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import predict  # noqa: E402

OLD_ENTRIES = {
    "prod": "d_signal_5d_model.txt",
    "v9": "d_signal_5d_model_v9.txt",
    "v10_dart": "d_signal_5d_model_v10.txt",
    "vpnl": "d_signal_5d_model_vpnl.txt",
    "vpnl_asym": "d_signal_5d_model_vpnl_asym.txt",
    "vpnl_huber": "d_signal_5d_model_vpnl_huber.txt",
    "vpnl_huber_ind": "d_signal_5d_model_vpnl_huber_industry.txt",
    "vpnl_huber_mkt": "d_signal_5d_model_vpnl_huber_market.txt",
    "vpnl_huber_stage1": "d_signal_5d_model_vpnl_huber_stage1.txt",
    "daban": "d_signal_5d_model_daban.txt",
}


def test_registry_is_three_tuple_and_old_entries_are_raw():
    for name, fname in OLD_ENTRIES.items():
        path, kind, basis = predict.MODEL_REGISTRY[name]
        assert Path(path).name == fname, name
        assert kind in ("cls", "reg")
        assert basis == "raw", name
    for name in ("daban_hfq", "vpnl_huber_stage1_hfq"):
        assert predict.MODEL_REGISTRY[name][2] == "hfq"


def test_defaults_are_untouched():
    assert predict.DEFAULT_MODEL == "daban"
    assert predict.FALLBACK_MODEL == "vpnl_huber_stage1"
    assert predict.POSITION_MODE_MODEL == {"small": "daban", "full": "vpnl_huber_stage1"}


def _with_attrs(df: pd.DataFrame, **kw) -> pd.DataFrame:
    out = df.copy()
    out.attrs.update(kw)
    return out


def _wire_symbol(monkeypatch, tmp_path):
    """合成日线 + 三个环境隔离：日期必须近（MAX_STALE_DAYS=10）、
    特征模式锁 legacy（候选因子是 polars 专属）、ADJ_DIR 指向不存在目录。"""
    d = tmp_path / "daily"; d.mkdir()
    n = 60
    closes = np.linspace(10.0, 12.0, n)
    end = pd.Timestamp.now().normalize()
    df = pd.DataFrame({
        "date": [date((end - pd.Timedelta(days=n - 1 - i)).year,
                      (end - pd.Timedelta(days=n - 1 - i)).month,
                      (end - pd.Timedelta(days=n - 1 - i)).day) for i in range(n)],
        "open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
        "volume": np.full(n, 1000.0), "amount": np.full(n, 1.0e7),
    })
    p = d / "sh600000.parquet"
    df.to_parquet(p, index=False)
    monkeypatch.setattr(predict, "PARQUET_DIR", d)
    monkeypatch.setattr(predict, "ADJ_DIR", tmp_path / "no_adj")
    monkeypatch.setattr(predict, "ACTIVE_FEATURE_MODE", "legacy")
    monkeypatch.setattr(predict, "_enrich_market_features", lambda r, d_: r)
    monkeypatch.setattr(predict, "_enrich_industry_features", lambda r, d_: r)
    return df


def test_raw_basis_never_touches_the_factor_table(monkeypatch, tmp_path):
    _wire_symbol(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("raw 口径不该构建 hfq 帧")

    monkeypatch.setattr(predict.adjusted, "frame", _boom)
    assert predict.compute_features("600000", "raw") is not None


def test_hfq_basis_with_low_coverage_returns_none_instead_of_raising(monkeypatch, tmp_path):
    df = _wire_symbol(monkeypatch, tmp_path)
    monkeypatch.setattr(predict.adjusted, "frame",
                        lambda code, kind, strict: _with_attrs(df, factor_cov=0.5))
    assert predict.compute_features("600000", "hfq") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_predict_price_basis.py -q`
Expected: FAIL（`ValueError: too many values to unpack` 或注册表断言失败）

- [ ] **Step 3: 改注册表与解包**

`scripts/predict.py` 顶部 import 加：

```python
from tdxfeed import adjusted  # noqa: E402
```

`MODEL_REGISTRY` 每条加上第三项 `"raw"`，并新增两条：

```python
MODEL_REGISTRY = {
    "prod":           (MODEL_FILE, "cls", "raw"),
    "v9":             (ROOT / "data" / "double_up" / "d_signal_5d_model_v9.txt", "cls", "raw"),
    "v10_dart":       (ROOT / "data" / "double_up" / "d_signal_5d_model_v10.txt", "cls", "raw"),
    "vpnl":           (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl.txt", "reg", "raw"),
    "vpnl_asym":      (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_asym.txt", "reg", "raw"),
    "vpnl_huber":     (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber.txt", "reg", "raw"),
    "vpnl_huber_ind": (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_industry.txt", "reg", "raw"),
    "vpnl_huber_mkt": (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_market.txt", "reg", "raw"),
    "vpnl_huber_stage1": (ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1.txt", "reg", "raw"),
    "daban":          (ROOT / "data" / "double_up" / "d_signal_5d_model_daban.txt", "cls", "raw"),
    # hfq 口径候选（2026-10-30 双跑产物；不设为默认，切流是独立决定）
    "daban_hfq": (ROOT / "data" / "double_up" / "d_signal_5d_model_daban_hfq_20261030.txt",
                  "cls", "hfq"),
    "vpnl_huber_stage1_hfq": (
        ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1_hfq_20261030.txt",
        "reg", "hfq"),
}
```

五处解包点（`:132`、`:134`、`:734-735`、`:744-745`、`:759-760`）统一改成：

```python
ACTIVE_MODEL_PATH = MODEL_REGISTRY[DEFAULT_MODEL][0]
ACTIVE_IS_REG = MODEL_REGISTRY[DEFAULT_MODEL][1] == "reg"
ACTIVE_PRICE_BASIS = MODEL_REGISTRY[DEFAULT_MODEL][2]
```

```python
    ACTIVE_MODEL_PATH = MODEL_REGISTRY[model_name][0]
    ACTIVE_IS_REG = MODEL_REGISTRY[model_name][1] == "reg"
    ACTIVE_PRICE_BASIS = MODEL_REGISTRY[model_name][2]
```

三处 `global` 声明同步加 `ACTIVE_PRICE_BASIS`。

- [ ] **Step 4: 改 `compute_features` 与 worker 初始化**

签名与取帧段：

```python
def compute_features(code: str, price_basis: str | None = None) -> dict:
    price_basis = price_basis or ACTIVE_PRICE_BASIS
    ...
    frame_hfq = None
    if price_basis == "hfq":
        try:
            # 用 fp.stem 而不是裸 code：fp 可能是回退探测命中的文件，
            # 只有 stem 保证咽喉读到的正是 df_raw 那份日线（如 sh000001 指数）。
            frame_hfq = adjusted.frame(fp.stem, kind="hfq", strict=False)
        except Exception as e:
            print(f"[WARN] {code} hfq 帧构建失败: {e!r}", file=sys.stderr)
            return None
        if frame_hfq.attrs.get("factor_cov", 0.0) < adjusted.MIN_FACTOR_COV:
            print(f"[WARN] {code} 后复权覆盖 {frame_hfq.attrs.get('factor_cov')} 低于门禁，跳过",
                  file=sys.stderr)
            return None
        if len(frame_hfq) != len(df_raw) or not (
                pd.to_datetime(frame_hfq["date"]).reset_index(drop=True)
                .equals(pd.to_datetime(df_raw["date"]).reset_index(drop=True))):
            print(f"[WARN] {code} hfq 帧与日线不同日，跳过", file=sys.stderr)
            return None
```

特征与候选因子调用改为：

```python
    feats = compute_window_features_extended(df_raw, code, frame_hfq=frame_hfq)
    ...
    if ACTIVE_FEATURE_MODE == "polars":
        cand = _compute_candidate_polars(
            frame_hfq if frame_hfq is not None else df_raw, code)
```

worker 初始化改为两参：

```python
def _init_feature_mode(mode: str, basis: str) -> None:
    """在每个 worker 进程内显式设置特征管线模式与价格口径（不依赖 fork 继承）。"""
    global ACTIVE_FEATURE_MODE, ACTIVE_PRICE_BASIS
    ACTIVE_FEATURE_MODE = mode
    ACTIVE_PRICE_BASIS = basis
```

以及：

```python
    with ProcessPoolExecutor(max_workers=max_workers,
                             initializer=_init_feature_mode,
                             initargs=(ACTIVE_FEATURE_MODE, ACTIVE_PRICE_BASIS)) as executor:
```

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_predict_price_basis.py -q`
Expected: PASS（4 passed）

- [ ] **Step 6: 提交**

```bash
git add scripts/predict.py tests/test_predict_price_basis.py
git commit -m "feat(predict): 注册表加价格口径字段，新增两条 hfq 条目（不改默认）"
```

---

### Task 8: 因子表单一权威扫描

**Files:**
- Test: `tests/test_single_factor_authority.py`

**Interfaces:**
- Consumes: 纯源码 AST，无数据依赖
- Produces: `scan(root) -> dict[str, list[int]]`；`FORBIDDEN`、`GUARDED` 常量

- [ ] **Step 1: 写守卫**

创建 `tests/test_single_factor_authority.py`：

```python
"""价格派生只经 tdxfeed/adjusted.py：八个模块不得引用因子表。

规则（AST，避开注释与 docstring 误伤）：
  R1 标识符 / 属性名命中 FORBIDDEN；
  R2 import 的模块名命中；
  R3 非 docstring 的字符串常量命中。
"""
from __future__ import annotations

import ast
from pathlib import Path

NEEDS_DATA = False

ROOT = Path(__file__).resolve().parents[1]
GUARDED = [
    "tdxfeed/features_5d.py",
    "tdxfeed/factors_candidates.py",
    "tdxfeed/factors_polars.py",
    "scripts/train_daban_model.py",
    "scripts/find_d_signal_5d.py",
    "scripts/feature_engineering.py",
    "scripts/train_pnl_proxy.py",
    "tdxfeed/stage1_wide_table.py",
]
FORBIDDEN = {
    "adj_factor", "adj_factor_dir", "adj_factor_path",
    "hfq_factor", "hfq_factor_dir", "hfq_factor_path",
}


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_nodes(tree)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN:
            lines.add(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            lines.add(node.lineno)
        elif isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] in FORBIDDEN for alias in node.names):
                lines.add(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[-1] in FORBIDDEN:
                lines.add(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs and node.value in FORBIDDEN:
                lines.add(node.lineno)
    return sorted(lines)


def scan(root: Path = ROOT) -> dict[str, list[int]]:
    bad = {}
    for rel in GUARDED:
        p = root / rel
        found = violations(p)
        if found:
            bad[rel] = found
    return bad


def test_guarded_modules_exist():
    for rel in GUARDED:
        assert (ROOT / rel).exists(), rel


def test_no_module_reads_the_factor_tables_directly():
    bad = scan()
    assert not bad, f"这些模块绕过 adjusted.py 直接引用因子表: {bad}"


def test_guard_catches_direct_reference(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        "from tdxfeed.paths import hfq_factor_path\n"
        "A = 'hfq_factor'\n",
        encoding="utf-8",
    )
    assert violations(p) == [1, 2]


def test_guard_ignores_docstring_mention(tmp_path):
    p = tmp_path / "m.py"
    p.write_text(
        '"""说明文字：不读 adj_factor / hfq_factor。"""\n'
        "B = 1\n",
        encoding="utf-8",
    )
    assert violations(p) == []
```

- [ ] **Step 2: 跑守卫**

Run: `venv/bin/pytest tests/test_single_factor_authority.py -q`
Expected: PASS（4 passed）。若红，说明目标模块还有直读因子的代码——改代码，不改名单。

- [ ] **Step 3: 提交**

```bash
git add tests/test_single_factor_authority.py
git commit -m "test(guards): 因子表单一权威扫描（八个模块）"
```

---

### Task 9: 全量回填与覆盖验收（ops，无提交）

**Files:**
- 产物：`data/hfq_factor/*.parquet`、`data/hfq_factor.bak-20261030/`、`data/.state/hfq_backfill.json`

**Interfaces:**
- Consumes: Task 1 的回填器、Task 2 的白名单守卫
- Produces: 覆盖验收输出 `data/hfq_backfill_coverage_20261030.json`

- [ ] **Step 1: 确认时窗与同步状态**

Run: `date; systemctl --user list-timers | grep -i tdx; systemctl list-timers | grep -i tdx-sync`
Expected: 当前时间 ≥ 18:00；`tdx-sync.timer` 今日已触发（`LAST` 是今天）。若尚未触发，等到 16:30 那轮结束后再跑。

- [ ] **Step 2: 全量回填（后台，约 2 小时）**

Run: `venv/bin/python scripts/backfill_hfq_history.py --workers 3 2>&1 | tee logs/hfq_backfill_20261030.log`
Expected: 每 100 只一行进度；结束 `完成：ok≈5221 not_in_source≈0 fail≈0`，退出码 0。

- [ ] **Step 3: 回填后白名单守卫**

Run: `venv/bin/pytest tests/test_hfq_factor_monotone.py -q`
Expected: PASS。若出现新下跳，先停下：`git status` 看是否有他人会话在写 `data/`，再决定是源修订还是写坏。

- [ ] **Step 4: 覆盖验收（走咽喉 attrs，不自己重算门禁）**

审计脚本必须用 `adjusted.frame` 的 attrs，而不是自己 `pd.read_parquet` 因子表——两套判定一分叉，`mismatch` 这种新门禁就漏了。

Run:

```bash
venv/bin/python - <<'PY' | tee data/hfq_backfill_coverage_20261030.json
import json
import sys

sys.path.insert(0, "tests")

from tdxfeed.adjusted import frame as adj_frame
from tdxfeed.paths import daily_files
from test_hfq_factor_monotone import scan

rows = []
for fp in daily_files():
    symbol = fp.stem
    a = adj_frame(symbol, kind="hfq", strict=False).attrs
    if a.get("factor_mismatch"):
        state = "mismatch"                      # 同码双文件：指数读到个股因子
    elif a.get("factor_first") is None:
        state = "no-factor"                     # 表里根本没有这只票
    elif (a.get("factor_head_lag") or 0) > 0:
        state = "head-lag"                      # 因子首日晚于日线首日
    elif (a.get("factor_tail_lag") or 0) > 1:
        state = "tail-lag"
    else:
        state = "ok"
    rows.append({"symbol": symbol, "state": state, "cov": a.get("factor_cov"),
                 "head_lag": a.get("factor_head_lag"),
                 "tail_lag": a.get("factor_tail_lag"),
                 "raw_dev": a.get("factor_raw_dev"),
                 "factor_first": a.get("factor_first")})

by_state = {s: sum(1 for r in rows if r["state"] == s) for s in {r["state"] for r in rows}}
pre = [{"code": c, "date": d, "ret": r}
       for c, d, r in scan(since=None) if d < "2020-01-01"]
print(json.dumps({"total": len(rows), "by_state": by_state,
                  "not_ok": [r for r in rows if r["state"] != "ok"],
                  "pre2020_jumps": pre}, ensure_ascii=False, indent=1))
PY
```

Expected（按本机实测基线）：

| state | 预期 | 含义 |
|---|---:|---|
| `ok` | **5221** | 回填到上市日、因子末日只允许落后 1 个交易日 |
| `mismatch` | **5** | `sh000001/000016/000688/000852/000905` 读到同码个股因子（`raw_dev` ≈ 99%） |
| `no-factor` | **10** | spec §1 清单：3 指数 + sh600001 + 6 只 2025–26 新上市 |
| `head-lag` / `tail-lag` | **0** | 任何一只都说明回填没到位或源回短了 |

`pre2020_jumps` 是**记录项**，不设预期值——它是回填新引入历史段的第一次实测，写进报告即可（白名单只对 2020+ 断言，见落地裁决 4）。若 `head-lag` 成片出现，说明本地日线比 baostock 更深，停下来评估（这会让 strict 模式把那些票排除出 hfq 训练集）。

- [ ] **Step 5: 记录结果（不提交）**

把 Step 4 的 `by_state` 与 `not_ok` 抄进 Task 11 的报告。`data/` 与 `logs/` 不进 git。

---

### Task 10: daban 双跑（先改代码提交，再跑两组）

**Files:**
- Modify: `scripts/train_daban_model.py:79-94`、`:208`
- 产物：`data/double_up/d_signal_5d_model_daban_hfq_20261030.txt`、`daban_train_report_hfq_20261030.md`、`daban_hfq_skipped_20261030.json`、`d_signal_5d_model_daban_raw_20261030.txt`、`daban_train_report_raw_20261030.md`

**Interfaces:**
- Consumes: Task 4 的分派、Task 9 的回填结果
- Produces: 两组 OOS AUC 与特征重要性

**关键事实**：`iter_stock_files` 返回的 `code` 是**裸 6 位**，而 `fp` 带市场前缀（`sh000001.parquet` 与 `sz000001.parquet` 会被当作同一个 `code="000001"` 各跑一遍，这是既有行为，本轮不改）。所以咽喉必须喂 `fp.stem`，排除/跳过清单也必须按 `fp.stem` 记账——按裸 `code` 排除会连带排掉同码的另一份文件。

- [ ] **Step 1: 给 `scripts/train_daban_model.py` 加口径参数**

`build_dataset` 签名追加：

```python
def build_dataset(
    limit: int = 0,
    exclude_open_limit: bool = False,
    extra_lookup=None,
    industry_map: dict | None = None,
    sample_space: str = "all",
    price_basis: str = "raw",
    exclude_symbols: set[str] | None = None,
    hfq_skipped_out: Path | None = None,
) -> pd.DataFrame:
```

循环体改成（相对现有结构的最小 diff）：

```python
    hfq_skipped: list[str] = []
    for i, (code, fp) in enumerate(files):
        symbol = fp.stem                     # sh600000：排除与跳过都按它记账
        if exclude_symbols and symbol in exclude_symbols:
            continue
        try:
            df = pd.read_parquet(fp)
        except Exception:
            continue
        if len(df) < 30:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        frame_hfq = None
        if price_basis == "hfq":
            f_hfq = adjusted.frame(symbol, kind="hfq", strict=False)
            if f_hfq.attrs.get("factor_cov", 0.0) < adjusted.MIN_FACTOR_COV:
                hfq_skipped.append(symbol)       # 缺因子 / 同码错配 → 整只跳过
                continue
            f_hfq = f_hfq.sort_values("date").reset_index(drop=True)
            if len(f_hfq) != len(df) or not (
                    pd.to_datetime(f_hfq["date"]).reset_index(drop=True)
                    .equals(pd.to_datetime(df["date"]))):
                hfq_skipped.append(symbol)       # 两帧不同日就不能用同一 sig_idx
                continue
            frame_hfq = f_hfq
        limit_val = get_limit(code)
```

（这段必须在 `rng` 抽样**之前**——当前脚本里 `rng = random.Random(RANDOM_SEED)` 在第 90 行、抽样在第 137 行，跳过放在读盘后即可，保证两组 RNG 流一致。）

特征调用改为：

```python
            feat = compute_window_features_extended(df, code, sig_idx=sig_idx,
                                                    frame_hfq=frame_hfq)
```

函数结尾写跳过清单：

```python
    if hfq_skipped_out is not None:
        hfq_skipped_out.write_text(
            json.dumps({"symbols": sorted(set(hfq_skipped))}), encoding="utf-8")
```

顶部 import：

```python
import json
from tdxfeed import adjusted
```

`main` 加参数并传入：

```python
    ap.add_argument("--price-basis", choices=["raw", "hfq"], default="raw")
    ap.add_argument("--exclude-symbols-file", default="",
                    help='JSON {"symbols": [...]}；这些 symbol 在抽样前整只跳过（双跑对齐）')
    ap.add_argument("--hfq-skipped-out", default="",
                    help='hfq 模式下覆盖率不足被跳过的 symbol 写到 {"symbols": [...]}')
    ...
    exclude_symbols = set()
    if args.exclude_symbols_file:
        exclude_symbols = set(json.loads(
            Path(args.exclude_symbols_file).read_text(encoding="utf-8"))["symbols"])
    df = build_dataset(
        args.limit,
        exclude_open_limit=args.exclude_open_limit,
        extra_lookup=extra_lookup,
        industry_map=industry_map,
        sample_space=args.sample_space,
        price_basis=args.price_basis,
        exclude_symbols=exclude_symbols,
        hfq_skipped_out=(Path(args.hfq_skipped_out) if args.hfq_skipped_out else None),
    )
```

报告头部加一行口径：

```python
             f"- 价格口径: {basis}",
```

`write_report(r, report_out)` 调用处传 `basis=args.price_basis`（函数签名加 `basis: str = "raw"`）。

- [ ] **Step 2: 跑机制测试（改完先验证现有测试没红）**

Run: `venv/bin/pytest tests/test_features_5d.py tests/test_features_parity.py tests/test_stage1_factors_parity.py tests/test_features_5d_price_basis.py -q`
Expected: PASS

- [ ] **Step 3: 先提交代码改动**

```bash
git add scripts/train_daban_model.py
git commit -m "feat(daban): 训练支持 price_basis，双跑按 symbol 对齐样本集"
```

（先提交再长跑：两轮各 40 分钟，跑的过程中工作树必须是干净可复现的。）

- [ ] **Step 4: hfq 组先跑（后台，约 40 分钟）**

Run:

```bash
venv/bin/python scripts/train_daban_model.py --price-basis hfq --tag hfq_20261030 \
  --hfq-skipped-out data/double_up/daban_hfq_skipped_20261030.json \
  2>&1 | tee logs/daban_train_hfq_20261030.log
```

Expected: `完成: 正样本 N, 负样本 M`；`模型保存: ..._daban_hfq_20261030.txt`；报告里 `OOS AUC`。

- [ ] **Step 5: raw 对照组跑（后台，约 40 分钟，样本集对齐）**

Run:

```bash
venv/bin/python scripts/train_daban_model.py --price-basis raw --tag raw_20261030 \
  --exclude-symbols-file data/double_up/daban_hfq_skipped_20261030.json \
  2>&1 | tee logs/daban_train_raw_20261030.log
```

Expected: `正样本/负样本` 与 hfq 组逐字相同（样本集对齐的证据）；`..._daban_raw_20261030.txt`。`完成:` 那行的两个数必须逐字一致——不一致说明排除集没生效（比如漏了同码双文件）。

- [ ] **Step 6: 记录三个数（不提交）**

从两份日志/报告抄出：raw 组 OOS AUC（对照基线 0.7478）、hfq 组 OOS AUC、13 个 hfq 维的 gain 排名变化。同时确认现役模型未被覆盖：

Run: `ls -l data/double_up/d_signal_5d_model_daban.txt`
Expected: mtime 早于今天（没被覆盖）。

（模型、报告、日志、跳过清单都在 `data/` 与 `logs/`，不入库。）

---

### Task 11: stage1 双建双跑、报告与文档收口

**Files:**
- 产物：`data/double_up/d_signal_5d_features_vpnl_hfq.csv`、`stage1_wide_table_hfq.parquet`、`d_signal_5d_model_vpnl_huber_stage1_hfq_20261030.txt`
- Create: `docs/research/2026-10-30-hfq-read-authority-dual-run.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: Task 5 的标签产物、Task 6 的宽表、Task 9/10 的数字
- Produces: 报告与 AGENTS.md 的一行口径

- [ ] **Step 1: 生成 hfq 标签 CSV**

先备份现役 CSV：

```bash
cp -a data/double_up/d_signal_5d_features_vpnl.csv \
      data/double_up/d_signal_5d_features_vpnl.bak-20261030.csv
```

Run: `venv/bin/python scripts/recompute_pnl_labels.py --price-basis hfq`
Expected: `RECOMPUTE_DONE rows=13064 valid=... basis=hfq out=.../d_signal_5d_features_vpnl_hfq.csv`

写前闸门默认按上一版 raw 非空数（9981）卡：hfq 下缺因子的票会合法变少，闸门会拒绝写盘。**先不加参数跑一次**，看被判定的缺口；确认差额正好是缺因子样本（对照 Task 9 的 `not_ok` 清单）后，用 `--expect-drop <N>` 重跑并在报告里记下这个 N 与名单。差额对不上就停——那是代码 bug，不是数据缺口。

- [ ] **Step 2: 备份并重建 raw 宽表、构建 hfq 宽表**

```bash
cp -a data/double_up/stage1_wide_table.parquet \
      data/double_up/stage1_wide_table.bak-20261030.parquet
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 \
  --src data/double_up/d_signal_5d_features_vpnl.csv \
  --out data/double_up/stage1_wide_table.parquet
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 --price-basis hfq \
  --src data/double_up/d_signal_5d_features_vpnl_hfq.csv \
  --out data/double_up/stage1_wide_table_hfq.parquet
```

Expected: raw 宽表 `rows=9981` 且 `parity_max` 与备份时同量级（<1e-6）；hfq 宽表 `rows=9981-n_hfq_skip`，`n_hfq_skip` 记进报告。

- [ ] **Step 3: 双跑 stage1**

```bash
venv/bin/python scripts/stage1_retrain.py \
  --wide-table data/double_up/stage1_wide_table.parquet \
  --out-model data/double_up/d_signal_5d_model_vpnl_huber_stage1_raw_20261030.txt \
  --frozen data/double_up/stage1_selected_features_raw_20261030.json
venv/bin/python scripts/stage1_retrain.py \
  --wide-table data/double_up/stage1_wide_table_hfq.parquet \
  --out-model data/double_up/d_signal_5d_model_vpnl_huber_stage1_hfq_20261030.txt \
  --frozen data/double_up/stage1_selected_features_hfq_20261030.json
```

`--frozen` 是**输出**契约（`write_frozen_contract`），默认指向生产在役文件 `data/double_up/stage1_selected_features.json`；两次都必须显式改道，否则两轮会先后覆盖同一份生产契约。

Expected: 两份模型 + 两份 fold 报告；现役 `d_signal_5d_model_vpnl_huber_stage1.txt` 与 `stage1_selected_features.json` 的 mtime 不变。对照基线 fold1 **0.3475 / 0.2045**。

- [ ] **Step 4: 写双跑报告**

创建 `docs/research/2026-10-30-hfq-read-authority-dual-run.md`，结构固定：

```markdown
# hfq 读侧权威双跑（2026-10-30）

## 0. 结论一句话
（口径效应方向与量级；是否建议后续切流——本轮不切）

## 1. 数据前提
- 回填覆盖：`by_state` 与 `not_ok`（来自 data/hfq_backfill_coverage_20261030.json）
- 下跳白名单：11 只 / 12 次，守卫 `tests/test_hfq_factor_monotone.py` 绿

## 2. daban（干净消融）
| 组 | OOS AUC | Top-5 命中 | Top-20 命中 | 样本 |
|---|---:|---:|---:|---:|
| raw（对照） |  |  |  |  |
| hfq（实验） |  |  |  |  |

- 非口径漂移 = raw AUC − 0.7478 =
- 口径效应 = hfq AUC − raw AUC =
- 13 个 hfq 维的 gain 排名变化（列前 5 升/降）
- 样本集对齐证据：两组正/负样本数逐字相同

## 3. stage1（同 9981 样本、同 21 维契约）
| 组 | fold1 AUC | fold1 MSE/评分 | 样本 |
|---|---:|---:|---:|
| raw（对照） |  |  |  |
| hfq（实验） |  |  |  |

- 非口径漂移 = raw − 0.3475 / 0.2045 =
- 口径效应 = hfq − raw =

## 4. 影响面
- 12 个异常日命中的样本数（口径见下，数字来自 Step 4 的 heredoc）
- 缺因子被排除的代码清单（daban 的 skipped JSON + stage1 的 n_hfq_skip）

## 5. 已知残余
- 样本宇宙仍用 qfq 翻倍筛选（49 个伪事件），本轮不动；后续：用 hfq 重筛宇宙需新一轮对照
- 12 次因子下跳不修，靠守卫钉住

## 6. 产物清单
（模型、CSV、宽表、日志的绝对路径与 mtime）
```

数字一律来自 Task 9/10/11 的实测输出，不引用旧报告。报告 §4 的 12 个异常日命中数用下面这段算（别手估）：

```bash
venv/bin/python - <<'PY'
import json
import sys

import pandas as pd

sys.path.insert(0, "tests")

from tdxfeed.paths import daily_files, exchange_prefix
from test_hfq_factor_monotone import KNOWN_JUMPS

files = {p.stem: p for p in daily_files()}
csv = pd.read_csv("data/double_up/d_signal_5d_features_vpnl.csv",
                  usecols=["code", "signal_date"])
csv["d"] = pd.to_datetime(csv["signal_date"])
out = []
for (code6, day), ret in sorted(KNOWN_JUMPS.items()):
    symbol = f"{exchange_prefix(code6)}{code6}"
    fp = files.get(symbol)
    if fp is None:
        out.append({"code": code6, "date": day, "state": "no-daily-file"})
        continue
    dates = pd.to_datetime(pd.read_parquet(fp, columns=["date"])["date"]).reset_index(drop=True)
    hit = dates.index[dates == pd.Timestamp(day)]
    if not len(hit):
        out.append({"code": code6, "date": day, "state": "no-bar(停牌)"})
        continue
    j = int(hit[0])
    sub = csv[csv["code"].astype(str).str[-6:] == code6]
    # 特征窗口：anchor=sig_idx-1，5 日 = [sig-5, sig-1] → 含 J 当且仅当 sig ∈ [J, J+4]
    n_feat = int(sub["d"].between(dates[j], dates[min(j + 4, len(dates) - 1)]).sum())
    # 持有窗口：入场 sig_idx+1，持有 10 根 → 含 J 当且仅当 sig ∈ [J-10, J-1]
    n_hold = int(sub["d"].between(dates[max(j - 10, 0)], dates[max(j - 1, 0)]).sum())
    out.append({"code": code6, "date": day, "ret": ret,
                "n_feature_window": n_feat, "n_hold_window": n_hold})
print(json.dumps(out, ensure_ascii=False, indent=1))
PY
```

窗口口径写进报告：特征窗口按 `anchor = sig_idx-1` 的 5 日窗口；持有窗口按入场 `sig_idx+1` 起 10 根。两者都按该事件票自己的交易日索引数，停牌日不计。

- [ ] **Step 5: 更新 AGENTS.md**

在「数据路径与表口径」段 `data/hfq_factor` 那一条之后补一行：

```
- **价格特征与收益率标签的读侧权威是 hfq（2026-10-30 起）**：13 个价格比率维与
  `hypothetical_pnl` 从 `data/hfq_factor` 取，涨跌停/封板/委托价仍对 raw；
  唯一入口 `tdxfeed/adjusted.py::frame`，业务模块不得直读因子表（守卫
  `tests/test_single_factor_authority.py`）。12 次因子下跳不修，白名单守卫
  `tests/test_hfq_factor_monotone.py`。样本宇宙的翻倍筛选仍用 qfq，是已知残余。
```

- [ ] **Step 6: 总门禁**

```bash
venv/bin/pytest tests/ -q
venv/bin/ruff check tdxfeed/adjusted.py scripts/backfill_hfq_history.py scripts/train_daban_model.py scripts/predict.py
make lint-all
```

Expected: 全绿；`make lint-all` 的阻断项（E9,F63,F7,F82）零命中。若红，先判断是否他人会话的改动。

- [ ] **Step 7: 提交**

```bash
git add docs/research/2026-10-30-hfq-read-authority-dual-run.md AGENTS.md
git commit -m "docs(research): hfq 读侧权威双跑报告与口径收口"
```

（`scripts/train_daban_model.py` 已在 Task 10 Step 3 单独提交；`docs/superpowers/specs|plans/` 的这份规格与计划在开工前已入库，执行中若按实测改了计划文字，把它们一起加进这次提交。`data/`、`logs/` 与模型产物不入库。）

---

## 执行顺序与时窗

| 时段 | 动作 |
|---|---|
| 盘中 | Task 1–8（纯代码与离线测试），Task 10 Step 1–3（改代码 + 机制测试 + 提交）；Task 9 Step 1 只看时窗与同步状态 |
| 18:00 后 | Task 9 Step 2 全量回填（后台约 2h，3 进程）；等待期间不动 `data/hfq_factor` 的其他写者 |
| 回填完成 | Task 9 Step 3–5 验收（白名单守卫 + 覆盖审计）；Task 10 Step 4–6 双跑（两个 ~40min 后台，hfq 先、raw 后） |
| 双跑完成 | Task 11 标签 CSV → 宽表双建 → stage1 双跑 → 报告 → AGENTS.md → 总门禁 |

回填与双跑都读写 `data/hfq_factor`，两者不能并行；双跑之间 raw 组必须等 hfq 组写完跳过清单再起（Step 5 的 `--exclude-symbols-file` 就是这份清单）。
