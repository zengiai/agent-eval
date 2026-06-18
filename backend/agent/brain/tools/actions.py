"""操作类 Tool Handler —— 3 个评测操作工具。

包括触发评测、采样评测、调度任务管理。
高风险操作标记 risk_level=high/medium，由 MessageRouter 执行二次确认。

trigger_evaluation / sample_and_evaluate 通过 EvalAPIClient 调用 eval-api，
manage_scheduler 保留直接调用调度器对象（进程内操作，非数据访问）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from backend.agent.brain.api_client import EvalAPIClient

logger = logging.getLogger(__name__)


def _get_client(context: Any) -> EvalAPIClient:
    """从 CommandContext 创建 API 客户端。"""
    api_base_url = getattr(context, "api_base_url", "http://localhost:18000")
    return EvalAPIClient(base_url=api_base_url)


# ===================================================================
# Tool 7: trigger_evaluation
# ===================================================================

async def trigger_evaluation(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """触发一次评测任务。

    风险等级：HIGH。需 MessageRouter 二次确认后才能调用此 handler。

    Returns:
        {"task_id": str, "agent_version": str, "case_set_name": str, "layers": [str]}
    """
    agent_version = args.get("agent_version")
    if not agent_version:
        raise ValueError("agent_version 是必填参数")

    client = _get_client(context)
    return await client.trigger_evaluation(
        agent_version=agent_version,
        case_set_name=args.get("case_set_name"),
        layers=args.get("layers"),
    )


# ===================================================================
# Tool 8: sample_and_evaluate
# ===================================================================

async def sample_and_evaluate(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """从生产环境 Trace 中手动采样并触发评测。

    风险等级：MEDIUM。采样量 > 20 时需 MessageRouter 二次确认。

    Returns:
        {"sampled": int, "batch_id": str, "hours_back": int}
    """
    client = _get_client(context)
    return await client.sample_and_evaluate(
        sample_size=int(args.get("sample_size", 10)),
        hours_back=int(args.get("hours_back", 24)),
        agent_version=args.get("agent_version"),
    )


# ===================================================================
# Tool 9: manage_scheduler
# ===================================================================

async def manage_scheduler(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """管理后台调度任务：查看/暂停/恢复/触发/修改。

    保留直接调用 scheduler 对象（进程内操作，非数据访问）。

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

        try:
            int(new_value)
            trigger_type = TriggerType.INTERVAL
        except ValueError:
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
