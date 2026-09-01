"""Tests for ci_tools.conan_setup."""
import subprocess
import sys
from unittest.mock import call, patch

import pytest

from xmsconan.ci_tools.conan_setup import (
    conan_setup, DEFAULT_REMOTE_NAME, DEFAULT_REMOTE_URL, main,
)
from xmsconan.ci_tools.credentials import CredentialsError
from .utils import patch_env


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
def test_main_passes_flags_through(mock_setup, monkeypatch, tmp_path):
    """main() forwards every CLI flag to conan_setup()."""
    password_file = tmp_path / "p.txt"
    password_file.write_text("p\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_setup", "--remote-url", "https://example.com/conan",
        "--login", "--remove-conancenter", "--username", "u",
        "--password-file", str(password_file),
        "--remote-name", "aquaveo-vs2019", "--append",
    ])

    main()

    mock_setup.assert_called_once_with(
        remote_url="https://example.com/conan",
        login=True,
        remove_conancenter=True,
        username="u",
        password="p",
        remote_name="aquaveo-vs2019",
        # --append maps to index=None; the default is index=0, i.e. first.
        index=None,
    )


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_adds_the_remote_first_by_default(mock_setup, monkeypatch):
    """Without --append the remote goes to index 0, as the CI remote always has.

    The pair matters more than either value: --append exists so a
    special-purpose remote does not become the first stop for every conan
    install on a shared machine, and that is only true if omitting it keeps the
    old behavior.
    """
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup"])

    main()

    _, kwargs = mock_setup.call_args
    assert kwargs["index"] == 0
    assert kwargs["remote_name"] == DEFAULT_REMOTE_NAME


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_has_no_password_flag(mock_setup, monkeypatch, capsys):
    """There is no --password: the secret never goes on this process's argv.

    _login_environment exists so the password stays out of argv on the way
    *out* of this command; a --password flag put it on the argv on the way in,
    which the same Event 4688 / Sysmon capture reads just as well.
    """
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_setup", "--login", "--password", "secret",
    ])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2  # argparse usage error
    mock_setup.assert_not_called()
    # And it is refused by name rather than swallowed by argparse's prefix
    # matching, which would otherwise read "secret" as a --password-file path.
    assert "--password-file" in capsys.readouterr().err


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_reads_the_password_file(mock_setup, monkeypatch, tmp_path):
    """--password-file supplies the password, minus the editor's newline."""
    password_file = tmp_path / "p.txt"
    password_file.write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_setup", "--login", "--password-file", str(password_file),
    ])

    main()

    assert mock_setup.call_args.kwargs["password"] == "s3cret"


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_rejects_an_unusable_password_file(mock_setup, monkeypatch, tmp_path, capsys):
    """A named file that holds no password stops the run, without logging in."""
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_setup", "--login",
        "--password-file", str(tmp_path / "missing.txt"),
    ])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2  # the same usage exit `vs2019 setup` uses
    assert "password file not found" in capsys.readouterr().err
    mock_setup.assert_not_called()


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_falls_back_to_the_conan_password_env_var(mock_setup, monkeypatch):
    """Without --password-file the password comes from $CONAN_PASSWORD."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup", "--login"])

    with patch_env({"CONAN_PASSWORD": "from-env"}):
        main()

    assert mock_setup.call_args.kwargs["password"] == "from-env"


@patch("xmsconan.ci_tools.conan_setup.conan_setup")
def test_main_leaves_the_password_none_when_no_source_has_one(mock_setup, monkeypatch):
    """No file and no env var is a resolved absence, not an empty string.

    conan_setup() still has ~/.xmsconan.toml to consult, and past that Conan
    prompts. An empty string would be a password as far as
    ``if username and password`` is concerned, and would be handed to Conan.
    """
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup", "--login"])

    with patch_env(clear=True):
        main()

    assert mock_setup.call_args.kwargs["password"] is None


@patch("xmsconan.ci_tools.conan_setup.conan_setup",
       side_effect=subprocess.CalledProcessError(4, "conan"))
def test_main_exits_with_conan_return_code(mock_setup, monkeypatch):
    """A failing conan command sets the process exit code."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 4


@patch("xmsconan.ci_tools.conan_setup.conan_setup",
       side_effect=CredentialsError("Could not parse /home/u/.xmsconan.toml: bad"))
def test_main_reports_an_unusable_config_file_as_a_usage_error(mock_setup, monkeypatch, capsys):
    """An unparseable ~/.xmsconan.toml exits 2 with one line, not a traceback.

    It is the same class of fault as an unusable --password-file, which exits 2
    two lines earlier in the same function, and `xmsconan vs2019 setup` already
    reports it that way. The handler is CredentialsError and not ValueError so
    it cannot also absorb one raised by the three subprocesses conan_setup runs.
    """
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "Could not parse" in capsys.readouterr().err


@patch("xmsconan.ci_tools.conan_setup.conan_setup", side_effect=ValueError("boom from a subprocess"))
def test_main_does_not_absorb_an_unrelated_value_error(mock_setup, monkeypatch):
    """Only CredentialsError is a usage error; anything else keeps its traceback.

    A bare `except ValueError` here would have relabelled a fault from any of
    conan_setup()'s three subprocess.run calls as an argparse usage error,
    which is the mislabelling this PR is otherwise busy removing.
    """
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_setup"])

    with pytest.raises(ValueError, match="boom from a subprocess"):
        main()
