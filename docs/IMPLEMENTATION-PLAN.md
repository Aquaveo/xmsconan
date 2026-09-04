# Implementation plan

| | |
|---|---|
| **Status** | Draft — sequencing proposed, nothing started |
| **Date** | 2026-09-03 |
| **Inputs** | [REVIEW-2026-09-03.md](REVIEW-2026-09-03.md) (R1–R24, S1–S6) and [DESIGN-ci-job-commands.md](DESIGN-ci-job-commands.md) |
| **Anchored to** | xmsconan `8084d9a` |

## How to read this

- Work is grouped into **phases**; each phase is one or more **PRs**. A PR
  is the unit of review and of consumer regeneration — a phase that touches
  a template regenerates the consumers **once**, at the end.
- Every PR runs the full gates before push: `pytest`, flake8 with the
  docstring / bugbear / import-order / naming plugins, and — once Phase 1
  lands — the coverage threshold and pre-commit.
- Every PR follows the documentation-drift policy in `CLAUDE.md`: a
  behavior visible from `build.toml`, the CLIs, the generated CI or the
  recipe updates `README.md` / `docs/USAGE.md` / the jinja header comments in
  the same PR, and the PR body says what was updated or that nothing needed
  to be.
- **Size** is relative, not hours: **S** fits in a sitting, **M** is a day
  or two including tests and docs, **L** spans several days or several PRs.
- Phases 0–4 stand on their own and pay off whether or not Phase 5 happens.
  Phase 5 is the design; Phases 3 and 4 are its prerequisites.

## Dependency sketch

```
Phase 0  operational (S1)                    — no code, do now
   │
Phase 1  foundations (R5 R6 R7 R8)            — CI of xmsconan itself
   │
   ├── Phase 2  secrets hardening (S1–S6)      — one template + one tool PR
   │
   ├── Phase 3  template safety net (R19 R18)  ─┐
   │                                            ├── Phase 5  xmsconan job (design)
   └── Phase 4  CLI consolidation (R11 R12 R13 R16)┘        5.1 → 5.2 → 5.3 → 5.4
                                                  │
Phase 6  architecture (R1 R2 R3 R4)  — independent of 5; R1 easier after 5.2
Phase 7  cleanup (R9 R10 R14 R15 R20–R24)  — anytime, mostly after 5
```

---

## Phase 0 — Operational, before any code

| # | Item | Owner action | Size |
|---|---|---|---|
| 0.1 | **S1** Set the four GitLab variables (`AQUAPI_USERNAME`, `AQUAPI_PASSWORD`, `CONAN_LOGIN_USERNAME_AQUAVEO_VS2019`, `CONAN_PASSWORD_AQUAVEO_VS2019`) **Masked + Protected** in every consuming project. | GitLab settings; the doc half of S1 lands in 2a.4. | S |

Done when: a tag pipeline in one GitLab consumer still deploys with the
flags set.

---

## Phase 1 — Foundations: xmsconan's own CI and dev tooling

One PR. Nothing here changes generated output.

| # | Item | Change | Done when |
|---|---|---|---|
| 1.1 | **R7** Declare dev tooling | `pyproject.toml`: `[dependency-groups] dev = [flake8 + the four plugins, pytest, pytest-cov, pyyaml]`; keep `[project.optional-dependencies] test` for consumers of the extra. Add `.pre-commit-config.yaml` running flake8 on staged files. `.github/workflows/xmsconan-ci.yaml` installs `--group dev` instead of ad-hoc `pip install`s. | `pyyaml` is declared, not inherited from conan; `pre-commit run --all-files` is clean |
| 1.2 | **R5** Python matrix | Workflow matrix `["3.10", "3.13", "3.14"]`. | The `toml` fallback branch (`build_toml.py:14-17`) executes on the 3.10 leg |
| 1.3 | **R6** Coverage gate | `pytest --cov=xmsconan --cov-fail-under=90` in CI; the per-module low spots in the review's health snapshot become the targets for 7.10. | CI fails below 90 %; the current tree passes |
| 1.4 | **R8** `toml` → `tomli` | `tomli; python_version < "3.11"` in dependencies; `credentials.py:56` and `build_toml.py:14-17` share one `_tomllib` import shim. | `toml` no longer appears in `pyproject.toml` or the templates' pip lines (the template line goes in 5.1 if not here) |
| 1.5 | **S6** `AQUAPI_URL` | *Moved to 2b.1.* xmsconan's own workflow hard-codes the index URL in `devpi use`, so nothing there is masked; every `secrets.AQUAPI_URL_DEV` reference is in the generated GitHub templates, which 2b.1 rewrites anyway. | — |

