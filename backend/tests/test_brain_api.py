"""Web Brain Console eval-api proxy 测试。"""

from __future__ import annotations

import httpx
from httpx import ASGITransport, AsyncClient
import pytest

from backend.api import app
from backend.api import brain as brain_api


@pytest.mark.asyncio
async def test_chat_with_brain_proxies_to_runtime(monkeypatch):
    calls = []

    async def fake_call(method, path, *, json=None):
        calls.append((method, path, json))
        return {
            "session_id": json["session_id"],
            "message_id": "runtime-msg-1",
            "reply_html": "<b>jobs</b>",
            "latency_ms": 12,
            "active_conversations": 2,
        }

    monkeypatch.setattr(brain_api, "_call_runtime_bridge", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/brain/chat",
            json={
                "message": " /jobs ",
                "session_id": "debug-session",
                "user_id": "web-debug",
                "username": "web-debug",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["reply_html"] == "<b>jobs</b>"
    assert calls == [
        (
            "POST",
            "/api/brain/chat",
            {
                "message": " /jobs ",
                "session_id": "debug-session",
                "user_id": "web-debug",
                "username": "web-debug",
            },
        )
    ]


@pytest.mark.asyncio
async def test_chat_with_blank_message_rejects_before_runtime(monkeypatch):
    called = False

    async def fake_call(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(brain_api, "_call_runtime_bridge", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/brain/chat",
            json={"message": "   ", "session_id": "debug-session"},
        )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "message 不能为空"
    assert called is False


@pytest.mark.asyncio
async def test_clear_brain_session_proxies_to_runtime(monkeypatch):
    calls = []

    async def fake_call(method, path, *, json=None):
        calls.append((method, path, json))
        return {
            "session_id": "debug-session",
            "cleared": True,
            "active_conversations": 0,
        }

    monkeypatch.setattr(brain_api, "_call_runtime_bridge", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/brain/sessions/debug-session")

    assert resp.status_code == 200
    assert resp.json()["cleared"] is True
    assert calls == [("DELETE", "/api/brain/sessions/debug-session", None)]


@pytest.mark.asyncio
async def test_clear_brain_session_url_encodes_runtime_path(monkeypatch):
    calls = []

    async def fake_call(method, path, *, json=None):
        calls.append((method, path, json))
        return {
            "session_id": "debug/session",
            "cleared": True,
            "active_conversations": 0,
        }

    monkeypatch.setattr(brain_api, "_call_runtime_bridge", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/brain/sessions/debug%2Fsession")

    assert resp.status_code == 200
    assert calls == [("DELETE", "/api/brain/sessions/debug%2Fsession", None)]


@pytest.mark.asyncio
async def test_brain_status_proxies_scheduler_state(monkeypatch):
    async def fake_call(method, path, *, json=None):
        return {
            "ready": True,
            "active_conversations": 1,
            "model": "test-model",
            "scheduler_started": True,
            "scheduler_job_count": 4,
            "platform": "web-debug",
        }

    monkeypatch.setattr(brain_api, "_call_runtime_bridge", fake_call)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/brain/status")

    assert resp.status_code == 200
    assert resp.json() == {
        "ready": True,
        "active_conversations": 1,
        "model": "test-model",
        "scheduler_started": True,
        "scheduler_job_count": 4,
        "platform": "web-debug",
    }


@pytest.mark.asyncio
async def test_runtime_bridge_unavailable_returns_503(monkeypatch):
    async def fake_request(self, method, url, json=None):
        raise httpx.ConnectError("connect failed")

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr(brain_api.settings, "AGENT_RUNTIME_WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(brain_api.settings, "AGENT_RUNTIME_WEB_PORT", 19999)

    with pytest.raises(Exception) as exc_info:
        await brain_api._call_runtime_bridge("GET", "/api/brain/status")

    exc = exc_info.value
    assert getattr(exc, "status_code", None) == 503
    assert "agent_runtime Web Brain 不可达" in exc.detail
