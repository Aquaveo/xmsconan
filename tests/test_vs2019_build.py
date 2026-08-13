"""Tests for build_tools.vs2019_build (the manual msvc 192 driver)."""
import json
import os
import re
import subprocess
from unittest import mock

import pytest

from xmsconan.build_tools import vs2019_build as vs
from .utils import patch_env


MODULE = "xmsconan.build_tools.vs2019_build"


def fake_packager(configurations=None, run_result=0):
    """Return a MagicMock standing in for XmsConanPackager."""
    packager = mock.MagicMock()
    packager.configurations = [{}] if configurations is None else configurations
    packager.run.return_value = run_result
    return packager


@pytest.fixture
def library_root(tmp_path):
    """Create <tmp>/xmscore/build.toml and return the root directory."""
    library_dir = tmp_path / "xmscore"
    library_dir.mkdir()
    (library_dir / "build.toml").write_text('library_name = "xmscore"\n', encoding="utf-8")
    return tmp_path


@pytest.fixture
def vswhere_installed(tmp_path, monkeypatch):
    """Point _vswhere_path() at a stub vswhere.exe under tmp_path."""
    installer = tmp_path / "Microsoft Visual Studio" / "Installer"
    installer.mkdir(parents=True)
    (installer / "vswhere.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    return tmp_path


# --- library table / selection ------------------------------------------


def test_library_table_is_in_dependency_order():
    """The stack is listed in dependency order and every entry explains itself.

    Deliberately *not* asserting which libraries are enabled. The flag is the
    one thing that moves as each library migrates to Conan 2, and both the
    module comment and README promise that enabling one is a single-flag
    change -- a test pinning today's flags would quietly make it two places.
    What must not drift is the *order*, because each library builds against
    the packages produced by the ones before it.
    """
    assert [library.name for library in vs.LIBRARIES] == [
        "xmscore", "xmsgrid", "xmsinterp", "xmsmesher",
        "xmsextractor", "xmsstamper", "xmsconstraint", "xmsgridtrace",
    ]
    assert all(library.note for library in vs.LIBRARIES)


def test_select_libraries_defaults_to_enabled():
    """No flags selects the enabled libraries, in table order.

    Run against a synthetic table for the same reason: the behavior under
    test is the filtering, not which libraries happen to be on today.
    """
    table = (
        vs.LibrarySpec("first", True),
        vs.LibrarySpec("second", False),
        vs.LibrarySpec("third", True),
    )
    with mock.patch(f"{MODULE}.LIBRARIES", table):
        assert [library.name for library in vs.select_libraries()] == ["first", "third"]


def test_select_libraries_only_overrides_enabled_flag():
    """--only builds a named library even while it is still disabled."""
    selected = vs.select_libraries(only=["xmsgrid", "xmscore"])
    # order follows the dependency order of LIBRARIES, not the flag order
    assert [library.name for library in selected] == ["xmscore", "xmsgrid"]


def test_select_libraries_from_truncates_the_stack():
    """--from drops everything before the named library."""
    selected = vs.select_libraries(only=["xmscore", "xmsinterp"], start_from="xmsgrid")
    assert [library.name for library in selected] == ["xmsinterp"]


def test_select_libraries_unknown_only():
    """An unknown --only name is a ValueError naming the known libraries."""
    with pytest.raises(ValueError, match="unknown library 'nope' for --only"):
        vs.select_libraries(only=["nope"])


def test_select_libraries_unknown_from():
    """An unknown --from name is a ValueError."""
    with pytest.raises(ValueError, match="unknown library 'nope' for --from"):
        vs.select_libraries(start_from="nope")


# --- password resolution -------------------------------------------------


@mock.patch(f"{MODULE}.load_conan_credentials", return_value={"username": "toml-user"})
def test_resolve_credentials_password_file_wins(mock_creds, tmp_path):
    """--password-file wins over the environment; the trailing newline is stripped."""
    password_file = tmp_path / "p.txt"
    password_file.write_text("s3cret\n", encoding="utf-8")
    with patch_env({"CONAN_PASSWORD": "from-env"}, clear=True):
        assert vs.resolve_credentials(str(password_file)) == ("toml-user", "s3cret")


@pytest.mark.parametrize("contents,message", [
    (None, "password file not found"),
    ("", "password file is empty"),
    ("   \n", "password file is empty"),
])
def test_resolve_credentials_rejects_an_unusable_password_file(contents, message, tmp_path):
    """An explicitly named password file never falls through to another source.

    Both halves matter. A typo'd path must not silently log you in with the
    password from ``~/.xmsconan.toml``, and neither must a file that exists
    but holds nothing -- the secret failed to land in it, and stripping the
    editor's newline leaves ``""``, which is falsy at exactly the two places
    the fallback is decided.
    """
    password_file = tmp_path / "p.txt"
    if contents is not None:
        password_file.write_text(contents, encoding="utf-8")
    with patch_env({"CONAN_PASSWORD": "from-env"}, clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials") as mock_creds, \
            pytest.raises(ValueError, match=message):
        vs.resolve_credentials(str(password_file))
    mock_creds.assert_not_called()


def test_resolve_credentials_unreadable_password_file(tmp_path):
    """A password file that cannot be read is a usage error, not an OSError.

    Escaping as an OSError would reach the CLI's launch-failure arm and be
    reported as ``conan`` missing from PATH, which is the wrong machine to go
    looking at.
    """
    password_file = tmp_path / "p.txt"
    password_file.write_text("s3cret\n", encoding="utf-8")
    with mock.patch("pathlib.Path.read_text", side_effect=PermissionError("denied")), \
            pytest.raises(ValueError, match="could not read password file"):
        vs.resolve_credentials(str(password_file))


def test_resolve_credentials_from_env():
    """The environment supplies both halves without touching the config file."""
    env = {"CONAN_PASSWORD": "from-env", "CONAN_LOGIN_USERNAME": "env-user"}
    with patch_env(env, clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials") as mock_creds:
        assert vs.resolve_credentials() == ("env-user", "from-env")
    mock_creds.assert_not_called()


@pytest.mark.parametrize("config,env,kwargs,expected", [
    ({"username": "toml-user", "password": "from-toml"}, {}, {}, ("toml-user", "from-toml")),
    ({}, {}, {}, ("aquaveo", None)),
    ({"password": "from-toml"}, {"CONAN_LOGIN_USERNAME": "env-user"}, {"username": "me"},
     ("me", "from-toml")),
], ids=["config-file-supplies-both", "no-source-supplies-anything", "explicit-username-wins"])
def test_resolve_credentials_falls_back_to_the_config_file(config, env, kwargs, expected):
    """~/.xmsconan.toml is the last resort, and it is read exactly once.

    The username used to be resolved separately in ci_tools.conan_setup, which
    both re-opened the file and -- because the caller always passed a truthy
    default -- meant a developer with a personal Artifactory account logged in
    as ``aquaveo`` with their own password. So each half falls back on its own:
    an explicit ``--username`` still leaves the file to supply the password,
    and a file supplying neither yields the shared username with no password
    at all, which is how conan is asked to prompt.
    """
    with patch_env(env, clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials", return_value=config) as mock_creds:
        credentials = vs.resolve_credentials(**kwargs)

    assert credentials == expected
    assert (credentials.username, credentials.password) == expected
    mock_creds.assert_called_once()


# --- preflight: visual studio -------------------------------------------


def test_vswhere_path_default_location():
    """Without ProgramFiles(x86) set, the canonical location is used."""
    with patch_env(clear=True):
        assert vs._vswhere_path() == os.path.join(
            r"C:\Program Files (x86)", "Microsoft Visual Studio",
            "Installer", "vswhere.exe",
        )


def test_check_visual_studio_missing_vswhere(tmp_path, monkeypatch):
    """No vswhere.exe -> actionable failure naming the expected path."""
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    check = vs.check_visual_studio_2019()
    assert not check.ok
    assert "vswhere.exe not found" in check.detail
    assert "Desktop development with C++" in check.detail


@mock.patch(f"{MODULE}.subprocess.run", side_effect=OSError("boom"))
def test_check_visual_studio_vswhere_fails(mock_run, vswhere_installed):
    """Vswhere blowing up is reported, not raised."""
    check = vs.check_visual_studio_2019()
    assert not check.ok
    assert "could not run vswhere" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_visual_studio_bad_json(mock_run, vswhere_installed):
    """Unparseable vswhere output is reported."""
    mock_run.return_value = mock.Mock(stdout="not json")
    check = vs.check_visual_studio_2019()
    assert not check.ok
    assert "could not parse vswhere output" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_visual_studio_no_instances(mock_run, vswhere_installed):
    """An empty instance list means no VS2019 with the C++ toolset is installed."""
    mock_run.return_value = mock.Mock(stdout="")
    check = vs.check_visual_studio_2019()
    assert not check.ok
    assert "no Visual Studio 2019 (16.x) installation with the C++ toolset" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_visual_studio_found_with_toolset(mock_run, vswhere_installed, tmp_path):
    """A found install reports its path, VS version, and MSVC toolset."""
    install_path = tmp_path / "VS2019"
    build_dir = install_path / "VC" / "Auxiliary" / "Build"
    build_dir.mkdir(parents=True)
    (build_dir / "Microsoft.VCToolsVersion.default.txt").write_text(
        "14.29.30133\n", encoding="utf-8"
    )
    mock_run.return_value = mock.Mock(stdout=json.dumps([
        {"installationPath": str(install_path), "installationVersion": "16.11.34"},
    ]))

    check = vs.check_visual_studio_2019()

    assert check.ok
    assert str(install_path) in check.detail
    assert "VS 16.11.34" in check.detail
    assert "MSVC toolset 14.29.30133" in check.detail
    argv = mock_run.call_args[0][0]
    # the version range filter is what keeps VS2022 from matching
    assert "[16.0,17.0)" in argv
    # ... and -requires is what keeps a .NET-only VS2019 from matching
    assert argv[argv.index("-requires") + 1] == vs.VC_TOOLS_COMPONENT


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_visual_studio_toolset_file_missing(mock_run, vswhere_installed, tmp_path):
    """A VS install with no MSVC toolset marker fails preflight.

    A missing ``Microsoft.VCToolsVersion.default.txt`` is precisely the "no
    C++ tools here" signal. Reporting it as "toolset unknown" with ok=True
    let a VS2019 carrying only the .NET workload pass preflight and then die
    on the first ``conan create``, hours into the run.
    """
    mock_run.return_value = mock.Mock(stdout=json.dumps([
        {"installationPath": str(tmp_path / "VS2019")},
    ]))
    check = vs.check_visual_studio_2019()
    assert not check.ok
    assert "carries no MSVC toolset" in check.detail
    assert "Desktop development with C++" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_visual_studio_toolset_file_unreadable(mock_run, vswhere_installed, tmp_path):
    """A marker file that exists but cannot be read names *that* fault.

    It is neither a missing C++ workload nor -- once the OSError escapes into
    the CLI's launch-failure arm -- ``conan`` missing from PATH. Reporting it
    as a normal failed check also keeps the remaining preflight checks running.
    """
    install_path = tmp_path / "VS2019"
    build_dir = install_path / "VC" / "Auxiliary" / "Build"
    build_dir.mkdir(parents=True)
    (build_dir / "Microsoft.VCToolsVersion.default.txt").write_text("14.29", encoding="utf-8")
    mock_run.return_value = mock.Mock(stdout=json.dumps([
        {"installationPath": str(install_path)},
    ]))

    with mock.patch("builtins.open", side_effect=PermissionError("denied")):
        check = vs.check_visual_studio_2019()

    assert not check.ok
    assert "could not read" in check.detail
    assert "Desktop development with C++" not in check.detail


# --- preflight: conan version / remote ----------------------------------


@mock.patch(f"{MODULE}.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "conan"))
def test_check_conan_version_not_runnable(mock_run):
    """A conan client that will not run is reported with the pip fix."""
    check = vs.check_conan_version()
    assert not check.ok
    assert 'pip install "conan~=2.31.0"' in check.detail


@pytest.mark.parametrize("stdout,ok,expected", [
    ("Conan version unknown", False, ["could not parse a version"]),
    ("Conan version 2.32.0", False, ["outside the pinned ~=2.31.0 series", "package_id"]),
    ("Conan version 2.31.2\n", True, ["2.31.2"]),
], ids=["unparseable", "outside-pinned-series", "patch-within-series"])
@mock.patch(f"{MODULE}.subprocess.run")
def test_check_conan_version(mock_run, stdout, ok, expected):
    """Only a 2.31.x patch level passes; anything else is reported, not raised.

    A minor bump is a failure rather than a warning because it can change
    package_id computation and silently detach these hand-built packages from
    the binaries already on the remote.
    """
    mock_run.return_value = mock.Mock(stdout=stdout)

    check = vs.check_conan_version()

    assert check.ok is ok
    assert all(text in check.detail for text in expected)


@mock.patch(f"{MODULE}.subprocess.run", side_effect=OSError("no conan"))
def test_check_remote_list_fails(mock_run):
    """`conan remote list` blowing up is reported, not raised."""
    check = vs.check_remote_configured()
    assert not check.ok
    assert "could not run `conan remote list`" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_remote_not_configured(mock_run):
    """A remote list without the VS2019 remote points at `setup`."""
    mock_run.return_value = mock.Mock(stdout="aquaveo: https://example.com [Enabled: True]\n")
    check = vs.check_remote_configured()
    assert not check.ok
    assert "xmsconan_vs2019 setup" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_remote_configured(mock_run):
    """The remote is found by its `name:` prefix, not a substring match."""
    mock_run.return_value = mock.Mock(stdout=(
        "aquaveo: https://example.com [Verify SSL: True, Enabled: True]\n"
        "aquaveo-vs2019: https://example.com [Verify SSL: True, Enabled: True]\n"
    ))
    assert vs.check_remote_configured().ok


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_remote_disabled(mock_run):
    """A configured but disabled remote fails preflight with the enable command.

    ``conan remote list`` keeps printing a disabled remote, so matching the
    name alone passed a remote that then failed every single download.
    """
    mock_run.return_value = mock.Mock(stdout=(
        "aquaveo-vs2019: https://example.com [Verify SSL: True, Enabled: False]\n"
    ))
    check = vs.check_remote_configured()
    assert not check.ok
    assert "conan remote enable aquaveo-vs2019" in check.detail


def test_run_preflight_prints_each_check(capsys):
    """run_preflight prints one OK/FAIL line per check and returns them."""
    with mock.patch(f"{MODULE}.check_visual_studio_2019",
                    return_value=vs.CheckResult("vs", True, "found")), \
            mock.patch(f"{MODULE}.check_conan_version",
                       return_value=vs.CheckResult("conan", False, "too new")), \
            mock.patch(f"{MODULE}.check_remote_configured",
                       return_value=vs.CheckResult("remote", True, "configured")) as remote:
        checks = vs.run_preflight(remote="custom")

    remote.assert_called_once_with("custom")
    assert [check.ok for check in checks] == [True, False, True]
    out = capsys.readouterr().out
    assert "[OK] vs: found" in out
    assert "[FAIL] conan: too new" in out


# --- setup ---------------------------------------------------------------


@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("x", True, "")])
@mock.patch(f"{MODULE}.conan_setup")
def test_setup_appends_vs2019_remote(mock_conan_setup, mock_preflight, tmp_path, capsys):
    """Setup appends the vs2019 remote, logs in, and never echoes the password.

    ``index=None`` is the point: inserting this remote at index 0 would make
    it the first stop for every ``conan install`` on the developer's machine,
    so a version range could resolve to a VS2019-only package during ordinary
    msvc 194 work -- exactly the mixing the separate remote exists to prevent.
    """
    password_file = tmp_path / "p.txt"
    password_file.write_text("s3cret\n", encoding="utf-8")

    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials", return_value={}):
        assert vs.setup(password_file=str(password_file)) == 0

    mock_conan_setup.assert_called_once_with(
        remote_url=vs.VS2019_REMOTE_URL,
        login=True,
        username="aquaveo",
        password="s3cret",
        remote_name="aquaveo-vs2019",
        index=None,
        use_config_file=False,
    )
    mock_preflight.assert_called_once_with(remote="aquaveo-vs2019")
    assert "s3cret" not in capsys.readouterr().out


@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("x", False, "")])
@mock.patch(f"{MODULE}.conan_setup")
def test_setup_returns_exit_usage_on_preflight_failure(mock_conan_setup, mock_preflight):
    """A failed preflight is "the machine was wrong", which is exit 2.

    ``build`` returns EXIT_USAGE for the identical condition, and the module
    docstring assigns it 2; ``setup`` returning 1 made the same failure mean
    "a library failed to build" to any wrapper script reading the code.
    """
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials", return_value={}):
        assert vs.setup() == vs.EXIT_USAGE


@mock.patch(f"{MODULE}.run_preflight")
@mock.patch(f"{MODULE}.conan_setup", side_effect=FileNotFoundError("conan"))
def test_setup_reports_a_conan_that_will_not_start(mock_conan_setup, mock_preflight):
    """A conan that will not launch is a ToolNotFoundError naming PATH."""
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials", return_value={}), \
            pytest.raises(vs.ToolNotFoundError, match=r"could not run conan .*Is it on PATH\?"):
        vs.setup()

    mock_preflight.assert_not_called()


# --- preview -------------------------------------------------------------


def _table_rows(out):
    """Return the data rows of the printed configuration table."""
    return [
        line for line in out.splitlines()
        if line.startswith("|") and "compiler.version" not in line
    ]


@patch_env(clear=True)
def test_preview_generates_the_verified_matrix(capsys):
    """The VS2019 matrix is 14 configs: 4 base + 4 wchar_t + 4 testing + 2 pybind."""
    exit_code = vs.preview(
        [vs.LibrarySpec("xmscore", True)], python_versions=vs.DEFAULT_PYTHON_VERSIONS,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "==> xmscore: 14 configuration(s)" in out
    # compiler.version is a per-row column, so the claim being made is that
    # *every* row is msvc 192 -- not that the digits appear somewhere.
    rows = _table_rows(out)
    assert len(rows) == 14
    assert all("192" in row for row in rows)


@patch_env(clear=True)
def test_preview_applies_filter(capsys):
    """--filter narrows the previewed matrix.

    Debug keeps half of each non-pybind group and drops pybind entirely
    (it is Release-only): 2 base + 2 wchar_t + 2 testing + 0 pybind = 6.
    """
    vs.preview([vs.LibrarySpec("xmscore", True)], config_filter={"build_type": "Debug"})

    out = capsys.readouterr().out
    assert "==> xmscore: 6 configuration(s)" in out
    assert len(_table_rows(out)) == 6


# --- build_library -------------------------------------------------------


#: The spec the loop in build() hands to build_library.
XMSCORE = vs.LibrarySpec("xmscore", True)
XMSGRID = vs.LibrarySpec("xmsgrid", False)


def test_build_library_missing_checkout(tmp_path):
    """A missing library directory is skipped, not fatal."""
    result = vs.build_library(XMSGRID, str(tmp_path))
    assert result.status == "skipped"
    assert "no checkout at" in result.message


def test_build_library_missing_build_toml(tmp_path):
    """A checkout without build.toml is skipped, not fatal."""
    (tmp_path / "xmsgrid").mkdir()
    result = vs.build_library(XMSGRID, str(tmp_path))
    assert result.status == "skipped"
    assert "no build.toml" in result.message


@mock.patch(f"{MODULE}.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "xmsconan_gen"))
def test_build_library_generator_failure(mock_run, library_root):
    """A failing xmsconan_gen fails the library without building."""
    result = vs.build_library(XMSCORE, str(library_root))
    assert result.status == "failed"
    assert "xmsconan_gen failed" in result.message


@mock.patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError("xmsconan_gen"))
def test_build_library_generator_missing(mock_run, library_root):
    """An xmsconan_gen that will not start aborts the run instead of failing one library.

    It would fail identically for every remaining library, so it is the
    machine being wrong (exit 2), not a build failure (exit 1) -- and the
    message has to name the tool that is actually missing.
    """
    with pytest.raises(vs.ToolNotFoundError, match=r"could not run xmsconan_gen .*PATH"):
        vs.build_library(XMSCORE, str(library_root))


@mock.patch(f"{MODULE}.XmsConanPackager")
@mock.patch(f"{MODULE}.subprocess.run")
def test_build_library_success(mock_run, mock_packager_cls, library_root):
    """The happy path generates, builds, and records per-config counts."""
    packager = fake_packager(configurations=[{}] * 14, run_result=0)
    mock_packager_cls.return_value = packager

    result = vs.build_library(
        XMSCORE, str(library_root), version="7.0.0",
        python_versions=["3.10", "3.13"], log_dir="logs",
    )

    mock_run.assert_called_once_with(
        ["xmsconan_gen", "--version", "7.0.0", "build.toml"],
        cwd=os.path.join(str(library_root), "xmscore"), check=True,
    )
    # both of these are required: neither is implied by the platform key
    mock_packager_cls.assert_called_once_with(
        "xmscore",
        conanfile_path=os.path.join(str(library_root), "xmscore", "conanfile.py"),
        build_missing=True,
        apply_boost_defaults=False,
        python_versions=["3.10", "3.13"],
    )
    packager.generate_configurations.assert_called_once_with("windows_vs2019")
    packager.filter_configurations.assert_not_called()
    packager.run.assert_called_once_with(log_dir="logs")
    assert (result.status, result.attempted, result.succeeded, result.failed) == \
        ("ok", 14, 14, 0)


@mock.patch(f"{MODULE}.XmsConanPackager")
@mock.patch(f"{MODULE}.subprocess.run")
def test_build_library_no_generate_and_filter(mock_run, mock_packager_cls, library_root):
    """--no-generate skips xmsconan_gen; --filter reaches the packager."""
    packager = fake_packager(configurations=[{}, {}], run_result=1)
    mock_packager_cls.return_value = packager

    result = vs.build_library(
        XMSCORE, str(library_root), generate=False,
        config_filter={"build_type": "Release"},
    )

    mock_run.assert_not_called()
    packager.filter_configurations.assert_called_once_with({"build_type": "Release"})
    assert (result.status, result.attempted, result.succeeded, result.failed) == \
        ("failed", 2, 1, 1)


@mock.patch(f"{MODULE}.XmsConanPackager")
@mock.patch(f"{MODULE}.subprocess.run")
def test_build_library_filter_matches_nothing(mock_run, mock_packager_cls, library_root):
    """A filter that matches no configuration skips the library."""
    packager = fake_packager(configurations=[])
    mock_packager_cls.return_value = packager

    result = vs.build_library(XMSCORE, str(library_root))

    packager.run.assert_not_called()
    assert result.status == "skipped"
    assert "no configurations matched" in result.message


# --- build ---------------------------------------------------------------


def test_build_stops_after_a_failure(capsys):
    """Without --continue-on-error, a failed library ends the run."""
    results = [
        vs.LibraryResult("xmscore", "failed"),
        vs.LibraryResult("xmsgrid", "ok"),
    ]
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.build_library", side_effect=results) as build_library:
        built = vs.build([XMSCORE, XMSGRID], "root")

    assert build_library.call_count == 1
    # the whole spec is handed down, not just the name: build_library reads
    # LibrarySpec.name itself now
    assert build_library.call_args[0][0] is XMSCORE
    assert [r.name for r in built] == ["xmscore"]
    assert "Stopping after xmscore failed" in capsys.readouterr().out


def test_build_continue_on_error():
    """--continue-on-error moves on to the next library after one fails."""
    results = [
        vs.LibraryResult("xmscore", "failed"),
        vs.LibraryResult("xmsgrid", "ok"),
    ]
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.build_library", side_effect=results):
        built = vs.build([XMSCORE, XMSGRID], "root", continue_on_error=True)

    assert [r.name for r in built] == ["xmscore", "xmsgrid"]


def test_build_exports_xms_version():
    """--version is exported as XMS_VERSION so it reaches each profile's [buildenv]."""
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.build_library",
                       return_value=vs.LibraryResult("xmscore", "ok")):
        vs.build([XMSCORE], "root", version="7.0.0")
        assert os.environ["XMS_VERSION"] == "7.0.0"


# --- summary -------------------------------------------------------------


def test_print_summary_reports_counts_and_exit_code(capsys):
    """The summary table lists every library; one built library means exit 0."""
    exit_code = vs.print_summary([
        vs.LibraryResult("xmscore", "ok", attempted=14, succeeded=14, elapsed=61.25),
        vs.LibraryResult("xmsgrid", "skipped", message="no checkout"),
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "xmscore" in out and "61.2s" in out and "no checkout" in out

    assert vs.print_summary([vs.LibraryResult("xmscore", "failed", failed=2)]) == 1


def test_print_summary_exits_nonzero_when_nothing_was_built(capsys):
    """Every library skipped is exit 3, not exit 0.

    A typo'd --root skips every library and used to exit 0, so any wrapper
    script or ``&&`` chain read "nothing happened" as "the stack is built".
    """
    exit_code = vs.print_summary([
        vs.LibraryResult("xmscore", "skipped", message="no checkout at E:\\typo\\xmscore"),
    ])

    assert exit_code == vs.EXIT_NOTHING_BUILT
    assert "nothing was built" in capsys.readouterr().err


# --- upload --------------------------------------------------------------


@pytest.mark.parametrize("upload_result", [0, 1])
@mock.patch(f"{MODULE}.XmsConanPackager")
def test_upload_targets_the_vs2019_remote(mock_packager_cls, upload_result):
    """Upload sends only msvc 192 binaries to the vs2019 remote.

    The package query is what keeps a mixed local cache -- the VS2019 build
    box is usually also a normal msvc 194 dev machine -- from leaking other
    toolchains onto the remote, because ``conan upload`` otherwise matches by
    reference alone. The packager's exit code is propagated so a failed
    publish never exits 0.
    """
    packager = fake_packager()
    packager.upload.return_value = upload_result
    mock_packager_cls.return_value = packager

    assert vs.upload("xmscore", "7.0.0") == upload_result

    packager.upload.assert_called_once_with(
        "7.0.0", remote="aquaveo-vs2019", package_query="compiler.version=192",
    )


@mock.patch(f"{MODULE}.XmsConanPackager")
def test_upload_refuses_a_foreign_remote(mock_packager_cls):
    """--remote aquaveo is one word from --remote aquaveo-vs2019, so it is refused."""
    with pytest.raises(ValueError, match="refusing to upload msvc 192 packages"):
        vs.upload("xmscore", "7.0.0", remote="aquaveo")

    mock_packager_cls.assert_not_called()


@mock.patch(f"{MODULE}.XmsConanPackager")
def test_upload_allows_a_foreign_remote_when_asked(mock_packager_cls):
    """--allow-other-remote is the explicit opt-out of that guard."""
    packager = fake_packager()
    packager.upload.return_value = 0
    mock_packager_cls.return_value = packager

    assert vs.upload("xmscore", "7.0.0", remote="scratch", allow_other_remote=True) == 0

    assert packager.upload.call_args.kwargs["remote"] == "scratch"


@mock.patch(f"{MODULE}.XmsConanPackager")
def test_upload_reports_a_conan_that_will_not_start(mock_packager_cls):
    """A conan that will not launch is a ToolNotFoundError naming PATH.

    Upload runs no preflight, so this is the first thing that touches conan.
    """
    packager = fake_packager()
    packager.upload.side_effect = FileNotFoundError("conan")
    mock_packager_cls.return_value = packager

    with pytest.raises(vs.ToolNotFoundError, match=r"could not run conan .*Is it on PATH\?"):
        vs.upload("xmscore", "7.0.0")


# --- filter parsing ------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    (None, None),
    ("", None),
    ('{"build_type": "Release"}', {"build_type": "Release"}),
])
def test_parse_filter_accepts(text, expected):
    """Empty filters are None; a JSON object parses to a dict."""
    assert vs._parse_filter(text) == expected


@pytest.mark.parametrize("text,message", [
    ("{oops", "not valid JSON"),
    ('["Release"]', "must be a JSON object"),
])
def test_parse_filter_rejects(text, message):
    """Malformed or non-object filters raise ValueError."""
    with pytest.raises(ValueError, match=message):
        vs._parse_filter(text)


# --- CLI -----------------------------------------------------------------


def run_main(argv):
    """Call main(argv) and return the exit code it raises."""
    with pytest.raises(SystemExit) as exc_info:
        vs.main(argv)
    return exc_info.value.code


def _rendered_help(argv, capsys):
    """Return the rendered ``--help`` output for ``argv``.

    ``COLUMNS`` is forced wide because argparse line-wraps help text to the
    terminal width, and a wrapped example path would break in the middle and
    read as an absent placeholder to any assertion below.
    """
    with patch_env({"COLUMNS": "200"}):
        run_main(argv + ["--help"])
    return capsys.readouterr().out


#: Absolute paths rooted in somebody's home directory: C:\Users\<name>\...,
#: /home/<name>/..., /Users/<name>/....  A help example must be a placeholder,
#: never a path that only exists on the machine the tool was written on.
_HOME_PATH_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/]|/home/|/Users/", re.IGNORECASE)


