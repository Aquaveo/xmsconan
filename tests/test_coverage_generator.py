"""Tests for coverage_tools.coverage_generator."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.build_toml import BuildToml, CiTable, CoverageTable
from xmsconan.coverage_tools.coverage_generator import (
    _append_github_summary,
    _assert_gcovr_collected_data,
    _BuildLeg,
    _conan_cache_path,
    _cpp_percent_from_summary,
    _find_coverage_package,
    _find_pytest_cov_artifact,
    _is_simple_relative_filter_pattern,
    _py_percent_from_summary,
    _reexec_under_xvfb,
    _resolve_coverage_python_version,
    _resolve_gcovr_filters,
    _run_coverage_builds,
    _warn_if_tracefile_empty,
    DEFAULT_LEG_TIMEOUT,
    EXIT_ERROR,
    EXIT_GATE_FAILED,
    run_coverage,
)
from xmsconan.generator_tools.ci_file_generator import _coverage_context


#: Scoping a caplog capture to this logger keeps an unrelated library's warning
#: from satisfying -- or falsifying -- an assertion. caplog collects every
#: record propagating to root for the whole test, not just the ones emitted
#: inside a with block, so every log assertion here filters on this name.
LOGGER_NAME = "xmsconan.coverage_tools.coverage_generator"


def logged_messages(caplog):
    """Return the messages this module logged, ignoring every other logger."""
    return [r.message for r in caplog.records if r.name == LOGGER_NAME]


class TestCoverageContextDefaults:
    """Defaults baked into _coverage_context match the issue spec."""

    def test_thresholds_default_to_zero(self):
        """Both thresholds default to 0 (report-only mode)."""
        ctx = _coverage_context(CoverageTable(), "xmscore")
        assert ctx["cpp_threshold"] == 0.0
        assert ctx["python_threshold"] == 0.0

    def test_filters_default_to_library_prefix(self):
        """The default gcovr filter scopes to the library's own source tree."""
        ctx = _coverage_context(CoverageTable(), "xmsgrid")
        assert ctx["filters"] == ["xmsgrid/"]

    def test_excludes_baked_in_when_absent(self):
        """Default excludes drop *.t.h and the package tests, but NOT the bindings.

        The binding directory used to be excluded, back when only the testing
        build was read -- that build does not compile the bindings, so the
        exclude removed nothing that existed. The pybind build is instrumented
        and merged in now, so excluding it would collect the binding layer's
        coverage and then throw it away.
        """
        ctx = _coverage_context(CoverageTable(), "xmsgrid")
        excludes = ctx["excludes"]
        assert any(r".*\.t\.h$" in e for e in excludes)
        assert any("_package/tests" in e for e in excludes)
        assert not any("xmsgrid/python" in e for e in excludes)

    def test_user_supplied_filters_win(self):
        """User-supplied filters replace the defaults entirely."""
        config = BuildToml(library_name="xmscore", coverage=CoverageTable(filters=["only/"]))
        ctx = _coverage_context(config.coverage, "xmsgrid")
        assert ctx["filters"] == ["only/"]

    def test_user_supplied_thresholds_win(self):
        """User-supplied thresholds replace the defaults."""
        config = BuildToml(library_name="xmscore", coverage=CoverageTable(cpp_threshold=70.0, python_threshold=65.0))
        ctx = _coverage_context(config.coverage, "xmsgrid")
        assert ctx["cpp_threshold"] == 70.0
        assert ctx["python_threshold"] == 65.0


class TestPercentExtraction:
    """JSON summary parsing for both layers."""

    def test_cpp_percent_from_gcovr_summary(self, tmp_path):
        """Reads line_percent from a gcovr JSON summary."""
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 72.4}))
        assert _cpp_percent_from_summary(summary) == 72.4

    def test_py_percent_from_pytest_cov_summary(self, tmp_path):
        """Reads totals.percent_covered from a pytest-cov JSON summary."""
        summary = tmp_path / "cov-py-summary.json"
        summary.write_text(json.dumps({"totals": {"percent_covered": 81.2}}))
        assert _py_percent_from_summary(summary) == 81.2

    def test_cpp_percent_raises_on_missing_key(self, tmp_path):
        """Schema drift (missing line_percent) raises rather than silently returning 0%."""
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"some_other_key": 1.0}))
        with pytest.raises(ValueError) as exc_info:
            _cpp_percent_from_summary(summary)
        assert str(summary) in str(exc_info.value)
        assert "line_percent" in str(exc_info.value)

    def test_py_percent_raises_on_missing_totals(self, tmp_path):
        """Schema drift (missing totals) raises rather than silently returning 0%."""
        summary = tmp_path / "cov-py-summary.json"
        summary.write_text(json.dumps({}))
        with pytest.raises(ValueError) as exc_info:
            _py_percent_from_summary(summary)
        assert str(summary) in str(exc_info.value)
        assert "percent_covered" in str(exc_info.value)

    def test_py_percent_raises_on_missing_percent_covered(self, tmp_path):
        """Schema drift (totals present but missing percent_covered) raises."""
        summary = tmp_path / "cov-py-summary.json"
        summary.write_text(json.dumps({"totals": {}}))
        with pytest.raises(ValueError) as exc_info:
            _py_percent_from_summary(summary)
        assert str(summary) in str(exc_info.value)
        assert "percent_covered" in str(exc_info.value)


class TestGithubStepSummary:
    """Markdown table append behavior for $GITHUB_STEP_SUMMARY."""

    def test_no_op_when_env_missing(self, tmp_path):
        """Does nothing (and does not raise) when $GITHUB_STEP_SUMMARY is unset."""
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        _append_github_summary([("C++", 70.0, 72.5, True)])

    def test_appends_rows_to_file(self, tmp_path):
        """Appends a markdown table for each row, preserving prior content."""
        summary_path = tmp_path / "summary.md"
        summary_path.write_text("# preamble\n")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary_path)
        try:
            _append_github_summary([
                ("C++", 70.0, 72.5, True),
                ("Python", 70.0, 65.2, False),
            ])
        finally:
            del os.environ["GITHUB_STEP_SUMMARY"]

        contents = summary_path.read_text()
        assert "# preamble" in contents
        assert "Coverage Summary" in contents
        assert "| C++ | 70.0% | 72.5% | PASS |" in contents
        assert "| Python | 70.0% | 65.2% | FAIL |" in contents

    def test_renders_unmeasured_actual_as_na(self, tmp_path):
        """A row whose actual is None renders as unmeasured, not as a percent.

        run_coverage passes None when a layer's build folder went missing, so
        the step summary must not show the narrowed percent as if it were a
        real (merely low) score.
        """
        summary_path = tmp_path / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary_path)
        try:
            _append_github_summary([("C++", 0.0, None, False)])
        finally:
            del os.environ["GITHUB_STEP_SUMMARY"]

        assert "| C++ | 0.0% | n/a (unmeasured) | FAIL |" in summary_path.read_text()


def _fake_conan_list_output(packages, *, library_ref="xmscore/1.0.0"):
    """Build a fake ``conan list --format=json`` stdout payload.

    ``packages`` is a list of dicts shaped like
    ``{"options": {...}, "settings": {...}, "ts": <timestamp>}``. Each
    dict becomes its own revision under ``library_ref`` with a unique
    package id so callers can assert which one was picked.

    Returns a ``MagicMock`` shaped like ``subprocess.run``'s
    ``CompletedProcess`` (only ``.stdout`` and ``.returncode`` are
    accessed by the function under test).
    """
    revisions = {}
    for i, pkg in enumerate(packages):
        revisions[f"rev{i}"] = {
            "timestamp": pkg.get("ts", i),
            "packages": {
                pkg.get("pid", f"pid{i}"): {
                    "info": {
                        "options": pkg.get("options", {}),
                        "settings": pkg.get("settings", {}),
                    }
                },
            },
        }
    return MagicMock(
        stdout=json.dumps({
            "Local Cache": {
                library_ref: {"revisions": revisions},
            },
        }),
        returncode=0,
    )


