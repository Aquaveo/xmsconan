#!/bin/sh
# Create and provision Claude Code ephemeral worktrees (.claude/worktrees/).
#
# Wired to the WorktreeCreate hook in .claude/settings.json. A configured
# WorktreeCreate hook OWNS worktree creation: it must create the worktree
# itself and print its path to stdout (only the path may go to stdout; all
# diagnostics go to stderr). Removal stays with Claude Code's built-in logic.
#
# Because the hook replaces Claude Code's own worktree creation, Claude Code
# does NOT process .worktreeinclude here -- this script does it instead, using
# the same two-condition rule: a path is copied only if it matches a pattern
# AND is gitignored. Tracked files arrive with the checkout already.
#
# Manual provisioning of an existing worktree:
#   sh .claude/provision_worktree.sh provision /path/to/worktree
set -eu

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

json_field() {
    # $1 = JSON document, $2 = top-level field name
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$1" | jq -r ".$2 // empty"
    else
        printf '%s' "$1" | python3 -c \
            "import json,sys; print(json.load(sys.stdin).get('$2') or '')"
    fi
}

copy_worktree_includes() {
    # $1 = destination worktree (absolute). Mirrors Claude Code's
    # .worktreeinclude rule: copy a path only when it matches a listed pattern
    # AND git ignores it. Every fallible step returns non-zero -- `set -e` does
    # not apply here, because the caller tests this function's status.
    [ -f "$PROJECT_DIR/.worktreeinclude" ] || return 0
    COPIED=0
    while IFS= read -r PATTERN || [ -n "$PATTERN" ]; do
        case "$PATTERN" in '' | \#*) continue ;; esac
        PATTERN="${PATTERN%/}"
        # Expand relative to the project dir without leaking the glob when it
        # matches nothing.
        for SRC in "$PROJECT_DIR"/$PATTERN; do
            [ -e "$SRC" ] || continue
            REL="${SRC#"$PROJECT_DIR"/}"
            # exit 1 is "not ignored" (skip); anything else is fatal (128 =
            # bad pathspec, unreadable repo) and must not look like a skip.
            if ! git -C "$PROJECT_DIR" check-ignore -q "$REL"; then
                STATUS=$?
                if [ "$STATUS" -ne 1 ]; then
                    echo "provision_worktree: git check-ignore failed (exit $STATUS) for $REL" >&2
                    return 1
                fi
                continue
            fi
            if [ -d "$SRC" ]; then
                # Copy the contents, not the directory: `cp -R dir dest` nests
                # as dest/dir when dest already exists (re-provisioning).
                mkdir -p "$1/$REL" || return 1
                cp -R "$SRC/." "$1/$REL/" || return 1
            else
                mkdir -p "$1/$(dirname "$REL")" || return 1
                cp -R "$SRC" "$1/$REL" || return 1
            fi
            COPIED=$((COPIED + 1))
        done
    done < "$PROJECT_DIR/.worktreeinclude"
    [ "$COPIED" -eq 0 ] || echo "provision_worktree: copied $COPIED .worktreeinclude path(s)" >&2
    return 0
}

