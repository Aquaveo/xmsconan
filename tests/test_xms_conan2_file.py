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
    "conan.tools.env",
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
from xmsconan.constants import (  # noqa: E402,I201
    MSVC_VS2019_VERSION,
    PYTHON_BINDING_TYPES,
    SUPPORTED_PYTHON_VERSIONS,
    TESTING_FRAMEWORKS,
)
from xmsconan.xms_conan2_file import XmsConan2File  # noqa: E402
from .utils import patch_env  # noqa: E402,I100,I202


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

    @patch("xmsconan.xms_conan2_file._has_uv", return_value=True)
    @patch("os.makedirs")
    def test_build_logs_are_not_suppressed(self, _makedirs, _has_uv):
        """``uv build`` runs without ``--no-build-logs``.

        With the flag, a backend failure reports three lines that name no cause
        -- "Call to `setuptools.build_meta.build_wheel` failed (exit code: 1)"
        and nothing else. On a CI runner the container is gone by the time
        anyone reads that, and the traceback went with it.
        """
        obj = _make_conan_file("Windows", "x86_64")

        obj._build_wheel()

        command, = obj.run.call_args.args
        assert command.startswith("uv build --wheel ")
        assert "--no-build-logs" not in command


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
    """Verify package_id trims the option axes that do not identify the binary."""

    class _InfoOptions:
        """Plain stub so that ``del`` actually removes the attribute."""

        def __init__(self, pybind, coverage=False):
            self.pybind = pybind
            self.python_version = "3.13"
            self.coverage = coverage

    def _make_obj(self, pybind, coverage=False):
        obj = object.__new__(XmsConan2File)
        obj.info = MagicMock()
        obj.info.options = self._InfoOptions(pybind=pybind, coverage=coverage)
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

    def test_drops_coverage_when_not_instrumented(self):
        """coverage=False leaves the id byte-identical to the pre-option recipe.

        Deleting the False value keeps every already-released binary valid;
        only instrumented builds get a new id.
        """
        obj = self._make_obj(pybind=False)
        obj.package_id()
        assert not hasattr(obj.info.options, "coverage")

    def test_keeps_coverage_for_instrumented_builds(self):
        """coverage=True stays in the id.

        --build=missing can never confuse an instrumented binary with a
        production one.
        """
        obj = self._make_obj(pybind=False, coverage=True)
        obj.package_id()
        assert obj.info.options.coverage is True


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
        obj.options.coverage = False

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
    """Verify the conanfile keys instrumentation off the coverage option."""

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
        obj.options.wchar_t = "builtin"
        obj.options.coverage = False
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
    def test_build_passes_xms_coverage_as_cmake_variable_when_option_set(self, mock_cmake_cls):
        """``coverage=True`` must reach CMake as a ``-DXMS_COVERAGE`` variable.

        Conan 2's ``cmake.configure()`` runs CMake as a subprocess, so the
        option cannot be observed there directly; the recipe forwards it as
        a ``-D`` variable, which is also the only trigger left in the
        generated CMakeLists (the env fallback is gone). Driving this from
        the option is what makes instrumentation part of the package_id.
        """
        recipe = self._make_recipe_for_build()
        recipe.options.coverage = True
        recipe.build()

        configure_mock = mock_cmake_cls.return_value.configure
        assert configure_mock.called, "cmake.configure must be invoked"
        call_kwargs = configure_mock.call_args.kwargs
        variables = call_kwargs.get("variables") or {}
        assert variables.get("XMS_COVERAGE") == "1", (
            "build() must pass XMS_COVERAGE=1 as a CMake -D variable when the "
            f"coverage option is set; got variables={variables!r}"
        )

    @patch("xmsconan.xms_conan2_file.CMake")
    def test_build_registers_per_case_ctest_entries_when_instrumented(
            self, mock_cmake_cls):
        """An instrumented build is the one shape ctest still schedules.

        Every other shape reaches the runner through xmsconan_test_shards,
        which is why the generated CMakeLists registers the whole binary as a
        single ctest entry. ctest cannot parallelize one entry, so the
        coverage job's CTEST_PARALLEL_LEVEL had nothing to act on and ran the
        instrumented suite in one process.
        """
        recipe = self._make_recipe_for_build()
        recipe.options.coverage = True
        recipe.build()

        variables = (mock_cmake_cls.return_value.configure
                     .call_args.kwargs.get("variables") or {})
        assert variables.get("XMS_GTEST_DISCOVER_TESTS") == "ON", (
            "an instrumented build must register per-case ctest entries so "
            f"CTEST_PARALLEL_LEVEL has something to schedule; got "
            f"variables={variables!r}"
        )

    @patch("xmsconan.xms_conan2_file.CMake")
    def test_build_leaves_ctest_registration_alone_without_coverage(
            self, mock_cmake_cls):
        """A production build keeps the single whole-binary ctest entry.

        Per-case entries pay a process spawn per test case. That is worth it
        only where ctest is the scheduler; test_shards builds would pay it
        for nothing, having already got their concurrency elsewhere.
        """
        recipe = self._make_recipe_for_build()
        recipe.build()

        variables = (mock_cmake_cls.return_value.configure
                     .call_args.kwargs.get("variables") or {})
        assert "XMS_GTEST_DISCOVER_TESTS" not in variables, (
            "only instrumented builds should override the ctest registration; "
            f"got variables={variables!r}"
        )

    @patch("xmsconan.xms_conan2_file.CMake")
    def test_build_ignores_the_env_var_when_option_is_false(self, mock_cmake_cls,
                                                            monkeypatch):
        """``XMS_COVERAGE`` in the environment alone must not instrument.

        The env-var era let a build instrument itself while its package_id
        said otherwise — the exact silent mismatch the option exists to
        close. A leaked ``-DXMS_COVERAGE=1`` would also link gcov
        instrumentation into the shipping wheel.
        """
        recipe = self._make_recipe_for_build()
        # monkeypatch, not a bare assignment with `finally: del`: the manual
        # form deletes the variable on the way out rather than restoring it, so
        # a developer running the suite with XMS_COVERAGE already exported has
        # it silently unset for every test that follows.
        monkeypatch.setenv("XMS_COVERAGE", "1")
        recipe.build()

        configure_mock = mock_cmake_cls.return_value.configure
        variables = configure_mock.call_args.kwargs.get("variables") or {}
        assert "XMS_COVERAGE" not in variables, (
            f"build() must key instrumentation off the coverage option, not "
            f"the environment; got variables={variables!r}"
        )

    def test_run_python_tests_uses_pytest_cov_when_option_set(self, tmp_path):
        """When coverage=True, pytest is invoked with --cov flags."""
        obj = object.__new__(XmsConan2File)
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(os.path.join(obj.source_folder, "_package", "tests"), exist_ok=True)
        with open(os.path.join(obj.source_folder, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        obj.options = MagicMock()
        obj.options.python_version = "3.13"
        obj.options.coverage = True
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        # Stub _find_wheel so we don't need a real wheel.
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))

        obj.run_python_tests()

        pytest_calls = [c for c in obj.run.call_args_list if "-m pytest" in str(c)]
        assert pytest_calls, "expected at least one pytest invocation"
        pytest_cmd = str(pytest_calls[-1])
        assert "--cov=xms.core" in pytest_cmd
        assert "--cov-report=xml:" in pytest_cmd
        assert "--cov-report=html:" in pytest_cmd
        assert "--cov-report=json:" in pytest_cmd

    def _python_test_recipe(self, tmp_path):
        """Build the minimum recipe instance run_python_tests() needs."""
        obj = object.__new__(XmsConan2File)
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(os.path.join(obj.source_folder, "_package", "tests"), exist_ok=True)
        with open(os.path.join(obj.source_folder, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        obj.options = MagicMock()
        obj.options.python_version = "3.13"
        obj.options.coverage = False
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))
        return obj

    def _dependency_at(self, package_folder):
        """Return a stub conan dependency reporting *package_folder*."""
        dependency = MagicMock()
        dependency.package_folder = str(package_folder)
        return dependency

    def test_run_python_tests_installs_dependency_package(self, tmp_path):
        """A dependency that ships _package/ is pip-installed from it."""
        obj = self._python_test_recipe(tmp_path)
        dep_folder = tmp_path / "xmscore-pkg"
        (dep_folder / "_package").mkdir(parents=True)
        obj.xms_dependencies = ["xmscore/[>=7.0.0]"]
        obj.dependencies.host = {"xmscore": self._dependency_at(dep_folder)}

        obj.run_python_tests()

        installs = [str(c) for c in obj.run.call_args_list if "_package" in str(c)]
        assert len(installs) == 1
        assert "--no-deps" in installs[0]

    def test_run_python_tests_skips_dependency_without_package_dir(self, tmp_path):
        """A dependency built with pybind=False is skipped, not installed.

        Such a dependency packages no _package/ directory, so
        `pip install <missing path>` exited non-zero and took down the Python
        test phase of the very consumer that asked for the smaller dependency
        through xms_dependency_options.
        """
        obj = self._python_test_recipe(tmp_path)
        dep_folder = tmp_path / "xmscore-pkg"
        dep_folder.mkdir()  # packaged, but with no _package/ inside
        obj.xms_dependencies = ["xmscore/[>=7.0.0]"]
        obj.dependencies.host = {"xmscore": self._dependency_at(dep_folder)}

        obj.run_python_tests()

        assert not [c for c in obj.run.call_args_list if "_package" in str(c)]
        assert any("xmscore" in str(c) for c in obj.output.info.call_args_list)

    def test_run_python_tests_applies_conanrun_environment(self, tmp_path):
        """Run pytest under conanrun so dependency shared libraries resolve.

        The installed module keeps only $ORIGIN as its RPATH, so without the
        run environment the loader cannot find a dependency built shared.
        """
        obj = self._python_test_recipe(tmp_path)

        obj.run_python_tests()

        pytest_calls = [c for c in obj.run.call_args_list if "pytest" in str(c)]
        assert pytest_calls, "expected a pytest invocation"
        assert pytest_calls[-1].kwargs["env"] == "conanrun"

    def test_dependency_runtime_dirs_keeps_existing_dirs_only(self, tmp_path):
        """Directories that do not exist are dropped, and none repeats."""
        obj = self._python_test_recipe(tmp_path)
        shared_dir = tmp_path / "laslib" / "bin"
        shared_dir.mkdir(parents=True)
        dependency = MagicMock()
        dependency.cpp_info.bindirs = [str(shared_dir), str(tmp_path / "gone")]
        dependency.cpp_info.libdirs = [str(shared_dir)]
        obj.dependencies.host = {"laslib": dependency}

        assert obj._dependency_runtime_dirs() == [str(shared_dir)]

    def test_windows_dll_hook_registers_dependency_dirs(self, tmp_path):
        """The hook registers each dependency directory with the DLL loader."""
        obj = self._python_test_recipe(tmp_path)
        venv_dir = tmp_path / "venv"
        (venv_dir / "Lib" / "site-packages").mkdir(parents=True)
        shared_dir = tmp_path / "laslib" / "bin"
        shared_dir.mkdir(parents=True)
        dependency = MagicMock()
        dependency.cpp_info.bindirs = [str(shared_dir)]
        dependency.cpp_info.libdirs = []
        obj.dependencies.host = {"laslib": dependency}

        obj._write_windows_dll_hook(str(venv_dir))

        hook = (venv_dir / "Lib" / "site-packages" / "sitecustomize.py").read_text()
        compile(hook, "sitecustomize.py", "exec")
        assert "os.add_dll_directory" in hook
        assert repr(str(shared_dir)) in hook

    def test_windows_dll_hook_handles_non_ascii_paths(self, tmp_path):
        """A dependency path outside ASCII survives the write.

        The hook records absolute paths, and repr() keeps non-ASCII characters
        as themselves, so writing in the platform default encoding raised
        UnicodeEncodeError on a host whose default is not UTF-8.
        """
        obj = self._python_test_recipe(tmp_path)
        venv_dir = tmp_path / "venv"
        (venv_dir / "Lib" / "site-packages").mkdir(parents=True)
        shared_dir = tmp_path / "テスト" / "bin"
        shared_dir.mkdir(parents=True)
        dependency = MagicMock()
        dependency.cpp_info.bindirs = [str(shared_dir)]
        dependency.cpp_info.libdirs = []
        obj.dependencies.host = {"laslib": dependency}

        obj._write_windows_dll_hook(str(venv_dir))

        hook_path = venv_dir / "Lib" / "site-packages" / "sitecustomize.py"
        hook = hook_path.read_text(encoding="utf-8")
        compile(hook, "sitecustomize.py", "exec")
        assert "テスト" in hook

    def test_windows_dll_hook_is_valid_python_without_dependencies(self, tmp_path):
        """A package with no shared dependencies still gets a usable hook."""
        obj = self._python_test_recipe(tmp_path)
        venv_dir = tmp_path / "venv"
        (venv_dir / "Lib" / "site-packages").mkdir(parents=True)

        obj._write_windows_dll_hook(str(venv_dir))

        hook = (venv_dir / "Lib" / "site-packages" / "sitecustomize.py").read_text()
        compile(hook, "sitecustomize.py", "exec")

    def test_windows_dll_hook_warns_when_site_packages_missing(self, tmp_path):
        """A venv without site-packages warns rather than failing the build."""
        obj = self._python_test_recipe(tmp_path)

        obj._write_windows_dll_hook(str(tmp_path / "missing-venv"))

        assert obj.output.warning.called

    def test_run_python_tests_does_not_add_cov_when_option_unset(
        self, tmp_path, monkeypatch,
    ):
        """coverage=False adds no --cov flags, even with XMS_COVERAGE in the env."""
        obj = object.__new__(XmsConan2File)
        obj.build_folder = str(tmp_path / "build")
        obj.source_folder = str(tmp_path / "source")
        os.makedirs(obj.build_folder, exist_ok=True)
        os.makedirs(os.path.join(obj.source_folder, "_package", "tests"), exist_ok=True)
        with open(os.path.join(obj.source_folder, "pytest.ini"), "w") as f:
            f.write("[pytest]\n")
        obj.options = MagicMock()
        obj.options.python_version = "3.13"
        obj.options.coverage = False
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))

        # See the note in test_build_ignores_the_env_var_when_option_is_false:
        # monkeypatch restores the prior value, `finally: del` does not.
        monkeypatch.setenv("XMS_COVERAGE", "1")
        obj.run_python_tests()

        pytest_cmd = str(obj.run.call_args_list[-1])
        assert "--cov" not in pytest_cmd

    def test_configure_raises_when_coverage_set_without_namespaced_dir(self):
        """coverage=True + pybind=True + missing python_namespaced_dir fails early.

        The deep run_python_tests check still exists as defense in depth, but
        catching this in configure() avoids wasting a full instrumented build
        before the failure surfaces. Only the pybind leg is gated — it is the
        one that runs pytest-cov.
        """
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = None
        obj.options = MagicMock()
        obj.options.pybind = True
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.options.coverage = True
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.settings = MagicMock()
        obj.settings.os = MagicMock(__str__=lambda s: "Linux")
        obj.settings.compiler = MagicMock(__str__=lambda s: "gcc")
        obj.settings.compiler.version = MagicMock(value="13.0")

        with pytest.raises(ConanException, match="python_namespaced_dir"):
            obj.configure()

    def test_configure_permits_missing_namespaced_dir_without_coverage(self):
        """With coverage=False, an unset python_namespaced_dir is allowed."""
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = None
        obj.options = MagicMock()
        obj.options.pybind = False
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.options.coverage = False
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.settings = MagicMock()
        obj.settings.os = MagicMock(__str__=lambda s: "Linux")
        obj.settings.compiler = MagicMock(__str__=lambda s: "gcc")
        obj.settings.compiler.version = MagicMock(value="13.0")

        obj.configure()  # must not raise

    def test_configure_permits_cpp_only_coverage_without_namespaced_dir(self):
        """coverage=True on a pybind=False leg needs no python_namespaced_dir.

        The C++ legs never run pytest-cov, so a hand-written pure-C++ recipe
        can collect C++ coverage without configuring a Python namespace it
        does not have.
        """
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = None
        obj.options = MagicMock()
        obj.options.pybind = False
        obj.options.testing = True
        obj.options.python_version = "3.13"
        obj.options.coverage = True
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.settings = MagicMock()
        obj.settings.os = MagicMock(__str__=lambda s: "Linux")
        obj.settings.compiler = MagicMock(__str__=lambda s: "gcc")
        obj.settings.compiler.version = MagicMock(value="13.0")

        obj.configure()  # must not raise

    def test_run_python_tests_raises_when_cov_target_unknown(self, tmp_path):
        """coverage=True without python_namespaced_dir must fail loudly.

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
        obj.options.coverage = True
        obj.python_namespaced_dir = None
        obj.xms_dependencies = []
        obj.dependencies = MagicMock()
        obj.dependencies.host = {}
        obj.run = MagicMock()
        obj.output = MagicMock()
        obj._find_wheel = MagicMock(return_value=str(tmp_path / "fake.whl"))

        with pytest.raises(ConanException, match="python_namespaced_dir"):
            obj.run_python_tests()


class _Setting(str):
    """A ``str`` that carries attributes, standing in for a Conan settings value.

    Conan's settings values stringify to their value, compare equal to plain
    strings, hang sub-settings off themselves (``compiler.version``) and expose
    ``.value``.  A MagicMock stringifies but does *not* compare equal to a
    string, which would silently skip every ``s_compiler == "gcc"`` comparison
    in ``_validate_settings``.
    """


def _make_settings(compiler, compiler_version, os_name="Windows"):
    """Build a mock ``settings`` object whose values behave like Conan's."""
    settings = MagicMock()
    settings.os = _Setting(os_name)
    settings.compiler = _Setting(compiler)
    settings.compiler.version = _Setting(compiler_version)
    settings.compiler.version.value = compiler_version
    return settings


