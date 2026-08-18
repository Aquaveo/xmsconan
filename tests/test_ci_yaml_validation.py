"""Validate that generated CI files are syntactically valid YAML.

These tests render every combination of CI options (windows, deploy,
coverage, xvfb) for both GitHub and GitLab templates, then parse the
output with PyYAML.  This catches template bugs that produce broken
YAML without needing a real CI runner.
"""
import itertools

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML required for CI validation tests")

from xmsconan.generator_tools.ci_file_generator import generate_ci  # noqa: E402
from .utils import GITLAB_NON_JOB_KEYS  # noqa: E402


# All boolean CI options and their possible values.
CI_OPTIONS = {
    "linux": [False, True],
    "windows": [False, True],
    "deploy": [False, True],
    "coverage": [False, True],
    "xvfb": [False, True],
    "linux_arm": [False, True],
}

# Every combination of the four boolean flags.
_OPTION_COMBOS = [
    dict(zip(CI_OPTIONS.keys(), combo))
    for combo in itertools.product(*CI_OPTIONS.values())
]


def _is_generatable_for_gitlab(options):
    """Whether generate_ci accepts this combination for a GitLab project.

    Two combinations are rejected by design (ci_file_generator): no platform at
    all, and coverage without the gcc job that instruments it. They are excluded
    here rather than expected to generate -- the dedicated rejection tests in
    test_ci_file_generator cover the raising itself.
    """
    if not options.get("linux", True) and not options.get("windows", True):
        return False
    if not options.get("linux", True) and options.get("coverage", False):
        return False
    return True


#: GitLab-valid subset of :data:`_OPTION_COMBOS`.
_GITLAB_COMBOS = [combo for combo in _OPTION_COMBOS if _is_generatable_for_gitlab(combo)]


def _combo_id(combo):
    """Readable test ID like 'win-deploy-cov' or 'minimal'."""
    parts = [k[:3] for k, v in combo.items() if v]
    return "-".join(parts) or "minimal"


