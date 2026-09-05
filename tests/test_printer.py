"""Tests for package_tools.printer."""
import sys

import pytest

from xmsconan.package_tools.printer import Printer


@pytest.fixture
def captured():
    """List to capture printer output."""
    return []


@pytest.fixture
def printer(captured):
    """Printer instance that captures output to a list."""
    return Printer(printer=captured.append)


# --- init ---


def test_default_printer_uses_stdout():
    """Default init uses sys.stdout.write."""
    p = Printer()
    assert p.printer == sys.stdout.write


def test_custom_printer_captures_output(printer, captured):
    """Callable captures output."""
    printer.printer("hello")
    assert captured == ["hello"]


# --- print_ascii_art ---


def test_print_ascii_art_contains_version(printer, captured):
    """Version string appears in output."""
    printer.print_ascii_art()
    output = "".join(captured)
    assert "Version:" in output


def test_print_ascii_art_contains_banner(printer, captured):
    """The whole six-row banner block renders, not just its first row.

    The old assertion was `"Package" in output or "____" in output`. The left
    side is false in every run -- the banner draws its words as ASCII art and
    never spells them -- so the whole check rested on the right side, which any
    run of underscores satisfies. A banner truncated to one row passed it.
    """
    printer.print_ascii_art()
    output = "".join(captured)
    art = [
        line for line in output.splitlines()
        if line.strip() and not line.startswith("Version:")
    ]

    assert len(art) == 6
    assert art[0].lstrip().startswith("____ ____ _____")
    assert art[-1].rstrip().endswith("/_/")


def test_print_ascci_art_is_the_same_method(printer):
    """The misspelled name is kept for one release, bound to the very same method."""
    assert printer.print_ascci_art.__func__ is printer.print_ascii_art.__func__


# --- print_message ---


def test_print_message_title_only(printer, captured):
    """Title printed without body."""
    printer.print_message("Building")
    output = "".join(captured)
    assert "Building" in output


def test_print_message_title_and_body(printer, captured):
    """Both title and body printed."""
    printer.print_message("Step 1", body="Details here")
    output = "".join(captured)
    assert "Step 1" in output
    assert "Details here" in output


# --- print_profile ---


def test_print_profile_contains_text(printer, captured):
    """Profile text appears in tabulate output."""
    printer.print_profile("/path/to/profile")
    output = "".join(captured)
    assert "/path/to/profile" in output
    assert "Profile" in output
