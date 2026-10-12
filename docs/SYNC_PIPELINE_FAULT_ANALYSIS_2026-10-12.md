# 本地 Obsidian 同步与数据管线 故障分析与修复方案 (2026-10-12)

> 本文档基于 2026-10-12 全链路实跑验证结果整理，按「问题 → 根因 → 修复 → 验证」组织。
> 涉及文件：`systemd/obsidian-sync.path`、`systemd/obsidian-sync.service`、`scripts/sync-to-obsidian.sh`、`tdxfeed/freshness.py`、`scripts/check_schema.py`、`scripts/plan_e_v3_live_track.py`、`Makefile`。

## 0. 总览

| 问题 | 严重度 | 当前状态 | 影响范围 |
|---|---|---|---|
| ① obsidian-sync.service `failed` | 中（阻塞文档发布） | `start-limit-hit` | 文档→Obsidian 单向同步失效 |
| ② 新鲜度闸门误杀 | 中（确认缺陷） | 默认拦截 3 条管线 | predict / plan_e_backtest / plan_e_journal |
| ③ 日K `date` 三态漂移 | 低-中 | 被合规率掩盖 | 7230 文件中 5231 个 `int64` |
| L1 `plan_e_v3_live_track.py` F821 | 低 | 运行时必 `NameError` | 实验性 v3 脚本（非核心链路） |
| L2 `fastparquet` 缺失 | 低 | 可选依赖 | 无影响（pyarrow 已覆盖） |
| L3 `Makefile train-*` 引用 `/tmp` | 低 | 训练不可复现 | 模型重训 |

**核心指标**：部署 Ollama 模型 9 个；日K 文件 7230；systemd timer 5 个（均 active）；canary 健康检查 6/6 通过（err=0）。
**故障计数**：failed 服务 1 个；默认被误杀的管线 3 条；schema 类型异常文件 **5231 / 7230（72.4%）**；`scripts/` lint 告警 466 处（其中 F821 未定义名称 8 处）。

---

## 问题①（阻塞）：`obsidian-sync.service` 处于 `failed` 状态

### 问题现象
- `systemctl --user list-units` 显示：`● obsidian-sync.service  loaded  failed  failed`
- `journalctl --user -u obsidian-sync.service` 末段：
  ```
  obsidian-sync.service: Start request repeated too quickly.
  obsidian-sync.service: Failed with result 'start-limit-hit'.
  Failed to start obsidian-sync.service - tdx-data-feed: 同步文档到 Obsidian Vault.
  ```
- 但脚本本身执行成功：`logs/obsidian-sync.log` 末尾显示「同步完成！文档数 60」，`logs/obsidian-sync.err` 为空。

### 根因分析（systemd path 单元"重启风暴"）
`.path` 单元当前监听 7 个路径（`systemd/obsidian-sync.path`）：
```
PathModified=/home/jiuben/tdx-data-feed/docs
PathModified=/home/jiuben/tdx-data-feed/README.md
PathModified=/home/jiuben/tdx-data-feed/DELIVERY.md
PathModified=/home/jiuben/tdx-data-feed/CHANGELOG.md
PathModified=/home/jiuben/tdx-data-feed/Makefile
PathModified=/home/jiuben/tdx-data-feed/training/eval
PathModified=/home/jiuben/tdx-data-feed/data/plan_e
```
**需要澄清一点前提**：同步脚本 `sync-to-obsidian.sh` 的**输出目录是 Obsidian Vault**（`/home/jiuben/ObsidianVaults/tdx-data-feed/...`，见脚本 `VAULT=` 与各处 `copy_doc`/`archive_report`/`generate_moc`），该 Vault **不在**上述监听列表中。因此严格意义上**不存在"同步脚本写回自身监听目录"的自激环路**。

真正的"重启风暴"来自两点叠加：

