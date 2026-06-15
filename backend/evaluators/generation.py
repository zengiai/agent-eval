"""生成层评测器 (GenerationEvaluator)。

混合评测：确定性维度（LanguageQuality / FormatCompliance）+ LLM Judge 维度（FactualAccuracy / Completeness / HallucinationScore）。
SemanticSimilarity 暂用词重叠，后续可升级为 BERTScore。
"""

import json
import logging
import re
from typing import Dict, Any, List, Optional

from backend.evaluators.base import BaseEvaluator, EvalMethod

logger = logging.getLogger(__name__)


class GenerationEvaluator(BaseEvaluator):
    """生成层评测：6 维度加权综合评分。

    LLM 维度依赖 llm_judge 通过 context 传入：
        context["llm_judge"] → LLMJudge 实例
    无 LLM Judge 时，LLM 维度标记为 skipped（降级到 MVP 行为）。
    """

    @property
    def layer_name(self) -> str:
        return "generation"

    @property
    def supported_methods(self):
        return [EvalMethod.HYBRID]

    def _default_weights(self) -> Dict[str, float]:
        return {
            "FactualAccuracy": 0.25,
            "Completeness": 0.20,
            "LanguageQuality": 0.10,
            "FormatCompliance": 0.10,
            "HallucinationScore": 0.20,
            "SemanticSimilarity": 0.15,
        }

    # ── 获取 LLM Judge ──────────────────────────────────────────

    def _get_llm_judge(self, context: Dict) -> Optional[Any]:
        """从 context 中提取 LLMJudge 实例。"""
        return context.get("llm_judge")

    # ── 核心评测入口 ────────────────────────────────────────────

    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        response = span.get("output", {}).get("response", "") or context.get("final_response", "")
        query = context.get("query", "")
        exp_answer = expected.get("expected_answer", {})
        gold_answer = expected.get("gold_answer", "")
        llm_judge = self._get_llm_judge(context)

        return {
            "FactualAccuracy": self._calc_factual_accuracy(response, query, gold_answer, llm_judge),
            "Completeness": self._calc_completeness(response, query, exp_answer, llm_judge),
            "LanguageQuality": self._calc_language_quality(response),
            "FormatCompliance": self._calc_format_compliance(response, exp_answer),
            "HallucinationScore": self._calc_hallucination(response, query, gold_answer, llm_judge),
            "SemanticSimilarity": self._calc_semantic_sim(response, gold_answer),
        }

    # ── LLM Judge 维度 ──────────────────────────────────────────

    def _calc_factual_accuracy(
        self, response: str, query: str, gold_answer: str, llm_judge
    ) -> dict:
        """事实准确性：LLM Judge 评分 1-5 → 0-100。"""
        if not response:
            return {"score": 0.0, "error": "empty response"}

        if llm_judge and llm_judge.is_available():
            try:
                result = llm_judge.judge_by_template(
                    "generation/factual_accuracy",
                    {
                        "query": query or "(未提供问题)",
                        "response": response,
                        "gold_answer": gold_answer or "(未提供参考标准答案，请基于自身知识判断)",
                    },
                )
                llm_score = result.get("score", 3)
                return {
                    "score": round((llm_score / 5) * 100, 2),
                    "llm_score": llm_score,
                    "judge_trace": result,
                    "method": "llm_judge",
                }
            except Exception as e:
                logger.warning("FactualAccuracy LLM 评测失败，回退跳过: %s", e)
                return {"score": 100.0, "skipped": True, "error": str(e)}

        return {"score": 100.0, "skipped": True, "note": "LLM Judge not available"}

    def _calc_completeness(
        self, response: str, query: str, exp_answer: Dict, llm_judge
    ) -> dict:
        """完整性：优先 LLM Judge 语义判断，回退关键词匹配。

        发散型问题（divergent_ok=true）跳过此维度。
        """
        if exp_answer.get("divergent_ok"):
            return {"score": 100.0, "skipped": True, "reason": "divergent_ok"}

        check_points = exp_answer.get("check_points", [])
        if not check_points:
            return {"score": 100.0}

        if not response:
            return {"score": 0.0, "error": "empty response"}

        # 优先 LLM Judge
        if llm_judge and llm_judge.is_available():
            try:
                # 格式化检查点列表
                cp_text = "\n".join(
                    f"{i+1}. {cp.get('point', str(cp))} (匹配模式: {cp.get('match', 'must_contain')})"
                    for i, cp in enumerate(check_points)
                )
                result = llm_judge.judge_by_template(
                    "generation/completeness",
                    {
                        "query": query or "(未提供问题)",
                        "response": response,
                        "check_points": cp_text,
                    },
                )
                return self._parse_completeness_llm_result(result, check_points)
            except Exception as e:
                logger.warning("Completeness LLM 评测失败，回退关键词匹配: %s", e)

        # 回退：关键词匹配
        return self._calc_completeness_keyword(response, check_points)

    def _parse_completeness_llm_result(self, result: Dict, check_points: List[Dict]) -> dict:
        """将 LLM completeness 结果转换为加权得分。"""
        llm_results = result.get("results", [])
        total_weight = 0.0
        earned_weight = 0.0
        details = []

        for i, cp in enumerate(check_points):
            point_text = cp.get("point", cp.get("key", str(cp)))
            w = cp.get("weight", 1.0)
            match_mode = cp.get("match", "must_contain")

            # 从 LLM 结果中匹配对应检查点（按索引或文本模糊匹配）
            llm_item = {}
            if i < len(llm_results):
                llm_item = llm_results[i]
            coverage = llm_item.get("coverage", "not_covered")

            detail = {
                "point": point_text,
                "match_mode": match_mode,
                "coverage": coverage,
                "weight": w,
                "evidence": llm_item.get("evidence", ""),
            }

            if match_mode == "must_contain":
                total_weight += w
                if coverage == "fully_covered":
                    earned_weight += w
                elif coverage == "partially_covered":
                    earned_weight += w * 0.5
            elif match_mode == "prefer_contain":
                total_weight += w
                if coverage == "fully_covered":
                    earned_weight += w
                elif coverage == "partially_covered":
                    earned_weight += w * 0.7
                else:
                    earned_weight += w * 0.5
            elif match_mode == "nice_to_have":
                if coverage in ("fully_covered", "partially_covered"):
                    earned_weight += w * 0.5

            details.append(detail)

        score = (earned_weight / total_weight * 100) if total_weight > 0 else 100.0
        return {
            "score": round(min(score, 100), 2),
            "details": details,
            "forced_weight": total_weight,
            "method": "llm_judge",
            "llm_overall_completeness": result.get("overall_completeness"),
        }

    def _calc_hallucination(
        self, response: str, query: str, gold_answer: str, llm_judge
    ) -> dict:
        """幻觉检测：LLM Judge 逐句标注 → 干净句比例 × 100。"""
        if not response:
            return {"score": 0.0, "error": "empty response"}

        if llm_judge and llm_judge.is_available():
            try:
                ref_material = gold_answer if gold_answer else "(未提供参考材料)"
                result = llm_judge.judge_by_template(
                    "generation/hallucination",
                    {
                        "query": query or "(未提供问题)",
                        "response": response,
                        "reference_materials": ref_material,
                    },
                )
                total = result.get("total_sentences", 1)
                hallucination_count = result.get("hallucination_count", 0)
                ratio = hallucination_count / max(total, 1)
                score = max(0, (1 - ratio)) * 100
                return {
                    "score": round(score, 2),
                    "total_sentences": total,
                    "hallucination_count": hallucination_count,
                    "hallucination_ratio": round(ratio, 4),
                    "overall_severity": result.get("overall_severity", "unknown"),
                    "judge_trace": result,
                    "method": "llm_judge",
                }
            except Exception as e:
                logger.warning("HallucinationScore LLM 评测失败，回退跳过: %s", e)
                return {"score": 100.0, "skipped": True, "error": str(e)}

        return {"score": 100.0, "skipped": True, "note": "LLM Judge not available"}

    # ── 确定性维度 ───────────────────────────────────────────────

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
        if not gold_answer:
            return {"score": 100.0, "skipped": True}
        resp_words = set(response.lower().split())
        gold_words = set(gold_answer.lower().split())
        if not gold_words:
            return {"score": 100.0}
        overlap = len(resp_words & gold_words) / len(gold_words)
        score = min(overlap * 100, 100)
        return {"score": round(score, 2), "method": "word_overlap"}

    def _calc_completeness_keyword(self, response: str, check_points: List[Dict]) -> dict:
        """关键词匹配（LLM 不可用时的回退方案）。"""
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

            details.append(detail)

        score = (earned_weight / total_weight * 100) if total_weight > 0 else 100.0
        return {
            "score": round(min(score, 100), 2),
            "details": details,
            "forced_weight": total_weight,
            "method": "keyword_match",
        }
