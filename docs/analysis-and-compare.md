# 数据分析与版本对比

> 本文档定义评测结果的聚合、版本对比、回归检测与报告导出。Dashboard 可视化、高级统计等进阶功能见末尾 TODO。

---

## 1. 数据聚合

### 1.1 聚合层级

```
eval_scores（单层单次得分）
    │ 聚合
    ▼
traces.overall_score（单 run 的加权总分，各层加权计算后回填）
    │ 聚合
    ▼
eval_tasks.summary_metrics（一个 Case Set × 一个版本）
    │ 聚合
    ▼
Version 级别指标（跨 Task 汇总）
```

| 层级 | 聚合对象 | 输出 | 典型查询 |
|------|---------|------|---------|
| **Run 级** | 5 条 `eval_scores` → 加权总分 | 回填 `traces.overall_score` + `spans.score` | 单次执行的完整得分 |
| **Task 级** | 一个 Task 下所有 Run | `eval_tasks.summary_metrics` | 某版本在某个 Case Set 上的整体表现 |
| **Version 级** | 同一版本的全部 Task | 内存聚合，不单独存储 | 版本质量画像 |
| **对比级** | 两个 Version × 同一 Case Set | 返回 Diff 对象 | 版本 A vs 版本 B |

### 1.2 聚合计算

```python
def aggregate_task(task_id: UUID) -> Dict:
    """
    聚合一个 Task 下所有成功 Run 的得分。

    Returns:
        {
            "overall": {"mean": 85.2, "median": 87.0, "p25": 78.0, "p75": 92.0, "std": 8.3},
            "layers": {
                "intent":     {"mean": 90.1, "median": 92.0, ...},
                "retrieval":  {"mean": 82.3, ...},
                "tool":       {"mean": 85.7, ...},
                "generation": {"mean": 83.4, ...},
                "outcome":    {"mean": 84.9, ...},
            },
            "case_count": 50,
            "failed_count": 1,
        }
    """
    runs = fetch_eval_runs(task_id)
    successful = [r for r in runs if r.status == "completed"]

    scores = fetch_eval_scores_for_runs([r.id for r in successful])

    return {
        "overall": aggregate(scores, key="total_score"),
        "layers": {
            layer: aggregate(layer_scores, key="total_score")
            for layer, layer_scores in group_by_layer(scores).items()
        },
        "case_count": len(successful),
        "failed_count": len(runs) - len(successful),
    }
```

---

## 2. 版本对比

### 2.1 前置条件

版本对比必须满足以下全部条件，否则结果不可信（此为权威定义，其他文档引用此处）：

| 条件 | 校验方式 | 不满足时的行为 |
|------|---------|-------------|
| 同一 Case Set | `task_a.case_set_id == task_b.case_set_id` | 拒绝对比，提示选择相同 Case Set 的 Task |
| 同一评测器版本 | `evaluator_version` 一致 | 拒绝对比，提示先执行「重评」统一版本 |
| 同一启用层 | `enabled_layers` 一致（`__meta__` 中记录） | 拒绝对比，提示启用层不一致 |
| 同一权重配置 | 各层维度权重一致（`config.weights` 中记录） | 警告：权重不同会导致总分不可比 |

### 2.2 对比算法

```python
@dataclass
class DiffResult:
    layer: str
    baseline_mean: float          # 基线版本均值
    target_mean: float            # 目标版本均值
    delta: float                  # 得分变化（正值=改进）
    delta_pct: float              # 相对变化百分比
    p_value: float                # Paired t-test 显著性
    significant: bool             # p < 0.05
    case_diffs: List[CaseDiff]    # 逐 Case 变动明细


def compare_versions(task_a_id: UUID, task_b_id: UUID) -> Dict[str, DiffResult]:
    """
    对比两个版本的评测结果。

    步骤：
      1. 加载两个 Task 的 eval_runs，按 eval_case_id 对齐
      2. 对整体 + 5 层分别执行 Paired t-test
      3. 返回差异对象
    """
    runs_a = load_task_runs(task_a_id)
    runs_b = load_task_runs(task_b_id)

    # 对齐：只对比两个版本都成功跑完的 Case
    common_cases = set(runs_a.keys()) & set(runs_b.keys())

    results = {}
    for layer in ALL_LAYERS + ["overall"]:
        scores_a = [runs_a[c].layer_score(layer) for c in common_cases]
        scores_b = [runs_b[c].layer_score(layer) for c in common_cases]

        t_stat, p_value = paired_ttest(scores_a, scores_b)

        results[layer] = DiffResult(
            layer=layer,
            baseline_mean=mean(scores_a),
            target_mean=mean(scores_b),
            delta=mean([b - a for a, b in zip(scores_a, scores_b)]),
            p_value=p_value,
            significant=p_value < 0.05,
            case_diffs=top_n_diffs(scores_a, scores_b, n=5),
        )

    return results
```

### 2.3 统计检验

选用 **Paired t-test**（配对 t 检验）而非独立 t 检验：

