"""``xmsconan job <kind>`` -- the argument parsing for a CI job as a tool call.

``job`` rather than ``run`` or ``stage``: ``ci`` is the generator that writes
these pipelines, ``build`` is already the library build, and what the argument
names is a *job* in the pipeline the generator wrote -- the same word GitLab
and GitHub both use for the thing whose ``script:`` this replaces.

``build``, ``deploy``, ``lint`` and ``coverage --pages`` are implemented in
this package. ``test`` and ``package`` are the existing ``test_shards`` and
``wheel_repair`` with their arguments filled in from ``build.toml`` and the
fixed output layout, which is the whole reason the template rendered flags onto
them; neither is rewritten. ``coverage`` takes only ``--pages`` for now -- its
measure and report phases are still ``xmsconan coverage --phase``, which the
generated coverage jobs call directly.
"""
import argparse
import logging
import subprocess
import sys

from xmsconan._cli import (add_verbosity_args, configure_logging, MissingToolError, resolve_tool,
                           run_main)
from xmsconan.build_toml import read_build_toml
from xmsconan.ci_tools import test_shards, wheel_repair
from xmsconan.constants import VS2019_PLATFORM_KEY
from xmsconan.exit_codes import EXIT_OK
from xmsconan.generator_tools.build_file_generator import generate_build_files
from xmsconan.generator_tools.version import FALLBACK_VERSION, VERSION_FLAG_HELP
from xmsconan.job_tools import common, pages, xvfb
from xmsconan.job_tools.build import job_build
from xmsconan.job_tools.deploy import job_deploy
from xmsconan.job_tools.pages import job_coverage_pages
from xmsconan.package_tools import packager

LOGGER = logging.getLogger(__name__)

#: What ``job lint`` lints. The generated ``_package/`` tree is the Python
#: half of a library -- the only generated source flake8 has anything to say
#: about -- and it is written by the ``gen`` this command runs first.
LINT_TARGET = "_package"


def job_test(label=None, toml_path="build.toml", runner_args=(), timeout=None):
    """Run the staged C++ test runner for one configuration.

    Everything ``xmsconan_test_shards`` needed as a flag except the label is a
    fact about this repository or the fixed layout: the artifacts directory the
    build wrote, the shard count from ``[ci].test_shards``, and whether the
    shards need displays -- ``[ci].xvfb`` asked through
    :func:`~xmsconan.job_tools.xvfb.wants_xvfb`, which is the same predicate
    the build and the coverage run ask, so a host with no ``xvfb-run`` warns
    and runs bare instead of every shard dying on a missing ``Xvfb``. The
    label stays a flag because it
    names *which* configuration this job tests, which is the one thing the job
    knows and ``build.toml`` does not.
    """
    config = read_build_toml(toml_path)
    shards = config.ci.test_shards if config.ci.test_shards > 1 else 1
    return test_shards.run(
        common.ARTIFACTS_DIR, shards, label=label,
        output=test_shards.DEFAULT_REPORT_NAME,
        xvfb=xvfb.wants_xvfb(config),
        timeout=test_shards.DEFAULT_SHARD_TIMEOUT if timeout is None else timeout,
        runner_args=list(runner_args),
    )


def job_package():
    """Repair the wheels the build staged, in whatever image this runs in.

    On Linux that image is manylinux and the tool is auditwheel; the platform
    is detected, as it always was. The directory is the fixed layout's.
    """
    wheel_repair.wheel_repair(wheel_dir=common.WHEEL_DIR)
    return EXIT_OK


def job_lint(toml_path="build.toml", runner=None):
    """Generate the build files, then lint the generated Python package.

    The version is the fallback rather than a resolved one: linting is not
    publishing, nothing here reaches a package name, and pinning it keeps a
    tag pipeline's lint job from differing from a branch's.

    flake8's own plugin set comes from the environment this runs in -- the
    ``[ci]`` extra carries the ones the generated ``.flake8`` is written
    against, and a job that wants the internal AQU rules installs
    ``flake8-aquaveo`` alongside. Deliberately not folded into the extra:
    that would switch those rules on for every repository that installs it,
    including the GitHub ones whose lint job has never run them, and changing
    eight repositories' lint policy is not a side effect this command should
    have.
    """
    runner = subprocess.run if runner is None else runner
    with common.log_section("Generate build files"):
        generated = generate_build_files(toml_file_path=toml_path, version=FALLBACK_VERSION)
        if generated:
            return generated
    flake8 = resolve_tool("flake8")
    if flake8 is None:
        raise MissingToolError(
            "flake8 is not installed. A generated lint job installs it with the "
            "xmsconan[ci] extra; see docs/USAGE.md."
        )
    with common.log_section(f"flake8 {LINT_TARGET}"):
        return runner([flake8, LINT_TARGET], check=False).returncode


