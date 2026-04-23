"""Tests for ci_tools.publish."""
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.ci_tools.publish import (
    _check_xvfb,
    _read_library_name,
    main,
    publish,
    PublishSteps,
)
from xmsconan.generator_tools.version import FALLBACK_VERSION
from .utils import patch_env


# --- fixtures ---


@pytest.fixture()
def mock_steps():
    """Return PublishSteps with MagicMock callables and xvfb disabled."""
    return PublishSteps(
        conan_setup=MagicMock(),
        subprocess_run=MagicMock(),
        wheel_repair=MagicMock(),
        wheel_deploy=MagicMock(),
        conan_deploy=MagicMock(),
        check_xvfb=lambda _toml_path="build.toml": False,
    )


# --- _read_library_name ---


def test_read_library_name(tmp_path):
    """Reads library_name from build.toml."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")
    assert _read_library_name(str(toml_file)) == "xmscore"


def test_read_library_name_missing_key(tmp_path):
    """Raises ValueError when library_name is missing."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('description = "desc"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="No library_name"):
        _read_library_name(str(toml_file))


# --- publish ---


def test_publish_full_pipeline(mock_steps, tmp_path):
    """Full publish runs all steps in order."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        url="https://x/",
        username="u",
        password="p",
        steps=mock_steps,
    )

    mock_steps.conan_setup.assert_called_once_with(login=True)
    # xmsconan_gen + build.py = 2 subprocess.run calls
    assert mock_steps.subprocess_run.call_count == 2
    mock_steps.wheel_repair.assert_called_once_with(wheel_dir="wheelhouse")
    mock_steps.wheel_deploy.assert_called_once_with(
        wheel_dir="wheelhouse", url="https://x/", username="u", password="p",
    )
    mock_steps.conan_deploy.assert_called_once_with("xmscore", "7.0.0", upload=True)


def test_publish_no_deploy(mock_steps, tmp_path):
    """deploy_wheel=False and deploy_conan=False skips uploads."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
        deploy_conan=False,
        steps=mock_steps,
    )

    mock_steps.conan_setup.assert_called_once()
    mock_steps.wheel_repair.assert_called_once()
    mock_steps.wheel_deploy.assert_not_called()
    mock_steps.conan_deploy.assert_not_called()


def test_publish_no_wheel(mock_steps, tmp_path):
    """deploy_wheel=False skips wheel upload but keeps conan."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
        steps=mock_steps,
    )

    mock_steps.wheel_deploy.assert_not_called()
    mock_steps.conan_deploy.assert_called_once()


def test_publish_no_conan(mock_steps, tmp_path):
    """deploy_conan=False skips conan upload but keeps wheel."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_conan=False,
        url="https://x/",
        username="u",
        password="p",
        steps=mock_steps,
    )

    mock_steps.wheel_deploy.assert_called_once()
    mock_steps.conan_deploy.assert_not_called()


def test_publish_with_filter(mock_steps, tmp_path):
    """build_filter is passed through to build.py."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        build_filter='{"build_type": "Release"}',
        deploy_wheel=False,
        deploy_conan=False,
        steps=mock_steps,
    )

    # The second subprocess_run call is build.py
    build_call = mock_steps.subprocess_run.call_args_list[1]
    cmd = build_call[0][0]
    assert "--filter" in cmd
    assert '{"build_type": "Release"}' in cmd


def test_publish_build_failure_stops(tmp_path):
    """Verify CalledProcessError from build.py propagates."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    steps = PublishSteps(
        conan_setup=MagicMock(),
        subprocess_run=MagicMock(
            side_effect=[None, subprocess.CalledProcessError(1, "build.py")],
        ),
        wheel_repair=MagicMock(),
        wheel_deploy=MagicMock(),
        conan_deploy=MagicMock(),
        check_xvfb=lambda _toml_path="build.toml": False,
    )

    with pytest.raises(subprocess.CalledProcessError):
        publish(
            version="7.0.0",
            toml_path=str(toml_file),
            deploy_wheel=False,
            deploy_conan=False,
            steps=steps,
        )


# --- version resolution ---


