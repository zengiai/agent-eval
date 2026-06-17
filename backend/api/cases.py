"""评测用例 CRUD API + Trace→Case + 单 Case 评分。"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update, delete
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.core.models import (
    EvalCase, CaseSet, CaseSetMember, Trace, Span, EvalRun, EvalTask, EvalScore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cases", tags=["cases"])


# ═══════════════════════════════════════════════════════════════
# 请求/响应模型
# ═══════════════════════════════════════════════════════════════

class CaseCreateRequest(BaseModel):
    query: str
    context: Optional[dict] = {}
    expected_intent: Optional[dict] = {}
    expected_retrieval: Optional[dict] = {}
    expected_tools: Optional[list] = []
    expected_answer: Optional[dict] = {}
    gold_answer: Optional[str] = None
    source: str = "manual"
    difficulty: str = "medium"
    category: Optional[str] = None
    tags: Optional[List[str]] = []
    case_set_ids: Optional[List[str]] = None  # 可选加入的测试集


class CaseResponse(BaseModel):
    id: str
    query: str
    source: str
    source_trace_id: Optional[str] = None
    difficulty: str
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    gold_answer: Optional[str] = None
    expected_intent: Optional[dict] = None
    expected_retrieval: Optional[dict] = None
    expected_tools: Optional[list] = None
    expected_answer: Optional[dict] = None
    review_status: str
    run_count: int
    last_avg_score: Optional[float] = None
    health_status: str
    is_active: bool
    latest_run_status: Optional[str] = None
    latest_run_id: Optional[str] = None
    metadata_: Optional[dict] = Field(None, alias="metadata")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class CaseListResponse(BaseModel):
    total: int
    items: List[CaseResponse]


class TraceToCaseRequest(BaseModel):
    """Trace 转 Case 时的补充参数。"""
    difficulty: str = "medium"
    category: Optional[str] = None
    tags: Optional[List[str]] = []
    case_set_ids: Optional[List[str]] = None


class EvaluateCaseResponse(BaseModel):
    case_id: str
    trace_id: str
    overall_score: Optional[float] = None
    layers: Optional[dict] = None
    error: Optional[str] = None


class EvaluateCaseAsyncResponse(BaseModel):
    case_id: str
    run_id: str
    task_id: str
    trace_id: str
    status: str  # "running"


class TraceBrief(BaseModel):
    """Trace 简要信息，用于 Dashboard 列表。"""
    id: str
    agent_version: str
    query: str
    source: str
    status: str
    overall_score: Optional[float] = None
    total_latency_ms: Optional[int] = None
    created_at: datetime
    already_case: bool = False  # 是否已转为 Case
    case_id: Optional[str] = None  # 已转 Case 时返回对应 Case ID


class TraceListResponse(BaseModel):
    total: int
    items: List[TraceBrief]


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _case_to_response(
    c: EvalCase,
    latest_run_status: Optional[str] = None,
    latest_run_id: Optional[str] = None,
) -> CaseResponse:
    return CaseResponse(
        id=str(c.id),
        query=c.query,
        source=c.source,
        source_trace_id=str(c.source_trace_id) if c.source_trace_id else None,
        difficulty=c.difficulty,
        category=c.category,
        tags=c.tags,
        gold_answer=c.gold_answer,
        expected_intent=c.expected_intent,
        expected_retrieval=c.expected_retrieval,
        expected_tools=c.expected_tools,
        expected_answer=c.expected_answer,
        review_status=c.review_status,
        run_count=c.run_count,
        last_avg_score=float(c.last_avg_score) if c.last_avg_score else None,
        health_status=c.health_status,
        is_active=c.is_active,
        latest_run_status=latest_run_status,
        latest_run_id=latest_run_id,
        metadata=c.metadata_,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


# ═══════════════════════════════════════════════════════════════
# Case CRUD
# ═══════════════════════════════════════════════════════════════

@router.get("", response_model=CaseListResponse)
async def list_cases(
    source: Optional[str] = None,
    category: Optional[str] = None,
    difficulty: Optional[str] = None,
    review_status: Optional[str] = None,
    health_status: Optional[str] = None,
    search: Optional[str] = Query(None, description="搜索 query 关键词"),
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """查询评测用例列表，支持多条件筛选。"""
    stmt = select(EvalCase)

    if source:
        stmt = stmt.where(EvalCase.source == source)
    if category:
        stmt = stmt.where(EvalCase.category == category)
    if difficulty:
        stmt = stmt.where(EvalCase.difficulty == difficulty)
    if review_status:
        stmt = stmt.where(EvalCase.review_status == review_status)
    if health_status:
        stmt = stmt.where(EvalCase.health_status == health_status)
    if search:
        stmt = stmt.where(EvalCase.query.ilike(f"%{search}%"))

    # 计数
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    # 分页
    stmt = stmt.order_by(EvalCase.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    cases = result.scalars().all()

    # 批量查询每个 Case 的最新 EvalRun 状态
    case_ids = [c.id for c in cases]
    run_status_map: dict = {}  # case_id → (status, run_id)
    if case_ids:
        # 子查询取每个 eval_case_id 的最新 created_at，再 JOIN 回主表
        latest_run_sub = (
            select(
                EvalRun.eval_case_id,
                func.max(EvalRun.created_at).label("max_created_at"),
            )
            .where(EvalRun.eval_case_id.in_(case_ids))
            .group_by(EvalRun.eval_case_id)
            .subquery()
        )
        latest_run_stmt = (
            select(EvalRun.eval_case_id, EvalRun.status, EvalRun.id)
            .join(
                latest_run_sub,
                (EvalRun.eval_case_id == latest_run_sub.c.eval_case_id)
                & (EvalRun.created_at == latest_run_sub.c.max_created_at),
            )
        )
        run_result = await db.execute(latest_run_stmt)
        for row in run_result.all():
            run_status_map[row[0]] = (row[1], str(row[2]))

    return CaseListResponse(
        total=total,
        items=[
            _case_to_response(
                c,
                latest_run_status=run_status_map.get(c.id, (None, None))[0],
                latest_run_id=run_status_map.get(c.id, (None, None))[1],
            )
            for c in cases
        ],
    )


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(case_id: str, db: AsyncSession = Depends(get_db)):
    """获取单个用例详情。"""
    case = await db.get(EvalCase, uuid.UUID(case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return _case_to_response(case)


@router.post("", response_model=CaseResponse, status_code=201)
async def create_case(req: CaseCreateRequest, db: AsyncSession = Depends(get_db)):
    """手动创建评测用例。"""
    case = EvalCase(
        query=req.query,
        context=req.context or {},
        expected_intent=req.expected_intent,
        expected_retrieval=req.expected_retrieval,
        expected_tools=req.expected_tools or [],
        expected_answer=req.expected_answer,
        gold_answer=req.gold_answer,
        source=req.source,
        difficulty=req.difficulty,
        category=req.category,
        tags=req.tags or [],
    )
    db.add(case)
    await db.flush()

    # 可选关联到测试集
    if req.case_set_ids:
        for cs_id in req.case_set_ids:
            member = CaseSetMember(case_set_id=uuid.UUID(cs_id), case_id=case.id)
            db.add(member)
        # 更新 case_count
        for cs_id in req.case_set_ids:
            await db.execute(
                update(CaseSet)
                .where(CaseSet.id == uuid.UUID(cs_id))
                .values(case_count=CaseSet.case_count + 1)
            )

    await db.commit()
    await db.refresh(case)
    return _case_to_response(case)


@router.delete("/{case_id}", status_code=204)
async def delete_case(case_id: str, db: AsyncSession = Depends(get_db)):
    """删除评测用例（同时清理 case_set_members 关联）。"""
    case = await db.get(EvalCase, uuid.UUID(case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # 减少关联 case_set 的计数
    result = await db.execute(
        select(CaseSetMember.case_set_id).where(CaseSetMember.case_id == uuid.UUID(case_id))
    )
    cs_ids = [row[0] for row in result.all()]
    for cs_id in cs_ids:
        await db.execute(
            update(CaseSet)
            .where(CaseSet.id == cs_id)
            .values(case_count=func.greatest(CaseSet.case_count - 1, 0))
        )

    await db.delete(case)
    await db.commit()


# ═══════════════════════════════════════════════════════════════
# Trace 列表 (Dashboard 用)
# ═══════════════════════════════════════════════════════════════

@router.get("/traces/list", response_model=TraceListResponse)
async def list_traces_for_case_conversion(
    source: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = Query(None, description="搜索 query 关键词"),
    only_without_case: bool = Query(False, description="仅显示未转 Case 的 Trace"),
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """查询 Trace 列表（Dashboard 用），标注是否已转为 Case。"""
    stmt = select(Trace)

    if source:
        stmt = stmt.where(Trace.source == source)
    if status:
        stmt = stmt.where(Trace.status == status)
    if search:
        stmt = stmt.where(Trace.query.ilike(f"%{search}%"))

    # 计数
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    # 分页
    stmt = stmt.order_by(Trace.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    traces = result.scalars().all()

    # 批量查哪些 trace 已转为 case（同时获取 case_id）
    trace_ids = [t.id for t in traces]
    trace_to_case: dict = {}  # trace_id → case_id
    if trace_ids:
        cs_result = await db.execute(
            select(EvalCase.source_trace_id, EvalCase.id).where(
                EvalCase.source_trace_id.in_(trace_ids)
            )
        )
        for row in cs_result.all():
            trace_to_case[row[0]] = str(row[1])

    items = []
    for t in traces:
        if only_without_case and t.id in trace_to_case:
            continue
        case_id = trace_to_case.get(t.id)
        items.append(TraceBrief(
            id=str(t.id),
            agent_version=t.agent_version,
            query=t.query,
            source=t.source,
            status=t.status,
            overall_score=float(t.overall_score) if t.overall_score else None,
            total_latency_ms=t.total_latency_ms,
            created_at=t.created_at,
            already_case=case_id is not None,
            case_id=case_id,
        ))

    # 重新计算 total（如果过滤了）
    if only_without_case:
        total = len(items)

    return TraceListResponse(total=total, items=items)


# ═══════════════════════════════════════════════════════════════
# Trace 详情 (Dashboard 用)
# ═══════════════════════════════════════════════════════════════

@router.get("/traces/{trace_id}")
async def get_trace_detail(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
):
    """获取 Trace 详情（含 spans 和评分），供 Dashboard 侧滑抽屉使用。"""
    try:
        tid = uuid.UUID(trace_id)
    except ValueError:
        raise HTTPException(400, "非法 trace_id")

    trace = await db.get(Trace, tid)
    if not trace:
        raise HTTPException(404, "Trace 不存在")

    spans_r = await db.execute(
        select(Span).where(Span.trace_id == tid).order_by(Span.sequence)
    )
    spans = spans_r.scalars().all()

    scores_r = await db.execute(
        select(EvalScore).where(EvalScore.trace_id == tid)
    )
    scores = scores_r.scalars().all()

    # span 类型分布
    type_counts: dict = {}
    for sp in spans:
        type_counts[sp.span_type] = type_counts.get(sp.span_type, 0) + 1

    # span_id → span_type 映射，用于标注 eval_score 所属层
    span_type_map = {sp.id: sp.span_type for sp in spans}

    return {
        "trace": {
            "id": str(trace.id),
            "query": trace.query,
            "status": trace.status,
            "source": trace.source,
            "final_response": trace.final_response,
            "overall_score": float(trace.overall_score) if trace.overall_score else None,
            "total_latency_ms": trace.total_latency_ms,
            "total_tokens": trace.total_tokens,
            "created_at": trace.created_at.isoformat() if trace.created_at else None,
            "span_count": len(spans),
            "span_distribution": type_counts,
        },
        "spans": [
            {
                "id": str(sp.id),
                "span_type": sp.span_type,
                "sequence": sp.sequence,
                "input": sp.input,
                "output": sp.output,
                "latency_ms": sp.latency_ms,
                "tokens": sp.tokens,
                "model": sp.model,
                "score": float(sp.score) if sp.score else None,
                "tool_name": sp.tool_name,
                "tool_params": sp.tool_params,
                "tool_result": sp.tool_result,
            }
            for sp in spans
        ],
        "eval_scores": [
            {
                "id": str(sc.id),
                "span_id": str(sc.span_id) if sc.span_id else None,
                "layer": span_type_map.get(sc.span_id) if sc.span_id else None,
                "score": float(sc.score),
                "metrics": sc.metrics,
                "method": sc.method,
            }
            for sc in scores
        ],
    }


# ═══════════════════════════════════════════════════════════════
# Trace → Case
# ═══════════════════════════════════════════════════════════════

@router.post("/from-trace/{trace_id}", response_model=CaseResponse, status_code=201)
async def create_case_from_trace(
    trace_id: str,
    req: TraceToCaseRequest = TraceToCaseRequest(),
    db: AsyncSession = Depends(get_db),
):
    """将指定 Trace 转换为评测用例。

    自动快照 trace 的：
    - query → case.query
    - context → case.context
    - final_response、spans 摘要 → case.metadata（用于回放）
    - source 设为 'trace'，source_trace_id 指向原 Trace
    """
    trace_uuid = uuid.UUID(trace_id)
    trace = await db.get(Trace, trace_uuid)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    # 检查是否已存在
    result = await db.execute(
        select(EvalCase).where(EvalCase.source_trace_id == trace_uuid)
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"该 Trace 已转为 Case: {existing.id}",
        )

    # 加载 spans 作摘要
    result = await db.execute(
        select(Span).where(Span.trace_id == trace_uuid).order_by(Span.sequence)
    )
    spans = result.scalars().all()

    spans_summary = []
    for s in spans:
        spans_summary.append({
            "span_type": s.span_type,
            "sequence": s.sequence,
            "tool_name": s.tool_name,
            "latency_ms": s.latency_ms,
            "model": s.model,
            "tool_status": s.tool_status,
        })

    metadata = {
        "trace_id": str(trace.id),
        "final_response": trace.final_response,
        "total_latency_ms": trace.total_latency_ms,
        "total_tokens": trace.total_tokens,
        "spans_summary": spans_summary,
        "snapshot_at": datetime.utcnow().isoformat(),
    }

    case = EvalCase(
        query=trace.query,
        context=trace.context or {},
        source="trace",
        source_trace_id=trace.id,
        difficulty=req.difficulty,
        category=req.category,
        tags=req.tags or [],
        metadata_=metadata,
        review_status="pending",
    )
    db.add(case)
    await db.flush()

    # 可选关联到测试集
    if req.case_set_ids:
        for cs_id in req.case_set_ids:
            member = CaseSetMember(case_set_id=uuid.UUID(cs_id), case_id=case.id)
            db.add(member)
        for cs_id in req.case_set_ids:
            await db.execute(
                update(CaseSet)
                .where(CaseSet.id == uuid.UUID(cs_id))
                .values(case_count=CaseSet.case_count + 1)
            )

    await db.commit()
    await db.refresh(case)
    return _case_to_response(case)


# ═══════════════════════════════════════════════════════════════
# 单 Case 评分（异步）
# ═══════════════════════════════════════════════════════════════


async def _run_evaluation_background(
    trace_id: str,
    eval_run_id: str,
    case_id: str,
    task_id: str,
) -> None:
    """后台执行评测 + 更新 Case 统计。

    与 HTTP 请求生命周期完全解耦，使用独立 session。
    """
    try:
        from backend.workers.eval_worker import evaluate_trace

        result = await evaluate_trace(trace_id, eval_run_id)

        # 更新 EvalCase 的 run_count 和 last_avg_score
        from backend.core.database import async_session_factory

        async with async_session_factory() as update_session:
            stmt = (
                update(EvalCase)
                .where(EvalCase.id == uuid.UUID(case_id))
                .values(
                    run_count=func.coalesce(EvalCase.run_count, 0) + 1,
                    last_avg_score=result.get("overall_score"),
                    updated_at=datetime.utcnow(),
                )
            )
            await update_session.execute(stmt)

            # 更新 EvalTask 为 completed
            await update_session.execute(
                update(EvalTask)
                .where(EvalTask.id == uuid.UUID(task_id))
                .values(
                    status="completed",
                    completed_cases=1,
                    completed_at=datetime.utcnow(),
                )
            )
            await update_session.commit()

    except Exception as e:
        logger.exception("后台评测失败: trace=%s run=%s", trace_id, eval_run_id)
        from backend.core.database import async_session_factory

        async with async_session_factory() as cleanup_session:
            await cleanup_session.execute(
                update(EvalRun)
                .where(EvalRun.id == uuid.UUID(eval_run_id))
                .values(
                    status="failed",
                    error_message=str(e)[:500],
                    completed_at=datetime.utcnow(),
                )
            )
            await cleanup_session.execute(
                update(EvalTask)
                .where(EvalTask.id == uuid.UUID(task_id))
                .values(
                    status="failed",
                    failed_cases=1,
                    completed_at=datetime.utcnow(),
                )
            )
            await cleanup_session.commit()


@router.post("/{case_id}/evaluate", response_model=EvaluateCaseAsyncResponse, status_code=202)
async def evaluate_case(case_id: str, db: AsyncSession = Depends(get_db)):
    """对单个 Case 执行评测（异步）。

    前提：该 Case 必须有关联的 source_trace_id（即从 Trace 创建）。
    流程：
    1. 加载 Case → 获取 expected 标注
    2. 加载关联 Trace + Spans
    3. 创建 EvalRun，写入 expected_snapshot
    4. 通过 asyncio.create_task 在后台启动评测引擎
    5. 立即返回 run_id + status="running"
    """
    case_uuid = uuid.UUID(case_id)
    case = await db.get(EvalCase, case_uuid)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if not case.source_trace_id:
        raise HTTPException(
            status_code=400,
            detail="该 Case 无关联 Trace（source_trace_id 为空），无法直接评分。请先将 Trace 转为 Case。",
        )

    # 加载 Trace
    trace = await db.get(Trace, case.source_trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="关联的 Trace 不存在（可能已被清理）")

    # 每次评分都创建新的 EvalRun（支持多次评分记录）
    task = EvalTask(
        name=f"单Case评分-{case.query[:20]}",
        agent_version=trace.agent_version,
        total_cases=1,
        status="running",
    )
    db.add(task)
    await db.flush()

    # 构建 expected_snapshot
    expected_snapshot = {
        "query": case.query,
        "context": case.context,
        "expected_intent": case.expected_intent,
        "expected_retrieval": case.expected_retrieval,
        "expected_tools": case.expected_tools,
        "expected_answer": case.expected_answer,
        "gold_answer": case.gold_answer,
    }

    eval_run = EvalRun(
        task_id=task.id,
        eval_case_id=case.id,
        agent_version=trace.agent_version,
        trace_id=trace.id,
        expected_snapshot=expected_snapshot,
        status="running",
        started_at=datetime.utcnow(),
    )
    db.add(eval_run)
    await db.flush()

    # 先提交 EvalRun，使 evaluate_trace（独立 session）可见
    await db.commit()

    # 在后台启动评测任务（不阻塞 HTTP 响应）
    asyncio.create_task(
        _run_evaluation_background(
            trace_id=str(trace.id),
            eval_run_id=str(eval_run.id),
            case_id=str(case.id),
            task_id=str(task.id),
        )
    )

    return EvaluateCaseAsyncResponse(
        case_id=str(case.id),
        run_id=str(eval_run.id),
        task_id=str(task.id),
        trace_id=str(trace.id),
        status="running",
    )


# ═══════════════════════════════════════════════════════════════
# Case 详细评分历史
# ═══════════════════════════════════════════════════════════════

@router.get("/{case_id}/scores")
async def get_case_scores(case_id: str, db: AsyncSession = Depends(get_db)):
    """获取 Case 的所有评测历史及详细打分项。

    返回每次评测的各层得分 + 各维度明细 + LLM Judge 调用痕迹。
    多轮评分通过 eval_run_id 严格隔离，每轮独立记录。
    """
    case_uuid = uuid.UUID(case_id)
    case = await db.get(EvalCase, case_uuid)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # 查询该 case 的所有已完成 EvalRun（过滤掉失败/进行中的）
    result = await db.execute(
        select(EvalRun)
        .where(EvalRun.eval_case_id == case_uuid, EvalRun.status == "completed")
        .order_by(EvalRun.created_at.desc())
    )
    runs = result.scalars().all()

    history = []
    for run in runs:
        # 通过 eval_run_id 精准查询该轮评分（而非 trace_id，避免多次评分串数据）
        result = await db.execute(
            select(EvalScore).where(EvalScore.eval_run_id == run.id)
        )
        eval_scores = result.scalars().all()

        # 查询 spans 获取 span_id → span_type 映射
        span_ids = [s.span_id for s in eval_scores if s.span_id]
        span_type_map: dict = {}
        if span_ids:
            result = await db.execute(
                select(Span.id, Span.span_type).where(Span.id.in_(span_ids))
            )
            for row in result.all():
                span_type_map[row[0]] = row[1]

        # 总分以编排器写入的 traces.overall_score 为准（与 last_avg_score 同源）
        # → 避免重算时因 EvalScore 缺少无 span 层（如 tool）导致偏差
        overall_score = None
        if run.trace_id:
            trace = await db.get(Trace, run.trace_id)
            overall_score = float(trace.overall_score) if trace and trace.overall_score else None

        history.append({
            "run_id": str(run.id),
            "trace_id": str(run.trace_id) if run.trace_id else None,
            "status": run.status,
            "overall_score": overall_score,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "expected_snapshot": run.expected_snapshot,
            "scores": [
                {
                    "id": str(s.id),
                    "span_id": str(s.span_id) if s.span_id else None,
                    "layer": _span_type_to_layer(s.span_id, span_type_map),
                    "score": float(s.score),
                    "metrics": s.metrics,
                    "method": s.method,
                    "judge_trace": s.judge_trace,
                    "evaluator_version": s.evaluator_version,
                    "evaluation_latency_ms": s.evaluation_latency_ms,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in eval_scores
            ],
        })

    return {
        "case_id": str(case.id),
        "query": case.query,
        "last_avg_score": float(case.last_avg_score) if case.last_avg_score else None,
        "run_count": case.run_count,
        "history": history,
    }


def _span_type_to_layer(span_id, span_type_map: dict) -> str:
    """将 span_id 映射为评测层名称。span_id 为空时代表 outcome 层。"""
    if span_id is None:
        return "outcome"
    stype = span_type_map.get(span_id, "unknown")
    # span_type "tool_call" → layer "tool"
    if stype == "tool_call":
        return "tool"
    return stype
