"""评测统计聚合 API —— 为 AgentBrain 提供数据查询端点。

包含：状态概览、评分趋势、弱点评分、版本对比、日报。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.core.models import EvalTask, Trace, EvalCase, EvalRun, EvalScore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stats", tags=["stats"])


# ═══════════════════════════════════════════════════════════════
# 1. 评测状态概览
# ═══════════════════════════════════════════════════════════════

@router.get("/overview")
async def get_eval_status_overview(
    agent_version: Optional[str] = Query(None),
    hours_back: int = Query(24, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
):
    """评测状态概览：任务状态计数、平均分、活跃版本。

    对应 Brain tool: ``get_latest_eval_status``
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # 任务状态统计
    stmt = select(
        EvalTask.status,
        func.count(EvalTask.id).label("cnt"),
    ).where(EvalTask.created_at >= since)

    if agent_version:
        stmt = stmt.where(EvalTask.agent_version == agent_version)

    stmt = stmt.group_by(EvalTask.status)
    result = await db.execute(stmt)
    status_counts = {row.status: row.cnt for row in result.fetchall()}
    total_tasks = sum(status_counts.values())

    # 平均评分
    score_stmt = select(func.avg(Trace.overall_score)).where(
        Trace.created_at >= since
    )
    if agent_version:
        score_stmt = score_stmt.where(Trace.agent_version == agent_version)

    score_result = await db.execute(score_stmt)
    avg_score = score_result.scalar()
    avg_score = round(float(avg_score), 2) if avg_score else 0.0

    # 活跃版本
    ver_stmt = (
        select(Trace.agent_version)
        .where(Trace.created_at >= since)
        .distinct()
        .order_by(Trace.agent_version)
    )
    ver_result = await db.execute(ver_stmt)
    active_versions = [row[0] for row in ver_result.fetchall()]

    return {
        "total_tasks": total_tasks,
        "status_counts": status_counts,
        "avg_overall_score": avg_score,
        "active_versions": active_versions,
        "hours_back": hours_back,
    }


# ═══════════════════════════════════════════════════════════════
# 2. 评分趋势
# ═══════════════════════════════════════════════════════════════

