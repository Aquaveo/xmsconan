"""The packager module."""
import concurrent.futures
import copy
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
from typing import Any, Optional

from xmsconan.constants import DEFAULT_REMOTE_NAME, MSVC_VS2019_VERSION
from xmsconan.package_tools.printer import Printer


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
    # Visual Studio 2019 (msvc 192). GitHub retired the `windows-2019`
    # runner image, so this matrix is built manually on a developer
    # workstation and published to the `aquaveo-vs2019` remote rather than
    # to `aquaveo` (see XmsConanPackager.upload's `remote` argument).
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


class XmsConanPackager(object):
    """The packager class."""

    SHARD_TIMEOUT = 600  # seconds per shard (10 minutes)

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
                Each dep/opt pair is emitted as a `pkg/*:opt=value` line in the
                profile's [options] section.
            python_versions: Python versions to fan out pybind builds across
                (e.g. ["3.10", "3.13"]). When None, falls back to the
                ``PYTHON_TARGET_VERSION`` environment variable (single value)
                or to ``DEFAULT_PYTHON_VERSIONS``. Each pybind variant is
                duplicated per version with the matching ``python_version``
                Conan option and ``PYTHON_TARGET_VERSION`` buildenv set.
            coverage: If True, allow Debug+pybind combinations in
                ``generate_configurations`` so ``xmsconan_coverage`` can
                instrument the pybind variant for Python coverage. Also
                exports ``XMS_COVERAGE=1`` into each profile's ``[buildenv]``
                so CMake adds ``--coverage``. The testing-only Debug variant
                that drives C++ coverage is always emitted (it does not
                require this flag). When None, defaults to ``True`` iff
                ``XMS_COVERAGE=1`` is set in the environment.
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
        self._profile_options = profile_options or {}
        self._python_versions = self._resolve_python_versions(python_versions)
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
    _PYTHON_VERSION_RE = re.compile(r'^\d+\.\d+$')

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
        return max(versions, key=lambda v: tuple(int(p) for p in v.split('.')))

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
        aquapi_username = os.getenv('AQUAPI_USERNAME', None)
        aquapi_password = os.getenv('AQUAPI_PASSWORD', None)
        aquapi_url = os.getenv('AQUAPI_URL', None)

        if ci_commit_tag != 'False':
            release_python = 'True'

        for combination in combinations:
            combination['options'] = {
                'wchar_t': 'builtin',
                'pybind': False,
                'testing': False,
            }
            combination['buildenv'] = {
                'XMS_VERSION': xms_version,
                'PYTHON_TARGET_VERSION': default_python_version,
                'CI_COMMIT_TAG': ci_commit_tag,
                'RELEASE_PYTHON': release_python,
                'AQUAPI_USERNAME': aquapi_username,
                'AQUAPI_PASSWORD': aquapi_password,
                'AQUAPI_URL': aquapi_url,
            }
            if self._artifacts_dir:
                combination['buildenv']['XMS_TEST_ARTIFACTS_DIR'] = self._artifacts_dir

            # XMS_COVERAGE has to ride into the profile's [buildenv] so the
            # conan activation script exports it for the CMake child process.
            # Without this, the recipe's Python-side `configure()` still sees
            # XMS_COVERAGE (it inherits the parent env), but CMake's
            # `if (DEFINED ENV{XMS_COVERAGE})` guard runs in an activated
            # buildenv subshell that only sees [buildenv] vars — so
            # `--coverage` is never added, no .gcno files are produced, and
            # gcovr reports 0% (see issue #69).
            if self._coverage:
                combination['buildenv']['XMS_COVERAGE'] = '1'

            # Set macOS deployment target for consistent wheel builds
            if combination.get('os') == 'Macos':
                combination['buildenv']['MACOSX_DEPLOYMENT_TARGET'] = '15.0'
                # Force correct platform tag for ARM-only wheels to prevent
                # universal2 tags from Apple's universal Python framework
                if combination.get('arch') == 'armv8':
                    combination['buildenv']['_PYTHON_HOST_PLATFORM'] = 'macosx-15.0-arm64'

        wchar_t_updated_builds = self._wchar_t_variants(combinations)
        pybind_updated_builds = self._pybind_variants(combinations)
        testing_updated_builds = self._testing_variants(combinations)
        combinations = combinations + wchar_t_updated_builds + pybind_updated_builds + testing_updated_builds

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

    def _pybind_variants(self, combinations):
        """Return the pybind copies of ``combinations``, one per python version.

        Debug+pybind is normally redundant (the Release wheel is what ships),
        but xmsconan_coverage needs a Debug+pybind build to instrument the
        Python-reachable C++ surface — so the Debug gate relaxes when coverage
        is enabled. The companion testing-only Debug build (emitted
        unconditionally by ``_testing_variants``) handles C++ coverage. On msvc,
        only the dynamic-runtime configurations get a pybind variant; every
        other toolchain fans out from all of them.

        Args:
            combinations: The base configurations to derive from.

        Returns:
            A new list of deep-copied configurations, ``len(python_versions)``
            per eligible base configuration.
        """
        variants = []
        for combination in combinations:
            allow_debug = combination['build_type'] != 'Debug' or self._coverage
            if allow_debug and \
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

        Raises ``ValueError`` when a top-level key is neither a known
        configuration setting nor one of ``options``/``buildenv`` — the prior
        behavior silently dropped such keys, which is how flat ``pybind``/
        ``testing`` filters slipped past unnoticed (see issue #62).
        """
        if self.configurations is None:
            return
        if self.configurations:
            sample_keys = set(self.configurations[0].keys())
            for key in filter_dict.keys():
                if key in ('options', 'buildenv'):
                    continue
                if key not in sample_keys:
                    raise ValueError(
                        f"Unknown filter key {key!r}: must be a top-level configuration "
                        f"setting or 'options'/'buildenv'. Did you mean "
                        f"{{'options': {{'{key}': ...}}}}?"
                    )
        filtered_configurations = []
        for configuration in self.configurations:
            include_configuration = True
            for key, value in filter_dict.items():
                if key in ['options', 'buildenv']:
                    for option_key, option_value in value.items():
                        if option_key in configuration[key] and configuration[key].get(option_key) != option_value:
                            include_configuration = False
                elif configuration.get(key) != value:
                    include_configuration = False
            if include_configuration:
                filtered_configurations.append(configuration)
        self._configurations = filtered_configurations

    def _config_label(self, combination):
        """Return a human-readable label for a build configuration.

        The label has to be *unique across the generated matrix*: it names the
        per-configuration log file (``run(log_dir=...)``) and the test-artifact
        directory (``XMS_TEST_ARTIFACTS_LABEL``), both of which are overwritten
        when two configurations share a label. ``compiler.runtime`` is
        therefore part of the label — the msvc matrices fan out over
        ``dynamic``/``static``, so without it 14 msvc configurations collapse
        onto 8 labels and each static build destroys the dynamic build's log.
        Toolchains that don't set ``compiler.runtime`` (gcc, apple-clang) are
        unaffected and keep their shorter labels.
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
        shard_failure_count = 0
        for label in sharded_test_runs:
            shard_failures = self._run_sharded_tests(label)
            shard_failure_count += len(shard_failures)

        total_failures = len(failing_configurations) + shard_failure_count
        if total_failures > 0:
            if failing_configurations:
                self.printer.print_message('The following configurations failed to build:')
                self.print_configuration_table(failing_configurations)
            if shard_failure_count:
                self.printer.print_message(f'{shard_failure_count} test shard(s) failed.')
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
            List of failed shard indices, or empty list if all passed.
        """
        artifact_dir = os.path.join(self._artifacts_dir, label)
        runner_name = "runner.exe" if sys.platform == "win32" else "runner"
        runner_path = os.path.join(artifact_dir, runner_name)
        if not os.path.isfile(runner_path):
            self.printer.print_message(f'WARNING: Runner not found at {runner_path}, skipping sharded tests')
            return []

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
            self.printer.print_message(f'No packages found for {ref}')
            return False

        data = json.loads(result.stdout)
        # Collect all pybind candidates with their revision timestamps so we
        # can pick the most recent one per python_version.
        candidates = []
        for exact_ref, cache in data.get('Local Cache', {}).items():
            for rev in cache.get('revisions', {}).values():
                ts = rev.get('timestamp', 0)
                for pid, pinfo in rev.get('packages', {}).items():
                    options = pinfo.get('info', {}).get('options', {})
                    if options.get('pybind') == 'True':
                        py_version = options.get('python_version', '')
                        candidates.append((ts, py_version, exact_ref, pid))
        candidates.sort(reverse=True)  # newest first

        seen_python_versions = set()
        extracted = False
        for _ts, py_version, exact_ref, pid in candidates:
            if py_version in seen_python_versions:
                continue
            path_result = subprocess.run(
                ['conan', 'cache', 'path', f'{exact_ref}:{pid}'],
                capture_output=True, text=True
            )
            pkg_dir = path_result.stdout.strip()
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

        Args:
            output_dir: Directory to copy shared libraries into.
        """
        result = subprocess.run(
            ['conan', 'config', 'home'], capture_output=True, text=True
        )
        conan_home = result.stdout.strip()
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

    def create_build_profile(self, configuration):
        """Create a temporary build profile."""
        settings = {k: v for k, v in configuration.items() if k not in ['options', 'buildenv']}

        # Create a temporary directory
        temp_profile_path = os.path.join(self._temp_dir_path, 'temp_profile')

        # Write the profile to the temporary file
        with open(temp_profile_path, 'w') as f:
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
                f.write(f'{k}={v}\n')

            # Use the temporary profile file as needed
            # For example, you can print the path to the temporary profile file
            print(f'Temporary profile created at: {temp_profile_path}')
            return temp_profile_path

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
