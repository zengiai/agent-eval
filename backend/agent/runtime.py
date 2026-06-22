"""Agent Runtime 常驻入口。

独立承载 Telegram Gateway 与 Scheduler，避免把长连接/调度组件挂到
uvicorn --reload 进程中导致多实例 polling 或重复调度。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend.agent.brain.base import CommandContext
from backend.agent.brain.executor import CommandExecutor
from backend.agent.brain.parser import LLMIntentParser
from backend.agent.brain.registry import FunctionRegistry
from backend.agent.brain.tools import register_all
from backend.agent.gateway.router import MessageRouter
from backend.agent.gateway.telegram import TelegramGateway
from backend.agent.runtime_web import RuntimeWebServer, create_runtime_web_app
from backend.agent.scheduler.jobs import (
    AlertCheckJob,
    DailyReportJob,
    DailySamplingJob,
    SamplingJob,
)
from backend.agent.scheduler.manager import JobManager
from backend.core.config import settings
from backend.core.database import async_session_factory, engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfig:
    """Agent Runtime 启动配置。"""

    telegram_bot_token: str
    telegram_mode: str
    telegram_allowed_users: set[str]
    telegram_proxy: Optional[str]
    telegram_reply_reaction: str
    dispatcher_model: str
    llm_api_key: str
    llm_base_url: str
    llm_temperature: float
    llm_max_retries: int
    dispatcher_max_history: int
    runtime_web_enabled: bool
    runtime_web_host: str
    runtime_web_port: int
    runtime_web_debug_user: str

    @classmethod
    def from_settings(cls) -> "RuntimeConfig":
        """从全局 settings 加载配置，并对 Gateway 必需项 fail fast。"""
        token = settings.TELEGRAM_BOT_TOKEN.strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN 不能为空，无法启动 agent_runtime")

        allowed_users = settings.telegram_allowed_users_set
        if not allowed_users:
            raise ValueError("TELEGRAM_ALLOWED_USERS 不能为空，无法启动 agent_runtime")

        mode = settings.TELEGRAM_MODE.strip().lower()
        if mode != "polling":
            raise ValueError(
                f"TELEGRAM_MODE={settings.TELEGRAM_MODE!r} 当前不支持，agent_runtime 仅支持 polling"
            )

        return cls(
            telegram_bot_token=token,
            telegram_mode=mode,
            telegram_allowed_users=allowed_users,
            telegram_proxy=settings.TELEGRAM_PROXY.strip() or None,
            telegram_reply_reaction=settings.TELEGRAM_REPLY_REACTION.strip(),
            dispatcher_model=settings.DISPATCHER_MODEL,
            llm_api_key=settings.LLM_API_KEY,
            llm_base_url=settings.LLM_BASE_URL,
            llm_temperature=settings.LLM_TEMPERATURE,
            llm_max_retries=settings.LLM_MAX_RETRIES,
            dispatcher_max_history=settings.DISPATCHER_MAX_HISTORY,
            runtime_web_enabled=settings.AGENT_RUNTIME_WEB_ENABLED,
            runtime_web_host=settings.AGENT_RUNTIME_WEB_HOST,
            runtime_web_port=settings.AGENT_RUNTIME_WEB_PORT,
            runtime_web_debug_user=settings.AGENT_RUNTIME_WEB_DEBUG_USER.strip(),
        )


def configure_logging() -> None:
    """配置 runtime 进程日志。脚本层负责重定向到独立文件。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def build_scheduler() -> JobManager:
    """创建并注册预置 Scheduler 任务。"""
    scheduler = JobManager(
        session_factory=async_session_factory,
        timezone="Asia/Shanghai",
    )
    scheduler.register(SamplingJob())
    scheduler.register(DailySamplingJob())
    scheduler.register(DailyReportJob())
    scheduler.register(AlertCheckJob())
    return scheduler


