"""Tests for :mod:`xmsconan.job_tools.deploy`, the publish jobs as a call.

A deploy is a sequence of calls whose *arguments* decide which remote receives
which binaries, so the fakes here record arguments rather than returning
values. Two of those arguments used to be written twice -- the tarball path,
composed by the build and again by the template, and the remote/query pair,
written as literals per platform -- and both failure modes were silent: a
restore of a path that no longer exists uploads nothing and exits 0, and so
does an upload queried on a compiler version the matrix has moved past.
"""
import inspect
import os

import pytest

from xmsconan.constants import (DEFAULT_REMOTE_NAME, VS2019_PLATFORM_KEY, VS2019_REMOTE_NAME,
                                VS2019_REMOTE_URL)
from xmsconan.exit_codes import EXIT_OK
from xmsconan.generator_tools.version import FALLBACK_VERSION, GITLAB_TAG_VARIABLE
from xmsconan.job_tools import common, deploy
from xmsconan.package_tools.packager import configurations
from .job_helpers import write_build_toml as _toml


class _Recorder:
    """Fakes for every :class:`~xmsconan.job_tools.deploy.DeploySteps` field."""

    def __init__(self):
        self.calls = []
        self.setup_kwargs = []
        self.deploy_kwargs = []
        self.wheel_kwargs = []

    def steps(self):
        """A DeploySteps wired to this recorder."""
        return deploy.DeploySteps(
            conan_setup=self._conan_setup,
            conan_deploy=self._conan_deploy,
            wheel_deploy=self._wheel_deploy,
        )

    def _conan_setup(self, **kwargs):
        self.calls.append("conan_setup")
        self.setup_kwargs.append(kwargs)

    def _conan_deploy(self, library, version, **kwargs):
        self.calls.append("conan_deploy")
        self.deploy_kwargs.append((library, version, kwargs))

    def _wheel_deploy(self, **kwargs):
        self.calls.append("wheel_deploy")
        self.wheel_kwargs.append(kwargs)


def _exported(tmp_path, *names):
    """Write *names* as tarballs under the export directory and return it."""
    export_dir = tmp_path / common.EXPORT_DIR
    export_dir.mkdir(exist_ok=True)
    for name in names:
        (export_dir / name).write_text("tarball", encoding="utf-8")
    return export_dir


def job_deploy_in(tmp_path, recorder, monkeypatch, **kwargs):
    """Run :func:`job_deploy` with *tmp_path* as the working directory.

    The export glob is relative, like every other path in the fixed output
    layout, so the working directory is part of what is under test rather
    than something to thread past it as a parameter.
    """
    monkeypatch.chdir(tmp_path)
    kwargs.setdefault("version", "1.2.3")
    return deploy.job_deploy(toml_path=_toml(tmp_path), steps=recorder.steps(),
                             environ={}, **kwargs)


# --- which remote, and what restricts the upload to it ---


@pytest.mark.parametrize("platform_key, platform, expected", [
    (None, "linux", (DEFAULT_REMOTE_NAME, None)),
    (None, "darwin", (DEFAULT_REMOTE_NAME, None)),
    (None, "win32", (DEFAULT_REMOTE_NAME, "compiler.version=194")),
    (VS2019_PLATFORM_KEY, "win32", (VS2019_REMOTE_NAME, "compiler.version=192")),
])
def test_each_platform_publishes_through_its_own_remote_and_query(
        platform_key, platform, expected):
    """The three pairings, including the two that carry a query.

    Linux and macOS each own their container and so their Conan cache; there
    is nothing else in it to exclude. A Windows runner's cache is per machine,
    so an unqueried ``conan upload <ref>:*`` from a job whose neighbour built
    the other toolchain publishes the neighbour's binaries under this job's
    name -- to the production remote in one direction and to the legacy one in
    the other.

    ``windows_vs2019`` is asserted as a *platform key*, because that is the
    only thing that distinguishes it: both Windows jobs run on the same fleet
    and report the same ``sys.platform``.
    """
    assert deploy.upload_target(platform_key=platform_key, platform=platform) == expected


@pytest.mark.parametrize("platform_key", ["windows", VS2019_PLATFORM_KEY])
def test_the_query_tracks_the_matrix_rather_than_a_literal(platform_key):
    """The compiler version comes from the matrix row the platform names.

    A literal that fell behind a toolchain bump would not fail loudly:
    ``conan upload -p compiler.version=194`` after a move to 195 matches
    nothing, and the job goes green having published no binaries at all.
    """
    expected, = configurations[platform_key]["compiler.version"]
    _, query = deploy.upload_target(platform_key=platform_key, platform="win32")
    assert query == f"compiler.version={expected}"


