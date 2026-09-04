# XMSConan

Methods and Modules used to aid in xmsconan projects.

> **Setting up a new library or trying to remember how a flag works?** See [docs/USAGE.md](docs/USAGE.md) — the consumer-facing guide. The rest of this README is a quick orientation.

## Installation

```bash
pip install xmsconan
```

## Usage

This package provides tools for building and generating files for XMS projects using Conan.
It was originally used with Conan 1, but support for that has been largely dropped in favor
of Conan 2 instead.

### Unified CLI

All tools are available under the `xmsconan` command:

```bash
xmsconan <command> [args...]
xmsconan --help              # list all commands
xmsconan gen --help          # help for a specific command
```

| Command | Description |
|---------|-------------|
| `xmsconan gen` | Generate build files from templates |
| `xmsconan ci` | Generate CI pipeline files (GitLab/GitHub) from templates |
| `xmsconan profiles` | Generate Conan profiles and `CMakePresets.json` from `build.toml` (run automatically by `xmsconan gen`) |
| `xmsconan coverage` | Run unified C++/Python coverage (see `docs/USAGE.md` §11) |
| `xmsconan test-shards` | Run a staged gtest runner as N parallel shards in one container and merge their JUnit reports (see `docs/USAGE.md` §10.2) |
| `xmsconan build` | Build XMS libraries |
| `xmsconan vs2019` | Build/publish the manual VS2019 (msvc 192) matrix (see `docs/USAGE.md` §16) |
| `xmsconan conan-setup` | Set up Conan profile and remotes for CI builds |
| `xmsconan wheel-repair` | Repair Python wheels for the current platform (Linux/macOS/Windows) |
| `xmsconan wheel-deploy` | Upload repaired wheels to a devpi index with `uv publish` |
| `xmsconan conan-deploy` | Save, restore, or upload Conan packages in CI |
| `xmsconan publish` | Build, repair, and deploy a library |

Legacy entry points (`xmsconan_gen`, `xmsconan_ci`, `xmsconan_build`, `xmsconan_coverage`, `xmsconan_vs2019`, etc.) remain available for backwards compatibility.

## build.toml Schema Reference

The `build.toml` file defines the structure and dependencies of your XMS library. All fields are optional unless marked as required.

### Required Fields

| Field | Type | Description | Example |
|-------|------|-------------|----------|
| `library_name` | string | Name of the library | `"xmscore"` |
| `description` | string | Brief description | `"Support library for XMS products"` |

### Source Files

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `library_sources` | array[string] | `[]` | C++ source files for the library | 
| `library_headers` | array[string] | `[]` | Public header files |
| `testing_sources` | array[string] | `[]` | Test implementation files (`.cpp`) |
| `testing_headers` | array[string] | `[]` | Test header files (`.t.h`) |
| `python_library_sources` | array[string] | `[]` | Python-specific C++ sources |
| `python_library_headers` | array[string] | `[]` | Python-specific headers |
| `pybind_sources` | array[string] | `[]` | Pybind11 binding sources |
| `pybind_headers` | array[string] | `[]` | Pybind11 binding headers |

### Dependencies

