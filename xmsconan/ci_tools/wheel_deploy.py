"""Upload repaired wheels to a devpi index with ``uv publish``.

Usage::

    xmsconan_wheel_deploy [--wheel-dir DIR] [--url URL] [--username USER]
                          [--password-file PATH] [--client {uv,devpi}]

Credentials are resolved in order:
  1. CLI arguments (``--url``, ``--username``, ``--password-file``)
  2. Environment variables (``AQUAPI_URL``, ``AQUAPI_USERNAME``, ``AQUAPI_PASSWORD``)
  3. ``~/.xmsconan.toml`` config file (see :mod:`xmsconan.ci_tools.credentials`)

For this entry point the password never goes on a command line, in either
direction -- except on the deprecated ``--client devpi`` path described
below, which is the one place it still does.  There is no ``--password``
flag, for the reason ``conan-setup`` has none: a process's argv is copied
into shell history, ``ps`` output, and the Windows Event 4688 / Sysmon
record that ships to the SIEM in cleartext.
And the upload itself is ``uv publish``, which reads ``UV_PUBLISH_USERNAME``
and ``UV_PUBLISH_PASSWORD`` from its environment, so the child gets the secret
the way ``conan remote login`` gets ``CONAN_PASSWORD_<REMOTE>`` from
``conan_setup``.

The qualifier is load-bearing: ``xmsconan publish`` still registers its own
``--password`` and forwards the value to :func:`wheel_deploy`, so that command
puts the same secret on xmsconan's argv.  Closing it is a change to that
command's interface and is made separately; until then the claim above is
about ``xmsconan_wheel_deploy``, not about every route into this function.

The ``uv`` that runs is the one the ``uv`` package -- a dependency of
xmsconan -- installed next to this code, found with ``uv.find_uv_bin()``.
Not whatever ``uv`` is first on ``PATH``: a ``uv tool install`` or pipx
layout exposes only xmsconan's own entry points, and the manual VS2019 track
runs ``xmsconan_wheel_deploy`` from exactly such an install.

``--client devpi`` keeps the previous ``devpi use`` / ``devpi login
--password`` / ``devpi upload`` sequence for one release.  It is the last
place xmsconan puts a password on a subprocess's argv, it says so on stderr
every time it runs, and it goes away in the release after this one.  The two
clients do not upload the same files: ``devpi upload --from-dir`` ships the
whole directory, sdists and anything stale in it included, where the uv path
ships exactly the ``*.whl`` :func:`_wheels_in` found.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from uv import find_uv_bin

from xmsconan.ci_tools.credentials import (
    add_refused_password_flag,
    CredentialsError,
    load_credentials,
    read_password_file,
)

UPLOAD_CLIENTS = ("uv", "devpi")


class WheelDeployError(ValueError):
    """A fault found before any upload tool runs: a missing credential or an empty wheelhouse.

    Its own class so ``main`` can report exactly these as usage errors, the
    way ``conan-setup`` reports a ``CredentialsError``, without also catching
    a ``ValueError`` raised from inside the upload itself.
    """


def _upload_environment(username, password):
    """Return the environment ``uv publish`` reads its credentials from.

    A copy of the current environment with the two variables set on top --
    the same shape as ``conan_setup._login_environment``.  A two-entry
    environment would keep the secret off argv just as well, but uv needs
    ``PATH``, ``HOME`` and the proxy variables to reach the index at all.

    Any ``UV_PUBLISH_*`` already exported is dropped first.  ``uv publish``
    reads ``--token`` from ``UV_PUBLISH_TOKEN`` and refuses a token next to a
    username, so a developer's exported PyPI token would otherwise turn a
    devpi upload into a usage error from the child.

    Args:
        username: Index username.
        password: Index password.

    Returns:
        The child environment.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_PUBLISH_")}
    env["UV_PUBLISH_USERNAME"] = username
    env["UV_PUBLISH_PASSWORD"] = password
    return env


