# xmsconan — Consumer Guide

`xmsconan` is a build-orchestration toolkit for Aquaveo's XMS C++ libraries. You write **one `build.toml`**; xmsconan generates everything else: the Conan recipe, CMake glue, a `build.py` driver, a Python package skeleton, and a CI pipeline. It also ships the runtime helpers those generated files depend on.

This doc is what a consumer needs to know to set up, build, publish, and depend on an xmsconan-managed library.

---

## 1. The mental model

```
build.toml                       (you write this)
   │
   │  xmsconan gen     →   conanfile.py, build.py, CMakeLists.txt,
   │                       _package/pyproject.toml, .flake8, pytest.ini,
   │                       xms_conan2_file.py
   │
   │  xmsconan ci      →   .github/workflows/<Lib>-CI.yaml  OR  .gitlab-ci.yml
   │
   ▼
python build.py            ← runs the full Conan matrix locally
xmsconan publish           ← build + repair wheel + deploy to devpi + push to Conan
```

The C++ source you write lives in `<library_name>/`; tests live alongside. `xmsconan gen` regenerates the *build* files from `build.toml` on each invocation — they are not meant to be edited by hand.

---

## 2. Installation

```bash
pip install xmsconan
# or, from the Aquaveo dev index
pip install xmsconan -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple
```

Conan 2 is a hard dependency and is installed transitively. You also need **CMake ≥ 3.21** and a C++17 compiler on the system PATH for actual builds.

---

## 3. Quickstart

```bash
# 1. Drop a build.toml into the root of your repo (see §5 for the schema)
# 2. Generate everything that xmsconan owns
xmsconan gen --version 0.0.0 build.toml

# 3. Generate a CI pipeline (one-time; commit it)
xmsconan ci  --version 0.0.0 build.toml

# 4. One-shot Conan setup (adds the aquaveo remote, etc.)
xmsconan conan-setup

# 5. Build the full matrix locally
python build.py --version 0.0.0 --wheel-dir wheelhouse --artifacts-dir test_artifacts
```

Everything past step 1 is reproducible — re-run `xmsconan gen` whenever `build.toml` changes.

---

## 4. The unified CLI

All commands live under the `xmsconan` umbrella:

| Command | What it does |
|---|---|
| `xmsconan gen` | Render build files (`conanfile.py`, `build.py`, `CMakeLists.txt`, `_package/pyproject.toml`, …) from `build.toml`. |
| `xmsconan ci` | Render `.github/workflows/<Lib>-CI.yaml` or `.gitlab-ci.yml`. |
| `xmsconan build` | Run `conan install` + `cmake configure` against a single profile. Used by the generated `build.py`; also useful for one-off configures. |
| `xmsconan conan-setup` | Detect a Conan profile, add the aquaveo remote, optionally login. |
| `xmsconan wheel-repair` | Run platform-appropriate wheel repair (auditwheel / delocate / delvewheel). |
| `xmsconan wheel-deploy` | Upload repaired wheels to devpi. |
| `xmsconan conan-deploy` | Save / restore / upload Conan packages between CI stages. |
| `xmsconan publish` | The full release pipeline (gen → build → repair → deploy). |

Run `xmsconan <cmd> --help` for the full flag set. The legacy underscored names (`xmsconan_gen`, `xmsconan_ci`, …) still work and are what the generated CI scripts call.

---

## 5. `build.toml` reference

`build.toml` is the **only** file you author for the build system. It controls everything xmsconan generates.

### 5.1 Required

| Field | Type | Description |
|---|---|---|
| `library_name` | string | Conan / CMake project name. e.g. `"xmscore"`. |
| `description` | string | One-line summary; flows into `conanfile.py` and the wheel metadata. |

### 5.2 Source layout

