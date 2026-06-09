#!/usr/bin/env python3
"""Initialize a project-local .agent development team harness.

The script intentionally uses only the Python standard library so it can run in
new repositories before dependencies are installed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Sequence


IGNORED_DIRS = {
    ".git",
    ".agent",
    ".idea",
    ".vscode",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".gradle",
}

LANGUAGE_EXTENSIONS = {
    ".java": "Java",
    ".kt": "Kotlin",
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".sql": "SQL",
}

# Risk factors for semantic code file grading.
# Each factor has a category and token list matched case-insensitively against file content.
# Categories: data_write, money, concurrency, external, auth, messaging
_RISK_FACTOR_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("data_write", ("INSERT", "UPDATE", "DELETE", "save(", "persist(", "@Transactional", ".save(", ".persist(")),
    ("money", ("amount", "price", "balance", "fee", "coupon", "refund", "settlement", "wallet", "payment", "pay(", "order")),
    ("concurrency", ("lock", "synchronized", "@Lock", "acquire", "tryLock", "mutex", "semaphore", "idempot")),
    ("external", ("http.", "rpc.", "grpc", "RestTemplate", "FeignClient", "requests.", "fetch(", "axios")),
    ("auth", ("auth", "security", "permission", "token", "jwt", "oauth", "@PreAuthorize")),
    ("messaging", ("mq", "kafka", "rocketmq", "rabbit", "publish", "@KafkaListener", "@RocketMQMessageListener")),
)

# Severity tiers for core path classification
# CRITICAL: money + data_write + concurrency → cannot fail silently
# HIGH: money + data_write, or 3+ distinct categories
# MEDIUM: 1-2 risk categories
_CORE_PATH_TIERS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# Model scoring rules: ordered list of (pattern, score) pairs.
# Patterns are matched case-insensitively against the model name.
# Rules are applied in order; the first matching rule sets the base score.
# Penalties and bonuses are applied additively after base score.
MODEL_SCORING_RULES: tuple[tuple[str, int], ...] = (
    # GPT family
    ("gpt-5.5", 100),
    ("gpt-5.4", 90),
    ("gpt-5", 80),
    ("gpt-4.1", 65),
    ("gpt-4o", 55),
    ("o4", 60),
    ("o3", 55),
    ("gpt-4", 50),
    ("gpt-3.5", 20),
    # Claude family
    ("claude-opus-4", 100),
    ("claude-4-opus", 100),
    ("claude-4-sonnet", 90),
    ("claude-sonnet-4", 90),
    ("claude-3-opus", 80),
    ("claude-3.5-sonnet", 70),
    ("claude-3-sonnet", 55),
    ("claude-haiku", 35),
    ("claude", 50),
    # DeepSeek family
    ("deepseek-v4-pro", 100),
    ("deepseek-v4-flash", 65),
    ("deepseek-v4", 100),
    ("deepseek-r1", 90),
    ("deepseek-v3", 80),
    ("deepseek-chat", 65),
    ("deepseek-coder", 65),
    ("deepseek-flash", 40),
    ("deepseek", 50),
)

MODEL_PENALTIES: tuple[tuple[str, int], ...] = (
    ("mini", -15), ("small", -15), ("lite", -15),
)

IMPLEMENTATION_ROLES = {"developer-agent", "java-engineer", "python-engineer", "go-engineer", "frontend-engineer"}
CODE_REVIEW_ROLES = {"code-reviewer"}
REASONING_ROLES = {"solution-architect", "java-architect", "performance-optimizer"}
BALANCED_ROLES = {"pm-orchestrator", "requirement-analyst", "gate-reviewer", "qa-tester"}

AGENTS_MD_START = "<!-- agent-team-harness:start -->"
AGENTS_MD_END = "<!-- agent-team-harness:end -->"
AGENTS_MD_SECTION = f"""{AGENTS_MD_START}
## Agent Team Harness

When this repository contains `.agent/`, use the project-local Agent Team Harness workflow for non-trivial development, bug fixes, refactors, code reviews, QA verification, production-risk changes, model routing, and delivery summaries.

For relevant work:

