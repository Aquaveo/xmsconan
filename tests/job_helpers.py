"""Shared helpers for the ``xmsconan job`` command tests.

Kept out of ``tests/ci_helpers.py``, whose ``write_build_toml`` is a
keyword-driven builder for the CI generator's tables -- these write a raw
body, because a job test's subject is usually one exact ``[ci]`` line and
spelling it out is what makes the test readable.

Two functions rather than one with a flag selecting its return type: the
commands take ``--toml`` and want the path, the predicates take a config and
want it parsed, and a boolean in the call would be the harder thing to read.
Previously all four ``tests/test_job_*.py`` modules carried a copy, two of
each shape.
"""
from xmsconan.build_toml import read_build_toml

#: What a build.toml needs to parse at all. Every helper here starts from it.
MINIMAL_BODY = 'library_name = "xmscore"\n'


def write_build_toml(tmp_path, body=MINIMAL_BODY):
    """Write a build.toml under *tmp_path* and return its path as a string."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(body, encoding="utf-8")
    return str(toml_file)


def build_toml_config(tmp_path, body=MINIMAL_BODY):
    """A parsed build.toml with the given *body*."""
    return read_build_toml(write_build_toml(tmp_path, body))
