"""Tests for generator_tools.build_filter."""
import pytest

from xmsconan.generator_tools.build_filter import (
    ci_filter_effects,
    CI_JOB_SETTINGS,
    ci_python_versions,
    coverage_conflicts,
    DEFAULT_CI_BUILD_TYPES,
    empty_ci_jobs,
    load_build_filter,
)
from xmsconan.package_tools.packager import configurations
from .utils import make_build_toml


# --- load_build_filter ---


def test_load_build_filter_defaults_to_empty():
    """A build.toml with no [filter] table yields no restriction."""
    assert load_build_filter(make_build_toml()) == {}


def test_load_build_filter_returns_table():
    """A valid table comes back as-is."""
    build_filter = {"build_type": "Release", "options": {"pybind": False}}
    assert load_build_filter(make_build_toml(filter=build_filter)) == build_filter


def test_load_build_filter_reports_build_toml_context():
    """Validation errors name build.toml so the fix location is obvious."""
    with pytest.raises(ValueError, match=r"Invalid \[filter\] table in build.toml"):
        load_build_filter(make_build_toml(filter={"pybind": True}))


def test_load_build_filter_rejects_filter_matching_nothing():
    """Individually valid options that cannot hold at once fail at generation.

    No generated configuration sets both ``testing`` and ``pybind``, so this
    filter empties the matrix on every platform — previously that surfaced only
    as an exit-1 from every CI leg, after the workflow had been committed.
    """
    with pytest.raises(ValueError, match="matches no configuration on any platform"):
        load_build_filter(make_build_toml(filter={"options": {"testing": True, "pybind": True}}))


def test_load_build_filter_rejects_python_version_outside_ci_versions():
    """A python_version pin nothing builds is rejected rather than dropping the wheel.

    The matrix stays non-empty (every non-pybind configuration survives), so
    only the pybind-specific check catches this one.
    """
    with pytest.raises(ValueError, match="matches no pybind configuration"):
        load_build_filter(make_build_toml(filter={"options": {"python_version": "3.9"}}))


def test_load_build_filter_accepts_python_version_from_ci_config():
    """A pin that [ci].python_versions does build is accepted."""
    config = make_build_toml(
        ci={"python_versions": ["3.10", "3.13"]},
        filter={"options": {"python_version": "3.10"}},
    )
    assert load_build_filter(config) == {"options": {"python_version": "3.10"}}


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
        config = make_build_toml(filter={"buildenv": {name: "1"}})

        assert load_build_filter(config) == {"buildenv": {name: "1"}}


def test_load_build_filter_still_rejects_settings_beside_a_buildenv_pin():
    """Skipping buildenv must not smuggle an impossible options table past the check."""
    config = make_build_toml(filter={
        "buildenv": {"XMS_VERSION": "1"},
        "options": {"testing": True, "pybind": True},
    })

    with pytest.raises(ValueError, match="matches no configuration"):
        load_build_filter(config)


def test_load_build_filter_rejects_a_runtime_matrix_excludes():
    """[matrix] and [filter] can each be valid and together build nothing.

    compiler.runtime is Windows-only, so the all-platforms emptiness check can't
    see this: the pin is a no-op on Linux and macOS, which stay non-empty.
    """
    config = make_build_toml(
        matrix={"compiler_runtime": ["dynamic"]},
        filter={"compiler.runtime": "static"},
    )

    with pytest.raises(ValueError, match="builds only"):
        load_build_filter(config)


def test_load_build_filter_accepts_a_runtime_the_matrix_keeps():
    """The same pin is fine when [matrix] still builds it."""
    config = make_build_toml(
        matrix={"compiler_runtime": ["dynamic", "static"]},
        filter={"compiler.runtime": "static"},
    )

    assert load_build_filter(config) == {"compiler.runtime": "static"}


def test_load_build_filter_checks_the_filter_against_the_narrowed_matrix():
    """A pybind build_type [matrix] adds is buildable, and one it omits is not.

    Validating against the unnarrowed fan-out gets this wrong in both
    directions: it accepts a filter that builds nothing, and rejects one that
    builds fine.
    """
    debug_pybind = {"build_type": "Debug", "options": {"pybind": True}}

    with pytest.raises(ValueError, match="matches no configuration"):
        load_build_filter(make_build_toml(filter=debug_pybind))

    widened = make_build_toml(
        matrix={"pybind_build_types": ["Release", "Debug"]},
        filter=debug_pybind,
    )
    assert load_build_filter(widened) == debug_pybind


def test_ci_python_versions_defaults_to_the_packager_default():
    """With no [ci] table the union is the one version the packager builds anyway."""
    assert ci_python_versions(make_build_toml()) == ["3.13"]
    assert ci_python_versions(make_build_toml(ci={"python_versions": ["3.13"]})) == ["3.13"]


def test_ci_python_versions_unions_the_per_platform_keys():
    """Every [ci] python list counts, sorted numerically rather than lexically.

    linux_python_versions and mac_python_versions override python_versions for
    their platform, so a version named only there is still built.
    """
    config = make_build_toml(ci={
        "python_versions": ["3.13"],
        "linux_python_versions": ["3.14"],
        "mac_python_versions": ["3.9"],
    })

    assert ci_python_versions(config) == ["3.9", "3.13", "3.14"]


def test_load_build_filter_accepts_python_version_from_a_platform_key():
    """A pin only linux_python_versions names is accepted, not rejected.

    Validating against [ci].python_versions alone would fail `xmsconan gen` on a
    build.toml whose Linux legs really do build that ABI.
    """
    config = make_build_toml(
        ci={"python_versions": ["3.13"], "linux_python_versions": ["3.13", "3.14"]},
        filter={"options": {"python_version": "3.14"}},
    )

    assert load_build_filter(config) == {"options": {"python_version": "3.14"}}


