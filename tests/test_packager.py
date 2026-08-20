"""Tests for package_tools.packager."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.constants import (
    build_folder_for_generator,
    DEFAULT_REMOTE_NAME,
    GENERATOR_FOLDER_SUFFIXES,
    MSVC_VS2019_VERSION,
    VS2019_REMOTE_NAME,
)
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
@pytest.mark.parametrize("platform_key,expected_msvc_version", [
    ("windows", "194"),   # CI / VS2022
    ("windows_vs2019", "192"),  # manually built, published to aquaveo-vs2019
])
def test_generate_configurations_windows_matrix(platform_key, expected_msvc_version):
    """Both msvc matrices produce the same 14-config shape, differing only in compiler.version.

    Composition with two python versions:
      4 base    (Release/Debug x dynamic/static runtime)
    + 4 wchar_t=typedef variants (one per base; all msvc)
    + 2 pybind  (Release + dynamic runtime only, one per python version)
    + 4 testing variants (one per base)
    = 14

    The pybind count is the interesting one for VS2019: nothing in the fan-out
    keys off the msvc version, so msvc 192 produces exactly the same shape as
    msvc 194.
    """
    p = XmsConanPackager("xmscore", python_versions=["3.10", "3.13"])
    configs = p.generate_configurations(system_platform=platform_key)

    assert {c["os"] for c in configs} == {"Windows"}
    assert {c["compiler"] for c in configs} == {"msvc"}
    assert {c["compiler.version"] for c in configs} == {expected_msvc_version}

    def select(**opts):
        return [c for c in configs if all(c["options"].get(k) == v for k, v in opts.items())]

    base = select(wchar_t="builtin", pybind=False, testing=False)
    typedef = select(wchar_t="typedef")
    pybind = select(pybind=True)
    testing = select(testing=True)

    assert len(base) == 4
    assert {(c["build_type"], c["compiler.runtime"]) for c in base} == {
        ("Release", "dynamic"), ("Release", "static"),
        ("Debug", "dynamic"), ("Debug", "static"),
    }
    assert len(typedef) == 4
    assert len(testing) == 4
    assert len(pybind) == 2
    assert {c["build_type"] for c in pybind} == {"Release"}
    assert {c["compiler.runtime"] for c in pybind} == {"dynamic"}
    assert {c["options"]["python_version"] for c in pybind} == {"3.10", "3.13"}
    assert len(configs) == 14


@patch_env(clear=True)
def test_matrix_compiler_runtime_drops_the_static_crt_configurations():
    """A library that ships no static-CRT build can stop producing them.

    Nothing has ever consumed a static-CRT build of some libraries, and two of
    the configurations cannot even link -- xmdf's "static" payload carries
    __declspec(dllimport) CRT references, so a /MT link of the test runner has
    unresolvable symbols. Cutting the base configurations cuts their wchar_t
    and testing copies with them.
    """
    p = XmsConanPackager("xmscore", python_versions=["3.13"],
                         matrix={"compiler_runtime": ["dynamic"]})
    configs = p.generate_configurations(system_platform="windows")

    assert {c["compiler.runtime"] for c in configs} == {"dynamic"}
    # 2 base + 2 wchar_t + 2 testing + 1 pybind (Release only, by default).
    assert len(configs) == 7


@patch_env(clear=True)
def test_matrix_compiler_runtime_is_inert_where_the_setting_does_not_exist():
    """One build.toml serves every platform, so the key cannot fail on Linux.

    Linux and macOS declare no compiler.runtime at all; a library that restricts
    it for Windows must still build there.
    """
    p = XmsConanPackager("xmscore", matrix={"compiler_runtime": ["dynamic"]})
    configs = p.generate_configurations(system_platform="linux")

    assert configs
    assert not any("compiler.runtime" in c for c in configs)


@patch_env(clear=True)
def test_matrix_pybind_build_types_restores_debug_pybind():
    """Debug+pybind is requestable without turning coverage on.

    Consumers link a Debug .pyd (bin/_<name>_d.<abi>.pyd), but the only way to
    get one used to be XMS_COVERAGE=1 -- unreachable for a Windows-only library,
    since the generated CMakeLists makes XMS_COVERAGE a FATAL_ERROR on MSVC.
    Whether the Debug module is built and whether it is instrumented are
    unrelated concerns.
    """
    p = XmsConanPackager("xmscore", python_versions=["3.13"], coverage=False,
                         matrix={"pybind_build_types": ["Release", "Debug"]})
    configs = p.generate_configurations(system_platform="windows")

    pybind = [c for c in configs if c["options"]["pybind"]]
    assert {c["build_type"] for c in pybind} == {"Release", "Debug"}
    # Still dynamic-runtime only: that gate is compiler_runtime's job, not this one.
    assert {c["compiler.runtime"] for c in pybind} == {"dynamic"}


@patch_env(clear=True)
def test_pybind_build_types_defaults_to_release_only():
    """Omitting the key leaves the historical fan-out untouched."""
    p = XmsConanPackager("xmscore", python_versions=["3.13"], coverage=False)
    configs = p.generate_configurations(system_platform="windows")

    pybind = [c for c in configs if c["options"]["pybind"]]
    assert {c["build_type"] for c in pybind} == {"Release"}


@patch_env(clear=True)
def test_coverage_still_adds_debug_pybind_to_an_explicit_release_only_list():
    """Coverage needs a Debug pybind build, so it unions rather than obeys.

    A library that pins Release-only must not silently lose the instrumented
    build the coverage tooling looks for.
    """
    p = XmsConanPackager("xmscore", python_versions=["3.13"], coverage=True,
                         matrix={"pybind_build_types": ["Release"]})
    configs = p.generate_configurations(system_platform="windows")

    pybind = [c for c in configs if c["options"]["pybind"]]
    assert {c["build_type"] for c in pybind} == {"Release", "Debug"}


@pytest.mark.parametrize("matrix,expected", [
    ({"compiler_runtimes": ["dynamic"]}, "compiler_runtimes"),   # misspelled key
    ({"compiler_runtime": ["MD"]}, "MD"),                        # Conan 1 spelling
    ({"compiler_runtime": []}, "compiler_runtime"),              # would build nothing
    ({"compiler_runtime": "dynamic"}, "compiler_runtime"),       # string, not list
    ({"pybind_build_types": ["RelWithDebInfo"]}, "RelWithDebInfo"),
    ({"pybind_build_types": []}, "pybind_build_types"),
    (["dynamic"], "matrix"),                                     # list, not a table
])
def test_matrix_rejects_bad_input(matrix, expected):
    """A misspelled key or value fails loudly instead of silently building nothing.

    Every failure mode here is otherwise invisible: an ignored key produces the
    full 13-configuration fan-out the library was trying to trim, and an empty
    list produces no configurations at all -- which looks like a successful
    build that packaged nothing.
    """
    with pytest.raises(ValueError) as exc_info:
        XmsConanPackager("xmscore", matrix=matrix)
    assert expected in str(exc_info.value)


@patch_env(clear=True)
def test_generate_configurations_auto_detects_platform_and_arch():
    """system_platform=None looks the platform up from platform.system() and uses the detected arch.

    The explicit-platform path keeps the configured arch; only the
    auto-detected path substitutes the machine's real architecture.
    """
    with patch("xmsconan.package_tools.packager.platform.system", return_value="Linux"), \
            patch("xmsconan.package_tools.packager.platform.machine", return_value="aarch64"):
        p = XmsConanPackager("xmscore")
        configs = p.generate_configurations()

    assert configs
    assert {c["os"] for c in configs} == {"Linux"}
    assert {c["arch"] for c in configs} == {"armv8"}


@patch_env(clear=True)
def test_generate_configurations_rejects_unknown_platform():
    """An unknown platform key names itself and the valid keys instead of raising AttributeError.

    The platform name reaches this method from a CLI flag, so a bare
    ``AttributeError: 'NoneType' object has no attribute 'copy'`` (the old
    behavior of ``configurations.get(...).copy()``) is not actionable.
    """
    p = XmsConanPackager("xmscore")
    with pytest.raises(ValueError) as exc_info:
        p.generate_configurations(system_platform="windows_vs2017")

    message = str(exc_info.value)
    assert "windows_vs2017" in message
    for valid in ("windows", "windows_vs2019", "linux", "darwin"):
        assert valid in message


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
    # apply_boost_defaults defaults to True, so the boost defaults
    # (without_stacktrace, without_locale) are injected here.  Any other
    # `pkg/*:` line would indicate a stray caller-supplied option leaking
    # through.
    dep_lines = [line for line in content.splitlines() if "/*:" in line]
    assert dep_lines == [
        "boost/*:without_stacktrace=True",
        "boost/*:without_locale=True",
    ]


@patch_env(clear=True)
def test_create_build_profile_skips_boost_defaults_when_disabled(tmp_path):
    """apply_boost_defaults=False keeps the boost 1.86-only options out of the profile.

    The VS2019 matrix builds against the legacy Aquaveo boost/1.74.0.3, which
    may not declare without_stacktrace / without_locale; Conan fails the build
    when a profile sets an option the recipe does not define.
    """
    p = XmsConanPackager("xmscore", apply_boost_defaults=False)
    p.generate_configurations(system_platform="windows_vs2019")

    profile_path = p.create_build_profile(p.configurations[0])
    with open(profile_path, "r") as f:
        content = f.read()

    assert "boost/*:" not in content


@patch_env(clear=True)
def test_boost_defaults_disabled_still_honors_explicit_profile_options(tmp_path):
    """Caller-supplied boost options survive apply_boost_defaults=False."""
    p = XmsConanPackager(
        "xmscore",
        profile_options={"boost": {"shared": False}},
        apply_boost_defaults=False,
    )
    p.generate_configurations(system_platform="windows_vs2019")

    profile_path = p.create_build_profile(p.configurations[0])
    with open(profile_path, "r") as f:
        content = f.read()

    dep_lines = [line for line in content.splitlines() if "/*:" in line]
    assert dep_lines == ["boost/*:shared=False"]


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


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_without_log_dir_inherits_stdout(mock_run):
    """Default run() lets conan create inherit stdout (no redirection)."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": False, "pybind": False}})

    p.run()

    assert "stdout" not in mock_run.call_args.kwargs


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_log_dir_redirects_output_per_configuration(mock_run, tmp_path, capsys):
    """log_dir writes <library>-<label>.log per config and prints a pointer."""
    log_dir = tmp_path / "logs"  # created by run(), not by the test
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    p.run(log_dir=str(log_dir))

    expected = log_dir / "xmscore-Release-testing.log"
    assert expected.is_file()
    assert mock_run.call_args.kwargs["stdout"].name == str(expected)
    assert mock_run.call_args.kwargs["stderr"] == subprocess.STDOUT
    assert str(expected) in capsys.readouterr().out


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_log_dir_writes_one_log_per_msvc_configuration(mock_run, tmp_path):
    """The whole msvc matrix lands in distinct log files — nothing is truncated.

    compiler.runtime fans the msvc matrix out over dynamic/static but was once
    missing from the config label, so 14 configurations shared 8 log paths and
    each static build silently destroyed the dynamic build's log.
    """
    log_dir = tmp_path / "logs"
    p = XmsConanPackager("xmscore", python_versions=["3.10", "3.13"])
    p.generate_configurations(system_platform="windows_vs2019")

    p.run(log_dir=str(log_dir))

    assert len(p.configurations) == 14  # the documented VS2019 matrix
    assert len(list(log_dir.glob("*.log"))) == len(p.configurations)


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_empty_log_dir_does_not_scatter_logs_into_cwd(mock_run, tmp_path, monkeypatch):
    """An empty --log-dir behaves like None instead of writing bare filenames."""
    monkeypatch.chdir(tmp_path)
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    p.run(log_dir="")

    assert "stdout" not in mock_run.call_args.kwargs
    assert list(tmp_path.iterdir()) == []


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_log_dir_archives_previous_runs_log(mock_run, tmp_path):
    """Re-running keeps the earlier run's log instead of truncating it."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    previous = log_dir / "xmscore-Release-testing.log"
    previous.write_text("first run: the compiler errors the developer needs")

    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    p.run(log_dir=str(log_dir))

    archived = list(log_dir.glob("xmscore-Release-testing.*.log"))
    assert len(archived) == 1
    assert archived[0].read_text() == "first run: the compiler errors the developer needs"
    assert previous.is_file() and previous.read_text() == ""  # current run's log


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_log_dir_archive_disambiguates_same_second(mock_run, tmp_path):
    """A second archive with the same mtime second gets a counter suffix.

    Two runs of a configuration that fails immediately share an mtime second,
    so the timestamp alone is not a unique archive name.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    previous = log_dir / "xmscore-Release-testing.log"
    previous.write_text("current")
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(os.path.getmtime(previous)))
    already_archived = log_dir / f"xmscore-Release-testing.{stamp}.log"
    already_archived.write_text("older")

    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    p.run(log_dir=str(log_dir))

    assert already_archived.read_text() == "older"
    assert (log_dir / f"xmscore-Release-testing.{stamp}.1.log").read_text() == "current"
    assert previous.read_text() == ""  # current run's log


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
@patch("xmsconan.package_tools.packager.os.makedirs", side_effect=OSError("read-only file system"))
def test_run_survives_unusable_log_dir(mock_makedirs, mock_run, tmp_path, capsys):
    """A log directory that can't be created warns and builds anyway.

    An OSError escaping here would skip run()'s summary table and take down a
    multi-hour matrix over a logging problem.
    """
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    assert p.run(log_dir=str(tmp_path / "logs")) == 0

    assert "stdout" not in mock_run.call_args.kwargs
    assert "could not create log directory" in capsys.readouterr().out


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_survives_unwritable_log_file(mock_run, tmp_path, capsys):
    """A log file that can't be opened warns and builds on the console instead.

    Distinct from the unusable-log-directory case above: there ``os.makedirs``
    fails and ``run()`` drops logging for the whole matrix; here the directory
    is fine and only the per-configuration log file cannot be opened, which
    ``_conan_create`` has to absorb by itself. An OSError escaping either
    handler would take down a multi-hour matrix over a logging problem.

    ``open`` is patched (rather than the file being made unopenable on disk)
    because no cross-platform on-disk state produces this failure: an
    unwritable path is renamed out of the way by the archive step first, and
    Windows ignores the read-only bit on directories.
    """
    real_open = open

    def open_failing_for_logs(file, *args, **kwargs):
        if str(file).endswith(".log"):
            raise OSError("no space left on device")
        return real_open(file, *args, **kwargs)

    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    with patch("builtins.open", side_effect=open_failing_for_logs):
        assert p.run(log_dir=str(tmp_path / "logs")) == 0

    assert "stdout" not in mock_run.call_args.kwargs
    out = capsys.readouterr().out
    assert "could not write" in out and "falling back to console output" in out


