"""IM Gateway 层 — 消息收发与路由。

提供统一的 IM 平台抽象，屏蔽 Telegram / Slack / Discord 差异。

Usage::

    from backend.agent.gateway import (
        IMMessage,
        IMGateway,
        TelegramGateway,
        MessageRouter,
        RateLimiter,
        MessageHandler,
    )
"""

from backend.agent.gateway.base import (
    IMMessage,
    IMGateway,
    MessageHandler,
    MessageTooLongError,
    SendFailedError,
)
from backend.agent.gateway.ratelimit import RateLimiter
from backend.agent.gateway.router import MessageRouter, PendingAction
from backend.agent.gateway.telegram import TelegramGateway

__all__ = [
    "IMMessage",
    "IMGateway",
    "MessageHandler",
    "MessageTooLongError",
    "SendFailedError",
    "MessageRouter",
    "PendingAction",
    "RateLimiter",
    "TelegramGateway",
]
