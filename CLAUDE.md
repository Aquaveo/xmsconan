# Project guidance for Claude

## Documentation drift policy

Treat user-facing docs as part of the public surface. Any change that affects
behavior visible from `build.toml`, the `xmsconan*` CLIs, generated CI files,
or the conanfile contract must update the corresponding documentation in the
**same commit / PR**.

When touching code, check for drift in these locations:

- `README.md` — quick start, supported options, CLI entry points listed in
  `pyproject.toml` `[project.scripts]`.
- `docs/USAGE.md` — full `build.toml` reference, option tables, examples.
- `xmsconan/generator_tools/ci_templates/*.jinja` — embedded comments that
  describe the generated CI.
- Recipe-side docstrings on `XmsConan2File` attributes (e.g.,
  `python_namespaced_dir`, `xms_dependency_options`) — these are the only
  docs downstream library authors see.

### When to update docs (non-exhaustive)

| Change | Update |
| --- | --- |
| New / renamed `build.toml` field | `docs/USAGE.md` option table + `README.md` if user-facing |
| New `xmsconan_*` console script | `pyproject.toml` scripts + `xmsconan/cli.py` `COMMANDS` dispatcher + `README.md` CLI section |
| New section in a generated CI template | jinja header comment + `docs/USAGE.md` CI section |
| New env var consumed by the recipe or tools (e.g., `XMS_COVERAGE`) | `docs/USAGE.md` env var section |
| New required attribute on `XmsConan2File` | the attribute's inline docstring + `docs/USAGE.md` |
| Removed / renamed option, attribute, or CLI flag | every doc that mentioned the old name |

### Pre-merge checklist

Before declaring a feature or fix done:

1. `grep` the old name across `README.md`, `docs/`, and `*.jinja` whenever
   anything is renamed or removed.
2. If a new `build.toml` key was added, confirm it appears in the option
   table in `docs/USAGE.md`.
3. If a new console script was added, confirm it's listed under
   `[project.scripts]`, registered in the `COMMANDS` dict in
   `xmsconan/cli.py`, **and** mentioned in `README.md`.
4. Call out doc updates (or the explicit absence of doc impact) in the PR
   description.

If a change genuinely has no doc impact, say so explicitly in the PR body —
silence reads as "forgot to check."

## Working in a Claude Code worktree

Agent work happens in a worktree, never in the main checkout. `.claude/settings.json`
wires a `WorktreeCreate` hook to `scripts/provision_claude_worktree.sh`, so a worktree
Claude creates arrives with a ready `.venv`: Python 3.13 (the CI matrix version),
`uv pip install -e ".[test]"`, plus the exact flake8 plugin set
`.github/workflows/xmsconan-ci.yaml` installs. `bin/xms-task` in the workspace root
calls the same script for `.tasks/<name>/xmsconan` worktrees.

Provision an existing worktree by hand with:

```bash
sh scripts/provision_claude_worktree.sh provision "$PWD"
```

**The editable install is per-worktree and that is the point.** The workspace rule
against `uv pip install -e ./xmsconan` targets *shared* environments, where it would
put a working copy behind `xmsconan_gen` on PATH and silently change what every repo
generates. Inside a worktree's own `.venv` the opposite is wanted: activate it and
`xmsconan_gen` runs that tree's generator, which is how a generator change gets
exercised against a consumer repo. Never activate a worktree venv and then regenerate
files in a repo you are not testing.

`.venv/` is deliberately absent from `.worktreeinclude` — it holds absolute paths and
an editable install pointing at the tree that built it, so a copied venv would import
the wrong worktree's `xmsconan`. It is always rebuilt, never copied.

Both CI gates run from the worktree venv:

```bash
.venv/bin/flake8 .
.venv/bin/python -m pytest tests/ -v
```

Note that `ultracode` is a Claude Code session setting, not a repo setting — there is
no `.claude/settings.json` key for it. Turn it on per-session with `/config`, or by
including the word in a prompt. This repo's config is what makes the agents it fans
out land in usable worktrees.
