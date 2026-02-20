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
