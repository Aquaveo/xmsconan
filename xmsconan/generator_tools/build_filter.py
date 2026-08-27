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
from xmsconan.build_toml import BuildToml
from xmsconan.constants import version_sort_key
from xmsconan.package_tools.packager import (
    summarize_filter_matches,
    validate_filter_dict,
    XmsConanPackager,
)

#: Build types the generated CI matrix covers when ``[filter]`` doesn't pin one.
DEFAULT_CI_BUILD_TYPES = ("Release", "Debug")

#: The fixed settings each generated CI job block builds under. Only the keys a
#: filter can pin are listed; ``build_type`` is a matrix axis and is handled by
#: ``ci_build_types`` instead.
_GCC_JOB = {
    "os": "Linux", "compiler": "gcc", "compiler.version": "13",
    "compiler.cppstd": "gnu17", "compiler.libcxx": "libstdc++11",
}
_MSVC_JOB = {
    "os": "Windows", "arch": "x86_64", "compiler": "msvc", "compiler.version": "194",
    "compiler.cppstd": "17",
}

CI_JOB_SETTINGS = {
    "github": {
        "mac": {
            "os": "Macos", "arch": "armv8", "compiler": "apple-clang",
            "compiler.version": "17", "compiler.cppstd": "gnu17",
            "compiler.libcxx": "libc++",
        },
        "linux": {**_GCC_JOB, "arch": "x86_64"},
        "linux-arm": {**_GCC_JOB, "arch": "armv8"},
        "windows": _MSVC_JOB,
    },
    "gitlab": {
        "Conan Build": {**_GCC_JOB, "arch": "x86_64"},
        "Conan Build - Windows": _MSVC_JOB,
    },
}

#: CI platform name -> the key it has in the packager's platform matrix. The
#: GitHub ``linux-arm`` job block shares Linux's entry: it differs by arch, and
#: pybind variants are produced for every arch, so wheel survival is the same
#: answer. ``windows_vs2019`` is absent because no generated pipeline builds it.
CI_WHEEL_PLATFORMS = {"mac": "darwin", "linux": "linux", "windows": "windows"}

#: What the two ``xmsconan coverage`` builds pin, keyed by the report each
#: produces. Mirrors ``coverage_generator.run_coverage``; ``python_version`` is
#: filled in from ``[coverage].python_version`` at check time.
COVERAGE_BUILD_FILTERS = {
    "C++": {"build_type": "Debug", "options": {"testing": True, "pybind": False}},
    "Python": {"build_type": "Release", "options": {"testing": False, "pybind": True}},
}


def load_build_filter(config: BuildToml) -> dict:
    """Read, validate, and sanity-check the ``[filter]`` table from ``build.toml``.

    Args:
        config: The parsed ``build.toml``.

    Returns:
        The filter dict, empty when the table is absent.

    Raises:
        ValueError: When the table has a key or value that could never match a
            generated configuration, or when the filter as a whole selects
            nothing to build.
    """
    build_filter = config.filter
    try:
        validate_filter_dict(build_filter)
    except ValueError as e:
        raise ValueError(f"Invalid [filter] table in build.toml: {e}") from e
    if build_filter:
        _reject_matrix_conflict(build_filter, config.matrix)
        _reject_unbuildable_filter(build_filter, ci_python_versions(config), config.matrix)
    return build_filter


#: The ``[ci]`` keys that can name a Python version some pybind leg builds.
#: ``python_versions`` is the base list (Windows); the per-platform keys override
#: it for mac and Linux and each default to the highest entry of the base, so the
#: union of whatever is present covers every ABI the matrix can produce. All
#: three are checked because a version named only in ``linux_python_versions``
#: still gets built -- validating against ``python_versions`` alone would reject
#: a `[filter]` pin that a real pipeline honors.
CI_PYTHON_VERSION_KEYS = ("python_versions", "linux_python_versions", "mac_python_versions")


def ci_python_versions(config: BuildToml) -> list:
    """The Python versions the pybind fan-out covers for this ``build.toml``."""
    versions = set()
    for key in CI_PYTHON_VERSION_KEYS:
        versions.update(getattr(config.ci, key) or [])
    return sorted(versions, key=version_sort_key) or None


def _reject_matrix_conflict(build_filter: dict, matrix) -> None:
    """Raise when ``[filter]`` pins a runtime ``[matrix]`` has already excluded.

    The emptiness check below cannot see this one. ``compiler.runtime`` is
    Windows-only, and a filter naming a setting a platform does not emit is a
    no-op there, so Linux and macOS stay non-empty however wrong the Windows
    pin is -- and the check only fires when *every* platform is empty (an
    ``os``/``arch`` pin has to stay legal, since those are warned about per job
    rather than rejected). ``[matrix] compiler_runtime = ["dynamic"]`` with
    ``[filter] "compiler.runtime" = "static"`` is individually valid on both
    sides and builds nothing on the one platform it applies to.

    Args:
        build_filter: The validated ``[filter]`` table.
        matrix: The ``[matrix]`` table, or None.

    Raises:
        ValueError: When the two tables cannot both hold.
    """
    resolved = XmsConanPackager.resolve_matrix(matrix)
    runtimes = resolved.get("compiler_runtime")
    pinned = build_filter.get("compiler.runtime")
    if runtimes and pinned is not None and pinned not in runtimes:
        raise ValueError(
            f'The [filter] table pins compiler.runtime = "{pinned}", but [matrix] '
            f'compiler_runtime builds only {list(runtimes)}, so no Windows '
            "configuration can match. Add it to [matrix].compiler_runtime, or drop "
            "the [filter] pin."
        )


