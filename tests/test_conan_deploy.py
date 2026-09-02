"""Tests for ci_tools.conan_deploy."""
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

from xmsconan.ci_tools.conan_deploy import conan_deploy, main


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_save_selects_binaries_not_just_the_recipe(mock_run):
    """--save passes `ref:*` so the tarball carries the binaries.

    A bare `pkg/version` is a recipe-only pattern to `conan cache save`. Passing it
    produced a tarball with the recipe and no packages, so the deploy job restored
    an empty package set and uploaded only a recipe -- consumers then failed with
    "Missing binary" even though the reference resolved. The `:*` is the fix.
    """
    conan_deploy("xmscore", "7.0.0", save="xmscore-7.0.0.tar.gz")

    mock_run.assert_called_once_with(
        [
            "conan", "cache", "save",
            "--file", "xmscore-7.0.0.tar.gz",
            "xmscore/7.0.0:*",
        ],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_restore(mock_run):
    """--restore calls conan cache restore."""
    conan_deploy("xmscore", "7.0.0", restore="xmscore-7.0.0.tar.gz")

    mock_run.assert_called_once_with(
        ["conan", "cache", "restore", "xmscore-7.0.0.tar.gz"],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_upload(mock_run):
    """--upload calls conan upload with aquaveo remote."""
    conan_deploy("xmsgrid", "2.0.0", upload=True)

    mock_run.assert_called_once_with(
        ["conan", "upload", "xmsgrid/2.0.0:*", "-r", "aquaveo", "--confirm"],
        check=True,
    )


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_restore_and_upload(mock_run):
    """--restore + --upload runs both in order."""
    conan_deploy(
        "xmscore", "7.0.0",
        restore="pkg.tar.gz",
        upload=True,
    )

    assert mock_run.call_count == 2
    calls = mock_run.call_args_list
    # restore first
    assert "restore" in calls[0][0][0]
    # upload second
    assert "upload" in calls[1][0][0]


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_save_and_upload(mock_run):
    """--save + --upload runs both in order."""
    conan_deploy("xmscore", "7.0.0", save="out.tar.gz", upload=True)

    assert mock_run.call_count == 2
    calls = mock_run.call_args_list
    assert "save" in calls[0][0][0]
    assert "upload" in calls[1][0][0]


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_save_with_a_package_query_resolves_a_package_list_first(mock_run):
    """A query turns --save into `conan list` + `conan cache save --list`.

    ``conan cache save`` accepts a pattern *or* a package list, and only the
    list can be narrowed by a binary query -- so the pattern form cannot be
    used here at all. The Conan cache on a CI runner is per machine, not per
    job, so the pattern form would tarball a concurrent msvc 194 job's
    binaries under the same reference.

    The ``#*`` on the end of the pattern is what makes the resulting list
    usable. ``conan list "<ref>:*"`` reports each binary as an ``info`` block
    and nothing else; ``conan cache save`` needs a *package revision* to know
    which folder to archive, so a list without one saves the recipe and
    silently skips every binary. Adding ``#*`` asks for the package revisions
    too, and the entries gain a ``revisions`` key.

    The failure this pins is quiet end to end: the save exits 0 with a
    recipe-only tarball, the deploy stage restores it and uploads the recipe
    alone, also exiting 0, and the break only surfaces when a consumer resolves
    the reference and finds no binary. Aquaveo/data_objects 5.1.0 shipped that
    way -- pipeline 63341 green, `No packages found for this revision` on the
    remote. Branch pipelines cannot catch it because they never upload.
    """
    conan_deploy(
        "xmscore", "7.0.0", save="out.tar.gz", package_query="compiler.version=192",
    )

    assert mock_run.call_count == 2
    # Whole commands, not slices with the middle skipped: `-c` sits in that
    # middle, and it is the flag that keeps the list a list of *cached*
    # binaries. Deleting it passed a `[:3]` / `[-3:]` pair of assertions.
    list_command = mock_run.call_args_list[0][0][0]
    assert list_command == [
        "conan", "list", "xmscore/7.0.0:*#*", "-c",
        "-p", "compiler.version=192", "--format=json",
    ]

    save_command = mock_run.call_args_list[1][0][0]
    list_file = save_command[save_command.index("--list") + 1]
    assert save_command == [
        "conan", "cache", "save", "--list", list_file, "--file", "out.tar.gz",
    ]
    # The unrestricted pattern must not appear: it is what the query exists to
    # avoid, and passing both is a Conan error anyway.
    assert "xmscore/7.0.0:*" not in save_command


def _list_file_recorder(seen, fail=False):
    """Record the ``--list`` path each `conan cache save` is handed.

    Shared by the cleanup tests so the two do not drift apart, and so neither
    has to know the flag's argv position.
    """
    def record(command, **kwargs):
        if "--list" in command:
            seen.append(command[command.index("--list") + 1])
            if fail:
                raise subprocess.CalledProcessError(1, "conan")
    return record


@pytest.mark.parametrize("fail", [False, True], ids=["save-succeeds", "save-fails"])
@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_save_with_a_package_query_removes_the_list_file(mock_run, fail):
    """The resolved package list never outlives the save, on either path.

    It is a temp file rather than an artifact-tree file so a pipeline cannot
    pick up a stale list from a previous job and publish that job's package
    ids -- which only holds if the cleanup is unconditional.
    """
    seen = []
    mock_run.side_effect = _list_file_recorder(seen, fail=fail)

    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            conan_deploy(
                "xmscore", "7.0.0", save="out.tar.gz",
                package_query="compiler.version=192",
            )
    else:
        conan_deploy(
            "xmscore", "7.0.0", save="out.tar.gz", package_query="compiler.version=192",
        )

    assert len(seen) == 1
    assert not os.path.exists(seen[0])


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, "conan"), FileNotFoundError("conan")],
    ids=["conan-exits-nonzero", "conan-not-on-path"],
)
@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_package_list_removed_when_conan_list_fails(mock_run, error):
    """A failed resolve leaves no file behind, however `conan list` failed.

    An empty or partial list is worse than none: `conan cache save --list`
    would accept it and produce a tarball missing binaries, and the deploy that
    follows would publish that as a complete release. The first version caught
    ``CalledProcessError`` only, so a `conan` missing from PATH -- a
    ``FileNotFoundError`` out of the same call -- leaked the file.
    """
    tempfiles = []
    real_mkstemp = tempfile.mkstemp

    def spy(*args, **kwargs):
        handle, path = real_mkstemp(*args, **kwargs)
        tempfiles.append(path)
        return handle, path

    mock_run.side_effect = error
    with patch("xmsconan.ci_tools.conan_deploy.tempfile.mkstemp", side_effect=spy):
        with pytest.raises(type(error)):
            conan_deploy(
                "xmscore", "7.0.0", save="out.tar.gz",
                package_query="compiler.version=192",
            )

    assert len(tempfiles) == 1
    assert not os.path.exists(tempfiles[0])


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_upload_honors_the_remote_and_the_package_query(mock_run):
    """--upload sends only matching binaries, and to the named remote.

    `conan upload` matches by reference, so the query is what keeps msvc 194
    binaries sitting in a shared runner's cache from being published to the
    VS2019 remote -- silently, and with exit 0.
    """
    conan_deploy(
        "xmscore", "7.0.0", upload=True,
        remote="aquaveo-vs2019", package_query="compiler.version=192",
    )

    mock_run.assert_called_once_with(
        [
            "conan", "upload", "xmscore/7.0.0:*", "-r", "aquaveo-vs2019", "--confirm",
            "-p", "compiler.version=192",
        ],
        check=True,
    )


