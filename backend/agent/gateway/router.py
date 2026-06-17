"""消息路由层：白名单校验、命令分发、速率限制、高风险操作确认。

MessageRouter 是 IM Gateway 与 AgentBrain 之间的桥梁：
1. 白名单校验（静默拒绝未授权用户）
2. 识别命令格式（``/`` 开头 → 直接路由到内置 handler）
3. 非命令文本 → 委托 AgentBrain 做 LLM 意图理解
4. 高风险操作二次确认（/eval、大量 /sample）
5. 格式化回复并通过 Gateway 发送
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Set

from backend.agent.gateway.base import IMMessage, IMGateway
from backend.agent.gateway.ratelimit import RateLimiter

if TYPE_CHECKING:
    from backend.agent.brain.executor import CommandExecutor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 待确认操作
# ---------------------------------------------------------------------------


@dataclass
class PendingAction:
    """高风险操作的待确认记录。

    用户发起 /eval 或大量 /sample 时生成确认 token，
    用户回复 ``confirm <token>`` 后校验并执行。
    """

    action: str
    """操作类型：``"eval"`` | ``"sample"``"""

    args: Dict[str, Any] = field(default_factory=dict)
    """操作参数，如 ``{"agent_version": "v2.3.1"}`` 或 ``{"sample_size": 30}``"""

    user_id: str = ""
    """发起者 ID"""

    chat_id: str = ""
    """发起会话 ID"""

    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    """过期时间"""


# ---------------------------------------------------------------------------
# 消息路由器
# ---------------------------------------------------------------------------


class MessageRouter:
    """消息路由层。

    职责：
    1. 白名单校验（空白名单 = 允许所有人）
    2. 速率限制
    3. ``/command`` 快速路由（绕过 LLM，直接执行）
    4. 高风险操作二次确认
    5. 自然语言 → 委托 AgentBrain 做 LLM 意图理解
    6. 格式化回复并通过 Gateway 发送

    内置命令（全部绕过 LLM）：

        /help          → 返回帮助信息
        /status        → 返回系统状态概览
        /ping          → 连通性检查
        /jobs          → 列出调度任务
        /eval <ver>    → 快捷触发评测（需二次确认）
        /sample [n]    → 快捷采样评测（n>20 需确认）
    """

    def __init__(
        self,
        gateway: IMGateway,
        brain: Optional["CommandExecutor"] = None,
        scheduler: Any = None,
        allowed_users: Optional[Set[str]] = None,
        rate_limit_per_minute: int = 30,
        confirmation_timeout: int = 60,
        enable_llm_fallback: bool = True,
    ) -> None:
        """初始化路由器。

        Args:
            gateway: IM 网关实例，用于发送回复。
            brain: CommandExecutor 实例（可为 None，此时 LLM 不可用）。
            scheduler: JobManager 实例（可为 None，此时调度功能不可用）。
            allowed_users: 用户 ID 白名单。为空时允许所有用户。
            rate_limit_per_minute: 每用户每分钟最大消息数。
            confirmation_timeout: 确认 token 有效期（秒）。
            enable_llm_fallback: 自然语言消息是否委托 LLM 意图理解。
        """
        self._gateway = gateway
        self._brain = brain
        self._scheduler = scheduler
        self._allowed_users = allowed_users or set()
        self._enable_llm = enable_llm_fallback

        # 速率限制
        self._rate_limiter = RateLimiter(max_per_minute=rate_limit_per_minute)

        # 待确认操作
        self._pending_confirmations: Dict[str, PendingAction] = {}
        self._confirmation_timeout = confirmation_timeout

        # 内置命令映射
        self._command_map: Dict[str, Callable] = {
            "help": self._cmd_help,
            "status": self._cmd_status,
            "ping": self._cmd_ping,
            "jobs": self._cmd_jobs,
            "eval": self._cmd_eval,
            "sample": self._cmd_sample,
        }

    # ------------------------------------------------------------------
    # 路由入口
    # ------------------------------------------------------------------

    async def handle(self, msg: IMMessage) -> Optional[str]:
        """路由入口：Gateway 收到消息后调用。

        Args:
            msg: 归一化后的消息。

        Returns:
            回复文本；``None`` 表示不回复（静默拒绝）。
        """
        # 1. 白名单校验（空白名单 = 不限制）
        allowed_identities = {msg.user_id, msg.username, f"@{msg.username}"}
        if self._allowed_users and self._allowed_users.isdisjoint(allowed_identities):
            logger.info(
                "Rejected message from unauthorized user=%s username=%s",
                msg.user_id,
                msg.username,
            )
            return None  # 静默拒绝

        # 2. 速率限制
        if not await self._rate_limiter.check(msg.user_id):
            return (
                "⚠️ 消息过于频繁，请稍后再试。\n"
                f"限制：每分钟最多 {self._rate_limiter._max} 条消息。"
            )

        # 3. 确认流程拦截
        if msg.text.startswith("confirm "):
            return await self._handle_confirmation(msg)

        # 4. 命令路由
        if msg.is_command and msg.text.startswith("/"):
            cmd = msg.text[1:].split()[0].lower()
            handler = self._command_map.get(cmd)
            if handler:
                logger.info("Direct command routing: /%s from user=%s", cmd, msg.user_id)
                return await handler(msg)
            else:
                logger.info("Unknown command: /%s from user=%s", cmd, msg.user_id)
                return f"未知命令: /{cmd}。输入 /help 查看可用命令。"

        # 5. LLM 意图理解（委托 AgentBrain）
        if self._enable_llm and self._brain is not None:
            logger.debug("Delegating to AgentBrain for user=%s", msg.user_id)
            try:
                return await self._brain.handle(msg)
            except Exception:
                logger.exception("AgentBrain failed for user=%s", msg.user_id)
                return "LLM 意图理解暂不可用，请使用 /help 查看可用命令。"

        # 6. 兜底
        if self._enable_llm:
            return "LLM 意图理解暂不可用，请使用 /help 查看可用命令。"
        return "抱歉，我不理解你的意思。输入 /help 查看可用命令。"

    # ------------------------------------------------------------------
    # 内置命令实现
    # ------------------------------------------------------------------

    async def _cmd_help(self, msg: IMMessage) -> str:
        """返回帮助信息。"""
        return (
            "🤖 **agent-eval 评测助手**\n\n"
            "**快速命令**（即时响应，不消耗 LLM Token）\n"
            "/help    — 显示此帮助信息\n"
            "/status  — 查看系统状态概览\n"
            "/ping    — 连通性检查\n"
            "/jobs    — 列出调度任务\n"
            "/eval <version> — 快捷触发评测（需二次确认）\n"
            "/sample [n]     — 快捷采样评测（n>20 需确认）\n\n"
            "**自然语言能力**（通过 AI 理解）\n"
            "直接输入中文即可，例如：\n"
            "• 查询评测状态、评分趋势、Trace 详情\n"
            "• 触发评测任务、手动采样评测\n"
            "• 管理后台调度任务\n"
            "• 查看日报、版本对比、弱点评分用例\n"
            "• 查询历史告警记录\n"
        )

    async def _cmd_status(self, msg: IMMessage) -> str:
        """返回系统状态概览。"""
        sched_status = "running" if (self._scheduler and self._scheduler.is_started) else "未初始化"
        return (
            '🟢 **系统状态**\n'
            f'• 网关平台: {msg.platform}\n'
            '• 评测引擎: running\n'
            f'• 调度器: {sched_status}\n'
            '• 更多详情请使用自然语言查询（如「最近评测状态」）'
        )

    async def _cmd_ping(self, msg: IMMessage) -> str:
        """连通性检查。"""
        return "🏓 pong! Agent is running."

    async def _cmd_jobs(self, msg: IMMessage) -> str:
        """列出调度任务状态。"""
        if self._scheduler is None:
            return (
                "📋 **调度任务**\n"
                "调度器尚未初始化，暂无任务信息。\n"
            )

        jobs = self._scheduler.list_jobs()
        if not jobs:
            return "📋 **调度任务**\n当前没有已注册的定时任务。"

        lines = ["📋 **调度任务**\n"]
        for j in jobs:
            trigger_desc = f"{j.trigger_type.value}={j.trigger_value}"
            status_icon = "🟢" if j.enabled else "🔴"
            lines.append(
                f"{status_icon} **{j.name}**\n"
                f"  • ID: `{j.job_id}`\n"
                f"  • 触发器: {trigger_desc}\n"
                f"  • 超时: {j.timeout_seconds}s\n"
            )
        return "\n".join(lines)

    async def _cmd_eval(self, msg: IMMessage) -> str:
        """快捷触发评测——高风险操作，需二次确认。"""
        args = msg.text[5:].strip()  # 去掉 "/eval "
        if not args:
            return "用法: /eval <version>，例如 /eval v2.3.1"

        token = self._generate_confirmation_token()
        self._pending_confirmations[token] = PendingAction(
            action="eval",
            args={"agent_version": args},
            user_id=msg.user_id,
            chat_id=msg.chat_id,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self._confirmation_timeout),
        )
        logger.info("Eval confirmation required: version=%s token=%s user=%s", args, token, msg.user_id)
        return (
            f"⚠️ 即将触发版本 **{args}** 的评测。\n"
            f"回复 `confirm {token}` 继续，{self._confirmation_timeout} 秒内有效。"
        )

    async def _cmd_sample(self, msg: IMMessage) -> str:
        """快捷采样评测。n≤20 直接执行，n>20 需确认。"""
        args = msg.text[7:].strip()  # 去掉 "/sample "
        try:
            n = int(args) if args else 10
        except ValueError:
            return f"用法: /sample [n]，n 为正整数。例如 /sample 15"

        if n <= 0:
            return "采样数必须大于 0。"

        if n > 20:
            token = self._generate_confirmation_token()
            self._pending_confirmations[token] = PendingAction(
                action="sample",
                args={"sample_size": n},
                user_id=msg.user_id,
                chat_id=msg.chat_id,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=self._confirmation_timeout),
            )
            logger.info("Sample confirmation required: n=%d token=%s user=%s", n, token, msg.user_id)
            return (
                f"⚠️ 采样量 {n} 较大，回复 `confirm {token}` 继续，"
                f"{self._confirmation_timeout} 秒内有效。"
            )

        # n ≤ 20，直接执行
        if self._brain is not None:
            try:
                return await self._brain.handle_sample(msg, n)
            except Exception:
                logger.exception("AgentBrain.handle_sample failed")
                return "采样执行失败，请稍后重试。"
        return f"✅ 采样评测已触发：n={n}（AgentBrain 尚未实现，此为占位响应）"

    # ------------------------------------------------------------------
    # 确认流程
    # ------------------------------------------------------------------

    async def _handle_confirmation(self, msg: IMMessage) -> Optional[str]:
        """处理 ``confirm <token>`` 消息。

        Returns:
            执行结果文本，或错误提示。
        """
        token = msg.text[8:].strip()  # 去掉 "confirm "
        if not token:
            return "用法: confirm <确认码>"

        pending = self._pending_confirmations.pop(token, None)
        if not pending:
            logger.info("Confirmation failed: invalid token=%s from user=%s", token, msg.user_id)
            return "❌ 确认码无效或已过期，请重新发起操作。"

        if pending.user_id != msg.user_id:
            logger.info(
                "Confirmation failed: user mismatch token=%s expected=%s got=%s",
                token, pending.user_id, msg.user_id,
            )
            return "❌ 确认码不属于当前用户。"

        if datetime.now(timezone.utc) > pending.expires_at:
            logger.info("Confirmation failed: expired token=%s", token)
            return "❌ 确认已过期，请重新发起操作。"

        # 委托 AgentBrain 执行
        logger.info("Confirmation accepted: action=%s args=%s user=%s", pending.action, pending.args, msg.user_id)

        if pending.action == "eval":
            if self._brain is not None:
                try:
                    return await self._brain.handle_eval(msg, pending.args)
                except Exception:
                    logger.exception("AgentBrain.handle_eval failed")
                    return "评测执行失败，请稍后重试。"
            return f"✅ 评测已触发：版本 {pending.args.get('agent_version', '?')}（AgentBrain 尚未实现，此为占位响应）"

        if pending.action == "sample":
            n = pending.args.get("sample_size", 0)
            if self._brain is not None:
                try:
                    return await self._brain.handle_sample(msg, n)
                except Exception:
                    logger.exception("AgentBrain.handle_sample failed")
                    return "采样执行失败，请稍后重试。"
            return f"✅ 采样评测已触发：n={n}（AgentBrain 尚未实现，此为占位响应）"

        return "✅ 操作已确认。"

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _generate_confirmation_token(self) -> str:
        """生成唯一确认 token（8 位 hex）。"""
        return uuid.uuid4().hex[:8]
