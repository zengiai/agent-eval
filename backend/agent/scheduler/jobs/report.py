"""报告生成任务。

TODO(BrainAPIGatewayRefactor): 此 Job 仍直连数据库查询 Trace/EvalScore 统计，
应在后续迭代中改为通过 EvalAPIClient 调用 eval-api 获取数据。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import Integer, select, func

from backend.agent.scheduler.base import BaseJob, JobConfig, TriggerType

logger = logging.getLogger(__name__)


class DailyReportJob(BaseJob):
    """每日评测报告生成。

    每天早上 8 点生成前一日评测摘要。
    报告内容:
        - 过去 24 小时评测总量
        - 各层平均得分
        - Token 消耗统计
    """

    def __init__(self, config: Dict = None) -> None:
        super().__init__(config)

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="report.daily",
            name="每日报告",
            description="每天早上 8 点生成前一日评测摘要",
            trigger_type=TriggerType.CRON,
            trigger_value="0 8 * * *",
            timeout_seconds=300,
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """生成日报摘要（骨架）。

        当前阶段：查询过去 24 小时评测统计数据。
        PushService 就绪后补全推送能力。
        """
        db_session_factory = context.get("db_session_factory")
        if db_session_factory is None:
            return {"error": "db_session_factory 未注入", "skipped": True}

        since = datetime.now(timezone.utc) - timedelta(hours=24)

        try:
            async with db_session_factory() as session:
                from backend.core.models import Trace, EvalScore

                # 评测总量
                trace_stmt = select(func.count(Trace.id)).where(
                    Trace.created_at >= since,
                )
                result = await session.execute(trace_stmt)
                total_evaluations = result.scalar_one()

                # Token 消耗（汇总）
                token_stmt = select(func.sum(
                    func.coalesce(
                        Trace.total_tokens["total"].astext.cast(Integer),
                        0,
                    )
                )).where(Trace.created_at >= since)
                result = await session.execute(token_stmt)
                total_tokens = result.scalar_one() or 0

            logger.info(
                "DailyReportJob: 过去 24 小时评测 %d 条，Token 消耗 %d",
                total_evaluations, total_tokens,
            )

            return {
                "period": "24h",
                "total_evaluations": total_evaluations,
                "total_tokens": total_tokens,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "note": "骨架实现 — PushService 就绪后补全推送",
            }

        except Exception as e:
            logger.exception("DailyReportJob 执行失败")
            return {"error": str(e), "skipped": True}
