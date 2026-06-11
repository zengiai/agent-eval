"""测试夹具 —— 为单元测试和集成测试提供共享 fixture。"""

import os
import uuid
from typing import Dict, Any, AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.database import Base
from backend.core.models import (
    CaseSet, CaseSetMember, EvalCase,
)

# ============================================================
# 测试数据库 URL（可通过环境变量覆盖）
# ============================================================
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://agent_eval:agent_eval_pass@localhost:5432/agent_eval_test",
)


def _db_available() -> bool:
    """检查集成测试是否启用。"""
    return os.environ.get("RUN_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes")


# ============================================================
# 不需要数据库的纯单元测试用 fixture
# ============================================================

@pytest.fixture
def sample_span_intent() -> Dict[str, Any]:
    """模拟 intent span。"""
    return {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "span_type": "intent",
        "sequence": 1,
        "input": {"query": "今天北京天气怎么样？"},
        "output": {"intents": ["weather_query"], "confidence": 0.95},
        "latency_ms": 120,
        "tokens": {"input": 50, "output": 20},
        "model": "gpt-4o",
    }


@pytest.fixture
def sample_span_retrieval() -> Dict[str, Any]:
    """模拟 retrieval span。"""
    return {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "span_type": "retrieval",
        "sequence": 2,
        "input": {"query": "今天北京天气怎么样？"},
        "output": {
            "results": [
                {"id": "weather_beijing_20250101", "title": "北京今日天气", "score": 0.92},
                {"id": "aqi_beijing_20250101", "title": "北京空气质量", "score": 0.85},
                {"id": "weather_shanghai_20250101", "title": "上海今日天气", "score": 0.30},
            ],
        },
        "latency_ms": 200,
        "tokens": {},
        "model": "text-embedding-3",
    }


@pytest.fixture
def sample_span_tool() -> Dict[str, Any]:
    """模拟 tool_call span。"""
    return {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "span_type": "tool_call",
        "sequence": 3,
        "input": {"tool_name": "get_weather", "params": {"city": "北京"}},
        "output": {"temperature": -1, "condition": "晴", "humidity": "30%"},
        "tool_name": "get_weather",
        "tool_params": {"city": "北京"},
        "tool_result": {"status": "success", "temperature": -1, "condition": "晴"},
        "tool_status": "success",
        "latency_ms": 350,
        "tokens": {},
        "model": None,
    }


@pytest.fixture
def sample_span_generation() -> Dict[str, Any]:
    """模拟 generation span。"""
    return {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "span_type": "generation",
        "sequence": 4,
        "output": {"response": "北京今天晴，气温 -5°C ~ 3°C。"},
        "latency_ms": 800,
        "tokens": {"input": 300, "output": 100},
        "model": "gpt-4o",
    }


@pytest.fixture
def sample_trace(sample_span_intent, sample_span_retrieval, sample_span_tool, sample_span_generation) -> Dict[str, Any]:
    """模拟完整的 Trace 数据（含 4 个 span）。"""
    return {
        "id": str(uuid.uuid4()),
        "agent_version": "v1.0.0",
        "query": "今天北京天气怎么样？",
        "context": {"user_location": "北京"},
        "final_response": "北京今天晴，气温 -5°C ~ 3°C，北风3-4级。",
        "status": "success",
        "source": "eval",
        "total_latency_ms": 1500,
        "total_tokens": {"input": 350, "output": 120},
        "total_cost_usd": 0.015,
        "spans": [
            sample_span_intent,
            sample_span_retrieval,
            sample_span_tool,
            sample_span_generation,
        ],
    }


@pytest.fixture
def expected_snapshot() -> Dict[str, Any]:
    """模拟期望快照（与 seed data case 1 对应）。"""
    return {
        "expected_intent": {
            "intents": ["weather_query"],
            "mode": "any",
        },
        "expected_retrieval": {
            "relevant_ids": ["weather_beijing_20250101", "aqi_beijing_20250101"],
        },
        "expected_tools": [
            {"tool_name": "get_weather", "params": {"city": "北京"}},
        ],
        "expected_answer": {
            "key_phrases": ["晴", "温度", "风力"],
        },
    }


# ============================================================
# 需要数据库的集成测试用 fixture（函数级，按需加载）
# ============================================================

@pytest.fixture
async def test_engine():
    """创建测试数据库引擎（函数级，仅集成测试时使用）。"""
    if not _db_available():
        pytest.skip("集成测试未启用（设置 RUN_INTEGRATION_TESTS=1 启用）")
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """提供数据库会话（每个测试独立事务，自动回滚）。"""
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()