| Field                    | Type          | Default | Description                                                                                                                                                                                                                                                                                                                                                                        |
|--------------------------|---------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `conan_profile_conf`     | object        | see below | `[conf]` entries written into every generated profile. Defaults to pinning `tools.cmake.cmaketoolchain:generator = "Ninja Multi-Config"` and disabling Conan's own `CMakeUserPresets.json`. An empty table (`{}`) omits the section, which also suppresses `CMakePresets.json`. |
| `conan_profile_variants` | array[object] | `[]`    | Extra generator renderings of the same settings, e.g. building Windows with both Ninja and Visual Studio. Object shape: `{ name = "vs", platforms = ["windows"], kinds = ["testing"], conf = { ... } }`. `name` is required; `platforms` accepts `linux` / `mac_os` / `windows` and `kinds` accepts `library` / `python` / `testing`, and omitting either filter means "applies everywhere". Each match is written a second time as `<stem>_<name>.txt`. |
| `conan_profile_options`  | object        | `{}`    | Dependent package options (format: `{"dep_name": {"option": "value"}}`). Emits values to the `[options]` section of profiles for all configurations being built. `*` can be used as a package name to apply options to everything, e.g. `{'*': {'shared': True}}` to make all dependencies shared. If a wildcard conflicts with a non-wildcard, the non-wildcard takes precedence. |
| `xms_dependencies`       | array[object] | `[]`    | XMS library dependencies. Each object: `{name="xmscore", version="7.0.0", no_python=false}`. Set `no_python=true` to exclude from Python package dependencies.                                                                                                                                                                                                                     |
| `xms_python_dependencies` | array[string] | `[]`   | Extra **Python** requirements for `_package/pyproject.toml`, in pip requirement form (`["geopandas", "data_objects>=4.0.0"]`). For runtime imports that are not XMS sister libraries. The Conan *dependency graph* is unaffected, but these are real wheel requirements that pip resolves from an index during the Conan build when `pybind` is on. See `docs/USAGE.md` §5.3. |
| `extra_dependency_cmake_names` | object  | `{}`    | CMake package-name overrides for `extra_dependencies` entries, keyed on the Conan package name (format: `{"xmdf": "Xmdf"}`). Only needed when a package's CMake config does not use its Conan reference name; an empty value keeps the dep in the Conan graph but out of the generated `CMakeLists.txt`. See `docs/USAGE.md` §5.3.                                              |
| `extra_dependencies`     | array[string] | `[]`    | Additional Conan dependencies (format: `["package/version"]`). Each entry also gets a `find_package` and the `EXT_*` plumbing in the generated `CMakeLists.txt`, the same treatment `xms_dependencies` entries get. Deduplicated by package name against the deps the recipe adds itself and against the rest of the list — an entry the recipe already requires is skipped and logged, so listing e.g. `pybind11` for a static half that needs it does not break the `pybind=True` builds. See `docs/USAGE.md` §5.3.                     |
| `xms_dependency_options` | object        | `{}`    | Per-dependency option overrides (format: `{"dep_name": {"pybind": false}}`). Applied in the generated conanfile's `configure()` method. Appears to be unreliable in some cases (at minimum, `profile_options` overrides it).                                                                                                                                                       |
| `vs2019_dependency_overrides` | object   | `{}`    | Per-dependency *reference* overrides applied only on a Visual Studio 2019 (msvc 192) build (format: `{"xmscore": "xmscore/[>=6.0.1 <7.0.0]"}`). Matched on the package name before the first `/`; every other toolchain ignores it. For libraries whose sister dependencies are pinned to a different line in the legacy desktop products. An entry may change the version or range only: renaming the package, or naming one that is not an `xms_dependencies` entry, fails the msvc 192 build instead of being ignored. See `docs/USAGE.md` §7.4.            |

