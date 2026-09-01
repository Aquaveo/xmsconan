"""Script to generate CI configuration files from build.toml."""
# 1. Standard python modules
import argparse
import logging
from pathlib import Path
import sys

# 2. Third party modules
from jinja2 import Environment, StrictUndefined

# 3. Aquaveo modules
from xmsconan.build_toml import BuildToml, CiTable, CoverageTable, read_build_toml
from xmsconan.ci_options import repairs_windows_wheel
from xmsconan.constants import (
    MSVC_VS2019_VERSION,
    SUPPORTED_PYTHON_VERSIONS,
    version_sort_key,
    VS2019_PLATFORM_KEY,
    VS2019_REMOTE_NAME,
    VS2019_REMOTE_URL,
)
from xmsconan.generator_tools.build_filter import (
    ci_build_jobs,
    ci_filter_effects,
    coverage_conflicts,
    empty_ci_jobs,
    load_build_filter,
)


LOGGER = logging.getLogger(__name__)

#: The GitHub build step's wheel request, gated to the leg that publishes one.
#:
#: Every step that consumes the wheel -- repair, artifact upload, devpi deploy
#: -- is gated on ``matrix.build_type == 'Release'``, and
#: ``[matrix].pybind_build_types`` defaults to Release only, so a Debug leg has
#: no pybind configuration to extract a wheel from. ``build.py`` exits 1 when
#: ``--wheel-dir`` yields no complete set of wheels, which is the right answer
#: on a Release leg and wrong on a Debug one: on Windows a Debug pybind
#: configuration produces no wheel by design (USAGE section 7.5), and on Linux
#: and macOS a Debug wheel would only be built and discarded. Asking for the
#: wheel where one is expected keeps that check at full strength where it
#: matters.
#:
#: GitLab needs no equivalent: its build step runs the whole matrix in one
#: invocation with no build-type filter, so the Release pybind configuration is
#: always in scope.
RELEASE_ONLY_WHEEL_DIR = (
    "${{ matrix.build_type == 'Release' && ' --wheel-dir wheelhouse' || '' }}"
)


def _configure_logging(args):
    """Configure logger from CLI verbosity flags."""
    if args.quiet:
        level = logging.ERROR
    elif args.verbose > 0:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')