def _reject_unbuildable_filter(build_filter: dict, python_versions, matrix=None) -> None:
    """Raise when a filter selects no configurations at all.

    Individually valid keys and values still compose into filters nothing can
    satisfy — ``testing`` and ``pybind`` are never both true in one generated
    configuration, and a ``python_version`` outside ``[ci].python_versions``
    matches no pybind build. Catching that here means `xmsconan gen` fails
    rather than every later build on every platform.

    ``[matrix]`` is part of that composition: it decides which configurations
    exist, so a filter is only unbuildable relative to the narrowed fan-out.
    ``[matrix] compiler_runtime = ["dynamic"]`` with ``[filter]
    "compiler.runtime" = "static"`` is the case -- every key and value is
    individually legal, and together they build nothing.
    """
    # [buildenv] is left out of this check: those values come from the
    # environment doing the generating (XMS_VERSION, CI_COMMIT_TAG, AQUAPI_* are
    # all os.getenv in generate_configurations, and XMS_TEST_ARTIFACTS_LABEL is
    # added per configuration by run()), so at generation time they are None or
    # absent and *any* pin would look unbuildable. Names and value shapes are
    # still validated by validate_filter_dict; whether a pin matches is only
    # knowable in the build environment.
    settings_only = {key: value for key, value in build_filter.items() if key != "buildenv"}
    if not settings_only:
        return
    summary = summarize_filter_matches(settings_only, python_versions, matrix)
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


def ci_filter_effects(build_filter: dict, config: BuildToml) -> dict:
    """What a ``[filter]`` table does to the generated CI.

    Both answers have to come from the configurations that actually survive, not
    from which keys the filter happens to pin -- the two are different, in both
    directions, and reading the pins got both wrong:

    * A filter empties the pybind subset without ever naming ``pybind``:
      ``build_type = "Debug"`` against the default ``[matrix].pybind_build_types``
      of ``("Release",)``, ``options.testing = true`` (the testing and pybind
      variants are disjoint copies of the base combinations), and
      ``"compiler.runtime" = "static"`` (msvc pybind variants require dynamic).
      Keeping the wheel steps for those reddens GitLab on every branch, because
      ``build.py`` exits 1 when ``--wheel-dir`` extracts no wheel -- and on
      GitHub, where every wheel step is Release-gated, it instead publishes a
      release with no wheel in it and stays green.
    * Symmetrically, ``options.pybind = true`` or an ``options.python_version``
      pin selects pybind-only configurations and so leaves the Debug leg with
      nothing to build, though it pins no ``build_type`` at all. That leg exits
      1 -- a red required check on a filter ``load_build_filter`` accepts.

    Args:
        build_filter: The validated ``[filter]`` table.
        config: The parsed build.toml, for [ci] python versions and the
            [matrix] table -- the filter has to be measured against the
            configurations this library really produces.

    Returns:
        ``{'build_types': [...], 'wheel_enabled': {ci_platform: bool}}``, keyed
        by the CI platform names in :data:`CI_WHEEL_PLATFORMS`. ``build_types``
        is never empty for a filter that reached here: ``load_build_filter`` has
        already rejected one that matches nothing on every platform, so at least
        one candidate build type keeps a configuration.

        The wheel answer is per platform because a filter can leave one
        platform with wheels and another without. ``"compiler.runtime" =
        "static"`` is the case: msvc pybind variants are only produced for the
        dynamic runtime, so Windows keeps no wheel, while Linux and macOS
        declare no ``compiler.runtime`` at all and are untouched. One global
        flag has to be wrong for one of them -- dropping the mac and Linux
        wheels that do build, or keeping Windows steps that exit 1.
    """
    pinned = build_filter.get("build_type")
    candidates = [pinned] if pinned is not None else list(DEFAULT_CI_BUILD_TYPES)
    python_versions = ci_python_versions(config)
    matrix = config.matrix

    build_types = []
    pybind = {name: 0 for name in CI_WHEEL_PLATFORMS}
    for build_type in candidates:
        # Probing one build type at a time is what makes these answers per-leg
        # rather than global: the GitHub matrix drops a leg that keeps nothing,
        # and each platform's wheel steps come out when that platform builds no
        # wheel on any leg.
        probe = dict(build_filter, build_type=build_type)
        summary = summarize_filter_matches(probe, python_versions, matrix)
        if any(counts["total"] for counts in summary.values()):
            build_types.append(build_type)
        for name, matrix_platform in CI_WHEEL_PLATFORMS.items():
            pybind[name] += summary[matrix_platform]["pybind"]

    return {
        "build_types": build_types,
        "wheel_enabled": {name: count > 0 for name, count in pybind.items()},
    }


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

    ``xmsconan coverage`` runs two builds whose options are the inverse of each
    other — ``testing=True, pybind=False`` at Debug for the C++ report and
    ``testing=False, pybind=True`` at Release for the Python one — and
    ``BUILD_FILTER`` ANDs with each. So a filter conflicts by *requiring* an
    option as much as by excluding it: ``pybind = true`` cancels the C++ build
    just as ``pybind = false`` cancels the Python one. The two pin *different*
    build types, so any ``build_type`` pin cancels exactly one of them.

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
    for report, coverage_filter in COVERAGE_BUILD_FILTERS.items():
        # The two builds pin different build types, so a pin cancels one of them.
        required_build_type = coverage_filter["build_type"]
        if pinned_build_type is not None and pinned_build_type != required_build_type:
            conflicts.append(
                f"build_type = \"{pinned_build_type}\" excludes the {report} coverage "
                f"build, which pins {required_build_type}"
            )
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
