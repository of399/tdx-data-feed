# 批次 A：数据停更收口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让三条"悄悄停更"的数据链（复权因子、派生表、同步清单）恢复自动刷新并带上新鲜度守卫，同时给两个生产入口补上失败退出码。

**Architecture:** 不新增调度源。`tdx-sync.service` 仍是唯一的每日同步入口，在它后面挂两行 `ExecStartPost`（派生表增量、复权因子增量），失败用 `-` 前缀隔离、由 canary 的新鲜度检查兜底。复权因子需要一个新的增量更新模块——现存的 `scripts/download_adj_factor_b.py` 命中已存在文件就跳过（[download_adj_factor_b.py:50-51](../../../scripts/download_adj_factor_b.py#L50)），从不更新，这正是停更 5 周的根因。

**Tech Stack:** Python 3.14（仓库自带 `venv/bin/python`）、pandas、pyarrow、baostock、systemd（system 作用域）、pytest。

**Spec:** 本批次没有新规格；需求来自 2026-10-29 审计，与现有规格的遗留项对应：`docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md` §12 遗留项 2（manifest last_date）、遗留项 3（derived 无调度）。执行本计划时两份文档都要读。

## Global Constraints

- 所有命令在仓库根 `/home/jiuben/tdx-data-feed` 执行；Python 一律 `venv/bin/python`、`venv/bin/pytest`。
- **共享工作树**：`git add` / `git commit` 必须带显式路径，禁止 `-A` 或 `.`；发现陌生脏文件先判断是否他人的改动，不顺手修。
- `data/`、`logs/`、`models/` 在 gitignore 里，git 救不回来；就地改写数据前先备份（本计划里 T3 会改 5221 个 adj 文件，先 `cp -al` 快照）。
- 表路径只从 `tdxfeed/paths.py` 取；不得出现 `data/parquet/...` 字面量（守卫 `tests/test_no_hardcoded_data_paths.py`）。
- 新增只读源码的守卫测试必须在模块顶部零缩进写 `NEEDS_DATA = False`，否则 CI 无数据时整模块 skip（`conftest.py` 约定）。
- 改 `systemd/system/` 下单元后必须同步部署到 `/etc/systemd/system/` 并 `daemon-reload`，否则 `tests/test_systemd_system_units_mirror.py` 会红；部署需要 root（见 T4）。
- CI 阈值是导出量：新增测试后必须重测「无数据演练 passed」基数再决定是否改 `.github/workflows/tests.yml` 的 `-ge`，不许只调阈值。
- 提交信息用中文，格式 `fix(scope): ...` / `feat(scope): ...` / `docs(scope): ...`。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `tdxfeed/full_sync.py` | 改 | manifest 的 last_date 用 bar 的日期，不用对象 repr |
| `tdxfeed/precompute/indicators.py` | 改 | 全市场派生表构建失败时退出非零 |
| `tdxfeed/incremental.py` | 改 | 批量增量失败时退出非零 |
| `tdxfeed/adj_factor.py` | 新建 | 复权因子增量更新（baostock 两条序列 → 合并 → 原子替换） |
| `tdxfeed/freshness.py` | 改 | 新增 `sample_latest_date()` 与 `stale_threshold()`，供 canary 复用 |
| `scripts/canary_health_check.py` | 改 | `check_data_files` 增加 adj_factor / derived 的末日期检查 |
| `systemd/system/tdx-sync.service` | 改 | 两行 `ExecStartPost=-`（派生表、复权因子） |
| `Makefile` | 改 | `data-adj` 改调增量模块；`timer-install` 无需改（没有新单元） |
| `AGENTS.md`、`docs/WORKFLOW.md` | 改 | 记录新的刷新路径与 adj 日期列类型 |
| `tests/test_full_sync_manifest.py` | 新建 | manifest 日期格式 |
| `tests/test_entry_exit_codes.py` | 新建 | indicators / incremental 退出码 |
| `tests/test_adj_factor_update.py` | 新建 | 合并逻辑与单只更新（不联网） |
| `tests/test_sync_post_jobs.py` | 新建 | 单元里的 ExecStartPost 与模块入口一致 |
| `tests/test_canary_table_freshness.py` | 新建 | canary 对 adj/derived 的陈旧判定 |

---

### Task 1: manifest 的 last_date 落成 8 位数字字符串 YYYYMMDD（`str`，不是 int）

**Files:**
- Modify: `tdxfeed/full_sync.py:231`
- Test: `tests/test_full_sync_manifest.py`（新建）

**Interfaces:**
- Produces: `tdxfeed.full_sync.manifest_last(bars) -> str`，返回 `YYYYMMDD`；空 bars 抛 `ValueError`。
- Consumes: `Writers._date_int`（[writers.py:81](../../../tdxfeed/writers.py#L81)），指数分支已在用同一写法（`full_sync.py:134`）。

**背景（实测）：** `data/.state/manifest.json` 共 5235 条、5227 条的 `last_date` 是
`'[datetime.datetime(2026, 10, 28, 0, 0), 9.8, 9.93, ...]'`。根因是 `bars` 是
list-of-list（`SinaSource.fetch_bars` 里 `bars.append([...])`），`str(bars[-1])` 取到整行 repr。
`normalize_date` 返回 `datetime`，所以 `bars[-1][0]` 就是日期。

- [ ] **Step 1: 写失败测试**

```python
"""manifest.last_date 必须是 8 位数字字符串 YYYYMMDD（不是 int）：bars 是 list-of-list，不能取整行 repr。"""
from __future__ import annotations

from datetime import datetime

import pytest

from tdxfeed.full_sync import manifest_last


def test_manifest_last_uses_bar_date():
    bars = [[datetime(2026, 10, 28, 0, 0), 9.8, 9.93, 9.73, 9.85, 647276.35, 0.0]]
    assert manifest_last(bars) == "20261028"


def test_manifest_last_is_digits_only():
    bars = [[datetime(2026, 1, 5, 0, 0), 1, 1, 1, 1, 1, 1]]
    assert manifest_last(bars) == "20260105"


def test_manifest_last_rejects_empty():
    with pytest.raises(ValueError):
        manifest_last([])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_full_sync_manifest.py -q`
Expected: FAIL — `ImportError: cannot import name 'manifest_last'`

- [ ] **Step 3: 实现**

在 `tdxfeed/full_sync.py` 的 `sync_exit_code` 之前插入：

```python
def manifest_last(bars) -> str:
    """manifest 的 last_date：8 位数字字符串 YYYYMMDD（`str`，不是 int）。

    bars 是 list-of-list（SinaSource.fetch_bars 的结构），首元素是 normalize_date
    返回的 datetime。不要写 str(bars[-1])——那是整行 repr，5235 条里会坏 5227 条。
    """
    if not bars:
        raise ValueError("manifest_last 需要非空 bars")
    return str(Writers._date_int(bars[-1][0]))
```

把 `full_sync.py:231` 的

```python
                last = str(bars[-1]).replace("-", "")
```

改成

```python
                last = manifest_last(bars)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_full_sync_manifest.py -q`
Expected: 3 passed（执行时补了两条守卫：两根 bars 取更晚、调用点计数 == 2，实际 5 passed）

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/full_sync.py tests/test_full_sync_manifest.py
git commit -m "fix(full_sync): manifest 的 last_date 落 bar 日期而不是整行 repr"
```

> 已有的 5227 条坏记录不需要迁移：每次同步都会对每个标的 `state.update()` 覆写，
> 下一轮自然自愈。只有长期失败/跳过的标的会保留旧值，这本身就是它们该有的信号。

---

### Task 2: 生产入口失败退出码（indicators / incremental）

**Files:**
- Modify: `tdxfeed/precompute/indicators.py:218-258`
- Modify: `tdxfeed/incremental.py`（main 结尾）
- Test: `tests/test_entry_exit_codes.py`（新建）

**Interfaces:**
- Produces: 两者 `main()` 返回 `int`，`__main__` 用 `sys.exit(main())`；有失败返回 1，全成功返回 0。
- 注意：`tdxfeed/full_sync.py` 已有 `sync_exit_code`（[full_sync.py:185](../../../tdxfeed/full_sync.py#L185)，坏率 >20% 退 1），本任务不重复改它。

**背景（实测）：** `indicators.py:218` 的 main 统计了 `fail` 但结尾只 `print`，
`__main__` 直接 `main()`，所以派生表缺片时 timer 仍记 success。`incremental.py` 同理。

- [ ] **Step 1: 写失败测试**

```python
"""生产入口失败必须退出非零，否则 systemd / canary 看到的是绿的。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tdxfeed import incremental
from tdxfeed.precompute import indicators


def _patch_argv(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)


def test_indicators_returns_1_when_any_fail(monkeypatch, tmp_path):
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [Path("a.parquet")])

    def boom(code, force=False):
        raise RuntimeError("read failed")

    monkeypatch.setattr(indicators, "process_one", boom)
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 1


def test_indicators_returns_0_when_all_ok(monkeypatch, tmp_path):
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [Path("a.parquet")])
    monkeypatch.setattr(indicators, "process_one", lambda code, force=False: tmp_path / "x.parquet")
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 0


def test_incremental_returns_1_when_any_fail(monkeypatch):
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])

    def boom(sym):
        raise RuntimeError("source down")

    monkeypatch.setattr(incremental, "incremental_update", boom)
    assert incremental.main() == 1


def test_incremental_returns_0_when_all_ok(monkeypatch):
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"status": "ok", "added": 1},
    )
    assert incremental.main() == 0


def test_incremental_counts_fetch_failed_as_failure(monkeypatch):
    """fetch_failed 是行情源全挂，不是「无需更新」——必须退 1。"""
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"symbol": sym, "status": "fetch_failed", "rows": 0, "added": 0},
    )
    assert incremental.main() == 1


def test_incremental_up_to_date_is_not_failure(monkeypatch):
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"symbol": sym, "status": "up_to_date", "rows": 10, "added": 0},
    )
    assert incremental.main() == 0


def test_incremental_unknown_status_counts_as_failure(monkeypatch):
    """未知状态按失败计：白名单分流，将来新增 write_failed 不会默认退 0。"""
    _patch_argv(monkeypatch, ["incremental", "--limit", "1"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [Path("sh600000.parquet")])
    monkeypatch.setattr(
        incremental, "incremental_update",
        lambda sym: {"symbol": sym, "status": "write_failed", "rows": 0, "added": 0},
    )
    assert incremental.main() == 1


def test_entry_points_fail_on_empty_target_list(monkeypatch, tmp_path):
    """空的日线清单 = 路径配错或数据被清空，必须退 1（对齐 sync_exit_code 的 total==0）。"""
    _patch_argv(monkeypatch, ["indicators", "--incremental"])
    monkeypatch.setattr(indicators, "daily_files", lambda: [])
    monkeypatch.setattr(indicators, "DERIVED_DIR", tmp_path)
    assert indicators.main() == 1

    _patch_argv(monkeypatch, ["incremental"])
    monkeypatch.setattr(incremental, "daily_files", lambda: [])
    assert incremental.main() == 1


def test_incremental_main_dispatch_is_whitelisted():
    """源码级守卫：up_to_date 必须显式放行、fetch_failed 必须显式计失败。"""
    src = (Path(__file__).resolve().parents[1] / "tdxfeed" / "incremental.py").read_text(
        encoding="utf-8"
    )
    assert 'elif st == "up_to_date":' in src
    assert 'elif st == "fetch_failed":' in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_entry_exit_codes.py -q`
Expected: FAIL — `assert None == 1`（两个 main 都返回 None）

- [ ] **Step 3: 实现**

`tdxfeed/precompute/indicators.py`：文件顶部补 `import sys`；把 `--code` 分支与结尾改为：

```python
    if args.code:
        try:
            out = process_one(args.code, force=args.force)
        except Exception as e:
            print(f"  ✗ {args.code}: {e}", file=sys.stderr)
            return 1
        print(f"[{args.code}] → {out}")
        return 0
```

```python
    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n完成：ok={ok} skip={skip} fail={fail}  耗时 {elapsed/60:.1f}min")
    print(f"输出目录：{DERIVED_DIR}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
```

`tdxfeed/incremental.py`：文件顶部补 `import sys`；把状态分流改为白名单三分（`fetch_failed` 与任何未知状态计失败，只有 `up_to_date` 是 skip），空清单直接失败，并改入口：

```python
    if not syms:
        log.error("没有可处理的标的（daily_files() 为空）")
        return 1
```

```python
        st = r["status"]
        if st == "ok":
            ok += 1
            added_total += r["added"]
        elif st == "up_to_date":
            skip += 1
        elif st == "fetch_failed":
            fail += 1
        else:
            fail += 1
            log.error("  未知状态 %r（%s），按失败计", st, sym)
```

```python
    log.warning("完成 ok=%d skip=%d fail=%d added=%d", ok, skip, fail, added_total)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
```

`tdxfeed/precompute/indicators.py` 的 `main()` 在拿到 `files` 后同样要判空：

```python
    files = daily_files()
    total = len(files)
    if total == 0:
        print("❌ 没有可处理的日线文件（daily_files() 为空）", file=sys.stderr)
        return 1
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_entry_exit_codes.py -q`
Expected: 4 passed（执行时补了 fetch_failed / up_to_date / 未知状态 / 空清单 / 分流守卫五条用例，实际 9 passed）

- [ ] **Step 5: 提交**

```bash
git add tdxfeed/precompute/indicators.py tdxfeed/incremental.py tests/test_entry_exit_codes.py
git commit -m "fix(precompute): 派生表与增量入口在部分失败时退出非零"
```

---

### Task 3: 复权因子增量更新模块

**Files:**
- Create: `tdxfeed/adj_factor.py`
- Modify: `Makefile`（`data-adj` 目标）
- Test: `tests/test_adj_factor_update.py`（新建）

**Interfaces:**
- Produces:
  - `tdxfeed.adj_factor.COLUMNS == ["code", "date", "adj_factor", "qfq_close", "raw_close"]`
  - `bs_code(code: str) -> str`
  - `last_date(path: Path) -> str | None`
  - `merge_rows(old: pd.DataFrame, qfq: list[list], raw: list[list]) -> pd.DataFrame | None`
  - `update_one(bs, code: str, out_dir: Path, today: str) -> str`（`"ok:+N"` / `"skip"` / `"empty"` / `"fail:<原因>"`）
  - `main(argv=None) -> int`
- Consumes: `tdxfeed.paths.adj_factor_dir()`；现有 schema 的 `date` 是 **`date32[day]`**（pyarrow 实测 `000001.parquet`），pandas 读出来是 `datetime.date` 对象。写回必须保持这一类型：`astype(str)` 会把它悄悄变成 `string` 列，`pd.to_datetime` 会变成 `timestamp[ns]`，两者都是 schema 漂移。

**背景（实测）：**
- 5221 个文件全部是股票代码（无 ETF），与 `data/all_a_stocks.json` 完全一致。
- 现存脚本 `scripts/download_adj_factor_b.py` 遇到已存在文件直接 `return 'cached'`，是**一次性回填**，不是更新器；`download_adj_factor_bj.py` 只补缺失的 920 段；`backfill_adj_etf.py` 只补缺失 ETF。三者都不更新已有文件 → 这是停更 5 周的根因。
- baostock 实测：登录 1.7s，单只两条序列各 0.02–0.03s，按此估算全量约 5 分钟。**执行时实测全量约 60 分钟**（5221 只 × 约 0.6 秒，含除权后整表重拉的标的）——计划低估了整表重拉与网络开销，文档口径以实测为准。
- `data/adj_factor` 不被 `write_table` 契约覆盖（AGENTS.md），保持裸 `to_parquet`，但必须 tmp + `os.replace` 原子替换，且日期列保持 `date32[day]`；临时文件**不要**以 `.parquet` 结尾（避免被 `daily_files()` 类 glob 当股票）。

- [ ] **Step 1: 写失败测试**

```python
"""复权因子增量合并：不联网，只测纯逻辑与本地文件写入。"""
from __future__ import annotations

from datetime import date

import pandas as pd

from tdxfeed.adj_factor import COLUMNS, bs_code, last_date, merge_rows, update_one, write_atomic


def _old_df():
    # 与现存文件同 schema：date 是 datetime.date → pyarrow date32[day]
    return pd.DataFrame(
        [
            ["600000", date(2026, 9, 23), 1.0, 8.98, 8.98],
            ["600000", date(2026, 9, 24), 1.0, 9.00, 9.00],
        ],
        columns=COLUMNS,
    )


def test_bs_code_mapping():
    assert bs_code("600000") == "sh.600000"
    assert bs_code("688001") == "sh.688001"
    assert bs_code("000001") == "sz.000001"
    assert bs_code("300750") == "sz.300750"
    assert bs_code("920001") == "bj.920001"
    assert bs_code("830799") == "bj.830799"


def test_merge_rows_appends_and_dedupes():
    qfq = [["2026-09-25", "9.10"], ["2026-09-28", "9.16"], ["2026-09-24", "9.00"]]
    raw = [["2026-09-25", "9.10"], ["2026-09-28", "9.16"], ["2026-09-24", "9.00"]]
    out = merge_rows(_old_df(), qfq, raw)
    assert out is not None
    assert [str(d) for d in out["date"]] == ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"]
    assert len(out) == 4
    assert out.iloc[-1]["adj_factor"] == 1.0


def test_merge_rows_returns_none_without_overlap():
    assert merge_rows(_old_df(), [], []) is None
    assert merge_rows(_old_df(), [["2026-09-25", "9.10"]], []) is None


def test_merge_rows_drops_non_positive_raw():
    qfq = [["2026-09-25", "0"]]
    raw = [["2026-09-25", "0"]]
    assert merge_rows(_old_df(), qfq, raw) is None


def test_update_one_skips_when_already_today(tmp_path, monkeypatch):
    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    monkeypatch.setattr(m, "_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    assert update_one(object(), "600000", tmp_path, "2026-09-24") == "skip"


def test_update_one_writes_atomically(tmp_path, monkeypatch):
    import pyarrow.parquet as pq

    p = tmp_path / "600000.parquet"
    _old_df().to_parquet(p, index=False)
    import tdxfeed.adj_factor as m

    calls = []

    def fake_query(bs, code, start, end, flag):
        calls.append((code, start, end, flag))
        return [["2026-09-25", "9.10"]]

    monkeypatch.setattr(m, "_query", fake_query)
    assert update_one(object(), "600000", tmp_path, "2026-09-25") == "ok:+1"
    assert calls[0][1] == "2026-09-25"
    assert calls[0][3] == "2"
    assert last_date(p) == "2026-09-25"
    assert str(pq.read_schema(p).field("date").type) == "date32[day]"
    assert not list(tmp_path.glob("*.parquet.tmp*"))


def test_write_atomic_leaves_no_parquet_tmp(tmp_path):
    p = tmp_path / "600000.parquet"
    write_atomic(p, _old_df())
    assert p.exists()
    assert [x.name for x in tmp_path.iterdir()] == ["600000.parquet"]


def test_write_atomic_preserves_date32(tmp_path):
    import pyarrow.parquet as pq

    p = tmp_path / "600000.parquet"
    write_atomic(p, _old_df())
    assert str(pq.read_schema(p).field("date").type) == "date32[day]"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_adj_factor_update.py -q`
Expected: FAIL — `ModuleNotFoundError: tdxfeed.adj_factor`

- [ ] **Step 3: 实现 `tdxfeed/adj_factor.py`**

```python
"""复权因子增量更新（baostock 前复权 / 不复权两条序列）。

历史由 scripts/download_adj_factor_b.py 一次性回填；那个脚本命中已存在文件即
return 'cached'，不负责更新，所以每日维护必须走这里。

schema 与现存文件逐列一致：code/date/adj_factor/qfq_close/raw_close，
date 是 date32[day]（pandas 里是 datetime.date），不是 string 也不是 timestamp。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tdxfeed.paths import adj_factor_dir

COLUMNS = ["code", "date", "adj_factor", "qfq_close", "raw_close"]


def bs_code(code: str) -> str:
    """6 位代码 → baostock 代码。4/8/92 段归北交所。"""
    c = str(code)[-6:]
    if c.startswith(("60", "68", "90", "11", "13")):
        return f"sh.{c}"
    if c.startswith(("4", "8", "92")):
        return f"bj.{c}"
    return f"sz.{c}"


def last_date(path: Path) -> str | None:
    if not Path(path).exists():
        return None
    df = pd.read_parquet(path, columns=["date"])
    if df.empty:
        return None
    return str(df["date"].iloc[-1])


def merge_rows(old: pd.DataFrame, qfq: list[list], raw: list[list]) -> pd.DataFrame | None:
    """把增量两条序列并入旧表；无可用新行时返回 None。"""
    if not qfq or not raw:
        return None
    dq = pd.DataFrame(qfq, columns=["date", "qfq_close"])
    dr = pd.DataFrame(raw, columns=["date", "raw_close"])
    new = dq.merge(dr, on="date")
    new["qfq_close"] = pd.to_numeric(new["qfq_close"], errors="coerce")
    new["raw_close"] = pd.to_numeric(new["raw_close"], errors="coerce")
    new = new.dropna(subset=["qfq_close", "raw_close"])
    new = new[new["raw_close"] > 0]
    if new.empty:
        return None
    new["adj_factor"] = (new["qfq_close"] / new["raw_close"]).round(6)
    new["code"] = old["code"].iloc[0]
    merged = pd.concat([old[COLUMNS], new[COLUMNS]], ignore_index=True)
    # 日期统一回 datetime.date：pyarrow 由此推出 date32[day]，与现存文件一致。
    # 别 astype(str)（会变 string 列），也别留 datetime64（会变 timestamp[ns]）。
    merged["date"] = pd.to_datetime(merged["date"]).dt.date
    merged = merged.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return merged.reset_index(drop=True)[COLUMNS]


def write_atomic(path: Path, df: pd.DataFrame) -> None:
    """tmp → os.replace。临时文件不以 .parquet 结尾，避免被文件枚举当股票。"""
    tmp = Path(str(path) + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _query(bs, code: str, start: str, end: str, adjustflag: str) -> list[list]:
    rs = bs.query_history_k_data_plus(
        code, "date,close", start_date=start, end_date=end,
        frequency="d", adjustflag=adjustflag,
    )
    # 错误码必须抛：吞掉它会让一次登录失效/接口异常退化成 5221 只「skip」，
    # 退出码仍是 0——正是这批改造要消灭的静默失败。
    if rs.error_code != "0":
        raise RuntimeError(f"baostock {rs.error_code}: {rs.error_msg}")
    rows: list[list] = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def update_one(bs, code: str, out_dir: Path, today: str) -> str:
    path = Path(out_dir) / f"{code}.parquet"
    start = last_date(path)
    if start is None:
        return "empty"
    if start >= today:
        return "skip"
    old = pd.read_parquet(path)
    start_next = (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        qfq = _query(bs, bs_code(code), start_next, today, "2")
        raw = _query(bs, bs_code(code), start_next, today, "3")
    except Exception as e:  # 单只失败不应中断整轮
        return f"fail:{type(e).__name__}:{str(e)[:60]}"
    merged = merge_rows(old, qfq, raw)
    if merged is None:
        return "skip"
    write_atomic(path, merged)
    return f"ok:+{len(merged) - len(old)}"


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="复权因子增量更新（baostock）")
    ap.add_argument("--code", help="只更新一只，如 600000")
    ap.add_argument("--limit", type=int, help="只处理前 N 只（试跑用）")
    ap.add_argument("--today", default=datetime.now().strftime("%Y-%m-%d"),
                    help="截止日期，默认今天（测试可覆盖）")
    args = ap.parse_args(argv)

    import baostock as bs

    out_dir = adj_factor_dir()
    if not out_dir.exists():
        print(f"❌ 复权因子目录不存在: {out_dir}", file=sys.stderr)
        return 1

    if args.code:
        codes = [args.code[-6:]]
    else:
        codes = sorted(p.stem for p in out_dir.glob("*.parquet")
                       if len(p.stem) == 6 and p.stem.isdigit())
    if args.limit:
        codes = codes[: args.limit]
    if not codes:
        print("❌ 没有可更新的复权因子文件", file=sys.stderr)
        return 1

    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}", file=sys.stderr)
        return 1
    ok = skip = fail = added = 0
    try:
        for i, code in enumerate(codes, 1):
            r = update_one(bs, code, out_dir, args.today)
            if r.startswith("ok:"):
                ok += 1
                added += int(r.split(":")[1].lstrip("+"))
            elif r == "skip":
                skip += 1
            else:
                fail += 1
                if fail <= 5:
                    print(f"  ✗ {code}: {r}", file=sys.stderr)
            if i % 200 == 0:
                print(f"  [{i}/{len(codes)}] ok={ok} skip={skip} fail={fail}", flush=True)
    finally:
        bs.logout()

    total = ok + skip + fail
    print(f"完成：ok={ok} skip={skip} fail={fail} 新增行={added} 截止={args.today}")
    return 1 if fail > max(20, total * 0.02) else 0


if __name__ == "__main__":
    sys.exit(main())
```

Makefile `data-adj` 改为增量入口（原来的全量回填脚本仍保留在文件里）：

```make
data-adj:  ## 增量更新复权因子（baostock；首次全量用 scripts/download_adj_factor_b.py）
	$(PYTHON) -m tdxfeed.adj_factor
	@echo "$(GREEN)✓ 复权因子增量完成$(NC)"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_adj_factor_update.py -q`
Expected: 8 passed

- [ ] **Step 5: 先备份，再试跑一只与十只**

备份必须先于任何写入（`data/` 不进 git，硬链接快照是唯一回滚手段；tmp→`os.replace` 会换 inode，所以快照不会被原地改写）：

```bash
cp -al data/adj_factor data/adj_factor_pre_batch_a_20261029
venv/bin/python -m tdxfeed.adj_factor --code 600000
venv/bin/python -m tdxfeed.adj_factor --limit 10
```
Expected: `完成：ok=N ...`；`600000` 的 `last_date` 变成最近交易日。

- [ ] **Step 6: 全量回填（5221 只，实测约 60 分钟）**

```bash
venv/bin/python -m tdxfeed.adj_factor
```
Expected: `fail` 小于 105（2%），退出码 0。

验证：

```bash
venv/bin/python - <<'EOF'
import glob, pandas as pd, collections
last = collections.Counter()
for f in glob.glob('data/adj_factor/*.parquet'):
    last[str(pd.read_parquet(f, columns=['date'])['date'].iloc[-1])] += 1
print(last.most_common(5))
EOF
```
Expected: 绝大多数文件末日期是最近交易日（节假日按 `tdxfeed/freshness.CN_HOLIDAYS`）。

- [ ] **Step 7: 提交**

```bash
git add tdxfeed/adj_factor.py tests/test_adj_factor_update.py Makefile
git commit -m "feat(adj): 复权因子增量更新模块，替换只回填不更新的旧入口"
```

---

### Task 4: 同步后自动刷新派生表与复权因子

**Files:**
- Modify: `systemd/system/tdx-sync.service`
- Test: `tests/test_sync_post_jobs.py`（新建）

**Interfaces:**
- Consumes: `python -m tdxfeed.precompute.indicators --incremental`（Task 2 已保证退出码）、`python -m tdxfeed.adj_factor`（Task 3）。
- 不新增 systemd 单元：`tests/test_systemd_system_units_mirror.py` 的 `MIRRORED` 集合不变。

**背景（实测）：** derived 全市场 `compute_one` 约 0.008s/只 → 5211 只约 **42 秒**（含整轮开销按约 1 分钟记）；
adj 全量增量**实测约 60 分钟**（计划时按单次请求耗时估成 5 分钟，未计整表重拉）。两者都远小于 `tdx-sync.service` 的 `TimeoutStartSec=10800`。
`-` 前缀让后置命令失败不把整轮同步判红（数据健康由 canary 兜底），
`ExecStart` 失败（坏率 >20%）时后置命令不会运行，这是想要的语义。

- [ ] **Step 1: 写失败测试**

```python
"""同步单元必须把派生表与复权因子挂在后面，且后置失败不判整轮失败。"""
from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "systemd" / "system" / "tdx-sync.service"

NEEDS_DATA = False


def test_sync_unit_has_post_jobs():
    text = UNIT.read_text(encoding="utf-8")
    assert "ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.precompute.indicators --incremental" in text
    assert "ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.adj_factor" in text


def test_post_job_modules_expose_main():
    for mod_name in ("tdxfeed.precompute.indicators", "tdxfeed.adj_factor"):
        mod = importlib.import_module(mod_name)
        assert callable(getattr(mod, "main", None)), f"{mod_name} 缺少 main()"


def test_indicators_supports_incremental_flag():
    src = (ROOT / "tdxfeed" / "precompute" / "indicators.py").read_text(encoding="utf-8")
    assert '"--incremental"' in src or "'--incremental'" in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_sync_post_jobs.py -q`
Expected: FAIL — 两条 `ExecStartPost` 不存在

- [ ] **Step 3: 修改单元**

`systemd/system/tdx-sync.service` 在 `ExecStart=` 之后追加：

```
# 同步成功后刷新派生表与复权因子（增量）。前缀 "-" 表示后置任务失败不把整轮同步判红：
# 数据是否真的新鲜由 canary 的 data_files 检查兜底，避免一次网络抖动让整个同步单元变红。
ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.precompute.indicators --incremental
ExecStartPost=-/home/jiuben/tdx-data-feed/venv/bin/python -m tdxfeed.adj_factor
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_sync_post_jobs.py tests/test_systemd_system_units_mirror.py -q`
Expected: 新测试 3 passed；镜像守卫此时**会红**（仓库与 `/etc` 不一致），这是预期的，Step 5 解决。

- [ ] **Step 5: 部署（需要 root，必须人工确认）**

AGENTS.md 记录的路径：从 Qoder 起的进程树带 `NoNewPrivs=1`，`sudo` 拿不到 root；
用 `systemd-run --user --pty --wait` 起 nnp=0 作用域，里面用 `sudo -A` + zenity 输密码：

```bash
systemd-run --user --pty --wait bash -lc '
  cp /home/jiuben/tdx-data-feed/systemd/system/tdx-sync.service /etc/systemd/system/tdx-sync.service &&
  systemctl daemon-reload &&
  systemctl cat tdx-sync.service | grep ExecStartPost'
```

**这一步会弹密码框，必须由用户本人在场确认。** 预期输出两条 `ExecStartPost=`。

- [ ] **Step 6: 跑镜像守卫确认部署与仓库一致**

Run: `venv/bin/pytest tests/test_systemd_system_units_mirror.py -q`
Expected: passed

- [ ] **Step 7: 提交**

```bash
git add systemd/system/tdx-sync.service tests/test_sync_post_jobs.py
git commit -m "feat(sync): 同步后自动增量刷新派生表与复权因子"
```

---

### Task 5: canary 增加表级新鲜度检查

**Files:**
- Modify: `tdxfeed/freshness.py`（新增两个公开函数）
- Modify: `scripts/canary_health_check.py`（`check_data_files`）
- Test: `tests/test_canary_table_freshness.py`（新建）

**Interfaces:**
- Produces:
  - `tdxfeed.freshness.sample_latest_date(files, sample_size=30, seed=42) -> date | None`（取抽样末日期中位数）
  - `tdxfeed.freshness.stale_threshold(ref: date, allowed_trading_days: int = 3, holidays=None) -> date`
  - `scripts.canary_health_check._today() -> date`（今天的唯一入口，测试 monkeypatch 它，避免用例随真实日期腐烂）
- Consumes: `canary_health_check.check_data_files()` 已有 paging 身份（`PAGING_CHECKS` 含 `data_files`），不新增 paging 类别，避免多一类每 5 分钟的推送。

**背景：** 现有 `check_data_files` 只数文件个数，所以 adj 停在 09-24、derived 停在 09-28 都显示 ok。
阈值取「最近已收盘交易日往前 3 个交易日」，节假日由 `CN_HOLIDAYS` 跳过。

- [ ] **Step 1: 写失败测试**

```python
"""canary 必须能发现 adj/derived 停更——只数文件个数是发现不了的。"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _canary():
    spec = importlib.util.spec_from_file_location(
        "canary_tbl_fresh", ROOT / "scripts" / "canary_health_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, last: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime([last])}).to_parquet(path, index=False)


def _dirs(tmp_path):
    daily, adj, derived = tmp_path / "daily", tmp_path / "adj", tmp_path / "derived"
    _write(daily / "sh600000.parquet", "2026-10-28")
    _write(derived / "sh600000_indicators.parquet", "2026-10-28")
    _write(adj / "600000.parquet", "2026-10-28")
    return daily, adj, derived


def _wire(m, monkeypatch, daily, adj, derived):
    monkeypatch.setattr(m, "_today", lambda: date(2026, 10, 29))
    monkeypatch.setattr(m, "daily_dir", lambda: daily)
    monkeypatch.setattr(m, "adj_factor_dir", lambda: adj)
    monkeypatch.setattr(m, "derived_dir", lambda: derived)
    monkeypatch.setattr(m, "PARQUET_MIN_COUNT", 1)
    monkeypatch.setattr(m, "ADJ_MIN_COUNT", 1)


def test_data_files_ok_when_all_fresh(tmp_path, monkeypatch):
    m = _canary()
    _wire(m, monkeypatch, *_dirs(tmp_path))
    r = m.check_data_files()
    assert r["status"] == "ok", r


def test_data_files_warns_when_adj_stale(tmp_path, monkeypatch):
    m = _canary()
    daily, adj, derived = _dirs(tmp_path)
    for i in range(5):
        _write(adj / f"60000{i}.parquet", "2026-09-24")
    _wire(m, monkeypatch, daily, adj, derived)
    r = m.check_data_files()
    assert r["status"] == "warn"
    assert "adj_factor" in r["message"]


def test_data_files_warns_when_derived_stale(tmp_path, monkeypatch):
    m = _canary()
    daily, adj, derived = _dirs(tmp_path)
    for i in range(5):
        _write(derived / f"sh60000{i}_indicators.parquet", "2026-09-28")
    _wire(m, monkeypatch, daily, adj, derived)
    r = m.check_data_files()
    assert r["status"] == "warn"
    assert "derived" in r["message"]


def test_stale_threshold_skips_holidays():
    from tdxfeed.freshness import stale_threshold

    # 2026-10-09 之前最近的已收盘交易日是 09-30（国庆休市），再回退 1 个交易日 = 09-29
    assert stale_threshold(date(2026, 10, 9), allowed_trading_days=1) == date(2026, 9, 29)
    assert stale_threshold(date(2026, 10, 29), allowed_trading_days=3) == date(2026, 10, 23)


def test_sample_latest_date_returns_median(tmp_path):
    from tdxfeed.freshness import sample_latest_date

    files = []
    for i, d in enumerate(["2026-09-24", "2026-09-25", "2026-09-26"]):
        p = tmp_path / f"{i}.parquet"
        _write(p, d)
        files.append(p)
    assert sample_latest_date(files) == date(2026, 9, 25)
    assert sample_latest_date([]) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_canary_table_freshness.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'derived_dir'` / 陈旧时仍 ok

- [ ] **Step 3: 在 `tdxfeed/freshness.py` 增加两个函数**

```python
def sample_latest_date(files, sample_size: int = 30, seed: int = 42) -> date | None:
    """抽样读 parquet 的末日期，返回中位数日期（None 表示没有可读文件）。"""
    files = [Path(f) for f in files]
    if not files:
        return None
    rng = np.random.default_rng(seed)
    sample = rng.choice(files, min(sample_size, len(files)), replace=False)
    dates = [d.date() for d in (_last_date_from_parquet(f) for f in sample) if d is not None]
    if not dates:
        return None
    dates.sort()
    return dates[len(dates) // 2]


def stale_threshold(ref: date, allowed_trading_days: int = 3,
                    holidays: set[str] | None = None) -> date:
    """晚于此日期即视为陈旧：最近已收盘交易日再往前回退 N 个交易日。"""
    hol = holidays if holidays is not None else CN_HOLIDAYS
    return _subtract_trading_days(last_trading_day(ref, hol), allowed_trading_days, hol)


__all__ = ["last_trading_day", "require_fresh", "sample_latest_date", "stale_threshold"]
```

- [ ] **Step 4: 改 `scripts/canary_health_check.py`**

顶部 import 改为：

```python
from _common import adj_factor_dir, daily_dir  # noqa: E402
from tdxfeed.freshness import sample_latest_date, stale_threshold  # noqa: E402
from tdxfeed.paths import derived_dir  # noqa: E402
```

`check_data_files` 在原有计数之后追加（`_today()` 是新增的「今天」唯一入口，放在 `PAGING_CHECKS` 旁边，便于测试 monkeypatch）：

```python
def _today() -> date:
    return date.today()


def check_data_files() -> dict:
    ...
    base = _today()
    limit = stale_threshold(base)
    tables = {
        "adj_factor": (adj_dir, ["*.parquet"]),
        "derived": (derived_dir(), ["*_indicators.parquet"]),
    }
    problems = []
    for name, (d, patterns) in tables.items():
        files = [p for pat in patterns for p in d.glob(pat)] if d.exists() else []
        latest = sample_latest_date(files)
        result["details"][f"{name}_latest"] = latest.isoformat() if latest else None
        if latest is None:
            problems.append(f"{name}: 无文件")
        elif latest < limit:
            problems.append(f"{name} 陈旧: 末日期中位数 {latest} < {limit}")

    result["details"]["stale_before"] = limit.isoformat()
    if problems:
        result["status"] = "warn"
        prev = result.get("message")
        result["message"] = "; ".join(([prev] if prev else []) + problems)
    return result
```

顶部补 `from datetime import date, datetime, timedelta`（当前只有 `datetime, timedelta`）。

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_canary_table_freshness.py tests/test_canary_sync_freshness.py tests/test_freshness.py -q`
Expected: 全 passed

- [ ] **Step 6: 本机实跑一次 canary**

Run: `venv/bin/python scripts/canary_health_check.py`
Expected: `data_files` 为 ok（回填后 adj/derived 都应新鲜）；若仍 warn，输出会写明是哪张表、中位数日期。

- [ ] **Step 7: 提交**

```bash
git add tdxfeed/freshness.py scripts/canary_health_check.py tests/test_canary_table_freshness.py
git commit -m "feat(canary): data_files 检查加入 adj_factor/derived 末日期"
```

---

### Task 6: 文档与总门禁

**Files:**
- Modify: `AGENTS.md`（「数据路径与表口径」与调度说明）
- Modify: `docs/WORKFLOW.md`（adj_factor / derived / adj_factor_ext 三行）
- Modify: `.github/workflows/tests.yml`（仅在基数变化时）
- Test: 全量

**Interfaces:** 无新接口。

- [ ] **Step 1: 改 AGENTS.md**

在「每日全市场同步只有一个调度源」那条后面补两句：

```
- 同步成功后由 `tdx-sync.service` 的两行 `ExecStartPost=-` 续跑：`python -m tdxfeed.precompute.indicators --incremental`（派生表，全市场约 1 分钟）与 `python -m tdxfeed.adj_factor`（复权因子增量，**全量约 60 分钟**）。`-` 前缀表示后置失败不把整轮同步判红，是否新鲜由 canary 的 `data_files` 检查兜底（`tests/test_canary_table_freshness.py`）。
- `data/adj_factor` 的 `date` 列是 **`date32[day]`**（不是 string、也不是 timestamp），写回时保持 `datetime.date` 对象，别 `astype(str)`；历史由 `scripts/download_adj_factor_b.py` 一次性回填，日常更新只走 `tdxfeed/adj_factor.py`（旧脚本命中已存在文件即跳过）。`data/.state/manifest.json` 的 `last_date` 是 8 位数字字符串 `YYYYMMDD`（`str`，不是 int）。
```

- [ ] **Step 2: 改 docs/WORKFLOW.md**

把 `adj_factor` 与 `derived` 的调度列从「手动」改为「`tdx-sync.service` ExecStartPost（工作日 16:30 后）」，并在 `adj_factor_ext/` 那行明确写「未回填、无消费者，不在每日链路内」——避免继续被当成现行权威。

- [ ] **Step 3: 跑总门禁**

```bash
venv/bin/pytest tests/ -q          # 有数据：期望全绿
TDX_TEST_DATA_ROOT=/nonexistent venv/bin/pytest tests/ -q   # 无数据演练
```
Expected: 两个基数都记下来。

- [ ] **Step 4: 更新 CI 阈值（仅当基数变化）**

CI 可达成值 = 无数据演练 passed − 本机专属 passed − 依赖本机服务的用例。
按上一轮口径（本机专属 10 条、告警栈 5 条）重算；新测试若都在 `tests/` 且不依赖数据，
可达成值会相应上升。只有确实变化时才改 `.github/workflows/tests.yml` 的注释与 `-ge`，
并在提交信息里写明新的基数算式。

- [ ] **Step 5: 提交**

```bash
git add AGENTS.md docs/WORKFLOW.md
git commit -m "docs: 记录派生表与复权因子的同步后刷新路径"
```

如果 Step 4 改了 workflow，单独一次提交：

```bash
git add .github/workflows/tests.yml
git commit -m "ci: 按新增用例重测可达成基数并更新阈值"
```

---

## 验收判据

1. `data/.state/manifest.json` 的 `last_date` 全为 8 位数字（下一轮同步后自愈，抽样验证）。
2. `data/adj_factor` 所有文件末日期 = 最近交易日；`data/parquet/derived` 与 daily 同集且同末日期。
3. `systemctl cat tdx-sync` 含两条 `ExecStartPost=-`，且镜像守卫绿。
4. `venv/bin/python scripts/canary_health_check.py` 的 `data_files` 为 ok，details 里有三张表的末日期。
5. `venv/bin/pytest tests/ -q` 全绿；无数据演练基数已重测并记录。
6. 派生表缺失或复权因子停更时，canary 会 warn 并因 `data_files` 在 `PAGING_CHECKS` 中而退出 1。

## 明确不做

- 不回填 `data/adj_factor_ext/`：目前零消费者（`grep` 只有测试引用），属于 10-26 决策与现实的落差，留到批次 C 一并定夺（回填或从「权威」措辞里撤下）。
- 不改 `State.update()` 每只股票写一次 manifest 的实现（5236 次小文件写），属性能债，不在本批次。
- 不处理 8 处裸码前缀解析、5 处涨跌停阈值旁路、quick-sop / docker-publish 假绿门禁——那些是批次 B。
- 不给 45 个 `scripts/*.py` 统一补退出码，只修两个生产入口。

---

### Task 3 修订（复审后，2026-10-29，取代上文 Step 3 的实现与 Step 1 中的两条断言）

首轮实现落地后复审发现 4 条 Important，均与「静默失败 / 数据不一致」同源，按下面重写：

1. **小规模退出码恒为 0**：`fail > max(20, total*0.02)` 在 `total ≤ 20` 时阈值恒为 20，`--code` / `--limit 1` 100% 失败也退 0。抽出可测函数：

```python
def exit_code(ok: int, skip: int, fail: int) -> int:
    total = ok + skip + fail
    if total == 0:
        return 1
    if total < 1000:                 # --code / --limit 试跑：不允许有失败
        return 1 if fail else 0
    return 1 if fail > max(20, total * 0.02) else 0
```

2. **空响应被记成 skip**：源整体返回空但 `error_code == "0"` 时，5221 只全 skip、退 0。区分三态：两条序列都空＝没有新交易日（skip）；只有一条空、或有行但全无有效值＝数据异常（fail）。

3. **除权后历史段不重算**：前复权基准在除权日整体平移，只追加会让旧段留在旧基准上。实测 `000513` 09-24 的 `adj_factor=1.0` 与 09-28 的 `0.965341` 并存（09-24 及以前应为 0.965341）。修法：查询窗口从 `last_date` 当日起（重叠 1 日），比对重叠日 `qfq_close`；变了就整表从 `START_DATE` 重拉并全量替换。

```python
START_DATE = "2020-01-01"


def _rebase_needed(old: pd.DataFrame, qfq: list[list]) -> bool:
    """重叠日的 qfq_close 变了 → 除权已重算前复权基准，必须整表重拉。"""
    if old.empty or not qfq:
        return False
    by_date = {row[0]: row[1] for row in qfq}
    last_day = str(old["date"].iloc[-1])
    if last_day not in by_date:
        return False
    try:
        new_val = float(by_date[last_day])
    except (TypeError, ValueError):
        return False
    old_val = float(old["qfq_close"].iloc[-1])
    return abs(new_val - old_val) > 1e-6 * max(1.0, abs(old_val))


def merge_rows(old: pd.DataFrame, qfq: list[list], raw: list[list],
               code: str | None = None) -> pd.DataFrame | None:
    ...
    new["code"] = code if code is not None else old["code"].iloc[0]
    ...
```

`update_one` 的新骨架（整段包 try，单只异常记 1 个 fail，不中止整轮）：

```python
def update_one(bs, code: str, out_dir: Path, today: str) -> str:
    path = Path(out_dir) / f"{code}.parquet"
    try:
        start = last_date(path)
        if start is None:
            return "empty"
        if start >= today:
            return "skip"
        old = pd.read_parquet(path)
        qfq = _query_retry(bs, bs_code(code), start, today, "2")
        raw = _query_retry(bs, bs_code(code), start, today, "3")
        if not qfq and not raw:
            return "skip"                       # 周末/节假日：没有新交易日
        if not qfq or not raw:
            return "fail:partial-empty"
        rebased = _rebase_needed(old, qfq)
        if rebased:
            qfq = _query_retry(bs, bs_code(code), START_DATE, today, "2")
            raw = _query_retry(bs, bs_code(code), START_DATE, today, "3")
            n_old = len(old)
            old = old.iloc[0:0]                 # 整表替换
        else:
            n_old = len(old)
        merged = merge_rows(old, qfq, raw, code=code)
        if merged is None:
            return "fail:no-valid-rows"
        write_atomic(path, merged)
    except Exception as e:
        return f"fail:{type(e).__name__}:{str(e)[:60]}"
    if rebased:
        return f"rebase:+{len(merged)}"
    return f"ok:+{len(merged) - n_old}"
```

4. **无超时/重试**：模块顶部 `socket.setdefaulttimeout(30)`；`_query_retry` 3 次退避（`2 ** i` 秒）。为避免登录失效时 5221 只 × 3 次退避拖满 3 小时，主循环加熔断：连续失败 ≥ 20 只就中止并退 1。

```python
def _query_retry(bs, code: str, start: str, end: str, adjustflag: str,
                 attempts: int = 3) -> list[list]:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return _query(bs, code, start, end, adjustflag)
        except Exception as e:
            last = e
            time.sleep(2 ** i)
    raise last
```

其余小项：`write_atomic` 失败时 `tmp.unlink(missing_ok=True)`；枚举文件改用 `paths.adj_factor_files()`；`main()` 末尾 `return exit_code(ok, skip, fail)`。

新增测试（追加到 `tests/test_adj_factor_update.py`）：
- `exit_code`：`(0,0,1)→1`、`(9,0,1)→1`、`(0,0,0)→1`、`(5210,11,0)→0`、`(5100,0,100)→0`、`(5100,0,121)→1`。
- `update_one`：重叠日 qfq 变化 → 返回 `rebase:+N`，且 09-24 的 `qfq_close`/`adj_factor` 已按新基准；`qfq` 有行 `raw` 空 → `fail:partial-empty`；两条都空 → `skip`；坏 parquet → `fail:...` 且不抛。
- 既有 `test_update_one_writes_atomically` 的 `calls[0][1]` 期望从 `"2026-09-25"` 改为 `"2026-09-24"`（窗口现在含重叠日）。

修完后必须重跑全量（约 35 分钟）：会再次查询所有 5221 只，其中除权的（复审按窗口内 `adj_factor<1` 扫出 ≥335 只）触发整表重拉。验收：`000513` 的 09-24 与 09-28 的 `adj_factor` 必须一致；备份仍在 `data/_archive/adj_factor_pre_batch_a_20261029`。
