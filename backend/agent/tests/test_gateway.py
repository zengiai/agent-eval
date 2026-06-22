"""IM Gateway 模块单元测试。

覆盖范围：
- IMMessage 数据结构
- IMGateway 抽象基类
- RateLimiter 速率限制（异步）
- MessageRouter 路由逻辑
  - 白名单、命令、LLM fallback
  - 高风险操作确认流程（/eval、/sample）
- TelegramGateway HTML 自动回复
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.agent.gateway.base import (
    IMMessage,
    IMGateway,
    MessageTooLongError,
    SendFailedError,
)
from backend.agent.gateway.ratelimit import RateLimiter
from backend.agent.gateway.router import MessageRouter, PendingAction
from backend.agent.gateway.telegram import TelegramGateway

from .conftest import MockGateway


# ======================================================================
# IMMessage
# ======================================================================


class TestIMMessage:
    """IMMessage 数据类测试。"""

    def test_default_values(self):
        """默认字段应有合理初始值。"""
        msg = IMMessage(
            platform="telegram",
            chat_id="chat_1",
            user_id="user_1",
            username="test",
            text="hello",
            message_id="msg_1",
        )
        assert msg.is_command is False
        assert msg.reply_to_message_id is None
        assert msg.raw == {}

    def test_command_detection(self):
        """is_command 字段应正确记录命令标记。"""
        msg = IMMessage(
            platform="telegram",
            chat_id="chat_1",
            user_id="user_1",
            username="test",
            text="/ping",
            message_id="msg_1",
            is_command=True,
        )
        assert msg.is_command is True
        assert msg.text == "/ping"

    def test_reply_to_message_id(self):
        """reply_to_message_id 应正确传递引用回复目标。"""
        msg = IMMessage(
            platform="telegram",
            chat_id="chat_1",
            user_id="user_1",
            username="test",
            text="agree",
            message_id="msg_2",
            reply_to_message_id="msg_1",
        )
        assert msg.reply_to_message_id == "msg_1"

    def test_raw_payload(self):
        """raw 字段应保留原始平台数据。"""
        raw_data = {"message": {"text": "hello", "from": {"id": 123}}}
        msg = IMMessage(
            platform="telegram",
            chat_id="chat_1",
            user_id="user_1",
            username="test",
            text="hello",
            message_id="msg_1",
            raw=raw_data,
        )
        assert msg.raw == raw_data


# ======================================================================
# IMGateway ABC
# ======================================================================


class TestIMGatewayAbstract:
    """IMGateway 抽象基类测试。"""

    def test_cannot_instantiate_abstract(self):
        """不能直接实例化抽象类。"""
        with pytest.raises(TypeError):
            IMGateway()  # type: ignore[abstract]

    def test_subclass_must_implement_all_abstract(self):
        """缺少抽象方法实现时应无法实例化。"""

        class IncompleteGateway(IMGateway):
            pass

        with pytest.raises(TypeError):
            IncompleteGateway()  # type: ignore[abstract]


# ======================================================================
# MockGateway
# ======================================================================


class TestMockGateway:
    """MockGateway 测试（同时验证 IMGateway 接口契约）。"""

    async def test_lifecycle(self, mock_gateway: MockGateway):
        """start/stop 应正确切换连接状态。"""
        assert mock_gateway.is_connected is False
        await mock_gateway.start()
        assert mock_gateway.is_connected is True
        await mock_gateway.stop()
        assert mock_gateway.is_connected is False

    async def test_send_message(self, mock_gateway: MockGateway):
        """send_message 应记录消息并返回 ID。"""
        msg_id = await mock_gateway.send_message("chat_1", "hello")
        assert msg_id == "msg_1"
        assert mock_gateway.sent_messages == [("chat_1", "hello", None)]

    async def test_send_message_with_reply(self, mock_gateway: MockGateway):
        """send_message 应支持引用回复。"""
        await mock_gateway.send_message("chat_1", "reply text", reply_to="msg_0")
        assert mock_gateway.sent_messages == [("chat_1", "reply text", "msg_0")]

    async def test_send_html(self, mock_gateway: MockGateway):
        """send_html 应记录 HTML 消息。"""
        msg_id = await mock_gateway.send_html("chat_1", "<b>bold</b>")
        assert msg_id == "html_1"
        assert mock_gateway.sent_html == [("chat_1", "<b>bold</b>", None)]

    async def test_on_message_registers_handler(self, mock_gateway: MockGateway):
        """on_message 应注册消息处理器。"""
        called = []

        async def handler(msg: IMMessage) -> str | None:
            called.append(msg.text)
            return f"echo: {msg.text}"

        mock_gateway.on_message(handler)

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="test_msg", message_id="m1",
        )
        reply = await mock_gateway.simulate_message(msg)
        assert called == ["test_msg"]
        assert reply == "echo: test_msg"

    async def test_on_message_replaces_previous(self, mock_gateway: MockGateway):
        """重复注册 on_message 应替换之前的 handler。"""
        async def handler1(msg): return "h1"
        async def handler2(msg): return "h2"

        mock_gateway.on_message(handler1)
        mock_gateway.on_message(handler2)

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="t", message_id="m1",
        )
        reply = await mock_gateway.simulate_message(msg)
        assert reply == "h2"

    def test_platform_name(self, mock_gateway: MockGateway):
        """platform_name 应返回初始化值。"""
        assert mock_gateway.platform_name == "mock"

        custom = MockGateway(platform="custom")
        assert custom.platform_name == "custom"


# ======================================================================
# RateLimiter（异步版本）
# ======================================================================


class TestRateLimiter:
    """RateLimiter 速率限制器测试。"""

    async def test_allows_first_request(self):
        """首次请求应被允许。"""
        limiter = RateLimiter(max_per_minute=2)
        assert await limiter.check("user_1") is True

    async def test_allows_up_to_limit(self):
        """在限制内应全部允许。"""
        limiter = RateLimiter(max_per_minute=3)
        for _ in range(3):
            assert await limiter.check("user_1") is True

    async def test_rejects_over_limit(self):
        """超过限制应拒绝。"""
        limiter = RateLimiter(max_per_minute=2)
        assert await limiter.check("user_1") is True
        assert await limiter.check("user_1") is True
        assert await limiter.check("user_1") is False

    async def test_different_users_independent(self):
        """不同用户应独立计算限额。"""
        limiter = RateLimiter(max_per_minute=1)
        assert await limiter.check("user_a") is True
        assert await limiter.check("user_a") is False
        assert await limiter.check("user_b") is True

    async def test_bucket_cleanup(self):
        """过期记录应被惰性清理。"""
        from collections import deque

        limiter = RateLimiter(max_per_minute=1)
        # 手动制造一个"过期"记录（61 秒前）
        old_time = datetime.now(timezone.utc) - timedelta(seconds=61)
        limiter._buckets["user_1"] = deque([old_time])

        # 现在应该可以放行（旧记录被清理）
        assert await limiter.check("user_1") is True

    def test_default_max_per_minute(self):
        """默认限制为 30。"""
        limiter = RateLimiter()
        assert limiter._max == 30


# ======================================================================
# MessageRouter — 白名单
# ======================================================================


class TestMessageRouterWhitelist:
    """MessageRouter 白名单测试。"""

    async def test_allows_whitelisted_user(self, mock_gateway: MockGateway):
        """白名单用户应被允许。"""
        router = MessageRouter(mock_gateway, allowed_users={"u1"})
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="good", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "pong" in reply

    async def test_allows_whitelisted_username(self, mock_gateway: MockGateway):
        """白名单支持 Telegram username，方便本地配置。"""
        router = MessageRouter(mock_gateway, allowed_users={"Zakiai6"})
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="123456",
            username="Zakiai6", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "pong" in reply

    async def test_allows_whitelisted_at_username(self, mock_gateway: MockGateway):
        """白名单支持带 @ 的 Telegram username。"""
        router = MessageRouter(mock_gateway, allowed_users={"@Zakiai6"})
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="123456",
            username="Zakiai6", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "pong" in reply

    async def test_rejects_non_whitelisted_user(self, mock_gateway: MockGateway):
        """非白名单用户应静默拒绝。"""
        router = MessageRouter(mock_gateway, allowed_users={"u1"})
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="evil",
            username="bad", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is None  # 静默拒绝

    async def test_empty_whitelist_allows_all(self, mock_gateway: MockGateway):
        """空白名单应允许所有用户（对齐新版设计）。"""
        router = MessageRouter(mock_gateway, allowed_users=set())
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "pong" in reply


# ======================================================================
# MessageRouter — 内置命令
# ======================================================================


class TestMessageRouterCommands:
    """MessageRouter 内置命令测试。"""

    @pytest.fixture
    def router(self, mock_gateway: MockGateway) -> MessageRouter:
        return MessageRouter(mock_gateway, allowed_users={"u1"})

    async def test_ping(self, router: MessageRouter):
        """/ping 应返回 pong。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "pong" in reply

    async def test_help(self, router: MessageRouter):
        """/help 应返回帮助信息。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/help", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "/help" in reply
        assert "/status" in reply
        assert "/ping" in reply
        assert "/eval" in reply
        assert "/sample" in reply

    async def test_status(self, router: MessageRouter):
        """/status 应返回状态概览。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/status", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "系统状态" in reply

    async def test_jobs(self, router: MessageRouter):
        """/jobs 应返回调度信息（占位）。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/jobs", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "调度" in reply

    async def test_unknown_command(self, router: MessageRouter):
        """未知命令应返回提示。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/unknown", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "未知命令" in reply
        assert "/help" in reply

    async def test_command_case_insensitive(self, router: MessageRouter):
        """命令应大小写不敏感。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/PING", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "pong" in reply


# ======================================================================
# MessageRouter — LLM Fallback（brain 替代 dispatcher）
# ======================================================================


class TestMessageRouterLLMFallback:
    """MessageRouter LLM Fallback 测试。"""

    async def test_without_brain_returns_fallback(self, mock_gateway: MockGateway):
        """没有 AgentBrain 时应返回友好提示。"""
        router = MessageRouter(mock_gateway, allowed_users={"u1"}, brain=None)
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="查一下评分", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "LLM" in reply or "help" in reply

    async def test_with_brain_delegates(self, mock_gateway: MockGateway):
        """有 AgentBrain 时应委托处理。"""
        called_with = []

        class MockBrain:
            async def handle(self, msg: IMMessage) -> str:
                called_with.append(msg.text)
                return "LLM response: score is 85"

        brain = MockBrain()
        router = MessageRouter(
            mock_gateway, allowed_users={"u1"}, brain=brain,
        )
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="查一下评分", message_id="m1",
        )
        reply = await router.handle(msg)
        assert called_with == ["查一下评分"]
        assert "LLM response" in reply

    async def test_brain_exception_is_handled(self, mock_gateway: MockGateway):
        """AgentBrain 异常应被捕获并返回友好提示。"""
        class BadBrain:
            async def handle(self, msg: IMMessage) -> str:
                raise RuntimeError("boom")

        router = MessageRouter(
            mock_gateway, allowed_users={"u1"}, brain=BadBrain(),
        )
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="查一下评分", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "LLM" in reply  # 报告不可用

    async def test_llm_disabled_skips_brain(self, mock_gateway: MockGateway):
        """LLM 禁用时不调用 AgentBrain。"""
        class SpyBrain:
            def __init__(self): self.called = False
            async def handle(self, msg): self.called = True; return "x"

        brain = SpyBrain()
        router = MessageRouter(
            mock_gateway, allowed_users={"u1"}, brain=brain,
            enable_llm_fallback=False,
        )
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="hello", message_id="m1",
        )
        reply = await router.handle(msg)
        assert not brain.called
        assert "help" in reply.lower() or "不理解" in reply


# ======================================================================
# MessageRouter — 速率限制集成
# ======================================================================


class TestMessageRouterRateLimit:
    """MessageRouter 速率限制集成测试。"""

    async def test_rate_limit_exceeded(self, mock_gateway: MockGateway):
        """超过速率限制应返回提示。"""
        from collections import deque
        from datetime import datetime, timedelta, timezone

        router = MessageRouter(mock_gateway, allowed_users={"u1"})
        # 手动设置极低限制 + 预填记录
        router._rate_limiter._max = 1
        router._rate_limiter._buckets["u1"] = deque([datetime.now(timezone.utc)])

        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/ping", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "频繁" in reply


# ======================================================================
# MessageRouter — /eval 命令
# ======================================================================


class TestMessageRouterEvalCommand:
    """MessageRouter /eval 命令测试。"""

    @pytest.fixture
    def router(self, mock_gateway: MockGateway) -> MessageRouter:
        return MessageRouter(mock_gateway, allowed_users={"u1"})

    async def test_eval_without_version(self, router: MessageRouter):
        """/eval 无参数应返回用法提示。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/eval", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "用法" in reply

    async def test_eval_generates_confirmation(self, router: MessageRouter):
        """/eval <version> 应返回确认 token。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/eval v2.3.1", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "confirm" in reply
        assert "v2.3.1" in reply
        # 确认已记录到 pending_confirmations
        assert len(router._pending_confirmations) == 1


# ======================================================================
# MessageRouter — /sample 命令
# ======================================================================


class TestMessageRouterSampleCommand:
    """MessageRouter /sample 命令测试。"""

    @pytest.fixture
    def router(self, mock_gateway: MockGateway) -> MessageRouter:
        return MessageRouter(mock_gateway, allowed_users={"u1"})

    async def test_sample_small_no_confirmation(self, router: MessageRouter):
        """/sample 10 应直接执行（无确认）。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/sample 10", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        # n≤20，不触发确认
        assert "confirm" not in reply
        assert len(router._pending_confirmations) == 0

    async def test_sample_large_confirmation(self, router: MessageRouter):
        """/sample 30 应触发确认。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/sample 30", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "confirm" in reply
        assert "30" in reply
        assert len(router._pending_confirmations) == 1

    async def test_sample_default_size(self, router: MessageRouter):
        """/sample 无参数默认 n=10。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/sample", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "confirm" not in reply  # 10 ≤ 20
        assert len(router._pending_confirmations) == 0

    async def test_sample_zero_rejected(self, router: MessageRouter):
        """/sample 0 应被拒绝。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/sample 0", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "必须大于" in reply

    async def test_sample_invalid_number(self, router: MessageRouter):
        """/sample abc 应返回用法提示。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="/sample abc", message_id="m1", is_command=True,
        )
        reply = await router.handle(msg)
        assert "用法" in reply


