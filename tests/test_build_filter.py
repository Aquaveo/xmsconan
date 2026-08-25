"""Tests for generator_tools.build_filter."""
import pytest

from xmsconan.generator_tools.build_filter import (
    ci_build_types,
    coverage_conflicts,
    load_build_filter,
)


# --- load_build_filter ---


def test_load_build_filter_defaults_to_empty():
    """A build.toml with no [filter] table yields no restriction."""
    assert load_build_filter({"library_name": "xmscore"}) == {}


def test_load_build_filter_returns_table():
    """A valid table comes back as-is."""
    build_filter = {"build_type": "Release", "options": {"pybind": False}}
    assert load_build_filter({"filter": build_filter}) == build_filter


def test_load_build_filter_reports_build_toml_context():
    """Validation errors name build.toml so the fix location is obvious."""
    with pytest.raises(ValueError, match=r"Invalid \[filter\] table in build.toml"):
        load_build_filter({"filter": {"pybind": True}})


# --- ci_build_types ---


def test_ci_build_types_defaults_to_both():
    """Without a pinned build_type the CI matrix keeps its full fan-out."""
    assert ci_build_types({}) == ["Release", "Debug"]


def test_ci_build_types_follows_pinned_build_type():
    """A pinned build_type drops the CI legs that could only build nothing."""
    assert ci_build_types({"build_type": "Release"}) == ["Release"]


def test_ci_build_types_ignores_unrelated_keys():
    """Filters that don't touch build_type leave the matrix alone."""
    assert ci_build_types({"options": {"pybind": False}}) == ["Release", "Debug"]


def test_ci_build_types_returns_a_copy():
    """Callers mutating the result can't corrupt the module default."""
    first = ci_build_types({})
    first.append("RelWithDebInfo")
    assert ci_build_types({}) == ["Release", "Debug"]


# --- coverage_conflicts ---


def test_coverage_conflicts_empty_for_compatible_filter():
    """A filter that leaves the Debug builds alone reports nothing."""
    assert coverage_conflicts({"compiler.runtime": "dynamic"}) == []


def test_coverage_conflicts_allows_debug_pin():
    """Pinning Debug is exactly what coverage builds, so it isn't a conflict."""
    assert coverage_conflicts({"build_type": "Debug"}) == []


def test_coverage_conflicts_flags_non_debug_build_type():
    """Release-only libraries can't run the coverage job."""
    conflicts = coverage_conflicts({"build_type": "Release"})
    assert len(conflicts) == 1
    assert "Debug" in conflicts[0]


def test_coverage_conflicts_flags_disabled_testing_and_pybind():
    """Both coverage builds are reported when both options are filtered off."""
    conflicts = coverage_conflicts({"options": {"testing": False, "pybind": False}})
    assert len(conflicts) == 2
    assert any("C++" in c for c in conflicts)
    assert any("Python" in c for c in conflicts)


def test_coverage_conflicts_ignores_enabled_options():
    """Requiring testing/pybind is compatible with the coverage builds."""
    assert coverage_conflicts({"options": {"testing": True, "pybind": True}}) == []
