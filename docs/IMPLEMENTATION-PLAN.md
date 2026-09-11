# Implementation plan

| | |
|---|---|
| **Status** | In progress — Phases 1–5 merged and released (xmsconan 2.30.0–2.30.2); Phase 5 exit criterion 1 met, 2–4 open; Phases 6 and 7 open; Phase 0 is an owner action this doc does not track. See [Status](#status). |
| **Date** | 2026-09-03; status reconciled 2026-09-11 |
| **Inputs** | [REVIEW-2026-09-03.md](REVIEW-2026-09-03.md) (R1–R24, S1–S6) and [DESIGN-ci-job-commands.md](DESIGN-ci-job-commands.md) |
| **Anchored to** | xmsconan `8084d9a` (the plan); master `7195bc0` (the status) |

## Status

Reconciled against master `7195bc0` on 2026-09-11. Every commit between the
anchor and master is one of #134–#146, and each row marked Done was checked
against the code on master, not only against the PR title.

| Phase | Status | Landed as |
|---|---|---|
| 0 | Not tracked here — an owner action in GitLab settings, which git does not record | — |
| 1 | Done | #134 `1d6844a` |
| 2a | Done; the `--client devpi` fallback is still in place | #135 `4298061` |
| 2b | Done; S6 as written superseded by #143 | #136 `e10ab49`, #143 `81d5b1e` |
| 3 | Done | #137 `eaa2ca0` |
| 4 | Done; the `print_ascci_art` alias is still in place | #138 `0eef40d` |
| 5.1 | Done | #139 `815a4d4` |
| 5.2 | Done | #140 `94a6158` |
| 5.3 | Done | #141 `e62763b` |
| 5.4 | Done; two parts dropped by decision | #142 `a492ba1`; follow-ups #144 `1078397`, #145 `94664ea` |
| 5, exit | Criterion 1 met; 2–4 open | #146 `7195bc0`; see [Phase 5 exit criteria](#phase-5-exit-criteria) |
| 6 | Not started | — |
| 7 | 7.6 done; 7.1, 7.9 and 7.10 partly done; the rest not started | 7.6: #146 `7195bc0` |

**Releases.** 2.30.0 (`a492ba1`) is the first release carrying any phase;
2.29.2 predates the `xmsconan job` CLI. 2.30.1 (`81d5b1e`) adds #143, and
2.30.2 (`7195bc0`) adds #144–#146.

**Consumer verification**, against 2.30.1, on a branch and on a tag per
host: xmsgrid `9.1.2` on GitHub (15 legs; wheels, Conan packages and release
assets uploaded) and xmsconstraint `6.0.14` on GitLab (all five tag-gated
deploy jobs). Both tag logs show the version coming from the tag —
`Version from GITHUB_REF_NAME: 9.1.2`, `Version from CI_COMMIT_TAG: 6.0.14`
— which is what let 5.1 delete the version and branch actions.

Against 2.30.2, on a branch: xmscore (Aquaveo/xmscore#133). Its workflows
were last generated with 2.23.0, so this was its first run of the
`xmsconan job` pipeline. All 15 XmsCore-CI jobs and the Coverage job
passed on both the push and the pull-request run, Coverage through
`xmsconan conan-setup` and `xmsconan coverage`. No tag-gated step ran;
those last ran on xmsgrid's `9.1.2` tag, against 2.30.1.

### What's left, in order

1. **Close Phase 5** against its [exit criteria](#phase-5-exit-criteria):
   criterion 1 is met (#146, which also did 7.6). Run criterion 2 on a
   workstation, and decide criteria 3 and 4.
2. **Remove the two one-release fallbacks.** Both have shipped in 2.30.0,
   2.30.1 and 2.30.2:
   - 2a.1's `--client devpi` (`ci_tools/wheel_deploy.py:270`). Its own
     docstring calls it the last place xmsconan puts a password on a
     subprocess's argv.
   - 4.4's `print_ascci_art` alias (`package_tools/printer.py:42`).
3. **Phase 6**, starting with 6.1 (R1). It changes no template, so no
   consumer needs regenerating.
4. **Phase 7**, the rows still open.
5. **Phase 0** stays with the project owner.

### Outside the plan

- `docs/USAGE.md:706` and `:1382` say only the tag-time deploy needs
  `CONAN_LOGIN_USERNAME_AQUAVEO_VS2019` / `CONAN_PASSWORD_AQUAVEO_VS2019`.
  #130, closed unmerged on 2026-09-02 with no comment, said the msvc 192
  build needs them too. One of the two is wrong; unresolved.
- #78 (`xmsconan format`) is open and is not part of this plan.
- The generated GitHub workflows use actions that target Node 20:
  `actions/checkout@v4`, `actions/setup-python@v5`,
  `actions/upload-artifact@v4`, and the pinned `ilammy/msvc-dev-cmd` and
  `microsoft/setup-msbuild`. Every leg of Aquaveo/xmscore#133 warns that
  they were forced onto Node 24. 7.1's status covers only xmsconan's own
  workflow.

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

**Status:** open. The flags are GitLab project settings, so git cannot show
whether they are set. The doc half (2a.4) is done.

---

## Phase 1 — Foundations: xmsconan's own CI and dev tooling

**Status:** Done — #134 (`1d6844a`).

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

**Status:** Done — #135 (`4298061`). The `--client devpi` fallback that
2a.1 keeps "for one release" is still in place and is now due for removal
(see [What's left](#whats-left-in-order)).

| # | Item | Change | Test |
|---|---|---|---|
| 2a.1 | **S2** Remove the argv password | `ci_tools/wheel_deploy.py`: replace `devpi use/login/upload` with `uv publish --publish-url <index> wheelhouse/*.whl`, passing `UV_PUBLISH_USERNAME` / `UV_PUBLISH_PASSWORD` in the child environment (same pattern as `conan_setup._login_environment`). Keep the `devpi-client` path behind `--client devpi` for one release, defaulting to `uv`. | A fake `subprocess.run` records argv and env; assert the password appears in env only. `test_main_has_no_password_flag`-style guard on argv |
| 2a.2 | **S4** Allow-list guard | `tests/test_packager.py` ~`:2000`: assert every `[buildenv]` key of every generated configuration ∈ `PUBLIC_BUILDENV_KEYS ∪ {XMS_TEST_ARTIFACTS_LABEL}`. Then delete the `public_only` parameter of `_serialize_profile` (`packager.py:1711`) — it is a no-op once the profile can only contain public keys. | The new test; existing profile tests unchanged |
| 2a.3 | **S5** Docstring | Rewrite `packager.py:1718-1721` to state the post-#125 rule: the ephemeral profile is printed in full, so it may only ever contain public keys. | — (doc) |
| 2a.4 | **S1** docs | USAGE §10.2: next to each variable, "set **Masked** and **Protected**"; one sentence on `CI_DEBUG_TRACE`. USAGE §13: replace the "devpi has no env var" exception with the `uv publish` variables. | `test_usage_documents_*` style drift test if one fits; otherwise the PR checklist |

### PR 2b — template side

**Status:** Done — #136 (`e10ab49`), with two changes since:

- **S6 as written is superseded by #143** (`81d5b1e`). The index URL now
  resolves from the variable `AQUAPI_URL_DEV`, then the secret of the same
  name, then a built-in default. The secret is where maintainers have always
  edited the URL, and reading only the variable left that edit silently
  ignored. While the secret holds the URL the log still shows `***`, so the
  deploy step echoes which of the three supplied it instead.
- The `build.py --upload` step that 2b.1 left `CONAN_PASSWORD` on no longer
  exists; 5.4 replaced it with `job deploy`.

S3 verified on a tag pipeline per host (xmsgrid `9.1.2`, xmsconstraint
`6.0.14`).

| # | Item | Change | Test |
|---|---|---|---|
| 2b.1 | **S3, S6** Step-scope GitHub secrets | `github-ci.yaml.jinja`: delete `CONAN_*` / `AQUAPI_*` from the job-level `env:` blocks (`:119-125`, `:281`, `:431`, `:575`; `github-coverage.yaml.jinja:34-39`). Put `CONAN_LOGIN_USERNAME` / `CONAN_PASSWORD` on the `xmsconan_conan_setup --login` step (`:158`) and `AQUAPI_*` on the wheel-upload step (`:216`). Leave `CONAN_PASSWORD` on the `build.py --upload` step until a tag pipeline confirms conan's persisted token suffices, then remove it. While there, read the index URL from `vars.AQUAPI_URL_DEV` instead of `secrets.` (S6): a public URL masked as `***` hides the `Looking in indexes:` diagnostic without protecting anything. | Golden-file test if Phase 3 has landed; otherwise assertions that no `secrets.` reference appears at job level; `Looking in indexes:` is legible in a consumer's GitHub CI log |
| 2b.2 | **S2** template | Replace the `devpi` install and the `xmsconan_wheel_deploy` invocation's environment with the `UV_PUBLISH_*` names from 2a.1 in both templates. | Same |

Regenerate consumers once after 2b. Size: 2a M, 2b S. Verify on one tag
pipeline per host before declaring S3 closed.

---

## Phase 3 — Template safety net

**Status:** Done — #137 (`eaa2ca0`).

One PR, before any template is restructured.

| # | Item | Change | Done when |
|---|---|---|---|
| 3.1 | **R19** Golden files | `tests/golden/<ci_type>-<variant>.yml` rendered from canonical `build.toml` fixtures (GitLab default; GitLab with `windows_vs2019` + `coverage` + `test_shards`; GitHub default; GitHub coverage). A `--update-golden` pytest option rewrites them. Failure output is a unified diff. | Every existing template branch is reached by at least one golden; the diff for a template edit is the whole review artifact |
| 3.2 | **R18** `--check` mode | `xmsconan gen --check`, `ci --check`, `profiles --check`: render to memory, compare with the files on disk, exit 1 with a diff and write nothing. | A consumer-CI job can call it; USAGE §4 and §10 document the flag |

Size: M. Docs: USAGE §4 (`--check`), `CONTRIBUTING`-style note in README on
`--update-golden`.

---

## Phase 4 — CLI consolidation (mechanical)

**Status:** Done — #138 (`0eef40d`). The `print_ascci_art` alias that 4.4
keeps "for one release" is still in place (`package_tools/printer.py:42`)
and is now due for removal.

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

**As built:** the module layout landed as drawn. `job coverage --leg/--report`
was dropped by decision — no new CLI surface for coverage — so the coverage
jobs run `xmsconan coverage` directly, as the `job_tools/cli.py` docstring
says (#146 respelled them from `xmsconan_coverage`). `job coverage --pages`
exists as 5.3 describes.

### PR 5.1 — `[ci]` extra and version from the CI environment

**Status:** Done — #139 (`815a4d4`).

| Change | Test |
|---|---|
| `pyproject.toml`: `[project.optional-dependencies] ci = [conan~=2.31, cmake>=3.21, gcovr>=7,<9, uv, flake8 + plugins]` (the pins currently at `gitlab-ci.yml.jinja:122,135,205,560` and the GitHub equivalents). | Installs in a clean venv; `pip install "xmsconan[ci]"` resolves from the dev index |
| `generator_tools/version.py:resolve_version`: the resolution order in design §3.3, with `GITHUB_REF_NAME` honoured only when `GITHUB_REF_TYPE == tag`. | Parametrized over the five sources with a fake environment |
| Templates: every `pip install conan…` line becomes the one `xmsconan[ci]` line; GitLab drops `export PACKAGE_VERSION=…` in favour of the tool reading `CI_COMMIT_TAG`; GitHub drops the three version/branch actions and the six dead variables (both listed in design §2.1). | Golden diff is all deletions plus one install line per job |

No build behavior changes. Size: M.

### PR 5.2 — `job build` on GitLab (+ `job test`, `job package`, `job lint`)

**Status:** Done — #140 (`94a6158`).

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

xmsgrid is a GitHub consumer, so that line could not be followed as
written. The GitLab side was verified on xmsconstraint, which has
`windows_vs2019`, against 2.30.x on a branch and on tag `6.0.14`.

### PR 5.3 — `job deploy` and `job coverage --pages`

**Status:** Done — #141 (`e62763b`). Verified on xmsconstraint's tag
`6.0.14`: all five tag-gated deploy jobs green.

| Change | Test |
|---|---|
| `job_tools/deploy.py`: glob `.export/*.tar.gz` → `conan_deploy --restore` each → remote and `--package-query` from the platform (`aquaveo` / `compiler.version=194`; `aquaveo-vs2019` / `192`) → `conan_deploy --upload` → wheel upload via 2a.1 → optional `--cache-archive NAME.tar.gz` (`conan cache save`). | Fakes record argv; one test per platform pairing; one for the archive |
| `job_tools/pages.py`: writes the `public/` tree and index that the two `pages` jobs build with inline `echo` today. | Renders a fixture set of `coverage-html-*` dirs; asserts the index links |
| `gitlab-ci.yml.jinja`: deploy jobs → `job deploy`; Windows `cp -r ~/.conan2/p/*` gone; the second `Coverage` / `pages` pair collapses onto the first. | Golden diff; the template loses its duplicated jobs |

Size: M. Verify on a tag in the same consumer as 5.2.

### PR 5.4 — GitHub

**Status:** Done — #142 (`a492ba1`); follow-ups #144 (`1078397`, the
secret-holding step table keyed per job rather than per workflow) and #145
(`94664ea`). Two parts dropped by decision: `github-coverage.yaml.jinja` →
`job coverage --leg/--report` (see *As built* above), and the mac job's
`--remove-conancenter`, so the four platform jobs stay one identical step
list. Verified on xmsgrid rather than xmscore: tag `9.1.2`, 15 legs, 14
release assets. xmscore then ran it on a branch against 2.30.2
(Aquaveo/xmscore#133): 15 jobs and coverage green.

| Change | Test |
|---|---|
| `github-ci.yaml.jinja`: the four platform jobs become one step list parametrized by matrix + toolchain action (design §3.4): install → `job build` (secrets on this step) → upload test artifacts → `job deploy --cache-archive` on tags (secrets on this step) → `upload-release-asset`. Windows loses its second build step. `github-coverage.yaml.jinja` → `job coverage --leg/--report`. | Golden diff |
| `.github/workflows/xmsconan-ci.yaml` (xmsconan's own): no change needed beyond Phase 1 — it does not build a library. | — |

Size: M. Verify on one GitHub consumer (`xmscore`) on a branch and a tag.

### Phase 5 exit criteria

| Criterion | Status (2026-09-11) |
|---|---|
| Both templates contain no `pip install` other than the `[ci]` line, no `export`, no `xvfb-run`, no `--filter`, no inline HTML. | **Met** — #146 (`7195bc0`) removed the three `pip install --upgrade pip` lines. No `xvfb-run`, no inline HTML, and `--filter` survives only in comments. Two recorded exceptions, each explained by a comment in the template: `export CTEST_PARALLEL_LEVEL` in the instrumented coverage jobs (`gitlab-ci.yml.jinja:144`, `:895`), which follows from dropping `job coverage --leg/--report`; and `pip install … flake8-aquaveo` in the GitLab lint job (`:552`), kept out of `[ci]` so the GitHub flake job does not gain the AQU rules. Revisit that one if the two hosts should lint alike. |
| `BUILD_TYPE=Release PYTHON_TARGET_VERSION=3.13 xmsconan job build` on a workstation produces the same `.export/` tarball name a CI leg does. | **Not run.** `export_tarball_name` (`job_tools/build.py`) builds the name from `build.toml`, `PYTHON_TARGET_VERSION` and the platform, so only the version segment can differ. |
| `tests/test_ci_file_generator.py` shrinks; the removed assertions are covered by `job_tools` unit tests and the golden files. | **Not met.** 3300 lines at `8084d9a`, 3905 on master. Decide whether to move the assertions the golden files already pin, or retire the criterion. |
| Open questions in the design §7 are each answered in the PR that touches them (name in 5.2, pin policy in 5.1, release asset in 5.4, `build.py` in 5.2, upload client in 2a). | **4 of 5.** Name: `xmsconan job`. Pin policy: `>=X,<X+1`. Release asset: stays on `upload-release-asset`. Upload client: `uv publish`. `build.py` is still open — the design says keep it for one release cycle, then decide — and `build.py.jinja` still ships. |

---

## Phase 6 — Architecture

Independent of Phase 5 in code, but R1 is easier once `job build` has
pulled the CI orchestration out of `XmsConanPackager`'s callers.

**Status:** not started. `packager.py` is 2259 lines, `package_tools/`
still holds only `__init__.py`, `packager.py` and `printer.py`, and
`__del__` is at `packager.py:736`. `copy_xms_conan2_file` still copies the
recipe base verbatim with `shutil.copy2`. `credentials.py` has no
`resolve`. Line anchors in the table are refreshed to master `94664ea`.

| # | Item | Shape | Size |
|---|---|---|---|
| 6.1 | **R1** Split `XmsConanPackager` | One PR per seam, each a pure move with the class delegating to the new module: `package_tools/matrix.py` (generation, filtering), `profiles.py` (serialization, presets), `conan_runner.py` (run, sharded tests, upload), `wheels.py` (extraction, dependency libs, Linux repair) — line anchors in R1. Last PR: `__del__` → context manager / `weakref.finalize`. | L (4–5 PRs) |
| 6.2 | **R2** Recipe constants | Step 1: render `xms_conan2_file.py` through jinja in `copy_xms_conan2_file` (`build_file_generator.py:288`) with `SUPPORTED_PYTHON_VERSIONS`, `TESTING_FRAMEWORKS`, `PYTHON_BINDING_TYPES`, `GENERATOR_FOLDER_SUFFIXES`, `MSVC_VS2019_VERSION` injected; delete the four pinning tests. Step 2 (separate decision): publish as a Conan 2 `python_requires`. | M, then L |
| 6.3 | **R3** One credential resolver | `ci_tools/credentials.py` gains `resolve(kind: "conan" \| "aquapi", *, explicit, password_file, env, config_file)` with one documented precedence (explicit → file → env → `~/.xmsconan.toml`). `vs2019_build.resolve_credentials` (`:324`), `conan_setup._resolve_password` (`:122`), `wheel_deploy` become callers. USAGE §17 documents the order once. | M |
| 6.4 | **R4** `vs2019_build` data | `LIBRARIES` (`:201`) moves to `xmsconan/data/vs2019_libraries.toml` (or a `--libraries FILE` input); `os.environ["XMS_VERSION"]` (`:1054`) becomes a parameter; `CONAN_PIN` (`:138`) reads the `[ci]` extra's pin. | M |

---

## Phase 7 — Cleanup and hygiene

Any time; each row is its own small PR.

| # | Item | Change | Size | Status (2026-09-11) |
|---|---|---|---|---|
| 7.1 | **R9** | Commit `uv.lock`; SHA-pin every Action; add Dependabot for pip and actions. | S | Partly. In `.github/workflows/xmsconan-ci.yaml`, `setup-uv` and `get-git-tag` are SHA-pinned; `actions/checkout@v4` and `actions/setup-python@v5` are not. No `uv.lock`, no Dependabot. Every job of the 2.30.2 run warns that `checkout@v4` and `setup-python@v5` — and, in `publish`, `get-git-tag` — target Node 20 and were forced onto Node 24; `publish` also warns that it uses the deprecated `set-output` command, most likely from `get-git-tag`, which `GITHUB_REF_NAME` could replace as 5.1 did in the generated workflows. |
| 7.2 | **R10** | `workflow_dispatch` + weekly schedule job running `pytest -m integration`. | S | Not started. |
| 7.3 | **R14** | `pyrightconfig.json` in basic mode over `build_toml`, `build_filter`, `test_shards`, `coverage_generator`, `job_tools`; add to CI; widen a module at a time. | M | Not started. |
| 7.4 | **R15** | Modernize `build_library.py` or fold it into `publish` / `job build`; its coverage target is in 7.10. | M | Not started. |
| 7.5 | **R20** | README = install + quickstart + link; delete its `build.toml` table in favour of USAGE §5. | S | Not started; the README still carries the `build.toml` schema tables. |
| 7.6 | **R21** | Templates emit `xmsconan <cmd>` (Phase 5 does this for every job it touches); `xmsconan_*` scripts stay as documented aliases. | S | Done — #146 (`7195bc0`). No template calls an `xmsconan_*` script: the coverage jobs call `xmsconan conan-setup` and `xmsconan coverage`, and every other job `xmsconan job`. `tests/test_ci_commands.py` fails a generated call to a subcommand or job kind the dispatcher does not register. The `xmsconan_*` scripts stay registered in `pyproject.toml`. |
| 7.7 | **R22** | Prune the 58 profiles to the ones the matrix and `xmsconan_build --profile` users need; USAGE §9.2 lists what remains. | S | Not started; still 58 profiles. |
| 7.8 | **R23** | `.gitignore` and `.flake8` exclude lists trimmed to what exists. | S | Not started; neither file has changed since `8084d9a`. |
| 7.9 | **R24** | Delete `conan1`, `stable`, `pr117`, merged `task/*` and `fix/*`, the `pre-rebase-backup` tag. | S | Partly. `pr117` and `pre-rebase-backup` are gone. Still on the remote: `conan1`, `stable`, `fix/conan-deploy-save-binaries` (merged as #92), `fix/upload-package-pattern` (#132, still open, but its change landed as `8084d9a`; close it), `fix/vs2019-credentials-doc` (#130, closed unmerged). Keep `fix/xvfb-repair-wheel-interpreter`: #99 left it in place for #100, still open. The `cp313-cp313` hardcode #100 describes is still at `gitlab-ci.yml.jinja:591`, though the branch predates Phase 5's rewrite of those lines. Not named in this row, and each carries commits master lacks, so check before deleting: `conan2-vtk` (1 commit, 2025-01-29), `feature/build-toml-filter-rebased` (3, 2026-08-26), `limit_numpy` (1, 2024-08-30), and `format/format-code` (#78, open). |
| 7.10 | Coverage | `wheel_deploy` (rewritten in 2a.1), `profile_generator` and `build_library` to ≥ 90 %. | S | Partly. `wheel_deploy` is at 100 % and `profile_generator` at 98 %. `build_library` is at 79 %; settle 7.4 first, since folding it away removes the target. |

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
