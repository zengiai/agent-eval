"""测试用例集 API。

包含 AgentBrain 查询使用的用例集列表，以及 Dashboard 批量输入 question
生成 CaseSet 的后台任务入口。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.brain import _call_runtime_bridge
from backend.core.config import settings
from backend.core.database import async_session_factory, get_db
from backend.core.models import CaseSet, CaseSetMember, EvalCase, EvalTask, Span, Trace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/case-sets", tags=["case-sets"])

MAX_BATCH_QUESTIONS = 50
MAX_BATCH_CONCURRENCY = 10
DEFAULT_TRACE_WAIT_SECONDS = 3.0

# Tests may replace this with a test DB session factory. Production uses the
# normal application session factory because background tasks run outside the
# FastAPI request dependency scope.
_batch_session_factory = async_session_factory


class CaseSetCreateRequest(BaseModel):
    """创建测试集请求。"""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=100)
    version: str = Field(default="1.0.0", max_length=50)
    tags: list[str] = Field(default_factory=list)


class CaseSetBatchFromQuestionsRequest(BaseModel):
    """批量 questions 生成 CaseSet 请求。"""

    case_set_id: Optional[str] = None
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=100)
    version: str = Field(default="1.0.0", max_length=50)
    tags: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    questions_text: Optional[str] = None
    agent_version: Optional[str] = Field(default=None, max_length=100)
    concurrency: int = Field(default=3, ge=1, le=MAX_BATCH_CONCURRENCY)
    difficulty: str = Field(default="medium", max_length=20)
    trace_wait_seconds: float = Field(default=DEFAULT_TRACE_WAIT_SECONDS, ge=0, le=15)


def _short_id(value: uuid.UUID) -> str:
    return str(value)[:8]


def _clean_tags(tags: Optional[list[str]]) -> list[str]:
    return [item.strip() for item in (tags or []) if item and item.strip()]


def _case_set_to_dict(cs: CaseSet) -> dict[str, Any]:
    return {
        "id": _short_id(cs.id),
        "full_id": str(cs.id),
        "name": cs.name,
        "description": cs.description or "",
        "category": cs.category or "",
        "case_count": cs.case_count,
        "version": cs.version,
        "tags": cs.tags or [],
        "metadata": cs.metadata_ or {},
        "created_at": cs.created_at.isoformat() if cs.created_at else "",
        "updated_at": cs.updated_at.isoformat() if cs.updated_at else "",
    }


def _normalize_questions(req: CaseSetBatchFromQuestionsRequest) -> list[str]:
    raw_questions: list[str] = []
    raw_questions.extend(req.questions or [])
    if req.questions_text:
        raw_questions.extend(req.questions_text.splitlines())

    questions = [item.strip() for item in raw_questions if item and item.strip()]
    if not questions:
        raise HTTPException(status_code=400, detail="questions 不能为空")
    if len(questions) > MAX_BATCH_QUESTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"单批最多支持 {MAX_BATCH_QUESTIONS} 条 question",
        )

    too_long = next((item for item in questions if len(item) > 4000), None)
    if too_long:
        raise HTTPException(status_code=400, detail="单条 question 不能超过 4000 字符")
    return questions


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"非法 {field_name}") from exc


async def _get_or_create_case_set(
    db: AsyncSession,
    req: CaseSetBatchFromQuestionsRequest,
) -> CaseSet:
    if req.case_set_id:
        case_set = await db.get(CaseSet, _parse_uuid(req.case_set_id, "case_set_id"))
        if not case_set:
            raise HTTPException(status_code=404, detail="CaseSet not found")
        return case_set

    name = (req.name or "").strip()
    if not name:
        name = f"case-set-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"

    existing = (
        await db.execute(select(CaseSet).where(CaseSet.name == name).limit(1))
    ).scalar_one_or_none()
    if existing:
        return existing

    case_set = CaseSet(
        name=name,
        description=req.description,
        category=req.category,
        version=req.version or "1.0.0",
        tags=_clean_tags(req.tags),
        case_count=0,
        metadata_={"created_by": "dashboard_batch"},
    )
    db.add(case_set)
    await db.flush()
    return case_set


@router.get("")
async def list_case_sets(
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """列出测试用例集。

    对应 Brain tool: ``list_case_sets``。保留原 `id` 短 ID，同时新增
    `full_id` 供 Dashboard 后续操作使用。
    """
    stmt = select(CaseSet).order_by(CaseSet.name)

    if category:
        stmt = stmt.where(CaseSet.category == category)
    if search:
        stmt = stmt.where(CaseSet.name.ilike(f"%{search}%"))

    result = await db.execute(stmt)
    case_sets = result.scalars().all()
    sets_list = [_case_set_to_dict(cs) for cs in case_sets]
    return {"case_sets": sets_list, "total": len(sets_list)}


@router.post("", status_code=201)
async def create_case_set(
    req: CaseSetCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """创建测试集。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    existing = (
        await db.execute(select(CaseSet).where(CaseSet.name == name).limit(1))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"CaseSet 已存在: {name}")

    case_set = CaseSet(
        name=name,
        description=req.description,
        category=req.category,
        version=req.version or "1.0.0",
        tags=_clean_tags(req.tags),
        case_count=0,
        metadata_={"created_by": "dashboard"},
    )
    db.add(case_set)
    await db.commit()
    await db.refresh(case_set)
    return _case_set_to_dict(case_set)


