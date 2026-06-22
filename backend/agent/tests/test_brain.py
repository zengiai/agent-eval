"""AgentBrain 模块单元测试。

覆盖核心组件：FunctionRegistry、LLMIntentParser（Mock LLM）、
CommandExecutor（Mock 依赖）、工具 handler 逻辑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agent.brain.base import CommandContext, FunctionDef, IntentResult
from backend.agent.brain.executor import CommandExecutor
from backend.agent.brain.parser import LLMIntentParser
from backend.agent.brain.registry import FunctionRegistry
from backend.agent.brain.tools import register_all
from backend.agent.gateway.base import IMMessage
from backend.agent.scheduler.base import JobConfig, JobExecution, TriggerType


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def registry() -> FunctionRegistry:
    """提供一个完整注册了 Brain 工具的 FunctionRegistry。"""
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
        text="列一下订单评测用例",
        message_id="msg_001",
    )


class MockScheduler:
    """Scheduler tool 测试用的最小 JobManager 替身。"""

    def __init__(self) -> None:
        self.is_started = True
        self.triggered_jobs: List[str] = []
        self.paused_jobs: List[str] = []
        self.resumed_jobs: List[str] = []
        self.history_limit: int | None = None
        self._job_registry = {
            "sampling.hourly": SimpleNamespace(
                execution_count=2,
                consecutive_failures=0,
                last_error=None,
            ),
            "report.daily": SimpleNamespace(
                execution_count=1,
                consecutive_failures=0,
                last_error=None,
            ),
        }

    def list_jobs(self) -> List[JobConfig]:
        return [
            JobConfig(
                job_id="sampling.hourly",
                name="每小时采样评测",
                description="从生产 Trace 中按小时采样并评测",
                trigger_type=TriggerType.INTERVAL,
                trigger_value="3600",
                enabled=True,
                timeout_seconds=60,
                metadata={"sampling_rate": 0.1},
            ),
            JobConfig(
                job_id="report.daily",
                name="每日评测报告",
                description="生成每日评测报告",
                trigger_type=TriggerType.CRON,
                trigger_value="0 8 * * *",
                enabled=True,
                timeout_seconds=120,
                metadata={"timezone": "Asia/Shanghai"},
            ),
        ]

    async def trigger_now(self, job_id: str) -> str:
        self.triggered_jobs.append(job_id)
        return "exec-new"

    def pause(self, job_id: str) -> None:
        self.paused_jobs.append(job_id)

    def resume(self, job_id: str) -> None:
        self.resumed_jobs.append(job_id)

    async def get_history(self, job_id: str, limit: int = 10) -> List[JobExecution]:
        self.history_limit = limit
        return [
            JobExecution(
                id="exec-001",
                job_id=job_id,
                started_at=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
                completed_at=datetime(2026, 6, 18, 10, 0, 1, tzinfo=timezone.utc),
                status="success",
                result={"processed": 12},
                duration_ms=1000,
            )
        ]


# ===================================================================
# FunctionRegistry 测试
# ===================================================================


class TestFunctionRegistry:
    """FunctionRegistry 基本功能测试。"""

    def test_register_and_count(self, registry: FunctionRegistry):
        """注册后 count 应包含 scheduler 核心工具。"""
        assert registry.count == 13

    def test_get_definitions_format(self, registry: FunctionRegistry):
        """get_definitions 返回 OpenAI 兼容格式。"""
        defs = registry.get_definitions()
        assert isinstance(defs, list)
        assert len(defs) == 13
        # 每条定义应是 {"type": "function", "function": {...}}
        for d in defs:
            assert d["type"] == "function"
            assert "name" in d["function"]
            assert "description" in d["function"]
            assert "parameters" in d["function"]

    def test_get_function_existing(self, registry: FunctionRegistry):
        """get_function 返回已注册的 FunctionDef。"""
        fd = registry.get_function("search_traces")
        assert fd.name == "search_traces"
        assert fd.category == "query"
        assert fd.risk_level == "low"

    def test_get_function_missing(self, registry: FunctionRegistry):
        """get_function 对未注册 function 抛出 KeyError。"""
        with pytest.raises(KeyError):
            registry.get_function("nonexistent_func")

    def test_registered_names(self, registry: FunctionRegistry):
        """registered_names 返回所有已注册名称。"""
        names = registry.registered_names
        assert "list_cases" in names
        assert "get_case_detail" in names
        assert "search_traces" in names
        assert "list_case_sets" in names
        assert "trigger_evaluation" in names
        assert "list_scheduler_jobs" in names
        assert "trigger_scheduler_job" in names
        assert "pause_scheduler_job" in names
        assert "resume_scheduler_job" in names
        assert "get_scheduler_job_detail" in names
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
# Scheduler Tool Handler 测试
# ===================================================================


class TestSchedulerTools:
    """Scheduler 核心工具 handler 测试。"""

    @pytest.fixture
    def scheduler_context(self) -> CommandContext:
        return CommandContext(
            user_id="user_001",
            chat_id="chat_001",
            username="testuser",
            scheduler=MockScheduler(),
        )

    async def test_list_scheduler_jobs(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        result = await registry.execute("list_scheduler_jobs", {}, scheduler_context)
        assert result["total"] == 2
        assert result["scheduler_started"] is True
        assert result["jobs"][0]["job_id"] == "sampling.hourly"
        assert result["jobs"][0]["metadata"]["sampling_rate"] == 0.1

    async def test_trigger_scheduler_job(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "trigger_scheduler_job",
            {"job_id": "sampling.hourly"},
            scheduler_context,
        )
        assert result["status"] == "triggered"
        assert result["execution_id"] == "exec-new"
        assert scheduler.triggered_jobs == ["sampling.hourly"]

    async def test_trigger_scheduler_job_unknown(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        result = await registry.execute(
            "trigger_scheduler_job",
            {"job_id": "missing.job"},
            scheduler_context,
        )
        assert "未知任务" in result["error"]

    async def test_pause_scheduler_job(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "pause_scheduler_job",
            {"job_ids": ["sampling.hourly"]},
            scheduler_context,
        )
        assert result["job_id"] == "sampling.hourly"
        assert result["status"] == "paused"
        assert result["success_count"] == 1
        assert result["failure_count"] == 0
        assert scheduler.paused_jobs == ["sampling.hourly"]

    async def test_pause_scheduler_job_legacy_single_job_id(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "pause_scheduler_job",
            {"job_id": "sampling.hourly"},
            scheduler_context,
        )
        assert result["job_id"] == "sampling.hourly"
        assert result["status"] == "paused"
        assert scheduler.paused_jobs == ["sampling.hourly"]

    async def test_pause_scheduler_jobs_batch(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "pause_scheduler_job",
            {"job_ids": ["sampling.hourly", "report.daily"]},
            scheduler_context,
        )
        assert result["job_ids"] == ["sampling.hourly", "report.daily"]
        assert result["status"] == "paused"
        assert result["success_count"] == 2
        assert result["failure_count"] == 0
        assert [item["status"] for item in result["results"]] == ["paused", "paused"]
        assert scheduler.paused_jobs == ["sampling.hourly", "report.daily"]

    async def test_pause_scheduler_all_jobs(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "pause_scheduler_job",
            {"all_jobs": True},
            scheduler_context,
        )
        assert result["job_ids"] == ["sampling.hourly", "report.daily"]
        assert result["status"] == "paused"
        assert result["success_count"] == 2
        assert result["failure_count"] == 0
        assert scheduler.paused_jobs == ["sampling.hourly", "report.daily"]

    async def test_pause_scheduler_jobs_batch_partial_failure(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "pause_scheduler_job",
            {"job_ids": ["sampling.hourly", "missing.job"]},
            scheduler_context,
        )
        assert result["status"] == "partial"
        assert result["success_count"] == 1
        assert result["failure_count"] == 1
        assert result["results"][1]["job_id"] == "missing.job"
        assert "未知任务" in result["results"][1]["error"]
        assert scheduler.paused_jobs == ["sampling.hourly"]

    async def test_resume_scheduler_job(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "resume_scheduler_job",
            {"job_ids": ["sampling.hourly"]},
            scheduler_context,
        )
        assert result["job_id"] == "sampling.hourly"
        assert result["status"] == "resumed"
        assert result["success_count"] == 1
        assert result["failure_count"] == 0
        assert scheduler.resumed_jobs == ["sampling.hourly"]

    async def test_pause_scheduler_job_unknown(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        result = await registry.execute(
            "pause_scheduler_job",
            {"job_id": "missing.job"},
            scheduler_context,
        )
        assert "未知任务" in result["error"]

    async def test_manage_scheduler_pause_resume_reuses_state_handlers(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler

        pause_result = await registry.execute(
            "manage_scheduler",
            {"action": "pause", "job_ids": ["sampling.hourly", "report.daily"]},
            scheduler_context,
        )
        resume_result = await registry.execute(
            "manage_scheduler",
            {"action": "resume", "job_ids": ["sampling.hourly", "report.daily"]},
            scheduler_context,
        )

        assert pause_result["action"] == "pause"
        assert pause_result["status"] == "paused"
        assert pause_result["success_count"] == 2
        assert resume_result["action"] == "resume"
        assert resume_result["status"] == "resumed"
        assert resume_result["success_count"] == 2
        assert scheduler.paused_jobs == ["sampling.hourly", "report.daily"]
        assert scheduler.resumed_jobs == ["sampling.hourly", "report.daily"]

    async def test_trigger_scheduler_without_scheduler(
        self, registry: FunctionRegistry, sample_context: CommandContext
    ):
        result = await registry.execute(
            "trigger_scheduler_job",
            {"job_id": "sampling.hourly"},
            sample_context,
        )
        assert result["error"] == "调度器未初始化"

    async def test_get_scheduler_job_detail(
        self, registry: FunctionRegistry, scheduler_context: CommandContext
    ):
        scheduler = scheduler_context.scheduler
        result = await registry.execute(
            "get_scheduler_job_detail",
            {"job_id": "sampling.hourly", "history_limit": 100},
            scheduler_context,
        )
        assert result["job"]["job_id"] == "sampling.hourly"
        assert result["job"]["runtime"]["execution_count"] == 2
        assert result["history_limit"] == 50
        assert scheduler.history_limit == 50
        assert result["executions"][0]["status"] == "success"


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
                            "name": "list_cases",
                            "arguments": '{"category": "order", "limit": 5}',
                        }
                    }]
                }
            }]
        }

        intent = parser._parse_tool_calls(raw_response)
        assert intent.function_name == "list_cases"
        assert intent.arguments == {"category": "order", "limit": 5}
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
            {"role": "user", "content": "列一下订单用例"},
            {"role": "assistant", "content": "已列出订单相关评测用例"},
        ]

        messages = parser._build_messages("那 hard 难度的呢", history)
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "那 hard 难度的呢"
        # 历史消息应在中间
        assert any(m["content"] == "列一下订单用例" for m in messages)
        assert any(m["content"] == "那 hard 难度的呢" for m in messages)

    @pytest.mark.asyncio
    async def test_complete_with_tool_result_uses_tool_messages(
        self, registry: FunctionRegistry
    ):
        """工具结果应作为 tool message 回填给最终 LLM 回复请求。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        async def fake_call(messages, tools):
            assert tools is None
            assert messages[0]["role"] == "system"
            assert messages[-3]["role"] == "user"
            assert messages[-2]["role"] == "assistant"
            assert messages[-2]["tool_calls"][0]["function"]["name"] == "list_cases"
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "call_list_cases"
            assert "...<truncated>" in messages[-1]["content"]
            return {"choices": [{"message": {"content": "找到 1 条订单用例。"}}]}

        parser._call_llm_with_retry = fake_call

        text = await parser.complete_with_tool_result(
            user_text="列一下订单评测用例",
            intent=IntentResult(function_name="list_cases", arguments={"category": "order"}),
            tool_result={"items": [{"id": "case-001", "query": "x" * 100}]},
            max_result_chars=80,
        )

        assert text == "找到 1 条订单用例。"


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

    @pytest.mark.asyncio
    async def test_handle_tool_result_with_final_llm_reply(
        self, registry: FunctionRegistry, sample_message: IMMessage
    ):
        """工具调用成功后应返回最终 LLM 回复。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        async def mock_parse(text, history):
            return IntentResult(
                function_name="list_cases",
                arguments={"category": "order", "limit": 1},
            )

        async def mock_complete_with_tool_result(**kwargs):
            assert kwargs["user_text"] == sample_message.text
            assert kwargs["intent"].function_name == "list_cases"
            assert kwargs["tool_result"]["total"] == 1
            return "找到 <1> 条订单相关评测用例，可以继续查看详情。"

        async def fake_list_cases(args, context):
            return {
                "total": 1,
                "items": [
                    {
                        "id": "case-001",
                        "query": "用户询问最近订单状态",
                        "source": "manual",
                        "category": "order",
                        "difficulty": "medium",
                        "health_status": "healthy",
                        "last_avg_score": 91.5,
                    }
                ],
            }

        parser.parse = mock_parse
        parser.complete_with_tool_result = mock_complete_with_tool_result
        registry.register(
            FunctionDef(
                name="list_cases",
                description="fake list cases",
                parameters={"type": "object", "properties": {}},
            ),
            fake_list_cases,
        )
        executor = CommandExecutor(parser=parser, registry=registry)

        reply = await executor.handle(sample_message)

        assert reply is not None
        assert reply == "找到 &lt;1&gt; 条订单相关评测用例，可以继续查看详情。"
        assert "工具结果" not in reply
        assert "case-001" not in reply

    @pytest.mark.asyncio
    async def test_handle_tool_result_final_reply_failure_falls_back(
        self, registry: FunctionRegistry, sample_message: IMMessage
    ):
        """最终 LLM 回复失败时应降级为原格式化结果。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")

        async def mock_parse(text, history):
            return IntentResult(
                function_name="list_cases",
                arguments={"category": "order", "limit": 1},
            )

        async def mock_complete_with_tool_result(**kwargs):
            raise RuntimeError("final completion timeout")

        async def fake_list_cases(args, context):
            return {
                "total": 1,
                "items": [
                    {
                        "id": "case-001",
                        "query": "用户询问最近订单状态",
                        "source": "manual",
                        "category": "order",
                        "difficulty": "medium",
                        "health_status": "healthy",
                        "last_avg_score": 91.5,
                    }
                ],
            }

        parser.parse = mock_parse
        parser.complete_with_tool_result = mock_complete_with_tool_result
        registry.register(
            FunctionDef(
                name="list_cases",
                description="fake list cases",
                parameters={"type": "object", "properties": {}},
            ),
            fake_list_cases,
        )
        executor = CommandExecutor(parser=parser, registry=registry)

        reply = await executor.handle(sample_message)

        assert reply is not None
        assert "🧾 <b>评测用例列表</b>" in reply
        assert "case-001" in reply

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

    @pytest.mark.asyncio
    async def test_handle_pause_scheduler_job(
        self, registry: FunctionRegistry, sample_message: IMMessage
    ):
        """自然语言经 Brain 解析为 pause_scheduler_job 后应成功暂停。"""
        parser = LLMIntentParser(registry=registry, api_key="test-key")
        scheduler = MockScheduler()

        async def mock_parse(text, history):
            return IntentResult(
                function_name="pause_scheduler_job",
                arguments={"job_ids": ["sampling.hourly"]},
                risk_level="medium",
            )

        parser.parse = mock_parse
        executor = CommandExecutor(
            parser=parser,
            registry=registry,
            context_factory=lambda msg: CommandContext(
                user_id=msg.user_id,
                chat_id=msg.chat_id,
                username=msg.username,
                scheduler=scheduler,
            ),
        )

        reply = await executor.handle(sample_message)
        assert reply is not None
        assert "Scheduler Job 已暂停" in reply
        assert "sampling.hourly" in reply
        assert scheduler.paused_jobs == ["sampling.hourly"]


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
            function_name="search_traces",
            arguments={"query_keyword": "timeout"},
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

    def test_fmt_list_cases(self, executor: CommandExecutor):
        """用例列表格式化。"""
        result = {
            "total": 2,
            "items": [
                {
                    "id": "case-001",
                    "query": "用户询问最近订单状态",
                    "source": "manual",
                    "category": "order",
                    "difficulty": "medium",
                    "review_status": "approved",
                    "health_status": "healthy",
                    "last_avg_score": 91.5,
                },
                {
                    "id": "case-002",
                    "query": "复杂售后退款链路",
                    "source": "trace",
                    "category": "refund",
                    "difficulty": "hard",
                    "review_status": "pending",
                    "health_status": "weak",
                    "last_avg_score": 63.0,
                },
            ],
        }
        text = executor._fmt_list_cases(result)
        assert "评测用例列表" in text
        assert "case-001" in text
        assert "91.5" in text
        assert "复杂售后退款链路" in text

    def test_fmt_case_detail(self, executor: CommandExecutor):
        """用例详情格式化。"""
        result = {
            "case": {
                "id": "case-001",
                "query": "用户询问最近订单状态",
                "source": "manual",
                "category": "order",
                "difficulty": "medium",
                "review_status": "approved",
                "health_status": "healthy",
                "gold_answer": "返回最近订单的物流状态",
                "expected_intent": {"name": "query_order"},
            },
            "score_summary": {"last_avg_score": 88.0, "run_count": 2},
            "scores": [
                {
                    "created_at": "2026-06-18T10:00:00",
                    "status": "completed",
                    "overall_score": 88.0,
                    "scores": [
                        {"layer": "intent", "score": 90.0},
                        {"layer": "generation", "score": 86.0},
                    ],
                },
            ],
        }
        text = executor._fmt_case_detail(result)
        assert "评测用例详情" in text
        assert "case-001" in text
        assert "88.0" in text
        assert "Intent" in text
        assert "generation" in text

    def test_fmt_search_traces_accepts_api_items(self, executor: CommandExecutor):
        """Trace 列表 API 返回 items 时也应正常渲染。"""
        result = {
            "total": 12,
            "items": [
                {
                    "id": "trace-001",
                    "agent_version": "v2.3.1",
                    "overall_score": 91.5,
                    "status": "success",
                    "created_at": "2026-06-22T10:00:00",
                },
            ],
        }
        text = executor._fmt_search_traces(result)
        assert "共 12 条，显示 1 条" in text
        assert "trace-001" in text
        assert "v2.3.1" in text
        assert "91.5" in text

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

    def test_fmt_scheduler_jobs(self, executor: CommandExecutor):
        """Scheduler Job 列表格式化。"""
        result = {
            "total": 1,
            "scheduler_started": True,
            "jobs": [
                {
                    "job_id": "sampling.hourly",
                    "name": "每小时<采样>&评测",
                    "trigger_type": "interval",
                    "trigger_value": "3600",
                    "enabled": True,
                    "timeout_seconds": 60,
                }
            ],
        }
        text = executor._fmt_scheduler_jobs(result)
        assert "<b>Scheduler Jobs</b>" in text
        assert "<pre>" in text
        assert "sampling.hourly" in text
        assert "每小时&lt;采样&gt;&amp;评测" in text
        assert "interval=3600" in text
        assert "**" not in text
        assert "| Job ID |" not in text

    def test_fmt_scheduler_trigger(self, executor: CommandExecutor):
        """Scheduler Job 触发格式化。"""
        text = executor._fmt_scheduler_trigger({
            "job_id": "sampling.hourly",
            "execution_id": "exec-new",
            "status": "triggered",
        })
        assert "<b>Scheduler Job 已触发</b>" in text
        assert "sampling.hourly" in text
        assert "exec-new" in text
        assert "**" not in text

    def test_fmt_scheduler_state_change(self, executor: CommandExecutor):
        """Scheduler Job 状态修改格式化。"""
        text = executor._fmt_scheduler_state_change(
            {"job_id": "sampling.hourly", "status": "paused"},
            "暂停",
            "paused",
        )
        assert "<b>Scheduler Job 已暂停</b>" in text
        assert "sampling.hourly" in text
        assert "paused" in text
        assert "**" not in text

    def test_fmt_scheduler_state_change_error(self, executor: CommandExecutor):
        """Scheduler Job 状态修改错误格式化。"""
        text = executor._fmt_scheduler_state_change(
            {"error": "未知任务: missing.job"},
            "暂停",
            "paused",
        )
        assert "暂停失败" in text
        assert "未知任务" in text

    def test_fmt_scheduler_state_change_batch(self, executor: CommandExecutor):
        """Scheduler Job 批量状态修改格式化。"""
        text = executor._fmt_scheduler_state_change(
            {
                "job_ids": ["sampling.hourly", "missing.job"],
                "status": "partial",
                "success_count": 1,
                "failure_count": 1,
                "results": [
                    {"job_id": "sampling.hourly", "status": "paused"},
                    {"job_id": "missing.job", "status": "failed", "error": "未知任务: missing.job"},
                ],
            },
            "暂停",
            "paused",
        )
        assert "批量暂停部分成功" in text
        assert "sampling.hourly" in text
        assert "missing.job" in text
        assert "未知任务" in text
        assert "<pre>" in text

    def test_fmt_scheduler_job_detail(self, executor: CommandExecutor):
        """Scheduler Job 详情格式化。"""
        result = {
            "scheduler_started": True,
            "job": {
                "job_id": "sampling.hourly",
                "name": "每小时采样评测",
                "description": "从生产 Trace 中按小时采样并评测",
                "trigger_type": "interval",
                "trigger_value": "3600",
                "enabled": True,
                "timeout_seconds": 60,
                "metadata": {"sampling_rate": 0.1},
                "runtime": {
                    "execution_count": 2,
                    "consecutive_failures": 0,
                    "last_error": None,
                },
            },
            "executions": [
                {
                    "id": "exec-001",
                    "status": "success",
                    "started_at": "2026-06-18T10:00:00+00:00",
                    "duration_ms": 1000,
                    "result": {"processed": 12},
                }
            ],
        }
        text = executor._fmt_scheduler_job_detail(result)
        assert "<b>Scheduler Job 详情</b>" in text
        assert "sampling.hourly" in text
        assert "sampling_rate" in text
        assert "success" in text
        assert "<pre>" in text
        assert "**" not in text
        assert "| 开始时间 |" not in text
