"""``xmsconan job <kind>`` -- the argument parsing for a CI job as a tool call.

``job`` rather than ``run`` or ``stage``: ``ci`` is the generator that writes
these pipelines, ``build`` is already the library build, and what the argument
names is a *job* in the pipeline the generator wrote -- the same word GitLab
and GitHub both use for the thing whose ``script:`` this replaces.

Only ``build`` and ``lint`` are implemented here. ``test`` and ``package`` are
the existing ``test_shards`` and ``wheel_repair`` with their arguments filled
in from ``build.toml`` and the fixed output layout, which is the whole reason
the template rendered flags onto them; neither is rewritten.
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
from xmsconan.job_tools import common
from xmsconan.job_tools.build import job_build
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
    shards need displays from ``[ci].xvfb``. The label stays a flag because it
    names *which* configuration this job tests, which is the one thing the job
    knows and ``build.toml`` does not.
    """
    config = read_build_toml(toml_path)
    shards = config.ci.test_shards if config.ci.test_shards > 1 else 1
    return test_shards.run(
        common.ARTIFACTS_DIR, shards, label=label,
        output=test_shards.DEFAULT_REPORT_NAME,
        xvfb=config.ci.xvfb,
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

    if args.kind == "build":
        return job_build(
            leg=args.leg,
            platform=args.platform,
            version=args.version,
            toml_path=args.toml_path,
            export=args.export,
            release_skips_testing=args.release_skips_testing,
            build_missing=args.build_missing,
        )
    if args.kind == "test":
        return job_test(label=args.label, toml_path=args.toml_path,
                        runner_args=args.runner_args, timeout=args.timeout)
    if args.kind == "package":
        return job_package()
    return job_lint(toml_path=args.toml_path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
