"""生产侧调 `get_limit` / `limit_threshold` 必须传 `on_date`（KI-DATA-008 的 B1 守卫）。

═══════════════════════════════════════════════════════════════════════════════
【钉什么】主板 ST/*ST 在 `MAIN_ST_RULE_CHANGE_DATE`（2026-07-06）之前是 ±5% 档，
判定值 0.045；`get_limit(code)` 不传 `is_st` 也不传 `on_date` 时恒按当日生效档（并轨后
=10%），于是并轨前的历史行按 10% 档打标签，涨幅落在 5% 档的真涨停被记成 y=0——中心档
少记 52,919 行、正样本低估 24.42%（取证：`docs/research/2026-11-05-daban-st-tier-label-pollution.md`，
复跑入口 `scripts/audit_daban_st_tier_pollution.py quantify --full`）。
本守卫把「生产侧传日期」变成可判的事，而不是口头约定。

═══════════════════════════════════════════════════════════════════════════════
【为什么判「在役闭包」而不是手工清单】手工清单的作弊面正好是用户点名的那一条：
**把生产文件登记成「研究脚本」凑绿**。所以生产面不由人写，由图算：

  根 = `systemd/*.service` 里 `ExecStart=` 指向的 `scripts/*.py`（全部单元，不只是
       enabled 的那些——多算只会更严，且 CI 里 `systemctl` 不可用，读文件是唯一
       确定性来源）∪ 现役模型生产者（`scripts/train_daban_model.py`、
       `scripts/train_pnl_proxy.py`）
  闭包 = 从根出发按 import 图可达的仓内 .py（`scripts/ tdxfeed/ quant/ mcp/`）

闭包内的每个 `get_limit` / `limit_threshold` **调用点**都必须带 `on_date=`（第 3 个
位置参数也算，见 `_passes_date`）。闭包外的调用点必须**逐条登记结论**，两层都是双向
闭合：出现未登记的命中 ⇒ 红；登记了却已无命中（登记腐化）⇒ 红。

2026-11-05 本批动手前实跑（`_closure_files()` 的原样输出）：闭包 44 个文件、闭包内
7 个调用点、其中 **4 个不传日期**——`scripts/train_daban_model.py:135`、
`tdxfeed/features_5d.py:148`、`quant/multifactor/factors.py:58`（那层壳的函数体）、
`quant/multifactor/factors.py:160`——与 KI-DATA-008 记的位置逐条相同，**图上没有第五个
漏网的**；闭包外 20 个调用点全部落在研究/基准/MCP 工具/重训侧，逐条登记在下面。

【防滑三条】① 闭包本身不许悄悄失能：`test_production_closure_is_not_blind` 要求根
解析得到、闭包非空、且必须包含 `tdxfeed/features_5d.py` 这个真调用点所在文件——否则
「图算错了/根名打错」和「生产面已经干净」长得一模一样。② 豁免清单不得盖住生产层：
`test_exempt_registry_does_not_cover_production_files`。③ 检测器本身要能抓到形状：
`test_control_*` 四条变异对照，全部改在 tmp 副本或内存字典上，不动仓库文件。

【第二维：dim1/dim15 的供给点】阈值传了日期还不够——现役 29 维里的 dim1（阈值）与
dim15（is_st）由 `compute_window_features*` 产出，调用点不传 `is_st=` 就等于仍在
「未供给」档。`test_inference_supply_sites_are_declared` 把生产闭包里这类调用点逐条登记：
2026-11-05 实测是 `scripts/predict.py:466`（在役推理）与 `scripts/find_d_signal_5d.py:105`
（旁路入口）两处未接，**归 B2 前置**（接它必须与重训同批，因为现役模型是 dim15≡0 训的）。
登记是双向闭合的：新出现的未登记供给点 ⇒ 红；接完了还挂着 ⇒ 红。

【行为中性是算术，不是承诺】`test_on_date_is_neutral_on_or_after_rule_change` 钉
「对 `on_date >= MAIN_ST_RULE_CHANGE_DATE` 的任意票，传 `is_st`/`on_date` 与不传结果
相等」——这正是"接了日期也不会改变今天的行"的原因（`limit_ratio` 只在主板且
`d < 并轨日` 时取 0.05）；反向由 `test_pre_rule_change_st_row_differs` 钉住"历史段
确实会变"。真数据上近 30 个交易日逐格比对在
`tests/test_st_supply_in_features.py`（数据依赖，登记在 `test_ci_data_skip.py`）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 本模块只读源码文本与 systemd 单元文本，不读行情产物；docstring 里提到的路径字样
# 会被根 conftest 的「无数据跳过」分类误判，故显式退出（且**不得**登记进
# tests/test_ci_data_skip.py::DATA_DEPENDENT_MODULES，那张表是等值判据）。
NEEDS_DATA = False

# 判定的函数名：判定阈值这一套合同（0.095/0.195/0.295），不是挂单价那套
# （`tdxfeed/daban/limit_price.py` 走 `limit_ratio`，本守卫不管它，管它是另一条线）。
JUDGED_FUNCS: frozenset[str] = frozenset({"get_limit", "limit_threshold"})

# import 图的扫描面。`v5/` 是历史线的独立解释器与脚本、`train/` 被 gitignore，
# 都不在现役 daban/stage1 的供给链上，故不入面（入面与否的判据由
# test_production_closure_is_not_blind 兜住：真漏了生产文件会红，不会静默）。
SCAN_DIRS: tuple[str, ...] = ("scripts", "tdxfeed", "quant", "mcp")
_EXCLUDED_PARTS: frozenset[str] = frozenset(
    {"_archive", "tests", "__pycache__", ".qoder", "venv", "venv-math", "venv-qlib", "node_modules"}
)

# 现役模型生产者：产物是被在役入口读的那只模型，所以它是生产根（即便没有 timer，
# 训练入口本身决定标签口径）。2026-11-05 现读：
#   scripts/train_daban_model.py -> data/double_up/d_signal_5d_model_daban.txt（现役 29 维）
#   scripts/train_pnl_proxy.py   -> stage1 系标签与模型
MODEL_PRODUCERS: tuple[str, ...] = (
    "scripts/train_daban_model.py",
    "scripts/train_pnl_proxy.py",
)

_UNIT_PY_RE = re.compile(r"ExecStart=.*?(scripts/[A-Za-z0-9_]+\.py)")

# 结论词表（登记即文档；每条要说清「为什么这一批不动它」）。
OUTSIDE_VERDICTS_CODE: dict[str, str] = {
    "retrain_side": "训练侧但未收编：只在 stage1/因子重训链上被调用，不在役 timer 闭包里；"
                    "本批范围是用户点名的四处，它归后续收编轮（残留可见，不是豁免）",
    "mcp_tool": "MCP 回测工具：由 MCP client 调用，不在 timer/训练闭包内，口径是历史回测；"
                "接线要连工具签名一起改，本批不动",
    "research": "研究/诊断脚本：一次性取证用，跑出来的数不进任何生产产物",
    "hist_baseline": "历史基准：脚本故意复刻「当时的口径」做纵向对比，改成按日供给就毁掉可比性",
    "deliberate_probe": "取证脚本里的对照项：它测的正是「不传日期会得到什么」，传了日期就不是它的判据了",
    "superseded": "已被同族的按日版本取代（现役那份已传 on_date），留着只为历史对账",
}

# 闭包外调用点的登记（键 = 仓内相对路径；行号由判据现读磁盘，不手抄）。
OUTSIDE_VERDICTS: dict[str, tuple[str, str]] = {
    "mcp/servers/backtest.py": ("mcp_tool", "748 行 `get_limit(sym)`：MCP 回测工具的阈值"),
    "scripts/audit_daban_st_tier_pollution.py": (
        "deliberate_probe", "98/99 行是「现役：不传 is_st/on_date」的基线读数，"
                            "100 行才是并轨前 ST 档——两条并存才是 +24.42% 的算式"),
    "scripts/compare_daban_vs_stage1.py": ("research", "daban vs stage1 横向对比的一次性脚本"),
    "scripts/daban_event_table.py": ("research", "事件表导出脚本"),
    "scripts/daban_label_diagnose.py": ("research", "标签诊断脚本"),
    "scripts/diagnose_sample_design.py": ("research", "样本设计诊断脚本"),
    "scripts/eval_daban_pattern.py": ("research", "形态评估脚本"),
    "scripts/factor_engine.py": ("retrain_side", "因子引擎（stage1 重训侧）109/347/404 三处"),
    "scripts/find_double_up_c_v2.py": ("superseded", "现役那份是 find_double_up_c.py（已传 on_date）"),
    "scripts/find_double_up_hfq.py": ("research", "hfq 口径探针脚本"),
    "scripts/top10_deep_dive.py": ("research", "Top10 复盘脚本"),
    "scripts/v3_premarket_top.py": ("hist_baseline", "v3 历史盘前脚本，钉当时口径"),
    "scripts/verify_daban_hit.py": ("research", "命中率复核脚本"),
    "tdxfeed/factors_polars.py": ("retrain_side", "89 行：stage1 宽表的 limit 维，被 "
                                                   "`tdxfeed/stage1_wide_table.py` 消费，"
                                                   "后者只在重训/研究脚本里被调用"),
}


# --------------------------------------------------------------------------- #
# 图：扫描面索引 → import 解析 → 从根求闭包
# --------------------------------------------------------------------------- #
def _iter_scan_files() -> list[Path]:
    out: list[Path] = []
    for rel in SCAN_DIRS:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if {q.name for q in p.parents} & _EXCLUDED_PARTS:
                continue
            out.append(p)
    return out


def _module_index() -> dict[str, Path]:
    """模块名 → 文件。三种键都建：`tdxfeed.features_5d`、`features_5d`、`_common.py`。

    `scripts/*.py` 靠 `sys.path.insert(ROOT/scripts)` 做**裸名** import
    （`from _common import get_limit`），所以按文件名与按点分名两种都要能解析，
    否则闭包会在第一条脚本级 import 处断掉——那正是「图算错了却像生产面干净」的形状。
    """
    idx: dict[str, Path] = {}
    for p in _iter_scan_files():
        rel = p.relative_to(ROOT)
        dotted = str(rel.with_suffix("")).replace("/", ".")
        idx.setdefault(dotted, p)
        idx.setdefault(rel.as_posix(), p)
        idx.setdefault(p.name, p)
        idx.setdefault(p.stem, p)
    return idx


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # 相对导入：按所在包目录折算
                pkg = path.parent
                for _ in range(node.level - 1):
                    pkg = pkg.parent
                tail = node.module.replace(".", "/") if node.module else ""
                cand = (pkg / tail) if tail else pkg
                names.add(cand.name)
            elif node.module:
                names.add(node.module)
                for a in node.names:              # `from pkg import mod` 也要能落到文件
                    names.add(f"{node.module}.{a.name}")
            elif node.names:
                for a in node.names:
                    names.add(a.name)
    return names


def _resolve(name: str, idx: dict[str, Path]) -> Path | None:
    if name in idx:
        return idx[name]
    tail = name.rsplit(".", 1)[-1]
    return idx.get(tail)


def timer_entry_scripts() -> list[str]:
    """`systemd/*.service` 的 `ExecStart` 里指向的仓内脚本（模板里的 `@TDX_HOME@` 不影响）。"""
    out: set[str] = set()
    for unit in sorted((ROOT / "systemd").glob("*.service")):
        out |= set(_UNIT_PY_RE.findall(unit.read_text(encoding="utf-8", errors="ignore")))
    return sorted(out)


def production_roots() -> list[str]:
    roots = [rel for rel in (*timer_entry_scripts(), *MODEL_PRODUCERS) if (ROOT / rel).is_file()]
    missing = [rel for rel in (*timer_entry_scripts(), *MODEL_PRODUCERS) if rel not in roots]
    assert not missing, f"生产根里有解析不到的文件，闭包会漏算这一支: {missing}"
    return roots


def _closure_files() -> set[Path]:
    idx = _module_index()
    seen: set[Path] = set()
    queue = [ROOT / rel for rel in production_roots()]
    while queue:
        p = queue.pop()
        if not p.is_file() or p in seen:
            continue
        seen.add(p)
        for imp in _imports_of(p):
            target = _resolve(imp, idx)
            if target is not None:
                queue.append(target)
    return seen


def production_files() -> set[str]:
    """闭包内文件的仓内相对路径集合（豁免清单不许盖住它，见对应判据）。"""
    return {p.relative_to(ROOT).as_posix() for p in _closure_files()}


# --------------------------------------------------------------------------- #
# 调用点检测
# --------------------------------------------------------------------------- #
def _func_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _passes_date(node: ast.Call) -> bool:
    """带 `on_date=` 才算带日期；`get_limit(code, name, d)` 的第 3 个位置参数同样算。

    位置参数那条不是宽容，是必要：`features_5d.get_limit(code, name="", on_date=None)`
    的真签名允许第三种写法，只认关键字的话「改成位置传参」就能绕过守卫（形状与
    `tests/test_no_hardcoded_data_paths.py` 的 PATH_CALLS 同一类防滑）。
    """
    if any(k.arg == "on_date" for k in node.keywords):
        return True
    return _func_name(node) == "get_limit" and len(node.args) >= 3


def call_sites(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and _func_name(n) in JUDGED_FUNCS]


def detected_sites(files: set[str]) -> dict[str, list[tuple[int, bool]]]:
    out: dict[str, list[tuple[int, bool]]] = {}
    for rel in sorted(files):
        path = ROOT / rel
        if not path.is_file():
            continue
        hits = [(n.lineno, _passes_date(n)) for n in call_sites(path)]
        if hits:
            out[rel] = hits
    return out


def format_limit_date_inventory() -> str:
    """把「生产面 / 闭包外」两层调用点排成可读清单（行号现读磁盘）。"""
    prod = production_files()
    rows: list[str] = []
    for rel, hits in detected_sites(prod).items():
        for lineno, ok in hits:
            rows.append(f"[生产] {rel}:{lineno}\t{'已传 on_date' if ok else '未传 on_date'}")
    for rel, hits in sorted(detected_sites(set(OUTSIDE_VERDICTS) - prod).items()):
        code, note = OUTSIDE_VERDICTS[rel]
        for lineno, ok in hits:
            rows.append(f"[闭包外] {rel}:{lineno}\t{code}\t"
                        f"{'已传 on_date' if ok else '未传'}｜{note}")
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #
def test_production_closure_is_not_blind() -> None:
    """闭包失能时「生产面干净」是假的：根必须解析到、闭包必须含已知的供给文件。"""
    roots = production_roots()
    assert len(roots) >= 10, f"生产根只剩 {len(roots)} 个（systemd 单元或生产者路径变了？）: {roots}"
    prod = production_files()
    assert prod, "闭包算出 0 个文件 ⇒ import 图解析失效，本守卫现在什么都判不到"
    must_be_in = {
        "scripts/train_daban_model.py",   # 现役 daban 标签生产者
        "tdxfeed/features_5d.py",         # 18/26/29 维特征供给（predict 与训练共用同一函数）
        "quant/multifactor/factors.py",   # 经 quant.multifactor.__init__ 被在役入口拉到
    }
    absent = sorted(must_be_in - prod)
    assert not absent, (
        f"这些生产文件本该在闭包内却不在 ⇒ 图或扫描面缩小，守卫对它们变聋了: {absent}"
    )
    # 反向：`from _common import get_limit` 这种裸名 import 必须解析得动，
    # 否则整条 scripts/ 链在第一跳就断（2026-11-05 实测闭包含 scripts/_common.py）。
    assert "scripts/_common.py" in prod, "scripts/_common.py 没进闭包 ⇒ 裸名 import 解析失效"


def test_production_call_sites_pass_on_date() -> None:
    """闭包内每个 get_limit/limit_threshold 调用点必须带 on_date —— 本批动手前它是红的。"""
    offenders = [
        f"{rel}:{lineno}"
        for rel, hits in detected_sites(production_files()).items()
        for lineno, ok in hits if not ok
    ]
    assert not offenders, (
        "生产侧（在役 timer 闭包 ∪ 模型生产者）调阈值不传交易日 ⇒ 并轨日之前的主板 ST 行"
        "按 10% 档打标签/供给特征（KI-DATA-008）。未收编的调用点：\n  "
        + "\n  ".join(sorted(offenders))
        + "\n（完整两层清单见 format_limit_date_inventory()）"
    )


def test_outside_sites_are_exactly_registered() -> None:
    """闭包外的**未传日期**调用点必须逐条登记（新漏网红），登记过的必须仍是 offender
    （收编完了还挂着 = 登记腐化红）。

    口径要说清：登记的对象是「违规且这一批不动」的调用点，不是所有闭包外调用点。
    已经按日供给的（`tdxfeed/st_history.py:717/718` 的翻转计数、
    `scripts/audit_daban_st_tier_pollution.py:100` 的并轨前 ST 档）不需要登记，
    否则每修一处都要改两次清单，清单迟早漂。
    """
    prod = production_files()
    all_sites = detected_sites({p.relative_to(ROOT).as_posix() for p in _iter_scan_files()})
    offenders = {rel for rel, hits in all_sites.items() if rel not in prod
                 and any(not ok for _lineno, ok in hits)}
    unregistered = sorted(offenders - set(OUTSIDE_VERDICTS))
    stale = sorted(set(OUTSIDE_VERDICTS) - offenders)
    detail = "; ".join(
        f"{rel}:{next(ln for ln, ok in all_sites[rel] if not ok)}" for rel in unregistered)
    assert not unregistered, (
        f"闭包外出现未登记的阈值调用点。它到底是生产文件还是研究脚本？判完再按结论词表登记: {detail}"
    )
    assert not stale, (
        f"这些登记项已经没有违规命中（登记腐化——收编完了就把登记删掉，留着会让下一个人以为还没修）: {stale}"
    )


def test_exempt_registry_does_not_cover_production_files() -> None:
    """把生产文件写进豁免清单就是作弊：闭包能算出来的事，不许由人把它说成非生产。"""
    prod = production_files()
    covered = sorted(set(OUTSIDE_VERDICTS) & prod)
    assert not covered, (
        f"这些文件在役闭包内，却被登记成「闭包外/非生产」来凑绿：{covered}\n"
        "正确做法是把调用点改成传 on_date（并按需接 ST 供给），然后从本清单里删掉登记。"
    )


def test_verdict_vocabulary_is_closed() -> None:
    """结论词表与登记双向闭合：词表里不许挂空结论，登记也不许用词表外的码。"""
    codes = {code for code, _note in OUTSIDE_VERDICTS.values()}
    unknown = sorted(codes - set(OUTSIDE_VERDICTS_CODE))
    assert not unknown, f"登记用了词表外的结论码: {unknown}"
    unused = sorted(set(OUTSIDE_VERDICTS_CODE) - codes)
    assert not unused, f"结论词表里有从未被使用的档位（词表该收口）: {unused}"


# --------------------------------------------------------------------------- #
# 第二维：生产闭包里的**特征供给点**必须传 is_st（推理侧未接是登记出来的残留）
# --------------------------------------------------------------------------- #
FEATURE_FUNCS: frozenset[str] = frozenset({"compute_window_features",
                                           "compute_window_features_extended"})

# 现役 dim1（阈值）/ dim15（is_st）由 `features_5d.compute_window_features*` 产出。
# 生产闭包里调它却不传 `is_st=` 的点 ⇒ 那一侧仍在「未供给」档（等价于旧的恒 0.095/恒 0）。
# 这一维不是「顺手加的」：训练侧接了而推理侧没接，重训后的 dim1/dim15 就会与在役供给口径
# 不一致（现役模型是 dim15≡0 训的，所以现在不接也不会立刻坏；**B2 重训前必须一起接**）。
SUPPLY_VERDICTS: dict[str, str] = {
    "b2_prerequisite": "在役推理侧：接它要同时处理「现役模型是 dim15≡0 训练的」这件事，"
                       "所以必须与重训同批做（B2），本批不动在役入口",
    "legacy_entry": "历史/旁路入口：不在 enabled timer 上，复用前必须显式供给",
}

UNWIRED_SUPPLY_SITES: dict[str, tuple[str, str]] = {
    "scripts/predict.py": ("b2_prerequisite", "466 行 `compute_window_features_extended(...)` "
                                              "未传 is_st ⇒ 在役推理仍是未供给档（B2 前置）"),
    "scripts/find_d_signal_5d.py": ("legacy_entry", "105 行：d 信号回溯脚本，被 predict 侧"
                                                   "import 但不在 timer 链上"),
}


def supply_sites(files: set[str]) -> dict[str, list[tuple[int, bool]]]:
    out: dict[str, list[tuple[int, bool]]] = {}
    for rel in sorted(files):
        path = ROOT / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        hits: list[tuple[int, bool]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = _func_name(node)
            if fn in FEATURE_FUNCS:
                hits.append((node.lineno, any(k.arg == "is_st" for k in node.keywords)))
        if hits:
            out[rel] = hits
    return out


def test_inference_supply_sites_are_declared() -> None:
    """生产闭包里的特征供给点要么已传 is_st，要么逐条登记为 B2 前置残留（双向闭合）。"""
    sites = supply_sites(production_files())
    offenders = {rel for rel, hits in sites.items() if any(not ok for _ln, ok in hits)}
    unregistered = sorted(offenders - set(UNWIRED_SUPPLY_SITES))
    stale = sorted(set(UNWIRED_SUPPLY_SITES) - offenders)
    assert not unregistered, (
        f"生产闭包出现未按日供给 ST 的特征调用点且未登记：{unregistered}。"
        "接上 `is_st=`，或说明它为什么不算生产"
    )
    assert not stale, (
        f"这些推理/供给点已经按日供给 ST 了，登记要删（留着会让下一个人以为还没接）: {stale}"
    )
    codes = {code for code, _n in UNWIRED_SUPPLY_SITES.values()}
    assert codes <= set(SUPPLY_VERDICTS), f"用了词表外的结论码: {sorted(codes - set(SUPPLY_VERDICTS))}"
    unused = sorted(set(SUPPLY_VERDICTS) - codes)
    assert not unused, f"结论词表里有从未被使用的档位: {unused}"


# --------------------------------------------------------------------------- #
# 行为中性是算术（无数据依赖，CI 也判）
# --------------------------------------------------------------------------- #
def test_on_date_is_neutral_on_or_after_rule_change() -> None:
    """判据：`on_date >= MAIN_ST_RULE_CHANGE_DATE` 时，传 is_st/on_date 与不传逐格相等。

    这条是「接线不会改变今天的行为」的全部依据，也是近 30 个交易日逐格比对的算术底座：
    `limit_ratio` 只在**主板**且 `d < 并轨日` 时取 0.05，其余分支与 is_st/on_date 无关。
    覆盖三种板（主板/双创/北交所）× is_st 真假 × 并轨日当天与其后若干天。
    """
    from datetime import timedelta

    from tdxfeed.execution.limits import MAIN_ST_RULE_CHANGE_DATE, limit_threshold

    codes = ("600000", "sh600000", "sz000001", "300750", "sz301111", "sh688981",
             "920001", "bj830799", "430047")
    days = [MAIN_ST_RULE_CHANGE_DATE,
            *(MAIN_ST_RULE_CHANGE_DATE + timedelta(days=k) for k in (1, 3, 30, 120, 400))]
    for code in codes:
        plain = limit_threshold(code)
        for d in days:
            for is_st in (False, True):
                assert limit_threshold(code, is_st=is_st, on_date=d) == plain, (
                    f"{code} 在 {d}（并轨日或之后）传 is_st={is_st} 与不传不相等 ⇒ "
                    "今天的行会被这一批改坏，行为中性破了")


def test_pre_rule_change_st_row_differs() -> None:
    """反向钉住「历史段确实允许变」：并轨前主板 ST 的判定值是 0.045，不是 0.095。

    没有这条，上一条的「处处相等」可能被读成「这一改根本不动任何东西」。
    """
    from datetime import date

    from tdxfeed.execution.limits import limit_threshold

    assert limit_threshold("600000") == 0.095
    assert limit_threshold("600000", is_st=True, on_date=date(2024, 1, 2)) == 0.045
    # 双创没有单独的 ST 档：并轨前也不该被改成 5%
    assert limit_threshold("300750", is_st=True, on_date=date(2024, 1, 2)) == 0.195


# --------------------------------------------------------------------------- #
# 变异对照（全部改在 tmp 副本 / 内存对象上，不动仓库文件）
# --------------------------------------------------------------------------- #
def test_control_detector_flags_a_site_without_on_date(tmp_path: Path) -> None:
    """对照①「删掉 on_date 仍判绿」：把真实形状写成临时文件，检测器必须判它未传。"""
    probe = tmp_path / "probe_train.py"
    probe.write_text(
        "from tdxfeed.features_5d import get_limit\n"
        "limit_val = get_limit(code)\n"
        "lim = get_limit(code, name, on_date=sig_date)\n"
        "other = get_limit(code, name, sig_date)\n",     # 第 3 个位置参数也算传了日期
        encoding="utf-8",
    )
    hits = [(n.lineno, _passes_date(n)) for n in call_sites(probe)]
    assert hits == [(2, False), (3, True), (4, True)], hits


def test_control_production_file_cannot_be_moved_into_exempt(tmp_path: Path, monkeypatch) -> None:
    """对照②「清单盖住非生产层/把生产文件挪进豁免」：生产文件出现在豁免集必须红。

    这里用 monkeypatch 把 `production_files()` 换成「含某登记项」的假集合，
    等价于「有人把生产文件写成非生产」——判据必须抓到它，而不是安静放行。
    """
    victim = "tdxfeed/factors_polars.py"
    assert victim in OUTSIDE_VERDICTS, f"对照要用一条真登记项，{victim} 已不在清单里"
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "production_files", lambda: {victim, "tdxfeed/features_5d.py"})
    try:
        test_exempt_registry_does_not_cover_production_files()
    except AssertionError as exc:
        assert victim in str(exc), exc
    else:
        raise AssertionError("把生产文件登记成非生产却没有判红 ⇒ 豁免清单这道闸是空的")


def test_control_stale_registration_is_red(monkeypatch) -> None:
    """对照③「登记腐化」：修好了还挂着登记，必须红。

    做法是内存里加一条指向不存在文件的登记（等价于「那处调用点已被收编」）。
    """
    mod = sys.modules[__name__]
    monkeypatch.setitem(mod.OUTSIDE_VERDICTS, "scripts/not_a_real_site.py",
                        ("research", "这条是变异对照造的"))
    try:
        mod.test_outside_sites_are_exactly_registered()
    except AssertionError as exc:
        assert "登记腐化" in str(exc), exc
    else:
        raise AssertionError("登记了却不存在的调用点没有判红 ⇒ 腐化检测是空的")


def test_control_import_graph_breaking_is_caught(monkeypatch) -> None:
    """对照④：import 图一断，闭包只剩根自己 ⇒ 必须被「不聋」那条抓到，而不是安静变绿。

    这是最真实的失效形状：`_imports_of` 因为语法/异常改动返回空集时，`features_5d.py`
    不再进闭包，于是它 :148 那处违规会被算成「闭包外的登记项」，本守卫会**当场变绿**——
    除非有这条绊线。
    """
    mod = sys.modules[__name__]
    monkeypatch.setattr(mod, "_imports_of", lambda path: set())
    try:
        mod.test_production_closure_is_not_blind()
    except AssertionError as exc:
        assert "守卫对它们变聋了" in str(exc), exc
    else:
        raise AssertionError("import 图断了却没有判红 ⇒ 闭包失能会伪装成『生产面已干净』")