@patch_env(clear=True)
@pytest.mark.parametrize("build_missing,expected", [(True, True), (False, False)])
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_run_passes_build_missing_to_conan(mock_run, build_missing, expected):
    """build_missing=True puts --build=missing on the conan create argv."""
    p = XmsConanPackager("xmscore", build_missing=build_missing)
    p.generate_configurations(system_platform="linux")
    p.filter_configurations({"build_type": "Release", "options": {"testing": True}})

    p.run()

    assert ("--build=missing" in mock_run.call_args[0][0]) is expected


# --- upload ---


@patch_env(clear=True)
@pytest.mark.parametrize("kwargs,expected_remote", [
    ({}, DEFAULT_REMOTE_NAME),  # default keeps every existing caller on the CI remote
    ({"remote": VS2019_REMOTE_NAME}, VS2019_REMOTE_NAME),  # manual VS2019 builds
])
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_upload_calls_conan_upload(mock_run, kwargs, expected_remote):
    """upload() calls conan upload with the correct command/remote and returns 0."""
    p = XmsConanPackager("xmscore")
    assert p.upload("7.0.0", **kwargs) == 0

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "conan"
    assert "upload" in cmd
    assert "xmscore/7.0.0*" in cmd
    assert cmd[cmd.index("-r") + 1] == expected_remote
    # No query given: conan matches by reference only, the historical behavior.
    assert "-p" not in cmd


