"""FastAPI 应用入口。"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.ingest import router as ingest_router
from backend.api.tasks import router as tasks_router
from backend.api.runs import router as runs_router
from backend.api.cases import router as cases_router
from backend.api.stats import router as stats_router
from backend.api.case_sets import router as case_sets_router
from backend.api.alerts import router as alerts_router
from backend.workers import IngestWorker

from fastapi.staticfiles import StaticFiles
from pathlib import Path


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    # 启动 Ingest 消费者，持续轮询 Redis 将事件落库
    _ingest_worker = IngestWorker()
    task = asyncio.create_task(_ingest_worker.start())
    yield
    # 关闭时停止 Ingest 消费者
    await _ingest_worker.stop()
    task.cancel()


app = FastAPI(
    title="Agent Eval API",
    description="Agent 调用链路自动评测系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(ingest_router)
app.include_router(tasks_router)
app.include_router(runs_router)
app.include_router(cases_router)
app.include_router(stats_router)
app.include_router(case_sets_router)
app.include_router(alerts_router)

# ── Dashboard 静态页面 ──────────────────────────────────────────
_dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"
_dashboard_dir.mkdir(exist_ok=True)
app.mount("/dashboard", StaticFiles(directory=str(_dashboard_dir), html=True), name="dashboard")


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
