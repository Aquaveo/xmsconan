"""Tests for package_tools.packager."""
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from xmsconan.package_tools.packager import get_current_arch, XmsConanPackager
from .utils import patch_env

# --- get_current_arch ---


@pytest.mark.parametrize("machine,expected", [
    ("x86_64", "x86_64"),
    ("AMD64", "x86_64"),
    ("arm64", "armv8"),
    ("aarch64", "armv8"),
    ("riscv64", "riscv64"),
])
def test_get_current_arch(machine, expected):
    """Platform machine string maps to Conan architecture."""
    with patch("xmsconan.package_tools.packager.platform.machine", return_value=machine):
        assert get_current_arch() == expected


# --- XmsConanPackager init / properties ---


def test_init_sets_library_name():
    """library_name property returns init value."""
    p = XmsConanPackager("xmscore")
    assert p.library_name == "xmscore"


def test_configurations_none_before_generate():
    """Verify configurations is None before generate_configurations is called."""
    p = XmsConanPackager("xmscore")
    assert p.configurations is None


# --- generate_configurations ---


@patch_env(clear=True)
def test_generate_configurations_linux():
    """Linux config has expected shape."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="linux")
    assert len(configs) > 0
    base = configs[0]
    assert base["os"] == "Linux"
    assert base["compiler"] == "gcc"
    assert "options" in base
    assert "buildenv" in base


@patch_env(clear=True)
def test_generate_configurations_windows():
    """Windows config uses msvc compiler."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="windows")
    base = configs[0]
    assert base["os"] == "Windows"
    assert base["compiler"] == "msvc"


@patch_env(clear=True)
def test_generate_configurations_darwin():
    """Verify macOS config sets MACOSX_DEPLOYMENT_TARGET."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="darwin")
    base = configs[0]
    assert base["os"] == "Macos"
    assert base["compiler"] == "apple-clang"
    assert base["buildenv"].get("MACOSX_DEPLOYMENT_TARGET") == "15.0"


@patch_env(clear=True)
def test_generate_configurations_darwin_arm_sets_host_platform():
    """Verify macOS ARM config sets _PYTHON_HOST_PLATFORM to avoid universal2 tag."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="darwin")
    arm_configs = [c for c in configs if c["arch"] == "armv8"]
    assert len(arm_configs) > 0
    for cfg in arm_configs:
        assert cfg["buildenv"].get("_PYTHON_HOST_PLATFORM") == "macosx-15.0-arm64"


@patch_env(clear=True)
def test_generate_configurations_includes_variants():
    """Windows produces base + wchar_t + pybind + testing variants."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="windows")
    # Windows has 4 base combos (2 build_type x 2 runtime)
    # + wchar_t variants (4 msvc combos)
    # + pybind variants (Release/dynamic only = 1)
    # + testing variants (4 combos)
    # Total should be > 4
    assert len(configs) > 4

    # Verify variant types exist
    has_typedef = any(c["options"].get("wchar_t") == "typedef" for c in configs)
    has_pybind = any(c["options"].get("pybind") is True for c in configs)
    has_testing = any(c["options"].get("testing") is True for c in configs)
    assert has_typedef
    assert has_pybind
    assert has_testing


@patch_env(clear=True)
def test_wchar_t_variants_only_for_msvc():
    """Only msvc compiler gets wchar_t=typedef variants."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    typedef_configs = [c for c in p.configurations if c["options"].get("wchar_t") == "typedef"]
    assert len(typedef_configs) == 0


@patch_env(clear=True)
def test_pybind_variants_exclude_debug():
    """No pybind variant for Debug build_type."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    pybind_configs = [c for c in p.configurations if c["options"].get("pybind") is True]
    for cfg in pybind_configs:
        assert cfg["build_type"] != "Debug"


@patch_env(clear=True)
def test_pybind_debug_emitted_when_coverage_flag():
    """coverage=True relaxes the Debug gate so xmsconan_coverage finds a build."""
    p = XmsConanPackager("xmscore", coverage=True)
    p.generate_configurations(system_platform="linux")

    debug_pybind = [
        c for c in p.configurations
        if c["build_type"] == "Debug" and c["options"].get("pybind") is True
    ]
    assert debug_pybind, (
        "coverage=True must emit at least one Debug+pybind config — without "
        "it xmsconan_coverage cannot locate a matching package in the cache"
    )


@patch_env({"XMS_COVERAGE": "1"}, clear=True)
def test_pybind_debug_emitted_when_xms_coverage_env():
    """XMS_COVERAGE=1 in the env is treated as coverage=True by default."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    debug_pybind = [
        c for c in p.configurations
        if c["build_type"] == "Debug" and c["options"].get("pybind") is True
    ]
    assert debug_pybind


