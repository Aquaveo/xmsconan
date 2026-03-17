"""
Conanfile base for the xmscore projects compatible with Conan 2.x.
"""
import glob
import os
import shutil
import sys

from conan import ConanFile
from conan.errors import ConanException
from conan.tools.cmake import CMake, cmake_layout, CMakeDeps, CMakeToolchain
from conan.tools.files import copy


def _has_uv():
    """Check if uv is available on the system."""
    return shutil.which("uv") is not None


def _pip_install_cmd(python_executable, *args):
    """Return a pip install command, using uv if available."""
    if _has_uv():
        return f'uv pip install --python "{python_executable}" {" ".join(args)}'
    return f'"{python_executable}" -m pip install {" ".join(args)}'


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
    testing_framework = "cxxtest"  # Options: "cxxtest" or "gtest"
    python_binding_type = "pybind11"  # Options: "pybind11" or "vtk_wrap"
    xms_dependency_options = {}  # Per-dependency option overrides: {"dep_name": {"pybind": False, "testing": False}}

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
            if self.testing_framework == "cxxtest":
                self.requires('cxxtest/4.4')
            elif self.testing_framework == "gtest":
                self.requires('gtest/1.17.0')
        if self.options.pybind and self.python_binding_type == "pybind11":
            self.requires("pybind11/3.0.1")

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
            dep_opts = self.xms_dependency_options.get(dep_name, {})
            self.options[dep_name].pybind = dep_opts.get('pybind', self.options.pybind)
            self.options[dep_name].testing = dep_opts.get('testing', self.options.testing)

    def layout(self):
        """The layout method."""
        cmake_layout(self)

    def generate(self):
        """The generate method for the conan class."""
        tc = CMakeToolchain(self)

        tc.variables["IS_PYTHON_BUILD"] = self.options.pybind
        tc.variables["BUILD_TESTING"] = self.options.testing
        tc.variables["XMS_TESTING_FRAMEWORK"] = self.testing_framework
        # tc.variables["XMS_TEST_PATH"] = "test_files"

        # Version Info
        tc.variables["XMS_VERSION"] = '{}'.format(self.version)
        tc.variables["PYTHON_TARGET_VERSION"] = self.buildenv.vars(self).get("PYTHON_TARGET_VERSION", "3.13")

        # Generate toolchain
        tc.generate()

        # Generate dependencies
        deps = CMakeDeps(self)
        if self.python_binding_type == "pybind11":
            deps.build_context_activated = ["pybind11"]
        deps.generate()

    def build(self):
        """The build method for the conan class."""
        cmake = CMake(self)

        variables = {}
        variables["IS_PYTHON_BUILD"] = self.options.pybind
        variables["BUILD_TESTING"] = self.options.testing
        variables["XMS_TESTING_FRAMEWORK"] = self.testing_framework
        # variables["XMS_TEST_PATH"] = "test_files"

        # Version Info
        variables["XMS_VERSION"] = '{}'.format(self.version)
        variables["PYTHON_TARGET_VERSION"] = self.buildenv.vars(self).get("PYTHON_TARGET_VERSION", "3.13")

        cmake.configure(variables=variables)
        cmake.build()
        cmake.install()

        # Run the tests if it is testing configuration.
        if self.options.testing:
            self.run_cxx_tests(cmake)

        # If this build is python, build the wheel then run tests.
        elif self.options.pybind:
            self._build_wheel()
            self.run_python_tests()

    def package(self):
        """The package method of the conan class."""
        cmake = CMake(self)
        cmake.install()

        copy(self, "license", src=os.path.join(self.source_folder), dst=os.path.join(self.package_folder, "licenses"),
             ignore_case=True, keep_path=False)

        # Copy the pre-built wheel into the package folder
        if self.options.pybind:
            copy(self, "*.whl", src=os.path.join(self.build_folder, "dist"),
                 dst=os.path.join(self.package_folder, "dist"))

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

    def _build_wheel(self):
        """Build a wheel from _package/ into build_folder/dist/."""
        package_src = os.path.join(self.package_folder, "_package")
        dist_dir = os.path.join(self.build_folder, "dist")
        os.makedirs(dist_dir, exist_ok=True)
        if _has_uv():
            self.run(f'uv build --wheel --no-build-logs --out-dir {dist_dir} {package_src}')
        else:
            self.run(f'"{sys.executable}" -m pip wheel . --no-deps --wheel-dir {dist_dir}', cwd=package_src)

    def _find_wheel(self):
        """Return the path to the built wheel in build_folder/dist/."""
        dist_dir = os.path.join(self.build_folder, "dist")
        wheels = glob.glob(os.path.join(dist_dir, "*.whl"))
        if not wheels:
            raise ConanException(f"No wheel found in {dist_dir}")
        return wheels[0]

    def run_python_tests(self):
        """Run Python tests in a virtual environment and optionally upload."""
        build_venv_dir = os.path.join(self.build_folder, "venv")
        tests_dest_dir = os.path.join(self.build_folder, "tests")

        if sys.platform == "win32":
            python_executable = os.path.join(build_venv_dir, "Scripts", "python")
        else:
            python_executable = os.path.join(build_venv_dir, "bin", "python")

        # Create a virtual environment
        if _has_uv():
            self.run(f'uv venv {build_venv_dir}')
        else:
            self.run(f'"{sys.executable}" -m venv {build_venv_dir}')

        # Install general dependencies
        general_dependencies = ["numpy", "wheel"]
        self.run(_pip_install_cmd(python_executable, *general_dependencies))

        # Install xms_dependencies one by one
        for dependency_spec in self.xms_dependencies:
            # Extract package name from version constraint (e.g., "xmscore/[>=7.0.0]" -> "xmscore")
            dependency_name = dependency_spec.split('/')[0]
            if dependency_name in self.dependencies.host:
                dependency = self.dependencies.host[dependency_name]
                if dependency.package_folder:
                    package_path = os.path.join(dependency.package_folder, "_package")
                    self.run(_pip_install_cmd(python_executable, package_path, "--no-deps"))

        # Install from the pre-built wheel
        wheel_path = self._find_wheel()
        self.run(_pip_install_cmd(python_executable, wheel_path))

        # Copy the tests folder into the build directory
        tests_src_dir = os.path.join(self.source_folder, "_package", "tests")
        if os.path.exists(tests_dest_dir):
            shutil.rmtree(tests_dest_dir)
        shutil.copytree(tests_src_dir, tests_dest_dir)

        # Run tests using the virtual environment's Python
        unittest_command = f"{python_executable} -m unittest discover -v -p \"*_pyt.py\" -s {tests_dest_dir}"
        self.run(unittest_command, cwd=self.build_folder)

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
        copy(self, 'xms_conan2_file.py', src=self.recipe_folder, dst=self.export_folder)

        for item in self.extra_exports:
            if os.path.isdir(item):
                copy(self, '*', src=os.path.join(self.recipe_folder, f'{item}'),
                     dst=os.path.join(self.export_folder, f'{item}'))
            else:
                copy(self, f'{item}', src=self.recipe_folder, dst=self.export_folder)
