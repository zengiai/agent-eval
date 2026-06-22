"""FunctionRegistry —— Function Calling 注册中心。

注册 → 查询 → 执行三阶段生命周期。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from backend.agent.brain.base import CommandContext, FunctionDef, ToolHandler

logger = logging.getLogger(__name__)


class FunctionRegistry:
    """Function Calling 注册中心。

    职责：
    1. 注册 function 定义及其 handler
    2. 导出 OpenAI 兼容的 ``tools`` 参数列表
    3. 按名称执行 handler

    用法::

        registry = FunctionRegistry()
        registry.register(FunctionDef(...), handler=my_handler)
        defs = registry.get_definitions()   # → tools 参数
        result = await registry.execute("list_cases", args, context)
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, ToolHandler] = {}
        self._definitions: Dict[str, FunctionDef] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, func_def: FunctionDef, handler: ToolHandler) -> None:
        """注册一个 function 及其 handler。

        Args:
            func_def: function 定义（name、description、parameters 等）。
            handler: 异步处理函数，签名为 ``async def handler(args: dict, context: CommandContext) -> Any``。
        """
        if func_def.name in self._handlers:
            logger.warning("Function %s 已注册，将被覆盖", func_def.name)
        self._handlers[func_def.name] = handler
        self._definitions[func_def.name] = func_def
        logger.debug("Function 已注册: name=%s category=%s risk=%s",
                     func_def.name, func_def.category, func_def.risk_level)

    def register_batch(
        self, items: List[tuple[FunctionDef, ToolHandler]]
    ) -> None:
        """批量注册 function。

        Args:
            items: ``[(FunctionDef, handler), ...]`` 列表。
        """
        for func_def, handler in items:
            self.register(func_def, handler)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_definitions(self) -> List[Dict[str, Any]]:
        """返回 OpenAI 兼容的 ``tools`` 参数列表。

        Returns:
            ``[{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}, ...]``
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": fd.name,
                    "description": fd.description,
                    "parameters": fd.parameters,
                },
            }
            for fd in self._definitions.values()
        ]

    def get_function(self, name: str) -> FunctionDef:
        """按名称查询 function 定义。

        Raises:
            KeyError: function 未注册。
        """
        return self._definitions[name]

    @property
    def registered_names(self) -> List[str]:
        """返回所有已注册的 function 名称列表。"""
        return list(self._definitions.keys())

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def execute(
        self, name: str, arguments: Dict[str, Any], context: CommandContext
    ) -> Any:
        """执行指定 function 的 handler。

        Args:
            name: function 名称。
            arguments: LLM 解析出的参数 dict。
            context: 命令执行上下文（含 DB session、eval_service 等）。

        Returns:
            handler 的返回值（由调用方负责格式化）。

        Raises:
            ValueError: function 未注册。
        """
        handler = self._handlers.get(name)
        if not handler:
            raise ValueError(f"Unknown function: {name}")

        logger.info("Executing function: %s args=%s", name, arguments)
        try:
            return await handler(arguments, context)
        except Exception:
            logger.exception("Function %s 执行失败", name)
            raise

    @property
    def count(self) -> int:
        """已注册的 function 数量。"""
        return len(self._definitions)
