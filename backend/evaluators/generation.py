"""生成层评测器 (GenerationEvaluator) —— MVP 确定性部分。

LLM 维度（FactualAccuracy / Completeness / HallucinationScore / SemanticSimilarity）
留待 Phase 2 接入 LLM-as-Judge 后补齐。
"""

import json
import re
from typing import Dict, Any, List

from backend.evaluators.base import BaseEvaluator, EvalMethod


class GenerationEvaluator(BaseEvaluator):
    """生成层评测：LanguageQuality / FormatCompliance（确定部分）。"""

    @property
    def layer_name(self) -> str:
        return "generation"

    @property
    def supported_methods(self):
        return [EvalMethod.DETERMINISTIC]

    def _default_weights(self) -> Dict[str, float]:
        return {
            "FactualAccuracy": 0.25,
            "Completeness": 0.20,
            "LanguageQuality": 0.10,
            "FormatCompliance": 0.10,
            "HallucinationScore": 0.20,
            "SemanticSimilarity": 0.15,
        }

    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        response = span.get("output", {}).get("response", "") or context.get("final_response", "")
        exp_answer = expected.get("expected_answer", {})
        gold_answer = expected.get("gold_answer")

        return {
            # LLM 维度留待 Phase 2，MVP 阶段跳过
            "FactualAccuracy": {"score": 100.0, "skipped": True, "note": "MVP: LLM Judge not yet integrated"},
            "Completeness": self._calc_completeness(response, exp_answer) if not exp_answer.get("divergent_ok") else {"score": 100.0, "skipped": True, "reason": "divergent_ok"},
            "LanguageQuality": self._calc_language_quality(response),
            "FormatCompliance": self._calc_format_compliance(response, exp_answer),
            "HallucinationScore": {"score": 100.0, "skipped": True, "note": "MVP: LLM Judge not yet integrated"},
            "SemanticSimilarity": self._calc_semantic_sim(response, gold_answer) if gold_answer else {"score": 100.0, "skipped": True},
        }

    # ---------- 计算子方法 ----------

    def _calc_language_quality(self, response: str) -> dict:
        if not response:
            return {"score": 0.0}
        sentences = re.split(r'[。！？.!?\n]+', response)
        sentences = [s.strip() for s in sentences if s.strip()]
        if not sentences:
            return {"score": 100.0}

        avg_len = sum(len(s) for s in sentences) / len(sentences)

        if 10 <= avg_len <= 25:
            sent_score = 100
        elif 5 <= avg_len <= 40:
            sent_score = 70
        else:
            sent_score = 40

        # 重复 4-gram 扣分
        words = response.split()
        if len(words) >= 4:
            fourgrams = [" ".join(words[i:i + 4]) for i in range(len(words) - 3)]
            total = len(fourgrams)
            unique = len(set(fourgrams))
            repeat_ratio = (total - unique) / max(total, 1)
            repeat_penalty = min(repeat_ratio * 100, 30)
        else:
            repeat_penalty = 0

        score = max(0, sent_score - repeat_penalty)
        return {"score": round(score, 2), "avg_sentence_len": round(avg_len, 1), "sentences": len(sentences)}

    def _calc_format_compliance(self, response: str, exp_answer: Dict) -> dict:
        expected_format = exp_answer.get("format", "text")
        if expected_format == "json":
            try:
                json.loads(response)
                return {"score": 100.0}
            except (json.JSONDecodeError, TypeError):
                # 尝试提取 ```json``` 代码块
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', response, re.DOTALL)
                if match:
                    try:
                        json.loads(match.group(1))
                        return {"score": 80.0}
                    except (json.JSONDecodeError, TypeError):
                        pass
                return {"score": 0.0}
        return {"score": 100.0}

    def _calc_semantic_sim(self, response: str, gold_answer: str) -> dict:
        # MVP 阶段：简单词重叠率
        if not gold_answer:
            return {"score": 100.0, "skipped": True}
        resp_words = set(response.lower().split())
        gold_words = set(gold_answer.lower().split())
        if not gold_words:
            return {"score": 100.0}
        overlap = len(resp_words & gold_words) / len(gold_words)
        score = min(overlap * 100, 100)
        return {"score": round(score, 2), "method": "word_overlap"}

    def _calc_completeness(self, response: str, exp_answer: Dict) -> dict:
        check_points = exp_answer.get("check_points", [])
        if not check_points:
            return {"score": 100.0}

        total_weight = 0.0
        earned_weight = 0.0
        details = []

        for cp in check_points:
            point_text = cp.get("point", cp.get("key", str(cp)))
            w = cp.get("weight", 1.0)
            match_mode = cp.get("match", "must_contain")
            covered = point_text.lower() in response.lower()

            detail = {"point": point_text, "match_mode": match_mode, "covered": covered, "weight": w}

            if match_mode == "must_contain":
                total_weight += w
                if covered:
                    earned_weight += w
            elif match_mode == "prefer_contain":
                total_weight += w
                earned_weight += w * (1.0 if covered else 0.5)
            elif match_mode == "nice_to_have":
                if covered:
                    earned_weight += w * 0.5
                # 不纳入 forced_total

            details.append(detail)

        score = (earned_weight / total_weight * 100) if total_weight > 0 else 100.0
        return {"score": round(min(score, 100), 2), "details": details, "forced_weight": total_weight}
