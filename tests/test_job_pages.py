"""Tests for :mod:`xmsconan.job_tools.pages`, the coverage site GitLab serves.

Real directories under ``tmp_path`` rather than doubles: the whole reason this
moved out of the template is that the shell could not tell a missing report
from a copied one, so "what is on disk afterwards" is the property under test.
"""
import pathlib

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
    unconditionally, so a pipeline whose C++ layer did not measure printed one
    line to stderr, carried on, and published an index whose first link 404s
    -- green.

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
