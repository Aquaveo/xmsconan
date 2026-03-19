"""Tests for ci_tools.conan_setup."""
import subprocess
from unittest.mock import call, patch

import pytest

from xmsconan.ci_tools.conan_setup import conan_setup, DEFAULT_REMOTE_URL


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_default_setup(mock_run):
    """Default invocation detects profile and adds aquaveo remote."""
    conan_setup()

    assert mock_run.call_count == 2
    mock_run.assert_any_call(
        ["conan", "profile", "detect", "-e"], check=True,
    )
    mock_run.assert_any_call(
        [
            "conan", "remote", "add", "--index", "0",
            "aquaveo", DEFAULT_REMOTE_URL, "--force",
        ],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_custom_remote_url(mock_run):
    """Custom URL is passed to conan remote add."""
    conan_setup(remote_url="https://custom.example.com/conan")

    add_call = mock_run.call_args_list[1]
    assert "https://custom.example.com/conan" in add_call[0][0]


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_login_flag(mock_run):
    """--login triggers conan remote login."""
    conan_setup(login=True)

    assert mock_run.call_count == 3
    mock_run.assert_any_call(
        ["conan", "remote", "login", "aquaveo"], check=True,
    )


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_remove_conancenter_flag(mock_run):
    """--remove-conancenter removes the conancenter remote."""
    conan_setup(remove_conancenter=True)

    assert mock_run.call_count == 3
    mock_run.assert_any_call(
        ["conan", "remote", "remove", "conancenter"], check=True,
    )


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_all_flags_together(mock_run):
    """All flags combined run in expected order."""
    conan_setup(login=True, remove_conancenter=True)

    assert mock_run.call_count == 4
    calls = mock_run.call_args_list
    # profile detect first
    assert calls[0] == call(
        ["conan", "profile", "detect", "-e"], check=True,
    )
    # remote add second
    assert "remote" in calls[1][0][0] and "add" in calls[1][0][0]
    # remove conancenter third
    assert calls[2] == call(
        ["conan", "remote", "remove", "conancenter"], check=True,
    )
    # login last
    assert calls[3] == call(
        ["conan", "remote", "login", "aquaveo"], check=True,
    )


@patch(
    "xmsconan.ci_tools.conan_setup.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_propagates_called_process_error(mock_run):
    """Verify CalledProcessError propagates to caller."""
    with pytest.raises(subprocess.CalledProcessError):
        conan_setup()