class TestFindCoveragePackage:
    """Conan cache parsing — newest match per kind, Debug testing / Release pybind."""

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_picks_newest_matching_package(self, mock_run):
        """When multiple revisions match, the highest timestamp wins."""
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Release"},
             "ts": 100, "pid": "old_pid"},
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Release"},
             "ts": 200, "pid": "new_pid"},
        ])
        ref, pid = _find_coverage_package(
            "xmscore", kind="pybind", python_version="3.13",
        )
        assert ref == "xmscore/1.0.0"
        assert pid == "new_pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_raises_when_no_match(self, mock_run):
        """Raises a clear error when the cache has no matching package."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"Local Cache": {}}), returncode=0,
        )
        with pytest.raises(RuntimeError, match="pybind=True"):
            _find_coverage_package(
                "xmscore", kind="pybind", python_version="3.13",
            )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_accepts_bool_and_lowercase_truthy_options(self, mock_run):
        """Conan's option repr is not contractually 'True' — accept bool/case variants."""
        mock_run.return_value = _fake_conan_list_output([
            # bool True/False (not the strings "True"/"False")
            {"options": {"pybind": True, "testing": False,
                         "python_version": "3.13"},
             "settings": {"build_type": "Release"},
             "ts": 100, "pid": "pid"},
        ])
        ref, pid = _find_coverage_package(
            "xmscore", kind="pybind", python_version="3.13",
        )
        assert ref == "xmscore/1.0.0"
        assert pid == "pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_pybind_kind_skips_debug_builds(self, mock_run):
        """kind='pybind' wants Release; a Debug pybind package must not match.

        The build type is per kind. Matching a Debug pybind build here would
        find whatever a library that names Debug in [matrix].pybind_build_types
        happens to have produced, rather than the Release build run_coverage
        asked for.
        """
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Debug"},
             "ts": 100, "pid": "debug_pid"},
        ])
        with pytest.raises(RuntimeError):
            _find_coverage_package(
                "xmscore", kind="pybind", python_version="3.13",
            )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_testing_kind_skips_release_builds(self, mock_run):
        """kind='testing' wants Debug; gcov instruments an unoptimized build."""
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"testing": "True", "pybind": "False"},
             "settings": {"build_type": "Release"},
             "ts": 100, "pid": "release_pid"},
        ])
        with pytest.raises(RuntimeError):
            _find_coverage_package("xmscore", kind="testing")

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_pybind_kind_rejects_combined_testing_true_pybind_true(self, mock_run):
        """kind='pybind' DOES NOT match a combined ``testing=True+pybind=True`` record.

        The new two-build coverage flow (issue #65 follow-up) carves out
        TWO disjoint configs: a Debug ``testing=True+pybind=False`` and a
        Release ``testing=False+pybind=True``. ``kind='pybind'`` must
        specifically pick the latter — a stale combined-config package
        left over from the prior flow must not be silently matched (it
        would conflate the two layers' coverage roles and reintroduce
        non-determinism between the CxxTest and pytest-cov sources).
        """
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"pybind": "True", "testing": "True",
                         "python_version": "3.13"},
             "settings": {"build_type": "Debug"},
             "ts": 100, "pid": "combined_pid"},
        ])
        with pytest.raises(RuntimeError):
            _find_coverage_package(
                "xmscore", kind="pybind", python_version="3.13",
            )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_pins_to_requested_python_version(self, mock_run):
        """Multi-ABI fan-out: cache has both 3.10 and 3.13, only the requested one wins.

        Issue #65: the prior code returned newest-by-timestamp across all
        pybind packages, so whichever Python build finished last drove
        the Python coverage report (non-determinism).
        """
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.10"},
             "settings": {"build_type": "Release"},
             "ts": 999, "pid": "pid_310"},  # newer
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Release"},
             "ts": 100, "pid": "pid_313"},  # older
        ])
        ref, pid = _find_coverage_package(
            "xmscore", kind="pybind", python_version="3.13",
        )
        # Older-by-timestamp wins because it's the requested ABI; the
        # newer 3.10 build must NOT be picked.
        assert pid == "pid_313"

    def test_find_testing_package_matches_testing_only_debug(self, monkeypatch):
        """kind='testing' matches testing=True, pybind=False, Debug. Ignores ABI."""
        monkeypatch.setattr(
            "xmsconan.coverage_tools.coverage_generator.subprocess.run",
            lambda *a, **kw: _fake_conan_list_output([
                {"options": {"testing": "True", "pybind": "False"},
                 "settings": {"build_type": "Debug"}, "ts": 1},
                {"options": {"testing": "False", "pybind": "True",
                             "python_version": "3.13"},
                 "settings": {"build_type": "Release"}, "ts": 2},
            ]),
        )
        ref, pid = _find_coverage_package(
            "xmscore", kind="testing",
        )
        assert ref and pid

    def test_find_pybind_package_matches_pybind_only_pinned_python(self, monkeypatch):
        """kind='pybind' matches testing=False, pybind=True, Release, pinned python."""
        monkeypatch.setattr(
            "xmsconan.coverage_tools.coverage_generator.subprocess.run",
            lambda *a, **kw: _fake_conan_list_output([
                {"options": {"testing": "True", "pybind": "False"},
                 "settings": {"build_type": "Debug"}, "ts": 1},
                {"options": {"testing": "False", "pybind": "True",
                             "python_version": "3.10"},
                 "settings": {"build_type": "Release"}, "ts": 2},
                {"options": {"testing": "False", "pybind": "True",
                             "python_version": "3.13"},
                 "settings": {"build_type": "Release"}, "ts": 3},
            ]),
        )
        ref, pid = _find_coverage_package(
            "xmscore", kind="pybind", python_version="3.13",
        )
        assert ref and pid

    def test_find_pybind_package_requires_python_version(self):
        """kind='pybind' without python_version raises a clear error."""
        with pytest.raises(ValueError, match="python_version"):
            _find_coverage_package("xmscore", kind="pybind")

    def test_find_coverage_package_rejects_unknown_kind(self):
        """Unknown kind values raise rather than silently mis-matching."""
        with pytest.raises(ValueError, match="kind"):
            _find_coverage_package("xmscore", kind="both")


class TestResolveCoveragePythonVersion:
    """Picks the single python_version the coverage build pins to (issue #65)."""

    def test_defaults_to_3_13_when_no_ci_python_versions(self):
        """An empty toml falls back to the global default ABI."""
        config = BuildToml(library_name="xmscore")
        assert _resolve_coverage_python_version(config) == "3.13"

    def test_uses_highest_ci_python_versions(self):
        """Highest entry in [ci].python_versions wins by (major, minor)."""
        config = BuildToml(library_name="xmscore", ci=CiTable(python_versions=["3.10", "3.13"]))
        assert _resolve_coverage_python_version(config) == "3.13"

    def test_handles_list_order_independence(self):
        """Order in [ci].python_versions doesn't matter."""
        config = BuildToml(library_name="xmscore", ci=CiTable(python_versions=["3.13", "3.10"]))
        assert _resolve_coverage_python_version(config) == "3.13"

    def test_explicit_coverage_python_version_overrides(self):
        """[coverage].python_version overrides the [ci].python_versions default."""
        config = BuildToml(
            library_name="xmscore",
            ci=CiTable(python_versions=["3.13"]),
            coverage=CoverageTable(python_version="3.10"),
        )
        assert _resolve_coverage_python_version(config) == "3.10"

    def test_empty_ci_python_versions_falls_back(self):
        """An empty list in [ci] is treated like the key was missing."""
        config = BuildToml(library_name="xmscore", ci=CiTable(python_versions=[]))
        assert _resolve_coverage_python_version(config) == "3.13"

    def test_rejects_a_coverage_python_version_the_recipe_does_not_allow(self):
        """[coverage].python_version is checked against the supported set.

        generate_ci validates the three [ci] lists, but run_coverage calls this
        resolver directly, so an unsupported value here reached the container
        image name and PYTHON_TARGET_VERSION unchecked and only failed at conan
        configure time, inside the build, attributed to the wrong thing.
        """
        config = BuildToml(library_name="xmscore", coverage=CoverageTable(python_version="3.11"))
        with pytest.raises(ValueError, match="python_version option does not allow"):
            _resolve_coverage_python_version(config)

    def test_coerces_a_non_string_version_entry(self):
        """An unquoted TOML version parses as a float and must not stay one.

        ``linux_python_versions = [3.14]`` yields ``[3.14]``, and the fallback
        returns an element of that list verbatim. version_sort_key stringifies
        only for comparison, so without a coercion here the float reaches
        subprocess's env, which rejects non-str values with a bare TypeError.
        """
        config = BuildToml(library_name="xmscore", ci=CiTable(linux_python_versions=[3.14]))
        resolved = _resolve_coverage_python_version(config)
        assert resolved == "3.14"
        assert isinstance(resolved, str), f"resolver returned {type(resolved).__name__}"


class TestConanCachePath:
    """`conan cache path --folder` reference-shape requirements (issue #66).

    Conan 2 requires:
      * a package reference (``ref:pid``) for per-package folders (``build``,
        and the default unnamed package folder),
      * a *recipe* reference (``ref`` only) for ``source``, ``export``, and
        ``export_source`` — those folders are shared across all packages
        built from the same recipe revision so a pid is meaningless and the
        CLI rejects it with ``'--folder source' requires a recipe reference``.

    The previous helper passed whatever shape the caller supplied straight
    through, which broke ``run_coverage``'s ``source_folder`` lookup.
    """

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_strips_pid_for_source_folder(self, mock_run):
        """``--folder=source`` must receive the recipe ref only (no ``:pid``)."""
        mock_run.return_value = MagicMock(stdout="/some/source/path\n")
        _conan_cache_path("xmscore/0.0.0:abc123", "source")
        cmd = mock_run.call_args[0][0]
        assert "xmscore/0.0.0" in cmd
        assert "xmscore/0.0.0:abc123" not in cmd, (
            "source folder lookup must use the recipe reference, not the "
            "package reference — conan rejects ref:pid for --folder=source"
        )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_strips_pid_for_export_folders(self, mock_run):
        """``export`` and ``export_source`` are recipe-scoped too."""
        mock_run.return_value = MagicMock(stdout="/p\n")
        for folder in ("export", "export_source"):
            _conan_cache_path("xmscore/0.0.0:abc123", folder)
            cmd = mock_run.call_args[0][0]
            assert "xmscore/0.0.0:abc123" not in cmd, (
                f"--folder={folder} must use the recipe reference"
            )
            assert "xmscore/0.0.0" in cmd

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_keeps_pid_for_build_folder(self, mock_run):
        """``--folder=build`` is per-package; the pid must remain."""
        mock_run.return_value = MagicMock(stdout="/some/build/path\n")
        _conan_cache_path("xmscore/0.0.0:abc123", "build")
        cmd = mock_run.call_args[0][0]
        assert "xmscore/0.0.0:abc123" in cmd, (
            "build folder lookup must keep the package id — the build "
            "folder is per-package"
        )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_strips_pid_but_preserves_recipe_revision(self, mock_run):
        """Recipe revision (``#<hex>``) must survive the strip.

        Conan 2 references look like
        ``xmscore/0.0.0#<recipe_rev>:<package_id>``. Splitting on the
        first ``:`` keeps the recipe revision intact — which is what
        ``conan cache path --folder=source`` actually needs to resolve
        the source folder of *that revision* (not just the latest
        recipe). Guards against a future "strip everything after ``#``
        too" refactor.
        """
        mock_run.return_value = MagicMock(stdout="/some/source/path\n")
        _conan_cache_path("xmscore/0.0.0#deadbeef:abc123", "source")
        cmd = mock_run.call_args[0][0]
        assert "xmscore/0.0.0#deadbeef" in cmd
        assert "abc123" not in " ".join(cmd), (
            "pid must be stripped, but the recipe revision (after #) must remain"
        )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_returns_path_from_stdout(self, mock_run):
        """The trimmed stdout becomes the returned Path."""
        mock_run.return_value = MagicMock(stdout="  /trimmed/path  \n")
        assert _conan_cache_path("xmscore/0.0.0:abc", "build") == Path("/trimmed/path")


