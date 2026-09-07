"""Tests for :mod:`xmsconan.job_tools.cli`, the ``xmsconan job`` front end.

Most of what these subcommands do is fill in arguments a generated pipeline
used to render as flags. The assertions are therefore about *which* arguments
get filled in and from where: a value that quietly reverted to a library-
agnostic default would still run, and would run the wrong thing.
"""
import subprocess
from unittest.mock import patch

import pytest

from xmsconan.cli import COMMANDS
from xmsconan.exit_codes import EXIT_OK, EXIT_USAGE
from xmsconan.job_tools import cli, common
from .job_helpers import write_build_toml as _toml


class _Result:
    """Just enough of a CompletedProcess for a return-code check."""

    def __init__(self, returncode=0):
        self.returncode = returncode


# --- registration ---


def test_job_is_reachable_through_the_unified_cli():
    """A console script the dispatcher does not know about is unreachable.

    The generated pipelines call ``xmsconan job build``; nothing else in the
    repository would fail if the entry were dropped from COMMANDS.
    """
    assert COMMANDS["job"].module == "xmsconan.job_tools.cli"
    assert COMMANDS["job"].function == "main"


def test_a_kind_is_required():
    """``xmsconan job`` alone must not pick a job for the reader."""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args([])

    # Pinned: argparse's usage error is 2, and an unpinned SystemExit would
    # also accept a clean exit 0 from a --help that swallowed the argument.
    assert excinfo.value.code == 2


@pytest.mark.parametrize("kind", ["build", "test", "package", "lint"])
def test_every_documented_kind_parses(kind):
    """The four jobs a generated pipeline invokes."""
    assert cli.build_parser().parse_args([kind]).kind == kind


def test_build_flags_default_to_the_whole_matrix():
    """A bare ``job build`` is a workstation building everything.

    Every narrowing is opt-in, so a job that forgot a flag builds too much
    rather than silently building nothing.
    """
    args = cli.build_parser().parse_args(["build"])
    assert args.leg is None
    assert args.platform is None
    assert args.export is False
    assert args.release_skips_testing is False
    assert args.defer_cxx_tests is False
    assert args.toml_path == "build.toml"


def test_build_rejects_a_leg_outside_the_matrix_vocabulary():
    """A misspelled --leg is caught by argparse, not by an empty build."""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["build", "--leg", "pybnid"])

    assert excinfo.value.code == 2


def test_runner_args_pass_through_to_the_shards():
    """``-- --gtest_filter=Foo*`` reaches every shard."""
    args = cli.build_parser().parse_args(["test", "--", "--gtest_filter=Foo*"])
    assert args.runner_args == ["--gtest_filter=Foo*"]


# --- job test ---


def test_job_test_fills_its_arguments_in_from_build_toml(tmp_path):
    """Shard count, artifacts directory and xvfb are facts about the repository.

    The template rendered all three as flags, which is why a repository that
    turned sharding on had to regenerate its CI before the shards ran.
    """
    recorded = {}

    def _run(artifacts_dir, shards, **kwargs):
        recorded.update(artifacts_dir=artifacts_dir, shards=shards, **kwargs)
        return EXIT_OK

    body = 'library_name = "xmscore"\n[ci]\nsplit_tests = true\ntest_shards = 4\nxvfb = true\n'
    with patch.object(cli.xvfb, "wants_xvfb", return_value=True):
        with patch.object(cli.test_shards, "run", _run):
            result = cli.job_test(label="Release-testing", toml_path=_toml(tmp_path, body))

    assert result == EXIT_OK
    assert recorded["artifacts_dir"] == common.ARTIFACTS_DIR
    assert recorded["shards"] == 4
    assert recorded["label"] == "Release-testing"
    assert recorded["xvfb"] is True
    assert recorded["output"] == cli.test_shards.DEFAULT_REPORT_NAME


def test_job_test_asks_the_same_xvfb_predicate_the_build_asks(tmp_path):
    """``[ci].xvfb`` is a request, not an answer.

    Passed raw, a repository that asked for a display got every shard dying on
    ``Popen(["Xvfb", ...])`` on any host without one -- a macOS workstation, a
    Linux image built without it, or a runner that already has ``$DISPLAY``.
    ``wants_xvfb`` is the predicate the build and the coverage run both ask,
    and it warns and answers False rather than raising.
    """
    recorded = {}

    def _run(artifacts_dir, shards, **kwargs):
        recorded.update(kwargs)
        return EXIT_OK

    body = 'library_name = "xmscore"\n[ci]\nxvfb = true\n'
    with patch.object(cli.xvfb, "wants_xvfb", return_value=False) as wants:
        with patch.object(cli.test_shards, "run", _run):
            cli.job_test(label="Release-testing", toml_path=_toml(tmp_path, body))

    assert recorded["xvfb"] is False
    assert wants.call_args.args[0].ci.xvfb is True


