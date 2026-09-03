"""The one place the TOML parser is chosen.

``tomllib`` is standard from Python 3.11; on 3.10 the same API comes from the
``tomli`` backport, which ``pyproject.toml`` requires only there. Every reader
in the package -- ``build.toml`` and ``~/.xmsconan.toml`` alike -- imports
:func:`loads` and :class:`TOMLDecodeError` from here, so they cannot disagree
about which parser, or which exception, they get.
"""
try:
    from tomllib import loads, TOMLDecodeError
except ModuleNotFoundError:  # Python < 3.11
    from tomli import loads, TOMLDecodeError

__all__ = ["TOMLDecodeError", "loads"]
