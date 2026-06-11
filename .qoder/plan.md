# Agent 评测系统 - 架构审查与开发方案

## Context

本项目是一套「大模型 Agent 调用链路」自动化评测平台，目前处于**设计阶段**，有 6 份完整的设计文档（architecture / data-model / trace-protocol / evaluation-design / analysis-and-compare / test-case-design），尚无实际代码。需要对设计文档进行一致性审查，发现问题后修复，再制定开发实施方案。

---

## 一、架构审查结论

共发现 **10 个问题**（4 个用户提及 + 6 个审查新发现），其中 **2 个高严重度**、**5 个中严重度**、**2 个低严重度**、**1 个非问题**。

### 🔴 高严重度（必须在任何代码开发前修复）

| 编号 | 问题 | 影响 |
|------|------|------|
| **P5** | **效果层(Outcome)评分无法存入 eval_scores 表**：`spans.span_type` CHECK 约束不含 `outcome`，且 `eval_scores.span_id` 为 `NOT NULL`，但 Outcome 层不绑定 span | DDL 必须修改（`span_id` 改为可空） |
| **P3** | **版本对比前置条件三处不一致**：data-model.md 只列 2 项，analysis-and-compare.md 列 3 项，evaluation-design.md 列 3 项（内容不同），无人列出完整 4 项 | 版本对比逻辑正确性 |

### 🟡 中严重度

| 编号 | 问题 |
|------|------|
| **P1** | test-case-design.md 扁平字段（`expected_checkpoints`/`expect_mode`/`tool_match_mode`/`application`）与 data-model.md JSONB 嵌套结构缺少映射说明 |
| **P2** | architecture.md 技术选型写 Kafka，但 trace-protocol.md 实现用 Redis（Kafka 是后续演进） |
| **P6** | data-model.md 示例 SQL 引用不存在的 `eval_cases.case_set_id` 列（实际通过 case_set_members 中间表） |
| **P7** | test-case-design.md 伪代码引用不存在的 `eval_runs.score` 字段（实际在 `traces.overall_score`） |

### 🟢 低严重度

| 编号 | 问题 |
|------|------|
| **P8** | eval_runs.eval_case_id / trace_id 缺少 FK 但未说明理由（agent_version 不设 FK 已有说明） |
| **P9** | test-case-design.md 核心字段表遗漏 `expected_answer`/`expected_retrieval`/`gold_answer` |
| **P10** | analysis-and-compare.md 聚合描述引用不存在的 `eval_runs.score` |

### ✅ 非问题

| 编号 | 说明 |
|------|------|
| **P4** | test-case-design.md 的 `source` 字段重复 data-model.md 定义，属于可接受的跨文档冗余 |

---

## 二、开发方案

### 阶段总览

```
MVP (第1-6周)  ────▶  完善 (第7-14周)  ────▶  进阶 (第15周+)
  核心链路跑通             全功能 + 前端              高级特性 + 性能优化
```

---

### Phase 1：MVP（第 1-6 周）

**目标**：评测核心链路端到端跑通 —— 手工用例 → Agent 执行 → SDK 上报 → 入库 → 评测器打分 → API 返回结果。

#### M1.1 文档修正 + 基础设施（第 1-2 周）

