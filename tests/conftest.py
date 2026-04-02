"""Shared pytest fixtures for xmsconan tests."""
import pytest


@pytest.fixture
def build_toml(tmp_path):
    """Write a minimal build.toml with library_name and description, return path."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core library"\npython_namespaced_dir = "core"\n',
        encoding="utf-8",
    )
    return toml_file


@pytest.fixture
def template_dir(tmp_path):
    """Create a directory with a simple .jinja template, return path."""
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "sample.txt.jinja").write_text(
        "version={{ version }}\n", encoding="utf-8"
    )
    return tpl_dir


@pytest.fixture
def ci_toml(tmp_path):
    """Write a build.toml with ci_type field, return path."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core library"\nci_type = "github"\n',
        encoding="utf-8",
    )
    return toml_file


@pytest.fixture
def profile_file(tmp_path):
    """Write a single Conan profile with [options], return path."""
    profile = tmp_path / "test_profile"
    profile.write_text(
        "[options]\ntesting=True\npybind=False\nwchar_t=builtin\n",
        encoding="utf-8",
    )
    return profile