@patch_env(clear=True)
@patch("xmsconan.package_tools.packager.subprocess.run")
def test_upload_restricts_binaries_with_package_query(mock_run, capsys):
    """package_query becomes `-p <query>` so only the intended binaries publish.

    Without it `conan upload` selects by reference alone: on a workstation that
    is both the VS2019 build box and a normal dev machine, every msvc 194
    binary of that version would land on the VS2019 remote and exit 0.
    """
    p = XmsConanPackager("xmscore")
    query = f"compiler.version={MSVC_VS2019_VERSION}"

    assert p.upload("7.0.0", remote=VS2019_REMOTE_NAME, package_query=query) == 0

    cmd = mock_run.call_args[0][0]
    assert cmd[cmd.index("-p") + 1] == query
    assert query in capsys.readouterr().out


@patch_env(clear=True)
@patch(
    "xmsconan.package_tools.packager.subprocess.run",
    side_effect=subprocess.CalledProcessError(3, "conan"),
)
def test_upload_returns_nonzero_on_called_process_error(mock_run, capsys):
    """A failed conan upload is reported and returns 1 instead of faking success."""
    p = XmsConanPackager("xmscore")
    # Should not raise — CalledProcessError is caught and turned into an exit code.
    assert p.upload("7.0.0", remote="aquaveo-vs2019") == 1

    out = capsys.readouterr().out
    assert "ERROR uploading xmscore/7.0.0* to aquaveo-vs2019" in out
    assert "exited 3" in out
    assert "All packages uploaded successfully" not in out


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


@patch_env(clear=True)
def test_config_label_includes_compiler_runtime():
    """Verify msvc's dynamic/static runtime is part of the label."""
    p = XmsConanPackager("xmscore")
    combo = {
        "build_type": "Release",
        "compiler.runtime": "static",
        "options": {"testing": True, "pybind": False, "wchar_t": "typedef"},
    }
    assert p._config_label(combo) == "Release-static-testing-wchar_typedef"


@patch_env(clear=True)
def test_config_labels_unique_across_msvc_matrix():
    """Every configuration of the msvc matrix gets its own label.

    The label names both the per-configuration log file and the test-artifact
    directory, so a duplicate label means one configuration's evidence
    overwrites another's.
    """
    p = XmsConanPackager("xmscore", python_versions=["3.10", "3.13"])
    p.generate_configurations(system_platform="windows_vs2019")

    labels = [p._config_label(c) for c in p.configurations]
    assert len(labels) == len(set(labels)) == 14
    assert {"Release-dynamic-testing", "Release-static-testing"} <= set(labels)


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


# --- collect_dependency_libs ---


# 1980-01-01 UTC; ZIP timestamps cannot go earlier.
ZIP_EPOCH = 315532800


