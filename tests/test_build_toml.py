"""Tests for the build.toml reader."""
from dataclasses import fields, FrozenInstanceError

import pytest

from xmsconan.build_toml import (
    BuildToml, CiTable, CoverageTable, KNOWN_KEYS, load_toml, read_build_toml,
    read_optional_build_toml, toml_to_dataclass, XmsDependency
)


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


def test_toml_to_dataclass_requires_library_name():
    """Every reader needs the name; the error names the file."""
    with pytest.raises(ValueError, match="build.toml does not define library_name"):
        toml_to_dataclass({"description": "d"}, "build.toml")


def test_toml_to_dataclass_converts_sub_tables():
    """[ci], [coverage] and xms_dependencies come back typed."""
    config = toml_to_dataclass({
        "library_name": "xmscore",
        "ci": {"xvfb": True, "python_versions": ["3.10", "3.13"]},
        "coverage": {"cpp_threshold": 80, "python_version": 3.13},
        "xms_dependencies": [{"name": "xmsgrid", "version": "7.0.0", "no_python": True}],
    }, "build.toml")
    assert config.ci == CiTable(xvfb=True, python_versions=["3.10", "3.13"])
    assert config.coverage.cpp_threshold == 80.0
    assert isinstance(config.coverage.cpp_threshold, float)
    assert config.coverage.python_version == "3.13"
    assert config.xms_dependencies == [XmsDependency("xmsgrid", "7.0.0", no_python=True)]


def test_toml_to_dataclass_leaves_matrix_and_filter_as_dicts():
    """The packager owns those vocabularies and the templates write them verbatim."""
    config = toml_to_dataclass({
        "library_name": "xmscore",
        "matrix": {"compiler_runtime": ["dynamic"]},
        "filter": {"build_type": "Release"},
    }, "build.toml")
    assert config.matrix == {"compiler_runtime": ["dynamic"]}
    assert config.filter == {"build_type": "Release"}


def test_toml_to_dataclass_rejects_a_bad_ci_key():
    """The [ci] allowlist is applied during conversion, so no reader can skip it."""
    with pytest.raises(ValueError, match="windows_repair_wheel"):
        toml_to_dataclass({"library_name": "x", "ci": {"windows_repair_wheel": True}}, "build.toml")


def test_toml_to_dataclass_rejects_an_unknown_coverage_key():
    """[coverage] gets the same unknown-key rule [ci] already has."""
    with pytest.raises(ValueError, match=r"\[coverage\] has unknown key\(s\) cpp_treshold"):
        toml_to_dataclass({"library_name": "x", "coverage": {"cpp_treshold": 80}}, "build.toml")


def test_toml_to_dataclass_rejects_a_non_table_coverage():
    """A scalar where a table belongs is named, not passed through."""
    with pytest.raises(ValueError, match=r"\[coverage\] must be a table"):
        toml_to_dataclass({"library_name": "x", "coverage": 80}, "build.toml")


@pytest.mark.parametrize("entry,expected", [
    pytest.param("xmscore/7.0.0", "must be a table", id="string-entry"),
    pytest.param({"name": "xmscore"}, "requires name and version", id="missing-version"),
    pytest.param({"name": "xmscore", "version": "7", "python": False}, r"unknown key\(s\) python",
                 id="unknown-key"),
])
def test_toml_to_dataclass_rejects_malformed_xms_dependencies(entry, expected):
    """An entry the conanfile template could not render fails at conversion instead."""
    with pytest.raises(ValueError, match=expected):
        toml_to_dataclass({"library_name": "x", "xms_dependencies": [entry]}, "build.toml")


def test_read_build_toml_returns_a_typed_config(tmp_path):
    """One call parses, validates, and converts."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core"\n[ci]\nxvfb = true\n', encoding="utf-8"
    )
    config = read_build_toml(toml_file)
    assert config == BuildToml(library_name="xmscore", description="Core", ci=CiTable(xvfb=True))


def test_read_build_toml_rejects_an_unknown_top_level_key(tmp_path):
    """The reader validates; load_toml alone does not."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\nhas_test_files = true\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"unknown top-level key\(s\) has_test_files"):
        read_build_toml(toml_file)


def test_read_build_toml_names_the_file_on_a_parse_error(tmp_path):
    """A decode error says which file, not just which line."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text("library_name = \n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"could not parse .*build\.toml"):
        read_build_toml(toml_file)


def test_read_build_toml_raises_when_the_file_is_missing(tmp_path):
    """The required reader does not guess; use read_optional_build_toml for that."""
    with pytest.raises(FileNotFoundError):
        read_build_toml(tmp_path / "build.toml")


def test_read_optional_build_toml_returns_none_when_absent(tmp_path):
    """A checkout with no build.toml is a normal state for the VS2019 driver."""
    assert read_optional_build_toml(tmp_path / "build.toml") is None


def test_read_optional_build_toml_reads_a_present_file(tmp_path):
    """Present files go through the same reader as the required form."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")
    assert read_optional_build_toml(toml_file) == read_build_toml(toml_file)
