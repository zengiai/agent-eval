"""告警 API —— 为 AgentBrain 提供告警历史查询端点。

当前为占位实现，AlertEngine 就绪后补全。
"""

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/history")
async def get_alert_history(
    severity: str = Query("", description="按严重级别筛选: info|warning|critical"),
    hours_back: int = Query(24, ge=1, le=720),
    limit: int = Query(20, ge=1, le=100),
):
    """告警历史查询（占位）。

    对应 Brain tool: ``get_alert_history``

    当前状态：AlertEngine 尚未实现，返回 501 占位。
    """
    logger.info("告警历史查询（占位）: severity=%s hours_back=%d limit=%d", severity, hours_back, limit)

    return JSONResponse(
        status_code=501,
        content={
            "alerts": [],
            "total": 0,
            "limit": limit,
            "hours_back": hours_back,
            "note": "AlertEngine 尚未实现，暂返回空列表。此端点将在告警规则引擎就绪后补全。",
        },
    )
