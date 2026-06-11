"""评测器注册中心 + 工厂。"""

from typing import Dict, Type, Optional, List

from backend.evaluators.base import BaseEvaluator


class EvaluatorRegistry:
    """支持多版本评测器管理。

    用法:
        registry = EvaluatorRegistry()
        registry.register("intent", IntentEvaluator, version="1.0.0")
        evaluator = registry.create("intent", config={"weights": {...}})
    """

    def __init__(self):
        self._registry: Dict[str, Dict] = {}

    def register(self, layer: str, evaluator_cls: Type[BaseEvaluator], version: str = "1.0.0", set_default: bool = True):
        if layer not in self._registry:
            self._registry[layer] = {"default": None, "versions": {}}
        self._registry[layer]["versions"][version] = evaluator_cls
        if set_default:
            self._registry[layer]["default"] = evaluator_cls

    def set_default_version(self, layer: str, version: str):
        if layer not in self._registry:
            raise ValueError(f"No evaluator registered for layer '{layer}'")
        if version not in self._registry[layer]["versions"]:
            available = list(self._registry[layer]["versions"].keys())
            raise ValueError(f"Version '{version}' not found. Available: {available}")
        self._registry[layer]["default"] = self._registry[layer]["versions"][version]

    def create(self, layer: str, version: Optional[str] = None, config: Optional[Dict] = None) -> BaseEvaluator:
        if layer not in self._registry:
            raise ValueError(f"No evaluator registered for layer '{layer}'")
        layer_reg = self._registry[layer]
        cls = layer_reg["versions"][version] if version else layer_reg["default"]
        if cls is None:
            raise ValueError(f"No default evaluator set for layer '{layer}'")
        return cls(config=config)

    def list_layers(self) -> List[str]:
        return list(self._registry.keys())

    def list_versions(self, layer: str) -> List[str]:
        if layer in self._registry:
            return list(self._registry[layer]["versions"].keys())
        return []


# 全局单例
evaluator_registry = EvaluatorRegistry()
