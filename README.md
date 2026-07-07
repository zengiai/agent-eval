**English** | [中文](README_CN.md)

---

# Agent Eval

> A **full-pipeline, layered, continuous** automated evaluation platform for LLM agent execution chains.

When you tweak a prompt, swap a model, or adjust a tool-calling strategy — the impact on end-to-end quality shouldn't be judged by gut feeling. Agent Eval measures every layer of your agent's execution (Intent → Retrieval → Tool → Generation → Outcome) independently, aggregates them into a quantitative quality profile, and supports scientific cross-version comparisons.

---

## Core Capabilities

### 📊 Five-Layer Evaluation

Each layer of the agent execution pipeline is measured independently, with dedicated evaluation dimensions and formulas:

| Layer | What It Evaluates | Key Metrics |
|-------|-------------------|-------------|
| **Intent** | Are user intents classified correctly? | Intent match accuracy, NER F1, confidence calibration |
| **Retrieval** | How precise and comprehensive are search results? | Precision@K, Recall@K, MRR, NDCG, diversity |
| **Tool** | Are the right tools called with the right parameters? | Tool selection accuracy, parameter correctness, sequence correctness, success rate |
| **Generation** | Is the answer factual and complete? | Factual accuracy, completeness, hallucination detection, semantic similarity, language quality |
| **Outcome** | Was the task completed end-to-end? | Task completion, latency score, token efficiency, error recovery |

### 🔌 Dual-Mode Data Collection

- **Custom SDK**: 4 lines of code embedded in the agent pipeline, with precise control over span granularity
- **OpenTelemetry Adapter**: Zero-code integration with LangChain, LlamaIndex, OpenAI Agents SDK, and other major frameworks via a three-tier span-type mapping strategy

### 🧠 Intelligent Eval Set Accumulation

A composite pipeline — "LLM periodic sampling → confidence-based triage → human review safety net" — automatically converts production traces into long-term evaluation cases, so your eval set keeps growing richer over time.

### 📈 Scientific Version Comparison

Paired t-tests + Cohen's d effect size + Bootstrap confidence intervals. Beyond mean scores, the system tells you whether differences are statistically significant and how large the effect really is.

### 🤖 7×24 Agent Runtime

The system doesn't just evaluate agents — it *is* a continuously running agent service. It accepts messages via Telegram Gateway, routes them through the Brain module for intent parsing and command execution, and uses a Scheduler to manage periodic sampling and daily report generation.

---

## Architecture Overview

```mermaid
flowchart TB
    subgraph L1["Access Layer"]
        Web["Browser / Dashboard"]
        Telegram["Telegram Users"]
        TargetAgent["Target Agent"]
    end

    subgraph L2["Runtime Layer"]
        FastAPI["FastAPI Backend<br/>API + Dashboard"]
        Runtime["Agent Runtime<br/>Gateway / Scheduler / Brain"]
        SDK["Agent Eval SDK<br/>OTel Adapter"]
    end

    subgraph L3["Application Layer"]
        CaseService["Case & Case Set Management"]
        TaskService["Eval Task Management"]
        IngestService["Trace Ingestion Service"]
        EvalService["Evaluation Service"]
    end

    subgraph L4["Domain Layer"]
        Orchestrator["Evaluation Orchestrator<br/>Five-Layer Parallel Scheduling"]
        Evaluators["Evaluator Suite<br/>Intent / Retrieval / Tool / Generation / Outcome"]
        LLMClient["LLM Client<br/>LLM-as-Judge"]
    end

    subgraph L5["Infrastructure Layer"]
        Redis["Redis<br/>Event Buffer / Queue"]
        PostgreSQL["PostgreSQL + JSONB<br/>Trace / Case / Run / Score"]
        Qwen["Qwen / GPT-4o<br/>Eval LLM"]
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

The system follows a **five-layer architecture**: **Access Layer** (FastAPI + Telegram Gateway) → **Scheduling Layer** (Celery task queue + Scheduler cron) → **Execution Layer** (Agent call → SDK report → Ingest consume → 5 evaluators) → **Storage Layer** (PostgreSQL + JSONB) → **Analysis Layer** (Aggregation → Version comparison → Regression alerts → Report export).

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Backend Framework | FastAPI + SQLAlchemy 2.0 async |
| Database | PostgreSQL 16 + JSONB (asyncpg) |
| Cache / Queue | Redis 7 (Celery broker + event buffer) |
| Async Tasks | Celery + asyncio |
| Eval LLM | Qwen / GPT-4o (temperature=0) |
| Frontend | Embedded Dashboard (HTML + vanilla JS) |
| Testing | pytest + pytest-asyncio |
| Migrations | Alembic |
| Deployment | Docker Compose |

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- LLM API Key (DashScope or OpenAI-compatible)

### 1. Start Infrastructure

```bash
docker compose up -d redis postgres
```

### 2. Configure Environment

In `backend/.env`:

```env
DASHSCOPE_API_KEY=your_api_key
DATABASE_URL=postgresql+asyncpg://aura:aura@localhost:5433/agent_eval
REDIS_URL=redis://localhost:6379/0
```

### 3. Install Dependencies

```bash
cd backend && pip install -e ".[dev]"
```

### 4. Database Migration

```bash
cd backend && alembic upgrade head
```

### 5. One-Click Launch

```bash
bash scripts/start_all.sh
```

After startup, you can access:

| Service | URL |
|---------|-----|
| 💬 Chat + 📊 Dashboard | `http://localhost:8800` |
| 🔧 Eval Backend API | `http://localhost:18000/docs` |
| ❤️ Health Check | `http://localhost:18000/health` |

