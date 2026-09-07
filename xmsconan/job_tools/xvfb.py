"""The one Xvfb implementation.

There were four: a ``xvfb-run`` prefix built in :mod:`xmsconan.ci_tools.publish`,
an ``os.execvpe`` re-exec in :mod:`xmsconan.coverage_tools.coverage_generator`,
a real Xvfb server per shard in :mod:`xmsconan.ci_tools.test_shards`, and an
``xvfb-run -a -s "-screen 0 1280x1024x24"`` string rendered into the GitLab
template. They disagreed about the geometry, about whether ``$DISPLAY`` counts,
and about what happens when ``xvfb-run`` is not installed -- and only one of
them had learned that ``xvfb-run`` hands a client a display before the server
can answer it.

Three mechanisms survive because three shapes of caller need them: a subprocess
gets a :func:`run_prefix`, a process that must be *inside* the display before it
imports anything gets :func:`reexec_under_xvfb`, and in-process work gets
:func:`display`, which owns a server and exports ``DISPLAY`` around a block.
The predicate that decides whether any of it happens is :func:`wants_xvfb`, and
it is the same one for all three.
"""
import contextlib
import errno
import logging
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

LOGGER = logging.getLogger(__name__)

#: What a display that is merely still starting refuses a probe with. Anything
#: else is a condition the deadline will not cure, so it is worth a line.
STARTUP_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ENOENT, errno.EAGAIN})

#: The screen ``xvfb-run`` is asked for, and the one a server started here
#: serves. 24-bit depth because the GL clients under test refuse to pick a
#: visual on 16.
SCREEN_GEOMETRY = "1280x1024x24"

#: ``xvfb-run -s`` takes the server arguments as one string.
RUN_SERVER_ARGUMENTS = f"-screen 0 {SCREEN_GEOMETRY}"

#: How a server started by this module is launched. ``-noreset`` is
#: load-bearing: an X server tears down and regenerates whenever its *last*
#: client disconnects, so both a readiness probe and a test that closes the
#: final render window would otherwise leave the next connection to land inside
#: a reset window and fail.
SERVER_ARGUMENTS = ("-screen", "0", SCREEN_GEOMETRY, "-noreset", "-nolisten", "tcp")

#: X display numbers start here and step by one per server. Each owner keeps
#: its number for the life of the run; a stale ``/tmp/.X99-lock`` in the
#: container makes that owner fail loudly with the Xvfb log naming the display,
#: which beats ``xvfb-run -a``'s silent renumbering (and its check-then-bind
#: race).
BASE_DISPLAY = 99

#: Seconds to wait for a server to answer the X11 handshake before its owner is
#: failed with the server's own log. Generous because several servers may
#: initialize at once against one container's CPU quota.
START_TIMEOUT = 60

#: Where X servers put their unix sockets.
SOCKET_DIR = "/tmp/.X11-unix"

#: Set in the child environment by :func:`reexec_under_xvfb` so the re-entered
#: process does not recurse.
REEXEC_VARIABLE = "XMSCONAN_XVFB_REEXEC"


