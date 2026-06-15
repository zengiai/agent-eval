"""测试 OTel Span 三层映射策略。

测试 _resolve_span_type 的各种边界条件，不依赖 Redis/DB。
"""

import pytest
from unittest.mock import MagicMock

from agent_eval_sdk.adapters.otel_exporter import (
    _resolve_span_type,
    _VALID_SPAN_TYPES,
    _FALLBACK_RULES,
)


def _make_span(name: str, attrs: dict = None) -> MagicMock:
    """构造一个最小化的 mock ReadableSpan。"""
    span = MagicMock()
    span.name = name
    span.attributes = attrs or {}
    return span


class TestResolveSpanTypeLayer1:
    """第 1 层：显式标注优先"""

    def test_explicit_valid(self):
        """设置合法 eval.span_type → 直接返回"""
        span = _make_span("irrelevant_name", {"eval.span_type": "generation"})
        assert _resolve_span_type(span) == "generation"

    def test_explicit_alias_tool(self):
        """"tool" 是 "tool_call" 的别名"""
        span = _make_span("irrelevant_name", {"eval.span_type": "tool"})
        assert _resolve_span_type(span) == "tool_call"

    def test_explicit_case_insensitive(self):
        """大小写不敏感"""
        span = _make_span("irrelevant_name", {"eval.span_type": "RETRIEVAL"})
        assert _resolve_span_type(span) == "retrieval"

    def test_explicit_whitespace_normalized(self):
        """前后空格被去除"""
        span = _make_span("irrelevant_name", {"eval.span_type": "  intent  "})
        assert _resolve_span_type(span) == "intent"

    def test_explicit_all_five_types(self):
        """所有 5 种合法类型都能通过"""
        for stype in _VALID_SPAN_TYPES:
            span = _make_span("x", {"eval.span_type": stype})
            assert _resolve_span_type(span) == stype


class TestResolveSpanTypeLayer2:
    """第 2 层：模式匹配"""

    def test_langchain_chatopenai(self):
        """LangChain ChatOpenAI → generation"""
        span = _make_span("ChatOpenAI")
        assert _resolve_span_type(span) == "generation"

    def test_langchain_retriever(self):
        """LangChain Retriever.invoke → retrieval"""
        span = _make_span("Retriever.invoke")
        assert _resolve_span_type(span) == "retrieval"

    def test_langchain_agentexecutor(self):
        """LangChain AgentExecutor → outcome"""
        span = _make_span("AgentExecutor")
        assert _resolve_span_type(span) == "outcome"

    def test_llamaindex_llm_predict(self):
        """LlamaIndex llm_predict → generation"""
        span = _make_span("llm_predict")
        assert _resolve_span_type(span) == "generation"

    def test_llamaindex_query_engine(self):
        """LlamaIndex query_engine → outcome"""
        span = _make_span("query_engine")
        assert _resolve_span_type(span) == "outcome"

    def test_openai_function_call(self):
        """OpenAI Agent function_call → tool_call"""
        span = _make_span("function_call")
        assert _resolve_span_type(span) == "tool_call"

    def test_case_insensitive_matching(self):
        """匹配时大小写不敏感"""
        span = _make_span("CHATOPENAI")
        assert _resolve_span_type(span) == "generation"

    def test_longest_match_priority(self):
        """chatopenai 比 chat 更长，应匹配到 chatopenai → generation"""
        span = _make_span("ChatOpenAI")
        # chat 映射到 generation, chatopenai 也映射到 generation
        # 但应该匹配 chatopenai（更长）
        assert _resolve_span_type(span) == "generation"

    def test_query_engine_vs_query(self):
        """query_engine 比 query 更长，query_engine → outcome"""
        span = _make_span("query_engine")
        assert _resolve_span_type(span) == "outcome"

    def test_simple_query_matches_retrieval(self):
        """纯 query 匹配 → retrieval（较短规则）"""
        span = _make_span("query")
        assert _resolve_span_type(span) == "retrieval"


class TestResolveSpanTypeLayer3:
    """第 3 层：兜底"""

    def test_unknown_name_fallback(self):
        """不匹配任何规则 → 返回原始 span.name"""
        span = _make_span("xyz_unknown_abc")  # 确保不含任何内置 pattern 子串
        assert _resolve_span_type(span) == "xyz_unknown_abc"

    def test_empty_name(self):
        """空名称兜底"""
        span = _make_span("")
        assert _resolve_span_type(span) == ""

    def test_known_limitation_substring_match(self):
        """文档化已知限制：包含匹配可能意外命中。
        例如 "stool" 包含 "tool" → tool_call（但 "stool" 不是工具调用）"""
        span = _make_span("stool")  # 不是工具调用，但包含 "tool"
        assert _resolve_span_type(span) == "tool_call"
        # 解决方法：用户通过自定义规则或 eval.span_type 属性覆盖


class TestResolveSpanTypeCustomRules:
    """自定义规则"""

    def test_custom_rule_overrides_builtin(self):
        """自定义规则覆盖内置规则"""
        span = _make_span("my_custom_llm")
        custom = {"my_custom_llm": "generation"}
        # 内置规则不包含 my_custom_llm，但自定义规则包含
        assert _resolve_span_type(span, custom) == "generation"

    def test_custom_rule_overrides_existing(self):
        """自定义规则覆盖同 pattern 的内置规则"""
        span = _make_span("ChatOpenAI")
        custom = {"chatopenai": "intent"}  # 覆盖内置的 generation
        assert _resolve_span_type(span, custom) == "intent"

    def test_custom_rule_case_insensitive_key(self):
        """自定义规则的 key 不区分大小写"""
        span = _make_span("MyFrameworkLLM")
        custom = {"myframeworkllm": "generation"}
        assert _resolve_span_type(span, custom) == "generation"


class TestResolveSpanTypeEdgeCases:
    """边界条件"""

    def test_explicit_non_string_ignored(self):
        """eval.span_type 为非字符串类型 → 忽略，进入模式匹配"""
        span = _make_span("ChatOpenAI", {"eval.span_type": 123})
        assert _resolve_span_type(span) == "generation"  # 通过模式匹配

    def test_explicit_none_skips(self):
        """eval.span_type 为 None → 进入模式匹配"""
        span = _make_span("ChatOpenAI", {"eval.span_type": None})
        assert _resolve_span_type(span) == "generation"

    def test_explicit_invalid_value_falls_through(self):
        """eval.span_type 非法值 → 降级到模式匹配"""
        span = _make_span("ChatOpenAI", {"eval.span_type": "invalid_type"})
        assert _resolve_span_type(span) == "generation"

    def test_no_attributes(self):
        """span 无 attributes → 直接进入模式匹配"""
        span = _make_span("ChatOpenAI")
        assert _resolve_span_type(span) == "generation"

    def test_tool_call_explicit_and_builtin(self):
        """显式 tool_call 和内置匹配同时存在，显式优先"""
        span = _make_span("tool_executor", {"eval.span_type": "generation"})
        assert _resolve_span_type(span) == "generation"
