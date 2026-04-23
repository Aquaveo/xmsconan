"""Tests for ci_tools.wheel_deploy."""
import subprocess
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.wheel_deploy import wheel_deploy
from .utils import patch_env


@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_with_explicit_args(mock_run):
    """Explicit URL/username/password are used in devpi calls."""
    wheel_deploy(
        wheel_dir="wh",
        url="https://example.com/dev/",
        username="user",
        password="pass",
    )

    assert mock_run.call_count == 3
    mock_run.assert_any_call(
        ["devpi", "use", "https://example.com/dev/"], check=True,
    )
    mock_run.assert_any_call(
        ["devpi", "login", "user", "--password", "pass"], check=True,
    )
    mock_run.assert_any_call(
        ["devpi", "upload", "--from-dir", "wh"], check=True,
    )


@patch_env(
    {
        "AQUAPI_URL": "https://env.example.com/",
        "AQUAPI_USERNAME": "envuser",
        "AQUAPI_PASSWORD": "envpass",
    }
)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_from_env_vars(mock_run):
    """Falls back to environment variables."""
    wheel_deploy()

    mock_run.assert_any_call(
        ["devpi", "use", "https://env.example.com/"], check=True,
    )
    mock_run.assert_any_call(
        ["devpi", "login", "envuser", "--password", "envpass"], check=True,
    )


@patch_env(clear=True)
@patch(
    "xmsconan.ci_tools.wheel_deploy.load_credentials",
    return_value={
        "url": "https://cfg.example.com/",
        "username": "cfguser",
        "password": "cfgpass",
    },
)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_from_config_file(mock_run, mock_creds):
    """Falls back to ~/.xmsconan.toml when no args or env vars."""
    wheel_deploy()

    mock_run.assert_any_call(
        ["devpi", "use", "https://cfg.example.com/"], check=True,
    )
    mock_run.assert_any_call(
        ["devpi", "login", "cfguser", "--password", "cfgpass"], check=True,
    )


@patch_env(clear=True)
@patch(
    "xmsconan.ci_tools.wheel_deploy.load_credentials",
    return_value={},
)
def test_missing_url_raises(mock_creds):
    """Raises ValueError when no URL is available."""
    with pytest.raises(ValueError, match="No devpi URL"):
        wheel_deploy()


@patch_env({"AQUAPI_URL": "https://x/"}, clear=True)
@patch(
    "xmsconan.ci_tools.wheel_deploy.load_credentials",
    return_value={},
)
def test_missing_username_raises(mock_creds):
    """Raises ValueError when no username is available."""
    with pytest.raises(ValueError, match="No devpi username"):
        wheel_deploy()


@patch_env(
    {"AQUAPI_URL": "https://x/", "AQUAPI_USERNAME": "u"},
    clear=True
)
@patch(
    "xmsconan.ci_tools.wheel_deploy.load_credentials",
    return_value={},
)
def test_missing_password_raises(mock_creds):
    """Raises ValueError when no password is available."""
    with pytest.raises(ValueError, match="No devpi password"):
        wheel_deploy()


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run", side_effect=subprocess.CalledProcessError(1, "devpi"))
def test_propagates_called_process_error(mock_run):
    """Verify CalledProcessError propagates to caller."""
    with pytest.raises(subprocess.CalledProcessError):
        wheel_deploy(url="https://x/", username="u", password="p")
