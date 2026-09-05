"""Tests for ci_tools.wheel_deploy."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import call, patch

import pytest
from uv import find_uv_bin

from xmsconan.ci_tools.wheel_deploy import main, wheel_deploy, WheelDeployError
from .doc_helpers import slice_between, usage_section
from .utils import patch_env

UV = "/venv/bin/uv"


@pytest.fixture(autouse=True)
def _no_developer_config():
    """Keep the suite off the real ``~/.xmsconan.toml`` of whoever runs it.

    ``wheel_deploy`` calls ``load_credentials()`` on every path, and
    ``patch_env(clear=True)`` does not stop it: unsetting HOME only makes
    ``Path.home()`` fall back to the password database, so the developer's own
    config file is still found. A file that holds a URL would then supply the
    credential a "missing credential" test needs absent, and one that does not
    parse would red every test here for a reason that has nothing to do with
    the code under test. Tests that want config values patch the same name
    themselves, which takes effect inside this one.
    """
    with patch("xmsconan.ci_tools.wheel_deploy.load_credentials", return_value={}):
        yield


def _wheelhouse(tmp_path, *names):
    """A wheel directory holding empty wheels called *names*, as a str path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in names or ("xmscore-7.0.0-cp313-cp313-linux_x86_64.whl",):
        (tmp_path / name).write_bytes(b"")
    return str(tmp_path)


def _publish_call(mock_run):
    """The single ``uv publish`` call recorded on *mock_run*: ``(argv, kwargs)``."""
    assert mock_run.call_count == 1
    (argv,), kwargs = mock_run.call_args
    return argv, kwargs


@patch("xmsconan.ci_tools.wheel_deploy.find_uv_bin", return_value=UV)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_runs_uv_publish_with_every_wheel(mock_run, mock_uv, tmp_path):
    """Explicit URL/username/password drive one uv publish over the wheels, in a stable order."""
    wheel_dir = _wheelhouse(tmp_path, "b-1.0-py3-none-any.whl", "a-1.0-py3-none-any.whl")

    wheel_deploy(wheel_dir=wheel_dir, url="https://example.com/dev/", username="user", password="pass")

    argv, kwargs = _publish_call(mock_run)
    assert argv == [
        UV, "publish", "--publish-url", "https://example.com/dev/",
        os.path.join(wheel_dir, "a-1.0-py3-none-any.whl"),
        os.path.join(wheel_dir, "b-1.0-py3-none-any.whl"),
    ]
    assert kwargs["check"] is True
    assert kwargs["env"]["UV_PUBLISH_USERNAME"] == "user"
    assert kwargs["env"]["UV_PUBLISH_PASSWORD"] == "pass"


@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_runs_the_uv_installed_with_xmsconan(mock_run, tmp_path):
    """The binary is the uv package's own, not whatever `uv` is first on PATH.

    A `uv tool install` or pipx layout exposes only xmsconan's entry points,
    so PATH is not where the dependency put the binary.
    """
    wheel_deploy(wheel_dir=_wheelhouse(tmp_path), url="https://x/", username="u", password="p")

    argv, _ = _publish_call(mock_run)
    assert argv[0] == find_uv_bin()
    assert os.path.isfile(argv[0])


@patch_env({"XMS_TEST_MARKER": "inherited"})
@patch("xmsconan.ci_tools.wheel_deploy.find_uv_bin", return_value=UV)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_keeps_the_password_off_the_command_line(mock_run, mock_uv, tmp_path):
    """The secret reaches uv through its environment, never its argv.

    That environment is the process's own plus the two uv variables, not a
    two-entry one: uv needs PATH and the proxy variables to reach the index.
    """
    wheel_deploy(wheel_dir=_wheelhouse(tmp_path), url="https://x/", username="u", password="s3cret")

    argv, kwargs = _publish_call(mock_run)
    assert not any("s3cret" in arg for arg in argv)
    assert kwargs["env"]["UV_PUBLISH_PASSWORD"] == "s3cret"
    assert kwargs["env"]["XMS_TEST_MARKER"] == "inherited"


