"""评测编排器 —— 控制五层评测的执行顺序、并行调度、异常处理和加权汇总。"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any

from backend.evaluators.registry import evaluator_registry
from backend.evaluators.base import EvalResult

logger = logging.getLogger(__name__)


class EvaluationOrchestrator:
    """评测编排器。

    执行流程（按 enabled_layers 动态裁剪）：
      Phase 1: Intent + Retrieval 并行执行
      Phase 2: Tool（依赖 Intent 结果）
      Phase 3: Generation（依赖 Tool 结果）
      Phase 4: Outcome（依赖全部前序层结果）
      Phase 5: 计算加权总分（仅纳入启用的层）

    LLM Judge 集成：
      通过 config["llm"] 传入 LLM 配置 dict：
        {"model": "qwen3.7-max", "api_key": "sk-xxx", "base_url": "...", ...}
      引擎自动创建 LLMJudge 实例并通过 context 传递给各评测器。
    """

    LAYER_WEIGHTS = {
        "intent": 0.15,
        "retrieval": 0.15,
        "tool": 0.25,
        "generation": 0.30,
        "outcome": 0.15,
    }

    LAYER_DEPENDENCIES = {
        "intent": [],
        "retrieval": [],
        "tool": ["intent"],
        "generation": ["tool"],
        "outcome": ["intent", "retrieval", "tool", "generation"],
    }

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.layer_weights = self.config.get("layer_weights", self.LAYER_WEIGHTS)
        self.enabled_layers = set(self.config.get("enabled_layers", list(self.LAYER_WEIGHTS.keys())))
        self._validate_enabled_layers()

        # ── LLM Judge 初始化 ─────────────────────────────────────
        self._llm_judge = None
        llm_config = self.config.get("llm")
        if llm_config and llm_config.get("api_key"):
            try:
                from backend.runner.llm_judge import LLMJudge
                self._llm_judge = LLMJudge(
                    model=llm_config.get("model", "qwen3.7-max"),
                    api_key=llm_config["api_key"],
                    base_url=llm_config.get("base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
                    temperature=llm_config.get("temperature", 0.0),
                    max_retries=llm_config.get("max_retries", 3),
                )
                logger.info("LLM Judge 已初始化: model=%s", llm_config.get("model", "qwen3.7-max"))
            except Exception as e:
                logger.warning("LLM Judge 初始化失败，LLM 维度将跳过: %s", e)

    def _validate_enabled_layers(self):
        """确保依赖链完整：若启用下游层，自动补全所有上游依赖（迭代至收敛）。"""
        changed = True
        while changed:
            changed = False
            required = set()
            for layer in self.enabled_layers:
                for dep in self.LAYER_DEPENDENCIES.get(layer, []):
                    required.add(dep)
            missing = required - self.enabled_layers
            if missing:
                self.enabled_layers |= missing
                changed = True

    def run(self, trace: Dict, expected_snapshot: Dict) -> Dict[str, Any]:
        """执行评测，仅运行 enabled_layers 中指定的层。

        Returns:
            {"intent": EvalResult, ..., "__overall__": overall_score,
             "__meta__": {"enabled_layers": [...], "skipped_layers": [...]}}
        """
        spans = trace.get("spans", [])
        span_map = self._group_spans_by_type(spans)
        results: Dict[str, EvalResult] = {}
        context = {
            "trace": trace,
            "all_spans": spans,
            "query": trace.get("query", ""),
            "llm_judge": self._llm_judge,
        }

        enabled = self.enabled_layers

        # Phase 1: Intent + Retrieval 并行
        phase1_tasks = []
        if "intent" in enabled:
            phase1_tasks.append(("intent", span_map.get("intent")))
        if "retrieval" in enabled:
            phase1_tasks.append(("retrieval", span_map.get("retrieval")))

        if phase1_tasks:
            phase1 = self._run_parallel(phase1_tasks, expected_snapshot, context)
            results.update(phase1)
            context.update({f"{k}_result": v for k, v in phase1.items()})

        # Phase 2: Tool（依赖 Intent）
        if "tool" in enabled:
            results["tool"] = self._run_single("tool", span_map.get("tool_call"), expected_snapshot, context)
            context["tool_result"] = results["tool"]

        # Phase 3: Generation（依赖 Tool）
        if "generation" in enabled:
            results["generation"] = self._run_single("generation", span_map.get("generation"), expected_snapshot, context)
            context["generation_result"] = results["generation"]

        # Phase 4: Outcome（依赖全部前序层，不绑定 span）
        if "outcome" in enabled:
            results["outcome"] = self._run_single("outcome", {}, expected_snapshot, context)

        # Phase 5: 加权总分
        overall = self._calc_overall_score(results)
        skipped = [l for l in self.LAYER_WEIGHTS if l not in enabled]

        return {
            **{k: v for k, v in results.items()},
            "__overall__": overall,
            "__meta__": {"enabled_layers": sorted(enabled), "skipped_layers": skipped},
        }

    def _run_single(self, layer: str, span: Optional[Dict], expected: Dict, context: Dict) -> EvalResult:
        """安全执行单个评测器。"""
        try:
            evaluator = evaluator_registry.create(layer)
            return evaluator.evaluate(span or {}, expected, **context)
        except Exception as e:
            return EvalResult(layer=layer, total_score=0.0, metrics={}, error=str(e))

    def _run_parallel(self, tasks: List, expected: Dict, context: Dict) -> Dict[str, EvalResult]:
        """并行执行多个评测器。"""
        results = {}
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = {
                executor.submit(self._run_single, layer, span, expected, context): layer
                for layer, span in tasks if span is not None
            }
            for future in as_completed(futures):
                layer = futures[future]
                try:
                    results[layer] = future.result(timeout=60)
                except Exception as e:
                    results[layer] = EvalResult(layer=layer, total_score=0.0, error=str(e))
        return results

    def _group_spans_by_type(self, spans: List[Dict]) -> Dict[str, Any]:
        """按 span_type 分组。tool_call 可能有多个，取列表。"""
        grouped: Dict[str, Any] = {}
        for s in spans:
            stype = s.get("span_type", "")
            if stype == "tool_call":
                if "tool_call" not in grouped:
                    grouped["tool_call"] = []
                grouped["tool_call"].append(s)
            else:
                grouped[stype] = s
        return grouped

    def _calc_overall_score(self, results: Dict[str, EvalResult]) -> float:
        """加权总分：仅纳入成功评测的层，按剩余权重归一化。"""
        total = 0.0
        weight_sum = 0.0
        for layer, weight in self.layer_weights.items():
            if layer in results and results[layer].error is None:
                total += results[layer].total_score * weight
                weight_sum += weight
        return round(total / weight_sum, 2) if weight_sum > 0 else 0.0