Size: M. Docs: README "Development" section gains the `uv sync --group dev`
and `pre-commit install` lines.

---

## Phase 2 — Secrets hardening

Two PRs: one in the tool, one in the templates. Independent of Phase 5,
and worth doing first because the GitHub template is the *last* thing
Phase 5 rewrites.

### PR 2a — tool side

| # | Item | Change | Test |
|---|---|---|---|
| 2a.1 | **S2** Remove the argv password | `ci_tools/wheel_deploy.py`: replace `devpi use/login/upload` with `uv publish --publish-url <index> wheelhouse/*.whl`, passing `UV_PUBLISH_USERNAME` / `UV_PUBLISH_PASSWORD` in the child environment (same pattern as `conan_setup._login_environment`). Keep the `devpi-client` path behind `--client devpi` for one release, defaulting to `uv`. | A fake `subprocess.run` records argv and env; assert the password appears in env only. `test_main_has_no_password_flag`-style guard on argv |
| 2a.2 | **S4** Allow-list guard | `tests/test_packager.py` ~`:2000`: assert every `[buildenv]` key of every generated configuration ∈ `PUBLIC_BUILDENV_KEYS ∪ {XMS_TEST_ARTIFACTS_LABEL}`. Then delete the `public_only` parameter of `_serialize_profile` (`packager.py:1711`) — it is a no-op once the profile can only contain public keys. | The new test; existing profile tests unchanged |
| 2a.3 | **S5** Docstring | Rewrite `packager.py:1718-1721` to state the post-#125 rule: the ephemeral profile is printed in full, so it may only ever contain public keys. | — (doc) |
| 2a.4 | **S1** docs | USAGE §10.2: next to each variable, "set **Masked** and **Protected**"; one sentence on `CI_DEBUG_TRACE`. USAGE §13: replace the "devpi has no env var" exception with the `uv publish` variables. | `test_usage_documents_*` style drift test if one fits; otherwise the PR checklist |

### PR 2b — template side

| # | Item | Change | Test |
|---|---|---|---|
| 2b.1 | **S3, S6** Step-scope GitHub secrets | `github-ci.yaml.jinja`: delete `CONAN_*` / `AQUAPI_*` from the job-level `env:` blocks (`:119-125`, `:281`, `:431`, `:575`; `github-coverage.yaml.jinja:34-39`). Put `CONAN_LOGIN_USERNAME` / `CONAN_PASSWORD` on the `xmsconan_conan_setup --login` step (`:158`) and `AQUAPI_*` on the wheel-upload step (`:216`). Leave `CONAN_PASSWORD` on the `build.py --upload` step until a tag pipeline confirms conan's persisted token suffices, then remove it. While there, read the index URL from `vars.AQUAPI_URL_DEV` instead of `secrets.` (S6): a public URL masked as `***` hides the `Looking in indexes:` diagnostic without protecting anything. | Golden-file test if Phase 3 has landed; otherwise assertions that no `secrets.` reference appears at job level; `Looking in indexes:` is legible in a consumer's GitHub CI log |
| 2b.2 | **S2** template | Replace the `devpi` install and the `xmsconan_wheel_deploy` invocation's environment with the `UV_PUBLISH_*` names from 2a.1 in both templates. | Same |

Regenerate consumers once after 2b. Size: 2a M, 2b S. Verify on one tag
pipeline per host before declaring S3 closed.

---

## Phase 3 — Template safety net

One PR, before any template is restructured.

| # | Item | Change | Done when |
|---|---|---|---|
| 3.1 | **R19** Golden files | `tests/golden/<ci_type>-<variant>.yml` rendered from canonical `build.toml` fixtures (GitLab default; GitLab with `windows_vs2019` + `coverage` + `test_shards`; GitHub default; GitHub coverage). A `--update-golden` pytest option rewrites them. Failure output is a unified diff. | Every existing template branch is reached by at least one golden; the diff for a template edit is the whole review artifact |
| 3.2 | **R18** `--check` mode | `xmsconan gen --check`, `ci --check`, `profiles --check`: render to memory, compare with the files on disk, exit 1 with a diff and write nothing. | A consumer-CI job can call it; USAGE §4 and §10 document the flag |

