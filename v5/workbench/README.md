# v5-VE1-M2 Workbench · 数学理解子层优化路线图工作台

## 概述

把 v5‑VE1‑M2 数学理解子层优化路线图开发为一个可交互的个人工作台。

- **后端**：FastAPI + SQLite（5 张表）
- **前端**：原生 JS + Chart.js + vis-network + SortableJS
- **数据**：本地持久化（DB + localStorage）
- **启动**：单端口（9000）

## 7 大模块

| 模块 | 内容 | 后端 API |
|---|---|---|
| 🏠 **概览** | 版本标识 / KPI / 阶段进度 | `/api/state`, `/api/metrics`, `/api/tasks` |
| 🗺️ **路线图** | 阶段 Tab + 任务列表 + 筛选搜索 | `/api/tasks` |
| 🕸️ **概念图谱** | vis-network 力导向图 | `/api/graph` |
| 📝 **笔记反思** | Markdown 笔记 + 类型筛选 | `/api/notes` |
| 📈 **指标面板** | KPI 卡片 + Chart.js 雷达图 | `/api/metrics` |
| ⚡ **快捷区** | 拖拽排序 + 添加删除（localStorage） | — |
| ⚙️ **偏好设置** | 主题 / 布局 / 通知 / 数据导出 | `/api/state` |

## 启动

```bash
cd /home/jiuben/tdx-data-feed/v5/workbench
./start.sh
# 或
/home/jiuben/tdx-data-feed/venv/bin/python server.py
```

打开 http://127.0.0.1:9000

首次访问会自动加载种子数据（25 任务 + 5 笔记 + 8 指标 + 20 概念 + 20 边）。

如需手动重置，点击右上角 "⟳ 重置"。

## API 速查

```
GET    /api/state              全局状态
PUT    /api/state/{key}        更新状态
GET    /api/tasks?phase=&status=&week=
POST   /api/tasks              创建
PATCH  /api/tasks/{id}         更新
DELETE /api/tasks/{id}         删除
GET    /api/notes?tag=&linked_task=&reflection_type=
POST   /api/notes
PATCH  /api/notes/{id}
DELETE /api/notes/{id}
GET    /api/metrics?category=
POST   /api/metrics
PATCH  /api/metrics/{id}
POST   /api/metrics/{id}/record  记录时序值
DELETE /api/metrics/{id}
GET    /api/concepts?category=
GET    /api/graph              节点 + 边
POST   /api/seed               重置为种子
GET    /api/stats              统计
```

## 数据模型

| 表 | 字段 |
|---|---|
| `state` | key / value / updated_at |
| `tasks` | id / title / description / phase / week / priority / status / progress / dependencies / due_date / category / roi / tags |
| `notes` | id / title / content / tags / linked_task_id / reflection_type |
| `metrics` | id / name / category / unit / baseline_value / current_value / target_value / display_type / direction |
| `metric_records` | id / metric_id / value / recorded_at / note |
| `concepts` | id / name / category / description / capability_level / related_metrics |
| `edges` | id / source_id / target_id / relation / weight / label |

## 偏好持久化（localStorage）

- 主题（light/dark/auto）
- 布局密度（comfortable/compact）
- 默认视图
- 通知开关（任务/笔记/指标）
- 快捷方式列表

## 文件结构

```
v5/workbench/
├── server.py              FastAPI 后端
├── workbench.db           SQLite 数据库
├── seed.json              种子数据
├── start.sh               启动脚本
├── README.md              本文档
└── static/
    ├── index.html         SPA 主页
    ├── app.js             前端逻辑
    ├── style.css          样式（双主题）
    └── vendor/            （预留扩展）
```
