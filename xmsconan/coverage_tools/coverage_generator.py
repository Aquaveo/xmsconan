"""Unified coverage runner for xmsconan libraries.

Runs two independent builds under XMS_COVERAGE=1:

  * ``testing=True, pybind=False, Debug`` — drives CxxTest under
    ``--coverage``. gcovr reads the .gcda set from this build folder for C++
    line coverage. Debug is required: gcov instruments an unoptimized build.
  * ``pybind=True, testing=False, Release`` (pinned ``python_version``) —
    drives ``pytest-cov`` against the wheel. The pytest-cov JSON/XML/HTML
    artifacts are copied up out of this build folder for Python coverage,
    and it is instrumented too: the binding layer is C++ that only a Python
    test can reach, so its .gcda is merged into the C++ report. Release
    rather than Debug because the CMake block appends ``-O0 -g`` after
    CMake's ``-O3`` -- the build is unoptimized either way -- while Debug
    cost every dependency a Debug+pybind binary, the one combination the
    xms libraries do not publish, and on Windows would demand a debug
    interpreter.

gcovr reads both build folders. They have different roots, so each is read
separately into a JSON tracefile and the two are combined with
``--add-tracefile``; hit counts sum per line, so a line the CxxTest runner
never reaches counts as covered when a Python test reaches it.

Combining the two flags in one Conan config was a fragile carve-out that
existed nowhere else in the matrix; this split lets the coverage configs
match shapes the rest of CI already builds, so a regression in
``testing_sources`` linkage or pybind ABI can no longer take Coverage
offline. Compares actuals to ``[coverage]`` thresholds from build.toml and
exits non-zero on regression.
"""
# 1. Standard python modules
import argparse
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import NamedTuple, Optional

# 3. Aquaveo modules
from xmsconan._cli import add_verbosity_args, configure_logging
from xmsconan.build_toml import read_build_toml
from xmsconan.exit_codes import (
    EXIT_ERROR as _EXIT_ERROR,
    EXIT_GATE_FAILED as _EXIT_GATE_FAILED,
    EXIT_OK as _EXIT_OK,
)
from xmsconan.generator_tools.ci_file_generator import (
    _coverage_context,
    _resolve_coverage_python_version,
)
from xmsconan.package_tools.packager import COVERAGE_PYBIND_BUILD_TYPE


LOGGER = logging.getLogger(__name__)

# Process exit codes.
#
# The coverage *gate* failing and the coverage *tool* failing are different
# events and the generated CI needs to tell them apart: under [ci].split_tests
# the tests already gate the pipeline from their own job, so a threshold miss
# here is advisory -- but a crash, neither build leg leaving a package, or
# gcovr falling over is not, and collapsing both onto exit 1 is what let a Coverage
# stage report green while producing no report at all. GitLab's
# `allow_failure: exit_codes:` keys off the number, so the gate gets one of its
# own and everything else stays fatal.
#
# 3 rather than 2: argparse exits 2 on a usage error, and a mistyped invocation
# must not read as an advisory coverage miss. The values live in
# xmsconan.exit_codes, shared with every other command, and are re-exported
# here because this module is where the coverage contract is documented.
EXIT_OK = _EXIT_OK
EXIT_ERROR = _EXIT_ERROR
EXIT_GATE_FAILED = _EXIT_GATE_FAILED

#: The two halves of a coverage run, and the default that does both in one
#: process.
#:
#: The split exists so the expensive half can move earlier in the pipeline. The
#: instrumented builds are a second full compile of the library -- they carry
#: their own package_id, so no production build can be reused for them -- and
#: running that in a late Coverage stage serialized it behind the entire
#: pipeline. ``collect`` is a Build-stage job that overlaps the production
#: build; ``report`` is what remains, and it only reads JSON.
#:
#: Nothing but rendered artifacts and small JSON crosses between them, which is
#: what makes the split viable at all: both instrumented builds stay inside the
#: collect job, so gcovr reads its ``.gcda`` and renders its HTML beside the
#: sources that produced them. A split placed anywhere later would have to move
#: the conan build folder between containers and relocate the paths compiled
#: into the ``.gcno``.
PHASE_ALL = "all"
PHASE_COLLECT = "collect"
PHASE_MEASURE = "measure"
PHASE_REPORT = "report"
COVERAGE_PHASES = (PHASE_ALL, PHASE_COLLECT, PHASE_MEASURE, PHASE_REPORT)

#: The two instrumented legs, as ``--leg`` selects them. ``collect`` runs both
#: in one job; ``measure`` runs exactly one, which is what lets the generated
#: GitLab pipeline compile them as two concurrent build jobs instead of one
#: job doing both in sequence.
LEG_CPP = "cpp"
LEG_PYTHON = "python"
COVERAGE_LEGS = (LEG_CPP, LEG_PYTHON)

#: What ``collect`` leaves for ``report``: the facts the report phase cannot
#: re-derive from the summary files alone. Thresholds are deliberately NOT in
#: here -- the report phase reads them from build.toml, so a threshold edit
#: takes effect without re-running the builds.
COVERAGE_STATUS_FILE = "coverage-status.json"

_XVFB_REEXEC_FLAG = "XMSCONAN_COVERAGE_XVFB_REEXEC"


def _opt_is_truthy(value) -> bool:
    """Return True if a Conan option value represents truthy regardless of repr.

    Conan does not contractually stringify booleans to ``"True"``; depending on
    the serializer path the value may come through as a real ``bool`` or as a
    case-variant string. Treat all of those as the same answer.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def _reexec_under_xvfb():
    """Re-exec the current process under xvfb-run.

    Sets _XVFB_REEXEC_FLAG in the child environment so the re-entered process
    does not recurse. No-op if xvfb-run is not on PATH; the caller logs and
    continues without a display, which surfaces test failures with a clear
    error rather than silently masking them.
    """
    if os.environ.get(_XVFB_REEXEC_FLAG):
        return
    xvfb_run = shutil.which("xvfb-run")
    if not xvfb_run:
        LOGGER.warning("ci.xvfb is true but xvfb-run is not on PATH; running without a display.")
        return
    env = os.environ.copy()
    env[_XVFB_REEXEC_FLAG] = "1"
    # Re-exec through `-m <module>`, not through sys.argv[0]: the `xmsconan`
    # dispatcher rewrites argv[0] to the literal "xmsconan coverage"
    # (cli.py), so passing it to the interpreter ran
    # `python "xmsconan coverage"` and died with "can't open file". The module
    # has a __main__ guard for exactly this entry.
    cmd = [
        xvfb_run, "-a", "-s", "-screen 0 1280x1024x24",
        sys.executable, "-m", "xmsconan.coverage_tools.coverage_generator",
        *sys.argv[1:],
    ]
    LOGGER.info("Re-execing under xvfb-run: %s", " ".join(cmd))
    os.execvpe(xvfb_run, cmd, env)


def _run(cmd, env=None, cwd=None):
    """Run a subprocess, streaming output and raising on non-zero exit."""
    LOGGER.info("$ %s", " ".join(cmd) if isinstance(cmd, list) else cmd)
    subprocess.run(cmd, env=env, cwd=cwd, check=True)


#: Seconds a captured coverage leg may run before it is killed and its partial
#: output surrendered. Longer than any healthy coverage build -- the point is
#: not to bound the work but to beat the CI job's own timeout, which would
#: otherwise discard the whole buffer along with the container.
DEFAULT_LEG_TIMEOUT = 7200


class _BuildLeg(NamedTuple):
    """One of the two builds a coverage run drives.

    Attributes:
        name: Human label used in the banners and the timing summary.
        filter_json: The ``build.py --filter`` argument that selects this
            leg's single configuration out of the generated matrix.
    """

    name: str
    filter_json: str


class _LegResult(NamedTuple):
    """What one coverage build did.

    Attributes:
        name: The leg's label.
        returncode: ``build.py``'s exit status.
        duration: Wall-clock seconds the leg took.
        output: The leg's combined stdout and stderr. Empty when the leg
            streamed straight through instead of being captured.
    """

    name: str
    returncode: int
    duration: float
    output: str

    @property
    def passed(self) -> bool:
        """Whether this leg succeeded."""
        return self.returncode == 0


def _build_command(leg: _BuildLeg, version: str) -> list:
    """The ``build.py`` argv for one coverage leg."""
    return [sys.executable, "build.py", "--version", version, "--filter", leg.filter_json]


def _run_leg_captured(leg: _BuildLeg, version: str, env, cwd,
                      timeout: int = DEFAULT_LEG_TIMEOUT) -> _LegResult:
    """Run one leg to completion, holding its output rather than streaming it.

    Every failure is returned, never raised. This runs on a worker thread, and
    an exception there surfaces from ``future.result()`` in the driver -- which
    would abort the whole coverage run at the point it is specifically supposed
    to carry on and report whatever the other leg produced.

    The timeout is what keeps a hung build debuggable. Streaming showed an
    operator where a build stopped; capturing shows nothing until the process
    ends, so a hang that runs out the CI job's own clock takes the entire
    buffered log down with it. Timing out first surrenders the partial output.
    """
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _build_command(leg, version), env=env, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return _LegResult(
            leg.name, 1, time.monotonic() - started,
            f"{output}\n{leg.name} build timed out after {timeout}s; "
            f"the output above is everything it produced first.",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _LegResult(leg.name, 1, time.monotonic() - started,
                          f"{leg.name} build could not run: {exc}")
    return _LegResult(leg.name, completed.returncode, time.monotonic() - started,
                      completed.stdout or "")


def _print_leg_output(result: _LegResult) -> None:
    """Replay one leg's captured output under a banner.

    Whole buffers, one leg at a time. Two concurrent `conan create`s writing to
    the same log interleave per line, and a compiler diagnostic split from the
    file name above it is not a diagnostic any more.
    """
    rule = "=" * 72
    status = "OK" if result.passed else f"FAILED (exit {result.returncode})"
    print(rule)
    print(f"{result.name} coverage build -- {status} in {result.duration:.1f}s")
    print(rule)
    print(result.output.rstrip("\n") if result.output else "(no output)")
    print("")


def _run_legs_concurrently(legs, version: str, env, cwd) -> list:
    """Run every leg at once, printing each one's output as it finishes."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(legs)) as executor:
        futures = [executor.submit(_run_leg_captured, leg, version, env, cwd)
                   for leg in legs]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            _print_leg_output(result)
            results.append(result)
    order = {leg.name: index for index, leg in enumerate(legs)}
    return sorted(results, key=lambda r: order[r.name])


