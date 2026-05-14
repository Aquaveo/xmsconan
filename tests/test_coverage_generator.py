"""Tests for coverage_tools.coverage_generator."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.coverage_tools.coverage_generator import (
    _append_github_summary,
    _conan_cache_path,
    _cpp_percent_from_summary,
    _find_coverage_package,
    _py_percent_from_summary,
    _resolve_coverage_python_version,
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
    """Conan cache parsing — picks the newest pybind+Debug package pinned to one ABI."""

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
                                            "options": {"pybind": "True",
                                                        "python_version": "3.13"},
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
                                            "options": {"pybind": "True",
                                                        "python_version": "3.13"},
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
        ref, pid = _find_coverage_package("xmscore", "3.13")
        assert ref == "xmscore/1.0.0"
        assert pid == "new_pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_raises_when_no_match(self, mock_run):
        """Raises a clear error when the cache has no matching package."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"Local Cache": {}}), returncode=0,
        )
        with pytest.raises(RuntimeError, match="pybind=True"):
            _find_coverage_package("xmscore", "3.13")

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
                                            "options": {"pybind": True,
                                                        "python_version": "3.13"},
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
        ref, pid = _find_coverage_package("xmscore", "3.13")
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
                                            "options": {"pybind": "True",
                                                        "python_version": "3.13"},
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
            _find_coverage_package("xmscore", "3.13")

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_matches_pybind_only_when_testing_false(self, mock_run):
        """testing=True is *not* required (issue #64).

        ``XmsConanPackager.generate_configurations`` never emits a config
        with both ``testing=True`` and ``pybind=True`` — they are
        mutually-exclusive derivatives of the same base list. Requiring
        ``testing=True`` is what made the cache lookup match zero rows
        even after #61/#62 were fixed.
        """
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
                                            "options": {
                                                "pybind": "True",
                                                "testing": "False",
                                                "python_version": "3.13",
                                            },
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
        ref, pid = _find_coverage_package("xmscore", "3.13")
        assert ref == "xmscore/1.0.0"
        assert pid == "pid"

    @patch("xmsconan.coverage_tools.coverage_generator.subprocess.run")
    def test_pins_to_requested_python_version(self, mock_run):
        """Multi-ABI fan-out: cache has both 3.10 and 3.13, only the requested one wins.

        Issue #65: the prior code returned newest-by-timestamp across all
        pybind packages, so whichever Python build finished last drove
        the Python coverage report (non-determinism).
        """
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "Local Cache": {
                    "xmscore/1.0.0": {
                        "revisions": {
                            "rev1": {
                                "timestamp": 999,  # newer
                                "packages": {
                                    "pid_310": {
                                        "info": {
                                            "options": {"pybind": "True",
                                                        "python_version": "3.10"},
                                            "settings": {"build_type": "Debug"},
                                        }
                                    },
                                },
                            },
                            "rev2": {
                                "timestamp": 100,  # older
                                "packages": {
                                    "pid_313": {
                                        "info": {
                                            "options": {"pybind": "True",
                                                        "python_version": "3.13"},
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
        ref, pid = _find_coverage_package("xmscore", "3.13")
        # Older-by-timestamp wins because it's the requested ABI; the
        # newer 3.10 build must NOT be picked.
        assert pid == "pid_313"


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
    def test_returns_path_from_stdout(self, mock_run):
        """The trimmed stdout becomes the returned Path."""
        mock_run.return_value = MagicMock(stdout="  /trimmed/path  \n")
        assert _conan_cache_path("xmscore/0.0.0:abc", "build") == Path("/trimmed/path")


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
    def test_build_py_filter_omits_testing(
        self, mock_run, mock_path, mock_find, tmp_path,
    ):
        """The --filter must NOT request testing=True (issue #64).

        The packager never produces a config that is both ``testing=True``
        and ``pybind=True`` — they are mutually-exclusive derivatives of
        the base combinations list. Asking for both matches zero configs
        and silently widens or fails the coverage build.
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
        assert "testing" not in filter_dict.get("options", {}), (
            "testing=True is mutually exclusive with pybind=True in the "
            "packager (issue #64); filter must not request it."
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
