"""Tests for :mod:`xmsconan.job_tools.build`, the CI build job as a call.

The sequence a generated ``script:`` used to spell out -- setup, generate,
build, stage, export -- is now an ordering inside one function, so these tests
assert the sequence rather than the individual steps: nothing else in the
repository records that the generate has to happen before the packager is
built, and it is the ordering constraint that a refactor is most likely to
lose.
"""
import contextlib
import os
import sys
from unittest.mock import patch

import pytest

from xmsconan.build_toml import read_build_toml
from xmsconan.constants import VS2019_PLATFORM_KEY, VS2019_REMOTE_NAME
from xmsconan.exit_codes import EXIT_ERROR, EXIT_OK
from xmsconan.job_tools import build, common
from xmsconan.job_tools.build import BuildSteps
from .job_helpers import write_build_toml as _toml


def _library_configuration(build_type="Release"):
    """A configuration with no pybind and no testing."""
    return {"build_type": build_type, "options": {"testing": False, "pybind": False}}


def _pybind_configuration(build_type="Release", python_version="3.13"):
    """A configuration that produces a wheel."""
    return {"build_type": build_type,
            "options": {"pybind": True, "testing": False, "python_version": python_version}}


class _FakePackager:
    """A packager that yields fixed configurations and records what ran.

    Stands in for :class:`~xmsconan.package_tools.packager.XmsConanPackager`,
    whose real ``run`` shells out to Conan. The filters are recorded rather
    than applied: which filters reach it, in which order, is the behavior
    ``job_build`` owns -- whether a given filter selects a given configuration
    is ``filter_configurations``' own tested behavior.
    """

    def __init__(self, configurations=None, run_result=0, extracted=True):
        self.configurations = ([_library_configuration()] if configurations is None
                               else list(configurations))
        self.filters = []
        self.run_result = run_result
        self.extracted = extracted
        self.system_platform = "unset"
        self.wheel_dirs = []
        self.dependency_lib_dirs = []
        self.events = []

    def generate_configurations(self, system_platform=None):
        self.system_platform = system_platform
        self.events.append("generate_configurations")

    def filter_configurations(self, selection):
        self.filters.append(selection)
        self.events.append("filter_configurations")

    def run(self):
        self.events.append("run")
        return self.run_result

    def extract_wheel(self, wheel_dir, version=None):
        self.wheel_dirs.append((wheel_dir, version))
        self.events.append("extract_wheel")
        return self.extracted

    def collect_dependency_libs(self, target):
        self.dependency_lib_dirs.append(target)
        self.events.append("collect_dependency_libs")


class _Recorder:
    """Fakes for every :class:`BuildSteps` field, recording the call order."""

    def __init__(self, packager=None, generate_result=0):
        self.calls = []
        self.packager = packager or _FakePackager()
        self.generate_result = generate_result
        self.conan_setup_kwargs = []
        self.deploy_kwargs = []
        self.repair_kwargs = []
        self.display_config = None

    def steps(self):
        return BuildSteps(
            print_versions=self._print_versions,
            conan_setup=self._conan_setup,
            generate=self._generate,
            make_packager=self._make_packager,
            display=self._display,
            wheel_repair=self._wheel_repair,
            conan_deploy=self._conan_deploy,
        )

    def _print_versions(self):
        self.calls.append("print_versions")

    def _conan_setup(self, **kwargs):
        self.calls.append("conan_setup")
        self.conan_setup_kwargs.append(kwargs)

    def _generate(self, toml_file_path=None, version=None):
        self.calls.append("generate")
        self.generated = (toml_file_path, version)
        return self.generate_result

    def _make_packager(self, config, toml_path, build_missing, platform_key):
        self.calls.append("make_packager")
        self.make_packager_args = (config, toml_path, build_missing, platform_key)
        return self.packager

    @contextlib.contextmanager
    def _display(self, config, log_dir=None, environ=None):
        self.calls.append("display-enter")
        self.display_config = config
        try:
            yield None
        finally:
            self.calls.append("display-exit")

    def _wheel_repair(self, **kwargs):
        self.calls.append("wheel_repair")
        self.repair_kwargs.append(kwargs)

    def _conan_deploy(self, library, version, **kwargs):
        self.calls.append("conan_deploy")
        self.deploy_kwargs.append((library, version, kwargs))


