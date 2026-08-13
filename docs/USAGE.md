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
| `xmsconan coverage` | Run the unified C++/Python coverage pipeline and enforce the `[coverage]` thresholds (see §11). |
| `xmsconan vs2019` | Drive the manual Visual Studio 2019 (msvc 192) build and publish it to the `aquaveo-vs2019` remote (see §16). Never runs in CI. |
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
| `testing_sources` | `[]` | `.cpp` files compiled into the library when `testing=True`, so dependent libraries can link the testing helpers. Excluded from Python builds — see the note below. |
| `testing_headers` | `[]` | Test fixture / helper headers (`*.t.h` for cxxtest). |
| `python_library_sources` | `[]` | C++ files compiled only when `pybind=True`. |
| `python_library_headers` | `[]` | Headers compiled only when `pybind=True`. |
| `pybind_sources` | `[]` | Pybind11 binding `.cpp` files. |
| `pybind_headers` | `[]` | Pybind11 binding headers. |

Paths are interpreted relative to the directory `build.toml` lives in.

#### Where `testing_sources` end up

Under `testing=True` they are compiled into the library itself, and the test
runner picks them up by linking it. This is deliberate: xms libraries publish
testing helpers (xmscore's `ttEqualPointsXYZ`, `ttTextFilesEqual`, ...) that
downstream libraries link from the package they already consume, so the helpers
have to live in the library that gets packaged.

Under `pybind=True` they are excluded from the library and the runner compiles
them directly instead. The pybind module links the main library, and these
translation units reference cxxtest helpers such as `CxxTest::charToString` —
declared in `cxxtest/ValueTraits.h` but defined only in
`cxxtest/ValueTraits.cpp`, which is pulled in solely by the cxxtestgen-generated
`runner.cpp`. A pybind module carrying them fails `dlopen` with an undefined
symbol.

The two options are never combined in a generated configuration (the packager
fans out `wchar_t`, `pybind` and `testing` independently rather than
cross-multiplying), so in practice `testing=True` always means the helpers are
in the library. The `IS_PYTHON_BUILD` guard exists for hand-built and
option-overridden configurations.

### 5.3 Dependencies

| Field | Type | Default | Description |
|---|---|---|---|
| `xms_dependencies` | array[object] | `[]` | XMS sister libraries. Object shape: `{ name = "xmscore", version = "7.0.0", no_python = false }`. `no_python = true` excludes the dep from `_package/pyproject.toml`. |
| `extra_dependencies` | array[string] | `[]` | Extra Conan deps in `"name/version"` form. |
| `xms_dependency_options` | object | `{}` | Override an XMS dep's options. e.g. `{ "xmscore" = { "pybind" = false } }`. |
| `vs2019_dependency_overrides` | object | `{}` | Replace an XMS dep's *reference* — but only on a Visual Studio 2019 (msvc 192) build. e.g. `{ "xmscore" = "xmscore/[>=6.0.1 <7.0.0]" }`. Matched on the package name before the first `/`; every other toolchain ignores it entirely. An entry may change the version or range only — renaming the package, or naming one that is not in `xms_dependencies`, fails the msvc 192 build. See §7.4. |
| `conan_profile_options` | object | `{}` | Per-package options written into the `[options]` section of every generated profile. e.g. `{ "boost" = { "shared" = true } }`. The wildcard `"*"` is supported (e.g. `{ "*" = { "shared" = true } }`); a more specific entry overrides the wildcard. |

Boost (`1.86.0`) and zlib (`1.3.1`) are added automatically by the recipe. On a Visual Studio 2019 (msvc 192) build the recipe swaps in the legacy stack instead — the same two packages at the versions the `aquaveo-vs2019` remote publishes msvc 192 binaries for: boost `1.74.0.3` and zlib `1.2.11`. See §7.4 and §16.

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

### 7.4 Third-party requirements and the VS2019 fork

The recipe resolves a different third-party stack when it detects Visual Studio 2019 — `compiler == msvc` **and** `compiler.version == 192`. **The third-party half of the fork is automatic**: there is nothing to add to `build.toml` for it, and the same `conanfile.py` builds both stacks. Three class attributes on `XmsConan2File` define the fork:

| Attribute | Default | Set from `build.toml`? | What it controls |
|---|---|---|---|
| `default_requirements` | `["boost/1.86.0", "zlib/1.3.1"]` | no | Third-party (non-XMS) requirements for the modern toolchains: gcc 13, apple-clang 17, msvc 194. |
| `vs2019_requirements` | `["boost/1.74.0.3", "zlib/1.2.11"]` | no | Third-party requirements used **instead** on msvc 192. Same package set as `default_requirements`, at the versions the `aquaveo-vs2019` remote publishes msvc 192 binaries for: the Aquaveo legacy boost the desktop products link against, and zlib `1.2.11` (`zlib/1.3.1` exists only on `aquaveo-stable`, msvc 194 only, so naming it here would force a from-source build or fail). zlib is not optional on either stack — the generated `CMakeLists.txt` calls `find_package(ZLIB REQUIRED)` and sources such as `daStreamIo.cpp` include `<zlib.h>`. |
| `vs2019_dependency_overrides` | `{}` | **yes** — the `[vs2019_dependency_overrides]` table (§5.3) | Per-library replacement of `xms_dependencies` references on msvc 192, e.g. `{"xmscore": "xmscore/[>=6.0.1 <7.0.0]"}`. Matched on the package name before the first `/`. Legacy desktop products pin xmscore 6.x while the Conan 2 line is at 7.x. Every other toolchain ignores this dict entirely — including its invalid entries, since the two rules below are checked only on msvc 192. |

Shared by both stacks: `pybind11/3.0.1` when `pybind=True`, and the `testing_framework` requirement (`cxxtest/4.4` or `gtest/1.17.0`) when `testing=True`. pybind11 on VS2019 is intentionally `3.0.1` — an upgrade from the Conan-1-era `2.9.1`, not an oversight.

The split in that third column is deliberate. `default_requirements` and `vs2019_requirements` describe the **shared third-party stack** — every XMS library resolves the same boost, so they live in xmsconan and are changed there. `vs2019_dependency_overrides` is a **per-library** statement about that library's own sister dependencies, so it is a `build.toml` key:

```toml
library_name = "xmsgrid"
description = "Geometry library for XMS products"

[[xms_dependencies]]
name = "xmscore"
version = "7.0.0"

# On msvc 192 only, resolve xmscore from the 6.x line the desktop
# products pin instead of the 7.0.0 above.
[vs2019_dependency_overrides]
xmscore = "xmscore/[>=6.0.1 <7.0.0]"
```

which `xmsconan gen` emits onto the generated recipe subclass:

```python
class XmsgridConanFile(XmsConan2File):
    ...
    vs2019_dependency_overrides = {'xmscore': 'xmscore/[>=6.0.1 <7.0.0]'}
    xms_dependencies = [
        "xmscore/7.0.0",
    ]
```

**Two rules are enforced, and only on msvc 192.** An entry's key must match a package already declared in `xms_dependencies`, and it may change the version or version range only — the package name on both sides of the entry must be identical. Either violation raises a `ConanException` naming the offending entry, rather than being skipped: `configure()` and `run_python_tests()` iterate the *unresolved* `xms_dependencies`, so a rename would set options on and pip-install from a package no longer in the graph, and a key that matches nothing (a typo in the `build.toml` table) would produce a VS2019 build that quietly resolves the very versions the override was written to replace. Silent wrong output is worse than a loud failure, and on this build in particular the failure is the only signal you get — nothing downstream would look wrong until the desktop products linked against it.

Omit the table and the attribute is not emitted at all, so the recipe inherits the empty default. **Do not hand-edit the value into `conanfile.py` or `xms_conan2_file.py`** — `xmsconan gen` rewrites the first and re-copies the second on every run (§6), so only the `build.toml` key survives regeneration.

One more msvc-192-only behavior, which needs no configuration: the recipe propagates its own `wchar_t` option into boost's (`self.options["boost"].wchar_t`). The legacy `boost/1.74.0.3` exposes that option and the Conan 1 recipe did the same; `boost/1.86.0` has none, which is why the assignment is VS2019-only and tolerant of a boost build that doesn't declare it.

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

Available profile names live under `xmsconan/build_tools/profiles/{debug,release}/`, and `--profile` matches the **exact filename** — a name that isn't a file there fails outright rather than falling back. Examples that exist today:

- `GCC13`, `GCC13_TESTING`, `GCC13_PYBIND`, `GCC13_TESTING_D`
- `CLANG17_PYBIND`, `CLANG16_TESTING_D`
- `VS2019`, `VS2019_TESTING`, `VS2019_TESTING_DYNAMIC`, `VS2019_TESTING_DYNAMIC_D`
- `VS2022_TESTING`, `VS2022_TESTING_D`

The full list is §20. You can also pass any explicit profile path with `--profile /path/to/profile`.

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
- The `flake` job installs xmsconan, runs `xmsconan_gen build.toml` to render `.flake8`, then runs plain `flake8 _package`. It deliberately does **not** pass flake8 settings on the command line: that duplicates `.flake8.jinja`, and the two copies drift apart silently (CI once used a different `ignore` list and a stale `conf.py` exclude, so a clean local run did not imply a clean CI run). Change lint settings in `.flake8.jinja` only.

### 10.2 GitLab specifics

- `Conan Build`, `Repair Wheel`, `Wheel Deploy`, `Conan Deploy - Linux`: single-version jobs running on 3.13. No `parallel:matrix`.
- `Conan Build - Windows`, `Conan Deploy - Windows`: `parallel:matrix` over `PYTHON_TARGET_VERSION`. The matrix also sets `PY_TAG` (`py310` / `py313`) which selects the runner via `image: GLR-${PY_TAG}`.
- Wheel-repair always runs `cp313-cp313`'s `xmsconan_wheel_repair` inside the manylinux container; auditwheel itself doesn't care about the host Python.
- Required CI variables: `AQUAPI_URL`, `AQUAPI_USERNAME`, `AQUAPI_PASSWORD` (for wheel deploy).

### 10.3 Toolchain versions

Generated jobs constrain the two tools they install, in opposite directions:

| Tool | Spec | Why |
|---|---|---|
| `xmsconan` | `>=<version that generated the file>`, always with `--upgrade` | An xmsconan fix reaches a repo on its next CI run, rather than requiring a regenerate-and-commit pass across the whole suite. The floor still rules out resolving a version older than the templates were generated against. |
| `conan` | `~=2.31.0` (patch series) | Conan computes package_ids and runs the compatibility plugin; a minor bump can silently detach a build from binaries already on the remote. |

**`--upgrade` is not optional on the xmsconan install.** pip treats an already-satisfied constraint as a no-op, so on a runner image with xmsconan baked in, the floor alone installs nothing and the job silently runs whatever version the image happens to carry. `test_xmsconan_installs_float_and_upgrade` asserts both halves — the `>=` and the `--upgrade` — across every CI option combination for both templates.

Two consequences worth knowing:

- **An xmsconan release reaches every repo on its next CI run.** That is the point of the floor, but it cuts both ways: a bad release is suite-wide immediately, and the committed CI file no longer records which xmsconan actually ran. The sharpest edge is that xmsconan owns the Conan profiles — a release that changes a profile's `compiler.version` changes **every package_id**, for every repo, without any repo changing a line. Treat profile changes with the same care as a breaking API change.
- **Generate CI from a released xmsconan.** A workflow generated from an unreleased working copy writes a floor that nothing on devpi satisfies yet (e.g. `>=2.16.1.dev1` while the newest release is `2.16.0`), and CI fails at `pip install`. Unlike the old `==` pin, this one self-heals: the same file starts working once that version ships.

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
- **GitHub**: `xmsconan ci` emits a separate `Coverage.yaml` workflow that runs on `push` and `pull_request`, uploads `coverage-html-*/` and `cov-*.xml` as artifacts, and appends a summary table to the run page. The workflow runs directly on `ubuntu-latest` (no docker container), which is what lets the `--upgrade xmsconan>=…` install of §10.3 actually take effect here — the image this job used to run in baked xmsconan in, so the install no-op'd and the canary was locked to whatever the image carried. Setting `[ci].xvfb = true` apt-installs `xvfb` as an extra step; `[ci].docker_image` is honored by the build/deploy workflows but not by Coverage.

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

## 16. VS2019 / msvc 192 packages (`xmsconan vs2019`)

GitHub retired the `windows-2019` runner image, so the msvc 192 (Visual Studio 2019) binaries that the Aquaveo desktop products (GMS/SMS/WMS) consume can no longer be produced in CI. They are built **by hand, on a developer workstation that has VS2019 installed**, and published to a **separate Conan remote, `aquaveo-vs2019`**, so they never mix with the CI-published binaries.

> **None of this runs in CI, and that is deliberate.** CI is untouched: it still builds gcc 13 / apple-clang 17 / msvc 194 and publishes to the `aquaveo` remote (the `aquaveo-stable` Artifactory repo). There is no Windows-2019 job to "restore" — the runner image is gone. If you are looking for the CI matrix, see §10.

Available both as `xmsconan vs2019 <subcommand>` and as the console script `xmsconan_vs2019 <subcommand>`. Three subcommands: `setup`, `build`, `upload`.

The recipe side of the fork — legacy boost, and the optional per-library `[vs2019_dependency_overrides]` table for libraries whose sister dependencies are pinned differently on VS2019 — is described in §7.4. Nothing below configures it; this section is only about driving the build.

### 16.1 End to end

```bash
# 1. One time: add + log in to the aquaveo-vs2019 remote, then preflight the machine
xmsconan_vs2019 setup --password-file <path to your conan password file>

# 2. See what would be built — prints the matrix per library and exits
xmsconan_vs2019 build --root E:\code\xms\migration --preview

# 3. Build it. Hours, not minutes; per-configuration logs land in .\vs2019-logs\
xmsconan_vs2019 build --root E:\code\xms\migration --log-dir .\vs2019-logs --version 7.0.0

# 4. Read the summary table, then publish
xmsconan_vs2019 upload --library xmscore --version 7.0.0
```

**`build` and `upload` are separate verbs on purpose: a build never uploads as a side effect.** The run takes hours and its output lands on a remote other people build against, so a human looks at the summary table before anything is published. There is no `--upload` flag on `build`.

### 16.2 `setup`

Adds the remote, logs in, then runs the preflight checks.

| Flag | Default | Effect |
|---|---|---|
| `--password-file PATH` | — | File holding the remote password on its own. Trailing whitespace (the editor's newline) is stripped. A path that doesn't exist, a file that can't be read, and a file that holds nothing but whitespace are all errors (exit 2) — see the resolution table below. |
| `--username NAME` | resolved (see below) | Remote username. |
| `--remote-url URL` | `https://conan2.aquaveo.com/artifactory/api/conan/aquaveo-vs2019` | Artifactory URL backing the remote. |
| `--remote-name NAME` | `aquaveo-vs2019` | Conan remote name to add. Pair it with `--remote-url` when pointing at a different Artifactory repo. |

**Credential resolution order** (first non-empty wins), resolved together so `~/.xmsconan.toml` is read at most once:

| | Username | Password |
|---|---|---|
| 1 | `--username` | `--password-file` — a path that doesn't exist, or a file that exists but holds no password, is an error, not a fall-through to the next source. A typo (or a secret that never landed in the file) must not silently log you in with a different password. |
| 2 | `$CONAN_LOGIN_USERNAME` | `$CONAN_PASSWORD` |
| 3 | `[conan] username` in `~/.xmsconan.toml` (§17) | `[conan] password` in the same file |
| 4 | `aquaveo` | — Conan prompts interactively |

Both halves fall back independently, so a personal Artifactory account configured in `~/.xmsconan.toml` is used as *your* username with *your* password. (The username used to be pinned to `aquaveo` before the config file was ever consulted, which meant a personal password was sent with the shared username and the login failed with no hint why.) If no source supplies a password, `setup` hands Conan nothing and Conan prompts for both username and password itself — the file is not consulted a second time to second-guess that, because it has already been consulted here.

**The password is never a command-line argument.** `setup` hands it to Conan in the child process's environment (`CONAN_LOGIN_USERNAME_AQUAVEO_VS2019` / `CONAN_PASSWORD_AQUAVEO_VS2019` — Conan derives those names by uppercasing the remote name and replacing hyphens with underscores) and runs a bare `conan remote login aquaveo-vs2019`. On a managed workstation, process-creation auditing (Windows Event 4688 with command-line capture, or Sysmon Event ID 1) copies the full argv of every process into the event log and ships it to the SIEM in cleartext, where it outlives and out-reads the NTFS ACLs on your password file.

**Where the remote lands in the list.** `setup` **appends** `aquaveo-vs2019` after the remotes already configured — it does *not* insert it at index 0 the way `xmsconan conan-setup` does for the CI remote. Conan resolves a version range such as `xmscore/[>=7.0.0 <8.0.0]` across every remote in list order, so a VS2019 remote at the front would be the first stop for every `conan install` and `conan create --build=missing` on the machine, including ordinary msvc 194 work, and a version present only on the VS2019 remote would win. That is the exact mixing the separate remote exists to prevent. `setup` prints `(appended)` when it adds the remote; run `conan remote list` to see the resulting order, and `conan remote update aquaveo-vs2019 --index N` if you ever need to change it deliberately.

`setup` exits 0 only when every preflight check (§16.3) also passed, so a fresh machine gets one pass/fail answer on whether it can build the matrix. A failed preflight exits 2 — the same code `build` uses for the same condition; an unusable `--password-file` exits 2; `conan` not being on `PATH` exits 2; any other failing `conan` command propagates its own exit code.

### 16.3 Preflight

Run at the end of `setup`, and again at the start of every `build` — a failure exits 2 either way, aborting the build before anything is compiled. Both verbs take `--remote-name`, and the third check follows it.

| Check | Passes when | Fix |
|---|---|---|
| Visual Studio 2019 | `vswhere.exe` finds a 16.x install **that carries the C++ toolset** (`-requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64`) **and** has a `Microsoft.VCToolsVersion.default.txt`. Reports the install path and the default MSVC toolset version. | Install VS2019 with the *Desktop development with C++* workload. msvc 192 packages can only be built on a machine that has it. |
| conan client | `conan --version` is on the pinned `~=2.31.0` series. | `pip install "conan~=2.31.0"` |
| conan remote `aquaveo-vs2019` | The remote appears in `conan remote list` **and is enabled**. `conan remote list` keeps printing disabled remotes as `[… Enabled: False]`, so the name alone is not enough. | `xmsconan_vs2019 setup --password-file <path>`, or `conan remote enable aquaveo-vs2019` if it is merely disabled. |

A conan version outside the pinned series is a **failure, not a warning**: a minor bump can change package_id computation and silently detach these hand-built packages from the binaries already on the remote. Same reasoning as the CI pin in §10.3.

A VS2019 carrying only, say, the .NET workload used to pass this check and then die on the first `conan create`, hours into the run — hence both the `-requires` filter and the toolset-file check.

### 16.4 `build`

| Flag | Default | Effect |
|---|---|---|
| `--root DIR` | `.` | Directory holding the library checkouts (one subdirectory per library). A `--root` that doesn't exist is an error (exit 2), not eight silent skips. |
| `--only LIB` | — | Build only this library; repeatable. Overrides the `enabled` flag, so a mid-migration library can be exercised. |
| `--from LIB` | — | Resume the stack at this library, skipping the ones before it. For picking up after a mid-stack failure. |
| `--preview` | off | Print the configuration matrix per library and exit. Nothing is built; preflight is not run and `--root` is not checked, because the matrix is computed from the library list and doesn't read the checkouts. |
| `--continue-on-error` | off | Attempt the next library after one fails. |
| `--no-generate` | off | Skip the `xmsconan_gen` step and use the `conanfile.py` already in the checkout. |
| `--log-dir DIR` | — | Redirect each configuration's `conan create` output to `<DIR>/<library>-<config label>.log` and print a one-line pointer instead. A wall of interleaved compiler output is unreadable on a 14-configuration run. The label carries the whole configuration, **runtime included** — `xmscore-Release-static-testing.log`, `xmscore-Debug-dynamic-wchar_typedef.log`, `xmscore-Release-dynamic-pybind-py3.13.log`. Without the runtime the 14 msvc configurations would collapse onto 8 filenames and each static build would overwrite the dynamic build's log. |
| `--python-versions X.Y [X.Y …]` | `3.10 3.13` | Python versions the pybind variants fan out across. |
| `--filter JSON` | — | Restrict the matrix, same shape as `build.py --filter`: `'{"build_type": "Release"}'`. |
| `--version V` | — | Passed to `xmsconan_gen`, and exported as `XMS_VERSION` so it reaches each profile's `[buildenv]` the way CI supplies it. |
| `--remote-name NAME` | `aquaveo-vs2019` | The remote the preflight check requires. Match it to the `--remote-name` you gave `setup`; a machine set up against a different Artifactory repo otherwise fails preflight (exit 2) on a remote it was never meant to have. |

Per library, in dependency order, `build`:

1. Skips the library (not a failure) when `<root>/<library>` or its `build.toml` is missing, so a partially migrated stack still builds what it can. A library whose matrix is emptied by `--filter` is likewise reported as `skipped`, with `no configurations matched --filter` in the notes column.
2. Runs `xmsconan_gen [--version V] build.toml` in the checkout, unless `--no-generate`.
3. Generates the `windows_vs2019` matrix, applies `--filter`, and runs `conan create` per configuration.

A single failing configuration does not stop the library — the packager runs the rest and reports the count. A failing *library* stops the run unless `--continue-on-error` is passed. Either way a summary table (attempted / succeeded / failed / elapsed per library) is printed at the end.

**Re-running is safe for the logs.** A configuration's log always lands at the canonical `<library>-<label>.log`; if one is already there from an earlier run it is *renamed* to `<library>-<label>.<timestamp>.log` first (with a `.1`/`.2` counter if two runs collide inside the same second). Re-running a failed matrix preserves the previous evidence rather than truncating it.

**`build` regenerates in place, and a partial run leaves that behind.** Step 2 runs `xmsconan_gen` inside your checkout, overwriting `conanfile.py`, `CMakeLists.txt`, and `build.py` and stamping them with the `--version` you passed. If the run fails, is interrupted, or you stop it after the summary, those regenerated files stay in the working tree — with the VS2019 version baked in. Check `git status` in each library before committing anything, or re-run `xmsconan_gen` with the version you actually want. `--no-generate` skips this step entirely and builds whatever `conanfile.py` is already there.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | At least one library built and none failed. |
| `1` | A library failed. |
| `2` | The request or the machine was wrong: an unknown `--only`/`--from` name, a `--filter` that isn't a JSON object, a `--python-versions` entry the packager rejects, a `--root` that doesn't exist, a selection that matches no library at all (`--only xmscore --from xmsgrid`, or a `--from` past the last enabled library), a failed preflight, `conan` or `xmsconan_gen` not on `PATH`, or a file the run needed that could not be read or written. The last two are told apart in the message: only an executable that would not start is reported as a `PATH` problem, and a `xmsconan_gen` that is missing stops the run here rather than being counted as one failed library (exit 1) — it would fail identically for every library after it. |
| `3` | The run completed but **nothing was built** — every selected library was skipped (no checkout, no `build.toml`, or `--filter` matched nothing). Not success: a typo in `--root` used to print a table of skips and exit 0, which any `&&` chain or wrapper script read as "the stack is built". |

### 16.5 The library list

The driver holds the XMS stack in dependency order — each library builds against the packages produced by the ones before it — with an `enabled` flag per entry:

```
xmscore (enabled) → xmsgrid → xmsinterp → xmsmesher → xmsextractor
→ xmsstamper → xmsconstraint → xmsgridtrace
```

**Only `xmscore` is enabled today**, because it is the only library with a Conan 2 recipe. As each of the others migrates, enabling it is a one-line change — flip `False` to `True` in the `LIBRARIES` tuple in `xmsconan/build_tools/vs2019_build.py`. Until then, `--only <lib>` builds a disabled library without touching the list.

### 16.6 The matrix — 14 configurations per library

With the default two Python versions, `windows_vs2019` (msvc 192, x86_64, cppstd 17) produces:

| Group | Count | Shape |
|---|---|---|
| base | 4 | `Release`/`Debug` × `static`/`dynamic` runtime |
| `wchar_t=typedef` | 4 | the same four, with the MSVC `/Zc:wchar_t-` toggle |
| testing | 4 | the same four, with `testing=True` |
| pybind | 2 | `Release` + `dynamic` runtime only, one per `--python-versions` entry |

`windows_vs2019` is a normal platform key of `XmsConanPackager.generate_configurations()`, so it can be driven directly too (the valid keys are `darwin`, `linux`, `windows`, `windows_vs2019`; anything else raises `ValueError` naming the unknown key and listing the valid ones).

Two things the platform key does *not* imply, which the driver passes explicitly:

- `XmsConanPackager(..., apply_boost_defaults=False)` — the `boost/*:without_stacktrace` and `without_locale` profile defaults name conan-center boost 1.86 options, and Conan fails a build when a profile sets an option no involved recipe declares. The legacy `boost/1.74.0.3` may not declare them. Pass one of them through `profile_options` if you need it individually.
- `upload(version, remote='aquaveo-vs2019', package_query='compiler.version=192')` — `XmsConanPackager.upload` still defaults to `aquaveo`, and without a `package_query` `conan upload` matches by *reference* only: every binary of that version sitting in the local cache is published. On a workstation that is both the VS2019 build box and a normal msvc 194 dev machine, that quietly pushes msvc 194 binaries onto the VS2019 remote and exits 0. `package_query` is passed through to `conan upload -p`.

### 16.7 `upload`

```bash
xmsconan_vs2019 upload --library xmscore --version 7.0.0
```

| Flag | Default | Effect |
|---|---|---|
| `--library NAME` | **required** | Library / Conan package name. |
| `--version V` | **required** | Package version. Every `<library>/<version>*` package in the local cache **whose `compiler.version` is 192** is uploaded. |
| `--remote NAME` | `aquaveo-vs2019` | Conan remote to upload to. Any other value is refused unless `--allow-other-remote` is also passed. |
| `--allow-other-remote` | off | Permit a `--remote` other than `aquaveo-vs2019`. |

Both `--library` and `--version` are required and there is deliberately **no `*` default** — a shared remote is the wrong place to discover that a wildcard matched more than you meant.

**Two guards keep msvc 192 binaries off the CI remote**, because the failure mode is silent and the cleanup is somebody else's afternoon:

- The upload is restricted to `compiler.version=192` (`conan upload -p`). Without it, `conan upload` matches by reference alone and takes the whole local cache — msvc 194 binaries included — along for the ride.
- `--remote aquaveo` is one word away from `--remote aquaveo-vs2019` and is **refused** (exit 2) with a message naming the right remote. Pass `--allow-other-remote` if you genuinely mean a different remote, e.g. a scratch repo.

**Exit code 0 means the packages actually landed.** A failing `conan upload` prints the reference, the remote, and conan's exit status, and the subcommand exits 1. `XmsConanPackager.upload()` returns `0`/`1` for this reason; the generated `build.py --upload` (§6) exits 1 on the same signal. `upload` runs no preflight, so `conan` missing from `PATH` surfaces here as exit 2 with a message rather than a traceback.

---

## 17. Credentials (`~/.xmsconan.toml`)

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

Always overridden by CLI flags / env vars when present. The `[conan]` section is the last fallback for `xmsconan conan-setup --login` and for `xmsconan vs2019 setup` (§16.2). **Don't commit this file.** It's read-only as far as xmsconan is concerned.

---

## 18. Consuming an XMS library from another project

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

### 18.1 Consuming the wheel (Python-only)

```bash
pip install xmscore -i https://public.aquapi.aquaveo.com/aquaveo/dev/+simple
```

Wheels are tagged `cp310-cp310-...` or `cp313-cp313-...`; pip picks the right one based on the active interpreter.

---

## 19. Troubleshooting

- **`auditwheel`/`delocate`/`delvewheel` missing libraries.** Run `build.py --wheel-dir wheelhouse` before repair — that step populates `wheelhouse/libs/`. Repairing without it produces a wheel that loads fine on the build host and crashes everywhere else.
- **`PYTHON_TARGET_VERSION` mismatch in CMake.** The recipe sets it from the `python_version` Conan option. If you're poking CMake directly, pass `-DPYTHON_TARGET_VERSION=3.13`.
- **`No pybind package found to extract`.** Means `build.py` ran but no pybind config was built. Check `build.py --preview` to see the matrix; common causes are `--filter` excluding the pybind variant, or every pybind variant having failed.
- **Dual wheel uploads colliding on devpi.** With `python_versions=["3.10","3.13"]`, wheels carry distinct `cp3XY` tags, so devpi treats them as separate uploads of the same release. No special config required.
- **Generated CI references a runner that doesn't exist.** Opt-in only matrices Windows, so the only new runner you need is `GLR-py310` (GitLab). If it isn't available, set `python_versions = ["3.13"]` until it is. The Linux/Mac side keeps using the existing 3.13 images.
- **`xmsconan vs2019 build` stops at preflight with "outside the pinned `~=2.31.0` series".** Intentional (§16.3). Install the pinned client — `pip install "conan~=2.31.0"` — rather than working around the check; a different minor can change package_ids and detach your build from what's already published.
- **VS2019 build fails on a boost option Conan says no recipe defines.** The legacy `boost/1.74.0.3` doesn't declare the conan-center 1.86 options. The driver already passes `apply_boost_defaults=False`; if you're constructing `XmsConanPackager` yourself for msvc 192, pass it too (§16.6).
- **VS2019 packages don't show up for consumers.** They go to `aquaveo-vs2019`, not `aquaveo` — the consuming machine needs that remote configured too. Note that `xmsconan_vs2019 setup` *appends* the remote rather than putting it first (§16.2), so it does not shadow `aquaveo` for your other work.
- **`xmsconan vs2019 build` exits 3 with a table of `skipped` rows.** Nothing was built. Almost always a typo in `--root` (each library is looked for at `<root>/<library>`), a `--filter` that matches no configuration, or a library that has no `build.toml` yet. See the exit-code table in §16.4.
- **`xmsconan vs2019 upload` refuses the remote.** `--remote` is restricted to `aquaveo-vs2019`; anything else needs `--allow-other-remote` (§16.7). This is deliberate — `--remote aquaveo` would publish msvc 192 binaries into the remote every CI consumer resolves against.

---

## 20. Reference: shipped Conan profiles

Located under `xmsconan/build_tools/profiles/{debug,release}/`. Use the basename with `xmsconan build --profile`. The match is on the **exact filename**, so only the names that actually exist work — the combinations below are the complete list, not a pattern to extrapolate from:

| Family | Release | Debug |
|---|---|---|
| GCC | `GCC5`, `GCC6`, `GCC7`, `GCC13`, each also `_TESTING` and `_PYBIND` | `GCC5_D`, `GCC6_D`, `GCC7_D`, `GCC13_D`, each also `_TESTING_D` |
| Clang | `CLANG9`, `CLANG9_TESTING`, `CLANG9_PYBIND`, `CLANG16_TESTING`, `CLANG16_PYBIND`, `CLANG17_PYBIND` | `CLANG9_D`, `CLANG9_TESTING_D`, `CLANG16_D`, `CLANG16_TESTING_D` |
| MSVC 192 (VS2019) | `VS2019`, `VS2019_TESTING`, `VS2019_TESTING_DYNAMIC`, `VS2019_PYBIND` | `VS2019_D`, `VS2019_TESTING_D`, `VS2019_TESTING_DYNAMIC_D` |
| MSVC 194 (VS2022) | `VS2022_TESTING` | `VS2022_TESTING_D` |

There is no bare `VS2022` profile, and no `VS2022_TESTING_DYNAMIC` in either flavor. `xmsconan build --profile VS2022` fails with `A valid --profile is required. Available profiles: [...]` listing everything that does exist. (The full matrix builds — `build.py`, `xmsconan vs2019 build` — don't use these files at all: `XmsConanPackager` writes a temporary profile per configuration. These are for configuring a single build tree by hand, §9.2.)

`profiles/base/` also sits on that walk, so its fragments (`vs_2019`, `release`, `x64`, `pybind`, …) are accepted as `--profile` values too. They are includes, not complete profiles; don't build with them directly.

Each suffix means:

- `_D` — Debug build
- `_TESTING` — Testing-enabled build (cxxtest/gtest runner)
- `_PYBIND` — Pybind-enabled build (Release only)
- `_DYNAMIC` (MSVC) — Dynamic CRT (`MD`/`MDd`) instead of static (`MT`/`MTd`)

For a custom mix, write your own profile that `include()`s entries from `xmsconan/build_tools/profiles/base/` and pass it via `--profile /path/to/profile`.
