"""Tests for the build.toml reader."""
from dataclasses import fields, FrozenInstanceError

import pytest

from xmsconan.build_toml import (
    _CI_KEY_TYPES, _load_toml, _matrix_table, _toml_to_dataclass, BuildToml, CiTable, CoverageTable, matrix_table,
    MatrixTable, read_build_toml, read_optional_build_toml, XmsDependency
)


#: A [ci] table carrying only what the reader insists on.
_REQUIRED_CI = {"python_versions": ["3.13"], "windows_wheel_repair": True}


def test_load_toml_returns_the_parsed_table(tmp_path):
    """_load_toml is a plain parser: no validation, no defaults."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\nanything = 1\n[ci]\nxvfb = true\n', encoding="utf-8")
    assert _load_toml(toml_file) == {"library_name": "xmscore", "anything": 1, "ci": {"xvfb": True}}


def test_build_toml_derives_python_namespaced_dir():
    """The default strips the xms prefix, the same rule gen and ci applied."""
    assert BuildToml(library_name="xmscore").python_namespaced_dir == "core"


def test_build_toml_keeps_an_explicit_python_namespaced_dir():
    """A value from the file is not overwritten by the derived default."""
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
    assert coverage.parallel is False
    assert coverage.python_version is None


def test_xms_dependency_defaults_no_python_off():
    """no_python defaults to False, as the normalizer in gen set it."""
    assert XmsDependency(name="xmscore", version="7.0.0").no_python is False


def test_toml_to_dataclass_requires_library_name():
    """Every reader needs the name; the error names the file."""
    with pytest.raises(ValueError, match="build.toml does not define library_name"):
        _toml_to_dataclass({"description": "d"}, "build.toml")


def test_toml_to_dataclass_converts_sub_tables():
    """[ci], [coverage] and xms_dependencies come back typed."""
    config = _toml_to_dataclass({
        "library_name": "xmscore",
        "ci": {"xvfb": True, "python_versions": ["3.10", "3.13"], "windows_wheel_repair": False},
        "coverage": {"cpp_threshold": 80, "python_version": "3.13"},
        "xms_dependencies": [{"name": "xmsgrid", "version": "7.0.0", "no_python": True}],
    }, "build.toml")
    assert config.ci == CiTable(xvfb=True, python_versions=["3.10", "3.13"], windows_wheel_repair=False)
    assert config.coverage.cpp_threshold == 80.0
    assert isinstance(config.coverage.cpp_threshold, float)
    assert config.coverage.python_version == "3.13"
    assert config.xms_dependencies == [XmsDependency("xmsgrid", "7.0.0", no_python=True)]


def test_toml_to_dataclass_leaves_matrix_and_filter_as_dicts():
    """The packager owns those vocabularies and the templates write them verbatim."""
    config = _toml_to_dataclass({
        "library_name": "xmscore",
        "ci": dict(_REQUIRED_CI),
        "matrix": {"compiler_runtime": ["dynamic"]},
        "filter": {"build_type": "Release"},
    }, "build.toml")
    assert config.matrix == {"compiler_runtime": ["dynamic"]}
    assert config.filter == {"build_type": "Release"}


def test_ci_key_types_name_every_ci_table_field():
    """The type table and the dataclass describe the same [ci] keys.

    They are two independent definitions of one schema, so either can grow a
    key the other lacks; this is the check that they cannot drift.
    """
    assert set(_CI_KEY_TYPES) == {f.name for f in fields(CiTable)}


def test_toml_to_dataclass_accepts_every_ci_key_at_its_declared_type():
    """A [ci] table naming every key at its declared type converts cleanly."""
    sample = {
        key: {bool: True, int: 4, str: "image", list: ["3.13"]}[expected]
        for key, expected in _CI_KEY_TYPES.items()
    }
    config = _toml_to_dataclass({"library_name": "x", "ci": sample}, "build.toml")
    assert config.ci == CiTable(**sample)


@pytest.mark.parametrize("table,expected", [
    ({"window_wheel_repair": False}, r"build\.toml: \[ci\] has unknown key\(s\) window_wheel_repair"),
    ({"windows_repair_wheel": False}, "windows_repair_wheel"),
    ({"windows_wheel_repair": "false"}, r"build\.toml: \[ci\]\.windows_wheel_repair must be bool"),
    ({"windows_wheel_repair": 0}, "must be bool"),
    ({"test_shards": True}, "must be an integer"),
    ({"docker_image": ["a"]}, "must be str"),
    ({"python_versions": "3.13"}, "must be list"),
], ids=[
    "misspelled-key-transposed",
    "misspelled-key-reordered",
    "quoted-boolean",
    "int-for-boolean",
    "boolean-for-int",
    "list-for-string",
    "string-for-list",
])
def test_toml_to_dataclass_rejects_bad_ci_keys_and_types(table, expected):
    """A typo or a quoted boolean fails at read time, naming the file.

    Every case here previously read as its default. For a switch whose whole
    purpose is to turn work *off*, the default means the work keeps happening,
    and the only symptom is the thing the switch exists to prevent.
    """
    with pytest.raises(ValueError, match=expected):
        _toml_to_dataclass({"library_name": "x", "ci": table}, "build.toml")


@pytest.mark.parametrize("table,expected", [
    (None, r"\[ci\] must set python_versions, windows_wheel_repair\."),
    ({}, r"\[ci\] must set python_versions, windows_wheel_repair\."),
    ({"xvfb": True}, r"\[ci\] must set python_versions, windows_wheel_repair\."),
    ({"python_versions": ["3.13"]}, r"\[ci\] must set windows_wheel_repair\.\n  windows_wheel_repair: "),
    ({"windows_wheel_repair": False}, r"\[ci\] must set python_versions\.\n  python_versions: "),
], ids=["no-table", "empty-table", "other-keys-only", "repair-missing", "versions-missing"])
def test_toml_to_dataclass_requires_python_versions_and_windows_wheel_repair(table, expected):
    """Both keys are required, and the error names every one that is missing.

    Each used to default, and each default fit only one of the two kinds of
    library this tool builds -- so a developer who never thought about the key
    found out from a broken install, after the release. The reader asks up
    front instead, and explains what each key decides so the answer can be
    given without opening the docs.
    """
    data = {"library_name": "x"}
    if table is not None:
        data["ci"] = table
    with pytest.raises(ValueError, match=r"build\.toml: " + expected):
        _toml_to_dataclass(data, "build.toml")


def test_a_typo_in_ci_is_reported_before_a_missing_required_key():
    """The more specific error wins: a misspelled key is not 'missing'."""
    with pytest.raises(ValueError, match=r"unknown key\(s\) window_wheel_repair"):
        _toml_to_dataclass({"library_name": "x", "ci": {"window_wheel_repair": False}}, "build.toml")


@pytest.mark.parametrize("key", ["python_versions", "linux_python_versions", "mac_python_versions"])
def test_toml_to_dataclass_rejects_an_empty_python_versions_list(key):
    """An empty list satisfies 'required' and builds no pybind leg; it is refused."""
    table = {**_REQUIRED_CI, key: []}
    with pytest.raises(ValueError, match=rf"\[ci\]\.{key} must name at least one Python version"):
        _toml_to_dataclass({"library_name": "x", "ci": table}, "build.toml")


@pytest.mark.parametrize("key", ["python_versions", "linux_python_versions", "mac_python_versions"])
def test_toml_to_dataclass_rejects_an_unquoted_python_version(key):
    """`python_versions = [3.10]` is the float 3.1; the fix is the quotes, so the error says so."""
    table = {**_REQUIRED_CI, key: [3.1]}
    with pytest.raises(ValueError, match=rf'\[ci\]\.{key} entries must be quoted strings like "3\.13", got 3\.1'):
        _toml_to_dataclass({"library_name": "x", "ci": table}, "build.toml")


def test_toml_to_dataclass_rejects_a_non_table_ci():
    """[ci] itself has to be a table, and the error names the file."""
    with pytest.raises(ValueError, match=r"build\.toml: \[ci\] must be a table"):
        _toml_to_dataclass({"library_name": "x", "ci": ["windows"]}, "build.toml")


def test_toml_to_dataclass_rejects_an_unknown_coverage_key():
    """[coverage] gets the same unknown-key rule [ci] already has."""
    with pytest.raises(ValueError, match=r"\[coverage\] has unknown key\(s\) cpp_treshold"):
        _toml_to_dataclass({"library_name": "x", "coverage": {"cpp_treshold": 80}}, "build.toml")


def test_toml_to_dataclass_rejects_a_non_table_coverage():
    """A scalar where a table belongs is named, not passed through."""
    with pytest.raises(ValueError, match=r"\[coverage\] must be a table"):
        _toml_to_dataclass({"library_name": "x", "coverage": 80}, "build.toml")


def test_toml_to_dataclass_names_a_non_numeric_threshold():
    """A threshold that is not a number is reported with its file and key."""
    with pytest.raises(ValueError, match=r"build\.toml: \[coverage\]\.cpp_threshold must be a number"):
        _toml_to_dataclass({"library_name": "x", "coverage": {"cpp_threshold": "80%"}}, "build.toml")


def test_toml_to_dataclass_rejects_a_boolean_threshold():
    """`cpp_threshold = true` is a mistake, not 100%; bool is an int subclass so float() would take it."""
    with pytest.raises(ValueError, match=r"\[coverage\]\.cpp_threshold must be a number, got a boolean"):
        _toml_to_dataclass({"library_name": "x", "coverage": {"cpp_threshold": True}}, "build.toml")


@pytest.mark.parametrize("key", ["filters", "excludes"])
def test_toml_to_dataclass_rejects_a_scalar_coverage_pattern_list(key):
    """A lone string would be iterated character by character into gcovr patterns."""
    with pytest.raises(ValueError, match=rf"\[coverage\]\.{key} must be a list"):
        _toml_to_dataclass({"library_name": "x", "coverage": {key: "xmscore/"}}, "build.toml")


def test_toml_to_dataclass_rejects_a_non_boolean_parallel():
    """A quoted "false" is truthy, so it would keep the overlap it was written to stop."""
    with pytest.raises(ValueError, match=r"\[coverage\]\.parallel must be true or false"):
        _toml_to_dataclass({"library_name": "x", "coverage": {"parallel": "false"}}, "build.toml")


def test_toml_to_dataclass_rejects_an_unquoted_coverage_python_version():
    """An unquoted 3.10 is the float 3.1; coercing it would pin a version nobody wrote."""
    with pytest.raises(ValueError, match=r"\[coverage\]\.python_version must be a quoted string"):
        _toml_to_dataclass({"library_name": "x", "coverage": {"python_version": 3.10}}, "build.toml")


@pytest.mark.parametrize("entry,expected", [
    pytest.param("xmscore/7.0.0", "must be a table", id="string-entry"),
    pytest.param({"name": "xmscore"}, "requires name and version", id="missing-version"),
    pytest.param({"name": "xmscore", "version": "7", "python": False}, r"unknown key\(s\) python",
                 id="unknown-key"),
    pytest.param({"name": "xmscore", "version": 7}, "version must be a string", id="numeric-version"),
    pytest.param({"name": "xmscore", "version": "7", "no_python": "false"}, "no_python must be true or false",
                 id="quoted-boolean"),
])
def test_toml_to_dataclass_rejects_malformed_xms_dependencies(entry, expected):
    """An entry the conanfile template could not render fails at conversion instead."""
    with pytest.raises(ValueError, match=expected):
        _toml_to_dataclass({"library_name": "x", "xms_dependencies": [entry]}, "build.toml")


def test_read_build_toml_returns_a_typed_config(tmp_path):
    """One call parses, validates, and converts."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core"\n'
        '[ci]\n'
        'python_versions = ["3.13"]\n'
        'windows_wheel_repair = true\n'
        'xvfb = true\n', encoding="utf-8"
    )
    config = read_build_toml(toml_file)
    assert config == BuildToml(
        library_name="xmscore", description="Core",
        ci=CiTable(xvfb=True, python_versions=["3.13"], windows_wheel_repair=True),
    )


