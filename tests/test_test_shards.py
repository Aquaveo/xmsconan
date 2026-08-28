"""Tests for xmsconan.ci_tools.test_shards — one-container gtest sharding."""
# 1. Standard python modules
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch
from xml.etree import ElementTree

# 2. Third party modules
import pytest

# 3. Aquaveo modules
from xmsconan.ci_tools import test_shards


def _shard_xml(suite, cases, failures=0):
    """Render a gtest --gtest_output=xml document with one suite."""
    case_xml = []
    for index, name in enumerate(cases):
        failure = ""
        if index < failures:
            failure = '<failure message="boom" type=""/>'
        case_xml.append(
            f'<testcase name="{name}" status="run" result="completed" '
            f'time="0.010" classname="{suite}">{failure}</testcase>'
        )
    body = "\n".join(case_xml)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuites tests="{len(cases)}" failures="{failures}" disabled="0" '
        f'errors="0" time="0.100" name="AllTests">\n'
        f'  <testsuite name="{suite}" tests="{len(cases)}" failures="{failures}" '
        f'disabled="0" errors="0" time="0.100">\n'
        f'{body}\n'
        '  </testsuite>\n</testsuites>\n'
    )


def _staged(tmp_path, label="Release-testing", with_runner=True, with_test_files=False):
    """Build a test_artifacts/ tree the way _save_test_artifacts stages one."""
    artifacts = tmp_path / "test_artifacts"
    artifact_dir = artifacts / label
    artifact_dir.mkdir(parents=True)
    if with_runner:
        runner = artifact_dir / ("runner.exe" if os.name == "nt" else "runner")
        runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if with_test_files:
        (artifact_dir / "test_files").mkdir()
        (artifact_dir / "test_files" / "grid.txt").write_text("data", encoding="utf-8")
    return artifacts, artifact_dir


# --- shard count ---

@pytest.mark.parametrize("value,expected", [("4", 4), (8, 8), ("1", 1)])
def test_resolve_shard_count_takes_an_explicit_number(value, expected):
    """An explicit count is used verbatim."""
    assert test_shards.resolve_shard_count(value) == expected


def test_resolve_shard_count_auto_is_at_least_two():
    """'auto' halves the CPU count but never drops below two.

    A one-shard 'auto' would silently turn the feature off on a small runner,
    which reads as sharding being enabled and doing nothing.
    """
    assert test_shards.resolve_shard_count("auto") >= 2


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_resolve_shard_count_rejects_nonsense(value):
    """A count below one, or a non-number, fails loudly rather than defaulting."""
    with pytest.raises(ValueError):
        test_shards.resolve_shard_count(value)


# --- locating the artifacts ---

def test_find_artifact_dir_picks_the_single_testing_directory(tmp_path):
    """The staged *-testing directory is found without being named."""
    artifacts, artifact_dir = _staged(tmp_path)
    assert test_shards.find_artifact_dir(artifacts) == artifact_dir


def test_find_artifact_dir_honors_an_explicit_label(tmp_path):
    """--label selects one configuration when several are staged."""
    artifacts, _ = _staged(tmp_path, label="Release-testing")
    (artifacts / "Debug-testing").mkdir()
    assert test_shards.find_artifact_dir(artifacts, "Debug-testing").name == "Debug-testing"


def test_find_artifact_dir_refuses_to_guess_between_configurations(tmp_path):
    """Several testing directories and no label is an error, not a coin flip.

    Picking one would test a configuration the operator never named and report
    it as the whole suite.
    """
    artifacts, _ = _staged(tmp_path, label="Release-testing")
    (artifacts / "Debug-testing").mkdir()
    with pytest.raises(ValueError, match="--label"):
        test_shards.find_artifact_dir(artifacts)


def test_find_artifact_dir_names_what_it_saw_when_nothing_matches(tmp_path):
    """The not-found message lists the directory contents.

    The shell this replaced took `ls | head -1` of an empty result and went on
    to report a runner missing from "/runner" -- a path nothing had written.
    """
    artifacts = tmp_path / "test_artifacts"
    (artifacts / "Release-python").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="Release-python"):
        test_shards.find_artifact_dir(artifacts)


