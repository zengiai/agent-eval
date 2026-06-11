# 工作流强制规则

> 本规则文件由 Qoder 自动读取，每次会话均生效。

---

## 触发条件

**唯一触发关键词：`(feat)`** — 用户消息最前面出现此关键词时，本规则立即生效。

识别规则：
- 消息以 `(feat)` 开头 → 启动工作流
- 消息不以 `(feat)` 开头 → 按普通请求处理，不启动工作流

示例：
```
(feat) 帮我实现 OpenTelemetry SpanExporter 适配器     → 启动工作流 ✅
帮我实现 OpenTelemetry SpanExporter 适配器            → 不启动 ❌
这个函数干什么的？                                      → 不启动 ❌
```

---

## 强制流程

```
Step 1: 创建 docs/features/<FeatureSlug>/ 目录
Step 2: 编写 01_REQUIREMENT_ANALYSIS.md（范围、验收标准、不做什么）
Step 3: 编写 02_SOLUTION_DESIGN.md（架构、接口、数据模型、风险）
Step 4: 编写 03_GATE_REVIEW.md（门禁检查清单）
Step 5: 进入代码实现（此时才允许 Write / SearchReplace）
Step 6: 编写 05_CODE_REVIEW.md
Step 7: 运行测试，编写 06_TEST_REPORT.md
```

---

## 禁止行为

以 `(feat)` 开头的消息，以下行为一律禁止：

- ❌ 跳过 Step 1-4 直接执行 `Write` 或 `SearchReplace`
- ❌ 以"workflow_guard.py 不存在"为由跳过流程
- ❌ 以"改动很小"为由跳过流程
- ❌ 以"已经讨论清楚了"为由跳过文档

---

## FeatureSlug 命名

使用 PascalCase 英文，反映核心变更：

- `OtelAdapterAndTutorial` — OTel 适配器 + 使用教程
- `Phase1MVP` — 第一阶段 MVP
- `FixOutcomeDivisionByZero` — 修复除零错误