@patch_env(clear=True)
def test_pybind_and_testing_are_never_combined_in_one_config():
    """The pybind=True+testing=True combination must never be emitted.

    The packager's three derivative loops (wchar_t, pybind, testing) are
    independent fan-outs that deliberately do not cross-multiply. The
    coverage runner (``xmsconan_coverage``) drives two separate builds —
    one ``testing=True+pybind=False`` for CxxTest C++ coverage and one
    ``testing=False+pybind=True`` for pytest-cov Python coverage — so it
    no longer needs a combined config. A combined config also broke at
    pybind ``dlopen`` time when ``testing_sources`` linkage shifted (the
    pybind ``.so`` carried unresolved CxxTest symbols into the venv).
    """
    for coverage_flag in (True, False):
        p = XmsConanPackager("xmscore", coverage=coverage_flag)
        p.generate_configurations(system_platform="linux")
        combined = []
        for c in p.configurations:
            pybind_on = c["options"].get("pybind") is True
            testing_on = c["options"].get("testing") is True
            if pybind_on and testing_on:
                combined.append(c)
        assert not combined, (
            f"pybind=True+testing=True must never be emitted "
            f"(coverage={coverage_flag}); got {len(combined)} such configs: "
            f"{[c['options'] for c in combined]!r}"
        )


@patch_env({"XMS_COVERAGE": "1"}, clear=True)
def test_explicit_coverage_false_overrides_env():
    """coverage=False wins over an XMS_COVERAGE=1 env (explicit beats implicit)."""
    p = XmsConanPackager("xmscore", coverage=False)
    p.generate_configurations(system_platform="linux")

    debug_pybind = [
        c for c in p.configurations
        if c["build_type"] == "Debug" and c["options"].get("pybind") is True
    ]
    assert not debug_pybind


@patch_env(clear=True)
def test_coverage_true_propagates_xms_coverage_into_buildenv():
    """coverage=True must export XMS_COVERAGE into the profile's [buildenv].

    Without it the activation script never sets ``XMS_COVERAGE`` for the
    CMake child process, so ``CMakeLists.txt.jinja``'s
    ``if (DEFINED ENV{XMS_COVERAGE})`` guard is false, ``--coverage`` is
    never added to the compile flags, no ``.gcno``/``.gcda`` files are
    produced, and gcovr reports 0% with "All coverage data is filtered
    out" (issue #69 — the 2.14.0/2.14.1/2.14.2 chain finally got the
    pipeline to a green run, but the C++ report was empty for exactly
    this reason).
    """
    p = XmsConanPackager("xmscore", coverage=True)
    p.generate_configurations(system_platform="linux")

    for cfg in p.configurations:
        assert cfg["buildenv"].get("XMS_COVERAGE") == "1", (
            f"every configuration must export XMS_COVERAGE=1 in buildenv "
            f"when coverage=True; missing on {cfg.get('build_type')!r} / "
            f"options={cfg.get('options')}"
        )


@patch_env(clear=True)
def test_coverage_false_omits_xms_coverage_from_buildenv():
    """coverage=False must not leak XMS_COVERAGE into the build profile.

    A non-coverage run reusing the same packager invocation must not
    pull instrumentation flags into the released binary — that would
    bloat object size, defeat optimization, and (more importantly)
    cause ``--coverage`` to be linked into the production wheel.
    """
    p = XmsConanPackager("xmscore", coverage=False)
    p.generate_configurations(system_platform="linux")

    for cfg in p.configurations:
        assert "XMS_COVERAGE" not in cfg["buildenv"], (
            f"coverage=False must not export XMS_COVERAGE; leaked on "
            f"{cfg.get('build_type')!r} / options={cfg.get('options')}"
        )


