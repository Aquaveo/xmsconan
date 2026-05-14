"""Unified coverage runner for xmsconan libraries.

Runs a single instrumented build (pybind=True, build_type=Debug, pinned to one
``python_version``) under XMS_COVERAGE=1, then produces C++ (gcovr) and Python
(pytest-cov) coverage reports. ``pytest-cov`` exercises the wheel's underlying
C++, which yields the .gcda files gcovr needs — no separate CxxTest build is
required for coverage to be meaningful (see issue #64). Compares actuals to
thresholds from build.toml and exits non-zero on regression.
"""
# 1. Standard python modules
import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

# 2. Third party modules
try:
    import tomllib
except ModuleNotFoundError:
    import toml as tomllib  # toml.loads is compatible with tomllib.loads

# 3. Aquaveo modules
from xmsconan.generator_tools.ci_file_generator import _coverage_context


LOGGER = logging.getLogger(__name__)

_XVFB_REEXEC_FLAG = "XMSCONAN_COVERAGE_XVFB_REEXEC"

_DEFAULT_COVERAGE_PYTHON_VERSION = "3.13"


def _resolve_coverage_python_version(toml_data: dict) -> str:
    """Pick the single python_version the coverage build should pin to.

    Precedence: ``[coverage].python_version`` (explicit opt-in) >
    highest entry in ``[ci].python_versions`` > the global default
    (``"3.13"``). Coverage runs a single instrumented build, so we must
    commit to one ABI up front rather than let ``_find_coverage_package``
    return whichever pybind config happened to finish last (see issue
    #65).
    """
    coverage_cfg = toml_data.get("coverage", {})
    explicit = coverage_cfg.get("python_version")
    if explicit:
        return str(explicit)
    ci_versions = toml_data.get("ci", {}).get("python_versions") or [_DEFAULT_COVERAGE_PYTHON_VERSION]
    return max(ci_versions, key=lambda v: tuple(int(p) for p in str(v).split(".")))


def _configure_logging(args):
    """Configure logger from CLI verbosity flags."""
    if args.quiet:
        level = logging.ERROR
    elif args.verbose > 0:
        level = logging.DEBUG
    else:
        level = logging.INFO
    # force=True so reconfiguration works when basicConfig has already been
    # called (e.g., by the xmsconan dispatcher or an earlier subcommand).
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s', force=True)


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


def _load_toml(toml_path: Path) -> dict:
    """Load a TOML file using stdlib tomllib when available."""
    return tomllib.loads(toml_path.read_text(encoding="utf-8"))


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
    cmd = [xvfb_run, "-a", "-s", "-screen 0 1280x1024x24", sys.executable, *sys.argv]
    LOGGER.info("Re-execing under xvfb-run: %s", " ".join(cmd))
    os.execvpe(xvfb_run, cmd, env)


def _run(cmd, env=None, cwd=None):
    """Run a subprocess, streaming output and raising on non-zero exit."""
    LOGGER.info("$ %s", " ".join(cmd) if isinstance(cmd, list) else cmd)
    subprocess.run(cmd, env=env, cwd=cwd, check=True)