class TestValidateSettings:
    """Verify configure() rejects toolchains this recipe cannot build."""

    def _make_obj(self, compiler, version, os_name):
        """Create a recipe stub whose configure() reaches the settings validation."""
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.options = MagicMock()
        obj.settings = _make_settings(compiler, version, os_name)
        return obj

    @pytest.mark.parametrize(
        "compiler,version,os_name,message",
        [
            ("apple-clang", "17", "Linux", "Clang on Linux"),
            ("gcc", "4.9", "Linux", "GCC < 5.0"),
            ("apple-clang", "8.0", "Macos", "Clang > 9.0 is required"),
        ],
    )
    def test_rejects_unsupported_toolchain(self, compiler, version, os_name, message):
        """Unsupported compiler/OS pairs fail at configure time, before any building."""
        obj = self._make_obj(compiler, version, os_name)
        with pytest.raises(ConanException, match=message):
            obj.configure()


class TestPackageInfo:
    """Verify package_info advertises the library a consumer should link."""

    @staticmethod
    def _make_obj(pybind, build_type, advertises=True, os_name="Windows"):
        """Create a recipe stub whose package_info can be called in isolation."""
        obj = object.__new__(XmsConan2File)
        obj.name = "data_objects"
        obj.settings = MagicMock()
        obj.settings.build_type = build_type
        obj.settings.os = MagicMock(__str__=lambda _s: os_name)
        obj.options = MagicMock()
        obj.options.pybind = pybind
        obj.pybind_advertises_module = advertises
        obj.package_folder = os.path.join("fake", "package")
        obj.cpp_info = MagicMock()
        obj.runenv_info = MagicMock()
        return obj

    @pytest.mark.parametrize(
        "pybind,build_type,expected",
        [
            (True, "Release", "_data_objects"),
            (True, "Debug", "_data_objects_d"),
            (False, "Release", "data_objectslib"),
            (False, "Debug", "data_objectslib_d"),
        ],
    )
    def test_opted_in_library_advertises_the_module(self, pybind, build_type, expected):
        """An opted-in Windows pybind package names the module's import library.

        XMS dynamically links the .pyd, which is what lets an msvc 192 consumer
        keep consuming an msvc 194 build -- the guarantee only runs
        newer-consumes-older, so handing it the static library instead is an
        unsupported link. The Debug name carries ``_d`` because the generated
        CMakeLists re-asserts ``DEBUG_POSTFIX`` on the module target, which
        pybind11 otherwise clears -- see
        ``test_debug_pybind_module_keeps_the_d_postfix``.
        """
        obj = self._make_obj(pybind, build_type)
        obj.package_info()
        assert obj.cpp_info.libs == [expected]

    @pytest.mark.parametrize("build_type,expected", [
        ("Release", "data_objectslib"),
        ("Debug", "data_objectslib_d"),
    ])
    def test_library_that_does_not_opt_in_keeps_the_static_library(self, build_type, expected):
        """Without the opt-in, a pybind package advertises what it always did.

        ``pybind`` propagates down the dependency graph -- ``configure()`` sets
        it on every ``xms_dependencies`` entry -- so keying off the option alone
        would redirect every sister library in a pybind graph to a module its
        consumers never asked for.
        """
        obj = self._make_obj(pybind=True, build_type=build_type, advertises=False)
        obj.package_info()
        assert obj.cpp_info.libs == [expected]

    @pytest.mark.parametrize("os_name", ["Linux", "Macos"])
    def test_module_is_never_advertised_off_windows(self, os_name):
        """Off Windows the module is not a linkable artifact, opt-in or not.

        A macOS ``MODULE`` target is a bundle that cannot be linked at all, and
        a Linux module is ``_<name>.cpython-313-x86_64-linux-gnu.so``, which the
        ``find_library(NAMES _<name>)`` CMakeDeps emits does not match -- so
        advertising it there turns a working package into a configure error.
        """
        obj = self._make_obj(pybind=True, build_type="Release", os_name=os_name)
        obj.package_info()
        assert obj.cpp_info.libs == ["data_objectslib"]

    def test_pybind_package_still_exports_pythonpath(self):
        """Selecting the module's lib must not disturb the _package PYTHONPATH entry.

        It has to go through append_path: plain append joins with a space, so a
        consumer that already has PYTHONPATH set ends up with the two paths in
        one string that matches nothing.
        """
        obj = self._make_obj(pybind=True, build_type="Release")
        obj.package_info()
        obj.runenv_info.append_path.assert_called_once_with(
            "PYTHONPATH", os.path.join("fake", "package", "_package")
        )
        assert not obj.runenv_info.append.called


