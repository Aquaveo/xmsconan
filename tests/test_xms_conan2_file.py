"""Tests for xms_conan2_file."""
import os
from pathlib import Path
import sys
import sysconfig
from unittest.mock import MagicMock, patch

import pytest

# Stub out the conan package so xms_conan2_file can be imported without
# conan installed in the test environment.
_conan_stubs = {}
for mod_name in [
    "conan",
    "conan.errors",
    "conan.tools",
    "conan.tools.cmake",
    "conan.tools.files",
]:
    stub = MagicMock()
    _conan_stubs[mod_name] = stub
    sys.modules.setdefault(mod_name, stub)

# Ensure ConanFile is a real class so object.__new__ works.
_conan_stubs["conan"].ConanFile = type("ConanFile", (), {})
sys.modules["conan"].ConanFile = _conan_stubs["conan"].ConanFile

# Ensure ConanException is a real Exception subclass so ``except ConanException``
# clauses in production code evaluate correctly under the stubs.
_conan_stubs["conan.errors"].ConanException = type("ConanException", (Exception,), {})
sys.modules["conan.errors"].ConanException = _conan_stubs["conan.errors"].ConanException

from conan.errors import ConanException  # noqa: E402,I100,I202
from xmsconan.xms_conan2_file import XmsConan2File  # noqa: E402,I201


def _make_conan_file(os_name, arch):
    """Create an XmsConan2File with mocked settings and folders."""
    obj = object.__new__(XmsConan2File)
    obj.settings = MagicMock()
    obj.settings.os = MagicMock(__str__=lambda s: os_name)
    obj.settings.arch = MagicMock(__str__=lambda s: arch)
    obj.package_folder = "/fake/package"
    obj.build_folder = "/fake/build"
    obj.run = MagicMock()
    return obj


class TestBuildWheelPlatformOverride:
    """Verify _build_wheel sets _PYTHON_HOST_PLATFORM for macOS ARM."""

    @patch("xmsconan.xms_conan2_file._has_uv", return_value=True)
    @patch("os.makedirs")
    def test_sets_host_platform_for_macos_arm(self, _makedirs, _has_uv):
        """_PYTHON_HOST_PLATFORM is set to arm64 for Macos/armv8."""
        obj = _make_conan_file("Macos", "armv8")
        captured_env = {}

        def capture_run(*args, **kwargs):
            captured_env["_PYTHON_HOST_PLATFORM"] = os.environ.get("_PYTHON_HOST_PLATFORM")

        obj.run.side_effect = capture_run

        os.environ.pop("_PYTHON_HOST_PLATFORM", None)
        obj._build_wheel()

        assert captured_env["_PYTHON_HOST_PLATFORM"] == "macosx-15.0-arm64"
        assert "_PYTHON_HOST_PLATFORM" not in os.environ

    @patch("xmsconan.xms_conan2_file._has_uv", return_value=True)
    @patch("os.makedirs")
    def test_not_set_for_linux(self, _makedirs, _has_uv):
        """_PYTHON_HOST_PLATFORM is not set for Linux builds."""
        obj = _make_conan_file("Linux", "x86_64")
        captured_env = {}

        def capture_run(*args, **kwargs):
            captured_env["_PYTHON_HOST_PLATFORM"] = os.environ.get("_PYTHON_HOST_PLATFORM")

        obj.run.side_effect = capture_run

        os.environ.pop("_PYTHON_HOST_PLATFORM", None)
        obj._build_wheel()

        assert captured_env["_PYTHON_HOST_PLATFORM"] is None
        assert "_PYTHON_HOST_PLATFORM" not in os.environ

    @patch("xmsconan.xms_conan2_file._has_uv", return_value=True)
    @patch("os.makedirs")
    def test_not_set_for_macos_x86(self, _makedirs, _has_uv):
        """_PYTHON_HOST_PLATFORM is not set for macOS x86_64 builds."""
        obj = _make_conan_file("Macos", "x86_64")
        captured_env = {}

        def capture_run(*args, **kwargs):
            captured_env["_PYTHON_HOST_PLATFORM"] = os.environ.get("_PYTHON_HOST_PLATFORM")

        obj.run.side_effect = capture_run

        os.environ.pop("_PYTHON_HOST_PLATFORM", None)
        obj._build_wheel()

        assert captured_env["_PYTHON_HOST_PLATFORM"] is None

    @patch("xmsconan.xms_conan2_file._has_uv", return_value=True)
    @patch("os.makedirs")
    def test_restores_previous_value(self, _makedirs, _has_uv):
        """Pre-existing _PYTHON_HOST_PLATFORM is restored after build."""
        obj = _make_conan_file("Macos", "armv8")

        os.environ["_PYTHON_HOST_PLATFORM"] = "original-value"
        try:
            obj._build_wheel()
            assert os.environ.get("_PYTHON_HOST_PLATFORM") == "original-value"
        finally:
            os.environ.pop("_PYTHON_HOST_PLATFORM", None)

    @patch("xmsconan.xms_conan2_file._has_uv", return_value=True)
    @patch("os.makedirs")
    def test_restores_on_error(self, _makedirs, _has_uv):
        """_PYTHON_HOST_PLATFORM is cleaned up even if build fails."""
        obj = _make_conan_file("Macos", "armv8")
        obj.run.side_effect = RuntimeError("build failed")

        os.environ.pop("_PYTHON_HOST_PLATFORM", None)
        with pytest.raises(RuntimeError):
            obj._build_wheel()

        assert "_PYTHON_HOST_PLATFORM" not in os.environ