def test_an_unrecognised_platform_key_falls_back_to_the_running_platform():
    """Only the VS2019 key changes the answer; anything else asks the machine.

    ``job build`` takes ``--platform linux`` and ``--platform mac`` too, and a
    deploy that inherited one of those must not silently stop querying on a
    Windows runner.
    """
    assert deploy.upload_target(platform_key="linux", platform="win32") == (
        DEFAULT_REMOTE_NAME, "compiler.version=194")


# --- what a deploy refuses to publish ---


@pytest.mark.parametrize("version, environ", [
    (None, {"GITLAB_CI": "true"}),
    (None, {"GITHUB_ACTIONS": "true"}),
    (FALLBACK_VERSION, {}),
    ("7.0.*", {}),
])
def test_a_version_no_release_names_is_refused_before_anything_publishes(
        tmp_path, monkeypatch, version, environ):
    """Both halves publish outward, so the version is asked about first.

    An untagged pipeline resolves the fallback, and uploading it puts
    ``<lib>/0.0.0`` on the remote every consumer resolves against with
    nothing to notice; a glob would publish every version in the cache.
    ``xmsconan conan-deploy`` refused both at its parser before ``job
    deploy`` replaced that entry point, and ``xmsconan publish`` still does.

    No generated job can reach this -- every deploy carries ``only: tags``
    -- so what it guards is the hand-run replay USAGE 10.5 invites, and the
    reason it is asserted here is that the deploy is the one job kind whose
    mistake cannot be taken back.
    """
    recorder = _Recorder()
    monkeypatch.chdir(tmp_path)
    _exported(tmp_path, "xmscore-linux-Release-0.0.0.tar.gz")

    with pytest.raises(ValueError, match="release version"):
        deploy.job_deploy(toml_path=_toml(tmp_path), steps=recorder.steps(),
                          environ=environ, version=version)

    assert recorder.calls == []


def test_the_tag_a_deploy_job_runs_under_is_accepted(tmp_path, monkeypatch):
    """The other direction, which is the half a refusal-of-everything passes.

    The guard is on the path every release takes, so a test that only
    asserts what it rejects would let a version predicate that rejects
    everything through.
    """
    recorder = _Recorder()
    monkeypatch.chdir(tmp_path)
    _exported(tmp_path, "xmscore-linux-Release-7.0.1.tar.gz")

    assert deploy.job_deploy(
        toml_path=_toml(tmp_path), steps=recorder.steps(), wheels=False,
        environ={GITLAB_TAG_VARIABLE: "7.0.1"}, version=None) == EXIT_OK

    assert {version for _, version, _ in recorder.deploy_kwargs} == {"7.0.1"}


# --- what gets restored ---


def test_the_tarballs_are_globbed_rather_than_named(tmp_path, monkeypatch):
    """Whatever the build wrote is what the deploy restores.

    The template used to reassemble each path from the library, platform, leg
    and version the build had already used to write it, so a rename in
    :func:`~xmsconan.job_tools.build.export_tarball_name` left the deploy
    restoring a file that is not there -- and a restore of nothing uploads
    nothing and exits 0.
    """
    monkeypatch.chdir(tmp_path)
    _exported(tmp_path, "xmscore-linux-Release-1.2.3.tar.gz",
              "xmscore-linux-Debug-1.2.3.tar.gz")

    assert deploy.exported_tarballs() == [
        os.path.join(common.EXPORT_DIR, "xmscore-linux-Debug-1.2.3.tar.gz"),
        os.path.join(common.EXPORT_DIR, "xmscore-linux-Release-1.2.3.tar.gz"),
    ]


