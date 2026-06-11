# 测试用例与测试集设计

> 本文档定义评测用例的标注规范、测试集管理与质量保障。DDL 定义统一见 [data-model.md §2](data-model.md)，用例自动生成等进阶功能见末尾 TODO。

---

## 1. 用例 Schema

用例的完整字段定义见 [data-model.md §2.3](data-model.md) `eval_cases` 表，本文档聚焦**标注规范**与**测试集设计**，仅列出与标注直接相关的核心字段：

| 字段 | 说明 | DDL 映射 | 示例 |
|------|------|---------|------|
| `query` | 用户问题原文 | `eval_cases.query` | "帮我查一下上个月的电费" |
| `expected_intent` | 期望意图（含 `mode`） | `eval_cases.expected_intent` (JSONB) | `{"intents": ["bill_query"], "mode": "all"}` |
| `expected_retrieval` | 期望召回的相关文档 ID | `eval_cases.expected_retrieval` (JSONB) | `{"relevant_ids": ["doc_001"]}` |
| `expected_tools` | 期望工具调用序列（含 `match_mode`） | `eval_cases.expected_tools` (JSONB) | `[{"tool_name": "get_bill", "match_mode": "exact"}]` |
| `expected_answer` | 期望回答检查点（含 `check_points`、`match` 模式） | `eval_cases.expected_answer` (JSONB) | `{"check_points": [{"key":"金额","match":"must_contain"}]}` |
| `gold_answer` | 参考标准答案 | `eval_cases.gold_answer` (TEXT) | "您上月电费为..." |
| `difficulty` | 难度等级 | `eval_cases.difficulty` | `easy` / `medium` / `hard` |
| `category` | 业务分类 | `eval_cases.category` | `查费类` / `办理类` / `咨询类` |
| `tags` | 自定义标签 | `eval_cases.tags` | `["多轮", "跨应用"]` |
| `source` | 来源（定义见 data-model.md） | `eval_cases.source` | `manual` / `trace` / `llm_auto` / `llm_reviewed` / `hybrid` |

> **字段映射说明**：本文档使用扁平化的标注术语（如 `expected_checkpoints`、`expect_mode`、`tool_match_mode`）来描述标注逻辑，这些概念在 DDL 中均嵌套在对应 JSONB 字段内：
> - `expected_checkpoints` / `checkpoint_mode` → `eval_cases.expected_answer.check_points` / `eval_cases.expected_answer.check_points[].match`
> - `expect_mode` → `eval_cases.expected_intent.mode`
> - `tool_match_mode` → `eval_cases.expected_tools[].match_mode`
> - `application` → `eval_cases.tags` 或 `eval_cases.category`

---

## 2. 标注规范

### 2.1 难度分级标准

| 难度 | 定义 | 特征 | 典型 Case 数占比 |
|------|------|------|:---:|
| **easy** | 单意图、单工具、确定性回答 | 查询单笔账单、查余额 | 40% |
| **medium** | 多意图 OR 多工具 OR 条件判断 | 跨月对比费用、条件办理 | 40% |
| **hard** | 多意图 + 多工具 + 推理链 | 跨应用协同、异常兜底 | 20% |

### 2.2 场景分类

```
📂 业务分类（category）
├── 查费类    —— 账单查询、余额查询、明细查询
├── 办理类    —— 套餐变更、业务开通、业务退订
├── 咨询类    —— 政策问询、使用帮助、常见问题
├── 投诉类    —— 故障申报、投诉处理
└── 异常类    —— 无效输入、权限不足、系统边界
```

### 2.3 标注流程

```
创建 Case（填写 query + 基本信息）
    │
    ▼
标注期望值（填写 expected_intent / expected_tools / expected_checkpoints）
    │
    ▼
设置匹配模式（expect_mode / tool_match_mode / checkpoint_mode）
    │
    ▼
分配难度 + 分类 + 标签
    │
    ▼
审核（source != manual 时必须人工审核）
```

### 2.4 标注示例

**示例 1：简单查费**

