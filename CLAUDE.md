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
| New `xmsconan_*` console script | `pyproject.toml` scripts + `README.md` CLI section |
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
   `[project.scripts]` **and** mentioned in `README.md`.
4. Call out doc updates (or the explicit absence of doc impact) in the PR
   description.

If a change genuinely has no doc impact, say so explicitly in the PR body —
silence reads as "forgot to check."
