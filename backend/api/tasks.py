"""评测任务 CRUD API。"""

import uuid
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from backend.case_set_results.policy import PassPolicy
from backend.case_set_results.service import CaseSetResultService
from backend.core.database import get_db
from backend.core.models import (
    CaseSetEvalResult,
    EvalTask,
    CaseSet,
    EvalCase,
    EvalRun,
    CaseSetMember,
)

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


# ═══════════════════════════════════════════════════════════════
# 评测触发（Brain tool 用）
# ═══════════════════════════════════════════════════════════════


class TriggerEvalRequest(BaseModel):
    """评测触发请求（对应 Brain trigger_evaluation tool）。"""
    agent_version: str
    case_set_name: Optional[str] = None
    layers: Optional[List[str]] = None
    pass_policy: Optional[PassPolicy] = None


class TriggerEvalResponse(BaseModel):
    task_id: str
    agent_version: str
    case_set_name: str
    total_cases: int
    total_runs: int
    layers: List[str]
    pass_policy: dict


@router.post("/trigger", response_model=TriggerEvalResponse, status_code=201)
async def trigger_evaluation(req: TriggerEvalRequest, db: AsyncSession = Depends(get_db)):
    """触发一次完整评测：创建 EvalTask + EvalRuns。

    对应 Brain tool: ``trigger_evaluation``
    """
    layers = req.layers or ["intent", "retrieval", "tool", "generation", "outcome"]
    pass_policy = req.pass_policy or PassPolicy()

    # 查找 CaseSet
    case_set = None
    if req.case_set_name:
        stmt = select(CaseSet).where(CaseSet.name.ilike(f"%{req.case_set_name}%")).limit(1)
        result = await db.execute(stmt)
        case_set = result.scalars().first()
        if not case_set:
            raise HTTPException(404, f"未找到测试集: {req.case_set_name}")

    # 获取用例
    case_ids = []
    if case_set:
        case_stmt = select(EvalCase.id).where(
            EvalCase.id.in_(
                select(CaseSetMember.case_id).where(CaseSetMember.case_set_id == case_set.id)
            )
        )
        case_result = await db.execute(case_stmt)
        case_ids = [row[0] for row in case_result.fetchall()]

    if not case_ids:
        case_stmt = (
            select(EvalCase.id)
            .where(EvalCase.is_active.is_(True))
            .limit(10)
        )
        case_result = await db.execute(case_stmt)
        case_ids = [row[0] for row in case_result.fetchall()]

    if not case_ids:
        raise HTTPException(400, "没有可用的评测用例，请先创建用例")

    # 创建 EvalTask
    task = EvalTask(
        name=f"手动评测 - {req.agent_version} - {datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
        agent_version=req.agent_version,
        case_set_id=case_set.id if case_set else None,
        status="pending",
        total_cases=len(case_ids),
        config={
            "layers": layers,
            "trigger": "manual_im",
            "pass_policy": pass_policy.to_config(),
            "total_runs": len(case_ids) * pass_policy.k,
        },
    )
    db.add(task)
    await db.flush()

    # 创建 EvalRuns
    for case_id in case_ids:
        for attempt_index in range(1, pass_policy.k + 1):
            run = EvalRun(
                task_id=task.id,
                eval_case_id=case_id,
                agent_version=req.agent_version,
                attempt_index=attempt_index,
                status="pending",
            )
            db.add(run)

    await db.commit()

    return TriggerEvalResponse(
        task_id=str(task.id),
        agent_version=req.agent_version,
        case_set_name=req.case_set_name or "默认",
        total_cases=len(case_ids),
        total_runs=len(case_ids) * pass_policy.k,
        layers=layers,
        pass_policy=pass_policy.to_config(),
    )


def _case_set_result_to_dict(result: CaseSetEvalResult, include_cases: bool = False) -> dict:
    data = {
        "id": str(result.id),
        "task_id": str(result.task_id),
        "case_set_id": str(result.case_set_id) if result.case_set_id else None,
        "agent_version": result.agent_version,
        "formula": result.formula,
        "k": result.k,
        "score_threshold": float(result.score_threshold),
        "power_threshold": float(result.power_threshold),
        "min_case_pass_rate": float(result.min_case_pass_rate),
        "status": result.status,
        "passed": result.passed,
        "total_cases": result.total_cases,
        "passed_cases": result.passed_cases,
        "failed_cases": result.failed_cases,
        "insufficient_cases": result.insufficient_cases,
        "case_pass_rate": float(result.case_pass_rate),
        "attempt_pass_rate": float(result.attempt_pass_rate),
        "metrics": result.metrics or {},
        "computed_at": result.computed_at.isoformat() if result.computed_at else None,
        "error_message": result.error_message,
    }
    if include_cases:
        data["cases"] = [
            {
                "id": str(item.id),
                "eval_case_id": str(item.eval_case_id),
                "passed": item.passed,
                "attempt_count": item.attempt_count,
                "completed_attempts": item.completed_attempts,
                "passed_attempts": item.passed_attempts,
                "required_passes": item.required_passes,
                "best_score": float(item.best_score) if item.best_score is not None else None,
                "avg_score": float(item.avg_score) if item.avg_score is not None else None,
                "attempts": item.attempts or [],
                "failure_reason": item.failure_reason,
            }
            for item in result.case_results
        ]
    return data


@router.get("/{task_id}/result")
async def get_task_case_set_result(
    task_id: str,
    include_cases: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """查询评测集 Pass 结果。"""
    stmt = select(CaseSetEvalResult).where(CaseSetEvalResult.task_id == uuid.UUID(task_id))
    if include_cases:
        stmt = stmt.options(selectinload(CaseSetEvalResult.case_results))
    result = (await db.execute(stmt)).scalar_one_or_none()
    if not result:
        raise HTTPException(status_code=404, detail="CaseSet eval result not found")
    return _case_set_result_to_dict(result, include_cases=include_cases)


@router.post("/{task_id}/result/recompute")
async def recompute_task_case_set_result(
    task_id: str,
    include_cases: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """手动幂等重算评测集 Pass 结果。"""
    service = CaseSetResultService(db)
    try:
        result = await service.recompute_for_task(uuid.UUID(task_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()

    stmt = select(CaseSetEvalResult).where(CaseSetEvalResult.id == result.id)
    if include_cases:
        stmt = stmt.options(selectinload(CaseSetEvalResult.case_results))
    result = (await db.execute(stmt)).scalar_one()
    return _case_set_result_to_dict(result, include_cases=include_cases)