class TestSaveTestArtifacts:
    """Verify _save_test_artifacts copies the right files."""

    def _make_obj(self, tmp_path, testing=False, pybind=False, artifacts_dir=None, label="test-label"):
        """Create an XmsConan2File with real temp directories."""
        obj = object.__new__(XmsConan2File)
        obj.settings = MagicMock()
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        obj.output = MagicMock()

        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(obj.source_folder, exist_ok=True)

        # Mock options
        obj.options = MagicMock()
        obj.options.testing = testing
        obj.options.pybind = pybind

        # Mock buildenv
        env = {}
        if artifacts_dir:
            env["XMS_TEST_ARTIFACTS_DIR"] = str(artifacts_dir)
            env["XMS_TEST_ARTIFACTS_LABEL"] = label
        obj.buildenv = MagicMock()
        obj.buildenv.vars.return_value = env

        return obj

    def test_noop_when_not_configured(self, tmp_path):
        """Returns early when XMS_TEST_ARTIFACTS_DIR is not set."""
        obj = self._make_obj(tmp_path, testing=True)
        obj._save_test_artifacts()
        # No makedirs call → nothing created
        assert not (tmp_path / "artifacts").exists()

    def test_copies_lasttest_log(self, tmp_path):
        """LastTest.log is copied for testing builds."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, testing=True, artifacts_dir=dest, label="Release-testing")

        # Create LastTest.log in build folder
        log_dir = os.path.join(obj.build_folder, "Testing", "Temporary")
        os.makedirs(log_dir)
        log_path = os.path.join(log_dir, "LastTest.log")
        with open(log_path, "w") as f:
            f.write("test output")

        obj._save_test_artifacts()

        copied = dest / "Release-testing" / "LastTest.log"
        assert copied.exists()
        assert copied.read_text() == "test output"

    def test_copies_test_files_for_testing(self, tmp_path):
        """test_files/ from source_folder is copied for testing builds."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, testing=True, artifacts_dir=dest, label="Debug-testing")

        # Create test_files in source folder
        tf_dir = os.path.join(obj.source_folder, "test_files")
        os.makedirs(tf_dir)
        with open(os.path.join(tf_dir, "output.png"), "w") as f:
            f.write("image data")

        obj._save_test_artifacts()

        copied = dest / "Debug-testing" / "test_files" / "output.png"
        assert copied.exists()
        assert copied.read_text() == "image data"

    def test_skips_test_files_for_pybind(self, tmp_path):
        """test_files/ is NOT copied for pybind builds."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, pybind=True, artifacts_dir=dest, label="Release-pybind")

        # Create test_files in source folder (should be ignored)
        tf_dir = os.path.join(obj.source_folder, "test_files")
        os.makedirs(tf_dir)
        with open(os.path.join(tf_dir, "output.png"), "w") as f:
            f.write("image data")

        obj._save_test_artifacts()

        assert not (dest / "Release-pybind" / "test_files").exists()

    def test_copies_package_for_pybind(self, tmp_path):
        """source_folder/_package/ is copied for pybind builds."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, pybind=True, artifacts_dir=dest, label="Release-pybind")

        # Create _package dir in source folder
        pkg_dir = os.path.join(obj.source_folder, "_package", "tests", "files")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "output.2dm"), "w") as f:
            f.write("mesh data")

        obj._save_test_artifacts()

        copied = dest / "Release-pybind" / "_package" / "tests" / "files" / "output.2dm"
        assert copied.exists()
        assert copied.read_text() == "mesh data"

    def test_skips_package_for_testing(self, tmp_path):
        """source_folder/_package/ is NOT copied for testing (C++) builds."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, testing=True, artifacts_dir=dest, label="Release-testing")

        # Create _package dir in source folder (should be ignored for C++ testing)
        pkg_dir = os.path.join(obj.source_folder, "_package")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "setup.py"), "w") as f:
            f.write("from setuptools import setup")

        obj._save_test_artifacts()

        assert not (dest / "Release-testing" / "_package").exists()

    def test_copies_runner_binary_multiconfig(self, tmp_path):
        """Runner binary is copied from Debug/ subfolder (multi-config generators)."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, testing=True, artifacts_dir=dest, label="Debug-testing")

        # Create a fake runner binary in Debug/ subfolder (Visual Studio, Ninja Multi-Config)
        if sys.platform == "win32":
            runner_path = os.path.join(obj.build_folder, "Debug", "runner.exe")
        else:
            runner_path = os.path.join(obj.build_folder, "Debug", "runner")
        os.makedirs(os.path.dirname(runner_path), exist_ok=True)
        with open(runner_path, "wb") as f:
            f.write(b"\x7fELF")  # fake binary

        obj._save_test_artifacts()

        copied = dest / "Debug-testing" / os.path.basename(runner_path)
        assert copied.exists()
        assert copied.read_bytes() == b"\x7fELF"

    def test_copies_runner_binary_singleconfig(self, tmp_path):
        """Runner binary is copied from build root (single-config generators like Ninja)."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, testing=True, artifacts_dir=dest, label="Debug-testing")

        # Create a fake runner binary at build root (Ninja single-config)
        runner_name = "runner.exe" if sys.platform == "win32" else "runner"
        runner_path = os.path.join(obj.build_folder, runner_name)
        with open(runner_path, "wb") as f:
            f.write(b"\x7fELF")  # fake binary

        obj._save_test_artifacts()

        copied = dest / "Debug-testing" / runner_name
        assert copied.exists()
        assert copied.read_bytes() == b"\x7fELF"

    def test_saves_test_path_metadata(self, tmp_path):
        """Metadata file records the compiled-in XMS_TEST_PATH."""
        dest = tmp_path / "artifacts"
        obj = self._make_obj(tmp_path, testing=True, artifacts_dir=dest, label="Debug-testing")

        obj._save_test_artifacts()

        meta = dest / "Debug-testing" / "test_metadata.json"
        assert meta.exists()
        import json
        data = json.loads(meta.read_text())
        assert "test_path" in data
        assert data["test_path"] == os.path.join(obj.source_folder, "test_files") + "/"
        assert "build_folder" in data
        assert data["build_folder"] == obj.build_folder


class TestSkipCxxTests:
    """Verify XMS_SKIP_CXX_TESTS env var skips test execution."""

    def test_skip_cxx_tests_when_env_set(self):
        """Tests are skipped when XMS_SKIP_CXX_TESTS=1."""
        obj = object.__new__(XmsConan2File)
        obj.options = MagicMock()
        obj.options.testing = True
        obj.options.pybind = False
        obj.output = MagicMock()

        cmake = MagicMock()
        os.environ["XMS_SKIP_CXX_TESTS"] = "1"
        try:
            obj.run_cxx_tests(cmake)
        finally:
            del os.environ["XMS_SKIP_CXX_TESTS"]

        cmake.test.assert_not_called()

    def test_runs_cxx_tests_when_env_not_set(self):
        """Tests run normally when XMS_SKIP_CXX_TESTS is not set."""
        obj = object.__new__(XmsConan2File)
        obj.options = MagicMock()
        obj.options.testing = True
        obj.options.pybind = False
        obj.output = MagicMock()

        cmake = MagicMock()
        os.environ.pop("XMS_SKIP_CXX_TESTS", None)
        obj.run_cxx_tests(cmake)

        cmake.test.assert_called_once()


class TestPackageId:
    """Verify package_id drops python_version for non-pybind builds."""

    class _InfoOptions:
        """Plain stub so that ``del`` actually removes the attribute."""

        def __init__(self, pybind):
            self.pybind = pybind
            self.python_version = "3.13"

    def _make_obj(self, pybind):
        obj = object.__new__(XmsConan2File)
        obj.info = MagicMock()
        obj.info.options = self._InfoOptions(pybind=pybind)
        return obj

    def test_drops_python_version_when_not_pybind(self):
        """package_id removes python_version from info.options when pybind is False."""
        obj = self._make_obj(pybind=False)
        obj.package_id()
        assert not hasattr(obj.info.options, "python_version")

    def test_keeps_python_version_for_pybind(self):
        """package_id keeps python_version on info.options when pybind is True."""
        obj = self._make_obj(pybind=True)
        obj.package_id()
        assert obj.info.options.python_version == "3.13"


class TestXmsDependencyPythonVersionPropagation:
    """Verify configure() propagates python_version into each xms_dependency.

    Consumers rely on the XmsConan2File option flowing into the dep options so
    that a top-level pybind=True / python_version=3.10 build wires every sister
    library to the matching ABI.

    In Conan 2, ``self.options[dep_name]`` returns a freshly-created empty
    consumer-side override proxy — ``'python_version' in <proxy>`` is always
    False — so the propagation must not gate the assignment on a containment
    check.  Setting an attribute on the proxy is the supported way to push a
    downstream option override; if the dep recipe doesn't define the option,
    Conan raises ConanException, which we tolerate.
    """

    def _make_obj(self, dep_options_override=None, dep_raises=False):
        """Construct an obj whose configure() will iterate xms_dependencies."""
        obj = object.__new__(XmsConan2File)
        obj.xms_dependencies = ["foo/1.0"]
        obj.xms_dependency_options = (
            {"foo": dep_options_override} if dep_options_override else {}
        )

        # Top-level options
        obj.options = MagicMock()
        obj.options.pybind = True
        obj.options.testing = False
        obj.options.python_version = "3.10"

        # Dep options accessed via self.options[dep_name].  Mirror Conan 2's
        # empty proxy: ``in`` reports False for everything.  When the dep
        # recipe lacks python_version, assigning it raises ConanException.
        if dep_raises:
            class _RaisingDepOpts:
                pybind = None
                testing = None

                def __contains__(self, _key):
                    return False

                def __setattr__(self, name, value):
                    if name == "python_version":
                        raise ConanException(f"option '{name}' doesn't exist")
                    object.__setattr__(self, name, value)

            dep_opts = _RaisingDepOpts()
        else:
            dep_opts = MagicMock()
            dep_opts.__contains__.return_value = False

        boost_opts = MagicMock()
        obj.options.__getitem__.side_effect = (
            lambda key: dep_opts if key == "foo" else boost_opts
        )

        # Settings — pick something configure() doesn't reject.
        obj.settings = MagicMock()
        obj.settings.os = MagicMock(__str__=lambda s: "Linux")
        obj.settings.compiler = MagicMock(__str__=lambda s: "gcc")
        obj.settings.compiler.version = MagicMock(value="13.0")

        return obj, dep_opts

    def test_propagates_top_level_python_version(self):
        """Without a per-dep override, the top-level python_version flows through."""
        obj, dep_opts = self._make_obj()
        obj.configure()
        assert dep_opts.python_version == "3.10"

    def test_per_dep_override_wins(self):
        """An xms_dependency_options entry overrides the top-level value."""
        obj, dep_opts = self._make_obj(
            dep_options_override={"python_version": "3.13"},
        )
        obj.configure()
        assert dep_opts.python_version == "3.13"

    def test_tolerates_dep_without_python_version_option(self):
        """Deps whose recipe doesn't define python_version don't break configure()."""
        obj, _dep_opts = self._make_obj(dep_raises=True)
        obj.configure()  # must not raise


class TestCoverageWiring:
    """Verify the conanfile cooperates with XMS_COVERAGE for combined builds."""

    def test_configure_options_method_removed(self):
        """The configure_options() method that del'd pybind on non-Release is gone.

        Pybind must be permitted with any build_type so the coverage workflow
        can run pybind=True, build_type=Debug in one shot.
        """
        assert not hasattr(XmsConan2File, "configure_options")

    def _make_recipe_for_build(self):
        """Build a stub XmsConan2File suitable for invoking ``build()`` under mocks."""
        obj = object.__new__(XmsConan2File)
        obj.options = MagicMock()
        obj.options.pybind = False
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.testing_framework = "cxxtest"
        obj.python_binding_type = "pybind11"
        obj.version = "0.0.0"
        obj.run = MagicMock()
        obj.output = MagicMock()
        obj._save_test_artifacts = MagicMock()
        obj._build_wheel = MagicMock()
        obj.run_python_tests = MagicMock()
        obj.run_cxx_tests = MagicMock()
        return obj

    @patch("xmsconan.xms_conan2_file.CMake")
    def test_build_passes_xms_coverage_as_cmake_variable_when_env_set(self, mock_cmake_cls):
        """``XMS_COVERAGE`` from the parent env must reach CMake as a ``-D`` variable.

        Conan 2's ``cmake.configure()`` runs CMake as a subprocess and
        does *not* inherit the recipe's parent-process env unless a
        ``VirtualBuildEnv`` generator is declared in ``generate()`` —
        which this recipe doesn't have. The recipe's Python ``configure()``
        sees XMS_COVERAGE fine (same process as conan-create), but CMake
        did not, so ``CMakeLists.txt.jinja``'s
        ``if (DEFINED ENV{XMS_COVERAGE})`` block was silently false even
        with XMS_COVERAGE in the profile's [buildenv]. Passing it as a
        ``-D`` variable bypasses that whole layer of uncertainty.
        """
        recipe = self._make_recipe_for_build()
        os.environ["XMS_COVERAGE"] = "1"
        try:
            recipe.build()
        finally:
            del os.environ["XMS_COVERAGE"]

        configure_mock = mock_cmake_cls.return_value.configure
        assert configure_mock.called, "cmake.configure must be invoked"
        call_kwargs = configure_mock.call_args.kwargs
        variables = call_kwargs.get("variables") or {}
        assert variables.get("XMS_COVERAGE") == "1", (
            "build() must pass XMS_COVERAGE=1 as a CMake -D variable when the "
            f"parent env has it; got variables={variables!r}"
        )

    @patch("xmsconan.xms_conan2_file.CMake")
    def test_build_omits_xms_coverage_when_env_unset(self, mock_cmake_cls):
        """Production builds (no ``XMS_COVERAGE`` in env) must not get ``--coverage``.

        A leaked ``-DXMS_COVERAGE=1`` would link gcov instrumentation
        into the shipping wheel — bloated binary, slower runtime, and
        a defeat of any optimization. The check on
        ``os.environ.get("XMS_COVERAGE")`` must be the only gate.
        """
        recipe = self._make_recipe_for_build()
        os.environ.pop("XMS_COVERAGE", None)
        recipe.build()

        configure_mock = mock_cmake_cls.return_value.configure
        variables = configure_mock.call_args.kwargs.get("variables") or {}
        assert "XMS_COVERAGE" not in variables, (
            f"build() must not leak XMS_COVERAGE into production builds; "
            f"got variables={variables!r}"
        )

    def test_run_python_tests_uses_pytest_cov_when_env_set(self, tmp_path):
        """When XMS_COVERAGE=1, pytest is invoked with --cov flags."""
        obj = object.__new__(XmsConan2File)
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(os.path.join(obj.source_folder, "_package", "tests"), exist_ok=True)
        with open(os.path.join(obj.source_folder, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        obj.options = MagicMock()
        obj.options.python_version = "3.13"
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        # Stub _find_wheel so we don't need a real wheel.
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))

        os.environ["XMS_COVERAGE"] = "1"
        try:
            obj.run_python_tests()
        finally:
            del os.environ["XMS_COVERAGE"]

        pytest_calls = [c for c in obj.run.call_args_list if "-m pytest" in str(c)]
        assert pytest_calls, "expected at least one pytest invocation"
        pytest_cmd = str(pytest_calls[-1])
        assert "--cov=xms.core" in pytest_cmd
        assert "--cov-report=xml:" in pytest_cmd
        assert "--cov-report=html:" in pytest_cmd
        assert "--cov-report=json:" in pytest_cmd

    def test_run_python_tests_does_not_add_cov_when_env_unset(self, tmp_path):
        """Without XMS_COVERAGE, no --cov flags are added."""
        obj = object.__new__(XmsConan2File)
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(os.path.join(obj.source_folder, "_package", "tests"), exist_ok=True)
        with open(os.path.join(obj.source_folder, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        obj.options = MagicMock()
        obj.options.python_version = "3.13"
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))

        os.environ.pop("XMS_COVERAGE", None)
        obj.run_python_tests()

        pytest_cmd = str(obj.run.call_args_list[-1])
        assert "--cov" not in pytest_cmd

    def test_configure_raises_when_coverage_set_without_namespaced_dir(self):
        """XMS_COVERAGE=1 + missing python_namespaced_dir fails at configure time.

        The deep run_python_tests check still exists as defense in depth, but
        catching this in configure() avoids wasting a full instrumented build
        before the failure surfaces.
        """
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = None
        obj.options = MagicMock()
        obj.options.pybind = False
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.settings = MagicMock()
        obj.settings.os = MagicMock(__str__=lambda s: "Linux")
        obj.settings.compiler = MagicMock(__str__=lambda s: "gcc")
        obj.settings.compiler.version = MagicMock(value="13.0")

        os.environ["XMS_COVERAGE"] = "1"
        try:
            with pytest.raises(ConanException, match="python_namespaced_dir"):
                obj.configure()
        finally:
            del os.environ["XMS_COVERAGE"]

    def test_configure_permits_missing_namespaced_dir_without_coverage(self):
        """Without XMS_COVERAGE, an unset python_namespaced_dir is allowed."""
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = None
        obj.options = MagicMock()
        obj.options.pybind = False
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.settings = MagicMock()
        obj.settings.os = MagicMock(__str__=lambda s: "Linux")
        obj.settings.compiler = MagicMock(__str__=lambda s: "gcc")
        obj.settings.compiler.version = MagicMock(value="13.0")

        os.environ.pop("XMS_COVERAGE", None)
        obj.configure()  # must not raise

    def test_run_python_tests_raises_when_cov_target_unknown(self, tmp_path):
        """XMS_COVERAGE=1 without python_namespaced_dir must fail loudly.

        Falling back to ``--cov=xms`` would silently mix in coverage from
        xms_dependencies installed in the venv.
        """
        obj = object.__new__(XmsConan2File)
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(os.path.join(obj.source_folder, "_package", "tests"), exist_ok=True)
        with open(os.path.join(obj.source_folder, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        obj.options = MagicMock()
        obj.options.python_version = "3.13"
        obj.python_namespaced_dir = None
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))

        os.environ["XMS_COVERAGE"] = "1"
        try:
            with pytest.raises(ConanException, match="python_namespaced_dir"):
                obj.run_python_tests()
        finally:
            del os.environ["XMS_COVERAGE"]


class TestGetPythonCmakeHints:
    """Verify _get_python_cmake_hints returns correct FindPython3 hints."""

    def test_returns_executable(self):
        """Python3_EXECUTABLE points to sys.executable."""
        obj = object.__new__(XmsConan2File)
        hints = obj._get_python_cmake_hints()
        assert Path(hints["Python3_EXECUTABLE"]) == Path(sys.executable)

    def test_returns_include_dir(self):
        """Python3_INCLUDE_DIR matches sysconfig include path."""
        obj = object.__new__(XmsConan2File)
        hints = obj._get_python_cmake_hints()
        assert Path(hints["Python3_INCLUDE_DIR"]) == Path(sysconfig.get_path('include'))

    def test_disables_framework_search(self):
        """Python3_FIND_FRAMEWORK is NEVER to skip macOS framework search."""
        obj = object.__new__(XmsConan2File)
        hints = obj._get_python_cmake_hints()
        assert hints["Python3_FIND_FRAMEWORK"] == "NEVER"
