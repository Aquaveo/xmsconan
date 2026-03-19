"""Tests for ci_tools.conan_deploy."""
import subprocess
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.conan_deploy import conan_deploy


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_save(mock_run):
    """--save calls conan cache save."""
    conan_deploy("xmscore", "7.0.0", save="xmscore-7.0.0.tar.gz")

    mock_run.assert_called_once_with(
        [
            "conan", "cache", "save",
            "--file", "xmscore-7.0.0.tar.gz",
            "xmscore/7.0.0",
        ],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_restore(mock_run):
    """--restore calls conan cache restore."""
    conan_deploy("xmscore", "7.0.0", restore="xmscore-7.0.0.tar.gz")

    mock_run.assert_called_once_with(
        ["conan", "cache", "restore", "xmscore-7.0.0.tar.gz"],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_upload(mock_run):
    """--upload calls conan upload with aquaveo remote."""
    conan_deploy("xmsgrid", "2.0.0", upload=True)

    mock_run.assert_called_once_with(
        ["conan", "upload", "xmsgrid/2.0.0", "-r", "aquaveo", "--confirm"],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_restore_and_upload(mock_run):
    """--restore + --upload runs both in order."""
    conan_deploy(
        "xmscore", "7.0.0",
        restore="pkg.tar.gz",
        upload=True,
    )

    assert mock_run.call_count == 2
    calls = mock_run.call_args_list
    # restore first
    assert "restore" in calls[0][0][0]
    # upload second
    assert "upload" in calls[1][0][0]


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_save_and_upload(mock_run):
    """--save + --upload runs both in order."""
    conan_deploy("xmscore", "7.0.0", save="out.tar.gz", upload=True)

    assert mock_run.call_count == 2
    calls = mock_run.call_args_list
    assert "save" in calls[0][0][0]
    assert "upload" in calls[1][0][0]


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_no_action_does_nothing(mock_run):
    """No flags → no subprocess calls."""
    conan_deploy("xmscore", "7.0.0")

    mock_run.assert_not_called()


@patch(
    "xmsconan.ci_tools.conan_deploy.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_propagates_called_process_error(mock_run):
    """Verify CalledProcessError propagates to caller."""
    with pytest.raises(subprocess.CalledProcessError):
        conan_deploy("xmscore", "7.0.0", upload=True)
