"""打板扫描器：实时识别封板 / 上板中 / 炸板 / 回封，计算封单比并打分排序。

数据链路::

    新浪 L1 五档  →  涨停价(精确到分)  →  封板状态判定
                                      →  封单比(股数法/金额法)
                                      →  历史 parquet 补 连板数/量比/换手
                                      →  打分排序  →  CSV + .blk

封单口径（计划书 §2.6 / 附录 E）：
- **股数法** ``封单股数 / 当日成交量``：L1 五档可算，本模块主用
- **金额法** ``封单金额 / 流通市值``：需流通股本；可用 ``--float-shares`` 传入
  （CSV: code,float_shares），否则降级为股数法并在 reasons 标注

封板状态判定：
- ``封板``   现价 ≥ 涨停价（±半分钱）且买一有量
- ``上板中`` 现价 ≥ 涨停价 × 99.5% 但未封死（扫板窗口）
- ``炸板``   最高价曾触涨停价但现价 < 涨停价
- ``回封``   Watcher 观测到"炸板后再次封死"（§3.3 第二优买点）
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from tdxfeed.daban.limit_price import (
    board_label,
    exchange_prefix,
    is_sealed,
    limit_ratio,
    limit_up_price,
    normalize_code,
)
from tdxfeed.daban.realtime import Quote, fetch_quote_map
from tdxfeed.daban.sector import (
    SectorStat,
    compute_sector_strength,
    load_industry_map,
    load_industry_names,
    load_sector_names,
    sector_of,
)
from tdxfeed.daban.session import SESSION_AUCTION, current_session
from tdxfeed.daban.tencent_source import TQuote, fetch_tencent_map
from tdxfeed.daban.ths_board import ConceptStrength, load_concept_snapshot, match_concepts
from tdxfeed.date_utils import parse_date_series
from tdxfeed.paths import universe_path

__all__ = [
    "Candidate",
    "ScanResult",
    "Scanner",
    "StockState",
]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
UNIVERSE_PATH = universe_path()
DAILY_DIR = DATA_DIR / "parquet" / "daily"

STATUS_SEALED = "封板"
STATUS_NEAR = "上板中"
STATUS_BROKEN = "炸板"
STATUS_RESEAL = "回封"
STATUS_AUCTION_SEAL = "竞价封板"  # 集合竞价阶段虚拟开盘即涨停
STATUS_AUCTION_GAP = "竞价高开"  # 集合竞价高开 ≥5%


@dataclass(slots=True)
class StockState:
    """盘中轮询维护的单股状态机。"""

    code: str
    name: str = ""
    first_seal_time: str = ""
    seal_count: int = 0
    break_count: int = 0
    max_seal_shares: float = 0.0
    last_seal_shares: float = 0.0
    last_status: str = ""
    first_seen_sealed: bool = False
    seal_series: list[tuple[float, float]] = field(default_factory=list)  # (ts, 封单股数)

    @property
    def seal_decay(self) -> float:
        """封单衰减率近似（1 - 当前/峰值）：>50% 视为封板动摇。"""
        if self.max_seal_shares <= 0:
            return 0.0
        return max(0.0, 1.0 - self.last_seal_shares / self.max_seal_shares)

    @property
    def cancel_rate(self) -> float:
        """封单衰减速率（万股/分钟）：最近 3 个采样点的平均撤单速度。

        真实撤单率需 L2 委托队列，此处用"封单量下降速度"做代理。
        """
        pts = self.seal_series[-4:]
        if len(pts) < 2:
            return 0.0
        (t0, s0), (t1, s1) = pts[0], pts[-1]
        if t1 <= t0:
            return 0.0
        drop = (s0 - s1) / 10000.0  # 万股
        return drop / ((t1 - t0) / 60.0)  # 万股/分钟


@dataclass(slots=True)
class Candidate:
    """一条打板候选。"""

    code: str
    name: str
    board: str
    status: str
    price: float
    prev_close: float
    limit_price: float
    pct: float
    seal_shares: float
    seal_amount: float
    seal_ratio_shares: float
    seal_ratio_amount: float | None
    seal_vs_vol5: float
    turnover: float | None
    consecutive: int
    first_seal_time: str
    break_count: int
    seal_decay: float
    cancel_rate: float  # 封单衰减速率（万股/分钟），撤单强度的代理
    score: float
    reasons: list[str] = field(default_factory=list)
    # 打分分项（供全局归一化后重算总分）
    s_seal: float = 0.5
    s_turn: float = 0.5
    s_status: float = 0.5
    s_pos: float = 0.5
    s_market: float = 0.5
    s_sector: float = 0.5
    s_concept: float = 0.5
    seal_grade: str = ""
    concept_name: str = ""
    concept_strength: float = 0.0
    vol_ratio: float = 0.0  # 量比（腾讯源）
    # 板块（§A.1 Sec 因子）
    sector_code: str = ""
    sector_name: str = ""
    sector_sec: float = 0.0
    sector_limit_up_n: int = 0
    sector_pct: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ScanResult:
    """一次扫描的产物。"""

    trade_date: str
    scan_time: str
    phase: str
    candidates: list[Candidate]
    stats: dict
    session: str = ""
    sector_stats: list[dict] = field(default_factory=list)

    def to_frame(self) -> pd.DataFrame:
        if not self.candidates:
            return pd.DataFrame()
        return pd.DataFrame([c.to_dict() for c in self.candidates])


def load_universe(path: str | Path = UNIVERSE_PATH) -> dict[str, str]:
    """载入全市场股票池 {6位代码: 名称}。"""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for code, name in raw.items():
        c = normalize_code(code)
        out[c] = str(name).replace(" ", "")
    return out


def daily_path(code: str, daily_dir: Path | None = None) -> Path | None:
    """定位日线 parquet。

    data/parquet/daily 的文件名带交易所前缀（sh600000.parquet / bj920000.parquet），
    而股票池代码是 6 位无前缀，两种命名都试一遍。
    """
    d = daily_dir or DAILY_DIR
    c = normalize_code(code)
    for name in (f"{exchange_prefix(c)}{c}.parquet", f"{c}.parquet"):
        p = d / name
        if p.exists():
            return p
    return None


def _read_daily(code: str, daily_dir: Path) -> pd.DataFrame | None:
    """读单只股票日线（最后 30 根），失败返回 None。

    ⚠ data/parquet/daily 的 date 列存在 schema 漂移：int64(YYYYMMDD) 与
    date32 两种格式混存，直接 pd.to_datetime 会把 int 当纳秒 → 落到 1970。
    统一走 tdxfeed.date_utils.parse_date_series 归一化。
    """
    p = daily_path(code, daily_dir)
    if p is None:
        return None
    df: pd.DataFrame | None = None
    try:
        df = pd.read_parquet(p, columns=["date", "open", "high", "low", "close", "volume"])
    except Exception:
        try:
            df = pd.read_parquet(p)
        except Exception:
            return None
    if df is None or df.empty:
        return None
    if "date" in df.columns:
        df = df.assign(date=parse_date_series(df["date"])).sort_values("date")
    return df.tail(30)


def _consecutive_limit(df: pd.DataFrame, code: str, name: str) -> int:
    """连板数：从最后一行往前连续满足"收盘 ≥ 当日涨停价"的天数。"""
    if df is None or len(df) < 2:
        return 0
    ratio = limit_ratio(code, name)
    n = 0
    for i in range(len(df) - 1, 0, -1):
        prev_close = float(df["close"].iloc[i - 1])
        if prev_close <= 0:
            break
        lp = limit_up_price(prev_close, ratio)
        if float(df["close"].iloc[i]) >= lp - 0.005:
            n += 1
        else:
            break
    return n


def _seal_grade(ratio: float) -> tuple[str, float]:
    """封单比分档（§2.6）→ (标签, 0~1 打分)。"""
    if ratio < 0.005:
        return "极弱", 0.10
    if ratio < 0.02:
        return "弱", 0.30
    if ratio < 0.08:
        return "正常偏强", 0.60
    if ratio < 0.20:
        return "强", 0.85
    return "极强", 1.00


def _vol_ratio_score(vol_ratio: float) -> float:
    """量比打分（1.0 = 温和放量甜区）。"""
    if vol_ratio <= 0:
        return 0.30
    if vol_ratio < 1.0:
        return 0.30
    if vol_ratio < 1.5:
        return 0.55
    if vol_ratio < 3.0:
        return 0.90
    if vol_ratio < 6.0:
        return 0.75
    return 0.45


def _turnover_score(turnover: float | None) -> float:
    """换手率打分：8%~20% 为活跃甜区（§A.1）。"""
    if turnover is None:
        return 0.50
    t = turnover * 100
    if t < 2:
        return 0.20
    if t < 8:
        return 0.55
    if t <= 20:
        return 1.00
    if t <= 35:
        return 0.60
    return 0.30


def _position_score(consecutive: int) -> float:
    """身位打分：连板高度越高分越高。"""
    if consecutive <= 0:
        return 0.50
    if consecutive == 1:
        return 0.60
    if consecutive == 2:
        return 0.72
    if consecutive == 3:
        return 0.85
    return 0.92


_PHASE_SCORE = {
    "启动期": 0.90,
    "上升期": 0.82,
    "高潮期": 0.55,
    "震荡市": 0.50,
    "冰点转折": 0.45,
    "退潮期": 0.20,
    "熊市": 0.10,
}


class Scanner:
    """打板扫描器。

    参数:
        universe: {code: name}；为 None 时从 data/all_a_stocks.json 载入
        float_shares: {code: 流通股本}，用于金额法封单比与换手率（可选）
        phase: 市场环境标签（决定环境分）
        min_pct: 涨跌幅门槛（%），低于此值不进候选
        allow_st: 是否保留 ST 股（默认剔除）
        max_history_workers: 读 parquet 的并发数
        use_sector: 是否计算板块强度 Sec 因子（§A.1）
        industry_map: code → sector_code；为 None 时从申万映射文件载入
        seal_ratio_map: {code: {seal_shares, seal_amount, ...}}，外部封单源
            （通达信导出 / L2）注入，优先于 L1 快照
    """

    def __init__(
        self,
        universe: dict[str, str] | None = None,
        float_shares: dict[str, float] | None = None,
        phase: str = "震荡市",
        min_pct: float = 4.0,
        allow_st: bool = False,
        daily_dir: Path | None = None,
        max_history_workers: int = 12,
        use_sector: bool = True,
        industry_map: dict[str, str] | None = None,
        sector_names: dict[str, str] | None = None,
        seal_ratio_map: dict[str, dict] | None = None,
        session_mode: str = "auto",
        use_tencent: bool = True,
        min_sector_sec: float = 0.0,
        min_sector_limitup: int = 0,
    ) -> None:
        self.universe = universe if universe is not None else load_universe()
        self.float_shares = float_shares or {}
        self.phase = phase
        self.min_pct = min_pct
        self.allow_st = allow_st
        self.daily_dir = daily_dir or DAILY_DIR
        self.max_history_workers = max_history_workers
        self.use_sector = use_sector
        self.industry_map = industry_map if industry_map is not None else load_industry_map()
        self.sector_names = sector_names if sector_names is not None else load_sector_names()
        self.industry_names = load_industry_names()
        self.concept_board: list[ConceptStrength] = load_concept_snapshot()
        self.seal_ratio_map = seal_ratio_map or {}
        self.use_tencent = use_tencent
        self.min_sector_sec = min_sector_sec
        self.min_sector_limitup = min_sector_limitup
        self.tquotes: dict[str, TQuote] = {}
        self.session_mode = session_mode
        self.session = current_session() if session_mode == "auto" else session_mode
        self.sector_stats: dict[str, SectorStat] = {}
        self.states: dict[str, StockState] = {}

    # ---------- 股票池 ----------

    def symbols(self) -> list[str]:
        """生成带交易所前缀的代码列表。"""
        out = []
        for code, name in self.universe.items():
            if not self.allow_st and ("ST" in name.upper() or "退" in name):
                continue
            out.append(f"{exchange_prefix(code)}{code}")
        return out

    # ---------- 单轮扫描 ----------

    def scan(self, quotes: dict[str, Quote] | None = None) -> ScanResult:
        """执行一次扫描。quotes 为空时自行拉取实时行情。"""
        if quotes is None:
            quotes = fetch_quote_map(self.symbols())

        if self.session_mode == "auto":
            self.session = current_session()

        # 腾讯源：补齐流通市值/成交量/换手率/量比/官方涨停价（新浪 L1 不提供）
        if self.use_tencent:
            syms = list(quotes.keys())
            try:
                self.tquotes = fetch_tencent_map(syms)
            except Exception:
                self.tquotes = {}

        # 板块强度（§A.1）：用全市场快照计算，候选股只是取用方
        if self.use_sector and self.industry_map:
            self.sector_stats = compute_sector_strength(
                quotes,
                self.industry_map,
                self._is_limit_up_quote,
                self.sector_names,
                industry_names=self.industry_names,
            )

        candidates: list[Candidate] = []
        prelim: list[tuple[str, Quote, float, float, str]] = []

        for symbol, q in quotes.items():
            if q.suspended or q.prev_close <= 0:
                continue
            pct = q.pct()
            if pct < self.min_pct:
                continue
            code = normalize_code(symbol)
            name = self.universe.get(code, q.name)
            ratio = limit_ratio(code, name)
            lp = limit_up_price(q.prev_close, ratio)
            status = self._status_of(q, lp)
            prelim.append((code, q, lp, pct, status))

        # 只对候选读历史 parquet，控制 IO
        codes = [c for c, *_ in prelim]
        histories = self._load_histories(codes)

        for code, q, lp, pct, status in prelim:
            name = self.universe.get(code, q.name)
            df = histories.get(code)
            cand = self._build_candidate(code, name, q, lp, pct, status, df)
            if cand is not None:
                candidates.append(cand)

        self._rescore(candidates)
        if self.min_sector_sec > 0:
            # "只做最强主线"（§1.1）：板块强度不足的不进候选
            candidates = [c for c in candidates if c.sector_sec >= self.min_sector_sec]
        if self.min_sector_limitup > 0:
            # 板块共振：当日同板块涨停家数不足的不进候选
            candidates = [
                c for c in candidates if c.sector_limit_up_n >= self.min_sector_limitup
            ]
        candidates.sort(key=lambda c: c.score, reverse=True)
        stats = self._market_stats(candidates)
        top_sectors = sorted(
            self.sector_stats.values(), key=lambda s: s.sec, reverse=True
        )[:10]
        return ScanResult(
            trade_date=self._trade_date(quotes),
            scan_time=time.strftime("%H:%M:%S"),
            phase=self.phase,
            candidates=candidates,
            stats=stats,
            session=self.session,
            sector_stats=[s.to_dict() for s in top_sectors],
        )

    def _is_limit_up_quote(self, price: float, prev_close: float, code: str) -> bool:
        """封死涨停判定（供板块统计使用）。"""
        if prev_close <= 0:
            return False
        name = self.universe.get(code, "")
        return is_sealed(price, limit_up_price(prev_close, limit_ratio(code, name)))

    def _status_of(self, q: Quote, limit_price: float) -> str:
        # 集合竞价阶段：现价 = 虚拟开盘价，封单量 = 未撮合买量（§3.2 竞价看确认）
        if self.session == SESSION_AUCTION:
            if is_sealed(q.price, limit_price):
                return STATUS_AUCTION_SEAL
            if q.prev_close > 0 and q.price / q.prev_close - 1.0 >= 0.05:
                return STATUS_AUCTION_GAP
            return ""
        if is_sealed(q.price, limit_price):
            if q.bid1_vol > 0:
                st = self.states.get(normalize_code(q.code))
                if st is not None and st.break_count > 0:
                    return STATUS_RESEAL
                return STATUS_SEALED
            return STATUS_SEALED  # 涨停但买一无挂单（卖压已尽），仍归封板
        if q.price >= limit_price * 0.995:
            return STATUS_NEAR
        if q.high >= limit_price - 0.005:
            return STATUS_BROKEN
        return ""

    def _load_histories(self, codes: list[str]) -> dict[str, pd.DataFrame]:
        if not codes:
            return {}
        out: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=self.max_history_workers) as pool:
            for code, df in zip(codes, pool.map(lambda c: _read_daily(c, self.daily_dir), codes), strict=True):
                if df is not None:
                    out[code] = df
        return out

    def _build_candidate(
        self,
        code: str,
        name: str,
        q: Quote,
        limit_price: float,
        pct: float,
        status: str,
        df: pd.DataFrame | None,
    ) -> Candidate | None:
        if not status:
            return None
        reasons: list[str] = []

        # 历史量（本地代理分母用）
        turnover: float | None = None
        vol5 = 0.0
        if df is not None and len(df) >= 2:
            vol5 = float(df["volume"].tail(5).mean()) or 0.0
        float_shares = self.float_shares.get(code)
        tq = self.tquotes.get(f"{exchange_prefix(code)}{code}")
        vol_ratio = tq.vol_ratio if tq is not None else 0.0

        # 封单：外部源（通达信导出 / L2）优先，否则用 L1 委买一量/额
        ext = self.seal_ratio_map.get(code, {})
        seal_shares = float(ext.get("seal_shares") or 0) or q.seal_shares
        seal_amount = float(ext.get("seal_amount") or 0) or q.seal_amount
        ext_float_mcap = float(ext.get("float_mcap") or 0)
        ext_volume = float(ext.get("volume") or 0)

        # 当日成交量（股）：通达信导出 > 腾讯 > 无
        if ext_volume > 0:
            volume_shares = ext_volume * 100.0  # 导出按手计
        elif tq is not None and tq.volume_shares > 0:
            volume_shares = tq.volume_shares
        else:
            volume_shares = 0.0

        vol5_shares = vol5 * 100.0  # 近5日均量：手 → 股
        # 股数法封单比：有当日成交量走标准口径，否则用近5日均量作本地代理
        if volume_shares > 0:
            seal_ratio_shares = seal_shares / volume_shares
        else:
            seal_ratio_shares = (seal_shares / vol5_shares) if vol5_shares > 0 else 0.0
        seal_vs_vol5 = (seal_shares / vol5_shares) if vol5_shares > 0 else 0.0

        # 流通市值：通达信导出 > 腾讯 > 流通股本×现价
        float_mcap = (
            ext_float_mcap
            or (tq.float_mcap_yuan if tq is not None else 0.0)
            or (float_shares * q.price if (float_shares and q.price > 0) else 0.0)
        )
        seal_ratio_amount: float | None = None
        if float_mcap > 0:
            seal_ratio_amount = seal_amount / float_mcap

        # 换手率：腾讯直接给；否则用流通股本推算
        if tq is not None and tq.turnover > 0:
            turnover = tq.turnover / 100.0
        elif float_shares and volume_shares > 0:
            turnover = volume_shares / float_shares

        # 涨停价交叉校验（腾讯 47 位是交易所口径）
        limit_mismatch = (
            tq is not None
            and tq.limit_up > 0
            and abs(tq.limit_up - limit_price) > 0.011
        )

        # 连板数
        consecutive = _consecutive_limit(df, code, name) if df is not None else 0

        # 盘中状态机
        state = self.states.get(code)
        if state is None:
            state = StockState(code=code, name=name)
            self.states[code] = state
        state.name = name
        if status in (STATUS_SEALED, STATUS_RESEAL):
            if state.last_status in (STATUS_BROKEN, STATUS_NEAR) and state.first_seal_time:
                state.break_count += 1
            if not state.first_seal_time:
                state.first_seal_time = f"{q.date} {q.time}"
            state.seal_count += 1
            state.last_seal_shares = seal_shares
            state.max_seal_shares = max(state.max_seal_shares, seal_shares)
        elif status == STATUS_BROKEN:
            state.break_count += 1
        state.last_status = status
        # 封单时间序列 → 撤单速率（§2.6 撤单率的本地代理）
        state.seal_series.append((time.time(), seal_shares))
        if len(state.seal_series) > 120:
            state.seal_series = state.seal_series[-120:]

        # 打分分项（总分在 scan() 收尾由 _rescore 统一算，便于全局归一化）
        s_seal = 0.5
        s_turn = _turnover_score(turnover)
        s_status = {
            STATUS_SEALED: 1.0,
            STATUS_RESEAL: 0.95,
            STATUS_AUCTION_SEAL: 0.95,
            STATUS_NEAR: 0.80,
            STATUS_AUCTION_GAP: 0.70,
            STATUS_BROKEN: 0.35,
        }
        s_status = s_status.get(status, 0.5)
        s_pos = _position_score(consecutive)
        s_market = _PHASE_SCORE.get(self.phase, 0.5)

        # 概念（题材）强度：THS 概念榜 + 名称弱匹配（§2.1 主线判断）
        hits = match_concepts(name, self.concept_board) if self.concept_board else []
        concept_name = hits[0][0].concept_name if hits else ""
        concept_strength = hits[0][0].strength if hits else 0.0
        s_concept = (concept_strength / 100.0) if hits else 0.5

        # 板块强度（§A.1）
        sec_code = sector_of(code, self.industry_map)
        stat = self.sector_stats.get(sec_code)
        s_sector = (stat.sec / 100.0) if stat else 0.5
        sector_name = stat.sector_name if stat else ""
        sector_sec = stat.sec if stat else 0.0
        sector_limit_up_n = stat.limit_up_n if stat else 0
        sector_pct = stat.median_pct if stat else 0.0

        reasons.append(f"封单{seal_shares / 10000:.0f}万股/{seal_amount / 10000:.0f}万元")
        reasons.append(f"封单/近5日均量={seal_vs_vol5:.1f}x")
        reasons.append(f"状态={status}")
        if consecutive > 0:
            reasons.append(f"{consecutive}连板")
        if turnover is not None:
            reasons.append(f"换手{turnover * 100:.1f}%")
        else:
            reasons.append("换手缺当日成交量(需L2/快照源)")
        if vol_ratio:
            reasons.append(f"量比{vol_ratio:.1f}")
        if limit_mismatch:
            reasons.append(
                f"涨停价校验差异:自算{limit_price:.2f}/交易所{tq.limit_up:.2f}"  # type: ignore[union-attr]
            )
        if seal_ratio_amount is None:
            reasons.append("金额法封单比缺流通股本")
        if concept_name:
            reasons.append(f"题材{concept_name}(强度{concept_strength:.0f})")
        if stat is not None:
            reasons.append(
                f"板块{sector_name or sec_code}(Sec{sector_sec:.0f} 涨{sector_pct:+.1f}% 涨停{sector_limit_up_n}家)"
            )
        if state.cancel_rate > 0:
            reasons.append(f"撤单速率{state.cancel_rate:.0f}万股/分")

        return Candidate(
            code=code,
            name=name,
            board=board_label(code, name),
            status=status,
            price=q.price,
            prev_close=q.prev_close,
            limit_price=limit_price,
            pct=round(pct, 2),
            seal_shares=seal_shares,
            seal_amount=seal_amount,
            seal_ratio_shares=seal_ratio_shares,
            seal_ratio_amount=seal_ratio_amount,
            seal_vs_vol5=round(seal_vs_vol5, 3),
            turnover=round(turnover, 4) if turnover is not None else None,
            vol_ratio=round(vol_ratio, 2),
            consecutive=consecutive,
            first_seal_time=state.first_seal_time,
            break_count=state.break_count,
            seal_decay=round(state.seal_decay, 3),
            cancel_rate=round(state.cancel_rate, 2),
            score=0.0,
            reasons=reasons,
            s_seal=s_seal,
            s_turn=s_turn,
            s_status=s_status,
            s_pos=s_pos,
            s_market=s_market,
            s_sector=s_sector,
            s_concept=s_concept,
            concept_name=concept_name,
            concept_strength=round(concept_strength, 1),
            sector_code=sec_code,
            sector_name=sector_name,
            sector_sec=round(sector_sec, 1),
            sector_limit_up_n=sector_limit_up_n,
            sector_pct=round(sector_pct, 2),
        )

    def _rescore(self, candidates: list[Candidate]) -> None:
        """统一重算打分。

        封单分的口径选择：
        - 有流通股本 → 金额法 ``封单额/流通市值``，套用计划书 §2.6 的绝对档位
        - 无流通股本 → ``封单量/近5日均量`` 与 §2.6 档位**不同量纲**，不能套绝对阈值，
          改用候选集内的相对分位（只用于排序，不作绝对强弱结论）
        """
        if not candidates:
            return
        has_amount = any(c.seal_ratio_amount is not None for c in candidates)
        if has_amount:
            for c in candidates:
                if c.seal_ratio_amount is not None:
                    c.seal_grade, c.s_seal = _seal_grade(c.seal_ratio_amount)
        else:
            vals = pd.Series([c.seal_vs_vol5 for c in candidates], dtype=float)
            ranks = vals.rank(pct=True).to_numpy()
            for c, r in zip(candidates, ranks, strict=True):
                c.seal_grade = "相对分位"
                c.s_seal = float(r)
        for c in candidates:
            c.score = round(
                0.20 * c.s_seal
                + 0.12 * c.s_turn
                + 0.28 * c.s_sector
                + 0.16 * c.s_status
                + 0.09 * c.s_pos
                + 0.10 * c.s_concept
                + 0.05 * c.s_market,
                4,
            )

    def _market_stats(self, candidates: list[Candidate]) -> dict:
        """由候选粗略估计市场情绪（涨停/炸板家数）。"""
        sealed = sum(1 for c in candidates if c.status in (STATUS_SEALED, STATUS_RESEAL))
        broken = sum(1 for c in candidates if c.status == STATUS_BROKEN)
        max_con = max((c.consecutive for c in candidates), default=0)
        return {
            "sealed_candidates": sealed,
            "broken_candidates": broken,
            "max_consecutive": max_con,
            "total_candidates": len(candidates),
        }

    @staticmethod
    def _trade_date(quotes: dict[str, Quote]) -> str:
        for q in quotes.values():
            if q.date:
                return q.date
        return time.strftime("%Y%m%d")


def iter_candidates(result: ScanResult, limit: int = 20) -> Iterable[Candidate]:
    """迭代前 N 条候选。"""
    yield from result.candidates[:limit]
