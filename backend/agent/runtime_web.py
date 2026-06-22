"""Agent Runtime Web Brain bridge.

该模块运行在 agent_runtime 进程内，给本机 Web Console 暴露一个轻量
HTTP bridge。请求会进入同一个 MessageRouter，因此与 Telegram 共享
Brain、Scheduler、确认 token 和会话历史。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.agent.brain.executor import CommandExecutor
from backend.agent.gateway.base import IMMessage
from backend.agent.gateway.router import MessageRouter
from backend.agent.scheduler.manager import JobManager

logger = logging.getLogger(__name__)


class RuntimeBrainChatRequest(BaseModel):
    """Runtime Web Brain 对话请求。"""

    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = Field(default=None, max_length=128)
    user_id: str = Field(default="web-debug", min_length=1, max_length=128)
    username: str = Field(default="web-debug", min_length=1, max_length=128)


class RuntimeBrainChatResponse(BaseModel):
    """Runtime Web Brain 对话响应。"""

    session_id: str
    message_id: str
    reply_html: str
    latency_ms: int
    active_conversations: int


class RuntimeBrainStatusResponse(BaseModel):
    """Runtime Web Brain 状态响应。"""

    ready: bool
    scheduler_started: bool
    scheduler_job_count: int
    active_conversations: int
    model: str
    platform: str


class RuntimeBrainClearResponse(BaseModel):
    """清空 runtime Brain 会话历史响应。"""

    session_id: str
    cleared: bool
    active_conversations: int


def _normalize_debug_identity(
    req: RuntimeBrainChatRequest,
    default_user: str,
) -> tuple[str, str]:
    """将 Web 默认身份映射到 runtime 允许的调试身份。"""
    user_id = req.user_id.strip()
    username = req.username.strip()

    if not user_id or user_id == "web-debug":
        user_id = default_user
    if not username or username == "web-debug":
        username = user_id.lstrip("@") or user_id
    return user_id, username


def create_runtime_web_app(
    *,
    message_router: MessageRouter,
    brain: CommandExecutor,
    scheduler: JobManager,
    default_user: str,
    model: str,
) -> FastAPI:
    """创建 agent_runtime 进程内的 Web Brain bridge app。"""
    app = FastAPI(title="Agent Runtime Web Brain", version="0.1.0")

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/brain/status", response_model=RuntimeBrainStatusResponse)
    async def brain_status() -> RuntimeBrainStatusResponse:
        return RuntimeBrainStatusResponse(
            ready=True,
            scheduler_started=scheduler.is_started,
            scheduler_job_count=scheduler.job_count,
            active_conversations=brain.active_conversations,
            model=model,
            platform="web-debug",
        )

    @app.post("/api/brain/chat", response_model=RuntimeBrainChatResponse)
    async def chat_with_brain(req: RuntimeBrainChatRequest) -> RuntimeBrainChatResponse:
        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message 不能为空")

        session_id = (req.session_id or "").strip() or f"web-{uuid.uuid4().hex[:12]}"
        user_id, username = _normalize_debug_identity(req, default_user)
        message_id = f"web-msg-{uuid.uuid4().hex[:12]}"
        msg = IMMessage(
            platform="web-debug",
            chat_id=session_id,
            user_id=user_id,
            username=username,
            text=message,
            message_id=message_id,
            is_command=message.startswith("/"),
            raw={"gateway": "runtime-web"},
        )

        started = time.perf_counter()
        try:
            reply = await message_router.handle(msg)
        except Exception as exc:
            logger.exception("Runtime Web Brain chat failed: session_id=%s", session_id)
            raise HTTPException(status_code=500, detail=f"Brain 调用失败: {type(exc).__name__}") from exc

        if reply is None:
            raise HTTPException(status_code=403, detail="Web Brain 用户未授权或 Router 未返回回复")

        return RuntimeBrainChatResponse(
            session_id=session_id,
            message_id=message_id,
            reply_html=reply,
            latency_ms=int((time.perf_counter() - started) * 1000),
            active_conversations=brain.active_conversations,
        )

    @app.delete("/api/brain/sessions/{session_id:path}", response_model=RuntimeBrainClearResponse)
    async def clear_brain_session(session_id: str) -> RuntimeBrainClearResponse:
        normalized = session_id.strip()
        if not normalized:
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        brain.clear_history(normalized)
        return RuntimeBrainClearResponse(
            session_id=normalized,
            cleared=True,
            active_conversations=brain.active_conversations,
        )

    return app


class RuntimeWebServer:
    """agent_runtime 内嵌 uvicorn server 生命周期封装。"""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        self._host = host
        self._port = port
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        # runtime 自己管理 SIGINT/SIGTERM，避免 uvicorn 覆盖 signal handler。
        self._server.install_signal_handlers = lambda: None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """后台启动 runtime Web bridge。"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._server.serve(),
            name="agent-runtime-web",
        )
        logger.info("Runtime Web Brain bridge starting on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """停止 runtime Web bridge。"""
        if not self._task:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Runtime Web Brain bridge stop timeout")
        finally:
            self._task = None
