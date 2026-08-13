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
        "python_version": ["3.10", "3.13"],
    }
    xms_dependencies = []
    extra_dependencies = []
    extra_exports = []
    extra_export_sources = []
    testing_framework = "cxxtest"  # Options: "cxxtest" or "gtest"
    python_binding_type = "pybind11"  # Options: "pybind11" or "vtk_wrap"
    # Submodule under xms.<...> (e.g. "core"). Set by the generated subclass; required
    # when XMS_COVERAGE=1 so pytest-cov scopes to xms.<python_namespaced_dir>.
    python_namespaced_dir = None
    xms_dependency_options = {}  # Per-dependency option overrides: {"dep_name": {"pybind": False, "testing": False}}

    # Third-party (non-XMS) requirements used by EVERY toolchain except msvc 192
    # — today that is gcc 13, apple-clang 17 and msvc 194 (VS2022), and any
    # future compiler lands here by default.  This is not the whole third-party
    # stack: ``requirements()`` additionally adds the testing framework when
    # ``testing`` is on and pybind11 when ``pybind`` is on, for this stack and
    # the VS2019 one alike.
    #
    # To change it in a subclass, REPLACE the whole list
    # (``default_requirements = [...]``).  Do not mutate it in place — it is a
    # class-level list shared by every recipe loaded in the process, so a
    # subclass-scope ``.append(...)`` also changes it for the base class and for
    # every sibling recipe.  Note that ``vs2019_requirements`` below is a
    # separate list that REPLACES this one on msvc 192 rather than extending it,
    # so a dependency added here must be added there too if VS2019 needs it.
    default_requirements = ["boost/1.86.0", "zlib/1.3.1"]

    # Third-party requirements for msvc 192 (VS2019), used INSTEAD OF
    # ``default_requirements`` — not in addition to it.  VS2019 packages are
    # built by hand against the LEGACY stack that the desktop products
    # (GMS/SMS/WMS) link against, which is why the VERSIONS differ from
    # ``default_requirements`` even though the package set is the same:
    #
    # * ``boost`` is the Aquaveo 1.74.0.3 build rather than conan-center
    #   1.86.0.
    # * ``zlib`` is 1.2.11 rather than 1.3.1.  The ``aquaveo-vs2019`` remote
    #   carries 1.2.11 with msvc 192 binaries; 1.3.1 exists only on
    #   ``aquaveo-stable`` and only with msvc 194 binaries, so naming it here
    #   would force a from-source build or fail outright.
    #
    # zlib is REQUIRED on this stack, not optional: the generated
    # ``CMakeLists.txt`` calls ``find_package(ZLIB REQUIRED)`` unconditionally
    # and sources such as ``xmscore/dataio/daStreamIo.cpp`` ``#include
    # <zlib.h>``.  It was once omitted here on the theory that the Conan 1
    # recipe never required it; that recipe only got away with it because its
    # CMake integration exposed every dependency's include directories
    # globally.  Under Conan 2's CMakeDeps a package must declare what it
    # uses — boost happening to require zlib transitively is an accident to
    # not depend on.  Replace the whole list in a subclass, with the same
    # no-in-place-mutation caveat as ``default_requirements``.
    vs2019_requirements = ["boost/1.74.0.3", "zlib/1.2.11"]

    # Per-library replacements for ``xms_dependencies`` on VS2019 builds:
    # {"xmscore": "xmscore/[>=6.0.1 <7.0.0]"}.  An entry replaces the matching
    # reference in ``xms_dependencies`` — matched on the package name before the
    # first ``/`` — when (and only when) the build is msvc 192.  Every other
    # toolchain ignores this dict entirely.  It exists because the legacy desktop
    # products pin xmscore 6.x while the Conan 2 line has moved on to 7.x.
    # Generated recipes populate it from the ``[vs2019_dependency_overrides]``
    # table in ``build.toml``; hand-written recipes can set it directly.
    #
    # CONSTRAINT: an override may change only the VERSION or version range —
    # the package name on both sides of the entry must be identical.
    # ``configure()`` and ``run_python_tests()`` iterate the UNRESOLVED
    # ``xms_dependencies``, so a rename would set options on, and pip-install
    # from, a package that is no longer in the graph, and would also orphan any
    # matching ``xms_dependency_options`` key.  Replace the whole dict in a
    # subclass rather than mutating this one in place (it is shared by every
    # recipe in the process, exactly like the two lists above).
    vs2019_dependency_overrides = {}

    default_options = {
        'wchar_t': 'builtin',
        'pybind': False,
        'testing': False,
        'python_version': '3.13',
    }

    def _is_vs2019(self):
        """Return True when this build targets Visual Studio 2019 (msvc 192).

        VS2019 packages are produced manually on a developer workstation and
        published to the ``aquaveo-vs2019`` remote, and they resolve the legacy
        third-party stack rather than the modern one.  ``settings`` values are
        Conan objects, not plain strings, so both sides are coerced with
        ``str()`` before comparing.

        The ``"192"`` literal is the same value as
        ``xmsconan.constants.MSVC_VS2019_VERSION``, but it cannot be imported
        here: ``build_file_generator.copy_xms_conan2_file()`` copies this file
        next to each library's generated ``conanfile.py``, which imports it as
        a bare top-level ``xms_conan2_file`` module with no ``xmsconan``
        package on the path.  Change both together.
        """
        compiler = str(self.settings.compiler)
        return compiler == "msvc" and str(self.settings.compiler.version) == "192"

    def _resolved_xms_dependencies(self):
        """Return ``xms_dependencies`` with the VS2019 overrides applied.

        On an msvc 192 build, any entry in ``vs2019_dependency_overrides`` whose
        key matches a dependency's package name (the text before the first
        ``/``) replaces that dependency's full reference.  On every other
        toolchain the list is returned unchanged.
        """
        if not self._is_vs2019():
            return list(self.xms_dependencies)
        return [
            self.vs2019_dependency_overrides.get(dependency.split('/')[0], dependency)
            for dependency in self.xms_dependencies
        ]

    def requirements(self):
        """Requirements."""
        # VS2019 resolves the legacy third-party stack the desktop products are
        # built against; everything else gets the modern one.
        for requirement in (self.vs2019_requirements if self._is_vs2019()
                            else self.default_requirements):
            self.requires(requirement)
        if self.options.testing:
            if self.testing_framework == "cxxtest":
                self.requires('cxxtest/4.4')
            elif self.testing_framework == "gtest":
                self.requires('gtest/1.17.0')
        if self.options.pybind and self.python_binding_type == "pybind11":
            # Intentionally 3.0.1 on VS2019 too — an upgrade from the
            # Conan-1-era pybind11/2.9.1.  Do not "fix" this back to 2.9.1.
            self.requires("pybind11/3.0.1")

        for dependency in self._resolved_xms_dependencies():
            self.requires(dependency)

        for dependency in self.extra_dependencies:
            self.requires(dependency)

    def configure(self):
        """The configure method."""
        # Fail fast at configure time when XMS_COVERAGE is requested but the
        # recipe does not name a python_namespaced_dir for pytest-cov to
        # target. Surfacing this before the build avoids burning a full
        # instrumented build only to abort deep in run_python_tests.
        if os.environ.get("XMS_COVERAGE") and not self.python_namespaced_dir:
            raise ConanException(
                "XMS_COVERAGE=1 requires python_namespaced_dir to be set on the "
                "recipe so pytest-cov measures only this library's namespace."
            )

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

        if self._is_vs2019():
            # The Conan 1 recipe propagated the recipe's own wchar_t setting
            # into the legacy Aquaveo boost; boost/1.86.0 has no such option,
            # which is why this is VS2019-only.
            #
            # ``self.options["boost"]`` is the consumer-side override proxy:
            # the assignment only records `boost/*:wchar_t=<value>`, and Conan
            # 2.31 SILENTLY IGNORES an override for an option the resolved
            # recipe does not declare.  The boost/1.74.0.3 currently published
            # on aquaveo-vs2019 is a binary-republish shim that declares no
            # options at all, so this line is a verified no-op today; it starts
            # taking effect once a boost that declares ``wchar_t`` is
            # published.  Nothing here can fail: the assignment performs no
            # validation, and real option validation happens later during graph
            # computation, outside configure().
            self.options["boost"].wchar_t = self.options.wchar_t

        for dependency in self.xms_dependencies:
            dep_split = dependency.split('/')
            dep_name = dep_split[0]
            dep_opts = self.xms_dependency_options.get(dep_name, {})
            self.options[dep_name].pybind = dep_opts.get('pybind', self.options.pybind)
            self.options[dep_name].testing = dep_opts.get('testing', self.options.testing)
            # In Conan 2, ``self.options[dep_name]`` is the consumer-side override
            # proxy and ``in`` always reports False, so assign unconditionally and
            # tolerate deps whose recipe doesn't define ``python_version``.
            try:
                self.options[dep_name].python_version = dep_opts.get(
                    'python_version', self.options.python_version
                )
            except ConanException:
                pass

    def package_id(self):
        """Drop python_version from the package_id when not building Python bindings."""
        if not self.info.options.pybind:
            del self.info.options.python_version

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
        tc.variables["PYTHON_TARGET_VERSION"] = str(self.options.python_version)

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
        variables["PYTHON_TARGET_VERSION"] = str(self.options.python_version)

        # Propagate XMS_COVERAGE as an explicit CMake -D variable when the
        # operator set it in the parent env (e.g., xmsconan_coverage). The
        # recipe's `configure()` already sees XMS_COVERAGE via os.environ
        # because it runs in the same process as conan-create, but CMake
        # is a child subprocess. Conan 2's `cmake.configure()` only
        # exports vars from the profile's [buildenv] when a
        # `VirtualBuildEnv` generator was declared in `generate()`, which
        # this recipe does not — so the env-based check in
        # CMakeLists.txt.jinja was silently false even when XMS_COVERAGE
        # was set in [buildenv]. Passing it as a -D var bypasses every
        # layer of env-propagation uncertainty.
        if os.environ.get("XMS_COVERAGE"):
            variables["XMS_COVERAGE"] = "1"

        if self.options.pybind:
            variables.update(self._get_python_cmake_hints())

        cmake.configure(variables=variables)
        cmake.build()
        cmake.install()

        try:
            if self.options.testing:
                self.run_cxx_tests(cmake)

            if self.options.pybind:
                self._build_wheel()
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
        python_target_version = str(self.options.python_version)
        coverage_enabled = bool(os.environ.get("XMS_COVERAGE"))

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
        if coverage_enabled:
            general_dependencies.append("pytest-cov")
        pip_extra_args = []
        pip_index = os.environ.get("XMS_COVERAGE_PIP_INDEX")
        if coverage_enabled and pip_index:
            pip_extra_args.extend(["--extra-index-url", pip_index])
        self.run(_pip_install_cmd(python_executable, *general_dependencies, *pip_extra_args))

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
        if coverage_enabled:
            if not self.python_namespaced_dir:
                raise ConanException(
                    "XMS_COVERAGE=1 requires python_namespaced_dir to be set on the "
                    "recipe so pytest-cov measures only this library's namespace."
                )
            cov_target = f"xms.{self.python_namespaced_dir}"
            cov_html_dir = os.path.join(self.build_folder, "coverage-html-py")
            cov_xml = os.path.join(self.build_folder, "cov-py.xml")
            cov_json = os.path.join(self.build_folder, "cov-py-summary.json")
            pytest_command += (
                f' --cov={cov_target}'
                f' --cov-report=xml:"{cov_xml}"'
                f' --cov-report=html:"{cov_html_dir}"'
                f' --cov-report=json:"{cov_json}"'
            )
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
