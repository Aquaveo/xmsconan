"""Validate that generated CI files are syntactically valid YAML.

These tests render every combination of CI options for both GitHub and
GitLab templates, then parse the
output with PyYAML.  This catches template bugs that produce broken
YAML without needing a real CI runner.
"""
import itertools

import pytest
import yaml

from xmsconan.generator_tools.ci_file_generator import generate_ci
from .ci_helpers import NON_JOB_SHAPE_KEYS, write_build_toml


# All boolean CI options and their possible values.
CI_OPTIONS = {
    "linux": [False, True],
    "windows": [False, True],
    "split_tests": [False, True],
    "deploy": [False, True],
    "coverage": [False, True],
    "xvfb": [False, True],
    "linux_arm": [False, True],
    "windows_wheel_repair": [False, True],
}

# Every combination of the boolean CI flags.
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

#: GitHub honours neither [ci].linux nor [ci].split_tests, so varying them here
#: would double the parametrization without changing a single generated file.
_GITHUB_KEYS = [key for key in CI_OPTIONS if key not in ("linux", "split_tests")]
_GITHUB_COMBOS = [
    dict(zip(_GITHUB_KEYS, combo))
    for combo in itertools.product(*(CI_OPTIONS[key] for key in _GITHUB_KEYS))
]


def _combo_id(combo):
    """Readable test ID like 'win-deploy-cov' or 'minimal'."""
    parts = [k[:3] for k, v in combo.items() if v]
    return "-".join(parts) or "minimal"


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", _GITHUB_COMBOS, ids=_combo_id)
def test_github_ci_produces_valid_yaml(options, tmp_path):
    """Generated GitHub CI is parseable YAML for every option combo."""
    toml_file = write_build_toml(tmp_path, "github", library_name='xmscore', description='Core library', **options)
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


