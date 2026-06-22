"""AgentBrain 工具集 —— 基础查询工具与操作工具定义与 handler 注册。

查询工具聚焦 case、trace、case set；操作工具保留评测触发、采样评测、调度管理。
通过 ``register_all(registry)`` 一键注册所有工具到 FunctionRegistry。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from backend.agent.brain.base import FunctionDef
from backend.agent.brain.tools import actions, queries

if TYPE_CHECKING:
    from backend.agent.brain.registry import FunctionRegistry


# ===================================================================
# FunctionDef 定义
# ===================================================================

# ── 查询工具 (queries) ──

FUNC_LIST_CASES = FunctionDef(
    name="list_cases",
    description="查询评测用例列表，支持按来源、分类、难度、状态筛选",
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "用例来源：manual/trace/sampling",
            },
            "category": {
                "type": "string",
                "description": "用例分类",
            },
            "difficulty": {
                "type": "string",
                "enum": ["easy", "medium", "hard"],
                "description": "难度等级",
            },
            "review_status": {
                "type": "string",
                "description": "审核状态",
            },
            "health_status": {
                "type": "string",
                "description": "健康状态",
            },
            "search": {
                "type": "string",
                "description": "搜索 query 关键词",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限",
                "default": 20,
            },
        },
        "required": [],
    },
    category="query",
    risk_level="low",
)

FUNC_GET_CASE_DETAIL = FunctionDef(
    name="get_case_detail",
    description="获取单个评测用例的完整详情：查询内容、期望标注、评分历史",
    parameters={
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "用例 ID",
            },
        },
        "required": ["case_id"],
    },
    category="query",
    risk_level="low",
)

FUNC_SEARCH_TRACES = FunctionDef(
    name="search_traces",
    description="按关键词搜索 Trace 记录，支持按来源、分数范围、状态、版本筛选",
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
            "agent_version": {
                "type": "string",
                "description": "Agent 版本号",
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

FUNC_LIST_SCHEDULER_JOBS = FunctionDef(
    name="list_scheduler_jobs",
    description="查看当前 Scheduler 已注册的后台任务列表，包括任务 ID、名称、触发器、启用状态",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    category="query",
    risk_level="low",
)

FUNC_TRIGGER_SCHEDULER_JOB = FunctionDef(
    name="trigger_scheduler_job",
    description="立即触发一个已注册的 Scheduler Job，不影响原有定时周期",
    parameters={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要触发的 Job ID，如 sampling.hourly、report.daily、alert.check",
            },
        },
        "required": ["job_id"],
    },
    category="action",
    risk_level="medium",
)

FUNC_PAUSE_SCHEDULER_JOB = FunctionDef(
    name="pause_scheduler_job",
    description="暂停一个或多个已注册的 Scheduler Job，保留任务配置但停止后续定时触发",
    parameters={
        "type": "object",
        "properties": {
            "job_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要暂停的 Job ID 列表，如 [\"sampling.hourly\", \"report.daily\"]",
            },
            "job_id": {
                "type": "string",
                "description": "兼容旧参数：单个要暂停的 Job ID，如 sampling.hourly",
            },
            "all_jobs": {
                "type": "boolean",
                "description": "是否暂停所有已注册的 Scheduler Job。用户说暂停全部、停用所有定时任务时传 true",
            },
        },
        "required": [],
    },
    category="action",
    risk_level="medium",
)

FUNC_RESUME_SCHEDULER_JOB = FunctionDef(
    name="resume_scheduler_job",
    description="恢复一个或多个已暂停的 Scheduler Job，使其重新按原调度周期触发",
    parameters={
        "type": "object",
        "properties": {
            "job_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要恢复的 Job ID 列表，如 [\"sampling.hourly\", \"report.daily\"]",
            },
            "job_id": {
                "type": "string",
                "description": "兼容旧参数：单个要恢复的 Job ID，如 sampling.hourly",
            },
            "all_jobs": {
                "type": "boolean",
                "description": "是否恢复所有已注册的 Scheduler Job。用户说恢复全部、启用所有定时任务时传 true",
            },
        },
        "required": [],
    },
    category="action",
    risk_level="medium",
)

FUNC_GET_SCHEDULER_JOB_DETAIL = FunctionDef(
    name="get_scheduler_job_detail",
    description="查看指定 Scheduler Job 的详情，包括任务参数、运行时状态和最近执行日志记录",
    parameters={
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "要查看的 Job ID，如 sampling.hourly、report.daily、alert.check",
            },
            "history_limit": {
                "type": "integer",
                "description": "返回最近多少条执行日志，默认 10，最大 50",
                "default": 10,
            },
        },
        "required": ["job_id"],
    },
    category="query",
    risk_level="low",
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
            "job_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "任务 ID 列表。pause/resume 可用，兼容批量操作",
            },
            "all_jobs": {
                "type": "boolean",
                "description": "是否对所有已注册任务生效。用户说全部、所有时传 true",
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


# ===================================================================
# 注册函数
# ===================================================================

def register_all(registry: "FunctionRegistry") -> None:
    """将 Brain 工具注册到 FunctionRegistry。

    Args:
        registry: FunctionRegistry 实例。
    """
    registry.register_batch([
        (FUNC_LIST_CASES, queries.list_cases),
        (FUNC_GET_CASE_DETAIL, queries.get_case_detail),
        (FUNC_SEARCH_TRACES, queries.search_traces),
        (FUNC_GET_TRACE_DETAIL, queries.get_trace_detail),
        (FUNC_LIST_CASE_SETS, queries.list_case_sets),
        (FUNC_TRIGGER_EVALUATION, actions.trigger_evaluation),
        (FUNC_SAMPLE_AND_EVALUATE, actions.sample_and_evaluate),
        (FUNC_LIST_SCHEDULER_JOBS, actions.list_scheduler_jobs),
        (FUNC_TRIGGER_SCHEDULER_JOB, actions.trigger_scheduler_job),
        (FUNC_PAUSE_SCHEDULER_JOB, actions.pause_scheduler_job),
        (FUNC_RESUME_SCHEDULER_JOB, actions.resume_scheduler_job),
        (FUNC_GET_SCHEDULER_JOB_DETAIL, actions.get_scheduler_job_detail),
        (FUNC_MANAGE_SCHEDULER, actions.manage_scheduler),
    ])
