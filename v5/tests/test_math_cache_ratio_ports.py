"""`/health` 与 `/metrics` 必须共用同一个命中率口径。

钉住的缺陷（实测 2026-11-04，线上 :8002 的 `/health` 原样印出）：

```
cache.math_cache       {total_entries: 190, total_hits: 353, hit_ratio: 1.858}
cache.derivation_cache {total: 0,          total_hits: 199,  hit_ratio: 199.0}
```

同一个概念在两个出口的形状相反：`/health` 报出一个 >1 的"比率"，`/metrics` 那条
`cache_hit_ratio{cache="derivation"}` 却因为 `if d_total > 1` 里的 `d_total` 恒为 1
（读错了键名：`cache_stats()` 返回的是 `total_entries`，没有 `total`）**永远不导出**。
绑在它上面的活规则 `v5/monitoring/prometheus-alerts.yml:53` 因此一直哑着。

这里用 `v5/tests/conftest.py` 的 `math_api` 底座（按仓库单元自起实例，端口向内核要），
所以测的是**代码**，不是线上那个进程 —— 线上要等负责人重启才会跟着变。

断言只碰**结构与边界**，不比对两次快照是否相等：两个出口是先后取的，共用同一张库，
计数器只会单调增长，要求数值相等会把并发流量伪装成失败。
"""

from __future__ import annotations

import json
import urllib.request

CACHES = ("math", "derivation")
HEALTH_KEYS = {
    "math": "math_cache",
    "derivation": "derivation_cache",
}


def _get(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(f"{base}{path}", timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


def _health(base: str) -> dict:
    status, body = _get(base, "/health")
    assert status == 200
    return json.loads(body)


def _gauge(base: str) -> dict[str, float]:
    """从 textfile 里取出 cache_hit_ratio 的每个 label 值；没有该系列时返回空表。"""
    status, body = _get(base, "/metrics")
    assert status == 200
    out: dict[str, float] = {}
    for line in body.splitlines():
        if line.startswith("cache_hit_ratio{"):
            labels = line.split("{", 1)[1].split("}", 1)[0]
            value = float(line.rsplit(" ", 1)[1])
            cache = labels.split("=", 1)[1].strip('"')
            out[cache] = value
    return out


def test_health_hit_ratio_is_bounded_or_none(math_api: str) -> None:
    """比率能大于 1 就说明分母取错了量 —— 这条就是缺陷的可执行定义。"""
    cache = _health(math_api)["cache"]
    for name, key in HEALTH_KEYS.items():
        ratio = cache[key]["hit_ratio"]
        assert ratio is None or 0.0 <= ratio <= 1.0, f"{name}: hit_ratio={ratio}"


def test_health_reports_the_lookup_denominator(math_api: str) -> None:
    """`/health` 必须把分母本身也报出来，否则读数的人无从复核分子。"""
    cache = _health(math_api)["cache"]
    for key in HEALTH_KEYS.values():
        block = cache[key]
        assert {"lookups", "lookup_hits", "lookup_misses"} <= set(block)
        assert block["lookups"] == block["lookup_hits"] + block["lookup_misses"]


def test_two_exits_agree_on_when_a_value_exists(math_api: str) -> None:
    """两个出口对「有没有这个数」必须一致：`lookups==0`  ⇒ `/health` 给 None 且
    `/metrics` 不导出该 label；`lookups>0` ⇒ 两边都在。**旧代码正是这里反着来**
    （`/health` 恒给一个数、`/metrics` 恒缺 derivation 那条）。"""
    cache = _health(math_api)["cache"]
    gauges = _gauge(math_api)
    for name, key in HEALTH_KEYS.items():
        lookups = cache[key]["lookups"]
        ratio = cache[key]["hit_ratio"]
        if lookups == 0:
            assert ratio is None, f"{name}: lookups=0 却报了 {ratio}"
            assert name not in gauges, f"{name}: 无查表却导出了 gauge"
        else:
            assert ratio is not None, f"{name}: lookups={lookups} 却没有比率"
            assert name in gauges, f"{name}: 有查表但 /metrics 缺该 label"


def test_metrics_gauge_is_bounded(math_api: str) -> None:
    """导出的 gauge 同样不许 >1，且必须是 0–1（HELP 里就是这么承诺的）。"""
    for name, value in _gauge(math_api).items():
        assert 0.0 <= value <= 1.0, f"{name}: gauge={value}"


def test_gauge_appears_after_a_real_lookup(math_api: str) -> None:
    """真彩排：同一个表达式算两次 ⇒ 第二次走缓存 ⇒ `lookups>0`，gauge 必须出现。

    前面几条只钉得住「结构」。这一条把发射分支真正跑一遍：`math_cache.lookup_stats`
    是新建的空表，若 `_append_cache_hit_ratio` 的判断写反（把 None 当有值、或有值时
    不导），只有真查一次表才会暴露。两次调用都要 200，否则这条会退化成"跳过"。
    """
    body = json.dumps({"latex": "\\frac{d}{dx}(x^2)", "action": "diff"}).encode("utf-8")
    for _ in range(2):
        req = urllib.request.Request(
            f"{math_api}/tools/sympy_compute",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.status == 200

    cache = _health(math_api)["cache"]["math_cache"]
    assert cache["lookups"] > 0, "两次真调用之后 lookups 仍是 0 —— 这条线没接上缓存"
    assert "math" in _gauge(math_api), "lookups>0 却没导出 gauge"