@patch_env({"UV_PUBLISH_TOKEN": "stale-pypi-token", "UV_PUBLISH_URL": "https://upload.pypi.org/legacy/"})
@patch("xmsconan.ci_tools.wheel_deploy.find_uv_bin", return_value=UV)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_drops_inherited_uv_publish_settings(mock_run, mock_uv, tmp_path):
    """An exported UV_PUBLISH_* does not reach the child.

    uv publish refuses a token next to a username, so a developer's PyPI
    token would otherwise turn the devpi upload into a usage error.
    """
    wheel_deploy(wheel_dir=_wheelhouse(tmp_path), url="https://x/", username="u", password="p")

    _, kwargs = _publish_call(mock_run)
    assert "UV_PUBLISH_TOKEN" not in kwargs["env"]
    assert "UV_PUBLISH_URL" not in kwargs["env"]
    assert kwargs["env"]["UV_PUBLISH_USERNAME"] == "u"


@patch_env(
    {
        "AQUAPI_URL": "https://env.example.com/",
        "AQUAPI_USERNAME": "envuser",
        "AQUAPI_PASSWORD": "envpass",
    }
)
@patch("xmsconan.ci_tools.wheel_deploy.find_uv_bin", return_value=UV)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_from_env_vars(mock_run, mock_uv, tmp_path):
    """Falls back to environment variables."""
    wheel_deploy(wheel_dir=_wheelhouse(tmp_path))

    argv, kwargs = _publish_call(mock_run)
    assert argv[2:4] == ["--publish-url", "https://env.example.com/"]
    assert kwargs["env"]["UV_PUBLISH_USERNAME"] == "envuser"
    assert kwargs["env"]["UV_PUBLISH_PASSWORD"] == "envpass"


@patch_env(clear=True)
@patch(
    "xmsconan.ci_tools.wheel_deploy.load_credentials",
    return_value={
        "url": "https://cfg.example.com/",
        "username": "cfguser",
        "password": "cfgpass",
    },
)
@patch("xmsconan.ci_tools.wheel_deploy.find_uv_bin", return_value=UV)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_deploy_from_config_file(mock_run, mock_uv, mock_creds, tmp_path):
    """Falls back to ~/.xmsconan.toml when no args or env vars."""
    wheel_deploy(wheel_dir=_wheelhouse(tmp_path))

    argv, kwargs = _publish_call(mock_run)
    assert argv[2:4] == ["--publish-url", "https://cfg.example.com/"]
    assert kwargs["env"]["UV_PUBLISH_USERNAME"] == "cfguser"
    assert kwargs["env"]["UV_PUBLISH_PASSWORD"] == "cfgpass"


