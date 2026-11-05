"""教师模型 tag 的写死守卫：在役入口零字面量 + 仓库/本机两层清单闭合（B 线）。

═══════════════════════════════════════════════════════════════════════════════
【为什么不和 A 线放一个文件】这不是风格问题，是 CI 覆盖面问题。
根 `conftest.py` 的「无 `data/` 即收集期跳过」是**按模块**生效的，而
`tests/test_predict_registry_artifacts.py` 的判据必须读 `data/double_up/` 下的真实模型
产物，只能整体 `NEEDS_DATA = True`。本文件的判据只读源码文本、一点 `data/` 都不碰——
放在一起就等于让「新的写死点会被 CI 拦下」这条价值在 CI 里静默。2026-11-04 实测：
合并版在 `TDX_TEST_DATA_ROOT=/nonexistent` 下 **13 条全部 skip**（退出码 0，本机绿、
CI 什么都不判）。所以拆成两个模块，两边 docstring 互相指认——**别合回去**。
反向约束：本模块**不得**登记进 `tests/test_ci_data_skip.py::DATA_DEPENDENT_MODULES`
（那张表是等值判据，多一条也红），也**不写** `NEEDS_DATA`。

═══════════════════════════════════════════════════════════════════════════════
【B · 钉什么】两条模型版本线撞名（LightGBM 线的 v8/v9 与 LLM/LoRA 线的 v7r1115
无关），源码里写死的教师 tag 一律不可信（AGENTS.md「模型与蒸馏」）。

B1 实测（2026-11-04 现跑）：5 条在役业务链脚本（predict.py / daban_entry.py /
   plan_e_backtest.py / plan_e_journal.py / find_double_up_c.py，取自 enabled timer 的
   `ExecStart`）对 `11434|ollama` grep 每只都 0 命中、整条命令退出码 1 —— 在役路径里
   没有一条写死 Ollama tag。
   （enabled timer 里唯一还带 `qwen3-14b-tdx` 字样的入口是 trainsh-health 跑的
   `scripts/train_health.sh`，那 6 处全是 `find -type d -name "qwen3-14b-tdx-*"` 这种
   训练产物目录名 glob，`11434|ollama` 命中 0；排除它的理由写在 `LIVE_TIMER_ENTRY_SCRIPTS`
   上方注释里，是显式排除而不是漏扫。）
   所以本守卫**不要求**把它们改成动态解析：`v5/scripts/`、`training/`、`train/`、
   `Makefile` 下那些钉死历史 tag 的脚本是 v4/v5/v6/v7/v8/v9 纵向对比的基准，改成运行时
   解析就毁掉可比性，属非目标。

B2 只做两件事：
   ① `test_live_timer_entries_do_not_hardcode_teacher_tag`：扫描在役入口清单
      （`LIVE_TIMER_ENTRY_SCRIPTS`，来源 = enabled timer 的 `ExecStart`，2026-11-04
      现读 `systemd/*.service`），出现形如 `qwen3-14b-tdx*` 的字符串字面量即判红。
   ② `TEACHER_TAG_SITES` / `TEACHER_TAG_SITES_LOCAL`：把其余写死该 tag 的既有位置逐条
      登记成清单（文件 → 结论 + 理由），行号与命中的 tag 由判据**现读磁盘**产出
      （`format_teacher_tag_inventory()`），不手抄，所以清单不会因行号漂移而说谎。
      两层各自双向闭合：出现未登记的命中 ⇒ 红；登记了但已无命中 ⇒ 红。

═══════════════════════════════════════════════════════════════════════════════
【清单的口径：两层，别混】
①**仓库层** `_TAG_SCAN_ROOTS` + `_TAG_SCAN_FILES`：这些根逐个实测都被 git 跟踪
   （`git ls-files scripts`=186 / `v5`=268 / `training`=39 …，`train`=0），所以 CI 检出里
   也在，CI 里真判。**`training/` 与 `train/` 是仓库根下两个不同的目录**，都真实存在。
②**本机层** `_TAG_LOCAL_SCAN_ROOTS = ("train",)`：`.gitignore` 把 `train/` 整个排除
   （实测第 48 行 `train/`），`git ls-files train` = 0 条 ⇒ CI 检出里根本没有这个目录。
   而 v8/v9 的训练、评估、merge、部署脚本恰好全在 `train/scripts/` 下——正是「v8/v9 用的
   是哪个」那条线，所以它**必须**进清单；但它不能在 CI 里判。处理方式：单独成表、单独
   闭合，目录在场时逐条等值判（`test_local_tag_inventory_is_closed_when_the_dir_exists`），
   不在场时无货可判。这是**声明出来的残留覆盖缺口**，不是漏扫，也不是自跳过（本模块没有
   任何 `pytest.skip`，因此不进 `tests/test_machine_state_skips.py` 那张表）。
   防「以后有人把 train/ 收进 git 却让它还留在本机层」的绊线是
   `test_local_tier_roots_are_really_gitignored`：它现读 `.gitignore`（这文件本身在 git
   里，CI 也判得了）核对每个本机层根确实被忽略。

【图案】只认 `qwen3-14b-tdx*`。`qwen3:14b` 这类**基座模型名**不在本清单口径内——本机
在场、由 `DEFAULT_CHAT_MODEL`/`OLLAMA_MODEL` 一类环境变量覆盖，且与「微调版本线撞名」
这条风险无关（写死它的是 `v5/mcp_servers/*`）。排除任何路径段是 `_archive`（归档不用于
推断现状）、`tests`（守卫自己要在断言里写这些字符串）、各种缓存目录。

【B3 · 不做运行时 tag 解析器】不新建「读 /api/tags 的解析模块」。理由：在役路径零命中，
没有消费者；`scripts/distill_outputs.py` 的 tag 取自必填的 `TDX_DISTILL_MODEL`
（`Makefile` 的 `distill` 目标不传 `MODEL` 就 exit 2），`scripts/canary_health_check.py`
已经在读 GET /api/tags 做健康检查——这两处已经是收口后的形状。凭空加一个解析模块属于
无人调用的新权威源，本次不做。A 线的注册表守卫见 `tests/test_predict_registry_artifacts.py`。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 在役入口清单：来源 = `systemctl --user list-timers --all` 里 enabled 的仓库 timer
# 的 `ExecStart`（2026-11-04 现读 systemd/*.service）：
#   predict-daily.timer     -> ExecStart=… scripts/predict.py
#   daban-ticket.timer      -> ExecStart=… scripts/daban_entry.py
#   plan-e-today.timer      -> ExecStart=… scripts/plan_e_backtest.py
#   plan-e-journal.timer    -> ExecStart=… scripts/plan_e_journal.py
#   plan-e-double-up.timer  -> ExecStart=… scripts/find_double_up_c.py
# 其余 enabled timer 的入口不消费教师模型，故不列入（2026-11-04 实测这些入口里
# `11434|ollama` 命中数为 0）：canary-health -> scripts/canary_health_check.py（只读
# GET /api/tags 做健康检查，正是「动态解析」的形状）、tdx-clean-logs -> make clean-logs、
# archive-obsidian-annual -> scripts/archive_obsidian_annual.sh。
# trainsh-health 的入口是 scripts/train_health.sh：它含 6 处 `qwen3-14b-tdx`，但全是
# `find -type d -name "qwen3-14b-tdx-${SHORT}-*"` 这种**训练产物目录名** glob（0 处
# ollama/11434），已按 artifact_dir 登记在下面的清单里；本判据只扫 .py 的字符串字面量，
# 收它进来会把「目录命名约定」误判成「写死教师 tag」，所以不列——排除是显式的，不是漏的。
LIVE_TIMER_ENTRY_SCRIPTS: tuple[str, ...] = (
    "scripts/predict.py",
    "scripts/daban_entry.py",
    "scripts/plan_e_backtest.py",
    "scripts/plan_e_journal.py",
    "scripts/find_double_up_c.py",
)

_TEACHER_TAG_RE = re.compile(r"qwen3-14b-tdx[A-Za-z0-9_.:-]*")

# 仓库层扫描面：都被 git 跟踪 ⇒ CI 里也判得到。
_TAG_SCAN_ROOTS: tuple[str, ...] = (
    "scripts",
    "v5",
    "deploy",
    "skills",
    "skill_router",
    "tdxfeed",
    "quant",
    "mcp",
    "training",
)
_TAG_SCAN_FILES: tuple[str, ...] = ("Makefile", "docker-compose.yml")
# 本机层扫描面：gitignore 掉的目录，CI 检出里不存在，只在本机判（见模块 docstring）。
_TAG_LOCAL_SCAN_ROOTS: tuple[str, ...] = ("train",)
_TAG_SCAN_SUFFIXES: frozenset[str] = frozenset({".py", ".sh", ".yaml", ".yml"})
_TAG_SCAN_EXCLUDED_PARTS: frozenset[str] = frozenset(
    {"_archive", "tests", "__pycache__", ".qoder", ".mypy_cache", ".ruff_cache"}
)

# 结论词表（登记即文档，每条给出「为什么不动它」）。
TEACHER_TAG_VERDICTS: dict[str, str] = {
    "hist_baseline": "历史基准：钉死当时的 tag 做 v4/v5/v6/v7/v8/v9 纵向对比，改成运行时解析就毁掉可比性 ⇒ 不改",
    "producer": "生产者侧：该脚本自己 merge / ollama create 这个 tag（tag 由它定义再使用），不是消费方写死别人的名字 ⇒ 不改",
    "artifact_dir": "产物目录名：train/output/qwen3-14b-tdx-* 这类 LoRA/merged 目录，不是传给 Ollama 的模型名 ⇒ 不改",
    "doc_text": "文案与示例：只出现在 docstring / argparse help / 报告文本里，命令行入口要求显式传 tag ⇒ 不改",
    "config_label": "配置声明：监控静态标签、compose/entrypoint 的可覆盖默认值、skill 声明，不在在役调用链上 ⇒ 不改（要改是口径决定）",
    "orphan_default": "孤立手工入口的默认值：不在任何 enabled timer 链上（全仓零引用），复用前必须显式传参 ⇒ 待收编，本次不动",
}

_NOTE_LORA_DIR = "训练配置里的 output_dir / adapter_name_or_path 目录名，非 Ollama 调用名"
_NOTE_BENCH = "bench 矩阵按版本钉 tag，钉死才是基准"
_NOTE_ROLL = "v4/v5/v6 历史纵向对比脚本，tag 是当时的被测对象"

TEACHER_TAG_SITES: dict[str, tuple[str, str]] = {
    # —— 历史基准（v4/v5/v6/v7 对比与部署链）——
    "v5/scripts/v4_historical_roll.py": ("hist_baseline", _NOTE_ROLL),
    "v5/scripts/v4_poc_analysis.py": ("hist_baseline", _NOTE_ROLL),
    "v5/scripts/v4_batch_analysis.py": ("hist_baseline", _NOTE_ROLL),
    "v5/scripts/v4_aggregate_report.py": ("hist_baseline", "汇总报告里印的被测模型名"),
    "v5/scripts/v6_vs_v4_baseline.py": ("hist_baseline", _NOTE_ROLL),
    "v5/scripts/v5_eval.py": ("hist_baseline", "v4 vs v5 对比的两侧 tag"),
    "v5/scripts/v6_eval.py": ("hist_baseline", "v4 vs v6 对比的两侧 tag"),
    "v5/scripts/v5_v6_compare.py": ("hist_baseline", "v5/v6 对比 tag（25-26 行）"),
    "v5/scripts/v4_full_daemon.sh": ("hist_baseline", "v4 时代的常驻脚本"),
    "v5/scripts/v5_full_a_daemon.sh": ("hist_baseline", "v5 时代的常驻脚本"),
    "v5/scripts/full_pipeline.sh": ("hist_baseline", "v5 时代的一次性流水线"),
    "scripts/bench_v7r.py": ("hist_baseline", _NOTE_BENCH),
    "scripts/bench_v567.py": ("hist_baseline", _NOTE_BENCH),
    "scripts/bench_cls2000.py": ("hist_baseline", _NOTE_BENCH),
    "Makefile": ("hist_baseline", "MODEL_V5/V6/V7/BASE 只供 deploy-v*/deploy-test 展示；"
                            "distill 已收成必填 $(MODEL)，不传就 exit 2"),
    # —— 生产者侧（自己 create 这个 tag）——
    "v5/scripts/deploy_v5.sh": ("producer", "merge → GGUF → Modelfile → ollama create"),
    "v5/scripts/deploy_v6.sh": ("producer", "merge → GGUF → Modelfile → ollama create"),
    "v5/scripts/deploy_v7.sh": ("producer", "v7 部署脚本自己定义 tag"),
    "scripts/v4fwd_post_train.sh": ("producer", "训练后自己 create 出 v4fwd tag"),
    "scripts/run_f6.sh": ("producer", "一次性 f6 编排：先 create v4/v7r-q8 再调它们"),
    # —— 产物目录名（LoRA / merged 目录，不是模型调用名）——
    "v5/scripts/v6_data_prep.py": ("artifact_dir", _NOTE_LORA_DIR),
    "v5/audit/v6_enriched_train.yaml": ("artifact_dir", "v6 审计快照里的 model_name_or_path"),
    "scripts/plot_loss.py": ("artifact_dir", "按目录名 glob 找训练日志"),
    "scripts/train_monitor.sh": ("artifact_dir", "trainer_log.jsonl 所在目录"),
    "scripts/train_health.sh": ("artifact_dir", "按目录名反查训练进程归属"),
    "scripts/gen_dashboard.py": ("artifact_dir", "loss_curve.png 的所在目录"),
    "scripts/merge_v5_v6_lora.py": ("artifact_dir", "merge 的输入/输出 adapter 目录"),
    "training/config/tdx_export_v4_qlora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_export_v4fwd_qlora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_export_verify.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_lora_packed.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_lora_resume_fast.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v4_qlora_real.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v4fwd_qlora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v4fwd_qlora_fast.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v5_lora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v5_lora_simple.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v6_lora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v7_resume.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_v8_resume.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_verify_dual_qlora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_verify_lora_nq.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "training/config/tdx_verify_single_qlora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    # —— 文案 / 配置声明 ——
    "scripts/distill_outputs.py": ("doc_text", "只在报错示例里出现；tag 取自必填的 TDX_DISTILL_MODEL"),
    "scripts/llm_signal_plan_e.py": ("doc_text", "docstring 与 --model help 的举例，--model 是必填"),
    "scripts/create_v4_model_note.py": ("doc_text", "生成的模型说明文案；其中「线上默认」措辞已过时，"
                                                   "现役 LLM 线是 v7r1115"),
    "skill_router/cli.py": ("doc_text", "usage 示例里的 checkpoint 路径"),
    "deploy/prometheus/prometheus.yml": ("config_label", "relabel 写的静态 model 标签，只影响指标维度"),
    "deploy/scripts/docker-entrypoint.sh": ("config_label", "TDX_OLLAMA_MODEL 可覆盖的默认 pull 目标；"
                                                       "compose 栈当前 0 个容器在跑"),
    "docker-compose.yml": ("config_label", "同上：TDX_OLLAMA_MODEL 的默认值"),
    "skills/kline-analysis.yaml": ("config_label", "model_id 与 adapter_path 两处声明；adapter_path "
                                              "指向不存在的目录，已由 tests/test_skill_runtime_prereqs.py 登记"),
    # —— 孤立手工入口 ——
    "scripts/plan_e_confidence.py": ("orphan_default", "68 行 sys.argv 缺省即 qwen3-14b-tdx-v4:latest；"
                                                  "全仓零引用、无单元调度"),
}

# 本机层（gitignored 的 train/）：v8/v9 的训练·评估·merge·部署脚本都在这——「v8/v9 用的
# 是哪个」那条线的答案就在这 9 个文件里。CI 检出里没有这个目录，所以它只在机器层闭合。
TEACHER_TAG_SITES_LOCAL: dict[str, tuple[str, str]] = {
    "train/tdx_lora.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "train/config/dpo.yaml": ("artifact_dir", _NOTE_LORA_DIR),
    "train/scripts/eval.py": ("doc_text", "Usage/Inputs 文案里的 --adapter 示例目录"),
    "train/scripts/eval_v8.py": ("artifact_dir", "v8 续训前后对比钉的两个 adapter 目录（v7-resume/v8-resume）"),
    "train/scripts/gen_v8_report.py": ("doc_text", "报告正文里钉的「续训起点」措辞，是历史快照文案"),
    "train/scripts/merge_lora.py": ("artifact_dir", "DEFAULT_ADAPTER 目录；112 行只是打印给人执行的 "
                                                  "ollama create 指令"),
    "train/scripts/merge_lora_shard.py": ("artifact_dir", "--adapter 默认值钉到 v9/checkpoint-6006 目录，"
                                                        "就是 v9 那条线"),
    "train/scripts/run_v8_pipeline.sh": ("artifact_dir", "v8-resume adapter 是否存在的探针路径"),
    "train/scripts/deploy_v7_resume.sh": ("producer", "MODEL_NAME=\"qwen3-14b-tdx-${NAME}\" + ollama create，"
                                                 "v7r/v7r1115 这两个 tag 由它自己产出"),
}


def _is_excluded(path: Path) -> bool:
    parts = {part.name for part in path.parents}
    return not parts.isdisjoint(_TAG_SCAN_EXCLUDED_PARTS)


def _targets(roots: tuple[str, ...], files: tuple[str, ...]) -> list[Path]:
    """显式点名的文件无条件进面（`Makefile` 没有后缀，按后缀过滤会把它整个漏掉）；
    按根遍历的才过后缀与排除两道筛。这条形状是被等值判据逼出来的：把两层合并成一个
    过滤器时，`Makefile` 会以「登记了却无命中」的假腐化红出来。"""
    out: list[Path] = [ROOT / rel for rel in files if (ROOT / rel).is_file()]
    for rel in roots:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in _TAG_SCAN_SUFFIXES and not _is_excluded(path):
                out.append(path)
    return out


def _tag_hits_in(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    for lineno, line in enumerate(text.splitlines(), start=1):
        found = _TEACHER_TAG_RE.search(line)
        if found:
            hits.append((lineno, found.group(0)))
    return hits


def _detected(roots: tuple[str, ...], files: tuple[str, ...]) -> dict[str, list[tuple[int, str]]]:
    detected: dict[str, list[tuple[int, str]]] = {}
    for path in _targets(roots, files):
        hits = _tag_hits_in(path)
        if hits:
            detected[path.relative_to(ROOT).as_posix()] = hits
    return detected


def _literal_strings_with_tag(path: Path) -> list[tuple[int, str]]:
    """取 .py 里含教师 tag 的字符串字面量（AST，不是整行文本匹配）。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found = _TEACHER_TAG_RE.search(node.value)
            if found:
                hits.append((node.lineno, found.group(0)))
    return hits