def _write_text_lf(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write text using LF line endings on all platforms."""
    content = content.replace("\r\n", "\n")
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(content)


def _display_name(library_name: str) -> str:
    """Convert library_name to display format (e.g., 'xmscore' -> 'XmsCore')."""
    return "Xms" + library_name[3:].title()


def _job_name_py(platform_python_versions: list) -> str:
    """Return the ``name:`` fragment that keeps fanned-out legs distinguishable.

    Empty on a single-version platform. GitHub uses an explicit ``name:``
    verbatim and only auto-appends matrix values when none is given, so legs
    differing solely by ABI would otherwise share one status-check name --
    ambiguous both in the checks list and in branch-protection matching. Held
    to the fan-out case for the same reason as :func:`_py_suffix`: adding it
    unconditionally would rename the required check in every repo that has not
    opted in.
    """
    if len(platform_python_versions) > 1:
        return ", ${{ matrix.python-version }}"
    return ""


def _platform_python_versions(ci: CiTable, platform: str, ci_python_versions: list) -> list:
    """Resolve the Python fan-out for one non-Windows platform.

    ``[ci].python_versions`` fans out Windows only, because Windows is the
    one platform whose interpreters all come from ``actions/setup-python``
    and therefore cost nothing but runner minutes. macOS and Linux opt in
    separately via ``[ci].mac_python_versions`` / ``[ci].linux_python_versions``:

    * macOS, so that adding a Windows-only ABI (3.10 ships in Windows
      wheels for the desktop products) does not silently triple the mac
      matrix.
    * Linux, because each entry needs a matching
      ``ghcr.io/aquaveo/conan-gcc13-py<version>`` container to exist. Naming
      a version with no published image yields a job that cannot start, so
      this list tracks the images that are actually built rather than
      whatever ``python_versions`` happens to say.

    Defaults to the single highest entry of ``ci_python_versions``, which is
    the behavior these platforms had when the version was hardcoded.
    """
    configured = getattr(ci, f"{platform}_python_versions")
    if configured:
        return list(configured)
    return [max(ci_python_versions, key=version_sort_key)]


def _py_suffix(platform_python_versions: list) -> str:
    """Return the per-ABI suffix for artifact and release-asset names.

    Empty on a single-version platform. Job names, uploaded artifacts and the
    release-asset tarball are all keyed off ``MATRIX_NAME``, so a platform that
    fans out across ABIs needs the version in that name or the legs overwrite
    each other. Suppressing the suffix when there is nothing to disambiguate
    keeps the asset names of every project that has not opted in byte-identical
    to what it published before -- release assets are fetched by exact name.
    """
    if len(platform_python_versions) > 1:
        return "-py${{ matrix.python-version }}"
    return ""


def _unsupported_python_versions(versions) -> list:
    """Return the entries of ``versions`` the conanfile's python_version option rejects.

    Both callers stringify before comparing, because a TOML list may hold
    floats -- an unquoted ``3.14`` parses as one -- and the supported set is
    written as strings. They keep their own error messages: the two failures
    surface at different times and want different explanations. What is shared
    is only the membership test, which is the part that would silently drift
    if one caller were updated and the other were not.
    """
    return [str(v) for v in versions if str(v) not in SUPPORTED_PYTHON_VERSIONS]


_DEFAULT_COVERAGE_PYTHON_VERSION = "3.13"


def _mark_export_jobs(build_jobs: list) -> list:
    """Mark the Linux build jobs that save a Conan cache tarball, and list them.

    Every job that runs on a tag, because with one build job per configuration
    each one has its own container and its own Conan cache: ``conan cache save``
    in the pybind job sees the pybind package and nothing else. Naming a single
    job the exporter -- which is what a pipeline with one build job could
    afford to do -- would publish that job's package id and silently drop every
    other one, so a release would ship a wheel and no library for the
    downstream repositories that link against it.

    Branch-only jobs are skipped because the deploy is ``only: tags``: their
    tarball would never be restored, and the ``.export`` upload would be paid
    on every branch pipeline for nothing.

    Sets ``exports`` on each job in place and returns the marked ones, in build
    order. Empty when there is no Linux build job at all, which the template
    reads as "emit no export step".
    """
    exporters = [job for job in build_jobs if job["tag_policy"] != "except"]
    for job in exporters:
        job["exports"] = True
    return exporters


def _resolve_coverage_python_version(config: BuildToml) -> str:
    """Pick the single python_version the coverage build should pin to.

    Precedence: ``[coverage].python_version`` (explicit opt-in) > highest
    entry in the resolved Linux list > the global default (``"3.13"``).
    Coverage runs a single instrumented build, so we must commit to one ABI up
    front rather than let ``_find_coverage_package`` return whichever pybind
    config happened to finish last (see issue #65).

    The fallback reads the *Linux* list rather than ``[ci].python_versions``
    because coverage only ever runs on Linux, and on GitLab the resolved
    version also selects the container image. Taking the highest Windows entry
    would pick a version with no published ``conan-gcc13-py<version>`` image --
    3.10 through 3.12 have none -- and the job would die pulling its container.

    Raises:
        ValueError: ``[coverage].python_version`` names a version outside
            :data:`SUPPORTED_PYTHON_VERSIONS`. The ``[ci]`` lists get the same
            check in :func:`generate_ci`, but coverage reads this key directly
            and so never reaches it.
    """
    explicit = config.coverage.python_version
    if explicit:
        if _unsupported_python_versions([explicit]):
            raise ValueError(
                f"build.toml [coverage].python_version names Python {explicit}, "
                f"which the conanfile's python_version option does not allow "
                f"(supported: {', '.join(SUPPORTED_PYTHON_VERSIONS)}). The [ci] "
                "version lists are checked for this, but the coverage run reads "
                "this key directly, so an unsupported value here would surface "
                "much later as a conan configure error inside the build."
            )
        return explicit
    linux_versions = _platform_python_versions(
        config.ci, "linux",
        list(config.ci.python_versions or [_DEFAULT_COVERAGE_PYTHON_VERSION]),
    )
    # str() because the fallback returns an element of the TOML list verbatim,
    # and an unquoted `linux_python_versions = [3.14]` puts a float there.
    # version_sort_key stringifies only for comparison, so the float would
    # survive into subprocess's env, which rejects non-str values outright.
    return str(max(linux_versions, key=version_sort_key))


def _coverage_context(coverage: CoverageTable, library_name: str) -> dict:
    """Build the coverage template context, applying the library-dependent default."""
    default_filters = [f"{library_name}/"]
    return {
        "cpp_threshold": coverage.cpp_threshold,
        "python_threshold": coverage.python_threshold,
        "filters": list(coverage.filters if coverage.filters is not None else default_filters),
        "excludes": list(coverage.excludes),
        # The C++ and Python halves of a coverage run are two independent
        # `conan create`s that share only the report step, so overlapping them
        # looks free. It is not, and it defaults to off.
        #
        # Two independent reasons. Conan 2's local cache is not safe for
        # concurrent writes -- two `conan create`s registering a recipe at the
        # same time hit a uniqueness constraint, which is what broke xmsvtk's
        # Coverage stage. And even with the cache serialized the legs contend
        # for CPU: an identical shard, timed by gtest itself, went from 245s to
        # 386s (1.57x) with a second leg running beside it, so the overlap
        # spends more wall clock than it saves while holding twice the runner
        # capacity for the duration.
        #
        # Set true only where neither applies -- a runner with a private cache
        # and cores to spare. An xvfb library whose image tests do not tolerate
        # a second client on the same display must leave it off regardless.
        "parallel": coverage.parallel,
    }


def _emitted_ci_jobs(ci_type: str, context: dict) -> list:
    """Names of the job blocks this generation writes, for the filter warnings."""
    if ci_type == "github":
        # [ci].windows and [ci].linux are GitLab-only -- the GitHub template has
        # no job gate for either, and generate_ci warns when a GitHub
        # build.toml sets them. Honoring them here would drop a job from this
        # list that the workflow really does emit, and a filter that empties it
        # would go unwarned. linux-arm is the one genuinely opt-in job block.
        jobs = ["mac", "linux", "windows"]
        if context["ci_linux_arm"]:
            jobs.insert(2, "linux-arm")
        return jobs
    # [ci].linux is GitLab-only -- the GitHub linux job above is not gated on it
    # -- and it takes the Linux build jobs out with it, so a filter cannot empty
    # a job that was never written.
    jobs = []
    if context["ci_linux"] and context["ci_wheel_only"]:
        # Under [matrix].wheel_only there is no single "Conan Build": the
        # template emits one job per surviving configuration, so naming the old
        # job here would point the warning at a block the pipeline does not
        # contain. One entry for the whole fan-out rather than one per job,
        # for two reasons. The pins this check is about -- os, arch, compiler
        # -- belong to the Linux runner every one of those jobs shares, so
        # enumerating would emit N identical warnings or none. And the case
        # worth warning about empties the fan-out to *zero* jobs, which is
        # exactly when a list built from it would have nothing left to warn
        # with. The settings still come from "Conan Build", which is where that
        # shared runner is registered.
        jobs = [("Linux build jobs", "Conan Build")]
    elif context["ci_linux"]:
        jobs = ["Conan Build"]
    if context["ci_windows"]:
        jobs.append("Conan Build - Windows")
    return jobs


def _warn_filter_conflicts(build_filter: dict, config: BuildToml, ci_type: str, context: dict) -> None:
    """Warn about pipelines the ``[filter]`` table leaves unbuildable.

    Emptying a job is only reported, never fixed: the platform fan-out lives in
    separate job blocks rather than a matrix axis, so unlike ``build_type``
    there is nothing for the generator to narrow.
    """
    for job_name in empty_ci_jobs(build_filter, ci_type, _emitted_ci_jobs(ci_type, context)):
        LOGGER.warning(
            "The [filter] table excludes everything %r builds, leaving it with "
            "an empty matrix. Drop the setting from [filter], or stop "
            "generating it.", job_name,
        )

    if not context["ci_coverage"]:
        return
    coverage_python_version = _resolve_coverage_python_version(config)
    for conflict in coverage_conflicts(build_filter, coverage_python_version):
        LOGGER.warning(
            "[ci].coverage is enabled but the [filter] table %s — "
            "`xmsconan coverage` will find no configurations to build.", conflict,
        )


def generate_ci(
    toml_file_path: str,
    version: str,
    output_dir: str,
    dry_run: bool = False,
):
    """
    Generate CI configuration file from build.toml.

    Args:
        toml_file_path (str): Path to the build.toml file.
        version (str): The build version.
        output_dir (str): Root directory for CI file output.
        dry_run (bool): If True, only log output files without writing them.
    """
    toml_file = Path(toml_file_path)
    output_dir = Path(output_dir)

    if not toml_file.exists():
        raise FileNotFoundError(f"The specified TOML file does not exist: {toml_file_path}")

    # Parse and validate the TOML file
    config = read_build_toml(toml_file)

    ci_type = config.ci_type
    if not ci_type:
        raise ValueError("build.toml must include a 'ci_type' key ('github' or 'gitlab')")
    if ci_type not in ("github", "gitlab"):
        raise ValueError(f"ci_type must be 'github' or 'gitlab', got '{ci_type}'")

    library_name = config.library_name
    display = _display_name(library_name)

    # A GitLab pipeline with neither platform builds nothing, and coverage runs
    # only under gcc.  Reject the impossible combinations here rather than
    # emitting a pipeline that fails opaquely in CI.  Each platform now stages
    # and deploys its own wheel -- Linux through the Package-stage Repair Wheel
    # job, Windows in place inside its build job -- so a Windows-only pipeline
    # publishes wheels and only the coverage rule below still needs Linux.
    if ci_type == "gitlab":
        if not config.ci.linux_enabled and not config.ci.windows_enabled:
            raise ValueError(
                "build.toml sets both [ci].linux and [ci].windows to false, "
                "which would generate a pipeline with nothing to build."
            )
        if config.ci.coverage and not config.ci.linux_enabled:
            raise ValueError(
                "build.toml sets [ci].coverage = true with [ci].linux = false. "
                "Coverage builds with --coverage under gcc; the generated "
                "CMakeLists rejects MSVC when XMS_COVERAGE is set."
            )
        # The msvc 192 jobs are emitted beside the msvc 194 ones and reuse
        # their shape -- the same runner, the same ABI fan-out, the same
        # export/restore split. With Windows switched off there is nothing to
        # emit them beside, and silently honoring the opt-in would resurrect
        # the Windows half of a pipeline the repository asked not to have.
        if config.ci.windows_vs2019 and not config.ci.windows_enabled:
            raise ValueError(
                "build.toml sets [ci].windows_vs2019 = true with [ci].windows = "
                "false. The VS2019 (msvc 192) jobs are an addition to the Windows "
                "jobs, not a replacement for them. Enable [ci].windows, or drop "
                "windows_vs2019."
            )
    if ci_type == "github":
        # Every wheel step in the GitHub workflow is gated on
        # `matrix.build_type == 'Release'` -- including the build step's
        # --wheel-dir -- so a library whose pybind configurations exclude
        # Release publishes nothing: the Release leg is the only leg that asks
        # for a wheel and it has no pybind configuration to get one from, so it
        # dies in build.py with "no complete set of wheels was extracted". The
        # Debug leg that could have produced one never stages it. Same class of
        # check as the two above.
        pybind_build_types = config.matrix.get("pybind_build_types")
        if pybind_build_types and "Release" not in pybind_build_types:
            raise ValueError(
                f"build.toml sets [matrix].pybind_build_types = "
                f"{list(pybind_build_types)} with ci_type = \"github\". Every wheel "
                f"step in the GitHub workflow runs only on the Release leg, so no "
                f"wheel would be published. Include \"Release\"."
            )
    # [ci].linux and [ci].windows are GitLab-only (see docs/USAGE.md, "CI
    # options"). The GitHub templates ignore both, so a project that sets
    # either here gets the full matrix and no indication its setting did
    # nothing. Documented is not the same as discoverable -- say so at
    # generation time. Warn on any explicit setting, not just false: setting
    # one to true is equally inert and equally worth knowing.
    if ci_type == "github":
        ignored = [key for key in ("linux", "windows") if getattr(config.ci, key) is not None]
        if ignored:
            LOGGER.warning(
                "build.toml sets %s, but %s GitLab-only; the generated GitHub "
                "workflow ignores %s and emits the full matrix.",
                " and ".join(f"[ci].{key}" for key in ignored),
                "these are" if len(ignored) > 1 else "this is",
                "them" if len(ignored) > 1 else "it",
            )
        # Same reasoning, separate check: windows_vs2019 is a plain bool, so
        # "explicitly set" and "true" are the same thing and it cannot join the
        # tri-state list above. Worth its own warning rather than silence --
        # a repository that opted into msvc 192 and got no msvc 192 job would
        # otherwise find out from a consumer that cannot resolve the package.
        if config.ci.windows_vs2019:
            LOGGER.warning(
                "build.toml sets [ci].windows_vs2019, but the VS2019 (msvc 192) "
                "jobs are GitLab-only; the generated GitHub workflow emits none "
                "and publishes nothing to the aquaveo-vs2019 remote."
            )

    ci_python_versions = list(config.ci.python_versions)
    ci_mac_python_versions = _platform_python_versions(config.ci, "mac", ci_python_versions)
    ci_linux_python_versions = _platform_python_versions(config.ci, "linux", ci_python_versions)

    for key, versions in (("python_versions", ci_python_versions),
                          ("mac_python_versions", ci_mac_python_versions),
                          ("linux_python_versions", ci_linux_python_versions)):
        unsupported = _unsupported_python_versions(versions)
        if unsupported:
            raise ValueError(
                f"build.toml [ci].{key} names Python {', '.join(unsupported)}, which "
                f"the conanfile's python_version option does not allow (supported: "
                f"{', '.join(SUPPORTED_PYTHON_VERSIONS)}). Generating that matrix leg "
                "would only defer the failure to conan configure time in CI."
            )

    # This guard is about the Linux fan-out, so it is moot when Linux is
    # switched off entirely -- the list is inert in that case.
    gitlab_split_tests = ci_type == "gitlab" and config.ci.split_tests
    linux_enabled = config.ci.linux_enabled
    if gitlab_split_tests and linux_enabled and len(ci_linux_python_versions) > 1:
        raise ValueError(
            "build.toml combines [ci].split_tests with more than one "
            "[ci].linux_python_versions. The GitLab C++ test job consumes the "
            "build job's artifacts by name, so a multi-ABI build would leave it "
            "testing an indeterminate one. Drop split_tests or keep "
            "linux_python_versions to a single entry."
        )

    build_filter = load_build_filter(config)
    # One measurement of the filter against the real matrix, feeding the
    # build_type axis, the wheel-step gate, and the test-job fan-out below.
    filter_effects = ci_filter_effects(build_filter, config)

    # The GitLab build stage emits one job per surviving Linux configuration
    # rather than one job looping all of them, so the legs compile in parallel
    # and each is requested in exactly the shape its consumer needs. Coverage
    # is passed through because it decides which of those jobs instrument --
    # the answer comes from the packager's own predicate, not from the
    # template guessing that "Debug means coverage".
    linux_build_jobs = ci_build_jobs(
        build_filter, config, ci_linux_python_versions,
        coverage=config.ci.coverage,
        coverage_python_version=_resolve_coverage_python_version(config),
    )

    # split_tests moves test execution out of the build job -- the build exports
    # XMS_SKIP_CXX_TESTS=1 and the Test stage runs the staged runners instead. A
    # filter that leaves no testing configuration therefore does not merely skip
    # the test jobs, it removes the only place the suite would have run, and the
    # pipeline stays green having compiled nothing to test. Fail generation
    # rather than ship that.
    if gitlab_split_tests and linux_enabled and not filter_effects["test_labels"]:
        raise ValueError(
            "build.toml sets [ci].split_tests, but its [filter] leaves no Linux "
            "testing configuration to stage a runner from, so the generated "
            "pipeline would skip the C++ suite entirely and still pass. Drop "
            "split_tests or widen the filter to keep a testing configuration."
        )

    from xmsconan import __version__ as xmsconan_version

    # Deferred: coverage_generator imports _coverage_context from this module,
    # so a module-scope import here would close a cycle. The generated job must
    # forgive exactly the code the tool exits with for a gate miss, so the
    # template takes the constant rather than repeating the number.
    from xmsconan.coverage_tools.coverage_generator import EXIT_GATE_FAILED

    # Build template context
    context = {
        "xmsconan_version": xmsconan_version,
        "library_name": library_name,
        "display_name": display,
        "version": version,
        "python_namespaced_dir": config.python_namespaced_dir,
        "ci_windows": config.ci.windows_enabled,
        # Opt-in second Windows toolchain. Not derived from windows_enabled:
        # the msvc 192 matrix resolves a different third-party stack (the
        # recipe forks boost/zlib on compiler.version) and publishes to a
        # different remote, so it is a deliberate per-repository choice rather
        # than something every Windows repository should inherit.
        "ci_windows_vs2019": config.ci.windows_vs2019,
        "vs2019_remote_name": VS2019_REMOTE_NAME,
        "vs2019_remote_url": VS2019_REMOTE_URL,
        "vs2019_platform_key": VS2019_PLATFORM_KEY,
        "vs2019_msvc_version": MSVC_VS2019_VERSION,
        # Windows-scoped on purpose: a manylinux wheel has to be repaired to be
        # installable, so there is no equivalent switch for Linux or macOS. The
        # default follows ci_type -- see repairs_windows_wheel.
        "ci_windows_wheel_repair": repairs_windows_wheel(config),
        "ci_linux": config.ci.linux_enabled,
        # Whether this repository gets the concurrent build stage. `wheel_only`
        # is the flag it is keyed to because that is the shape the restructure
        # was designed and measured against: a matrix with no library
        # configurations, where the build jobs and the coverage legs are the
        # same small set. A repository that publishes library packages keeps
        # the single looping "Conan Build" and its separate "Coverage Build"
        # until the concurrent stage has been proven against that shape too.
        "ci_wheel_only": bool(config.matrix.get("wheel_only")),
        "ci_deploy": config.ci.deploy,
        "ci_coverage": config.ci.coverage,
        "ci_xvfb": config.ci.xvfb,
        "ci_linux_arm": config.ci.linux_arm,
        "docker_image": config.ci.docker_image,
        "ci_split_tests": config.ci.split_tests,
        "ci_test_shards": config.ci.test_shards,
        "ci_python_versions": ci_python_versions,
        "ci_mac_python_versions": ci_mac_python_versions,
        "ci_linux_python_versions": ci_linux_python_versions,
        "ci_mac_py_suffix": _py_suffix(ci_mac_python_versions),
        "ci_wheel_dir_flag": RELEASE_ONLY_WHEEL_DIR,
        "gitlab_linux_fanout": len(ci_linux_python_versions) > 1,
        "gitlab_linux_image_py": (
            "${PYTHON_TARGET_VERSION}" if len(ci_linux_python_versions) > 1
            else ci_linux_python_versions[0]
        ),
        "gitlab_linux_single_py": max(ci_linux_python_versions, key=version_sort_key),
        "gitlab_linux_py_suffix": (
            "-py${PYTHON_TARGET_VERSION}" if len(ci_linux_python_versions) > 1 else ""
        ),
        "ci_linux_py_suffix": _py_suffix(ci_linux_python_versions),
        "ci_linux_name_py": _job_name_py(ci_linux_python_versions),
        "coverage": _coverage_context(config.coverage, library_name),
        "coverage_gate_exit_code": EXIT_GATE_FAILED,
        "coverage_python_version": _resolve_coverage_python_version(config),
        "ci_build_types": filter_effects["build_types"],
        # One GitLab test job per staged testing configuration, each naming its
        # own artifact directory. Computed here rather than spelled
        # "<build_type>-testing" in the template: the label format is
        # config_label's, and a copy of it in Jinja is free to drift from what
        # the build writes.
        "ci_test_labels": filter_effects["test_labels"],
        "ci_linux_build_jobs": linux_build_jobs,
        "ci_instrumented_build_jobs": [
            job for job in linux_build_jobs if job["instrumented"]
        ],
        # Only the uninstrumented testing configurations get a separate test
        # job. An instrumented one runs its own suite inside the build, beside
        # the .gcno it has to be read against, so splitting it out would mean
        # moving the build folder to reach the data.
        "ci_linux_test_jobs": [
            job for job in linux_build_jobs
            if job["kind"] == "testing" and not job["instrumented"]
        ],
        # Also sets `exports` on the job dicts themselves, which is what the
        # build-job loop reads to decide whether to emit the save step. The two
        # context entries hold the same dict objects, so the marking is visible
        # through ci_linux_build_jobs no matter which entry is evaluated first.
        "ci_linux_export_jobs": _mark_export_jobs(linux_build_jobs),
        # Every job that can produce a wheel. There is more than one because
        # the pybind configuration is emitted twice -- instrumented on a
        # branch, clean on a tag. Only the clean one writes a wheel, which is
        # why Repair Wheel is tag-gated; the list still names both so the
        # `optional: true` need has a name to be optional about.
        "ci_linux_wheel_jobs": [
            job for job in linux_build_jobs if job["kind"] == "pybind"
        ],
        # Per platform: a filter can leave one platform building wheels and
        # another not (see ci_filter_effects). linux-arm reads the Linux flag.
        "ci_wheel_enabled_mac": filter_effects["wheel_enabled"]["mac"],
        "ci_wheel_enabled_linux": filter_effects["wheel_enabled"]["linux"],
        "ci_wheel_enabled_windows": filter_effects["wheel_enabled"]["windows"],
    }

    if build_filter:
        _warn_filter_conflicts(build_filter, config, ci_type, context)

    # Select templates and output paths
    template_dir = Path(__file__).parent / "ci_templates"
    if ci_type == "github":
        renders = [(template_dir / "github-ci.yaml.jinja",
                    output_dir / ".github" / "workflows" / f"{display}-CI.yaml")]
        if context["ci_coverage"]:
            renders.append((template_dir / "github-coverage.yaml.jinja",
                            output_dir / ".github" / "workflows" / "Coverage.yaml"))
    else:
        renders = [(template_dir / "gitlab-ci.yml.jinja", output_dir / ".gitlab-ci.yml")]

    for template_file, _ in renders:
        if not template_file.exists():
            raise FileNotFoundError(f"CI template not found: {template_file}")

    # Use custom delimiters to avoid conflicts with GitHub Actions ${{ }}
    env = Environment(
        variable_start_string="<<",
        variable_end_string=">>",
        block_start_string="<%",
        block_end_string="%>",
        comment_start_string="<#",
        comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        newline_sequence="\n",
        undefined=StrictUndefined,
    )

    for template_file, output_path in renders:
        template_content = template_file.read_text(encoding="utf-8")
        template = env.from_string(template_content)
        rendered = template.render(context)

        if dry_run:
            LOGGER.info("[DRY-RUN] Would write CI file: %s", output_path)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _write_text_lf(output_path, rendered)
            LOGGER.info("Generated CI file: %s", output_path)


def main():
    """Main function to parse arguments and generate CI configuration."""
    parser = argparse.ArgumentParser(description="Generate CI configuration from build.toml.")
    parser.add_argument(
        "--output_dir",
        default=".",
        help="Root directory for CI file output. Defaults to current directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show files that would be generated without writing them.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase output verbosity (use -v for debug details).",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")
    parser.add_argument(
        "--version", default=None,
        help="The build version. If omitted, tries setuptools-scm then falls back to 0.0.0.",
    )
    parser.add_argument("toml_file", nargs="?", default="build.toml",
                        help="Path to the build.toml file. Defaults to build.toml in the current directory.")

    args = parser.parse_args()
    _configure_logging(args)

    from xmsconan.generator_tools.version import resolve_version
    version = resolve_version(args.version)

    try:
        generate_ci(
            toml_file_path=args.toml_file,
            version=version,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
