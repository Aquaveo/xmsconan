"""
Conanfile base for the xmscore projects compatible with Conan 2.x.
"""
import glob
import json
import os
import re
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
        "python_version": ["3.10", "3.13", "3.14"],
    }
    xms_dependencies = []
    # Non-XMS Conan deps, "name/version". Deduplicated by package name against
    # the recipe's own requires and against itself; see requirements().
    extra_dependencies = []
    extra_exports = []
    extra_export_sources = []
    testing_framework = "cxxtest"  # Options: "cxxtest" or "gtest"
    python_binding_type = "pybind11"  # Options: "pybind11" or "vtk_wrap"
    # Submodule under xms.<...> (e.g. "core"). Set by the generated subclass; required
    # when XMS_COVERAGE=1 so pytest-cov scopes to xms.<python_namespaced_dir>.
    python_namespaced_dir = None
    # Opt in to advertising the pybind module's import library (_<name>) to C++
    # consumers instead of the static library. Windows-only by construction: a
    # MODULE library is an unlinkable bundle on macOS, and on Linux the module
    # carries a SOABI suffix that find_library cannot match. Only for a library
    # whose consumers deliberately link the .pyd -- see package_info().
    pybind_advertises_module = False
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
    # CONSTRAINT (ENFORCED, see ``_validate_vs2019_dependency_overrides``): an
    # override may change only the VERSION or version range — the package name
    # on both sides of the entry must be identical — and its key must match a
    # declared ``xms_dependencies`` entry.  ``configure()`` and
    # ``run_python_tests()`` iterate the UNRESOLVED ``xms_dependencies``, so a
    # rename would set options on, and pip-install from, a package that is no
    # longer in the graph, and would also orphan any matching
    # ``xms_dependency_options`` key.  Replace the whole dict in a subclass
    # rather than mutating this one in place (it is shared by every recipe in
    # the process, exactly like the two lists above).
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

    def _validate_vs2019_dependency_overrides(self):
        """Reject ``vs2019_dependency_overrides`` entries that cannot do what they say.

        Both checks enforce the constraints documented on the attribute, and
        both run only on msvc 192 — every other toolchain ignores the dict, so
        an unused override is not an error there:

        * An entry may change the VERSION or version range only; the package
          name on both sides must be identical.  ``configure()`` and
          ``run_python_tests()`` iterate the UNRESOLVED ``xms_dependencies``
          and key ``xms_dependency_options`` by the original package name, so
          a rename would silently drop that dependency's per-dependency
          options and pip-install from a package that is no longer in the
          graph.
        * An entry's key must match a declared ``xms_dependencies`` package
          name.  Ignoring a key that matches nothing turns a typo in
          ``build.toml``'s ``[vs2019_dependency_overrides]`` table into a
          VS2019 build that quietly resolves the very versions the override
          was written to replace.

        Raises:
            ConanException: When an entry renames its package, or names a
                package this recipe does not declare as an xms_dependency.
        """
        for name, reference in self.vs2019_dependency_overrides.items():
            override_name = reference.split('/')[0]
            if override_name != name:
                raise ConanException(
                    f"vs2019_dependency_overrides entry '{name}' -> '{reference}' renames the "
                    f"package ('{name}' -> '{override_name}'). An override may change only the "
                    f"version or version range: configure() and run_python_tests() key off the "
                    f"unresolved xms_dependencies, so a rename would silently drop "
                    f"'{name}' option propagation and pip-install."
                )
        declared = {dependency.split('/')[0] for dependency in self.xms_dependencies}
        unmatched = sorted(set(self.vs2019_dependency_overrides) - declared)
        if unmatched:
            raise ConanException(
                f"vs2019_dependency_overrides names {', '.join(unmatched)}, which is not "
                f"declared in xms_dependencies. Declared xms_dependencies: "
                f"{', '.join(sorted(declared)) or '(none)'}."
            )

    def _resolved_xms_dependencies(self):
        """Return ``xms_dependencies`` with the VS2019 overrides applied.

        On an msvc 192 build, any entry in ``vs2019_dependency_overrides`` whose
        key matches a dependency's package name (the text before the first
        ``/``) replaces that dependency's full reference.  On every other
        toolchain the list is returned unchanged.

        Raises:
            ConanException: On msvc 192, when an override renames its package
                or matches no declared dependency (see
                ``_validate_vs2019_dependency_overrides``).
        """
        if not self._is_vs2019():
            return list(self.xms_dependencies)
        self._validate_vs2019_dependency_overrides()
        return [
            self.vs2019_dependency_overrides.get(dependency.split('/')[0], dependency)
            for dependency in self.xms_dependencies
        ]

    def requirements(self):
        """Requirements.

        ``extra_dependencies`` is deduplicated against everything the recipe
        requires on its own, and against itself.  Conan's duplicate check is by
        package name -- ``Requirement.__hash__`` is ``(ref.name, build)`` -- so
        a second entry naming the same package aborts graph computation no
        matter which version it carries.  A library whose static half needs
        Python has to list ``pybind11`` to keep its ``pybind=False``
        configurations compiling, and without this the same listing made every
        ``pybind=True`` build unbuildable.  The recipe's own reference wins,
        because that is the one the generated CMake and ``package_id`` agree
        with.
        """
        # VS2019 resolves the legacy third-party stack the desktop products are
        # built against; everything else gets the modern one.
        references = list(self.vs2019_requirements if self._is_vs2019()
                          else self.default_requirements)
        if self.options.testing:
            if self.testing_framework == "cxxtest":
                references.append('cxxtest/4.4')
            elif self.testing_framework == "gtest":
                references.append('gtest/1.17.0')
        if self.options.pybind and self.python_binding_type == "pybind11":
            # Intentionally 3.0.1 on VS2019 too — an upgrade from the
            # Conan-1-era pybind11/2.9.1.  Do not "fix" this back to 2.9.1.
            references.append("pybind11/3.0.1")

        references += self._resolved_xms_dependencies()

        # Keyed by package name so a skip can name the reference that won, not
        # just the one that lost.
        required = {reference.split('/')[0]: reference for reference in references}
        for entry in self.extra_dependencies:
            # Stripped: a stray space made "  boost/1.86.0" a different package
            # name here, so it slipped past the dedupe and reached
            # self.requires() beside the recipe's own boost.
            dependency = entry.strip()
            if not dependency:
                raise ConanException(
                    "extra_dependencies contains an empty entry. Remove it, or "
                    "name a package as 'name/version'."
                )
            name = dependency.split('/')[0]
            winner = required.get(name)
            if winner == dependency:
                # Exact duplicate of something already required: nothing is lost
                # by dropping it, so there is nothing to report.
                continue
            if winner is not None:
                # A warning, not info: ConanOutput.info is ConanOutput.status,
                # untagged text beside routine progress narration, once per
                # configuration in a 14-configuration run. What is being
                # discarded is a version pin the author wrote deliberately --
                # someone who pinned 7.1.0 to dodge a broken 7.2.0 gets 7.2.0,
                # and the generated find_package is version-agnostic so nothing
                # downstream catches it.
                self.output.warning(
                    f"extra_dependencies entry '{dependency}' is ignored: the recipe "
                    f"already requires '{winner}', and Conan deduplicates by package "
                    f"name. The build will use '{winner}'. Remove the entry or align "
                    f"its version."
                )
                continue
            required[name] = dependency
            references.append(dependency)

        for reference in references:
            self.requires(reference)

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

        self._validate_settings()
        self._propagate_dependency_options()

    def _validate_settings(self):
        """Reject compiler / OS combinations this recipe does not support.

        Raises:
            ConanException: For apple-clang on Linux, gcc older than 5.0, and
                apple-clang older than 9.0 on macOS.
        """
        s_os = self.settings.os
        s_compiler = self.settings.compiler
        s_compiler_version = self.settings.compiler.version

        if s_compiler == "apple-clang" and s_os == 'Linux':
            raise ConanException("Clang on Linux is not supported.")

        if s_compiler == "gcc" and float(s_compiler_version.value) < 5.0:
            raise ConanException("GCC < 5.0 is not supported.")

        if s_compiler == "apple-clang" and s_os == 'Macos' and float(s_compiler_version.value) < 9.0:
            raise ConanException("Clang > 9.0 is required for Mac.")

    def _propagate_dependency_options(self):
        """Push this recipe's options down onto the packages it consumes.

        Covers the VS2019-only boost ``wchar_t`` forward and the per-library
        ``pybind`` / ``testing`` / ``python_version`` propagation into every
        ``xms_dependencies`` entry (with ``xms_dependency_options`` overriding
        the top-level value per dependency).
        """
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

    # CMake refuses to reuse a binary directory configured by a different
    # generator ("Does not match the generator used previously"), so a Ninja
    # build and a Visual Studio build of the same recipe cannot share one.
    # Deriving the folder from the generator lets both coexist instead of
    # forcing a wipe to switch between them.
    _GENERATOR_FOLDER_SUFFIXES = {
        'ninja multi-config': 'ninja',
        'ninja': 'ninja',
        'xcode': 'xcode',
        'unix makefiles': 'make',
    }

    def _build_folder_name(self):
        """Return the build folder for this profile's generator and build kind.

        Must stay in step with ``xmsconan.constants.build_folder_for_generator``,
        which the generated CMakePresets.json uses for binaryDir. This file is
        copied next to the generated conanfile.py and imported standalone, so it
        cannot import from the xmsconan package (see the note in constants.py);
        the two are pinned together by a parity test.
        """
        generator = self.conf.get('tools.cmake.cmaketoolchain:generator', default=None)
        if not generator:
            # No generator pinned -- this is the ephemeral profile that build.py
            # uses. Keep the historical "build" folder so existing local and CI
            # workflows are untouched by profile-driven builds.
            return 'build'

        key = str(generator).strip().lower()
        suffix = self._GENERATOR_FOLDER_SUFFIXES.get(key)
        if suffix is None:
            # Visual Studio generators carry a version and year, so match the
            # family rather than enumerating every release.
            suffix = 'vs' if key.startswith('visual studio') else re.sub(r'[^a-z0-9]+', '-', key).strip('-')

        # Each kind installs a different dependency set, so they cannot share a
        # build folder without overwriting one another's generated toolchain.
        if self.options.get_safe('testing'):
            kind = 'testing'
        elif self.options.get_safe('pybind'):
            kind = 'python'
        else:
            kind = 'library'

        # The remaining package-id axes that vary within one platform matrix.
        # The Windows matrix fans out over runtime and wchar_t, so without these,
        # configurations that link against different runtimes would share one
        # binary directory and silently overwrite each other's toolchain.
        # compiler.version is deliberately excluded -- see the rationale in
        # xmsconan.constants.build_folder_for_generator.
        discriminators = []
        python_version = self.options.get_safe('python_version')
        if self.options.get_safe('pybind') and python_version:
            discriminators.append('py' + str(python_version).replace('.', ''))
        wchar_t = self.options.get_safe('wchar_t')
        if wchar_t and str(wchar_t) != 'builtin':
            discriminators.append(str(wchar_t))
        runtime = self.settings.get_safe('compiler.runtime')
        if runtime:
            discriminators.append(str(runtime))

        # Slugged like the generator suffix above, matching what
        # build_folder_for_generator does on the preset side.
        discriminators = [re.sub(r'[^A-Za-z0-9]+', '-', str(part)).strip('-')
                          for part in discriminators if part]
        # Underscore, not hyphen: the xms repos already ignore `build_*/`.
        parts = ['build', kind, suffix, *discriminators]
        return '_'.join(str(part) for part in parts if part) or 'build'

    def layout(self):
        """The layout method."""
        cmake_layout(self, build_folder=self._build_folder_name())
        # Editable-mode include root. ``self.cpp.source`` is consulted ONLY
        # when this package is consumed via ``conan editable add``; the
        # packaged layout is described by ``package_info()`` and is unaffected.
        # (``cmake_layout`` already points libdirs at build/<cfg> for editables,
        # so only the include root needs correcting here.)
        #
        # Headers are written as ``#include <xmscore/misc/XmError.h>``, i.e.
        # relative to the REPO ROOT, because that is where they sit in the
        # source tree. A built package is different: ``cmake.install()``
        # relocates them under ``include/``, which is why ``package_info()``
        # advertises ``include`` instead. Without this override an editable
        # consumer is handed ``<repo>/include``, which does not exist in a
        # source checkout, and every include fails to resolve.
        self.cpp.source.includedirs = ["."]

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

            if self.options.pybind and self._builds_wheel():
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

    def _builds_wheel(self):
        """Whether this pybind configuration should build and test a wheel.

        Everything but a Windows Debug build.  There the module is named
        ``_<name>_d``: ``pybind11_add_module`` clears the debug postfix and the
        generated CMakeLists re-asserts it, because ``_<name>_d`` is what
        ``package_info()`` advertises and what a C++ consumer links.  The
        generated ``_package/pyproject.toml`` names the extension module
        exactly once, as ``xms.<dir>._<name>``, so a Windows Debug wheel would
        install a module ``import`` cannot find and ``run_python_tests`` would
        fail on it.  That configuration exists to produce the Conan package a
        C++ consumer links (``bin/_<name>_d.<abi>.pyd`` plus
        ``lib/_<name>_d.lib``), not a wheel; ``Release`` is what ships to an
        index.

        Off Windows the postfix is never re-asserted, so a Debug module keeps
        its importable name -- and a Debug pybind build is exactly what
        ``xmsconan coverage`` instruments for the Python half of its report.
        Skipping the wheel there would report zero Python coverage with no
        error, so it must build its wheel and run its tests.

        Returns:
            True when the wheel should be built and the Python tests run.
        """
        if str(self.settings.build_type) == 'Debug' and str(self.settings.os) == 'Windows':
            self.output.info(
                "Windows Debug pybind build: skipping the wheel and the Python tests. The "
                "module is named _<name>_d, which the wheel's pyproject.toml cannot "
                "declare; this configuration ships the Conan package only."
            )
            return False
        return True

    def _advertises_module(self):
        """Whether ``cpp_info.libs`` should name the module's import library.

        Three conditions, all required.  ``pybind_advertises_module`` is the
        library's own opt-in: a consumer that links the .pyd gets only the
        symbols the module exports (``PyInit__<name>`` plus anything explicitly
        ``__declspec(dllexport)``), where the static library gives it
        everything, so this is a deliberate per-library choice and not a
        default.  ``options.pybind`` is what makes a module exist at all -- and
        note it propagates down the graph (see ``configure``), so a plain sister
        library in a pybind graph must not be caught by it.  Windows is
        structural: only there is the module's linkable half a separate import
        library.  A macOS ``MODULE`` target is a bundle that cannot be linked,
        and a Linux module carries a SOABI suffix
        (``_<name>.cpython-313-x86_64-linux-gnu.so``) that CMakeDeps'
        ``find_library(NAMES _<name>)`` cannot match, so advertising it there
        produces a configure error rather than a usable package.

        Returns:
            True when the module's import library should be advertised.
        """
        if not self.pybind_advertises_module:
            return False
        if not self.options.pybind:
            return False
        return str(self.settings.os) == 'Windows'

    def package_info(self):
        """The package_info method of the conan class.

        A package normally advertises its static library (``<name>lib``).  A
        library that sets ``pybind_advertises_module`` advertises the *module's*
        import library (``_<name>``) on Windows instead, for consumers that
        deliberately link the .pyd -- which is what lets an msvc 192
        application consume an msvc 194 build, since the compatibility
        guarantee only runs newer-consumes-older and a static link across that
        boundary is not supported.  Both libraries are in the package either
        way.  ``CMAKE_DEBUG_POSTFIX`` supplies the ``_d`` on the static
        library.  The module target takes more work: ``pybind11_add_module``
        overwrites ``DEBUG_POSTFIX`` with the interpreter's value, which is
        empty for a release interpreter, so the generated CMakeLists
        re-asserts ``_d`` on Windows.  That is what keeps the Debug module
        ``_<name>_d`` on disk as well as here.
        """
        if self.options.pybind:
            self.runenv_info.append('PYTHONPATH', os.path.join(self.package_folder, "_package"))

        lib_name = f'_{self.name}' if self._advertises_module() else f'{self.name}lib'
        if self.settings.build_type == 'Debug':
            self.cpp_info.libs = [f'{lib_name}_d']
        else:
            self.cpp_info.libs = [lib_name]

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
