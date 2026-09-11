"""What a generated job runs: ``xmsconan <cmd>``, never a legacy ``xmsconan_*`` script."""
import pytest

from xmsconan.generator_tools.ci_file_generator import generate_ci
from .ci_helpers import (
    LEGACY_SCRIPT,
    uncommented_lines,
    unknown_calls,
    WHEEL_ONLY,
    write_github_toml,
    write_gitlab_toml,
)


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
    """Every command a generated job runs is spelled ``xmsconan <cmd>``, with a ``<cmd>`` that exists.

    The ``xmsconan_*`` scripts stay installed as aliases for anyone typing
    them, but a template that still called one kept two spellings of one tool
    in a single pipeline -- the coverage jobs ran ``xmsconan_coverage`` beside
    build jobs running ``xmsconan job`` -- and left the old names load-bearing
    in every consumer's CI. Comment lines are skipped: the header still names
    ``xmsconan_ci`` as the generator.

    ``dispatched`` is one call each file must contain, so the absence check
    cannot pass on a file that rendered none of the jobs it is about. Neither
    check would notice ``xmsconan conan-setp`` or ``xmsconan job biuld``,
    which fail only at run time and which ``--update-golden`` would copy into
    the golden files, so every ``<cmd>`` is also checked against the
    dispatcher's own table, and every ``job <kind>`` against the parser of
    ``xmsconan job``.

    This reads only what the templates render. ``xmsconan coverage``,
    ``xmsconan publish`` and ``xmsconan vs2019`` still spawn ``xmsconan_gen``
    by name, so passing here does not mean the aliases can be dropped.
    """
    toml_file = writer(tmp_path, **flags)
    output_dir = tmp_path / "output"
    generate_ci(str(toml_file), "1.0.0", str(output_dir))
    content = (output_dir / workflow).read_text(encoding="utf-8")
    lines = uncommented_lines(content)

    assert any(dispatched in line for line in lines)
    assert [line for line in lines if LEGACY_SCRIPT.search(line)] == []
    assert unknown_calls(content) == []


@pytest.mark.parametrize("line, unknown", [
    pytest.param("  - xmsconan conan-setup", [], id="known"),
    pytest.param("  - xmsconan conan-setp", ["xmsconan conan-setp"], id="misspelled"),
    pytest.param("  - xmsconan Conan-setup", ["xmsconan Conan-setup"], id="capitalized"),
    pytest.param('  - bash -c "xmsconan conan-setp"', ["xmsconan conan-setp"], id="quoted-call"),
    pytest.param('  - xmsconan "conan-setp"', ["xmsconan conan-setp"], id="quoted-subcommand"),
    pytest.param(r"  - C:\Py\Scripts\xmsconan conan-setp", ["xmsconan conan-setp"], id="backslash-path"),
    pytest.param("  - xmsconan -v conan-setup", ["xmsconan -v"], id="flag-as-subcommand"),
    pytest.param("  - xmsconan job biuld", ["xmsconan job biuld"], id="misspelled-job-kind"),
    pytest.param('  - xmsconan "job" biuld', ["xmsconan job biuld"], id="quoted-job-misspelled-kind"),
    pytest.param("  - xmsconan conan-setp && xmsconan job biuld", ["xmsconan conan-setp", "xmsconan job biuld"],
                 id="two-calls"),
    pytest.param("  - /opt/python/cp313-cp313/bin/xmsconan job package", [], id="absolute-path"),
    pytest.param('  - xmsconan "job" build', [], id="quoted-job"),
    pytest.param('  - pip install "xmsconan[ci]>=2.30"', [], id="ci-extra"),
    pytest.param("  - xmsconan_ci build.toml", [], id="legacy-script"),
    pytest.param("  # xmsconan conan-setp", [], id="comment"),
])
def test_unknown_calls_reports_each_unknown_subcommand_or_job_kind(line, unknown):
    """``unknown_calls`` names each call whose subcommand or job kind does not exist, and nothing else.

    The rendering tests can only show that today's templates make no unknown
    call; they never meet one, so they cannot show the check would catch it.
    These lines are the shapes a regression would take -- a misspelled,
    capitalized or quoted word, a call behind a Windows path, a flag in the
    subcommand's place -- beside the shapes the check has to pass over. A
    quoted ``job`` is followed by its kind only if the closing quote is
    consumed; otherwise the kind reads as missing and a correct call is
    reported.
    """
    assert unknown_calls(line) == unknown
