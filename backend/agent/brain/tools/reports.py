"""报告类 Tool Handler —— 3 个报告/分析工具。

包括版本对比、日报、告警历史查询。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import desc, func, select, text

logger = logging.getLogger(__name__)


# ===================================================================
# Tool 10: compare_versions
# ===================================================================

async def compare_versions(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """对比两个 Agent 版本的评测得分。

    Returns:
        {"version_a": str, "version_b": str, "comparison": [...]}
    """
    from backend.core.models import Trace, EvalScore

    version_a = args.get("version_a", "")
    version_b = args.get("version_b", "")
    if not version_a or not version_b:
        raise ValueError("version_a 和 version_b 都是必填参数")

    async with context.db_session_factory() as session:
        # 版本 A 的平均分
        stmt_a = select(func.avg(Trace.overall_score)).where(
            Trace.agent_version == version_a,
            Trace.overall_score.isnot(None),
        )
        result_a = await session.execute(stmt_a)
        avg_a = result_a.scalar()

        # 版本 B 的平均分
        stmt_b = select(func.avg(Trace.overall_score)).where(
            Trace.agent_version == version_b,
            Trace.overall_score.isnot(None),
        )
        result_b = await session.execute(stmt_b)
        avg_b = result_b.scalar()

        # 各层得分对比
        stmt_layer = select(
            Trace.agent_version,
            func.avg(Trace.overall_score).label("avg_score"),
            func.count(Trace.id).label("cnt"),
        ).where(
            Trace.agent_version.in_([version_a, version_b]),
            Trace.overall_score.isnot(None),
        ).group_by(Trace.agent_version)

        result_layer = await session.execute(stmt_layer)
        version_scores = {}
        for row in result_layer.fetchall():
            version_scores[row[0]] = {
                "avg_score": round(float(row[1]), 2) if row[1] else 0.0,
                "count": row[2],
            }

    avg_a_val = round(float(avg_a), 2) if avg_a else 0.0
    avg_b_val = round(float(avg_b), 2) if avg_b else 0.0
    delta = round(avg_b_val - avg_a_val, 2)

    comparison = [
        {
            "metric": "overall",
            "version_a": version_scores.get(version_a, {}).get("avg_score", 0.0),
            "version_b": version_scores.get(version_b, {}).get("avg_score", 0.0),
            "delta": delta,
            "count_a": version_scores.get(version_a, {}).get("count", 0),
            "count_b": version_scores.get(version_b, {}).get("count", 0),
        }
    ]

    return {
        "version_a": version_a,
        "version_b": version_b,
        "comparison": comparison,
        "overall_delta": delta,
        "significant": abs(delta) >= 5.0,
    }


# ===================================================================
# Tool 11: get_daily_report
# ===================================================================

async def get_daily_report(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取指定日期的评测日报摘要。

    Returns:
        {"date": str, "total_evals": int, "avg_score": float, "layers": {}, "alerts": int}
    """
    from backend.core.models import Trace, EvalTask
    from backend.agent.scheduler.models import AgentJobExecution

    date_str = args.get("date", "")
    agent_version = args.get("agent_version")

    if date_str:
        try:
            report_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"日期格式错误: {date_str}，需要 YYYY-MM-DD")
    else:
        # 默认昨天
        report_date = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    day_end = report_date + timedelta(days=1)

    async with context.db_session_factory() as session:
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

        result = await session.execute(stmt)
        row = result.fetchone()
        total_evals = row[0] or 0
        avg_score = round(float(row[1]), 2) if row[1] else 0.0

        # 各层得分（简化：从 eval_scores 按 span_type 分组）
        # 这里用 trace 的 overall_score 近似
        layers = {
            "overall": avg_score,
        }

        # 任务统计
        task_stmt = select(func.count(EvalTask.id)).where(
            EvalTask.created_at >= report_date,
            EvalTask.created_at < day_end,
        )
        if agent_version:
            task_stmt = task_stmt.where(EvalTask.agent_version == agent_version)
        task_result = await session.execute(task_stmt)
        total_tasks = task_result.scalar() or 0

        # 告警统计
        alert_stmt = select(func.count(AgentJobExecution.id)).where(
            AgentJobExecution.started_at >= report_date,
            AgentJobExecution.started_at < day_end,
            AgentJobExecution.result.has_key("triggered"),
        )
        alert_result = await session.execute(alert_stmt)
        alert_count = alert_result.scalar() or 0

    return {
        "date": report_date.strftime("%Y-%m-%d"),
        "total_evals": total_evals,
        "total_tasks": total_tasks,
        "avg_score": avg_score,
        "layers": layers,
        "alert_count": alert_count,
    }


# ===================================================================
# Tool 12: get_alert_history
# ===================================================================

async def get_alert_history(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查询历史告警记录。

    Returns:
        {"alerts": [...], "total": int, "limit": int}
    """
    from backend.agent.scheduler.models import AgentJobExecution

    severity = args.get("severity")
    hours_back = int(args.get("hours_back", 24))
    limit = min(int(args.get("limit", 20)), 100)
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    async with context.db_session_factory() as session:
        stmt = (
            select(AgentJobExecution)
            .where(
                AgentJobExecution.started_at >= since,
                AgentJobExecution.result.isnot(None),
            )
            .order_by(desc(AgentJobExecution.started_at))
            .limit(limit)
        )

        result = await session.execute(stmt)
        executions = result.scalars().all()

        # 过滤告警相关
        alerts = []
        for exe in executions:
            res = exe.result or {}
            details = res.get("details", [])
            for d in details:
                if severity and d.get("severity") != severity:
                    continue
                alerts.append({
                    "execution_id": str(exe.id)[:8],
                    "rule_id": d.get("rule_id", ""),
                    "rule_name": d.get("rule_name", ""),
                    "severity": d.get("severity", ""),
                    "triggered": d.get("triggered", False),
                    "message": d.get("message", ""),
                    "current_value": d.get("current_value"),
                    "threshold": d.get("threshold"),
                    "checked_at": exe.started_at.isoformat() if exe.started_at else "",
                })

        # 如果执行记录中没有 detail 结构，尝试直接读 result
        if not alerts:
            for exe in executions:
                res = exe.result or {}
                if res.get("triggered"):
                    alerts.append({
                        "execution_id": str(exe.id)[:8],
                        "triggered_count": res.get("triggered", 0),
                        "total_rules": res.get("total_rules", 0),
                        "checked_at": exe.started_at.isoformat() if exe.started_at else "",
                    })

    return {
        "alerts": alerts[:limit],
        "total": len(alerts),
        "limit": limit,
        "hours_back": hours_back,
    }
