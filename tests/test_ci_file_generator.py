"""Tests for generator_tools.ci_file_generator."""
import logging

import pytest
import yaml

from xmsconan.constants import version_sort_key
from xmsconan.generator_tools.ci_file_generator import (
    _display_name,
    generate_ci,
)
from .ci_helpers import NON_JOB_SHAPE_KEYS, write_github_toml, write_gitlab_toml


@pytest.mark.parametrize("input_name,expected", [
    ("xmscore", "XmsCore"),
    ("xmsgrid", "XmsGrid"),
    ("xmsinterp", "XmsInterp"),
    ("xmsextractor", "XmsExtractor"),
])
def test_display_name_converts_library_name(input_name, expected):
    """Library name is converted to display format."""
    assert _display_name(input_name) == expected


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
    assert "XmsCore" in content
    assert "xmscore" in content


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


def _github_jobs(toml_file, tmp_path, name="XmsCore"):
    """Render a GitHub workflow and return its parsed ``jobs`` mapping.

    Every per-platform assertion below indexes the job it is about. Substring
    tallies over the whole document cannot name the platform that regressed,
    and several platforms emit similar-looking lines -- the Windows job alone
    hardcodes both ``-${{ matrix.build_type }}-py${{ matrix.python-version }}``
    and ``wheel-${{ runner.os }}-py${{ matrix.python-version }}`` -- so a
    document-wide check silently passes on the wrong job's output.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    workflow = output_dir / ".github" / "workflows" / f"{name}-CI.yaml"
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]


def _matrix_pythons(job):
    """The ``python-version`` axis of one job's matrix."""
    return job["strategy"]["matrix"]["python-version"]


def _upload_artifact_names(job):
    """Every ``upload-artifact`` name in one job, in step order."""
    return [
        step["with"]["name"]
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]


def test_github_linux_container_image_tracks_the_matrix_python(ci_toml, tmp_path):
    """The Linux container image is derived from the job's python-version leg."""
    jobs = _github_jobs(ci_toml, tmp_path)
    assert jobs["linux"]["container"]["image"] == (
        "ghcr.io/aquaveo/conan-gcc13-py${{ matrix.python-version }}:latest"
    )


def test_github_explicit_docker_image_overrides_the_derived_one(tmp_path):
    """[ci].docker_image replaces the image outright, python fan-out or not."""
    toml_file = write_github_toml(
        tmp_path,
        docker_image="ghcr.io/aquaveo/custom:latest",
        linux_python_versions=["3.13", "3.14"],
    )
    jobs = _github_jobs(toml_file, tmp_path)
    assert jobs["linux"]["container"]["image"] == "ghcr.io/aquaveo/custom:latest"
    assert "conan-gcc13-py" not in yaml.dump(jobs)


def test_github_default_python_matrix_is_3_13_on_every_platform(ci_toml, tmp_path):
    """Without [ci].python_versions every platform builds 3.13 alone."""
    jobs = _github_jobs(ci_toml, tmp_path)
    assert _matrix_pythons(jobs["flake"]) == ["3.13"]
    assert _matrix_pythons(jobs["mac"]) == ["3.13"]
    assert _matrix_pythons(jobs["linux"]) == ["3.13"]
    assert _matrix_pythons(jobs["windows"]) == ["3.13"]


@pytest.mark.parametrize("job_name", ["mac", "linux"])
def test_github_single_version_platform_keeps_unsuffixed_names(ci_toml, tmp_path, job_name):
    """A platform that does not fan out keeps the names it always published.

    Release assets and wheel artifacts are fetched by exact name, so the ABI
    suffix must stay off until there is more than one leg to disambiguate.
    """
    job = _github_jobs(ci_toml, tmp_path)[job_name]
    assert "py${{ matrix.python-version }}" not in job["env"]["MATRIX_NAME"]
    assert "py${{ matrix.python-version }}" not in job["name"]
    assert "wheel-${{ runner.os }}" in _upload_artifact_names(job)


