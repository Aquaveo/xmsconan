"""Repair a Python wheel for the current platform.

Usage::

    xmsconan_wheel_repair [--wheel-dir DIR] [--platform linux|macos|windows]
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import sysconfig

from xmsconan._cli import resolve_tool


def _pip_install_cmd(*packages):
    """Return a pip install command list, using uv if available."""
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, *packages]
    return [sys.executable, "-m", "pip", "install", *packages]


def _repair_env(**overrides):
    """Return a subprocess environment that can find the just-installed repair tool.

    ``_pip_install_cmd`` installs the repair tool into the interpreter running this
    module, which places its console script in that interpreter's script directory.
    That directory is not necessarily on ``PATH``. When xmsconan is installed as a
    ``uv tool`` the launcher execs the tool venv's interpreter directly, so the
    venv's ``bin`` never joins ``PATH`` and invoking the script by bare name fails
    with ``FileNotFoundError`` even though the install just succeeded. Prepending
    the script directory makes the invocation resolve to the same environment the
    install targeted.

    Args:
        **overrides: Extra environment variables to set, such as the platform's
            dynamic loader search path.

    Returns:
        dict: A copy of ``os.environ`` with ``PATH`` extended and *overrides* applied.
    """
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(filter(None, [sysconfig.get_path("scripts"), env.get("PATH", "")]))
    env.update(overrides)
    return env


def _tool_argv0(tool, env):
    """Return an absolute path to *tool*, falling back to the bare name.

    Prepending the script directory to *env* is not enough on its own. Windows
    resolves an unqualified program name against the *calling* process's ``PATH``
    rather than the environment block handed to the child, so ``CreateProcess``
    never sees the prepend and the lookup fails exactly as before. Resolving here
    and passing an absolute ``argv[0]`` makes one mechanism carry every platform.

    Args:
        tool: Console-script name, such as ``'delvewheel'``.
        env: Environment whose ``PATH`` is searched, from :func:`_repair_env`.

    Returns:
        str: The resolved absolute path, or *tool* unchanged when it is not found
        -- in which case the invocation fails with the same ``FileNotFoundError``
        it would have raised anyway.
    """
    return resolve_tool(tool, path=env["PATH"]) or tool


def _detect_platform():
    """Return ``'linux'``, ``'macos'``, or ``'windows'`` from *sys.platform*."""
    if sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform == "darwin":
        return "macos"
    elif sys.platform == "win32":
        return "windows"
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def wheel_repair(wheel_dir="wheelhouse", platform=None):
    """Install the appropriate repair tool, repair wheels, and swap dirs.

    Args:
        wheel_dir: Directory containing the wheel and a ``libs/`` subfolder.
        platform: One of ``'linux'``, ``'macos'``, ``'windows'``.
            Auto-detected from *sys.platform* when ``None``.
    """
    if platform is None:
        platform = _detect_platform()

    # Normalize before deriving the sibling directory. A trailing separator --
    # which every shell tab-completion adds -- turns "wheelhouse/" into the
    # *child* "wheelhouse/_repaired", so the rmtree below deletes the repaired
    # wheels along with the originals and move() then raises on a path that no
    # longer exists. Both the built and the repaired wheels are gone by then.
    wheel_dir = os.path.normpath(wheel_dir)
    repaired_dir = f"{wheel_dir}_repaired"
    wheels = glob.glob(os.path.join(wheel_dir, "*.whl"))
    if not wheels:
        raise FileNotFoundError(f"No .whl files found in {wheel_dir}")

    libs_path = os.path.abspath(os.path.join(wheel_dir, "libs"))

    if platform == "linux":
        subprocess.run(_pip_install_cmd("auditwheel", "patchelf"), check=True)
        env = _repair_env(LD_LIBRARY_PATH=libs_path)
        for whl in wheels:
            subprocess.run(
                [_tool_argv0("auditwheel", env), "repair", whl, "-w", repaired_dir],
                check=True,
                env=env,
            )
    elif platform == "macos":
        subprocess.run(_pip_install_cmd("delocate"), check=True)
        env = _repair_env(DYLD_LIBRARY_PATH=libs_path)
        for whl in wheels:
            subprocess.run(
                [_tool_argv0("delocate-wheel", env), "-w", repaired_dir, "-v", whl],
                check=True,
                env=env,
            )
    elif platform == "windows":
        subprocess.run(_pip_install_cmd("delvewheel"), check=True)
        env = _repair_env()
        for whl in wheels:
            subprocess.run(
                [
                    _tool_argv0("delvewheel", env), "repair", whl,
                    "--add-path", libs_path,
                    "--namespace-pkg", "xms",
                    "-w", repaired_dir,
                ],
                check=True,
                env=env,
            )
    else:
        raise ValueError(f"Unknown platform: {platform}")

    shutil.rmtree(wheel_dir)
    shutil.move(repaired_dir, wheel_dir)


def main():
    """CLI entry point for ``xmsconan_wheel_repair``."""
    parser = argparse.ArgumentParser(description="Repair a Python wheel for the current platform.", )
    parser.add_argument(
        "--wheel-dir",
        default="wheelhouse",
        help="Directory containing .whl files (default: wheelhouse).",
    )
    parser.add_argument(
        "--platform",
        choices=["linux", "macos", "windows"],
        default=None,
        help="Target platform (default: auto-detect).",
    )
    args = parser.parse_args()
    try:
        wheel_repair(wheel_dir=args.wheel_dir, platform=args.platform)
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
