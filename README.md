# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 + 冲突裁决 | 预警编号、关联指标、口径版本、比较运算符、预警级别 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 指标预警冲突裁决

预警规则必须绑定指标口径版本（`alertconfig.口径版本`），同一指标同一周期只形成一条预警状态。
裁决引擎在 `backend/app/services/alert_arbitration.py`，相关表均落在内存仓库（`alert_*`）：

| 表 | 含义 |
| --- | --- |
| `alert_period` | 指标周期观测值（含口径版本、周期序号、观测值） |
| `alert_hit` | 命中消费记录，命中即先持久化，后续裁决失败也不丢 |
| `alert_evaluation` | 规则评估缓存，按「规则 + 周期 + 口径版本 + 规则指纹」复用 |
| `alert_state` | 每指标每周期唯一预警状态，携带裁决版本、被吸收规则身份、连续周期 |
| `alert_notification` | 通知清单，与预警状态读取同一裁决版本 |
| `alert_conflict` | 同级规则同时命中的冲突留痕 |

优先级（确定性字典序）：**预警级别（P1>P2>P3） → 阈值边界严格度（`>`/`<` 优先于 `>=`/`<=`）
→ 持续周期数（短优先） → 生效时间（早优先） → 规则 id 兜底**。

四类可区分结论：触发、吸收、复用、冲突（另有未命中）。
口径版本变化时只重评引用该版本的规则，仍引用旧版本的结论继续复用；
并发修改规则或重复计算同一周期，在 `(指标, 周期)` 分段锁内归并为一份。

主要接口（前缀 `/api/alertconfig`）：

- `POST /arbitration/run` 执行全部周期裁决（`{"force": true}` 强制重裁）
- `POST /arbitration/periods` 登记周期观测；`GET /arbitration/periods` 查询
- `POST /arbitration/periods/{metric}/{period}` 单周期裁决
- `POST /arbitration/reevaluate/{metric}` 口径版本变化后按版本重评
- `GET /arbitration/states|notifications|conflicts|hits|evaluations|consistency`
- `POST /{id}/bind-version` 把规则迁移绑定到指标当前口径版本
- `POST /api/metric/{id}/caliber-version` 指标发布新口径版本（vN → vN+1）

后端测试：`cd backend && .venv/bin/python -m pytest tests/ -q`

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
