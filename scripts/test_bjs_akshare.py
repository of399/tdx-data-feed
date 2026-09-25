#!/usr/bin/env python3
"""
test_bjs_akshare.py — 北交所 akshare 接口可行性测试
==================================================

目的：
    计划书 §5 已核实：pytdx 清单无 8/4/9 开头（北交所独立于沪深）。
    测试 akshare 是否提供北交所接口：
      - 代码清单（8xxxxx, 4xxxxx, 92xxxxx）
      - 日 K 线
      - 分钟线
      - 财务摘要

用法：
    python scripts/test_bjs_akshare.py

输出：
    scripts/bjs_akshare_test_report_<timestamp>.md
"""

import json
from datetime import datetime
from pathlib import Path

ROOT = Path("/home/jiuben/tdx-data-feed")

# 北交所代码前缀（北交所股票代码段）
BJS_PREFIXES = ("83", "87", "88", "43", "92")


def try_call(name, fn, *args, **kwargs):
    """尝试调用 akshare 接口，返回 (ok, result/error, rows)"""
    print(f"  · 测试 {name} ...", end=" ", flush=True)
    try:
        df = fn(*args, **kwargs)
        if df is None:
            print("✗ 返回 None")
            return False, "返回 None", 0
        rows = len(df) if hasattr(df, "__len__") else 0
        print(f"✓ rows={rows}, cols={list(df.columns)[:5] if hasattr(df, 'columns') else 'N/A'}")
        return True, df, rows
    except Exception as e:
        print(f"✗ {type(e).__name__}: {str(e)[:80]}")
        return False, str(e), 0


def test_bjs_interfaces():
    """测试 akshare 北交所接口可用性"""
    import akshare as ak
    results = {}

    # === 1. 代码清单（最关键）===========================================
    print("\n[1] 北交所代码清单接口")
    candidates = [
        ("stock_bj_a_spot_em", lambda: ak.stock_bj_a_spot_em()),
        ("stock_info_a_code_name (过滤)", lambda: ak.stock_info_a_code_name()),
        ("stock_zh_a_spot_em (整体)", lambda: ak.stock_zh_a_spot_em()),
    ]
    for name, fn in candidates:
        ok, res, rows = try_call(name, fn)
        results[name] = {"ok": ok, "rows": rows}
        if ok and name == "stock_info_a_code_name" and hasattr(res, "columns"):
            # 过滤北交所
            code_col = "code" if "code" in res.columns else res.columns[0]
            bjs = res[res[code_col].astype(str).str.startswith(BJS_PREFIXES)]
            print(f"    ↳ 北交所代码（{BJS_PREFIXES} 前缀）：{len(bjs)} 条")
            results[f"{name}_bjs_count"] = len(bjs)
            if len(bjs) > 0 and len(bjs) <= 20:
                print(f"    ↳ 示例：{bjs[code_col].tolist()[:10]}")

    # === 2. 日 K 线测试（单标的）========================================
    print("\n[2] 北交所日 K 接口（需先有代码）")
    # 尝试拉北交所单标的
    test_codes = [
        ("bj834021", lambda: ak.stock_zh_a_hist(symbol="834021", period="daily", adjust="")),
        ("bj430047", lambda: ak.stock_zh_a_hist(symbol="430047", period="daily", adjust="")),
    ]
    for code, fn in test_codes:
        ok, res, rows = try_call(f"stock_zh_a_hist({code})", fn)
        results[f"hist_{code}"] = {"ok": ok, "rows": rows}

    # === 3. 财务接口 ===================================================
    print("\n[3] 北交所财务接口")
    finance_candidates = [
        ("stock_financial_report_sina (整体)", lambda: ak.stock_financial_report_sina(stock="bj834021")),
    ]
    for name, fn in finance_candidates:
        ok, res, rows = try_call(name, fn)
        results[name] = {"ok": ok, "rows": rows}

    # === 4. 检查 akshare 是否有北交所专用接口 ===========================
    print("\n[4] 检查 akshare 接口名包含 'bj' 或 'bjs' 的所有接口")
    bj_interfaces = [attr for attr in dir(ak) if any(k in attr.lower() for k in ("bj", "bjs", "bjse"))]
    print(f"    发现 {len(bj_interfaces)} 个含 'bj' 的接口：")
    for i, name in enumerate(bj_interfaces[:15]):
        print(f"      {i+1}. {name}")
    if len(bj_interfaces) > 15:
        print(f"      ... 还有 {len(bj_interfaces) - 15} 个")
    results["bj_interfaces_count"] = len(bj_interfaces)
    results["bj_interfaces_sample"] = bj_interfaces[:15]

    return results