provision() {
    # $1 = worktree path, $2 = mode ("hook" or "manual"). In hook mode a missing
    # toolchain soft-skips (returns 0) so a provisioning nicety never blocks
    # worktree creation; in manual mode the user asked for it, so it fails.
    # A real generation/sync failure always returns non-zero.
    MODE="${2:-hook}"
    SKIP=0
    [ "$MODE" = "hook" ] || SKIP=1
    # Canonicalize before the cd: $1 is also used as a destination prefix, and a
    # relative one would resolve against the new cwd and copy into the wrong tree.
    cd "$1" || return 1
    WT_DIR="$PWD"
    copy_worktree_includes "$WT_DIR" || return 1

    # Independent checks, not elif: a repo carrying both files needs both.
    RC=0
    if [ -f build.toml ]; then
        # CMakeLists.txt, conanfile.py, build.py, .flake8, conan_profiles/ and
        # CMakePresets.json are generated and gitignored, so a fresh worktree
        # has none of them and cannot configure or build until this runs.
        if command -v xmsconan_gen >/dev/null 2>&1; then
            xmsconan_gen build.toml 1>&2 || return 1
            echo "provision_worktree: build files generated in $WT_DIR" >&2
        else
            echo "provision_worktree: xmsconan_gen not on PATH; skipping build-file generation." >&2
            echo 'Install it, then run: xmsconan_gen build.toml' >&2
            RC=$SKIP
        fi
    fi
    if [ -f pyproject.toml ]; then
        if command -v uv >/dev/null 2>&1; then
            # Never inherit another worktree's venv.
            unset VIRTUAL_ENV VIRTUAL_ENV_PROMPT
            # --all-extras, not plain sync: test deps live in
            # [project.optional-dependencies], so a plain sync yields a venv
            # that cannot run the repo's own test suite. --all-extras rather
            # than --extra test keeps this script repo-agnostic.
            if [ -f uv.lock ]; then
                uv sync --frozen --all-extras 1>&2 || return 1
            else
                uv sync --all-extras 1>&2 || return 1
            fi
            echo "provision_worktree: .venv ready in $WT_DIR" >&2
        else
            echo "provision_worktree: uv not on PATH; skipping venv setup." >&2
            echo 'Install uv, then run: uv sync --all-extras' >&2
            RC=$SKIP
        fi
    fi
    return $RC
}

if [ "${1:-}" = "provision" ]; then
    provision "${2:?usage: provision_worktree.sh provision <path>}" manual || exit 1
    exit 0
fi

INPUT="$(cat)"
EVENT="$(json_field "$INPUT" hook_event_name)"

case "$EVENT" in
WorktreeCreate)
    NAME="$(json_field "$INPUT" name)"
    # Allow only safe path characters, then let git validate ref legality
    # (leading dot, trailing slash/dot, .lock suffix, etc.); anything rejected
    # gets a generated name.
    case "$NAME" in
    '' | -* | *..* | *[!A-Za-z0-9./_-]*)
        echo "provision_worktree: unsafe worktree name '${NAME:-<empty>}'; using wt-$$" >&2
        NAME="wt-$$"
        ;;
    esac
    if ! git check-ref-format --branch "$NAME" >/dev/null 2>&1; then
        echo "provision_worktree: '$NAME' is not a valid branch name; using wt-$$" >&2
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
    rollback() {
        # Never leave a registered-but-unreported worktree behind -- on failure
        # or a signal (hook timeout sends TERM) remove what this run created.
        # Each step reports its own failure: a rollback that cannot finish must
        # say so, or the leftover worktree is exactly what the trap prevents.
        cd "$PROJECT_DIR" ||
            echo "provision_worktree: rollback could not cd to $PROJECT_DIR" >&2
        echo "provision_worktree: $1; rolling back $WT" >&2
        git -C "$PROJECT_DIR" worktree remove --force "$WT" 1>&2 ||
            echo "provision_worktree: rollback could not remove worktree $WT; remove it by hand" >&2
        git -C "$PROJECT_DIR" branch -D "$BRANCH" 1>&2 ||
            echo "provision_worktree: rollback could not delete branch $BRANCH; delete it by hand" >&2
        exit 1
    }

    mkdir -p "$(dirname "$WT")"
    git -C "$PROJECT_DIR" worktree add -b "$BRANCH" "$WT" HEAD 1>&2
    # Armed on the next line after the add: from here on the worktree exists,
    # so every exit path has to be able to undo it.
    trap 'rollback "interrupted"' TERM INT HUP

    if ! provision "$WT" hook; then
        rollback "provisioning failed"
    fi
    trap - TERM INT HUP
    printf '%s\n' "$WT"
    ;;
*)
    echo "provision_worktree: unexpected event '${EVENT:-<none>}' (stdin: $INPUT)" >&2
    exit 1
    ;;
esac