# --- the sequence ---


def test_job_build_runs_the_steps_in_the_order_the_template_spelled_out(tmp_path):
    """Versions, setup, generate, packager, build -- in that order.

    The generate must precede the packager: it writes the ``conanfile.py``
    the packager is constructed against, and a packager built first would be
    reading the previous run's file, or none at all in a fresh clone.
    """
    recorder = _Recorder()
    result = job_build_in(tmp_path, recorder, version="1.2.3")

    assert result == EXIT_OK
    assert recorder.calls[:5] == [
        "print_versions", "conan_setup", "generate", "make_packager", "display-enter",
    ]
    assert recorder.packager.events.index("generate_configurations") \
        < recorder.packager.events.index("run")


def job_build_in(tmp_path, recorder, body='library_name = "xmscore"\n', environ=None, **kwargs):
    """Run :func:`job_build` against a build.toml written under *tmp_path*."""
    environ = {} if environ is None else environ
    return build.job_build(toml_path=_toml(tmp_path, body), steps=recorder.steps(),
                           environ=environ, **kwargs)


def test_conan_setup_does_not_log_in(tmp_path):
    """``conan remote login`` with no credentials prompts, and a runner hangs.

    Conan reads CONAN_LOGIN_USERNAME and CONAN_PASSWORD from the environment
    itself, which is where a CI secret belongs. No generated job has ever run
    a login, and this is the call that replaces them.
    """
    recorder = _Recorder()
    job_build_in(tmp_path, recorder, version="1.2.3")
    assert recorder.conan_setup_kwargs == [{"login": False}]


@pytest.mark.parametrize("defer, expected", [(True, "1"), (False, None)])
def test_the_defer_flag_reaches_the_environment_the_build_runs_under(
        tmp_path, defer, expected):
    """The flag is only worth parsing if it lands in the recipe's environment.

    ``tests/test_job_cli.py`` asserts the flag reaches this function and
    ``tests/test_job_common.py`` asserts what ``set_job_environment`` does
    with it; neither notices if the hop between them is dropped. Hardcoding
    ``defer_cxx_tests=False`` at the call site passes both of those, and the
    only symptom in CI is a C++ suite that runs twice.
    """
    environ = {}
    recorder = _Recorder()
    job_build_in(
        tmp_path, recorder, version="1.2.3", environ=environ,
        defer_cxx_tests=defer,
        body='library_name = "xmscore"\n[ci]\nsplit_tests = true\n',
    )

    assert environ.get(common.SKIP_CXX_TESTS_VARIABLE) == expected


def test_a_failed_generate_stops_before_the_build(tmp_path):
    """Nothing downstream is meaningful once the build files are wrong."""
    recorder = _Recorder(generate_result=EXIT_ERROR)
    result = job_build_in(tmp_path, recorder, version="1.2.3")

    assert result == EXIT_ERROR
    assert "make_packager" not in recorder.calls


def test_a_failed_build_stops_before_the_wheel_and_the_export(tmp_path):
    """A build that returned errors has no artifacts worth staging."""
    recorder = _Recorder(packager=_FakePackager(
        configurations=[_pybind_configuration()], run_result=2))
    result = job_build_in(tmp_path, recorder, version="1.2.3", export=True)

    assert result == EXIT_ERROR
    assert "extract_wheel" not in recorder.packager.events
    assert "conan_deploy" not in recorder.calls
    assert recorder.calls[-1] == "display-exit"


# --- filters ---


