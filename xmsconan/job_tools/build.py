"""``xmsconan job build`` -- one CI build leg, driven in this process.

What a generated build job used to be: an ``export`` of the version, a
``conan_setup``, a ``gen``, a ``build.py`` whose six flags the template derived
from ``build.toml`` and whose matrix leg was a JSON literal interpolated into a
quoted shell argument, and a ``conan_deploy --save`` whose tarball name was
assembled in Jinja. Every one of those is a decision this package can make from
``build.toml`` and the job's own environment, and making them here is what
lets the template's ``script:`` be an install line and this call.

The packager is constructed directly rather than through the generated
``build.py``: that file is regenerated two steps earlier in this same process,
and importing it would make the CI path depend on a generated file being
current -- the one thing the regeneration exists to stop mattering.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any, Callable, Optional

from xmsconan.build_toml import read_build_toml
from xmsconan.ci_options import repairs_wheel
from xmsconan.ci_tools.conan_deploy import conan_deploy as _conan_deploy
from xmsconan.ci_tools.conan_setup import conan_setup as _conan_setup
from xmsconan.ci_tools.wheel_repair import wheel_repair as _wheel_repair
from xmsconan.constants import VS2019_PLATFORM_KEY, VS2019_REMOTE_NAME, VS2019_REMOTE_URL
from xmsconan.exit_codes import EXIT_ERROR, EXIT_OK
from xmsconan.generator_tools.build_file_generator import generate_build_files
from xmsconan.generator_tools.version import is_release_version, resolve_version
from xmsconan.job_tools import common, xvfb
from xmsconan.package_tools import packager

#: ``sys.platform`` prefix -> the segment naming it in an export tarball. The
#: names are the ones the GitLab template rendered, so a pipeline's tarballs
#: keep the names its deploy job restores.
_PLATFORM_SEGMENTS = (("linux", "linux"), ("win32", "windows"), ("darwin", "macos"))


def _platform_segment(platform_key=None, platform=None):
    """The platform part of an export tarball's name."""
    if platform_key == VS2019_PLATFORM_KEY:
        return "windows-vs2019"
    platform = sys.platform if platform is None else platform
    for prefix, segment in _PLATFORM_SEGMENTS:
        if platform.startswith(prefix):
            return segment
    return platform


def export_tarball_name(library_name, version, configurations, leg=None,
                        platform_key=None, platform=None, environ=None):
    """Name the Conan cache tarball this job hands to the deploy.

    ``<library>-<platform>-<discriminator>-<version>.tar.gz``. Every exporting
    job in a pipeline writes into one artifact space, so the discriminator's
    only job is to keep two of them apart:

    * A job that named a leg builds one configuration, and its
      :func:`~xmsconan.package_tools.packager.config_label` is what
      distinguishes it -- two legs can share a build type (a library and a
      testing configuration both build Release) and an ABI is not a
      distinguisher at all for a library configuration, which has none.
    * A job that builds a platform's whole matrix is one of a ``parallel:
      matrix`` fan-out over ABIs, so ``py<version>`` is what separates the
      instances.

    Deliberately keyed on whether a leg was named rather than on how many
    configurations survived: the second would rename a job's tarball when a
    ``[filter]`` change happened to leave its matrix with one entry.
    """
    environ = os.environ if environ is None else environ
    if leg is not None and len(configurations) == 1:
        discriminator = packager.config_label(configurations[0])
    else:
        python_version = environ.get(common.PYTHON_TARGET_VARIABLE)
        discriminator = f"py{python_version}" if python_version else None
    segment = _platform_segment(platform_key, platform)
    parts = [library_name, segment, discriminator, version]
    return f"{'-'.join(part for part in parts if part)}.tar.gz"


def export_package_query(configurations, platform=None):
    """The ``--package-query`` restricting this job's save to what it built.

    Windows only, and not decoration. A runner's Conan cache is per machine
    rather than per job, and ``conan cache save <ref>:*`` matches by reference
    -- so on a fleet where the msvc 194 and msvc 192 jobs build the same
    reference at the same time, an unqueried save tarballs whichever binaries
    are in the cache and the deploy publishes them to a remote whose whole
    purpose is to keep the two toolchains apart.

    Linux and macOS get None because each of their jobs owns its container and
    therefore its cache; there is nothing else in it to exclude.

    Returns:
        ``"compiler.version=<v>"``, or None on a platform that does not need one.

    Raises:
        ValueError: A Windows build whose configurations do not agree on a
            single ``compiler.version``. Saving unqueried is the failure this
            query exists to prevent, so there is nothing safe to fall back to
            -- :func:`~xmsconan.package_tools.packager.only_msvc_version`,
            which the deploy's query and the generated template both read,
            raises on the same condition one layer up.
    """
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return None
    versions = {configuration.get("compiler.version") for configuration in configurations}
    versions.discard(None)
    if len(versions) != 1:
        raise ValueError(
            f"cannot restrict the Conan save: this build's {len(configurations)} "
            f"configurations name {len(versions)} compiler.version values "
            f"({', '.join(sorted(versions)) or 'none'}); an unqueried save would "
            f"tarball whatever else is in this runner's cache."
        )
    return f"compiler.version={versions.pop()}"