@pytest.mark.parametrize("options", _GITHUB_COMBOS, ids=_combo_id)
def test_github_ci_job_steps_are_lists(options, tmp_path):
    """Every job's 'steps' field is a list (not accidentally a string)."""
    toml_file = write_build_toml(tmp_path, "github", library_name='xmscore', description='Core library', **options)
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
    toml_file = write_build_toml(tmp_path, "gitlab", library_name='xmscore', description='Core library', **options)
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
    toml_file = write_build_toml(tmp_path, "gitlab", library_name='xmscore', description='Core library', **options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    # Keys that aren't jobs
    non_job_keys = NON_JOB_SHAPE_KEYS
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
    toml_file = write_build_toml(tmp_path, "gitlab", library_name='xmscore', description='Core library', **options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    stages = set(parsed["stages"])
    # Deliberately narrower than GITLAB_NON_JOB_KEYS: `pages` IS a job (it
    # declares stage: Pages) and this is the only test asserting its stage is
    # declared. Excluding it here would drop that guard silently.
    non_job_keys = {"stages", "variables"}
    for key, value in parsed.items():
        if key in non_job_keys or not isinstance(value, dict):
            continue
        if "stage" in value:
            assert value["stage"] in stages, (
                f"Job '{key}' uses stage '{value['stage']}' "
                f"not in {stages}"
            )


@pytest.mark.parametrize("options", _GITLAB_COMBOS, ids=_combo_id)
def test_gitlab_every_declared_stage_is_entered(options, tmp_path):
    """No stage is declared that no job ever enters.

    The inverse of test_gitlab_ci_stages_match_jobs, which only checks that
    every stage a job names is declared. An orphan stage is not a hard GitLab
    error, but it is always a template bug -- and it is exactly what the
    Build-stage gate in this PR was meant to prevent.
    """
    toml_file = write_build_toml(tmp_path, "gitlab", library_name="xmscore",
                                 description="Core library", **options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    declared = set(parsed.get("stages", []))
    entered = {
        value["stage"] for value in parsed.values()
        if isinstance(value, dict) and "stage" in value
    }
    assert declared - entered == set(), f"stages declared but never entered: {sorted(declared - entered)}"


@pytest.mark.parametrize("options", _GITLAB_COMBOS, ids=_combo_id)
def test_gitlab_ci_needs_reference_defined_jobs(options, tmp_path):
    """Every 'needs' entry names a defined job and uses no unexpanded variables.

    GitLab does not expand variables inside needs:parallel:matrix.  A selector
    such as "PYTHON_TARGET_VERSION: $PYTHON_TARGET_VERSION" is compared as a
    literal, matches no job, and fails the entire pipeline at config time with
    "undefined need" — producing zero jobs rather than a failing job.
    """
    toml_file = write_build_toml(tmp_path, "gitlab", library_name='xmscore', description='Core library', **options)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".gitlab-ci.yml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    non_job_keys = NON_JOB_SHAPE_KEYS
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
    github_toml = write_build_toml(
        tmp_path / "gh", "github", library_name="xmscore", description="Core library", deploy=True,
    )
    gitlab_toml = write_build_toml(
        tmp_path / "gl", "gitlab", library_name="xmscore", description="Core library", deploy=True,
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


_INSTALL_CASES = [("github", combo) for combo in _GITHUB_COMBOS]
_INSTALL_CASES += [("gitlab", combo) for combo in _GITLAB_COMBOS]


@pytest.mark.parametrize("ci_type,options", _INSTALL_CASES,
                         ids=lambda value: value if isinstance(value, str) else _combo_id(value))
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
    toml_file = write_build_toml(tmp_path, ci_type, library_name='xmscore', description='Core library', **options)
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


#: Python fan-out shapes to YAML-validate. The boolean sweep above cannot reach
#: these -- it only varies flags -- so a template bug that only appears once a
#: platform has more than one ABI leg would otherwise go unparsed. The xvfb pair
#: is included because it is the only route to the xvfb ``Repair Wheel`` image,
#: which resolves a single version even while the build fans out.
_FANOUT_CASES = [
    pytest.param({"python_versions": ["3.10", "3.13", "3.14"]}, id="windows-only"),
    pytest.param({"mac_python_versions": ["3.13", "3.14"]}, id="mac"),
    pytest.param({"linux_python_versions": ["3.13", "3.14"]}, id="linux"),
    pytest.param({"linux_arm": True, "linux_python_versions": ["3.13", "3.14"]}, id="linux-arm"),
    pytest.param({"xvfb": True, "linux_python_versions": ["3.13", "3.14"]}, id="xvfb-linux"),
    pytest.param(
        {
            "python_versions": ["3.10", "3.13", "3.14"],
            "mac_python_versions": ["3.13", "3.14"],
            "linux_python_versions": ["3.13", "3.14"],
            "linux_arm": True,
            "coverage": True,
            "deploy": True,
        },
        id="everything",
    ),
]


@pytest.mark.parametrize("ci_type", ["github", "gitlab"])
@pytest.mark.parametrize("ci_flags", _FANOUT_CASES)
def test_python_fanout_renders_valid_yaml(tmp_path, ci_type, ci_flags):
    """Every fan-out shape must parse as YAML on both templates."""
    toml_file = write_build_toml(tmp_path, ci_type, **ci_flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    rendered = list((output_dir / ".github" / "workflows").glob("*.yaml"))
    rendered += [path for path in [output_dir / ".gitlab-ci.yml"] if path.exists()]
    assert rendered, "generate_ci produced no CI file"
    for path in rendered:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict) and parsed


# ---------------------------------------------------------------------------
# [filter] table
# ---------------------------------------------------------------------------


def test_github_ci_with_pinned_build_type_produces_valid_yaml(tmp_path):
    """A [filter]-narrowed build_type matrix is still a valid YAML sequence."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'ci_type = "github"\n'
        'python_namespaced_dir = "core"\n'
        "\n[ci]\nlinux_arm = true\n"
        "\n[filter]\nbuild_type = \"Release\"\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))

    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    parsed = yaml.safe_load(ci_file.read_text(encoding="utf-8"))

    build_jobs = [
        job for job in parsed["jobs"].values()
        if "build_type" in job.get("strategy", {}).get("matrix", {})
    ]
    assert build_jobs, "No job carries a build_type matrix"
    for job in build_jobs:
        assert job["strategy"]["matrix"]["build_type"] == ["Release"]
