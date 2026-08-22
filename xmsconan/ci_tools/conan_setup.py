"""Set up Conan profile and remotes for CI builds.

Usage::

    xmsconan_conan_setup [--remote-url URL] [--login] [--remove-conancenter]
    xmsconan_conan_setup --login --username USER --password-file PATH
"""
import argparse
import os
import subprocess
import sys

from xmsconan.ci_tools.credentials import (
    CredentialsError,
    load_conan_credentials,
    read_password_file,
)
from xmsconan.constants import DEFAULT_REMOTE_NAME, DEFAULT_REMOTE_URL


def _login_environment(remote_name, username, password):
    """Return a child environment carrying the remote credentials out of band.

    The password must never be a command-line argument: on a managed Windows
    workstation, process-creation auditing (Event 4688 with command-line
    capture, or Sysmon Event ID 1) writes the full argv of every process to
    the event log, which is then shipped to a SIEM and retained for months.
    A password file protected by NTFS ACLs is worth nothing once its contents
    have been copied there in cleartext.  Conan reads the credentials from
    ``CONAN_LOGIN_USERNAME_<REMOTE>`` / ``CONAN_PASSWORD_<REMOTE>`` instead,
    where ``<REMOTE>`` is the remote name uppercased with hyphens replaced by
    underscores (verified against the pinned conan 2.31 client, which builds
    the same names in ``conan/internal/rest/remote_credentials.py``) -- so
    ``aquaveo-vs2019`` becomes ``CONAN_PASSWORD_AQUAVEO_VS2019``.

    Args:
        remote_name: Conan remote name the credentials belong to.
        username: Conan remote username.
        password: Conan remote password.

    Returns:
        A copy of ``os.environ`` with the two variables added.
    """
    suffix = remote_name.replace("-", "_").upper()
    env = dict(os.environ)
    env[f"CONAN_LOGIN_USERNAME_{suffix}"] = username
    env[f"CONAN_PASSWORD_{suffix}"] = password
    return env


def conan_setup(
    remote_url=None, login=False, remove_conancenter=False,
    username=None, password=None, remote_name=DEFAULT_REMOTE_NAME,
    index=0, use_config_file=True,
):
    """Detect a Conan profile and configure the Aquaveo remote.

    Args:
        remote_url: Conan remote URL. Defaults to the Aquaveo stable remote.
        login: If ``True``, log in to the remote after adding it.
        remove_conancenter: If ``True``, remove the conancenter remote.
        username: Conan remote username. Falls back to ``~/.xmsconan.toml``.
        password: Conan remote password. Falls back to ``~/.xmsconan.toml``.
            Passed to Conan through the environment, never through argv.
        remote_name: Conan remote name to add and log in to. Defaults to
            ``"aquaveo"`` (the CI-published stable remote). The manually
            built VS2019 / msvc 192 matrix passes ``"aquaveo-vs2019"`` so
            those binaries stay out of the CI remote (see
            :mod:`xmsconan.build_tools.vs2019_build`).
        index: Position to insert the remote at in the remote list, or None
            to append it after the remotes already configured. Defaults to
            ``0`` -- the CI remote is meant to be consulted before
            conancenter. Adding a *second* Aquaveo remote at index 0 would
            make it the first stop for every ``conan install`` on the
            machine, including unrelated ones, so callers adding a
            special-purpose remote pass ``index=None``.
        use_config_file: Consult ``~/.xmsconan.toml`` for whichever of
            ``username`` / ``password`` was not supplied. Pass ``False`` when
            the caller already resolved credentials through that same file
            (:func:`xmsconan.build_tools.vs2019_build.resolve_credentials`
            does, so that both halves come from one read and one account): a
            password that is still None after such a caller resolved it is a
            deliberate "let Conan prompt", and re-reading the file here would
            both read it a second time and overturn that decision.
    """
    if remote_url is None:
        remote_url = DEFAULT_REMOTE_URL

    subprocess.run(["conan", "profile", "detect", "-e"], check=True)
    add_command = ["conan", "remote", "add"]
    if index is not None:
        add_command += ["--index", str(index)]
    add_command += [remote_name, remote_url, "--force"]
    subprocess.run(add_command, check=True)

    if remove_conancenter:
        subprocess.run(
            ["conan", "remote", "remove", "conancenter"],
            check=True,
        )

    if login:
        if use_config_file and (not username or not password):
            conan_creds = load_conan_credentials()
            username = username or conan_creds.get("username")
            password = password or conan_creds.get("password")

        # With no credentials to hand over, conan prompts interactively.
        env = None
        if username and password:
            env = _login_environment(remote_name, username, password)
        subprocess.run(
            ["conan", "remote", "login", remote_name],
            check=True,
            env=env,
        )