@patch_env(clear=True)
def test_testing_variants_added_for_all():
    """Every base config gets a testing variant."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    testing_configs = [c for c in p.configurations if c["options"].get("testing") is True]
    assert len(testing_configs) > 0


@patch_env(clear=True)
def test_pybind_fans_out_per_python_version():
    """Each pybind variant is duplicated per configured python version."""
    p = XmsConanPackager("xmscore", python_versions=["3.10", "3.13"])
    p.generate_configurations(system_platform="linux")

    pybind_configs = [c for c in p.configurations if c["options"].get("pybind") is True]
    assert pybind_configs, "expected at least one pybind config"

    versions_seen = {c["options"].get("python_version") for c in pybind_configs}
    assert versions_seen == {"3.10", "3.13"}

    for cfg in pybind_configs:
        # python_version option must align with the buildenv var so the
        # generated cmake/profile reads back the same value.
        assert cfg["buildenv"]["PYTHON_TARGET_VERSION"] == cfg["options"]["python_version"]


@patch_env(clear=True)
def test_non_pybind_configs_have_no_python_version_option():
    """python_version is omitted from non-pybind builds (recipe drops it from package_id)."""
    p = XmsConanPackager("xmscore", python_versions=["3.10", "3.13"])
    p.generate_configurations(system_platform="linux")

    for cfg in p.configurations:
        if cfg["options"].get("pybind") is True:
            continue
        assert "python_version" not in cfg["options"]


def test_python_versions_default_when_env_unset(monkeypatch):
    """python_versions falls back to the default list when env is unset."""
    monkeypatch.delenv("PYTHON_TARGET_VERSION", raising=False)
    p = XmsConanPackager("xmscore")
    assert p.python_versions == XmsConanPackager.DEFAULT_PYTHON_VERSIONS


def test_python_versions_honors_env_when_arg_missing(monkeypatch):
    """A single-version env override collapses the fan-out to that version."""
    monkeypatch.setenv("PYTHON_TARGET_VERSION", "3.10")
    p = XmsConanPackager("xmscore")
    assert p.python_versions == ["3.10"]


def test_python_versions_arg_wins_over_env(monkeypatch):
    """Explicit python_versions kwarg overrides the env."""
    monkeypatch.setenv("PYTHON_TARGET_VERSION", "3.10")
    p = XmsConanPackager("xmscore", python_versions=["3.13"])
    assert p.python_versions == ["3.13"]


@patch_env(clear=True)
@pytest.mark.parametrize("bogus", ["3", "3.x", "py3.13", "", "3.10.0"])
def test_python_versions_rejects_malformed_entries(bogus):
    """Non-X.Y strings fail fast instead of silently propagating."""
    with pytest.raises(ValueError):
        XmsConanPackager("xmscore", python_versions=["3.13", bogus])


@patch_env(clear=True)
def test_python_versions_rejects_non_string_entries():
    """Numeric entries are caught even though they look version-ish."""
    with pytest.raises(ValueError):
        XmsConanPackager("xmscore", python_versions=[3.13])


@patch_env(clear=True)
def test_python_versions_rejects_non_list():
    """A bare string is a common mistake but is rejected explicitly."""
    with pytest.raises(ValueError):
        XmsConanPackager("xmscore", python_versions="3.13")


@patch_env(clear=True)
def test_python_versions_empty_list_falls_back_to_default():
    """An empty list is treated like None — fall back to env / default."""
    p = XmsConanPackager("xmscore", python_versions=[])
    assert p.python_versions == XmsConanPackager.DEFAULT_PYTHON_VERSIONS


@patch_env({"PYTHON_TARGET_VERSION": "py313"}, clear=True)
def test_python_versions_rejects_malformed_env():
    """An obviously-broken PYTHON_TARGET_VERSION is rejected too."""
    with pytest.raises(ValueError):
        XmsConanPackager("xmscore")


@patch_env(clear=True)
def test_default_python_version_uses_max_not_list_order():
    """Non-pybind buildenv seeds PYTHON_TARGET_VERSION with the highest version regardless of list order."""
    p = XmsConanPackager("xmscore", python_versions=["3.13", "3.10"])
    p.generate_configurations(system_platform="linux")
    non_pybind = [c for c in p.configurations if not c["options"].get("pybind")]
    assert non_pybind, "expected at least one non-pybind config"
    for cfg in non_pybind:
        assert cfg["buildenv"]["PYTHON_TARGET_VERSION"] == "3.13"


# --- filter_configurations ---


@patch_env(clear=True)
def test_filter_configurations_by_build_type():
    """Filters by top-level key."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    total = len(p.configurations)

    p.filter_configurations({"build_type": "Release"})
    assert all(c["build_type"] == "Release" for c in p.configurations)
    assert len(p.configurations) < total