def mark_ready() -> None:
    """写入 ready 文件，供启动脚本判断 Gateway/Scheduler 已完成初始化。"""
    ready_file = os.environ.get("AGENT_RUNTIME_READY_FILE", "").strip()
    if not ready_file:
        return
    path = Path(ready_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")


def clear_ready() -> None:
    """清理 ready 文件。"""
    ready_file = os.environ.get("AGENT_RUNTIME_READY_FILE", "").strip()
    if not ready_file:
        return
    try:
        Path(ready_file).unlink()
    except FileNotFoundError:
        pass


def build_brain(
    config: RuntimeConfig,
    scheduler: JobManager,
    gateway: TelegramGateway,
) -> CommandExecutor:
    """组装 AgentBrain 工具链。"""
    registry = FunctionRegistry()
    register_all(registry)

    parser = LLMIntentParser(
        registry=registry,
        model=config.dispatcher_model,
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        temperature=config.llm_temperature,
        max_retries=config.llm_max_retries,
        max_history=config.dispatcher_max_history,
    )

    def context_factory(msg) -> CommandContext:
        return CommandContext(
            user_id=msg.user_id,
            chat_id=msg.chat_id,
            username=msg.username,
            api_base_url="http://localhost:18000",
            scheduler=scheduler,
            gateway=gateway,
            llm_config={
                "model": config.dispatcher_model,
                "api_key": config.llm_api_key,
                "base_url": config.llm_base_url,
            },
            config={
                "telegram_mode": config.telegram_mode,
                "telegram_allowed_users": sorted(config.telegram_allowed_users),
            },
        )

    return CommandExecutor(
        parser=parser,
        registry=registry,
        context_factory=context_factory,
        max_history=config.dispatcher_max_history,
    )


def _runtime_web_default_user(config: RuntimeConfig) -> str:
    """选择 Web bridge 默认调试身份，确保能通过 Router 白名单。"""
    if config.runtime_web_debug_user:
        return config.runtime_web_debug_user
    return sorted(config.telegram_allowed_users)[0]


async def run_runtime(
    config: RuntimeConfig,
    stop_event: asyncio.Event | None = None,
) -> None:
    """启动 agent runtime 并阻塞直到收到退出信号。"""
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop_event.set())

    gateway = TelegramGateway(
        token=config.telegram_bot_token,
        allowed_users=config.telegram_allowed_users,
        proxy=config.telegram_proxy,
        reply_reaction=config.telegram_reply_reaction,
    )
    scheduler = build_scheduler()
    brain = build_brain(config, scheduler, gateway)
    router = MessageRouter(
        gateway=gateway,
        brain=brain,
        scheduler=scheduler,
        allowed_users=config.telegram_allowed_users,
    )
    runtime_web: RuntimeWebServer | None = None

    gateway.on_message(router.handle)
    scheduler.set_context(gateway=gateway, config={"runtime": "agent_runtime"})

    try:
        await scheduler.start()
        logger.info("Scheduler started, jobs=%d", scheduler.job_count)

        if config.runtime_web_enabled:
            runtime_web_app = create_runtime_web_app(
                message_router=router,
                brain=brain,
                scheduler=scheduler,
                default_user=_runtime_web_default_user(config),
                model=config.dispatcher_model,
            )
            runtime_web = RuntimeWebServer(
                runtime_web_app,
                host=config.runtime_web_host,
                port=config.runtime_web_port,
            )
            await runtime_web.start()

        await gateway.start()
        logger.info(
            "Agent runtime started: gateway=%s allowed_users=%d",
            gateway.platform_name,
            len(config.telegram_allowed_users),
        )
        mark_ready()

        await stop_event.wait()
        logger.info("Agent runtime shutdown signal received")
    finally:
        clear_ready()
        if runtime_web is not None:
            await runtime_web.stop()
        try:
            await gateway.stop()
        finally:
            await scheduler.stop(wait=True)
            await engine.dispose()
            logger.info("Agent runtime stopped")


def main() -> int:
    """命令行入口。"""
    configure_logging()
    try:
        config = RuntimeConfig.from_settings()
        if "--check-config" in sys.argv[1:]:
            logger.info(
                "agent_runtime 配置检查通过: mode=%s allowed_users=%d",
                config.telegram_mode,
                len(config.telegram_allowed_users),
            )
            return 0
        asyncio.run(run_runtime(config))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("agent_runtime 启动失败: %s", exc, exc_info=True)
        print(f"agent_runtime 启动失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