def _wheels_in(wheel_dir):
    """Return every ``.whl`` under *wheel_dir*, sorted for a stable argv.

    ``uv publish`` takes files, and given none it falls back to ``dist/*`` --
    so an empty wheelhouse has to be refused here, or a deploy job with
    nothing to upload exits 0 having uploaded nothing.

    A directory that is not there is reported as itself rather than as an
    empty one.  They are different faults -- an artifact download that never
    ran, against a build that produced no wheel -- and answering the first
    with "no wheels to upload" sends the operator to the wrong one.

    The match is ``Path.glob`` rather than ``glob.glob`` because the latter
    reads ``[``, ``]``, ``*`` and ``?`` in *wheel_dir* itself as pattern
    syntax: a wheelhouse under such a path matched nothing and looked empty.

    Args:
        wheel_dir: Directory holding the repaired wheels.

    Returns:
        The wheel paths.

    Raises:
        WheelDeployError: *wheel_dir* is not a directory, or holds no wheel.
    """
    directory = Path(wheel_dir)
    if not directory.is_dir():
        raise WheelDeployError(f"Wheel directory does not exist: {wheel_dir}")
    wheels = sorted(str(wheel) for wheel in directory.glob("*.whl"))
    if not wheels:
        raise WheelDeployError(f"No wheels to upload in {wheel_dir}")
    return wheels


def _publish_with_uv(url, username, password, wheels):
    """Run ``uv publish`` over *wheels* with the credentials in its environment."""
    subprocess.run(
        [find_uv_bin(), "publish", "--publish-url", url, *wheels],
        check=True,
        env=_upload_environment(username, password),
    )


def _publish_with_devpi(url, username, password, wheel_dir):
    """Run the pre-uv devpi-client sequence.  Deprecated: the password is on argv."""
    print(
        "xmsconan wheel-deploy: --client devpi puts the password on `devpi login`'s "
        "command line and is removed in the next release; drop the flag to upload "
        "with `uv publish`.",
        file=sys.stderr,
    )
    subprocess.run(["devpi", "use", url], check=True)
    subprocess.run(
        ["devpi", "login", username, "--password", password],
        check=True,
    )
    subprocess.run(
        ["devpi", "upload", "--from-dir", wheel_dir],
        check=True,
    )


def wheel_deploy(wheel_dir="wheelhouse", *, url=None, username=None, password=None, client="uv"):
    """Upload the wheels in *wheel_dir* to a devpi index.

    Everything after *wheel_dir* is keyword-only.  Four bare strings in a row,
    three of them a URL, a username and a password, are interchangeable at a
    call site: swapping the last two puts the password in
    ``UV_PUBLISH_USERNAME``, where the index's own authentication failure is
    free to echo it back into the job log.  Both callers already pass
    keywords, so the restriction costs nothing and removes the shape.

    Args:
        wheel_dir: Directory containing repaired ``.whl`` files.
        url: devpi index URL -- the index itself, which is also its upload
            endpoint, not the ``+simple/`` page pip reads.  Falls back to
            ``$AQUAPI_URL``, then ``~/.xmsconan.toml``.
        username: devpi username.  Falls back to ``$AQUAPI_USERNAME``,
            then ``~/.xmsconan.toml``.
        password: devpi password.  Falls back to ``$AQUAPI_PASSWORD``,
            then ``~/.xmsconan.toml``.
        client: ``"uv"`` runs ``uv publish`` with the credentials in its
            environment.  ``"devpi"`` runs the previous devpi-client
            sequence, which passes the password on ``devpi login``'s
            command line; kept for one release.

    Raises:
        WheelDeployError: A credential is missing or is not a string, or
            *wheel_dir* holds no wheel.
        CredentialsError: ``~/.xmsconan.toml`` exists but cannot be used.
        ValueError: *client* is not one of ``UPLOAD_CLIENTS``.
        FileNotFoundError: The upload tool is not installed.
        subprocess.CalledProcessError: The upload tool failed.
    """
    if client not in UPLOAD_CLIENTS:
        raise ValueError(
            f"Unknown upload client {client!r}; expected one of {', '.join(UPLOAD_CLIENTS)}"
        )

    creds = load_credentials()
    url = url or os.environ.get("AQUAPI_URL") or creds.get("url")
    username = username or os.environ.get("AQUAPI_USERNAME") or creds.get("username")
    password = password or os.environ.get("AQUAPI_PASSWORD") or creds.get("password")

    if not url:
        raise WheelDeployError(
            "No devpi URL provided (--url, $AQUAPI_URL, or ~/.xmsconan.toml)"
        )
    if not username:
        raise WheelDeployError(
            "No devpi username provided (--username, $AQUAPI_USERNAME, or ~/.xmsconan.toml)"
        )
    if not password:
        raise WheelDeployError(
            "No devpi password provided (--password-file, $AQUAPI_PASSWORD, or ~/.xmsconan.toml)"
        )

    # A value out of ~/.xmsconan.toml is whatever TOML decoded, and only the
    # [aquapi] table itself is type-checked on the way out (credentials.py).
    # `password = 12345678` without quotes -- a realistic all-digit password --
    # decodes as an int, which reaches subprocess.run's env as a non-str and
    # raises TypeError from inside the upload, past every handler in main().
    # Every other unusable-config shape here answers with one line and exit 2,
    # so this one does too. The type is named and the value is not: whichever
    # of the three is wrong, the message is going to a terminal or a job log.
    for name, value in (("url", url), ("username", username), ("password", password)):
        if not isinstance(value, str):
            raise WheelDeployError(
                f"The {name} is {type(value).__name__}, not a string. In "
                f'~/.xmsconan.toml write it quoted: {name} = "..."'
            )

    # The emptiness check applies to both clients: a deploy with nothing to
    # upload is an error whichever tool would have run.
    wheels = _wheels_in(wheel_dir)

    if client == "devpi":
        _publish_with_devpi(url, username, password, wheel_dir)
    else:
        _publish_with_uv(url, username, password, wheels)


