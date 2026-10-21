"""同花顺（THS）概念板块强度。

**为什么需要它**：申万行业能拿到成分股 → 能算"板块内涨停家数/领涨股"，
但同花顺概念（题材）**没有成分股接口**，而打板最关心的恰恰是题材（§2.1）。
THS 的 ``stock_board_concept_info_ths`` 虽无成分股，但直接给出**板块指数行情**：

============  =====================  ==========================================
项目          含义                   用途
============  =====================  ==========================================
板块涨幅      概念指数今日涨幅        强度主项
涨幅排名      "89/390"              排名百分位（§A.1 的"排名百分位"项）
涨跌家数      "778/284"              广度（上涨家数占比）
资金净流入    亿元                   资金合力
成交额        亿元                   热度
============  =====================  ==========================================

概念强度分::

    ConceptStrength = 40×排名百分位 + 30×min(涨幅,5%)/5%×100 + 30×min(资金净流入,50亿)/50亿×100

个股→概念的归属用**名称关键词弱匹配**（无成分股时的可用替代）：
概念名去掉"概念/板块/产业/设备/服务/技术"等后缀后，核心词出现在个股简称中即命中。
弱匹配只作"题材提示"，不可作为交易依据。

缓存：``data/concept/ths_board_snapshot.csv``（带抓取时间），盘中可增量刷新。
"""
from __future__ import annotations

import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ConceptStrength",
    "fetch_concept_board",
    "load_concept_snapshot",
    "match_concepts",
    "save_concept_snapshot",
]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CONCEPT_LIST = DATA_DIR / "concept" / "ths_concept_list.csv"
SNAPSHOT_CSV = DATA_DIR / "concept" / "ths_board_snapshot.csv"

# 概念名里的通用后缀，弱匹配时剔除
_STOPWORDS = (
    "概念",
    "板块",
    "产业",
    "设备",
    "服务",
    "技术",
    "行业",
    "制造",
    "产品",
    "其他",
    "Ⅱ",
    "Ⅲ",
)


@dataclass(slots=True)
class ConceptStrength:
    """单个概念板块的强度。"""

    concept_name: str
    concept_code: str = ""
    pct: float = 0.0  # 板块涨幅 %
    rank: str = ""  # 涨幅排名 "89/390"
    rank_pct: float = 0.0  # 排名百分位 0~1（越大越强）
    up_n: int = 0
    down_n: int = 0
    net_inflow: float = 0.0  # 资金净流入（亿）
    amount: float = 0.0  # 成交额（亿）
    strength: float = 0.0  # 0~100

    def to_dict(self) -> dict:
        return {
            "concept_code": self.concept_code,
            "concept_name": self.concept_name,
            "pct": round(self.pct, 2),
            "rank": self.rank,
            "rank_pct": round(self.rank_pct, 3),
            "up_n": self.up_n,
            "down_n": self.down_n,
            "net_inflow": round(self.net_inflow, 2),
            "amount": round(self.amount, 2),
            "strength": round(self.strength, 1),
        }


def _parse_num(text: str) -> float:
    s = str(text).replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _parse_rank(text: str) -> tuple[int, int]:
    """'89/390' → (89, 390)。"""
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(text))
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def load_concept_list(path: Path | str = CONCEPT_LIST) -> list[tuple[str, str]]:
    """载入本地概念列表 [(code, name)]。"""
    p = Path(path)
    out: list[tuple[str, str]] = []
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = str(row.get("concept_code", "")).strip()
            name = str(row.get("concept_name", "")).strip()
            if name:
                out.append((code, name))
    return out


