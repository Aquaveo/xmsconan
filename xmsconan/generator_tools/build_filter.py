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
# 1. Standard python modules
import collections
import json
from typing import Optional

# 3. Aquaveo modules
from xmsconan.build_toml import BuildToml
from xmsconan.constants import version_sort_key
from xmsconan.package_tools.packager import (
    config_label,
    COVERAGE_PYBIND_BUILD_TYPE,
    filter_matches,
    is_instrumented_configuration,
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
    if "coverage" in build_filter.get("options", {}):
        # Normal builds omit the coverage option entirely (it is only set on
        # `xmsconan coverage` runs), so a permanent [filter] pin would
        # exclude every normal configuration — and the generic emptiness
        # check would blame "options that cannot hold at once".
        raise ValueError(
            "The [filter] table in build.toml pins options.coverage, but the "
            "coverage option is only emitted during `xmsconan coverage` runs, "
            "so the pin would exclude every normal build. Pass it as a "
            "--filter to build.py during a coverage run instead."
        )
    if build_filter:
        _reject_matrix_conflict(build_filter, config.matrix)
        python_versions = ci_python_versions(config)
        _reject_unbuildable_filter(build_filter, python_versions, config.matrix)
    return build_filter


#: The ``[ci]`` keys that can name a Python version some pybind leg builds.
#: ``python_versions`` is the base list (Windows); the per-platform keys override
#: it for mac and Linux and each default to the highest entry of the base, so the
#: union of whatever is present covers every ABI the matrix can produce. All
#: three are checked because a version named only in ``linux_python_versions``
#: still gets built -- validating against ``python_versions`` alone would reject
#: a `[filter]` pin that a real pipeline honors.
CI_PYTHON_VERSION_KEYS = ("python_versions", "linux_python_versions", "mac_python_versions")


def ci_python_versions(config: BuildToml) -> Optional[list]:
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
        ``{'build_types': [...], 'wheel_enabled': {ci_platform: bool},
        'test_labels': [...]}``, with ``wheel_enabled`` keyed by the CI platform
        names in :data:`CI_WHEEL_PLATFORMS`. ``build_types``
        is never empty for a filter that reached here: ``load_build_filter`` has
        already rejected one that matches nothing on every platform, so at least
        one candidate build type keeps a configuration.

        ``test_labels`` names the Linux ``test_artifacts/<label>/`` directories
        the build stages, one per surviving testing configuration, and is what
        the split-out GitLab test jobs pass to ``xmsconan_test_shards --label``.
        It is deliberately not derived from ``build_types``: that axis counts
        every surviving configuration, so a filter selecting only pybind or only
        plain-library builds leaves a build type listed there with no test
        runner staged. Unlike ``build_types`` it *can* be empty — a filter with
        ``options.testing = false`` builds no runner at all — and the caller
        emits no test jobs rather than one that would fail looking for them.

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
    # Linux only: [ci].split_tests splits the Linux build alone. The Windows job
    # compiles and runs its tests in one place and so never needs to name an
    # artifact directory.
    test_labels = []
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
        for label in summary[CI_WHEEL_PLATFORMS["linux"]]["testing_labels"]:
            if label not in test_labels:
                test_labels.append(label)

    return {
        "build_types": build_types,
        "wheel_enabled": {name: count > 0 for name, count in pybind.items()},
        "test_labels": test_labels,
    }


def ci_build_jobs(build_filter: dict, config: BuildToml,
                  python_versions: list, coverage: bool = False,
                  coverage_python_version: Optional[str] = None) -> list[dict]:
    """One GitLab build job per surviving Linux configuration.

    The generated pipeline used to run a single ``Conan Build`` that looped
    every configuration in sequence, and a ``Coverage Build`` that recompiled
    two of them instrumented -- five compiles, three of them of source the
    pipeline had already compiled. Emitting a job per configuration instead
    makes the legs concurrent (they declare ``needs: []``, so they start
    together rather than at their stage's turn) and lets each one be requested
    in exactly the shape its consumer needs, so nothing is built twice.

    Instrumentation is not decided here. It comes from
    :func:`is_instrumented_configuration`, the same predicate the packager
    applies when it sets the ``coverage`` option, because the two answers have
    to agree: a job that ran an uninstrumented build and then looked for
    ``.gcda`` would report the layer it was meant to measure as 0% covered,
    and a job instrumented without a consumer would pay the compile for data
    nobody reads.

    Args:
        build_filter: The validated ``[filter]`` table.
        config: The parsed build.toml, for its [matrix] table.
        python_versions: The Linux Python versions this pipeline builds --
            ``[ci].linux_python_versions``, already resolved by the caller.
            Deliberately not ``ci_python_versions``, which unions every
            platform's list: that is the right input for a yes/no "does any
            platform build a wheel" question, but here it would emit a build
            job per ABI that only macOS builds, each one compiling a Python
            version the Linux image does not have.
        coverage: Whether this repository generates a coverage pipeline. False
            leaves every job uninstrumented, which is also the shape a tag
            pipeline wants.
        coverage_python_version: The single ABI the coverage run pins to. Only
            that pybind job is instrumented; a repository building three
            wheels would otherwise emit three instrumented pybind jobs to feed
            a reader that commits to one ABI up front, paying two extra
            instrumented compiles for data nothing opens. None instruments
            whichever pybind jobs the predicate accepts.

    Returns:
        A list of job dicts, in matrix order, each with:

        ``name``
            The GitLab job name, e.g. ``"Debug Instrumented Build"``.
        ``label``
            :func:`config_label` for this configuration -- the
            ``test_artifacts/<label>/`` directory the build writes.
        ``filter_json``
            The ``build.py --filter`` argument selecting this one
            configuration out of the generated matrix.
        ``instrumented``
            Whether this job compiles with coverage instrumentation, and so
            whether it measures and publishes a coverage tracefile.
        ``coverage_leg``
            The ``xmsconan_coverage --leg`` value this job measures, or None
            when it is not instrumented.
        ``kind``
            ``"testing"``, ``"pybind"`` or ``"library"``.
        ``build_type``, ``python_version``
            The configuration's own settings; ``python_version`` is None
            except on pybind jobs.
        ``tag_policy``
            ``"always"``, ``"except"`` (branch pipelines only) or ``"only"``
            (tag pipelines only) -- rendered as the job's ``only:``/``except:``
            gate.
        ``exports``
            Whether this job saves a Conan cache tarball for the deploy to
            restore. Assigned by the caller, not here, because it depends on
            the whole set of jobs.

        Empty when the filter leaves Linux with nothing to build, which the
        caller reads as "emit no Linux build jobs" rather than emitting one
        that would exit 1 looking for a configuration that was filtered away.
    """
    pinned = build_filter.get("build_type")
    candidates = [pinned] if pinned is not None else list(DEFAULT_CI_BUILD_TYPES)
    linux = CI_WHEEL_PLATFORMS["linux"]

    jobs = []
    for build_type in candidates:
        probe = dict(build_filter, build_type=build_type)
        for combination in filter_matches(linux, probe, python_versions,
                                          config.matrix):
            options = combination["options"]
            instrumented = coverage and is_instrumented_configuration(combination)
            # A wheel ABI the coverage run does not read. `not in` rather
            # than two comparisons because it fits on one line: with W503 and
            # W504 both enabled, a break at a binary operator is a lint error
            # from whichever side it falls on.
            abi = options.get("python_version")
            wrong_abi = coverage_python_version not in (None, abi)
            if instrumented and options.get("pybind") and wrong_abi:
                instrumented = False
            # And a wheel *build type* the coverage run does not read. The
            # Python leg's filter pins one build type, so a library naming two
            # in [matrix].pybind_build_types gets a second pybind
            # configuration that `--leg python` never builds -- but
            # is_instrumented_configuration says True for any pybind, so
            # without this it would be planned as an instrumented job. It
            # would then compile an instrumented module nothing reads and
            # write coverage-status-python.json and
            # cov-cpp-tracefile-python.json over the leg that is read, because
            # both files are named for the leg and neither is named for the
            # build type. Same shape as wrong_abi above, same reason for the
            # temporary: W503 and W504 are both on, so the condition cannot
            # break at its operators.
            wrong_build_type = build_type != COVERAGE_PYBIND_BUILD_TYPE
            if instrumented and options.get("pybind") and wrong_build_type:
                instrumented = False
            if options.get("pybind"):
                kind = "pybind"
                python_version = options.get("python_version")
                selector = {"pybind": True, "testing": False,
                            "python_version": python_version}
            elif options.get("testing"):
                kind = "testing"
                python_version = None
                selector = {"testing": True, "pybind": False}
            else:
                kind = "library"
                python_version = None
                selector = {"testing": False, "pybind": False}
            spec = {
                "name": None,  # assigned below, once the whole set is known
                "label": config_label(combination),
                # Compact separators, so the rendered YAML stays parseable.
                # json.dumps' default ", " / ": " puts a colon-space inside
                # the script line, and a colon-space is what ends a YAML plain
                # scalar -- the whole build command then parses as a mapping
                # and the pipeline is rejected before it starts.
                "filter_json": json.dumps({
                    "build_type": combination["build_type"],
                    "options": selector,
                }, separators=(",", ":")),
                "instrumented": instrumented,
                # The `xmsconan_coverage --leg` selector for this job, or None
                # when it builds nothing coverage reads. Carried on the job
                # rather than derived in the template, so the one place that
                # decides a job is instrumented is also the place that says
                # which leg it is.
                "coverage_leg": (
                    ("python" if kind == "pybind" else "cpp")
                    if instrumented else None
                ),
                "kind": kind,
                "build_type": combination["build_type"],
                "python_version": python_version,
                # A tag pipeline exists to publish, so it builds exactly what
                # gets published: the library binaries downstream repositories
                # link against, and the pybind binary the wheel wraps. A
                # testing configuration builds a test runner -- nothing
                # installs it and no tag pipeline runs it -- so it is
                # branch-only. The old single build job compiled the test
                # configurations on tags too, and for a wheel_only repository
                # like xmsvtk, whose matrix is two testing configurations and a
                # pybind one, that was two thirds of what a release spent its
                # time on.
                "tag_policy": "except" if kind == "testing" else "always",
                # Set by _mark_export_jobs once the whole set is known: which
                # jobs save a Conan cache tarball for the deploy to restore.
                "exports": False,
            }
            if instrumented and kind == "pybind":
                # Instrumentation is part of the package_id, so this job's
                # binary is not the one a release publishes. It stays on
                # branches and the clean twin below takes the tag.
                spec["tag_policy"] = "except"
            jobs.append(spec)
            if instrumented and kind == "pybind":
                # The wheel still has to be built on a tag, and it must not be
                # the instrumented one -- `--coverage` changes the binary that
                # gets published, and the Coverage stage that would consume the
                # instrumentation does not run on a tag anyway. So the pybind
                # configuration is emitted twice, under two names and two
                # mutually exclusive tag policies: exactly one of them exists
                # in any given pipeline.
                jobs.append(dict(
                    spec, instrumented=False, tag_policy="only", name=None,
                    coverage_leg=None,
                ))

    _name_build_jobs(jobs)
    return jobs


def _name_build_jobs(jobs: list[dict]) -> None:
    """Give each build job a name, in place.

    Names are read by humans scanning a pipeline and by ``needs:`` in the
    template, so they say what the job builds rather than repeating its filter:
    ``"Release Build"``, ``"Debug Instrumented Build"``, ``"Python
    Instrumented Build"``.

    A pybind job's name disambiguates on two axes, each added only when the
    fan-out actually spans it. GitLab job names must be unique, but naming the
    single-ABI case ``"Python Instrumented Build - py3.14"`` would put the
    interpreter version into a name that has no second version to be
    distinguished from, and into ``needs:`` lines that then churn on every
    Python bump; the same argument applies to a build type when only one
    pybind build type exists.

    * The **build type** is prefixed when ``[matrix].pybind_build_types`` names
      more than one -- ``"Debug Python Build"`` -- matching how the library
      jobs are named.
    * The **Python ABI** is suffixed when more than one pybind job exists.

    Whether the two axes are enough is not left to inspection: the caller
    checks the finished names for duplicates and raises, because a duplicate
    GitLab job name is silently last-wins rather than an error.
    """
    pybind_jobs = [job for job in jobs
                   if job["kind"] == "pybind" and job["tag_policy"] != "only"]
    # The build type is a second axis the pybind fan-out can span, and unlike
    # the ABI it is measured over *every* pybind job including the `only: tags`
    # twins. The ABI suffix can skip them because a twin is always the clean
    # half of a pair and the "Instrumented" word already separates it from its
    # own sibling -- but it does not separate it from a different build type's
    # uninstrumented job, which carries no such word either. With
    # pybind_build_types = ["Release", "Debug"] that is exactly the collision:
    # the Release twin and the Debug job both want "Python Build".
    pybind_build_types = {job["build_type"] for job in jobs
                          if job["kind"] == "pybind"}
    for job in jobs:
        if job["kind"] == "pybind":
            base = ("Python" if len(pybind_build_types) < 2
                    else f"{job['build_type']} Python")
        elif job["kind"] == "library":
            base = f"{job['build_type']} Library"
        else:
            base = job["build_type"]
        middle = " Instrumented" if job["instrumented"] else ""
        name = f"{base}{middle} Build"
        if job["kind"] == "pybind" and len(pybind_jobs) > 1:
            name = f"{name} - py{job['python_version']}"
        job["name"] = name

    # A GitLab job name is a YAML mapping key, so two jobs sharing one is not
    # an error the pipeline reports -- the later block silently replaces the
    # earlier, one configuration is never built, and the deploy then restores
    # an export tarball no job wrote (the tarball is named for the *label*,
    # which does carry the build type). Fail here instead: a naming axis this
    # function does not yet know about should stop generation, not ship a
    # pipeline that quietly builds less than the matrix asked for.
    counts = collections.Counter(job["name"] for job in jobs)
    duplicates = sorted(name for name, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(
            "Two or more CI build jobs would be generated with the same name "
            f"({', '.join(repr(name) for name in duplicates)}), and a GitLab "
            "job name has to be unique -- the duplicate would overwrite the "
            "first and that configuration would never be built. This is a "
            "generator bug: some axis of the matrix is not part of the job "
            "name."
        )


def empty_ci_jobs(build_filter: dict, ci_type: str, emitted_jobs) -> list[str]:
    """Name the generated CI jobs a filter leaves with nothing to build.

    ``ci_build_types`` handles ``build_type``, but ``os``, ``arch``, and the
    ``compiler`` settings are fixed per job block rather than being matrix axes,
    so a filter pinning one of those empties whole jobs. Those jobs cannot be
    dropped (the platform fan-out is structural), so the caller warns instead.

    Args:
        build_filter: The validated ``[filter]`` table.
        ci_type: ``"github"`` or ``"gitlab"``.
        emitted_jobs: Names of the jobs this generation actually writes. An
            entry may instead be a ``(name, settings_name)`` pair, for a job
            whose platform settings are registered under a different name --
            the ``[matrix].wheel_only`` Linux fan-out emits one job per
            configuration, and every one of them runs on the same runner
            ``CI_JOB_SETTINGS`` knows as ``"Conan Build"``. Without the pair
            form each generated name would have to appear in that table, and a
            table keyed on names the generator invents is a table that goes
            stale the next time a job is renamed.

    Returns:
        The subset of ``emitted_jobs`` whose fixed settings the filter excludes,
        as names.
    """
    empty = []
    for entry in emitted_jobs:
        job_name, settings_name = entry if isinstance(entry, tuple) else (entry, entry)
        settings = CI_JOB_SETTINGS[ci_type][settings_name]
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