### Build Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `testing_framework` | string | `"cxxtest"` | Testing framework (`"cxxtest"` or `"gtest"`) |
| `python_binding_type` | string | `"pybind11"` | Python binding framework (`"pybind11"` or `"vtk_wrap"`) |
| `python_namespaced_dir` | string | `""` | Python module subdirectory (e.g., `"core"` for `xms.core`) |
| `pybind_root` | boolean | `false` | Whether this is the root pybind package |
| `pybind_advertises_module` | boolean | `false` | Advertise the pybind module's import library (`_<name>`) to C++ consumers instead of the static library, and install the module to `bin/` + `lib/`. Windows-only in effect; opt-in because module consumers see only exported symbols. Also what renames the Windows Debug module to `_<name>_d`, matching what `cpp_info` advertises. See `docs/USAGE.md` §7.5. |
| `[matrix].compiler_runtime` | array[string] | `["dynamic", "static"]` | Which MSVC runtimes the fan-out builds. `["dynamic"]` drops the static-CRT configurations (and their `wchar_t` / `testing` copies) for a library nothing consumes a `/MT` build of. Inert on Linux and macOS. See `docs/USAGE.md` §5.4.1. |
| `[matrix].wheel_only` | boolean | `false` | Build only what a wheel release needs — Release with tests, Debug with tests, and the pybind build. Three configurations for a single-ABI library (down from 5 on Linux/macOS, 13 on Windows), matching in everything but `build_type` and `pybind`. The pybind leg fans out, so each extra `python_versions` or `pybind_build_types` entry adds one configuration on top of the three. Drops the library-only and `wchar_t=typedef` configurations and narrows `compiler_runtime` to `["dynamic"]` unless set explicitly. See `docs/USAGE.md` §5.4.1. |
| `[ci].windows_vs2019` | boolean | `false` | Also build the `windows_vs2019` (msvc 192) matrix in CI and publish it to the `aquaveo-vs2019` remote. **GitLab only** — a GitHub project setting it is warned and gets no jobs. Emitted beside the msvc 194 Windows jobs, so it requires `[ci].windows` and is rejected with it off. No wheels: an msvc 192 wheel and an msvc 194 wheel are the same devpi filename. See `docs/USAGE.md` §10.2. |
| `[matrix].pybind_build_types` | array[string] | `["Release"]` | Which build types get a pybind configuration. Add `"Debug"` when consumers link a Debug module; `XMS_COVERAGE=1` no longer adds `Debug` on top, because coverage takes its Python half from the Release pybind build. On Windows the Debug leg publishes no wheel and runs no Python tests. See `docs/USAGE.md` §5.4.1 and §7.5. |

### Build Matrix Filter (`[filter]`)

Optional baseline restriction on the configuration matrix, in the same shape as
`build.py --filter`. `xmsconan gen` bakes it into the generated `build.py` (which
applies it before any `--filter` given on the command line), and `xmsconan ci`
narrows the generated `build_type` matrix to match.

```toml
[filter]
build_type = "Release"   # never build Debug packages

[filter.options]
pybind = false           # this library ships no wheel
```

Top-level keys are Conan settings (`build_type`, `arch`, `compiler`,
`compiler.runtime`, …); `[filter.options]` accepts `wchar_t`, `pybind`, `testing`,
and `python_version`; `[filter.buildenv]` accepts the env var names the profiles
set. Keys and values are matched for equality, one value per key, and both are
validated when the files are generated — a value nothing builds (`"release"`, an
unquoted `3.13`, `testing` and `pybind` both true) fails `xmsconan gen` rather
than every later build.

The generated CI follows what the filter leaves buildable, not which keys it
pins: the wheel repair / upload steps come out per platform when that platform
builds no wheel, and a build type with no configurations left is dropped from
the matrix. So `options.pybind = false` gets a pipeline that can pass, and so
does `"compiler.runtime" = "static"` — which keeps the macOS and Linux wheels
and drops only the Windows ones. An `os` / `arch` / `compiler` pin is only
warned about — those are separate CI job blocks, not a matrix axis. Pass `--ignore-build-filter` to `build.py` for a
one-off build of an excluded configuration. See
[docs/USAGE.md §5.8](docs/USAGE.md) for the full behavior.

### Advanced

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `extra_cmake_text` | string | `""` | Additional CMake code injected into CMakeLists.txt |
| `post_library_cmake_text` | string | `""` | CMake code added after library target definition |
| `extra_export_sources` | array[string] | `[]` | Additional directories/files to export (e.g., `["test_files"]`) |

### Example build.toml

```toml
library_name = "xmscore"
description = "Support library for XMS products"

xms_dependencies = []

python_namespaced_dir = "core"
pybind_root = true

library_sources = [
    "xmscore/math/math.cpp",
    "xmscore/misc/StringUtil.cpp",
]

library_headers = [
    "xmscore/math/math.h",
    "xmscore/misc/StringUtil.h",
]

testing_sources = [
    "xmscore/testing/TestTools.cpp"
]

testing_headers = [
    "xmscore/math/math.t.h",
    "xmscore/testing/TestTools.h",
]

pybind_sources = [
    "xmscore/python/xmscore_py.cpp",
]
```