def test_both_filters_reach_the_packager_in_order(tmp_path):
    """The repository's [filter] first, then this job's leg.

    Order is not cosmetic: each call narrows what the previous one left, and
    the [filter] is the repository saying which configurations exist at all.
    """
    recorder = _Recorder(packager=_FakePackager(configurations=[_library_configuration()]))
    job_build_in(
        tmp_path, recorder,
        body='library_name = "xmscore"\n[filter]\nbuild_type = "Release"\n',
        environ={"BUILD_TYPE": "Release"}, leg="library", version="1.2.3",
    )

    assert recorder.packager.filters == [
        {"build_type": "Release"},
        {"build_type": "Release", "options": {"testing": False, "pybind": False}},
    ]


def test_an_empty_filter_is_not_applied(tmp_path):
    """No leg and no BUILD_TYPE means nothing narrows the matrix.

    ``filter_configurations({})`` iterates no keys and matches everything, so
    calling it would be harmless -- but the printed "Applying leg filter" line
    would claim a narrowing that never happened.
    """
    recorder = _Recorder()
    job_build_in(tmp_path, recorder, version="1.2.3")
    assert recorder.packager.filters == []


def test_filters_that_leave_nothing_fail_the_job(tmp_path, capsys):
    """Building nothing and exiting 0 reads as a passing build.

    That is what a leg whose configurations the [filter] had already removed
    used to do: green, with no packages produced and no line saying so.
    """
    recorder = _Recorder(packager=_FakePackager(configurations=[]))
    result = job_build_in(tmp_path, recorder, leg="pybind", version="1.2.3")

    assert result == EXIT_ERROR
    assert "No configurations match this job" in capsys.readouterr().out
    assert "run" not in recorder.packager.events


# --- the wheel ---


def test_a_pybind_leg_stages_its_wheel(tmp_path):
    """A wheel exists exactly when a pybind configuration was built."""
    recorder = _Recorder(packager=_FakePackager(configurations=[_pybind_configuration()]))
    job_build_in(tmp_path, recorder, leg="pybind", version="1.2.3")

    assert recorder.packager.wheel_dirs == [("wheelhouse", "1.2.3")]
    assert recorder.packager.dependency_lib_dirs == [os.path.join("wheelhouse", "libs")]


def test_a_library_leg_stages_no_wheel(tmp_path):
    """Read off the configurations built, not off a flag the template set."""
    recorder = _Recorder(packager=_FakePackager(configurations=[_library_configuration()]))
    job_build_in(tmp_path, recorder, leg="library", version="1.2.3")
    assert recorder.packager.wheel_dirs == []


def test_an_incomplete_wheel_extraction_fails_the_job(tmp_path, capsys):
    """A partial extraction has already copied the wheels it did find.

    For a job whose wheel is the artifact it exists to produce, that is a
    failure, not the warning ``extract_wheel`` returns it as.
    """
    recorder = _Recorder(packager=_FakePackager(
        configurations=[_pybind_configuration()], extracted=False))
    result = job_build_in(tmp_path, recorder, leg="pybind", version="1.2.3")

    assert result == EXIT_ERROR
    assert "no complete set of wheels" in capsys.readouterr().out


def test_dependency_libs_are_skipped_when_repair_is_off(tmp_path):
    """[ci].windows_wheel_repair is a Windows rule and must not reach Linux.

    ``repairs_wheel`` answers True unconditionally off win32
    (``xmsconan/ci_options.py``), so a Linux job that honoured the flag would
    stop staging the libraries ``job package`` needs in the manylinux image --
    and the wheel it publishes would carry unresolved imports. Asserted
    unconditionally under a patched platform rather than behind an ``if``: the
    guard this replaces was false on every non-Windows machine, so the test
    passed there having checked nothing.
    """
    recorder = _Recorder(packager=_FakePackager(configurations=[_pybind_configuration()]))
    with patch.object(build.sys, "platform", "linux"):
        job_build_in(
            tmp_path, recorder, leg="pybind", version="1.2.3",
            body='library_name = "xmscore"\n[ci]\nwindows_wheel_repair = false\n',
        )

    assert recorder.packager.dependency_lib_dirs == [os.path.join("wheelhouse", "libs")]
    assert recorder.repair_kwargs == []


