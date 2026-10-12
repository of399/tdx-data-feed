"""quant：workspace 内数据获取子模块。

在 tdxfeed.sources 既有抽象之上封装统一 DataFrame 接口，复用而非重建。
阶段一后端固定新浪（ak.stock_zh_a_daily / SinaSource），禁用东财后端 stock_zh_a_hist（本环境被阻断）。
"""
