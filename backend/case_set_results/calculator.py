"""评测集 Pass 公式计算。

该模块只处理内存数据，不访问数据库、不调用 LLM、不写入 eval_scores。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from statistics import mean
from typing import Iterable
from uuid import UUID

from backend.case_set_results.policy import PassFormula, PassPolicy


TERMINAL_RUN_STATUSES = {"completed", "failed", "timeout"}


@dataclass(frozen=True)
class AttemptInput:
    """单次 EvalRun attempt 输入。"""

    run_id: str
    eval_case_id: UUID
    attempt_index: int
    status: str
    score: float | None
    trace_id: str | None = None


@dataclass(frozen=True)
class CasePassCalculation:
    eval_case_id: UUID
    passed: bool
    attempt_count: int
    completed_attempts: int
    passed_attempts: int
    required_passes: int
    best_score: float | None
    avg_score: float | None
    attempts: list[dict]
    failure_reason: str | None
    pending: bool
    insufficient: bool


@dataclass(frozen=True)
class CaseSetCalculation:
    status: str
    passed: bool
    total_cases: int
    passed_cases: int
    failed_cases: int
    insufficient_cases: int
    case_pass_rate: float
    attempt_pass_rate: float
    case_results: list[CasePassCalculation]
    metrics: dict


class CaseSetPassCalculator:
    """评测集 Pass 公式计算器。

    线程安全说明：无共享可变状态，可并发调用。
    """

    def __init__(self, policy: PassPolicy):
        self.policy = policy

    def calculate(
        self,
        attempts: Iterable[AttemptInput],
        expected_case_ids: Iterable[UUID] | None = None,
    ) -> CaseSetCalculation:
        grouped: dict[UUID, list[AttemptInput]] = {}
        for attempt in attempts:
            grouped.setdefault(attempt.eval_case_id, []).append(attempt)

        for case_id in expected_case_ids or []:
            grouped.setdefault(case_id, [])

        case_results = [
            self._calculate_case(case_id, sorted(items, key=lambda item: item.attempt_index))
            for case_id, items in sorted(grouped.items(), key=lambda item: str(item[0]))
        ]

        total_cases = len(case_results)
        passed_cases = sum(1 for item in case_results if item.passed)
        insufficient_cases = sum(1 for item in case_results if item.insufficient)
        pending_cases = sum(1 for item in case_results if item.pending)
        failed_cases = total_cases - passed_cases - pending_cases - insufficient_cases

        total_attempts = sum(item.attempt_count for item in case_results)
        passed_attempts = sum(item.passed_attempts for item in case_results)
        case_pass_rate = round(passed_cases / total_cases, 4) if total_cases else 0.0
        attempt_pass_rate = round(passed_attempts / total_attempts, 4) if total_attempts else 0.0

        if pending_cases:
            status = "pending"
            passed = False
        elif insufficient_cases:
            status = "insufficient"
            passed = False
        else:
            status = "completed"
            passed = case_pass_rate >= self.policy.min_case_pass_rate

        return CaseSetCalculation(
            status=status,
            passed=passed,
            total_cases=total_cases,
            passed_cases=passed_cases,
            failed_cases=max(failed_cases, 0),
            insufficient_cases=insufficient_cases,
            case_pass_rate=case_pass_rate,
            attempt_pass_rate=attempt_pass_rate,
            case_results=case_results,
            metrics={
                "formula": self.policy.formula_value,
                "k": self.policy.k,
                "score_threshold": self.policy.score_threshold,
                "power_threshold": self.policy.power_threshold,
                "min_case_pass_rate": self.policy.min_case_pass_rate,
                "pending_cases": pending_cases,
                "total_attempts": total_attempts,
                "passed_attempts": passed_attempts,
            },
        )

    def _calculate_case(self, case_id: UUID, attempts: list[AttemptInput]) -> CasePassCalculation:
        required_passes = self._required_passes()
        passed_attempts = 0
        completed_attempts = 0
        scores: list[float] = []
        attempt_details: list[dict] = []
        failure_reasons: list[str] = []
        has_pending = False

        for attempt in attempts:
            attempt_passed = False
            reason = None
            score = attempt.score

            if attempt.status not in TERMINAL_RUN_STATUSES:
                has_pending = True
                reason = "run_not_terminal"
            elif attempt.status != "completed":
                completed_attempts += 1
                reason = f"run_status_{attempt.status}"
            elif score is None:
                completed_attempts += 1
                reason = "missing_score"
            else:
                completed_attempts += 1
                scores.append(score)
                attempt_passed = score >= self.policy.score_threshold
                if not attempt_passed:
                    reason = "score_below_threshold"

            if attempt_passed:
                passed_attempts += 1
            elif reason:
                failure_reasons.append(reason)

            attempt_details.append({
                "run_id": attempt.run_id,
                "trace_id": attempt.trace_id,
                "attempt_index": attempt.attempt_index,
                "status": attempt.status,
                "score": score,
                "passed": attempt_passed,
                "failure_reason": reason,
            })

        insufficient = len(attempts) < self.policy.k
        passed = (passed_attempts >= required_passes) and not has_pending and not insufficient
        failure_reason = None
        if has_pending:
            failure_reason = "pending_attempt"
        elif insufficient:
            failure_reason = "insufficient_attempts"
        elif not passed:
            failure_reason = failure_reasons[0] if failure_reasons else "not_enough_passed_attempts"

        return CasePassCalculation(
            eval_case_id=case_id,
            passed=passed,
            attempt_count=len(attempts),
            completed_attempts=completed_attempts,
            passed_attempts=passed_attempts,
            required_passes=required_passes,
            best_score=round(max(scores), 2) if scores else None,
            avg_score=round(mean(scores), 2) if scores else None,
            attempts=attempt_details,
            failure_reason=failure_reason,
            pending=has_pending,
            insufficient=insufficient,
        )

    def _required_passes(self) -> int:
        if self.policy.formula_value == PassFormula.PASS_POWER_K.value:
            return max(1, ceil(self.policy.k * self.policy.power_threshold))
        return 1
