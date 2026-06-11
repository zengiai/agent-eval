"""编排器单元测试 —— 验证五层评测的调度顺序、并行执行、权重计算和 enabled_layers 裁剪。"""

import pytest
from backend.runner.engine import EvaluationOrchestrator


# ============================================================
# 编排器初始化
# ============================================================

class TestOrchestratorInit:
    def test_default_enabled_layers(self):
        orch = EvaluationOrchestrator()
        assert orch.enabled_layers == {"intent", "retrieval", "tool", "generation", "outcome"}

    def test_custom_enabled_layers_triggers_auto_expand(self):
        """启用 generation 时自动补全依赖链：tool → intent。"""
        orch = EvaluationOrchestrator(config={"enabled_layers": ["generation"]})
        assert "intent" in orch.enabled_layers
        assert "tool" in orch.enabled_layers
        assert "generation" in orch.enabled_layers

    def test_enabled_outcome_expands_all(self):
        """启用 outcome 时自动补全所有上游层。"""
        orch = EvaluationOrchestrator(config={"enabled_layers": ["outcome"]})
        assert orch.enabled_layers == {"intent", "retrieval", "tool", "generation", "outcome"}

    def test_custom_weights(self):
        custom = {"intent": 0.5, "retrieval": 0.5}
        orch = EvaluationOrchestrator(config={"layer_weights": custom, "enabled_layers": ["intent", "retrieval"]})
        assert orch.layer_weights == custom

    def test_partial_layers_only_run_enabled(self):
        """仅启用 intent + retrieval 时跳过 tool/generation/outcome。"""
        orch = EvaluationOrchestrator(config={"enabled_layers": ["intent", "retrieval"]})
        trace = {
            "id": "t1",
            "query": "test",
            "spans": [
                {"span_type": "intent", "output": {"intents": ["test"], "confidence": 0.9}},
                {"span_type": "retrieval", "output": {"results": []}},
            ],
        }
        expected = {
            "expected_intent": {"intents": ["test"]},
            "expected_retrieval": {"relevant_ids": []},
        }
        results = orch.run(trace, expected)
        assert "intent" in results
        assert "retrieval" in results
        assert "tool" not in results
        assert "generation" not in results
        assert "outcome" not in results
        assert results["__meta__"]["skipped_layers"] == ["tool", "generation", "outcome"]


# ============================================================
# 完整评测管线
# ============================================================

class TestFullPipeline:
    def test_full_five_layer_evaluation(self, sample_trace, expected_snapshot):
        """完整五层评测：所有层都应产生有效得分。"""
        orch = EvaluationOrchestrator()
        results = orch.run(sample_trace, expected_snapshot)

        assert "__overall__" in results
        assert 0 <= results["__overall__"] <= 100

        # 五层结果都存在
        for layer in ["intent", "retrieval", "tool", "generation", "outcome"]:
            assert layer in results, f"缺少 {layer} 层结果"
            er = results[layer]
            assert er.error is None, f"{layer} 出错: {er.error}"
            assert 0 <= er.total_score <= 100, f"{layer} 得分异常: {er.total_score}"

    def test_overall_score_range(self, sample_trace, expected_snapshot):
        orch = EvaluationOrchestrator()
        results = orch.run(sample_trace, expected_snapshot)
        overall = results["__overall__"]
        assert isinstance(overall, float)
        assert 0 <= overall <= 100

    def test_intent_only_pipeline(self, sample_trace, expected_snapshot):
        """仅评测意图层：其他层不出现。"""
        orch = EvaluationOrchestrator(config={"enabled_layers": ["intent"]})
        results = orch.run(sample_trace, expected_snapshot)

        assert "intent" in results
        assert "retrieval" not in results
        assert "tool" not in results
        assert "generation" not in results
        assert "outcome" not in results
        assert results["__overall__"] == results["intent"].total_score  # 只有一层，权重归一化为 1.0

    def test_span_grouping(self):
        """验证 span 按类型分组：tool_call 返回列表。"""
        orch = EvaluationOrchestrator()
        spans = [
            {"span_type": "intent"},
            {"span_type": "retrieval"},
            {"span_type": "tool_call", "tool_name": "a"},
            {"span_type": "tool_call", "tool_name": "b"},
            {"span_type": "generation"},
        ]
        grouped = orch._group_spans_by_type(spans)
        assert grouped["intent"] == {"span_type": "intent"}
        assert isinstance(grouped["tool_call"], list)
        assert len(grouped["tool_call"]) == 2

    def test_evaluator_error_handling(self):
        """评测器异常时不应中断整体流程。"""
        orch = EvaluationOrchestrator()
        trace = {
            "id": "t_err",
            "query": "test",
            "spans": [],  # 空 spans
        }
        expected = {
            "expected_intent": {"intents": ["test"]},
        }
        results = orch.run(trace, expected)
        # 空 spans 时编排器不崩溃，返回 overall 和 meta
        assert "__overall__" in results
        assert results["__overall__"] == 0.0