| Field | Default | Description |
|---|---|---|
| `library_sources` | `[]` | C++ implementation files (`.cpp`) for the static library. |
| `library_headers` | `[]` | Public headers exported to consumers. |
| `testing_sources` | `[]` | `.cpp` files compiled into the test runner only. |
| `testing_headers` | `[]` | Test fixture / helper headers (`*.t.h` for cxxtest). |
| `python_library_sources` | `[]` | C++ files compiled only when `pybind=True`. |
| `python_library_headers` | `[]` | Headers compiled only when `pybind=True`. |
| `pybind_sources` | `[]` | Pybind11 binding `.cpp` files. |
| `pybind_headers` | `[]` | Pybind11 binding headers. |

Paths are interpreted relative to the directory `build.toml` lives in.

### 5.3 Dependencies

| Field | Type | Default | Description |
|---|---|---|---|
| `xms_dependencies` | array[object] | `[]` | XMS sister libraries. Object shape: `{ name = "xmscore", version = "7.0.0", no_python = false }`. `no_python = true` excludes the dep from `_package/pyproject.toml`. |
| `extra_dependencies` | array[string] | `[]` | Extra Conan deps in `"name/version"` form. |
| `xms_dependency_options` | object | `{}` | Override an XMS dep's options. e.g. `{ "xmscore" = { "pybind" = false } }`. |
| `conan_profile_options` | object | `{}` | Per-package options written into the `[options]` section of every generated profile. e.g. `{ "boost" = { "shared" = true } }`. The wildcard `"*"` is supported (e.g. `{ "*" = { "shared" = true } }`); a more specific entry overrides the wildcard. |

Boost (`1.86.0`) and zlib (`1.3.1`) are added automatically by the recipe.

### 5.4 Build configuration

| Field | Default | Description |
|---|---|---|
| `testing_framework` | `"cxxtest"` | `"cxxtest"` or `"gtest"`. Selects the test discovery / runner template in CMake. |
| `python_binding_type` | `"pybind11"` | `"pybind11"` or `"vtk_wrap"`. |
| `python_namespaced_dir` | derived | The submodule under `xms.<...>`. e.g. `"core"` produces `xms.core`. Defaults to `library_name` minus the `xms` prefix when omitted. |
| `pybind_root` | `false` | Whether this library hosts the root `xms` namespace. |

### 5.5 CMake escape hatches

| Field | Default | Description |
|---|---|---|
| `extra_cmake_text` | `""` | Raw CMake injected near the top of `CMakeLists.txt`. |
| `post_library_cmake_text` | `""` | Raw CMake appended after the library target is defined. |
| `extra_export_sources` | `[]` | Additional files / directories Conan exports with the recipe (e.g. `["test_files"]`). |

### 5.6 CI configuration (`[ci]` table)

These drive the CI templates. All optional.

| Field | Default | Description |
|---|---|---|
| `ci_type` | — | `"github"` or `"gitlab"`. **Required** for `xmsconan ci`. (Lives at the top level, not under `[ci]`.) |
| `[ci].windows` | `true` | Emit a Windows job. |
| `[ci].linux_arm` | `false` | Emit a Linux ARM job (GitHub only). |
| `[ci].deploy` | `true` | Emit deploy jobs (only run on tag pushes). |
| `[ci].coverage` | `false` | Emit a coverage job. On GitLab adds a `Coverage` stage + Pages upload; on GitHub adds a separate `Coverage.yaml` workflow. Both delegate to `xmsconan coverage`. Thresholds and filters come from `[coverage]` (see §5.7). |
| `[ci].xvfb` | `false` | Wrap test execution in `xvfb-run` (use for libraries that link X11/VTK). |
| `[ci].split_tests` | `false` | Split build and C++ test into two stages so testing artifacts can be reused. |
| `[ci].test_shards` | `0` | When >1 (and `split_tests=true`), shard C++ tests over N parallel jobs using gtest sharding. |
| `[ci].docker_image` | `""` | Override the build container image (skips the default Aquaveo images). |
| `[ci].python_versions` | `["3.13"]` | Python versions to build. **Only the Windows matrix fans out across multiple versions** — Linux and macOS always use the highest entry (default `3.13`). Set to `["3.10", "3.13"]` to build a Windows 3.10 wheel + Conan binary in addition to 3.13. See §8. |

### 5.7 Coverage thresholds (`[coverage]` table)