def test_github_python_versions_opt_in_adds_3_10_only_on_windows(tmp_path):
    """[ci].python_versions = ["3.10", "3.13"] only expands the Windows matrix."""
    toml_file = write_github_toml(tmp_path, python_versions=["3.10", "3.13"])
    jobs = _github_jobs(toml_file, tmp_path)
    assert _matrix_pythons(jobs["windows"]) == ["3.10", "3.13"]
    # mac and linux fall back to the highest entry rather than inheriting 3.10,
    # which has no container image and no consumer outside Windows.
    assert _matrix_pythons(jobs["mac"]) == ["3.13"]
    assert _matrix_pythons(jobs["linux"]) == ["3.13"]


def test_github_mac_python_versions_fans_out_mac_only(tmp_path):
    """[ci].mac_python_versions expands mac and leaves linux on the default."""
    toml_file = write_github_toml(
        tmp_path,
        python_versions=["3.10", "3.13", "3.14"],
        mac_python_versions=["3.13", "3.14"],
    )
    jobs = _github_jobs(toml_file, tmp_path)
    assert _matrix_pythons(jobs["mac"]) == ["3.13", "3.14"]
    assert _matrix_pythons(jobs["linux"]) == ["3.14"]  # highest of the ci list
    # Mac fans out, so its own names carry the ABI; linux's stay bare.
    assert jobs["mac"]["env"]["MATRIX_NAME"].endswith("-py${{ matrix.python-version }}")
    assert jobs["linux"]["env"]["MATRIX_NAME"] == "linux-GCC13-${{ matrix.build_type }}"


def test_github_linux_python_versions_fans_out_containers(tmp_path):
    """[ci].linux_python_versions expands linux and linux-arm together."""
    toml_file = write_github_toml(
        tmp_path,
        linux_arm=True,
        python_versions=["3.10", "3.13", "3.14"],
        linux_python_versions=["3.13", "3.14"],
    )
    jobs = _github_jobs(toml_file, tmp_path)
    for job_name, prefix, wheel in (
        ("linux", "linux-GCC13", "wheel-${{ runner.os }}"),
        ("linux-arm", "linux-arm-GCC13", "wheel-${{ runner.os }}-arm64"),
    ):
        job = jobs[job_name]
        assert _matrix_pythons(job) == ["3.13", "3.14"]
        assert job["env"]["MATRIX_NAME"] == (
            f"{prefix}-" + "${{ matrix.build_type }}-py${{ matrix.python-version }}"
        )
        assert f"{wheel}-py" + "${{ matrix.python-version }}" in _upload_artifact_names(job)


@pytest.mark.parametrize("job_name", ["linux", "linux-arm"])
def test_github_fanned_out_linux_jobs_get_distinct_check_names(tmp_path, job_name):
    """Legs differing only by ABI must not share one status-check name.

    GitHub uses an explicit ``name:`` verbatim, so without the version two of
    the four legs would report under the same name -- ambiguous in the checks
    list and in branch-protection matching.
    """
    toml_file = write_github_toml(
        tmp_path, linux_arm=True, linux_python_versions=["3.13", "3.14"],
    )
    assert "${{ matrix.python-version }}" in _github_jobs(toml_file, tmp_path)[job_name]["name"]


def test_github_rejects_a_python_version_the_recipe_does_not_allow(tmp_path):
    """A version that generates but cannot build is caught here, not in CI."""
    toml_file = write_github_toml(tmp_path, python_versions=["3.13", "3.12"])
    with pytest.raises(ValueError, match="python_version option does not allow"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))


def test_gitlab_default_python_version_is_3_13_only(tmp_path):
    """Without [ci].python_versions GitLab references only 3.13."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert "PYTHON_TARGET_VERSION: '3.13'" in content
    assert "PYTHON_TARGET_VERSION: '3.10'" not in content


def test_gitlab_python_versions_opt_in_only_fans_out_windows(tmp_path):
    """[ci].python_versions = ["3.10", "3.13"] only expands the Windows matrix in GitLab."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'python_versions = ["3.10", "3.13"]\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    # Only Windows fans out; PY_TAG variable is windows-only.
    assert "PY_TAG: 'py310'" in content
    assert "PY_TAG: 'py313'" in content
    # Linux jobs are single-version and use a static image.
    assert "conan-gcc13-py3.13" in content
    assert "cp313-cp313" in content
    # Linux jobs are NOT fanned out — so CP_TAG (only the wheel-repair var) is gone.
    assert "CP_TAG" not in content


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


