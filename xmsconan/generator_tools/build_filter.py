"""Shared handling for the ``[filter]`` table in ``build.toml``.

The table declares a baseline restriction on the build matrix, using the same
shape as ``build.py --filter``::

    [filter]
    build_type = "Release"

    [filter.options]
    testing = true

``xmsconan gen`` bakes it into the generated ``build.py`` as ``BUILD_FILTER``,
where it is applied before any ``--filter`` given on the command line (the two
AND together). ``xmsconan ci`` reads the same table so the generated pipeline
does not emit steps the filter has made unbuildable.
"""
# 3. Aquaveo modules
from xmsconan.constants import version_sort_key
from xmsconan.package_tools.packager import summarize_filter_matches, validate_filter_dict

#: Build types the generated CI matrix covers when ``[filter]`` doesn't pin one.
DEFAULT_CI_BUILD_TYPES = ("Release", "Debug")

#: The fixed settings each generated CI job block builds under. Only the keys a
#: filter can pin are listed; ``build_type`` is a matrix axis and is handled by
#: ``ci_build_types`` instead.
CI_JOB_SETTINGS = {
    "github": {
        "mac": {"os": "Macos", "arch": "armv8", "compiler": "apple-clang", "compiler.version": "17"},
        "linux": {"os": "Linux", "arch": "x86_64", "compiler": "gcc", "compiler.version": "13"},
        "linux-arm": {"os": "Linux", "arch": "armv8", "compiler": "gcc", "compiler.version": "13"},
        "windows": {"os": "Windows", "arch": "x86_64", "compiler": "msvc", "compiler.version": "194"},
    },
    "gitlab": {
        "Conan Build": {"os": "Linux", "arch": "x86_64", "compiler": "gcc", "compiler.version": "13"},
        "Conan Build - Windows": {
            "os": "Windows", "arch": "x86_64", "compiler": "msvc", "compiler.version": "194",
        },
    },
}

#: What the two ``xmsconan coverage`` builds pin, keyed by the report each
#: produces. Mirrors ``coverage_generator.run_coverage``; ``python_version`` is
#: filled in from ``[coverage].python_version`` at check time.
COVERAGE_BUILD_FILTERS = {
    "C++": {"build_type": "Debug", "options": {"testing": True, "pybind": False}},
    "Python": {"build_type": "Debug", "options": {"testing": False, "pybind": True}},
}


def load_build_filter(toml_data: dict) -> dict:
    """Read, validate, and sanity-check the ``[filter]`` table from ``build.toml``.

    Args:
        toml_data: The parsed ``build.toml`` contents.

    Returns:
        The filter dict, empty when the table is absent.

    Raises:
        ValueError: When the table has a key or value that could never match a
            generated configuration, or when the filter as a whole selects
            nothing to build.
    """
    build_filter = toml_data.get("filter", {})
    try:
        validate_filter_dict(build_filter)
    except ValueError as e:
        raise ValueError(f"Invalid [filter] table in build.toml: {e}") from e
    if build_filter:
        _reject_unbuildable_filter(build_filter, ci_python_versions(toml_data))
    return build_filter


#: The ``[ci]`` keys that can name a Python version some pybind leg builds.
#: ``python_versions`` is the base list (Windows); the per-platform keys override
#: it for mac and Linux and each default to the highest entry of the base, so the
#: union of whatever is present covers every ABI the matrix can produce. All
#: three are checked because a version named only in ``linux_python_versions``
#: still gets built -- validating against ``python_versions`` alone would reject
#: a `[filter]` pin that a real pipeline honors.
CI_PYTHON_VERSION_KEYS = ("python_versions", "linux_python_versions", "mac_python_versions")


def ci_python_versions(toml_data: dict) -> list:
    """The Python versions the pybind fan-out covers for this ``build.toml``."""
    ci_config = toml_data.get("ci", {}) or {}
    versions = set()
    for key in CI_PYTHON_VERSION_KEYS:
        versions.update(ci_config.get(key) or [])
    return sorted(versions, key=version_sort_key) or None


def _reject_unbuildable_filter(build_filter: dict, python_versions) -> None:
    """Raise when a filter selects no configurations at all.

    Individually valid keys and values still compose into filters nothing can
    satisfy — ``testing`` and ``pybind`` are never both true in one generated
    configuration, and a ``python_version`` outside ``[ci].python_versions``
    matches no pybind build. Catching that here means `xmsconan gen` fails
    rather than every later build on every platform.
    """
    summary = summarize_filter_matches(build_filter, python_versions)
    # The python_version check comes first because it is the more actionable
    # message and it subsumes the general one for this cause: a pinned
    # python_version that no pybind configuration carries excludes every
    # configuration, since a filtered options key a configuration lacks does
    # not match it. The general message would name the symptom and leave the
    # reader to find the pin.
    if "python_version" in build_filter.get("options", {}) and \
            all(counts["pybind"] == 0 for counts in summary.values()):
        pinned = build_filter["options"]["python_version"]
        raise ValueError(
            f"The [filter] table pins options.python_version = \"{pinned}\", which "
            "matches no pybind configuration on any platform. Add it to "
            "[ci].python_versions (or linux_python_versions / "
            "mac_python_versions), or drop the pin."
        )
    if all(counts["total"] == 0 for counts in summary.values()):
        raise ValueError(
            "The [filter] table in build.toml matches no configuration on any "
            f"platform ({', '.join(sorted(summary))}), so there would be nothing "
            "to build. Check for options that cannot hold at once — no generated "
            "configuration sets both testing and pybind, for instance."
        )


