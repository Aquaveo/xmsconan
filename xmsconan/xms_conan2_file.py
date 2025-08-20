"""
Conanfile base for the xmscore projects compatible with Conan 2.x.
"""
import os
import shutil
import sys

from conan import ConanFile
from conan.errors import ConanException
from conan.tools.cmake import CMake, cmake_layout, CMakeDeps, CMakeToolchain
from conan.tools.files import copy


class XmsConan2File(ConanFile):
    """
    XmsConan class used for defining the conan info.
    """
    license = "FreeBSD Software License"
    settings = "os", "compiler", "build_type", "arch"
    options = {
        "wchar_t": ["builtin", "typedef"],
        "pybind": [True, False],
        "testing": [True, False],
    }
    xms_dependencies = []
    extra_dependencies = []
    extra_exports = []
    extra_export_sources = []

    default_options = {
        'wchar_t': 'builtin',
        'pybind': False,
        'testing': False,
    }

    def requirements(self):
        """Requirements."""
        self.requires("boost/1.86.0")
        self.requires("zlib/1.3.1")
        if self.options.testing:
            self.requires('cxxtest/4.4')
        # Pybind if not clang
        if not self.settings.compiler == "clang" and self.options.pybind:
            self.requires("pybind11/2.13.6")

        for dependency in self.xms_dependencies:
            self.requires(dependency)

        for dependency in self.extra_dependencies:
            self.requires(dependency)

    def configure_options(self):
        """Configure the options for the conan class."""
        if self.settings.build_type != "Release":
            del self.options.pybind

    def configure(self):
        """The configure method."""
        # self.version = envvars.get('XMS_VERSION', '99.99.99')

        # Raise ConanExceptions for Unsupported Versions
        s_os = self.settings.os
        s_compiler = self.settings.compiler
        s_compiler_version = self.settings.compiler.version

        if s_compiler == "apple-clang" and s_os == 'Linux':
            raise ConanException("Clang on Linux is not supported.")

        if s_compiler == "gcc" and float(s_compiler_version.value) < 5.0:
            raise ConanException("GCC < 5.0 is not supported.")

        if s_compiler == "apple-clang" and s_os == 'Macos' and float(s_compiler_version.value) < 9.0:
            raise ConanException("Clang > 9.0 is required for Mac.")

        for dependency in self.xms_dependencies:
            dep_split = dependency.split('/')
            dep_name = dep_split[0]
            self.options[dep_name].pybind = self.options.pybind
            self.options[dep_name].testing = self.options.testing

    def layout(self):
        """The layout method."""
        cmake_layout(self)

    def generate(self):
        """The generate method for the conan class."""
        tc = CMakeToolchain(self)

        tc.variables["IS_PYTHON_BUILD"] = self.options.pybind
        tc.variables["BUILD_TESTING"] = self.options.testing
        # tc.variables["XMS_TEST_PATH"] = "test_files"

        # Version Info
        tc.variables["XMS_VERSION"] = '{}'.format(self.version)
        tc.variables["PYTHON_TARGET_VERSION"] = self.buildenv.vars(self).get("PYTHON_TARGET_VERSION", "3.10")

        # Generate toolchain
        tc.generate()

        # Generate dependencies
        deps = CMakeDeps(self)
        deps.generate()

    def build(self):
        """The build method for the conan class."""
        cmake = CMake(self)

        variables = {}
        variables["IS_PYTHON_BUILD"] = self.options.pybind
        variables["BUILD_TESTING"] = self.options.testing
        # variables["XMS_TEST_PATH"] = "test_files"

        # Version Info
        variables["XMS_VERSION"] = '{}'.format(self.version)
        variables["PYTHON_TARGET_VERSION"] = self.buildenv.vars(self).get("PYTHON_TARGET_VERSION", "3.10")

        cmake.configure(variables=variables)
        cmake.build()
        cmake.install()

        # Run the tests if it is testing configuration.
        if self.options.testing:
            self.run_cxx_tests(cmake)

        # If this build is python run the python tests.
        elif self.options.pybind:
            self.run_python_tests_and_upload()

    def package(self):
        """The package method of the conan class."""
        cmake = CMake(self)
        cmake.install()

        copy(self, "license", src=os.path.join(self.source_folder), dst=os.path.join(self.package_folder, "licenses"),
             ignore_case=True, keep_path=False)

    def package_info(self):
        """The package_info method of the conan class."""
        if self.options.pybind:
            self.runenv_info.append('PYTHONPATH', os.path.join(self.package_folder, "_package"))

        if self.settings.build_type == 'Debug':
            self.cpp_info.libs = [f'{self.name}lib_d']
        else:
            self.cpp_info.libs = [f'{self.name}lib']

        self.cpp_info.includedirs = [os.path.join(self.package_folder, 'include')]
        self.cpp_info.bindirs = [os.path.join(self.package_folder, 'bin')]

    def run_cxx_tests(self, cmake):
        """A function to run the cxx_tests."""
        try:
            cmake.test()
        except ConanException:
            raise
        finally:
            if os.path.isfile("TEST-cxxtest.xml"):
                with open("TEST-cxxtest.xml", "r") as f:
                    for line in f.readlines():
                        no_newline = line.strip('\n')
                        self.output.info(no_newline)

    def run_python_tests_and_upload(self):
        """Run Python tests in a virtual environment and optionally upload."""
        build_venv_dir = os.path.join(self.build_folder, "venv")
        tests_dest_dir = os.path.join(self.build_folder, "tests")  # Destination for tests

        if sys.platform == "win32":
            python_executable = os.path.join(build_venv_dir, "Scripts", "python")
            pip_executable = os.path.join(build_venv_dir, "Scripts", "pip")
        else:
            python_executable = os.path.join(build_venv_dir, "bin", "python")
            pip_executable = os.path.join(build_venv_dir, "bin", "pip")

        # Create a virtual environment
        self.run(f"{sys.executable} -m venv {build_venv_dir}")

        # Upgrade pip in the virtual environment
        self.run(f"{python_executable} -m pip install --upgrade pip")

        # Install general dependencies
        general_dependencies = ["numpy", "wheel"]
        self.run(f"{pip_executable} install {' '.join(general_dependencies)}")

        # Install xms_dependencies one by one
        for dependency_name in self.xms_dependencies:
            if dependency_name in self.dependencies.host:
                dependency = self.dependencies.host[dependency_name]
                if dependency.package_folder:
                    package_path = os.path.join(dependency.package_folder, "_package")
                    self.run(f"{pip_executable} install {package_path} --no-deps")

        # Install the current package into the virtual environment
        package_folder = os.path.join(self.package_folder, "_package")
        self.run(f"{pip_executable} install .", cwd=package_folder)

        # Copy the tests folder into the build directory
        tests_src_dir = os.path.join(self.source_folder, "_package", "tests")
        if os.path.exists(tests_dest_dir):
            shutil.rmtree(tests_dest_dir)  # Remove existing folder to avoid conflicts
        shutil.copytree(tests_src_dir, tests_dest_dir)

        # Run tests using the virtual environment's Python
        unittest_command = f"{python_executable} -m unittest discover -v -p \"*_pyt.py\" -s {tests_dest_dir}"
        self.run(unittest_command, cwd=self.build_folder)

        # Upload the package if it's a release
        # We are uploading to aquapi here instead of pypi because pypi doesn't accept
        # the type of package 'linux_x86_64 that we want to upload. They only accept
        # manylinux1 as the plat-tag
        is_release = self.buildenv.vars(self).get("RELEASE_PYTHON", 'False') == 'True'
        is_mac_os = self.settings.os == 'Macos'
        is_gcc_13 = self.settings.os == "Linux" and float(self.settings.compiler.version.value) == 13.0
        is_windows_md = (self.settings.os == "Windows" and str(self.settings.compiler.runtime) == "dynamic")
        if is_release and (is_mac_os or is_gcc_13 or is_windows_md):
            self.upload_python_package()

    def upload_python_package(self):
        """Upload the python package to AQUAPI_URL."""
        devpi_url = self.buildenv.vars(self).get("AQUAPI_URL", 'NO_URL')
        devpi_username = self.buildenv.vars(self).get("AQUAPI_USERNAME", 'NO_USERNAME')
        devpi_password = self.buildenv.vars(self).get("AQUAPI_PASSWORD", 'NO_PASSWORD')
        self.run('devpi use {}'.format(devpi_url))
        self.run('devpi login {} --password {}'.format(devpi_username, devpi_password))
        # Create platform-specific wheels with compiled extensions
        dist_dir = os.path.join(self.package_folder, "dist")
        if not os.path.exists(dist_dir):
            os.makedirs(dist_dir)

        package_dir = os.path.join(self.package_folder, "_package")

        # Use pip wheel which is better at detecting binary content and creating platform-specific wheels
        print('Creating wheel...')
        self.run(f'ls {package_dir}')
        print(f'pip wheel . --wheel-dir {dist_dir} --no-build-isolation --no-deps')
        self.run(f'pip wheel {package_dir} --wheel-dir {dist_dir} --no-build-isolation --no-deps')
        self.run(f'devpi upload --from-dir {dist_dir}', cwd=".")

    def export_sources(self):
        """Specify sources to be exported."""
        copy(self, '*', src=os.path.join(self.recipe_folder, f'{self.name}'),
             dst=os.path.join(self.export_sources_folder, f'{self.name}'))
        copy(self, '*', src=os.path.join(self.recipe_folder, '_package'),
             dst=os.path.join(self.export_sources_folder, '_package'))
        copy(self, 'CMakeLists.txt', src=self.recipe_folder, dst=self.export_sources_folder)

        for item in self.extra_export_sources:
            if os.path.isdir(item):
                copy(self, '*', src=os.path.join(self.recipe_folder, f'{item}'),
                     dst=os.path.join(self.export_sources_folder, f'{item}'))
            else:
                copy(self, f'{item}', src=self.recipe_folder, dst=self.export_sources_folder)

    def export(self):
        """Specify files to be exported."""
        self.output.info("Exporting files...")
        copy(self, 'LICENSE', src=self.recipe_folder, dst=self.export_folder)
        for item in self.extra_exports:
            if os.path.isdir(item):
                copy(self, '*', src=os.path.join(self.recipe_folder, f'{item}'),
                     dst=os.path.join(self.export_folder, f'{item}'))
            else:
                copy(self, f'{item}', src=self.recipe_folder, dst=self.export_folder)
