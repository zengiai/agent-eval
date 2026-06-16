"""端到端集成测试 —— 模拟 Agent SDK 上报完整 Trace → Ingest 落表 → 评测执行 → 验证结果。

通过 SDK 写入 Redis → IngestWorker 消费 → evaluate_trace 评测 → 验证:
  1. 5 条 eval_scores 生成（含 Outcome 层 span_id=NULL + trace_id 关联）
  2. spans.score 回填正确（仅前四层）
  3. traces.overall_score 回填正确
  4. eval_run.status 更新为 completed

需要: docker-compose up (PostgreSQL + Redis) + RUN_INTEGRATION_TESTS=1
"""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import select

from backend.core.config import settings
from backend.core.database import async_session_factory, Base, engine
from backend.core.models import (
    CaseSet, CaseSetMember, EvalCase, EvalTask, EvalRun,
    Trace, Span, EvalScore,
)
from backend.workers.ingest_worker import IngestWorker
from backend.workers.eval_worker import evaluate_trace


def _skip_if_no_integration():
    if os.environ.get("RUN_INTEGRATION_TESTS", "").lower() not in ("1", "true", "yes"):
        pytest.skip("集成测试未启用（设置 RUN_INTEGRATION_TESTS=1 启用）")


@pytest_asyncio.fixture
async def setup_tables():
    """确保表存在。"""
    _skip_if_no_integration()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # 不删除表，避免影响其他测试


@pytest_asyncio.fixture
async def seed_eval_data(setup_tables):
    """创建评测任务 + Case + Run（模拟评测触发路径）。"""
    _skip_if_no_integration()
    async with async_session_factory() as session:
        async with session.begin():
            # Case
            case = EvalCase(
                query="今天北京天气怎么样？",
                context={"user_location": "北京"},
                expected_intent={"intents": ["weather_query"], "mode": "any"},
                expected_retrieval={
                    "doc_ids": ["weather_beijing_20250101", "aqi_beijing_20250101"],
                    "min_precision": 0.8,
                },
                expected_tools=[{"tool_name": "get_weather", "params": {"city": "北京"}}],
                expected_answer={"key_phrases": ["晴", "温度", "风力"]},
                gold_answer="北京今天晴，气温 -5°C ~ 3°C，北风3-4级。",
                source="manual", difficulty="easy", category="weather", review_status="approved",
            )
            session.add(case)
            await session.flush()

            # CaseSet
            case_set = CaseSet(name="e2e-smoke", category="smoke", version="1.0.0", case_count=1)
            session.add(case_set)
            await session.flush()
            session.add(CaseSetMember(case_set_id=case_set.id, case_id=case.id))

            # Task
            task = EvalTask(
                name="e2e-test-task",
                agent_version="v1.0.0",
                case_set_id=case_set.id,
                total_cases=1,
            )
            session.add(task)
            await session.flush()

            # Run（暂不关联 trace_id，等 Trace 创建后回填）
            run = EvalRun(
                task_id=task.id,
                eval_case_id=case.id,
                agent_version="v1.0.0",
                status="pending",
                expected_snapshot={
                    "expected_intent": {"intents": ["weather_query"], "mode": "any"},
                    "expected_retrieval": {
                        "doc_ids": ["weather_beijing_20250101", "aqi_beijing_20250101"],
                        "min_precision": 0.8,
                    },
                    "expected_tools": [{"tool_name": "get_weather", "params": {"city": "北京"}}],
                    "expected_answer": {"key_phrases": ["晴", "温度", "风力"]},
                    "gold_answer": "北京今天晴，气温 -5°C ~ 3°C，北风3-4级。",
                },
            )
            session.add(run)
            await session.flush()

            case_id = str(case.id)
            task_id = str(task.id)
            run_id = str(run.id)

    return {"case_id": case_id, "task_id": task_id, "run_id": run_id}


# ============================================================
# E2E: SDK → Redis → Ingest → 评测
# ============================================================

