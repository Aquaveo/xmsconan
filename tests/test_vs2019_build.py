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


def test_only_xmscore_is_enabled():
    """Xmscore is the only library on today; the rest await Conan 2."""
    enabled = [library.name for library in vs.LIBRARIES if library.enabled]
    assert enabled == ["xmscore"]
    assert [library.name for library in vs.LIBRARIES] == [
        "xmscore", "xmsgrid", "xmsinterp", "xmsmesher",
        "xmsextractor", "xmsstamper", "xmsconstraint", "xmsgridtrace",
    ]
    assert all(library.note for library in vs.LIBRARIES)


def test_select_libraries_defaults_to_enabled():
    """No flags selects only the enabled libraries."""
    assert [library.name for library in vs.select_libraries()] == ["xmscore"]


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


def test_resolve_credentials_missing_file(tmp_path):
    """A missing password file errors instead of falling through."""
    with pytest.raises(ValueError, match="password file not found"):
        vs.resolve_credentials(str(tmp_path / "absent.txt"))


def test_resolve_credentials_from_env():
    """The environment supplies both halves without touching the config file."""
    env = {"CONAN_PASSWORD": "from-env", "CONAN_LOGIN_USERNAME": "env-user"}
    with patch_env(env, clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials") as mock_creds:
        assert vs.resolve_credentials() == ("env-user", "from-env")
    mock_creds.assert_not_called()


@mock.patch(f"{MODULE}.load_conan_credentials",
            return_value={"username": "toml-user", "password": "from-toml"})
def test_resolve_credentials_from_config(mock_creds):
    """~/.xmsconan.toml is the last resort, and it is read exactly once.

    The username used to be resolved separately in ci_tools.conan_setup, which
    both re-opened the file and -- because the caller always passed a truthy
    default -- meant a developer with a personal Artifactory account logged in
    as ``aquaveo`` with their own password.
    """
    with patch_env(clear=True):
        assert vs.resolve_credentials() == ("toml-user", "from-toml")
    mock_creds.assert_called_once()


@mock.patch(f"{MODULE}.load_conan_credentials", return_value={})
def test_resolve_credentials_none_available(mock_creds):
    """No source supplies a password -> None (conan prompts) and the shared user."""
    with patch_env(clear=True):
        assert vs.resolve_credentials() == ("aquaveo", None)


@mock.patch(f"{MODULE}.load_conan_credentials", return_value={"password": "from-toml"})
def test_resolve_credentials_explicit_username_wins(mock_creds):
    """An explicit --username beats every fallback."""
    with patch_env({"CONAN_LOGIN_USERNAME": "env-user"}, clear=True):
        assert vs.resolve_credentials(username="me") == ("me", "from-toml")


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


# --- preflight: conan version / remote ----------------------------------


@mock.patch(f"{MODULE}.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "conan"))
def test_check_conan_version_not_runnable(mock_run):
    """A conan client that will not run is reported with the pip fix."""
    check = vs.check_conan_version()
    assert not check.ok
    assert 'pip install "conan~=2.31.0"' in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_conan_version_unparseable(mock_run):
    """Output without a version number is reported."""
    mock_run.return_value = mock.Mock(stdout="Conan version unknown")
    check = vs.check_conan_version()
    assert not check.ok
    assert "could not parse a version" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_conan_version_outside_pinned_series(mock_run):
    """A minor bump fails: it can change package_id computation."""
    mock_run.return_value = mock.Mock(stdout="Conan version 2.32.0")
    check = vs.check_conan_version()
    assert not check.ok
    assert "outside the pinned ~=2.31.0 series" in check.detail
    assert "package_id" in check.detail


@mock.patch(f"{MODULE}.subprocess.run")
def test_check_conan_version_patch_within_series_passes(mock_run):
    """Any 2.31.x patch level is accepted."""
    mock_run.return_value = mock.Mock(stdout="Conan version 2.31.2\n")
    check = vs.check_conan_version()
    assert check.ok
    assert "2.31.2" in check.detail


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
    )
    mock_preflight.assert_called_once_with(remote="aquaveo-vs2019")
    assert "s3cret" not in capsys.readouterr().out


@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("x", False, "")])
@mock.patch(f"{MODULE}.conan_setup")
def test_setup_returns_one_on_preflight_failure(mock_conan_setup, mock_preflight):
    """Setup exits nonzero when the machine fails preflight."""
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.load_conan_credentials", return_value={}):
        assert vs.setup() == 1


# --- preview -------------------------------------------------------------