def _resolve_password(password_file):
    """Resolve the remote password from the sources that are not argv.

    ``--password-file`` first, then ``$CONAN_PASSWORD``.  A ``None`` result
    is not "no password": it means the two sources this function knows about
    are empty, and :func:`conan_setup` still has ``~/.xmsconan.toml`` to
    consult (and Conan itself still has its own reading of ``$CONAN_PASSWORD``
    if nothing at all turns up).

    There is no ``--password`` flag to resolve here, and that is the point --
    see :func:`_login_environment` for why a password on a command line is
    worth nothing on a SIEM-audited workstation.

    Args:
        password_file: Value of ``--password-file``, or None.

    Returns:
        The password, or None when neither source supplies one.

    Raises:
        ValueError: ``password_file`` was given but is unusable.
    """
    if password_file:
        return read_password_file(password_file)
    return os.environ.get("CONAN_PASSWORD")


def main():
    """CLI entry point for ``xmsconan_conan_setup``."""
    parser = argparse.ArgumentParser(
        description="Set up Conan profile and remotes for CI builds.",
    )
    parser.add_argument(
        "--remote-url",
        default=None,
        help="Conan remote URL (default: Aquaveo stable).",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run 'conan remote login aquaveo' after adding the remote.",
    )
    parser.add_argument(
        "--remove-conancenter",
        action="store_true",
        help="Remove the conancenter remote.",
    )
    parser.add_argument(
        "--username", default=None,
        help="Conan remote username. Falls back to ~/.xmsconan.toml.",
    )
    parser.add_argument(
        "--password-file", default=None,
        help="Path to a file holding the remote password on its own line. "
             "Falls back to $CONAN_PASSWORD, then ~/.xmsconan.toml. There is "
             "deliberately no --password: see the note in _login_environment.",
    )
    # Kept only to be refused. Dropping it outright would not have removed it:
    # argparse expands unambiguous prefixes, so `--password secret` would have
    # bound to --password-file and reported "password file not found: secret",
    # with the secret in the argv either way and nothing said about the flag
    # that replaced it.
    parser.add_argument("--password", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.password is not None:
        parser.error(
            "--password was removed: its value lands in this process's argv, "
            "which process-creation auditing copies into the event log in "
            "cleartext. Use --password-file PATH, $CONAN_PASSWORD, or the "
            "[conan] section of ~/.xmsconan.toml. Rotate the password you "
            "just typed."
        )
    try:
        password = _resolve_password(args.password_file)
    except CredentialsError as exc:  # unusable --password-file
        # A usage error, and exit 2 like every other one -- the same code
        # `xmsconan vs2019 setup` returns for the same unusable file.
        parser.error(str(exc))
    try:
        conan_setup(
            remote_url=args.remote_url,
            login=args.login,
            remove_conancenter=args.remove_conancenter,
            username=args.username,
            password=password,
        )
    except CredentialsError as exc:
        # load_conan_credentials() raises this for a ~/.xmsconan.toml that does
        # not parse or whose [conan] is not a table. Same class of usage error
        # as an unusable --password-file above, so it gets the same exit 2 and
        # the same one-line message rather than a traceback. Narrower than
        # ValueError deliberately: conan_setup() runs three subprocesses, and a
        # ValueError out of one of those is not a usage error.
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