def _write_toml(tmp_path, ci_type, options):
    """Write a build.toml with the given ci_type and [ci] options."""
    toml_file = tmp_path / "build.toml"
    lines = [
        'library_name = "xmscore"',
        'description = "Core library"',
        f'ci_type = "{ci_type}"',
        'python_namespaced_dir = "core"',
        "",
        "[ci]",
    ]
    for key, value in options.items():
        lines.append(f"{key} = {'true' if value else 'false'}")
    toml_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml_file


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", _OPTION_COMBOS, ids=_combo_id)
def test_github_ci_produces_valid_yaml(options, tmp_path):
    """Generated GitHub CI is parseable YAML for every option combo."""
    toml_file = _write_toml(tmp_path, "github", options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    assert ci_file.exists(), f"CI file not generated for {options}"

    content = ci_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    # Basic structure checks
    assert isinstance(parsed, dict), "Top-level YAML must be a mapping"
    assert "name" in parsed, "Missing 'name' key"
    assert "jobs" in parsed, "Missing 'jobs' key"
    assert isinstance(parsed["jobs"], dict), "'jobs' must be a mapping"
    assert len(parsed["jobs"]) > 0, "Must have at least one job"


@pytest.mark.parametrize("options", _OPTION_COMBOS, ids=_combo_id)
def test_github_ci_job_steps_are_lists(options, tmp_path):
    """Every job's 'steps' field is a list (not accidentally a string)."""
    toml_file = _write_toml(tmp_path, "github", options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    for job_name, job in parsed["jobs"].items():
        if "steps" in job:
            assert isinstance(job["steps"], list), (
                f"Job '{job_name}' steps must be a list"
            )
            for i, step in enumerate(job["steps"]):
                assert isinstance(step, dict), (
                    f"Job '{job_name}' step {i} must be a mapping"
                )


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", _GITLAB_COMBOS, ids=_combo_id)
def test_gitlab_ci_produces_valid_yaml(options, tmp_path):
    """Generated GitLab CI is parseable YAML for every option combo."""
    toml_file = _write_toml(tmp_path, "gitlab", options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    assert ci_file.exists(), f"CI file not generated for {options}"

    content = ci_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    assert isinstance(parsed, dict), "Top-level YAML must be a mapping"
    assert "stages" in parsed, "Missing 'stages' key"
    assert isinstance(parsed["stages"], list), "'stages' must be a list"


@pytest.mark.parametrize("options", _GITLAB_COMBOS, ids=_combo_id)
def test_gitlab_ci_jobs_have_script(options, tmp_path):
    """Every GitLab job has a 'script' list (not a bare string)."""
    toml_file = _write_toml(tmp_path, "gitlab", options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    # Keys that aren't jobs
    non_job_keys = GITLAB_NON_JOB_KEYS
    for key, value in parsed.items():
        if key in non_job_keys:
            continue
        if not isinstance(value, dict):
            continue
        # It's a job — it must have a script
        assert "script" in value, f"Job '{key}' missing 'script'"
        assert isinstance(value["script"], list), (
            f"Job '{key}' script must be a list"
        )


@pytest.mark.parametrize("options", _GITLAB_COMBOS, ids=_combo_id)
def test_gitlab_ci_stages_match_jobs(options, tmp_path):
    """Every job's stage is listed in the top-level stages list."""
    toml_file = _write_toml(tmp_path, "gitlab", options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    stages = set(parsed["stages"])
    non_job_keys = GITLAB_NON_JOB_KEYS
    for key, value in parsed.items():
        if key in non_job_keys or not isinstance(value, dict):
            continue
        if "stage" in value:
            assert value["stage"] in stages, (
                f"Job '{key}' uses stage '{value['stage']}' "
                f"not in {stages}"
            )


@pytest.mark.parametrize("options", _GITLAB_COMBOS, ids=_combo_id)
def test_gitlab_ci_needs_reference_defined_jobs(options, tmp_path):
    """Every 'needs' entry names a defined job and uses no unexpanded variables.

    GitLab does not expand variables inside needs:parallel:matrix.  A selector
    such as "PYTHON_TARGET_VERSION: $PYTHON_TARGET_VERSION" is compared as a
    literal, matches no job, and fails the entire pipeline at config time with
    "undefined need" — producing zero jobs rather than a failing job.
    """
    toml_file = _write_toml(tmp_path, "gitlab", options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    non_job_keys = GITLAB_NON_JOB_KEYS
    job_names = {
        key for key, value in parsed.items()
        if key not in non_job_keys and isinstance(value, dict)
    }

    for key, value in parsed.items():
        if key in non_job_keys or not isinstance(value, dict):
            continue
        for entry in value.get("needs", []):
            needed = entry["job"] if isinstance(entry, dict) else entry
            assert needed in job_names, (
                f"Job '{key}' needs '{needed}', which is not a defined job"
            )
            if not isinstance(entry, dict):
                continue
            for selector in entry.get("parallel", {}).get("matrix", []):
                for var, selected in selector.items():
                    assert "$" not in str(selected), (
                        f"Job '{key}' needs '{needed}' with an unexpanded "
                        f"variable in its matrix selector: {var}={selected}. "
                        f"GitLab compares these literally."
                    )


# ---------------------------------------------------------------------------
# Cross-template consistency
# ---------------------------------------------------------------------------


def test_both_templates_reference_same_xmsconan_version(tmp_path):
    """Verify GitHub and GitLab templates reference the same xmsconan version."""
    (tmp_path / "gh").mkdir()
    (tmp_path / "gl").mkdir()
    github_toml = _write_toml(
        tmp_path / "gh", "github", {"deploy": True},
    )
    gitlab_toml = _write_toml(
        tmp_path / "gl", "gitlab", {"deploy": True},
    )
    gh_out = tmp_path / "gh_out"
    gl_out = tmp_path / "gl_out"
    generate_ci(str(github_toml), "1.0.0", str(gh_out))
    generate_ci(str(gitlab_toml), "1.0.0", str(gl_out))

    gh_content = (
        gh_out / ".github" / "workflows" / "XmsCore-CI.yaml"
    ).read_text(encoding="utf-8")
    gl_content = (gl_out / ".gitlab-ci.yml").read_text(encoding="utf-8")

    # Extract all xmsconan>=X.Y.Z references
    import re
    gh_versions = set(re.findall(r"xmsconan>=([\d.]+)", gh_content))
    gl_versions = set(re.findall(r"xmsconan>=([\d.]+)", gl_content))

    assert len(gh_versions) == 1, f"GitHub has multiple versions: {gh_versions}"
    assert len(gl_versions) == 1, f"GitLab has multiple versions: {gl_versions}"
    assert gh_versions == gl_versions, (
        f"Version mismatch: GitHub={gh_versions}, GitLab={gl_versions}"
    )


@pytest.mark.parametrize("ci_type", ["github", "gitlab"])
@pytest.mark.parametrize("options", _OPTION_COMBOS, ids=_combo_id)
def test_xmsconan_installs_float_and_upgrade(ci_type, options, tmp_path):
    """Every generated xmsconan install floors with >= and passes --upgrade.

    The two halves are one contract. ``>=`` is what lets a repo pick up an
    xmsconan release without regenerating its CI, but pip treats an
    already-satisfied constraint as a no-op, so on a runner image with
    xmsconan baked in the floor alone installs nothing. That is how the
    2.15.1 fix never reached xmscore CI (see ad248ed): the image carried a
    version that already satisfied the floor, so pip skipped the install on
    every run. ``--upgrade`` is what makes the floor actually resolve to the
    newest release on devpi.
    """
    if ci_type == "gitlab" and not _is_generatable_for_gitlab(options):
        pytest.skip("combination is rejected for GitLab by design")

    toml_file = _write_toml(tmp_path, ci_type, options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    install_lines = [
        stripped
        for path in sorted(output_dir.rglob("*")) if path.is_file()
        for line in path.read_text(encoding="utf-8").splitlines()
        # Comments describing the pin are not commands; only executed
        # install steps carry the contract.
        if (stripped := line.strip()) and not stripped.startswith("#")
        if "pip install" in stripped and "xmsconan" in stripped
    ]
    assert install_lines, f"no xmsconan install rendered for {ci_type} {options}"

    for line in install_lines:
        assert "xmsconan==" not in line, f"pinned rather than floored: {line}"
        assert "xmsconan>=" in line, f"no version floor: {line}"
        assert "--upgrade" in line, (
            f"floor without --upgrade is a no-op when the runner image "
            f"already carries a satisfying xmsconan: {line}"
        )