@patch_env(clear=True)
def test_filter_configurations_by_option():
    """Filters by nested option value."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    p.filter_configurations({"options": {"testing": True}})
    assert all(c["options"]["testing"] is True for c in p.configurations)


def test_filter_configurations_noop_when_none():
    """No crash when configurations haven't been generated yet."""
    p = XmsConanPackager("xmscore")
    p.filter_configurations({"build_type": "Release"})  # Should not raise
    assert p.configurations is None


@patch_env(clear=True)
def test_filter_configurations_rejects_flat_option_key():
    """Flat pybind/testing at the top level raises instead of silently dropping.

    The old code silently ignored unknown top-level keys, which is how
    xmsconan_coverage's flat filter (issue #62) widened the build to every
    Debug configuration without anyone noticing.
    """
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    with pytest.raises(ValueError, match="pybind"):
        p.filter_configurations({"build_type": "Debug", "pybind": True})


# --- create_build_profile ---


@patch_env(clear=True)
def test_create_build_profile_writes_settings_and_options(tmp_path):
    """Profile file contains [settings] and [options] sections."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    profile_path = p.create_build_profile(p.configurations[0])
    with open(profile_path, "r") as f:
        content = f.read()

    assert "[settings]" in content
    assert "[options]" in content
    assert "[buildenv]" in content
    assert "os=Linux" in content


@patch_env(clear=True)
def test_create_build_profile_not_confused_by_new_combination_keys(tmp_path):
    """
    Ensure adding a new key to combinations doesn't trip up the profile generator.

    The profile generator mixes setting names and some of its own things in the same combination dict. It has a
    hard-coded list of keys it treats as "special" and assumes everything else is a setting name (since setting names
    are an open-ended set). When new keys get added, they might be assumed to be setting names.

    It would be better to have a separate namespace for settings so this isn't an issue, but I didn't want to untangle
    that right now so I added a Band-Aid test that will catch it.
    """
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    profile_path = p.create_build_profile(p.configurations[0])
    with open(profile_path, "r") as f:
        content = f.read()

    expected = (
        '[settings]\n'
        'os=Linux\n'
        'build_type=Release\n'
        'arch=x86_64\n'
        'compiler=gcc\n'
        'compiler.version=13\n'
        'compiler.cppstd=gnu17\n'
        'compiler.libcxx=libstdc++11\n'
        '\n'
        '[options]\n'
        '&:wchar_t=builtin\n'
        '&:pybind=False\n'
        '&:testing=False\n'
        'boost/*:without_stacktrace=True\n'
        'boost/*:without_locale=True\n'
        '\n'
        '[buildenv]\n'
        'XMS_VERSION=None\n'
        'PYTHON_TARGET_VERSION=3.13\n'
        'CI_COMMIT_TAG=False\n'
        'RELEASE_PYTHON=False\n'
        'AQUAPI_USERNAME=None\n'
        'AQUAPI_PASSWORD=None\n'
        'AQUAPI_URL=None\n'
    )

    assert content == expected


@patch_env(clear=True)
def test_create_build_profile_writes_profile_options(tmp_path):
    """Per-dependency option overrides result in `pkg/*:opt=value` lines being written to the profile."""
    profile_options = {
        "boost": {"wchar_t": "builtin"},
        "laslib": {"shared": True},
        "example": {"test_option": "test-value"},
    }

    p = XmsConanPackager("lidar", profile_options=profile_options)
    p.generate_configurations(system_platform="linux")

    pybind_configs = [c for c in p.configurations if c["options"].get("pybind") is True]
    assert pybind_configs, "expected at least one pybind=True configuration on Linux"
    config = pybind_configs[0]

    profile_path = p.create_build_profile(config)
    with open(profile_path, "r") as f:
        content = f.read()

    assert "os=Linux" in content
    assert "&:pybind=True" in content
    assert "boost/*:wchar_t=builtin" in content
    assert "laslib/*:shared=True" in content
    assert "example/*:test_option=test-value" in content


@patch_env(clear=True)
def test_create_build_profile_puts_wildcards_first(tmp_path):
    """Wildcard dependency options appear before non-wildcard ones."""
    profile_options = {
        "boost": {"wchar_t": "builtin"},
        "*": {"everything": True},
        "example": {"test_option": "test-value"},
    }

    p = XmsConanPackager("lidar", profile_options=profile_options)
    p.generate_configurations(system_platform="linux")

    config = p.configurations[0]
    profile_path = p.create_build_profile(config)
    with open(profile_path, "r") as f:
        content = f.read()

    boost_location = content.find('boost/*:wchar_t=builtin')
    wild_card_location = content.find('*:everything=True')
    example_location = content.find('example/*:test_option=test-value')
    assert boost_location >= 0 and wild_card_location >= 0 and example_location >= 0
    assert wild_card_location < boost_location and wild_card_location < example_location


@patch_env(clear=True)
def test_create_build_profile_with_no_profile_options(tmp_path):
    """Only the built-in boost defaults are emitted when no caller-supplied profile_options exist."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    config = p.configurations[0]

    profile_path = p.create_build_profile(config)
    with open(profile_path, "r") as f:
        content = f.read()
    # The packager unconditionally injects boost defaults (without_stacktrace,
    # without_locale).  Any other `pkg/*:` line would indicate a stray
    # caller-supplied option leaking through.
    dep_lines = [line for line in content.splitlines() if "/*:" in line]
    assert dep_lines == [
        "boost/*:without_stacktrace=True",
        "boost/*:without_locale=True",
    ]