@patch("xmsconan.package_tools.packager.subprocess.run")
def test_collect_dependency_libs_rewrites_pre_1980_mtimes(mock_run, tmp_path):
    """Verify staged libraries get a ZIP-representable mtime.

    Conan zeroes mtimes in package tarballs, so libraries restored from a
    remote are dated 1970. Copying that mtime through to the staging dir
    makes the wheel repair tools fail with "ZIP does not support
    timestamps before 1980".
    """
    conan_home = tmp_path / "conan_home"
    pkg_dir = conan_home / "p" / "somepkg" / "p" / "bin"
    pkg_dir.mkdir(parents=True)
    for name in ("msvcp140.dll", "libfoo.dylib", "libbar.so"):
        lib = pkg_dir / name
        lib.write_bytes(b"MZ")
        os.utime(lib, (0, 0))

    mock_run.return_value = subprocess.CompletedProcess(
        [], 0, stdout=f"{conan_home}\n"
    )

    output_dir = tmp_path / "libs"
    XmsConanPackager("xmsvtk").collect_dependency_libs(str(output_dir))

    staged = sorted(p.name for p in output_dir.iterdir())
    assert staged == ["libbar.so", "libfoo.dylib", "msvcp140.dll"]
    for lib in output_dir.iterdir():
        assert lib.stat().st_mtime >= ZIP_EPOCH, f"{lib.name} is not ZIP-representable"


@patch("xmsconan.package_tools.packager.subprocess.run")
def test_collect_dependency_libs_skips_non_libraries(mock_run, tmp_path):
    """Verify only shared libraries are staged."""
    conan_home = tmp_path / "conan_home"
    pkg_dir = conan_home / "p" / "somepkg" / "p" / "bin"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "libbaz.so.1.2").write_bytes(b"\x7fELF")
    (pkg_dir / "readme.txt").write_text("not a library")
    (pkg_dir / "tool.exe").write_bytes(b"MZ")

    mock_run.return_value = subprocess.CompletedProcess(
        [], 0, stdout=f"{conan_home}\n"
    )

    output_dir = tmp_path / "libs"
    XmsConanPackager("xmsvtk").collect_dependency_libs(str(output_dir))

    assert sorted(p.name for p in output_dir.iterdir()) == ["libbaz.so.1.2"]


# --- profile generation (write_profiles / profile_name) ---


def _packager_for(system_platform, **kwargs):
    """Packager with the deterministic matrix for ``system_platform`` generated.

    The two inputs that would otherwise be read from the developer's shell are
    pinned: python_versions falls back to PYTHON_TARGET_VERSION and coverage to
    XMS_COVERAGE, and either one exported locally changes the matrix these tests
    assert literal names and counts against. Pinned rather than patched over the
    environment, because callers here set other variables deliberately -- and
    the env fallbacks have their own tests.
    """
    kwargs.setdefault("python_versions", ["3.13"])
    kwargs.setdefault("coverage", False)
    packager = XmsConanPackager("xmscore", **kwargs)
    packager.generate_configurations(system_platform)
    return packager


def _linux_packager(**kwargs):
    """Packager with the deterministic linux matrix already generated."""
    return _packager_for("linux", **kwargs)


@pytest.mark.parametrize("configuration,expected", [
    pytest.param({"os": "Macos", "build_type": "Release",
                  "options": {"pybind": False, "testing": False}},
                 "mac_os_library_release", id="mac-library"),
    pytest.param({"os": "Linux", "build_type": "Debug",
                  "options": {"pybind": False, "testing": True}},
                 "linux_testing_debug", id="linux-testing-debug"),
    pytest.param({"os": "Macos", "build_type": "Release",
                  "options": {"pybind": True, "testing": False, "python_version": "3.13"}},
                 "mac_os_python_release_py313", id="mac-python-py313"),
    pytest.param({"os": "Windows", "build_type": "Debug", "compiler.runtime": "static",
                  "options": {"pybind": False, "testing": False, "wchar_t": "typedef"}},
                 "windows_library_debug_typedef_static", id="windows-wchar-runtime"),
])
def test_profile_name(configuration, expected):
    """Configuration maps to the xmsvtk-style profile file stem."""
    assert XmsConanPackager.profile_name(configuration) == expected


def test_write_profiles_writes_one_file_per_configuration(tmp_path):
    """Every configuration produces its own profile, and none overwrite each other."""
    packager = _linux_packager()

    written = packager.write_profiles(str(tmp_path))

    # The linux matrix: library and testing at Debug and Release, plus one
    # pybind configuration at the default python version.
    assert len(written) == 5
    assert len(set(written)) == len(written)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(os.path.basename(w) for w in written)


def test_write_profiles_returns_sorted_paths(tmp_path):
    """Returned paths are sorted so callers get a stable order."""
    written = _linux_packager().write_profiles(str(tmp_path))

    assert written == sorted(written)


@patch_env({"AQUAPI_PASSWORD": "s3cret-value", "AQUAPI_USERNAME": "ci-bot"})
def test_write_profiles_omits_credentials(tmp_path):
    """Credentials present in the environment never reach a profile on disk."""
    written = _linux_packager().write_profiles(str(tmp_path))

    contents = "\n".join(p.read_text() for p in tmp_path.iterdir())
    assert written, "no profiles written -- the assertions below would pass vacuously"
    assert "s3cret-value" not in contents
    assert "ci-bot" not in contents
    assert "AQUAPI_PASSWORD" not in contents


def test_write_profiles_keeps_public_buildenv(tmp_path):
    """Non-credential build variables are still written."""
    _linux_packager().write_profiles(str(tmp_path))

    contents = (tmp_path / "linux_library_release.txt").read_text()
    assert "PYTHON_TARGET_VERSION=" in contents


def test_write_profiles_drops_unset_values(tmp_path):
    """An unset variable is omitted rather than serialized as the string 'None'."""
    written = _linux_packager().write_profiles(str(tmp_path))

    contents = "\n".join(p.read_text() for p in tmp_path.iterdir())
    assert written, "no profiles written -- the assertion below would pass vacuously"
    assert "=None" not in contents


def test_write_profiles_emits_default_conf(tmp_path):
    """Generated profiles pin the CMake generator so it does not vary by machine."""
    _linux_packager().write_profiles(str(tmp_path))

    contents = (tmp_path / "linux_library_release.txt").read_text()
    assert "[conf]" in contents
    assert "tools.cmake.cmaketoolchain:generator=Ninja Multi-Config" in contents