#### Example generation

```bash
xmsconan gen --version 9.0.0 build.toml
```

#### Example generation dry-run

```bash
xmsconan gen --dry-run -v --version 9.0.0 build.toml
```

#### Example build into a shared builds folder

```bash
xmsconan build --cmake_dir . --build_dir ../builds/xmscore --profile VS2022_TESTING --generator vs2022
```

#### Example build dry-run

```bash
xmsconan build --cmake_dir . --build_dir ../builds/xmscore --profile VS2022_TESTING --generator vs2022 --dry-run -v
```

#### Useful build flags

- `--allow-missing-test-files`: Continue when test data path is missing
- `--dry-run`: Print Conan/CMake commands and options without executing
- `-v` / `-q`: Increase debug output or suppress informational logs

### CI Tools

These commands replace inline shell scripts in CI templates, reducing duplication and making pipelines easier to maintain.

#### Conan Setup

```bash
# Default: detect profile, add Aquaveo remote
xmsconan conan-setup

# GitHub Actions: also login and remove conancenter
xmsconan conan-setup --remote-url https://conan2.aquaveo.com/... --login --remove-conancenter

# Log in from a workstation with the password read from a file
xmsconan conan-setup --login --username myuser --password-file ~/.conan-password
```

The password reaches Conan through the child process's environment
(`CONAN_LOGIN_USERNAME_<REMOTE>` / `CONAN_PASSWORD_<REMOTE>`), never on a
command line. `--password-file` falls back to `$CONAN_PASSWORD` and then the
`[conan]` section of `~/.xmsconan.toml`; there is deliberately no `--password`
flag. In CI, nothing is passed at all — `CONAN_LOGIN_USERNAME` /
`CONAN_PASSWORD` are read by Conan itself, from the `Setup Conan` step's own
`env:` on GitHub (the job holds no secrets; see `docs/USAGE.md` §10.1) and
from the project's CI/CD variables on GitLab.

#### Wheel Repair

```bash
# Auto-detect platform and repair wheels in wheelhouse/
xmsconan wheel-repair --wheel-dir wheelhouse

# Explicit platform
xmsconan wheel-repair --wheel-dir wheelhouse --platform macos
```

Windows repair can be switched off per library with
`[ci].windows_wheel_repair`, which drops the step from the **Windows job** of the
generated CI, from `xmsconan publish`, and from the `xmsconan vs2019` track. The
default follows `ci_type` — on for `github` (public wheels need their DLLs
bundled), off for `gitlab` (internal wheels are loaded by a host that supplies
the C++ runtime itself). See `docs/USAGE.md` §12.1.

#### Wheel Deploy

```bash
# Reads $AQUAPI_URL / $AQUAPI_USERNAME / $AQUAPI_PASSWORD, then ~/.xmsconan.toml
xmsconan wheel-deploy --wheel-dir wheelhouse

# Point at a different index without moving the credentials
xmsconan wheel-deploy --wheel-dir wheelhouse --url https://... --username user

# Password from a file rather than the environment
xmsconan wheel-deploy --wheel-dir wheelhouse --password-file ~/.aquapi-password
```

The upload is `uv publish`, with the username and password handed to it in its
environment (`UV_PUBLISH_USERNAME` / `UV_PUBLISH_PASSWORD`), so the password is
never on a command line — not this process's (there is deliberately no
`--password` flag, only `--password-file`, as with `conan-setup`) and not the
child's. `--client devpi` keeps the old devpi-client path for one release; it is
the last place a password went on a subprocess's argv. See `docs/USAGE.md` §13
and §17.

#### Conan Deploy

```bash
# Save a package to a tarball
xmsconan conan-deploy xmscore 7.0.0 --save xmscore-7.0.0.tar.gz

# Restore and upload
xmsconan conan-deploy xmscore 7.0.0 --restore xmscore-7.0.0.tar.gz --upload
```