Consumed by `xmsconan coverage`; only relevant when `[ci].coverage = true` (or when you run the tool locally). All optional.

| Field | Default | Description |
|---|---|---|
| `[coverage].cpp_threshold` | `0` | Minimum C++ line coverage percent. `xmsconan coverage` exits non-zero when gcovr reports below this. |
| `[coverage].python_threshold` | `0` | Minimum Python line coverage percent (pytest-cov). |
| `[coverage].filters` | `["<library_name>/"]` | gcovr `--filter` patterns. Defaults to the library's own source tree. |
| `[coverage].excludes` | `[".*\\.t\\.h$", ".*/<library_name>/python/.*", ".*/_package/tests/.*"]` | gcovr `--exclude` patterns. Strips test fixtures, the pybind layer, and the Python test tree from the C++ measurement. |
| `[coverage].python_version` | highest of `[ci].python_versions` (default `"3.13"`) | The single Python ABI the pybind coverage build is pinned to. `xmsconan coverage` runs two builds: a `testing=True+pybind=False+Debug` build for C++ coverage (no ABI dependency) and a `pybind=True+testing=False+Debug` build for Python coverage that gets pinned to this version. Multi-Python fan-out is intentionally collapsed on the pybind side so the Python report is deterministic. Override only when the highest CI version isn't the one you want to gate on. |

Both thresholds default to `0`, which means "report only, don't gate." Set them to real values once a baseline has been established.

### 5.8 Example

```toml
library_name = "xmscore"
description = "Support library for XMS products"
ci_type = "github"

python_namespaced_dir = "core"
pybind_root = true

xms_dependencies = []

library_sources = [
    "xmscore/math/math.cpp",
    "xmscore/misc/StringUtil.cpp",
]
library_headers = [
    "xmscore/math/math.h",
    "xmscore/misc/StringUtil.h",
]
testing_sources = ["xmscore/testing/TestTools.cpp"]
testing_headers = ["xmscore/math/math.t.h", "xmscore/testing/TestTools.h"]
pybind_sources  = ["xmscore/python/xmscore_py.cpp"]

[ci]
linux_arm = true
python_versions = ["3.10", "3.13"]
```

---

## 6. What `xmsconan gen` writes

After `xmsconan gen --version X.Y.Z build.toml` you will have (alongside `build.toml`):

```
.
├── build.toml
├── conanfile.py              # Conan recipe (extends XmsConan2File)
├── build.py                  # Driver: orchestrates the conan-create matrix
├── CMakeLists.txt            # CMake project — ALL knobs are cache vars
├── xms_conan2_file.py        # Runtime helper imported by conanfile.py
├── pytest.ini
├── .flake8
└── _package/
    └── pyproject.toml        # Python package metadata for the wheel
```

**Don't hand-edit these.** Treat them like generated code: regenerate from `build.toml` on every change. The exception is `xms_conan2_file.py`, which is *copied* (not rendered) — it's part of xmsconan itself and updates whenever you upgrade the `xmsconan` Python package.

---

## 7. The Conan recipe — what it exposes

The generated `conanfile.py` is a thin subclass of `xmsconan.xms_conan2_file.XmsConan2File`. The interesting bits for consumers:

### 7.1 Settings

Standard Conan: `os`, `compiler`, `build_type`, `arch`.

### 7.2 Options

| Option | Values | Default | What it controls |
|---|---|---|---|
| `wchar_t` | `"builtin"` / `"typedef"` | `"builtin"` | MSVC `/Zc:wchar_t-` toggle. Only `"typedef"` is built on MSVC (and is excluded from non-MSVC). |
| `pybind` | `True` / `False` | `False` | Build the Python binding module + wheel. Allowed for any `build_type`. |
| `testing` | `True` / `False` | `False` | Build the test runner. Mutually exclusive with `pybind=True` in the standard packager fan-out — the coverage runner instruments each shape in a separate Conan create rather than combining them. |
| `python_version` | `"3.10"` / `"3.13"` | `"3.13"` | Which Python ABI to target when `pybind=True`. **Dropped from `package_id` when `pybind=False`**, so non-Python builds remain a single binary regardless. |

