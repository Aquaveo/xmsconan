"""Launch ``xmsconan publish`` inside a Docker container.

Usage (called automatically by ``xmsconan publish --docker``)::

    xmsconan publish --version 0.0.1 --docker
    xmsconan publish --version 0.0.1 --docker --xmsconan-dir ../xmsconan
    xmsconan publish --version 0.0.1 --docker --docker-image my-image

This module handles Docker image selection, volume mounting, credential
forwarding, and re-invocation of the publish command inside the container.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from xmsconan.toml_utils import load_toml, validate_top_level_keys

# Docker image registry and naming convention (matches CI templates).
DOCKER_REGISTRY = "docker.aquaveo.com/aquaveo/conan-docker"
DOCKER_IMAGE_BASE = "conan-gcc13-py3.13"
DOCKER_IMAGE_XVFB = "conan-gcc13-x11-gdal-py3.13"

# pip index for installing xmsconan inside the container.
PIP_INDEX = "https://public.aquapi.aquaveo.com/aquaveo/dev/+simple"


def resolve_docker_image(docker_image=None, toml_path="build.toml"):
    """Determine the Docker image to use.

    When *docker_image* is ``None``, the image is chosen based on the
    ``ci.xvfb`` flag in *toml_path* (same logic as the CI templates).

    Args:
        docker_image: Explicit image override (from ``--docker-image``).
        toml_path: Path to ``build.toml`` for ``ci.xvfb`` detection.

    Returns:
        Docker image string.
    """
    if docker_image:
        return docker_image

    data = load_toml(toml_path)
    validate_top_level_keys(data, toml_path)
    ci_config = data.get("ci", {})

    toml_image = ci_config.get("docker_image")
    if toml_image:
        return toml_image

    image_name = DOCKER_IMAGE_XVFB if ci_config.get("xvfb", False) else DOCKER_IMAGE_BASE
    return f"{DOCKER_REGISTRY}/{image_name}"


def _build_env_flags():
    """Build ``-e`` flags for devpi credential forwarding.

    Forwards ``AQUAPI_*`` env vars if they are set on the host.  The flags
    name the variables and stop there: ``docker run -e VAR`` makes the docker
    client read ``VAR`` out of its own environment, which ``subprocess.run``
    hands it unchanged, so the container sees the same value it would have
    seen from ``-e VAR=value`` -- without ``AQUAPI_PASSWORD=<secret>`` sitting
    in the argv of the docker process, where process-creation auditing
    (Windows Event 4688, Sysmon Event ID 1, ``ps`` on Linux) can read it.
    The rest of this package goes to some length to keep secrets off command
    lines (see :func:`xmsconan.ci_tools.conan_setup._login_environment`);
    this is the same rule applied to the one command that spawns another.

    Returns:
        List of ``["-e", "VAR"]`` pairs, for the vars that are set.
    """
    env_vars = [
        "AQUAPI_URL",
        "AQUAPI_USERNAME",
        "AQUAPI_PASSWORD",
    ]
    flags = []
    for var in env_vars:
        # Still guarded on the value: `-e VAR` for an unset VAR asks docker to
        # forward nothing, and an empty AQUAPI_URL inside the container reads
        # as "configured, and configured wrong" rather than "not configured".
        if os.environ.get(var):
            flags.extend(["-e", var])
    return flags


def _build_config_mount():
    """Mount ``~/.xmsconan.toml`` read-only if it exists on the host.

    Returns:
        List of ``["-v", "..."]`` args, or an empty list.
    """
    config_path = Path.home() / ".xmsconan.toml"
    if config_path.is_file():
        return ["-v", f"{config_path}:/root/.xmsconan.toml:ro"]
    return []


def _build_install_cmd(xmsconan_dir=None):
    """Build the install command for inside the container.

    Args:
        xmsconan_dir: If set, install from this mounted path.

    Returns:
        Shell command string.
    """
    if xmsconan_dir:
        return "pip install --force-reinstall /xmsconan"
    from xmsconan import __version__
    return f"pip install 'xmsconan>={__version__}' -i {PIP_INDEX}"


def _build_publish_args(args):
    """Reconstruct ``xmsconan publish`` flags for inside the container.

    Strips ``--docker``, ``--docker-image``, and ``--xmsconan-dir``.

    Args:
        args: The parsed argparse namespace from publish's ``main()``.

    Returns:
        List of argument strings.
    """
    parts = []
    if args.version:
        parts.extend(["--version", args.version])
    if args.wheel_dir != "wheelhouse":
        parts.extend(["--wheel-dir", args.wheel_dir])
    if args.toml != "build.toml":
        parts.extend(["--toml", args.toml])
    if args.build_filter:
        parts.extend(["--filter", args.build_filter])
    if args.no_deploy:
        parts.append("--no-deploy")
    if args.no_wheel:
        parts.append("--no-wheel")
    if args.no_conan:
        parts.append("--no-conan")
    return parts


def docker_publish(args):
    """Execute ``xmsconan publish`` inside a Docker container.

    Args:
        args: Parsed argparse namespace from publish's ``main()``.

    Raises:
        SystemExit: On Docker command failure or missing Docker.
    """
    if not shutil.which("docker"):
        raise SystemExit(
            "Error: 'docker' not found on PATH. "
            "Install Docker to use --docker."
        )

    image = resolve_docker_image(
        docker_image=args.docker_image,
        toml_path=args.toml,
    )

    project_dir = os.getcwd()
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{project_dir}:/workspace",
        "-w", "/workspace",
    ]

    # Mount local xmsconan source if requested.
    if args.xmsconan_dir:
        xmsconan_abs = os.path.abspath(args.xmsconan_dir)
        docker_cmd.extend(["-v", f"{xmsconan_abs}:/xmsconan"])

    # Mount credentials config file.
    docker_cmd.extend(_build_config_mount())

    # Forward devpi env vars.
    docker_cmd.extend(_build_env_flags())

    docker_cmd.append(image)

    # Build inner shell command.
    install_cmd = _build_install_cmd(args.xmsconan_dir)
    publish_args = _build_publish_args(args)
    # shlex.join, not " ".join: this string is handed to the container's bash,
    # which re-splits it on whitespace. The documented
    # --filter '{"build_type": "Release"}' arrived inside the container as the
    # two words {"build_type": and "Release"}, so every --docker run that
    # passed a filter died in argparse before the build started.
    publish_cmd = shlex.join(["xmsconan", "publish", *publish_args])
    inner_script = f"{install_cmd} && {publish_cmd}"

    docker_cmd.extend(["bash", "-c", inner_script])

    print(f"==> Docker image: {image}")
    print(f"==> Project dir:  {project_dir}")
    print(f"==> Running:      {inner_script}")

    result = subprocess.run(docker_cmd)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
