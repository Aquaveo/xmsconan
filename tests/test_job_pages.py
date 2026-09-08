"""Tests for :mod:`xmsconan.job_tools.pages`, the coverage site GitLab serves.

Real directories under ``tmp_path`` rather than doubles: the whole reason this
moved out of the template is that the shell could not tell a missing report
from a copied one, so "what is on disk afterwards" is the property under test.
"""
import pathlib
import sys

import pytest

from xmsconan.exit_codes import EXIT_OK
from xmsconan.job_tools import pages


def _render(base, *names):
    """Write a plausible gcovr/pytest-cov output tree for each of *names*."""
    tmp_path = pathlib.Path(base)
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in names:
        report = tmp_path / name
        report.mkdir()
        (report / "index.html").write_text(f"<html>{name}</html>", encoding="utf-8")
        (report / "detail.html").write_text("<html>detail</html>", encoding="utf-8")
    return tmp_path


def _index(pages_dir):
    """The generated landing page's text."""
    return (pages_dir / "index.html").read_text(encoding="utf-8")


def test_both_reports_are_published_and_linked(tmp_path):
    """The C++ and Python trees land under their documented names."""
    source = _render(tmp_path / "src", "coverage-html-cpp", "coverage-html-py")
    destination = tmp_path / pages.PAGES_DIR

    published = pages.write_pages("xmscore", source_dir=source, pages_dir=destination)

    assert published == ["cpp", "python"]
    assert (destination / "cpp" / "index.html").exists()
    assert (destination / "python" / "detail.html").exists()
    assert 'href="cpp/index.html"' in _index(destination)
    assert 'href="python/index.html"' in _index(destination)


@pytest.mark.parametrize("rendered, published, linked, absent", [
    (["coverage-html-cpp"], ["cpp"], "cpp", "python"),
    (["coverage-html-py"], ["python"], "python", "cpp"),
])
def test_only_the_reports_that_exist_are_copied_and_linked(
        tmp_path, rendered, published, linked, absent):
    """A report that was not rendered appears in neither the tree nor the index.

    The pairing is the whole point. The shell version ran ``cp -r
    coverage-html-cpp public/cpp`` unconditionally and linked it
    unconditionally, while guarding the Python half with a ``[ -d ]``, so a
    pipeline whose C++ layer did not measure failed on the ``cp`` and
    published nothing at all -- and one that left the directory behind empty
    copied it, linked it, and served a 404, green.

    Asserted in both directions, because the C++ half is the one that was
    unconditional and a fix that merely swapped which half is assumed would
    pass a one-sided test.
    """
    source = _render(tmp_path / "src", *rendered)
    destination = tmp_path / pages.PAGES_DIR

    assert pages.write_pages("xmscore", source_dir=source,
                             pages_dir=destination) == published

    assert (destination / linked).is_dir()
    assert not (destination / absent).exists()
    assert f'href="{linked}/index.html"' in _index(destination)
    assert absent not in _index(destination)


def test_a_report_directory_without_an_index_is_not_published(tmp_path):
    """A gcovr run can leave its output directory behind having rendered nothing.

    That directory is what the shell's unconditional ``cp -r`` copied
    happily, and the link printed beside it 404s -- the same broken page as a
    missing report, without the failed job that would have said so. So the
    question is the file the link resolves to, not the directory holding it.
    """
    source = _render(tmp_path / "src", "coverage-html-py")
    (source / "coverage-html-cpp").mkdir()
    destination = tmp_path / pages.PAGES_DIR

    assert pages.write_pages("xmscore", source_dir=source,
                             pages_dir=destination) == ["python"]

    assert not (destination / "cpp").exists()
    assert "cpp" not in _index(destination)


def test_an_empty_report_directory_alone_is_no_report_at_all(tmp_path):
    """The same question, asked where it decides the job's exit code."""
    source = _render(tmp_path / "src")
    (source / "coverage-html-cpp").mkdir()

    with pytest.raises(ValueError, match="no coverage report to publish"):
        pages.write_pages("xmscore", source_dir=source, pages_dir=tmp_path / "public")


