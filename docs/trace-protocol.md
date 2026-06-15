# 数据上报协议

> 定义被测 Agent 与评测系统之间的数据上报接口、SDK API、落表策略及 OpenTelemetry 集成方案。

---

## 1. 上报架构

```mermaid
graph TD
    subgraph Agent 侧
        SDK[自研上报 SDK]
        OTel[OpenTelemetry SDK]
    end

    subgraph 评测系统侧
        Redis[(Redis<br/>缓冲队列)]
        Ingest[Ingest 消费者]
        DB[(PostgreSQL<br/>traces + spans)]
    end

    SDK -->|批量写入| Redis
    OTel -->|Span 导出| SDK
    Redis -->|定时消费| Ingest
    Ingest -->|批量 INSERT| DB
```

- SDK 将 Span 事件写入 Redis List，进程崩溃数据不丢
- Ingest 消费者定时从 Redis 拉取，批量写入 PostgreSQL
- 后续可替换 Redis 为 Kafka，接口不变

---

## 2. 上报两种场景

```mermaid
sequenceDiagram
    participant ES as 评测系统
    participant AG as Agent
    participant SDK as 上报 SDK
    participant Redis as Redis
    participant Ingest as Ingest 消费者
    participant DB as PostgreSQL

    rect rgb(240, 248, 255)
        Note over ES,DB: 场景 A：评测触发（source = 'eval'）
        ES->>AG: 创建 eval_run + expected_snapshot，传入 run_id
        AG->>SDK: start_trace(run_id=xxx, source='eval')
        SDK->>Redis: RPUSH trace_start 事件
        Ingest->>Redis: 拉取 → INSERT traces (status='running')
        AG->>SDK: report_span(span_type='intent', ...)
        SDK->>Redis: RPUSH span 事件
        AG->>SDK: report_span(span_type='retrieval', ...)
        SDK->>Redis: RPUSH span 事件
        AG->>SDK: report_span(span_type='tool_call', ...)
        SDK->>Redis: RPUSH span 事件
        AG->>SDK: report_span(span_type='generation', ...)
        SDK->>Redis: RPUSH span 事件
        Ingest->>Redis: 定时拉取 → 批量 INSERT spans
        AG->>SDK: finish_trace(status='success')
        SDK->>Redis: RPUSH trace_finish 事件
        Ingest->>Redis: 拉取 → UPDATE traces SET status='success'
        ES->>DB: 检测 trace 完成，触发评测
    end

    rect rgb(255, 248, 240)
        Note over AG,DB: 场景 B：生产采样（source = 'production'）
        AG->>SDK: start_trace(source='production')
        SDK->>Redis: RPUSH
        AG->>SDK: report_span(...) × N
        SDK->>Redis: RPUSH × N
        AG->>SDK: finish_trace(...)
        SDK->>Redis: RPUSH
        Ingest->>DB: 批量写入 traces + spans
    end
```

---

## 3. SDK API

### 3.1 初始化

```python
from agent_eval_sdk import TraceReporter

reporter = TraceReporter(
    agent_version="v2.3.1",           # 必填，对应 traces.agent_version

    # Redis 配置（默认值）
    redis_url="redis://localhost:6379/0",
    redis_key_prefix="eval:events:",   # Redis Key 前缀

    flush_interval_ms=500,            # 刷新间隔，见第 7 节
    flush_batch_size=100,             # 单次拉取条数上限
)
```

### 3.2 Trace 生命周期

