# Agent 调用链路自动评测系统 — 架构总览

> 本文档是系统顶层架构设计，定义模块边界和整体技术选型。各模块详细设计见对应子文档。

---

## 1. 系统定位

一套面向「大模型 Agent 调用链路」的自动化评测平台。对 Agent 执行过程的每一层（意图 → 召回 → 工具 → 生成 → 效果）进行独立度量，汇集为端到端的质量画像，并支持跨版本对比。

### 1.1 核心流程

```
┌──────────────┐    ┌─────────────────┐    ┌──────────────────┐
│  测试用例管理  │───▶│   评测执行引擎    │───▶│   结果存储与分析   │
│  (TestCase)   │    │  (Eval Runner)   │    │  (Storage + Viz)  │
└──────────────┘    └─────────────────┘    └──────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                  ▼
    ┌──────────┐     ┌──────────┐       ┌──────────┐
    │ 被测Agent │     │ 评测器集合 │       │ 数据采集器 │
    │ (Target)  │     │(Evaluators)│      │ (Collector)│
    └──────────┘     └──────────┘       └──────────┘
```

一次评测运行的生命周期：
1. **初始化** — 选定 Agent 版本 + 测试集，创建评测任务
2. **执行** — 逐条运行测试用例，Agent 通过 SDK 上报各阶段事件
3. **分层评测** — Ingest 服务组装完整 Trace，评测器对各层独立打分
4. **汇总** — 聚合指标，生成评测报告，触发退化告警

### 1.2 架构分层

| 层 | 职责 |
|---|------|
| **接入层** | REST API / Web Console，管理测试用例、发起评测、查看报告 |
| **调度层** | Celery 任务队列、并发控制、超时管理、重试策略 |
| **执行层** | 调用被测 Agent，采集全链路 trace，执行五层评测器 |
| **存储层** | PostgreSQL + JSONB，测试用例/Agent Trace/评测结果持久化 |
| **分析层** | 指标计算、版本对比（Paired t-test + Cohen's d）、趋势分析、报告生成 |

---

## 2. 模块索引

各模块详细设计见对应子文档：

| 模块 | 文档 | 核心内容 |
|------|------|---------|
| **数据模型与存储** | [data-model.md](data-model.md) | 实体关系、完整 DDL、索引策略、JSONB 选型论证、数据生命周期 |
| **数据上报协议** | [trace-protocol.md](trace-protocol.md) | 分阶段事件流 Schema、SDK API、Ingest 服务、容错与 OpenTelemetry 适配 |
| **评测维度与方法** | [evaluation-design.md](evaluation-design.md) | 评测器插件架构、五层详细维度、评分公式、LLM-as-Judge Prompt 模板 |
| **分析与版本对比** | [analysis-and-compare.md](analysis-and-compare.md) | 可视化方案、Dashboard 设计、版本对比 API、统计检验、回归告警 |
| **测试用例设计** | [test-case-design.md](test-case-design.md) | 用例 Schema、标注规范、测试集管理、用例生成策略、质量评估 |

### 2.1 文档间引用关系

```
architecture.md (本文档)
    │
    ├── data-model.md         ← 定义 EvalRun/LayerEval 的表结构
    │      │
    │      └── evaluation-design.md  ← 引用 LayerEval.metrics 结构
    │
    ├── trace-protocol.md     ← 定义 EvalRun.trace_data 的生成方式
    │      │
    │      └── data-model.md  ← Ingest 服务写入的数据表
    │
    ├── evaluation-design.md  ← 评测器消费 trace_data 生成 LayerEval
    │      │
    │      └── analysis-and-compare.md  ← 消费 LayerEval 做版本对比
    │
    ├── analysis-and-compare.md
    │      │
    │      └── test-case-design.md  ← 对比报告引用用例信息
    │
    └── test-case-design.md   ← 用例标注定义各层评测的 expected 基准
```

---

## 3. 技术选型

| 层次       | 推荐方案                       | 备选                          |
| -------- | -------------------------- | --------------------------- |
| 后端框架     | Python + FastAPI           | Go + Gin, Node.js + Express |
| 数据库      | PostgreSQL 16+             | MySQL 8+ (JSON支持)           |
| 任务队列     | Celery + Redis             | RQ, Argo Workflows          |
| 评测 LLM   | GPT-4o / Claude 3.5 Sonnet | 开源模型（vLLM 部署）               |
| 前端       | React + ECharts            | Vue + Chart.js, Grafana     |
| 消息通道     | Redis List（V1）→ Kafka（V2） | Redis Streams               |
| Trace 采集 | 自研 SDK + OpenTelemetry 适配  | —                           |
| 部署       | Docker Compose             | Kubernetes                  |

---

## 4. 项目结构

