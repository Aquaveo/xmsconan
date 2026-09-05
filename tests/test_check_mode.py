"""Tests for --check mode and the output plan the three generators share.

``--check`` answers one question: would a real run change this tree? It is
worth only as much as that equivalence, so the tests below pin the two ways
it could quietly stop being true -- reporting drift that a regeneration
would not fix (a CRLF working copy), and passing a tree a regeneration
would change (a stale profile no template renders any more).

The end-to-end half runs the real generators rather than a fabricated plan,
because the failure this mode exists to catch is a *generator* that no
longer produces what is committed.
"""
import io
from pathlib import Path

import pytest

from xmsconan.generator_tools import build_file_generator as build_file_generator_module
from xmsconan.generator_tools import ci_file_generator as ci_file_generator_module
from xmsconan.generator_tools import profile_generator as profile_generator_module
from xmsconan.generator_tools.output_plan import (
    check_plan,
    describe_plan,
    report_drift,
    write_plan,
    write_text_lf,
)
from .ci_helpers import write_build_toml


# --- writing a plan ---


def test_write_text_lf_creates_the_parent_directory(tmp_path):
    """A generator names an output path; it does not first mkdir the tree."""
    path = tmp_path / "nested" / "deeper" / "out.txt"

    write_text_lf(path, "hello\n")

    assert path.read_text(encoding="utf-8") == "hello\n"


def test_write_text_lf_normalizes_crlf_content_to_lf(tmp_path):
    """Line endings come out LF whatever the template or platform supplied.

    Read in binary: text mode would translate the bytes back and let a
    CRLF file pass as LF on the one platform where it matters.
    """
    path = tmp_path / "out.txt"

    write_text_lf(path, "one\r\ntwo\r\n")

    assert path.read_bytes() == b"one\ntwo\n"


