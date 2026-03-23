"""Read deployment credentials from ``~/.xmsconan.toml``.

The file uses TOML format::

    [aquapi]
    url = "https://public.aquapi.aquaveo.com/aquaveo/dev/"
    username = "myuser"
    password = "mypass"

Credentials are resolved in order: CLI arguments > environment variables >
config file.  This module provides the config-file layer.
"""
from pathlib import Path

import toml

CONFIG_FILENAME = ".xmsconan.toml"


def _config_path():
    """Return the path to the user config file."""
    return Path.home() / CONFIG_FILENAME


def load_credentials(config_path=None):
    """Load the ``[aquapi]`` section from ``~/.xmsconan.toml``.

    Args:
        config_path: Path to the config file.  Defaults to
            ``~/.xmsconan.toml``.

    Returns:
        A dict with ``url``, ``username``, and ``password`` keys.
        Missing keys are omitted.  Returns an empty dict if the file
        does not exist or has no ``[aquapi]`` section.
    """
    path = config_path or _config_path()
    if not path.is_file():
        return {}
    try:
        data = toml.load(path)
    except toml.TomlDecodeError:
        return {}
    return data.get("aquapi", {})


def load_conan_credentials(config_path=None):
    """Load the ``[conan]`` section from ``~/.xmsconan.toml``.

    Args:
        config_path: Path to the config file.  Defaults to
            ``~/.xmsconan.toml``.

    Returns:
        A dict with ``username`` and ``password`` keys.
        Missing keys are omitted.  Returns an empty dict if the file
        does not exist or has no ``[conan]`` section.
    """
    path = config_path or _config_path()
    if not path.is_file():
        return {}
    try:
        data = toml.load(path)
    except toml.TomlDecodeError:
        return {}
    return data.get("conan", {})
