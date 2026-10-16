#!/usr/bin/env bash
# archive_obsidian_annual.sh
# 每年 1/1 把上一年的 08-报告/*.md zip 到 08-报告/archive/YYYY.zip
# 由 systemd user timer archive-obsidian-annual.timer 触发（OnCalendar=*-01-01T01:00:00）
# 决策依据：docs/obsidian_upgrade_plan_20261015.md 决策 5（年度保留）
set -uo pipefail

REPORTS="/home/jiuben/ObsidianVaults/tdx-data-feed/08-报告"
ARCHIVE="$REPORTS/archive"
TS="$(date +%Y)"
PREV_YEAR=$((TS - 1))

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

# 1. 目标目录
[[ -d "$REPORTS" ]] || { echo "✗ 报告目录不存在: $REPORTS" >&2; exit 1; }
mkdir -p "$ARCHIVE"

# 2. 选 [PREV_YEAR-01-01, PREV_YEAR-12-31 23:59:59] mtime 范围内的 .md
#    注意：find -newermt 起始为该日 00:00:00；! -newermt 终止为次日 00:00:00（避免 23:59:59 时区/精度坑）
NEXT_YEAR=$((PREV_YEAR + 1))
mapfile -t FILES < <(find "$REPORTS" -maxdepth 1 -type f -name "*.md" \
  -newermt "$PREV_YEAR-01-01" \
  ! -newermt "$NEXT_YEAR-01-01" \
  2>/dev/null | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "✓ $PREV_YEAR 无报告可归档"
  exit 0
fi

echo "候选归档文件（$PREV_YEAR）: ${#FILES[@]} 份"
for f in "${FILES[@]}"; do
  echo "  - $(basename "$f")"
done

ZIP="$ARCHIVE/$PREV_YEAR.zip"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] 将创建: $ZIP"
  echo "[dry-run] 将包含: ${#FILES[@]} 份 .md"
  exit 0
fi

# 3. 打包
cd "$REPORTS" || exit 1
zip -j "$ZIP" "${FILES[@]##*/}" >/dev/null

# 4. 校验 zip
if [[ -f "$ZIP" ]] && unzip -l "$ZIP" >/dev/null 2>&1; then
  echo "✓ $PREV_YEAR 已归档: $ZIP ($(du -h "$ZIP" | cut -f1))"
  echo "  含 $(unzip -l "$ZIP" | tail -1 | awk '{print $2}') 个文件"
  exit 0
else
  echo "✗ zip 失败或校验失败: $ZIP" >&2
  exit 1
fi
