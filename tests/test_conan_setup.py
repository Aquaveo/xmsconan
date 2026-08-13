"""Tests for ci_tools.conan_setup."""
import subprocess
import sys
from unittest.mock import call, patch

import pytest

from xmsconan.ci_tools.conan_setup import conan_setup, DEFAULT_REMOTE_URL, main


def _login_call(mock_run):
    """Return the ``conan remote login`` call recorded on ``mock_run``."""
    logins = [
        c for c in mock_run.call_args_list
        if c[0][0][:3] == ["conan", "remote", "login"]
    ]
    assert len(logins) == 1, f"expected one login call, got {logins}"
    return logins[0]


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_default_setup(mock_run):
    """Default invocation detects profile and adds aquaveo remote at index 0."""
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


@patch("xmsconan.ci_tools.conan_setup.load_conan_credentials", return_value={})
@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_login_flag(mock_run, mock_creds):
    """--login triggers conan remote login; no credentials means no env."""
    conan_setup(login=True)

    assert mock_run.call_count == 3
    mock_run.assert_any_call(
        ["conan", "remote", "login", "aquaveo"], check=True, env=None,
    )


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_remove_conancenter_flag(mock_run):
    """--remove-conancenter removes the conancenter remote."""
    conan_setup(remove_conancenter=True)

    assert mock_run.call_count == 3
    mock_run.assert_any_call(
        ["conan", "remote", "remove", "conancenter"], check=True,
    )


@patch("xmsconan.ci_tools.conan_setup.load_conan_credentials", return_value={})
@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_all_flags_together(mock_run, mock_creds):
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
        ["conan", "remote", "login", "aquaveo"], check=True, env=None,
    )


@patch(
    "xmsconan.ci_tools.conan_setup.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_propagates_called_process_error(mock_run):
    """Verify CalledProcessError propagates to caller."""
    with pytest.raises(subprocess.CalledProcessError):
        conan_setup()


# --- remote list position ---


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_index_none_appends_the_remote(mock_run):
    """index=None appends instead of making the remote the machine's first stop.

    Conan resolves a version range across every remote in list order, so a
    special-purpose remote inserted at index 0 would be consulted first by
    every unrelated ``conan install`` on the developer's machine.
    """
    conan_setup(remote_url="https://example.com/x", remote_name="x", index=None)

    add_call = mock_run.call_args_list[1][0][0]
    assert add_call == ["conan", "remote", "add", "x", "https://example.com/x", "--force"]
    assert "--index" not in add_call


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_explicit_index_is_stringified(mock_run):
    """A non-default index reaches conan as a string argument."""
    conan_setup(index=2)

    assert mock_run.call_args_list[1][0][0][:5] == [
        "conan", "remote", "add", "--index", "2",
    ]


# --- credential-based login ---


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_login_passes_credentials_through_the_environment(mock_run):
    """The password reaches conan through env vars and never through argv.

    Process-creation auditing (Windows Event 4688, Sysmon Event ID 1) copies
    the full command line of every process into the event log, so a password
    passed as ``-p mypass`` ends up in the SIEM in cleartext.
    """
    conan_setup(login=True, username="myuser", password="mypass")

    login = _login_call(mock_run)
    assert login[0][0] == ["conan", "remote", "login", "aquaveo"]
    assert "mypass" not in " ".join(login[0][0])
    assert "-p" not in login[0][0]
    env = login[1]["env"]
    assert env["CONAN_LOGIN_USERNAME_AQUAVEO"] == "myuser"
    assert env["CONAN_PASSWORD_AQUAVEO"] == "mypass"
    # the rest of the environment is inherited, not replaced
    assert len(env) > 2


@patch("xmsconan.ci_tools.conan_setup.load_conan_credentials",
       return_value={"username": "cfguser", "password": "cfgpass"})
@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_login_with_config_file(mock_run, mock_creds):
    """Falls back to ~/.xmsconan.toml when no explicit credentials."""
    conan_setup(login=True)

    mock_creds.assert_called_once()
    env = _login_call(mock_run)[1]["env"]
    assert env["CONAN_LOGIN_USERNAME_AQUAVEO"] == "cfguser"
    assert env["CONAN_PASSWORD_AQUAVEO"] == "cfgpass"


@patch("xmsconan.ci_tools.conan_setup.load_conan_credentials",
       return_value={"username": "cfguser", "password": "cfgpass"})
@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_login_without_the_config_file(mock_run, mock_creds):
    """use_config_file=False leaves ~/.xmsconan.toml alone entirely.

    The manual VS2019 driver resolves both halves from that same file itself
    (so one read serves both, and one account's username pairs with its own
    password). A password still None afterwards is therefore a resolved "let
    conan prompt", and re-reading the file here would both open it a second
    time and overturn that decision.
    """
    conan_setup(login=True, username="resolved", use_config_file=False)

    mock_creds.assert_not_called()
    # nothing to hand over, so conan is left to prompt for the password
    assert _login_call(mock_run)[1]["env"] is None


@patch("xmsconan.ci_tools.conan_setup.load_conan_credentials",
       return_value={"username": "cfguser", "password": "cfgpass"})
@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_login_explicit_overrides_config(mock_run, mock_creds):
    """Explicit args take precedence over config file."""
    conan_setup(login=True, username="explicit", password="secret")

    env = _login_call(mock_run)[1]["env"]
    assert env["CONAN_LOGIN_USERNAME_AQUAVEO"] == "explicit"
    assert env["CONAN_PASSWORD_AQUAVEO"] == "secret"


# --- remote name ---


@patch("xmsconan.ci_tools.conan_setup.subprocess.run")
def test_custom_remote_name(mock_run):
    """A custom remote name is used for `remote add`, `remote login`, and the env vars.

    The manual VS2019 (msvc 192) matrix publishes to ``aquaveo-vs2019``
    instead of the CI remote.  Conan derives its per-remote credential env
    vars by uppercasing the remote name and replacing hyphens with
    underscores, so the hyphen matters here.
    """
    conan_setup(
        remote_url="https://example.com/aquaveo-vs2019",
        login=True,
        username="aquaveo",
        password="secret",
        remote_name="aquaveo-vs2019",
        index=None,
    )

    mock_run.assert_any_call(
        [
            "conan", "remote", "add",
            "aquaveo-vs2019", "https://example.com/aquaveo-vs2019", "--force",
        ],
        check=True,
    )
    login = _login_call(mock_run)
    assert login[0][0] == ["conan", "remote", "login", "aquaveo-vs2019"]
    assert "secret" not in " ".join(login[0][0])
    assert login[1]["env"]["CONAN_PASSWORD_AQUAVEO_VS2019"] == "secret"


# --- CLI entry point ---


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_passes_flags_through(mock_setup, monkeypatch):
    """main() forwards every CLI flag to conan_setup()."""
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_setup", "--remote-url", "https://example.com/conan",
        "--login", "--remove-conancenter", "--username", "u", "--password", "p",
    ])

    main()

    mock_setup.assert_called_once_with(
        remote_url="https://example.com/conan",
        login=True,
        remove_conancenter=True,
        username="u",
        password="p",
    )


@patch("xmsconan.ci_tools.conan_setup.conan_setup",
       side_effect=subprocess.CalledProcessError(4, "conan"))
def test_main_exits_with_conan_return_code(mock_setup, monkeypatch):
    """A failing conan command sets the process exit code."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 4
