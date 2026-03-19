"""Upload repaired wheels to a devpi index.

Usage::

    xmsconan_wheel_deploy [--wheel-dir DIR] [--url URL] [--username USER] [--password PASS]

Credentials are resolved in order:
  1. CLI arguments
  2. Environment variables (``AQUAPI_URL``, ``AQUAPI_USERNAME``, ``AQUAPI_PASSWORD``)
  3. ``~/.xmsconan.toml`` config file (see :mod:`xmsconan.ci_tools.credentials`)
"""
import argparse
import os
import subprocess
import sys

from xmsconan.ci_tools.credentials import load_credentials


def wheel_deploy(wheel_dir="wheelhouse", url=None, username=None, password=None):
    """Use *devpi* to upload wheels from *wheel_dir*.

    Args:
        wheel_dir: Directory containing repaired ``.whl`` files.
        url: devpi index URL.  Falls back to ``$AQUAPI_URL``, then
            ``~/.xmsconan.toml``.
        username: devpi username.  Falls back to ``$AQUAPI_USERNAME``,
            then ``~/.xmsconan.toml``.
        password: devpi password.  Falls back to ``$AQUAPI_PASSWORD``,
            then ``~/.xmsconan.toml``.
    """
    creds = load_credentials()
    url = url or os.environ.get("AQUAPI_URL") or creds.get("url")
    username = username or os.environ.get("AQUAPI_USERNAME") or creds.get("username")
    password = password or os.environ.get("AQUAPI_PASSWORD") or creds.get("password")

    if not url:
        raise ValueError(
            "No devpi URL provided (--url, $AQUAPI_URL, or ~/.xmsconan.toml)"
        )
    if not username:
        raise ValueError(
            "No devpi username provided (--username, $AQUAPI_USERNAME, or ~/.xmsconan.toml)"
        )
    if not password:
        raise ValueError(
            "No devpi password provided (--password, $AQUAPI_PASSWORD, or ~/.xmsconan.toml)"
        )

    subprocess.run(["devpi", "use", url], check=True)
    subprocess.run(
        ["devpi", "login", username, "--password", password],
        check=True,
    )
    subprocess.run(
        ["devpi", "upload", "--from-dir", wheel_dir],
        check=True,
    )


def main():
    """CLI entry point for ``xmsconan_wheel_deploy``."""
    parser = argparse.ArgumentParser(
        description="Upload repaired wheels to a devpi index.",
    )
    parser.add_argument(
        "--wheel-dir",
        default="wheelhouse",
        help="Directory containing .whl files (default: wheelhouse).",
    )
    parser.add_argument("--url", default=None, help="devpi index URL.")
    parser.add_argument("--username", default=None, help="devpi username.")
    parser.add_argument("--password", default=None, help="devpi password.")
    args = parser.parse_args()
    try:
        wheel_deploy(
            wheel_dir=args.wheel_dir,
            url=args.url,
            username=args.username,
            password=args.password,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
