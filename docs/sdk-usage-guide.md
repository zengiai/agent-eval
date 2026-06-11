# Agent Eval SDK 使用教程

> 版本：SDK v0.2.0 | 最后更新：2026-06-11

---

## 目录

1. [SDK 概览](#1-sdk-概览)
2. [安装与配置](#2-安装与配置)
   - [2.2 两种启动模式](#22-两种启动模式)
   - [2.3 配置方式](#23-配置方式)
3. [方式 A：自研 SDK（显式调用）](#3-方式-a自研-sdk显式调用)
4. [方式 B：OpenTelemetry（零侵入）](#4-方式-bopentelemetry零侵入)
5. [部署架构](#5-部署架构)
   - [5.1 挂载模式](#51-模式一挂载模式推荐)
   - [5.2 独立部署](#52-模式二独立部署)
6. [嵌入 Agent 项目](#6-嵌入-agent-项目)
   - [6.1 挂载式启动](#61-挂载式启动推荐)
   - [6.2 SDK 嵌入模式](#62-sdk-嵌入模式仅上报不挂载评测引擎)
7. [看板与结果查看](#7-看板与结果查看)
8. [常见问题](#8-常见问题)

---

## 1. SDK 概览

Agent Eval SDK 是评测系统的数据上报客户端，将 Agent 执行链路的每个阶段（意图识别、召回、工具调用、生成）以 Span 事件上报到评测系统。

评测系统支持两种运行模式：**挂载模式**（评测引擎内嵌在 Agent 进程中）和**独立部署模式**（评测系统单独运行）。

```mermaid
graph LR
    subgraph "Agent 进程"
        Pipeline[Agent Pipeline]
        SDK[agent_eval_sdk]
        Eval[评测引擎<br/>（挂载模式）]
    end
    subgraph "基础设施"
        Redis[(Redis)]
        DB[(PostgreSQL)]
    end
    Pipeline -->|方式 A: 显式调用| SDK
    SDK -->|方式 B: OTel 自动| Redis
    SDK -->|RPUSH| Redis
    Redis --> Eval --> DB
```

### 两种上报方式对比

| 维度 | 方式 A（自研 SDK） | 方式 B（OpenTelemetry） |
|------|:---:|:---:|
| 侵入性 | 需手动插入 4~6 行代码 | **零侵入**，框架自动埋点 |
| Span 粒度 | 手动精确控制 | 依赖框架埋点粒度 |
| 依赖 | 仅 `redis` + `httpx` | 额外需 `opentelemetry-sdk` |
| 适用场景 | 自研 Agent、自定义 Span 结构 | LangChain / LlamaIndex |
| SDK 版本要求 | v0.1.0+ | v0.2.0+ |

---

## 2. 安装与配置

### 2.1 安装 SDK

```bash
# 基础安装（方式 A 已足够）
pip install agent-eval-sdk

# 含 OTel 支持（方式 B 需要）
pip install agent-eval-sdk[otel]

# 如需挂载完整评测系统（含评测引擎 + API 看板）
pip install agent-eval-backend
```

### 2.2 两种启动模式

评测系统支持两种运行模式，按场景选择：

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **挂载模式**（推荐） | 评测系统以 Python 对象内嵌在 Agent 进程中，随 Agent 进程启停 | 日常开发、CI 评测、单机压测 |
| **独立部署模式** | PostgreSQL、Redis、FastAPI、Ingest Worker 各自独立进程 | 生产环境长期运行、多 Agent 共享评测服务 |

#### 2.2.1 挂载模式（进程内嵌）

只需启动基础设施依赖，评测引擎随 Agent 进程一起挂载：

```bash
# 仅启动数据库和缓存（Agent 进程外）
docker compose up -d redis postgres
```

Agent 代码中通过配置文件 + CLI 参数完成挂载（详见 [§6 嵌入 Agent 项目](#6-嵌入-agent-项目)）：

```python
from backend.eval_service import EvalService

# 挂载评测系统到当前 Agent 进程
eval_service = EvalService(
    config_file="eval_config.yaml",
    enabled_layers=["intent", "generation", "outcome"],
    sampling_rate=0.05,
)
await eval_service.mount()  # 启动 Ingest 消费者 + 评测引擎
```

#### 2.2.2 独立部署模式

```bash
# 在评测系统项目目录下
cd agent-eval
docker compose up -d redis postgres

# 启动后端服务
cd backend
uvicorn backend.api:app --host 0.0.0.0 --port 8000 &

# 启动 Celery Worker（可选，用于异步评测）
celery -A backend.workers.eval_worker worker --loglevel=info &

# 启动 Ingest 消费者（持续消费 Redis 事件写入 DB）
python -m backend.workers.ingest_worker &
```

### 2.3 配置方式

#### 2.3.1 数据库连接配置（YAML/TOML 配置文件）

数据库连接信息通过配置文件在代码层面传入，不走 `.env`：

```yaml
# eval_config.yaml —— 评测系统配置文件
databases:
  postgres:
    url: "postgresql+asyncpg://aura:aura@localhost:5432/agent_eval"
    pool_size: 20
    max_overflow: 10

  redis:
    url: "redis://localhost:6379/0"
    key_prefix: "eval:events:"

celery:
  broker_url: "redis://localhost:6379/1"
  result_backend: "redis://localhost:6379/2"
```

或等价 TOML：

```toml
# eval_config.toml
[databases.postgres]
url = "postgresql+asyncpg://aura:aura@localhost:5432/agent_eval"
pool_size = 20
max_overflow = 10

[databases.redis]
url = "redis://localhost:6379/0"
key_prefix = "eval:events:"
```

#### 2.3.2 功能开关（CLI 启动参数）

评测层开关、采样率等功能控制通过 CLI 参数在启动时传入：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--enabled-layers` | `intent,retrieval,tool,generation,outcome` | 启用的评测层（逗号分隔） |
| `--sampling-rate` | `0.05` | 生产采样比例（0~1） |
| `--sampling-daily-limit` | `100` | 每日采样上限 |
| `--agent-version` | 必填 | Agent 版本号，对应评测任务 |
| `--ingest-interval` | `500` | Ingest 消费间隔（毫秒） |
| `--ingest-batch-size` | `100` | 单批消费上限 |
| `--config` | `eval_config.yaml` | 数据库配置文件路径 |
| `--llm-model` | `gpt-4o` | 评测用 LLM 模型 |

#### 2.3.3 评测 LLM 配置（代码层面）

```python
# 在挂载时通过代码指定评测 LLM
eval_service = EvalService(
    config_file="eval_config.yaml",
    llm={
        "model": "gpt-4o",
        "api_key": "sk-xxx",
        "base_url": "https://api.openai.com/v1",
        "temperature": 0.0,
        "max_retries": 3,
    },
)
```

---

## 3. 方式 A：自研 SDK（显式调用）

### 3.1 最小示例

```python
from agent_eval_sdk import TraceReporter

# 1) 初始化（全局单例）
reporter = TraceReporter(
    agent_version="v2.3.1",
    redis_url="redis://localhost:6379/0",
)

# 2) 开始一次 Trace
trace = reporter.start_trace(
    query="帮我查一下上周五NBA湖人队的比赛结果",
    source="eval",          # 'eval' = 评测触发 | 'production' = 生产采样
    run_id="run_xxx",       # 评测场景传入
)

# 3) 逐阶段上报 Span
trace.report_span(
    span_type="intent",
    input={"query": "..."},
    output={"intents": ["sports_query"], "confidence": 0.95},
    latency_ms=120,
    model="intent-classifier-v3",
)

trace.report_span(
    span_type="tool_call",
    input={"params": {}},
    output={"result": {}},
    tool_name="web_search",
    tool_result={"status": "success", "data": "..."},
    latency_ms=1800,
)

trace.report_span(
    span_type="generation",
    input={"prompt": "..."},
    output={"response": "湖人队以112:105战胜..."},
    tokens={"input": 1200, "output": 350},
    model="gpt-4o",
    latency_ms=2800,
)

# 4) 结束 Trace
trace.finish(
    final_response="湖人队以112:105战胜凯尔特人...",
    status="success",       # success | error | timeout
)

# 5) 程序退出前释放连接
reporter.close()
```

### 3.2 完整 Span 字段

```python
trace.report_span(
    span_type="tool_call",       # 必填：intent | retrieval | tool_call | generation
    input={"params": {...}},     # 输入 JSON
    output={"result": {...}},    # 输出 JSON
    latency_ms=1800,             # 延迟（毫秒）
    tokens={"input": 100, "output": 50},  # Token 用量
    model="gpt-4o",              # 模型名
    tool_name="web_search",      # 工具名（仅 tool_call）
    tool_params={"q": "..."},    # 工具参数（仅 tool_call）
    tool_result={"status": "success"},  # 工具结果（仅 tool_call）
)
```

### 3.3 生产采样模式

```python
# source="production"，不绑定 run_id，不触发评测
trace = reporter.start_trace(
    query="用户问题",
    source="production",
    source_ref="prod-instance-01",  # 生产实例标识
)
# ... 同上上报 Span ...
trace.finish(final_response="回答", status="success")
```

生产 Trace 存入数据库后可被 LLM 抽样标注、转为 Evaluation Case，持续积累评测集。

---

## 4. 方式 B：OpenTelemetry（零侵入）

> 需要 SDK v0.2.0+，安装 `pip install agent-eval-sdk[otel]`

### 4.1 最小配置

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from agent_eval_sdk.adapters import EvalSpanExporter

# 注册 Eval Exporter
provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(
        EvalSpanExporter(
            redis_url="redis://localhost:6379/0",
            agent_version="v2.3.1",
        )
    )
)
```

之后 LangChain / LlamaIndex 的每次 `chain.invoke()` 会自动产生 Span，Exporter 将其写入 Redis。

### 4.2 OTel 属性映射

OTel 属性仅支持 `str/int/float/bool` 基础类型。对于 `dict`/`list` 类型，**需用 `json.dumps()` 转为 JSON 字符串**，Exporter 会自动反序列化。

| OTel Span 属性 | 对应 Eval 字段 | 类型 |
|---------------|---------------|------|
| `Span.name` | `span_type` | str |
| `start_time` / `end_time` | `latency_ms` | 自动计算 |
| `query` | trace 级别查询文本 | str |
| `context` → JSON 字符串 | trace 上下文 | `json.dumps({...})` |
| `input` → JSON 字符串 | Span 输入 | `json.dumps({...})` |
| `output` → JSON 字符串 | Span 输出 | `json.dumps({...})` |
| `llm.model` | 模型名 | str |
| `llm.usage` → JSON 字符串 | Token 用量 | `json.dumps({...})` |
| `tool_name` | 工具名 | str |
| `tool_params` → JSON 字符串 | 工具参数 | `json.dumps({...})` |
| `tool_result` → JSON 字符串 | 工具结果 | `json.dumps({...})` |
| `run_id` | 评测运行 ID | str |
| `source` | eval / production | str |
| `final_response` | trace 最终回复 | str |

### 4.3 LangChain 集成示例

```python
import json
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.langchain import LangchainInstrumentor
from agent_eval_sdk.adapters import EvalSpanExporter

# 1) 注册 Eval Exporter
provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(
        EvalSpanExporter(
            redis_url="redis://localhost:6379/0",
            agent_version="v2.3.1",
            source="eval",
            run_id="run_demo_001",
        )
    )
)

# 2) 启用 LangChain 自动埋点
LangchainInstrumentor().instrument()

# 3) 正常使用 LangChain（无需任何 SDK 调用）
from langchain.chains import LLMChain

chain = LLMChain(...)
result = chain.invoke({"query": "NBA湖人队赛果"})
# ↑ 自动产生 intent → retrieval → tool_call → generation 对应的 OTel Span
#   EvalSpanExporter 自动将 Span 写入 Redis → 评测系统

provider.shutdown()  # 程序退出前刷新缓冲区
```

### 4.4 自定义 Span 属性（高级用法）

如果需要自定义 Span 属性（如附加 `run_id`），可使用 OTel API：

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("agent_pipeline") as root_span:
    # 设置 trace 级别元信息
    root_span.set_attributes({
        "query": "用户问题",
        "source": "eval",
        "run_id": "run_xxx",
        "context": json.dumps({"user_id": "u123"}),
    })

    # 子 Span：意图识别
    with tracer.start_as_current_span("intent") as span:
        span.set_attributes({
            "input": json.dumps({"query": "..."}),
            "output": json.dumps({"intents": ["sports"]}),
        })
```

---

## 5. 部署架构

### 5.1 模式一：挂载模式（推荐）

评测系统作为 Python 对象内嵌在 Agent 进程中，SDK 上报、Ingest 消费、评测引擎全部运行在同一进程内。Agent 进程退出时自动释放所有资源。

```mermaid
graph TD
    subgraph "Agent 进程（被测方 + 评测方一体）"
        A1[Agent Pipeline]
        A2[agent_eval_sdk<br/>TraceReporter]
        A3[EvalService<br/>挂载入口]
        subgraph "挂载子模块"
            I[Ingest 消费者]
            Eval[评测引擎]
            API[FastAPI 看板<br/>:8000]
        end
    end

    subgraph "基础设施"
        R[(Redis)]
        DB[(PostgreSQL)]
    end

    subgraph "用户"
        U[浏览器 / curl]
    end

    A1 -->|显式调用| A2
    A2 -->|RPUSH| R
    I -->|定时拉取| R
    I -->|批量写入| DB
    Eval -->|异步评测| DB
    A3 -->|mount| 挂载子模块
    API --> DB
    U -->|查看结果| API
```

**启动方式：**

```bash
# 1) 启动基础设施
docker compose up -d redis postgres

# 2) 直接启动 Agent（评测系统自动挂载）
python agent_main.py \
    --config eval_config.yaml \
    --agent-version v2.3.1 \
    --enabled-layers intent,generation,outcome \
    --sampling-rate 0.05
```

### 5.2 模式二：独立部署

评测系统各组件独立运行，Agent 侧仅使用 SDK 上报数据。

```mermaid
graph TD
    subgraph "Agent 项目（被测方）"
        A1[Agent 代码]
        A2[agent_eval_sdk]
        A3[OTel Tracer]
    end

    subgraph "评测系统（独立部署）"
        R[(Redis)]
        I[Ingest Worker]
        DB[(PostgreSQL)]
        API[FastAPI :8000]
        CW[Celery Worker]
    end

    subgraph "用户"
        U[浏览器 / curl]
    end

    A1 -->|显式调用| A2
    A3 -->|Span Export| A2
    A2 -->|RPUSH| R
    I -->|定时拉取| R
    I -->|批量写入| DB
    API --> DB
    CW -->|异步评测| DB
    U -->|查看结果| API
```

**进程清单：**

| 进程 | 启动命令 | 必需 |
|------|---------|:--:|
| PostgreSQL | `docker compose up -d postgres` | ✅ |
| Redis | `docker compose up -d redis` | ✅ |
| FastAPI | `uvicorn backend.api:app --port 8000` | ✅ |
| Ingest | `python -m backend.workers.ingest_worker` | ✅ |
| Celery Worker | `celery -A backend.workers.eval_worker worker` | ⚠️ 异步评测时需要 |

### 5.3 配置对比

| 配置项 | 挂载模式 | 独立部署 |
|--------|----------|----------|
| 数据库连接 | YAML/TOML 配置文件 | `.env` 环境变量 |
| 功能开关 | CLI 启动参数 | 代码内 config dict |
| 评测 LLM | 代码传参 | `.env` 环境变量 |
| Agent 项目侧依赖 | 只需 Redis 地址 | 只需 Redis 地址 |

---

## 6. 嵌入 Agent 项目

### 6.1 挂载式启动（推荐）

评测系统作为 Agent 进程的一部分启动，无需额外运维进程。

#### 步骤一：创建配置文件

```yaml
# eval_config.yaml
databases:
  postgres:
    url: "postgresql+asyncpg://aura:aura@localhost:5432/agent_eval"
    pool_size: 20
    max_overflow: 10

  redis:
    url: "redis://localhost:6379/0"
    key_prefix: "eval:events:"
```

#### 步骤二：Agent 入口挂载评测系统

```python
# agent_main.py —— Agent 入口，通过 CLI 参数控制评测行为
import argparse
import asyncio
from agent_eval_sdk import TraceReporter
from backend.eval_service import EvalService

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="eval_config.yaml")
    parser.add_argument("--agent-version", required=True)
    parser.add_argument("--enabled-layers", default="intent,retrieval,tool,generation,outcome")
    parser.add_argument("--sampling-rate", type=float, default=0.05)
    parser.add_argument("--ingest-interval", type=int, default=500)
    parser.add_argument("--ingest-batch-size", type=int, default=100)
    args = parser.parse_args()

    # 1) 初始化上报器
    reporter = TraceReporter(
        agent_version=args.agent_version,
        redis_url="redis://localhost:6379/0",
    )

    # 2) 挂载评测系统（Ingest 消费者 + 评测引擎 + API 看板）
    eval_service = EvalService(
        config_file=args.config,
        enabled_layers=args.enabled_layers.split(","),
        sampling_rate=args.sampling_rate,
        llm={
            "model": "gpt-4o",
            "api_key": "sk-xxx",
            "temperature": 0.0,
        },
    )
    await eval_service.mount()
    print(f"评测系统已挂载，启用层: {eval_service.enabled_layers}")

    # 3) 初始化 Agent 管道（注入 Reporter）
    pipeline = AgentPipeline(reporter)

    try:
        # 4) 正常执行 Agent 逻辑
        result = await pipeline.run("用户问题", run_id="run_demo_001")
        print(f"Agent 输出: {result}")
    finally:
        # 5) 释放：先关 Reporter，再卸载评测系统
        reporter.close()
        await eval_service.unmount()
        print("评测系统已安全卸载")

asyncio.run(main())
```

#### 启动命令

```bash
# 基础评测（仅意图 + 生成 + 结果层）
python agent_main.py \
    --config eval_config.yaml \
    --agent-version v2.3.1 \
    --enabled-layers intent,generation,outcome

# 全量评测 + 1%生产采样
python agent_main.py \
    --config eval_config.yaml \
    --agent-version v2.3.1 \
    --enabled-layers intent,retrieval,tool,generation,outcome \
    --sampling-rate 0.01

# 极简模式：不启动评测，仅上报（纯 SDK）
python agent_main.py \
    --agent-version v2.3.1 \
    --enabled-layers none
```

### 6.2 SDK 嵌入模式（仅上报，不挂载评测引擎）

如果只需数据上报、不启动评测引擎，仅使用 SDK 即可：

```bash
# 步骤 1：安装 SDK
pip install agent-eval-sdk

# 步骤 2：在 Agent 入口初始化 Reporter（全局单例）
# 步骤 3：在管道各阶段插入 report_span()
# 步骤 4：管道结束调用 trace.finish()
```

#### 推荐组织方式

```python
# agent_pipeline.py —— 推荐的组织方式

from agent_eval_sdk import TraceReporter

class AgentPipeline:
    def __init__(self, reporter: TraceReporter):
        self.reporter = reporter

    async def run(self, query: str, run_id: str = None) -> str:
        trace = self.reporter.start_trace(
            query=query,
            source="eval" if run_id else "production",
            run_id=run_id,
        )

        try:
            # Phase 1: 意图识别
            intent_result = await self.intent_classifier(query)
            trace.report_span(
                span_type="intent",
                input={"query": query},
                output=intent_result,
                latency_ms=intent_result["latency_ms"],
            )

            # Phase 2: 召回
            docs = await self.retriever(query, intent_result)
            trace.report_span(
                span_type="retrieval",
                input={"intents": intent_result},
                output={"docs": docs},
                latency_ms=docs["latency_ms"],
            )

            # Phase 3: 工具调用
            tool_result = await self.tool_executor(query, docs)
            trace.report_span(
                span_type="tool_call",
                tool_name=tool_result.get("tool"),
                tool_result={"status": "success", "data": tool_result},
                latency_ms=tool_result["latency_ms"],
            )

            # Phase 4: 生成
            response = await self.llm.generate(query, docs, tool_result)
            trace.report_span(
                span_type="generation",
                input={"prompt": response["prompt"]},
                output={"response": response["text"]},
                tokens=response["usage"],
                model=response["model"],
                latency_ms=response["latency_ms"],
            )

            trace.finish(final_response=response["text"], status="success")
            return response["text"]

        except Exception as e:
            trace.finish(status="error")
            raise
```

### 6.3 嵌入模式对比

| 维度 | 挂载模式（6.1） | 纯 SDK 模式（6.2） |
|------|----------------|-------------------|
| 安装依赖 | `agent-eval-sdk` + `agent-eval-backend` | 仅 `agent-eval-sdk` |
| 启动方式 | Agent 进程内 Python 对象挂载 | Agent 进程内 Reporter 实例化 |
| 配置方式 | YAML/TOML 配置文件 + CLI 参数 | 代码中直接传参 |
| 评测引擎 | 进程内运行，实时评测 | 需独立部署评测服务 |
| 适用场景 | 日常开发、CI 评测、小规模压测 | 仅需数据上报，评测由独立服务完成 |

### 6.4 侵入性对比（纯 SDK 模式）

| 阶段 | 原代码行数 | 嵌入后增加 | 增加比例 |
|------|:--:|:--:|:--:|
| 意图识别 | ~3 行 | +1 行 `report_span()` | 33% |
| 召回 | ~3 行 | +1 行 | 33% |
| 工具调用 | ~5 行 | +1 行 | 20% |
| 生成 | ~5 行 | +1 行 | 20% |
| Trace 生命周期 | 0 行 | +2 行 (`start` + `finish`) | — |
| **合计** | ~20 行 | **+6 行** | 30% |

---

## 7. 看板与结果查看

### 7.1 API 端点一览

评测系统通过 REST API 暴露评测结果，可直接用 `curl` 或接入内部看板。

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/tasks` | 创建评测任务 |
| `GET` | `/api/tasks` | 查询任务列表 |
| `GET` | `/api/tasks/{task_id}` | 任务详情 |
| `GET` | `/api/runs` | 查询运行记录 |
| `GET` | `/api/runs/{run_id}` | 运行详情 + 评测得分 |

### 7.2 创建评测任务

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "v2.3.1 全量回归",
    "agent_version": "v2.3.1",
    "case_set_id": "<测试集UUID>",
    "created_by": "qa-team"
  }'
```

### 7.3 查看评测结果

```bash
# 查看某 Agent 版本的所有任务
curl "http://localhost:8000/api/tasks?agent_version=v2.3.1" | python -m json.tool

# 查看某次运行的详细评分
curl "http://localhost:8000/api/runs/<run_id>" | python -m json.tool
```

### 7.4 运行详情响应示例

```json
{
  "id": "run_abc123",
  "task_id": "task_xyz789",
  "status": "completed",
  "agent_version": "v2.3.1",
  "trace_id": "5158ea23-98f8-d246-78cf-a33771c95f15",
  "scores": [
    {
      "span_id": "span_001",
      "score": 92.5,
      "metrics": {
        "precision": 1.0,
        "recall": 0.85,
        "intent_match": true
      },
      "method": "rule",
      "evaluator_version": "1.0.0"
    },
    {
      "span_id": null,
      "score": 87.3,
      "metrics": {
        "bleu": 0.72,
        "rouge_l": 0.81,
        "token_efficiency": 85.0
      },
      "method": "rule+llm",
      "evaluator_version": "1.0.0"
    }
  ]
}
```

### 7.5 数据看板（建议）

当前暂无内置 UI，推荐通过以下方式构建看板：

1. **Grafana** — PostgreSQL 数据源，直接查询 `eval_scores`、`eval_runs` 表
2. **Metabase** — 零代码拖拽式看板
3. **自建前端** — 调用上述 REST API

推荐 SQL 面板：

```sql
-- 按 Agent 版本统计平均分
SELECT
    r.agent_version,
    COUNT(DISTINCT r.id) AS run_count,
    ROUND(AVG(s.score), 2) AS avg_score,
    ROUND(STDDEV(s.score), 2) AS stddev_score
FROM eval_runs r
JOIN eval_scores s ON s.trace_id = r.trace_id
WHERE s.span_id IS NULL       -- Outcome 层（总体分）
GROUP BY r.agent_version
ORDER BY avg_score DESC;
```

```sql
-- 各层得分趋势（最近 50 次运行）
SELECT
    r.created_at::date AS date,
    s.method AS layer,
    ROUND(AVG(s.score), 2) AS avg_score
FROM eval_runs r
JOIN eval_scores s ON s.trace_id = r.trace_id
WHERE r.status = 'completed'
GROUP BY date, layer
ORDER BY date DESC, layer;
```

---

## 8. 常见问题

### Q1：SDK 调用会阻塞 Agent 吗？

**不会。** `report_span()` 底层是 Redis `RPUSH` 操作（O(1) 复杂度），即刻返回。SDK 不做任何同步等待。

### Q2：Agent 进程崩溃，数据会丢吗？

**不会。** 数据写入 Redis List，Redis 本身有 AOF/RDB 持久化。Ingest 消费者独立运行，Agent 崩溃不影响已写入 Redis 的数据。

### Q3：如何从方式 A 切换到方式 B？

无需修改 Agent 代码。只需安装 `agent-eval-sdk[otel]`，注册 `EvalSpanExporter`，框架的自动埋点 Span 会以相同格式写入 Redis。

### Q4：SDK 依赖很重吗？

**不重。** 方式 A 仅依赖 `redis` + `httpx` 两个包。方式 B 额外需要 `opentelemetry-sdk` + `opentelemetry-api`。

### Q5：生产环境如何控制采样率？

**挂载模式**：通过 CLI 参数 `--sampling-rate` 控制，无需改代码。

```bash
python agent_main.py --agent-version v2.3.1 --sampling-rate 0.1
```

**纯 SDK 模式**：在代码中手动判断。

```python
import random

SAMPLING_RATE = 0.1  # 10% 采样

def should_trace():
    return random.random() < SAMPLING_RATE

if should_trace():
    trace = reporter.start_trace(query=query, source="production")
    # ... 上报 Span ...
```

### Q6：挂载模式和独立部署怎么选？

| 场景 | 推荐模式 |
|------|----------|
| 本地开发 / 调试 Agent 行为 | 挂载模式 |
| CI 流水线中跑回归评测 | 挂载模式 |
| 生产 Agent 需要实时评测反馈 | 挂载模式 |
| 多个 Agent 实例共享评测服务 | 独立部署 |
| 评测和 Agent 集群独立扩缩容 | 独立部署 |
| 仅需数据上报、评测由 QA 团队维护 | 纯 SDK + 独立部署 |

### Q7：支持哪些 Agent 框架？

| 框架 | 方式 A | 方式 B（OTel） |
|------|:---:|:---:|
| 自研 Agent | ✅ | ⚠️ 需手动埋点 |
| LangChain | ✅ | ✅ 原生支持 |
| LlamaIndex | ✅ | ✅ 原生支持 |
| OpenAI Agents SDK | ✅ | ✅ 原生支持 |
| CrewAI | ✅ | ✅ |
| AutoGen | ✅ | ✅ |
