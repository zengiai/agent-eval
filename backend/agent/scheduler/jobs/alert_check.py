"""告警检查任务。"""

import logging
from typing import Any, Dict

from backend.agent.scheduler.base import BaseJob, JobConfig, TriggerType

logger = logging.getLogger(__name__)


class AlertCheckJob(BaseJob):
    """定期检查告警规则。

    每 30 分钟检查一次告警规则，触发时推送通知。
    内置规则（AlertEngine 就绪后启用）:
        - score.drop.consecutive: 连续 3 次总分下降
        - score.drop.severe: 单次总分低于历史均值 60%
        - layer.hallucination.spike: 幻觉得分 < 40
        - layer.tool.failure_rate: 工具失败率 > 20%
        - latency.spike: P95 延迟 > 2x 历史 P95
    """

    def __init__(self, config: Dict = None) -> None:
        super().__init__(config)

    def get_config(self) -> JobConfig:
        return JobConfig(
            job_id="alert.check",
            name="告警检查",
            description="每 30 分钟检查告警规则，触发时推送通知",
            trigger_type=TriggerType.INTERVAL,
            trigger_value="1800",
            timeout_seconds=120,
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """告警检查逻辑（骨架）。

        当前阶段：返回占位结果。
        AlertEngine 就绪后补全真实告警规则检查。
        """
        logger.debug("AlertCheckJob: 告警规则检查（当前为骨架实现）")

        # AlertEngine 尚未实现，返回占位
        return {
            "total_rules": 0,
            "triggered": 0,
            "details": [],
            "note": "骨架实现 — AlertEngine 就绪后补全告警规则检查",
        }
