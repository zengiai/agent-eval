"""预置定时任务集合。

所有任务遵循 BaseJob 接口，可通过 JobManager.register() 注册。
"""

from backend.agent.scheduler.jobs.sampling import SamplingJob, DailySamplingJob
from backend.agent.scheduler.jobs.report import DailyReportJob
from backend.agent.scheduler.jobs.alert_check import AlertCheckJob

__all__ = [
    "SamplingJob",
    "DailySamplingJob",
    "DailyReportJob",
    "AlertCheckJob",
]