class TestBuildsWheel:
    """Verify which pybind configurations build and test a wheel."""

    @staticmethod
    def _make_obj(build_type: str, os_name: str = "Windows") -> XmsConan2File:
        """Create a recipe stub whose _builds_wheel can be called in isolation."""
        obj = object.__new__(XmsConan2File)
        obj.settings = MagicMock()
        obj.settings.build_type = MagicMock(__str__=lambda _s: build_type)
        obj.settings.os = MagicMock(__str__=lambda _s: os_name)
        obj.output = MagicMock()
        return obj

    @staticmethod
    def _skip_messages(obj: XmsConan2File) -> list[str]:
        """Return the logged lines that announce the wheel being skipped.

        Matched on the skip's own wording rather than "was output.info called at
        all", so an unrelated log line added to ``_builds_wheel`` later does not
        fail these tests, and so a skip that stops naming Windows does.
        """
        return [call.args[0] for call in obj.output.info.call_args_list
                if "skipping the wheel" in call.args[0]]

    @pytest.mark.parametrize("os_name", ["Windows", "Linux", "Macos"])
    def test_release_builds_the_wheel(self, os_name: str) -> None:
        """Release is the configuration that ships to an index, on every platform."""
        obj = self._make_obj("Release", os_name)
        assert obj._builds_wheel() is True
        assert self._skip_messages(obj) == []

    def test_windows_debug_does_not_build_a_wheel(self) -> None:
        """A Windows Debug wheel would install a module the shipped Python cannot import.

        For an opted-in library the generated CMakeLists re-asserts
        ``DEBUG_POSTFIX "_d"`` on the module target -- pybind11 having
        overwritten it -- so the Windows Debug build produces
        ``_<name>_d.<abi>.pyd`` while the shipped Python imports
        ``xms.<dir>._<name>``. Renaming the module is not an option: consumers
        link ``_<name>_d`` by that exact name. The skip is deliberately broader
        than the rename, since no wheel step in the generated CI publishes a
        Debug leg.
        """
        obj = self._make_obj("Debug")
        assert obj._builds_wheel() is False

        messages = self._skip_messages(obj)
        assert len(messages) == 1, "skipping a deliverable must be stated, once"
        # The old message stated a Windows-specific reason on every platform.
        # Pinning the platform stops it drifting back to a bare "Debug".
        assert "Windows Debug" in messages[0]

    @pytest.mark.parametrize("os_name", ["Linux", "Macos"])
    def test_debug_builds_the_wheel_off_windows(self, os_name: str) -> None:
        """A Debug pybind build off Windows still builds its wheel and tests.

        The postfix is re-asserted on Windows only, so off Windows the module
        keeps the name the shipped Python imports it by and its wheel installs
        like any other. A library only has a Debug pybind configuration because
        it named ``Debug`` in ``[matrix].pybind_build_types``, so a
        ``build_type``-only skip would silently drop the Python tests for the
        very module its consumers link.
        """
        obj = self._make_obj("Debug", os_name)
        assert obj._builds_wheel() is True
        assert self._skip_messages(obj) == []


