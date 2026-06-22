"""操作类 Tool Handler —— 评测操作与 Scheduler 管理工具。

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


def _enum_value(value: Any) -> Any:
    """兼容 Enum 与普通字符串。"""
    return value.value if hasattr(value, "value") else value


def _serialize_job_config(job: Any, scheduler: Any = None) -> Dict[str, Any]:
    """将 JobConfig 序列化为 tool 返回结构。"""
    job_id = getattr(job, "job_id", "")
    runtime_job = None
    registry = getattr(scheduler, "_job_registry", None) if scheduler else None
    if isinstance(registry, dict):
        runtime_job = registry.get(job_id)

    data = {
        "job_id": job_id,
        "name": getattr(job, "name", ""),
        "description": getattr(job, "description", ""),
        "trigger_type": _enum_value(getattr(job, "trigger_type", "")),
        "trigger_value": getattr(job, "trigger_value", ""),
        "enabled": bool(getattr(job, "enabled", False)),
        "timeout_seconds": getattr(job, "timeout_seconds", None),
        "metadata": getattr(job, "metadata", {}) or {},
    }

    if runtime_job is not None:
        data["runtime"] = {
            "execution_count": getattr(runtime_job, "execution_count", 0),
            "consecutive_failures": getattr(runtime_job, "consecutive_failures", 0),
            "last_error": getattr(runtime_job, "last_error", None),
        }
    return data


def _serialize_execution(item: Any) -> Dict[str, Any]:
    """将 JobExecution 序列化为 tool 返回结构。"""
    started_at = getattr(item, "started_at", None)
    completed_at = getattr(item, "completed_at", None)
    return {
        "id": str(getattr(item, "id", "")),
        "job_id": getattr(item, "job_id", ""),
        "status": getattr(item, "status", ""),
        "started_at": started_at.isoformat() if started_at else "",
        "completed_at": completed_at.isoformat() if completed_at else "",
        "duration_ms": getattr(item, "duration_ms", None),
        "error_message": getattr(item, "error_message", None),
        "result": getattr(item, "result", None),
    }


def _get_scheduler(context: Any) -> Any:
    """从上下文获取 scheduler。"""
    return getattr(context, "scheduler", None)


def _find_job_config(scheduler: Any, job_id: str) -> Any:
    """按 job_id 查找当前注册的 JobConfig。"""
    for job in scheduler.list_jobs():
        if getattr(job, "job_id", "") == job_id:
            return job
    return None


def _validate_scheduler_job(args: Dict[str, Any], context: Any) -> tuple[Any, str, Dict[str, Any] | None]:
    """校验 scheduler 与 job_id，并返回匹配的 JobConfig。"""
    scheduler = _get_scheduler(context)
    if not scheduler:
        return None, "", {"error": "调度器未初始化"}

    job_id = str(args.get("job_id", "")).strip()
    if not job_id:
        return scheduler, "", {"error": "job_id 不能为空"}

    job = _find_job_config(scheduler, job_id)
    if job is None:
        return scheduler, job_id, {"error": f"未知任务: {job_id}", "job_id": job_id}

    return scheduler, job_id, None


def _is_all_jobs_token(value: Any) -> bool:
    """判断参数是否表达所有 Scheduler Job。"""
    token = str(value).strip().lower()
    return token in {"*", "all", "all_jobs", "__all__", "所有", "全部"}


def _all_scheduler_job_ids(scheduler: Any) -> list[str]:
    """返回当前已注册的所有 Scheduler Job ID。"""
    return [
        str(getattr(job, "job_id", "")).strip()
        for job in scheduler.list_jobs()
        if str(getattr(job, "job_id", "")).strip()
    ]


def _normalize_job_ids(args: Dict[str, Any], scheduler: Any | None = None) -> tuple[list[str], str | None]:
    """兼容 job_ids list、job_ids string 与旧 job_id string。"""
    if args.get("all_jobs") is True:
        if scheduler is None:
            return [], "all_jobs 需要可用的 scheduler"
        job_ids = _all_scheduler_job_ids(scheduler)
        if not job_ids:
            return [], "当前没有已注册的 Scheduler Job"
        return job_ids, None

    raw_job_ids = args.get("job_ids")
    if raw_job_ids is None or raw_job_ids == "":
        raw_job_ids = args.get("job_id")

    if raw_job_ids is None or raw_job_ids == "":
        return [], "job_ids 不能为空"

    if isinstance(raw_job_ids, str):
        candidates = [raw_job_ids]
    elif isinstance(raw_job_ids, (list, tuple, set)):
        candidates = list(raw_job_ids)
    else:
        return [], "job_ids 必须是字符串列表"

    job_ids: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        job_id = str(item).strip()
        if not job_id or job_id in seen:
            continue
        if _is_all_jobs_token(job_id):
            if scheduler is None:
                return [], "all job token 需要可用的 scheduler"
            all_job_ids = _all_scheduler_job_ids(scheduler)
            if not all_job_ids:
                return [], "当前没有已注册的 Scheduler Job"
            return all_job_ids, None
        seen.add(job_id)
        job_ids.append(job_id)

    if not job_ids:
        return [], "job_ids 不能为空"
    return job_ids, None


def _build_batch_state_response(
    job_ids: list[str],
    results: list[Dict[str, Any]],
    success_status: str,
) -> Dict[str, Any]:
    """构建批量状态修改返回，保留单 Job 兼容字段。"""
    success_count = sum(1 for item in results if item.get("status") == success_status)
    failure_count = len(results) - success_count
    aggregate_status = (
        success_status
        if failure_count == 0
        else ("failed" if success_count == 0 else "partial")
    )
    response: Dict[str, Any] = {
        "job_ids": job_ids,
        "results": results,
        "success_count": success_count,
        "failure_count": failure_count,
        "status": aggregate_status,
    }
    if len(job_ids) == 1:
        response["job_id"] = job_ids[0]
        if failure_count:
            response["error"] = results[0].get("error", "操作失败")
    return response


def _change_scheduler_jobs_state(
    args: Dict[str, Any],
    context: Any,
    method_name: str,
    success_status: str,
    error_label: str,
) -> Dict[str, Any]:
    """批量修改 Scheduler Job 状态。"""
    scheduler = _get_scheduler(context)
    if not scheduler:
        return {"error": "调度器未初始化"}

    job_ids, parse_error = _normalize_job_ids(args, scheduler)
    if parse_error:
        return {"error": parse_error}

    results: list[Dict[str, Any]] = []
    for job_id in job_ids:
        if _find_job_config(scheduler, job_id) is None:
            results.append({
                "job_id": job_id,
                "status": "failed",
                "error": f"未知任务: {job_id}",
            })
            continue

        try:
            getattr(scheduler, method_name)(job_id)
        except Exception as exc:
            logger.warning("%s Scheduler Job 失败: job_id=%s error=%s", error_label, job_id, exc)
            results.append({
                "job_id": job_id,
                "status": "failed",
                "error": f"{error_label}任务失败: {type(exc).__name__}: {exc}",
            })
            continue

        results.append({
            "job_id": job_id,
            "status": success_status,
        })

    return _build_batch_state_response(job_ids, results, success_status)


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
# Scheduler Core Tools
# ===================================================================

async def list_scheduler_jobs(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查看当前 Scheduler 已注册的任务列表。"""
    scheduler = _get_scheduler(context)
    if not scheduler:
        return {"error": "调度器未初始化", "jobs": [], "total": 0}

    jobs = scheduler.list_jobs()
    return {
        "jobs": [_serialize_job_config(job, scheduler) for job in jobs],
        "total": len(jobs),
        "scheduler_started": bool(getattr(scheduler, "is_started", False)),
    }


