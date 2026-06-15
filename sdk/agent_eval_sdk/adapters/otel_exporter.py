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

Span 类型映射（三层策略）：
  1. 优先：span.attributes["eval.span_type"] 显式标注
  2. 次选：span.name.lower() 对内置映射表做包含匹配（最长匹配优先）
  3. 兜底：返回 span.name 原值
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent_eval_sdk.reporter import TraceReporter

logger = logging.getLogger(__name__)

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


# ── span_type 枚举与映射表 ───────────────────────────────────────────────

_VALID_SPAN_TYPES = {"intent", "retrieval", "tool_call", "generation", "outcome"}

# 硬编码 fallback 规则（不依赖 YAML 时使用）
_FALLBACK_RULES: Dict[str, str] = {
    # LangChain / LangGraph
    "chatopenai": "generation",
    "chat": "generation",
    "llm": "generation",
    "openai.chat": "generation",
    "retriever": "retrieval",
    "retriev": "retrieval",
    "similarity_search": "retrieval",
    "vectorstore": "retrieval",
    "tool": "tool_call",
    "agentexecutor": "outcome",
    "agent": "outcome",
    "chain.invoke": "generation",
    "chain": "generation",
    # LlamaIndex
    "llm_predict": "generation",
    "complete": "generation",
    "query_engine": "outcome",
    "query": "retrieval",
    "retrieve": "retrieval",
    "node_parser": "retrieval",
    # OpenAI Agents SDK
    "openai_agent": "outcome",
    "agent_runner": "outcome",
    "function_call": "tool_call",
    "tool_call": "tool_call",
    "model_response": "generation",
}

_DEFAULT_RULES_PATH = Path(__file__).parent / "span_type_mapping.yaml"
_DEFAULT_RULES: Optional[Dict[str, str]] = None



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

    子 Span 到达时先缓存，根 Span（parent is None）到达时统一输出：
    trace_start → 所有 span × N → trace_finish
    兼容 SimpleSpanProcessor（逐 Span 导出）和 BatchSpanProcessor（批量导出）。
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        agent_version: str = "unknown",
        redis_key_prefix: str = "eval:events:",
        source: str = "production",
        run_id: Optional[str] = None,
        span_type_rules: Optional[Dict[str, str]] = None,
    ):
        """
        Args:
            redis_url: Redis 连接地址
            agent_version: Agent 版本号
            redis_key_prefix: Redis Key 前缀
            source: 上报来源（'eval' | 'production'）
            run_id: 评测运行 ID（评测场景传入）
            span_type_rules: 自定义 span_type 映射表 {pattern: span_type}，
                             与内置映射表合并，自定义规则优先级更高
        """
        if _otel_import_error is not None:
            raise ImportError(
                "使用 EvalSpanExporter 需要安装 OpenTelemetry SDK：\n"
                "    pip install agent-eval-sdk[otel]"
            ) from _otel_import_error

        self._agent_version = agent_version
        self._source = source
        self._run_id = run_id
        self._span_type_rules = span_type_rules
        self._reporter = TraceReporter(
            agent_version=agent_version,
            redis_url=redis_url,
            redis_key_prefix=redis_key_prefix,
        )
        self._redis = self._reporter._redis
        self._span_key = self._reporter._span_key
        # 缓存未完成的 trace：{trace_id: {"root": None, "spans": []}}
        self._pending: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"root": None, "spans": []}
        )

    def export(self, spans: Sequence[ReadableSpan]) -> "SpanExportResult":
        """导出一批已结束的 OTel Span 到 Redis。

        子 Span（parent is not None）→ 缓存到 _pending
        根 Span（parent is None）→ 将缓存的所有子 Span + 根 Span 一次性输出：
          trace_start → span × N → trace_finish
        """
        for span in spans:
            tid = _format_trace_id(span.context.trace_id)
            is_root = span.parent is None

            if is_root:
                # 根 Span 到达 → 写入完整 trace
                group = self._pending.pop(tid, {"root": None, "spans": []})
                all_spans: List[ReadableSpan] = list(group["spans"])
                all_spans.append(span)
                all_spans.sort(key=lambda s: s.start_time)

                attrs = _safe_attrs(span)

                def _parse(key: str, default: Any = None) -> Any:
                    val = attrs.get(key, default)
                    if isinstance(val, str) and val.strip().startswith(("{", "[")):
                        try:
                            return json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    return val

                # trace_start
                self._push({
                    "type": "trace_start",
                    "trace_id": tid,
                    "agent_version": attrs.get("agent_version", self._agent_version),
                    "query": attrs.get("query", span.name),
                    "context": _parse("context", {}),
                    "source": attrs.get("source", self._source),
                    "run_id": attrs.get("run_id", self._run_id),
                    "source_ref": attrs.get("source_ref"),
                    "session_id": attrs.get("session_id"),
                    "timestamp": time.time(),
                })

                # span × N
                for seq, s in enumerate(all_spans, start=1):
                    self._push(_span_to_event(s, seq, self._span_type_rules))

                # trace_finish
                self._push({
                    "type": "trace_finish",
                    "trace_id": tid,
                    "final_response": attrs.get("final_response"),
                    "status": _map_status(span),
                    "timestamp": time.time(),
                })
            else:
                # 子 Span → 缓存，等根 Span 到达后统一输出
                self._pending[tid]["spans"].append(span)

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