class TestRequirements:
    """Verify requirements() picks the right third-party stack and deps."""

    def _make_obj(self, compiler="gcc", version="13", testing=False, pybind=False,
                  testing_framework="cxxtest", python_binding_type="pybind11",
                  xms_dependencies=(), extra_dependencies=(), overrides=None):
        """Create a recipe stub that records every requires() call."""
        obj = object.__new__(XmsConan2File)
        obj.settings = _make_settings(compiler, version)
        obj.options = MagicMock()
        obj.options.testing = testing
        obj.options.pybind = pybind
        obj.testing_framework = testing_framework
        obj.python_binding_type = python_binding_type
        obj.xms_dependencies = list(xms_dependencies)
        obj.extra_dependencies = list(extra_dependencies)
        if overrides is not None:
            obj.vs2019_dependency_overrides = overrides
        obj.requires = MagicMock()
        obj.output = MagicMock()
        return obj

    @staticmethod
    def _required(obj):
        """Return the list of references passed to requires()."""
        return [call.args[0] for call in obj.requires.call_args_list]

    def test_modern_toolchain_gets_default_requirements(self):
        """Non-VS2019 builds get boost 1.86.0 + zlib and nothing legacy."""
        obj = self._make_obj(compiler="msvc", version="194")
        obj.requirements()
        assert self._required(obj) == ["boost/1.86.0", "zlib/1.3.1"]

    def test_vs2019_gets_legacy_requirements(self):
        """VS2019 gets the same packages as the modern stack at legacy versions.

        zlib is required on both stacks — the generated CMakeLists calls
        find_package(ZLIB REQUIRED) — but msvc 192 binaries exist only for
        1.2.11, so the version differs from default_requirements.

        Driving this from ``MSVC_VS2019_VERSION`` is also what pins the
        standalone ``"192"`` literal in ``xms_conan2_file._is_vs2019`` to
        ``xmsconan.constants``: the recipe file is copied next to each
        generated ``conanfile.py`` and imported as a bare top-level module, so
        it cannot import the constant and has to repeat the value. Tests have
        no such restriction, so bumping either side alone reddens here instead
        of silently forking the two values.
        """
        obj = self._make_obj(compiler="msvc", version=MSVC_VS2019_VERSION)
        obj.requirements()
        assert self._required(obj) == ["boost/1.74.0.3", "zlib/1.2.11"]

    @pytest.mark.parametrize(
        "framework,expected",
        [
            ("cxxtest", ["cxxtest/4.4"]),
            ("gtest", ["gtest/1.17.0"]),
            # configure() now rejects an unrecognized framework outright (see
            # TestValidateRecipeAttributes); requirements() keeps adding
            # nothing for one as defense in depth, since a hand-written recipe
            # can call it without going through configure().
            ("unknown", []),
        ],
    )
    def test_testing_framework_requirement(self, framework, expected):
        """testing=True pulls the configured framework (and only that one)."""
        obj = self._make_obj(testing=True, testing_framework=framework)
        obj.requirements()
        assert self._required(obj)[2:] == expected

    @pytest.mark.parametrize("compiler,version", [("gcc", "13"), ("msvc", "192")])
    def test_pybind11_is_shared_by_both_stacks(self, compiler, version):
        """pybind11/3.0.1 is required on VS2019 too — not the Conan-1-era 2.9.1."""
        obj = self._make_obj(compiler=compiler, version=version, pybind=True)
        obj.requirements()
        assert "pybind11/3.0.1" in self._required(obj)

    def test_vtk_wrap_binding_skips_pybind11(self):
        """A vtk_wrap binding build does not pull pybind11."""
        obj = self._make_obj(pybind=True, python_binding_type="vtk_wrap")
        obj.requirements()
        assert not any(r.startswith("pybind11/") for r in self._required(obj))

    def test_xms_and_extra_dependencies_pass_through(self):
        """Off VS2019, xms_dependencies and extra_dependencies are used verbatim.

        The override here is deliberately invalid on both counts the VS2019
        path rejects (it renames its package, and its key matches no
        dependency): off msvc 192 the dict is neither applied nor validated, so
        a gcc build must not fail over a table it ignores.
        """
        obj = self._make_obj(
            xms_dependencies=["xmscore/[>=7.0.0 <8.0.0]"],
            extra_dependencies=["fmt/10.2.1"],
            overrides={"xmsgrd": "xmsgrid/1.2.3"},
        )
        obj.requirements()
        required = self._required(obj)
        assert "xmscore/[>=7.0.0 <8.0.0]" in required, "overrides must be VS2019-only"
        assert "xmsgrid/1.2.3" not in required
        assert "fmt/10.2.1" in required

    @pytest.mark.parametrize("extra", ["pybind11/3.0.1", "pybind11/2.9.1"])
    def test_extra_dependency_duplicating_a_recipe_requirement_is_skipped(self, extra):
        """An extra_dependencies entry the recipe already requires is dropped.

        Conan's duplicate check is by package name -- ``Requirement.__hash__``
        is ``(ref.name, build)`` -- so naming a different version does not dodge
        it. A library whose static half needs pybind11 must list it to keep the
        ``pybind=False`` configurations compiling, and that listing used to
        abort every ``pybind=True`` build with "Duplicated requirement:
        pybind11/3.0.1".
        """
        obj = self._make_obj(pybind=True, extra_dependencies=[extra])
        obj.requirements()
        required = self._required(obj)
        assert [r for r in required if r.startswith("pybind11/")] == ["pybind11/3.0.1"]
        if extra == "pybind11/3.0.1":
            # An exact duplicate loses nothing, so there is nothing to report.
            assert not obj.output.warning.called
        else:
            # A discarded *pin* is a warning that names both references, so the
            # author can see which version the build actually used.
            message = obj.output.warning.call_args[0][0]
            assert extra in message
            assert "pybind11/3.0.1" in message

    def test_extra_dependency_is_kept_when_nothing_else_requires_it(self):
        """The same entry survives on a configuration that does not require it itself.

        The mirror of the test above: with ``pybind=False`` the recipe adds no
        pybind11, so the library's own entry is the only thing keeping
        ``find_package(pybind11)`` satisfied for its cxxtest configurations.
        """
        obj = self._make_obj(pybind=False, extra_dependencies=["pybind11/3.0.1"])
        obj.requirements()
        assert "pybind11/3.0.1" in self._required(obj)
        # Specifically no *skip* report -- asserting nothing was logged at all
        # would redden on any future diagnostic in requirements().
        assert not obj.output.warning.called

    def test_extra_dependencies_are_deduplicated_against_each_other(self):
        """Two entries naming one package collapse to the first.

        Same Conan constraint reached from a different direction -- a repeated
        name inside ``extra_dependencies`` aborts graph computation exactly as a
        clash with the recipe's own requires does.
        """
        obj = self._make_obj(extra_dependencies=["fmt/10.2.1", "fmt/11.0.0"])
        obj.requirements()
        required = self._required(obj)
        assert [r for r in required if r.startswith("fmt/")] == ["fmt/10.2.1"]

    def test_extra_dependency_duplicating_an_xms_dependency_is_skipped(self):
        """The dedupe covers xms_dependencies, not just the third-party stack."""
        obj = self._make_obj(
            xms_dependencies=["xmscore/[>=7.0.0 <8.0.0]"],
            extra_dependencies=["xmscore/7.1.0"],
        )
        obj.requirements()
        required = self._required(obj)
        assert [r for r in required if r.startswith("xmscore/")] == ["xmscore/[>=7.0.0 <8.0.0]"]
        # The warning has to name the range that won, not just the pin that lost.
        message = obj.output.warning.call_args[0][0]
        assert "xmscore/7.1.0" in message
        assert "xmscore/[>=7.0.0 <8.0.0]" in message

    def test_vs2019_dependency_override_replaces_matching_reference(self):
        """On VS2019 an override replaces its dep, matched on the name before '/'."""
        obj = self._make_obj(
            compiler="msvc",
            version="192",
            xms_dependencies=["xmscore/[>=7.0.0 <8.0.0]", "xmsgrid/1.2.3"],
            overrides={"xmscore": "xmscore/[>=6.0.1 <7.0.0]"},
        )
        obj.requirements()
        required = self._required(obj)
        assert "xmscore/[>=6.0.1 <7.0.0]" in required
        assert "xmscore/[>=7.0.0 <8.0.0]" not in required
        assert "xmsgrid/1.2.3" in required, "unlisted deps must be untouched"

    def test_vs2019_override_rejects_package_rename(self):
        """An override that renames its package fails instead of silently dropping options.

        ``configure()`` and ``run_python_tests()`` iterate the UNRESOLVED
        ``xms_dependencies``, so the renamed package would never receive its
        ``xms_dependency_options`` entry and would never be pip-installed —
        the recipe would build against one package while configuring another.
        """
        obj = self._make_obj(
            compiler="msvc",
            version=MSVC_VS2019_VERSION,
            xms_dependencies=["xmscore/[>=7.0.0 <8.0.0]"],
            overrides={"xmscore": "xmsgrid/1.0"},
        )
        with pytest.raises(ConanException) as exc_info:
            obj.requirements()

        message = str(exc_info.value)
        assert "xmscore" in message and "xmsgrid" in message

    def test_vs2019_override_rejects_key_matching_no_dependency(self):
        """An override key that matches nothing is a build.toml typo, not a no-op.

        Ignoring it would produce a VS2019 build that quietly resolves the very
        versions the ``[vs2019_dependency_overrides]`` entry was written to
        replace.
        """
        obj = self._make_obj(
            compiler="msvc",
            version=MSVC_VS2019_VERSION,
            xms_dependencies=["xmscore/[>=7.0.0 <8.0.0]"],
            overrides={"xmsocre": "xmsocre/[>=6.0.1 <7.0.0]"},
        )
        with pytest.raises(ConanException) as exc_info:
            obj.requirements()

        message = str(exc_info.value)
        assert "xmsocre" in message
        assert "xmscore" in message, "the message should list the declared dependencies"