async def trigger_scheduler_job(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """立即触发指定 Scheduler Job。"""
    scheduler, job_id, error = _validate_scheduler_job(args, context)
    if error:
        return error

    execution_id = await scheduler.trigger_now(job_id)
    return {
        "job_id": job_id,
        "execution_id": execution_id,
        "status": "triggered",
    }


async def pause_scheduler_job(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """暂停一个或多个 Scheduler Job。"""
    return _change_scheduler_jobs_state(
        args=args,
        context=context,
        method_name="pause",
        success_status="paused",
        error_label="暂停",
    )


async def resume_scheduler_job(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """恢复一个或多个 Scheduler Job。"""
    return _change_scheduler_jobs_state(
        args=args,
        context=context,
        method_name="resume",
        success_status="resumed",
        error_label="恢复",
    )


async def get_scheduler_job_detail(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查看指定 Scheduler Job 的配置与最近执行日志。"""
    scheduler, job_id, error = _validate_scheduler_job(args, context)
    if error:
        return error

    job = _find_job_config(scheduler, job_id)

    try:
        history_limit = int(args.get("history_limit", 10) or 10)
    except (TypeError, ValueError):
        return {"error": "history_limit 必须是整数", "job_id": job_id}
    history_limit = max(1, min(history_limit, 50))

    history = await scheduler.get_history(job_id, limit=history_limit)
    return {
        "job": _serialize_job_config(job, scheduler),
        "scheduler_started": bool(getattr(scheduler, "is_started", False)),
        "history_limit": history_limit,
        "executions": [_serialize_execution(item) for item in history],
    }


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
        result = await pause_scheduler_job(args, context)
        return {"action": "pause", **result}

    elif action == "resume":
        result = await resume_scheduler_job(args, context)
        return {"action": "resume", **result}

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
