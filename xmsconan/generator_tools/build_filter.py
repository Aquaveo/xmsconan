"""Shared handling for the ``[filter]`` table in ``build.toml``.

The table declares a baseline restriction on the build matrix, using the same
shape as ``build.py --filter``::

    [filter]
    build_type = "Release"

    [filter.options]
    pybind = false

``xmsconan gen`` bakes it into the generated ``build.py`` as ``BUILD_FILTER``,
where it is applied before any ``--filter`` given on the command line (the two
AND together). ``xmsconan ci`` reads the same table so the generated CI matrix
does not fan out over build types the library has filtered away.
"""
# 3. Aquaveo modules
from xmsconan.package_tools.packager import validate_filter_dict

#: Build types the generated CI matrix covers when ``[filter]`` doesn't pin one.
DEFAULT_CI_BUILD_TYPES = ["Release", "Debug"]


def load_build_filter(toml_data: dict) -> dict:
    """Read and validate the ``[filter]`` table from parsed ``build.toml`` data.

    Args:
        toml_data: The parsed ``build.toml`` contents.

    Returns:
        The filter dict, empty when the table is absent.

    Raises:
        ValueError: When the table has a key or value shape that could never
            match a generated configuration.
    """
    build_filter = toml_data.get("filter", {})
    try:
        validate_filter_dict(build_filter)
    except ValueError as e:
        raise ValueError(f"Invalid [filter] table in build.toml: {e}") from e
    return build_filter


def ci_build_types(build_filter: dict) -> list:
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


def coverage_conflicts(build_filter: dict) -> list:
    """Describe filter entries that make ``xmsconan coverage`` unable to build.

    ``xmsconan coverage`` runs two Debug builds — ``testing=True`` for the C++
    report and ``pybind=True`` for the Python report. A filter that excludes any
    of those leaves the corresponding build with no configurations at all.

    Args:
        build_filter: The validated ``[filter]`` table.

    Returns:
        A list of human-readable conflict descriptions; empty when the filter and
        the coverage job can coexist.
    """
    conflicts = []
    build_type = build_filter.get("build_type")
    if build_type is not None and build_type != "Debug":
        conflicts.append(
            f"build_type = \"{build_type}\" excludes the Debug builds coverage instruments"
        )
    options = build_filter.get("options", {})
    for option in ("testing", "pybind"):
        if options.get(option) is False:
            conflicts.append(
                f"options.{option} = false excludes the "
                f"{'C++' if option == 'testing' else 'Python'} coverage build"
            )
    return conflicts
