"""Tests for build_tools.build_library."""

from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xmsconan.build_tools import build_library


class BuildLibraryTests(unittest.TestCase):
    """Tests for profile parsing and main execution flow."""

    def test_parse_profile_options_with_includes(self):
        """Parse profile options from includes and ignore scoped dependency options."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_profile = root / "base_profile"
            release_profile = root / "release_profile"

            base_profile.write_text(
                """
[options]
&:pybind=True
testing=True
wchar_t=builtin
xmscore/*:*testing=False
""".strip(),
                encoding="utf-8",
            )

            release_profile.write_text(
                f"""
include({base_profile.name})
[options]
&:pybind=False
""".strip(),
                encoding="utf-8",
            )

            parsed = build_library._parse_profile_options(str(release_profile))

            self.assertEqual(parsed.get("testing"), "True")
            self.assertEqual(parsed.get("pybind"), "False")
            self.assertEqual(parsed.get("wchar_t"), "builtin")
            self.assertNotIn("xmscore/*:*testing", parsed)

    def test_parse_profile_options_with_cyclic_includes(self):
        """Handle cyclical include references without recursion failure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profile_a = root / "profile_a"
            profile_b = root / "profile_b"

            profile_a.write_text(
                f"""
include({profile_b.name})
[options]
testing=True
""".strip(),
                encoding="utf-8",
            )

            profile_b.write_text(
                f"""
include({profile_a.name})
[options]
&:pybind=True
""".strip(),
                encoding="utf-8",
            )

            parsed = build_library._parse_profile_options(str(profile_a))

            self.assertEqual(parsed.get("testing"), "True")
            self.assertEqual(parsed.get("pybind"), "True")

    def test_main_dry_run_skips_subprocess(self):
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

    def test_main_non_dry_run_executes_subprocess(self):
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

            self.assertEqual(mock_run.call_count, 2)
            first_call = mock_run.call_args_list[0][0][0]
            second_call = mock_run.call_args_list[1][0][0]
            self.assertEqual(first_call, ["conan", "install"])
            self.assertEqual(second_call, ["cmake", "-S", ".", "-B", "build"])


if __name__ == "__main__":
    unittest.main()
