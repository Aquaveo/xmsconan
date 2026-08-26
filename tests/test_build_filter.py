"""Tests for generator_tools.build_filter."""
import pytest

from xmsconan.generator_tools.build_filter import (
    ci_build_types,
    CI_JOB_SETTINGS,
    ci_python_versions,
    ci_wheel_enabled,
    coverage_conflicts,
    DEFAULT_CI_BUILD_TYPES,
    empty_ci_jobs,
    load_build_filter,
)
from xmsconan.package_tools.packager import configurations


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


def test_load_build_filter_rejects_filter_matching_nothing():
    """Individually valid options that cannot hold at once fail at generation.

    No generated configuration sets both ``testing`` and ``pybind``, so this
    filter empties the matrix on every platform — previously that surfaced only
    as an exit-1 from every CI leg, after the workflow had been committed.
    """
    with pytest.raises(ValueError, match="matches no configuration on any platform"):
        load_build_filter({"filter": {"options": {"testing": True, "pybind": True}}})


def test_load_build_filter_rejects_python_version_outside_ci_versions():
    """A python_version pin nothing builds is rejected rather than dropping the wheel.

    The matrix stays non-empty (every non-pybind configuration survives), so
    only the pybind-specific check catches this one.
    """
    with pytest.raises(ValueError, match="matches no pybind configuration"):
        load_build_filter({"filter": {"options": {"python_version": "3.9"}}})


def test_load_build_filter_accepts_python_version_from_ci_config():
    """A pin that [ci].python_versions does build is accepted."""
    toml_data = {
        "ci": {"python_versions": ["3.10", "3.13"]},
        "filter": {"options": {"python_version": "3.10"}},
    }
    assert load_build_filter(toml_data) == {"options": {"python_version": "3.10"}}


@pytest.mark.parametrize("build_filter,expected", [
    pytest.param({"compiler.cppstd": "17"}, ["mac", "linux"], id="cppstd-msvc-only"),
    pytest.param({"compiler.libcxx": "libc++"}, ["linux"], id="libcxx-apple-only"),
])
def test_empty_ci_jobs_covers_the_cppstd_and_libcxx_pins(build_filter, expected):
    """Cppstd and libcxx are documented filter keys, so they must be accounted for.

    They differ per platform (gnu17 vs 17, libstdc++11 vs libc++) and Windows
    declares no libcxx at all, so pinning one empties some job blocks and is a
    no-op for others. Leaving them out of CI_JOB_SETTINGS made those jobs go
    unwarned -- the matrix stays non-empty on the platform that matches, so
    load_build_filter does not reject them either.
    """
    emitted = ["mac", "linux", "windows"]

    assert empty_ci_jobs(build_filter, "github", emitted) == expected


def test_ci_job_settings_match_the_platform_matrix():
    """Every fixed job setting is a value that platform's configurations emit.

    Hand-listed job settings drift from the matrix silently: a wrong value makes
    empty_ci_jobs warn about a healthy job or stay quiet about a dead one, and
    nothing else would notice.
    """
    platform_of = {
        "mac": "darwin", "linux": "linux", "linux-arm": "linux", "windows": "windows",
        "Conan Build": "linux", "Conan Build - Windows": "windows",
    }

    for ci_type, jobs in CI_JOB_SETTINGS.items():
        for job_name, settings in jobs.items():
            emitted = configurations[platform_of[job_name]]
            for key, value in settings.items():
                # linux-arm runs the Linux configuration block on an ARM runner,
                # so its arch is the one setting that is not the block's own.
                if key == "arch" and job_name == "linux-arm":
                    continue
                assert key in emitted, f"{ci_type}/{job_name}: {key} is not a setting"
                assert value in emitted[key], (
                    f"{ci_type}/{job_name}: {key}={value!r} not in {emitted[key]}"
                )


def test_load_build_filter_accepts_a_buildenv_pin():
    """A [filter.buildenv] pin is not judged unbuildable at generation time.

    Those values come from the environment doing the generating -- XMS_VERSION
    and the AQUAPI_* names are os.getenv in generate_configurations, and
    XMS_TEST_ARTIFACTS_LABEL is added per configuration by run() -- so at
    generation time they are None or absent. Checking them for emptiness
    rejected every buildenv pin, making a documented feature unusable.
    """
    for name in ("XMS_VERSION", "XMS_COVERAGE", "XMS_TEST_ARTIFACTS_LABEL"):
        toml_data = {"filter": {"buildenv": {name: "1"}}}

        assert load_build_filter(toml_data) == {"buildenv": {name: "1"}}


