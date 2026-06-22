"""Web Brain Console API proxy.

eval-api 不创建本地 Brain/Scheduler。这里保持 Web Console 的同源 API
路径，并把请求代理到 agent_runtime 进程内的 Web Brain bridge，从而
复用 Telegram 同一套 MessageRouter、CommandExecutor 与 JobManager。
"""

from __future__ import annotations

from urllib.parse import quote
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.config import settings

router = APIRouter(prefix="/api/brain", tags=["brain"])


class BrainChatRequest(BaseModel):
    """Web Brain 对话请求。"""

    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = Field(default=None, max_length=128)
    user_id: str = Field(default="web-debug", min_length=1, max_length=128)
    username: str = Field(default="web-debug", min_length=1, max_length=128)


class BrainChatResponse(BaseModel):
    """Web Brain 对话响应。"""

    session_id: str
    message_id: str
    reply_html: str
    latency_ms: int
    active_conversations: int


class BrainStatusResponse(BaseModel):
    """Web Brain runtime 状态。"""

    ready: bool
    active_conversations: int
    model: str
    scheduler_started: bool | None = None
    scheduler_job_count: int | None = None
    platform: str | None = None


class BrainClearResponse(BaseModel):
    """清空会话历史响应。"""

    session_id: str
    cleared: bool
    active_conversations: int


async def _call_runtime_bridge(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """调用 agent_runtime Web Brain bridge，并规范化错误。"""
    base_url = settings.agent_runtime_web_base_url.rstrip("/")
    url = f"{base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=settings.AGENT_RUNTIME_WEB_TIMEOUT) as client:
            resp = await client.request(method, url, json=json)
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"agent_runtime Web Brain 不可达 ({base_url})，请确认 agent_runtime 已启动",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail=f"agent_runtime Web Brain 请求超时 ({path})",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"agent_runtime Web Brain 代理失败: {type(exc).__name__}",
        ) from exc

    if resp.status_code >= 400:
        detail: Any = resp.text[:200]
        try:
            body = resp.json()
            detail = body.get("detail", body)
        except ValueError:
            pass
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


@router.post("/chat", response_model=BrainChatResponse)
async def chat_with_brain(req: BrainChatRequest) -> dict[str, Any]:
    """向 agent_runtime 内的真实 AgentBrain 发送一条 Web 调试消息。"""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    return await _call_runtime_bridge(
        "POST",
        "/api/brain/chat",
        json=req.model_dump(),
    )


@router.delete("/sessions/{session_id:path}", response_model=BrainClearResponse)
async def clear_brain_session(session_id: str) -> dict[str, Any]:
    """清空 agent_runtime 内指定 Web Brain 会话历史。"""
    normalized = session_id.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    return await _call_runtime_bridge(
        "DELETE",
        f"/api/brain/sessions/{quote(normalized, safe='')}",
    )


@router.get("/status", response_model=BrainStatusResponse)
async def brain_status() -> dict[str, Any]:
    """返回 agent_runtime Web Brain bridge 状态。"""
    return await _call_runtime_bridge("GET", "/api/brain/status")
