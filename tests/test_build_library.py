"""Tests for build_tools.build_library."""
from argparse import Namespace
import os
from unittest.mock import patch

import pytest

from xmsconan.build_tools import build_library


# --- Converted from existing unittest tests ---


def test_parse_profile_options_with_includes(tmp_path):
    """Parse profile options from includes and ignore scoped dependency options."""
    base_profile = tmp_path / "base_profile"
    release_profile = tmp_path / "release_profile"

    base_profile.write_text(
        "[options]\n"
        "&:pybind=True\n"
        "testing=True\n"
        "wchar_t=builtin\n"
        "xmscore/*:*testing=False\n",
        encoding="utf-8",
    )

    release_profile.write_text(
        f"include({base_profile.name})\n"
        "[options]\n"
        "&:pybind=False\n",
        encoding="utf-8",
    )

    parsed = build_library._parse_profile_options(str(release_profile))

    assert parsed.get("testing") == "True"
    assert parsed.get("pybind") == "False"
    assert parsed.get("wchar_t") == "builtin"
    assert "xmscore/*:*testing" not in parsed


def test_parse_profile_options_with_cyclic_includes(tmp_path):
    """Handle cyclical include references without recursion failure."""
    profile_a = tmp_path / "profile_a"
    profile_b = tmp_path / "profile_b"

    profile_a.write_text(
        f"include({profile_b.name})\n"
        "[options]\n"
        "testing=True\n",
        encoding="utf-8",
    )

    profile_b.write_text(
        f"include({profile_a.name})\n"
        "[options]\n"
        "&:pybind=True\n",
        encoding="utf-8",
    )

    parsed = build_library._parse_profile_options(str(profile_a))
    assert parsed.get("testing") == "True"
    assert parsed.get("pybind") == "True"


def test_main_dry_run_skips_subprocess():
    """Dry-run should never execute subprocess commands."""
    args = Namespace(
        profile="VS2022_TESTING",
        cmake_dir=".",
        build_dir="builds/dry_run",
        generator="vs2022",
        python_version=None,
        xms_version="7.0.0",
        test_files=None,
        allow_missing_test_files=True,
        dry_run=True,
        verbose=0,
        quiet=True,
    )

    with patch.object(build_library, "get_args", return_value=args), \
         patch.object(build_library, "conan_install", return_value=["conan", "install"]), \
         patch.object(build_library, "run_cmake", return_value=["cmake"]), \
         patch("xmsconan.build_tools.build_library.subprocess.run") as mock_run:
        build_library.main()
        mock_run.assert_not_called()


def test_main_non_dry_run_executes_subprocess():
    """Normal mode should execute Conan and CMake subprocess commands."""
    args = Namespace(
        profile="VS2022_TESTING",
        cmake_dir=".",
        build_dir="builds/run",
        generator="vs2022",
        python_version=None,
        xms_version="7.0.0",
        test_files=None,
        allow_missing_test_files=True,
        dry_run=False,
        verbose=0,
        quiet=True,
    )

    with patch.object(build_library, "get_args", return_value=args), \
         patch.object(build_library, "conan_install", return_value=["conan", "install"]), \
         patch.object(build_library, "run_cmake", return_value=["cmake", "-S", ".", "-B", "build"]), \
         patch("xmsconan.build_tools.build_library.subprocess.run") as mock_run:
        build_library.main()

        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0] == ["conan", "install"]
        assert mock_run.call_args_list[1][0][0] == ["cmake", "-S", ".", "-B", "build"]


