# XMSConan

Methods and Modules used to aid in xmsconan projects.

## Installation

```bash
pip install xmsconan
```

## Usage

This package provides tools for building and generating files for XMS projects using Conan.
The generation system only targets Conan 2. Conan 1 projects typically had handwritten
conanfiles that would inherit `xmsconan.xms_conan_file.XmsConanFile` and use the builder
returned by `xmsconan.build_helpers.get_builder()`.

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
| `xmsconan build` | Build XMS libraries |
| `xmsconan conan-setup` | Set up Conan profile and remotes for CI builds |
| `xmsconan wheel-repair` | Repair Python wheels for the current platform (Linux/macOS/Windows) |
| `xmsconan wheel-deploy` | Upload repaired wheels to a devpi index |
| `xmsconan conan-deploy` | Save, restore, or upload Conan packages in CI |
| `xmsconan publish` | Build, repair, and deploy a library |

Legacy entry points (`xmsconan_gen`, `xmsconan_ci`, `xmsconan_build`, etc.) remain available for backwards compatibility.

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

| Field                    | Type          | Default | Description                                                                                                                                                                                                                  |
|--------------------------|---------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `profile_options`        | object        | `{}`    | Dependent package options (format: `{"dep_name": {"option": "value"}}`). Emits values to the `[options]` section of profiles for all configurations being built.                                                             |
| `xms_dependencies`       | array[object] | `[]`    | XMS library dependencies. Each object: `{name="xmscore", version="7.0.0", no_python=false}`. Set `no_python=true` to exclude from Python package dependencies.                                                               |
| `extra_dependencies`     | array[string] | `[]`    | Additional Conan dependencies (format: `["package/version"]`)                                                                                                                                                                |
| `xms_dependency_options` | object        | `{}`    | Per-dependency option overrides (format: `{"dep_name": {"pybind": false}}`). Applied in the generated conanfile's `configure()` method. Appears to be unreliable in some cases (at minimum, `profile_options` overrides it). |

### Build Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `testing_framework` | string | `"cxxtest"` | Testing framework (`"cxxtest"` or `"gtest"`) |
| `python_binding_type` | string | `"pybind11"` | Python binding framework (`"pybind11"` or `"vtk_wrap"`) |
| `python_namespaced_dir` | string | `""` | Python module subdirectory (e.g., `"core"` for `xms.core`) |
| `pybind_root` | boolean | `false` | Whether this is the root pybind package |

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
```

#### Wheel Repair

```bash
# Auto-detect platform and repair wheels in wheelhouse/
xmsconan wheel-repair --wheel-dir wheelhouse

# Explicit platform
xmsconan wheel-repair --wheel-dir wheelhouse --platform macos
```

#### Wheel Deploy

```bash
# Uses $AQUAPI_URL, $AQUAPI_USERNAME, $AQUAPI_PASSWORD env vars
xmsconan wheel-deploy --wheel-dir wheelhouse

# Or pass credentials explicitly
xmsconan wheel-deploy --wheel-dir wheelhouse --url https://... --username user --password pass
```

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

Alternatively, pass credentials as environment variables:

```bash
docker exec -e AQUAPI_URL=https://public.aquapi.aquaveo.com/aquaveo/dev/ \
            -e AQUAPI_USERNAME=user \
            -e AQUAPI_PASSWORD=pass \
            nextms-dev-arm bash -c "cd /workspace/xmscore && xmsconan publish --version 7.0.0"
```

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

## License

BSD 2-Clause License