def _run_legs_sequentially(legs, version: str, env, cwd) -> list:
    """Run the legs one after another, streaming each straight through.

    Output is not captured here: with one build running there is nothing to
    interleave with, and streaming is what lets an operator watch a long
    compile rather than wait for a wall of text at the end.
    """
    results = []
    for leg in legs:
        started = time.monotonic()
        LOGGER.info("Running the %s coverage build", leg.name)
        try:
            completed = subprocess.run(_build_command(leg, version), env=env, cwd=cwd)
            returncode = completed.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            # Same contract as the concurrent path: one leg failing must not
            # stop the other from running or the report from being written.
            LOGGER.error("%s build could not run: %s", leg.name, exc)
            returncode = 1
        results.append(_LegResult(leg.name, returncode,
                                  time.monotonic() - started, ""))
    return results


def _run_coverage_builds(legs, version: str, env, cwd, parallel: bool) -> bool:
    """Drive the coverage builds and report how each one fared.

    Args:
        legs: The :class:`_BuildLeg` list to build.
        version: Version passed to ``build.py``.
        env: Environment for the builds.
        cwd: Directory to build in.
        parallel: Overlap the legs. They are independent ``conan create``s
            that share only the report step, but they also share the local
            Conan cache and the machine's cores, so overlapping is opt-in --
            see ``[coverage].parallel`` (§5.7).

    Returns:
        True when any leg failed. A failure is reported, not raised: the
        surviving artifacts still produce a partial report, and the caller
        gates on this flag at the end.
    """
    started = time.monotonic()
    runner = _run_legs_concurrently if parallel else _run_legs_sequentially
    results = runner(legs, version, env, cwd)
    elapsed = time.monotonic() - started

    LOGGER.info("Coverage builds (%s):", "concurrent" if parallel else "sequential")
    for result in results:
        LOGGER.info("  %s: %s in %.1fs", result.name,
                    "OK" if result.passed else f"exit {result.returncode}",
                    result.duration)
    LOGGER.info("  wall clock %.1fs", elapsed)

    failed = [result for result in results if not result.passed]
    for result in failed:
        LOGGER.error(
            "build.py exited %s during the %s coverage build; continuing so "
            "gcovr and artifact collection still run and partial coverage "
            "remains available.", result.returncode, result.name,
        )
    return bool(failed)


