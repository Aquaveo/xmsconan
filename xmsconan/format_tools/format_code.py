"""Format C++ (clang-format) and Python (yapf) source in a repository.

Usage::

    xmsconan format [PATHS ...] [--check] [--cpp-only | --py-only]
                    [--exclude DIR ...] [-v] [-q]

Walks each path (default: the current directory), collects C++ and Python
source files, and formats them in place. C++ uses the repository's own
``.clang-format`` (clang-format's normal auto-discovery); Python uses a fixed
yapf style (facebook base, 120-column) and intentionally ignores any
repo-local yapf config via ``--no-local-style``.
"""
import argparse
import fnmatch
import importlib.util
import logging
import os
import shutil
import subprocess
import sys

try:
    from clang_format import get_executable as _clang_format_get_executable
except ImportError:  # pragma: no cover - only hit when the clang-format wheel is absent
    _clang_format_get_executable = None

logger = logging.getLogger(__name__)

# Fixed yapf style — the canonical Python style is defined here, not discovered
# from the repo (``--no-local-style`` ignores .style.yapf / setup.cfg / pyproject).
YAPF_STYLE = "{based_on_style: facebook, column_limit: 120}"

# Extensions are matched case-insensitively; keep them as tuples for str.endswith.
CPP_EXTENSIONS = (".cpp", ".cxx", ".cc", ".h", ".hpp", ".hxx")
PY_EXTENSIONS = (".py",)

# Directory basenames pruned from the walk: universal build/vcs/cache dirs plus
# ``_package`` (generated). Mirrors the spirit of the repo's .flake8 excludes.
DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env",
    "__pycache__", ".eggs", ".tox", ".cache", ".mypy_cache", ".pytest_cache",
    "build", "dist", "node_modules",
    "_package",
})

# Directory basename glob patterns pruned from the walk (matched at any depth).
EXCLUDE_GLOBS = frozenset({"*.egg-info"})

# Directory path *suffixes* ("/"-separated components) pruned from the walk at any
# depth, and skipped if handed in directly as a path. Excluding ``tests/fixtures``
# keeps the command from reformatting test fixtures (e.g. xmsconan's own C++ stubs
# under tests/fixtures/coverage_stub, whose exact content the coverage tests assert)
# while still formatting real "fixtures" dirs that are not under ``tests/``.
DEFAULT_REL_EXCLUDES = frozenset({"tests/fixtures"})

# Cap files per subprocess invocation to stay under the OS argument-length limit.
_BATCH_SIZE = 200


class FormatToolError(RuntimeError):
    """A required formatter binary is missing or could not be run."""


def _log_level(args):
    """Return the logging level for the given CLI verbosity flags."""
    if args.quiet:
        return logging.ERROR
    if args.verbose > 0:
        return logging.DEBUG
    return logging.INFO


def _configure_logging(args):
    """Configure the logger from CLI verbosity flags."""
    logging.basicConfig(level=_log_level(args), format="%(levelname)s: %(message)s")


def _chunked(seq, size):
    """Yield successive ``size``-length chunks of ``seq``."""
    for start in range(0, len(seq), size):
        yield seq[start:start + size]


def _classify(path, cpp, py):
    """Add ``path`` to the ``cpp`` or ``py`` set based on its extension."""
    lowered = path.lower()
    if lowered.endswith(CPP_EXTENSIONS):
        cpp.add(path)
    elif lowered.endswith(PY_EXTENSIONS):
        py.add(path)


def _matches_rel_exclude(path, rel_excludes):
    """Return True if ``path`` ends with any rel-exclude's component sequence.

    Matching is by trailing path components (not a string suffix), so it fires at
    any depth and regardless of the walk root, but ``mytests/fixtures`` does not
    match ``tests/fixtures``.
    """
    parts = os.path.normpath(path).replace(os.sep, "/").split("/")
    for rel in rel_excludes:
        rel_parts = rel.split("/")
        if len(parts) >= len(rel_parts) and parts[-len(rel_parts):] == rel_parts:
            return True
    return False


def _is_excluded(name, dirpath, exclude_dirs, rel_excludes):
    """Return True if directory ``name`` under ``dirpath`` should be pruned."""
    if name in exclude_dirs or any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS):
        return True
    return _matches_rel_exclude(os.path.join(dirpath, name), rel_excludes)


def _collect_files(roots, exclude_dirs, rel_excludes):
    """Return ``(cpp_files, py_files)`` found under ``roots``, skipping excludes.

    Args:
        roots: Iterable of file or directory paths to search.
        exclude_dirs: Directory basenames to prune from the walk.
        rel_excludes: Directory path suffixes ("/"-separated components) to prune
            at any depth (also skipped when handed in directly as a root).

    Returns:
        A tuple of two sorted, de-duplicated lists: C++ files and Python files.

    Raises:
        FormatToolError: if an explicitly given ``root`` does not exist. Failing
            loud keeps a mistyped path from silently "succeeding" with exit 0.
    """
    cpp, py = set(), set()
    for root in roots:
        if os.path.isfile(root):
            _classify(root, cpp, py)
            continue
        if not os.path.exists(root):
            raise FormatToolError(f"path does not exist: {root}")
        if _matches_rel_exclude(root, rel_excludes):
            logger.info("Skipping excluded path: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if not _is_excluded(d, dirpath, exclude_dirs, rel_excludes)
            ]
            for name in filenames:
                _classify(os.path.join(dirpath, name), cpp, py)
    return sorted(cpp), sorted(py)


