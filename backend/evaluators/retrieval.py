"""召回层评测器 (RetrievalEvaluator) —— 全确定性计算。"""

import math
from typing import Dict, Any, List

from backend.evaluators.base import BaseEvaluator, EvalMethod


class RetrievalEvaluator(BaseEvaluator):
    """召回层评测：Precision@K / Recall@K / MRR / NDCG / Diversity。

    全部使用确定性指标计算，不接入 LLM。
    """

    K = 10  # 默认 @K 值

    @property
    def layer_name(self) -> str:
        return "retrieval"

    @property
    def supported_methods(self):
        return [EvalMethod.DETERMINISTIC]

    def _default_weights(self) -> Dict[str, float]:
        return {
            "PrecisionAtK": 0.25,
            "RecallAtK": 0.25,
            "MRR": 0.20,
            "NDCG": 0.20,
            "Diversity": 0.10,
        }

    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        results = span.get("output", {}).get("results", [])[:self.K]
        relevant_ids = expected.get("expected_retrieval", {}).get("relevant_ids", [])
        has_relevant = bool(relevant_ids)

        return {
            "PrecisionAtK": self._calc_precision(results, relevant_ids) if has_relevant else {"score": 100.0, "skipped": True},
            "RecallAtK": self._calc_recall(results, relevant_ids) if has_relevant else {"score": 100.0, "skipped": True},
            "MRR": self._calc_mrr(results, relevant_ids) if has_relevant else {"score": 100.0, "skipped": True},
            "NDCG": self._calc_ndcg(results, relevant_ids) if has_relevant else {"score": 100.0, "skipped": True},
            "Diversity": self._calc_diversity(results),
        }

    # ---------- 计算子方法 ----------

    def _get_id(self, item: Dict) -> str:
        return str(item.get("id", item.get("doc_id", "")))

    def _calc_precision(self, results: List[Dict], relevant_ids: List[str]) -> dict:
        if not results:
            return {"score": 0.0}
        result_ids = [self._get_id(r) for r in results]
        tp = sum(1 for rid in result_ids if rid in set(relevant_ids))
        score = (tp / len(results)) * 100
        return {"score": round(score, 2), "tp": tp, "k": len(results)}

    def _calc_recall(self, results: List[Dict], relevant_ids: List[str]) -> dict:
        if not relevant_ids:
            return {"score": 100.0, "skipped": True}
        result_ids = [self._get_id(r) for r in results]
        tp = sum(1 for rid in result_ids if rid in set(relevant_ids))
        score = (tp / len(relevant_ids)) * 100
        return {"score": round(score, 2), "tp": tp, "total_relevant": len(relevant_ids)}

    def _calc_mrr(self, results: List[Dict], relevant_ids: List[str]) -> dict:
        rel_set = set(relevant_ids)
        for i, r in enumerate(results, start=1):
            if self._get_id(r) in rel_set:
                return {"score": round((1 / i) * 100, 2), "rank": i}
        return {"score": 0.0}

    def _calc_ndcg(self, results: List[Dict], relevant_ids: List[str]) -> dict:
        if not results:
            return {"score": 0.0}
        rel_set = set(relevant_ids)
        rels = [1 if self._get_id(r) in rel_set else 0 for r in results]

        dcg = sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(rels))
        ideal_rels = sorted(rels, reverse=True)
        idcg = sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(ideal_rels))
        score = (dcg / idcg * 100) if idcg > 0 else 0.0
        return {"score": round(score, 2), "dcg": dcg, "idcg": idcg}

    def _calc_diversity(self, results: List[Dict]) -> dict:
        if len(results) < 2:
            return {"score": 100.0}
        # 简化版：检查是否有 embedding，无 embedding 则直接给满分
        embeddings = [r.get("embedding") for r in results if r.get("embedding")]
        if len(embeddings) < 2:
            return {"score": 100.0, "note": "embeddings unavailable"}

        # 计算平均成对余弦相似度
        import numpy as np
        emb_matrix = np.array(embeddings)
        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        normalized = emb_matrix / norms
        sim_matrix = normalized @ normalized.T
        n = len(embeddings)
        upper_tri = sim_matrix[np.triu_indices(n, k=1)]
        avg_sim = np.mean(upper_tri) if len(upper_tri) > 0 else 0.0
        score = (1 - avg_sim) * 100
        return {"score": round(max(score, 0), 2), "avg_similarity": round(avg_sim, 4)}
