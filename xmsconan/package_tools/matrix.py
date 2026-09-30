"""The build matrix: the configurations each platform builds, and the filters that narrow them.

Split out of :mod:`xmsconan.package_tools.packager`, which still re-exports
the public names that were module-level there. The ``build.py`` template
reads ``packager.configurations``, so every generated ``build.py``, current
or older, depends on that spelling.

Nothing here runs Conan or writes a file. Every function is a query over
:data:`configurations`, the ``[matrix]`` and ``[filter]`` tables and a few
reads of the running process, which is what lets the CI generators ask what
a build *would* produce without constructing a packager. Those reads are:

- ``PYTHON_TARGET_VERSION``, which :func:`resolve_python_versions` falls
  back to;
- ``XMS_VERSION``, ``CI_COMMIT_TAG`` and ``RELEASE_PYTHON``, which
  :func:`generate_configurations` copies into each configuration's
  ``[buildenv]``, and ``GITHUB_REF_TYPE``, which decides ``RELEASE_PYTHON``;
- the host's OS and architecture, when no platform is named;
- the working directory, which the filter queries resolve ``artifacts``
  against.
"""
import copy
import functools
import itertools
import os
import platform
import re
from typing import Optional

from xmsconan.constants import MSVC_VS2019_VERSION, version_sort_key


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


def only_msvc_version(platform_key: str) -> str:
    """Return the single ``compiler.version`` the named Windows matrix pins.

    A query over the matrix above, not a literal written anywhere else.
    ``xmsconan job deploy`` restricts a Windows publish to this value, and a
    literal that fell behind a toolchain bump would not fail loudly: ``conan
    upload -p compiler.version=194`` after a move to 195 matches nothing, and
    the job goes green having published no binaries at all. The generated CI
    file used to carry that literal per platform; it renders none now, which
    is what the reading has to stay here to keep true.

    :func:`~xmsconan.job_tools.build.export_package_query` answers the same
    question for a *build*, from the configurations that build actually
    produced rather than from the matrix key -- a build's job is to restrict
    the save to what it compiled, and the two can only agree by construction
    if neither writes the number down.

    Args:
        platform_key: Key into :data:`configurations`.

    Returns:
        The compiler version as a string.

    Raises:
        ValueError: When *platform_key* is not a key, does not name an msvc
            matrix, or pins more or fewer than one version -- each of which
            makes "the version this job publishes" ambiguous or wrong.
    """
    if platform_key not in configurations:
        raise ValueError(
            f"no {platform_key!r} in the matrix; it holds "
            f"{', '.join(sorted(configurations))}."
        )
    row = configurations[platform_key]
    if row["compiler"] != ["msvc"]:
        # The failure this function exists to prevent, one level up: a
        # non-Windows key answers with its own compiler's version, and
        # `conan upload -p compiler.version=13` on a Windows publish matches
        # nothing and exits 0 having published nothing.
        raise ValueError(
            f"platform {platform_key!r} builds with {'/'.join(row['compiler'])}, not msvc; "
            f"its compiler.version names no toolchain a Windows publish can restrict to."
        )
    versions = row["compiler.version"]
    if len(versions) != 1:
        raise ValueError(
            f"platform {platform_key!r} pins {len(versions)} compiler.version values "
            f"({', '.join(versions)}); a publish from this platform restricts to exactly "
            f"one, so this needs a decision rather than a guess."
        )
    return versions[0]


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


def configuration_matches(configuration, filter_dict):
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
    passing None reaches ``resolve_python_versions``, which falls back to
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
    return generate_configurations(
        platform_name,
        matrix=matrix,
        python_versions=list(python_versions) if python_versions else list(DEFAULT_PYTHON_VERSIONS),
        coverage=coverage,
        artifacts_dir=os.path.abspath('artifacts'),
    )


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
    pass them to ``xmsconan job test --label``. Any second implementation of
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
            Defaults to :data:`DEFAULT_PYTHON_VERSIONS`, deliberately not
            ``PYTHON_TARGET_VERSION``.
        matrix: The ``[matrix]`` table, so the filter is checked against the
            configurations this library actually produces.

    Returns:
        The surviving configurations, in matrix order.
    """
    return [
        configuration
        for configuration in _reference_matrix(platform_name, python_versions, matrix=matrix)
        if configuration_matches(configuration, filter_dict)
    ]


def summarize_filter_matches(filter_dict, python_versions=None, matrix=None):
    """Report what a filter would keep on each platform.

    Lets a generator answer "does this filter select anything at all?" without
    waiting for a build to discover it.

    Args:
        filter_dict: A filter already through ``validate_filter_dict``.
        python_versions: Python versions the pybind fan-out should assume,
            e.g. ``[ci].python_versions`` from ``build.toml``. Defaults to
            :data:`DEFAULT_PYTHON_VERSIONS`, deliberately not
            ``PYTHON_TARGET_VERSION``.
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


#: Python versions a pybind configuration fans out across when neither the
#: caller nor ``PYTHON_TARGET_VERSION`` names any.
DEFAULT_PYTHON_VERSIONS = ["3.13"]

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


