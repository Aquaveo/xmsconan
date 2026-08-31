"""The per-leg coverage status files and how the report phase unions them.

A ``collect`` run measures both layers in one job and writes one status file.
A split pipeline measures each layer in its own concurrent build job, and each
one writes a file named for its leg. Both shapes have to reach the report phase
as the same three answers, which is what these cover.
"""
import json
from pathlib import Path

from xmsconan.coverage_tools.coverage_generator import (
    _read_coverage_status,
    _write_coverage_status,
    COVERAGE_STATUS_FILE,
)


def _write(output_dir, name, **fields):
    """Write a status file directly, for the malformed and legacy cases."""
    (Path(output_dir) / name).write_text(json.dumps(fields), encoding="utf-8")


class TestWriteCoverageStatus:
    """What ``--phase measure`` leaves behind for the report job."""

    def test_no_leg_writes_the_whole_run_file(self, tmp_path):
        """``collect`` measures both layers itself, so it owns the plain name."""
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=True, py_measured=True)
        assert (tmp_path / COVERAGE_STATUS_FILE).exists()
        assert not list(tmp_path.glob("coverage-status-*.json"))

    def test_a_leg_suffixes_the_filename(self, tmp_path):
        """Two concurrent jobs must not write the same path.

        They upload into one artifact space, so a fixed name would leave
        whichever finished last as the only surviving answer -- discarding the
        other layer's measurement while still looking like a complete status.
        """
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=True, py_measured=False, leg="cpp")
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=False, py_measured=True, leg="python")
        assert sorted(p.name for p in tmp_path.glob("coverage-status-*.json")) == [
            "coverage-status-cpp.json", "coverage-status-python.json",
        ]
        assert not (tmp_path / COVERAGE_STATUS_FILE).exists()


class TestReadCoverageStatus:
    """The union the report phase applies over whatever the legs wrote."""

    def test_absent_status_is_none(self, tmp_path):
        """Distinguishable from a status that says nothing was measured.

        The report phase treats the two differently: no file means the
        measuring phase did not finish, which is an error rather than a gate
        failure.
        """
        assert _read_coverage_status(tmp_path) is None

    def test_whole_run_file_is_read_unchanged(self, tmp_path):
        """``--phase all`` and ``--phase collect`` keep working as they were."""
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=True, py_measured=True)
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": True, "py_measured": True,
        }

    def test_two_legs_union_into_both_layers_measured(self, tmp_path):
        """Each leg reports only on itself; together they cover both layers."""
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=True, py_measured=False, leg="cpp")
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=False, py_measured=True, leg="python")
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": True, "py_measured": True,
        }

    def test_a_missing_leg_leaves_its_own_layer_unmeasured(self, tmp_path):
        """The surviving leg must not vouch for the layer it did not measure.

        This is the case the union exists for. Python's threshold defaults to
        0, so a run where the Python job never uploaded would clear its gate on
        a default and report green -- while measuring nothing.
        """
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=True, py_measured=False, leg="cpp")
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": True, "py_measured": False,
        }

    def test_one_leg_failing_tests_fails_the_union(self, tmp_path):
        """Either leg's tests failing has to reach the gate."""
        _write_coverage_status(tmp_path, tests_failed=True,
                               cpp_measured=True, py_measured=False, leg="cpp")
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=False, py_measured=True, leg="python")
        assert _read_coverage_status(tmp_path)["tests_failed"] is True

    def test_legacy_and_per_leg_files_union_together(self, tmp_path):
        """A pipeline mid-migration can have both, and neither may be ignored."""
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=True, py_measured=False)
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=False, py_measured=True, leg="python")
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": True, "py_measured": True,
        }

    def test_an_unreadable_leg_does_not_discard_the_readable_one(self, tmp_path):
        """A truncated upload must not erase the leg that arrived intact."""
        (tmp_path / "coverage-status-cpp.json").write_text("{not json",
                                                           encoding="utf-8")
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=False, py_measured=True, leg="python")
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": False, "py_measured": True,
        }

    def test_a_leg_holding_json_that_is_not_an_object_is_ignored(self, tmp_path):
        """Valid JSON of the wrong shape must not crash the gate.

        The union calls ``.get`` on every payload, so a file holding a list or
        a bare string would reach it as a readable status and raise
        AttributeError from inside the job whose whole purpose is to report a
        verdict. Treated as unreadable instead, which falls through to the
        unmeasured-layer path and fails the gate loudly.
        """
        (tmp_path / "coverage-status-cpp.json").write_text(
            '["cpp_measured"]', encoding="utf-8")
        _write_coverage_status(tmp_path, tests_failed=False,
                               cpp_measured=False, py_measured=True, leg="python")
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": False, "py_measured": True,
        }

    def test_every_leg_holding_a_non_object_reads_as_no_status_at_all(self, tmp_path):
        """With nothing readable left, the report phase gets None, not {}.

        None is what it distinguishes "the measuring phase did not finish" by,
        and that has to stay an error rather than a gate miss -- an empty dict
        would gate on defaults and forgive a pipeline that measured nothing.
        """
        (tmp_path / "coverage-status-cpp.json").write_text("3", encoding="utf-8")
        assert _read_coverage_status(tmp_path) is None

    def test_a_leg_missing_a_field_reads_as_not_measured(self, tmp_path):
        """An older or partial writer must not be read as a measurement."""
        _write(tmp_path, "coverage-status-cpp.json", cpp_measured=True)
        assert _read_coverage_status(tmp_path) == {
            "tests_failed": False, "cpp_measured": True, "py_measured": False,
        }
