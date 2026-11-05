"""MODEL_REGISTRY 产物在场性守卫（A 线）：默认链零豁免 + 缺失登记双向闭合。

教师模型 tag 的写死守卫（B 线）在 `tests/test_teacher_tag_hardcoding.py`。
**别把两条合回一个文件**：根 conftest 的「无 `data/` 即收集期跳过」是按模块生效的，而本
文件必须读 `data/double_up/` 下的真实模型产物，只能整体 `NEEDS_DATA = True`。合在一起的
实测形状是 `TDX_TEST_DATA_ROOT=/nonexistent` 下 13 条全部 skip（退出码 0）——「新的教师
tag 写死点会被 CI 拦下」这条价值在 CI 里静默。拆开是为了让 B 线在 CI 真跑，不是风格问题。

═══════════════════════════════════════════════════════════════════════════════
【A · 钉三件事】注册表条目一律从 `predict.MODEL_REGISTRY` 现读：本文件不维护第二份
「键 → 文件名」映射（那是第二权威源，且与 tests/test_predict_price_basis.py 重叠）。

A1 默认链无豁免：`DEFAULT_MODEL`、`FALLBACK_MODEL`、`POSITION_MODE_MODEL` 的两个值，
   对应产物必须在场。这条判据**不带**白名单、不带跳过口子。为什么要钉它：2026-11-04
   之前没有任何东西钉「默认链的产物在不在场」。`data/double_up/d_signal_5d_model_daban.txt`
   一旦被误删或改名，18:05 的 `predict-daily` 只会走 `resolve_model` 里的静默回退
   （scripts/predict.py 打一行 `[WARN] 自动回退到 vpnl_huber_stage1`），出票照样生成，
   但排序模型已经换了一只（29 维分类 → 21 维回归），产物里看不出来。
A2 其余键：产物必须在场，除非登记在 `KNOWN_MISSING_MODEL_ARTIFACTS`（值 = 理由）。
A3 双向防腐化（形状照 tests/test_skill_runtime_prereqs.py 的同一套判据）：
   登记了但文件已在场 ⇒ 红（缺陷其实已经不在了，登记该删）；登记的键必须是注册表里
   真实存在的键（不许挂空名）；登记不得覆盖默认链（否则 A1 的「无豁免」能被登记绕过）；
   理由不得为空（登记即文档，空理由等于没登记）。

【为什么 "v9" 不能直接删】
`MODEL_REGISTRY["v9"]` 指向 `d_signal_5d_model_v9.txt`，2026-11-04 实测不在场，但它有
生产者：`scripts/feature_engineering.py:50` 的 `MODEL_OUT` 就是这份文件（第 248 行
`booster_.save_model(str(MODEL_OUT))`）。所以它是「有生产者、产物当前未生成」的合法
占位，不是手误。同时 `tests/test_predict_price_basis.py` 的 `OLD_ENTRIES` 已钉住这条
键，删键会让那条既有用例 KeyError。登记而不删除。

【四条永久变异对照】A1 / A2 / A3 各一条，外加「给默认链开后门」一条：判据在内存里被喂进
缺陷状态时必须报事——没有它们，「绿」和「判据根本没在判事」的绿长得一模一样。对照只改
`predict.MODEL_REGISTRY` 的条目与登记表（monkeypatch 负责还原），`data/` 里一个真文件都不动。

【数据依赖】本模块的判据要读 `data/double_up/` 下的真实产物（路径由注册表间接给出），
CI 无 `data/` 时整体跳过，因此写 `NEEDS_DATA = True` 并登记在
`tests/test_ci_data_skip.py::DATA_DEPENDENT_MODULES`（那张表是等值判据）。B 线的模块
**不得**登记进去——它不读 `data/`，登记进那张表等于在 CI 里把它关掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import predict  # noqa: E402

# 间接数据依赖：被测对象是 predict 的注册表产物（data/double_up/*.txt），本文件的
# 源码里没有 data/ 字面量，靠这个显式标记加入「无数据即收集期跳过」的集合。
NEEDS_DATA = True

# ────────────────────────────── A · 注册表产物 ──────────────────────────────

# 已登记的缺失产物：键 = MODEL_REGISTRY 的键，值 = 「为什么允许它暂时不在场」。
KNOWN_MISSING_MODEL_ARTIFACTS: dict[str, str] = {
    "v9": (
        "生产者 scripts/feature_engineering.py:50 的 MODEL_OUT 就是 d_signal_5d_model_v9.txt"
        "（第 248 行 save_model 落盘）；2026-11-04 实测该产物当前不在场，属「有生产者、"
        "产物未生成」的合法占位，非缺陷。重跑 feature_engineering 后本条登记必须删除。"
    ),
}


def _default_chain() -> tuple[str, ...]:
    """默认链的模型键（从 predict 现读，去重保序）。"""
    names: list[str] = []
    for name in (
        predict.DEFAULT_MODEL,
        predict.FALLBACK_MODEL,
        *predict.POSITION_MODE_MODEL.values(),
    ):
        if name not in names:
            names.append(name)
    return tuple(names)


def _artifact_of(name: str) -> Path:
    """注册表条目 → 产物 Path；条目形状不对就直接 raise，不做兜底。"""
    entry = predict.MODEL_REGISTRY[name]
    if not isinstance(entry, tuple) or len(entry) != 3:
        raise AssertionError(
            f"MODEL_REGISTRY[{name!r}] 不是 (path, kind, basis) 三元组：{entry!r}"
        )
    return Path(entry[0])


def _default_chain_failures() -> list[str]:
    """A1：默认链的产物缺失，或引用了注册表里没有的键。"""
    failures: list[str] = []
    for name in _default_chain():
        if name not in predict.MODEL_REGISTRY:
            failures.append(f"默认链引用了注册表里没有的键: {name}")
            continue
        path = _artifact_of(name)
        if not path.exists():
            failures.append(f"默认链产物缺失: {name} -> {path}")
    return failures


def _other_entry_failures() -> tuple[list[str], list[str]]:
    """A2 + A3：返回（未登记的缺失, 登记了却已在场的腐化）。默认链由 A1 单独判。"""
    chain = set(_default_chain())
    unregistered: list[str] = []
    stale: list[str] = []
    for name in predict.MODEL_REGISTRY:
        if name in chain:
            continue
        exists = _artifact_of(name).exists()
        if name in KNOWN_MISSING_MODEL_ARTIFACTS:
            if exists:
                stale.append(
                    f"{name} -> {_artifact_of(name)} 已在场，登记项该删（登记腐化）"
                )
        elif not exists:
            unregistered.append(
                f"{name} -> {_artifact_of(name)} 不存在，且没有登记理由"
            )
    return unregistered, stale


def test_default_chain_artifacts_are_present_with_no_exemption() -> None:
    """A1：默认链（DEFAULT_MODEL / FALLBACK_MODEL / POSITION_MODE_MODEL）产物全在场。"""
    failures = _default_chain_failures()
    assert not failures, (
        "默认链的模型产物缺失——predict.py 会静默回退到 FALLBACK_MODEL，"
        "出票照常生成但排序模型已换，产物里看不出来：\n" + "\n".join(failures)
    )


def test_non_default_registry_entries_are_present_or_registered() -> None:
    """A2 + A3 前半：未登记的缺失 ⇒ 红；已登记但已在场 ⇒ 也红。"""
    unregistered, stale = _other_entry_failures()
    assert not stale, (
        "KNOWN_MISSING_MODEL_ARTIFACTS 里有登记已经腐化（产物其实已经在了）：\n"
        + "\n".join(stale)
    )
    assert not unregistered, "MODEL_REGISTRY 里有产物的文件不在场：\n" + "\n".join(unregistered)


def _registration_specificity_failures() -> list[str]:
    """登记表自身的形状判据：键真实存在、不覆盖默认链、理由非空。"""
    failures: list[str] = []
    for name in sorted(set(KNOWN_MISSING_MODEL_ARTIFACTS) - set(predict.MODEL_REGISTRY)):
        failures.append(f"登记了 MODEL_REGISTRY 里并不存在的键（挂空名）: {name}")
    for name in sorted(set(KNOWN_MISSING_MODEL_ARTIFACTS) & set(_default_chain())):
        failures.append(
            f"默认链的键被写进了登记表（A1 无豁免，登记绕不过去）: {name}"
            f"；默认链 = {_default_chain()}"
        )
    for name, reason in sorted(KNOWN_MISSING_MODEL_ARTIFACTS.items()):
        if not reason.strip():
            failures.append(f"登记项没有写理由（登记即文档）: {name}")
    return failures


def test_registration_keys_are_live_and_specific() -> None:
    """A3 后半：登记项必须挂得住——键真实存在、不覆盖默认链、理由非空。"""
    failures = _registration_specificity_failures()
    assert not failures, "KNOWN_MISSING_MODEL_ARTIFACTS 自身的形状有问题：\n" + "\n".join(failures)


def test_control_a1_missing_default_artifact_is_reported(
    monkeypatch, tmp_path: Path
) -> None:
    """变异对照 A1：把 daban 的产物路径指到不在场的名字，判据必须报缺失。

    不动 data/ 里的真文件——只改内存里的注册表条目（monkeypatch 负责还原）。
    没有这条对照，「A1 绿」可能只是「判据根本没在判事」。
    """
    fake = tmp_path / "no_such_daban_model.txt"
    monkeypatch.setitem(predict.MODEL_REGISTRY, "daban", (fake, "cls", "raw"))
    reported = _default_chain_failures()
    assert reported, "daban 产物被改成不在场，A1 判据却没报缺失 ⇒ 判据形同虚设"
    assert any("daban" in line for line in reported), reported


def test_control_a2_unregistered_missing_artifact_is_reported(
    monkeypatch, tmp_path: Path
) -> None:
    """变异对照 A2：把一条未登记的键（prod）指到不在场的名字 ⇒ 必须进「未登记的缺失」。"""
    fake = tmp_path / "no_such_prod_model.txt"
    monkeypatch.setitem(predict.MODEL_REGISTRY, "prod", (fake, "cls", "raw"))
    unregistered, stale = _other_entry_failures()
    assert any(line.startswith("prod ") for line in unregistered), (
        f"未登记的缺失产物没被 A2 抓到: unregistered={unregistered} stale={stale}"
    )


def test_control_a3_registered_but_present_artifact_is_reported(
    monkeypatch, tmp_path: Path
) -> None:
    """变异对照 A3：让登记过的 v9 指向一个真实在场的文件 ⇒ 必须报登记腐化。"""
    present = tmp_path / "suddenly_built_model.txt"
    present.write_text("booster\n", encoding="utf-8")
    monkeypatch.setitem(predict.MODEL_REGISTRY, "v9", (present, "cls", "raw"))
    unregistered, stale = _other_entry_failures()
    assert any("v9" in line for line in stale), (
        f"v9 已在场却还挂着登记，A3 没报腐化: stale={stale} unregistered={unregistered}"
    )


def test_control_registration_cannot_exempt_the_default_chain(monkeypatch) -> None:
    """变异对照 A3-键：给默认链开后门（把 daban 写进登记表），判据必须报红。

    没有这条对照，「A1 无豁免」只是文档措辞——谁都能靠加一行登记把它悄悄关掉。
    """
    monkeypatch.setitem(KNOWN_MISSING_MODEL_ARTIFACTS, "daban", "试图给默认链开后门")
    failures = _registration_specificity_failures()
    assert any("daban" in line for line in failures), (
        f"默认链的键被登记时，越权检查没报红: {failures}"
    )