def test_github_ci_version_floor(ci_toml, tmp_path):
    """Rendered GitHub CI floors xmsconan at the generating version.

    Every job installs ``xmsconan>=<version that generated the file>`` rather
    than pinning with ``==``, so a repo picks up an xmsconan release without
    regenerating and committing its CI. The floor still rules out resolving a
    version older than the templates were written against.
    """
    from xmsconan import __version__
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert f"xmsconan>={__version__}" in content
    assert "xmsconan==" not in content


def test_github_flake_job_uses_generated_flake8_config(ci_toml, tmp_path):
    """The flake job lints with the generated .flake8, not inline duplicates.

    Inlining the settings duplicated .flake8.jinja, and the two copies drifted:
    CI used a different ignore list and a stale sphinx conf.py exclude, so a
    clean local run did not imply a clean CI run.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")

    assert "flake8 _package" in content
    # --isolated makes flake8 ignore .flake8 entirely, which is what allowed
    # the two configs to diverge unnoticed.
    assert "--isolated" not in content
    assert "--max-line-length" not in content
    # The config has to exist before flake8 runs.
    assert "xmsconan_gen build.toml" in content


def test_ci_pins_conan_version(ci_toml, tmp_path):
    """Conan is pinned to a patch series, not installed unpinned.

    A conan minor bump can change package_id computation and silently detach
    a build from binaries already published to the remote.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")

    assert '"conan~=' in content
    assert "pip install conan " not in content


def test_github_ci_includes_artifacts_dir_flag(ci_toml, tmp_path):
    """Rendered GitHub CI build commands include --artifacts-dir test_artifacts."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "--artifacts-dir test_artifacts" in content


def test_github_ci_includes_test_artifact_upload(ci_toml, tmp_path):
    """Rendered GitHub CI has upload-artifact steps for test artifacts."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "test-artifacts-" in content
    assert "test_artifacts/" in content


def test_github_ci_test_artifact_upload_uses_always(ci_toml, tmp_path):
    """Verify test artifact upload step uses if: always()."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    # Find lines with "Upload test artifacts" and check the surrounding context
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if "Upload test artifacts" in line:
            # Look for 'if: always()' within the next few lines
            block = "\n".join(lines[i:i + 8])
            assert "always()" in block


def test_gitlab_ci_includes_artifacts_dir_flag(tmp_path):
    """Rendered GitLab CI build commands include --artifacts-dir test_artifacts."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "--artifacts-dir test_artifacts" in content


def test_gitlab_ci_includes_test_artifacts_path(tmp_path):
    """Rendered GitLab CI artifacts paths include test_artifacts/."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "test_artifacts/" in content


def test_gitlab_ci_uses_when_always(tmp_path):
    """Rendered GitLab CI Conan Build job uses when: always for artifacts."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "when: always" in content


def test_gitlab_ci_sets_ctest_parallel_level(tmp_path):
    """Rendered GitLab CI sets CTEST_PARALLEL_LEVEL in Conan Build job."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "export CTEST_PARALLEL_LEVEL=${CTEST_PARALLEL_LEVEL:-8}" in content


def test_github_ci_sets_ctest_parallel_level(ci_toml, tmp_path):
    """Rendered GitHub CI sets CTEST_PARALLEL_LEVEL."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "CTEST_PARALLEL_LEVEL: '8'" in content


def test_gitlab_ci_split_tests_generates_separate_jobs(tmp_path):
    """When split_tests = true, generates separate Build and Test jobs."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'split_tests = true\n'
        'coverage = true\n'
        'xvfb = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert '"Run C++ Tests":' in content
    assert "XMS_SKIP_CXX_TESTS" in content
    import yaml
    ci = yaml.safe_load(content)
    # Build and Test should be separate stages
    assert ci["Conan Build"]["stage"] == "Build"
    assert ci["Run C++ Tests"]["stage"] == "Test"
    # Coverage should be informational-only when tests run in a separate job
    assert ci["Coverage"].get("allow_failure") is True


def test_gitlab_ci_no_split_tests_by_default(tmp_path):
    """Without split_tests, no separate test job is generated."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert '"Run C++ Tests":' not in content
    assert "XMS_SKIP_CXX_TESTS" not in content
    # No Build stage — everything stays in Test
    import yaml
    ci = yaml.safe_load(content)
    assert ci["Conan Build"]["stage"] == "Test"
    assert "Build" not in ci.get("stages", [])


