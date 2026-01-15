"""Conan package tools."""
__version__ = '0.0.0'


def get_client_version():
    """Get the client version."""
    from conans.model.version import Version
    from conan import __version__ as client_version
    # It is a mess comparing dev versions, lets assume that the -dev is the further release
    return Version(client_version.replace("-dev", ""))
