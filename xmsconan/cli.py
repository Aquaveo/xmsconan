"""Unified CLI entry point for xmsconan.

Dispatches ``xmsconan <subcommand> [args...]`` to the appropriate module's
``main()`` function while rewriting *sys.argv* so that each subcommand's
argparse shows the correct program name (e.g. ``xmsconan gen``).
"""

import importlib
import sys
from typing import NamedTuple


class Command(NamedTuple):
    """One dispatchable ``xmsconan`` subcommand.

    A named tuple rather than a bare 3-tuple: all three fields are strings,
    so a swapped ``module``/``function`` pair would survive every check here
    and only surface as an :func:`importlib.import_module` failure.

    Attributes:
        description: One-line summary shown in the top-level usage listing.
        module: Dotted path of the module implementing the subcommand.
        function: Name of that module's entry-point function.
    """

    description: str
    module: str
    function: str


#: Subcommand name -> :class:`Command`.
COMMANDS = {
    "gen": Command("Generate build files from templates", "xmsconan.generator_tools.build_file_generator", "main"),
    "ci": Command("Generate CI pipeline files from templates", "xmsconan.generator_tools.ci_file_generator", "main"),
    "coverage": Command("Run unified C++/Python coverage", "xmsconan.coverage_tools.coverage_generator", "main"),
    "build": Command("Build XMS libraries", "xmsconan.build_tools.build_library", "main"),
    "vs2019": Command("Build/publish the manual VS2019 (msvc 192) matrix", "xmsconan.build_tools.vs2019_build", "main"),
    "conan-setup": Command("Set up Conan profile and remotes", "xmsconan.ci_tools.conan_setup", "main"),
    "wheel-repair": Command("Repair Python wheels for the current platform", "xmsconan.ci_tools.wheel_repair", "main"),
    "wheel-deploy": Command("Upload repaired wheels to devpi", "xmsconan.ci_tools.wheel_deploy", "main"),
    "conan-deploy": Command("Save, restore, or upload Conan packages", "xmsconan.ci_tools.conan_deploy", "main"),
    "publish": Command("Build, repair, and deploy a library", "xmsconan.ci_tools.publish", "main"),
}


def _print_usage(file=sys.stdout):
    """Print top-level usage listing all subcommands."""
    prog = "xmsconan"
    lines = [
        f"usage: {prog} <command> [args...]\n",
        "Available commands:\n",
    ]
    max_name = max(len(name) for name in COMMANDS)
    for name, command in COMMANDS.items():
        lines.append(f"  {name:<{max_name}}  {command.description}")
    lines.append(f"\nRun '{prog} <command> --help' for details on a specific command.")
    print("\n".join(lines), file=file)


def main():
    """Dispatch ``xmsconan <subcommand>`` to the appropriate module."""
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        _print_usage()
        sys.exit(0)

    subcmd = sys.argv[1]

    if subcmd not in COMMANDS:
        print(f"xmsconan: unknown command '{subcmd}'\n", file=sys.stderr)
        _print_usage(file=sys.stderr)
        sys.exit(1)

    command = COMMANDS[subcmd]
    # Rewrite argv so the subcommand's argparse shows the right program name.
    sys.argv = [f"xmsconan {subcmd}"] + sys.argv[2:]

    module = importlib.import_module(command.module)
    getattr(module, command.function)()
