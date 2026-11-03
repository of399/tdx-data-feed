#!/usr/bin/env python
"""岛内导出器：把 qlib 的 Alpha158 默认因子配置落成可复核、可消费的工件。

为什么要有这个脚本
------------------
`venv-qlib/` 是环境孤岛（主 venv 是 Python 3.14，pyqlib 的 wheel 只到 cp312），孤岛原则是
「表达式字符串离开岛、数值计算回主 venv」。但在那之前，岛只在安装脚本里 self-check 过一句
「能出 158 条」——没有落盘工件，就没人能复核，也没人能消费。这个脚本把那句话变成一份带来源、
算子闭包与内容 hash 的清单：`config/alpha158_expressions.json`。

怎么用
------
    venv-qlib/bin/python scripts/qlib_island/export_alpha158.py            # 写工件
    venv-qlib/bin/python scripts/qlib_island/export_alpha158.py --check     # 岛与工件是否一致
    venv-qlib/bin/python scripts/qlib_island/export_alpha158.py --check --artifact <副本>

三条约定
--------
1. **只能在岛里跑**：`import qlib` 失败就直接非零退出并说明该用哪个解释器。生产侧解释器能把
   它跑起来 = 岛边界名存实亡，所以守卫 `tests/test_alpha158_artifact.py` 专门反向证明这条。
2. **抽取口径单源**：算子 / `$列` / 窗口后缀三条正则与守卫逐字共用（守卫用 AST 比对字面量）。
   两份实现早晚漂，漂了就是工件与判定各说各话。
3. **原子写**：tmp + `os.replace`（SEM001）。这份工件是下游因子宽表的输入，读者不能看到半截。

内容 hash 只覆盖 features 的 name/expr/ops 三键的规范化 JSON，不含时间与 pyqlib 版本——
否则同一条清单换个日期重导也会「漂」。版本变化要体现在 `--check` 失败，而不是哈希里。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from collections import Counter
from datetime import date
from importlib.metadata import version as pkg_version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_REL = Path("config") / "alpha158_expressions.json"
SOURCE_LABEL = "qlib.contrib.data.loader.Alpha158DL.get_feature_config()"

# 抽取口径：与 tests/test_alpha158_artifact.py 逐字相同（守卫会比对这三条字面量）。
FUNC_RE = r"\b([A-Z][A-Za-z0-9]*)\("
DOLLAR_RE = r"(\$+)([A-Za-z_][A-Za-z0-9_]*)"
WINDOW_SUFFIX_RE = r"^(?P<base>.+?)(?P<win>\d+)$"
WINDOWS = (5, 10, 20, 30, 60)

# 比较符号要**先试双字符**：`>=` 里含 `>`，按单字符扫会把一个算子记成另一个。
COMPARISON_RE = r">=|<=|!=|==|[><]"

# 读侧口径：下游必须先解决这两条，否则 158 维里混着伪信号。
CAVEATS = (
    "vwap：本仓库日线没有 vwap 列，只能由 amount/volume 推得；而 2026-09-16 起 amount 是 "
    "close×volume 的代理值（见 KI-DATA-001），这一段里推出来的 vwap 恒等于 close，"
    "相关因子在该区间是伪信号。消费方要按 PROXY_CALIBER_START 分时代处理。",
    "价格列的复权口径：$close/$open/$high/$low 必须来自 `tdxfeed.adjusted.frame` 的读侧权威"
    "（价格特征用 hfq），不得直读 data/hfq_factor 或裸收盘价表——守卫 "
    "tests/test_single_factor_authority.py。",
    "涨跌停/封板类判定仍对 raw 价格，不在本清单的 158 条里；别把两套口径混成一列。",
)


def _qlib_loader():
    try:
        from qlib.contrib.data.loader import Alpha158DL
    except ImportError as exc:  # 离开岛就没有 qlib：显式说明该用哪个解释器
        sys.stderr.write(
            f"需要 qlib（{exc}）：本脚本只在环境孤岛里跑，请用 "
            f"{ROOT / 'venv-qlib' / 'bin' / 'python'} 运行；"
            "生产侧 venv 不装 pyqlib（wheel 只到 cp312，且会带进 mlflow/gym/cvxpy）。\n"
        )
        raise SystemExit(2) from exc
    return Alpha158DL


def _ops(expr: str) -> list[str]:
    return sorted(set(re.findall(FUNC_RE, expr)))


def _split_dollar(expr: str) -> tuple[list[str], list[str]]:
    """`$col` 是日线列，`$$col` 是标的级静态字段：来源不同，必须分流记录。"""
    cols, instr = set(), set()
    for sig, name in re.findall(DOLLAR_RE, expr):
        (instr if sig == "$$" else cols).add(name)
    return sorted(cols), sorted(instr)


def build_payload() -> dict:
    alpha = _qlib_loader()
    fields, names = alpha.get_feature_config()
    assert len(fields) == len(names), "qlib 返回的表达式与名称数量不一致，工件无法一一对应"

    features = [{"name": n, "expr": e, "ops": _ops(e)} for n, e in zip(names, fields, strict=True)]

    families: dict[str, list[int]] = {}
    windowless: list[str] = []
    for name in names:
        m = re.match(WINDOW_SUFFIX_RE, name)
        if m and int(m.group("win")) in WINDOWS:
            families.setdefault(m.group("base"), []).append(int(m.group("win")))
        else:
            windowless.append(name)

    columns: set[str] = set()
    instrument: set[str] = set()
    arithmetic: set[str] = set()
    comparisons: set[str] = set()
    operators: Counter[str] = Counter()
    for expr in fields:
        cols, instr = _split_dollar(expr)
        columns.update(cols)
        instrument.update(instr)
        arithmetic.update(ch for ch in "+-*/" if ch in expr)
        comparisons.update(re.findall(COMPARISON_RE, expr))
        for op in _ops(expr):
            operators[op] += 1

    payload = {
        "version": 1,
        "source": SOURCE_LABEL,
        "provenance": {
            "interpreter": "venv-qlib",
            "python": platform.python_version(),
            "pyqlib": pkg_version("pyqlib"),
            "exported_by": Path(__file__).resolve().relative_to(ROOT).as_posix(),
            "generated_at": date.today().isoformat(),
        },
        "count": len(features),
        "shape": {
            "windows": sorted({w for ws in families.values() for w in ws}),
            "rolling_families": len(families),
            "family_sizes": sorted({len(ws) for ws in families.values()}),
            "windowless": len(windowless),
            "windowless_names": sorted(windowless),
        },
        "columns": sorted(columns),
        "instrument_fields": sorted(instrument),
        # 值 = 「用到该算子的表达式条数」（同一表达式里重复用只记一次），不是出现次数；
        # 四则运算符单列在 arithmetic、比较符号单列在 comparison_operators，三者合起来
        # 才是下游求值器必须实现的完整闭包。qlib 把 `>` 解析成 `Gt`、`<` 解析成 `Lt`
        # （不是 `Greater`/`Less`——那两个取大取小），符号到类名的映射在 ② 工件里。
        "operator_expression_counts": dict(sorted(operators.items())),
        "arithmetic": sorted(arithmetic),
        "comparison_operators": sorted(comparisons),
        "caveats": list(CAVEATS),
        "features": features,
    }
    payload["content_sha256"] = content_sha(features)
    return payload


def content_sha(features: list[dict]) -> str:
    """与守卫同式：只对 name/expr/ops 的规范化 JSON 取 sha256（不含时间与版本）。"""
    canon = json.dumps(
        [{"name": f["name"], "expr": f["expr"], "ops": f["ops"]} for f in features],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def dump(payload: dict) -> str:
    """稳定的文本形态：键序固定、缩进两格、行尾一个换行。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write(path: Path, text: str) -> None:
    """tmp + os.replace：读者要么看到旧清单、要么看到新清单，不会看到半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def first_content_diff(left: dict, right: dict) -> str:
    """比对失败时给出第一条具体差异；只报「hash 不同」等于让人重新二分 158 条。"""
    la, ra = left.get("features", []), right.get("features", [])
    if len(la) != len(ra):
        return f"条数不同：{len(la)} vs {len(ra)}"
    for a, b in zip(la, ra, strict=True):
        if (a["name"], a["expr"]) != (b["name"], b["expr"]):
            return f"第一条差异 {a['name']}: {a['expr']!r} vs {b['name']}: {b['expr']!r}"
    return "features 内容一致，但 hash 不同——两处的 hash 口径分叉了"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出/核对 Alpha158 表达式清单（只能在 venv-qlib 里跑）")
    ap.add_argument("--out", default=str(ROOT / ARTIFACT_REL), help="工件写入路径")
    ap.add_argument("--check", action="store_true", help="与已有工件比对，不写盘")
    ap.add_argument("--artifact", default=None, help="--check 时改比对这份工件（守卫的变异用例用）")
    args = ap.parse_args(argv)

    payload = build_payload()
    target = Path(args.artifact if args.artifact else args.out)

    if args.check:
        if not target.exists():
            sys.stderr.write(f"工件不存在：{target}\n")
            return 1
        stored = json.loads(target.read_text(encoding="utf-8"))
        if stored.get("content_sha256") == payload["content_sha256"]:
            print(f"✅ 一致：{payload['count']} 条，content_sha256={payload['content_sha256'][:16]}…")
            return 0
        print("❌ 岛内重新导出的清单与工件不一致：")
        print("   " + first_content_diff(stored, payload))
        print(f"   工件 hash={stored.get('content_sha256', '')[:16]}… "
              f"岛内 hash={payload['content_sha256'][:16]}…")
        print(f"   工件来自 pyqlib={stored.get('provenance', {}).get('pyqlib')}，"
              f"岛内是 pyqlib={payload['provenance']['pyqlib']}")
        return 1

    atomic_write(target, dump(payload))
    shape = payload["shape"]
    print(f"✅ 写出 {target.relative_to(ROOT)}：{payload['count']} 条 = "
          f"{shape['windowless']} 无窗口 + {shape['rolling_families']} 族 × "
          f"{len(shape['windows'])} 窗口；函数算子 {len(payload['operator_expression_counts'])} 个 "
          f"+ 四则 {payload['arithmetic']} + 比较 {payload['comparison_operators']}；"
          f"字段 {payload['columns']}；"
          f"content_sha256={payload['content_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