def _make_packager(config, toml_path, build_missing, platform_key, test_shards=0):
    """Build the packager ``build.py`` would have built, from build.toml.

    ``LIBRARY_NAME``, ``CONAN_PROFILE_OPTIONS`` and ``CONAN_MATRIX`` in the
    generated ``conanfile.py`` are rendered straight from these same three
    fields, so reading them here reaches the same values without importing a
    generated module.

    *test_shards* above 1 makes the recipe skip ``cmake.test()`` and the
    packager run the staged runner as that many in-process gtest shards once
    the build is done. It defaults to 0 -- no sharding -- because a caller
    that has not thought about whether another job runs these tests should
    not silently run them twice; :func:`job_build` is what decides.
    """
    # The msvc 192 matrix resolves the legacy third-party stack (the recipe
    # forks boost/zlib on compiler.version), and the boost defaults the
    # packager would otherwise inject name conan-center boost 1.86 options
    # that boost/1.74.0.3 does not declare -- Conan fails a build outright
    # when a profile sets an option no recipe in the graph defines. Derived
    # from the platform rather than exposed as a second flag, for the reason
    # build.py derives it: one input, no way to set the matrix and the
    # toolchain assumptions to disagree.
    apply_boost_defaults = platform_key != VS2019_PLATFORM_KEY
    return packager.XmsConanPackager(
        config.library_name,
        str(Path(toml_path).resolve().parent / "conanfile.py"),
        build_missing=build_missing,
        artifacts_dir=common.ARTIFACTS_DIR,
        profile_options=config.conan_profile_options,
        matrix=config.matrix,
        apply_boost_defaults=apply_boost_defaults,
        test_shards=test_shards,
    )


@dataclass
class BuildSteps:
    """Callable steps used by :func:`job_build`.

    Each field defaults to the production implementation; tests supply fakes
    that record the sequence, which is the property worth asserting here --
    the order the six steps run in is what the template used to spell out.

    The fields default to None and are resolved in ``__post_init__`` rather
    than defaulting to the functions themselves: a dataclass default is bound
    when the class is created, which would freeze the module globals these
    name against any later patch of them. No test in this module patches one
    today -- they all pass fakes in -- but :class:`~xmsconan.ci_tools.publish.
    PublishSteps` carries the same resolution for tests that do, and having
    the two classes answer the question differently is a trap for whoever
    writes the first such test here.
    """

    print_versions: Optional[Callable[..., Any]] = None
    conan_setup: Optional[Callable[..., Any]] = None
    generate: Optional[Callable[..., Any]] = None
    make_packager: Optional[Callable[..., Any]] = None
    display: Optional[Callable[..., Any]] = None
    wheel_repair: Optional[Callable[..., Any]] = None
    conan_deploy: Optional[Callable[..., Any]] = None

    def __post_init__(self):  # noqa: D105
        if self.print_versions is None:
            self.print_versions = common.print_tool_versions
        if self.conan_setup is None:
            self.conan_setup = _conan_setup
        if self.generate is None:
            self.generate = generate_build_files
        if self.make_packager is None:
            self.make_packager = _make_packager
        if self.display is None:
            self.display = xvfb.display
        if self.wheel_repair is None:
            self.wheel_repair = _wheel_repair
        if self.conan_deploy is None:
            self.conan_deploy = _conan_deploy