Size: M. Docs: USAGE §4 (`--check`), `CONTRIBUTING`-style note in README on
`--update-golden`.

---

## Phase 4 — CLI consolidation (mechanical)

One PR. Pure refactor; a prerequisite for Phase 5 because the `job`
commands need one logging setup and one exit-code vocabulary to build on.

| # | Item | Change |
|---|---|---|
| 4.1 | **R11** `xmsconan/_cli.py` | `add_verbosity_args(parser)`, `configure_logging(args)` (always `force=True`, the variant that works under the dispatcher), `write_text_lf`, `resolve_tool`. Replace the five `_configure_logging`, two `_write_text_lf`, two `_resolve_tool` copies. |
| 4.2 | **R12** Error contract | `xmsconan/exit_codes.py` with the vocabulary in design §3.3; vs2019's `EXIT_NOTHING_BUILT` takes a new value and USAGE §16 says so. `_cli.run_main(fn)`: one-line message by default, traceback under `-v`, `CalledProcessError` → its return code. The three `print(f"Error: {e}")` mains named in R12 adopt it. |
| 4.3 | **R13** `print` → `logging` | `vs2019_build`, `test_shards`, `publish`: prints become `LOGGER.info`; the report tables stay `print`. |
| 4.4 | **R16** Small | `packager.py:2105` TODO → `LOGGER.warning`; `print_ascci_art` → `print_ascii_art` with the old name kept as an alias for one release; `xmsconan --version` in `cli.py`. |

Tests: existing suites cover the mains; add one test per exit code in
`run_main`. Size: M. Docs: USAGE §11.6 and §16 exit-code tables.

---

## Phase 5 — `xmsconan job` (the design)

Four PRs, each regenerating consumers once. GitLab first. Module layout:

```
xmsconan/job_tools/
    __init__.py
    common.py     resolve_ci_version(), log_section(), output paths, env → leg
    xvfb.py       the one Xvfb implementation (moved from publish/test_shards/coverage)
    build.py      job build
    deploy.py     job deploy
    pages.py      job coverage --pages
    cli.py        argparse dispatch; registered as "job" in xmsconan/cli.py COMMANDS
```

`job test`, `job package` and `job coverage --leg/--report` are aliases to
the existing `test_shards`, `wheel_repair` and `coverage_generator` mains;
they are not rewritten.

### PR 5.1 — `[ci]` extra and version from the CI environment

| Change | Test |
|---|---|
| `pyproject.toml`: `[project.optional-dependencies] ci = [conan~=2.31, cmake>=3.21, gcovr>=7,<9, uv, flake8 + plugins]` (the pins currently at `gitlab-ci.yml.jinja:122,135,205,560` and the GitHub equivalents). | Installs in a clean venv; `pip install "xmsconan[ci]"` resolves from the dev index |
| `generator_tools/version.py:resolve_version`: the resolution order in design §3.3, with `GITHUB_REF_NAME` honoured only when `GITHUB_REF_TYPE == tag`. | Parametrized over the five sources with a fake environment |
| Templates: every `pip install conan…` line becomes the one `xmsconan[ci]` line; GitLab drops `export PACKAGE_VERSION=…` in favour of the tool reading `CI_COMMIT_TAG`; GitHub drops the three version/branch actions and the six dead variables (both listed in design §2.1). | Golden diff is all deletions plus one install line per job |

No build behavior changes. Size: M.

### PR 5.2 — `job build` on GitLab (+ `job test`, `job package`, `job lint`)

