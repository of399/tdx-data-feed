"""训练 Dashboard HTML 生成器
综合：
- v5/v6 loss 曲线对比图
- v5/v6 训练统计
- 翻倍样本分析 Top 10
- 方案 D 模型 AUC

输出：single HTML file (内嵌 base64 图)
"""
import base64
from datetime import datetime
from pathlib import Path

import pandas as pd

PARQUET = Path('/home/jiuben/tdx-data-feed/data/double_up')
OUT = Path('/home/jiuben/tdx-data-feed/data/double_up/training_dashboard.html')

V5_PNG = Path('/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v5-simple/loss_curve.png')
V6_PNG = Path('/home/jiuben/tdx-data-feed/train/output/qwen3-14b-tdx-v6-gpu1/loss_curve.png')


def img_to_b64(fp):
    if not fp.exists():
        return ''
    return base64.b64encode(fp.read_bytes()).decode()


def main():
    v1_by = pd.read_csv(PARQUET / 'double_up_2020_2026_by_code.csv', dtype={'code': str})
    top10 = v1_by.head(10)
    year_dist = pd.read_csv(PARQUET / 'double_up_2020_2026_year_dist.csv')

    v5_b64 = img_to_b64(V5_PNG)
    v6_b64 = img_to_b64(V6_PNG)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>tdx-data-feed 训练 Dashboard</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1400px; margin: 0 auto; padding: 20px; background: #fafafa; }}
