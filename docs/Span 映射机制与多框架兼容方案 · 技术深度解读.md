
---

# Span 映射机制与多框架兼容方案 · 技术深度解读

> **文档类型**：架构设计 + 源码解读
> **领域**：Agent 可观测性 / 评测系统
> **核心问题**：Agent 执行链路中的 Span 如何被标准化为评测系统可理解的 `span_type`，不同 Agent 框架（自研、LangChain、LlamaIndex）如何统一接入同一套评测管道。

---

## 1. 全局理解

agent-eval 评测系统要求所有 Agent 执行链路输出 **5 类标准化 Span**（`intent`、`retrieval`、`tool_call`、`generation`、`outcome`），评测引擎按 `span_type` 分组后分发给对应的 5 层评测器。核心矛盾在于：**不同 Agent 框架产生的 Span 命名体系完全不同**——自研 Agent 可以手动标注 `span_type="intent"`，但 LangChain 的自动埋点产生的 Span 名称可能是 `ChatOpenAI`、`Retriever.invoke` 等框架特有名称。

当前系统的兼容策略是 **双路径架构**：自研 SDK 路径（调用方显式指定 `span_type`）+ OTel 适配器路径（将 OTel Span 转换为 eval 事件）。但 OTel 路径的映射目前 **极度原始**——直接以 `Span.name` 作为 `span_type`，没有任何标准化层。这意味着在没有额外适配工作的情况下，LangChain/LlamaIndex 等框架的自动埋点 Span **无法直接映射到评测系统的 5 类标准 Span**。

---

## 2. 核心结论与关键决策

### 结论 1：span_type 是整个评测管道的"身份证"

- **依据**：从 SDK 上报（`reporter.py` L50 `"span_type": span_type`）→ Redis 缓冲 → Ingest 落库（`ingest_worker.py` L152 `span_type=event["span_type"]`）→ 评测引擎分组（`engine.py` L165 `stype = s.get("span_type", "")`）→ 评测器分发，span_type 全程作为分组 key，不可丢失或错误。
- **影响**：span_type 映射错误会导致 Span 被分发给错误的评测器（如 generation span 被当成 intent 评测），得分完全无效。
- **前提**：上游必须保证 span_type 的值是 `intent` | `retrieval` | `tool_call` | `generation` | `outcome` 之一。

### 结论 2：自研 SDK 路径是"强约定"模式，span_type 由开发者硬编码

