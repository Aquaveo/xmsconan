"""The packager module."""
import copy
import itertools
import os
import platform
import subprocess
import tempfile

from xmsconan.package_tools.printer import Printer

configurations = {
    'windows': {
        'os': ['Windows'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['msvc'],
        'compiler.cppstd': ['17'],
        'compiler.version': ['192'],
        'compiler.runtime': ['dynamic', 'static'],
    },
    'linux': {
        'os': ['Linux'],
        'cppstd': ['17'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['gcc'],
        'compiler.version': ['12'],
    },
    'darwin': {  # macos
        'os': ['Macos'],
        'build_type': ['Release', 'Debug'],
        'arch': ['armv8'],
        'compiler': ['apple-clang'],
        'compiler.version': ['16'],
        'compiler.cppstd': ['gnu17'],
        'compiler.libcxx': ['libc++'],
    },
}


class XmsConanPackager(object):
    """The packager class."""

    def __init__(self, libary_name, conanfile_path='.'):
        """Initialize the packager."""
        self._library_name = libary_name
        self._conanfile_path = conanfile_path
        self._configurations = None
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

    def get_builder(self):
        """Get the builder for the current system_platform."""
        if self._builder is None:
            self._builder = self._get_builder()
        return self._builder

    def _get_builder(self):
        """Get the builder for the current system_platform."""
        builder = None
        return builder

    def generate_configurations(self, system_platform=None):
        """Generate the configurations for the build process."""
        # Get system_platform name

        if system_platform is None:
            system_platform = platform.system().lower()

        # Get the current system_platform configuration
        system_platform_configuration = configurations.get(system_platform)

        # Get the cartesian product of all the configurations
        keys = system_platform_configuration.keys()
        values = (system_platform_configuration[key] for key in keys)
        combinations = [dict(zip(keys, combination)) for combination in itertools.product(*values)]

        xms_version = os.getenv('XMS_VERSION', None)
        python_target_version = os.getenv('PYTHON_TARGET_VERSION', "3.12")
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
                    (combination['compiler'] != 'Visual Studio' or combination['compiler.runtime'] in ['MD', 'MDd']):
                if combination['compiler'] == 'Visual Studio' and int(combination['compiler.version']) <= 12:
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

    def run(self):
        """Run the build process."""
        self.printer.print_ascci_art()
        self.print_configuration_table(self.configurations)
        failing_configurations = []
        for i, combination in enumerate(self.configurations):
            self.printer.print_message('*-' * 40 + '\n')
            self.printer.print_message(f'Building configuration {i + 1} of {len(self.configurations)}')
            profile_path = self.create_build_profile(combination)
            self.printer.print_profile(profile_path)
            cmd = ['conan', 'create', self._conanfile_path, '--profile', profile_path]
            try:
                exit_code = subprocess.call(cmd)
                if exit_code == 0:
                    self.printer.print_message(f'Finished building configuration {i + 1} of {len(self.configurations)}')
                else:
                    self.printer.print_message(f'ERROR building configuration {i + 1} of {len(self.configurations)}')
                    failing_configurations.append(f'{i + 1}')
            except subprocess.CalledProcessError:
                self.printer.print_message(
                    f'ERROR running build of configuration {i + 1} of {len(self.configurations)}')
                failing_configurations.append(f'{i + 1}')
            self.printer.print_message('*-' * 40 + '\n')
        if len(failing_configurations) > 0:
            self.printer.print_message(
                f'One or more configurations failed to build. ({",".join(failing_configurations)})')
        else:
            self.printer.print_message('All configurations built successfully.')

    def upload(self, version):
        """Upload the packages to the server."""
        self.printer.print_message('Uploading packages to the server.')
        cmd = ['conan', 'upload', f'{self._library_name}/{version}*', '-r', 'aquaveo', '--confirm']
        try:
            subprocess.call(cmd)
            self.printer.print_message('Finished uploading')
        except subprocess.CalledProcessError:
            self.printer.print_message('ERROR uploading')
            return
        self.printer.print_message('*-' * 40 + '\n')
        self.printer.print_message('All packages uploaded successfully.')

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

            f.write('\n[buildenv]\n')
            for k, v in configuration['buildenv'].items():
                f.write(f'{k}={v}\n')

            # Use the temporary profile file as needed
            # For example, you can print the path to the temporary profile file
            print(f'Temporary profile created at: {temp_profile_path}')
            return temp_profile_path

    def print_configuration_table(self):
        """Print the configuration table."""
        headers = ["#", "cppstd", "runtime", "build_type", "compiler", "compiler.version", "arch",
                   "xmscore:wchar_t", "xmscore:pybind", "xmscore:testing"]
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
        for i, config in enumerate(self.configurations, start=1):
            wchar_t_option = config['options'].get('wchar_t', False)
            pybind_option = config['options'].get('pybind', False)
            testing_option = config['options'].get('testing', False)
            row = "| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} | {:^17} | {:^16} | {:^17} |".format(
                i,
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