### 7.3 Required CMake variables (set by the recipe via `tc.variables`)

`PYTHON_TARGET_VERSION`, `IS_PYTHON_BUILD`, `BUILD_TESTING`, `XMS_TESTING_FRAMEWORK`, `XMS_VERSION`. The generated `CMakeLists.txt` already wires these up; only relevant if you write `extra_cmake_text`.

---

## 8. Python version support (3.10 + 3.13)

xmsconan defaults to **Python 3.13 only** everywhere. Some downstream Aquaveo projects (currently Windows-only) need a Python 3.10 build, so the matrix can opt in **just on Windows** to keep the rest of CI simple:

```toml
[ci]
python_versions = ["3.10", "3.13"]
```

What this turns on:

- **Windows CI matrix expands to both versions.** GitHub Actions: `python-version: ["3.10", "3.13"]` on the Windows job only. GitLab: `parallel:matrix` over `PYTHON_TARGET_VERSION` (and the derived `PY_TAG`) on `Conan Build - Windows` and `Conan Deploy - Windows`.
- **Linux, Linux-ARM, and macOS stay 3.13 only.** Their containers stay on `conan-gcc13-py3.13`, the manylinux wheel-repair stays on `cp313-cp313`, and `Wheel Deploy` / `Conan Deploy - Linux` run as single jobs. No 3.10 docker image is needed.
- **Conan binaries.** Each Windows pybind variant carries the `python_version` option in its `package_id`, so consumers select `xmscore/X.Y.Z@... pybind=True python_version=3.10` vs `=3.13`. Non-pybind builds drop `python_version` from `package_id`, so testing/plain-library binaries remain a single shared binary regardless.
- **Wheel output.** Windows produces both `cp310-cp310-win_amd64.whl` and `cp313-cp313-win_amd64.whl`; pip on the consumer side picks the right one.

For local builds (`python build.py`), the matrix is single-version: it uses `PYTHON_TARGET_VERSION` from the environment if set, otherwise `3.13`. Per-`python_version` fan-out is currently a CI/Windows-only feature; to build both wheels locally you'd need to invoke `python build.py` once per version (or construct `XmsConanPackager` directly with `python_versions=["3.10", "3.13"]`).

> **Runner / image expectations.** Opt-in assumes the `GLR-py310` GitLab Windows runner tag exists. The Linux/Mac side keeps using existing `conan-gcc13-py3.13` images, so no new images are required.

---

## 9. Local development workflow

### 9.1 Generate, then build everything

```bash
xmsconan gen      --version 0.0.0 build.toml
python build.py   --version 0.0.0 --wheel-dir wheelhouse --artifacts-dir test_artifacts
```

`build.py` flags worth knowing:

| Flag | Effect |
|---|---|
| `--filter '{"build_type": "Release"}'` | Restrict to a subset of the matrix. Keys match the configuration dict (`build_type`, `arch`, `compiler`, `options.pybind`, `options.python_version`, …). |
| `--python-only` | Equivalent to `--filter '{"options": {"pybind": true}}'`. |
| `--preview` | Print the configuration table and exit. Nothing is built. |
| `--build-missing` | Pass `--build=missing` to `conan create`. |
| `--wheel-dir DIR` | After the build, copy each `pybind` package's wheel into `DIR`. With `python_versions=["3.10","3.13"]`, you get one wheel per version. |
| `--repair` | Run `repair_linux_wheel` after extraction (Docker required). |
| `--artifacts-dir DIR` | Save per-config test artifacts (LastTest.log, runner binary, `_package/`, `test_files/`) for debugging. |
| `--test-shards N\|auto` | Run gtest sharding for testing builds. |
| `--skip-build --upload` | After a successful build, push the matrix to the Conan remote. |

### 9.2 Configure a single profile (for an IDE)

`build.py` runs `conan create` for every config. To get a buildable IDE configuration for one profile (no `conan create`, just install + configure):

```bash
xmsconan build \
    --cmake_dir . \
    --build_dir ../builds/xmscore \
    --profile VS2022_TESTING \
    --generator vs2022
```