def test_help_carries_no_developer_specific_paths(capsys):
    r"""Every flag's help text uses placeholders, not one machine's real paths.

    The published ``--help`` is read by everyone who runs the tool, so an
    example naming a real credential location on one workstation is both
    noise and a bad hint.

    Asserting the general shape rather than one past offender's name: a
    literal ``"Claude" not in text`` check passes cleanly for a help string
    reading ``C:\\Users\\someone\\secrets\\conan.txt``.
    """
    text = "\n".join(
        _rendered_help(argv, capsys)
        for argv in ([], ["setup"], ["build"], ["upload"])
    )

    assert r"C:\path\to\conan-password.txt" in text, \
        "the --password-file example is missing (or was wrapped)"
    assert not _HOME_PATH_RE.search(text), "help text names a real home directory"


@mock.patch(f"{MODULE}.setup", return_value=0)
def test_main_setup_defaults(mock_setup):
    """`setup` leaves the username unresolved and defaults the remote; no path is baked in."""
    assert run_main(["setup"]) == 0
    mock_setup.assert_called_once_with(
        password_file=None, username=None, remote_url=vs.VS2019_REMOTE_URL,
        remote_name=vs.VS2019_REMOTE_NAME,
    )


@mock.patch(f"{MODULE}.setup", return_value=0)
def test_main_setup_remote_name_is_reachable(mock_setup):
    """--remote-name pairs with --remote-url so a redirected setup is named right."""
    assert run_main([
        "setup", "--remote-url", "https://example.com/scratch",
        "--remote-name", "scratch", "--username", "me",
    ]) == 0
    assert mock_setup.call_args.kwargs["remote_name"] == "scratch"
    assert mock_setup.call_args.kwargs["username"] == "me"


