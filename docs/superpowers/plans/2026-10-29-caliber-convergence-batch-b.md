# 批次 B：口径收敛（涨跌停阈值 + 市场前缀 + 假绿门禁）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「是否涨停」只剩 `tdxfeed/execution/limits.py` 一个实现，让「代码 → 交易所前缀」只剩 `tdxfeed/paths.exchange_prefix` 一个实现，并让两条会吞失败的 CI 步骤真正会红。

**Architecture:** 六个研究脚本各有一份本地阈值表（`0.095/0.195/0.295` 内联或函数），改规则时不会同步；其中 `daban_event_table.py` 漏北交所、`factor_engine.py` 把 B 股 200 当双创。统一改为从 `scripts/_common.py` 再导出的 `features_5d.get_limit` → 内核 `limits.limit_threshold`。市场前缀侧，21 个脚本都 import `tdxfeed.daban.limit_price.exchange_prefix`，而它先 normalize 再重推，导致 `sh000001`（上证指数）被判成 `sz`；改为直接委托 `tdxfeed.paths.exchange_prefix`（保留入参前缀）。CI 侧修 `quick-sop.yml` 的 try/except 只 print 与 `--collect-only`，以及 `docker-publish.yml` 的 Trivy `continue-on-error`。

**Tech Stack:** Python 3.14 / pytest / pandas / PyYAML / GitHub Actions。

**Spec:** 本仓库无独立规格文件；权威口径在 `tdxfeed/execution/limits.py` 模块 docstring 与 `AGENTS.md`「数据路径与表口径」「回测成交口径」两节。涨跌停两个合同（判定阈值 vs 挂单比例）不可互换。

## Global Constraints

- 共享工作树：`git add` / `git commit` 必须带显式路径，禁止 `-A` 与 `.`；不碰他人脏文件（当前 `scripts/backfill_minutes.py`、`tdxfeed/kline.py`、`v5/audit/verification/latest.json` 与本批次无关）。
- 判定阈值只走 `limits.limit_threshold`（0.095/0.195/0.295）；挂单价只走 `limits.limit_ratio` + `limit_up_price`。不得把两者互换。
- 北交所代码段 = `920` / `8` / `4`；双创 = `300` / `301` / `688` / `689`；其余主板。
- 脚本侧统一从 `scripts/_common.py` 取 `get_limit`（它已委托 `features_5d.get_limit`）。
- 前缀解析：入参自带 `sh` / `sz` / `bj` 时以入参为准，不得重推（`sh000001` ≠ `sz000001`）。
- 每条回复用中文；长跑前先跑最便宜的证伪测试；提交前先验证。

---

### Task 1: 涨跌停判定阈值收口到内核

**Files:**
- Modify: `scripts/daban_event_table.py:183`
- Modify: `scripts/factor_engine.py:67-73`（删本地 `get_limit`，改 import）
- Modify: `scripts/top10_deep_dive.py:30-41`
- Modify: `scripts/find_double_up_c.py:39-41,69-84`
- Modify: `scripts/find_double_up_c_v2.py:40-42,55-67`
- Modify: `scripts/v3_premarket_top.py:54-66`
- Test: `tests/test_limit_single_source.py`

**Interfaces:**
- Consumes: `scripts/_common.py` 的 `get_limit(code, name="", on_date=None) -> float`（再导出 `features_5d.get_limit`，其值等于 `limits.limit_threshold`）。
- Produces: 六个脚本模块内 `get_limit` 均为 `_common.get_limit` 的同一函数对象；`daban_event_table` 不再有内联阈值。

- [x] **Step 1: 写失败守卫**

在 `tests/test_limit_single_source.py` 末尾追加：

```python
SCRIPT_GET_LIMIT = (
    "scripts/daban_event_table.py",
    "scripts/factor_engine.py",
    "scripts/top10_deep_dive.py",
    "scripts/find_double_up_c.py",
    "scripts/find_double_up_c_v2.py",
    "scripts/v3_premarket_top.py",
)
ROOT = Path(__file__).resolve().parents[1]


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
```

