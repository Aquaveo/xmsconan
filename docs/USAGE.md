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
| `xmsconan test-shards` | Run a staged gtest runner as N parallel shards inside the current container and merge their JUnit reports into one. What the generated GitLab `Run C++ Tests` job calls (see §10.2). |
| `xmsconan vs2019` | Drive the manual Visual Studio 2019 (msvc 192) build and publish it to the `aquaveo-vs2019` remote (see §16). Never runs in CI. |
| `xmsconan conan-setup` | Detect a Conan profile, add the aquaveo remote, optionally login. The password comes from `--password-file`, `$CONAN_PASSWORD`, or `~/.xmsconan.toml` — there is no `--password` flag (§17). |
| `xmsconan wheel-repair` | Run platform-appropriate wheel repair (auditwheel / delocate / delvewheel). |
| `xmsconan wheel-deploy` | Upload repaired wheels to devpi. |
| `xmsconan conan-deploy` | Save / restore / upload Conan packages between CI stages. |
| `xmsconan publish` | The full release pipeline (gen → build → repair → deploy). |

Run `xmsconan <cmd> --help` for the full flag set. The legacy underscored names (`xmsconan_gen`, `xmsconan_ci`, …) still work and are what the generated CI scripts call.

---

## 5. `build.toml` reference

`build.toml` is the **only** file you author for the build system. It controls everything xmsconan generates.

