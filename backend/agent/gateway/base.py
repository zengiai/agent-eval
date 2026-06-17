"""IM Gateway 抽象基类与归一化消息模型。

为所有 IM 平台（Telegram、Slack、Discord 等）提供统一的消息收发接口。
上层模块（AgentBrain、Notifier）仅依赖此抽象，不感知具体平台实现。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

#: 消息处理器签名：接收 IMMessage，可选返回回复文本
MessageHandler = Callable[["IMMessage"], Awaitable[Optional[str]]]

# ---------------------------------------------------------------------------
# 归一化消息
# ---------------------------------------------------------------------------


@dataclass
class IMMessage:
    """归一化后的 IM 消息，屏蔽平台差异。

    无论来自 Telegram / Slack / Discord，
    进入系统后都以此结构传递。
    """

    platform: str
    """平台标识：``"telegram"`` | ``"slack"`` | ``"discord"``"""

    chat_id: str
    """会话标识，用于回复定位。"""

    user_id: str
    """发送者标识。"""

    username: str
    """发送者名称，用于日志/审计。"""

    text: str
    """消息正文（纯文本，Markdown 符号保留）。"""

    message_id: str
    """平台原生消息 ID，用于去重、引用回复。"""

    is_command: bool = False
    """是否为 ``/`` 开头的显式命令。"""

    reply_to_message_id: Optional[str] = None
    """回复目标消息 ID。"""

    raw: Dict[str, Any] = field(default_factory=dict)
    """原始 payload，仅调试用。"""


# ---------------------------------------------------------------------------
# 抽象网关
# ---------------------------------------------------------------------------


class IMGateway(ABC):
    """IM 网关抽象基类。

    所有具体平台实现（TelegramGateway、SlackGateway 等）
    必须继承此类并实现全部抽象方法。

    生命周期::

        gateway = TelegramGateway(...)
        gateway.on_message(my_handler)
        await gateway.start()   # 开始接收消息
        ...                     # 运行中
        await gateway.stop()    # 优雅关闭
    """

    # ---- 生命周期 ----

    @abstractmethod
    async def start(self) -> None:
        """启动网关，开始接收消息。

        具体行为因平台而异：
        - Telegram polling：启动 asyncio 轮询循环
        - Telegram webhook：注册 webhook URL + 启动 HTTP server
        - Slack：建立 WebSocket 连接

        Raises:
            RuntimeError: Token 无效或网络不可达时阻止启动。
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止网关。

        具体行为：
        - 关闭长连接 / 取消轮询任务
        - 注销 webhook
        - 等待进行中的消息处理完成（graceful shutdown）
        """
        ...

    # ---- 消息发送 ----

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """发送纯文本消息。

        Args:
            chat_id: 目标会话 ID。
            text: 消息正文（纯文本，最大 4096 字符）。
            reply_to: 可选，引用回复的消息 ID。

        Returns:
            平台返回的消息 ID。

        Raises:
            MessageTooLongError: 消息超长（> 4096）。
            SendFailedError: 发送失败（网络 / 平台限流）。
        """
        ...

    @abstractmethod
    async def send_markdown(
        self,
        chat_id: str,
        markdown: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """发送 Markdown 格式消息。

        支持的 Markdown 子集（Telegram MarkdownV2 兼容）：
        - **粗体**、*斜体*、``行内代码``
        - 无序列表、有序列表
        - [链接](url)
        - ```代码块```

        Args:
            chat_id: 目标会话 ID。
            markdown: Markdown 文本。
            reply_to: 可选，引用回复的消息 ID。

        Returns:
            平台返回的消息 ID。
        """
        ...

    # ---- 回调注册 ----

    @abstractmethod
    def on_message(self, handler: MessageHandler) -> None:
        """注册消息处理器。

        网关收到任何消息时，调用 ``handler(IMMessage)``。

        - handler 返回 ``str`` → 自动回复该消息
        - handler 返回 ``None`` → 不回复

        一个网关只能注册一个 handler（由 MessageRouter 统一管理）。
        重复注册会替换之前的 handler。
        """
        ...

    # ---- 状态查询 ----

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """网关是否处于连接状态。"""
        ...

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """返回平台名称，如 ``"telegram"``。"""
        ...


# ---------------------------------------------------------------------------
# 异常定义
# ---------------------------------------------------------------------------


class GatewayError(Exception):
    """网关层基础异常。"""


class MessageTooLongError(GatewayError):
    """消息超过平台长度限制。"""

    def __init__(self, max_length: int = 4096) -> None:
        super().__init__(f"消息超过最大长度限制 ({max_length} 字符)")
        self.max_length = max_length


class SendFailedError(GatewayError):
    """消息发送失败（网络 / 平台限流）。"""

    def __init__(self, reason: str, retry_after: Optional[int] = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after