def _inventory_rows(detected: dict[str, list[tuple[int, str]]],
                    registered: dict[str, tuple[str, str]],
                    tier: str) -> list[str]:
    lines: list[str] = []
    for rel in sorted(detected):
        code, note = registered[rel]
        for lineno, tag in detected[rel]:
            lines.append(
                f"[{tier}] {rel}:{lineno}\t{tag}\t{code}\t{TEACHER_TAG_VERDICTS[code]}｜{note}"
            )
    return lines


def format_teacher_tag_inventory() -> str:
    """把清单排成「层 文件:行  tag  结论」的文本（行号与 tag 现读磁盘，不手抄）。"""
    rows = _inventory_rows(_detected(_TAG_SCAN_ROOTS, _TAG_SCAN_FILES), TEACHER_TAG_SITES, "仓库")
    rows += _inventory_rows(_detected(_TAG_LOCAL_SCAN_ROOTS, ()), TEACHER_TAG_SITES_LOCAL, "本机")
    return "\n".join(rows)


def test_tracked_tag_scan_roots_are_all_present() -> None:
    """仓库层扫描面不许悄悄缩小：登记的根与文件必须都还在（少一个＝守卫对那片变聋）。

    只判**被 git 跟踪**的那一层：本机层（train/）在 CI 检出里本来就不存在，判它会变成
    假红——那一层的约束由 test_local_tier_roots_are_really_gitignored 与本机等值判据管。
    """
    absent = [rel for rel in _TAG_SCAN_ROOTS if not (ROOT / rel).is_dir()]
    absent += [rel for rel in _TAG_SCAN_FILES if not (ROOT / rel).is_file()]
    assert not absent, f"tag 扫描面（仓库层）里有不存在的路径，判据覆盖面已缩小: {absent}"