@router.get("/trend")
async def get_score_trend(
    agent_version: Optional[str] = Query(None),
    last_n: int = Query(5, ge=1, le=50),
    layer: str = Query("overall", pattern="^(overall|intent|retrieval|tool|generation|outcome)$"),
    case_set_name: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """评分趋势：指定版本最近 N 次评测得分变化。

    对应 Brain tool: ``query_score_trend``
    """
    if layer == "overall":
        stmt = (
            select(Trace.created_at, Trace.overall_score)
            .where(Trace.overall_score.isnot(None))
            .order_by(desc(Trace.created_at))
            .limit(last_n)
        )
        if agent_version:
            stmt = stmt.where(Trace.agent_version == agent_version)

        result = await db.execute(stmt)
        rows = result.fetchall()
        trend = [
            {"run_time": row[0].isoformat() if row[0] else "", "score": float(row[1]) if row[1] else 0.0}
            for row in rows
        ]
    else:
        stmt = (
            select(EvalRun.created_at, EvalScore.score)
            .select_from(EvalRun)
            .join(EvalScore, EvalScore.eval_run_id == EvalRun.id)
            .where(EvalScore.metrics.has_key(layer))
            .order_by(desc(EvalRun.created_at))
            .limit(last_n)
        )
        if agent_version:
            stmt = stmt.where(EvalRun.agent_version == agent_version)

        result = await db.execute(stmt)
        rows = result.fetchall()
        trend = [
            {"run_time": row[0].isoformat() if row[0] else "", "score": float(row[1]) if row[1] else 0.0}
            for row in rows
        ]

    # 计算 delta
    delta = 0.0
    if len(trend) >= 2:
        delta = round(trend[0]["score"] - trend[1]["score"], 2)

    return {
        "version": agent_version or "全部",
        "layer": layer,
        "trend": trend[::-1],
        "last_n": last_n,
        "delta": delta,
    }


# ═══════════════════════════════════════════════════════════════
# 3. 弱点评分用例
# ═══════════════════════════════════════════════════════════════

@router.get("/weakest-cases")
async def get_weakest_cases(
    agent_version: Optional[str] = Query(None),
    top_n: int = Query(10, ge=1, le=50),
    layer: str = Query("overall", pattern="^(overall|intent|retrieval|tool|generation|outcome)$"),
    db: AsyncSession = Depends(get_db),
):
    """弱点评分用例：评分最低的测试用例 Top N。

    对应 Brain tool: ``get_weakest_cases``
    """
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

    result = await db.execute(stmt)
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


# ═══════════════════════════════════════════════════════════════
# 4. 版本对比
# ═══════════════════════════════════════════════════════════════

@router.get("/compare")
async def compare_versions(
    version_a: str = Query(..., min_length=1),
    version_b: str = Query(..., min_length=1),
    case_set_name: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """版本对比：两个 Agent 版本的评测得分差异。

    对应 Brain tool: ``compare_versions``
    """
    # 各版本统计
    stmt = select(
        Trace.agent_version,
        func.avg(Trace.overall_score).label("avg_score"),
        func.count(Trace.id).label("cnt"),
    ).where(
        Trace.agent_version.in_([version_a, version_b]),
        Trace.overall_score.isnot(None),
    ).group_by(Trace.agent_version)

    result = await db.execute(stmt)
    version_scores = {}
    for row in result.fetchall():
        version_scores[row[0]] = {
            "avg_score": round(float(row[1]), 2) if row[1] else 0.0,
            "count": row[2],
        }

    avg_a = version_scores.get(version_a, {}).get("avg_score", 0.0)
    avg_b = version_scores.get(version_b, {}).get("avg_score", 0.0)
    delta = round(avg_b - avg_a, 2)

    return {
        "version_a": version_a,
        "version_b": version_b,
        "comparison": [
            {
                "metric": "overall",
                "version_a": avg_a,
                "version_b": avg_b,
                "delta": delta,
                "count_a": version_scores.get(version_a, {}).get("count", 0),
                "count_b": version_scores.get(version_b, {}).get("count", 0),
            }
        ],
        "overall_delta": delta,
        "significant": abs(delta) >= 5.0,
    }


# ═══════════════════════════════════════════════════════════════
# 5. 日报
# ═══════════════════════════════════════════════════════════════

@router.get("/daily-report")
async def get_daily_report(
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD，默认昨天"),
    agent_version: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """日报摘要：指定日期的评测量、平均分、告警统计。

    对应 Brain tool: ``get_daily_report``
    """
    if date:
        try:
            report_date = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, f"日期格式错误: {date}，需要 YYYY-MM-DD")
    else:
        report_date = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    day_end = report_date + timedelta(days=1)

    # 评测总量 + 平均分
    stmt = select(
        func.count(Trace.id).label("total"),
        func.avg(Trace.overall_score).label("avg_score"),
    ).where(
        Trace.created_at >= report_date,
        Trace.created_at < day_end,
    )
    if agent_version:
        stmt = stmt.where(Trace.agent_version == agent_version)

    result = await db.execute(stmt)
    row = result.fetchone()
    total_evals = row[0] or 0
    avg_score = round(float(row[1]), 2) if row[1] else 0.0

    # 任务统计
    task_stmt = select(func.count(EvalTask.id)).where(
        EvalTask.created_at >= report_date,
        EvalTask.created_at < day_end,
    )
    if agent_version:
        task_stmt = task_stmt.where(EvalTask.agent_version == agent_version)
    task_result = await db.execute(task_stmt)
    total_tasks = task_result.scalar() or 0

    # 告警统计（从 agent_job_executions）
    from backend.agent.scheduler.models import AgentJobExecution
    alert_stmt = select(func.count(AgentJobExecution.id)).where(
        AgentJobExecution.started_at >= report_date,
        AgentJobExecution.started_at < day_end,
        AgentJobExecution.result.has_key("triggered"),
    )
    alert_result = await db.execute(alert_stmt)
    alert_count = alert_result.scalar() or 0

    return {
        "date": report_date.strftime("%Y-%m-%d"),
        "total_evals": total_evals,
        "total_tasks": total_tasks,
        "avg_score": avg_score,
        "layers": {"overall": avg_score},
        "alert_count": alert_count,
    }
