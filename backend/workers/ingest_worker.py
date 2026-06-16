"""Ingest 消费者 —— 从 Redis List 拉取事件，批量写入 PostgreSQL。

定时轮询 Redis，将 SDK 上报的 trace_start / span / trace_finish 事件
解析后写入 traces 和 spans 表。

支持两种模式：
- 独立部署：从 backend.core.config.settings 读取全局配置
- 挂载模式：通过构造函数传入配置参数
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.core.config import settings
from backend.core.database import async_session_factory
from backend.core.models import Trace, Span, EvalRun

logger = logging.getLogger(__name__)


class IngestWorker:
    """Redis 事件消费者，负责将事件落表。

    用法（挂载模式）:
        worker = IngestWorker(
            session_factory=my_session_factory,
            redis_url="redis://localhost:6379/0",
            redis_key_prefix="eval:events:",
            flush_interval_ms=500,
            flush_batch_size=100,
        )
        await worker.start()
        # ... Agent 运行 ...
        await worker.stop()
    """

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker] = None,
        redis_url: Optional[str] = None,
        redis_key_prefix: Optional[str] = None,
        flush_interval_ms: Optional[int] = None,
        flush_batch_size: Optional[int] = None,
    ):
        self._running = False
        self._redis: Optional[aioredis.Redis] = None
        self._session_factory = session_factory or async_session_factory
        self._redis_url = redis_url or settings.REDIS_URL
        self._redis_key_prefix = redis_key_prefix or settings.REDIS_KEY_PREFIX
        self._flush_interval_ms = flush_interval_ms or settings.FLUSH_INTERVAL_MS
        self._flush_batch_size = flush_batch_size or settings.FLUSH_BATCH_SIZE
        self._span_key = f"{self._redis_key_prefix}span"

    async def start(self):
        """启动消费者循环。"""
        self._redis = aioredis.from_url(self._redis_url)
        self._running = True
        logger.info("Ingest 消费者已启动，监听 key: %s", self._span_key)

        while self._running:
            try:
                await self._consume_batch()
            except Exception as e:
                logger.exception("Ingest 消费异常: %s", e)
            await asyncio.sleep(self._flush_interval_ms / 1000.0)

    async def stop(self):
        """停止消费者。"""
        self._running = False
        if self._redis:
            await self._redis.aclose()
        logger.info("Ingest 消费者已停止")

    async def _consume_batch(self):
        """拉取一批事件并写入数据库。"""
        batch = []
        for _ in range(self._flush_batch_size):
            raw = await self._redis.lpop(self._span_key)
            if raw is None:
                break
            try:
                event = json.loads(raw)
                batch.append(event)
            except json.JSONDecodeError:
                logger.warning("跳过无效 JSON 事件")
                continue

        if not batch:
            return

        # 按事件类型分组（同时过滤非法 UUID 事件，防止整批崩溃）
        trace_starts = []
        spans = []
        trace_finishes = []
        for e in batch:
            etype = e.get("type", "")
            # 校验 trace_id 是否为合法 UUID
            tid = e.get("trace_id", "")
            try:
                UUID(tid)
            except (ValueError, AttributeError):
                logger.warning("跳过 trace_id 非法的事件: %s", tid)
                continue
            if etype == "trace_start":
                trace_starts.append(e)
            elif etype == "span":
                spans.append(e)
            elif etype == "trace_finish":
                trace_finishes.append(e)
            else:
                logger.warning("跳过未知事件类型: %s", etype)

        async with self._session_factory() as session:
            # 处理 trace_start
            for event in trace_starts:
                trace = Trace(
                    id=UUID(event["trace_id"]),
                    agent_version=event.get("agent_version", ""),
                    query=event["query"],
                    context=event.get("context", {}),
                    source=event.get("source", "eval"),
                    source_ref=event.get("source_ref"),
                    session_id=event.get("session_id"),
                )
                session.add(trace)

                # 回写 eval_runs.trace_id（容忍非法 run_id）
                run_id = event.get("run_id")
                if run_id:
                    try:
                        stmt = (
                            update(EvalRun)
                            .where(EvalRun.id == UUID(run_id))
                            .values(trace_id=UUID(event["trace_id"]))
                        )
                        await session.execute(stmt)
                    except (ValueError, AttributeError):
                        logger.warning("跳过非法的 run_id: %s", run_id)

            # 处理 span 事件
            for event in spans:
                span = Span(
                    trace_id=UUID(event["trace_id"]),
                    span_type=event["span_type"],
                    sequence=event["sequence"],
                    input=event.get("input"),
                    output=event.get("output"),
                    latency_ms=event.get("latency_ms"),
                    tokens=event.get("tokens"),
                    model=event.get("model"),
                    tool_name=event.get("tool_name"),
                    tool_params=event.get("tool_params"),
                    tool_result=event.get("tool_result"),
                )
                # 从 tool_result 提取 tool_status
                if event.get("tool_result") and isinstance(event["tool_result"], dict):
                    span.tool_status = event["tool_result"].get("status")
                session.add(span)

            # 处理 trace_finish
            for event in trace_finishes:
                tid = UUID(event["trace_id"])
                total_latency_ms = event.get("total_latency_ms")
                total_tokens = event.get("total_tokens")

                # 兜底：若事件未携带聚合值，从已入库 spans 实时计算
                if total_latency_ms is None or total_tokens is None:
                    span_result = await session.execute(
                        select(Span.latency_ms, Span.tokens).where(Span.trace_id == tid)
                    )
                    span_rows = span_result.all()
                    if total_latency_ms is None:
                        total_latency_ms = sum(
                            (row[0] or 0) for row in span_rows
                        )
                    if total_tokens is None:
                        agg_tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                        for row in span_rows:
                            usage = row[1]
                            if isinstance(usage, dict):
                                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                    if key in usage:
                                        agg_tokens[key] = agg_tokens.get(key, 0) + int(usage[key])
                        total_tokens = agg_tokens

                stmt = (
                    update(Trace)
                    .where(Trace.id == tid)
                    .values(
                        final_response=event.get("final_response"),
                        status=event.get("status", "success"),
                        total_latency_ms=total_latency_ms,
                        total_tokens=total_tokens,
                        total_cost_usd=event.get("total_cost_usd"),
                    )
                )
                await session.execute(stmt)

            await session.commit()

        logger.debug("Ingest 写入 %d 条事件（start=%d, span=%d, finish=%d）",
                     len(batch), len(trace_starts), len(spans), len(trace_finishes))