def test_local_tier_roots_are_really_gitignored() -> None:
    """机器层的每个根必须确实被 gitignore，否则它就该待在仓库层（CI 才判得到）。

    防的是「以后有人把 train/ 收进 git，却还让它留在机器层」——那等于把一条本可在 CI 判的
    面降级成只有本机判。判据现读 `.gitignore`（这文件本身被跟踪，CI 里也判得了）。
    取**字面匹配的最后一条**规则：gitignore 是后者覆盖前者，只看「规则里出现过 train/」
    会被后面一条 `!train/` 骗过去（实测 `git check-ignore -v train/scripts/eval.py` 现在
    给的确实是 `.gitignore:48: train/`，这条判据是它在不跑 git 的前提下的近似）。
    """
    rules = [
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    for rel in _TAG_LOCAL_SCAN_ROOTS:
        wanted = {rel, f"{rel}/", f"!{rel}", f"!{rel}/"}
        last = next((rule for rule in reversed(rules) if rule in wanted), None)
        verdict = (
            "没匹配到任何规则" if last is None else f"最后匹配到的是 {last!r}（反选，已重新纳入版本）"
        )
        assert last is not None and not last.startswith("!"), (
            f"{rel}/ 被登记成「本机层（CI 判不了）」，但 .gitignore 对它的结论不是「忽略」——"
            f"{verdict}。它现在是被跟踪的，请把它挪进 _TAG_SCAN_ROOTS "
            f"并把登记项并进 TEACHER_TAG_SITES"
        )
    leaked = sorted(set(_TAG_LOCAL_SCAN_ROOTS) & set(_TAG_SCAN_ROOTS))
    assert not leaked, f"同一个根既在仓库层又在机器层（等值判据会互相打架）: {leaked}"


def test_live_timer_entries_do_not_hardcode_teacher_tag() -> None:
    """B2①：在役入口（enabled timer 的 ExecStart）里不许出现教师 tag 字面量。

    判据取 AST 字符串字面量（含 docstring，保守）：在役链全是 LightGBM/规则，
    没有任何正当理由写教师模型 tag——注释里提到不算命中，字面量里出现就算。
    """
    offenders: list[str] = []
    for rel in LIVE_TIMER_ENTRY_SCRIPTS:
        path = ROOT / rel
        if not path.is_file():
            offenders.append(f"{rel} 不存在（在役入口清单需与 timer 的 ExecStart 重新对齐）")
            continue
        for lineno, tag in _literal_strings_with_tag(path):
            offenders.append(f"{rel}:{lineno} 出现教师模型 tag 字面量 {tag}")
    assert not offenders, (
        "在役路径写死了教师 tag——本机 Ollama 的权威是 GET /api/tags，"
        "写死的名字随时可能是空气：\n" + "\n".join(offenders)
    )


def test_tracked_tag_inventory_is_exactly_registered() -> None:
    """B2②（仓库层）：命中集与登记集必须完全一致——多一个是漏网，少一个是腐化。"""
    detected = _detected(_TAG_SCAN_ROOTS, _TAG_SCAN_FILES)
    unregistered = sorted(set(detected) - set(TEACHER_TAG_SITES))
    stale = sorted(set(TEACHER_TAG_SITES) - set(detected))
    detail = "; ".join(
        f"{rel}:{detected[rel][0][0]}（{detected[rel][0][1]}）" for rel in unregistered
    )
    assert not unregistered, (
        "出现未登记的教师 tag 写死点，先判断它是不是在役路径，再按结论词表登记："
        f"{detail}\n（完整清单可由 format_teacher_tag_inventory() 现读生成）"
    )
    assert not stale, (
        f"登记面里这些文件已经没有教师 tag 命中（登记腐化，该删或该重新评估）: {stale}"
    )


def test_local_tag_inventory_is_closed_when_the_dir_exists() -> None:
    """B2②（机器层）：train/ 在场时逐条等值闭合；不在场时无货可判（CI 就是这个形状）。

    这一层是**声明出来的**覆盖缺口：`.gitignore` 里有 `train/`，CI 检出里没这个目录，
    所以 CI 判不到它——但正因为如此，本机必须判得住，否则「v8/v9 那条线」连一份登记都没有
    （2026-11-04 返工前的实际状态就是 9 个文件既没登记也没判红）。
    两条防滑：机器层声明不得被清空；根名打错（目录在场却扫出 0 条）在本机必须红，
    否则「加了 train 但拼错」和「已经收干净」长得一样。
    """
    assert _TAG_LOCAL_SCAN_ROOTS, "机器层声明被清空 ⇒ train/ 那条线又没人判了"
    present = [rel for rel in _TAG_LOCAL_SCAN_ROOTS if (ROOT / rel).is_dir()]
    for rel in present:
        detected = _detected((rel,), ())
        assert detected, (
            f"{rel}/ 在场却扫出 0 条 qwen3-14b-tdx ⇒ 根名/后缀/图案有一处失效，"
            f"不是『这片已经收干净』"
        )
        unregistered = sorted(set(detected) - set(TEACHER_TAG_SITES_LOCAL))
        stale = sorted(set(TEACHER_TAG_SITES_LOCAL) - set(detected))
        detail = "; ".join(
            f"{name}:{detected[name][0][0]}（{detected[name][0][1]}）" for name in unregistered
        )
        assert not unregistered, (
            f"{rel}/（本机层）出现未登记的教师 tag 写死点：{detail}"
        )
        assert not stale, f"{rel}/（本机层）有登记已腐化（该目录里已无命中）: {stale}"


def test_tag_verdicts_are_from_the_vocabulary() -> None:
    """每条登记都得落到结论词表上，且词表里每条结论都要有人用（不许挂空结论）。"""
    all_sites = {**TEACHER_TAG_SITES, **TEACHER_TAG_SITES_LOCAL}
    codes = {code for code, _note in all_sites.values()}
    unknown = sorted(codes - set(TEACHER_TAG_VERDICTS))
    assert not unknown, f"登记用了词表外的结论码: {unknown}"
    unused = sorted(set(TEACHER_TAG_VERDICTS) - codes)
    assert not unused, f"结论词表里有从未被使用的档位（词表该收口）: {unused}"
    overlap = sorted(set(TEACHER_TAG_SITES) & set(TEACHER_TAG_SITES_LOCAL))
    assert not overlap, f"同一个文件被登记进两层（清单会有两份结论）: {overlap}"


def test_control_gitignore_provenance_detects_reinclusion(monkeypatch, tmp_path: Path) -> None:
    """变异对照：`.gitignore` 最后一条是 `!train/`（重新纳入版本）时，溯源判据必须红。

    只看「规则里出现过 train/」会被后面的反选骗过去；这条对照钉的是「后者覆盖前者」这个
    语义，也是本模块唯一一条机器层失效绊线（CI 里没有 train/ 目录，存在性判据判不了它）。
    """
    (tmp_path / ".gitignore").write_text("train/\n!train/\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="本机层"):
        test_local_tier_roots_are_really_gitignored()


def test_control_tag_literal_detector_catches_a_known_offender() -> None:
    """变异对照 B：字面量检测器必须抓得到一个真写死了 tag 的文件。

    「在役入口零命中」只有在检测器本身能命中时才有意义，否则绿的形状和「图案写错了」
    一模一样。这里拿仓库里那条真 offender（scripts/plan_e_confidence.py:68）当对照。
    """
    hits = _literal_strings_with_tag(ROOT / "scripts" / "plan_e_confidence.py")
    assert hits, "plan_e_confidence.py 里写死的教师 tag 没被抓到 ⇒ 图案或取字面量的方式失效"
    assert any(tag.startswith("qwen3-14b-tdx-v4") for _lineno, tag in hits), hits


def test_control_scan_walk_detects_a_written_site(monkeypatch, tmp_path: Path) -> None:
    """变异对照 B2：在临时树里按真实形状写一条带 tag 的行，文本扫描必须抓到。

    钉的是「扫描链条本身」：根遍历 + 后缀过滤 + 图案，在真实形状（`train/scripts/*.py`）
    上必须逐条抓到，而不是静默返回空集。「根名打错」这一半由
    `test_local_tier_roots_are_really_gitignored` 绊住（打错的名字在 .gitignore 里没有规则
    ⇒ 红，且 CI 也判得了）；这一条管的是另一半：根名对、但链条某处断了。
    """
    (tmp_path / "train" / "scripts").mkdir(parents=True)
    scratch = tmp_path / "train" / "scripts" / "scratch_merge.py"
    scratch.write_text('ADAPTER = "train/output/qwen3-14b-tdx-v9/checkpoint-6006"\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    detected = _detected(("train",), ())
    assert detected == {"train/scripts/scratch_merge.py": [(1, "qwen3-14b-tdx-v9")]}, detected


def test_control_tag_text_scan_is_not_blind() -> None:
    """变异对照 B2：文本扫描与清单排版必须扫得到每一条命中。

    0 命中意味着扫描面/后缀/图案有一处失效——那和「已经收干净」的绿的形状一模一样，
    所以这里显式要求扫到东西；清单还必须逐条给行号（只给每文件第一条＝清单在漏报）。
    """
    detected = _detected(_TAG_SCAN_ROOTS, _TAG_SCAN_FILES)
    assert detected, "扫描面里一条 qwen3-14b-tdx 都没扫到 ⇒ 判据形同虚设（不是『已经收干净』）"
    rows = [line.split("\t") for line in format_teacher_tag_inventory().splitlines()]
    expected = sum(len(hits) for hits in detected.values())
    if (ROOT / "train").is_dir():
        expected += sum(
            len(hits) for hits in _detected(_TAG_LOCAL_SCAN_ROOTS, ()).values()
        )
    assert len(rows) == expected, (
        f"清单只排了 {len(rows)} 行，命中却有 {expected} 处 ⇒ 排版在漏报或两层漏算"
    )
    covered = {row[0].split("] ", 1)[1].rsplit(":", 1)[0] for row in rows}
    assert covered == set(detected) | (
        set(_detected(_TAG_LOCAL_SCAN_ROOTS, ())) if (ROOT / "train").is_dir() else set()
    ), f"清单与命中面的文件集不一致: {sorted(covered ^ (set(detected) | set(_detected(_TAG_LOCAL_SCAN_ROOTS, ()))))}"