def test_read_build_toml_rejects_an_unknown_top_level_key(tmp_path):
    """The reader validates; _load_toml alone does not."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n'
                         'has_test_files = true\n'
                         '\n'
                         '[ci]\n'
                         'python_versions = ["3.13"]\n'
                         'windows_wheel_repair = true\n', encoding="utf-8")
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
    toml_file.write_text('library_name = "xmscore"\n'
                         '\n'
                         '[ci]\n'
                         'python_versions = ["3.13"]\n'
                         'windows_wheel_repair = true\n', encoding="utf-8")
    assert read_optional_build_toml(toml_file) == read_build_toml(toml_file)


# --- [matrix] ---


def test_matrix_table_reads_a_full_table():
    """Every key lands in its field; lists become tuples because the record is frozen."""
    table = matrix_table({
        "compiler_runtime": ["dynamic"],
        "pybind_build_types": ["Release", "Debug"],
        "wheel_only": True,
    }, "build.toml: [matrix]")
    assert table == MatrixTable(compiler_runtime=("dynamic",), pybind_build_types=("Release", "Debug"),
                                wheel_only=True)


@pytest.mark.parametrize("raw", [None, {}], ids=["absent", "empty"])
def test_matrix_table_absent_is_all_unset(raw):
    """No table and an empty table both mean the file said nothing."""
    assert matrix_table(raw, "build.toml: [matrix]") == MatrixTable()


def test_matrix_table_returns_a_table_unchanged():
    """A caller that already has a MatrixTable pays nothing and gets the same object back."""
    table = MatrixTable(wheel_only=True)
    assert matrix_table(table, "anywhere") is table


def test_matrix_table_accepts_an_empty_list():
    """An empty list is well-typed. Whether it builds anything is for the code that reads it."""
    table = matrix_table({"compiler_runtime": []}, "build.toml: [matrix]")
    assert table.compiler_runtime == ()


@pytest.mark.parametrize("raw,expected", [
    (["dynamic"], r"\[matrix\] must be a table, got list"),
    ({"compiler_runtimes": ["dynamic"]},
     r"\[matrix\] has unknown key\(s\) compiler_runtimes\. Accepted keys: compiler_runtime, pybind_build_types, "
     r"wheel_only\."),
    ({"wheel_only": "true"}, r"\[matrix\]\.wheel_only must be a boolean, got str \('true'\)"),
    ({"wheel_only": 1}, r"\[matrix\]\.wheel_only must be a boolean, got int \(1\)"),
    ({"compiler_runtime": "dynamic"}, r"\[matrix\]\.compiler_runtime must be a list, got str"),
    ({"compiler_runtime": ["MD"]},
     r"\[matrix\]\.compiler_runtime names unknown value\(s\) MD\. Accepted values: dynamic, static\."),
    ({"pybind_build_types": ["RelWithDebInfo", "Release"]},
     r"\[matrix\]\.pybind_build_types names unknown value\(s\) RelWithDebInfo\. Accepted values: Release, Debug\."),
    ({"pybind_build_types": [1]}, r"\[matrix\]\.pybind_build_types names unknown value\(s\) 1\."),
], ids=[
    "not-a-table", "misspelled-key", "quoted-boolean", "int-for-boolean", "string-for-list",
    "unknown-runtime", "unknown-build-type", "non-string-entry",
])
def test_matrix_table_rejects_bad_shape_naming_the_file(raw, expected):
    """A typo, a wrong type, or a word outside the vocabulary fails at read time, naming the file.

    The accepted words are build.toml's own. They match Conan's and CMake's
    spellings today because the file was designed that way, which is why the
    check lives in the reader and not with the code that hands them to Conan.
    """
    with pytest.raises(ValueError, match=r"build\.toml: " + expected):
        matrix_table(raw, "build.toml: [matrix]")


def test_matrix_table_prefixes_messages_with_where():
    """The generated conanfile hands the packager the same table, and its errors should name that file."""
    with pytest.raises(ValueError, match=r"^conanfile\.py CONAN_MATRIX\.wheel_only must be a boolean"):
        matrix_table({"wheel_only": "yes"}, "conanfile.py CONAN_MATRIX")


def test_private_matrix_table_names_the_toml_path():
    """The reader's wrapper supplies the ``<path>: [matrix]`` prefix the other tables use."""
    with pytest.raises(ValueError, match=r"^sub/build\.toml: \[matrix\] has unknown key"):
        _matrix_table({"bogus": 1}, "sub/build.toml")


@pytest.mark.parametrize("table,expected", [
    (MatrixTable(), {}),
    (MatrixTable(wheel_only=False), {}),
    (MatrixTable(compiler_runtime=("dynamic",)), {"compiler_runtime": ["dynamic"]}),
    (MatrixTable(compiler_runtime=("dynamic", "static"), pybind_build_types=("Release", "Debug"), wheel_only=True),
     {"compiler_runtime": ["dynamic", "static"], "pybind_build_types": ["Release", "Debug"], "wheel_only": True}),
], ids=["default", "explicit-false", "one-key", "all-keys"])
def test_matrix_table_to_dict_spells_the_table_as_the_file_would(table, expected):
    """Only set keys appear, as lists, so the generated CONAN_MATRIX literal reads as before."""
    assert table.to_dict() == expected


def test_matrix_table_round_trips_through_to_dict():
    """What the reader accepts, to_dict reproduces, and the reader accepts again."""
    raw = {"compiler_runtime": ["static"], "pybind_build_types": ["Debug"], "wheel_only": True}
    table = matrix_table(raw, "build.toml: [matrix]")
    assert table.to_dict() == raw
    assert matrix_table(table.to_dict(), "build.toml: [matrix]") == table