| Change | Test |
|---|---|
| `job_tools/common.py`: `resolve_leg(env, config)` — `PYTHON_TARGET_VERSION`, `BUILD_TYPE`, `--leg` plus `[filter]` / `[matrix]` and the tag policy → a `BuildFilter`. This is the `gh_build_filter` / `BUILD_MATRIX_FILTER` logic as a function. | Table-driven: every (platform, build type, python, tagged?) combination the templates emit today produces the same filter JSON the golden files contain |
| `job_tools/xvfb.py`: `under_xvfb(argv) -> argv` and a context manager; the three existing implementations (design §2.1) become callers. | Existing Xvfb tests move here |
| `job_tools/build.py`: conan setup (idempotent; `aquaveo-vs2019` appended when `[ci].windows_vs2019`) → `xmsconan_gen` in-process → `XmsConanPackager(...)` from `read_build_toml()` → `run(log_dir="test_artifacts/logs")` → wheel staging → `wheel_repair` when `repairs_windows_wheel()` → `conan_deploy --save .export/<name>.tar.gz` when `[ci].deploy`. Follows the design §3.3 conventions (phase markers, version banner, exit codes). | `PublishSteps`-style fakes (as `publish.py` already has) record the call sequence; one test per branch of the sequence |
| `publish.py`: becomes `job build` + `job deploy` in-process. | Existing `publish` tests keep passing against the fakes |
| `gitlab-ci.yml.jinja`: build jobs' `script:` → two lines; test / repair / lint jobs → `job test` / `job package` / `job lint`; `artifacts:` paths become the fixed layout. | Golden diff |
| Docs: USAGE §10 (what a generated job does now, the fixed output layout), §15 (`publish` = two jobs), new §"Replaying a CI job locally". | Drift checklist |

Size: L. Verify: one GitLab consumer (`xmsgrid` — it has `windows_vs2019`,
`coverage` and `test_shards`) on a branch and on a tag before merging.

### PR 5.3 — `job deploy` and `job coverage --pages`

| Change | Test |
|---|---|
| `job_tools/deploy.py`: glob `.export/*.tar.gz` → `conan_deploy --restore` each → remote and `--package-query` from the platform (`aquaveo` / `compiler.version=194`; `aquaveo-vs2019` / `192`) → `conan_deploy --upload` → wheel upload via 2a.1 → optional `--cache-archive NAME.tar.gz` (`conan cache save`). | Fakes record argv; one test per platform pairing; one for the archive |
| `job_tools/pages.py`: writes the `public/` tree and index that the two `pages` jobs build with inline `echo` today. | Renders a fixture set of `coverage-html-*` dirs; asserts the index links |
| `gitlab-ci.yml.jinja`: deploy jobs → `job deploy`; Windows `cp -r ~/.conan2/p/*` gone; the second `Coverage` / `pages` pair collapses onto the first. | Golden diff; the template loses its duplicated jobs |

Size: M. Verify on a tag in the same consumer as 5.2.

### PR 5.4 — GitHub