```
agent-eval/
├── backend/
│   ├── api/                # FastAPI 路由
│   │   ├── tasks.py        # 评测任务 CRUD
│   │   ├── runs.py         # 评测运行记录
│   │   ├── compare.py      # 版本对比
│   │   ├── ingest.py       # 事件上报入口
│   │   └── analytics.py    # 数据分析接口
│   ├── core/
│   │   ├── config.py
│   │   ├── database.py     # SQLAlchemy + asyncpg
│   │   └── models.py       # ORM 模型
│   ├── evaluators/         # 评测器插件
│   │   ├── base.py
│   │   ├── intent.py
│   │   ├── retrieval.py
│   │   ├── tool.py
│   │   ├── generation.py
│   │   └── outcome.py
│   ├── runner/             # 评测执行引擎
│   │   ├── engine.py       # 核心编排
│   │   ├── assembler.py    # Trace 事件组装
│   │   └── llm_judge.py    # LLM-as-Judge 客户端
│   ├── workers/            # Celery 任务
│   └── migrations/         # Alembic 迁移
├── sdk/                    # Agent 侧上报 SDK
│   └── agent_eval_sdk/
│       ├── reporter.py
│       └── adapters/
├── frontend/               # React 前端
│   ├── src/pages/
│   │   ├── Dashboard.tsx
│   │   ├── TraceViewer.tsx
│   │   └── CompareView.tsx
│   └── package.json
├── notebooks/              # Jupyter 分析
├── docker-compose.yml
└── docs/
    ├── architecture.md     # 本文档
    ├── data-model.md
    ├── trace-protocol.md
    ├── evaluation-design.md
    ├── analysis-and-compare.md
    └── test-case-design.md
```

---

## 5. 关键设计决策

### 5.1 为什么用 JSONB 而不是纯关系型存储 trace？

Agent 调用链路的内部结构随版本迭代频繁变化（新增工具类型、变更意图分类体系、调整召回策略），纯关系型 schema 迁移成本高。JSONB 允许 schema-on-read，配合 GIN 索引仍可高效查询。详见 [data-model.md](data-model.md) §2。

### 5.2 为什么用分阶段事件流而不是一次性上报完整 Trace？

分阶段上报让评测系统可以在 Agent 执行过程中就开始组装和评测，提高实时性；Agent 无需缓存全链路数据，降低内存压力；部分事件丢失不影响其他层评测。详见 [trace-protocol.md](trace-protocol.md) 第 3 节。

### 5.3 LLM-as-Judge 的一致性问题

LLM 评判存在随机性。应对策略：固定 temperature=0、多次采样取均值、定期人工标注校准、关键指标双验证。详见 [evaluation-design.md](evaluation-design.md) §4。

### 5.4 评测器自身的版本管理

评测器升级会导致同一 trace 得分变化，破坏版本对比的公平性。策略：评测器版本记录在 LayerEval 中，对比时强制要求版本一致，提供「重评」功能。详见 [evaluation-design.md](evaluation-design.md) §2。

### 5.5 版本对比的统计严谨性

不仅看均值变化，还通过 Paired t-test 检验显著性、Cohen's d 衡量效应量、Bootstrap 给出置信区间，避免将随机波动误判为退化。详见 [analysis-and-compare.md](analysis-and-compare.md) §2。

---

## 6. 数据流全景

```
                        ┌──────────────────────┐
  测试用例设计师           │   测试用例管理         │
  (人工/LLM辅助)  ──────▶│   Test Suite + Case   │
                        └──────────┬───────────┘
                                   │
                                   ▼
                        ┌──────────────────────┐
  评测发起者              │   评测任务创建         │
  ──────────────────────▶│   EvalTask           │
                        └──────────┬───────────┘
                                   │ 触发执行
                                   ▼
┌──────────────────────────────────────────────────────────────┐
│                        评测执行引擎                            │
│                                                              │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────────┐   │
│  │ 被测Agent │───▶│ SDK 上报  │───▶│ Redis / HTTP        │   │
│  │ (执行用例) │    │ (分阶段)  │    │                      │   │
│  └──────────┘    └──────────┘    └──────────┬───────────┘   │
│                                             │               │
│                                    ┌────────▼───────────┐   │
│                                    │  Ingest Service     │   │
│                                    │  - 事件组装 Trace    │   │
│                                    │  - 写入 EvalRun     │   │
│                                    │  - 触发评测任务      │   │
│                                    └────────┬───────────┘   │
│                                             │               │
│                              ┌──────────────┼───────────┐   │
│                              ▼              ▼           ▼   │
│                        ┌─────────┐  ┌─────────┐  ┌────────┐ │
│                        │意图评测器│  │召回评测器│  │ ...    │ │
│                        └────┬────┘  └────┬────┘  └───┬────┘ │
│                             │            │           │      │
│                             └────────────┼───────────┘      │
│                                          ▼                  │
│                                    ┌──────────┐            │
│                                    │ LayerEval│            │
│                                    │ (5条/run)│            │
│                                    └────┬─────┘            │
└─────────────────────────────────────────┼──────────────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    ▼                     ▼                     ▼
            ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
            │  聚合指标     │    │  版本对比     │    │  报告导出     │
            │  summary     │    │  Diff Report │    │  PDF/CSV     │
            │  _metrics    │    │  + 回归告警   │    │  /JSON/HTML  │
            └──────────────┘    └──────────────┘    └──────────────┘
```