@pytest.mark.parametrize("wchar_t,expected", [
    ("builtin", "-DUSE_TYPEDEF_WCHAR_T=False"),
    ("typedef", "-DUSE_TYPEDEF_WCHAR_T=True"),
])
def test_get_cmake_options_forwards_wchar_t(wchar_t, expected, tmp_path):
    """The profile's wchar_t reaches CMake as the variable CMake actually reads.

    It used to go through _parse_bool_option, which collapsed BOTH legal values
    to False, and be emitted as -DXMS_BUILD, which no CMakeLists.txt in the
    tree reads. USE_TYPEDEF_WCHAR_T is what gates /Zc:wchar_t-.
    """
    profile = tmp_path / "profile"
    profile.write_text(
        f"[options]\ntesting=True\npybind=False\nwchar_t={wchar_t}\n",
        encoding="utf-8",
    )

    args = Namespace(
        profile=str(profile),
        cmake_dir=".",
        build_dir="builds/test",
        generator="vs2022",
        python_version=None,
        xms_version="7.0.0",
        test_files="NONE",
        allow_missing_test_files=True,
        dry_run=True,
        verbose=0,
        quiet=True,
    )

    options = build_library.get_cmake_options(args)
    assert expected in options
    assert not any(opt.startswith("-DXMS_BUILD=") for opt in options)


# --- New tests ---


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_parse_bool_option_true_values(value):
    """Standard true-ish strings map to True."""
    assert build_library._parse_bool_option(value) is True


def test_parse_bool_option_none_returns_false():
    """None maps to False."""
    assert build_library._parse_bool_option(None) is False


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "builtin", "typedef", ""])
def test_parse_bool_option_false_values(value):
    """Anything not explicitly true-ish is False.

    "builtin" used to be a true-ish alias here, for the sole benefit of the
    wchar_t option -- which is a two-valued string, not a boolean, and no
    longer goes through this function at all.
    """
    assert build_library._parse_bool_option(value) is False


def test_require_tool_returns_the_resolved_path():
    """A tool resolve_tool finds is handed back as found."""
    with patch("xmsconan.build_tools.build_library.resolve_tool", return_value="/usr/bin/cmake"):
        assert build_library._require_tool("cmake") == "/usr/bin/cmake"


def test_require_tool_not_found_raises():
    """A tool the build cannot run without is a RuntimeError naming it, not a None argv[0]."""
    with patch("xmsconan.build_tools.build_library.resolve_tool", return_value=None):
        with pytest.raises(RuntimeError, match="'nonexistent_tool' not found"):
            build_library._require_tool("nonexistent_tool")


def test_is_dir_valid(tmp_path):
    """Returns abspath for a real directory."""
    result = build_library.is_dir(str(tmp_path))
    assert result == os.path.abspath(str(tmp_path))


def test_is_dir_invalid_raises(tmp_path):
    """Raises TypeError for non-directory path."""
    with pytest.raises(TypeError, match="is not a directory"):
        build_library.is_dir(str(tmp_path / "no_such_dir"))


def test_is_file_valid(tmp_path):
    """Returns abspath for a real file."""
    f = tmp_path / "real_file.txt"
    f.write_text("hello", encoding="utf-8")
    result = build_library.is_file(str(f))
    assert result == os.path.abspath(str(f))


def test_is_file_invalid_raises(tmp_path):
    """Raises TypeError for non-file path."""
    with pytest.raises(TypeError, match="is not a file"):
        build_library.is_file(str(tmp_path / "no_such_file"))


def test_conan_install_returns_command(profile_file, tmp_path):
    """conan_install returns a command list with expected structure."""
    with patch.object(build_library, "_require_tool", return_value="/usr/bin/conan"):
        cmd = build_library.conan_install(
            str(profile_file), str(tmp_path), str(tmp_path / "build"), dry_run=True
        )
    assert cmd[0] == "/usr/bin/conan"
    assert "install" in cmd
    assert "-pr" in cmd


def test_conan_install_creates_build_dir(profile_file, tmp_path):
    """Non-dry-run creates the build directory."""
    build_dir = tmp_path / "new_build_dir"
    with patch.object(build_library, "_require_tool", return_value="/usr/bin/conan"):
        build_library.conan_install(
            str(profile_file), str(tmp_path), str(build_dir), dry_run=False
        )
    assert build_dir.is_dir()


