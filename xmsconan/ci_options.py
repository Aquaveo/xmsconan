"""The ``[ci]`` table of ``build.toml``: its vocabulary, types, and defaults.

One module so the several readers of that table cannot drift apart. Before this
existed, ``windows_wheel_repair`` was read independently in the CI generator and
in the publish CLI, each restating the default, and neither checking the key
name or the value's type -- so ``windows_repair_wheel = false`` or
``windows_wheel_repair = "false"`` both left repair silently enabled. The only
symptom would have been the thing the option exists to prevent.
"""
# 1. Standard python modules

# 2. Third party modules

# 3. Aquaveo modules
from xmsconan.toml_utils import load_toml

#: Every key the ``[ci]`` table accepts, mapped to the type a value must have.
#: A key absent from here is a typo: the readers below would silently fall back
#: to a default, which for a switch that turns work *off* means the work keeps
#: happening.
CI_KEY_TYPES = {
    "windows": bool,
    "linux": bool,
    "linux_arm": bool,
    "deploy": bool,
    "coverage": bool,
    "xvfb": bool,
    "split_tests": bool,
    "windows_wheel_repair": bool,
    "test_shards": int,
    "docker_image": str,
    "python_versions": list,
    "linux_python_versions": list,
    "mac_python_versions": list,
}


def validate_ci_table(ci_config: dict) -> None:
    """Reject an unknown key or a wrongly typed value in ``[ci]``.

    Args:
        ci_config: The ``[ci]`` table, possibly empty.

    Raises:
        ValueError: When ``ci_config`` is not a table, carries a key outside
            :data:`CI_KEY_TYPES`, or gives a key a value of the wrong type.
    """
    if not isinstance(ci_config, dict):
        raise ValueError(f'[ci] must be a table, got {type(ci_config).__name__}')

    unknown = sorted(set(ci_config) - set(CI_KEY_TYPES))
    if unknown:
        raise ValueError(
            f'[ci] has unknown key(s) {", ".join(unknown)}. '
            f'Accepted keys: {", ".join(sorted(CI_KEY_TYPES))}.'
        )

    for key, value in ci_config.items():
        expected = CI_KEY_TYPES[key]
        # bool is a subclass of int, so an int-typed key must not accept True.
        if expected is int and isinstance(value, bool):
            raise ValueError(f'[ci].{key} must be an integer, got a boolean')
        if not isinstance(value, expected):
            raise ValueError(
                f'[ci].{key} must be {expected.__name__}, got '
                f'{type(value).__name__} ({value!r})'
            )


def repairs_windows_wheel(toml_data: dict) -> bool:
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
        toml_data: The parsed ``build.toml``.

    Returns:
        True when the Windows wheel should be repaired.

    Raises:
        ValueError: When the ``[ci]`` table is invalid (see
            :func:`validate_ci_table`).
    """
    ci_config = toml_data.get("ci", {})
    validate_ci_table(ci_config)
    if "windows_wheel_repair" in ci_config:
        return bool(ci_config["windows_wheel_repair"])
    return toml_data.get("ci_type") != "gitlab"


def repairs_windows_wheel_for(toml_path: str) -> bool:
    """Read ``build.toml`` from disk and resolve the Windows repair decision.

    Args:
        toml_path: Path to the library's ``build.toml``.

    Returns:
        True when the Windows wheel should be repaired.
    """
    return repairs_windows_wheel(load_toml(toml_path))
