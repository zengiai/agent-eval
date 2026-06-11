"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.ingest import router as ingest_router
from backend.api.tasks import router as tasks_router
from backend.api.runs import router as runs_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。"""
    # 启动时：可在这里启动 Ingest 消费者
    yield
    # 关闭时：清理资源


app = FastAPI(
    title="Agent Eval API",
    description="Agent 调用链路自动评测系统",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(ingest_router)
app.include_router(tasks_router)
app.include_router(runs_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