def _build_parser():
    """Return the ``xmsconan_wheel_deploy`` parser.

    Split out so ``main`` reads as what it does -- resolve the password file,
    deploy, map each fault to an exit status -- instead of that logic sitting
    below thirty lines of declarative ``add_argument``.
    """
    parser = argparse.ArgumentParser(
        description="Upload repaired wheels to a devpi index with uv publish.",
    )
    parser.add_argument(
        "--wheel-dir",
        default="wheelhouse",
        help="Directory containing .whl files (default: wheelhouse).",
    )
    parser.add_argument(
        "--url", default=None,
        help="devpi index URL, e.g. https://public.aquapi.aquaveo.com/aquaveo/dev/. "
             "Falls back to $AQUAPI_URL, then ~/.xmsconan.toml.",
    )
    parser.add_argument(
        "--username", default=None,
        help="devpi username. Falls back to $AQUAPI_USERNAME, then ~/.xmsconan.toml.",
    )
    parser.add_argument(
        "--password-file", default=None,
        help="Path to a file holding the devpi password on its own line. "
             "Falls back to $AQUAPI_PASSWORD, then ~/.xmsconan.toml. There is "
             "deliberately no --password: see the module docstring.",
    )
    parser.add_argument(
        "--client", choices=UPLOAD_CLIENTS, default="uv",
        help="Upload tool (default: uv). 'devpi' keeps the previous devpi-client "
             "sequence for one release; it passes the password on devpi login's "
             "command line.",
    )
    add_refused_password_flag(parser, env_var="AQUAPI_PASSWORD", section="aquapi")
    return parser


def main():
    """CLI entry point for ``xmsconan_wheel_deploy``."""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        password = read_password_file(args.password_file) if args.password_file else None
        wheel_deploy(
            wheel_dir=args.wheel_dir,
            url=args.url,
            username=args.username,
            password=password,
            client=args.client,
        )
    except (CredentialsError, WheelDeployError) as exc:
        # Usage errors -- an unusable --password-file or ~/.xmsconan.toml, a
        # missing credential, an empty wheel directory -- get the one line and
        # exit 2 the sibling entry points give theirs, not a traceback.
        # Narrower than ValueError on purpose, for conan-setup's reason: the
        # upload tool runs inside this try, and a ValueError out of it is not
        # a usage error.
        parser.error(str(exc))
    except FileNotFoundError as exc:
        # The uv package without its binary, or no `devpi` on PATH.
        parser.error(f"could not start the upload tool: {exc}")
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
