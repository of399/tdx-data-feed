# 阶段 2：标签重算与 stage1 重训 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 阶段 1 改了 PnL 标签的结算口径（入场次根才判定、双触发结算亏损侧、跳空按开盘），现有 `hypothetical_pnl` 列与已训练模型全部是旧口径产物。本阶段冻结旧基线、用新口径重算标签、重装配宽表、用冻结的 21 维与超参重训，并给出可回滚的对照结论。

**Architecture:** 顺序为「冻结 → 重算 → 重装配 → 重训 → 对照 → 决策」。每一步都用前一步的产物做输入，且每步都有独立校验点。重训**不重跑 Optuna**：特征集与超参取自 `stage1_selected_features.json`（21 维 + 冻结超参），只换标签。

**Tech Stack:** pandas、LightGBM（`venv/bin/python`）、现有 `scripts/train_pnl_proxy.py` / `tdxfeed/stage1_wide_table.py` / `scripts/stage1_retrain.py`。

**Spec:** `docs/superpowers/specs/2026-10-28-settle-kernel-and-data-authority-design.md`（§2.4 + §6 阶段 2）

**前置:** 阶段 1 全部完成（`docs/superpowers/plans/2026-10-28-settle-kernel-phase1.md`），`scripts/train_pnl_proxy.compute_hypothetical_pnl` 已委托内核。

## Global Constraints

- 命令一律 `venv/bin/python` / `venv/bin/pytest`，在仓库根执行。
- `data/` 在 gitignore 里：**每一步改写前都做带日期备份**，git 救不回来。
- 共享工作树：`git add` / `git commit` 用显式路径；不要提交别的会话的脏文件。
- 不重跑 Optuna、不改 21 维特征集、不改超参——本阶段只换标签。
- 训练脚本对「旧模型」有只读保护（`chmod 0444`），改完要记得 `git status` 确认没有把只读位带进仓库。
- 本阶段结束时 `hypothetical_pnl` 的数值**必然**变；任何"数值应该不变"的断言都是错的。

## 现状（2026-10-28 实测，改前）

| 产物 | 规模 | 说明 |
|---|---|---|
| `data/double_up/d_signal_5d_features_vpnl.csv` | 13064 行 / 13061 非空 PnL | 旧口径标签 |
| `data/double_up/stage1_wide_table.parquet` | 13048 行 / 4977 只 | 宽表 |
| `data/double_up/stage1_selected_features.json` | 21 维 + 超参 | **冻结契约，不改** |
| `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt` | 21 维 LGBM | 当前 stage1 模型 |

> **数据可用性（派发前实测，决定本阶段样本边界）**：CSV 里 **3083 行（23.6%）** 的 code 在
> `data/parquet/daily` 里**没有文件**——bj 1539 行（281 只）+ ETF 1544 行（923 只，前缀全是
> `15/51/56/58/52/53`）。`full_sync` 自 2026-10-21 起只同步沪深股票（`185deac`），bj 缺口已在
> `docs/PLAN_E_LIVE.md` §4.2 定案「只报告，不动策略口径」；`min1` 只有 2 周–1 个月历史，补不回
> 2023–2026 的标签期。所以这些行**重算后必为 null**，宽表重装配时按既有 `n_missing_file` 逻辑
> 丢弃（13048 → 9981 行）。详见账本 R7。

## 与规格 §6 阶段 3 的 2 处更正（实施时以此为准）

| 规格原写 | 实测更正 |
|---|---|
| 标签写新文件 `d_signal_5d_features_vpnl_v2.csv`，旧文件不动（§6 阶段 3-1） | **同路径重写 + 带日期快照**。`train_pnl_proxy` 的 `OUT_DATA`、`stage1_wide_table.build_wide_table` 的 `src_csv`、`predict.py` 的读入都是硬编码路径；另起 `_v2` 链要在同一轮里改三处默认值并长期维护两条真值，而本轮目的只是「换标签看指标」。改为 `data/double_up/_pre_settle_kernel_20261028/` 快照 + `MANIFEST.md`（含 md5），回滚 `cp -p` 一条命令。 |
| 宽表/模型同样出 `_v2` 文件名，模型切换 = `predict.py` 改一行默认项（§6 阶段 3-2/3-3、§8） | 宽表与模型也保持原路径，`predict.py` **一行都不改**；v1/v2 对照由快照目录提供（Task 5 并行读新旧两份算指标）。代价：同一时刻只有一套真值，事后想复现旧模型必须从快照恢复——快照在 gitignore 里，删了就没了，所以 Task 1 的 md5 清单必须留。 |