def test_find_artifact_dir_reports_a_missing_artifacts_directory(tmp_path):
    """An absent artifacts/ directory points at the job that should have staged it."""
    with pytest.raises(FileNotFoundError, match="--artifacts-dir|artifacts"):
        test_shards.find_artifact_dir(tmp_path / "nope")


# --- the runner binary ---

def test_find_runner_restores_the_executable_bit(tmp_path):
    """The exec bit does not survive a GitLab artifact round trip, so it is re-applied."""
    _, artifact_dir = _staged(tmp_path)
    runner = test_shards.find_runner(artifact_dir)
    assert runner.is_file()
    assert os.access(runner, os.X_OK)


def test_find_runner_missing_is_an_error_not_a_skip(tmp_path):
    """No runner means no C++ test ran at all, which must fail the job.

    With sharding on, the recipe sets XMS_SKIP_CXX_TESTS and these shards are
    the only thing that runs the suite -- so a tolerated absence is a green
    pipeline that tested nothing.
    """
    _, artifact_dir = _staged(tmp_path, with_runner=False)
    with pytest.raises(FileNotFoundError, match="runner"):
        test_shards.find_runner(artifact_dir)


# --- test_files relinking ---

def test_link_test_files_restores_the_compiled_in_path(tmp_path):
    """test_files/ is linked back to the absolute path baked into the binary."""
    _, artifact_dir = _staged(tmp_path, with_test_files=True)
    compiled_in = tmp_path / "conan" / "build" / "test_files"
    (artifact_dir / "test_metadata.json").write_text(
        json.dumps({"test_path": str(compiled_in) + "/"}), encoding="utf-8",
    )

    linked = test_shards.link_test_files(artifact_dir)

    # The trailing separator the recipe writes must come off before the parent
    # is taken, or the link lands a directory too deep.
    assert linked == compiled_in
    assert (compiled_in / "grid.txt").read_text(encoding="utf-8") == "data"


def test_link_test_files_is_a_no_op_without_metadata(tmp_path):
    """A library with no test data has nothing to link, which is not an error."""
    _, artifact_dir = _staged(tmp_path)
    assert test_shards.link_test_files(artifact_dir) is None


def test_link_test_files_replaces_a_stale_link(tmp_path):
    """Re-running over an existing link does not fail."""
    _, artifact_dir = _staged(tmp_path, with_test_files=True)
    compiled_in = tmp_path / "conan" / "build" / "test_files"
    (artifact_dir / "test_metadata.json").write_text(
        json.dumps({"test_path": str(compiled_in) + "/"}), encoding="utf-8",
    )

    test_shards.link_test_files(artifact_dir)
    assert test_shards.link_test_files(artifact_dir) == compiled_in


# --- running the shards ---

def _stub_run(returncode=0, stdout="ok"):
    """Return a subprocess.run stub that writes a shard report and succeeds."""
    def _run(command, env=None, timeout=None, **kwargs):
        xml_path = None
        for arg in command:
            if str(arg).startswith("--gtest_output=xml:"):
                xml_path = Path(str(arg).split(":", 1)[1])
        if xml_path is not None:
            index = env["GTEST_SHARD_INDEX"]
            xml_path.write_text(_shard_xml("Suite", [f"case_{index}"]), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, stdout=stdout)
    return _run


def test_run_shards_gives_each_shard_its_own_gtest_index(tmp_path):
    """Every shard gets a distinct GTEST_SHARD_INDEX and the same total.

    That pair is what makes the union of the shards the whole suite exactly
    once; a repeated index would run one slice twice and skip another entirely.
    """
    _, artifact_dir = _staged(tmp_path)
    runner = test_shards.find_runner(artifact_dir)
    seen = []

    def _record(command, env=None, timeout=None, **kwargs):
        seen.append((env["GTEST_TOTAL_SHARDS"], env["GTEST_SHARD_INDEX"]))
        return subprocess.CompletedProcess(command, 0, stdout="")

    with patch.object(test_shards.subprocess, "run", _record):
        results = test_shards.run_shards(runner, 3, artifact_dir)

    assert sorted(seen) == [("3", "0"), ("3", "1"), ("3", "2")]
    assert [r.index for r in results] == [0, 1, 2]
    assert all(r.passed for r in results)