1. **监听目录与"数据管线的产出目录"重叠**：监听列表里的 `data/plan_e` 与 `training/eval` 正是**方案 E 回测 / 日志脚本的产出目录**。`plan_e_backtest.py` 每次运行会向 `data/plan_e/` 写入 `plan_e_backtest_v2_YYYYMMDD_HHMMSS.md`、`plan_e_compare_*.md` 等多份报告；`plan_e_journal.py` 同样写 `data/plan_e/`。这些写入会立即触发 `obsidian-sync.service`。
2. **`PathModified` 过于敏感 + `StartLimit` 余量不足**：`PathModified` 对**目录 mtime/元数据变化**也触发（不仅内容），而一次回测会连续写多个文件、或编辑器原子保存（写临时文件 + rename）会产生成倍 inotify 事件；systemd 默认 `StartLimitIntervalSec=10s` / `StartLimitBurst=5`，10 秒内连超 5 次启动即触发 `start-limit-hit`，单元被标记为 `failed` 并停止后续触发——于是 `obsidian-sync` 卡在 failed，直到 `reset-failed` 或限额窗口过期。

> 结论：不是同步自身的自激，而是"监听目录 = 其他管线输出目录"造成的**交叉触发风暴**，叠加 `PathModified` 过触发与默认 `StartLimit` 余量不足。

### 修复方案
四处协同修复（解耦 + 降敏 + 增余量 + 防重入）：

**1. 解耦监听与管线产出目录**（`systemd/obsidian-sync.path`）
仅保留"人工编写的源文档"监听，移除"其他脚本的产出目录"：
```
[Path]
PathModified=/home/jiuben/tdx-data-feed/docs
PathModified=/home/jiuben/tdx-data-feed/README.md
PathModified=/home/jiuben/tdx-data-feed/DELIVERY.md
PathModified=/home/jiuben/tdx-data-feed/CHANGELOG.md
PathModified=/home/jiuben/tdx-data-feed/Makefile
Unit=obsidian-sync.service
```
（移除 `training/eval`、`data/plan_e`。若确实需要把回测/评测报告同步进 Vault，改为在 `plan-e-weekly.timer` / `plan-e-journal.timer` 的 service 里**显式调用**一次 `sync-to-obsidian.sh`，而非靠 path 监听自动触发。）

**2. `PathModified` → `PathChanged`**（同文件）
`PathChanged` 仅在文件**内容**变化时触发，避免 mtime/权限/空目录事件造成的多余触发。

**3. 提高 `.service` 启动限额**（`systemd/obsidian-sync.service` 的 `[Service]` 段）
```
StartLimitIntervalSec=60
StartLimitBurst=10
```
吸收正常的高频编辑/多文件写入突发的重触发。

**4. 脚本加 `flock` + 冷却，杜绝并发与回环**（`scripts/sync-to-obsidian.sh` 顶部）
```bash
LOCK=/tmp/obsidian-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "已有同步在运行，跳过"; exit 0; }   # 已运行则直接退出，避免叠加
# 冷却：距上次成功运行不足 30s 则跳过
STATE=/tmp/obsidian-sync.lastrun
now=$(date +%s)
if [ -f "$STATE" ]; then
  last=$(cat "$STATE"); [ $((now-last)) -lt 30 ] && { echo "冷却期内，跳过"; exit 0; }
fi
echo "$now" > "$STATE"
```

### 验证步骤
```bash
# 1) 清除 failed 状态并重新加载
systemctl --user reset-failed obsidian-sync.service
systemctl --user daemon-reload

# 2) 单次手动运行，校验退出码与 active 状态
systemctl --user start obsidian-sync.service
echo "exit=$?"                       # 应为 0
systemctl --user is-active obsidian-sync.service   # 应为 active(running/exit) 而非 failed

# 3) 确认无 start-limit-hit
journalctl --user -u obsidian-sync.service -n 10 --no-pager | grep -i "start-limit\|failed with result" && echo "仍有风暴" || echo "风暴已消除"

# 4) 触发式验证：修改一个被监听的源文档，确认仅触发一次同步
touch /home/jiuben/tdx-data-feed/README.md
sleep 3
journalctl --user -u obsidian-sync.service -n 5 --no-pager
```

---

## 问题②（确认缺陷）：新鲜度闸门在 3 条管线误杀

