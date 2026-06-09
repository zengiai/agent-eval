#!/usr/bin/env python3
"""Repository entrypoint for project-local agent team initialization."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "agent-team-harness"
        / "scripts"
        / "init_agent_team.py"
    )
    if not script.exists():
        raise SystemExit(f"missing skill initializer: {script}")
    sys.argv = [str(script), *sys.argv[1:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()

