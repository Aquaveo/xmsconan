"""Shared helpers for the documentation-drift tests.

Kept out of ``tests/ci_helpers.py``, which is about generating and inspecting
CI files: these slice Markdown, and the suites that assert against
``docs/USAGE.md`` are not only the CI ones. ``test_wheel_deploy.py`` owned the
only copy until ``test_ci_file_generator.py`` grew a second, bare
``str.index`` one -- which is the failure these exist to prevent.
"""
from pathlib import Path


def slice_between(text, start, end, source):
    """The text of *source* from heading *start* up to heading *end*.

    Both headings are asserted before they are used as offsets. ``str.index``
    alone answers a renamed or renumbered heading with ``ValueError:
    substring not found``, which says nothing about which document moved --
    and the whole point of these tests is to name the drift.
    """
    assert start in text, f"{source} no longer contains {start!r}"
    assert end in text, f"{source} no longer contains {end!r}"
    return text[text.index(start):text.index(end)]


def usage_text():
    """The full text of ``docs/USAGE.md``."""
    return (Path(__file__).parent.parent / "docs" / "USAGE.md").read_text(encoding="utf-8")


def usage_section(number):
    """The text of ``docs/USAGE.md`` section *number*."""
    return slice_between(usage_text(), f"\n## {number}.", f"\n## {number + 1}.", "docs/USAGE.md")
