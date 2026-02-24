# XMSConan

Methods and Modules used to aid in xmsconan projects.

## Installation

```bash
pip install xmsconan
```

## Usage

This package provides tools for building and generating files for XMS projects using Conan.

### Command Line Tools

- `xmsconan_gen`: Generate build files from templates
- `xmsconan_build`: Build XMS libraries

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

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `xms_dependencies` | array[object] | `[]` | XMS library dependencies. Each object: `{name="xmscore", version="7.0.0", no_python=false}`. Set `no_python=true` to exclude from Python package dependencies. |
| `extra_dependencies` | array[string] | `[]` | Additional Conan dependencies (format: `["package/version"]`) |
| `xms_dependency_options` | object | `{}` | Per-dependency option overrides (format: `{"dep_name": {"pybind": false}}`) |

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
xmsconan_gen --version 9.0.0 build.toml
```

#### Example build into a shared builds folder

```bash
xmsconan_build --cmake_dir . --build_dir ../builds/xmscore --profile VS2022_TESTING --generator vs2022
```

## License

BSD 2-Clause License
