"""Tests for the one version resolver every generated CI command reads."""
from unittest.mock import patch

import pytest

from xmsconan.generator_tools.version import FALLBACK_VERSION, is_release_version, resolve_version

# Both hosts' tag variables at once, so the precedence cases below can show
# which one wins when every source has an answer.
TAGGED_GITLAB = {"GITLAB_CI": "true", "CI_COMMIT_TAG": "7.1.0"}
TAGGED_GITHUB = {"GITHUB_ACTIONS": "true", "GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "7.2.0"}
SCM_VERSION = "1.2.3.dev4+gabcdef0"


@pytest.mark.parametrize("explicit, environ, scm, expected, asks_scm", [
    pytest.param("9.9.9", {**TAGGED_GITLAB, **TAGGED_GITHUB}, SCM_VERSION, "9.9.9", False,
                 id="flag-beats-everything"),
    pytest.param(None, TAGGED_GITLAB, SCM_VERSION, "7.1.0", False, id="gitlab-tag"),
    pytest.param(None, TAGGED_GITHUB, SCM_VERSION, "7.2.0", False, id="github-tag"),
    pytest.param(None, {**TAGGED_GITLAB, **TAGGED_GITHUB}, SCM_VERSION, "7.1.0", False,
                 id="gitlab-tag-before-github-tag"),
    pytest.param(None, {"GITLAB_CI": "true"}, SCM_VERSION, FALLBACK_VERSION, False,
                 id="untagged-gitlab-job"),
    pytest.param(None, {"GITHUB_ACTIONS": "true", "GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main"},
                 SCM_VERSION, FALLBACK_VERSION, False, id="github-branch-is-not-a-tag"),
    pytest.param(None, {"GITHUB_REF_NAME": "7.2.0"}, SCM_VERSION, SCM_VERSION, True,
                 id="github-ref-name-without-a-type-is-ignored"),
    pytest.param(None, {}, SCM_VERSION, SCM_VERSION, True, id="checkout"),
    pytest.param(None, {}, LookupError("no scm version"), FALLBACK_VERSION, True, id="no-scm-version"),
    pytest.param(None, {"CI_COMMIT_TAG": "", "GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "", "GITLAB_CI": ""},
                 SCM_VERSION, SCM_VERSION, True, id="empty-values-count-as-unset"),
    pytest.param("", {}, SCM_VERSION, SCM_VERSION, True, id="empty-flag-counts-as-unset"),
])
def test_resolve_version_takes_the_first_source_that_answers(explicit, environ, scm, expected, asks_scm):
    """The flag, then the CI tag, then 0.0.0 on an untagged job, then setuptools-scm.

    ``asks_scm`` is the half that matters on a runner: an untagged job has
    always built 0.0.0, and setuptools-scm cannot reproduce that from a CI
    checkout -- a shallow clone makes it invent ``0.1.devN`` -- so on either
    host's job the resolver must answer before it gets there.
    """
    with patch("xmsconan.generator_tools.version.get_version", side_effect=[scm]) as get_version:
        assert resolve_version(explicit, environ=environ) == expected

    assert get_version.called is asks_scm


def test_resolve_version_reads_the_process_environment_by_default(monkeypatch):
    """Without an ``environ`` the resolver reads ``os.environ``, which is what every CLI does."""
    monkeypatch.setenv("CI_COMMIT_TAG", "7.3.0")

    assert resolve_version() == "7.3.0"


@pytest.mark.parametrize("version, expected", [
    pytest.param("7.0.0", True, id="release"),
    pytest.param("7.0.1.dev3+gabcdef0", True, id="scm-dev-version"),
    pytest.param(FALLBACK_VERSION, False, id="fallback"),
    pytest.param("*", False, id="glob"),
    pytest.param("7.*", False, id="partial-glob"),
    pytest.param("", False, id="empty"),
    pytest.param(None, False, id="none"),
])
def test_is_release_version(version, expected):
    """The rule every uploading command applies: one release, not the fallback and not a glob."""
    assert is_release_version(version) is expected