（文件头需要 `from pathlib import Path`。）

- [x] **Step 2: 跑守卫确认红**

Run: `venv/bin/pytest tests/test_limit_single_source.py::test_scripts_have_no_local_limit_table -q`
Expected: FAIL，offenders 列出 6 个文件。

- [x] **Step 3: 六处改委托**

`daban_event_table.py`：
```python
from _common import get_limit, minute_dir  # noqa: E402
...
    threshold = get_limit(code)
```
（保留注释「近 20 日"大涨"次数（主板 9.5%，双创 19.5%，北交所 29.5%）；ST 已从可执行池剔除。」）

`factor_engine.py`：import 行补 `get_limit`，删除本地 `def get_limit`（保留 `get_board`）。

`top10_deep_dive.py`：`from _common import daily_dir, get_limit`，删除 `LIMIT_MAP` 与本地 `def get_limit`。

`find_double_up_c.py` / `find_double_up_c_v2.py`：`from _common import ..., get_limit`，删除 `LIMIT_MAIN/LIMIT_GEM/LIMIT_BSE` 与本地 `def get_limit`。

`v3_premarket_top.py`：import 行补 `get_limit`，删除本地 `def get_limit`。

- [x] **Step 4: 跑守卫与脚本相关测试**

Run: `venv/bin/pytest tests/test_limit_single_source.py tests/test_daban_event_table.py tests/test_double_up_sample_refresh.py -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add scripts/daban_event_table.py scripts/factor_engine.py scripts/top10_deep_dive.py scripts/find_double_up_c.py scripts/find_double_up_c_v2.py scripts/v3_premarket_top.py tests/test_limit_single_source.py
git commit -m "refactor(limits): 六个脚本的涨跌停阈值改委托内核，北交所不再漏判"
```

---

### Task 2: 市场前缀收口到 paths.exchange_prefix

**Files:**
- Modify: `tdxfeed/daban/limit_price.py:77-82`
- Test: `tests/test_limit_single_source.py`

**Interfaces:**
- Consumes: `tdxfeed.paths.exchange_prefix(code) -> str`（入参前缀优先）。
- Produces: `tdxfeed.daban.limit_price.exchange_prefix is tdxfeed.paths.exchange_prefix`（同一函数对象），21 个 import 它的脚本自动跟随。

- [x] **Step 1: 写失败测试**

```python
def test_daban_exchange_prefix_delegates_to_paths():
    from tdxfeed import paths
    assert limit_price.exchange_prefix is paths.exchange_prefix


def test_daban_exchange_prefix_keeps_explicit_prefix():
    """sh000001 是上证指数；重推会指到 sz000001 平安银行。"""
    assert limit_price.exchange_prefix("sh000001") == "sh"
    assert limit_price.exchange_prefix("sz000001") == "sz"
    assert limit_price.exchange_prefix("000001") == "sz"
```

- [x] **Step 2: 跑测试确认红**

Run: `venv/bin/pytest tests/test_limit_single_source.py -q -k prefix`
Expected: `test_daban_exchange_prefix_delegates_to_paths` 与 `..._keeps_explicit_prefix` FAIL。

- [x] **Step 3: 改委托**

`tdxfeed/daban/limit_price.py` 顶部 import `from tdxfeed.paths import exchange_prefix as _paths_exchange_prefix`，函数体改为：

```python
def exchange_prefix(code: str) -> str:
    """交易所前缀：sh / sz / bj。入参自带前缀时以入参为准（sh000001 ≠ sz000001）。"""
    return _paths_exchange_prefix(code)
```

保留本模块 docstring 中对两个合同差异的说明。

- [x] **Step 4: 跑测试**

