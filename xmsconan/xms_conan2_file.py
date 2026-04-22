"""
Conanfile base for the xmscore projects compatible with Conan 2.x.
"""
import glob
import json
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
    extra_dependency_options = {}  # Per-dependency option overrides for extra_dependencies: {"dep_name": {"opt": value}}

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

        # Disable boost stacktrace to avoid __cxa_allocate_exception symbol
        # conflict with -static-libstdc++ in pybind shared modules.
        self.options["boost"].without_stacktrace = True

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

        for dep_name, opts in self.extra_dependency_options.items():
            for opt_name, opt_value in opts.items():
                setattr(self.options[dep_name], opt_name, opt_value)

    def layout(self):
        """The layout method."""
        cmake_layout(self)

    def _get_python_cmake_hints(self):
        """Return CMake hint variables for FindPython3.

        Non-framework Python installations (e.g., uv, pyenv, python.org standalone)
        on macOS aren't discoverable by CMake's default framework search. This
        provides explicit paths so FindPython3 can locate Development.Module.
        """
        import sysconfig
        return {
            "Python3_EXECUTABLE": sys.executable.replace("\\", "/"),
            "Python3_INCLUDE_DIR": sysconfig.get_path('include').replace("\\", "/"),
            "Python3_FIND_FRAMEWORK": "NEVER",
        }

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

        if self.options.pybind:
            for key, value in self._get_python_cmake_hints().items():
                tc.variables[key] = value

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

        if self.options.pybind:
            variables.update(self._get_python_cmake_hints())

        cmake.configure(variables=variables)
        cmake.build()
        cmake.install()

        # Run the tests if it is testing configuration.
        if self.options.testing:
            try:
                self.run_cxx_tests(cmake)
            finally:
                self._save_test_artifacts()

        # If this build is python, build the wheel then run tests.
        elif self.options.pybind:
            self._build_wheel()
            try:
                self.run_python_tests()
            finally:
                self._save_test_artifacts()

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
        if os.environ.get("XMS_SKIP_CXX_TESTS"):
            self.output.info("XMS_SKIP_CXX_TESTS is set — skipping C++ test execution.")
            return
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

    def _save_test_artifacts(self):
        """Copy test artifacts to an external directory if configured.

        Reads XMS_TEST_ARTIFACTS_DIR and XMS_TEST_ARTIFACTS_LABEL from the
        build environment.  For testing builds, copies LastTest.log and the
        test_files/ directory.  For pybind builds, copies the _package/
        directory (module, tests, and baseline files).
        """
        env_vars = self.buildenv.vars(self)
        artifacts_dir = env_vars.get("XMS_TEST_ARTIFACTS_DIR")
        if not artifacts_dir:
            return

        label = env_vars.get("XMS_TEST_ARTIFACTS_LABEL", "unknown")
        dest = os.path.join(artifacts_dir, label)
        os.makedirs(dest, exist_ok=True)
        self.output.info(f"Saving test artifacts to {dest}")

        # CTest log
        last_test_log = os.path.join(
            self.build_folder, "Testing", "Temporary", "LastTest.log"
        )
        if os.path.isfile(last_test_log):
            shutil.copy2(last_test_log, os.path.join(dest, "LastTest.log"))
            self.output.info("  Copied LastTest.log")

        # test_files/ — testing builds only (lives in source_folder per CMakeLists.txt)
        if self.options.testing:
            test_files_src = os.path.join(self.source_folder, "test_files")
            if os.path.isdir(test_files_src):
                dest_tf = os.path.join(dest, "test_files")
                if os.path.exists(dest_tf):
                    shutil.rmtree(dest_tf)
                shutil.copytree(test_files_src, dest_tf)
                self.output.info("  Copied test_files/")

            # runner binary — check build root first (single-config generators
            # like Ninja), then Debug/ and Release/ (multi-config generators)
            runner_name = "runner.exe" if sys.platform == "win32" else "runner"
            runner_src = None
            search_dirs = [
                self.build_folder,
                os.path.join(self.build_folder, "Debug"),
                os.path.join(self.build_folder, "Release"),
            ]
            for d in search_dirs:
                candidate = os.path.join(d, runner_name)
                if os.path.isfile(candidate):
                    runner_src = candidate
                    break
            if runner_src:
                shutil.copy2(runner_src, os.path.join(dest, runner_name))
                self.output.info(f"  Copied {runner_name}")

            # test_metadata.json — records paths needed by the parallel test runner
            test_path = os.path.join(self.source_folder, "test_files") + "/"
            metadata = {
                "test_path": test_path,
                "build_folder": self.build_folder,
            }
            metadata_path = os.path.join(dest, "test_metadata.json")
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)
            self.output.info("  Wrote test_metadata.json")

        # _package/ — pybind builds only (contains module, tests, and baselines)
        if self.options.pybind:
            package_src = os.path.join(self.source_folder, "_package")
            if os.path.isdir(package_src):
                dest_pkg = os.path.join(dest, "_package")
                if os.path.exists(dest_pkg):
                    shutil.rmtree(dest_pkg)
                shutil.copytree(package_src, dest_pkg)
                self.output.info("  Copied _package/")

    def _build_wheel(self):
        """Build a wheel from _package/ into build_folder/dist/."""
        package_src = os.path.join(self.package_folder, "_package")
        dist_dir = os.path.join(self.build_folder, "dist")
        os.makedirs(dist_dir, exist_ok=True)

        # Force correct platform tag for macOS ARM builds.
        # Apple's universal Python framework causes wheels to be tagged
        # universal2, but we only build ARM binaries.
        old_host_platform = os.environ.get("_PYTHON_HOST_PLATFORM")
        if str(self.settings.os) == "Macos" and str(self.settings.arch) == "armv8":
            os.environ["_PYTHON_HOST_PLATFORM"] = "macosx-15.0-arm64"
        try:
            if _has_uv():
                self.run(f'uv build --wheel --no-build-logs --out-dir {dist_dir} {package_src}')
            else:
                self.run(f'"{sys.executable}" -m pip wheel . --no-deps --wheel-dir {dist_dir}', cwd=package_src)
        finally:
            if old_host_platform is None:
                os.environ.pop("_PYTHON_HOST_PLATFORM", None)
            else:
                os.environ["_PYTHON_HOST_PLATFORM"] = old_host_platform

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
        python_target_version = self.buildenv.vars(self).get("PYTHON_TARGET_VERSION", "3.13")

        if sys.platform == "win32":
            python_executable = os.path.join(build_venv_dir, "Scripts", "python.exe")
        else:
            python_executable = os.path.join(build_venv_dir, "bin", "python")

        # Create a virtual environment
        if _has_uv():
            self.run(f'uv venv --python {python_target_version} {build_venv_dir}')
        else:
            self.run(f'"{sys.executable}" -m venv {build_venv_dir}')

        # Install general dependencies
        general_dependencies = ["numpy", "wheel", "pytest"]
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
        pytest_ini = os.path.join(self.source_folder, "pytest.ini")
        pytest_command = f'"{python_executable}" -m pytest -c "{pytest_ini}" {tests_dest_dir} -v'
        self.run(pytest_command, cwd=self.build_folder)

    def export_sources(self):
        """Specify sources to be exported."""
        copy(self, '*', src=os.path.join(self.recipe_folder, f'{self.name}'),
             dst=os.path.join(self.export_sources_folder, f'{self.name}'))
        copy(self, '*', src=os.path.join(self.recipe_folder, '_package'),
             dst=os.path.join(self.export_sources_folder, '_package'))
        copy(self, 'CMakeLists.txt', src=self.recipe_folder, dst=self.export_sources_folder)
        copy(self, 'pytest.ini', src=self.recipe_folder, dst=self.export_sources_folder)

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