def _find_coverage_package(library_name: str, python_version: str) -> tuple[str, str]:
    """Locate the pybind+Debug package in the local Conan cache.

    Returns (exact_ref, package_id) for the newest matching revision.

    The matcher requires ``pybind=True`` and ``build_type=Debug`` but does
    *not* require ``testing=True`` — the packager never produces a config
    with both ``testing=True`` and ``pybind=True`` (they are mutually
    exclusive derivatives of the base combinations list, see issue #64),
    and a pybind-only build is sufficient: ``pytest-cov`` exercises the
    wheel's underlying C++ which is what produces the .gcda files gcovr
    needs.

    ``python_version`` pins the result to a single Python ABI so multi-
    version fan-outs cannot non-deterministically pick whichever pybind
    config finished last (see issue #65).
    """
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
                if not _opt_is_truthy(opts.get("pybind")):
                    continue
                if settings.get("build_type") != "Debug":
                    continue
                if opts.get("python_version") != python_version:
                    continue
                candidates.append((ts, exact_ref, pid))
    if not candidates:
        raise RuntimeError(
            f"No pybind=True, build_type=Debug, python_version={python_version} package "
            f"found for {library_name} in the local Conan cache. Did the coverage build "
            "complete?"
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


def _find_pytest_cov_artifact(build_folder: Path, name: str):
    """Locate a pytest-cov artifact (file or directory) inside the conan build folder.

    The recipe's ``run_python_tests`` writes coverage artifacts into a
    layout-specific subdirectory (e.g. ``<build_folder>/build/Debug/``),
    not the conan-managed build root that ``conan cache path
    --folder=build`` returns. Walking with ``rglob`` is robust against
    recipe layout changes and multi-build-type folders (see issue #71).

    Returns the matching path. ``None`` if the artifact isn't present
    (legitimate when pytest-cov never ran — e.g., ``pybind=False``).

    When more than one match exists (e.g. stale leftover from a prior
    build type) the newest by ``st_mtime`` is returned and a warning is
    logged so the operator can clean up.
    """
    matches = sorted(build_folder.rglob(name))
    if not matches:
        return None
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


def _run_gcovr(source_folder: Path, build_folder: Path, coverage_cfg: dict,
               output_dir: Path) -> Path:
    """Run gcovr against the build folder. Returns path to JSON summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    html_index = output_dir / "coverage-html-cpp" / "index.html"
    html_index.parent.mkdir(parents=True, exist_ok=True)
    xml_path = output_dir / "cov-cpp.xml"
    json_summary = output_dir / "cov-cpp-summary.json"

    cmd = [
        "gcovr",
        "--root", str(source_folder),
        "--txt",
        "--html-details", str(html_index),
        "--xml", str(xml_path),
        "--json-summary", str(json_summary),
        "--gcov-ignore-errors=no_working_dir_found",
        "--gcov-ignore-errors=source_not_found",
    ]
    for f in coverage_cfg.get("filters", []):
        cmd.extend(["--filter", f])
    for e in coverage_cfg.get("excludes", []):
        cmd.extend(["--exclude", e])
    cmd.append(str(build_folder))
    _run(cmd)
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


def _append_github_summary(rows: list[tuple[str, float, float, bool]]):
    """Append a markdown table to $GITHUB_STEP_SUMMARY if present."""
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
        lines.append(f"| {layer} | {threshold:.1f}% | {actual:.1f}% | {status} |")
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def run_coverage(toml_file_path: str, version: str, output_dir: str) -> int:
    """Drive a single-build coverage run.

    Returns the process exit code (0 = pass, non-zero = threshold failure).
    """
    toml_file = Path(toml_file_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not toml_file.exists():
        raise FileNotFoundError(f"The specified TOML file does not exist: {toml_file_path}")

    toml_data = _load_toml(toml_file)
    library_name = toml_data["library_name"]
    ci_config = toml_data.get("ci", {})
    coverage_cfg = _coverage_context(toml_data.get("coverage", {}), library_name)
    coverage_python_version = _resolve_coverage_python_version(toml_data)

    if ci_config.get("xvfb"):
        _reexec_under_xvfb()

    # 1. Generate build artifacts.
    _run(
        ["xmsconan_gen", "--version", version, "--output_dir", str(output_dir),
         str(toml_file)],
        cwd=str(output_dir),
    )

    # 2. Single instrumented build: pybind + Debug, pinned to one python
    #    ABI. A test failure inside conan create's test phase must NOT abort
    #    the rest of the run — the artifacts and step summary are most
    #    valuable exactly when a test failed, so we record the failure and
    #    press on.
    #
    #    Notes:
    #      * No ``testing=True`` — the packager never emits a config that
    #        is both ``testing=True`` and ``pybind=True`` (see issue #64),
    #        and pytest-cov against the wheel produces the .gcda files
    #        gcovr needs without a CxxTest build.
    #      * ``python_version`` is pinned so a multi-version fan-out
    #        cannot leave ``_find_coverage_package`` picking whichever
    #        pybind config finished last (issue #65).
    env = os.environ.copy()
    env["XMS_COVERAGE"] = "1"
    # XmsConanPackager.filter_configurations only honors options when
    # nested under "options" — a flat dict here silently drops them
    # (see issue #62), which would widen the build to every Debug config.
    filter_arg = json.dumps({
        "build_type": "Debug",
        "options": {"pybind": True, "python_version": coverage_python_version},
    })
    tests_failed = False
    try:
        _run(
            [sys.executable, "build.py", "--version", version, "--filter", filter_arg],
            env=env, cwd=str(output_dir),
        )
    except subprocess.CalledProcessError as exc:
        tests_failed = True
        LOGGER.error(
            "build.py exited %s during the coverage build; continuing through "
            "gcovr and artifact collection so partial coverage and the step "
            "summary remain available.",
            exc.returncode,
        )

    # 3. Find the build/source folder for the instrumented package.
    exact_ref, pid = _find_coverage_package(library_name, coverage_python_version)
    ref_with_pid = f"{exact_ref}:{pid}"
    build_folder = _conan_cache_path(ref_with_pid, "build")
    source_folder = _conan_cache_path(ref_with_pid, "source")
    LOGGER.info("Coverage build folder:  %s", build_folder)
    LOGGER.info("Coverage source folder: %s", source_folder)

    # 4. Generate C++ coverage report via gcovr.
    cpp_summary = _run_gcovr(source_folder, build_folder, coverage_cfg, output_dir)

    # 5. Locate Python coverage artifacts produced inside the build folder by
    #    run_python_tests, and copy them up to the workspace root. The recipe
    #    writes them into a layout-specific subdirectory (e.g.
    #    ``<build_folder>/build/Debug/``), not the conan-managed build root,
    #    so we walk to find them regardless of depth (see issue #71).
    py_summary = output_dir / "cov-py-summary.json"
    py_summary_src = _find_pytest_cov_artifact(build_folder, "cov-py-summary.json")
    if py_summary_src is not None:
        shutil.copy2(py_summary_src, py_summary)
    py_xml_src = _find_pytest_cov_artifact(build_folder, "cov-py.xml")
    if py_xml_src is not None:
        shutil.copy2(py_xml_src, output_dir / "cov-py.xml")
    py_html_src = _find_pytest_cov_artifact(build_folder, "coverage-html-py")
    py_html_dst = output_dir / "coverage-html-py"
    if py_html_src is not None and py_html_src.is_dir():
        if py_html_dst.exists():
            shutil.rmtree(py_html_dst)
        shutil.copytree(py_html_src, py_html_dst)

    # 6. Threshold gating. Compare raw percentages so a 69.95% build does not
    #    sneak past a 70.0 threshold via display rounding; round only when
    #    formatting for the log line and the GitHub step summary table.
    cpp_raw = _cpp_percent_from_summary(cpp_summary)
    py_raw = _py_percent_from_summary(py_summary) if py_summary.exists() else 0.0
    cpp_threshold = coverage_cfg["cpp_threshold"]
    py_threshold = coverage_cfg["python_threshold"]
    cpp_pass = cpp_raw >= cpp_threshold
    py_pass = py_raw >= py_threshold

    rows = [
        ("C++", cpp_threshold, round(cpp_raw, 1), cpp_pass),
        ("Python", py_threshold, round(py_raw, 1), py_pass),
    ]
    LOGGER.info("Coverage summary:")
    for layer, threshold, actual, passed in rows:
        LOGGER.info("  %s: %.1f%% (threshold %.1f%%) -> %s",
                    layer, actual, threshold, "PASS" if passed else "FAIL")
    _append_github_summary(rows)

    if tests_failed:
        LOGGER.error("Coverage gate FAIL: build.py exited non-zero earlier; "
                     "see the build log above for the failing test(s).")
    return 0 if (cpp_pass and py_pass and not tests_failed) else 1


def main():
    """Entry point for ``xmsconan coverage`` (and the legacy ``xmsconan_coverage`` script)."""
    parser = argparse.ArgumentParser(description="Run xmsconan unified coverage workflow.")
    parser.add_argument(
        "--output_dir", default=".",
        help="Workspace directory the coverage artifacts are written into.",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase output verbosity (use -v for debug details).",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")
    parser.add_argument(
        "--version", default=None,
        help="The build version. If omitted, tries setuptools-scm then falls back to 0.0.0.",
    )
    parser.add_argument(
        "toml_file", nargs="?", default="build.toml",
        help="Path to build.toml. Defaults to ./build.toml.",
    )

    args = parser.parse_args()
    _configure_logging(args)

    from xmsconan.generator_tools.version import resolve_version
    version = resolve_version(args.version)

    try:
        exit_code = run_coverage(args.toml_file, version, args.output_dir)
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
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
