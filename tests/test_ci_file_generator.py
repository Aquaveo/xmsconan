"""Tests for generator_tools.ci_file_generator."""
import logging
import re

import pytest
import yaml

from xmsconan.constants import version_sort_key
from xmsconan.coverage_tools.coverage_generator import EXIT_GATE_FAILED
from xmsconan.generator_tools.ci_file_generator import (
    _display_name,
    generate_ci,
)
from .ci_helpers import (
    NON_JOB_SHAPE_KEYS,
    WHEEL_ONLY,
    write_github_toml,
    write_gitlab_toml,
)


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


def test_generate_ci_rejects_an_unknown_top_level_key(tmp_path):
    """`xmsconan ci` rejects what `gen` and `profiles` reject.

    It reads the same build.toml with `.get()` and was the last of the three
    entry points not validating, so it emitted a pipeline from a file the other
    two refuse -- and the committed CI then kept whatever default the typo
    produced, with no symptom anywhere.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n'
        'has_test_files = true\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown top-level key\\(s\\) has_test_files"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))


def test_gitlab_pins_conan_version(tmp_path):
    """Conan installs in the GitLab pipeline are pinned, same as GitHub's.

    Only the GitHub workflows were asserted; the GitLab template installs conan
    on five separate lines and a bump slipping into any one of them detaches
    that leg from the binaries the others resolved.
    """
    # coverage=True so the fifth install -- the coverage job's, the one the
    # GitHub side already had a test for -- is rendered too.
    toml_file = write_gitlab_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    conan_installs = _conan_install_lines(content)

    assert conan_installs
    assert [line for line in conan_installs if '"conan~=' not in line] == []


def _conan_install_lines(content):
    """Return the rendered lines that pip-install conan itself.

    `xmsconan` is excluded because it matches the substring and is pinned on
    its own `>=` line. Checked per line rather than file-wide: the old
    `'"conan~=' in content` was satisfied by any one pinned install anywhere,
    and its `"pip install conan " not in content` half stayed true when the
    conan install was dropped from the workflow altogether.
    """
    return [
        line
        for line in content.splitlines()
        if "pip install" in line and "conan" in line and "xmsconan" not in line
    ]


def test_ci_pins_conan_version(ci_toml, tmp_path):
    """Conan is pinned to a patch series, not installed unpinned.

    A conan minor bump can change package_id computation and silently detach
    a build from binaries already published to the remote.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")

    conan_installs = _conan_install_lines(content)

    assert conan_installs
    assert [line for line in conan_installs if '"conan~=' not in line] == []


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
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, split_tests=True, coverage=True, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    assert "XMS_SKIP_CXX_TESTS" in content
    ci = yaml.safe_load(content)
    # Build and Test are separate stages, and the build fans out into one job
    # per configuration rather than one job looping them.
    build_jobs = {}
    for name, job in ci.items():
        # The Windows build job shares the stage but not the shape: it is one
        # matrix job over the ABIs, not a job per configuration.
        if not isinstance(job, dict) or "Windows" in name:
            continue
        if job.get("stage") == "Build":
            build_jobs[name] = job
    assert build_jobs, "split_tests must emit Linux Build-stage jobs"
    needs = {name: job.get("needs") for name, job in build_jobs.items()}
    assert all(value == [] for value in needs.values()), (
        "every Linux build job declares needs: [] so they start together "
        f"rather than at their stage's turn: {needs}"
    )
    # The Release leg is uninstrumented, so its suite runs in a Test job. The
    # Debug leg is instrumented under coverage = true and runs its own suite
    # inside its build, beside the .gcno gcovr has to read the .gcda against.
    assert '"Run C++ Tests - Release-testing":' in content
    assert '"Run C++ Tests - Debug-testing":' not in content
    assert ci["Run C++ Tests - Release-testing"]["stage"] == "Test"
    # Coverage is informational-only when tests run in a separate job -- but
    # only for the coverage gate itself. The tool failing still fails the job.
    # Asserted through the constant: the forgiven code must be the one the
    # tool actually exits with, not a number the template happens to repeat.
    assert ci["Coverage"].get("allow_failure") == {"exit_codes": [EXIT_GATE_FAILED]}


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
    ci = yaml.safe_load(content)
    build_jobs = {name: job for name, job in ci.items()
                  if isinstance(job, dict) and "build.py" in str(job.get("script", ""))}
    assert build_jobs, "the Linux build jobs must still be emitted"
    assert all(job["stage"] == "Test" for job in build_jobs.values()), build_jobs
    assert "Build" not in ci.get("stages", [])