def test_write_profiles_conf_is_overridable(tmp_path):
    """A repo can replace the default [conf] entries."""
    packager = _linux_packager(profile_conf={"tools.cmake.cmaketoolchain:generator": "Xcode"})

    packager.write_profiles(str(tmp_path))

    contents = (tmp_path / "linux_library_release.txt").read_text()
    assert "tools.cmake.cmaketoolchain:generator=Xcode" in contents


def test_write_profiles_omits_conf_when_empty(tmp_path):
    """An empty profile_conf omits the section entirely."""
    packager = _linux_packager(profile_conf={})

    packager.write_profiles(str(tmp_path))

    assert "[conf]" not in (tmp_path / "linux_library_release.txt").read_text()


def test_write_profiles_includes_dependency_options(tmp_path):
    """Per-dependency options reach the profile so package ids match the build."""
    packager = _linux_packager(profile_options={"boost": {"without_locale": True}})

    packager.write_profiles(str(tmp_path))

    assert "boost/*:without_locale=True" in (tmp_path / "linux_library_release.txt").read_text()


@patch_env({"AQUAPI_PASSWORD": "s3cret-value"})
def test_create_build_profile_still_carries_full_environment():
    """The ephemeral build profile is unchanged: it keeps credentials the build needs."""
    packager = _linux_packager()

    path = packager.create_build_profile(packager.configurations[0])

    with open(path) as profile:
        assert "AQUAPI_PASSWORD=s3cret-value" in profile.read()


# --- generator variants ---


VS_VARIANT = {
    "name": "vs",
    "platforms": ["windows"],
    "kinds": ["testing", "python"],
    "conf": {"tools.cmake.cmaketoolchain:generator": "Visual Studio 17 2022"},
}


@pytest.mark.parametrize("configuration,expected", [
    ({"os": "Windows", "options": {"testing": True}}, True),
    ({"os": "Windows", "options": {"pybind": True}}, True),
    ({"os": "Windows", "options": {}}, False),          # library kind is out of scope
    ({"os": "Linux", "options": {"testing": True}}, False),   # wrong platform
    ({"os": "Macos", "options": {"testing": True}}, False),
])
def test_variant_applies(configuration, expected):
    """A variant only matches the platforms and kinds it declares."""
    assert XmsConanPackager.variant_applies(VS_VARIANT, configuration) is expected


def test_variant_without_filters_applies_everywhere():
    """Omitting platforms/kinds means the variant is unrestricted."""
    variant = {"name": "alt", "conf": {}}

    assert XmsConanPackager.variant_applies(variant, {"os": "Linux", "options": {}})
    assert XmsConanPackager.variant_applies(variant, {"os": "Windows", "options": {"testing": True}})


@pytest.mark.parametrize("configuration,expected", [
    ({"options": {"testing": True}}, "testing"),
    ({"options": {"pybind": True}}, "python"),
    ({"options": {"testing": True, "pybind": True}}, "testing"),
    ({"options": {}}, "library"),
])
def test_configuration_kind(configuration, expected):
    """Kind is derived from the testing/pybind options, testing taking precedence."""
    assert XmsConanPackager.configuration_kind(configuration) == expected


def test_write_profiles_emits_scoped_variant_profiles(tmp_path):
    """A windows-scoped variant adds profiles only for the kinds it names."""
    packager = XmsConanPackager("xmscore", profile_variants=[VS_VARIANT])
    packager.generate_configurations("windows")

    packager.write_profiles(str(tmp_path))

    variant_names = sorted(n for n in (p.stem for p in tmp_path.iterdir()) if n.endswith("_vs"))
    kinds = sorted({n.split("_")[1] for n in variant_names})
    # library configurations are outside the variant's declared kinds
    assert kinds == ["python", "testing"], variant_names


def test_variant_profile_overlays_conf(tmp_path):
    """The variant's conf replaces the default generator in its own profile only."""
    packager = XmsConanPackager("xmscore", profile_variants=[VS_VARIANT])
    packager.generate_configurations("windows")

    packager.write_profiles(str(tmp_path))

    variant = next((p for p in tmp_path.iterdir() if p.stem.endswith("_vs")), None)
    base = next((p for p in tmp_path.iterdir()
                 if p.stem.startswith("windows_testing") and not p.stem.endswith("_vs")), None)
    assert variant is not None, sorted(p.stem for p in tmp_path.iterdir())
    assert base is not None, sorted(p.stem for p in tmp_path.iterdir())
    assert "generator=Visual Studio 17 2022" in variant.read_text()
    assert "generator=Ninja Multi-Config" in base.read_text()


def test_variant_does_not_apply_to_other_platforms(tmp_path):
    """A windows-scoped variant leaves the linux matrix untouched."""
    packager = XmsConanPackager("xmscore", profile_variants=[VS_VARIANT])
    packager.generate_configurations("linux")

    written = packager.write_profiles(str(tmp_path))

    assert written, "no profiles written -- the assertion below would pass vacuously"
    assert not any(p.stem.endswith("_vs") for p in tmp_path.iterdir())


def test_plan_profiles_matches_write_profiles(tmp_path):
    """The plan is exactly what gets written.

    The dry-run consumer of this plan is covered by
    tests/test_profile_generator.py; this pins the plan itself, which is what
    both consumers read.
    """
    packager = XmsConanPackager("xmscore", profile_variants=[VS_VARIANT])
    packager.generate_configurations("windows")

    planned = [entry.filename for entry in packager.plan_profiles()]
    written = packager.write_profiles(str(tmp_path))

    assert sorted(planned) == sorted(os.path.basename(w) for w in written)


# --- CMakePresets.json generation ---


def _recipe_class():
    """Import XmsConan2File lazily.

    Deliberately not a module-level import: xms_conan2_file imports the real
    conan package, and tests/test_xms_conan2_file.py installs MagicMock conan
    stubs with sys.modules.setdefault(), which silently no-ops if conan is
    already imported. A module-level import here collects first (alphabetically)
    and would disable those stubs for the whole run.
    """
    from xmsconan.xms_conan2_file import XmsConan2File
    return XmsConan2File


