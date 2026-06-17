"""gateway 模块测试 fixtures。"""

import pytest

from backend.agent.gateway.base import IMMessage, IMGateway, MessageHandler
from typing import Optional


class MockGateway(IMGateway):
    """测试用 Mock 网关，不依赖真实 IM 平台。

    记录所有发送的消息，支持断言消息内容和目标。
    """

    def __init__(self, platform: str = "mock") -> None:
        self._handler: Optional[MessageHandler] = None
        self._platform = platform
        self._connected = False
        self.sent_messages: list[tuple[str, str, Optional[str]]] = []
        """记录发送的纯文本消息: [(chat_id, text, reply_to), ...]"""
        self.sent_markdown: list[tuple[str, str, Optional[str]]] = []
        """记录发送的 Markdown 消息: [(chat_id, markdown, reply_to), ...]"""

    async def start(self) -> None:
        self._connected = True

    async def stop(self) -> None:
        self._connected = False

    async def send_message(
        self, chat_id: str, text: str, reply_to: Optional[str] = None
    ) -> str:
        self.sent_messages.append((chat_id, text, reply_to))
        return f"msg_{len(self.sent_messages)}"

    async def send_markdown(
        self, chat_id: str, markdown: str, reply_to: Optional[str] = None
    ) -> str:
        self.sent_markdown.append((chat_id, markdown, reply_to))
        return f"md_{len(self.sent_markdown)}"

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def platform_name(self) -> str:
        return self._platform

    # ---- 测试辅助方法 ----

    async def simulate_message(self, msg: IMMessage) -> Optional[str]:
        """模拟收到一条消息，触发 handler 并返回回复。"""
        if self._handler:
            return await self._handler(msg)
        return None


@pytest.fixture
def mock_gateway() -> MockGateway:
    """提供一个已启动的 MockGateway。"""
    return MockGateway()


@pytest.fixture
def sample_message() -> IMMessage:
    """提供一个标准测试消息。"""
    return IMMessage(
        platform="mock",
        chat_id="chat_001",
        user_id="user_001",
        username="testuser",
        text="hello",
        message_id="msg_001",
    )