class TestVs2019BoostWcharT:
    """Verify configure() forwards wchar_t to the legacy boost on VS2019 only.

    There is deliberately no "boost doesn't declare wchar_t" test here.  The
    assignment goes through Conan's consumer-side override proxy, which does
    no validation and cannot raise; Conan 2.31 silently ignores an override for
    an option the resolved recipe never declares.  A test double that raised on
    assignment would prove nothing about the real client.
    """

    def _make_obj(self, compiler="msvc", version="192"):
        """Create a recipe stub whose configure() reaches the boost propagation."""
        obj = object.__new__(XmsConan2File)
        obj.python_namespaced_dir = "core"
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        obj.settings = _make_settings(compiler, version)
        obj.options = MagicMock()
        obj.options.pybind = False
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.options.wchar_t = "typedef"
        boost_opts = MagicMock()
        obj.options.__getitem__.side_effect = lambda key: boost_opts
        return obj, boost_opts

    def test_propagates_wchar_t_on_vs2019(self):
        """The legacy boost 1.74.0.3 exposes wchar_t; the recipe's value flows in."""
        obj, boost_opts = self._make_obj()
        obj.configure()
        assert boost_opts.wchar_t == "typedef"

    def test_no_propagation_off_vs2019(self):
        """boost/1.86.0 has no wchar_t option, so modern builds never touch it.

        The observable property is that ``configure()`` never reaches for the
        ``self.options["boost"]`` override proxy at all — this stub declares no
        xms_dependencies, so any subscript of ``options`` would be that lookup.
        Inspecting the returned proxy instead would prove nothing: assigning an
        attribute on a mock is not a recorded method call, and reading it back
        out of the mock's attribute storage tests unittest.mock, not the recipe.
        """
        obj, _boost_opts = self._make_obj(compiler="msvc", version="194")
        obj.configure()
        obj.options.__getitem__.assert_not_called()


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


