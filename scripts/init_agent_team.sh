#!/bin/sh
# init_agent_team.sh — minimal shell bootstrap when Python is unavailable.
#
# Usage: sh init_agent_team.sh [--target DIR] [--force]
#
# This script only copies the .agent template into the target project.
# Full project scanning and variable rendering must be completed by:
#   python init_agent_team.py --target DIR --models "..." --force
#
# When Python IS available, prefer the Python initializer directly:
#   python scripts/init_agent_team.py --target DIR --models "model-a,model-b"

set -eu

TARGET="."
FORCE=""
DRY_RUN=""
TEMPLATE_DIR=""

usage() {
    echo "Usage: sh init_agent_team.sh [--target DIR] [--force] [--dry-run]"
    echo ""
    echo "Minimal shell bootstrap for agent-team-harness."
    echo "Copies the .agent template. Full rendering requires the Python initializer."
    exit 1
}

find_script_dir() {
    dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
    echo "$dir"
}

find_template_root() {
    script_dir="$1"
    # Try relative to the repo root scripts/ directory
    if [ -d "$script_dir/../skills/agent-team-harness/assets/project-agent/.agent" ]; then
        echo "$script_dir/../skills/agent-team-harness/assets/project-agent/.agent"
        return
    fi
    # Try relative to skills/agent-team-harness/scripts/
    if [ -d "$script_dir/../assets/project-agent/.agent" ]; then
        echo "$script_dir/../assets/project-agent/.agent"
        return
    fi
    echo "" >&2
    echo "Error: cannot locate assets/project-agent/.agent template" >&2
    echo "Run this script from the agent-team-harness repository root." >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --target)
            TARGET="$2"
            shift 2
            ;;
        --force)
            FORCE="1"
            shift
            ;;
        --dry-run)
            DRY_RUN="1"
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

SCRIPT_DIR=$(find_script_dir)
TEMPLATE_DIR=$(find_template_root "$SCRIPT_DIR")

if [ ! -d "$TEMPLATE_DIR" ]; then
    echo "Error: template directory not found: $TEMPLATE_DIR" >&2
    exit 1
fi

if [ ! -d "$TARGET" ]; then
    echo "Error: target directory does not exist: $TARGET" >&2
    exit 1
fi

DEST="$TARGET/.agent"

if [ -d "$DEST" ] && [ -z "$FORCE" ]; then
    echo "Warning: $DEST already exists. Use --force to overwrite." >&2
    echo "Skipping copy. Review existing .agent/ configuration." >&2
    exit 0
fi

echo "Template source: $TEMPLATE_DIR"
echo "Target:          $DEST"

if [ -n "$DRY_RUN" ]; then
    echo "Dry run: would copy template files from $TEMPLATE_DIR to $DEST"
    exit 0
fi

# Copy template (preserving structure)
mkdir -p "$DEST"
cp -R "$TEMPLATE_DIR"/. "$DEST"/

echo ""
echo "Template copied successfully."
echo ""
echo "Next steps:"
echo "  1. python scripts/init_agent_team.py --target $TARGET --models \"...\" --force"
echo "  2. python $DEST/scripts/workflow_guard.py check-materials"
echo "  3. Review $DEST/harness.yaml and $DEST/generated/model-routing.md before development."
