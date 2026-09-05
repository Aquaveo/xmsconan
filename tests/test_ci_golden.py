"""Whole-file comparison of the generated CI against checked-in golden output.

The rest of the CI suite asserts that a particular job holds a particular
string. That catches the thing it names and nothing else: a template edit
lands as a scatter of assertion failures in unrelated tests, or as no
failure at all in the many lines no assertion mentions. These tests compare
the *whole* rendered tree, so the review artifact for a template change is
the diff of its output -- which is the thing a reviewer actually has to
judge.

They do not replace the targeted tests. A golden says "this changed"; only a
named assertion says "and that is wrong". The two fail together on a real
regression, and the golden alone fails on an intended change, which is the
signal to run::

    pytest tests/test_ci_golden.py --update-golden

and put the resulting diff in the pull request.
"""
import pytest

from xmsconan.build_toml import read_build_toml
from .golden_helpers import (
    capture_contexts,
    diff_text,
    gated_context_names,
    GOLDEN_DIR,
    golden_files,
    INPUT_NAME,
    render_variant,
    variants,
    write_golden,
)


@pytest.mark.parametrize("variant", variants())
def test_generated_ci_matches_the_golden_files(tmp_path, monkeypatch, update_golden, variant):
    """Every rendered file equals its golden, and no golden is left over.

    The two halves are separate failures on purpose. A content mismatch is a
    template edit; a set mismatch is a file appearing or disappearing, which
    is the change most likely to be invisible in a suite of substring
    assertions -- nothing asserts on a file that is no longer generated.
    """
    rendered = render_variant(variant, tmp_path, monkeypatch)

    if update_golden:
        write_golden(variant, rendered)
        pytest.skip(f"rewrote the {variant} golden files; nothing was compared")

    expected = golden_files(variant)
    assert set(rendered) == set(expected), (
        f"{variant}: rendered {sorted(rendered)}, golden holds {sorted(expected)} "
        f"-- rerun with --update-golden if that is intended"
    )

    # Every differing file, not the first: a template change usually lands in
    # all of a variant's files, and the reviewer wants the whole diff at once.
    reports = []
    for name, text in sorted(rendered.items()):
        golden = expected[name].read_text(encoding="utf-8")
        if golden != text:
            reports.append(diff_text(variant, name, golden, text))
    if reports:
        pytest.fail(
            f"{len(reports)} {variant} file(s) differ from their goldens. Rerun with "
            "--update-golden if the change is intended.\n\n" + "\n\n".join(reports)
        )


@pytest.mark.parametrize("variant", variants())
def test_every_golden_variant_declares_its_input(variant):
    """Each variant directory holds the build.toml it is rendered from.

    The input is the half of a golden that explains the output. A directory
    that lost it still compares green -- ``render_variant`` would fail
    first, but with a FileNotFoundError naming a path rather than the reason
    -- and nothing would say which build.toml produced the file being
    reviewed.
    """
    assert (GOLDEN_DIR / variant / INPUT_NAME).is_file()


def test_golden_variants_reach_every_gated_template_branch(tmp_path, monkeypatch):
    """No template conditional is gated on a flag every variant leaves off.

    A golden is only a safety net for the lines it renders. A branch no
    variant enters is not covered by 2298 lines of golden output any more
    than by none, and the gap is invisible -- the goldens stay green while
    the branch rots.

    This checks *reached*, not both ways: a flag true in some variant and
    false in another is better still, but "at least one" is what makes the
    output the review artifact for a change to that branch. The names come
    from the templates and the values from the contexts the templates were
    actually rendered with, so adding a ``<% if ci_something %>`` with no
    variant that sets it fails here rather than passing unnoticed.
    """
    contexts = []
    for variant in variants():
        contexts += capture_contexts(variant, tmp_path / variant, monkeypatch)
    assert contexts

    known = set().union(*(set(context) for context in contexts))
    gated = gated_context_names(known)
    # Without this the test passes by finding nothing to check: the names are
    # scraped with a regex over the templates' custom `<% %>` delimiters, and
    # a delimiter change would empty the set rather than fail anything. Two
    # flags the templates gate on prove the scrape still finds them.
    assert {"ci_coverage", "ci_windows"} <= gated, f"scraped only {sorted(gated)} from the templates"

    never_true = sorted(name for name in gated if not any(context.get(name) for context in contexts))

    assert never_true == [], (
        "no golden variant enables these, so the branches they gate are "
        f"never rendered: {never_true}"
    )


def test_golden_variants_cover_both_ci_types():
    """The variant set renders both hosts.

    A guard on the fixture set rather than on the generator: the goldens are
    only a safety net for the templates they reach, and both templates are
    edited by the same changes.
    """
    ci_types = {read_build_toml(GOLDEN_DIR / variant / INPUT_NAME).ci_type for variant in variants()}

    assert ci_types == {"github", "gitlab"}
