# Obsidian Phase 1.2 回滚方案（2026-10-15）

> 配套：`docs/obsidian_phase_1_2_plan_20261015.md`。

## 回滚总原则

Phase 1.2 不删任何 vault 内笔记/插件数据，仅重排 vault `.obsidian/*.json` 配置文件。三层防御保证可回滚：

1. **install 自动备份**：每次 install 覆盖前生成 `.bak-<时间戳>`
2. **实施前快照**：Phase 1.2 实施前手动 `cp -r .obsidian .obsidian.pre-1.2.<ts>`
3. **git 兜底**（Phase 1.4 启用后）：任何 commit 都能 `git checkout` 还原

## 回滚触发条件

满足任一即触发回滚：

- [x] `make verify-obsidian-templates` 安装后仍报漂移（源/部署未对齐）
- [x] Obsidian 启动后插件异常（dataview 报错、templater 失败、periodic-notes 路径错）
- [x] 用户主观判断 Phase 1.2 副作用不可接受

## 回滚操作

### 方案 A：单文件回滚（最常见）

```bash
# 示例：回滚 StockVault 的 community-plugins.json
ls /home/jiuben/StockVault/.obsidian/*.bak-*  # 找最近一份
mv /home/jiuben/StockVault/.obsidian/community-plugins.json.bak-20261015-130358 \
   /home/jiuben/StockVault/.obsidian/community-plugins.json

# 验证
make verify-obsidian-templates VAULT=/home/jiuben/StockVault VAULT_KIND=stockvault
```

### 方案 B：整 vault `.obsidian/` 回滚

```bash
# 找到实施前快照
TS="20261015-130358"
ls -d /home/jiuben/StockVault/.obsidian.pre-1.2.${TS}

# 整目录还原
rm -rf /home/jiuben/StockVault/.obsidian
mv /home/jiuben/StockVault/.obsidian.pre-1.2.${TS} /home/jiuben/StockVault/.obsidian

# 验证
make verify-obsidian-templates VAULT=/home/jiuben/StockVault VAULT_KIND=stockvault
```

### 方案 C：git 还原（Phase 1.4 之后）

```bash
git -C /home/jiuben/StockVault log --oneline -5         # 找 Phase 1.2 前的 commit
git -C /home/jiuben/StockVault checkout <commit-sha> -- .obsidian/

# 验证
make verify-obsidian-templates VAULT=/home/jiuben/StockVault VAULT_KIND=stockvault
```

### 方案 D：源回滚（仓库侧）

如果分 vault 源写错，从 git 历史还原（需仓库自身已 git 化）：

```bash
cd /home/jiuben/tdx-data-feed
git log --oneline -- etc/obsidian/                       # 找写错前的 commit
git checkout <commit-sha> -- etc/obsidian/stockvault/ etc/obsidian/project/

# 重新跑 install
bash scripts/install-obsidian-templates.sh --vault /home/jiuben/StockVault --kind stockvault
```

## 回滚后状态

| 维度 | 回滚后 |
|---|---|
| vault `.obsidian/*.json` | 回到 Phase 1.2 实施前状态（功能完全一致）|
| `etc/obsidian/stockvault/`、`etc/obsidian/project/` | 保留（不影响 vault 部署）|
| verify 退出码 | 0（与原状态一致）|
| Obsidian 启动 | 应正常（配置回原状）|

## 预防措施

- **写前必 inventory**：`cp <vault>/.obsidian/<file>.json /tmp/pre-1.2-<file>.json`
- **install 必 dry-run**：`bash scripts/install-obsidian-templates.sh --dry-run --vault ... --kind ...`
- **改完先 verify**：`make verify-obsidian-templates` 必须 exit 0 才算完成
- **小步推进**：先一个 vault，验证 OK 再下一个

## 责任人

- 实施：项目所有者
- 验证：项目所有者
- 紧急回滚决策：项目所有者

## 已知边角

- Phase 1.2 不会改任何笔记内容（仅改 vault `.obsidian/*.json`）
- 笔记里 `<% %>` Templater 模板不受影响
- copilot / smart-connections 启用状态保留
- Periodic-notes 历史 daily 笔记位置不变

## 边角：project vault 孤立数学文件清理

**现象**：Phase 1.2 早期 install 曾把数学模板写到 project vault（与项目 vault 是 repo-doc 镜像的设计冲突），后来改为 `kind=project` 时跳过数学段。已写入的 8 个数学文件未自动清理。

**清理命令**（用户手动执行）：

```bash
bash /home/jiuben/tdx-data-feed/scripts/install-obsidian-templates.sh \
  --vault /home/jiuben/ObsidianVaults/tdx-data-feed --vault-kind project --cleanup-orphans
```

**清理范围**：
- `06-模板/math/T-*.md` × 7
- `00_索引/总览.md` × 1
- 自动 rmdir 空目录

**影响**：删除后 verify 仍 exit 0（verify 在 `kind=project` 时已跳过数学段）；Obsidian 启动无影响（periodic-notes 路径 10-研究日志/daily，与数学无关）。