# ======================================================================
# MessageRouter — 确认流程
# ======================================================================


class TestMessageRouterConfirmation:
    """MessageRouter 确认流程测试。"""

    @pytest.fixture
    def router(self, mock_gateway: MockGateway) -> MessageRouter:
        return MessageRouter(mock_gateway, allowed_users={"u1", "evil"})

    def _setup_pending_eval(self, router: MessageRouter, user_id: str = "u1") -> str:
        """辅助：预注册一个 eval 待确认操作，返回 token。"""
        token = router._generate_confirmation_token()
        router._pending_confirmations[token] = PendingAction(
            action="eval",
            args={"agent_version": "v2.0"},
            user_id=user_id,
            chat_id="c1",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        )
        return token

    async def test_confirmation_valid_token(self, router: MessageRouter):
        """有效 token + 匹配 user_id → 确认成功。"""
        token = self._setup_pending_eval(router)
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text=f"confirm {token}", message_id="m1",
        )
        reply = await router.handle(msg)
        assert reply is not None
        assert "暂未实现" in reply or "已触发" in reply  # 占位响应
        # token 被消耗
        assert token not in router._pending_confirmations

    async def test_confirmation_invalid_token(self, router: MessageRouter):
        """无效 token → 拒绝。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="confirm deadbeef", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "无效" in reply or "过期" in reply

    async def test_confirmation_expired(self, router: MessageRouter):
        """过期 token → 拒绝。"""
        token = router._generate_confirmation_token()
        router._pending_confirmations[token] = PendingAction(
            action="eval",
            args={"agent_version": "v2.0"},
            user_id="u1",
            chat_id="c1",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),  # 已过期
        )
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text=f"confirm {token}", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "过期" in reply

    async def test_confirmation_wrong_user(self, router: MessageRouter):
        """不同用户 token → 拒绝。"""
        token = self._setup_pending_eval(router, user_id="u1")
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="evil",
            username="evil", text=f"confirm {token}", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "不属于" in reply

    async def test_confirmation_empty_token(self, router: MessageRouter):
        """空 token 应返回用法提示。"""
        msg = IMMessage(
            platform="mock", chat_id="c1", user_id="u1",
            username="u", text="confirm ", message_id="m1",
        )
        reply = await router.handle(msg)
        assert "用法" in reply


# ======================================================================
# MessageRouter — 构造参数
# ======================================================================


class TestMessageRouterConstructor:
    """MessageRouter 构造函数参数测试。"""

    def test_rate_limit_per_minute_config(self, mock_gateway: MockGateway):
        """rate_limit_per_minute 应正确传递给 RateLimiter。"""
        router = MessageRouter(mock_gateway, rate_limit_per_minute=10)
        assert router._rate_limiter._max == 10

    def test_confirmation_timeout_default(self, mock_gateway: MockGateway):
        """默认确认超时为 60 秒。"""
        router = MessageRouter(mock_gateway)
        assert router._confirmation_timeout == 60

    def test_confirmation_timeout_custom(self, mock_gateway: MockGateway):
        """确认超时可自定义。"""
        router = MessageRouter(mock_gateway, confirmation_timeout=30)
        assert router._confirmation_timeout == 30


# ======================================================================
# PendingAction 数据类
# ======================================================================


class TestPendingAction:
    """PendingAction 数据类测试。"""

    def test_default_values(self):
        """默认值应有合理初始值。"""
        pa = PendingAction(action="eval")
        assert pa.action == "eval"
        assert pa.args == {}
        assert pa.user_id == ""
        assert pa.chat_id == ""

    def test_full_construction(self):
        """完整构造应正确存储。"""
        expires = datetime.now(timezone.utc) + timedelta(seconds=60)
        pa = PendingAction(
            action="sample",
            args={"sample_size": 30},
            user_id="u1",
            chat_id="c1",
            expires_at=expires,
        )
        assert pa.action == "sample"
        assert pa.args == {"sample_size": 30}
        assert pa.user_id == "u1"
        assert pa.chat_id == "c1"
        assert pa.expires_at == expires


# ======================================================================
# TelegramGateway 初始化
# ======================================================================


class TestTelegramGatewayInit:
    """TelegramGateway 初始化测试。"""

    def test_empty_token_raises(self):
        """空 Token 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="Token"):
            TelegramGateway(token="")

    def test_valid_token_creates_instance(self):
        """有效 Token 应成功创建实例（但不启动）。"""
        gw = TelegramGateway(token="123:abc")
        assert gw.platform_name == "telegram"
        assert gw.is_connected is False

    def test_allowed_users_default_empty(self):
        """allowed_users 默认为空 set。"""
        gw = TelegramGateway(token="123:abc")
        assert gw._allowed_users == set()

    def test_allowed_users_passed(self):
        """allowed_users 应正确传递。"""
        gw = TelegramGateway(token="123:abc", allowed_users={"u1", "u2"})
        assert gw._allowed_users == {"u1", "u2"}

    def test_default_reply_reaction_is_thinking(self):
        """默认处理中 reaction 应使用思考表情。"""
        gw = TelegramGateway(token="123:abc")
        assert gw._reply_reaction == "🤔"

    def test_command_handler_registration_accepts_all_commands(self):
        """python-telegram-bot v21 不支持 CommandHandler(None)，应使用 COMMAND filter。"""
        from telegram.ext import MessageHandler as TGMessageHandler
        from telegram.ext import filters

        handler = TGMessageHandler(filters.COMMAND, lambda *_: None)

        assert handler is not None


