"""Generate Conan profiles for a repository from its build.toml.

The build configuration that ``build.py`` uses lives in Python
(:mod:`xmsconan.package_tools.packager`) and is only applied when a build goes
through ``build.py``. Every other entry point -- a bare ``conan install``, a
``conan editable`` consumer, an IDE, a freshly created worktree -- falls back to
whatever ``conan profile detect`` produced, which computes different package ids
and reports missing binaries for dependencies that are in fact available.

This module writes that same configuration out as ordinary Conan profiles so
those entry points can use it. The profiles are generated artifacts, regenerated
from ``build.toml`` alongside CMakeLists.txt and conanfile.py -- not local state,
and not hand-edited.
"""
# 1. Standard python modules
import argparse
import json
import logging
import os
import sys

# 3. Aquaveo modules
from xmsconan.build_toml import read_build_toml
from xmsconan.generator_tools.output_plan import check_plan
from xmsconan.package_tools.packager import configurations, XmsConanPackager

LOGGER = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "conan_profiles"


def _remove_stale_profiles(profiles_dir: str, written: list[str]) -> list[str]:
    """Delete profiles in ``profiles_dir`` this run did not write.

    Profiles are generated artifacts, so the directory has to *match* the
    matrix rather than accumulate every matrix a repo ever had. Trimming
    ``[matrix]`` otherwise left the dropped configurations' profiles on disk,
    indistinguishable from the fresh ones, while ``CMakePresets.json`` -- a
    single rewritten file -- lost them. A developer or IDE picking a stale
    profile computes a package id nothing was published for and gets missing-
    binary errors that look like a remote problem.

    Only ``.txt`` files are considered, so anything else a user keeps in that
    directory is left alone.

    Args:
        profiles_dir: Directory the profiles were written to.
        written: Absolute paths this run wrote.

    Returns:
        The paths removed, sorted.
    """
    removed = _stale_profiles(profiles_dir, written)
    for path in removed:
        os.remove(path)
    return removed


def _stale_profiles(profiles_dir: str, written: list[str]) -> list[str]:
    """Profiles in ``profiles_dir`` that this run's plan does not produce.

    The finding half of :func:`_remove_stale_profiles`, split out so that
    ``--check`` reports exactly the files a real run would delete rather than
    re-deriving the rule and drifting from it.

    Args:
        profiles_dir: Directory the profiles are written to.
        written: Paths this run's plan produces.

    Returns:
        The stale paths, sorted.
    """
    if not os.path.isdir(profiles_dir):
        return []
    keep = {os.path.normcase(os.path.abspath(path)) for path in written}
    stale = []
    for name in os.listdir(profiles_dir):
        if not name.endswith(".txt"):
            continue
        path = os.path.join(profiles_dir, name)
        if os.path.normcase(os.path.abspath(path)) not in keep:
            stale.append(path)
    return sorted(stale)


def _packager_for(toml_file_path, output_dir):
    """Build the packager the profiles and presets are derived from.

    Shared by :func:`generate_profiles` and :func:`plan_profile_files` so
    that what ``--check`` compares against is produced by the same object a
    real run writes from, rather than by a second reading of build.toml free
    to resolve the matrix differently.

    Returns:
        ``(packager, profiles_dir, base_dir)``.
    """
    config = read_build_toml(toml_file_path)
    library_name = config.library_name

    base_dir = os.path.dirname(os.path.abspath(toml_file_path))
    profiles_dir = output_dir if os.path.isabs(output_dir) else os.path.join(base_dir, output_dir)

    packager = XmsConanPackager(
        library_name,
        conanfile_path=base_dir,
        profile_options=config.conan_profile_options or None,
        profile_conf=config.conan_profile_conf,
        profile_variants=config.conan_profile_variants,
        # Profiles and presets describe the configurations that get built, so
        # they have to read the same [matrix] table build.py does. Otherwise a
        # library that trims its matrix still gets profiles and IDE presets for
        # configurations nothing builds, and one that adds Debug+pybind gets no
        # profile for the configuration it just asked for.
        matrix=config.matrix,
    )
    # The [filter] table is deliberately *not* applied here, though it also
    # narrows what build.py builds. The two tables say different things: [matrix]
    # declares which configurations this library *has*, while [filter] is a
    # baseline restriction on what CI builds by default, with a documented
    # per-invocation escape hatch (`build.py --ignore-build-filter`). These
    # profiles and CMakePresets.json are what an IDE and a hand-run cmake
    # configure from, which is exactly when a developer reaches for that hatch --
    # so a filtered-out configuration still gets a profile to configure with.
    # (build.py itself never reads them; XmsConanPackager.run writes a throwaway
    # profile per configuration via create_build_profile.)

    return packager, profiles_dir, base_dir