def test_load_build_filter_still_rejects_settings_beside_a_buildenv_pin():
    """Skipping buildenv must not smuggle an impossible options table past the check."""
    toml_data = {"filter": {
        "buildenv": {"XMS_VERSION": "1"},
        "options": {"testing": True, "pybind": True},
    }}

    with pytest.raises(ValueError, match="matches no configuration"):
        load_build_filter(toml_data)


def test_load_build_filter_rejects_a_runtime_matrix_excludes():
    """[matrix] and [filter] can each be valid and together build nothing.

    compiler.runtime is Windows-only, so the all-platforms emptiness check can't
    see this: the pin is a no-op on Linux and macOS, which stay non-empty.
    """
    toml_data = {
        "matrix": {"compiler_runtime": ["dynamic"]},
        "filter": {"compiler.runtime": "static"},
    }

    with pytest.raises(ValueError, match="builds only"):
        load_build_filter(toml_data)


def test_load_build_filter_accepts_a_runtime_the_matrix_keeps():
    """The same pin is fine when [matrix] still builds it."""
    toml_data = {
        "matrix": {"compiler_runtime": ["dynamic", "static"]},
        "filter": {"compiler.runtime": "static"},
    }

    assert load_build_filter(toml_data) == {"compiler.runtime": "static"}


def test_load_build_filter_checks_the_filter_against_the_narrowed_matrix():
    """A pybind build_type [matrix] adds is buildable, and one it omits is not.

    Validating against the unnarrowed fan-out gets this wrong in both
    directions: it accepts a filter that builds nothing, and rejects one that
    builds fine.
    """
    debug_pybind = {"build_type": "Debug", "options": {"pybind": True}}

    with pytest.raises(ValueError, match="matches no configuration"):
        load_build_filter({"filter": debug_pybind})

    widened = {"matrix": {"pybind_build_types": ["Release", "Debug"]},
               "filter": debug_pybind}
    assert load_build_filter(widened) == debug_pybind


def test_ci_python_versions_defaults_to_none():
    """No [ci].python_versions means "let the packager decide"."""
    assert ci_python_versions({}) is None
    assert ci_python_versions({"ci": {"python_versions": ["3.13"]}}) == ["3.13"]


def test_ci_python_versions_unions_the_per_platform_keys():
    """Every [ci] python list counts, sorted numerically rather than lexically.

    linux_python_versions and mac_python_versions override python_versions for
    their platform, so a version named only there is still built.
    """
    toml_data = {"ci": {
        "python_versions": ["3.13"],
        "linux_python_versions": ["3.14"],
        "mac_python_versions": ["3.9"],
    }}

    assert ci_python_versions(toml_data) == ["3.9", "3.13", "3.14"]


def test_load_build_filter_accepts_python_version_from_a_platform_key():
    """A pin only linux_python_versions names is accepted, not rejected.

    Validating against [ci].python_versions alone would fail `xmsconan gen` on a
    build.toml whose Linux legs really do build that ABI.
    """
    toml_data = {
        "ci": {"python_versions": ["3.13"], "linux_python_versions": ["3.13", "3.14"]},
        "filter": {"options": {"python_version": "3.14"}},
    }

    assert load_build_filter(toml_data) == {"options": {"python_version": "3.14"}}


# --- ci_build_types ---


@pytest.mark.parametrize("build_filter,expected", [
    pytest.param({}, ["Release", "Debug"], id="no-filter"),
    pytest.param({"build_type": "Release"}, ["Release"], id="pinned-release"),
    pytest.param({"build_type": "Debug"}, ["Debug"], id="pinned-debug"),
    pytest.param({"options": {"testing": True}}, ["Release", "Debug"], id="unrelated-key"),
])
def test_ci_build_types(build_filter, expected):
    """The CI matrix follows a pinned build_type and ignores everything else."""
    assert ci_build_types(build_filter) == expected


def test_ci_build_types_returns_a_fresh_list():
    """Callers get a list of their own, not the module default."""
    assert ci_build_types({}) is not DEFAULT_CI_BUILD_TYPES
    assert isinstance(DEFAULT_CI_BUILD_TYPES, tuple), "the default must not be mutable"


# --- ci_wheel_enabled ---