#: A complete `upload` invocation; both flags are required, never defaulted.
UPLOAD_ARGV = ["upload", "--library", "xmscore", "--version", "7.0.0"]


@pytest.mark.parametrize("verb,exception,argv,exit_code,message", [
    ("setup", vs.ToolNotFoundError("could not run conan (x). Is it on PATH?"),
     ["setup"], 2, "Is it on PATH?"),
    ("setup", ValueError("password file not found: p"),
     ["setup", "--password-file", "p"], 2, "password file not found"),
    ("setup", subprocess.CalledProcessError(3, "conan"), ["setup"], 3, "conan setup failed"),
    ("build", vs.ToolNotFoundError("could not run xmsconan_gen (x). Is it on PATH?"),
     ["build", "--root", "."], 2, "Is it on PATH?"),
    ("upload", vs.ToolNotFoundError("could not run conan (x). Is it on PATH?"),
     UPLOAD_ARGV, 2, "Is it on PATH?"),
    ("upload", ValueError("refusing to upload"),
     UPLOAD_ARGV + ["--remote", "aquaveo"], 2, "refusing to upload"),
], ids=[
    "setup-conan-not-on-path", "setup-bad-password-file", "setup-conan-exit-code",
    "build-generator-not-on-path", "upload-conan-not-on-path", "upload-refused-remote",
])
@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
def test_main_reports_a_failing_verb(mock_preflight, verb, exception, argv, exit_code,
                                     message, capsys):
    """Every subcommand turns a failure into a message on stderr, not a traceback.

    An executable that will not start and a bad request are both "exit 2" by
    the module's exit-code contract; a conan command that ran and failed
    propagates its own code instead.
    """
    with mock.patch(f"{MODULE}.{verb}", side_effect=exception):
        assert run_main(argv) == exit_code

    assert message in capsys.readouterr().err