def _span_to_event(span: ReadableSpan, sequence: int,
                   custom_rules: Optional[Dict[str, str]] = None) -> dict:
    """将单个 OTel ReadableSpan 转为 eval span 事件字典。

    span_type 通过三层策略推导：
      1. span.attributes["eval.span_type"] 显式标注
      2. span.name.lower() 对映射表做包含匹配
      3. 兜底返回 span.name

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
        "span_type": _resolve_span_type(span, custom_rules),
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


# ── Span 类型映射 ────────────────────────────────────────────────────────

def _load_default_rules() -> Dict[str, str]:
    """加载内置映射表（惰性初始化，避免导入时 I/O）。

    优先从 YAML 文件加载，失败时使用硬编码 fallback。
    """
    global _DEFAULT_RULES
    if _DEFAULT_RULES is not None:
        return _DEFAULT_RULES

    try:
        import yaml
        with open(_DEFAULT_RULES_PATH, "r", encoding="utf-8") as f:
            _DEFAULT_RULES = yaml.safe_load(f) or {}
        logger.debug("已加载 span_type 映射文件: %s", _DEFAULT_RULES_PATH)
    except Exception:
        _DEFAULT_RULES = dict(_FALLBACK_RULES)
        logger.debug("映射文件不可用，使用硬编码 fallback 规则 (%d 条)", len(_DEFAULT_RULES))
    return _DEFAULT_RULES


def _build_rules(custom_rules: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构建最终映射表：内置规则 + 自定义规则（自定义覆盖内置）。

    Args:
        custom_rules: 用户自定义映射 {pattern: span_type}

    Returns:
        合并后的映射表，所有 key 已转为小写
    """
    rules = dict(_load_default_rules())
    if custom_rules:
        # 自定义规则 key 也转为小写以保证匹配一致性
        rules.update({k.lower(): v for k, v in custom_rules.items()})
    return rules


def _resolve_span_type(
    span: "ReadableSpan",
    custom_rules: Optional[Dict[str, str]] = None,
) -> str:
    """三层策略推导 span_type。

    优先级：
      1. span.attributes["eval.span_type"] 显式标注（存在且合法）
      2. span.name.lower() 对映射表做包含匹配（最长匹配优先）
      3. 兜底返回 span.name

    Args:
        span: OTel ReadableSpan
        custom_rules: 用户自定义映射规则，与内置规则合并

    Returns:
        推导出的 span_type 字符串
    """
    attrs = dict(span.attributes) if span.attributes else {}

    # ── 第 1 层：显式标注优先 ──────────────────────────────────────────
    explicit = attrs.get("eval.span_type")
    if explicit is not None and isinstance(explicit, str):
        explicit_lower = explicit.strip().lower()
        # "tool" 是 "tool_call" 的别名
        if explicit_lower == "tool":
            explicit_lower = "tool_call"
        if explicit_lower in _VALID_SPAN_TYPES:
            return explicit_lower
        logger.warning(
            "非法的 eval.span_type 值 '%s'（合法值: %s），降级到模式匹配",
            explicit, sorted(_VALID_SPAN_TYPES),
        )

    # ── 第 2 层：模式匹配 ──────────────────────────────────────────────
    name_lower = span.name.lower().strip()
    rules = _build_rules(custom_rules)

    # 收集所有匹配的规则，取 pattern 最长者
    best_match: Optional[str] = None
    best_len = 0
    for pattern, target in rules.items():
        if pattern in name_lower:
            if len(pattern) > best_len:
                best_match = target
                best_len = len(pattern)

    if best_match is not None:
        return best_match

    # ── 第 3 层：兜底 ──────────────────────────────────────────────────
    logger.debug("span_type 无匹配规则，兜底返回 span.name: '%s'", span.name)
    return span.name