def job_build(leg=None, platform=None, version=None, toml_path="build.toml",
              export=False, release_skips_testing=False, build_missing=False,
              defer_cxx_tests=False, steps=None, environ=None):
    """Run one CI build leg.

    Args:
        leg: Which configuration kind to build -- one of
            :data:`~xmsconan.job_tools.common.LEG_KINDS` -- or None for every
            configuration this platform's matrix produces.
        platform: Matrix key to build (``windows_vs2019``); None detects it
            from the running machine.
        version: Explicit version, or None to resolve it the way every command
            does: the CI tag, then the untagged-CI fallback, then
            setuptools-scm.
        toml_path: Path to build.toml.
        export: Save a Conan cache tarball under ``.export/`` for the deploy
            job to restore. Passed by the jobs that a tag pipeline publishes
            from; a branch pipeline's tarball would never be restored.
        release_skips_testing: On a release version, drop the testing
            configurations. In a pipeline that is the tag jobs and only those,
            because an untagged pipeline resolves the ``0.0.0`` fallback. See
            :func:`~xmsconan.job_tools.common.resolve_leg`.
        build_missing: Build missing dependencies from source. Implied by the
            VS2019 platform, whose legacy dependency graph is not prebuilt.
        defer_cxx_tests: A separate job in this pipeline runs the C++ suite
            this build compiles, so it must not also run inline here, and
            ``[ci].test_shards`` is that job's business rather than this
            one's. Passed by the generator, which is the only layer that
            knows the job graph; inert unless ``[ci].split_tests`` is on.
        steps: :class:`BuildSteps` instance (production defaults if omitted).
        environ: The environment to read and set; ``os.environ`` when None.

    Returns:
        An exit code from :mod:`xmsconan.exit_codes`.
    """
    steps = steps or BuildSteps()
    environ = os.environ if environ is None else environ

    steps.print_versions()

    version = resolve_version(version, environ=environ)
    config = read_build_toml(toml_path)
    build_missing = build_missing or platform == VS2019_PLATFORM_KEY
    defaulted = common.set_job_environment(
        config, defer_cxx_tests=defer_cxx_tests, environ=environ)
    if defaulted:
        # Named in the log because they change what the build does and nothing
        # else in the job's output says they were this command's doing.
        print(f"Defaulted for this job: {', '.join(defaulted)}.")

    with common.log_section("Conan setup", environ=environ):
        # No login: a generated job has never run one. Conan reads
        # CONAN_LOGIN_USERNAME and CONAN_PASSWORD from the environment itself,
        # which is where a CI secret belongs, and `conan remote login` with no
        # credentials to hand it prompts -- on a runner, that is a job that
        # hangs rather than one that says what is missing.
        steps.conan_setup(login=False)
        if platform == VS2019_PLATFORM_KEY:
            # Appended, not inserted first: it must not become the first stop
            # for every `conan install` on a shared runner. It is where the
            # legacy dependencies resolve from as well as where the results go.
            steps.conan_setup(remote_name=VS2019_REMOTE_NAME,
                              remote_url=VS2019_REMOTE_URL, index=None, login=False)

    with common.log_section("Generate build files", environ=environ):
        generated = steps.generate(toml_file_path=toml_path, version=version)
        if generated:
            return generated

    # [ci].test_shards reaches the packager only when this job runs the suite
    # it compiles. Deferred, `job test` runs those shards in another job from
    # the staged runner, and asking for them here as well would run the whole
    # suite twice on two runners. This is the GitHub path: it generates no
    # separate test job, so the sharding its workflow used to render onto
    # `build.py --test-shards` has to be decided here or not at all.
    test_shards = 0 if common.defers_cxx_tests(config, defer_cxx_tests) \
        else config.ci.test_shards
    builder = steps.make_packager(config, toml_path, build_missing, platform, test_shards)
    builder.generate_configurations(system_platform=platform)
    if config.filter:
        print(f"Applying build.toml [filter]: {config.filter}")
        builder.filter_configurations(config.filter)
    leg_filter = common.resolve_leg(
        leg=leg,
        release=is_release_version(version),
        release_skips_testing=release_skips_testing,
        environ=environ,
    )
    if leg_filter:
        print(f"Applying leg filter: {leg_filter}")
        builder.filter_configurations(leg_filter)

    if not builder.configurations:
        # Filters that cancel each other out used to build nothing and exit 0,
        # which reads as a passing build that produced no packages.
        print(
            "No configurations match this job. Applied: "
            f"build.toml [filter]={config.filter or None}, leg={leg!r}, "
            f"filter={leg_filter or None}."
        )
        return EXIT_ERROR

    with common.log_section(f"Build ({len(builder.configurations)} configurations)",
                            environ=environ):
        with steps.display(config, log_dir=common.ARTIFACTS_DIR, environ=environ):
            if builder.run() > 0:
                print("errors when building... exiting...")
                return EXIT_ERROR

    configurations = list(builder.configurations)
    wheel_result = _stage_wheel(builder, config, configurations, version, platform,
                                environ=environ)
    if wheel_result != EXIT_OK:
        return wheel_result
    _repair_wheel(config, configurations, platform, steps, environ=environ)

    if export:
        with common.log_section("Export Conan packages", environ=environ):
            steps.conan_deploy(
                config.library_name, version,
                save=os.path.join(common.EXPORT_DIR, export_tarball_name(
                    config.library_name, version, configurations, leg=leg,
                    platform_key=platform, environ=environ,
                )),
                package_query=export_package_query(configurations),
            )
    return EXIT_OK