Available profile names live under `xmsconan/build_tools/profiles/{debug,release}/`. Append `_d` for debug. Examples:

- `GCC13`, `GCC13_TESTING`, `GCC13_PYBIND`, `GCC13_TESTING_D`
- `CLANG17_PYBIND`, `CLANG16_TESTING_D`
- `VS2022`, `VS2022_TESTING`, `VS2022_TESTING_DYNAMIC_D`

You can also pass any explicit profile path with `--profile /path/to/profile`.

### 9.3 Useful build flags

- `--allow-missing-test-files` — Build even when `./test_files/` doesn't exist.
- `--dry-run` — Print the Conan and CMake commands without running them.
- `-v` / `-q` — Verbose / quiet output.

---

## 10. Generating CI

```bash
xmsconan ci --version 0.0.0 build.toml
```

Emits `.github/workflows/<Lib>-CI.yaml` (when `ci_type = "github"`) or `.gitlab-ci.yml` (when `ci_type = "gitlab"`). **Commit the result** — CI runs against the committed file.

The generated jobs follow the pattern:

1. **Setup Python + Conan** (`xmsconan_conan_setup --remote-url … --login`)
2. **Generate build files** (`xmsconan_gen --version …`)
3. **Build** (`python build.py --filter='{"build_type": "<type>"}' --wheel-dir wheelhouse --artifacts-dir test_artifacts`)
4. **Repair wheel** on Release (`xmsconan_wheel_repair --wheel-dir wheelhouse`)
5. **On tag pushes:** `xmsconan_wheel_deploy` and `xmsconan_conan_deploy … --upload`

### 10.1 GitHub specifics

- Mac / Linux / Linux-ARM matrices: `build_type × python-version=['3.13']` (single version).
- **Windows** matrix: `build_type × compiler-version × python-version=ci_python_versions` — only this job expands when you opt in to 3.10.
- Wheel artifacts: `wheel-${{ runner.os }}` for mac/linux/arm, `wheel-${{ runner.os }}-py${{ matrix.python-version }}` for Windows so the two Python ABIs don't collide.
- Linux containers stay on `conan-gcc13-py3.13:latest`.

### 10.2 GitLab specifics

- `Conan Build`, `Repair Wheel`, `Wheel Deploy`, `Conan Deploy - Linux`: single-version jobs running on 3.13. No `parallel:matrix`.
- `Conan Build - Windows`, `Conan Deploy - Windows`: `parallel:matrix` over `PYTHON_TARGET_VERSION`. The matrix also sets `PY_TAG` (`py310` / `py313`) which selects the runner via `image: GLR-${PY_TAG}`.
- Wheel-repair always runs `cp313-cp313`'s `xmsconan_wheel_repair` inside the manylinux container; auditwheel itself doesn't care about the host Python.
- Required CI variables: `AQUAPI_URL`, `AQUAPI_USERNAME`, `AQUAPI_PASSWORD` (for wheel deploy).

---

## 11. Coverage (`xmsconan coverage`)

`xmsconan coverage` (also available as the legacy `xmsconan_coverage` script) runs two instrumented Conan builds and produces both C++ and Python coverage reports. Configured via the `[coverage]` table in `build.toml` (§5.7).

```bash
xmsconan coverage --version 0.0.0 build.toml
```

What it does:

1. Generates `conanfile.py`, `build.py`, and `CMakeLists.txt` from `build.toml` (via `xmsconan gen`).
2. Sets `XMS_COVERAGE=1` and invokes `build.py` twice in sequence:
   - First: `--filter '{"build_type":"Debug","options":{"testing":true,"pybind":false}}'` — Conan create that builds the library + CxxTest runner under `--coverage`, then runs the runner. Produces the `.gcda` set gcovr reads.
   - Second: `--filter '{"build_type":"Debug","options":{"pybind":true,"testing":false,"python_version":"3.13"}}'` — Conan create that builds the library + pybind wheel under `--coverage`, then runs `pytest-cov` against the wheel. Produces `cov-py.xml`, `cov-py-summary.json`, and `coverage-html-py/` inside the build folder.

   `XMS_COVERAGE=1` does two things: it tells `XmsConanPackager` to relax the usual "no Debug+pybind" gate so the Python coverage build can exist (normally Debug+pybind is redundant since the Release wheel is what ships), and it rides into each profile's `[buildenv]` so CMake adds `--coverage` to both builds. The testing-only Debug variant is part of the standard packager fan-out and is emitted whether `XMS_COVERAGE` is set or not. `python_version` on the pybind build is pinned to one ABI (highest of `[ci].python_versions` by default, or `[coverage].python_version` when set) so multi-version fan-outs cannot non-deterministically pick whichever pybind config finished last.
3. Locates the two build folders in the local Conan cache, runs gcovr against the testing build folder to produce `cov-cpp.xml`, `cov-cpp-summary.json`, and `coverage-html-cpp/`, and copies the pytest-cov artifacts out of the pybind build folder.
4. Writes `cov-cpp.xml`, `cov-py.xml`, `cov-cpp-summary.json`, `cov-py-summary.json`, `coverage-html-cpp/`, and `coverage-html-py/` into `--output_dir`.
5. Compares the line-coverage percent for each layer against `[coverage].cpp_threshold` / `[coverage].python_threshold` and exits non-zero on regression. The tool also exits non-zero if `build.py` reported a test failure in either layer — but only *after* still producing gcovr reports, copying artifacts, and (if `$GITHUB_STEP_SUMMARY` is set) appending the markdown summary, so the coverage data and the failing-test signal are both visible in the same run. If a coverage summary file is present but missing its expected keys (gcovr/pytest-cov schema drift, truncated write), the tool raises rather than reporting a misleading 0%.

### 11.1 What this requires of the recipe

- `python_namespaced_dir` **must** be set on the recipe (it is by default — `xmsconan_gen` derives it from `library_name`). With `XMS_COVERAGE=1` the recipe targets `pytest-cov` at exactly `xms.<python_namespaced_dir>` so coverage doesn't leak across `xms_dependencies` installed in the build venv. The recipe's `configure()` raises at install time if `XMS_COVERAGE=1` is set without `python_namespaced_dir`, so the run fails fast before a full instrumented build is wasted.
- The compiler must support `--coverage` (GCC/Clang). MSVC is rejected by the generated `CMakeLists.txt` when `XMS_COVERAGE` is non-empty.
- `gcovr` must be on `PATH` (the generated CI pipelines `pip install gcovr` for you).

### 11.2 `[ci].xvfb = true`

If the library needs an X server to run its tests (VTK, GUI libs), `xmsconan coverage` re-execs itself under `xvfb-run -a -s "-screen 0 1280x1024x24"` automatically. A missing `xvfb-run` is logged as a warning, not a fatal error, so the test failures surface clearly.

### 11.3 Environment variables consumed

| Variable | Set by | Effect |
|---|---|---|
| `XMS_COVERAGE` | `xmsconan coverage` | Recipe-side flag. Enables `--coverage` in `CMakeLists.txt`, installs `pytest-cov`, and passes `--cov=xms.<python_namespaced_dir>` to pytest. |
| `XMS_COVERAGE_PIP_INDEX` | you | Optional extra `--extra-index-url` for the build venv's `pip install` when coverage is enabled (useful when `pytest-cov` lives on a private index). |
| `GITHUB_STEP_SUMMARY` | GitHub Actions | When set, a coverage summary table is appended. |

### 11.4 GitLab vs GitHub

