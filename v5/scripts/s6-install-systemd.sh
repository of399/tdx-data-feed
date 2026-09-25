#!/usr/bin/env bash
# ============================================================
# v5-VE1-M2 · S6 systemd 自启安装脚本（待 sudo 授权）
#
# 用法：
#   chmod +x v5/scripts/s6-install-systemd.sh
#   sudo ./v5/scripts/s6-install-systemd.sh
#
# 卸载：
#   sudo ./v5/scripts/s6-install-systemd.sh --uninstall
# ============================================================
set -euo pipefail

V5_ROOT="/home/jiuben/tdx-data-feed/v5"
SYSTEMD_SRC="$V5_ROOT/systemd"
SYSTEMD_DST="/etc/systemd/system"

UNINSTALL=false
if [[ "${1:-}" == "--uninstall" ]]; then
  UNINSTALL=true
fi

SERVICES=(
  "ollama.service"
  "mcp-llm-ollama.service"
  "vllm-qwen3-judge.service"
)

if [[ "$UNINSTALL" == true ]]; then
  echo "[S6/uninstall] 停止 + 禁用 + 删除 unit"
  for svc in "${SERVICES[@]}"; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
    rm -f "$SYSTEMD_DST/$svc"
    echo "  - $svc 卸载"
  done
  systemctl daemon-reload
  echo "[S6/uninstall] 完成"
  exit 0
fi

# 1. 拷贝 unit 文件
echo "[S6/install] 拷贝 systemd unit 到 $SYSTEMD_DST"
for svc in "${SERVICES[@]}"; do
  if [[ ! -f "$SYSTEMD_SRC/$svc" ]]; then
    echo "  ❌ 缺失源文件：$SYSTEMD_SRC/$svc" >&2
    exit 1
  fi
  cp -v "$SYSTEMD_SRC/$svc" "$SYSTEMD_DST/$svc"
done

# 2. 校验 [Install] 段（README Q9）
echo "[S6/install] 校验 [Install] 段"
for svc in "${SERVICES[@]}"; do
  if ! grep -q "^\[Install\]" "$SYSTEMD_DST/$svc"; then
    echo "  ⚠️  $svc 缺 [Install] 段，自动追加"
    cat >> "$SYSTEMD_DST/$svc" <<'EOF'

[Install]
WantedBy=multi-user.target
EOF
  fi
done

# 3. daemon-reload + enable --now
echo "[S6/install] daemon-reload + enable --now"
systemctl daemon-reload

for svc in "${SERVICES[@]}"; do
  echo "  → systemctl enable --now $svc"
  systemctl enable --now "$svc" || echo "    ⚠️  $svc 启动失败（可能 GPU/依赖未就绪）"
done

# 4. 状态汇总
echo
echo "[S6/install] 当前状态："
for svc in "${SERVICES[@]}"; do
  STATUS=$(systemctl is-active "$svc" 2>&1 || echo "unknown")
  ENABLED=$(systemctl is-enabled "$svc" 2>&1 || echo "unknown")
  echo "  $svc : active=$STATUS enabled=$ENABLED"
done

echo
echo "[S6/install] 完成。可通过 'systemctl status <svc>' 查看详情。"