def test_run_shards_gives_each_xvfb_shard_its_own_display(tmp_path):
    """Shards must not share an X display.

    `xvfb-run -a` picks a free server number, but the gap between that check
    and the bind is exactly what N simultaneous starts land in.
    """
    _, artifact_dir = _staged(tmp_path)
    runner = test_shards.find_runner(artifact_dir)
    commands = []

    def _record(command, env=None, timeout=None, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    with patch.object(test_shards.subprocess, "run", _record):
        test_shards.run_shards(runner, 3, artifact_dir, xvfb=True)

    displays = sorted(
        arg for command in commands for arg in command
        if str(arg).startswith("--server-num=")
    )
    assert len(set(displays)) == 3
    assert all(command[0] == "xvfb-run" for command in commands)


def test_run_shards_clears_reports_from_a_previous_run(tmp_path):
    """A stale report must not stand in for one this run never wrote.

    Otherwise a shard that dies before serializing anything is merged from the
    previous run's file and reads as a pass.
    """
    _, artifact_dir = _staged(tmp_path)
    runner = test_shards.find_runner(artifact_dir)
    stale = artifact_dir / "TEST-shard-0.xml"
    stale.write_text(_shard_xml("Old", ["stale_case"]), encoding="utf-8")

    with patch.object(test_shards.subprocess, "run",
                      lambda command, env=None, timeout=None, **kwargs:
                      subprocess.CompletedProcess(command, 0, stdout="")):
        test_shards.run_shards(runner, 1, artifact_dir)

    assert not stale.exists()


def test_run_shards_records_a_timeout_as_a_failure(tmp_path):
    """A shard killed on timeout is a failure carrying its reason."""
    _, artifact_dir = _staged(tmp_path)
    runner = test_shards.find_runner(artifact_dir)

    def _timeout(command, env=None, timeout=None, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout, output="partial output")

    with patch.object(test_shards.subprocess, "run", _timeout):
        results = test_shards.run_shards(runner, 1, artifact_dir, timeout=5)

    assert not results[0].passed
    assert "timed out" in results[0].error
    assert "partial output" in results[0].output


def test_shard_output_is_replayed_in_one_contiguous_block(tmp_path, capsys):
    """Each shard's output is printed whole, not interleaved with its siblings.

    Interleaving is what makes a one-container run unreadable: it splits a
    failing assertion away from the test name that identifies it.
    """
    _, artifact_dir = _staged(tmp_path)
    runner = test_shards.find_runner(artifact_dir)

    def _chatty(command, env=None, timeout=None, **kwargs):
        index = env["GTEST_SHARD_INDEX"]
        body = "\n".join(f"shard{index}-line{n}" for n in range(5))
        return subprocess.CompletedProcess(command, 0, stdout=body)

    with patch.object(test_shards.subprocess, "run", _chatty):
        test_shards.run_shards(runner, 2, artifact_dir)

    out = capsys.readouterr().out
    for index in range(2):
        block = "\n".join(f"shard{index}-line{n}" for n in range(5))
        assert block in out


# --- merging the reports ---

def test_merge_reports_merges_suites_by_name(tmp_path):
    """One suite arrives split across shards, because gtest shards by case.

    Concatenating would show the reader N partial copies of every suite; the
    cases are appended into one and the counters summed.
    """
    results = []
    for index, cases in enumerate((["a", "b"], ["c"])):
        xml_path = tmp_path / f"TEST-shard-{index}.xml"
        xml_path.write_text(_shard_xml("GridTest", cases), encoding="utf-8")
        results.append(test_shards.ShardResult(
            index=index, returncode=0, duration=1.0, output="", xml_path=xml_path,
        ))

    output = tmp_path / "merged.xml"
    totals = test_shards.merge_reports(results, output, elapsed=2.5)

    root = ElementTree.parse(output).getroot()
    suites = root.findall("testsuite")
    assert len(suites) == 1
    assert suites[0].get("name") == "GridTest"
    assert len(suites[0].findall("testcase")) == 3
    assert totals.tests == 3
    assert totals.failures == 0
    # Wall clock, not the sum of the shards' times: the point of sharding is
    # that those two numbers differ.
    assert float(root.get("time")) == pytest.approx(2.5)


def test_merge_reports_sums_failures_across_shards(tmp_path):
    """A failure in any shard reaches the merged totals."""
    results = []
    for index, failures in enumerate((0, 1)):
        xml_path = tmp_path / f"TEST-shard-{index}.xml"
        xml_path.write_text(
            _shard_xml("GridTest", ["a", "b"], failures=failures), encoding="utf-8",
        )
        results.append(test_shards.ShardResult(
            index=index, returncode=0 if not failures else 1, duration=1.0,
            output="", xml_path=xml_path,
        ))

    totals = test_shards.merge_reports(results, tmp_path / "merged.xml", elapsed=1.0)
    assert totals.tests == 4
    assert totals.failures == 1


def test_merge_reports_synthesizes_an_error_for_a_shard_that_wrote_nothing(tmp_path):
    """A shard that died before serializing must not leave a silently short report.

    Contributing nothing would show every case green in the JUnit view next to
    a red job, which is the one reading the report is least likely to question.
    """
    good = tmp_path / "TEST-shard-0.xml"
    good.write_text(_shard_xml("GridTest", ["a"]), encoding="utf-8")
    results = [
        test_shards.ShardResult(index=0, returncode=0, duration=1.0, output="",
                                xml_path=good),
        test_shards.ShardResult(index=1, returncode=None, duration=9.0,
                                output="segfault", xml_path=tmp_path / "missing.xml",
                                error="timed out after 600s"),
    ]

    output = tmp_path / "merged.xml"
    totals = test_shards.merge_reports(results, output, elapsed=9.0)

    assert totals.errors == 1
    text = output.read_text(encoding="utf-8")
    assert "shard-1" in text
    assert "timed out after 600s" in text


def test_merge_reports_treats_an_unparseable_report_as_an_error(tmp_path):
    """A truncated XML is recorded, not raised past the summary."""
    broken = tmp_path / "TEST-shard-0.xml"
    broken.write_text("<testsuites><testsuite", encoding="utf-8")
    results = [test_shards.ShardResult(index=0, returncode=0, duration=1.0,
                                       output="", xml_path=broken)]

    totals = test_shards.merge_reports(results, tmp_path / "merged.xml", elapsed=1.0)
    assert totals.errors == 1


# --- end to end ---

def test_run_returns_zero_when_every_shard_passes(tmp_path, monkeypatch):
    """The happy path: shards pass, reports merge, exit code 0."""
    artifacts, artifact_dir = _staged(tmp_path)
    monkeypatch.chdir(tmp_path)

    with patch.object(test_shards.subprocess, "run", _stub_run()):
        code = test_shards.run(artifacts, 2, output=str(tmp_path / "merged.xml"))

    assert code == 0
    assert (tmp_path / "merged.xml").is_file()


def test_run_fails_when_a_shard_fails(tmp_path, monkeypatch):
    """A non-zero shard fails the job."""
    artifacts, _ = _staged(tmp_path)
    monkeypatch.chdir(tmp_path)

    with patch.object(test_shards.subprocess, "run", _stub_run(returncode=1)):
        code = test_shards.run(artifacts, 2, output=str(tmp_path / "merged.xml"))

    assert code == 1


def test_run_fails_on_a_failure_only_the_report_records(tmp_path, monkeypatch):
    """A green exit code does not override a failure recorded in the XML.

    Belt and braces against a runner that reports a failing case and still
    exits 0 -- which is what a custom main() or a crash handler can produce.
    """
    artifacts, _ = _staged(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _passing_exit_failing_report(command, env=None, timeout=None, **kwargs):
        for arg in command:
            if str(arg).startswith("--gtest_output=xml:"):
                Path(str(arg).split(":", 1)[1]).write_text(
                    _shard_xml("GridTest", ["a"], failures=1), encoding="utf-8",
                )
        return subprocess.CompletedProcess(command, 0, stdout="")

    with patch.object(test_shards.subprocess, "run", _passing_exit_failing_report):
        code = test_shards.run(artifacts, 1, output=str(tmp_path / "merged.xml"))

    assert code == 1