def test_a_destination_file_is_named_rather_than_removed(tmp_path):
    """The tree is replaced wholesale, so the path has to be one this owns.

    Saying which it found instead is the difference between "that is not the
    directory you meant" and an error from inside ``shutil``.
    """
    source = _render(tmp_path / "src", "coverage-html-cpp")
    destination = tmp_path / pages.PAGES_DIR
    destination.write_text("not a site", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory this can replace"):
        pages.write_pages("xmscore", source_dir=source, pages_dir=destination)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="creating a symlink needs a privilege the runner may not hold")
def test_a_destination_symlink_is_named_rather_than_removed(tmp_path):
    """``shutil.rmtree`` refuses a symlink, with an OSError that reads as a bug here."""
    source = _render(tmp_path / "src", "coverage-html-cpp")
    destination = tmp_path / pages.PAGES_DIR
    destination.symlink_to(_render(tmp_path / "elsewhere"), target_is_directory=True)

    with pytest.raises(ValueError, match="not a directory this can replace"):
        pages.write_pages("xmscore", source_dir=source, pages_dir=destination)


def test_no_report_at_all_raises_rather_than_publishing_an_empty_index(tmp_path):
    """An index linking nothing looks like a measurement of zero.

    Which is worse than a failed job: a reader who opens the page sees a
    coverage site, not a coverage run that did not happen.
    """
    source = _render(tmp_path / "src")

    with pytest.raises(ValueError, match="no coverage report to publish"):
        pages.write_pages("xmscore", source_dir=source, pages_dir=tmp_path / "public")


def test_a_rerun_does_not_serve_the_previous_run_s_report(tmp_path):
    """The tree is replaced, not merged.

    ``mkdir -p public`` left whatever was there. On a re-run whose Python
    layer stopped measuring, that published this run's index beside the
    previous run's ``python/`` -- a report dated to a pipeline the page does
    not name.
    """
    source = _render(tmp_path / "src", "coverage-html-cpp", "coverage-html-py")
    destination = tmp_path / pages.PAGES_DIR
    pages.write_pages("xmscore", source_dir=source, pages_dir=destination)

    (source / "coverage-html-py" / "index.html").unlink()
    (source / "coverage-html-py" / "detail.html").unlink()
    (source / "coverage-html-py").rmdir()
    published = pages.write_pages("xmscore", source_dir=source, pages_dir=destination)

    assert published == ["cpp"]
    assert not (destination / "python").exists()


def test_the_library_name_is_escaped_into_the_page(tmp_path):
    """The title and heading name the library, and neither can inject markup.

    The name comes from ``build.toml``, so this is not a hostile input so much
    as a correctness one -- but an ``&`` in a library name producing invalid
    HTML is a page nobody notices is broken.
    """
    source = _render(tmp_path / "src", "coverage-html-cpp")
    destination = tmp_path / pages.PAGES_DIR

    pages.write_pages("xms<core>&", source_dir=source, pages_dir=destination)

    index = _index(destination)
    assert "<title>xms&lt;core&gt;&amp; coverage</title>" in index
    assert "<h1>xms&lt;core&gt;&amp; coverage</h1>" in index


def test_the_entry_point_writes_the_documented_directory(tmp_path, monkeypatch):
    """``job coverage --pages`` takes no paths: both are the fixed layout.

    The template's ``artifacts: paths:`` names ``public`` statically, and the
    ``coverage-html-*`` directories are where ``xmsconan_coverage`` renders
    them, so a flag for either would be a second place to change them.
    """
    monkeypatch.chdir(_render(tmp_path, "coverage-html-cpp"))

    assert pages.job_coverage_pages("xmscore") == EXIT_OK

    assert (tmp_path / pages.PAGES_DIR / "cpp" / "index.html").exists()
    assert (tmp_path / pages.PAGES_DIR / "index.html").exists()
