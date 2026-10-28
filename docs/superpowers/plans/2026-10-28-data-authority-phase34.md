# 阶段 3+4：路径权威层、存量 derived 重建、因子与 MCP 收编 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「数据在哪」和「表怎么写」各只有一个回答：业务代码不再拼 `data/parquet/...` 字面量，`date` 列的写法只有一处实现，存量 `derived` 按统一口径重写一遍，26 维特征与 MCP 工具表各自只剩一个定义。

**Architecture:** 三层。①**定位层**：`tdxfeed/paths.py` 扩面（`parquet_root` / `adj_factor_dir` / `hfq_factor_dir` / `derived_dir` / `minute_dir` / `finance_parquet` / `finance_deep_dir` / `xdxr_path` / `daily_files` / `adj_factor_files`），`v5/scripts/_paths.py` 是它零耦合的孪生；`scripts/_common.py` 做再导出桥，脚本侧一个 import 行到位。②**读写层**：新建 `tdxfeed/parquet_io.py`，`write_table()` 是 `date32` + `tmp→rename` 原子写的唯一实现。③**收编层**：AST 守卫（`tests/test_no_hardcoded_data_paths.py`）把「不许写字面量」变成 CI 事实；`resolve_parquet` 三份重复改为委托；对 `daily` 目录做 universe 枚举的站点改走 `daily_files()`（排除 `.v1.bak`，本轮收口今天的无过滤站点）；`derived` 全量重建；`features_5d` 26/29 维为唯一实现；MCP 契约测试覆盖 6/6 服务器。

**Tech Stack:** Python 3.12（仓库 venv）、pyarrow、pandas、polars、pytest、jsonschema 4.26。

**Spec:** `docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md`（第 4 章 + 第 5 章 + 第 6 章阶段 3/4）

**前置依赖:** 阶段 1 已执行完毕——`tdxfeed/paths.py` 存在，且已提供 `DATA_ROOT`（读 `TDX_OUT_ROOT`）、`_six()`、`exchange_prefix()`、`daily_dir()`、`daily_path()`、`minute_path()`、`adj_factor_path()`、`hfq_factor_path()`、`derived_path()`、`universe_path()`、`read_path()`。本计划不改这些既有函数的行为，只在其上加新访问器。

## Global Constraints

- 命令一律在仓库根执行，Python 走 `venv/bin/python` / `venv/bin/pytest`，不用系统 `python3`。
- **共享工作树**：`git add` / `git commit` 必须带显式路径，禁止 `-A` 和 `.`。当前已知他人未提交改动：`scripts/backfill_minutes.py`（SINA `--period 1/5` + `--datalen`）、`tdxfeed/kline.py`（`SINA_SCALES` 加 `1: 'min1'`）、`v5/audit/verification/*`。本计划对前两个文件各只改**一行**（`scripts/backfill_minutes.py:35`、`tdxfeed/kline.py:24`），与他们的 hunk 不相邻；**这两个文件不进本计划的任何 commit**，改完留在工作树，由它们所属的会话提交。其余陌生脏文件看到就跳过，不修不提交。
- `data/`、`logs/`、`models/` 在 gitignore 里。本计划唯一写 `data/` 的动作是 Task 9 的 `derived` 重建，前置动作是 `cp -al` 硬链接快照（不占额外空间）。
- 本阶段**不改**任何表名、列名、策略参数、阈值；`paths.py` 只是把既存的命名事实编码下来，不「统一命名」。
- 不联网、不跑长任务：迁移验证用 `make lint-all`（阻断语法与未定义名）+ `make test` + 定向功能测试。**不要**用 `--help` 逐个烟测 58 个脚本——其中若干在 import 期读数据或连行情源。唯一例外是 Task 10 Step 7 的后台 mf 报表：它本身就是「阈值改了报表还能生成」的证据，必须跑。
- 计划里的行号取自 2026-10-28 工作树；阶段 1/2 已改过 `quant/multifactor/factors.py` 等文件，执行时一律用 `old_string` 定位，**不要**按行号跳。
- 提交信息中文，一行标题 + 必要正文；每个 Task 一个提交。
- 写到一半发现规格与实测不符时，**按本计划开头的更正表执行**，并在 Task 12 把差异补进规格文档的「实施期更正」小节。

## 与规格正文的 10 处更正（实施时以此为准）

| 编号 | 规格原写 | 实测更正 |
|---|---|---|
| C4′ | §4.2：`scripts/` 25 文件、`tdxfeed/` 5、`mcp/` 1、`v5/` 16 | AST 守卫口径实测 **90 文件 / 119 行**：`scripts` 58 / 80 行、`tdxfeed` 6 / 7 行、`quant` 2 / 2 行、`mcp` 3 / 7 行、`train` 2 / 3 行、`v5` 19 / 20 行。另 5 处字符串拼接型（`PK + sym`）守卫抓不到，靠 Task 3/6 的人工清单兜。 |
| C5 | §5.2：math 双传输工具集一致 | stdio 20 个 `Tool(...)`，HTTP `TOOL_NAMES` 23 个，**HTTP 多 3 个**：`math_concept_link` / `math_bilingual_translate` / `math_ontology_stats`（HTTP 派发链有分支、stdio 无）。parity 断言写成 `HTTP == stdio ∪ HTTP_ONLY`，不是等号。 |
| C6 | §4.3：`derived` 是唯一仍留 int64 的表 | 实测 `derived` 的 `date` 是 **`timestamp[ns]`**（`daily`/`adj_factor` 才是 `date32[day]`，分钟表是 `string`）。重建目标统一次到 `date32[day]`；唯一下游 `train/scripts/build_v8_dataset.py` 只按位置对齐、不比较日期类型，安全。 |
| C7 | §4.1：收编 4 份 date32 写入实现（含 `tdxfeed/amount_fix.py` 调用点） | `amount_fix.py` **不写盘**，只是 `merge_write` 的调用方；真正的第 4 个写入端是 `scripts/download_universe.py:84`，它写的是 **int64 日期**（v1 口径），会污染 `daily` 的 schema 一致性。 |
| C8 | §4.3：`derived` 整表重建 5200 个 | 5200 是**旧**派生集；按当前 canonical `daily`（5236）重建应为 **5236**：多出 8 个指数（`sh000001` 等）+ 28 只新上市个股（`sz3017xx` / `sh601123` 等）。重建后文件数断言用 5236，且必须不含 `*.v1.bak*`。 |
| C9 | §4.1：`finance_path()` | 实际命名 `finance_parquet()`：它指 `data/parquet/finance.parquet` 单文件，与同目录的 `xdxr_path()` 区分；既有常量名也是 `FIN_PQ` / `FIN_PARQUET`。 |
| C10 | §4.1：分钟表写入端「统一 `YYYY-MM-DD HH:MM:SS`」 | 分钟表时间列名是 **`datetime`**（`string`），不是 `date`。`parquet_io.write_table` 只对名为 `date` 的列做 date32 归一，分钟表原样写；格式守卫只断言 `string` 且首行形如 `YYYY-MM-DD HH:MM:SS`。 |
| C11 | §5.1-3：第三份 8 维自算改为调用 `factors_polars` | 权威实现是 **`tdxfeed.features_5d.compute_window_features_extended`**（29 键，含 3 个打板键）。`factors_polars` 是**迁移期复刻**且入参必须是 polars DataFrame（传 pandas 会 `AttributeError: 'DataFrame' object has no attribute 'height'`）。第三份副本改为调用 `features_5d` 并按 `FEATURE_BASE + NEW_FEATURES` 取 26 键，v9 CSV 的 26 列合约与列序不变。 |
| C12 | §5.1-2：委托后 `zt_density_20d` 在并轨前 ST 票上会变值，重跑 mf 报表附 diff | 委托**不带 `is_st`**（`data/all_a_stocks.json` 只有当前名称快照，无按日 ST 历史），主板阈值不变，**ST 偏差仍在**；真正变值的是 `689` 段（旧实现按主板 9.5%，实为科创板 19.5%）。全 5236 个 canonical 代码里只有 `sh689009` 一个受影响，其 `zt_density_20d` 有 81/1047 行不同。 |
| C13 | §5.2-1：`tools.json` 与各 server `list_tools()` 名称、数量一致 | 名称与**键序**实测 6/6 一致（可断列表相等）；但两边另有 8 处 `inputSchema`、8 处 description 漂移（json 侧更详细，如 `read_finance` / `calc_macd` / `search` / `add_doc`）。本轮只锁名称/数量/必填自洽，逐字同步另开。 |

## File Structure

新建：

- `tdxfeed/parquet_io.py` — `read_daily` / `write_daily` / `read_minute` / `read_derived` / `write_table`。
- `v5/scripts/_paths.py` — v5 侧路径定位（零耦合孪生）。
- `tests/test_data_layer.py` — 路径访问器 + `parquet_io` + 两套 `_paths` parity。
- `tests/test_common_reexports.py` — `scripts/_common.py` 再导出桥的 12 个名字（含 `write_table` 不在其中）。
- `tests/test_no_hardcoded_data_paths.py` — AST 守卫（含自测用例）。
- `tests/test_daily_listing_and_resolve.py` — 三份 `resolve_parquet` 委托 + `daily` 列举只走 `daily_files()`。
- `tests/test_write_entry_single_source.py` — 写入口收口（`write_table` 唯一 / `to_parquet` 清零）。
- `tests/test_parquet_schema_contract.py` — 生产表 schema 契约（`data/` 缺失时 skip）。
- `tests/test_features_parity.py` — 26/29 维双写 parity + 第三份副本只是取键。
- `tests/test_names_single_source.py` — `predict.is_st` 委托 `tdxfeed.names`。
- `tests/test_mcp_contracts.py` — 6/6 服务器契约 + calc 真调用 + JSON Schema 合法性。
- `v5/mcp_servers/math_tool_specs.py` — math 工具表单一来源（生成器产出）。

修改：

- `tdxfeed/paths.py`（加访问器）、`scripts/_common.py`（再导出）。
- 58 个 `scripts/*.py`、6 个 `tdxfeed/` 文件、2 个 `quant/multifactor/` 文件、3 个 `mcp/servers/` 文件、2 个 `train/scripts/` 文件、19 个 `v5/scripts/` 文件（清单见各 Task 表格）。
- `scripts/factor_engine.py`、`quant/multifactor/factors.py`、`scripts/sim_daily_compare.py`（`resolve_parquet` 委托）。
- `tdxfeed/writers.py`、`tdxfeed/incremental.py`、`scripts/download_hfq_factor.py`、`scripts/download_universe.py`、`scripts/migrate_v1_to_v2.py`（写入端收口）。
- `scripts/feature_engineering.py`（删第三份 8 维自算）。
- `scripts/predict.py`（`is_st` 委托 `tdxfeed.names`）。
- `tests/test_limit_single_source.py`（阶段 1 建的，追加 2 条 quant 阈值用例）。
- `v5/mcp_servers/math_sympy.py`、`v5/mcp_servers/math_sympy_http.py`、`v5/tests/test_math_sympy_http.py`、`v5/README.md`。
- `AGENTS.md`、规格文档（追加实施期更正）、`.github/workflows/tests.yml`（通过数下限）。

---

### Task 1: 定位层扩面 + `parquet_io` + v5 孪生

**Files:**
- Modify: `tdxfeed/paths.py`
- Create: `tdxfeed/parquet_io.py`
- Create: `v5/scripts/_paths.py`
- Test: `tests/test_data_layer.py`

**Interfaces:**
- Produces（`tdxfeed.paths`）：
  - `parquet_root() -> Path`、`adj_factor_dir() -> Path`、`hfq_factor_dir() -> Path`、`derived_dir() -> Path`
  - `minute_dir(period: str) -> Path`（`"60"`/`"min60"` 同义）
  - `finance_parquet() -> Path`、`finance_deep_dir() -> Path`、`xdxr_path() -> Path`
  - `daily_files() -> list[Path]`、`adj_factor_files() -> list[Path]`（均按名排序，`daily_files()` 排除 `.v1.bak.parquet`）
  - `DAILY_BAK_SUFFIX: str = ".v1.bak.parquet"`
- Produces（`tdxfeed.parquet_io`）：
  - `write_table(path: Path, df: pd.DataFrame, date_col: str = "date") -> Path`
  - `read_daily(src: str | Path, columns=None) -> pd.DataFrame`
  - `write_daily(df: pd.DataFrame, code: str) -> Path`
  - `read_minute(code: str, period: str, parse_time: bool = False) -> pd.DataFrame`
  - `read_derived(code: str | Path) -> pd.DataFrame`
- Produces（`v5/scripts/_paths`）：`ROOT`、`DATA_ROOT`、`DAILY_DIR`、`ADJ_DIR`、`daily_dir()`、`daily_path()`、`daily_files()`、`minute_dir()`、`derived_dir()`、`adj_factor_dir()`、`adj_factor_files()`、`parquet_root()`——语义与主包逐字相同，但不 `import tdxfeed`。

- [ ] **Step 1: 写失败测试**

`tests/test_data_layer.py`：

```python
"""路径访问器只定位不 fallback；写入口是 date32 + 原子写；v5 孪生与主包同解。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from tdxfeed import parquet_io, paths

ROOT = Path(__file__).resolve().parents[1]


def test_table_dirs_share_one_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.parquet_root() == tmp_path / "parquet"
    assert paths.daily_dir() == tmp_path / "parquet" / "daily"
    assert paths.derived_dir() == tmp_path / "parquet" / "derived"
    assert paths.adj_factor_dir() == tmp_path / "adj_factor"
    assert paths.hfq_factor_dir() == tmp_path / "hfq_factor"
    assert paths.finance_parquet() == tmp_path / "parquet" / "finance.parquet"
    assert paths.finance_deep_dir() == tmp_path / "parquet" / "finance_deep"
    assert paths.xdxr_path() == tmp_path / "parquet" / "xdxr.parquet"


def test_minute_dir_accepts_both_spellings(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    assert paths.minute_dir("60") == paths.minute_dir("min60") == tmp_path / "parquet" / "min60"
    assert paths.minute_path("600000", "60").parent == paths.minute_dir("60")
    assert paths.minute_path("600000", "45") == tmp_path / "parquet" / "min45" / "sh600000.parquet"


def test_daily_files_excludes_migration_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.daily_dir()
    d.mkdir(parents=True)
    for name in ("sh600000.parquet", "sh600000.v1.bak.parquet", "sz000001.parquet",
                 "_audit.csv"):
        (d / name).write_bytes(b"")
    assert [p.name for p in paths.daily_files()] == ["sh600000.parquet", "sz000001.parquet"]


def test_adj_factor_files_lists_table(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    d = paths.adj_factor_dir()
    d.mkdir(parents=True)
    (d / "000001.parquet").write_bytes(b"")
    (d / "600000.parquet").write_bytes(b"")
    assert [p.name for p in paths.adj_factor_files()] == ["000001.parquet", "600000.parquet"]


def _bars(n: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n, freq="D"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "close": [10.1] * n, "volume": [1e6] * n, "amount": [1e7] * n,
    })


def test_write_table_forces_date32_and_leaves_no_tmp(tmp_path):
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, _bars())
    assert str(pq.read_schema(fp).field("date").type) == "date32[day]"
    assert [p.name for p in tmp_path.iterdir()] == ["x.parquet"]


def test_write_table_replaces_inode_not_overwrites(tmp_path):
    """硬链接快照的前提：写盘必须换 inode，否则 cp -al 备份会被就地改写穿透。"""
    fp = tmp_path / "x.parquet"
    parquet_io.write_table(fp, _bars())
    before = os.stat(fp).st_ino
    parquet_io.write_table(fp, _bars(8))
    assert os.stat(fp).st_ino != before


def test_read_daily_sorts_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    df = _bars(4).iloc[::-1].reset_index(drop=True)          # 倒序
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)     # 重复日期
    (paths.daily_dir()).mkdir(parents=True)
    df.to_parquet(paths.daily_path("600000"), index=False)    # 故意写成 timestamp
    out = parquet_io.read_daily("600000")
    assert out["date"].is_monotonic_increasing
    assert len(out) == 4
    assert str(out["date"].dtype).startswith("datetime64")


def test_write_daily_uses_canonical_name(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    fp = parquet_io.write_daily(_bars(), "600000")
    assert fp.name == "sh600000.parquet"


def test_read_minute_keeps_string_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    paths.minute_dir("60").mkdir(parents=True)
    df = pd.DataFrame({"datetime": ["2026-10-27 15:00:00", "2026-10-27 14:00:00"],
                       "close": [10.0, 10.1]})
    df.to_parquet(paths.minute_path("600000", "60"), index=False)
    raw = parquet_io.read_minute("600000", "60")
    assert raw["datetime"].iloc[0] == "2026-10-27 14:00:00"
    assert raw["datetime"].dtype == object
    parsed = parquet_io.read_minute("600000", "60", parse_time=True)
    assert str(parsed["datetime"].dtype).startswith("datetime64")


def test_missing_table_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        parquet_io.read_daily("600000")


def _load_v5_paths():
    spec = importlib.util.spec_from_file_location("v5_paths_under_test",
                                                  ROOT / "v5/scripts/_paths.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v5_twin_agrees_with_main_package(tmp_path, monkeypatch):
    v5 = _load_v5_paths()
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(v5, "DATA_ROOT", tmp_path)
    for code in ("600519", "000001", "920001", "sh600519", "bj920001", "300750"):
        assert v5.daily_path(code) == paths.daily_path(code), code
        assert v5.adj_factor_path(code) == paths.adj_factor_path(code), code
    for period in ("5", "15", "60", "min60"):
        assert v5.minute_dir(period) == paths.minute_dir(period), period
    assert v5.derived_path("sh600519") == paths.derived_path("sh600519")
    assert v5.DAILY_BAK_SUFFIX == paths.DAILY_BAK_SUFFIX


def test_v5_twin_does_not_import_main_package():
    src = (ROOT / "v5/scripts/_paths.py").read_text(encoding="utf-8")
    assert "import tdxfeed" not in src
    assert "from tdxfeed" not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_data_layer.py -q`
Expected: FAIL — `AttributeError: module 'tdxfeed.paths' has no attribute 'parquet_root'`（第一个用例），其余因缺少 `tdxfeed.parquet_io` / `v5/scripts/_paths.py` 而 collection error。