```json
{
  "query": "帮我查一下上个月的用电量",
  "application": "bill_query",
  "expected_intent": ["bill_query"],
  "expect_mode": "any",
  "expected_tools": ["get_bill"],
  "tool_match_mode": "exact",
  "expected_checkpoints": [
    {"key": "用电量", "mode": "must_contain"},
    {"key": "上月",   "mode": "must_contain"}
  ],
  "checkpoint_mode": "must_contain",
  "difficulty": "easy",
  "category": "查费类",
  "tags": ["单轮"]
}
```

**示例 2：对比型查费（发散型）**

```json
{
  "query": "最近三个月哪个月用电最多，帮我分析下原因",
  "application": "bill_query",
  "expected_intent": ["bill_query", "bill_analysis"],
  "expect_mode": "all",
  "expected_tools": ["get_bill", "analyze_usage"],
  "tool_match_mode": "exact_or_alternative",
  "expected_checkpoints": [
    {"key": "月用电量", "mode": "must_contain"},
    {"key": "原因分析", "mode": "prefer_contain"},
    {"key": "对比结论", "mode": "nice_to_have"}
  ],
  "checkpoint_mode": "prefer_contain",
  "difficulty": "medium",
  "category": "查费类",
  "tags": ["多轮", "分析型"]
}
```

**示例 3：跨应用协同**

```json
{
  "query": "我要搬家了，帮我办理地址变更，然后把新地址的账户绑定",
  "application": "account_service",
  "expected_intent": ["address_change", "account_binding"],
  "expect_mode": "all",
  "expected_tools": ["change_address", "rebind_account"],
  "tool_match_mode": "any_in_category",
  "expected_checkpoints": [
    {"key": "地址变更成功", "mode": "must_contain"},
    {"key": "账户已绑定",   "mode": "must_contain"}
  ],
  "checkpoint_mode": "must_contain",
  "difficulty": "hard",
  "category": "办理类",
  "tags": ["跨应用", "多步骤"]
}
```

### 2.5 标注规则速查

| 规则 | 说明 |
|------|------|
| **独立可测** | 每个 Case 自包含，不依赖其他 Case 的执行结果 |
| **期望明确** | 即使 `fuzzy` 模式，也要给出模糊范围或方向 |
| **标注可复现** | 不同标注者面对同一 Case 应给出相同或等效的期望值 |
| **避免冗余** | Case Set 内 Query 重复率 < 5% |
| **覆盖边界** | 每个分类下至少有 easy/medium/hard 三层覆盖，且包含正常 + 异常 + 边界 |

---

## 3. 测试集管理

### 3.1 测试集模型

测试集（Case Set）是一组用例的逻辑集合，服务于特定评测目标。表结构定义见 [data-model.md §2.1](data-model.md)（`case_sets`）和 [data-model.md §2.2](data-model.md)（`case_set_members`），此处不再重复 DDL。

> 一个 Case 可同时属于多个测试集（M:N），例如一个用例既在 `smoke` 又在 `full` 测试集中。

### 3.2 推荐测试集

| 测试集名称 | 用途 | Case 数量 | 来源 | 更新频率 |
|---------|------|:-------:|------|---------|
| `smoke` | 冒烟测试（CI/CD 每次触发） | 10-20 | 全手动标注 | 版本发布时 Review |
| `regression` | 回归测试（每日定时） | 100-200 | 手动 + 历史退化 Case | 每周补充 |
| `production_sample` | 生产采样集（每周全量） | 50-100 | LLM 自动标注 + 人工审核 | 每周更新 |
| `full` | 全量回归（版本发布前） | 300+ | 上述测试集的并集 | 按需更新 |

### 3.3 测试集策略

```
PR 提交
  │
  ├──→ smoke（快速反馈，< 2 min）
  │      └── 必须全部通过
  │
每日定时
  │
  └──→ regression（完整覆盖）
         └── 总得分退化 > 3 分 → 告警

版本发布前
  │
  └──→ full（全量回归）
         └── 生成版本对比报告
```

