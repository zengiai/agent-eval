"""ORM 模型 —— 全部 9 张表，与 data-model.md DDL 严格对应。"""

import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    String, Integer, Float, Text, Boolean, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB, NUMERIC
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.database import Base


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


# ============================================================
# case_sets
# ============================================================
class CaseSet(Base):
    __tablename__ = "case_sets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.0.0")
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), default=[])
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    members: Mapped[List["CaseSetMember"]] = relationship(back_populates="case_set", cascade="all, delete-orphan")
    tasks: Mapped[List["EvalTask"]] = relationship(back_populates="case_set")


# ============================================================
# case_set_members
# ============================================================
class CaseSetMember(Base):
    __tablename__ = "case_set_members"

    case_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("case_sets.id", ondelete="CASCADE"), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("eval_cases.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    case_set: Mapped["CaseSet"] = relationship(back_populates="members")
    eval_case: Mapped["EvalCase"] = relationship(back_populates="set_memberships")


# ============================================================
# eval_cases
# ============================================================
class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)

    # 期望标注
    expected_intent: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    expected_retrieval: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    expected_tools: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    expected_answer: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    gold_answer: Mapped[Optional[str]] = mapped_column(Text)

    # 来源与审核
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    source_trace_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    annotation_method: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    annotation_confidence: Mapped[Optional[float]] = mapped_column(NUMERIC(3, 2))
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    sampling_batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(100))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Trace 快照
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, default=dict)

    # 健康状态
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_avg_score: Mapped[Optional[float]] = mapped_column(NUMERIC(5, 2))
    health_status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # 元信息
    difficulty: Mapped[str] = mapped_column(String(20), default="medium")
    category: Mapped[Optional[str]] = mapped_column(String(100))
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), default=[])
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    set_memberships: Mapped[List["CaseSetMember"]] = relationship(back_populates="eval_case", cascade="all, delete-orphan")
    runs: Mapped[List["EvalRun"]] = relationship(
        back_populates="eval_case",
        primaryjoin="foreign(EvalRun.eval_case_id) == EvalCase.id",
    )

    __table_args__ = (
        Index("idx_eval_cases_source_trace", "source_trace_id"),
        Index("idx_eval_cases_difficulty", "difficulty"),
        Index("idx_eval_cases_category", "category"),
        Index("idx_eval_cases_source", "source"),
        Index("idx_eval_cases_review_status", "review_status"),
        Index("idx_eval_cases_batch", "sampling_batch_id"),
        Index("idx_eval_cases_health", "health_status"),
    )


# ============================================================
# agent_versions
# ============================================================
class AgentVersion(Base):
    __tablename__ = "agent_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    version_tag: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40))
    description: Mapped[Optional[str]] = mapped_column(Text)
    config_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB)
    deploy_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("idx_agent_versions_tag", "version_tag"),)


# ============================================================
# eval_tasks
# ============================================================
class EvalTask(Base):
    __tablename__ = "eval_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)
    case_set_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("case_sets.id"))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    summary_metrics: Mapped[Optional[dict]] = mapped_column(JSONB)
    config: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)

    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    case_set: Mapped[Optional["CaseSet"]] = relationship(back_populates="tasks")
    runs: Mapped[List["EvalRun"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    case_set_eval_result: Mapped[Optional["CaseSetEvalResult"]] = relationship(
        back_populates="task",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_eval_tasks_version", "agent_version"),
        Index("idx_eval_tasks_status", "status"),
        Index("idx_eval_tasks_created", "created_at"),
    )


# ============================================================
# eval_runs
# ============================================================
class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("eval_tasks.id", ondelete="CASCADE"), nullable=False)
    eval_case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # 不设 FK，Case 删除后 Run 保留
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    trace_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))  # 不设 FK，回填前为 NULL

    expected_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB)

    error_message: Mapped[Optional[str]] = mapped_column(Text)
    error_type: Mapped[Optional[str]] = mapped_column(String(50))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    task: Mapped["EvalTask"] = relationship(back_populates="runs")
    eval_case: Mapped["EvalCase"] = relationship(
        back_populates="runs",
        primaryjoin="foreign(EvalRun.eval_case_id) == EvalCase.id",
    )
    trace: Mapped[Optional["Trace"]] = relationship(
        back_populates="eval_run",
        primaryjoin="foreign(EvalRun.trace_id) == Trace.id",
    )
    eval_scores: Mapped[List["EvalScore"]] = relationship(back_populates="eval_run", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_eval_runs_task", "task_id"),
        Index("idx_eval_runs_task_case_attempt", "task_id", "eval_case_id", "attempt_index"),
        Index("idx_eval_runs_trace", "trace_id"),
        Index("idx_eval_runs_version", "agent_version", "created_at"),
        Index("idx_eval_runs_status", "status"),
    )