def test_gitlab_ci_coverage_allow_failure_without_split_tests(tmp_path):
    """Coverage is required (no allow_failure) when split_tests is not set."""
    toml_file = write_gitlab_toml(tmp_path, coverage=True, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    ci = yaml.safe_load(content)
    assert "allow_failure" not in ci.get("Coverage", {})


def test_gitlab_ci_test_shards_run_in_one_container(tmp_path):
    """test_shards > 1 shards inside one job rather than fanning out into N jobs.

    `parallel: N` bought its concurrency by starting N containers, each
    repeating the container start, the pip install and the artifact download to
    run one Nth of the suite. The shard count now reaches xmsconan_test_shards,
    which forks N processes in the single container this job already has.
    """
    toml_file = write_gitlab_toml(tmp_path, split_tests=True, test_shards=4, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    ci = yaml.safe_load(content)
    assert "parallel" not in ci["Run C++ Tests - Debug-testing"]
    assert "--shards 4" in content
    # The GTEST_* variables are the shard runner's business now. Exporting them
    # from the job would pin every shard in the container to the same index.
    assert "GTEST_TOTAL_SHARDS" not in content
    assert "GTEST_SHARD_INDEX" not in content
    # The merged report is what makes a failing case visible in the MR widget.
    junit = ci["Run C++ Tests - Debug-testing"]["artifacts"]["reports"]["junit"]
    assert junit == "TEST-cxxtest.xml"


def test_gitlab_ci_no_shards_without_config(tmp_path):
    """Without test_shards the job runs a single shard, still via the runner script.

    One code path for both cases: a single shard is the N=1 degenerate one, so
    the artifact-finding and test_files relinking that the job needs either way
    does not have to exist twice.
    """
    toml_file = write_gitlab_toml(tmp_path, split_tests=True, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    ci = yaml.safe_load(content)
    assert "parallel" not in ci["Run C++ Tests - Debug-testing"]
    assert "GTEST_TOTAL_SHARDS" not in content
    assert "--shards 1" in content


def test_gitlab_ci_test_shards_without_split_tests_no_parallel(tmp_path):
    """test_shards alone (without split_tests) does not produce a parallel test job."""
    toml_file = write_gitlab_toml(tmp_path, test_shards=4)
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
    """Setting xvfb = true reaches the shard runner as --xvfb.

    The wrapping moved into the script because each shard needs its *own*
    display: `xvfb-run -a` picks a free server number, but between that check
    and the bind is a window that N simultaneous starts land in.
    """
    toml_file = write_gitlab_toml(tmp_path, split_tests=True, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    # Every test job gets it, not just whichever one happens to render first.
    sections = re.findall(r'"Run C\+\+ Tests - .*?(?=\n\S|\Z)', content, re.DOTALL)
    assert len(sections) == 2, sections
    assert all("--xvfb" in section for section in sections)


def _cxx_test_jobs(ci):
    """The generated C++ test jobs, keyed by job name."""
    return {name: job for name, job in ci.items() if name.startswith("Run C++ Tests")}


def test_gitlab_split_tests_runs_every_staged_testing_configuration(tmp_path):
    """One test job per staged testing configuration, each naming its own label.

    The regression this pins: a single job found its artifact directory by
    falling back to the first of Debug-testing, Release-testing that existed, so
    a matrix building both compiled the Release runner on every pipeline and
    never ran it. Nothing was red -- the suite simply went unexecuted for one of
    the two configurations.
    """
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, split_tests=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    ci = yaml.safe_load(content)

    jobs = _cxx_test_jobs(ci)
    assert set(jobs) == {
        "Run C++ Tests - Release-testing",
        "Run C++ Tests - Debug-testing",
    }
    for label in ("Release-testing", "Debug-testing"):
        script = "\n".join(jobs[f"Run C++ Tests - {label}"]["script"])
        assert f"--label {label}" in script
    # Peers in the Test stage, each off its own build job rather than off one
    # shared build -- which is what lets the builds run concurrently.
    assert all(job["stage"] == "Test" for job in jobs.values())
    assert jobs["Run C++ Tests - Release-testing"]["needs"] == [
        {"job": "Release Build", "artifacts": True},
    ]
    assert jobs["Run C++ Tests - Debug-testing"]["needs"] == [
        {"job": "Debug Build", "artifacts": True},
    ]
    # No invocation may reach the label fallback: passing --label is the fix.
    assert "xmsconan_test_shards --artifacts-dir test_artifacts --shards" not in content


def test_gitlab_split_test_job_uploads_only_its_own_configuration(tmp_path):
    """Each test job re-uploads its own artifacts, not every other job's too.

    ``needs: artifacts: true`` downloads the whole staged tree, so an unscoped
    ``test_artifacts/`` path would have each job upload a second copy of every
    configuration it did not run.
    """
    toml_file = write_gitlab_toml(tmp_path, split_tests=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    for name, job in _cxx_test_jobs(ci).items():
        label = name.removeprefix("Run C++ Tests - ")
        assert job["artifacts"]["paths"] == [
            f"test_artifacts/{label}/", "TEST-cxxtest.xml",
        ]


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


def _instrumented_build_jobs(parsed):
    """The build jobs that compile with coverage instrumentation.

    Found by what they run rather than by name. The generator names them from
    the configuration they build ("Debug Instrumented Build", "Python
    Instrumented Build"), and a test that hard-coded those names would be
    asserting the naming scheme rather than the invariant it cares about --
    and would go green if a rename left a job uninstrumented.
    """
    jobs = {}
    for name, job in parsed.items():
        if not isinstance(job, dict):
            continue
        script = job.get("script") or []
        if any("--phase measure" in str(line) for line in script):
            jobs[name] = job
    return jobs


def test_gitlab_coverage_job_declares_python_target_version(tmp_path):
    """Each instrumented build's ABI target and container image come from one value.

    The old single Coverage Build selected its image from the resolved coverage
    ABI but declared no ``PYTHON_TARGET_VERSION``, so the packager generated a
    matrix for the silent 3.13 default inside a 3.14 container and the tool's
    ``--filter`` then matched nothing. Asserting the image and the variable
    *agree* is the point: a test on either one alone would still have passed
    while the two disagreed.

    Now that the instrumented builds are separate concurrent jobs, the pair has
    to agree on every one of them -- one job drifting is exactly the failure
    the original bug was, reintroduced in a job the old assertion never saw.
    """
    toml_file = write_gitlab_toml(
        tmp_path,
        matrix_table=WHEEL_ONLY, coverage=True, linux_python_versions=["3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    jobs = _instrumented_build_jobs(parsed)
    assert jobs, "coverage = true must emit at least one instrumented build job"
    for name, job in jobs.items():
        pinned = job["variables"]["PYTHON_TARGET_VERSION"]
        assert pinned == "3.14", name
        assert job["image"].endswith(f"-py{pinned}"), (
            f"{name} image {job['image']!r} must match its pinned ABI "
            f"{pinned!r}; a mismatch fails "
            "find_package(Python3 ... EXACT REQUIRED) at configure."
        )


def test_gitlab_coverage_pins_python_target_version_under_an_explicit_image(tmp_path):
    """An explicit [ci].docker_image replaces the image but not the ABI pin.

    The other two image branches derive the image from the same resolved ABI,
    so image and pin cannot disagree there. This branch takes whatever image
    the repo named, which is exactly why the pin has to keep coming from the
    resolver: dropping it here would put the packager back on its silent 3.13
    default inside somebody else's container.
    """
    toml_file = write_gitlab_toml(
        tmp_path,
        matrix_table=WHEEL_ONLY, coverage=True, linux_python_versions=["3.14"],
        docker_image="registry.example.com/custom:latest",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    jobs = _instrumented_build_jobs(parsed)
    assert jobs, "coverage = true must emit at least one instrumented build job"
    for name, job in jobs.items():
        assert job["image"] == "registry.example.com/custom:latest", name
        assert job["variables"]["PYTHON_TARGET_VERSION"] == "3.14", name


def test_gitlab_coverage_pins_python_target_version_under_xvfb(tmp_path):
    """The xvfb image branch derives its image from the ABI too, and still pins it.

    This is the second of the two derived-image branches the sibling tests
    claim cannot drift; the x11 image name is built from the same resolved
    value, so asserting the pair here is what makes that claim true for both
    rather than for whichever branch happened to get a test.
    """
    toml_file = write_gitlab_toml(
        tmp_path,
        matrix_table=WHEEL_ONLY, coverage=True, xvfb=True, linux_python_versions=["3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    jobs = _instrumented_build_jobs(parsed)
    assert jobs, "coverage = true must emit at least one instrumented build job"
    for name, job in jobs.items():
        pinned = job["variables"]["PYTHON_TARGET_VERSION"]
        assert pinned == "3.14", name
        assert "x11" in job["image"], (
            f"{name} image {job['image']!r} must be the xvfb variant "
            "under xvfb = true"
        )
        assert job["image"].endswith(f"-py{pinned}"), (
            f"{name} image {job['image']!r} must match its pinned ABI "
            f"{pinned!r}; a mismatch fails "
            "find_package(Python3 ... EXACT REQUIRED) at configure."
        )


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


def test_gitlab_pages_takes_the_html_from_the_job_that_rendered_it(tmp_path):
    """The HTML is published from the job that renders it.

    That is now "Coverage": the instrumented builds each emit only a tracefile,
    and the merge that turns those into HTML happens in the gate job. It
    uploads ``when: always`` so the report still publishes on a gate miss --
    the run you most want to read is the one that missed its threshold.
    """
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    assert parsed["pages"]["dependencies"] == ["Coverage"]
    report = parsed["Coverage"]["artifacts"]
    assert "coverage-html-cpp/" in report["paths"]
    assert "coverage-html-py/" in report["paths"]
    assert report["when"] == "always", (
        "a gate miss must still publish its report; without when: always the "
        "failing run is the one that publishes nothing"
    )
    # cov-cpp.xml stays: GitLab reads the cobertura report off the job that
    # declares it, which has to be the one carrying the coverage: regex.
    assert "cov-cpp.xml" in report["paths"]


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
    # "Conan Build", not the per-configuration jobs: without [matrix].wheel_only
    # a repository keeps the single looping build job.
    assert "\nConan Build:" in content
    assert "Repair Wheel:" in content
    assert '"Wheel Deploy":' in content
    assert '"Conan Deploy - Linux":' in content
    assert "  - Package" in content


def test_gitlab_linux_false_drops_every_linux_job(tmp_path):
    """[ci].linux = false removes the Linux jobs and the Linux wheel chain.

    The Linux build owns the ``wheelhouse`` that the Package-stage Repair Wheel
    job and the "Wheel Deploy" job consume through ``dependencies:``. Dropping
    the producer while keeping those consumers would leave a pipeline that fails
    at run time on a missing artifact, so they go together -- as does the Package
    stage that would otherwise be empty. The Windows wheel chain is independent:
    that build repairs its own wheel in place and "Wheel Deploy - Windows"
    uploads it, so it survives here.

    Asserted on the parsed document rather than raw substrings: a set equality
    over the job names catches a job that should have gone and one that should
    have stayed, and does not pass vacuously when a job is merely renamed.
    """
    toml_file = write_gitlab_toml(tmp_path, linux=False)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    jobs = {key for key in parsed if key not in NON_JOB_SHAPE_KEYS}
    assert jobs == {
        "Conan Build - Windows", "Wheel Deploy - Windows", "Conan Deploy - Windows", "Lint",
    }
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
    assert jobs == {
        "Conan Build - Windows", "Wheel Deploy - Windows", "Conan Deploy - Windows", "Lint",
    }


def test_gitlab_linux_image_tracks_the_implicit_linux_default(tmp_path):
    """With no explicit Linux list the image follows the highest CI entry."""
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, python_versions=["3.10", "3.14"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    # Linux falls back to the highest CI entry, rendered literally because there
    # is no matrix to interpolate from.
    parsed = yaml.safe_load(content)
    build_jobs = {name: job for name, job in parsed.items()
                  if isinstance(job, dict) and job.get("needs") == []}
    assert build_jobs, "the Linux build jobs must be emitted"
    for name, job in build_jobs.items():
        assert job["image"].endswith("conan-gcc13-py3.14"), name
        assert job["variables"]["PYTHON_TARGET_VERSION"] == "3.14", name


def _linux_export_names(content):
    """The Conan cache tarballs the Linux build jobs save, in job order.

    Windows saves one too, from its own job and under a name of its own shape
    (``-windows-py${PYTHON_TARGET_VERSION}-``). It is matched by a bare
    ``--save .export/`` search and would be counted as a Linux export, which is
    how a count assertion goes green for the wrong reason.
    """
    return [line.split("--save ")[1].strip()
            for line in content.splitlines()
            if "--save .export/" in line and "-linux-" in line]


def test_gitlab_linux_fanout_builds_each_abi_and_deploys_them_together(tmp_path):
    """Multi-entry linux_python_versions fans the build out and the deploy in.

    The build fans out: one job per surviving configuration, so a pybind
    configuration becomes a job per Python version and they compile
    concurrently instead of in one job's loop.

    The deploy does the opposite. It used to carry a ``parallel: matrix`` over
    the ABIs. One job restores every tarball and uploads once instead, which is
    one fewer way for two legs to race on the same Conan reference -- and it
    stays correct if the exporting set ever stops being one-per-ABI.
    """
    toml_file = write_gitlab_toml(
        tmp_path,
        matrix_table=WHEEL_ONLY,
        python_versions=["3.10", "3.13", "3.14"],
        linux_python_versions=["3.13", "3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    for version in ("3.13", "3.14"):
        job = parsed[f"Python Build - py{version}"]
        assert job["variables"]["PYTHON_TARGET_VERSION"] == version
        assert job["image"].endswith(f"conan-gcc13-py{version}")
        assert "parallel" not in job, (
            "the ABI fan-out is the job list now; a matrix on top of it would "
            "build every ABI in every job"
        )

    deploy = parsed["Conan Deploy - Linux"]
    assert "parallel" not in deploy, (
        "one job restores every tarball; legs would race on the same Conan "
        "reference and no longer line up with the exports anyway"
    )
    # Every configuration a tag builds hands the deploy a package. Under
    # wheel_only that is the clean pybind build of each ABI, and nothing else:
    # the testing configurations are branch-only and there are no library
    # configurations to publish.
    assert set(deploy["dependencies"]) == {
        "Python Build - py3.13", "Python Build - py3.14",
    }
    # ...and each one is restored before the single upload at the end.
    restores = [line for line in deploy["script"] if "--restore" in line]
    assert len(restores) == 2, restores
    assert deploy["script"][-1].endswith("--upload"), (
        "one upload after the whole set is restored: `conan upload <ref>` "
        "publishes every package id, so uploading per tarball would push the "
        "same recipe once per tarball"
    )


def test_gitlab_linux_export_tarballs_are_named_per_configuration(tmp_path):
    """Two build jobs sharing an export name would overwrite each other.

    They upload into one artifact space, so a shared name leaves whichever
    finished last as the only tarball the deploy can restore -- and the deploy
    would still look like it succeeded, having restored *a* package and
    uploaded it. The name is the configuration's whole label, not just its ABI,
    so it stays unique if a build type or an option ever splits the exporting
    set along an axis that is not the interpreter.
    """
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, linux_python_versions=["3.13", "3.14"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    names = _linux_export_names(content)
    assert len(names) == len(set(names)) == 2, names
    for label in ("Release-pybind-py3.13", "Release-pybind-py3.14"):
        expected = f".export/xmssnap-linux-{label}-${{PACKAGE_VERSION}}.tar.gz"
        assert expected in names, (expected, names)


def test_gitlab_single_linux_version_still_qualifies_its_export_tarballs(tmp_path):
    """The save and the restore have to spell the same name, fan-out or not.

    With one ABI there is one exporting job, so nothing would collide on a bare
    name -- but the deploy restores by name, and a build that saved a
    label-qualified tarball while the deploy asked for a bare one would fail
    only on a tag, in the job that publishes. The label is unconditional so the
    two ends cannot disagree.
    """
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY,
                                  linux_python_versions=["3.13"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    names = _linux_export_names(content)
    assert names == [".export/xmssnap-linux-Release-pybind-py3.13-${PACKAGE_VERSION}.tar.gz"]

    parsed = yaml.safe_load(content)
    restores = [line for line in parsed["Conan Deploy - Linux"]["script"]
                if "--restore" in line]
    assert len(restores) == 1 and names[0] in restores[0], (restores, names)


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
        tmp_path,
        matrix_table=WHEEL_ONLY, split_tests=True, linux_python_versions=["3.14"],
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    # split_tests still splits, and the single-version build stays a plain job.
    assert "Run C++ Tests - Debug-testing" in parsed
    assert "parallel" not in parsed["Debug Build"]


def test_gitlab_coverage_image_uses_the_coverage_python_version(tmp_path):
    """Coverage pins one ABI, so it tracks the coverage version, not the fan-out."""
    toml_file = write_gitlab_toml(
        tmp_path,
        matrix_table=WHEEL_ONLY, coverage=True, linux_python_versions=["3.13", "3.14"],
        coverage_table={"python_version": "3.13"},
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    jobs = _instrumented_build_jobs(parsed)
    assert jobs, "coverage = true must emit at least one instrumented build job"
    for name, job in jobs.items():
        assert job["image"].endswith("conan-gcc13-py3.13"), name


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


def _gitlab_jobs(tmp_path, **ci_flags):
    """Render a GitLab pipeline and return its parsed YAML."""
    toml_file = write_gitlab_toml(tmp_path, **ci_flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    return yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def test_gitlab_windows_build_stages_a_wheel(tmp_path):
    """The Windows build job asks build.py for a wheel and keeps it as an artifact.

    Without both halves the wheel is built and then discarded when the job ends,
    which is what left Windows with no publishable wheel.
    """
    job = _gitlab_jobs(tmp_path, windows=True)["Conan Build - Windows"]
    # The wheels alone: `when: always` on the whole directory would upload the
    # several hundred DLLs the repair staging leaves in wheelhouse/libs.
    assert "wheelhouse/*.whl" in job["artifacts"]["paths"]
    assert "wheelhouse/" not in job["artifacts"]["paths"]
    assert any("--wheel-dir wheelhouse" in step for step in job["script"])


def test_gitlab_windows_build_repairs_its_own_wheel(tmp_path):
    """An opted-in Windows build job repairs in place, naming the windows platform.

    delvewheel reads the DLL imports of a win_amd64 .pyd, so the repair cannot be
    delegated to the manylinux container that repairs the Linux wheel. GitLab
    libraries do not repair by default, so this asks for it explicitly.
    """
    job = _gitlab_jobs(
        tmp_path, windows=True, windows_wheel_repair=True
    )["Conan Build - Windows"]
    assert any(
        "xmsconan_wheel_repair" in step and "--platform windows" in step
        for step in job["script"]
    )


def test_gitlab_windows_repair_can_be_switched_off(tmp_path):
    """windows_wheel_repair = false drops the repair step and the libs collection.

    delvewheel's ignore list excuses vcruntime140 for a cp3xx wheel but not
    msvcp140, so a .pyd with no third-party imports still gets a mangled CRT
    vendored beside it -- overriding the runtime XMS deliberately supplies to
    the Python process. A library with nothing to vendor skips the step, and
    with it the ~800-DLL collect_dependency_libs pass that only feeds it.
    """
    job = _gitlab_jobs(tmp_path, windows=True, windows_wheel_repair=False)["Conan Build - Windows"]

    assert not any("xmsconan_wheel_repair" in step for step in job["script"])
    # The wheel is still staged and still deployed; only the repair is gone.
    assert any("--wheel-dir wheelhouse" in step for step in job["script"])
    assert any("--skip-dependency-libs" in step for step in job["script"])


def test_gitlab_windows_repair_is_off_by_default(tmp_path):
    """A GitLab library does not repair its Windows wheel unless it asks to.

    GitLab wheels are internal, published to AquaPi, and loaded only by the XMS
    Python, which supplies the C++ runtime on PATH deliberately. delvewheel does
    not ignore msvcp140.dll, so repairing such a wheel vendors a private mangled
    copy of the very runtime the host is controlling. These pipelines also staged
    no Windows wheel at all before this feature, so there is no prior repairing
    behavior to preserve.
    """
    job = _gitlab_jobs(tmp_path, windows=True)["Conan Build - Windows"]

    assert not any("xmsconan_wheel_repair" in step for step in job["script"])
    assert any("--skip-dependency-libs" in step for step in job["script"])


def test_gitlab_windows_wheel_deploy_survives_a_skipped_repair(tmp_path):
    """The unrepaired wheel is what gets uploaded; the deploy job stays."""
    pipeline = _gitlab_jobs(tmp_path, windows=True, windows_wheel_repair=False)

    assert any("xmsconan_wheel_deploy" in step
               for step in pipeline["Wheel Deploy - Windows"]["script"])


def _github_windows_job(tmp_path, **ci_flags):
    """Render the GitHub workflow and return its parsed Windows build job."""
    toml_file = write_github_toml(tmp_path, **ci_flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    workflow = (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").read_text(
        encoding="utf-8"
    )
    jobs = yaml.safe_load(workflow)["jobs"]
    # Selected by job key: every platform job renders `runs-on: ${{
    # matrix.platform }}`, so the runner string cannot tell them apart.
    assert "windows" in jobs, f"no windows job in {sorted(jobs)}"
    return jobs["windows"], jobs


def _step_runs(job):
    """Return the ``run:`` text of every step in a workflow job."""
    return [str(step.get("run", "")) for step in job["steps"]]


def test_github_windows_repair_is_on_by_default(tmp_path):
    """The Windows step must exist, asserted on the Windows job itself.

    A whole-file substring check cannot make this claim: the Linux and macOS
    steps also run xmsconan_wheel_repair, so the Windows step could vanish
    entirely without the assertion noticing.
    """
    windows, _jobs = _github_windows_job(tmp_path)
    runs = _step_runs(windows)

    assert any("--platform windows" in run for run in runs)
    assert not any("--skip-dependency-libs" in run for run in runs)


def test_github_windows_repair_can_be_switched_off(tmp_path):
    """The GitHub windows job honors the same key, for the same reason."""
    windows, jobs = _github_windows_job(tmp_path, windows_wheel_repair=False)
    runs = _step_runs(windows)

    assert not any("--platform windows" in run for run in runs)
    assert any("--skip-dependency-libs" in run for run in runs)
    # Linux and macOS repair is untouched: a manylinux wheel has to be repaired,
    # and their build steps must not have grown the skip flag either.
    other_runs = [
        run for name, job in jobs.items() if name != "windows"
        for run in _step_runs(job)
    ]
    assert any("--platform linux" in run for run in other_runs)
    assert any("--platform macos" in run for run in other_runs)
    assert not any("--skip-dependency-libs" in run for run in other_runs)


#: The build step's wheel request, as the GitHub expression that gates it.
#:
#: Spelled out here rather than imported from ci_file_generator so the
#: assertions below compare the rendered workflow against a literal. Importing
#: the constant would assert the template against its own input, which stays
#: green through any change to the expression itself.
RELEASE_GATED_WHEEL_DIR = (
    "${{ matrix.build_type == 'Release' && ' --wheel-dir wheelhouse' || '' }}"
)

#: The step that runs build.py, in every platform job.
BUILD_STEP_NAME = "Build the Conan Packages"

#: The GitHub jobs that build the library, as opposed to linting it.
BUILDING_JOBS = ("mac", "linux", "linux-arm", "windows")

#: Every (job, step) that reads the wheelhouse the build step fills.
WHEEL_CONSUMING_STEPS = sorted(
    (job, step)
    for job in BUILDING_JOBS
    for step in ("Repair wheel", "Upload wheel artifact", "Upload wheel to Aquapi")
)


@pytest.fixture
def github_arm_jobs(tmp_path):
    """The parsed jobs of a GitHub workflow with every platform job present."""
    return _github_jobs(write_github_toml(tmp_path, linux_arm=True), tmp_path)


def _is_build_step(step):
    """Whether a step compiles the library, rather than consuming what it built.

    Matched on the prefix because a job may have more than one: the Windows job
    emits a branch step and a tag step under mutually exclusive ``if:``
    conditions, the tag one narrowing the filter to the configurations a
    release publishes. It runs under ``shell: cmd``, where ``%VAR%`` expands
    before the argument is parsed, so a filter held in a variable would break
    its own quoting -- the other three legs carry one step and select the
    filter with a ``env:`` expression instead.
    """
    return str(step.get("name", "")).startswith(BUILD_STEP_NAME)


def _build_step_run(job, job_name):
    """Return the ``run:`` text of one job's build step.

    The first, which is the branch-pipeline one wherever a job has two.
    """
    for step in job["steps"]:
        if _is_build_step(step):
            return str(step["run"])
    raise AssertionError(f"no {BUILD_STEP_NAME!r} step in the {job_name} job")


def _touches_the_wheelhouse(step):
    """Whether a step names the wheelhouse, in its command or its inputs.

    Matched on the directory rather than on a list of known commands, so a
    wheel step added later under a different spelling is covered too. The
    artifact upload is a ``uses:`` step that names it only in ``with.path``.
    """
    text = str(step.get("run", ""))
    text += " ".join(str(value) for value in step.get("with", {}).values())
    return "wheelhouse" in text


def test_github_every_platform_job_builds(github_arm_jobs):
    """The build step exists on each platform job, and only on those."""
    building = [
        name for name, job in github_arm_jobs.items()
        if any(_is_build_step(step) for step in job.get("steps", []))
    ]
    assert sorted(building) == sorted(BUILDING_JOBS)


@pytest.mark.parametrize("job_name", BUILDING_JOBS)
def test_github_build_step_requests_a_wheel_only_on_the_release_leg(github_arm_jobs, job_name):
    """The build step asks for a wheel dir on the Release leg and only there.

    build.py exits 1 when --wheel-dir extracts no complete set of wheels, and
    [matrix].pybind_build_types defaults to Release only -- so an unguarded
    --wheel-dir fails every Debug leg of every repository on the default
    configuration. Parametrized by job so a failure names the platform, and
    with the guard removed from the text so a second, unguarded request cannot
    hide behind the guarded one.
    """
    run = _build_step_run(github_arm_jobs[job_name], job_name)
    assert RELEASE_GATED_WHEEL_DIR in run
    assert "--wheel-dir" not in run.replace(RELEASE_GATED_WHEEL_DIR, "")


def test_github_wheel_steps_stay_release_only(github_arm_jobs):
    """Every step that reads the wheelhouse runs only where one is filled.

    The guard above is only correct while this holds; if a consuming step lost
    its ``if:`` it would run on a Debug leg with no wheelhouse at all. The
    build step is excluded because it is the step that *creates* the
    wheelhouse, and its own Release gate lives inside the run text rather than
    in an ``if:``.
    """
    wheel_steps = [
        (name, step.get("name"), str(step.get("if", "")))
        for name, job in github_arm_jobs.items()
        for step in job.get("steps", [])
        if not _is_build_step(step) and _touches_the_wheelhouse(step)
    ]

    assert sorted((name, step) for name, step, _ in wheel_steps) == WHEEL_CONSUMING_STEPS
    for name, step, condition in wheel_steps:
        assert "matrix.build_type == 'Release'" in condition, (name, step)


def test_gitlab_windows_wheel_deploy_exists_and_is_tag_only(tmp_path):
    """A Windows wheel reaches devpi, and only from a tag."""
    job = _gitlab_jobs(tmp_path, windows=True)["Wheel Deploy - Windows"]
    assert any("xmsconan_wheel_deploy" in step for step in job["script"])
    assert job["only"] == ["tags"]
    assert job["needs"] == [{"job": "Conan Build - Windows", "artifacts": True}]


def test_gitlab_windows_wheel_deploy_absent_without_windows(tmp_path):
    """No Windows jobs at all when the platform is switched off."""
    pipeline = _gitlab_jobs(tmp_path, windows=False)
    assert "Wheel Deploy - Windows" not in pipeline
    assert "Conan Build - Windows" not in pipeline


def test_gitlab_windows_wheel_deploy_absent_without_deploy(tmp_path):
    """Setting deploy = false suppresses the Windows wheel upload with every other deploy."""
    pipeline = _gitlab_jobs(tmp_path, windows=True, deploy=False)
    assert "Wheel Deploy - Windows" not in pipeline
    assert "Conan Build - Windows" in pipeline


def test_gitlab_windows_only_pipeline_publishes_a_wheel(tmp_path):
    """A Windows-only pipeline publishes a wheel.

    The direct inverse of the old documented gap, where dropping Linux dropped
    the only path that staged and uploaded a wheel.
    """
    pipeline = _gitlab_jobs(tmp_path, linux=False, windows=True)
    assert "Repair Wheel" not in pipeline  # the Linux-only Package-stage job
    assert "Wheel Deploy" not in pipeline  # the Linux-only deploy job
    deploy = pipeline["Wheel Deploy - Windows"]
    assert any("xmsconan_wheel_deploy" in step for step in deploy["script"])
    assert deploy["stage"] in pipeline["stages"]


def test_github_pybind_build_types_without_release_is_rejected(tmp_path):
    """A GitHub library that excludes Release from pybind publishes no wheel.

    Every wheel step in the GitHub workflow is gated on
    ``matrix.build_type == 'Release'``. On Linux and macOS the Debug leg builds
    a wheel and discards it; on Windows it builds none at all. Either way the
    Release leg dies inside xmsconan_wheel_repair with "No .whl files found".
    Rejected at generation, like the other impossible combinations.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '[matrix]\n'
        'pybind_build_types = ["Debug"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pybind_build_types"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))


def test_github_pybind_build_types_with_release_is_allowed(tmp_path):
    """Adding Debug alongside Release is fine -- the Release leg still publishes."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "github"\n'
        '[matrix]\n'
        'pybind_build_types = ["Release", "Debug"]\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    assert (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").exists()


# --- third-party action pinning ---


#: `uses: owner/repo@ref` in a rendered GitHub workflow. The repo half accepts
#: `/` because a composite action is referenced as `owner/repo/subdir@ref`, which
#: the stricter `[\w.-]+` matched not at all -- such a line would have passed the
#: pin check by being invisible to it rather than by being pinned. No template
#: emits one today; this is so the check still applies when one does.
_USES_RE = re.compile(r"uses:\s+([\w.-]+)/([\w./-]+)@(\S+)")

#: Owners whose actions may be referenced by tag. `actions/*` is GitHub's own
#: namespace: a compromise there is a compromise of the runner regardless of
#: how this workflow spells the reference.
_UNPINNED_OWNERS = frozenset({"actions"})


def _third_party_uses_lines(content):
    """Return the rendered ``uses:`` lines that name a third-party action."""
    return [
        line
        for line in content.splitlines()
        if (match := _USES_RE.search(line)) and match.group(1) not in _UNPINNED_OWNERS
    ]


def _unpinned_third_party_actions(content):
    """Return the `owner/repo@ref` references that are not commit SHAs."""
    return [
        f"{owner}/{repo}@{ref}"
        for owner, repo, ref in _USES_RE.findall(content)
        if owner not in _UNPINNED_OWNERS and not re.fullmatch(r"[0-9a-f]{40}", ref)
    ]


def test_github_ci_pins_third_party_actions_to_a_sha(ci_toml, tmp_path):
    """Third-party actions are referenced by commit SHA, not by tag.

    A tag is a movable ref in a repository we do not control: its owner can
    retarget it at new code, which then runs in this job with the workflow's
    Conan and devpi secrets in its environment. The tag stays in a trailing
    comment so the reference is still readable.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").read_text(
        encoding="utf-8",
    )

    third_party = _third_party_uses_lines(content)

    assert third_party
    assert _unpinned_third_party_actions(content) == []
    # And the pins are still legible -- a bare SHA nobody can place is how a
    # pinned workflow ends up frozen on an action three years stale. Checked per
    # line: `"  # v" in content` was satisfied by any one commented pin anywhere
    # in the file, including a comment on a line with no `uses:` at all.
    assert [line for line in third_party if "  # v" not in line] == []


def test_github_coverage_pins_third_party_actions_to_a_sha(tmp_path):
    """The coverage workflow gets the same treatment as the CI workflow."""
    toml_file = write_github_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "Coverage.yaml").read_text(
        encoding="utf-8",
    )
    third_party = _third_party_uses_lines(content)

    # Same three assertions as its CI twin. On its own, `== []` is satisfied by
    # a Coverage.yaml carrying no `uses:` line at all.
    assert third_party
    assert _unpinned_third_party_actions(content) == []
    assert [line for line in third_party if "  # v" not in line] == []


def test_github_ci_uses_a_current_setup_python(ci_toml, tmp_path):
    """Every setup-python is v5.

    The flake job sat on v2 while the build jobs used v5, so the one job that
    lints the project ran on a Node action GitHub has since deprecated -- and
    would have started failing on its own schedule, in the job least likely
    to be looked at.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").read_text(
        encoding="utf-8",
    )

    assert "actions/setup-python@v5" in content
    assert "actions/setup-python@v2" not in content


def test_github_flake_job_installs_the_banned_modules_plugin(ci_toml, tmp_path):
    """The flake job installs the plugin that `banned-modules` needs.

    The .flake8 this job generates sets `banned-modules = osgeo.*`, an option
    only flake8-tidy-imports registers. flake8 ignores options no installed
    plugin claims, so the ban was accepted and enforced nothing while the job
    reported the same green as a run that had checked it. GitLab already
    installed the plugin, so the two pipelines linted to different rules.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").read_text(
        encoding="utf-8",
    )

    assert "flake8-tidy-imports" in content


def test_github_coverage_pins_conan_version(tmp_path):
    """The coverage workflow pins conan to the same series as the CI workflow.

    A conan minor bump can change package_id computation; a coverage run that
    resolves different package ids than the build workflow is measuring a
    different set of binaries.
    """
    toml_file = write_github_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "Coverage.yaml").read_text(
        encoding="utf-8",
    )

    conan_installs = _conan_install_lines(content)

    assert conan_installs
    assert [line for line in conan_installs if '"conan~=' not in line] == []


def test_gitlab_windows_cache_snapshot_reports_an_empty_copy(tmp_path):
    """The cache-snapshot copy says so when it finds nothing.

    `|| true` made a failed copy indistinguishable from a successful one, so
    a runner whose user is not `admin` shipped an empty conan_packages/
    artifact with nothing in the log. It stays non-fatal -- the upload has
    already succeeded by then and nothing consumes the artifact.
    """
    toml_file = write_gitlab_toml(tmp_path, windows=True, deploy=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert "conan_packages/ || true" not in content
    assert "conan_packages/ || echo" in content


# --- [filter] table -> CI matrix, wheel steps, and warnings ---


_FILTERED_TOML = """\
library_name = "xmscore"
description = "Core library"
python_namespaced_dir = "core"
ci_type = "{ci_type}"
{ci_table}{filter_table}"""

_RELEASE_ONLY_FILTER = """
[filter]
build_type = "Release"
"""

_NO_PYBIND_FILTER = """
[filter.options]
pybind = false
"""

_LINUX_ARM_CI_TABLE = """
[ci]
linux_arm = true
"""

_COVERAGE_CI_TABLE = """
[ci]
coverage = true
"""


def _write_filtered_toml(tmp_path, ci_type="github", ci_table="", filter_table=_RELEASE_ONLY_FILTER):
    """Write a build.toml carrying a [filter] table."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        _FILTERED_TOML.format(ci_type=ci_type, ci_table=ci_table, filter_table=filter_table),
        encoding="utf-8",
    )
    return toml_file


def _github_workflow(tmp_path):
    """Parse the generated GitHub workflow."""
    path = tmp_path / ".github" / "workflows" / "XmsCore-CI.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")), path.read_text(encoding="utf-8")


def _build_type_matrices(workflow):
    """Map job name to its build_type matrix axis, for jobs that have one."""
    return {
        name: job["strategy"]["matrix"]["build_type"]
        for name, job in workflow["jobs"].items()
        if "build_type" in job.get("strategy", {}).get("matrix", {})
    }


def test_github_matrix_fans_out_over_both_build_types_by_default(ci_toml, tmp_path):
    """With no [filter] table every build job keeps the Release + Debug matrix."""
    generate_ci(str(ci_toml), "1.0.0", str(tmp_path))

    workflow, _ = _github_workflow(tmp_path)
    matrices = _build_type_matrices(workflow)
    assert matrices, "no job carries a build_type matrix"
    assert all(types == ["Release", "Debug"] for types in matrices.values()), matrices


def test_github_matrix_narrows_to_pinned_build_type(tmp_path):
    """A pinned build_type drops the CI legs that could only build nothing."""
    toml_file = _write_filtered_toml(tmp_path, ci_table=_LINUX_ARM_CI_TABLE)
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    workflow, _ = _github_workflow(tmp_path)
    matrices = _build_type_matrices(workflow)
    # Every emitted job block — mac, linux, linux-ARM, windows — follows it.
    assert len(matrices) == 4, matrices
    assert all(types == ["Release"] for types in matrices.values()), matrices


def test_invalid_filter_table_fails_ci_generation(tmp_path):
    """`xmsconan ci` rejects the same bad filters as `xmsconan gen`."""
    toml_file = _write_filtered_toml(tmp_path, filter_table="\n[filter]\npybind = true\n")
    with pytest.raises(ValueError, match=r"Invalid \[filter\] table in build.toml"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))


# --- wheel steps ---


def test_github_keeps_wheel_steps_by_default(ci_toml, tmp_path):
    """An unfiltered library still repairs and uploads its wheel."""
    generate_ci(str(ci_toml), "1.0.0", str(tmp_path))

    _, content = _github_workflow(tmp_path)
    assert "xmsconan_wheel_repair" in content
    assert "xmsconan_wheel_deploy" in content
    assert "--wheel-dir wheelhouse" in content


def test_github_drops_wheel_steps_when_pybind_filtered_off(tmp_path):
    """A library that builds no pybind config gets no wheel steps.

    xmsconan_wheel_repair raises on an empty wheelhouse, and the repair step is
    gated on build_type only — so leaving it in reddens every Release leg.
    """
    toml_file = _write_filtered_toml(tmp_path, filter_table=_NO_PYBIND_FILTER)
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    workflow, content = _github_workflow(tmp_path)
    assert "xmsconan_wheel_repair" not in content
    assert "xmsconan_wheel_deploy" not in content
    assert "--wheel-dir" not in content, "build.py would warn about a wheel nobody wants"
    assert workflow["jobs"], "the rest of the pipeline survives"


def test_gitlab_drops_wheel_jobs_when_pybind_filtered_off(tmp_path):
    """The GitLab wheel work is whole jobs, not steps, so those come out entirely."""
    toml_file = _write_filtered_toml(tmp_path, ci_type="gitlab", filter_table=_NO_PYBIND_FILTER)
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    parsed = yaml.safe_load((tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert "Repair Wheel" not in parsed
    assert "Wheel Deploy" not in parsed
    assert "Conan Build" in parsed, "the rest of the pipeline survives"


_SPLIT_TESTS_CI_TABLE = """
[ci]
split_tests = true
"""


def test_gitlab_test_jobs_follow_a_build_type_pin(tmp_path):
    """A pinned build_type leaves one testing configuration, so one test job."""
    toml_file = _write_filtered_toml(
        tmp_path,
        ci_type="gitlab",
        ci_table=_SPLIT_TESTS_CI_TABLE,
        filter_table=_RELEASE_ONLY_FILTER,
    )
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    parsed = yaml.safe_load((tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert _cxx_test_jobs(parsed).keys() == {"Run C++ Tests - Release-testing"}


def test_gitlab_test_jobs_survive_a_pybind_pin_that_keeps_no_runner(tmp_path):
    """split_tests with nothing to test is rejected, not silently green.

    A pybind-only filter still leaves ``ci_build_types`` non-empty -- Release
    keeps configurations -- so deriving the test jobs from that axis would emit
    a job looking for a runner the build never staged. The build job has already
    exported XMS_SKIP_CXX_TESTS=1 by then, so the suite would not have run
    anywhere.
    """
    toml_file = _write_filtered_toml(
        tmp_path,
        ci_type="gitlab",
        ci_table=_SPLIT_TESTS_CI_TABLE,
        filter_table="\n[filter.options]\npybind = true\n",
    )
    with pytest.raises(ValueError, match="no Linux testing configuration"):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))


def test_gitlab_drops_windows_wheel_work_when_pybind_filtered_off(tmp_path):
    """The Windows leg's wheel work goes too, and the Conan deploys stay.

    Windows repairs its wheel in place inside the build job rather than in a
    separate Package job, so its --wheel-dir, its repair step, and the
    "Wheel Deploy - Windows" job each need the same gate the Linux jobs get.
    build.py exits 1 when --wheel-dir extracts no complete set of wheels, so an
    un-gated --wheel-dir here is a red pipeline on every branch. The two
    "Conan Deploy" jobs publish packages rather than wheels and must survive.
    """
    toml_file = _write_filtered_toml(
        tmp_path, ci_type="gitlab",
        ci_table="\n[ci]\ndeploy = true\nwindows_wheel_repair = true\n",
        filter_table=_NO_PYBIND_FILTER,
    )
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    content = (tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    assert "--wheel-dir" not in content, "build.py exits 1 when no wheel is extracted"
    assert "xmsconan_wheel_repair" not in content
    assert "xmsconan_wheel_deploy" not in content
    assert "Wheel Deploy - Windows" not in parsed
    assert "Conan Build - Windows" in parsed, "the Windows build itself survives"
    assert "Conan Deploy - Windows" in parsed, "package deploy is not wheel work"
    assert "Conan Deploy - Linux" in parsed, "package deploy is not wheel work"


def test_gitlab_keeps_windows_wheel_work_by_default(tmp_path):
    """Unfiltered, the Windows wheel chain is intact -- the gate is opt-in only."""
    toml_file = _write_filtered_toml(
        tmp_path, ci_type="gitlab",
        ci_table="\n[ci]\ndeploy = true\nwindows_wheel_repair = true\n",
        filter_table="",
    )
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    content = (tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    assert "--wheel-dir wheelhouse" in content
    assert "--platform windows" in content, "the in-place repair step is still emitted"
    assert "Wheel Deploy - Windows" in parsed


def test_gitlab_keeps_wheel_jobs_by_default(tmp_path):
    """The unfiltered GitLab pipeline still carries both wheel jobs."""
    toml_file = _write_filtered_toml(tmp_path, ci_type="gitlab", filter_table="")
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    parsed = yaml.safe_load((tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert "Repair Wheel" in parsed
    assert "Wheel Deploy" in parsed


# --- generation-time warnings ---


def test_warns_about_job_the_filter_empties(tmp_path, caplog):
    """os/arch/compiler are fixed per job block, so pinning one empties whole jobs."""
    toml_file = _write_filtered_toml(
        tmp_path, filter_table='\n[filter]\nos = "Windows"\n',
    )

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    warned = [r.getMessage() for r in caplog.records if "empty matrix" in r.getMessage()]
    assert len(warned) == 2, warned  # mac + linux; linux-ARM is opt-in, windows matches
    assert any("'mac'" in message for message in warned), warned
    assert any("'linux'" in message for message in warned), warned


def test_no_empty_job_warning_for_a_job_ci_turned_off(tmp_path, caplog):
    """A filter cannot empty the GitLab Linux job when [ci].linux never wrote it.

    An os = "Windows" pin excludes everything the Linux "Conan Build" job builds,
    but that job is generated only under [ci].linux -- so warning about it points
    at a job that is not in the pipeline.
    """
    toml_file = _write_filtered_toml(
        tmp_path, ci_type="gitlab", ci_table="\n[ci]\nlinux = false\n",
        filter_table='\n[filter]\nos = "Windows"\n',
    )

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    warned = [r.getMessage() for r in caplog.records if "empty matrix" in r.getMessage()]
    assert not any("Conan Build'" in message for message in warned), warned


def test_no_empty_job_warning_for_build_type_pin(tmp_path, caplog):
    """build_type is narrowed rather than warned about."""
    toml_file = _write_filtered_toml(tmp_path)

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    assert [r.getMessage() for r in caplog.records if "empty matrix" in r.getMessage()] == []


def test_coverage_job_warns_about_conflicting_filter(tmp_path, caplog):
    """A Release-only filter can't satisfy the coverage job, so generation warns."""
    toml_file = _write_filtered_toml(tmp_path, ci_table=_COVERAGE_CI_TABLE)

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    warned = [r.getMessage() for r in caplog.records if "xmsconan coverage" in r.getMessage()]
    assert len(warned) == 1, warned
    assert "Debug" in warned[0]


def test_coverage_warning_covers_the_inclusion_direction(tmp_path, caplog):
    """Requiring pybind cancels the C++ coverage build just as excluding it cancels Python."""
    toml_file = _write_filtered_toml(
        tmp_path, ci_table=_COVERAGE_CI_TABLE,
        filter_table="\n[filter.options]\npybind = true\n",
    )

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    warned = [r.getMessage() for r in caplog.records if "xmsconan coverage" in r.getMessage()]
    assert len(warned) == 1, warned
    assert "C++" in warned[0]


def test_coverage_warning_uses_the_resolved_coverage_python_version(tmp_path, caplog):
    """The Python coverage build pins one ABI; [coverage].python_version picks it."""
    toml_file = _write_filtered_toml(
        tmp_path,
        # 3.10 has to be a version CI builds, or the filter is rejected outright.
        ci_table=_COVERAGE_CI_TABLE + 'python_versions = ["3.10", "3.13"]\n'
        '\n[coverage]\npython_version = "3.13"\n',
        filter_table='\n[filter.options]\npython_version = "3.10"\n',
    )

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    warned = [r.getMessage() for r in caplog.records if "xmsconan coverage" in r.getMessage()]
    assert len(warned) == 1, warned
    assert "python_version" in warned[0]


def test_no_coverage_warning_without_coverage_job(tmp_path, caplog):
    """The same filter is silent when no coverage job is generated."""
    toml_file = _write_filtered_toml(tmp_path)

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    assert [r.getMessage() for r in caplog.records if "xmsconan coverage" in r.getMessage()] == []


#: A [ci] table switching Windows off, and a filter only the Linux job matches.
CI_WINDOWS_FALSE = "\n[ci]\nwindows = false\n"
FILTER_COMPILER_GCC = '\n[filter]\ncompiler = "gcc"\n'


def test_github_still_warns_about_windows_when_ci_windows_is_false(tmp_path, caplog):
    """[ci].windows is GitLab-only, so it must not silence the GitHub warning.

    The GitHub template has no job gate for [ci].windows -- generate_ci warns
    that the key is ignored -- so the windows job is emitted whatever it says.
    Treating it as a toggle here dropped the job from the accounting and left a
    filter that empties it unreported.
    """
    toml_file = _write_filtered_toml(
        tmp_path, ci_table=CI_WINDOWS_FALSE,
        filter_table=FILTER_COMPILER_GCC,
    )

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    workflow, _ = _github_workflow(tmp_path)
    assert "windows" in workflow["jobs"], "the GitHub template ignores [ci].windows"

    warned = [r.getMessage() for r in caplog.records if "empty matrix" in r.getMessage()]
    assert len(warned) == 2, warned  # mac (apple-clang) and windows (msvc)
    assert any("'mac'" in message for message in warned), warned
    assert any("'windows'" in message for message in warned), warned


def test_gitlab_warns_only_about_jobs_the_ci_toggles_emit(tmp_path, caplog):
    """The same job accounting applies to the GitLab job names."""
    toml_file = _write_filtered_toml(
        tmp_path, ci_type="gitlab", ci_table="\n[ci]\nwindows = false\n",
        filter_table='\n[filter]\ncompiler = "msvc"\n',
    )

    with caplog.at_level(logging.WARNING):
        generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    warned = [r.getMessage() for r in caplog.records if "empty matrix" in r.getMessage()]
    assert len(warned) == 1, warned  # the Linux build job; the Windows one is off
    assert "'Conan Build'" in warned[0]


def test_github_ci_build_step_shards_the_suite(tmp_path):
    """[ci].test_shards reaches build.py on every GitHub platform job.

    GitHub has no split-test job -- the runner that built the package is the
    one that tests it -- so the shard count goes to build.py, which skips
    cmake.test() during the build and then runs N in-process gtest shards.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'test_shards = 4\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    workflow = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = workflow.read_text(encoding="utf-8")

    build_steps = [line for line in content.splitlines()
                   if "--artifacts-dir test_artifacts" in line]
    assert build_steps
    assert all("--test-shards 4" in line for line in build_steps)
    # The upload-only invocation builds nothing, so a shard count there would
    # be noise at best and a skipped-test claim at worst.
    upload_steps = [line for line in content.splitlines()
                    if "--skip-build --upload" in line]
    assert upload_steps
    assert all("--test-shards" not in line for line in upload_steps)


def test_github_ci_omits_the_shard_flag_when_unset(ci_toml, tmp_path):
    """Without [ci].test_shards the build command is unchanged.

    ctest keeps its own parallelism in that case; adding `--test-shards 1`
    would route the suite through the shard path for no benefit.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    workflow = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = workflow.read_text(encoding="utf-8")

    assert "--artifacts-dir test_artifacts" in content
    assert "--test-shards" not in content


def test_github_ci_shard_flag_needs_more_than_one_shard(tmp_path):
    """test_shards = 1 is the same as not asking for shards."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        'test_shards = 1\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    workflow = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = workflow.read_text(encoding="utf-8")

    assert "--test-shards" not in content


#: A single backslash, named so the escaping assertions below do not have
#: to contain one -- an escape sequence in a test about escape sequences is
#: exactly where an off-by-one level of quoting hides.
BACKSLASH = chr(92)


def _gate(job):
    """How a GitLab job is gated: "always", "only:<refs>" or "except:<refs>"."""
    if "only" in job:
        return "only:" + ",".join(job["only"])
    if "except" in job:
        return "except:" + ",".join(job["except"])
    return "always"


def _linux_build_gates(parsed):
    """Each Linux build job's name mapped to its gate, in pipeline order."""
    return {name: _gate(job) for name, job in parsed.items()
            if isinstance(job, dict) and job.get("needs") == []}


def test_gitlab_tag_pipeline_skips_the_testing_configurations(tmp_path):
    """A tag builds what it publishes, and nothing installs a test runner.

    The pipeline used to compile the testing configurations on a tag as well,
    because one build job looped the whole matrix and had no way to build part
    of it. A job per configuration can be gated per configuration, and the test
    binaries are the part a release has no use for.
    """
    gates = _linux_build_gates(_gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, coverage=True))

    assert gates["Release Build"] == "except:tags", (
        "the Release testing configuration is branch-only: its consumer, the "
        "C++ test job, is branch-only too"
    )
    assert gates["Debug Instrumented Build"] == "except:tags", (
        "and so is the Debug testing configuration, which is additionally "
        "instrumented for a Coverage stage that does not run on a tag"
    )


def test_gitlab_tag_pipeline_keeps_the_configurations_it_publishes(tmp_path):
    """What a release ships still runs on the tag that ships it.

    The narrowing drops the testing configurations, which nothing installs. It
    must not drop the binary the wheel wraps -- a tag that built none of the
    matrix would publish an empty release and still go green. Under wheel_only
    the pybind build is the whole of what a tag publishes, and it reaches the
    tag as the clean twin of the instrumented branch build.
    """
    gates = _linux_build_gates(_gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, coverage=True))

    assert gates["Python Build"] == "only:tags"
    assert any(gate != "except:tags" for gate in gates.values()), (
        "a tag pipeline with every build job gated off would publish nothing"
    )


def test_gitlab_instrumented_pybind_never_reaches_a_tag(tmp_path):
    """Instrumentation is in the package_id, so it must not be published.

    The pybind configuration is emitted twice under mutually exclusive gates
    rather than once with a runtime conditional, so exactly one of them exists
    in any given pipeline and no shell branch decides which binary ships.
    """
    parsed = _gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, coverage=True)
    instrumented = parsed["Python Instrumented Build"]
    clean = parsed["Python Build"]

    assert _gate(instrumented) == "except:tags"
    assert _gate(clean) == "only:tags"
    assert any("--phase measure" in step for step in instrumented["script"])
    assert not any("--phase measure" in step for step in clean["script"]), (
        "the published wheel is built by build.py directly, with no coverage "
        "option in its package_id"
    )


def test_gitlab_only_taggable_jobs_save_an_export_tarball(tmp_path):
    """A branch-only job's tarball would never be restored.

    The deploy that restores them is ``only: tags``, so exporting from a
    branch-only job would pay the artifact upload on every branch pipeline for
    a file nothing ever reads.
    """
    parsed = _gitlab_jobs(tmp_path, coverage=True)
    for name, job in parsed.items():
        if not isinstance(job, dict) or job.get("needs") != []:
            continue
        saves = any("--save .export/" in step for step in job.get("script", []))
        assert saves == (_gate(job) != "except:tags"), (name, _gate(job), saves)


def test_gitlab_windows_tag_pipeline_narrows_its_filter(tmp_path):
    """Windows narrows the same way, through a rules-selected variable.

    Not through two jobs the way Linux does: this is still one job, and
    splitting it would mean either duplicating forty lines or renaming it --
    and "Conan Build - Windows" is the name both Windows deploy jobs point at.
    """
    job = _gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, windows=True)["Conan Build - Windows"]

    # The branch default matches every configuration, so the build line can
    # pass --filter unconditionally and quoted rather than relying on an empty
    # variable disappearing through unquoted word splitting.
    assert job["variables"]["BUILD_MATRIX_FILTER"] == '{"options":{}}'
    assert job["rules"][0]["if"] == "$CI_COMMIT_TAG"
    assert job["rules"][0]["variables"]["BUILD_MATRIX_FILTER"] == (
        '{"options":{"testing":false}}'
    )
    assert job["rules"][-1] == {"when": "on_success"}, (
        "without a catch-all rule a non-tag pipeline would drop the job"
    )
    build = [step for step in job["script"] if "build.py" in step]
    assert build == [step for step in build if '--filter "${BUILD_MATRIX_FILTER}"' in step]


def test_github_build_legs_narrow_their_filter_on_a_tag(tmp_path):
    """The three bash legs select the filter with a step-level expression."""
    jobs = _github_jobs(write_github_toml(tmp_path, matrix_table=WHEEL_ONLY, linux_arm=True), tmp_path)

    for job_name in ("mac", "linux", "linux-arm"):
        steps = [step for step in jobs[job_name]["steps"]
                 if _is_build_step(step)]
        assert len(steps) == 1, (job_name, steps)
        step = steps[0]
        assert '--filter=\"${BUILD_MATRIX_FILTER}\"' in str(step["run"]), job_name
        expression = step["env"]["BUILD_MATRIX_FILTER"]
        assert "startsWith(github.ref, 'refs/tags/')" in expression, job_name
        assert '"options":{{"testing":false}}' in expression, job_name
        assert BACKSLASH not in expression, (
            "the JSON is written plainly inside the expression. An env "
            "var is expanded by bash after both the YAML and the shell "
            "have finished with the line, so it needs none of the escaping "
            "the inline form carried -- that one spelled every quote it "
            "contained as a backslash pyramid to survive all three layers."
        )


def test_github_windows_leg_narrows_with_two_gated_steps(tmp_path):
    """The Windows leg gates two steps rather than selecting a variable.

    ``%VAR%`` is substituted before the argument is parsed, so a filter held in
    a variable would break its own quoting the moment it reached the command
    line. The Windows leg keeps the filter inline and picks between two steps.
    """
    jobs = _github_jobs(write_github_toml(tmp_path, matrix_table=WHEEL_ONLY, linux_arm=True), tmp_path)
    steps = [step for step in jobs["windows"]["steps"] if _is_build_step(step)]

    assert len(steps) == 2, steps
    branch, release = steps
    assert branch["if"] == "!startsWith(github.ref, 'refs/tags/')"
    assert release["if"] == "startsWith(github.ref, 'refs/tags/')"
    assert all(step["shell"] == "cmd" for step in steps)
    assert "BUILD_MATRIX_FILTER" not in str(steps), (
        "cmd would substitute it before the argument is parsed"
    )
    assert "testing" not in str(branch["run"]), (
        "a branch build takes the whole matrix for its build type"
    )
    assert "testing" in str(release["run"]), (
        "and a tag build excludes the testing configurations"
    )


# --- [matrix].wheel_only gates the concurrent build stage -------------------
#
# The per-configuration build stage, the tag narrowing and the export fan-out
# are one change, and it is wheel_only's alone. Every repository without the
# flag has to keep generating exactly the pipeline it generated before, so
# these tests assert the *old* shape is what comes out -- the gate is the
# subject, not the pipeline.


def test_gitlab_without_wheel_only_keeps_the_single_looping_build(tmp_path):
    """No flag, no fan-out: one "Conan Build" that loops the whole matrix.

    The concurrent stage was designed and measured against a wheel_only matrix,
    which has no library configurations. A repository that publishes library
    packages keeps what it had until that shape is proven too, so the flag is
    what selects between them and not, say, the presence of coverage.
    """
    parsed = _gitlab_jobs(tmp_path, coverage=True, deploy=True)

    assert "Conan Build" in parsed
    # "Coverage Build" declares `needs: []` in this shape too, and always has:
    # it is what starts the instrumented compile alongside "Conan Build"
    # rather than at its stage's turn. The per-configuration build jobs are
    # the ones that must not be here.
    assert set(_linux_build_gates(parsed)) == {"Coverage Build"}, (
        "`needs: []` is what makes the per-configuration jobs concurrent; "
        "without wheel_only there are no such jobs to make concurrent"
    )
    for name in ("Release Build", "Debug Instrumented Build", "Python Build"):
        assert name not in parsed, f"{name} belongs to the wheel_only stage"


def test_gitlab_wheel_only_is_what_turns_the_concurrent_stage_on(tmp_path):
    """The same build.toml plus the flag renders the other shape.

    Paired with the test above so the two are read together: nothing but
    [matrix].wheel_only differs between them.
    """
    parsed = _gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, coverage=True, deploy=True)

    assert "Conan Build" not in parsed
    gates = _linux_build_gates(parsed)
    assert gates, "the per-configuration jobs all declare needs: []"
    assert "Debug Instrumented Build" in gates


def test_gitlab_without_wheel_only_keeps_the_separate_coverage_build(tmp_path):
    """Coverage still compiles in a job of its own, and pages reads it there.

    Without the fan-out there are no instrumented build-stage jobs to take
    tracefiles from, so removing "Coverage Build" here would leave the report
    job merging nothing and the pipeline green with no coverage measured.
    """
    parsed = _gitlab_jobs(tmp_path, coverage=True)

    assert "Coverage Build" in parsed
    assert parsed["Coverage"]["needs"] == [{"job": "Coverage Build", "artifacts": True}]
    assert parsed["pages"]["dependencies"] == ["Coverage Build"]


def test_gitlab_without_wheel_only_split_tests_need_the_build_that_exists(tmp_path):
    """A `needs:` naming an undefined job fails the pipeline at config time.

    The wheel_only test jobs each need their own per-configuration build. Those
    jobs do not exist here, so the test jobs have to name "Conan Build" -- and
    this is the failure the gate is most likely to reintroduce, because it
    breaks the whole pipeline rather than one job.
    """
    parsed = _gitlab_jobs(tmp_path, split_tests=True, coverage=True)

    defined = set(parsed)
    for name, job in parsed.items():
        if not isinstance(job, dict):
            continue
        for need in job.get("needs", []):
            named = need["job"] if isinstance(need, dict) else need
            assert named in defined, f"{name} needs undefined job {named!r}"
    test_jobs = [job for name, job in parsed.items()
                 if isinstance(name, str) and name.startswith("Run C++ Tests")]
    assert test_jobs, "split_tests must still emit the C++ test jobs"
    for job in test_jobs:
        assert job["needs"] == [{"job": "Conan Build", "artifacts": True}]


def test_gitlab_without_wheel_only_leaves_the_windows_build_unfiltered(tmp_path):
    """The Windows tag narrowing is part of the same change, so it waits too."""
    parsed = _gitlab_jobs(tmp_path, windows=True, deploy=True)
    windows = parsed["Conan Build - Windows"]

    assert "BUILD_MATRIX_FILTER" not in windows.get("variables", {})
    assert "rules" not in windows
    build = [step for step in windows["script"] if "build.py" in step]
    assert len(build) == 1 and "--filter" not in build[0], build


def test_gitlab_without_wheel_only_deploys_from_the_one_tarball(tmp_path):
    """One build job means one export, restored and uploaded in one step."""
    parsed = _gitlab_jobs(tmp_path, deploy=True)

    assert parsed["Conan Deploy - Linux"]["dependencies"] == ["Conan Build"]
    restores = [line for line in parsed["Conan Deploy - Linux"]["script"]
                if "--restore" in line]
    assert len(restores) == 1 and restores[0].endswith("--upload"), restores


def test_github_without_wheel_only_builds_the_same_on_a_tag(tmp_path):
    """Every GitHub leg keeps its unconditional build step.

    The bash legs took the filter from a step `env:` and the cmd leg split into
    two `if:`-gated steps. Both are the tag narrowing, so both wait for the
    flag -- checked on every leg because they are four separate blocks in the
    template and gating three of them would be a silent asymmetry.
    """
    jobs = _github_jobs(write_github_toml(tmp_path, linux_arm=True), tmp_path)

    for name, job in jobs.items():
        build = [step for step in job.get("steps", [])
                 if _is_build_step(step)]
        if not build:
            continue
        assert len(build) == 1, (name, [step.get("name") for step in build])
        step = build[0]
        assert "env" not in step or "BUILD_MATRIX_FILTER" not in step["env"], name
        assert "if" not in step, name
        assert "BUILD_MATRIX_FILTER" not in step["run"], name


# --- the wheel a branch never builds ----------------------------------------
#
# The restructure made the clean pybind build `only: tags`, because a branch
# has no use for the configuration a release publishes. Repair Wheel stayed
# ungated and so kept running on branches, where the only surviving pybind job
# is the instrumented one -- which measures coverage and writes no wheel. The
# job then failed every branch pipeline on an empty wheelhouse.


def _jobs_reaching(parsed, *, tags):
    """The jobs GitLab would put in a tag pipeline, or in a branch pipeline."""
    reaching = {}
    for name, job in parsed.items():
        if not isinstance(job, dict) or "script" not in job:
            continue
        gate = _gate(job)
        if gate == "always":
            reaching[name] = job
        elif gate.startswith("only:"):
            if tags and gate == "only:tags":
                reaching[name] = job
        elif gate.startswith("except:"):
            if not (tags and gate == "except:tags"):
                reaching[name] = job
    return reaching


def _upstreams(job):
    """The jobs a job takes artifacts from, however it spells the dependency."""
    for need in job.get("needs", []):
        yield need["job"] if isinstance(need, dict) else need
    for name in job.get("dependencies", []):
        yield name


def _script(job):
    return "\n".join(job.get("script", []))


def _supplies_a_wheel(name, reaching, seen=None):
    """Whether `name` can hand a wheel downstream in this pipeline.

    Either it builds one, or it inherits one from an upstream job that is in
    this pipeline too. Following the dependency edges is the whole point: a
    wheel only reaches a job the artifacts flow to, so a producer running on
    some other platform's leg is not an answer.
    """
    seen = seen or set()
    if name in seen or name not in reaching:
        return False
    seen.add(name)
    script = _script(reaching[name])
    if "build.py" in script and "--wheel-dir" in script:
        return True
    return any(_supplies_a_wheel(up, reaching, seen)
               for up in _upstreams(reaching[name]))


@pytest.mark.parametrize("tags", [False, True], ids=["branch", "tag"])
@pytest.mark.parametrize("matrix_table", [None, WHEEL_ONLY], ids=["plain", "wheel_only"])
def test_gitlab_wheel_consumers_have_a_producer_in_their_own_pipeline(
        tmp_path, matrix_table, tags):
    """No pipeline reaches a wheel-consuming job the wheel cannot reach.

    The general form of the bug, asserted per pipeline flavor: repairing and
    deploying both read `wheelhouse`, and a gate that leaves a consumer with no
    producer upstream is what failed xmsvtk pipeline 63162. Windows keeps its
    own ungated producer, so this has to follow the dependency edges rather
    than ask whether the pipeline builds any wheel at all -- that weaker
    question passes while Linux is broken.
    """
    parsed = _gitlab_jobs(
        tmp_path, matrix_table=matrix_table, coverage=True, deploy=True)
    reaching = _jobs_reaching(parsed, tags=tags)

    for name, job in reaching.items():
        script = _script(job)
        if "xmsconan_wheel_repair" not in script and "xmsconan_wheel_deploy" not in script:
            continue
        assert _supplies_a_wheel(name, reaching), (
            f"{name!r} reads wheelhouse, but no job it depends on builds a "
            f"wheel in this pipeline; its upstreams were "
            f"{sorted(_upstreams(job))}"
        )


def test_gitlab_wheel_only_repairs_the_wheel_only_on_a_tag(tmp_path):
    """Repair Wheel is gated to match the one job that builds a wheel."""
    parsed = _gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, coverage=True, deploy=True)

    assert _gate(parsed["Repair Wheel"]) == "only:tags"
    assert _gate(parsed["Python Build"]) == "only:tags", (
        "the gate is only correct while the clean pybind build is the producer"
    )


def test_gitlab_without_wheel_only_repairs_the_wheel_on_every_pipeline(tmp_path):
    """No flag, no gate: one ungated build still makes a wheel on a branch.

    Paired with the test above. The gate belongs to the restructure, so a
    repository without the flag has to keep repairing on branches the way it
    always did -- adding `only: tags` there would silently drop a check.
    """
    parsed = _gitlab_jobs(tmp_path, coverage=True, deploy=True)

    assert _gate(parsed["Repair Wheel"]) == "always"
    assert _gate(parsed["Conan Build"]) == "always"


# --- wheel_only: filter warnings and the keys a narrowed matrix can empty ---


def _wheel_only_with_filter(tmp_path, filter_table, **ci_flags):
    """Render a wheel_only GitLab pipeline whose build.toml carries a [filter].

    ``write_gitlab_toml`` has no [filter] parameter -- the table is appended
    here rather than added to the shared helper, because these are the only
    tests that need one and the helper's callers all read better without it.
    """
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, **ci_flags)
    toml_file.write_text(toml_file.read_text(encoding="utf-8") + filter_table,
                         encoding="utf-8")
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    return yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))


def test_wheel_only_empty_job_warning_does_not_name_a_job_it_never_emits(
    tmp_path, caplog,
):
    """The warning has to name the fan-out, not the job wheel_only replaced.

    An os pin excludes every Linux configuration, so the concurrent build stage
    renders empty. Naming "Conan Build" there would point the reader at a block
    this pipeline does not contain -- and looking the name up in the emitted
    jobs, finding none, would drop the warning entirely in exactly the case it
    exists for.
    """
    with caplog.at_level(logging.WARNING):
        _wheel_only_with_filter(tmp_path, '\n[filter]\nos = "Windows"\n',
                                coverage=True, deploy=True)

    warned = [r.getMessage() for r in caplog.records
              if "empty matrix" in r.getMessage()]
    assert warned, "an os pin empties the whole Linux fan-out and must warn"
    assert any("Linux build jobs" in message for message in warned), warned
    assert not any("Conan Build'" in message for message in warned), warned


def test_deploy_omits_dependencies_when_the_filter_leaves_nothing_exporting(
    tmp_path,
):
    """An empty `dependencies:` is a null key, and GitLab rejects the file.

    Only jobs that run on a tag export a cache tarball, and a testing-only
    filter makes every Linux job branch-only -- while the deploy job stays
    gated on [ci].deploy alone. The key has to be dropped rather than emitted
    empty; without `dependencies:` GitLab falls back to taking every earlier
    stage's artifacts, which is what this block narrows rather than depends on.
    """
    parsed = _wheel_only_with_filter(
        tmp_path, "\n[filter.options]\ntesting = true\n", deploy=True,
    )

    deploy = parsed["Conan Deploy - Linux"]
    assert "dependencies" not in deploy, (
        f"dependencies: rendered with nothing under it -> "
        f"{deploy.get('dependencies')!r}"
    )


def test_coverage_omits_needs_when_the_filter_instruments_nothing(tmp_path):
    """Same null-key guard on the Coverage job's `needs:`.

    A Release pin cancels the Debug C++ leg and `pybind = false` the Python
    one, which generation warns about and then renders anyway. The warning must
    not be followed by a pipeline GitLab refuses to parse, or it reads as a
    template bug instead of the filter problem it is.
    """
    parsed = _wheel_only_with_filter(
        tmp_path,
        '\n[filter]\nbuild_type = "Release"\n\n[filter.options]\npybind = false\n',
        coverage=True,
    )

    coverage = parsed["Coverage"]
    assert "needs" not in coverage, (
        f"needs: rendered with nothing under it -> {coverage.get('needs')!r}"
    )
