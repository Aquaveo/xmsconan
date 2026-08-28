"""The Windows wheel-repair decision, read from ``build.toml``.

One function so the several readers of that switch cannot drift apart. Before
this existed, ``windows_wheel_repair`` was read independently in the CI
generator and in the publish CLI, each restating the default -- so a misspelled
key or a quoted boolean left repair silently enabled, and the only symptom
would have been the thing the option exists to prevent. The key and type
checks that catch those now live with the ``[ci]`` schema in
:mod:`xmsconan.build_toml`.
"""
from xmsconan.build_toml import BuildToml


def repairs_windows_wheel(config: BuildToml) -> bool:
    """Whether the Windows wheel should be repaired for this library.

    The default follows ``ci_type``, because it decides who installs the wheel:

    * ``github`` -> True. Those wheels are published for installation into
      arbitrary Python environments, which have no XMS runtime on ``PATH``, so
      the DLLs a module needs have to travel with it.
    * ``gitlab`` -> False. Those wheels are internal, and the only thing that
      loads them supplies the C++ runtime itself -- the desktop products point
      ``PATH`` at their own shipped redistributable on purpose. delvewheel does
      not ignore ``msvcp140.dll``, so repairing such a wheel vendors a private
      mangled copy of the very runtime the host is trying to control.

    A flat default is wrong in one direction or the other: ``True`` would start
    the GitLab repos repairing wheels they never even staged before, and
    ``False`` would stop the GitHub repos bundling DLLs their users need. An
    explicit ``[ci].windows_wheel_repair`` overrides either way.

    Args:
        config: The parsed ``build.toml``.

    Returns:
        True when the Windows wheel should be repaired.
    """
    if config.ci.windows_wheel_repair is not None:
        return config.ci.windows_wheel_repair
    return config.ci_type != "gitlab"
