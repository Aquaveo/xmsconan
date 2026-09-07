"""What every ``xmsconan job`` command shares: paths, environment, log sections.

The output layout here is fixed and not configurable from a template. That is
what lets a generated job declare its ``artifacts:`` statically -- a path the
template computed would have to be computed identically by whatever writes it,
and the two lived in different files.
"""
import contextlib
import logging
import os
import re
import subprocess
import sys
import time
from types import MappingProxyType

import xmsconan

LOGGER = logging.getLogger(__name__)

#: Conan cache tarballs, one per exporting job, for the deploy to restore.
EXPORT_DIR = ".export"

#: Wheels, before and after repair.
WHEEL_DIR = "wheelhouse"

#: Staged test runners and their data, under ``<label>/`` per configuration.
ARTIFACTS_DIR = "test_artifacts"

#: Per-configuration build logs. Inside ARTIFACTS_DIR so a failing leg is
#: readable from the job's artifacts without a second `paths:` entry -- the
#: console shows a one-line pointer per configuration once `run(log_dir=...)`
#: is redirecting, and without the upload that pointer names nothing.
BUILD_LOG_DIR = os.path.join(ARTIFACTS_DIR, "logs")

#: The build type this job builds. Set in the generated job's `variables:`;
#: unset on a workstation, where it means "every build type in the matrix".
BUILD_TYPE_VARIABLE = "BUILD_TYPE"

#: The wheel ABI this job builds, and on GitLab also the container image tag.
PYTHON_TARGET_VARIABLE = "PYTHON_TARGET_VERSION"

#: ctest's own parallelism. Exported by every generated job today; set here
#: instead, and only when the environment has not already chosen a value.
CTEST_PARALLEL_VARIABLE = "CTEST_PARALLEL_LEVEL"

#: What that export set.
DEFAULT_CTEST_PARALLEL_LEVEL = "8"

#: Read by the recipe: compile the test runner but do not run it, because a
#: separate job will. Set when [ci].split_tests is on and this job builds the
#: testing leg.
SKIP_CXX_TESTS_VARIABLE = "XMS_SKIP_CXX_TESTS"

#: The interpreter every ``uv build`` in the dependency graph builds with.
#: uv reads it itself, which is why it is an environment variable rather than
#: a flag: it has to reach the recipe copies of dependencies Conan builds from
#: source, and nothing on this process's command line does.
UV_PYTHON_VARIABLE = "UV_PYTHON"

#: The three configuration kinds a build leg can select, as
#: ``filter_configurations`` option selectors. Testing and pybind are disjoint
#: copies of the base combinations, so each kind pins *both* flags: a selector
#: naming only ``pybind`` would take the testing configurations too on a
#: matrix that builds both.
LEG_SELECTORS = MappingProxyType({
    "library": MappingProxyType({"testing": False, "pybind": False}),
    "testing": MappingProxyType({"testing": True, "pybind": False}),
    "pybind": MappingProxyType({"pybind": True, "testing": False}),
})

#: ``--leg`` choices, in matrix order.
LEG_KINDS = tuple(LEG_SELECTORS)

_SECTION_KEY = re.compile(r"[^A-Za-z0-9_]+")


def resolve_leg(leg=None, release=False, release_skips_testing=False, environ=None):
    """The matrix filter this job builds, from ``--leg`` and the environment.

    This is the ``--filter '<< job.filter_json >>'`` expression and the
    ``BUILD_MATRIX_FILTER`` variable the GitLab template used to interpolate,
    as a function. The two spellings existed because a Linux job builds one
    configuration and the Windows job builds the platform's whole matrix; both
    reduce to a filter, so both are produced here.

    Args:
        leg: One of :data:`LEG_KINDS`, or None for every configuration the
            platform's matrix produces.
        release: Whether the resolved version names a release -- a tag
            pipeline. Only consulted with *release_skips_testing*.
        release_skips_testing: Drop the testing configurations on a release.
            The Windows job's tag rule: nothing installs a test runner and no
            tag pipeline runs one, and for a wheel-only repository the testing
            configurations are most of the matrix. It is a flag rather than an
            unconditional rule because the jobs that loop the whole matrix on
            Linux publish from that same build, and their tarball would lose
            binaries a release ships.
        environ: The environment to read; ``os.environ`` when None.

    Returns:
        A filter dict for
        :meth:`~xmsconan.package_tools.packager.XmsConanPackager.filter_configurations`.
        Empty when nothing narrows the matrix, which that method treats the
        same way as not being called at all.

    Raises:
        ValueError: *leg* is not one of :data:`LEG_KINDS`.
    """
    if leg is not None and leg not in LEG_SELECTORS:
        raise ValueError(f"unknown leg {leg!r}; expected one of {', '.join(LEG_KINDS)}")

    environ = os.environ if environ is None else environ
    selection = {}

    build_type = environ.get(BUILD_TYPE_VARIABLE)
    if build_type:
        selection["build_type"] = build_type

    if leg is not None:
        options = dict(LEG_SELECTORS[leg])
        if leg == "pybind":
            # Only the pybind variants carry python_version, and an options key
            # the configuration does not have never matches -- so the key is
            # omitted rather than set to None when the environment names no
            # ABI, which is a workstation asking for every ABI it builds.
            python_version = environ.get(PYTHON_TARGET_VARIABLE)
            if python_version:
                options["python_version"] = python_version
        selection["options"] = options
    elif release and release_skips_testing:
        selection["options"] = {"testing": False}

    return selection


