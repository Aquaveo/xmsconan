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


def _pip_install_cmd(*packages):
    """Return a pip install command list, using uv if available."""
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable, *packages]
    return [sys.executable, "-m", "pip", "install", *packages]


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

    repaired_dir = f"{wheel_dir}_repaired"
    wheels = glob.glob(os.path.join(wheel_dir, "*.whl"))
    if not wheels:
        raise FileNotFoundError(f"No .whl files found in {wheel_dir}")

    if platform == "linux":
        subprocess.run(_pip_install_cmd("auditwheel", "patchelf"), check=True)
        libs_path = os.path.abspath(os.path.join(wheel_dir, "libs"))
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = libs_path
        for whl in wheels:
            subprocess.run(
                ["auditwheel", "repair", whl, "-w", repaired_dir],
                check=True,
                env=env,
            )
    elif platform == "macos":
        subprocess.run(_pip_install_cmd("delocate"), check=True)
        libs_path = os.path.abspath(os.path.join(wheel_dir, "libs"))
        env = os.environ.copy()
        env["DYLD_LIBRARY_PATH"] = libs_path
        for whl in wheels:
            subprocess.run(
                ["delocate-wheel", "-w", repaired_dir, "-v", whl],
                check=True,
                env=env,
            )
    elif platform == "windows":
        subprocess.run(_pip_install_cmd("delvewheel"), check=True)
        libs_path = os.path.abspath(os.path.join(wheel_dir, "libs"))
        for whl in wheels:
            subprocess.run(
                [
                    "delvewheel", "repair", whl,
                    "--add-path", libs_path,
                    "--namespace-pkg", "xms",
                    "-w", repaired_dir,
                ],
                check=True,
            )
    else:
        raise ValueError(f"Unknown platform: {platform}")

    shutil.rmtree(wheel_dir)
    shutil.move(repaired_dir, wheel_dir)


def main():
    """CLI entry point for ``xmsconan_wheel_repair``."""
    parser = argparse.ArgumentParser(
        description="Repair a Python wheel for the current platform.",
    )
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