@pytest.mark.parametrize(("present", "message"), [
    pytest.param({}, "No devpi URL", id="url"),
    pytest.param({"AQUAPI_URL": "https://x/"}, "No devpi username", id="username"),
    pytest.param(
        {"AQUAPI_URL": "https://x/", "AQUAPI_USERNAME": "u"},
        # Names the flag that supplies it, which is --password-file and not
        # the --password this entry point refuses.
        r"No devpi password provided \(--password-file",
        id="password",
    ),
])
def test_missing_credential_raises(present, message, monkeypatch):
    """Each missing credential is its own WheelDeployError naming how to supply it.

    Parametrized over which credentials are present rather than written three
    times: the cases differ only in that, and the id says which one is under
    test when one fails.
    """
    for name in ("AQUAPI_URL", "AQUAPI_USERNAME", "AQUAPI_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in present.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(WheelDeployError, match=message):
        wheel_deploy()


@pytest.mark.parametrize("key", ["url", "username", "password"])
@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_non_string_credential_from_the_config_file_is_a_usage_error(mock_run, key, tmp_path):
    """An unquoted TOML value is reported, not left to fail inside subprocess.

    ``password = 12345678`` in ~/.xmsconan.toml decodes as an int, and only
    the [aquapi] table itself is type-checked on the way out. Without this
    guard the int reaches ``subprocess.run``'s env and raises TypeError from
    inside the upload -- past every handler in ``main``, so the operator gets
    a traceback where every other unusable-config shape gives one line.
    """
    config = {"url": "https://x/", "username": "u", "password": "p"}
    config[key] = 12345678

    with patch("xmsconan.ci_tools.wheel_deploy.load_credentials", return_value=config):
        with pytest.raises(WheelDeployError, match=f"The {key} is int, not a string"):
            wheel_deploy(wheel_dir=_wheelhouse(tmp_path))

    mock_run.assert_not_called()


@patch_env(clear=True)
def test_a_refused_credential_type_is_named_but_never_printed(tmp_path):
    """The message names the type and not the value, whichever credential it is.

    All three land in a terminal or a job log, and the password is not the
    only one worth keeping out of it -- an index URL can carry a token.
    """
    config = {"url": "https://x/", "username": "u", "password": 12345678}

    with patch("xmsconan.ci_tools.wheel_deploy.load_credentials", return_value=config):
        with pytest.raises(WheelDeployError) as exc_info:
            wheel_deploy(wheel_dir=_wheelhouse(tmp_path))

    assert "12345678" not in str(exc_info.value)


def test_wheel_deploy_error_is_a_value_error():
    """Callers that caught ValueError before the class existed still do."""
    assert issubclass(WheelDeployError, ValueError)


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_empty_wheel_dir_raises(mock_run, tmp_path):
    """Nothing to upload is an error: uv publish given no files would fall back to dist/*."""
    with pytest.raises(WheelDeployError, match="No wheels to upload"):
        wheel_deploy(wheel_dir=str(tmp_path), url="https://x/", username="u", password="p")

    mock_run.assert_not_called()


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_missing_wheel_dir_is_not_reported_as_empty(mock_run, tmp_path):
    """A wheelhouse that was never created is its own fault.

    An artifact download that did not run and a build that produced no wheel
    are different problems; answering the first with "no wheels to upload"
    sends whoever reads the job log to the wrong one.
    """
    with pytest.raises(WheelDeployError, match="Wheel directory does not exist"):
        wheel_deploy(wheel_dir=str(tmp_path / "never-downloaded"), url="https://x/",
                     username="u", password="p")

    mock_run.assert_not_called()


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.find_uv_bin", return_value=UV)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_wheel_dir_holding_glob_metacharacters_is_read(mock_run, mock_uv, tmp_path):
    """``[`` in the directory name is part of the path, not a pattern to match with.

    Under ``glob.glob`` such a wheelhouse matched nothing and was reported as
    empty, which is the one failure a deploy job cannot tell from success.
    """
    wheel_dir = _wheelhouse(tmp_path / "build [1]", "a-1.0-py3-none-any.whl")

    wheel_deploy(wheel_dir=wheel_dir, url="https://x/", username="u", password="p")

    argv, _ = _publish_call(mock_run)
    assert argv[-1] == str(Path(wheel_dir) / "a-1.0-py3-none-any.whl")


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_unknown_client_raises(mock_run, tmp_path):
    """A client outside UPLOAD_CLIENTS is refused before anything runs."""
    with pytest.raises(ValueError, match="Unknown upload client 'twine'"):
        wheel_deploy(wheel_dir=_wheelhouse(tmp_path), url="https://x/", username="u", password="p",
                     client="twine")

    mock_run.assert_not_called()


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run")
def test_devpi_client_keeps_the_previous_sequence(mock_run, tmp_path, capsys):
    """--client devpi is the one-release fallback: the old three calls, and a warning."""
    wheel_dir = _wheelhouse(tmp_path)

    wheel_deploy(wheel_dir=wheel_dir, url="https://example.com/dev/", username="user", password="pass",
                 client="devpi")

    # In sequence, not assert_any_call: `use` selects the index `login`
    # authenticates against and `upload` writes to, so the order is the
    # behavior, and three order-free assertions would pass on any permutation.
    assert mock_run.mock_calls == [
        call(["devpi", "use", "https://example.com/dev/"], check=True),
        call(["devpi", "login", "user", "--password", "pass"], check=True),
        call(["devpi", "upload", "--from-dir", wheel_dir], check=True),
    ]
    assert "--client devpi" in capsys.readouterr().err


@patch_env(clear=True)
@patch("xmsconan.ci_tools.wheel_deploy.subprocess.run", side_effect=subprocess.CalledProcessError(1, "uv"))
def test_propagates_called_process_error(mock_run, tmp_path):
    """Verify CalledProcessError propagates to caller."""
    with pytest.raises(subprocess.CalledProcessError):
        wheel_deploy(wheel_dir=_wheelhouse(tmp_path), url="https://x/", username="u", password="p")


# --- CLI ---


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy")
def test_main_has_no_password_flag(mock_deploy, monkeypatch, capsys):
    """--password is refused by name, before anything runs, and the reply names --password-file."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_wheel_deploy", "--password", "secret"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    mock_deploy.assert_not_called()
    assert "--password-file" in capsys.readouterr().err


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy")
def test_main_reads_the_password_file(mock_deploy, monkeypatch, tmp_path):
    """--password-file hands the file's content to wheel_deploy; the client defaults to uv."""
    password_file = tmp_path / "aquapi-password"
    password_file.write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_wheel_deploy", "--wheel-dir", "wh", "--password-file", str(password_file),
    ])

    main()

    mock_deploy.assert_called_once_with(
        wheel_dir="wh", url=None, username=None, password="s3cret", client="uv",
    )


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy")
def test_main_reports_an_unusable_password_file(mock_deploy, monkeypatch, tmp_path, capsys):
    """A --password-file that does not exist is a usage error, not a fall-through or a traceback."""
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_wheel_deploy", "--password-file", str(tmp_path / "missing"),
    ])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    mock_deploy.assert_not_called()
    assert "password file not found" in capsys.readouterr().err


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy")
def test_main_passes_the_client_through(mock_deploy, monkeypatch):
    """--client devpi reaches wheel_deploy."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_wheel_deploy", "--client", "devpi"])

    main()

    assert mock_deploy.call_args.kwargs["client"] == "devpi"


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy",
       side_effect=WheelDeployError("No wheels to upload in wh"))
def test_main_reports_a_deploy_error_as_usage(mock_deploy, monkeypatch, capsys):
    """A missing credential or an empty wheelhouse is one line and exit 2, not a traceback."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_wheel_deploy", "--wheel-dir", "wh"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "No wheels to upload in wh" in capsys.readouterr().err


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy", side_effect=ValueError("boom from inside the upload"))
def test_main_does_not_absorb_an_unrelated_value_error(mock_deploy, monkeypatch):
    """Only the usage-error classes are usage errors; anything else keeps its traceback.

    Same rule as conan-setup: a bare `except ValueError` would relabel a
    fault from inside the upload as an argparse usage error.
    """
    monkeypatch.setattr(sys, "argv", ["xmsconan_wheel_deploy"])

    with pytest.raises(ValueError, match="boom from inside the upload"):
        main()


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy",
       side_effect=FileNotFoundError(2, "No such file or directory", "devpi"))
def test_main_reports_a_missing_upload_tool(mock_deploy, monkeypatch, capsys):
    """An upload tool that cannot be started is one line and exit 2."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_wheel_deploy", "--client", "devpi"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "could not start the upload tool" in capsys.readouterr().err


@patch("xmsconan.ci_tools.wheel_deploy.wheel_deploy", side_effect=subprocess.CalledProcessError(3, "uv"))
def test_main_exits_with_the_upload_tools_code(mock_deploy, monkeypatch):
    """A failed upload propagates the tool's own exit code."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_wheel_deploy"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 3


# --- documentation drift ---


def test_docs_describe_the_uv_publish_upload():
    """USAGE section 13 and the README describe the upload the code performs."""
    section = usage_section(13)
    assert "uv publish" in section
    assert "UV_PUBLISH_USERNAME" in section
    assert "UV_PUBLISH_PASSWORD" in section
    assert "--password-file" in section
    assert "--client devpi" in section
    assert "find_uv_bin" in section

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    wheel_deploy_section = slice_between(
        readme, "#### Wheel Deploy", "#### Conan Deploy", "README.md",
    )
    assert "uv publish" in wheel_deploy_section
    assert "--password-file" in wheel_deploy_section
