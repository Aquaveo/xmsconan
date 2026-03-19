"""Tests for ci_tools.publish."""
import subprocess
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.publish import _read_library_name, publish


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


@patch("xmsconan.ci_tools.publish.conan_deploy")
@patch("xmsconan.ci_tools.publish.wheel_deploy")
@patch("xmsconan.ci_tools.publish.wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish.conan_setup")
def test_publish_full_pipeline(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy, tmp_path,
):
    """Full publish runs all steps in order."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        url="https://x/",
        username="u",
        password="p",
    )

    mock_setup.assert_called_once()
    # xmsconan_gen + build.py = 2 subprocess.run calls
    assert mock_run.call_count == 2
    mock_repair.assert_called_once_with(wheel_dir="wheelhouse")
    mock_wdeploy.assert_called_once_with(
        wheel_dir="wheelhouse", url="https://x/", username="u", password="p",
    )
    mock_cdeploy.assert_called_once_with("xmscore", "7.0.0", upload=True)


@patch("xmsconan.ci_tools.publish.conan_deploy")
@patch("xmsconan.ci_tools.publish.wheel_deploy")
@patch("xmsconan.ci_tools.publish.wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish.conan_setup")
def test_publish_no_deploy(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy, tmp_path,
):
    """deploy_wheel=False and deploy_conan=False skips uploads."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
        deploy_conan=False,
    )

    mock_setup.assert_called_once()
    mock_repair.assert_called_once()
    mock_wdeploy.assert_not_called()
    mock_cdeploy.assert_not_called()


@patch("xmsconan.ci_tools.publish.conan_deploy")
@patch("xmsconan.ci_tools.publish.wheel_deploy")
@patch("xmsconan.ci_tools.publish.wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish.conan_setup")
def test_publish_no_wheel(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy, tmp_path,
):
    """deploy_wheel=False skips wheel upload but keeps conan."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        deploy_wheel=False,
    )

    mock_wdeploy.assert_not_called()
    mock_cdeploy.assert_called_once()


@patch("xmsconan.ci_tools.publish.conan_deploy")
@patch("xmsconan.ci_tools.publish.wheel_deploy")
@patch("xmsconan.ci_tools.publish.wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish.conan_setup")
def test_publish_no_conan(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy, tmp_path,
):
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
    )

    mock_wdeploy.assert_called_once()
    mock_cdeploy.assert_not_called()


@patch("xmsconan.ci_tools.publish.conan_deploy")
@patch("xmsconan.ci_tools.publish.wheel_deploy")
@patch("xmsconan.ci_tools.publish.wheel_repair")
@patch("xmsconan.ci_tools.publish.subprocess.run")
@patch("xmsconan.ci_tools.publish.conan_setup")
def test_publish_with_filter(
    mock_setup, mock_run, mock_repair, mock_wdeploy, mock_cdeploy, tmp_path,
):
    """build_filter is passed through to build.py."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    publish(
        version="7.0.0",
        toml_path=str(toml_file),
        build_filter='{"build_type": "Release"}',
        deploy_wheel=False,
        deploy_conan=False,
    )

    # The second subprocess.run call is build.py
    build_call = mock_run.call_args_list[1]
    cmd = build_call[0][0]
    assert "--filter" in cmd
    assert '{"build_type": "Release"}' in cmd


@patch("xmsconan.ci_tools.publish.conan_setup")
def test_publish_build_failure_stops(mock_setup, tmp_path):
    """Verify CalledProcessError from build.py propagates."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text('library_name = "xmscore"\n', encoding="utf-8")

    with patch(
        "xmsconan.ci_tools.publish.subprocess.run",
        side_effect=[None, subprocess.CalledProcessError(1, "build.py")],
    ):
        with pytest.raises(subprocess.CalledProcessError):
            publish(
                version="7.0.0",
                toml_path=str(toml_file),
                deploy_wheel=False,
                deploy_conan=False,
            )