def _add_build_arguments(parser):
    """Flags for ``job build``."""
    parser.add_argument(
        "--leg", default=None, choices=list(common.LEG_KINDS),
        help="Which configuration kind this job builds. Default: every "
             "configuration this platform's matrix produces, narrowed by "
             f"${common.BUILD_TYPE_VARIABLE} and the build.toml [filter].",
    )
    parser.add_argument(
        "--platform", default=None, choices=sorted(packager.configurations),
        help="Matrix to build. Default: detect from the running machine. "
             f"{VS2019_PLATFORM_KEY} also appends the VS2019 Conan remote, "
             "builds missing dependencies from source, and stages no wheel.",
    )
    parser.add_argument(
        "--export", action="store_true",
        help="Save a Conan cache tarball under "
             f"{common.EXPORT_DIR}/ for the deploy job to restore. Passed by "
             "the jobs a tag pipeline publishes from.",
    )
    parser.add_argument(
        "--defer-cxx-tests", action="store_true",
        help="A separate job in this pipeline runs the C++ suite this build "
             f"compiles, so set ${common.SKIP_CXX_TESTS_VARIABLE} and do not "
             "also run it inline. Inert unless [ci].split_tests is on.",
    )
    parser.add_argument(
        "--release-skips-testing", action="store_true",
        help="On a release version, drop the testing configurations. Nothing "
             "installs a test runner and no tag pipeline runs one; for a "
             "wheel-only repository they are most of the matrix.",
    )
    parser.add_argument(
        "--build-missing", action="store_true",
        help="Build missing dependencies from source.",
    )
    parser.add_argument(
        "--version", default=None,
        help=f"Package version string. {VERSION_FLAG_HELP}",
    )


def _add_deploy_arguments(parser):
    """Flags for ``job deploy``."""
    parser.add_argument(
        "--platform", default=None, choices=sorted(packager.configurations),
        help="Matrix whose remote this publishes to. Default: detect from the "
             f"running machine. Only {VS2019_PLATFORM_KEY} changes the answer, "
             "sending its binaries to the VS2019 remote instead of the CI one.",
    )
    # Mutually exclusive rather than two independent switches: passing both
    # would ask for a deploy that publishes nothing, and the argument parser is
    # a better place to learn that than a green job with an empty log.
    halves = parser.add_mutually_exclusive_group()
    halves.add_argument(
        "--conan-only", action="store_true",
        help="Publish the Conan packages and not the wheels. GitLab uploads "
             "wheels from a separate job on a plain Python image.",
    )
    halves.add_argument(
        "--wheels-only", action="store_true",
        help="Publish the wheels and not the Conan packages.",
    )
    parser.add_argument(
        "--cache-archive", default=None, metavar="PATH",
        help="After uploading, write a Conan cache tarball here for a forge to "
             "attach to the release.",
    )
    parser.add_argument(
        "--from-cache", action="store_true",
        help=f"Publish what this runner's Conan cache already holds rather "
             f"than restoring {common.EXPORT_DIR}/. For a workflow whose build "
             "and deploy are one job on one runner, which is every GitHub "
             "platform job; GitLab's are separate runners and must restore.",
    )
    parser.add_argument(
        "--version", default=None,
        help=f"Package version string. {VERSION_FLAG_HELP}",
    )


