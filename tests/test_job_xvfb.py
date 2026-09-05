"""Tests for :mod:`xmsconan.job_tools.xvfb`, the one Xvfb implementation.

The cases here were three separate suites -- one beside ``publish``, one beside
``test_shards`` and one beside ``coverage_generator`` -- because the code was.
They test one module now, which is the point: the predicate that decides
whether a display is wanted has one answer for all three mechanisms, and it is
asserted once.
"""
import os
import socket
import struct
import subprocess
import threading
from unittest.mock import patch

import pytest

from xmsconan.build_toml import read_build_toml
from xmsconan.job_tools import xvfb
from .utils import patch_env


def _config(tmp_path, body='library_name = "xmscore"\n[ci]\nxvfb = true\n'):
    """A parsed build.toml with the given body."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(body, encoding="utf-8")
    return read_build_toml(toml_file)


# --- wants_xvfb ---


@patch("xmsconan.job_tools.xvfb.shutil.which", return_value="/usr/bin/xvfb-run")
@patch_env(clear=True)
def test_wants_xvfb_true_on_linux(mock_which, tmp_path):
    """True on Linux when ci.xvfb=true, no DISPLAY, and xvfb-run is installed."""
    assert xvfb.wants_xvfb(_config(tmp_path), platform="linux") is True


def test_wants_xvfb_false_on_macos(tmp_path):
    """False off Linux: macOS and Windows have a window server of their own."""
    assert xvfb.wants_xvfb(_config(tmp_path), platform="darwin") is False


@patch_env({"DISPLAY": ":0"})
def test_wants_xvfb_false_when_display_set(tmp_path):
    """False when a display already exists; a workstation needs nothing."""
    assert xvfb.wants_xvfb(_config(tmp_path), platform="linux") is False


@patch_env(clear=True)
def test_wants_xvfb_false_when_xvfb_not_configured(tmp_path):
    """False when the repository never asked for a display."""
    config = _config(tmp_path, 'library_name = "xmscore"\n')
    assert xvfb.wants_xvfb(config, platform="linux") is False


@patch("xmsconan.job_tools.xvfb.shutil.which", return_value=None)
@patch_env(clear=True)
def test_wants_xvfb_warns_when_the_tool_is_missing(mock_which, tmp_path, caplog):
    """A repository that asked for a display on an image without one is told.

    The other three negatives are the machine or the repository saying no. This
    one is a misconfiguration whose only other symptom is a VTK test
    segfaulting with no mention of X.
    """
    with caplog.at_level("WARNING"):
        assert xvfb.wants_xvfb(_config(tmp_path), platform="linux") is False
    assert "xvfb-run not found" in caplog.text


# --- run_prefix / under_xvfb ---


def test_run_prefix_asks_for_the_screen_the_templates_asked_for():
    """The geometry is the one every caller used, spelled once."""
    assert xvfb.run_prefix() == ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24"]


@patch("xmsconan.job_tools.xvfb.shutil.which", return_value="/usr/bin/xvfb-run")
@patch_env(clear=True)
def test_under_xvfb_prefixes_only_when_wanted(mock_which, tmp_path):
    """The same argv, wrapped or not, decided by the one predicate."""
    config = _config(tmp_path)
    wrapped = xvfb.under_xvfb(["python", "build.py"], config, platform="linux")
    bare = xvfb.under_xvfb(["python", "build.py"], config, platform="darwin")
    assert wrapped == [*xvfb.run_prefix(), "python", "build.py"]
    assert bare == ["python", "build.py"]


# --- reexec_under_xvfb ---


def test_reexec_runs_the_module_not_argv_zero():
    """Re-exec through ``-m <module>``.

    The ``xmsconan`` dispatcher rewrites argv[0] to the literal "xmsconan
    coverage", so handing it to the interpreter ran ``python "xmsconan
    coverage"`` and died with "can't open file".
    """
    recorded = {}

    def _exec(program, command, env):
        recorded.update(program=program, command=command, env=env)

    with patch("xmsconan.job_tools.xvfb.shutil.which", return_value="/usr/bin/xvfb-run"):
        xvfb.reexec_under_xvfb("xmsconan.coverage_tools.coverage_generator",
                               environ={}, argv=["--phase", "measure"], exec_fn=_exec)

    assert recorded["program"] == "/usr/bin/xvfb-run"
    assert recorded["command"][:4] == xvfb.run_prefix()
    assert "-m" in recorded["command"]
    module_index = recorded["command"].index("-m")
    assert recorded["command"][module_index + 1] == "xmsconan.coverage_tools.coverage_generator"
    assert recorded["command"][module_index + 2:] == ["--phase", "measure"]
    assert recorded["env"][xvfb.REEXEC_VARIABLE] == "1"


def test_reexec_does_not_recurse():
    """The re-entered process sees the flag and runs the work itself."""
    calls = []
    xvfb.reexec_under_xvfb("some.module", environ={xvfb.REEXEC_VARIABLE: "1"},
                           argv=[], exec_fn=lambda *a: calls.append(a))
    assert calls == []


def test_reexec_without_xvfb_run_continues_without_a_display(caplog):
    """A missing tool is a warning, not a failure.

    The caller continues and its tests fail with an X error, which names the
    problem; refusing to run would name it too, but only after the caller had
    already decided a display was optional.
    """
    calls = []
    with patch("xmsconan.job_tools.xvfb.shutil.which", return_value=None), \
            caplog.at_level("WARNING"):
        xvfb.reexec_under_xvfb("some.module", environ={}, argv=[],
                               exec_fn=lambda *a: calls.append(a))
    assert calls == []
    assert "xvfb-run is not on PATH" in caplog.text


# --- the server ---


def test_ensure_socket_dir_creates_a_sticky_world_writable_dir(tmp_path):
    """A fresh container has no /tmp/.X11-unix; make one the way X would."""
    target = tmp_path / "x11-sockets"

    with patch.object(xvfb, "SOCKET_DIR", str(target)):
        xvfb.ensure_socket_dir()
        # A second call must be a no-op, not an error.
        xvfb.ensure_socket_dir()

    assert target.is_dir()
    if os.name != "nt":
        assert os.stat(target).st_mode & 0o1777 == 0o1777


def test_start_server_reports_a_server_that_dies_early(tmp_path):
    """An Xvfb that exits before serving surfaces its own log, not a hang."""
    class _Dead:
        returncode = 1

        def poll(self):
            return 1

    with patch.object(xvfb.subprocess, "Popen", lambda *a, **k: _Dead()):
        _, error = xvfb.start_server(99, tmp_path / "xvfb.log")

    assert "exited with code 1" in error


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX display sockets")
def test_server_answers_requires_a_success_reply():
    """Only the server's success byte proves the display can serve.

    A failure byte means a server that is up but refusing; no listener means
    no server at all. Neither may start the runner: the socket file existing
    is exactly the false signal that let the GLX crash through.
    """
    os.makedirs("/tmp/.X11-unix", exist_ok=True)

    def _serve(display_number, reply):
        path = f"/tmp/.X11-unix/X{display_number}"
        if os.path.exists(path):
            os.unlink(path)
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)

        def _answer():
            connection, _ = server.accept()
            connection.recv(64)
            connection.sendall(reply)
            connection.close()
            server.close()

        threading.Thread(target=_answer, daemon=True).start()
        return path

    accepting = _serve(47901, struct.pack("<B", 1))
    refusing = _serve(47902, struct.pack("<B", 0))
    try:
        assert xvfb.server_answers(47901) is True
        assert xvfb.server_answers(47902) is False
        assert xvfb.server_answers(47903) is False
    finally:
        for path in (accepting, refusing):
            if os.path.exists(path):
                os.unlink(path)


class _FakeServer:
    """An Xvfb that is running until it is asked to stop."""

    def __init__(self):
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):  # pragma: no cover - the timeout path has its own test
        self.killed = True


def test_stop_server_escalates_to_kill(tmp_path):
    """A server that ignores SIGTERM is killed rather than waited on forever."""
    class _Stubborn(_FakeServer):
        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("Xvfb", timeout)
            return 0

        def kill(self):
            self.killed = True

    server = _Stubborn()
    xvfb.stop_server(server)
    assert server.killed


# --- display() ---


def test_display_is_a_no_op_when_no_display_is_wanted(tmp_path):
    """Callers wrap unconditionally, so the negative case must cost nothing."""
    environ = {}
    with xvfb.display(_config(tmp_path), environ=environ, platform="darwin") as server:
        assert server is None
    assert environ == {}


@patch("xmsconan.job_tools.xvfb.shutil.which", return_value="/usr/bin/xvfb-run")
def test_display_exports_display_and_stops_the_server(mock_which, tmp_path):
    """Children inherit DISPLAY from the environment; it goes away afterwards."""
    server = _FakeServer()
    environ = {}
    seen = {}

    with patch.object(xvfb, "ensure_socket_dir", lambda: None), \
            patch.object(xvfb, "start_server", lambda number, path: (server, None)):
        with xvfb.display(_config(tmp_path), log_dir=tmp_path, environ=environ,
                          platform="linux"):
            seen["display"] = environ["DISPLAY"]

    assert seen["display"] == f":{xvfb.BASE_DISPLAY}"
    assert "DISPLAY" not in environ
    assert server.terminated


@patch("xmsconan.job_tools.xvfb.shutil.which", return_value="/usr/bin/xvfb-run")
def test_display_raises_when_the_server_never_answers(mock_which, tmp_path):
    """The build must not run against a display that cannot serve it.

    It would fail anyway, somewhere less legible -- a GL client that limps past
    a failed XOpenDisplay dies later on GLXMakeCurrent, with nothing in the
    message about X.
    """
    server = _FakeServer()

    with patch.object(xvfb, "ensure_socket_dir", lambda: None), \
            patch.object(xvfb, "start_server", lambda number, path: (server, "did not answer")):
        with pytest.raises(RuntimeError, match="did not answer"):
            with xvfb.display(_config(tmp_path), log_dir=tmp_path, environ={},
                              platform="linux"):
                pass  # pragma: no cover - the context manager raises on entry

    assert server.terminated


def test_ensure_socket_dir_tolerates_a_directory_it_does_not_own(tmp_path):
    """On a host with a real X server the directory is root's and correct.

    Insisting on the chmod there would fail a run that needed nothing done.
    """
    target = tmp_path / "x11-sockets"
    target.mkdir()

    with patch.object(xvfb, "SOCKET_DIR", str(target)), \
            patch.object(xvfb.os, "chmod", side_effect=OSError("not permitted")):
        xvfb.ensure_socket_dir()

    assert target.is_dir()


def test_stop_server_leaves_an_exited_server_alone():
    """A server that already exited has nothing to terminate."""
    class _Exited(_FakeServer):
        def poll(self):
            return 0

    server = _Exited()
    xvfb.stop_server(server)
    assert server.terminated is False