```python
# 开始一次 Trace
trace = reporter.start_trace(
    query="帮我查一下上周五NBA湖人队的比赛结果",
    context={"user_id": "u123"},
    source="eval",                     # 'eval' | 'production'
    run_id="run_xxx",                  # 评测场景传入，绑定 eval_runs
    source_ref=None,                   # 生产环境引用
)

# 逐阶段上报 Span（即刻写入 Redis，不阻塞 Agent）
trace.report_span(
    span_type="intent",
    input={"query": "..."},
    output={"intents": ["sports_query"], "confidence": 0.95},
    latency_ms=120,
    tokens={"input": 50, "output": 15},
    model="intent-classifier-v3",
)

trace.report_span(
    span_type="retrieval",
    input={"query_rewrites": [...]},
    output={"results": [...]},
    latency_ms=350,
)

trace.report_span(
    span_type="tool_call",
    input={"params": {"query": "NBA Lakers"}},
    output={"result": {...}},
    tool_name="web_search",
    tool_params={"query": "NBA Lakers"},
    tool_result={"status": "success", "data": "..."},
    latency_ms=1800,
)

trace.report_span(
    span_type="generation",
    input={"prompt": {...}},
    output={"response": "湖人队以112:105战胜..."},
    tokens={"input": 1200, "output": 350},
    model="gpt-4o",
    latency_ms=2800,
)

# 结束 Trace
trace.finish(
    final_response="湖人队以112:105战胜凯尔特人...",
    status="success",
)
```

### 3.3 异常场景

```python
trace.finish(status="error")
trace.finish(status="timeout")
```

---

## 4. 落表映射

### 4.1 start_trace → traces 表

| SDK 参数 | traces 列 | 说明 |
|---------|----------|------|
| 自动生成 | `id` | UUID |
| `agent_version` | `agent_version` | 初始化时配置 |
| `query` | `query` | 用户输入 |
| `context` | `context` | JSONB，用户上下文 |
| `source` | `source` | eval / production |
| `run_id` | —（通过 eval_runs 关联） | Ingest 消费时回写 eval_runs.trace_id |
| `source_ref` | `source_ref` | 生产环境引用 |
| — | `status` | 初始值 'running' |
| — | `overall_score` | NULL，评测后回填 |
| — | `total_latency_ms` | NULL，finish 时汇总 |
| — | `total_tokens` | NULL，finish 时汇总 |
| — | `total_cost_usd` | NULL，finish 时计算 |

### 4.2 report_span → spans 表

| SDK 参数 | spans 列 | 说明 |
|---------|---------|------|
| 自动生成 | `id` | UUID |
| `trace.id` | `trace_id` | 外键 |
| `span_type` | `span_type` | intent/retrieval/tool_call/generation |
| 自动递增 | `sequence` | 同一 trace 内自增 |
| `input` | `input` | JSONB |
| `output` | `output` | JSONB |
| `tool_name` | `tool_name` | 仅 tool_call |
| `tool_params` | `tool_params` | 仅 tool_call |
| `tool_result` | `tool_result` | 仅 tool_call |
| — | `tool_status` | 从 result 提取 |
| `latency_ms` | `latency_ms` | 毫秒 |
| `tokens` | `tokens` | JSONB |
| `model` | `model` | 模型名 |
| — | `score` | NULL，评测后回填 |

### 4.3 finish_trace → traces 表（UPDATE）

| SDK 参数 | traces 列 |
|---------|----------|
| `final_response` | `final_response` |
| `status` | `status` |
| 汇总自 spans | `total_latency_ms` |
| 汇总自 spans | `total_tokens` |
| 计算 | `total_cost_usd` |

---

## 5. 评测场景关联机制

```mermaid
sequenceDiagram
    participant ES as 评测引擎
    participant DB as PostgreSQL
    participant AG as Agent
    participant SDK as SDK
    participant Redis as Redis
    participant Ingest as Ingest

    ES->>DB: 1. INSERT eval_run (status='running', expected_snapshot={...})
    DB-->>ES: run_id = 'run_xxx'
    ES->>AG: 2. 调用 Agent，传入 run_id
    AG->>SDK: 3. start_trace(run_id='run_xxx', ...)
    SDK->>Redis: RPUSH trace_start 事件（含 run_id）
    AG->>SDK: 4. report_span(...) × N
    SDK->>Redis: RPUSH span 事件 × N
    AG->>SDK: 5. finish_trace(...)
    SDK->>Redis: RPUSH trace_finish 事件
    Ingest->>Redis: 消费 trace_start → INSERT traces，获取 trace_id
    Ingest->>DB: UPDATE eval_runs SET trace_id = 'trace_yyy'
    Ingest->>Redis: 消费 span → INSERT spans
    Ingest->>Redis: 消费 trace_finish → UPDATE traces SET status='success'
    ES->>DB: 6. 检测 trace 完成 → 触发评测
```

