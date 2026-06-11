"""评测器单元测试 —— 覆盖五层评测器的确定性计算逻辑和自适应跳过。"""

import pytest
from backend.evaluators.intent import IntentEvaluator
from backend.evaluators.retrieval import RetrievalEvaluator
from backend.evaluators.tool import ToolEvaluator
from backend.evaluators.generation import GenerationEvaluator
from backend.evaluators.outcome import OutcomeEvaluator


# ============================================================
# IntentEvaluator
# ============================================================

class TestIntentEvaluator:
    def test_accuracy_mode_all_full_match(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query", "location_lookup"]}}
        expected = {"expected_intent": {"intents": ["weather_query", "location_lookup"], "mode": "all"}}
        result = e.evaluate(span, expected)
        assert result.total_score == 100.0
        assert result.metrics["IntentAccuracy"]["score"] == 100.0

    def test_accuracy_mode_all_partial_match(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"]}}
        expected = {"expected_intent": {"intents": ["weather_query", "location_lookup"], "mode": "all"}}
        result = e.evaluate(span, expected)
        assert result.total_score < 100.0
        assert result.metrics["IntentAccuracy"]["score"] == 50.0

    def test_accuracy_mode_any(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"]}}
        expected = {"expected_intent": {"intents": ["weather_query", "location_lookup"], "mode": "any"}}
        result = e.evaluate(span, expected)
        assert result.metrics["IntentAccuracy"]["score"] == 100.0

    def test_accuracy_mode_any_no_match(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["translation"]}}
        expected = {"expected_intent": {"intents": ["weather_query", "location_lookup"], "mode": "any"}}
        result = e.evaluate(span, expected)
        assert result.metrics["IntentAccuracy"]["score"] == 0.0

    def test_accuracy_mode_at_least_n(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query", "recommendation"]}}
        expected = {"expected_intent": {"intents": ["weather_query", "location_lookup", "recommendation"], "mode": "at_least_n", "mode_n": 2}}
        result = e.evaluate(span, expected)
        assert result.metrics["IntentAccuracy"]["score"] == 100.0  # 2/2=1.0

    def test_confidence_skipped(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"]}}
        expected = {"expected_intent": {"intents": ["weather_query"]}}
        result = e.evaluate(span, expected)
        assert "ConfidenceCalibration" in result.metrics["_skipped"]

    def test_confidence_valid(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"], "confidence": 0.95}}
        expected = {"expected_intent": {"intents": ["weather_query"]}}
        result = e.evaluate(span, expected)
        assert "ConfidenceCalibration" not in result.metrics["_skipped"]
        assert result.metrics["ConfidenceCalibration"]["score"] == 100.0

    def test_confidence_invalid(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"], "confidence": 1.5}}
        expected = {"expected_intent": {"intents": ["weather_query"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["ConfidenceCalibration"]["score"] == 0.0

    def test_ner_skipped_no_expected_entities(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"], "entities": []}}
        expected = {"expected_intent": {"intents": ["weather_query"]}}
        result = e.evaluate(span, expected)
        assert "NERAccuracy" in result.metrics["_skipped"]

    def test_ner_full_match(self):
        e = IntentEvaluator()
        span = {"output": {"intents": ["weather_query"], "entities": [{"type": "city", "value": "北京"}]}}
        expected = {"expected_intent": {"intents": ["weather_query"], "entities": [{"type": "city", "value": "北京"}]}}
        result = e.evaluate(span, expected)
        assert result.metrics["NERAccuracy"]["score"] == 100.0


# ============================================================
# RetrievalEvaluator
# ============================================================

class TestRetrievalEvaluator:
    def test_precision_full_match(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_1"}, {"id": "doc_2"}, {"id": "doc_3"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1", "doc_2"]}}
        result = e.evaluate(span, expected)
        assert pytest.approx(result.metrics["PrecisionAtK"]["score"], 0.1) == 66.67

    def test_recall_full_match(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_1"}, {"id": "doc_2"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1", "doc_2"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["RecallAtK"]["score"] == 100.0

    def test_recall_partial(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_1"}, {"id": "doc_3"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1", "doc_2"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["RecallAtK"]["score"] == 50.0

    def test_mrr_first_rank(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_1"}, {"id": "doc_2"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["MRR"]["score"] == 100.0

    def test_mrr_second_rank(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_3"}, {"id": "doc_1"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["MRR"]["score"] == 50.0

    def test_mrr_no_match(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_3"}, {"id": "doc_4"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["MRR"]["score"] == 0.0

    def test_ndcg_perfect(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_1"}, {"id": "doc_2"}, {"id": "doc_3"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": ["doc_1", "doc_2"]}}
        result = e.evaluate(span, expected)
        assert result.metrics["NDCG"]["score"] == 100.0

    def test_diversity_no_embeddings(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [
            {"id": "doc_1"}, {"id": "doc_2"},
        ]}}
        expected = {"expected_retrieval": {"relevant_ids": []}}
        result = e.evaluate(span, expected)
        assert "Diversity" not in result.metrics["_skipped"]
        assert result.metrics["Diversity"]["note"] == "embeddings unavailable"

    def test_all_skipped_when_no_relevant(self):
        e = RetrievalEvaluator()
        span = {"output": {"results": [{"id": "doc_1"}]}}
        expected = {"expected_retrieval": {}}
        result = e.evaluate(span, expected)
        assert "PrecisionAtK" in result.metrics["_skipped"]


# ============================================================
# ToolEvaluator
# ============================================================

class TestToolEvaluator:
    def test_selection_full_match(self):
        e = ToolEvaluator()
        span = {"tool_name": "get_weather", "tool_params": {"city": "北京"}, "tool_status": "success"}
        expected = {"expected_tools": [{"tool_name": "get_weather", "params": {"city": "北京"}}]}
        result = e.evaluate(span, expected)
        assert result.metrics["ToolSelection"]["score"] == 100.0

    def test_selection_alt_match(self):
        e = ToolEvaluator()
        span = {"tool_name": "weather_api_v2", "tool_params": {}, "tool_status": "success"}
        expected = {"expected_tools": [{"tool_name": "weather_api", "params": {}}]}
        result = e.evaluate(span, expected)
        assert result.metrics["ToolSelection"]["score"] == 70.0  # 替代匹配得 0.7

    def test_param_accuracy_full_match(self):
        e = ToolEvaluator()
        span = {"tool_name": "get_weather", "tool_params": {"city": "北京", "date": "2025-01-01"}, "tool_status": "success"}
        expected = {"expected_tools": [{"tool_name": "get_weather", "params": {"city": "北京", "date": "2025-01-01"}}]}
        result = e.evaluate(span, expected)
        assert result.metrics["ParamAccuracy"]["score"] == 100.0

    def test_param_accuracy_partial(self):
        e = ToolEvaluator()
        span = {"tool_name": "get_weather", "tool_params": {"city": "上海"}, "tool_status": "success"}
        expected = {"expected_tools": [{"tool_name": "get_weather", "params": {"city": "北京"}}]}
        result = e.evaluate(span, expected)
        assert result.metrics["ParamAccuracy"]["score"] == 0.0

    def test_efficiency_exact_match(self):
        e = ToolEvaluator()
        span = {"tool_name": "get_weather", "tool_params": {}, "tool_status": "success"}
        expected = {"expected_tools": [{"tool_name": "get_weather", "params": {}}]}
        result = e.evaluate(span, expected)
        assert result.metrics["Efficiency"]["score"] == 100.0

    def test_success_rate_all_success(self):
        e = ToolEvaluator()
        spans = [
            {"tool_name": "get_weather", "tool_status": "success"},
            {"tool_name": "get_aqi", "tool_status": "success"},
        ]
        expected = {"expected_tools": []}
        result = e.evaluate(spans, expected)
        assert result.metrics["SuccessRate"]["score"] == 100.0

    def test_success_rate_partial_fail(self):
        e = ToolEvaluator()
        spans = [
            {"tool_name": "get_weather", "tool_status": "success"},
            {"tool_name": "get_aqi", "tool_status": "error"},
        ]
        expected = {"expected_tools": []}
        result = e.evaluate(spans, expected)
        assert result.metrics["SuccessRate"]["score"] == 50.0

    def test_multi_spans_passed_as_list(self):
        e = ToolEvaluator()
        spans = [
            {"tool_name": "search", "tool_params": {"q": "test"}, "tool_status": "success"},
            {"tool_name": "fetch", "tool_params": {"id": "1"}, "tool_status": "success"},
        ]
        expected = {"expected_tools": [
            {"tool_name": "search", "params": {"q": "test"}},
            {"tool_name": "fetch", "params": {"id": "1"}},
        ]}
        result = e.evaluate(spans, expected)
        assert result.metrics["ToolSelection"]["score"] == 100.0
        assert result.metrics["Efficiency"]["score"] == 100.0


# ============================================================
# GenerationEvaluator
# ============================================================

class TestGenerationEvaluator:
    def test_language_quality_good(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "北京今天天气晴朗，气温适中，适合户外活动。"}}
        expected = {}
        result = e.evaluate(span, expected)
        assert "LanguageQuality" not in result.metrics["_skipped"]
        assert result.metrics["LanguageQuality"]["score"] > 0

    def test_language_quality_empty(self):
        e = GenerationEvaluator()
        span = {"output": {"response": ""}}
        expected = {}
        result = e.evaluate(span, expected)
        assert result.metrics["LanguageQuality"]["score"] == 0.0

    def test_format_compliance_json(self):
        e = GenerationEvaluator()
        span = {"output": {"response": '{"key": "value"}'}}
        expected = {"expected_answer": {"format": "json"}}
        result = e.evaluate(span, expected)
        assert result.metrics["FormatCompliance"]["score"] == 100.0

    def test_format_compliance_json_fail(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "not a json"}}
        expected = {"expected_answer": {"format": "json"}}
        result = e.evaluate(span, expected)
        assert result.metrics["FormatCompliance"]["score"] == 0.0

    def test_format_compliance_json_in_markdown(self):
        e = GenerationEvaluator()
        span = {"output": {"response": '```json\n{"key": "value"}\n```'}}
        expected = {"expected_answer": {"format": "json"}}
        result = e.evaluate(span, expected)
        assert result.metrics["FormatCompliance"]["score"] == 80.0

    def test_semantic_sim_word_overlap(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "Beijing is sunny today with mild temperature."}}
        expected = {"gold_answer": "Beijing is sunny and warm today."}
        result = e.evaluate(span, expected)
        assert "SemanticSimilarity" not in result.metrics["_skipped"]
        assert result.metrics["SemanticSimilarity"]["score"] > 0

    def test_semantic_sim_no_gold(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "hello"}}
        expected = {}
        result = e.evaluate(span, expected)
        assert "SemanticSimilarity" in result.metrics["_skipped"]

    def test_factual_accuracy_skipped_mvp(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "anything"}}
        expected = {}
        result = e.evaluate(span, expected)
        assert "FactualAccuracy" in result.metrics["_skipped"]

    def test_completeness_with_checkpoints(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "今天天气晴朗，温度适中。"}}
        expected = {"expected_answer": {
            "check_points": [
                {"point": "天气晴朗", "match": "must_contain"},
                {"point": "温度适中", "match": "must_contain"},
            ],
        }}
        result = e.evaluate(span, expected)
        assert result.metrics["Completeness"]["score"] == 100.0

    def test_completeness_partial(self):
        e = GenerationEvaluator()
        span = {"output": {"response": "今天天气晴朗。"}}
        expected = {"expected_answer": {
            "check_points": [
                {"point": "天气晴朗", "weight": 1.0, "match": "must_contain"},
                {"point": "温度适中", "weight": 1.0, "match": "must_contain"},
            ],
        }}
        result = e.evaluate(span, expected)
        assert result.metrics["Completeness"]["score"] == 50.0


# ============================================================
# OutcomeEvaluator
# ============================================================

class TestOutcomeEvaluator:
    def test_task_completion_no_layer_results(self):
        e = OutcomeEvaluator()
        span = {}
        expected = {}
        result = e.evaluate(span, expected, trace={"status": "success"})
        assert result.metrics["TaskCompletion"]["score"] == 50.0
        assert "no layer results" in result.metrics["TaskCompletion"]["note"]

    def test_latency_score_fast(self):
        e = OutcomeEvaluator()
        span = {}
        expected = {}
        result = e.evaluate(span, expected, trace={"total_latency_ms": 500}, all_spans=[])
        assert result.metrics["LatencyScore"]["score"] == 100

    def test_latency_score_medium(self):
        e = OutcomeEvaluator()
        span = {}
        expected = {}
        result = e.evaluate(span, expected, trace={"total_latency_ms": 4000}, all_spans=[])
        assert result.metrics["LatencyScore"]["score"] == 75

    def test_latency_score_slow(self):
        e = OutcomeEvaluator()
        span = {}
        expected = {}
        result = e.evaluate(span, expected, trace={"total_latency_ms": 15000}, all_spans=[])
        assert result.metrics["LatencyScore"]["score"] == 25

    def test_error_recovery_no_tool_spans(self):
        e = OutcomeEvaluator()
        span = {}
        expected = {}
        result = e.evaluate(span, expected, trace={}, all_spans=[])
        assert result.metrics["ErrorRecovery"]["score"] == 100.0

    def test_error_recovery_with_recovery(self):
        e = OutcomeEvaluator()
        spans = [
            {"span_type": "tool_call", "tool_name": "api", "tool_status": "error"},
            {"span_type": "tool_call", "tool_name": "api", "tool_status": "success"},
        ]
        expected = {}
        result = e.evaluate({}, expected, trace={}, all_spans=spans)
        assert result.metrics["ErrorRecovery"]["score"] == 100.0
        assert result.metrics["ErrorRecovery"]["recovered"] == 1

    def test_error_recovery_no_recovery(self):
        e = OutcomeEvaluator()
        spans = [
            {"span_type": "tool_call", "tool_name": "api", "tool_status": "error"},
            {"span_type": "tool_call", "tool_name": "other", "tool_status": "success"},
        ]
        expected = {}
        result = e.evaluate({}, expected, trace={}, all_spans=spans)
        assert result.metrics["ErrorRecovery"]["score"] == 50.0
        assert result.metrics["ErrorRecovery"]["recovered"] == 0
