"""Tests for xms_conan2_file."""
import os
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

from xmsconan.xms_conan2_file import XmsConan2File  # noqa: E402


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


class TestGetPythonCmakeHints:
    """Verify _get_python_cmake_hints returns correct FindPython3 hints."""

    def test_returns_executable(self):
        """Python3_EXECUTABLE points to sys.executable."""
        obj = object.__new__(XmsConan2File)
        hints = obj._get_python_cmake_hints()
        assert hints["Python3_EXECUTABLE"] == sys.executable

    def test_returns_include_dir(self):
        """Python3_INCLUDE_DIR matches sysconfig include path."""
        obj = object.__new__(XmsConan2File)
        hints = obj._get_python_cmake_hints()
        assert hints["Python3_INCLUDE_DIR"] == sysconfig.get_path('include')

    def test_disables_framework_search(self):
        """Python3_FIND_FRAMEWORK is NEVER to skip macOS framework search."""
        obj = object.__new__(XmsConan2File)
        hints = obj._get_python_cmake_hints()
        assert hints["Python3_FIND_FRAMEWORK"] == "NEVER"