- [ ] **Step 3: 实现 `tdxfeed/paths.py` 的扩面**

在 `daily_dir()` 之前插入（`_TABLE_DIRS` 不需要，不要留没用的常量）：

```python
DAILY_BAK_SUFFIX = ".v1.bak.parquet"


def _minute_name(period: str) -> str:
    p = str(period).strip().lower()
    return p if p.startswith("min") else f"min{p.lstrip('m')}"


def parquet_root() -> Path:
    return DATA_ROOT / "parquet"


def adj_factor_dir() -> Path:
    return DATA_ROOT / "adj_factor"


def hfq_factor_dir() -> Path:
    return DATA_ROOT / "hfq_factor"


def derived_dir() -> Path:
    return parquet_root() / "derived"


def minute_dir(period: str) -> Path:
    return parquet_root() / _minute_name(period)


def finance_parquet() -> Path:
    return parquet_root() / "finance.parquet"


def finance_deep_dir() -> Path:
    return parquet_root() / "finance_deep"


def xdxr_path() -> Path:
    return parquet_root() / "xdxr.parquet"


def daily_files() -> list[Path]:
    """active daily 文件，排除 .v1.bak 迁移备份；按文件名排序。"""
    return sorted(p for p in daily_dir().glob("*.parquet")
                  if not p.name.endswith(DAILY_BAK_SUFFIX))


def adj_factor_files() -> list[Path]:
    return sorted(adj_factor_dir().glob("*.parquet"))
```

同时把既有的三处改成走新访问器（行为不变，只去掉重复的路径拼装）：

```python
def daily_dir() -> Path:
    return parquet_root() / "daily"


def minute_path(code: str, period: str) -> Path:
    return minute_dir(period) / f"{exchange_prefix(code)}{_six(code)}.parquet"


def derived_path(code: str) -> Path:
    return derived_dir() / f"{exchange_prefix(code)}{_six(code)}_indicators.parquet"
```

- [ ] **Step 4: 实现 `tdxfeed/parquet_io.py`**

```python
"""行情表的读写入口：date 口径与写盘原子性只在这里定义。

写盘用 tmp → rename 而不是就地 to_parquet：存量 derived 的备份是
`cp -al` 硬链接快照，就地改写会穿透硬链接把备份一起改掉。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .paths import daily_path, derived_path, minute_path, read_path


def _as_date32(df: pd.DataFrame) -> pd.Series:
    s = df["date"]
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.date
    if pd.api.types.is_integer_dtype(s):
        return pd.to_datetime(s.astype("int64").astype(str), format="%Y%m%d").dt.date
    return pd.to_datetime(s, errors="coerce").dt.date


def write_table(path: Path, df: pd.DataFrame, date_col: str = "date") -> Path:
    """写 parquet：date 列强制 date32[day]；先写 .tmp 再 rename。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    if date_col in df.columns:
        idx = table.schema.get_field_index(date_col)
        table = table.set_column(idx, date_col, pa.array(_as_date32(df), type=pa.date32()))
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="snappy")
    os.replace(tmp, path)
    return path


def read_daily(src: str | Path, columns=None) -> pd.DataFrame:
    """src 传代码（sh600519 / 600519）或 .parquet 路径；date→datetime64，升序去重。"""
    fp = Path(src) if str(src).endswith(".parquet") else daily_path(src)
    df = pd.read_parquet(read_path(fp), columns=columns)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date", kind="mergesort").drop_duplicates("date", keep="last")
    return df.reset_index(drop=True)


def write_daily(df: pd.DataFrame, code: str) -> Path:
    return write_table(daily_path(code), df)


def read_minute(code: str, period: str, parse_time: bool = False) -> pd.DataFrame:
    """分钟表时间列是 string（YYYY-MM-DD HH:MM:SS）；parse_time=True 才转 datetime64。"""
    df = pd.read_parquet(read_path(minute_path(code, period)))
    if parse_time:
        df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime", kind="mergesort").reset_index(drop=True)


def read_derived(code: str | Path) -> pd.DataFrame:
    fp = Path(code) if str(code).endswith(".parquet") else derived_path(code)
    return pd.read_parquet(read_path(fp))
```

- [ ] **Step 5: 实现 `v5/scripts/_paths.py`**

```python
"""v5 脚本的路径定位：与主包零耦合（不 import tdxfeed），读同一个 TDX_OUT_ROOT。

与 tdxfeed/paths.py 的一致性由 tests/test_data_layer.py 的 parity 用例锁住。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("TDX_OUT_ROOT", ROOT / "data"))

DAILY_BAK_SUFFIX = ".v1.bak.parquet"


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


def parquet_root() -> Path:
    return DATA_ROOT / "parquet"


def daily_dir() -> Path:
    return parquet_root() / "daily"


def adj_factor_dir() -> Path:
    return DATA_ROOT / "adj_factor"


def derived_dir() -> Path:
    return parquet_root() / "derived"


def _minute_name(period: str) -> str:
    p = str(period).strip().lower()
    return p if p.startswith("min") else f"min{p.lstrip('m')}"


def minute_dir(period: str) -> Path:
    return parquet_root() / _minute_name(period)


def daily_path(code: str) -> Path:
    return daily_dir() / f"{exchange_prefix(code)}{_six(code)}.parquet"


def adj_factor_path(code: str) -> Path:
    return adj_factor_dir() / f"{_six(code)}.parquet"


def derived_path(code: str) -> Path:
    return derived_dir() / f"{exchange_prefix(code)}{_six(code)}_indicators.parquet"


def daily_files() -> list[Path]:
    return sorted(p for p in daily_dir().glob("*.parquet")
                  if not p.name.endswith(DAILY_BAK_SUFFIX))


def adj_factor_files() -> list[Path]:
    return sorted(adj_factor_dir().glob("*.parquet"))


DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()
```

- [ ] **Step 6: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_data_layer.py -q`
Expected: PASS（14 passed）。若 `test_read_daily_sorts_and_dedups` 失败于 `to_parquet` 写 timestamp 时的 dtype，确认它是**故意**的（测试要证明读入口会把 timestamp 归一成 datetime64 并去重）。

- [ ] **Step 7: 提交**

```bash
git add tdxfeed/paths.py tdxfeed/parquet_io.py v5/scripts/_paths.py tests/test_data_layer.py docs/superpowers/plans/2026-10-28-data-authority-phase34.md
git commit -m "feat(paths): 表目录访问器 + parquet_io 单一写入口 + v5 零耦合孪生

date32 归一与 tmp→rename 原子写集中到 write_table；derived 备份依赖该原子性。
v5/_paths 与主包逐字同解，parity 用例锁住漂移。"
```

---

### Task 2: 硬编码数据路径守卫

**Files:**
- Test: `tests/test_no_hardcoded_data_paths.py`

**Interfaces:**
- Produces: 模块级 `violations(path: Path) -> list[int]`、`scan() -> dict[str, list[int]]`；`python -m pytest tests/test_no_hardcoded_data_paths.py -q` 在有违例时失败并打印 `文件: 行号列表`。

- [ ] **Step 1: 写守卫（先让它红）**

`tests/test_no_hardcoded_data_paths.py`：

```python
"""业务代码不得写死数据表路径。

规则三条（AST，非正则，避免注释与文档字符串误伤）：
  R1 路径类调用的直接参数里出现可疑字面量；
  R2 任何 `a / b` 拼接里同时出现 "data" 与表段名；
  R3 单行字面量赋值/返回（≤160 字符）里出现可疑字面量。
白名单只有两个模块：它们就是权威层本身。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FLAT = re.compile(r"data[/\\](?:parquet\b|adj_factor\b|hfq_factor\b|finance\b)")
SEGMENTS = {"parquet", "adj_factor", "hfq_factor", "finance", "finance.parquet", "xdxr.parquet"}
PATH_CALLS = {
    "Path", "read_parquet", "to_parquet", "read_csv", "to_csv", "open",
    "glob", "exists", "read_text", "write_text", "listdir", "mkdir",
    "iterdir", "rglob",
}
WHITELIST = {"tdxfeed/paths.py", "v5/scripts/_paths.py"}
SKIP_DIRS = {"venv", "venv-math", "_archive", "node_modules", ".git", ".qoder", "tests"}


def _strs(node: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _suspect(node: ast.AST) -> bool:
    values = _strs(node)
    if any(FLAT.search(v) for v in values):
        return True
    return "data" in values and any(v in SEGMENTS for v in values)


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def violations(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) in PATH_CALLS:
            direct = [a for a in [*node.args, *node.keywords]
                      if isinstance(a, (ast.Constant, ast.JoinedStr, ast.BinOp))]
            if any(_suspect(a) for a in direct):
                lines.add(node.lineno)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _suspect(node):
                lines.add(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Return)):
            value = getattr(node, "value", None)
            if not isinstance(value, (ast.Constant, ast.JoinedStr)):
                continue
            text = "".join(_strs(value))
            if "\n" in text or len(text) > 160:
                continue
            if _suspect(value):
                lines.add(node.lineno)
    return sorted(lines)


def scan() -> dict[str, list[int]]:
    bad: dict[str, list[int]] = {}
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.as_posix() in WHITELIST:
            continue
        try:
            found = violations(p)
        except SyntaxError:
            continue
        if found:
            bad[rel.as_posix()] = found
    return bad


def _report(bad: dict[str, list[int]]) -> str:
    rows = [f"{f}  {lines}" for f, lines in sorted(bad.items())[:20]]
    tail = "" if len(bad) <= 20 else f"\n... 另有 {len(bad) - 20} 个文件"
    return (f"{len(bad)} 文件 / {sum(len(v) for v in bad.values())} 行写死了数据表路径：\n"
            + "\n".join(rows) + tail)


def test_no_hardcoded_table_paths():
    bad = scan()
    assert not bad, _report(bad)


def test_guard_catches_the_three_rules(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from pathlib import Path\n"
        "ROOT = Path('/x')\n"
        "A = ROOT / 'data' / 'parquet' / 'daily'\n"
        "B = '/home/u/proj/data/adj_factor'\n"
        "C = open('data/parquet/finance.parquet')\n",
        encoding="utf-8",
    )
    assert violations(sample) == [3, 4, 5]


def test_guard_ignores_comments_and_docstrings(tmp_path):
    sample = tmp_path / "ok.py"
    sample.write_text(
        '"""数据在 data/parquet/daily 下（只是说明文字）。"""\n'
        "# data/adj_factor 也是\n"
        "META = {'note': 'data/parquet'}\n",
        encoding="utf-8",
    )
    assert violations(sample) == []


def test_guard_whitelists_the_authority_modules():
    assert "tdxfeed/paths.py" in WHITELIST
    assert "v5/scripts/_paths.py" in WHITELIST
    for rel in WHITELIST:
        assert (ROOT / rel).exists(), rel
```

- [ ] **Step 2: 跑守卫确认红，并记录基线**

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q`
Expected: `test_guard_catches_the_three_rules` / `test_guard_ignores_comments_and_docstrings` / `test_guard_whitelists_the_authority_modules` PASS，`test_no_hardcoded_table_paths` FAIL，报 **90 文件 / 119 行**（分组：`scripts` 58/80、`tdxfeed` 6/7、`quant` 2/2、`mcp` 3/7、`train` 2/3、`v5` 19/20）。

若数字与上面不一致：以实际输出为准，把差异追加到本文档 Task 3–6 的表格里，**不要**为了对齐数字去放宽规则（规则一松，守卫就失去意义）。

- [ ] **Step 3: 提交守卫（红着提交，后续按 Task 清零）**

```bash
git add tests/test_no_hardcoded_data_paths.py
git commit -m "test(paths): 硬编码数据表路径 AST 守卫（当前 90 文件/119 行待清）"
```

---

### Task 3: scripts 批量迁移（58 文件 / 80 行）

**Files:**
- Modify: `scripts/_common.py`
- Create: `tests/test_common_reexports.py`
- Modify: 下表 58 个 `scripts/*.py`
- Test: `tests/test_no_hardcoded_data_paths.py`（Task 2 已建）

**Interfaces:**
- Consumes: Task 1 的 `tdxfeed.paths` 访问器（`parquet_root` / `adj_factor_dir` / `hfq_factor_dir` / `derived_dir` / `minute_dir` / `xdxr_path` / `daily_files` / `adj_factor_files` 等）。
- Produces: `scripts/_common.py` 再导出 12 个名字（`DATA_ROOT`、`daily_dir`、`daily_files`、`daily_path`、`adj_factor_dir`、`adj_factor_files`、`hfq_factor_dir`、`minute_dir`、`parquet_root`、`derived_dir`、`xdxr_path`、`read_path`）。脚本侧只加一行 `from _common import <names>  # noqa: E402`，不再各自拼路径。

- [ ] **Step 1: 写再导出的失败测试**

`tests/test_common_reexports.py`：

```python
"""scripts/_common.py 是路径访问器的再导出桥：名字必须真存在、已声明、且是同一对象。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "DATA_ROOT", "daily_dir", "daily_files", "daily_path",
    "adj_factor_dir", "adj_factor_files", "hfq_factor_dir",
    "minute_dir", "parquet_root", "derived_dir", "xdxr_path", "read_path",
}