@patch_env(clear=True)
def test_preview_generates_the_verified_matrix(capsys):
    """The VS2019 matrix is 14 configs: 4 base + 4 wchar_t + 4 testing + 2 pybind."""
    exit_code = vs.preview(
        vs.select_libraries(), python_versions=vs.DEFAULT_PYTHON_VERSIONS,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "==> xmscore: 14 configuration(s)" in out
    # every row is msvc 192, and the table is really printed
    assert "192" in out


@patch_env(clear=True)
def test_preview_applies_filter(capsys):
    """--filter narrows the previewed matrix."""
    vs.preview(vs.select_libraries(), config_filter={"build_type": "Debug"})
    assert "==> xmscore: 6 configuration(s)" in capsys.readouterr().out


# --- build_library -------------------------------------------------------


def test_build_library_missing_checkout(tmp_path):
    """A missing library directory is skipped, not fatal."""
    result = vs.build_library("xmsgrid", str(tmp_path))
    assert result.status == "skipped"
    assert "no checkout at" in result.message


def test_build_library_missing_build_toml(tmp_path):
    """A checkout without build.toml is skipped, not fatal."""
    (tmp_path / "xmsgrid").mkdir()
    result = vs.build_library("xmsgrid", str(tmp_path))
    assert result.status == "skipped"
    assert "no build.toml" in result.message


@mock.patch(f"{MODULE}.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "xmsconan_gen"))
def test_build_library_generator_failure(mock_run, library_root):
    """A failing xmsconan_gen fails the library without building."""
    result = vs.build_library("xmscore", str(library_root))
    assert result.status == "failed"
    assert "xmsconan_gen failed" in result.message


@mock.patch(f"{MODULE}.XmsConanPackager")
@mock.patch(f"{MODULE}.subprocess.run")
def test_build_library_success(mock_run, mock_packager_cls, library_root):
    """The happy path generates, builds, and records per-config counts."""
    packager = fake_packager(configurations=[{}] * 14, run_result=0)
    mock_packager_cls.return_value = packager

    result = vs.build_library(
        "xmscore", str(library_root), version="7.0.0",
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
    assert result.elapsed >= 0


@mock.patch(f"{MODULE}.XmsConanPackager")
@mock.patch(f"{MODULE}.subprocess.run")
def test_build_library_no_generate_and_filter(mock_run, mock_packager_cls, library_root):
    """--no-generate skips xmsconan_gen; --filter reaches the packager."""
    packager = fake_packager(configurations=[{}, {}], run_result=1)
    mock_packager_cls.return_value = packager

    result = vs.build_library(
        "xmscore", str(library_root), generate=False,
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

    result = vs.build_library("xmscore", str(library_root))

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
        built = vs.build(vs.select_libraries(only=["xmscore", "xmsgrid"]), "root")

    assert build_library.call_count == 1
    assert [r.name for r in built] == ["xmscore"]
    assert "Stopping after xmscore failed" in capsys.readouterr().out


def test_build_continue_on_error_and_xms_version():
    """--continue-on-error moves to the next library; --version sets XMS_VERSION."""
    results = [
        vs.LibraryResult("xmscore", "failed"),
        vs.LibraryResult("xmsgrid", "ok"),
    ]
    with patch_env(clear=True), \
            mock.patch(f"{MODULE}.build_library", side_effect=results):
        built = vs.build(
            vs.select_libraries(only=["xmscore", "xmsgrid"]), "root",
            version="7.0.0", continue_on_error=True,
        )
        assert os.environ["XMS_VERSION"] == "7.0.0"

    assert [r.name for r in built] == ["xmscore", "xmsgrid"]


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


def _all_help_strings():
    """Return the help text of every flag on the parser and its subparsers."""
    parser = vs._build_parser()
    actions = []
    for action in parser._actions:
        actions.append(action)
        for subparser in (getattr(action, "choices", None) or {}).values():
            actions.extend(subparser._actions)
    return [action.help for action in actions if action.help]


#: Absolute paths rooted in somebody's home directory: C:\Users\<name>\...,
#: /home/<name>/..., /Users/<name>/....  A help example must be a placeholder,
#: never a path that only exists on the machine the tool was written on.
_HOME_PATH_RE = re.compile(r"[A-Za-z]:[\\/]Users[\\/]|/home/|/Users/", re.IGNORECASE)


def test_help_carries_no_developer_specific_paths():
    r"""Every flag's help text uses placeholders, not one machine's real paths.

    The published ``--help`` is read by everyone who runs the tool, so an
    example naming a real credential location on one workstation is both
    noise and a bad hint. Asserted on the raw ``help`` strings rather than
    on rendered output, which argparse line-wraps to the terminal width.

    Asserting the general shape rather than one past offender's name: a
    literal ``"Claude" not in text`` check passes cleanly for a help string
    reading ``C:\\Users\\someone\\secrets\\conan.txt``.
    """
    help_strings = _all_help_strings()

    password_help = [text for text in help_strings if "password" in text]
    assert password_help, "the --password-file flag lost its help text"
    assert any(r"C:\path\to\conan-password.txt" in text for text in password_help)
    offenders = [text for text in help_strings if _HOME_PATH_RE.search(text)]
    assert not offenders, f"help text names a real home directory: {offenders}"


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


@mock.patch(f"{MODULE}.setup", side_effect=FileNotFoundError("conan"))
def test_main_setup_conan_not_on_path(mock_setup, capsys):
    """A missing conan executable is a message, not a traceback."""
    assert run_main(["setup"]) == 2
    assert "Is it on PATH?" in capsys.readouterr().err


@mock.patch(f"{MODULE}.setup", side_effect=ValueError("password file not found: p"))
def test_main_setup_bad_password_file(mock_setup, capsys):
    """A missing password file exits 2 with the reason on stderr."""
    assert run_main(["setup", "--password-file", "p"]) == 2
    assert "password file not found" in capsys.readouterr().err


@mock.patch(f"{MODULE}.setup",
            side_effect=subprocess.CalledProcessError(3, "conan"))
def test_main_setup_conan_failure(mock_setup, capsys):
    """A failing conan command propagates its exit code."""
    assert run_main(["setup"]) == 3
    assert "conan setup failed" in capsys.readouterr().err


@mock.patch(f"{MODULE}.preview", return_value=0)
def test_main_build_preview(mock_preview):
    """--preview skips preflight and builds nothing."""
    assert run_main(["build", "--preview", "--filter", '{"build_type": "Debug"}']) == 0
    mock_preview.assert_called_once()
    assert mock_preview.call_args.kwargs["config_filter"] == {"build_type": "Debug"}
    assert mock_preview.call_args.kwargs["python_versions"] == ["3.10", "3.13"]


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


@mock.patch(f"{MODULE}.build", side_effect=FileNotFoundError("conan"))
@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
def test_main_build_conan_not_on_path(mock_preflight, mock_build, capsys, tmp_path):
    """A missing conan executable is a message, not a traceback."""
    assert run_main(["build", "--root", str(tmp_path)]) == 2
    assert "Is it on PATH?" in capsys.readouterr().err


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


@mock.patch(f"{MODULE}.build",
            return_value=[vs.LibraryResult("xmscore", "failed", attempted=14, failed=1)])
@mock.patch(f"{MODULE}.run_preflight", return_value=[vs.CheckResult("vs", True, "ok")])
def test_main_build_failure_exit_code(mock_preflight, mock_build):
    """A failed configuration makes the whole run exit nonzero."""
    assert run_main(["build"]) == 1


@mock.patch(f"{MODULE}.upload", return_value=0)
def test_main_upload(mock_upload):
    """Upload requires --library and --version explicitly."""
    assert run_main(["upload", "--library", "xmscore", "--version", "7.0.0"]) == 0
    mock_upload.assert_called_once_with(
        "xmscore", "7.0.0", remote="aquaveo-vs2019", allow_other_remote=False,
    )


@mock.patch(f"{MODULE}.upload", side_effect=ValueError("refusing to upload"))
def test_main_upload_refused_remote(mock_upload, capsys):
    """A refused remote exits 2 with the reason, not a traceback."""
    argv = ["upload", "--library", "xmscore", "--version", "7.0.0",
            "--remote", "aquaveo"]
    assert run_main(argv) == 2
    assert "refusing to upload" in capsys.readouterr().err


@mock.patch(f"{MODULE}.upload", side_effect=FileNotFoundError("conan"))
def test_main_upload_conan_not_on_path(mock_upload, capsys):
    """Upload runs no preflight, so a missing conan surfaces here as exit 2."""
    argv = ["upload", "--library", "xmscore", "--version", "7.0.0"]
    assert run_main(argv) == 2
    assert "Is it on PATH?" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["upload", "--library", "xmscore"],   # no version -- never defaulted to '*'
    ["upload", "--version", "7.0.0"],     # no library
    [],                                   # no subcommand
])
def test_main_rejects_incomplete_invocations(argv):
    """Argparse refuses an upload without both --library and --version."""
    assert run_main(argv) == 2