class _FakeTelegramMessage:
    """测试用 Telegram 消息对象。"""

    def __init__(self, message_id: int = 456) -> None:
        self.message_id = message_id


class _FakeTelegramBot:
    """测试用 Telegram Bot，记录发送与 reaction 调用。"""

    def __init__(
        self,
        reaction_error: Exception | None = None,
        html_send_error: Exception | None = None,
    ) -> None:
        self.reaction_error = reaction_error
        self.html_send_error = html_send_error
        self.send_attempts: list[dict[str, object]] = []
        self.sent_messages: list[dict[str, object]] = []
        self.reactions: list[dict[str, object]] = []
        self.events: list[tuple[str, object, object]] = []

    async def send_message(self, **kwargs) -> _FakeTelegramMessage:
        self.send_attempts.append(kwargs)
        if kwargs.get("parse_mode") is not None and self.html_send_error is not None:
            raise self.html_send_error
        self.sent_messages.append(kwargs)
        self.events.append(("send", kwargs["text"], kwargs.get("reply_to_message_id")))
        return _FakeTelegramMessage()

    async def set_message_reaction(self, **kwargs) -> bool:
        self.reactions.append(kwargs)
        self.events.append(
            ("reaction", kwargs["reaction"], kwargs.get("message_id"))
        )
        if self.reaction_error is not None:
            raise self.reaction_error
        return True


