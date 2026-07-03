# Agent Eval — Project Story

## Inspiration

In early 2026, LLM agents were transitioning from "barely working" to "actually useful," but one fundamental question remained unanswered: **how do you know if your agent is getting better or worse?**

Change a single line of prompt, swap a model, tweak a tool-calling strategy — the impact of these changes on end-to-end quality is, in most teams, judged by gut feeling and "let's run a few cases and see." Without a quantitative evaluation framework, every agent iteration is a flight in the dark.

We've seen these scenarios play out too many times: a seemingly harmless prompt tweak drops intent classification accuracy by 15%. A model upgrade *increases* hallucination rates. A new retrieval source pushes the most relevant document out of the Top 5. Worse still, these regressions often surface only when users complain.

**Evaluation shouldn't be a one-off "run before shipping" ritual — it should be a full-lifecycle companion for every agent.** This was the starting point for Agent Eval: building an automated evaluation platform for LLM agent execution pipelines — one that makes every layer measurable, every change traceable, and every regression alertable.

---

## What it does

Agent Eval is a **full-pipeline, layered, continuous** agent evaluation system. It's not just a "scorer" — it covers the complete loop from data collection to version comparison:

### Five-Layer Evaluation

Each layer of the agent execution pipeline is measured independently:

| Layer | What It Evaluates | Key Metrics |
|-------|-------------------|-------------|
| **Intent** | Are user intents classified correctly? | Intent match accuracy, NER F1-score, confidence calibration |
| **Retrieval** | How precise and comprehensive are search results? | Precision@K, Recall@K, MRR, NDCG, diversity |
| **Tool** | Are the right tools called with the right parameters? | Tool selection accuracy, parameter correctness, sequence correctness, success rate |
| **Generation** | Is the answer factual and complete? | Factual accuracy, completeness, hallucination detection, semantic similarity, language quality |
| **Outcome** | Was the task completed end-to-end? | Task completion, latency score, token efficiency, error recovery |

### Dual-Mode Data Collection

- **Custom SDK**: 4 lines of code embedded in the agent pipeline, with precise control over span granularity across `intent` → `retrieval` → `tool_call` → `generation`
- **OpenTelemetry Adapter**: Zero-code integration with LangChain, LlamaIndex, OpenAI Agents SDK, and other major frameworks via a three-tier span-type mapping strategy

### Intelligent Eval Set Accumulation

A composite pipeline — "LLM periodic sampling → confidence-based triage → human review safety net" — automatically converts production traces into long-term evaluation cases, so your eval set keeps growing richer over time.

### Scientific Version Comparison

Paired t-tests + Cohen's d effect size + Bootstrap confidence intervals. Beyond mean scores, the system tells you whether differences are statistically significant and how large the effect really is.

### 7×24 Agent Runtime

The system doesn't just evaluate agents — it *is* a continuously running agent service itself. It accepts messages via Telegram Gateway, routes them through the Brain module for intent parsing and command execution, and uses a Scheduler to manage periodic sampling and daily report generation.

---

## How we built it

### Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Backend | Python + FastAPI | Native async support, seamless asyncio ecosystem integration |
| Database | PostgreSQL 16 + JSONB | B-tree indexes on structured columns, JSONB for dynamic span structures |
| Message Queue | Redis List → Kafka (planned) | V1 lightweight bootstrap; V2 for multi-consumer scaling |
| Async Tasks | Celery + asyncio | Non-blocking evaluation; API returns immediately with 202 |
| Eval LLM | Qwen / GPT-4o | Fixed temperature=0 for reproducible results |
| Frontend | Embedded Dashboard (HTML + vanilla JS) | Zero-dependency deployment, no separate frontend project |
| Deployment | Docker Compose | One-command startup for PostgreSQL + Redis |

### Architecture

The system follows a **five-layer architecture**:

```
Access Layer (FastAPI + Telegram Gateway)
    ↓
Scheduling Layer (Celery task queue + Scheduler cron)
    ↓
Execution Layer (Agent call → SDK report → Ingest consume → 5 evaluators)
    ↓
Storage Layer (PostgreSQL + JSONB, full persistence of traces/spans/scores)
    ↓
Analysis Layer (Aggregation → Version comparison → Regression alerts → Report export)
```

Key design decisions include:

1. **Phased event stream over single-shot reporting**: The agent reports events as they happen; the eval system assembles them in real-time. No buffering of full traces on the agent side.
2. **Evaluator plugin architecture**: Five evaluators managed through a registry, supporting multi-version coexistence, dynamic switching, and independent upgrades.
3. **Multi-sample LLM judging with fallback**: Critical dimensions sample 3 times and average; auto-switch to a cheaper model when the token budget exceeds 80%.
4. **Mounted mode**: The eval engine can be embedded as a Python object inside the agent process — zero extra ops for dev and debugging.

---

## Challenges we ran into

### 1. The LLM-as-Judge Consistency Problem

The biggest dilemma: evaluating LLMs *with* LLMs yields scores that are not reproducible. The same answer might score 85 today and 78 tomorrow from GPT-4o. We addressed this with three layered strategies:

- **temperature=0** + multi-sample averaging
- **Dual verification**: critical dimensions (hallucination detection) cross-validate deterministic and LLM-based judgments
- **Evaluator version pinning**: every evaluation records `evaluator_version`; version comparison enforces identical evaluator versions

Even with these measures, LLM Judge variance remains a continuous optimization target — we've designed in periodic human calibration and Cohen's Kappa consistency checks as planned features.

### 2. Evaluation Degradation Without Expected Values

Production traces have no pre-annotated ground truth. Metrics like Precision@K and NERAccuracy depend entirely on `expected_*` data and must be skipped when absent.

We designed an **"detect → skip → renormalize"** adaptive formula system: it dynamically decides which dimensions to evaluate based on the actual availability of expected data, then proportionally redistributes weights across remaining dimensions. The Outcome layer works almost fully without expected values; the Generation layer handles most dimensions through LLM self-knowledge fallback.

### 3. OTel Span-to-Eval-Layer Mapping

Different agent frameworks produce wildly different span names — LangChain calls it `ChatOpenAI`, LlamaIndex calls it `llm_predict`, and custom frameworks may call it anything. Unifying these into the four standard span types (`intent/retrieval/tool_call/generation`) requires both precision and broad compatibility.

The solution is a **three-tier strategy**: attribute priority (`eval.span_type` explicit annotation) → pattern matching (YAML mapping table + longest-match principle) → fallback (return raw `span.name`). With YAML + hardcoded fallback dual-safety, the system keeps running even if the config file is corrupted.

### 4. Race Conditions in Async Evaluation

Converting evaluation from synchronous to asynchronous introduced a classic "wrong Run" race condition: when the same trace is submitted for evaluation multiple times in rapid succession, querying "the latest EvalRun" could grab a concurrent, not-yet-completed run.

The fix is straightforward but easily overlooked: **precise lookup by `eval_run_id`** rather than relying on timestamp ordering. Additionally, EvalScore links through `eval_run_id` as a foreign key, supporting independent multi-round scoring records for the same trace with full audit trail.

### 5. Evaluator Version Management

Evaluator upgrades change scores for the same trace — a trace scored with v1.0 yesterday is not comparable to v1.1 today. This directly breaks the fairness of version comparisons.

We introduced full SemVer version management for evaluators: every evaluation records `evaluator_version`; version comparison enforces consistency checks. Bug fixes bump PATCH, new dimensions bump MINOR, framework overhauls bump MAJOR. Old and new versions can run in parallel, with a "re-evaluate" feature to rescore historical data using newer evaluators.

---

## Accomplishments that we're proud of

### Complete Data Loop

From agent execution → SDK reporting → Ingest consumption → five-layer evaluation → score persistence → version comparison, every step writes to the database and every step is traceable. The evaluation system's own output (eval_scores) is itself structured and analyzable.

### Adaptive Formula System

Rather than rigidly applying fixed formulas, the system dynamically adapts to the actual content of expected values: `mode: "any"` degrades to binary judgment, `divergent_ok: true` skips completeness checks, `nice_to_have` checkpoints add bonus points without penalty. The same set of evaluators handles everything from strict factual Q&A to open-ended recommendations.

### Automated LLM Eval Set Pipeline

Upgraded from "passively waiting for annotations" to "proactive sampling + confidence triage + human safety net" as an assembly-line process: daily stratified sampling → LLM batch annotation with confidence scores → `≥ 0.9` auto-ingested, `0.6–0.9` queued for human review, `< 0.6` held in candidate pool. Evaluation sets are no longer the bottleneck.