def resolve_matrix(matrix: Optional[dict]) -> dict:
    """Validate the ``[matrix]`` table and fill in its defaults.

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

    accepted_keys = set(_MATRIX_VALUES) | set(_MATRIX_FLAGS)
    unknown = sorted(set(matrix) - accepted_keys)
    if unknown:
        raise ValueError(
            f'matrix has unknown key(s) {", ".join(unknown)}. '
            f'Accepted keys: {", ".join(sorted(accepted_keys))}.'
        )

    resolved = {}
    for key, default in _MATRIX_FLAGS.items():
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
    for key, accepted in _MATRIX_VALUES.items():
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
        resolved['pybind_build_types'] = DEFAULT_PYBIND_BUILD_TYPES
    return resolved


def resolve_python_versions(python_versions: Optional[list[str]]):
    """Resolve a python_versions list from the caller's value or the environment.

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
                if not isinstance(entry, str) or not PYTHON_VERSION_RE.match(entry):
                    raise ValueError(
                        f'python_versions entries must be "X.Y" strings, got {entry!r}'
                    )
                cleaned.append(entry)
            return cleaned
    env_version = os.getenv('PYTHON_TARGET_VERSION')
    if env_version:
        if not PYTHON_VERSION_RE.match(env_version):
            raise ValueError(
                f'PYTHON_TARGET_VERSION must be an "X.Y" string, got {env_version!r}'
            )
        return [env_version]
    return list(DEFAULT_PYTHON_VERSIONS)


def highest_python_version(versions):
    """Return the version string with the largest (major, minor) tuple."""
    return max(versions, key=version_sort_key)


def generate_configurations(system_platform=None, *, python_versions, matrix=None, coverage=False,
                            artifacts_dir=None):
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
        python_versions: The versions the pybind configurations fan out
            across, raw or as :func:`resolve_python_versions` returns them.
            Required, so a caller never reaches the environment by leaving it
            out: None or an empty list falls back to
            ``PYTHON_TARGET_VERSION``, then to :data:`DEFAULT_PYTHON_VERSIONS`.
        matrix: The ``[matrix]`` table, raw or as :func:`resolve_matrix`
            returns it; None is the full fan-out.
        coverage: Whether to set the recipe's ``coverage`` option on the
            configurations :func:`is_instrumented_configuration` selects.
            Never read from ``XMS_COVERAGE`` here; the packager's
            constructor does that.
        artifacts_dir: An absolute directory exported as
            ``XMS_TEST_ARTIFACTS_DIR`` in every configuration's
            ``[buildenv]``, or None to leave the name out.

    Returns:
        The configurations.

    Raises:
        ValueError: When ``python_versions`` or ``matrix`` fails its
            resolver, or when ``system_platform`` is not a key of
            ``configurations``. The platform is user-facing input (a CLI
            flag), so that message names the unknown key and lists the
            valid ones instead of failing with a bare ``AttributeError`` on
            None.
    """
    # Both resolvers return resolved input unchanged, so a caller that has
    # already resolved (the packager's constructor) pays only a re-check.
    # Python versions first, then the matrix: the order the constructor
    # validates them in, so a build.toml wrong in both reports the same
    # error whichever path reads it.
    python_versions = resolve_python_versions(python_versions)
    matrix = resolve_matrix(matrix)

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
    runtimes = matrix['compiler_runtime']
    wheel_only = matrix['wheel_only']
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
    default_python_version = highest_python_version(python_versions)
    ci_commit_tag = os.environ.get('CI_COMMIT_TAG', 'False')  # Gitlab
    release_python = os.getenv('RELEASE_PYTHON', 'False')

    # A tag pipeline on either host is a release. GitHub Actions reports
    # its ref through GITHUB_REF_TYPE; the generated workflow used to
    # translate that into RELEASE_PYTHON with a third-party action.
    if ci_commit_tag != 'False' or os.environ.get('GITHUB_REF_TYPE') == 'tag':
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
        if artifacts_dir:
            combination['buildenv']['XMS_TEST_ARTIFACTS_DIR'] = artifacts_dir

        # Set macOS deployment target for consistent wheel builds
        if combination.get('os') == 'Macos':
            combination['buildenv']['MACOSX_DEPLOYMENT_TARGET'] = '15.0'
            # Force correct platform tag for ARM-only wheels to prevent
            # universal2 tags from Apple's universal Python framework
            if combination.get('arch') == 'armv8':
                combination['buildenv']['_PYTHON_HOST_PLATFORM'] = 'macosx-15.0-arm64'

    pybind_updated_builds = _pybind_variants(combinations, matrix, python_versions)
    testing_updated_builds = _testing_variants(combinations)
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
        wchar_t_updated_builds = _wchar_t_variants(combinations)
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
    if coverage:
        for combination in combinations:
            if is_instrumented_configuration(combination):
                combination['options']['coverage'] = True

    return combinations


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


def _pybind_build_types(matrix) -> set:
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

    Args:
        matrix: The ``[matrix]`` table as :func:`resolve_matrix` returns it.

    Returns:
        The build-type names, as a set.
    """
    return set(matrix['pybind_build_types'])


def _pybind_variants(combinations, matrix, python_versions):
    """Return the pybind copies of ``combinations``, one per python version.

    The companion testing-only Debug build (emitted unconditionally by
    ``_testing_variants``) handles C++ coverage. On msvc, only the
    dynamic-runtime configurations get a pybind variant; every other
    toolchain fans out from all of them.

    Args:
        combinations: The base configurations to derive from.
        matrix: The ``[matrix]`` table as :func:`resolve_matrix` returns it.
        python_versions: The versions to fan out across.

    Returns:
        A new list of deep-copied configurations, ``len(python_versions)``
        per eligible base configuration.
    """
    build_types = _pybind_build_types(matrix)
    variants = []
    for combination in combinations:
        if combination['build_type'] in build_types and \
                (combination['compiler'] != 'msvc' or combination['compiler.runtime'] in ['dynamic']):
            for py_version in python_versions:
                pybind_options = copy.deepcopy(combination)
                pybind_options['options'].update({
                    'pybind': True,
                    'python_version': py_version,
                })
                pybind_options['buildenv']['PYTHON_TARGET_VERSION'] = py_version
                variants.append(pybind_options)
    return variants


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