---

## 6. OpenTelemetry 集成

```mermaid
graph TD
    subgraph 方式 A: 自研 SDK
        A1[Agent 代码] -->|调用| A2[TraceReporter]
        A2 -->|写入| A3[(Redis)]
    end

    subgraph 方式 B: 原生 OTel
        B1[Agent 框架<br/>LangChain/LlamaIndex] -->|自动埋点| B2[OTel Tracer]
        B2 -->|Span Processor| B3[Eval Exporter]
        B3 -->|写入| B4[(Redis)]
    end

    Ingest[Ingest 消费者]
    DB[(PostgreSQL)]
    A3 --> Ingest
    B4 --> Ingest
    Ingest --> DB
```

### 6.1 OTel Span → spans 表映射

| OTel 属性 | spans 列 |
|-----------|---------|
| `Span.name` | `span_type`（如 `"intent"`, `"tool.web_search"`） |
| `Span.start_time / end_time` | `latency_ms` |
| `Span.attributes["input"]` | `input`（JSONB） |
| `Span.attributes["output"]` | `output`（JSONB） |
| `Span.attributes["tool_name"]` | `tool_name` |
| `Span.attributes["llm.model"]` | `model` |
| `Span.attributes["llm.usage"]` | `tokens`（JSONB） |

---

### 6.2 配置切换

项目中通过单一开关选择上报方式：

```python
# config.py
TRACE_MODE = "sdk"   # "sdk" | "otel"
REDIS_URL = "redis://localhost:6379/0"
AGENT_VERSION = "v2.3.1"
```

#### 方式 A：自研 SDK（TRACE_MODE = "sdk"）

```python
# 适合：无 OTel 集成的 Agent，或想手动控制 Span 粒度
from agent_eval_sdk import TraceReporter

reporter = TraceReporter(
    agent_version=AGENT_VERSION,
    redis_url=REDIS_URL,
)

# Agent 代码中显式调用 report_span
with reporter.start_trace(query="...", source="eval") as trace:
    # ... 意图识别 ...
    trace.report_span(span_type="intent", ...)
    # ... 召回 ...
    trace.report_span(span_type="retrieval", ...)
    # ... 工具调用 ...
    trace.report_span(span_type="tool_call", ...)
    # ... 生成 ...
    trace.report_span(span_type="generation", ...)
```

#### 方式 B：原生 OTel（TRACE_MODE = "otel"）

```python
# 适合：LangChain / LlamaIndex 等已自动埋点的 Agent 框架
# 无需 import agent_eval_sdk，只注册 Exporter

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# 注册 Eval Exporter（将 OTel Span 写入 Redis）
provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(EvalSpanExporter(redis_url=REDIS_URL))
)

# Agent 框架正常执行，自动埋点，无需手动 report_span
# LangChain 的 chain.invoke() 会自动产生 intent/retrieval/tool/generation 对应的 Span
```

#### 切换对比

| 维度 | 方式 A（自研 SDK） | 方式 B（原生 OTel） |
|------|-------------------|---------------------|
| 侵入性 | Agent 代码需显式调用 `report_span` | 零侵入，框架自动埋点 |
| Span 粒度 | 手动控制，精确到业务阶段 | 依赖框架埋点粒度 |
| 依赖 | 仅 `agent_eval_sdk` | `opentelemetry-sdk` + EvalExporter |
| 适用 | 自研 Agent、需要自定义 Span 结构 | LangChain / LlamaIndex 等标准框架 |
| Redis Key | `eval:events:span` | 相同，Ingest 无差别消费 |

两种方式写入 Redis 的数据结构完全一致，Ingest 消费者无感知。

---

### 6.3 Span 类型映射规则

