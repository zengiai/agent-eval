"""OpenTelemetry SpanExporter —— 将 OTel Span 导出到 Agent Eval 评测系统。

通过注册此 Exporter，LangChain / LlamaIndex 等已集成 OTel 自动埋点的 Agent 框架
无需任何代码侵入即可将执行链路数据上报到评测系统。

用法:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from agent_eval_sdk.adapters import EvalSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(
            EvalSpanExporter(
                redis_url="redis://localhost:6379/0",
                agent_version="v2.3.1",
            )
        )
    )
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from agent_eval_sdk.reporter import TraceReporter


# ── OTel 依赖延迟导入（作为可选依赖）─────────────────────────────────────
_otel_import_error: Optional[ImportError] = None
try:
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import StatusCode
except ImportError as e:
    SpanExporter = object  # type: ignore[assignment]
    SpanExportResult = None
    ReadableSpan = None
    StatusCode = None
    _otel_import_error = e


class EvalSpanExporter(SpanExporter):
    """将 OpenTelemetry Span 导出到 Agent Eval 评测系统 Redis 队列。

    核心映射：
    ┌──────────────────────┬──────────────────────┐
    │ OTel Span 属性        │ Eval 事件字段          │
    ├──────────────────────┼──────────────────────┤
    │ Span.name            │ span_type             │
    │ start_time/end_time  │ latency_ms            │
    │ attributes["input"]  │ input                 │
    │ attributes["output"] │ output                │
    │ attributes["llm.*"]  │ model / tokens        │
    │ attributes["tool_*"] │ tool_name/params/result│
    └──────────────────────┴──────────────────────┘

    Span 按 trace_id 分组，识别根 Span 自动生成 trace_start / trace_finish 事件。
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        agent_version: str = "unknown",
        redis_key_prefix: str = "eval:events:",
        source: str = "production",
        run_id: Optional[str] = None,
    ):
        if _otel_import_error is not None:
            raise ImportError(
                "使用 EvalSpanExporter 需要安装 OpenTelemetry SDK：\n"
                "    pip install agent-eval-sdk[otel]"
            ) from _otel_import_error

        self._agent_version = agent_version
        self._source = source
        self._run_id = run_id
        self._reporter = TraceReporter(
            agent_version=agent_version,
            redis_url=redis_url,
            redis_key_prefix=redis_key_prefix,
        )
        self._redis = self._reporter._redis
        self._span_key = self._reporter._span_key

    def export(self, spans: Sequence[ReadableSpan]) -> "SpanExportResult":
        """导出一批已结束的 OTel Span 到 Redis。

        按 trace_id 分组，每个 trace 组按顺序写入：
        1. trace_start（含 query、context 等元信息）
        2. span × N（每个 Span 一个事件，按 start_time 排序）
        3. trace_finish（含 status）
        """
        # ── 按 trace_id 分组 ──────────────────────────────────────────
        groups: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"root": None, "spans": []}
        )

        for span in spans:
            tid = _format_trace_id(span.context.trace_id)
            groups[tid]["spans"].append(span)
            if span.parent is None:
                groups[tid]["root"] = span

        # ── 逐组写入 Redis ────────────────────────────────────────────
        for tid, group in groups.items():
            root: Optional[ReadableSpan] = group["root"]
            all_spans: List[ReadableSpan] = group["spans"]

            # 按 start_time 排序，保证 sequence 一致
            all_spans.sort(key=lambda s: s.start_time)

            # 1) trace_start — 从根 Span 属性提取元信息
            attrs = _safe_attrs(root) if root else {}

            def _parse(key: str, default: Any = None) -> Any:
                """从 OTel 属性中提取值，自动 JSON 解析字符串。"""
                val = attrs.get(key, default)
                if isinstance(val, str) and val.strip().startswith(("{", "[")):
                    try:
                        return json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
                return val

            self._push({
                "type": "trace_start",
                "trace_id": tid,
                "agent_version": attrs.get("agent_version", self._agent_version),
                "query": attrs.get("query", root.name if root else ""),
                "context": _parse("context", {}),
                "source": attrs.get("source", self._source),
                "run_id": attrs.get("run_id", self._run_id),
                "source_ref": attrs.get("source_ref"),
                "session_id": attrs.get("session_id"),
                "timestamp": time.time(),
            })

            # 2) span × N
            for seq, span in enumerate(all_spans, start=1):
                self._push(_span_to_event(span, seq))

            # 3) trace_finish — 从根 Span 状态推断
            final_status = _map_status(root) if root else "success"
            self._push({
                "type": "trace_finish",
                "trace_id": tid,
                "final_response": attrs.get("final_response"),
                "status": final_status,
                "timestamp": time.time(),
            })

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """关闭 Redis 连接。"""
        self._reporter.close()

    def _push(self, event: dict) -> None:
        """RPUSH 一个 JSON 事件到 Redis List。"""
        payload = json.dumps(event, ensure_ascii=False, default=str)
        self._redis.rpush(self._span_key, payload)


# ── 工具函数 ────────────────────────────────────────────────────────────

def _safe_attrs(span: Optional[ReadableSpan]) -> Dict[str, Any]:
    """安全提取 Span 属性为 dict。"""
    if span is None or span.attributes is None:
        return {}
    return dict(span.attributes)


def _format_trace_id(trace_id: int) -> str:
    """将 OTel 128-bit trace_id 转为 32 字符 hex 字符串。"""
    return format(trace_id, "032x")


def _map_status(root_span: Optional[ReadableSpan]) -> str:
    """从 OTel Span Status 映射为 eval 状态字符串。"""
    if root_span is None:
        return "success"
    if not root_span.status.is_ok:
        code = root_span.status.status_code
        if code == StatusCode.ERROR:
            return "error"
        return "error"
    return "success"


def _span_to_event(span: ReadableSpan, sequence: int) -> dict:
    """将单个 OTel ReadableSpan 转为 eval span 事件字典。

    OTel 属性仅支持原始类型（str/int/float/bool），对于 dict/list 类型的属性，
    上游应以 JSON 字符串形式设置，Exporter 会自动反序列化。
    """
    # 计算延迟
    latency_ns = span.end_time - span.start_time
    latency_ms = max(0, latency_ns // 1_000_000)

    # 提取属性（自动 JSON 解析复杂类型）
    attrs: Dict[str, Any] = dict(span.attributes) if span.attributes else {}

    def _parse_attr(key: str) -> Any:
        """从 OTel 属性中提取值，自动尝试 JSON 反序列化字符串值。"""
        val = attrs.get(key)
        if isinstance(val, str) and val.strip().startswith(("{", "[")):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
        return val

    # 提取 tool_status（从 tool_result 推断）
    tool_result = _parse_attr("tool_result")
    tool_status = None
    if isinstance(tool_result, dict):
        tool_status = tool_result.get("status")

    return {
        "type": "span",
        "trace_id": _format_trace_id(span.context.trace_id),
        "span_type": span.name,
        "sequence": sequence,
        "input": _parse_attr("input"),
        "output": _parse_attr("output"),
        "latency_ms": latency_ms,
        "tokens": _parse_attr("llm.usage"),
        "model": attrs.get("llm.model"),
        "tool_name": attrs.get("tool_name"),
        "tool_params": _parse_attr("tool_params"),
        "tool_result": tool_result,
        "tool_status": tool_status,
        "timestamp": time.time(),
    }
