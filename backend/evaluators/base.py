"""评测器抽象基类、EvalResult 数据结构、自适应评测机制。"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any


class EvalMethod(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM_JUDGE = "llm_judge"
    HYBRID = "hybrid"
    MANUAL = "manual"


@dataclass
class EvalResult:
    """单层评测结果。"""

    layer: str
    total_score: float = 0.0
    metrics: Dict[str, Any] = field(default_factory=dict)
    judge_trace: Optional[Dict] = None
    method: EvalMethod = EvalMethod.DETERMINISTIC
    latency_ms: float = 0.0
    evaluator_version: str = "1.0.0"
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "layer": self.layer,
            "total_score": self.total_score,
            "metrics": self.metrics,
            "judge_trace": self.judge_trace,
            "method": self.method.value,
            "latency_ms": self.latency_ms,
            "evaluator_version": self.evaluator_version,
            "error": self.error,
        }


class BaseEvaluator(ABC):
    """评测器抽象基类。

    子类必须实现:
      - layer_name    : 返回层名标识
      - _default_weights() : 返回各维度默认权重
      - _evaluate_dimensions() : 核心评测逻辑
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._weights = self._default_weights()
        if "weights" in self.config:
            self._weights.update(self.config["weights"])

    @property
    @abstractmethod
    def layer_name(self) -> str:
        ...

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def supported_methods(self) -> List[EvalMethod]:
        return [EvalMethod.DETERMINISTIC]

    @abstractmethod
    def _default_weights(self) -> Dict[str, float]:
        ...

    @abstractmethod
    def _evaluate_dimensions(self, span: Dict, expected: Dict, **context) -> Dict[str, Any]:
        ...

    # ========================
    # 公共接口
    # ========================

    def evaluate(self, span: Dict, expected: Dict, **context) -> EvalResult:
        """执行评测（含自适应感知 + 计时 + 异常保护）。"""
        start = time.perf_counter()
        try:
            result = self._adaptive_evaluate(span, expected, **context)
            result.latency_ms = round((time.perf_counter() - start) * 1000, 2)
            result.evaluator_version = self.version
            return result
        except Exception as e:
            return EvalResult(
                layer=self.layer_name,
                total_score=0.0,
                metrics={},
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                evaluator_version=self.version,
                error=str(e),
            )

    def get_weights(self) -> Dict[str, float]:
        return dict(self._weights)

    def set_weight(self, dim: str, value: float):
        self._weights[dim] = value

    # ========================
    # 自适应评测：感知 → 跳过 → 重归一化
    # ========================

    def _adaptive_evaluate(self, span: Dict, expected: Dict, **context) -> EvalResult:
        """根据 expected 的实际内容决定哪些维度参与计算。"""
        raw_dims = self._evaluate_dimensions(span, expected, **context)
        dims = {}
        skipped = []

        for dim_name, weight in self._weights.items():
            if dim_name in raw_dims:
                dim_val = raw_dims[dim_name]
                if isinstance(dim_val, dict) and dim_val.get("skipped"):
                    skipped.append(dim_name)
                else:
                    dims[dim_name] = dim_val

        # 权重归一化
        active_weights = self._renormalize_weights(skipped)
        total = sum(
            self._extract_score(dims[name]) * active_weights.get(name, 0)
            for name in dims
        )
        total = round(min(max(total, 0), 100), 2)

        return EvalResult(
            layer=self.layer_name,
            total_score=total,
            metrics={**dims, "_skipped": skipped, "_active_weights": active_weights},
            method=self._infer_method(),
        )

    def _renormalize_weights(self, skipped_dims: List[str]) -> Dict[str, float]:
        """被跳过的维度权重按比例分配给剩余维度。"""
        active = {k: v for k, v in self._weights.items() if k not in skipped_dims}
        total = sum(active.values())
        if total == 0:
            return active
        return {k: v / total for k, v in active.items()}

    def _extract_score(self, dim_val: Any) -> float:
        if isinstance(dim_val, dict):
            return dim_val.get("score", 0.0)
        return float(dim_val)

    def _infer_method(self) -> EvalMethod:
        methods = self.supported_methods
        has_llm = EvalMethod.LLM_JUDGE in methods
        has_det = EvalMethod.DETERMINISTIC in methods
        if has_llm and has_det:
            return EvalMethod.HYBRID
        elif has_llm:
            return EvalMethod.LLM_JUDGE
        return EvalMethod.DETERMINISTIC