def _recipe_stub(generator, options=None, settings=None):
    """Minimal stand-in for the ConanFile surface _build_folder_name() touches.

    Binds the real method so the parity test exercises the shipped
    implementation rather than a copy of its logic.
    """
    recipe_class = _recipe_class()
    option_values = options or {}
    setting_values = settings or {}

    class _Conf:
        def get(self, name, default=None):
            if name == "tools.cmake.cmaketoolchain:generator":
                return generator
            return default

    class _Mapping:
        def __init__(self, values):
            self._values = values

        def get_safe(self, name):
            return self._values.get(name)

    class _Stub:
        _GENERATOR_FOLDER_SUFFIXES = recipe_class._GENERATOR_FOLDER_SUFFIXES
        _build_folder_name = recipe_class._build_folder_name

        def __init__(self):
            self.conf = _Conf()
            self.options = _Mapping(option_values)
            self.settings = _Mapping(setting_values)

    return _Stub()


def test_generator_folder_suffixes_match_recipe():
    """The recipe's standalone copy of the suffix table matches constants."""
    assert _recipe_class()._GENERATOR_FOLDER_SUFFIXES == GENERATOR_FOLDER_SUFFIXES


@pytest.mark.parametrize("generator,options,settings,kind,discriminators,expected_folder", [
    pytest.param("Ninja Multi-Config", {}, {}, "library", [],
                 "build_library_ninja", id="library"),
    pytest.param("Ninja Multi-Config", {"testing": True}, {}, "testing", [],
                 "build_testing_ninja", id="testing"),
    pytest.param("Ninja Multi-Config", {"pybind": True, "python_version": "3.13"}, {},
                 "python", ["py313"], "build_python_ninja_py313", id="python"),
    pytest.param("Visual Studio 17 2022", {"testing": True}, {}, "testing", [],
                 "build_testing_vs", id="testing-vs"),
    pytest.param("Unix Makefiles", {}, {}, "library", [],
                 "build_library_make", id="library-make"),
    # The axes the Windows matrix fans out over, and the reason this test exists
    # in its current form: these four used to collapse onto build_library_ninja.
    pytest.param("Ninja Multi-Config", {}, {"compiler.runtime": "static"},
                 "library", ["static"], "build_library_ninja_static", id="static"),
    pytest.param("Ninja Multi-Config", {}, {"compiler.runtime": "dynamic"},
                 "library", ["dynamic"], "build_library_ninja_dynamic", id="dynamic"),
    pytest.param("Ninja Multi-Config", {"wchar_t": "typedef"}, {"compiler.runtime": "static"},
                 "library", ["typedef", "static"], "build_library_ninja_typedef_static",
                 id="wchar-and-runtime"),
    pytest.param("Ninja Multi-Config", {"wchar_t": "builtin"}, {},
                 "library", [], "build_library_ninja", id="builtin-wchar-omitted"),
    pytest.param(None, {}, {}, None, [], "build", id="no-generator"),
])
def test_recipe_build_folder_matches_constants(generator, options, settings, kind,
                                               discriminators, expected_folder):
    """Recipe and generated presets agree on the build folder.

    They must: the presets' binaryDir points at the folder the recipe creates,
    and the recipe cannot import from xmsconan (it is copied standalone), so the
    logic is duplicated and this pins the two together. Both the kind and the
    discriminators are written out rather than computed, so a change to the
    naming rule shows up here as a literal diff instead of being reproduced by
    the expectation.
    """
    recipe_folder = _recipe_stub(generator, options, settings)._build_folder_name()

    assert recipe_folder == expected_folder
    assert build_folder_for_generator(generator, kind, discriminators) == expected_folder


def test_build_folder_rejects_a_bare_string_of_discriminators():
    """A bare str is a sequence, so it would splat one folder segment per char.

    `build_library_ninja_s_t_a_t_i_c` is a plausible-looking wrong answer, and
    this function exists because plausible-looking wrong folder names are what
    the collision it fixes produced.
    """
    with pytest.raises(TypeError, match="sequence of strings"):
        build_folder_for_generator("Ninja Multi-Config", "library", "static")


@pytest.mark.parametrize("discriminator,expected_folder", [
    pytest.param("3.13", "build_library_ninja_3-13", id="dot"),
    pytest.param("msvc 194", "build_library_ninja_msvc-194", id="space"),
    pytest.param("../escape", "build_library_ninja_escape", id="separator"),
])
def test_build_folder_slugs_discriminators(discriminator, expected_folder):
    """Discriminators are slugged like the generator suffix, not interpolated raw.

    Nothing reaching this today carries a separator -- the values are matrix
    enums plus a regex-checked python version -- so this pins the guarantee for
    whatever axis is added next rather than a bug that exists now.
    """
    folder = build_folder_for_generator("Ninja Multi-Config", "library", [discriminator])

    assert folder == expected_folder


@pytest.mark.parametrize("system_platform", ["linux", "windows", "darwin"])
def test_every_configure_preset_gets_its_own_binary_dir(system_platform):
    """No two configure presets configure into the same directory.

    The regression this pins: the folder name was derived from the build kind
    alone, but the Windows matrix also fans out over compiler.runtime and
    wchar_t, so four library configurations and two testing configurations
    landed on build_library_ninja and build_testing_ninja respectively. Nothing
    complained -- they share a generator, so CMake reconfigures in place -- and
    each configure silently overwrote the previous one's toolchain.
    """
    document = _packager_for(system_platform).plan_cmake_presets()

    binary_dirs = [p["binaryDir"] for p in document["configurePresets"]]
    duplicates = sorted({folder for folder in binary_dirs if binary_dirs.count(folder) > 1})
    assert binary_dirs, "no presets planned"
    assert duplicates == []


def test_windows_matrix_fans_out_past_one_preset_per_kind():
    """Windows really does produce several presets per kind.

    Guards the test above from passing vacuously: if the matrix ever collapsed
    to one configuration per kind, uniqueness would hold for the wrong reason
    and the collision could return unnoticed.
    """
    document = _packager_for("windows").plan_cmake_presets()

    library = [p for p in document["configurePresets"] if p["name"].startswith("library")]
    assert len(library) > 1


def test_plan_cmake_presets_one_configure_preset_per_kind():
    """Each build kind gets its own configure preset and its own binaryDir."""
    document = _linux_packager().plan_cmake_presets()

    names = sorted(p["name"] for p in document["configurePresets"])
    binary_dirs = [p["binaryDir"] for p in document["configurePresets"]]
    assert names == ["library", "python-py313", "testing"]
    assert len(set(binary_dirs)) == len(binary_dirs)


