"""LLMIntentParser —— 基于 LLM Function Calling 的意图理解器。

将用户的自然语言消息解析为结构化的意图（function_name + arguments），
支持多轮对话历史和领域知识注入。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from backend.agent.brain.base import IntentResult
from backend.agent.brain.registry import FunctionRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是 agent-eval 评测系统的智能助手，运行在 Telegram 中。
你可以帮助用户查询评测用例与 Trace、触发评测任务、管理调度任务。

## 你的能力
- 查询评测用例列表与单个用例详情
- 搜索 Trace 与查看 Trace 详情
- 列出测试用例集信息
- 触发评测任务、手动采样评测
- 查看、触发、暂停、恢复后台 Scheduler Job，并查看 Job 详情和执行日志

## 路由规则
1. 如果用户意图明确匹配某个可用函数 → 调用对应 function
2. 如果用户问题与评测系统无关 → 调用 fallback_chat，友好说明你的能力范围
3. 如果参数不完整 → 尝试从上下文推断（如对话历史中的 case_id、trace_id、分类）
4. 仅做查询，不主动执行高风险操作，需用户明确指令

## 当前上下文
- 项目: agent-eval 评测系统
- 可查询的数据: eval_cases, case_sets, eval_tasks, eval_runs, traces, spans, eval_scores
- 支持的评测层: intent, retrieval, tool, generation, outcome
"""

FINAL_RESPONSE_SYSTEM_PROMPT = SYSTEM_PROMPT + """

## 最终回复规则
你现在处于工具调用完成后的最终回答阶段。
工具结果已经作为 tool message 提供给你。
请直接回答用户问题，只基于工具结果，不要编造不存在的数据。
输出纯文本，不要输出 HTML 标签、Markdown 表格或代码块。
"""

# 兜底 function 定义
FALLBACK_FUNCTION_DEF = {
    "type": "function",
    "function": {
        "name": "fallback_chat",
        "description": "当用户问题与评测系统无关，或无法确定调用哪个函数时的兜底回复。友好告知你的能力范围并建议尝试 /help",
        "parameters": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "友好回复文本，告知用户你的能力范围",
                }
            },
            "required": ["reply"],
        },
    },
}


# ---------------------------------------------------------------------------
# LLMIntentParser
# ---------------------------------------------------------------------------


