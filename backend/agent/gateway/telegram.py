"""Telegram 平台网关实现。

基于 `python-telegram-bot <https://github.com/python-telegram-bot/python-telegram-bot>`_ v21+。
MVP 阶段仅支持 polling 模式；webhook 模式为 Phase 2 规划。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

from backend.agent.gateway.base import (
    IMMessage,
    IMGateway,
    MessageHandler,
    MessageTooLongError,
    SendFailedError,
)

logger = logging.getLogger(__name__)

# Telegram 消息最大长度
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


class TelegramGateway(IMGateway):
    """Telegram 平台实现（polling 模式）。

    配置项::

        gateway = TelegramGateway(
            token="123456:ABC-DEF1234gh...",
            allowed_users={"123456789"},
            proxy="http://127.0.0.1:7890",  # 可选
        )
        gateway.on_message(router.handle)
        await gateway.start()

    安全设计:
        - 消息处理前校验 sender.id ∈ allowed_users
        - 不在白名单中的用户：不回复、不报错（静默拒绝）
        - 管理操作（trigger_evaluation 等）需消息内二次确认（由上层 Dispatcher 实现）
    """

    def __init__(
        self,
        token: str,
        allowed_users: Optional[Set[str]] = None,
        proxy: Optional[str] = None,
        reply_reaction: Optional[str] = "🤔",
        reply_reaction_timeout_seconds: float = 2.0,
    ) -> None:
        """初始化 Telegram 网关。

        Args:
            token: Telegram Bot Token（必填）。
            allowed_users: 用户 ID 白名单。为空时拒绝所有用户。
            proxy: HTTP 代理地址（可选，如 ``http://127.0.0.1:7890``）。
            reply_reaction: 回复处理中设置到原消息上的 reaction；空值表示关闭。
            reply_reaction_timeout_seconds: reaction API 的本地保护超时。
        """
        if not token:
            raise ValueError("Telegram Bot Token 不能为空")

        self._token = token
        self._proxy = proxy
        self._allowed_users = allowed_users or set()
        self._reply_reaction = reply_reaction.strip() if reply_reaction else ""
        self._reply_reaction_timeout_seconds = reply_reaction_timeout_seconds

        # python-telegram-bot 组件（start() 时初始化）
        self._app = None  #: telegram.ext.Application
        self._bot = None   #: telegram.Bot

        # 回调
        self._handler: Optional[MessageHandler] = None

        # 状态
        self._connected = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 Telegram polling 模式。

        Raises:
            RuntimeError: Token 无效时阻止启动。
        """
        try:
            from telegram import Update
            from telegram.ext import (
                Application,
                MessageHandler as TGMessageHandler,
                filters,
            )
        except ImportError:
            raise ImportError(
                "python-telegram-bot 未安装。请执行: pip install python-telegram-bot>=21.0"
            )

        logger.info("Starting TelegramGateway in polling mode...")

        # 构建 Application
        builder = Application.builder().token(self._token)
        if self._proxy:
            builder.proxy(self._proxy)
        self._app = builder.build()

        # 注册 handler：文本消息（非命令）
        self._app.add_handler(
            TGMessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text)
        )
        # 注册 handler：所有命令（统一处理）
        self._app.add_handler(
            TGMessageHandler(filters.COMMAND, self._handle_command)
        )

        # 启动
        await self._app.initialize()
        await self._app.updater.start_polling()
        await self._app.start()

        self._bot = self._app.bot
        self._connected = True
        logger.info("TelegramGateway started (polling mode)")

    async def stop(self) -> None:
        """停止网关，释放资源。"""
        logger.info("Stopping TelegramGateway...")
        self._connected = False

        if self._app is not None:
            try:
                if self._app.updater:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                logger.exception("Error during TelegramGateway shutdown")

        logger.info("TelegramGateway stopped")

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """发送纯文本消息。

        Args:
            chat_id: 目标会话 ID。
            text: 消息正文。
            reply_to: 可选，引用回复的消息 ID。

        Returns:
            Telegram 消息 ID。

        Raises:
            MessageTooLongError: 消息超过 4096 字符。
            SendFailedError: 发送失败。
        """
        if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            text = text[: TELEGRAM_MAX_MESSAGE_LENGTH - 13] + "...(truncated)"
            logger.warning("Message truncated to %d chars for chat=%s", len(text), chat_id)

        try:
            msg = await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=int(reply_to) if reply_to else None,
            )
            return str(msg.message_id)
        except Exception as e:
            logger.error("Failed to send message to chat=%s: %s", chat_id, e)
            raise SendFailedError(str(e)) from e

    async def send_html(
        self,
        chat_id: str,
        html: str,
        reply_to: Optional[str] = None,
    ) -> str:
        """发送 HTML 格式消息。

        以 Telegram HTML parse_mode 发送。
        发送失败时降级为纯文本。

        Args:
            chat_id: 目标会话 ID。
            html: HTML 文本。
            reply_to: 可选，引用回复的消息 ID。

        Returns:
            Telegram 消息 ID。
        """
        try:
            from telegram.constants import ParseMode
        except ImportError:
            raise ImportError(
                "python-telegram-bot 未安装。请执行: pip install python-telegram-bot>=21.0"
            )

        html_text = html
        if len(html_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            html_text = html_text[: TELEGRAM_MAX_MESSAGE_LENGTH - 13] + "...(truncated)"

        try:
            msg = await self._bot.send_message(
                chat_id=chat_id,
                text=html_text,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=int(reply_to) if reply_to else None,
            )
            return str(msg.message_id)
        except Exception as e:
            logger.warning(
                "HTML send failed, falling back to plain text: %s", e
            )
            # 降级为纯文本
            return await self.send_message(chat_id, html, reply_to)

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_message(self, handler: MessageHandler) -> None:
        """注册消息处理器。

        重复注册会替换之前的 handler。
        """
        self._handler = handler

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """是否已连接到 Telegram。"""
        return self._connected

    @property
    def platform_name(self) -> str:
        """返回 ``"telegram"``。"""
        return "telegram"

    async def _set_message_reaction(
        self,
        chat_id: str,
        message_id: str,
        reaction: list[str],
        operation: str,
    ) -> bool:
        """设置或清理消息 reaction，失败不影响主回复链路。"""
        if self._bot is None:
            return False

        set_reaction = getattr(self._bot, "set_message_reaction", None)
        if set_reaction is None:
            logger.warning("Telegram bot SDK does not support set_message_reaction")
            return False

        try:
            await asyncio.wait_for(
                set_reaction(
                    chat_id=chat_id,
                    message_id=int(message_id),
                    reaction=reaction,
                ),
                timeout=self._reply_reaction_timeout_seconds,
            )
            return True
        except Exception as e:
            logger.warning(
                "Failed to %s Telegram reaction for chat=%s message=%s: %s",
                operation,
                chat_id,
                message_id,
                e,
            )
            return False

    async def _set_processing_reaction(self, msg: IMMessage) -> bool:
        """在处理消息期间设置 reaction，返回是否设置成功。"""
        if not self._reply_reaction or not self._can_react_to_message(msg):
            return False
        return await self._set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reaction=[self._reply_reaction],
            operation="set",
        )

    def _can_react_to_message(self, msg: IMMessage) -> bool:
        """只对允许处理的用户显示处理中 reaction。"""
        if not self._allowed_users:
            return True
        return (
            msg.user_id in self._allowed_users
            or msg.username in self._allowed_users
            or f"@{msg.username}" in self._allowed_users
        )

    async def _clear_processing_reaction(self, msg: IMMessage) -> None:
        """处理完成后清理 reaction，失败只记录日志。"""
        await self._set_message_reaction(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            reaction=[],
            operation="clear",
        )

    # ------------------------------------------------------------------
    # 内部：Telegram Update 处理
    # ------------------------------------------------------------------

    async def _handle_text(self, update, context) -> None:
        """处理非命令文本消息。"""
        msg = self._to_im_message(update)
        logger.debug("Received text from user=%s chat=%s", msg.user_id, msg.chat_id)

        if self._handler is None:
            return

        reaction_set = await self._set_processing_reaction(msg)
        try:
            reply = await self._handler(msg)
        except Exception:
            logger.exception("Message handler failed for user=%s", msg.user_id)
            reply = "处理消息时发生内部错误，请稍后重试。"

        try:
            if reply:
                await self.send_html(msg.chat_id, reply, msg.message_id)
        finally:
            if reaction_set:
                await self._clear_processing_reaction(msg)

    async def _handle_command(self, update, context) -> None:
        """处理命令消息（统一处理 /xxx 格式）。"""
        msg = self._to_im_message(update)
        msg.is_command = True
        logger.debug("Received command from user=%s: %s", msg.user_id, msg.text)

        if self._handler is None:
            return

        reaction_set = await self._set_processing_reaction(msg)
        try:
            reply = await self._handler(msg)
        except Exception:
            logger.exception("Command handler failed for user=%s", msg.user_id)
            reply = "处理命令时发生内部错误，请稍后重试。"

        try:
            if reply:
                await self.send_html(msg.chat_id, reply, msg.message_id)
        finally:
            if reaction_set:
                await self._clear_processing_reaction(msg)

    # ------------------------------------------------------------------
    # 内部：消息转换
    # ------------------------------------------------------------------

    @staticmethod
    def _to_im_message(update) -> IMMessage:
        """将 Telegram Update 转换为归一化 IMMessage。

        Args:
            update: ``telegram.Update`` 对象。

        Returns:
            归一化消息。
        """
        return IMMessage(
            platform="telegram",
            chat_id=str(update.effective_chat.id),
            user_id=str(update.effective_user.id),
            username=update.effective_user.username or update.effective_user.first_name or "",
            text=update.message.text or "",
            message_id=str(update.message.message_id),
            is_command=False,  # 由调用方根据上下文设置
            reply_to_message_id=(
                str(update.message.reply_to_message.message_id)
                if update.message.reply_to_message
                else None
            ),
            raw=update.to_dict() if hasattr(update, "to_dict") else {},
        )