def test_multi_config_generator_collapses_build_types():
    """Debug and Release share a configure preset and differ as build presets."""
    document = _linux_packager().plan_cmake_presets()

    library = next(p for p in document["configurePresets"] if p["name"] == "library")
    library_builds = sorted(b["name"] for b in document["buildPresets"]
                            if b["configurePreset"] == "library")
    assert library["cacheVariables"]["CMAKE_CONFIGURATION_TYPES"] == "Debug;Release"
    assert library_builds == ["library-debug", "library-release"]


def test_preset_toolchain_lives_in_its_binary_dir():
    """Every preset's toolchain path sits under that preset's own binaryDir."""
    document = _linux_packager().plan_cmake_presets()

    for preset in document["configurePresets"]:
        assert preset["toolchainFile"].startswith(preset["binaryDir"] + "/")


def test_write_cmake_presets_skipped_without_generator(tmp_path):
    """With no generator pinned there is nothing to express, so no file."""
    packager = _linux_packager(profile_conf={})

    assert packager.write_cmake_presets(str(tmp_path / "CMakePresets.json")) is None
    assert not (tmp_path / "CMakePresets.json").exists()


def test_write_cmake_presets_is_valid_json(tmp_path):
    """The written document parses and carries the presets schema version."""
    path = tmp_path / "CMakePresets.json"

    _linux_packager().write_cmake_presets(str(path))

    document = json.loads(path.read_text())
    assert document["version"] == 6
    assert document["configurePresets"]


# --- single-config generators ---

#: A single-config generator, which Conan's cmake_layout lays out differently
#: from the Ninja Multi-Config default every other test here exercises.
NINJA_SINGLE_CONF = {"tools.cmake.cmaketoolchain:generator": "Ninja"}


def test_single_config_presets_get_their_own_binary_dir():
    """Debug and Release are separate presets with separate folders.

    Conan's cmake_layout appends the build type for a single-config generator,
    so two presets sharing one binaryDir would reconfigure over each other.
    """
    document = _linux_packager(profile_conf=NINJA_SINGLE_CONF).plan_cmake_presets()

    library = sorted(p["binaryDir"] for p in document["configurePresets"]
                     if p["name"].startswith("library"))

    assert library == ["build_library_ninja/Debug", "build_library_ninja/Release"]


def test_single_config_preset_toolchain_matches_cmake_layout():
    """The toolchain path names the file cmake_layout actually writes."""
    document = _linux_packager(profile_conf=NINJA_SINGLE_CONF).plan_cmake_presets()

    library = next((p for p in document["configurePresets"] if p["name"] == "library-debug"), None)

    assert library is not None, sorted(p["name"] for p in document["configurePresets"])
    assert library["toolchainFile"] == "build_library_ninja/Debug/generators/conan_toolchain.cmake"


def test_single_config_presets_pin_the_build_type():
    """A single-config preset carries CMAKE_BUILD_TYPE; multi-config does not."""
    single = _linux_packager(profile_conf=NINJA_SINGLE_CONF).plan_cmake_presets()
    multi = _linux_packager().plan_cmake_presets()

    single_debug = next(p for p in single["configurePresets"] if p["name"] == "library-debug")
    multi_library = next(p for p in multi["configurePresets"] if p["name"] == "library")

    assert single_debug["cacheVariables"]["CMAKE_BUILD_TYPE"] == "Debug"
    assert "CMAKE_BUILD_TYPE" not in multi_library["cacheVariables"]


def test_install_prefix_is_anchored_to_the_preset_file():
    """CMAKE_INSTALL_PREFIX does not depend on the invoking directory."""
    document = _linux_packager().plan_cmake_presets()

    prefixes = {p["cacheVariables"]["CMAKE_INSTALL_PREFIX"] for p in document["configurePresets"]}

    assert prefixes == {"${sourceDir}/_install"}


def test_presets_carry_no_bookkeeping_keys():
    """Only real preset fields are serialized."""
    document = _linux_packager().plan_cmake_presets()

    assert not [key for preset in document["configurePresets"]
                for key in preset if key.startswith("_")]


# --- profile_variants validation ---


@pytest.mark.parametrize("variant,expected_message", [
    pytest.param({"conf": {}}, 'must have a non-empty "name"', id="missing-name"),
    pytest.param({"name": ""}, 'must have a non-empty "name"', id="blank-name"),
    pytest.param({"name": "vs", "platform": ["windows"]}, "unknown key", id="unknown-key"),
    pytest.param({"name": "vs", "platforms": ["macos"]}, "unknown platforms", id="unknown-platform"),
    pytest.param({"name": "vs", "kinds": ["tests"]}, "unknown kinds", id="unknown-kind"),
    pytest.param({"name": "vs", "conf": "generator=Ninja"}, 'non-table "conf"', id="conf-not-table"),
    pytest.param("vs", "must be tables", id="not-a-table"),
])
def test_profile_variants_are_validated(variant, expected_message):
    """A malformed variant is rejected where it is declared, not where it is used.

    Both failure modes are otherwise invisible: a missing name raises a bare
    KeyError deep in plan_profiles, and a misspelled filter raises nothing at
    all -- the variant is simply never emitted.
    """
    with pytest.raises(ValueError, match=expected_message):
        XmsConanPackager("xmscore", profile_variants=[variant])


def test_profile_variants_error_lists_the_accepted_platforms():
    """The message names the accepted spellings, which appear in no other doc."""
    with pytest.raises(ValueError, match="linux, mac_os, windows"):
        XmsConanPackager("xmscore", profile_variants=[{"name": "vs", "platforms": ["macos"]}])


def test_valid_profile_variant_is_accepted():
    """The documented shape passes validation unchanged."""
    packager = XmsConanPackager("xmscore", profile_variants=[VS_VARIANT])

    assert packager._profile_variants == [VS_VARIANT]


# --- colliding profile stems ---


def test_colliding_stems_are_disambiguated(tmp_path):
    """Two configurations with the same stem both survive, rather than one overwriting the other.

    profile_name() only names the settings it knows to be discriminating, so a
    matrix that varies something else collides. The suffix is a safety net: it
    keeps both profiles on disk, and its presence means profile_name is missing
    a discriminator.
    """
    packager = XmsConanPackager("xmscore")
    packager.generate_configurations("linux")
    # Same platform, kind and build type, differing only in a setting that
    # profile_name does not encode.
    duplicate = dict(packager.configurations[0])
    duplicate["compiler.version"] = "13"
    packager._configurations = [packager.configurations[0], duplicate]

    written = [os.path.basename(path) for path in packager.write_profiles(str(tmp_path))]

    assert sorted(written) == ["linux_library_release.txt", "linux_library_release_2.txt"]


