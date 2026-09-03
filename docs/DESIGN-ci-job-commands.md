# Design: `xmsconan job` — CI jobs as tool calls

| | |
|---|---|
| **Status** | Proposed |
| **Date** | 2026-09-03 |
| **Anchored to** | xmsconan `8084d9a` — every `file:line` below is against that commit |
| **Scope** | The generated CI (`xmsconan ci`), `ci_tools/publish.py`, the generated `build.py`, packaging |
| **Related** | [REVIEW-2026-09-03.md](REVIEW-2026-09-03.md), [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) |

## 1. Summary

Restructure the generated CI so every job has the same three-part shape:

1. **setup** — checkout, interpreter, platform toolchain, `pip install "xmsconan[ci]"`;
2. **work** — one `xmsconan job <kind>` call;
3. **cleanup** — CI-native artifact and report declarations.

Everything between (1) and (3) that is currently shell or Jinja logic inside
`gitlab-ci.yml.jinja` (1,098 lines) and `github-ci.yaml.jinja` (717 lines)
moves into xmsconan as ordinary, unit-testable Python.

`xmsconan publish` already embodies the whole sequence for a workstation —
conan setup → gen → build → repair → deploy (`ci_tools/publish.py:118-212`).
CI cannot call it, because CI has to *split* that sequence across jobs and
stages: build on a branch, deploy on a tag, artifacts in between. So the
design is not "call `publish` from CI"; it is a small family of job-shaped
commands that both `publish` and the templates become thin wrappers over.

## 2. Problem

### 2.1 Where the work lives today

Both templates, with comment blocks stripped, sort into three buckets.

**Setup that legitimately belongs to CI:** checkout, `setup-python` /
`uv venv`, the MSVC and Xcode actions, image and runner selection, secrets.

**Work that is already an xmsconan call:** `xmsconan_conan_setup`,
`xmsconan_gen`, `xmsconan_conan_deploy --save/--restore/--upload`,
`xmsconan_wheel_repair`, `xmsconan_wheel_deploy`, `xmsconan_test_shards`,
`xmsconan_coverage --phase …`. The GitLab template is mostly this.

**Work that is shell or Jinja logic living in the template** — the part
this design moves:

| Today | Where |
|---|---|
| Version resolution: `export PACKAGE_VERSION=${CI_COMMIT_TAG:-0.0.0}` in every GitLab job; on GitHub three third-party actions (`little-core-labs/get-git-tag`, `allenevans/set-env`, `nelonoel/branch-name`) juggling `XMS_VERSION` / `CONAN_REFERENCE` / `RELEASE_PYTHON` | both |
| Matrix-leg selection: the `gh_build_filter` Jinja expression escaping JSON into a shell string; `BUILD_MATRIX_FILTER` swapped by GitLab `rules:` on tags; `--filter '<< job.filter_json >>'`; on Windows/GitHub *two* build steps (`if: !tags` / `if: tags`) because the filter differs on a release | both |
| Flags derived from `build.toml` but rendered into YAML: `--wheel-dir`, `--skip-dependency-libs` (from `ci_options.repairs_windows_wheel`), `--artifacts-dir`, `--test-shards N`, `--platform windows_vs2019 --build-missing`, `--package-query compiler.version=19x`, `--remote aquaveo-vs2019`, `--xvfb` | both |
| `xvfb-run -a -s "-screen 0 1280x1024x24"` prefix — the fourth Xvfb implementation, beside `ci_tools/publish.py:61-85`, `ci_tools/test_shards.py:84-197` and `coverage_tools/coverage_generator.py:140` | GitLab |
| Tool pins: `conan~=2.31.0`, `cmake>=3.21`, `gcovr>=7,<9`, `devpi-client`, `toml packaging`, the flake8 plugin list — about fifteen `pip install` lines | both |
| Post-work: `conan cache save … :*` + `bruceadams/get-release` + `actions/upload-release-asset` (GitHub); `cp -r ~/.conan2/p/* conan_packages/` (GitLab Windows); the `pages` job's inline HTML index, present twice | both |
| Dead environment: `CONAN_CHANNEL`, `CONAN_ARCHS`, `CONAN_UPLOAD`, `CONAN_STABLE_BRANCH_PATTERN`, `CONAN_REFERENCE`, `CONAN_USERNAME` — set in every GitHub job (`github-ci.yaml.jinja:114-118` and three repeats), read by nothing in the package. Conan 1 `conan-package-tools` relics | GitHub |