class TestFindPytestCovArtifact:
    """Locate pytest-cov outputs anywhere under the conan build folder (issue #71).

    ``conan cache path --folder=build`` returns the conan-managed build root
    (e.g. ``/.conan2/p/b/xmsXXX/b``), but the recipe's ``run_python_tests``
    writes the coverage artifacts into a *layout-specific* subdirectory
    (e.g. ``<root>/build/Debug/``). The previous code looked at the root
    only and silently fell through the ``if exists()`` guards, defaulting
    ``py_raw`` to 0.0.

    These tests pin the new ``_find_pytest_cov_artifact`` helper so the
    tool tolerates whatever depth the recipe chose.
    """

    def test_finds_artifact_at_root(self, tmp_path):
        """Artifact directly at build_folder/ is returned."""
        artifact = tmp_path / "cov-py-summary.json"
        artifact.write_text("{}", encoding="utf-8")
        assert _find_pytest_cov_artifact(tmp_path, "cov-py-summary.json") == artifact

    def test_finds_artifact_in_layout_subdir(self, tmp_path):
        """Artifact at build_folder/build/Debug/ (the xmscore case) is returned."""
        layout_subdir = tmp_path / "build" / "Debug"
        layout_subdir.mkdir(parents=True)
        artifact = layout_subdir / "cov-py-summary.json"
        artifact.write_text("{}", encoding="utf-8")
        assert _find_pytest_cov_artifact(tmp_path, "cov-py-summary.json") == artifact

    def test_returns_none_when_absent(self, tmp_path):
        """No matching file anywhere under build_folder → None (no exception).

        This is the legitimate ``pybind=False`` case where pytest-cov
        never ran, not an error.
        """
        assert _find_pytest_cov_artifact(tmp_path, "cov-py-summary.json") is None

    def test_picks_newest_on_collision_and_warns(self, tmp_path, caplog):
        """Multiple matches → newest by mtime wins, with a warning logged.

        Multi-build-type folders (e.g. ``Debug`` and ``RelWithDebInfo`` both
        present, perhaps from a stale cache) can each contain their own
        pytest-cov artifacts. Pick the most recent and tell the operator,
        rather than picking silently or raising.
        """
        old_dir = tmp_path / "build" / "Debug"
        new_dir = tmp_path / "build" / "RelWithDebInfo"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        old = old_dir / "cov-py.xml"
        new = new_dir / "cov-py.xml"
        old.write_text("<old/>", encoding="utf-8")
        new.write_text("<new/>", encoding="utf-8")
        # Force old to be older than new by a clear margin.
        old_time = new.stat().st_mtime - 100.0
        os.utime(old, (old_time, old_time))

        with caplog.at_level("WARNING",
                             logger="xmsconan.coverage_tools.coverage_generator"):
            result = _find_pytest_cov_artifact(tmp_path, "cov-py.xml")

        assert result == new
        assert any("Multiple" in rec.message or "multiple" in rec.message.lower()
                   for rec in caplog.records), (
            "a warning naming the collision must be emitted so the operator can fix it"
        )

    def test_kind_dir_filters_out_stale_file_with_same_name(self, tmp_path):
        """A real directory must win over a same-named stale file (PR #72 review).

        The exact scenario the reviewer flagged: a stale leftover *file*
        named ``coverage-html-py`` sitting next to (or anywhere near) the
        real ``coverage-html-py/`` directory. Without ``kind="dir"``,
        ``rglob`` returns both, mtime sort can pick the file, the call
        site's ``is_dir()`` is False, the HTML report is silently
        skipped, and the operator sees no diagnostic. With ``kind="dir"``
        the stale file is filtered out at the helper level and the real
        directory always wins.
        """
        real_dir = tmp_path / "build" / "Debug" / "coverage-html-py"
        real_dir.mkdir(parents=True)
        (real_dir / "index.html").write_text("<html/>", encoding="utf-8")
        stale_file = tmp_path / "cache" / "coverage-html-py"
        stale_file.parent.mkdir(parents=True)
        stale_file.write_text("stale", encoding="utf-8")
        # Make the stale file the newer one — exactly the failure mode
        # described in the review.
        new_time = real_dir.stat().st_mtime + 100.0
        os.utime(stale_file, (new_time, new_time))

        result = _find_pytest_cov_artifact(tmp_path, "coverage-html-py", kind="dir")
        assert result == real_dir
        assert result.is_dir()

    def test_kind_file_filters_out_directories_with_same_name(self, tmp_path):
        """Symmetric guard: a directory must not shadow a same-named real file.

        Less likely than the dir-vs-file failure mode but still possible
        if someone manually created an empty directory whose name
        collides with a pytest-cov artifact. ``kind="file"`` keeps the
        helper symmetric.
        """
        real_file = tmp_path / "build" / "Debug" / "cov-py.xml"
        real_file.parent.mkdir(parents=True)
        real_file.write_text("<coverage/>", encoding="utf-8")
        stale_dir = tmp_path / "cov-py.xml"
        stale_dir.mkdir()
        new_time = real_file.stat().st_mtime + 100.0
        os.utime(stale_dir, (new_time, new_time))

        result = _find_pytest_cov_artifact(tmp_path, "cov-py.xml", kind="file")
        assert result == real_file
        assert result.is_file()

    def test_kind_invalid_value_raises(self, tmp_path):
        """An invalid ``kind`` is a programming error, not a silent fall-through."""
        with pytest.raises(ValueError, match="kind"):
            _find_pytest_cov_artifact(tmp_path, "cov-py.xml", kind="bogus")


class TestResolveGcovrFilters:
    """Filter resolution emits both relative and absolute-anchored forms.

    The anchor is the **build** folder, not the recipe source folder:
    ``cmake_layout()`` copies sources into the build folder before
    compilation, so ``.gcno`` files embed paths under ``build_folder``.
    Anchoring against the conan source folder (the original PR #72
    behavior) silently filtered everything out even when ``.gcno`` and
    ``.gcda`` files were present.

    Emitting both the relative form and the build-folder-anchored form
    is purely additive — gcovr ORs ``--filter`` entries — and guards
    against subtle differences in how gcovr resolves source paths
    across versions.
    """

    def test_simple_relative_filter_emits_both_forms(self):
        """A bare path segment gets both its original AND an anchored copy."""
        build_folder = Path("/conan/p/b/xmsXXX/b")
        out = _resolve_gcovr_filters(["xmscore/"], build_folder)
        assert "xmscore/" in out
        # Anchored form: re.escape of build_folder + "/" + the original
        # Dots in the build folder must be escaped so they're literal.
        assert any(
            "/conan/p/b/xmsXXX/b/xmscore/" in entry
            for entry in out
            if entry != "xmscore/"
        ), f"expected an absolute-anchored copy in {out}"
        # Exactly two entries (one original, one anchored):
        assert len(out) == 2

    def test_anchored_form_escapes_dots_in_build_folder(self):
        """The conan build path contains dots (e.g. ``.conan2``) — they must be escaped.

        Without ``re.escape``, the dots would be regex wildcards and the
        anchored filter would match *anything* in the same position,
        defeating the purpose of anchoring.
        """
        import re as _re
        build_folder = Path("/github/home/.conan2/p/b/xmsXXX/b")
        out = _resolve_gcovr_filters(["xmscore/"], build_folder)
        anchored = [e for e in out if e != "xmscore/"][0]
        # The anchored form must work as a regex against the real absolute path.
        real_path = "/github/home/.conan2/p/b/xmsXXX/b/xmscore/math/math.cpp"
        assert _re.search(anchored, real_path), (
            f"anchored filter {anchored!r} must match the real absolute path"
        )
        # And it must NOT match a near-miss where the dot is a different char,
        # proving the dot was actually escaped:
        near_miss = "/github/home/Xconan2/p/b/xmsXXX/b/xmscore/math/math.cpp"
        assert not _re.search(anchored, near_miss), (
            f"anchored filter {anchored!r} must treat dots as literals"
        )

    def test_regex_pattern_passes_through_unchanged(self):
        """A pattern with regex metacharacters is the user's deliberate choice."""
        build_folder = Path("/conan/p/b/xmsXXX/b")
        out = _resolve_gcovr_filters([r".*/xmscore/.*\.cpp$"], build_folder)
        assert out == [r".*/xmscore/.*\.cpp$"], (
            "regex-looking filters must not be doubled-up — user knows what they want"
        )

    def test_absolute_pattern_passes_through_unchanged(self):
        """An already-absolute pattern is treated as the user's deliberate choice."""
        build_folder = Path("/conan/p/b/xmsXXX/b")
        out = _resolve_gcovr_filters(["/some/abs/path/"], build_folder)
        assert out == ["/some/abs/path/"]

    def test_anchored_pattern_with_caret_passes_through(self):
        """``^``-anchored patterns are explicit regexes and shouldn't be doubled."""
        build_folder = Path("/conan/p/b/xmsXXX/b")
        out = _resolve_gcovr_filters(["^xmscore/"], build_folder)
        assert out == ["^xmscore/"]

    def test_empty_filter_list_returns_empty(self):
        """No filters in, no filters out."""
        assert _resolve_gcovr_filters([], Path("/anywhere")) == []

    def test_mixed_list_handles_each_independently(self):
        """A mix of simple-relative and regex patterns: each treated correctly."""
        build_folder = Path("/conan/p/b/xmsXXX/b")
        out = _resolve_gcovr_filters(
            ["xmscore/", r".*/python/.*"], build_folder,
        )
        # The simple-relative gets doubled (2 entries); the regex stays once (1 entry).
        assert len(out) == 3
        assert "xmscore/" in out
        assert r".*/python/.*" in out