def _fetch_one(name: str, retries: int = 2) -> ConceptStrength | None:
    import akshare as ak

    for _ in range(retries):
        try:
            df = ak.stock_board_concept_info_ths(symbol=name)
            if df is None or df.empty:
                return None
            kv = {str(r["项目"]).strip(): str(r["值"]).strip() for _, r in df.iterrows()}
            cs = ConceptStrength(concept_name=name)
            cs.pct = _parse_num(kv.get("板块涨幅", "0"))
            cs.rank = kv.get("涨幅排名", "")
            r, total = _parse_rank(cs.rank)
            cs.rank_pct = (1 - (r - 1) / total) if r and total else 0.0
            up, down = _parse_num(kv.get("涨跌家数", "0").split("/")[0]), 0.0
            parts = kv.get("涨跌家数", "0").split("/")
            if len(parts) == 2:
                up, down = _parse_num(parts[0]), _parse_num(parts[1])
            cs.up_n, cs.down_n = int(up), int(down)
            cs.net_inflow = _parse_num(kv.get("资金净流入(亿)", "0"))
            cs.amount = _parse_num(kv.get("成交额(亿)", "0"))
            cs.strength = (
                40 * cs.rank_pct
                + 30 * min(max(cs.pct, 0.0), 5.0) / 5.0
                + 30 * min(max(cs.net_inflow, 0.0), 50.0) / 50.0
            )
            return cs
        except Exception:
            time.sleep(0.3)
    return None


def fetch_concept_board(
    workers: int = 8,
    limit: int = 0,
) -> list[ConceptStrength]:
    """并发拉取全部概念板块强度（本地概念列表）。"""
    pairs = load_concept_list()
    if limit:
        pairs = pairs[:limit]
    if not pairs:
        return []
    out: list[ConceptStrength] = []
    names = [n for _, n in pairs]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for code, _name, cs in zip(
            pairs,
            names,
            pool.map(lambda n: _fetch_one(n), names),
            strict=True,
        ):
            if cs is None:
                continue
            cs.concept_code = code
            out.append(cs)
    out.sort(key=lambda c: c.strength, reverse=True)
    return out


def save_concept_snapshot(
    board: list[ConceptStrength],
    path: Path | str = SNAPSHOT_CSV,
) -> Path:
    """落盘快照（带抓取时间），供盘中快速读取。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fields = list(board[0].to_dict().keys()) if board else ["concept_name"]
    fields = [*fields, "fetched_at"]
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for c in board:
            row = c.to_dict()
            row["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(row)
    return p


def load_concept_snapshot(path: Path | str = SNAPSHOT_CSV) -> list[ConceptStrength]:
    """读取快照。"""
    p = Path(path)
    out: list[ConceptStrength] = []
    if not p.exists():
        return out
    with p.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            cs = ConceptStrength(concept_name=row.get("concept_name", ""))
            cs.concept_code = row.get("concept_code", "")
            cs.pct = _parse_num(row.get("pct", "0"))
            cs.rank = row.get("rank", "")
            cs.rank_pct = _parse_num(row.get("rank_pct", "0"))
            cs.up_n = int(_parse_num(row.get("up_n", "0")))
            cs.down_n = int(_parse_num(row.get("down_n", "0")))
            cs.net_inflow = _parse_num(row.get("net_inflow", "0"))
            cs.amount = _parse_num(row.get("amount", "0"))
            cs.strength = _parse_num(row.get("strength", "0"))
            out.append(cs)
    out.sort(key=lambda c: c.strength, reverse=True)
    return out


def _core_words(concept_name: str) -> list[str]:
    """概念名 → 核心词（去通用后缀后剩下的 2 字以上片段）。"""
    name = concept_name
    for w in _STOPWORDS:
        name = name.replace(w, "")
    name = re.sub(r"[（(].*?[)）]", "", name)
    if len(name) >= 2:
        return [name]
    return []


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（用于"美格智能"↔"AI智能体"这类部分命中）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def match_concepts(
    stock_name: str,
    board: list[ConceptStrength],
    top: int = 3,
    min_lcs: int = 2,
) -> list[tuple[ConceptStrength, float]]:
    """个股简称 → 候选概念（名称最长公共子串弱匹配）。

    返回 [(概念, 相似度)]，按 概念强度×相似度 排序。

    例："美格智能" ↔ "AI智能体"（公共子串"智能"，相似度 0.4）
    """
    if not stock_name:
        return []
    scored: list[tuple[ConceptStrength, float]] = []
    for cs in board:
        best_sim = 0.0
        for word in _core_words(cs.concept_name):
            lcs = _lcs_len(word, stock_name)
            if lcs >= min_lcs:
                best_sim = max(best_sim, lcs / len(word))
        if best_sim > 0:
            scored.append((cs, best_sim))
    scored.sort(key=lambda pair: pair[0].strength * pair[1], reverse=True)
    return scored[:top]
