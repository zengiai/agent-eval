"""AgentBrain 模块单元测试。

覆盖核心组件：FunctionRegistry、LLMIntentParser（Mock LLM）、
CommandExecutor（Mock 依赖）、工具 handler 逻辑。
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agent.brain.base import CommandContext, FunctionDef, IntentResult
from backend.agent.brain.executor import CommandExecutor
from backend.agent.brain.parser import LLMIntentParser
from backend.agent.brain.registry import FunctionRegistry
from backend.agent.brain.tools import register_all
from backend.agent.gateway.base import IMMessage


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def registry() -> FunctionRegistry:
    """提供一个完整注册了 12 个工具的 FunctionRegistry。"""
    r = FunctionRegistry()
    register_all(r)
    return r


@pytest.fixture
def sample_context() -> CommandContext:
    """提供一个最小 CommandContext。"""
    return CommandContext(
        user_id="user_001",
        chat_id="chat_001",
        username="testuser",
        llm_config={
            "model": "test-model",
            "api_key": "test-key",
            "base_url": "https://test.api/v1",
        },
    )


@pytest.fixture
def sample_message() -> IMMessage:
    """提供一条标准测试消息。"""
    return IMMessage(
        platform="mock",
        chat_id="chat_001",
        user_id="user_001",
        username="testuser",
        text="查一下 v2.3.1 的评分趋势",
        message_id="msg_001",
    )


# ===================================================================
# FunctionRegistry 测试
# ===================================================================


class TestFunctionRegistry:
    """FunctionRegistry 基本功能测试。"""

    def test_register_and_count(self, registry: FunctionRegistry):
        """注册后 count 应为 12。"""
        assert registry.count == 12

    def test_get_definitions_format(self, registry: FunctionRegistry):
        """get_definitions 返回 OpenAI 兼容格式。"""
        defs = registry.get_definitions()
        assert isinstance(defs, list)
        assert len(defs) == 12
        # 每条定义应是 {"type": "function", "function": {...}}
        for d in defs:
            assert d["type"] == "function"
            assert "name" in d["function"]
            assert "description" in d["function"]
            assert "parameters" in d["function"]

    def test_get_function_existing(self, registry: FunctionRegistry):
        """get_function 返回已注册的 FunctionDef。"""
        fd = registry.get_function("query_score_trend")
        assert fd.name == "query_score_trend"
        assert fd.category == "query"
        assert fd.risk_level == "low"

    def test_get_function_missing(self, registry: FunctionRegistry):
        """get_function 对未注册 function 抛出 KeyError。"""
        with pytest.raises(KeyError):
            registry.get_function("nonexistent_func")

    def test_registered_names(self, registry: FunctionRegistry):
        """registered_names 返回所有已注册名称。"""
        names = registry.registered_names
        assert "get_latest_eval_status" in names
        assert "trigger_evaluation" in names
        assert "compare_versions" in names
        assert "fallback_chat" not in names  # fallback 不在 registry 中

    async def test_execute_unknown_function(self, registry: FunctionRegistry, sample_context: CommandContext):
        """执行未注册 function 抛出 ValueError。"""
        with pytest.raises(ValueError, match="Unknown function"):
            await registry.execute("nonexistent", {}, sample_context)

    def test_register_duplicate(self):
        """重复注册同一 function 应覆盖。"""
        r = FunctionRegistry()
        fd = FunctionDef(name="test", description="desc", parameters={"type": "object", "properties": {}})

        async def handler1(args, ctx):
            return "v1"

        async def handler2(args, ctx):
            return "v2"

        r.register(fd, handler1)
        r.register(fd, handler2)
        assert r.count == 1  # 仍是 1 个


# ===================================================================
# LLMIntentParser 测试
# ===================================================================


class TestLLMIntentParser:
    """LLMIntentParser 意图解析测试。"""

    def test_parser_init(self, registry: FunctionRegistry):
        """解析器初始化成功。"""
        parser = LLMIntentParser(
            registry=registry,
            model="test-model",
            api_key="test-key",
        )
        assert parser is not None

    def test_parse_tool_calls_with_function(self, registry: FunctionRegistry):
        """解析带 tool_calls 的 LLM 响应。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        raw_response = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "query_score_trend",
                            "arguments": '{"agent_version": "v2.3.1", "last_n": 5}',
                        }
                    }]
                }
            }]
        }

        intent = parser._parse_tool_calls(raw_response)
        assert intent.function_name == "query_score_trend"
        assert intent.arguments == {"agent_version": "v2.3.1", "last_n": 5}
        assert not intent.is_fallback
        assert intent.risk_level == "low"

    def test_parse_tool_calls_fallback(self, registry: FunctionRegistry):
        """LLM 调用 fallback_chat 时的解析。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        raw_response = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "fallback_chat",
                            "arguments": '{"reply": "我不确定你的意思"}',
                        }
                    }]
                }
            }]
        }

        intent = parser._parse_tool_calls(raw_response)
        assert intent.is_fallback
        assert "我不确定你的意思" in intent.arguments.get("reply", "")

    def test_parse_no_tool_calls_no_content(self, registry: FunctionRegistry):
        """LLM 未返回 tool_calls 且有 text content。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        raw_response = {
            "choices": [{
                "message": {
                    "content": "你好，今天我能帮你什么？"
                }
            }]
        }

        intent = parser._parse_tool_calls(raw_response)
        assert intent.is_fallback
        assert "你好" in intent.arguments.get("reply", "")

    def test_parse_empty_choices(self, registry: FunctionRegistry):
        """LLM 返回空 choices。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        raw_response = {"choices": []}

        intent = parser._parse_tool_calls(raw_response)
        assert intent.is_fallback

    def test_parse_high_risk_function(self, registry: FunctionRegistry):
        """高风险 function 正确传递 risk_level。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        raw_response = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "function": {
                            "name": "trigger_evaluation",
                            "arguments": '{"agent_version": "v2.3.1"}',
                        }
                    }]
                }
            }]
        }

        intent = parser._parse_tool_calls(raw_response)
        assert intent.function_name == "trigger_evaluation"
        assert intent.risk_level == "high"
        assert intent.require_confirmation is True

    @pytest.mark.asyncio
    async def test_parse_with_history(self, registry: FunctionRegistry):
        """带对话历史的 parse 请求构造。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        history = [
            {"role": "user", "content": "查一下 v2.3.1"},
            {"role": "assistant", "content": "已查到 v2.3.1 的评分趋势"},
        ]

        messages = parser._build_messages("那 v2.3.0 呢", history)
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "那 v2.3.0 呢"
        # 历史消息应在中间
        assert any(m["content"] == "查一下 v2.3.1" for m in messages)
        assert any(m["content"] == "那 v2.3.0 呢" for m in messages)


# ===================================================================
# CommandExecutor 测试
# ===================================================================


class TestCommandExecutor:
    """CommandExecutor 编排逻辑测试。"""

    def test_executor_init(self, registry: FunctionRegistry):
        """编排器初始化成功。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")
        executor = CommandExecutor(parser=parser, registry=registry)
        assert executor.active_conversations == 0

    @pytest.mark.asyncio
    async def test_handle_fallback(self, registry: FunctionRegistry, sample_message: IMMessage):
        """fallback_chat 意图应返回格式化回复。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        # Mock parse 返回 fallback
        async def mock_parse(text, history):
            return IntentResult(
                function_name="fallback_chat",
                arguments={"reply": "抱歉，我不太懂你的意思"},
            )

        parser.parse = mock_parse
        executor = CommandExecutor(parser=parser, registry=registry)

        reply = await executor.handle(sample_message)
        assert reply is not None
        assert "抱歉" in reply
        assert "/help" in reply

    @pytest.mark.asyncio
    async def test_handle_llm_unavailable(self, registry: FunctionRegistry, sample_message: IMMessage):
        """LLM 不可用时返回降级提示。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        async def mock_parse_raise(text, history):
            raise RuntimeError("LLM timeout")

        parser.parse = mock_parse_raise
        executor = CommandExecutor(parser=parser, registry=registry)

        reply = await executor.handle(sample_message)
        assert reply is not None
        assert "LLM 服务暂时不可用" in reply
        assert "/help" in reply

    def test_conversation_history_management(self, registry: FunctionRegistry):
        """对话历史管理功能。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")
        executor = CommandExecutor(parser=parser, registry=registry, max_history=3)

        # 手动模拟对话历史
        executor._conversations["chat_001"] = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "reply1"},
        ]

        assert executor.active_conversations == 1
        history = executor.get_history("chat_001")
        assert len(history) == 2

        executor.clear_history("chat_001")
        assert executor.active_conversations == 0
        assert executor.get_history("chat_001") == []


# ===================================================================
# IntentResult 测试
# ===================================================================


class TestIntentResult:
    """IntentResult 数据类测试。"""

    def test_is_fallback_true(self):
        """function_name 为 fallback_chat 时 is_fallback=True。"""
        intent = IntentResult(
            function_name="fallback_chat",
            arguments={"reply": "test"},
        )
        assert intent.is_fallback is True

    def test_is_fallback_false(self):
        """正常 function 时 is_fallback=False。"""
        intent = IntentResult(
            function_name="query_score_trend",
            arguments={"version": "v2.3.1"},
        )
        assert intent.is_fallback is False

    def test_default_values(self):
        """默认值合理性。"""
        intent = IntentResult(
            function_name="test_func",
            arguments={},
        )
        assert intent.reasoning == ""
        assert intent.confidence == 1.0
        assert intent.risk_level == "low"
        assert intent.require_confirmation is False


# ===================================================================
# CommandContext 测试
# ===================================================================


class TestCommandContext:
    """CommandContext 数据类测试。"""

    def test_minimal_context(self):
        """最小上下文创建。"""
        ctx = CommandContext(
            user_id="u1",
            chat_id="c1",
            username="test",
        )
        assert ctx.user_id == "u1"
        assert ctx.chat_id == "c1"
        assert ctx.db_session_factory is None
        assert ctx.llm_config == {}
        assert ctx.config == {}

    def test_full_context(self):
        """完整上下文创建。"""
        mock_db = MagicMock()
        mock_eval = MagicMock()
        mock_sched = MagicMock()
        mock_gw = MagicMock()

        ctx = CommandContext(
            user_id="u1",
            chat_id="c1",
            username="test",
            db_session_factory=mock_db,
            eval_service=mock_eval,
            scheduler=mock_sched,
            gateway=mock_gw,
            llm_config={"model": "gpt-4"},
            config={"debug": True},
        )
        assert ctx.db_session_factory is mock_db
        assert ctx.eval_service is mock_eval
        assert ctx.scheduler is mock_sched
        assert ctx.gateway is mock_gw
        assert ctx.llm_config["model"] == "gpt-4"
        assert ctx.config["debug"] is True


# ===================================================================
# 回复格式化测试
# ===================================================================


class TestReplyFormatting:
    """CommandExecutor 回复格式化测试。"""

    @pytest.fixture
    def executor(self, registry: FunctionRegistry) -> CommandExecutor:
        parser = LLMIntentParser(registry=registry, api_key="test-key")
        return CommandExecutor(parser=parser, registry=registry)

    def test_fmt_eval_status(self, executor: CommandExecutor):
        """评测状态格式化。"""
        result = {
            "total_tasks": 42,
            "status_counts": {"pending": 10, "running": 5, "completed": 27},
            "avg_overall_score": 85.5,
            "active_versions": ["v2.3.1", "v2.3.0"],
            "hours_back": 24,
        }
        text = executor._fmt_eval_status(result)
        assert "42" in text
        assert "85.5" in text
        assert "v2.3.1" in text

    def test_fmt_score_trend(self, executor: CommandExecutor):
        """评分趋势格式化。"""
        result = {
            "version": "v2.3.1",
            "layer": "overall",
            "trend": [
                {"run_time": "2026-06-17T10:00:00", "score": 85.0},
                {"run_time": "2026-06-17T12:00:00", "score": 87.5},
            ],
            "delta": 2.5,
        }
        text = executor._fmt_score_trend(result)
        assert "v2.3.1" in text
        assert "85.0" in text
        assert "87.5" in text
        assert "2.5" in text

    def test_fmt_trace_detail(self, executor: CommandExecutor):
        """Trace 详情格式化。"""
        result = {
            "trace": {
                "id": "abc12345-1234-1234-1234-123456789abc",
                "query": "今天天气怎么样",
                "status": "success",
                "overall_score": 88.0,
                "agent_version": "v2.3.1",
                "total_latency_ms": 1500,
            },
            "spans": [
                {"span_type": "intent", "sequence": 1, "score": 90.0, "tool_name": None, "tool_status": None, "latency_ms": 200},
                {"span_type": "generation", "sequence": 2, "score": 85.0, "tool_name": None, "tool_status": None, "latency_ms": 800},
            ],
            "eval_scores": [
                {"id": "sc001", "score": 88.0, "metrics": {}, "method": "llm"},
            ],
        }
        text = executor._fmt_trace_detail(result)
        assert "abc12345" in text
        assert "88.0" in text
        assert "v2.3.1" in text
        assert "intent" in text

    def test_fmt_weakest_cases(self, executor: CommandExecutor):
        """弱点评分用例格式化。"""
        result = {
            "cases": [
                {"query": "复杂多步查询", "category": "multi-step", "difficulty": "hard", "avg_score": 45.0, "run_count": 10},
                {"query": "简单问候", "category": "greeting", "difficulty": "easy", "avg_score": 50.0, "run_count": 5},
            ],
            "top_n": 5,
            "layer": "overall",
        }
        text = executor._fmt_weakest_cases(result)
        assert "45.0" in text
        assert "50.0" in text
        assert "复杂多步查询" in text

    def test_fmt_compare_versions(self, executor: CommandExecutor):
        """版本对比格式化。"""
        result = {
            "version_a": "v2.3.0",
            "version_b": "v2.3.1",
            "comparison": [{"metric": "overall", "version_a": 82.0, "version_b": 87.5, "delta": 5.5}],
            "overall_delta": 5.5,
            "significant": True,
        }
        text = executor._fmt_compare(result)
        assert "v2.3.0" in text
        assert "v2.3.1" in text
        assert "差异显著" in text

    def test_fmt_fallback_reply(self, executor: CommandExecutor):
        """兜底回复格式化。"""
        intent = IntentResult(
            function_name="fallback_chat",
            arguments={"reply": "我不确定你想做什么"},
        )
        text = executor._format_fallback(intent)
        assert "我不确定你想做什么" in text
        assert "/help" in text

    def test_fmt_trigger_eval(self, executor: CommandExecutor):
        """评测触发格式化。"""
        result = {
            "task_id": "task-uuid-123",
            "agent_version": "v2.3.1",
            "case_set_name": "默认",
            "total_cases": 10,
            "layers": ["intent", "generation", "outcome"],
        }
        text = executor._fmt_trigger_eval(result)
        assert "task-uuid-123" in text
        assert "v2.3.1" in text
        assert "10" in text