- **GitLab**: `xmsconan ci` emits a `Coverage` stage that invokes `xmsconan_coverage` (the legacy alias used throughout the generated CI for consistency with `xmsconan_gen`, `xmsconan_conan_setup`, etc.; equivalent to `xmsconan coverage`), exposes `cov-cpp.xml` as the `cobertura` coverage report, and ships HTML to Pages. The `coverage:` regex still matches gcovr's `TOTAL` line (the `--txt` summary is printed to stdout). The `pages` stage publishes a small landing page at the Pages root linking both `cpp/` and `python/` reports (the Python link is dropped if Python coverage was not produced).
- **GitHub**: `xmsconan ci` emits a separate `Coverage.yaml` workflow that runs on `push` and `pull_request`, uploads `coverage-html-*/` and `cov-*.xml` as artifacts, and appends a summary table to the run page. The workflow runs directly on `ubuntu-latest` (no docker container) and pip-installs `--upgrade xmsconan>=…` on every run, so the Coverage canary always exercises the latest xmsconan on devpi. Setting `[ci].xvfb = true` apt-installs `xvfb` as an extra step; `[ci].docker_image` is honored by the build/deploy workflows but not by Coverage.

---

## 12. Wheel repair (`xmsconan wheel-repair`)

Conan-built wheels reference shared libraries from the build environment that won't exist on consumer machines. Wheel repair bundles them in.

```bash
# Auto-detect the platform from sys.platform
xmsconan wheel-repair --wheel-dir wheelhouse

# Or be explicit
xmsconan wheel-repair --wheel-dir wheelhouse --platform linux
```

What runs per platform:

| Platform | Tool | How libs are found |
|---|---|---|
| Linux | `auditwheel repair` (with `patchelf`) | `LD_LIBRARY_PATH={wheel_dir}/libs` |
| macOS | `delocate-wheel` | `DYLD_LIBRARY_PATH={wheel_dir}/libs` |
| Windows | `delvewheel repair --namespace-pkg xms` | `--add-path {wheel_dir}/libs` |

`build.py` already populates `wheelhouse/libs/` for you when `--wheel-dir` is set. After repair, the original `wheelhouse/` is replaced with the repaired version (the `libs/` directory is removed).

---

## 13. Wheel deploy (`xmsconan wheel-deploy`)

Uploads `wheelhouse/*.whl` to a devpi index.

```bash
xmsconan wheel-deploy --wheel-dir wheelhouse
```

**Credential resolution order** (first non-empty wins):

1. CLI flags: `--url`, `--username`, `--password`
2. Environment: `AQUAPI_URL`, `AQUAPI_USERNAME`, `AQUAPI_PASSWORD`
3. `~/.xmsconan.toml` `[aquapi]` section

---

## 14. Conan deploy (`xmsconan conan-deploy`)

Used to ship Conan binaries between CI stages or to upload them at the end. The three modes:

```bash
# Save the cached package(s) to a tarball
xmsconan conan-deploy xmscore 7.0.0 --save xmscore-linux-7.0.0.tar.gz

# Restore from a tarball (e.g. produced by an earlier CI stage)
xmsconan conan-deploy xmscore 7.0.0 --restore xmscore-linux-7.0.0.tar.gz

# Upload the cached package(s) to the aquaveo remote
xmsconan conan-deploy xmscore 7.0.0 --upload

# Or, end-to-end in one shot
xmsconan conan-deploy xmscore 7.0.0 --restore xmscore-linux-7.0.0.tar.gz --upload
```

At least one of `--save / --restore / --upload` is required.

---

## 15. Full release pipeline (`xmsconan publish`)

Wraps the entire flow — useful for CI and for one-off local releases inside a Docker container.

```bash
# Resolves version from git tag; reads creds from ~/.xmsconan.toml or env
xmsconan publish --version 7.0.0

# Build only, no upload
xmsconan publish --version 7.0.0 --no-deploy

# Skip the wheel half / Conan half independently
xmsconan publish --version 7.0.0 --no-wheel    # Conan-only release
xmsconan publish --version 7.0.0 --no-conan    # wheel-only release

# Restrict the matrix
xmsconan publish --version 7.0.0 --filter '{"build_type": "Release"}'
```

What it runs (with `--no-deploy=false`):

1. `xmsconan_conan_setup --login`
2. `xmsconan_gen --version <ver> build.toml`
3. `python build.py --version <ver> --wheel-dir <dir>` (wrapped in `xvfb-run` if `[ci].xvfb=true` and there is no `$DISPLAY` on Linux)
4. `xmsconan_wheel_repair --wheel-dir <dir>`
5. `xmsconan_wheel_deploy --wheel-dir <dir>` *(skipped with `--no-wheel`)*
6. `xmsconan_conan_deploy <library> <version> --upload` *(skipped with `--no-conan`)*

