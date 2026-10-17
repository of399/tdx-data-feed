# 代码审计改造实施方案 (2026-10-17)

> 回答"二/三/四/五/六 各章节需要怎么改"。基于审计报告 `CODE_AUDIT_2026-10-17.md`。
> P0 已落地（commit `777ca8c`）；本文给**剩余项的具体改法**（代码级）。
> 原则：每项可独立执行 + 独立验证；按用户偏好"完成一件再做下一件"，建议按顺序逐项做。

## 二、代码结构与依赖关系

### ✅ 已完成（P0-3）
死代码归档：transform/、tools/、根目录 3 游离脚本、`_v111_analysis.py` → `_archive/`。

### 改 2-1：补归档 `mcp/`（P0-3 遗漏项，10 分钟）

mcp/servers/（calc/parquet_reader/vault 3 个服务）与 `v5/mcp_servers/` 功能重叠双轨，Makefile 零 target 引用。

```bash
# 先验证（应零输出，若有力引用则跳过本项）:
grep -rn "mcp/servers\|mcp_servers.vault\|mcp_servers.parquet" Makefile systemd/ scripts/ v5/ --include="*.py" -l
git mv mcp _archive/mcp
```
风险：低——若 `.codebuddy` MCP 配置引用了它需同步改（先查 `~/.codebuddy` 配置）。

### 改 2-2：scripts 私有函数导入公共化（30 分钟）

`scripts/plan_e_sim_live.py:216` 导入 predict 的私有函数：

```python
# 现状（坏味道: 跨文件依赖私有符号）
from predict import _enrich_industry_features
```
改法：把 `_enrich_industry_features` 提升到 `scripts/_common.py`（去掉前导下划线命名 `enrich_industry_features`），predict.py 与 plan_e_sim_live.py 都从 `_common` 导入。

### 改 2-3：v5/ 边界声明（10 分钟，最保守方案）

**不建议**现在包化（v5 有 19 个 systemd 单元依赖运行，动结构风险高）。改法：在根 `README.md` 加一节"v5 子项目边界"：
- v5 是独立运行时（零 import 主仓、自己的 systemd/monitoring）
- 主仓与 v5 交互仅通过：文件系统（data/）+ HTTP（port 8002/8010/9101）
- **禁止**从 v5 import 主仓代码，反之亦然

包化/sys.path 清理留 P2（v5 独立 repo 决策时一并做）。

### 改 2-4：scripts 平铺互引收敛（P2，可不做）

4 个文件 `import plan_e_backtest` 的模式暂不动（能跑、有测试兜底）；P2 做 plan_e 整合时自然消除。

---

## 三、代码质量

### 改 3-1：统一 `_norm_code`/`is_st`/`is_bj`（P1 最高价值，2-3 小时分批）

**第一步**：`scripts/_common.py` 追加三个函数（单一真相源）：

```python
def norm_code(code) -> str:
    """股票代码规范化: 6 位零填充, 去掉 sh/sz/bj 前缀。"""
    s = str(code).strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[2:]
    return s.zfill(6)

def is_st(name: str) -> bool:
    """ST 判定（含 *ST/ST/退市整理）。"""
    n = str(name)
    return bool(n) and ("ST" in n.upper() or n.startswith("退"))

def is_bj(code) -> bool:
    """北交所: 920 开头或 8 开头老三板代码。"""
    c = norm_code(code)
    return c.startswith("920") or c.startswith("8")
```

**第二步**：分批切换（每批 2-3 文件，切换后跑对应脚本 `--help` 或小样本冒烟）：
- 批 1：`predict.py` + `find_double_up_c_v2.py`（is_st）
- 批 2：`monitor_plan_e_live.py` + `compare_plan_e_signals.py`（is_bj + norm_code）
- 批 3：`oracle_plan_e_ceiling.py` + `plan_e_confidence.py` + `llm_signal_plan_e.py`

**验证**：每批切换后 `grep -rn "def _norm_code\|def is_st\|startswith(\"920\")" scripts/` 应递减；行为对比用一批股票代码（含 920/ST/带前缀）在旧新函数上断言相等。

### 改 3-2：Markdown 报告 builder（P1，1-2 小时）

新建 `scripts/report_writer.py`（约 100 行）：