### Agent Runtime: From "Eval Tool" to "Evaluable Service"

The system doesn't just evaluate others — it runs its own 7×24 Telegram agent. Daily conversations accumulate production traces, which are then converted into evaluation cases through the sampling pipeline, forming a flywheel of **"produce data → evaluate itself → iterate itself."**

### Zero-Invasion OTel Adapter

Agent frameworks need zero code changes. Just register an Exporter, and LangChain/LlamaIndex auto-instrumentation spans automatically flow into the evaluation system. The three-tier mapping strategy covers core span types of major frameworks, with custom mapping tables for quick adoption of new ones.

### 2,800+ Lines of Test Code

Especially in the Agent Runtime module — a 7×24 long-running service doesn't have HTTP timeout safety nets, so tests must be more thorough. Five test files cover intent parsing, message routing, Brain execution, Scheduler orchestration, and Runtime lifecycle. The test code alone exceeds the entire project size of the first commit.

---

## What we learned

### The "Meta-Problem" of Evaluation Systems

An evaluation system *itself* needs to be evaluated. Are LLM Judge scores reliable? How biased are the eval set annotations? Do the same cases produce consistent scores across different evaluator versions? We invested heavily in thinking through these questions during design — annotation source tracking (`annotation_method` JSONB), evaluator version pinning, periodic human calibration, and stale case detection. These are meta-problems that no evaluation system can afford to ignore.

### Documentation First, Code Second

The project follows a strict 7-phase development workflow: requirement analysis → solution design → gate review → development → code review → test verification → delivery summary. In practice, the phase that feels most "time-wasting" — documentation — turns out to be the highest-ROI investment: a good design doc eliminates 80% of rework before a single line is written.

### Layered Beats Monolithic

The initial instinct was to build a "universal scorer" — feed in query + response, get back a score. But an agent is not a black box. Its behavior spans four stages (intent, retrieval, tool, generation), each with a completely different definition of "good." The five-layer approach looks more complex on paper, but it makes each layer's evaluation logic clear and maintainable.

### JSONB Is a Natural Fit for Agent Data

Agent pipeline structures change frequently across versions — new tool types, evolving intent taxonomies, adjusted retrieval strategies. Relational schema migrations incur too much overhead. JSONB + schema-on-read keeps the data model elastic, while GIN indexes still enable efficient queries.

### Redis as a Buffer Layer Was the Right Call

The SDK doesn't write directly to PostgreSQL. Instead, it writes to Redis Lists, and an independent Ingest consumer pulls and batch-inserts. This design makes agent-side reporting nearly zero-latency (O(1) RPUSH) while preventing database connection pools from being overwhelmed by concurrent reports. Process crashes don't lose data — Redis AOF has your back.

---

## What's next for Agent Eval

### Production-Grade Message Queue Upgrade

Redis List works well for V1's lightweight needs. As sampling volume and concurrent agent counts grow, we're planning a migration to Kafka — supporting consumer groups, message persistence, and horizontal scaling.

### LLM Judge Self-Calibration

Introduce an automated pipeline for periodic human calibration + Cohen's Kappa consistency checks. The goal: when the gap between LLM Judge and human scoring exceeds a threshold, automatically trigger prompt tuning or model switching.

### Enhanced Dashboard Visualization

Upgrade from the current embedded HTML dashboard to a React frontend: radar charts for overall scores, box plots for per-layer score distributions, trend line charts for version history, and heatmaps for regressed cases. Make evaluation results not just data, but interactive insights.

### Multimodal Agent Evaluation

The current system focuses on text pipelines. As multimodal agents (image understanding + text generation) proliferate, we need to extend the span protocol to support image inputs and factual accuracy evaluation for visual question answering.

### Community & Open Source

The SDK and evaluator protocols already have clean boundaries. The plan is to release the SDK and OTel adapter as independent open-source packages, enabling any agent project to integrate with the evaluation system at minimal cost. Evaluator prompt templates will also be opened for community contributions.

### Cost-Aware Evaluation

Incorporate token consumption and API call costs into evaluation dimensions, enabling decisions along the quality–cost Pareto frontier. Answer not just "which version is better?" but also "is the extra cost worth it?"
