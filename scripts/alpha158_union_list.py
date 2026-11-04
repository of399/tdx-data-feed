"""把「现役独立 + 158 净新增」这份候选并集从 run_meta 落成可复核的工件。

为什么需要第三个工件：`config/alpha158_expressions.json` 是 158 条**全集**，而接进宽表的是
判据筛过的一个视图——它随 ρ 与 horizon 漂（ρ=0.7/0.8/0.9 的净新增是 30/47/78 条；h=5 的
47 条与 h=1/10/20 的交集只有 31 条）。所以名单必须由导出器从那次面板的 `run_meta` 现取，
带齐 ρ/horizon/tag 三项口径，下游只读工件——抄进 .py 常量表就等于在代码里留了第二套口径，
而它不会报错，只会悄悄跟着上一次的面板。

用法：
    # 从某轮面板的 run_meta 产出工件
    venv/bin/python scripts/alpha158_union_list.py \
        --run-meta data/alpha158_ic_vwapfix/run_meta_2024-01-01_2026-09-15_h5_n1200_s20261104.json \
        --rho 0.8 --horizon 5
    # 核对工件与 run_meta 是否还一致（run_meta 不在时退化为自证哈希）
    venv/bin/python scripts/alpha158_union_list.py --check

守卫：tests/test_alpha158_union.py。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tdxfeed.stage1_wide_table import FEAT26_ORDER  # noqa: E402  # 现役维的单一来源

DEFAULT_OUT = ROOT / "config" / "alpha158_union.json"
EXPRESSIONS = ROOT / "config" / "alpha158_expressions.json"
EXPORTED_BY = "scripts/alpha158_union_list.py"


def content_hash(body: dict) -> str:
    """去掉 content_sha256 后按 sort_keys 序列化再 sha256（与守卫同一算法）。"""
    stripped = {k: v for k, v in body.items() if k != "content_sha256"}
    return hashlib.sha256(
        json.dumps(stripped, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def alpha158_names() -> list[str]:
    payload = json.loads(EXPRESSIONS.read_text(encoding="utf-8"))
    return [f["name"] for f in payload["features"]]


def _rel(p: Path) -> str:
    """provenance 里记仓库相对路径；仓库外（临时目录、别的盘）就原样记，不能因此抛异常。

    写成 parents 判定而不是 try/except `relative_to`：语义守卫（SEM002）要把「except 里
    既不 raise 也不计数」的吞错判成红，而这里本来就不是错误路径——用条件判断更诚实。
    """
    resolved = Path(p).resolve()
    if ROOT in resolved.parents:
        return str(resolved.relative_to(ROOT))
    return str(p)


def build_union(run_meta: Path, rho: float, horizon: int) -> dict:
    """从一轮面板的 run_meta 取两个视图，并做两侧权威名单的解析检查。"""
    meta = json.loads(run_meta.read_text(encoding="utf-8"))
    key = str(rho)
    block = meta.get("dedup", {}).get(key)
    if block is None:
        raise SystemExit(f"❌ run_meta 的 dedup 里没有 ρ={key} 这一档；"
                         f"有的档位是 {sorted(meta.get('dedup', {}))}")
    if str(horizon) not in meta.get("horizons", {}):
        raise SystemExit(f"❌ run_meta 的 horizons 里没有 h={horizon}；"
                         f"有的是 {sorted(meta.get('horizons', {}))}")
    nn = list(block["net_new"]["kept_dims"])
    f26 = list(block["within"]["FEAT26"]["kept_dims"])
    names = set(alpha158_names())
    stray = [c for c in nn if c not in names]
    if stray:
        raise SystemExit(f"❌ 净新增里有不在 158 清单工件的维: {stray}")
    stray_f = [c for c in f26 if c not in FEAT26_ORDER]
    if stray_f:
        raise SystemExit(f"❌ 现役独立里有不在 FEAT26_ORDER 的维: {stray_f}")
    overlap = sorted(set(nn) & set(f26))
    if overlap:
        raise SystemExit(f"❌ 两侧有重名维，直接相加会重复计数: {overlap}")

    body: dict = {
        "version": 1,
        "dedup": {
            "rho": rho,
            "horizon": horizon,
            "criterion": "tdxfeed.alpha158.ic.independent_subset：按 IC 序列 |corr| ≥ ρ 归并，"
                         "每簇留 |mean_IC| 最大的一条；净新增 = 以现役独立维为 seed 再筛一次",
        },
        "feat26_independent": f26,
        "alpha158_net_new": nn,
        "union": f26 + nn,
        "counts": {"feat26_independent": len(f26), "alpha158_net_new": len(nn),
                   "union": len(f26) + len(nn)},
        "caveats": [
            "这份名单是**一个判据视图**，不是「独立信息量」：ρ 从 0.8 挪到 0.7/0.9，净新增是 "
            "30/47/78 条；换前瞻窗口，h=5 的 47 条与 h=1/10/20 的交集只有 31 条（每窗离开的 "
            "8–11 条里绝大多数是簇内代表换位，被现役真挡掉的只有 1–2 条）。消费前先看 dedup。",
            "名单对**装配口径**敏感：跨口径的伪 VWAP0 曾把现役 is_gem/limit 当冗余挡掉，同口径修 "
            "复后净新增从 8 条变 9 条（KI-DATA-005）。特征列换了口径，这份清单就得重导。",
            "现役 26 维里被本视图丢掉的那 11 条不是「没信号」，是「与留下的那条同一天一起响」；"
            "所以宽表按本清单接列时，是**降维**而不是裁剪——对照跑必须同时给出 base26 与 base26+"
            "净新增两种接法，否则读不出「丢维」的代价。",
        ],
        "provenance": {
            "exported_by": EXPORTED_BY,
            "generated_at": date.today().isoformat(),
            "run_meta": _rel(run_meta),
            "panel_tag": meta.get("tag"),
            "window": [meta.get("start"), meta.get("end")],
            "seed": meta.get("seed"),
            "min_codes": meta.get("min_codes"),
            "codes_built": meta.get("codes_built"),
            "days": meta.get("days"),
        },
    }
    body["content_sha256"] = content_hash(body)
    return body


def check(path: Path, run_meta: Path | None, rho: float, horizon: int) -> int:
    """① 自证哈希（不依赖 data/，CI 也跑）；② run_meta 在场时逐字段重算比对。"""
    body = json.loads(path.read_text(encoding="utf-8"))
    stored = body.get("content_sha256")
    if stored != content_hash(body):
        print(f"❌ {path.name} 的 content_sha256 与内容不符——工件被手改过", file=sys.stderr)
        return 1
    target = run_meta or (ROOT / body["provenance"]["run_meta"])
    if not Path(target).exists():
        print(f"⚠️ run_meta 不在场（{target}）：只核了自证哈希，没有与面板重算比对")
        return 0
    fresh = build_union(Path(target), rho, horizon)
    a = {k: v for k, v in fresh.items() if k not in ("content_sha256",)}
    b = {k: v for k, v in body.items() if k not in ("content_sha256",)}
    a["provenance"] = {**a["provenance"], "generated_at": b["provenance"]["generated_at"]}
    if a != b:
        diff = [k for k in a if a[k] != b.get(k)]
        print(f"❌ {path.name} 与 run_meta 重算不一致，字段: {diff}"
              f"（名单漂了就先重导工件，别改代码里的常量）", file=sys.stderr)
        return 1
    print(f"✅ {path.name} 与 run_meta 逐字段一致（ρ={rho}、h={horizon}、"
          f"{body['counts']['union']} 条）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-meta", type=str, default=None,
                    help="某一轮 alpha158_ic 面板的 run_meta_*.json")
    ap.add_argument("--rho", type=float, default=0.8)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    ap.add_argument("--check", type=str, nargs="?", const=str(DEFAULT_OUT), default=None,
                    help="核对工件（默认核对 config/alpha158_union.json）")
    args = ap.parse_args(argv)

    if args.check is not None:
        return check(Path(args.check),
                     Path(args.run_meta) if args.run_meta else None,
                     args.rho, args.horizon)
    if not args.run_meta:
        ap.error("产出模式要带 --run-meta")
    body = build_union(Path(args.run_meta), args.rho, args.horizon)
    out = Path(args.out)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(str(tmp), str(out))  # 换 inode：与 write_table 同一套原子写语义
    print(f"写出 {out}：现役独立 {body['counts']['feat26_independent']} + "
          f"净新增 {body['counts']['alpha158_net_new']} = "
          f"并集 {body['counts']['union']} 条（ρ={args.rho}、h={args.horizon}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
