"""Tests for ci_tools.docker_run."""
import argparse
import shlex
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.docker_run import (
    _build_env_flags,
    _build_publish_args,
    DOCKER_IMAGE_BASE,
    DOCKER_IMAGE_XVFB,
    docker_publish,
    DOCKER_REGISTRY,
    resolve_docker_image,
)
from .utils import patch_env


# --- resolve_docker_image ---


def test_resolve_image_explicit_override(tmp_path):
    """Explicit --docker-image takes precedence."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n[ci]\nxvfb = true\n',
        encoding="utf-8",
    )
    assert resolve_docker_image("my-image", str(toml_file)) == "my-image"


def test_resolve_image_xvfb_true(tmp_path):
    """ci.xvfb=true selects the X11/GDAL image."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n[ci]\nxvfb = true\n',
        encoding="utf-8",
    )
    expected = f"{DOCKER_REGISTRY}/{DOCKER_IMAGE_XVFB}"
    assert resolve_docker_image(toml_path=str(toml_file)) == expected


def test_resolve_image_xvfb_false(tmp_path):
    """ci.xvfb=false (or missing) selects the base image."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")
    expected = f"{DOCKER_REGISTRY}/{DOCKER_IMAGE_BASE}"
    assert resolve_docker_image(toml_path=str(toml_file)) == expected


# --- _build_env_flags ---


@patch_env({
    "AQUAPI_URL": "https://x/",
    "AQUAPI_USERNAME": "user",
    "AQUAPI_PASSWORD": "pass",
})
def test_build_env_flags_forwards_set_vars_by_name():
    """Forwards the names of the AQUAPI_* vars that are set, not their values.

    `docker run -e VAR` makes the docker client read VAR from its own
    environment, which subprocess.run passes through, so the container sees
    the same value -- without the password sitting in the docker process's
    argv where `ps` and process-creation auditing read it.
    """
    flags = _build_env_flags()

    assert flags == [
        "-e", "AQUAPI_URL",
        "-e", "AQUAPI_USERNAME",
        "-e", "AQUAPI_PASSWORD",
    ]
    assert not any("=" in flag for flag in flags)
    assert "pass" not in " ".join(flags)


@patch_env({
    "AQUAPI_URL": "https://x/",
    "AQUAPI_PASSWORD": "",
}, clear=True)
def test_build_env_flags_skips_an_empty_var():
    """An empty var is not forwarded at all.

    `-e AQUAPI_PASSWORD` for an empty value would set it *empty* inside the
    container, which reads as "configured, and configured wrong" rather than
    leaving the container's own credential resolution to fall through.
    """
    assert _build_env_flags() == ["-e", "AQUAPI_URL"]


@patch_env(clear=True)
def test_build_env_flags_skips_unset_vars():
    """Produces no flags when env vars are not set."""
    assert _build_env_flags() == []


# --- _build_publish_args ---


def test_build_publish_args_all_flags():
    """All publish flags are reconstructed."""
    args = argparse.Namespace(
        version="1.0.0",
        wheel_dir="wh",
        toml="custom.toml",
        build_filter='{"build_type": "Release"}',
        no_deploy=True,
        no_wheel=True,
        no_conan=True,
        verbose=0,
        quiet=False,
    )
    result = _build_publish_args(args)
    assert "--version" in result
    assert "1.0.0" in result
    assert "--wheel-dir" in result
    assert "--toml" in result
    assert "--filter" in result
    assert "--no-deploy" in result
    assert "--no-wheel" in result
    assert "--no-conan" in result


def test_build_publish_args_defaults_only():
    """Only version is included when everything else is default."""
    args = argparse.Namespace(
        version="2.0.0",
        wheel_dir="wheelhouse",
        toml="build.toml",
        build_filter=None,
        no_deploy=False,
        no_wheel=False,
        no_conan=False,
        verbose=0,
        quiet=False,
    )
    result = _build_publish_args(args)
    assert result == ["--version", "2.0.0"]


def test_build_publish_args_forwards_the_verbosity_flags():
    """-vv and -q reach the publish that runs inside the container, where the work is."""
    args = argparse.Namespace(
        version="2.0.0",
        wheel_dir="wheelhouse",
        toml="build.toml",
        build_filter=None,
        no_deploy=False,
        no_wheel=False,
        no_conan=False,
        verbose=2,
        quiet=True,
    )
    result = _build_publish_args(args)
    assert result == ["--version", "2.0.0", "-v", "-v", "-q"]


# --- docker_publish ---


@patch("xmsconan.ci_tools.docker_run.shutil.which", return_value=None)
def test_docker_publish_missing_docker(mock_which):
    """Raises SystemExit when docker is not on PATH."""
    args = argparse.Namespace(docker_image=None, toml="build.toml")
    with pytest.raises(SystemExit, match="docker.*not found"):
        docker_publish(args)


@patch("xmsconan.ci_tools.docker_run.subprocess.run")
@patch("xmsconan.ci_tools.docker_run._build_config_mount", return_value=[])
@patch("xmsconan.ci_tools.docker_run.shutil.which", return_value="/usr/bin/docker")
def test_docker_publish_builds_correct_command(
    mock_which, mock_config, mock_run, tmp_path,
):
    """Verify the docker run command is constructed correctly."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    mock_run.return_value = argparse.Namespace(returncode=0)

    args = argparse.Namespace(
        version="1.0.0",
        wheel_dir="wheelhouse",
        toml=str(toml_file),
        build_filter=None,
        no_deploy=True,
        no_wheel=False,
        no_conan=False,
        verbose=0,
        quiet=False,
        docker_image=None,
        xmsconan_dir=None,
    )

    with patch_env(clear=True):
        docker_publish(args)

    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "docker"
    assert "run" in cmd
    assert "--rm" in cmd
    assert "/workspace" in " ".join(cmd)
    assert "bash" in cmd
    assert "--no-deploy" in " ".join(cmd)