@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
@mock.patch(f"{MODULE}.build",
            side_effect=PermissionError("[Errno 13] Permission denied: 'profile.txt'"))
def test_main_build_reports_an_io_error_for_what_it_is(mock_build, mock_preflight, capsys):
    """A disk or permission fault is reported verbatim, not blamed on PATH.

    Writing a build profile and reading the VS toolset marker both raise
    OSError from inside the same try, so a single "could not run conan -- is
    it on PATH?" arm sent the reader to check an installation that was fine.
    """
    assert run_main(["build", "--root", "."]) == 2

    err = capsys.readouterr().err
    assert "Permission denied" in err
    assert "Is it on PATH?" not in err


@mock.patch(f"{MODULE}.preview", return_value=0)
def test_main_build_preview(mock_preview):
    """--preview skips preflight and builds nothing."""
    assert run_main(["build", "--preview", "--filter", '{"build_type": "Debug"}']) == 0
    mock_preview.assert_called_once()
    assert mock_preview.call_args.kwargs["config_filter"] == {"build_type": "Debug"}
    # the argparse default is the module tuple itself, handed through as-is
    assert mock_preview.call_args.kwargs["python_versions"] == ("3.10", "3.13")


def test_main_build_unknown_library(capsys):
    """An unknown --only name exits 2 without touching the machine."""
    assert run_main(["build", "--only", "nope"]) == 2
    assert "unknown library 'nope'" in capsys.readouterr().err


