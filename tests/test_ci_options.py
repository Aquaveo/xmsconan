"""Tests for the Windows wheel-repair decision."""
import pytest

from xmsconan.build_toml import BuildToml, CiTable
from xmsconan.ci_options import repairs_windows_wheel


# --- repairs_windows_wheel ----------------------------------------------


@pytest.mark.parametrize("ci_type", ["github", "gitlab", None])
@pytest.mark.parametrize("explicit", [True, False])
def test_the_explicit_value_is_the_answer_on_every_host(ci_type, explicit):
    """``ci_type`` has no say any more.

    It used to pick the default (repair on GitHub, not on GitLab) as a proxy
    for who installs the wheel, and the proxy went stale: libraries whose
    wheels the desktop products load and libraries whose wheels go to plain
    Python environments now live on both hosts.
    """
    config = BuildToml(library_name="xmscore", ci_type=ci_type, ci=CiTable(windows_wheel_repair=explicit))
    assert repairs_windows_wheel(config) is explicit


@pytest.mark.parametrize("ci_type", ["github", "gitlab", None])
def test_an_unset_key_is_refused_rather_than_guessed(ci_type):
    """A config built in code without the key gets an error naming it.

    A file cannot reach here unset -- the reader rejects it -- so this guards
    the programmatic path, where a silent default would bring back the guess
    the reader exists to prevent.
    """
    config = BuildToml(library_name="xmscore", ci_type=ci_type)
    with pytest.raises(ValueError, match=r"\[ci\]\.windows_wheel_repair is not set"):
        repairs_windows_wheel(config)