| Change | Test |
|---|---|
| `github-ci.yaml.jinja`: the four platform jobs become one step list parametrized by matrix + toolchain action (design §3.4): install → `job build` (secrets on this step) → upload test artifacts → `job deploy --cache-archive` on tags (secrets on this step) → `upload-release-asset`. Windows loses its second build step. `github-coverage.yaml.jinja` → `job coverage --leg/--report`. | Golden diff |
| `.github/workflows/xmsconan-ci.yaml` (xmsconan's own): no change needed beyond Phase 1 — it does not build a library. | — |

Size: M. Verify on one GitHub consumer (`xmscore`) on a branch and a tag.

### Phase 5 exit criteria

- Both templates contain no `pip install` other than the `[ci]` line, no
  `export`, no `xvfb-run`, no `--filter`, no inline HTML.
- `BUILD_TYPE=Release PYTHON_TARGET_VERSION=3.13 xmsconan job build` on a
  workstation produces the same `.export/` tarball name a CI leg does.
- `tests/test_ci_file_generator.py` shrinks; the removed assertions are
  covered by `job_tools` unit tests and the golden files.
- Open questions in the design §7 are each answered in the PR that touches
  them (name in 5.2, pin policy in 5.1, release asset in 5.4, `build.py` in
  5.2, upload client in 2a).

---

## Phase 6 — Architecture

Independent of Phase 5 in code, but R1 is easier once `job build` has
pulled the CI orchestration out of `XmsConanPackager`'s callers.

| # | Item | Shape | Size |
|---|---|---|---|
| 6.1 | **R1** Split `XmsConanPackager` | One PR per seam, each a pure move with the class delegating to the new module: `package_tools/matrix.py` (generation, filtering), `profiles.py` (serialization, presets), `conan_runner.py` (run, sharded tests, upload), `wheels.py` (extraction, dependency libs, Linux repair) — line anchors in R1. Last PR: `__del__` → context manager / `weakref.finalize`. | L (4–5 PRs) |
| 6.2 | **R2** Recipe constants | Step 1: render `xms_conan2_file.py` through jinja in `copy_xms_conan2_file` (`build_file_generator.py:275`) with `SUPPORTED_PYTHON_VERSIONS`, `TESTING_FRAMEWORKS`, `PYTHON_BINDING_TYPES`, `GENERATOR_FOLDER_SUFFIXES`, `MSVC_VS2019_VERSION` injected; delete the four pinning tests. Step 2 (separate decision): publish as a Conan 2 `python_requires`. | M, then L |
| 6.3 | **R3** One credential resolver | `ci_tools/credentials.py` gains `resolve(kind: "conan" \| "aquapi", *, explicit, password_file, env, config_file)` with one documented precedence (explicit → file → env → `~/.xmsconan.toml`). `vs2019_build.resolve_credentials`, `conan_setup._resolve_password`, `wheel_deploy` become callers. USAGE §17 documents the order once. | M |
| 6.4 | **R4** `vs2019_build` data | `LIBRARIES` (`:198`) moves to `xmsconan/data/vs2019_libraries.toml` (or a `--libraries FILE` input); `os.environ["XMS_VERSION"]` (`:1051`) becomes a parameter; `CONAN_PIN` (`:130`) reads the `[ci]` extra's pin. | M |

---

## Phase 7 — Cleanup and hygiene

Any time; each row is its own small PR.

| # | Item | Change | Size |
|---|---|---|---|
| 7.1 | **R9** | Commit `uv.lock`; SHA-pin every Action; add Dependabot for pip and actions. | S |
| 7.2 | **R10** | `workflow_dispatch` + weekly schedule job running `pytest -m integration`. | S |
| 7.3 | **R14** | `pyrightconfig.json` in basic mode over `build_toml`, `build_filter`, `test_shards`, `coverage_generator`, `job_tools`; add to CI; widen a module at a time. | M |
| 7.4 | **R15** | Modernize `build_library.py` or fold it into `publish` / `job build`; its coverage target is in 7.10. | M |
| 7.5 | **R20** | README = install + quickstart + link; delete its `build.toml` table in favour of USAGE §5. | S |
| 7.6 | **R21** | Templates emit `xmsconan <cmd>` (Phase 5 does this for every job it touches); `xmsconan_*` scripts stay as documented aliases. | S |
| 7.7 | **R22** | Prune the 58 profiles to the ones the matrix and `xmsconan_build --profile` users need; USAGE §9.2 lists what remains. | S |
| 7.8 | **R23** | `.gitignore` and `.flake8` exclude lists trimmed to what exists. | S |
| 7.9 | **R24** | Delete `conan1`, `stable`, `pr117`, merged `task/*` and `fix/*`, the `pre-rebase-backup` tag. | S |
| 7.10 | Coverage | `wheel_deploy` (rewritten in 2a.1), `profile_generator` and `build_library` to ≥ 90 %. | S |

---

## Coverage matrix — every finding has a home

| Finding | Phase | Finding | Phase | Finding | Phase |
|---|---|---|---|---|---|
| R1 | 6.1 | R9 | 7.1 | R17 | 5 (all) |
| R2 | 6.2 | R10 | 7.2 | R18 | 3.2 |
| R3 | 6.3 | R11 | 4.1 | R19 | 3.1 |
| R4 | 6.4 | R12 | 4.2 | R20 | 7.5 |
| R5 | 1.2 | R13 | 4.3 | R21 | 7.6 |
| R6 | 1.3 | R14 | 7.3 | R22 | 7.7 |
| R7 | 1.1 | R15 | 7.4 | R23 | 7.8 |
| R8 | 1.4 | R16 | 4.4 | R24 | 7.9 |
| S1 | 0.1 + 2a.4 | S3 | 2b.1 (fully realised in 5.4) | S5 | 2a.3 |
| S2 | 2a.1 + 2b.2 | S4 | 2a.2 | S6 | 2b.1 |

## Definition of done, per PR

1. Gates pass in the PR: pytest (3.10 / 3.13 / 3.14 after 1.2), flake8,
   coverage threshold (after 1.3), golden files (after 3.1).
2. Docs updated per the drift table in `CLAUDE.md`, or the PR body says
   "no doc impact" and why.
3. If a template changed: consumers regenerated once, and the regenerated
   diff in one GitLab and one GitHub consumer reviewed as part of the PR.
4. If a deploy path changed: one tag pipeline observed green on the
   affected host before the PR is marked ready.
5. The review IDs the PR closes are named in its description.
