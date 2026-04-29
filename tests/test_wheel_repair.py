"""Tests for ci_tools.wheel_repair."""
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.wheel_repair import _detect_platform, _pip_install_cmd, wheel_repair


# --- _detect_platform ---


@patch("xmsconan.ci_tools.wheel_repair.sys.platform", "linux")
def test_detect_linux():
    """sys.platform 'linux' maps to 'linux'."""
    assert _detect_platform() == "linux"


@patch("xmsconan.ci_tools.wheel_repair.sys.platform", "darwin")
def test_detect_macos():
    """sys.platform 'darwin' maps to 'macos'."""
    assert _detect_platform() == "macos"


@patch("xmsconan.ci_tools.wheel_repair.sys.platform", "win32")
def test_detect_windows():
    """sys.platform 'win32' maps to 'windows'."""
    assert _detect_platform() == "windows"


@patch("xmsconan.ci_tools.wheel_repair.sys.platform", "freebsd12")
def test_detect_unsupported_raises():
    """Unsupported platforms raise RuntimeError."""
    with pytest.raises(RuntimeError, match="Unsupported platform"):
        _detect_platform()


# --- _pip_install_cmd ---


@patch("xmsconan.ci_tools.wheel_repair.shutil.which", return_value="/usr/bin/uv")
def test_pip_install_cmd_uses_uv(mock_which):
    """Uses uv pip install when uv is available."""
    cmd = _pip_install_cmd("delocate")
    assert cmd[0] == "uv"
    assert "delocate" in cmd


@patch("xmsconan.ci_tools.wheel_repair.shutil.which", return_value=None)
def test_pip_install_cmd_falls_back_to_pip(mock_which):
    """Falls back to python -m pip when uv is not available."""
    cmd = _pip_install_cmd("delocate")
    assert cmd[1] == "-m"
    assert cmd[2] == "pip"
    assert "delocate" in cmd


# --- wheel_repair ---


@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/foo.whl"])
def test_linux_repair(mock_glob, mock_run, mock_rmtree, mock_move):
    """Linux uses auditwheel with LD_LIBRARY_PATH."""
    wheel_repair(wheel_dir="/tmp/wh", platform="linux")

    # pip install auditwheel patchelf
    pip_call = mock_run.call_args_list[0]
    assert "auditwheel" in pip_call[0][0]
    assert "patchelf" in pip_call[0][0]

    # auditwheel repair
    repair_call = mock_run.call_args_list[1]
    assert repair_call[0][0][0] == "auditwheel"
    assert Path(repair_call[1]["env"]["LD_LIBRARY_PATH"]) == Path(
        os.path.abspath("/tmp/wh/libs")
    )

    mock_rmtree.assert_called_once_with("/tmp/wh")
    mock_move.assert_called_once_with("/tmp/wh_repaired", "/tmp/wh")


@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["wh/foo.whl"])
def test_linux_repair_absolutizes_relative_wheel_dir(mock_glob, mock_run, mock_rmtree, mock_move):
    """A relative wheel_dir produces an absolute LD_LIBRARY_PATH."""
    wheel_repair(wheel_dir="wh", platform="linux")

    repair_call = mock_run.call_args_list[1]
    ld_library_path = repair_call[1]["env"]["LD_LIBRARY_PATH"]
    # The staged libs path is prepended; it must be absolute.
    expected = os.path.abspath(os.path.join("wh", "libs"))
    assert ld_library_path.startswith(expected), (
        f"expected LD_LIBRARY_PATH to start with absolute {expected!r}, "
        f"got {ld_library_path!r}"
    )
    assert os.path.isabs(expected)


@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/foo.whl"])
def test_linux_repair_preserves_existing_ld_library_path(mock_glob, mock_run, mock_rmtree, mock_move):
    """An inherited LD_LIBRARY_PATH is preserved (prepended, not overwritten)."""
    with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/preexisting/lib"}):
        wheel_repair(wheel_dir="/tmp/wh", platform="linux")

    repair_call = mock_run.call_args_list[1]
    ld_library_path = repair_call[1]["env"]["LD_LIBRARY_PATH"]
    expected_first = os.path.abspath("/tmp/wh/libs")
    # Staged dir comes first (prepended), so it wins linker resolution,
    # and the inherited path is preserved after it.
    assert ld_library_path == expected_first + ":" + "/preexisting/lib"


@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/bar.whl"])
def test_macos_repair(mock_glob, mock_run, mock_rmtree, mock_move):
    """Verify macOS uses delocate with DYLD_LIBRARY_PATH."""
    wheel_repair(wheel_dir="/tmp/wh", platform="macos")

    # pip install delocate
    pip_call = mock_run.call_args_list[0]
    assert "delocate" in pip_call[0][0]

    # delocate-wheel
    repair_call = mock_run.call_args_list[1]
    assert repair_call[0][0][0] == "delocate-wheel"
    assert Path(repair_call[1]["env"]["DYLD_LIBRARY_PATH"]) == Path(
        os.path.abspath("/tmp/wh/libs")
    )


@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/baz.whl"])
def test_windows_repair(mock_glob, mock_run, mock_rmtree, mock_move):
    """Windows uses delvewheel with --add-path."""
    wheel_repair(wheel_dir="/tmp/wh", platform="windows")

    # pip install delvewheel
    pip_call = mock_run.call_args_list[0]
    assert "delvewheel" in pip_call[0][0]

    # delvewheel repair
    repair_call = mock_run.call_args_list[1]
    cmd = repair_call[0][0]
    assert cmd[0] == "delvewheel"
    assert "--add-path" in cmd
    assert os.path.abspath(os.path.join("/tmp/wh", "libs")) in cmd
    assert "--namespace-pkg" in cmd
    assert "xms" in cmd


@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=[])
def test_no_wheels_raises(mock_glob):
    """Raises FileNotFoundError when no .whl files exist."""
    with pytest.raises(FileNotFoundError, match="No .whl files"):
        wheel_repair(wheel_dir="/tmp/empty", platform="linux")


@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/x.whl"])
def test_unknown_platform_raises(mock_glob):
    """Unknown platform string raises ValueError."""
    with pytest.raises(ValueError, match="Unknown platform"):
        wheel_repair(wheel_dir="/tmp/wh", platform="solaris")


@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch(
    "xmsconan.ci_tools.wheel_repair.glob.glob",
    return_value=["/tmp/wh/a.whl", "/tmp/wh/b.whl"],
)
def test_multiple_wheels_repaired(mock_glob, mock_run, mock_rmtree, mock_move):
    """All wheels in the directory are repaired."""
    wheel_repair(wheel_dir="/tmp/wh", platform="linux")

    # 1 pip install + 2 auditwheel repair calls
    assert mock_run.call_count == 3


@patch(
    "xmsconan.ci_tools.wheel_repair.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "auditwheel"),
)
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/x.whl"])
def test_propagates_called_process_error(mock_glob, mock_run):
    """Verify CalledProcessError from pip/repair propagates."""
    with pytest.raises(subprocess.CalledProcessError):
        wheel_repair(wheel_dir="/tmp/wh", platform="linux")
