"""AgentBrain 核心数据类型 —— FunctionDef / IntentResult / CommandContext。

所有 brain 子模块共享的基础数据载体，无外部业务依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from backend.agent.scheduler.manager import JobManager
    from backend.agent.gateway.base import IMGateway


# ---------------------------------------------------------------------------
# Handler 签名
# ---------------------------------------------------------------------------

#: Tool handler 签名：接收 arguments dict 和 CommandContext，返回任意结果
ToolHandler = Callable[..., Any]


# ---------------------------------------------------------------------------
# FunctionDef
# ---------------------------------------------------------------------------


@dataclass
class FunctionDef:
    """Function Calling 工具定义（OpenAI 兼容格式）。

    每个工具对应一个 eval 领域操作，由 FunctionRegistry 统一管理。
    LLM 根据 description 和 parameters schema 判断何时调用。
    """

    name: str
    """函数名，全局唯一，如 ``"query_score_trend"``"""

    description: str
    """描述，LLM 用于判断调用哪个 function。需简明描述能力、参数和返回值。"""

    parameters: Dict[str, Any]
    """JSON Schema 格式的参数定义，符合 OpenAI Function Calling 规范。"""

    category: str = "query"
    """工具分类：``"query"`` | ``"action"`` | ``"report"``"""

    risk_level: str = "low"
    """风险等级：``"low"`` | ``"medium"`` | ``"high"``。
    high 操作需上层（MessageRouter）二次确认。"""

    require_confirmation: bool = False
    """是否需要用户二次确认。由 MessageRouter 负责执行确认流程。"""


# ---------------------------------------------------------------------------
# IntentResult
# ---------------------------------------------------------------------------


@dataclass
class IntentResult:
    """LLM 意图解析结果。

    由 LLMIntentParser 产生，传递给 FunctionRegistry 执行。
    包含 LLM 选择的 function、解析的参数、置信度等元信息。
    """

    function_name: str
    """LLM 选择的 function 名称，如 ``"query_score_trend"``"""

    arguments: Dict[str, Any]
    """LLM 从用户消息中提取的参数，已解析为 dict"""

    reasoning: str = ""
    """LLM 的解释（可解释性），方便调试"""

    confidence: float = 1.0
    """置信度（0~1），当前 LLM 不返回置信度时默认为 1.0"""

    raw_response: Optional[Dict] = None
    """LLM 原始响应（调试用），包含完整的 API 返回"""

    risk_level: str = "low"
    """风险等级，传递自 FunctionDef。MessageRouter 据此决定是否二次确认"""

    require_confirmation: bool = False
    """是否需要确认，传递自 FunctionDef"""

    @property
    def is_fallback(self) -> bool:
        """是否为兜底意图（LLM 无法匹配任何 tool）。"""
        return self.function_name == "fallback_chat"


# ---------------------------------------------------------------------------
# CommandContext
# ---------------------------------------------------------------------------


@dataclass
class CommandContext:
    """命令执行上下文，注入到每个 tool handler。

    包含数据库会话工厂、评测服务、调度器、网关等运行时依赖。
    tool handler 通过此对象访问系统资源，无需直接导入模块。
    """

    user_id: str
    """消息发送者 ID"""

    chat_id: str
    """会话 ID"""

    username: str
    """发送者名称（日志/审计用）"""

    db_session_factory: Any = None
    """**[DEPRECATED]** SQLAlchemy async_session_factory。
    Brain 重构后不再直连数据库，请使用 api_base_url 通过 eval-api 访问数据。"""

    api_base_url: str = "http://localhost:18000"
    """eval-api 地址，Brain 通过 HTTP 调用 eval-api 获取数据。"""

    eval_service: Any = None
    """EvalService 实例（挂载式评测服务入口）"""

    scheduler: JobManager | None = None
    """JobManager 实例（调度器管理接口）"""

    gateway: IMGateway | None = None
    """IMGateway 实例（消息收发接口）"""

    llm_config: Dict[str, Any] = field(default_factory=dict)
    """LLM 配置：``{"model": "...", "api_key": "...", "base_url": "..."}``"""

    config: Dict[str, Any] = field(default_factory=dict)
    """Agent 全局配置的扩展字段"""
