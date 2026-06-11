"""效果层评测器 (OutcomeEvaluator) —— MVP 确定性部分。

效果层不绑定单条 span，而是对整条 Trace 的端到端评估。
"""

import math
from typing import Dict, Any, List

from backend.evaluators.base import BaseEvaluator, EvalMethod


class OutcomeEvaluator(BaseEvaluator):
    """效果层评测：TaskCompletion / SatisfactionEstimate / LatencyScore / TokenEfficiency / ErrorRecovery。"""

    @property
    def layer_name(self) -> str:
        return "outcome"

    @property
    def supported_methods(self):
        return [EvalMethod.DETERMINISTIC]

    def _default_weights(self) -> Dict[str, float]:
        return {
            "TaskCompletion": 0.35,
            "SatisfactionEstimate": 0.15,
            "LatencyScore": 0.20,
            "TokenEfficiency": 0.10,
            "ErrorRecovery": 0.20,
        }

    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        trace = context.get("trace", {})
        all_spans = context.get("all_spans", [])
        layer_results = {
            k: v for k, v in context.items()
            if k.endswith("_result")
        }

        return {
            "TaskCompletion": self._calc_task_completion(trace, layer_results),
            "SatisfactionEstimate": self._calc_satisfaction(trace, layer_results),
            "LatencyScore": self._calc_latency(trace, all_spans),
            "TokenEfficiency": self._calc_token_efficiency(trace, layer_results),
            "ErrorRecovery": self._calc_error_recovery(all_spans),
        }

    # ---------- 计算子方法 ----------

    def _calc_task_completion(self, trace: Dict, layer_results: Dict) -> dict:
        # MVP 阶段：基于前四层得分简单推断。Phase 2 接入 LLM Judge 后替换。
        scores = []
        for key in ["intent_result", "retrieval_result", "tool_result", "generation_result"]:
            r = layer_results.get(key)
            if r and hasattr(r, "total_score"):
                scores.append(r.total_score)

        if not scores:
            return {"score": 50.0, "note": "MVP: no layer results available"}

        score = sum(scores) / len(scores)
        # 状态惩罚
        if trace.get("status") not in ("success", None):
            score *= 0.5

        return {"score": round(score, 2), "method": "layer_score_average", "layer_count": len(scores)}

    def _calc_satisfaction(self, trace: Dict, layer_results: Dict) -> dict:
        task_score = self._calc_task_completion(trace, layer_results).get("score", 50) / 100
        total_latency = trace.get("total_latency_ms", 0) or 0

        penalty = 0.0
        if total_latency > 10000:
            penalty += 0.10
        if trace.get("status") not in ("success", None):
            penalty += 0.30

        score = max(0, task_score * (1 - penalty)) * 100
        return {"score": round(score, 2), "penalty": penalty, "task_score": task_score}

    def _calc_latency(self, trace: Dict, all_spans: List[Dict]) -> dict:
        total_ms = trace.get("total_latency_ms") or sum(s.get("latency_ms", 0) or 0 for s in all_spans)

        if total_ms < 1000:
            score = 100
        elif total_ms < 3000:
            score = 90
        elif total_ms < 5000:
            score = 75
        elif total_ms < 10000:
            score = 50
        elif total_ms < 20000:
            score = 25
        else:
            score = 10

        return {"score": score, "total_latency_ms": total_ms}

    def _calc_token_efficiency(self, trace: Dict, layer_results: Dict) -> dict:
        total_tokens = trace.get("total_tokens", {})
        if isinstance(total_tokens, dict):
            total = total_tokens.get("input", 0) + total_tokens.get("output", 0)
        else:
            total = 100  # 默认值

        scores = []
        for key in ["intent_result", "retrieval_result", "tool_result", "generation_result"]:
            r = layer_results.get(key)
            if r and hasattr(r, "total_score"):
                scores.append(r.total_score)

        avg_score = sum(scores) / len(scores) if scores else 50
        if total <= 0:
            total = 1  # 避免 log2(1)=0 导致除零
        efficiency = avg_score / max(math.log2(total + 1), 0.01)
        score = min(max(efficiency * 10, 0), 100)
        return {"score": round(score, 2), "total_tokens": total, "avg_layer_score": avg_score}

    def _calc_error_recovery(self, all_spans: List[Dict]) -> dict:
        tool_spans = [s for s in all_spans if s.get("span_type") == "tool_call"]
        if not tool_spans:
            return {"score": 100.0}

        errors = [
            s for s in tool_spans
            if s.get("tool_status") not in (None, "success", "")
            and (s.get("tool_result") or {}).get("status") not in (None, "success", "")
        ]

        if not errors:
            return {"score": 100.0}

        # 检查错误后是否有同工具重试成功
        recovered = 0
        for i, err in enumerate(errors):
            err_name = err.get("tool_name", "")
            for later in tool_spans[i + 1:]:
                later_name = later.get("tool_name", "")
                later_status = later.get("tool_status") or (later.get("tool_result") or {}).get("status", "")
                if later_name == err_name and later_status in (None, "success", ""):
                    recovered += 1
                    break

        recovery_rate = recovered / len(errors)
        score = 50 + recovery_rate * 50
        return {"score": round(score, 2), "errors": len(errors), "recovered": recovered, "recovery_rate": recovery_rate}
