"""One TOML reader for every tool in the package.

Each entry point used to carry its own ``tomllib``/``toml`` fallback, and the
copies had already drifted -- some called ``load`` on a binary handle, others
``loads`` on decoded text, which read the same file with two different encoding
rules. There is one shim here instead, so adding an entry point does not mean
adding a fifth.
"""
# 1. Standard python modules
from pathlib import Path

# 2. Third party modules
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

import toml


def load_toml(toml_path):
    """Parse a TOML file, preferring the stdlib parser when it is available.

    Args:
        toml_path: Path to the TOML file, as ``str`` or ``Path``.

    Returns:
        The parsed document as a dict.
    """
    text = Path(toml_path).read_text(encoding="utf-8")
    if tomllib:
        return tomllib.loads(text)
    return toml.loads(text)