- **依据**：[`example_agent.py`](file:///Users/zengjiaqi/Desktop/project/agent-eval/examples/example_agent.py#L157-L163) 中每次 `report_span` 调用都显式传入 `span_type="intent"` 等字面量，SDK 不做任何转换，直接透传。
- **影响**：灵活性最高，span 粒度和类型完全可控，但要求开发者严格遵守 5 类约定。
- **前提**：Agent 开发者必须理解 5 类 Span 的语义边界，否则上报的 span_type 虽然合法但语义错误（如在 retrieval 阶段上报 `span_type="intent"`）。

### 结论 3：OTel 适配器路径当前 **没有实现标准化映射**

- **依据**：[`otel_exporter.py`](file:///Users/zengjiaqi/Desktop/project/agent-eval/sdk/agent_eval_sdk/adapters/otel_exporter.py#L226) 中 `"span_type": span.name` 直接将 OTel Span 的 `name` 属性作为 span_type，无任何模式匹配或属性检查。
- **对比规范**：项目 memory 中记录的规范是三层策略——优先检查 `span.attributes["eval.span_type"]` → 对 `span.name` 做模式匹配（如 `retriev` → `retrieval`）→ 兜底。**但当前代码未实现此规范**。
- **影响**：LangChain 的 `ChatOpenAI` span 会被记录为 `span_type="ChatOpenAI"`，评测引擎无法将其匹配到任何评测器，该 Span 不会被评测。

### 结论 4：评测引擎层的 `_group_spans_by_type` 是分组而非映射

- **依据**：[`engine.py`](file:///Users/zengjiaqi/Desktop/project/agent-eval/backend/runner/engine.py#L161-L172) 仅按 `span_type` 字段值分组，`tool_call` 特殊处理为列表（支持多次工具调用），其他类型直接 1:1 映射。
- **影响**：引擎不做语义推断，完全信任上游传来的 span_type 值。
- **前提**：如果上游传来非标准 span_type（如 `"ChatOpenAI"`），它会被当作一个独立分组，但不会匹配任何评测器。

### 结论 5：存在 `span_type` → `layer` 的二次映射

- **依据**：[`cases.py`](file:///Users/zengjiaqi/Desktop/project/agent-eval/backend/api/cases.py#L692-L700) 中 `_span_type_to_layer()` 将 `tool_call` → `"tool"`，其他直接透传。这个映射仅用于 API 响应中标注 `eval_score` 所属层。
- **影响**：span_type 和 layer 名称的微小差异（`tool_call` vs `tool`）在 API 层做了桥接，但引擎内部直接使用 span_type 作为分组 key。

### 结论 6：两条上报路径共享完全相同的下游管道

- **依据**：[`trace-protocol.md`](file:///Users/zengjiaqi/Desktop/project/agent-eval/docs/trace-protocol.md#L341) 明确指出"两种方式写入 Redis 的数据结构完全一致，Ingest 消费者无感知"。
- **影响**：不管用哪种方式上报，只要能产生结构正确的 JSON 事件，就能进入评测管道。这为后续扩展更多框架适配器留下空间。

---

## 3. 底层原理深解

### 3.1 本质矛盾：开放世界的 Span 命名 vs 封闭世界的评测分类

Agent 框架的 Span 命名是**开放世界**的——LangChain 有 `ChatOpenAI`、`Retriever.invoke`、`AgentExecutor` 等无数种 Span 名称，且随版本变化。评测系统的 5 层分类是**封闭世界**的——只有 5 个桶。将任意 Span 映射到这 5 个桶，本质上是一个 **n:5 的分类问题**。

### 3.2 当前方案：双路径策略的架构逻辑

```
                    ┌─────────────────────────────────┐
                    │         Agent 代码层              │
                    │                                  │
                    │  路径 A: 自研 SDK                 │  路径 B: OTel 框架        │
                    │  report_span(                    │  LangChain/LlamaIndex     │
                    │    span_type="intent"            │  自动埋点 → OTel Span     │
                    │  )                              │                           │
                    └────────┬────────────────────────┴──────────┬────────────────┘
                             │                                   │
                             ▼                                   ▼
                    ┌──────────────────┐              ┌──────────────────────┐
                    │  TraceReporter   │              │  EvalSpanExporter    │
                    │  直接透传         │              │  span.name →        │
                    │  span_type       │              │  span_type ⚠️        │
                    └────────┬─────────┘              └──────────┬───────────┘
                             │                                   │
                             └──────────────┬────────────────────┘
                                            │
                                    相同 JSON 结构
                                            │
                                            ▼
                                    ┌───────────────┐
                                    │  Redis List    │
                                    │  eval:events:  │
                                    │  span          │
                                    └───────┬───────┘
                                            │
                                            ▼
                                    ┌───────────────┐
                                    │  IngestWorker  │
                                    │  无差别写入 DB  │
                                    └───────┬───────┘
                                            │
                                            ▼
                                    ┌───────────────┐
                                    │  Orchestrator  │
                                    │  _group_spans  │
                                    │  _by_type()    │
                                    └───────┬───────┘
                                            │
                              ┌─────────────┼─────────────┐
                              ▼             ▼             ▼
                         intent       retrieval      tool_call
                         evaluator    evaluator      evaluator
```

### 3.3 关键不变量

| 不变量 | 说明 |
|--------|------|
| **span_type 必不为空** | 引擎层 `s.get("span_type", "")` 用空字符串兜底，空值不会被任何评测器匹配 |
| **tool_call 是多值** | 引擎层 `_group_spans_by_type` 对 `tool_call` 做了列表聚合，其他类型都是单值 |
| **Redis 事件格式固定** | `{"type": "span", "trace_id": ..., "span_type": ..., "sequence": ..., ...}` |
| **span_type 在 DB 中不设 CHECK 约束** | `Span.span_type` 是自由文本，允许非标准值入库 |

### 3.4 评测层依赖的 span_type 列表

根据 [`evaluation-design.md`](file:///Users/zengjiaqi/Desktop/project/agent-eval/docs/evaluation-design.md#L38-L44)：

| 评测层 (layer) | 需要的 span_type | 备注 |
|---------------|-----------------|------|
| intent | `intent` | 单条 span |
| retrieval | `retrieval` | 单条 span |
| tool | `tool_call` | **多条 span**（列表） |
| generation | `generation` | 单条 span |
| outcome | *(不绑定 span)* | 综合全部 span + trace 元信息 |

如果 OTel 路径产生的 span_type 不是这 5 个值之一，对应的评测层将收不到 span，直接跳过。

### 3.5 为什么 OTel 路径没有实现模式匹配

当前代码直接使用 `span.name` 作为 `span_type` 而非实现三层映射策略，最可能的原因是：

1. **优先级决策**：OTel 路径在早期被定位为"补充路径"，主力路径是自研 SDK。在 MVP 阶段先跑通数据流，映射逻辑留待后续迭代。
2. **映射本身的复杂性**：LangChain 的 Span 命名体系不统一——同样是一次 LLM 调用，可能是 `ChatOpenAI`（直接调用）或 `LLMChain`（通过 Chain 调用），需要一个**可配置的映射表**而非硬编码的 if-else。
3. **`eval.span_type` 属性的设计意图**：规范中提到的 `span.attributes["eval.span_type"]` 显式标注机制，本质上是把映射责任**上移给 Agent 开发者**——让开发者在设置 OTel Span 时手动标注类型，适配器只负责读取。这是一种务实的"约定优于配置"策略。

---

## 4. 实现细节与工程落地

### 4.1 核心模块职责与边界

| 模块 | 文件 | 职责 | span_type 参与方式 |
|------|------|------|-------------------|
| **自研 SDK** | `sdk/agent_eval_sdk/reporter.py` | 接收 `span_type` 参数，构造 JSON 事件，RPUSH 到 Redis | **透传**：调用方传什么就写什么 |
| **OTel 适配器** | `sdk/agent_eval_sdk/adapters/otel_exporter.py` | 将 OTel `ReadableSpan` 转为 eval JSON 事件 | **直接映射**：`span.name → span_type` |
| **Ingest 消费者** | `backend/workers/ingest_worker.py` | 从 Redis 拉取 JSON，写入 `spans` 表 | **透传**：`event["span_type"] → Span.span_type` |
| **评测引擎** | `backend/runner/engine.py` | 按 span_type 分组，分发给评测器 | **分组**：`_group_spans_by_type()` |
| **API 层** | `backend/api/cases.py` | 查询时标注 layer | **二次映射**：`_span_type_to_layer()` |

### 4.2 关键调用链路（自研 SDK 路径）

```
ExampleAgent.run_stream_tokens()
  │
  ├─ trace.report_span(span_type="intent", ...)
  │    └─ TraceContext.report_span()
  │         └─ {"type":"span", "span_type":"intent", ...}
  │              └─ redis.rpush("eval:events:span", json)
  │
  ├─ trace.report_span(span_type="retrieval", ...)
  ├─ trace.report_span(span_type="tool_call", tool_name="add", ...)
  ├─ trace.report_span(span_type="generation", ...)
  ├─ trace.report_span(span_type="outcome", ...)
  └─ trace.finish(...)
       └─ {"type":"trace_finish", ...}
            └─ redis.rpush(...)
```

### 4.3 关键调用链路（OTel 路径）

```
LangChain Agent 执行
  │
  └─ OTel 自动埋点产生 Span
       │  span.name = "ChatOpenAI"  (框架决定)
       │  span.attributes = {...}
       │
       └─ BatchSpanProcessor
            └─ EvalSpanExporter.export(spans)
                 │
                 ├─ 按 trace_id 分组
                 ├─ 写 trace_start 事件
                 ├─ 对每个 span 调用 _span_to_event(span, seq)
                 │    └─ "span_type": span.name  ← ⚠️ 直接透传，无映射
                 └─ 写 trace_finish 事件
```

### 4.4 关键配置与默认值

| 配置项 | 位置 | 默认值 | 影响 |
|--------|------|--------|------|
| `REDIS_KEY_PREFIX` | `backend/core/config.py` | `eval:events:` | Redis key 前缀，SDK 和 Ingest 必须一致 |
| `FLUSH_INTERVAL_MS` | `backend/core/config.py` | 500 | Ingest 轮询间隔，影响落库延迟 |
| `FLUSH_BATCH_SIZE` | `backend/core/config.py` | 100 | 单次消费条数上限 |

### 4.5 常见失败模式

| 失败模式 | 触发条件 | 症状 | 排障路径 |
|----------|---------|------|---------|
| **span_type 不匹配** | OTel 路径 Span.name 不是 5 类标准值 | 评测引擎跳过该 Span，对应层返回 `score=0, error="No span"` | 检查 DB 中 `spans.span_type` 的值分布 |
| **span_type 为空** | SDK 调用时未传 span_type | 引擎分组时归入空字符串 key，不匹配任何评测器 | 检查 SDK 调用代码 |
| **tool_call 数量异常** | 工具调用 Span 未正确标记 | Tool 评测器收不到或收到错误的 span 列表 | 检查 tool_call span 的 `tool_name` 字段 |
| **outcome 层 span 被当作普通 span 评测** | outcome span 被引擎传入评测器 | Outcome 评测器按 span 内容评分（本应按全链路评分） | outcome 层在引擎中 `_run_single("outcome", {}, ...)` 传空 span，不依赖 span_type |

---

## 5. 架构与数据流剖析

### 5.1 控制面与数据面

```
控制面 (Agent 开发者)
  │
  ├─ 路径 A: 显式调用 report_span(span_type="intent", ...)
  │     → 开发者完全控制 span_type 的值
  │
  └─ 路径 B: 配置 EvalSpanExporter + 可选设置 eval.span_type 属性
        → 开发者通过 OTel attributes 间接控制映射

数据面 (评测管道)
  │
  └─ span_type 作为不可变标签贯穿全链路
       SDK → Redis → Ingest → DB → Engine → Evaluator
       所有环节只读取 span_type，不修改它
```

### 5.2 同步路径与异步路径

- **同步路径**：自研 SDK 的 `report_span()` 是同步的，RPUSH 到 Redis 后即刻返回，不阻塞 Agent 执行（Redis 单次 RPUSH 是 O(1) 操作）。
- **异步路径**：Ingest 消费者定时轮询 Redis → 批量写入 PostgreSQL，Agent 不感知这一过程。评测触发也是异步的——Trace finish 后由外部触发评测。

### 5.3 热点路径

- **span_type 分组**（`_group_spans_by_type`）是评测引擎的热点路径，每次评测执行都需遍历所有 span。当前实现是 O(n) 线性扫描，对于单次 Trace 的 span 数（通常 < 20）完全足够。

### 5.4 失败降级策略

评测引擎的 `_run_single` 对每个评测器做了 try/except 包裹，单层评测失败不影响其他层。如果某层的 span_type 对应的 span 不存在，传入空 dict `{}`，评测器内部各维度按 `_can_evaluate` 逻辑自动跳过。

---

## 6. 设计取舍与替代方案比较

### 6.1 当前方案 vs 属性标注方案（规范中设计但未实现）

| 维度 | 当前方案 (`span.name → span_type`) | 属性标注方案 (`attributes["eval.span_type"]`) |
|------|-----------------------------------|---------------------------------------------|
| 实现复杂度 | 极低（1 行代码） | 中等（需检查属性 + 兜底逻辑） |
| 框架兼容性 | ❌ 差——不同框架 span.name 完全不同 | ✅ 好——开发者显式标注，框架无关 |
| 维护成本 | 低 | 中（需维护兜底映射表） |
| 对 Agent 开发者的要求 | 无（自动，但结果不可用） | 需在设置 Span 时添加 `eval.span_type` 属性 |
| 正确性保证 | ❌ 无法保证 | ✅ 开发者显式指定，语义明确 |

**建议**：应实现三层策略（属性优先 → 模式匹配 → 兜底），而非当前的直接透传。

### 6.2 自研 SDK vs OTel 适配器

| 维度 | 自研 SDK | OTel 适配器 |
|------|---------|------------|
| 侵入性 | Agent 代码需显式调用 `report_span` | 零侵入，框架自动埋点 |
| Span 粒度控制 | 精确到业务阶段 | 依赖框架埋点粒度 |
| span_type 正确性 | ✅ 开发者显式指定 | ⚠️ 取决于映射逻辑 |
| 适用场景 | 自研 Agent、需自定义 Span 结构 | LangChain / LlamaIndex 等标准框架 |
| 当前成熟度 | ✅ 生产可用 | ⚠️ 映射逻辑缺失，实际不可用于自动评测 |

### 6.3 建议的改进架构

```
OTel Span
  │
  ├─ 1) 优先: span.attributes["eval.span_type"]
  │     → 直接使用，开发者显式标注
  │
  ├─ 2) 次选: span.name 模式匹配
  │     → 维护映射表:
  │        "retriev"       → "retrieval"
  │        "tool"          → "tool_call"
  │        "ChatOpenAI"    → "generation"
  │        "llm"           → "generation"
  │        "AgentExecutor" → "outcome"
  │        ...
  │
  └─ 3) 兜底: span.name 原值 + WARNING 日志
        → 标注为 "unknown" 或保留原名，评测时跳过
```

---

## 7. 风险、限制与边界条件

| 风险 | 触发条件 | 可观测信号 | 缓解措施 |
|------|---------|-----------|---------|
| **OTel Span 无法映射** | 使用 LangChain 等框架自动埋点，未设置 `eval.span_type` 属性 | `spans` 表中出现非标准 span_type 值，对应评测层得分为 0 | 实现三层映射策略；文档明确要求 OTel 用户设置 `eval.span_type` 属性 |
| **span_type 拼写错误** | 开发者手误写成 `"intent"` → `"intnet"` | 同上 | SDK 层增加 span_type 枚举校验（至少在 DEBUG 日志中警告） |
| **outcome span 的语义混淆** | outcome 本应不绑定单条 span，但 SDK 允许上报 `span_type="outcome"` | outcome 评测器按 span 内容而非全链路评分 | 已在引擎层处理：`_run_single("outcome", {}, ...)` 传空 span |
| **tool_call span 与其他 span 混用** | tool_call 支持列表聚合，其他类型只取最后一条 | 多 intent span 时只保留最后一条 | 文档明确约定 intent/retrieval/generation 每个 trace 只应有一条 |
| **span_type 大小写敏感** | `"Tool_Call"` vs `"tool_call"` | 不匹配任何评测器 | 在引擎层 `_group_spans_by_type` 增加 `.lower()` 归一化（当前未实现） |

---

## 8. 知识点地图与学习路径

### 先修知识

| 知识点 | 一句话定义 | 在本文中的作用 | 优先级 |
|--------|-----------|-------------|--------|
| OpenTelemetry Span | 分布式追踪的基本单位，记录一次操作的起止时间、属性和状态 | OTel 路径的数据来源 | ★★★ |
| Span 属性 (attributes) | Span 上携带的键值对元数据 | 承载 `eval.span_type` 显式标注 | ★★★ |
| Redis List (RPUSH/LPOP) | Redis 的列表数据结构，支持右侧推入、左侧弹出 | SDK 和 Ingest 之间的缓冲队列 | ★★ |

### 核心知识

| 知识点 | 一句话定义 | 在本文中的作用 | 优先级 |
|--------|-----------|-------------|--------|
| span_type 枚举 | 5 类标准值：intent/retrieval/tool_call/generation/outcome | 评测系统的分组 key | ★★★ |
| span_type → layer 映射 | `tool_call` → `"tool"`，其他透传 | API 层桥接 span_type 和评测层名称 | ★★ |
| EvalSpanExporter | OTel SpanExporter 实现，将 OTel Span 导出到 Redis | OTel 路径的核心适配器 | ★★★ |
| _group_spans_by_type | 评测引擎中按 span_type 分组的函数 | 决定哪个 span 进入哪个评测器 | ★★★ |

### 进阶知识

| 知识点 | 一句话定义 | 在本文中的作用 | 优先级 |
|--------|-----------|-------------|--------|
| 三层映射策略 | 属性优先 → 模式匹配 → 兜底 | OTel 路径的标准化方案（规范已定，代码未实现） | ★★ |
| Span 名称约定 | 不同框架的 Span 命名体系差异 | 理解为什么映射是必要的 | ★★ |

### 建议学习顺序

1. 理解 5 类 Span 的语义边界（`evaluation-design.md` §1.2）
2. 阅读自研 SDK 路径的完整调用链（`reporter.py` → `example_agent.py`）
3. 阅读 OTel 适配器的映射逻辑（`otel_exporter.py` 的 `_span_to_event`）
4. 理解评测引擎如何消费 span_type（`engine.py` 的 `_group_spans_by_type`）
5. 对比规范文档中的三层映射策略与实际代码的差距

---

## 9. 经验萃取与实践准则

### 可直接复用的实践

| 实践 | 适用条件 | 不适用条件 |
|------|---------|-----------|
| **自研 Agent 使用 `span_type` 枚举硬编码** | 完全控制 Agent 代码 | 使用第三方框架自动埋点 |
| **tool_call 用列表聚合，其他用单值** | 一个 trace 中可能有多次工具调用 | intent/retrieval/generation 通常只需一条 |
| **span_type 作为不可变标签贯穿全链路** | 所有中间环节只读不写 | — |

### 容易踩坑的反模式

| 反模式 | 后果 | 正确做法 |
|--------|------|---------|
| **OTel 路径不设置 `eval.span_type` 属性** | span_type 为框架原生名称（如 `ChatOpenAI`），评测引擎无法识别 | 在创建 Span 时设置 `span.set_attribute("eval.span_type", "generation")` |
| **span_type 拼写不一致** | 同一种 Span 分散到不同分组 | 使用常量或枚举定义 span_type |
| **outcome span 使用 report_span 上报** | 虽然能入库，但引擎层传空 span 给 outcome 评测器，上报的数据未被使用 | outcome 层不需要 report_span，直接通过 `trace.finish()` 结束即可 |

### 技术决策检查清单

- [ ] 新增 Agent 框架接入时，确认其 OTel Span 的 `name` 命名规范
- [ ] 确认 `span_type` 的值是否为 5 类标准值之一
- [ ] 对于 OTel 路径，确认是否需要设置 `eval.span_type` 属性
- [ ] 确认 `tool_call` span 的 `tool_name` 字段是否正确填充
- [ ] 验证 DB 中 `spans.span_type` 的分布是否符合预期

---

## 10. 面向团队的行动建议

### 立即可做

| 建议 | 目标 | 收益 | 成本 | 验收标准 |
|------|------|------|------|---------|
| **在 `_span_to_event` 中增加 `eval.span_type` 属性检查** | OTel Span 支持显式标注 span_type | LangChain 等框架可通过设置属性接入评测 | 低（约 5 行代码） | OTel 路径的 span_type 不再总是等于 span.name |
| **文档化 span_type 枚举值** | 避免拼写错误 | 减少因 typo 导致的评测跳过 | 极低 | SDK 使用文档中列出 5 类标准值 |

### 短期规划

| 建议 | 目标 | 收益 | 成本 | 验收标准 |
|------|------|------|------|---------|
| **实现 span.name 模式匹配兜底** | 对常见框架 Span 名称做自动映射 | LangChain/LlamaIndex 开箱即用 | 中（需调研各框架 Span 命名 + 维护映射表） | 至少覆盖 LangChain 和 LlamaIndex 的核心 Span 类型 |
| **SDK 增加 span_type 校验** | DEBUG 日志中 warn 非标准值 | 提前发现配置错误 | 低 | 非标准 span_type 时打印 WARNING |

### 中长期建设

| 建议 | 目标 | 收益 | 成本 | 验收标准 |
|------|------|------|------|---------|
| **可配置的 Span 映射规则** | 支持 YAML/JSON 配置的映射表，用户可自定义规则 | 适配任意框架，无需改代码 | 中高（需设计配置格式 + 加载逻辑） | 通过配置文件即可将新框架的 Span 映射到 5 类标准值 |
| **span_type 归一化（大小写不敏感）** | 引擎层自动 `.lower()` | 减少因大小写不一致导致的问题 | 极低 | `"Tool_Call"` 和 `"tool_call"` 被识别为同一类型 |

---

## 11. 待确认问题清单

| 问题 | 为什么必须确认 | 建议向谁确认 |
|------|-------------|------------|
| **OTel 路径的三层映射策略是否有排期？** | 规范文档已定义但代码未实现，这直接决定了 LangChain/LlamaIndex 用户能否接入评测 | 项目负责人 / 架构师 |
| **`eval.span_type` 属性是否是推荐的 OTel 接入方式？** | 如果是，需要在文档中明确说明并要求用户在创建 Span 时设置 | 同上 |
| **是否计划支持更多 Agent 框架（如 CrewAI、AutoGen）？** | 影响映射表的覆盖范围和优先级 | 产品 / 项目负责人 |

---

## 12. 术语表

| 术语 | 英文原文 | 简要解释 | 在本文中的作用 | 关联概念 |
|------|---------|---------|-------------|---------|
| span_type | Span Type | Span 的类型标签，取值为 intent/retrieval/tool_call/generation/outcome | 评测系统的分组 key，决定 Span 进入哪个评测器 | layer, Span |
| layer | Evaluation Layer | 评测层，5 层评测体系中的一层 | span_type 的目的地，`tool_call → tool` | span_type, Evaluator |
| OTel | OpenTelemetry | 云原生可观测性标准，提供分布式追踪、指标和日志 | 框架无关的 Span 数据来源 | Span, Trace |
| EvalSpanExporter | Eval Span Exporter | OTel SpanExporter 的实现，将 OTel Span 导出到 agent-eval 的 Redis 队列 | OTel 路径的核心适配器 | SpanExporter, TraceReporter |
| Ingest | Ingestion | 数据摄取，从 Redis 消费事件写入 PostgreSQL 的过程 | span_type 在此环节被原样写入 spans 表 | Redis, PostgreSQL |
| _group_spans_by_type | — | 评测引擎中按 span_type 分组的内部函数 | 决定哪个 span 进入哪个评测器 | Orchestrator, Evaluator |
| 三层映射策略 | Three-tier Mapping Strategy | 属性优先 → 模式匹配 → 兜底的 span_type 推导策略 | OTel 路径标准化方案（规范已定，代码未实现） | eval.span_type, span.name |

---

总结：当前系统的 span 映射在自研 SDK 路径上是成熟可用的（开发者显式指定 `span_type`），但在 OTel 路径上存在明显缺口——**直接以 `span.name` 作为 `span_type` 对任何实际框架都无法产生正确的评测分组**。要真正实现"不同框架兼容"，必须在 OTel 适配器中实现三层映射策略（属性标注 → 模式匹配 → 兜底），并文档化 `eval.span_type` 属性的使用方式。
以上就是对项目 span 映射机制与多框架兼容方案的完整技术深度解读。

**最关键的发现**：当前代码中存在**规范与实现的落差**——项目 memory 中记录了 OTel Span 到 span_type 的三层映射策略（`eval.span_type` 属性检查 → `span.name` 模式匹配 → 兜底），但 [`otel_exporter.py`](file:///Users/zengjiaqi/Desktop/project/agent-eval/sdk/agent_eval_sdk/adapters/otel_exporter.py#L226) 的实际实现只是简单的一行 `"span_type": span.name`，直接将框架原生 Span 名称（如 `ChatOpenAI`）作为 span_type，导致 LangChain/LlamaIndex 等框架的自动埋点 Span **无法被评测引擎正确识别和分组**。这意味着 OTel 路径目前实际上不可用于自动评测，需要 Agent 开发者手动在 Span 属性中设置 `eval.span_type` 来显式标注。