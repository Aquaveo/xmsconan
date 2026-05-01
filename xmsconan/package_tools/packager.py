"""The packager module."""
import concurrent.futures
import copy
import itertools
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

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

    def __init__(self, library_name, conanfile_path='.', build_missing=False, artifacts_dir=None, test_shards=0, profile_options: Optional[dict]=None):
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
        """
        self._library_name = library_name
        self._conanfile_path = conanfile_path
        self._configurations = None
        self._build_missing = build_missing
        self._artifacts_dir = os.path.abspath(artifacts_dir) if artifacts_dir else None
        self._test_shards = test_shards
        self._profile_options = profile_options or {}
        self.printer = Printer()
        self._temp_dir = tempfile.TemporaryDirectory()
        self._temp_dir_path = self._temp_dir.name

    def __del__(self):
        """Cleanup the temporary directory."""
        self._temp_dir.cleanup()

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
        """
        # Get system_platform name
        auto_detected = system_platform is None
        if system_platform is None:
            system_platform = platform.system().lower()

        # Get the current system_platform configuration
        system_platform_configuration = configurations.get(system_platform).copy()

        # Override arch with detected architecture only when platform was auto-detected
        if auto_detected:
            system_platform_configuration['arch'] = [get_current_arch()]

        # Get the cartesian product of all the configurations
        keys = system_platform_configuration.keys()
        values = (system_platform_configuration[key] for key in keys)
        combinations = [dict(zip(keys, combination)) for combination in itertools.product(*values)]

        xms_version = os.getenv('XMS_VERSION', None)
        python_target_version = os.getenv('PYTHON_TARGET_VERSION', "3.13")
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
                'PYTHON_TARGET_VERSION': python_target_version,
                'CI_COMMIT_TAG': ci_commit_tag,
                'RELEASE_PYTHON': release_python,
                'AQUAPI_USERNAME': aquapi_username,
                'AQUAPI_PASSWORD': aquapi_password,
                'AQUAPI_URL': aquapi_url,
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

        wchar_t_updated_builds = []
        for combination in combinations:
            if combination['compiler'] == 'msvc':
                wchar_t_options = copy.deepcopy(combination)
                wchar_t_options['options'].update({
                    'wchar_t': 'typedef',
                })
                wchar_t_updated_builds.append(wchar_t_options)

        pybind_updated_builds = []
        for combination in combinations:
            if combination['build_type'] != 'Debug' and \
                    (combination['compiler'] != 'msvc' or combination['compiler.runtime'] in ['dynamic']):
                if combination['compiler'] == 'msvc' and int(combination['compiler.version']) <= 12:
                    continue
                pybind_options = copy.deepcopy(combination)
                pybind_options['options'].update({
                    'pybind': True,
                })
                pybind_updated_builds.append(pybind_options)

        testing_updated_builds = []
        for combination in combinations:
            testing_options = copy.deepcopy(combination)
            testing_options['options'].update({
                'testing': True,
            })
            testing_updated_builds.append(testing_options)

        combinations = combinations + wchar_t_updated_builds + pybind_updated_builds + testing_updated_builds

        self._configurations = combinations
        return combinations

    def filter_configurations(self, filter_dict):
        """
        Filter the configurations based on the filter_dict.

        Should only be called after `self.generate_configurations` has been called to initialize the configurations.

        The filter dict specifies values of things to keep. Example:
        {
          'options': { 'testing': True },  # only keep testing configurations
          'build_type': 'Debug'  # ... that are built in debug mode
        }
        """
        if self.configurations is None:
            return
        filtered_configurations = []
        for configuration in self.configurations:
            include_configuration = True
            for key, value in filter_dict.items():
                if key in ['options', 'buildenv']:
                    for option_key, option_value in value.items():
                        if option_key in configuration[key].keys() and configuration[key].get(option_key) != option_value:
                            include_configuration = False
                elif key in configuration.keys() and configuration.get(key) != value:
                    include_configuration = False
            if include_configuration:
                filtered_configurations.append(configuration)
        self._configurations = filtered_configurations

    def _config_label(self, combination):
        """Return a human-readable label for a build configuration."""
        parts = [combination.get('build_type', 'unknown')]
        opts = combination.get('options', {})
        if opts.get('testing'):
            parts.append('testing')
        elif opts.get('pybind'):
            parts.append('pybind')
        if opts.get('wchar_t') == 'typedef':
            parts.append('wchar_typedef')
        return '-'.join(parts)

    def run(self):
        """Run the build process."""
        self.printer.print_ascci_art()
        self.print_configuration_table()
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
                subprocess.run(cmd, check=True, env=env)
                self.printer.print_message(f'Finished building configuration {i + 1} of {len(self.configurations)}')
                if shard_this and self._artifacts_dir:
                    label = self._config_label(combination)
                    sharded_test_runs.append(label)
            except subprocess.CalledProcessError:
                self.printer.print_message(
                    f'ERROR building configuration {i + 1} of {len(self.configurations)}')
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
                    self.printer.print_message(
                        f'  Shard {shard_index + 1}/{self._test_shards} ERROR: {exc}')
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

    def upload(self, version):
        """Upload the packages to the server."""
        self.printer.print_message('Uploading packages to the server.')
        cmd = ['conan', 'upload', f'{self._library_name}/{version}*', '-r', 'aquaveo', '--confirm']
        try:
            subprocess.run(cmd, check=True)
            self.printer.print_message('Finished uploading')
        except subprocess.CalledProcessError:
            self.printer.print_message('ERROR uploading')
            return
        self.printer.print_message('*-' * 40 + '\n')
        self.printer.print_message('All packages uploaded successfully.')

    def extract_wheel(self, output_dir, version='*'):
        """Extract the pre-built wheel from the pybind Conan package.

        Args:
            output_dir: Directory to copy the .whl file into.
            version: Package version (default '*' matches any).

        Returns:
            True if a wheel was extracted, False otherwise.
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
        # Collect all pybind candidates with their revision timestamps
        # so we can pick the most recently built one.
        candidates = []
        for exact_ref, cache in data.get('Local Cache', {}).items():
            for rev in cache.get('revisions', {}).values():
                ts = rev.get('timestamp', 0)
                for pid, pinfo in rev.get('packages', {}).items():
                    if pinfo.get('info', {}).get('options', {}).get('pybind') == 'True':
                        candidates.append((ts, exact_ref, pid))
        candidates.sort(reverse=True)  # newest first

        for _ts, exact_ref, pid in candidates:
            path_result = subprocess.run(
                ['conan', 'cache', 'path', f'{exact_ref}:{pid}'],
                capture_output=True, text=True
            )
            pkg_dir = path_result.stdout.strip()
            dist_dir = os.path.join(pkg_dir, 'dist')
            if not os.path.isdir(dist_dir):
                continue
            os.makedirs(output_dir, exist_ok=True)
            for fname in os.listdir(dist_dir):
                if fname.endswith('.whl'):
                    shutil.copy2(os.path.join(dist_dir, fname), output_dir)
                    self.printer.print_message(f'Extracted {fname} to {output_dir}')
            return True

        self.printer.print_message('No pybind package found to extract.')
        return False

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
        portable manylinux wheel. Requires Docker.

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

        self.printer.print_message(f'Repairing wheel with auditwheel in {image}')
        cmd = [
            'docker', 'run', '--rm',
            '-v', f'{abs_wheel_dir}:/wheels',
            image,
            'bash', '-c',
            'export PATH="/opt/python/cp313-cp313/bin:$PATH" && '
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
            # Nobody remembers for sure why this is here. Someone thought it was an obscure bug that might have been
            # exclusive to an old Docker image. It may be unnecessary now.
            if configuration.get('os') == 'Linux' and configuration['options'].get('pybind'):
                f.write('*:shared=False\n')
            # Conan2 seems to apply option values in a last-wins order, so the hard-coded default needs to come first
            # or nobody will ever be able to override it with their build.toml.
            for dep_name, dep_opts in self._profile_options.items():
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
                   f"{self._library_name}:testing"]
        table = []

        # Create the header row
        header_row = "| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} |" \
                     " {:^17} | {:^16} | {:^17} |".format(*headers)
        separator = "+-----+----------+----------+--------------+----------------+--------------------+--------+" \
                    "-------------------+------------------+-------------------+"

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
            row = "| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} | {:^17} | {:^16} | {:^17} |".format(
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
            )
            table.append(row)
            table.append(separator)

        # Print the table
        print('\n')
        for line in table:
            print(line)