# --- VS2019 ---


def test_vs2019_appends_its_remote_and_builds_missing_dependencies(tmp_path):
    """The legacy stack is not prebuilt, and lives on its own remote.

    Appended rather than inserted first: it must not become the first stop for
    every ``conan install`` on a shared runner.
    """
    recorder = _Recorder()
    job_build_in(tmp_path, recorder, platform=VS2019_PLATFORM_KEY, version="1.2.3")

    assert recorder.conan_setup_kwargs[0] == {"login": False}
    assert recorder.conan_setup_kwargs[1]["remote_name"] == VS2019_REMOTE_NAME
    assert recorder.conan_setup_kwargs[1]["index"] is None
    _, _, build_missing, platform_key = recorder.make_packager_args
    assert build_missing is True
    assert platform_key == VS2019_PLATFORM_KEY


def test_vs2019_stages_no_wheel(tmp_path):
    """A wheel's tags say nothing about which MSVC built it.

    An msvc 192 wheel and an msvc 194 wheel are the same filename on the
    index, so publishing both would come down to upload order.
    """
    recorder = _Recorder(packager=_FakePackager(configurations=[_pybind_configuration()]))
    job_build_in(tmp_path, recorder, platform=VS2019_PLATFORM_KEY, version="1.2.3")
    assert recorder.packager.wheel_dirs == []


# --- export ---


def test_no_export_unless_asked(tmp_path):
    """A branch pipeline's tarball would never be restored."""
    recorder = _Recorder()
    job_build_in(tmp_path, recorder, version="1.2.3")
    assert recorder.deploy_kwargs == []


def test_export_saves_a_tarball_under_the_fixed_export_dir(tmp_path):
    """The deploy job restores by name from one artifact space."""
    recorder = _Recorder(packager=_FakePackager(configurations=[_library_configuration()]))
    job_build_in(tmp_path, recorder, leg="library", version="1.2.3", export=True)

    library, version, kwargs = recorder.deploy_kwargs[0]
    assert (library, version) == ("xmscore", "1.2.3")
    # The segment is a literal per platform, not build._platform_segment() --
    # computing the expectation with the function under test would agree with
    # any renaming it grew.
    segment = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    assert kwargs["save"] == os.path.join(
        ".export", f"xmscore-{segment}-Release-1.2.3.tar.gz")


# --- export naming and queries, on their own ---


def test_a_named_leg_is_discriminated_by_its_configuration_label():
    """Two legs can share a build type, and a library leg has no ABI.

    A Release library build and a Release testing build would collide on
    build type alone, and ``py3.13`` does not distinguish a configuration
    that has no python_version at all.
    """
    name = build.export_tarball_name(
        "xmscore", "1.2.3", [_library_configuration()], leg="library",
        platform="linux", environ={"PYTHON_TARGET_VERSION": "3.13"},
    )
    assert name == "xmscore-linux-Release-1.2.3.tar.gz"


def test_a_whole_matrix_job_is_discriminated_by_its_abi():
    """Those jobs are a parallel:matrix fan-out over ABIs."""
    name = build.export_tarball_name(
        "xmscore", "1.2.3", [_pybind_configuration(), _library_configuration()],
        platform="win32", environ={"PYTHON_TARGET_VERSION": "3.13"},
    )
    assert name == "xmscore-windows-py3.13-1.2.3.tar.gz"


def test_the_name_is_keyed_on_the_leg_not_on_how_many_survived():
    """A [filter] change must not rename a fan-out job's tarball.

    Keying on ``len(configurations) == 1`` alone would do exactly that, and
    the deploy job restores by name.
    """
    name = build.export_tarball_name(
        "xmscore", "1.2.3", [_pybind_configuration()],
        platform="win32", environ={"PYTHON_TARGET_VERSION": "3.13"},
    )
    assert name == "xmscore-windows-py3.13-1.2.3.tar.gz"


