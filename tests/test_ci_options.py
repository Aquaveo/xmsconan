"""Tests for the Windows wheel-repair decision."""
import pytest

from xmsconan.ci_options import repairs_windows_wheel
from .utils import make_build_toml


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
    assert repairs_windows_wheel(make_build_toml(ci_type=ci_type)) is expected


@pytest.mark.parametrize("ci_type", ["github", "gitlab"])
@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_value_overrides_the_default(ci_type, explicit):
    """An explicit key wins on either host."""
    config = make_build_toml(ci_type=ci_type, ci={"windows_wheel_repair": explicit})
    assert repairs_windows_wheel(config) is explicit


def test_unknown_ci_type_repairs():
    """An unrecognized (or absent) ci_type keeps the safer, DLL-bundling default."""
    assert repairs_windows_wheel(make_build_toml()) is True