# --- print_configuration_table ---


@patch_env(clear=True)
def test_print_configuration_table_all(capsys):
    """Default (None) prints all configurations."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.print_configuration_table()

    captured = capsys.readouterr()
    # Should have a row for each configuration
    assert "gcc" in captured.out


@patch_env(clear=True)
def test_print_configuration_table_subset(capsys):
    """Specific index list prints only those rows."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.print_configuration_table([0])

    captured = capsys.readouterr()
    assert "1" in captured.out  # row number 1 (0-indexed config, 1-indexed display)


@patch_env(clear=True)
def test_print_configuration_table_uses_library_name(capsys):
    """Table headers use the actual library name, not a hardcoded value."""
    p = XmsConanPackager("xmsgrid")
    p.generate_configurations(system_platform="linux")
    p.print_configuration_table()

    captured = capsys.readouterr()
    assert "xmsgrid:wchar_t" in captured.out
    assert "xmsgrid:pybind" in captured.out
    assert "xmsgrid:testing" in captured.out


# --- run ---


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_returns_zero_on_success(mock_run):
    """All configurations pass → returns 0."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    # Filter to just one config for speed
    p.filter_configurations({"build_type": "Release", "options": {"testing": False, "pybind": False}})

    result = p.run()
    assert result == 0
    assert mock_run.call_count >= 1


@patch_env(clear=True)
@patch(
    "xmsconan.package_tools.packager.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_run_returns_failure_count(mock_run):
    """Failed configurations → returns count of failures."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": False, "pybind": False}})

    result = p.run()
    assert result > 0


# --- upload ---


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_upload_calls_conan_upload(mock_run):
    """upload() calls conan upload with correct command structure."""
    p = XmsConanPackager("xmscore")
    p.upload("7.0.0")

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "conan"
    assert "upload" in cmd
    assert "xmscore/7.0.0*" in cmd


