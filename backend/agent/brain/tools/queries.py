"""查询类 Tool Handler —— 6 个评测数据查询工具。

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
# Tool 1: get_latest_eval_status
# ===================================================================

async def get_latest_eval_status(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """获取最近评测任务的全局状态概览。

    Returns:
        {"total_tasks": int, "status_counts": dict, "avg_overall_score": float,
         "active_versions": [str], "hours_back": int}
    """
    client = _get_client(context)
    return await client.get_eval_status(
        agent_version=args.get("agent_version"),
        hours_back=int(args.get("hours_back", 24)),
    )


# ===================================================================
# Tool 2: query_score_trend
# ===================================================================

async def query_score_trend(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """查询指定 Agent 版本最近 N 次评测的得分趋势。

    Returns:
        {"version": str, "layer": str, "trend": [...], "last_n": int, "delta": float}
    """
    client = _get_client(context)
    return await client.query_score_trend(
        agent_version=args.get("agent_version"),
        last_n=int(args.get("last_n", 5)),
        layer=args.get("layer", "overall"),
        case_set_name=args.get("case_set_name"),
    )


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


# ===================================================================
# Tool 6: get_weakest_cases
# ===================================================================

async def get_weakest_cases(args: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """找出当前评分最低的测试用例（退化热点）。

    Returns:
        {"cases": [...], "top_n": int, "layer": str}
    """
    client = _get_client(context)
    return await client.get_weakest_cases(
        agent_version=args.get("agent_version"),
        top_n=int(args.get("top_n", 10)),
        layer=args.get("layer", "overall"),
    )
