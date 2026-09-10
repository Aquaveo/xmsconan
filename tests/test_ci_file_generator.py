"""Tests for generator_tools.ci_file_generator."""
import logging
import re

import pytest
import yaml

from xmsconan.build_toml import read_build_toml
from xmsconan.constants import version_sort_key
from xmsconan.coverage_tools.coverage_generator import EXIT_GATE_FAILED
from xmsconan.generator_tools.ci_file_generator import (
    _display_name,
    generate_ci,
    xmsconan_requirement,
)
from xmsconan.job_tools import build as job_build, common as job_common
from .ci_helpers import (
    NON_JOB_SHAPE_KEYS,
    requirement_names,
    steps_running,
    WHEEL_ONLY,
    workflow_document,
    write_github_toml,
    write_gitlab_toml,
)
from .doc_helpers import slice_between, usage_text
from .utils import patch_env


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
    """display_name reaches the output; library_name no longer needs to.

    The workflow named the library three times -- a LIBRARY_NAME nothing
    read, and the two `xmsconan_conan_deploy <library>` lines. Every command
    left reads it from build.toml, so the one name here is the display name
    in the workflow's own title, and a library rename no longer has to
    reach the generated file to be correct.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "2.3.5", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "XmsCore" in content
    assert "xmscore" not in content


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
    return workflow_document(workflow)["jobs"]


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
    # Only the Windows *build* fans out. Read that off its parallel matrix
    # rather than off a substring of the file: every Linux job also declares a
    # PYTHON_TARGET_VERSION, under `variables`, so a bare `"3.10" in content`
    # would keep passing on a Windows job that had stopped fanning out at all.
    parsed = yaml.safe_load(content)
    matrix = parsed["Conan Build - Windows"]["parallel"]["matrix"]
    assert [entry["PYTHON_TARGET_VERSION"] for entry in matrix] == ["3.10", "3.13"]
    # Its deploy does not: restoring and uploading is ABI-independent, and one
    # instance per ABI would each restore the whole artifact set and race to
    # upload the same Conan reference. It still pins a version, for the venv.
    deploy = parsed["Conan Deploy - Windows"]
    assert "parallel" not in deploy
    assert deploy["variables"]["PYTHON_TARGET_VERSION"] == "3.13"
    # Linux jobs are single-version and use a static image.
    assert "conan-gcc13-py3.13" in content
    assert "cp313-cp313" in content
    # Linux jobs are NOT fanned out — so CP_TAG (only the wheel-repair var) is gone.
    assert "CP_TAG" not in content


def test_gitlab_windows_jobs_run_on_the_uv_runner(tmp_path):
    """Both Windows jobs select GLR-UV and still route to the WinVM fleet.

    ``image:`` picks the VM template and ``tags:`` picks the fleet -- the two
    are not interchangeable, and collapsing three per-ABI images into one could
    plausibly have dropped either.
    """
    toml_file = write_gitlab_toml(tmp_path)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    for name in ("Conan Build - Windows", "Conan Deploy - Windows"):
        assert parsed[name]["image"] == "GLR-UV", name
        assert parsed[name]["tags"] == ["WinVM"], name
    # The retired per-ABI images, and the matrix variable whose only job was to
    # spell one, must not survive anywhere in the file.
    assert "GLR-py" not in content
    assert "PY_TAG" not in content


def test_gitlab_windows_jobs_build_a_uv_venv_of_the_matrix_abi(tmp_path):
    """Each Windows job creates and activates a venv on its own PYTHON_TARGET_VERSION.

    This is what let three per-ABI runner images collapse into one. The recipe
    hands CMake ``Python3_EXECUTABLE = sys.executable`` and the generated
    CMakeLists.txt does ``find_package(Python3 ... EXACT REQUIRED)``, so the
    interpreter running conan *is* the ABI being built. GLR-py310 / GLR-py313
    supplied that as the machine's own ``python``; on GLR-UV the job has to
    establish it, and before anything else reaches for an interpreter.
    """
    toml_file = write_gitlab_toml(tmp_path, python_versions=["3.10", "3.13"])
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    for name in ("Conan Build - Windows", "Conan Deploy - Windows"):
        script = parsed[name]["script"]
        venv = script.index("uv venv --python ${PYTHON_TARGET_VERSION} .venv")
        activate = script.index("source .venv/Scripts/activate")
        assert venv < activate, name
        # Nothing may reach for an interpreter, or for a console script
        # installed beside one, before the venv is on PATH.
        for step in script[:activate]:
            assert not step.startswith(("python ", "python-m", "pip ", "xmsconan_")), (
                f"{name}: {step!r} runs before the venv is activated"
            )
        # uv is proven present before a package install depends on it, so a
        # runner without uv fails on a line that says so.
        assert script.index("uv --version") < venv, name


def test_gitlab_windows_jobs_install_with_uv_not_pip(tmp_path):
    """The Windows jobs install through ``uv pip``; bare ``pip`` is gone from them.

    Asserted per job rather than over the whole file: the Linux jobs keep plain
    pip, because they run in containers that ship one and have no uv.
    """
    toml_file = write_gitlab_toml(tmp_path)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    for name in ("Conan Build - Windows", "Conan Deploy - Windows"):
        # "pip install", not "install": a future `conan install` step in these
        # jobs would otherwise be swept in and asserted to start with "uv pip".
        installs = [step for step in parsed[name]["script"] if "pip install" in step]
        assert installs, name
        for step in installs:
            assert step.startswith("uv pip install "), f"{name}: {step!r}"
            # `-i` is pip's spelling for the index; uv takes the long option.
            assert " -i http" not in step, f"{name}: {step!r}"
        assert any("xmsconan[ci]>=" in step for step in installs), name


def test_gitlab_build_jobs_install_the_extra_before_the_job_command(tmp_path):
    """Each ``Conan Build`` job installs the ``[ci]`` extra, then runs a command from it.

    GLR-py310 / GLR-py313 carried cmake on PATH and GLR-UV does not, so the
    Windows jobs have always had to supply it -- conan shells out to ``cmake``
    to configure every configuration, including the dependencies it builds
    from source. The ``[ci]`` extra carries it, and ``xmsconan job build`` is
    itself a console script from that same extra, so the install has to come
    first and has to be the only one: a second install line is a second
    opinion about which version of the tool the job is running.

    What the job command does *once it starts* -- print the tool versions
    before anything builds, so a cmake that fails to resolve reads as one line
    rather than resurfacing as a per-configuration build error much later --
    moved into :mod:`xmsconan.job_tools.build` with the rest of the script,
    and is asserted against the tool in ``tests/test_job_build.py``.

    The instrumented jobs and ``Coverage Build`` are outside this on purpose:
    they install the same extra, but hand the build to ``xmsconan coverage``.
    """
    toml_file = write_gitlab_toml(tmp_path, windows_vs2019=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    for name in ("Conan Build", "Conan Build - Windows", "Conan Build - Windows VS2019"):
        script = parsed[name]["script"]
        installs = [step for step in script if "pip install" in step]
        assert len(installs) == 1 and "xmsconan[ci]" in installs[0], f"{name}: {installs!r}"
        # Asserted before it is indexed: a job that stopped invoking the
        # command would otherwise raise ValueError from min() and name
        # neither the job nor the missing step.
        builds = [index for index, step in enumerate(script)
                  if "xmsconan job build" in step]
        assert builds, f"{name}: {script!r}"
        assert script.index(installs[0]) < min(builds), name


def test_gitlab_windows_deploy_snapshots_no_conan_cache(tmp_path):
    """No `cp -r ~/.conan2/p/*`, and no conan_packages/ artifact holding it.

    It was an unqueried copy of a *shared* runner's Conan cache, so on a
    machine also running the msvc 192 matrix it snapshotted the other
    toolchain's binaries beside this job's -- unusable for the one question it
    existed to answer. Nothing consumed it. ``job deploy --cache-archive``
    writes the queried equivalent for a caller that wants one.
    """
    toml_file = write_gitlab_toml(tmp_path)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    job = parsed["Conan Deploy - Windows"]
    assert not [step for step in job["script"] if step.startswith("cp -r")], job["script"]
    assert "conan_packages" not in content


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


def test_github_ci_bakes_no_version_into_the_workflow(ci_toml, tmp_path):
    """Nothing in the rendered workflow names a version, or rewrites one.

    The version handed to generate_ci is the generating xmsconan's argument,
    not the library's; it used to leak into the file as an ``XMS_VERSION``
    default of 0.0.0 that two third-party actions then overwrote in
    GITHUB_ENV on a tag, next to a Conan reference and channel nothing read.
    The tools resolve GITHUB_REF_NAME themselves now, when GITHUB_REF_TYPE
    says it is a tag, so the workflow has no version to carry and no step
    that rewrites the environment for the next one.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "7.0.1", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "XmsCore-CI.yaml").read_text(encoding="utf-8")

    assert "7.0.1" not in content
    for gone in ("XMS_VERSION", "CONAN_REFERENCE", "CONAN_CHANNEL", "RELEASE_PYTHON",
                 "get-git-tag", "set-env", "branch-name", "xmsconan_gen"):
        assert gone not in content, gone
    # The regeneration did not go away, it moved: `job build` and `job lint`
    # each run it in-process, at the version they resolved themselves.
    assert "run: xmsconan job build" in content


