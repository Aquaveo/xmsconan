"""Tests for :mod:`xmsconan.job_tools.common`.

Two of these functions replace text the CI generator used to write into a
pipeline -- ``resolve_leg`` replaces the ``--filter '<json>'`` argument and the
``BUILD_MATRIX_FILTER`` variable, ``set_job_environment`` replaces a block of
``export`` lines -- so the assertions are pinned to what those rendered. A
change that made this module disagree with the golden pipelines would otherwise
show up as a matrix leg quietly not being built.
"""
import io
import json

import pytest

from xmsconan.build_toml import read_build_toml
from xmsconan.job_tools import common


def _config(tmp_path, body='library_name = "xmscore"\n'):
    """A parsed build.toml with the given body."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(body, encoding="utf-8")
    return read_build_toml(toml_file)


# --- resolve_leg ---


@pytest.mark.parametrize("leg, expected_options", [
    ("library", {"testing": False, "pybind": False}),
    ("testing", {"testing": True, "pybind": False}),
    ("pybind", {"pybind": True, "testing": False, "python_version": "3.13"}),
])
def test_resolve_leg_reproduces_the_filter_the_generator_renders(leg, expected_options):
    """The same selector the CI generator writes into each job's filter.

    Both spellings have to agree: ``ci_build_jobs`` serializes these selectors
    into the golden pipelines, and this function reproduces them at run time
    from ``--leg`` alone. If they drift, a job filters to a leg the pipeline
    was not planned around and builds nothing.
    """
    environ = {"BUILD_TYPE": "Release", "PYTHON_TARGET_VERSION": "3.13"}
    resolved = common.resolve_leg(leg=leg, environ=environ)
    assert resolved == {"build_type": "Release", "options": expected_options}


def test_every_leg_pins_both_flags():
    """A selector naming one flag would take the other leg's configurations.

    Testing and pybind are disjoint copies of the base combinations, so
    ``{"pybind": True}`` alone matches nothing extra but ``{"testing": False}``
    alone takes the pybind configurations along with the library ones.
    """
    for selector in common.LEG_SELECTORS.values():
        assert {"testing", "pybind"} <= set(selector)


def test_pybind_omits_python_version_when_the_environment_names_none():
    """A workstation builds every ABI its matrix produces.

    ``python_version: None`` would be an options key no configuration carries,
    which matches nothing -- an empty build rather than every ABI.
    """
    resolved = common.resolve_leg(leg="pybind", environ={})
    assert resolved == {"options": {"pybind": True, "testing": False}}


def test_no_leg_and_no_build_type_is_an_empty_filter():
    """Nothing to narrow means no filter, which is not the same as no build."""
    assert common.resolve_leg(environ={}) == {}


def test_release_skips_testing_only_on_a_release():
    """The Windows tag rule, and only under a tag.

    ``rules:`` swapped BUILD_MATRIX_FILTER to ``{"options":{"testing":false}}``
    on a tag pipeline and left it empty otherwise.
    """
    on_tag = common.resolve_leg(release=True, release_skips_testing=True, environ={})
    on_branch = common.resolve_leg(release=False, release_skips_testing=True, environ={})
    assert on_tag == {"options": {"testing": False}}
    assert on_branch == {}


def test_release_skips_testing_is_ignored_when_a_leg_is_named():
    """A named leg already says which configurations it wants.

    The rule exists for the job that loops a whole matrix. Applying it to a
    named leg would delete the testing job's entire build on a tag.
    """
    resolved = common.resolve_leg(leg="testing", release=True, release_skips_testing=True,
                                  environ={})
    assert resolved["options"]["testing"] is True


def test_resolve_leg_rejects_an_unknown_leg():
    """A misspelled --leg must not silently build the whole matrix."""
    with pytest.raises(ValueError, match="unknown leg 'pybnid'"):
        common.resolve_leg(leg="pybnid", environ={})


def test_leg_filters_serialize_to_the_json_the_pipelines_carry():
    """A round-trip through the generator's serialization, unchanged.

    ``ci_build_jobs`` dumps its selectors with ``separators=(",", ":")`` into
    the golden files; the point of asserting the parsed form is that the two
    describe the same matrix leg, not the same bytes.
    """
    resolved = common.resolve_leg(leg="testing", environ={"BUILD_TYPE": "Debug"})
    rendered = json.dumps(resolved, separators=(",", ":"))
    assert json.loads(rendered) == {
        "build_type": "Debug", "options": {"testing": True, "pybind": False},
    }


# --- set_job_environment ---


def test_set_job_environment_defaults_ctest_parallelism(tmp_path):
    """``${CTEST_PARALLEL_LEVEL:-8}``, as a function."""
    environ = {}
    common.set_job_environment(_config(tmp_path), environ=environ)
    assert environ[common.CTEST_PARALLEL_VARIABLE] == common.DEFAULT_CTEST_PARALLEL_LEVEL


def test_set_job_environment_never_overrides_a_chosen_value(tmp_path):
    """A runner or a developer that already chose keeps its value.

    That is what the ``:-`` in the template's export meant, and it is the only
    way to raise the level on a bigger runner without regenerating the CI.
    """
    environ = {common.CTEST_PARALLEL_VARIABLE: "32", common.UV_PYTHON_VARIABLE: "3.10",
               common.PYTHON_TARGET_VARIABLE: "3.13"}
    set_names = common.set_job_environment(_config(tmp_path), environ=environ)
    assert environ[common.CTEST_PARALLEL_VARIABLE] == "32"
    assert environ[common.UV_PYTHON_VARIABLE] == "3.10"
    assert set_names == []


def test_uv_python_follows_the_python_target(tmp_path):
    """Every ``uv build`` in the graph builds for the ABI this job targets.

    ``uv build`` given no interpreter discovers one itself and takes the newest
    it can see, which on GLR-UV is 3.14 regardless of which leg is running --
    so a 3.10 leg built a cp314 wheel and then refused to install it into its
    own venv. This is what makes the wheel carry the ABI the rest of the build
    was compiled against.

    uv reads UV_PYTHON itself, which is why it has to be in the environment:
    it must reach the recipe copies of dependencies Conan builds from source
    (the msvc 192 job passes ``--build-missing``, so there are some), and
    nothing on this process's command line does.
    """
    environ = {common.PYTHON_TARGET_VARIABLE: "3.14"}
    common.set_job_environment(_config(tmp_path), environ=environ)
    assert environ[common.UV_PYTHON_VARIABLE] == "3.14"


def test_uv_python_is_left_alone_when_no_abi_is_targeted(tmp_path):
    """A library leg targets no ABI, and must not pin uv to this interpreter."""
    environ = {}
    common.set_job_environment(_config(tmp_path), environ=environ)
    assert common.UV_PYTHON_VARIABLE not in environ


def test_split_tests_skips_the_run_only_on_the_testing_leg(tmp_path):
    """The compile-here-run-there variable, on the leg that compiles a runner.

    The other legs build no runner, so setting it there would be a no-op that
    still has to be explained the next time someone reads the environment.
    """
    config = _config(tmp_path, 'library_name = "xmscore"\n[ci]\nsplit_tests = true\n')
    testing_environ = {}
    library_environ = {}
    common.set_job_environment(config, leg="testing", environ=testing_environ)
    common.set_job_environment(config, leg="library", environ=library_environ)
    assert testing_environ[common.SKIP_CXX_TESTS_VARIABLE] == "1"
    assert common.SKIP_CXX_TESTS_VARIABLE not in library_environ


def test_split_tests_off_runs_the_tests_in_the_build(tmp_path):
    """Without split_tests there is no separate job to run them."""
    environ = {}
    common.set_job_environment(_config(tmp_path), leg="testing", environ=environ)
    assert common.SKIP_CXX_TESTS_VARIABLE not in environ


# --- log_section ---


def test_log_section_emits_gitlab_collapsible_markers():
    """A section_start/section_end pair with a matching key is what folds."""
    stream = io.StringIO()
    with common.log_section("Conan setup", environ={"GITLAB_CI": "true"}, stream=stream):
        print("building", file=stream)

    output = stream.getvalue()
    assert "section_start:" in output
    assert ":conan_setup\r" in output
    assert "section_end:" in output
    assert output.count(":conan_setup") == 2


def test_log_section_emits_github_groups():
    """Only ::group:: folds on GitHub, and the two markers never mix."""
    stream = io.StringIO()
    with common.log_section("Build (3 configurations)",
                            environ={"GITHUB_ACTIONS": "true"}, stream=stream):
        pass

    output = stream.getvalue()
    assert output.startswith("::group::Build (3 configurations)")
    assert output.endswith("::endgroup::\n")
    assert "section_start" not in output


def test_log_section_is_a_plain_banner_off_ci():
    """A workstation run still looks like the ``==>`` lines publish prints."""
    stream = io.StringIO()
    with common.log_section("Stage wheel", environ={}, stream=stream):
        pass
    assert stream.getvalue() == "==> Stage wheel\n"


def test_log_section_closes_the_fold_when_the_body_raises():
    """An unclosed section swallows the rest of the log into the failure.

    Which is precisely the part of a failing job a reader needs open.
    """
    stream = io.StringIO()
    with pytest.raises(RuntimeError):
        with common.log_section("Build", environ={"GITLAB_CI": "true"}, stream=stream):
            raise RuntimeError("boom")
    assert "section_end:" in stream.getvalue()


@pytest.mark.parametrize("title, expected", [
    ("Conan setup", "conan_setup"),
    ("Build (3 configurations)", "build_3_configurations"),
    ("flake8 _package", "flake8__package"),
    ("!!!", "section"),
])
def test_section_keys_are_restricted_to_what_gitlab_accepts(title, expected):
    """A section name carrying anything but word characters is dropped."""
    assert common._section_key(title) == expected


# --- tool versions ---


def test_tool_version_commands_report_the_compiler_only_on_linux():
    """Windows has no ``cl`` on PATH until a build shell sets one up.

    MSVC is selected by the Conan profile there, so the version that matters
    is the profile's, not whatever a bare shell would find.
    """
    assert common.tool_version_commands(platform="linux") == [
        ["gcc", "--version"], ["cmake", "--version"],
    ]
    assert common.tool_version_commands(platform="win32") == [["cmake", "--version"]]


def test_print_tool_versions_leads_with_the_resolved_xmsconan():
    """The generated job installs a range; this line says what it resolved to.

    It is the one fact about a job's toolchain that cannot be recovered from
    the pipeline definition.
    """
    stream = io.StringIO()
    common.print_tool_versions(runner=lambda command, check=False: None,
                               platform="win32", stream=stream)
    first_line = stream.getvalue().splitlines()[0]
    assert first_line.startswith("xmsconan ")


def test_print_tool_versions_survives_a_missing_tool():
    """A diagnostic banner must not be what fails the job.

    A cmake that is really absent fails the build a minute later with an error
    that names cmake; failing here would hide that behind a version check.
    """
    def _missing(command, check=False):
        raise OSError("No such file or directory: 'gcc'")

    stream = io.StringIO()
    common.print_tool_versions(runner=_missing, platform="linux", stream=stream)
    assert "gcc: not available" in stream.getvalue()