Run: `venv/bin/pytest tests/test_limit_single_source.py tests/test_paths_and_names.py tests/test_daban_mcp.py -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add tdxfeed/daban/limit_price.py tests/test_limit_single_source.py
git commit -m "fix(daban): exchange_prefix 委托 paths，显式前缀不再被重推"
```

---

### Task 3: CI 假绿门禁

**Files:**
- Modify: `.github/workflows/quick-sop.yml:38-83`
- Modify: `.github/workflows/docker-publish.yml:167-180`
- Test: `tests/test_ci_workflows_not_fake_green.py`（新建）

**Interfaces:**
- Consumes: PyYAML（已在 `requirements-ci.txt`）。
- Produces: 一条守卫测试，断言 smoke 步骤不吞导入异常、Trivy 扫描步骤不 `continue-on-error`。

- [x] **Step 1: 写失败守卫**

```python
"""CI 里「跑完永远绿」的步骤必须被拆掉：导入失败要红，扫描失败要红。"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NEEDS_DATA = False


def _steps(workflow: str) -> dict[str, dict]:
    data = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8"))
    return {s.get("name", ""): s for s in data["jobs"].popitem()[1]["steps"]}


def test_quick_sop_smoke_step_does_not_swallow_import_errors():
    step = _steps("quick-sop.yml")["业务模块可导入性 smoke test"]
    run = step["run"]
    assert "except Exception" not in run
    assert "|| true" not in run


def test_quick_sop_runs_tests_not_only_collects():
    for step in _steps("quick-sop.yml").values():
        run = step.get("run", "")
        if "pytest" in run:
            assert "--collect-only" not in run


def test_docker_trivy_scan_is_blocking():
    step = _steps("docker-publish.yml")["Scan image (Trivy)"]
    assert step.get("continue-on-error") is not True
    assert str(step["with"].get("exit-code", "1")) == "1"
```

- [x] **Step 2: 跑守卫确认红**

Run: `venv/bin/pytest tests/test_ci_workflows_not_fake_green.py -q`
Expected: 3 条都 FAIL。

- [x] **Step 3: 改两个 workflow**

`quick-sop.yml`：安装依赖改为 `pip install -r requirements-ci.txt pytest`；smoke 步骤改成直接逐个 import（失败即非零）：

```yaml
      - name: 业务模块可导入性 smoke test
        run: |
          python - <<'PY'
          import v5.mcp_math.strategy_selector  # noqa: F401
          import v5.mcp_math.derivation  # noqa: F401
          import v5.mcp_math.math_cache  # noqa: F401
          import v5.mcp_servers.math_sympy  # noqa: F401
          import v5.mcp_servers.math_sympy_http  # noqa: F401
          print("smoke ok")
          PY
```

pytest 步骤改为真跑（去掉 `--collect-only`）：
```yaml
      - name: pytest (冒烟子集)
        run: |
          pytest tests/test_limit_single_source.py v5/tests/test_alerting_pipeline.py -q 2>&1 | tee /tmp/pytest-smoke.txt
```
（artifact 路径同步改名。）

`docker-publish.yml`：Trivy 步骤去掉 `continue-on-error: true`，`with` 加 `exit-code: '1'`；上传 SARIF 保留 `continue-on-error` 但加 `if: always() && hashFiles('trivy-results.sarif') != ''`。

- [x] **Step 4: 跑守卫与 YAML 语法检查**