h1 {{ color: #1a1a1a; border-bottom: 3px solid #0066cc; padding-bottom: 10px; }}
h2 {{ color: #0066cc; margin-top: 30px; border-left: 4px solid #0066cc; padding-left: 10px; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
th {{ background: #0066cc; color: white; padding: 8px; text-align: left; }}
td {{ padding: 8px; border-bottom: 1px solid #ddd; }}
tr:hover {{ background: #f5f5f5; }}
.metric {{ display: inline-block; padding: 15px 25px; margin: 10px; background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.metric .num {{ font-size: 32px; font-weight: bold; color: #0066cc; }}
.metric .label {{ color: #666; font-size: 14px; }}
.chart {{ background: white; padding: 15px; border-radius: 8px; margin: 15px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
.alert {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 10px; margin: 10px 0; border-radius: 4px; }}
.success {{ background: #d4edda; border-left: 4px solid #28a745; padding: 10px; margin: 10px 0; border-radius: 4px; }}
</style>
</head>
<body>
<h1>📊 tdx-data-feed 训练 Dashboard</h1>
<p>生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

<h2>1. v5/v6 训练结果对比</h2>

<div>
  <div class="metric"><div class="num">1188</div><div class="label">v5 step</div></div>
  <div class="metric"><div class="num">984</div><div class="label">v6 step</div></div>
  <div class="metric"><div class="num">0.1388</div><div class="label">v5 train_loss</div></div>
  <div class="metric"><div class="num">0.1150</div><div class="label">v6 train_loss ⬇</div></div>
  <div class="metric"><div class="num">0.1958</div><div class="label">v5 eval_loss ⬇</div></div>
  <div class="metric"><div class="num">0.2883</div><div class="label">v6 eval_loss</div></div>
  <div class="metric"><div class="num">12656</div><div class="label">v5 数据</div></div>
  <div class="metric"><div class="num">5238</div><div class="label">v6 数据</div></div>
</div>

<h3>Loss 曲线对比</h3>
<div class="chart">
  <p><b>v5 (12656 条数据, 3 epoch, 单卡 GPU 0)</b></p>
  <img src="data:image/png;base64,{v5_b64}" style="max-width:100%;">
</div>
<div class="chart">
  <p><b>v6 (5238 条精选, 3 epoch, 单卡 GPU 1)</b></p>
  <img src="data:image/png;base64,{v6_b64}" style="max-width:100%;">
</div>

<div class="alert">
  <b>🔍 关键观察：</b>
  <ul>
    <li>v6 train_loss 最低 (0.115) → 精选数据效果显著</li>
    <li>v5 eval_loss 更低 (0.196 vs 0.288) → v6 略有过拟合</li>
    <li>v5 数据多 2.4x → 训练时间多 3.5x（6.6h vs 1.9h）</li>
  </ul>
</div>

<h2>2. 翻倍样本分析（v1 全 8035 只 + 北交所）</h2>

<div>
  <div class="metric"><div class="num">22972</div><div class="label">翻倍样本数</div></div>
  <div class="metric"><div class="num">1942</div><div class="label">妖股数</div></div>
  <div class="metric"><div class="num">24.2%</div><div class="label">命中率</div></div>
  <div class="metric"><div class="num">9</div><div class="label">北交所 Top 20</div></div>
</div>

<h3>Top 10 妖股</h3>
<table>
<tr><th>排名</th><th>代码</th><th>名称</th><th>市场</th><th>翻倍次数</th><th>最大涨幅</th></tr>
"""
    for i, r in top10.iterrows():
        market = '北交所' if r['code'].startswith('bj') else ('创业板/科创板' if (r['code'].startswith('sz3') or r['code'].startswith('sh688')) else '沪深主板')
        html += f'<tr><td>{i+1}</td><td>{r["code"]}</td><td>{r["name"]}</td><td>{market}</td><td>{int(r["hit_count"])}</td><td>{r["max_pct"]:.1f}%</td></tr>\n'

    html += """
</table>

<h3>年份分布</h3>
<table>
<tr><th>年份</th><th>翻倍次数</th><th>涉及标的</th><th>平均涨幅</th><th>最大涨幅</th></tr>
"""
    for _, r in year_dist.iterrows():
        html += f'<tr><td>{int(r["year"])}</td><td>{int(r["hit_count"])}</td><td>{int(r["unique_codes"])}</td><td>{r["avg_pct"]:.1f}%</td><td>{r["max_pct"]:.1f}%</td></tr>\n'

    html += r"""
</table>

<h2>3. v1 vs v3 偏差分析（不复权 vs 前复权）</h2>

<div>
  <div class="metric"><div class="num">3.7%</div><div class="label">v1 假阳性</div></div>
  <div class="metric"><div class="num">0.8%</div><div class="label">v2 漏判</div></div>
  <div class="metric"><div class="num">0.3%</div><div class="label">共同 \|差异\|>5pct</div></div>
</div>

<div class="success">
  <b>✅ 结论：</b>v1 不复价偏差 &lt;5%；重要决策请用 v3（前复权）
</div>

<h2>4. 方案 D：翻倍前 5 日信号识别（LightGBM）</h2>

<div>
  <div class="metric"><div class="num">0.8144</div><div class="label">Test AUC ⭐</div></div>
  <div class="metric"><div class="num">83.15%</div><div class="label">threshold=0.7 精度</div></div>
  <div class="metric"><div class="num">89.40%</div><div class="label">threshold=0.8 精度</div></div>
  <div class="metric"><div class="num">13023</div><div class="label">训练样本</div></div>
</div>

<h3>Top 5 关键信号</h3>
<table>
<tr><th>排名</th><th>特征</th><th>重要性</th><th>含义</th></tr>
<tr><td>1</td><td><code>prev_close</code></td><td>488</td><td>T-1 收盘价（价格水平）</td></tr>
<tr><td>2</td><td><code>avg_amount_5d</code></td><td>485</td><td>5 日均成交额</td></tr>
<tr><td>3</td><td><code>close_pos_5d</code></td><td>355</td><td>T-1 收盘价在 5 日 K 线位置</td></tr>
<tr><td>4</td><td><code>volatility_5d</code></td><td>345</td><td>5 日波动率</td></tr>
<tr><td>5</td><td><code>turnover_proxy</code></td><td>328</td><td>T-1 换手代理</td></tr>
</table>

<div class="alert">
  <b>💡 关键洞察：</b>
  <ul>
    <li><b>价格水平 + 量能</b> 才是真正的"妖股基因"</li>
    <li><b>T-1 收盘在 5 日 K 线高位</b> = 洗盘结束信号</li>
    <li>涨停类特征不重要 → 妖股启动前未必连续涨停</li>
  </ul>
</div>

<h2>5. v5 vs v6 输出风格（30 只核心股对比）</h2>

<table>
<tr><th>维度</th><th>v5</th><th>v6</th><th>胜者</th></tr>
<tr><td>训练数据</td><td>12656</td><td>5238</td><td>v5（多）</td></tr>
<tr><td>eval_loss</td><td><b>0.1958</b></td><td>0.2883</td><td>v5（泛化好）</td></tr>
<tr><td>建议风格</td><td>保守</td><td>积极</td><td>取决于业务</td></tr>
<tr><td>字段完整度</td><td>5/5</td><td>4.0/5</td><td>v5</td></tr>
<tr><td>Token 数</td><td>257</td><td><b>111</b></td><td>v6（精简）</td></tr>
<tr><td>输出相似度</td><td colspan="2">12%</td><td>差异大</td></tr>
</table>

<h2>6. 当前状态</h2>
<ul>
  <li>✅ v5 训练完成 · 部署到 Ollama</li>
  <li>✅ v6 训练完成 · 部署到 Ollama</li>
  <li>🔄 v6_eval (v6 vs v4 fast) · 后台跑</li>
  <li>✅ v5_v6_compare (v5 vs v6) · 30/30 完成</li>
  <li>✅ 方案 D 模型 · AUC 0.8144 · 已保存</li>
  <li>⏸ v7 训练（合并数据）· 等 v6_eval 完成</li>
  <li>❌ v8 LoRA fusion · rank 不同无法直接融合</li>
</ul>

<hr>
<p style="color:#888; font-size:12px;">
生成自 tdx-data-feed/scripts/gen_dashboard.py
</p>
</body>
</html>
"""
    OUT.write_text(html, encoding='utf-8')
    print(f'✓ Dashboard: {OUT} ({len(html)/1024:.1f} KB)')


if __name__ == '__main__':
    main()