@router.post("/batch-from-questions", status_code=202)
async def create_case_set_batch_from_questions(
    req: CaseSetBatchFromQuestionsRequest,
    db: AsyncSession = Depends(get_db),
):
    """批量执行 questions，生成 Trace-backed Cases 并归属到 CaseSet。"""
    questions = _normalize_questions(req)
    case_set = await _get_or_create_case_set(db, req)
    agent_version = (req.agent_version or settings.AGENT_VERSION or "0.0.0").strip() or "0.0.0"
    concurrency = min(max(req.concurrency, 1), MAX_BATCH_CONCURRENCY)

    task = EvalTask(
        name=f"CaseSet批量对话-{case_set.name[:30]}-{datetime.utcnow().strftime('%Y%m%d_%H%M')}",
        agent_version=agent_version,
        case_set_id=case_set.id,
        status="running",
        total_cases=len(questions),
        completed_cases=0,
        failed_cases=0,
        config={
            "type": "caseset_batch_dialog",
            "question_count": len(questions),
            "concurrency": concurrency,
            "difficulty": req.difficulty,
            "trace_wait_seconds": req.trace_wait_seconds,
            "source": "dashboard",
        },
        summary_metrics={"item_results": []},
        created_by="dashboard",
        started_at=datetime.utcnow(),
    )
    db.add(task)
    await db.flush()

    metadata = dict(case_set.metadata_ or {})
    metadata["last_batch_task_id"] = str(task.id)
    metadata["last_batch_started_at"] = datetime.utcnow().isoformat()
    case_set.metadata_ = metadata

    await db.commit()

    asyncio.create_task(
        _run_case_set_batch_task(
            task_id=str(task.id),
            case_set_id=str(case_set.id),
            questions=questions,
            agent_version=agent_version,
            concurrency=concurrency,
            difficulty=req.difficulty,
            category=req.category or case_set.category,
            tags=_clean_tags(req.tags) or (case_set.tags or []),
            trace_wait_seconds=req.trace_wait_seconds,
        )
    )

    return {
        "task_id": str(task.id),
        "case_set_id": str(case_set.id),
        "status": "running",
        "total_questions": len(questions),
        "concurrency": concurrency,
    }


@router.get("/batch-tasks/{task_id}")
async def get_case_set_batch_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """查询 CaseSet 批量生成任务状态。"""
    task = await db.get(EvalTask, _parse_uuid(task_id, "task_id"))
    if not task:
        raise HTTPException(status_code=404, detail="Batch task not found")
    if not isinstance(task.config, dict) or task.config.get("type") != "caseset_batch_dialog":
        raise HTTPException(status_code=404, detail="Batch task not found")
    return _task_to_batch_response(task)