class TestIsSimpleRelativeFilterPattern:
    """Classifier for which filter patterns get the anchored-copy treatment."""

    def test_bare_path_segment_is_simple_relative(self):
        """Plain path segments — the default-filter shape — count as simple relative."""
        assert _is_simple_relative_filter_pattern("xmscore/")
        assert _is_simple_relative_filter_pattern("xmscore")
        assert _is_simple_relative_filter_pattern("subdir/lib/")

    def test_regex_metachars_disqualify(self):
        """Any regex metacharacter signals a user-authored regex — leave it alone."""
        assert not _is_simple_relative_filter_pattern(".*xmscore")
        assert not _is_simple_relative_filter_pattern("xms.*core")
        assert not _is_simple_relative_filter_pattern(r"xmscore/.*\.cpp$")
        assert not _is_simple_relative_filter_pattern("xms(core|grid)")
        assert not _is_simple_relative_filter_pattern("xms?core")

    def test_anchors_disqualify(self):
        """Leading ``^`` / ``/`` / ``(`` are explicit anchors — already user-controlled."""
        assert not _is_simple_relative_filter_pattern("^xmscore")
        assert not _is_simple_relative_filter_pattern("/abs/xmscore")
        assert not _is_simple_relative_filter_pattern("(group)")

    def test_empty_string_is_not_simple_relative(self):
        """An empty pattern is never simple-relative — nothing to anchor."""
        assert not _is_simple_relative_filter_pattern("")


class TestAssertGcovrCollectedData:
    """Loud failure when gcovr returns an empty summary (PR #72 review, option B).

    Before this guard, ``line_total == 0`` silently coerced ``cpp_raw``
    to 0.0 and — with the default ``[coverage].cpp_threshold = 0`` —
    produced a "PASS" with an empty report. The operator had to scroll
    the run log for gcovr's `All coverage data is filtered out` line
    to understand what happened. This guard converts that into a hard
    failure with a diagnostic naming the three most likely causes.
    """

    def test_raises_when_line_total_zero(self, tmp_path):
        """Empty merged result → RuntimeError naming every folder read."""
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 0.0, "line_total": 0}))
        build_folder = tmp_path / "build"
        with pytest.raises(RuntimeError) as exc_info:
            _assert_gcovr_collected_data(
                summary, [(build_folder, ["xmscore/"])],
            )
        msg = str(exc_info.value)
        assert str(build_folder) in msg
        # Diagnostic must name the most likely cause (XMS_COVERAGE / #69):
        assert "XMS_COVERAGE" in msg or "#69" in msg
        # And echo the filters we actually used, so the operator can
        # compare them against the real source paths:
        assert "xmscore/" in msg

    def test_raises_pairs_each_folder_with_its_own_filters(self, tmp_path):
        """Each folder appears with the filters resolved against THAT folder.

        Filters are anchored to one folder's absolute paths, so a message that
        pairs folder A with folder B's filters tells the reader to compare
        patterns against a tree those patterns never described -- pointing at
        the filter cause when the real one is usually missing instrumentation.
        """
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 0.0, "line_total": 0}))
        cpp, py = tmp_path / "cpp-build", tmp_path / "py-build"
        # Sentinel filter values rather than real anchored paths: the message
        # renders folders with str() and filters with repr(), so a filter
        # containing a Windows path would be backslash-escaped and never match
        # a naive substring search. The ordering is what this test is about.
        with pytest.raises(RuntimeError) as exc_info:
            _assert_gcovr_collected_data(summary, [
                (cpp, ["FILTER-FOR-CPP"]),
                (py, ["FILTER-FOR-PY"]),
            ])
        msg = str(exc_info.value)
        # Membership first: str.index raises a bare ValueError naming neither
        # the missing substring nor the message, and a reworded error is the
        # likelier failure here than a wrong order.
        for needle in (str(cpp), str(py), "FILTER-FOR-CPP", "FILTER-FOR-PY"):
            assert needle in msg, f"{needle!r} missing from message:\n{msg}"
        # Each folder is followed by its own filter, before the next folder.
        cpp_at, py_at = msg.index(str(cpp)), msg.index(str(py))
        assert cpp_at < msg.index("FILTER-FOR-CPP") < py_at
        assert py_at < msg.index("FILTER-FOR-PY")

    def test_passes_when_line_total_positive(self, tmp_path):
        """A real measurement (any non-zero line_total) is not an error.

        Includes the legitimate 0%-but-non-zero-lines case: 1000 lines
        instrumented, 0 covered by tests. That's a real result, the
        threshold check handles it, this guard must not interfere.
        """
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 0.0, "line_total": 1000}))
        _assert_gcovr_collected_data(summary, [(tmp_path, ["xmscore/"])])

    def test_passes_when_line_total_key_absent(self, tmp_path):
        """Schema drift (no ``line_total`` at all) is left to the percent extractor.

        ``_cpp_percent_from_summary`` already raises a clear
        ``ValueError`` on missing keys; this guard doesn't double up.
        """
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 50.0}))
        _assert_gcovr_collected_data(summary, [(tmp_path, ["xmscore/"])])


class TestWarnIfTracefileEmpty:
    """A build folder that contributes nothing must say so in the log.

    The merged report is what fails the run, and it stays healthy on one
    folder's data alone -- so a folder silently dropping out of the merge is
    invisible in the number. The binding layer simply disappears from the
    report with no other signal.
    """

    _messages = staticmethod(logged_messages)

    def test_warns_when_no_files(self, tmp_path, caplog):
        """An empty file list warns, naming the folder and its own filters."""
        tracefile = tmp_path / "trace.json"
        tracefile.write_text(json.dumps({"files": []}))
        folder = tmp_path / "py-build"
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            _warn_if_tracefile_empty(tracefile, folder, ["FILTER-SENTINEL"])
        text = " ".join(self._messages(caplog))
        assert str(folder) in text
        assert "FILTER-SENTINEL" in text
        # Points at instrumentation, the actual cause, rather than at tests
        # not covering the code -- a compiled file appears here regardless.
        assert "XMS_COVERAGE" in text or "#69" in text

    def test_silent_when_files_present(self, tmp_path, caplog):
        """A folder that contributed files is not worth mentioning.

        Zero *coverage* is not zero *files*: .gcno is written at compile time,
        so a compiled-but-never-executed file still appears. Warning on that
        would fire for every library whose bindings a test never exercises.
        """
        tracefile = tmp_path / "trace.json"
        tracefile.write_text(json.dumps({
            "files": [{"file": "xmscore/foo.cpp", "lines": []}],
        }))
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            _warn_if_tracefile_empty(tracefile, tmp_path, ["x/"])
        assert self._messages(caplog) == []

    def test_unreadable_tracefile_warns_without_raising(self, tmp_path, caplog):
        """A tracefile that cannot be read warns instead of raising.

        Covers the missing case only. gcovr writes this file and ``_run``
        raises on a non-zero exit, so the other unreadable cases need gcovr
        to both succeed and write something unusable.
        """
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            _warn_if_tracefile_empty(tmp_path / "absent.json", tmp_path, [])
        assert any("absent.json" in m for m in self._messages(caplog))


def _is_tracefile_run(call):
    """True when a mocked subprocess call is a gcovr per-folder tracefile run."""
    cmd = call.args[0]
    if not isinstance(cmd, list) or cmd[:1] != ["gcovr"]:
        return False
    return "--json" in cmd and "--json-summary" not in cmd


