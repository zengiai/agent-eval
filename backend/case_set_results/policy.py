"""评测集 Pass 策略定义。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PassFormula(str, Enum):
    """评测集通过公式。"""

    PASS_K = "pass_k"
    PASS_POWER_K = "pass_power_k"


class PassPolicy(BaseModel):
    """评测集 Pass 策略。

    线程安全说明：该对象为不可变值对象，不持有外部资源，可跨请求复用。
    """

    formula: PassFormula = PassFormula.PASS_K
    k: int = Field(default=1, ge=1, le=20)
    score_threshold: float = Field(default=80.0, ge=0.0, le=100.0)
    power_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    min_case_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    model_config = {"use_enum_values": True, "validate_default": True}

    @field_validator("formula", mode="before")
    @classmethod
    def _normalize_formula(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @classmethod
    def from_config(cls, raw: Any) -> "PassPolicy":
        """从 task config 或 API 请求中归一化策略。"""
        if isinstance(raw, PassPolicy):
            return raw
        if isinstance(raw, dict):
            return cls(**raw)
        return cls()

    @property
    def formula_value(self) -> str:
        if isinstance(self.formula, PassFormula):
            return self.formula.value
        return str(self.formula)

    def to_config(self) -> dict[str, Any]:
        return {
            "formula": self.formula_value,
            "k": self.k,
            "score_threshold": self.score_threshold,
            "power_threshold": self.power_threshold,
            "min_case_pass_rate": self.min_case_pass_rate,
        }
