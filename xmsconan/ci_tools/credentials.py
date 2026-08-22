"""Read deployment credentials from disk, never from a command line.

The file uses TOML format::

    [aquapi]
    url = "https://public.aquapi.aquaveo.com/aquaveo/dev/"
    username = "myuser"
    password = "mypass"

Credentials are resolved in order: CLI arguments > environment variables >
config file.  This module provides the config-file layer, plus the
``--password-file`` reader shared by ``xmsconan conan-setup`` and
``xmsconan vs2019 setup`` -- the two commands that take a password from a
file precisely so it never has to be typed onto a command line.
"""
from pathlib import Path

import toml

CONFIG_FILENAME = ".xmsconan.toml"


class CredentialsError(ValueError):
    """A credential source exists but cannot be used.

    Subclasses ``ValueError`` because that is what these functions have always
    raised and what callers already catch. It exists so an entry point can map
    *this* fault to a one-line usage error without also absorbing a
    ``ValueError`` from unrelated work in the same ``try`` -- ``conan_setup()``
    runs three subprocesses inside one.
    """


def _config_path():
    """Return the path to the user config file."""
    return Path.home() / CONFIG_FILENAME


def _load_section(config_path, section):
    """Return *section* of the config file, or ``{}`` when there is no file.

    An absent config file is a normal state -- credentials may come from the
    CLI or the environment instead -- so it yields an empty dict. A file that
    exists but does not parse is not: swallowing ``TomlDecodeError`` made the
    two indistinguishable, so a stray character in ``~/.xmsconan.toml``
    surfaced as ``wheel_deploy``'s "No devpi URL provided (--url,
    $AQUAPI_URL, or ~/.xmsconan.toml)" -- advice to configure the very file
    that had just failed to parse.

    Args:
        config_path: Path to the config file, or ``None`` for the default.
        section: Top-level table to return.

    Returns:
        The requested table, or an empty dict when the file or table is absent.

    Raises:
        CredentialsError: The file exists but is not valid TOML, or *section*
            is present but is not a table.
    """
    path = config_path or _config_path()
    if not path.is_file():
        return {}
    try:
        data = toml.load(path)
    except toml.TomlDecodeError as exc:
        raise CredentialsError(f"Could not parse {path}: {exc}") from exc
    table = data.get(section, {})
    if not isinstance(table, dict):
        raise CredentialsError(
            f"{path}: [{section}] must be a table, got {type(table).__name__}"
        )
    return table


def load_credentials(config_path=None):
    """Load the ``[aquapi]`` section from ``~/.xmsconan.toml``.

    Args:
        config_path: Path to the config file.  Defaults to
            ``~/.xmsconan.toml``.

    Returns:
        A dict with ``url``, ``username``, and ``password`` keys.
        Missing keys are omitted.  Returns an empty dict if the file
        does not exist or has no ``[aquapi]`` section.

    Raises:
        CredentialsError: The file exists but is not valid TOML, or the
            section is present but is not a table.
    """
    return _load_section(config_path, "aquapi")


def load_conan_credentials(config_path=None):
    """Load the ``[conan]`` section from ``~/.xmsconan.toml``.

    Args:
        config_path: Path to the config file.  Defaults to
            ``~/.xmsconan.toml``.

    Returns:
        A dict with ``username`` and ``password`` keys.
        Missing keys are omitted.  Returns an empty dict if the file
        does not exist or has no ``[conan]`` section.

    Raises:
        CredentialsError: The file exists but is not valid TOML, or the
            section is present but is not a table.
    """
    return _load_section(config_path, "conan")


def read_password_file(password_file):
    """Read a password from a file so it never has to appear on any argv.

    An explicitly named file must not fall through to another credential
    source -- that is the whole reason the flag exists -- so a path that does
    not exist, a file that cannot be read, and a file holding nothing but
    whitespace are all errors rather than "no password here, try the next
    source".  A typo (or a secret that never landed in the file) must not
    silently log in with a different account's password.

    Args:
        password_file: Path to a file holding the password on its own.
            Trailing whitespace -- the editor's newline -- is stripped.

    Returns:
        The password.

    Raises:
        CredentialsError: The file is missing, unreadable, or holds no
            password.
    """
    path = Path(password_file)
    if not path.is_file():
        raise CredentialsError(f"password file not found: {password_file}")
    try:
        password = path.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        raise CredentialsError(f"could not read password file {password_file}: {exc}") from exc
    if not password:
        raise CredentialsError(f"password file is empty: {password_file}")
    return password
