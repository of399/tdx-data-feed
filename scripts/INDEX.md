# scripts/ 脚本目录（W1 归档索引）

> 本文件为脚本分类索引，便于检索与维护。**未做物理移动**，避免破坏现有自动化管线。
> 如要对具体脚本做物理归档（移入 `scripts/archive/`），请单独确认。

## 一、数据采集（daily / 复权 / 分钟线）
| 脚本 | 用途 |
|---|---|
| `download_universe.py` | 全市场日 K 同步（股票/ETF；**债券已移除**，见下） |
| `download_etf_bond.py` | ETF 日 K 下载（**可转债已弃用**：项目政策不再保留债券数据） |
| `download_adj_factor_b.py` / `_bj.py` / `_watch.py` | 复权因子下载（沪/深/北交所 + 监控） |
| `download_bj_daily.py` | 北交所日 K |
| `download_hfq_factor.py` | 后复权因子 |
| `backfill_adj_etf.py` | ETF 复权因子补算（**CB 分支已弃用**） |
| `backfill_minutes.py` | 分钟线补算 |
| `min_sync.py` | 分钟线全量同步（core-full / market-60d） |
| `min_sync2.py` | 分钟线同步 v2：`market-60d`(min15) / `core-1y`(min60) / `market-5d`(min5, Sina) / `core-full-em`(Eastmoney 全历史，目前接口不稳定) |
| `akshare_industry_loader.py` | 同花顺/申万行业分类加载(股票→行业映射) |
| `akshare_concept_loader.py` | 同花顺(THS)题材/概念板块列表加载与查询(名称+代码) |
| `tdx_concept_loader.py` | 通达信(tdx)个股→概念归属(解析 block_gn/hy/fg/zs/wg.dat，独立于 THS) |

## 二、特征 / 因子
| 脚本 | 用途 |
|---|---|
| `factor_engine.py` | 因子引擎 |
| `factor_significance.py` | 因子显著性检验 |
| `sentiment_factor.py` | 情绪因子 |
| `check_schema.py` | schema 校验 |

## 三、翻倍样本 / 蒸馏
| 脚本 | 用途 |
|---|---|
| `find_double_up_c.py` / `find_double_up_c_v2.py` | 翻倍样本挖掘（v2 为最新） |
| `find_d_signal_5d.py` | 5 日翻倍信号样本 |
| `build_distill_skeleton.py` / `batch_distill.py` / `convert_distill.py` / `distill_outputs.py` | 蒸馏数据构建与转换 |

## 四、训练 / 评估
| 脚本 | 用途 |
|---|---|
| `finalize_eval.py` / `redo_finalize.py` | 训练收尾与评测 |
| `verify_v2.py` | v2 schema 验证 |
| `migrate_v1_to_v2.py` | v1→v2 迁移（一次性） |
| `merge_v5_v6_lora.py` | LoRA 合并（产出 v8-merged） |
| `plot_loss.py` | 训练 loss 曲线 |

## 五、预测 / 推理 / 策略
| 脚本 | 用途 |
|---|---|
| `predict.py` | 方案 D 每日翻倍潜力预测（LightGBM）；**新增 `--web` Gradio UI** |
| `plan_e_backtest.py` / `plan_e_v3_early_backtest.py` / `plan_e_v3_live_track.py` / `plan_e_journal.py` | 方案 E 回测 / 实盘跟踪 / 日志 |

## 六、监控 / 运维 / 分析
| 脚本 | 用途 |
|---|---|
| `canary_health_check.py` | canary 健康检查（含 ollama） |
| `train_health.sh` / `train_monitor.sh` / `monitor_gpu_temp.sh` | 训练监控 / GPU 温度 |
| `watch_eastmoney.sh` | Eastmoney 接口探测（触发分钟线续传） |
| `sync-to-obsidian.sh` | 笔记同步（项目库 repo → vault，单向） |
| `install-math-templates.sh` | 数学笔记库模板安装到 vault（`--init-config` / `--patch-templater` / `--dry-run`） |
| `install-obsidian-plugins.sh` | 从 GitHub Releases 安装 Obsidian 社区插件本体（`--vault` / `--plugins` / `--retries`） |
| `drive_expand.sh` / `drive_train_10k.sh` | 训练驱动 |
| `v3_premarket_top.py` / `v3_simulate.py` / `v3_simulate_rolling.py` / `top10_deep_dive.py` | v3 盘前/模拟/深度分析 |
| `gen_dashboard.py` / `analyze_one.py` / `xdxr_verify.py` / `test_bjs_akshare.py` | 看板 / 单标的分析 / 除权验证 / 北交所源测试 |

## 七、对标 / 基准（新增）
| 脚本 | 用途 |
|---|---|
| `bench_v567.py` | **F1**：v5/v6/v7 推理对标——BLEU/ROUGE + 翻倍分类准确率 + P95 延迟 + 显存峰值 |

## 八、数据治理状态（2026-09-28）
- **债券数据下线**：`download_universe.py`/`download_etf_bond.py`/`backfill_adj_etf.py` 已移除债券处理；已删除 `data/parquet/daily/` 下 317 个债券落盘文件与 `data/convertible_bond_codes.txt`。后续不再保留债券数据。
- **大文件出 git**：`v5/audit/` 下 ~80MB 大文件已 `git rm --cached` 并加入 `.gitignore`（磁盘保留，仅停止跟踪）。
- **min5 补齐**：`min_sync2.py --mode market-5d`（Sina scale=5，全市场）补齐 `data/parquet/min5/`（Eastmoney 全历史接口当前不可用）。