def ci_build_types(build_filter: dict) -> list[str]:
    """Return the build types a generated CI matrix should fan out over.

    A ``build.toml`` filter that pins ``build_type`` would otherwise leave the
    non-matching CI legs with an empty matrix, so the generated pipeline drops
    them instead of running jobs that can only build nothing.

    Args:
        build_filter: The validated ``[filter]`` table.

    Returns:
        A list of Conan build types for the CI matrix.
    """
    pinned = build_filter.get("build_type")
    if pinned is None:
        return list(DEFAULT_CI_BUILD_TYPES)
    return [pinned]


def ci_wheel_enabled(build_filter: dict) -> bool:
    """Whether the generated CI should keep its wheel repair / upload steps.

    ``xmsconan_wheel_repair`` raises when ``wheelhouse/`` is empty, so a filter
    that builds no pybind configuration would redden every pipeline unless the
    wheel steps come out with it.

    Args:
        build_filter: The validated ``[filter]`` table.

    Returns:
        False only when the filter explicitly excludes the pybind builds.
    """
    return build_filter.get("options", {}).get("pybind") is not False


def empty_ci_jobs(build_filter: dict, ci_type: str, emitted_jobs) -> list[str]:
    """Name the generated CI jobs a filter leaves with nothing to build.

    ``ci_build_types`` handles ``build_type``, but ``os``, ``arch``, and the
    ``compiler`` settings are fixed per job block rather than being matrix axes,
    so a filter pinning one of those empties whole jobs. Those jobs cannot be
    dropped (the platform fan-out is structural), so the caller warns instead.

    Args:
        build_filter: The validated ``[filter]`` table.
        ci_type: ``"github"`` or ``"gitlab"``.
        emitted_jobs: Names of the jobs this generation actually writes.

    Returns:
        The subset of ``emitted_jobs`` whose fixed settings the filter excludes.
    """
    empty = []
    for job_name in emitted_jobs:
        settings = CI_JOB_SETTINGS[ci_type][job_name]
        if any(key in settings and settings[key] != value for key, value in build_filter.items()):
            empty.append(job_name)
    return empty


def coverage_conflicts(build_filter: dict, coverage_python_version: str = None) -> list:
    """Describe filter entries that make ``xmsconan coverage`` unable to build.

    ``xmsconan coverage`` runs two Debug builds whose options are the inverse of
    each other — ``testing=True, pybind=False`` for the C++ report and
    ``testing=False, pybind=True`` for the Python one — and ``BUILD_FILTER`` ANDs
    with each. So a filter conflicts by *requiring* an option as much as by
    excluding it: ``pybind = true`` cancels the C++ build just as
    ``pybind = false`` cancels the Python one.

    Args:
        build_filter: The validated ``[filter]`` table.
        coverage_python_version: The ABI the Python coverage build pins, from
            ``coverage_generator``. When None the ``python_version`` pin is not
            checked.

    Returns:
        A list of human-readable conflict descriptions; empty when the filter and
        the coverage job can coexist.
    """
    conflicts = []
    filter_options = build_filter.get("options", {})
    pinned_build_type = build_filter.get("build_type")
    # Both coverage builds are Debug, so a pinned build_type conflicts once.
    if pinned_build_type is not None and pinned_build_type != "Debug":
        conflicts.append(
            f"build_type = \"{pinned_build_type}\" excludes the Debug builds "
            "both coverage reports come from"
        )
    for report, coverage_filter in COVERAGE_BUILD_FILTERS.items():
        for option, required in coverage_filter["options"].items():
            if option in filter_options and filter_options[option] != required:
                conflicts.append(
                    f"options.{option} = {str(filter_options[option]).lower()} excludes "
                    f"the {report} coverage build, which pins it to {str(required).lower()}"
                )
    pinned_version = filter_options.get("python_version")
    if coverage_python_version and pinned_version and pinned_version != coverage_python_version:
        conflicts.append(
            f"options.python_version = \"{pinned_version}\" excludes the Python "
            f"coverage build, which pins \"{coverage_python_version}\""
        )
    return conflicts