def test_a_missing_export_directory_is_a_failed_artifact_handoff(tmp_path, monkeypatch):
    """No ``.export/`` at all: the build jobs' artifacts never arrived."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="directory"):
        deploy.exported_tarballs()


def test_an_empty_export_directory_raises_rather_than_publishing_nothing(
        tmp_path, monkeypatch):
    """An empty directory is a different fault from a missing one, and says so.

    Returning ``[]`` would make this a successful no-op, which is how a deploy
    that published nothing survives a release. Distinguished from the missing
    case because the two send an operator to different places: artifacts that
    did not arrive, versus builds that did not run with ``--export``.
    """
    monkeypatch.chdir(tmp_path)
    _exported(tmp_path)

    with pytest.raises(ValueError, match="--export"):
        deploy.exported_tarballs()


# --- the conan half ---


def test_every_tarball_is_restored_before_the_single_upload(tmp_path, monkeypatch):
    """Restore the whole set, then upload once.

    ``conan upload <ref>:*`` publishes every package id under the reference,
    so an upload per tarball would push the same recipe once per exporting
    job.
    """
    recorder = _Recorder()
    _exported(tmp_path, "xmscore-linux-Debug-1.2.3.tar.gz",
              "xmscore-linux-Release-1.2.3.tar.gz")

    assert job_deploy_in(tmp_path, recorder, monkeypatch, wheels=False) == EXIT_OK

    restores = [kwargs.get("restore") for _, _, kwargs in recorder.deploy_kwargs]
    uploads = [kwargs for _, _, kwargs in recorder.deploy_kwargs if kwargs.get("upload")]
    # The paths, not a projection of them onto "restored something": each
    # tarball has to be restored, and one restored twice while the other is
    # skipped publishes half the matrix and looks identical from a count.
    assert restores == [
        os.path.join(common.EXPORT_DIR, "xmscore-linux-Debug-1.2.3.tar.gz"),
        os.path.join(common.EXPORT_DIR, "xmscore-linux-Release-1.2.3.tar.gz"),
        None,
    ]
    assert len(uploads) == 1
    assert uploads[0]["remote"] == DEFAULT_REMOTE_NAME
    assert uploads[0]["package_query"] is None


def test_the_library_and_version_reach_every_step(tmp_path, monkeypatch):
    """One resolution, spent on the restores and the upload alike.

    Every deploy block used to open with ``export
    PACKAGE_VERSION=${CI_COMMIT_TAG:-0.0.0}`` and spend it twice, so a save in
    one job and a restore in another agreed only while two rendered lines
    stayed in step.
    """
    recorder = _Recorder()
    _exported(tmp_path, "xmscore-linux-Release-4.5.6.tar.gz")

    job_deploy_in(tmp_path, recorder, monkeypatch, version="4.5.6", wheels=False)

    assert {(library, version) for library, version, _ in recorder.deploy_kwargs} == {
        ("xmscore", "4.5.6")}


def test_the_conan_setup_does_not_log_in(tmp_path, monkeypatch):
    """``conan remote login`` with no credentials prompts, and a runner hangs.

    Conan reads CONAN_LOGIN_USERNAME and CONAN_PASSWORD from the environment
    itself, which is where a CI secret belongs. The same call ``job build``
    makes, for the same reason.
    """
    recorder = _Recorder()
    _exported(tmp_path, "xmscore-linux-Release-1.2.3.tar.gz")

    job_deploy_in(tmp_path, recorder, monkeypatch, wheels=False)

    assert recorder.setup_kwargs == [{"login": False}]


def test_the_vs2019_platform_appends_its_remote_rather_than_inserting_it(
        tmp_path, monkeypatch):
    """The legacy remote is added after the CI one, never before it.

    Two things at once. The CI remote is still configured -- the recipe's own
    dependencies resolve from there even on the legacy toolchain -- and the
    legacy remote goes on the end, because on a shared runner it must not
    become the first stop for every ``conan install`` on the machine,
    including unrelated ones.
    """
    recorder = _Recorder()
    _exported(tmp_path, "xmscore-windows-vs2019-py3.13-1.2.3.tar.gz")

    job_deploy_in(tmp_path, recorder, monkeypatch,
                  platform=VS2019_PLATFORM_KEY, wheels=False)

    assert recorder.setup_kwargs == [
        {"login": False},
        {"remote_name": VS2019_REMOTE_NAME, "remote_url": VS2019_REMOTE_URL,
         "index": None, "login": False},
    ]
    upload, = [kwargs for _, _, kwargs in recorder.deploy_kwargs if kwargs.get("upload")]
    assert upload["remote"] == VS2019_REMOTE_NAME
    assert upload["package_query"] == "compiler.version=192"


def test_the_cache_archive_is_queried_like_the_upload(tmp_path, monkeypatch):
    """A save reads the same shared cache the upload publishes from.

    An unqueried ``conan cache save`` would tarball whichever toolchain's
    binaries a neighbouring job left in the runner's cache, which is the
    failure the retired ``cp -r ${HOME}/.conan2/p/*`` snapshot had and could
    not report.
    """
    recorder = _Recorder()
    _exported(tmp_path, "xmscore-windows-py3.13-1.2.3.tar.gz")

    job_deploy_in(tmp_path, recorder, monkeypatch, platform=VS2019_PLATFORM_KEY,
                  wheels=False, cache_archive="release.tar.gz")

    save, = [kwargs for _, _, kwargs in recorder.deploy_kwargs if kwargs.get("save")]
    assert save["save"] == "release.tar.gz"
    assert save["package_query"] == "compiler.version=192"


def test_no_cache_archive_is_written_unless_one_is_asked_for(tmp_path, monkeypatch):
    """The archive exists for a forge that attaches it to a release."""
    recorder = _Recorder()
    _exported(tmp_path, "xmscore-linux-Release-1.2.3.tar.gz")

    job_deploy_in(tmp_path, recorder, monkeypatch, wheels=False)

    assert not [kwargs for _, _, kwargs in recorder.deploy_kwargs if kwargs.get("save")]


# --- the wheel half ---


def test_the_wheel_upload_never_asks_what_platform_it_is_on(tmp_path, monkeypatch):
    """The Windows wheels publish from a plain Linux python image on GitLab.

    A devpi upload of a finished wheel needs no toolchain and no Windows host,
    so the wheel half takes the fixed directory and nothing else -- including
    on a job that named ``--platform`` for the Conan half.
    """
    recorder = _Recorder()

    assert job_deploy_in(tmp_path, recorder, monkeypatch, conan=False,
                         platform=VS2019_PLATFORM_KEY) == EXIT_OK

    assert recorder.wheel_kwargs == [{"wheel_dir": common.WHEEL_DIR}]
    assert recorder.calls == ["wheel_deploy"]


def test_each_half_can_run_without_the_other(tmp_path, monkeypatch):
    """The two forges split the work differently.

    GitLab publishes wheels from a plain ``python`` image and Conan packages
    from a Windows VM, as separate jobs; GitHub does both in the one job that
    built them. A half that could not be switched off would have the GitLab
    wheel job trying to restore tarballs it has no artifacts for.
    """
    conan_only = _Recorder()
    _exported(tmp_path, "xmscore-linux-Release-1.2.3.tar.gz")
    job_deploy_in(tmp_path, conan_only, monkeypatch, wheels=False)
    assert "wheel_deploy" not in conan_only.calls

    wheels_only = _Recorder()
    job_deploy_in(tmp_path, wheels_only, monkeypatch, conan=False)
    assert wheels_only.calls == ["wheel_deploy"]

    both = _Recorder()
    job_deploy_in(tmp_path, both, monkeypatch)
    assert both.calls[0] == "conan_setup"
    assert both.calls[-1] == "wheel_deploy"


# --- the production steps ---


def test_the_default_steps_are_the_real_publish_functions():
    """A ``DeploySteps()`` with no arguments is what a runner gets.

    Resolved in ``__post_init__`` rather than as dataclass defaults, which
    bind at class creation and would freeze these module globals against a
    later patch of them.
    """
    steps = deploy.DeploySteps()

    assert steps.conan_setup is deploy._conan_setup
    assert steps.conan_deploy is deploy._conan_deploy
    assert steps.wheel_deploy is deploy._wheel_deploy


def test_the_recorded_calls_are_calls_the_production_functions_accept():
    """A rename in a real signature has to fail somewhere, and this is where.

    Recording a call rather than reproducing it is what makes the arguments
    assertable, and the cost is that :class:`_Recorder` accepts anything: a
    ``conan_deploy`` that renamed ``package_query`` would leave every test in
    this file green while the deploy raised on the runner. Binding the same
    calls against the production signatures is the other half of that seam --
    a signature check rather than a call, because these functions publish.
    """
    setup = inspect.signature(deploy._conan_setup)
    setup.bind(login=False)
    setup.bind(remote_name=VS2019_REMOTE_NAME, remote_url=VS2019_REMOTE_URL,
               index=None, login=False)

    conan = inspect.signature(deploy._conan_deploy)
    conan.bind("xmscore", "1.2.3", restore="xmscore-linux-Release-1.2.3.tar.gz")
    conan.bind("xmscore", "1.2.3", upload=True, remote=DEFAULT_REMOTE_NAME,
               package_query="compiler.version=194")
    conan.bind("xmscore", "1.2.3", save="release.tar.gz",
               package_query="compiler.version=194")

    inspect.signature(deploy._wheel_deploy).bind(wheel_dir=common.WHEEL_DIR)
