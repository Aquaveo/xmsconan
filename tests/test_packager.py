"""Tests for package_tools.packager."""
import subprocess
from unittest.mock import patch

from xmsconan.package_tools.packager import get_current_arch, XmsConanPackager


# --- get_current_arch ---


@patch("xmsconan.package_tools.packager.platform.machine", return_value="x86_64")
def test_get_current_arch_x86_64(mock_machine):
    """x86_64 maps to x86_64."""
    assert get_current_arch() == "x86_64"


@patch("xmsconan.package_tools.packager.platform.machine", return_value="AMD64")
def test_get_current_arch_amd64(mock_machine):
    """amd64 (case-insensitive) maps to x86_64."""
    assert get_current_arch() == "x86_64"


@patch("xmsconan.package_tools.packager.platform.machine", return_value="arm64")
def test_get_current_arch_arm64(mock_machine):
    """arm64 maps to armv8."""
    assert get_current_arch() == "armv8"


@patch("xmsconan.package_tools.packager.platform.machine", return_value="aarch64")
def test_get_current_arch_aarch64(mock_machine):
    """aarch64 maps to armv8."""
    assert get_current_arch() == "armv8"


@patch("xmsconan.package_tools.packager.platform.machine", return_value="riscv64")
def test_get_current_arch_unknown_passthrough(mock_machine):
    """Unknown architecture passed through as-is (lowered)."""
    assert get_current_arch() == "riscv64"


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


@patch.dict("os.environ", {}, clear=True)
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


@patch.dict("os.environ", {}, clear=True)
def test_generate_configurations_windows():
    """Windows config uses msvc compiler."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="windows")
    base = configs[0]
    assert base["os"] == "Windows"
    assert base["compiler"] == "msvc"


@patch.dict("os.environ", {}, clear=True)
def test_generate_configurations_darwin():
    """Verify macOS config sets MACOSX_DEPLOYMENT_TARGET."""
    p = XmsConanPackager("xmscore")
    configs = p.generate_configurations(system_platform="darwin")
    base = configs[0]
    assert base["os"] == "Macos"
    assert base["compiler"] == "apple-clang"
    assert base["buildenv"].get("MACOSX_DEPLOYMENT_TARGET") == "15.0"


@patch.dict("os.environ", {}, clear=True)
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


@patch.dict("os.environ", {}, clear=True)
def test_wchar_t_variants_only_for_msvc():
    """Only msvc compiler gets wchar_t=typedef variants."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    typedef_configs = [c for c in p.configurations if c["options"].get("wchar_t") == "typedef"]
    assert len(typedef_configs) == 0


@patch.dict("os.environ", {}, clear=True)
def test_pybind_variants_exclude_debug():
    """No pybind variant for Debug build_type."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    pybind_configs = [c for c in p.configurations if c["options"].get("pybind") is True]
    for cfg in pybind_configs:
        assert cfg["build_type"] != "Debug"


@patch.dict("os.environ", {}, clear=True)
def test_testing_variants_added_for_all():
    """Every base config gets a testing variant."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")

    testing_configs = [c for c in p.configurations if c["options"].get("testing") is True]
    assert len(testing_configs) > 0


# --- filter_configurations ---


@patch.dict("os.environ", {}, clear=True)
def test_filter_configurations_by_build_type():
    """Filters by top-level key."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    total = len(p.configurations)

    p.filter_configurations({"build_type": "Release"})
    assert all(c["build_type"] == "Release" for c in p.configurations)
    assert len(p.configurations) < total


@patch.dict("os.environ", {}, clear=True)
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


# --- create_build_profile ---


@patch.dict("os.environ", {}, clear=True)
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


# --- print_configuration_table ---


@patch.dict("os.environ", {}, clear=True)
def test_print_configuration_table_all(capsys):
    """Default (None) prints all configurations."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.print_configuration_table()

    captured = capsys.readouterr()
    # Should have a row for each configuration
    assert "gcc" in captured.out


@patch.dict("os.environ", {}, clear=True)
def test_print_configuration_table_subset(capsys):
    """Specific index list prints only those rows."""
    p = XmsConanPackager("xmscore")
    p.generate_configurations(system_platform="linux")
    p.print_configuration_table([0])

    captured = capsys.readouterr()
    assert "1" in captured.out  # row number 1 (0-indexed config, 1-indexed display)


@patch.dict("os.environ", {}, clear=True)
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


@patch.dict("os.environ", {}, clear=True)
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


@patch.dict("os.environ", {}, clear=True)
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


@patch.dict("os.environ", {}, clear=True)
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


@patch.dict("os.environ", {}, clear=True)
@patch(
    "xmsconan.package_tools.packager.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_upload_handles_called_process_error(mock_run):
    """Handles CalledProcessError without crashing."""
    p = XmsConanPackager("xmscore")
    # Should not raise — CalledProcessError is caught internally
    p.upload("7.0.0")
