"""Brain HTTP API 客户端 —— 封装对 eval-api 的所有 HTTP 调用。

每个方法对应一个 Brain tool，通过 httpx 调用 eval-api (localhost:18000)。
Brain tool handler 通过此客户端获取数据，不再直连数据库。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# eval-api 默认地址（同进程内嵌部署）
DEFAULT_BASE_URL = "http://localhost:18000"


class EvalAPIClient:
    """eval-api HTTP 客户端。

    用法::

        client = EvalAPIClient(base_url="http://localhost:18000")
        status = await client.get_eval_status(agent_version="v2.3.1")
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------------
    # 查询类
    # ------------------------------------------------------------------

    async def get_eval_status(
        self,
        agent_version: Optional[str] = None,
        hours_back: int = 24,
    ) -> Dict[str, Any]:
        """评测状态概览 → GET /api/stats/overview"""
        params = {"hours_back": hours_back}
        if agent_version:
            params["agent_version"] = agent_version
        return await self._get("/api/stats/overview", params=params)

    async def query_score_trend(
        self,
        agent_version: Optional[str] = None,
        last_n: int = 5,
        layer: str = "overall",
        case_set_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """评分趋势 → GET /api/stats/trend"""
        params: Dict[str, Any] = {"last_n": last_n, "layer": layer}
        if agent_version:
            params["agent_version"] = agent_version
        if case_set_name:
            params["case_set_name"] = case_set_name
        return await self._get("/api/stats/trend", params=params)

    async def search_traces(
        self,
        query_keyword: Optional[str] = None,
        source: Optional[str] = None,
        min_score: Optional[float] = None,
        max_score: Optional[float] = None,
        status: Optional[str] = None,
        agent_version: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """搜索 Trace → GET /api/cases/traces/list"""
        params: Dict[str, Any] = {"limit": min(limit, 50)}
        if query_keyword:
            params["search"] = query_keyword
        if source:
            params["source"] = source
        if min_score is not None:
            params["min_score"] = min_score
        if max_score is not None:
            params["max_score"] = max_score
        if status:
            params["status"] = status
        if agent_version:
            params["agent_version"] = agent_version
        return await self._get("/api/cases/traces/list", params=params)

    async def get_trace_detail(self, trace_id: str) -> Dict[str, Any]:
        """Trace 详情 → GET /api/cases/traces/{trace_id}"""
        return await self._get(f"/api/cases/traces/{trace_id}")

    async def list_case_sets(
        self,
        category: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """测试用例集列表 → GET /api/case-sets"""
        params: Dict[str, Any] = {}
        if category:
            params["category"] = category
        if search:
            params["search"] = search
        return await self._get("/api/case-sets", params=params)

    async def get_weakest_cases(
        self,
        agent_version: Optional[str] = None,
        top_n: int = 10,
        layer: str = "overall",
    ) -> Dict[str, Any]:
        """弱点评分用例 → GET /api/stats/weakest-cases"""
        params: Dict[str, Any] = {"top_n": top_n, "layer": layer}
        if agent_version:
            params["agent_version"] = agent_version
        return await self._get("/api/stats/weakest-cases", params=params)

    # ------------------------------------------------------------------
    # 操作类
    # ------------------------------------------------------------------

    async def trigger_evaluation(
        self,
        agent_version: str,
        case_set_name: Optional[str] = None,
        layers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """触发评测 → POST /api/tasks/trigger"""
        body: Dict[str, Any] = {"agent_version": agent_version}
        if case_set_name:
            body["case_set_name"] = case_set_name
        if layers:
            body["layers"] = layers
        return await self._post("/api/tasks/trigger", json=body)

    async def sample_and_evaluate(
        self,
        sample_size: int = 10,
        hours_back: int = 24,
        agent_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """采样评测 → POST /api/cases/sample"""
        body: Dict[str, Any] = {
            "sample_size": sample_size,
            "hours_back": hours_back,
        }
        if agent_version:
            body["agent_version"] = agent_version
        return await self._post("/api/cases/sample", json=body)

    # ------------------------------------------------------------------
    # 报告类
    # ------------------------------------------------------------------

    async def compare_versions(
        self,
        version_a: str,
        version_b: str,
        case_set_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """版本对比 → GET /api/stats/compare"""
        params: Dict[str, Any] = {"version_a": version_a, "version_b": version_b}
        if case_set_name:
            params["case_set_name"] = case_set_name
        return await self._get("/api/stats/compare", params=params)

    async def get_daily_report(
        self,
        date: Optional[str] = None,
        agent_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """日报 → GET /api/stats/daily-report"""
        params: Dict[str, Any] = {}
        if date:
            params["date"] = date
        if agent_version:
            params["agent_version"] = agent_version
        return await self._get("/api/stats/daily-report", params=params)

    async def get_alert_history(
        self,
        severity: Optional[str] = None,
        hours_back: int = 24,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """告警历史 → GET /api/alerts/history"""
        params: Dict[str, Any] = {"hours_back": hours_back, "limit": limit}
        if severity:
            params["severity"] = severity
        return await self._get("/api/alerts/history", params=params)

    # ------------------------------------------------------------------
    # HTTP 底层
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """GET 请求，统一错误处理。"""
        url = f"{self._base_url}{path}"
        logger.debug("API GET %s params=%s", url, params)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params)
                return self._handle_response(resp)
        except httpx.TimeoutException:
            raise ValueError(f"eval-api 请求超时: GET {path}")
        except httpx.ConnectError:
            raise ValueError(f"eval-api 不可达 ({self._base_url})，请确认服务已启动")

    async def _post(self, path: str, json: Optional[Dict] = None) -> Dict[str, Any]:
        """POST 请求，统一错误处理。"""
        url = f"{self._base_url}{path}"
        logger.debug("API POST %s body=%s", url, json)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=json)
                return self._handle_response(resp)
        except httpx.TimeoutException:
            raise ValueError(f"eval-api 请求超时: POST {path}")
        except httpx.ConnectError:
            raise ValueError(f"eval-api 不可达 ({self._base_url})，请确认服务已启动")

    def _handle_response(self, resp: httpx.Response) -> Dict[str, Any]:
        """统一处理 HTTP 响应。"""
        if resp.status_code == 501:
            # 占位端点，返回原始内容（含 note）
            return resp.json()
        if resp.status_code >= 400:
            detail = "未知错误"
            try:
                body = resp.json()
                detail = body.get("detail", str(body))
            except Exception:
                detail = resp.text[:200]
            raise ValueError(f"eval-api 错误 ({resp.status_code}): {detail}")
        return resp.json()