---

## 16. Credentials (`~/.xmsconan.toml`)

Avoid passing credentials on every command:

```toml
[aquapi]
url      = "https://public.aquapi.aquaveo.com/aquaveo/dev/"
username = "your_username"
password = "your_password"

[conan]
username = "your_username"
password = "your_password"
```

Always overridden by CLI flags / env vars when present. **Don't commit this file.** It's read-only as far as xmsconan is concerned.

---

## 17. Consuming an XMS library from another project

Once a release has been pushed to the Aquaveo Conan remote, downstream Conan consumers depend on it like any other Conan 2 package:

```python
# downstream conanfile.py
class MyApp(ConanFile):
    settings = "os", "compiler", "build_type", "arch"

    def requirements(self):
        # C++-only consumer — no python_version, no pybind
        self.requires("xmscore/7.0.0")

    def configure(self):
        # If you DO want the Python bindings, set both:
        self.options["xmscore"].pybind = True
        self.options["xmscore"].python_version = "3.13"   # or "3.10"
```

Or with explicit options on the install:

```bash
conan install . \
    -s build_type=Release \
    -o "xmscore/*:pybind=True" \
    -o "xmscore/*:python_version=3.10"
```

The `xms_dependencies` field in *your* `build.toml` handles the same wiring automatically for sister XMS libraries.

### 17.1 Consuming the wheel (Python-only)

```bash
pip install xmscore -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple
```

Wheels are tagged `cp310-cp310-...` or `cp313-cp313-...`; pip picks the right one based on the active interpreter.

---

## 18. Troubleshooting

- **`auditwheel`/`delocate`/`delvewheel` missing libraries.** Run `build.py --wheel-dir wheelhouse` before repair — that step populates `wheelhouse/libs/`. Repairing without it produces a wheel that loads fine on the build host and crashes everywhere else.
- **`PYTHON_TARGET_VERSION` mismatch in CMake.** The recipe sets it from the `python_version` Conan option. If you're poking CMake directly, pass `-DPYTHON_TARGET_VERSION=3.13`.
- **`No pybind package found to extract`.** Means `build.py` ran but no pybind config was built. Check `build.py --preview` to see the matrix; common causes are `--filter` excluding the pybind variant, or every pybind variant having failed.
- **Dual wheel uploads colliding on devpi.** With `python_versions=["3.10","3.13"]`, wheels carry distinct `cp3XY` tags, so devpi treats them as separate uploads of the same release. No special config required.
- **Generated CI references a runner that doesn't exist.** Opt-in only matrices Windows, so the only new runner you need is `GLR-py310` (GitLab). If it isn't available, set `python_versions = ["3.13"]` until it is. The Linux/Mac side keeps using the existing 3.13 images.

---

## 19. Reference: shipped Conan profiles

Located under `xmsconan/build_tools/profiles/{debug,release}/`. Use the basename with `xmsconan build --profile`:

| Family | Examples |
|---|---|
| GCC | `GCC5`, `GCC7`, `GCC13`, plus `_TESTING`, `_PYBIND`, `_D` (debug) suffixes |
| Clang | `CLANG9`, `CLANG16`, `CLANG17`, plus `_TESTING`, `_PYBIND`, `_D` |
| MSVC | `VS2019`, `VS2022`, plus `_TESTING`, `_TESTING_DYNAMIC`, `_D` |

Each suffix means:

- `_D` — Debug build
- `_TESTING` — Testing-enabled build (cxxtest/gtest runner)
- `_PYBIND` — Pybind-enabled build (Release only)
- `_DYNAMIC` (MSVC) — Dynamic CRT (`MD`/`MDd`) instead of static (`MT`/`MTd`)

For a custom mix, write your own profile that `include()`s entries from `xmsconan/build_tools/profiles/base/` and pass it via `--profile /path/to/profile`.
