#!/usr/bin/env python3
"""
Canary 健康检查脚本
====================

每 5 分钟跑一次，检查（与 `run_all_checks()` 的维度顺序一致，11 维；
文档侧的同一张表在 docs/WORKFLOW.md「canary 维度」行，守卫 tests/test_canary_dimensions_documented.py）：
  1. ollama            Ollama 服务在线 + tdx 模型可访问
  2. tdxfeed_sync      数据采集（系统级 tdx-sync.timer）触发时间与上次结果
  3. data_files        关键表文件数 + 抽样末日期中位数（adj/hfq/derived）
  4. data_files_tail   尾部新鲜度：全量逐文件末日期，抓「个别标的整表停更」（KI-OPS-005）
  5. data_coverage     解析覆盖率：读不出 date 末字段的日线文件占比
  6. disk              磁盘剩余
  7. timers            关键 systemd timer 是否激活
  8. gpu               GPU 状态（利用率长期 >95% 视为卡住）
  9. obsidian_sync     obsidian-sync 单元部署/重载状态（防漂移与 start-limit-hit）
 10. verification      每日验证快照新鲜度、all_ok、latest 指针
 11. swallowed_errors  吞错计数趋势（SEM002 运营闭环）

维度是否「翻页」（warn/error 就 exit 1 → OnFailure → 微信）只看 PAGING_CHECKS，
装配点在 `_paging_checks()`；长期为真的项一律 report-only，否则每 5 分钟推一次就是刷屏。

输出：
  - logs/canary.log（追加）
  - logs/canary_history.jsonl（结构化时序）

异常时写 stderr → systemd 会发邮件或触发 alert

【运行】
  venv/bin/python scripts/canary_health_check.py            # 一次性
  venv/bin/python scripts/canary_health_check.py --quiet    # 静默模式（仅异常时输出）
"""

import argparse
import atexit
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
LOG_FILE = ROOT / 'logs' / 'canary.log'
HISTORY_FILE = ROOT / 'logs' / 'canary_history.jsonl'
VERIFICATION_DIR = ROOT / 'v5' / 'audit' / 'verification'

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被 tests 按文件路径加载时 scripts/ 不在 sys.path
from _common import adj_factor_dir, daily_dir, derived_dir, hfq_factor_dir  # noqa: E402

from tdxfeed.errors import (  # noqa: E402
    flush as _flush_swallowed_counts,
)
from tdxfeed.freshness import (  # noqa: E402
    _is_a_share_stock,
    sample_latest_date,
    stale_threshold,
    table_freshness,
)

# 吞错计数（SEM002）：每处被吞掉的错误都计入统计，按函数名分桶，便于事后追溯失败率
_ERROR_COUNT: Counter[str] = Counter()

atexit.register(_flush_swallowed_counts)

# 吞错计数巡检窗口与告警阈值（阈值可用环境变量覆盖）
SWALLOWED_WINDOW_HOURS = int(os.environ.get('TDX_SWALLOWED_WINDOW_HOURS', '24'))
SWALLOWED_WARN_THRESHOLD = int(os.environ.get('TDX_SWALLOWED_WARN_THRESHOLD', '20'))

# 阈值
OLLAMA_TIMEOUT = 5           # 秒
PARQUET_MIN_COUNT = 5000     # 最少日 K 文件数
ADJ_MIN_COUNT = 5000         # 最少复权因子文件数
HFQ_MIN_COUNT = 5000         # 最少后复权因子文件数
DISK_MIN_FREE_GB = 20        # 最小剩余磁盘 GB
VERIFICATION_MAX_AGE_HOURS = 30  # 每日 02:00 UTC 快照；留 6 小时余量

# 这些检查出问题（error 或 warn）才退出非零 → systemd OnFailure → 微信。
# GPU/磁盘这类长期 warn 不在此列：每 5 分钟推一次就是刷屏，留 journal 与 canary 历史即可。
BASE_PAGING_CHECKS = frozenset({'tdxfeed_sync', 'data_files', 'timers', 'verification'})

# ---------- 尾部新鲜度两维（KI-OPS-005）：翻页开关与它的实测依据 ----------
# 两维名字。`data_files_tail` = 有没有个别标的从表尾掉队；
# `data_coverage` = 有多少文件根本读不出日期。两者正交，各自 warn 条件不同。
TAIL_CHECKS = frozenset({'data_files_tail', 'data_coverage'})

