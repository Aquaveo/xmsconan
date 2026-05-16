"""Tests for coverage_tools.coverage_generator."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.coverage_tools.coverage_generator import (
    _append_github_summary,
    _assert_gcovr_collected_data,
    _conan_cache_path,
    _cpp_percent_from_summary,
    _find_coverage_package,
    _find_pytest_cov_artifact,
    _is_simple_relative_filter_pattern,
    _py_percent_from_summary,
    _resolve_coverage_python_version,
    _resolve_gcovr_filters,
    run_coverage,
)
from xmsconan.generator_tools.ci_file_generator import _coverage_context


class TestCoverageContextDefaults:
    """Defaults baked into _coverage_context match the issue spec."""

    def test_thresholds_default_to_zero(self):
        """Both thresholds default to 0 (report-only mode)."""
        ctx = _coverage_context({}, "xmscore")
        assert ctx["cpp_threshold"] == 0.0
        assert ctx["python_threshold"] == 0.0

    def test_filters_default_to_library_prefix(self):
        """The default gcovr filter scopes to the library's own source tree."""
        ctx = _coverage_context({}, "xmsgrid")
        assert ctx["filters"] == ["xmsgrid/"]

    def test_excludes_baked_in_when_absent(self):
        """Default excludes drop *.t.h, the pybind dir, and the package tests."""
        ctx = _coverage_context({}, "xmsgrid")
        excludes = ctx["excludes"]
        assert any(r".*\.t\.h$" in e for e in excludes)
        assert any("xmsgrid/python" in e for e in excludes)
        assert any("_package/tests" in e for e in excludes)

    def test_user_supplied_filters_win(self):
        """User-supplied filters replace the defaults entirely."""
        ctx = _coverage_context({"filters": ["only/"]}, "xmsgrid")
        assert ctx["filters"] == ["only/"]

    def test_user_supplied_thresholds_win(self):
        """User-supplied thresholds replace the defaults and are coerced to float."""
        ctx = _coverage_context(
            {"cpp_threshold": 70, "python_threshold": 65}, "xmsgrid",
        )
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
    """Conan cache parsing — picks the newest pybind-only OR testing-only Debug package."""

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_picks_newest_matching_package(self, mock_run):
        """When multiple revisions match, the highest timestamp wins."""
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Debug"},
             "ts": 100, "pid": "old_pid"},
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Debug"},
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
             "settings": {"build_type": "Debug"},
             "ts": 100, "pid": "pid"},
        ])
        ref, pid = _find_coverage_package(
            "xmscore", kind="pybind", python_version="3.13",
        )
        assert ref == "xmscore/1.0.0"
        assert pid == "pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_skips_release_builds(self, mock_run):
        """Release build_type must not match — we only want Debug for instrumentation."""
        mock_run.return_value = _fake_conan_list_output([
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Release"},
             "ts": 100, "pid": "release_pid"},
        ])
        with pytest.raises(RuntimeError):
            _find_coverage_package(
                "xmscore", kind="pybind", python_version="3.13",
            )

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_pybind_kind_rejects_combined_testing_true_pybind_true(self, mock_run):
        """kind='pybind' DOES NOT match a combined ``testing=True+pybind=True`` record.

        The new two-build coverage flow (issue #65 follow-up) carves out
        TWO disjoint Debug configs: one ``testing=True+pybind=False`` and
        one ``testing=False+pybind=True``. ``kind='pybind'`` must
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
             "settings": {"build_type": "Debug"},
             "ts": 999, "pid": "pid_310"},  # newer
            {"options": {"pybind": "True", "testing": "False",
                         "python_version": "3.13"},
             "settings": {"build_type": "Debug"},
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
                 "settings": {"build_type": "Debug"}, "ts": 2},
            ]),
        )
        ref, pid = _find_coverage_package(
            "xmscore", kind="testing",
        )
        assert ref and pid

    def test_find_pybind_package_matches_pybind_only_pinned_python(self, monkeypatch):
        """kind='pybind' matches testing=False, pybind=True, Debug, pinned python."""
        monkeypatch.setattr(
            "xmsconan.coverage_tools.coverage_generator.subprocess.run",
            lambda *a, **kw: _fake_conan_list_output([
                {"options": {"testing": "True", "pybind": "False"},
                 "settings": {"build_type": "Debug"}, "ts": 1},
                {"options": {"testing": "False", "pybind": "True",
                             "python_version": "3.10"},
                 "settings": {"build_type": "Debug"}, "ts": 2},
                {"options": {"testing": "False", "pybind": "True",
                             "python_version": "3.13"},
                 "settings": {"build_type": "Debug"}, "ts": 3},
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
        assert _resolve_coverage_python_version({}) == "3.13"

    def test_uses_highest_ci_python_versions(self):
        """Highest entry in [ci].python_versions wins by (major, minor)."""
        toml_data = {"ci": {"python_versions": ["3.10", "3.13"]}}
        assert _resolve_coverage_python_version(toml_data) == "3.13"

    def test_handles_list_order_independence(self):
        """Order in [ci].python_versions doesn't matter."""
        toml_data = {"ci": {"python_versions": ["3.13", "3.10"]}}
        assert _resolve_coverage_python_version(toml_data) == "3.13"

    def test_explicit_coverage_python_version_overrides(self):
        """[coverage].python_version overrides the [ci].python_versions default."""
        toml_data = {
            "ci": {"python_versions": ["3.13"]},
            "coverage": {"python_version": "3.10"},
        }
        assert _resolve_coverage_python_version(toml_data) == "3.10"

    def test_empty_ci_python_versions_falls_back(self):
        """An empty list in [ci] is treated like the key was missing."""
        toml_data = {"ci": {"python_versions": []}}
        assert _resolve_coverage_python_version(toml_data) == "3.13"


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
        """Empty gcovr result → RuntimeError naming the build folder."""
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 0.0, "line_total": 0}))
        build_folder = tmp_path / "build"
        with pytest.raises(RuntimeError) as exc_info:
            _assert_gcovr_collected_data(summary, build_folder, ["xmscore/"])
        msg = str(exc_info.value)
        assert str(build_folder) in msg
        # Diagnostic must name the most likely cause (XMS_COVERAGE / #69):
        assert "XMS_COVERAGE" in msg or "#69" in msg
        # And echo the filters we actually used, so the operator can
        # compare them against the real source paths:
        assert "xmscore/" in msg

    def test_passes_when_line_total_positive(self, tmp_path):
        """A real measurement (any non-zero line_total) is not an error.

        Includes the legitimate 0%-but-non-zero-lines case: 1000 lines
        instrumented, 0 covered by tests. That's a real result, the
        threshold check handles it, this guard must not interfere.
        """
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 0.0, "line_total": 1000}))
        _assert_gcovr_collected_data(summary, tmp_path, ["xmscore/"])

    def test_passes_when_line_total_key_absent(self, tmp_path):
        """Schema drift (no ``line_total`` at all) is left to the percent extractor.

        ``_cpp_percent_from_summary`` already raises a clear
        ``ValueError`` on missing keys; this guard doesn't double up.
        """
        summary = tmp_path / "cov-cpp-summary.json"
        summary.write_text(json.dumps({"line_percent": 50.0}))
        _assert_gcovr_collected_data(summary, tmp_path, ["xmscore/"])


