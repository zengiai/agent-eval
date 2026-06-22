"""Agent Runtime Web Brain bridge 测试。"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

from backend.agent.runtime_web import create_runtime_web_app


class FakeRouter:
    def __init__(self, reply="<b>ok</b>") -> None:
        self.reply = reply
        self.messages = []

    async def handle(self, msg):
        self.messages.append(msg)
        return self.reply


class FakeBrain:
    active_conversations = 2

    def __init__(self) -> None:
        self.cleared = []

    def clear_history(self, session_id):
        self.cleared.append(session_id)
        self.active_conversations = 0


class FakeScheduler:
    is_started = True
    job_count = 4


@pytest.mark.asyncio
async def test_runtime_web_chat_uses_message_router_and_default_identity():
    router = FakeRouter(reply="<b>jobs</b>")
    brain = FakeBrain()
    app = create_runtime_web_app(
        message_router=router,
        brain=brain,
        scheduler=FakeScheduler(),
        default_user="allowed-user",
        model="test-model",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://runtime") as client:
        resp = await client.post(
            "/api/brain/chat",
            json={
                "message": "/jobs",
                "session_id": "s1",
                "user_id": "web-debug",
                "username": "web-debug",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["reply_html"] == "<b>jobs</b>"
    assert len(router.messages) == 1
    msg = router.messages[0]
    assert msg.platform == "web-debug"
    assert msg.chat_id == "s1"
    assert msg.user_id == "allowed-user"
    assert msg.username == "allowed-user"
    assert msg.text == "/jobs"
    assert msg.is_command is True


@pytest.mark.asyncio
async def test_runtime_web_chat_returns_403_when_router_silent():
    router = FakeRouter(reply=None)
    app = create_runtime_web_app(
        message_router=router,
        brain=FakeBrain(),
        scheduler=FakeScheduler(),
        default_user="allowed-user",
        model="test-model",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://runtime") as client:
        resp = await client.post(
            "/api/brain/chat",
            json={"message": "hello", "session_id": "s1"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_runtime_web_status_exposes_scheduler_state():
    app = create_runtime_web_app(
        message_router=FakeRouter(),
        brain=FakeBrain(),
        scheduler=FakeScheduler(),
        default_user="allowed-user",
        model="test-model",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://runtime") as client:
        resp = await client.get("/api/brain/status")

    assert resp.status_code == 200
    assert resp.json() == {
        "ready": True,
        "scheduler_started": True,
        "scheduler_job_count": 4,
        "active_conversations": 2,
        "model": "test-model",
        "platform": "web-debug",
    }


@pytest.mark.asyncio
async def test_runtime_web_clear_history_uses_runtime_brain():
    brain = FakeBrain()
    app = create_runtime_web_app(
        message_router=FakeRouter(),
        brain=brain,
        scheduler=FakeScheduler(),
        default_user="allowed-user",
        model="test-model",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://runtime") as client:
        resp = await client.delete("/api/brain/sessions/s1")

    assert resp.status_code == 200
    assert resp.json()["active_conversations"] == 0
    assert brain.cleared == ["s1"]


@pytest.mark.asyncio
async def test_runtime_web_clear_history_accepts_encoded_slash():
    brain = FakeBrain()
    app = create_runtime_web_app(
        message_router=FakeRouter(),
        brain=brain,
        scheduler=FakeScheduler(),
        default_user="allowed-user",
        model="test-model",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://runtime") as client:
        resp = await client.delete("/api/brain/sessions/debug%2Fsession")

    assert resp.status_code == 200
    assert brain.cleared == ["debug/session"]