def plan_profile_files(toml_file_path="build.toml", output_dir=DEFAULT_OUTPUT_DIR,
                       system_platform=None, write_presets=True):
    """Render the profiles and presets without writing them.

    Args:
        toml_file_path: Path to the repository's build.toml.
        output_dir: Directory the profiles would go in, relative to the TOML file.
        system_platform: Override the platform matrix key. None auto-detects.
        write_presets: Include CMakePresets.json in the plan.

    Returns:
        ``(plan, stale)`` -- a mapping of path to content, and the profile
        paths a real run would delete. The stale half matters as much as the
        plan: a dropped ``[matrix]`` entry leaves a profile behind that an
        IDE will happily configure from, and a check that only compared the
        files it renders would report that tree as up to date.
    """
    packager, profiles_dir, base_dir = _packager_for(toml_file_path, output_dir)

    plan = packager.render_profiles(profiles_dir, system_platform)

    if write_presets:
        document = packager.plan_cmake_presets(system_platform)
        if document["configurePresets"]:
            plan[os.path.join(base_dir, "CMakePresets.json")] = json.dumps(document, indent=2) + "\n"

    return plan, _stale_profiles(profiles_dir, list(plan))


def generate_profiles(toml_file_path="build.toml", output_dir=DEFAULT_OUTPUT_DIR,
                      system_platform=None, dry_run=False, write_presets=True):
    """Write one Conan profile per build configuration.

    Args:
        toml_file_path: Path to the repository's build.toml.
        output_dir: Directory to write profiles into, relative to the TOML file.
        system_platform: Override the platform matrix key ('windows', 'linux',
            'darwin'). None auto-detects.
        dry_run: Report what would be written without writing it.
        write_presets: Also write CMakePresets.json next to build.toml. The
            presets name the same generator and build folder as the profiles,
            derived from the same plan, so the two cannot disagree.

    Returns:
        List of paths (that would be) written.
    """
    packager, profiles_dir, base_dir = _packager_for(toml_file_path, output_dir)

    if dry_run:
        # Reuse the same plan write_profiles executes, so a dry run cannot
        # disagree with a real run.
        paths = [os.path.join(profiles_dir, entry.filename)
                 for entry in packager.plan_profiles(system_platform)]
        for path in sorted(paths):
            LOGGER.info("[DRY-RUN] Would write profile: %s", path)
        if write_presets and packager.plan_cmake_presets(system_platform)["configurePresets"]:
            presets_path = os.path.join(base_dir, "CMakePresets.json")
            LOGGER.info("[DRY-RUN] Would write presets: %s", presets_path)
            paths.append(presets_path)
        return sorted(paths)

    written = packager.write_profiles(profiles_dir, system_platform)
    for path in written:
        LOGGER.info("Wrote profile: %s", path)
    for path in _remove_stale_profiles(profiles_dir, written):
        LOGGER.info("Removed stale profile: %s", path)
    LOGGER.info("Generated %d profile(s) in %s", len(written), profiles_dir)

    if write_presets:
        presets_path = packager.write_cmake_presets(os.path.join(base_dir, "CMakePresets.json"),
                                                    system_platform)
        if presets_path:
            LOGGER.info("Wrote presets: %s", presets_path)
            written.append(presets_path)
        else:
            LOGGER.info("No generator pinned in any profile; skipped CMakePresets.json")

    return written


def _configure_logging(args):
    """Configure logging to match the other generator entry points."""
    if args.quiet:
        level = logging.ERROR
    elif args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def main():
    """Entry point for ``xmsconan_profiles``."""
    parser = argparse.ArgumentParser(description="Generate Conan profiles from a build.toml.")
    parser.add_argument("toml_file", nargs="?", default="build.toml",
                        help="Path to the TOML file. Defaults to build.toml in the current directory.")
    parser.add_argument("--output_dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"Directory to write profiles into. Defaults to {DEFAULT_OUTPUT_DIR}.")
    platform_keys = ", ".join(f"'{key}'" for key in sorted(configurations))
    parser.add_argument("--platform", default=None, dest="system_platform",
                        help=f"Platform matrix key ({platform_keys}). Defaults to auto-detect.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show files that would be generated without writing them.")
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 with a diff if any profile is out of date. Writes nothing.")
    parser.add_argument("--no-presets", action="store_true",
                        help="Skip CMakePresets.json. Presets are generated by default.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase output verbosity.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")

    args = parser.parse_args()
    _configure_logging(args)

    try:
        if args.check:
            plan, stale = plan_profile_files(
                toml_file_path=args.toml_file,
                output_dir=args.output_dir,
                system_platform=args.system_platform,
                write_presets=not args.no_presets,
            )
            sys.exit(check_plan(plan, stale=stale))
        generate_profiles(
            toml_file_path=args.toml_file,
            output_dir=args.output_dir,
            system_platform=args.system_platform,
            dry_run=args.dry_run,
            write_presets=not args.no_presets,
        )
    except Exception as exc:  # surfaced to the user, not swallowed
        LOGGER.error("Profile generation failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
