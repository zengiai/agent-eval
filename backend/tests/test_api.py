"""API 端点测试 —— 验证 FastAPI 路由的请求/响应格式和状态码。

注意：API 测试需要数据库支持，如未设置 RUN_INTEGRATION_TESTS=1 则会自动跳过。
"""

import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api import app
from backend.core.models import EvalTask, CaseSet, CaseSetMember, EvalCase, EvalRun, Trace


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