@patch("xmsconan.ci_tools.docker_run.subprocess.run")
@patch("xmsconan.ci_tools.docker_run._build_config_mount", return_value=[])
@patch("xmsconan.ci_tools.docker_run.shutil.which", return_value="/usr/bin/docker")
def test_docker_publish_quotes_the_filter(mock_which, mock_config, mock_run, tmp_path):
    """--filter survives the container's shell as one argument.

    The inner command is a string handed to `bash -c`, so a JSON filter joined
    with plain spaces reached argparse inside the container as the two words
    {"build_type": and "Release"} -- making the documented
    `xmsconan publish --docker --filter '{...}'` fail every time.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    mock_run.return_value = argparse.Namespace(returncode=0)

    build_filter = '{"build_type": "Release"}'
    args = argparse.Namespace(
        version="1.0.0",
        wheel_dir="wheelhouse",
        toml=str(toml_file),
        build_filter=build_filter,
        no_deploy=False,
        no_wheel=False,
        no_conan=False,
        verbose=0,
        quiet=False,
        docker_image=None,
        xmsconan_dir=None,
    )

    with patch_env(clear=True):
        docker_publish(args)

    cmd = mock_run.call_args[0][0]
    # ... image bash -c <script>: the script is the argument after -c.
    inner_script = cmd[cmd.index("-c", cmd.index("bash")) + 1]
    _install, _sep, publish_cmd = inner_script.partition(" && ")
    assert shlex.split(publish_cmd) == [
        "xmsconan", "publish",
        "--version", "1.0.0",
        "--toml", str(toml_file),
        "--filter", build_filter,
    ]


@patch("xmsconan.ci_tools.docker_run.subprocess.run")
@patch("xmsconan.ci_tools.docker_run._build_config_mount", return_value=[])
@patch("xmsconan.ci_tools.docker_run.shutil.which", return_value="/usr/bin/docker")
def test_docker_publish_mounts_xmsconan_dir(
    mock_which, mock_config, mock_run, tmp_path,
):
    """--xmsconan-dir mounts local source and installs from it."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    mock_run.return_value = argparse.Namespace(returncode=0)

    args = argparse.Namespace(
        version="1.0.0",
        wheel_dir="wheelhouse",
        toml=str(toml_file),
        build_filter=None,
        no_deploy=False,
        no_wheel=False,
        no_conan=False,
        verbose=0,
        quiet=False,
        docker_image=None,
        xmsconan_dir="/home/user/xmsconan",
    )

    with patch_env(clear=True):
        docker_publish(args)

    cmd = mock_run.call_args[0][0]
    cmd_str = " ".join(cmd)
    assert "/xmsconan" in cmd_str
    assert "pip install --force-reinstall /xmsconan" in cmd_str


@patch("xmsconan.ci_tools.docker_run.subprocess.run")
@patch("xmsconan.ci_tools.docker_run._build_config_mount", return_value=[])
@patch("xmsconan.ci_tools.docker_run.shutil.which", return_value="/usr/bin/docker")
def test_docker_publish_propagates_exit_code(
    mock_which, mock_config, mock_run, tmp_path,
):
    """Non-zero exit from docker run propagates."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    mock_run.return_value = argparse.Namespace(returncode=42)

    args = argparse.Namespace(
        version="1.0.0",
        wheel_dir="wheelhouse",
        toml=str(toml_file),
        build_filter=None,
        no_deploy=False,
        no_wheel=False,
        no_conan=False,
        verbose=0,
        quiet=False,
        docker_image=None,
        xmsconan_dir=None,
    )

    with patch_env(clear=True):
        with pytest.raises(SystemExit) as exc_info:
            docker_publish(args)
    assert exc_info.value.code == 42


def test_resolve_image_rejects_an_unknown_top_level_key(tmp_path):
    """--docker reads build.toml before the container starts; a typo should fail here."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\nhas_test_files = true\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"unknown top-level key\(s\) has_test_files"):
        resolve_docker_image(toml_path=toml_file)