# --- extract_wheel: which pybind package's wheel gets staged ---


def _conan_list_json(*packages):
    """Build a ``conan list --format=json`` payload for one recipe revision.

    Args:
        packages: ``(package_id, build_type, python_version)`` triples, all in
            the same revision -- which is the case this exists to exercise,
            since every binary in a revision shares its timestamp.
    """
    return json.dumps({
        "Local Cache": {
            "xmscore/1.0": {
                "revisions": {
                    "rev1": {
                        "timestamp": 1700000000.0,
                        "packages": {
                            pid: {
                                "info": {
                                    "options": {"pybind": "True", "python_version": py},
                                    "settings": {"build_type": build_type},
                                }
                            }
                            for pid, build_type, py in packages
                        },
                    }
                }
            }
        }
    })


def _fake_conan(list_json, cache_root):
    """Return a subprocess.run stub answering ``conan list`` and ``conan cache path``."""
    def run(cmd, *_args, **_kwargs):
        result = MagicMock()
        result.returncode = 0
        if cmd[1] == "list":
            result.stdout = list_json
        else:  # conan cache path <ref>:<pid>
            result.stdout = os.path.join(cache_root, cmd[3].split(":")[1])
        return result
    return run


def _seed_package(cache_root, pid, wheel_name):
    """Create a fake package folder holding one wheel."""
    dist = os.path.join(cache_root, pid, "dist")
    os.makedirs(dist, exist_ok=True)
    with open(os.path.join(dist, wheel_name), "w", encoding="utf-8") as handle:
        handle.write("wheel")


@patch_env(clear=True)
def test_extract_wheel_prefers_the_release_build(tmp_path):
    """With a Release and a Debug pybind package, the Release wheel is staged.

    Both carry a wheel, both share the recipe revision's timestamp, and the two
    wheel filenames are identical -- so before build_type entered the decision
    the winner was whichever package-id hash sorted higher, and nothing
    downstream could tell which one had been published.
    """
    cache_root = str(tmp_path / "cache")
    # 'aaa' sorts below 'zzz', so the Debug id would win a hash-order tie.
    _seed_package(cache_root, "zzz", "xmscore-1.0-cp313-cp313-win_amd64.whl")
    _seed_package(cache_root, "aaa", "xmscore-1.0-cp313-cp313-win_amd64.whl")
    list_json = _conan_list_json(("aaa", "Release", "3.13"), ("zzz", "Debug", "3.13"))

    packager = XmsConanPackager("xmscore", python_versions=["3.13"])
    out_dir = str(tmp_path / "wheelhouse")
    with patch("xmsconan.package_tools.packager.subprocess.run",
               side_effect=_fake_conan(list_json, cache_root)) as run:
        assert packager.extract_wheel(out_dir, version="1.0") is True

    # The Release package's path is the one that was resolved and copied.
    resolved = [call.args[0][3] for call in run.call_args_list if call.args[0][1] == "cache"]
    assert resolved == ["xmscore/1.0:aaa"]


@patch_env(clear=True)
def test_extract_wheel_warns_when_only_a_debug_build_exists(tmp_path, capsys):
    """A Debug-only wheel is still staged, but not silently.

    Preferring Release must not turn into dropping the only wheel there is --
    that would be a green build with an empty wheelhouse.
    """
    cache_root = str(tmp_path / "cache")
    _seed_package(cache_root, "ddd", "xmscore-1.0-cp313-cp313-win_amd64.whl")
    list_json = _conan_list_json(("ddd", "Debug", "3.13"))

    packager = XmsConanPackager("xmscore", python_versions=["3.13"])
    with patch("xmsconan.package_tools.packager.subprocess.run",
               side_effect=_fake_conan(list_json, cache_root)):
        assert packager.extract_wheel(str(tmp_path / "wheelhouse"), version="1.0") is True

    out = capsys.readouterr().out
    assert "Debug build" in out


@patch_env(clear=True)
def test_extract_wheel_keeps_one_wheel_per_python_version(tmp_path):
    """Two python versions each get their own Release wheel."""
    cache_root = str(tmp_path / "cache")
    _seed_package(cache_root, "p310", "xmscore-1.0-cp310-cp310-win_amd64.whl")
    _seed_package(cache_root, "p313", "xmscore-1.0-cp313-cp313-win_amd64.whl")
    list_json = _conan_list_json(("p310", "Release", "3.10"), ("p313", "Release", "3.13"))

    packager = XmsConanPackager("xmscore", python_versions=["3.10", "3.13"])
    out_dir = str(tmp_path / "wheelhouse")
    with patch("xmsconan.package_tools.packager.subprocess.run",
               side_effect=_fake_conan(list_json, cache_root)):
        assert packager.extract_wheel(out_dir, version="1.0") is True

    assert sorted(os.listdir(out_dir)) == [
        "xmscore-1.0-cp310-cp310-win_amd64.whl",
        "xmscore-1.0-cp313-cp313-win_amd64.whl",
    ]


# --- matrix restrictions that would build no module ---


@patch_env(clear=True)
def test_matrix_static_only_runtime_is_rejected():
    """A static-only restriction leaves no pybind configuration, so it is refused.

    _pybind_variants only fans out from dynamic-runtime msvc configurations, so
    this produced 6 configurations with no module and no wheel. Downstream that
    is an opaque "No .whl files found" from the repair step, or -- with
    windows_wheel_repair off -- a green pipeline that publishes nothing.
    """
    packager = XmsConanPackager("xmscore", matrix={"compiler_runtime": ["static"]})
    with pytest.raises(ValueError, match="no dynamic-runtime configuration"):
        packager.generate_configurations(system_platform="windows")


@patch_env(clear=True)
def test_matrix_static_only_runtime_is_inert_on_linux():
    """The rejection is msvc-specific: linux declares no compiler.runtime at all."""
    packager = XmsConanPackager("xmscore", matrix={"compiler_runtime": ["static"]})
    configs = packager.generate_configurations(system_platform="linux")
    assert [c for c in configs if c["options"]["pybind"]]
