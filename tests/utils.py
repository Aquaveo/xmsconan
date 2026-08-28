"""Helpers for patching os.environ in tests."""
import os
from unittest.mock import patch


_IS_WINDOWS = os.name == 'nt'


def patch_env(values=(), clear=False, **kwargs):
    """Drop-in for patch.dict(os.environ, ...) that preserves USERPROFILE on Windows.

    Use as a decorator or context manager, same as patch.dict:

        @patch_env({'USERNAME': ''})
        def test_...(): ...

        with patch_env({'USERNAME': ''}):
            ...

    On Windows, USERPROFILE is implicitly added to the patched values so it remains
    set during the test. This matters when the test runs a subprocess or anything
    resolves the user's home directory. On other platforms this is a plain passthrough.
    """
    values_dict = dict(values)
    if 'USERPROFILE' in values_dict or 'USERPROFILE' in kwargs:
        # I wasn't sure what semantics to apply in this case and didn't need it.
        raise ValueError('patch_env does not support passing USERPROFILE explicitly. ')
    if _IS_WINDOWS:
        values_dict['USERPROFILE'] = os.environ['USERPROFILE']
    return patch.dict(os.environ, values_dict, clear=clear, **kwargs)