# ⚠ 决策常量：**False = 只报告不翻页**。改它之前先读完下面那段实测，并同步
# tests/test_canary_tail_freshness.py::test_measured_decision_is_report_only…
TAIL_PAGING_ENABLED = False

# 决策依据（2026-11-03 在本机 data/ 真数据上直接调用 table_freshness 取的实际值；
# 没走 canary.main()，因此不写 logs/、不触发 OnFailure、不推手机）：
#   范围：data/parquet/daily 的 A 股个股（_is_a_share_stock 且文件名不含 .bak）
#   文件数 5,232（paths.daily_files() 规范名 5,236 只里有 4 只非个股）
#   ref_date=2026-11-03（周二）→ 最近已收盘交易日 2026-11-02
#   allowed_trading_days=1 → threshold=2026-10-30
#   parsed=5232/5232（解析失败 0，覆盖率维因此 ok）
#   latest=2026-11-02  oldest=2009-12-15
#   stale_count=9  stale_ratio=0.1720%
# 实际滞后的代码 + 末日期（末日期 < 2026-10-30 全部列出），带标的名称便于分类：
#   sh600001  2009-12-15  ← 不在 all_a_stocks.json 名单里（5,221 只）的历史残留文件；
#                            _is_a_share_stock 只看代码段（600 前缀）所以收了它
#   sz000016  2026-09-03  *ST康佳A
#   sh688496  2026-09-10  *ST清越
#   sh601059  2026-09-14  信达证券   ← adj/hfq 同票停在 10-16，三只同日前后脚，像停牌
#   sh601198  2026-09-14  东兴证券   ← 同上
#   sz002717  2026-10-12  *ST岭南
#   sh601326  2026-10-20  秦港股份
#   sz301139  2026-10-27  *ST元道
#   sh688121  2026-10-28  *ST卓然
# 这批的形状本身就是「不该现在翻页」的第二个理由：9 只里 5 只是 *ST、1 只是名单外的
# 历史残留，都不是 full_sync 没跑成——真出故障时要靠的是 tdxfeed_sync / data_files /
# data_coverage 那几维。把它们变成每 5 分钟的微信推送，等于把长期事实训练成噪声。
# 判据（不可协商）：stale_count == 0 才允许把两维并进 PAGING_CHECKS。
# 现在 9 > 0 ⇒ **保持 report-only**，否则 canary-health 每 5 分钟一次
# OnFailure=onfailure-alert@canary-health.service → pushplus → 微信就是刷屏。
# 翻 PAGING 需要先把上面这批清零——退市/停牌/残留要**显式**进白名单，不能靠忽略——
# 或者按表分级给单独阈值；收紧口径本身也是选项（拿 paths.universe_path() 的 5,221 只
# 名单当范围，sh600001 那类残留就不在统计里了）。三种都要重跑一次实测再改这个常量。
# 参考：同一天 adj_factor / hfq_factor 也各有 4 只滞后（601059、601198 停在 10-16，
# 000016 停在 10-22，301139 停在 10-27），derived 与 daily 同为 9 只；那三张表目前仍走
# 抽样中位数的 data_files 维，尾部覆盖是后续项（本维按规格只做 daily A 股个股）。


def _paging_checks() -> frozenset[str]:
    """PAGING_CHECKS 的唯一装配点：开关关着时两维只进 journal 与 canary 历史。"""
    return BASE_PAGING_CHECKS | (TAIL_CHECKS if TAIL_PAGING_ENABLED else frozenset())


PAGING_CHECKS = _paging_checks()

# 尾部维的容忍度与覆盖率阈值
TAIL_ALLOWED_TRADING_DAYS = 1     # 与 data_files 同口径：结构性只能追到 T-1
COVERAGE_WARN_RATIO = 0.01        # 解析失败比例 > 1% ⇒ warn（严格大于）


def _today() -> date:
    """「今天」的唯一入口——测试 monkeypatch 它，避免用例随真实日期腐烂。"""
    return date.today()


