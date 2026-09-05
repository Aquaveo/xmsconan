"""Tests for the helpers every console script shares, and the exit-code vocabulary."""
import argparse
import logging
import os
import subprocess
import sys
from unittest import mock

import pytest

from xmsconan import exit_codes
from xmsconan._cli import (
    add_verbosity_args, configure_logging, MissingToolError, resolve_tool, run_main, tracebacks_wanted,
)


def _parse(*argv):
    """Parse *argv* with a parser that has only the verbosity flags."""
    parser = argparse.ArgumentParser()
    add_verbosity_args(parser)
    return parser.parse_args(argv)


def _only(items):
    """The one element of *items*; the failure names what was found instead."""
    assert len(items) == 1, items
    return items[0]


# --- exit codes ---


def test_exit_codes_are_distinct_and_ordered():
    """One number, one meaning, across every command -- and the numbers are already out there.

    The generated GitLab job forgives exactly ``EXIT_GATE_FAILED``. The
    template renders the constant, so a freshly generated pipeline would
    follow a change here -- but every consumer pipeline generated so far, and
    USAGE §11.6, hold the literal 3; and argparse exits 2 on a usage error
    whatever ``EXIT_USAGE`` says. Changing a value is a coordinated release,
    not an edit, and this pin is what makes that visible.
    """
    codes = [
        exit_codes.EXIT_OK, exit_codes.EXIT_ERROR, exit_codes.EXIT_USAGE,
        exit_codes.EXIT_GATE_FAILED, exit_codes.EXIT_NOTHING_BUILT,
    ]
    assert codes == [0, 1, 2, 3, 4]


# --- verbosity flags and configure_logging ---


@pytest.mark.parametrize("argv,level", [
    ((), logging.INFO),
    (("-v",), logging.DEBUG),
    (("-vv",), logging.DEBUG),
    (("-q",), logging.ERROR),
], ids=["default", "verbose", "very-verbose", "quiet"])
def test_configure_logging_sets_the_root_level(argv, level):
    """The flags map onto the root logger's level; -v also enables tracebacks."""
    configure_logging(_parse(*argv))

    assert logging.getLogger().level == level
    assert tracebacks_wanted() is (level == logging.DEBUG)


def test_verbose_counts():
    """``-vv`` is 2, so a parser that gains levels later keeps its flag."""
    assert _parse("-vv").verbose == 2
    assert _parse().quiet is False


def test_configure_logging_replaces_an_earlier_configuration():
    """Under the dispatcher the root logger is already configured; -v must still win.

    The setup uses ``force=True`` too: without it ``basicConfig`` is a no-op
    whenever the root logger already has a handler, which under pytest it
    always does, and the test would pass without an earlier configuration
    to replace.
    """
    logging.basicConfig(level=logging.ERROR, force=True)
    earlier = _only([h for h in logging.getLogger().handlers if h.__class__ is logging.StreamHandler])

    configure_logging(_parse("-v"))

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert earlier not in root.handlers


# --- run_main ---


@pytest.mark.parametrize("returned,expected", [
    (None, exit_codes.EXIT_OK),
    (0, exit_codes.EXIT_OK),
    (3, 3),
], ids=["none", "zero", "three"])
def test_run_main_returns_what_the_body_returned(returned, expected):
    """None is success; an int is the exit code as given."""
    assert run_main(lambda: returned) == expected


def test_run_main_reports_an_exception_in_one_line(caplog):
    """Without -v a failure is its message, at ERROR, with no traceback attached."""
    def body():
        raise ValueError("no library_name in build.toml")

    assert run_main(body) == exit_codes.EXIT_ERROR

    record = _only([r for r in caplog.records if r.levelno == logging.ERROR])
    assert record.getMessage() == "no library_name in build.toml"
    assert not record.exc_info
    assert "Traceback" not in caplog.text


def test_run_main_attaches_the_traceback_under_verbose(caplog):
    """With the root logger at DEBUG -- what -v sets -- the same failure carries its traceback."""
    caplog.set_level(logging.DEBUG)

    def body():
        raise ValueError("no library_name in build.toml")

    assert run_main(body) == exit_codes.EXIT_ERROR

    record = _only([r for r in caplog.records if r.levelno == logging.ERROR])
    assert record.exc_info is not None
    assert "ValueError: no library_name in build.toml" in caplog.text


def test_run_main_names_an_exception_that_has_no_message(caplog):
    """A bare ``RuntimeError()`` is reported by its class, not as an empty line."""
    def body():
        raise RuntimeError()

    assert run_main(body) == exit_codes.EXIT_ERROR
    assert "RuntimeError" in caplog.text


def test_run_main_reports_a_missing_tool_as_a_usage_error(caplog):
    """A tool the machine lacks is exit 2 -- the request cannot be honoured -- as one line naming it."""
    def body():
        raise MissingToolError("Tool 'conan' not found. Please ensure it is installed.")

    assert run_main(body) == exit_codes.EXIT_USAGE

    record = _only([r for r in caplog.records if r.levelno == logging.ERROR])
    assert record.getMessage().startswith("Tool 'conan' not found")
    assert not record.exc_info


@pytest.mark.parametrize("cmd,returncode,expected", [
    (["/opt/conan2/bin/conan", "create", ".", "--password", "s3cret"], 6, 6),
    ("cmake --build build --config Release", 2, 2),
    (["conan", "create", "."], -11, exit_codes.EXIT_ERROR),
], ids=["list-argv", "string-argv", "killed-by-signal"])
def test_run_main_propagates_a_child_process_code(cmd, returncode, expected, caplog):
    """A child that ran and failed is reported with its own code and its program name only.

    The rest of the argv stays out of the log: a credential that reaches a
    command line by mistake must not reach the log by contract. A negative
    code (a signal) is not one a caller can act on, so it becomes EXIT_ERROR.
    """
    def body():
        raise subprocess.CalledProcessError(returncode, cmd)

    assert run_main(body) == expected

    program = "conan" if "conan" in str(cmd) else "cmake"
    assert f"{program} failed with exit status {returncode}" in caplog.text
    assert "create" not in caplog.text
    assert "--build" not in caplog.text
    assert "s3cret" not in caplog.text


def test_run_main_lets_system_exit_through():
    """The SystemExit argparse raises for --help and usage errors passes through untouched."""
    def body():
        raise SystemExit(2)

    with pytest.raises(SystemExit) as excinfo:
        run_main(body)

    assert excinfo.value.code == 2


# --- resolve_tool ---


def test_resolve_tool_prefers_the_search_path():
    """A tool on PATH is what shutil.which found, searched on the path given."""
    with mock.patch("xmsconan._cli.shutil.which", return_value="/usr/bin/conan") as which:
        assert resolve_tool("conan", path="/usr/bin") == "/usr/bin/conan"

    which.assert_called_once_with("conan", path="/usr/bin")


def test_resolve_tool_falls_back_to_the_interpreter_scripts(tmp_path, monkeypatch):
    """Off PATH, the running interpreter's script directory is searched next."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    scripts = tmp_path / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir()
    exe = scripts / ("conan.exe" if os.name == "nt" else "conan")
    exe.write_text("", encoding="utf-8")

    with mock.patch("xmsconan._cli.shutil.which", return_value=None):
        assert resolve_tool("conan") == str(exe)


def test_resolve_tool_returns_none_when_the_tool_is_nowhere(tmp_path, monkeypatch):
    """The caller decides what a missing tool means, so nothing is raised here."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))

    with mock.patch("xmsconan._cli.shutil.which", return_value=None):
        assert resolve_tool("conan") is None
