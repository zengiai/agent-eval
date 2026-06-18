"""报告类 Tool Handler —— 3 个报告/分析工具。

包括版本对比、日报、告警历史查询。
所有数据通过 EvalAPIClient 调用 eval-api 获取，不再直连数据库。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from backend.agent.brain.api_client import EvalAPIClient

logger = logging.getLogger(__name__)


def _get_client(context: Any) -> EvalAPIClient:
    """从 CommandContext 创建 API 客户端。"""
    api_base_url = getattr(context, "api_base_url", "http://localhost:18000")
    return EvalAPIClient(base_url=api_base_url)


# ===================================================================
# Tool 10: compare_versions
# ===================================================================

async def compare_versions(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """对比两个 Agent 版本的评测得分。

    Returns:
        {"version_a": str, "version_b": str, "comparison": [...], "overall_delta": float, "significant": bool}
    """
    version_a = args.get("version_a", "")
    version_b = args.get("version_b", "")
    if not version_a or not version_b:
        raise ValueError("version_a 和 version_b 都是必填参数")

    client = _get_client(context)
    return await client.compare_versions(
        version_a=version_a,
        version_b=version_b,
        case_set_name=args.get("case_set_name"),
    )


# ===================================================================
# Tool 11: get_daily_report
# ===================================================================

async def get_daily_report(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取指定日期的评测日报摘要。

    Returns:
        {"date": str, "total_evals": int, "avg_score": float, "layers": {}, "alert_count": int}
    """
    client = _get_client(context)
    return await client.get_daily_report(
        date=args.get("date"),
        agent_version=args.get("agent_version"),
    )


# ===================================================================
# Tool 12: get_alert_history
# ===================================================================

async def get_alert_history(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查询历史告警记录。

    Returns:
        {"alerts": [...], "total": int, "limit": int, "hours_back": int}
    """
    client = _get_client(context)
    return await client.get_alert_history(
        severity=args.get("severity"),
        hours_back=int(args.get("hours_back", 24)),
        limit=min(int(args.get("limit", 20)), 100),
    )