def test_gitlab_ci_coverage_allow_failure_without_split_tests(tmp_path):
    """Coverage is required (no allow_failure) when split_tests is not set."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n'
        'xvfb = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    import yaml
    ci = yaml.safe_load(content)
    assert "allow_failure" not in ci.get("Coverage", {})


def test_gitlab_ci_test_shards_generates_parallel_jobs(tmp_path):
    """When test_shards > 1, Run C++ Tests uses parallel and GTEST_SHARD env vars."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'split_tests = true\n'
        'test_shards = 4\n'
        'xvfb = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    import yaml
    ci = yaml.safe_load(content)
    assert ci["Run C++ Tests"]["parallel"] == 4
    assert "GTEST_TOTAL_SHARDS=4" in content
    assert "GTEST_SHARD_INDEX" in content


def test_gitlab_ci_no_shards_without_config(tmp_path):
    """Without test_shards, no parallel or GTEST env vars are generated."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'split_tests = true\n'
        'xvfb = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    import yaml
    ci = yaml.safe_load(content)
    assert "parallel" not in ci["Run C++ Tests"]
    assert "GTEST_TOTAL_SHARDS" not in content


def test_gitlab_ci_test_shards_without_split_tests_no_parallel(tmp_path):
    """test_shards alone (without split_tests) does not produce a parallel test job."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'test_shards = 4\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert '"Run C++ Tests":' not in content
    assert "GTEST_TOTAL_SHARDS" not in content
    # The python-version matrix uses `parallel:` too, so check for the
    # test-sharding form ("parallel: <int>") specifically.
    assert "parallel: 4" not in content


def test_gitlab_ci_split_test_job_uses_xvfb(tmp_path):
    """Split test job wraps runner with xvfb-run when xvfb = true."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'split_tests = true\n'
        'xvfb = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    # Find the "Run C++ Tests:" section
    import re
    test_section = re.search(r'"Run C\+\+ Tests":.*?(?=\n\S|\Z)', content, re.DOTALL)
    assert test_section is not None
    assert "xvfb-run" in test_section.group()


def test_github_coverage_yaml_generated_when_coverage_true(tmp_path):
    """Setting [ci].coverage = true renders an additional Coverage.yaml workflow."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    cov = output_dir / ".github" / "workflows" / "Coverage.yaml"
    assert cov.exists()
    content = cov.read_text(encoding="utf-8")
    assert "xmsconan_coverage" in content
    # The default GitHub Coverage workflow now runs directly on
    # ubuntu-latest — NOT inside the conan-gcc13-py3.13 docker image. That
    # image used to bake xmsconan in, which silently shadowed any
    # ``pip install xmsconan>=X.Y.Z`` in the workflow (the constraint was
    # already satisfied so pip skipped the install), locking the canary
    # to whatever version the image happened to carry. Containerless
    # runs always pull the latest xmsconan from devpi, which is the
    # contract a Coverage canary needs.
    assert "container:" not in content, (
        f"default Coverage workflow must not declare a container; got:\n{content}"
    )
    assert "ghcr.io/aquaveo/conan-gcc13-py3.13" not in content
    assert "actions/setup-python" in content
    assert 'pip install --upgrade "xmsconan>=' in content


def test_github_coverage_yaml_omitted_when_coverage_false(ci_toml, tmp_path):
    """Coverage.yaml is not rendered when ci.coverage is not set."""
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    cov = output_dir / ".github" / "workflows" / "Coverage.yaml"
    assert not cov.exists()


def test_github_coverage_apt_installs_xvfb_when_requested(tmp_path):
    """Coverage + xvfb apt-installs xvfb on the containerless ubuntu-latest runner.

    Previously this selected a docker container image with xvfb baked in;
    that introduced the stale-in-image lock-in bug. The Coverage workflow
    no longer uses a container, so xvfb support is provided by apt
    install in a job step instead.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsvtk"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n'
        'xvfb = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    cov = output_dir / ".github" / "workflows" / "Coverage.yaml"
    content = cov.read_text(encoding="utf-8")
    assert "container:" not in content
    assert "apt-get install -y xvfb" in content


def test_gitlab_coverage_stage_delegates_to_xmsconan_coverage(tmp_path):
    """The GitLab Coverage stage now invokes xmsconan_coverage instead of inline gcovr."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert "xmsconan_coverage" in content
    # The hand-rolled coverage preset / profile references should be gone.
    assert "linux_testing_debug_coverage" not in content
    assert "cmake --preset coverage" not in content