@patch("xmsconan.ci_tools.publish.resolve_version", return_value="8.1.0")
def test_publish_version_from_scm(mock_resolve, mock_steps, tmp_path):
    """Version resolved from setuptools-scm when --version omitted."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        toml_path=str(toml_file),
        deploy_wheel=False,
        deploy_conan=False,
        steps=mock_steps,
    )

    mock_resolve.assert_called_once_with(None)
    # Resolved version propagated to xmsconan_gen and build.py
    gen_call = mock_steps.subprocess_run.call_args_list[0][0][0]
    assert "8.1.0" in gen_call


def test_publish_rejects_fallback_version(tmp_path):
    """Publish refuses to proceed when version resolves to fallback."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    with patch(
        "xmsconan.ci_tools.publish.resolve_version",
        return_value=FALLBACK_VERSION,
    ):
        with pytest.raises(SystemExit, match="could not determine version"):
            publish(toml_path=str(toml_file))


# --- _check_xvfb ---


@patch("xmsconan.ci_tools.publish.shutil.which", return_value="/usr/bin/xvfb-run")
@patch("xmsconan.ci_tools.publish.sys.platform", "linux")
@patch_env(clear=True)
def test_check_xvfb_true_on_linux(mock_which, tmp_path):
    """Returns True on Linux when ci.xvfb=true and no DISPLAY."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n[ci]\nxvfb = true\n',
        encoding="utf-8",
    )
    assert _check_xvfb(str(toml_file)) is True


@patch("xmsconan.ci_tools.publish.sys.platform", "darwin")
def test_check_xvfb_false_on_macos(tmp_path):
    """Returns False on macOS."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n[ci]\nxvfb = true\n',
        encoding="utf-8",
    )
    assert _check_xvfb(str(toml_file)) is False


@patch("xmsconan.ci_tools.publish.sys.platform", "linux")
@patch_env({"DISPLAY": ":0"})
def test_check_xvfb_false_when_display_set(tmp_path):
    """Returns False when DISPLAY is already set."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n[ci]\nxvfb = true\n',
        encoding="utf-8",
    )
    assert _check_xvfb(str(toml_file)) is False


@patch("xmsconan.ci_tools.publish.sys.platform", "linux")
@patch_env(clear=True)
def test_check_xvfb_false_when_xvfb_not_configured(tmp_path):
    """Returns False when ci.xvfb is not set."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")
    assert _check_xvfb(str(toml_file)) is False


def test_publish_calls_conan_setup_with_login(mock_steps, tmp_path):
    """Publish calls conan_setup with login=True."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
        deploy_conan=False,
        steps=mock_steps,
    )

    mock_steps.conan_setup.assert_called_once_with(login=True)


def test_publish_wraps_build_with_xvfb_run(tmp_path):
    """Build command is prefixed with xvfb-run when xvfb is needed."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    steps = PublishSteps(
        conan_setup=MagicMock(),
        subprocess_run=MagicMock(),
        wheel_repair=MagicMock(),
        wheel_deploy=MagicMock(),
        conan_deploy=MagicMock(),
        check_xvfb=lambda _toml_path="build.toml": True,
    )

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
        deploy_conan=False,
        steps=steps,
    )

    # The second subprocess_run call is build.py
    build_call = steps.subprocess_run.call_args_list[1]
    cmd = build_call[0][0]
    assert cmd[0] == "xvfb-run"


# --- main() Docker dispatch ---


@patch("xmsconan.ci_tools.publish.publish")
@patch("sys.argv", ["xmsconan_publish", "--docker", "--version", "1.0.0"])
def test_main_docker_dispatches(mock_publish):
    """--docker dispatches to docker_publish, not publish()."""
    with patch("xmsconan.ci_tools.docker_run.docker_publish") as mock_docker:
        main()

    mock_docker.assert_called_once()
    mock_publish.assert_not_called()


@patch("xmsconan.ci_tools.publish._check_xvfb", return_value=False)
@patch("xmsconan.ci_tools.publish._conan_deploy")
@patch("xmsconan.ci_tools.publish._wheel_deploy")
@patch("xmsconan.ci_tools.publish._wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish._conan_setup")
@patch("sys.argv", ["xmsconan_publish", "--version", "1.0.0", "--no-deploy"])
def test_main_without_docker_runs_publish(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy,
    mock_xvfb, tmp_path,
):
    """Without --docker, main() calls publish() normally."""
    import os
    original_dir = os.getcwd()
    os.chdir(tmp_path)
    (tmp_path / "build.toml").write_text(
        'library_name = "xmscore"\n', encoding="utf-8",
    )
    try:
        main()
    finally:
        os.chdir(original_dir)

    mock_setup.assert_called_once_with(login=True)
