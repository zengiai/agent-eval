"""Agent Runtime 启动入口测试。"""

import asyncio

import pytest

from backend.agent import runtime


def test_runtime_config_requires_telegram_token(monkeypatch):
    monkeypatch.setattr(runtime.settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_ALLOWED_USERS", "123")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        runtime.RuntimeConfig.from_settings()


def test_runtime_config_requires_allowed_users(monkeypatch):
    monkeypatch.setattr(runtime.settings, "TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_ALLOWED_USERS", "")

    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USERS"):
        runtime.RuntimeConfig.from_settings()


def test_runtime_config_accepts_polling(monkeypatch):
    monkeypatch.setattr(runtime.settings, "TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_ALLOWED_USERS", "123,456")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_MODE", "polling")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_REPLY_REACTION", "👌")

    config = runtime.RuntimeConfig.from_settings()

    assert config.telegram_bot_token == "dummy"
    assert config.telegram_allowed_users == {"123", "456"}
    assert config.telegram_mode == "polling"
    assert config.telegram_reply_reaction == "👌"
    assert config.runtime_web_enabled is True
    assert config.runtime_web_host == "127.0.0.1"


def test_runtime_config_rejects_non_polling_mode(monkeypatch):
    monkeypatch.setattr(runtime.settings, "TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_ALLOWED_USERS", "123")
    monkeypatch.setattr(runtime.settings, "TELEGRAM_MODE", "webhook")

    with pytest.raises(ValueError, match="仅支持 polling"):
        runtime.RuntimeConfig.from_settings()


def test_build_scheduler_registers_default_jobs():
    scheduler = runtime.build_scheduler()

    job_ids = {job.job_id for job in scheduler.list_jobs()}

    assert job_ids == {
        "sampling.hourly",
        "sampling.daily",
        "report.daily",
        "alert.check",
    }


@pytest.mark.asyncio
async def test_run_runtime_startup_chain(monkeypatch, tmp_path):
    """覆盖 runtime 启动链路：scheduler -> gateway -> ready -> stop。"""
    events: list[str] = []
    ready_file = tmp_path / "agent_runtime.ready"
    stop_event = asyncio.Event()

    class FakeGateway:
        platform_name = "telegram"

        def __init__(self, token, allowed_users, proxy=None, reply_reaction=None):
            events.append("gateway:init")
            events.append(f"gateway:reaction:{reply_reaction}")
            self._handler = None
            self.reply_reaction = reply_reaction

        def on_message(self, handler):
            events.append("gateway:on_message")
            self._handler = handler

        async def start(self):
            events.append("gateway:start")
            stop_event.set()

        async def stop(self):
            events.append("gateway:stop")

    class FakeScheduler:
        job_count = 4

        def set_context(self, **kwargs):
            events.append("scheduler:set_context")

        async def start(self):
            events.append("scheduler:start")

        async def stop(self, wait=True):
            events.append(f"scheduler:stop:{wait}")

    class FakeEngine:
        async def dispose(self):
            events.append("engine:dispose")

    monkeypatch.setattr(runtime, "TelegramGateway", FakeGateway)
    monkeypatch.setattr(runtime, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(runtime, "build_brain", lambda config, scheduler, gateway: object())
    monkeypatch.setattr(runtime, "engine", FakeEngine())
    monkeypatch.setenv("AGENT_RUNTIME_READY_FILE", str(ready_file))

    config = runtime.RuntimeConfig(
        telegram_bot_token="dummy",
        telegram_mode="polling",
        telegram_allowed_users={"Zakiai6"},
        telegram_proxy=None,
        telegram_reply_reaction="🤔",
        dispatcher_model="qwen3.7-max",
        llm_api_key="",
        llm_base_url="https://example.invalid",
        llm_temperature=0.0,
        llm_max_retries=1,
        dispatcher_max_history=1,
        runtime_web_enabled=False,
        runtime_web_host="127.0.0.1",
        runtime_web_port=19090,
        runtime_web_debug_user="",
    )

    await runtime.run_runtime(config, stop_event=stop_event)

    assert events == [
        "gateway:init",
        "gateway:reaction:🤔",
        "gateway:on_message",
        "scheduler:set_context",
        "scheduler:start",
        "gateway:start",
        "gateway:stop",
        "scheduler:stop:True",
        "engine:dispose",
    ]
    assert not ready_file.exists()


@pytest.mark.asyncio
async def test_run_runtime_starts_runtime_web_bridge(monkeypatch, tmp_path):
    """Web bridge 必须复用 agent_runtime 内同一套 router/brain/scheduler。"""
    events: list[str] = []
    ready_file = tmp_path / "agent_runtime.ready"
    stop_event = asyncio.Event()

    class FakeGateway:
        platform_name = "telegram"

        def __init__(self, token, allowed_users, proxy=None, reply_reaction=None):
            events.append("gateway:init")

        def on_message(self, handler):
            events.append("gateway:on_message")

        async def start(self):
            events.append("gateway:start")
            stop_event.set()

        async def stop(self):
            events.append("gateway:stop")

    class FakeScheduler:
        job_count = 4
        is_started = True

        def set_context(self, **kwargs):
            events.append("scheduler:set_context")

        async def start(self):
            events.append("scheduler:start")

        async def stop(self, wait=True):
            events.append(f"scheduler:stop:{wait}")

    class FakeRuntimeWebServer:
        def __init__(self, app, host, port):
            events.append(f"runtime_web:init:{host}:{port}")

        async def start(self):
            events.append("runtime_web:start")

        async def stop(self):
            events.append("runtime_web:stop")

    class FakeEngine:
        async def dispose(self):
            events.append("engine:dispose")

    monkeypatch.setattr(runtime, "TelegramGateway", FakeGateway)
    monkeypatch.setattr(runtime, "build_scheduler", lambda: FakeScheduler())
    monkeypatch.setattr(runtime, "build_brain", lambda config, scheduler, gateway: object())
    monkeypatch.setattr(runtime, "create_runtime_web_app", lambda **kwargs: object())
    monkeypatch.setattr(runtime, "RuntimeWebServer", FakeRuntimeWebServer)
    monkeypatch.setattr(runtime, "engine", FakeEngine())
    monkeypatch.setenv("AGENT_RUNTIME_READY_FILE", str(ready_file))

    config = runtime.RuntimeConfig(
        telegram_bot_token="dummy",
        telegram_mode="polling",
        telegram_allowed_users={"Zakiai6"},
        telegram_proxy=None,
        telegram_reply_reaction="",
        dispatcher_model="qwen3.7-max",
        llm_api_key="",
        llm_base_url="https://example.invalid",
        llm_temperature=0.0,
        llm_max_retries=1,
        dispatcher_max_history=1,
        runtime_web_enabled=True,
        runtime_web_host="127.0.0.1",
        runtime_web_port=19090,
        runtime_web_debug_user="",
    )

    await runtime.run_runtime(config, stop_event=stop_event)

    assert events == [
        "gateway:init",
        "gateway:on_message",
        "scheduler:set_context",
        "scheduler:start",
        "runtime_web:init:127.0.0.1:19090",
        "runtime_web:start",
        "gateway:start",
        "runtime_web:stop",
        "gateway:stop",
        "scheduler:stop:True",
        "engine:dispose",
    ]
    assert not ready_file.exists()