class TestRunCoverageEndToEnd:
    """End-to-end ``run_coverage`` behavior with all subprocess calls mocked.

    Threshold gating is only four of these; the rest cover the build.py
    invocations (filter shape, ABI plumbing), gcovr anchoring, artifact
    discovery and build-failure resilience. They share ``_setup_workspace``
    because they all need the same faked two-build workspace, which is what
    holds them in one class rather than the gating subject.
    """

    def _setup_workspace(self, tmp_path, *, cpp_percent, py_percent,
                         python_versions=None, linux_python_versions=None,
                         coverage_python_version=None):
        """Create a workspace with build.toml and fake coverage outputs.

        Two build folders are created — one mirrors the C++ coverage
        build (testing=True, pybind=False) and the other the Python
        coverage build (pybind=True, testing=False). pytest-cov
        artifacts are staged inside the pybind folder because that's
        where ``run_python_tests`` writes them under the two-build flow.

        ``python_versions``, ``linux_python_versions`` and
        ``coverage_python_version`` feed the three inputs
        ``_resolve_coverage_python_version`` reads, lowest precedence first.
        All default to omitting their key entirely, so a caller that passes
        none gets the same build.toml this helper has always written.
        """
        toml_file = tmp_path / "build.toml"
        ci_entries = []
        if python_versions is not None:
            entries = ", ".join(f'"{v}"' for v in python_versions)
            ci_entries.append(f"python_versions = [{entries}]")
        if linux_python_versions is not None:
            entries = ", ".join(f'"{v}"' for v in linux_python_versions)
            ci_entries.append(f"linux_python_versions = [{entries}]")
        ci_body = "\n".join(ci_entries)
        ci_section = f"\n[ci]\n{ci_body}\n" if ci_entries else ""
        coverage_pin = ""
        if coverage_python_version is not None:
            coverage_pin = f'python_version = "{coverage_python_version}"\n'
        toml_file.write_text(
            'library_name = "xmscore"\n'
            'description = "desc"\n'
            'python_namespaced_dir = "core"\n'
            f'{ci_section}'
            '\n'
            '[coverage]\n'
            'cpp_threshold = 70\n'
            'python_threshold = 70\n'
            f'{coverage_pin}',
            encoding="utf-8",
        )
        cpp_build_folder = tmp_path / "fake-cpp-build"
        py_build_folder = tmp_path / "fake-py-build"
        cpp_build_folder.mkdir()
        py_build_folder.mkdir()
        # pytest-cov artifacts live under the pybind build folder.
        (py_build_folder / "cov-py-summary.json").write_text(
            json.dumps({"totals": {"percent_covered": py_percent}})
        )
        (py_build_folder / "cov-py.xml").write_text("<coverage/>")

        # gcovr is mocked. It is invoked once per build folder to write a
        # tracefile (--json), then once more to merge them into the reports
        # (--json-summary), so the fake has to serve both shapes.
        def fake_run(cmd, env=None, cwd=None, **_kw):
            if isinstance(cmd, list) and cmd and cmd[0] == "gcovr":
                if "--json-summary" in cmd:
                    idx = cmd.index("--json-summary")
                    Path(cmd[idx + 1]).write_text(
                        json.dumps({
                            "line_percent": cpp_percent,
                            "line_total": 100,
                        }),
                    )
                if "--json" in cmd:
                    # Non-empty: a real gcovr run reports every file it found
                    # .gcno for, whether or not a test executed any of it. An
                    # empty list here would trip _warn_if_tracefile_empty in
                    # every end-to-end test, and a warning that always fires
                    # is one nobody reads when it means something.
                    idx = cmd.index("--json")
                    Path(cmd[idx + 1]).write_text(json.dumps({
                        "files": [{"file": "xmscore/foo.cpp", "lines": []}],
                    }))
            return MagicMock(returncode=0)

        return toml_file, cpp_build_folder, py_build_folder, fake_run

    def _capture_build_envs(self, tmp_path, mock_run, mock_path, mock_find,
                            **toml_kwargs):
        """Run coverage and return the ``(cmd, env)`` pair per build.py call.

        The threshold tests only ever needed the command; the ABI plumbing is
        carried in the environment, so these capture both.
        """
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=80.0, py_percent=80.0, **toml_kwargs,
            )
        )
        captured = []

        def capture(cmd, env=None, cwd=None, **kw):
            # Snapshot the env, don't store the reference: run_coverage builds
            # one dict and hands the same object to both build.py calls, so
            # keeping the reference would make every per-build assertion read
            # the same post-run state -- and a change that set the ABI for only
            # one of the two invocations would still pass.
            captured.append((
                list(cmd) if isinstance(cmd, list) else cmd,
                dict(env) if env is not None else None,
            ))
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        return [
            (cmd, env) for cmd, env in captured
            if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "build.py"
        ]

    @staticmethod
    def _pybind_filter(builds):
        """Return the ``--filter`` dict of the build.py call that builds the wheel.

        Selected by the filter's own ``pybind`` option rather than by position.
        ``run_coverage``'s call order is an implementation detail, and indexing
        would silently retarget these assertions at the C++ build the moment
        the two invocations were reordered.
        """
        for cmd, _env in builds:
            build_filter = json.loads(cmd[cmd.index("--filter") + 1])
            if build_filter.get("options", {}).get("pybind"):
                return build_filter
        raise AssertionError(f"no pybind build.py invocation among {builds!r}")

    @staticmethod
    def _assert_every_build_pins(builds, version):
        """Assert both build.py calls carry ``PYTHON_TARGET_VERSION=version``.

        Asserting per build rather than once on the shared env is the point:
        ``run_coverage`` hands the same dict to both invocations today, and a
        change that pinned only one of them has to fail here, naming the leg.
        """
        assert len(builds) == 2, "run_coverage must invoke build.py twice"
        for cmd, env in builds:
            assert env is not None, f"build.py invocation {cmd!r} got no explicit env"
            assert env.get("PYTHON_TARGET_VERSION") == version, (
                f"build.py invocation {cmd!r} carried "
                f"PYTHON_TARGET_VERSION={env.get('PYTHON_TARGET_VERSION')!r}, "
                f"expected {version!r}; the matrix it generates cannot satisfy "
                "the --filter without it."
            )

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_passes_when_both_layers_meet_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Exits 0 when C++ and Python percentages both clear their thresholds."""
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=72.5, py_percent=71.2,
            )
        )
        mock_run.side_effect = fake_run
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == 0

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_when_cpp_under_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Fails the gate (exit 3) when C++ percentage is below cpp_threshold."""
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=65.0, py_percent=85.0,
            )
        )
        mock_run.side_effect = fake_run
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == EXIT_GATE_FAILED

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_when_python_under_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Fails the gate (exit 3) when Python percentage is below python_threshold."""
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=85.0, py_percent=50.0,
            )
        )
        mock_run.side_effect = fake_run
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == EXIT_GATE_FAILED

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_when_raw_just_below_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """A raw 69.95% must not pass a 70.0 threshold via display rounding."""
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=69.95, py_percent=99.0,
            )
        )
        mock_run.side_effect = fake_run
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == EXIT_GATE_FAILED, \
            "69.95% must not satisfy a 70.0 threshold"

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_reports_partial_coverage_when_one_leg_left_no_package(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """A leg that built nothing degrades to partial coverage, not a crash.

        `_run_coverage_builds` promises that a failed leg is "reported, not
        raised" so "partial coverage remains available". The lookup that
        followed was unguarded, so a failed leg raised RuntimeError out of
        run_coverage and the surviving leg's report was lost with it -- the
        fallback was unreachable in exactly the case it existed for.
        """
        toml_file, cpp_build_folder, _py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=95.0, py_percent=95.0,
            )
        )
        mock_run.side_effect = fake_run

        def find(library_name, *, kind, python_version=None):
            if kind == "pybind":
                raise RuntimeError("no pybind package in the local Conan cache")
            return ("xmscore/0.0.0", "pid-cpp")

        mock_find.side_effect = find
        mock_path.side_effect = lambda ref_with_pid, folder: cpp_build_folder

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        # The gate still fails -- an unmeasured Python layer is not a pass --
        # but it fails as a gate, having produced the C++ half of the report.
        assert exit_code == EXIT_GATE_FAILED

        tracefiles = [call for call in mock_run.call_args_list
                      if _is_tracefile_run(call)]
        assert len(tracefiles) == 1, (
            "gcovr should have read the one surviving build folder, not the "
            f"missing one as well; got {len(tracefiles)} tracefile runs"
        )
        cmd = tracefiles[0].args[0]
        assert cmd[cmd.index("--root") + 1] == str(cpp_build_folder), \
            "the tracefile run should target the surviving C++ build folder"

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_gate_when_testing_leg_left_no_package(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """A missing testing leg cannot pass the C++ gate on the pybind leg alone.

        gcovr merges whatever folders survived, so with the testing folder
        missing the C++ percent silently narrows to the binding layer, and
        cpp_threshold defaults to 0 -- without the measured-guard this
        returned EXIT_OK with the C++ layer mostly unmeasured. Both
        thresholds are met and the pybind leg is intact here, so only the
        missing C++ measurement can be what fails the gate.
        """
        toml_file, _cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=95.0, py_percent=95.0,
            )
        )
        mock_run.side_effect = fake_run

        def find(library_name, *, kind, python_version=None):
            if kind == "testing":
                raise RuntimeError("no testing package in the local Conan cache")
            return ("xmscore/0.0.0", "pid-py")

        mock_find.side_effect = find
        mock_path.side_effect = lambda ref_with_pid, folder: py_build_folder

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == EXIT_GATE_FAILED

        tracefiles = [call for call in mock_run.call_args_list
                      if _is_tracefile_run(call)]
        assert len(tracefiles) == 1, (
            "gcovr should have read the one surviving build folder, not the "
            f"missing one as well; got {len(tracefiles)} tracefile runs"
        )
        cmd = tracefiles[0].args[0]
        assert cmd[cmd.index("--root") + 1] == str(py_build_folder), \
            "the tracefile run should target the surviving pybind build folder"

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_errors_when_neither_leg_left_a_package(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Both legs missing is a tool failure, not a coverage gate failure.

        There is no .gcda to report on, so writing a report would publish 0%
        for a run that never measured anything. It exits EXIT_ERROR rather
        than EXIT_GATE_FAILED so the generated CI's allow_failure -- which
        forgives only the gate -- still fails the job.
        """
        toml_file, _cpp, _py, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=95.0, py_percent=95.0,
        )
        mock_run.side_effect = fake_run

        def find(library_name, *, kind, python_version=None):
            raise RuntimeError(
                f"No {kind} package found for xmscore in the local Conan "
                f"cache. Did the {kind} coverage build complete?"
            )

        mock_find.side_effect = find
        mock_path.side_effect = AssertionError(
            "no package was found, so no cache path should be resolved"
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == EXIT_ERROR

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_xmsconan_gen_called_with_explicit_output_dir(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """run_coverage passes --output_dir to xmsconan_gen instead of relying on cwd."""
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=80.0, py_percent=80.0,
            )
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        gen_cmds = [c for c in captured_cmds
                    if isinstance(c, list) and c and c[0] == "xmsconan_gen"]
        assert gen_cmds, "xmsconan_gen should have been invoked"
        gen_cmd = gen_cmds[0]
        assert "--output_dir" in gen_cmd, (
            f"xmsconan_gen must be invoked with --output_dir; got {gen_cmd}"
        )

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_py_filter_uses_nested_options_shape(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """The --filter passed to build.py nests its option keys under "options".

        XmsConanPackager.filter_configurations silently dropped flat top-level
        keys before issue #62 was fixed (the packager now raises on unknown
        top-level keys, but this test pins the call site too so the
        regression cannot come back via the coverage tool). Checked on
        BOTH build.py invocations (testing-only and pybind-only).
        """
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=80.0, py_percent=80.0,
            )
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        build_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and len(c) >= 2 and c[1] == "build.py"
        ]
        assert build_cmds, "build.py should have been invoked"
        for cmd in build_cmds:
            filter_idx = cmd.index("--filter")
            filter_dict = json.loads(cmd[filter_idx + 1])
            # Debug for the C++ build gcov instruments, Release for the pybind
            # build that only produces pytest-cov output.
            expected = (
                "Release" if filter_dict["options"].get("pybind") else "Debug"
            )
            assert filter_dict["build_type"] == expected
            # Options must live under "options", never at the top level.
            assert "pybind" not in filter_dict
            assert "testing" not in filter_dict
            assert "python_version" not in filter_dict

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_py_invoked_twice_with_disjoint_filters(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """run_coverage drives TWO build.py invocations: testing-only, then pybind-only.

        Both flags appearing in the same Conan config is what produced
        the recurring pybind-dlopen / CxxTest-symbol regressions. The split
        moves CxxTest into one Conan create and pytest-cov into a different
        Conan create so the two binary shapes never collide.
        """
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=80.0, py_percent=80.0,
            )
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture

        # _find_coverage_package is called once per kind; route by kind.
        def find(library_name, *, kind, python_version=None):
            if kind == "testing":
                return ("xmscore/0.0.0", "pid-cpp")
            return ("xmscore/0.0.0", "pid-py")
        mock_find.side_effect = find

        # _conan_cache_path returns the corresponding build folder per pid.
        def cache_path(ref_with_pid, folder):
            if "pid-cpp" in ref_with_pid:
                return cpp_build_folder
            return py_build_folder
        mock_path.side_effect = cache_path

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        build_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and len(c) >= 2 and c[1] == "build.py"
        ]
        assert len(build_cmds) == 2, (
            f"run_coverage must invoke build.py exactly twice (testing-only, "
            f"then pybind-only); got {len(build_cmds)} invocation(s)."
        )

        filter_dicts = []
        for cmd in build_cmds:
            filter_idx = cmd.index("--filter")
            filter_dicts.append(json.loads(cmd[filter_idx + 1]))

        # First build is the testing-only one (C++ coverage).
        assert filter_dicts[0]["options"]["testing"] is True
        assert filter_dicts[0]["options"]["pybind"] is False
        assert filter_dicts[0]["build_type"] == "Debug"

        # Second build is the pybind-only one (Python coverage), pinned to one
        # Python ABI, and Release rather than Debug: Debug+pybind is the one
        # combination the xms libraries do not publish, so requiring it left
        # every dependency short a binary. Release costs nothing in line data
        # because the XMS_COVERAGE CMake block appends -O0 -g after CMake's
        # -O3. This folder IS read -- see the collection-roots assertion below.
        assert filter_dicts[1]["options"]["pybind"] is True
        assert filter_dicts[1]["options"]["testing"] is False
        assert filter_dicts[1]["build_type"] == "Release"
        assert "python_version" in filter_dicts[1]["options"]

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_gcovr_merges_both_build_folders(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Run gcovr against BOTH build folders, then merge the tracefiles.

        The testing build carries what CxxTest reaches; the pybind build
        carries the binding layer and anything only a Python test exercises.
        Each is read with its own ``--root`` — one invocation cannot span two
        roots — and the tracefiles are combined with ``--add-tracefile`` so hit
        counts sum per line.

        Asserted over every gcovr invocation rather than just the first: a
        check that only looked at ``gcovr_cmds[0]`` would keep passing if the
        pybind folder were silently dropped again, since the testing folder is
        read first either way.
        """
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=80.0, py_percent=80.0,
            )
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture

        def find(library_name, *, kind, python_version=None):
            return ("xmscore/0.0.0",
                    "pid-cpp" if kind == "testing" else "pid-py")
        mock_find.side_effect = find

        def cache_path(ref_with_pid, folder):
            if "pid-cpp" in ref_with_pid:
                return cpp_build_folder
            return py_build_folder
        mock_path.side_effect = cache_path

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        gcovr_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and c and c[0] == "gcovr"
        ]
        assert gcovr_cmds, "gcovr should have been invoked"

        # One collection run per build folder, each rooted at its own folder.
        collect_roots = [
            cmd[cmd.index("--root") + 1]
            for cmd in gcovr_cmds if "--json" in cmd
        ]
        assert collect_roots == [str(cpp_build_folder), str(py_build_folder)], (
            f"gcovr must read both build folders; collected roots were "
            f"{collect_roots!r}"
        )

        # One merge run combining every tracefile the collection runs wrote.
        merge_cmds = [cmd for cmd in gcovr_cmds if "--json-summary" in cmd]
        assert len(merge_cmds) == 1, (
            f"expected exactly one merge invocation; got {len(merge_cmds)}"
        )
        tracefiles = [
            merge_cmds[0][i + 1]
            for i, arg in enumerate(merge_cmds[0]) if arg == "--add-tracefile"
        ]
        assert len(tracefiles) == 2, (
            f"the merge must combine one tracefile per build folder; got "
            f"{tracefiles!r}"
        )

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_gcovr_root_and_filter_anchor_against_build_folder(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Gcovr's --root and the doubled-filter anchor are the BUILD folder.

        ``cmake_layout()`` copies sources into the build folder before
        compilation, so ``.gcno`` files embed paths under
        ``build_folder`` — never under the conan source folder. gcovr's
        ``--filter`` is ``re.match``-style (anchored at the start of an
        absolute path), so anchoring the doubled filter form against the
        recipe-scoped source folder (the prior behavior) never matched
        any real ``.gcno`` path and gcovr silently filtered every file
        out, even when ``.gcno``/``.gcda`` data was present.
        """
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=80.0, py_percent=80.0,
            )
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        gcovr_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and c and c[0] == "gcovr"
        ]
        assert gcovr_cmds, "gcovr should have been invoked"
        cmd = gcovr_cmds[0]

        # 1. --root must be the C++ (testing) build folder.
        root_idx = cmd.index("--root")
        assert cmd[root_idx + 1] == str(cpp_build_folder), (
            f"--root must be {cpp_build_folder!s}, got {cmd[root_idx + 1]!r}. "
            "Anchoring against the source folder filters out every file "
            "since .gcno paths live under build_folder."
        )

        # 2. The doubled --filter form must anchor against the build folder.
        filter_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--filter"]
        anchored = [
            f for f in filter_args
            if f != "xmscore/" and "xmscore/" in f
        ]
        assert anchored, (
            "expected a build-folder-anchored copy of the default filter "
            f"among --filter entries; got {filter_args!r}"
        )
        # re.escape on a path with literal characters produces the same
        # path; the key invariant is that the anchored form starts with
        # an escaped build_folder prefix.
        import re as _re
        for entry in anchored:
            real_source = f"{cpp_build_folder.as_posix()}/xmscore/math/math.cpp"
            assert _re.search(entry, real_source), (
                f"anchored filter {entry!r} must match a real .gcno-style "
                f"absolute path under the build folder; tested against "
                f"{real_source!r}"
            )

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_py_filter_pins_python_version(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """The pybind --filter pins a python_version from [ci].python_versions (issue #65)."""
        builds = self._capture_build_envs(
            tmp_path, mock_run, mock_path, mock_find,
            python_versions=["3.10", "3.13"],
        )
        assert len(builds) == 2, "run_coverage must invoke build.py twice"
        # Highest of ["3.10", "3.13"] is 3.13.
        assert self._pybind_filter(builds)["options"]["python_version"] == "3.13"
        # _find_coverage_package must be told to pin to the same ABI for the
        # pybind kind so the lookup matches what the build produced.
        pybind_calls = [
            c for c in mock_find.call_args_list
            if c.kwargs.get("kind") == "pybind"
        ]
        assert pybind_calls, "_find_coverage_package must be called with kind='pybind'"
        assert pybind_calls[0].kwargs.get("python_version") == "3.13"

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_env_pins_python_target_version(
        self, mock_run, mock_path, mock_find, tmp_path, monkeypatch,
    ):
        """Both coverage builds are told which ABI to *produce*, not just which to keep.

        ``--filter`` narrows the matrix build.py generates; it cannot introduce
        an option value the matrix lacks. With no ``PYTHON_TARGET_VERSION`` in
        the child env the packager falls back to ``DEFAULT_PYTHON_VERSIONS``
        (``["3.13"]``), so a coverage ABI of 3.14 filtered a 3.13-only matrix
        down to zero configurations and the pybind package lookup then raised.

        The delenv is what makes this a regression detector rather than a
        coincidence: ``run_coverage`` seeds the child env from ``os.environ``,
        and USAGE.md tells operators to export this very variable, so on a
        developer machine already exporting 3.14 the assertion below would
        hold with the export removed from the tool entirely.
        """
        monkeypatch.delenv("PYTHON_TARGET_VERSION", raising=False)
        builds = self._capture_build_envs(
            tmp_path, mock_run, mock_path, mock_find,
            linux_python_versions=["3.14"],
        )
        self._assert_every_build_pins(builds, "3.14")

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_env_python_target_version_follows_explicit_coverage_override(
        self, mock_run, mock_path, mock_find, tmp_path, monkeypatch,
    ):
        """The exported ABI tracks ``[coverage].python_version``, same as the --filter.

        Pinning both to the single resolver is the point of the assertion: an
        env var that disagreed with the filter would build the matrix for one
        ABI and then search it for another. Cleared from the environment for
        the same reason as the sibling test above.
        """
        monkeypatch.delenv("PYTHON_TARGET_VERSION", raising=False)
        builds = self._capture_build_envs(
            tmp_path, mock_run, mock_path, mock_find,
            linux_python_versions=["3.14"], coverage_python_version="3.13",
        )
        self._assert_every_build_pins(builds, "3.13")

        py_filter = self._pybind_filter(builds)
        assert py_filter["options"]["python_version"] == "3.13", (
            "the pybind --filter and PYTHON_TARGET_VERSION must come from the "
            "same resolver or they can drift apart again."
        )

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_env_overrides_ambient_python_target_version(
        self, mock_run, mock_path, mock_find, tmp_path, monkeypatch, caplog,
    ):
        """build.toml wins over an operator's exported PYTHON_TARGET_VERSION, audibly.

        ``run_coverage`` seeds the child env from ``os.environ``, so an ambient
        value would otherwise survive into build.py and select an ABI the
        --filter goes on to reject. Overriding it silently is its own failure:
        the operator asked for one ABI and got another with no signal, so the
        warning is part of the behavior, not decoration.
        """
        monkeypatch.setenv("PYTHON_TARGET_VERSION", "3.10")
        caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
        builds = self._capture_build_envs(
            tmp_path, mock_run, mock_path, mock_find,
            linux_python_versions=["3.14"],
        )
        # Filtered to this module's logger: caplog collects everything that
        # propagates to root, so matching on caplog.text would let an unrelated
        # library emitting the same substring satisfy the assertion.
        warnings = logged_messages(caplog)
        assert any("Ignoring PYTHON_TARGET_VERSION=3.10" in m for m in warnings), (
            "an overridden ambient ABI must be reported, not discarded silently; "
            f"captured warnings were {warnings!r}"
        )
        self._assert_every_build_pins(builds, "3.14")

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_failure_still_produces_artifacts(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """A failing build.py does not abort gcovr or artifact collection.

        Coverage artifacts and the step summary are most valuable when a test
        failed; the tool exits non-zero but only after producing them.
        """
        toml_file, cpp_build_folder, py_build_folder, fake_run = (
            self._setup_workspace(
                tmp_path, cpp_percent=99.0, py_percent=99.0,
            )
        )

        def run_with_build_failure(cmd, env=None, cwd=None, **kw):
            is_build_py = isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "build.py"
            if is_build_py:
                raise subprocess.CalledProcessError(1, cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = run_with_build_failure
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        assert exit_code != 0, "Build failure must surface as non-zero exit"
        assert (tmp_path / "cov-cpp-summary.json").exists(), (
            "gcovr summary must be produced even when the build step failed"
        )

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_finds_python_artifacts_in_layout_subdir(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """run_coverage locates pytest-cov outputs under build_folder/build/<type>/.

        Production reality: the recipe's ``run_python_tests`` writes
        ``cov-py-summary.json`` into ``<conan-build-root>/build/Debug/``,
        not at the conan-build-root itself. The earlier code looked at the
        root only, silently fell through ``if exists()``, and defaulted
        ``py_raw`` to 0.0 (issue #71). This test pins the layout-subdir
        path so the regression can't come back.
        """
        toml_file = tmp_path / "build.toml"
        toml_file.write_text(
            'library_name = "xmscore"\n'
            'description = "desc"\n'
            'python_namespaced_dir = "core"\n'
            '\n'
            '[coverage]\n'
            'cpp_threshold = 0\n'
            'python_threshold = 70\n',
            encoding="utf-8",
        )
        cpp_build_folder = tmp_path / "fake-cpp-build"
        py_build_folder = tmp_path / "fake-py-build"
        cpp_build_folder.mkdir()
        # Layout-specific subdir under the *pybind* build folder: under the
        # two-build flow, pytest-cov artifacts live in the pybind build.
        layout_subdir = py_build_folder / "build" / "Debug"
        layout_subdir.mkdir(parents=True)
        # Stage pytest-cov artifacts at the *deep* path:
        (layout_subdir / "cov-py-summary.json").write_text(
            json.dumps({"totals": {"percent_covered": 82.5}})
        )
        (layout_subdir / "cov-py.xml").write_text("<coverage/>")
        (layout_subdir / "coverage-html-py").mkdir()
        (layout_subdir / "coverage-html-py" / "index.html").write_text(
            "<html>py</html>",
        )

        def fake_run(cmd, env=None, cwd=None, **_kw):
            if isinstance(cmd, list) and cmd and cmd[0] == "gcovr":
                if "--json-summary" in cmd:
                    idx = cmd.index("--json-summary")
                    Path(cmd[idx + 1]).write_text(
                        json.dumps({"line_percent": 99.0, "line_total": 100}),
                    )
                if "--json" in cmd:
                    # Non-empty: a real gcovr run reports every file it found
                    # .gcno for, whether or not a test executed any of it. An
                    # empty list here would trip _warn_if_tracefile_empty in
                    # every end-to-end test, and a warning that always fires
                    # is one nobody reads when it means something.
                    idx = cmd.index("--json")
                    Path(cmd[idx + 1]).write_text(json.dumps({
                        "files": [{"file": "xmscore/foo.cpp", "lines": []}],
                    }))
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        mock_find.side_effect = lambda library_name, *, kind, python_version=None: (
            ("xmscore/0.0.0", "pid-cpp" if kind == "testing" else "pid-py")
        )
        mock_path.side_effect = lambda ref_with_pid, folder: (
            cpp_build_folder if "pid-cpp" in ref_with_pid else py_build_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        # The summary, the xml, and the html dir must all have been hoisted
        # up to the workspace root from their deep layout location.
        assert (tmp_path / "cov-py-summary.json").exists(), (
            "run_coverage must find cov-py-summary.json under build_folder/build/Debug/"
        )
        assert (tmp_path / "cov-py.xml").exists()
        assert (tmp_path / "coverage-html-py" / "index.html").exists()
        # And the percentage must reflect the real 82.5% from the staged file,
        # not the silent 0.0 default that came back when the tool looked at
        # the wrong depth.
        assert exit_code == 0, (
            "82.5% must satisfy the 70.0 python_threshold — getting non-zero "
            "exit means the artifact wasn't found and py_raw fell back to 0.0"
        )


def test_run_coverage_raises_on_missing_toml(tmp_path):
    """A missing build.toml surfaces a FileNotFoundError immediately."""
    with pytest.raises(FileNotFoundError):
        run_coverage(str(tmp_path / "missing.toml"), "0.0.0", str(tmp_path))


class TestMainErrorHandling:
    """main()'s error path preserves captured stderr and the traceback."""

    def test_calledprocesserror_stderr_surfaces(self, capsys, monkeypatch):
        """A CalledProcessError carrying conan stderr is printed, not swallowed."""
        from xmsconan.coverage_tools import coverage_generator

        boom = subprocess.CalledProcessError(
            1, ["conan", "list"], output="", stderr="ERROR: conan list blew up\n",
        )
        monkeypatch.setattr(
            coverage_generator, "run_coverage",
            MagicMock(side_effect=boom),
        )
        monkeypatch.setattr(
            "sys.argv", ["xmsconan_coverage", "build.toml"],
        )

        with pytest.raises(SystemExit) as exc_info:
            coverage_generator.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        # Original conan stderr must reach the operator.
        assert "ERROR: conan list blew up" in captured.err
        # Full traceback should be present (frame from main() is enough proof).
        assert "Traceback" in captured.err

    def test_generic_exception_prints_traceback(self, capsys, monkeypatch):
        """Non-CalledProcessError failures still get a traceback (not just str(e))."""
        from xmsconan.coverage_tools import coverage_generator

        monkeypatch.setattr(
            coverage_generator, "run_coverage",
            MagicMock(side_effect=RuntimeError("kapow")),
        )
        monkeypatch.setattr(
            "sys.argv", ["xmsconan_coverage", "build.toml"],
        )

        with pytest.raises(SystemExit) as exc_info:
            coverage_generator.main()

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "kapow" in captured.err
        assert "Traceback" in captured.err


class TestReexecUnderXvfb:
    """The xvfb re-exec has to name a target the interpreter can actually run."""

    def test_reexecs_through_the_module_not_argv0(self, monkeypatch):
        """`python -m <module>`, because argv[0] is not a path.

        The `xmsconan` dispatcher rewrites sys.argv[0] to the literal
        "xmsconan coverage", so re-execing `python sys.argv[0]` asked the
        interpreter to open a file by that name. Every ci.xvfb=true coverage
        run died there.
        """
        recorded = {}

        def _record(file, args, env):
            recorded["file"] = file
            recorded["args"] = args
            recorded["env"] = env

        monkeypatch.delenv("XMSCONAN_COVERAGE_XVFB_REEXEC", raising=False)
        monkeypatch.setattr(
            "sys.argv", ["xmsconan coverage", "--version", "1.0.0", "build.toml"],
        )
        with patch("xmsconan.coverage_tools.coverage_generator.shutil.which",
                   return_value="/usr/bin/xvfb-run"), \
             patch("xmsconan.coverage_tools.coverage_generator.os.execvpe", _record):
            _reexec_under_xvfb()

        args = recorded["args"]
        assert args[0] == "/usr/bin/xvfb-run"
        assert "xmsconan coverage" not in args
        assert args[args.index("-m") - 1] == sys.executable
        assert args[args.index("-m") + 1] == "xmsconan.coverage_tools.coverage_generator"
        assert args[args.index("-m") + 2:] == ["--version", "1.0.0", "build.toml"]
        assert recorded["env"]["XMSCONAN_COVERAGE_XVFB_REEXEC"] == "1"

    def test_second_pass_does_not_recurse(self, monkeypatch):
        """The re-exec flag short-circuits the nested call."""
        monkeypatch.setenv("XMSCONAN_COVERAGE_XVFB_REEXEC", "1")
        with patch("xmsconan.coverage_tools.coverage_generator.os.execvpe") as execvpe:
            _reexec_under_xvfb()
        execvpe.assert_not_called()


def test_run_coverage_rejects_an_unknown_top_level_key(tmp_path):
    """The coverage run validates build.toml before shelling out to anything."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\nhas_test_files = true\n', encoding="utf-8")
    shelled_out = AssertionError("run_coverage shelled out before rejecting build.toml")
    with patch("xmsconan.coverage_tools.coverage_generator._run", side_effect=shelled_out):
        with pytest.raises(ValueError, match=r"unknown top-level key\(s\) has_test_files"):
            run_coverage(toml_file, "0.0.0", tmp_path)


_LEGS = (
    _BuildLeg("C++", '{"build_type": "Debug"}'),
    _BuildLeg("Python", '{"build_type": "Release"}'),
)


class TestConcurrentCoverageBuilds:
    """Scheduling of the two coverage builds.

    Sequential by default, overlapped when a library opts in.
    """

    def test_parallel_defaults_off(self):
        """The two builds run in sequence unless the library opts in.

        Overlapping them races conan 2's local cache, which is not safe for
        concurrent writes, and costs more wall clock than it saves once the
        legs contend for CPU. Opting in stays possible; it is no longer the
        default anyone gets without asking.
        """
        assert _coverage_context(CoverageTable(), "xmscore")["parallel"] is False

    def test_parallel_can_be_disabled(self):
        """[coverage].parallel = false puts the builds back in sequence."""
        assert _coverage_context(CoverageTable(parallel=False), "xmscore")["parallel"] is False

    def test_parallel_opt_in_survives(self):
        """[coverage].parallel = true still opts a library in.

        With the default flipped to off, only this pins that the toml key is
        read at all -- a regression hard-coding False in _coverage_context
        would pass every other test in this class.
        """
        assert _coverage_context(CoverageTable(parallel=True), "xmscore")["parallel"] is True

    def test_builds_run_concurrently(self):
        """Both legs are in flight at once.

        A barrier is the deterministic form of this assertion: it releases only
        once both legs have reached it, so it cannot pass under a sequential
        driver -- the first leg would sit there waiting for a partner that has
        not been started yet, and time out.
        """
        barrier = threading.Barrier(2, timeout=10)

        def _rendezvous(cmd, env=None, cwd=None, **kwargs):
            barrier.wait()
            return subprocess.CompletedProcess(cmd, 0, stdout="")

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _rendezvous):
            failed = _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=True)

        assert failed is False

    def test_builds_do_not_overlap_when_parallel_is_off(self):
        """[coverage].parallel = false really does serialize them.

        The escape hatch exists for a shared Conan cache under concurrent load,
        and for xvfb libraries whose image tests do not tolerate a second client
        on the display. A flag that quietly kept overlapping would be worse than
        no flag at all.
        """
        lock = threading.Lock()
        in_flight = 0
        peak = 0
        started_count = 0

        def _counted(cmd, env=None, cwd=None, **kwargs):
            nonlocal in_flight, peak, started_count
            with lock:
                in_flight += 1
                started_count += 1
                peak = max(peak, in_flight)
            try:
                return subprocess.CompletedProcess(cmd, 0, stdout="")
            finally:
                with lock:
                    in_flight -= 1

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _counted):
            _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=False)

        # A peak of one is the property under test, and it holds without a
        # sleep: comparing wall-clock intervals needs the legs to last long
        # enough to measure, which makes the test both slower and flaky on a
        # loaded runner.
        assert started_count == 2
        assert peak == 1

    def test_build_output_is_replayed_per_leg(self, capsys):
        """Each leg's output is printed whole rather than interleaved.

        Two concurrent `conan create`s writing to one log split every compiler
        diagnostic away from the file name printed above it.
        """
        def _chatty(cmd, env=None, cwd=None, **kwargs):
            name = "cpp" if "Debug" in " ".join(cmd) else "py"
            return subprocess.CompletedProcess(
                cmd, 0, stdout="\n".join(f"{name}-line{n}" for n in range(5)),
            )

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _chatty):
            _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=True)

        out = capsys.readouterr().out
        for name in ("cpp", "py"):
            assert "\n".join(f"{name}-line{n}" for n in range(5)) in out

    def test_build_failure_is_reported_not_raised(self):
        """A failing leg is recorded so the other leg and the report still run."""
        def _fail_cpp(cmd, env=None, cwd=None, **kwargs):
            code = 1 if "Debug" in " ".join(cmd) else 0
            return subprocess.CompletedProcess(cmd, code, stdout="")

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _fail_cpp):
            failed = _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=True)

        assert failed is True

    def test_leg_that_cannot_start_is_a_failure_not_a_crash(self):
        """An exception on a worker thread must not abort the whole run.

        It would otherwise surface out of future.result() in the driver, at
        exactly the point the run is supposed to carry on and report the half
        that did work.
        """
        def _explode(cmd, env=None, cwd=None, **kwargs):
            raise OSError("no such file: build.py")

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _explode):
            failed = _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=True)

        assert failed is True

    def test_hung_leg_times_out_and_keeps_what_it_had_printed(self, capsys):
        """A hung build must not take its buffered log down with it.

        Streaming let an operator see where a build stopped. Capturing shows
        nothing until the process ends, so a leg that never ends loses every
        line it produced when the CI job's own clock kills the container. The
        timeout fires first and surrenders the partial output.
        """
        def _hang(cmd, env=None, cwd=None, timeout=None, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd, timeout or 1, output="configuring...\ncompiling foo.cpp\n")

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _hang):
            failed = _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=True)

        assert failed is True
        out = capsys.readouterr().out
        assert "compiling foo.cpp" in out
        assert "timed out" in out

    def test_captured_legs_are_given_a_timeout(self):
        """The timeout has to reach subprocess.run, not just exist as a default."""
        seen = {}

        def _record(cmd, env=None, cwd=None, timeout=None, **kwargs):
            seen["timeout"] = timeout
            return subprocess.CompletedProcess(cmd, 0, stdout="")

        with patch("xmsconan.coverage_tools.coverage_generator.subprocess.run",
                   _record):
            _run_coverage_builds(_LEGS, "1.0.0", None, ".", parallel=True)

        assert seen["timeout"] == DEFAULT_LEG_TIMEOUT
