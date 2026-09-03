"""Tests for the TOML parser shim.

``xmsconan._tomllib`` binds ``tomllib`` on 3.11+ and the ``tomli`` backport
below that. Both readers in the package import from it, so the shim is the
one place a parser swap could go wrong -- and the fallback branch is the one
line of it that only the 3.10 CI leg would otherwise ever execute.
"""
import importlib
import sys

import pytest

from xmsconan import _tomllib


def test_loads_parses_a_table():
    """The shim exposes a working ``loads``."""
    assert _tomllib.loads('[aquapi]\nurl = "https://x/"\n') == {"aquapi": {"url": "https://x/"}}


def test_decode_error_is_a_value_error():
    """``TOMLDecodeError`` is what both readers catch, and a ``ValueError`` for callers that only know that."""
    with pytest.raises(_tomllib.TOMLDecodeError) as excinfo:
        _tomllib.loads("this is not valid toml [[[")
    assert isinstance(excinfo.value, ValueError)


def test_falls_back_to_tomli_when_tomllib_is_missing(monkeypatch):
    """Without ``tomllib`` (Python < 3.11) the shim binds the ``tomli`` backport instead.

    A ``None`` entry in ``sys.modules`` makes ``import tomllib`` raise
    ``ModuleNotFoundError`` on any interpreter, which is exactly what the
    shim's ``except`` clause is written for. The module is reloaded again
    afterwards so later tests see the binding the interpreter really has.
    """
    tomli = pytest.importorskip("tomli")
    monkeypatch.setitem(sys.modules, "tomllib", None)
    try:
        reloaded = importlib.reload(_tomllib)
        assert reloaded.loads is tomli.loads
        assert reloaded.TOMLDecodeError is tomli.TOMLDecodeError
    finally:
        monkeypatch.undo()
        importlib.reload(_tomllib)
