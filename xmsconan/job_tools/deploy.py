"""``xmsconan job deploy`` -- publish what the build jobs exported.

What a generated deploy job used to be: a ``conan_setup``, one
``conan_deploy --restore`` line per exporting job with the tarball's name
reassembled in Jinja from the same parts ``job build`` had just used to write
it, an ``--upload`` carrying a remote and a ``--package-query`` written as
literals per platform, and -- on Windows -- a ``cp -r`` of the Conan cache
into an artifact nothing read.

Two of those were duplication with a failure mode. The names were composed
twice, so a rename in :func:`~xmsconan.job_tools.build.export_tarball_name`
left the deploy restoring a path that no longer existed; this globs
:data:`~xmsconan.job_tools.common.EXPORT_DIR` instead, which cannot disagree
with what the build wrote. And the remote/query pairing was three literals in
the template, where a toolchain bump makes ``-p compiler.version=194`` match
nothing and the job publish nothing, green -- so it is one mapping here, over
the version :func:`~xmsconan.package_tools.packager.only_msvc_version` reads
from the matrix itself.

The two halves are separately selectable because the two forges run them
differently: GitLab publishes wheels from a plain ``python`` image and Conan
packages from a Windows VM, as different jobs, while GitHub does both in the
one job that built them.

That difference is also why the restore is a flag. On GitLab the packages
arrive as artifacts from runners this job never shared, so restoring them is
the whole hand-off and an empty ``.export/`` is a broken pipeline. On GitHub
the build ran in this job, on this runner, into this cache -- there is no
hand-off to check, and ``--from-cache`` says so. The caller states which
world it is in rather than the tool guessing from an empty directory,
because those two worlds want opposite answers from the same evidence.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any, Callable, Optional

from xmsconan.build_toml import read_build_toml
from xmsconan.ci_tools.conan_deploy import conan_deploy as _conan_deploy
from xmsconan.ci_tools.conan_setup import conan_setup as _conan_setup
from xmsconan.ci_tools.wheel_deploy import wheel_deploy as _wheel_deploy
from xmsconan.constants import (DEFAULT_REMOTE_NAME, VS2019_PLATFORM_KEY, VS2019_REMOTE_NAME,
                                VS2019_REMOTE_URL, WINDOWS_PLATFORM_KEY)
from xmsconan.exit_codes import EXIT_OK
from xmsconan.generator_tools.version import (FALLBACK_VERSION, is_release_version,
                                              resolve_version)
from xmsconan.job_tools import common
from xmsconan.package_tools.packager import only_msvc_version

#: Tarballs this job restores, in name order -- not newest-first or -last,
#: which the names do not encode: sorted so two runs over the same artifacts
#: log the same sequence. ``job build`` writes them; see
#: :func:`~xmsconan.job_tools.build.export_tarball_name`.
EXPORT_GLOB = "*.tar.gz"


def upload_target(platform_key=None, platform=None):
    """The Conan remote and package query this platform publishes through.

    Three pairings, and the query half of two of them is what keeps a shared
    Windows runner honest: its Conan cache is per machine, so ``conan upload
    <ref>:*`` from a job whose neighbour built the other toolchain would
    publish the neighbour's binaries under this job's name. Linux and macOS
    each own their container and therefore their cache, so there is nothing
    else in it to exclude -- the same asymmetry
    :func:`~xmsconan.job_tools.build.export_package_query` states for the save.

    The msvc version is read from the packager matrix rather than written here,
    because the failure of a stale literal is silent: ``-p
    compiler.version=194`` after a bump to 195 matches nothing and the upload
    exits 0 having published nothing.

    Args:
        platform_key: The build.toml matrix key this job deploys, when it names
            one. Only ``windows_vs2019`` changes the answer.
        platform: ``sys.platform`` value to answer for; the running one when
            None.

    Returns:
        ``(remote, package_query)``; the query is None where none is needed.
    """
    if platform_key == VS2019_PLATFORM_KEY:
        return VS2019_REMOTE_NAME, f"compiler.version={only_msvc_version(VS2019_PLATFORM_KEY)}"
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return DEFAULT_REMOTE_NAME, None
    return DEFAULT_REMOTE_NAME, f"compiler.version={only_msvc_version(WINDOWS_PLATFORM_KEY)}"


def exported_tarballs(export_dir=None):
    """Every cache tarball the build jobs left for this deploy, sorted.

    Globbed rather than named. The template used to reassemble each path from
    the library, platform, leg and version the build had already used to write
    it, which made a rename in one place a restore of a file that is not there
    -- and, before ``conan cache save`` learned ``:*``, a restore of nothing
    that uploaded nothing and exited 0.

    An empty directory raises instead of returning ``[]`` for that same reason:
    a deploy with nothing to publish is a broken artifact hand-off, not a
    successful no-op. :func:`~xmsconan.ci_tools.wheel_deploy._wheels_in`
    already refuses its empty case, and it distinguishes a missing directory
    from an empty one because they send an operator to different faults.

    Raises:
        ValueError: The export directory is missing, or holds no tarball.
    """
    directory = Path(common.EXPORT_DIR if export_dir is None else export_dir)
    if not directory.is_dir():
        raise ValueError(
            f"no {directory} directory: this job depends on the build jobs' artifacts, "
            f"and none arrived. Nothing was published."
        )
    tarballs = sorted(str(path) for path in directory.glob(EXPORT_GLOB))
    if not tarballs:
        raise ValueError(
            f"{directory} holds no {EXPORT_GLOB}: the build jobs either did not run with "
            f"--export or their artifacts did not reach this job. Nothing was published."
        )
    return tarballs


@dataclass
class DeploySteps:
    """Callable steps used by :func:`job_deploy`.

    Each field defaults to the production implementation; tests supply fakes
    that record the arguments, which is the property worth asserting here --
    a deploy is a sequence of calls whose *arguments* decide which remote
    receives which binaries.

    The fields default to None and are resolved in ``__post_init__`` rather
    than defaulting to the functions themselves: a dataclass default is bound
    when the class is created, which would freeze the module globals these
    name against any later patch of them, and
    :class:`~xmsconan.ci_tools.publish.PublishSteps` resolves the same way for
    tests that patch exactly those globals.
    """

    conan_setup: Optional[Callable[..., Any]] = None
    conan_deploy: Optional[Callable[..., Any]] = None
    wheel_deploy: Optional[Callable[..., Any]] = None

    def __post_init__(self):  # noqa: D105
        if self.conan_setup is None:
            self.conan_setup = _conan_setup
        if self.conan_deploy is None:
            self.conan_deploy = _conan_deploy
        if self.wheel_deploy is None:
            self.wheel_deploy = _wheel_deploy


def _deploy_conan(config, version, platform, steps, cache_archive, environ, from_cache):
    """Restore every exported tarball into this cache, then publish once."""
    remote, package_query = upload_target(platform_key=platform)
    # Read before `conan_setup`, not after: on a miss this job has nothing to
    # publish either way, and setup rewrites a `remotes.json` that several
    # jobs on a shared Windows runner write at once (USAGE 10.2). A failure
    # that touched nothing is one less concurrent writer for no benefit.
    tarballs = () if from_cache else exported_tarballs()

    with common.log_section("Conan setup", environ=environ):
        # No login, for the reason `job build` states: Conan reads
        # CONAN_LOGIN_USERNAME and CONAN_PASSWORD from the environment itself,
        # and `conan remote login` with nothing to hand it prompts -- on a
        # runner that is a hang rather than a message.
        steps.conan_setup(login=False)
        if platform == VS2019_PLATFORM_KEY:
            # Appended rather than inserted first: it must not become the first
            # stop for every `conan install` on a shared runner.
            steps.conan_setup(remote_name=VS2019_REMOTE_NAME,
                              remote_url=VS2019_REMOTE_URL, index=None, login=False)

    if tarballs:
        with common.log_section("Restore exported packages", environ=environ):
            for tarball in tarballs:
                print(f"Restoring {tarball}")
                steps.conan_deploy(config.library_name, version, restore=tarball)
    else:
        print("Publishing this runner's own cache: --from-cache, nothing to restore.")

    # One upload after the whole set is restored, not one per tarball: `conan
    # upload <ref>:*` publishes every package id under the reference, so a
    # per-tarball upload would push the same recipe once per exporting job.
    with common.log_section(f"Upload to {remote}", environ=environ):
        steps.conan_deploy(config.library_name, version, upload=True,
                           remote=remote, package_query=package_query)

    if cache_archive:
        # Queried like the upload, and for the same reason: this reads the
        # runner's shared cache, so an unqueried save would tarball whichever
        # toolchain's binaries a neighbouring job left behind.
        with common.log_section("Save cache archive", environ=environ):
            steps.conan_deploy(config.library_name, version, save=cache_archive,
                               package_query=package_query)


def job_deploy(platform=None, version=None, toml_path="build.toml", conan=True, wheels=True,
               cache_archive=None, from_cache=False, steps=None, environ=None):
    """Publish this pipeline's Conan packages and wheels.

    Args:
        platform: The build.toml matrix key whose remote this publishes to.
            Only ``windows_vs2019`` changes anything; every other job's remote
            and query follow the running platform.
        version: Package version. Resolved from the CI environment when None,
            and refused when that resolves to something nobody released.
        toml_path: Path to build.toml, read for the library name.
        conan: Restore the exported tarballs and upload them.
        wheels: Upload the staged wheels.
        cache_archive: Write a ``conan cache save`` tarball at this path after
            the upload, for a forge that attaches it to a release.
        from_cache: Publish what this runner's Conan cache already holds
            instead of restoring ``.export/``. For a forge whose build and
            deploy are the same job on the same runner; see the module
            docstring.
        steps: Injected callables; the production ones when None.
        environ: Environment to read; ``os.environ`` when None.

    Returns:
        :data:`~xmsconan.exit_codes.EXIT_OK`. Every failure raises, and
        :func:`~xmsconan._cli.run_main` turns it into one line and exit 1.

    Raises:
        ValueError: The version is not one a release names, or -- from
            :func:`exported_tarballs`, which is the likelier fault, and
            which *from_cache* does not consult -- ``.export/`` is missing
            or holds no tarball.
    """
    steps = DeploySteps() if steps is None else steps
    environ = os.environ if environ is None else environ

    version = resolve_version(version, environ=environ)
    if not is_release_version(version):
        # Asked before either half runs, because both publish outward and
        # neither takes it back: a Conan reference every consumer resolves
        # against, and a wheel on the index they install from. The entry
        # points this replaced refused it -- `xmsconan conan-deploy` at its
        # parser, `xmsconan publish` before its first step -- and a generated
        # job cannot reach it, every deploy being `only: tags`, so what this
        # guards is the hand-run replay USAGE 10.5 invites.
        raise ValueError(
            f"deploy needs a release version, not {version!r}: {FALLBACK_VERSION} is what an "
            "untagged build resolves to, and a glob would publish every version in the cache. "
            "Run it from a tag pipeline, or pass --version."
        )
    config = read_build_toml(toml_path)
    print(f"Deploying {config.library_name} {version}.")

    if conan:
        _deploy_conan(config, version, platform, steps, cache_archive, environ, from_cache)

    if wheels:
        # The wheel half never asks what platform it is on: GitLab publishes
        # the Windows wheels from a plain Linux python image, because a devpi
        # upload of a finished wheel needs no toolchain and no Windows host.
        with common.log_section("Upload wheels", environ=environ):
            steps.wheel_deploy(wheel_dir=common.WHEEL_DIR)

    return EXIT_OK
