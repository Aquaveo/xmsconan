"""Tests for the ``xmsconan format`` subcommand."""
import argparse
import logging
import os
import subprocess
import sys
from unittest import mock

import pytest

from xmsconan.format_tools import format_code as fmt

RUN = "xmsconan.format_tools.format_code.subprocess.run"
CLANG_BASE = "xmsconan.format_tools.format_code._clang_format_base"
YAPF_BASE = "xmsconan.format_tools.format_code._yapf_base"


def _ok(*_args, **_kwargs):
    """subprocess.run replacement that reports a clean (exit 0) run."""
    return subprocess.CompletedProcess(args=[], returncode=0)


def _needs_formatting(*_args, **_kwargs):
    """subprocess.run replacement that reports a needed change (exit non-zero)."""
    return subprocess.CompletedProcess(args=[], returncode=1)


@pytest.fixture
def source_tree(tmp_path):
    """Build a tree with C++, Python, and excluded directories; return its root."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cpp").write_text("int main(){}\n", encoding="utf-8")
    (tmp_path / "src" / "a.h").write_text("int f();\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x=1\n", encoding="utf-8")
    # Excluded: build output, virtualenv, and tests/fixtures.
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.cpp").write_text("int g(){}\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("y=2\n", encoding="utf-8")
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "stub.cpp").write_text("int s(){}\n", encoding="utf-8")
    # A "fixtures" dir NOT under tests/ is still formatted.
    (tmp_path / "data" / "fixtures").mkdir(parents=True)
    (tmp_path / "data" / "fixtures" / "keep.py").write_text("z=3\n", encoding="utf-8")
    return tmp_path


def _commands(mock_run):
    """Return the command list passed to each subprocess.run call."""
    return [call.args[0] for call in mock_run.call_args_list]


def _cmd_for(mock_run, tool):
    """Return the single command whose executable ends with ``tool``."""
    return next(c for c in _commands(mock_run) if c[0].endswith(tool))


class TestCollectFiles:
    """File discovery picks up source and prunes excluded directories."""

    def test_finds_source_and_skips_excluded_dirs(self, source_tree):
        """build/, .venv/, and tests/fixtures/ are pruned; other dirs are kept."""
        cpp, py = fmt._collect_files(
            [str(source_tree)], fmt.DEFAULT_EXCLUDE_DIRS, fmt.DEFAULT_REL_EXCLUDES
        )
        names = {os.path.basename(p) for p in cpp + py}
        assert names == {"a.cpp", "a.h", "mod.py", "keep.py"}

    def test_user_exclude_prunes_named_dir(self, source_tree):
        """A directory passed via --exclude is removed from the results."""
        cpp, py = fmt._collect_files(
            [str(source_tree)],
            fmt.DEFAULT_EXCLUDE_DIRS | {"src"},
            fmt.DEFAULT_REL_EXCLUDES,
        )
        names = {os.path.basename(p) for p in cpp + py}
        assert "a.cpp" not in names
        assert "mod.py" in names

    def test_subpath_root_still_prunes_tests_fixtures(self, source_tree):
        """tests/fixtures is pruned even when the walk root is `tests`, not the repo root."""
        cpp, py = fmt._collect_files(
            [str(source_tree / "tests")], fmt.DEFAULT_EXCLUDE_DIRS, fmt.DEFAULT_REL_EXCLUDES
        )
        assert cpp == []
        assert py == []

    def test_explicit_tests_fixtures_root_is_skipped(self, source_tree):
        """Handing tests/fixtures in directly as a path is skipped, not formatted."""
        cpp, py = fmt._collect_files(
            [str(source_tree / "tests" / "fixtures")],
            fmt.DEFAULT_EXCLUDE_DIRS,
            fmt.DEFAULT_REL_EXCLUDES,
        )
        assert cpp == []
        assert py == []


class TestApplyMode:
    """Apply mode invokes both formatters in place."""

    @mock.patch(RUN, side_effect=_ok)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_invokes_clang_format_and_yapf_in_place(self, _clang, _yapf, mock_run, source_tree):
        """clang-format -i and yapf -i (with the fixed style) are both run."""
        assert fmt.format_code([str(source_tree)]) == 0

        cpp_cmd = _cmd_for(mock_run, "clang-format")
        py_cmd = _cmd_for(mock_run, "yapf")
        assert "-i" in cpp_cmd
        assert "-i" in py_cmd
        assert f"--style={fmt.YAPF_STYLE}" in py_cmd
        assert "--no-local-style" in py_cmd

    @mock.patch(RUN, side_effect=_ok)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_cpp_only_skips_python(self, _clang, mock_yapf, mock_run, source_tree):
        """--cpp-only runs clang-format and never resolves or invokes yapf."""
        fmt.format_code([str(source_tree)], cpp_only=True)
        tools = [c[0] for c in _commands(mock_run)]
        assert any(t.endswith("clang-format") for t in tools)
        assert not any(t.endswith("yapf") for t in tools)
        mock_yapf.assert_not_called()

    @mock.patch(RUN, side_effect=_ok)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_py_only_skips_cpp(self, mock_clang, _yapf, mock_run, source_tree):
        """--py-only runs yapf and never resolves or invokes clang-format."""
        fmt.format_code([str(source_tree)], py_only=True)
        tools = [c[0] for c in _commands(mock_run)]
        assert any(t.endswith("yapf") for t in tools)
        assert not any(t.endswith("clang-format") for t in tools)
        mock_clang.assert_not_called()

    @mock.patch(RUN, side_effect=_needs_formatting)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_formatter_error_returns_one(self, _clang, _yapf, _run, source_tree):
        """In apply mode, a formatter exiting non-zero makes format_code return 1."""
        assert fmt.format_code([str(source_tree)]) == 1


class TestCheckMode:
    """--check verifies without modifying and reflects the diff in the exit code."""

    @mock.patch(RUN, side_effect=_needs_formatting)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_uses_dry_run_flags_and_returns_nonzero(self, _clang, _yapf, mock_run, source_tree):
        """Check mode uses --dry-run/--diff (no -i) and returns 1 on a diff."""
        assert fmt.format_code([str(source_tree)], check=True) == 1

        cpp_cmd = _cmd_for(mock_run, "clang-format")
        py_cmd = _cmd_for(mock_run, "yapf")
        assert "--dry-run" in cpp_cmd and "-Werror" in cpp_cmd
        assert "--diff" in py_cmd
        assert "-i" not in cpp_cmd
        assert "-i" not in py_cmd

    @mock.patch(RUN, side_effect=_ok)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_clean_tree_returns_zero(self, _clang, _yapf, _run, source_tree):
        """Check mode returns 0 when no file needs formatting."""
        assert fmt.format_code([str(source_tree)], check=True) == 0


class TestToolResolution:
    """Formatter binaries resolve without depending on PATH."""

    def test_yapf_base_uses_current_interpreter(self):
        """Resolve yapf to `python -m yapf` in the running interpreter."""
        assert fmt._yapf_base() == [sys.executable, "-m", "yapf"]

    def test_clang_format_base_resolves_existing_binary(self):
        """Resolve clang-format to an existing executable named clang-format."""
        base = fmt._clang_format_base()
        assert len(base) == 1
        assert os.path.exists(base[0])
        assert os.path.basename(base[0]).startswith("clang-format")

    @mock.patch("xmsconan.format_tools.format_code.shutil.which", return_value=None)
    @mock.patch("xmsconan.format_tools.format_code._clang_format_get_executable", None)
    def test_clang_format_missing_raises(self, _which):
        """A missing clang-format (no wheel, not on PATH) raises FormatToolError."""
        with pytest.raises(fmt.FormatToolError, match="clang-format"):
            fmt._clang_format_base()

    @mock.patch("xmsconan.format_tools.format_code.importlib.util.find_spec", return_value=None)
    def test_yapf_missing_raises(self, _find_spec):
        """A missing yapf raises FormatToolError."""
        with pytest.raises(fmt.FormatToolError, match="yapf"):
            fmt._yapf_base()


class TestEmptyTree:
    """A tree with no source files is a clean no-op."""

    def test_returns_zero_without_running_formatters(self, tmp_path):
        """No files found means exit 0 and no formatter resolution."""
        assert fmt.format_code([str(tmp_path)]) == 0


class TestInputValidation:
    """Bad input fails loud rather than silently succeeding."""

    def test_nonexistent_path_raises(self, tmp_path):
        """An explicit path that does not exist raises FormatToolError (not exit 0)."""
        missing = str(tmp_path / "does_not_exist")
        with pytest.raises(fmt.FormatToolError, match="does not exist"):
            fmt.format_code([missing])

    def test_cpp_only_and_py_only_raises(self, source_tree):
        """Requesting both cpp_only and py_only is a contradiction, not a no-op."""
        with pytest.raises(ValueError, match="mutually exclusive"):
            fmt.format_code([str(source_tree)], cpp_only=True, py_only=True)


class TestLogLevel:
    """Verbosity flags map to the expected logging level."""

    @pytest.mark.parametrize("quiet,verbose,expected", [
        (False, 0, logging.INFO),
        (False, 1, logging.DEBUG),
        (True, 0, logging.ERROR),
        (True, 1, logging.ERROR),  # --quiet wins over -v
    ])
    def test_log_level(self, quiet, verbose, expected):
        """_log_level picks ERROR/DEBUG/INFO from the quiet/verbose flags."""
        args = argparse.Namespace(quiet=quiet, verbose=verbose)
        assert fmt._log_level(args) == expected


class TestMain:
    """The CLI entry point maps the worker exit code onto SystemExit."""

    @mock.patch(RUN, side_effect=_needs_formatting)
    @mock.patch(YAPF_BASE, return_value=["yapf"])
    @mock.patch(CLANG_BASE, return_value=["clang-format"])
    def test_check_propagates_nonzero_exit(self, _clang, _yapf, _run, source_tree, monkeypatch):
        """`xmsconan format --check` exits non-zero when files need formatting."""
        monkeypatch.setattr(sys, "argv", ["xmsconan format", "--check", str(source_tree)])
        with pytest.raises(SystemExit) as exc:
            fmt.main()
        assert exc.value.code == 1
