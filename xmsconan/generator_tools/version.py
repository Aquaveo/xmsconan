"""Version resolution for xmsconan generators."""
import logging
import os

from setuptools_scm import get_version

LOGGER = logging.getLogger(__name__)

FALLBACK_VERSION = "0.0.0"

# What every --version flag says about its default, so five commands cannot
# describe five different orders.
VERSION_FLAG_HELP = ("Default: the CI tag (CI_COMMIT_TAG, or GITHUB_REF_NAME on a tag), "
                     "0.0.0 on an untagged CI job, else setuptools-scm.")

# GitLab exports CI_COMMIT_TAG on a tag pipeline and nothing otherwise.
# GitHub Actions sets GITHUB_REF_NAME on every run -- a branch name as often
# as a tag -- so it only counts when GITHUB_REF_TYPE says which it is.
GITLAB_TAG_VARIABLE = "CI_COMMIT_TAG"
GITHUB_REF_NAME_VARIABLE = "GITHUB_REF_NAME"
GITHUB_REF_TYPE_VARIABLE = "GITHUB_REF_TYPE"
# Set to "true" by the host on every job, tagged or not.
CI_MARKER_VARIABLES = ("GITLAB_CI", "GITHUB_ACTIONS")


def resolve_version(explicit_version=None, environ=None):
    """Resolve the build version: the flag, then the CI tag, then setuptools-scm.

    In order:

    1. ``explicit_version`` -- the ``--version`` flag.
    2. The CI tag: ``CI_COMMIT_TAG`` on GitLab, or ``GITHUB_REF_NAME`` on
       GitHub Actions when ``GITHUB_REF_TYPE`` is ``tag``.
    3. ``0.0.0`` on an untagged CI job (``GITLAB_CI`` or ``GITHUB_ACTIONS``
       is set). Every untagged pipeline has always built ``0.0.0``, and
       setuptools-scm cannot reproduce that from a runner's checkout: a
       shallow clone makes it invent ``0.1.devN``, and a full one would
       stamp a dev version onto packages the pipeline downloads, restores
       and names under the fallback.
    4. setuptools-scm, for a developer's checkout.
    5. ``0.0.0``.

    Every generated CI command reads the version this way, so ``xmsconan_gen``,
    ``build.py``, ``xmsconan_coverage`` and ``xmsconan_conan_deploy`` agree on
    it without a job exporting a variable between them.

    Args:
        explicit_version: Version string from --version flag, or None.
        environ: The environment to read; ``os.environ`` when None. Empty
            values count as unset.

    Returns:
        A version string.
    """
    if explicit_version:
        LOGGER.debug("Using explicit version: %s", explicit_version)
        return explicit_version

    environ = os.environ if environ is None else environ
    gitlab_tag = environ.get(GITLAB_TAG_VARIABLE)
    if gitlab_tag:
        LOGGER.info("Version from %s: %s", GITLAB_TAG_VARIABLE, gitlab_tag)
        return gitlab_tag
    github_ref = environ.get(GITHUB_REF_NAME_VARIABLE)
    if github_ref and environ.get(GITHUB_REF_TYPE_VARIABLE) == "tag":
        LOGGER.info("Version from %s: %s", GITHUB_REF_NAME_VARIABLE, github_ref)
        return github_ref
    if any(environ.get(marker) for marker in CI_MARKER_VARIABLES):
        LOGGER.info("Untagged CI job: using fallback version %s", FALLBACK_VERSION)
        return FALLBACK_VERSION

    try:
        version = get_version()
        LOGGER.info("Version from setuptools-scm: %s", version)
        return version
    except LookupError:
        LOGGER.debug("setuptools-scm read no version here, using fallback")

    LOGGER.info("Using fallback version: %s", FALLBACK_VERSION)
    return FALLBACK_VERSION


def is_release_version(version):
    """Whether ``version`` names exactly one publishable version.

    ``0.0.0`` is what an untagged pipeline resolves to, and what a tree
    setuptools-scm can read no version from falls back to; neither names a
    version anyone asked for. A glob is what ``build.py --version`` once
    defaulted to, and ``conan upload <lib>/*`` matched every version of the
    library in the local cache. Every command that uploads asks this before
    it does, so the rule is written once.

    A setuptools-scm dev version (``7.0.1.dev3``) passes, deliberately: it
    names one version, and between tags it is the only thing a checkout can
    resolve to, so rejecting it would take deliberate pre-release publishing
    away -- including the explicit ``--version 7.0.1.dev3`` the error message
    tells the caller to pass. No CI job reaches it either way, since a
    pipeline resolves a tag or the fallback. Which versions an index accepts
    is that index's policy, not this predicate's.
    """
    return bool(version) and version != FALLBACK_VERSION and "*" not in version
