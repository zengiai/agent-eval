"""Example Agent —— 简易 Agent 接入 Agent Eval 评测系统。

支持:
  - 工具调用（add / subtract）
  - 流式输出（SSE 兼容）
  - 5 类 Span 全覆盖: intent / retrieval / tool_call / generation / outcome

用法:
    export DATABASE_URL="postgresql+asyncpg://agent_eval:agent_eval_pass@localhost:5433/agent_eval"
    python examples/example_agent.py              # 命令行模式
    python examples/agent_server.py               # Web 模式 (http://localhost:8800)
"""

import json as _json
import os
import sys
import time
import uuid
from typing import Any, Dict, Generator, List, Optional

from openai import OpenAI

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent_eval_sdk import TraceReporter

# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════

LLM_CONFIG = {
    "model": "qwen3.7-plus",
    "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "api_key": os.environ.get(
        "DASHSCOPE_API_KEY",
        "sk-ws-H.HYXRDH.Pfz9.MEMCHxknGBxxfv-ymjc6Y-QPJuZhiNz9hioGE2Cq5qAZsAoCIHXpJPDBB7PqdQSAbbbGVD3iCQRfgqcalASLdpF0_E4N",
    ),
}

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
AGENT_VERSION = "example-v1.0.0"


# ═══════════════════════════════════════════════════════════════════════════
# Tools
# ═══════════════════════════════════════════════════════════════════════════

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "计算两个数的和，返回 a + b 的结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "第一个加数"},
                    "b": {"type": "number", "description": "第二个加数"},
                },
                "required": ["a", "b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subtract",
            "description": "计算两个数的差，返回 a - b 的结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "被减数"},
                    "b": {"type": "number", "description": "减数"},
                },
                "required": ["a", "b"],
            },
        },
    },
]


def execute_tool(name: str, args: dict) -> str:
    """执行工具调用，返回结果字符串。"""
    if name == "add":
        return str(args["a"] + args["b"])
    elif name == "subtract":
        return str(args["a"] - args["b"])
    return f"未知工具: {name}"


# ═══════════════════════════════════════════════════════════════════════════
# ExampleAgent
# ═══════════════════════════════════════════════════════════════════════════