# ============================================================
# case_set_eval_results
# ============================================================
class CaseSetEvalResult(Base):
    __tablename__ = "case_set_eval_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("eval_tasks.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    case_set_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("case_sets.id"))
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)

    formula: Mapped[str] = mapped_column(String(30), nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    score_threshold: Mapped[float] = mapped_column(NUMERIC(5, 2), nullable=False)
    power_threshold: Mapped[float] = mapped_column(NUMERIC(5, 4), nullable=False)
    min_case_pass_rate: Mapped[float] = mapped_column(NUMERIC(5, 4), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_cases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    case_pass_rate: Mapped[float] = mapped_column(NUMERIC(7, 4), nullable=False, default=0)
    attempt_pass_rate: Mapped[float] = mapped_column(NUMERIC(7, 4), nullable=False, default=0)

    metrics: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    task: Mapped["EvalTask"] = relationship(back_populates="case_set_eval_result")
    case_results: Mapped[List["CaseSetEvalCaseResult"]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_case_set_eval_results_task", "task_id"),
        Index("idx_case_set_eval_results_case_set", "case_set_id"),
        Index("idx_case_set_eval_results_status", "status"),
        Index("idx_case_set_eval_results_passed", "passed"),
    )


# ============================================================
# case_set_eval_case_results
# ============================================================
class CaseSetEvalCaseResult(Base):
    __tablename__ = "case_set_eval_case_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("case_set_eval_results.id", ondelete="CASCADE"), nullable=False
    )
    eval_case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    required_passes: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    best_score: Mapped[Optional[float]] = mapped_column(NUMERIC(5, 2))
    avg_score: Mapped[Optional[float]] = mapped_column(NUMERIC(5, 2))
    attempts: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)

    result: Mapped["CaseSetEvalResult"] = relationship(back_populates="case_results")

    __table_args__ = (
        UniqueConstraint("result_id", "eval_case_id", name="uq_case_set_eval_case_result"),
        Index("idx_case_set_eval_case_results_result", "result_id"),
        Index("idx_case_set_eval_case_results_case", "eval_case_id"),
        Index("idx_case_set_eval_case_results_passed", "passed"),
    )


# ============================================================
# traces
# ============================================================
class Trace(Base):
    __tablename__ = "traces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    agent_version: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[Optional[str]] = mapped_column(String(100))

    # 输入
    query: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)

    # 输出
    final_response: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="success")

    # 来源
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="eval")
    source_ref: Mapped[Optional[str]] = mapped_column(String(255))

    # 性能
    total_latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    total_tokens: Mapped[Optional[dict]] = mapped_column(JSONB)
    total_cost_usd: Mapped[Optional[float]] = mapped_column(NUMERIC(10, 6))

    # 得分
    overall_score: Mapped[Optional[float]] = mapped_column(NUMERIC(5, 2))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    spans: Mapped[List["Span"]] = relationship(back_populates="trace", cascade="all, delete-orphan")
    eval_scores: Mapped[List["EvalScore"]] = relationship(back_populates="trace", cascade="all, delete-orphan")
    eval_run: Mapped[Optional["EvalRun"]] = relationship(
        back_populates="trace",
        uselist=False,
        primaryjoin="foreign(EvalRun.trace_id) == Trace.id",
    )

    __table_args__ = (
        Index("idx_traces_version", "agent_version", "created_at"),
        Index("idx_traces_source", "source"),
        Index("idx_traces_score", "overall_score"),
        Index("idx_traces_status", "status"),
    )


# ============================================================
# spans
# ============================================================
class Span(Base):
    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False)
    span_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    # 输入输出
    input: Mapped[Optional[dict]] = mapped_column(JSONB)
    output: Mapped[Optional[dict]] = mapped_column(JSONB)

    # 工具调用特有
    tool_name: Mapped[Optional[str]] = mapped_column(String(100))
    tool_params: Mapped[Optional[dict]] = mapped_column(JSONB)
    tool_result: Mapped[Optional[dict]] = mapped_column(JSONB)
    tool_status: Mapped[Optional[str]] = mapped_column(String(20))

    # 性能
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    tokens: Mapped[Optional[dict]] = mapped_column(JSONB)
    model: Mapped[Optional[str]] = mapped_column(String(100))

    # 得分
    score: Mapped[Optional[float]] = mapped_column(NUMERIC(5, 2))

    # 元数据
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    trace: Mapped["Trace"] = relationship(back_populates="spans")
    eval_scores: Mapped[List["EvalScore"]] = relationship(back_populates="span", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("trace_id", "span_type", "sequence"),
        Index("idx_spans_trace", "trace_id", "sequence"),
        Index("idx_spans_type", "span_type"),
        Index("idx_spans_score", "span_type", "score"),
        Index("idx_spans_tool", "tool_name"),
    )


# ============================================================
# eval_scores
# ============================================================
class EvalScore(Base):
    __tablename__ = "eval_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("traces.id", ondelete="CASCADE"), nullable=False)
    span_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("spans.id", ondelete="CASCADE"))  # 可空：Outcome 层不绑定 span
    eval_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"))

    score: Mapped[float] = mapped_column(NUMERIC(5, 2), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    evaluator_version: Mapped[Optional[str]] = mapped_column(String(50))
    judge_trace: Mapped[Optional[dict]] = mapped_column(JSONB)
    evaluation_latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    method: Mapped[Optional[str]] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    span: Mapped[Optional["Span"]] = relationship(back_populates="eval_scores")
    trace: Mapped["Trace"] = relationship(back_populates="eval_scores")
    eval_run: Mapped[Optional["EvalRun"]] = relationship(back_populates="eval_scores")

    __table_args__ = (
        Index("idx_eval_scores_trace", "trace_id"),
        Index("idx_eval_scores_span", "span_id"),
        Index("idx_eval_scores_run", "eval_run_id"),
    )
