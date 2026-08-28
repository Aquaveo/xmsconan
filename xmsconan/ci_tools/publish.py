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
     (plus ``--skip-dependency-libs`` when step 4 is skipped)
  4. ``xmsconan_wheel_repair --wheel-dir DIR`` -- skipped on Windows when
     ``[ci].windows_wheel_repair`` resolves to false
  5. ``xmsconan_wheel_deploy --wheel-dir DIR``
  6. ``xmsconan_conan_deploy LIBRARY VERSION --upload``

Credentials for wheel deployment are resolved from CLI arguments,
environment variables, or ``~/.xmsconan.toml`` (see
:mod:`xmsconan.ci_tools.credentials`).
"""
import argparse
from dataclasses import dataclass, field
import os
import shutil
import subprocess
import sys

from xmsconan.build_toml import read_build_toml
from xmsconan.ci_options import repairs_windows_wheel
from xmsconan.ci_tools.conan_deploy import conan_deploy as _conan_deploy
from xmsconan.ci_tools.conan_setup import conan_setup as _conan_setup
from xmsconan.ci_tools.wheel_deploy import wheel_deploy as _wheel_deploy
from xmsconan.ci_tools.wheel_repair import wheel_repair as _wheel_repair
from xmsconan.generator_tools.version import FALLBACK_VERSION, resolve_version


def _repairs_wheel(config) -> bool:
    """Whether this platform's wheel should be repaired.

    Only Windows is switchable, and the decision -- key name, type and
    ``ci_type``-derived default -- lives in :mod:`xmsconan.ci_options` so this
    reader and the CI generator cannot disagree about it. Linux and macOS have
    no such switch: an unrepaired manylinux wheel is not installable.

    Args:
        config: The parsed build.toml.

    Returns:
        True when the wheel should be repaired on this platform.
    """
    if sys.platform != "win32":
        return True
    return repairs_windows_wheel(config)


def _check_xvfb(config):
    """Check if xvfb-run should wrap commands.

    Returns ``True`` on Linux when ``[ci].xvfb`` is set, no ``$DISPLAY`` is
    available, and ``xvfb-run`` is on PATH.
    """
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("DISPLAY"):
        return False
    if not config.ci.xvfb:
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


@dataclass
class PublishSteps:
    """Callable steps used by :func:`publish`.

    Each field defaults to the production implementation.  Tests can
    supply fakes to avoid patching module-level names.
    """

    conan_setup: object = field(default=None)
    subprocess_run: object = field(default=None)
    wheel_repair: object = field(default=None)
    wheel_deploy: object = field(default=None)
    conan_deploy: object = field(default=None)
    check_xvfb: object = field(default=None)

    def __post_init__(self):  # noqa: D105
        if self.conan_setup is None:
            self.conan_setup = _conan_setup
        if self.subprocess_run is None:
            self.subprocess_run = subprocess.run
        if self.wheel_repair is None:
            self.wheel_repair = _wheel_repair
        if self.wheel_deploy is None:
            self.wheel_deploy = _wheel_deploy
        if self.conan_deploy is None:
            self.conan_deploy = _conan_deploy
        if self.check_xvfb is None:
            self.check_xvfb = _check_xvfb


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
    steps=None,
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
        steps: :class:`PublishSteps` instance (production defaults if omitted).
    """
    steps = steps or PublishSteps()

    version = resolve_version(version)
    if version == FALLBACK_VERSION:
        raise SystemExit(
            "Error: could not determine version from git tag. "
            "Pass --version explicitly."
        )

    config = read_build_toml(toml_path)
    library_name = config.library_name
    use_xvfb = steps.check_xvfb(config)
    xvfb = _xvfb_prefix() if use_xvfb else []

    # 1. Setup Conan
    print("==> Setting up Conan...")
    steps.conan_setup(login=True)

    # 2. Generate build files
    print("==> Generating build files...")
    steps.subprocess_run(
        ["xmsconan_gen", "--version", version, toml_path],
        check=True,
    )

    # 3. Build (wrapped with xvfb-run if needed)
    print("==> Building...")
    repair_wheel = _repairs_wheel(config)
    build_cmd = xvfb + [
        sys.executable, "build.py",
        "--version", version,
        "--wheel-dir", wheel_dir,
    ]
    if not repair_wheel:
        # The staged libraries exist only to let the repair tools resolve
        # imports, so collecting them is pure cost once repair is off.
        build_cmd.append("--skip-dependency-libs")
    if build_filter:
        build_cmd.extend(["--filter", build_filter])
    steps.subprocess_run(build_cmd, check=True)

    # 4. Repair wheel
    if repair_wheel:
        print("==> Repairing wheel...")
        steps.wheel_repair(wheel_dir=wheel_dir)
    else:
        print("==> Skipping wheel repair ([ci].windows_wheel_repair = false)")

    # 5. Deploy wheel
    if deploy_wheel:
        print("==> Uploading wheel...")
        steps.wheel_deploy(
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
        steps.conan_deploy(library_name, version, upload=True)
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
        # No try/except: sys.exit already does the right thing with either code
        # docker_publish raises. An int (the container's returncode) becomes the
        # exit status; a str is printed to stderr and the status becomes 1. The
        # isinstance(int) guard that used to be here substituted a bare 1 for
        # the string, so `xmsconan publish --docker` on a machine without Docker
        # exited 1 with no explanation of why.
        docker_publish(args)
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
