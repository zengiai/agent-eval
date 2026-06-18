"""CommandExecutor —— Agent 大脑的编排者。

串联 LLM 意图理解 → 工具执行 → 回复格式化的全流程。
内建 chat_id 维度多轮对话历史管理。
"""

from __future__ import annotations

import asyncio
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
        → 格式化结果 → 返回 Markdown 文本

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
                        reply = self._format_reply(intent, result)
                    except ValueError as e:
                        # 工具层的参数校验错误（如 ValueError("trace_id 是必填参数")）
                        reply = f"❌ 参数错误: {e}"
                    except KeyError as e:
                        # 注册表不一致（function 存在但定义丢失）
                        logger.error("Function 注册表不一致: %s", e)
                        reply = "❌ 系统内部错误，请联系管理员。"
                    except Exception as e:
                        logger.exception("Tool 执行失败: %s", intent.function_name)
                        reply = f"❌ 执行 `{intent.function_name}` 时出错: {e}，请稍后重试"

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
            return f"❌ 评测触发失败: {e}"

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
            return f"❌ 采样评测失败: {e}"

    # ------------------------------------------------------------------
    # 回复格式化
    # ------------------------------------------------------------------

    def _format_reply(self, intent: IntentResult, result: Any) -> str:
        """根据 function 类型格式化工具执行结果为 Markdown。"""
        func_name = intent.function_name

        # 查询类格式化
        if func_name == "get_latest_eval_status":
            return self._fmt_eval_status(result)
        elif func_name == "query_score_trend":
            return self._fmt_score_trend(result)
        elif func_name == "search_traces":
            return self._fmt_search_traces(result)
        elif func_name == "get_trace_detail":
            return self._fmt_trace_detail(result)
        elif func_name == "list_case_sets":
            return self._fmt_case_sets(result)
        elif func_name == "get_weakest_cases":
            return self._fmt_weakest_cases(result)
        # 操作类
        elif func_name == "trigger_evaluation":
            return self._fmt_trigger_eval(result)
        elif func_name == "sample_and_evaluate":
            return self._fmt_sample_eval(result)
        elif func_name == "manage_scheduler":
            return self._fmt_scheduler(result)
        # 报告类
        elif func_name == "compare_versions":
            return self._fmt_compare(result)
        elif func_name == "get_daily_report":
            return self._fmt_daily_report(result)
        elif func_name == "get_alert_history":
            return self._fmt_alert_history(result)
        else:
            return f"✅ 执行完成: `{func_name}`\n```json\n{result}\n```"

    def _format_fallback(self, intent: IntentResult) -> str:
        """格式化兜底回复。"""
        reply = intent.arguments.get("reply", "抱歉，我不太理解你的意思。")
        return (
            f"{reply}\n\n"
            "💡 试试以下操作：\n"
            "• 查状态：「最近评测状态怎么样？」\n"
            "• 看趋势：「v2.3.1 的评分趋势」\n"
            "• 搜 Trace：「搜索包含 timeout 的 Trace」\n"
            "• 看版本对比：「对比 v2.3.0 和 v2.3.1」\n"
            "• 输入 /help 查看全部命令"
        )

    def _fmt_eval_status(self, result: Dict) -> str:
        status_lines = "\n".join(
            f"  • {s}: {c} 个" for s, c in result.get("status_counts", {}).items()
        )
        versions = ", ".join(result.get("active_versions", []))
        return (
            f"📊 **评测状态概览**（过去 {result.get('hours_back', 24)} 小时）\n\n"
            f"**任务总数**: {result.get('total_tasks', 0)}\n"
            f"{status_lines}\n"
            f"**平均总分**: {result.get('avg_overall_score', 0)}\n"
            f"**活跃版本**: {versions or '无'}"
        )

    def _fmt_score_trend(self, result: Dict) -> str:
        trend = result.get("trend", [])
        delta = result.get("delta", 0)
        delta_icon = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")

        lines = [f"📊 **评分趋势 — {result.get('version', '')}**"]
        lines.append(f"评测层: {result.get('layer', 'overall')}")
        lines.append("")
        lines.append("| # | 时间 | 得分 |")
        lines.append("|---|------|------|")

        for i, t in enumerate(trend, 1):
            time_str = t.get("run_time", "")[:16] if t.get("run_time") else ""
            score = t.get("score", 0)
            lines.append(f"| {i} | {time_str} | {score:.1f} |")

        lines.append("")
        lines.append(f"{delta_icon} 趋势: {'+' if delta > 0 else ''}{delta:.1f} (较上次)")
        return "\n".join(lines)

    def _fmt_search_traces(self, result: Dict) -> str:
        traces = result.get("traces", [])
        total = result.get("total", 0)

        lines = [f"🔍 **Trace 搜索结果**（共 {total} 条，显示 {len(traces)} 条）\n"]
        lines.append("| ID | 版本 | 得分 | 状态 | 时间 |")
        lines.append("|---|------|------|------|------|")

        for t in traces:
            score_str = f"{t.get('overall_score', '-')}" if t.get('overall_score') is not None else "-"
            time_str = t.get("created_at", "")[:16] if t.get("created_at") else ""
            lines.append(
                f"| `{t.get('id', '')}` | {t.get('agent_version', '')} | {score_str} | {t.get('status', '')} | {time_str} |"
            )

        return "\n".join(lines)

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
            f"📋 **Trace 详情**\n\n"
            f"**ID**: `{trace.get('id', '')}`\n"
            f"**Query**: {trace.get('query', '')[:200]}\n"
            f"**状态**: {trace.get('status', '')}\n"
            f"**总分**: {trace.get('overall_score', '-')}\n"
            f"**版本**: {trace.get('agent_version', '')}\n"
            f"**延迟**: {trace.get('total_latency_ms', '-')}ms\n\n"
            f"**Spans** ({len(spans)}):\n{span_lines}\n\n"
            f"**评分明细** ({len(scores)}):\n{score_lines}"
        )

    def _fmt_case_sets(self, result: Dict) -> str:
        sets = result.get("case_sets", [])
        lines = [f"📦 **测试用例集**（共 {result.get('total', 0)} 个）\n"]
        lines.append("| 名称 | 分类 | 用例数 | 版本 |")
        lines.append("|------|------|--------|------|")

        for cs in sets[:20]:
            lines.append(
                f"| {cs.get('name', '')} | {cs.get('category', '-')} | {cs.get('case_count', 0)} | {cs.get('version', '')} |"
            )

        return "\n".join(lines)

    def _fmt_weakest_cases(self, result: Dict) -> str:
        cases = result.get("cases", [])
        lines = [f"⚠️ **弱点评分用例**（最低 {result.get('top_n', 10)} 个）\n"]
        lines.append("| # | 用例 | 分类 | 难度 | 均分 | 评测次数 |")
        lines.append("|---|------|------|------|------|----------|")

        for i, c in enumerate(cases, 1):
            query = c.get("query", "")[:40]
            lines.append(
                f"| {i} | {query} | {c.get('category', '-')} | {c.get('difficulty', '-')} | {c.get('avg_score', 0)} | {c.get('run_count', 0)} |"
            )

        return "\n".join(lines)

    def _fmt_trigger_eval(self, result: Dict) -> str:
        return (
            f"✅ **评测任务已创建**\n\n"
            f"**任务 ID**: `{result.get('task_id', '')}`\n"
            f"**版本**: {result.get('agent_version', '')}\n"
            f"**测试集**: {result.get('case_set_name', '')}\n"
            f"**用例数**: {result.get('total_cases', 0)}\n"
            f"**评测层**: {', '.join(result.get('layers', []))}\n\n"
            f"评测将在后台执行，可通过「最近评测状态」查询进度。"
        )

    def _fmt_sample_eval(self, result: Dict) -> str:
        sampled = result.get("sampled", 0)
        if sampled == 0:
            return f"ℹ️ {result.get('message', '没有找到符合条件的生产 Trace')}"
        return (
            f"✅ **采样评测已触发**\n\n"
            f"**采样量**: {sampled}\n"
            f"**批次**: `{result.get('batch_id', '')}`\n"
            f"**任务 ID**: `{result.get('task_id', '')}`\n"
            f"**时间窗口**: 过去 {result.get('hours_back', 24)} 小时"
        )

    def _fmt_scheduler(self, result: Dict) -> str:
        action = result.get("action", "")
        if "error" in result:
            return f"❌ 调度操作失败: {result['error']}"

        if action == "list":
            jobs = result.get("jobs", [])
            lines = ["📋 **调度任务列表**\n"]
            for j in jobs:
                trigger = f"{j.get('trigger_type', '')}={j.get('trigger_value', '')}"
                status_icon = "🟢" if j.get("enabled") else "🔴"
                lines.append(f"{status_icon} **{j.get('name', '')}**")
                lines.append(f"  • ID: `{j.get('job_id', '')}`")
                lines.append(f"  • 触发器: {trigger}")
            return "\n".join(lines)

        return (
            f"✅ **调度操作完成**\n"
            f"操作: {action}\n"
            f"任务: `{result.get('job_id', '')}`"
        )

    def _fmt_compare(self, result: Dict) -> str:
        items = result.get("comparison", [])
        delta = result.get("overall_delta", 0)
        significant = result.get("significant", False)
        sig_text = "⚠️ 差异显著（≥5 分）" if significant else "✅ 差异不显著"

        lines = [
            f"📊 **版本对比**",
            f"{result.get('version_a', '')} vs {result.get('version_b', '')}",
            "",
        ]
        for item in items:
            item_delta = item.get("delta", delta)
            lines.append(
                f"**{item.get('metric', 'overall')}**: "
                f"{item.get('version_a', 0)} → {item.get('version_b', 0)} "
                f"(Δ {'+' if item_delta > 0 else ''}{item_delta})"
            )
        lines.append("")
        lines.append(sig_text)
        return "\n".join(lines)

    def _fmt_daily_report(self, result: Dict) -> str:
        layers = result.get("layers", {})
        layer_lines = "\n".join(
            f"  • {k}: {v}" for k, v in layers.items()
        )
        return (
            f"📊 **评测日报 — {result.get('date', '')}**\n\n"
            f"**评测总量**: {result.get('total_evals', 0)}\n"
            f"**任务数**: {result.get('total_tasks', 0)}\n"
            f"**平均总分**: {result.get('avg_score', 0)}\n"
            f"**各层得分**:\n{layer_lines}\n"
            f"**告警**: {result.get('alert_count', 0)} 条"
        )

    def _fmt_alert_history(self, result: Dict) -> str:
        alerts = result.get("alerts", [])
        if not alerts:
            return f"ℹ️ 过去 {result.get('hours_back', 24)} 小时内暂无告警记录。"

        lines = [
            f"🔔 **告警历史**（过去 {result.get('hours_back', 24)} 小时，共 {result.get('total', 0)} 条）\n"
        ]
        for a in alerts[:10]:
            sev_emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(a.get("severity", ""), "⚪")
            lines.append(
                f"{sev_emoji} **{a.get('rule_name', a.get('rule_id', ''))}** — {a.get('message', '')[:100]}"
            )
            if a.get("current_value") is not None:
                lines.append(f"  当前值: {a.get('current_value')} / 阈值: {a.get('threshold', '-')}")
            lines.append(f"  _{a.get('checked_at', '')[:16]}_")

        return "\n".join(lines)

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