```bash
# Management commands
bash scripts/start_all.sh --stop      # Stop all services
bash scripts/start_all.sh --status    # Check running status
bash scripts/start_all.sh --restart   # Restart all services
```

---

## Project Structure

```
agent-eval/
├── backend/                    # Backend Core
│   ├── agent/                  # Agent Runtime (Gateway / Brain / Scheduler)
│   │   ├── brain/              #   Brain executor + tools
│   │   ├── gateway/            #   Message gateway (Telegram / Router / RateLimit)
│   │   └── scheduler/          #   Cron job scheduling
│   ├── api/                    # FastAPI Routes
│   │   ├── cases.py            #   Eval case management
│   │   ├── case_sets.py        #   Case set management
│   │   ├── tasks.py            #   Eval task CRUD
│   │   ├── runs.py             #   Eval run records
│   │   ├── ingest.py           #   Event ingestion endpoint
│   │   ├── stats.py            #   Analytics API
│   │   ├── alerts.py           #   Regression alerts
│   │   └── brain.py            #   Brain console proxy
│   ├── core/                   # Core config & ORM
│   │   ├── config.py
│   │   ├── database.py
│   │   └── models.py
│   ├── evaluators/             # Evaluator Plugins
│   │   ├── base.py             #   Abstract base + EvalResult
│   │   ├── registry.py         #   Registry (multi-version support)
│   │   ├── intent.py           #   Intent layer evaluator
│   │   ├── retrieval.py        #   Retrieval layer evaluator
│   │   ├── tool.py             #   Tool layer evaluator
│   │   ├── generation.py       #   Generation layer evaluator
│   │   ├── outcome.py          #   Outcome layer evaluator
│   │   └── prompts/            #   LLM-as-Judge prompt templates
│   ├── runner/                 # Evaluation Execution Engine
│   │   ├── engine.py           #   Orchestrator (five-layer parallel scheduling)
│   │   └── llm_judge.py        #   LLM-as-Judge client
│   ├── workers/                # Celery Async Tasks
│   │   ├── eval_worker.py
│   │   └── ingest_worker.py
│   ├── migrations/             # Alembic Migrations
│   └── tests/                  # Backend Tests
├── sdk/                        # Agent-Side Reporting SDK
│   └── agent_eval_sdk/
│       ├── reporter.py         #   Core reporting client
│       └── adapters/           #   OTel adapter
├── examples/                   # Example Agent
│   ├── agent_server.py         #   Example agent server
│   └── example_agent.py
├── docs/                       # Documentation
│   ├── architecture.md         #   Architecture overview
│   ├── data-model.md           #   Data model & DDL
│   ├── trace-protocol.md       #   Data reporting protocol
│   ├── evaluation-design.md    #   Evaluation dimensions & methods
│   ├── analysis-and-compare.md #   Version comparison & analysis
│   ├── test-case-design.md     #   Test case design
│   └── features/               #   Feature docs
├── scripts/                    # Ops Scripts
│   ├── start_all.sh            #   One-click launch script
│   └── init_agent_team.py
├── docker-compose.yml          # Local infrastructure
└── AGENTS.md                   # Qoder project instructions
```

---

## Key Design Decisions

### Layered Over Monolithic

An agent is not a black box. Its behavior spans four stages (intent, retrieval, tool, generation), each with a completely different definition of "good." The five-layer approach makes each layer's evaluation logic clear and maintainable.

### JSONB as a Natural Fit for Agent Data

Agent pipeline structures change frequently across versions — new tool types, evolving intent taxonomies, adjusted retrieval strategies. JSONB + schema-on-read keeps the data model elastic, while GIN indexes still enable efficient queries.

### Redis Buffer Layer

The SDK doesn't write directly to PostgreSQL. Instead, it writes to Redis Lists, and an independent Ingest consumer pulls and batch-inserts. Agent-side reporting is nearly zero-latency, while preventing database connection pools from being overwhelmed.

### Evaluator Version Management

Evaluator upgrades change scores for the same trace, breaking the fairness of version comparisons. Full SemVer management: bug fixes bump PATCH, new dimensions bump MINOR, framework overhauls bump MAJOR. Version comparisons enforce identical evaluator versions.

### Adaptive Formula System

Formulas dynamically adapt to the actual content of expected values — `mode: "any"` degrades to binary judgment, `divergent_ok: true` skips completeness checks, `nice_to_have` checkpoints add bonus points without penalty. The same evaluator suite handles everything from strict factual Q&A to open-ended recommendations.

---

## Documentation

| Document | Content |
|----------|---------|
| [Architecture Overview](docs/architecture.md) | System architecture, module boundaries, technology selection |
| [Data Model](docs/data-model.md) | Entity relationships, complete DDL, indexing strategy, JSONB rationale |
| [Trace Protocol](docs/trace-protocol.md) | Phased event stream schema, SDK API, OTel adapter |
| [Evaluation Design](docs/evaluation-design.md) | Evaluator plugin architecture, five-layer dimensions, scoring formulas, LLM-as-Judge |
| [Version Comparison](docs/analysis-and-compare.md) | Visualization, statistical tests, regression alerts |
| [Test Case Design](docs/test-case-design.md) | Case schema, annotation standards, case set management |
| [Project Story](PROJECT_STORY.md) | Origin, challenges & learnings, roadmap |
| [中文文档](README_CN.md) | Chinese version of this README |

---

## License

MIT License © 2026