```python
def md_header(title: str, meta: dict) -> str
def md_table(headers: list[str], rows: list[list]) -> str
def md_metrics_table(kvs: dict) -> str
def write_report(path, sections: list[str])
```
先只在**新写的报告**里用（不回头改 10 个旧文件）；旧的在 P2 plan_e 整合时自然迁移。**收益**：止血——防止第 11、12 个同构报告出现。

### 改 3-3：巨型函数拆分（P1，只拆 2 个热点）

**拆前必做**：先给目标函数补"golden 快照测试"（跑一次存输出 JSON，重构后 assert 一致）。

1. `quant/multifactor/engine.py:254 run_mf_backtest()`（297 行）拆为：
   `load_returns() → estimate_cov() → optimize_weights() → simulate() → summarize_mf()` 五段
2. `scripts/predict.py:531 main()`（254 行）拆为：
   `parse_args() → load_universe() → run_parallel_predict() → emit_outputs()` 四段

其余 4 个巨型函数（journal/monitor/summarize/backtest_one）**不拆**——在 P2 plan_e 整合时顺带处理，避免纯重构风险。

### 改 3-4：docstring 补核心入口（30 分钟）

只补 `quant/multifactor/engine.py`（当前 20%）：给 `run_mf_backtest`/`optimize` 相关 5 个入口函数加一行 docstring（输入/输出/副作用）。不追求全仓覆盖率。

---

## 四、性能瓶颈

### 改 4-1：predict.py 读取裁剪（P1 最高性能收益，预期 12 min → 3-5 min）

**实测瓶颈**（`scripts/predict.py:316,330`）：
- 每股票 2 次**全量** read_parquet（~1000 行 × 2），但特征只需**尾部 ~45 行**（20 日窗口 + 25 日扩展）
- 6882 只 × 2 次 ≈ 13,764 次文件 IO，读取量是实际需要的 ~20 倍
- **新发现的隐藏问题**：`_PARQUET_CACHE`（predict.py:313-317）在 ProcessPoolExecutor 下每个 worker 缓存**全量 DataFrame 常驻**——8 workers × ~860 只 × 1000 行 = 内存浪费且缓存命中率低

**改法（分两步，每步独立验证）**：

Step A —— 列裁剪（零风险，先做）：
```python
# predict.py:316 —— 全量列 → 只读特征所需列
NEEDED_COLS = ["date", "open", "close", "high", "low", "volume", "amount"]  # 以 compute_window_features_extended 实际用列为准
raw = pd.read_parquet(fp, columns=NEEDED_COLS)
# adj 同理（现已是隐式 2 列 merge，显式化）:
adj_df = pd.read_parquet(adj_fp, columns=["date", "adj_factor"])
```

Step B —— 尾部行裁剪 + 缓存改窗口缓存：
```python
# 用 pyarrow 只读尾部 row groups（parquet 按 row group 存储，尾部读开销小）:
import pyarrow.parquet as pq
pf = pq.ParquetFile(fp)
n_rows = pf.metadata.num_rows
skip = max(0, n_rows - 60)   # 多读 15 行余量
tbl = pf.read_row_group(pf.metadata.num_row_groups - 1)  # 简版: 读最后 1-2 个 row group
raw = tbl.to_pandas()
# 或保守版: pd.read_parquet 后直接 raw = raw.tail(60) 仍省 pandas 解析全量列的时间
# _PARQUET_CACHE 缓存 tail(60) 的窗口而非全量:
_PARQUET_CACHE[code_clean] = raw.tail(60)
```

**验证（三道防线）**：
1. `venv/bin/python -m pytest tests/test_stage1_factors_parity.py -x`（特征位级一致）
2. 改前后各跑一次 `predict.py --codes sh600000,sz000001` 对比 prob 值（应完全一致）
3. 改前后各跑一次 `--top 20`（全市场）对比 Top 20 列表 + 耗时

**风险**：中——若 `compute_window_features_extended` 隐式依赖超过 60 行的历史（如 20 日 rolling 需要前置 NaN 处理），窗口过短会改变特征。**对策**：先跑 parity 测试，失败则把窗口从 60 → 120 逐步放宽直到位级一致。

### 改 4-2：不做的事