def set_job_environment(config, defer_cxx_tests=False, environ=None):
    """Set the variables a generated job used to ``export`` before its build.

    Each one is set only when the environment has not already chosen a value,
    so a runner or a developer can still override it -- which is exactly what
    ``${CTEST_PARALLEL_LEVEL:-8}`` meant in the template.

    Args:
        config: The parsed build.toml.
        defer_cxx_tests: This build's C++ suite is run by a separate job in the
            same pipeline, so it must not also run inline here.
        environ: The environment to modify; ``os.environ`` when None.

    Returns:
        The names set, in the order they were set. Empty when the environment
        already carried all of them.
    """
    environ = os.environ if environ is None else environ
    set_names = []

    def _default(name, value):
        if not environ.get(name):
            environ[name] = value
            set_names.append(name)

    _default(CTEST_PARALLEL_VARIABLE, DEFAULT_CTEST_PARALLEL_LEVEL)

    python_version = environ.get(PYTHON_TARGET_VARIABLE)
    if python_version:
        _default(UV_PYTHON_VARIABLE, python_version)

    # Which jobs hand their runner to a separate test job is a fact about the
    # pipeline's shape, so the generator states it with `--defer-cxx-tests`
    # rather than the tool inferring it from `--leg`. Inferring it was wrong in
    # both directions: the `--leg`-less Linux build feeds the "Run C++ Tests"
    # jobs and got no skip, while the Windows build renders the same flagless
    # command and must keep running its suite inline, because no Windows test
    # job is generated to run it. [ci].split_tests still gates, so turning
    # sharding off takes effect without regenerating.
    if config.ci.split_tests and defer_cxx_tests:
        _default(SKIP_CXX_TESTS_VARIABLE, "1")

    return set_names


def _section_key(title):
    """A GitLab section name: letters, digits and underscores only."""
    return _SECTION_KEY.sub("_", title).strip("_").lower() or "section"


@contextlib.contextmanager
def log_section(title, environ=None, stream=None):
    """Group a phase's output in the host's log viewer.

    GitLab and GitHub both fold a job's log, and neither reads the other's
    markers, so the command emits whichever the environment says it is under
    and a plain banner otherwise -- the ``==>`` line ``publish`` prints today,
    which is what a workstation run should still look like.

    The end marker is emitted even when the body raises: an unclosed section
    swallows the rest of the log into the failing phase's fold, which is the
    part of the job a reader most needs open.
    """
    environ = os.environ if environ is None else environ
    stream = sys.stdout if stream is None else stream
    key = _section_key(title)
    timestamp = int(time.time())

    if environ.get("GITLAB_CI"):
        print(f"\x1b[0Ksection_start:{timestamp}:{key}\r\x1b[0K{title}", file=stream)
    elif environ.get("GITHUB_ACTIONS"):
        print(f"::group::{title}", file=stream)
    else:
        print(f"==> {title}", file=stream)
    stream.flush()

    try:
        yield
    finally:
        if environ.get("GITLAB_CI"):
            print(f"\x1b[0Ksection_end:{int(time.time())}:{key}\r\x1b[0K", file=stream)
        elif environ.get("GITHUB_ACTIONS"):
            print("::endgroup::", file=stream)
        stream.flush()


def tool_version_commands(platform=None):
    """The external tools a job reports the version of before it works.

    cmake because the ``[ci]`` extra's wheel lands ahead of the image's copy on
    PATH and the one printed has to be the one that configures; gcc because the
    Linux images are named for a compiler version that the job should confirm
    rather than assume. Windows prints neither compiler line: MSVC is selected
    by the profile, and ``cl`` is not on PATH until a build shell sets it up.
    """
    platform = sys.platform if platform is None else platform
    commands = []
    if platform.startswith("linux"):
        commands.append(["gcc", "--version"])
    commands.append(["cmake", "--version"])
    return commands


def print_tool_versions(runner=None, platform=None, stream=None):
    """Print the toolchain this job is about to run with.

    xmsconan's own version first: the generated job installs
    ``xmsconan[ci]>=X,<X+1`` and the line says which X it resolved to, which is
    the one thing a reader cannot recover from the job definition.

    A tool that is missing or fails is reported and does not stop the job. The
    banner is diagnostic; a cmake that is really absent fails the build a
    minute later with an error about cmake, and failing here instead would
    hide that behind a version check.
    """
    runner = subprocess.run if runner is None else runner
    stream = sys.stdout if stream is None else stream
    print(f"xmsconan {xmsconan.__version__}", file=stream)
    print(f"python {sys.version.split()[0]} ({sys.executable})", file=stream)
    stream.flush()
    for command in tool_version_commands(platform):
        try:
            runner(command, check=False)
        except OSError as error:
            print(f"{command[0]}: not available ({error})", file=stream)
