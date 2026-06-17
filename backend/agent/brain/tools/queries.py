"""查询类 Tool Handler —— 6 个评测数据查询工具。

每个 handler 接收 (args: dict, context: CommandContext)，返回结构化数据。
由 CommandExecutor._format_reply() 统一渲染为 Markdown。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import String, desc, func, select

logger = logging.getLogger(__name__)


# ===================================================================
# Tool 1: get_latest_eval_status
# ===================================================================

async def get_latest_eval_status(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取最近评测任务的全局状态概览。

    Returns:
        {"total_tasks": int, "status_counts": dict, "avg_overall_score": float,
         "active_versions": [str], "hours_back": int}
    """
    from backend.core.models import EvalTask, Trace

    agent_version = args.get("agent_version")
    hours_back = int(args.get("hours_back", 24))
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    async with context.db_session_factory() as session:
        # 任务状态统计
        stmt = select(
            EvalTask.status,
            func.count(EvalTask.id).label("cnt"),
        ).where(EvalTask.created_at >= since)

        if agent_version:
            stmt = stmt.where(EvalTask.agent_version == agent_version)

        stmt = stmt.group_by(EvalTask.status)
        result = await session.execute(stmt)
        status_counts = {row.status: row.cnt for row in result.fetchall()}

        total_tasks = sum(status_counts.values())

        # 平均评分
        score_stmt = select(func.avg(Trace.overall_score)).where(
            Trace.created_at >= since
        )
        if agent_version:
            score_stmt = score_stmt.where(Trace.agent_version == agent_version)

        score_result = await session.execute(score_stmt)
        avg_score = score_result.scalar()
        avg_score = round(float(avg_score), 2) if avg_score else 0.0

        # 活跃版本
        ver_stmt = (
            select(Trace.agent_version)
            .where(Trace.created_at >= since)
            .distinct()
            .order_by(Trace.agent_version)
        )
        ver_result = await session.execute(ver_stmt)
        active_versions = [row[0] for row in ver_result.fetchall()]

    return {
        "total_tasks": total_tasks,
        "status_counts": status_counts,
        "avg_overall_score": avg_score,
        "active_versions": active_versions,
        "hours_back": hours_back,
    }


# ===================================================================
# Tool 2: query_score_trend
# ===================================================================

