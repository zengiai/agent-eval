# Model Routing Policy

Use this policy when initializing or reviewing `.agent/harness.yaml`.

## Inputs

- Available model names from the active project or platform.
- Role risk: implementation, code review, architecture, performance, PM, requirement, gate, QA.
- Project type: backend, frontend, full-stack, data, infrastructure, or mixed.
- Change risk: core path, data consistency, security, performance, or low-risk support code.

## Selection Rules

1. Rank available models by coding ability, reasoning depth, reliability, and project compatibility.
2. Assign the strongest available model to:
   - `developer-agent`
   - language-specific implementation roles such as `java-engineer`
   - `code-reviewer`
3. Assign a strong reasoning model to:
   - `solution-architect`
   - `java-architect`
   - `performance-optimizer`
4. Assign a balanced model to:
   - `pm-orchestrator`
   - `requirement-analyst`
   - `gate-reviewer`
   - `qa-tester`
5. Escalate non-implementation roles only when the change is core-path, high-concurrency, data-consistency, security, or release-critical.

## Fallback

If the environment cannot list models, keep placeholders:

- `MODEL_STRONGEST_AVAILABLE`
- `MODEL_REASONING_AVAILABLE`
- `MODEL_BALANCED_AVAILABLE`

Mark `.agent/generated/model-routing.md` as review required before development starts.