def test_gitlab_coverage_version_is_parameterized(tmp_path):
    """The GitLab Coverage stage uses ${CI_COMMIT_TAG:-0.0.0}, not a hardcoded version."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    # The Coverage stage must source the version from CI_COMMIT_TAG with a 0.0.0 fallback.
    assert "${PACKAGE_VERSION}" in content
    assert "${CI_COMMIT_TAG:-0.0.0}" in content
    # And must not bake a literal 0.0.0 into the xmsconan_coverage invocation.
    assert "xmsconan_coverage --version 0.0.0" not in content


def test_gitlab_pages_landing_links_both_coverage_reports(tmp_path):
    """The pages stage emits a landing page with links to both cpp/ and python/."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert 'href="cpp/index.html"' in content
    assert 'href="python/index.html"' in content
    # The old "C++ only" landing copy must be gone.
    assert "cp coverage-html-cpp/index.html public/index.html" not in content


def test_github_coverage_pins_gcovr(tmp_path):
    """Generated Coverage.yaml pins the gcovr major to keep CI reproducible."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    cov = output_dir / ".github" / "workflows" / "Coverage.yaml"
    content = cov.read_text(encoding="utf-8")
    # gcovr must carry a version constraint.
    assert "pip install conan wheel gcovr" not in content
    assert "gcovr>=" in content


def test_github_coverage_overrides_version_from_git_tag(tmp_path):
    """Generated Coverage.yaml derives XMS_VERSION from the git tag when present."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'coverage = true\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    cov = output_dir / ".github" / "workflows" / "Coverage.yaml"
    content = cov.read_text(encoding="utf-8")
    # The Get Tag + Set Coverage Version steps must be present.
    assert "little-core-labs/get-git-tag" in content
    assert "steps.gitTag.outputs.tag" in content


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


# --- [ci].linux ----------------------------------------------------------


def test_gitlab_linux_defaults_on(tmp_path):
    """Without [ci].linux the Linux jobs are emitted, exactly as before."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert "\nConan Build:" in content
    assert "Repair Wheel:" in content
    assert '"Wheel Deploy":' in content
    assert '"Conan Deploy - Linux":' in content
    assert "  - Package" in content


def test_gitlab_linux_false_drops_every_linux_job(tmp_path):
    """[ci].linux = false removes the Linux jobs and the wheel chain they feed.

    The Linux job owns ``wheelhouse``; Repair Wheel and Wheel Deploy consume it
    through ``dependencies:``. Dropping the producer while keeping the consumers
    would leave a pipeline that fails at run time on a missing artifact, so they
    go together -- as does the Package stage that would otherwise be empty.

    Asserted on the parsed document rather than raw substrings: a set equality
    over the job names catches a job that should have gone and one that should
    have stayed, and does not pass vacuously when a job is merely renamed.
    """
    toml_file = write_gitlab_toml(tmp_path, linux=False)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    jobs = {key for key in parsed if key not in NON_JOB_SHAPE_KEYS}
    assert jobs == {"Conan Build - Windows", "Conan Deploy - Windows", "Lint"}
    assert parsed["stages"] == ["Test", "Deploy"]


@pytest.mark.parametrize("ci_flags,expected_message", [
    ({"linux": False, "windows": False}, r"\[ci\]\.linux and \[ci\]\.windows to false"),
    ({"linux": False, "coverage": True}, r"\[ci\]\.coverage = true with \[ci\]\.linux = false"),
], ids=["no-platform", "coverage-without-linux"])
def test_gitlab_rejects_impossible_flag_combinations(tmp_path, ci_flags, expected_message):
    """Combinations that cannot produce a working pipeline fail at generation.

    Nothing to build, and coverage without the gcc job that instruments it. The
    patterns name the offending keys rather than a bare word, so a different
    ValueError mentioning "coverage" cannot satisfy the test.
    """
    toml_file = write_gitlab_toml(tmp_path, **ci_flags)

    with pytest.raises(ValueError, match=expected_message):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))


def test_github_is_unaffected_by_linux_flag(tmp_path):
    """[ci].linux is a GitLab concept, matching [ci].windows.

    Neither flag is referenced by the GitHub template, so a GitHub project that
    sets one still gets its full matrix rather than a silently truncated one.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core"\nci_type = "github"\n'
        '\n[ci]\nlinux = false\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").read_text(encoding="utf-8")
    assert "\n  linux:" in content


