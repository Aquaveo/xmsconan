"""Build and publish an XMS library (wheels and/or Conan packages).

Usage::

    xmsconan_publish --version 7.0.0
    xmsconan_publish                          # version from git tag
    xmsconan_publish --version 7.0.0 --no-deploy
    xmsconan_publish --version 7.0.0 --no-wheel --no-conan
    xmsconan_publish --version 7.0.0 --filter '{"build_type": "Release"}'

Steps:
  1. ``xmsconan_conan_setup``
  2. ``xmsconan_gen --version VERSION build.toml``
  3. ``python build.py --version VERSION --wheel-dir DIR [--filter ...]``
  4. ``xmsconan_wheel_repair --wheel-dir DIR``
  5. ``xmsconan_wheel_deploy --wheel-dir DIR``
  6. ``xmsconan_conan_deploy LIBRARY VERSION --upload``

Credentials for wheel deployment are resolved from CLI arguments,
environment variables, or ``~/.xmsconan.toml`` (see
:mod:`xmsconan.ci_tools.credentials`).
"""
import argparse
import os
import shutil
import subprocess
import sys

import toml

from xmsconan.ci_tools.conan_deploy import conan_deploy
from xmsconan.ci_tools.conan_setup import conan_setup
from xmsconan.ci_tools.wheel_deploy import wheel_deploy
from xmsconan.ci_tools.wheel_repair import wheel_repair
from xmsconan.generator_tools.version import FALLBACK_VERSION, resolve_version


def _read_library_name(toml_path="build.toml"):
    """Read ``library_name`` from *toml_path*."""
    data = toml.load(toml_path)
    name = data.get("library_name")
    if not name:
        raise ValueError(f"No library_name found in {toml_path}")
    return name


def _read_ci_xvfb(toml_path="build.toml"):
    """Read ``ci.xvfb`` from *toml_path*.  Returns ``False`` if not set."""
    data = toml.load(toml_path)
    return data.get("ci", {}).get("xvfb", False)


def _needs_xvfb(toml_path="build.toml"):
    """Check if xvfb-run should wrap commands.

    Returns ``True`` on Linux when ``ci.xvfb`` is set, no ``$DISPLAY`` is
    available, and ``xvfb-run`` is on PATH.
    """
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("DISPLAY"):
        return False
    if not _read_ci_xvfb(toml_path):
        return False
    if not shutil.which("xvfb-run"):
        print(
            "WARNING: ci.xvfb=true but xvfb-run not found on PATH. "
            "VTK tests may segfault.",
            file=sys.stderr,
        )
        return False
    return True


def _xvfb_prefix():
    """Return the xvfb-run prefix for wrapping commands."""
    return ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24"]


def publish(
    version=None,
    wheel_dir="wheelhouse",
    toml_path="build.toml",
    build_filter=None,
    deploy_wheel=True,
    deploy_conan=True,
    url=None,
    username=None,
    password=None,
):
    """Build, repair, and publish an XMS library.

    Args:
        version: Package version string, or ``None`` to resolve from git tag.
        wheel_dir: Directory for wheel output.
        toml_path: Path to ``build.toml``.
        build_filter: JSON filter string for ``build.py --filter``.
        deploy_wheel: Upload wheel to devpi.
        deploy_conan: Upload Conan package to aquaveo remote.
        url: devpi index URL (falls back to env / config file).
        username: devpi username (falls back to env / config file).
        password: devpi password (falls back to env / config file).
    """
    version = resolve_version(version)
    if version == FALLBACK_VERSION:
        raise SystemExit(
            "Error: could not determine version from git tag. "
            "Pass --version explicitly."
        )

    library_name = _read_library_name(toml_path)
    use_xvfb = _needs_xvfb(toml_path)
    xvfb = _xvfb_prefix() if use_xvfb else []

    # 1. Setup Conan
    print("==> Setting up Conan...")
    conan_setup(login=True)

    # 2. Generate build files
    print("==> Generating build files...")
    subprocess.run(
        ["xmsconan_gen", "--version", version, toml_path],
        check=True,
    )

    # 3. Build (wrapped with xvfb-run if needed)
    print("==> Building...")
    build_cmd = xvfb + [
        sys.executable, "build.py",
        "--version", version,
        "--wheel-dir", wheel_dir,
    ]
    if build_filter:
        build_cmd.extend(["--filter", build_filter])
    subprocess.run(build_cmd, check=True)

    # 4. Repair wheel
    print("==> Repairing wheel...")
    wheel_repair(wheel_dir=wheel_dir)

    # 5. Deploy wheel
    if deploy_wheel:
        print("==> Uploading wheel...")
        wheel_deploy(
            wheel_dir=wheel_dir,
            url=url,
            username=username,
            password=password,
        )
    else:
        print("==> Skipping wheel upload (--no-wheel)")

    # 6. Deploy Conan package
    if deploy_conan:
        print("==> Uploading Conan package...")
        conan_deploy(library_name, version, upload=True)
    else:
        print("==> Skipping Conan upload (--no-conan)")

    print("==> Done.")


def main():
    """CLI entry point for ``xmsconan_publish``."""
    parser = argparse.ArgumentParser(
        description="Build and publish an XMS library.",
    )
    parser.add_argument(
        "--version", default=None,
        help="Package version string (default: from git tag via setuptools-scm).",
    )
    parser.add_argument(
        "--wheel-dir", default="wheelhouse",
        help="Directory for wheel output (default: wheelhouse).",
    )
    parser.add_argument(
        "--toml", default="build.toml",
        help="Path to build.toml (default: build.toml).",
    )
    parser.add_argument(
        "--filter", default=None, dest="build_filter",
        help="JSON filter for build.py (e.g. '{\"build_type\": \"Release\"}').",
    )
    parser.add_argument(
        "--no-deploy", action="store_true",
        help="Build and repair only; skip all uploads.",
    )
    parser.add_argument(
        "--no-wheel", action="store_true",
        help="Skip wheel upload.",
    )
    parser.add_argument(
        "--no-conan", action="store_true",
        help="Skip Conan package upload.",
    )
    parser.add_argument("--url", default=None, help="devpi index URL.")
    parser.add_argument("--username", default=None, help="devpi username.")
    parser.add_argument("--password", default=None, help="devpi password.")

    # Docker arguments
    parser.add_argument(
        "--docker", action="store_true",
        help="Run the publish workflow inside a Docker container.",
    )
    parser.add_argument(
        "--docker-image", default=None,
        help="Docker image to use (default: auto-detect from build.toml).",
    )
    parser.add_argument(
        "--xmsconan-dir", default=None,
        help="Path to local xmsconan source to install inside the container.",
    )
    args = parser.parse_args()

    if args.docker:
        from xmsconan.ci_tools.docker_run import docker_publish
        try:
            docker_publish(args)
        except SystemExit as exc:
            sys.exit(exc.code if isinstance(exc.code, int) else 1)
        return

    deploy_wheel = not args.no_deploy and not args.no_wheel
    deploy_conan = not args.no_deploy and not args.no_conan

    try:
        publish(
            version=args.version,
            wheel_dir=args.wheel_dir,
            toml_path=args.toml,
            build_filter=args.build_filter,
            deploy_wheel=deploy_wheel,
            deploy_conan=deploy_conan,
            url=args.url,
            username=args.username,
            password=args.password,
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