## Building with Docker

XMS C++ libraries can be built inside Docker containers for Linux. This is the recommended approach for producing Linux wheels and Conan packages from macOS or Windows.

### Prerequisites

The workspace `docker-compose.dev.yml` provides two dev containers:

| Container | Platform | Runner | Best for |
|-----------|----------|--------|----------|
| `nextms-dev-arm` | linux/arm64 | Native on Apple Silicon | ARM Linux builds on macOS |
| `nextms-dev-x86` | linux/amd64 | QEMU on Apple Silicon, native on x86 | x86_64 Linux builds, CI parity |

Start the container you need:

```bash
# From the workspace root (aqua_dev/)
docker compose -f docker-compose.dev.yml up dev-arm -d   # ARM (fast on Apple Silicon)
docker compose -f docker-compose.dev.yml up dev-x86 -d   # x86_64 (matches CI)
```

### Credential Setup

Create `~/.xmsconan.toml` on your host machine to avoid passing credentials on every build:

```toml
[aquapi]
url = "https://public.aquapi.aquaveo.com/aquaveo/dev/"
username = "your_username"
password = "your_password"
```

Mount it into the container by adding a volume to `docker-compose.dev.yml`:

```yaml
volumes:
  - ~/.xmsconan.toml:/root/.xmsconan.toml:ro
```

Alternatively, keep the same three variables in an env file and let Docker read
them out of it — `-e VAR=value` would copy the password into the docker
client's own command line, which `ps` and process-creation auditing read:

```bash
# ~/.aquapi.env, chmod 600 — VAR=value lines, no quotes, no export
#   AQUAPI_URL=https://public.aquapi.aquaveo.com/aquaveo/dev/
#   AQUAPI_USERNAME=user
#   AQUAPI_PASSWORD=...
docker exec --env-file ~/.aquapi.env \
            nextms-dev-arm bash -c "cd /workspace/xmscore && xmsconan publish --version 7.0.0"
```

`xmsconan publish --docker` does the same thing for you: it forwards
`AQUAPI_URL`, `AQUAPI_USERNAME`, and `AQUAPI_PASSWORD` as bare `-e NAME` flags,
so the values travel through the docker client's environment rather than its
argv, and it mounts `~/.xmsconan.toml` read-only when the file exists.

### Building a Single Library

```bash
# Full build + upload (reads credentials from ~/.xmsconan.toml or env vars)
docker exec nextms-dev-arm bash -c "cd /workspace/xmscore && xmsconan publish --version 7.0.0"

# Build and repair wheel only, skip uploads
docker exec nextms-dev-arm bash -c "cd /workspace/xmscore && xmsconan publish --version 7.0.0 --no-deploy"

# Upload wheel only, skip Conan package
docker exec nextms-dev-arm bash -c "cd /workspace/xmscore && xmsconan publish --version 7.0.0 --no-conan"

# Filter to Release builds only
docker exec nextms-dev-arm bash -c "cd /workspace/xmscore && xmsconan publish --version 7.0.0 --filter '{\"build_type\": \"Release\"}'"
```

### Building Libraries in Dependency Order

Libraries must be built in dependency order so Conan packages are available for downstream builds:

```
xmscore → xmsgrid → xmsinterp → xmsmesher
                               → xmsextractor → xmsconstraint
xmscore → xmsvtk
```

Example for a full ARM build:

```bash
CONTAINER=nextms-dev-arm
VERSION=7.0.0

docker exec $CONTAINER bash -c "cd /workspace/xmscore && xmsconan publish --version $VERSION --no-deploy"
docker exec $CONTAINER bash -c "cd /workspace/xmsgrid && xmsconan publish --version $VERSION --no-deploy"
docker exec $CONTAINER bash -c "cd /workspace/xmsinterp && xmsconan publish --version $VERSION --no-deploy"
docker exec $CONTAINER bash -c "cd /workspace/xmsmesher && xmsconan publish --version $VERSION --no-deploy"
docker exec $CONTAINER bash -c "cd /workspace/xmsextractor && xmsconan publish --version $VERSION --no-deploy"
docker exec $CONTAINER bash -c "cd /workspace/xmsconstraint && xmsconan publish --version $VERSION --no-deploy"
```