# --- ci_filter_effects ---


def _effects(build_filter, matrix=None, ci=None):
    """Measure a filter the way generate_ci does, through load_build_filter."""
    overrides = {"filter": build_filter}
    if matrix:
        overrides["matrix"] = matrix
    if ci:
        overrides["ci"] = ci
    config = make_build_toml(**overrides)
    build_filter = load_build_filter(config)
    return ci_filter_effects(build_filter, config)


@pytest.mark.parametrize("build_filter,expected", [
    pytest.param({}, ["Release", "Debug"], id="no-filter"),
    pytest.param({"build_type": "Release"}, ["Release"], id="pinned-release"),
    pytest.param({"build_type": "Debug"}, ["Debug"], id="pinned-debug"),
    # A pybind-only filter pins no build_type, but the default
    # [matrix].pybind_build_types is Release-only, so the Debug leg would select
    # zero configurations and build.py would exit 1 on a required check.
    pytest.param({"options": {"pybind": True}}, ["Release"], id="pybind-required"),
    pytest.param({"options": {"python_version": "3.13"}}, ["Release"], id="python-version-pin"),
    # testing variants exist for both build types, so neither leg is empty.
    pytest.param({"options": {"testing": True}}, ["Release", "Debug"], id="testing-required"),
])
def test_ci_build_types_follow_what_survives(build_filter, expected):
    """The CI matrix keeps a build type only when configurations survive on it.

    Keying on whether `build_type` was pinned left a leg that builds nothing
    whenever some *other* key excluded a whole build type's configurations.
    """
    assert _effects(build_filter)["build_types"] == expected


def test_ci_build_types_widen_with_the_matrix_table():
    """A Debug pybind leg is legitimate once [matrix] asks for Debug modules."""
    effects = _effects({"options": {"pybind": True}},
                       matrix={"pybind_build_types": ["Release", "Debug"]})

    assert effects["build_types"] == ["Release", "Debug"]


def test_ci_build_types_returns_a_fresh_list():
    """Callers get a list of their own, not the module default."""
    assert _effects({})["build_types"] is not DEFAULT_CI_BUILD_TYPES
    assert isinstance(DEFAULT_CI_BUILD_TYPES, tuple), "the default must not be mutable"


@pytest.mark.parametrize("build_filter,expected", [
    pytest.param({}, {"mac": True, "linux": True, "windows": True}, id="no-filter"),
    pytest.param({"options": {"pybind": True}},
                 {"mac": True, "linux": True, "windows": True}, id="pybind-required"),
    pytest.param({"options": {"pybind": False}},
                 {"mac": False, "linux": False, "windows": False}, id="pybind-excluded"),
    # Each of these empties the pybind subset without naming pybind at all.
    pytest.param({"build_type": "Debug"},
                 {"mac": False, "linux": False, "windows": False}, id="debug-has-no-pybind"),
    pytest.param({"options": {"testing": True}},
                 {"mac": False, "linux": False, "windows": False},
                 id="testing-is-disjoint-from-pybind"),
    # Windows-only: msvc pybind variants are produced for the dynamic runtime
    # only, while Linux and macOS declare no compiler.runtime to match against.
    pytest.param({"compiler.runtime": "static"},
                 {"mac": True, "linux": True, "windows": False}, id="static-runtime-windows-only"),
])
def test_wheel_steps_follow_the_surviving_pybind_count(build_filter, expected):
    """Wheel steps come out per platform, on survivors rather than on the pin.

    Asking only whether `pybind` was pinned False kept the wheel steps for
    filters that build no wheel: on GitLab `build.py` exits 1 when --wheel-dir
    extracts nothing, and on GitHub the Release-gated steps publish a release
    with no wheel in it and stay green.
    """
    assert _effects(build_filter)["wheel_enabled"] == expected


def test_wheel_steps_survive_where_the_matrix_restores_pybind():
    """A Debug pin is fine when [matrix] builds Debug modules."""
    effects = _effects({"build_type": "Debug"},
                       matrix={"pybind_build_types": ["Release", "Debug"]})

    assert effects["wheel_enabled"] == {"mac": True, "linux": True, "windows": True}


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
    """A filter that leaves both coverage builds alone reports nothing."""
    assert coverage_conflicts({"compiler.runtime": "dynamic"}, "3.13") == []


def test_coverage_conflicts_empty_when_build_type_unpinned():
    """Leaving build_type open is the only way both coverage builds survive."""
    assert coverage_conflicts({}, "3.13") == []


@pytest.mark.parametrize("pinned,cancelled,survives", [
    pytest.param("Debug", "Python", "Release", id="debug-pin-cancels-python"),
    pytest.param("Release", "C++", "Debug", id="release-pin-cancels-cpp"),
])
def test_coverage_conflicts_flags_any_build_type_pin(pinned, cancelled, survives):
    """The two coverage builds pin different types, so any pin cancels exactly one.

    The C++ report comes from a Debug build and the Python report from a Release
    one, so there is no build_type a filter can pin that leaves both alive.
    """
    conflicts = coverage_conflicts({"build_type": pinned}, "3.13")
    assert len(conflicts) == 1
    assert f'build_type = "{pinned}"' in conflicts[0]
    assert f"{cancelled} coverage build" in conflicts[0]
    assert f"pins {survives}" in conflicts[0]


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
