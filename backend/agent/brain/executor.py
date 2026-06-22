"""CommandExecutor —— Agent 大脑的编排者。

串联 LLM 意图理解 → 工具执行 → 回复格式化的全流程。
内建 chat_id 维度多轮对话历史管理。
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Dict, List, Optional

from backend.agent.brain.base import CommandContext, IntentResult
from backend.agent.brain.parser import LLMIntentParser
from backend.agent.brain.registry import FunctionRegistry
from backend.agent.gateway.base import IMMessage

logger = logging.getLogger(__name__)


class CommandExecutor:
    """Agent 大脑的编排者。

    串联 LLM 理解 → 工具执行 → 回复格式化 的完整链路：

        IMMessage → LLMIntentParser.parse()
        → IntentResult → FunctionRegistry.execute()
        → 格式化结果 → 返回 Telegram HTML 文本

    用法::

        executor = CommandExecutor(
            parser=intent_parser,
            registry=function_registry,
            context_factory=lambda msg: CommandContext(...),
        )
        reply = await executor.handle(im_message)
    """

    def __init__(
        self,
        parser: LLMIntentParser,
        registry: FunctionRegistry,
        context_factory: callable = None,
        max_history: int = 10,
    ) -> None:
        """初始化编排器。

        Args:
            parser: LLM 意图解析器。
            registry: 函数注册中心。
            context_factory: 从 IMMessage 创建 CommandContext 的工厂函数。
            max_history: 每个 chat_id 最多保留的对话轮数。
        """
        self._parser = parser
        self._registry = registry
        self._context_factory = context_factory or (lambda msg: CommandContext(
            user_id=msg.user_id,
            chat_id=msg.chat_id,
            username=msg.username,
            api_base_url="http://localhost:18000",
        ))
        self._max_history = max_history

        # chat_id → 对话历史
        self._conversations: Dict[str, List[Dict[str, str]]] = {}
        # chat_id → asyncio.Lock（防止同会话并发写入）
        self._history_locks: Dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def handle(self, msg: IMMessage) -> Optional[str]:
        """处理一条自然语言消息并返回回复文本。

        Args:
            msg: 归一化后的 IM 消息。

        Returns:
            格式化后的回复文本；LLM 解析失败时返回 None（由 MessageRouter 兜底）。
        """
        chat_id = msg.chat_id
        lock = self._history_locks.setdefault(chat_id, asyncio.Lock())

        async with lock:
            history = self._conversations.get(chat_id, [])

            try:
                # 1. 解析意图
                intent = await self._parser.parse(msg.text, history)
                logger.info(
                    "Intent: function=%s args=%s fallback=%s risk=%s",
                    intent.function_name, intent.arguments, intent.is_fallback, intent.risk_level,
                )

                # 2. 执行 function（fallback_chat 特殊处理）
                if intent.is_fallback:
                    reply = self._format_fallback(intent)
                else:
                    try:
                        context = self._context_factory(msg)
                        result = await self._registry.execute(
                            intent.function_name, intent.arguments, context
                        )
                        reply = await self._complete_tool_reply(
                            msg=msg,
                            intent=intent,
                            result=result,
                            history=history,
                        )
                    except ValueError as e:
                        # 工具层的参数校验错误（如 ValueError("trace_id 是必填参数")）
                        reply = f"❌ 参数错误: {self._html(e)}"
                    except KeyError as e:
                        # 注册表不一致（function 存在但定义丢失）
                        logger.error("Function 注册表不一致: %s", e)
                        reply = "❌ 系统内部错误，请联系管理员。"
                    except Exception as e:
                        logger.exception("Tool 执行失败: %s", intent.function_name)
                        reply = (
                            f"❌ 执行 <code>{self._html(intent.function_name)}</code> 时出错: "
                            f"{self._html(e)}，请稍后重试"
                        )

                # 3. 更新对话历史
                history.append({"role": "user", "content": msg.text})
                history.append({"role": "assistant", "content": reply})
                self._conversations[chat_id] = history[-self._max_history * 2 :]

                return reply

            except RuntimeError as e:
                # LLM API 不可用
                logger.error("LLM 意图解析不可用: %s", e)
                return (
                    "⚠️ LLM 服务暂时不可用，请稍后重试。\n"
                    "你也可以使用以下快速命令：\n"
                    "/help — 查看可用命令\n"
                    "/status — 系统状态\n"
                    "/ping — 连通性检查"
                )
            except Exception:
                logger.exception("CommandExecutor.handle 未预期错误")
                return "抱歉，处理你的消息时遇到问题。请使用 /help 查看可用命令。"

    # ------------------------------------------------------------------
    # 快捷方法（供 MessageRouter 调用）
    # ------------------------------------------------------------------

    async def handle_eval(self, msg: IMMessage, args: Dict[str, Any]) -> str:
        """MessageRouter 确认后调用的评测执行入口。

        Args:
            msg: 原始消息。
            args: 包含 ``agent_version`` 等参数的 dict。

        Returns:
            执行结果文本。
        """
        try:
            context = self._context_factory(msg)
            result = await self._registry.execute("trigger_evaluation", args, context)
            return self._format_eval_result(result)
        except Exception as e:
            logger.exception("handle_eval 执行失败")
            return f"❌ 评测触发失败: {self._html(e)}"

    async def handle_sample(self, msg: IMMessage, sample_size: int) -> str:
        """MessageRouter 调用的采样评测入口。

        Args:
            msg: 原始消息。
            sample_size: 采样数量。

        Returns:
            执行结果文本。
        """
        try:
            context = self._context_factory(msg)
            result = await self._registry.execute(
                "sample_and_evaluate",
                {"sample_size": sample_size},
                context,
            )
            return self._format_sample_result(result)
        except Exception as e:
            logger.exception("handle_sample 执行失败")
            return f"❌ 采样评测失败: {self._html(e)}"

    # ------------------------------------------------------------------
    # 回复格式化
    # ------------------------------------------------------------------

    def _format_reply(self, intent: IntentResult, result: Any) -> str:
        """根据 function 类型格式化工具执行结果为 Telegram HTML。"""
        func_name = intent.function_name

        # 查询类格式化
        if func_name == "list_cases":
            return self._fmt_list_cases(result)
        elif func_name == "get_case_detail":
            return self._fmt_case_detail(result)
        elif func_name == "search_traces":
            return self._fmt_search_traces(result)
        elif func_name == "get_trace_detail":
            return self._fmt_trace_detail(result)
        elif func_name == "list_case_sets":
            return self._fmt_case_sets(result)
        # 操作类
        elif func_name == "trigger_evaluation":
            return self._fmt_trigger_eval(result)
        elif func_name == "sample_and_evaluate":
            return self._fmt_sample_eval(result)
        elif func_name == "list_scheduler_jobs":
            return self._fmt_scheduler_jobs(result)
        elif func_name == "trigger_scheduler_job":
            return self._fmt_scheduler_trigger(result)
        elif func_name == "pause_scheduler_job":
            return self._fmt_scheduler_state_change(result, "暂停", "paused")
        elif func_name == "resume_scheduler_job":
            return self._fmt_scheduler_state_change(result, "恢复", "resumed")
        elif func_name == "get_scheduler_job_detail":
            return self._fmt_scheduler_job_detail(result)
        elif func_name == "manage_scheduler":
            return self._fmt_scheduler(result)
        else:
            return (
                f"✅ 执行完成: <code>{self._html(func_name)}</code>\n"
                f"{self._pre(result)}"
            )

    def _format_fallback(self, intent: IntentResult) -> str:
        """格式化兜底回复。"""
        reply = intent.arguments.get("reply", "抱歉，我不太理解你的意思。")
        return (
            f"{self._html(reply)}\n\n"
            "💡 试试以下操作：\n"
            "• 查用例：「列一下最近的评测用例」\n"
            "• 看用例详情：「查看 case-001 的详情」\n"
            "• 搜 Trace：「搜索包含 timeout 的 Trace」\n"
            "• 看测试集：「列出可用测试集」\n"
            "• 输入 /help 查看全部命令"
        )

    async def _complete_tool_reply(
        self,
        *,
        msg: IMMessage,
        intent: IntentResult,
        result: Any,
        history: List[Dict[str, str]],
    ) -> str:
        """将工具结果回填给 LLM，并返回最终回复。"""
        if isinstance(result, dict) and result.get("error"):
            return self._format_reply(intent, result)

        try:
            final_reply = await self._parser.complete_with_tool_result(
                user_text=msg.text,
                intent=intent,
                tool_result=result,
                history=history,
            )
        except Exception as exc:
            logger.warning(
                "工具结果最终回复生成失败，降级返回格式化结果: function=%s error=%s",
                intent.function_name,
                exc,
            )
            return self._format_reply(intent, result)

        if not final_reply:
            return self._format_reply(intent, result)

        return self._html(final_reply)

    @staticmethod
    def _cell(value: Any, default: str = "-") -> str:
        """将任意值转成单行展示文本。"""
        if value is None or value == "":
            return default
        return str(value).replace("\n", " ").replace("|", "/")

    @staticmethod
    def _html(value: Any, default: str = "-") -> str:
        """转义 Telegram HTML 文本节点中的动态值。"""
        if value is None or value == "":
            return default
        return html.escape(str(value), quote=False)

    def _code(self, value: Any, default: str = "-") -> str:
        """格式化 Telegram HTML 行内代码。"""
        return f"<code>{self._html(value, default)}</code>"

    def _pre(self, value: Any) -> str:
        """格式化 Telegram HTML 等宽块。"""
        return f"<pre>{self._html(value, '')}</pre>"

    def _pre_table(self, headers: list[str], rows: list[list[Any]]) -> str:
        """生成 Telegram 可稳定展示的等宽文本表格。"""
        normalized_rows = [[self._cell(cell) for cell in row] for row in rows]
        widths = [len(header) for header in headers]
        for row in normalized_rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))

        def fmt_row(row: list[str]) -> str:
            return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

        table_lines = [fmt_row(headers), fmt_row(["-" * width for width in widths])]
        table_lines.extend(fmt_row(row) for row in normalized_rows)
        return self._pre("\n".join(table_lines))

    def _short_text(self, value: Any, limit: int = 80) -> str:
        text = self._cell(value, "")
        if not text:
            return "-"
        return text if len(text) <= limit else f"{text[:limit - 1]}…"

    def _fmt_list_cases(self, result: Dict) -> str:
        cases = result.get("items") or result.get("cases") or []
        total = result.get("total", len(cases))
        if not cases:
            return f"ℹ️ <b>评测用例列表</b>\n\n未找到符合条件的用例（total={self._html(total)}）。"

        rows = []
        for case in cases[:20]:
            case_id = self._short_text(case.get("id") or case.get("case_id"), 10)
            score = case.get("last_avg_score", case.get("avg_score"))
            score_text = "-" if score is None else f"{float(score):.1f}"
            status = case.get("health_status") or case.get("review_status") or case.get("latest_run_status")
            rows.append([
                case_id,
                self._cell(case.get("source")),
                self._cell(case.get("category")),
                self._cell(case.get("difficulty")),
                self._cell(status),
                score_text,
                self._short_text(case.get("query"), 48),
            ])

        return (
            f"🧾 <b>评测用例列表</b>（共 {self._html(total)} 条，显示 {len(rows)} 条）\n\n"
            f"{self._pre_table(['ID', '来源', '分类', '难度', '状态', '均分', 'Query'], rows)}"
        )

    def _fmt_case_detail(self, result: Dict) -> str:
        case = result.get("case") or result
        scores = result.get("scores") or result.get("history") or []
        summary = result.get("score_summary") or {}
        last_avg = summary.get("last_avg_score", case.get("last_avg_score"))
        run_count = summary.get("run_count", case.get("run_count", len(scores)))

        score_text = "-" if last_avg is None else f"{float(last_avg):.1f}"
        lines = [
            "🧾 <b>评测用例详情</b>",
            "",
            f"<b>ID</b>: {self._code(case.get('id') or case.get('case_id'))}",
            f"<b>Query</b>: {self._html(self._short_text(case.get('query'), 240))}",
            f"<b>来源/分类/难度</b>: {self._html(self._cell(case.get('source')))} / {self._html(self._cell(case.get('category')))} / {self._html(self._cell(case.get('difficulty')))}",
            f"<b>审核/健康状态</b>: {self._html(self._cell(case.get('review_status')))} / {self._html(self._cell(case.get('health_status')))}",
            f"<b>最近均分</b>: {self._html(score_text)}（评测 {self._html(run_count or 0)} 次）",
        ]

        if case.get("gold_answer"):
            lines.extend(["", f"<b>Gold Answer</b>: {self._html(self._short_text(case.get('gold_answer'), 240))}"])
        expected_parts = []
        for key, label in (
            ("expected_intent", "Intent"),
            ("expected_retrieval", "Retrieval"),
            ("expected_tools", "Tools"),
            ("expected_answer", "Answer"),
        ):
            if case.get(key):
                expected_parts.append(
                    f"- {self._html(label)}: {self._code(self._short_text(case.get(key), 160))}"
                )
        if expected_parts:
            lines.extend(["", "<b>期望标注</b>:", *expected_parts])

        if scores:
            score_rows = []
            for item in scores[:5]:
                created_at = self._cell(item.get("created_at"), "")[:16] or "-"
                overall = item.get("overall_score")
                overall_text = "-" if overall is None else f"{float(overall):.1f}"
                layer_scores = []
                for score in item.get("scores", [])[:6]:
                    value = score.get("score")
                    value_text = "-" if value is None else f"{float(value):.1f}"
                    layer_scores.append(f"{score.get('layer', '-')}: {value_text}")
                score_rows.append([
                    created_at,
                    overall_text,
                    self._cell(item.get("status")),
                    self._cell("; ".join(layer_scores)),
                ])
            lines.extend([
                "",
                "<b>最近评分历史</b>:",
                self._pre_table(["时间", "总分", "状态", "层评分"], score_rows),
            ])

        return "\n".join(lines)

    def _fmt_search_traces(self, result: Dict) -> str:
        traces = result.get("traces") or result.get("items") or []
        total = result.get("total", len(traces))

        rows = []
        for t in traces:
            score_str = f"{t.get('overall_score', '-')}" if t.get('overall_score') is not None else "-"
            time_str = t.get("created_at", "")[:16] if t.get("created_at") else ""
            rows.append([
                self._cell(t.get("id", "")),
                self._cell(t.get("agent_version", "")),
                score_str,
                self._cell(t.get("status", "")),
                time_str,
            ])

        return (
            f"🔍 <b>Trace 搜索结果</b>（共 {self._html(total)} 条，显示 {len(traces)} 条）\n\n"
            f"{self._pre_table(['ID', '版本', '得分', '状态', '时间'], rows)}"
        )

    def _fmt_trace_detail(self, result: Dict) -> str:
        trace = result.get("trace", {})
        spans = result.get("spans", [])
        scores = result.get("eval_scores", [])

        span_lines = "\n".join(
            f"  • [{s.get('span_type', '')}] #{s.get('sequence', 0)} score={s.get('score', '-')}"
            for s in spans[:10]
        )
        score_lines = "\n".join(
            f"  • {s.get('id', '')}: {s.get('score', 0):.1f}"
            for s in scores[:10]
        )

        return (
            f"📋 <b>Trace 详情</b>\n\n"
            f"<b>ID</b>: {self._code(trace.get('id', ''))}\n"
            f"<b>Query</b>: {self._html(str(trace.get('query', ''))[:200])}\n"
            f"<b>状态</b>: {self._html(trace.get('status', ''))}\n"
            f"<b>总分</b>: {self._html(trace.get('overall_score', '-'))}\n"
            f"<b>版本</b>: {self._html(trace.get('agent_version', ''))}\n"
            f"<b>延迟</b>: {self._html(trace.get('total_latency_ms', '-'))}ms\n\n"
            f"<b>Spans</b> ({len(spans)}):\n{self._html(span_lines)}\n\n"
            f"<b>评分明细</b> ({len(scores)}):\n{self._html(score_lines)}"
        )

    def _fmt_case_sets(self, result: Dict) -> str:
        sets = result.get("case_sets", [])
        rows = []
        for cs in sets[:20]:
            rows.append([
                self._cell(cs.get("name", "")),
                self._cell(cs.get("category", "-")),
                self._cell(cs.get("case_count", 0)),
                self._cell(cs.get("version", "")),
            ])

        return (
            f"📦 <b>测试用例集</b>（共 {self._html(result.get('total', 0))} 个）\n\n"
            f"{self._pre_table(['名称', '分类', '用例数', '版本'], rows)}"
        )

    def _fmt_trigger_eval(self, result: Dict) -> str:
        return (
            f"✅ <b>评测任务已创建</b>\n\n"
            f"<b>任务 ID</b>: {self._code(result.get('task_id', ''))}\n"
            f"<b>版本</b>: {self._html(result.get('agent_version', ''))}\n"
            f"<b>测试集</b>: {self._html(result.get('case_set_name', ''))}\n"
            f"<b>用例数</b>: {self._html(result.get('total_cases', 0))}\n"
            f"<b>评测层</b>: {self._html(', '.join(result.get('layers', [])))}\n\n"
            f"评测将在后台执行，稍后可查看相关用例或 Trace 结果。"
        )

    def _fmt_sample_eval(self, result: Dict) -> str:
        sampled = result.get("sampled", 0)
        if sampled == 0:
            return f"ℹ️ {self._html(result.get('message', '没有找到符合条件的生产 Trace'))}"
        return (
            f"✅ <b>采样评测已触发</b>\n\n"
            f"<b>采样量</b>: {self._html(sampled)}\n"
            f"<b>批次</b>: {self._code(result.get('batch_id', ''))}\n"
            f"<b>任务 ID</b>: {self._code(result.get('task_id', ''))}\n"
            f"<b>时间窗口</b>: 过去 {self._html(result.get('hours_back', 24))} 小时"
        )

    def _fmt_scheduler_jobs(self, result: Dict) -> str:
        if "error" in result:
            return f"❌ Scheduler 查询失败: {self._html(result['error'])}"

        jobs = result.get("jobs", [])
        total = result.get("total", len(jobs))
        status = "running" if result.get("scheduler_started") else "stopped"
        if not jobs:
            return f"📋 <b>Scheduler Jobs</b>（{self._html(status)}）\n\n当前没有注册的 Job。"

        rows = []
        for job in jobs:
            trigger = f"{self._cell(job.get('trigger_type'))}={self._cell(job.get('trigger_value'))}"
            enabled = "yes" if job.get("enabled") else "no"
            timeout = job.get("timeout_seconds")
            timeout_text = "-" if timeout is None else f"{timeout}s"
            rows.append([
                self._cell(job.get("job_id")),
                self._cell(job.get("name")),
                trigger,
                enabled,
                timeout_text,
            ])
        return (
            f"📋 <b>Scheduler Jobs</b>（{self._html(status)}，共 {self._html(total)} 个）\n\n"
            f"{self._pre_table(['Job ID', '名称', '触发器', '启用', '超时'], rows)}"
        )

    def _fmt_scheduler_trigger(self, result: Dict) -> str:
        if "error" in result:
            return f"❌ Scheduler 触发失败: {self._html(result['error'])}"
        return (
            "✅ <b>Scheduler Job 已触发</b>\n\n"
            f"<b>Job ID</b>: {self._code(result.get('job_id'))}\n"
            f"<b>Execution ID</b>: {self._code(result.get('execution_id'))}\n"
            f"<b>状态</b>: {self._html(self._cell(result.get('status')))}"
        )

    def _fmt_scheduler_state_change(
        self, result: Dict, action_label: str, expected_status: str
    ) -> str:
        if "error" in result:
            return f"❌ Scheduler Job {action_label}失败: {self._html(result['error'])}"
        results = result.get("results") or []
        if len(results) > 1:
            success_count = result.get("success_count", 0)
            failure_count = result.get("failure_count", 0)
            if failure_count == 0:
                title = f"✅ <b>Scheduler Job 批量{self._html(action_label)}完成</b>"
            elif success_count == 0:
                title = f"❌ <b>Scheduler Job 批量{self._html(action_label)}失败</b>"
            else:
                title = f"⚠️ <b>Scheduler Job 批量{self._html(action_label)}部分成功</b>"

            rows = [
                [
                    self._cell(item.get("job_id")),
                    self._cell(item.get("status")),
                    self._cell(item.get("error")),
                ]
                for item in results
            ]
            return (
                f"{title}\n\n"
                f"<b>成功</b>: {self._html(success_count)}\n"
                f"<b>失败</b>: {self._html(failure_count)}\n\n"
                f"{self._pre_table(['Job ID', '状态', '错误'], rows)}"
            )

        return (
            f"✅ <b>Scheduler Job 已{self._html(action_label)}</b>\n\n"
            f"<b>Job ID</b>: {self._code(result.get('job_id'))}\n"
            f"<b>状态</b>: {self._html(self._cell(result.get('status'), expected_status))}"
        )

    def _fmt_scheduler_job_detail(self, result: Dict) -> str:
        if "error" in result:
            return f"❌ Scheduler Job 查询失败: {self._html(result['error'])}"

        job = result.get("job", {})
        runtime = job.get("runtime") or {}
        metadata = job.get("metadata") or {}
        executions = result.get("executions", [])
        trigger = f"{self._cell(job.get('trigger_type'))}={self._cell(job.get('trigger_value'))}"
        status = "running" if result.get("scheduler_started") else "stopped"
        timeout = job.get("timeout_seconds")
        timeout_text = "-" if timeout is None else f"{timeout}s"

        lines = [
            "📋 <b>Scheduler Job 详情</b>",
            "",
            f"<b>Job ID</b>: {self._code(job.get('job_id'))}",
            f"<b>名称</b>: {self._html(self._cell(job.get('name')))}",
            f"<b>描述</b>: {self._html(self._cell(job.get('description')))}",
            f"<b>Scheduler 状态</b>: {self._html(status)}",
            f"<b>触发器</b>: {self._html(trigger)}",
            f"<b>启用</b>: {'yes' if job.get('enabled') else 'no'}",
            f"<b>超时</b>: {self._html(timeout_text)}",
            f"<b>参数</b>: {self._code(self._short_text(metadata, 180))}",
        ]

        if runtime:
            lines.extend([
                "",
                "<b>运行时状态</b>:",
                f"- 累计成功执行: {self._html(self._cell(runtime.get('execution_count')))}",
                f"- 连续失败: {self._html(self._cell(runtime.get('consecutive_failures')))}",
                f"- 最近错误: {self._html(self._cell(runtime.get('last_error')))}",
            ])

        lines.extend(["", f"<b>最近执行记录</b>（{len(executions)} 条）:"])
        if not executions:
            lines.append("暂无执行记录。")
            return "\n".join(lines)

        rows = []
        for item in executions[:10]:
            started_at = self._cell(item.get("started_at"), "")[:19] or "-"
            duration = item.get("duration_ms")
            duration_text = "-" if duration is None else f"{duration}ms"
            message = item.get("error_message") or item.get("result") or "-"
            rows.append([
                started_at,
                self._cell(item.get("status")),
                duration_text,
                self._short_text(message, 72),
            ])
        lines.append(self._pre_table(["开始时间", "状态", "耗时", "错误/结果"], rows))
        return "\n".join(lines)

    def _fmt_scheduler(self, result: Dict) -> str:
        action = result.get("action", "")
        if "error" in result:
            return f"❌ 调度操作失败: {self._html(result['error'])}"

        if action == "list":
            jobs = result.get("jobs", [])
            lines = ["📋 <b>调度任务列表</b>\n"]
            for j in jobs:
                trigger = f"{j.get('trigger_type', '')}={j.get('trigger_value', '')}"
                status_icon = "🟢" if j.get("enabled") else "🔴"
                lines.append(f"{status_icon} <b>{self._html(j.get('name', ''))}</b>")
                lines.append(f"  • ID: {self._code(j.get('job_id', ''))}")
                lines.append(f"  • 触发器: {self._html(trigger)}")
            return "\n".join(lines)

        return (
            f"✅ <b>调度操作完成</b>\n"
            f"操作: {self._html(action)}\n"
            f"任务: {self._code(result.get('job_id', ''))}"
        )

    def _format_eval_result(self, result: Dict) -> str:
        """handle_eval 的专属格式化。"""
        return self._fmt_trigger_eval(result)

    def _format_sample_result(self, result: Dict) -> str:
        """handle_sample 的专属格式化。"""
        return self._fmt_sample_eval(result)

    # ------------------------------------------------------------------
    # 对话历史管理
    # ------------------------------------------------------------------

    def clear_history(self, chat_id: str) -> None:
        """清除指定会话的对话历史。"""
        self._conversations.pop(chat_id, None)
        logger.info("对话历史已清除: chat_id=%s", chat_id)

    def get_history(self, chat_id: str) -> List[Dict[str, str]]:
        """获取指定会话的对话历史（只读）。"""
        return self._conversations.get(chat_id, [])[:]

    @property
    def active_conversations(self) -> int:
        """当前活跃的会话数。"""
        return len(self._conversations)