class TestRunCoverageThresholdGating:
    """End-to-end gating logic with all subprocess calls mocked."""

    def _setup_workspace(self, tmp_path, *, cpp_percent, py_percent):
        """Create a workspace with build.toml and fake coverage outputs."""
        toml_file = tmp_path / "build.toml"
        toml_file.write_text(
            'library_name = "xmscore"\n'
            'description = "desc"\n'
            'python_namespaced_dir = "core"\n'
            '\n'
            '[coverage]\n'
            'cpp_threshold = 70\n'
            'python_threshold = 70\n',
            encoding="utf-8",
        )
        build_folder = tmp_path / "fake-build"
        source_folder = tmp_path / "fake-source"
        build_folder.mkdir()
        source_folder.mkdir()
        (build_folder / "cov-py-summary.json").write_text(
            json.dumps({"totals": {"percent_covered": py_percent}})
        )
        (build_folder / "cov-py.xml").write_text("<coverage/>")

        # gcovr is mocked to write the summary into the workspace.
        def fake_run(cmd, env=None, cwd=None, **_kw):
            if isinstance(cmd, list) and cmd and cmd[0] == "gcovr":
                idx = cmd.index("--json-summary")
                Path(cmd[idx + 1]).write_text(
                    json.dumps({"line_percent": cpp_percent}),
                )
            return MagicMock(returncode=0)

        return toml_file, build_folder, source_folder, fake_run

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_passes_when_both_layers_meet_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Exits 0 when C++ and Python percentages both clear their thresholds."""
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=72.5, py_percent=71.2,
        )
        mock_run.side_effect = fake_run
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == 0

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_when_cpp_under_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Exits 1 when C++ percentage is below cpp_threshold."""
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=65.0, py_percent=85.0,
        )
        mock_run.side_effect = fake_run
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == 1

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_when_python_under_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """Exits 1 when Python percentage is below python_threshold."""
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=85.0, py_percent=50.0,
        )
        mock_run.side_effect = fake_run
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == 1

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_fails_when_raw_just_below_threshold(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """A raw 69.95% must not pass a 70.0 threshold via display rounding."""
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=69.95, py_percent=99.0,
        )
        mock_run.side_effect = fake_run
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        exit_code = run_coverage(str(toml_file), "0.0.0", str(tmp_path))
        assert exit_code == 1, "69.95% must not satisfy a 70.0 threshold"

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_xmsconan_gen_called_with_explicit_output_dir(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """run_coverage passes --output_dir to xmsconan_gen instead of relying on cwd."""
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=80.0, py_percent=80.0,
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
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
        regression cannot come back via the coverage tool).
        """
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=80.0, py_percent=80.0,
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        build_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and len(c) >= 2 and c[1] == "build.py"
        ]
        assert build_cmds, "build.py should have been invoked"
        cmd = build_cmds[0]
        filter_idx = cmd.index("--filter")
        filter_dict = json.loads(cmd[filter_idx + 1])
        assert filter_dict["build_type"] == "Debug"
        assert filter_dict.get("options", {}).get("pybind") is True
        # And the bug-triggering shape must NOT come back: option keys
        # must live under "options", never at the top level.
        assert "pybind" not in filter_dict
        assert "python_version" not in filter_dict

    @patch("xmsconan.coverage_tools.coverage_generator._find_coverage_package")
    @patch("xmsconan.coverage_tools.coverage_generator._conan_cache_path")
    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_build_py_filter_requests_testing_true(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """The --filter must request testing=True alongside pybind=True.

        Under ``XMS_COVERAGE=1`` the packager carves out a combined
        ``pybind=True+testing=True+Debug`` config so the recipe's
        ``build()`` runs both ``run_cxx_tests`` (gated on ``testing``)
        and ``run_python_tests`` (gated on ``pybind``) against the same
        instrumented binary. Both runs contribute ``.gcda`` data to the
        same ``.gcno`` set and gcovr collects the union. The filter
        must pin to that combined config — without ``testing=True`` it
        would match a plain ``pybind=True+Debug`` package (which is
        also emitted under coverage, transitionally) and silently lose
        the CxxTest contribution to C++ coverage.
        """
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=80.0, py_percent=80.0,
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        build_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and len(c) >= 2 and c[1] == "build.py"
        ]
        cmd = build_cmds[0]
        filter_idx = cmd.index("--filter")
        filter_dict = json.loads(cmd[filter_idx + 1])
        assert filter_dict.get("options", {}).get("testing") is True, (
            "filter must request testing=True so the packager selects the "
            "pybind=True+testing=True+Debug config that runs CxxTest + "
            "pytest-cov against the same instrumented binary."
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
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=80.0, py_percent=80.0,
        )
        captured_cmds = []

        def capture(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = capture
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: build_folder

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        gcovr_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and c and c[0] == "gcovr"
        ]
        assert gcovr_cmds, "gcovr should have been invoked"
        cmd = gcovr_cmds[0]

        # 1. --root must be the build folder, not the source folder.
        root_idx = cmd.index("--root")
        assert cmd[root_idx + 1] == str(build_folder), (
            f"--root must be {build_folder!s}, got {cmd[root_idx + 1]!r}. "
            "Anchoring against the source folder filters out every file "
            "since .gcno paths live under build_folder."
        )
        assert str(source_folder) not in cmd, (
            "source_folder must not appear anywhere in the gcovr command — "
            "it is irrelevant to .gcno path resolution."
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
            real_source = f"{build_folder.as_posix()}/xmscore/math/math.cpp"
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
        """The --filter pins a python_version derived from [ci].python_versions (issue #65)."""
        toml_file = tmp_path / "build.toml"
        toml_file.write_text(
            'library_name = "xmscore"\n'
            'description = "desc"\n'
            'python_namespaced_dir = "core"\n'
            '\n'
            '[ci]\n'
            'python_versions = ["3.10", "3.13"]\n'
            '\n'
            '[coverage]\n'
            'cpp_threshold = 70\n'
            'python_threshold = 70\n',
            encoding="utf-8",
        )
        build_folder = tmp_path / "fake-build"
        source_folder = tmp_path / "fake-source"
        build_folder.mkdir()
        source_folder.mkdir()
        (build_folder / "cov-py-summary.json").write_text(
            json.dumps({"totals": {"percent_covered": 80.0}})
        )

        captured_cmds = []

        def fake_run(cmd, env=None, cwd=None, **kw):
            captured_cmds.append(list(cmd) if isinstance(cmd, list) else cmd)
            if isinstance(cmd, list) and cmd and cmd[0] == "gcovr":
                idx = cmd.index("--json-summary")
                Path(cmd[idx + 1]).write_text(json.dumps({"line_percent": 80.0}))
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
        )

        run_coverage(str(toml_file), "0.0.0", str(tmp_path))

        build_cmds = [
            c for c in captured_cmds
            if isinstance(c, list) and len(c) >= 2 and c[1] == "build.py"
        ]
        cmd = build_cmds[0]
        filter_dict = json.loads(cmd[cmd.index("--filter") + 1])
        # Highest of ["3.10", "3.13"] is 3.13.
        assert filter_dict["options"]["python_version"] == "3.13"
        # _find_coverage_package must be told to pin to the same ABI so
        # the lookup matches what the build produced.
        find_call = mock_find.call_args
        assert find_call.args[1] == "3.13" or find_call.kwargs.get("python_version") == "3.13"

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
        toml_file, build_folder, source_folder, fake_run = self._setup_workspace(
            tmp_path, cpp_percent=99.0, py_percent=99.0,
        )

        def run_with_build_failure(cmd, env=None, cwd=None, **kw):
            is_build_py = isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "build.py"
            if is_build_py:
                raise subprocess.CalledProcessError(1, cmd)
            return fake_run(cmd, env=env, cwd=cwd, **kw)

        mock_run.side_effect = run_with_build_failure
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
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
        build_folder = tmp_path / "fake-build"
        source_folder = tmp_path / "fake-source"
        # Layout-specific subdir, mirroring what the recipe does:
        layout_subdir = build_folder / "build" / "Debug"
        layout_subdir.mkdir(parents=True)
        source_folder.mkdir()
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
                idx = cmd.index("--json-summary")
                Path(cmd[idx + 1]).write_text(json.dumps({"line_percent": 99.0}))
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run
        mock_find.return_value = ("xmscore/0.0.0", "pid")
        mock_path.side_effect = lambda _ref, kind: (
            build_folder if kind == "build" else source_folder
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