class _FakeTelegramUpdate:
    """测试用 Telegram Update，只实现网关读取的字段。"""

    def __init__(
        self,
        text: str = "hello",
        chat_id: int = 100,
        user_id: int = 200,
        message_id: int = 41,
        username: str = "tester",
    ) -> None:
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.effective_user = SimpleNamespace(
            id=user_id,
            username=username,
            first_name="Tester",
        )
        self.message = SimpleNamespace(
            text=text,
            message_id=message_id,
            reply_to_message=None,
        )

    def to_dict(self) -> dict[str, object]:
        return {"message": {"message_id": self.message.message_id}}


class TestTelegramGatewayHtml:
    """Telegram HTML 发送测试。"""

    async def test_send_html_uses_html_parse_mode(self):
        """send_html 应使用 Telegram HTML parse_mode。"""
        from telegram.constants import ParseMode

        gw = TelegramGateway(token="123:abc")
        bot = _FakeTelegramBot()
        gw._bot = bot

        msg_id = await gw.send_html("chat_1", "<b>ok</b>", reply_to="41")

        assert msg_id == "456"
        assert bot.sent_messages == [
            {
                "chat_id": "chat_1",
                "text": "<b>ok</b>",
                "parse_mode": ParseMode.HTML,
                "reply_to_message_id": 41,
            }
        ]

    async def test_send_html_falls_back_to_plain_text(self, caplog):
        """HTML 发送失败时应降级为纯文本。"""
        from telegram.constants import ParseMode

        gw = TelegramGateway(token="123:abc")
        bot = _FakeTelegramBot(html_send_error=RuntimeError("bad html"))
        gw._bot = bot
        caplog.set_level(logging.WARNING)

        msg_id = await gw.send_html("chat_1", "<b>bad", reply_to="41")

        assert msg_id == "456"
        assert bot.send_attempts[0]["parse_mode"] == ParseMode.HTML
        assert bot.sent_messages == [
            {
                "chat_id": "chat_1",
                "text": "<b>bad",
                "reply_to_message_id": 41,
            }
        ]
        assert "HTML send failed, falling back to plain text" in caplog.text