def _find_coverage_package(
    library_name: str, *, kind: str, python_version: Optional[str] = None,
) -> tuple[str, str]:
    """Locate a coverage-build package in the local Conan cache.

    ``xmsconan_coverage`` drives two builds and discovers each here:

      * ``kind="testing"`` matches ``testing=True``, ``pybind=False``,
        ``build_type=Debug``. ``python_version`` is irrelevant — the
        testing build does not depend on a Python ABI — and is ignored
        if passed.
      * ``kind="pybind"`` matches ``testing=False``, ``pybind=True``,
        ``build_type=Release``, and the pinned ``python_version``. Passing
        ``python_version=None`` for this kind is a programming error and
        raises ``ValueError``; the multi-version fan-out would otherwise
        non-deterministically pick whichever ABI finished last (#65).

    The build type is per kind rather than one value for both, and must stay
    in step with the filters ``run_coverage`` builds with: a matcher looking
    for a build nothing produced reports "did the build complete?" for a build
    that completed fine.

    Returns (exact_ref, package_id) for the newest matching revision.
    """
    if kind == "testing":
        want_pybind = False
        want_testing = True
        want_build_type = "Debug"
        match_python_version = None
    elif kind == "pybind":
        if python_version is None:
            raise ValueError(
                "kind='pybind' requires python_version; otherwise the matcher "
                "would pick whichever ABI finished last (issue #65)."
            )
        want_pybind = True
        want_testing = False
        want_build_type = "Release"
        match_python_version = python_version
    else:
        raise ValueError(
            f"kind must be 'testing' or 'pybind', got {kind!r}"
        )

    result = subprocess.run(
        ["conan", "list", f"{library_name}/*:*", "--format=json"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    candidates = []  # (timestamp, exact_ref, package_id)
    for exact_ref, cache in data.get("Local Cache", {}).items():
        for rev in cache.get("revisions", {}).values():
            ts = rev.get("timestamp", 0)
            for pid, pinfo in rev.get("packages", {}).items():
                info = pinfo.get("info", {})
                opts = info.get("options", {})
                settings = info.get("settings", {})
                if _opt_is_truthy(opts.get("pybind")) != want_pybind:
                    continue
                if _opt_is_truthy(opts.get("testing")) != want_testing:
                    continue
                # Instrumented and production builds coexist in the cache as
                # sibling package_ids under one recipe revision (the recipe's
                # coverage option). An uninstrumented sibling — whose
                # package_id drops the option entirely, so the key is absent
                # here — has no .gcda and must never be handed to gcovr.
                if not _opt_is_truthy(opts.get("coverage")):
                    continue
                if settings.get("build_type") != want_build_type:
                    continue
                if match_python_version is not None and \
                        opts.get("python_version") != match_python_version:
                    continue
                candidates.append((ts, exact_ref, pid))
    if not candidates:
        desc = (
            "coverage=True, testing=True, pybind=False, Debug" if kind == "testing"
            else f"coverage=True, pybind=True, testing=False, Release, "
                 f"python_version={match_python_version}"
        )
        raise RuntimeError(
            f"No {desc} package found for {library_name} in the local Conan "
            f"cache. Did the {kind} coverage build complete?"
        )
    candidates.sort(reverse=True)
    _, exact_ref, pid = candidates[0]
    return exact_ref, pid


# Folders that conan 2's `cache path` requires a recipe reference (no
# `:pid`) for — source, export, and export_source are shared across every
# package built from the same recipe revision, so a package id is
# meaningless there and conan rejects ref:pid with
# ``'--folder source' requires a recipe reference`` (see issue #66).
_RECIPE_SCOPED_FOLDERS = frozenset({"source", "export", "export_source"})


def _find_pytest_cov_artifact(build_folder: Path, name: str, kind: Optional[str] = None):
    """Locate a pytest-cov artifact (file or directory) inside the conan build folder.

    The recipe's ``run_python_tests`` writes coverage artifacts into a
    layout-specific subdirectory (e.g. ``<build_folder>/build/Debug/``),
    not the conan-managed build root that ``conan cache path
    --folder=build`` returns. Walking with ``rglob`` is robust against
    recipe layout changes and multi-build-type folders (see issue #71).

    ``kind`` filters matches by filesystem type — ``"file"`` keeps only
    regular files, ``"dir"`` keeps only directories, ``None`` (the
    default) keeps both. This guards against a real
    ``coverage-html-py/`` directory being shadowed by a same-named
    stale *file* (which would silently fall through the call-site
    ``is_dir()`` check), and vice versa. Without ``kind``, the helper
    behaves exactly as a name-based ``rglob`` does.

    Returns the matching path. ``None`` if the artifact isn't present
    (legitimate when pytest-cov never ran — e.g., ``pybind=False``).

    When more than one match exists (e.g. stale leftover from a prior
    build type) the newest by ``st_mtime`` is returned and a warning is
    logged so the operator can clean up.
    """
    matches = list(build_folder.rglob(name))
    if kind == "file":
        matches = [m for m in matches if m.is_file()]
    elif kind == "dir":
        matches = [m for m in matches if m.is_dir()]
    elif kind is not None:
        raise ValueError(
            f"kind must be 'file', 'dir', or None; got {kind!r}"
        )
    if not matches:
        LOGGER.warning(
            "No %s found under %s; the artifact it feeds will be missing.",
            name, build_folder,
        )
        return None
    matches.sort()
    if len(matches) > 1:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        LOGGER.warning(
            "Multiple %s entries under %s; using newest by mtime: %s. All candidates: %s",
            name, build_folder, matches[0],
            [str(m) for m in matches],
        )
    return matches[0]


def _conan_cache_path(ref_with_pid: str, folder: str) -> Path:
    """Resolve ``conan cache path <ref-or-ref:pid> --folder=<folder>``.

    Conan 2 requires a recipe reference (no package id) for ``source``,
    ``export``, and ``export_source`` folders, and a package reference
    (with ``:pid``) for ``build`` and the default package folder. Callers
    can pass either shape — this strips the pid when the folder is
    recipe-scoped so the same helper works for both kinds of lookup.
    """
    if folder in _RECIPE_SCOPED_FOLDERS:
        ref = ref_with_pid.split(":", 1)[0]
    else:
        ref = ref_with_pid
    result = subprocess.run(
        ["conan", "cache", "path", ref, f"--folder={folder}"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


_REGEX_METACHARS = frozenset(r".*+?[]{}|\\$")


def _locate_coverage_build(
    library_name: str, *, kind: str, python_version: Optional[str] = None,
) -> Optional[Path]:
    """Return one leg's conan build folder, or None when the leg left none.

    ``_run_coverage_builds`` deliberately reports a failed leg rather than
    raising, "so gcovr and artifact collection still run and partial coverage
    remains available". That promise is only real if the lookup which follows
    tolerates a missing package: a leg that failed registers nothing in the
    cache, so ``_find_coverage_package`` raises ``RuntimeError``, and calling it
    unguarded turned every single-leg failure into a crashed job with no report
    at all -- discarding the surviving leg's coverage in exactly the case the
    fallback exists to cover.

    A bad ``kind`` still raises: that is a programming error in the caller, not
    a build that did not finish.
    """
    try:
        exact_ref, package_id = _find_coverage_package(
            library_name, kind=kind, python_version=python_version,
        )
    except RuntimeError as exc:
        LOGGER.error("No %s coverage package to report on: %s", kind, exc)
        return None
    try:
        return _conan_cache_path(f"{exact_ref}:{package_id}", "build")
    except (subprocess.CalledProcessError, OSError) as exc:
        LOGGER.error(
            "Found the %s coverage package (%s:%s) but could not resolve its "
            "build folder: %s", kind, exact_ref, package_id, exc,
        )
        return None


def _is_simple_relative_filter_pattern(pattern: str) -> bool:
    """True if ``pattern`` looks like a bare relative path segment.

    The default ``[coverage].filters`` value is ``"<library_name>/"`` — a
    plain path segment. Such patterns get re-emitted with an absolute-
    path-anchored copy by ``_resolve_gcovr_filters`` so gcovr can match
    them regardless of whether it normalizes paths to relative-to-root
    or compares against absolute paths internally.

    A pattern is considered "simple relative" when it has no leading
    anchor (``/``, ``^``, ``(``) and no regex metacharacters. Users who
    write real regexes get their patterns through unchanged.
    """
    if not pattern:
        return False
    if pattern.startswith(("/", "^", "(")):
        return False
    if any(c in pattern for c in _REGEX_METACHARS):
        return False
    return True


def _resolve_gcovr_filters(filters, build_folder: Path):
    """Build the list of ``--filter`` values to pass to gcovr.

    For each entry in ``filters`` that looks like a simple relative
    path segment (per ``_is_simple_relative_filter_pattern``), this
    function emits *two* entries: the original (which matches against
    relative-to-root paths, gcovr's default normalization), AND an
    absolute-path-anchored form (``re.escape(build_folder) + "/" +
    pattern``) which matches the absolute paths embedded in ``.gcno``
    files. ``conan``'s ``cmake_layout()`` copies sources *into* the
    build folder before compilation, so ``.gcno`` paths point under
    ``build_folder``, not under the recipe's source folder — anchoring
    against ``source_folder`` would never match (see issue causing the
    "all coverage data is filtered out" diagnostic even when ``.gcno``
    and ``.gcda`` files are present).

    Multiple ``--filter`` entries are OR'd by gcovr (a file is kept if
    any filter matches), so emitting both forms is purely additive — it
    can only widen matches, never narrow them — and guards against
    subtle differences in how gcovr resolves source paths across
    versions. Regex-looking patterns and patterns that already start
    with an anchor pass through unchanged.
    """
    # ``as_posix()`` so the anchored form uses forward slashes regardless
    # of the host OS — coverage runs in a Linux container, and the
    # ``.gcno`` source paths there are forward-slash even when the
    # helper executes on a Windows dev machine.
    result = []
    abs_root = build_folder.as_posix().rstrip("/")
    for pattern in filters:
        result.append(pattern)
        if _is_simple_relative_filter_pattern(pattern):
            result.append(re.escape(abs_root) + "/" + pattern)
    return result


def _assert_gcovr_collected_data(summary_path: Path, collections) -> None:
    """Raise if gcovr's merged summary reports zero instrumented lines.

    Args:
        summary_path: The merged ``--json-summary`` gcovr wrote.
        collections: ``(build_folder, resolved_filters)`` pairs, one per folder
            read. Kept as pairs because a filter list is anchored to a single
            folder's absolute paths, so the diagnostic can only be acted on if
            each folder is shown with the filters used against it.

    ``line_total == 0`` means gcovr ran successfully but produced an
    empty report. Since the summary is merged, that means *no* folder
    contributed -- a single folder dropping out is reported by
    :func:`_warn_if_tracefile_empty` instead. Three common causes, in
    descending likelihood:

      1. The binary was compiled without ``--coverage``. The recipe's
         ``coverage=True`` option drives ``-DXMS_COVERAGE`` into CMake;
         if the ``add_compile_options(--coverage -O0 -g)`` block was
         skipped, no ``.gcno`` files exist for gcovr to read.
      2. ``[coverage].filters`` / ``[coverage].excludes`` filtered out
         every source file. The defaults are conservative, but a custom
         filter that doesn't match the absolute paths in the ``.gcno``
         files would silently exclude everything.
      3. The build folder is genuinely empty (e.g., the package was
         pulled from a binary-only cache rather than rebuilt).

    A legitimate 0% (no tests covered any lines) reports
    ``line_total > 0`` with ``line_percent == 0.0`` — that is NOT
    raised here; it's a real measurement and the threshold check
    handles it.

    Schema drift (``line_total`` missing entirely) is tolerated: leave
    the diagnostic to ``_cpp_percent_from_summary``, which already
    surfaces missing keys with a clear ``ValueError``.
    """
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    line_total = data.get("line_total")
    if line_total is None or line_total > 0:
        return
    # Every folder is named, with the filters that were used against *it*:
    # a merged total of zero means no folder contributed, and filters are
    # anchored to one folder's absolute paths, so pairing them wrongly sends
    # the reader to compare patterns against a tree they never described.
    per_folder = "".join(
        f"\n     - {folder}\n       filters: {filters!r}"
        for folder, filters in collections
    )
    raise RuntimeError(
        "gcovr collected zero instrumented lines from any build folder. "
        "The C++ coverage report is empty. Common causes:\n"
        "  1. The binary was compiled without --coverage. Verify that "
        "the coverage=True option reached the build (the recipe passes "
        "it to CMake as -DXMS_COVERAGE; this is the most common cause).\n"
        "  2. The filter patterns excluded every source file. Compare each "
        "folder's filters against the absolute source paths embedded in the "
        ".gcno files under that same folder:" + per_folder + "\n"
        "  3. No .gcno files exist in the build folders at all (the "
        "packages may have been pulled from a binary cache rather than "
        "rebuilt with instrumentation)."
    )


def _collect_gcovr_tracefile(build_folder: Path, coverage_cfg: dict,
                             tracefile: Path) -> list[str]:
    """Read one build folder's .gcda into a gcovr JSON tracefile.

    ``--root`` is the build folder rather than the conan source folder
    because ``cmake_layout()`` copies sources into the build folder
    before compilation; ``.gcno`` files therefore embed paths under
    ``build_folder``, and gcovr's default ``re.match``-style filter
    semantics need ``--root`` to align with those paths or every file
    is silently filtered out.

    Filters are applied here rather than on the merge, because
    :func:`_resolve_gcovr_filters` anchors them to this specific build
    folder's absolute paths. The tracefile it writes stores paths
    relative to ``--root``, which is what lets two folders' tracefiles
    line up on the same source file when they are merged.

    Returns the resolved filter list, for the caller's diagnostics.
    """
    resolved_filters = _resolve_gcovr_filters(
        coverage_cfg.get("filters", []), build_folder,
    )
    cmd = [
        "gcovr",
        "--root", str(build_folder),
        "--json", str(tracefile),
        "--gcov-ignore-errors=no_working_dir_found",
        "--gcov-ignore-errors=source_not_found",
    ]
    for f in resolved_filters:
        cmd.extend(["--filter", f])
    for e in coverage_cfg.get("excludes", []):
        cmd.extend(["--exclude", e])
    cmd.append(str(build_folder))
    _run(cmd)
    _warn_if_tracefile_empty(tracefile, build_folder, resolved_filters)
    return resolved_filters


def _warn_if_tracefile_empty(tracefile: Path, build_folder: Path,
                             resolved_filters) -> None:
    """Warn when one build folder contributed no files to the merge.

    Warns rather than raises: a folder legitimately contributing nothing is
    possible, and the merged report is what the run is judged on --
    :func:`_assert_gcovr_collected_data` still fails an empty *merged* result.
    But a folder silently dropping out is invisible in the merged number, which
    stays healthy on the other folder's data, so the signal has to exist
    somewhere.

    A compiled file appears here even when no test executed it -- .gcno is
    written at compile time -- so an empty tracefile means compilation or
    instrumentation did not happen, not that tests missed the code.

    Reported here rather than after the loop so the folder and the filters
    named in the message are the pair actually used together; the filters
    :func:`_resolve_gcovr_filters` produces are anchored to one folder's
    absolute paths and mean nothing against another's.
    """
    try:
        data = json.loads(tracefile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read gcovr tracefile %s: %s", tracefile, exc)
        return
    if data.get("files"):
        return
    LOGGER.warning(
        "gcovr collected no files from %s, so nothing from that build reaches "
        "the merged C++ report. A file compiled there would appear even with "
        "no test exercising it, so this points at compilation or "
        "instrumentation rather than test coverage: check that the "
        "coverage=True option reached this build's CMake configure step. "
        "Filters used for this folder were: %r",
        build_folder, resolved_filters,
    )


def _log_gcov_data_size(build_folders: list) -> None:
    """Log how much gcov data each instrumented build folder holds.

    ``.gcno`` is written at compile time and ``.gcda`` at run time, and gcovr
    needs both side by side. That pairing is what makes the volume worth
    knowing: splitting the compile and the test run into separate CI jobs
    means staging every ``.gcno`` as an artifact for the job that runs the
    binary, and a template-heavy translation unit's notes file is not small.
    Measuring it here costs a directory walk beside data that already exists;
    discovering it is too large costs a pipeline.

    Args:
        build_folders: Instrumented conan build folders, as located by
            :func:`_locate_coverage_build`.
    """
    for folder in build_folders:
        counts = {}
        for suffix in (".gcno", ".gcda"):
            files = list(Path(folder).rglob("*" + suffix))
            total = sum(f.stat().st_size for f in files if f.is_file())
            counts[suffix] = (len(files), total)
        LOGGER.info(
            "gcov data in %s: %d .gcno (%.1f MiB), %d .gcda (%.1f MiB)",
            folder,
            counts[".gcno"][0], counts[".gcno"][1] / (1024 * 1024),
            counts[".gcda"][0], counts[".gcda"][1] / (1024 * 1024),
        )


def _run_gcovr(build_folders: list[Path], coverage_cfg: dict,
               output_dir: Path) -> Path:
    """Merge every build folder's C++ coverage into one report.

    Each folder is read separately -- they have different roots, so one
    gcovr invocation cannot span them -- and the resulting tracefiles are
    combined with ``--add-tracefile``. Hit counts sum per line, so a line
    the CxxTest runner never reaches still counts as covered when a Python
    test reaches it through the bindings.

    The combination has to happen at this level. ``.gcda`` counters carry a
    checksum tied to the ``.gcno`` they were compiled against, so merging
    them -- with ``gcov-tool merge`` or by letting libgcov accumulate into
    one tree -- is only defined for runs of the *same* binary. These two
    folders hold different binaries: one is Debug with the C++ suite linked
    in, the other Release with the bindings. Merging their ``.gcda`` would
    match no checksums and yield nothing usable. gcovr's JSON is already
    resolved to source lines, which is what makes a union of the two
    meaningful.

    Within a single folder the same-binary condition does hold, which is why
    the parallel ctest run underneath can let libgcov merge its processes'
    counters in place.

    ``build_folders`` is ordered; the first is used as the merge ``--root``
    so ``--html-details`` can find sources to render. Both folders hold a
    copy of the same exported tree, so either serves.

    Returns the path to the merged JSON summary.
    """
    build_folders = [Path(f) for f in build_folders]
    output_dir.mkdir(parents=True, exist_ok=True)
    tracefiles, collections = _collect_tracefiles(
        build_folders, coverage_cfg, output_dir,
        names=[str(i) for i in range(len(build_folders))],
    )
    return _merge_tracefiles(
        tracefiles, build_folders[0], output_dir, collections=collections,
    )


def _collect_tracefiles(build_folders: list[Path], coverage_cfg: dict,
                        output_dir: Path, names: list) -> tuple:
    """Read each build folder's .gcda into its own gcovr JSON tracefile.

    Args:
        build_folders: The instrumented conan build folders to read.
        coverage_cfg: The resolved ``[coverage]`` context.
        output_dir: Where the tracefiles are written.
        names: One name per folder, spliced into
            ``cov-cpp-tracefile-<name>.json``. The merge globs that pattern, so
            the names only have to be distinct -- the collect phase numbers
            them, and the split build jobs use their leg name so two jobs
            writing into the same artifact space cannot land on one filename.

    Returns:
        ``(tracefiles, collections)``, where ``collections`` pairs each folder
        with the filters resolved against it -- kept as pairs, not a flat list,
        because the filters are anchored to one folder's absolute paths, so the
        folder has to travel with them for any diagnostic built from them to be
        actionable.
    """
    tracefiles = []
    collections = []
    for name, build_folder in zip(names, build_folders):
        tracefile = output_dir / f"cov-cpp-tracefile-{name}.json"
        resolved_filters = _collect_gcovr_tracefile(
            build_folder, coverage_cfg, tracefile,
        )
        tracefiles.append(tracefile)
        collections.append((build_folder, resolved_filters))
    return tracefiles, collections


def _merge_tracefiles(tracefiles: list[Path], root, output_dir: Path,
                      collections=None) -> Path:
    """Combine tracefiles into the rendered C++ report.

    ``root`` only has to be a tree the tracefiles' *relative* paths resolve
    against, which is what lets this run somewhere the build folders do not
    exist. :func:`_collect_gcovr_tracefile` roots each tracefile at its own
    build folder, so the paths it stores are relative to the copy of the
    exported source tree ``cmake_layout()`` put there -- and the repository
    checkout has that same layout. The split GitLab pipeline relies on this:
    the two instrumented builds each emit a tracefile from a container holding
    a conan cache, and the report job merges them from a plain python image
    with nothing but the checkout.

    Returns the path to the merged JSON summary.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    html_index = output_dir / "coverage-html-cpp" / "index.html"
    html_index.parent.mkdir(parents=True, exist_ok=True)
    xml_path = output_dir / "cov-cpp.xml"
    json_summary = output_dir / "cov-cpp-summary.json"

    cmd = [
        "gcovr",
        "--root", str(root),
        "--txt",
        "--html-details", str(html_index),
        "--xml", str(xml_path),
        "--json-summary", str(json_summary),
    ]
    for tracefile in tracefiles:
        cmd.extend(["--add-tracefile", str(tracefile)])
    _run(cmd)
    # Only an empty *merged* report fails the run. A single folder contributing
    # nothing is legitimate -- a library whose bindings are a stub has little to
    # instrument there -- and is reported as a warning by the collection step.
    _assert_gcovr_collected_data(json_summary, collections or [])
    return json_summary


def _cpp_percent_from_summary(summary_path: Path) -> float:
    """Extract the line coverage percentage from a gcovr JSON summary.

    Raises ``ValueError`` naming the summary path when the expected key is
    absent, so a gcovr schema change or a truncated write surfaces as a clear
    diagnostic rather than collapsing to an indistinguishable 0%.
    """
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    try:
        return float(data["line_percent"])
    except KeyError as exc:
        raise ValueError(
            f"gcovr summary at {summary_path} is missing key 'line_percent'; "
            "gcovr schema may have changed or the file is truncated."
        ) from exc


def _py_percent_from_summary(summary_path: Path) -> float:
    """Extract the line coverage percentage from a pytest-cov JSON summary.

    Raises ``ValueError`` naming the summary path when the expected key is
    absent, so a pytest-cov schema change or a truncated write surfaces as a
    clear diagnostic rather than collapsing to an indistinguishable 0%.
    """
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    try:
        return float(data["totals"]["percent_covered"])
    except KeyError as exc:
        raise ValueError(
            f"pytest-cov summary at {summary_path} is missing "
            "'totals.percent_covered'; pytest-cov schema may have changed or "
            "the file is truncated."
        ) from exc


def _append_github_summary(rows: list[tuple[str, float, Optional[float], bool]]):
    """Append a markdown table to $GITHUB_STEP_SUMMARY if present.

    A row's actual of ``None`` means the layer was never (fully) measured.
    It renders as words rather than a percent so a narrowed or absent
    measurement cannot be misread as a merely low score.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "",
        "## Coverage Summary",
        "",
        "| Layer  | Threshold | Actual | Status |",
        "| ------ | --------- | ------ | ------ |",
    ]
    for layer, threshold, actual, passed in rows:
        status = "PASS" if passed else "FAIL"
        actual_cell = f"{actual:.1f}%" if actual is not None else "n/a (unmeasured)"
        lines.append(f"| {layer} | {threshold:.1f}% | {actual_cell} | {status} |")
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def run_coverage(toml_file_path: str | Path, version: str,
                 output_dir: str | Path, phase: str = PHASE_ALL,
                 leg: Optional[str] = None) -> int:
    """Drive a two-build coverage run (C++ via CxxTest + Python via pytest-cov).

    Args:
        toml_file_path: Path to ``build.toml``.
        version: The build version passed through to ``xmsconan_gen`` and
            ``build.py``. Unused by :data:`PHASE_REPORT`, which builds nothing.
        output_dir: Workspace the artifacts are written into and read back from.
        phase: One of :data:`COVERAGE_PHASES`. The default runs everything in
            one process, which is what a local run and the GitHub workflow do.
            ``measure`` builds a single leg and reads its data where it was
            compiled, which is what lets the generated GitLab pipeline run the
            two instrumented builds concurrently; ``report`` then merges their
            tracefiles and gates.
        leg: Required by :data:`PHASE_MEASURE`, and meaningless to every other
            phase. One of :data:`COVERAGE_LEGS`.

    Returns the process exit code: ``EXIT_OK`` when both layers clear their
    thresholds and every leg built, ``EXIT_GATE_FAILED`` for a threshold miss,
    an unmeasured layer, or a build.py failure in either layer, and
    ``EXIT_ERROR`` when neither leg left a package to report on. The
    generated CI distinguishes the gate from the error -- see the exit-code
    constants.

    A failed collect short-circuits: there is nothing to gate on, and returning
    the gate code for it would let ``allow_failure: exit_codes`` forgive a
    coverage run that produced no report at all.
    """
    if phase not in COVERAGE_PHASES:
        raise ValueError(
            f"Unknown coverage phase {phase!r}; expected one of "
            f"{', '.join(COVERAGE_PHASES)}."
        )
    if phase == PHASE_REPORT:
        return _report_coverage(toml_file_path, output_dir)
    if phase == PHASE_MEASURE:
        if leg is None:
            raise ValueError(
                f"The {PHASE_MEASURE!r} phase measures one leg, so it needs "
                f"--leg (one of {', '.join(COVERAGE_LEGS)}). Without it there "
                "is no way to tell which build to run, and guessing would "
                "silently measure one layer while reporting for both."
            )
        return _measure_coverage(toml_file_path, version, output_dir, leg)

    exit_code = _collect_coverage(toml_file_path, version, output_dir)
    if phase == PHASE_COLLECT or exit_code != EXIT_OK:
        return exit_code
    return _report_coverage(toml_file_path, output_dir)


def _copy_pytest_cov_artifacts(py_build_folder, output_dir: Path):
    """Lift pytest-cov's output out of the pybind build folder.

    ``run_python_tests`` writes them inside the conan build folder, which is
    private to the job that compiled it. Copying them up to ``output_dir`` is
    what lets them survive as CI artifacts.

    Args:
        py_build_folder: The instrumented pybind build folder, or None when
            that leg left no package.
        output_dir: Workspace the artifacts are copied into.

    Returns:
        The source path of ``cov-py-summary.json``, or None when it was not
        produced -- which the caller records as *no measurement*, distinct
        from a measured 0%.
    """
    if py_build_folder is None:
        return None
    py_summary_src = _find_pytest_cov_artifact(
        py_build_folder, "cov-py-summary.json", kind="file",
    )
    if py_summary_src is not None:
        shutil.copy2(py_summary_src, output_dir / "cov-py-summary.json")
    py_xml_src = _find_pytest_cov_artifact(
        py_build_folder, "cov-py.xml", kind="file",
    )
    if py_xml_src is not None:
        shutil.copy2(py_xml_src, output_dir / "cov-py.xml")
    py_html_src = _find_pytest_cov_artifact(
        py_build_folder, "coverage-html-py", kind="dir",
    )
    py_html_dst = output_dir / "coverage-html-py"
    if py_html_src is not None:
        if py_html_dst.exists():
            shutil.rmtree(py_html_dst)
        shutil.copytree(py_html_src, py_html_dst)
    return py_summary_src


def _coverage_legs(coverage_python_version: str) -> dict:
    """The instrumented builds a coverage run drives, keyed by ``--leg`` name.

    One definition, two callers: ``collect`` builds both in a single job,
    ``measure`` builds exactly one so the generated GitLab pipeline can run
    them as concurrent build jobs. A second copy of these filters would be a
    copy free to drift, and the drift would be near-invisible -- a leg built
    under a filter the reader does not use still produces a package, so the
    run stays green and quietly measures nothing.
    """
    return {
        LEG_CPP: _BuildLeg("C++", json.dumps({
            "build_type": "Debug",
            "options": {"testing": True, "pybind": False},
        })),
        LEG_PYTHON: _BuildLeg("Python", json.dumps({
            "build_type": COVERAGE_PYBIND_BUILD_TYPE,
            "options": {
                "pybind": True,
                "testing": False,
                "python_version": coverage_python_version,
            },
        })),
    }


class _CoverageRun(NamedTuple):
    """The setup every instrumented phase needs before it can build.

    Shared so ``collect`` (both legs in one job) and ``measure`` (one leg per
    job) cannot drift on the two things that must agree across them: the
    ``XMS_COVERAGE`` environment that makes the generated profiles request the
    ``coverage`` option, and the single Python ABI the filter, the package
    lookup and the container image are all pinned to.
    """

    toml_file: Path
    output_dir: Path
    config: object
    library_name: str
    coverage_cfg: dict
    coverage_python_version: str
    env: dict


def _prepare_coverage_run(toml_file_path, version, output_dir) -> _CoverageRun:
    """Read build.toml, regenerate under coverage, and build the leg env."""
    toml_file = Path(toml_file_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not toml_file.exists():
        raise FileNotFoundError(
            f"The specified TOML file does not exist: {toml_file_path}"
        )

    config = read_build_toml(toml_file)
    library_name = config.library_name
    coverage_cfg = _coverage_context(config.coverage, library_name)
    coverage_python_version = _resolve_coverage_python_version(config)

    if config.ci.xvfb:
        _reexec_under_xvfb()

    # Regenerate with XMS_COVERAGE set, so every profile this run builds
    # from carries the coverage option rather than the production one.
    _run(
        ["xmsconan_gen", "--version", version, "--output_dir", str(output_dir),
         str(toml_file)],
        cwd=str(output_dir),
    )

    env = os.environ.copy()
    env["XMS_COVERAGE"] = "1"
    # Tell build.py's matrix generator which ABI to *produce*. The --filter
    # below only narrows the matrix it already built, so without this the
    # packager falls back to DEFAULT_PYTHON_VERSIONS and a coverage ABI of
    # anything else matches zero configurations. Set deliberately after the
    # os.environ copy: build.toml outranks an ambient PYTHON_TARGET_VERSION,
    # because the same resolved value also drives the --filter and the
    # _find_coverage_package lookup, and the three must not disagree.
    ambient_python_version = env.get("PYTHON_TARGET_VERSION")
    if ambient_python_version and ambient_python_version != coverage_python_version:
        LOGGER.warning(
            "Ignoring PYTHON_TARGET_VERSION=%s from the environment; build.toml "
            "resolves the coverage ABI to %s, and the --filter and package "
            "lookup are already pinned to it. Set [coverage].python_version = "
            '"%s" in build.toml to cover that ABI instead.',
            ambient_python_version, coverage_python_version, ambient_python_version,
        )
    env["PYTHON_TARGET_VERSION"] = coverage_python_version
    return _CoverageRun(
        toml_file=toml_file, output_dir=output_dir, config=config,
        library_name=library_name, coverage_cfg=coverage_cfg,
        coverage_python_version=coverage_python_version, env=env,
    )


def _collect_coverage(toml_file_path: str | Path, version: str,
                      output_dir: str | Path) -> int:
    """Build both instrumented legs and render every coverage artifact.

    The packager emits a Debug+testing-only config and a Release+pybind-only
    config independently; this builds both -- sequentially unless
    ``[coverage].parallel = true`` (§5.7) overlaps them -- then runs gcovr
    against both build folders and merges the result, and copies pytest-cov
    artifacts out of the pybind build folder. The two builds never share a
    binary shape, so changes to CxxTest linkage or pybind options cannot break
    the other layer. Both are instrumented: the testing build supplies what
    CxxTest reaches, the pybind build the binding layer and anything only a
    Python test exercises.

    Everything gcovr needs paths for happens here, beside the build folders
    that produced the data. What leaves is portable: rendered HTML, XML, the
    two summary JSONs, and :data:`COVERAGE_STATUS_FILE`.

    Returns ``EXIT_OK`` once the artifacts are written -- including when the
    tests failed, which is recorded for the report phase to gate on -- and
    ``EXIT_ERROR`` when neither leg left a package to report on.
    """
    run = _prepare_coverage_run(toml_file_path, version, output_dir)
    output_dir = run.output_dir
    library_name = run.library_name
    coverage_cfg = run.coverage_cfg
    coverage_python_version = run.coverage_python_version
    env = run.env

    # 2/3. The two coverage builds.
    #
    #   * C++: testing=True, pybind=False, Debug. Runs the C++ suite under
    #     --coverage, producing the .gcda set gcovr reads.
    #   * Python: pybind=True, testing=False, Release, pinned to one Python
    #     ABI. Drives pytest-cov against the wheel inside the recipe's
    #     run_python_tests, and is instrumented so step 5 can merge its .gcda
    #     for the binding layer. Release rather than Debug because Debug+pybind
    #     is often not published for XMS libraries and may not exist, and the
    #     XMS_COVERAGE CMake block appends -O0 -g after CMake's -O3, so a
    #     Release build is unoptimized and loses nothing in line data.
    #
    # They share no binary shape and no output file -- separate conan package
    # ids, separate build folders, separate reports -- which is what lets them
    # overlap. Only the report step below reads both. They run in sequence by
    # default; `[coverage].parallel = true` overlaps them on a runner where
    # the conan cache race and the CPU contention do not apply.
    legs = list(_coverage_legs(coverage_python_version).values())
    tests_failed = _run_coverage_builds(
        legs, version, env, str(output_dir), coverage_cfg["parallel"],
    )

    # 4. Locate the two build folders. gcovr reads .gcda from both; pytest-cov
    #    artifacts live under the pybind folder. Either may be absent when its
    #    leg failed above, which is not fatal on its own -- the surviving leg
    #    still has .gcda worth reporting, and step 7 fails the gate anyway.
    cpp_build_folder = _locate_coverage_build(library_name, kind="testing")
    LOGGER.info("C++ coverage build folder:    %s",
                cpp_build_folder if cpp_build_folder else "<missing>")

    py_build_folder = _locate_coverage_build(
        library_name, kind="pybind",
        python_version=coverage_python_version,
    )
    LOGGER.info("Python coverage build folder: %s",
                py_build_folder if py_build_folder else "<missing>")

    gcovr_folders = [folder for folder in (cpp_build_folder, py_build_folder)
                     if folder is not None]
    if not gcovr_folders:
        LOGGER.error(
            "Neither coverage build left a package in the local Conan cache, "
            "so there is no .gcda to report on. Failing the run rather than "
            "writing an empty report that would look like 0%% coverage."
        )
        return EXIT_ERROR

    # 5. Generate the C++ coverage report from both instrumented builds. The
    #    testing build covers what CxxTest reaches; the pybind build covers the
    #    binding layer and anything only a Python test exercises.
    #    Its JSON summary lands in output_dir, which is where the report phase
    #    reads it back from -- the return value is that path.
    _log_gcov_data_size(gcovr_folders)
    _run_gcovr(gcovr_folders, coverage_cfg, output_dir)

    # 6. Locate Python coverage artifacts produced inside the pybind build
    #    folder by run_python_tests, and copy them up to the workspace root.
    py_summary_src = _copy_pytest_cov_artifacts(py_build_folder, output_dir)

    # 7. Record what only this phase can know. A missing testing leg does not
    #    zero the C++ number -- gcovr merges whatever folders survived step 4,
    #    so the percent quietly narrows to the binding layer alone, and
    #    cpp_threshold defaults to 0, which would read as PASS. The report
    #    phase cannot tell that from the summary file, so it is recorded here.
    cpp_measured = cpp_build_folder is not None
    if not cpp_measured:
        LOGGER.error(
            "No C++ coverage measurement: the testing coverage build left no "
            "build folder to read, so the C++ percent covers the binding "
            "layer alone. The gate will fail rather than report a mostly "
            "unmeasured layer as passing."
        )
    # An absent summary is not 0% coverage, it is *no measurement*, and the two
    # have to be told apart: python_threshold defaults to 0, so 0.0 >= 0.0
    # would report "Python: 0.0% -> PASS" for a layer that never ran. Step 3
    # always builds pybind with the coverage option and the coverage job is
    # Linux-only in both CI templates, so the only way the file is legitimately
    # absent is the pybind leg failing outright.
    py_measured = py_summary_src is not None
    if not py_measured:
        if py_build_folder is None:
            LOGGER.error(
                "No Python coverage measurement: the pybind coverage build "
                "left no package in the cache, so its summary could not be "
                "collected. The gate will fail rather than report an "
                "unmeasured layer as passing."
            )
        else:
            LOGGER.error(
                "No cov-py-summary.json under %s. The pybind coverage build "
                "ran, so run_python_tests should have written one -- the gate "
                "will fail rather than report an unmeasured layer as passing.",
                py_build_folder,
            )
    _write_coverage_status(
        output_dir,
        tests_failed=tests_failed,
        cpp_measured=cpp_measured,
        py_measured=py_measured,
    )
    return EXIT_OK


def _measure_coverage(toml_file_path: str | Path, version: str,
                      output_dir: str | Path, leg_name: str) -> int:
    """Build one instrumented leg and read its coverage where it was compiled.

    The single-leg counterpart to :func:`_collect_coverage`. It exists because
    gcovr needs the ``.gcno`` written at compile time beside the ``.gcda``
    written at run time, so whatever reads a leg's data has to be the job that
    compiled it -- and a single job compiling both legs is a job doing two
    compiles in sequence. Splitting them lets the generated pipeline run the
    legs as concurrent build jobs and merge afterwards.

    What leaves is small and portable: a gcovr tracefile whose paths are
    relative to the source tree (see :func:`_merge_tracefiles`), this leg's
    slice of the status file, and, for the Python leg, pytest-cov's own
    output. The build folder itself never moves -- at 245 MiB of ``.gcno``
    against 5.5 MiB of ``.gcda`` on xmsvtk's C++ leg, moving it would cost far
    more than the compile it saves.

    Returns ``EXIT_OK`` once this leg's artifacts are written -- including when
    its tests failed, which is recorded for the report phase to gate on -- and
    ``EXIT_ERROR`` when the leg left no package to read.
    """
    if leg_name not in COVERAGE_LEGS:
        raise ValueError(
            f"Unknown coverage leg {leg_name!r}; expected one of "
            f"{', '.join(COVERAGE_LEGS)}."
        )
    run = _prepare_coverage_run(toml_file_path, version, output_dir)
    output_dir = run.output_dir
    leg = _coverage_legs(run.coverage_python_version)[leg_name]

    tests_failed = _run_coverage_builds(
        [leg], version, run.env, str(output_dir), parallel=False,
    )

    if leg_name == LEG_CPP:
        build_folder = _locate_coverage_build(run.library_name, kind="testing")
    else:
        build_folder = _locate_coverage_build(
            run.library_name, kind="pybind",
            python_version=run.coverage_python_version,
        )
    LOGGER.info("%s coverage build folder: %s", leg.name,
                build_folder if build_folder else "<missing>")

    if build_folder is None:
        LOGGER.error(
            "The %s coverage build left no package in the local Conan cache, "
            "so there is no .gcda to read. Recording the leg as unmeasured "
            "rather than writing an empty tracefile, which the merge would "
            "read as a layer that is genuinely 0%% covered.",
            leg.name,
        )
        _write_coverage_status(
            output_dir, tests_failed=tests_failed,
            cpp_measured=False, py_measured=False, leg=leg_name,
        )
        return EXIT_ERROR

    _log_gcov_data_size([build_folder])
    _collect_tracefiles(
        [build_folder], run.coverage_cfg, output_dir, names=[leg_name],
    )

    py_summary_src = None
    if leg_name == LEG_PYTHON:
        py_summary_src = _copy_pytest_cov_artifacts(build_folder, output_dir)
        if py_summary_src is None:
            LOGGER.error(
                "The pybind coverage build produced no cov-py-summary.json in "
                "%s. Its tests ran inside the build, so run_python_tests "
                "should have written one -- the gate will fail rather than "
                "report an unmeasured layer as passing.", build_folder,
            )

    _write_coverage_status(
        output_dir,
        tests_failed=tests_failed,
        # Each leg reports only on itself. The report phase unions the files,
        # so a leg that never ran leaves its layer unmeasured rather than
        # being asserted measured by the other leg's status.
        cpp_measured=(leg_name == LEG_CPP),
        py_measured=(leg_name == LEG_PYTHON and py_summary_src is not None),
        leg=leg_name,
    )
    return EXIT_OK


def _write_coverage_status(output_dir: Path, *, tests_failed: bool,
                           cpp_measured: bool, py_measured: bool,
                           leg: Optional[str] = None) -> None:
    """Hand the report phase the facts it cannot re-derive from the summaries.

    Written even on a healthy run, so its absence in the report phase is
    unambiguous: the measuring phase did not finish, rather than finishing
    with nothing to say.

    Args:
        output_dir: Where the file is written.
        tests_failed: Whether any test run this phase drove failed.
        cpp_measured: Whether the C++ layer was actually measured.
        py_measured: Whether the Python layer was actually measured.
        leg: The leg this status covers, which suffixes the filename. None
            writes the whole-run file ``collect`` produces. The suffix is what
            keeps two concurrent build jobs from overwriting each other: they
            upload into one artifact space, and a single fixed filename would
            leave whichever job finished last as the only surviving answer --
            silently discarding the other layer's measurement while still
            looking like a complete status.
    """
    status = {
        "tests_failed": bool(tests_failed),
        "cpp_measured": bool(cpp_measured),
        "py_measured": bool(py_measured),
    }
    name = COVERAGE_STATUS_FILE
    if leg is not None:
        name = f"{Path(COVERAGE_STATUS_FILE).stem}-{leg}.json"
    path = Path(output_dir) / name
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    LOGGER.info("Wrote %s: %s", path, status)


def _read_one_status(path: Path) -> Optional[dict]:
    """Read a single status file, or None when it is absent or unreadable.

    "Unreadable" includes well-formed JSON that is not an object. The caller
    unions these with ``.get()``, so a file holding a list or a bare string
    would reach it as a valid payload and raise ``AttributeError`` from inside
    the gate -- a crash in the job whose whole purpose is to report a verdict.
    Treating it as a missing file instead lets the union fall through to its
    unmeasured-layer path, which fails the gate loudly and names the file.
    """
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Could not read %s: %s", path, exc)
        return None
    if not isinstance(status, dict):
        LOGGER.error("Ignoring %s: expected a JSON object, got %s",
                     path, type(status).__name__)
        return None
    return status


def _read_coverage_status(output_dir: Path) -> Optional[dict]:
    """Read what the measuring phase recorded, or None when it left nothing.

    A ``collect`` run writes one whole-run file; a split pipeline writes one
    file per leg, and this unions them. The union is not symmetric across the
    three fields, and each asymmetry is deliberate:

    * ``tests_failed`` is an OR. Either leg's tests failing has to fail the
      gate, and a leg that never ran cannot vouch for the other's tests.
    * ``cpp_measured`` / ``py_measured`` are ORs over legs that each report
      only on themselves. A missing leg therefore leaves its own layer
      unmeasured rather than inheriting the surviving leg's answer -- which is
      the case that matters, because an unmeasured layer whose threshold
      defaults to 0 would otherwise read as a clean PASS.

    The whole-run file is read too, so ``--phase all`` and ``--phase collect``
    keep working unchanged.
    """
    output_dir = Path(output_dir)
    paths = [output_dir / COVERAGE_STATUS_FILE]
    stem = Path(COVERAGE_STATUS_FILE).stem
    paths.extend(sorted(output_dir.glob(f"{stem}-*.json")))

    found = [status for status in (_read_one_status(path) for path in paths)
             if status is not None]
    if not found:
        return None
    return {
        "tests_failed": any(s.get("tests_failed") for s in found),
        "cpp_measured": any(s.get("cpp_measured") for s in found),
        "py_measured": any(s.get("py_measured") for s in found),
    }


def _report_coverage(toml_file_path: str | Path,
                     output_dir: str | Path) -> int:
    """Apply the ``[coverage]`` thresholds to what the collect phase produced.

    Reads only JSON, so it needs neither a conan cache nor a compiler and can
    run in a job of its own. Thresholds come from ``build.toml`` rather than
    from the status file, so tightening one takes effect without re-running the
    builds.

    Returns ``EXIT_OK`` when both layers clear their thresholds,
    ``EXIT_GATE_FAILED`` for a threshold miss, an unmeasured layer, or tests
    that failed during collection, and ``EXIT_ERROR`` when the collect phase
    left nothing to report on.
    """
    toml_file = Path(toml_file_path).resolve()
    output_dir = Path(output_dir).resolve()
    config = read_build_toml(toml_file)
    coverage_cfg = _coverage_context(config.coverage, config.library_name)

    status = _read_coverage_status(output_dir)
    if status is None:
        LOGGER.error(
            "No readable %s (or per-leg %s-<leg>.json) in %s, so there is "
            "nothing to gate on. The measuring phase writes one on every run "
            "that gets as far as producing reports -- check that it ran and "
            "that its artifacts reached this job.",
            COVERAGE_STATUS_FILE, Path(COVERAGE_STATUS_FILE).stem, output_dir,
        )
        return EXIT_ERROR

    cpp_summary = output_dir / "cov-cpp-summary.json"
    py_summary = output_dir / "cov-py-summary.json"

    # A split pipeline arrives here with per-leg tracefiles and no rendered
    # report: each measuring job read its own build folder and stopped there,
    # because merging needs both legs and no single build job has both. The
    # merge happens here instead. It needs no conan cache -- the tracefiles
    # store source paths relative to their build folder's copy of the exported
    # tree, and the repository checkout has that same layout, so the checkout
    # serves as --root. A `collect` run has already rendered the summary and
    # skips this.
    tracefiles = sorted(output_dir.glob("cov-cpp-tracefile-*.json"))
    if tracefiles and not cpp_summary.exists():
        LOGGER.info("Merging %d coverage tracefile(s) into the C++ report: %s",
                    len(tracefiles), ", ".join(f.name for f in tracefiles))
        _merge_tracefiles(tracefiles, Path.cwd(), output_dir)

    if not cpp_summary.exists():
        LOGGER.error(
            "No %s, and no cov-cpp-tracefile-*.json to build one from. The "
            "measuring phase writes one or the other on every run that gets "
            "as far as reading a build folder -- check that it ran and that "
            "its artifacts reached this job.", cpp_summary,
        )
        return EXIT_ERROR

    # Compare raw percentages so a 69.95% build does not sneak past a 70.0
    # threshold via display rounding.
    cpp_raw = _cpp_percent_from_summary(cpp_summary)
    py_raw = _py_percent_from_summary(py_summary) if py_summary.exists() else 0.0
    cpp_threshold = coverage_cfg["cpp_threshold"]
    py_threshold = coverage_cfg["python_threshold"]
    cpp_measured = bool(status.get("cpp_measured"))
    py_measured = bool(status.get("py_measured"))
    tests_failed = bool(status.get("tests_failed"))
    cpp_pass = cpp_measured and cpp_raw >= cpp_threshold
    py_pass = py_measured and py_raw >= py_threshold

    rows = [
        ("C++", cpp_threshold,
         round(cpp_raw, 1) if cpp_measured else None, cpp_pass),
        ("Python", py_threshold,
         round(py_raw, 1) if py_measured else None, py_pass),
    ]
    LOGGER.info("Coverage summary:")
    for layer, threshold, actual, passed in rows:
        shown = f"{actual:.1f}%" if actual is not None else "n/a (unmeasured)"
        LOGGER.info("  %s: %s (threshold %.1f%%) -> %s",
                    layer, shown, threshold, "PASS" if passed else "FAIL")
    _append_github_summary(rows)
    # The line GitLab's `coverage:` regex reads. Printed by this phase because
    # this is the job that gates -- gcovr's own --txt TOTAL is written in the
    # collect job, whose log GitLab does not scrape for the pipeline number.
    # "n/a" when unmeasured, so an unmeasured layer cannot publish a percentage
    # that looks like a real measurement.
    LOGGER.info("Coverage total: %s",
                f"{cpp_raw:.1f}%" if cpp_measured else "n/a")

    if not cpp_measured:
        LOGGER.error("Coverage gate FAIL: the C++ layer was not measured.")
    if not py_measured:
        LOGGER.error("Coverage gate FAIL: the Python layer was not measured.")
    if tests_failed:
        LOGGER.error("Coverage gate FAIL: build.py exited non-zero during "
                     "collection; see that job's log for the failing test(s).")
    if cpp_pass and py_pass and not tests_failed:
        return EXIT_OK
    return EXIT_GATE_FAILED


def main():
    """Entry point for ``xmsconan coverage`` (and the legacy ``xmsconan_coverage`` script)."""
    from xmsconan.generator_tools.version import resolve_version, VERSION_FLAG_HELP

    parser = argparse.ArgumentParser(description="Run xmsconan unified coverage workflow.")
    parser.add_argument(
        "--output_dir", default=".",
        help="Workspace directory the coverage artifacts are written into.",
    )
    add_verbosity_args(parser)
    parser.add_argument(
        "--version", default=None,
        help=f"The build version. {VERSION_FLAG_HELP}",
    )
    parser.add_argument(
        "--phase", choices=COVERAGE_PHASES, default=PHASE_ALL,
        help=(
            "Which part of the run to do. 'measure' builds ONE instrumented "
            "leg (see --leg) and writes that leg's tracefile beside the build "
            "folder that produced it; 'collect' does both legs in one process; "
            "'report' merges whatever tracefiles it finds and applies the "
            "[coverage] thresholds. The default does everything in one "
            "process. The generated GitLab pipeline uses 'measure' so the two "
            "instrumented builds are separate concurrent jobs, and 'report' "
            "so the gate runs in a seconds-long job that needs no toolchain."
        ),
    )
    parser.add_argument(
        "--leg", choices=COVERAGE_LEGS, default=None,
        help=(
            "Which instrumented build to run, for --phase measure. 'cpp' is "
            "the Debug testing configuration, 'python' the Release pybind one. "
            "Ignored by every other phase."
        ),
    )
    parser.add_argument(
        "toml_file", nargs="?", default="build.toml",
        help="Path to build.toml. Defaults to ./build.toml.",
    )

    args = parser.parse_args()
    configure_logging(args)

    version = resolve_version(args.version)

    try:
        exit_code = run_coverage(
            args.toml_file, version, args.output_dir, phase=args.phase,
            leg=args.leg,
        )
    except Exception as exc:
        # subprocess.run(..., capture_output=True, check=True) callers
        # (_find_coverage_package, _conan_cache_path) raise CalledProcessError
        # with the conan stderr buffered inside. Surface it before printing the
        # traceback so the operator sees the actual conan diagnostic.
        if isinstance(exc, subprocess.CalledProcessError):
            stderr = exc.stderr
            if stderr:
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
        traceback.print_exc()
        raise SystemExit(EXIT_ERROR) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