### 2.2 What that costs

- **Every behavior change is a template change.** A fix to the Xvfb prefix or
  the tag-time filter is: edit the template → release xmsconan →
  `xmsconan ci` in each consumer → commit `.gitlab-ci.yml` /
  `.github/workflows/*.yml` in roughly ten repositories.
- **The logic is tested through rendered YAML.** `tests/test_ci_file_generator.py`
  is 3,300 lines of assertions on shell strings. Filter composition, tag
  policy, remote selection and the Xvfb decision are tested by looking for
  the substring the template was expected to emit.
- **The two platforms have diverged.** GitHub carries four near-identical
  platform jobs of 17–21 steps each; GitLab carries two complete `Coverage`
  jobs and two `pages` jobs on different Jinja branches.
- **A job cannot be replayed locally.** The version, the filter and the
  flags are assembled by the CI runner, not by anything a developer can run.
- **Secrets are job-scoped on GitHub** (`github-ci.yaml.jinja:119-125`)
  because the steps that need them are interleaved with steps that do not.
  Every step — including `conan create`, which `pip install`s from PyPI and
  runs the library's own tests — inherits `AQUAPI_PASSWORD` and
  `CONAN_PASSWORD`.

## 3. Proposal

### 3.1 Packaging: the `xmsconan[ci]` extra

One optional-dependency group, beside the existing `test` extra in
`pyproject.toml`, pins everything the templates install by hand today (the
tool-pin row in §2.1). A job's setup becomes one line:

```
pip install "xmsconan[ci]>=X" -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple
```

and the pins live in `pyproject.toml`, where a bump is one edit and one
release rather than a template edit and a regenerate everywhere.

### 3.2 The commands

`xmsconan job <kind>` — `ci` is already the generator, so the verb is not
`ci`; the exact name is bikeshed (see §7).

| Command | Reads | Does | Writes |
|---|---|---|---|
| `job build` | `build.toml`; `PYTHON_TARGET_VERSION`, `BUILD_TYPE`, `--leg <name>`; `CI_COMMIT_TAG` / `GITHUB_REF_NAME` / `--version`; conan and devpi credentials from the environment | Resolves the version; runs conan setup itself (remotes, login when credentials are present, `aquaveo-vs2019` appended when `[ci].windows_vs2019`); regenerates; composes the leg's filter from the env plus `[filter]` / `[matrix]` and the tag policy (`testing=false` on a release); wraps the build in the one shared Xvfb implementation; drives `XmsConanPackager` **in-process**; stages the wheel; repairs it when `repairs_windows_wheel()` says so; saves the cache tarball when deploy is on | `.export/<lib>-<platform>-<leg>-<version>.tar.gz`, `wheelhouse/`, `test_artifacts/<label>/`, per-configuration logs under `test_artifacts/` |
| `job test --label X` | `test_artifacts/` from `job build`; `[ci].test_shards`, `[ci].xvfb` | Exactly what `xmsconan_test_shards` does today — it is already this shape | `TEST-cxxtest.xml`, `test_artifacts/<label>/` |
| `job package` | `wheelhouse/` | Exactly what `xmsconan_wheel_repair` does today, in the manylinux image | `wheelhouse/` (repaired) |
| `job deploy` | `.export/*.tar.gz` (all of them, globbed); `wheelhouse/`; platform; credentials from the environment | Restores every tarball it finds; picks remote and package query from the platform (`aquaveo` + `compiler.version=194`, `aquaveo-vs2019` + `192`); uploads conan and wheel; `--cache-archive NAME.tar.gz` writes the GitHub release asset | the archive, when asked |
| `job coverage --leg cpp\|python` / `--report` / `--pages` | `build.toml` `[coverage]`; tracefiles from the measure legs | `--leg` and `--report` are today's `xmsconan_coverage --phase measure/report`; `--pages` writes the `public/` tree and index that the `pages` job currently builds with inline `echo` | `cov-*.json/xml`, `coverage-html-*/`, `public/` |
| `job lint` | `build.toml` | `xmsconan_gen` then `flake8 _package`; the plugin list comes from the `[ci]` extra | — |