OTel 模式下，框架自动埋点产生的 Span 使用框架原生命名（如 `ChatOpenAI`、`retriever`、`llm_predict`），而评测系统需要标准化的 5 类 `span_type`（`intent`、`retrieval`、`tool_call`、`generation`、`outcome`）。`EvalSpanExporter` 通过**三层策略**自动完成映射推导。

#### 三层映射策略

```
优先级：属性优先 → 模式匹配 → 兜底
    │            │           │
    │            │           └── 第 3 层：返回 span.name 原值
    │            └── 第 2 层：span.name.lower() 包含匹配（最长匹配优先）
    └── 第 1 层：span.attributes["eval.span_type"] 显式标注
```

**第 1 层 — 属性优先（最高优先级）**

开发者可在 Span 属性中显式设置 `eval.span_type`，完全绕过所有自动推导逻辑：

```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("custom_processing") as span:
    span.set_attribute("eval.span_type", "intent")
    # → span_type = "intent"，不进入后续层
```

支持的合法值：`intent` / `retrieval` / `tool_call` / `generation` / `outcome`，以及别名 `"tool"`（自动映射为 `"tool_call"`）。非法值记录 WARNING 日志后降级到第 2 层。

**第 2 层 — 模式匹配**

当未设置 `eval.span_type` 属性时，对 `span.name.lower()` 执行包含匹配（substring match）。映射规则来源：

| 来源 | 文件 | 优先级 | 容错 |
|------|------|--------|------|
| YAML 映射表 | `sdk/agent_eval_sdk/adapters/span_type_mapping.yaml` | 默认加载 | 不可用时使用硬编码 fallback |
| 硬编码 fallback | `otel_exporter.py` `_FALLBACK_RULES` | YAML 不可用时启用 | 与 YAML 内容同步 |
| 运行时自定义 | `EvalSpanExporter(span_type_rules={...})` | 覆盖前两者 | — |

**最长匹配原则**：当多个 pattern 同时命中时，取 pattern 字符串最长者，确保更具体的规则生效。例如 `"chatopenai"`（9 字符）优先于 `"chat"`（4 字符），`"query_engine"`（12 字符）优先于 `"query"`（5 字符）。

内置映射表覆盖范围：

| span_type | 匹配的 span.name 模式 |
|-----------|----------------------|
| `generation` | `chatopenai`, `chat`, `llm`, `openai.chat`, `chain.invoke`, `chain`, `llm_predict`, `complete`, `model_response` |
| `retrieval` | `retriever`, `retriev`, `similarity_search`, `vectorstore`, `query`, `retrieve`, `node_parser` |
| `tool_call` | `tool`, `tool_call`, `function_call` |
| `outcome` | `agentexecutor`, `agent`, `query_engine`, `openai_agent`, `agent_runner` |

**第 3 层 — 兜底返回**

当所有模式均不匹配时，直接返回 `span.name` 原值。这种方式保证了系统对未知框架的兼容性——span_name 虽然不一定精确对应五个标准类型之一，但数据不会丢失，可通过后续 Ingest 层或评测引擎做二次处理。

#### 自定义映射

```python
from agent_eval_sdk.adapters import EvalSpanExporter

exporter = EvalSpanExporter(
    redis_url="redis://localhost:6379/0",
    agent_version="v2.3.1",
    span_type_rules={
        "MyCustomLLM":    "generation",
        "MySearchEngine": "retrieval",
        "MyAgentRunner":  "outcome",
    },
)
```

自定义规则与内置规则合并，key 不区分大小写，自定义覆盖内置。

#### YAML 映射表结构

```yaml
# sdk/agent_eval_sdk/adapters/span_type_mapping.yaml
# 格式：pattern: span_type
# 匹配方式：span.name.lower() 中包含 pattern 时触发

# === LangChain ===
chatopenai:         generation
retriever:          retrieval
tool:               tool_call

# === LlamaIndex ===
llm_predict:        generation
query_engine:       outcome

# === OpenAI Agents SDK ===
function_call:      tool_call
model_response:     generation
```

YAML + 硬编码 fallback 双重保障：即使 YAML 文件损坏或被误删，系统仍可正常工作。

---

### 6.4 不同 Agent 框架的兼容性

