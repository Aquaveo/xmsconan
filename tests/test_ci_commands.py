"""What a generated job runs: ``xmsconan <cmd>``, never a legacy ``xmsconan_*`` script."""
import pytest

from xmsconan.generator_tools.ci_file_generator import generate_ci
from .ci_helpers import LEGACY_SCRIPT, WHEEL_ONLY, write_github_toml, write_gitlab_toml


@pytest.mark.parametrize("writer, flags, workflow, dispatched", [
    pytest.param(write_github_toml, {"coverage": True}, ".github/workflows/XmsCore-CI.yaml",
                 "xmsconan job build", id="github-ci"),
    pytest.param(write_github_toml, {"coverage": True}, ".github/workflows/Coverage.yaml",
                 "xmsconan coverage build.toml", id="github-coverage"),
    pytest.param(write_gitlab_toml, {"windows": True, "windows_vs2019": True, "coverage": True}, ".gitlab-ci.yml",
                 "xmsconan coverage --phase collect", id="gitlab"),
    pytest.param(write_gitlab_toml, {"coverage": True, "matrix_table": WHEEL_ONLY}, ".gitlab-ci.yml",
                 "xmsconan coverage --phase measure", id="gitlab-wheel-only"),
])
def test_generated_jobs_call_xmsconan_rather_than_a_legacy_script(tmp_path, writer, flags, workflow, dispatched):
    """Every command a generated job runs is spelled ``xmsconan <cmd>``.

    The ``xmsconan_*`` scripts stay installed as aliases for anyone typing
    them, but a template that still called one kept two spellings of one tool
    in a single pipeline -- the coverage jobs ran ``xmsconan_coverage`` beside
    build jobs running ``xmsconan job`` -- and left the old names load-bearing
    in every consumer's CI. Comment lines are skipped: the header still names
    ``xmsconan_ci`` as the generator.

    ``dispatched`` is one call each file must contain, so the absence check
    cannot pass on a file that rendered none of the jobs it is about.

    This reads only what the templates render. ``xmsconan coverage``,
    ``xmsconan publish`` and ``xmsconan vs2019`` still spawn ``xmsconan_gen``
    by name, so passing here does not mean the aliases can be dropped.
    """
    toml_file = writer(tmp_path, **flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / workflow).read_text(encoding="utf-8")

    assert dispatched in content
    legacy = [line.strip() for line in content.splitlines()
              if LEGACY_SCRIPT.search(line) and not line.strip().startswith("#")]
    assert legacy == []