- Use `$agent-team-harness` when installing, refreshing, repairing, or operating the `.agent` workflow.
- Read `.agent/rule.md` and `.agent/harness.yaml` before editing production code.
- Run or respect `.agent/scripts/workflow_guard.py` stage gates for feature work.
- Use `.agent/skill-teams/project-dev-team` role guidance for requirement analysis, solution design, gate review, development, code review, QA, and PM orchestration.
- Treat guard failures as blockers unless the project owner explicitly overrides them.
{AGENTS_MD_END}
"""


@dataclass(frozen=True)
class ModelRouting:
    strongest: str
    reasoning: str
    balanced: str
    source: str
    review_required: bool
    available: tuple[str, ...]


@dataclass(frozen=True)
class ProjectScan:
    name: str
    project_type: str
    languages: tuple[str, ...]
    frameworks: tuple[str, ...]
    validation_command: str
    production_paths: tuple[str, ...]
    core_paths: tuple[str, ...]
    module_rows: tuple[tuple[str, str, str], ...]
    focused_tests: tuple[tuple[str, tuple[str, ...]], ...]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize .agent team workflow in a target project.")
    parser.add_argument("--target", default=".", help="target project root; defaults to current directory")
    parser.add_argument("--project-name", help="override detected project name")
    parser.add_argument("--project-type", help="override detected project type")
    parser.add_argument("--languages", help="comma-separated language override")
    parser.add_argument(
        "--models",
        help="comma-separated available models. If omitted, AGENT_TEAM_MODELS is used; otherwise placeholders are kept.",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing .agent files")
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without writing files")
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser().resolve()
    if not target.exists() or not target.is_dir():
        raise SystemExit(f"target project root does not exist or is not a directory: {target}")

    template_agent = _template_agent_root()
    existing_agent = (target / ".agent").exists()
    scan = _scan_project(target)
    if args.project_name:
        scan = _replace_scan(scan, name=args.project_name)
    if args.project_type:
        scan = _replace_scan(scan, project_type=args.project_type)
    if args.languages:
        languages = tuple(_split_csv(args.languages))
        scan = _replace_scan(scan, languages=languages)

    model_text = args.models or os.environ.get("AGENT_TEAM_MODELS", "")
    routing = _select_models(tuple(_split_csv(model_text)))

    actions: list[str] = []
    _copy_template(template_agent, target / ".agent", force=args.force, dry_run=args.dry_run, actions=actions)
    conflict_mode = existing_agent and not args.force
    _render_project_files(
        target,
        template_agent,
        scan,
        routing,
        force=args.force,
        dry_run=args.dry_run,
        conflict_mode=conflict_mode,
        actions=actions,
    )
    _update_agents_md(target, dry_run=args.dry_run, actions=actions)

    print("Agent team initialization complete" if not args.dry_run else "Agent team initialization dry run")
    print(f"Target: {target}")
    for action in actions:
        print(action)
    print("Next:")
    print("  python .agent/scripts/workflow_guard.py check-materials")
    print("  Review .agent/harness.yaml and .agent/generated/model-routing.md before development.")
    return 0


def _template_agent_root() -> Path:
    script = Path(__file__).resolve()
    candidates = (
        script.parents[1] / "assets" / "project-agent" / ".agent",
        script.parents[3] / "skills" / "agent-team-harness" / "assets" / "project-agent" / ".agent",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("cannot locate assets/project-agent/.agent template")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _replace_scan(scan: ProjectScan, **updates: object) -> ProjectScan:
    values = {
        "name": scan.name,
        "project_type": scan.project_type,
        "languages": scan.languages,
        "frameworks": scan.frameworks,
        "validation_command": scan.validation_command,
        "production_paths": scan.production_paths,
        "core_paths": scan.core_paths,
        "module_rows": scan.module_rows,
        "focused_tests": scan.focused_tests,
    }
    values.update(updates)
    return ProjectScan(**values)  # type: ignore[arg-type]


def _copy_template(src: Path, dest: Path, *, force: bool, dry_run: bool, actions: list[str]) -> None:
    for source in sorted(path for path in src.rglob("*") if path.is_file()):
        if source.name == ".DS_Store":
            continue
        relative = source.relative_to(src)
        target = dest / relative
        data = source.read_bytes()
        _safe_write_bytes(target, data, force=force, dry_run=dry_run, actions=actions)


def _safe_write_bytes(path: Path, data: bytes, *, force: bool, dry_run: bool, actions: list[str]) -> None:
    if path.exists() and path.read_bytes() == data:
        actions.append(f"unchanged: {_display(path)}")
        return
    target = path
    if path.exists() and not force:
        target = path.with_name(path.name + ".agent-team-new")
    actions.append(("write: " if target == path else "conflict-write: ") + _display(target))
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _safe_write_text(
    path: Path,
    text: str,
    *,
    force: bool,
    dry_run: bool,
    conflict_mode: bool,
    actions: list[str],
) -> None:
    data = text.encode("utf-8")
    target = path
    if path.exists() and path.read_bytes() == data:
        actions.append(f"unchanged: {_display(path)}")
        return
    if path.exists() and (conflict_mode and not force):
        target = path.with_name(path.name + ".agent-team-new")
    actions.append(("write: " if target == path else "conflict-write: ") + _display(target))
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _update_agents_md(target: Path, *, dry_run: bool, actions: list[str]) -> None:
    """Append or refresh the project-level AGENTS.md integration when present."""
    path = target / "AGENTS.md"
    if not path.exists():
        actions.append(f"skip: {_display(path)} (not found)")
        return
    current = path.read_text(encoding="utf-8", errors="ignore")
    updated = _merge_agents_md_section(current)
    if updated == current:
        actions.append(f"unchanged: {_display(path)}")
        return
    actions.append(f"write: {_display(path)}")
    if dry_run:
        return
    path.write_text(updated, encoding="utf-8")


def _merge_agents_md_section(current: str) -> str:
    section = AGENTS_MD_SECTION.strip()
    pattern = re.compile(
        rf"{re.escape(AGENTS_MD_START)}.*?{re.escape(AGENTS_MD_END)}",
        re.DOTALL,
    )
    if pattern.search(current):
        return pattern.sub(section, current)
    separator = "\n\n" if current.rstrip() else ""
    return f"{current.rstrip()}{separator}{section}\n"


def _scan_project(root: Path) -> ProjectScan:
    files = tuple(_iter_project_files(root))
    languages = _detect_languages(files)
    name = _detect_project_name(root)
    frameworks = _detect_frameworks(root)
    validation = _detect_validation_command(root)
    production_paths = _detect_production_paths(root, languages)
    core_paths = _detect_core_paths(root, files, production_paths)
    project_type = _detect_project_type(languages, frameworks, files)
    module_rows = _module_rows(production_paths, languages)
    focused_tests = _focused_tests(root, validation)
    return ProjectScan(
        name=name,
        project_type=project_type,
        languages=languages or ("Unknown",),
        frameworks=frameworks or ("None detected",),
        validation_command=validation,
        production_paths=production_paths,
        core_paths=core_paths,
        module_rows=module_rows,
        focused_tests=focused_tests,
    )


def _iter_project_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        yield relative


def _detect_languages(files: Sequence[Path]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for path in files:
        language = LANGUAGE_EXTENSIONS.get(path.suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    if Path("pom.xml") in files or any(path.name.endswith(".gradle") for path in files):
        counts["Java"] = counts.get("Java", 0) + 1
    if Path("package.json") in files:
        counts["TypeScript"] = counts.get("TypeScript", 0) + 1
    if Path("pyproject.toml") in files:
        counts["Python"] = counts.get("Python", 0) + 1
    return tuple(name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _detect_project_name(root: Path) -> str:
    package_json = root / "package.json"
    if package_json.exists():
        try:
            name = json.loads(package_json.read_text(encoding="utf-8")).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        except (OSError, json.JSONDecodeError):
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        match = re.search(r"(?m)^name\s*=\s*[\"']([^\"']+)[\"']", pyproject.read_text(encoding="utf-8", errors="ignore"))
        if match:
            return match.group(1)

    pom = root / "pom.xml"
    if pom.exists():
        match = re.search(r"<artifactId>([^<]+)</artifactId>", pom.read_text(encoding="utf-8", errors="ignore"))
        if match:
            return match.group(1)

    return root.name


def _detect_frameworks(root: Path) -> tuple[str, ...]:
    frameworks: set[str] = set()
    _detect_package_json_frameworks(root / "package.json", frameworks)
    _detect_text_frameworks(root / "pom.xml", frameworks)
    for gradle in (root / "build.gradle", root / "build.gradle.kts"):
        _detect_text_frameworks(gradle, frameworks)
    _detect_pyproject_frameworks(root / "pyproject.toml", frameworks)
    return tuple(sorted(frameworks))


def _detect_package_json_frameworks(path: Path, frameworks: set[str]) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    deps: dict[str, object] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = data.get(key)
        if isinstance(value, dict):
            deps.update(value)
    names = set(deps)
    checks = {
        "react": "React",
        "vue": "Vue",
        "next": "Next.js",
        "vite": "Vite",
        "express": "Express",
        "@nestjs/core": "NestJS",
    }
    for dependency, label in checks.items():
        if dependency in names:
            frameworks.add(label)


def _detect_text_frameworks(path: Path, frameworks: set[str]) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    checks = {
        "spring-boot": "Spring Boot",
        "mybatis": "MyBatis",
        "rocketmq": "RocketMQ",
        "nacos": "Nacos",
        "sentinel": "Sentinel",
        "seata": "Seata",
    }
    for needle, label in checks.items():
        if needle in text:
            frameworks.add(label)


def _detect_pyproject_frameworks(path: Path, frameworks: set[str]) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    checks = {
        "fastapi": "FastAPI",
        "django": "Django",
        "flask": "Flask",
        "pytest": "Pytest",
    }
    for needle, label in checks.items():
        if needle in text:
            frameworks.add(label)


def _detect_validation_command(root: Path) -> str:
    package_json = root / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if isinstance(scripts, dict) and "test" in scripts:
            if (root / "pnpm-lock.yaml").exists():
                return "pnpm test"
            if (root / "yarn.lock").exists():
                return "yarn test"
            return "npm test"
    if (root / "pom.xml").exists():
        return "mvn test"
    if (root / "gradlew").exists():
        return "./gradlew test"
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").exists():
        return "python -m pytest"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "Cargo.toml").exists():
        return "cargo test"
    return "TODO: set project validation command"


def _detect_project_type(languages: Sequence[str], frameworks: Sequence[str], files: Sequence[Path]) -> str:
    language_set = set(languages)
    framework_set = set(frameworks)
    if {"Java", "TypeScript"} <= language_set or {"Java", "JavaScript"} <= language_set:
        return "full-stack-application"
    if "Spring Boot" in framework_set:
        return "backend-service"
    if framework_set.intersection({"React", "Vue", "Next.js", "Vite"}):
        return "frontend-application"
    if "Python" in language_set and any(str(path).startswith("tests/") for path in files):
        return "python-library-or-service"
    if len(language_set) > 1:
        return "mixed-language-project"
    return "software-project"


def _detect_production_paths(root: Path, languages: Sequence[str]) -> tuple[str, ...]:
    paths: list[str] = []
    language_set = set(languages)
    if "Java" in language_set or "Kotlin" in language_set:
        paths.extend(["src/main/**"])
    if "Python" in language_set:
        packages = [
            item.name + "/**"
            for item in sorted(root.iterdir())
            if item.is_dir()
            and (item / "__init__.py").exists()
            and item.name not in {"tests", "docs"}
            and not item.name.startswith(".")
        ]
        paths.extend(packages or ["*.py"])
    if {"TypeScript", "JavaScript"}.intersection(language_set):
        for candidate in ("src", "app", "pages", "components", "lib", "packages"):
            if (root / candidate).exists():
                paths.append(f"{candidate}/**")
    if "Go" in language_set:
        paths.extend(path for path in ("cmd/**", "internal/**", "pkg/**") if _glob_exists(root, path))
        paths.append("*.go")
    if "Rust" in language_set:
        paths.extend(["src/**", "crates/**/src/**"])
    if not paths:
        paths.extend(["src/**", "app/**", "lib/**"])
    return _dedupe(paths)


def _glob_exists(root: Path, pattern: str) -> bool:
    return any(root.glob(pattern))


def _detect_core_paths(root: Path, files: Sequence[Path], production_paths: Sequence[str]) -> tuple[str, ...]:
    """Semantic code file grading using risk factor density analysis.

    Reads file content (first 500 lines) of production-path files, counts distinct
    risk categories, and assigns a severity tier:
      CRITICAL: money + data_write + concurrency
      HIGH: money + data_write, or 3+ categories
      MEDIUM: 1-2 categories
      LOW: no risk categories detected
    Returns directory-level glob patterns for CRITICAL and HIGH files.
    """
    critical_dirs: set[str] = set()
    high_dirs: set[str] = set()
    for path in files:
        if not _path_matches_any(path.as_posix(), production_paths):
            continue
        tier = _classify_file_risk(root, path)
        if tier in ("CRITICAL", "HIGH"):
            if len(path.parts) > 1:
                dir_pattern = Path(*path.parts[:-1]).as_posix() + "/**"
                if tier == "CRITICAL":
                    critical_dirs.add(dir_pattern)
                else:
                    high_dirs.add(dir_pattern)
            else:
                (critical_dirs if tier == "CRITICAL" else high_dirs).add(path.as_posix())

    # Merge: CRITICAL first, then HIGH (excluding duplicates), then fallback
    core = list(critical_dirs) + [d for d in high_dirs if d not in critical_dirs]
    if core:
        return tuple(sorted(core))
    # Fallback: use risk-token path-name matching as secondary heuristic
    fallback = _fallback_core_paths(files, production_paths)
    return fallback if fallback else tuple(production_paths[: min(5, len(production_paths))])


def _classify_file_risk(root: Path, path: Path) -> str:
    """Read the first 500 lines of a file and count distinct risk categories."""
    try:
        content = (root / path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "LOW"
    # Only scan the first 500 lines to keep it fast for large files
    head = "\n".join(content.splitlines()[:500]).lower()
    categories_hit = 0
    has_money = False
    has_data_write = False
    has_concurrency = False
    for category, tokens in _RISK_FACTOR_CATEGORIES:
        if any(token.lower() in head for token in tokens):
            categories_hit += 1
            if category == "money":
                has_money = True
            elif category == "data_write":
                has_data_write = True
            elif category == "concurrency":
                has_concurrency = True
    if has_money and has_data_write and has_concurrency:
        return "CRITICAL"
    if has_money and has_data_write:
        return "HIGH"
    if categories_hit >= 3:
        return "HIGH"
    if categories_hit >= 1:
        return "MEDIUM"
    return "LOW"


def _fallback_core_paths(files: Sequence[Path], production_paths: Sequence[str]) -> tuple[str, ...]:
    """Path-name heuristic fallback when content analysis yields no core paths."""
    path_risk_tokens = {
        "order", "trade", "payment", "pay", "refund", "inventory", "stock",
        "coupon", "promotion", "marketing", "risk", "auth", "security",
        "permission", "gateway", "transaction", "settlement", "account",
        "wallet", "mq", "rocketmq", "kafka", "redis", "cache", "lock",
    }
    candidates: set[str] = set()
    for path in files:
        text = path.as_posix().lower()
        if not _path_matches_any(path.as_posix(), production_paths):
            continue
        if any(token in text for token in path_risk_tokens):
            if len(path.parts) > 1:
                candidates.add(Path(*path.parts[:-1]).as_posix() + "/**")
            else:
                candidates.add(path.as_posix())
    return tuple(sorted(candidates)) if candidates else ()


def _path_matches_any(path: str, patterns: Sequence[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _module_rows(production_paths: Sequence[str], languages: Sequence[str]) -> tuple[tuple[str, str, str], ...]:
    primary = ", ".join(languages) if languages else "Unknown"
    rows = []
    for pattern in production_paths:
        area = pattern.split("/")[0].strip("*") or "root"
        rows.append((area, pattern, f"{primary} production code; review local ownership."))
    return tuple(rows)


def _focused_tests(root: Path, validation: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    groups: list[tuple[str, tuple[str, ...]]] = [("default", (validation,))]
    tests = root / "tests"
    if tests.exists():
        groups.append(("focused_python_tests", ("python -m pytest tests",)))
    if (root / "src" / "test").exists():
        groups.append(("focused_java_tests", ("mvn test",)))
    if (root / "package.json").exists():
        groups.append(("frontend_or_node_tests", (validation if "test" in validation else "npm test",)))
    return tuple(groups)


def _select_models(models: Sequence[str]) -> ModelRouting:
    available = tuple(_dedupe(models))
    if not available:
        return ModelRouting(
            strongest="MODEL_STRONGEST_AVAILABLE",
            reasoning="MODEL_REASONING_AVAILABLE",
            balanced="MODEL_BALANCED_AVAILABLE",
            source="not_provided",
            review_required=True,
            available=(),
        )
    ranked = tuple(sorted(available, key=_model_score, reverse=True))
    strongest = ranked[0]
    reasoning = ranked[1] if len(ranked) > 1 else strongest
    balanced = ranked[-1] if len(ranked) > 2 else reasoning
    return ModelRouting(
        strongest=strongest,
        reasoning=reasoning,
        balanced=balanced,
        source="argument_or_environment",
        review_required=False,
        available=available,
    )


def _model_score(model: str) -> tuple[int, str]:
    """Score a model name using configurable rules supporting GPT, Claude, and DeepSeek families."""
    text = model.lower()
    score = 0
    for pattern, base_score in MODEL_SCORING_RULES:
        if pattern in text:
            score = base_score
            break
    if score == 0:
        score += 10
    if "codex" in text:
        score += 8
    for pattern, penalty in MODEL_PENALTIES:
        if pattern in text:
            score += penalty
    return score, model


def _render_project_files(
    target: Path,
    template_agent: Path,
    scan: ProjectScan,
    routing: ModelRouting,
    *,
    force: bool,
    dry_run: bool,
    conflict_mode: bool,
    actions: list[str],
) -> None:
    agent = target / ".agent"
    replacements = {
        "__PROJECT_NAME__": scan.name,
        "__PROJECT_TYPE__": scan.project_type,
        "__PRIMARY_LANGUAGE__": scan.languages[0] if scan.languages else "Unknown",
        "__PRIMARY_LANGUAGES__": ", ".join(scan.languages),
        "__FRAMEWORKS__": ", ".join(scan.frameworks),
        "__VALIDATION_COMMAND__": scan.validation_command,
        "__GENERATED_AT__": date.today().isoformat(),
        "__REVIEW_REQUIRED__": "true" if routing.review_required else "false",
        "__MODEL_AVAILABILITY_SOURCE__": routing.source,
        "__MODEL_REVIEW_REQUIRED__": "true" if routing.review_required else "false",
        "__MODEL_STRONGEST_AVAILABLE__": routing.strongest,
        "__MODEL_REASONING_AVAILABLE__": routing.reasoning,
        "__MODEL_BALANCED_AVAILABLE__": routing.balanced,
        "__ALLOWED_WRITE_PATHS__": _yaml_list(scan.production_paths + ("tests/**", ".agent/**", "docs/features/**"), indent=4),
        "__CORE_PATHS__": _yaml_list(scan.core_paths, indent=4),
        "__FOCUSED_TESTS__": _yaml_focused_tests(scan.focused_tests, indent=4),
        "__MODULE_BOUNDARY_ROWS__": _markdown_module_rows(scan.module_rows),
    }

    for relative in ("harness.yaml", "materials/project-code-profile.md"):
        path = agent / relative
        text = _render_source(
            path,
            template_agent / relative,
            conflict_mode=conflict_mode,
            force=force,
            dry_run=dry_run,
        ).read_text(encoding="utf-8")
        _safe_write_text(
            path,
            _replace_all(text, replacements),
            force=force,
            dry_run=dry_run,
            conflict_mode=conflict_mode,
            actions=actions,
        )

    _safe_write_text(
        agent / "generated" / "production_paths.txt",
        _lines(scan.production_paths),
        force=force,
        dry_run=dry_run,
        conflict_mode=False,
        actions=actions,
    )
    _safe_write_text(
        agent / "generated" / "core_paths.txt",
        _lines(scan.core_paths),
        force=force,
        dry_run=dry_run,
        conflict_mode=False,
        actions=actions,
    )
    _safe_write_text(
        agent / "generated" / "model-routing.md",
        _model_routing_markdown(routing),
        force=force,
        dry_run=dry_run,
        conflict_mode=False,
        actions=actions,
    )
    _update_role_model_files(agent, routing, force=force, dry_run=dry_run, conflict_mode=conflict_mode, actions=actions)


def _replace_all(text: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _yaml_list(items: Sequence[str], *, indent: int) -> str:
    prefix = " " * indent
    return "\n".join(f"{prefix}- {item}" for item in _dedupe(items))


def _yaml_focused_tests(groups: Sequence[tuple[str, tuple[str, ...]]], *, indent: int) -> str:
    prefix = " " * indent
    lines: list[str] = []
    for name, commands in groups:
        lines.append(f"{prefix}{name}:")
        for command in commands:
            lines.append(f"{prefix}  - {command}")
    return "\n".join(lines)


def _markdown_module_rows(rows: Sequence[tuple[str, str, str]]) -> str:
    return "\n".join(f"| {area} | `{path}` | {responsibility} |" for area, path, responsibility in rows)


def _lines(items: Sequence[str]) -> str:
    return "\n".join(_dedupe(items)) + "\n"


def _model_routing_markdown(routing: ModelRouting) -> str:
    available = ", ".join(routing.available) if routing.available else "not provided"
    review = "yes" if routing.review_required else "no"
    return f"""# Model Routing