def _recipe_builds_wheel(configuration):
    """Whether the recipe leaves a wheel in this configuration's package.

    A pybind configuration does, except on Windows Debug, and that exception is
    the recipe's: for a library that advertises its module the Windows Debug
    build is ``_<name>_d.<abi>.pyd``, which the shipped Python cannot import
    under the name a wheel installs it as, so ``XmsConan2File.build()`` skips
    the wheel and the Python tests there (USAGE section 7.5). Only a library
    naming ``Debug`` in ``[matrix].pybind_build_types`` has such a
    configuration at all.

    Read off the configuration's own ``os``, which is what the recipe reads
    (``str(self.settings.os) == 'Windows'``) and what the packager puts on
    every combination it generates. The running interpreter's platform would
    agree today and is a different question -- whether *this machine* is
    Windows is what decides where delvewheel can run (:func:`_repair_wheel`),
    not what the recipe built.

    Restated here rather than shared: the recipe base is copied into each
    library's checkout as a generated file and imports nothing from this
    package. The GitHub template used to stand in for it, asking for a wheel
    only on the Release leg -- so this is where that knowledge went when the
    template stopped deciding, and asking for a wheel the recipe did not build
    is what :func:`_stage_wheel` fails the job on.
    """
    if not configuration.get("options", {}).get("pybind"):
        return False
    windows_debug = configuration.get("os") == "Windows" and configuration.get("build_type") == "Debug"
    return not windows_debug


def _builds_wheel(configurations, platform_key):
    """Whether this job produced a wheel worth staging.

    Read off the configurations that survived the filters rather than from a
    flag: a wheel exists exactly when a pybind configuration was built and the
    recipe built its wheel, which is the same question the CI generator
    answers per platform when it decides whether to emit the wheel steps --
    asked here per job, against the configurations actually built.

    The VS2019 matrix is the one exception, and it is a publishing rule rather
    than a build one: a wheel's tags (``cp310-cp310-win_amd64``) say nothing
    about which MSVC built it, so an msvc 192 wheel and an msvc 194 wheel are
    the same filename on the index and would overwrite each other by upload
    order.
    """
    if platform_key == VS2019_PLATFORM_KEY:
        return False
    return any(_recipe_builds_wheel(configuration) for configuration in configurations)


def _stage_wheel(builder, config, configurations, version, platform_key, environ=None):
    """Extract this leg's wheel and stage the libraries its repair will need.

    The staging half runs on every platform that built a wheel; only the
    repair that consumes it is Windows-only, and that is :func:`_repair_wheel`.
    """
    if not _builds_wheel(configurations, platform_key):
        # Named for the reason the empty-matrix exit above is: a job that was
        # expected to produce a wheel and produced none otherwise says nothing at
        # all, and an absent "Stage wheel" section reads the same in the log as a
        # leg that never had a wheel to stage.
        reason = (f"the {VS2019_PLATFORM_KEY} matrix publishes none"
                  if platform_key == VS2019_PLATFORM_KEY else
                  "no configuration it built leaves one in its package")
        print(f"No wheel to stage from this job's {len(configurations)} "
              f"configuration(s): {reason}.")
        return EXIT_OK

    with common.log_section("Stage wheel", environ=environ):
        # extract_wheel returns False both when nothing was extracted and when
        # only some of the expected python_versions were -- and in the partial
        # case it has already copied the wheels it did find. A job whose wheel
        # is the artifact it exists to produce cannot treat that as a warning.
        if not builder.extract_wheel(common.WHEEL_DIR, version=version):
            print(f"error: no complete set of wheels was extracted into "
                  f"{common.WHEEL_DIR}; see the message above.")
            return EXIT_ERROR
        # The staged libraries exist only so the repair tools can resolve
        # imports, so collecting them is pure cost once repair is off.
        if repairs_wheel(config, platform=sys.platform):
            builder.collect_dependency_libs(os.path.join(common.WHEEL_DIR, "libs"))
    return EXIT_OK


def _repair_wheel(config, configurations, platform_key, steps, environ=None, platform=None):
    """Repair this job's wheel in place, on the one platform that can.

    Windows repairs in place; Linux does not. delvewheel resolves the DLL
    imports of a win_amd64 .pyd and can only run on a Windows host, so a
    manylinux container cannot stand in and a second WinVM allocation would be
    the alternative. The Linux wheel is repaired by ``job package``, in the
    manylinux image, because auditwheel needs that image's glibc.

    *platform* is threaded in rather than read here and again inside
    :func:`~xmsconan.ci_options.repairs_wheel`: one value has to answer both,
    or a caller can reach a repair whose staged libraries were skipped.
    """
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not _builds_wheel(configurations, platform_key):
        return
    if not repairs_wheel(config, platform=platform):
        return
    with common.log_section("Repair wheel", environ=environ):
        steps.wheel_repair(wheel_dir=common.WHEEL_DIR, platform="windows")
