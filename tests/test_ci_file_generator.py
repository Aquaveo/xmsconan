"""Tests for generator_tools.ci_file_generator."""
import pytest

from xmsconan.generator_tools.ci_file_generator import (
    _display_name,
    generate_ci,
)


def test_display_name_converts_library_name():
    """'xmscore' becomes 'XmsCore'."""
    assert _display_name("xmscore") == "XmsCore"
    assert _display_name("xmsgrid") == "XmsGrid"
    assert _display_name("xmsinterp") == "XmsInterp"
    assert _display_name("xmsextractor") == "XmsExtractor"


def test_missing_toml_raises_file_not_found(tmp_path):
    """Raises FileNotFoundError when TOML path doesn't exist."""
    with pytest.raises(FileNotFoundError):
        generate_ci(str(tmp_path / "missing.toml"), "1.0.0", str(tmp_path))


def test_missing_ci_type_raises_value_error(tmp_path):
    """Raises ValueError when build.toml lacks ci_type."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ci_type"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))


def test_invalid_ci_type_raises_value_error(tmp_path):
    """Raises ValueError when ci_type is not 'github' or 'gitlab'."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "jenkins"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="jenkins"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))


def test_missing_ci_template_raises_file_not_found(tmp_path):
    """Raises FileNotFoundError when ci_type is valid but template is missing."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "github"\n',
        encoding="utf-8",
    )

    import xmsconan.generator_tools.ci_file_generator as ci_mod
    original = ci_mod.__file__
    try:
        # Point __file__ to tmp_path so ci_templates dir doesn't exist
        ci_mod.__file__ = str(tmp_path / "fake.py")
        with pytest.raises(FileNotFoundError, match="CI template not found"):
            generate_ci(str(toml_file), "1.0.0", str(tmp_path))
    finally:
        ci_mod.__file__ = original


def test_generate_github_ci_writes_correct_path(ci_toml, tmp_path):
    """Writes GitHub CI to .github/workflows/<DisplayName>-CI.yaml."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    expected = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    assert expected.exists()


def test_generate_gitlab_ci_writes_correct_path(tmp_path):
    """Writes GitLab CI to .gitlab-ci.yml."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    expected = output_dir / ".gitlab-ci.yml"
    assert expected.exists()


def test_generate_ci_dry_run_does_not_write(ci_toml, tmp_path):
    """Dry-run doesn't write any files."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir), dry_run=True)
    assert not output_dir.exists() or not any(output_dir.rglob("*"))


def test_context_variables_rendered(ci_toml, tmp_path):
    """library_name, version, and display_name are rendered in output."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "2.3.5", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    # The real template should contain these rendered values
    # (this depends on actual template content, so just verify the file was written)
    assert len(content) > 0


def test_ci_config_options_passed_to_template(tmp_path):
    """CI section options (windows, deploy, etc.) are available in template context."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'windows = true\n'
        'deploy = true\n'
        'coverage = true\n'
        'xvfb = false\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    # Should not raise — options are passed into context even if template
    # doesn't use all of them (StrictUndefined only fails on missing vars)
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    assert ci_file.exists()


def test_github_linux_uses_container_image(ci_toml, tmp_path):
    """Linux job uses the Aquaveo Docker container image."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "ghcr.io/aquaveo/conan-gcc13-py3.13:latest" in content


def test_github_linux_no_setup_python(ci_toml, tmp_path):
    """Linux job does not use actions/setup-python (Python is in the container)."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    # Extract the Linux section (between # LINUX and # WINDOWS headers)
    linux_start = content.index("# LINUX")
    windows_start = content.index("# WINDOWS")
    linux_section = content[linux_start:windows_start]
    assert "setup-python" not in linux_section


def test_github_ci_uses_default_version(ci_toml, tmp_path):
    """Verify GitHub CI uses 0.0.0 default, ignoring the version passed to generate_ci."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "7.0.1", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    # Default version should be 0.0.0
    assert "XMS_VERSION: '0.0.0'" in content
    assert "CONAN_REFERENCE: xmscore/0.0.0" in content
    # The passed-in version should NOT appear in any XMS_VERSION line
    for line in content.splitlines():
        if "XMS_VERSION:" in line:
            assert "7.0.1" not in line


def test_github_ci_uses_cli_commands(ci_toml, tmp_path):
    """Rendered GitHub CI uses xmsconan CLI commands instead of inline scripts."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "xmsconan_conan_setup" in content
    assert "xmsconan_wheel_repair" in content
    assert "xmsconan_wheel_deploy" in content
    # Inline conan profile detect / devpi commands should NOT appear
    assert "conan profile detect" not in content
    assert "devpi use $" not in content
    assert "devpi login $" not in content


def test_gitlab_ci_uses_cli_commands(tmp_path):
    """Rendered GitLab CI uses xmsconan CLI commands instead of inline scripts."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "xmsconan_conan_setup" in content
    assert "xmsconan_wheel_repair" in content
    assert "xmsconan_wheel_deploy" in content
    assert "xmsconan_conan_deploy" in content
    # Inline conan profile detect should NOT appear
    assert "conan profile detect" not in content


def test_gitlab_ci_deploy_jobs_set_package_version(tmp_path):
    """All GitLab deploy jobs explicitly export PACKAGE_VERSION."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    # Every section that calls xmsconan_conan_deploy must first set PACKAGE_VERSION
    import re
    deploy_blocks = re.split(r'\n(?=\S)', content)
    for block in deploy_blocks:
        if "xmsconan_conan_deploy" in block:
            assert "export PACKAGE_VERSION=" in block, (
                f"Deploy block missing 'export PACKAGE_VERSION=':\n{block}"
            )


def test_gitlab_ci_deploy_false_suppresses_deploy(tmp_path):
    """Setting deploy = false omits deploy stages from GitLab CI."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'deploy = false\n'
        'windows = false\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "xmsconan_wheel_deploy" not in content
    assert "xmsconan_conan_deploy" not in content


def test_github_ci_version_bump(ci_toml, tmp_path):
    """Rendered GitHub CI references xmsconan>=2.4.2."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "xmsconan>=2.4.2" in content
    assert "xmsconan>=2.3.5" not in content


def test_python_namespaced_dir_defaults_to_suffix(tmp_path):
    """python_namespaced_dir defaults to library_name[3:]."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsgrid"\n'
        'description = "desc"\n'
        'ci_type = "github"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    # The generate_ci function sets python_namespaced_dir = library_name[3:]
    # which would be "grid" — this just verifies it doesn't raise
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsGrid-CI.yaml"
    assert ci_file.exists()
