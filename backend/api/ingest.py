"""事件上报入口 —— HTTP 端点。

作为 Redis 上报的备用通道，Agent 也可通过 HTTP POST 直接提交事件。
"""

import json
import uuid
from typing import Optional

import redis
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.config import settings

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class SpanEvent(BaseModel):
    span_type: str
    trace_id: str
    sequence: Optional[int] = None
    input: Optional[dict] = None
    output: Optional[dict] = None
    latency_ms: Optional[int] = None
    tokens: Optional[dict] = None
    model: Optional[str] = None
    tool_name: Optional[str] = None
    tool_params: Optional[dict] = None
    tool_result: Optional[dict] = None


class TraceStartEvent(BaseModel):
    agent_version: str
    query: str
    context: Optional[dict] = None
    source: str = "eval"
    run_id: Optional[str] = None
    source_ref: Optional[str] = None
    session_id: Optional[str] = None


class TraceFinishEvent(BaseModel):
    trace_id: str
    final_response: Optional[str] = None
    status: str = "success"


def _redis_client():
    return redis.from_url(settings.REDIS_URL)


@router.post("/trace/start")
async def trace_start(event: TraceStartEvent):
    """接收 trace_start 事件，写入 Redis。"""
    trace_id = str(uuid.uuid4())
    payload = {
        "type": "trace_start",
        "trace_id": trace_id,
        "agent_version": event.agent_version,
        "query": event.query,
        "context": event.context or {},
        "source": event.source,
        "run_id": event.run_id,
        "source_ref": event.source_ref,
        "session_id": event.session_id,
    }
    r = _redis_client()
    r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps(payload, default=str))
    r.close()
    return {"trace_id": trace_id, "status": "accepted"}


@router.post("/trace/span")
async def trace_span(event: SpanEvent):
    """接收 span 事件，写入 Redis。"""
    payload = {
        "type": "span",
        "trace_id": event.trace_id,
        "span_type": event.span_type,
        "sequence": event.sequence,
        "input": event.input,
        "output": event.output,
        "latency_ms": event.latency_ms,
        "tokens": event.tokens,
        "model": event.model,
        "tool_name": event.tool_name,
        "tool_params": event.tool_params,
        "tool_result": event.tool_result,
    }
    r = _redis_client()
    r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps(payload, default=str))
    r.close()
    return {"status": "accepted"}


@router.post("/trace/finish")
async def trace_finish(event: TraceFinishEvent):
    """接收 trace_finish 事件，写入 Redis。"""
    payload = {
        "type": "trace_finish",
        "trace_id": event.trace_id,
        "final_response": event.final_response,
        "status": event.status,
    }
    r = _redis_client()
    r.rpush(f"{settings.REDIS_KEY_PREFIX}span", json.dumps(payload, default=str))
    r.close()
    return {"status": "accepted"}