- 不引入 polars 重写读取（pandas+pyarrow 裁剪已够；重写=大动干戈）
- 不做全市场合并 wide parquet（改动面大，收益与 4-1 重叠）

---

## 五、错误处理

### ✅ 已完成（P0-2）
33/37 处吞错留痕（`except X: pass` → `except X as e: print("[WARN] ...")`），控制流不变。

### 改 5-1：freshness 代码层防御（P1，30 分钟，根治仙股问题）

**背景**：周末 Top 20 出现数据陈旧 36/47 天的仙股（688496/002731），靠人工剔除——代码层没有防线。

**改法**：`predict.py` 的 `load_single_stock()`（约 L296-345）加数据新鲜度检查：

```python
# 在 len(df_raw) < 30 检查之后追加:
if len(df_raw) >= 30:
    last_date = pd.to_datetime(df_raw["date"]).max()
    # 基准日默认今天; 陈旧超过 5 个交易日视为停牌/退市/数据断裂
    max_stale_days = 5
    if (pd.Timestamp.now().normalize() - last_date).days > max_stale_days * 1.6:
        # 周末/节假日补偿 1.6x; 更精确可用交易日历
        print(f"[WARN] {code} 数据陈旧: 最新 {last_date.date()}, 剔除")
        return None
```
**验证**：`--codes 688496` 应输出剔除警告（该股数据停在 09-10）；`--codes sh600000` 正常。
**收益**：Top 20 永不再出仙股；**风险**：低（剔除逻辑与 `--no-freshness-check` 的既有旁路语义一致）。

### 改 5-2：明确不改的

- v5/tests 4 处 `except: pass`——测试故意走异常路径，**语义正确**
- 139 处 `except Exception` 中非 pass 的宽泛捕获——多为顶层兜底（CLI 脚本合理），不逐个收窄（过度工程）

---

## 六、依赖管理

### ✅ 已完成（P0-1）
删 6 个 0-import 包 + 关键包上限 pin + `requirements.lock`（257 包）+ Makefile update 漂移源移除 + `make sync-lock`。
二次验证修正：akquant（`factors_candidates.py:23` 用 `akquant.talib`）与 gguf（Makefile:132 + deploy.sh CLI）**有引用保留**；本轮再验证 baostock（3 处）/akshare（18 处）在用，requirements 保持。

### 改 6-1：声明 Python 版本约束（10 分钟）

requirements.txt 头部加：
```
# Python: 3.14（py3.14 无 cp314 wheel 的包需源码编译, 参考 flash-attn 回退 SDPA 教训）
# 安装: python3.14 -m venv venv && venv/bin/pip install -r requirements.txt
```

### 改 6-2：install 后自检（10 分钟）

Makefile `install` target 末尾追加：
```makefile
	$(PIP) check || (echo "$(RED)✗ 依赖冲突, 请对照 requirements.lock$(NC)" && exit 1)
```
**收益**：未来任何依赖安装后自动发现冲突（torchvision ABI 错配类问题当场暴露）。

---

## 执行顺序建议（对齐"完成一件再做下一件"）

| 序号 | 项 | 耗时 | 依赖 |
|---|---|---|---|
| 1 | 改 2-1 归档 mcp/ + 改 6-1 + 改 6-2 | 30 min | 无（收尾性质）|
| 2 | **改 5-1 freshness 防御** | 30 min | 无（独立、防线上价值最高）|
| 3 | **改 4-1 predict 读取裁剪** | 1-1.5 h | parity 测试先跑通 |
| 4 | 改 3-1 统一 norm_code/is_st/is_bj | 2-3 h（分 3 批）| 无 |
| 5 | 改 3-3 巨型函数拆分 ×2 | 2 h | 先补 golden 快照 |
| 6 | 改 3-2 report_writer（新报告止血）| 1 h | 无 |
| 7 | 改 3-4 engine docstring | 30 min | 无 |
| — | 改 2-3 v5 边界声明 | 10 min | 无（随时）|

改 2-4（scripts 平铺互引）与 v5 包化**不做**（留 P2）。

**全部做完的净效果**：Top 20 无仙股（5-1）、predict 提速 2-3x（4-1）、单一真相源消灭 11 处重复（3-1）、依赖冲突当场暴露（6-2）、死代码全部出仓（2-1）。
