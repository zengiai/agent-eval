"""评测集 Pass 结果计算测试。"""

import uuid

from backend.case_set_results.calculator import AttemptInput, CaseSetPassCalculator
from backend.case_set_results.policy import PassPolicy


def _attempt(case_id, idx, status="completed", score=90.0):
    return AttemptInput(
        run_id=str(uuid.uuid4()),
        eval_case_id=case_id,
        attempt_index=idx,
        status=status,
        score=score,
        trace_id=str(uuid.uuid4()) if score is not None else None,
    )


def test_pass_k_passes_when_any_attempt_reaches_threshold():
    case_id = uuid.uuid4()
    policy = PassPolicy(formula="pass_k", k=3, score_threshold=80)
    result = CaseSetPassCalculator(policy).calculate([
        _attempt(case_id, 1, score=70),
        _attempt(case_id, 2, score=81),
        _attempt(case_id, 3, score=60),
    ])

    assert result.status == "completed"
    assert result.passed is True
    assert result.passed_cases == 1
    assert result.case_results[0].passed_attempts == 1


def test_pass_k_fails_when_all_attempts_below_threshold():
    case_id = uuid.uuid4()
    policy = PassPolicy(formula="pass_k", k=3, score_threshold=80)
    result = CaseSetPassCalculator(policy).calculate([
        _attempt(case_id, 1, score=70),
        _attempt(case_id, 2, score=79),
        _attempt(case_id, 3, score=60),
    ])

    assert result.status == "completed"
    assert result.passed is False
    assert result.failed_cases == 1
    assert result.case_results[0].failure_reason == "score_below_threshold"


def test_pass_power_k_uses_required_pass_threshold():
    case_id = uuid.uuid4()
    policy = PassPolicy(formula="pass_power_k", k=10, score_threshold=80, power_threshold=0.9)

    result_9 = CaseSetPassCalculator(policy).calculate(
        [_attempt(case_id, idx, score=90 if idx <= 9 else 70) for idx in range(1, 11)]
    )
    result_8 = CaseSetPassCalculator(policy).calculate(
        [_attempt(case_id, idx, score=90 if idx <= 8 else 70) for idx in range(1, 11)]
    )

    assert result_9.passed is True
    assert result_9.case_results[0].required_passes == 9
    assert result_8.passed is False


def test_policy_to_config_serializes_formula_as_string():
    policy = PassPolicy(formula="pass_power_k", k=2)

    assert policy.formula_value == "pass_power_k"
    assert policy.to_config()["formula"] == "pass_power_k"


def test_failed_missing_score_and_missing_trace_are_failed_attempts():
    case_id = uuid.uuid4()
    policy = PassPolicy(formula="pass_power_k", k=3, score_threshold=80, power_threshold=1.0)
    result = CaseSetPassCalculator(policy).calculate([
        _attempt(case_id, 1, score=90),
        _attempt(case_id, 2, status="failed", score=None),
        _attempt(case_id, 3, status="completed", score=None),
    ])

    case_result = result.case_results[0]
    assert result.status == "completed"
    assert result.passed is False
    assert case_result.completed_attempts == 3
    assert case_result.passed_attempts == 1
    assert case_result.failure_reason in {"run_status_failed", "missing_score"}


def test_pending_and_insufficient_do_not_pass():
    case_id = uuid.uuid4()
    policy = PassPolicy(formula="pass_k", k=3, score_threshold=80)

    pending = CaseSetPassCalculator(policy).calculate([
        _attempt(case_id, 1, status="running", score=None),
        _attempt(case_id, 2, score=90),
        _attempt(case_id, 3, score=90),
    ])
    insufficient = CaseSetPassCalculator(policy).calculate([
        _attempt(case_id, 1, score=90),
        _attempt(case_id, 2, score=90),
    ])

    assert pending.status == "pending"
    assert pending.passed is False
    assert insufficient.status == "insufficient"
    assert insufficient.passed is False


def test_expected_case_without_attempt_is_insufficient():
    case_id = uuid.uuid4()
    policy = PassPolicy(formula="pass_k", k=1, score_threshold=80)

    result = CaseSetPassCalculator(policy).calculate([], expected_case_ids=[case_id])

    assert result.status == "insufficient"
    assert result.total_cases == 1
    assert result.insufficient_cases == 1
    assert result.case_results[0].failure_reason == "insufficient_attempts"
