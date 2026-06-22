"""评测运行记录 API。"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.core.models import EvalRun, EvalScore

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunResponse(BaseModel):
    id: str
    task_id: str
    eval_case_id: str
    agent_version: str
    status: str
    trace_id: Optional[str]
    error_message: Optional[str]
    retry_count: int
    created_at: datetime
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class RunDetailResponse(RunResponse):
    expected_snapshot: Optional[dict] = None
    scores: Optional[list] = None


@router.get("", response_model=list[RunResponse])
async def list_runs(
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """查询评测运行记录列表。"""
    stmt = select(EvalRun)
    if task_id:
        stmt = stmt.where(EvalRun.task_id == uuid.UUID(task_id))
    if status:
        stmt = stmt.where(EvalRun.status == status)
    stmt = stmt.order_by(EvalRun.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    runs = result.scalars().all()

    return [
        RunResponse(
            id=str(r.id),
            task_id=str(r.task_id),
            eval_case_id=str(r.eval_case_id),
            agent_version=r.agent_version,
            status=r.status,
            trace_id=str(r.trace_id) if r.trace_id else None,
            error_message=r.error_message,
            retry_count=r.retry_count,
            created_at=r.created_at,
            completed_at=r.completed_at,
        )
        for r in runs
    ]


@router.get("/{run_id}", response_model=RunDetailResponse)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """获取单条运行记录详情（含评测得分）。"""
    run = await db.get(EvalRun, uuid.UUID(run_id))
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # 查询关联的评测得分（通过 eval_run_id 精准关联，而非 trace_id）
    scores = None
    if run.trace_id:
        result = await db.execute(
            select(EvalScore).where(EvalScore.eval_run_id == run.id)
        )
        eval_scores = result.scalars().all()
        scores = [
            {
                "id": str(s.id),
                "span_id": str(s.span_id) if s.span_id else None,
                "score": float(s.score),
                "metrics": s.metrics,
                "method": s.method,
                "evaluator_version": s.evaluator_version,
            }
            for s in eval_scores
        ]

    return RunDetailResponse(
        id=str(run.id),
        task_id=str(run.task_id),
        eval_case_id=str(run.eval_case_id),
        agent_version=run.agent_version,
        status=run.status,
        trace_id=str(run.trace_id) if run.trace_id else None,
        error_message=run.error_message,
        retry_count=run.retry_count,
        created_at=run.created_at,
        completed_at=run.completed_at,
        expected_snapshot=run.expected_snapshot,
        scores=scores,
    )
