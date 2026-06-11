"""工具层评测器 (ToolEvaluator) —— MVP 确定性部分。"""

from typing import Dict, Any, List

from backend.evaluators.base import BaseEvaluator, EvalMethod


class ToolEvaluator(BaseEvaluator):
    """工具层评测：ToolSelection / ParamAccuracy / SequenceCorrectness / Efficiency / SuccessRate。"""

    @property
    def layer_name(self) -> str:
        return "tool"

    @property
    def supported_methods(self):
        return [EvalMethod.DETERMINISTIC]

    def _default_weights(self) -> Dict[str, float]:
        return {
            "ToolSelection": 0.30,
            "ParamAccuracy": 0.25,
            "SequenceCorrectness": 0.20,
            "Efficiency": 0.10,
            "SuccessRate": 0.15,
        }

    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        # span 可能是单个 dict 或 list（多个 tool_call）
        all_spans = span if isinstance(span, list) else [span]
        expected_tools = expected.get("expected_tools", [])
        has_expected = bool(expected_tools)

        return {
            "ToolSelection": self._calc_selection(all_spans, expected_tools) if has_expected else {"score": 100.0, "skipped": True},
            "ParamAccuracy": self._calc_params(all_spans, expected_tools) if has_expected else {"score": 100.0, "skipped": True},
            "SequenceCorrectness": self._calc_sequence(all_spans, expected_tools) if has_expected else {"score": 100.0, "skipped": True},
            "Efficiency": self._calc_efficiency(all_spans, expected_tools),
            "SuccessRate": self._calc_success_rate(all_spans),
        }

    # ---------- 计算子方法 ----------

    def _calc_selection(self, actual_spans: List[Dict], expected_tools: List[Dict]) -> dict:
        actual_names = [s.get("tool_name", "") for s in actual_spans]
        expected_names = [et.get("tool_name", "") for et in expected_tools]

        matches = 0
        for exp_name in expected_names:
            if exp_name in actual_names:
                matches += 1.0
            elif any(self._alt_match(exp_name, an) for an in actual_names):
                matches += 0.7

        score = (matches / len(expected_names)) * 100 if expected_names else 100.0
        return {"score": round(score, 2), "matches": matches, "total": len(expected_names)}

    def _alt_match(self, exp_name: str, actual_name: str) -> bool:
        """简化版替代匹配：名称包含关系。完整版需要 LLM 语义判断。"""
        return exp_name.lower() in actual_name.lower() or actual_name.lower() in exp_name.lower()

    def _calc_params(self, actual_spans: List[Dict], expected_tools: List[Dict]) -> dict:
        total_params = 0
        matched_params = 0

        for exp_tool in expected_tools:
            exp_name = exp_tool.get("tool_name", "")
            exp_params = exp_tool.get("params", {})
            required = exp_tool.get("required_params", list(exp_params.keys()))

            # 找到对应的实际工具调用
            matching_span = None
            for s in actual_spans:
                if s.get("tool_name") == exp_name or self._alt_match(exp_name, s.get("tool_name", "")):
                    matching_span = s
                    break

            if matching_span:
                actual_params = matching_span.get("tool_params", {})
                for key in required:
                    total_params += 1
                    if key in actual_params and str(actual_params[key]) == str(exp_params.get(key, "")):
                        matched_params += 1
            else:
                total_params += len(required)

        score = (matched_params / total_params * 100) if total_params > 0 else 100.0
        return {"score": round(score, 2), "matched": matched_params, "total": total_params}

    def _calc_sequence(self, actual_spans: List[Dict], expected_tools: List[Dict]) -> dict:
        actual_names = [s.get("tool_name", "") for s in actual_spans]
        expected_names = [et.get("tool_name", "") for et in expected_tools]
        is_ordered = any(et.get("order_sensitive", True) for et in expected_tools)

        if is_ordered:
            # 最长公共子序列比率
            a_set = set(actual_names)
            e_set = set(expected_names)
            intersection = a_set & e_set
            union = a_set | e_set
            score = (len(intersection) / len(union) * 100) if union else 100.0
        else:
            # Jaccard
            a_set = set(actual_names)
            e_set = set(expected_names)
            intersection = a_set & e_set
            union = a_set | e_set
            score = (len(intersection) / len(union) * 100) if union else 100.0

        return {"score": round(score, 2), "ordered": is_ordered}

    def _calc_efficiency(self, actual_spans: List[Dict], expected_tools: List[Dict]) -> dict:
        actual_count = len(actual_spans)
        expected_count = len(expected_tools) if expected_tools else max(actual_count, 1)
        diff_ratio = abs(actual_count - expected_count) / max(expected_count, 1)
        score = max(0, 1 - diff_ratio) * 100
        return {"score": round(score, 2), "actual_count": actual_count, "expected_count": expected_count}

    def _calc_success_rate(self, actual_spans: List[Dict]) -> dict:
        if not actual_spans:
            return {"score": 100.0}
        success = 0
        for s in actual_spans:
            status = s.get("tool_status") or s.get("tool_result", {}).get("status", "success")
            if status in (None, "success", ""):
                success += 1
        score = (success / len(actual_spans)) * 100
        return {"score": round(score, 2), "success": success, "total": len(actual_spans)}
