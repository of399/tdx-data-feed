"""五张行情表的路径定位：只定位，不 fallback，缺文件就报错。

各表命名不一致是历史事实，不要在这里抹平：
  daily / min*   → 带市场前缀 sh600519.parquet
  adj_factor / hfq_factor → 纯 6 位 600519.parquet
  derived        → 带前缀 + _indicators 后缀 sh600519_indicators.parquet
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("TDX_OUT_ROOT", ROOT / "data"))


def _six(code: str) -> str:
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            c = c[2:]
            break
    return c.zfill(6)


def exchange_prefix(code: str) -> str:
    """文件名该用的市场前缀：入参自带前缀时以入参为准，否则按代码段推断。

    不能无条件重推：sh000001（上证指数）与 sz000001（平安银行）同时存在，
    重推会把前者悄悄指到后者。
    """
    c = str(code).lower().strip()
    for p in ("sh", "sz", "bj"):
        if c.startswith(p):
            return p
    six = _six(c)
    if six.startswith(("920", "8", "4")):
        return "bj"
    return "sh" if six[0] in "69" else "sz"


DAILY_BAK_SUFFIX = ".v1.bak.parquet"
DERIVED_SUFFIX = "_indicators"
_DAILY_NAME_RE = re.compile(r"(?:sh|sz|bj)\d{6}\.parquet")
_HFQ_NAME_RE = re.compile(r"\d{6}\.parquet")


def _minute_name(period: str) -> str:
    p = str(period).strip().lower()
    if p.startswith("min"):
        p = p[len("min"):]
    elif p.endswith("min"):
        p = p[:-len("min")]
    if p.startswith("m"):
        p = p[1:]
    return f"min{p}"


def parquet_root(root: str | Path | None = None) -> Path:
    """root 只为调用方带自己的数据根（如 --root / Writer.out_root）时保留覆盖。"""
    return (Path(root) if root is not None else DATA_ROOT) / "parquet"


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
    """active daily 文件：只认 前缀+6位 的规范名（排除迁移备份与中断残留）；按名排序。"""
    return sorted(p for p in daily_dir().glob("*.parquet")
                  if _DAILY_NAME_RE.fullmatch(p.name))


def adj_factor_files() -> list[Path]:
    return sorted(adj_factor_dir().glob("*.parquet"))


def hfq_factor_codes() -> list[str]:
    """后复权因子表的标的枚举：只认裸 6 位文件名，排除备份与中断残留（`*.tmp`）。

    写侧（`tdxfeed.hfq_factor` 的全量/缺项更新）与审计侧（`scripts/audit_hfq_raw_close.py`）
    必须扫同一批文件——否则「全市场对账」这句话没有可比对象。判据只放这一处。
    """
    return sorted(p.stem for p in hfq_factor_dir().glob("*.parquet")
                  if _HFQ_NAME_RE.fullmatch(p.name))


def daily_dir(root: str | Path | None = None) -> Path:
    return parquet_root(root) / "daily"


def daily_path(code: str) -> Path:
    return daily_dir() / f"{exchange_prefix(code)}{_six(code)}.parquet"


def minute_path(code: str, period: str) -> Path:
    return minute_dir(period) / f"{exchange_prefix(code)}{_six(code)}.parquet"


def adj_factor_path(code: str) -> Path:
    return adj_factor_dir() / f"{_six(code)}.parquet"


def hfq_factor_path(code: str) -> Path:
    return hfq_factor_dir() / f"{_six(code)}.parquet"


def derived_path(code: str) -> Path:
    c = str(code).lower().strip()
    if c.endswith(DERIVED_SUFFIX):
        c = c[:-len(DERIVED_SUFFIX)]
    return derived_dir() / f"{exchange_prefix(c)}{_six(c)}{DERIVED_SUFFIX}.parquet"


def universe_path() -> Path:
    return DATA_ROOT / "all_a_stocks.json"


def model_dir() -> Path:
    return DATA_ROOT / "double_up"


def model_path(name: str) -> Path:
    """LightGBM 模型文件的读侧定位；只拼路径，不检查存在性。"""
    return model_dir() / name


def state_dir() -> Path:
    return DATA_ROOT / ".state"


def amount_ledger_path() -> Path:
    """估算成交额的旁路清单（不改 daily 表 schema）。"""
    return state_dir() / "amount_estimated.json"


def factor_run_path() -> Path:
    """因子任务（adj / hfq）的进度心跳落点。

    为什么要有这份：`ExecStartPost=-` 会把后置任务的失败吞成 `Finished`，而
    `TimeoutStartSec` 截断整轮时后半段一个完成标记都不留（KI-OPS-004）。有这份
    心跳，「跑到第几只断的、还剩多少只」事后能查；没这份就只能翻日志末行。
    """
    return state_dir() / "factor_run.json"


def st_history_dir() -> Path:
    """日期化 ST 面板的**归组目录**：面板 + 原始台账 + 覆盖度报告三样都在这个目录下。

    不放 `data/parquet/` 是刻意的：那张根目录按仓库惯例是行情表与其备份的容器
    （`daily/ derived/ finance/ min1/ …`，顶层那个 `finance.parquet` 是历史例外），
    把非行情产物放进去会让「这个目录里的东西都受 `write_table` 行情契约管」这条直觉变模糊。
    面板本身是 B1/B2 要消费的产物（不是 `.state/` 那种可丢弃旁路），所以它与它的原始底座
    同目录归组，比散开更可发现。
    """
    return DATA_ROOT / "st_history"


def st_event_path() -> Path:
    """ST 事件长表本体（唯一真相：不落派生的逐日区间表）。与原始台账同目录归组。"""
    return st_history_dir() / "st_events.parquet"


def st_board_snapshot_path() -> Path:
    """ST 板块**逐日累积**快照表：台账只能回溯少数几条，历史段就靠这张表一天一天攒出来。"""
    return st_history_dir() / "st_board_snapshot.parquet"


def st_raw_dir() -> Path:
    """原始抓取落点：先落 JSONL 再解析，采集可续跑、解析可重放。"""
    return st_history_dir() / "raw"


def st_f10_ledger_path() -> Path:
    return st_raw_dir() / "f10_ledger.jsonl"


def st_block_snapshot_path() -> Path:
    return st_raw_dir() / "block_gn_st.json"


def st_snapshot_days_path() -> Path:
    """「哪些日子采到了板块快照」的登记：空成员那天也要记，否则断档会被读成『那天没人 ST』。"""
    return st_raw_dir() / "block_gn_captured_days.json"


def st_coverage_path() -> Path:
    return st_history_dir() / "coverage.json"


def read_path(path: Path) -> Path:
    """存在性守卫：把「路径算错」变成立刻可见的异常，而不是空表。"""
    if not Path(path).exists():
        raise FileNotFoundError(str(path))
    return Path(path)
