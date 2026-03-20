"""Unified CLI entry point for xmsconan.

Dispatches ``xmsconan <subcommand> [args...]`` to the appropriate module's
``main()`` function while rewriting *sys.argv* so that each subcommand's
argparse shows the correct program name (e.g. ``xmsconan gen``).
"""

import importlib
import sys

# (description, module_path, function_name)
COMMANDS = {
    "gen": ("Generate build files from templates", "xmsconan.generator_tools.build_file_generator", "main"),
    "ci": ("Generate CI pipeline files from templates", "xmsconan.generator_tools.ci_file_generator", "main"),
    "build": ("Build XMS libraries", "xmsconan.build_tools.build_library", "main"),
    "conan-setup": ("Set up Conan profile and remotes", "xmsconan.ci_tools.conan_setup", "main"),
    "wheel-repair": ("Repair Python wheels for the current platform", "xmsconan.ci_tools.wheel_repair", "main"),
    "wheel-deploy": ("Upload repaired wheels to devpi", "xmsconan.ci_tools.wheel_deploy", "main"),
    "conan-deploy": ("Save, restore, or upload Conan packages", "xmsconan.ci_tools.conan_deploy", "main"),
    "publish": ("Build, repair, and deploy a library", "xmsconan.ci_tools.publish", "main"),
}


def _print_usage(file=sys.stdout):
    """Print top-level usage listing all subcommands."""
    prog = "xmsconan"
    lines = [
        f"usage: {prog} <command> [args...]\n",
        "Available commands:\n",
    ]
    max_name = max(len(name) for name in COMMANDS)
    for name, (desc, _, _) in COMMANDS.items():
        lines.append(f"  {name:<{max_name}}  {desc}")
    lines.append(f"\nRun '{prog} <command> --help' for details on a specific command.")
    print("\n".join(lines), file=file)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        _print_usage()
        sys.exit(0)

    subcmd = sys.argv[1]

    if subcmd not in COMMANDS:
        print(f"xmsconan: unknown command '{subcmd}'\n", file=sys.stderr)
        _print_usage(file=sys.stderr)
        sys.exit(1)

    desc, module_path, func_name = COMMANDS[subcmd]
    # Rewrite argv so the subcommand's argparse shows the right program name.
    sys.argv = [f"xmsconan {subcmd}"] + sys.argv[2:]

    module = importlib.import_module(module_path)
    getattr(module, func_name)()
