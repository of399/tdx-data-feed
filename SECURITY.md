# SECURITY.md · tdx-data-feed 安全策略

> 本文档说明 tdx-data-feed 的**安全策略**、**漏洞报告流程**、**安全最佳实践**、**支持的版本**以及**合规性要求**。
>
> 最后更新：2026-09-27 · 维护者：jiuben

---

## 目录

- [1. 安全策略概述](#1-安全策略概述)
- [2. 支持的版本](#2-支持的版本)
- [3. 漏洞报告流程](#3-漏洞报告流程)
- [4. 安全最佳实践](#4-安全最佳实践)
- [5. 数据保护](#5-数据保护)
- [6. 网络安全](#6-网络安全)
- [7. 系统加固](#7-系统加固)
- [8. 凭据管理](#8-凭据管理)
- [9. 日志与审计](#9-日志与审计)
- [10. 合规要求](#10-合规要求)
- [11. 应急响应](#11-应急响应)
- [12. 联系方式](#12-联系方式)

---

## 1. 安全策略概述

### 1.1 核心原则

1. **纵深防御**：多层安全控制，无单一失效点
2. **最小权限**：每个组件仅获必需的权限
3. **默认安全**：默认配置即安全配置
4. **可审计性**：所有关键操作有日志可追溯
5. **快速响应**：发现漏洞 24 小时内响应

### 1.2 安全边界

```
┌────────────────────────────────────────────────┐
│                外部世界                         │
│  ┌──────────────┐         ┌──────────────┐    │
│  │  GitHub API  │         │ baostock/ak  │    │
│  └──────────────┘         └──────────────┘    │
└───────────────┬────────────────────┬───────────┘
                │                    │
       ┌────────▼────────┐  ┌────────▼─────────┐
       │  HTTPS / 加密   │  │   公开数据 API   │
       └────────┬────────┘  └────────┬─────────┘
                │                    │
┌───────────────▼────────────────────▼────────────┐
│              系统边界（systemd user）             │
│  ┌──────────────┐         ┌──────────────┐      │
│  │  Ollama API  │         │ Local Ollama │      │
│  │  (11434)     │         │  (本地套接字) │      │
│  └──────────────┘         └──────────────┘      │
│  ┌──────────────┐         ┌──────────────┐      │
│  │  数据文件    │         │  GGUF 模型   │      │
│  │  (本地权限)  │         │  (本地权限)  │      │
│  └──────────────┘         └──────────────┘      │
└─────────────────────────────────────────────────┘
                │
                ▼
       ┌────────────────┐
       │  仅 localhost  │
       │   可访问       │
       └────────────────┘
```

### 1.3 信任模型

| 组件 | 信任级别 | 验证方式 |
|---|---|---|
| 数据源 API | 半信任 | HTTPS + 数据 schema 校验 |
| 用户输入 | 不信任 | argparse + 类型检查 |
| 文件系统 | 信任 | 文件权限 + chown |
| Ollama HTTP | 半信任 | localhost only + 可选 token |
| GitHub MCP | 半信任 | Docker 沙箱 + 仅 PAT 权限 |

---

## 2. 支持的版本

### 2.1 版本支持矩阵

| 版本 | 状态 | 安全更新 | EOL |
|---|---|:---:|---|
| 1.0.x（当前）| ✅ 活跃 | ✅ | - |
| 0.9.x | ⚠ 维护 | ✅ 仅严重 | 2026-12-31 |
| 0.8.x | ❌ 弃用 | ❌ | 2026-09-30 |
| < 0.8 | ❌ 不支持 | ❌ | - |

### 2.2 更新策略

- **关键漏洞**：24 小时内发布补丁
- **严重漏洞**：7 天内发布补丁
- **一般漏洞**：下一个 minor 版本
- **EOL 政策**：EOL 前 90 天公告

---

## 3. 漏洞报告流程

### 3.1 报告渠道

| 优先级 | 渠道 | 响应时间 |
|---|---|---|
| 🔴 严重 | security@jiuben.com | 24h |
| 🟡 一般 | GitHub Issue（私有）| 7 天 |
| 🟢 低 | GitHub Discussion | 14 天 |

### 3.2 报告模板

```markdown
## 漏洞报告

### 标题
[简洁描述，如 "predict.py 路径遍历漏洞"]

### 严重性
- [ ] 🔴 严重（数据泄露 / RCE / 权限提升）
- [ ] 🟡 一般（信息泄露 / DoS / 越权读）
- [ ] 🟢 低（最佳实践违反）

### 描述
详细描述漏洞的性质和影响。

### 影响范围
- 受影响版本：[如 0.9.x]
- 受影响组件：[如 scripts/predict.py]
- 攻击向量：[网络 / 本地 / 物理]

### 复现步骤
1. 执行 `python scripts/predict.py --codes $(恶意输入)`
2. ...
3. 观察到 [异常行为]

### 预期 vs 实际
- 预期：[正常处理]
- 实际：[被攻击]

### 缓解措施（如有）
临时缓解方案。

### 环境
- OS: Ubuntu 24.04
- Python: 3.14
- 项目 commit: abc123

### 联系信息
- 邮箱：reporter@example.com
- 是否需要致谢：是 / 否
```

### 3.3 处理流程

```
1. 报告接收 (T+0)
   ↓
2. 初步评估 (T+24h) - 确认是否为真实漏洞
   ↓
3. 严重性分级 (T+48h) - 严重/一般/低
   ↓
4. 复现与根因分析 (T+72h)
   ↓
5. 修复开发 (T+1~7 天，依赖严重性)
   ↓
6. 内部测试 + 报告者验证 (T+8)
   ↓
7. 发布补丁 (T+9)
   ↓
8. 公开发布 Security Advisory (T+10)
   ↓
9. 致谢 + 公开时间表 (T+14)
```

### 3.4 披露政策

- **协调披露**：发现后通知项目维护者，等待修复后公开
- **窗口期**：90 天（Google Project Zero 标准）
- **公开例外**：漏洞在被利用时立即公开
- **致谢**：所有报告者可在 Security Advisory 中致谢

---

## 4. 安全最佳实践

### 4.1 输入验证

```python
# ✅ 推荐：白名单 + 类型检查 + 范围验证
def process_codes(codes: str) -> list:
    if not codes:
        return []
    result = []
    for c in codes.split(','):
        c = c.strip().zfill(6)
        if not c.isdigit() or len(c) != 6:
            raise ValueError(f"Invalid code: {c}")
        if c.startswith('920') and not ALLOW_BJ:
            continue  # 跳过北交所
        result.append(c)
    return result

# ❌ 不推荐：直接使用用户输入
def process_codes_bad(codes: str):
    return codes.split(',')  # 无验证
```

### 4.2 命令执行

```python
# ✅ 推荐：使用 subprocess + shell=False + 参数列表
import subprocess
result = subprocess.run(
    ['python', 'scripts/predict.py', '--codes', code],
    capture_output=True, text=True, timeout=30
)

# ❌ 危险：shell=True + 字符串拼接
result = subprocess.run(f'python predict.py --codes {user_input}', shell=True)
```

### 4.3 文件操作

```python
# ✅ 推荐：Path + 路径白名单
from pathlib import Path
SAFE_ROOT = Path('/home/jiuben/tdx-data-feed/data')

def safe_read(rel_path: str) -> str:
    abs_path = (SAFE_ROOT / rel_path).resolve()
    if not str(abs_path).startswith(str(SAFE_ROOT)):
        raise ValueError(f"Path traversal detected: {rel_path}")
    return abs_path.read_text()

# ❌ 危险：直接拼接路径
def read_unsafe(rel_path):
    return open(f'/data/{rel_path}').read()  # 可被 ../ 绕过
```

### 4.4 反序列化

```python
# ✅ 推荐：白名单字段
df = pd.read_parquet(fp, columns=['date', 'close'])

# ⚠ 中等：完整读取 + schema 校验
df = pd.read_parquet(fp)
assert set(df.columns) >= {'date', 'close'}

# ❌ 危险：pickle.load 反序列化不可信源
import pickle
data = pickle.load(open(fp, 'rb'))  # 可执行任意代码
```

### 4.5 错误处理

```python
# ✅ 推荐：捕获具体异常 + 记录 + 不泄露内部信息
try:
    df = pd.read_parquet(fp)
except FileNotFoundError:
    logger.warning(f"Missing file: {fp.name}")
    return None
except Exception as e:
    logger.error(f"Read failed: {fp.name}: {e}")
    return None

# ❌ 危险：暴露堆栈跟踪给用户
try:
    df = pd.read_parquet(fp)
except Exception as e:
    return f"Error: {traceback.format_exc()}"  # 信息泄露
```

---

## 5. 数据保护

### 5.1 数据分类

| 级别 | 类型 | 示例 | 保护 |
|---|---|---|---|
| 🔴 机密 | 凭据 | API Token | 仅本地 + 加密 |
| 🟠 敏感 | 交易记录 | journal.csv | 文件权限 600 |
| 🟡 内部 | 模型权重 | GGUF 文件 | 文件权限 644 |
| 🟢 公开 | 日 K | parquet/daily | 文件权限 644 |

### 5.2 数据流保护

```
数据源 (baostock/akshare)         [公开]
    ↓ HTTPS
本地磁盘 (parquet/daily)          [内部] 文件权限 644
    ↓ 仅本地进程
特征计算 (predict.py)              [内部]
    ↓ 仅内存
LightGBM 模型推理                 [内部] 无持久化
    ↓ HTTP (localhost)
应用层 (plan_e_journal.py)         [内部]
    ↓ 文件权限 600
journal.csv (交易记录)             [敏感]
```

### 5.3 数据保留策略

| 数据类型 | 保留期 | 删除方式 |
|---|---|---|
| 日 K | 永久 | 备份后归档 |
| 复权因子 | 永久 | 同上 |
| 翻倍样本 | 永久 | 同上 |
| 预测结果 | 90 天 | 自动清理 |
| 交易记录 | 7 年（合规）| 加密归档 |
| 临时日志 | 7 天 | 自动清理 |
| 备份文件 | 90 天 | 自动清理 |

### 5.4 备份加密

```bash
# 备份加密示例
tar czf - data/ | gpg --symmetric --cipher-algo AES256 \
    --output backup-$(date +%Y%m%d).tar.gz.gpg

# 解密
gpg --decrypt backup-20260927.tar.gz.gpg | tar xzf -
```

---

## 6. 网络安全

### 6.1 防火墙配置

```bash
# 仅允许 localhost 访问 Ollama API
sudo iptables -A INPUT -p tcp --dport 11434 -s 127.0.0.1 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 11434 -j DROP

# 或用 ufw
sudo ufw allow from 127.0.0.1 to any port 11434
sudo ufw deny 11434
```

### 6.2 Ollama API 加固

```bash
# 方案 1：仅监听本地
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"

# 方案 2：使用 Nginx 反向代理 + Token
# /etc/nginx/sites-available/ollama
location /api/ {
    auth_basic "Ollama API";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:11434/api/;
}
```

### 6.3 HTTPS / TLS

```bash
# 自签证书（dev）
openssl req -x509 -nodes -days 365 \
    -newkey rsa:2048 \
    -keyout /etc/ssl/private/ollama.key \
    -out /etc/ssl/certs/ollama.crt

# Let's Encrypt（prod）
sudo certbot --nginx -d api.yourdomain.com
```

### 6.4 数据传输

- ✅ 所有外部 API：HTTPS only
- ✅ 本地 API：HTTP（仅 localhost）+ 可选 mTLS
- ❌ 禁止：HTTP over 公网

---

## 7. 系统加固

### 7.1 文件权限

```bash
# 数据目录
chmod 755 data/
chmod 644 data/parquet/daily/*
chmod 644 data/adj_factor/*

# 交易记录（敏感）
chmod 700 data/plan_e/live/
chmod 600 data/plan_e/live/journal.csv
chmod 600 data/plan_e/live/positions.json

# 模型权重
chmod 644 /home/jiuben/models/Qwen3-14B-tdx-*.gguf
chmod 755 /home/jiuben/models/

# 日志
chmod 644 logs/*.log
chmod 600 logs/canary_history.jsonl
```

### 7.2 进程隔离

```bash
# systemd 服务隔离
[Service]
# 不能获取新权限
NoNewPrivileges=yes

# 私有 /tmp
PrivateTmp=yes

# 私有网络命名空间（可选）
PrivateNetwork=yes

# 只读文件系统（除数据目录）
ProtectSystem=strict
ReadWritePaths=/home/jiuben/tdx-data-feed/data
```

### 7.3 用户权限

```bash
# 运行用户
User=jiuben
Group=jiuben

# 禁止 root
User=jiuben

# 仅必要能力（Capabilities）
CapabilityBoundingSet=
AmbientCapabilities=
```

### 7.4 内核参数

```bash
# /etc/sysctl.d/99-security.conf

# 禁用 IP 转发
net.ipv4.ip_forward = 0

# 启用 SYN Cookie
net.ipv4.tcp_syncookies = 1

# 忽略 ICMP 重定向
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0

# 忽略源路由
net.ipv4.conf.all.accept_source_route = 0
```

---

## 8. 凭据管理

### 8.1 当前凭据清单

| 凭据 | 用途 | 存储位置 |
|---|---|---|
| GitHub PAT | MCP GitHub | `~/.codebuddy/mcp.json`（应加密）|
| Ollama 无认证 | API 访问 | localhost only |
| 无 DB 密码 | 无数据库 | - |

### 8.2 凭据存储最佳实践

```bash
# ✅ 推荐：使用系统 keyring
python3 -c "import keyring; keyring.set_password('akshare', 'token', 'xxx')"

# ✅ 推荐：使用 .env 文件 + gitignore
echo "GITHUB_TOKEN=xxx" > .env
echo ".env" >> .gitignore

# ❌ 禁止：硬编码在脚本中
GITHUB_TOKEN = "ghp_xxx"  # 永远不要这样写

# ❌ 禁止：提交到 git
git add config.py  # 如果包含 token
```

### 8.3 凭据轮换

| 凭据 | 轮换周期 | 负责人 |
|---|---|---|
| GitHub PAT | 90 天 | jiuben |
| Ollama Token | 365 天 | jiuben |
| systemd user keyring | N/A | 系统 |

### 8.4 凭据泄露应急

1. **立即吊销**：`gh auth revoke` 或 `Settings → Developer settings → PAT`
2. **审计日志**：`gh audit-log` 或 GitHub Events
3. **生成新凭据**：新建 PAT，权限最小化
4. **更新配置**：`~/.codebuddy/mcp.json`
5. **通知团队**：通过加密渠道
6. **复盘**：48 小时内完成 post-mortem

---

## 9. 日志与审计

### 9.1 日志分类

| 类型 | 路径 | 保留 | 权限 |
|---|---|---|---|
| 应用日志 | `logs/*.log` | 7 天 | 644 |
| Canary 历史 | `logs/canary_history.jsonl` | 30 天 | 600 |
| 交易记录 | `data/plan_e/live/journal.csv` | 7 年 | 600 |
| 错误日志 | `logs/*.err` | 30 天 | 644 |
| systemd journal | journalctl | 系统管理 | root |

### 9.2 日志内容要求

**应该记录**：
- ✅ 关键操作（开仓 / 平仓 / 模型加载）
- ✅ 异常事件（API 失败 / OOM / 超时）
- ✅ 配置变更（Modelfile 修改）
- ✅ 访问审计（API 调用）

**不应记录**：
- ❌ 用户密码 / Token
- ❌ 完整请求体（含敏感数据）
- ❌ 完整的堆栈跟踪（生产环境）

### 9.3 审计检查清单

```bash
# 每月审计
- [ ] 异常登录检测
- [ ] API 调用统计
- [ ] 文件权限审计（find -perm 检查）
- [ ] systemd 服务状态
- [ ] 磁盘空间
- [ ] 模型完整性
```

### 9.4 日志分析工具

```bash
# 实时监控
tail -F logs/canary.log

# 错误统计
grep -c ERROR logs/*.log | sort -t: -k2 -n -r

# 时段分析
awk '/2026-09-27/{print}' logs/predict.log | wc -l
```

---

## 10. 合规要求

### 10.1 监管要求

| 法规 | 要求 | 实施 |
|---|---|---|
| **个人信息保护法** | 不收集个人信息 | ✅ 无用户数据收集 |
| **数据安全法** | 数据分类分级 | ✅ 见第 5 节 |
| **证券法** | 不构成投资建议 | ⚠ 需免责声明 |
| **网络安全法** | 日志留存 ≥ 6 个月 | ✅ 7 天日志 + 备份 |

### 10.2 免责声明

```
⚠️ 重要声明：

本项目所有分析、策略、回测结果仅供学习和研究使用，
不构成任何投资建议。

使用本项目进行实盘交易的所有后果由用户自行承担。
项目维护者不对任何投资损失负责。

用户应：
- 自行评估投资风险
- 遵守当地证券法规
- 必要时咨询持牌投资顾问
```

### 10.3 数据来源合规

- ✅ baostock：公开免费数据
- ✅ akshare：开源财经数据
- ✅ 通联数据：API 授权数据
- ❌ 禁止：使用未授权数据源

---

## 11. 应急响应

### 11.1 安全事件分级

| 级别 | 描述 | 响应时间 |
|---|---|---|
| 🔴 P0 | 数据泄露 / RCE / 大量用户影响 | 1h |
| 🟠 P1 | 单点攻击 / 信息泄露 | 4h |
| 🟡 P2 | 异常行为 / 探测 | 24h |
| 🟢 P3 | 误报 / 可疑 | 7d |

### 11.2 应急响应流程

```
1. 检测 (Detection)
   ↓ canary 异常 / 外部报告
2. 遏制 (Containment)
   ↓ 隔离受影响系统
3. 根除 (Eradication)
   ↓ 删除恶意代码 / 修复漏洞
4. 恢复 (Recovery)
   ↓ 恢复服务 + 验证
5. 复盘 (Lessons Learned)
   ↓ Post-mortem 报告
```

### 11.3 关键应急 Runbook

#### 11.3.1 Ollama 被未授权访问

```bash
# 1. 立即停止 Ollama
sudo systemctl stop ollama

# 2. 检查访问日志
journalctl -u ollama.service --since "1 hour ago"

# 3. 修改配置（仅监听本地）
sudo systemctl edit ollama
# 添加：Environment="OLLAMA_HOST=127.0.0.1:11434"

# 4. 重启
sudo systemctl start ollama

# 5. 验证
ss -tlnp | grep 11434
```

#### 11.3.2 数据被篡改

```bash
# 1. 停止所有服务
make timer-disable

# 2. 验证数据完整性
sha256sum data/parquet/daily/000001.parquet | diff - sha256sum_backup.txt

# 3. 从备份恢复
make restore FILE=/home/jiuben/backups/tdx-data-feed-20260920.tar.gz

# 4. 重启服务
make timer-enable

# 5. 审查访问日志
```

#### 11.3.3 模型被替换

```bash
# 1. 停止 ollama
sudo systemctl stop ollama

# 2. 验证模型完整性
sha256sum /home/jiuben/models/*.gguf | diff - sha256sum_models.txt

# 3. 重新部署（从训练好的 adapter）
make deploy-all

# 4. 测试
make deploy-test

# 5. 启动
sudo systemctl start ollama
```

---

## 12. 联系方式

### 12.1 安全团队

| 角色 | 姓名 | 邮箱 | 备注 |
|---|---|---|---|
| 维护者 | jiuben | jiuben@jiuben.com | 通用安全联系 |
| GitHub | [@jiuben](https://github.com/jiuben) | - | Issue/PR |
| 紧急 | security@jiuben.com | 24h 响应 | 严重漏洞专用 |

### 12.2 PGP 公钥

```
-----BEGIN PGP PUBLIC KEY BLOCK-----
[公钥待补充]
-----END PGP PUBLIC KEY BLOCK-----
```

### 12.3 致谢

我们感谢以下安全研究人员的负责任披露：

（暂无致谢记录，欢迎提交第一个）

---

## 附录

### A. 安全检查清单（CI/CD）

```yaml
# .github/workflows/security.yml
name: Security
on: [push, pull_request]

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      # 1. 依赖漏洞扫描
      - name: pip-audit
        run: pip install pip-audit && pip-audit

      # 2. 代码静态扫描
      - name: bandit
        run: pip install bandit && bandit -r scripts/ v5/

      # 3. Secret 扫描
      - name: gitleaks
        uses: gitleaks/gitleaks-action@v2

      # 4. SAST
      - name: semgrep
        uses: returntocorp/semgrep-action@v1
```

### B. 工具列表

| 工具 | 用途 | 命令 |
|---|---|---|
| pip-audit | 依赖漏洞 | `pip-audit` |
| bandit | 代码安全 | `bandit -r .` |
| gitleaks | Secret 扫描 | `gitleaks detect` |
| semgrep | SAST | `semgrep --config=auto` |
| trivy | 容器扫描 | `trivy image` |
| nmap | 端口扫描 | `nmap localhost` |
| openssl | TLS 检查 | `openssl s_client` |

### C. 相关文档

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 系统总览 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南（含安全 PR 流程）|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 架构决策（含安全设计）|
| [docs/API.md](docs/API.md) | API 文档（含安全错误码）|

---

## 许可

私有项目 · 内部使用 · 未经授权不得传播

> 最后更新：2026-09-27 · 维护者：jiuben
> 安全版本：1.0.0
> 下次审计：2026-12-27