class TestE2EEvaluation:
    @pytest.mark.anyio
    async def test_full_pipeline(self, seed_eval_data, setup_tables):
        """端到端完整管线测试。"""
        _skip_if_no_integration()
        run_id = seed_eval_data["run_id"]

        # ---- Phase 1: 模拟 Agent SDK 上报事件到 Redis ----
        r = aioredis.from_url(settings.REDIS_URL)
        trace_id = str(uuid.uuid4())

        # trace_start
        import json
        import time
        await r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps({
            "type": "trace_start",
            "trace_id": trace_id,
            "agent_version": "v1.0.0",
            "query": "今天北京天气怎么样？",
            "context": {"user_location": "北京"},
            "source": "eval",
            "run_id": run_id,
            "timestamp": time.time(),
        }))

        # span: intent
        await r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps({
            "type": "span",
            "trace_id": trace_id,
            "span_type": "intent",
            "sequence": 1,
            "output": {"intents": ["weather_query"], "confidence": 0.95},
            "latency_ms": 120,
            "tokens": {"input": 50, "output": 20},
            "model": "qwen3.7-max",
            "timestamp": time.time(),
        }))

        # span: retrieval
        await r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps({
            "type": "span",
            "trace_id": trace_id,
            "span_type": "retrieval",
            "sequence": 2,
            "output": {
                "results": [
                    {"id": "weather_beijing_20250101", "title": "北京今日天气", "score": 0.92},
                    {"id": "aqi_beijing_20250101", "title": "北京空气质量", "score": 0.85},
                    {"id": "weather_shanghai_20250101", "title": "上海今日天气", "score": 0.30},
                ],
            },
            "latency_ms": 200,
            "model": "text-embedding-3",
            "timestamp": time.time(),
        }))

        # span: tool_call
        await r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps({
            "type": "span",
            "trace_id": trace_id,
            "span_type": "tool_call",
            "sequence": 3,
            "tool_name": "get_weather",
            "tool_params": {"city": "北京"},
            "tool_result": {"status": "success", "temperature": -1, "condition": "晴"},
            "latency_ms": 350,
            "timestamp": time.time(),
        }))

        # span: generation
        await r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps({
            "type": "span",
            "trace_id": trace_id,
            "span_type": "generation",
            "sequence": 4,
            "output": {"response": "北京今天晴，气温-5°C~3°C，北风3-4级。空气质量良好。"},
            "latency_ms": 800,
            "tokens": {"input": 300, "output": 100},
            "model": "qwen3.7-max",
            "timestamp": time.time(),
        }))

        # trace_finish
        await r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps({
            "type": "trace_finish",
            "trace_id": trace_id,
            "final_response": "北京今天晴，气温-5°C~3°C，北风3-4级。空气质量良好。",
            "status": "success",
            "total_latency_ms": 1500,
            "total_tokens": {"input": 350, "output": 120},
            "total_cost_usd": 0.015,
            "timestamp": time.time(),
        }))

        await r.close()

        # ---- Phase 2: IngestWorker 消费 Redis 事件 → 写入 DB ----
        worker = IngestWorker()
        worker._redis = aioredis.from_url(settings.REDIS_URL)
        await worker._consume_batch()
        await worker._redis.close()

        # 验证 Trace 和 Spans 已落表
        async with async_session_factory() as session:
            trace = await session.get(Trace, uuid.UUID(trace_id))
            assert trace is not None, "Trace 未写入数据库"
            assert trace.query == "今天北京天气怎么样？"
            assert trace.status == "success"
            assert trace.final_response is not None

            result = await session.execute(
                select(Span).where(Span.trace_id == uuid.UUID(trace_id)).order_by(Span.sequence)
            )
            spans = result.scalars().all()
            assert len(spans) == 4, f"期望 4 个 Span，实际 {len(spans)}"
            span_types = {s.span_type for s in spans}
            assert span_types == {"intent", "retrieval", "tool_call", "generation"}

            # 验证 eval_runs.trace_id 已回填
            run = await session.get(EvalRun, uuid.UUID(run_id))
            assert run is not None
            assert str(run.trace_id) == trace_id, f"eval_run.trace_id 未回填: {run.trace_id} != {trace_id}"

        # ---- Phase 3: 执行评测 ----
        result = await evaluate_trace(trace_id)
        assert result.get("error") is None, f"评测失败: {result.get('error')}"
        assert "__overall__" not in result  # evaluate_trace 返回简化结构
        assert "overall_score" in result
        assert 0 <= result["overall_score"] <= 100

        # ---- Phase 4: 验证评测结果 ----
        async with async_session_factory() as session:
            # 4a: 验证 5 条 eval_scores（含 Outcome span_id=NULL）
            eval_scores_result = await session.execute(
                select(EvalScore).where(EvalScore.trace_id == uuid.UUID(trace_id))
            )
            scores = eval_scores_result.scalars().all()
            assert len(scores) == 5, f"期望 5 条 eval_scores，实际 {len(scores)}"

            # 验证 Outcome 层 span_id=NULL
            outcome_scores = [s for s in scores if s.span_id is None]
            assert len(outcome_scores) >= 1, "Outcome 层应至少有 1 条 span_id=NULL 的得分记录"
            for os_ in outcome_scores:
                assert os_.trace_id == uuid.UUID(trace_id), "Outcome 层分数应关联 trace_id"
                assert 0 <= float(os_.score) <= 100

            # 验证每条 score 都有 trace_id
            for s in scores:
                assert s.trace_id == uuid.UUID(trace_id), f"eval_score {s.id} 缺少 trace_id"

            # 4b: 验证 spans.score 回填（前四层）
            result = await session.execute(
                select(Span).where(Span.trace_id == uuid.UUID(trace_id)).order_by(Span.sequence)
            )
            spans = result.scalars().all()
            span_by_type = {s.span_type: s for s in spans}

            for layer in ["intent", "retrieval", "tool_call", "generation"]:
                span = span_by_type.get(layer)
                assert span is not None, f"缺少 {layer} span"
                assert span.score is not None, f"{layer} span.score 未回填"
                assert 0 <= float(span.score) <= 100, f"{layer} span.score 范围异常: {span.score}"

            # 4c: 验证 traces.overall_score 回填
            trace = await session.get(Trace, uuid.UUID(trace_id))
            assert trace.overall_score is not None, "traces.overall_score 未回填"
            assert 0 <= float(trace.overall_score) <= 100

            # 4d: 验证 eval_run.status 更新为 completed
            run = await session.get(EvalRun, uuid.UUID(run_id))
            assert run.status == "completed", f"eval_run.status 应为 completed，实际: {run.status}"

            # 4e: 打印验证汇总
            print(f"\n✅ E2E 验证通过:")
            print(f"   Trace: {trace_id}")
            print(f"   Overall Score: {trace.overall_score}")
            for layer in ["intent", "retrieval", "tool_call", "generation"]:
                print(f"   {layer}: span.score = {span_by_type[layer].score}")
            print(f"   eval_scores: {len(scores)} 条（含 {len(outcome_scores)} 条 Outcome span_id=NULL）")
            print(f"   eval_run.status: {run.status}")