def test_vs2019_tarballs_are_named_apart_from_the_current_msvc():
    """Both are win32; only the platform key separates them."""
    name = build.export_tarball_name(
        "xmscore", "1.2.3", [_library_configuration()], leg="library",
        platform_key=VS2019_PLATFORM_KEY, platform="win32", environ={},
    )
    assert name == "xmscore-windows-vs2019-Release-1.2.3.tar.gz"


def test_windows_restricts_its_save_to_the_toolchain_it_built():
    """A Windows runner's Conan cache is per machine, not per job.

    ``conan cache save <ref>:*`` matches by reference, so an unqueried save on
    a fleet running msvc 192 and msvc 194 jobs together tarballs whichever
    binaries happen to be in the cache.
    """
    configurations = [{"build_type": "Release", "compiler.version": "194"}]
    assert build.export_package_query(configurations, platform="win32") \
        == "compiler.version=194"


def test_platforms_that_own_their_cache_need_no_query():
    """Each Linux and macOS job owns its container, and so its cache."""
    configurations = [{"build_type": "Release", "compiler.version": "194"}]
    assert build.export_package_query(configurations, platform="linux") is None


def test_disagreeing_configurations_refuse_to_save_rather_than_save_unqueried():
    """A save this cannot restrict is the failure the query exists to prevent.

    Warning and returning None left the caller saving every binary in a
    runner's shared cache -- which on a fleet running both toolchains at once
    is how msvc 192 packages reach the remote that exists to keep them apart.
    The generator raises on the same condition one layer up
    (``ci_file_generator._only_msvc_version``), so this agrees with it.
    """
    configurations = [
        {"build_type": "Release", "compiler.version": "194"},
        {"build_type": "Release", "compiler.version": "192"},
    ]
    with pytest.raises(ValueError) as excinfo:
        build.export_package_query(configurations, platform="win32")

    assert "192" in str(excinfo.value) and "194" in str(excinfo.value)


def test_configurations_naming_no_compiler_version_also_refuse():
    """The empty case reaches the same place: nothing to restrict the save to."""
    with pytest.raises(ValueError):
        build.export_package_query([{"build_type": "Release"}], platform="win32")


@pytest.mark.parametrize("platform, expected", [
    ("linux", "linux"),
    ("linux2", "linux"),
    ("win32", "windows"),
    ("darwin", "macos"),
])
def test_platform_segments_are_the_names_the_template_rendered(platform, expected):
    """A pipeline's tarballs keep the names its deploy job restores."""
    assert build._platform_segment(platform=platform) == expected


# --- the display ---


def test_the_build_runs_inside_a_display_when_the_repository_asks(tmp_path):
    """``conan create`` and the ctest run inside it land on the same server.

    Children inherit DISPLAY, which is why the display wraps the build rather
    than prefixing a command: there is no command here to prefix.
    """
    recorder = _Recorder()
    config_path = _toml(tmp_path, 'library_name = "xmscore"\n[ci]\nxvfb = true\n')
    build.job_build(toml_path=config_path, steps=recorder.steps(), environ={},
                    version="1.2.3")

    assert recorder.calls.index("display-enter") < recorder.calls.index("display-exit")
    assert recorder.display_config.ci.xvfb is True
    assert "run" in recorder.packager.events


def test_the_display_wraps_only_the_build(tmp_path):
    """Setup and generate need no display, and the export happens after it."""
    recorder = _Recorder(packager=_FakePackager(configurations=[_library_configuration()]))
    job_build_in(tmp_path, recorder, leg="library", version="1.2.3", export=True)

    assert recorder.calls.index("generate") < recorder.calls.index("display-enter")
    assert recorder.calls.index("display-exit") < recorder.calls.index("conan_deploy")


# --- the packager the job builds ---