@pytest.mark.parametrize("ci_flags,expected", [
    ({"linux": False}, ["[ci].linux"]),
    ({"windows": False}, ["[ci].windows"]),
    ({"linux": False, "windows": False}, ["[ci].linux", "[ci].windows"]),
    ({"linux": True}, ["[ci].linux"]),
], ids=["linux", "windows", "both", "explicit-true"])
def test_github_warns_for_any_explicit_gitlab_only_flag(tmp_path, caplog, ci_flags, expected):
    """A GitHub project setting either GitLab-only flag is told it does nothing.

    The behavior is documented, but documented is not discoverable: without
    this the setting is accepted in silence and the full matrix is emitted
    anyway. Setting one to true is as inert as setting it to false, so both
    are warned about.
    """
    toml_file = write_github_toml(tmp_path, **ci_flags)

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))

    assert "GitLab-only" in caplog.text
    for flag in expected:
        assert flag in caplog.text


def test_github_does_not_warn_when_the_flags_are_absent(tmp_path, caplog):
    """No [ci] platform flags means nothing to warn about."""
    toml_file = write_github_toml(tmp_path, xvfb=True)

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))

    assert "GitLab-only" not in caplog.text


def test_gitlab_project_gets_no_such_warning(tmp_path, caplog):
    """The warning is GitHub-specific, and on GitLab the flag is actually honored.

    Asserting the absence of the warning alone would pass even if the flag were
    ignored on GitLab too, which is the opposite of the documented behavior --
    so assert the Linux jobs really are gone.
    """
    output_dir = tmp_path / "output"
    toml_file = write_gitlab_toml(tmp_path, linux=False)

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(output_dir))

    assert "GitLab-only" not in caplog.text
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    jobs = {key for key in parsed if key not in NON_JOB_SHAPE_KEYS}
    assert jobs == {"Conan Build - Windows", "Conan Deploy - Windows", "Lint"}