@router.get("/{case_set_id}")
async def get_case_set_detail(
    case_set_id: str,
    db: AsyncSession = Depends(get_db),
):
    """获取测试集详情及成员 Case 列表。"""
    case_set = await db.get(CaseSet, _parse_uuid(case_set_id, "case_set_id"))
    if not case_set:
        raise HTTPException(status_code=404, detail="CaseSet not found")

    cases_r = await db.execute(
        select(EvalCase)
        .join(CaseSetMember, CaseSetMember.case_id == EvalCase.id)
        .where(CaseSetMember.case_set_id == case_set.id)
        .order_by(EvalCase.created_at.desc())
    )
    cases = cases_r.scalars().all()

    task_candidates = (
        await db.execute(
            select(EvalTask)
            .where(EvalTask.case_set_id == case_set.id)
            .order_by(EvalTask.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    latest_task = next(
        (
            task
            for task in task_candidates
            if isinstance(task.config, dict)
            and task.config.get("type") == "caseset_batch_dialog"
        ),
        None,
    )

    return {
        "case_set": _case_set_to_dict(case_set),
        "latest_batch_task": _task_to_batch_response(latest_task) if latest_task else None,
        "cases": [
            {
                "id": str(case.id),
                "query": case.query,
                "source": case.source,
                "source_trace_id": str(case.source_trace_id) if case.source_trace_id else None,
                "difficulty": case.difficulty,
                "category": case.category,
                "run_count": case.run_count,
                "last_avg_score": float(case.last_avg_score) if case.last_avg_score is not None else None,
                "health_status": case.health_status,
                "created_at": case.created_at.isoformat() if case.created_at else "",
            }
            for case in cases
        ],
    }


def _task_to_batch_response(task: EvalTask) -> dict[str, Any]:
    total = task.total_cases or 0
    done = (task.completed_cases or 0) + (task.failed_cases or 0)
    return {
        "task_id": str(task.id),
        "case_set_id": str(task.case_set_id) if task.case_set_id else None,
        "status": task.status,
        "agent_version": task.agent_version,
        "total_cases": total,
        "completed_cases": task.completed_cases,
        "failed_cases": task.failed_cases,
        "processed_cases": done,
        "progress": round((done / total) * 100, 1) if total else 100.0,
        "config": task.config or {},
        "summary_metrics": task.summary_metrics or {},
        "created_at": task.created_at.isoformat() if task.created_at else "",
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


async def _run_case_set_batch_task(
    *,
    task_id: str,
    case_set_id: str,
    questions: list[str],
    agent_version: str,
    concurrency: int,
    difficulty: str,
    category: Optional[str],
    tags: list[str],
    trace_wait_seconds: float,
) -> None:
    """后台执行批量 question 对话并转 Case。"""
    logger.info("CaseSet batch task started: task=%s case_set=%s count=%d", task_id, case_set_id, len(questions))
    semaphore = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []
    try:
        async def _run_one(index: int, question: str) -> dict[str, Any]:
            async with semaphore:
                result = await _execute_question_to_case(
                    task_id=task_id,
                    case_set_id=case_set_id,
                    index=index,
                    question=question,
                    agent_version=agent_version,
                    difficulty=difficulty,
                    category=category,
                    tags=tags,
                    trace_wait_seconds=trace_wait_seconds,
                )
                await _record_item_progress(task_id, success=result["status"] == "completed")
                return result

        results = await asyncio.gather(
            *[_run_one(index, question) for index, question in enumerate(questions, start=1)]
        )
    except Exception as exc:  # pragma: no cover - defensive guard for unexpected task-level failures.
        logger.exception("CaseSet batch task crashed: task=%s", task_id)
        results.append({"status": "failed", "error": f"batch crashed: {type(exc).__name__}"})
    finally:
        await _finalize_batch_task(task_id, case_set_id, results)


async def _execute_question_to_case(
    *,
    task_id: str,
    case_set_id: str,
    index: int,
    question: str,
    agent_version: str,
    difficulty: str,
    category: Optional[str],
    tags: list[str],
    trace_wait_seconds: float,
) -> dict[str, Any]:
    session_id = f"caseset-{uuid.UUID(task_id).hex[:12]}-{index}"
    source_ref = f"caseset_batch:{task_id}:{index}"
    started_at = datetime.utcnow() - timedelta(seconds=1)
    started_perf = time.perf_counter()

    try:
        runtime_resp = await _call_runtime_bridge(
            "POST",
            "/api/brain/chat",
            json={
                "message": question,
                "session_id": session_id,
                "user_id": "case-set-batch",
                "username": "case-set-batch",
            },
        )
        reply_html = str(runtime_resp.get("reply_html") or "").strip()
        if not reply_html:
            raise ValueError("runtime 返回空回复")

        latency_ms = int((time.perf_counter() - started_perf) * 1000)
        trace_id = await _find_recent_trace_id(
            session_id=session_id,
            source_ref=source_ref,
            started_at=started_at,
            wait_seconds=trace_wait_seconds,
        )

        async with _batch_session_factory() as session:
            trace_origin = "runtime_collected"
            if trace_id:
                trace = await session.get(Trace, uuid.UUID(trace_id))
                if not trace:
                    trace = await _create_wrapper_trace(
                        session=session,
                        trace_id=None,
                        question=question,
                        reply_html=reply_html,
                        agent_version=agent_version,
                        session_id=session_id,
                        source_ref=source_ref,
                        latency_ms=latency_ms,
                    )
                    trace_origin = "dashboard_case_set_batch_wrapper"
            else:
                trace = await _create_wrapper_trace(
                    session=session,
                    trace_id=None,
                    question=question,
                    reply_html=reply_html,
                    agent_version=agent_version,
                    session_id=session_id,
                    source_ref=source_ref,
                    latency_ms=latency_ms,
                )
                trace_origin = "dashboard_case_set_batch_wrapper"

            batch_metadata = {
                "batch_task_id": task_id,
                "batch_item_index": index,
                "session_id": session_id,
                "source_ref": source_ref,
                "trace_origin": trace_origin,
            }
            await _attach_batch_metadata_to_trace(
                session=session,
                trace=trace,
                case_set_id=uuid.UUID(case_set_id),
                batch_metadata=batch_metadata,
            )

            case = await _create_or_reuse_case_from_trace(
                session=session,
                trace=trace,
                case_set_id=uuid.UUID(case_set_id),
                difficulty=difficulty,
                category=category,
                tags=tags,
                batch_metadata=batch_metadata,
            )
            await session.commit()

        return {
            "index": index,
            "question": question,
            "status": "completed",
            "case_id": str(case.id),
            "trace_id": str(trace.id),
            "trace_origin": trace_origin,
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        logger.warning("CaseSet batch item failed: task=%s index=%d error=%s", task_id, index, exc)
        return {
            "index": index,
            "question": question,
            "status": "failed",
            "error": str(exc)[:500],
        }


async def _find_recent_trace_id(
    *,
    session_id: str,
    source_ref: str,
    started_at: datetime,
    wait_seconds: float,
) -> Optional[str]:
    """等待 Ingest 写入 runtime 采集 Trace，并返回 Trace ID。"""
    deadline = time.perf_counter() + wait_seconds
    while True:
        async with _batch_session_factory() as session:
            trace = (
                await session.execute(
                    select(Trace)
                    .where(
                        Trace.created_at >= started_at,
                        ((Trace.session_id == session_id) | (Trace.source_ref == source_ref)),
                        Trace.final_response.is_not(None),
                    )
                    .order_by(Trace.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if trace:
                return str(trace.id)

        if time.perf_counter() >= deadline:
            return None
        await asyncio.sleep(0.25)


async def _create_wrapper_trace(
    *,
    session: AsyncSession,
    trace_id: Optional[uuid.UUID],
    question: str,
    reply_html: str,
    agent_version: str,
    session_id: str,
    source_ref: str,
    latency_ms: int,
) -> Trace:
    """创建显式标记的包装 Trace，兜底保持 Trace -> Case 一致模型。"""
    trace = Trace(
        id=trace_id or uuid.uuid4(),
        agent_version=agent_version,
        session_id=session_id,
        query=question,
        context={
            "source": "dashboard_case_set_batch",
            "source_ref": source_ref,
            "trace_origin": "dashboard_case_set_batch_wrapper",
        },
        final_response=reply_html,
        status="success",
        source="eval",
        source_ref=source_ref,
        total_latency_ms=latency_ms,
        total_tokens={},
    )
    session.add(trace)
    await session.flush()

    session.add(
        Span(
            trace_id=trace.id,
            span_type="generation",
            sequence=1,
            input={"query": question},
            output={"response": reply_html},
            latency_ms=latency_ms,
            tokens={},
            model="agent_runtime",
            metadata_={"trace_origin": "dashboard_case_set_batch_wrapper"},
        )
    )
    await session.flush()
    return trace


async def _attach_batch_metadata_to_trace(
    *,
    session: AsyncSession,
    trace: Trace,
    case_set_id: uuid.UUID,
    batch_metadata: dict[str, Any],
) -> None:
    """在 Trace 上补充 CaseSet batch 审计信息，不覆盖既有 runtime 来源。"""
    context = dict(trace.context or {})
    case_set_batch = dict(context.get("case_set_batch") or {})
    case_set_batch.update(
        {
            "case_set_id": str(case_set_id),
            **batch_metadata,
        }
    )
    context["case_set_batch"] = case_set_batch
    context.setdefault("source", "dashboard_case_set_batch")
    context.setdefault("trace_origin", batch_metadata.get("trace_origin"))
    context.setdefault("source_ref", batch_metadata.get("source_ref"))
    context.setdefault("session_id", batch_metadata.get("session_id"))
    trace.context = context
    if not trace.source_ref:
        trace.source_ref = batch_metadata.get("source_ref")
    if not trace.session_id:
        trace.session_id = batch_metadata.get("session_id")
    await session.flush()


async def _create_or_reuse_case_from_trace(
    *,
    session: AsyncSession,
    trace: Trace,
    case_set_id: uuid.UUID,
    difficulty: str,
    category: Optional[str],
    tags: list[str],
    batch_metadata: dict[str, Any],
) -> EvalCase:
    existing = (
        await session.execute(
            select(EvalCase).where(EvalCase.source_trace_id == trace.id).limit(1)
        )
    ).scalar_one_or_none()

    if existing:
        case = existing
    else:
        spans = (
            await session.execute(
                select(Span).where(Span.trace_id == trace.id).order_by(Span.sequence)
            )
        ).scalars().all()
        spans_summary = [
            {
                "span_type": span.span_type,
                "sequence": span.sequence,
                "tool_name": span.tool_name,
                "latency_ms": span.latency_ms,
                "model": span.model,
                "tool_status": span.tool_status,
            }
            for span in spans
        ]
        metadata = {
            "trace_id": str(trace.id),
            "final_response": trace.final_response,
            "total_latency_ms": trace.total_latency_ms,
            "total_tokens": trace.total_tokens,
            "spans_summary": spans_summary,
            "snapshot_at": datetime.utcnow().isoformat(),
            "case_set_batch": batch_metadata,
        }
        case = EvalCase(
            query=trace.query,
            context=trace.context or {},
            source="trace",
            source_trace_id=trace.id,
            difficulty=difficulty,
            category=category,
            tags=tags,
            metadata_=metadata,
            review_status="pending",
        )
        session.add(case)
        await session.flush()

    member = (
        await session.execute(
            select(CaseSetMember).where(
                CaseSetMember.case_set_id == case_set_id,
                CaseSetMember.case_id == case.id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        session.add(CaseSetMember(case_set_id=case_set_id, case_id=case.id))
        await session.flush()
    return case


async def _record_item_progress(task_id: str, *, success: bool) -> None:
    async with _batch_session_factory() as session:
        values = (
            {"completed_cases": EvalTask.completed_cases + 1}
            if success
            else {"failed_cases": EvalTask.failed_cases + 1}
        )
        await session.execute(
            update(EvalTask)
            .where(EvalTask.id == uuid.UUID(task_id))
            .values(**values)
        )
        await session.commit()


async def _finalize_batch_task(
    task_id: str,
    case_set_id: str,
    results: list[dict[str, Any]],
) -> None:
    success_count = sum(1 for item in results if item.get("status") == "completed")
    failed_count = len(results) - success_count
    final_status = "completed" if success_count > 0 else "failed"

    async with _batch_session_factory() as session:
        case_count = (
            await session.execute(
                select(func.count()).where(CaseSetMember.case_set_id == uuid.UUID(case_set_id))
            )
        ).scalar() or 0

        await session.execute(
            update(CaseSet)
            .where(CaseSet.id == uuid.UUID(case_set_id))
            .values(case_count=case_count, updated_at=datetime.utcnow())
        )
        await session.execute(
            update(EvalTask)
            .where(EvalTask.id == uuid.UUID(task_id))
            .values(
                status=final_status,
                completed_cases=success_count,
                failed_cases=failed_count,
                completed_at=datetime.utcnow(),
                summary_metrics={
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "item_results": results,
                },
            )
        )
        await session.commit()
    logger.info(
        "CaseSet batch task finished: task=%s status=%s success=%d failed=%d",
        task_id,
        final_status,
        success_count,
        failed_count,
    )
