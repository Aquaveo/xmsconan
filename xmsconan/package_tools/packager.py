"""The packager module."""
import concurrent.futures
import copy
import functools
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, NamedTuple, Optional

from xmsconan.constants import (
    build_folder_for_generator,
    DEFAULT_REMOTE_NAME,
    is_multi_config_generator,
    MSVC_VS2019_VERSION,
    version_sort_key,
)
from xmsconan.package_tools.printer import Printer


#: Suffix `_run_sharded_tests` appends to a label when the runner was never
#: built, so `run` can keep that fault out of the failed-shard count. The two
#: methods communicate through this spelling; it is not for display.
RUNNER_MISSING_SUFFIX = '-runner-missing'


def get_current_arch():
    """Get the current architecture in Conan format."""
    machine = platform.machine().lower()
    arch_map = {
        'x86_64': 'x86_64',
        'amd64': 'x86_64',
        'aarch64': 'armv8',
        'arm64': 'armv8',
    }
    return arch_map.get(machine, machine)


class ProfilePlan(NamedTuple):
    """One profile that :meth:`XmsConanPackager.write_profiles` will write.

    A named tuple rather than a dict so every consumer spells the fields the
    same way: the dry-run path once unpacked three names from a four-key dict
    and raised on the first entry, which a plain mapping cannot make obvious.
    """

    filename: str
    configuration: dict
    conf: dict
    variant: Optional[str]


# Build-environment keys a generated profile may carry.
#
# Every profile xmsconan produces is public. The ones `write_profiles` writes
# are committed to a repository, and the ephemeral one `create_build_profile`
# hands to `conan create` is printed to the job log in full -- and conan then
# echoes whatever profile it is given under "Input profiles" at the start of
# every build. A job log outlives the temporary file and is readable by anyone
# with project access. So there is no such thing as a private [buildenv]
# entry: a secret must never reach `combination['buildenv']` at all, and
# `generate_configurations` is where credentials are kept out of it.
#
# This set is therefore not a filter applied on the way to disk. It was one,
# and a filter that guards only the committed profile leaves the printed one
# unguarded -- and fails OPEN for it, since buildenv is assembled from the
# process environment and a name nobody thought to list goes straight
# through. It is the ALLOW-list that `test_every_generated_buildenv_key_is_public`
# holds every configuration to: a new [buildenv] name fails that test until it
# is consciously admitted here, which is the moment to ask whether it belongs
# in a log.
PUBLIC_BUILDENV_KEYS = frozenset({
    'XMS_VERSION',
    'PYTHON_TARGET_VERSION',
    'CI_COMMIT_TAG',
    'RELEASE_PYTHON',
    'MACOSX_DEPLOYMENT_TARGET',
    '_PYTHON_HOST_PLATFORM',
    'XMS_TEST_ARTIFACTS_DIR',
    # Added per configuration by run(), not generate_configurations, so the
    # profile conan is handed carries one name the matrix itself does not.
    'XMS_TEST_ARTIFACTS_LABEL',
})

# Default [conf] for generated profiles. Ninja Multi-Config matches what the
# hand-maintained xmsvtk profiles pin on every platform except their explicit
# Visual Studio variant. Without a generator in the profile Conan falls back to
# a platform default, which differs between machines -- the exact class of
# silent divergence these profiles exist to remove.
DEFAULT_PROFILE_CONF = {
    'tools.cmake.cmaketoolchain:generator': 'Ninja Multi-Config',
    # Disable Conan's CMakeUserPresets.json. We generate CMakePresets.json
    # ourselves with stable, readable names; Conan's file otherwise ACCUMULATES
    # an include per output folder, and because it names every preset
    # `conan-default` regardless of options, a second install makes
    # `cmake --list-presets` fail outright with "Duplicate preset".
    'tools.cmake.cmaketoolchain:user_presets': '',
}

