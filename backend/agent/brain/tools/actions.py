"""操作类 Tool Handler —— 3 个评测操作工具。

包括触发评测、采样评测、调度任务管理。
高风险操作标记 risk_level=high/medium，由 MessageRouter 执行二次确认。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import desc, func, select

logger = logging.getLogger(__name__)


# ===================================================================
# Tool 7: trigger_evaluation
# ===================================================================

async def trigger_evaluation(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """触发一次评测任务。

    风险等级：HIGH。需 MessageRouter 二次确认后才能调用此 handler。

    Returns:
        {"task_id": str, "agent_version": str, "case_set_name": str, "layers": [str]}
    """
    from backend.core.models import EvalTask, EvalRun, CaseSet, EvalCase

    agent_version = args.get("agent_version")
    if not agent_version:
        raise ValueError("agent_version 是必填参数")

    case_set_name = args.get("case_set_name", "")
    layers = args.get("layers", ["intent", "retrieval", "tool", "generation", "outcome"])

    async with context.db_session_factory() as session:
        # 查找 CaseSet
        case_set = None
        if case_set_name:
            stmt = select(CaseSet).where(CaseSet.name.ilike(f"%{case_set_name}%")).limit(1)
            result = await session.execute(stmt)
            case_set = result.scalars().first()
            if not case_set:
                raise ValueError(f"未找到测试集: {case_set_name}")

        # 获取用例（如果指定了 case_set）
        case_ids = []
        if case_set:
            case_stmt = select(EvalCase.id).where(
                EvalCase.id.in_(
                    select(EvalCase.id).where(
                        EvalCase.set_memberships.any(case_set_id=case_set.id)
                    )
                )
            )
            case_result = await session.execute(case_stmt)
            case_ids = [row[0] for row in case_result.fetchall()]

        if not case_ids:
            # 默认取最近的活跃用例
            case_stmt = (
                select(EvalCase.id)
                .where(EvalCase.is_active.is_(True))
                .limit(10)
            )
            case_result = await session.execute(case_stmt)
            case_ids = [row[0] for row in case_result.fetchall()]

        if not case_ids:
            raise ValueError("没有可用的评测用例，请先创建用例")

        # 创建 EvalTask
        task = EvalTask(
            name=f"手动评测 - {agent_version} - {datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
            agent_version=agent_version,
            case_set_id=case_set.id if case_set else None,
            status="pending",
            total_cases=len(case_ids),
            config={"layers": layers, "trigger": "manual_im"},
        )
        session.add(task)
        await session.flush()

        # 创建 EvalRun
        runs_created = 0
        for case_id in case_ids:
            run = EvalRun(
                task_id=task.id,
                eval_case_id=case_id,
                agent_version=agent_version,
                status="pending",
            )
            session.add(run)
            runs_created += 1

        await session.commit()

        logger.info(
            "评测任务已创建: task_id=%s version=%s cases=%d layers=%s",
            task.id, agent_version, runs_created, layers,
        )

    return {
        "task_id": str(task.id),
        "agent_version": agent_version,
        "case_set_name": case_set_name or "默认",
        "total_cases": runs_created,
        "layers": layers,
    }


# ===================================================================
# Tool 8: sample_and_evaluate
# ===================================================================

async def sample_and_evaluate(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """从生产环境 Trace 中手动采样并触发评测。

    风险等级：MEDIUM。采样量 > 20 时需 MessageRouter 二次确认。

    Returns:
        {"sampled": int, "batch_id": str, "hours_back": int}
    """
    from backend.core.models import Trace, EvalCase, EvalTask, EvalRun

    sample_size = int(args.get("sample_size", 10))
    hours_back = int(args.get("hours_back", 24))
    agent_version = args.get("agent_version")
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    batch_id = uuid.uuid4()

    async with context.db_session_factory() as session:
        # 查询生产 Trace
        stmt = (
            select(Trace)
            .where(Trace.source == "production")
            .where(Trace.created_at >= since)
            .order_by(func.random())
            .limit(sample_size)
        )
        if agent_version:
            stmt = stmt.where(Trace.agent_version == agent_version)

        result = await session.execute(stmt)
        traces = result.scalars().all()

        if not traces:
            return {"sampled": 0, "batch_id": str(batch_id), "message": "没有找到符合条件的生产 Trace"}

        # 为每条 Trace 创建 EvalCase
        sampled_count = 0
        for trace in traces:
            case = EvalCase(
                query=trace.query,
                context=trace.context or {},
                source="sampling",
                source_trace_id=trace.id if isinstance(trace.id, uuid.UUID) else None,
                sampling_batch_id=batch_id,
                difficulty="medium",
                metadata_={
                    "trace_id": str(trace.id),
                    "agent_version": trace.agent_version,
                },
            )
            session.add(case)
            sampled_count += 1

        # 创建 EvalTask + EvalRuns
        task = EvalTask(
            name=f"采样评测 {datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
            agent_version=agent_version or "unknown",
            status="pending",
            total_cases=sampled_count,
            config={
                "trigger": "manual_sample",
                "batch_id": str(batch_id),
                "hours_back": hours_back,
            },
        )
        session.add(task)
        await session.flush()

        # 获取刚创建的 cases 并创建 runs
        case_stmt = select(EvalCase.id).where(EvalCase.sampling_batch_id == batch_id)
        case_result = await session.execute(case_stmt)
        created_case_ids = [row[0] for row in case_result.fetchall()]

        for case_id in created_case_ids:
            run = EvalRun(
                task_id=task.id,
                eval_case_id=case_id,
                agent_version=agent_version or "unknown",
                status="pending",
            )
            session.add(run)

        await session.commit()

        logger.info(
            "采样评测已创建: batch=%s count=%d task=%s",
            batch_id, sampled_count, task.id,
        )

    return {
        "sampled": sampled_count,
        "batch_id": str(batch_id),
        "task_id": str(task.id),
        "hours_back": hours_back,
    }


# ===================================================================
# Tool 9: manage_scheduler
# ===================================================================

async def manage_scheduler(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """管理后台调度任务：查看/暂停/恢复/触发/修改。

    Returns:
        {"action": str, "result": dict}
    """
    action = args.get("action", "list")
    job_id = args.get("job_id", "")

    scheduler = context.scheduler
    if not scheduler:
        return {"action": action, "error": "调度器未初始化"}

    if action == "list":
        jobs = scheduler.list_jobs()
        return {
            "action": "list",
            "jobs": [
                {
                    "job_id": j.job_id,
                    "name": j.name,
                    "description": j.description,
                    "trigger_type": j.trigger_type.value if hasattr(j.trigger_type, 'value') else str(j.trigger_type),
                    "trigger_value": j.trigger_value,
                    "enabled": j.enabled,
                }
                for j in jobs
            ],
            "total": len(jobs),
        }

    elif action == "pause":
        if not job_id:
            return {"action": "pause", "error": "job_id 不能为空"}
        scheduler.pause(job_id)
        return {"action": "pause", "job_id": job_id, "status": "paused"}

    elif action == "resume":
        if not job_id:
            return {"action": "resume", "error": "job_id 不能为空"}
        scheduler.resume(job_id)
        return {"action": "resume", "job_id": job_id, "status": "resumed"}

    elif action == "trigger":
        if not job_id:
            return {"action": "trigger", "error": "job_id 不能为空"}
        exec_id = await scheduler.trigger_now(job_id)
        return {"action": "trigger", "job_id": job_id, "execution_id": exec_id}

    elif action == "update":
        if not job_id:
            return {"action": "update", "error": "job_id 不能为空"}
        new_value = args.get("new_trigger_value", "")
        if not new_value:
            return {"action": "update", "error": "new_trigger_value 不能为空"}

        from backend.agent.scheduler.base import TriggerType

        # 根据 value 格式判断触发器类型
        try:
            int(new_value)
            trigger_type = TriggerType.INTERVAL
        except ValueError:
            # 尝试作为 cron 解析
            try:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger.from_crontab(new_value)
                trigger_type = TriggerType.CRON
            except Exception:
                return {"action": "update", "error": "触发器值格式无效，请输入秒数（数字）或标准 cron 表达式（如 '0 8 * * *'）"}

        scheduler.update_schedule(job_id, trigger_type, new_value)
        return {
            "action": "update",
            "job_id": job_id,
            "trigger_type": trigger_type.value,
            "trigger_value": new_value,
        }

    elif action == "history":
        if not job_id:
            return {"action": "history", "error": "job_id 不能为空"}
        history = await scheduler.get_history(job_id, limit=10)
        return {
            "action": "history",
            "job_id": job_id,
            "executions": [
                {
                    "id": str(e.id)[:8] if hasattr(e, 'id') else "",
                    "status": e.status,
                    "started_at": e.started_at.isoformat() if e.started_at else "",
                    "duration_ms": e.duration_ms,
                    "error_message": e.error_message,
                }
                for e in history
            ],
        }

    return {"action": action, "error": f"未知操作: {action}"}
