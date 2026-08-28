"""Tests for tests.utils.patch_env."""
import os

import pytest

from .utils import patch_env

WINDOWS = os.name == 'nt'

# Captured at import time so the decorator-form test can assert equality
# against the pre-patch USERPROFILE — by the time the wrapped test body runs,
# patch.dict has already replaced os.environ.
_REAL_USERPROFILE = os.environ.get('USERPROFILE')


# --- decorator form ---


@pytest.mark.skipif(not WINDOWS, reason="USERPROFILE handling is Windows-only")
@patch_env({'USERNAME': 'ignored'})
def test_decorator_preserves_userprofile():
    """USERPROFILE stays set to the real value when used as a decorator."""
    assert os.environ['USERPROFILE'] == _REAL_USERPROFILE
    assert os.environ['USERNAME'] == 'ignored'


# --- context manager form ---


@pytest.mark.skipif(not WINDOWS, reason="USERPROFILE handling is Windows-only")
def test_context_manager_preserves_userprofile():
    """USERPROFILE stays set to the real value when used as a context manager."""
    real = os.environ['USERPROFILE']
    with patch_env({'USERNAME': 'ignored'}):
        assert os.environ['USERPROFILE'] == real
        assert os.environ['USERNAME'] == 'ignored'


@pytest.mark.skipif(not WINDOWS, reason="USERPROFILE handling is Windows-only")
def test_clear_still_preserves_userprofile():
    """clear=True wipes everything else but USERPROFILE survives."""
    real = os.environ['USERPROFILE']
    with patch_env({'USERNAME': 'ignored'}, clear=True):
        assert set(os.environ) == {'USERNAME', 'USERPROFILE'}
        assert os.environ['USERPROFILE'] == real
        assert os.environ['USERNAME'] == 'ignored'


@pytest.mark.skipif(not WINDOWS, reason="USERPROFILE handling is Windows-only")
def test_restores_environment_on_exit():
    """Patched values are reverted after the context manager exits."""
    original_username = os.environ.get('USERNAME')
    with patch_env({'USERNAME': 'ignored'}):
        pass
    assert os.environ.get('USERNAME') == original_username


# --- error cases ---


def test_raises_when_userprofile_in_values_dict():
    """Passing USERPROFILE in the values dict raises ValueError."""
    with pytest.raises(ValueError, match="USERPROFILE"):
        patch_env({'USERPROFILE': 'C:\\fake'})


def test_raises_when_userprofile_as_kwarg():
    """Passing USERPROFILE as a keyword argument raises ValueError."""
    with pytest.raises(ValueError, match="USERPROFILE"):
        patch_env(USERPROFILE='C:\\fake')


# --- non-Windows passthrough ---


@pytest.mark.skipif(WINDOWS, reason="tests non-Windows passthrough")
def test_non_windows_is_plain_passthrough():
    """On non-Windows, patch_env does not touch USERPROFILE."""
    original = os.environ.get('USERPROFILE')
    with patch_env({'SOME_VAR': 'x'}):
        assert os.environ['SOME_VAR'] == 'x'
        assert os.environ.get('USERPROFILE') == original


# --- additional contract coverage ---


def test_kwargs_form_sets_variable():
    """Non-USERPROFILE kwargs flow through to patch.dict."""
    with patch_env(SOME_VAR='x'):
        assert os.environ['SOME_VAR'] == 'x'


def test_no_args_is_valid():
    """patch_env() with no arguments is a valid no-op patch."""
    with patch_env():
        pass
