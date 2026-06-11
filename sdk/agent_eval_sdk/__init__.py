"""agent_eval_sdk —— Agent 侧上报 SDK。

提供两种上报方式：
- 方式 A（显式调用）：TraceReporter + TraceContext，手动控制上报粒度
- 方式 B（零侵入）：EvalSpanExporter，注册到 OpenTelemetry 即可自动采集
"""

from agent_eval_sdk.reporter import TraceReporter, TraceContext

__all__ = ["TraceReporter", "TraceContext"]