def main():
    print("=" * 70)
    print("北交所 akshare 接口可行性测试")
    print("=" * 70)

    try:
        import akshare as ak
        print(f"\nakshare 版本：{ak.__version__}")
    except ImportError:
        print("❌ akshare 未安装。请先：pip install akshare")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = test_bjs_interfaces()

    # 汇总判断
    has_bj_codes = results.get("stock_info_a_code_name_bjs_count", 0) > 0
    has_hist = any(k.startswith("hist_bj") and v.get("ok") for k, v in results.items())

    verdict = "✅ 北交所接口可用" if has_bj_codes and has_hist else (
        "⚠️ 北交所接口部分可用，需手工维护代码清单" if has_bj_codes else
        "❌ akshare 无北交所接口，需用其他方案（手动清单 / Wind / 聚宽等）"
    )

    print("\n" + "=" * 70)
    print(f"结论：{verdict}")
    print("=" * 70)

    # 输出报告
    md = f"""# 北交所 akshare 接口测试报告（{ts}）

## 结论

**{verdict}**

## 测试结果

### 代码清单
- `stock_info_a_code_name` 过滤北交所（{BJS_PREFIXES} 前缀）：**{results.get('stock_info_a_code_name_bjs_count', 0)}** 条
- akshare 含 'bj' 的接口数：{results.get('bj_interfaces_count', 0)} 个

### 日 K 接口测试
"""
    for code in ("bj834021", "bj430047"):
        r = results.get(f"hist_{code}", {})
        md += f"- `{code}`：{'✓ 可用，rows=' + str(r.get('rows', 0)) if r.get('ok') else '✗ 失败：' + str(r.get('rows', 'N/A'))}\n"

    md += """
## 下一步

"""
    if has_bj_codes and has_hist:
        md += "- ✅ akshare 北交所接口可用，建议在 symbols.py 中加入 `83/87/88/43/92` 前缀的代码段支持\n"
        md += "- 在 `tdxfeed/sources/akshare_source.py` 中增加北交所专用接口调用分支\n"
    elif has_bj_codes:
        md += "- ⚠️ akshare 仅提供北交所代码清单，K 线需用其他源（如新浪 bj 前缀 / 手工导入）\n"
        md += "- 建议维护一份 `bjs_codes.csv` 手工清单，每周更新\n"
    else:
        md += "- ❌ akshare 暂无北交所完整接口\n"
        md += "- 替代方案：\n"
        md += "  1. 手工维护 `bjs_codes.csv`（每月底从北交所官网下载）\n"
        md += "  2. 用 pytdx `get_security_list` 在不同 market 试连\n"
        md += "  3. 接入其他数据源（Wind / 聚宽 / 同花顺 iFinD）\n"

    md += f"\n\n## 原始结果\n\n```json\n{json.dumps({k: v for k, v in results.items()}, ensure_ascii=False, indent=2, default=str)}\n```\n"

    out_md = ROOT / f"scripts/test_bjs_akshare_report_{ts}.md"
    out_md.write_text(md, encoding="utf-8")
    out_json = ROOT / f"scripts/test_bjs_akshare_report_{ts}.json"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[MD] {out_md}")
    print(f"[JSON] {out_json}")


if __name__ == "__main__":
    main()