def test_supported_python_versions_matches_the_recipe():
    """Constants must agree with the conanfile's closed option set.

    ``xms_conan2_file`` cannot import from ``constants`` -- it is copied next to
    each generated conanfile and has to stand alone -- so the list is duplicated
    by design and pinned here instead. This lives in this module rather than the
    CI-generator tests because importing the recipe requires the conan stubs
    installed at the top of this file.
    """
    assert set(SUPPORTED_PYTHON_VERSIONS) == set(XmsConan2File.options["python_version"])


def test_vocabularies_match_the_recipe():
    """The generator's accepted values must agree with the recipe's.

    Duplicated for the same standalone-copy reason as
    SUPPORTED_PYTHON_VERSIONS. If they drift, `xmsconan gen` accepts a value
    the recipe then rejects at configure time -- or worse, the reverse.
    """
    assert set(TESTING_FRAMEWORKS) == set(XmsConan2File.TESTING_FRAMEWORKS)
    assert set(PYTHON_BINDING_TYPES) == set(XmsConan2File.PYTHON_BINDING_TYPES)


class TestValidateRecipeAttributes:
    """configure() rejects attribute values that name nothing that exists."""

    def _make_obj(self, **attributes):
        """Create a recipe stub carrying only what configure() validation reads."""
        obj = object.__new__(XmsConan2File)
        obj.testing_framework = "cxxtest"
        obj.python_binding_type = "pybind11"
        obj.xms_dependencies = []
        obj.xms_dependency_options = {}
        for name, value in attributes.items():
            setattr(obj, name, value)
        return obj

    def test_configure_runs_the_validation(self):
        """configure() is what actually reaches the check.

        Every other case here calls _validate_recipe_attributes() directly, so
        without this one the call configure() makes could be deleted outright
        and the whole class would stay green while the check quietly stopped
        running in every consumer's recipe.
        """
        obj = self._make_obj(testing_framework="gtst")
        obj.python_namespaced_dir = "core"
        obj.options = MagicMock()
        obj.settings = _make_settings("msvc", "194")

        with pytest.raises(ConanException, match="testing_framework"):
            obj.configure()

    @pytest.mark.parametrize("attribute,value,expected", [
        ("testing_framework", "gtst", "testing_framework"),
        ("python_binding_type", "pybind", "python_binding_type"),
    ])
    def test_misspelled_vocabulary_value_raises(self, attribute, value, expected):
        """A near-miss value fails here rather than much later, or never.

        "gtst" used to add no test framework requirement and fail at CMake
        configure with "cannot find cxxtest"; "pybind" was silent end to end
        and shipped a Python package with no native module in it.
        """
        obj = self._make_obj(**{attribute: value})

        with pytest.raises(ConanException, match=expected) as excinfo:
            obj._validate_recipe_attributes()

        assert value in str(excinfo.value)

    @pytest.mark.parametrize("framework", XmsConan2File.TESTING_FRAMEWORKS)
    def test_documented_frameworks_are_accepted(self, framework):
        """Every value the docs offer passes."""
        self._make_obj(testing_framework=framework)._validate_recipe_attributes()

    @pytest.mark.parametrize("binding", XmsConan2File.PYTHON_BINDING_TYPES)
    def test_documented_binding_types_are_accepted(self, binding):
        """Every value the docs offer passes."""
        self._make_obj(python_binding_type=binding)._validate_recipe_attributes()

    def test_dependency_options_for_undeclared_package_raises(self):
        """An override keyed on a package that is not a dependency is a typo.

        Dropping it silently meant the option someone wrote -- usually to turn
        a dependency's pybind off -- never applied, and the only symptom was a
        heavier build than asked for.
        """
        obj = self._make_obj(
            xms_dependencies=["xmscore/[>=7.0.0]"],
            xms_dependency_options={"xmscoer": {"pybind": False}},
        )

        with pytest.raises(ConanException, match="xmscoer") as excinfo:
            obj._validate_recipe_attributes()

        assert "xmscore" in str(excinfo.value)

    def test_dependency_options_for_declared_package_is_accepted(self):
        """A key matching a declared dependency passes."""
        obj = self._make_obj(
            xms_dependencies=["xmscore/[>=7.0.0]"],
            xms_dependency_options={"xmscore": {"pybind": False}},
        )

        obj._validate_recipe_attributes()


