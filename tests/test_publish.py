"""Tests for ci_tools.publish."""
import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from xmsconan.ci_tools.publish import (
    main,
    publish,
    PublishSteps,
)
from xmsconan.exit_codes import EXIT_ERROR, EXIT_OK
from xmsconan.generator_tools.version import FALLBACK_VERSION


# --- fixtures ---


@pytest.fixture()
def mock_steps():
    """Return PublishSteps with MagicMock callables and xvfb disabled."""
    return PublishSteps(
        conan_setup=MagicMock(),
        generate=MagicMock(return_value=EXIT_OK),
        subprocess_run=MagicMock(),
        wheel_repair=MagicMock(),
        wheel_deploy=MagicMock(),
        conan_deploy=MagicMock(),
        check_xvfb=lambda _config: False,
    )


# --- publish ---


def test_publish_rejects_a_build_toml_without_library_name(mock_steps, tmp_path):
    """The reader names the file and the missing key."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('description = "desc"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="does not define library_name"):
        publish(version="7.0.0", toml_path=toml_file, steps=mock_steps)


def test_publish_rejects_an_unknown_top_level_key(mock_steps, tmp_path):
    """Publish validates the same way gen/ci/profiles do, so a typo fails here first."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\nhas_test_files = true\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"unknown top-level key\(s\) has_test_files"):
        publish(version="7.0.0", toml_path=toml_file, steps=mock_steps)


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
    mock_steps.generate.assert_called_once_with(toml_file_path=str(toml_file), version="7.0.0")
    # build.py is the one child process; the build files are generated in-process
    assert mock_steps.subprocess_run.call_count == 1
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


def _write_publish_toml(tmp_path, **ci_keys):
    """Write a minimal build.toml, with any given keys under ``[ci]``."""
    lines = ['library_name = "xmscore"']
    if ci_keys:
        lines.append("[ci]")
        lines += [
            f"{key} = {'true' if value else 'false'}" for key, value in ci_keys.items()
        ]
    toml_file = tmp_path / "build.toml"
    toml_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml_file


def _build_argv(mock_steps):
    """Return the argv of the build.py call, the only subprocess_run call."""
    return mock_steps.subprocess_run.call_args[0][0]


@patch("xmsconan.ci_tools.publish.sys.platform", "win32")
def test_publish_skips_windows_repair_when_the_toml_opts_out(mock_steps, tmp_path):
    """On Windows, ci.windows_wheel_repair = false skips the repair step.

    Same reason the generated CI skips it: delvewheel vendors a mangled
    msvcp140 beside a .pyd that needs nothing vendored. The wheel still uploads,
    and the dependency-libs staging that only feeds repair is skipped with it.
    """
    toml_file = _write_publish_toml(tmp_path, windows_wheel_repair=False)

    publish(version="7.0.0", toml_path=str(toml_file), steps=mock_steps)

    mock_steps.wheel_repair.assert_not_called()
    mock_steps.wheel_deploy.assert_called_once()
    assert "--skip-dependency-libs" in _build_argv(mock_steps)


@patch("xmsconan.ci_tools.publish.sys.platform", "linux")
def test_publish_still_repairs_on_linux_when_windows_repair_is_off(mock_steps, tmp_path):
    """The key is Windows-scoped: a manylinux wheel must still be repaired.

    And the libs it is repaired against must still be staged -- passing
    --skip-dependency-libs here would leave delvewheel/auditwheel resolving
    from the build box's own paths.
    """
    toml_file = _write_publish_toml(tmp_path, windows_wheel_repair=False)

    publish(version="7.0.0", toml_path=str(toml_file), steps=mock_steps)

    mock_steps.wheel_repair.assert_called_once_with(wheel_dir="wheelhouse")
    assert "--skip-dependency-libs" not in _build_argv(mock_steps)


@patch("xmsconan.ci_tools.publish.sys.platform", "win32")
def test_publish_repairs_on_windows_by_default(mock_steps, tmp_path):
    """Omitting the key keeps the historical behavior, staging included."""
    toml_file = _write_publish_toml(tmp_path)

    publish(version="7.0.0", toml_path=str(toml_file), steps=mock_steps)

    mock_steps.wheel_repair.assert_called_once_with(wheel_dir="wheelhouse")
    assert "--skip-dependency-libs" not in _build_argv(mock_steps)


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

    cmd = _build_argv(mock_steps)
    assert "--filter" in cmd
    assert '{"build_type": "Release"}' in cmd


