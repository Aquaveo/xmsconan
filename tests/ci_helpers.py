"""Shared helpers for the CI-generator tests.

Kept out of ``tests/utils.py``, which exists for environment patching -- these
are about generating and inspecting CI files, and both CI test modules need
them. Previously each module carried its own copy of the build.toml writer and
its own idea of which top-level keys are not jobs.
"""

#: Top-level keys in a generated ``.gitlab-ci.yml`` that are not jobs, for
#: checks that inspect job *shape*.
#:
#: ``pages`` is listed because it has no ``script:`` and would fail those
#: checks -- but it IS a job and does declare ``stage: Pages``, so stage
#: validation must NOT use this set or it silently stops guarding the one
#: thing it exists to guard.
NON_JOB_SHAPE_KEYS = frozenset({"stages", "variables", "pages"})


def _toml_value(value):
    """Render a Python value as TOML.

    ``str(value).lower()`` is wrong for anything but a bool -- it would emit a
    bare, unquoted string for ``docker_image``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return f'"{value}"'


def write_build_toml(tmp_path, ci_type, library_name="xmssnap", description="Snap",
                     coverage_table=None, **ci_flags):
    """Write a minimal build.toml with the given ci_type, [ci] and [coverage] tables."""
    lines = [
        f'library_name = "{library_name}"',
        f'description = "{description}"',
        f'ci_type = "{ci_type}"',
    ]
    if ci_flags:
        lines += ["", "[ci]"] + [f"{key} = {_toml_value(value)}" for key, value in ci_flags.items()]
    if coverage_table:
        lines += ["", "[coverage]"] + [
            f"{key} = {_toml_value(value)}" for key, value in coverage_table.items()
        ]
    toml_file = tmp_path / "build.toml"
    toml_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml_file


def write_gitlab_toml(tmp_path, **ci_flags):
    """Write a minimal GitLab build.toml."""
    return write_build_toml(tmp_path, "gitlab", **ci_flags)


def write_github_toml(tmp_path, **ci_flags):
    """Write a minimal GitHub build.toml."""
    return write_build_toml(tmp_path, "github", library_name="xmscore", description="Core", **ci_flags)
