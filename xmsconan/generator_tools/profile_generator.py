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
import argparse
import logging
import os
import sys

import toml

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from xmsconan.package_tools.packager import XmsConanPackager

LOGGER = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "conan_profiles"


def _load_toml(toml_file_path):
    """Load a TOML file, preferring the stdlib parser when available."""
    if tomllib:
        with open(toml_file_path, "rb") as toml_file:
            return tomllib.load(toml_file)
    with open(toml_file_path, "r", encoding="utf-8") as toml_file:
        return toml.load(toml_file)


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
    data = _load_toml(toml_file_path)
    library_name = data.get("library_name")
    if not library_name:
        raise ValueError(f"{toml_file_path} does not define library_name")

    base_dir = os.path.dirname(os.path.abspath(toml_file_path))
    profiles_dir = output_dir if os.path.isabs(output_dir) else os.path.join(base_dir, output_dir)

    packager = XmsConanPackager(
        library_name,
        conanfile_path=base_dir,
        profile_options=data.get("conan_profile_options") or None,
        profile_conf=data.get("conan_profile_conf"),
        profile_variants=data.get("conan_profile_variants"),
    )

    if dry_run:
        # Reuse the same plan write_profiles executes, so a dry run cannot
        # disagree with a real run.
        paths = [os.path.join(profiles_dir, filename)
                 for filename, _configuration, _conf in packager.plan_profiles(system_platform)]
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
    parser.add_argument("--platform", default=None, dest="system_platform",
                        help="Platform matrix key ('windows', 'linux', 'darwin'). Defaults to auto-detect.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show files that would be generated without writing them.")
    parser.add_argument("--no-presets", action="store_true",
                        help="Skip CMakePresets.json. Presets are generated by default.")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase output verbosity.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")

    args = parser.parse_args()
    _configure_logging(args)

    try:
        generate_profiles(
            toml_file_path=args.toml_file,
            output_dir=args.output_dir,
            system_platform=args.system_platform,
            dry_run=args.dry_run,
            write_presets=not args.no_presets,
        )
    except Exception as exc:  # surfaced to the user, not swallowed
        LOGGER.error("Profile generation failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
