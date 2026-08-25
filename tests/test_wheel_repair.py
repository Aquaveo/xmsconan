"""Tests for ci_tools.wheel_repair."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.wheel_repair import (
    _detect_platform,
    _pip_install_cmd,
    _repair_env,
    wheel_repair,
)

SYSCONFIG_GET_PATH = "xmsconan.ci_tools.wheel_repair.sysconfig.get_path"
SCRIPT_DIR = os.path.join(os.sep, "tool-venv", "bin")

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


@patch("xmsconan.ci_tools.wheel_repair._resolve_tool", side_effect=lambda tool, env: tool)
@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/foo.whl"])
def test_linux_repair(mock_glob, mock_run, mock_rmtree, mock_move, mock_resolve):
    """Linux uses auditwheel with LD_LIBRARY_PATH."""
    wheel_repair(wheel_dir="/tmp/wh", platform="linux")

    # pip install auditwheel patchelf
    pip_call = mock_run.call_args_list[0]
    assert "auditwheel" in pip_call[0][0]
    assert "patchelf" in pip_call[0][0]

    # auditwheel repair
    repair_call = mock_run.call_args_list[1]
    assert repair_call[0][0][0] == "auditwheel"
    assert Path(repair_call[1]["env"]["LD_LIBRARY_PATH"]) == Path(os.path.abspath("/tmp/wh/libs"))

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


@patch("xmsconan.ci_tools.wheel_repair._resolve_tool", side_effect=lambda tool, env: tool)
@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/bar.whl"])
def test_macos_repair(mock_glob, mock_run, mock_rmtree, mock_move, mock_resolve):
    """Verify macOS uses delocate with DYLD_LIBRARY_PATH."""
    wheel_repair(wheel_dir="/tmp/wh", platform="macos")

    # pip install delocate
    pip_call = mock_run.call_args_list[0]
    assert "delocate" in pip_call[0][0]

    # delocate-wheel
    repair_call = mock_run.call_args_list[1]
    assert repair_call[0][0][0] == "delocate-wheel"
    assert Path(repair_call[1]["env"]["DYLD_LIBRARY_PATH"]) == Path(os.path.abspath("/tmp/wh/libs"))


@patch("xmsconan.ci_tools.wheel_repair._resolve_tool", side_effect=lambda tool, env: tool)
@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run")
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/baz.whl"])
def test_windows_repair(mock_glob, mock_run, mock_rmtree, mock_move, mock_resolve):
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


# --- wheel_repair against a real filesystem ---
#
# Every test above mocks rmtree and move, so none of them exercises the
# directory swap that ends wheel_repair() -- which is exactly where the
# trailing-separator bug lived. These drive the real filesystem and stub only
# the external repair tool.


def _stub_repair_tool(cmd, *args, **kwargs):
    """Stand in for pip and auditwheel, writing what auditwheel would write."""
    if "-w" in cmd:
        out_dir = Path(cmd[cmd.index("-w") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        source = Path(cmd[cmd.index("repair") + 1])
        (out_dir / f"{source.stem}-manylinux.whl").write_bytes(b"repaired")
    return subprocess.CompletedProcess(cmd, 0)


def _make_wheelhouse(tmp_path):
    """Create a wheelhouse holding one built wheel and a staged libs/ dir."""
    wheel_dir = tmp_path / "wheelhouse"
    (wheel_dir / "libs").mkdir(parents=True)
    (wheel_dir / "foo.whl").write_bytes(b"built")
    return wheel_dir


@pytest.mark.parametrize("trailing", ["", os.sep])
def test_repaired_wheels_replace_the_originals(tmp_path, trailing):
    """The repaired wheel ends up in wheel_dir and the originals are gone.

    With a trailing separator the derived ``{wheel_dir}_repaired`` used to land
    *inside* wheel_dir, so rmtree deleted the repaired wheels along with the
    built ones and move() raised on the path it had just destroyed -- losing
    both copies of a wheel that can take an hour to rebuild.
    """
    wheel_dir = _make_wheelhouse(tmp_path)

    with patch("xmsconan.ci_tools.wheel_repair.subprocess.run", side_effect=_stub_repair_tool):
        wheel_repair(wheel_dir=f"{wheel_dir}{trailing}", platform="linux")

    assert wheel_dir.is_dir()
    assert [p.name for p in wheel_dir.glob("*.whl")] == ["foo-manylinux.whl"]
    assert not (tmp_path / "wheelhouse_repaired").exists()
    assert not (wheel_dir / "_repaired").exists()


# --- _repair_env ---


def test_repair_env_preserves_inherited_path():
    """The inherited PATH is kept, after the prepended script directory."""
    with patch(SYSCONFIG_GET_PATH, return_value=SCRIPT_DIR):
        with patch.dict(os.environ, {"PATH": "/inherited/bin"}):
            env = _repair_env()

    assert env["PATH"] == os.pathsep.join([SCRIPT_DIR, "/inherited/bin"])


def test_repair_env_without_inherited_path_has_no_empty_entry():
    """An absent PATH leaves no trailing empty entry, which would resolve to the cwd."""
    with patch(SYSCONFIG_GET_PATH, return_value=SCRIPT_DIR):
        with patch.dict(os.environ, {}, clear=True):
            env = _repair_env()

    assert env["PATH"] == SCRIPT_DIR


def test_repair_env_applies_overrides():
    """Keyword overrides land in the returned environment."""
    env = _repair_env(DYLD_LIBRARY_PATH="/libs")

    assert env["DYLD_LIBRARY_PATH"] == "/libs"


# --- repair tool is reachable on PATH ---


@pytest.mark.parametrize(
    "platform,tool",
    [("linux", "auditwheel"), ("macos", "delocate-wheel"), ("windows", "delvewheel")],
)
@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run", autospec=True)
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/foo.whl"])
def test_repair_invokes_the_resolved_tool_path(
    mock_glob, mock_run, mock_rmtree, mock_move, platform, tool
):
    """Every platform invokes its repair tool by resolved absolute path.

    Regression guard. The repair tool is installed into the interpreter running
    this module, so a bare-name invocation only resolves when that interpreter's
    script directory is on PATH -- which it is not under a ``uv tool`` install.
    Asserting the *environment* handed to the child would not catch the failure:
    Windows resolves an unqualified name against the calling process's PATH and
    never consults that environment block, so the lookup would still fail while
    the assertion passed. Pinning the resolved argv[0] is what closes the bug on
    all three platforms.
    """
    resolved = os.path.join(SCRIPT_DIR, tool)
    with patch(SYSCONFIG_GET_PATH, return_value=SCRIPT_DIR), \
            patch("xmsconan.ci_tools.wheel_repair.shutil.which") as mock_which:
        mock_which.side_effect = lambda name, path=None: resolved if name == tool else None
        wheel_repair(wheel_dir="/tmp/wh", platform=platform)

    repair_call = next(c for c in mock_run.call_args_list if c.args[0][0] != sys.executable)
    assert repair_call.args[0][0] == resolved, "repair tool must be invoked by absolute path"

    # The lookup that produced it searched the interpreter's script directory.
    which_call = next(c for c in mock_which.call_args_list if c.args[0] == tool)
    assert which_call.kwargs["path"].split(os.pathsep)[0] == SCRIPT_DIR


@pytest.mark.parametrize(
    "platform,loader_var",
    [("linux", "LD_LIBRARY_PATH"), ("macos", "DYLD_LIBRARY_PATH")],
)
@patch("xmsconan.ci_tools.wheel_repair.shutil.move")
@patch("xmsconan.ci_tools.wheel_repair.shutil.rmtree")
@patch("xmsconan.ci_tools.wheel_repair.subprocess.run", autospec=True)
@patch("xmsconan.ci_tools.wheel_repair.glob.glob", return_value=["/tmp/wh/foo.whl"])
def test_repair_keeps_loader_path_alongside_script_dir(
    mock_glob, mock_run, mock_rmtree, mock_move, platform, loader_var
):
    """Prepending the script dir does not displace the platform's loader path."""
    with patch(SYSCONFIG_GET_PATH, return_value=SCRIPT_DIR), \
            patch("xmsconan.ci_tools.wheel_repair.shutil.which", return_value=None):
        wheel_repair(wheel_dir="/tmp/wh", platform=platform)

    repair_call = next(c for c in mock_run.call_args_list if c.args[0][0] != sys.executable)
    env = repair_call.kwargs["env"]
    assert env["PATH"].split(os.pathsep)[0] == SCRIPT_DIR
    assert env[loader_var] == os.path.abspath("/tmp/wh/libs")