def check_ollama() -> dict:
    """检查 Ollama 服务"""
    result = {'name': 'ollama', 'status': 'ok', 'details': {}}
    try:
        r = requests.get('http://localhost:11434/api/tags', timeout=OLLAMA_TIMEOUT)
        if r.status_code == 200:
            models = r.json().get('models', [])
            tdx_models = [m['name'] for m in models if 'tdx' in m['name']]
            result['details'] = {
                'total_models': len(models),
                'tdx_models': tdx_models,
            }
            if not tdx_models:
                result['status'] = 'warn'
                result['message'] = 'no tdx models loaded'
        else:
            result['status'] = 'error'
            result['message'] = f'HTTP {r.status_code}'
    except requests.exceptions.ConnectionError:
        _ERROR_COUNT['check_ollama'] += 1
        result['status'] = 'error'
        result['message'] = 'connection refused (ollama not running)'
    except Exception as e:
        _ERROR_COUNT['check_ollama'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def _show(unit: str, props: str) -> dict:
    out = subprocess.run(
        ['systemctl', 'show', unit, f'--property={props}'],
        capture_output=True, text=True, timeout=5
    )
    details = {}
    for line in out.stdout.strip().splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            details[k.strip()] = v.strip()
    return details


def _parse_systemd_ts(value: str):
    """解析 `Tue 2026-10-27 16:31:59 CST` → datetime；空的/不可解析返回 None。

    只取末尾两段（日期 + 时刻）：星期前缀随 locale 变，`%Y-%m-%d` 也吃不掉它。
    """
    value = value.replace('CST', '').strip()
    parts = value.split()
    if len(parts) < 2:
        return None
    try:
        return datetime.strptime(' '.join(parts[-2:]), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        _ERROR_COUNT['_parse_systemd_ts'] += 1
        return None


def check_tdxfeed_sync() -> dict:
    """检查最后一次全市场同步（系统级 tdx-sync.timer/service，应 ≤ 48 小时前）。

    时间源必须是 timer 的 `LastTriggerUSec`：oneshot service 跑完被 GC 后，
    `ActiveEnterTimestamp` 一律为空，原来的年龄检查会整段跳过——同步停两周也报 ok。
    """
    result = {'name': 'tdxfeed_sync', 'status': 'ok', 'details': {}}
    try:
        timer = _show('tdx-sync.timer', 'LastTriggerUSec,ActiveState')
        service = _show('tdx-sync.service', 'Result')
        result['details'] = {**timer, **service}

        problems = []
        last_result = service.get('Result', '')
        if last_result and last_result != 'success':
            problems.append(f'last run result: {last_result}')

        ts = _parse_systemd_ts(timer.get('LastTriggerUSec', ''))
        if ts is None:
            problems.append('sync never triggered')
        else:
            age = datetime.now() - ts
            result['details']['age_hours'] = round(age.total_seconds() / 3600, 2)
            if age > timedelta(days=2):
                problems.append(f'sync too old: {age}')

        if problems:
            result['status'] = 'warn'
            result['message'] = '; '.join(problems)
    except Exception as e:
        _ERROR_COUNT['check_tdxfeed_sync'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_data_files() -> dict:
    """检查关键数据文件数量与表级新鲜度（adj_factor / hfq_factor / derived 的末日期）。

    只数文件个数发现不了停更：adj 停在 09-24 时文件一个不少，旧实现照样报 ok。
    表级新鲜度取抽样末日期中位数，阈值是「最近已收盘交易日往前 1 个交易日」。
    """
    result = {'name': 'data_files', 'status': 'ok', 'details': {}}

    pq_dir = daily_dir()
    adj_dir = adj_factor_dir()
    hfq_dir = hfq_factor_dir()

    n_pq = len([p for p in pq_dir.glob('*.parquet') if '.bak' not in p.name]) if pq_dir.exists() else 0
    n_adj = len([p for p in adj_dir.glob('*.parquet') if '.bak' not in p.name]) if adj_dir.exists() else 0
    n_hfq = len([p for p in hfq_dir.glob('*.parquet') if '.bak' not in p.name]) if hfq_dir.exists() else 0

    result['details'] = {
        'parquet_daily': n_pq,
        'adj_factor': n_adj,
        'hfq_factor': n_hfq,
    }

    # 所有原因都进 problems 再一次性拼接：逐个覆盖 result['message'] 会让先发现的问题静默。
    problems = []
    if n_pq < PARQUET_MIN_COUNT:
        problems.append(f'parquet/daily only {n_pq} (< {PARQUET_MIN_COUNT})')
    if n_adj < ADJ_MIN_COUNT:
        problems.append(f'adj_factor only {n_adj} (< {ADJ_MIN_COUNT})')
    if n_hfq < HFQ_MIN_COUNT:
        problems.append(f'hfq_factor only {n_hfq} (< {HFQ_MIN_COUNT})')

    # 容忍 T-1 是有意的：后置任务在 16:30 同步后立即跑，而 baostock 当日 EOD
    # 晚间才发布，所以复权因子/后复权因子的增量结构性只能追到 T-1——这是正常态，
    # 不是故障。再晚一个交易日（T-2）就说明后置任务连着两天没成功，这才该报警。
    limit = stale_threshold(_today(), allowed_trading_days=1)
    tables = {
        'adj_factor': (adj_dir, ['*.parquet']),
        'hfq_factor': (hfq_dir, ['*.parquet']),
        'derived': (derived_dir(), ['*_indicators.parquet']),
    }
    for name, (d, patterns) in tables.items():
        files = [p for pat in patterns for p in d.glob(pat)] if d.exists() else []
        latest = sample_latest_date(files)
        result['details'][f'{name}_latest'] = latest.isoformat() if latest else None
        if latest is None:
            problems.append(f'{name}: 无文件')
        elif latest < limit:
            problems.append(f'{name} 陈旧: 末日期中位数 {latest} < {limit}')

    result['details']['stale_before'] = limit.isoformat()
    if problems:
        result['status'] = 'warn'
        prev = result.get('message')
        result['message'] = '; '.join(([prev] if prev else []) + problems)

    return result


def _tail_daily_files() -> list[Path]:
    """尾部统计的输入：data/parquet/daily 里的 A 股个股，排除 .bak 迁移备份。

    与 `tdxfeed.freshness.require_fresh` 同一套过滤（ETF/LOF 159/51/56、北交所 920/8
    不在 full_sync 的 akshare 清单里、长期停在旧日期，混进来只会污染尾部统计）。
    """
    d = daily_dir()
    if not d.exists():
        return []
    return [p for p in d.glob('*.parquet')
            if '.bak' not in p.name and _is_a_share_stock(p.name)]


def check_data_files_tail() -> dict:
    """尾部新鲜度：全量逐文件末日期，抓「个别标的整表停更」（KI-OPS-005）。

    为什么 `data_files` 不够：它用 `sample_latest_date` 的**固定 30 只抽样中位数**。
    一只票停更时中位数纹丝不动——表级报 ok，坏行永远在盲区。这里遍历全部 A 股个股
    文件（实测 5,232 只 0.2 秒级，走 parquet footer 列统计，不是整列读的 ~21 秒），
    末日期早于阈值就点名，payload 带 stale_count / stale_ratio / examples。

    状态：stale_count>0 → warn；一个文件都没有 → error（数据链整段没跑，不该静默 ok）。
    是否翻页由 TAIL_PAGING_ENABLED 决定，当前 **False**（本机实测 stale_count=9，
    依据与滞后清单在文件头注释里）。
    """
    result = {'name': 'data_files_tail', 'status': 'ok', 'details': {}}
    try:
        files = _tail_daily_files()
        if not files:
            result['status'] = 'error'
            result['message'] = f'{daily_dir()} 下没有可统计的 A 股个股日线文件'
            result['details'] = {'dir': str(daily_dir()), 'total': 0, 'parsed': 0,
                                 'stale_count': 0, 'examples': []}
            return result

        rep = table_freshness(files, _today(),
                              allowed_trading_days=TAIL_ALLOWED_TRADING_DAYS)
        result['details'] = {
            'dir': str(daily_dir()),
            'scope': 'daily/A 股个股（排除 .bak）',
            'allowed_trading_days': TAIL_ALLOWED_TRADING_DAYS,
            'total': rep['total'],
            'parsed': rep['parsed'],
            'latest': rep['latest'].isoformat() if rep['latest'] else None,
            'oldest': rep['oldest'].isoformat() if rep['oldest'] else None,
            'threshold': rep['threshold'].isoformat(),
            'stale_count': rep['stale_count'],
            'stale_ratio': round(rep['stale_ratio'], 6),
            'examples': rep['examples'],
        }
        if rep['stale_count']:
            result['status'] = 'warn'
            result['message'] = (
                f"{rep['stale_count']}/{rep['total']} 只末日期早于 {rep['threshold']}"
                f"（stale_count={rep['stale_count']}，最滞后 {rep['oldest']}，"
                f"examples={rep['examples']}）"
            )
    except Exception as e:  # 巡检维本身崩了要比静默 ok 有用
        _ERROR_COUNT['check_data_files_tail'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_data_coverage() -> dict:
    """解析覆盖率：全量扫里「读不出 date 列末日期」的文件占比 > 1% ⇒ warn。

    与尾部新鲜度正交：一个文件读不出来时没有可比较的日期，所以它不进 stale_count
    （`table_freshness` 里也是这么分的）；否则同一件事两维各喊一遍。
    分母是 total（含读不出的），空表按最该喊的形态处理：total==0 → warn。
    """
    result = {'name': 'data_coverage', 'status': 'ok', 'details': {}}
    try:
        files = _tail_daily_files()
        rep = table_freshness(files, _today(),
                              allowed_trading_days=TAIL_ALLOWED_TRADING_DAYS)
        total, parsed = rep['total'], rep['parsed']
        failed = total - parsed
        ratio = 1.0 if total == 0 else failed / total
        result['details'] = {
            'total': total,
            'parsed': parsed,
            'failed': failed,
            'fail_ratio': ratio,
            'warn_above': COVERAGE_WARN_RATIO,
        }
        if total == 0:
            result['status'] = 'warn'
            result['message'] = f'{daily_dir()} 没有任何可解析的 A 股个股日线文件'
        elif ratio > COVERAGE_WARN_RATIO:
            result['status'] = 'warn'
            result['message'] = (f'解析失败 {failed}/{total}（{ratio:.2%}）'
                                 f' > {COVERAGE_WARN_RATIO:.0%}，末日期不可读的样本：'
                                 f'见 details.failed')
    except Exception as e:
        _ERROR_COUNT['check_data_coverage'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_verification(directory: Path | None = None,
                       now: datetime | None = None) -> dict:
    """每日验证快照的新鲜度、结果与 latest 指针。

    `tdx-verification` 是 system 作用域，而 `onfailure-alert@.service` 只部署在用户管理器，
    系统侧写 `OnFailure=` 会找不到模板、静默无效——所以失败兜底放在这里：
    快照超过 30 小时没更新（timer 停了）、all_ok 不为 true（验证真的红了）、
    latest 没跟到最新快照（symlink 漂移），三者都进 PAGING_CHECKS。
    """
    result = {'name': 'verification', 'status': 'ok', 'details': {}}
    directory = directory or VERIFICATION_DIR
    now = now or datetime.now().astimezone()
    problems: list[str] = []

    snapshots = sorted(directory.glob('verification-*.json'))
    result['details'] = {'dir': str(directory), 'snapshots': len(snapshots)}
    if not snapshots:
        problems.append(f'{directory} 下没有 verification-*.json')
    else:
        result['details']['newest_snapshot'] = snapshots[-1].name

    latest = directory / 'latest.json'
    if not latest.exists():
        problems.append('缺 latest.json（或符号链接已断）')
    else:
        if not latest.is_symlink():
            problems.append('latest.json 不是符号链接，可能被拷贝成普通文件')
        if snapshots:
            target = latest.resolve()
            if target.name != snapshots[-1].name:
                problems.append(f'latest.json → {target.name}，最新快照是 {snapshots[-1].name}')
        try:
            payload = json.loads(latest.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f'latest.json 读不了: {str(e)[:120]}')
            payload = None
        if payload is not None:
            result['details']['ts'] = payload.get('ts')
            result['details']['all_ok'] = payload.get('all_ok')
            if payload.get('all_ok') is not True:
                problems.append(f"all_ok={payload.get('all_ok')!r}")
            try:
                ts = datetime.fromisoformat(str(payload.get('ts')))
            except ValueError:
                problems.append(f"ts 无法解析: {payload.get('ts')!r}")
            else:
                if ts.tzinfo is None:
                    problems.append('ts 没有时区，无法判断新鲜度')
                else:
                    age = now - ts
                    result['details']['age_hours'] = round(age.total_seconds() / 3600, 2)
                    if age > timedelta(hours=VERIFICATION_MAX_AGE_HOURS):
                        problems.append(f'快照太旧: {age}')

    if problems:
        result['status'] = 'warn'
        result['message'] = '; '.join(problems)
    return result


def check_disk() -> dict:
    """检查磁盘空间"""
    result = {'name': 'disk', 'status': 'ok', 'details': {}}
    try:
        st = os.statvfs('/home')
        free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)
        total_gb = (st.f_blocks * st.f_frsize) / (1024 ** 3)
        used_pct = (1 - st.f_bavail / st.f_blocks) * 100

        result['details'] = {
            'free_gb': round(free_gb, 2),
            'total_gb': round(total_gb, 2),
            'used_pct': round(used_pct, 2),
        }

        if free_gb < DISK_MIN_FREE_GB:
            result['status'] = 'error'
            result['message'] = f'only {free_gb:.1f}GB free (< {DISK_MIN_FREE_GB}GB)'
    except Exception as e:
        _ERROR_COUNT['check_disk'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_timers() -> dict:
    """检查关键 systemd timer 是否激活。

    同步已收口到系统级 tdx-sync.timer（16:30），它不属于 --user 管理器；
    只用 `--user` 查会把它读成 inactive，天天误报。这里按作用域逐个查。
    """
    result = {'name': 'timers', 'status': 'ok', 'details': {}}
    timers = (
        ('predict-daily.timer', 'user'),
        ('plan-e-journal.timer', 'user'),
        ('tdx-sync.timer', 'system'),
        ('tdx-verification.timer', 'system'),
    )
    inactive = []
    for t, scope in timers:
        try:
            cmd = ['systemctl', 'is-active', t]
            if scope == 'user':
                cmd.insert(1, '--user')
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if out.stdout.strip() != 'active':
                inactive.append(t)
        except Exception:
            _ERROR_COUNT['check_timers'] += 1
            inactive.append(t)
    result['details'] = {'checked': [t for t, _ in timers], 'inactive': inactive}
    if inactive:
        result['status'] = 'warn'
        result['message'] = f'inactive timers: {inactive}'
    return result


def check_gpu() -> dict:
    """检查 GPU 状态"""
    result = {'name': 'gpu', 'status': 'ok', 'details': {}}
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=index,memory.used,utilization.gpu',
             '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in out.stdout.strip().split('\n') if l.strip()]
        result['details']['gpus'] = lines
        # 如果有 GPU 利用率持续 > 95% 5分钟以上，警告（防止卡住）
        for line in lines:
            parts = line.split(',')
            if len(parts) >= 3:
                util = parts[2].strip().replace('%', '')
                if util and int(util) > 95:
                    result['status'] = 'warn'
                    result['message'] = f'GPU {parts[0]} util {util}% (likely stuck)'
                    break
    except FileNotFoundError:
        _ERROR_COUNT['check_gpu'] += 1
        result['status'] = 'skip'
        result['message'] = 'nvidia-smi not found'
    except Exception as e:
        _ERROR_COUNT['check_gpu'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
    return result


def check_obsidian_sync() -> dict:
    """检查 obsidian-sync 的 systemd 单元部署/重载状态（防漂移与 start-limit-hit）

    直接调用 scripts/verify_obsidian_systemd.sh --readonly（只读静态检查 V0–V4），
    由其作为单一事实来源，避免逻辑重复。失败时返回 'error' 以参与 canary 的非零退出告警。
    """
    result = {'name': 'obsidian_sync', 'status': 'ok', 'details': {}}
    script = ROOT / 'scripts' / 'verify_obsidian_systemd.sh'
    if not script.exists():
        result['status'] = 'error'
        result['message'] = f'verify script missing: {script}'
        return result
    try:
        proc = subprocess.run(
            ['bash', str(script), '--readonly'],
            capture_output=True, text=True, timeout=30
        )
        out = proc.stdout.strip()
        fail_lines = [l for l in out.splitlines() if '[FAIL]' in l]
        summary = [l for l in out.splitlines() if l.startswith('OBSIDIAN_SYSTEMD:')]
        result['details'] = {'failures': fail_lines, 'summary': summary}
        if proc.returncode != 0:
            result['status'] = 'error'
            result['message'] = '; '.join(fail_lines)[:300] or 'obsidian systemd check failed'
        else:
            result['status'] = 'ok'
            result['message'] = summary[0] if summary else 'ok'
    except subprocess.TimeoutExpired:
        _ERROR_COUNT['check_obsidian_sync'] += 1
        result['status'] = 'error'
        result['message'] = 'verify script timeout'
    except Exception as e:
        _ERROR_COUNT['check_obsidian_sync'] += 1
        result['status'] = 'error'
        result['message'] = f'check error: {str(e)[:200]}'
    return result


def check_swallowed_errors() -> dict:
    """吞错计数巡检（SEM002 的运营闭环）。

    读取生产入口进程退出时落盘的吞错计数（data/quality/swallowed_counts.jsonl），
    统计最近窗口内被吞掉的错误总数与集中点。

    说明：这是"计数被读到"的证据链末端 —— 存量 349 处失败从完全不可见，
    变为代码中可见、可 grep、可统计，并进一步可在巡检里按窗口观察趋势。
    """
    result = {'name': 'swallowed_errors', 'status': 'ok', 'details': {}}
    try:
        from tdxfeed.errors import read_recent
        rep = read_recent(hours=SWALLOWED_WINDOW_HOURS)
    except Exception as e:
        _ERROR_COUNT['check_swallowed_errors'] += 1
        result['status'] = 'error'
        result['message'] = str(e)[:200]
        return result
    total = int(rep.get('total', 0))
    result['details'] = {
        'window_hours': SWALLOWED_WINDOW_HOURS,
        'total': total,
        'records': rep.get('records', 0),
        'exists': rep.get('exists', False),
        'top_modules': list(rep.get('modules', {}).items())[:5],
        'top_functions': list(rep.get('top_functions', []))[:5],
    }
    if total > SWALLOWED_WARN_THRESHOLD:
        result['status'] = 'warn'
        result['message'] = (f'最近 {SWALLOWED_WINDOW_HOURS}h 吞错 {total} 处'
                             f'（阈值 {SWALLOWED_WARN_THRESHOLD}）')
    return result


def run_all_checks() -> dict:
    """跑所有检查"""
    return {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'checks': [
            check_ollama(),
            check_tdxfeed_sync(),
            check_data_files(),
            check_data_files_tail(),
            check_data_coverage(),
            check_disk(),
            check_timers(),
            check_gpu(),
            check_obsidian_sync(),
            check_verification(),
            check_swallowed_errors(),
        ]
    }


def main():
    parser = argparse.ArgumentParser(description='Canary 健康检查')
    parser.add_argument('--quiet', action='store_true', help='仅异常时输出')
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    report = run_all_checks()
    all_checks = report['checks']
    n_err = sum(1 for c in all_checks if c['status'] == 'error')
    n_warn = sum(1 for c in all_checks if c['status'] == 'warn')

    # 写历史
    with open(HISTORY_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(report, ensure_ascii=False) + '\n')

    # 决定输出
    is_problem = n_err > 0 or n_warn > 0
    if args.quiet and not is_problem:
        sys.exit(0)

    # 简洁摘要
    status_emoji = {'ok': '✅', 'warn': '⚠️', 'error': '❌', 'skip': '⏭️'}
    line = f"[{report['ts']}] "
    for c in all_checks:
        line += f"{status_emoji.get(c['status'], '?')}{c['name']} "
    line += f"| err={n_err} warn={n_warn}"

    print(line)

    # 详情（异常时）
    if is_problem:
        print()
        for c in all_checks:
            if c['status'] in ('error', 'warn'):
                print(f"  ⚠ {c['name']}: {c['message']}")
                print(f"    details: {json.dumps(c['details'], ensure_ascii=False)[:200]}")

    # 写日志
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')

    # 异常时退出非零（让 systemd 的 OnFailure 变成微信推送）
    # 数据链路的 warn 也要喊：同步变陈、关键文件不足、timer 掉线都不会抛异常，
    # 只打印进 journal 等于没人知道——那正是「静默失败」。
    paging = [c for c in all_checks
              if c['name'] in PAGING_CHECKS and c['status'] in ('warn', 'error')]
    sys.exit(1 if (n_err > 0 or paging) else 0)


if __name__ == '__main__':
    main()