@patch_env(clear=True)
@patch(
    "xmsconan.package_tools.packager.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_upload_handles_called_process_error(mock_run):
    """Handles CalledProcessError without crashing."""
    p = XmsConanPackager("xmscore")
    # Should not raise — CalledProcessError is caught internally
    p.upload("7.0.0")


# --- artifacts_dir ---


def test_init_accepts_artifacts_dir(tmp_path):
    """artifacts_dir is stored as an absolute path."""
    p = XmsConanPackager("xmscore", artifacts_dir=str(tmp_path / "arts"))
    assert p._artifacts_dir == str(tmp_path / "arts")


def test_init_artifacts_dir_defaults_to_none():
    """artifacts_dir is None when not provided."""
    p = XmsConanPackager("xmscore")
    assert p._artifacts_dir is None


@patch_env(clear=True)
def test_generate_configurations_injects_artifacts_dir(tmp_path):
    """Every configuration gets XMS_TEST_ARTIFACTS_DIR when artifacts_dir set."""
    p = XmsConanPackager("xmscore", artifacts_dir=str(tmp_path))
    p.generate_configurations(system_platform="linux")

    for cfg in p.configurations:
        assert cfg["buildenv"]["XMS_TEST_ARTIFACTS_DIR"] == str(tmp_path)


@patch_env(clear=True)
def test_generate_configurations_no_artifacts_dir():
    """XMS_TEST_ARTIFACTS_DIR absent when artifacts_dir not set."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    for cfg in p.configurations:
        assert "XMS_TEST_ARTIFACTS_DIR" not in cfg["buildenv"]


# --- _config_label ---


@patch_env(clear=True)
def test_config_label_testing():
    """Testing config produces 'Release-testing' label."""
    p = XmsConanPackager("xmscore")
    combo = {"build_type": "Release", "options": {"testing": True, "pybind": False, "wchar_t": "builtin"}}
    assert p._config_label(combo) == "Release-testing"


@patch_env(clear=True)
def test_config_label_pybind():
    """Pybind config produces 'Release-pybind' label."""
    p = XmsConanPackager("xmscore")
    combo = {"build_type": "Release", "options": {"testing": False, "pybind": True, "wchar_t": "builtin"}}
    assert p._config_label(combo) == "Release-pybind"


@patch_env(clear=True)
def test_config_label_wchar_typedef():
    """wchar_t=typedef suffix appears in label."""
    p = XmsConanPackager("xmscore")
    combo = {"build_type": "Release", "options": {"testing": True, "pybind": False, "wchar_t": "typedef"}}
    assert p._config_label(combo) == "Release-testing-wchar_typedef"


@patch_env(clear=True)
def test_config_label_plain():
    """Plain config (no testing, no pybind) is just build_type."""
    p = XmsConanPackager("xmscore")
    combo = {"build_type": "Debug", "options": {"testing": False, "pybind": False, "wchar_t": "builtin"}}
    assert p._config_label(combo) == "Debug"


# --- run with artifacts ---


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_injects_label_into_profile(mock_run, tmp_path):
    """Profile written during run() contains XMS_TEST_ARTIFACTS_LABEL."""
    p = XmsConanPackager("xmscore", artifacts_dir=str(tmp_path))
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    p.run()

    # Read the profile that was written
    profile_path = p._temp_dir_path + "/temp_profile"
    with open(profile_path, "r") as f:
        content = f.read()
    assert "XMS_TEST_ARTIFACTS_LABEL=Release-testing" in content
    assert f"XMS_TEST_ARTIFACTS_DIR={tmp_path}" in content


# --- test sharding ---


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_skips_tests_when_sharding(mock_run, tmp_path):
    """Testing configs get XMS_SKIP_CXX_TESTS=1 when test_shards > 1."""
    p = XmsConanPackager("xmscore", artifacts_dir=str(tmp_path), test_shards=4)
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Debug", "options": {"testing": True}})

    p.run()

    # conan create should have been called with env containing XMS_SKIP_CXX_TESTS
    call_kwargs = mock_run.call_args_list[0]
    env = call_kwargs.kwargs.get('env') or call_kwargs[1].get('env')
    assert env is not None
    assert env.get('XMS_SKIP_CXX_TESTS') == '1'


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_no_skip_without_sharding(mock_run):
    """Testing configs run tests normally when test_shards is 0."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Debug", "options": {"testing": True}})

    p.run()

    call_kwargs = mock_run.call_args_list[0]
    env = call_kwargs.kwargs.get('env') or call_kwargs[1].get('env')
    assert env is None


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
def test_run_sharded_tests_invokes_runner(mock_run, tmp_path):
    """After build, runner is invoked N times with GTEST shard env vars."""
    artifacts_dir = tmp_path / "artifacts"
    label_dir = artifacts_dir / "Debug-testing"
    label_dir.mkdir(parents=True)

    # Create a fake runner binary
    runner_name = "runner.exe" if sys.platform == "win32" else "runner"
    runner_path = label_dir / runner_name
    runner_path.write_bytes(b"\x7fELF")
    runner_path.chmod(0o755)

    p = XmsConanPackager("xmscore", artifacts_dir=str(artifacts_dir), test_shards=2)
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Debug", "options": {"testing": True}})

    result = p.run()
    assert result == 0

    # First call is conan create, remaining calls are shard runs
    runner_calls = [call for call in mock_run.call_args_list if Path(call.args[0][0]) == runner_path]
    assert len(runner_calls) == 2

    # Verify shard env vars
    shard_envs = []
    for call in runner_calls:
        env = call.kwargs.get('env') or call[1].get('env', {})
        shard_envs.append((env.get('GTEST_TOTAL_SHARDS'), env.get('GTEST_SHARD_INDEX')))
    shard_envs.sort(key=lambda x: x[1])
    assert shard_envs == [('2', '0'), ('2', '1')]


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_no_shard_when_shards_is_one(mock_run):
    """test_shards=1 does NOT trigger sharding — tests run normally."""
    p = XmsConanPackager("xmscore", test_shards=1)
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Debug", "options": {"testing": True}})

    p.run()

    call_kwargs = mock_run.call_args_list[0]
    env = call_kwargs.kwargs.get('env') or call_kwargs[1].get('env')
    assert env is None


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
def test_run_sharded_tests_handles_subprocess_exception(mock_run, tmp_path):
    """Exception in a shard thread is caught and reported as a failure."""
    artifacts_dir = tmp_path / "artifacts"
    label_dir = artifacts_dir / "Debug-testing"
    label_dir.mkdir(parents=True)

    runner_name = "runner.exe" if sys.platform == "win32" else "runner"
    runner_path = label_dir / runner_name
    runner_path.write_bytes(b"\x7fELF")
    runner_path.chmod(0o755)

    # First call (conan create) succeeds; subsequent calls (shards) raise OSError
    mock_run.side_effect = [
        subprocess.CompletedProcess([], 0),
        OSError("runner crashed"),
        OSError("runner crashed"),
    ]

    p = XmsConanPackager("xmscore", artifacts_dir=str(artifacts_dir), test_shards=2)
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Debug", "options": {"testing": True}})

    result = p.run()
    assert result > 0  # failures reported, not an unhandled crash


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
def test_run_sharded_tests_handles_timeout(mock_run, tmp_path):
    """Verify TimeoutExpired in a shard is caught and reported as a failure."""
    artifacts_dir = tmp_path / "artifacts"
    label_dir = artifacts_dir / "Debug-testing"
    label_dir.mkdir(parents=True)

    runner_name = "runner.exe" if sys.platform == "win32" else "runner"
    runner_path = label_dir / runner_name
    runner_path.write_bytes(b"\x7fELF")
    runner_path.chmod(0o755)

    mock_run.side_effect = [
        subprocess.CompletedProcess([], 0),  # conan create
        subprocess.TimeoutExpired("runner", 600),  # shard 0 times out
        subprocess.CompletedProcess([], 0),  # shard 1 passes
    ]

    p = XmsConanPackager("xmscore", artifacts_dir=str(artifacts_dir), test_shards=2)
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Debug", "options": {"testing": True}})

    result = p.run()
    assert result > 0  # one shard failed
