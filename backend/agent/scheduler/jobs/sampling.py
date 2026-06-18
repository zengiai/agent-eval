"""采样评测任务 —— 从生产 Trace 中定时采样并触发评测。

TODO(BrainAPIGatewayRefactor): 此 Job 仍直连数据库查询 Trace 计数，
应在后续迭代中改为通过 EvalAPIClient 调用 eval-api 获取数据。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select, func

from backend.agent.scheduler.base import BaseJob, JobConfig, TriggerType

logger = logging.getLogger(__name__)


class SamplingJob(BaseJob):
    """每小时从 production Trace 中采样并触发评测。

    配置:
        - sampling_rate: 采样比例（默认 0.05）
        - sampling_daily_limit: 每日上限（默认 100）
        - hours_back: 采样窗口（小时，默认 1）
    """

    def __init__(self, config: Dict = None) -> None:
        super().__init__(config)
        self._sampling_rate = float(self._config.get("sampling_rate", 0.05))
        self._daily_limit = int(self._config.get("sampling_daily_limit", 100))
        self._hours_back = int(self._config.get("hours_back", 1))

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="sampling.hourly",
            name="每小时采样评测",
            description="从生产 Trace 中每小时采样并执行五层评测",
            trigger_type=TriggerType.INTERVAL,
            trigger_value=str(self._hours_back * 3600),
            timeout_seconds=1200,
            metadata={
                "sampling_rate": self._sampling_rate,
                "daily_limit": self._daily_limit,
                "hours_back": self._hours_back,
            },
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """执行采样逻辑。

        当前为骨架实现：查询 production trace 计数并记录。
        待 evaluate_trace() 链路完全就绪后补全评测触发。
        """
        db_session_factory = context.get("db_session_factory")
        if db_session_factory is None:
            return {"error": "db_session_factory 未注入", "skipped": True}

        # 计算时间窗口
        since = datetime.now(timezone.utc) - timedelta(hours=self._hours_back)

        try:
            async with db_session_factory() as session:
                # 查询 production trace 数量（轻量计数，不加载完整数据）
                from backend.core.models import Trace

                stmt = select(func.count(Trace.id)).where(
                    Trace.source == "production",
                    Trace.created_at >= since,
                )
                result = await session.execute(stmt)
                total_count = result.scalar_one()

            # 计算采样量
            sample_count = min(
                int(total_count * self._sampling_rate),
                self._daily_limit,
            )

            logger.info(
                "SamplingJob: 过去 %d 小时共 %d 条 production trace，计划采样 %d 条",
                self._hours_back, total_count, sample_count,
            )

            return {
                "total_traces": total_count,
                "sample_count": sample_count,
                "sampling_rate": self._sampling_rate,
                "window_hours": self._hours_back,
                "status": "skipped" if sample_count == 0 else "completed",
                "note": "骨架实现 — evaluate_trace() 链路就绪后补全评测触发",
            }

        except Exception as e:
            logger.exception("SamplingJob 执行失败")
            return {"error": str(e), "skipped": True}


class DailySamplingJob(BaseJob):
    """每日凌晨 2 点对过去 24 小时的 production trace 做更全面的采样。

    与 hourly 的区别:
        - 采样量更大（上限 200 条）
        - 包含更宽的采样窗口（24 小时）
    """

    def __init__(self, config: Dict = None) -> None:
        super().__init__(config)
        self._sampling_rate = float(self._config.get("sampling_rate", 0.10))
        self._daily_limit = int(self._config.get("sampling_daily_limit", 200))
        self._hours_back = int(self._config.get("hours_back", 24))

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="sampling.daily",
            name="每日全量采样评测",
            description="凌晨 2 点对过去 24 小时的 production trace 采样评测",
            trigger_type=TriggerType.CRON,
            trigger_value="0 2 * * *",
            timeout_seconds=3600,
            metadata={
                "sampling_rate": self._sampling_rate,
                "daily_limit": self._daily_limit,
                "hours_back": self._hours_back,
            },
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """每日采样执行逻辑（骨架）。"""
        db_session_factory = context.get("db_session_factory")
        if db_session_factory is None:
            return {"error": "db_session_factory 未注入", "skipped": True}

        since = datetime.now(timezone.utc) - timedelta(hours=self._hours_back)

        try:
            async with db_session_factory() as session:
                from backend.core.models import Trace

                stmt = select(func.count(Trace.id)).where(
                    Trace.source == "production",
                    Trace.created_at >= since,
                )
                result = await session.execute(stmt)
                total_count = result.scalar_one()

            sample_count = min(
                int(total_count * self._sampling_rate),
                self._daily_limit,
            )

            logger.info(
                "DailySamplingJob: 过去 24 小时共 %d 条 production trace，计划采样 %d 条",
                total_count, sample_count,
            )

            return {
                "total_traces": total_count,
                "sample_count": sample_count,
                "sampling_rate": self._sampling_rate,
                "window_hours": self._hours_back,
                "status": "skipped" if sample_count == 0 else "completed",
                "note": "骨架实现 — evaluate_trace() 链路就绪后补全评测触发",
            }

        except Exception as e:
            logger.exception("DailySamplingJob 执行失败")
            return {"error": str(e), "skipped": True}