class ExampleAgent:
    """支持工具调用 + 流式输出 + 全 Span 上报的简易 Agent。

    Pipeline: Intent → Retrieval → [Tool ↔ LLM] → Generation → Outcome
    """

    SYSTEM_PROMPT = (
        "你是一个有帮助的AI助手。请用简洁、准确的中文回答用户问题。"
        "如果用户的问题涉及数学计算，请使用提供的 add / subtract 工具。"
    )

    def __init__(
        self,
        reporter: Optional[TraceReporter],
        model: str = "qwen3.7-plus",
        base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key: Optional[str] = None,
    ):
        self.reporter = reporter
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    # ── 非流式（兼容旧接口）─────────────────────────────────────────

    def run(self, query: str, run_id: Optional[str] = None) -> str:
        """同步模式：收集完整回复后返回。"""
        full = ""
        for chunk in self.run_stream(query, run_id):
            full += chunk
        return full

    # ── 流式（生成器）───────────────────────────────────────────────

    def run_stream(
        self, query: str, run_id: Optional[str] = None
    ) -> Generator[str, None, None]:
        """流式执行管道，逐 token yield。

        Pipeline:
          1. intent      → 意图分类
          2. retrieval   → 知识检索（模拟）
          3. tool_call   → 工具调用循环（模型自主决定）
          4. generation  → 最终回复生成（流式）
          5. outcome     → 综合评分
        """
        trace = (
            self.reporter.start_trace(
                query=query,
                source="eval" if run_id else "production",
                run_id=run_id,
            )
            if self.reporter
            else None
        )

        try:
            # ── Span 1: intent ──────────────────────────────────────
            t0 = time.monotonic()
            intent_result = self._classify_intent(query)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if trace:
                trace.report_span(
                    span_type="intent",
                    input={"query": query},
                    output=intent_result,
                    latency_ms=latency_ms,
                    model=self.model,
                )

            # ── Span 2: retrieval（模拟）─────────────────────────────
            t0 = time.monotonic()
            retrieval_result = self._simulate_retrieval(query, intent_result)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if trace:
                trace.report_span(
                    span_type="retrieval",
                    input={"intents": intent_result},
                    output=retrieval_result,
                    latency_ms=latency_ms,
                )

            # ── Span 3+4: tool_call + generation（循环）──────────────
            messages = self._build_messages(query, intent_result, retrieval_result)
            total_usage = {"input": 0, "output": 0}
            tool_call_count = 0

            # 工具调用循环（最多 3 轮）
            for _round in range(3):
                t0 = time.monotonic()
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.3,
                    max_tokens=1024,
                )
                choice = resp.choices[0]
                msg = choice.message

                # Token 统计
                if resp.usage:
                    total_usage["input"] += resp.usage.prompt_tokens or 0
                    total_usage["output"] += resp.usage.completion_tokens or 0

                # 模型要调工具
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        fn_name = tc.function.name
                        fn_args = _json.loads(tc.function.arguments)
                        tool_t0 = time.monotonic()
                        tool_output = execute_tool(fn_name, fn_args)
                        tool_latency = int((time.monotonic() - tool_t0) * 1000)

                        if trace:
                            trace.report_span(
                                span_type="tool_call",
                                tool_name=fn_name,
                                tool_params=fn_args,
                                tool_result={"status": "success", "output": tool_output},
                                latency_ms=tool_latency,
                                model=self.model,
                            )
                        tool_call_count += 1

                        messages.append({
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": fn_name,
                                        "arguments": tc.function.arguments,
                                    },
                                }
                            ],
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_output,
                        })
                    continue  # 继续下一轮，让模型基于工具结果回答

                # 模型生成最终回复 → 流式 yield
                final_text = msg.content or ""
                latency_ms = int((time.monotonic() - t0) * 1000)

                if trace:
                    trace.report_span(
                        span_type="generation",
                        input={
                            "query": query,
                            "intent": intent_result,
                            "tool_rounds": _round,
                        },
                        output={"response": final_text},
                        tokens={
                            "input": total_usage["input"],
                            "output": total_usage["output"],
                        },
                        model=self.model,
                        latency_ms=latency_ms,
                    )

                # ── Span 5: outcome ──────────────────────────────────
                if trace:
                    trace.report_span(
                        span_type="outcome",
                        input={
                            "intent": intent_result,
                            "tool_calls": tool_call_count,
                        },
                        output={
                            "response_length": len(final_text),
                            "tokens": total_usage,
                        },
                        tokens=total_usage,
                        model=self.model,
                    )

                if trace:
                    trace.finish(final_response=final_text, status="success")
                yield final_text
                return

            # Fallback: 达到最大轮次仍无最终回复
            last_text = "抱歉，我无法完成这个计算。"
            if trace:
                trace.report_span(
                    span_type="generation",
                    output={"response": last_text},
                    tokens=total_usage,
                    model=self.model,
                )
                trace.report_span(span_type="outcome", output={"error": "max_rounds"})
                trace.finish(final_response=last_text, status="error")
            yield last_text

        except Exception:
            if trace:
                trace.finish(status="error")
            raise

    # ── 流式逐 token 版本 ───────────────────────────────────────────

    def run_stream_tokens(
        self, query: str, run_id: Optional[str] = None
    ) -> Generator[str, None, None]:
        """流式 + 真正逐 token 输出的版本（用于 SSE）。

        对于非 generation 阶段（intent / retrieval / tool_call）用 [status] 消息通知前端，
        对于 generation 阶段逐 token yield。
        """
        trace = (
            self.reporter.start_trace(
                query=query,
                source="eval" if run_id else "production",
                run_id=run_id,
            )
            if self.reporter
            else None
        )

        try:
            # ── Span 1: intent ──────────────────────────────────────
            yield "[status] 🔍 正在识别意图..."
            t0 = time.monotonic()
            intent_result = self._classify_intent(query)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if trace:
                trace.report_span(
                    span_type="intent",
                    input={"query": query},
                    output=intent_result,
                    latency_ms=latency_ms,
                    model=self.model,
                )
            yield f"[status] ✅ 意图: {', '.join(intent_result.get('intents', ['unknown']))}"

            # ── Span 2: retrieval ───────────────────────────────────
            yield "[status] 📚 正在检索知识..."
            t0 = time.monotonic()
            retrieval_result = self._simulate_retrieval(query, intent_result)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if trace:
                trace.report_span(
                    span_type="retrieval",
                    input={"intents": intent_result},
                    output=retrieval_result,
                    latency_ms=latency_ms,
                )
            yield f"[status] ✅ 检索到 {retrieval_result.get('count', 0)} 条相关信息"

            # ── 工具 + 生成循环 ─────────────────────────────────────
            messages = self._build_messages(query, intent_result, retrieval_result)
            total_usage = {"input": 0, "output": 0}
            tool_call_count = 0
            final_text = ""

            for _round in range(3):
                t0 = time.monotonic()

                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    temperature=0.3,
                    max_tokens=1024,
                    stream=True,
                )

                # 收集流式 chunks，区分 tool_calls 和 content
                tool_call_deltas: Dict[int, Dict[str, str]] = {}
                content_parts: List[str] = []
                final_tool_calls: List[Dict] = []
                is_tool_mode = False

                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    # 处理 tool_calls delta
                    if delta.tool_calls:
                        is_tool_mode = True
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_call_deltas:
                                tool_call_deltas[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc_delta.id:
                                tool_call_deltas[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_call_deltas[idx]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_call_deltas[idx]["arguments"] += tc_delta.function.arguments

                    # 处理 content delta（仅在非 tool 模式时流式输出）
                    if delta.content and not is_tool_mode:
                        content_parts.append(delta.content)
                        yield delta.content

                # 解析 tool_calls
                for idx in sorted(tool_call_deltas.keys()):
                    td = tool_call_deltas[idx]
                    if td["name"]:
                        final_tool_calls.append({
                            "id": td["id"],
                            "function": {
                                "name": td["name"],
                                "arguments": td["arguments"],
                            },
                        })

                if final_tool_calls:
                    # 工具调用模式 → 执行工具
                    for tc in final_tool_calls:
                        fn_name = tc["function"]["name"]
                        fn_args = _json.loads(tc["function"]["arguments"])
                        tool_t0 = time.monotonic()
                        tool_output = execute_tool(fn_name, fn_args)
                        tool_latency = int((time.monotonic() - tool_t0) * 1000)

                        yield f"[tool] 🔧 {fn_name}({fn_args}) = {tool_output}"

                        if trace:
                            trace.report_span(
                                span_type="tool_call",
                                tool_name=fn_name,
                                tool_params=fn_args,
                                tool_result={"status": "success", "output": tool_output},
                                latency_ms=tool_latency,
                                model=self.model,
                            )
                        tool_call_count += 1

                        messages.append({
                            "role": "assistant",
                            "tool_calls": [{
                                "id": tc["id"],
                                "type": "function",
                                "function": tc["function"],
                            }],
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_output,
                        })
                    continue  # 下一轮

                # 文本回复模式
                final_text = "".join(content_parts)
                latency_ms = int((time.monotonic() - t0) * 1000)
                break

            # ── Span 4: generation ──────────────────────────────────
            if trace:
                trace.report_span(
                    span_type="generation",
                    input={
                        "query": query,
                        "intent": intent_result,
                        "tool_rounds": tool_call_count,
                    },
                    output={"response": final_text},
                    tokens=total_usage,
                    model=self.model,
                    latency_ms=latency_ms if final_text else None,
                )

            # ── Span 5: outcome ──────────────────────────────────────
            if trace:
                trace.report_span(
                    span_type="outcome",
                    input={
                        "intent": intent_result,
                        "tool_calls": tool_call_count,
                    },
                    output={
                        "response_length": len(final_text),
                        "tokens": total_usage,
                    },
                    tokens=total_usage,
                    model=self.model,
                )

            if not final_text and not tool_call_count:
                final_text = "抱歉，我无法处理这个请求。"
                yield final_text

            if trace:
                trace.finish(
                    final_response=final_text,
                    status="success" if final_text else "error",
                )

        except Exception:
            if trace:
                trace.finish(status="error")
            yield "[error] 处理出错，请重试"
            raise

    # ── Internal Methods ─────────────────────────────────────────────

    def _classify_intent(self, query: str) -> dict:
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "请判断以下用户问题的类别，仅回复 JSON：\n\n"
                    f"用户问题：{query}\n\n"
                    '输出格式：{"intents":["类别1"],"confidence":0.0~1.0}\n'
                    "类别：knowledge_query, chitchat, task_request, code_generation, "
                    "translation, summarization, math_calculation"
                ),
            },
        ]
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0.1, max_tokens=128
        )
        content = resp.choices[0].message.content.strip()
        try:
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
            return _json.loads(content)
        except _json.JSONDecodeError:
            return {"intents": ["unknown"], "confidence": 0.5, "raw": content}

    def _simulate_retrieval(self, query: str, intent: dict) -> dict:
        """模拟知识检索（演示用）。"""
        return {
            "count": 3,
            "results": [
                {"id": "doc_001", "score": 0.95, "snippet": f"关于「{query[:20]}」的相关文档片段 1"},
                {"id": "doc_002", "score": 0.82, "snippet": f"关于「{query[:20]}」的相关文档片段 2"},
                {"id": "doc_003", "score": 0.71, "snippet": f"关于「{query[:20]}」的相关文档片段 3"},
            ],
        }

    def _build_messages(self, query: str, intent: dict, retrieval: dict) -> list:
        intents = intent.get("intents", [])
        hint = ""
        if intents and intents[0] != "unknown":
            hint = f"检测到意图: {', '.join(intents)}。"
        if retrieval.get("count", 0) > 0:
            snippets = "\n".join(
                f"- {r['snippet']}" for r in retrieval.get("results", [])[:2]
            )
            hint += f"\n相关知识:\n{snippets}"

        return [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": f"{hint}\n\n用户问题：{query}"},
        ]


