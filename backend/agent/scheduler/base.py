"""调度框架抽象基类与核心数据类型。

定义所有定时任务的统一接口和数据契约。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class TriggerType(str, Enum):
    """调度触发器类型。"""
    CRON = "cron"
    INTERVAL = "interval"
    DATE = "date"


class JobStatus(str, Enum):
    """任务执行状态。"""
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class JobLifecycle(str, Enum):
    """任务生命周期状态。"""
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class JobConfig:
    """任务注册配置。

    定义一个定时任务的元信息，由 BaseJob.get_config() 返回。
    """

    job_id: str
    """全局唯一 ID，如 ``"sampling.hourly"``"""

    name: str
    """人类可读名称，如 ``"每小时采样评测"``"""

    description: str = ""
    trigger_type: TriggerType = TriggerType.INTERVAL
    trigger_value: str = "3600"
    """cron 表达式 / 秒数 / ISO datetime"""

    enabled: bool = True
    timeout_seconds: int = 600
    """单次执行超时（秒）"""

    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobExecution:
    """单次执行记录（内存快照）。"""

    id: str
    job_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "running"
    result: Optional[Dict] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class BaseJob(ABC):
    """所有定时任务的抽象基类。

    子类需要实现:
        - :meth:`execute` → 核心任务逻辑
        - :meth:`get_config` → 任务元信息

    可选覆盖:
        - :meth:`on_error` → 自定义错误处理（默认记录日志）

    用法::

        class MyJob(BaseJob):
            def get_config(self) -> JobConfig:
                return JobConfig(job_id="my.job", name="My Job", ...)

            async def execute(self, context: Dict) -> Dict:
                session = context["db_session_factory"]()
                ...
                return {"processed": 100}
    """

    def __init__(self, config: Optional[Dict] = None) -> None:
        self._config = config or {}
        self._execution_count = 0
        self._last_error: Optional[str] = None
        self._consecutive_failures = 0

    # ── 子类必须实现 ──

    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """执行任务逻辑。

        Args:
            context: 运行时上下文，包含:
                - ``db_session_factory``: SQLAlchemy async_sessionmaker
                - ``eval_service``: EvalService 实例（可选）
                - ``config``: Agent 全局配置
                - ``logger``: logging.Logger

        Returns:
            任务结果 dict，会序列化到 agent_job_executions.result

        Raises:
            Exception: 任何未捕获异常将被 JobManager 记录并触发 on_error
        """
        ...

    @abstractmethod
    def get_config(self) -> JobConfig:
        """返回任务配置。

        Returns:
            JobConfig 实例，包含 job_id、name、trigger 等元信息。
        """
        ...

    # ── 可选覆盖 ──

    async def on_error(self, error: Exception, context: Dict[str, Any]) -> None:
        """任务执行失败时的回调。

        默认行为: 记录 ERROR 日志。可覆盖为发送 IM 告警、记录指标等。

        Args:
            error: 捕获的异常对象。
            context: 运行时上下文（与 execute 相同）。
        """
        job_logger = context.get("logger", logger)
        job_logger.error(
            "Job [%s] 执行失败 (第 %d 次): %s",
            self.get_config().job_id,
            self._execution_count + 1,
            error,
            exc_info=True,
        )

    # ── 运行时属性 ──

    @property
    def execution_count(self) -> int:
        """累计执行次数（含失败）。"""
        return self._execution_count

    @property
    def last_error(self) -> Optional[str]:
        """最近一次错误信息。"""
        return self._last_error

    @property
    def consecutive_failures(self) -> int:
        """连续失败次数。成功执行后重置为 0。"""
        return self._consecutive_failures