def test_github_ci_uses_cli_commands(ci_toml, tmp_path):
    """Rendered GitHub CI runs the job commands, as GitLab's does.

    The entry points named as absences are the ones the job commands
    replaced. Either reappearing means a step went back to spelling out a
    wheel directory, a remote URL or a matrix filter that the tool reads
    from build.toml and this job's environment.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "xmsconan job build" in content
    assert "xmsconan job lint" in content
    assert "xmsconan job package" in content
    assert "xmsconan job deploy" in content
    for gone in ("xmsconan_conan_setup", "xmsconan_conan_deploy", "xmsconan_wheel_repair",
                 "xmsconan_wheel_deploy", "build.py"):
        assert gone not in content, gone
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
    assert "xmsconan job build" in content
    assert "xmsconan job lint" in content
    assert "xmsconan job package" in content
    assert "xmsconan job deploy" in content
    # The publish steps the deploy job replaced. Named as absences because
    # either one reappearing means a job went back to spelling out a restore
    # path or a wheel directory the tool already knows.
    assert "xmsconan_wheel_deploy" not in content
    assert "xmsconan_conan_deploy" not in content
    assert "xmsconan_conan_setup" not in content
    # Inline conan profile detect should NOT appear
    assert "conan profile detect" not in content


def test_gitlab_ci_deploy_jobs_leave_the_version_to_the_tool(tmp_path):
    """No job exports a version; ``xmsconan_conan_deploy`` resolves the tag itself.

    Every deploy block used to open with ``export PACKAGE_VERSION=${CI_COMMIT_TAG:-0.0.0}``
    and spend it twice, as the positional version and inside the tarball
    name, so a save in one job and a restore in another agreed only while
    two lines in each block stayed in step. There is one source now: the
    tool reads CI_COMMIT_TAG, and the ``{version}`` in the tarball path is
    filled from the same resolution.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'ci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert "PACKAGE_VERSION" not in content
    assert "DEFAULT_VERSION" not in content
    parsed = yaml.safe_load(content)
    deploys = [step for name, job in parsed.items() if name not in NON_JOB_SHAPE_KEYS
               for step in job.get("script", []) if step.startswith("xmsconan job deploy")]
    assert deploys
    for step in deploys:
        # No version, and no longer a tarball path either: `job deploy` globs
        # `.export/` rather than reassembling the name the build composed, so
        # the two ends cannot spell the same file differently.
        assert "{version}" not in step, step
        assert ".tar.gz" not in step, step


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
    """Rendered GitHub CI floors xmsconan at the generating version and caps it at the next major.

    Every job installs ``xmsconan[ci]>=<generating version>,<<next major>``
    rather than pinning with ``==``, so a repo picks up an xmsconan release
    without regenerating and committing its CI. The floor rules out resolving
    a version older than the templates were written against; the cap is where
    the job contract is allowed to change, so a workflow nobody regenerated
    keeps installing the xmsconan its commands were written for.
    """
    from xmsconan import __version__
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert f'"xmsconan[ci]{xmsconan_requirement(__version__)}"' in content
    assert "xmsconan==" not in content


@pytest.mark.parametrize("version, expected", [
    ("2.18.0", ">=2.18.0,<3"),
    # A local label is legal only with == and !=, so it comes off the floor.
    ("2.19.1.dev3+gabcdef0", ">=2.19.1.dev3,<3"),
    ("10.0.0", ">=10.0.0,<11"),
    # An uninstalled checkout: satisfied by nothing on the index, and loud about it at pip install.
    ("0.0.0", ">=0.0.0,<1"),
])
def test_xmsconan_requirement_caps_at_the_next_major(version, expected):
    """The specifier the jobs install with: the generating version as floor, the next major as cap."""
    assert xmsconan_requirement(version) == expected


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

    # Both halves are `job lint`: it generates the build files and then runs
    # flake8 over the generated package, in that order, in one process.
    assert "run: xmsconan job lint" in content
    # --isolated makes flake8 ignore .flake8 entirely, which is what allowed
    # the two configs to diverge unnoticed.
    assert "--isolated" not in content
    assert "--max-line-length" not in content


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


def test_github_ci_uploads_the_directory_the_build_writes(ci_toml, tmp_path):
    """No step names an artifacts directory; the upload still has to find it.

    `job build` stages into common.ARTIFACTS_DIR, so the flag the build line
    used to carry is gone -- but the upload step is a path in YAML that
    nothing checks, and it is the one place the template still has to agree
    with the tool about where the runner logs land.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    ci_file = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = ci_file.read_text(encoding="utf-8")
    assert "--artifacts-dir" not in content
    assert f"path: {job_common.ARTIFACTS_DIR}/" in content


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


def test_gitlab_ci_collects_the_directories_the_job_command_writes(tmp_path):
    """The rendered ``artifacts:`` paths are the layout the tool writes into.

    These were a flag pair -- ``--artifacts-dir test_artifacts --wheel-dir
    wheelhouse`` on the build line, spelled again in ``artifacts: paths:``
    just below it. The flags are gone: ``xmsconan job build`` owns the layout,
    so the template's only remaining say in it is which directories it
    collects, and a rename on either side now silently uploads nothing.

    Asserted through the constants rather than against the literals, so the
    two ends cannot be renamed apart -- which is the whole failure this
    replaces the flags with.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    paths = parsed["Conan Build"]["artifacts"]["paths"]
    assert f"{job_common.ARTIFACTS_DIR}/" in paths, paths
    assert f"{job_common.WHEEL_DIR}/" in paths, paths


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


def test_gitlab_ci_leaves_the_ctest_level_to_the_job_command(tmp_path):
    """No job exports CTEST_PARALLEL_LEVEL; ``set_job_environment`` sets it.

    The export took the runner's value when it had one (``${VAR:-8}``) and 8
    otherwise, and the tool reproduces exactly that. But it reproduces it by
    *not overriding what is already set* -- so a template export left in place
    would not merely be redundant, it would win, and the tool's default would
    become unreachable from the one place a bigger runner can raise it.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\nci_type = "gitlab"\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    # Anchored on the command that now owns the variable: an absence assertion
    # alone would pass against an empty pipeline, or one whose test job stopped
    # rendering -- the two outcomes this is least able to notice.
    assert "xmsconan job build" in content
    assert job_common.CTEST_PARALLEL_VARIABLE not in content


def test_github_ci_leaves_ctest_parallelism_to_the_tool(ci_toml, tmp_path):
    """The job env set it on every leg; `set_job_environment` defaults it.

    Only when the environment has not already chosen a value, which is what
    the job-level `CTEST_PARALLEL_LEVEL: '8'` meant and what a runner that
    wants a different number still gets. The GitLab template dropped its own
    export for the same reason; this is the last copy.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    document = workflow_document(output_dir / ".github" / "workflows" / "XmsCore-CI.yaml")

    settings = [
        (name, mapping)
        for name, job in document["jobs"].items()
        for mapping in [job.get("env") or {}] + [step.get("env") or {} for step in job["steps"]]
        if job_common.CTEST_PARALLEL_VARIABLE in mapping
    ]
    assert settings == []
    # Anchored on the command that owns the variable, as the GitLab sibling
    # is: the absence assertion alone also passes a workflow whose build
    # steps stopped rendering.
    assert [name for name, job in document["jobs"].items()
            if steps_running(job, "xmsconan job build")]