def _clang_format_base():
    """Return the base argv for clang-format, preferring the bundled wheel binary.

    The ``clang-format`` pip wheel installs a binary under its package data and
    exposes it via ``get_executable``. Using that path is PATH-independent and
    pins formatting to the version that shipped with xmsconan rather than any
    system clang-format that happens to be on PATH.
    """
    if _clang_format_get_executable is not None:
        executable = _clang_format_get_executable("clang-format")
    else:
        executable = shutil.which("clang-format")
    if not executable or not os.path.exists(executable):
        raise FormatToolError(
            "clang-format was not found. It ships as an xmsconan dependency; "
            "reinstall it with `pip install clang-format`."
        )
    return [executable]


def _yapf_base():
    """Return the base argv for yapf, run via the current interpreter.

    Invoking ``python -m yapf`` uses the yapf installed in the same environment
    as xmsconan without depending on the bin/Scripts directory being on PATH.
    """
    if importlib.util.find_spec("yapf") is None:
        raise FormatToolError(
            "yapf was not found. It ships as an xmsconan dependency; "
            "reinstall it with `pip install yapf`."
        )
    return [sys.executable, "-m", "yapf"]


def _run_in_batches(base_cmd, files):
    """Run ``base_cmd`` over ``files`` in batches; return the failing-batch count.

    A non-zero return code counts as a failure. In ``--check`` mode that means
    "files need formatting"; in apply mode it means the formatter errored.
    """
    failures = 0
    for batch in _chunked(files, _BATCH_SIZE):
        # stderr is intentionally inherited (not captured) so the formatter's
        # own per-file diagnostics reach the user.
        try:
            result = subprocess.run(base_cmd + batch)
        except OSError as exc:
            raise FormatToolError(
                f"failed to run formatter ({' '.join(base_cmd)}): {exc}"
            ) from exc
        if result.returncode != 0:
            failures += 1
    return failures


def _format_cpp(files, check):
    """Run clang-format over ``files``; return the failing-batch count."""
    base = _clang_format_base()
    flags = ["--dry-run", "-Werror"] if check else ["-i"]
    logger.info(
        "%s %d C++ file(s) with clang-format...",
        "Checking" if check else "Formatting", len(files),
    )
    return _run_in_batches(base + flags, files)


def _format_python(files, check):
    """Run yapf over ``files`` with the fixed style; return failing-batch count."""
    base = _yapf_base()
    style = [f"--style={YAPF_STYLE}", "--no-local-style"]
    flags = ["--diff"] if check else ["-i"]
    logger.info(
        "%s %d Python file(s) with yapf...",
        "Checking" if check else "Formatting", len(files),
    )
    return _run_in_batches(base + flags + style, files)


def format_code(paths, check=False, cpp_only=False, py_only=False, exclude=()):
    """Format (or check) C++ and Python source under ``paths``.

    Args:
        paths: Files or directories to process.
        check: If True, report files needing formatting without modifying them.
        cpp_only: If True, only process C++ files.
        py_only: If True, only process Python files.
        exclude: Extra directory basenames to prune from the walk.

    Returns:
        ``0`` if everything is clean/formatted, ``1`` otherwise.

    Raises:
        ValueError: if both ``cpp_only`` and ``py_only`` are set (the CLI makes
            them mutually exclusive; this guards direct callers).
    """
    if cpp_only and py_only:
        raise ValueError("cpp_only and py_only are mutually exclusive")
    exclude_dirs = DEFAULT_EXCLUDE_DIRS | set(exclude)
    cpp_files, py_files = _collect_files(paths, exclude_dirs, DEFAULT_REL_EXCLUDES)
    if py_only:
        cpp_files = []
    if cpp_only:
        py_files = []

    if not cpp_files and not py_files:
        logger.info("No C++ or Python files found to format.")
        return 0

    failures = 0
    if cpp_files:
        failures += _format_cpp(cpp_files, check)
    if py_files:
        failures += _format_python(py_files, check)

    if failures:
        if check:
            logger.error("Some files need formatting. Run `xmsconan format` to fix them.")
        else:
            logger.error("One or more formatters reported errors.")
        return 1
    logger.info("All files are correctly formatted." if check else "Formatting complete.")
    return 0


def main():
    """CLI entry point for ``xmsconan format`` (and ``xmsconan_format``)."""
    parser = argparse.ArgumentParser(
        description="Format C++ (clang-format) and Python (yapf) source in place.",
    )
    parser.add_argument(
        "paths", nargs="*", default=["."],
        help="Files or directories to format (default: current directory).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Report files that need formatting and exit non-zero without modifying them.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cpp-only", action="store_true", help="Only format C++ files.")
    group.add_argument("--py-only", action="store_true", help="Only format Python files.")
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="DIR",
        help="Directory basename to skip, matched at any depth "
             "(repeatable; added to the built-in excludes).",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase output verbosity (use -v for debug details).",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")
    args = parser.parse_args()
    _configure_logging(args)

    try:
        exit_code = format_code(
            paths=args.paths,
            check=args.check,
            cpp_only=args.cpp_only,
            py_only=args.py_only,
            exclude=args.exclude,
        )
    except FormatToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)
