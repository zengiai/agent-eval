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

    config = runtime.RuntimeConfig.from_settings()

    assert config.telegram_bot_token == "dummy"
    assert config.telegram_allowed_users == {"123", "456"}
    assert config.telegram_mode == "polling"


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

        def __init__(self, token, allowed_users, proxy=None):
            events.append("gateway:init")
            self._handler = None

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
        dispatcher_model="qwen3.7-max",
        llm_api_key="",
        llm_base_url="https://example.invalid",
        llm_temperature=0.0,
        llm_max_retries=1,
        dispatcher_max_history=1,
    )

    await runtime.run_runtime(config, stop_event=stop_event)

    assert events == [
        "gateway:init",
        "gateway:on_message",
        "scheduler:set_context",
        "scheduler:start",
        "gateway:start",
        "gateway:stop",
        "scheduler:stop:True",
        "engine:dispose",
    ]
    assert not ready_file.exists()