def test_run_cmake_with_generator():
    """run_cmake includes -G flag for non-make generators."""
    with patch.object(build_library, "_require_tool", return_value="/usr/bin/cmake"):
        cmd = build_library.run_cmake(".", "build", "ninja", ["-DFOO=bar"])
    assert "-G" in cmd
    assert "Ninja" in cmd


def test_run_cmake_without_generator():
    """run_cmake omits -G for make generator."""
    with patch.object(build_library, "_require_tool", return_value="/usr/bin/cmake"):
        cmd = build_library.run_cmake(".", "build", "make", ["-DFOO=bar"])
    assert "-G" not in cmd


def test_get_cmake_options_debug_build_type(profile_file):
    """Profile ending in _d sets CMAKE_BUILD_TYPE to Debug."""
    args = Namespace(
        profile=str(profile_file) + "_d",
        cmake_dir=".",
        build_dir="builds/test",
        generator="ninja",
        python_version=None,
        xms_version="1.0.0",
        test_files="NONE",
        allow_missing_test_files=True,
        dry_run=True,
        verbose=0,
        quiet=True,
    )

    # Write a profile at the _d path so parsing works
    from pathlib import Path
    Path(str(profile_file) + "_d").write_text(
        "[options]\ntesting=False\npybind=False\nwchar_t=builtin\n",
        encoding="utf-8",
    )

    options = build_library.get_cmake_options(args)
    assert "-DCMAKE_BUILD_TYPE=Debug" in options


def test_get_cmake_options_pybind_sets_python_version(tmp_path):
    """PYTHON_TARGET_VERSION set when pybind is enabled."""
    profile = tmp_path / "pybind_profile"
    profile.write_text(
        "[options]\ntesting=False\npybind=True\nwchar_t=builtin\n",
        encoding="utf-8",
    )

    args = Namespace(
        profile=str(profile),
        cmake_dir=".",
        build_dir="builds/test",
        generator="ninja",
        python_version="3.12",
        xms_version="1.0.0",
        test_files=None,
        allow_missing_test_files=True,
        dry_run=True,
        verbose=0,
        quiet=True,
    )

    options = build_library.get_cmake_options(args)
    assert "-DPYTHON_TARGET_VERSION=3.12" in options


def test_get_cmake_options_testing_with_test_files(tmp_path):
    """XMS_TEST_PATH set when testing enabled and test_files dir exists."""
    profile = tmp_path / "test_profile"
    profile.write_text(
        "[options]\ntesting=True\npybind=False\nwchar_t=builtin\n",
        encoding="utf-8",
    )

    test_files_dir = tmp_path / "test_files"
    test_files_dir.mkdir()

    args = Namespace(
        profile=str(profile),
        cmake_dir=".",
        build_dir="builds/test",
        generator="ninja",
        python_version=None,
        xms_version="1.0.0",
        test_files=str(test_files_dir),
        allow_missing_test_files=False,
        dry_run=True,
        verbose=0,
        quiet=True,
    )

    options = build_library.get_cmake_options(args)
    matching = [o for o in options if o.startswith("-DXMS_TEST_PATH=")]
    assert len(matching) == 1


def test_get_cmake_options_missing_test_files_raises(tmp_path):
    """Raises RuntimeError when test_files dir missing and not allowed."""
    profile = tmp_path / "test_profile"
    profile.write_text(
        "[options]\ntesting=True\npybind=False\nwchar_t=builtin\n",
        encoding="utf-8",
    )

    args = Namespace(
        profile=str(profile),
        cmake_dir=".",
        build_dir="builds/test",
        generator="ninja",
        python_version=None,
        xms_version="1.0.0",
        test_files=str(tmp_path / "nonexistent_test_files"),
        allow_missing_test_files=False,
        dry_run=True,
        verbose=0,
        quiet=True,
    )

    with pytest.raises(RuntimeError, match="does not exist"):
        build_library.get_cmake_options(args)
