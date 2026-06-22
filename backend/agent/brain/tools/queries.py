"""查询类 Tool Handler —— 基础 trace 和 case 查询工具。

每个 handler 通过 EvalAPIClient 调用 eval-api 获取数据，
不再直接连接数据库。handler 保留参数校验，将请求转发给 API 客户端。
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
# Tool 1: list_cases
# ===================================================================

async def list_cases(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查询评测用例列表，支持多条件筛选。

    Returns:
        {"total": int, "items": [...]}
    """
    client = _get_client(context)
    return await client.list_cases(
        source=args.get("source"),
        category=args.get("category"),
        difficulty=args.get("difficulty"),
        review_status=args.get("review_status"),
        health_status=args.get("health_status"),
        search=args.get("search"),
        limit=int(args.get("limit", 20)),
    )


# ===================================================================
# Tool 2: get_case_detail
# ===================================================================

async def get_case_detail(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取单个评测用例的完整详情及评分历史。

    Returns:
        {"case": dict, "scores": [...]}
    """
    case_id = args.get("case_id", "")
    if not case_id:
        raise ValueError("case_id 是必填参数")

    client = _get_client(context)
    return await client.get_case_detail(case_id)


# ===================================================================
# Tool 3: search_traces
# ===================================================================

async def search_traces(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """按关键词/来源/分数范围搜索 Trace 记录。

    Returns:
        {"traces": [...], "total": int, "limit": int}
    """
    client = _get_client(context)
    return await client.search_traces(
        query_keyword=args.get("query_keyword"),
        source=args.get("source"),
        min_score=args.get("min_score"),
        max_score=args.get("max_score"),
        status=args.get("status"),
        agent_version=args.get("agent_version"),
        limit=min(int(args.get("limit", 10)), 50),
    )


# ===================================================================
# Tool 4: get_trace_detail
# ===================================================================

async def get_trace_detail(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取指定 Trace 的完整详情。

    Returns:
        {"trace": dict, "spans": [...], "eval_scores": [...]}
    """
    trace_id_raw = args.get("trace_id", "")
    if not trace_id_raw:
        raise ValueError("trace_id 是必填参数")

    client = _get_client(context)
    return await client.get_trace_detail(trace_id_raw)


# ===================================================================
# Tool 5: list_case_sets
# ===================================================================

async def list_case_sets(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """列出当前可用的测试用例集。

    Returns:
        {"case_sets": [...], "total": int}
    """
    client = _get_client(context)
    return await client.list_case_sets(
        category=args.get("category"),
        search=args.get("search"),
    )