@pytest.mark.parametrize("build_filter,expected", [
    pytest.param({}, True, id="no-filter"),
    pytest.param({"options": {"pybind": True}}, True, id="pybind-required"),
    pytest.param({"options": {"testing": True}}, True, id="unrelated-option"),
    pytest.param({"options": {"pybind": False}}, False, id="pybind-excluded"),
])
def test_ci_wheel_enabled(build_filter, expected):
    """Wheel steps come out only when the filter excludes the pybind builds."""
    assert ci_wheel_enabled(build_filter) is expected


# --- empty_ci_jobs ---


def test_empty_ci_jobs_none_for_build_type_only_filter():
    """build_type is a matrix axis, so pinning it empties no job."""
    jobs = ["mac", "linux", "windows"]
    assert empty_ci_jobs({"build_type": "Release"}, "github", jobs) == []


def test_empty_ci_jobs_flags_platform_pins():
    """An os pin leaves every non-matching job block with nothing to build."""
    jobs = ["mac", "linux", "linux-arm", "windows"]
    assert empty_ci_jobs({"os": "Windows"}, "github", jobs) == ["mac", "linux", "linux-arm"]


def test_empty_ci_jobs_flags_arch_and_compiler_pins():
    """The arch and compiler settings are fixed per job block too."""
    jobs = ["mac", "linux", "linux-arm", "windows"]
    assert empty_ci_jobs({"arch": "armv8"}, "github", jobs) == ["linux", "windows"]
    assert empty_ci_jobs({"compiler": "gcc"}, "github", jobs) == ["mac", "windows"]


def test_empty_ci_jobs_only_considers_emitted_jobs():
    """A job the [ci] toggles left out isn't reported as empty."""
    assert empty_ci_jobs({"os": "Windows"}, "github", ["windows"]) == []


def test_empty_ci_jobs_handles_gitlab_job_names():
    """The GitLab build jobs are named differently from GitHub's blocks."""
    jobs = ["Conan Build", "Conan Build - Windows"]
    assert empty_ci_jobs({"os": "Windows"}, "gitlab", jobs) == ["Conan Build"]


# --- coverage_conflicts ---


def test_coverage_conflicts_empty_for_compatible_filter():
    """A filter that leaves the Debug builds alone reports nothing."""
    assert coverage_conflicts({"compiler.runtime": "dynamic"}, "3.13") == []


def test_coverage_conflicts_allows_debug_pin():
    """Pinning Debug is exactly what coverage builds, so it isn't a conflict."""
    assert coverage_conflicts({"build_type": "Debug"}, "3.13") == []


def test_coverage_conflicts_flags_non_debug_build_type_once():
    """Release-only libraries can't run the coverage job — reported once, not per report."""
    conflicts = coverage_conflicts({"build_type": "Release"}, "3.13")
    assert len(conflicts) == 1
    assert "Debug" in conflicts[0]


@pytest.mark.parametrize("option,value,report", [
    pytest.param("testing", False, "C++", id="testing-excluded"),
    pytest.param("pybind", False, "Python", id="pybind-excluded"),
    pytest.param("testing", True, "Python", id="testing-required"),
    pytest.param("pybind", True, "C++", id="pybind-required"),
])
def test_coverage_conflicts_flags_both_directions(option, value, report):
    """The two coverage builds pin inverse options, so requiring one cancels the other.

    ``pybind = true`` is as fatal to the C++ coverage build as ``pybind = false``
    is to the Python one — the ``is False``-only check missed that half.
    """
    conflicts = coverage_conflicts({"options": {option: value}}, "3.13")
    assert len(conflicts) == 1
    assert report in conflicts[0]
    assert f"options.{option}" in conflicts[0]


def test_coverage_conflicts_flags_mismatched_python_version():
    """The Python coverage build pins one ABI; a filter pinning another cancels it."""
    conflicts = coverage_conflicts({"options": {"python_version": "3.10"}}, "3.13")
    assert len(conflicts) == 1
    assert "python_version" in conflicts[0]


def test_coverage_conflicts_accepts_matching_python_version():
    """Pinning the ABI coverage already uses is not a conflict."""
    assert coverage_conflicts({"options": {"python_version": "3.13"}}, "3.13") == []


def test_coverage_conflicts_skips_python_version_when_unresolved():
    """Callers without a resolved coverage ABI don't get a bogus conflict."""
    assert coverage_conflicts({"options": {"python_version": "3.10"}}, None) == []
