"""Shared pytest fixtures for xmsconan tests."""
import logging

import pytest


# tests/fixtures/coverage_stub/_package/tests/test_stub.py is part of the
# stub recipe that the coverage integration test stands up — it is meant
# to run inside the recipe's pytest invocation under conan create, not at
# the xmsconan top-level pytest run, where ``xms.stub`` is not importable.
collect_ignore = ["fixtures"]


@pytest.fixture(autouse=True)
def _reset_root_logging():
    """Undo what a ``main()`` under test did to the root logger.

    ``xmsconan._cli.configure_logging`` runs ``basicConfig(force=True)``,
    which replaces the root handlers with a ``StreamHandler`` bound to
    whatever ``sys.stderr`` was at the time -- under ``capsys``, a buffer
    that is closed when the test ends -- and sets the root level. Left in
    place, the next test's log records go to a closed stream and INFO
    records nobody asked for reach its ``caplog``.

    What it cannot undo: ``force=True`` also removes and closes pytest's
    session-scoped ``--log-file`` and ``log_cli`` handlers, so after the
    first ``main()`` under test those outputs are dead for the rest of the
    run, and within such a test ``caplog`` sees nothing logged after the
    ``main()`` returned. Neither option is set in ``pyproject.toml``; read a
    ``main()``'s output through ``capsys`` rather than ``caplog``.
    """
    root = logging.getLogger()
    level = root.level
    yield
    for handler in root.handlers[:]:
        # basicConfig installs a plain StreamHandler; pytest's are subclasses.
        if handler.__class__ is logging.StreamHandler:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def pytest_addoption(parser):
    """Register the golden-file update switch."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite tests/golden/ from the current templates instead of comparing against it.",
    )


@pytest.fixture
def update_golden(request):
    """Whether this run rewrites the golden files instead of asserting on them.

    A switch rather than an environment variable so that it shows up in
    ``pytest --help`` next to the suite it belongs to, and so that a CI job
    cannot inherit it from a stray export and silently accept a template
    change it was meant to catch.
    """
    return request.config.getoption("--update-golden")


@pytest.fixture
def build_toml(tmp_path):
    """Write a minimal build.toml with library_name and description, return path."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core library"\npython_namespaced_dir = "core"\n',
        encoding="utf-8",
    )
    return toml_file


@pytest.fixture
def template_dir(tmp_path):
    """Create a directory with a simple .jinja template, return path."""
    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "sample.txt.jinja").write_text(
        "version={{ version }}\n", encoding="utf-8"
    )
    return tpl_dir


@pytest.fixture
def ci_toml(tmp_path):
    """Write a build.toml with ci_type field, return path."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core library"\nci_type = "github"\n',
        encoding="utf-8",
    )
    return toml_file


@pytest.fixture
def profile_file(tmp_path):
    """Write a single Conan profile with [options], return path."""
    profile = tmp_path / "test_profile"
    profile.write_text(
        "[options]\ntesting=True\npybind=False\nwchar_t=builtin\n",
        encoding="utf-8",
    )
    return profile
