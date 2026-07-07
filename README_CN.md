[English](README.md) | **中文**

---

# Agent Eval

> 面向大模型 Agent 调用链路的**全链路、分层式、持续化**自动化评测平台。

一个 Agent 改了 prompt、换了模型、调整了工具调用策略——这些变更对端到端质量的影响，不应该靠「感觉」来判断。Agent Eval 让你对 Agent 执行过程的每一层（意图 → 召回 → 工具 → 生成 → 效果）进行独立度量，汇集为量化的质量画像，并支持跨版本的科学对比。

---

## 核心能力

### 📊 五层分层评测

对 Agent 执行链路逐层独立度量，每一层有专属的评测维度和公式：

| 评测层 | 评测对象 | 核心指标 |
|--------|---------|---------|
| **意图层** (Intent) | 用户意图分类是否准确 | 意图匹配精度、NER F1、置信度校准 |
| **召回层** (Retrieval) | 检索结果的精度与覆盖 | Precision@K、Recall@K、MRR、NDCG、多样性 |
| **工具层** (Tool) | 工具选择与参数正确性 | 工具选择精度、参数准确性、调用序列正确性、成功率 |
| **生成层** (Generation) | 答案的事实性与完整性 | 事实准确性、完整性、幻觉检测、语义相似度、语言质量 |
| **效果层** (Outcome) | 端到端任务完成度 | 任务完成度、延迟评分、Token 效率、错误恢复能力 |

### 🔌 双模数据采集

- **自研 SDK**：4 行代码嵌入 Agent 管道，精确控制 Span 粒度
- **OpenTelemetry 适配器**：零侵入接入 LangChain / LlamaIndex / OpenAI Agents SDK 等主流框架

### 🧠 智能评测集积累

「LLM 定时抽样 → 置信度分流 → 人工兜底」的复合管线，将生产环境 Trace 自动转化为长期评测用例，评测集越用越丰富。

### 📈 科学版本对比

基于 Paired t-test + Cohen's d 效应量 + Bootstrap 置信区间，不只看均值变化，还能判断差异是否统计显著。

### 🤖 7×24 Agent Runtime

系统自身也是一个持续在线的 Agent 服务——通过 Telegram Gateway 接收消息，Brain 模块做意图解析与命令执行，Scheduler 管理定时抽样和日报生成。

---

## 架构总览

```mermaid
flowchart TB
    subgraph L1["接入层"]
        Web["浏览器 / Dashboard"]
        Telegram["Telegram 用户"]
        TargetAgent["被测 Agent"]
    end

    subgraph L2["运行时层"]
        FastAPI["FastAPI 后端<br/>API + Dashboard"]
        Runtime["Agent Runtime<br/>Gateway / Scheduler / Brain"]
        SDK["Agent Eval SDK<br/>OTel Adapter"]
    end

    subgraph L3["应用服务层"]
        CaseService["用例与测试集管理"]
        TaskService["评测任务管理"]
        IngestService["Trace 采集服务"]
        EvalService["评测执行服务"]
    end

    subgraph L4["领域能力层"]
        Orchestrator["评测编排器<br/>五层并行调度"]
        Evaluators["评测器集合<br/>Intent / Retrieval / Tool / Generation / Outcome"]
        LLMClient["LLM Client<br/>LLM-as-Judge"]
    end

    subgraph L5["基础设施层"]
        Redis["Redis<br/>事件缓冲 / 队列"]
        PostgreSQL["PostgreSQL + JSONB<br/>Trace / Case / Run / Score"]
        Qwen["Qwen / GPT-4o<br/>评测 LLM"]
    end

    Web --> FastAPI
    Telegram --> Runtime
    TargetAgent --> SDK

    FastAPI --> CaseService
    FastAPI --> TaskService
    SDK --> Redis
    FastAPI --> Redis

    IngestService --> Redis
    IngestService --> PostgreSQL
    CaseService --> PostgreSQL
    TaskService --> EvalService
    EvalService --> Orchestrator

    Orchestrator --> Evaluators
    Evaluators --> LLMClient
    Runtime --> LLMClient

    LLMClient --> Qwen
    EvalService --> PostgreSQL
    FastAPI --> PostgreSQL
```

系统采用五层架构：**接入层**（FastAPI + Telegram Gateway）→ **调度层**（Celery 任务队列 + Scheduler 定时器）→ **执行层**（Agent 调用 → SDK 上报 → Ingest 消费 → 五层评测器）→ **存储层**（PostgreSQL + JSONB）→ **分析层**（聚合 → 版本对比 → 退化告警 → 报告导出）。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 后端框架 | FastAPI + SQLAlchemy 2.0 async |
| 数据库 | PostgreSQL 16 + JSONB（asyncpg） |
| 缓存/队列 | Redis 7（Celery broker + 事件缓冲） |
| 异步任务 | Celery + asyncio |
| 评测 LLM | Qwen / GPT-4o（temperature=0） |
| 前端 | 内嵌 Dashboard（HTML + vanilla JS） |
| 测试 | pytest + pytest-asyncio |
| 迁移 | Alembic |
| 部署 | Docker Compose |

---

## 快速开始

### 前置条件

- Python 3.11+
- Docker & Docker Compose
- LLM API Key（DashScope 或 OpenAI 兼容）

### 1. 启动基础设施

```bash
docker compose up -d redis postgres
```

### 2. 配置环境变量

在 `backend/.env` 中配置：

```env
DASHSCOPE_API_KEY=your_api_key
DATABASE_URL=postgresql+asyncpg://aura:aura@localhost:5433/agent_eval
REDIS_URL=redis://localhost:6379/0
```

### 3. 安装依赖

```bash
cd backend && pip install -e ".[dev]"
```