Replace `--no-deploy` with no flag to also upload each package as it's built.

### macOS vs. Windows

| | macOS (Apple Silicon) | Windows |
|---|---|---|
| **ARM builds** | `nextms-dev-arm` — native, fast | Not available |
| **x86_64 builds** | `nextms-dev-x86` — QEMU, slower | `nextms-dev-x86` — native |
| **Docker command** | `docker exec nextms-dev-arm ...` | `docker exec nextms-dev-x86 ...` |
| **Workspace mount** | `/workspace` | `/workspace` |

On Windows, use `nextms-dev-x86` for x86_64 Linux builds. The commands are identical — just change the container name.

## VS2019 (msvc 192) Packages

The msvc 192 binaries the Aquaveo desktop products (GMS/SMS/WMS) consume are published to a separate Conan remote, `aquaveo-vs2019`, so they never mix with the CI-published ones. `xmsconan vs2019` drives that build **manually, on a developer workstation with Visual Studio 2019 installed**.

**GitLab CI can build this matrix too.** The `GLR-UV` runner carries VS2019 alongside VS2022, so a repository can set `[ci].windows_vs2019 = true` and get a `Conan Build - Windows VS2019` job on every pipeline, publishing to the same `aquaveo-vs2019` remote — see `docs/USAGE.md` §10.2. GitHub cannot: it retired the `windows-2019` image and has no replacement, so a GitHub project setting the flag is warned and gets no jobs.

