"""Save, restore, or upload Conan packages in CI.

Usage::

    xmsconan_conan_deploy <library> <version> --save FILE
    xmsconan_conan_deploy <library> <version> --restore FILE [--upload]
    xmsconan_conan_deploy <library> <version> --upload
"""
import argparse
import subprocess
import sys


def conan_deploy(library, version, save=None, restore=None, upload=False):
    """Perform Conan cache save, restore, or upload operations.

    Args:
        library: Library name (e.g. ``xmscore``).
        version: Package version string.
        save: Path to write the cache tarball to (``conan cache save``).
        restore: Path to read a cache tarball from (``conan cache restore``).
        upload: If ``True``, upload the package to the ``aquaveo`` remote.
    """
    ref = f"{library}/{version}"

    if save:
        subprocess.run(
            ["conan", "cache", "save", "--file", save, ref],
            check=True,
        )

    if restore:
        subprocess.run(
            ["conan", "cache", "restore", restore],
            check=True,
        )

    if upload:
        subprocess.run(
            ["conan", "upload", ref, "-r", "aquaveo", "--confirm"],
            check=True,
        )


def main():
    """CLI entry point for ``xmsconan_conan_deploy``."""
    parser = argparse.ArgumentParser(
        description="Save, restore, or upload Conan packages in CI.",
    )
    parser.add_argument("library", help="Library name (e.g. xmscore).")
    parser.add_argument("version", help="Package version string.")
    parser.add_argument(
        "--save",
        default=None,
        help="Save the Conan cache to this tarball path.",
    )
    parser.add_argument(
        "--restore",
        default=None,
        help="Restore the Conan cache from this tarball path.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the package to the aquaveo remote.",
    )
    args = parser.parse_args()

    if not args.save and not args.restore and not args.upload:
        parser.error("At least one of --save, --restore, or --upload is required.")

    try:
        conan_deploy(
            library=args.library,
            version=args.version,
            save=args.save,
            restore=args.restore,
            upload=args.upload,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
