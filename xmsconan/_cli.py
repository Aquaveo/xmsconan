"""What every ``xmsconan`` console script needs and none should carry a copy of.

Four helpers, each of which existed as several near-identical copies across
the entry points before this module:

* :func:`add_verbosity_args` and :func:`configure_logging` -- the ``-v`` /
  ``-q`` flags and the logging setup they drive.
* :func:`run_main` -- the error contract: one line on stderr for a failure,
  the traceback only under ``-v``, and a child process's own exit code when
  that is what failed.
* :func:`resolve_tool` -- where a console script such as ``conan`` lives.

The unified dispatcher lives in :mod:`xmsconan.cli`; this module is what the
subcommands it dispatches to share.
"""
import argparse
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Optional

from xmsconan.exit_codes import EXIT_ERROR, EXIT_OK, EXIT_USAGE

LOGGER = logging.getLogger(__name__)

#: The one log format every command prints in.
LOG_FORMAT = "%(levelname)s: %(message)s"


def add_verbosity_args(parser: argparse.ArgumentParser) -> None:
    """Add the ``-v/--verbose`` and ``-q/--quiet`` flags to *parser*.

    ``--verbose`` counts, so a parser that gains levels later does not change
    its flag; today anything above zero is DEBUG.

    Args:
        parser: The parser, or subparser, to add the flags to.
    """
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase output verbosity (use -v for debug details and tracebacks).",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")


def configure_logging(args: argparse.Namespace) -> None:
    """Configure the root logger from the flags :func:`add_verbosity_args` added.

    ``force=True`` always: under the ``xmsconan`` dispatcher, or in a process
    that imported a module which configured logging at import, the root
    logger already has a handler, and ``basicConfig`` without ``force`` would
    silently leave the earlier configuration in place -- which is how ``-v``
    used to do nothing for some commands and everything for others.

    The same flag replaces every root handler, pytest's log capture included,
    so a test that calls a ``main()`` and then reads ``caplog`` sees nothing:
    ``tests/conftest.py`` restores the root logger after each test for that
    reason.

    Args:
        args: Parsed arguments carrying ``quiet`` and ``verbose``.
    """
    if args.quiet:
        level = logging.ERROR
    elif args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)


def tracebacks_wanted() -> bool:
    """Return whether a failure should be reported with its traceback.

    That is the ``-v`` contract: the root logger is at DEBUG only when
    :func:`configure_logging` was handed ``--verbose``, so a command that
    failed before it parsed its arguments reports one line, like every
    other failure without ``-v``.
    """
    return logging.getLogger().isEnabledFor(logging.DEBUG)


class MissingToolError(RuntimeError):
    """A tool the command cannot run without is on neither ``PATH`` nor in this interpreter's environment.

    Raised by a caller of :func:`resolve_tool` for which None is fatal.
    :func:`run_main` reports it as ``EXIT_USAGE`` -- the machine cannot
    honour the request, the code argparse gives a bad flag -- rather than
    the ``EXIT_ERROR`` of a command that ran and failed.
    """


def run_main(body: Callable[[], Optional[int]]) -> int:
    """Run a console script's body under the shared error contract.

    Every ``main()`` that adopts this reports a failure the same way, which is
    the point: ``xmsconan gen`` printing ``Error: ...`` while ``xmsconan
    profiles`` logged a traceback for the same ``OSError`` meant the reader
    had to know which command they ran to know what a failure looked like.

    Args:
        body: The command. Returns its exit code, or None for success. It
            should call :func:`configure_logging` itself, once it has parsed
            its arguments; a failure before that point is reported without
            a traceback, since nothing asked for one.

    Returns:
        The process exit code. What *body* returned, ``EXIT_OK`` for None;
        a :class:`subprocess.CalledProcessError`'s own return code, so a
        ``conan`` or ``cmake`` that ran and failed is reported with the code
        it failed with (``EXIT_ERROR`` if a signal killed it, since a
        negative code is not one a caller can act on); ``EXIT_USAGE`` for a
        :class:`MissingToolError`, since a tool the machine lacks is the
        request failing, not the build; ``EXIT_ERROR`` for any other
        exception. Each is logged as its message -- and its traceback when
        :func:`tracebacks_wanted`. :class:`SystemExit`, which argparse
        raises for ``--help`` and a usage error, passes through untouched.
    """
    try:
        code = body()
    except MissingToolError as exc:
        LOGGER.error("%s", exc, exc_info=tracebacks_wanted())
        return EXIT_USAGE
    except subprocess.CalledProcessError as exc:
        # The program name, not the argv: a credential that reaches a command
        # line by mistake must not reach the log by contract. -v shows the
        # whole command in the traceback, which is what -v is for.
        LOGGER.error(
            "%s failed with exit status %s", _program_name(exc.cmd), exc.returncode,
            exc_info=tracebacks_wanted(),
        )
        return exc.returncode if exc.returncode > 0 else EXIT_ERROR
    except Exception as exc:  # the contract: every failure is one line, not a traceback
        LOGGER.error("%s", str(exc) or type(exc).__name__, exc_info=tracebacks_wanted())
        return EXIT_ERROR
    return EXIT_OK if code is None else code


def _program_name(cmd) -> str:
    """Return the executable a :class:`subprocess.CalledProcessError` ran, and nothing after it."""
    if isinstance(cmd, (list, tuple)):
        first = str(cmd[0]) if cmd else ""
    else:
        first, _, _ = str(cmd).strip().partition(" ")
    return os.path.basename(first) or "command"


def resolve_tool(tool: str, path: Optional[str] = None) -> Optional[str]:
    """Return the absolute path of the console script *tool*, or None.

    Looks on *path* first -- this process's ``PATH`` by default -- and then
    in the script directory of the running interpreter, which is where
    ``uv run`` and an activated venv put ``conan`` and ``cmake`` whether or
    not the shell that started this process could see them. The result is
    an absolute ``argv[0]`` on purpose: Windows resolves an unqualified
    program name against the *calling* process's ``PATH`` rather than the
    environment block handed to the child, so a ``PATH`` prepended for the
    child alone does not reach ``CreateProcess``.

    Args:
        tool: Console-script name, such as ``'conan'`` or ``'delvewheel'``.
        path: The search path to look on instead of ``os.environ['PATH']``.

    Returns:
        The absolute path, or None when *tool* is on neither. Callers decide
        what a missing tool means: a :class:`MissingToolError` for a build
        that cannot proceed without it, which :func:`run_main` reports as
        ``EXIT_USAGE``, or the bare name for a child that will fail with the
        same :class:`FileNotFoundError` either way.
    """
    found = shutil.which(tool, path=path)
    if found:
        return found
    scripts_dir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    candidate = scripts_dir / (f"{tool}.exe" if os.name == "nt" else tool)
    return str(candidate) if candidate.is_file() else None