def test_gitlab_linux_image_tracks_the_implicit_linux_default(tmp_path):
    """With no explicit Linux list the image follows the highest CI entry."""
    toml_file = write_gitlab_toml(tmp_path, python_versions=["3.10", "3.14"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    # Linux falls back to the highest CI entry, rendered literally because there
    # is no matrix to interpolate from.
    parsed = yaml.safe_load(content)
    assert parsed["Conan Build"]["image"].endswith("conan-gcc13-py3.14")
    assert parsed["Conan Build"]["variables"]["PYTHON_TARGET_VERSION"] == "3.14"


def test_gitlab_linux_fanout_adds_matching_build_and_deploy_matrices(tmp_path):
    """Multi-entry linux_python_versions fans out the build and its deploy."""
    toml_file = write_gitlab_toml(
        tmp_path,
        python_versions=["3.10", "3.13", "3.14"],
        linux_python_versions=["3.13", "3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    legs = [{"PYTHON_TARGET_VERSION": "3.13"}, {"PYTHON_TARGET_VERSION": "3.14"}]
    assert parsed["Conan Build"]["parallel"]["matrix"] == legs
    assert parsed["Conan Deploy - Linux"]["parallel"]["matrix"] == legs
    # The image interpolates the leg rather than naming a version.
    assert parsed["Conan Build"]["image"].endswith("conan-gcc13-py${PYTHON_TARGET_VERSION}")
    # A fanned-out build must not also carry a fixed PYTHON_TARGET_VERSION
    # variable competing with the one the matrix supplies.
    assert "PYTHON_TARGET_VERSION" not in parsed["Conan Build"].get("variables", {})


def test_gitlab_linux_fanout_gives_each_leg_its_own_export_tarball(tmp_path):
    """Without a per-ABI name the parallel legs overwrite one another's export."""
    toml_file = write_gitlab_toml(tmp_path, linux_python_versions=["3.13", "3.14"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert ".export/xmssnap-linux-py${PYTHON_TARGET_VERSION}-${PACKAGE_VERSION}.tar.gz" in content
    assert ".export/xmssnap-linux-${PACKAGE_VERSION}.tar.gz" not in content


def test_gitlab_single_linux_version_keeps_unsuffixed_export_tarball(tmp_path):
    """The export name only grows the ABI suffix once there is a fan-out."""
    toml_file = write_gitlab_toml(tmp_path, linux_python_versions=["3.13"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert ".export/xmssnap-linux-${PACKAGE_VERSION}.tar.gz" in content
    # Windows legitimately carries the suffix; only the Linux name must be bare.
    assert "xmssnap-linux-py${PYTHON_TARGET_VERSION}" not in content


def test_gitlab_split_tests_with_multiple_linux_versions_is_rejected(tmp_path):
    """The C++ test job takes the build's artifacts by name, so it cannot fan out."""
    toml_file = write_gitlab_toml(
        tmp_path, split_tests=True, linux_python_versions=["3.13", "3.14"],
    )
    with pytest.raises(ValueError, match="split_tests"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))


def test_gitlab_split_tests_with_one_linux_version_is_allowed(tmp_path):
    """The guard is about fan-out, not about split_tests itself."""
    toml_file = write_gitlab_toml(
        tmp_path, split_tests=True, linux_python_versions=["3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    # split_tests still splits, and the single-version build stays a plain job.
    assert "Run C++ Tests" in parsed
    assert "parallel" not in parsed["Conan Build"]


def test_gitlab_coverage_image_uses_the_coverage_python_version(tmp_path):
    """Coverage pins one ABI, so it tracks the coverage version, not the fan-out."""
    toml_file = write_gitlab_toml(
        tmp_path, coverage=True, linux_python_versions=["3.13", "3.14"],
        coverage_table={"python_version": "3.13"},
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert parsed["Coverage"]["image"].endswith("conan-gcc13-py3.13")


def test_github_coverage_workflow_sets_up_the_pinned_python(tmp_path):
    """Coverage.yaml must install the interpreter the coverage build pins to.

    ``xmsconan coverage`` filters its pybind build to the resolved version, so a
    workflow that set up a different one would hunt for a package the runner
    never built.
    """
    toml_file = write_github_toml(tmp_path, coverage=True, python_versions=["3.10", "3.14"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "Coverage.yaml").read_text(encoding="utf-8")
    job = yaml.safe_load(content)["jobs"]["coverage"]
    assert job["env"]["PYTHON_TARGET_VERSION"] == "3.14"
    setup = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert [s["with"]["python-version"] for s in setup] == ["3.14"]


def test_version_sort_key_orders_numerically_not_lexically():
    """Version 3.9 must sort below 3.10, which string comparison gets backwards.

    Exercised directly rather than through build.toml: every version the recipe
    currently allows happens to sort the same either way, so a rendering test
    could not tell the two orderings apart.
    """
    assert max(["3.9", "3.10"], key=version_sort_key) == "3.10"
    assert sorted(["3.14", "3.9", "3.10"], key=version_sort_key) == ["3.9", "3.10", "3.14"]


@pytest.mark.parametrize("xvfb", [False, True])
def test_gitlab_repair_wheel_interpreter_matches_its_image(tmp_path, xvfb):
    """The absolute interpreter path must name the ABI its image actually ships.

    With xvfb the job runs ``conan-gcc13-x11-gdal-py<version>``, which carries
    only the one ABI its tag names, so a hardcoded ``cp313-cp313`` breaks as
    soon as the resolved Linux version is anything but 3.13.
    """
    toml_file = write_gitlab_toml(
        tmp_path, xvfb=xvfb, linux_python_versions=["3.13", "3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    job = parsed["Repair Wheel"]
    assert all("/opt/python/cp314-cp314/bin/" in line for line in job["script"])
    if xvfb:
        assert job["image"].endswith("conan-gcc13-x11-gdal-py3.14")


def test_gitlab_repair_wheel_interpreter_unchanged_without_fanout(tmp_path):
    """A project on one Linux version keeps the cp313 path it already had."""
    toml_file = write_gitlab_toml(tmp_path, python_versions=["3.10", "3.13"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert all("/opt/python/cp313-cp313/bin/" in line
               for line in parsed["Repair Wheel"]["script"])
