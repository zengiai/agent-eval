"""Gateway ↔ Brain ↔ Scheduler 三模块端到端集成测试。

验证核心连通路径：
1. IMMessage → MessageRouter → CommandExecutor → FunctionRegistry → 格式化回复
2. /status /jobs 命令 → 调度器状态查询
3. /eval 确认流程 → Brain 执行评测
4. tool handler 通过 CommandContext 访问 scheduler/gateway
5. 异常降级与边界场景
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from backend.agent.brain.base import CommandContext, FunctionDef, IntentResult
from backend.agent.brain.executor import CommandExecutor
from backend.agent.brain.parser import LLMIntentParser
from backend.agent.brain.registry import FunctionRegistry
from backend.agent.brain.tools import register_all
from backend.agent.gateway.base import IMMessage
from backend.agent.gateway.router import MessageRouter, PendingAction
from backend.agent.scheduler.base import BaseJob, JobConfig, TriggerType
from backend.agent.scheduler.manager import JobManager

from .conftest import MockGateway


# ======================================================================
# Mock 辅助 — DB Session（无真实数据库依赖）
# ======================================================================

class _MockResult:
    """Mock SQLAlchemy Result，兼容 fetchall / scalars / scalar / all。"""
    def fetchall(self):
        return []
    def scalars(self):
        return _MockScalars()
    def scalar(self):
        return None
    def first(self):
        return None
    def all(self):
        return []


class _MockScalars:
    """Mock scalars() 返回。"""
    def all(self):
        return []
    def one(self):
        return 0
    def one_or_none(self):
        return None
    def first(self):
        return None


class _MockSession:
    """Mock 异步会话，兼容 async with 协议和常见 SQLAlchemy 操作。"""
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def commit(self): pass
    async def rollback(self): pass
    async def execute(self, stmt):
        return _MockResult()
    async def get(self, model, ident):
        return None
    def add(self, obj):
        pass


def _mock_session_factory() -> _MockSession:
    return _MockSession()


# ======================================================================
# 简单 Job 用于测试
# ======================================================================

class SimpleJob(BaseJob):
    """测试用简单 Job。"""

    def __init__(self, job_id: str = "test.simple", config: Dict = None) -> None:
        super().__init__(config)
        self._job_id = job_id

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id=self._job_id,
            name="Test Simple Job",
            trigger_type=TriggerType.INTERVAL,
            trigger_value="3600",
            timeout_seconds=5,
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok"}


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def mock_gateway() -> MockGateway:
    """Mock 网关（无真实 IM 平台）。"""
    return MockGateway()


@pytest.fixture
def registry() -> FunctionRegistry:
    """完整注册 12 个工具的 FunctionRegistry。"""
    r = FunctionRegistry()
    register_all(r)
    return r


@pytest.fixture
def scheduler() -> JobManager:
    """JobManager（使用 Mock DB session，无真实数据库）。"""
    mgr = JobManager(session_factory=_mock_session_factory, timezone="Asia/Shanghai")
    return mgr


@pytest.fixture
def brain_with_context(registry: FunctionRegistry, scheduler: JobManager, mock_gateway: MockGateway) -> CommandExecutor:
    """带完整 context_factory 的 CommandExecutor，注入 scheduler + gateway。"""
    parser = LLMIntentParser(registry=registry, api_key="test-key")

    def context_factory(msg: IMMessage) -> CommandContext:
        return CommandContext(
            user_id=msg.user_id,
            chat_id=msg.chat_id,
            username=msg.username,
            db_session_factory=_mock_session_factory,
            scheduler=scheduler,
            gateway=mock_gateway,
            llm_config={"model": "test-model", "api_key": "test-key", "base_url": "https://test.api/v1"},
        )

    return CommandExecutor(parser=parser, registry=registry, context_factory=context_factory)


# ======================================================================
# 集成测试 1：Gateway → Brain（完整 LLM Fallback 链路）
# ======================================================================

class TestGatewayBrainIntegration:
    """Gateway ↔ Brain 全链路集成测试。"""

    @pytest.mark.asyncio
    async def test_full_llm_fallback_chain(
        self, mock_gateway: MockGateway, registry: FunctionRegistry, brain_with_context: CommandExecutor
    ):
        """IMMessage → MessageRouter → CommandExecutor → 格式化回复 的完整链路。"""
        router = MessageRouter(
            mock_gateway,
            brain=brain_with_context,
            allowed_users={"u1"},
        )

        # Mock LLM 响应：query_score_trend
        async def mock_parse(text, history):
            return IntentResult(
                function_name="query_score_trend",
                arguments={"agent_version": "v2.3.1", "last_n": 3},
                risk_level="low",
            )

        brain_with_context._parser.parse = mock_parse

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="查一下 v2.3.1 的评分趋势", message_id="m1",
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "评分趋势" in reply
        assert "v2.3.1" in reply

    @pytest.mark.asyncio
    async def test_router_delegates_to_brain_and_formats_reply(
        self, mock_gateway: MockGateway, registry: FunctionRegistry, brain_with_context: CommandExecutor
    ):
        """验证 MessageRouter 正确委托给 brain 并返回格式化回复。"""
        router = MessageRouter(mock_gateway, brain=brain_with_context, allowed_users={"u1"})

        # Mock LLM 返回 get_latest_eval_status 意图
        async def mock_parse(text, history):
            return IntentResult(
                function_name="get_latest_eval_status",
                arguments={"hours_back": 24},
                risk_level="low",
            )

        brain_with_context._parser.parse = mock_parse

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="最近的评测状态怎么样", message_id="m1",
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "评测状态概览" in reply

    @pytest.mark.asyncio
    async def test_brain_exception_propagates_to_router(
        self, mock_gateway: MockGateway, brain_with_context: CommandExecutor
    ):
        """Brain 异常 → MessageRouter 兜底提示。"""
        router = MessageRouter(mock_gateway, brain=brain_with_context, allowed_users={"u1"})

        # 让 brain 抛出异常
        async def mock_parse_raise(text, history):
            raise RuntimeError("LLM explosion")

        brain_with_context._parser.parse = mock_parse_raise

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="hello", message_id="m1",
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "LLM" in reply

    @pytest.mark.asyncio
    async def test_conversation_history_flow(
        self, mock_gateway: MockGateway, brain_with_context: CommandExecutor
    ):
        """多轮对话历史正确管理。"""
        router = MessageRouter(mock_gateway, brain=brain_with_context, allowed_users={"u1"})

        call_count = [0]

        async def mock_parse(text, history):
            call_count[0] += 1
            # 第二轮应包含历史
            if call_count[0] == 2:
                assert any(m["content"] == "查一下评分" for m in history)
                assert any(m["content"] == "第一轮回复" for m in history)
            return IntentResult(
                function_name="query_score_trend",
                arguments={"agent_version": "v2.3.1", "last_n": 3},
                risk_level="low",
            )

        brain_with_context._parser.parse = mock_parse

        msg1 = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="查一下评分", message_id="m1",
        )
        reply1 = await router.handle(msg1)
        assert reply1 is not None

        msg2 = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="那 v2.3.0 呢", message_id="m2",
        )
        reply2 = await router.handle(msg2)
        assert reply2 is not None
        assert call_count[0] == 2


# ======================================================================
# 集成测试 2：Gateway → Scheduler（状态/任务查询）
# ======================================================================

class TestGatewaySchedulerIntegration:
    """Gateway ↔ Scheduler 全链路集成测试。"""

    @pytest.mark.asyncio
    async def test_status_command_with_real_scheduler(
        self, mock_gateway: MockGateway, scheduler: JobManager
    ):
        """/status 命令应正确查询调度器状态。"""
        router = MessageRouter(mock_gateway, scheduler=scheduler, allowed_users={"u1"})

        # 调度器未启动
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="/status", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "系统状态" in reply
        # 调度器未初始化/未启动
        assert "未初始化" in reply.lower() or "running" in reply.lower()

        # 启动调度器后再次查询
        scheduler._scheduler.start()
        scheduler._started = True

        reply2 = await router.handle(msg)
        assert "running" in reply2

        scheduler._scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_jobs_command_with_real_scheduler(
        self, mock_gateway: MockGateway, scheduler: JobManager
    ):
        """/jobs 命令应正确列出调度任务。"""
        router = MessageRouter(mock_gateway, scheduler=scheduler, allowed_users={"u1"})

        # 无任务时
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="/jobs", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "调度" in reply

        # 注册任务后
        scheduler.register(SimpleJob(job_id="test.integration"))
        scheduler.register(SimpleJob(job_id="test.integration2"))

        reply2 = await router.handle(msg)
        assert "test.integration" in reply2
        assert "test.integration2" in reply2

    @pytest.mark.asyncio
    async def test_jobs_without_scheduler_is_safe(
        self, mock_gateway: MockGateway
    ):
        """无 scheduler 时 /jobs 应安全降级。"""
        router = MessageRouter(mock_gateway, scheduler=None, allowed_users={"u1"})

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="/jobs", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "调度" in reply
        # 不应抛出 AttributeError


# ======================================================================
# 集成测试 3：三模块全链路（Gateway → Brain → Scheduler）
# ======================================================================

class TestFullChainIntegration:
    """Gateway → Brain → Scheduler 三模块全链路测试。"""

    @pytest.mark.asyncio
    async def test_tool_handler_accesses_scheduler_via_context(
        self, mock_gateway: MockGateway, registry: FunctionRegistry,
        scheduler: JobManager, brain_with_context: CommandExecutor
    ):
        """Tool handler 通过 CommandContext.scheduler 访问调度器。"""
        # 注册一个使用 scheduler 的 handler
        handler_called_with_scheduler = []

        async def test_handler(args: dict, ctx: CommandContext) -> dict:
            handler_called_with_scheduler.append(ctx.scheduler is not None)
            if ctx.scheduler:
                jobs = ctx.scheduler.list_jobs()
                return {"action": "list", "jobs": [{"job_id": j.job_id, "name": j.name} for j in jobs]}
            return {"action": "list", "jobs": []}

        registry.register(
            FunctionDef(
                name="manage_scheduler",
                description="管理调度器",
                parameters={"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]},
                category="action",
                risk_level="medium",
            ),
            test_handler,
        )

        router = MessageRouter(
            mock_gateway, brain=brain_with_context,
            scheduler=scheduler, allowed_users={"u1"},
        )

        # 注册任务到 scheduler
        scheduler.register(SimpleJob(job_id="test.context_job"))

        # Mock LLM 返回 manage_scheduler 意图
        async def mock_parse(text, history):
            return IntentResult(
                function_name="manage_scheduler",
                arguments={"action": "list"},
                risk_level="medium",
            )

        brain_with_context._parser.parse = mock_parse

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="列出调度任务", message_id="m1",
        )
        reply = await router.handle(msg)
        assert reply is not None
        # scheduler 应该被正确注入
        assert len(handler_called_with_scheduler) == 1
        assert handler_called_with_scheduler[0] is True

    @pytest.mark.asyncio
    async def test_scheduler_is_started_reflects_apscheduler_state(
        self, scheduler: JobManager
    ):
        """验证 is_started 同时检查 APScheduler 原生 running 状态。"""
        # 初始：未启动
        assert scheduler.is_started is False

        # 手动设置 _started=True 但 scheduler 未运行
        scheduler._started = True
        # is_started 应返回 False（因为 APScheduler 未 running）
        assert scheduler.is_started is False

        # 真正启动后
        scheduler._scheduler.start()
        assert scheduler.is_started is True

        scheduler._scheduler.shutdown(wait=False)

    @pytest.mark.asyncio
    async def test_command_context_injects_all_dependencies(
        self, mock_gateway: MockGateway, registry: FunctionRegistry,
        scheduler: JobManager, brain_with_context: CommandExecutor
    ):
        """验证 CommandContext 同时注入 scheduler + gateway。"""
        captured_context: list[CommandContext] = []

        async def capture_handler(args: dict, ctx: CommandContext) -> dict:
            captured_context.append(ctx)
            return {"status": "captured"}

        registry.register(
            FunctionDef(
                name="get_latest_eval_status",
                description="获取评测状态",
                parameters={"type": "object", "properties": {}, "required": []},
                category="query",
                risk_level="low",
            ),
            capture_handler,
        )

        router = MessageRouter(
            mock_gateway, brain=brain_with_context,
            scheduler=scheduler, allowed_users={"u1"},
        )

        async def mock_parse(text, history):
            return IntentResult(
                function_name="get_latest_eval_status",
                arguments={},
                risk_level="low",
            )

        brain_with_context._parser.parse = mock_parse

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="评测状态", message_id="m1",
        )
        await router.handle(msg)

        assert len(captured_context) == 1
        ctx = captured_context[0]
        assert ctx.scheduler is scheduler
        assert ctx.gateway is mock_gateway
        assert ctx.db_session_factory is not None
        assert ctx.llm_config["model"] == "test-model"


# ======================================================================
# 集成测试 4：确认流程（/eval → confirm token → Brain 执行）
# ======================================================================

class TestConfirmationFlowIntegration:
    """确认流程端到端集成测试。"""

    @pytest.mark.asyncio
    async def test_eval_confirmation_full_flow(
        self, mock_gateway: MockGateway, registry: FunctionRegistry,
        brain_with_context: CommandExecutor
    ):
        """/eval → 返回确认 token → confirm token → Brain 执行评测。"""
        eval_called_args = []

        async def trigger_eval_handler(args: dict, ctx: CommandContext) -> dict:
            eval_called_args.append(args)
            return {
                "task_id": "task-uuid-001",
                "agent_version": args.get("agent_version", ""),
                "case_set_name": "默认",
                "total_cases": 10,
                "layers": ["intent", "generation", "outcome"],
            }

        # 重新注册 trigger_evaluation（覆盖原有骨架实现）
        registry.register(
            FunctionDef(
                name="trigger_evaluation",
                description="触发评测",
                parameters={"type": "object", "properties": {"agent_version": {"type": "string"}}, "required": ["agent_version"]},
                category="action",
                risk_level="high",
                require_confirmation=True,
            ),
            trigger_eval_handler,
        )

        router = MessageRouter(
            mock_gateway, brain=brain_with_context, allowed_users={"u1"},
        )

        # Step 1: 发起 /eval v2.5.0
        msg_eval = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="/eval v2.5.0", message_id="m1", is_command=True,
        )
        reply1 = await router.handle(msg_eval)
        assert reply1 is not None
        assert "confirm" in reply1
        assert "v2.5.0" in reply1

        # 提取 token
        token = None
        for t in router._pending_confirmations:
            if router._pending_confirmations[t].action == "eval":
                token = t
                break
        assert token is not None, "应生成确认 token"

        # Step 2: 确认
        msg_confirm = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text=f"confirm {token}", message_id="m2",
        )
        reply2 = await router.handle(msg_confirm)
        assert reply2 is not None
        assert "v2.5.0" in reply2
        assert eval_called_args == [{"agent_version": "v2.5.0"}]

    @pytest.mark.asyncio
    async def test_confirmation_expires(
        self, mock_gateway: MockGateway, brain_with_context: CommandExecutor
    ):
        """过期 token 应被拒绝。"""
        router = MessageRouter(
            mock_gateway, brain=brain_with_context, allowed_users={"u1"},
        )

        # 手动创建一个已过期的 pending action
        token = router._generate_confirmation_token()
        router._pending_confirmations[token] = PendingAction(
            action="eval",
            args={"agent_version": "v2.0"},
            user_id="u1",
            chat_id="c1",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text=f"confirm {token}", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "过期" in reply

    @pytest.mark.asyncio
    async def test_sample_large_requires_confirmation_then_executes(
        self, mock_gateway: MockGateway, registry: FunctionRegistry,
        brain_with_context: CommandExecutor
    ):
        """大量 /sample → 确认 → 执行。"""
        sample_called_args = []

        async def sample_handler(args: dict, ctx: CommandContext) -> dict:
            sample_called_args.append(args)
            return {
                "sampled": args.get("sample_size", 0),
                "batch_id": "batch-001",
                "task_id": "task-001",
                "hours_back": 24,
            }

        registry.register(
            FunctionDef(
                name="sample_and_evaluate",
                description="采样并评测",
                parameters={"type": "object", "properties": {"sample_size": {"type": "integer"}}, "required": []},
                category="action",
                risk_level="medium",
            ),
            sample_handler,
        )

        router = MessageRouter(
            mock_gateway, brain=brain_with_context, allowed_users={"u1"},
        )

        # Step 1: /sample 30 → 确认
        msg1 = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="/sample 30", message_id="m1", is_command=True,
        )
        reply1 = await router.handle(msg1)
        assert "confirm" in reply1
        assert "30" in reply1

        # 提取 token
        token = next(
            t for t, pa in router._pending_confirmations.items()
            if pa.action == "sample"
        )

        # Step 2: 确认
        msg2 = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text=f"confirm {token}", message_id="m2",
        )
        reply2 = await router.handle(msg2)
        assert reply2 is not None
        assert sample_called_args == [{"sample_size": 30}]


# ======================================================================
# 集成测试 5：速率限制 + 白名单 + Brain 组合
# ======================================================================

class TestCombinedSecurityFlow:
    """白名单 + 速率限制 + Brain 组合测试。"""

    @pytest.mark.asyncio
    async def test_whitelist_blocks_before_brain(
        self, mock_gateway: MockGateway, brain_with_context: CommandExecutor
    ):
        """非白名单用户应被静默拒绝，不会调用 Brain。"""
        brain_called = False

        async def spy_parse(text, history):
            nonlocal brain_called
            brain_called = True
            return IntentResult(function_name="fallback_chat", arguments={"reply": "x"})

        brain_with_context._parser.parse = spy_parse

        router = MessageRouter(
            mock_gateway, brain=brain_with_context, allowed_users={"good_user"},
        )

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="evil_user",
            username="evil", text="hello", message_id="m1",
        )
        reply = await router.handle(msg)
        assert reply is None  # 静默拒绝
        assert brain_called is False  # Brain 未被调用

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_before_brain(
        self, mock_gateway: MockGateway, brain_with_context: CommandExecutor
    ):
        """速率限制应在调用 Brain 之前拦截。"""
        brain_called = False

        async def spy_parse(text, history):
            nonlocal brain_called
            brain_called = True
            return IntentResult(function_name="fallback_chat", arguments={"reply": "x"})

        brain_with_context._parser.parse = spy_parse

        router = MessageRouter(
            mock_gateway, brain=brain_with_context, allowed_users={"u1"},
        )
        # 设置极低限制
        router._rate_limiter._max = 1
        router._rate_limiter._buckets["u1"] = __import__("collections").deque(
            [datetime.now(timezone.utc)]
        )

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="test", text="hello", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "频繁" in reply
        assert brain_called is False


# ======================================================================
# 集成测试 6：JobManager ↔ APScheduler 状态一致性
# ======================================================================

class TestSchedulerStateConsistency:
    """JobManager 与 APScheduler 状态一致性测试。"""

    @pytest.mark.asyncio
    async def test_is_started_reflects_real_state(self, scheduler: JobManager):
        """is_started 应反映 APScheduler 真实运行状态。"""
        # 初始状态
        assert scheduler.is_started is False

        # 模拟标志位不一致场景
        scheduler._started = True
        assert scheduler.is_started is False  # APScheduler 未运行

        # 真正启动
        scheduler._scheduler.start()
        assert scheduler.is_started is True

        # 关闭
        scheduler._scheduler.shutdown(wait=False)
        scheduler._started = False
        assert scheduler.is_started is False

    @pytest.mark.asyncio
    async def test_trigger_now_creates_valid_execution_id(self, scheduler: JobManager):
        """trigger_now 应返回有效 UUID。"""
        scheduler.register(SimpleJob(job_id="test.trigger"))
        exec_id = await scheduler.trigger_now("test.trigger")
        assert exec_id is not None
        uuid.UUID(exec_id)  # 验证 UUID 格式

    @pytest.mark.asyncio
    async def test_pause_resume_roundtrip(self, scheduler: JobManager):
        """暂停 → 恢复 完整流程。"""
        job = SimpleJob(job_id="test.pause_resume")
        scheduler.register(job)

        scheduler._scheduler.start()
        scheduler._started = True

        # 暂停
        scheduler.pause("test.pause_resume")
        aps_job = scheduler._scheduler.get_job("test.pause_resume")
        assert aps_job.next_run_time is None

        # 恢复
        scheduler.resume("test.pause_resume")
        aps_job = scheduler._scheduler.get_job("test.pause_resume")
        assert aps_job.next_run_time is not None

        scheduler._scheduler.shutdown(wait=False)
