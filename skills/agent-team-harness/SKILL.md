---
name: agent-team-harness
description: Use this skill when the user asks to initialize, install, refresh, repair, migrate, or operate Agent Team Harness, a reusable project-local `.agent` AI development team workflow; configure role-specific model routing; bootstrap requirement/design/development/code-review/QA feature documents; enforce workflow gates; update target AGENTS.md integration; or improve reusable team materials after a feature delivery.
---

# Agent Team Harness

## Overview

Use this skill to install a reusable `.agent` team workflow into another repository and adapt it to that repository's language, code structure, validation commands, and available model set.

## Initialization Workflow

1. Inspect the target project structure, existing instructions, major languages, test/build files, and high-risk code paths.
2. Discover or request the currently available model names. If the environment cannot list models, ask the user or pass a known list through `--models`.
3. Run the initializer:

```bash
python skills/agent-team-harness/scripts/init_agent_team.py --target /path/to/project --models "model-a,model-b"
```

4. If the target project already has `AGENTS.md`, the initializer appends or refreshes a managed Agent Team Harness section so future agents know when to use the project-local workflow. It does not create `AGENTS.md` when the file is absent.

5. Review generated files before starting development:

- `.agent/harness.yaml`
- `.agent/rule.md`
- `.agent/materials/project-code-profile.md`
- `.agent/generated/model-routing.md`
- `.agent/generated/production_paths.txt`
- `.agent/generated/core_paths.txt`
- `AGENTS.md` managed section, only when the target already had `AGENTS.md`

6. If the target already has `.agent` files, do not overwrite them silently. Use `--force` only when the user approves replacing local project rules.

## Model Routing Rule

Read `references/model-routing.md` when choosing or reviewing role models.

Default policy:

- Implementation and code review roles receive the strongest available coding model and highest practical reasoning effort.
- Architecture and performance roles receive a strong reasoning model, usually one step below implementation unless the change is core-path or high-risk.
- PM, requirement, gate, and QA roles receive a balanced model unless the project owner raises risk level.
- If model availability differs by project, regenerate `.agent/harness.yaml` with the project's model list instead of copying another repository's fixed routing.

## Feature Workflow

After initialization, large requirements must move through:

```mermaid
stateDiagram-v2
    [*] --> REQUIREMENT_ANALYSIS
    REQUIREMENT_ANALYSIS --> SOLUTION_DESIGN : scope approved
    SOLUTION_DESIGN --> GATE_REVIEW : design complete
    SOLUTION_DESIGN --> REQUIREMENT_ANALYSIS : missing info
    GATE_REVIEW --> DEVELOPMENT : GO / CONDITIONAL GO
    GATE_REVIEW --> REQUIREMENT_ANALYSIS : blocked
    GATE_REVIEW --> SOLUTION_DESIGN : design gap
    DEVELOPMENT --> CODE_REVIEW : implementation complete
    DEVELOPMENT --> SOLUTION_DESIGN : design conflict
    CODE_REVIEW --> TEST_VERIFICATION : APPROVED / APPROVED_WITH_NOTES
    CODE_REVIEW --> DEVELOPMENT : must-fix items
    TEST_VERIFICATION --> DELIVERY_SUMMARY : READY
    TEST_VERIFICATION --> DEVELOPMENT : blocking defects
    DELIVERY_SUMMARY --> [*]

    note right of GATE_REVIEW
        Hard rule: DEVELOPMENT_MUST_NOT_START
        if blockers exist
    end note

    note right of CODE_REVIEW
        Hard rule: TEST_VERIFICATION_MUST_NOT_START
        if must-fix items remain
    end note

    note left of TEST_VERIFICATION
        Hard rule: 3 consecutive rollbacks
        → force re-architecture review
    end note
```

Use `.agent/scripts/workflow_guard.py` to initialize documents and enforce stage gates. Treat a non-zero guard result as a blocker.

### Guard Commands

```mermaid
flowchart TD
    A[check-materials] --> B{6 materials + templates ok?}
    B -->|no| BLOCK[BLOCKED: fix materials]
    B -->|yes| C[init FeatureSlug]
    C --> D[enter REQUIREMENT_ANALYSIS]
    D --> E[complete REQUIREMENT_ANALYSIS]
    E --> F[enter SOLUTION_DESIGN]
    F --> G[complete SOLUTION_DESIGN]
    G --> H[enter GATE_REVIEW]
    H --> I[complete GATE_REVIEW]
    I --> J{Decision == GO?}
    J -->|no| BLOCK
    J -->|yes| K[enter DEVELOPMENT]
    K --> L[pre-edit --files]
    L --> M{production files > 3?}
    M -->|yes| SPLIT[BLOCKED: split task]
    M -->|no| N{core path touched?}
    N -->|yes| O[verify 7 core-path notes]
    N -->|no| P[complete DEVELOPMENT]
    O --> P
    P --> Q[enter CODE_REVIEW]
    Q --> R[complete CODE_REVIEW]
    R --> S{APPROVED?}
    S -->|no| BLOCK
    S -->|yes| T[enter TEST_VERIFICATION]
    T --> U[complete TEST_VERIFICATION]
    U --> V{READY?}
    V -->|no| BLOCK
    V -->|yes| W[DELIVERY_SUMMARY]
```

## Continuous Improvement

After a large feature finishes, run:

```bash
python .agent/scripts/feature_improvement.py <FeatureSlug>
```

Review the generated note and update `.agent/materials`, `.agent/rule.md`, templates, or role skills only when the feature produced a reusable rule, pattern, checklist item, or failure mode.

## Resources

- `scripts/init_agent_team.py`: deterministic initializer for target projects.
- `assets/project-agent/.agent`: reusable project-local team template.
- `references/model-routing.md`: model selection policy.
- `references/initialization-workflow.md`: detailed review checklist for initialization.