def test_publish_build_failure_stops(tmp_path):
    """Verify CalledProcessError from build.py propagates."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    steps = PublishSteps(
        conan_setup=MagicMock(),
        generate=MagicMock(return_value=EXIT_OK),
        subprocess_run=MagicMock(
            side_effect=subprocess.CalledProcessError(1, "build.py"),
        ),
        wheel_repair=MagicMock(),
        wheel_deploy=MagicMock(),
        conan_deploy=MagicMock(),
        check_xvfb=lambda _config: False,
    )

    with pytest.raises(subprocess.CalledProcessError):
        publish(
            version="7.0.0",
            toml_path=str(toml_file),
            deploy_wheel=False,
            deploy_conan=False,
            steps=steps,
        )


def test_publish_stops_when_generation_fails(mock_steps, tmp_path):
    """A generator that fails ends the run with its exit code, before anything is built.

    The generator has already logged why; building from files it did not
    finish writing would only bury that message under a compiler's.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")
    mock_steps.generate.return_value = EXIT_ERROR

    with pytest.raises(SystemExit) as excinfo:
        publish(version="7.0.0", toml_path=str(toml_file), steps=mock_steps)

    assert excinfo.value.code == EXIT_ERROR
    mock_steps.subprocess_run.assert_not_called()
    mock_steps.wheel_deploy.assert_not_called()
    mock_steps.conan_deploy.assert_not_called()


@pytest.mark.parametrize("error", [
    pytest.param(ValueError("Missing field in build.toml: 'description'."), id="rejected-build-toml"),
    pytest.param(PermissionError("[Errno 13] Permission denied: 'conanfile.py'"), id="unwritable-file"),
])
def test_publish_reports_a_generator_exception_in_one_line(error, mock_steps, tmp_path, caplog):
    """A build.toml the generator rejects, or a file it cannot write, is one line and exit 1.

    That is how ``xmsconan gen`` reported it when this step launched it, and
    how every command wrapped in ``run_main`` reports it. ``publish.main`` is
    not wrapped, so without the catch the run would end in a traceback.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")
    mock_steps.generate.side_effect = error
    caplog.set_level(logging.INFO)

    with pytest.raises(SystemExit) as excinfo:
        publish(version="7.0.0", toml_path=str(toml_file), steps=mock_steps)

    assert excinfo.value.code == EXIT_ERROR
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == str(error)
    assert not record.exc_info  # no traceback attached

    mock_steps.subprocess_run.assert_not_called()


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
    # The resolved version reaches the generated build files and build.py
    mock_steps.generate.assert_called_once_with(toml_file_path=str(toml_file), version="8.1.0")
    assert "8.1.0" in _build_argv(mock_steps)


def test_publish_rejects_fallback_version(tmp_path):
    """Publish refuses to proceed when version resolves to fallback."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    with patch(
        "xmsconan.ci_tools.publish.resolve_version",
        return_value=FALLBACK_VERSION,
    ):
        with pytest.raises(SystemExit, match="must name a release"):
            publish(toml_path=str(toml_file))


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
        generate=MagicMock(return_value=EXIT_OK),
        subprocess_run=MagicMock(),
        wheel_repair=MagicMock(),
        wheel_deploy=MagicMock(),
        conan_deploy=MagicMock(),
        check_xvfb=lambda _config: True,
    )

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
        deploy_conan=False,
        steps=steps,
    )

    cmd = steps.subprocess_run.call_args[0][0]
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


@pytest.mark.parametrize("code", [
    "Error: 'docker' not found on PATH. Install Docker to use --docker.",
    2,
])
@patch("xmsconan.ci_tools.publish.publish")
@patch("sys.argv", ["xmsconan_publish", "--docker", "--version", "1.0.0"])
def test_main_docker_preserves_exit_code(mock_publish, code):
    """A SystemExit from docker_publish reaches the caller unchanged.

    docker_run raises SystemExit with a string when docker is missing from
    PATH and with the container's integer returncode otherwise. Both have to
    survive: an earlier wrapper replaced any non-int code with a bare 1, so
    the missing-docker message never reached the operator.
    """
    with patch("xmsconan.ci_tools.docker_run.docker_publish",
               side_effect=SystemExit(code)):
        with pytest.raises(SystemExit) as excinfo:
            main()

    assert excinfo.value.code == code
    mock_publish.assert_not_called()


@patch("xmsconan.ci_tools.publish.generate_build_files", return_value=EXIT_OK)
@patch("xmsconan.ci_tools.publish._check_xvfb", return_value=False)
@patch("xmsconan.ci_tools.publish._conan_deploy")
@patch("xmsconan.ci_tools.publish._wheel_deploy")
@patch("xmsconan.ci_tools.publish._wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish._conan_setup")
@patch("sys.argv", ["xmsconan_publish", "--version", "1.0.0", "--no-deploy"])
def test_main_without_docker_runs_publish(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy,
    mock_xvfb, mock_generate, tmp_path,
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
    mock_generate.assert_called_once_with(toml_file_path="build.toml", version="1.0.0")
