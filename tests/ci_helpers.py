"""Shared helpers for the CI-generator tests.

Kept out of ``tests/utils.py``, which exists for environment patching -- these
are about generating and inspecting CI files, and both CI test modules need
them. Previously each module carried its own copy of the build.toml writer and
its own idea of which top-level keys are not jobs.
"""
from pathlib import Path
import re

import yaml

from xmsconan._tomllib import loads

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
                     coverage_table=None, matrix_table=None, **ci_flags):
    """Write a minimal build.toml with the given ci_type, [ci], [coverage] and [matrix] tables."""
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
    if matrix_table:
        lines += ["", "[matrix]"] + [
            f"{key} = {_toml_value(value)}" for key, value in matrix_table.items()
        ]
    toml_file = tmp_path / "build.toml"
    toml_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml_file


def write_gitlab_toml(tmp_path, **ci_flags):
    """Write a minimal GitLab build.toml."""
    return write_build_toml(tmp_path, "gitlab", **ci_flags)


#: The [matrix] table that opts a repository into the concurrent build stage.
#: Spelled out at each call site rather than defaulted, because whether a
#: pipeline has one build job per configuration or one job looping all of them
#: is exactly what these tests are about.
WHEEL_ONLY = {"wheel_only": True}


def write_github_toml(tmp_path, **ci_flags):
    """Write a minimal GitHub build.toml."""
    return write_build_toml(tmp_path, "github", library_name="xmscore", description="Core", **ci_flags)


def workflow_document(path):
    """The parsed document of an already rendered CI file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def steps_running(job, command):
    """The steps of one job whose ``run:`` line invokes *command*."""
    return [step for step in job["steps"] if command in step.get("run", "")]


def requirement_names(line):
    """The bare distribution names on one ``pip install`` line, case-folded.

    A substring match over the whole line is wrong in one direction: it also
    matches ``tomli`` and ``tomlkit``, which are different distributions, and
    would match a ``build.toml`` path if an install line ever carried one. A
    bare ``line.split()`` match is wrong in the other -- it misses every
    decorated form a re-added requirement would arrive in: ``toml==0.10``,
    ``"toml"``, ``toml>=0.10``, ``toml@https://...``. Names are folded because
    distribution names are matched case-insensitively.
    """
    names = set()
    for token in line.split():
        names.add(re.split(r"[=<>!~\[;@]", token.strip("\"'"), maxsplit=1)[0].casefold())
    return names


def ci_extra():
    """The ``[ci]`` extra as pyproject.toml declares it: one requirement string per entry.

    Read from the file rather than ``importlib.metadata``: an editable
    install's metadata is written at sync time, so an unsynced pyproject.toml
    edit would be tested against the previous extra.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    return loads(pyproject.read_text(encoding="utf-8"))["project"]["optional-dependencies"]["ci"]
