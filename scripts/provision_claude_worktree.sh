#!/bin/sh
# Create and provision Claude Code ephemeral worktrees (.claude/worktrees/).
#
# Wired to the WorktreeCreate hook in .claude/settings.json. A configured
# WorktreeCreate hook OWNS worktree creation: it must create the worktree
# itself and print its path to stdout (only the path may go to stdout; all
# diagnostics go to stderr). Removal stays with Claude Code's built-in logic.
#
# `bin/xms-task` in the workspace root calls this same script in manual mode
# when it provisions a .tasks/<name>/xmsconan worktree.
#
# Manual venv provisioning for an existing worktree:
#   sh scripts/provision_claude_worktree.sh provision /path/to/worktree
set -eu

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

# The lint stack must match .github/workflows/xmsconan-ci.yaml, or a worktree
# passes flake8 locally and still fails the Flake Project job on the same code.
FLAKE_DEPS="flake8 flake8-docstrings flake8-bugbear flake8-import-order pep8-naming"

json_field() {
    # $1 = JSON document, $2 = top-level field name
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$1" | jq -r ".$2 // empty"
    else
        printf '%s' "$1" | python3 -c \
            "import json,sys; print(json.load(sys.stdin).get('$2', ''))"
    fi
}

provision() {
    # $1 = worktree path, $2 = mode ("hook" or "manual"). In hook mode a
    # missing uv soft-skips (returns 0) so a provisioning nicety never blocks
    # worktree creation; in manual mode the user explicitly asked for a venv,
    # so that case fails. A real install failure always returns non-zero.
    MODE="${2:-hook}"
    SKIP=0
    [ "$MODE" = "hook" ] || SKIP=1
    cd "$1" || return 1
    if ! command -v uv >/dev/null 2>&1; then
        echo "provision_claude_worktree: uv not on PATH; skipping venv setup." >&2
        echo "Install uv, then run: uv venv && uv pip install -e '.[test]' $FLAKE_DEPS" >&2
        return $SKIP
    fi
    if [ ! -f pyproject.toml ]; then
        # The worktree base predates the current layout (e.g. an old tag).
        echo "provision_claude_worktree: no pyproject.toml in $1; skipping venv setup." >&2
        return $SKIP
    fi
    # Never inherit another worktree's venv.
    unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT
    # 3.13 matches the CI matrix; fall back to uv's default interpreter when
    # 3.13 is neither installed nor fetchable (offline).
    uv venv --python 3.13 1>&2 || uv venv 1>&2 || return 1
    # xmsconan has no uv.lock -- dependencies resolve from pyproject.toml. The
    # editable install is what makes `xmsconan_gen` inside the worktree run
    # THIS tree's generator instead of the wheel installed on PATH.
    # shellcheck disable=SC2086
    uv pip install -e ".[test]" $FLAKE_DEPS 1>&2 || return 1
    [ -f .envrc ] || printf 'source .venv/bin/activate\n' > .envrc
    echo "provision_claude_worktree: .venv ready in $1" >&2
}

if [ "${1:-}" = "provision" ]; then
    # Propagate the failure: an unconditional `exit 0` would discard every
    # non-zero return the function makes in manual mode, so a provision that
    # left no usable .venv behind would still report success to the caller.
    provision "${2:?usage: provision_claude_worktree.sh provision <path>}" manual || exit 1
    exit 0
fi

INPUT="$(cat)"
EVENT="$(json_field "$INPUT" hook_event_name)"

case "$EVENT" in
WorktreeCreate)
    NAME="$(json_field "$INPUT" name)"
    # Allow only safe path characters, then let git validate ref legality
    # (leading dot, trailing slash/dot, .lock suffix, etc.); anything
    # rejected gets a generated name.
    case "$NAME" in
    '' | -* | *..* | *[!A-Za-z0-9./_-]*)
        echo "provision_claude_worktree: unsafe worktree name '${NAME:-<empty>}'; using wt-$$" >&2
        NAME="wt-$$"
        ;;
    esac
    if ! git check-ref-format --branch "$NAME" >/dev/null 2>&1; then
        echo "provision_claude_worktree: '$NAME' is not a valid branch name; using wt-$$" >&2
        NAME="wt-$$"
    fi
    git -C "$PROJECT_DIR" worktree prune 1>&2
    WT="$PROJECT_DIR/.claude/worktrees/$NAME"
    BRANCH="$NAME"
    if [ -e "$WT" ] || git -C "$PROJECT_DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
        NAME="$NAME-$$"
        WT="$PROJECT_DIR/.claude/worktrees/$NAME"
        BRANCH="$NAME"
    fi
    mkdir -p "$(dirname "$WT")"
    git -C "$PROJECT_DIR" worktree add -b "$BRANCH" "$WT" HEAD 1>&2

    rollback() {
        # Never leave a registered-but-unreported worktree behind -- on install
        # failure or a signal (a hook timeout sends TERM) remove what this
        # invocation just created.
        cd "$PROJECT_DIR" || true
        echo "provision_claude_worktree: $1; rolling back $WT" >&2
        git -C "$PROJECT_DIR" worktree remove --force "$WT" 1>&2 || true
        git -C "$PROJECT_DIR" branch -D "$BRANCH" 1>&2 || true
        exit 1
    }
    trap 'rollback "interrupted"' TERM INT HUP

    if ! provision "$WT" hook; then
        rollback "provisioning failed"
    fi
    trap - TERM INT HUP
    printf '%s\n' "$WT"
    ;;
*)
    echo "provision_claude_worktree: unexpected event '${EVENT:-<none>}' (stdin: $INPUT)" >&2
    exit 1
    ;;
esac
