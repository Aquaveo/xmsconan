"""Save, restore, or upload Conan packages in CI.

Usage::

    xmsconan_conan_deploy <library> <version> --save FILE
    xmsconan_conan_deploy <library> <version> --restore FILE [--upload]
    xmsconan_conan_deploy <library> <version> --upload
    xmsconan_conan_deploy <library> <version> --restore FILE --upload \
        --remote aquaveo-vs2019 --package-query compiler.version=192
"""
import argparse
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from xmsconan.constants import DEFAULT_REMOTE_NAME


@contextlib.contextmanager
def _package_list(ref, package_query):
    """Yield a Conan package list of ``ref``'s binaries matching ``package_query``.

    ``conan cache save`` takes either a pattern or a package list, and only the
    list can be narrowed by a binary query -- so a query is resolved to a list
    first, with ``conan list``, and the list is what gets saved. The list must
    name package revisions (``:*#*``, not ``:*``) or the save silently drops
    every binary; see the comment on the command below.

    A context manager rather than a function returning a path: the file must not
    outlive the save, and an ownership contract that lives only in a docstring
    is one that a later caller can miss. It also makes the cleanup total. The
    first version unlinked on ``CalledProcessError`` alone, which left the file
    behind for the two failures that do not raise it -- ``conan`` missing from
    PATH (``FileNotFoundError``) and an unwritable temp directory (``OSError``
    from ``open``) -- while the docstring promised a failed resolve leaves
    nothing behind.

    Args:
        ref: ``<library>/<version>`` reference.
        package_query: Conan binary query, e.g. ``"compiler.version=192"``.

    Yields:
        Path to the package list JSON, valid for the duration of the block.

    Raises:
        subprocess.CalledProcessError: ``conan list`` exited nonzero.
        OSError: The list could not be written, or ``conan`` is not on PATH.
    """
    handle, path = tempfile.mkstemp(prefix="xmsconan-pkglist-", suffix=".json")
    os.close(handle)
    # `-c` is the default when no `--remote` is given, but it is spelled out:
    # this list decides what gets published, and "the local cache" must not be
    # something a reader has to know a default to be sure of.
    #
    # `#*` asks for each binary's package REVISION, and without it this whole
    # path is a no-op. `conan list "<ref>:*"` reports a package as an `info`
    # block alone; `conan cache save` archives a package *revision* folder, so
    # handed a list with no revision it saves the recipe and skips every
    # binary -- exit 0, tarball written, nothing in it. The deploy stage then
    # restores that and uploads a lone recipe, also exit 0. Nothing fails until
    # a consumer resolves the reference and finds no binary for its settings.
    command = ["conan", "list", f"{ref}:*#*", "-c", "-p", package_query, "--format=json"]
    try:
        with open(path, "w", encoding="utf-8") as output:
            subprocess.run(command, check=True, stdout=output)
        yield path
    finally:
        # missing_ok rather than an `if os.path.exists` guard: mkstemp created
        # the file, so the only way it is gone is that the body moved or
        # removed it -- and a cleanup that raised would mask whatever exception
        # is being unwound. Expressed without a branch because that branch is
        # not reachable from any caller and so could not be tested.
        Path(path).unlink(missing_ok=True)


def conan_deploy(library, version, save=None, restore=None, upload=False,
                 remote=DEFAULT_REMOTE_NAME, package_query=None):
    """Perform Conan cache save, restore, or upload operations.

    Args:
        library: Library name (e.g. ``xmscore``).
        version: Package version string.
        save: Path to write the cache tarball to (``conan cache save``).
        restore: Path to read a cache tarball from (``conan cache restore``).
        upload: If ``True``, upload the package to ``remote``.
        remote: Conan remote to upload to. Defaults to
            :data:`~xmsconan.constants.DEFAULT_REMOTE_NAME`; the msvc 192 jobs
            pass :data:`~xmsconan.constants.VS2019_REMOTE_NAME` so those
            binaries stay out of the CI remote.
        package_query: Conan binary query restricting both ``--save`` and
            ``--upload`` to matching binaries, e.g. ``"compiler.version=192"``.

            Not optional decoration for a toolchain-specific remote. Both
            ``conan cache save <ref>:*`` and ``conan upload <ref>`` match by
            *reference*, and the Conan cache on a CI runner is per machine, not
            per job -- so a msvc 192 job sharing a Windows VM with a msvc 194
            job would tarball and publish the other one's binaries to the
            VS2019 remote, and exit 0 having done it.
    """
    ref = f"{library}/{version}"

    if save:
        if package_query:
            with _package_list(ref, package_query) as list_file:
                subprocess.run(
                    ["conan", "cache", "save", "--list", list_file, "--file", save],
                    check=True,
                )
        else:
            # `conan cache save` treats a bare `pkg/version` as a recipe-only pattern, so
            # the tarball would carry the recipe and none of the binaries built alongside
            # it. `:*` selects every package id under the reference. Without it the deploy
            # job restores a recipe with no packages and silently uploads nothing.
            subprocess.run(
                ["conan", "cache", "save", "--file", save, f"{ref}:*"],
                check=True,
            )

    if restore:
        subprocess.run(
            ["conan", "cache", "restore", restore],
            check=True,
        )

    if upload:
        # `:*` for the same reason `conan cache save` needs it above, and the
        # omission here was worse: a bare `pkg/version` is a recipe-only
        # pattern, so `-p` filtered an empty set of packages and conan uploaded
        # the recipe alone and exited 0. Without `-p` a bare reference does
        # carry the binaries, which is why the unqueried Linux deploy published
        # correctly and every Windows deploy silently published nothing --
        # Aquaveo/xmsconstraint 6.0.12, pipeline 63339, where all five jobs went
        # green and only Linux had packages in its upload summary.
        command = ["conan", "upload", f"{ref}:*", "-r", remote, "--confirm"]
        if package_query:
            command += ["-p", package_query]
        subprocess.run(command, check=True)


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
        help=f"Upload the package to the --remote (default: {DEFAULT_REMOTE_NAME}).",
    )
    parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE_NAME,
        help=f"Conan remote to upload to (default: {DEFAULT_REMOTE_NAME}).",
    )
    parser.add_argument(
        "--package-query",
        default=None,
        help="Restrict --save and --upload to binaries matching this Conan "
             "query, e.g. 'compiler.version=192'. Required when publishing to "
             "a toolchain-specific remote from a shared runner: both commands "
             "otherwise match by reference and would carry another job's "
             "binaries.",
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
            remote=args.remote,
            package_query=args.package_query,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
