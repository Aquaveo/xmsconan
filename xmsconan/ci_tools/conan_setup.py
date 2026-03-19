"""Set up Conan profile and remotes for CI builds.

Usage::

    xmsconan_conan_setup [--remote-url URL] [--login] [--remove-conancenter]
"""
import argparse
import subprocess
import sys

DEFAULT_REMOTE_URL = (
    "https://conan2.aquaveo.com/artifactory/api/conan/aquaveo-stable"
)


def conan_setup(remote_url=None, login=False, remove_conancenter=False):
    """Detect a Conan profile and configure the Aquaveo remote.

    Args:
        remote_url: Conan remote URL. Defaults to the Aquaveo stable remote.
        login: If ``True``, run ``conan remote login aquaveo`` after adding.
        remove_conancenter: If ``True``, remove the conancenter remote.
    """
    if remote_url is None:
        remote_url = DEFAULT_REMOTE_URL

    subprocess.run(["conan", "profile", "detect", "-e"], check=True)
    subprocess.run(
        [
            "conan", "remote", "add", "--index", "0",
            "aquaveo", remote_url, "--force",
        ],
        check=True,
    )

    if remove_conancenter:
        subprocess.run(
            ["conan", "remote", "remove", "conancenter"],
            check=True,
        )

    if login:
        subprocess.run(["conan", "remote", "login", "aquaveo"], check=True)


def main():
    """CLI entry point for ``xmsconan_conan_setup``."""
    parser = argparse.ArgumentParser(
        description="Set up Conan profile and remotes for CI builds.",
    )
    parser.add_argument(
        "--remote-url",
        default=None,
        help="Conan remote URL (default: Aquaveo stable).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run 'conan remote login aquaveo' after adding the remote.",
    )
    parser.add_argument(
        "--remove-conancenter",
        action="store_true",
        help="Remove the conancenter remote.",
    )
    args = parser.parse_args()
    try:
        conan_setup(
            remote_url=args.remote_url,
            login=args.login,
            remove_conancenter=args.remove_conancenter,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