def test_gitlab_ci_split_tests_generates_separate_jobs(tmp_path):
    """When split_tests = true, generates separate Build and Test jobs."""
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY, split_tests=True, coverage=True, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
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
    # No Build stage — everything stays in Test
    ci = yaml.safe_load(content)
    build_jobs = {name: job for name, job in ci.items()
                  if isinstance(job, dict)
                  if "xmsconan job build" in str(job.get("script", ""))}
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
    run one Nth of the suite. The shards are processes forked inside the single
    container this job already has -- ``xmsconan job test`` reads the count
    from build.toml, so the template no longer renders it as a flag and the
    absence of ``parallel:`` is the whole of what it still decides.
    """
    toml_file = write_gitlab_toml(tmp_path, split_tests=True, test_shards=4, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    ci = yaml.safe_load(content)
    assert "parallel" not in ci["Run C++ Tests - Debug-testing"]
    # The GTEST_* variables are the shard runner's business now. Exporting them
    # from the job would pin every shard in the container to the same index.
    assert "GTEST_TOTAL_SHARDS" not in content
    assert "GTEST_SHARD_INDEX" not in content
    # The merged report is what makes a failing case visible in the MR widget.
    junit = ci["Run C++ Tests - Debug-testing"]["artifacts"]["reports"]["junit"]
    assert junit == "TEST-cxxtest.xml"


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


def test_gitlab_every_split_test_job_runs_the_job_command(tmp_path):
    """Each test job hands its suite to ``xmsconan job test``, naming its label.

    Every test job, not just whichever one happens to render first -- the
    label is the only thing that differs between them, so a template that
    emitted the command once and the label twice would run one configuration's
    suite twice and the other's never.

    Whether that suite runs under a display is the tool's decision now: it
    reads ``xvfb`` from build.toml, and starts one server per shard because
    ``xvfb-run -a`` picks a free server number and then races N simultaneous
    starts to bind it. ``tests/test_job_cli.py`` holds that end.
    """
    toml_file = write_gitlab_toml(tmp_path, split_tests=True, xvfb=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    ci_file = output_dir / ".gitlab-ci.yml"
    content = ci_file.read_text(encoding="utf-8")
    ci = yaml.safe_load(content)

    jobs = {name: job for name, job in ci.items() if name.startswith("Run C++ Tests")}
    assert len(jobs) == 2, sorted(jobs)
    for name, job in jobs.items():
        label = name.split(" - ", 1)[1]
        assert f"xmsconan job test --label {label}" in job["script"], (name, job["script"])


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
    # Asserted over every test job rather than as the absence of an
    # unlabelled invocation -- a marker that stops being emitted makes an
    # absence assertion pass without checking anything.
    for name, job in jobs.items():
        runs = [step for step in job["script"] if "xmsconan job test" in step]
        assert len(runs) == 1, (name, job["script"])
        assert "--label" in runs[0].split(), (name, runs[0])


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
    assert "xmsconan coverage" in content
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
    assert 'pip install --upgrade "xmsconan[ci]>=' in content


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
    """The GitLab Coverage stage now invokes xmsconan coverage instead of inline gcovr."""
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
    assert "xmsconan coverage" in content
    # The hand-rolled coverage preset / profile references should be gone.
    assert "linux_testing_debug_coverage" not in content
    assert "cmake --preset coverage" not in content


def test_gitlab_coverage_jobs_pass_no_version(tmp_path):
    """The Coverage stage names no version: ``xmsconan coverage`` reads CI_COMMIT_TAG itself.

    The measure and collect jobs used to receive ``--version ${PACKAGE_VERSION}``
    from an export two lines up, the same ``${CI_COMMIT_TAG:-0.0.0}`` the tool
    now resolves on its own. What must never come back is a literal ``0.0.0``
    on the command, which would label a tag pipeline's report with the
    fallback.
    """
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

    coverage_lines = [line.strip() for line in content.splitlines() if "xmsconan coverage" in line]
    assert coverage_lines
    assert [line for line in coverage_lines if "--version" in line] == []
    assert "PACKAGE_VERSION" not in content


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


def test_gitlab_pages_builds_the_site_with_the_tool_and_no_inline_markup(tmp_path):
    """One `pages:` job, running `job coverage --pages`, with no HTML in the yaml.

    The landing page used to be fourteen `echo`s inside a block scalar,
    rendered on both Jinja branches -- duplicated markup in a template, which
    is the shape where the two copies drift. It also copied and linked the
    C++ report unconditionally, which failed the job outright when the
    directory was absent and served a 404 -- green -- when it was there and
    empty. Both are :mod:`xmsconan.job_tools.pages`' problem now, and
    ``tests/test_job_pages.py`` holds the links, the empty-report case and
    the missing-report case.
    """
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
    parsed = yaml.safe_load(content)

    assert any("xmsconan job coverage --pages" in step
               for step in parsed["pages"]["script"]), parsed["pages"]
    assert parsed["pages"]["artifacts"]["paths"] == ["public"]
    # No markup, and no shell reaching for a report that may not be there.
    assert "index.html" not in content
    assert "coverage-html-cpp public/cpp" not in content


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


def test_github_coverage_installs_nothing_but_the_extra(tmp_path):
    """Coverage.yaml has one install line, and it asks for ``xmsconan[ci]``.

    gcovr's major bound and conan's patch series live in the extra now, so a
    bare ``pip install conan wheel gcovr`` here would make this the one job
    measuring with a toolchain the build workflow did not resolve.
    """
    toml_file = write_github_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "Coverage.yaml").read_text(encoding="utf-8")

    install_lines = [line.strip() for line in content.splitlines()
                     if "pip install" in line and not line.strip().startswith("#")]
    assert len(install_lines) == 1, install_lines
    assert install_lines[0].startswith('pip install --upgrade "xmsconan[ci]>='), install_lines[0]


def test_github_coverage_reads_the_tag_without_an_action(tmp_path):
    """Coverage.yaml passes no version and runs no action to find the tag.

    Two third-party actions used to find the tag and write XMS_VERSION into
    GITHUB_ENV on ``refs/tags/*``; ``xmsconan coverage`` reads GITHUB_REF_NAME
    itself when GITHUB_REF_TYPE says it is a tag, so the report advertises the
    tag with nothing outside GitHub's own namespace running in the job.
    """
    toml_file = write_github_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "Coverage.yaml").read_text(encoding="utf-8")

    assert "run: xmsconan coverage build.toml" in content
    for gone in ("XMS_VERSION", "get-git-tag", "set-env", "xmsconan coverage --version"):
        assert gone not in content, gone


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


def _job_build_tokens(job):
    """The single ``xmsconan job build`` invocation in a job's script, tokenized."""
    steps = [step for step in job.get("script", []) if "xmsconan job build" in step]
    assert len(steps) == 1, steps
    return steps[0].split()


def _tool_export_name(toml_path, job, platform_key=None, platform="linux"):
    """The tarball ``xmsconan job build --export`` saves when this job runs it.

    The save name used to be rendered into the build job beside the restore
    name in the deploy job, where a single template kept the two in step. The
    deploy globs now, so what is left to check is the exporting jobs against
    each other: they write into one artifact space, and a name two of them
    share leaves whichever finished last as the only tarball there. Only the
    tool can answer what a job writes -- it composes the name from the flags
    on the job's command and the environment GitLab gives it -- so this
    composes it the way :func:`~xmsconan.job_tools.build.job_build` does:
    real packager, build.toml ``[filter]``, then the leg filter.

    The job's own rendered ``variables:`` stand in for the environment, which
    is what makes ``BUILD_TYPE`` and ``PYTHON_TARGET_VERSION`` part of the
    assertion rather than assumptions restated here. They are patched into
    ``os.environ`` as well as passed, because the packager resolves the ABI
    it fans pybind out over from the process environment
    (``XmsConanPackager._resolve_python_versions``) rather than from anything
    a caller hands it -- so a job's matrix is not reproducible without them.
    """
    tokens = _job_build_tokens(job)
    leg = tokens[tokens.index("--leg") + 1] if "--leg" in tokens else None
    environ = {key: str(value) for key, value in (job.get("variables") or {}).items()}

    config = read_build_toml(str(toml_path))
    with patch_env(environ):
        builder = job_build._make_packager(config, str(toml_path), False, platform_key)
        builder.generate_configurations(system_platform=platform_key or platform)
    if config.filter:
        builder.filter_configurations(config.filter)
    leg_filter = job_common.resolve_leg(
        leg=leg,
        release=True,
        release_skips_testing="--release-skips-testing" in tokens,
        environ=environ,
    )
    if leg_filter:
        builder.filter_configurations(leg_filter)
    return job_build.export_tarball_name(
        config.library_name, "{version}", builder.configurations, leg=leg,
        platform_key=platform_key, platform=platform, environ=environ,
    )


def _named_tarballs(job):
    """Every ``.tar.gz`` path a job's script spells out.

    Expected to be empty on a deploy job. ``job deploy`` globs ``.export/``,
    so a path here means some step went back to reassembling a name the build
    already composed -- the disagreement that only surfaces on a tag, in the
    job that publishes.
    """
    return [token for step in job.get("script", []) for token in step.split()
            if token.endswith(".tar.gz")]


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
    # ...and one command restores the whole set and uploads once. Which
    # tarballs those are is not spelled here: the tool globs `.export/`, so
    # the two ABIs' names cannot fall out of step with what the builds wrote.
    assert deploy["script"][-1] == "xmsconan job deploy --conan-only", deploy["script"]
    assert _named_tarballs(deploy) == [], deploy["script"]


@pytest.mark.parametrize("linux_python_versions", [["3.13", "3.14"], ["3.13"]],
                         ids=["fanout", "single"])
def test_gitlab_linux_export_names_stay_distinct_and_unspelled(tmp_path,
                                                               linux_python_versions):
    """Exporting jobs write distinct names, and no deploy job names one.

    Two halves of the same hazard. Every exporting job writes into one
    artifact space, so a shared name leaves whichever finished last as the
    only tarball there and the deploy still exits 0 having restored *a*
    package and uploaded it -- that is why the names must differ.

    The pairing itself used to need holding too: the deploy restored by name,
    computed by the template at generation time, against a save computed by
    :func:`~xmsconan.job_tools.build.export_tarball_name` at run time, so a
    rename on either side failed only on a tag, in the job that publishes.
    ``job deploy`` globs ``.export/`` now, which removes the second name
    rather than checking it against the first -- so what is asserted here is
    that no path came back.

    Asserted with one ABI as well as two. Nothing would collide on a bare name
    with a single exporting job, which is exactly the condition under which a
    qualified save and an unqualified restore both look reasonable in
    isolation.
    """
    toml_file = write_gitlab_toml(tmp_path, matrix_table=WHEEL_ONLY,
                                  linux_python_versions=linux_python_versions)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    parsed = yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    exporters = {name: job for name, job in parsed.items()
                 if isinstance(job, dict) and "Windows" not in name
                 if "--export" in str(job.get("script", ""))}
    assert len(exporters) == len(linux_python_versions), sorted(exporters)

    saved = {name: _tool_export_name(toml_file, job) for name, job in exporters.items()}
    assert len(set(saved.values())) == len(saved), saved

    deploy_job = parsed["Conan Deploy - Linux"]
    # Positive first: an emptied or renamed script: would satisfy the
    # absence below on its own, and this test would go on passing while the
    # pipeline published nothing.
    assert deploy_job["script"][-1] == "xmsconan job deploy --conan-only"
    assert _named_tarballs(deploy_job) == [], saved


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


def _build_steps(pipeline):
    """Every ``xmsconan job build`` invocation in *pipeline*, by job name."""
    steps = {}
    for name, job in pipeline.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("script") or []:
            if "xmsconan job build" in step:
                steps[name] = step
    return steps


@pytest.mark.parametrize("matrix_table, deferring", [
    (None, {"Conan Build"}),
    (WHEEL_ONLY, {"Release Build", "Debug Build"}),
])
def test_gitlab_split_tests_defers_only_the_builds_with_a_downstream_test_job(
        tmp_path, matrix_table, deferring):
    """``--defer-cxx-tests`` marks the builds whose runner another job runs.

    The tool cannot work this out for itself, and the collapse to
    ``xmsconan job build`` is why: on a non-wheel_only repository the Linux
    "Conan Build" and "Conan Build - Windows" render the *same* flagless
    command, and only one of them has "Run C++ Tests" jobs downstream. A rule
    keyed on ``--leg`` skipped neither, so the C++ suite ran inline in the
    build and again in each test job; a rule keyed on ``leg is None`` would
    skip the Windows suite, which nothing else runs.

    ``tests/test_job_common.py`` holds the other end -- that the flag is what
    sets ``XMS_SKIP_CXX_TESTS``, and that ``[ci].split_tests`` still gates it.
    """
    flags = {} if matrix_table is None else {"matrix_table": matrix_table}
    steps = _build_steps(_gitlab_jobs(tmp_path, split_tests=True, **flags))

    assert steps, "no build job rendered; the rest of this asserts nothing"
    assert {name for name, step in steps.items()
            if "--defer-cxx-tests" in step} == deferring
    # Named rather than left to the set comparison: the Windows job is the one
    # a plausible fix gets wrong, because no Windows test job exists to notice.
    assert "--defer-cxx-tests" not in steps["Conan Build - Windows"]


@pytest.mark.parametrize("matrix_table", [None, WHEEL_ONLY])
def test_gitlab_defers_nothing_without_split_tests(tmp_path, matrix_table):
    """With no separate test job, the build is where the suite runs."""
    flags = {} if matrix_table is None else {"matrix_table": matrix_table}
    steps = _build_steps(_gitlab_jobs(tmp_path, **flags))

    assert steps, "no build job rendered; the rest of this asserts nothing"
    assert all("--defer-cxx-tests" not in step for step in steps.values()), steps


def _gitlab_jobs(tmp_path, **ci_flags):
    """Render a GitLab pipeline and return its parsed YAML."""
    toml_file = write_gitlab_toml(tmp_path, **ci_flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    return yaml.safe_load((output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"))


class TestVs2019Ci:
    """The msvc 192 opt-in: the jobs it adds, and the toolchain separation it needs.

    Grouped because they share one premise -- two Windows toolchains building
    the same reference on one runner fleet -- and because the msvc 194
    assertions here only make sense next to the msvc 192 ones they mirror.
    """

    def test_gitlab_windows_publishes_name_their_toolchain_and_no_version(self, tmp_path):
        """Each Windows deploy selects its toolchain by --platform, not by a literal.

        The hazard is symmetric and the mitigation has to be. A runner's Conan cache
        is per machine, not per job, and `conan cache save <ref>:*` / `conan upload
        <ref>` match by *reference* -- so with both toolchains building the same
        reference on the same fleet at the same time, an unqueried step on either
        side ships the other's binaries. The msvc 192 direction pollutes a legacy
        remote; the msvc 194 direction pollutes the production one.

        Neither restriction is rendered here any more. ``xmsconan job deploy``
        reads the version from the packager matrix row ``--platform`` selects
        (msvc 194 by detecting Windows, msvc 192 by being told), which is why a
        `compiler.version=` anywhere in this file is now a *defect*: a literal
        that fell behind a toolchain bump would match nothing and publish
        nothing, green. ``tests/test_job_deploy.py`` holds the remote and query
        each platform resolves to; the build jobs' half is in
        ``tests/test_job_build.py``.

        Asserted with the opt-in *off* as well, because the msvc 194 side must
        not depend on the msvc 192 one being configured.
        """
        for vs2019 in (False, True):
            case_dir = tmp_path / f"vs2019-{vs2019}"
            case_dir.mkdir()
            toml_file = write_gitlab_toml(case_dir, windows_vs2019=vs2019)
            output_dir = case_dir / "output"
            generate_ci(str(toml_file), "1.0.0", str(output_dir))
            content = (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8")
            pipeline = yaml.safe_load(content)

            assert "--package-query" not in content, vs2019
            for step in [s for job in pipeline.values() if isinstance(job, dict)
                         for s in job.get("script", [])]:
                assert "compiler.version=" not in step, step

            deploy, = pipeline["Conan Deploy - Windows"]["script"][-1:]
            assert deploy == "xmsconan job deploy --conan-only", deploy
            if vs2019:
                legacy, = pipeline["Conan Deploy - Windows VS2019"]["script"][-1:]
                assert legacy == (
                    "xmsconan job deploy --conan-only --platform windows_vs2019"
                ), legacy

    def test_gitlab_vs2019_jobs_are_opt_in(self, tmp_path):
        """No [ci].windows_vs2019 means no msvc 192 jobs.

        The matrix roughly doubles the Windows half of a pipeline and only the
        libraries the VS2019-era desktop products consume need it, so a repository
        that has never asked for msvc 192 must not start building it because its
        runner acquired a second toolchain.
        """
        pipeline = _gitlab_jobs(tmp_path)

        assert "Conan Build - Windows VS2019" not in pipeline
        assert "Conan Deploy - Windows VS2019" not in pipeline
        # The msvc 194 jobs are untouched by the opt-in being absent.
        assert "Conan Build - Windows" in pipeline

    def test_gitlab_vs2019_build_selects_the_msvc_192_matrix(self, tmp_path):
        """The build job passes --platform windows_vs2019 and nothing else does.

        That flag is the whole difference in what gets compiled, and it is now
        the *only* difference the template spells: ``xmsconan job build``
        derives the rest from it -- the msvc 192 matrix, the legacy remote, the
        ``--build-missing`` the not-fully-prebuilt legacy graph needs, the
        dropped boost option defaults boost/1.74.0.3 does not declare, the
        suppressed wheel, and the tarball name. So the msvc 194 job acquiring
        this flag would not be one wrong argument, it would be six.
        """
        pipeline = _gitlab_jobs(tmp_path, windows_vs2019=True)

        builds = _job_build_tokens(pipeline["Conan Build - Windows VS2019"])
        assert "--platform" in builds
        assert builds[builds.index("--platform") + 1] == "windows_vs2019"

        other = _job_build_tokens(pipeline["Conan Build - Windows"])
        assert "--platform" not in other, other

    def test_gitlab_vs2019_build_publishes_no_wheel(self, tmp_path):
        """No --wheel-dir, and no wheel artifact, on either msvc 192 job.

        A wheel's tags (cp310-cp310-win_amd64) say nothing about which MSVC built
        it, so an msvc 192 wheel and an msvc 194 wheel are the same devpi filename
        -- publishing both would have them overwrite each other by upload order.
        """
        pipeline = _gitlab_jobs(tmp_path, windows_vs2019=True)

        job = pipeline["Conan Build - Windows VS2019"]
        # The staging half is the job command's (tests/test_job_build.py);
        # collecting it is the template's, and is what still reads here.
        assert all("wheel" not in path for path in job["artifacts"]["paths"])
        # And no deploy job was added for a wheel that is never staged.
        assert "Wheel Deploy - Windows VS2019" not in pipeline

    def test_gitlab_vs2019_deploy_says_which_toolchain_and_nothing_else(self, tmp_path):
        """``--platform windows_vs2019`` is the whole of what this job declares.

        It is what routes the upload to the legacy remote and restricts it to
        msvc 192, and it is the only Windows deploy job carrying it. The Conan
        cache on a runner is per machine, not per job, so an upload matching by
        reference alone would carry the msvc 194 job's binaries onto a remote
        whose only purpose is to keep the two toolchains apart -- and exit 0
        having done it.

        What the flag resolves to is ``tests/test_job_deploy.py``' subject: the
        remote name, the query, and that the legacy remote is *appended* rather
        than inserted first -- on a shared runner it must not become the first
        stop for every ``conan install`` -- with the CI remote still configured
        alongside it, because the recipe's own dependencies resolve from there
        even on the legacy toolchain. The build job's half is the same flag,
        held in ``tests/test_job_build.py``.
        """
        pipeline = _gitlab_jobs(tmp_path, windows_vs2019=True)

        deploys = [step for step in pipeline["Conan Deploy - Windows VS2019"]["script"]
                   if step.startswith("xmsconan job deploy")]
        assert deploys == ["xmsconan job deploy --conan-only --platform windows_vs2019"]

        # ...and only on that job. The msvc 194 deploy detects Windows and
        # publishes to the CI remote; naming the legacy platform there would
        # send the production binaries to the legacy remote.
        others = [step for name, job in pipeline.items()
                  if isinstance(job, dict) and name != "Conan Deploy - Windows VS2019"
                  for step in job.get("script", [])
                  if step.startswith("xmsconan job deploy")]
        assert others and all("--platform" not in step for step in others), others

    def test_gitlab_vs2019_export_is_distinct_and_no_deploy_names_it(self, tmp_path):
        """The two Windows toolchains export different names into one space.

        Both Windows build jobs write into the same artifact directory, so the
        msvc 192 tarball carries an extra ``-vs2019-`` segment; without it the
        second job to finish would be the only tarball there and the deploy
        would exit 0 having published the wrong toolchain's binaries.

        The deploy used to have to spell that name too, computed by the
        template at generation time against a save computed by the tool at run
        time -- a rename on either side failing only on a tag, in the job that
        publishes. ``job deploy`` globs ``.export/`` now, so what is asserted
        is that neither Windows deploy names a tarball at all.
        """
        pipeline = _gitlab_jobs(tmp_path, windows_vs2019=True)

        # Neither Windows build job names a leg, so the tarball's discriminator
        # is the ABI straight from the environment and no matrix is consulted.
        # The build fans out over ABIs, which is why the variable stays
        # unexpanded and can be passed through as the literal it is.
        def saved_name(platform_key):
            return job_build.export_tarball_name(
                "xmssnap", "{version}", configurations=[], leg=None,
                platform_key=platform_key, platform="win32",
                environ={job_common.PYTHON_TARGET_VARIABLE: "${PYTHON_TARGET_VERSION}"},
            )

        saved = saved_name("windows_vs2019")
        assert "-vs2019-" in saved
        assert saved != saved_name(None)

        for name in ("Conan Deploy - Windows", "Conan Deploy - Windows VS2019"):
            # Positive first, for the reason given on the Linux deploy: an
            # empty script: satisfies an absence assertion by itself.
            assert pipeline[name]["script"][-1].startswith(
                "xmsconan job deploy --conan-only"), name
            assert _named_tarballs(pipeline[name]) == [], name

    def test_gitlab_vs2019_jobs_match_the_msvc_194_shape(self, tmp_path):
        """Same runner, same ABI fan-out, same tag-gated deploy as the msvc 194 pair."""
        pipeline = _gitlab_jobs(
            tmp_path, windows_vs2019=True, python_versions=["3.10", "3.13"],
        )

        for name in ("Conan Build - Windows VS2019", "Conan Deploy - Windows VS2019"):
            job = pipeline[name]
            assert job["image"] == "GLR-UV", name
            assert job["tags"] == ["WinVM"], name
            assert job["script"].index("uv venv --python ${PYTHON_TARGET_VERSION} .venv") < \
                job["script"].index("source .venv/Scripts/activate"), name

        # The build fans out over the ABIs; the deploy pins one, because
        # restoring and uploading is ABI-independent and N instances would
        # each restore the whole artifact set and race on one Conan reference.
        matrix = pipeline["Conan Build - Windows VS2019"]["parallel"]["matrix"]
        assert [entry["PYTHON_TARGET_VERSION"] for entry in matrix] == ["3.10", "3.13"]
        assert "parallel" not in pipeline["Conan Deploy - Windows VS2019"]
        assert pipeline["Conan Deploy - Windows VS2019"]["variables"] == {
            "PYTHON_TARGET_VERSION": "3.13",
        }

        assert pipeline["Conan Build - Windows VS2019"]["stage"] == \
            pipeline["Conan Build - Windows"]["stage"]
        deploy = pipeline["Conan Deploy - Windows VS2019"]
        assert deploy["stage"] == "Deploy"
        assert deploy["only"] == ["tags"]
        assert deploy["needs"] == [{"job": "Conan Build - Windows VS2019", "artifacts": True}]

    def test_gitlab_vs2019_without_deploy_exports_nothing(self, tmp_path):
        """[ci].deploy = false leaves the msvc 192 build with no export and no deploy job.

        `artifacts: paths:` with nothing under it is not a harmless no-op -- GitLab
        parses the key as null and rejects the file -- so the build job has to keep
        a path of its own when the export goes away.
        """
        pipeline = _gitlab_jobs(tmp_path, windows_vs2019=True, deploy=False)

        job = pipeline["Conan Build - Windows VS2019"]
        assert job["artifacts"]["paths"] == ["test_artifacts/"]
        assert all(not step.startswith("xmsconan_conan_deploy") for step in job["script"])
        assert "Conan Deploy - Windows VS2019" not in pipeline

    def test_gitlab_vs2019_requires_windows(self, tmp_path):
        """windows_vs2019 with [ci].windows = false is rejected, not silently honored.

        The msvc 192 jobs are an addition to the Windows jobs; emitting them under
        [ci].windows = false would resurrect the Windows half of a pipeline the
        repository asked not to have.
        """
        toml_file = write_gitlab_toml(tmp_path, windows_vs2019=True, windows=False)

        with pytest.raises(ValueError, match=r"windows_vs2019 = true with \[ci\].windows"):
            generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))

    def test_github_warns_that_vs2019_is_gitlab_only(self, tmp_path, caplog):
        """A GitHub project opting into msvc 192 is told it gets nothing.

        windows_vs2019 is a plain bool, so it cannot join the tri-state flags the
        neighbouring warning covers -- and silence here would leave a repository
        believing it publishes msvc 192 until a consumer failed to resolve it.
        """
        toml_file = write_github_toml(tmp_path, windows_vs2019=True)

        with caplog.at_level(logging.WARNING):
            generate_ci(str(toml_file), "1.0.0", str(tmp_path / "output"))

        assert "windows_vs2019" in caplog.text
        assert "GitLab-only" in caplog.text


def test_gitlab_windows_build_keeps_its_wheel_as_an_artifact(tmp_path):
    """The Windows build job keeps the wheel it stages, and only the wheel.

    Staging it and collecting it are two halves of one thing: without the
    artifact entry the wheel is built and then discarded when the job ends,
    which is what left Windows with no publishable wheel. The staging half is
    ``xmsconan job build``'s -- it stages one whenever a pybind configuration
    survived its filters -- so this end is what the template still decides.
    """
    job = _gitlab_jobs(tmp_path, windows=True)["Conan Build - Windows"]
    # The wheels alone: `when: always` on the whole directory would upload the
    # several hundred DLLs the repair staging leaves in wheelhouse/libs.
    assert f"{job_common.WHEEL_DIR}/*.whl" in job["artifacts"]["paths"]
    assert f"{job_common.WHEEL_DIR}/" not in job["artifacts"]["paths"]


@pytest.mark.parametrize("repair", [True, False, None])
def test_gitlab_windows_wheel_repair_is_not_a_rendered_step(tmp_path, repair):
    """No Windows job renders a repair step, at any setting of the opt-in.

    delvewheel reads the DLL imports of a win_amd64 .pyd, so the repair cannot
    be delegated to the manylinux container that repairs the Linux wheel: it
    has to happen inside the build job, which is now the job command's own
    business. It reads ``windows_wheel_repair`` from build.toml and repairs
    only on a Windows host, so a rendered step would be a second, unguarded
    opinion -- one that would run the repair on a runner where the opt-in is
    off, vendoring a private mangled msvcp140 beside a .pyd whose host
    supplies that runtime deliberately.

    Parametrized over the settings rather than looped in the body, because a
    step rendered unconditionally and a step rendered under the flag fail
    differently, and a loop reports one result for all three.
    ``tests/test_job_build.py`` holds the tool's end of both.
    """
    flags = {} if repair is None else {"windows_wheel_repair": repair}
    job = _gitlab_jobs(tmp_path, windows=True, **flags)["Conan Build - Windows"]

    # Anchored on the command that owns the decision now: without it both
    # absences below would hold just as well for a job with no script at all,
    # or one whose build step stopped rendering.
    assert any("xmsconan job build" in step for step in job["script"]), job
    assert not any("wheel_repair" in step for step in job["script"]), (repair, job)
    assert not any("--skip-dependency-libs" in step for step in job["script"]), repair


def test_gitlab_windows_wheel_deploy_survives_a_skipped_repair(tmp_path):
    """The unrepaired wheel is what gets uploaded; the deploy job stays."""
    pipeline = _gitlab_jobs(tmp_path, windows=True, windows_wheel_repair=False)

    assert any("xmsconan job deploy --wheels-only" in step
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


def test_github_windows_repairs_inside_its_build_step(tmp_path):
    """The Windows job has no repair step of its own, at either setting.

    Only a Windows host can run delvewheel, so the repair happens in the
    build job either way -- as a separate step before, and inside `job
    build` now. [ci].windows_wheel_repair reaches that from build.toml, so
    it renders nothing here and both settings produce the same workflow;
    what the key does is covered in tests/test_job_build.py.
    """
    on, _jobs = _github_windows_job(tmp_path)
    off, jobs = _github_windows_job(tmp_path, windows_wheel_repair=False)

    assert _step_runs(on) == _step_runs(off)
    assert not any("Repair" in str(step.get("name", "")) for step in on["steps"])
    assert not any("--skip-dependency-libs" in run for run in _step_runs(on))
    # Linux and macOS repair in a step of their own, in the image that can:
    # a manylinux wheel has to be repaired to be installable.
    other_repairs = [
        step.get("name") for name, job in jobs.items() if name != "windows"
        for step in job["steps"] if "xmsconan job package" in str(step.get("run", ""))
    ]
    assert other_repairs == ["Repair wheel", "Repair wheel"]


#: The step that builds the library, in every platform job.
BUILD_STEP_NAME = "Build the Conan Packages"

#: The GitHub jobs that build the library, as opposed to linting it.
BUILDING_JOBS = ("mac", "linux", "linux-arm", "windows")

#: Every (job, step) that reads the wheel the build step staged. Windows has
#: no "Repair wheel": delvewheel runs on a Windows host only, so `job build`
#: repairs there in place rather than in a step of its own.
WHEEL_CONSUMING_STEPS = sorted(
    (job, step)
    for job in BUILDING_JOBS
    for step in ("Repair wheel", "Upload wheel artifact", "Upload wheel to Aquapi")
    if not (job == "windows" and step == "Repair wheel")
)


@pytest.fixture
def github_arm_jobs(tmp_path):
    """The parsed jobs of a GitHub workflow with every platform job present."""
    return _github_jobs(write_github_toml(tmp_path, linux_arm=True), tmp_path)


def _is_build_step(step):
    """Whether a step compiles the library, rather than consuming what it built.

    Matched on the prefix rather than on equality because the Windows job used
    to carry two, a branch one and a tag one under mutually exclusive ``if:``
    conditions. Both spelled a JSON matrix filter inline, which is what made
    them two: ``shell: cmd`` expands ``%VAR%`` before the argument is parsed,
    so the ``env:`` expression the other legs selected would have broken its
    own quoting there. There is no JSON left to quote -- and the prefix match
    stays, so a second build step reappearing is a failure rather than an
    invisible extra.
    """
    return str(step.get("name", "")).startswith(BUILD_STEP_NAME)


def _build_step_run(job, job_name):
    """Return the ``run:`` text of one job's build step."""
    for step in job["steps"]:
        if _is_build_step(step):
            return str(step["run"])
    raise AssertionError(f"no {BUILD_STEP_NAME!r} step in the {job_name} job")


def _touches_the_wheelhouse(step):
    """Whether a step reads the wheel the build staged.

    Matched on the step's name as well as its inputs. The directory alone no
    longer finds them: `job package` and `job deploy --wheels-only` read
    common.WHEEL_DIR themselves, so the path those commands used to spell out
    appears in the workflow only in the artifact upload's ``with.path``.
    """
    text = " ".join(str(value) for value in step.get("with", {}).values())
    return "wheelhouse" in text or "wheel" in str(step.get("name", "")).lower()


def test_github_every_platform_job_builds(github_arm_jobs):
    """The build step exists on each platform job, and only on those."""
    building = [
        name for name, job in github_arm_jobs.items()
        if any(_is_build_step(step) for step in job.get("steps", []))
    ]
    assert sorted(building) == sorted(BUILDING_JOBS)


def _github_leg_configurations(toml_path, job, job_name, release):
    """What ``xmsconan job build`` actually builds when this GitHub job runs it.

    The GitLab twin of this is :func:`_tool_export_name`, and the reason both
    exist is the same: the template no longer states the matrix leg, so the
    only thing that can say what a rendered job builds is the tool, given
    that job's own ``env:``. Rendering assertions cannot reach it -- a job
    whose command and environment are both exactly as intended can still
    resolve to nothing, and a golden file pins that outcome as readily as any
    other.

    Composed the way :func:`~xmsconan.job_tools.build.job_build` composes it:
    real packager, build.toml ``[filter]``, then the leg filter from the
    job's environment. The environment is patched as well as passed, because
    the packager resolves the ABI it fans pybind out over from the process
    environment rather than from anything a caller hands it.
    """
    # The packager's own platform keys, not sys.platform values: they are
    # what name the matrix a `system_platform` selects.
    platform = {"mac": "darwin", "windows": "windows"}.get(job_name, "linux")
    run = _build_step_run(job, job_name)
    environ = {key: _expand_matrix(str(value))
               for key, value in (job.get("env") or {}).items()}

    config = read_build_toml(str(toml_path))
    with patch_env(environ):
        # test_shards passed rather than defaulted: it is job_build's other
        # argument to this call and reaches no configuration, so leaving it off
        # would let a reordered signature keep composing something plausible.
        builder = job_build._make_packager(config, str(toml_path), False, None, 0)
        builder.generate_configurations(system_platform=platform)
    if config.filter:
        builder.filter_configurations(config.filter)
    leg_filter = job_common.resolve_leg(
        leg=None,
        release=release,
        release_skips_testing="--release-skips-testing" in run.split(),
        environ=environ,
    )
    if leg_filter:
        builder.filter_configurations(leg_filter)
    return builder.configurations


def _expand_matrix(value, build_type="Debug", python_version="3.13"):
    """Substitute the matrix expressions a job's ``env:`` interpolates.

    GitHub expands ``${{ matrix.* }}`` per leg; a test standing in for the
    runner has to pick one. Debug is the leg picked, because it is the one a
    filter or a release rule is likeliest to empty -- ``pybind_build_types``
    defaults to Release, so Debug is where a job can be left with nothing.
    """
    return (value.replace("${{ matrix.build_type }}", build_type)
                 .replace("${{ matrix.python-version }}", python_version))


@pytest.mark.parametrize("release", [pytest.param(False, id="branch"),
                                     pytest.param(True, id="tag")])
@pytest.mark.parametrize("job_name", BUILDING_JOBS)
def test_every_github_leg_has_something_to_build(tmp_path, job_name, release):
    """Each rendered platform job resolves to a non-empty matrix, tag or branch.

    ``job build`` exits 1 on a leg that matches no configuration, and it
    should: normally that means a ``[filter]`` and a job disagree. But the
    job's environment is now the only thing narrowing the matrix, so the
    generator can render a job that is empty by construction and nothing else
    here would notice -- the command reads correctly, the ``env:`` reads
    correctly, and the golden pins both.

    The Debug leg on a tag is the interesting one: it is where the release
    rule and ``[matrix].pybind_build_types`` can cancel out, and where a
    branch pipeline stays green because ``0.0.0`` is not a release version.
    """
    toml_file = write_github_toml(tmp_path, linux_arm=True)
    jobs = _github_jobs(toml_file, tmp_path)

    configurations = _github_leg_configurations(toml_file, jobs[job_name], job_name, release)
    assert configurations, (
        "this job matches no configuration and would exit 1"
    )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Pre-existing, and unchanged by the move onto `job build`: under "
    "[matrix].wheel_only the whole matrix is pybind (Release) plus testing, "
    "so a Debug leg holds only testing configurations -- which is exactly "
    "what a release drops. The old template rendered the same empty set as a "
    "JSON --filter. Branch pipelines stay green because 0.0.0 is not a "
    "release version, so only a tag shows it. The fix is a generator "
    "decision about whether that leg should run on a tag at all, not a "
    "relaxation of job build's empty-match guard."
))
def test_a_wheel_only_github_debug_leg_has_something_to_build_on_a_tag(tmp_path):
    """Known gap, pinned so the fix flips this green loudly."""
    toml_file = write_github_toml(tmp_path, matrix_table=WHEEL_ONLY, linux_arm=True)
    jobs = _github_jobs(toml_file, tmp_path)

    assert _github_leg_configurations(toml_file, jobs["linux"], "linux", release=True)


@pytest.mark.parametrize("job_name", BUILDING_JOBS)
def test_github_build_step_asks_for_no_wheel_directory(github_arm_jobs, job_name):
    """Whether a leg stages a wheel is read from what it built, not gated here.

    The step used to append ``--wheel-dir wheelhouse`` behind
    ``matrix.build_type == 'Release'``, because ``build.py --wheel-dir`` exits
    1 when no wheel came out and [matrix].pybind_build_types defaults to
    Release only. That gate was a proxy for a question `job build` now asks
    directly -- did this job build a pybind configuration the recipe gives a
    wheel -- and the proxy was wrong for the library that names Debug there:
    on Windows the recipe builds no Debug wheel at all (USAGE 7.5).

    Parametrized by job so a failure names the platform, and asserted as
    equality so the four legs cannot drift into four different commands.
    """
    assert _build_step_run(github_arm_jobs[job_name], job_name) == "xmsconan job build"


def test_github_wheel_steps_stay_release_only(github_arm_jobs):
    """Every step that reads the wheelhouse runs only where one is filled.

    A consuming step that lost its ``if:`` would run on a Debug leg with no
    wheelhouse at all -- `job package` raises on an empty one, and `job
    deploy --wheels-only` refuses to publish nothing. The build step is
    excluded because it is the step that *fills* the wheelhouse, and it runs
    on every leg.
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
    assert any("xmsconan job deploy --wheels-only" in step for step in job["script"])
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
    assert any("xmsconan job deploy --wheels-only" in step for step in deploy["script"])
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
    retarget it at new code, which then runs in this job on a runner that has
    logged in to Conan and can write GITHUB_ENV for every later step. The tag
    stays in a trailing comment so the reference is still readable.
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


def test_github_coverage_runs_no_third_party_action(tmp_path):
    """Coverage.yaml uses only ``actions/*``.

    Its two third-party actions existed to find the tag and write it into
    GITHUB_ENV, and the tool reads the tag itself now. So the CI workflow's
    pin test has no twin here: there is nothing to pin, and an assertion that
    every third-party action is pinned is satisfied by an empty list for the
    wrong reason. This holds the list empty on purpose.
    """
    toml_file = write_github_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / ".github" / "workflows" / "Coverage.yaml").read_text(encoding="utf-8")

    assert _USES_RE.findall(content)
    assert _third_party_uses_lines(content) == []


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


def test_github_flake_job_takes_its_plugins_from_the_extra(ci_toml, tmp_path):
    """The flake job installs ``xmsconan[ci]`` and names no plugin itself.

    The .flake8 this job generates sets ``banned-modules = osgeo.*``, an
    option only flake8-tidy-imports registers, and flake8 ignores an option
    no installed plugin claims: the ban was accepted and enforced nothing
    while the job reported the same green as a run that had checked it. The
    plugin list lives in the extra now, where ``test_ci_extra`` holds it
    against the options the generated config sets, so this job cannot lint
    to a different rule set than GitLab's Lint by installing a different list.
    """
    output_dir = tmp_path / "output"
    generate_ci(str(ci_toml), "1.0.0", str(output_dir))
    workflow = workflow_document(output_dir / ".github" / "workflows" / "XmsCore-CI.yaml")

    install_lines = [
        line.strip()
        for step in steps_running(workflow["jobs"]["flake"], "pip install")
        for line in step["run"].splitlines() if "pip install" in line
    ]
    assert [line for line in install_lines if "xmsconan[ci]" in line], install_lines
    plugins = {name for line in install_lines for name in requirement_names(line)
               if name.startswith("flake8") or name == "pep8-naming"}
    assert plugins == set(), plugins


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
    assert "xmsconan job package" in content
    assert "xmsconan job deploy --wheels-only" in content
    assert "path: wheelhouse/*.whl" in content


def test_github_drops_wheel_steps_when_pybind_filtered_off(tmp_path):
    """A library that builds no pybind config gets no wheel steps.

    xmsconan_wheel_repair raises on an empty wheelhouse, and the repair step is
    gated on build_type only — so leaving it in reddens every Release leg.
    """
    toml_file = _write_filtered_toml(tmp_path, filter_table=_NO_PYBIND_FILTER)
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    workflow, content = _github_workflow(tmp_path)
    assert "xmsconan job package" not in content
    assert "--wheels-only" not in content
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
    separate Package job, so both the collected wheel artifact and the
    "Wheel Deploy - Windows" job need the same gate the Linux jobs get. The
    build fails when it is asked for a wheel and extracts none, so an
    un-gated wheel here is a red pipeline on every branch. The two "Conan
    Deploy" jobs publish packages rather than wheels and must survive.
    """
    toml_file = _write_filtered_toml(
        tmp_path, ci_type="gitlab",
        ci_table="\n[ci]\ndeploy = true\nwindows_wheel_repair = true\n",
        filter_table=_NO_PYBIND_FILTER,
    )
    generate_ci(str(toml_file), "1.0.0", str(tmp_path))

    content = (tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    assert all("wheel" not in path
               for path in parsed["Conan Build - Windows"]["artifacts"]["paths"]), \
        "a collected wheel path is a wheel this pipeline never builds"
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

    parsed = yaml.safe_load((tmp_path / ".gitlab-ci.yml").read_text(encoding="utf-8"))

    build = parsed["Conan Build - Windows"]
    assert f"{job_common.WHEEL_DIR}/*.whl" in build["artifacts"]["paths"]
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


@pytest.mark.parametrize("shards", [0, 1, 4])
def test_github_ci_renders_no_shard_flag_at_any_setting(tmp_path, shards):
    """[ci].test_shards reaches the packager without passing through here.

    GitHub has no split-test job -- the runner that built the package is the
    one that tests it -- so the shard count still has to reach the packager,
    which skips cmake.test() during the build and then runs N in-process
    gtest shards. `job build` reads it from build.toml and decides, because
    it is also what knows whether another job runs this suite. Rendering it
    here as well would be a second answer to the same question, and the
    template's copy is the one that goes stale in a checked-in workflow.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'ci_type = "github"\n'
        '\n'
        '[ci]\n'
        f'test_shards = {shards}\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    workflow = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"
    content = workflow.read_text(encoding="utf-8")

    assert "--test-shards" not in content
    assert "run: xmsconan job build" in content


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

    Rendered ``wheel_only``, which is the shape that has more than one
    exporting job and therefore the only one where this can go wrong. It is
    also what keeps the test honest: the assertion is over every build job in
    the pipeline, so a marker that stopped being emitted fails here instead of
    quietly leaving nothing to check.
    """
    parsed = _gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, coverage=True)
    builds = {name: job for name, job in parsed.items()
              if isinstance(job, dict)
              if "xmsconan job build" in str(job.get("script", ""))}
    assert len(builds) > 1, sorted(builds)

    exporting = set()
    for name, job in builds.items():
        exports = "--export" in _job_build_tokens(job)
        assert exports == (_gate(job) != "except:tags"), (name, _gate(job), exports)
        if exports:
            exporting.add(name)
    assert exporting, sorted(builds)


def test_gitlab_windows_tag_pipeline_narrows_its_filter(tmp_path):
    """Windows narrows on a tag by asking the job command to, not through rules.

    Not through two jobs the way Linux does: this is still one job, and
    splitting it would mean either duplicating forty lines or renaming it --
    and "Conan Build - Windows" is the name both Windows deploy jobs point at.

    It was a ``rules:`` block selecting a ``BUILD_MATRIX_FILTER`` variable that
    the build line interpolated, whose two branches said "on a tag, drop the
    testing configurations" and "otherwise, match everything". The whole of
    that is ``--release-skips-testing``: the tool decides at run time, from the
    version it already resolved, which is the same CI_COMMIT_TAG the rule
    tested. Nothing selects it, so there is nothing to leave a job unreachable
    -- which the old block needed its trailing catch-all rule to avoid.
    """
    job = _gitlab_jobs(tmp_path, matrix_table=WHEEL_ONLY, windows=True)["Conan Build - Windows"]

    assert "BUILD_MATRIX_FILTER" not in job.get("variables", {})
    assert "rules" not in job
    assert "--release-skips-testing" in _job_build_tokens(job)


def test_github_build_legs_narrow_on_a_tag_with_one_flag(tmp_path):
    """All four legs, one step each, and the tag test is the tool's.

    This was a step-level ``env:`` expression on the three bash legs and a
    second ``if:``-gated step on the Windows one, both spelling out "on a
    tag, drop the testing configurations". ``--release-skips-testing`` is
    that sentence: `job build` decides from the version it already resolved,
    which is the same tag ``startsWith(github.ref, 'refs/tags/')`` tested.

    Windows is in the loop rather than in a test of its own now. It was
    separate because ``shell: cmd`` expands ``%VAR%`` before the argument is
    parsed, so it could not select a filter through a variable the way the
    others did -- a difference that only existed while the filter was JSON.
    """
    jobs = _github_jobs(write_github_toml(tmp_path, matrix_table=WHEEL_ONLY, linux_arm=True), tmp_path)

    for job_name in BUILDING_JOBS:
        steps = [step for step in jobs[job_name]["steps"] if _is_build_step(step)]
        assert len(steps) == 1, (job_name, steps)
        assert str(steps[0]["run"]) == "xmsconan job build --release-skips-testing", job_name
        assert "if" not in steps[0], job_name
    assert "BUILD_MATRIX_FILTER" not in str(jobs)


def test_github_build_legs_keep_every_configuration_without_wheel_only(tmp_path):
    """A library that publishes Conan packages publishes them from those builds.

    ``--release-skips-testing`` is a flag rather than an unconditional rule
    because the tarball a library job exports has to carry the binaries the
    release ships, testing configurations included.
    """
    jobs = _github_jobs(write_github_toml(tmp_path, linux_arm=True), tmp_path)

    for job_name in BUILDING_JOBS:
        assert _build_step_run(jobs[job_name], job_name) == "xmsconan job build", job_name


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
    assert "--release-skips-testing" not in _job_build_tokens(windows)


def test_gitlab_without_wheel_only_deploys_from_the_one_tarball(tmp_path):
    """One build job means one export, restored and uploaded in one step."""
    parsed = _gitlab_jobs(tmp_path, deploy=True)

    deploy = parsed["Conan Deploy - Linux"]
    assert deploy["dependencies"] == ["Conan Build"]
    assert deploy["script"][-1] == "xmsconan job deploy --conan-only", deploy["script"]
    assert "parallel" not in deploy, (
        "the deploy takes the whole build job's artifacts either way, so a "
        "fan-out here is N instances restoring the same set and racing to "
        "upload one Conan reference"
    )


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


def _builds_a_wheel(job):
    """Whether a job compiles a wheel and hands it on.

    Both halves. ``xmsconan job build`` stages a wheel whenever a pybind
    configuration survives its filters, which the template cannot see -- but a
    wheel only leaves the job through an artifact entry, and that the template
    does decide. The pair is also what keeps the repair and deploy jobs out:
    they collect ``wheelhouse`` too, and matching on the directory alone would
    let every consumer answer this question for itself.
    """
    paths = job.get("artifacts", {}).get("paths", [])
    staged = [path for path in paths if path.startswith(job_common.WHEEL_DIR)]
    return bool(staged) and "xmsconan job build" in _script(job)


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
    if _builds_a_wheel(reaching[name]):
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
        if "xmsconan job package" not in script and "xmsconan_wheel_deploy" not in script:
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


# --- secrets scope ---


CONAN_LOGIN_STEP_ENV = {
    "CONAN_LOGIN_USERNAME": "${{ secrets.CONAN2_USER_SECRET }}",
    "CONAN_PASSWORD": "${{ secrets.CONAN2_PASSWORD_SECRET }}",
}

AQUAPI_STEP_ENV = {
    "AQUAPI_USERNAME": "${{ secrets.AQUAPI_USERNAME_SECRET }}",
    "AQUAPI_PASSWORD": "${{ secrets.AQUAPI_PASSWORD_SECRET }}",
    "AQUAPI_URL": (
        "${{ vars.AQUAPI_URL_DEV || secrets.AQUAPI_URL_DEV"
        " || 'https://public.aquapi.aquaveo.com/aquaveo/dev/' }}"
    ),
    "AQUAPI_URL_SOURCE": (
        "${{ vars.AQUAPI_URL_DEV && 'the AQUAPI_URL_DEV variable'"
        " || secrets.AQUAPI_URL_DEV && 'the AQUAPI_URL_DEV secret'"
        " || 'the built-in default' }}"
    ),
}

#: Every step of one platform build job allowed to carry a secret, by name. A
#: new entry here is a review question -- what does the step do with it -- not
#: a formality.
PLATFORM_BUILD_SECRET_STEPS = frozenset({
    "Build the Conan Packages",
    "Upload Releases to Conan",
    "Upload wheel to Aquapi",
    "Get Release",
    "Upload Zipped Conan Packages",
})

#: What each generated workflow must be handing a credential to, exactly, per
#: job. An allow-list on its own is only an upper bound, and a workflow that
#: lost every credential satisfies it -- the coverage workflow in particular
#: has one secret-bearing step and no wheel or release work, so "no job-level
#: secrets" is true of a Coverage.yaml with no Conan login at all.
#:
#: Keyed by ``(workflow, job)`` rather than by workflow, so the table names the
#: jobs as well as their steps. A per-workflow set says which steps may hold a
#: credential but not which jobs must, leaving the guard to ask each rendered
#: job whether it still reaches the Conan remote -- and a job stripped of its
#: credentials stops qualifying, as the guard's docstring spells out. A leg
#: that legitimately differs, one that publishes no wheel, also gets a row of
#: its own.
#:
#: Written out per job rather than built from a list of build jobs, so that the
#: table reads as the answer to "which steps may hold credentials in which job"
#: without the reader running a comprehension in their head. ``flake`` is in it
#: for the same reason: an empty set is a claim about that job, and one made
#: here rather than inferred from the job's own contents -- it runs ``job
#: lint``, builds nothing and must hold nothing.
#:
#: ``Coverage.yaml`` names ``Run Coverage`` and not the ``Setup Conan`` beside
#: it. That is the one GitHub step still running ``xmsconan conan-setup`` on
#: its own (GitLab keeps two), but ``Run Coverage`` is what resolves this
#: library's dependencies, so that is where the credential goes.
SECRET_HOLDING_STEPS = {
    ("XmsCore-CI.yaml", "flake"): frozenset(),
    ("XmsCore-CI.yaml", "mac"): PLATFORM_BUILD_SECRET_STEPS,
    ("XmsCore-CI.yaml", "linux"): PLATFORM_BUILD_SECRET_STEPS,
    ("XmsCore-CI.yaml", "linux-arm"): PLATFORM_BUILD_SECRET_STEPS,
    ("XmsCore-CI.yaml", "windows"): PLATFORM_BUILD_SECRET_STEPS,
    ("Coverage.yaml", "coverage"): frozenset({"Run Coverage"}),
}

#: The jobs the table names that render only when ``linux_arm`` is on. Every
#: other entry is expected in both legs, so a job that stops rendering is a
#: failure rather than a key the test quietly skips.
ARM_ONLY_JOBS = frozenset({"linux-arm"})

#: Every step name the table allows a credential on, anywhere. Derived, so the
#: USAGE prose check below and the per-job guard stay one statement about one
#: set: a step added to the table is a step the docs must then name.
SECRET_BEARING_STEPS = frozenset().union(*SECRET_HOLDING_STEPS.values())

#: ``linux_arm`` is opt-in (``build_toml.py``), so the default job set leaves it
#: out -- and it is a fourth, separately maintained copy of the build job, the
#: one place a credential can come back on the job with the other three still
#: clean. Every test below renders both ways.
LINUX_ARM = [
    pytest.param(False, id="default-jobs"),
    pytest.param(True, id="with-linux-arm"),
]

GITHUB_WORKFLOWS = ["XmsCore-CI.yaml", "Coverage.yaml"]


def _secrets_workflow(tmp_path, workflow, linux_arm):
    """Render both GitHub workflows for these flags and parse *workflow*."""
    toml_file = write_github_toml(tmp_path, coverage=True, linux_arm=linux_arm)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    return workflow_document(output_dir / ".github" / "workflows" / workflow)


def _secret_env(mapping, label):
    """``env:`` entries of *mapping* whose value names a secret, keyed by *label*."""
    return {
        f"{label}.{key}": value
        for key, value in (mapping.get("env") or {}).items()
        if "secrets." in str(value)
    }


def _step_label(step):
    """A name for *step* that exists even when the step has none."""
    return step.get("name") or step.get("uses") or step.get("run") or "<unnamed step>"


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
@pytest.mark.parametrize("workflow", GITHUB_WORKFLOWS)
def test_github_workflows_keep_secrets_off_the_job_environment(tmp_path, workflow, linux_arm):
    """No workflow-, job-, or container-level ``env:`` value references ``secrets.``.

    All of them are inherited by every step: the pinned third-party actions,
    and ``conan create``, which builds a venv, installs the test dependencies
    from PyPI and runs the library's own suite. Log masking hides a secret
    that is printed; it does nothing about a step that reads ``os.environ``
    and sends it elsewhere. A credential goes on the step that uses it.

    ``container`` and ``services`` are scanned alongside the job because the
    Linux jobs run in a container: GitHub hands ``jobs.<id>.container.env`` to
    every step exactly as it hands it ``jobs.<id>.env``, so a guard reading
    only the latter would pass the same leak one key deeper.
    """
    document = _secrets_workflow(tmp_path, workflow, linux_arm)

    leaks = _secret_env(document, "env")
    for name, job in document["jobs"].items():
        leaks.update(_secret_env(job, f"{name}.env"))
        container = job.get("container")
        if isinstance(container, dict):
            leaks.update(_secret_env(container, f"{name}.container.env"))
        for service, spec in (job.get("services") or {}).items():
            if isinstance(spec, dict):
                leaks.update(_secret_env(spec, f"{name}.services.{service}.env"))

    assert leaks == {}


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
def test_github_ci_gives_the_conan_login_to_every_build_step(tmp_path, linux_arm):
    """Every ``job build`` step carries the login, and carries only it.

    It has to: the build resolves this library's dependencies from the
    private remote, and nothing ahead of it logs in. ``conan remote login``
    is not what happens instead -- conan reads the pair from the environment
    when it needs it, and the same command with no credentials to hand it
    prompts, which on a runner is a job that hangs rather than one that says
    what is missing.

    Naming the step in ``SECRET_HOLDING_STEPS`` pins only that *something*
    secret sits on it; this pins *what*, so halving the env block cannot
    pass on the strength of the surviving key.
    """
    jobs = _secrets_workflow(tmp_path, "XmsCore-CI.yaml", linux_arm)["jobs"]

    builds = {
        name: steps_running(job, "xmsconan job build")
        for name, job in jobs.items()
        if steps_running(job, "xmsconan job build")
    }
    assert builds
    for name, steps in builds.items():
        assert len(steps) == 1, f"{name}: {len(steps)} build steps"
        assert steps[0].get("env") == CONAN_LOGIN_STEP_ENV, name


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
def test_github_coverage_gives_the_login_to_the_run_and_not_to_the_setup(tmp_path, linux_arm):
    """The coverage workflow moved the pair one step later, for the same reason.

    ``xmsconan conan-setup`` writes the remote into this runner's Conan home
    and reaches nothing, so the credentials it used to carry were a
    credential on a step that could not have used them. The coverage run is
    what resolves dependencies, so that is where they go.
    """
    jobs = _secrets_workflow(tmp_path, "Coverage.yaml", linux_arm)["jobs"]

    setups = [step for job in jobs.values() for step in steps_running(job, "xmsconan conan-setup")]
    runs = [step for job in jobs.values() for step in steps_running(job, "xmsconan coverage")]
    assert len(setups) == 1
    assert len(runs) == 1
    assert "env" not in setups[0]
    assert runs[0].get("env") == CONAN_LOGIN_STEP_ENV


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
def test_github_ci_gives_the_conan_login_to_every_package_upload(tmp_path, linux_arm):
    """Every build job's Conan upload carries the login, and carries only it.

    Its own copy, rather than a login the build step left behind: the build
    runs the Conan setup in-process and never writes credentials anywhere,
    so each command that reaches the remote brings its own. There is no
    counterpart in ``Coverage.yaml``: that workflow builds nothing it publishes.
    """
    jobs = _secrets_workflow(tmp_path, "XmsCore-CI.yaml", linux_arm)["jobs"]

    build_jobs = {
        name: job for name, job in jobs.items() if steps_running(job, "xmsconan job build")
    }
    assert build_jobs
    for name, job in build_jobs.items():
        upload = steps_running(job, "xmsconan job deploy --conan-only")
        assert len(upload) == 1, f"{name}: {len(upload)} Conan upload steps"
        assert upload[0].get("env") == CONAN_LOGIN_STEP_ENV, name


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
def test_github_ci_gives_the_index_credentials_to_the_wheel_upload(tmp_path, linux_arm):
    """Every wheel upload carries the index credentials, and carries only them.

    The ``run:`` assertion is not decoration. ``AQUAPI_URL_SOURCE`` says which
    of the variable, the secret, or the built-in default supplied the URL --
    the one line in the job log that tells a maintainer whether the place they
    just edited is the place being read, because the URL itself renders as
    ``***`` whenever the secret still holds it. An env entry no step echoes
    answers nobody, and without this assertion deleting the echo would red
    only the whole-file golden, whose failure text offers ``--update-golden``.
    """
    jobs = _secrets_workflow(tmp_path, "XmsCore-CI.yaml", linux_arm)["jobs"]

    deploy_steps = [
        (name, step)
        for name, job in jobs.items()
        for step in steps_running(job, "xmsconan job deploy --wheels-only")
    ]
    assert deploy_steps
    for name, step in deploy_steps:
        assert step.get("env") == AQUAPI_STEP_ENV, name
        assert "$AQUAPI_URL_SOURCE" in step["run"], f"{name}: source not echoed"


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
@pytest.mark.parametrize("workflow", GITHUB_WORKFLOWS)
def test_github_workflows_hand_secrets_to_exactly_the_documented_steps(tmp_path, workflow, linux_arm):
    """Each job's secret-bearing steps are exactly the ones ``SECRET_HOLDING_STEPS`` names.

    The whole step mapping is scanned, not just its ``env:``: the reset this
    section guards handed a secret to an action through ``with:``. Equality
    rather than a subset, because ``<=`` alone passes a workflow that lost
    every credential and ``>=`` alone passes one that grew a new holder.

    Per job, not per workflow. The build job exists in four separately
    maintained copies, so a union over all of them is satisfied by three:
    dropping ``GITHUB_TOKEN`` from the linux-arm ``Get Release`` alone leaves
    a workflow-wide set of names identical, which is the copy-drift this
    section is here to catch.

    ``expected`` is read out of the table alone -- nothing about it is computed
    from the document under test. Keying it on the rendered job instead, by
    asking whether that job still reaches the Conan remote, makes the guard
    agree with whatever rendered: a job that stops running ``job build`` stops
    being a job that must hold credentials, and the leg that dropped them
    reports no holders against an expectation of none. Both the jobs in
    ``expected`` and the steps each must hold have to come from the table for
    an absence to fail, so every job is named there, including ``flake``,
    which runs no build and must hold nothing.

    That also makes the job set itself part of the assertion: a build job that
    stops rendering fails as a missing key, and a new one fails as an extra,
    rather than either slipping past a guard that only walks what it was given.
    """
    document = _secrets_workflow(tmp_path, workflow, linux_arm)

    holders = {
        name: {_step_label(step) for step in job["steps"] if "secrets." in str(step)}
        for name, job in document["jobs"].items()
    }
    expected = {
        job: set(steps)
        for (table_workflow, job), steps in SECRET_HOLDING_STEPS.items()
        if table_workflow == workflow and (linux_arm or job not in ARM_ONLY_JOBS)
    }

    assert holders == expected


@pytest.mark.parametrize("linux_arm", LINUX_ARM)
def test_github_ci_reads_the_index_url_from_a_variable_then_a_secret(tmp_path, linux_arm):
    """``AQUAPI_URL_DEV`` resolves variable, then secret, then a public default.

    The secret is where this value lived before any of this was generated, so
    reading it keeps a maintainer who edits it there in control of the upload
    target. Dropping it would not have broken a pipeline -- the default is the
    same URL the secret holds -- it would have broken the next person who
    edited the secret and waited for an effect that never came.

    The variable is offered first because the URL is public: a secret renders
    the upload target as ``***`` in the log, which is where an upload to the
    wrong index would have to be noticed.

    ``||`` yields the first *truthy* operand and the empty string is falsy, so
    the order *is* the behaviour -- create a non-empty variable and the secret
    stops mattering, silently. What keeps that from being a new trap is the
    header and the source label -- the header by
    ``test_github_ci_header_documents_the_index_url_precedence``, the label by
    ``test_github_ci_gives_the_index_credentials_to_the_wheel_upload``, which
    pins both the ``AQUAPI_URL_SOURCE`` expression and the ``echo`` that makes
    it visible.

    The expression is spelled out here rather than read from
    ``AQUAPI_STEP_ENV``: that constant is what the sibling tests compare
    against, so sharing it would leave the expression pinned in one place
    only, and the two tests could not cross-check each other.
    """
    toml_file = write_github_toml(tmp_path, coverage=True, linux_arm=linux_arm)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    path = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"

    urls = [
        (name, step["env"]["AQUAPI_URL"])
        for name, job in workflow_document(path)["jobs"].items()
        for step in steps_running(job, "xmsconan job deploy --wheels-only")
    ]
    assert urls
    for name, url in urls:
        assert url == (
            "${{ vars.AQUAPI_URL_DEV || secrets.AQUAPI_URL_DEV"
            " || 'https://public.aquapi.aquaveo.com/aquaveo/dev/' }}"
        ), name


def _flatten_comment(header):
    """*header* with its ``#`` markers and hard wrapping collapsed to one line."""
    return " ".join(header.replace("#", " ").split())


def test_github_ci_header_documents_the_index_url_precedence(tmp_path):
    """The generated header names the resolution order and what defeats it.

    This is the half of the change a maintainer actually meets: the expression
    makes editing the secret work again, and the header is what stops a later
    variable from silently taking that back. Deleting the explanation has to
    fail the suite, or the trap moves instead of closing.

    Asserted against flattened prose rather than the file's own lines. The
    comment is hard-wrapped, so a verbatim needle matches only at today's wrap
    width -- adding a word upstream reflows the block and reds the test with
    the explanation fully intact. Flattening pins the sentences and leaves the
    wrapping free.

    Scoped to the ``AQUAPI_URL_DEV`` paragraph rather than the whole header,
    so the ordering assertion keeps meaning it if some later paragraph above
    this one starts talking about a variable or a secret of its own.

    Not parametrized over ``linux_arm``: the header is the same text whether
    or not the fourth build job renders, and
    ``test_gitlab_header_lists_the_conan_login_variables`` is unparametrized
    for the same reason.
    """
    toml_file = write_github_toml(tmp_path, coverage=True)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    path = output_dir / ".github" / "workflows" / "XmsCore-CI.yaml"

    prose = _flatten_comment(slice_between(
        path.read_text(encoding="utf-8"),
        "# Required repository secrets:", "# Generated by xmsconan_ci", path.name,
    ))
    start = "AQUAPI_URL_DEV - devpi index URL"
    assert start in prose, start
    paragraph = prose[prose.index(start):]

    for needle in (
        "*variable* AQUAPI_URL_DEV",
        "*secret* of the same name",
        "A non-empty variable wins",
        "An empty variable does not win",
        "Masking follows the secret's value",
    ):
        assert needle in paragraph, needle
    assert paragraph.index("*variable*") < paragraph.index("*secret*"), "variable first"


@pytest.mark.parametrize("writer, ci_flags, workflow", [
    pytest.param(write_github_toml, {"coverage": True}, ".github/workflows/XmsCore-CI.yaml", id="github-ci"),
    pytest.param(write_github_toml, {"coverage": True}, ".github/workflows/Coverage.yaml", id="github-coverage"),
    pytest.param(write_gitlab_toml, {"windows": True}, ".gitlab-ci.yml", id="gitlab"),
])
def test_generated_ci_installs_no_devpi_client(tmp_path, writer, ci_flags, workflow):
    """No install line names ``devpi-client`` or ``toml``.

    ``xmsconan wheel-deploy`` uploads with the ``uv`` that xmsconan depends
    on, so nothing a generated job runs calls ``devpi`` any more. xmsconan
    itself still depends on ``devpi-client`` for one release, for
    ``--client devpi``, so the runner keeps receiving it through
    ``pip install xmsconan`` until that goes; the install lines just stop
    asking for it by name. ``toml`` lost its reader when xmsconan moved to
    ``tomli`` and was still on the Windows install line.
    """
    toml_file = writer(tmp_path, **ci_flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / workflow).read_text(encoding="utf-8")

    install_lines = [line for line in content.splitlines() if "pip install" in line]
    assert install_lines
    assert [line for line in install_lines if "devpi" in line] == []
    assert [line for line in install_lines if "toml" in requirement_names(line)] == []


@pytest.mark.parametrize("deploy", [pytest.param(True, id="deploy"), pytest.param(False, id="no-deploy")])
def test_gitlab_header_lists_the_conan_login_variables(tmp_path, deploy):
    """The GitLab header names the Conan pair whether or not the pipeline deploys.

    No generated job sets them -- Conan reads them from the environment
    itself -- so this header is the only place a repository is told to define
    them, and every build needs the remote, not just the tag-time deploy the
    aquapi variables are listed under. Those stay gated, which is what the
    ``no-deploy`` leg holds: a header that listed everything unconditionally
    would satisfy the first two assertions and tell a non-deploying
    repository to create three variables nothing reads.
    """
    toml_file = write_gitlab_toml(tmp_path, windows=True, deploy=deploy)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    header = slice_between(
        (output_dir / ".gitlab-ci.yml").read_text(encoding="utf-8"),
        "# Required CI/CD variables:", "# Generated by xmsconan_ci", ".gitlab-ci.yml",
    )

    assert "CONAN_LOGIN_USERNAME" in header
    assert "CONAN_PASSWORD" in header
    assert ("AQUAPI_PASSWORD" in header) is deploy


def test_usage_documents_step_scoped_github_secrets():
    """USAGE section 10.1 names the variable and every step that carries a secret.

    ``SECRET_HOLDING_STEPS`` drives the step half: the two are one statement
    about the same set, and the pair that carries the release token was
    pinned by the test and left out of the prose until this was keyed off it.
    """
    section = slice_between(
        usage_text(), "### 10.1 GitHub specifics", "### 10.2 GitLab specifics", "docs/USAGE.md",
    )

    for needle in {"AQUAPI_URL_DEV", "AQUAVEO_GITHUB_TOKEN"} | set(SECRET_BEARING_STEPS):
        assert needle in section, needle