class TestCMakeVariables:
    """generate() and build() must seed CMake from one place, and gate wchar_t."""

    def _make_recipe(self, wchar_t="builtin", pybind=False):
        """Build a stub recipe carrying what _cmake_variables() reads."""
        obj = object.__new__(XmsConan2File)
        obj.options = MagicMock()
        obj.options.pybind = pybind
        obj.options.testing = False
        obj.options.python_version = "3.13"
        obj.options.wchar_t = wchar_t
        obj.options.coverage = False
        obj.testing_framework = "cxxtest"
        obj.python_binding_type = "pybind11"
        obj.version = "0.0.0"
        obj.output = MagicMock()
        obj._save_test_artifacts = MagicMock()
        obj.run_cxx_tests = MagicMock()
        return obj

    @pytest.mark.parametrize("wchar_t,expected", [("typedef", True), ("builtin", False)])
    def test_use_typedef_wchar_t_follows_the_option(self, wchar_t, expected):
        """The variable that gates /Zc:wchar_t- tracks the wchar_t option.

        Nothing set it before: the only setter in the tree was the Conan 1
        recipe base. So every msvc wchar_t=typedef package built since the
        Conan 2 migration is a builtin build carrying a different package_id.
        """
        variables = self._make_recipe(wchar_t=wchar_t)._cmake_variables()
        assert variables["USE_TYPEDEF_WCHAR_T"] is expected

    # Cleared as a guard: build() keys XMS_COVERAGE off the coverage option
    # now, but this test is the drift detector, and clearing proves nothing
    # under these mocks reads the environment — a regression back to env
    # sensitivity would fail here on a developer machine that exports it.
    @patch_env(clear=True)
    @patch("xmsconan.xms_conan2_file.CMakeDeps")
    @patch("xmsconan.xms_conan2_file.CMakeToolchain")
    @patch("xmsconan.xms_conan2_file.CMake")
    def test_generate_and_build_cannot_drift(self, mock_cmake, mock_toolchain, mock_deps):
        """Both entry points set exactly the same variables, minus XMS_COVERAGE.

        They were two hand-maintained copies, and USE_TYPEDEF_WCHAR_T is what
        happened when one of them needed a new variable: neither got it.
        """
        toolchain_variables = {}
        mock_toolchain.return_value.variables = toolchain_variables

        self._make_recipe(wchar_t="typedef").generate()
        self._make_recipe(wchar_t="typedef").build()

        configure_variables = mock_cmake.return_value.configure.call_args.kwargs["variables"]
        assert toolchain_variables == configure_variables
        assert toolchain_variables["USE_TYPEDEF_WCHAR_T"] is True

    @patch("xmsconan.xms_conan2_file.CMakeDeps")
    @patch("xmsconan.xms_conan2_file.CMakeToolchain")
    @patch("xmsconan.xms_conan2_file.VirtualRunEnv")
    def test_generate_declares_a_run_environment(self, mock_run_env, mock_toolchain, mock_deps):
        """generate() must write the conanrun script run_python_tests depends on.

        The installed pybind module keeps only $ORIGIN as its RPATH, so without
        conanrun the Python tests cannot load a dependency built as a shared
        library. That is one line in generate(), and nothing else here would
        notice if it went away.
        """
        recipe = self._make_recipe()

        recipe.generate()

        mock_run_env.assert_called_once_with(recipe)
        mock_run_env.return_value.generate.assert_called_once()

    @patch("xmsconan.xms_conan2_file.CMake")
    def test_coverage_stays_out_of_the_toolchain(self, mock_cmake):
        """XMS_COVERAGE belongs to configure() alone, not to the shared set."""
        assert "XMS_COVERAGE" not in self._make_recipe()._cmake_variables()