### 3.3 Conventions every `job` command follows

- **Version resolution order:** `--version` → `CI_COMMIT_TAG` →
  `GITHUB_REF_NAME` when the ref is a tag →
  `generator_tools/version.py:resolve_version` (setuptools-scm) → `0.0.0`.
  The three GitHub actions that exist to compute this go away.
- **Conan setup is implicit and idempotent.** No template ever calls
  `xmsconan_conan_setup` again; each command adds the remotes it needs and
  logs in when credentials are in the environment.
- **Fixed output layout.** `.export/`, `wheelhouse/`, `test_artifacts/`,
  `cov-*`, `public/` — documented, never configurable from the template. This
  is what lets the template's `artifacts:` block be static.
- **Log sections.** When `GITLAB_CI` or `GITHUB_ACTIONS` is set the command
  emits `section_start` / `::group::` markers around each phase, and keeps the
  `==>` banners `publish` prints today. `XmsConanPackager.run(log_dir=…)`
  (`package_tools/packager.py:1231`) already writes a log per configuration;
  those land in `test_artifacts/` so a failing leg is readable after the job.
- **Prints its own version first.** The generated file pins
  `xmsconan[ci]>=X`; the first line of every job says which X it got.
- **Exit codes** come from one shared module: 0 ok, 1 error, 2 usage,
  3 gate failed (the coverage gate keeps its current code so
  `allow_failure: exit_codes:` keeps working).
- **Credentials come from the environment, never argv.** This is the rule
  `conan_setup._login_environment` and `docker_run._build_env_flags` already
  state; `job deploy` uploads wheels through a client that reads a password
  variable (`uv publish` with `UV_PUBLISH_PASSWORD`, or twine) so the one
  remaining `devpi login --password` goes away with it.

### 3.4 What a generated job looks like

GitLab, one build leg:

```yaml
"GCC-13 (Release, 3.13)":
  stage: Build
  image: docker.aquaveo.com/aquaveo/conan-docker/conan-gcc13-py3.13
  needs: []
  variables: { PYTHON_TARGET_VERSION: "3.13", BUILD_TYPE: Release }
  script:
    - pip install "xmsconan[ci]>=X" -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple
    - xmsconan job build
  artifacts:
    when: always
    expire_in: 1d
    paths: [.export/, wheelhouse/, test_artifacts/]
```

GitHub, one platform job (the build and deploy halves in one job, as today):

```yaml
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ matrix.python-version }} }
      - run: pip install "xmsconan[ci]>=X" -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple
      - run: xmsconan job build
        env:
          BUILD_TYPE: ${{ matrix.build_type }}
          CONAN_LOGIN_USERNAME: ${{ secrets.CONAN2_USER_SECRET }}
          CONAN_PASSWORD: ${{ secrets.CONAN2_PASSWORD_SECRET }}
      - uses: actions/upload-artifact@v4
        if: always()
        with: { name: test-artifacts-${{ env.MATRIX_NAME }}, path: test_artifacts/ }
      - run: xmsconan job deploy --cache-archive ${{ env.MATRIX_NAME }}.tar.gz
        if: startsWith(github.ref, 'refs/tags/')
        env:
          AQUAPI_USERNAME: ${{ secrets.AQUAPI_USERNAME_SECRET }}
          AQUAPI_PASSWORD: ${{ secrets.AQUAPI_PASSWORD_SECRET }}
          CONAN_PASSWORD: ${{ secrets.CONAN2_PASSWORD_SECRET }}
      - uses: actions/upload-release-asset@v1
        if: startsWith(github.ref, 'refs/tags/')
        …
```

Secrets are now on the two steps that use them, and the three third-party
version/branch actions plus the six dead environment variables are gone.

### 3.5 What stays in the template

The things a tool cannot do and should not pretend to: the stage and job
graph, `needs` / `dependencies`, `parallel: matrix`, `image` / `runs-on` /
`tags`, `only` / `except` / `rules`, `allow_failure: exit_codes`, the
`coverage:` regex, `reports: junit` / `cobertura`, artifact declarations,
secrets wiring, checkout, the toolchain actions, release-asset upload.

