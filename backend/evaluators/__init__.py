"""Evaluators 模块。"""

from backend.evaluators.base import BaseEvaluator, EvalResult, EvalMethod
from backend.evaluators.registry import EvaluatorRegistry, evaluator_registry
from backend.evaluators.intent import IntentEvaluator
from backend.evaluators.retrieval import RetrievalEvaluator
from backend.evaluators.tool import ToolEvaluator
from backend.evaluators.generation import GenerationEvaluator
from backend.evaluators.outcome import OutcomeEvaluator

# 注册默认版本
_registry_map = {
    IntentEvaluator: "intent",
    RetrievalEvaluator: "retrieval",
    ToolEvaluator: "tool",
    GenerationEvaluator: "generation",
    OutcomeEvaluator: "outcome",
}
for cls, layer in _registry_map.items():
    evaluator_registry.register(layer, cls)

__all__ = [
    "BaseEvaluator", "EvalResult", "EvalMethod",
    "EvaluatorRegistry", "evaluator_registry",
    "IntentEvaluator", "RetrievalEvaluator", "ToolEvaluator",
    "GenerationEvaluator", "OutcomeEvaluator",
]
