"""One TOML reader for every tool in the package.

Each entry point used to carry its own ``tomllib``/``toml`` fallback, and the
copies had already drifted -- some called ``load`` on a binary handle, others
``loads`` on decoded text, which read the same file with two different encoding
rules. There is one shim here instead, so adding an entry point does not mean
adding a fifth.
"""
# 1. Standard python modules
from pathlib import Path

# 2. Third party modules
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

import toml


def load_toml(toml_path):
    """Parse a TOML file, preferring the stdlib parser when it is available.

    Args:
        toml_path: Path to the TOML file, as ``str`` or ``Path``.

    Returns:
        The parsed document as a dict.
    """
    text = Path(toml_path).read_text(encoding="utf-8")
    if tomllib:
        return tomllib.loads(text)
    return toml.loads(text)


# Every top-level key a build.toml may carry. A key absent from here is a typo:
# the generators read this file with .setdefault()/.get(), so a misspelling has
# no symptom at all -- the default is kept, the generated artifact is silently
# not what was asked for, and the mistake surfaces (if ever) as a missing source
# file or an option that "didn't take". This mirrors what [ci]
# (ci_options.validate_ci_table), [matrix] (XmsConanPackager.resolve_matrix),
# [filter] (build_filter.load_build_filter), conan_profile_variants and
# vs2019_dependency_overrides already enforce for their own sub-tables.
KNOWN_KEYS = frozenset({
    # identity
    "library_name",
    "description",
    "version",
    "ci_type",
    # source lists
    "library_sources",
    "library_headers",
    "testing_sources",
    "testing_headers",
    "python_library_sources",
    "python_library_headers",
    "pybind_sources",
    "pybind_headers",
    "extra_export_sources",
    # dependencies
    "xms_dependencies",
    "xms_python_dependencies",
    "xms_dependency_options",
    "extra_dependencies",
    "extra_dependency_cmake_names",
    "vs2019_dependency_overrides",
    # build shape
    "testing_framework",
    "python_binding_type",
    "python_namespaced_dir",
    "pybind_root",
    "pybind_advertises_module",
    "extra_cmake_text",
    "post_library_cmake_text",
    # conan profiles
    "conan_profile_conf",
    "conan_profile_options",
    "conan_profile_variants",
    # sub-tables, each with its own validator
    "ci",
    "coverage",
    "filter",
    "matrix",
})


def validate_top_level_keys(toml_data, toml_path):
    """Reject a build.toml key that no generator, template, or doc knows.

    Args:
        toml_data: The parsed build.toml.
        toml_path: Path the data came from, for the error message.

    Raises:
        ValueError: When *toml_data* carries a key outside :data:`KNOWN_KEYS`.
    """
    unknown = sorted(set(toml_data) - KNOWN_KEYS)
    if unknown:
        raise ValueError(
            f'{toml_path} has unknown top-level key(s) {", ".join(unknown)}. '
            f'Accepted keys: {", ".join(sorted(KNOWN_KEYS))}.'
        )