def _load_common():
    spec = importlib.util.spec_from_file_location(
        "scripts_common_under_test", ROOT / "scripts/_common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_common_reexports_every_path_accessor():
    common = _load_common()
    assert sorted(n for n in EXPECTED if not hasattr(common, n)) == []


def test_reexports_are_declared_in_all():
    common = _load_common()
    assert sorted(EXPECTED - set(common.__all__)) == []


def test_reexports_are_the_authority_objects():
    common = _load_common()
    from tdxfeed import paths
    for name in sorted(EXPECTED):
        assert getattr(common, name) is getattr(paths, name), name
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_common_reexports.py -q`
Expected: FAIL — 前两个用例把 12 个名字全列进差异（`_common` 还没有这些再导出）；第三个用例 `AttributeError`。

- [ ] **Step 3: 给 `scripts/_common.py` 加再导出**

在 `from tdxfeed.features_5d import (...)` 之后插入：

```python
from tdxfeed.paths import (  # noqa: E402
    DATA_ROOT,
    adj_factor_dir,
    adj_factor_files,
    daily_dir,
    daily_files,
    daily_path,
    derived_dir,
    hfq_factor_dir,
    minute_dir,
    parquet_root,
    read_path,
    xdxr_path,
)
```

把 `__all__` 整个替换为（原 11 项 + 新 12 项，按字母序）：

```python
__all__ = [
    "DATA_ROOT",
    "adj_factor_dir",
    "adj_factor_files",
    "compute_window_features",
    "compute_window_features_extended",
    "daily_dir",
    "daily_files",
    "daily_path",
    "derived_dir",
    "get_limit",
    "hfq_factor_dir",
    "is_bj",
    "is_limit_up_ret",
    "is_open_limit_up",
    "is_yizi",
    "minute_dir",
    "norm_code",
    "parquet_root",
    "parse_date",
    "parse_date_series",
    "read_path",
    "to_date_str",
    "xdxr_path",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_common_reexports.py -q`
Expected: `3 passed`。

- [ ] **Step 5: 按表批量改写（每个文件一次 import，逐行换右值）**

编辑规则，**逐文件**执行：

1. 同一文件有多行时，**从行号大的往小的改**（先改下面的行），避免行号漂移。
2. 用 Read 确认该行内容确实是「常量赋值 + `ROOT / ...`」或 `Path("...")` 形态；把 `=` 右边整个表达式换成「改写」列的表达式（左值变量名保持不变）。
3. 在该文件**被改的最小行号**那一行的上方插一行 `from _common import <导入名>  # noqa: E402`（每文件只插一次）。若该文件已有 `from _common import ...`（`bench_v567` / `bench_v7r` / `build_cls2000` / `daban_label_diagnose` / `find_d_signal_5d` / `llm_signal_plan_e` / `plan_e_backtest` / `plan_e_sim_live` / `predict` / `sim_daily_compare` 这 10 个），仍单独插一行，不合并——规则统一才好机械复核。
4. `scripts/backfill_minutes.py:35` 照改（`OUT = parquet_root()`，插 `from _common import parquet_root`），但**这个文件不进本任务的 commit**（他人未提交改动在上面）。

### scripts/ — 58 文件 / 80 行

| 文件 | 插入行位置 | 导入名 | 逐行改写 |
|---|---|---|---|
| `scripts/analyze_one.py` | 12 | daily_path | 见 Step 6（MANUAL） |
| `scripts/analyze_pre90.py` | 24 | daily_dir, adj_factor_dir | 24→`PARQUET = daily_dir()`; 25→`ADJ = adj_factor_dir()` |
| `scripts/audit_daily_adjust_convention.py` | 32 | daily_dir, adj_factor_dir | 32→`DAILY = daily_dir()`; 33→`ADJ = adj_factor_dir()` |
| `scripts/audit_daily_history_depth.py` | 39 | daily_dir | 39→`DAILY = daily_dir()` |
| `scripts/backfill_adj_etf.py` | 30 | adj_factor_dir | 30→`OUT = adj_factor_dir()` |
| `scripts/backfill_daily_amount.py` | 32 | daily_dir, DATA_ROOT | 见 Step 6（MANUAL） |
| `scripts/backfill_minutes.py` | 35 | parquet_root | 35→`OUT = parquet_root()`（**不提交**） |
| `scripts/backtest_daban_topk_pnl.py` | 39 | daily_dir, adj_factor_dir | 39→`DAILY_DIR = daily_dir()`; 40→`ADJ_DIR = adj_factor_dir()` |
| `scripts/bench_v567.py` | 31 | daily_dir | 31→`PARQUET_DIR = daily_dir()` |
| `scripts/bench_v7r.py` | 31 | daily_dir | 31→`PARQUET_DIR = daily_dir()` |
| `scripts/build_cls2000.py` | 36 | daily_dir | 36→`PARQUET_DIR = daily_dir()` |
| `scripts/build_distill_skeleton.py` | 8 | daily_files | 见 Step 6（MANUAL） |
| `scripts/build_v4_forward_labels.py` | 27 | daily_dir | 27→`PK = daily_dir()` |
| `scripts/canary_health_check.py` | 144 | daily_dir, adj_factor_dir | 144→`pq_dir = daily_dir()`; 145→`adj_dir = adj_factor_dir()` |
| `scripts/check_schema.py` | 25 | daily_dir, adj_factor_dir | 25→`DAILY_DIR = daily_dir()`; 26→`ADJ_DIR = adj_factor_dir()` |
| `scripts/clean_double_up_detail.py` | 34 | daily_dir, adj_factor_dir | 34→`PARQUET = daily_dir()`; 35→`ADJ = adj_factor_dir()` |
| `scripts/daban_alpha_sources.py` | 32 | daily_dir | 32→`DAILY_DIR = daily_dir()` |
| `scripts/daban_backtest.py` | 41 | daily_dir, adj_factor_dir | 41→`DAILY_DIR = daily_dir()`; 42→`ADJ_DIR = adj_factor_dir()` |
| `scripts/daban_event_table.py` | 50 | minute_dir | 50→`MIN60_DIR = minute_dir("min60")` |
| `scripts/daban_intraday_ic.py` | 34 | daily_dir | 34→`DAILY_DIR = daily_dir()` |
| `scripts/daban_label_diagnose.py` | 58 | daily_dir | 58→`PARQUET = daily_dir()` |
| `scripts/daban_min60_features.py` | 30 | minute_dir, daily_dir | 30→`MIN60_DIR = minute_dir("min60")`; 31→`DAILY_DIR = daily_dir()` |
| `scripts/daban_relay3_robustness.py` | 27 | daily_dir | 27→`DAILY_DIR = daily_dir()` |
| `scripts/daban_relturn_validation.py` | 26 | daily_dir | 26→`DAILY_DIR = daily_dir()` |
| `scripts/daban_sector_features.py` | 43 | daily_dir | 43→`DAILY_DIR = daily_dir()` |
| `scripts/download_adj_factor_b.py` | 19 | adj_factor_dir | 19→`OUT_DIR = adj_factor_dir()` |
| `scripts/download_adj_factor_bj.py` | 20 | adj_factor_dir | 20→`OUT_DIR = adj_factor_dir()` |
| `scripts/download_adj_factor_watch.py` | 12 | adj_factor_dir | 12→`ADJ_DIR = adj_factor_dir()` |
| `scripts/download_bj_daily.py` | 16 | daily_dir | 16→`PARQUET_DIR = daily_dir()` |
| `scripts/download_etf_bond.py` | 40 | daily_dir | 40→`DAILY_DIR = daily_dir()` |
| `scripts/download_hfq_factor.py` | 37 | hfq_factor_dir | 37→`OUT_DIR = hfq_factor_dir()` |
| `scripts/download_universe.py` | 18 | daily_dir, daily_files | 见 Step 6（MANUAL） |
| `scripts/download_valuation.py` | 49 | daily_dir | 49→`DAILY_DIR = daily_dir()` |
| `scripts/factor_engine.py` | 40 | daily_dir, adj_factor_dir | 40→`PARQUET = daily_dir()`; 41→`ADJ_DIR = adj_factor_dir()` |
| `scripts/find_d_signal_5d.py` | 31 | daily_dir | 31→`PARQUET = daily_dir()` |
| `scripts/find_double_30d.py` | 32 | daily_dir, adj_factor_dir | 32→`PARQUET = daily_dir()`; 33→`ADJ = adj_factor_dir()` |
| `scripts/find_double_up_c.py` | 18 | daily_dir | 18→`PARQUET = daily_dir()` |
| `scripts/find_double_up_c_v2.py` | 28 | daily_dir, adj_factor_dir | 28→`PARQUET = daily_dir()`; 29→`ADJ = adj_factor_dir()` |
| `scripts/hengpan_t1_eval.py` | 56 | daily_dir | 56→`DAILY_DIR = daily_dir()` |
| `scripts/llm_signal_plan_e.py` | 40 | daily_dir | 40→`PARQUET_DIR = daily_dir()` |
| `scripts/migrate_v1_to_v2.py` | 36 | daily_dir, adj_factor_dir | 36→`DAILY_DIR = daily_dir()`; 37→`ADJ_DIR = adj_factor_dir()` |
| `scripts/monitor_plan_e_live.py` | 69 | daily_dir | 69→`PARQUET = daily_dir()` |
| `scripts/plan_e_backtest.py` | 85 | daily_dir | 85→`PARQUET_DIR = daily_dir()` |
| `scripts/plan_e_journal.py` | 145 | daily_dir | 145→`PARQUET = daily_dir()`; 339→`PARQUET = daily_dir()` |
| `scripts/plan_e_sim_live.py` | 46 | daily_dir | 46→`PARQUET_DIR = daily_dir()` |
| `scripts/plan_e_v3_live_track.py` | 51 | daily_dir | 51→`PARQUET = daily_dir()` |
| `scripts/predict.py` | 88 | daily_dir, adj_factor_dir | 88→`PARQUET_DIR = daily_dir()`; 89→`ADJ_DIR = adj_factor_dir()` |
| `scripts/sim_daily_compare.py` | 42 | daily_dir | 42→`PARQUET_DIR = daily_dir()` |
| `scripts/top10_deep_dive.py` | 21 | daily_dir | 21→`PARQUET = daily_dir()` |
| `scripts/train_daban_model.py` | 44 | daily_dir | 44→`PARQUET = daily_dir()` |
| `scripts/trend_hold_eval.py` | 56 | daily_dir, adj_factor_dir | 56→`DAILY_DIR = daily_dir()`; 57→`ADJ_DIR = adj_factor_dir()` |
| `scripts/v3_premarket_top.py` | 29 | daily_dir, adj_factor_dir | 29→`PARQUET = daily_dir()`; 30→`ADJ = adj_factor_dir()` |
| `scripts/v3_simulate.py` | 23 | daily_dir | 23→`PARQUET = daily_dir()` |
| `scripts/v3_simulate_rolling.py` | 30 | daily_dir | 30→`PARQUET = daily_dir()` |
| `scripts/verify_daban_hit.py` | 32 | daily_dir | 32→`PARQUET_DIR = daily_dir()` |
| `scripts/verify_training_data_parity.py` | 27 | daily_dir | 27→`DAILY = daily_dir()` |
| `scripts/verify_v2.py` | 38 | daily_dir, adj_factor_dir, hfq_factor_dir, minute_dir | 38→`DAILY_DIR = daily_dir()`; 39→`ADJ_DIR = adj_factor_dir()`; 40→`HFQ_DIR = hfq_factor_dir()`; 41→`MIN15_DIR = minute_dir("min15")`; 42→`MIN5_DIR = minute_dir("min5")` |
| `scripts/xdxr_verify.py` | 34 | daily_dir, xdxr_path | 34→`DAILY_DIR = daily_dir()`; 35→`XDXR_PARQUET = xdxr_path()` |

- [ ] **Step 6: 4 个字符串拼接型文件（守卫抓不到，必须手工改）**

**6.1 `scripts/backfill_daily_amount.py`**（已有 `sys.path` 引导与 `from tdxfeed.amount_fix import ...`，不插 `_common`，直接扩展 tdxfeed import 区）：

```python
from tdxfeed.amount_fix import backfill_daily_dir  # noqa: E402
from tdxfeed.paths import DATA_ROOT, daily_dir  # noqa: E402
```

`main()` 里三行改成：

```python
    ap.add_argument("--daily-dir", default=str(daily_dir()))
    ap.add_argument("--out-root", default=str(DATA_ROOT))
    ap.add_argument("--backup-dir", default=str(daily_dir().with_name("daily_amount_bak")),
                    help="落盘前的原文件备份目录")
```

（`daily_dir().with_name("daily_amount_bak")` == `<root>/parquet/daily_amount_bak`，与原来同义。`--out-root` 原来就是 `data/` 根，用 `DATA_ROOT` 而不是 `parquet_root()`。）

**6.2 `scripts/analyze_one.py`**（`PK` 是带尾斜杠的 str，`PK + sym + '.parquet'` 拼接）：

- 删除 `PK = '/home/jiuben/tdx-data-feed/data/parquet/daily/'`（L12），在原位换一行：
  `from _common import daily_path  # noqa: E402`
- L38 改成：
  `    df = pd.read_parquet(daily_path(sym)).sort_values('date').reset_index(drop=True)`
- `sym` 来自 `sys.argv[1]`，默认 `'sh601988'`（带前缀）；`daily_path('sh601988')` → `sh601988.parquet`，同解。

**6.3 `scripts/build_distill_skeleton.py`**（`PK` 供 `glob.glob(PK + '*.parquet')`）：

- 删除 `PK = '/home/jiuben/tdx-data-feed/data/parquet/daily/'`（L8），在原位换一行：
  `from _common import daily_files  # noqa: E402`
- 删除 `import glob`（L1；改完后本文件不再用 glob）。
- L64 改成：`    files = daily_files()`
- 行为有意变化：`daily_files()` 排除 `*.v1.bak.parquet`，因此 skeleton 不再把 5232 个 v1 备份当样本——这是本计划要的（canonical 只留活跃文件）。

**6.4 `scripts/download_universe.py`**（已有 `sys.path.insert` 与 `from tdxfeed...`）：

- import 区加一行：`from tdxfeed.paths import daily_dir, daily_files`
- L18：`OUT = daily_dir()`
- L62：`    existing = {p.stem for p in daily_files()}`
- L84：`        df.to_parquet(OUT / f"{s}.parquet", index=False)`
- 删除 `import glob`、`import os`（改后两者在本文件都没有使用；`os.path.basename` 与 `glob.glob` 都被 `daily_files()` 替掉）。
- 注意：这里只换路径，**不动**它写 `int64` 日期的问题——那是 Task 8 的活。

- [ ] **Step 7: 复核 import 名、跑 lint 与守卫**

先机械核对表里的每个导入名都能从 `_common` 解析（避免手误）：

```bash
cd scripts && ../venv/bin/python - <<'PY'
import _common
names = "DATA_ROOT daily_dir daily_files daily_path adj_factor_dir adj_factor_files hfq_factor_dir minute_dir parquet_root derived_dir xdxr_path read_path".split()
missing = [n for n in names if not hasattr(_common, n)]
bad = [n for n in names if n not in _common.__all__]
print("missing:", missing, "not_in_all:", bad)
PY
```

Expected: `missing: [] not_in_all: []`。

Run: `make lint-all`
Expected: 没有 `F821 undefined name` / `E9` 类错误（若报错，说明某文件漏插 import 或导入名写错）。

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q`
Expected: 只剩 **32 文件 / 39 行**（`tdxfeed` 6/7、`quant` 2/2、`mcp` 3/7、`train` 2/3、`v5` 19/20）。若数字不对，按实际输出记录，并检查是否漏行。

- [ ] **Step 8: 提交（显式路径，排除 `scripts/backfill_minutes.py`）**

```bash
git add tests/test_common_reexports.py scripts/_common.py \
    scripts/analyze_one.py scripts/analyze_pre90.py scripts/audit_daily_adjust_convention.py scripts/audit_daily_history_depth.py \
    scripts/backfill_adj_etf.py scripts/backfill_daily_amount.py scripts/backtest_daban_topk_pnl.py scripts/bench_v567.py \
    scripts/bench_v7r.py scripts/build_cls2000.py scripts/build_distill_skeleton.py scripts/build_v4_forward_labels.py \
    scripts/canary_health_check.py scripts/check_schema.py scripts/clean_double_up_detail.py scripts/daban_alpha_sources.py \
    scripts/daban_backtest.py scripts/daban_event_table.py scripts/daban_intraday_ic.py scripts/daban_label_diagnose.py \
    scripts/daban_min60_features.py scripts/daban_relay3_robustness.py scripts/daban_relturn_validation.py scripts/daban_sector_features.py \
    scripts/download_adj_factor_b.py scripts/download_adj_factor_bj.py scripts/download_adj_factor_watch.py scripts/download_bj_daily.py \
    scripts/download_etf_bond.py scripts/download_hfq_factor.py scripts/download_universe.py scripts/download_valuation.py \
    scripts/factor_engine.py scripts/find_d_signal_5d.py scripts/find_double_30d.py scripts/find_double_up_c.py \
    scripts/find_double_up_c_v2.py scripts/hengpan_t1_eval.py scripts/llm_signal_plan_e.py scripts/migrate_v1_to_v2.py \
    scripts/monitor_plan_e_live.py scripts/plan_e_backtest.py scripts/plan_e_journal.py scripts/plan_e_sim_live.py \
    scripts/plan_e_v3_live_track.py scripts/predict.py scripts/sim_daily_compare.py scripts/top10_deep_dive.py \
    scripts/train_daban_model.py scripts/trend_hold_eval.py scripts/v3_premarket_top.py scripts/v3_simulate.py \
    scripts/v3_simulate_rolling.py scripts/verify_daban_hit.py scripts/verify_training_data_parity.py scripts/verify_v2.py \
    scripts/xdxr_verify.py
git commit -m "refactor(paths): 58 个 scripts 改用路径访问器（硬编码 80 行清零）"
```

提交前 `git status` 里 `scripts/backfill_minutes.py` 应仍是未暂存的修改（那是我们刚改的 `OUT = parquet_root()` 加上他人的 `--datalen`），**不要**把它加进来。

---

### Task 4: tdxfeed 包内 6 文件 / 7 行 + quant 2 行

**Files:**
- Modify: `tdxfeed/fin_parquet.py`、`tdxfeed/freshness.py`、`tdxfeed/incremental.py`、`tdxfeed/precompute/indicators.py`、`tdxfeed/stage1_wide_table.py`、`tdxfeed/kline.py`（**不提交**）、`quant/multifactor/engine.py`、`quant/multifactor/factors.py`

**Interfaces:**
- Consumes: Task 1 的 `parquet_root` / `daily_dir` / `derived_dir`。
- Produces: 无新 API；`tdxfeed.fin_parquet.OUT`、`tdxfeed.freshness.DEFAULT_PARQUET_DIR`、`tdxfeed.incremental.DAILY_DIR`、`tdxfeed.precompute.indicators.DAILY_DIR`/`DERIVED_DIR`、`tdxfeed.stage1_wide_table.DAILY`、`quant.multifactor.{engine,factors}.PARQUET_DIR` 值不变（仍是 `Path`），只是来源换成访问器。

- [ ] **Step 1: 逐文件改写**

包内模块一律**绝对导入**（`tdxfeed/__init__` 不导入 `paths`，无环）；只有 `kline.py` 需要自己引导 `sys.path`，因为它被 `scripts/backfill_minutes.py` 当**顶层模块** `kline` 导入。

**4.1 `tdxfeed/fin_parquet.py`** — 该文件有 `__main__`（`python tdxfeed/fin_parquet.py incremental` 是文档里的跑法），文件模式 `sys.path[0]` 是 `tdxfeed/`，所以先引导再导入。在 `OUT = ...` 上方插入：

```python

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tdxfeed.paths import parquet_root  # noqa: E402
```

L18 改成：`OUT = str(parquet_root())`（`OUT` 是 str 基路径，下面 4 个 `os.path.join(OUT, ...)` 不动）。

**4.2 `tdxfeed/freshness.py`** — 在 `DEFAULT_PARQUET_DIR = ...` 上方插入 `from tdxfeed.paths import daily_dir  # noqa: E402`，L27 改成：`DEFAULT_PARQUET_DIR = daily_dir()`。

**4.3 `tdxfeed/incremental.py`** — 在 `from tdxfeed.writers import Writers` 之后插入 `from tdxfeed.paths import daily_dir  # noqa: E402`，L42 改成：`DAILY_DIR = daily_dir()`。

**4.4 `tdxfeed/precompute/indicators.py`** — 在 `DAILY_DIR = ...` 上方插入 `from tdxfeed.paths import daily_dir, derived_dir  # noqa: E402`，L40/L41 改成：

```python
DAILY_DIR = daily_dir()
DERIVED_DIR = derived_dir()
```

（文档用法是 `python -m tdxfeed.precompute.indicators --all`，`-m` 模式把 cwd 放进 `sys.path`，绝对导入可用。）

**4.5 `tdxfeed/stage1_wide_table.py`** — 该文件是相对导入风格（`from .factors_candidates import ...`），跟随本地风格：在 `from .factors_polars import ...` 之后插入 `from .paths import daily_dir`，L30 改成：`DAILY = daily_dir()`。

**4.6 `tdxfeed/kline.py`**（**改但不提交**：该文件有他人未提交的 `SINA_SCALES` 改动）— `os` / `sys` 已在 L8/L10 导入，`Path` 未导入，所以用 `os.path` 引导。在 `OUT_ROOT = ...` 上方插入：

```python

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tdxfeed.paths import parquet_root  # noqa: E402
```

L24 改成：`OUT_ROOT = str(parquet_root())`（下游 3 处 `os.path.join(OUT_ROOT, ...)` 不动）。

**4.7 `quant/multifactor/engine.py`** — 在 `import pandas as pd` 之后插入 `from tdxfeed.paths import daily_dir  # noqa: E402`，L50 改成：`PARQUET_DIR = daily_dir()`（`ROOT` 还被 `SENT_DIR` 等用到，保留）。

**4.8 `quant/multifactor/factors.py`** — 同样插 `from tdxfeed.paths import daily_dir  # noqa: E402`，L19 改成：`PARQUET_DIR = daily_dir()`。

- [ ] **Step 2: 导入与取值冒烟（比 lint 更强的真实检查）**

```bash
venv/bin/python - <<'PY'
import importlib, sys
sys.path.insert(0, '.')
for m in ['tdxfeed.fin_parquet', 'tdxfeed.freshness', 'tdxfeed.incremental',
          'tdxfeed.precompute.indicators', 'tdxfeed.stage1_wide_table',
          'quant.multifactor.factors', 'quant.multifactor.engine']:
    importlib.import_module(m)
from tdxfeed import paths
import tdxfeed.fin_parquet as fp
import tdxfeed.freshness as fr
import tdxfeed.incremental as inc
import tdxfeed.precompute.indicators as ind
import tdxfeed.stage1_wide_table as s1
assert fp.OUT == str(paths.parquet_root()), fp.OUT
assert fr.DEFAULT_PARQUET_DIR == paths.daily_dir(), fr.DEFAULT_PARQUET_DIR
assert inc.DAILY_DIR == paths.daily_dir(), inc.DAILY_DIR
assert ind.DAILY_DIR == paths.daily_dir() and ind.DERIVED_DIR == paths.derived_dir()
assert s1.DAILY == paths.daily_dir(), s1.DAILY
print("tdxfeed/quant ok", fp.OUT)

sys.path.insert(0, 'tdxfeed')
import kline
assert kline.OUT_ROOT == str(paths.parquet_root()), kline.OUT_ROOT
print("kline ok", kline.OUT_ROOT)
PY
```

Expected: 两行 `ok`，无 AssertionError。

- [ ] **Step 3: 守卫与门禁**

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q`
Expected: 只剩 **24 文件 / 30 行**（`mcp` 3/7、`train` 2/3、`v5` 19/20；3+2+19=24 文件，7+3+20=30 行）。

Run: `make lint-all`
Expected: 无 `E9`/`F63`/`F7`/`F82`。

Run: `venv/bin/pytest tests/test_data_layer.py tests/test_common_reexports.py tests/test_paths_and_names.py -q`
Expected: 全绿（Task 1/3 的产物没被这一步破坏）。

- [ ] **Step 4: 提交（排除 `tdxfeed/kline.py`）**

```bash
git add tdxfeed/fin_parquet.py tdxfeed/freshness.py tdxfeed/incremental.py \
    tdxfeed/precompute/indicators.py tdxfeed/stage1_wide_table.py \
    quant/multifactor/engine.py quant/multifactor/factors.py
git commit -m "refactor(paths): tdxfeed/quant 改用路径访问器（9 行）"
```

`git status` 里 `tdxfeed/kline.py` 应仍是未暂存修改，留给它所属的会话提交。

---

### Task 5: mcp 3 文件 / 7 行 + train 2 文件 / 3 行

**Files:**
- Modify: `mcp/servers/backtest.py`、`mcp/servers/finance_deep.py`、`mcp/servers/parquet_reader.py`、`train/scripts/build_v8_dataset.py`、`train/scripts/eval_v8.py`

**Interfaces:**
- Consumes: `daily_dir` / `adj_factor_dir` / `finance_parquet` / `finance_deep_dir` / `xdxr_path` / `derived_dir` / `daily_path`。
- Produces: 无新 API；各模块的 `DAILY_DIR` / `ADJ_DIR` / `FIN_PARQUET` / `DEEP_DIR` / `FINANCE_PARQUET` / `XDXR_PARQUET` / `DAILY_DIR`+`DERIVED_DIR` 值不变。

MCP server 与 train 脚本都不在仓库根下运行（`sys.path[0]` 分别是 `mcp/servers` / `train/scripts`），必须先插仓库根。三者原本就 `import numpy/pandas`，所以 `import tdxfeed`（会拉 `features_5d` / `freshness`）**不新增依赖要求**。

- [ ] **Step 1: mcp 三个 server**

**5.1 `mcp/servers/backtest.py`** — 已有 `sys.path` 引导，但它在常量**下面**，必须把常量挪到引导之后。把 L45–L52 这段：

```python
ROOT = Path("/home/jiuben/tdx-data-feed")
DAILY_DIR = ROOT / "data/parquet/daily"
ADJ_DIR = ROOT / "data/adj_factor"
DABAN_MODEL = ROOT / "data/double_up/d_signal_5d_model_daban.txt"
UNIVERSE_FILE = ROOT / "data/all_a_stocks.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

替换成：

```python
ROOT = Path("/home/jiuben/tdx-data-feed")
DABAN_MODEL = ROOT / "data/double_up/d_signal_5d_model_daban.txt"
UNIVERSE_FILE = ROOT / "data/all_a_stocks.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import adj_factor_dir, daily_dir  # noqa: E402

DAILY_DIR = daily_dir()
ADJ_DIR = adj_factor_dir()
```

（`from tdxfeed.daban.limit_price import ...` 那一组还在下面，不动。）

**5.2 `mcp/servers/finance_deep.py`** — 无引导，在 `ROOT = ...` 之后插入引导与导入，并改两个常量：

```python
ROOT = Path("/home/jiuben/tdx-data-feed")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import finance_deep_dir, finance_parquet  # noqa: E402

FIN_PARQUET = finance_parquet()
DEEP_DIR = finance_deep_dir()
DEEP_DIR.mkdir(parents=True, exist_ok=True)
```

（`DEEP_DIR.mkdir(...)` 必须留在赋值之后；`UNIVERSE_FILE` / `CACHE_TTL_DAYS` 不动。）

**5.3 `mcp/servers/parquet_reader.py`** — 同样处理：

```python
ROOT = Path("/home/jiuben/tdx-data-feed")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import daily_dir, finance_parquet, xdxr_path  # noqa: E402

DAILY_DIR = daily_dir()
FINANCE_PARQUET = finance_parquet()
XDXR_PARQUET = xdxr_path()
```

（`LOG_FILE = ROOT / "logs/mcp.log"` 及其 `mkdir` 留在后面不动。）

- [ ] **Step 2: train 两个脚本**

**5.4 `train/scripts/build_v8_dataset.py`** — import 区加 `import sys`（现在没有）；在 `ROOT = ...` 之后插入引导，并把两个目录常量改成访问器：

```python
ROOT = Path("/home/jiuben/tdx-data-feed")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import daily_dir, derived_dir  # noqa: E402

DAILY_DIR = daily_dir()
DERIVED_DIR = derived_dir()
```

（`OUT_DIR = ROOT / "train/data"` 不是行情表路径，不动。）

**5.5 `train/scripts/eval_v8.py`** — import 区加 `import sys`、删 `import glob`（改完后本文件不再用 glob）；在 import 区之后、`BASE = ...` 之前插入：

```python
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.paths import daily_path  # noqa: E402
```

把 L47–L53 这段（`if code not in _PARQUET_CACHE:` 整个分支）替换成：

```python
    if code not in _PARQUET_CACHE:
        fp = daily_path(code)
        if not fp.exists():
            _PARQUET_CACHE[code] = None
        else:
            try:
                _PARQUET_CACHE[code] = (
                    pd.read_parquet(fp).sort_values("date").reset_index(drop=True)
                )
            except Exception:
                _PARQUET_CACHE[code] = None
```

顺带修掉一个真 bug：原来的 `glob("*000001.parquet")` 对 `code="000001"` 会同时命中 `sh000001`（上证指数）和 `sz000001`（平安银行），`cands[0]` 排序后取到的是**指数**。`daily_path("000001")` 按 `exchange_prefix` 给 `sz000001`。

- [ ] **Step 3: 冒烟（importlib 真加载 + 既有契约测试）**

```bash
venv/bin/python - <<'PY'
import importlib.util, sys
sys.path.insert(0, '.')
for name in ['backtest', 'finance_deep', 'parquet_reader']:
    spec = importlib.util.spec_from_file_location(f"mcp_{name}", f"mcp/servers/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print(name, "ok")
PY
venv/bin/pytest tests/test_daban_mcp.py -q
venv/bin/python -m py_compile train/scripts/build_v8_dataset.py train/scripts/eval_v8.py && echo compile-ok
```

Expected: 三行 `ok`、`test_daban_mcp` 全绿、`compile-ok`。

- [ ] **Step 4: 守卫与门禁**

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q`
Expected: 只剩 **20 文件 / 20 行**（`v5` 19/20）。

Run: `make lint-all`
Expected: 无阻断项。

- [ ] **Step 5: 提交**

```bash
git add mcp/servers/backtest.py mcp/servers/finance_deep.py mcp/servers/parquet_reader.py \
    train/scripts/build_v8_dataset.py train/scripts/eval_v8.py
git commit -m "refactor(paths): mcp/train 改用路径访问器（10 行，顺带修 eval_v8 同码不同表）"
```

---

### Task 6: v5/scripts 19 文件 / 20 行（守卫清零）

**Files:**
- Modify: 下表 19 个 `v5/scripts/*.py`

**Interfaces:**
- Consumes: Task 1 建的 `v5/scripts/_paths.py`（`daily_dir()` / `daily_path()` / `daily_files()` 等，与主包同解且不 `import tdxfeed`）。
- Produces: 无新 API。

这些脚本的文档跑法是 `venv/bin/python v5/scripts/<name>.py`（`sys.path[0]` 就是 `v5/scripts`），所以 `from _paths import ...` 直接可用；**不要**改成 `-m v5.scripts.x`（那会要求 `v5/scripts` 变成包，反而破坏现有跑法）。

- [ ] **Step 1: 15 个常量文件（逐行换右值）**

编辑规则：把 `=` 右边 `Path("/home/jiuben/tdx-data-feed/data/parquet/daily")` 整个换成 `daily_dir()`，左值名不变；并在文件**顶部 import 区末尾**（最后一个顶层 `import` / `from` 之后、第一个常量之前）插一行 `from _paths import daily_dir  # noqa: E402`。

| 文件 | 行 | 改写 |
|---|---|---|
| `v5/scripts/v4_aggregate_report.py` | 18 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v4_batch_analysis.py` | 27 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v4_historical_roll.py` | 19 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v4_poc_analysis.py` | 16 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v4_seq_only.py` | 17 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v5_eval.py` | 15 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v5_v6_compare.py` | 22 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_data_prep.py` | 17 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_eval.py` | 16 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_eval_direct.py` | 19 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_seq_fixed.py` | 20 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_seq_only.py` | 17 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_vs_v4_baseline.py` | 20 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/v6_vs_v4_seq.py` | 20 | `PARQUET_DIR = daily_dir()` |
| `v5/scripts/vault_to_v5.py` | 23 | `PARQUET_DIR = daily_dir()` |

`from pathlib import Path` 若改完只剩 `PARQUET_DIR` 用过，留不留都行（`make lint-all` 不阻断 F401），**不要**为了清它去改别的行。

- [ ] **Step 2: 4 个拼接型文件（守卫抓不到，手工改）**

**6.1 `v5/scripts/scan_double.py`**（L197，在 `main()` 里）

- 顶部 import 区加：`from _paths import daily_files  # noqa: E402`
- 删 `import glob`（L31；改后无其他使用）
- L197 改成：

```python
    files = sorted(daily_files())
```

行为有意变化：原来 `glob('data/parquet/daily/*.parquet')` 扫到 **10468** 个文件（含 5232 个 `*.v1.bak.parquet`，且 `.v1.bak` 会被 `Path(f).stem` 当成独立 symbol 重复扫描）；改后是 **5236** 个 canonical。下游 `Path(f).stem` 与 `scan_one(symbol, f)`（内部 `pd.read_parquet(daily_path)`）都接受 `Path`，无需改动。

**6.2 `v5/scripts/train_baseline_lgbm.py`**（L81 / L117）

- 顶部 import 区加：`from _paths import daily_files, daily_path  # noqa: E402`
- 删 `import glob`（L20；改后无其他使用）
- L81 改成：`    df = pd.read_parquet(str(daily_path(symbol)))`
- L117 改成：`    sym_to_path = {p.stem for p in daily_files()}`（原来构造的 dict 值从未被读取，只有 `if sym not in sym_to_path`，所以换成集合；变量名保留，减少 diff）

**6.3 `v5/scripts/train_multiclass.py`**（L41）

- 顶部 import 区加：`from _paths import daily_path  # noqa: E402`
- L41 改成：`    f = str(daily_path(sym))`

**6.4 `v5/scripts/trigger_e2e_v6.py`**（L49–L51）

- 顶部 import 区加：`from _paths import daily_path  # noqa: E402`
- 这三行：

```python
    path = f"data/parquet/daily/{symbol}.parquet"
    if not Path(path).exists():
        return None
```

改成：

```python
    path = daily_path(symbol)
    if not path.exists():
        return None
```

（`Path` 在该文件别处仍在用则保留 import。）

- [ ] **Step 3: 冒烟（孪生对真实目录 + 全量编译）**

```bash
venv/bin/python - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, 'v5/scripts')
import _paths

files = _paths.daily_files()
raw = sorted(Path(_paths.DAILY_DIR).glob("*.parquet"))
expected = [p for p in raw if not p.name.endswith(_paths.DAILY_BAK_SUFFIX)]
assert [p.name for p in files] == [p.name for p in expected]
assert len(files) < len(raw), (len(files), len(raw))   # 排除生效
print("twin ok", len(files), "of", len(raw))
PY
venv/bin/python -m py_compile $(git ls-files 'v5/scripts/*.py') && echo compile-ok
```

Expected: `twin ok 5236 of 10468`（数量随每日同步会变，断言只比「canonical == 非 bak 子集」且 `canonical < 全量`）、`compile-ok`。

不要逐个 `python v5/scripts/*.py` 跑——其中多数在 import 期读数据或连 Ollama。

- [ ] **Step 4: 守卫清零 + 门禁**

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q`
Expected: **4 passed**（`scan()` 返回空，`test_no_hardcoded_table_paths` 绿）。

Run: `make lint-all && make test`
Expected: 全绿。

**注意（交付阻塞）**：守卫在**当前工作树**绿，是因为 `tdxfeed/kline.py:24` 与 `scripts/backfill_minutes.py:35` 的改动已经落在工作树里，而这两个文件按约束不进本计划的任何 commit。在它们被各自所属会话提交之前，**干净 checkout 上的守卫仍是红的**——Task 12 不要再重复这点结论，直接按「两文件已入库」作为收尾前提；未入库就只做本地验证、不推送。

- [ ] **Step 5: 提交**

```bash
git add v5/scripts/v4_aggregate_report.py v5/scripts/v4_batch_analysis.py v5/scripts/v4_historical_roll.py \
    v5/scripts/v4_poc_analysis.py v5/scripts/v4_seq_only.py v5/scripts/v5_eval.py v5/scripts/v5_v6_compare.py \
    v5/scripts/v6_data_prep.py v5/scripts/v6_eval.py v5/scripts/v6_eval_direct.py v5/scripts/v6_seq_fixed.py \
    v5/scripts/v6_seq_only.py v5/scripts/v6_vs_v4_baseline.py v5/scripts/v6_vs_v4_seq.py v5/scripts/vault_to_v5.py \
    v5/scripts/scan_double.py v5/scripts/train_baseline_lgbm.py v5/scripts/train_multiclass.py v5/scripts/trigger_e2e_v6.py
git commit -m "refactor(paths): v5/scripts 改用 _paths 孪生（20 行，硬编码守卫清零）"
```

---

### Task 7: `resolve_parquet` 三处委托 + daily 列举统一

**Files:**
- Create: `tests/test_daily_listing_and_resolve.py`
- Modify（委托）: `scripts/factor_engine.py`、`quant/multifactor/factors.py`、`scripts/sim_daily_compare.py`
- Modify（列举）: `scripts/factor_engine.py`、`scripts/check_schema.py`、`tdxfeed/incremental.py`、`tdxfeed/precompute/indicators.py`、`v5/scripts/v4_batch_analysis.py`、`train/scripts/build_v8_dataset.py`、`quant/multifactor/engine.py`

**Interfaces:**
- Consumes: Task 1 的 `paths.daily_path` / `paths.daily_files` / `paths.read_path`；Task 3 的 `_common` 再导出；Task 4/5/6 已在这些文件里插好的 `daily_dir` 导入行。
- Produces: 三个 `resolve_parquet(code) -> Path` 同签名同语义——**带不带 `sh`/`sz`/`bj` 前缀都行，缺文件抛 `FileNotFoundError`**；`daily_files()` 成为 `daily` 目录唯一列举入口。

**为什么改（以下都是本机实测，不是推测）**

1. `factor_engine.resolve_parquet('sh600000')` 返回 `data/parquet/daily/600000.parquet`（**不存在**）。`daily` 目录现在全是带前缀的名字（10,468 个 parquet，其中 5,232 个 `.v1.bak`，canonical 5,236 个；唯一非 `sh`/`sz` 前缀的文件是 `_audit_adjust_convention.csv`）。裸名回退是 v1 时代的遗留。
2. `parse_daily` 把读失败吞成 `None`（`scripts/factor_engine.py:95` 起），所以 (1) 不报错、只是静默丢样本。
3. (1) **不是每次都踩到**：负样本循环优先命中正样本循环已填的 `cache`。实测 `--limit 47` 时把 `resolve_parquet` 换成会抛异常的探针，调用 **0 次**、负样本仍输出 45 行。所以后果是「候选未进 cache 时被静默丢弃」（采样覆盖损失），**不要**在提交信息里写「负样本从 0 行修好」——那是假的。
4. `sim_daily_compare.resolve_parquet('000001')` 返回 **`sh000001.parquet`（上证指数）**，而 `000001` 是平安银行 `sz000001.parquet`。四个前缀按 `sh→sz→bj` 顺序试，裸 6 位码必然先撞上同号指数。这是**直接算错标的**，是本节最实在的一个 bug。
5. `.v1.bak` 的 stem 泄漏进标的列表：`incremental` 会去拉 `sh600000.v1.bak` 这种假代码；`precompute/indicators --all` 会写出 `*_indicators.parquet` 的备份副本（Task 9 的全量重建必须先修掉这条）；`v4_batch_analysis.get_all_stocks()` 返回 10,468 个 stem；`factor_engine --cross-section` 同理会产出重复 pseudo-code。

- [ ] **Step 1: 写失败测试**

`tests/test_daily_listing_and_resolve.py`：

```python
"""daily 表列举唯一走 paths.daily_files()；三份 resolve_parquet 委托给 paths.daily_path。"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from tdxfeed import paths

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    not paths.daily_dir().exists(), reason="需要 data/parquet/daily（CI 精简集没有 data/）"
)

LISTING_SITES = [
    "scripts/factor_engine.py",
    "scripts/check_schema.py",
    "tdxfeed/incremental.py",
    "tdxfeed/precompute/indicators.py",
    "v5/scripts/v4_batch_analysis.py",
    "train/scripts/build_v8_dataset.py",
    "quant/multifactor/engine.py",
]


def _load(rel: str, name: str):
    """按路径加载脚本：scripts/ 不在包里，v5/scripts 需要能 `from _paths import ...`。"""
    for extra in (SCRIPTS, ROOT / "v5" / "scripts"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # dataclass / 内部自引用需要先登记
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def resolvers():
    return {
        "factor_engine": _load("scripts/factor_engine.py", "probe_factor_engine"),
        "quant_factors": _load("quant/multifactor/factors.py", "probe_mf_factors"),
        "sim_daily_compare": _load("scripts/sim_daily_compare.py", "probe_sim_daily_compare"),
    }


@pytest.mark.parametrize("name", ["factor_engine", "quant_factors", "sim_daily_compare"])
@pytest.mark.parametrize("code", ["sh600000", "sz000001", "600519"])
def test_resolve_agrees_with_authority(resolvers, name, code):
    fp = resolvers[name].resolve_parquet(code)
    assert fp == paths.daily_path(code)
    assert fp.exists()


@pytest.mark.parametrize("name", ["factor_engine", "quant_factors", "sim_daily_compare"])
def test_resolve_raises_on_unknown(resolvers, name):
    with pytest.raises(FileNotFoundError):
        resolvers[name].resolve_parquet("sh999999")


def test_sim_daily_compare_bare_code_picks_stock_not_index(resolvers):
    """裸 000001 是平安银行（sz）；上证指数是同号的 sh000001，旧实现返回后者。"""
    assert resolvers["sim_daily_compare"].resolve_parquet("000001").name == "sz000001.parquet"


def test_v4_batch_analysis_lists_canonical_only():
    mod = _load("v5/scripts/v4_batch_analysis.py", "probe_v4_batch_analysis")
    stems = mod.get_all_stocks()
    assert len(stems) == len(paths.daily_files())
    assert not [s for s in stems if s.endswith(".v1.bak")]


def test_precompute_all_iterates_canonical_codes(monkeypatch):
    from tdxfeed.precompute import indicators

    seen: list[str] = []
    monkeypatch.setattr(indicators, "process_one", lambda code, force=False: seen.append(code))
    monkeypatch.setattr(sys, "argv", ["indicators", "--all"])
    indicators.main()
    assert len(seen) == len(paths.daily_files())
    assert not [c for c in seen if c.endswith(".v1.bak")]


@pytest.mark.parametrize("rel", LISTING_SITES)
def test_listing_goes_through_daily_files(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "daily_files(" in src, rel
    assert not re.search(r"\.glob\(\s*['\"]\*\.parquet['\"]\s*\)", src), rel
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_daily_listing_and_resolve.py -q`

Expected: **13 failed, 1 passed**（Parametrize 展开数可能因你改的行不同而差 1，按实际记录）。逐条应是：

- `test_resolve_agrees_with_authority[factor_engine-*]` ×3、`[quant_factors-*]` ×3：断言 `fp == paths.daily_path(code)` 失败（返回裸名）；
- `test_resolve_agrees_with_authority[sim_daily_compare-*]` ×3：**全过**（sim 的 `sh/sz/bj` 顺序在带前缀输入上恰好对）；
- `test_resolve_raises_on_unknown[factor_engine]` / `[quant_factors]`：`DID NOT RAISE`；
- `test_sim_daily_compare_bare_code_picks_stock_not_index`：`sh000001.parquet != sz000001.parquet`；
- `test_v4_batch_analysis_lists_canonical_only`、`test_precompute_all_iterates_canonical_codes`：`10468 != 5236`；
- `test_listing_goes_through_daily_files[7 个文件]`：7 条全红（无 `daily_files(`）。

- [ ] **Step 3: 三处 `resolve_parquet` 改成委托**

`scripts/factor_engine.py`——替换整函数：

```python
def resolve_parquet(code: str) -> Path:
    """code 带不带 sh/sz/bj 前缀都行；路径由 paths.daily_path 唯一决定，缺文件抛 FileNotFoundError。"""
    return read_path(daily_path(code))
```

（`norm_code` 保留——`name_map.get(norm_code(code))` 还在用。但它 docstring 里「文件名实为裸代码（600825）」这句现在是错的（v1→v2 迁移后 canonical 全是 `sh`/`sz` 前缀，裸名只剩 `.v1.bak`），把那两行改成：「data/parquet/daily 的文件名是 sh/sz 前缀；裸名只存在于 `.v1.bak` 迁移备份里，统一归一后 name_map / ST 集合 / 翻倍样本匹配才一致。」）

`scripts/factor_engine.py` 负样本循环（`for code in sampled:` 里）——`resolve_parquet` 现在会抛，必须接住并跳过：

```python
                if code not in cache:
                    try:
                        fp = resolve_parquet(code)
                    except FileNotFoundError:
                        continue
                    df_full = parse_daily(fp)
```

`quant/multifactor/factors.py`：

```python
def resolve_parquet(code: str) -> Path:
    """code 带不带前缀都行；路径由 tdxfeed.paths 唯一决定（本函数无仓内调用方）。"""
    return read_path(daily_path(code))
```

`scripts/sim_daily_compare.py`——注意 `str(code).zfill(6).lstrip("bj")` 这个组合还有第二层错：`lstrip` 按**字符集**剥，`'bj600000'` 能剥对但 `'000001'` 原样保留后按 `sh` 先试，于是拿到指数：

```python
def resolve_parquet(code: str) -> Path:
    """带不带前缀都行；裸 6 位按交易所规则定前缀（000001 → sz000001，不是上证指数）。"""
    return read_path(daily_path(code))
```

- [ ] **Step 4: 11 处列举改走 `daily_files()`**

行号是**当前工作树**的；Task 3–6 插过 import 行后会整体后移，用 `old_string` 定位，不要按行号跳。改完这 11 处后，有 5 个常量彻底没有读者，**连同导入行里的名字一起删掉**（Task 3–6 刚把它们换成访问器，别留着当装饰）：

| 文件 | 现状（old） | 改成（new） |
|---|---|---|
| `scripts/factor_engine.py:226` | `files = sorted(PARQUET.glob("*.parquet"))` | `files = daily_files()` |
| `scripts/factor_engine.py:317` | `files = sorted(p for p in PARQUET.glob('*.parquet') if '.bak' not in p.name)` | `files = daily_files()` |
| `scripts/check_schema.py:109` | `daily_paths = sorted(DAILY_DIR.glob("*.parquet"))` | `daily_paths = daily_files()` |
| `scripts/check_schema.py:110` | `adj_paths = sorted(ADJ_DIR.glob("*.parquet"))` | `adj_paths = adj_factor_files()` |
| `tdxfeed/incremental.py:179` | `syms = sorted([p.stem for p in DAILY_DIR.glob("*.parquet")])` | `syms = [p.stem for p in daily_files()]` |
| `tdxfeed/precompute/indicators.py:233` | `files = sorted(DAILY_DIR.glob("*.parquet"))` | `files = daily_files()` |
| `v5/scripts/v4_batch_analysis.py:66` | `for f in PARQUET_DIR.glob("*.parquet"):` | `for f in daily_files():` |
| `v5/scripts/v4_batch_analysis.py:81` | `return sorted([f.stem for f in PARQUET_DIR.glob("*.parquet")])` | `return [p.stem for p in daily_files()]` |
| `v5/scripts/v4_batch_analysis.py:87` | `return sorted([f.stem for f in PARQUET_DIR.glob("*.parquet") if f.stem.startswith(a_prefixes)])` | `return [p.stem for p in daily_files() if p.stem.startswith(a_prefixes)]` |
| `train/scripts/build_v8_dataset.py:220` | `files = sorted(DAILY_DIR.glob("*.parquet"))` | `files = daily_files()` |
| `quant/multifactor/engine.py:284` | `files = [f for f in sorted(PARQUET_DIR.glob("*.parquet"))`（下一行接 `if ".bak" not in f.name and is_stock_code(f.stem)]`） | `files = [f for f in daily_files() if is_stock_code(f.stem)]`（删掉原来那两行的条件拼接） |

删常量（删之前 `grep -n "<名字>"` 确认除定义外无引用）：

- `scripts/factor_engine.py`：`PARQUET = ...`（改完 0 引用）、`ADJ_DIR = ...`（**改前就是死常量**，全文件只有这一行）。
- `scripts/check_schema.py`：`DAILY_DIR`、`ADJ_DIR`（改完 0 引用；`ROOT` 保留，报告里 `"root": str(ROOT)` 还在用）。
- `quant/multifactor/factors.py`：`PARQUET_DIR`（改完 0 引用；`ROOT` 保留，`SENT_DIR` 用）。
- `scripts/sim_daily_compare.py`：`PARQUET_DIR`（改完 0 引用；`ROOT` 保留，`OUT_DIR` 用）。
- `quant/multifactor/engine.py`：`PARQUET_DIR`（改完 0 引用；`ROOT` 被 `OUTPUT_DIR` 等用，保留）。
- `train/scripts/build_v8_dataset.py`：`DAILY_DIR`（改完 0 引用；`DERIVED_DIR` 保留）。
- 保留：`tdxfeed/incremental.DAILY_DIR`（L123 写文件用）、`tdxfeed/precompute/indicators.DAILY_DIR`（`process_one` 用）、`v5/scripts/v4_batch_analysis.PARQUET_DIR`（L163/L265 用）。

- [ ] **Step 5: 同步改导入行**

每行都是 Task 3/4/5/6 插的那行，位置不动、只扩名：

| 文件 | old | new |
|---|---|---|
| `scripts/factor_engine.py` | `from _common import daily_dir, adj_factor_dir  # noqa: E402` | `from _common import daily_files, daily_path, read_path  # noqa: E402` |
| `scripts/sim_daily_compare.py` | `from _common import daily_dir  # noqa: E402` | `from _common import daily_path, read_path  # noqa: E402` |
| `scripts/check_schema.py` | `from _common import daily_dir, adj_factor_dir  # noqa: E402` | `from _common import adj_factor_files, daily_files  # noqa: E402` |
| `tdxfeed/incremental.py` | `from tdxfeed.paths import daily_dir  # noqa: E402` | `from tdxfeed.paths import daily_dir, daily_files  # noqa: E402` |
| `tdxfeed/precompute/indicators.py` | `from tdxfeed.paths import daily_dir, derived_dir  # noqa: E402` | `from tdxfeed.paths import daily_dir, daily_files, derived_dir  # noqa: E402` |
| `v5/scripts/v4_batch_analysis.py` | `from _paths import daily_dir  # noqa: E402` | `from _paths import daily_dir, daily_files  # noqa: E402` |
| `train/scripts/build_v8_dataset.py` | `from tdxfeed.paths import daily_dir, derived_dir  # noqa: E402` | `from tdxfeed.paths import daily_files, derived_dir  # noqa: E402` |
| `quant/multifactor/factors.py` | `from tdxfeed.paths import daily_dir  # noqa: E402` | `from tdxfeed.paths import daily_path, read_path  # noqa: E402` |
| `quant/multifactor/engine.py` | `from tdxfeed.paths import daily_dir  # noqa: E402` | `from tdxfeed.paths import daily_files  # noqa: E402` |

（`scripts/check_schema.py` 若 Task 3 写的名字顺序是 `adj_factor_dir, daily_dir`，按实际那行替换即可；关键是改完后 `daily_dir`/`adj_factor_dir` 不再被引用。）

- [ ] **Step 6: 转绿 + 回归实跑**

Run: `venv/bin/pytest tests/test_daily_listing_and_resolve.py -q`
Expected: `14 passed`。

Run（`factor_engine` 回归：与修复前同样的输入、同样的输出行数，写 `/tmp` 不碰 `data/`）：

```bash
venv/bin/python - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
import factor_engine as fe

fe.OUT_DIR = Path('/tmp/fe_rehearsal_after')
sys.argv = ['factor_engine.py', '--limit', '47', '--neg-ratio', '1']
fe.main()
PY
```

Expected: 与修复前一致——`正样本提取完成: 45 行`、`负样本采样完成: 45 行`、`输出: /tmp/fe_rehearsal_after/event_factors.parquet (90 行 × 28 列)`，耗时 ~1 s。这条**不是**「修好了 0 行」的证据，是「委托没有改变输出」的回归证据。

Run（委托后的实际取值）：

```bash
venv/bin/python - <<'PY'
import sys
sys.path.insert(0, 'scripts')
import factor_engine as fe, sim_daily_compare as sc
for code in ('sh600000', '000001', '600519'):
    print(code, '->', fe.resolve_parquet(code).name, '|', sc.resolve_parquet(code).name)
PY
```

Expected: `sh600000 -> sh600000.parquet | sh600000.parquet`、`000001 -> sz000001.parquet | sz000001.parquet`、`600519 -> sh600519.parquet | sh600519.parquet`。

- [ ] **Step 7: 守卫与门禁**

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q`
Expected: 仍 **4 passed**（本轮删的是 `.glob` 调用和死常量，不再有路径字面量；如果变红，说明你在删常量时把别处的字面量带出来了）。

Run: `make lint-all`
Expected: 无 `F821 undefined name`（典型漏改：某个文件用了 `daily_files()` 但导入行没扩名）、无 `E9`。

Run: `make test`
Expected: 全绿。

- [ ] **Step 8: 提交**

```bash
git add tests/test_daily_listing_and_resolve.py scripts/factor_engine.py scripts/check_schema.py \
    scripts/sim_daily_compare.py quant/multifactor/factors.py quant/multifactor/engine.py \
    tdxfeed/incremental.py tdxfeed/precompute/indicators.py train/scripts/build_v8_dataset.py \
    v5/scripts/v4_batch_analysis.py
git commit -m "refactor(paths): resolve_parquet 三处委托给 daily_path，daily 列举统一走 daily_files()"
```

**不在本轮**：`scripts/predict.py`、`scripts/plan_e_backtest.py`、`scripts/bench_v567.py`、`scripts/bench_v7r.py`、`scripts/build_cls2000.py`、`scripts/llm_signal_plan_e.py`、`scripts/plan_e_sim_live.py`、`scripts/verify_daban_hit.py` 等**内联**的四前缀回退没动——规格只要求收编三份**同名** `resolve_parquet` 实现，这些文件在 Task 3 只换过路径常量，行为不变。想扩面另开议题，别在本计划里顺手改。

---

### Task 8: 写入口收口到 `write_table`

**Files:**
- Create: `tests/test_write_entry_single_source.py`
- Modify: `tdxfeed/writers.py`、`tdxfeed/incremental.py`、`scripts/download_hfq_factor.py`、`scripts/download_universe.py`、`scripts/migrate_v1_to_v2.py`、`tdxfeed/precompute/indicators.py`

**Interfaces:**
- Consumes: Task 1 的 `parquet_io.write_table(path, df, date_col="date")`（date→date32 + `os.replace` 原子替换）。
- Produces: 6 个生产者的写盘都走 `write_table`；仓内不再有 `pa.date32(` / `pq.write_table(`（除 `tdxfeed/parquet_io.py`）。

**为什么改**

今天有 4 处手写 date32、1 处干脆不写 date32：

| 站点 | 现状 | 问题 |
|---|---|---|
| `tdxfeed/writers.py:100-119` | `pa.array(..., pa.date32())` + `pq.write_table` | 就地写，inode 不变 |
| `tdxfeed/incremental.py:75-85` | 同上 + `V2_COLS` 断言 | 就地写 |
| `scripts/download_hfq_factor.py:114-119` | 同上（列序被手工重排） | 就地写 |
| `tdxfeed/precompute/indicators.py:212` | `out.to_parquet(dst)` | **不强制 date32**（实测存量 derived 的 `date` 是 `timestamp[ns]`，而 `daily`/`adj_factor` 是 `date32[day]`） |
| `scripts/download_universe.py:84` | `df.to_parquet(...)`，`date` 是 int64 | **C7 的真身**：新补的标的是 int64 日期 |
| `scripts/migrate_v1_to_v2.py:161` | `df.to_parquet(...)`（`_norm_date_series` 之后） | 不原子 |

「就地写」在 Task 9 会变成实打实的伤害：derived 的备份是 `cp -al` 硬链接快照，就地写会穿透硬链接把备份一起改掉，快照就失去意义。`write_table` 先写 `.tmp` 再 `os.replace`，换 inode 才安全。

- [ ] **Step 1: 写失败测试**

`tests/test_write_entry_single_source.py`：

```python
"""行情表写盘只有一个入口：tdxfeed.parquet_io.write_table（date32 + 原子替换）。"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]

ALLOWED = {"tdxfeed/parquet_io.py"}
PA_DATE32 = re.compile(r"\bpa\.date32\(")
PQ_WRITE = re.compile(r"\bpq\.write_table\(")
SKIP_DIRS = {".git", ".qoder", "_archive", "node_modules", "tests"}
PRODUCERS = [
    "scripts/download_universe.py",
    "scripts/migrate_v1_to_v2.py",
    "tdxfeed/precompute/indicators.py",
]


def _iter_sources():
    for p in ROOT.rglob("*.py"):
        parts = p.relative_to(ROOT).parts[:-1]
        if any(part in SKIP_DIRS or part.startswith("venv") for part in parts):
            continue
        yield p


def _bars(n: int = 80) -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2026-01-05", periods=n, freq="D"),
        "open": [10.0] * n, "high": [10.5] * n, "low": [9.5] * n,
        "close": [10.1] * n, "volume": [1e6] * n, "amount": [1e7] * n,
    })


def _date_type(fp: Path) -> str:
    return str(pq.read_schema(fp).field("date").type)


def test_no_raw_parquet_writer_outside_parquet_io():
    offenders = []
    for p in _iter_sources():
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        if PA_DATE32.search(src) or PQ_WRITE.search(src):
            offenders.append(rel)
    assert offenders == []


@pytest.mark.parametrize("rel", PRODUCERS)
def test_producers_call_write_table(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "write_table(" in src, rel
    assert ".to_parquet(" not in src, rel


def test_writers_helper_is_date32_and_replaces_inode(tmp_path):
    from tdxfeed.writers import Writers

    fp = tmp_path / "sh600000.parquet"
    Writers._write_parquet_date32(str(fp), _bars())
    assert _date_type(fp) == "date32[day]"
    ino = fp.stat().st_ino
    Writers._write_parquet_date32(str(fp), _bars(60))
    assert fp.stat().st_ino != ino, "就地覆盖会穿透 cp -al 快照"
    assert [p.name for p in tmp_path.iterdir()] == ["sh600000.parquet"]


def test_incremental_write_v2_keeps_column_guard(tmp_path):
    from tdxfeed import incremental as inc

    fp = tmp_path / "sh600000.parquet"
    with pytest.raises(ValueError):
        inc._write_v2(fp, _bars().drop(columns=["amount"]))
    inc._write_v2(fp, _bars())
    assert _date_type(fp) == "date32[day]"
    assert [p.name for p in tmp_path.iterdir()] == ["sh600000.parquet"]


def test_process_one_writes_date32_derived(tmp_path, monkeypatch):
    from tdxfeed.precompute import indicators as ind

    daily = tmp_path / "parquet" / "daily"
    daily.mkdir(parents=True)
    derived = tmp_path / "parquet" / "derived"
    monkeypatch.setattr(ind, "DAILY_DIR", daily)
    monkeypatch.setattr(ind, "DERIVED_DIR", derived)
    _bars().to_parquet(daily / "sh600000.parquet", index=False)   # 故意写成 timestamp[ns]

    out = ind.process_one("sh600000")
    assert out is not None and out.name == "sh600000_indicators.parquet"
    sch = pq.read_schema(out)
    assert len(sch.names) == 29
    assert str(sch.field("date").type) == "date32[day]"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_write_entry_single_source.py -q`

Expected: **5 failed, 1 passed**：

| 用例 | 现在 |
|---|---|
| `test_no_raw_parquet_writer_outside_parquet_io` | 红（3 个 offender：`scripts/download_hfq_factor.py`、`tdxfeed/incremental.py`、`tdxfeed/writers.py`） |
| `test_producers_call_write_table[scripts/download_universe.py]` | 红（`.to_parquet(` 还在） |
| `test_producers_call_write_table[scripts/migrate_v1_to_v2.py]` | 红（同上） |
| `test_producers_call_write_table[tdxfeed/precompute/indicators.py]` | 红（同上） |
| `test_writers_helper_is_date32_and_replaces_inode` | 红（今天 date32 是对的，**inode 不变**这条红） |
| `test_incremental_write_v2_keeps_column_guard` | **绿**——今天 `_write_v2` 的列校验 + date32 + 不留 tmp 本来就成立；它是回归锁，不是 RED |

如果你看到 `test_incremental_write_v2_keeps_column_guard` 也红了，说明工作树里 `incremental.py` 已被别的会话改过，先停下来查，不要顺手修。

- [ ] **Step 3: 实现**

**3.1 `tdxfeed/writers.py`** —— import 区：删 `import pyarrow as pa`、`import pyarrow.parquet as pq`；加 `from pathlib import Path`（现在没有）与 `from tdxfeed.parquet_io import write_table`（放 `from tdxfeed.amount_fix import ...` 那一行旁边）。整个 `_write_parquet_date32` 体换成三行：

```python
    @staticmethod
    def _write_parquet_date32(p: str, df: pd.DataFrame) -> None:
        """写 daily parquet：date 强制 date32 并原子替换（实现见 tdxfeed/parquet_io.py）。"""
        write_table(Path(p), df)
```

（`writers.py:88` 的 `df.to_parquet(p, index=False)` 是 `write_parquet` 的非 daily 分支，**不动**——它不在行情表契约里，别扩大范围。）

**3.2 `tdxfeed/incremental.py`** —— import 区：删 `import pyarrow as pa` / `import pyarrow.parquet as pq`，在 `from tdxfeed.writers import Writers` 旁边加 `from tdxfeed.parquet_io import write_table`。`_write_v2` 换成：

```python
def _write_v2(fp: Path, df: pd.DataFrame) -> None:
    """v2 写入：列契约先校验，date 由 write_table 强制 date32。"""
    if list(df.columns) != V2_COLS:
        raise ValueError(f"列不符 v2: {list(df.columns)}")
    write_table(fp, df)
```

**3.3 `scripts/download_hfq_factor.py`** —— 删 `import pyarrow as pa`、`import pyarrow.parquet as pq`；在 Task 3 插的 `from _common import hfq_factor_dir  # noqa: E402` 之后加 `from tdxfeed.parquet_io import write_table  # noqa: E402`。把 L114–119 这段（`# v2 写入：date date32` 起 6 行）整体换成：

```python
            # v2 写入：date date32 + 原子替换（列序由 write_table 按 df 顺序保留）
            write_table(fp, merged[["code", "hfq_factor", "qfq_close", "hfq_close", "date"]])
            return "ok"
```

（列序必须保持 `code, hfq_factor, qfq_close, hfq_close, date`——这是 Task 9 契约测试的断言对象。）

**3.4 `scripts/download_universe.py`** —— 导入行改成：

```python
from tdxfeed.paths import daily_files, daily_path
from tdxfeed.parquet_io import write_table
```

删 `OUT = daily_dir()` 那一行（Task 3 的 Step 6.4 已把它改成 `OUT = daily_dir()`；本步 `daily_path(s)` 取代它，`daily_dir` 也就不再需要）。写盘那行：

```python
        write_table(daily_path(s), df)
```

`df["date"] = pd.to_datetime(...).dt.strftime("%Y%m%d").astype(int)` **保留**——`write_table._as_date32` 认得 int YYYYMMDD，改了反而增加风险面。

**3.5 `scripts/migrate_v1_to_v2.py`** —— 在 Task 3 插的 `from _common import ...  # noqa: E402` 之后加 `from tdxfeed.parquet_io import write_table  # noqa: E402`；L161 `df.to_parquet(p, index=False, engine="pyarrow")` → `write_table(p, df)`。

**3.6 `tdxfeed/precompute/indicators.py`** —— 在 Task 4 插的 `from tdxfeed.paths import ...` 之后加 `from tdxfeed.parquet_io import write_table  # noqa: E402`；L212 `out.to_parquet(dst, index=False)` → `write_table(dst, out)`。上方的 `DERIVED_DIR.mkdir(parents=True, exist_ok=True)` 留着（`write_table` 也会建父目录，但这行是给人读的意图声明，不删）。

- [ ] **Step 4: 转绿 + 守卫与门禁**

Run: `venv/bin/pytest tests/test_write_entry_single_source.py -q`
Expected: `6 passed`。

Run: `venv/bin/pytest tests/test_freshness_v2_migration.py tests/test_plan_e_random_timing.py -q`
Expected: 全绿——`writers.merge_write` 的契约（新覆盖旧、date 归一 int 后合并）没有变；`test_freshness_v2_migration` 里 `pq` 的用法在 `tests/` 内，被守卫跳过。

Run: `venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q && make lint-all`
Expected: 4 passed；lint 无 `F821`（典型漏改：删了 `import pyarrow` 却还有 `pa.` 引用）。

- [ ] **Step 5: 提交**

```bash
git add tests/test_write_entry_single_source.py tdxfeed/writers.py tdxfeed/incremental.py \
    scripts/download_hfq_factor.py scripts/download_universe.py scripts/migrate_v1_to_v2.py \
    tdxfeed/precompute/indicators.py
git commit -m "refactor(write): 6 个生产者收口到 write_table（date32 + 原子替换，修 download_universe 的 int64 日期）"
```

---

### Task 9: 存量 `derived` 全量重建（本轮唯一写 `data/` 的一步）

**Files:**
- Create: `tests/test_parquet_schema_contract.py`
- 数据（gitignored，不进 commit）: `data/parquet/derived` 全量重写；备份快照 `data/parquet/derived_pre_settle_20261028`

**Interfaces:**
- Consumes: Task 7 的 `daily_files()`（只有 canonical 才会被枚举）、Task 8 的 `write_table`（`process_one` 已经改完）。
- Produces: `derived` 与 `daily` **同集**、`date` 为 `date32[day]`、29 列、无 `.v1.bak` 衍生物。

**当前实测（这就是 RED 的证据）**

- `data/parquet/daily`：10,468 个 parquet，其中 5,232 个 `.v1.bak`，canonical **5,236**。
- `data/parquet/derived`：**5,200** 个 `*_indicators.parquet`，`date` 类型是 **`timestamp[ns]`**，无 `.v1.bak` 衍生物，2.5 GB。
- 差 36 个 = canonical 有、derived 没有（近期新股 + `--incremental` 只按 mtime 跳过导致的漏网）。
- 单标的 `compute_one` 实测 0.008 s（6,029 行），全量主要开销在 I/O，预计 **3–8 min**；以日志里的 `速率=` 为准，别按估时判断卡死。

- [ ] **Step 1: 写契约测试**

`tests/test_parquet_schema_contract.py`：

```python
"""五张行情表的 schema 契约：date 口径、列集、derived 与 daily 同集。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from tdxfeed import paths

pytestmark = pytest.mark.skipif(
    not paths.daily_dir().exists(), reason="需要 data/parquet（CI 精简集没有 data/）"
)

DAILY_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]
ADJ_COLS = ["code", "date", "adj_factor", "qfq_close", "raw_close"]
DERIVED_COLS = 29


def _sample(files: list[Path], k: int = 12) -> list[Path]:
    if len(files) <= k:
        return list(files)
    step = len(files) // k
    return [files[i * step] for i in range(k)]


def _schema(fp: Path) -> dict[str, str]:
    sch = pq.read_schema(fp)
    return {n: str(t) for n, t in zip(sch.names, sch.types)}


def test_daily_is_canonical_and_date32():
    files = paths.daily_files()
    assert files
    raw = list(paths.daily_dir().glob("*.parquet"))
    assert len(files) < len(raw), "canonical 应排除 .v1.bak 备份"
    assert not [p for p in files if p.name.endswith(paths.DAILY_BAK_SUFFIX)]
    for fp in _sample(files):
        sch = _schema(fp)
        assert list(sch) == DAILY_COLS, fp
        assert sch["date"] == "date32[day]", fp


def test_adj_factor_is_date32():
    files = paths.adj_factor_files()
    assert files
    for fp in _sample(files):
        sch = _schema(fp)
        assert list(sch) == ADJ_COLS, fp
        assert sch["date"] == "date32[day]", fp


def test_derived_mirrors_daily():
    daily_stems = {p.stem for p in paths.daily_files()}
    derived = sorted(paths.derived_dir().glob("*_indicators.parquet"))
    stems = {p.name.removesuffix("_indicators.parquet") for p in derived}
    assert stems == daily_stems
    for fp in _sample(derived):
        sch = _schema(fp)
        assert len(sch) == DERIVED_COLS, (fp.name, len(sch))
        assert sch["date"] == "date32[day]", fp.name


def test_minute_datetime_is_string():
    files = sorted(paths.minute_dir("60").glob("*.parquet"))
    assert files
    for fp in _sample(files, 3):
        assert _schema(fp)["datetime"] == "string", fp.name
        ts = pd.read_parquet(fp, columns=["datetime"])["datetime"].iloc[0]
        assert len(str(ts)) == 19, fp.name
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_parquet_schema_contract.py -q`

Expected: **2 failed, 3 passed**——`test_derived_mirrors_daily` 红（`stems != daily_stems`，5200 vs 5236；就算集合巧合相等，抽样里 `date` 也会是 `timestamp[ns]`）；`daily`/`adj`/`minute` 三条绿。若 `test_daily_is_canonical_and_date32` 也红，说明 Task 7 没生效或有别的写入端把 int64 日期写回来了，先查再往下走。

- [ ] **Step 3: 快照（硬链接，秒级，不占额外空间）**

```bash
cd /home/jiuben/tdx-data-feed
SNAP=data/parquet/derived_pre_settle_20261028
test -e "$SNAP" && echo "快照已存在，跳过 cp -al" || cp -al data/parquet/derived "$SNAP"
cat > "$SNAP/MANIFEST.txt" <<EOF
created_at: $(date -Is)
git_head: $(git rev-parse HEAD)
files: $(ls "$SNAP"/*.parquet | wc -l)
size: $(du -sh "$SNAP" | cut -f1)
sample_head5:
$(ls "$SNAP"/*.parquet | head -5)
EOF
cat "$SNAP/MANIFEST.txt"
```

Expected: `files: 5200`（快照时刻的数字，别和重建后的 5236 混）——`MANIFEST.txt` 是这份快照的出生证，重建后靠它区分「快照是旧的」还是「重建失败回退了」。

**为什么用 `cp -al`**：Task 8 已把写盘改成 `os.replace`，重建只会**换 inode**、不会原地改文件；所以硬链接快照的 5,200 个文件在重建后仍然指向旧数据。这条链路任何一环退回 `to_parquet` 就地写，快照就会被穿透——真出这种事，`MANIFEST.txt` 里的 `size` 和抽样 `date` 类型会立刻对不上。

- [ ] **Step 4: 全量重建**

```bash
mkdir -p logs
nohup venv/bin/python -m tdxfeed.precompute.indicators --all --force \
    > logs/derived_rebuild_20261028.log 2>&1 &
echo started $!
```

盯日志（每 200 个打一行 `[i/total] ok=… skip=… fail=… 速率=… ETA=…`）：

```bash
tail -f logs/derived_rebuild_20261028.log
```

Expected 收尾三行形如：

```
完成：ok=5236 skip=0 fail=0  耗时 4.2min
输出目录：/home/jiuben/tdx-data-feed/data/parquet/derived
```

`fail > 0` 就停下：日志前 5 条会打 `✗ {code}: {异常}`，按 code 单独 `--code` 复现，不要带着 fail 继续。

- [ ] **Step 5: 全量复核（不抽样）**

```bash
venv/bin/python - <<'PY'
import pyarrow.parquet as pq
from tdxfeed import paths

derived = sorted(paths.derived_dir().glob("*_indicators.parquet"))
daily = {p.stem for p in paths.daily_files()}
stems = {p.name.removesuffix("_indicators.parquet") for p in derived}
bad = []
for p in derived:
    sch = pq.read_schema(p)
    if str(sch.field("date").type) != "date32[day]" or len(sch.names) != 29:
        bad.append((p.name, str(sch.field("date").type), len(sch.names)))
print("derived:", len(derived), "daily:", len(daily))
print("missing:", sorted(daily - stems)[:10], len(daily - stems))
print("extra:", sorted(stems - daily)[:10], len(stems - daily))
print("bak:", [p.name for p in derived if ".v1.bak" in p.name][:5])
print("bad:", bad[:5], len(bad))
PY
```

Expected: `derived: 5236  daily: 5236`、`missing: [] 0`、`extra: [] 0`、`bak: []`、`bad: [] 0`。

`extra` 非空时**不要** `rm`：先 `ls data/parquet/daily` 确认这些 code 是真的退市（canonical 里确实没有），再逐个删；`missing` 非空说明 `process_one` 对这些 code 返回了 `None`（`len(df) < ...` 或 `compute_one` 出空表），用 `--code` 单跑看原因。

- [ ] **Step 6: 幂等 + 与源码重算一致**

```bash
venv/bin/python - <<'PY'
import numpy as np
import pandas as pd

from tdxfeed import paths
from tdxfeed.parquet_io import read_derived
from tdxfeed.precompute.indicators import compute_one

files = paths.daily_files()
for fp in (files[0], files[len(files) // 2], files[-1]):
    code = fp.stem
    recomputed = compute_one(code, pd.read_parquet(fp))
    stored = read_derived(code)
    assert list(recomputed.columns) == list(stored.columns), code
    assert len(recomputed) == len(stored), code
    for col in recomputed.columns:
        if col == "date":
            continue
        a = pd.to_numeric(recomputed[col], errors="coerce").to_numpy(dtype="float64")
        b = pd.to_numeric(stored[col], errors="coerce").to_numpy(dtype="float64")
        assert np.allclose(a, b, rtol=1e-9, atol=1e-9, equal_nan=True), (code, col)
    print("parity ok", code, len(stored), "rows")
PY
```

Expected: 3 行 `parity ok <code> <rows> rows`。用 `np.allclose(..., equal_nan=True)` 而不是比较 `NaN`——`ma5`/`ma10` 的前几行就是 NaN，手写比较会假红。

- [ ] **Step 7: 契约转绿 + 全量门禁**

Run: `venv/bin/pytest tests/test_parquet_schema_contract.py -q`
Expected: **5 passed**。

Run: `make test`
Expected: 全绿。**这一步会跑起来变慢**（`tests/` 里有多处读真实 derived 的用例），是数据变新的正常后果，不是退化。

Run: `git status --porcelain data/ | head`
Expected: 空输出（`data/` 在 gitignore 里，重建不产生任何待提交项）。

- [ ] **Step 8: 提交（只提交测试文件）**

```bash
git add tests/test_parquet_schema_contract.py
git commit -m "test(data): 行情表 schema 契约（derived 与 daily 同集 + date32），配合 2026-10-28 derived 全量重建"
```

（重建本身在 `data/` 下，不进 git。要证明「重建做过」，证据是 `data/parquet/derived_pre_settle_20261028/MANIFEST.txt` + 本 Task 的日志 `logs/derived_rebuild_20261028.log`。）

---

### Task 10: 26/29 维单一实现 + 阈值变更的可核实证据

**Files:**
- Create: `tests/test_features_parity.py`
- Create: `tests/test_names_single_source.py`
- Modify: `tests/test_limit_single_source.py`（阶段 1 建的文件，末尾追加 2 个用例）
- Modify: `scripts/feature_engineering.py:38`（import 行）、`:68-121`（`build_extended_features`）
- Modify: `scripts/predict.py:173`（import 块）、`:176-181`（`is_st`）

**Interfaces:**
- Consumes:
  - `tdxfeed.features_5d.compute_window_features_extended(df, code, sig_idx=-1) -> dict | None`（29 键：18 基础 + 8 工程化 + 3 打板）
  - `tdxfeed.factors_polars.compute_window_features_extended_polars(polars_df, code, sig_idx=-1)`（**入参必须是 polars DataFrame**）
  - `tdxfeed.names.is_st_name(name: str) -> bool`（阶段 1 产出）
  - `tdxfeed.execution.limits.limit_threshold(code, *, is_st=False, on_date=None, buffer=0.005)`（阶段 1 产出；`quant.factors.get_limit` 已委托）
- Produces:
  - `scripts.feature_engineering.build_extended_features(window_df, code) -> dict | None`，仍返回 **26 键**、键序 `FEATURE_BASE + [n for n, _ in NEW_FEATURES]`
  - `scripts.predict.is_st(name) -> int`（仍是 0/1，`None` 也得 0）

**为什么改（本机实测）：**

- 三份实现的差额固定为 3 个打板键：`is_limit_up_today` / `consecutive_boards` / `board_position`（29 − 26）。v9 CSV 的合约是 26 列，直接换成 29 键会让 `d_signal_5d_features_v9.csv` 的列宽变 29。
- 第三份副本的**数值早就一致**：对同一帧 `fe.build_extended_features(df, code)` 与「29 键过滤后取 26 键」`dict ==` 为 `True`。所以它的 RED **只能是结构性的**（源码里还在自己算），写数值 parity 只会得到一条永远绿的假测试。
- `compute_window_features` 返回的 18 键与 `FEATURE_BASE` **逐字同序**（实测 `FEATURE_BASE == list(base)`），所以按 `FEATURE_BASE + NEW_FEATURES` 取键既能锁列宽也能锁列序，不需要再维护一份 `BOARD_KEYS` 过滤表。
- `scripts/predict.py:176` 的 `is_st` 是同义副本（返回值 int，内部 `'ST' in name`），归 `tdxfeed.names.is_st_name`；`NULL`/非字符串入参的历史行为（返回 0）在委托后不变（`is_st_name(None)` → `"NONE"` 不含 `ST`）。

- [ ] **Step 1: 写失败测试**

`tests/test_features_parity.py`：

```python
"""features_5d 是 26/29 维唯一实现：polars 复刻逐位一致，第三份副本只是取键。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import feature_engineering as fe  # noqa: E402

from tdxfeed.factors_polars import compute_window_features_extended_polars  # noqa: E402
from tdxfeed.features_5d import compute_window_features_extended  # noqa: E402

V9_KEYS = fe.FEATURE_BASE + [name for name, _ in fe.NEW_FEATURES]


def bars(n: int = 60, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 10 * (1 + np.cumsum(rng.normal(0, 0.012, n)))
    return pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=n),
        "open": close * (1 + rng.normal(0, 0.004, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.008, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.008, n))),
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n).astype(float),
        "amount": close * rng.integers(100_000, 1_000_000, n),
    })


@pytest.mark.parametrize("sig_idx", [-1, -3])
def test_pandas_and_polars_agree_on_29_keys(sig_idx):
    df = bars()
    a = compute_window_features_extended(df, "sh600519", sig_idx=sig_idx)
    b = compute_window_features_extended_polars(pl.from_pandas(df), "sh600519", sig_idx=sig_idx)
    assert a is not None and b is not None
    assert len(a) == len(b) == 29
    assert set(a) == set(b)
    for key in a:
        assert a[key] == pytest.approx(b[key], rel=1e-12, abs=1e-12, nan_ok=True), key


def test_third_copy_is_a_projection_of_the_authority():
    df = bars()
    got = fe.build_extended_features(df, "sh600519")
    ext = compute_window_features_extended(df, "sh600519")
    assert list(got) == V9_KEYS
    assert len(got) == 26
    for key in V9_KEYS:
        assert got[key] == pytest.approx(ext[key], rel=1e-12, abs=1e-12, nan_ok=True), key


def test_short_frame_returns_none():
    df = bars(n=4)
    assert compute_window_features_extended(df, "sh600519") is None
    assert fe.build_extended_features(df, "sh600519") is None


def test_third_copy_has_no_own_math():
    """结构性断言：数值早就一致，能抓到的回归是「有人把自算又抄回来」。"""
    src = inspect.getsource(fe.build_extended_features)
    assert "compute_window_features_extended(" in src
    for token in ("pct_change", "daily_range", "amount_3d_mean", "dist_high", "amt_per_price"):
        assert token not in src, token
```

`tests/test_names_single_source.py`：

```python
"""ST 判定单一来源：predict 不再自带一份字符串判断。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import predict  # noqa: E402

from tdxfeed.names import is_st_name  # noqa: E402


def test_predict_is_st_delegates_to_names():
    src = inspect.getsource(predict.is_st)
    assert "is_st_name(" in src
    assert "'ST' in" not in src


def test_predict_is_st_keeps_int_contract():
    assert predict.is_st("平安银行") == 0
    assert predict.is_st("*ST美丽") == 1
    assert predict.is_st(None) == 0


def test_predict_agrees_with_authority():
    for name in ("平安银行", "ST 康佳", "*ST美丽", "", None):
        assert predict.is_st(name) == int(is_st_name(name)), name
```

`tests/test_limit_single_source.py` 末尾追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_features_parity.py tests/test_names_single_source.py tests/test_limit_single_source.py -q`

Expected: **2 failed, 14 passed**。红的必须是这两条，且失败原因是结构断言（不是 ImportError）：

```
FAILED tests/test_features_parity.py::test_third_copy_has_no_own_math - AssertionError: pct_change
FAILED tests/test_names_single_source.py::test_predict_is_st_delegates_to_names - AssertionError
```

其余 14 条是锁：pandas/polars 29 键 parity（2 条参数化）、第三副本数值 parity、短帧 None、predict 取值、以及阶段 1 建的 6 条 limit 用例 + 新增的 2 条 quant 用例。若 `test_threshold_change_is_confined_to_689` 红了，先确认阶段 1 Task 7 的 `quant.get_limit` 委托已落地——这条是在替阶段 1 补证据，不是新 RED。

- [ ] **Step 3: 第三份副本改为取键**

`scripts/feature_engineering.py:38`：

```python
from _common import compute_window_features_extended
```

（原来的 `compute_window_features` 不再使用，一并删掉，否则 `make lint-all` 报未使用导入。）

`scripts/feature_engineering.py:68-121` 整个 `build_extended_features` 换成：

```python
def build_extended_features(window_df: pd.DataFrame, code: str) -> dict | None:
    """18 维基础 + 8 维工程化 = 26 维。全部计算委托 tdxfeed.features_5d。"""
    ext = compute_window_features_extended(window_df, code)
    if ext is None:
        return None
    return {key: ext[key] for key in FEATURE_BASE + [name for name, _ in NEW_FEATURES]}
```

（`FEATURE_BASE` / `NEW_FEATURES` 列表顺序就是 v9 CSV 的列序；3 个打板键不进 CSV 是既定合约，不改。）

- [ ] **Step 4: `predict.is_st` 委托**

`scripts/predict.py:173` 的 `tdxfeed` 导入块里加一行：

```python
from tdxfeed.names import is_st_name  # noqa: E402
```

`scripts/predict.py:176-181`：

```python
def is_st(name: str) -> int:
    """判断 ST（影响涨跌幅）"""
    return int(is_st_name(name))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_features_parity.py tests/test_names_single_source.py tests/test_limit_single_source.py -q`
Expected: **16 passed**

- [ ] **Step 6: 量化阈值变更的影响面（附进提交信息）**

```bash
venv/bin/python - <<'PY'
import re
import sys

sys.path.insert(0, "scripts")
import pandas as pd

from quant.multifactor.factors import get_limit
from tdxfeed.paths import daily_files


def legacy(code: str) -> float:      # 委托前 quant 的实现，逐字复刻
    s = re.sub(r"^(sh|sz|bj)", "", str(code))
    if s.startswith("920") or s.startswith("8"):
        return 0.295
    if s.startswith("688") or s.startswith("300") or s.startswith("301") or s.startswith("200"):
        return 0.195
    return 0.095


files = daily_files()
changed = [(fp.stem, legacy(fp.stem), get_limit(fp.stem))
           for fp in files if abs(legacy(fp.stem) - get_limit(fp.stem)) > 1e-12]
print("canonical", len(files))
print("threshold changed:", changed)

fp = next(p for p in files if p.stem == "sh689009")
df = pd.read_parquet(fp, columns=["close"])
pct = df["close"].pct_change()


def zt(limit: float):
    up = (pct >= limit - 0.001).fillna(False)
    return up.rolling(20, min_periods=5).mean()


a, b = zt(legacy("sh689009")), zt(get_limit("sh689009"))
print("zt_density rows differing:", int((a.fillna(-1) != b.fillna(-1)).sum()), "/", len(df))
PY
```

Expected（2026-10-28 实测）：

```
canonical 5236
threshold changed: [('sh689009', 0.095, 0.195)]
zt_density rows differing: 81 / 1047
```

`canonical` 与 `81` 会随行情追加变化，`threshold changed` **只允许有一个元素**。**不要**在提交信息里写「ST 票阈值修正」——委托不带 `is_st`，ST 偏差仍在（C12）。

- [ ] **Step 7: 重跑一次 mf 报表（后台，长任务）**

```bash
mkdir -p logs
nohup venv/bin/python scripts/plan_e_backtest.py --mf --mf-start 2015-01-01 --mf-end 2026-10-24 \
  > logs/mf_report_after_limit_20261028.log 2>&1 &
```

成本实测：50 只 / 2018 起 = 34 秒（13 分钟 CPU，多线程）；全池约 5200 只预计 **40–70 分钟**，所以放后台。完成判据：

```bash
tail -3 logs/mf_report_after_limit_20261028.log          # 含 "=== ✅ 完成 ==="
ls -t data/plan_e/plan_e_backtest_mf_v1_*.md | head -1   # 新时间戳的一对产物
```

报表本身进 `data/`（gitignore），不进提交；提交信息里附的是 Step 6 的行级 diff 与这份报表的路径、md5。

- [ ] **Step 8: 全量门禁**

Run: `make test`
Expected: 全绿（新增 16 条）。

- [ ] **Step 9: 提交**

```bash
git add scripts/feature_engineering.py scripts/predict.py tests/test_features_parity.py tests/test_names_single_source.py tests/test_limit_single_source.py
git commit -m "refactor(factors): 26 维第三份副本改为取键，predict 的 ST 判定并入 names"
```

---

### Task 11: MCP 契约 6/6 + math 工具表单一来源

**Files:**
- Create: `tests/test_mcp_contracts.py`
- Create: `v5/mcp_servers/math_tool_specs.py`（**由生成器产出**，不手写）
- Modify: `v5/mcp_servers/math_sympy.py`（`list_tools` 体 + 规格导入）
- Modify: `v5/mcp_servers/math_sympy_http.py`（`TOOL_NAMES` + 3 处旧计数文案）
- Modify: `v5/tests/test_math_sympy_http.py`（硬编码 `12` → `len(HTTP_TOOL_NAMES)`）
- Modify: `v5/README.md:49`（「9 个 Tool」→ 23）

**Interfaces:**
- Consumes:
  - `mcp/servers/{parquet_reader,calc,vault,news,finance_deep,backtest}.py` 的 `TOOLS: dict[str, {description, inputSchema, handler}]` 与 `handle_request(req) -> dict | None`
  - `mcp/schemas/tools.json`（`servers[key]["tools"]`、`tool_count`、`total_tools`）
  - `v5/mcp_servers/math_sympy.py` 的 `list_tools()`（20 个 `Tool`）、`call_tool(name, arguments)`
  - `v5/mcp_servers/math_sympy_http.py` 的 `TOOL_NAMES`、`_run_tool(name, arguments)`
- Produces（`v5.mcp_servers.math_tool_specs`）：
  - `STDIO_TOOL_SPECS: list[dict]`（20 条，每条恰 3 个键 `name/description/inputSchema`）
  - `STDIO_TOOL_NAMES: list[str]`（20）、`HTTP_ONLY_TOOL_NAMES: list[str]`（3）
  - `HTTP_TOOL_NAMES: list[str]`（23）= `STDIO_TOOL_NAMES + HTTP_ONLY_TOOL_NAMES`

**为什么改（本机实测）：**

- 6 个 server 的 `TOOLS` 键序与 `tools.json` 数组**逐字同序**（实测 6/6），所以契约可以断「列表相等」，比集合相等多锁一层顺序。
- 两边另有 **8 处 `inputSchema`、8 处 description 漂移**（`read_finance` / `read_xdxr` / `list_codes` / `calc_macd` / `calc_kdj` / `calc_rsi` / `calc_boll` / `calc_adj_factor` / `search` / `add_doc`），json 侧更详细。规格只要求名称与数量一致，本轮不逐字同步（C13），`inputSchema` 只断「合法 JSON Schema + `required ⊆ properties`」。
- stdio 20 个 `Tool(...)`、HTTP `TOOL_NAMES` 23 个，HTTP 多 3 个（`math_concept_link` / `math_bilingual_translate` / `math_ontology_stats`），见 C5。
- 生成器可行性已实测：`t.model_dump(exclude_none=True)` 每条恰 3 键，`Tool(**spec)` 重建后 `model_dump` **逐位相等**（20/20），`pprint` 输出也逐字节相同——所以「重跑生成器 → `git diff` 为空」是可靠的幂等判据。
- `math_sympy.py` 既可能被当脚本直跑（`v5/scripts/s7-mcp-servers.json` 与 systemd 都是绝对路径），也可能被当包导入（HTTP 侧 `from mcp_servers.math_sympy import ...`、`.github/workflows/quick-sop.yml` 的 `from v5.mcp_servers import math_sympy`），所以规格导入必须双模：`try: from .math_tool_specs ... except ImportError: from math_tool_specs ...`。

- [ ] **Step 1: 写失败测试**

`tests/test_mcp_contracts.py`：

```python
"""MCP 契约：schema 与实现同名同序；math 的 stdio/HTTP 工具表同源。"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

ROOT = Path(__file__).resolve().parents[1]

SERVER_MODULES = {
    "tdx-parquet-reader": "parquet_reader.py",
    "tdx-calc": "calc.py",
    "tdx-vault": "vault.py",
    "tdx-news": "news.py",
    "tdx-finance-deep": "finance_deep.py",
    "tdx-backtest": "backtest.py",
}


def _load(rel: str, name: str):
    """按文件路径加载独立模块（mcp/servers 不是包，避免 sys.path 互相打架）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dispatch_names(mod, func_name: str) -> list[str]:
    """抽取函数里所有 `name == "..."` 分支（if/elif 链，按源码顺序）。"""
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name)
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name) and node.left.id == "name"
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)):
            out.append(node.comparators[0].value)
    return out


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads((ROOT / "mcp" / "schemas" / "tools.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def servers() -> dict:
    return {key: _load(f"mcp/servers/{fn}", f"mcp_srv_{fn[:-3]}")
            for key, fn in SERVER_MODULES.items()}


@pytest.fixture(scope="module")
def math_specs():
    sys.path.insert(0, str(ROOT / "v5"))
    return importlib.import_module("mcp_servers.math_tool_specs")


def test_schema_totals(schema):
    assert len(schema["servers"]) == 6
    assert schema["tool_count"] == schema["total_tools"] == 23
    assert sum(len(s["tools"]) for s in schema["servers"].values()) == 23


def test_server_tools_match_schema(schema, servers):
    for key, mod in servers.items():
        declared = [t["name"] for t in schema["servers"][key]["tools"]]
        assert declared == list(mod.TOOLS), key


def test_each_tool_has_valid_schema(servers):
    for key, mod in servers.items():
        for name, spec in mod.TOOLS.items():
            assert spec["description"].strip(), (key, name)
            assert callable(spec["handler"]), (key, name)
            input_schema = spec["inputSchema"]
            validator_for(input_schema).check_schema(input_schema)
            assert set(input_schema.get("required", [])) <= set(input_schema.get("properties", {})), (key, name)


def test_calc_ma_over_jsonrpc(servers):
    resp = servers["tdx-calc"].handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "calc_ma", "arguments": {"prices": [1, 2, 3, 4, 5], "window": 2}},
    })
    assert "error" not in resp, resp.get("error")
    assert json.loads(resp["result"]["content"][0]["text"]) == [None, 1.5, 2.5, 3.5, 4.5]


def test_tools_list_endpoint_matches_registry(servers):
    resp = servers["tdx-calc"].handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in resp["result"]["tools"]] == list(servers["tdx-calc"].TOOLS)


def test_math_specs_shape(math_specs):
    specs = math_specs.STDIO_TOOL_SPECS
    assert len(specs) == 20
    assert {tuple(s) for s in specs} == {("name", "description", "inputSchema")}


def test_math_http_is_stdio_plus_three(math_specs):
    assert math_specs.HTTP_ONLY_TOOL_NAMES == [
        "math_concept_link", "math_bilingual_translate", "math_ontology_stats",
    ]
    assert math_specs.HTTP_TOOL_NAMES == math_specs.STDIO_TOOL_NAMES + math_specs.HTTP_ONLY_TOOL_NAMES
    assert len(math_specs.STDIO_TOOL_NAMES) == 20
    assert len(math_specs.HTTP_TOOL_NAMES) == 23


def test_math_dispatch_matches_names(math_specs):
    sys.path.insert(0, str(ROOT / "v5"))
    stdio = importlib.import_module("mcp_servers.math_sympy")
    http = importlib.import_module("mcp_servers.math_sympy_http")
    stdio_names = _dispatch_names(stdio, "call_tool")
    http_names = _dispatch_names(http, "_run_tool")
    assert set(stdio_names) == set(math_specs.STDIO_TOOL_NAMES) and len(stdio_names) == 20
    assert set(http_names) == set(math_specs.HTTP_TOOL_NAMES) and len(http_names) == 23
    assert http.TOOL_NAMES == math_specs.HTTP_TOOL_NAMES


def test_stdio_list_tools_is_generated(math_specs):
    tree = ast.parse((ROOT / "v5" / "mcp_servers" / "math_sympy.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_tools")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "Tool"]
    assert len(calls) == 1
    assert calls[0].keywords and calls[0].keywords[0].arg is None, "list_tools 应由 **spec 生成"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_mcp_contracts.py -q`

Expected: **5 passed, 1 failed, 3 errors**：

- 3 个 `ERROR`：`test_math_specs_shape` / `test_math_http_is_stdio_plus_three` / `test_math_dispatch_matches_names` —— `ModuleNotFoundError: No module named 'mcp_servers.math_tool_specs'`；
- 1 个 `FAILED`：`test_stdio_list_tools_is_generated`（当前 `list_tools` 里有 20 个 `Tool(...)`）。

若 `test_server_tools_match_schema` 红，说明有人刚改过 `tools.json` 或注册表，先查清再继续。

- [ ] **Step 3: 生成工具表**

```bash
venv/bin/python - > v5/mcp_servers/math_tool_specs.py <<'PY'
import asyncio
import pprint
import sys

sys.path.insert(0, "v5")

from mcp_servers import math_sympy

specs = [t.model_dump(exclude_none=True) for t in asyncio.run(math_sympy.list_tools())]
HTTP_ONLY = ["math_concept_link", "math_bilingual_translate", "math_ontology_stats"]

print('"""math 工具表单一来源：stdio 的 list_tools 与 HTTP 的工具文档都由它生成。')
print()
print("本文件由实施计划 Task 11 Step 3 的生成器产出，不要手改；")
print("改工具签名请改 math_sympy.py 后重跑生成器。")
print('"""')
print("from __future__ import annotations")
print()
print("HTTP_ONLY_TOOL_NAMES = [")
for name in HTTP_ONLY:
    print(f'    "{name}",')
print("]")
print()
print("STDIO_TOOL_SPECS =")
print(pprint.pformat(specs, width=100, sort_dicts=False))
print()
print('STDIO_TOOL_NAMES = [s["name"] for s in STDIO_TOOL_SPECS]')
print("HTTP_TOOL_NAMES = STDIO_TOOL_NAMES + HTTP_ONLY_TOOL_NAMES")
print()
print("__all__ = [")
print('    "HTTP_ONLY_TOOL_NAMES",')
print('    "HTTP_TOOL_NAMES",')
print('    "STDIO_TOOL_NAMES",')
print('    "STDIO_TOOL_SPECS",')
print("]")
PY
```

生成后自检（不要跳过）：

```bash
venv/bin/python -c "import sys; sys.path.insert(0,'v5'); from mcp_servers import math_tool_specs as s; print(len(s.STDIO_TOOL_SPECS), len(s.HTTP_TOOL_NAMES))"
```

Expected: `20 23`

- [ ] **Step 4: `list_tools` 改为读表**

`v5/mcp_servers/math_sympy.py`，在 `from mcp.types import ...` 那段 `try/except ImportError` 之后加：

```python
try:  # 作为包被导入（HTTP 侧 / 测试 / quick-sop 的 from v5.mcp_servers import ...）
    from .math_tool_specs import STDIO_TOOL_SPECS
except ImportError:  # 作为脚本直跑（s7-mcp-servers.json 与 systemd 都是绝对路径直跑）
    from math_tool_specs import STDIO_TOOL_SPECS
```

`@server.list_tools()` 下的整个 `return [ Tool(...), ... ]` 字面量（20 个 `Tool`）换成：

```python
@server.list_tools()
async def list_tools() -> list:
    return [Tool(**spec) for spec in STDIO_TOOL_SPECS]
```

（`Tool` 的导入保留——这里仍要用；`call_tool` 的 20 个分支**不动**。）

- [ ] **Step 5: HTTP 侧读同一张表**

`v5/mcp_servers/math_sympy_http.py`：

1. 在 `from mcp_servers.math_sympy import (...)` 之后加：

```python
from math_tool_specs import HTTP_TOOL_NAMES  # noqa: E402
```

（该文件已把 `_PKG_DIR` 插进 `sys.path`，所以绝对导入可用。）

2. `TOOL_NAMES = [ ... ]` 整段（23 行字符串字面量）换成：

```python
# 工具清单的唯一来源：math_tool_specs（stdio 20 + HTTP 独有 3）
TOOL_NAMES = list(HTTP_TOOL_NAMES)
```

3. 三处旧计数文案：

| 位置 | 原文 | 改为 |
|---|---|---|
| 模块 docstring 首段 | `- HTTP/JSON 端点暴露 9 个 Tool` | `- HTTP/JSON 端点暴露 23 个 Tool（完整清单见 math_tool_specs.HTTP_TOOL_NAMES）` |
| `call_tool` 路由上方注释 | `# 注册所有 12 个端点` | `# 注册所有 23 个端点（清单见 TOOL_NAMES）` |
| `main()` 启动日志 | `log.info("  9 个 Tool 端点: POST /tools/{name}")` | `log.info(f"  {len(TOOL_NAMES)} 个 Tool 端点: POST /tools/{{name}}")` |

- [ ] **Step 6: 去掉旁路硬编码**

`v5/tests/test_math_sympy_http.py`：

- 头部加 `sys.path.insert(0, "/home/jiuben/tdx-data-feed/v5/mcp_servers")` 与 `from math_tool_specs import HTTP_TOOL_NAMES`（该脚本是独立验收脚本，`sys.path` 里没有这个目录）；
- `check("tool_count=12", data.get("tool_count") == 12)` → `check(f"tool_count={len(HTTP_TOOL_NAMES)}", data.get("tool_count") == len(HTTP_TOOL_NAMES))`；
- `check("有 12 个 tools", len(data.get("tools", [])) == 12)` → `len(...) == len(HTTP_TOOL_NAMES)`；
- docstring 里 `POST /tools/{tool_name}（9 个 Tool）` → `（23 个 Tool）`。

`v5/README.md:49`：`math_sympy.py ← MCP server 主入口（9 个 Tool）` → `（20 个 Tool，HTTP 侧另有 3 个传输专用工具，见 math_tool_specs.py）`。

- [ ] **Step 7: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_mcp_contracts.py -q`
Expected: **9 passed**

Run: `venv/bin/pytest v5/tests/test_math_sympy.py -q`
Expected: 全绿（stdio 侧 20 个注册名不变）。

- [ ] **Step 8: 证明生成器幂等**

```bash
venv/bin/python - > /tmp/math_tool_specs_regen.py <<'PY'
import asyncio
import pprint
import sys

sys.path.insert(0, "v5")

from mcp_servers import math_sympy

specs = [t.model_dump(exclude_none=True) for t in asyncio.run(math_sympy.list_tools())]
HTTP_ONLY = ["math_concept_link", "math_bilingual_translate", "math_ontology_stats"]

print('"""math 工具表单一来源：stdio 的 list_tools 与 HTTP 的工具文档都由它生成。')
print()
print("本文件由实施计划 Task 11 Step 3 的生成器产出，不要手改；")
print("改工具签名请改 math_sympy.py 后重跑生成器。")
print('"""')
print("from __future__ import annotations")
print()
print("HTTP_ONLY_TOOL_NAMES = [")
for name in HTTP_ONLY:
    print(f'    "{name}",')
print("]")
print()
print("STDIO_TOOL_SPECS =")
print(pprint.pformat(specs, width=100, sort_dicts=False))
print()
print('STDIO_TOOL_NAMES = [s["name"] for s in STDIO_TOOL_SPECS]')
print("HTTP_TOOL_NAMES = STDIO_TOOL_NAMES + HTTP_ONLY_TOOL_NAMES")
print()
print("__all__ = [")
print('    "HTTP_ONLY_TOOL_NAMES",')
print('    "HTTP_TOOL_NAMES",')
print('    "STDIO_TOOL_NAMES",')
print('    "STDIO_TOOL_SPECS",')
print("]")
PY
diff /tmp/math_tool_specs_regen.py v5/mcp_servers/math_tool_specs.py && echo IDEMPOTENT
```

Expected: `IDEMPOTENT`（`diff` 无输出）。这一步同时证明 `Tool(**spec)` 往返无损；若 diff 非空，说明规格里有重建时会被补默认值的字段，停下来报告，不要手改生成物。

- [ ] **Step 9: 提交**

```bash
git add v5/mcp_servers/math_tool_specs.py v5/mcp_servers/math_sympy.py v5/mcp_servers/math_sympy_http.py v5/tests/test_math_sympy_http.py v5/README.md tests/test_mcp_contracts.py
git commit -m "refactor(mcp): math 工具表单一来源，6 个 server 契约测试进 CI"
```

---

### Task 12: 文档约定 + 总门禁

**Files:**
- Modify: `AGENTS.md`（在 `## 风险边界（动手前读）` 之前插入一节）
- Modify: `docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md`（文末追加 `## 10. 实施期更正`）
- Modify: `.github/workflows/tests.yml:35`（通过数下限）

**Interfaces:**
- Consumes: Task 1–11 的全部产物（`paths` 访问器与孪生、AST 守卫、`write_table`、`derived` 重建证据、26 维单一实现、MCP 契约测试）与阶段 1/2 已入库的提交。
- Produces: `AGENTS.md` 的 `## 数据路径与表口径` 五条约定、规格 §10 的 `C4′`–`C15` 更正表、`.github/workflows/tests.yml` 的具体通过数下限；不发新代码。

- [ ] **Step 1: AGENTS.md 插入数据路径约定**

在 `## 风险边界（动手前读）` 这一行之前插入（整段，不夹数字——文件数与行数会漂）：

```markdown
## 数据路径与表口径

- 定位只从 `tdxfeed/paths.py` 取：脚本侧 `from _common import ...` 是它的再导出桥，`v5/scripts/_paths.py` 是零耦合孪生（v5 不 import tdxfeed）。业务代码不写 `data/parquet/...` 字面量，守卫 `tests/test_no_hardcoded_data_paths.py`。
- 表名**不统一**，按既存事实：日线/分钟表文件带市场前缀（`sh600000.parquet`），`adj_factor` / `hfq_factor` 是裸 6 位（`600000.parquet`），`derived` 是前缀 + `_indicators`（`sh600000_indicators.parquet`）。要枚举标的池用 `paths.daily_files()`——它排除 `.v1.bak.parquet` 迁移备份；自己 `glob("*.parquet")` 会把备份当股票。
- 写盘只走 `tdxfeed.parquet_io.write_table`（`date` 列归一成 `date32[day]`，tmp→`os.replace` 换 inode）。派生表备份依赖这个换 inode 语义，`cp -al` 出来的快照才不会被原地改写。分钟表的时间列**不叫** `date`、是 `string`（`YYYY-MM-DD HH:MM:SS`），原样写。
- 判涨停用 `limit_threshold`（精确比例减 0.5% 缓冲），算挂单价用 `limit_ratio` + `limit_up_price`（精确比例、Decimal 取整到分）。两者是两套合同，不可互换；两者都在 `tdxfeed/execution/limits.py`。
- `data/`、`logs/` 不进 git。`derived` 全量重建的证据是 `data/parquet/derived_pre_<日期>/MANIFEST.txt` + 重建日志，不是「文件数看起来对」。
```

- [ ] **Step 2: 规格追加实施期更正**

规格文末可能已有阶段 1 Task 16 追加的 `## 10. 实施期更正（2026-10-28）`。**先 `grep -n '实施期更正' 规格文件` 确认**：

- 已有该节 → 把下面的表格与段落**并入**该节（导语改成指向三份计划），**不要**新建第二个同名标题；
- 没有 → 在 `## 9. 明确不做` 之后新建一节。

要并入/新建的内容：

```markdown
## 10. 实施期更正

本节记录实施期发现的规格偏差，编号与三份计划文档开头的更正表一致（`2026-10-28-settle-kernel-phase1.md` 的 §3.5 四项、`2026-10-28-label-retrain-phase2.md` 的 §6 阶段 3 两项，在本表编号为 C14/C15；本阶段自身为 C4′–C13）；正文保持批准时的原文不动，冲突时以本节为准。

| 编号 | 原文 | 实测 |
|---|---|---|
| C4′ | §4.2 的 47 文件统计 | AST 守卫口径是 90 文件 / 119 行（scripts 58、tdxfeed 6、quant 2、mcp 3、train 2、v5 19），另有 5 处字符串拼接型守卫抓不到，靠人工清单兜。 |
| C5 | §5.2 math 双传输工具集一致 | stdio 20 个，HTTP 23 个，HTTP 多 `math_concept_link` / `math_bilingual_translate` / `math_ontology_stats`。parity 写成 `HTTP == stdio ∪ HTTP_ONLY`。 |
| C6 | §4.3 derived 是 int64 | derived 的 `date` 实为 `timestamp[ns]`；重建目标统一到 `date32[day]`。 |
| C7 | §4.1 第 4 个写入端是 `amount_fix.py` | `amount_fix.py` 不写盘；真正的第 4 端是 `scripts/download_universe.py`，写的是 int64 日期。 |
| C8 | §4.3 重建 5200 个 | 按当前 canonical `daily` 重建为 5236 个（多 8 个指数 + 28 只新上市），且不含 `*.v1.bak*`。 |
| C9 | §4.1 `finance_path()` | 实际命名 `finance_parquet()`。 |
| C10 | §4.1 分钟表写 `YYYY-MM-DD HH:MM:SS` | 时间列名是 `datetime`（`string`），`write_table` 只归一 `date` 列。 |
| C11 | §5.1-3 第三份副本改为调用 `factors_polars` | 改调 `features_5d.compute_window_features_extended` 并取 26 键；`factors_polars` 是迁移期复刻且只接受 polars DataFrame。 |
| C12 | §5.1-2 委托后 ST 票会变值 | 委托不带 `is_st`（无按日 ST 历史），主板阈值不变、ST 偏差仍在；实际变值的是 `689` 段，全库只有 `sh689009` 受影响。 |
| C13 | §5.2-1 tools.json 与注册表一致 | 名称与顺序 6/6 一致；另有 8 处 `inputSchema`、8 处描述漂移（json 侧更详细），本轮只锁名称/数量/必填自洽。 |
| C14 | §6 阶段 3-1 标签写 `d_signal_5d_features_vpnl_v2.csv`、旧文件不动 | 同路径重写 + `data/double_up/_pre_settle_kernel_20261028/` 快照与 md5 清单。`train_pnl_proxy` / `build_wide_table` / `predict.py` 三处读的都是硬编码路径，另起 v2 链等于长期维护两条真值。 |
| C15 | §6 阶段 3-2/3-3 与 §8：宽表/模型出 `_v2` 文件名，模型切换改 `predict.py` 一行 | 宽表与模型保持原路径，`predict.py` 一行不改；v1/v2 对照改为从快照并行读新旧两份算同一批指标，结论与回滚写在 `docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md`。 |

C12 的遗留：`quant/multifactor/factors.get_limit` 没有 ST 分支，`zt_density_20d` 在 2026-07-06 并轨前的 ST 票上仍会少数涨停。补齐它需要**按交易日的 ST 历史**，仓库里只有 `data/all_a_stocks.json` 的当前名称快照（`tdxfeed.names.load_universe()`），拿当前名称回填全历史会把并轨后的日期也按 5% 判。因此本轮不做，列入「明确不做」，等有 ST 变更流水再开。
```

- [ ] **Step 3: 无数据演练 + 上调 CI 下限**

```bash
TDX_TEST_DATA_ROOT=/nonexistent venv/bin/pytest tests/ -q 2>&1 | tail -3
```

记下 `passed` 数（记作 `N`）。把 `.github/workflows/tests.yml:35` 的阈值改成 **N − 10**（留种子/平台余量，数字必须是十进制的具体值，不能留占位符）。改完核对：

```bash
grep -n 'passed:-0' .github/workflows/tests.yml
venv/bin/python - <<'PY'
import re, pathlib
line = [l for l in pathlib.Path(".github/workflows/tests.yml").read_text().splitlines()
        if "passed:-0" in l][0]
print(line)
assert re.search(r"-ge\s+\d+\s*$", line), "阈值必须是行尾的具体数字"
PY
```

下限只用来防空跑，不是覆盖率指标；实测值写进提交信息。

- [ ] **Step 4: 有数据全量门禁**

```bash
make lint-all
make test
```

Expected: 两条都绿。`make test` 会跑 `tests/` + `v5/tests/` 两套；本阶段新增守卫（`test_data_layer` / `test_no_hardcoded_data_paths` / `test_parquet_schema_contract` / `test_daily_listing_and_resolve` / `test_write_entry_single_source` / `test_features_parity` / `test_names_single_source` / `test_mcp_contracts`）必须全部被收集。

- [ ] **Step 5: 收尾自检（本阶段的验收面）**

```bash
venv/bin/pytest tests/test_no_hardcoded_data_paths.py -q
venv/bin/pytest tests/test_daily_listing_and_resolve.py tests/test_write_entry_single_source.py -q
venv/bin/pytest tests/test_parquet_schema_contract.py -q
venv/bin/pytest tests/test_features_parity.py tests/test_mcp_contracts.py -q
venv/bin/python -c "from tdxfeed.paths import daily_files; print(len(daily_files()))"
```

Expected: 前四条全绿；最后一行打印 canonical 数（2026-10-28 是 5236，随上市/退市漂移）。任一红都说明本阶段的某条收编没落地，不要提交文档。

- [ ] **Step 6: 五个定时脚本各手动跑一轮（规格 §7 第 6 条）**

这不是 Global Constraints 里禁的「逐脚本 `--help` 烟测」，而是验收动作：命令逐字取自单元文件的 `ExecStart`，跑一次只取退出码。

```bash
venv/bin/python scripts/plan_e_backtest.py --today
venv/bin/python scripts/plan_e_journal.py --mode daily
venv/bin/python scripts/find_double_up_c.py
venv/bin/python scripts/daban_entry.py
venv/bin/python scripts/predict.py --position-mode small --threshold 0.7 --workers 8
```

Expected: 五条都退出 0。读退出码时注意三件事：

- `plan_e_journal.py` 的 `PLAN_E_PREMARKET_LIST_ACTIVE = False` 是有意关闭的（18:10 那轮读当日收盘、`entry_date` 落在明天，会被新鲜度守卫丢掉），「没出清单」不是失败。
- `daban_entry.py` 依赖盘中行情：非交易时段跑会空转，退出 0 不代表能出单；**它只产人工委托单文件**，不会下单。
- `predict.py` 需要 `data/parquet/daily` 有当日/前一日数据；若 `scripts/precheck_freshness.sh` 先返回 2（数据未到位），说明同步没跑，先跑 `systemctl --user start tdx-sync` 再来。

产出全部落 `data/` 与 `logs/`（gitignore），不进提交；把五条的退出码贴进提交信息。

- [ ] **Step 7: 提交**

```bash
git add AGENTS.md .github/workflows/tests.yml docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md
git commit -m "docs(agents): 数据路径与表口径约定 + 规格实施期更正 + CI 通过数下限上调"
```

---

## 阶段 3+4 收尾

1. 路径层：`tdxfeed/paths.py` 是唯一回答；`scripts/_common.py` 再导出；`v5/scripts/_paths.py` 零耦合孪生；AST 守卫 0 命中。
2. 写层：`write_table` 唯一实现（tmp→rename、`date32`），6 个写入端已收口，`writers.py:88` 的非日线写盘按计划保留。
3. 数据：`derived` 5236 个按 `date32` 重建，快照与 MANIFEST 可回溯。
4. 因子：26/29 维单一实现，第三份副本只剩取键；`get_limit` 单源，689 段的修正有行级证据（81/1047），ST 偏差如实记为遗留（C12）。
5. MCP：6/6 服务器契约进 CI；math 的 stdio/HTTP 工具表由 `math_tool_specs.py` 生成，幂等可验。
6. 文档：`AGENTS.md` 的三条约定齐（结算走 execution、parquet 走 paths/parquet_io、`limit_ratio` ≠ `limit_threshold`），规格 §10 记录了 10 处偏差。