### 问题现象
`predict.py` / `plan_e_backtest.py` / `plan_e_journal.py` 默认均在入口调用 `tdxfeed.freshness.require_fresh(max_age_days=2)`，未剔除周末/非交易日后，周一或节假日的手动运行被拦截：
```
❌ 数据新鲜度不达标：抽样 50 只，100% 末日期早于 2026-10-10
  parsed_ok=50/50 阈值=2d  请先跑 full_sync
```
而 `verify_daily_freshness.py` 显示全市场最新交易日为 **2026-10-09**（周五），数据其实是最新的——属误判。注：定时 18:30 链路因 18:00 的 `tdxfeed-sync` 已先拿到当日数据而侥幸通过，掩盖了该缺陷。

### 根因分析（`tdxfeed/freshness.py:62`）
```python
threshold_date = datetime.now().date() - timedelta(days=max_age_days)   # 日历日，不剔除周末/节假日
...
if last.date() < threshold_date:   # 仅当"抽样中 >20% 末日期过期"才拦截
    stale += 1
```
两个缺陷：
1. **日历日阈值**：周五（10-09）→ 周一（10-12）相差 3 个日历日 > 2，必然误杀；节假日同理。
2. **漏检 1 天停滞**：仅当抽样中 >20% 样本过期才拦截，单日同步失败（1 天停滞）不会被发现。

### 修复方案（交易日感知）
引入"最近一个已收盘交易日"作为基准，而非 `today` 的日历日：
```python
import datetime as dt

# 简易 A 股交易日历：剔除周末 + 已知休市日（按需补充每年假期）
CN_HOLIDAYS = {
    "2026-01-01","2026-01-02","2026-04-06","2026-04-07","2026-05-01","2026-05-04",
    "2026-06-19","2026-09-25","2026-10-01","2026-10-02","2026-10-05","2026-10-06",
    "2026-10-07","2026-10-08",  # 2026 国庆调休（示例，按交易所公告校准）
}

def last_trading_day(ref: dt.date) -> dt.date:
    d = ref
    while True:
        d -= dt.timedelta(days=1)
        if d.weekday() >= 5:            # 跳过周六(5)/周日(6)
            continue
        if d.isoformat() in CN_HOLIDAYS: # 跳过休市
            continue
        return d

def require_fresh(parquet_dir=DEFAULT_PARQUET_DIR, max_age_days=2,
                  sample_size=50, allowed_stale_trading_days=1) -> None:
    ref = last_trading_day(dt.date.today())                 # 最近已收盘交易日
    threshold_date = ref - dt.timedelta(days=allowed_stale_trading_days)  # 容忍 1 个交易日的滞后
    # ……其余抽样/判定逻辑不变……
```
- 周一：最近交易日 = 上周五 → 阈值 = 上周五 → 数据为周五即通过。
- 节假日后的首个交易日：跳过假期 → 仍按最近交易日判定。
- 将 `allowed_stale_trading_days` 设为 1，可正确捕捉"同步失败导致数据落后超过 1 个交易日"的真实停滞。

> 进阶：把 `CN_HOLIDAYS` 改为从交易所公告文件/接口加载，避免每年手工维护。

### 验证方式
```bash
# 1) 周一/节假日后，不带 --no-freshness-check 直接运行，应放行
venv/bin/python scripts/predict.py --top 30 --threshold 0.7
#   预期：通过新鲜度闸门，进入特征计算（而非报"不达标"）

# 2) 反向验证（人为制造陈旧）：临时把阈值调到极小，确认能拦截
#   改 max_age_days=0 或 allowed_stale_trading_days=0 后再跑，应 sys.exit(2)
```
同时建议在 `tdxfeed/freshness.py` 增加单测：构造 `last_trading_day` 对周一/节假日/普通工作日返回正确值。

---

## 问题③（低-中）：日K `date` 三态类型漂移，被合规率掩盖

### 问题现象
`scripts/check_schema.py` 扫描 `data/parquet/daily/` 7230 个文件：
```
date 类型分布: {'date32[day]': 1997, 'int64': 5231, 'timestamp[ns]': 1}
date 类型异常: 5232
合规率:       100.0%
```
即 **72.4% 的日K 文件 `date` 列为 `int64`（YYYYMMDD 整数）或 `timestamp[ns]`**，仅 1997 个为规范化的 `date32[day]`；但脚本仍报"合规率 100%"——因为合规口径**只校验列名**，未把 `date` 类型纳入。

