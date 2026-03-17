"""Version resolution for xmsconan generators."""
import logging

from setuptools_scm import get_version

LOGGER = logging.getLogger(__name__)

FALLBACK_VERSION = "0.0.0"


def resolve_version(explicit_version=None):
    """Resolve the build version from explicit value, git tag, or fallback.

    Args:
        explicit_version: Version string from --version flag, or None.

    Returns:
        A version string.
    """
    if explicit_version:
        LOGGER.debug("Using explicit version: %s", explicit_version)
        return explicit_version

    try:
        version = get_version()
        LOGGER.info("Version from setuptools-scm: %s", version)
        return version
    except LookupError:
        LOGGER.debug("No git tag found, using fallback")

    LOGGER.info("Using fallback version: %s", FALLBACK_VERSION)
    return FALLBACK_VERSION
