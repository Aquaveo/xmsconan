"""Tests for the build.toml reader."""
from dataclasses import fields, FrozenInstanceError

import pytest

from xmsconan.build_toml import BuildToml, CiTable, CoverageTable, KNOWN_KEYS, load_toml, XmsDependency


def test_load_toml_returns_the_parsed_table(tmp_path):
    """load_toml is a plain parser: no validation, no defaults."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\nanything = 1\n[ci]\nxvfb = true\n', encoding="utf-8")
    assert load_toml(toml_file) == {"library_name": "xmscore", "anything": 1, "ci": {"xvfb": True}}


def test_build_toml_fields_match_known_keys():
    """The dataclass and the key allowlist describe the same file."""
    assert frozenset(f.name for f in fields(BuildToml)) == KNOWN_KEYS


def test_build_toml_derives_python_namespaced_dir():
    """The default strips the xms prefix, the same rule gen and ci applied."""
    assert BuildToml(library_name="xmscore").python_namespaced_dir == "core"
    assert BuildToml(library_name="xmscore", python_namespaced_dir="c").python_namespaced_dir == "c"


def test_build_toml_defaults_match_the_generator():
    """The defaults gen used to apply with setdefault live on the dataclass now."""
    config = BuildToml(library_name="xmscore")
    assert config.testing_framework == "cxxtest"
    assert config.python_binding_type == "pybind11"
    assert config.extra_cmake_text == ""
    assert config.xms_dependencies == []
    assert config.xms_dependency_options == {}
    assert config.matrix == {}
    assert config.filter == {}
    assert config.conan_profile_conf is None
    assert config.conan_profile_variants is None
    assert config.ci_type is None
    assert config.description is None
    assert config.ci == CiTable()
    assert config.coverage == CoverageTable()


def test_build_toml_is_frozen():
    """Readers cannot mutate the shared configuration."""
    with pytest.raises(FrozenInstanceError):
        BuildToml(library_name="xmscore").library_name = "other"


def test_ci_table_defaults_match_the_ci_generator():
    """Defaults the CI generator used to apply with .get() live here now."""
    ci = CiTable()
    assert ci.windows is None and ci.windows_enabled is True
    assert ci.linux is None and ci.linux_enabled is True
    assert CiTable(linux=False).linux_enabled is False
    assert ci.deploy is True
    assert ci.coverage is False
    assert ci.xvfb is False
    assert ci.linux_arm is False
    assert ci.split_tests is False
    assert ci.windows_wheel_repair is None
    assert ci.test_shards == 0
    assert ci.docker_image == ""
    assert ci.python_versions == ["3.13"]
    assert ci.linux_python_versions is None
    assert ci.mac_python_versions is None


def test_coverage_table_defaults():
    """Thresholds default to 0; filters stay None because their default needs library_name."""
    coverage = CoverageTable()
    assert coverage.cpp_threshold == 0.0
    assert coverage.python_threshold == 0.0
    assert coverage.filters is None
    assert coverage.excludes == [r".*\.t\.h$", r".*/_package/tests/.*"]
    assert coverage.python_version is None


def test_xms_dependency_defaults_no_python_off():
    """no_python defaults to False, as the normalizer in gen set it."""
    assert XmsDependency(name="xmscore", version="7.0.0").no_python is False