def test_write_plan_writes_every_file_and_returns_the_paths(tmp_path):
    """The plan is the whole output: nothing is written outside it."""
    plan = {tmp_path / "a.txt": "a\n", tmp_path / "b" / "c.txt": "c\n"}

    written = write_plan(plan, "test file")

    assert [Path(path) for path in written] == [tmp_path / "a.txt", tmp_path / "b" / "c.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (tmp_path / "b" / "c.txt").read_text(encoding="utf-8") == "c\n"


def test_describe_plan_writes_nothing(tmp_path):
    """--dry-run reports the same plan --check compares and a run writes."""
    plan = {tmp_path / "a.txt": "a\n"}

    describe_plan(plan, "test file")

    assert list(tmp_path.iterdir()) == []


# --- reporting drift ---


def test_report_drift_finds_nothing_when_the_tree_matches(tmp_path):
    """The up-to-date case is the one that has to be exactly zero."""
    path = tmp_path / "a.txt"
    path.write_text("a\n", encoding="utf-8")

    assert report_drift({path: "a\n"}, stream=io.StringIO()) == 0


def test_report_drift_reports_a_planned_file_that_is_absent(tmp_path):
    """A missing file is drift, not a skip.

    The generated build files are gitignored in the consuming repositories,
    so "not there at all" is the most common state of a fresh checkout and
    the one a comparison over existing files alone would call clean.
    """
    stream = io.StringIO()

    differences = report_drift({tmp_path / "a.txt": "a\n"}, stream=stream)

    assert differences == 1
    assert "(missing)" in stream.getvalue()


def test_report_drift_diffs_a_file_whose_content_changed(tmp_path):
    """The report is the diff a reviewer has to judge, not a bare file name."""
    path = tmp_path / "a.txt"
    path.write_text("old line\n", encoding="utf-8")
    stream = io.StringIO()

    differences = report_drift({path: "new line\n"}, stream=stream)

    report = stream.getvalue()
    assert differences == 1
    assert "-old line" in report
    assert "+new line" in report


def test_report_drift_accepts_a_crlf_working_copy_of_an_lf_plan(tmp_path):
    """A Windows checkout of a generated file is not out of date.

    Git hands the same generated file to a Windows working copy with CRLF.
    Comparing the raw bytes would fail --check on every Windows runner for
    a reason no regeneration could fix -- the generator writes LF -- so the
    mode would be turned off rather than believed.
    """
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo\r\n")

    assert report_drift({path: "one\ntwo\n"}, stream=io.StringIO()) == 0


def test_report_drift_accepts_a_crlf_plan_against_an_lf_working_copy(tmp_path):
    """The other Windows direction: the *template* was checked out with CRLF.

    Templates are files too. On a Windows checkout they carry CRLF, so the
    rendered content does as well, while the generated file it is compared
    against is LF. Normalizing only the disk side would fail --check on the
    platform it was normalized for.
    """
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\ntwo\n")

    assert report_drift({path: "one\r\ntwo\r\n"}, stream=io.StringIO()) == 0


def test_report_drift_counts_a_file_a_real_run_would_delete(tmp_path):
    """Stale output is drift even though its content matches nothing in the plan."""
    stream = io.StringIO()

    differences = report_drift({}, stale=[tmp_path / "dropped.txt"], stream=stream)

    assert differences == 1
    assert "(would be removed)" in stream.getvalue()


def test_report_drift_counts_a_file_that_does_not_decode(tmp_path):
    """Whatever a file that is not UTF-8 is, it is not the generated file.

    Escaping with a UnicodeDecodeError instead would drop the report for
    every file after it, and end in the same exit code as drift does.
    """
    path = tmp_path / "a.txt"
    path.write_bytes(b"\xff\xfe not text\n")
    stream = io.StringIO()

    differences = report_drift({path: "a\n"}, stream=stream)

    assert differences == 1
    assert "(on disk, not utf-8)" in stream.getvalue()


def test_report_drift_counts_a_path_that_cannot_be_read(tmp_path):
    """A directory where the generated file should be is the same finding.

    Only the prefix is asserted: the reason is the OS's own wording, and
    Windows refuses to open a directory with a different message than
    POSIX does.
    """
    path = tmp_path / "a.txt"
    path.mkdir()
    stream = io.StringIO()

    differences = report_drift({path: "a\n"}, stream=stream)

    assert differences == 1
    assert "(on disk, unreadable:" in stream.getvalue()


# --- the exit code CI reads ---


def test_check_plan_exits_zero_and_names_the_count(tmp_path):
    """Success says how many files it checked.

    "No output" and "the tool never ran" are the same log in CI, and only
    one of them means the tree is up to date.
    """
    path = tmp_path / "a.txt"
    path.write_text("a\n", encoding="utf-8")
    stream = io.StringIO()

    assert check_plan({path: "a\n"}, stream=stream) == 0
    assert "1 generated file(s) are up to date." in stream.getvalue()


def test_check_plan_exits_one_and_says_how_to_fix_it(tmp_path):
    """The failure names the remedy, since the reader is a CI log."""
    stream = io.StringIO()

    exit_code = check_plan({tmp_path / "a.txt": "a\n"}, stream=stream)

    report = stream.getvalue()
    assert exit_code == 1
    assert "1 generated file(s) are out of date." in report
    assert "Re-run the generator without --check" in report


# --- end to end, through the console scripts ---

GEN = ["xmsconan_gen", "--version", "1.2.3", "-q", "build.toml"]
GEN_CHECK = ["xmsconan_gen", "--version", "1.2.3", "--check", "build.toml"]
CI = ["xmsconan_ci", "--version", "1.2.3", "-q"]
CI_CHECK = ["xmsconan_ci", "--version", "1.2.3", "--check"]
PROFILES = ["xmsconan_profiles", "-q", "build.toml"]
PROFILES_CHECK = ["xmsconan_profiles", "--check", "build.toml"]


@pytest.fixture
def generator_dir(tmp_path, monkeypatch):
    """The working directory of a generator run: a build.toml and nothing else.

    Complete enough for a full gen, ci, or profiles run, and written once per
    test, so a test that generates and then checks compares against the same
    input both times.
    """
    write_build_toml(tmp_path, "github", library_name="xmscore", description="Core library")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(module, monkeypatch, argv):
    """Run a generator's main() with *argv*, returning its exit code."""
    monkeypatch.setattr("sys.argv", argv)
    try:
        module.main()
    except SystemExit as exit_request:
        return exit_request.code
    return 0


def test_ci_check_fails_before_anything_is_generated(generator_dir, monkeypatch, capsys):
    """A checkout that never ran the generator is out of date, and says so."""
    exit_code = _run(ci_file_generator_module, monkeypatch, CI_CHECK)

    assert exit_code == 1
    assert "(missing)" in capsys.readouterr().out


def test_ci_check_passes_on_freshly_generated_output(generator_dir, monkeypatch, capsys):
    """What the generator just wrote is what --check expects to find.

    The round trip is the whole contract. If writing and comparing could
    disagree, --check would be noise on a tree nobody had touched.
    """
    _run(ci_file_generator_module, monkeypatch, CI)
    capsys.readouterr()

    exit_code = _run(ci_file_generator_module, monkeypatch, CI_CHECK)

    assert exit_code == 0
    assert "are up to date." in capsys.readouterr().out


def test_gen_check_passes_on_a_fully_generated_tree(generator_dir, monkeypatch, capsys):
    """The gen plan covers the build files, the recipe, _package/ and the profiles.

    A subset would be worse than nothing: it would report a tree as up to
    date while leaving the half it never looked at stale.
    """
    _run(build_file_generator_module, monkeypatch, GEN)
    capsys.readouterr()

    exit_code = _run(build_file_generator_module, monkeypatch, GEN_CHECK)

    assert exit_code == 0
    assert "are up to date." in capsys.readouterr().out


@pytest.mark.parametrize(
    "module, generate, check, edited",
    [
        pytest.param(ci_file_generator_module, CI, CI_CHECK, Path(".github", "workflows", "XmsCore-CI.yaml"), id="ci"),
        pytest.param(build_file_generator_module, GEN, GEN_CHECK, Path("CMakeLists.txt"), id="gen"),
    ],
)
def test_check_fails_on_a_hand_edited_generated_file(
    generator_dir, monkeypatch, capsys, module, generate, check, edited
):
    """The case the mode exists for: someone edited a generated file.

    The CI file carries a "do not edit manually" header; CMakeLists.txt is
    gitignored downstream, so its edit is invisible to review as well. Until
    now nothing reported either.
    """
    _run(module, monkeypatch, generate)
    path = generator_dir / edited
    path.write_text(path.read_text(encoding="utf-8") + "\n# hand-edited\n", encoding="utf-8")
    capsys.readouterr()

    exit_code = _run(module, monkeypatch, check)

    assert exit_code == 1
    assert "-# hand-edited" in capsys.readouterr().out


@pytest.mark.parametrize(
    "module, generate, check",
    [
        pytest.param(profile_generator_module, PROFILES, PROFILES_CHECK, id="profiles"),
        pytest.param(build_file_generator_module, GEN, GEN_CHECK, id="gen"),
    ],
)
def test_check_fails_on_a_profile_a_real_run_would_delete(generator_dir, monkeypatch, capsys, module, generate, check):
    """A dropped [matrix] entry leaves a profile behind that still configures.

    Nothing renders it any more, so a check that only compared the files it
    plans would call this tree clean while a real run deleted one. Both
    generators that write profiles delete stale ones, so both report them.
    """
    _run(module, monkeypatch, generate)
    stale = generator_dir / "conan_profiles" / "dropped_configuration.txt"
    # What it holds is beside the point: the finding is that it exists.
    stale.write_text("# rendered by a [matrix] entry that is gone\n", encoding="utf-8")
    capsys.readouterr()

    exit_code = _run(module, monkeypatch, check)

    report = capsys.readouterr().out
    assert exit_code == 1
    assert "dropped_configuration.txt" in report
    assert "(would be removed)" in report


@pytest.mark.parametrize(
    "module, check",
    [
        pytest.param(build_file_generator_module, GEN_CHECK, id="gen"),
        pytest.param(ci_file_generator_module, CI_CHECK, id="ci"),
        pytest.param(profile_generator_module, PROFILES_CHECK, id="profiles"),
    ],
)
def test_check_writes_nothing(generator_dir, monkeypatch, capsys, module, check):
    """Reporting drift never repairs it, so --check is safe on any tree.

    It is meant for a CI job and for a pre-commit hook, both of which run
    against a tree whose cleanliness is then asserted. A --check that wrote
    the very files it was reporting as missing would pass on the second run
    and hide the drift it exists to find.
    """
    exit_code = _run(module, monkeypatch, check)

    report = capsys.readouterr().out
    # The summary line as well as the exit code: a generator that crashed
    # would also exit 1 having written nothing, and prove nothing.
    assert exit_code == 1
    assert "are out of date." in report
    assert [path.name for path in generator_dir.rglob("*")] == ["build.toml"]
