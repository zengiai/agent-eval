# Initialization Review Checklist

After running `init_agent_team.py`, review these files before using the workflow:

- `.agent/harness.yaml`: project name, type, allowed write paths, validation command, model routing.
- `.agent/rule.md`: no stale source-project language, domain, or path assumptions.
- `.agent/materials/project-code-profile.md`: detected languages, module boundaries, test commands, high-risk paths.
- `.agent/generated/production_paths.txt`: production edit detection matches the repository.
- `.agent/generated/core_paths.txt`: core-path detection is conservative enough.
- `.agent/generated/model-routing.md`: model names are valid for the active project.

Do not start development if generated TODO or REVIEW REQUIRED markers affect the touched path.