class LLMIntentParser:
    """基于 LLM Function Calling 的意图理解器。

    工作流程:
        1. 构造 messages: [system_prompt, ...history, user_message]
        2. 调用 LLM API（带 tools 参数）
        3. 解析返回的 tool_calls → IntentResult
        4. 如果 LLM 选择不调用任何 tool → fallback_chat

    用法::

        parser = LLMIntentParser(
            registry=function_registry,
            model="qwen3.7-max",
            api_key="sk-xxx",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        intent = await parser.parse("列一下订单相关评测用例")
    """

    def __init__(
        self,
        registry: FunctionRegistry,
        model: str = "qwen3.7-max",
        api_key: str = "",
        base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        temperature: float = 0.0,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_history: int = 10,
    ) -> None:
        self._registry = registry
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_history = max_history * 2  # user + assistant 轮次

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def parse(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """解析用户自然语言消息为结构化意图。

        Args:
            user_text: 用户输入文本。
            history: 对话历史，格式 ``[{"role": "user", "content": "..."}, ...]``。

        Returns:
            IntentResult，包含 function_name、arguments 等信息。
        """
        # 构建 messages
        messages = self._build_messages(user_text, history)

        # 构建 tools 参数（业务 tools + fallback）
        tools = self._registry.get_definitions() + [FALLBACK_FUNCTION_DEF]

        # 调用 LLM（带重试）
        raw_response = await self._call_llm_with_retry(messages, tools)

        # 解析 tool_calls
        return self._parse_tool_calls(raw_response)

    async def complete_with_tool_result(
        self,
        *,
        user_text: str,
        intent: IntentResult,
        tool_result: Any,
        history: Optional[List[Dict[str, str]]] = None,
        max_result_chars: int = 4000,
    ) -> str:
        """基于工具调用结果生成最终 LLM 回复。

        Args:
            user_text: 当前用户输入。
            intent: 已解析出的工具调用意图。
            tool_result: 工具返回的结构化结果。
            history: 当前会话历史，用于理解多轮上下文。
            max_result_chars: 传给 LLM 的工具结果最大字符数。

        Returns:
            最终面向用户的纯文本回复。调用失败由上层降级处理。
        """
        result_json = json.dumps(tool_result, ensure_ascii=False, default=str)
        if len(result_json) > max_result_chars:
            result_json = f"{result_json[:max_result_chars]}\n...<truncated>"

        args_json = json.dumps(intent.arguments, ensure_ascii=False, default=str)
        tool_call_id = self._tool_call_id(intent)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": FINAL_RESPONSE_SYSTEM_PROMPT},
        ]
        if history:
            truncated = history[-self._max_history :] if len(history) > self._max_history else history
            messages.extend(truncated)

        messages.extend([
            {"role": "user", "content": user_text},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": intent.function_name,
                            "arguments": args_json,
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_json,
            },
        ])

        raw_response = await self._call_llm_with_retry(messages, tools=None)
        choices = raw_response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return str(message.get("content", "")).strip()

    def _tool_call_id(self, intent: IntentResult) -> str:
        """从原始响应中提取 tool_call id；测试或兜底场景生成稳定 id。"""
        try:
            choices = (intent.raw_response or {}).get("choices", [])
            message = choices[0].get("message", {})
            tool_calls = message.get("tool_calls") or []
            tool_call = tool_calls[0] if isinstance(tool_calls, list) else tool_calls
            tool_call_id = tool_call.get("id")
            if tool_call_id:
                return str(tool_call_id)
        except (AttributeError, IndexError, KeyError, TypeError):
            pass
        return f"call_{intent.function_name}"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_text: str,
        history: Optional[List[Dict[str, str]]],
    ) -> List[Dict[str, str]]:
        """构建完整的 messages 列表。"""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # 注入对话历史（截断到 max_history）
        if history:
            truncated = history[-self._max_history :] if len(history) > self._max_history else history
            messages.extend(truncated)

        # 当前用户消息
        messages.append({"role": "user", "content": user_text})

        return messages

    async def _call_llm_with_retry(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """调用 LLM API，支持重试。"""
        last_error = None
        for attempt in range(self._max_retries):
            try:
                return await self._call_llm(messages, tools)
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
                last_error = e
                if attempt < self._max_retries - 1:
                    wait = 2**attempt
                    logger.warning(
                        "LLM 意图解析失败 (attempt %d/%d)，%ds 后重试: %s",
                        attempt + 1, self._max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("LLM 意图解析最终失败: %s", e)

        raise RuntimeError(
            f"LLM API 调用失败（{self._max_retries} 次重试后）: {last_error}"
        )

    async def _call_llm(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """单次 LLM API 调用。"""
        body: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if tools is not None:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        url = f"{self._base_url}/chat/completions"
        logger.debug("LLM Intent 调用: model=%s messages=%d tools=%d",
                     self._model, len(messages), len(tools or []))

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        usage = data.get("usage", {})
        logger.debug("LLM Intent 响应: tokens=%s", usage.get("total_tokens", "?"))
        return data

    def _parse_tool_calls(self, raw_response: Dict[str, Any]) -> IntentResult:
        """从 LLM 原始响应中提取 tool_calls 并转换为 IntentResult。

        处理三种情况：
        1. LLM 调用了某个业务 tool → 返回对应 IntentResult
        2. LLM 调用了 fallback_chat → 返回兜底意图
        3. LLM 未调用任何 tool（返回纯文本） → 走 fallback_chat
        """
        choices = raw_response.get("choices", [])
        if not choices:
            return self._make_fallback("LLM 未返回有效响应，请稍后重试。")

        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls")

        # 情况 3：无 tool_calls，从 content 提取文本
        if not tool_calls:
            content = message.get("content", "").strip()
            if content:
                return self._make_fallback(content)
            return self._make_fallback("抱歉，我不太理解你的意思。输入 /help 查看可用命令。")

        # 解析第一个 tool_call
        tool_call = tool_calls[0] if isinstance(tool_calls, list) else tool_calls
        func_info = tool_call.get("function", {})
        func_name = func_info.get("name", "fallback_chat")

        # 解析 arguments（可能是 JSON 字符串）
        arguments_raw = func_info.get("arguments", "{}")
        if isinstance(arguments_raw, str):
            try:
                arguments = json.loads(arguments_raw)
            except json.JSONDecodeError:
                logger.warning("LLM 返回非 JSON arguments: %s", arguments_raw[:200])
                arguments = {"raw": arguments_raw}
        else:
            arguments = arguments_raw

        # 获取 function 定义以读取 risk_level
        try:
            func_def = self._registry.get_function(func_name)
            risk_level = func_def.risk_level
            require_confirmation = func_def.require_confirmation
        except KeyError:
            # fallback_chat 或未知 function
            risk_level = "low"
            require_confirmation = False

        logger.info(
            "Intent 解析: function=%s args=%s risk=%s confirm=%s",
            func_name, arguments, risk_level, require_confirmation,
        )

        return IntentResult(
            function_name=func_name,
            arguments=arguments,
            reasoning=func_name,
            raw_response=raw_response,
            risk_level=risk_level,
            require_confirmation=require_confirmation,
        )

    def _make_fallback(self, reply: str) -> IntentResult:
        """构造兜底意图。"""
        return IntentResult(
            function_name="fallback_chat",
            arguments={"reply": reply},
            reasoning="fallback",
            risk_level="low",
            require_confirmation=False,
        )
