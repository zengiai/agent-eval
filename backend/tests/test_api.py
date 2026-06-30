"""API 端点测试 —— 验证 FastAPI 路由的请求/响应格式和状态码。

注意：API 测试需要数据库支持，如未设置 RUN_INTEGRATION_TESTS=1 则会自动跳过。
"""

import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import func, select

from backend.api import app
from backend.api import cases as cases_api
from backend.api import case_sets as case_sets_api
from backend.core.database import get_db
from backend.core.models import EvalScore, EvalTask, CaseSet, CaseSetMember, EvalCase, EvalRun, Span, Trace


def _override_get_db(session_factory):
    async def _get_db_override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db_override


def _clear_overrides():
    app.dependency_overrides.clear()


# ============================================================
# Health Check（无需数据库）
# ============================================================

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["version"] == "0.1.0"


# ============================================================
# Tasks API
# ============================================================

class TestTasksAPI:
    @pytest.mark.asyncio
    async def test_create_task(self, test_engine):
        """通过 API 创建任务并验证返回。"""
        transport = ASGITransport(app=app)

        # 先用 API 无法直接创建 CaseSet（无对应端点），用 DB 插入
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                case = EvalCase(query="test query", source="manual", difficulty="easy")
                session.add(case)
                await session.flush()
                case_set = CaseSet(name="api-test-set", case_count=1)
                session.add(case_set)
                await session.flush()
                session.add(CaseSetMember(case_set_id=case_set.id, case_id=case.id))
                await session.flush()
                case_set_id = str(case_set.id)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/tasks", json={
                "name": "api-test-task",
                "agent_version": "v1.0.0",
                "case_set_id": case_set_id,
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "api-test-task"
            assert data["agent_version"] == "v1.0.0"

    @pytest.mark.asyncio
    async def test_create_task_case_set_not_found(self, test_engine):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/tasks", json={
                "name": "test",
                "agent_version": "v1.0.0",
                "case_set_id": str(uuid.uuid4()),
            })
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_tasks(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                task = EvalTask(name="list-test", agent_version="v1.0.0", total_cases=0)
                session.add(task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/tasks")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_get_task(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                task = EvalTask(name="get-test", agent_version="v2.0.0", total_cases=5)
                session.add(task)
                await session.flush()
                task_id = str(task.id)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/tasks/{task_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "get-test"
            assert data["agent_version"] == "v2.0.0"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, test_engine):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/tasks/{uuid.uuid4()}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_trigger_evaluation_creates_k_attempt_runs(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                case_set = CaseSet(name=f"trigger-k-{uuid.uuid4().hex[:8]}", case_count=2)
                session.add(case_set)
                await session.flush()
                for idx in range(2):
                    case = EvalCase(query=f"trigger query {idx}", source="manual")
                    session.add(case)
                    await session.flush()
                    session.add(CaseSetMember(case_set_id=case_set.id, case_id=case.id))
                case_set_name = case_set.name

        _override_get_db(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/tasks/trigger",
                    json={
                        "agent_version": "v-k",
                        "case_set_name": case_set_name,
                        "pass_policy": {"formula": "pass_k", "k": 3, "score_threshold": 80},
                    },
                )

            assert resp.status_code == 201
            data = resp.json()
            assert data["total_cases"] == 2
            assert data["total_runs"] == 6
            assert data["pass_policy"]["k"] == 3

            async with session_factory() as session:
                runs = (
                    await session.execute(
                        select(EvalRun).where(EvalRun.task_id == uuid.UUID(data["task_id"]))
                    )
                ).scalars().all()
            assert len(runs) == 6
            assert sorted({run.attempt_index for run in runs}) == [1, 2, 3]
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_recompute_and_get_case_set_result(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                case_set = CaseSet(name=f"result-api-{uuid.uuid4().hex[:8]}", case_count=1)
                session.add(case_set)
                case = EvalCase(query="result query", source="manual")
                session.add(case)
                await session.flush()
                session.add(CaseSetMember(case_set_id=case_set.id, case_id=case.id))
                task = EvalTask(
                    name="result-task",
                    agent_version="v-result",
                    case_set_id=case_set.id,
                    total_cases=1,
                    config={"pass_policy": {"formula": "pass_power_k", "k": 2, "score_threshold": 80, "power_threshold": 0.9}},
                )
                session.add(task)
                await session.flush()
                for idx, score in enumerate([90.0, 88.0], start=1):
                    trace = Trace(
                        agent_version="v-result",
                        query=f"result query {idx}",
                        status="success",
                        source="eval",
                        overall_score=score,
                    )
                    session.add(trace)
                    await session.flush()
                    session.add(EvalRun(
                        task_id=task.id,
                        eval_case_id=case.id,
                        agent_version="v-result",
                        attempt_index=idx,
                        status="completed",
                        trace_id=trace.id,
                    ))
                task_id = str(task.id)

        _override_get_db(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                recompute = await client.post(f"/api/tasks/{task_id}/result/recompute?include_cases=true")
                fetched = await client.get(f"/api/tasks/{task_id}/result?include_cases=true")

            assert recompute.status_code == 200
            assert fetched.status_code == 200
            data = fetched.json()
            assert data["status"] == "completed"
            assert data["passed"] is True
            assert data["formula"] == "pass_power_k"
            assert data["passed_cases"] == 1
            assert len(data["cases"]) == 1
            assert data["cases"][0]["passed_attempts"] == 2
            assert data["cases"][0]["required_passes"] == 2
        finally:
            _clear_overrides()


# ============================================================
# Runs API
# ============================================================

class TestRunsAPI:
    @pytest.mark.asyncio
    async def test_list_runs(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                task = EvalTask(name="runs-test", agent_version="v1.0.0")
                session.add(task)
                await session.flush()
                run = EvalRun(
                    task_id=task.id,
                    eval_case_id=uuid.uuid4(),
                    agent_version="v1.0.0",
                    status="pending",
                )
                session.add(run)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/runs")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_get_run_not_found(self, test_engine):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/runs/{uuid.uuid4()}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_run_with_scores(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                task = EvalTask(name="score-test", agent_version="v1.0.0")
                session.add(task)
                await session.flush()
                trace = Trace(
                    agent_version="v1.0.0",
                    query="test query",
                    status="success",
                    source="eval",
                    overall_score=85.0,
                )
                session.add(trace)
                await session.flush()
                run = EvalRun(
                    task_id=task.id,
                    eval_case_id=uuid.uuid4(),
                    agent_version="v1.0.0",
                    status="completed",
                    trace_id=trace.id,
                )
                session.add(run)
                await session.flush()
                run_id = str(run.id)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/runs/{run_id}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            assert data["trace_id"] == str(trace.id)


# ============================================================
# Cases API — Span 层归属与评分前置 Answer
# ============================================================

class TestCasesAPI:
    @pytest.mark.asyncio
    async def test_update_case_span_layer_without_scores(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                trace = Trace(
                    agent_version="v1.0.0",
                    query="span layer test",
                    status="success",
                    source="production",
                )
                session.add(trace)
                await session.flush()
                span = Span(
                    trace_id=trace.id,
                    span_type="generation",
                    sequence=1,
                    output={"response": "ok"},
                )
                session.add(span)
                case = EvalCase(
                    query=trace.query,
                    source="trace",
                    source_trace_id=trace.id,
                    metadata_={
                        "spans_summary": [
                            {"span_type": "generation", "sequence": 1},
                        ]
                    },
                )
                session.add(case)
                await session.flush()
                case_id = str(case.id)
                span_id = str(span.id)

        _override_get_db(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.patch(
                    f"/api/cases/{case_id}/spans/{span_id}/layer",
                    json={"span_type": "retrieval"},
                )

            assert resp.status_code == 200
            data = resp.json()
            assert data["old_span_type"] == "generation"
            assert data["span_type"] == "retrieval"

            async with session_factory() as session:
                updated = await session.get(Span, uuid.UUID(span_id))
                updated_case = await session.get(EvalCase, uuid.UUID(case_id))
                assert updated.span_type == "retrieval"
                assert updated_case.metadata_["spans_summary"][0]["span_type"] == "retrieval"
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_update_case_span_layer_rejects_scored_trace(self, test_engine):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                trace = Trace(
                    agent_version="v1.0.0",
                    query="scored span layer test",
                    status="success",
                    source="production",
                )
                session.add(trace)
                await session.flush()
                span = Span(trace_id=trace.id, span_type="generation", sequence=1)
                session.add(span)
                await session.flush()
                session.add(EvalScore(
                    trace_id=trace.id,
                    span_id=span.id,
                    score=90.0,
                    metrics={},
                    method="rule",
                ))
                case = EvalCase(query=trace.query, source="trace", source_trace_id=trace.id)
                session.add(case)
                await session.flush()
                case_id = str(case.id)
                span_id = str(span.id)

        _override_get_db(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.patch(
                    f"/api/cases/{case_id}/spans/{span_id}/layer",
                    json={"span_type": "intent"},
                )

            assert resp.status_code == 409
            assert "评分" in resp.json()["detail"]
        finally:
            _clear_overrides()

    @pytest.mark.asyncio
    async def test_evaluate_case_accepts_manual_gold_answer(self, test_engine, monkeypatch):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            async with session.begin():
                trace = Trace(
                    agent_version="v1.0.0",
                    query="answer override test",
                    status="success",
                    source="production",
                    final_response="实际回答",
                )
                session.add(trace)
                await session.flush()
                session.add(Span(
                    trace_id=trace.id,
                    span_type="generation",
                    sequence=1,
                    output={"response": "实际回答"},
                ))
                case = EvalCase(query=trace.query, source="trace", source_trace_id=trace.id)
                session.add(case)
                await session.flush()
                case_id = str(case.id)

        class DummyTask:
            pass

        def fake_create_task(coro):
            coro.close()
            return DummyTask()

        monkeypatch.setattr(cases_api.asyncio, "create_task", fake_create_task)
        _override_get_db(session_factory)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/cases/{case_id}/evaluate",
                    json={"gold_answer": "人工设置的期望 Answer"},
                )

            assert resp.status_code == 202
            async with session_factory() as session:
                run = (
                    await session.execute(
                        select(EvalRun).where(EvalRun.eval_case_id == uuid.UUID(case_id))
                    )
                ).scalar_one()
                updated_case = await session.get(EvalCase, uuid.UUID(case_id))
                assert run.expected_snapshot["gold_answer"] == "人工设置的期望 Answer"
                assert updated_case.gold_answer == "人工设置的期望 Answer"
        finally:
            _clear_overrides()


# ============================================================
# CaseSets API — 批量 question 对话转 Case
# ============================================================

class TestCaseSetsAPI:
    def test_normalize_batch_questions_filters_blank_lines(self):
        req = case_sets_api.CaseSetBatchFromQuestionsRequest(
            name="batch",
            questions=["  q1  ", ""],
            questions_text="\nq2\n  \nq3  ",
        )

        assert case_sets_api._normalize_questions(req) == ["q1", "q2", "q3"]

    @pytest.mark.asyncio
    async def test_batch_from_questions_creates_trace_backed_cases(self, test_engine, monkeypatch):
        session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(case_sets_api, "_batch_session_factory", session_factory)

        runtime_calls = []

        async def fake_runtime_call(method, path, json=None):
            runtime_calls.append((method, path, json))
            return {"reply_html": f"answer for {json['message']}"}

        captured_tasks = []

        def fake_create_task(coro):
            captured_tasks.append(coro)

            class DummyTask:
                pass

            return DummyTask()

        monkeypatch.setattr(case_sets_api, "_call_runtime_bridge", fake_runtime_call)
        monkeypatch.setattr(case_sets_api.asyncio, "create_task", fake_create_task)

        _override_get_db(session_factory)
        try:
            case_set_name = f"batch-api-test-{uuid.uuid4().hex[:8]}"
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/case-sets/batch-from-questions",
                    json={
                        "name": case_set_name,
                        "category": "smoke",
                        "agent_version": "v-test",
                        "questions_text": "q1\nq2",
                        "concurrency": 2,
                        "trace_wait_seconds": 0,
                    },
                )

            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "running"
            assert data["total_questions"] == 2
            assert len(captured_tasks) == 1

            await captured_tasks[0]

            async with session_factory() as session:
                case_set = await session.get(CaseSet, uuid.UUID(data["case_set_id"]))
                task = await session.get(EvalTask, uuid.UUID(data["task_id"]))
                member_count = (
                    await session.execute(
                        select(func.count()).where(CaseSetMember.case_set_id == uuid.UUID(data["case_set_id"]))
                    )
                ).scalar()
                member_case_ids = (
                    await session.execute(
                        select(CaseSetMember.case_id).where(CaseSetMember.case_set_id == uuid.UUID(data["case_set_id"]))
                    )
                ).scalars().all()
                cases = (
                    await session.execute(select(EvalCase).where(EvalCase.id.in_(member_case_ids)).order_by(EvalCase.query))
                ).scalars().all()
                trace_ids = [case.source_trace_id for case in cases]
                traces = (
                    await session.execute(select(Trace).where(Trace.id.in_(trace_ids)))
                ).scalars().all()
                spans = (
                    await session.execute(select(Span).where(Span.trace_id.in_(trace_ids)).order_by(Span.sequence))
                ).scalars().all()

            assert len(runtime_calls) == 2
            assert case_set.case_count == 2
            assert task.status == "completed"
            assert task.completed_cases == 2
            assert task.failed_cases == 0
            assert task.summary_metrics["success_count"] == 2
            assert member_count == 2
            assert len(traces) == 2
            assert all(trace.context["case_set_batch"]["trace_origin"] == "dashboard_case_set_batch_wrapper" for trace in traces)
            assert all(trace.source_ref and trace.source_ref.startswith("caseset_batch:") for trace in traces)
            assert {case.query for case in cases} == {"q1", "q2"}
            assert all(case.source == "trace" and case.source_trace_id for case in cases)
            assert all(case.metadata_["case_set_batch"]["trace_origin"] == "dashboard_case_set_batch_wrapper" for case in cases)
            assert len(spans) == 2
            assert all(span.span_type == "generation" for span in spans)
        finally:
            _clear_overrides()