def test_the_packager_is_constructed_from_build_toml(tmp_path):
    """No import of the generated build.py, and no second source of truth.

    ``LIBRARY_NAME``, ``CONAN_PROFILE_OPTIONS`` and ``CONAN_MATRIX`` in the
    generated conanfile are rendered from these same fields, so reading them
    here reaches the same values without depending on a generated file being
    current -- the one thing the regeneration exists to stop mattering.

    The constructor is spied rather than exercised: which arguments
    ``build.toml`` and the platform key resolve to is what this function
    owns, and what the packager then does with them is its own tested
    behavior.
    """
    toml_path = _toml(tmp_path, 'library_name = "xmscore"\n'
                                '[matrix]\nwheel_only = true\n')
    config = read_build_toml(toml_path)
    recorded = {}

    def _spy(library_name, conanfile_path, **kwargs):
        recorded.update(library_name=library_name, conanfile_path=conanfile_path, **kwargs)

    with patch.object(build.packager, "XmsConanPackager", _spy):
        build._make_packager(config, toml_path, build_missing=False, platform_key=None)

    assert recorded["library_name"] == "xmscore"
    assert recorded["conanfile_path"] == str(tmp_path.resolve() / "conanfile.py")
    assert recorded["matrix"] == config.matrix
    assert recorded["profile_options"] == config.conan_profile_options
    assert recorded["artifacts_dir"] == "test_artifacts"


def test_vs2019_does_not_inject_conan_center_boost_options(tmp_path):
    """boost/1.74.0.3 does not declare the options boost 1.86 does.

    Conan fails a build outright when a profile sets an option no recipe in
    the graph defines, so the defaults have to be off for the legacy matrix.
    Derived from the platform key rather than exposed as a second flag: one
    input, and no way to set the matrix and the toolchain assumptions to
    disagree.
    """
    toml_path = _toml(tmp_path)
    config = read_build_toml(toml_path)
    recorded = []

    def _spy(library_name, conanfile_path, **kwargs):
        recorded.append(kwargs["apply_boost_defaults"])

    with patch.object(build.packager, "XmsConanPackager", _spy):
        build._make_packager(config, toml_path, False, None)
        build._make_packager(config, toml_path, True, VS2019_PLATFORM_KEY)

    assert recorded == [True, False]


def test_the_real_packager_accepts_what_build_toml_resolves_to(tmp_path):
    """The spy above proves the arguments; this proves they are the right ones.

    A renamed or dropped keyword would pass every assertion made against a
    fake and fail in CI at the first build.
    """
    toml_path = _toml(tmp_path, 'library_name = "xmscore"\n[matrix]\nwheel_only = true\n')
    config = read_build_toml(toml_path)

    packager = build._make_packager(config, toml_path, build_missing=False, platform_key=None)
    packager.generate_configurations(system_platform="linux")

    # wheel_only is Release-testing, Debug-testing and the pybind leg.
    assert len(packager.configurations) == 3
    assert any(configuration["options"].get("pybind") for configuration in
               packager.configurations)


# --- the Windows-only repair ---


def test_windows_repairs_its_wheel_in_the_build_job(tmp_path):
    """Only a Windows host can run delvewheel.

    A manylinux container cannot stand in for it, and the alternative to
    repairing here is a second WinVM allocation. The Linux wheel is repaired
    by ``job package`` instead, in the image whose glibc auditwheel needs.
    """
    recorder = _Recorder(packager=_FakePackager(configurations=[_pybind_configuration()]))
    with patch.object(build.sys, "platform", "win32"):
        job_build_in(tmp_path, recorder, leg="pybind", version="1.2.3")

    assert recorder.repair_kwargs == [{"wheel_dir": "wheelhouse", "platform": "windows"}]


def test_windows_wheel_repair_can_be_turned_off(tmp_path):
    """A repository that opted out gets neither the repair nor its inputs."""
    recorder = _Recorder(packager=_FakePackager(configurations=[_pybind_configuration()]))
    with patch.object(build.sys, "platform", "win32"):
        job_build_in(
            tmp_path, recorder, leg="pybind", version="1.2.3",
            body='library_name = "xmscore"\n[ci]\nwindows_wheel_repair = false\n',
        )

    assert recorder.repair_kwargs == []
    assert recorder.packager.dependency_lib_dirs == []
