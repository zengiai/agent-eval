"""Agent 调度框架。

提供:
    - BaseJob / JobConfig / JobExecution: 任务抽象与数据类型
    - JobManager: APScheduler 封装，管理任务注册/调度/持久化
    - 预置任务: SamplingJob, DailySamplingJob, DailyReportJob, AlertCheckJob
"""

from backend.agent.scheduler.base import (
    BaseJob,
    JobConfig,
    JobExecution,
    JobLifecycle,
    JobStatus,
    TriggerType,
)
from backend.agent.scheduler.manager import JobManager

__all__ = [
    "BaseJob",
    "JobConfig",
    "JobExecution",
    "JobLifecycle",
    "JobManager",
    "JobStatus",
    "TriggerType",
]
