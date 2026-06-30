"""评测集 Pass 结果持久化服务。"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.case_set_results.calculator import AttemptInput, CaseSetPassCalculator
from backend.case_set_results.policy import PassPolicy
from backend.core.database import async_session_factory
from backend.core.models import (
    CaseSetMember,
    CaseSetEvalCaseResult,
    CaseSetEvalResult,
    EvalRun,
    EvalTask,
    Trace,
)

logger = logging.getLogger(__name__)


class CaseSetResultService:
    """评测集 Pass 结果服务。

    事务边界：每次 `recompute_for_task` 在调用者传入的 session 内完成读取、
    删除旧明细、upsert 汇总和新增明细。调用方负责 commit/rollback。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def recompute_for_task(self, task_id: uuid.UUID | str) -> CaseSetEvalResult:
        task_uuid = uuid.UUID(str(task_id))
        task = await self.session.get(EvalTask, task_uuid)
        if not task:
            raise ValueError(f"EvalTask not found: {task_id}")

        raw_policy = (task.config or {}).get("pass_policy")
        if task.case_set_id is None and raw_policy is None:
            raise ValueError(f"EvalTask is not a case-set evaluation task: {task_id}")

        policy = PassPolicy.from_config(raw_policy)
        attempts = await self._load_attempts(task_uuid)
        expected_case_ids = await self._load_expected_case_ids(task)
        calculation = CaseSetPassCalculator(policy).calculate(attempts, expected_case_ids=expected_case_ids)

        existing = (
            await self.session.execute(
                select(CaseSetEvalResult)
                .options(selectinload(CaseSetEvalResult.case_results))
                .where(CaseSetEvalResult.task_id == task_uuid)
            )
        ).scalar_one_or_none()

        result = existing or CaseSetEvalResult(task_id=task_uuid)
        result.case_set_id = task.case_set_id
        result.agent_version = task.agent_version
        result.formula = policy.formula_value
        result.k = policy.k
        result.score_threshold = policy.score_threshold
        result.power_threshold = policy.power_threshold
        result.min_case_pass_rate = policy.min_case_pass_rate
        result.status = calculation.status
        result.passed = calculation.passed
        result.total_cases = calculation.total_cases
        result.passed_cases = calculation.passed_cases
        result.failed_cases = calculation.failed_cases
        result.insufficient_cases = calculation.insufficient_cases
        result.case_pass_rate = calculation.case_pass_rate
        result.attempt_pass_rate = calculation.attempt_pass_rate
        result.metrics = calculation.metrics
        result.computed_at = datetime.utcnow()
        result.error_message = None

        if existing is None:
            self.session.add(result)
            await self.session.flush()
        else:
            await self.session.execute(
                delete(CaseSetEvalCaseResult).where(CaseSetEvalCaseResult.result_id == result.id)
            )

        for case_result in calculation.case_results:
            self.session.add(CaseSetEvalCaseResult(
                result_id=result.id,
                eval_case_id=case_result.eval_case_id,
                passed=case_result.passed,
                attempt_count=case_result.attempt_count,
                completed_attempts=case_result.completed_attempts,
                passed_attempts=case_result.passed_attempts,
                required_passes=case_result.required_passes,
                best_score=case_result.best_score,
                avg_score=case_result.avg_score,
                attempts=case_result.attempts,
                failure_reason=case_result.failure_reason,
            ))

        await self.session.flush()
        return result

    async def _load_attempts(self, task_id: uuid.UUID) -> list[AttemptInput]:
        rows = (
            await self.session.execute(
                select(EvalRun, Trace)
                .outerjoin(Trace, EvalRun.trace_id == Trace.id)
                .where(EvalRun.task_id == task_id)
                .order_by(EvalRun.eval_case_id, EvalRun.attempt_index)
            )
        ).all()

        attempts: list[AttemptInput] = []
        for run, trace in rows:
            score = float(trace.overall_score) if trace and trace.overall_score is not None else None
            attempts.append(AttemptInput(
                run_id=str(run.id),
                eval_case_id=run.eval_case_id,
                attempt_index=run.attempt_index or 1,
                status=run.status,
                score=score,
                trace_id=str(run.trace_id) if run.trace_id else None,
            ))
        return attempts

    async def _load_expected_case_ids(self, task: EvalTask) -> list[uuid.UUID]:
        if task.case_set_id:
            return list(
                (
                    await self.session.execute(
                        select(CaseSetMember.case_id).where(CaseSetMember.case_set_id == task.case_set_id)
                    )
                ).scalars().all()
            )

        return list(
            (
                await self.session.execute(
                    select(EvalRun.eval_case_id)
                    .where(EvalRun.task_id == task.id)
                    .distinct()
                )
            ).scalars().all()
        )


async def recompute_case_set_result_best_effort(task_id: uuid.UUID | str | None) -> None:
    """best-effort 重算评测集结果。

    异常处理策略：吞掉异常并记录日志，避免旁路影响主评分链路。
    """
    if not task_id:
        return
    try:
        async with async_session_factory() as session:
            service = CaseSetResultService(session)
            await service.recompute_for_task(task_id)
            await session.commit()
    except Exception:
        logger.exception("CaseSet pass result recompute failed: task=%s", task_id)