OTel 适配器通过三层映射策略实现了对不同 Agent 框架的无侵入兼容。以下是各框架的接入方式和兼容性详情。

#### 框架兼容性矩阵

| 框架 | 接入方式 | span_type 获取方式 | 是否需要手动标注 | 覆盖完整度 | 备注 |
|------|---------|-------------------|----------------|-----------|------|
| **自研 Agent** | 自研 SDK（`TraceReporter`） | 代码显式传入 `span_type` 参数 | ❌ 不需要 | ★★★★★ | 完全控制，无需映射 |
| **LangChain** | OTel 适配器（自动埋点） | 模式匹配（`chatopenai`→generation 等） | ❌ 不需要 | ★★★★☆ | 13 条内置规则，覆盖核心 Span |
| **LlamaIndex** | OTel 适配器（自动埋点） | 模式匹配（`llm_predict`→generation 等） | ❌ 不需要 | ★★★★☆ | 6 条内置规则，覆盖核心 Span |
| **OpenAI Agents SDK** | OTel 适配器（自动埋点） | 模式匹配 + 建议显式标注 | ⚠️ 建议标注 | ★★★☆☆ | 5 条内置规则，`function_call` 已覆盖；复杂 agent 链建议用 `eval.span_type` |
| **其他 OTel 框架** | OTel 适配器 | 模式匹配（兜底） | ⚠️ 建议提供自定义映射表或显式标注 | ★★☆☆☆ | 内置映射表可能不覆盖，建议使用时传入 `span_type_rules` |
| **任意 OTel 应用** | OTel 适配器 | 第 1 层属性标注 | ✅ 需要手动设置 `eval.span_type` | ★★★★★ | 最灵活，但需开发者主动标注 |

#### 各框架 Span 映射详情

**自研 Agent（SDK 模式）**

```python
from agent_eval_sdk import TraceReporter

trace = reporter.start_trace(query="...", source="eval")
trace.report_span(span_type="intent", ...)      # ← 显式传入
trace.report_span(span_type="retrieval", ...)
trace.report_span(span_type="generation", ...)
```

无需任何映射逻辑，直接使用标准 `span_type`。

**LangChain**

LangChain 自动埋点产生的典型 Span 名称及映射：

| LangChain Span 名称 | OTel 适配器映射 | 说明 |
|--------------------|----------------|------|
| `ChatOpenAI` | `generation` | LLM 调用 |
| `Retriever` | `retrieval` | 检索操作 |
| `similarity_search` | `retrieval` | 向量检索 |
| `tool` | `tool_call` | 工具调用 |
| `AgentExecutor` | `outcome` | Agent 执行器 |
| `chain.invoke` | `generation` | Chain 调用 |

LangChain 的 `agent` 和 `agentexecutor` 模式的匹配顺序：「agentexecutor」（12 字符）优先于「agent」（5 字符），确保更具体的匹配生效。

**LlamaIndex**

| LlamaIndex Span 名称 | OTel 适配器映射 | 说明 |
|----------------------|----------------|------|
| `llm_predict` | `generation` | LLM 预测 |
| `complete` | `generation` | 补全 |
| `query_engine` | `outcome` | 查询引擎 |
| `query` | `retrieval` | 查询 |
| `retrieve` | `retrieval` | 检索 |
| `node_parser` | `retrieval` | 文档解析 |

LlamaIndex 的 `query_engine` 与 `query` 的匹配顺序：「query_engine」（12 字符）优先于「query」（5 字符），引擎级 Span 正确映射为 `outcome` 而非 `retrieval`。

**OpenAI Agents SDK**

| OpenAI Agent Span 名称 | OTel 适配器映射 | 说明 |
|-----------------------|----------------|------|
| `function_call` | `tool_call` | 函数调用 |
| `tool_call` | `tool_call` | 工具调用（别名） |
| `model_response` | `generation` | 模型响应 |
| `openai_agent` | `outcome` | Agent 运行 |
| `agent_runner` | `outcome` | Agent 执行器 |

