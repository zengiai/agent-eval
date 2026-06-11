"""Agent Eval SDK 适配器层。

提供与主流可观测性框架的集成适配器：
- OpenTelemetry SpanExporter（零侵入集成 LangChain/LlamaIndex 等框架）
"""

from agent_eval_sdk.adapters.otel_exporter import EvalSpanExporter

__all__ = ["EvalSpanExporter"]
