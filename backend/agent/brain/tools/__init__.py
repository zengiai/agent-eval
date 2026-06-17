"""AgentBrain 工具集 —— 12 个 Function Calling Tool 定义与 handler 注册。

通过 ``register_all(registry)`` 一键注册所有工具到 FunctionRegistry。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.agent.brain.base import FunctionDef
from backend.agent.brain.tools import actions, queries, reports

if TYPE_CHECKING:
    from backend.agent.brain.registry import FunctionRegistry


# ===================================================================
# FunctionDef 定义
# ===================================================================

# ── 查询工具 (queries) ──

FUNC_GET_LATEST_EVAL_STATUS = FunctionDef(
    name="get_latest_eval_status",
    description="获取最近评测任务的全局状态概览：各状态任务数、平均分、活跃版本",
    parameters={
        "type": "object",
        "properties": {
            "agent_version": {
                "type": "string",
                "description": "Agent 版本号（可选，不填返回所有版本汇总）",
            },
            "hours_back": {
                "type": "integer",
                "description": "查询过去多少小时内的数据",
                "default": 24,
            },
        },
        "required": [],
    },
    category="query",
    risk_level="low",
)

FUNC_QUERY_SCORE_TREND = FunctionDef(
    name="query_score_trend",
    description="查询指定 Agent 版本最近 N 次评测的得分趋势（总分 + 各层得分变化）",
    parameters={
        "type": "object",
        "properties": {
            "agent_version": {
                "type": "string",
                "description": "Agent 版本号，如 v2.3.1。不填则查询所有版本",
            },
            "last_n": {
                "type": "integer",
                "description": "查询最近 N 次评测",
                "default": 5,
            },
            "layer": {
                "type": "string",
                "enum": ["overall", "intent", "retrieval", "tool", "generation", "outcome"],
                "description": "指定评测层。overall 表示加权总分",
                "default": "overall",
            },
            "case_set_name": {
                "type": "string",
                "description": "限定测试集名称（可选）",
            },
        },
        "required": [],
    },
    category="query",
    risk_level="low",
)

FUNC_SEARCH_TRACES = FunctionDef(
    name="search_traces",
    description="按关键词搜索 Trace 记录，支持按来源、分数范围、状态筛选",
    parameters={
        "type": "object",
        "properties": {
            "query_keyword": {
                "type": "string",
                "description": "搜索 Trace 的 query 字段关键词",
            },
            "source": {
                "type": "string",
                "enum": ["eval", "production"],
                "description": "Trace 来源",
            },
            "min_score": {
                "type": "number",
                "description": "总分下限（0-100）",
            },
            "max_score": {
                "type": "number",
                "description": "总分上限（0-100）",
            },
            "status": {
                "type": "string",
                "enum": ["success", "error", "timeout", "partial"],
                "description": "Trace 执行状态",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限",
                "default": 10,
            },
        },
        "required": [],
    },
    category="query",
    risk_level="low",
)

FUNC_GET_TRACE_DETAIL = FunctionDef(
    name="get_trace_detail",
    description="获取指定 Trace 的完整详情：各 Span 输入输出、各层评分明细",
    parameters={
        "type": "object",
        "properties": {
            "trace_id": {
                "type": "string",
                "description": "Trace UUID（支持短前缀匹配，至少 8 位）",
            },
        },
        "required": ["trace_id"],
    },
    category="query",
    risk_level="low",
)

FUNC_LIST_CASE_SETS = FunctionDef(
    name="list_case_sets",
    description="列出当前可用的测试用例集及其概要信息（名称、用例数、分类）",
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "按分类筛选（可选）",
            },
            "search": {
                "type": "string",
                "description": "按名称模糊搜索（可选）",
            },
        },
        "required": [],
    },
    category="query",
    risk_level="low",
)

FUNC_GET_WEAKEST_CASES = FunctionDef(
    name="get_weakest_cases",
    description="找出当前评分最低的测试用例（退化热点），帮助定位 Agent 短板",
    parameters={
        "type": "object",
        "properties": {
            "agent_version": {
                "type": "string",
                "description": "Agent 版本号（可选）",
            },
            "top_n": {
                "type": "integer",
                "description": "返回最低分的 N 个用例",
                "default": 10,
            },
            "layer": {
                "type": "string",
                "enum": ["overall", "intent", "retrieval", "tool", "generation", "outcome"],
                "description": "按指定层排序，overall 按总分排",
                "default": "overall",
            },
        },
        "required": [],
    },
    category="query",
    risk_level="low",
)

# ── 操作工具 (actions) ──

FUNC_TRIGGER_EVALUATION = FunctionDef(
    name="trigger_evaluation",
    description="触发一次评测任务。指定测试用例集和 Agent 版本，启动五层评测执行",
    parameters={
        "type": "object",
        "properties": {
            "agent_version": {
                "type": "string",
                "description": "要评测的 Agent 版本号",
            },
            "case_set_name": {
                "type": "string",
                "description": "测试用例集名称（支持模糊匹配，不填则用默认集）",
            },
            "layers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["intent", "retrieval", "tool", "generation", "outcome"],
                },
                "description": "要启用的评测层，默认全部启用",
            },
        },
        "required": ["agent_version"],
    },
    category="action",
    risk_level="high",
    require_confirmation=True,
)

FUNC_SAMPLE_AND_EVALUATE = FunctionDef(
    name="sample_and_evaluate",
    description="从生产环境的 Trace 中手动采样并立即触发评测（不等定时任务）",
    parameters={
        "type": "object",
        "properties": {
            "sample_size": {
                "type": "integer",
                "description": "采样数量",
                "default": 10,
            },
            "hours_back": {
                "type": "integer",
                "description": "从过去多少小时的数据中采样",
                "default": 24,
            },
            "agent_version": {
                "type": "string",
                "description": "限定版本号（可选）",
            },
        },
        "required": [],
    },
    category="action",
    risk_level="medium",
)

FUNC_MANAGE_SCHEDULER = FunctionDef(
    name="manage_scheduler",
    description="管理后台调度任务：查看状态、暂停、恢复、立即触发、修改周期、查看执行历史",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "pause", "resume", "trigger", "update", "history"],
                "description": "操作类型：list=列表, pause=暂停, resume=恢复, trigger=立即触发, update=修改周期, history=执行历史",
            },
            "job_id": {
                "type": "string",
                "description": "任务 ID（如 sampling.hourly）。list 时可选，其他操作必填",
            },
            "new_trigger_value": {
                "type": "string",
                "description": "新的调度值（仅 action=update 时需要）。cron 表达式或秒数（间隔模式）",
            },
        },
        "required": ["action"],
    },
    category="action",
    risk_level="medium",
)

# ── 报告工具 (reports) ──

FUNC_COMPARE_VERSIONS = FunctionDef(
    name="compare_versions",
    description="对比两个 Agent 版本的评测得分（各层得分差异 + 总分变化）",
    parameters={
        "type": "object",
        "properties": {
            "version_a": {
                "type": "string",
                "description": "基准版本号",
            },
            "version_b": {
                "type": "string",
                "description": "对比版本号",
            },
            "case_set_name": {
                "type": "string",
                "description": "限定特定测试集（可选，不填则全局对比）",
            },
        },
        "required": ["version_a", "version_b"],
    },
    category="report",
    risk_level="low",
)

FUNC_GET_DAILY_REPORT = FunctionDef(
    name="get_daily_report",
    description="获取指定日期的评测日报摘要：评测量、平均分、各层得分、告警统计",
    parameters={
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "日期，格式 YYYY-MM-DD。默认为昨天",
            },
            "agent_version": {
                "type": "string",
                "description": "可选版本筛选",
            },
        },
        "required": [],
    },
    category="report",
    risk_level="low",
)

FUNC_GET_ALERT_HISTORY = FunctionDef(
    name="get_alert_history",
    description="查询历史告警记录，了解系统健康状态和异常模式",
    parameters={
        "type": "object",
        "properties": {
            "severity": {
                "type": "string",
                "enum": ["info", "warning", "critical"],
                "description": "按严重级别筛选",
            },
            "hours_back": {
                "type": "integer",
                "description": "查询过去多少小时",
                "default": 24,
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限",
                "default": 20,
            },
        },
        "required": [],
    },
    category="report",
    risk_level="low",
)


# ===================================================================
# 注册函数
# ===================================================================

def register_all(registry: "FunctionRegistry") -> None:
    """将所有 12 个工具注册到 FunctionRegistry。

    Args:
        registry: FunctionRegistry 实例。
    """
    registry.register_batch([
        # 查询类 (6)
        (FUNC_GET_LATEST_EVAL_STATUS, queries.get_latest_eval_status),
        (FUNC_QUERY_SCORE_TREND, queries.query_score_trend),
        (FUNC_SEARCH_TRACES, queries.search_traces),
        (FUNC_GET_TRACE_DETAIL, queries.get_trace_detail),
        (FUNC_LIST_CASE_SETS, queries.list_case_sets),
        (FUNC_GET_WEAKEST_CASES, queries.get_weakest_cases),
        # 操作类 (3)
        (FUNC_TRIGGER_EVALUATION, actions.trigger_evaluation),
        (FUNC_SAMPLE_AND_EVALUATE, actions.sample_and_evaluate),
        (FUNC_MANAGE_SCHEDULER, actions.manage_scheduler),
        # 报告类 (3)
        (FUNC_COMPARE_VERSIONS, reports.compare_versions),
        (FUNC_GET_DAILY_REPORT, reports.get_daily_report),
        (FUNC_GET_ALERT_HISTORY, reports.get_alert_history),
    ])