- 同一个 Case 在新旧版本上的得分是**配对样本**（同一输入、同一期望标注）
- Paired t-test 消除 Case 自身难度差异的干扰，检验力更高
- `p < 0.05` 视为统计显著

```
H₀: μ_diff = 0      （两个版本无差异）
H₁: μ_diff ≠ 0      （两个版本有差异）

p < 0.05 → 拒绝 H₀ → 存在统计显著差异
p ≥ 0.05 → 无法拒绝 H₀ → 差异可能由随机波动导致
```

### 2.4 逐 Case Diff

```python
@dataclass
class CaseDiff:
    case_id: UUID
    query: str
    baseline_score: float
    target_score: float
    delta: float
    direction: str           # "improved" | "degraded" | "unchanged"


def top_n_diffs(scores_a, scores_b, n=5):
    """返回退化最严重的前 N 个 Case"""
    diffs = [
        CaseDiff(
            case_id=...,
            query=...,
            baseline_score=a,
            target_score=b,
            delta=b - a,
            direction="degraded" if b < a else "improved" if b > a else "unchanged",
        )
        for a, b in zip(scores_a, scores_b)
    ]
    return sorted(diffs, key=lambda d: d.delta)[:n]
```

---

## 3. 回归检测与告警

### 3.1 自动触发时机

当新版本评测完成后，自动与**最近一次已完成且满足对比前置条件的 Task** 做对比：

```python
def auto_check_regression(current_task: EvalTask):
    """新版本评测完成后自动执行回归检测"""
    baseline = find_latest_completed_task(
        agent_version__ne=current_task.agent_version,
        case_set_id=current_task.case_set_id,
        evaluator_version=current_task.evaluator_version,
    )
    if not baseline:
        return  # 无基线，跳过

    diff = compare_versions(baseline.id, current_task.id)
    alerts = detect_regression(diff)
    if alerts:
        send_alerts(alerts)
```

### 3.2 退化判定阈值

| 指标 | WARNING 阈值 | CRITICAL 阈值 |
|------|-------------|--------------|
| 总分变动 | < -3 分 且 p<0.05 | < -8 分 且 p<0.05 |
| 单层变动 | < -5 分 且 p<0.05 | < -10 分 且 p<0.05 |
| 退化 Case 占比 | > 20% | > 35% |

### 3.3 告警内容

```json
{
  "severity": "WARNING",
  "agent_version": "v2.3.1",
  "baseline_version": "v2.3.0",
  "case_set": "production_critical_cases",
  "regressions": [
    {
      "layer": "generation",
      "baseline_mean": 85.3,
      "target_mean": 78.1,
      "delta": -7.2,
      "p_value": 0.003,
      "top_degraded_cases": [
        {"case_id": "...", "query": "...", "delta": -22.0},
        {"case_id": "...", "query": "...", "delta": -18.5}
      ]
    }
  ]
}
```

---

## 4. 报告导出

### 4.1 导出格式

| 格式 | 用途 | 包含内容 |
|------|------|---------|
| **JSON** | API 集成、自动化流水线 | 完整 DiffResult + 逐 Case 明细 |
| **CSV** | Excel 分析、离线处理 | 逐 Case × 逐层得分矩阵 |
| **PDF** | 版本发布 Review | 摘要 + 图表 + 退化 Case 清单 |

### 4.2 CSV 导出结构

```csv
case_id,query,difficulty,category,
intent_baseline,intent_target,intent_delta,
retrieval_baseline,retrieval_target,retrieval_delta,
tool_baseline,tool_target,tool_delta,
generation_baseline,generation_target,generation_delta,
outcome_baseline,outcome_target,outcome_delta,
overall_baseline,overall_target,overall_delta
```

---

## 5. 后续 TODO

以下功能标记为进阶/高级，留待后续迭代：

### 5.1 Dashboard 可视化

- 总览面板：当前版本总分、各层雷达图
- 分布面板：分数直方图 + 箱线图（p50 / p75 / p90 / p95）
- 切片分析：按 difficulty / category / tags 下钻
- 趋势线：连续 N 个版本的时间序列
- 退化热力图：Case × Version 矩阵

### 5.2 高级统计

- Cohen's d 效应量（量化差异幅度，>0.2 小、>0.5 中、>0.8 大）
- Bootstrap 置信区间（避免小样本偏差）
- 多重检验校正（Bonferroni，五层同时检验时控制 Family-Wise Error Rate）

### 5.3 分析增强

- **标注来源偏差分析**：`llm_auto` vs `human` 标注的 Case 得分是否存在系统性偏差
- **LLM Judge 一致性**：同一 Case 多次 LLM 评分的标准差（一致性检验）
- **Case 失效检测**：新旧版本均低分（< 20）的 Case → 标记 `suspected_stale`
- **成本趋势**：Token 消耗、延迟趋势与得分的相关性

---

> **关联文档**：[架构总览](architecture.md) · [数据模型](data-model.md) · [评测设计](evaluation-design.md) · [上报协议](trace-protocol.md)