### 4. 数据库迁移

```bash
cd backend && alembic upgrade head
```

### 5. 一键启动

```bash
bash scripts/start_all.sh
```

启动后可访问：

| 服务 | 地址 |
|------|------|
| 💬 对话界面 + 📊 看板 | `http://localhost:8800` |
| 🔧 评测后端 API | `http://localhost:18000/docs` |
| ❤️ 健康检查 | `http://localhost:18000/health` |

```bash
# 管理命令
bash scripts/start_all.sh --stop      # 停止所有服务
bash scripts/start_all.sh --status    # 查看运行状态
bash scripts/start_all.sh --restart   # 重启所有服务
```

---

## 项目结构

```
agent-eval/
├── backend/                    # 后端核心
│   ├── agent/                  # Agent Runtime（Gateway / Brain / Scheduler）
│   │   ├── brain/              #   Brain 执行器 + 工具
│   │   ├── gateway/            #   消息网关（Telegram / Router / RateLimit）
│   │   └── scheduler/          #   定时任务调度
│   ├── api/                    # FastAPI 路由
│   │   ├── cases.py            #   评测用例管理
│   │   ├── case_sets.py        #   测试集管理
│   │   ├── tasks.py            #   评测任务 CRUD
│   │   ├── runs.py             #   评测运行记录
│   │   ├── ingest.py           #   事件上报入口
│   │   ├── stats.py            #   数据分析接口
│   │   ├── alerts.py           #   退化告警
│   │   └── brain.py            #   Brain 控制台代理
│   ├── core/                   # 核心配置与 ORM
│   │   ├── config.py
│   │   ├── database.py
│   │   └── models.py
│   ├── evaluators/             # 评测器插件
│   │   ├── base.py             #   抽象基类 + EvalResult
│   │   ├── registry.py         #   注册中心（支持多版本共存）
│   │   ├── intent.py           #   意图层评测器
│   │   ├── retrieval.py        #   召回层评测器
│   │   ├── tool.py             #   工具层评测器
│   │   ├── generation.py       #   生成层评测器
│   │   ├── outcome.py          #   效果层评测器
│   │   └── prompts/            #   LLM-as-Judge Prompt 模板
│   ├── runner/                 # 评测执行引擎
│   │   ├── engine.py           #   编排器（五层并行调度）
│   │   └── llm_judge.py        #   LLM-as-Judge 客户端
│   ├── workers/                # Celery 异步任务
│   │   ├── eval_worker.py
│   │   └── ingest_worker.py
│   ├── migrations/             # Alembic 数据库迁移
│   └── tests/                  # 后端测试
├── sdk/                        # Agent 侧上报 SDK
│   └── agent_eval_sdk/
│       ├── reporter.py         #   核心上报客户端
│       └── adapters/           #   OTel 适配器
├── examples/                   # 示例 Agent
│   ├── agent_server.py         #   示例 Agent 服务
│   └── example_agent.py
├── docs/                       # 项目文档
│   ├── architecture.md         #   架构总览
│   ├── data-model.md           #   数据模型与 DDL
│   ├── trace-protocol.md       #   数据上报协议
│   ├── evaluation-design.md    #   评测维度与方法设计
│   ├── analysis-and-compare.md #   版本对比与分析
│   ├── test-case-design.md     #   测试用例设计
│   └── features/               #   Feature 文档
├── scripts/                    # 运维脚本
│   ├── start_all.sh            #   一键启动脚本
│   └── init_agent_team.py
├── docker-compose.yml          # 本地基础设施
└── AGENTS.md                   # Qoder 项目指令
```

---

## 关键设计决策

### 分层优于大统一

Agent 不是黑盒，其行为分布在意图、召回、工具、生成四个阶段，每个阶段的「好」有完全不同的定义。五层分层评测让每一层的评测逻辑清晰可维护。

### JSONB 作为 Agent 数据的天然选择

Agent 链路内部结构随版本迭代频繁变化，纯关系型 schema 迁移成本高。JSONB + schema-on-read 保持数据模型弹性，配合 GIN 索引仍可高效查询。

### Redis 缓冲层

SDK 不直接写 PostgreSQL，而是通过 Redis List 做缓冲，Ingest 消费者独立拉取批量写入。Agent 侧上报几乎零延迟，同时避免数据库连接池被打满。

### 评测器版本管理

评测器自身升级会导致同一 Trace 得分变化，破坏版本对比公平性。采用完整 SemVer 管理：Bug 修复 bump PATCH，新维度 bump MINOR，框架重构 bump MAJOR。版本对比强制要求评测器版本一致。

### 自适应公式系统

根据期望值的实际内容动态调整——`mode: "any"` 退化为二值判断，`divergent_ok: true` 跳过完整性检查，`nice_to_have` 只加分不扣分。同一套评测器能处理严格事实问答到发散推荐建议等各类场景。

---

## 文档

| 文档 | 内容 |
|------|------|
| [架构总览](docs/architecture.md) | 系统顶层架构、模块边界、技术选型 |
| [数据模型](docs/data-model.md) | 实体关系、完整 DDL、索引策略、JSONB 选型论证 |
| [上报协议](docs/trace-protocol.md) | 分阶段事件流 Schema、SDK API、OTel 适配 |
| [评测设计](docs/evaluation-design.md) | 评测器插件架构、五层详细维度、评分公式、LLM-as-Judge |
| [版本对比](docs/analysis-and-compare.md) | 可视化方案、统计检验、回归告警 |
| [测试用例](docs/test-case-design.md) | 用例 Schema、标注规范、测试集管理 |
| [项目故事](PROJECT_STORY.md) | 项目起源、挑战与收获、未来规划 |

---

## License

MIT License © 2026
