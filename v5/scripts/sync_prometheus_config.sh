#!/usr/bin/env bash
# sync_prometheus_config.sh — 消除线上/仓库 Prometheus 配置漂移（补齐 canary job）
#
# 为什么需要 sudo：/etc/prometheus/prometheus.yml 属主 root，普通用户不可写。
# 用法：sudo bash v5/scripts/sync_prometheus_config.sh
# 副作用：备份旧配置到 /etc/prometheus/prometheus.yml.bak.<ts>，并热重载 Prometheus。
set -euo pipefail

SRC="/home/jiuben/tdx-data-feed/v5/monitoring/prometheus.yml"
DST="/etc/prometheus/prometheus.yml"

[[ -f "$SRC" ]] || { echo "源配置不存在: $SRC" >&2; exit 1; }
[[ -f "$DST" ]] || { echo "目标配置不存在: $DST" >&2; exit 1; }

if grep -q "job_name: *'canary'" "$DST"; then
    echo "[skip] canary job 已存在于线上配置，无需修改"
else
    cp -a "$DST" "$DST.bak.$(date +%Y%m%d%H%M%S)"
    echo "[backup] 已备份旧配置到 $DST.bak.*"
    install -m 644 -o root -g root "$SRC" "$DST"
    echo "[sync] 已同步仓库版配置（补 canary job @ 127.0.0.1:9101）"
fi

# 语法校验
if command -v promtool >/dev/null 2>&1; then
    promtool check config "$DST"
else
    echo "[warn] 未找到 promtool，跳过校验"
fi

# 热重载（安装脚本已开启 --web.enable-lifecycle）
if curl -sf -X POST http://127.0.0.1:9090/-/reload >/dev/null; then
    echo "[reload] Prometheus 配置已热重载"
else
    echo "[warn] 热重载失败，请手动: sudo systemctl reload prometheus"
fi

echo
echo "验证: curl -s '127.0.0.1:9090/api/v1/targets' | grep -o '\"job\":\"[^\"]*\"' | sort -u"
