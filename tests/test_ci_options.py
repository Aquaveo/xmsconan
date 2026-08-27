"""Tests for the shared ``[ci]`` table contract."""
import pytest

from xmsconan.ci_options import (
    CI_KEY_TYPES,
    repairs_windows_wheel,
    validate_ci_table,
)


# --- validate_ci_table ---------------------------------------------------


def test_empty_table_is_valid():
    """A library with no [ci] table is the common case."""
    validate_ci_table({})


def test_every_documented_key_is_accepted():
    """A table naming every key at its declared type passes.

    Guards against a key being added to one reader and left out of the
    allowlist, which would make a correct build.toml fail generation.
    """
    sample = {
        key: {bool: True, int: 4, str: "image", list: ["3.13"]}[expected]
        for key, expected in CI_KEY_TYPES.items()
    }
    validate_ci_table(sample)


@pytest.mark.parametrize("table,expected", [
    ({"window_wheel_repair": False}, "window_wheel_repair"),
    ({"windows_repair_wheel": False}, "windows_repair_wheel"),
    ({"windows_wheel_repair": "false"}, "must be bool"),
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
def test_bad_keys_and_types_are_rejected(table, expected):
    """A typo or a quoted boolean fails at generation, not silently.

    Every case here previously read as its default. For a switch whose whole
    purpose is to turn work *off*, the default means the work keeps happening,
    and the only symptom is the thing the switch exists to prevent.
    """
    with pytest.raises(ValueError, match=expected):
        validate_ci_table(table)


def test_non_table_is_rejected():
    """[ci] itself has to be a table."""
    with pytest.raises(ValueError, match=r"\[ci\] must be a table"):
        validate_ci_table(["windows"])


# --- repairs_windows_wheel ----------------------------------------------


@pytest.mark.parametrize("ci_type,expected", [
    ("github", True),
    ("gitlab", False),
])
def test_default_follows_ci_type(ci_type, expected):
    """The default is derived from who installs the wheel, not hardcoded.

    GitHub wheels go to a public index and are installed into arbitrary Python
    environments, which supply no XMS runtime -- they need their DLLs bundled.
    GitLab wheels are internal and loaded only by the XMS Python, which puts its
    own shipped C++ runtime on PATH deliberately; repairing those vendors a
    private mangled msvcp140 that overrides it.

    A flat default is wrong in one direction or the other, and both directions
    change what an existing repo publishes on its next tag.
    """
    assert repairs_windows_wheel({"ci_type": ci_type}) is expected


@pytest.mark.parametrize("ci_type", ["github", "gitlab"])
@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_value_overrides_the_default(ci_type, explicit):
    """An explicit key wins on either host."""
    data = {"ci_type": ci_type, "ci": {"windows_wheel_repair": explicit}}
    assert repairs_windows_wheel(data) is explicit


def test_unknown_ci_type_repairs():
    """An unrecognized (or absent) ci_type keeps the safer, DLL-bundling default."""
    assert repairs_windows_wheel({}) is True


def test_resolver_validates_the_table():
    """The resolver is also the validation point, so no reader can skip it."""
    with pytest.raises(ValueError, match="windows_repair_wheel"):
        repairs_windows_wheel({"ci_type": "gitlab", "ci": {"windows_repair_wheel": True}})