@pytest.mark.parametrize("package_query", [None, "compiler.version=192"])
@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_upload_selects_binaries_not_just_the_recipe(mock_run, package_query):
    """The upload pattern carries ``:*``, with or without a query.

    A bare ``pkg/version`` is a recipe-only pattern. Unqueried, conan uploads
    the binaries under it anyway; add ``-p`` and the query filters an empty set
    of packages, so the recipe goes up alone and the command exits 0. That
    published Aquaveo/xmsconstraint 6.0.12 with no Windows binaries on either
    remote while all five deploy jobs reported success.

    Asserted for the unqueried case too. That one is not broken today, but it
    is broken by the same pattern, and the queried case only differs by a flag
    -- so a future edit that "simplifies" one back to a bare reference should
    fail here rather than at the next release.
    """
    conan_deploy("xmscore", "7.0.0", upload=True, package_query=package_query)

    command, = mock_run.call_args.args
    assert "xmscore/7.0.0:*" in command
    assert "xmscore/7.0.0" not in command


@patch("xmsconan.ci_tools.conan_deploy.subprocess.run")
def test_no_action_does_nothing(mock_run):
    """No flags → no subprocess calls."""
    conan_deploy("xmscore", "7.0.0")

    mock_run.assert_not_called()


@patch(
    "xmsconan.ci_tools.conan_deploy.subprocess.run",
    side_effect=subprocess.CalledProcessError(1, "conan"),
)
def test_propagates_called_process_error(mock_run):
    """Verify CalledProcessError propagates to caller."""
    with pytest.raises(subprocess.CalledProcessError):
        conan_deploy("xmscore", "7.0.0", upload=True)


@patch("xmsconan.ci_tools.conan_deploy.conan_deploy")
def test_main_passes_flags_through(mock_deploy, monkeypatch):
    """main() forwards every CLI flag to conan_deploy()."""
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_deploy", "xmscore", "7.0.0",
        "--save", "out.tar.gz", "--restore", "in.tar.gz", "--upload",
        "--remote", "aquaveo-vs2019", "--package-query", "compiler.version=192",
    ])

    main()

    mock_deploy.assert_called_once_with(
        library="xmscore",
        version="7.0.0",
        save="out.tar.gz",
        restore="in.tar.gz",
        upload=True,
        remote="aquaveo-vs2019",
        package_query="compiler.version=192",
    )


@patch("xmsconan.ci_tools.conan_deploy.conan_deploy")
def test_main_requires_an_action(mock_deploy, monkeypatch):
    """No --save/--restore/--upload is a usage error, not a silent no-op."""
    monkeypatch.setattr(sys, "argv", ["xmsconan_conan_deploy", "xmscore", "7.0.0"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    mock_deploy.assert_not_called()


@patch(
    "xmsconan.ci_tools.conan_deploy.conan_deploy",
    side_effect=subprocess.CalledProcessError(3, "conan"),
)
def test_main_exits_with_conan_return_code(mock_deploy, monkeypatch):
    """A failing conan command sets the process exit code."""
    monkeypatch.setattr(sys, "argv", [
        "xmsconan_conan_deploy", "xmscore", "7.0.0", "--upload",
    ])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 3