async def query_score_trend(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查询指定 Agent 版本最近 N 次评测的得分趋势。

    Returns:
        {"version": str, "layer": str, "trend": [{"run_time": str, "score": float}, ...],
         "last_n": int, "delta": float}
    """
    from backend.core.models import EvalRun, Trace

    agent_version = args.get("agent_version")
    last_n = int(args.get("last_n", 5))
    layer = args.get("layer", "overall")
    case_set_name = args.get("case_set_name")

    async with context.db_session_factory() as session:
        if layer == "overall":
            # 查 traces.overall_score
            stmt = (
                select(Trace.created_at, Trace.overall_score)
                .where(Trace.overall_score.isnot(None))
                .order_by(desc(Trace.created_at))
                .limit(last_n)
            )
            if agent_version:
                stmt = stmt.where(Trace.agent_version == agent_version)
            # NOTE: case_set_name 过滤暂未实现，需 JOIN EvalRun + EvalTask + CaseSet 表

            result = await session.execute(stmt)
            rows = result.fetchall()
            trend = [
                {"run_time": row[0].isoformat() if row[0] else "", "score": float(row[1]) if row[1] else 0.0}
                for row in rows
            ]
        else:
            # 按层查 eval_runs + eval_scores
            stmt = (
                select(EvalRun.created_at, EvalScore.score)
                .select_from(EvalRun)
                .join(EvalScore, EvalScore.eval_run_id == EvalRun.id)
                .where(EvalScore.metrics.has_key(layer))  # JSONB contains
                .order_by(desc(EvalRun.created_at))
                .limit(last_n)
            )
            if agent_version:
                stmt = stmt.where(EvalRun.agent_version == agent_version)
            # NOTE: case_set_name 过滤暂未实现，需 JOIN EvalTask + CaseSet 表

            result = await session.execute(stmt)
            rows = result.fetchall()
            trend = [
                {"run_time": row[0].isoformat() if row[0] else "", "score": float(row[1]) if row[1] else 0.0}
                for row in rows
            ]

    # 计算 delta（最新 vs 前一次）
    delta = 0.0
    if len(trend) >= 2:
        delta = round(trend[0]["score"] - trend[1]["score"], 2)

    return {
        "version": agent_version or "全部",
        "layer": layer,
        "trend": trend[::-1],  # 时间升序
        "last_n": last_n,
        "delta": delta,
    }


# ===================================================================
# Tool 3: search_traces
# ===================================================================

async def search_traces(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """按关键词/来源/分数范围搜索 Trace 记录。

    Returns:
        {"traces": [...], "total": int, "limit": int}
    """
    from backend.core.models import Trace

    query_keyword = args.get("query_keyword")
    source = args.get("source")
    min_score = args.get("min_score")
    max_score = args.get("max_score")
    status = args.get("status")
    limit = min(int(args.get("limit", 10)), 50)

    async with context.db_session_factory() as session:
        stmt = select(Trace).order_by(desc(Trace.created_at))

        if query_keyword:
            stmt = stmt.where(Trace.query.ilike(f"%{query_keyword}%"))
        if source:
            stmt = stmt.where(Trace.source == source)
        if min_score is not None:
            stmt = stmt.where(Trace.overall_score >= min_score)
        if max_score is not None:
            stmt = stmt.where(Trace.overall_score <= max_score)
        if status:
            stmt = stmt.where(Trace.status == status)

        # Count total
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_result = await session.execute(count_stmt)
        total = total_result.scalar() or 0

        # Fetch
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        traces = result.scalars().all()

        trace_list = [
            {
                "id": str(t.id)[:8],
                "query": t.query[:100] if t.query else "",
                "status": t.status,
                "overall_score": float(t.overall_score) if t.overall_score else None,
                "source": t.source,
                "agent_version": t.agent_version,
                "created_at": t.created_at.isoformat() if t.created_at else "",
            }
            for t in traces
        ]

    return {"traces": trace_list, "total": total, "limit": limit}


# ===================================================================
# Tool 4: get_trace_detail
# ===================================================================

async def get_trace_detail(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取指定 Trace 的完整详情。

    Returns:
        {"trace": dict, "spans": [...], "eval_scores": [...]}
    """
    from backend.core.models import Trace, Span, EvalScore

    trace_id_raw = args.get("trace_id", "")
    if not trace_id_raw:
        raise ValueError("trace_id 是必填参数")

    async with context.db_session_factory() as session:
        # 支持短前缀匹配（至少 8 位）
        if len(trace_id_raw) < 36:
            stmt = (
                select(Trace)
                .where(Trace.id.cast(String).like(f"{trace_id_raw}%"))
                .order_by(desc(Trace.created_at))
                .limit(1)
            )
        else:
            stmt = select(Trace).where(Trace.id == trace_id_raw)

        result = await session.execute(stmt)
        trace = result.scalars().first()

        if not trace:
            raise ValueError(f"未找到 Trace: {trace_id_raw}")

        # 查询 spans
        span_stmt = (
            select(Span)
            .where(Span.trace_id == trace.id)
            .order_by(Span.sequence)
        )
        span_result = await session.execute(span_stmt)
        spans = span_result.scalars().all()

        # 查询 eval_scores
        score_stmt = select(EvalScore).where(EvalScore.trace_id == trace.id)
        score_result = await session.execute(score_stmt)
        scores = score_result.scalars().all()

    return {
        "trace": {
            "id": str(trace.id),
            "query": trace.query,
            "status": trace.status,
            "overall_score": float(trace.overall_score) if trace.overall_score else None,
            "agent_version": trace.agent_version,
            "source": trace.source,
            "total_latency_ms": trace.total_latency_ms,
            "final_response": trace.final_response[:500] + "..." if trace.final_response and len(trace.final_response) > 500 else trace.final_response,
            "created_at": trace.created_at.isoformat() if trace.created_at else "",
        },
        "spans": [
            {
                "id": str(s.id)[:8],
                "span_type": s.span_type,
                "sequence": s.sequence,
                "tool_name": s.tool_name,
                "tool_status": s.tool_status,
                "score": float(s.score) if s.score else None,
                "latency_ms": s.latency_ms,
            }
            for s in spans
        ],
        "eval_scores": [
            {
                "id": str(s.id)[:8],
                "score": float(s.score) if s.score else 0.0,
                "metrics": s.metrics,
                "method": s.method,
            }
            for s in scores
        ],
    }


# ===================================================================
# Tool 5: list_case_sets
# ===================================================================

async def list_case_sets(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """列出当前可用的测试用例集。

    Returns:
        {"case_sets": [...], "total": int}
    """
    from backend.core.models import CaseSet

    category = args.get("category")
    search = args.get("search")

    async with context.db_session_factory() as session:
        stmt = select(CaseSet).order_by(CaseSet.name)

        if category:
            stmt = stmt.where(CaseSet.category == category)
        if search:
            stmt = stmt.where(CaseSet.name.ilike(f"%{search}%"))

        result = await session.execute(stmt)
        case_sets = result.scalars().all()

        sets_list = [
            {
                "id": str(cs.id)[:8],
                "name": cs.name,
                "description": cs.description or "",
                "category": cs.category or "",
                "case_count": cs.case_count,
                "version": cs.version,
                "created_at": cs.created_at.isoformat() if cs.created_at else "",
            }
            for cs in case_sets
        ]

    return {"case_sets": sets_list, "total": len(sets_list)}


# ===================================================================
# Tool 6: get_weakest_cases
# ===================================================================

async def get_weakest_cases(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """找出当前评分最低的测试用例（退化热点）。

    Returns:
        {"cases": [...], "top_n": int, "layer": str}
    """
    from backend.core.models import EvalCase, EvalRun, EvalScore

    agent_version = args.get("agent_version")
    top_n = int(args.get("top_n", 10))
    layer = args.get("layer", "overall")

    async with context.db_session_factory() as session:
        # 按 case 分组，取平均分最低的 N 个
        subq = (
            select(
                EvalRun.eval_case_id,
                func.avg(EvalScore.score).label("avg_score"),
                func.count(EvalRun.id).label("run_count"),
            )
            .select_from(EvalRun)
            .join(EvalScore, EvalScore.eval_run_id == EvalRun.id)
            .where(EvalScore.score.isnot(None))
        )

        if agent_version:
            subq = subq.where(EvalRun.agent_version == agent_version)

        subq = subq.group_by(EvalRun.eval_case_id).subquery()

        stmt = (
            select(EvalCase, subq.c.avg_score, subq.c.run_count)
            .join(subq, EvalCase.id == subq.c.eval_case_id)
            .order_by(subq.c.avg_score.asc())
            .limit(top_n)
        )

        result = await session.execute(stmt)
        rows = result.fetchall()

        cases = [
            {
                "id": str(row[0].id)[:8],
                "query": row[0].query[:100] if row[0].query else "",
                "category": row[0].category or "",
                "difficulty": row[0].difficulty,
                "avg_score": round(float(row[1]), 2) if row[1] else 0.0,
                "run_count": row[2],
                "health_status": row[0].health_status,
            }
            for row in rows
        ]

    return {"cases": cases, "top_n": top_n, "layer": layer}