def wants_xvfb(config, environ=None, platform=None):
    """Whether this machine should run the build behind a virtual display.

    Four conditions, and the order matters for the message: Linux only (macOS
    and Windows have a window server), no ``$DISPLAY`` already (a workstation
    with a real one needs nothing), ``[ci].xvfb`` on, and ``xvfb-run`` present.
    The last is the only one worth a warning: the first three are the machine
    or the repository saying no, while a missing ``xvfb-run`` is a repository
    that asked for a display on an image that cannot provide one, and the
    symptom otherwise is a VTK test segfaulting with no mention of X.

    ``xvfb-run`` rather than ``Xvfb`` is the probe even for :func:`display`,
    which launches ``Xvfb`` directly: ``xvfb-run`` is a shell wrapper around
    it, so the two are installed together, and probing the name the templates
    and the warning have always named keeps one answer for all three
    mechanisms.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return False
    if environ.get("DISPLAY"):
        return False
    if not config.ci.xvfb:
        return False
    if not shutil.which("xvfb-run"):
        LOGGER.warning("ci.xvfb=true but xvfb-run not found on PATH. VTK tests may segfault.")
        return False
    return True


def run_prefix():
    """The ``xvfb-run`` prefix for a command this process is about to spawn."""
    return ["xvfb-run", "-a", "-s", RUN_SERVER_ARGUMENTS]


def reexec_under_xvfb(module, environ=None, argv=None, exec_fn=None):
    """Replace this process with itself, running under ``xvfb-run``.

    For a command that has to be inside the display before it does anything --
    the coverage run imports and drives a test suite in-process, so wrapping a
    child would be too late.

    Re-execs through ``-m <module>`` rather than ``sys.argv[0]``: the
    ``xmsconan`` dispatcher rewrites ``argv[0]`` to the literal
    ``"xmsconan coverage"``, so handing it to the interpreter ran
    ``python "xmsconan coverage"`` and died with "can't open file".

    Returns without doing anything when the flag is already set (this *is* the
    re-entered process) or when ``xvfb-run`` is not installed -- the caller
    then continues without a display, which surfaces test failures with a clear
    error rather than silently masking them.

    Args:
        module: Dotted module path with a ``__main__`` guard to re-enter.
        environ: The environment to read and copy; ``os.environ`` when None.
        argv: The arguments to pass through; ``sys.argv[1:]`` when None.
        exec_fn: The exec to call; ``os.execvpe`` when None.
    """
    environ = os.environ if environ is None else environ
    argv = sys.argv[1:] if argv is None else list(argv)
    exec_fn = os.execvpe if exec_fn is None else exec_fn

    if environ.get(REEXEC_VARIABLE):
        return
    xvfb_run = shutil.which("xvfb-run")
    if not xvfb_run:
        LOGGER.warning("ci.xvfb is true but xvfb-run is not on PATH; running without a display.")
        return
    child_environment = dict(environ)
    child_environment[REEXEC_VARIABLE] = "1"
    command = [*run_prefix(), sys.executable, "-m", module, *argv]
    LOGGER.info("Re-execing under xvfb-run: %s", " ".join(command))
    exec_fn(xvfb_run, command, child_environment)


def ensure_socket_dir():
    """Create the X socket directory before a server storm needs it.

    Concurrent cold-starting Xvfbs race each other creating it in a fresh
    container: the loser's mkdir fails with EEXIST, its unix listener is never
    created, and its display never answers (seen on xmsvtk's runner -- one
    Xvfb per container never hit this because the first server created the
    directory alone). One mkdir up front removes the race. The chmod matches
    the sticky world-writable mode X expects but does not insist: on a host
    with a real X server the directory already exists, owned by root, already
    correct, and not ours to touch.
    """
    os.makedirs(SOCKET_DIR, exist_ok=True)
    try:
        os.chmod(SOCKET_DIR, 0o1777)
    except OSError as error:
        # Expected on a host whose X server already owns the directory, which
        # is why it is not fatal -- but a chmod that failed for another reason
        # is worth having in the log when a display later refuses.
        LOGGER.debug("Leaving %s as it is: %s", SOCKET_DIR, error)


def server_answers(display_number):
    """One X11 connection-setup handshake against *display_number*.

    Sends the 12-byte setup request (little-endian byte order, protocol 11.0,
    no authorization) and requires the server's success reply. Merely seeing
    the socket file is not enough -- Xvfb binds it early in startup, before it
    can answer a client, and a connection accepted that early still fails. The
    Linux abstract namespace is tried first because that is Xlib's own first
    choice, so a server that lost its filesystem listener but holds the
    abstract one still serves its client and counts as ready.
    """
    socket_path = f"{SOCKET_DIR}/X{display_number}"
    for address in (f"\0{socket_path}", socket_path):
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(2)
        try:
            probe.connect(address)
            probe.sendall(struct.pack("<BxHHHHxx", ord("l"), 11, 0, 0, 0))
            if probe.recv(1) == b"\x01":
                return True
        except OSError as error:
            # A server still starting refuses, or has not bound its socket
            # yet; that is what this loop polls through. EACCES is not that --
            # it will still be true at the deadline, and reporting it only as
            # "did not answer within 60s" hides the one actionable cause.
            if error.errno not in STARTUP_ERRNOS:
                LOGGER.debug("Probe of display :%s at %r failed: %s",
                             display_number, address, error)
        finally:
            probe.close()
    return False


def start_server(display_number, log_path):
    """Start one Xvfb and wait until it answers the handshake.

    This module owns the server rather than delegating to ``xvfb-run`` because
    ``xvfb-run`` launches Xvfb and the client back to back with no readiness
    handshake: under simultaneous cold starts squeezed into one job container's
    CPU quota, a runner can dial its display before the server answers, and a
    GL client that limps past that first failed ``XOpenDisplay`` dies later
    with a fatal ``BadAccess`` on ``GLXMakeCurrent``.

    Returns:
        ``(process, None)`` once the server answers, or ``(process, error)``
        with the server's captured output when it exited early or never became
        ready -- the caller fails loudly instead of running against a display
        that cannot serve it.
    """
    log_path = Path(log_path)
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            ["Xvfb", f":{display_number}", *SERVER_ARGUMENTS],
            stdout=log, stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process, (
                f"Xvfb :{display_number} exited with code {process.returncode} "
                f"before serving: {log_path.read_text(errors='replace').strip()}"
            )
        if server_answers(display_number):
            return process, None
        time.sleep(0.2)
    stop_server(process)
    return process, (
        f"Xvfb :{display_number} did not answer within {START_TIMEOUT}s: "
        f"{log_path.read_text(errors='replace').strip()}"
    )


def stop_server(process):
    """Terminate a server, escalating to kill if it lingers."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@contextlib.contextmanager
def display(config, display_number=BASE_DISPLAY, log_dir=None, environ=None, platform=None):
    """Run the block with ``$DISPLAY`` pointing at a server this owns.

    For work this process drives itself: ``job build`` constructs the packager
    in-process, so there is no command to prefix with ``xvfb-run`` and no point
    early enough to re-exec from. Children inherit ``DISPLAY`` from
    ``os.environ``, so a ``conan create`` and the ctest run inside it land on
    the same server.

    A no-op when :func:`wants_xvfb` says no, including on Windows and macOS,
    so the caller wraps unconditionally.

    Raises:
        RuntimeError: The server exited early or never answered. The build must
            not proceed: it would run the same tests against no display and
            fail somewhere less legible.
    """
    if not wants_xvfb(config, environ=environ, platform=platform):
        yield None
        return

    environ = os.environ if environ is None else environ
    log_dir = Path("." if log_dir is None else log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ensure_socket_dir()
    server, error = start_server(display_number, log_dir / f"xvfb-{display_number}.log")
    if error:
        stop_server(server)
        raise RuntimeError(error)

    previous = environ.get("DISPLAY")
    environ["DISPLAY"] = f":{display_number}"
    LOGGER.info("Started Xvfb on DISPLAY=%s", environ["DISPLAY"])
    try:
        yield server
    finally:
        if previous is None:
            environ.pop("DISPLAY", None)
        else:
            environ["DISPLAY"] = previous
        stop_server(server)
