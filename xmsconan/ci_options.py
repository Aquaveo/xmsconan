"""The Windows wheel-repair decision, read from ``build.toml``.

One function so the several readers of that switch cannot drift apart. Before
this existed, ``windows_wheel_repair`` was read independently in the CI
generator and in the publish CLI, each restating a default -- so a misspelled
key or a quoted boolean left repair silently enabled, and the only symptom
would have been the thing the option exists to prevent. The key and type
checks that catch those now live with the ``[ci]`` schema in
:mod:`xmsconan.build_toml`, which also refuses a file that leaves the key out.
"""
import sys

from xmsconan.build_toml import BuildToml


def repairs_windows_wheel(config: BuildToml) -> bool:
    """Whether the Windows wheel should be repaired for this library.

    There is no default: the answer depends on who loads the wheel, and
    nothing in ``build.toml`` reliably says who that is. ``ci_type`` used to
    stand in for it (GitHub wheels repaired, GitLab wheels not), but libraries
    of both kinds now live on both forges, so the guess was wrong often enough
    that :func:`xmsconan.build_toml.read_build_toml` requires the key instead.
    The two answers:

    * ``true`` for a wheel installed into ordinary Python environments, which
      have no XMS runtime on ``PATH``, so the DLLs a module needs have to
      travel with it.
    * ``false`` for a wheel the desktop products load. They point ``PATH`` at
      their own shipped redistributable on purpose, and delvewheel does not
      ignore ``msvcp140.dll``, so repairing such a wheel vendors a private
      mangled copy of the very runtime the host is trying to control.

    Args:
        config: The parsed ``build.toml``.

    Returns:
        True when the Windows wheel should be repaired.

    Raises:
        ValueError: When ``config`` was built in code without the key. A file
            read through :func:`xmsconan.build_toml.read_build_toml` cannot
            reach this; the reader rejects it first.
    """
    if config.ci.windows_wheel_repair is None:
        raise ValueError(
            "[ci].windows_wheel_repair is not set; build.toml must say whether "
            "the Windows wheel is repaired"
        )
    return config.ci.windows_wheel_repair


def repairs_wheel(config: BuildToml, platform: str = None) -> bool:
    """Whether this platform's wheel gets repaired at all.

    Only Windows is switchable, and the decision -- key name, type and the
    absence of a default -- is :func:`repairs_windows_wheel` above, so no two
    readers of the key can disagree about it. The CI generator is not one of
    them any more: it renders the same Windows job at either setting, and the job
    reads this when it runs. Linux and macOS have no such switch: an unrepaired
    manylinux wheel is not installable.

    The answer also decides whether the Conan cache's shared libraries are
    staged next to the wheel. They exist only so the repair tools can resolve
    imports, so collecting them is pure cost once repair is off -- which is why
    one predicate answers both questions rather than two that could disagree.

    Args:
        config: The parsed build.toml.
        platform: ``sys.platform`` value to answer for; the running one when
            None.

    Returns:
        True when the wheel should be repaired on that platform.
    """
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return True
    return repairs_windows_wheel(config)
