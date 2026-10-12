#!/bin/bash
# systemd ExecStartPre 数据新鲜度守卫：50 抽样中 >20% 末日期早于 today-2d 则 exit 2
exec /home/jiuben/tdx-data-feed/venv/bin/python -c "from tdxfeed.freshness import require_fresh; require_fresh(max_age_days=2)" || exit 2
