"""agent-eval 7×24 Agent 模块。

通过 Telegram IM 与开发者交互，提供评测查询、任务触发、告警推送能力。

子模块：
- gateway/: IM 网关（Telegram 适配、消息路由、安全校验）
- scheduler/: 调度框架（定时采样、报告、告警检查）
- brain/: Agent 大脑（LLM 意图理解、Function Calling 工具、多轮对话）
- notifier/: 通知系统（告警推送、消息模板）—— 待实现
"""

# AgentCore 将在后续阶段实现