def test_job_test_runs_one_shard_when_sharding_is_off(tmp_path):
    """``test_shards = 0`` means unsharded, not zero shards.

    Passing 0 through would ask the runner for no processes at all.
    """
    recorded = {}

    def _run(artifacts_dir, shards, **kwargs):
        recorded["shards"] = shards
        return EXIT_OK

    with patch.object(cli.test_shards, "run", _run):
        cli.job_test(toml_path=_toml(tmp_path))

    assert recorded["shards"] == 1


def test_job_test_keeps_the_shard_timeout_default(tmp_path):
    """A job that names no timeout gets the tool's, not None."""
    recorded = {}

    def _run(artifacts_dir, shards, **kwargs):
        recorded.update(kwargs)
        return EXIT_OK

    with patch.object(cli.test_shards, "run", _run):
        cli.job_test(toml_path=_toml(tmp_path))

    assert recorded["timeout"] == cli.test_shards.DEFAULT_SHARD_TIMEOUT


# --- job package ---


def test_job_package_repairs_the_fixed_wheel_directory():
    """The layout is fixed, so the job and the artifacts: entry cannot drift."""
    recorded = {}

    def _repair(wheel_dir=None):
        recorded["wheel_dir"] = wheel_dir

    with patch.object(cli.wheel_repair, "wheel_repair", _repair):
        assert cli.job_package() == EXIT_OK

    assert recorded["wheel_dir"] == common.WHEEL_DIR


# --- job lint ---


def test_job_lint_generates_before_it_lints(tmp_path):
    """There is nothing to lint in a fresh clone until the files are written.

    ``_package/`` is generated and gitignored, so the lint job's first act has
    always been a ``gen``.
    """
    events = []

    def _generate(toml_file_path=None, version=None):
        events.append(("generate", version))
        return EXIT_OK

    def _runner(command, check=False):
        events.append(("flake8", command[-1]))
        return _Result(0)

    with patch.object(cli, "generate_build_files", _generate), \
            patch.object(cli, "resolve_tool", lambda tool: "/usr/bin/flake8"):
        result = cli.job_lint(toml_path=_toml(tmp_path), runner=_runner)

    assert result == EXIT_OK
    assert events == [("generate", cli.FALLBACK_VERSION), ("flake8", cli.LINT_TARGET)]


def test_job_lint_pins_the_version_so_a_tag_lints_the_same(tmp_path):
    """Linting is not publishing, and nothing here reaches a package name.

    A resolved version would make a tag pipeline's lint job differ from the
    branch pipeline's that just passed.
    """
    versions = []

    with patch.object(cli, "generate_build_files",
                      lambda toml_file_path=None, version=None: versions.append(version)), \
            patch.object(cli, "resolve_tool", lambda tool: "/usr/bin/flake8"):
        cli.job_lint(toml_path=_toml(tmp_path), runner=lambda command, check=False: _Result(0))

    assert versions == [cli.FALLBACK_VERSION]


def test_job_lint_stops_when_the_files_cannot_be_generated(tmp_path):
    """Linting the previous run's output would report on the wrong tree."""
    linted = []

    with patch.object(cli, "generate_build_files",
                      lambda toml_file_path=None, version=None: 1):
        result = cli.job_lint(toml_path=_toml(tmp_path),
                              runner=lambda command, check=False: linted.append(command))

    assert result == 1
    assert linted == []


def test_job_lint_reports_a_missing_flake8_as_a_usage_error(tmp_path):
    """``resolve_tool`` returning None would otherwise be an obscure failure.

    ``[None, "_package"]`` reaches subprocess as a TypeError about a NoneType
    argument, which says nothing about the tool the image is missing.
    """
    with patch.object(cli, "generate_build_files",
                      lambda toml_file_path=None, version=None: EXIT_OK), \
            patch.object(cli, "resolve_tool", lambda tool: None):
        with pytest.raises(cli.MissingToolError, match="flake8 is not installed"):
            cli.job_lint(toml_path=_toml(tmp_path))


