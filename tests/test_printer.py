"""Tests for package_tools.printer."""
import sys
from types import SimpleNamespace

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


# --- print_ascci_art ---


def test_print_ascii_art_contains_version(printer, captured):
    """Version string appears in output."""
    printer.print_ascci_art()
    output = "".join(captured)
    assert "Version:" in output


def test_print_ascii_art_contains_banner(printer, captured):
    """ASCII art banner contains the CPT/Package Tools art."""
    printer.print_ascci_art()
    output = "".join(captured)
    # The banner uses ASCII art letters — check for recognizable fragments
    assert "Package" in output or "____" in output


# --- print_in_docker ---


def test_print_in_docker_contains_container_name(printer, captured):
    """Container name appears in Docker ASCII art."""
    printer.print_in_docker(container="nextms-dev-arm")
    output = "".join(captured)
    assert "nextms-dev-arm" in output


# --- print_command ---


def test_print_command_wraps_with_rules(printer, captured):
    """Command is printed between rules."""
    printer.print_command("cmake --build .")
    output = "".join(captured)
    assert "cmake --build ." in output
    assert "_" * 100 in output


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


# --- print_rule ---


def test_print_rule_default_char(printer, captured):
    """Default rule uses * character, 100 chars wide."""
    printer.print_rule()
    output = "".join(captured)
    assert "*" * 100 in output


def test_print_rule_custom_char(printer, captured):
    """Custom character used for rule."""
    printer.print_rule(char="-")
    output = "".join(captured)
    assert "-" * 100 in output


# --- print_current_page ---


def test_print_current_page(printer, captured):
    """Page format is 'Page: N/M'."""
    printer.print_current_page(2, 5)
    output = "".join(captured)
    assert "Page: 2/5" in output


# --- print_dict ---


def test_print_dict_contains_keys_and_values(printer, captured):
    """Dict keys and values appear in table output."""
    printer.print_dict({"compiler": "gcc", "version": "13"})
    output = "".join(captured)
    assert "compiler" in output
    assert "gcc" in output
    assert "version" in output
    assert "13" in output


# --- foldable_output ---


def test_foldable_output_prints_name(printer, captured):
    """Context manager prints fold name."""
    with printer.foldable_output("test_section"):
        pass
    output = "".join(captured)
    assert "test_section" in output


# --- print_jobs ---


def test_print_jobs_empty_list(printer, captured):
    """Empty job list prints 'no jobs' message."""
    printer.print_jobs([])
    output = "".join(captured)
    assert "no jobs" in output.lower()


def test_print_jobs_with_builds(printer, captured):
    """Non-empty job list renders tabulate table."""
    job = SimpleNamespace(
        settings={"compiler": "gcc", "build_type": "Release"},
        options={"testing": True},
    )
    printer.print_jobs([job])
    output = "".join(captured)
    assert "gcc" in output
    assert "Release" in output


# --- end_fold ---


def test_end_fold_is_noop(printer, captured):
    """end_fold() completes without output."""
    printer.end_fold("section")
    assert captured == []