def test_main_build_no_libraries_selected(capsys):
    """A selection that matches nothing exits 2 instead of pretending to succeed.

    ``--only xmscore --from xmsgrid`` is always a mistake -- the two flags cut
    the stack in opposite directions -- and so is a ``--from`` past the last
    enabled library. Reporting success for a build that could not run means a
    release script goes on to tag and upload nothing.
    """
    assert run_main(["build", "--only", "xmscore", "--from", "xmsgrid"]) == 2
    assert "no libraries selected" in capsys.readouterr().err


def test_main_build_missing_root(capsys, tmp_path):
    """A --root that does not exist is an error, not eight silent skips.

    The "partially migrated stack" rationale covers a missing *library*; a
    missing root means the path itself is a typo and nothing can be found.
    """
    assert run_main(["build", "--root", str(tmp_path / "typo")]) == 2
    assert "--root directory not found" in capsys.readouterr().err


@mock.patch(f"{MODULE}.run_preflight",
            return_value=[vs.CheckResult("vs", False, "missing")])
def test_main_build_preflight_failure(mock_preflight, capsys):
    """A failed preflight stops the build with exit 2."""
    assert run_main(["build"]) == 2
    assert "preflight failed" in capsys.readouterr().err


@mock.patch(f"{MODULE}.build",
            return_value=[vs.LibraryResult("xmscore", "ok", attempted=14, succeeded=14)])