### 3.4 测试集维护

```python
def refresh_case_set(case_set_id: UUID):
    """重新计算测试集的 case_count 并清理无效成员"""
    count = db.scalar(
        "SELECT COUNT(*) FROM case_set_members WHERE case_set_id = :sid AND case_id IN (SELECT id FROM eval_cases)",
        {"sid": case_set_id}
    )
    db.execute(
        "UPDATE case_sets SET case_count = :n, updated_at = now() WHERE id = :sid",
        {"n": count, "sid": case_set_id}
    )
```

---

## 4. 质量保障

### 4.1 标注质量检查项

| 检查项 | 自动/人工 | 不通过处理 |
|--------|:--------:|-----------|
| Query 不为空 | 自动 | 阻止保存 |
| expected_intent 不为空数组 | 自动 | 阻止保存（除 `source != manual` 待审核状态） |
| expected_checkpoints 至少 1 个 | 自动 | 警告 |
| difficulty 在枚举范围内 | 自动 | 阻止保存 |
| Case Set 内 Query 去重 | 自动 | 警告 + 拒绝关联 |
| 同 `source = manual` 的 Case 需人工审核 | 人工 | 标记 `review_status = 'pending'` |

### 4.2 Case 有效性监控

每个评测执行后，增量更新 `eval_cases` 的汇总字段（`run_count` / `last_avg_score` / `health_status`，字段定义见 [data-model.md §2.3](data-model.md)）：

**失效判定规则**：

| 状态 | 条件 |
|------|------|
| `suspected_stale` | 连续 3 个版本得分 < 20，且版本间方差 < 2 |
| `deprecated` | 标记 `suspected_stale` 后，人工确认无效 |

```python
def update_case_health(case_ids: List[UUID]):
    """每轮评测后更新 Case 健康状态"""
    for cid in case_ids:
        recent = db.query(
            """SELECT t.overall_score AS score FROM traces t
               JOIN eval_runs r ON r.trace_id = t.id
               WHERE r.eval_case_id = :cid
               ORDER BY t.created_at DESC LIMIT 10""",
            {"cid": cid}
        )
        avg = mean(r.score for r in recent)
        db.execute(
            """UPDATE eval_cases SET run_count = run_count + 1,
               last_avg_score = :avg WHERE id = :cid""",
            {"avg": avg, "cid": cid}
        )
        # 判定失效
        if len(recent) >= 6 and avg < 20 and stddev(r.score for r in recent) < 2:
            db.execute(
                "UPDATE eval_cases SET health_status = 'suspected_stale' WHERE id = :cid",
                {"cid": cid}
            )
```

### 4.3 覆盖度看板

以 `category × difficulty` 交叉统计确保覆盖均衡：

```sql
SELECT category, difficulty, COUNT(*) AS cnt
FROM eval_cases
WHERE health_status = 'active'
GROUP BY category, difficulty
ORDER BY category, difficulty;
```

---

## 5. 后续 TODO

以下功能标记为进阶，留待后续迭代：

### 5.1 用例自动生成

- **意图模板扩充**：基于意图字典 + 参数槽位生成变体 Query
- **对抗样本生成**：同义改写、句式变换、错别字注入 → 测试鲁棒性
- **边界挖掘**：从生产日志中挖掘低频但曾失败的 Query 模式
- **LLM 辅助标注**：给定 Query → LLM 自动生成 expected_*，仅需人工审核

### 5.2 批量导入

- CSV / JSON 批量导入接口
- 从生产 Trace 一键转为 Case（跳过 LLM 步骤，直接人工标注）
- 测试集克隆：快速复制测试集用于 A/B 测试

### 5.3 标注质量评估

- 标注者间一致性（Inter-Annotator Agreement，Cohen's Kappa）
- 标注置信度与评测得分的相关性分析
- Case 难度与评测区分度的关联分析

---

> **关联文档**：[架构总览](architecture.md) · [数据模型](data-model.md) · [评测设计](evaluation-design.md) · [分析对比](analysis-and-compare.md)
