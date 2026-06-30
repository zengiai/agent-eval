"""Celery 评测 Worker —— 异步执行单条 Case 的评测任务。

在 Ingest 消费完一条 Trace 的所有 Span 后触发，调用编排器执行五层评测。
"""

import asyncio
import json
import logging
from datetime import datetime
from uuid import UUID
from typing import Dict, Any

from sqlalchemy import select, update, desc
from sqlalchemy.orm import selectinload

from backend.core.database import async_session_factory
from backend.core.models import (
    Trace, Span, EvalRun, EvalScore, EvalTask, EvalCase,
)
from backend.runner.engine import EvaluationOrchestrator
from backend.core.config import settings
from backend.case_set_results.service import recompute_case_set_result_best_effort

logger = logging.getLogger(__name__)


async def evaluate_trace(trace_id: str, eval_run_id: str = None) -> Dict[str, Any]:
    """对一条已完成 Trace 执行五层评测。

    Args:
        trace_id: Trace UUID 字符串
        eval_run_id: EvalRun UUID 字符串，用于关联 EvalScore 到具体 Run（支持多轮评分隔离）

    Returns:
        评测结果字典，含各层 EvalResult 和加权总分。

    流程：
        1. 加载 Trace + Spans + 关联的 EvalRun
        2. 从 EvalRun.expected_snapshot 获取期望值
        3. 调用 EvaluationOrchestrator 执行评测
        4. 写入 eval_scores（含 eval_run_id，Outcome 层 span_id=NULL）
        5. 回填 spans.score 和 traces.overall_score
        6. 更新 eval_run.status
    """
    async with async_session_factory() as session:
        # 1. 加载数据
        trace = await session.get(Trace, UUID(trace_id))
        if not trace:
            logger.error("Trace %s not found", trace_id)
            return {"error": "Trace not found"}

        # 加载关联的 Spans
        result = await session.execute(
            select(Span).where(Span.trace_id == UUID(trace_id)).order_by(Span.sequence)
        )
        spans = result.scalars().all()

        # 加载关联的 EvalRun（优先通过 eval_run_id 精确查询，避免并发场景取错）
        eval_run = None
        if eval_run_id:
            eval_run = await session.get(EvalRun, UUID(eval_run_id))
        if not eval_run:
            # 兜底：取该 trace 最新一条
            result = await session.execute(
                select(EvalRun)
                .where(EvalRun.trace_id == UUID(trace_id))
                .order_by(desc(EvalRun.created_at))
                .limit(1)
            )
            eval_run = result.scalar_one_or_none()

        # 2. 准备期望值
        expected_snapshot = {}
        if eval_run and eval_run.expected_snapshot:
            expected_snapshot = eval_run.expected_snapshot

        # 3. 执行评测
        trace_dict = {
            "id": str(trace.id),
            "agent_version": trace.agent_version,
            "query": trace.query,
            "context": trace.context,
            "final_response": trace.final_response,
            "status": trace.status,
            "source": trace.source,
            "total_latency_ms": trace.total_latency_ms,
            "total_tokens": trace.total_tokens,
            "total_cost_usd": trace.total_cost_usd,
            "spans": [
                {
                    "id": str(s.id),
                    "trace_id": str(s.trace_id),
                    "span_type": s.span_type,
                    "sequence": s.sequence,
                    "input": s.input,
                    "output": s.output,
                    "tool_name": s.tool_name,
                    "tool_params": s.tool_params,
                    "tool_result": s.tool_result,
                    "tool_status": s.tool_status,
                    "latency_ms": s.latency_ms,
                    "tokens": s.tokens,
                    "model": s.model,
                }
                for s in spans
            ],
        }

        orchestrator = EvaluationOrchestrator(
            config={
                "enabled_layers": ["generation"],
                "llm": {
                    "model": settings.LLM_MODEL,
                    "api_key": settings.LLM_API_KEY,
                    "base_url": settings.LLM_BASE_URL,
                    "temperature": settings.LLM_TEMPERATURE,
                    "max_retries": settings.LLM_MAX_RETRIES,
                },
            }
        )
        # 将同步阻塞的 orchestrator.run() 放入线程池执行，避免阻塞事件循环
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, orchestrator.run, trace_dict, expected_snapshot)

        # 4. 写入 eval_scores（不再删除旧分数，通过 eval_run_id 隔离多轮评分）
        span_map = {s.span_type: s for s in spans}
        # 处理 tool_call（可能有多个，取第一个匹配）
        tool_spans = [s for s in spans if s.span_type == "tool_call"]

        run_uuid = eval_run.id if eval_run else (UUID(eval_run_id) if eval_run_id else None)

        for layer in orchestrator.enabled_layers:
            layer_result = results.get(layer)
            if layer_result is None or layer_result.error:
                continue

            trace_uuid = UUID(trace_id)
            if layer == "outcome":
                # Outcome 层不绑定 span，但记录 trace_id
                eval_score = EvalScore(
                    trace_id=trace_uuid,
                    span_id=None,
                    eval_run_id=run_uuid,
                    score=layer_result.total_score,
                    metrics=layer_result.metrics,
                    evaluator_version=layer_result.evaluator_version,
                    judge_trace=layer_result.judge_trace,
                    evaluation_latency_ms=int(layer_result.latency_ms),
                    method=layer_result.method.value,
                )
                session.add(eval_score)
            elif layer == "tool":
                if tool_spans:
                    eval_score = EvalScore(
                        trace_id=trace_uuid,
                        span_id=tool_spans[0].id,
                        eval_run_id=run_uuid,
                        score=layer_result.total_score,
                        metrics=layer_result.metrics,
                        evaluator_version=layer_result.evaluator_version,
                        judge_trace=layer_result.judge_trace,
                        evaluation_latency_ms=int(layer_result.latency_ms),
                        method=layer_result.method.value,
                    )
                    session.add(eval_score)
            else:
                span = span_map.get(layer)
                if span:
                    eval_score = EvalScore(
                        trace_id=trace_uuid,
                        span_id=span.id,
                        eval_run_id=run_uuid,
                        score=layer_result.total_score,
                        metrics=layer_result.metrics,
                        evaluator_version=layer_result.evaluator_version,
                        judge_trace=layer_result.judge_trace,
                        evaluation_latency_ms=int(layer_result.latency_ms),
                        method=layer_result.method.value,
                    )
                    session.add(eval_score)

        await session.flush()

        # 5. 回写 LLM 自动生成的 expected_snapshot
        gen_result = results.get("generation")
        if gen_result and gen_result.metrics.get("_enriched_expected") and eval_run:
            enriched = dict(gen_result.metrics["_enriched_expected"])
            # 清理内部标记字段，不污染持久化数据
            enriched.pop("_enriched_expected", None)
            enriched.pop("_annotation_source", None)
            await session.execute(
                update(EvalRun).where(EvalRun.id == eval_run.id).values(
                    expected_snapshot=enriched,
                )
            )

        # 6. 回填 spans.score（仅前四层）
        for layer in ["intent", "retrieval", "tool", "generation"]:
            layer_result = results.get(layer)
            span = span_map.get(layer)
            if span and layer_result and layer_result.error is None:
                await session.execute(
                    update(Span).where(Span.id == span.id).values(score=layer_result.total_score)
                )
            # 对 tool 层，如果有多个 span，也回填第一个
            if layer == "tool" and tool_spans and layer_result and layer_result.error is None:
                await session.execute(
                    update(Span).where(Span.id == tool_spans[0].id).values(score=layer_result.total_score)
                )

        # 回填 traces.overall_score
        overall = results["__overall__"]
        await session.execute(
            update(Trace).where(Trace.id == UUID(trace_id)).values(overall_score=overall)
        )

        # 7. 更新 eval_run.status
        if eval_run:
            await session.execute(
                update(EvalRun).where(EvalRun.id == eval_run.id).values(
                    status="completed",
                    completed_at=datetime.utcnow(),
                )
            )

        task_id_for_pass_result = eval_run.task_id if eval_run else None

        await session.commit()

        # 旁路计算评测集 pass 结果。best-effort，失败只记录日志，不影响主评分结果。
        await recompute_case_set_result_best_effort(task_id_for_pass_result)

        logger.info("评测完成: trace=%s overall=%.2f layers=%s",
                     trace_id, overall, list(results.keys()))

        return {
            "trace_id": trace_id,
            "overall_score": overall,
            "layers": {k: v.to_dict() for k, v in results.items() if hasattr(v, "to_dict")},
            "meta": results.get("__meta__", {}),
        }