@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
def test_main_build_passes_every_flag(mock_preflight, mock_build, tmp_path):
    """Each build flag reaches build() and the summary sets the exit code."""
    exit_code = run_main([
        "build", "--root", str(tmp_path), "--only", "xmscore",
        "--continue-on-error", "--no-generate", "--log-dir", "logs",
        "--python-versions", "3.13", "--version", "7.0.0",
    ])

    assert exit_code == 0
    args, kwargs = mock_build.call_args
    assert [library.name for library in args[0]] == ["xmscore"]
    assert args[1] == str(tmp_path)
    assert kwargs == {
        "version": "7.0.0", "generate": False, "python_versions": ["3.13"],
        "config_filter": None, "log_dir": "logs", "continue_on_error": True,
    }
    mock_preflight.assert_called_once_with(remote=vs.VS2019_REMOTE_NAME)


@mock.patch(f"{MODULE}.build",
            return_value=[vs.LibraryResult("xmscore", "ok", attempted=14, succeeded=14)])
@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
def test_main_build_remote_name_reaches_preflight(mock_preflight, mock_build, tmp_path):
    """--remote-name redirects the preflight check, matching setup's flag.

    ``setup --remote-name scratch`` is a supported way to point a machine at
    a different Artifactory repo, but preflight required the hard-coded
    aquaveo-vs2019 remote, so every such machine failed at exit 2 with no way
    to say what it had actually been set up with.
    """
    assert run_main([
        "build", "--root", str(tmp_path), "--remote-name", "scratch",
    ]) == 0
    mock_preflight.assert_called_once_with(remote="scratch")


@mock.patch(f"{MODULE}.build",
            return_value=[vs.LibraryResult("xmscore", "failed", attempted=14, failed=1)])
@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
def test_main_build_failure_exit_code(mock_preflight, mock_build):
    """A failed configuration makes the whole run exit nonzero."""
    assert run_main(["build"]) == 1


@mock.patch(f"{MODULE}.upload", return_value=0)
def test_main_upload(mock_upload):
    """Upload requires --library and --version explicitly."""
    assert run_main(UPLOAD_ARGV) == 0
    mock_upload.assert_called_once_with(
        "xmscore", "7.0.0", remote="aquaveo-vs2019", allow_other_remote=False,
    )


@pytest.mark.parametrize("argv", [
    ["upload", "--library", "xmscore"],   # no version -- never defaulted to '*'
    ["upload", "--version", "7.0.0"],     # no library
    [],                                   # no subcommand
])
def test_main_rejects_incomplete_invocations(argv):
    """Argparse refuses an upload without both --library and --version."""
    assert run_main(argv) == 2