### 根因分析（v1/v2 写入口径不一致）
不同下载/增量写入口径不一致（记忆确认）：Sina/akshare 原始行情的 `date` 常以整数 `YYYYMMDD` 或 `datetime` 形式落入 parquet，而部分路径（或历史批次）产出 `date32[day]`。读取层统一用 `_norm_date` 归一化（int64→`to_datetime(str)`、timestamp→`to_datetime`），故**当前非阻塞**；但这是 v1/v2 schema 漂移，与 `tdx-data-schema-v2.md` 提案目标（统一 `date32[day]`）相悖，且 `check_schema` 的"100% 合规"会长期掩盖该漂移，成为隐患。

### 修复方案
**B（立即、非破坏，推荐先做）**：增强 `check_schema.py`，把 `date` 类型纳入合规率
- 新增一列「`date` 类型合规」：文件 `date` 列类型 == `date32[day]` 记为合规，否则异常。
- 输出真实合规率（当前约 `1997/7230 ≈ 27.6%`），让漂移可见、可跟踪。

**A（根本，走 schema 变更流程）**：按 `tdx-data-schema-v2.md` 双写统一 `date32`
1. 新建迁移脚本 `scripts/migrate_v1_to_v2.py`：遍历 7230 文件，将 `date` 列统一 `cast('date')[day]` 后重写（**仅重写类型，不改字段名/顺序/其他列**，符合 FROZEN 约束）。
2. 遵循变更流程：v2 提案已存在 → 提供迁移脚本 → **双写期 30 天**（`_norm_date` 已天然兼容旧类型，无需改读取层）→ 切流 + 删旧。
3. 禁止直接破坏旧 parquet（遵守"不删已有 parquet / 不改字段类型"原则——本方案是新增规范化写入，旧文件保留至切流）。

### 验证方式
```bash
# B 验证：重跑 check_schema，确认出现真实"date 类型合规率"（应远低于 100%）
venv/bin/python scripts/check_schema.py

# A 验证：先对单文件 dry-run 迁移，确认类型变为 date32[day] 且行数/列不变
venv/bin/python scripts/migrate_v1_to_v2.py --dry-run --limit 1
# 全量迁移后在副本上校验：所有文件 date 均为 date32[day]，合规率→100%
```

---

## 低危项

### L1 `plan_e_v3_live_track.py:336-351` 的 8 处 F821 未定义名称
`ruff` 报告 8 处 `F821 Undefined name`，全部集中在实验性脚本 `scripts/plan_e_v3_live_track.py` 的 `cmd_daily()` 函数：
```
336: positions, actions = update_positions(positions, signals, today, cfg)
342: anomalies = check_anomalies(journal, positions, cfg)
350: daily_md = generate_daily_report(today, signals, positions, actions, anomalies, cfg)
351: daily_fp = LIVE_DIR / 'daily_reports' / f'daily_{today.strftime("%Y%m%d")}.md'
```
`signals`、`today`、`cfg` 在 `cmd_daily` 内**从未定义**（应为上游 `load_signals()` / `date.today()` / `load_config()` 的产物），运行到此处必抛 `NameError`。该脚本属 v3 实验性实时跟踪，**不参与核心每日链路**（核心用 `plan_e_journal.py` / `monitor_plan_e_live.py`），故不影响主线，但仍应修复。
**修复**：在 `cmd_daily` 顶部补齐：
```python
today = datetime.now().date()
cfg = load_config()            # 或既有的配置加载函数
signals = load_signals(today)  # 或既有的信号加载函数
```
（具体函数名以该文件既有定义为准；若这些加载函数尚不存在，需按 v3 设计补实现。）

### L2 `fastparquet` 缺失
依赖探测显示 `fastparquet` 未安装（`ModuleNotFoundError`），但 `pyarrow` 已安装并覆盖所有 parquet 读写（check_schema / predict / backtest 均经 pyarrow）。属**可选依赖缺失，无运行影响**。
**处理**：先确认无任何脚本 `import fastparquet`（当前已知无）。若有历史兼容需求，再 `venv/bin/pip install fastparquet`；否则在 `requirements.txt` 中标注其为可选（或移除）。