**An unknown top-level key is an error.** Every tool that reads the file — `xmsconan gen`, `ci`, `profiles`, `coverage`, `publish` (with or without `--docker`) and `vs2019` — rejects a key that is not in the tables below, naming it and listing what is accepted. Every optional key has a documented default, so a misspelling otherwise had no symptom at all — the default was kept and the generated artifact quietly was not what the file asked for. The same rule already applied to the `[ci]`, `[matrix]`, `conan_profile_variants` and `vs2019_dependency_overrides` sub-tables; it now covers the top level too. The `[ci]` table's key and type check now runs in every tool that reads the file, not only `xmsconan ci`. `testing_framework`, `python_binding_type` and the keys of `xms_dependency_options` are checked against their vocabularies at the same point.

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
| `xms_dependencies` | array[object] | `[]` | XMS sister libraries. Object shape: `{ name = "xmscore", version = "7.0.0", no_python = false }`. `no_python = true` excludes the dep from `_package/pyproject.toml`. Each entry must be a table with `name` and `version` as strings and, if present, `no_python` as a boolean; any other key is rejected, not ignored. |
| `xms_python_dependencies` | array[string] | `[]` | Extra **Python** requirements written into `_package/pyproject.toml`, in pip requirement form (`"geopandas"`, `"data_objects>=4.0.0"`). For runtime imports that are not XMS sister libraries and so have no entry in `xms_dependencies`. Appended after the XMS entries, in the order given. The Conan *dependency graph* is unaffected — nothing is added to `conanfile.py` — but these are real wheel requirements, so pip resolves them from an index during the Conan build when `pybind` is on (see §5.3). |
| `extra_dependencies` | array[string] | `[]` | Extra Conan deps in `"name/version"` form. Each entry is also wired into the generated `CMakeLists.txt` — a `find_package(<name> REQUIRED)` in the non-conda branch plus the matching `EXT_INCLUDE_DIRS` / `EXT_LIB_DIRS` / `EXT_LIBS` appends, exactly as `xms_dependencies` entries are. A header-only package defines no `_LIBRARY_DIRS` or `_LIBRARIES`; CMake expands an undefined variable to nothing, so those appends are harmless. Entries the generated `CMakeLists.txt` already finds are dropped from the CMake side — every `xms_dependencies` entry, plus `boost`, `zlib`, `pybind11`, `cxxtest` and `gtest`. That is not cosmetic: Conan publishes boost's and zlib's configs as `Boost` and `ZLIB`, so a lowercase duplicate is a hard configure error, and `pybind11` is found inside the `IS_PYTHON_BUILD` block where Python is available. So the CMake side and the recipe side apply the same rule. Deduplicated by package name against the deps the recipe adds itself (boost, zlib, the test framework, `pybind11`, every `xms_dependencies` entry) and against the rest of this list, keeping the first reference. Conan's duplicate check is by name, so a clash is a hard graph error no version dodges — this is what lets a library whose *static* half needs `pybind11` list it here without breaking its `pybind=True` configurations. An exact duplicate is dropped silently; an entry whose *version* differs from the reference that wins is a **warning** naming both, since what is being discarded is a pin somebody wrote on purpose. Entries are whitespace-stripped, and an empty entry is an error. |
| `extra_dependency_cmake_names` | object | `{}` | Override the **CMake** package name `find_package` is called with for an `extra_dependencies` entry, keyed on its Conan package name: `{ "xmdf" = "Xmdf" }`. Needed only when a package's CMake config does not use its Conan reference name. An empty string (`{ "somepkg" = "" }`) keeps the dependency in the Conan graph but leaves it out of the generated `CMakeLists.txt` entirely — for a package that ships no CMake config. Values must be strings, and a key naming a package that is not in `extra_dependencies` is **rejected**, not ignored: an ignored key's only symptom was a `find_package` failing on the *dependency*, pointing nowhere near the misspelled key. |
| `xms_dependency_options` | object | `{}` | Override an XMS dep's options. e.g. `{ "xmscore" = { "pybind" = false } }`. A key naming a package that is not in `xms_dependencies` is **rejected**, not ignored — an ignored key meant the override (usually one turning a dependency's `pybind` off) never applied, and the only symptom was a heavier build than asked for. |
| `vs2019_dependency_overrides` | object | `{}` | Replace an XMS dep's *reference* — but only on a Visual Studio 2019 (msvc 192) build. e.g. `{ "xmscore" = "xmscore/[>=6.0.1 <7.0.0]" }`. Matched on the package name before the first `/`; every other toolchain ignores it entirely. An entry may change the version or range only — renaming the package, or naming one that is not in `xms_dependencies`, fails the msvc 192 build. See §7.4. |
| `conan_profile_conf` | object | see §5.3 note | `[conf]` entries written into the `[conf]` section of every generated profile. Defaults to `{ "tools.cmake.cmaketoolchain:generator" = "Ninja Multi-Config", "tools.cmake.cmaketoolchain:user_presets" = "" }` — the generator is pinned so it does not vary by machine, and Conan's own `CMakeUserPresets.json` is disabled because `xmsconan` writes `CMakePresets.json` instead. An empty table (`{}`) omits the section entirely; with no generator pinned there is nothing to express, so no `CMakePresets.json` is written either. |
| `conan_profile_variants` | array[object] | `[]` | Additional renderings of the same settings under a different generator — the same configuration built with both Ninja and Visual Studio, for instance. Object shape: `{ name = "vs", platforms = ["windows"], kinds = ["testing", "python"], conf = { "tools.cmake.cmaketoolchain:generator" = "Visual Studio 17 2022" } }`. Only `name` is required. `platforms` accepts `linux`, `mac_os` (note the underscore — it is the profile-filename spelling of Conan's `Macos`), and `windows`; `kinds` accepts `library`, `python`, and `testing`. An omitted filter means "no restriction". A variant overlays `[conf]` only — settings and options are identical to the base rendering, which is what makes the pair comparable — and each match is written as `<stem>_<name>.txt` with its own CMake preset. Unknown keys and unknown `platforms` / `kinds` values are rejected when the profiles are generated, rather than silently producing no variant. |
| `conan_profile_options` | object | `{}` | Per-package options written into the `[options]` section of every generated profile. e.g. `{ "boost" = { "shared" = true } }`. The wildcard `"*"` is supported (e.g. `{ "*" = { "shared" = true } }`); a more specific entry overrides the wildcard. |

Boost (`1.86.0`) and zlib (`1.3.1`) are added automatically by the recipe. On a Visual Studio 2019 (msvc 192) build the recipe swaps in the legacy stack instead — the same two packages at the versions the `aquaveo-vs2019` remote publishes msvc 192 binaries for: boost `1.74.0.3` and zlib `1.2.11`. See §7.4 and §16.

### 5.4 Build configuration

| Field | Default | Description |
|---|---|---|
| `testing_framework` | `"cxxtest"` | `"cxxtest"` or `"gtest"`. Selects the test discovery / runner template in CMake. Any other value is rejected at generate time and again in the recipe's `configure()` — it used to add no framework requirement at all and fail much later with a CMake "cannot find cxxtest". |
| `python_binding_type` | `"pybind11"` | `"pybind11"` or `"vtk_wrap"`. Any other value is rejected the same way; it used to be silent end to end and ship a Python package with no native module in it. |
| `python_namespaced_dir` | derived | The submodule under `xms.<...>`. e.g. `"core"` produces `xms.core`. Defaults to `library_name` minus the `xms` prefix when omitted. |
| `pybind_root` | `false` | Whether this library hosts the root `xms` namespace. |
| `pybind_advertises_module` | `false` | Advertise the pybind module's import library (`_<name>`) to C++ consumers instead of the static library, and install the module to `bin/` + `lib/`. Windows-only in effect, and only for `pybind = True` packages. Opt-in because a consumer of the module sees only its exported symbols, and because `pybind` propagates down the dependency graph. See §7.5. |

### 5.4.1 Which configurations get built (`[matrix]` table)

The fan-out is otherwise fixed: on Windows, `build_type` × `compiler.runtime` gives 4 base configurations, plus a `wchar_t=typedef` copy of each, plus a `testing=True` copy of each, plus one pybind configuration per Python version — 13 for a single-version library. `[matrix]` narrows or widens that. Every key is optional; omit the table for the historical fan-out.

| Field | Default | Description |
|---|---|---|
| `[matrix].compiler_runtime` | `["dynamic", "static"]` | Which MSVC runtimes to build. Applied to the base matrix *before* the product, so the `wchar_t` and `testing` copies shrink with it — `["dynamic"]` turns 13 configurations into 7. **Inert on Linux and macOS**, which declare no `compiler.runtime`: one `build.toml` serves every platform, so a Windows-only statement must not fail elsewhere. Use it for a library nothing consumes a static-CRT build of. |
| `[matrix].wheel_only` | `false` | Build only what a wheel release needs: **Release with tests, Debug with tests, and the pybind build** — three configurations for a single-ABI library, down from 5 on Linux/macOS and 13 on Windows. The two testing legs are fixed; the pybind leg is not, so a library naming two `python_versions` gets four configurations and one naming two `pybind_build_types` as well gets six. It drops the library-only configurations (nothing consumes this library's Conan package, so a build with no tests in it proves nothing) and the whole `wchar_t=typedef` fan-out, and it narrows `compiler_runtime` to `["dynamic"]` unless the key is set explicitly — a pybind module links the dynamic CRT, so a static-CRT leg cannot match it. The three that remain differ **only** in `build_type` and `pybind`, which is the point: a wheel that passes its tests in a configuration nothing else shares is not evidence about the wheel that ships. Combine with `pybind_build_types` to choose which build type carries the pybind leg. |
| `[matrix].pybind_build_types` | `["Release"]` | Which build types get a pybind configuration. Add `"Debug"` for a library whose consumers link a Debug module (`bin/_<name>_d.<abi>.pyd` and its import library — that `bin/` install and the `_<name>_d` rename both require `pybind_advertises_module`; without it the Debug module keeps its plain name under `_package/`). **On Windows the Debug leg publishes no wheel and runs no Python tests** (§7.5); Linux and macOS Debug legs build and test theirs as usual. `XMS_COVERAGE=1` no longer adds `Debug` on top: `xmsconan coverage` takes its Python coverage from the Release pybind build, so this key alone decides the pybind legs. |

```toml
[matrix]
compiler_runtime = ["dynamic"]              # nothing consumes a /MT build of this library
pybind_build_types = ["Release", "Debug"]   # the desktop application links the Debug module
wheel_only = true                           # only the wheel ships; 3 builds, not 13
```

An unknown key, a value outside the accepted set (`"MD"`, `"RelWithDebInfo"`), or an empty list is rejected when the packager is constructed. An empty list would build nothing at all, which looks exactly like a successful build — omit the key instead.

The table reaches the packager as `CONAN_MATRIX` in the generated `conanfile.py`, which `build.py` forwards. `xmsconan vs2019` reads the `build.toml` in each checkout directly, so the same table applies on that track (§16).

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
| `[ci].linux` | `true` | Emit the Linux jobs. **GitLab only**, like `[ci].windows` — the GitHub templates ignore both. Setting it to `false` also removes `Repair Wheel`, `Wheel Deploy`, `Conan Deploy - Linux` and the `Package` stage: those three jobs are Linux-specific and consume the Linux job's artifacts through `dependencies:`. The Windows wheel jobs are unaffected — each platform stages and publishes its own wheel (§10.2). Intended for libraries that cannot build on Linux at all; see the note below. |
| `[ci].windows_wheel_repair` | **derived from `ci_type`**: `true` on `github`, `false` on `gitlab` | Repair the Windows wheel. `false` drops `xmsconan_wheel_repair --platform windows` from the Windows job on **both** GitLab and GitHub, drops the same step from `xmsconan publish` when it runs on Windows and from the `xmsconan vs2019` track, and passes `--skip-dependency-libs` to `build.py` so the Conan cache's DLLs are not staged for a repair that no longer happens. The unrepaired wheel is still built, uploaded as an artifact, and deployed. Windows-scoped by design — see §12.1 for the default's rationale. |
| `[ci].linux_arm` | `false` | Emit a Linux ARM job (GitHub only). |
| `[ci].deploy` | `true` | Emit deploy jobs (only run on tag pushes). |
| `[ci].coverage` | `false` | Emit a coverage job. On GitLab adds a `Coverage` stage + Pages upload; on GitHub adds a separate `Coverage.yaml` workflow. Both delegate to `xmsconan coverage`. Thresholds and filters come from `[coverage]` (see §5.7). |
| `[ci].xvfb` | `false` | Wrap test execution in `xvfb-run` (use for libraries that link X11/VTK). |
| `[ci].split_tests` | `false` | Split build and C++ test into two stages so testing artifacts can be reused. |
| `[ci].test_shards` | `0` | When >1, run the C++ tests as N gtest shards **inside a single container**. The recipe skips `cmake.test()` in this mode, so the shards are the only thing that runs the C++ tests — a missing runner binary fails the build rather than being skipped. On GitLab it needs `split_tests = true` and reaches the `Run C++ Tests` job as `xmsconan_test_shards --shards N`, which forks N runner processes and merges their JUnit XML into one `TEST-cxxtest.xml` (§10.2). On GitHub, where the build job runs its own tests, it reaches `build.py --test-shards N` in every platform job instead. This was previously GitLab's `parallel: N`, which bought its concurrency by starting N containers — each one repeating the container start, the `pip install` and the artifact download to run one Nth of the suite. |
| `[ci].docker_image` | `""` | Override the build container image (skips the default Aquaveo images). |
| `[ci].python_versions` | `["3.13"]` | Python versions to build **on Windows**. macOS and Linux take the highest entry unless they are given their own list below. Set to `["3.10", "3.13"]` to build a Windows 3.10 wheel + Conan binary in addition to 3.13. See §8. |
| `[ci].mac_python_versions` | highest of `[ci].python_versions` | Python versions the macOS matrix fans out across. Separate from `python_versions` so a Windows-only ABI (3.10 ships in Windows wheels for the desktop products) does not silently multiply the mac matrix. See §8. |
| `[ci].linux_python_versions` | highest of `[ci].python_versions` | Python versions the Linux and Linux-ARM matrices fan out across. Every entry needs a matching `conan-gcc13-py<version>` container to exist, which is why this tracks the published images rather than `python_versions`. See §8. |

All three lists are checked against the conanfile's `python_version` option at generation time. A version the recipe does not allow fails `xmsconan ci` with a message naming the supported set, rather than generating a matrix leg that dies later at `conan` configure time in CI.

### 5.7 Coverage thresholds (`[coverage]` table)

Consumed by `xmsconan coverage`; only relevant when `[ci].coverage = true` (or when you run the tool locally). All optional.

| Field | Default | Description |
|---|---|---|
| `[coverage].parallel` | `true` | Run the C++ and Python coverage builds at the same time rather than one after the other. The two builds are independent — different build types, different Conan packages, separate build folders — and each spends most of its wall clock compiling, so overlapping them roughly halves the coverage job. Each leg's output is buffered and replayed as one block when it finishes, so a compiler diagnostic still appears under the file name that produced it. Set to `false` for a library whose tests cannot tolerate a second concurrent client — an `[ci].xvfb` library whose image tests share one display is the case this exists for — or where a shared Conan cache is the bottleneck rather than the CPU. |
| `[coverage].cpp_threshold` | `0` | Minimum C++ line coverage percent. `xmsconan coverage` exits non-zero when gcovr reports below this. |
| `[coverage].python_threshold` | `0` | Minimum Python line coverage percent (pytest-cov). |
| `[coverage].filters` | `["<library_name>/"]` | gcovr `--filter` patterns. Defaults to the library's own source tree. |
| `[coverage].excludes` | `[".*\\.t\\.h$", ".*/_package/tests/.*"]` | gcovr `--exclude` patterns. Strips test fixtures and the Python test tree from the C++ measurement. The pybind layer is **not** excluded any more: it was, back when only the testing build was read — and that build does not compile the bindings, so the exclude removed nothing that existed. Now that the pybind build is instrumented and merged in, excluding it would collect the binding layer's coverage and then throw it away. Name it here to restore the old behavior. |
| `[coverage].python_version` | highest of `[ci].linux_python_versions` (default `"3.13"`) | The single Python ABI the pybind coverage build is pinned to. The fallback reads the **Linux** list because coverage only runs on Linux, and on GitLab the resolved version also selects the container image — taking the highest Windows entry could name a version with no published image. `xmsconan coverage` runs two builds: a `testing=True+pybind=False+Debug` build for C++ coverage (no ABI dependency) and a `pybind=True+testing=False+Release` build for Python coverage that gets pinned to this version. Multi-Python fan-out is intentionally collapsed on the pybind side so the Python report is deterministic. Override only when the highest CI version isn't the one you want to gate on. The resolved value is also exported as `PYTHON_TARGET_VERSION` into **both** coverage builds, so the matrix `build.py` generates can actually satisfy the `--filter` applied to it — `--filter` narrows an existing matrix and cannot introduce an ABI the matrix lacks. It **overrides** any ambient `PYTHON_TARGET_VERSION` (and logs a warning naming both values, plus the `[coverage].python_version` setting that would honor the one it ignored): the same resolved value drives the pybind `--filter` and the package lookup, and all three must agree or the build produces one ABI and the lookup searches for another. Like the `[ci]` lists, an explicit value here is checked against the conanfile's `python_version` option at resolution time — the coverage run reads this key directly rather than through the `[ci]` validation, so the check lives with the resolver and fires for `xmsconan ci` and `xmsconan coverage` alike. |

Both thresholds default to `0`, which means "report only, don't gate." Set them to real values once a baseline has been established.

An unknown `[coverage]` key is rejected, the same as an unknown `[ci]` key. Thresholds may be written as integers; they are read as floats. A boolean threshold, a `filters` or `excludes` value that is not a list, and an unquoted `python_version` (which TOML reads as a float) are rejected too.

### 5.8 Build matrix filter (`[filter]` table)

A baseline restriction on the configuration matrix, for libraries that should never build part of it (no Debug packages, no Python bindings, dynamic runtime only). The table uses exactly the shape `build.py --filter` takes as JSON: top-level Conan settings plus the nested `options` and `buildenv` tables.

```toml
[filter]
build_type = "Release"       # a top-level Conan setting
"compiler.runtime" = "dynamic"

[filter.options]
pybind = false               # this library ships no wheel
```

| Key | Accepted keys | Accepted values |
|---|---|---|
| top level | `os`, `arch`, `build_type`, `compiler`, `compiler.version`, `compiler.cppstd`, `compiler.runtime`, `compiler.libcxx` | A **single** value that some configuration actually carries — `"Release"`, not `["Release"]` and not `"release"`. Settings a platform doesn't emit (`compiler.runtime` off Windows) are ignored there rather than matching nothing. |
| `[filter.options]` | `wchar_t`, `pybind`, `testing`, `python_version` | `pybind` / `testing` take `true` or `false`; `wchar_t` takes `"builtin"` or `"typedef"`; `python_version` takes a **quoted** `"X.Y"` string that appears in any of `[ci].python_versions`, `[ci].linux_python_versions` or `[ci].mac_python_versions` — a version only one platform builds still counts (unquoted `3.13` is a TOML float and is rejected). |
| `[filter.buildenv]` | the names the generated profiles set: `XMS_VERSION`, `PYTHON_TARGET_VERSION`, `CI_COMMIT_TAG`, `RELEASE_PYTHON`, `AQUAPI_*`, `XMS_COVERAGE`, `XMS_TEST_ARTIFACTS_DIR`, `XMS_TEST_ARTIFACTS_LABEL`, `MACOSX_DEPLOYMENT_TARGET`, `_PYTHON_HOST_PLATFORM` | A single value, compared against what the profile sets. The **name** is validated, the value is not: most of these are read from the environment at build time (`XMS_VERSION`, `CI_COMMIT_TAG`, `AQUAPI_*`), so whether a pin matches is only knowable in the build environment and `xmsconan gen` does not judge a `[filter.buildenv]` pin unbuildable. |

Everything above is checked by `xmsconan gen` and `xmsconan ci`, including whether the filter as a whole selects anything: a combination no configuration can satisfy (`testing` and `pybind` both `true`, say) fails generation rather than every later build. That check reads `[matrix]` (§5.4.1) too, since that table decides which configurations exist at all — `[matrix] compiler_runtime = ["dynamic"]` with `[filter] "compiler.runtime" = "static"` is two individually valid statements that together build nothing, and is rejected by name.

How it flows through the generated files:

- **`xmsconan gen`** bakes the table into `build.py` as `BUILD_FILTER`. Every `python build.py` invocation applies it first, prints `Applying build.toml [filter]: …`, then applies any `--filter` on top — the two **AND** together. `--ignore-build-filter` skips it for a one-off build of an excluded configuration.
- **Filters that cancel out now fail.** When nothing survives, `build.py` prints which filters it applied and exits `1` instead of "succeeding" with zero packages built. `--preview` prints the same message but still exits `0` — it is the flag you reach for to diagnose this.
- **`xmsconan ci`** reads the same table:
  - The `build_type` matrix in every generated GitHub job keeps only the build types that still have configurations. A pinned `build_type` is the obvious case, but so is `[filter.options] pybind = true`: with the default `[matrix].pybind_build_types` of `["Release"]` the Debug leg has nothing to build, and a leg that builds nothing exits `1`. GitLab has no `build_type` matrix — its jobs run the whole matrix through `build.py`, which honors the filter anyway.
  - **The wheel steps** (repair, artifact upload, devpi deploy, and `--wheel-dir`) come out **per platform**, whenever that platform keeps no pybind configuration — which a filter can do without ever naming `pybind`. `options.pybind = false` is the explicit case; `build_type = "Debug"` against the default `[matrix].pybind_build_types` and `options.testing = true` (the testing and pybind variants are disjoint) both do it on every platform, and `"compiler.runtime" = "static"` does it on **Windows only** — msvc builds a pybind module for the dynamic runtime alone, while macOS and Linux declare no `compiler.runtime` at all and keep their wheels. `xmsconan_wheel_repair` fails on an empty `wheelhouse/`, and `build.py` exits `1` when `--wheel-dir` extracts no wheel, so a wheel-less library would otherwise get a red pipeline on every branch. This covers the Windows legs as well, which repair in place inside the build job rather than in a separate one: GitLab drops its `Repair Wheel` and `Wheel Deploy` jobs and `Wheel Deploy - Windows`, while the `Conan Deploy` jobs — which publish packages, not wheels — stay.
  - **`os`, `arch`, and `compiler*` pins are only warned about, not applied.** The platform fan-out lives in separate job blocks rather than a matrix axis, so there is nothing to narrow: `[filter] os = "Windows"` leaves the macOS and Linux jobs to fail with an empty matrix. `xmsconan ci` names each such job at generation time; drop the pin, or turn the job off through `[ci]`.
- **`xmsconan coverage`** builds Debug + `testing=True` (C++ report) and Release + `pybind=True` (Python report), pinned to `[coverage].python_version`. Because those two pin the *inverse* of each other, a filter conflicts by requiring an option as much as by excluding it — `pybind = true` cancels the C++ build just as `pybind = false` cancels the Python one. They also pin *different* build types, so any `build_type` pin cancels exactly one of them (§11). `xmsconan ci` warns about each conflict when `[ci].coverage = true`.

Two related changes reach repos with **no** `[filter]` table, since `--filter` and `[filter]` share one validator:

- A `--filter` value that could never match (a list, a misspelled option, `"release"`) now raises instead of silently matching nothing.
- A `--filter` naming a setting the running platform doesn't emit (`compiler.runtime` on Linux) is now a no-op there instead of an "Unknown filter key" error, so one filter can be used across platforms.

### 5.9 Example

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
| `wchar_t` | `"builtin"` / `"typedef"` | `"builtin"` | MSVC `/Zc:wchar_t-` toggle. Only `"typedef"` is built on MSVC (and is excluded from non-MSVC). The recipe sets `USE_TYPEDEF_WCHAR_T` from it, which is what the generated `CMakeLists.txt` gates the flag on. **Typedef packages published before the release carrying this fix do not carry `/Zc:wchar_t-`** — nothing set that variable between the Conan 2 migration and that release, so those binaries are builtin builds with a typedef `package_id`. Nothing in the package metadata distinguishes them -- it records the option, not the flag it failed to produce -- so age is the only signal: rebuild and republish the typedef variants of a library before consuming them (see the rollout order in §7.4). |
| `pybind` | `True` / `False` | `False` | Build the Python binding module + wheel. Allowed for any `build_type`. |
| `testing` | `True` / `False` | `False` | Build the test runner. Mutually exclusive with `pybind=True` in the standard packager fan-out — the coverage runner instruments each shape in a separate Conan create rather than combining them. |
| `python_version` | `"3.10"` / `"3.13"` / `"3.14"` | `"3.13"` | Which Python ABI to target when `pybind=True`. **Dropped from `package_id` when `pybind=False`**, so non-Python builds remain a single binary regardless. |

### 7.3 Required CMake variables (set by the recipe via `tc.variables`)

`PYTHON_TARGET_VERSION`, `IS_PYTHON_BUILD`, `BUILD_TESTING`, `XMS_TESTING_FRAMEWORK`, `XMS_VERSION`, `USE_TYPEDEF_WCHAR_T`. The generated `CMakeLists.txt` already wires these up; only relevant if you write `extra_cmake_text`.

All of them come from a single `_cmake_variables()` on the recipe, which feeds both `generate()` (the toolchain file) and `build()` (`cmake.configure`). Those were two hand-kept copies, and `USE_TYPEDEF_WCHAR_T` is what that cost: neither of them set it. `XMS_COVERAGE` is the one variable deliberately outside the shared set — it is added in `build()` only, so an instrumented build is never baked into a cached toolchain file.

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

### 7.5 What a package exposes to a C++ consumer

`cpp_info.libs` names **one** library. By default it is always the static library:

| `pybind_advertises_module` | Platform | `cpp_info.libs` | Where it is installed |
|---|---|---|---|
| `false` (default) | any | `<name>lib` (`<name>lib_d` on Debug) | `lib/` |
| `true` | Windows, `pybind = True` | `_<name>` (`_<name>_d` on Debug) — the *module's* import library | `bin/` holds the `.pyd`, `lib/` its import library |
| `true` | Windows, `pybind = False` | `<name>lib` — there is no module to advertise | `lib/` |
| `true` | Linux / macOS | `<name>lib` | `lib/` |

**`pybind_advertises_module` is opt-in per library, and Windows-only.** Three reasons it is not a default:

- A consumer that links the module gets only the symbols the module *exports* — `PyInit__<name>` plus anything explicitly `__declspec(dllexport)`. The static library gives it everything. Redirecting a consumer to the module is a deliberate trade, made by the one library whose consumers want it.
- `pybind` propagates down the dependency graph (`configure()` sets it on every `xms_dependencies` entry), so keying off the option alone would redirect every sister library in a pybind graph.
- Off Windows there is nothing to advertise. A macOS `MODULE` target is a bundle that cannot be linked, and a Linux module is `_<name>.cpython-313-x86_64-linux-gnu.so`, which the `find_library(NAMES _<name>)` that CMakeDeps emits does not match — so advertising it turns a working package into a configure error.

Where it does apply, the point is msvc-version skew: the consumer links the `.pyd` dynamically, which is what lets an msvc 192 application consume an msvc 194 build. The MSVC compatibility guarantee runs newer-consumes-older, so a static link across that boundary is unsupported.

Both libraries are in the package either way. An opted-in library installs the module twice on purpose: once under `_package/xms/<python_namespaced_dir>/`, the tree the wheel is built from, and once into `bin/` + `lib/`, because Conan's generators look only there. The import library is installed **by file name**, not through `install(TARGETS ... ARCHIVE)`: CMake tracks no `ARCHIVE` artifact for a `MODULE` target, so an `ARCHIVE` clause installs nothing and reports nothing — verified against CMake 3.28 and MSVC v143, with and without `ENABLE_EXPORTS`.

The `_d` suffix on the static library comes from `set(CMAKE_DEBUG_POSTFIX _d)`. It does **not** reach the pybind module target: `pybind11_add_module` calls `pybind11_extension`, which *overwrites* the target's `DEBUG_POSTFIX` with `PYTHON_MODULE_DEBUG_POSTFIX` — the `NAME_WE` of the interpreter's `EXT_SUFFIX`, which is the empty string for a release interpreter (`NAME_WE` of `.cp313-win_amd64.pyd`) — and that overrides the directory-scope value. Verified against the pinned `pybind11/3.0.1`; `2.9.1` set no `DEBUG_POSTFIX` at all.

So for an opted-in library the generated CMakeLists re-asserts `DEBUG_POSTFIX "_d"` on the module target under `if (WIN32)`, giving `bin/_<name>_d.<abi>.pyd` and `lib/_<name>_d.lib`. Three properties of that guard are deliberate:

- **`WIN32`, with no test on the postfix value.** `set_target_properties` assigns rather than appends, so re-asserting `_d` over a postfix that is already `_d` — a genuine debug Python build — is idempotent. Testing the value first would skip the re-assert for any *other* non-empty `PYTHON_MODULE_DEBUG_POSTFIX`, which a cross-compiling build is required to set and anyone can pass on the command line; the module would then carry a postfix `cpp_info` does not advertise, and nothing would fail until the consumer's link.
- **Windows only.** The import library exists only there, and off Windows the module must keep the name the shipped Python imports it by, because the shipped Python is what imports it; a library that names `Debug` in `[matrix].pybind_build_types` still builds and tests that module off Windows.
- **Gated on `pybind_advertises_module`, exactly like `cpp_info`.** The opt-in is what makes `_<name>_d` a name anything links. A library that has not opted in keeps pybind11's name, so its Windows Debug `_package` tree stays importable.

**A Windows Debug pybind configuration produces no wheel.** For an opted-in library the module there is `_<name>_d.<abi>.pyd`, and the shipped Python imports it as `xms.<dir>._<name>`, so a wheel built from that tree installs a module `import` cannot find and the Python tests fail on it. The generated `_package/pyproject.toml` does not catch this — its `ext-modules` entry is `sources = []` with `optional = true`, so setuptools neither compiles nor validates the name, and its package-data glob is `*.pyd` — the failure is the import itself. Renaming the module is not an option, since consumers link `_<name>_d` by that exact name.

`build()` therefore skips the wheel and the Python tests for **Windows** Debug, and the skip is logged. It is deliberately broader than the rename: it also covers a Windows Debug pybind build that has *not* opted in, and a `vtk_wrap` library, which has no `_<name>` module target at all. Neither is renamed, but every wheel step in the generated CI is gated on `Release` (§10.2), so a Debug wheel would be built only to be discarded.

Off Windows a Debug module keeps its importable name, and `build()` builds its wheel and runs its Python tests as usual. Coverage no longer depends on that path — its Python half is a `pybind=True, testing=False, Release` build (§5.7, §11) — but a library that asks for a Debug module still expects its wheel and its Python tests, so the build runs them.

**Naming note for anything migrating off Conan 1.** The static library is `<name>lib`, not Conan 1's `lib<name>`. Anything reading `cpp_info` is unaffected; a consumer hard-coding the old file name is not.

### 7.6 Python tests and dependency shared libraries

`run_python_tests()` builds a venv, installs the **unrepaired** wheel into it, and runs pytest. That puts the module outside the build tree, where CMake's build RPATH no longer applies — the installed module keeps only `$ORIGIN`, and the wheel does not carry its dependencies yet. A dependency built as a shared library (`laslib/*:shared=True`, say) is then unreachable and the test fails at import with `libFoo.so: cannot open shared object file` or `DLL load failed while importing _<name>`.

The recipe puts those libraries back on the loader's path, by a different route per platform:

- **Linux and macOS.** `generate()` declares a `VirtualRunEnv`, and pytest runs with `env="conanrun"`, so `LD_LIBRARY_PATH` / `DYLD_LIBRARY_PATH` name every dependency's library directories. Conan degrades gracefully here: if nothing needs a run environment, no `conanrun` script is written and the command runs unwrapped. The same run environment also carries the `PYTHONPATH` entry each pybind dependency exports from `package_info` (§7.5), so those `_package` directories are on `sys.path` during the tests as well as pip-installed into the venv. Same files from the same package folder, so the duplication is harmless.
- **Windows.** `PATH` does not work. Since Python 3.8 an extension module's dependent DLLs resolve only from the module's own directory, the system directories, and directories passed to `os.add_dll_directory` — so the `conanrun` `PATH` cannot carry them. The recipe writes a `sitecustomize.py` into the venv that registers each dependency's `bindirs` and `libdirs` at interpreter startup, before pytest imports anything. This is the same mechanism `delvewheel` injects into a repaired wheel.

This affects the **test** only. The shipped wheel is repaired separately (§12), which vendors the dependencies next to the module where `$ORIGIN` and the Windows module-directory search already find them.

A library whose dependencies are all static needs none of this and is unaffected.

---

## 8. Python version support (3.10, 3.13, 3.14)

xmsconan defaults to **Python 3.13 only** everywhere. Each platform opts in to more
versions through its own list, because what limits the fan-out differs per platform:

```toml
[ci]
python_versions       = ["3.10", "3.13", "3.14"]   # Windows
mac_python_versions   = ["3.13", "3.14"]           # macOS
linux_python_versions = ["3.13", "3.14"]           # Linux + Linux-ARM
```

Both `mac_python_versions` and `linux_python_versions` default to the **highest**
entry of `python_versions`, which is the behavior those platforms had when the
version was hardcoded — so a project that sets none of them generates the same CI
it did before.

Why three lists instead of one:

- **Windows** interpreters all come from `actions/setup-python` (GitHub) or a
  `GLR-pyXYZ` runner tag (GitLab), so a version costs only runner minutes. 3.10
  lives here because the desktop products (GMS/SMS/WMS) consume a 3.10 Windows
  wheel; nothing else needs it.
- **macOS** is cheap to fan out too, but inherits nothing from the Windows list —
  adding 3.10 for the desktop products should not triple the mac matrix.
- **Linux** runs in a container, and each version needs a published
  `conan-gcc13-py<version>` image. Only **3.13 and 3.14** images exist; there is
  no `conan-gcc13-py3.10/3.11/3.12`. Naming a version with no image yields a job
  that cannot start, which is exactly why this list is not derived from
  `python_versions`.

What opting in turns on:

- **CI matrices expand.** GitHub Actions: `python-version` on the mac, linux,
  linux-arm and windows jobs from their respective lists. GitLab: `parallel:matrix`
  over `PYTHON_TARGET_VERSION` on `Conan Build` / `Conan Deploy - Linux` (from
  `linux_python_versions`) and on `Conan Build - Windows` / `Conan Deploy - Windows`
  (from `python_versions`, which also derives `PY_TAG`).
- **Linux container images follow the matrix leg** —
  `ghcr.io/aquaveo/conan-gcc13-py${{ matrix.python-version }}:latest` on GitHub and
  `…/conan-gcc13-py${PYTHON_TARGET_VERSION}` on GitLab. An explicit
  `[ci].docker_image` still overrides both outright.
- **Artifact names grow an ABI suffix, but only where a platform actually fans
  out.** A platform left on one version keeps the exact `MATRIX_NAME`, wheel-artifact
  and release-asset names it published before, since release assets are fetched by
  exact name.
- **Conan binaries.** Each pybind variant carries the `python_version` option in its
  `package_id`, so consumers select `xmscore/X.Y.Z@… pybind=True python_version=3.14`.
  Non-pybind builds drop `python_version` from `package_id`, so testing/plain-library
  binaries remain a single shared binary regardless.
- **Wheel output.** Each version produces its own `cp3XY` wheel; pip on the consumer
  side picks the right one.

Wheel repair is unaffected: `xmsconan_wheel_repair` only hosts `auditwheel`, which
keys off each wheel's own tags, so the manylinux `cp313-cp313` interpreter repairs a
`cp314` wheel fine.

For local builds (`python build.py`), the matrix is single-version: it uses
`PYTHON_TARGET_VERSION` from the environment if set, otherwise `3.13`. To build
several wheels locally, invoke `python build.py` once per version (or construct
`XmsConanPackager` directly with `python_versions=["3.13", "3.14"]`).

`xmsconan coverage` is the exception: it sets `PYTHON_TARGET_VERSION` itself from
the resolved coverage ABI (§5.7) before invoking `build.py`, so a local coverage run
needs no manual export — and an ambient value does not win over `build.toml`.

Either way, a local pybind build only works **from an interpreter of the version being built**: the recipe points CMake at `sys.executable` and the generated `CMakeLists.txt` requires that exact version. CI never notices because `actions/setup-python` supplies a matching interpreter; on a workstation you supply it, which is why the VS2019 wheel workflow is one run per Python version from a virtual environment of that version (§16.8).

> **Runner / image expectations.** A Windows opt-in assumes the matching `GLR-pyXYZ` GitLab runner tag exists (`GLR-py310`, `GLR-py314`). A Linux opt-in assumes the matching `conan-gcc13-py<version>` image exists **and is readable by CI** — on GHCR the 3.13 image is public while 3.14 is private, and the generated `container:` block pulls anonymously, so a private image needs its visibility flipped (or a `credentials:` stanza added) before its leg can start.

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
| `--filter '{"build_type": "Release"}'` | Restrict to a subset of the matrix. Keys *and values* match the configuration dict (`build_type`, `arch`, `compiler`, `options.pybind`, `options.python_version`, …) and are validated the same way as the `[filter]` table (§5.8). A configuration that does not carry the filtered `options`/`buildenv` key does **not** match it — `'{"options": {"python_version": "3.13"}}'` selects only pybind configurations, since `python_version` is set on those alone. Applied on top of `[filter]`, not instead of it. |
| `--ignore-build-filter` | Drop the `[filter]` table baked in from `build.toml`, for a one-off build of a configuration it excludes. |
| `--python-only` | Equivalent to `--filter '{"options": {"pybind": true}}'`. |
| `--preview` | Print the configuration table and exit. Nothing is built. |
| `--build-missing` | Pass `--build=missing` to `conan create`. |
| `--wheel-dir DIR` | After the build, copy each `pybind` package's wheel into `DIR`. With `python_versions=["3.10","3.13"]`, you get one wheel per version. **A run that asked for wheels and got no complete set exits 1** — the flag is a request, not a hint, so only pass it where a wheel is expected. A matrix with no pybind configuration at all (`--filter '{"build_type": "Debug"}'` with the default `[matrix].pybind_build_types`, §5.4.1) is that case. |
| `--repair` | Run `repair_linux_wheel` after extraction (Docker required). |
| `--artifacts-dir DIR` | Save per-config test artifacts (LastTest.log, runner binary, `_package/`, `test_files/`) for debugging. |
| `--test-shards N\|auto` | Run the gtest suite as N shards after the build instead of during it. The recipe skips `cmake.test()`, the runner binary is invoked once per shard with `GTEST_TOTAL_SHARDS`/`GTEST_SHARD_INDEX` set, and a shard that fails fails the build. `auto` is half the CPU count (minimum 2) — half rather than all, because each shard is a full test process with its own memory and I/O. **Requires `--artifacts-dir`**, which is where the runner is sharded from: the two flags together are what the generated CI passes. `--test-shards` alone is refused, because the recipe would skip `cmake.test()` for every testing configuration and nothing would then run the suite. Set `[ci].test_shards` to have the generated CI pass both for you (§5.6). |
| `--skip-dependency-libs` | Do not stage the Conan cache's shared libraries into `<wheel-dir>/libs`. They exist only so the repair tools can resolve imports, so this is for a build whose wheel is not repaired — the generated CI passes it automatically when `[ci].windows_wheel_repair` is off (§12.1). |
| `--version VERSION` | The version `--upload` publishes and `--wheel-dir` stages. Defaults to `$XMS_VERSION` — the variable every generated CI leg sets — and falls back to the glob `*`. |
| `--skip-build --upload` | After a successful build, push the matrix to the Conan remote. **Refuses to run without a concrete version** -- `*`, or empty, which is what an exported-but-unset `XMS_VERSION` yields: as a glob it matched every version of the library in the local cache, and whatever it matched is on the shared remote afterwards. Pass `--version` or set `XMS_VERSION`. |

`xmsconan gen` and `xmsconan profiles` also **remove** any `.txt` profile in `conan_profiles/` that the current matrix does not produce, so narrowing `[matrix]` does not leave stale profiles behind for an IDE to pick. `CMakePresets.json` is a single rewritten file and was always clean; the two now agree. Non-`.txt` files in that directory are left alone.

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
- `-DXMS_GTEST_DISCOVER_TESTS=ON` — Register every gtest case as its own ctest test. Off by default: `gtest_discover_tests` makes `ctest` spawn one process per `TEST_F`, and for a suite of a few hundred cases the process starts cost more than the tests. The default registers the runner as a single ctest entry that runs the whole suite in one process; parallelism comes from `--test-shards` instead. Turn it on when you want `ctest -R` to select an individual case by name.
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
3. **Build** (`python build.py --filter='{"build_type": "<type>"}' --artifacts-dir test_artifacts`, plus `--wheel-dir wheelhouse` on the Release leg only — see §10.1)
4. **Repair wheel** on Release (`xmsconan_wheel_repair --wheel-dir wheelhouse`)
5. **On tag pushes:** `xmsconan_wheel_deploy` and `xmsconan_conan_deploy … --upload`

### 10.1 GitHub specifics

- Mac matrix: `build_type × python-version=ci_mac_python_versions`.
- Linux / Linux-ARM matrices: `build_type × python-version=ci_linux_python_versions`.
- Job `name:` carries the version on any platform that fans out (`GCC-13 (Release, 3.14, Linux)`). GitHub uses an explicit `name:` verbatim and only auto-appends matrix values when none is given, so without this two legs would share one status-check name — ambiguous in the checks list and in branch-protection matching. A single-version platform keeps its original name, so existing required checks keep matching.
- **Windows** matrix: `build_type × compiler-version × python-version=ci_python_versions`.
- The `build_type` axis defaults to `[Release, Debug]` and shrinks to whatever the `[filter]` table leaves buildable (§5.8) — a `build_type` pin, or any filter that empties a whole build type. The wheel repair / upload / deploy steps drop per platform when that platform builds no wheel. An `os`/`arch`/`compiler` pin is *not* applied — those are separate job blocks, so `xmsconan ci` warns about the jobs it would empty instead.
- Wheel artifacts carry `-py${{ matrix.python-version }}` on any platform that fans out; a single-version platform keeps its bare `wheel-${{ runner.os }}` name.
- Linux containers resolve to `conan-gcc13-py${{ matrix.python-version }}:latest`.
- **`--wheel-dir` is passed on the Release leg only.** Every step that consumes the wheel — repair, artifact upload, devpi deploy — is gated on `matrix.build_type == 'Release'`, and `[matrix].pybind_build_types` defaults to Release only (§5.4.1), so a Debug leg has no pybind configuration to extract a wheel from. `build.py` exits 1 when `--wheel-dir` yields no complete set of wheels (§9.1), which is the right answer on a Release leg and wrong on a Debug one: on Windows a Debug pybind configuration produces no wheel by design (§7.5), and on Linux and macOS a Debug wheel would only be built and discarded. GitLab is unaffected — its build step runs the whole matrix in one invocation, so the Release pybind configuration is always in scope.
- `flake` deliberately stays on a single hardcoded interpreter — linting is ABI-independent, and pinning it keeps lint results identical across repos.
- Third-party actions are referenced by **commit SHA**, with the tag in a trailing comment (`uses: nelonoel/branch-name@1ea5c86…  # v1.0.1`). A tag is a movable ref in a repository Aquaveo does not control, and the job it runs in holds the Conan and devpi secrets. `actions/*` stays on tags — a compromise of GitHub's own namespace is a compromise of the runner regardless. To move a pin, resolve the new tag with `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha` and edit the **template**. `test_github_ci_pins_third_party_actions_to_a_sha` fails on any third-party action added by tag.
- The `flake` job installs xmsconan, runs `xmsconan_gen build.toml` to render `.flake8`, then runs plain `flake8 _package`. It deliberately does **not** pass flake8 settings on the command line: that duplicates `.flake8.jinja`, and the two copies drift apart silently (CI once used a different `ignore` list and a stale `conf.py` exclude, so a clean local run did not imply a clean CI run). Change lint settings in `.flake8.jinja` only. It installs `flake8-tidy-imports` alongside the other plugins because `.flake8.jinja` sets `banned-modules` — an option only that plugin registers. flake8 accepts config options no installed plugin claims and enforces nothing, so without it the `osgeo.*` ban reported the same green as a run that had checked it, while GitLab (which already installed the plugin) linted to a stricter rule.

### 10.2 GitLab specifics

- Jobs invoke `build.py` without a `--filter`, so a `[filter]` table in `build.toml` (§5.8) applies with no template change — GitLab has no `build_type` matrix axis to narrow. A filter that leaves a platform with no pybind configuration drops that platform's wheel work: the `Repair Wheel` and `Wheel Deploy` jobs and the Linux `--wheel-dir` for Linux, and `Wheel Deploy - Windows` plus the in-place repair step for Windows. The two are independent — `"compiler.runtime" = "static"` takes out only the Windows side. The `Conan Deploy` jobs publish packages rather than wheels and are unaffected.
- `Conan Build` and `Conan Deploy - Linux`: `parallel:matrix` over `PYTHON_TARGET_VERSION` from `linux_python_versions`, each leg saving/restoring its own `-py<version>` export tarball. With a single version they stay plain jobs with no `parallel:matrix`, exactly as before.
- `Repair Wheel` and `Wheel Deploy` stay single jobs: they collect every leg's wheels and act on all of them at once.
- `Coverage` pins both its container image and `PYTHON_TARGET_VERSION` from the resolved coverage ABI (`[coverage].python_version`, §5.7), so the interpreter inside the image and the ABI the pybind build targets are the same value. Under an explicit `[ci].docker_image` only the pin is resolved — the image is whatever the repo named, so keeping that image's interpreter in step is the repo's job. `xmsconan_coverage` exports the variable itself, so the job is correct without it; it is declared here as well so the pairing is visible in one place, and so a regression in the tool cannot pass unnoticed on GitLab specifically. Unlike `Conan Build`, `Coverage` never fans out — coverage commits to a single ABI by design.
- `[ci].split_tests` cannot be combined with a multi-version `linux_python_versions` — the C++ test job takes the build job's artifacts by name, so a multi-ABI build would leave it testing an indeterminate one. `xmsconan ci` rejects that combination at generation time.
- **`Run C++ Tests` shards inside one container.** With `[ci].split_tests = true` the job downloads the build job's `test_artifacts/`, then runs `xmsconan_test_shards`, which locates the testing artifact directory, restores the executable bit on the runner (a GitLab artifact round-trip drops it), relinks `test_files/` next to the runner, and forks `[ci].test_shards` runner processes with `GTEST_TOTAL_SHARDS`/`GTEST_SHARD_INDEX` set. Each shard writes its own gtest XML, which the tool merges by suite name into `TEST-cxxtest.xml` and declares as `artifacts:reports:junit`, so a failing case shows up in the MR widget by name. Per-shard console output is held and replayed as one contiguous block rather than interleaved. A shard that dies before writing XML — a timeout, a segfault — contributes a synthetic error suite to the merged report, so a red job never shows an all-green test tab. Without `test_shards` the same script runs a single shard: one code path, so the artifact discovery and `test_files` relinking do not have to exist twice. With `[ci].xvfb = true` each shard gets its own X display, because `xvfb-run -a` picks a free server number and N simultaneous starts race for it.
- `Conan Build - Windows`, `Conan Deploy - Windows`: `parallel:matrix` over `PYTHON_TARGET_VERSION` from `python_versions`. The matrix also sets `PY_TAG` (`py310` / `py313` / `py314`) which selects the runner via `image: GLR-${PY_TAG}`.
- Wheel-repair always runs `cp313-cp313`'s `xmsconan_wheel_repair` inside the manylinux container; auditwheel itself doesn't care about the host Python.
- Required CI variables: `AQUAPI_URL`, `AQUAPI_USERNAME`, `AQUAPI_PASSWORD` (for wheel deploy).
- `[ci].linux = false` yields a **Windows-only pipeline**: `Conan Build - Windows`, `Lint`, and (on tags) `Wheel Deploy - Windows` and `Conan Deploy - Windows`. Two combinations are rejected at generation time rather than producing a pipeline that fails opaquely later — `linux = false` together with `windows = false` (nothing would build), and `linux = false` with `coverage = true` (coverage compiles with `--coverage` under gcc, and the generated `CMakeLists.txt` rejects MSVC when `XMS_COVERAGE` is set).
- **Each platform stages and publishes its own wheel.** Linux builds one, repairs it in the Package-stage `Repair Wheel` job, and uploads it from `Wheel Deploy`. Windows passes `--wheel-dir wheelhouse` and then repairs *in place* inside `Conan Build - Windows`, because `delvewheel` reads the DLL imports of a `win_amd64` `.pyd` and only runs on a Windows host — the manylinux container that repairs the Linux wheel cannot stand in. `Wheel Deploy - Windows` uploads the result on tags from a plain `python:3.13` image, since a devpi upload needs no MSVC toolchain. A Windows-only pipeline therefore publishes wheels, and the manual VS2019 track (§16.8) is no longer the only route. `[ci].windows_wheel_repair = false` drops the Windows repair step for a library that has nothing to vendor — see §12.1; the deploy job is unaffected either way.
- **The Windows cache snapshot is best-effort, and says so.** `Conan Deploy - Windows` copies the Conan cache into a `conan_packages/` artifact for debugging after the upload has already succeeded. It used to end in `|| true`, which made "copied nothing" indistinguishable from "copied everything" — a runner whose home directory is not `/c/Users/admin` shipped an empty artifact with no line in the log. It now prints a warning instead, and stays non-fatal: nothing consumes the artifact, and the upload it follows fails loudly on its own.
- **The Windows wheel jobs fan out with the build matrix.** `Conan Build - Windows` runs once per `PYTHON_TARGET_VERSION`, and each instance stages its own wheel into `wheelhouse/`. Each wheel's own ABI tag (`cp310` / `cp313` / `cp314`) keeps the files distinct, so the merged artifact holds every wheel and one `Wheel Deploy - Windows` job uploads the set.

### 10.3 Toolchain versions

Generated jobs constrain the two tools they install, in opposite directions:

| Tool | Spec | Why |
|---|---|---|
| `xmsconan` | `>=<version that generated the file>`, always with `--upgrade` | An xmsconan fix reaches a repo on its next CI run, rather than requiring a regenerate-and-commit pass across the whole suite. The floor still rules out resolving a version older than the templates were generated against. |
| `conan` | `~=2.31.0` (patch series) | Conan computes package_ids and runs the compatibility plugin; a minor bump can silently detach a build from binaries already on the remote. The coverage workflow carries the same pin — it used to install conan unpinned, so the run that reports coverage could resolve different package ids than the run that builds. |

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
2. Sets `XMS_COVERAGE=1` and invokes `build.py` twice — **concurrently by default**, see `[coverage].parallel` (§5.7):
   - First: `--filter '{"build_type":"Debug","options":{"testing":true,"pybind":false}}'` — Conan create that builds the library + CxxTest runner under `--coverage`, then runs the runner. Produces the `.gcda` set gcovr reads.
   - Second: `--filter '{"build_type":"Release","options":{"pybind":true,"testing":false,"python_version":"3.13"}}'` — Conan create that builds the library + pybind wheel, then runs `pytest-cov` against the wheel. Instrumented like the first, so the binding layer -- C++ that only a Python test can reach -- produces its own `.gcda`. Release is not a problem for line data: the `XMS_COVERAGE` block appends `-O0 -g` after CMake's `-O3`, so the build is unoptimized either way, while still resolving against Release dependencies. Produces `cov-py.xml`, `cov-py-summary.json`, and `coverage-html-py/` inside the build folder.

   The two coverage builds pin the inverse of each other, and they pin *different* build types — Debug for the CxxTest half, Release for the pybind half — so a `[filter]` table (§5.8) conflicts by *requiring* `testing`/`pybind` as much as by excluding it, and any `build_type` pin cancels one of the two outright. Either way one of the builds is left with no configurations and `build.py` exits non-zero. A `python_version` pin other than `[coverage].python_version` cancels the Python build the same way. `xmsconan ci` warns about each conflict at generation time.

   `XMS_COVERAGE=1` rides into the `[buildenv]` of every profile so CMake adds `--coverage` there. It no longer changes which configurations exist: the testing-only Debug variant is part of the standard packager fan-out either way, and the Python half reuses whatever pybind configuration `[matrix].pybind_build_types` already names. Pybind profiles are injected too: the binding layer is C++ that only a Python test can reach, and gcovr merges that build's `.gcda` into the C++ report. The recipe separately keys `pytest-cov` off `XMS_COVERAGE` in the inherited process environment, which is why Python coverage is collected from the same build. Requiring a Debug pybind build used to cost every dependency a `Debug`+`pybind` binary — the one combination the xms libraries do not publish — and would need a debug interpreter on Windows. `python_version` on the pybind build is pinned to one ABI (highest of `[ci].linux_python_versions` by default, or `[coverage].python_version` when set) so multi-version fan-outs cannot non-deterministically pick whichever pybind config finished last.
3. Locates the two build folders in the local Conan cache and runs gcovr against **both**. Each folder is read separately with its own `--root` -- one invocation cannot span two roots, and `--root` has to match the absolute paths embedded in that folder's `.gcno` files -- writing a JSON tracefile each. A final gcovr run combines them with `--add-tracefile` into `cov-cpp.xml`, `cov-cpp-summary.json`, and `coverage-html-cpp/`. Tracefile paths are relative to `--root`, so the two folders' copies of the same source line up and hit counts sum: a line the CxxTest runner never reaches still counts as covered when a Python test reaches it through the bindings. The pytest-cov artifacts are copied out of the pybind build folder as before.
   The two legs write into separate Conan build folders and share no state, so running them together changes only the wall clock. Their console output is captured per leg and replayed whole once that leg finishes rather than interleaved line by line, which would otherwise split every compiler diagnostic away from the file name printed above it. A leg that fails — or cannot start at all — is recorded and reported after both have finished, so the other leg's reports are still produced.
4. Writes `cov-cpp.xml`, `cov-py.xml`, `cov-cpp-summary.json`, `cov-py-summary.json`, `coverage-html-cpp/`, and `coverage-html-py/` into `--output_dir`.
5. Compares the line-coverage percent for each layer against `[coverage].cpp_threshold` / `[coverage].python_threshold` and exits non-zero on regression. The tool also exits non-zero if `build.py` reported a test failure in either layer — but only *after* still producing gcovr reports, copying artifacts, and (if `$GITHUB_STEP_SUMMARY` is set) appending the markdown summary, so the coverage data and the failing-test signal are both visible in the same run. If a coverage summary file is present but missing its expected keys (gcovr/pytest-cov schema drift, truncated write), the tool raises rather than reporting a misleading 0%. A `cov-py-summary.json` that is *absent* fails the Python layer outright: the pybind build in step 2 always runs with `XMS_COVERAGE=1`, so there is no configuration in which pytest-cov legitimately writes nothing, and `python_threshold` defaults to `0` — treating the absent file as 0% would report PASS for a layer that was never measured.

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
| `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` | you | Read by pip itself, not by xmsconan. The wheel install at the end of a pybind build resolves `xms_python_dependencies` from whatever index pip is configured with; set these when any of those requirements live on a private index. |
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

Each tool is installed into the interpreter running `xmsconan_wheel_repair` and then invoked by the absolute path it was installed to, resolved against that interpreter's script directory. Installing xmsconan as a `uv tool` leaves that directory off `PATH`, so a bare-name invocation would fail with `FileNotFoundError` even though the install had just succeeded.

`build.py` already populates `wheelhouse/libs/` for you when `--wheel-dir` is set — `collect_dependency_libs` copies **every** `.dll` / `.so` / `.dylib` anywhere in the Conan cache, which is routinely several hundred files. After repair, the original `wheelhouse/` is replaced with the repaired version (the `libs/` directory is removed).

### 12.1 Opting out on Windows (`[ci].windows_wheel_repair = false`)

Windows repair is the one that can be turned off, because it can make a wheel worse.

delvewheel's default ignore lists excuse `vcruntime140.dll` and `vcruntime140_1.dll` for a CPython wheel (they ship with the interpreter), and `python3xx.dll` / `api-ms-*` by regex. **`msvcp140.dll` is on none of them.** Any `.pyd` built with MSVC imports it, so delvewheel treats it as a needed dependency and vendors it — mangled to `msvcp140-<hash>.dll`, with the `_delvewheel_patch` hook rewriting the module's import table to point at the wheel's private copy. It will always find one: `--add-path {wheel_dir}/libs` holds everything from the Conan cache, and `System32` is on `PATH` besides.

For a library whose `.pyd` statically links everything and imports nothing third-party there is nothing legitimate to bundle, so the entire outcome of repair is a second C++ runtime inside the process. That matters where the host application supplies the runtime itself — GMS/SMS/WMS scrub `PATH` in `dmGetScriptEnvironment` and point it at their own shipped `ms_redist_*` DLLs precisely so one CRT is in play.

**The default is derived from `ci_type`, not hardcoded**, because `ci_type` is a proxy for who installs the wheel:

| `ci_type` | Default | Why |
|---|---|---|
| `github` | `true` | Those wheels are published for installation into arbitrary Python environments, which have no XMS runtime on `PATH`. The DLLs a module needs must travel with it. |
| `gitlab` | `false` | Those wheels are internal, and the only thing that loads them supplies the C++ runtime itself. Repairing them vendors a private mangled copy of the runtime the host is deliberately controlling. |

A flat default would be wrong in one direction or the other: `true` everywhere would start the GitLab repos repairing wheels they previously never even staged, and `false` everywhere would stop the GitHub repos bundling DLLs their users need. Set the key explicitly to override either way:

```toml
[ci]
windows_wheel_repair = false
```

Every reader — the CI generator, `xmsconan publish`, and the `xmsconan vs2019` driver — resolves this through one function, and the whole `[ci]` table is validated against a key allowlist with per-key types at generation time. A misspelled key (`windows_repair_wheel`) or a quoted boolean (`"false"`) is rejected rather than falling back to the default, because for a switch that turns work *off* the default means the work keeps happening and the only symptom is the harm the switch exists to prevent.

That removes the repair step from the generated GitLab and GitHub Windows jobs, skips it in `xmsconan publish` when it runs on Windows, and passes `--skip-dependency-libs` to `build.py` so the hundreds of cache DLLs are not staged for a step that no longer runs. The wheel is still built, still uploaded as an artifact, and still deployed — unrepaired, which for such a library is what it already was in substance.

There is no equivalent switch for Linux or macOS. A Linux wheel has to be repaired to carry a `manylinux` platform tag and to bundle `libstdc++.so.6`; skipping it would publish something pip cannot install portably.

Confirm what a given wheel actually got by unzipping the repaired output: a vendored CRT shows up as `xms/<subdir>/*.libs/msvcp140-<hash>.dll` alongside a `_delvewheel_patch` entry in the package `__init__.py`.

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

**Prefer sources 2 and 3 for the password.** `--password` puts it in this
process's argv, and from there into your shell history, `ps` output, and —
on a managed Windows box — the Event 4688 / Sysmon record that gets shipped to
the SIEM in cleartext. This is the one place xmsconan still passes a password
on a command line: `devpi-client` reads no password environment variable and
its `getpass` fallback reads the console rather than a pipe, so there is no
drop-in replacement yet. Everything else (`conan-setup`, `vs2019 setup`,
`publish --docker`) keeps the secret in an environment variable or a file.

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

# 5. Python wheels are a separate pass, one per Python version — see §16.8
```

**`build` and `upload` are separate verbs on purpose: a build never uploads as a side effect.** The run takes hours and its output lands on a remote other people build against, so a human looks at the summary table before anything is published. There is no `--upload` flag on `build`. Wheels follow the same rule: `build --wheel-dir` stages them, and `xmsconan_wheel_repair` / `xmsconan_wheel_deploy` are the commands that repair and publish (§16.8).

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
| python interpreter *(`build` only)* | The matrix that is actually about to be built contains **no pybind configuration**, or every pybind configuration targets the Python version this shell is running. | Re-run from a virtual environment of the requested version, or pass `--python-versions <the version you are running>`. Full explanation in §16.8. |

The interpreter check is the one check that depends on the *request* rather than the machine, so it runs after the matrix has been generated and `--filter` applied, and prints into the same block. That scoping is deliberate: all but the pybind configurations have `pybind=False` and don't care which interpreter is running, so building only those from whatever virtualenv you happen to be in keeps working. `setup` doesn't run it — there is no matrix at that point.

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
| `--python-versions X.Y [X.Y …]` | `3.10 3.13` | Python versions the pybind variants fan out across. A pybind configuration only builds when this matches the Python running conan, so wheels are built one version per run — §16.8. |
| `--wheel-dir DIR` | — | After the build, copy each pybind package's `.whl` into `DIR` and fill `DIR/libs` with the shared libraries the repair step needs. Repair and publish stay separate commands (§16.8). A run that asked for a wheel and got none exits 1. |
| `--filter JSON` | — | Restrict the matrix, same shape as `build.py --filter`: `'{"build_type": "Release"}'`. Nested keys are spelled out — `'{"options": {"pybind": true}}'` selects the pybind configurations. |
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
| `1` | A library failed, **or** `--wheel-dir` was given and no complete set of wheels came out (§16.8). The second case reuses code 1 rather than adding a fourth: the run was asked for an artifact, it ran, and the artifact isn't there — and the `xmsconan_wheel_repair` you'd run next would be handed an empty directory. |
| `2` | The request or the machine was wrong: an unknown `--only`/`--from` name, a `--filter` that isn't a JSON object, a `--python-versions` entry the packager rejects, a `--root` that doesn't exist, a selection that matches no library at all (`--only xmscore --from xmsgrid`, or a `--from` past the last enabled library), a failed preflight — including the interpreter check in §16.8 — `conan` or `xmsconan_gen` not on `PATH`, or a file the run needed that could not be read or written. The last two are told apart in the message: only an executable that would not start is reported as a `PATH` problem, and a `xmsconan_gen` that is missing stops the run here rather than being counted as one failed library (exit 1) — it would fail identically for every library after it. |
| `3` | The run completed but **nothing was built** — every selected library was skipped (no checkout, no `build.toml`, or `--filter` matched nothing). Not success: a typo in `--root` used to print a table of skips and exit 0, which any `&&` chain or wrapper script read as "the stack is built". |

### 16.5 The library list

The driver holds the XMS stack in dependency order — each library builds against the packages produced by the ones before it — with an `enabled` flag per entry:

```
xmscore → xmsgrid → xmsinterp → xmsmesher → xmsextractor
→ xmsstamper → xmsconstraint → xmsgridtrace → xmssnap
```

**Every entry now has a Conan 2 recipe, so all of them are enabled.** The flag remains so a library can be dropped from a plain `build` run without deleting its row; `--only <lib>` ignores the flag entirely, so a library can still be built while it is off.

`xmssnap` sits last because nothing builds against it: Python is its only consumer, and the pybind wheel is the only consumable artifact. `xmsgridtrace` has a Conan 2 recipe but no msvc 192 packages published yet.

A name that is not in this tuple cannot be built by the driver at all — `--only`/`--from` validate against it and exit 2 with `unknown library`. Adding a newly migrated library here is a one-line change in `xmsconan/build_tools/vs2019_build.py`.

### 16.6 The matrix — 14 configurations per library by default

With the default two Python versions, `windows_vs2019` (msvc 192, x86_64, cppstd 17) produces:

| Group | Count | Shape |
|---|---|---|
| base | 4 | `Release`/`Debug` × `static`/`dynamic` runtime |
| `wchar_t=typedef` | 4 | the same four, with the MSVC `/Zc:wchar_t-` toggle |
| testing | 4 | the same four, with `testing=True` |
| pybind | 2 | `Release` + `dynamic` runtime only, one per `--python-versions` entry |

A library's `[matrix]` table (§5.4.1) narrows this the same way it narrows the CI matrix — the driver reads the `build.toml` in each checkout, so `--preview` and the build agree. `pybind_build_types = ["Release", "Debug"]` is how this track produces the Debug module the desktop products link.

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

### 16.8 Python wheels — one run per Python version

The msvc 192 wheels published to Aquaveo's devpi index are built the same way as the Conan packages: by hand, on the VS2019 box. The whole sequence, for Python 3.10:

```bash
# from a Python 3.10 virtual environment with conan + xmsconan installed
xmsconan vs2019 build --root <root> --version 7.0.0 --python-versions 3.10 \
    --filter '{"options": {"pybind": true}}' --wheel-dir wheelhouse
xmsconan_wheel_repair --wheel-dir wheelhouse --platform windows
xmsconan_wheel_deploy --wheel-dir wheelhouse
```

Then repeat the whole thing from a 3.13 virtual environment, into a *different* `--wheel-dir`, if you need the 3.13 wheel too.

> **PowerShell mangles `--filter`.** It strips the inner double quotes, so conan sees `{options: {pybind: true}}` and the driver rejects it as invalid JSON (exit 2). Run these commands from **Git Bash**, where they work exactly as written. In PowerShell you'd have to write `--filter '{\"options\": {\"pybind\": true}}'`; using Git Bash is the supported path.

**Why one run per version.** `XmsConan2File` hands CMake `Python3_EXECUTABLE = sys.executable` — the interpreter running conan — while the generated `CMakeLists.txt` requires `find_package(Python3 ${PYTHON_TARGET_VERSION} EXACT REQUIRED)`. **The recipe therefore assumes the interpreter running conan is the target Python.** CI satisfies that implicitly: `actions/setup-python` installs the matrix version and conan runs under it. A workstation doesn't — nothing installs a matching interpreter for you — so from a 3.12 venv a `--python-versions 3.10` run dies at configure with

```
Could NOT find Python3: Found unsuitable version "3.12.0",
but required is exact version "3.10" (found .../python.exe)
```

on *every* pybind configuration, after the non-pybind ones that don't care have already built. `build` catches this before compiling anything (§16.3) and exits 2, naming the version you're running, the version(s) the matrix wants, and both ways out. From a 3.10 venv everything lines up with no recipe change: the `EXACT` check passes, the wheel comes out tagged `cp310`, and the recipe's Python test venv is 3.10 as well.

**What `--wheel-dir` does.** After a clean build, per library:

1. `XmsConanPackager.extract_wheel(DIR, version=<--version or '*'>)` copies each pybind package's `.whl` out of the Conan cache.
2. `XmsConanPackager.collect_dependency_libs(DIR/libs)` gathers the shared libraries next to it — the same call, in the same place, that the generated `build.py` makes in CI. It is **not** optional on Windows: `xmsconan_wheel_repair --platform windows` passes `delvewheel repair --add-path <DIR>/libs` unconditionally, and a repair that finds nothing there yields a wheel that imports on the build box and fails everywhere else.

Then it prints what is staged and what comes next:

```
==> Wheels: 1 in E:\code\xms\migration\wheelhouse: xmscore-7.0.0-cp310-cp310-win_amd64.whl
    Next: xmsconan_wheel_repair --wheel-dir wheelhouse --platform windows, then xmsconan_wheel_deploy --wheel-dir wheelhouse
```

Points worth knowing:

- **Extraction is skipped when a configuration failed.** The cache may still hold a wheel from an earlier run, and staging *that* for `xmsconan_wheel_deploy` is worse than staging none. The run already exits 1 on the failure itself.
- **A matrix with no pybind configuration is a failure, not a no-op.** `extract_wheel` searches the whole local cache, so without that guard it would find last week's wheel and report success. If you pass `--wheel-dir`, select the pybind configurations (`--filter '{"options": {"pybind": true}}'`) or build the full matrix.
- **A partial fan-out is a failure too.** `extract_wheel` returns False when it finds wheels for only some of the `--python-versions` entries, which is why `--python-versions 3.10` (exactly the version you're running) belongs in the command above: leaving the default `3.10 3.13` there would fail the interpreter check first, and a `--filter` narrowed to one `python_version` while `--python-versions` still lists two reports a missing wheel at the end.
- **The summary lists the whole directory**, not just this run's copies — that directory is what repair and deploy act on next, so a wheel left over from an earlier run or a different version is about to be published too. Use a fresh `--wheel-dir` per version.
- **`collect_dependency_libs` walks the entire Conan cache**, so on a box that is also a normal msvc 194 dev machine it stages DLLs from those packages as well. `delvewheel` only vendors libraries the wheel actually imports, but if you have same-named DLLs from both toolchains, repair from a clean cache or check the `delvewheel` output.
- **No devpi upload path lives in this driver.** `xmsconan_wheel_deploy` is a separate command for the same reason `upload` is separate from `build` — see §13 for its credential resolution (`AQUAPI_URL` / `AQUAPI_USERNAME` / `AQUAPI_PASSWORD`, or `~/.xmsconan.toml`).

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

Always overridden by CLI flags / env vars when present. The `[conan]` section is the last fallback for `xmsconan conan-setup --login` and for `xmsconan vs2019 setup` (§16.2) — both resolve `--password-file`, then `$CONAN_PASSWORD`, then this file, and neither has a `--password` flag to put the secret on a command line. **Don't commit this file.** It's read-only as far as xmsconan is concerned.

A missing file is fine — credentials can come from flags or the environment instead. A file that exists but is **not valid TOML** raises `Could not parse <path>` rather than being treated as absent, so a mistyped config reports itself instead of surfacing later as "No devpi URL provided (--url, `$AQUAPI_URL`, or `~/.xmsconan.toml`)" — advice to configure the very file that failed to parse.

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

- **`ImportError: libFoo.so: cannot open shared object file`, or `DLL load failed while importing _<name>`, during the Python tests.** The module links a dependency built as a shared library and cannot find it. The recipe handles this (§7.6) through `conanrun` off Windows and a generated `sitecustomize.py` on it; if it still happens, check that the dependency declares the directory holding its shared library in `bindirs` or `libdirs`.
- **`auditwheel`/`delocate`/`delvewheel` missing libraries.** Run `build.py --wheel-dir wheelhouse` before repair — that step populates `wheelhouse/libs/`. Repairing without it produces a wheel that loads fine on the build host and crashes everywhere else.
- **`PYTHON_TARGET_VERSION` mismatch in CMake.** The recipe sets it from the `python_version` Conan option. If you're poking CMake directly, pass `-DPYTHON_TARGET_VERSION=3.13`.
- **`No pybind package found to extract`.** Means `build.py` ran but no pybind config was built. Check `build.py --preview` to see the matrix; common causes are `--filter` or the `[filter]` table in `build.toml` (§5.8) excluding the pybind variant, or every pybind variant having failed.
- **`No configurations match the requested filters`.** The `[filter]` table and the `--filter` on the command line narrow the matrix together, and nothing survived both. The message lists what was applied; drop the conflicting `--filter`, loosen `[filter]` in `build.toml`, or pass `--ignore-build-filter` for a one-off build.
- **Dual wheel uploads colliding on devpi.** With `python_versions=["3.10","3.13"]`, wheels carry distinct `cp3XY` tags, so devpi treats them as separate uploads of the same release. No special config required.
- **Generated CI references a runner or image that doesn't exist.** A Windows opt-in needs the matching `GLR-pyXYZ` GitLab runner tag; a Linux opt-in needs the matching `conan-gcc13-py<version>` container. If one isn't available, drop that version from the relevant list until it is — the platforms are independent, so Windows can carry a version Linux cannot.
- **Linux job fails pulling its container.** The generated `container:` block pulls without credentials, which only works for a public image. On GHCR `conan-gcc13-py3.13` is public but `conan-gcc13-py3.14` is not, so a 3.14 Linux leg fails at pull until the package visibility is changed or a `credentials:` stanza is added.
- **`xmsconan vs2019 build` stops at preflight with "outside the pinned `~=2.31.0` series".** Intentional (§16.3). Install the pinned client — `pip install "conan~=2.31.0"` — rather than working around the check; a different minor can change package_ids and detach your build from what's already published.
- **VS2019 build fails on a boost option Conan says no recipe defines.** The legacy `boost/1.74.0.3` doesn't declare the conan-center 1.86 options. The driver already passes `apply_boost_defaults=False`; if you're constructing `XmsConanPackager` yourself for msvc 192, pass it too (§16.6).
- **VS2019 packages don't show up for consumers.** They go to `aquaveo-vs2019`, not `aquaveo` — the consuming machine needs that remote configured too. Note that `xmsconan_vs2019 setup` *appends* the remote rather than putting it first (§16.2), so it does not shadow `aquaveo` for your other work.
- **`xmsconan vs2019 build` exits 3 with a table of `skipped` rows.** Nothing was built. Almost always a typo in `--root` (each library is looked for at `<root>/<library>`), a `--filter` that matches no configuration, or a library that has no `build.toml` yet. See the exit-code table in §16.4.
- **`Could NOT find Python3: Found unsuitable version "3.12.0", but required is exact version "3.10"`.** The interpreter running conan *is* the target Python for a pybind build (§16.8). Re-run from a 3.10 virtual environment, or build the version you're running with `--python-versions 3.12`. `xmsconan vs2019 build` now catches this at preflight and exits 2 before compiling anything — but only when the filtered matrix actually contains a pybind configuration.
- **`--filter is not valid JSON` in PowerShell.** PowerShell strips the inner double quotes from `'{"options": {"pybind": true}}'`. Run it from Git Bash, or escape them: `'{\"options\": {\"pybind\": true}}'`.
- **`xmsconan vs2019 build` exits 1 with "no complete set of wheels".** `--wheel-dir` was given but the run produced no wheel, or only part of the `--python-versions` fan-out. Usual causes: the matrix had no pybind configuration (add `--filter '{"options": {"pybind": true}}'`), or `--python-versions` listed a version the run never built. See §16.8.
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