class TestTelegramGatewayReplyReaction:
    """Telegram 处理期间 reaction 测试。"""

    async def test_handle_text_sets_and_clears_reaction_around_reply(self):
        """处理消息时先设置 reaction，回复发送完成后清空 reaction。"""
        from telegram.constants import ParseMode

        gw = TelegramGateway(token="123:abc", reply_reaction="👌")
        bot = _FakeTelegramBot()
        gw._bot = bot

        async def handler(msg: IMMessage) -> str:
            assert msg.message_id == "41"
            return "ok"

        gw.on_message(handler)
        await gw._handle_text(_FakeTelegramUpdate(), None)

        assert bot.sent_messages == [
            {
                "chat_id": "100",
                "text": "ok",
                "parse_mode": ParseMode.HTML,
                "reply_to_message_id": 41,
            }
        ]
        assert bot.reactions == [
            {
                "chat_id": "100",
                "message_id": 41,
                "reaction": ["👌"],
            },
            {
                "chat_id": "100",
                "message_id": 41,
                "reaction": [],
            },
        ]
        assert bot.events == [
            ("reaction", ["👌"], 41),
            ("send", "ok", 41),
            ("reaction", [], 41),
        ]

    async def test_handle_command_replies_with_html(self):
        """命令自动回复也应使用 HTML parse_mode。"""
        from telegram.constants import ParseMode

        gw = TelegramGateway(token="123:abc", reply_reaction="")
        bot = _FakeTelegramBot()
        gw._bot = bot

        async def handler(msg: IMMessage) -> str:
            assert msg.is_command is True
            return "<b>cmd ok</b>"

        gw.on_message(handler)
        await gw._handle_command(_FakeTelegramUpdate(text="/help"), None)

        assert bot.sent_messages == [
            {
                "chat_id": "100",
                "text": "<b>cmd ok</b>",
                "parse_mode": ParseMode.HTML,
                "reply_to_message_id": 41,
            }
        ]

    async def test_handle_text_skips_reaction_when_disabled(self):
        """reply_reaction 为空时，应只发送回复，不调用 reaction API。"""
        gw = TelegramGateway(token="123:abc", reply_reaction="")
        bot = _FakeTelegramBot()
        gw._bot = bot

        async def handler(msg: IMMessage) -> str:
            return "ok"

        gw.on_message(handler)
        await gw._handle_text(_FakeTelegramUpdate(), None)

        assert bot.sent_messages[0]["reply_to_message_id"] == 41
        assert bot.reactions == []

    async def test_handle_text_skips_reaction_for_non_allowed_user(self):
        """配置白名单时，非白名单消息不显示处理中 reaction。"""
        gw = TelegramGateway(
            token="123:abc",
            allowed_users={"allowed_user"},
            reply_reaction="👌",
        )
        bot = _FakeTelegramBot()
        gw._bot = bot

        async def handler(msg: IMMessage) -> str:
            return "ok"

        gw.on_message(handler)
        await gw._handle_text(_FakeTelegramUpdate(username="blocked_user"), None)

        assert bot.sent_messages[0]["text"] == "ok"
        assert bot.reactions == []

    async def test_send_message_does_not_manage_processing_reaction(self):
        """send_message 只负责发消息，不在回复后额外设置 reaction。"""
        gw = TelegramGateway(token="123:abc", reply_reaction="👌")
        bot = _FakeTelegramBot()
        gw._bot = bot

        msg_id = await gw.send_message("chat_1", "ok", reply_to="41")

        assert msg_id == "456"
        assert bot.sent_messages[0]["reply_to_message_id"] == 41
        assert bot.reactions == []

    async def test_reaction_set_failure_does_not_fail_reply(self, caplog):
        """设置思考 reaction 失败时，主回复仍发送并记录 warning。"""
        gw = TelegramGateway(token="123:abc", reply_reaction="👍")
        bot = _FakeTelegramBot(reaction_error=RuntimeError("reaction denied"))
        gw._bot = bot
        caplog.set_level(logging.WARNING)

        async def handler(msg: IMMessage) -> str:
            return "ok"

        gw.on_message(handler)
        await gw._handle_text(_FakeTelegramUpdate(), None)

        assert bot.sent_messages[0]["text"] == "ok"
        assert bot.reactions == [
            {
                "chat_id": "100",
                "message_id": 41,
                "reaction": ["👍"],
            }
        ]
        assert "Failed to set Telegram reaction" in caplog.text


# ======================================================================
# 异常
# ======================================================================


class TestExceptions:
    """网关异常类测试。"""

    def test_message_too_long_error(self):
        """MessageTooLongError 应包含最大长度。"""
        err = MessageTooLongError()
        assert "4096" in str(err)
        assert err.max_length == 4096

    def test_message_too_long_custom(self):
        """可自定义最大长度。"""
        err = MessageTooLongError(max_length=1024)
        assert err.max_length == 1024

    def test_send_failed_error(self):
        """SendFailedError 应包含原因。"""
        err = SendFailedError("network timeout")
        assert "network timeout" in str(err)
        assert err.retry_after is None

    def test_send_failed_error_with_retry(self):
        """SendFailedError 应包含 retry_after。"""
        err = SendFailedError("rate limited", retry_after=5)
        assert err.retry_after == 5