### L3 `Makefile` 的 `train-v5/v6/v7` 引用已删除的 `/tmp` 训练脚本
`Makefile:248/253/258`：
```
train-v5:  $(PYTHON) /tmp/v5_train_single.py
train-v6:  $(PYTHON) /tmp/v6_train_single.py
train-v7:  $(PYTHON) /tmp/v7_train_v3.py
```
`/tmp` 下的训练脚本已被清理 → `make train-*` 直接 `FileNotFoundError`，**模型重训不可复现**（训练配方不在仓库内）。训练栈本身已验证（QLoRA 单卡可行，见 `training/config/` 与 `scripts/verify_single_qlora.sh`），缺的只是可复现的启动脚本。
**修复**：
1. 将训练启动脚本迁入仓库，如 `training/scripts/train_v5_single.py` 等（与现有 `training/config/*.yaml` 配套）。
2. 更新 `Makefile` 三处路径指向新位置。
3. 提交训练配方（`.gguf` 不入 git，但脚本/配置应入 git）。
**验证**：`make -n train-v7` 应指向存在的文件；`bash -n training/scripts/train_v7_single.py` 语法 OK；`make train-v7` 可实跑（GPU 占用正常、产出新 adapter）。

---

## 未实跑项及原因
| 项 | 原因 | 间接验证 |
|---|---|---|
| 全量 `data-sync`（~40min 下载） | 耗时过长、会写生产数据，本次未跑 | `verify_daily_freshness.py` 源失败率 0% + 数据最新至 2026-10-09 + canary `tdxfeed_sync` 绿 |
| GPU 训练（`make train-*`） | 被 L3 的 `/tmp` 脚本缺失阻断，无法执行 | QLoRA 单卡训练此前已验证可行 |
| 3 条管线"默认闸门"路径 | 被问题②缺陷拦截；本次以 `--no-freshness-check` 验证了**管线本体**，闸门本身即问题② | 见问题②验证方式 |

---

## 下一步可执行建议清单
1. **修复 ①**：编辑 `obsidian-sync.path`（解耦 `data/plan_e`/`training/eval` + `PathChanged`）、`obsidian-sync.service`（加 `StartLimitIntervalSec/Burst`）、`sync-to-obsidian.sh`（加 `flock`+冷却）；`reset-failed` 后按 §问题①验证。
2. **修复 ②**：改造 `tdxfeed/freshness.py` 为交易日感知（增加 `last_trading_day` + `allowed_stale_trading_days`），并补单测；按 §问题②验证周一放行。
3. **修复 ③-B（立即）**：增强 `check_schema.py` 把 `date` 类型纳入合规率，让漂移可见；**③-A（规划）**走 `tdx-data-schema-v2.md` 双写迁移。
4. **修复 L1**：补齐 `plan_e_v3_live_track.py:cmd_daily` 的 `signals/today/cfg` 定义（或实现对应加载函数）。
5. **修复 L3**：迁移训练脚本进 `training/scripts/` 并更新 `Makefile`，恢复 `make train-*` 可复现。
6. **可选 L2**：确认无脚本依赖 `fastparquet` 后，于 `requirements.txt` 标注其可选或安装。
7. **收尾**：提交此前已改动的 `.gitignore`（当前仍为未提交状态，见 `git status`）。

---

## ⚠️ 风险提示
1. 修复 ① 时若保留 `data/plan_e` 监听，需同步把"回测/评测报告入 Vault"改为由对应 timer 的 service 显式调用，避免再次交叉触发。
2. 问题②的 `CN_HOLIDAYS` 需按交易所每年度公告校准，否则跨年/调休仍可能误判。
3. 问题③-A 迁移属于 schema 变更，必须走 `tdx-data-schema-v2.md` 完整流程（双写 30 天 + 切流），不得直接覆盖旧 parquet。
4. L1 脚本为实验性 v3，修改后需确认其加载函数存在，避免引入新的 `NameError`。
5. 历史回测/预测结果不代表未来收益；翻倍策略属激进策略，建议小资金并严格止损（-10%）。

---
生成时间：2026-10-12 12:30:00
生成者：端到端验证 + 故障分析（agent）
数据日期：2026-10-09（全市场最新交易日）
