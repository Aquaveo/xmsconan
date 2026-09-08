"""``xmsconan job coverage --pages`` -- the coverage site GitLab Pages serves.

Fourteen lines of ``echo`` inside a YAML block scalar, rendered twice on two
Jinja branches, built this HTML. Duplicated markup in a template is the shape
where the two copies drift, and the copies here were already one comment apart.

The shell also made the copy decision separately from the link that depends on
it: ``cp -r coverage-html-cpp public/cpp`` ran unconditionally and the C++
``<li>`` was printed unconditionally, while the Python half was guarded by a
``[ -d ]``. That split two ways, and neither is what a reader wants. With no
``coverage-html-cpp`` at all the ``cp`` failed and took the job with it (a
GitLab script aborts on a non-zero line), so a run whose Python layer measured
fine published nothing. With the directory present but empty -- gcovr made it
and rendered no ``index.html`` into it -- the ``cp`` succeeded, the link 404'd,
and the job went green.

So the tree is written here, where "the C++ report is missing" is a value the
code can branch on; and what it asks for is the file the link resolves to,
which is the half the shell could not have checked without saying it twice.
"""
import html
import os
from pathlib import Path
import shutil
from typing import NamedTuple

from xmsconan.exit_codes import EXIT_OK

#: Where GitLab Pages publishes from. Part of the fixed output layout, like
#: ``.export/`` and ``wheelhouse/``; the template's ``artifacts: paths:`` names
#: it statically.
PAGES_DIR = "public"

#: The page a directory link resolves to. Both gcovr and pytest-cov write it,
#: and it is what makes a published report a report rather than a directory.
INDEX_NAME = "index.html"


class Section(NamedTuple):
    """One coverage report: where it is rendered, served, and what links it.

    Named rather than a bare 3-tuple because all three fields are ``str``, so
    the table below is three positional strings per row and nothing would
    catch two of them swapped: the report would publish under its own label
    and read as a typo in the HTML rather than as a wrong path.
    """

    #: Directory the coverage job rendered, relative to the source directory.
    rendered: str
    #: Directory it is served from, under :data:`PAGES_DIR`.
    name: str
    #: Link text the index gives it.
    label: str


#: The reports this site can hold, in the order the index lists them: C++
#: first because it is the layer the pipeline gates on.
COVERAGE_SECTIONS = (
    Section("coverage-html-cpp", "cpp", "C++ coverage (gcovr)"),
    Section("coverage-html-py", "python", "Python coverage (pytest-cov)"),
)


def _index_html(library_name, sections):
    """The index page linking each rendered report that exists."""
    title = html.escape(library_name)
    items = "\n".join(
        f'<li><a href="{section.name}/{INDEX_NAME}">{html.escape(section.label)}</a></li>'
        for section in sections
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        f"<title>{title} coverage</title></head><body>\n"
        f"<h1>{title} coverage</h1>\n"
        "<ul>\n"
        f"{items}\n"
        "</ul></body></html>\n"
    )


def write_pages(library_name, source_dir=None, pages_dir=None):
    """Assemble the coverage site under :data:`PAGES_DIR`.

    Each report that was rendered is copied in and linked; each one that was
    not is left out of both. That pairing is the point -- the shell version
    copied and linked C++ unconditionally, which failed the job outright when
    the directory was absent and served a 404 when it was there and empty.

    "Rendered" therefore means the directory holds an :data:`INDEX_NAME`,
    which is the file the index links to. A directory alone is what gcovr
    leaves behind when it makes its output directory and then writes nothing
    into it, and publishing that is the 404 in its quiet form.

    An existing tree is replaced rather than merged, so a re-run cannot serve a
    previous run's report beside this run's index.

    Args:
        library_name: Name shown in the page title and heading.
        source_dir: Directory holding the ``coverage-html-*`` trees; the
            working directory when None.
        pages_dir: Destination; :data:`PAGES_DIR` when None.

    Returns:
        The section directory names that were published, in index order.

    Raises:
        ValueError: No coverage report was found at all -- the site would be
            an index linking nothing, which is worse than a failed job because
            it looks like a measurement of zero -- or the destination exists
            and is not a directory this may replace.
    """
    source = Path(os.getcwd() if source_dir is None else source_dir)
    destination = Path(PAGES_DIR if pages_dir is None else pages_dir)

    published = [section for section in COVERAGE_SECTIONS
                 if (source / section.rendered / INDEX_NAME).is_file()]

    if not published:
        names = ", ".join(section.rendered for section in COVERAGE_SECTIONS)
        raise ValueError(
            f"no coverage report to publish: no {INDEX_NAME} under any of {names} in "
            f"{source}. The coverage job either did not run or did not render its HTML."
        )

    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        # Named rather than deleted. The tree is replaced wholesale, and
        # `shutil.rmtree` on a symlink raises an OSError that reads as a bug
        # in this tool rather than as "that is not the directory you meant".
        raise ValueError(
            f"{destination} is not a directory this can replace: it is a "
            f"{'symlink' if destination.is_symlink() else 'file'}. The pages site is "
            f"written fresh each run, so it needs the path to itself."
        )
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for section in published:
        shutil.copytree(source / section.rendered, destination / section.name)
        print(f"Published {section.rendered} as {destination / section.name}.")

    index = destination / INDEX_NAME
    index.write_text(_index_html(library_name, published), encoding="utf-8")
    print(f"Wrote {index}.")
    return [section.name for section in published]


def job_coverage_pages(library_name):
    """Entry point for ``job coverage --pages``."""
    write_pages(library_name)
    return EXIT_OK
