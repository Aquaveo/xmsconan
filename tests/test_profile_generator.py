"""Tests for generator_tools.profile_generator."""
import json
import os

import pytest

from xmsconan.generator_tools.profile_generator import DEFAULT_OUTPUT_DIR, generate_profiles
from .utils import patch_env


def _write_build_toml(tmp_path, **keys):
    """Write a build.toml holding the keys profile generation reads."""
    lines = [f'library_name = "{keys.pop("library_name", "xmscore")}"', 'description = "Core"']
    for key, value in keys.items():
        lines.append(f"{key} = {json.dumps(value)}")
    toml_file = tmp_path / "build.toml"
    toml_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml_file


@patch_env(clear=True)
def test_dry_run_reports_exactly_what_a_real_run_writes(tmp_path):
    """The preview and the real run agree, file for file.

    Regression guard: the dry-run path once unpacked three names from the
    four-field plan entries and raised ValueError on the first profile, and the
    caller in xmsconan_gen degraded that to a warning with exit code 0.
    """
    toml_file = _write_build_toml(tmp_path)

    previewed = generate_profiles(toml_file_path=str(toml_file), dry_run=True,
                                  system_platform="linux")
    written = generate_profiles(toml_file_path=str(toml_file), system_platform="linux")

    assert previewed == sorted(written)


@patch_env(clear=True)
def test_dry_run_writes_nothing(tmp_path):
    """A preview leaves the working tree alone."""
    toml_file = _write_build_toml(tmp_path)

    previewed = generate_profiles(toml_file_path=str(toml_file), dry_run=True,
                                  system_platform="linux")

    assert previewed, "nothing planned -- the assertion below would pass vacuously"
    assert not [path for path in previewed if os.path.exists(path)]


@patch_env(clear=True)
def test_profiles_land_in_the_default_directory(tmp_path):
    """Profiles are written under conan_profiles/ next to build.toml."""
    toml_file = _write_build_toml(tmp_path)

    written = generate_profiles(toml_file_path=str(toml_file), system_platform="linux")

    profiles = sorted(p.name for p in (tmp_path / DEFAULT_OUTPUT_DIR).iterdir())
    assert profiles == ["linux_library_debug.txt", "linux_library_release.txt",
                        "linux_python_release_py313.txt", "linux_testing_debug.txt",
                        "linux_testing_release.txt"]
    assert str(tmp_path / "CMakePresets.json") in written


@patch_env(clear=True)
def test_relative_output_dir_resolves_against_the_toml_not_the_cwd(tmp_path, monkeypatch):
    """A relative output_dir follows build.toml, wherever the command was run from."""
    toml_file = _write_build_toml(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    generate_profiles(toml_file_path=str(toml_file), output_dir="profiles",
                      system_platform="linux")

    assert (tmp_path / "profiles").is_dir()
    assert not (elsewhere / "profiles").exists()


@patch_env(clear=True)
def test_absolute_output_dir_is_used_as_given(tmp_path):
    """An absolute output_dir is not re-anchored to build.toml."""
    toml_file = _write_build_toml(tmp_path)
    target = tmp_path / "absolute"

    written = generate_profiles(toml_file_path=str(toml_file), output_dir=str(target),
                                system_platform="linux")

    assert target.is_dir()
    assert [path for path in written if path.startswith(str(target))]


@patch_env(clear=True)
def test_presets_are_written_beside_build_toml(tmp_path):
    """CMakePresets.json is a project file, so it sits at the repo root."""
    toml_file = _write_build_toml(tmp_path)

    generate_profiles(toml_file_path=str(toml_file), system_platform="linux")

    document = json.loads((tmp_path / "CMakePresets.json").read_text())
    assert document["version"] == 6
    assert document["configurePresets"]


@patch_env(clear=True)
def test_no_presets_leaves_the_presets_file_alone(tmp_path):
    """write_presets=False generates profiles only."""
    toml_file = _write_build_toml(tmp_path)

    written = generate_profiles(toml_file_path=str(toml_file), write_presets=False,
                                system_platform="linux")

    assert written, "no profiles written -- the assertions below would pass vacuously"
    assert not (tmp_path / "CMakePresets.json").exists()


@patch_env(clear=True)
def test_dry_run_previews_the_presets_file(tmp_path):
    """The preview names CMakePresets.json too, since a real run writes it."""
    toml_file = _write_build_toml(tmp_path)

    previewed = generate_profiles(toml_file_path=str(toml_file), dry_run=True,
                                  system_platform="linux")

    assert str(tmp_path / "CMakePresets.json") in previewed


def test_missing_library_name_names_the_file(tmp_path):
    """A build.toml without library_name fails with a message naming the file."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('description = "Core"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="does not define library_name"):
        generate_profiles(toml_file_path=str(toml_file))


@patch_env(clear=True)
def test_variant_keys_reach_the_packager(tmp_path):
    """conan_profile_variants in build.toml produces the extra profiles."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core"\n'
        '[[conan_profile_variants]]\n'
        'name = "vs"\n'
        'platforms = ["windows"]\n'
        'kinds = ["testing"]\n'
        '[conan_profile_variants.conf]\n'
        '"tools.cmake.cmaketoolchain:generator" = "Visual Studio 17 2022"\n',
        encoding="utf-8",
    )

    written = generate_profiles(toml_file_path=str(toml_file), system_platform="windows")

    assert [path for path in written if path.endswith("_vs.txt")]


def test_malformed_variant_is_rejected_by_name(tmp_path):
    """A misspelled platform fails generation instead of silently emitting nothing."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core"\n'
        '[[conan_profile_variants]]\n'
        'name = "vs"\n'
        'platforms = ["macos"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown platforms macos"):
        generate_profiles(toml_file_path=str(toml_file), system_platform="windows")