对于复杂 Agent 链或自定义 Span，建议设置 `eval.span_type` 属性以绕过模式匹配的不确定性。

#### 接入新框架指南

对任意支持 OpenTelemetry 的 Agent 框架，有三种接入方式，按推荐度排序：

1. **提供自定义映射表**（推荐）：
   ```python
   EvalSpanExporter(span_type_rules={
       "my_framework.llm": "generation",
       "my_framework.search": "retrieval",
       "my_framework.runner": "outcome",
   })
   ```

2. **在 Span 属性中设置 `eval.span_type`**（最精确）：
   ```python
   span.set_attribute("eval.span_type", "generation")
   ```

3. **依赖兜底机制**（零配置，精度最低）：
   不传任何规则，span_type 将直接使用 `span.name`。评测引擎会收到非标准 span_type，但数据不会丢失。

---

## 7. Redis 缓冲与消费策略

```mermaid
sequenceDiagram
    participant AG as Agent 进程
    participant R as Redis
    participant IN as Ingest 消费者
    participant DB as PostgreSQL

    AG->>R: report_span(intent)<br/>RPUSH eval:events:span
    Note over AG,R: O(1) 写入，即刻返回

    AG->>R: report_span(retrieval)<br/>RPUSH
    AG->>R: report_span(tool_call)<br/>RPUSH
    AG->>R: report_span(generation)<br/>RPUSH

    loop 定时轮询（500ms）或事件数 ≥ 100
        IN->>R: BRPOPLPUSH / LRANGE + LTRIM
        R-->>IN: 批量拉取事件
        IN->>DB: 批量 INSERT spans<br/>（单条 SQL，多行 VALUES）
    end

    AG->>R: finish_trace<br/>RPUSH trace_finish
    IN->>R: BRPOPLPUSH
    R-->>IN: trace_finish 事件
    IN->>DB: UPDATE traces<br/>SET status, final_response...
    IN->>DB: COMMIT
```

**为什么用 Redis 而不是内存缓冲**：

| 方案 | 进程崩溃 | 数据量 | 扩展性 |
|------|---------|--------|--------|
| 内存 RingBuffer | ❌ 数据全丢 | 受进程内存限制 | 单机 |
| Redis List | ✅ AOF/RDB 持久化 | TB 级 | 多 Agent 共享队列 |

**关键参数**：

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `flush_interval_ms` | 500 | Ingest 定时轮询间隔。低频时保证数据不滞留超过 500ms |
| `flush_batch_size` | 100 | 单次拉取条数上限。高频时批量消费，减少 DB 往返 |
| `redis_key_prefix` | `eval:events:` | Key 前缀。`{prefix}span` 存 Span 事件，`{prefix}trace` 存 Trace 生命周期事件 |

**两个参数是或关系，先到先触发**：

- 高频场景：500ms 内事件堆积超过 100 条 → `flush_batch_size` 先触发，拉取一批写入 DB
- 低频场景：事件数始终不到 100 → `flush_interval_ms` 先触发（500ms 到），保证最多 500ms 延迟

Ingest 使用 `LRANGE + LTRIM` 原子操作，消费的同时从队列移除。Redis 单线程模型保证消费期间新 `RPUSH` 进来的事件追加到队列尾部，不会被误删或遗漏。

---

## 8. 后续扩展：Redis → Kafka

```mermaid
graph LR
    subgraph V1[初版: Redis 队列]
        SDK1[SDK] -->|RPUSH| R[Redis]
        R -->|BRPOPLPUSH| I1[Ingest]
        I1 -->|批量 INSERT| DB1[(PG)]
    end

    subgraph V2[扩展: Kafka]
        SDK2[SDK] -->|Produce| K[Kafka]
        K -->|Consumer Group| I2[Ingest]
        I2 -->|批量 INSERT| DB2[(PG)]
    end
```

切换方式：修改 `TraceReporter` 初始化参数，SDK API 不变。

```python
# 初版
reporter = TraceReporter(channel="redis", redis_url="...")

# 扩展
reporter = TraceReporter(channel="kafka", kafka_config={...})
```