---

### Task 1: 冻结 v1 基线

**Files:**
- Create: `data/double_up/_pre_settle_kernel_20261028/`（备份目录，gitignored）
- Create: `data/double_up/_pre_settle_kernel_20261028/MANIFEST.md`（md5 清单）

**Interfaces:**
- Produces: 一个可回滚的快照目录 + 一份 md5 清单，供 Task 5 对照与回滚。

- [ ] **Step 1: 备份**

```bash
cd /home/jiuben/tdx-data-feed
BK=data/double_up/_pre_settle_kernel_20261028
mkdir -p "$BK"
for f in d_signal_5d_features_vpnl.csv stage1_wide_table.parquet \
         stage1_selected_features.json d_signal_5d_model_vpnl_huber_stage1.txt \
         d_signal_5d_model_vpnl_huber.txt; do
  [ -f "data/double_up/$f" ] && cp -p "data/double_up/$f" "$BK/$f"
done
ls -la "$BK"
```

- [ ] **Step 2: 写清单**

```bash
cd /home/jiuben/tdx-data-feed
BK=data/double_up/_pre_settle_kernel_20261028
{
  echo "# v1 基线（结算内核改造前）"
  echo
  echo "冻结时间: $(date -Iseconds)"
  echo "阶段 1 提交: $(git log --oneline -1 -- tdxfeed/execution)"
  echo
  echo "## 校验"
  echo '```'
  (cd "$BK" && md5sum *)
  echo '```'
  echo
  echo "## 回滚"
  echo '```bash'
  echo "cp -p $BK/d_signal_5d_features_vpnl.csv data/double_up/"
  echo "cp -p $BK/stage1_wide_table.parquet data/double_up/"
  echo "cp -p $BK/d_signal_5d_model_vpnl_huber_stage1.txt data/double_up/"
  echo '```'
} > "$BK/MANIFEST.md"
cat "$BK/MANIFEST.md"
```

Expected: 五个文件（缺哪个就少哪行）与一段 md5 输出。

- [ ] **Step 3: 复核备份完整性**

```bash
cd /home/jiuben/tdx-data-feed
BK=data/double_up/_pre_settle_kernel_20261028
for f in "$BK"/*.csv "$BK"/*.parquet; do
  [ -f "$f" ] || continue
  a=$(md5sum "$f" | cut -d' ' -f1)
  b=$(md5sum "data/double_up/$(basename "$f")" | cut -d' ' -f1)
  echo "$(basename "$f") $([ "$a" = "$b" ] && echo OK || echo MISMATCH)"
done
```

Expected: 每个文件 `OK`。

- [ ] **Step 4: 提交清单（备份本体不入库）**

```bash
cd /home/jiuben/tdx-data-feed
git check-ignore -v data/double_up/_pre_settle_kernel_20261028/MANIFEST.md
```

Expected: 输出一条 `data/` 的忽略规则。若**没有**输出（说明没被忽略），停下来：不要 `git add` 这个目录，改成把 MANIFEST 拷到 `docs/research/2026-10-28-v1-baseline-manifest.md` 再提交。

本任务没有代码改动，不产生提交（除非上一步走了 `docs/research/` 分支）：

```bash
# 仅当 MANIFEST 落在 docs/research/ 时
git add docs/research/2026-10-28-v1-baseline-manifest.md
git commit -m "docs(research): 冻结结算内核改造前的 v1 标签/模型基线"
```

---

### Task 2: 用新口径重算 PnL 标签

**Files:**
- Create: `scripts/recompute_pnl_labels.py`（只换标签列的入口）
- Modify（重写）: `data/double_up/d_signal_5d_features_vpnl.csv`
- Test: `tests/test_pnl_label_recompute.py`（新）

**Interfaces:**
- Consumes: 阶段 1 的 `scripts/train_pnl_proxy.compute_hypothetical_pnl`（已委托内核）与其 `attach_pnl` 并行包装
- Produces: 同路径 CSV，`hypothetical_pnl` 列换成新口径；行数、列序、`(code, signal_date)` 集合不变。

> **坑（已实测，必须按下面的方式做，不要跑 `train_pnl_proxy.py` 本体）**：
> ① `main()` 在 `OUT_DATA` 存在且 PnL 非空率 >90% 时直接复用旧列；
> ② 它的特征源 `SOURCE = data/double_up/d_signal_5d_features_v9.csv` **在本机已不存在**（实测 `ls` 无此文件），所以 `main()` 现在必以 `❌ 训练数据不存在` 退出；
> ③ 现存 `d_signal_5d_features.csv`（18 维）与 `_greedy.csv`（20 维）都**缺** vpnl 的 26 维，不能当特征源。
> 因此标签重算改为「以现有 vpnl CSV 自身的 26 维特征为输入，只重算 `hypothetical_pnl` 列」——这正是本阶段唯一要变的东西，且不需要 `SOURCE`。

- [ ] **Step 1: 写失败测试（重算后必须成立的性质）**

`tests/test_pnl_label_recompute.py`：

```python
"""重算后的标签必须满足内核口径；这是对数据文件的守卫，不是对函数的。

只在 data/ 存在时跑：CI 无数据环境由根 conftest 跳过。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
NEW = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
OLD = ROOT / "data" / "double_up" / "_pre_settle_kernel_20261028" / "d_signal_5d_features_vpnl.csv"
DAILY = ROOT / "data" / "parquet" / "daily"

pytestmark = pytest.mark.skipif(
    not NEW.exists() or not OLD.exists() or not DAILY.exists(),
    reason="缺 data/double_up 标签文件或 Task 1 快照（快照在 gitignore 的 data/ 里）",
)


def _daily_codes() -> set[str]:
    return {p.stem for p in DAILY.glob("*.parquet")}


def test_same_keys_and_row_count():
    new = pd.read_csv(NEW, usecols=["code", "signal_date"])
    old = pd.read_csv(OLD, usecols=["code", "signal_date"])
    assert len(new) == len(old)
    assert list(new.columns) == list(old.columns)
    assert new["signal_date"].str.len().eq(10).all()


def test_labels_exist_exactly_where_daily_bars_exist():
    """缺日线的 code（bj 1539 行 + ETF 1544 行 = 3083 行）重算不出标签，必须为 null；
    有日线的行必须全部算出标签。"""
    df = pd.read_csv(NEW, usecols=["code", "hypothetical_pnl"])
    have = df["code"].isin(_daily_codes())
    assert (~have).sum() > 0, "daily 目录像被整体删了：先确认数据源，别当测试坏了"
    assert df.loc[have, "hypothetical_pnl"].notna().all(), "有日线的行出现了新的 None"
    assert df.loc[~have, "hypothetical_pnl"].isna().all(), "缺日线的行仍有旧口径标签"


def test_labels_actually_changed():
    """口径变了，有日线的行标签不可能逐位相同——相同说明根本没重算。"""
    new = pd.read_csv(NEW, usecols=["code", "signal_date", "hypothetical_pnl"])
    old = pd.read_csv(OLD, usecols=["code", "signal_date", "hypothetical_pnl"])
    m = new.merge(old, on=["code", "signal_date"], suffixes=("_new", "_old"))
    m = m[m["hypothetical_pnl_new"].notna() & m["hypothetical_pnl_old"].notna()]
    assert len(m) > 9000
    diff = (m["hypothetical_pnl_new"] - m["hypothetical_pnl_old"]).abs()
    assert (diff > 1e-9).mean() > 0.95, "标签与旧口径几乎一致，检查是否复用了旧列"


def test_non_label_columns_are_untouched():
    """只换标签列：其余 29 列必须与快照逐位相同。

    round_trip 读取是必须的——默认解析器会把 ~100 个浮点读偏 1 ULP，重写后
    特征就悄悄变了（这条守卫就是为抓那次漂移而加）。
    """
    new = pd.read_csv(NEW, float_precision="round_trip")
    old = pd.read_csv(OLD, float_precision="round_trip")
    others = [c for c in old.columns if c != "hypothetical_pnl"]
    pd.testing.assert_frame_equal(new[others], old[others], check_exact=True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_pnl_label_recompute.py -q`
Expected: `test_labels_actually_changed` FAIL（当前 CSV 仍是旧口径，diff 恒为 0）与
`test_labels_exist_exactly_where_daily_bars_exist` FAIL（缺日线的 3083 行还挂着旧标签），
共 2 failed / 2 passed（`test_non_label_columns_are_untouched` 此时应绿——若它红，说明 CSV 在
本任务开始前就被人改过，先查清楚）。

- [ ] **Step 3: 写只换标签的入口**

`scripts/recompute_pnl_labels.py`：

```python
"""用当前内核口径重算 d_signal_5d_features_vpnl.csv 的 hypothetical_pnl 列。

只换标签：特征列、(code, signal_date) 集合、列序都不动。
不使用 train_pnl_proxy.main()——它的特征源 d_signal_5d_features_v9.csv 已不存在，
且存在 90% 非空就复用旧列的短路，二者都会让「重算」变成空操作。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd

from train_pnl_proxy import OUT_DATA, attach_pnl


def main() -> int:
    # round_trip 解析：默认解析器会让 ~100 个浮点读偏 1 ULP，重写后就不是「只换一列」了
    df = pd.read_csv(OUT_DATA, float_precision="round_trip")
    if "hypothetical_pnl" not in df.columns:
        print(f"❌ 缺 hypothetical_pnl 列: {OUT_DATA}", file=sys.stderr)
        return 1
    # 写前闸门：attach_pnl 逐行吞异常返 None，没有这道检查，一次空跑就会 exit 0
    # 覆盖掉唯一产物（data/ 不在 git 里）。期望值取自上一版在有日线行上的非空数。
    have = {p.stem for p in (ROOT / "data" / "parquet" / "daily").glob("*.parquet")}
    expected = int(df.loc[df["code"].isin(have), "hypothetical_pnl"].notna().sum())
    features = df.drop(columns=["hypothetical_pnl"])
    renamed = attach_pnl(features, max_workers=8)
    valid = int(renamed["hypothetical_pnl"].notna().sum())
    if valid < expected:
        print(f"❌ 重算后非空 {valid} < 上一版有日线行的 {expected}，拒写 {OUT_DATA}",
              file=sys.stderr)
        return 1
    tmp = OUT_DATA.with_name(OUT_DATA.name + ".tmp")
    try:
        renamed.to_csv(tmp, index=False)
        tmp.replace(OUT_DATA)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    print(f"RECOMPUTE_DONE rows={len(renamed)} valid={valid} out={OUT_DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑重算**

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python scripts/recompute_pnl_labels.py 2>&1 | tee /tmp/pnl_recompute.log | tail -5
```

Expected: `RECOMPUTE_DONE rows=13064 valid=9981`，退出码 0，耗时数分钟（8 进程逐只读 parquet）。
关键核对：`valid` 必须正好等于「CSV 里 code 在 `data/parquet/daily` 有文件的行数」（改前实测 9981）。
比它少 → 新内核在某个有日线的行上返回了 None，停下把 code/signal_date 列进报告，不要直接放行；
比它多 → daily 目录在重算中途变了，同样停下核对。

- [ ] **Step 5: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_pnl_label_recompute.py -q`
Expected: 4 passed（若 `test_labels_exist_exactly_where_daily_bars_exist` 失败，先在报告里给出「有日线却算不出标签」的 code/signal_date 清单，不要直接改断言——见 Step 7 之前的裁决）。

- [ ] **Step 6: 记录标签分布变化**

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python - <<'PY'
import pandas as pd
new = pd.read_csv("data/double_up/d_signal_5d_features_vpnl.csv", usecols=["hypothetical_pnl"])
old = pd.read_csv("data/double_up/_pre_settle_kernel_20261028/d_signal_5d_features_vpnl.csv",
                  usecols=["hypothetical_pnl"])
for tag, s in (("old", old["hypothetical_pnl"]), ("new", new["hypothetical_pnl"])):
    print(f"{tag}: n={s.notna().sum()} mean={s.mean():.4f} median={s.median():.4f} "
          f"pos={ (s>0).mean()*100:.1f}% min={s.min():.4f} max={s.max():.4f}")
PY
```

Expected: `new: n=9981`（`old: n=13061`）；均值/中位数/正样本占比**方向不预设**。抽样 400 行实测
均值 0.1108 → 0.1171、`new<old` 占 60%——跳空穿越 +30% 时按开盘成交会高于阈值价，所以「更保守」
只对双触发与止损跳空成立，整体均值可能上移。把两行数字原样抄进 Step 7 的提交信息，不要写方向结论。

- [ ] **Step 7: 提交（脚本 + 测试；CSV 在 data/ 不入库）**

```bash
cd /home/jiuben/tdx-data-feed
git add scripts/recompute_pnl_labels.py tests/test_pnl_label_recompute.py
git commit -m "feat(labels): 按结算内核口径重算 PnL 标签（只换标签列）

旧→新: mean <旧> → <新>, 正样本 <旧>% → <新>%"
```

（提交信息里的尖括号数字用 Step 6 的真实输出替换后再提交。）

---

### Task 3: 重装配 stage1 宽表

**Files:**
- Modify（重写）: `data/double_up/stage1_wide_table.parquet`
- Test: `tests/test_wide_table_label_source.py`（新）

**Interfaces:**
- Consumes: Task 2 的 `d_signal_5d_features_vpnl.csv`
- Produces: 同路径 parquet，列集合不变，`hypothetical_pnl` 与 CSV 同源。

> **坑**：`tdxfeed/stage1_wide_table.py` 的 `__main__` 里 `--sample` 默认 **200**，不加 `--sample 0` 就是采样跑。

- [ ] **Step 1: 写失败测试**

`tests/test_wide_table_label_source.py`：

```python
"""宽表的标签必须来自当前 CSV，不能是上一轮遗留的旧列。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
WIDE = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"
CSV = ROOT / "data" / "double_up" / "d_signal_5d_features_vpnl.csv"
DAILY = ROOT / "data" / "parquet" / "daily"

pytestmark = pytest.mark.skipif(not WIDE.exists(), reason="缺 stage1 宽表")


def _frames():
    wide = pd.read_parquet(WIDE, columns=["code", "signal_date", "hypothetical_pnl"])
    csv = pd.read_csv(CSV, usecols=["code", "signal_date", "hypothetical_pnl"])
    for df in (wide, csv):
        df["code"] = df["code"].astype(str).str.zfill(6)
    return wide, csv


def test_labels_match_current_csv():
    # CSV 有 6 个键各 32 行完全重复（重算前既有），先去重键再比，避免 32×32 叉积
    wide, csv = _frames()
    wide = wide.drop_duplicates(subset=["code", "signal_date"])
    csv = csv.drop_duplicates(subset=["code", "signal_date"])
    m = wide.merge(csv, on=["code", "signal_date"], suffixes=("_w", "_c"))
    assert len(m) == len(wide), "宽表里出现了 CSV 没有的键"
    assert (m["hypothetical_pnl_w"] - m["hypothetical_pnl_c"]).abs().max() < 1e-9


def test_wide_table_covers_the_whole_available_universe():
    """宽表 = CSV 里 code 有日线文件的那部分（缺日线的 3083 行按 n_missing_file 丢弃），
    且不是 --sample 采样产物。预期集合从数据现算，不写死规模。"""
    wide, csv = _frames()
    have = {p.stem for p in DAILY.glob("*.parquet")}
    expected = csv[csv["code"].isin(have)]
    got = set(zip(wide["code"], wide["signal_date"]))
    want = set(zip(expected["code"], expected["signal_date"]))
    assert got.issubset(want), f"宽表多出 {len(got - want)} 个键"
    assert len(got) >= len(want) - 100, f"宽表缺 {len(want - got)} 个键，像被采样了"
    assert wide["code"].nunique() > 3000, "宽表 code 数异常，先查是不是 --sample 没设 0"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_wide_table_label_source.py -q`
Expected: `test_labels_match_current_csv` FAIL（宽表还是旧标签，与 Task 2 新 CSV 对不上）。

- [ ] **Step 3: 重装配**

```bash
cd /home/jiuben/tdx-data-feed
cp -p data/double_up/stage1_wide_table.parquet /tmp/wide_old.parquet
venv/bin/python -m tdxfeed.stage1_wide_table --sample 0 2>&1 | tail -20
```

Expected: 日志出现 `宇宙 code 数: 4977（采样 0）`、`parity_max` 一行、`缺失文件=3083`，产物
`rows≈9981`（改前宽表 13048；差的 3067 行就是缺日线的 bj/ETF）。若 `缺失文件` 不是 3083 或 rows 差异
超过 100，停下核对 daily 目录——不要为了过测试改断言。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_wide_table_label_source.py tests/test_stage1_factors_parity.py -q`
Expected: 全绿（parity 测试验证 26 维特征与 `features_5d` 逐位一致 ≤1e-9；若红，说明 Task 7 的 `get_limit` 委托改动了特征列 `limit`，那正是预期的口径变化——此时改 parity 测试的**容差或期望值**并在提交信息里说明，不要回退委托）。

- [ ] **Step 5: 提交**

```bash
cd /home/jiuben/tdx-data-feed
git add tests/test_wide_table_label_source.py
git commit -m "test(stage1): 宽表标签与当前 CSV 同源 + 全量非采样守卫"
```

---

### Task 4: 用冻结的 21 维与超参重训

**Files:**
- Modify: `data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt`（新模型权重）
- Create: `data/plan_e/stage1_retrain_after_settle_kernel.md`（对照报告，gitignored）
- Test: `tests/test_model_label_version.py`（新）

**Interfaces:**
- Consumes: Task 3 的宽表 + `data/double_up/stage1_selected_features.json`
- Produces: 同路径模型文件，`num_feature == 21`，特征名与冻结集完全一致。

- [ ] **Step 1: 写失败测试（模型与宽表必须是同一版标签）**

`tests/test_model_label_version.py`：

```python
"""21 维模型必须是用当前宽表训出来的：特征名一致，且预测与冻结超参可复现。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "data" / "double_up" / "d_signal_5d_model_vpnl_huber_stage1.txt"
FROZEN = ROOT / "data" / "double_up" / "stage1_selected_features.json"

pytestmark = pytest.mark.skipif(not MODEL.exists() or not FROZEN.exists(),
                                reason="缺 stage1 模型或冻结契约")


def test_model_feature_names_match_frozen_contract():
    import lightgbm as lgb
    booster = lgb.Booster(model_file=str(MODEL))
    assert booster.num_feature() == 21
    assert booster.feature_name() == json.loads(FROZEN.read_text())["selected_features"]


def test_model_mtime_is_after_wide_table_label_rewrite():
    wide = ROOT / "data" / "double_up" / "stage1_wide_table.parquet"
    assert MODEL.stat().st_mtime >= wide.stat().st_mtime, "模型比宽表旧：标签换了但模型没重训"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv/bin/pytest tests/test_model_label_version.py -q`
Expected: `test_model_mtime_is_after_wide_table_label_rewrite` FAIL（模型是旧的）。

- [ ] **Step 3: 重训**

```bash
cd /home/jiuben/tdx-data-feed
cp -p data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt /tmp/stage1_model_old.txt
venv/bin/python scripts/stage1_retrain.py \
  --config data/double_up/stage1_selected_features.json \
  --frozen /tmp/stage1_contract_after.json \
  2>&1 | tee /tmp/stage1_retrain.log | tail -40
```

> **两个必须先知道的实测事实**：
> ① 脚本默认 `--config logs/stage1_optuna_best.json`，**本机该文件不存在**（实测 `ls logs/` 无命中），不加 `--config` 会以 `FileNotFoundError: Step 4 配置缺失` 失败。冻结契约 `stage1_selected_features.json` 恰好含 `selected_features` + `hyperparams` 两个键，正是脚本需要的，所以直接拿它当 config——这同时保证「21 维与超参不来自别处」。
> ② 脚本的 `write_frozen_contract` 会**无条件重写** `--frozen` 指向的文件（只有 `created` 时间戳会变）。冻结契约本阶段不可变，所以把 `--frozen` 指到 `/tmp`，仓库内那份保持不动。
> ③ 训练样本本轮是 **9981**（不是 13048）：缺日线的 bj/ETF 行已在 Task 3 被宽表丢弃。`is_bj` 仍在
> 21 维契约里，但训练集里恒为 0（LightGBM 会当常量特征忽略），特征名与维数都不变——这是预期行为，
> 不要为了「让 is_bj 有区分度」把它从契约里删掉（删了就改冻结契约）。

Expected: 日志给出 walk-forward 各折 Spearman / Top-20 / Top-50，以及「纯 OOS」段。脚本会：备份旧部署模型、`chmod 0444` 保护、写 `*_stage1.txt`、回读校验 `num_feature == 21`。若出现 `RETRAIN_FAIL`，原因通常是宽表路径或特征名不符。

- [ ] **Step 3b: 确认冻结契约未被改写**

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python - <<'PY'
import json, pathlib
cur = json.loads(pathlib.Path("data/double_up/stage1_selected_features.json").read_text())
snap = json.loads(pathlib.Path("data/double_up/_pre_settle_kernel_20261028/stage1_selected_features.json").read_text())
assert cur["selected_features"] == snap["selected_features"], "冻结特征集被改动"
assert cur["hyperparams"] == snap["hyperparams"], "冻结超参被改动"
print("FROZEN_CONTRACT_UNCHANGED", len(cur["selected_features"]), "维")
PY
```

Expected: `FROZEN_CONTRACT_UNCHANGED 21 维`。

- [ ] **Step 4: 跑测试确认通过**

Run: `venv/bin/pytest tests/test_model_label_version.py -q`
Expected: 2 passed

- [ ] **Step 5: 出对照报告**

```bash
cd /home/jiuben/tdx-data-feed
mkdir -p data/plan_e
venv/bin/python - <<'PY'
import json, re
from datetime import datetime
from pathlib import Path

log = Path("/tmp/stage1_retrain.log").read_text(encoding="utf-8", errors="ignore")
out = Path("data/plan_e/stage1_retrain_after_settle_kernel.md")
frozen = json.loads(Path("data/double_up/stage1_selected_features.json").read_text())
out.write_text(
    "# stage1 重训（结算内核改造后）\n\n"
    f"> 生成: {datetime.now().isoformat(timespec='seconds')}\n"
    f"> 冻结特征数: {len(frozen['selected_features'])}\n\n"
    "## 1. 为什么重训\n\n"
    "- 阶段 1 把 PnL 标签的结算口径改成：入场次根才判定、双触发结算亏损侧、跳空按开盘。\n"
    "- 旧模型学的是旧口径标签，与新标签不同分布，必须重训。\n\n"
    "## 2. 特征与超参\n\n"
    "- 特征集与超参取自 `stage1_selected_features.json`，**本轮未重跑 Optuna**。\n\n"
    "## 3. 训练日志（尾部 120 行）\n\n```\n" + "\n".join(log.splitlines()[-120:]) + "\n```\n"
)
print(out)
PY
head -20 data/plan_e/stage1_retrain_after_settle_kernel.md
```

- [ ] **Step 6: 提交**

```bash
cd /home/jiuben/tdx-data-feed
git add tests/test_model_label_version.py
git commit -m "test(stage1): 模型特征集与冻结契约一致 + 模型不早于标签重写"
```

---

### Task 5: 对照、决策与文档

**Files:**
- Create: `docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md`
- Modify: `AGENTS.md`（「显著性推断四条约定」段落后补一行标签口径说明）

**Interfaces:**
- Produces: 一份可复核的结论（保留 / 回滚），以及下游读者需要的口径说明。

- [ ] **Step 1: 读重训日志，抄关键数字**

```bash
cd /home/jiuben/tdx-data-feed
grep -nE "Spearman|Top-20|Top-50|OOS|holdout|best_iteration" /tmp/stage1_retrain.log | tail -30
```

Expected: 各折与池化指标。把这些数字与 `data/double_up/stage1_selected_features.json` 里记录的基线（`scripts/stage1_gate.py` 的硬闸门：池化 Spearman ≥ 0.315 / Top-20 ≥ 0.21 / Top-50 ≥ 0.20）对照。

- [ ] **Step 1b: 匹配宇宙对照（主判据，先做这步再写结论）**

闸门是在**旧宇宙**（13048 行、含 bj/ETF）上标定的，本轮样本只有 9981 行；拿新宇宙的绝对指标直接
比闸门是跨口径比较。所以先把快照旧模型与重训新模型放在**同一份新宽表、同一 holdout 窗口**上各评一次：

```bash
cd /home/jiuben/tdx-data-feed
venv/bin/python - <<'PY'
import json, sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, "scripts")
from stage1_baseline import _eval_fold

feats = json.loads(Path("data/double_up/stage1_selected_features.json").read_text())["selected_features"]
df = pd.read_parquet("data/double_up/stage1_wide_table.parquet")
df = df.dropna(subset=["hypothetical_pnl"] + feats)
dates = pd.to_datetime(df["signal_date"]).values
va = dates >= np.datetime64("2025-01-01")
y = df["hypothetical_pnl"].astype(float).values
X = df[feats]
print(f"holdout n={int(va.sum())} / total={len(df)}")
for tag, path in (
    ("old", "data/double_up/_pre_settle_kernel_20261028/d_signal_5d_model_vpnl_huber_stage1.txt"),
    ("new", "data/double_up/d_signal_5d_model_vpnl_huber_stage1.txt"),
):
    m = lgb.Booster(model_file=path)
    assert m.feature_name() == feats, f"{tag} 模型特征名与冻结契约不一致"
    r = _eval_fold(y[va], m.predict(X[va]), [10, 20, 50])
    t = r["top"]
    print(f"{tag}: spearman={r['spearman']:.4f} "
          f"top20_mean={t[20]['mean_pnl']:.4f} top20_win={t[20]['win_rate']:.3f} "
          f"top50_mean={t[50]['mean_pnl']:.4f} top50_win={t[50]['win_rate']:.3f}")
PY
```

Expected: 两行（`old:` / `new:`），holdout n≈2695。把两行原样抄进结论文档第 3 节。
若 `new` 的 Spearman 与 Top-20 均值同时低于 `old`，先不要下回滚结论——先看第 1 节的口径差异
是否解释了它（例如旧标签的 TP 优先会系统性抬高正样本 PnL），把原因写清楚再决定。

- [ ] **Step 2: 写结论文档**

`docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md`：

```markdown
# stage1 重训结论（结算内核改造后）

> 一次性研究快照，记录 2026-10-28 那一轮的口径变化与数字；不是现状规格。

## 1. 改了什么

| 项 | 旧口径 | 新口径 |
|---|---|---|
| 入场后第一根判定 | 入场根自身也算 | 入场次根才判定 |
| 同根双触发 | 止盈优先 | 止损优先 |
| 跳空穿越 | 按阈值价成交 | 按当日开盘成交 |
| 成本 | `gross − 2×(fee+slip)` 加法 | 内核 `CostModel.round_trip` 乘法 |

## 2. 样本边界（本轮先变的不是标签，是样本）

- 标签 CSV 仍 13064 行，但其中 **3083 行（bj 1539 + ETF 1544）在 `data/parquet/daily` 没有日线文件**，
  重算后为 null；宽表重装配按既有 `n_missing_file` 逻辑丢弃，**训练样本 13048 → 9981 行**。
- 依据：`full_sync` 自 2026-10-21 只同步沪深股票（`185deac`）；bj 缺口已于 2026-10-24 定案
  「只报告，不动策略口径」（`docs/PLAN_E_LIVE.md §4.2`）；`predict.py` 推理宇宙本就排除 bj/ETF。
- 这不是新缺陷，是训练宇宙对齐推理宇宙。若将来要覆盖 bj/ETF，先补历史数据再重跑本阶段。

## 3. 标签分布

（抄 Task 2 Step 6 的两行输出，不写升降结论）

## 4. 重训结果

（抄 Task 4 的池化指标与最后一折 OOS，以及 Step 1b 的 `old:` / `new:` 两行匹配对照）

## 5. 决策

- **主判据**：Step 1b 的匹配对照——同一批 9981 行、同一 holdout 窗口下，新模型的 Spearman /
  Top-20 均值不低于旧模型。这是唯一同口径的比较。
- **参考**：`scripts/stage1_gate.py` 的闸门 0.315 / 0.21 / 0.20 是在旧宇宙（13048 行）上标定的，
  与新样本的绝对指标不可直接横比；只有「跌破闸门 **且** 同时低于旧模型」才构成回滚理由。
- [ ] 保留新模型（匹配对照不劣于旧模型）
- [ ] 回滚（匹配对照 Spearman 与 Top-20 同时明显更低）：逐文件 `cp -p` 快照（**只读模型要先替换**）：
  ```bash
  BK=data/double_up/_pre_settle_kernel_20261028
  for f in d_signal_5d_features_vpnl.csv stage1_wide_table.parquet stage1_selected_features.json d_signal_5d_model_vpnl_huber_stage1.txt; do
    cp -p "$BK/$f" data/double_up/
  done
  # d_signal_5d_model_vpnl_huber.txt 是 444 只读，cp 到同名目标会 Permission denied
  cp --remove-destination -p "$BK/d_signal_5d_model_vpnl_huber.txt" data/double_up/
  ```
  回滚后逐文件 `md5sum -c` 对照 `$BK/MANIFEST.md`。

## 6. 下游影响

- `predict.py` 默认模型不变路径；预测分布会变（标签口径变了）。
- 历史报告（`data/plan_e/*.md`）是旧口径产物，引用时须注明。
```

填完勾选项后再提交。

- [ ] **Step 3: 补 AGENTS.md 一行**

在「显著性推断四条约定」段之后追加：

```markdown
## 训练标签口径

- `hypothetical_pnl` 由 `scripts/train_pnl_proxy.compute_hypothetical_pnl` 产出，已委托 `tdxfeed/execution/` 的内核 A。2026-10-28 起标签口径为：入场次根起判定、同根双触发结算亏损侧、跳空按开盘。**此前训练的模型与报告都是旧口径**，横比前先看标签版本（`data/double_up/_pre_settle_kernel_20261028/` 是旧版快照）。
```

- [ ] **Step 4: 全量回归**

```bash
cd /home/jiuben/tdx-data-feed
make test
```

Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
cd /home/jiuben/tdx-data-feed
git add docs/research/2026-10-28-stage1-retrain-after-settle-kernel.md AGENTS.md
git commit -m "docs(research): stage1 重训结论与标签口径说明（结算内核改造后）"
```

---

## 阶段 2 完成判据

1. `data/double_up/_pre_settle_kernel_20261028/` 有五个旧产物 + md5 清单，可一键回滚。
2. `tests/test_pnl_label_recompute.py`、`tests/test_wide_table_label_source.py`、`tests/test_model_label_version.py` 全绿。
3. `stage1_wide_table.parquet` 全量（≥4000 只），标签与当前 CSV 逐位一致。
4. 新模型 `num_feature == 21` 且特征名与冻结契约完全相同，mtime 晚于宽表。
5. 结论文档写清「保留 / 回滚」并列出三个闸门指标的实际值。
6. `make test` 全绿。