# ═══════════════════════════════════════════════════════════════════════════
# OtelExampleAgent —— OTel 埋点版 Agent
# ═══════════════════════════════════════════════════════════════════════════

class OtelExampleAgent(ExampleAgent):
    """OTel 埋点版 Agent —— 用 OpenTelemetry Span 替代 SDK report_span()。

    继承 ExampleAgent 的全部内部方法（_classify_intent / _simulate_retrieval /
    _build_messages），仅覆写 run_stream_tokens() 的埋点方式。

    依赖：opentelemetry-api + opentelemetry-sdk + EvalSpanExporter
    """

    def __init__(
        self,
        model: str = "qwen3.7-plus",
        base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key: Optional[str] = None,
    ):
        # OTel 模式不需要 TraceReporter
        super().__init__(reporter=None, model=model, base_url=base_url, api_key=api_key)

    def run_stream_tokens(
        self, query: str, run_id: Optional[str] = None
    ) -> Generator[str, None, None]:
        """流式 + OTel Span 埋点版本。

        Pipeline: intent → retrieval → [tool_call ↔ generation] → outcome
        每阶段通过 tracer.start_as_current_span() 创建 Span，
        EvalSpanExporter 自动导出到 Redis。
        """
        import json as _json_mod
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import StatusCode

        tracer = otel_trace.get_tracer(__name__)

        # 根 Span：使用 start_as_current_span 确保子 Span 正确建立父子关系
        with tracer.start_as_current_span(
            "agent_execution",
            attributes={
                "agent_version": "example-otel-v1.0.0",
                "query": query,
                "source": "eval" if run_id else "production",
                "run_id": run_id or "",
            },
        ) as root_span:
            try:
                # ── Span 1: intent ──────────────────────────────────
                yield "[status] 🔍 正在识别意图..."
                t0 = time.monotonic()
                with tracer.start_as_current_span(
                    "intent_classify",
                    attributes={"eval.span_type": "intent", "llm.model": self.model},
                ) as span:
                    intent_result = self._classify_intent(query)
                    span.set_attribute("input", _json_mod.dumps({"query": query}))
                    span.set_attribute("output", _json_mod.dumps(intent_result))
                    span.set_attribute(
                        "latency_ms", int((time.monotonic() - t0) * 1000)
                    )
                yield f"[status] ✅ 意图: {', '.join(intent_result.get('intents', ['unknown']))}"

                # ── Span 2: retrieval ───────────────────────────────
                yield "[status] 📚 正在检索知识..."
                t0 = time.monotonic()
                with tracer.start_as_current_span(
                    "knowledge_retrieval",
                    attributes={"eval.span_type": "retrieval"},
                ) as span:
                    retrieval_result = self._simulate_retrieval(query, intent_result)
                    span.set_attribute("input", _json_mod.dumps({"intents": intent_result}))
                    span.set_attribute("output", _json_mod.dumps(retrieval_result))
                    span.set_attribute(
                        "latency_ms", int((time.monotonic() - t0) * 1000)
                    )
                yield f"[status] ✅ 检索到 {retrieval_result.get('count', 0)} 条相关信息"

                # ── 工具 + 生成循环 ─────────────────────────────────
                messages = self._build_messages(query, intent_result, retrieval_result)
                total_usage = {"input": 0, "output": 0}
                tool_call_count = 0
                final_text = ""
                gen_latency_ms = 0

                for _round in range(3):
                    t0 = time.monotonic()

                    stream = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        tools=TOOLS,
                        tool_choice="auto",
                        temperature=0.3,
                        max_tokens=1024,
                        stream=True,
                    )

                    tool_call_deltas: Dict[int, Dict[str, str]] = {}
                    content_parts: List[str] = []
                    final_tool_calls: List[Dict] = []
                    is_tool_mode = False

                    for chunk in stream:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta is None:
                            continue

                        if delta.tool_calls:
                            is_tool_mode = True
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index
                                if idx not in tool_call_deltas:
                                    tool_call_deltas[idx] = {
                                        "id": tc_delta.id or "",
                                        "name": "",
                                        "arguments": "",
                                    }
                                if tc_delta.id:
                                    tool_call_deltas[idx]["id"] = tc_delta.id
                                if tc_delta.function:
                                    if tc_delta.function.name:
                                        tool_call_deltas[idx]["name"] += tc_delta.function.name
                                    if tc_delta.function.arguments:
                                        tool_call_deltas[idx]["arguments"] += tc_delta.function.arguments

                        if delta.content and not is_tool_mode:
                            content_parts.append(delta.content)
                            yield delta.content

                    # 解析 tool_calls
                    for idx in sorted(tool_call_deltas.keys()):
                        td = tool_call_deltas[idx]
                        if td["name"]:
                            final_tool_calls.append({
                                "id": td["id"],
                                "function": {
                                    "name": td["name"],
                                    "arguments": td["arguments"],
                                },
                            })

                    if final_tool_calls:
                        for tc in final_tool_calls:
                            fn_name = tc["function"]["name"]
                            fn_args = _json.loads(tc["function"]["arguments"])
                            tool_t0 = time.monotonic()
                            tool_output = execute_tool(fn_name, fn_args)
                            tool_latency = int((time.monotonic() - tool_t0) * 1000)

                            yield f"[tool] 🔧 {fn_name}({fn_args}) = {tool_output}"

                            with tracer.start_as_current_span(
                                "tool_execution",
                                attributes={
                                    "eval.span_type": "tool_call",
                                    "tool_name": fn_name,
                                    "llm.model": self.model,
                                },
                            ) as tool_span:
                                tool_span.set_attribute(
                                    "tool_params", _json_mod.dumps(fn_args)
                                )
                                tool_span.set_attribute(
                                    "tool_result",
                                    _json_mod.dumps(
                                        {"status": "success", "output": tool_output}
                                    ),
                                )
                                tool_span.set_attribute("latency_ms", tool_latency)
                            tool_call_count += 1

                            messages.append({
                                "role": "assistant",
                                "tool_calls": [{
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": tc["function"],
                                }],
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": tool_output,
                            })
                        continue

                    final_text = "".join(content_parts)
                    gen_latency_ms = int((time.monotonic() - t0) * 1000)
                    break

                # ── Span 4: generation ──────────────────────────────
                with tracer.start_as_current_span(
                    "response_generation",
                    attributes={
                        "eval.span_type": "generation",
                        "llm.model": self.model,
                    },
                ) as span:
                    span.set_attribute(
                        "input",
                        _json_mod.dumps({
                            "query": query,
                            "intent": intent_result,
                            "tool_rounds": tool_call_count,
                        }),
                    )
                    span.set_attribute(
                        "output", _json_mod.dumps({"response": final_text})
                    )
                    span.set_attribute(
                        "llm.usage",
                        _json_mod.dumps(total_usage) if total_usage else "",
                    )
                    if final_text:
                        span.set_attribute("latency_ms", gen_latency_ms)

                # ── Span 5: outcome ─────────────────────────────────
                with tracer.start_as_current_span(
                    "outcome_evaluation",
                    attributes={"eval.span_type": "outcome", "llm.model": self.model},
                ) as span:
                    span.set_attribute(
                        "input",
                        _json_mod.dumps({
                            "intent": intent_result,
                            "tool_calls": tool_call_count,
                        }),
                    )
                    span.set_attribute(
                        "output",
                        _json_mod.dumps({
                            "response_length": len(final_text),
                            "tokens": total_usage,
                        }),
                    )
                    span.set_attribute(
                        "llm.usage",
                        _json_mod.dumps(total_usage) if total_usage else "",
                    )

                if not final_text and not tool_call_count:
                    final_text = "抱歉，我无法处理这个请求。"
                    yield final_text

                root_span.set_attribute("final_response", final_text)
                root_span.set_status(
                    StatusCode.OK if final_text else StatusCode.ERROR
                )

            except Exception:
                root_span.set_status(StatusCode.ERROR)
                yield "[error] 处理出错，请重试"
                raise


