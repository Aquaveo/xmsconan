"""The packager module."""
import concurrent.futures
import copy
import json
import os
import platform
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
)
from xmsconan.package_tools import matrix as build_matrix
from xmsconan.package_tools.matrix import config_label, configuration_matches, validate_filter_dict
# Re-exported. These lived in this module until the matrix moved to its own.
# The build.py template reads `packager.configurations`, so every generated
# build.py, current or older, depends on that spelling. The rest are the
# public names that moved with it, kept for any caller outside this
# repository; code inside it imports them from the matrix module.
from xmsconan.package_tools.matrix import (  # noqa: F401
    configurations,
    COVERAGE_PYBIND_BUILD_TYPE,
    COVERAGE_TESTING_BUILD_TYPES,
    emitted_buildenv_keys,
    filter_matches,
    FILTER_NESTED_KEYS,
    FILTER_OPTION_KEYS,
    FILTER_OPTION_VALUES,
    FILTER_SETTING_KEYS,
    FILTER_SETTING_VALUES,
    get_current_arch,
    is_instrumented_configuration,
    only_msvc_version,
    PYTHON_VERSION_RE,
    summarize_filter_matches,
)
from xmsconan.package_tools.printer import Printer


#: Suffix `_run_sharded_tests` appends to a label when the runner was never
#: built, so `run` can keep that fault out of the failed-shard count. The two
#: methods communicate through this spelling; it is not for display.
RUNNER_MISSING_SUFFIX = '-runner-missing'


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
#
# `_serialize_profile` refuses to write a name that is not here. That is the
# backstop, not the guard -- it fires only if a key reached a configuration in
# spite of the tests above -- and it refuses instead of filtering, so it stops
# the committed profile and the printed one together rather than quietly
# dropping an entry from one of them.
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
        self._python_versions = build_matrix.resolve_python_versions(python_versions)
        self._matrix = build_matrix.resolve_matrix(matrix)
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

    # The class's public defaults before the matrix moved to its own module;
    # kept as the module's own objects so an outside caller reading them here
    # keeps working. Reading only: resolution uses the module's values, so
    # overriding these on a subclass no longer changes any default.
    DEFAULT_PYTHON_VERSIONS = build_matrix.DEFAULT_PYTHON_VERSIONS
    DEFAULT_PYBIND_BUILD_TYPES = build_matrix.DEFAULT_PYBIND_BUILD_TYPES

    #: Keys a ``conan_profile_variants`` entry may carry.
    _VARIANT_KEYS = frozenset({'name', 'conf', 'platforms', 'kinds'})

    #: Values ``kinds`` may name; the return values of :meth:`configuration_kind`.
    _VARIANT_KINDS = frozenset({'library', 'python', 'testing'})

    @classmethod
    def resolve_matrix(cls, matrix: Optional[dict]) -> dict:
        """Validate the ``[matrix]`` table and fill in its defaults.

        A delegate to :func:`xmsconan.package_tools.matrix.resolve_matrix`,
        which documents the table and its failure modes. Kept for callers
        outside this repository that validated ``[matrix]`` through the
        class; code inside it calls the module function.
        """
        return build_matrix.resolve_matrix(matrix)

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

    @property
    def python_versions(self):
        """Get the list of python versions pybind builds will fan out across."""
        return list(self._python_versions)

    @property
    def library_name(self):
        """Get the library name."""
        return self._library_name

    # Not a redefinition of the `configurations` dict imported above: that is a
    # module-level re-export, and this is the instance's own matrix.
    @property
    def configurations(self):  # noqa: F811
        """Get the configurations for the build process."""
        return self._configurations

    def generate_configurations(self, system_platform=None):
        """Generate this packager's configurations for one platform, and keep them.

        A delegate to :func:`xmsconan.package_tools.matrix.generate_configurations`,
        which documents the fan-out. The result is what :attr:`configurations`
        returns until :meth:`filter_configurations` narrows it or this runs again.

        Args:
            system_platform: Key into the module-level ``configurations`` dict,
                or None to detect the running platform.

        Returns:
            The configurations.

        Raises:
            ValueError: When ``system_platform`` is not a key of
                ``configurations``.
        """
        self._configurations = build_matrix.generate_configurations(
            system_platform,
            matrix=self._matrix,
            python_versions=self._python_versions,
            coverage=self._coverage,
            artifacts_dir=self._artifacts_dir,
        )
        return self._configurations

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
        ``matrix.configuration_matches`` -- because ``python_version`` is set only on
        the pybind variants, so treating it as absent-matches-anything made a
        one-ABI filter select the whole matrix.
        """
        validate_filter_dict(filter_dict)
        if self.configurations is None:
            return
        self._configurations = [
            configuration for configuration in self.configurations
            if configuration_matches(configuration, filter_dict)
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
        self.printer.print_ascii_art()
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

        python_version = build_matrix.highest_python_version(self._python_versions)
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

        A thin writer over :meth:`_render_profile`, which holds the format and
        the allow-list check. The split exists so ``--check`` can compare a
        profile it has not written against the one on disk; both kinds of
        write still go through the one renderer, so the refusal below covers
        them together.

        Rendered before the file is opened, not into it: a refused profile
        must leave nothing behind, and ``open(path, 'w')`` truncates before
        the renderer gets to raise.
        """
        content = self._render_profile(configuration, path, skip_empty=skip_empty, conf=conf)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def _render_profile(self, configuration, path, skip_empty=False, conf=None):
        """Render one configuration as Conan profile text.

        Single serialization path shared by the ephemeral build profile and the
        profiles written into a repository by :meth:`write_profiles`. Every
        profile is public -- the committed one obviously, and the ephemeral one
        because :meth:`create_build_profile` prints it and conan echoes it
        under "Input profiles" -- so a ``[buildenv]`` name outside
        ``PUBLIC_BUILDENV_KEYS`` stops the write.

        It refuses rather than filters, which is the whole difference. A filter
        drops the offending entry and lets the build go on with a profile
        nobody was told had changed, and it guards whichever profile it sits
        in front of. Raising at the one path both *kinds* of profile go
        through covers them with one check, and stops the ephemeral one before
        conan can echo it. It is not atomic across a :meth:`write_profiles`
        run: the check is per file, so profiles serialized before the raise
        are already on disk. None of them holds the refused name -- the file
        that would have is the one that raised.
        The keys are still kept out of ``combination['buildenv']`` in
        ``generate_configurations``; this is the backstop for that, not a
        replacement for it.

        Args:
            configuration: One entry from :attr:`configurations`.
            path: Destination file path. Named in the refusal below, so the
                message says which profile was rejected even when nothing is
                being written.
            skip_empty: Drop buildenv entries whose value is None. Without this
                an unset variable serializes as the literal string ``None``,
                which Conan would faithfully export into the build.
            conf: Mapping written as a ``[conf]`` section. None omits the
                section entirely, preserving the ephemeral profile's shape.

        Returns:
            The profile text.

        Raises:
            ValueError: A ``[buildenv]`` name is not in
                ``PUBLIC_BUILDENV_KEYS``.
        """
        settings = {k: v for k, v in configuration.items() if k not in ['options', 'buildenv']}

        buildenv = configuration['buildenv']
        outside = sorted(set(buildenv) - PUBLIC_BUILDENV_KEYS)
        if outside:
            raise ValueError(
                f'Refusing to write {path}: [buildenv] names outside '
                f'PUBLIC_BUILDENV_KEYS: {outside}. Every profile xmsconan writes is '
                f'public -- the committed one, and the ephemeral one conan echoes '
                f'under "Input profiles" -- so a name that is not on the allow-list '
                f'must not reach a profile at all. Add it to PUBLIC_BUILDENV_KEYS if '
                f'it is safe to print, or keep it out of the configuration.'
            )

        lines = ['[settings]\n']
        for k, v in settings.items():
            lines.append(f'{k}={v}\n')

        lines.append('\n[options]\n')
        for k, v in configuration['options'].items():
            lines.append(f'&:{k}={v}\n')

        for dep_name, dep_opts in _profile_order(self._profile_options):
            for opt_name, opt_value in dep_opts.items():
                lines.append(f'{dep_name}/*:{opt_name}={opt_value}\n')

        lines.append('\n[buildenv]\n')
        for k, v in buildenv.items():
            if skip_empty and v is None:
                continue
            lines.append(f'{k}={v}\n')

        if conf:
            lines.append('\n[conf]\n')
            for k, v in conf.items():
                lines.append(f'{k}={v}\n')

        return ''.join(lines)

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

    def render_profiles(self, output_dir, system_platform=None):
        """Render every profile :meth:`write_profiles` would write, without writing.

        Same plan and same renderer as the write path, so ``--check`` cannot
        report a tree as up to date that a real run would change. It does not
        share :meth:`write_profiles`' loop on purpose: that one serializes
        each profile as it goes and is documented as not atomic, and folding
        the two together would quietly make a refusal leave nothing behind
        rather than leaving the profiles already written.

        Returns:
            Mapping of profile path under *output_dir* to its text, in plan order.
        """
        if self._configurations is None:
            self.generate_configurations(system_platform)

        rendered = {}
        for entry in self.plan_profiles(system_platform):
            path = os.path.join(output_dir, entry.filename)
            rendered[path] = self._render_profile(
                entry.configuration, path, skip_empty=True, conf=entry.conf,
            )
        return rendered

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
            # Through the printer like the rest of the packager's output: a
            # generated build.py never configures logging, so a log record
            # from here would be dropped. Not a warning either -- an option
            # the profile already sets is the developer's choice, and warning
            # about it on every build would teach them to ignore warnings.
            existing = self._profile_options[package][option]
            self.printer.print_message(
                f"{package}:{option} is already {existing!r} in the profile options",
                body=f"the default {value!r} was not applied",
            )
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
