"""Tests for coverage_tools.coverage_generator."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.coverage_tools.coverage_generator import (
    _append_github_summary,
    _cpp_percent_from_summary,
    _find_coverage_package,
    _py_percent_from_summary,
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


class TestFindCoveragePackage:
    """Conan cache parsing — picks the newest testing+pybind+Debug package."""

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_picks_newest_matching_package(self, mock_run):
        """When multiple revisions match, the highest timestamp wins."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "Local Cache": {
                    "xmscore/1.0.0": {
                        "revisions": {
                            "rev1": {
                                "timestamp": 100,
                                "packages": {
                                    "old_pid": {
                                        "info": {
                                            "options": {"testing": "True", "pybind": "True"},
                                            "settings": {"build_type": "Debug"},
                                        }
                                    },
                                },
                            },
                            "rev2": {
                                "timestamp": 200,
                                "packages": {
                                    "new_pid": {
                                        "info": {
                                            "options": {"testing": "True", "pybind": "True"},
                                            "settings": {"build_type": "Debug"},
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            }),
            returncode=0,
        )
        ref, pid = _find_coverage_package("xmscore")
        assert ref == "xmscore/1.0.0"
        assert pid == "new_pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_raises_when_no_match(self, mock_run):
        """Raises a clear error when the cache has no matching package."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"Local Cache": {}}), returncode=0,
        )
        with pytest.raises(RuntimeError, match="No testing=True"):
            _find_coverage_package("xmscore")

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_accepts_bool_and_lowercase_truthy_options(self, mock_run):
        """Conan's option repr is not contractually 'True' — accept bool/case variants."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "Local Cache": {
                    "xmscore/1.0.0": {
                        "revisions": {
                            "rev1": {
                                "timestamp": 100,
                                "packages": {
                                    "pid": {
                                        "info": {
                                            # bool True (not the string "True")
                                            "options": {"testing": True, "pybind": "true"},
                                            "settings": {"build_type": "Debug"},
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            }),
            returncode=0,
        )
        ref, pid = _find_coverage_package("xmscore")
        assert ref == "xmscore/1.0.0"
        assert pid == "pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_skips_release_builds(self, mock_run):
        """Release build_type must not match — we only want Debug for instrumentation."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "Local Cache": {
                    "xmscore/1.0.0": {
                        "revisions": {
                            "rev1": {
                                "timestamp": 100,
                                "packages": {
                                    "release_pid": {
                                        "info": {
                                            "options": {"testing": "True", "pybind": "True"},
                                            "settings": {"build_type": "Release"},
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            }),
            returncode=0,
        )
        with pytest.raises(RuntimeError):
            _find_coverage_package("xmscore")


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
        """The --filter passed to build.py nests pybind/testing under "options".

        XmsConanPackager.filter_configurations silently drops flat top-level
        pybind/testing keys (the bug fixed in issue #62 by raising on unknown
        keys, but this test pins the call site too so the regression cannot
        come back via the coverage tool).
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
        assert filter_dict.get("options", {}).get("testing") is True
        assert filter_dict.get("options", {}).get("pybind") is True
        # And the bug-triggering shape must NOT come back: testing/pybind
        # must live under "options", never at the top level.
        assert "testing" not in filter_dict
        assert "pybind" not in filter_dict

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
