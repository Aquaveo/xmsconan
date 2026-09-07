"""``xmsconan job coverage --pages`` -- the coverage site GitLab Pages serves.

Fourteen lines of ``echo`` inside a YAML block scalar, rendered twice on two
Jinja branches, built this HTML. Duplicated markup in a template is the shape
where the two copies drift, and the copies here were already one comment apart;
worse, the shell that wrote it swallowed its own failures -- ``cp -r
coverage-html-cpp public/cpp`` on a run whose C++ layer produced nothing left
``public/`` with an index linking to a page that is not there, and the job went
green.

So the tree is written here, where "the C++ report is missing" is a value the
code can branch on rather than a ``cp`` that printed to stderr and carried on.
"""
import html
import os
from pathlib import Path
import shutil

from xmsconan.exit_codes import EXIT_OK

#: Where GitLab Pages publishes from. Part of the fixed output layout, like
#: ``.export/`` and ``wheelhouse/``; the template's ``artifacts: paths:`` names
#: it statically.
PAGES_DIR = "public"

#: Rendered coverage directory -> its path under :data:`PAGES_DIR` and the link
#: text the index gives it. Order is the order the index lists them in: C++
#: first because it is the layer the pipeline gates on.
COVERAGE_SECTIONS = (
    ("coverage-html-cpp", "cpp", "C++ coverage (gcovr)"),
    ("coverage-html-py", "python", "Python coverage (pytest-cov)"),
)


def _index_html(library_name, sections):
    """The index page linking each rendered report that exists."""
    title = html.escape(library_name)
    items = "\n".join(
        f'<li><a href="{destination}/index.html">{html.escape(label)}</a></li>'
        for destination, label in sections
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
    linked C++ unconditionally, so a pipeline whose C++ layer did not measure
    published an index whose first link 404s.

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
        ValueError: No coverage report was found at all. The site would be an
            index linking nothing, which is worse than a failed job: it looks
            like a measurement of zero.
    """
    source = Path(os.getcwd() if source_dir is None else source_dir)
    destination = Path(PAGES_DIR if pages_dir is None else pages_dir)

    published = []
    for rendered, name, label in COVERAGE_SECTIONS:
        if (source / rendered).is_dir():
            published.append((rendered, name, label))

    if not published:
        names = ", ".join(rendered for rendered, _, _ in COVERAGE_SECTIONS)
        raise ValueError(
            f"no coverage report to publish: none of {names} is in {source}. The "
            f"coverage job either did not run or did not render its HTML."
        )

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for rendered, name, _ in published:
        shutil.copytree(source / rendered, destination / name)
        print(f"Published {rendered} as {destination / name}.")

    index = destination / "index.html"
    index.write_text(
        _index_html(library_name, [(name, label) for _, name, label in published]),
        encoding="utf-8",
    )
    print(f"Wrote {index}.")
    return [name for _, name, _ in published]


def job_coverage_pages(library_name):
    """Entry point for ``job coverage --pages``."""
    write_pages(library_name)
    return EXIT_OK
