"""意图层评测器 (IntentEvaluator) —— MVP 确定性部分。"""

from typing import Dict, Any, List

from backend.evaluators.base import BaseEvaluator, EvalMethod


class IntentEvaluator(BaseEvaluator):
    """意图层评测：IntentAccuracy / IntentCompleteness / ConfidenceCalibration / NERAccuracy。"""

    @property
    def layer_name(self) -> str:
        return "intent"

    @property
    def supported_methods(self):
        return [EvalMethod.DETERMINISTIC]

    def _default_weights(self) -> Dict[str, float]:
        return {
            "IntentAccuracy": 0.35,
            "IntentCompleteness": 0.25,
            "ConfidenceCalibration": 0.20,
            "NERAccuracy": 0.20,
        }

    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        predicted = span.get("output", {})
        exp_intent = expected.get("expected_intent", {})

        has_intents = bool(exp_intent.get("intents"))
        has_entities = bool(exp_intent.get("entities"))

        return {
            "IntentAccuracy": self._calc_accuracy(predicted, exp_intent) if has_intents else {"score": 100.0, "skipped": True},
            "IntentCompleteness": self._calc_completeness(predicted, exp_intent) if has_intents else {"score": 100.0, "skipped": True},
            "ConfidenceCalibration": self._calc_calibration(predicted),
            "NERAccuracy": self._calc_ner(predicted, exp_intent) if has_entities else {"score": 100.0, "skipped": True},
        }

    # ---------- 计算子方法 ----------

    def _calc_accuracy(self, predicted: Dict, exp_intent: Dict) -> dict:
        pred_intents = predicted.get("intents", [])
        exp_intents = exp_intent.get("intents", [])
        mode = exp_intent.get("mode", "all")

        if not exp_intents:
            return {"score": 100.0, "mode": mode, "skipped": True}

        if mode == "any":
            hit = any(ei in pred_intents for ei in exp_intents)
            return {"score": 100.0 if hit else 0.0, "mode": "any", "hit": hit}

        if mode == "at_least_n":
            n = exp_intent.get("mode_n", 1)
            hits = sum(1 for ei in exp_intents if ei in pred_intents)
            score = min(hits / n, 1.0) * 100
            return {"score": round(score, 2), "mode": "at_least_n", "hits": hits, "required": n}

        # mode == "all" (default)
        exact = sum(1 for ei in exp_intents if ei in pred_intents)
        score = (exact / len(exp_intents)) * 100
        return {"score": round(score, 2), "mode": "all", "exact": exact, "total": len(exp_intents)}

    def _calc_completeness(self, predicted: Dict, exp_intent: Dict) -> dict:
        pred_intents = predicted.get("intents", []) if isinstance(predicted, dict) else []
        exp_intents = exp_intent.get("intents", [])
        if not exp_intents:
            return {"score": 100.0, "skipped": True}
        covered = sum(1 for ei in exp_intents if ei in pred_intents)
        score = (covered / len(exp_intents)) * 100
        return {"score": round(score, 2), "covered": covered, "total": len(exp_intents)}

    def _calc_calibration(self, predicted: Dict) -> dict:
        confidence = predicted.get("confidence")
        if confidence is None:
            return {"score": 100.0, "skipped": True}
        score = 100.0 if 0.0 <= float(confidence) <= 1.0 else 0.0
        return {"score": score, "confidence": confidence}

    def _calc_ner(self, predicted: Dict, exp_intent: Dict) -> dict:
        pred_entities_raw = predicted.get("entities", [])
        exp_entities_raw = exp_intent.get("entities", [])

        pred_set = set()
        for e in pred_entities_raw:
            if isinstance(e, dict):
                pred_set.add((e.get("type", ""), e.get("value", "")))
            else:
                pred_set.add(("", str(e)))

        exp_set = set()
        for e in exp_entities_raw:
            if isinstance(e, dict):
                exp_set.add((e.get("type", ""), e.get("value", "")))
            else:
                exp_set.add(("", str(e)))

        if not exp_set:
            return {"score": 100.0, "skipped": True}

        tp = len(pred_set & exp_set)
        precision = tp / len(pred_set) if pred_set else 0.0
        recall = tp / len(exp_set) if exp_set else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        return {"score": round(f1 * 100, 2), "precision": precision, "recall": recall, "f1": f1}