def test_a_missing_flake8_exits_usage_not_error(tmp_path, monkeypatch):
    """The machine cannot honour the request; the build did not fail.

    ``run_main`` draws that distinction, and this is the path that relies on
    it -- an image without flake8 is a CI configuration problem, not a lint
    failure someone should go looking for in the diff.
    """
    monkeypatch.setattr("sys.argv", ["xmsconan job", "lint", "--toml", _toml(tmp_path)])
    with patch.object(cli, "generate_build_files",
                      lambda toml_file_path=None, version=None: EXIT_OK), \
            patch.object(cli, "resolve_tool", lambda tool: None):
        assert cli.main() == EXIT_USAGE


def test_job_lint_returns_what_flake8_returned(tmp_path):
    """The job fails when the lint fails, and with the linter's own code."""
    with patch.object(cli, "generate_build_files",
                      lambda toml_file_path=None, version=None: EXIT_OK), \
            patch.object(cli, "resolve_tool", lambda tool: "/usr/bin/flake8"):
        result = cli.job_lint(toml_path=_toml(tmp_path),
                              runner=lambda command, check=False: _Result(1))

    assert result == 1


def test_job_lint_does_not_raise_on_a_lint_failure(tmp_path):
    """``check=False``: a nonzero flake8 is a result, not an exception.

    ``run_main`` reports a CalledProcessError with its own return code, so
    raising would still exit 1 -- but it would log a traceback over the
    flake8 output the reader actually needs.
    """
    checks = []

    def _runner(command, check=False):
        checks.append(check)
        return _Result(1)

    with patch.object(cli, "generate_build_files",
                      lambda toml_file_path=None, version=None: EXIT_OK), \
            patch.object(cli, "resolve_tool", lambda tool: "/usr/bin/flake8"):
        cli.job_lint(toml_path=_toml(tmp_path), runner=_runner)

    assert checks == [False]


# --- dispatch ---


def test_main_dispatches_build_with_the_flags_it_parsed(tmp_path, monkeypatch):
    """Every ``job build`` flag has to reach :func:`job_build`.

    A flag parsed and dropped is the failure mode with no symptom: the job
    runs, passes, and quietly builds a matrix nobody asked for.
    """
    monkeypatch.setattr("sys.argv", [
        "xmsconan job", "build", "--leg", "pybind", "--export",
        "--release-skips-testing", "--build-missing", "--defer-cxx-tests",
        "--version", "1.2.3", "--toml", _toml(tmp_path),
    ])

    # autospec, so that renaming a job_build parameter fails here instead of
    # being absorbed by a fake that accepts any keyword at all.
    with patch.object(cli, "job_build", autospec=True) as job_build:
        job_build.return_value = EXIT_OK
        assert cli.main() == EXIT_OK

    recorded = job_build.call_args.kwargs
    assert recorded["leg"] == "pybind"
    assert recorded["export"] is True
    assert recorded["release_skips_testing"] is True
    assert recorded["build_missing"] is True
    assert recorded["defer_cxx_tests"] is True
    assert recorded["version"] == "1.2.3"


def test_main_dispatches_package(monkeypatch):
    """No flags at all: everything it needs is the fixed layout."""
    monkeypatch.setattr("sys.argv", ["xmsconan job", "package"])
    with patch.object(cli.wheel_repair, "wheel_repair", lambda wheel_dir=None: None):
        assert cli.main() == EXIT_OK


def test_main_reports_a_failed_tool_with_its_own_exit_code(tmp_path, monkeypatch):
    """A conan or cmake that ran and failed keeps the code it failed with."""
    monkeypatch.setattr("sys.argv", ["xmsconan job", "build", "--toml", _toml(tmp_path)])
    failure = subprocess.CalledProcessError(3, ["conan"])
    with patch.object(cli, "job_build", autospec=True, side_effect=failure):
        assert cli.main() == 3


def test_main_dispatches_test_with_its_label_and_timeout(tmp_path, monkeypatch):
    """The label names which staged configuration this job runs."""
    recorded = {}
    monkeypatch.setattr("sys.argv", [
        "xmsconan job", "test", "--label", "Debug-testing", "--timeout", "60",
        "--toml", _toml(tmp_path), "--", "--gtest_filter=Foo*",
    ])

    def _job_test(**kwargs):
        recorded.update(kwargs)
        return EXIT_OK

    with patch.object(cli, "job_test", _job_test):
        assert cli.main() == EXIT_OK

    assert recorded["label"] == "Debug-testing"
    assert recorded["timeout"] == 60
    assert recorded["runner_args"] == ["--gtest_filter=Foo*"]
