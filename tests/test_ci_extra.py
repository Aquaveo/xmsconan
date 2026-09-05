"""The ``[ci]`` extra: what a generated job installs, held against what the templates need."""
from pathlib import Path
import re

import pytest

from xmsconan import generator_tools
from xmsconan.generator_tools.ci_file_generator import generate_ci
from .ci_helpers import ci_extra, requirement_names, write_github_toml, write_gitlab_toml


def _carried_names():
    """The distribution names the extra installs."""
    return {name for requirement in ci_extra() for name in requirement_names(requirement)}


def test_ci_extra_pins_conan_to_a_patch_series():
    """The conan pin is ``~=`` on a three-part version, so only its patch level floats.

    A conan minor bump can change package_id computation and silently detach
    a build from binaries already published to the remote. The pin used to sit
    on five install lines in the GitLab template alone and three more on
    GitHub's; it is one entry here now, and bumping it is a deliberate
    xmsconan release that every generated job then takes.
    """
    conan = [requirement for requirement in ci_extra() if requirement_names(requirement) == {"conan"}]

    assert len(conan) == 1, conan
    assert re.fullmatch(r"conan~=\d+\.\d+\.\d+", conan[0]), conan[0]


def test_ci_extra_bounds_gcovr_below_the_next_major():
    """The gcovr bound stops below the next major: its report format is what ``xmsconan_coverage`` parses."""
    gcovr = [requirement for requirement in ci_extra() if requirement_names(requirement) == {"gcovr"}]

    assert len(gcovr) == 1, gcovr
    assert re.fullmatch(r"gcovr>=\d+,<\d+", gcovr[0]), gcovr[0]


@pytest.mark.parametrize("option, plugin", [
    ("banned-modules", "flake8-tidy-imports"),
    ("import-order-style", "flake8-import-order"),
    ("application-import-names", "flake8-import-order"),
    ("application-package-names", "flake8-import-order"),
    ("docstring-convention", "flake8-docstrings"),
])
def test_ci_extra_carries_the_plugin_behind_each_generated_flake8_option(option, plugin):
    """Every option the generated .flake8 sets has its plugin in the extra.

    flake8 ignores a config option no installed plugin claims, so a missing
    plugin does not fail the lint job -- it quietly stops checking: the
    ``osgeo`` ban was accepted and enforced nothing on GitHub for as long as
    the flake job's hand-written plugin list lacked flake8-tidy-imports. The
    option is asserted present in the template too, so a rename there fails
    here instead of leaving a stale pairing.
    """
    flake8_template = Path(generator_tools.__file__).parent / "templates" / ".flake8.jinja"

    assert f"\n{option} = " in flake8_template.read_text(encoding="utf-8")
    assert plugin in _carried_names()


@pytest.mark.parametrize("writer, ci_flags, workflow", [
    pytest.param(write_github_toml, {"coverage": True}, ".github/workflows/XmsCore-CI.yaml", id="github-ci"),
    pytest.param(write_github_toml, {"coverage": True}, ".github/workflows/Coverage.yaml", id="github-coverage"),
    pytest.param(write_gitlab_toml, {"windows": True, "windows_vs2019": True, "coverage": True}, ".gitlab-ci.yml",
                 id="gitlab"),
])
def test_generated_jobs_install_the_toolchain_only_through_the_extra(tmp_path, writer, ci_flags, workflow):
    """Every xmsconan install asks for ``[ci]``, and no line names a tool the extra carries.

    A line that installed conan, cmake, gcovr or a flake8 plugin by name again
    would be a second copy of a version that lives in pyproject.toml, which is
    the drift the extra exists to end. Two lines are allowed to name something
    else: pip upgrading itself, and the GitLab Lint job's flake8-aquaveo, which
    the extra leaves out on purpose -- the GitHub flake job has never run the
    AQU rules, and whether both hosts should is the lint command's decision to
    make, not this extra's to preempt.
    """
    toml_file = writer(tmp_path, **ci_flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / workflow).read_text(encoding="utf-8")
    carried = _carried_names()

    install_lines = [line.strip() for line in content.splitlines()
                     if "pip install" in line and not line.strip().startswith("#")]
    assert install_lines
    assert [line for line in install_lines if requirement_names(line) & carried] == []
    assert [line for line in install_lines if "xmsconan" in line and "xmsconan[ci]" not in line] == []
    others = [line for line in install_lines if "xmsconan[ci]" not in line]
    assert [line for line in others
            if not line.endswith("pip install --upgrade pip") and "flake8-aquaveo" not in line] == []
