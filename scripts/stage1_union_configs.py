#!/usr/bin/env python3
"""stage1 双跑契约生成器：B0（基线 21）/ B1（21+净新增）/ B2（并集）全部程序化派生。

双跑比的就是一条轴——**特征集合**。所以三份契约不许有任何一份是手抄的：

- 基线 21 维与超参取自 hfq 冻结契约（`--base`，默认 2026-10-30 那次双跑的产物）；
- 净新增与现役独立维取自去重工件（`--union`，`config/alpha158_union.json`，
  由 `scripts/alpha158_union_list.py` 从面板 run_meta 产出，ρ=0.8、h=5）。

抄一份列名进 .py 的后果不是报错，而是**名单与工件来自不同时刻**：基线还是旧契约那 21 条、
加法侧却是新窗口重跑出的 47 条，于是差值同时掺了「窗口变化」和「装配口径变化」，读不出
「Alpha158 到底加了多少」。这条与 KI-DATA-005 是同一个坑的形状——形状对、值来自两套口径。

产物三份 JSON 与 `write_frozen_contract` 同形（`selected_features` + `hyperparams` 必备），
可以直接喂给 `scripts/stage1_retrain.py --config`；额外带 provenance，指回两份输入工件与
去重参数。生成器**不含时间戳**，重跑逐字复现，`--check` 因此能当幂等判据用。

用法：
    venv/bin/python scripts/stage1_union_configs.py --out-dir data/double_up
    venv/bin/python scripts/stage1_union_configs.py --out-dir data/double_up --check
    ... --wide-table data/double_up/stage1_wide_table_union47_hfq.parquet  # 用真宽表验列名
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = ROOT / "data" / "double_up" / "stage1_selected_features_hfq_20261030.json"
DEFAULT_UNION = ROOT / "config" / "alpha158_union.json"

# 契约 id → (人读的标签, 取哪几段名单)。顺序即拼接顺序：先基线/现役，再净新增，
# 这样 B1 的前 21 列与 B0 逐字同序，模型对比时列序变化不会混进差值。
CONTRACT_IDS = ("b0", "b1", "b2")


def _rel(path: Path) -> str:
    """仓库内路径写成相对串；仓库外（如 /tmp）原样返回，不抛。

    用 parents 判定而不是 try/except `relative_to`：这不是错误路径，语义守卫（SEM002）
    也不该把它记成吞错。
    """
    resolved = Path(path).resolve()
    if ROOT in resolved.parents:
        return str(resolved.relative_to(ROOT))
    return str(path)


def build_contracts(base: dict, union: dict,
                    known_cols: list[str] | None = None, *,
                    base_path: Path = DEFAULT_BASE,
                    union_path: Path = DEFAULT_UNION) -> dict[str, dict]:
    """由两份权威工件派生三份契约。任何一条名字解析不上都拒绝，不静默丢维。"""
    for key, where in (("selected_features", "base 契约"), ("hyperparams", "base 契约")):
        if key not in base:
            raise ValueError(f"{where}缺字段 {key}")
    for key in ("feat26_independent", "alpha158_net_new", "union"):
        if key not in union:
            raise ValueError(f"union 工件缺字段 {key}")

    base_feats = list(base["selected_features"])
    net_new = list(union["alpha158_net_new"])
    f26_indep = list(union["feat26_independent"])

    overlap = sorted(set(base_feats) & set(net_new))
    if overlap:
        raise ValueError(
            f"净新增与基线契约重叠 {overlap}——去重时基线应作 seed，工件侧再确认一次")

    labels = {
        "b0": f"基线：冻结契约的 {len(base_feats)} 维",
        "b1": f"纯加法：基线 {len(base_feats)} + Alpha158 净新增 {len(net_new)}",
        "b2": f"并集：现役独立 {len(f26_indep)} + Alpha158 净新增 {len(net_new)}",
    }
    feature_sets = {
        "b0": list(base_feats),
        "b1": list(base_feats) + net_new,
        "b2": list(f26_indep) + net_new,
    }

    out: dict[str, dict] = {}
    for cid in CONTRACT_IDS:
        feats = feature_sets[cid]
        dupes = sorted({f for f in feats if feats.count(f) > 1})
        if dupes:
            raise ValueError(f"契约 {cid} 内出现重复维 {dupes}")
        if known_cols is not None:
            missing = sorted(set(feats) - set(known_cols))
            if missing:
                raise ValueError(
                    f"契约 {cid} 有 {len(missing)} 条维不在宽表列中：{missing[:8]}")
        body = {
            "contract_id": cid,
            "label": labels[cid],
            "selected_features": feats,
            "n_features": len(feats),
            "n_alpha158_added": len(net_new) if cid != "b0" else 0,
            "hyperparams": copy.deepcopy(base["hyperparams"]),
            "objective": base.get("objective", "huber"),
            "alpha": base.get("alpha", 0.5),
            "target": base.get("target", "hypothetical_pnl"),
            "provenance": {
                "generator": _rel(Path(__file__)),
                "base_contract": _rel(base_path),
                "base_created": base.get("created"),
                "union_artifact": _rel(union_path),
                "union_content_sha256": union.get("content_sha256"),
                "dedup": union.get("dedup"),
                "n_baseline": len(base_feats),
                "n_net_new": len(net_new),
                "n_feat26_independent": len(f26_indep),
            },
            "note": ("双跑的唯一特征契约；由 stage1_union_configs.py 从 base+union 两份工件"
                     "派生，改名单请重跑生成器并 --check，不要手改本文件"),
        }
        if cid == "b1":
            body["provenance"]["arithmetic"] = f"{len(base_feats)} + {len(net_new)} = {len(feats)}"
        if cid == "b2":
            outside = sorted(set(f26_indep) - set(base_feats))
            body["provenance"]["feat26_outside_baseline"] = outside
            body["provenance"]["dropped_vs_baseline"] = sorted(set(base_feats) - set(f26_indep))
            body["note"] += ("；并集会剔掉基线里现役独立子集没选中的维"
                             f"（{len(body['provenance']['dropped_vs_baseline'])} 条），"
                             "所以它是诊断对照，不是加法对照")
        out[cid] = body
    return out


def _path_for(out_dir: Path, cid: str) -> Path:
    return out_dir / f"stage1_contract_{cid}.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    # mkstemp 固定 0600，而 `open(...,'w')` 走 umask（本机 0644）。契约文件若哪天落到
    # config/ 这类跟踪目录，权限差会变成看不见的行为差——写完显式回到 0644。
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="stage1 双跑契约生成器（B0/B1/B2）")
    p.add_argument("--base", type=str, default=str(DEFAULT_BASE),
                   help="基线冻结契约 JSON（selected_features + hyperparams）")
    p.add_argument("--union", type=str, default=str(DEFAULT_UNION),
                   help="去重并集工件（alpha158_net_new / feat26_independent）")
    p.add_argument("--out-dir", type=str, default=str(ROOT / "data" / "double_up"))
    p.add_argument("--wide-table", type=str, default=None,
                   help="给出就用真宽表的列名校验三份契约（缺维即非零）")
    p.add_argument("--check", action="store_true",
                   help="不写盘，核对现存产物与重算结果逐字一致")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    base_path, union_path = Path(args.base), Path(args.union)
    base = json.loads(base_path.read_text(encoding="utf-8"))
    union = json.loads(union_path.read_text(encoding="utf-8"))

    known_cols = None
    if args.wide_table:
        import pyarrow.parquet as pq
        known_cols = list(pq.ParquetFile(Path(args.wide_table)).schema_arrow.names)
    contracts = build_contracts(base, union, known_cols=known_cols,
                                base_path=base_path, union_path=union_path)

    out_dir = Path(args.out_dir)
    bad: list[str] = []
    for cid in CONTRACT_IDS:
        text = json.dumps(contracts[cid], ensure_ascii=False, indent=2) + "\n"
        path = _path_for(out_dir, cid)
        if args.check:
            if not path.exists():
                bad.append(f"{_rel(path)} 缺失")
            elif path.read_text(encoding="utf-8") != text:
                bad.append(f"{_rel(path)} 与重算结果不一致（手改或工件漂移）")
        else:
            _atomic_write(path, text)
            print(f"写出 {_rel(path)}  n_features={contracts[cid]['n_features']}"
                  f"  [{contracts[cid]['label']}]")
    if bad:
        print("❌ CHECK FAIL:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    if args.check:
        print(f"✅ {len(CONTRACT_IDS)} 份契约与重算逐字一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