# ═══════════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import asyncio as _asyncio

    print("=" * 60)
    print("  Example Agent —— 工具调用 + 流式 + 5 Span 演示")
    print("=" * 60)

    print("\n[1/4] 初始化 SDK ...")
    try:
        reporter = TraceReporter(agent_version=AGENT_VERSION, redis_url=REDIS_URL)
        reporter._redis.ping()
        print(f"       ✅ Redis OK ({REDIS_URL})")
    except Exception as e:
        print(f"       ⚠️  Redis 不可用 ({e})")
        reporter = None

    print("\n[2/4] 初始化 Agent ...")
    agent = ExampleAgent(reporter=reporter, **LLM_CONFIG)

    print("\n[3/4] 示例查询 ...")
    queries = [
        "123 + 456 等于多少？",
        "1000 减去 357 是多少？",
    ]
    for q in queries:
        run_id = str(uuid.uuid4())
        print(f"\n  Q: {q}")
        print(f"  A: ", end="", flush=True)
        try:
            for token in agent.run_stream_tokens(q, run_id):
                print(token, end="", flush=True)
            print()
        except Exception as e:
            print(f"  ❌ {e}")

    print("\n[4/4] Flush Redis → DB ...")
    if reporter:
        try:
            _asyncio.run(_flush_redis_to_db())
            print("       ✅ 数据已入库")
        except Exception as e:
            print(f"       ⚠️  {e}")
    print("\n完成！")


async def _flush_redis_to_db():
    from backend.workers.ingest_worker import IngestWorker
    import redis.asyncio as aioredis

    worker = IngestWorker()
    worker._redis = aioredis.from_url(REDIS_URL)
    consumed = 0
    for _ in range(100):
        if await worker._redis.llen(worker._span_key) == 0:
            break
        try:
            await worker._consume_batch()
            consumed += 1
        except Exception:
            break
    await worker._redis.aclose()
    print(f"       共消费 {consumed} 批")


if __name__ == "__main__":
    main()