Every one of those is declarative. After the change the templates contain
declarations and two-line `script:` blocks, and the four GitHub platform
jobs differ only in their matrix and toolchain action.

### 3.6 `publish` and `build.py` afterwards

- `publish` stays as the workstation entry point (USAGE §15) and shrinks to
  `job build` + `job deploy`.
- The generated `build.py` (`generator_tools/templates/build.py.jinja`) stays
  for local developers (USAGE §9) but CI no longer imports it: `job build`
  constructs `XmsConanPackager` from `read_build_toml()` directly, so the CI
  path no longer depends on a generated file being current.

## 4. Benefits

1. **Behavior changes stop requiring a regenerate-and-commit in every
   consumer.** With the logic in the tool, a fix is a version bump.
2. **The logic is tested as Python.** Filter composition, tag policy, the
   Xvfb decision, remote selection — plain functions with plain tests, in
   place of substring assertions on rendered YAML.
3. **A job can be replayed on a workstation:**
   `BUILD_TYPE=Debug PYTHON_TARGET_VERSION=3.13 xmsconan job build`.
4. **One implementation** each of version resolution, Xvfb, filter policy
   and tool pins, instead of two to four.
5. **Both templates' `script:` blocks collapse to two lines**, and the GitHub
   template's four platform jobs become identical apart from the matrix.
6. **Step-scoped secrets on GitHub** fall out of the shape for free.

## 5. Trade-offs and mitigations

| Trade-off | Mitigation |
|---|---|
| One command hides the phases in the job log | Phase markers (`section_start` / `::group::`), the `==>` banners, per-configuration logs saved as artifacts (§3.3) |
| Behavior floats with the installed tool version | Already true today for every `xmsconan_*` line; this widens an existing exposure rather than creating one. Pin the tool (the policy is an open question, §7) and print its version first (§3.3) |
| GitHub runs build and deploy in one job; GitLab separates them through artifacts | `job build` writes `.export/` and `job deploy` reads it, so both topologies are the same two commands. Do not let GitHub grow a "build-and-deploy" special case |
| `build.py` and CI diverge if the in-process path and the generated script drift | Both construct `XmsConanPackager` from the same `read_build_toml()`; a test drives each through one configuration and compares |
| A failing leg is harder to bisect without per-step `if:` | The leg is one configuration; the log per configuration and the `test_artifacts/<label>/` layout already identify it |

## 6. Migration outline

Four PRs, GitLab first, in the order that shrinks the templates fastest
with the least behavior change. Modules, tests and acceptance criteria are
in [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) Phase 5.

1. `[ci]` extra and version from the CI environment — plan 5.1.
2. `job build`, `job test`, `job package`, `job lint` on GitLab — plan 5.2.
3. `job deploy` and `job coverage --pages` — plan 5.3.
4. GitHub, last, because it holds the most shell — plan 5.4.

The golden-file template tests (plan 3.1) land before step 2 so each
step's template shrink is reviewable as a diff.

## 7. Open questions

- **Name.** `xmsconan job <kind>` is proposed; `run` and `stage` are the
  alternatives. `ci` is taken by the generator.
- **Pin policy.** `>=X,<X+1` in the generated file, or an explicit
  `[ci].xmsconan_version` in `build.toml` that the generator writes through?
- **Release asset.** Should `job deploy --cache-archive` also upload it (needs
  `gh` and a token in the environment), or stay with `upload-release-asset`?
  Proposed: stay with the action; it is declarative and already SHA-pinned.
- **`build.py`.** Keep generating it for local use, or point USAGE §9 at
  `job build` and stop? Proposed: keep for one release cycle, then decide.
- **Wheel upload client.** `uv publish` (uv is already the toolchain in every
  generated job) or twine; either removes the argv password.

## 8. Related findings

This design is the remedy for R17 in
[REVIEW-2026-09-03.md](REVIEW-2026-09-03.md), delivers S2 and S3 as a side
effect of §3.3 and §3.4, and builds on R11, R12, R18 and R19. The plan's
coverage matrix maps every finding to its phase.
