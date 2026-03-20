"""Tests for the unified xmsconan CLI dispatcher."""

import io
import sys
from unittest import mock

import pytest

from xmsconan.cli import COMMANDS, _print_usage, main


class TestCliDispatch:
    """Each subcommand dispatches to the correct module's main()."""

    @pytest.fixture(autouse=True)
    def _restore_argv(self):
        """Restore sys.argv after every test."""
        original = sys.argv[:]
        yield
        sys.argv = original

    @pytest.mark.parametrize("subcmd,module_path", [
        (name, mod) for name, (_, mod, _) in COMMANDS.items()
    ])
    def test_dispatch(self, subcmd, module_path):
        """Subcommand dispatches to its module's main() with rewritten argv."""
        sentinel = mock.MagicMock()
        fake_module = mock.MagicMock()
        fake_module.main = sentinel

        sys.argv = ["xmsconan", subcmd, "--help"]

        with mock.patch("importlib.import_module", return_value=fake_module) as imp:
            main()

        imp.assert_called_once_with(module_path)
        sentinel.assert_called_once()
        # argv[0] should be rewritten for argparse
        assert sys.argv[0] == f"xmsconan {subcmd}"
        assert sys.argv[1:] == ["--help"]


class TestCliHelp:
    """--help and no-args print usage."""

    @pytest.fixture(autouse=True)
    def _restore_argv(self):
        original = sys.argv[:]
        yield
        sys.argv = original

    def test_print_usage_lists_all_commands(self):
        """_print_usage outputs all subcommands with descriptions."""
        buf = io.StringIO()
        _print_usage(file=buf)
        output = buf.getvalue()
        assert "Available commands:" in output
        for name, (desc, _, _) in COMMANDS.items():
            assert name in output
            assert desc in output

    @pytest.mark.parametrize("args", [
        ["xmsconan"],
        ["xmsconan", "--help"],
        ["xmsconan", "-h"],
    ])
    def test_help_exits_zero(self, args):
        """No-args and --help/-h exit with code 0."""
        sys.argv = args
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0


class TestCliUnknown:
    """Unknown subcommands print an error to stderr."""

    @pytest.fixture(autouse=True)
    def _restore_argv(self):
        original = sys.argv[:]
        yield
        sys.argv = original

    def test_unknown_subcommand(self):
        """Unknown subcommand prints error + usage to stderr and exits 1."""
        sys.argv = ["xmsconan", "nope"]
        buf = io.StringIO()
        with pytest.raises(SystemExit) as exc_info, \
                mock.patch("xmsconan.cli._print_usage") as mock_usage:
            with mock.patch("sys.stderr", buf):
                main()
        assert exc_info.value.code == 1
        assert "unknown command 'nope'" in buf.getvalue()
        mock_usage.assert_called_once()