Run: `venv/bin/pytest tests/test_ci_workflows_not_fake_green.py -q && venv/bin/python -c "import yaml,pathlib; [yaml.safe_load(p.read_text()) for p in pathlib.Path('.github/workflows').glob('*.yml')]"`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add .github/workflows/quick-sop.yml .github/workflows/docker-publish.yml tests/test_ci_workflows_not_fake_green.py
git commit -m "ci: smoke 导入失败即红、Trivy 高危阻断发布"
```

---

### Task 2b: 日线定位 sweep（执行中追加）

**背景：** Task 2 只修了「代码→前缀」这一个函数，但仍有 25 处脚本自己试前缀。
daily 目录里 000001/000016/000688/000852/000905 同时存在 sh 与 sz 文件，
`for prefix in ("sh","sz","bj")` 先命中 sh，会把个股读成同名指数
（实测 sh000001 收盘 3651.434、sz000001 平安银行 11.54）。

**Files:**
- Modify: `scripts/llm_signal_plan_e.py`、`scripts/build_v4_forward_labels.py`、
  `scripts/plan_e_sim_live.py`、`scripts/monitor_plan_e_live.py`、
  `scripts/verify_daban_hit.py`、`scripts/bench_v567.py`、`scripts/bench_v7r.py`、
  `scripts/build_cls2000.py`
- Modify: `v5/scripts/` 14 个文件 17 处（v4_aggregate_report / v4_historical_roll /
  v4_poc_analysis / v4_seq_only / v5_eval / v5_v6_compare / v6_data_prep / v6_eval /
  v6_eval_direct / v6_seq_fixed / v6_seq_only / v6_vs_v4_baseline / v6_vs_v4_seq / vault_to_v5）
- Modify: `scripts/_common.py`（`norm_code` 委托 `limits.normalize_code`）
- Test: `tests/test_daily_lookup_single_source.py`（新建）

**Interfaces:**
- Consumes: `tdxfeed.paths.daily_path(code) -> Path`（入参前缀优先，裸码按代码段推断）。
- Produces: 脚本侧定位日线一律 `daily_path(code)`；静态守卫禁 `for prefix in (…)` 探针。

- [x] **Step 1: 写失败守卫**：扫 `scripts/` 与 `v5/scripts/`，出现
  `for prefix in ("sh", "sz", "bj")` 或 `("", "sh", "sz", "bj")` 即失败；
  另钉 17 个已收编文件必须含 `daily_path(`，并断言 `_common.norm_code` 与
  `limits.normalize_code` 同值。
- [x] **Step 2: 跑守卫确认红**：14 个 v5 文件命中（scripts 侧已先改完）。
- [x] **Step 3: 批量改写**：探针换 `p = daily_path(code)` + `if not p.exists(): return <原默认>`；
  `find_parquet` 型保留 `return p if p.exists() else None`；删除仅探针使用的 `PARQUET_DIR`。
- [x] **Step 4: 跑测试**：守卫 3 passed；路径/阈值相关 50 passed；受影响脚本测试 24 passed；
  真实调用 `000001` 全部得到 11.54，`sh000001` 仍是 3651。
- [x] **Step 5: 提交**：`ea8cff9 refactor(paths): 日线定位统一走 daily_path，收编 25 处试前缀探针`。

---

### Task 4: 总门禁

- [x] **Step 1: 跑相关测试全集**

Run: `venv/bin/pytest tests/test_limit_single_source.py tests/test_paths_and_names.py tests/test_daban_event_table.py tests/test_double_up_sample_refresh.py tests/test_ci_workflows_not_fake_green.py tests/test_no_hardcoded_data_paths.py tests/test_write_entry_single_source.py -q`
Expected: PASS。

- [x] **Step 2: 跑 lint**

Run: `make lint-all`
Expected: 无阻断项（E9,F63,F7,F82）。

- [x] **Step 3: 全量测试（后台）**

Run: `make test`（后台跑，完成后核对 passed 数不低于批次 A 的 998/750 基线）。

- [x] **Step 4: 更新 AGENTS.md 与账本**

在 `AGENTS.md`「数据路径与表口径」补一行：涨跌停判定与市场前缀各只有一个入口；CI 两处假绿已拆。账本 `.superpowers/sdd/2026-10-29-data-freshness-batch-a/progress.md` 追加批次 B 段。

- [x] **Step 5: 提交**

```bash
git add AGENTS.md
git commit -m "docs: 批次 B 口径收敛落地记录"
```
