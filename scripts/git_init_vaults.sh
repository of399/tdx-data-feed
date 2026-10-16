#!/usr/bin/env bash
# git_init_vaults.sh (v1)
# 一键 git 化两个 Obsidian vault（仅本地，决策 3+6）。
# 步骤：
#   1. 检查 vault 存在且未 .git 化
#   2. git init -b main
#   3. config user.name/email
#   4. config core.hooksPath 指向 scripts/hooks/
#   5. chmod +x pre-commit
#   6. 检查 obsidian-git 插件启用
# 用法：bash /home/jiuben/tdx-data-feed/scripts/git_init_vaults.sh
set -uo pipefail

REPO="/home/jiuben/tdx-data-feed"
HOOKS_DIR="$REPO/scripts/hooks"
PRE_COMMIT="$HOOKS_DIR/pre-commit"

VAULT_STOCK="/home/jiuben/StockVault"
VAULT_PROJ="/home/jiuben/ObsidianVaults/tdx-data-feed"

AUTHOR_NAME="vault-bot"
AUTHOR_EMAIL="vault@local"

# 1. 依赖
for cmd in git chmod; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "✗ 缺少依赖: $cmd" >&2; exit 1; }
done

# 2. 前置检查
[[ -d "$VAULT_STOCK" ]] || { echo "✗ vault 不存在: $VAULT_STOCK" >&2; exit 1; }
[[ -d "$VAULT_PROJ" ]]  || { echo "✗ vault 不存在: $VAULT_PROJ"  >&2; exit 1; }
[[ -f "$PRE_COMMIT" ]]  || { echo "✗ pre-commit 钩子不存在: $PRE_COMMIT" >&2; exit 1; }

init_one() {
  local vault="$1" label="$2"
  echo "=== $label ($vault) ==="

  if [[ -d "$vault/.git" ]]; then
    echo "[跳过] 已是 git 仓库"
    return 0
  fi

  cd "$vault" || { echo "✗ cd 失败: $vault" >&2; return 1; }
  git init -b main 2>&1 | sed 's/^/  /'
  git config user.name "$AUTHOR_NAME"
  git config user.email "$AUTHOR_EMAIL"
  git config core.hooksPath "$HOOKS_DIR"
  echo "  user.name  = $AUTHOR_NAME"
  echo "  user.email = $AUTHOR_EMAIL"
  echo "  core.hooksPath = $HOOKS_DIR"
  return 0
}

chmod_hooks() {
  if [[ -f "$PRE_COMMIT" ]]; then
    chmod +x "$PRE_COMMIT"
    echo "  pre-commit +x: $(ls -l "$PRE_COMMIT" | awk '{print $1}')"
  fi
}

check_obsidian_git() {
  local vault="$1" label="$2"
  local cfg="$vault/.obsidian/obsidian-git.json"
  if [[ ! -f "$cfg" ]]; then
    echo "  ⚠ obsidian-git.json 不在 $vault（Phase 1.1+1.2 install 后应有）"
    return 1
  fi
  if ! grep -q "obsidian-git" "$vault/.obsidian/community-plugins.json" 2>/dev/null; then
    echo "  ⚠ obsidian-git 未在 community-plugins.json 启用（需在 Obsidian UI 启用）"
    return 1
  fi
  echo "  ✓ obsidian-git 插件已启用"
  return 0
}

echo "=== git_init_vaults.sh v1 ==="
echo "REPO: $REPO"
echo ""

init_one "$VAULT_STOCK" "StockVault"
chmod_hooks
check_obsidian_git "$VAULT_STOCK" "StockVault"
echo ""

init_one "$VAULT_PROJ" "Project vault"
chmod_hooks
check_obsidian_git "$VAULT_PROJ" "Project vault"
echo ""

# 3. 后续操作提示
cat <<'EOF'

=== 下一步（手动执行）===

# 1. 首次 commit 锁定基线
cd /home/jiuben/StockVault
git status -s | head -10     # 确认 .gitignore 生效
git add -A
git commit -m "vault: init $(date -Iseconds) StockVault baseline (post-Phase-1.2)"

cd /home/jiuben/ObsidianVaults/tdx-data-feed
git status -s | head -10
git add -A
git commit -m "vault: init $(date -Iseconds) Project baseline (post-Phase-1.2)"

# 2. 验证 pre-commit 钩子
cd /home/jiuben/StockVault
echo "test" >> 06-模板/math/T-定义.md        # 制造漂移
git add -A
git commit -m "test"                          # 应被 pre-commit 拒绝
# 恢复
git checkout -- 06-模板/math/T-定义.md

# 3. 打开 Obsidian → 启用 obsidian-git → 编辑笔记 → 等 30s
#    git log --oneline -1 应有 vault: 2026-... 新 commit
EOF
