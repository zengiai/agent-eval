"""评测任务 CRUD API。"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.core.models import EvalTask, CaseSet, EvalCase

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreateRequest(BaseModel):
    name: str
    agent_version: str
    case_set_id: str
    config: Optional[dict] = None
    created_by: Optional[str] = None


class TaskResponse(BaseModel):
    id: str
    name: str
    agent_version: str
    case_set_id: Optional[str]
    status: str
    total_cases: int
    completed_cases: int
    failed_cases: int
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("", response_model=TaskResponse)
async def create_task(req: TaskCreateRequest, db: AsyncSession = Depends(get_db)):
    """创建评测任务。"""
    # 验证 case_set 存在
    case_set = await db.get(CaseSet, uuid.UUID(req.case_set_id))
    if not case_set:
        raise HTTPException(status_code=404, detail="CaseSet not found")

    # 计算 case 数量
    from sqlalchemy import func
    from backend.core.models import CaseSetMember
    count_result = await db.execute(
        select(func.count()).where(CaseSetMember.case_set_id == uuid.UUID(req.case_set_id))
    )
    case_count = count_result.scalar() or 0

    task = EvalTask(
        name=req.name,
        agent_version=req.agent_version,
        case_set_id=uuid.UUID(req.case_set_id),
        total_cases=case_count,
        config=req.config or {},
        created_by=req.created_by,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    return TaskResponse(
        id=str(task.id),
        name=task.name,
        agent_version=task.agent_version,
        case_set_id=str(task.case_set_id) if task.case_set_id else None,
        status=task.status,
        total_cases=task.total_cases,
        completed_cases=task.completed_cases,
        failed_cases=task.failed_cases,
        created_at=task.created_at,
    )


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    agent_version: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """查询评测任务列表。"""
    stmt = select(EvalTask)
    if agent_version:
        stmt = stmt.where(EvalTask.agent_version == agent_version)
    if status:
        stmt = stmt.where(EvalTask.status == status)
    stmt = stmt.order_by(EvalTask.created_at.desc()).limit(limit)

    result = await db.execute(stmt)
    tasks = result.scalars().all()

    return [
        TaskResponse(
            id=str(t.id),
            name=t.name,
            agent_version=t.agent_version,
            case_set_id=str(t.case_set_id) if t.case_set_id else None,
            status=t.status,
            total_cases=t.total_cases,
            completed_cases=t.completed_cases,
            failed_cases=t.failed_cases,
            created_at=t.created_at,
        )
        for t in tasks
    ]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """获取单个评测任务详情。"""
    task = await db.get(EvalTask, uuid.UUID(task_id))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return TaskResponse(
        id=str(task.id),
        name=task.name,
        agent_version=task.agent_version,
        case_set_id=str(task.case_set_id) if task.case_set_id else None,
        status=task.status,
        total_cases=task.total_cases,
        completed_cases=task.completed_cases,
        failed_cases=task.failed_cases,
        created_at=task.created_at,
    )
