"""测试用例集 API —— 为 AgentBrain 提供用例集查询端点。"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import get_db
from backend.core.models import CaseSet

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/case-sets", tags=["case-sets"])


@router.get("")
async def list_case_sets(
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """列出测试用例集。

    对应 Brain tool: ``list_case_sets``
    """
    stmt = select(CaseSet).order_by(CaseSet.name)

    if category:
        stmt = stmt.where(CaseSet.category == category)
    if search:
        stmt = stmt.where(CaseSet.name.ilike(f"%{search}%"))

    result = await db.execute(stmt)
    case_sets = result.scalars().all()

    sets_list = [
        {
            "id": str(cs.id)[:8],
            "name": cs.name,
            "description": cs.description or "",
            "category": cs.category or "",
            "case_count": cs.case_count,
            "version": cs.version,
            "created_at": cs.created_at.isoformat() if cs.created_at else "",
        }
        for cs in case_sets
    ]

    return {"case_sets": sets_list, "total": len(sets_list)}