configurations = {
    'windows': {
        'os': ['Windows'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['msvc'],
        'compiler.cppstd': ['17'],
        'compiler.version': ['194'],
        'compiler.runtime': ['dynamic', 'static'],
    },
    # Visual Studio 2019 (msvc 192). Published to the `aquaveo-vs2019` remote
    # rather than to `aquaveo` (see XmsConanPackager.upload's `remote`
    # argument), so the two toolchains never mix. Built either on a developer
    # workstation (xmsconan.build_tools.vs2019_build) or, where a repository
    # sets `[ci].windows_vs2019`, by the GitLab jobs that pass
    # `build.py --platform windows_vs2019`. GitHub cannot build it: it retired
    # the `windows-2019` runner image.
    # Identical to 'windows' apart from the compiler version.
    'windows_vs2019': {
        'os': ['Windows'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['msvc'],
        'compiler.cppstd': ['17'],
        'compiler.version': [MSVC_VS2019_VERSION],
        'compiler.runtime': ['dynamic', 'static'],
    },
    'linux': {
        'os': ['Linux'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['gcc'],
        'compiler.version': ['13'],
        'compiler.cppstd': ['gnu17'],
        'compiler.libcxx': ['libstdc++11'],
    },
    'darwin': {  # macos
        'os': ['Macos'],
        'build_type': ['Release', 'Debug'],
        'arch': ['armv8'],
        'compiler': ['apple-clang'],
        'compiler.version': ['17'],
        'compiler.cppstd': ['gnu17'],
        'compiler.libcxx': ['libc++'],
    },
}


def _collect_setting_values():
    """Map each settings key to every value any platform emits for it."""
    values = {}
    for platform_config in configurations.values():
        for key, key_values in platform_config.items():
            values.setdefault(key, set()).update(key_values)
    return {key: frozenset(key_values) for key, key_values in values.items()}


# Every settings key/value pair that appears in any platform's block above.
# Filters are validated against the union rather than the running platform's
# keys so a ``build.toml`` ``[filter]`` can pin a Windows-only setting (e.g.
# ``compiler.runtime``) without breaking the Linux and macOS builds that never
# see that key. Values are validated too: filters match by equality, so
# ``build_type = "release"`` can no more match a configuration than a list can.
FILTER_SETTING_VALUES = _collect_setting_values()
FILTER_SETTING_KEYS = frozenset(FILTER_SETTING_VALUES)

# Option keys ``generate_configurations`` emits, and how their values are
# checked. ``None`` means the value has a dedicated rule below rather than a
# fixed set. Keep in sync with ``XmsConan2File.options`` — importing the recipe
# here would drag conan into the generators, which only need this table.
FILTER_OPTION_VALUES = {
    'wchar_t': frozenset({'builtin', 'typedef'}),
    'pybind': None,          # bool
    'testing': None,         # bool
    'python_version': None,  # "X.Y" string
    'coverage': None,        # bool; emitted only on coverage runs
}
FILTER_OPTION_KEYS = frozenset(FILTER_OPTION_VALUES)

# Nested tables in a filter dict; their values are compared per-key.
FILTER_NESTED_KEYS = ('options', 'buildenv')

PYTHON_VERSION_RE = re.compile(r'^\d+\.\d+$')


def _validate_scalar(value, where):
    """Reject values the equality comparison could never match."""
    if isinstance(value, (list, tuple, dict, set)):
        raise ValueError(
            f"Filter value for {where} must be a single value, not "
            f"{type(value).__name__} — filters match by equality, so a "
            f"collection can never match a configuration."
        )
    if not isinstance(value, (str, bool, int)):
        # Anything else (a TOML float or date, say) is both unmatchable and
        # unrenderable into the generated build.py, which imports no modules
        # that could name it.
        raise ValueError(
            f"Filter value for {where} must be a string, bool, or int, not "
            f"{type(value).__name__} — quote it if it is meant to be a string "
            f"(TOML reads 3.13 as a float, \"3.13\" as a version)."
        )


def _validate_option(option_key, value):
    """Validate one ``[filter.options]`` entry."""
    if option_key not in FILTER_OPTION_KEYS:
        raise ValueError(
            f"Unknown filter option {option_key!r}: must be one of "
            f"{sorted(FILTER_OPTION_KEYS)}."
        )
    _validate_scalar(value, f"option {option_key!r}")
    allowed = FILTER_OPTION_VALUES[option_key]
    if allowed is not None:
        if value not in allowed:
            raise ValueError(
                f"Filter option {option_key!r} must be one of {sorted(allowed)}, got {value!r}."
            )
    elif option_key in ('pybind', 'testing', 'coverage'):
        if not isinstance(value, bool):
            raise ValueError(
                f"Filter option {option_key!r} must be true or false, got {value!r}."
            )
        if option_key == 'coverage' and value is False:
            # Coverage runs emit coverage=True and normal runs omit the
            # option entirely, so false can never match a configuration.
            raise ValueError(
                "Filter option 'coverage' can only be true: coverage runs "
                "emit coverage=True and normal runs omit the option, so "
                "false would match nothing. Drop the pin instead."
            )
    elif not isinstance(value, str) or not PYTHON_VERSION_RE.match(value):
        raise ValueError(
            f"Filter option {option_key!r} must be a quoted \"X.Y\" version "
            f"string, got {value!r}."
        )


def _validate_buildenv(env_key, value):
    """Validate one ``[filter.buildenv]`` entry."""
    if env_key not in emitted_buildenv_keys():
        raise ValueError(
            f"Unknown filter buildenv name {env_key!r}: the generated profiles "
            f"set {sorted(emitted_buildenv_keys())}."
        )
    _validate_scalar(value, f"buildenv {env_key!r}")


def validate_filter_dict(filter_dict):
    """
    Validate the shape and values of a configuration filter dict.

    Accepts the same shape as ``build.py --filter`` and the ``[filter]`` table
    in ``build.toml``: top-level Conan settings plus the nested ``options`` and
    ``buildenv`` tables. Keys *and* values are checked against what
    ``generate_configurations`` emits, so a filter that could never match any
    configuration is rejected here rather than quietly narrowing the matrix to
    nothing at build time.

    Args:
        filter_dict: The filter to validate.

    Raises:
        ValueError: When a key or value would never match a generated
            configuration.
    """
    if not isinstance(filter_dict, dict):
        raise ValueError(f"Filter must be a table/dict, got {type(filter_dict).__name__}.")
    for key, value in filter_dict.items():
        if key in FILTER_NESTED_KEYS:
            if not isinstance(value, dict):
                raise ValueError(
                    f"Filter key {key!r} must be a table/dict of "
                    f"{key} names to values, got {type(value).__name__}."
                )
            for nested_key, nested_value in value.items():
                if key == 'options':
                    _validate_option(nested_key, nested_value)
                else:
                    _validate_buildenv(nested_key, nested_value)
            continue
        if key not in FILTER_SETTING_KEYS:
            raise ValueError(
                f"Unknown filter key {key!r}: must be a top-level configuration "
                f"setting {sorted(FILTER_SETTING_KEYS)} or 'options'/'buildenv'. "
                f"Did you mean {{'options': {{'{key}': ...}}}}?"
            )
        _validate_scalar(value, repr(key))
        if value not in FILTER_SETTING_VALUES[key]:
            raise ValueError(
                f"Filter value for {key!r} must be one of "
                f"{sorted(FILTER_SETTING_VALUES[key])}, got {value!r}."
            )


def _configuration_matches(configuration, filter_dict):
    """Whether one generated configuration survives a filter.

    A *setting* the configuration doesn't carry is skipped rather than excluding
    it: ``compiler.runtime`` exists only on Windows, so a filter pinning it has
    to stay usable on Linux and macOS.

    A missing ``options``/``buildenv`` key is the opposite -- it does **not**
    match. ``python_version`` is set only on pybind variants, so treating an
    absent key as a match made ``{'options': {'python_version': '3.13'}}`` keep
    every non-pybind configuration as well, and the filter that was supposed to
    select one wheel selected the whole matrix.
    """
    for key, value in filter_dict.items():
        if key in FILTER_NESTED_KEYS:
            section = configuration.get(key, {})
            for nested_key, nested_value in value.items():
                if section.get(nested_key) != nested_value:
                    return False
        elif key in configuration and configuration.get(key) != value:
            return False
    return True


def _reference_matrix(platform_name, python_versions=None, coverage=False, matrix=None):
    """Generate one platform's configurations for validating a filter against.

    Coverage defaults to off so the matrix a filter is checked against does not
    depend on whether ``XMS_COVERAGE`` happens to be set in the environment
    doing the generating. (Coverage mode now contributes the ``coverage``
    *option* rather than a ``[buildenv]`` name; ``emitted_buildenv_keys``
    still passes ``coverage=True`` so the derived key set tracks whatever a
    coverage run emits.)

    ``python_versions`` gets the same env-independence, and for the same reason:
    passing None reaches ``_resolve_python_versions``, which falls back to
    ``PYTHON_TARGET_VERSION`` from the environment -- so an
    ``options.python_version`` pin would be accepted or rejected according to
    what the shell running ``xmsconan gen`` happens to export, while every
    generated CI leg exports the default. A generator must not read a build.toml
    two ways on two machines.

    ``matrix`` is the ``[matrix]`` table, and it has to be honored here: it is
    what decides which configurations exist at all, so validating a filter
    against the unnarrowed fan-out accepts filters that select nothing once
    ``[matrix]`` has been applied -- which is the "fails every later build
    instead of failing generation" case this validation exists to prevent.
    """
    packager = XmsConanPackager(
        '_filter_validation',
        python_versions=(list(python_versions) if python_versions
                         else list(XmsConanPackager.DEFAULT_PYTHON_VERSIONS)),
        coverage=coverage,
        artifacts_dir='artifacts',
        matrix=matrix,
    )
    try:
        return packager.generate_configurations(system_platform=platform_name)
    finally:
        packager._temp_dir.cleanup()


@functools.lru_cache(maxsize=None)
def emitted_buildenv_keys():
    """Every ``[buildenv]`` name the generated profiles can carry.

    Derived from the matrix itself rather than hand-listed, so a new
    ``[buildenv]`` entry in ``generate_configurations`` becomes filterable
    without a second edit here.
    """
    keys = set()
    for platform_name in configurations:
        for combination in _reference_matrix(platform_name, coverage=True):
            keys.update(combination['buildenv'])
    # run() adds this one per configuration when --artifacts-dir is in play.
    keys.add('XMS_TEST_ARTIFACTS_LABEL')
    return frozenset(keys)


def config_label(combination):
    """Return a human-readable label for a build configuration.

    The label has to be *unique across the generated matrix*: it names the
    per-configuration log file (``run(log_dir=...)``) and the test-artifact
    directory (``XMS_TEST_ARTIFACTS_LABEL``), both of which are overwritten
    when two configurations share a label. ``compiler.runtime`` is therefore
    part of the label — the msvc matrices fan out over ``dynamic``/``static``,
    so without it 14 msvc configurations collapse onto 8 labels and each static
    build destroys the dynamic build's log. Toolchains that don't set
    ``compiler.runtime`` (gcc, apple-clang) are unaffected and keep their
    shorter labels.

    Module-level rather than a method because the CI generator has to name the
    same directories the build will write: ``summarize_filter_matches`` reports
    the testing labels a platform stages, and the generated GitLab test jobs
    pass them to ``xmsconan_test_shards --label``. Any second implementation of
    this naming — a template concatenating ``<build_type>-testing``, say —
    would be a copy free to drift from the one the build actually uses.
    """
    parts = [combination.get('build_type', 'unknown')]
    runtime = combination.get('compiler.runtime')
    if runtime:
        parts.append(str(runtime))
    opts = combination.get('options', {})
    if opts.get('testing'):
        parts.append('testing')
    elif opts.get('pybind'):
        py_version = opts.get('python_version')
        parts.append(f'pybind-py{py_version}' if py_version else 'pybind')
    if opts.get('wchar_t') == 'typedef':
        parts.append('wchar_typedef')
    return '-'.join(parts)


#: Build types whose testing configuration carries C++ instrumentation on a
#: coverage run. Every other testing leg stays optimized so it exercises what
#: ships rather than an instrumented copy of it.
COVERAGE_TESTING_BUILD_TYPES = ('Debug',)

#: The build type whose pybind configuration the Python coverage leg reads.
#: ``[matrix].pybind_build_types`` may name more than one -- a library whose
#: consumers link a Debug module names both -- but the coverage run builds and
#: measures exactly one of them, so the others must not be instrumented: they
#: would compile a second instrumented module nothing reads, and write their
#: status and tracefile under the same per-leg names as the one that is read.
#: Shared with :func:`xmsconan.coverage_tools.coverage_generator._coverage_legs`,
#: which pins the leg's filter, so the CI planner and the coverage run cannot
#: disagree about which pybind build type is the measured one.
COVERAGE_PYBIND_BUILD_TYPE = 'Release'


def is_instrumented_configuration(combination) -> bool:
    """Whether a configuration is compiled with coverage instrumentation.

    Only the configurations coverage actually reads data from are instrumented,
    so a coverage run still produces one optimized testing leg. Pybind is
    always instrumented: the binding layer is C++ that only a Python test can
    reach, so without its own .gcda it is not counted at all. The Debug testing
    leg is instrumented because that is what drives C++ coverage. The Release
    testing leg is not -- it exists to exercise the configuration the shipped
    wheel is built from, and instrumenting it would leave nothing running
    optimized code.

    Module-level rather than a method for the same reason as
    :func:`config_label`: the CI generator has to decide which *build jobs* are
    instrumented, and it must reach that verdict through the rule the packager
    actually applies. A second implementation in a template -- "Debug means
    instrumented", say -- would be a copy free to drift from this one, and the
    drift would show up as a job that compiles without coverage and then
    reports 0% for the layer it was supposed to measure.

    Args:
        combination: One configuration from the fan-out, before its options are
            serialized into a profile.

    Returns:
        True when this configuration should carry ``coverage=True``.
    """
    options = combination['options']
    if options.get('pybind'):
        return True
    return bool(options.get('testing')) and (
        combination['build_type'] in COVERAGE_TESTING_BUILD_TYPES)


def filter_matches(platform_name, filter_dict, python_versions=None, matrix=None):
    """Return one platform's configurations that survive a filter.

    The configurations themselves, not a count of them.
    :func:`summarize_filter_matches` answers "how many, and of what kind",
    which is all the GitHub matrix needs; the GitLab generator emits one build
    *job* per surviving configuration and so needs each one's build type,
    options and Python version to write its ``build.py --filter`` argument.

    Args:
        platform_name: A key of ``configurations`` -- ``linux``, ``darwin`` or
            ``windows``.
        filter_dict: A filter already through ``validate_filter_dict``.
        python_versions: Python versions the pybind fan-out should assume.
            Defaults to the packager's own resolution.
        matrix: The ``[matrix]`` table, so the filter is checked against the
            configurations this library actually produces.

    Returns:
        The surviving configurations, in matrix order.
    """
    return [
        configuration
        for configuration in _reference_matrix(platform_name, python_versions, matrix=matrix)
        if _configuration_matches(configuration, filter_dict)
    ]


def summarize_filter_matches(filter_dict, python_versions=None, matrix=None):
    """Report what a filter would keep on each platform.

    Lets a generator answer "does this filter select anything at all?" without
    waiting for a build to discover it.

    Args:
        filter_dict: A filter already through ``validate_filter_dict``.
        python_versions: Python versions the pybind fan-out should assume,
            e.g. ``[ci].python_versions`` from ``build.toml``. Defaults to the
            packager's own resolution.
        matrix: The ``[matrix]`` table, so the filter is checked against the
            configurations this library actually produces rather than the
            unnarrowed fan-out.

    Returns:
        ``{platform: {'total': int, 'pybind': int, 'testing_labels': [str]}}``
        — the number of surviving configurations, how many of those build the
        Python bindings, and the :func:`config_label` of each surviving
        *testing* configuration, in matrix order.

        ``testing_labels`` is what the generated CI needs and neither count can
        supply: ``total`` includes the pybind and plain-library configurations,
        so a filter can leave a build type with configurations but no test
        runner to stage. The labels name the ``test_artifacts/<label>/``
        directories the build will actually write.
    """
    summary = {}
    for platform_name in configurations:
        kept = filter_matches(platform_name, filter_dict, python_versions, matrix)
        summary[platform_name] = {
            'total': len(kept),
            'pybind': sum(1 for c in kept if c['options'].get('pybind')),
            'testing_labels': [
                config_label(c) for c in kept if c['options'].get('testing')
            ],
        }
    return summary


class XmsConanPackager(object):
    """The packager class."""

    #: Seconds a single shard may run before it is killed. 20 minutes, raised
    #: from the original 10: xmsvtk's slowest shard takes 497s on an idle
    #: runner, so the old ceiling left 21% headroom and a runner busy with a
    #: second pipeline pushed every shard past it. A killed shard reports as
    #: "0 failed, N errored", which reads like a test failure but is a
    #: stopwatch, so the ceiling should be far enough away that crossing it
    #: means a hang rather than a slow day.
    SHARD_TIMEOUT = 1200

    def __init__(
        self,
        library_name,
        conanfile_path='.',
        build_missing=False,
        artifacts_dir=None,
        test_shards=0,
        profile_options: Optional[dict] = None,
        python_versions: Optional[list[str]] = None,
        coverage: Optional[bool] = None,
        apply_boost_defaults: bool = True,
        profile_conf: Optional[dict] = None,
        profile_variants: Optional[list] = None,
        matrix: Optional[dict] = None,
    ):
        """Initialize the packager.

        Args:
            library_name: Name of the library to build.
            conanfile_path: Path to the conanfile.
            build_missing: If True, build missing dependencies from source.
            artifacts_dir: If set, test artifacts are saved here during builds.
            test_shards: If > 1, skip tests during build and run them afterward
                in parallel shards using GTest's GTEST_TOTAL_SHARDS.
            profile_options: Per-dependency option overrides written into each
                configuration's profile, e.g. {'boost': {'shared': True}}.
            profile_conf: ``[conf]`` entries for profiles written by
                :meth:`write_profiles`. None uses ``DEFAULT_PROFILE_CONF``; an
                empty dict omits the section.
            profile_variants: Additional generator variants, each a dict with
                ``name`` plus optional ``platforms`` (``linux``, ``mac_os``,
                ``windows``) / ``kinds`` (``library``, ``python``, ``testing``)
                filters and a ``conf`` overlay. A matching configuration is emitted a second
                time under ``<stem>_<name>``. Used for cases like Windows,
                where the same settings are built with both Ninja and Visual
                Studio; scoped rather than blanket so a repo does not get
                variants for configurations it never builds that way.
                Each dep/opt pair is emitted as a `pkg/*:opt=value` line in the
                profile's [options] section.
            python_versions: Python versions to fan out pybind builds across
                (e.g. ["3.10", "3.13"]). When None, falls back to the
                ``PYTHON_TARGET_VERSION`` environment variable (single value)
                or to ``DEFAULT_PYTHON_VERSIONS``. Each pybind variant is
                duplicated per version with the matching ``python_version``
                Conan option and ``PYTHON_TARGET_VERSION`` buildenv set.
            coverage: If True, sets the recipe's ``coverage=True`` option on
                every configuration, so CMake adds ``--coverage -O0 -g`` and
                the instrumented builds carry their own package_id. It does
                not change which configurations are produced: the testing-only
                Debug variant that drives C++ coverage is always emitted, and
                the Python half of coverage uses whatever pybind configuration
                ``[matrix]`` already names. Pybind profiles are instrumented
                too, so the binding layer -- C++ reachable only from Python --
                produces its own ``.gcda``. Because the CMake block forces
                ``-O0`` after CMake's own ``-O3``, a Release pybind build is
                unoptimized and its line data is as usable as a Debug build's,
                while still resolving against Release dependencies. When None,
                defaults to ``True`` iff ``XMS_COVERAGE=1`` is set in the
                environment.
            matrix: Which configurations the fan-out should produce, from the
                ``[matrix]`` table of ``build.toml``. ``compiler_runtime``
                restricts the platform's ``compiler.runtime`` values (a subset of
                ``["dynamic", "static"]``) before the cartesian product, so the
                ``wchar_t`` and ``testing`` copies shrink with the base
                configurations; it is inert on platforms that declare no such
                setting, since one ``build.toml`` serves every platform.
                ``pybind_build_types`` names the build types that get a pybind
                variant (a subset of ``["Release", "Debug"]``, default
                ``["Release"]``), and is the only thing that decides them --
                ``coverage`` instruments the configurations this names rather
                than adding one of its own. None means the full historical
                fan-out.
            apply_boost_defaults: If True (the default), inject the boost
                ``without_stacktrace`` / ``without_locale`` defaults described
                below into the profile options. Set to False when building
                against the legacy Aquaveo ``boost/1.74.0.3`` recipe (used by
                the VS2019 / msvc 192 matrix): both defaults are conan-center
                boost 1.86 options that the legacy recipe may not declare, and
                Conan fails the build when a profile sets an option the recipe
                does not define. A caller that needs only one of the two can
                still pass it explicitly through ``profile_options``.
        """
        self._library_name = library_name
        self._conanfile_path = conanfile_path
        self._configurations = None
        self._build_missing = build_missing
        self._artifacts_dir = os.path.abspath(artifacts_dir) if artifacts_dir else None
        self._test_shards = test_shards
        if test_shards > 1 and not self._artifacts_dir:
            # Sharding needs somewhere to have staged the runner. Without it,
            # the build still exports XMS_SKIP_CXX_TESTS=1 for every testing
            # configuration but never reaches _run_sharded_tests, so the run
            # skips the C++ suite, shards nothing, and exits 0 -- a green build
            # in which no test executed. Refuse the combination instead.
            raise ValueError(
                f'test_shards={test_shards} needs an artifacts_dir: the runner is '
                'sharded from the staged artifacts, and without them the recipe '
                'would skip the C++ tests and nothing would run them. Pass '
                '--artifacts-dir alongside --test-shards.'
            )
        self._profile_options = profile_options or {}
        # Only consulted by write_profiles(); the ephemeral build profile has
        # never carried a [conf] section and still does not.
        self._profile_conf = dict(DEFAULT_PROFILE_CONF) if profile_conf is None else dict(profile_conf)
        self._profile_variants = self._resolve_profile_variants(profile_variants)
        self._python_versions = self._resolve_python_versions(python_versions)
        self._matrix = self.resolve_matrix(matrix)
        self._coverage = (
            coverage if coverage is not None
            else os.environ.get('XMS_COVERAGE') == '1'
        )
        self.printer = Printer()
        self._temp_dir = tempfile.TemporaryDirectory()
        self._temp_dir_path = self._temp_dir.name

        # Both defaults below name conan-center boost 1.86 options. The legacy
        # Aquaveo boost/1.74.0.3 recipe required by the VS2019 (msvc 192)
        # matrix may not declare them, and Conan errors out on a profile option
        # an involved recipe does not define — hence the opt-out.
        if apply_boost_defaults:
            # Disable boost stacktrace to avoid __cxa_allocate_exception symbol
            # conflict with -static-libstdc++ in pybind shared modules.
            self._set_default_option_value('boost', 'without_stacktrace', True)

            # Disable boost.locale: boost/1.86.0 passes
            # `boost.locale.iconv.lib=libiconv` to b2 unconditionally on macOS,
            # but b2 >=5.2 validates command-line features before loading the
            # locale Jamfile and rejects it as unknown. No xms library uses
            # boost.locale, so dropping it is the cleanest unblock.
            self._set_default_option_value('boost', 'without_locale', True)

    def __del__(self):
        """Cleanup the temporary directory."""
        # Constructor validation can raise before _temp_dir is assigned.
        temp_dir = getattr(self, '_temp_dir', None)
        if temp_dir is not None:
            temp_dir.cleanup()

    DEFAULT_PYTHON_VERSIONS = ["3.13"]
    _PYTHON_VERSION_RE = PYTHON_VERSION_RE

    #: Keys a ``conan_profile_variants`` entry may carry.
    _VARIANT_KEYS = frozenset({'name', 'conf', 'platforms', 'kinds'})

    #: Values ``kinds`` may name; the return values of :meth:`configuration_kind`.
    _VARIANT_KINDS = frozenset({'library', 'python', 'testing'})

    #: Accepted values per ``[matrix]`` key. The keys double as the vocabulary
    #: check: anything else in the table is a misspelling, and a misspelling that
    #: is skipped rather than rejected produces the very fan-out the library was
    #: trying to trim.
    _MATRIX_VALUES = {
        'compiler_runtime': ('dynamic', 'static'),
        'pybind_build_types': ('Release', 'Debug'),
    }

    #: Boolean ``[matrix]`` keys, mapped to their default. Separate from
    #: :data:`_MATRIX_VALUES` because those validate membership in a vocabulary
    #: and a flag has none, only a type. Both feed the unknown-key check below,
    #: so a misspelled flag is rejected rather than quietly left at its default
    #: -- which for a switch that *removes* builds means the builds keep
    #: happening and the only symptom is a pipeline that is no faster.
    _MATRIX_FLAGS = {
        'wheel_only': False,
    }

    #: Build types that get a pybind variant when ``[matrix]`` does not say.
    DEFAULT_PYBIND_BUILD_TYPES = ('Release',)

    @classmethod
    def resolve_matrix(cls, matrix: Optional[dict]) -> dict:
        """Validate the ``[matrix]`` table and fill in its defaults.

        Public because it is a service to callers outside this package:
        :func:`xmsconan.generator_tools.build_file_generator.render_template_with_toml`
        validates ``[matrix]`` through this method before writing it verbatim
        into a generated ``conanfile.py``. Renaming it breaks build-file
        generation, not just this class.

        Both failure modes are silent otherwise. A misspelled key
        (``compiler_runtimes``) is simply never read, so the library keeps
        producing configurations it asked to stop producing, and an empty list
        yields no configurations at all -- a build that succeeds having packaged
        nothing.

        Args:
            matrix: The ``[matrix]`` table, or None for the full fan-out.

        Returns:
            The table with every accepted key present: ``compiler_runtime`` is
            None when unrestricted, ``pybind_build_types`` always a tuple, and
            ``wheel_only`` always a bool.

        Raises:
            ValueError: When ``matrix`` is not a mapping, carries an unknown key,
                or names a value outside the accepted vocabulary -- including an
                empty list, which would build nothing.
        """
        if matrix is None:
            matrix = {}
        if not isinstance(matrix, dict):
            raise ValueError(f'matrix must be a table, got {type(matrix).__name__}')

        accepted_keys = set(cls._MATRIX_VALUES) | set(cls._MATRIX_FLAGS)
        unknown = sorted(set(matrix) - accepted_keys)
        if unknown:
            raise ValueError(
                f'matrix has unknown key(s) {", ".join(unknown)}. '
                f'Accepted keys: {", ".join(sorted(accepted_keys))}.'
            )

        resolved = {}
        for key, default in cls._MATRIX_FLAGS.items():
            value = matrix.get(key, default)
            if not isinstance(value, bool):
                # A string "true" is the likely typo, and it is truthy, so an
                # unchecked value would turn the flag on for a repo that wrote
                # it wrong -- silently dropping the library configurations.
                raise ValueError(
                    f'matrix.{key} must be a boolean, got '
                    f'{type(value).__name__} ({value!r}).'
                )
            resolved[key] = value
        for key, accepted in cls._MATRIX_VALUES.items():
            values = matrix.get(key)
            if values is None:
                resolved[key] = None
                continue
            if not isinstance(values, (list, tuple)):
                raise ValueError(
                    f'matrix.{key} must be a list, got {type(values).__name__}'
                )
            if not values:
                raise ValueError(
                    f'matrix.{key} is empty, which would build nothing. '
                    f'Omit the key for every value, or name a subset of: {", ".join(accepted)}.'
                )
            invalid = sorted(str(value) for value in values if value not in accepted)
            if invalid:
                raise ValueError(
                    f'matrix.{key} names unknown value(s) {", ".join(invalid)}. '
                    f'Accepted values: {", ".join(accepted)}.'
                )
            resolved[key] = tuple(values)

        if resolved['pybind_build_types'] is None:
            resolved['pybind_build_types'] = cls.DEFAULT_PYBIND_BUILD_TYPES
        return resolved

    @classmethod
    def _resolve_profile_variants(cls, profile_variants):
        """Validate and normalize the ``conan_profile_variants`` list.

        Checked here rather than where it is used because both failure modes are
        otherwise invisible: a missing ``name`` raises a bare ``KeyError`` deep
        in :meth:`plan_profiles`, and a misspelled filter -- ``platforms =
        ["macos"]`` when the accepted spelling is ``mac_os`` -- raises nothing
        at all. The variant is simply never emitted, and the omission surfaces
        much later as a missing binary.

        Raises:
            ValueError: When an entry is not a mapping, omits ``name``, carries
                an unknown key, or names a platform or kind outside the
                accepted vocabulary.
        """
        if profile_variants is None:
            return []
        if not isinstance(profile_variants, (list, tuple)):
            raise ValueError(
                f'conan_profile_variants must be a list, got {type(profile_variants).__name__}'
            )

        platforms = sorted(set(cls._PLATFORM_KEYS.values()))
        kinds = sorted(cls._VARIANT_KINDS)
        resolved = []
        for variant in profile_variants:
            if not isinstance(variant, dict):
                raise ValueError(
                    f'conan_profile_variants entries must be tables, got {type(variant).__name__}'
                )
            name = variant.get('name')
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f'conan_profile_variants entry {variant!r} must have a non-empty "name"'
                )
            unknown = sorted(set(variant) - cls._VARIANT_KEYS)
            if unknown:
                raise ValueError(
                    f'conan_profile_variants entry {name!r} has unknown key(s) '
                    f'{", ".join(unknown)}. Accepted keys: {", ".join(sorted(cls._VARIANT_KEYS))}.'
                )
            conf = variant.get('conf')
            if conf is not None and not isinstance(conf, dict):
                raise ValueError(
                    f'conan_profile_variants entry {name!r} has a non-table "conf"'
                )
            for key, accepted in (('platforms', platforms), ('kinds', kinds)):
                values = variant.get(key)
                if values is None:
                    continue
                if not isinstance(values, (list, tuple)):
                    raise ValueError(
                        f'conan_profile_variants entry {name!r} has a non-list "{key}"'
                    )
                invalid = sorted(str(v) for v in values if v not in accepted)
                if invalid:
                    raise ValueError(
                        f'conan_profile_variants entry {name!r} names unknown {key} '
                        f'{", ".join(invalid)}. Accepted values: {", ".join(accepted)}.'
                    )
            resolved.append(dict(variant))
        return resolved

    @classmethod
    def _resolve_python_versions(cls, python_versions: Optional[list[str]]):
        """Resolve the python_versions list from the constructor arg or env.

        Validation here only checks the *shape* of each entry (a ``"X.Y"``
        string). It does not check membership in the recipe's allowed set
        (see ``XmsConan2File.options["python_version"]``); a syntactically
        valid but unsupported version like ``"3.11"`` will pass this gate
        and fail later when Conan rejects the option.

        Raises:
            ValueError: When ``python_versions`` is not iterable, contains a
                non-string entry, or contains a string that is not in
                ``"X.Y"`` form.
        """
        if python_versions is not None:
            if not isinstance(python_versions, (list, tuple)):
                raise ValueError(
                    f'python_versions must be a list or tuple, got {type(python_versions).__name__}'
                )
            if python_versions:
                cleaned = []
                for entry in python_versions:
                    if not isinstance(entry, str) or not cls._PYTHON_VERSION_RE.match(entry):
                        raise ValueError(
                            f'python_versions entries must be "X.Y" strings, got {entry!r}'
                        )
                    cleaned.append(entry)
                return cleaned
        env_version = os.getenv('PYTHON_TARGET_VERSION')
        if env_version:
            if not cls._PYTHON_VERSION_RE.match(env_version):
                raise ValueError(
                    f'PYTHON_TARGET_VERSION must be an "X.Y" string, got {env_version!r}'
                )
            return [env_version]
        return list(cls.DEFAULT_PYTHON_VERSIONS)

    @staticmethod
    def _highest_python_version(versions):
        """Return the version string with the largest (major, minor) tuple."""
        return max(versions, key=version_sort_key)

    @property
    def python_versions(self):
        """Get the list of python versions pybind builds will fan out across."""
        return list(self._python_versions)

    @property
    def library_name(self):
        """Get the library name."""
        return self._library_name

    @property
    def configurations(self):
        """Get the configurations for the build process."""
        return self._configurations

    def generate_configurations(self, system_platform=None):
        """
        Generate the configurations for the build process.

        Considers common Conan settings like arch, build_type, compiler, and os. Also considers standard XMS options:
        pybind, testing, wchar_t. Result is essentially every combination of values for those settings that make sense
        to build for the given platform.

        Args:
            system_platform: Key into the module-level ``configurations`` dict
                (e.g. ``'windows'``, ``'windows_vs2019'``, ``'linux'``,
                ``'darwin'``). When None the platform is auto-detected from
                ``platform.system()`` and the detected architecture replaces the
                configured one.

        Raises:
            ValueError: When ``system_platform`` is not a key of
                ``configurations``. This is user-facing input (a CLI flag), so
                the message names the unknown key and lists the valid ones
                instead of failing with a bare ``AttributeError`` on None.
        """
        # Get system_platform name
        auto_detected = system_platform is None
        if system_platform is None:
            system_platform = platform.system().lower()

        # Get the current system_platform configuration
        if system_platform not in configurations:
            raise ValueError(
                f'Unknown platform {system_platform!r}. Valid platforms are: '
                f'{", ".join(sorted(configurations))}.'
            )
        system_platform_configuration = configurations[system_platform].copy()

        # Trim the base matrix before the product so the wchar_t and testing
        # copies shrink with it. Platforms that declare no compiler.runtime
        # (linux, darwin) ignore the restriction rather than failing: one
        # build.toml drives every platform, so a Windows-only statement has to
        # be inert elsewhere.
        runtimes = self._matrix['compiler_runtime']
        wheel_only = self._matrix['wheel_only']
        if wheel_only and runtimes is None:
            # The wheel comes from the pybind configuration, and msvc only gets
            # one on the dynamic runtime (see _pybind_variants). Keeping the
            # static half would carry a Release/Debug testing pair that nothing
            # consumes -- two of the very builds wheel_only exists to remove.
            # An explicit [matrix].compiler_runtime still wins; it is already
            # required to include "dynamic" by the check below.
            runtimes = ('dynamic',)
        if runtimes and 'compiler.runtime' in system_platform_configuration:
            kept = [
                runtime for runtime in system_platform_configuration['compiler.runtime']
                if runtime in runtimes
            ]
            if not kept:
                raise ValueError(
                    f'matrix.compiler_runtime {list(runtimes)} keeps none of the runtimes '
                    f'{system_platform_configuration["compiler.runtime"]} that platform '
                    f'{system_platform!r} declares, so nothing would be built.'
                )
            # A pybind variant is only produced for a dynamic-runtime msvc
            # configuration (see _pybind_variants), so a static-only restriction
            # silently yields a matrix with no module and no wheel. Downstream
            # that is either an opaque "No .whl files found" from the repair
            # step or -- with windows_wheel_repair off -- a green pipeline that
            # publishes nothing. Rejected here for the same reason an empty
            # value list is.
            if 'dynamic' not in kept:
                raise ValueError(
                    f'matrix.compiler_runtime {list(runtimes)} leaves platform '
                    f'{system_platform!r} with no dynamic-runtime configuration, and a '
                    f'pybind variant is only produced for those -- so no module and no '
                    f'wheel would be built. Include "dynamic", or drop the pybind '
                    f'configurations deliberately with a --filter.'
                )
            system_platform_configuration['compiler.runtime'] = kept

        # Override arch with detected architecture only when platform was auto-detected
        if auto_detected:
            system_platform_configuration['arch'] = [get_current_arch()]

        # Get the cartesian product of all the configurations
        keys = system_platform_configuration.keys()
        values = (system_platform_configuration[key] for key in keys)
        combinations = [dict(zip(keys, combination)) for combination in itertools.product(*values)]

        xms_version = os.getenv('XMS_VERSION', None)
        # Non-pybind builds don't depend on the python version (the recipe
        # drops it from package_id), but PYTHON_TARGET_VERSION still feeds
        # CMake / pre-conan2 paths, so seed it with the highest version.
        default_python_version = self._highest_python_version(self._python_versions)
        ci_commit_tag = os.environ.get('CI_COMMIT_TAG', 'False')  # Gitlab
        release_python = os.getenv('RELEASE_PYTHON', 'False')

        if ci_commit_tag != 'False':
            release_python = 'True'

        for combination in combinations:
            combination['options'] = {
                'wchar_t': 'builtin',
                'pybind': False,
                'testing': False,
            }
            # AQUAPI_USERNAME / AQUAPI_PASSWORD / AQUAPI_URL are deliberately
            # absent. Conan prints the profile it is handed to stdout under
            # "Input profiles" before every build, so anything placed here is
            # published to the CI job log -- and the devpi password was, on
            # every build, in cleartext.
            #
            # Nothing needed them here to begin with. The recipe never reads
            # AQUAPI_*; neither does the generated CMake. The three real
            # consumers -- `xmsconan wheel-deploy`, `xmsconan docker-run` and
            # the credential resolver behind them -- read os.environ directly
            # and run outside any conan build, so they are unaffected. A build
            # step that genuinely needs one still inherits it from the process
            # environment; buildenv is what puts it in the log.
            combination['buildenv'] = {
                'XMS_VERSION': xms_version,
                'PYTHON_TARGET_VERSION': default_python_version,
                'CI_COMMIT_TAG': ci_commit_tag,
                'RELEASE_PYTHON': release_python,
            }
            if self._artifacts_dir:
                combination['buildenv']['XMS_TEST_ARTIFACTS_DIR'] = self._artifacts_dir

            # Set macOS deployment target for consistent wheel builds
            if combination.get('os') == 'Macos':
                combination['buildenv']['MACOSX_DEPLOYMENT_TARGET'] = '15.0'
                # Force correct platform tag for ARM-only wheels to prevent
                # universal2 tags from Apple's universal Python framework
                if combination.get('arch') == 'armv8':
                    combination['buildenv']['_PYTHON_HOST_PLATFORM'] = 'macosx-15.0-arm64'

        pybind_updated_builds = self._pybind_variants(combinations)
        testing_updated_builds = self._testing_variants(combinations)
        if wheel_only:
            # What is dropped is the library-only shape (pybind=False,
            # testing=False): the binary a C++ consumer links, which a
            # wheel_only library by definition has none of. The wchar_t copies
            # go with it -- they are a fan-out of that same shape, and the
            # /Zc:wchar_t- toggle only matters to a C++ consumer linking it.
            #
            # What survives is the three configurations that each produce
            # something: Release+testing and Debug+testing run the C++ suite,
            # and the pybind build carries the wheel and runs the Python suite.
            # They differ only in build_type and the pybind/testing options --
            # every setting matches, because the base they are copied from is
            # now a single runtime with no wchar_t variant.
            combinations = pybind_updated_builds + testing_updated_builds
        else:
            wchar_t_updated_builds = self._wchar_t_variants(combinations)
            combinations = combinations + wchar_t_updated_builds + pybind_updated_builds + testing_updated_builds

        # Coverage rides on the recipe's `coverage` *option*, not on
        # [buildenv], so an instrumented build carries its own package_id and
        # can never satisfy --build=missing for a production build (or vice
        # versa). The recipe forwards the option to CMake as -DXMS_COVERAGE,
        # which also retires the [buildenv] ride-along that issue #69 needed
        # for env propagation.
        #
        # This runs here rather than in the loop above because the pybind and
        # testing options that select a leg are only set by the variant
        # helpers -- inside the loop every configuration still carries the
        # reset defaults, so nothing would ever match.
        #
        # Instrumentation is per configuration, not blanket. The Debug testing
        # leg and the pybind leg between them reach every line anyone
        # measures -- the binding layer is only compiled in the latter --
        # while the Release testing leg stays optimized on purpose: it exists
        # to exercise the configuration the shipped wheel is built from, and
        # an instrumented copy of it would prove nothing about what ships.
        if self._coverage:
            for combination in combinations:
                if is_instrumented_configuration(combination):
                    combination['options']['coverage'] = True

        self._configurations = combinations
        return combinations

    @staticmethod
    def _wchar_t_variants(combinations):
        """Return a ``wchar_t=typedef`` copy of every msvc configuration.

        ``wchar_t`` selects between the MSVC built-in type and the legacy
        typedef, so it only fans out on msvc; gcc and apple-clang yield nothing
        here.

        Args:
            combinations: The base configurations to derive from.

        Returns:
            A new list of deep-copied configurations (possibly empty).
        """
        variants = []
        for combination in combinations:
            if combination['compiler'] == 'msvc':
                wchar_t_options = copy.deepcopy(combination)
                wchar_t_options['options'].update({
                    'wchar_t': 'typedef',
                })
                variants.append(wchar_t_options)
        return variants

    def _pybind_build_types(self) -> set:
        """Return the build types a pybind variant is produced for.

        ``[matrix].pybind_build_types`` decides this alone, defaulting to
        Release only: for most libraries the Release wheel is what ships and a
        Debug module is redundant. A library whose consumers link a Debug module
        names Debug as well.

        Coverage used to add Debug on top, so that a Debug+pybind build could
        instrument the Python-reachable C++ surface. It no longer needs its own
        build type to do that: ``xmsconan coverage`` instruments the pybind
        configuration the matrix already produces and merges its ``.gcda`` into
        the C++ report, so the binding layer is still measured. Requiring Debug
        specifically cost every dependency a Debug+pybind binary -- the one
        combination the xms libraries do not publish -- while buying nothing,
        because the ``XMS_COVERAGE`` CMake block appends ``-O0 -g`` after
        CMake's ``-O3`` and a Release build is therefore unoptimized anyway.

        Returns:
            The build-type names, as a set.
        """
        return set(self._matrix['pybind_build_types'])

    def _pybind_variants(self, combinations):
        """Return the pybind copies of ``combinations``, one per python version.

        The companion testing-only Debug build (emitted unconditionally by
        ``_testing_variants``) handles C++ coverage. On msvc, only the
        dynamic-runtime configurations get a pybind variant; every other
        toolchain fans out from all of them.

        Args:
            combinations: The base configurations to derive from.

        Returns:
            A new list of deep-copied configurations, ``len(python_versions)``
            per eligible base configuration.
        """
        build_types = self._pybind_build_types()
        variants = []
        for combination in combinations:
            if combination['build_type'] in build_types and \
                    (combination['compiler'] != 'msvc' or combination['compiler.runtime'] in ['dynamic']):
                for py_version in self._python_versions:
                    pybind_options = copy.deepcopy(combination)
                    pybind_options['options'].update({
                        'pybind': True,
                        'python_version': py_version,
                    })
                    pybind_options['buildenv']['PYTHON_TARGET_VERSION'] = py_version
                    variants.append(pybind_options)
        return variants

    @staticmethod
    def _testing_variants(combinations):
        """Return a ``testing=True`` copy of every configuration.

        Args:
            combinations: The base configurations to derive from.

        Returns:
            A new list of deep-copied configurations, one per input.
        """
        variants = []
        for combination in combinations:
            testing_options = copy.deepcopy(combination)
            testing_options['options'].update({
                'testing': True,
            })
            variants.append(testing_options)
        return variants

    def filter_configurations(self, filter_dict):
        """
        Filter the configurations based on the filter_dict.

        Should only be called after `self.generate_configurations` has been called to initialize the configurations.

        The filter dict specifies values of things to keep. Example:
        {
          'options': { 'testing': True },  # only keep testing configurations
          'build_type': 'Debug'  # ... that are built in debug mode
        }

        Raises ``ValueError`` (via ``validate_filter_dict``) when a top-level key
        is neither a known configuration setting nor one of
        ``options``/``buildenv`` — the prior behavior silently dropped such keys,
        which is how flat ``pybind``/``testing`` filters slipped past unnoticed
        (see issue #62).

        Settings that this platform's configurations don't carry (e.g.
        ``compiler.runtime``, which only Windows emits) are skipped rather than
        excluding everything. That keeps a platform-specific ``[filter]`` in
        ``build.toml`` usable on every platform. ``options``/``buildenv`` keys
        are the opposite -- an absent one does not match, see
        ``_configuration_matches`` -- because ``python_version`` is set only on
        the pybind variants, so treating it as absent-matches-anything made a
        one-ABI filter select the whole matrix.
        """
        validate_filter_dict(filter_dict)
        if self.configurations is None:
            return
        self._configurations = [
            configuration for configuration in self.configurations
            if _configuration_matches(configuration, filter_dict)
        ]

    def _config_label(self, combination):
        """Return a human-readable label for a build configuration.

        Thin delegate to the module-level :func:`config_label`; see there for
        what the label has to guarantee.
        """
        return config_label(combination)

    def run(self, log_dir=None):
        """Run the build process.

        Args:
            log_dir: When set, each configuration's ``conan create`` output is
                redirected to ``<log_dir>/<library>-<config label>.log`` and a
                one-line pointer is printed to the console instead. Intended
                for long batch runs (e.g. the manual VS2019 matrix), where a
                wall of interleaved compiler output is unreadable. Defaults to
                None, which keeps the historical behavior of letting the child
                process inherit stdout/stderr. An empty string is treated the
                same as None (a shell that expands ``--log-dir "$X"`` to
                nothing must not scatter bare log files into the cwd).
                Logging is best-effort: if the directory or a log file cannot
                be opened, a warning is printed and that output falls back to
                the console rather than aborting a multi-hour run.

        Returns:
            The number of failed configurations plus failed test shards; 0 when
            everything succeeded.
        """
        self.printer.print_ascci_art()
        self.print_configuration_table()
        if log_dir:
            try:
                os.makedirs(log_dir, exist_ok=True)
            except OSError as exc:
                self.printer.print_message(
                    f'WARNING: could not create log directory {log_dir} ({exc}); '
                    f'falling back to console output.'
                )
                log_dir = None
        failing_configurations = []
        sharded_test_runs = []
        for i, combination in enumerate(self.configurations):
            self.printer.print_message('*-' * 40 + '\n')
            self.printer.print_message(f'Building configuration {i + 1} of {len(self.configurations)}')
            build_combination = combination
            if self._artifacts_dir:
                build_combination = copy.deepcopy(combination)
                build_combination['buildenv']['XMS_TEST_ARTIFACTS_LABEL'] = self._config_label(combination)
            is_testing = combination.get('options', {}).get('testing', False)
            shard_this = is_testing and self._test_shards > 1
            env = None
            if shard_this:
                env = os.environ.copy()
                env['XMS_SKIP_CXX_TESTS'] = '1'
            profile_path = self.create_build_profile(build_combination)
            self.printer.print_profile(profile_path)
            cmd = ['conan', 'create', self._conanfile_path, '--profile', profile_path]
            if self._build_missing:
                cmd.append('--build=missing')
            try:
                self._conan_create(cmd, env, self._config_label(combination), log_dir)
                self.printer.print_message(f'Finished building configuration {i + 1} of {len(self.configurations)}')
                if shard_this and self._artifacts_dir:
                    label = self._config_label(combination)
                    sharded_test_runs.append(label)
            except subprocess.CalledProcessError:
                self.printer.print_message(f'ERROR building configuration {i + 1} of {len(self.configurations)}')
                failing_configurations.append(i)
            self.printer.print_message('*-' * 40 + '\n')

        # Run sharded tests after all builds complete
        shard_failures = []
        for label in sharded_test_runs:
            shard_failures.extend(self._run_sharded_tests(label))
        # A runner that was never built is a different fault from a shard that
        # ran and failed, and the ERROR already printed for it says so. Folding
        # both into one "N test shard(s) failed" line renames the cause at the
        # only place a reader looks for the summary.
        missing_runners = [
            f[:-len(RUNNER_MISSING_SUFFIX)]
            for f in shard_failures
            if f.endswith(RUNNER_MISSING_SUFFIX)
        ]
        shard_failure_count = len(shard_failures)

        total_failures = len(failing_configurations) + shard_failure_count
        if total_failures > 0:
            if failing_configurations:
                self.printer.print_message('The following configurations failed to build:')
                self.print_configuration_table(failing_configurations)
            if missing_runners:
                self.printer.print_message(
                    f'{len(missing_runners)} configuration(s) ran no C++ test at '
                    f'all -- test runner missing: {", ".join(missing_runners)}'
                )
            failed_shards = shard_failure_count - len(missing_runners)
            if failed_shards:
                self.printer.print_message(f'{failed_shards} test shard(s) failed.')
            return total_failures
        else:
            self.printer.print_message('All configurations built successfully.')
            return 0

    def _archive_existing_log(self, log_path):
        """Move an existing log aside so a re-run cannot destroy it.

        The canonical path stays ``<library>-<label>.log`` (that is what the
        docs and the console pointer name), so the *previous* run's file is the
        one that moves, to ``<library>-<label>.<timestamp>.log`` where the
        timestamp is that file's own mtime. Re-running after a failure is the
        normal way to work through a long matrix, and the failed run's compiler
        output is exactly what the developer still needs.

        Args:
            log_path: Path the current run is about to write.

        Raises:
            OSError: When the existing log cannot be renamed; the caller
                degrades to console output.
        """
        if not os.path.exists(log_path):
            return
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(os.path.getmtime(log_path)))
        root, extension = os.path.splitext(log_path)
        archived = f'{root}.{stamp}{extension}'
        # Two runs of the same configuration can share an mtime second (a
        # configuration that fails immediately), so disambiguate.
        counter = 1
        while os.path.exists(archived):
            archived = f'{root}.{stamp}.{counter}{extension}'
            counter += 1
        os.replace(log_path, archived)
        self.printer.print_message(f'Archived previous log to {archived}')

    def _conan_create(self, cmd, env, label, log_dir):
        """Run one ``conan create``, optionally redirecting its output to a log.

        Args:
            cmd: The ``conan create`` argv.
            env: Environment for the child process; None inherits this one's.
            label: Configuration label used to name the log file.
            log_dir: Directory for the log file, or None/empty to let the child
                inherit stdout/stderr.

        Raises:
            subprocess.CalledProcessError: When ``conan create`` fails.
        """
        if not log_dir:
            subprocess.run(cmd, check=True, env=env)
            return
        log_path = os.path.join(log_dir, f'{self._library_name}-{label}.log')
        try:
            self._archive_existing_log(log_path)
            log_file = open(log_path, 'w', encoding='utf-8')
        except OSError as exc:
            # Losing a log must not lose the build: fall back to the console
            # rather than letting OSError escape run()'s CalledProcessError
            # handler and take the whole matrix (and its summary) down.
            self.printer.print_message(
                f'WARNING: could not write {log_path} ({exc}); '
                f'falling back to console output for this configuration.'
            )
            subprocess.run(cmd, check=True, env=env)
            return
        self.printer.print_message(f'Logging output to {log_path}')
        with log_file:
            subprocess.run(cmd, check=True, env=env, stdout=log_file, stderr=subprocess.STDOUT)

    def _run_sharded_tests(self, label):
        """Run test runner in parallel shards using GTest sharding.

        Args:
            label: The artifact label (e.g. "Debug-testing") to find the runner.

        Returns:
            List of failure labels, or an empty list if every shard passed. A
            missing runner counts as a failure: with ``test_shards > 1`` the
            recipe deliberately skips ``cmake.test()`` so these shards are the
            only thing that runs the C++ tests, so returning [] for an absent
            runner reported "All configurations built successfully" for a run
            in which no test executed at all. The generated GitLab job exits 1
            on the same condition.
        """
        artifact_dir = os.path.join(self._artifacts_dir, label)
        runner_name = "runner.exe" if sys.platform == "win32" else "runner"
        runner_path = os.path.join(artifact_dir, runner_name)
        if not os.path.isfile(runner_path):
            self.printer.print_message(
                f'ERROR: test runner not found at {runner_path}. With '
                f'test_shards={self._test_shards} the recipe skips cmake.test(), '
                f'so no C++ test ran for {label}.'
            )
            return [f'{label}{RUNNER_MISSING_SUFFIX}']

        os.chmod(runner_path, 0o755)

        self.printer.print_message(f'Running {self._test_shards} test shards for {label}')

        failures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._test_shards) as executor:
            futures = {}
            for shard_index in range(self._test_shards):
                env = os.environ.copy()
                env['GTEST_TOTAL_SHARDS'] = str(self._test_shards)
                env['GTEST_SHARD_INDEX'] = str(shard_index)
                output_file = os.path.join(artifact_dir, f'TEST-shard-{shard_index}.xml')
                cmd = [runner_path, f'--gtest_output=xml:{output_file}']
                future = executor.submit(subprocess.run, cmd, env=env, timeout=self.SHARD_TIMEOUT)
                futures[future] = shard_index

            for future in concurrent.futures.as_completed(futures):
                shard_index = futures[future]
                try:
                    result = future.result()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.printer.print_message(f'  Shard {shard_index + 1}/{self._test_shards} ERROR: {exc}')
                    failures.append(f'{label}-shard-{shard_index}')
                    continue
                if result.returncode != 0:
                    self.printer.print_message(f'  Shard {shard_index + 1}/{self._test_shards} FAILED')
                    failures.append(f'{label}-shard-{shard_index}')
                else:
                    self.printer.print_message(f'  Shard {shard_index + 1}/{self._test_shards} passed')

        if not failures:
            self.printer.print_message(f'All {self._test_shards} shards passed for {label}')
        return failures

    def upload(self, version, remote=DEFAULT_REMOTE_NAME, package_query=None):
        """Upload the packages to the server.

        Args:
            version: Package version to upload; every package matching
                ``<library>/<version>*`` in the local cache is sent.
            remote: Conan remote name to upload to. Defaults to
                ``DEFAULT_REMOTE_NAME`` (the CI-published aquaveo-stable
                remote). The manually-built VS2019 / msvc 192 matrix passes
                ``VS2019_REMOTE_NAME`` so those binaries stay out of the
                CI remote.
            package_query: Conan binary query passed through as
                ``-p/--package-query``, e.g. ``'compiler.version=192'``.
                Without it, ``conan upload`` matches by *reference* only and
                publishes every binary of that version sitting in the local
                cache — on a workstation that is both the VS2019 build box and
                a normal dev machine, that quietly pushes msvc 194 binaries to
                the VS2019 remote and exits 0. Callers that publish to a
                toolchain-specific remote must pass the matching query.

        Returns:
            0 when ``conan upload`` succeeded, 1 when it failed. Shaped as a
            process exit code — the same convention ``run()`` uses — because
            both callers (the generated ``build.py`` and ``xmsconan_vs2019
            upload``) turn the result straight into one. A failed publish to
            a shared remote must never be reported as success.
        """
        self.printer.print_message(f'Uploading packages to the server ({remote}).')
        cmd = ['conan', 'upload', f'{self._library_name}/{version}*', '-r', remote, '--confirm']
        if package_query:
            # Verified against the pinned conan 2.31 client: `conan upload`
            # accepts `-p/--package-query`.
            cmd += ['-p', package_query]
            self.printer.print_message(f'Restricting upload to packages matching: {package_query}')
        try:
            subprocess.run(cmd, check=True)
            self.printer.print_message('Finished uploading')
        except subprocess.CalledProcessError as exc:
            self.printer.print_message(
                f'ERROR uploading {self._library_name}/{version}* to {remote} '
                f'(conan upload exited {exc.returncode}).'
            )
            return 1
        self.printer.print_message('*-' * 40 + '\n')
        self.printer.print_message('All packages uploaded successfully.')
        return 0

    def extract_wheel(self, output_dir, version='*'):
        """Extract pre-built wheels from every pybind Conan package.

        With multiple ``python_version`` options there can be more than one
        pybind binary in the cache (e.g. one for 3.10 and one for 3.13);
        every distinct ``python_version`` value yields a separate wheel and
        all of them are copied to ``output_dir``.

        Args:
            output_dir: Directory to copy the .whl files into.
            version: Package version (default '*' matches any).

        Returns:
            True only when a wheel was extracted for every version in
            ``self.python_versions``. Returns False on a partial fan-out
            (some expected versions missing) or when nothing was extracted,
            so callers can distinguish "shipped all wheels" from "shipped a
            subset" before publishing.
        """
        ref = f'{self._library_name}/{version}'
        result = subprocess.run(
            ['conan', 'list', f'{ref}:*', '--format=json'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # "No packages found" was printed for both an empty cache and a
            # conan client that blew up -- and it dropped conan's stderr, which
            # is the only place the real cause (bad remote, corrupt cache, a
            # reference conan cannot parse) appears.
            self.printer.print_message(
                f'ERROR: `conan list {ref}:*` exited {result.returncode}; cannot '
                f'tell whether any wheel exists. conan said:\n{result.stderr.strip()}'
            )
            return False

        data = json.loads(result.stdout)
        # Collect all pybind candidates, newest revision first, Release ahead of
        # any other build type for the same python_version.
        #
        # build_type has to be part of the decision, not just the dedupe key.
        # ``ts`` is the *recipe-revision* timestamp, shared by every binary in
        # the revision, so a Release and a Debug pybind package for one
        # python_version tie on (ts, py_version, exact_ref) and the winner would
        # be whichever package-id hash happens to sort higher -- a choice that
        # flips on any dependency bump. Both carry a wheel whose filename is
        # identical, so nothing downstream could tell which one got published.
        # Debug is only ever a candidate at all because [matrix]
        # pybind_build_types can now ask for it; Release is what ships.
        candidates = []
        for exact_ref, cache in data.get('Local Cache', {}).items():
            for rev in cache.get('revisions', {}).values():
                ts = rev.get('timestamp', 0)
                for pid, pinfo in rev.get('packages', {}).items():
                    info = pinfo.get('info', {})
                    options = info.get('options', {})
                    if options.get('pybind') == 'True':
                        py_version = options.get('python_version', '')
                        build_type = info.get('settings', {}).get('build_type', '')
                        release_first = 0 if build_type == 'Release' else 1
                        candidates.append(
                            (-ts, release_first, py_version, exact_ref, pid, build_type)
                        )
        candidates.sort()

        seen_python_versions = set()
        extracted = False
        for _ts, _release_first, py_version, exact_ref, pid, build_type in candidates:
            if py_version in seen_python_versions:
                self.printer.print_message(
                    f'Ignoring the {build_type or "unknown"}-build wheel for python '
                    f'{py_version}: a wheel for that version was already staged.'
                )
                continue
            if build_type and build_type != 'Release':
                self.printer.print_message(
                    f'Warning: the only pybind package for python {py_version} is a '
                    f'{build_type} build. Staging its wheel, which is not what a release '
                    f'should publish.'
                )
            path_result = subprocess.run(
                ['conan', 'cache', 'path', f'{exact_ref}:{pid}'],
                capture_output=True, text=True
            )
            pkg_dir = path_result.stdout.strip()
            if path_result.returncode != 0 or not pkg_dir:
                # os.path.join('', 'dist') is 'dist' -- a cwd-relative path. On
                # a build machine that happens to hold a stale ./dist, the
                # staged wheel would come from there and get published as this
                # package's.
                self.printer.print_message(
                    f'ERROR: `conan cache path {exact_ref}:{pid}` exited '
                    f'{path_result.returncode} with no path; skipping this '
                    f'package. conan said:\n{path_result.stderr.strip()}'
                )
                continue
            dist_dir = os.path.join(pkg_dir, 'dist')
            if not os.path.isdir(dist_dir):
                continue
            seen_python_versions.add(py_version)
            os.makedirs(output_dir, exist_ok=True)
            for fname in os.listdir(dist_dir):
                if fname.endswith('.whl'):
                    shutil.copy2(os.path.join(dist_dir, fname), output_dir)
                    self.printer.print_message(f'Extracted {fname} to {output_dir}')
                    extracted = True

        if not extracted:
            self.printer.print_message('No pybind package found to extract.')
            return False

        expected_versions = set(self._python_versions)
        missing = expected_versions - seen_python_versions
        if missing:
            missing_list = ', '.join(sorted(missing))
            self.printer.print_message(
                f'Warning: extracted wheels for {sorted(seen_python_versions)} '
                f'but expected {sorted(expected_versions)} '
                f'(missing python_version: {missing_list}).'
            )
            return False
        return True

    def collect_dependency_libs(self, output_dir):
        """Collect shared libraries from the Conan cache for wheel repair.

        Scans all packages in the Conan cache and copies shared libraries
        (.so, .dylib, .dll) into output_dir so repair tools can find them.

        Copied libraries have their modification time reset to now. Conan
        zeroes mtimes in package tarballs, so libraries restored from a
        remote land in the cache dated 1970. The repair tools (delvewheel,
        auditwheel, delocate) write these libraries into the wheel's ZIP
        archive, and ZIP cannot represent timestamps before 1980.

        Args:
            output_dir: Directory to copy shared libraries into.
        """
        result = subprocess.run(
            ['conan', 'config', 'home'], capture_output=True, text=True
        )
        conan_home = result.stdout.strip()
        if result.returncode != 0 or not conan_home:
            # Unchecked, an empty stdout makes cache_pkg_dir the cwd-relative
            # 'p', which almost never exists -- so the repair silently gets no
            # dependency libraries instead of reporting why.
            self.printer.print_message(
                f'ERROR: `conan config home` exited {result.returncode} with no '
                f'path; no dependency libraries will be staged. conan said:\n'
                f'{result.stderr.strip()}'
            )
            return
        cache_pkg_dir = os.path.join(conan_home, 'p')

        if not os.path.isdir(cache_pkg_dir):
            self.printer.print_message('Conan cache not found.')
            return

        os.makedirs(output_dir, exist_ok=True)
        count = 0
        for root, _dirs, files in os.walk(cache_pkg_dir):
            for fname in files:
                is_shared_lib = fname.endswith(('.so', '.dylib', '.dll')) or '.so.' in fname
                if is_shared_lib:
                    dst = os.path.join(output_dir, fname)
                    if not os.path.exists(dst):
                        shutil.copy2(os.path.join(root, fname), dst)
                        os.utime(dst, None)
                        count += 1
        self.printer.print_message(f'Collected {count} shared libraries to {output_dir}')

    def repair_linux_wheel(self, wheel_dir):
        """Repair a Linux wheel for manylinux_2_28 using a Docker container.

        Runs auditwheel inside quay.io/pypa/manylinux_2_28 to produce a
        portable manylinux wheel. Requires Docker. Auditwheel is itself
        python-version-agnostic for the repair step, but the manylinux
        image needs *some* installed interpreter to run pip; we use the
        highest version from ``self.python_versions``.

        Args:
            wheel_dir: Directory containing the .whl file and libs/ subdirectory.
        """
        machine = platform.machine().lower()
        arch_map = {
            'x86_64': 'x86_64',
            'amd64': 'x86_64',
            'aarch64': 'aarch64',
            'arm64': 'aarch64',
        }
        arch = arch_map.get(machine, machine)
        image = f'quay.io/pypa/manylinux_2_28_{arch}'
        abs_wheel_dir = os.path.abspath(wheel_dir)

        python_version = self._highest_python_version(self._python_versions)
        cp_tag = f'cp{python_version.replace(".", "")}'

        self.printer.print_message(f'Repairing wheel with auditwheel in {image} (python {python_version})')
        cmd = [
            'docker', 'run', '--rm',
            '-v', f'{abs_wheel_dir}:/wheels',
            image,
            'bash', '-c',
            f'export PATH="/opt/python/{cp_tag}-{cp_tag}/bin:$PATH" && '
            'pip install auditwheel patchelf && '
            'LD_LIBRARY_PATH=/wheels/libs auditwheel repair /wheels/*.whl '
            '-w /wheels_repaired && '
            'rm -f /wheels/*.whl && rm -rf /wheels/libs && '
            'mv /wheels_repaired/* /wheels/ && rm -rf /wheels_repaired'
        ]
        subprocess.run(cmd, check=True)
        self.printer.print_message('Wheel repair completed successfully.')

    def _serialize_profile(self, configuration, path, skip_empty=False, conf=None):
        """Write one configuration to a Conan profile file.

        Single serialization path shared by the ephemeral build profile and the
        profiles written into a repository by :meth:`write_profiles`. It writes
        ``[buildenv]`` as it finds it, on purpose: every profile is public --
        the committed one obviously, and the ephemeral one because
        :meth:`create_build_profile` prints it and conan echoes it under
        "Input profiles" -- so the keys are held to ``PUBLIC_BUILDENV_KEYS``
        where the configurations are generated, not filtered here. A filter at
        this point would protect the file on disk and not the log.

        Args:
            configuration: One entry from :attr:`configurations`.
            path: Destination file path.
            skip_empty: Drop buildenv entries whose value is None. Without this
                an unset variable serializes as the literal string ``None``,
                which Conan would faithfully export into the build.
            conf: Mapping written as a ``[conf]`` section. None omits the
                section entirely, preserving the ephemeral profile's shape.
        """
        settings = {k: v for k, v in configuration.items() if k not in ['options', 'buildenv']}

        with open(path, 'w') as f:
            f.write('[settings]\n')
            for k, v in settings.items():
                f.write(f'{k}={v}\n')

            f.write('\n[options]\n')
            for k, v in configuration['options'].items():
                f.write(f'&:{k}={v}\n')

            for dep_name, dep_opts in _profile_order(self._profile_options):
                for opt_name, opt_value in dep_opts.items():
                    f.write(f'{dep_name}/*:{opt_name}={opt_value}\n')

            f.write('\n[buildenv]\n')
            for k, v in configuration['buildenv'].items():
                if skip_empty and v is None:
                    continue
                f.write(f'{k}={v}\n')

            if conf:
                f.write('\n[conf]\n')
                for k, v in conf.items():
                    f.write(f'{k}={v}\n')

        return path

    def create_build_profile(self, configuration):
        """Create a temporary build profile."""
        temp_profile_path = os.path.join(self._temp_dir_path, 'temp_profile')
        self._serialize_profile(configuration, temp_profile_path)
        print(f'Temporary profile created at: {temp_profile_path}')
        return temp_profile_path

    # Conan `os` value -> the platform key used in profile filenames and in a
    # variant's `platforms` filter. Kept in one place so the two cannot drift.
    _PLATFORM_KEYS = {'Macos': 'mac_os', 'Linux': 'linux', 'Windows': 'windows'}

    @classmethod
    def platform_key(cls, configuration):
        """Return the filename platform key for a configuration."""
        os_value = configuration.get('os')
        return cls._PLATFORM_KEYS.get(os_value, str(os_value or 'unknown').lower())

    @staticmethod
    def configuration_kind(configuration):
        """Return 'testing', 'python' or 'library' for a configuration.

        Mirrors how the hand-maintained xmsvtk profiles are grouped, and is what
        a variant's ``kinds`` filter matches against.
        """
        options = configuration.get('options', {})
        if options.get('testing'):
            return 'testing'
        if options.get('pybind'):
            return 'python'
        return 'library'

    @classmethod
    def variant_applies(cls, variant, configuration):
        """Whether a generator variant should be emitted for a configuration.

        An absent filter means "no restriction", so a variant with neither
        ``platforms`` nor ``kinds`` applies everywhere.
        """
        platforms = variant.get('platforms')
        if platforms and cls.platform_key(configuration) not in platforms:
            return False
        kinds = variant.get('kinds')
        if kinds and cls.configuration_kind(configuration) not in kinds:
            return False
        return True

    @classmethod
    def profile_name(cls, configuration):
        """Return the file stem for a configuration, e.g. ``mac_os_testing_debug``.

        Follows the naming convention already used by the hand-maintained
        profiles in xmsvtk so generated profiles are recognizable to anyone who
        has used those.
        """
        parts = [cls.platform_key(configuration), cls.configuration_kind(configuration),
                 str(configuration.get('build_type', '')).lower()]
        parts.extend(cls._discriminator_parts(configuration))
        return '_'.join(part for part in parts if part)

    @classmethod
    def _discriminator_parts(cls, configuration):
        """Return the name parts that separate otherwise identical configurations.

        Shared by :meth:`profile_name` and :meth:`preset_name`, which differ
        only in their prefix and separator: two copies of this would let a
        profile and the preset that consumes it drift apart on a new setting.
        """
        options = configuration.get('options', {})
        parts = []
        if options.get('pybind') and options.get('python_version'):
            parts.append('py' + str(options['python_version']).replace('.', ''))
        if options.get('wchar_t') and options['wchar_t'] != 'builtin':
            parts.append(str(options['wchar_t']))
        if configuration.get('compiler.runtime'):
            parts.append(str(configuration['compiler.runtime']))
        return parts

    def write_profiles(self, output_dir, system_platform=None):
        """Write one Conan profile per configuration into ``output_dir``.

        These are generated artifacts, regenerated from build.toml like the
        other generated build files — not local state to be hand-edited. They
        exist so that entry points other than ``build.py`` (a bare
        ``conan install``, ``conan editable``, an IDE, a fresh worktree) resolve
        the same package ids the build does, instead of whatever
        ``conan profile detect`` happens to produce.

        Returns:
            List of written profile paths, sorted.
        """
        if self._configurations is None:
            self.generate_configurations(system_platform)

        os.makedirs(output_dir, exist_ok=True)
        written = []
        for entry in self.plan_profiles(system_platform):
            path = os.path.join(output_dir, entry.filename)
            self._serialize_profile(entry.configuration, path, skip_empty=True, conf=entry.conf)
            written.append(path)

        return sorted(written)

    def plan_profiles(self, system_platform=None):
        """Return a :class:`ProfilePlan` for every profile to write.

        The single source of truth for what :meth:`write_profiles` and
        :meth:`plan_cmake_presets` produce, so a dry run reports exactly what a
        real run writes rather than re-deriving the names and drifting from it.
        """
        if self._configurations is None:
            self.generate_configurations(system_platform)

        planned = []
        used = {}
        for configuration in self._configurations:
            base_stem = self.profile_name(configuration)

            # Base rendering, plus one per generator variant that matches this
            # configuration. A variant only overlays [conf]; settings and
            # options are identical, which is what makes the pair meaningful.
            renderings = [(base_stem, self._profile_conf, None)]
            for variant in self._profile_variants:
                if not self.variant_applies(variant, configuration):
                    continue
                merged_conf = dict(self._profile_conf)
                merged_conf.update(variant.get('conf') or {})
                renderings.append((f"{base_stem}_{variant['name']}", merged_conf, variant['name']))

            for stem, conf, variant_name in renderings:
                # Deterministic disambiguation: identical stems would otherwise
                # silently overwrite one another and drop configurations.
                seen = used.get(stem, 0)
                used[stem] = seen + 1
                filename = stem if seen == 0 else f'{stem}_{seen + 1}'
                planned.append(ProfilePlan(
                    filename=f'{filename}.txt',
                    configuration=configuration,
                    conf=conf,
                    variant=variant_name,
                ))

        return planned

    @classmethod
    def preset_name(cls, configuration, variant_name=None, include_build_type=False):
        """Return the CMake preset name for a configuration.

        Deliberately shorter than :meth:`profile_name`: a presets file is
        consumed on the machine it was generated for, so the platform prefix
        would be noise. Build type is omitted for multi-config generators,
        which express it as a build preset instead of a second configure step.
        """
        parts = [cls.configuration_kind(configuration)]
        if include_build_type:
            parts.append(str(configuration.get('build_type', '')).lower())
        parts.extend(cls._discriminator_parts(configuration))
        if variant_name:
            parts.append(variant_name)
        return '-'.join(part for part in parts if part)

    def plan_cmake_presets(self, system_platform=None):
        """Return the CMakePresets.json document for this repository.

        Derived from the same plan as the profiles, so a preset and the profile
        that provisions it always name the same generator and build folder --
        the pair previously had to be kept in sync by hand.

        Configurations whose profile pins no generator are skipped: without one
        there is nothing to express that Conan's own generated presets do not
        already cover.
        """
        configure_presets = {}
        # Build types per preset, kept beside the document rather than inside
        # it: this is bookkeeping for the loop, and a stray key in a preset is
        # serialized straight into CMakePresets.json.
        preset_build_types = {}
        build_presets = []

        for entry in self.plan_profiles(system_platform):
            configuration = entry.configuration
            generator = (entry.conf or {}).get('tools.cmake.cmaketoolchain:generator')
            if not generator:
                continue

            multi_config = is_multi_config_generator(generator)
            build_type = str(configuration.get('build_type', 'Release'))
            # The same discriminators preset_name uses. Names and folders have
            # to agree on what makes a configuration distinct, or two presets
            # get different names and one binary directory.
            base_folder = build_folder_for_generator(
                generator,
                self.configuration_kind(configuration),
                self._discriminator_parts(configuration),
            )
            # Conan's cmake_layout appends the build type for a single-config
            # generator and only collapses to the bare folder for multi-config
            # (conan/tools/cmake/layout.py). The preset has to name the same
            # path, or it points at a conan_toolchain.cmake that was never
            # written -- and both build types would share one binary dir.
            folder = base_folder if multi_config else f'{base_folder}/{build_type}'
            name = self.preset_name(configuration, entry.variant, include_build_type=not multi_config)
            options = configuration.get('options', {})

            preset = configure_presets.get(name)
            if preset is None:
                cache_variables = {
                    'CMAKE_EXPORT_COMPILE_COMMANDS': 'ON',
                    'BUILD_TESTING': 'ON' if options.get('testing') else 'OFF',
                    'IS_PYTHON_BUILD': 'YES' if options.get('pybind') else 'NO',
                    # Relative to the preset file, not to wherever cmake was
                    # invoked from.
                    'CMAKE_INSTALL_PREFIX': '${sourceDir}/_install',
                }
                # Raw `cmake --preset` builds never run the recipe's build(),
                # so the coverage option cannot reach them; the regenerated
                # preset carries the flag instead of the retired
                # $ENV{XMS_COVERAGE} fallback in CMakeLists.txt. Written in
                # BOTH modes: a CMake cache variable persists across
                # reconfigures, so omitting the key after a coverage run
                # would leave a previously-instrumented build dir
                # instrumented — the explicit "0" overwrites the stale entry.
                cache_variables['XMS_COVERAGE'] = '1' if self._coverage else '0'
                if not multi_config:
                    cache_variables['CMAKE_BUILD_TYPE'] = build_type
                preset = {
                    'name': name,
                    'displayName': f'{name} ({generator})',
                    'generator': generator,
                    'binaryDir': folder,
                    'toolchainFile': f'{folder}/generators/conan_toolchain.cmake',
                    'cacheVariables': cache_variables,
                }
                configure_presets[name] = preset
                preset_build_types[name] = []

            if build_type not in preset_build_types[name]:
                preset_build_types[name].append(build_type)
                build_presets.append({
                    'name': f'{name}-{build_type.lower()}' if multi_config else name,
                    'displayName': f'{name} {build_type}',
                    'configurePreset': name,
                    'configuration': build_type,
                })

        ordered = []
        for name in sorted(configure_presets):
            preset = configure_presets[name]
            if is_multi_config_generator(preset['generator']):
                preset['cacheVariables']['CMAKE_CONFIGURATION_TYPES'] = ';'.join(
                    sorted(preset_build_types[name]))
            ordered.append(preset)

        return {
            'version': 6,
            'configurePresets': ordered,
            'buildPresets': sorted(build_presets, key=lambda b: b['name']),
        }

    def write_cmake_presets(self, path, system_platform=None):
        """Write CMakePresets.json to ``path``. Returns the path, or None."""
        document = self.plan_cmake_presets(system_platform)
        if not document['configurePresets']:
            return None
        with open(path, 'w') as presets_file:
            json.dump(document, presets_file, indent=2)
            presets_file.write('\n')
        return path

    def print_configuration_table(self, configurations_to_print=None):
        """
        Print the configuration table.

        Args:
            configurations_to_print (list): A list of configurations indexes to print.
        """
        if configurations_to_print is None:
            # print all configurations
            configurations_to_print = range(len(self.configurations))

        headers = ["#", "cppstd", "runtime", "build_type", "compiler", "compiler.version", "arch",
                   f"{self._library_name}:wchar_t", f"{self._library_name}:pybind",
                   f"{self._library_name}:testing", f"{self._library_name}:python_version"]
        table = []

        # Create the header row
        header_row = "| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} |" \
                     " {:^17} | {:^16} | {:^17} | {:^15} |".format(*headers)
        separator = "+-----+----------+----------+--------------+----------------+--------------------+--------+" \
                    "-------------------+------------------+-------------------+-----------------+"

        # Add the header row and separator to the table
        table.append(separator)
        table.append(header_row)
        table.append(separator)

        # Create the data rows
        for i in configurations_to_print:
            config = self.configurations[i]
            wchar_t_option = config['options'].get('wchar_t', False)
            pybind_option = config['options'].get('pybind', False)
            testing_option = config['options'].get('testing', False)
            python_version_option = config['options'].get('python_version', '-')
            row = ("| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} |"
                   " {:^17} | {:^16} | {:^17} | {:^15} |").format(
                i + 1,
                config.get("compiler.cppstd", ""),
                config.get("compiler.runtime", ""),
                config.get("build_type", ""),
                config.get("compiler", ""),
                config.get("compiler.version", ""),
                config.get("arch", ""),
                wchar_t_option,
                'True' if pybind_option else 'False',
                'True' if testing_option else 'False',
                python_version_option,
            )
            table.append(row)
            table.append(separator)

        # Print the table
        print('\n')
        for line in table:
            print(line)

    def _set_default_option_value(self, package: str, option: str, value: Any):
        """
        Set an option's value if not already set.

        Args:
            package: Name of the package to assign a default to.
            option: Name of the option to assign a default to.
            value: Default value to assign.
        """
        self._profile_options.setdefault(package, {})
        if option in self._profile_options[package]:
            # TODO: Make some sort of warning visible
            return

        self._profile_options[package][option] = value


def _profile_order(packages: dict):
    """Yield the keys and values in a profile options dict in the order they should be written to the profile."""
    # Conan2 uses last-wins resolution, but most-specific-wins seems more reasonable.
    # Put the wildcards first so they can be overridden.
    if '*' in packages:
        yield '*', packages['*']

    # The rest are sorted for easy scanning.
    for key in sorted(packages.keys()):
        if key != '*':
            yield key, packages[key]
