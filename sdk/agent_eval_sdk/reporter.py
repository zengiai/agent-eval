"""TraceReporter —— Agent 侧上报 SDK 核心。

将 Agent 执行过程中的各阶段 Span 事件分阶段写入 Redis List，
评测系统侧的 Ingest 消费者定时拉取后批量写入 PostgreSQL。
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

import redis


@dataclass
class TraceContext:
    """一次 Trace 的上下文句柄。"""

    trace_id: str
    agent_version: str
    query: str
    context: Dict[str, Any] = field(default_factory=dict)
    source: str = "eval"
    run_id: Optional[str] = None
    source_ref: Optional[str] = None
    _reporter: Optional["TraceReporter"] = field(default=None, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)

    def report_span(
        self,
        span_type: str,
        input: Optional[Dict] = None,
        output: Optional[Dict] = None,
        latency_ms: Optional[int] = None,
        tokens: Optional[Dict] = None,
        model: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_params: Optional[Dict] = None,
        tool_result: Optional[Dict] = None,
    ) -> None:
        """上报一个 Span 事件到 Redis（即刻写入，不阻塞 Agent）。"""
        if self._reporter is None:
            raise RuntimeError("TraceContext 未绑定 TraceReporter")

        self._sequence += 1
        event = {
            "type": "span",
            "trace_id": self.trace_id,
            "span_type": span_type,
            "sequence": self._sequence,
            "input": input,
            "output": output,
            "latency_ms": latency_ms,
            "tokens": tokens,
            "model": model,
            "tool_name": tool_name,
            "tool_params": tool_params,
            "tool_result": tool_result,
            "timestamp": time.time(),
        }
        self._reporter._push_event(event)

    def finish(
        self,
        final_response: Optional[str] = None,
        status: str = "success",
    ) -> None:
        """结束 Trace，上报 finish 事件。"""
        if self._reporter is None:
            raise RuntimeError("TraceContext 未绑定 TraceReporter")

        event = {
            "type": "trace_finish",
            "trace_id": self.trace_id,
            "final_response": final_response,
            "status": status,
            "timestamp": time.time(),
        }
        self._reporter._push_event(event)


class TraceReporter:
    """Agent 侧上报器。

    用法:
        reporter = TraceReporter(agent_version="v2.3.1")
        trace = reporter.start_trace(query="...", source="eval", run_id="run_xxx")
        trace.report_span(span_type="intent", output={"intents": [...]})
        trace.report_span(span_type="generation", output={"response": "..."})
        trace.finish(final_response="...", status="success")
    """

    def __init__(
        self,
        agent_version: str,
        redis_url: str = "redis://localhost:6379/0",
        redis_key_prefix: str = "eval:events:",
        flush_interval_ms: int = 500,
        flush_batch_size: int = 100,
    ):
        self.agent_version = agent_version
        self.redis_key_prefix = redis_key_prefix
        self._redis = redis.from_url(redis_url)
        self._span_key = f"{redis_key_prefix}span"

    def start_trace(
        self,
        query: str,
        context: Optional[Dict] = None,
        source: str = "eval",
        run_id: Optional[str] = None,
        source_ref: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> TraceContext:
        """开始一次 Trace，上报 start 事件并返回 TraceContext 句柄。"""
        trace_id = str(uuid.uuid4())
        event = {
            "type": "trace_start",
            "trace_id": trace_id,
            "agent_version": self.agent_version,
            "query": query,
            "context": context or {},
            "source": source,
            "run_id": run_id,
            "source_ref": source_ref,
            "session_id": session_id,
            "timestamp": time.time(),
        }
        self._push_event(event)

        return TraceContext(
            trace_id=trace_id,
            agent_version=self.agent_version,
            query=query,
            context=context or {},
            source=source,
            run_id=run_id,
            source_ref=source_ref,
            _reporter=self,
        )

    def _push_event(self, event: Dict) -> None:
        """将事件 RPUSH 到 Redis List。"""
        payload = json.dumps(event, ensure_ascii=False, default=str)
        self._redis.rpush(self._span_key, payload)

    def close(self) -> None:
        """关闭 Redis 连接。"""
        self._redis.close()
