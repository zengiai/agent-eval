"""Agent 调度器 ORM 模型 —— agent_jobs + agent_job_executions。

与 03_SCHEDULER.md §7 的 DDL 严格对应。
"""

import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    String, Integer, Text, Boolean, DateTime, ForeignKey, Index,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.database import Base


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class AgentJob(Base):
    """调度任务注册表。

    APScheduler 通过 SQLAlchemyJobStore 自动管理此表的 APScheduler 内部字段。
    本 ORM 模型仅定义业务扩展字段（name, description, config 等），
    不与 APScheduler 内部字段冲突。
    """

    __tablename__ = "agent_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    # APScheduler 使用 job_state 等内部列；job_id 作为业务标识
    job_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="interval"
    )
    trigger_value: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="stopped"
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    config: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    executions: Mapped[List["AgentJobExecution"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_agent_jobs_status", "status"),
        Index("idx_agent_jobs_enabled", "enabled"),
    )


class AgentJobExecution(Base):
    """任务执行历史。

    每次 Job 触发时写入一条记录，用于追溯执行状态和结果。
    """

    __tablename__ = "agent_job_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    job_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("agent_jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )
    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)

    job: Mapped["AgentJob"] = relationship(back_populates="executions")

    __table_args__ = (
        Index("idx_agent_job_exec_job", "job_id", "started_at"),
    )