The manual track below stays, and is still the only route for **wheels** (CI deliberately publishes none for msvc 192 — a wheel's tags do not record which MSVC built it, so it would collide with the msvc 194 wheel on devpi) and for **libraries whose repository has not opted in**.

```bash
# One time: add + log in to the aquaveo-vs2019 remote, then check the machine
xmsconan_vs2019 setup --password-file <path to your conan password file>

# Preview the matrix (14 configurations per library by default, fewer if
# build.toml restricts it with [matrix]) without building
xmsconan_vs2019 build --root E:\code\xms\migration --preview

# Build. Hours, not minutes — per-configuration logs go to the --log-dir
xmsconan_vs2019 build --root E:\code\xms\migration --log-dir .\vs2019-logs --version 7.0.0

# Review the summary table, then publish
xmsconan_vs2019 upload --library xmscore --version 7.0.0
```

`build` never uploads as a side effect — publishing to a shared remote after a multi-hour local run is a decision a human makes, so it is a separate verb. Every library in the stack now has a Conan 2 recipe and is built by default; the `enabled` flag remains so one can be dropped from a plain run without deleting its row. A library that is not listed in the `LIBRARIES` tuple in `xmsconan/build_tools/vs2019_build.py` cannot be built by the driver at all, so adding a newly migrated one there is a one-line change.

`setup` **appends** `aquaveo-vs2019` to your Conan remote list rather than putting it first, so it never becomes the first stop for your ordinary msvc 194 work, and it passes the password to Conan through the environment rather than on the command line (see `docs/USAGE.md` §16.2). `build` regenerates `conanfile.py` / `CMakeLists.txt` / `build.py` **in place** in each checkout, so an interrupted run leaves those files behind stamped with the VS2019 `--version` — check `git status` before committing.

The recipe forks its third-party dependency *versions* automatically when it sees msvc 192 — the same packages as the modern stack, resolved to the legacy builds the `aquaveo-vs2019` remote publishes msvc 192 binaries for (boost `1.74.0.3`, zlib `1.2.11`). A library whose *sister* dependencies are pinned to a different line on VS2019 declares that itself with the `[vs2019_dependency_overrides]` table in `build.toml` — see `docs/USAGE.md` §7.4 for the recipe attributes and §16 for the full flag reference.

`upload` publishes only `compiler.version=192` binaries and refuses any remote other than `aquaveo-vs2019` unless `--allow-other-remote` is passed, so a mixed local cache can't leak msvc 194 packages onto the legacy remote and a one-word typo can't push legacy packages into the CI remote. It exits nonzero when `conan upload` fails, so a failed publish is never reported as success. `build` exits `1` if a library failed or a requested wheel never appeared, `2` for a bad request or a machine that can't build, and `3` when nothing was built at all (`docs/USAGE.md` §16.4).

### VS2019 Python wheels — one run per Python version

`build --wheel-dir DIR` copies each pybind package's `.whl` out of the Conan cache and fills `DIR/libs` with the libraries the repair step needs. Repairing and publishing stay separate commands, the same way `build` and `upload` are:

```bash
# from a Python 3.10 virtual environment with conan + xmsconan installed
xmsconan vs2019 build --root <root> --version 7.0.0 --python-versions 3.10 \
    --filter '{"options": {"pybind": true}}' --wheel-dir wheelhouse
xmsconan_wheel_repair --wheel-dir wheelhouse --platform windows
xmsconan_wheel_deploy --wheel-dir wheelhouse
```

Run those from **Git Bash** — PowerShell strips the inner quotes out of `--filter`. Repeat from a 3.13 virtual environment, into a different `--wheel-dir`, for the 3.13 wheel.

**The Python running conan is the target Python.** The recipe hands CMake `Python3_EXECUTABLE = sys.executable` while the generated `CMakeLists.txt` requires `find_package(Python3 ${PYTHON_TARGET_VERSION} EXACT REQUIRED)`. In CI `actions/setup-python` installs the matrix version and conan runs under it; on a workstation you supply it, so a 3.12 venv building `--python-versions 3.10` fails every pybind configuration at CMake configure. `build` checks this against the *filtered* matrix before compiling anything and exits 2 — the non-pybind configurations don't care which interpreter is running, so a non-pybind build from any environment still works. Full workflow in `docs/USAGE.md` §16.8.

## Development

### Setting up

`uv` is the toolchain. One sync gives you an editable install plus the
`dev` dependency group from `pyproject.toml` — flake8 with the plugins
CI runs, pytest, pytest-cov, and pre-commit:

```bash
uv sync --group dev
uv run pre-commit install    # flake8 on the staged files at every commit
```

`uv run flake8 .` lints the whole tree; `.flake8` holds the configuration.
flake8 is the authoritative linter — when a formatter disagrees with it,
flake8 wins.

### Running tests

The default suite runs fast and mocks every external tool:

```bash
uv run pytest tests/ -v
```

CI runs the same suite on Python 3.10, 3.13, and 3.14, on Linux and
Windows, behind the coverage floor declared as `fail_under` in
`pyproject.toml`. The same gate locally:

```bash
uv run pytest tests/ --cov=xmsconan
```

The repository also ships a no-mock integration test that drives
`xmsconan coverage` end-to-end against a stub recipe (boost, zlib,
pybind11, a real `conan create`, real `gcovr`, real `pytest-cov`). It
is registered under the `integration` pytest marker and gated behind
the `XMS_INTEGRATION_TESTS` environment variable so it does not run by
default — wall time is in the multi-minute range:

```bash
XMS_INTEGRATION_TESTS=1 uv run pytest -m integration -v
```

Prerequisites: `conan` and `gcovr` on `PATH`, plus a working C++
toolchain (`g++` on Linux). The test is automatically skipped on
Windows because coverage instrumentation requires gcc/clang.

It is the wire-format canary for the `xmsconan coverage` pipeline —
failure here means xmsconan and one of the CLIs it shells out to
(conan, cmake, gcovr, pytest-cov) no longer agree on a shape, before
any downstream library's CI catches it. Run it locally before merging
any change that touches the coverage tooling.

## License

BSD 2-Clause License