| 任务 | 产出文件 |
|------|---------|
| 修复 P1-P10 所有文档问题（修正 DDL、统一对比前置条件、修正示例 SQL、补充映射表） | 6 份 docs/*.md 修订 |
| 项目脚手架：`backend/pyproject.toml`、Docker Compose（PG + Redis） | `backend/`, `docker-compose.yml` |
| 数据库迁移：Alembic 初始 DDL（`eval_scores.span_id` 改为 `NULL`） | `backend/migrations/`, `backend/core/database.py`, `backend/core/models.py` |

#### M1.2 SDK + 数据上报链路（第 2-3 周）

| 任务 | 产出文件 |
|------|---------|
| SDK 核心：`TraceReporter`（init / start_trace / report_span / finish_trace） | `sdk/agent_eval_sdk/reporter.py` |
| Ingest 消费者：Redis List 轮询 → 批量 INSERT traces/spans → 回写 eval_runs.trace_id | `backend/workers/ingest_worker.py`, `backend/api/ingest.py` |
| 集成测试：SDK → Redis → Ingest → DB 全链路 | `backend/tests/test_ingest_flow.py` |

#### M1.3 评测器核心（第 3-4 周）

| 任务 | 产出文件 |
|------|---------|
| `BaseEvaluator` + `EvalResult` + `EvaluatorRegistry` | `backend/evaluators/base.py`, `registry.py` |
| 五层评测器（仅确定性部分，不含 LLM） | `backend/evaluators/{intent,retrieval,tool,generation,outcome}.py` |
| 公式动态适配机制（感知→跳过→权重归一化） | 合入 `base.py` |

#### M1.4 评测编排 + API（第 4-5 周）

| 任务 | 产出文件 |
|------|---------|
| `EvaluationOrchestrator`：依赖解析 + 并行调度 + enabled_layers 裁剪 | `backend/runner/engine.py` |
| FastAPI 路由：tasks / runs / ingest | `backend/api/{tasks,runs,ingest}.py` |
| Celery Worker：异步执行单条 Case 评测 | `backend/workers/eval_worker.py` |

#### M1.5 端到端验证（第 5-6 周）

| 任务 |
|------|
| Seed 数据：3-5 条 eval_cases + 1 个 smoke case_set |
| 模拟 Agent 上报完整 Trace → 验证 5 条 eval_scores（Outcome 的 span_id=NULL） |
| 验证 span.score / traces.overall_score 回填 |
| 编写 MVP 集成测试套件 |

---

### Phase 2：完善（第 7-14 周）

**目标**：全功能覆盖 —— LLM-as-Judge、前端 Dashboard、版本对比、生产采样管线。

#### M2.1 LLM-as-Judge 接入（第 7-9 周）

- PromptManager（YAML 模板加载/渲染）+ LLM Judge 客户端（重试/降级/并行）
- 7 个 Prompt 模板编写（factual_accuracy / hallucination / completeness / semantic_match / tool_equivalence / param_closeness / task_completion）
- 补齐 5 层评测器的 LLM 维度
- 多次采样去极值 + 缓存复用

#### M2.2 分析层（第 9-10 周）

- 聚合引擎：Run → Task → Version 三级
- 版本对比引擎：Paired t-test + DiffResult
- 回归检测：自动触发 + WARNING/CRITICAL 阈值
- 报告导出：JSON / CSV
- API：`/api/compare` + `/api/analytics`

#### M2.3 前端基础（第 10-12 周）

- React 项目初始化（Vite + ECharts）
- 5 个页面：Dashboard / Tasks / TraceViewer / CompareView / CaseManager

#### M2.4 生产采样管线（第 12-14 周）

- 定时分层抽样引擎 + LLM 批量标注 + 置信度分流
- 审核队列 UI + API
- Trace 快照机制 + Case 健康监控
- MONITOR_MODE 支持（无期望值时降级评测）

#### M2.5 测试集管理 + CI/CD（第 13-14 周）

- 测试集维护工具 + smoke/regression 测试集构建
- CI 集成脚本（PR 触发 smoke）+ Celery Beat 定时任务

---

### Phase 3：进阶（第 15 周+）

- 高级统计：Cohen's d / Bootstrap CI / Bonferroni 校正
- Dashboard 增强：雷达图 / 趋势线 / 退化热力图
- Redis → Kafka 迁移 + OpenTelemetry 原生集成
- 用例自动生成 + 批量导入 + 标注质量评估

---

## 三、关键依赖链

```
文档修正(T1.1)
  ├── 脚手架(T1.2) ── DB迁移(T1.3) ── 评测器基类(T3.1) ── 5层评测器(T3.2-T3.7)
  │                                                            │
  └── Redis(T1.4) ── SDK(T2.1) ── Ingest链路(T2.2-T2.6)       │
                                        │                      │
       API(T4.3) ◀── DB迁移            │                      │
            │                           │                      │
            └── Celery(T4.6) ◀── 编排器(T4.1) ◀────────────────┘
                     │
                     └── 端到端验证(T5.1-T5.5)
                              │
                              ├── LLM Judge(M2.1) ── 分析层(M2.2)
                              │                          │
                              ├── 前端(M2.3)             │
                              │                          │
                              └── 生产采样(M2.4) ────────┘
```

---

## 四、验证方式

1. **MVP 验证**：运行 `docker-compose up` 后，通过 seed 脚本创建用例 → 模拟 Agent SDK 上报 → API 查询 eval_scores，确认 5 层评分完整、Outcome 的 span_id 为 NULL、回填正确
2. **完善版验证**：同一个 Case Set 对两个模拟版本执行评测 → `/api/compare` 返回 DiffResult → 前端 CompareView 正确渲染
3. **进阶版验证**：生产 Trace → 定时抽样 → LLM 标注 → 审核队列 → 自动入库全流程走通
