"""Agent 大脑模块 —— 7×24 Agent 的核心智能层。

提供 LLM 意图理解、Function Calling 工具执行、多轮对话管理能力。

快速开始::

    from backend.agent.brain import CommandExecutor, LLMIntentParser, FunctionRegistry
    from backend.agent.brain.tools import register_all

    registry = FunctionRegistry()
    register_all(registry)

    parser = LLMIntentParser(registry=registry, model="qwen3.7-max", api_key="sk-xxx")
    executor = CommandExecutor(parser=parser, registry=registry)

    reply = await executor.handle(im_message)
"""

from backend.agent.brain.base import (
    CommandContext,
    FunctionDef,
    IntentResult,
    ToolHandler,
)
from backend.agent.brain.executor import CommandExecutor
from backend.agent.brain.parser import LLMIntentParser
from backend.agent.brain.registry import FunctionRegistry

__all__ = [
    "CommandExecutor",
    "LLMIntentParser",
    "FunctionRegistry",
    "FunctionDef",
    "IntentResult",
    "CommandContext",
    "ToolHandler",
]