- Availability source: {routing.source}
- Review required: {review}
- Available models: {available}

## Selected Models

| Role group | Model | Reasoning effort |
| --- | --- | --- |
| Implementation | `{routing.strongest}` | `xhigh` |
| Code review | `{routing.strongest}` | `xhigh` |
| Architecture and performance | `{routing.reasoning}` | `high` |
| PM, requirement, gate, QA | `{routing.balanced}` | `medium` |

## Rule

Implementation and code review use the strongest available coding model.
Architecture and performance use a strong reasoning model. Lightweight roles use
a balanced model unless the feature document records a core-path or release-risk
escalation reason.
"""


def _update_role_model_files(
    agent: Path,
    routing: ModelRouting,
    *,
    force: bool,
    dry_run: bool,
    conflict_mode: bool,
    actions: list[str],
) -> None:
    root = agent / "skill-teams" / "project-dev-team"
    if not root.exists():
        return
    for yaml_path in sorted(root.glob("*/agents/openai.yaml")):
        role = yaml_path.parents[1].name
        if role in IMPLEMENTATION_ROLES or role in CODE_REVIEW_ROLES:
            model = routing.strongest
            effort = "xhigh"
        elif role in REASONING_ROLES:
            model = routing.reasoning
            effort = "high"
        else:
            model = routing.balanced
            effort = "medium"
        template_path = agent.parent / "__missing_template__"
        try:
            template_path = (Path(__file__).resolve().parents[1] / "assets" / "project-agent" / ".agent" / yaml_path.relative_to(agent))
        except ValueError:
            pass
        text = _render_source(
            yaml_path,
            template_path,
            conflict_mode=conflict_mode,
            force=force,
            dry_run=dry_run,
        ).read_text(encoding="utf-8")
        text = re.sub(r'(?m)^  model: ".+?"$', f'  model: "{model}"', text)
        text = re.sub(r'(?m)^  reasoning_effort: ".+?"$', f'  reasoning_effort: "{effort}"', text)
        _safe_write_text(yaml_path, text, force=force, dry_run=dry_run, conflict_mode=conflict_mode, actions=actions)


def _render_source(path: Path, template_path: Path, *, conflict_mode: bool, force: bool, dry_run: bool) -> Path:
    conflict_path = path.with_name(path.name + ".agent-team-new")
    if conflict_mode and not force and conflict_path.exists():
        return conflict_path
    if conflict_mode and not force and dry_run and template_path.exists():
        return template_path
    if dry_run and not path.exists() and template_path.exists():
        return template_path
    return path


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _display(path: Path) -> str:
    return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