def check_deploy_arguments(parser, args):
    """Refuse ``job deploy`` flags the half being published would never read.

    ``--from-cache`` and ``--cache-archive`` are consulted only while the Conan
    half publishes, so beside ``--wheels-only`` they are accepted and then
    ignored. Refused for the reason the ``--conan-only``/``--wheels-only``
    group is: a deploy that quietly did something other than what its flags
    asked for goes green saying nothing.

    A function rather than parser configuration because ``argparse`` cannot
    state a rule spanning two flags that are not mutually exclusive with each
    other; it lives here so the whole deploy contract stays in one place.

    Args:
        parser: The parser to report a usage error through.
        args: The parsed arguments.
    """
    if args.kind != "deploy" or not args.wheels_only:
        return
    unread = [name for name, value in (("--from-cache", args.from_cache),
                                       ("--cache-archive", args.cache_archive))
              if value]
    if unread:
        parser.error(
            f"{' and '.join(unread)} {'is' if len(unread) == 1 else 'are'} read only "
            "when the Conan half is published, which --wheels-only turns off"
        )


def _add_toml_argument(parser):
    """The ``--toml`` flag, on every subcommand that reads build.toml."""
    parser.add_argument(
        "--toml", default="build.toml", dest="toml_path",
        help="Path to build.toml (default: build.toml).",
    )


def build_parser():
    """The ``xmsconan job`` parser."""
    parser = argparse.ArgumentParser(
        description="Run one CI job: the work a generated pipeline's script: used to spell out.",
    )
    subparsers = parser.add_subparsers(dest="kind", required=True)

    build = subparsers.add_parser("build", help="Build one matrix leg and stage its outputs.")
    _add_build_arguments(build)
    _add_toml_argument(build)
    add_verbosity_args(build)

    test = subparsers.add_parser("test", help="Run a staged C++ test runner as parallel shards.")
    test.add_argument(
        "--label", default=None,
        help="Configuration label to test, e.g. 'Release-testing'. Only needed "
             "when several testing configurations are staged.",
    )
    test.add_argument(
        "--timeout", type=int, default=None,
        help=f"Seconds a single shard may run (default: {test_shards.DEFAULT_SHARD_TIMEOUT}).",
    )
    _add_toml_argument(test)
    add_verbosity_args(test)
    test.add_argument(
        "runner_args", nargs="*",
        help="Extra arguments passed through to every shard, e.g. "
             "--gtest_filter=Foo*. Put them after a bare --.",
    )

    package = subparsers.add_parser("package", help="Repair the staged wheels for this platform.")
    add_verbosity_args(package)

    deploy = subparsers.add_parser("deploy", help="Publish the exported packages and wheels.")
    _add_deploy_arguments(deploy)
    _add_toml_argument(deploy)
    add_verbosity_args(deploy)

    coverage = subparsers.add_parser(
        "coverage", help="Assemble the coverage site from the rendered reports.")
    coverage.add_argument(
        "--pages", action="store_true", required=True,
        help=f"Write the {pages.PAGES_DIR}/ tree and its index from the "
             "coverage-html-* directories the coverage job rendered. Required: "
             "the measure and report phases are `xmsconan coverage --phase`, "
             "and this subcommand does not stand in for them.",
    )
    _add_toml_argument(coverage)
    add_verbosity_args(coverage)

    lint = subparsers.add_parser("lint", help="Generate the build files and lint the package.")
    _add_toml_argument(lint)
    add_verbosity_args(lint)

    return parser


def main():
    """Entry point for ``xmsconan job``: returns the process exit code."""
    return run_main(_main)


def _main():
    """Parse arguments and run the named job."""
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args)
    check_deploy_arguments(parser, args)

    if args.kind == "build":
        return job_build(
            leg=args.leg,
            platform=args.platform,
            version=args.version,
            toml_path=args.toml_path,
            export=args.export,
            release_skips_testing=args.release_skips_testing,
            build_missing=args.build_missing,
            defer_cxx_tests=args.defer_cxx_tests,
        )
    if args.kind == "test":
        return job_test(label=args.label, toml_path=args.toml_path,
                        runner_args=args.runner_args, timeout=args.timeout)
    if args.kind == "package":
        return job_package()
    if args.kind == "deploy":
        return job_deploy(
            platform=args.platform,
            version=args.version,
            toml_path=args.toml_path,
            conan=not args.wheels_only,
            wheels=not args.conan_only,
            cache_archive=args.cache_archive,
            from_cache=args.from_cache,
        )
    if args.kind == "coverage":
        return job_coverage_pages(read_build_toml(args.toml_path).library_name)
    return job_lint(toml_path=args.toml_path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
