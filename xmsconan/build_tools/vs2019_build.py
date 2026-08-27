r"""Drive the manual Visual Studio 2019 (msvc 192) package build.

GitHub retired the ``windows-2019`` runner image, so the msvc 192 binaries
can no longer be produced in CI.  They are built by hand, on a developer
workstation that has Visual Studio 2019 installed, and published to a
separate Conan remote (``aquaveo-vs2019``) so they never mix with the
CI-published ``aquaveo`` remote.  Nothing in this module runs in CI.

Three subcommands::

    xmsconan_vs2019 setup  --password-file C:\path\to\p.txt
    xmsconan_vs2019 build  --root E:\code\xms\migration --log-dir logs
    xmsconan_vs2019 upload --library xmscore --version 7.0.0

``build`` and ``upload`` are deliberately separate verbs: a multi-hour local
build must never push binaries to a shared remote as a side effect, so a
human looks at the build result before running ``upload``.

Python wheels follow the same split.  ``build --wheel-dir DIR`` copies each
pybind package's ``.whl`` out of the Conan cache and stages the shared
libraries the repair step needs; *repairing* and *publishing* stay separate
commands, exactly as ``upload`` is separate from ``build`` and for the same
reason::

    # from a Python 3.10 virtual environment
    xmsconan_vs2019 build --root E:\code\xms\migration --version 7.0.0 \
        --python-versions 3.10 --filter '{"options": {"pybind": true}}' \
        --wheel-dir wheelhouse
    xmsconan_wheel_repair --wheel-dir wheelhouse --platform windows
    xmsconan_wheel_deploy --wheel-dir wheelhouse

**One run per Python version, from an interpreter of that version.**
:class:`~xmsconan.xms_conan2_file.XmsConan2File` points CMake at
``sys.executable`` -- the interpreter running conan -- while the generated
``CMakeLists.txt`` requires ``find_package(Python3 ${PYTHON_TARGET_VERSION}
EXACT REQUIRED)``.  CI satisfies that implicitly, because
``actions/setup-python`` installs the matrix version and conan runs under it;
a workstation does not, so a 3.12 virtualenv building
``--python-versions 3.10`` fails every pybind configuration at configure
time.  :func:`check_python_versions` catches that before anything is
compiled, and only when the *filtered* matrix actually contains a pybind
configuration -- the non-pybind configurations do not care which
interpreter is running.

Process exit codes are the contract with whatever wrapper script drives
this, so they are distinct:

* ``0`` -- everything asked for happened.
* ``1`` -- a library failed to build, ``conan upload`` failed, or a
  ``--wheel-dir`` run produced no wheel.  A missing wheel is a build failure
  rather than a code of its own: the run was asked for an artifact, it ran,
  and the artifact is not there.
* ``2`` -- the request or the machine was wrong: a bad flag, a ``--root``
  that does not exist, a selection that matches no library, a failed
  preflight (from either ``setup`` or ``build`` -- the same condition gets
  the same code), an unusable password file, a file the run needed that
  could not be read or written, or ``conan`` / ``xmsconan_gen`` not on
  ``PATH``.
* ``3`` -- nothing was built.  Every selected library was skipped (no
  checkout, no ``build.toml``, or ``--filter`` matched no configuration).
  This is *not* success: a typo in ``--root`` used to exit 0.

Output goes to ``print`` rather than the module ``LOGGER`` its sibling
:mod:`xmsconan.build_tools.build_library` uses.  This driver is only ever
run interactively by a developer watching a multi-hour build, so its
progress lines and summary table are the product, not diagnostics to be
filtered by level.

The VS2019 dependency fork itself lives in
:mod:`xmsconan.xms_conan2_file` (boost 1.74.0.3 + zlib 1.2.11 instead of
boost 1.86.0 + zlib 1.3.1); this module only drives it.  Two settings are
*not* implied by the
``windows_vs2019`` platform key and must be passed explicitly, which is why
they are hard-coded here:

* ``apply_boost_defaults=False`` -- the boost ``without_stacktrace`` /
  ``without_locale`` defaults name conan-center boost 1.86 options that the
  legacy ``boost/1.74.0.3`` recipe may not declare, and Conan fails a build
  when a profile sets an option no involved recipe defines.
* ``remote='aquaveo-vs2019'`` plus a ``compiler.version=192`` package query
  on upload -- the packager still defaults to the CI remote, and without the
  query ``conan upload`` matches by reference only and would publish every
  binary of that version sitting in the local cache.
"""
import argparse
from dataclasses import dataclass
import json
import os
import re
import subprocess
import sys
import time
from typing import NamedTuple, Optional

from tabulate import tabulate

from xmsconan.ci_options import repairs_windows_wheel
from xmsconan.ci_tools.conan_setup import conan_setup
from xmsconan.ci_tools.credentials import load_conan_credentials, read_password_file
from xmsconan.constants import (
    MSVC_VS2019_VERSION, version_sort_key, VS2019_REMOTE_NAME, VS2019_REMOTE_URL,
)
from xmsconan.package_tools.packager import XmsConanPackager
from xmsconan.toml_utils import load_toml, validate_top_level_keys

#: Username used to log in to :data:`VS2019_REMOTE_NAME` when neither the
#: CLI, the environment, nor ``~/.xmsconan.toml`` names one.
DEFAULT_REMOTE_USERNAME = "aquaveo"

#: Key into :data:`xmsconan.package_tools.packager.configurations`.
PLATFORM_KEY = "windows_vs2019"

#: Conan client requirement, matching the pin used by the CI workflows.
CONAN_PIN = "~=2.31.0"
#: ``(major, minor)`` of :data:`CONAN_PIN`; only the patch level may vary.
CONAN_PINNED_SERIES = (2, 31)

#: vswhere component id for the C++ toolset.  Without it a Visual Studio
#: 2019 carrying only the .NET workload satisfies the preflight check and
#: then dies on the first ``conan create``.
VC_TOOLS_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"

#: Python versions the pybind variants fan out across.  Two versions are
#: what makes the verified 14-configuration VS2019 matrix (4 base + 4
#: ``wchar_t=typedef`` + 4 testing + 2 pybind).
DEFAULT_PYTHON_VERSIONS = ("3.10", "3.13")

#: Process exit code for "the run completed but produced nothing".
EXIT_NOTHING_BUILT = 3
#: Process exit code for a bad request or an unusable machine.
EXIT_USAGE = 2


class ToolNotFoundError(OSError):
    """A required executable (``conan``, ``xmsconan_gen``) would not start.

    An :class:`OSError` subclass so the CLI keeps a single ``except OSError``
    arm per subcommand, but raised only at the *launch* sites.  Everything
    else that reaches that arm -- a password file that cannot be read, a
    build profile that cannot be written -- is a disk or permission fault and
    is reported verbatim, instead of being blamed on ``PATH``.
    """


def _tool_not_found(tool, exc):
    """Return the :class:`ToolNotFoundError` for an executable that would not start.

    Args:
        tool: Name of the executable that could not be launched.
        exc: The :class:`OSError` :mod:`subprocess` raised.

    Returns:
        A :class:`ToolNotFoundError` carrying the actionable message.
    """
    return ToolNotFoundError(f"could not run {tool} ({exc}). Is it on PATH?")


@dataclass(frozen=True)
class LibrarySpec:
    """One XMS library in the build stack.

    Attributes:
        name: Directory name of the checkout under ``--root``, which is also
            the Conan package name.
        enabled: Whether a plain ``build`` run includes this library.  ``--only``
            ignores this flag, so a library can still be built while off.
        note: What makes the library special -- its packaging status, or why
            it is held out of a plain ``build`` run when it is.
    """

    name: str
    enabled: bool
    note: str


PUBLISHED = "Conan 2 recipe; msvc 192 packages on aquaveo-vs2019."

#: The XMS stack in dependency order -- each library is built against the
#: packages produced by the ones above it, so the order matters.  Every entry
#: has a Conan 2 recipe, so all of them are enabled; the flag remains so a
#: library can be taken out of a plain ``build`` run without deleting it.
LIBRARIES = (
    LibrarySpec("xmscore", True, PUBLISHED),
    LibrarySpec("xmsgrid", True, PUBLISHED),
    LibrarySpec("xmsinterp", True, PUBLISHED),
    LibrarySpec("xmsmesher", True, PUBLISHED),
    LibrarySpec("xmsextractor", True, PUBLISHED),
    LibrarySpec("xmsstamper", True, PUBLISHED),
    LibrarySpec("xmsconstraint", True, PUBLISHED),
    LibrarySpec("xmsgridtrace", True, "Conan 2 recipe; no msvc 192 packages published yet."),
    LibrarySpec("xmssnap", True, "Python-only -- the pybind wheel is the only consumable artifact."),
)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single preflight check.

    Attributes:
        name: Short name of the thing being checked.
        ok: True when the machine satisfies the check.
        detail: What was found, or an actionable fix when ``ok`` is False.
    """

    name: str
    ok: bool
    detail: str


@dataclass
class LibraryResult:
    """Outcome of building one library.

    Attributes:
        name: Library name.
        status: ``'ok'``, ``'failed'``, or ``'skipped'``.
        attempted: Configurations handed to ``packager.run()``.
        succeeded: Configurations that built.
        failed: Configurations that failed.
        elapsed: Wall-clock seconds spent on this library.
        message: Why it was skipped, or what went wrong.
        wheels: True when ``--wheel-dir`` was given and a wheel was extracted
            for every requested Python version, False when the extraction
            produced nothing or only part of the fan-out, and None when no
            wheel was asked for -- or when the library never got that far
            (skipped, or a configuration failed).  None therefore never means
            "a wheel is missing"; those runs already exit nonzero on their own
            status.
    """

    name: str
    status: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    elapsed: float = 0.0
    message: str = ""
    wheels: Optional[bool] = None


# --- library selection ---------------------------------------------------


def select_libraries(only=None, start_from=None):
    """Pick the libraries to act on, in dependency order.

    Args:
        only: Library names to restrict the run to.  A name listed here is
            built even when its :attr:`LibrarySpec.enabled` flag is False, so
            a developer can exercise a library mid-migration.
        start_from: Resume the stack at this library, dropping everything
            before it.  Used to pick up after a mid-stack failure.

    Returns:
        List of :class:`LibrarySpec` in dependency order.  May be empty when
        ``only`` and ``start_from`` disagree, or when nothing past
        ``start_from`` is enabled; the caller turns that into an error rather
        than a silent success.

    Raises:
        ValueError: When ``only`` or ``start_from`` names a library that is
            not in :data:`LIBRARIES`.
    """
    names = [library.name for library in LIBRARIES]
    libraries = list(LIBRARIES)
    if start_from:
        if start_from not in names:
            raise ValueError(
                f"unknown library {start_from!r} for --from; known libraries: "
                f"{', '.join(names)}"
            )
        libraries = libraries[names.index(start_from):]
    if only:
        unknown = [name for name in only if name not in names]
        if unknown:
            raise ValueError(
                f"unknown library {', '.join(repr(u) for u in unknown)} for --only; "
                f"known libraries: {', '.join(names)}"
            )
        return [library for library in libraries if library.name in only]
    return [library for library in libraries if library.enabled]


# --- credentials ---------------------------------------------------------


class Credentials(NamedTuple):
    """Conan remote credentials resolved from every configured source.

    A named tuple rather than a bare pair: the two fields are both strings
    and the resolution order reads "password file, then username", so a
    transposed positional unpack at a call site would be type-silent and
    would send the password out as the login name.

    Attributes:
        username: Remote username; never None.
        password: Remote password, or None when no source supplied one, in
            which case Conan is left to prompt interactively.
    """

    username: str
    password: Optional[str]


def resolve_credentials(password_file=None, username=None):
    """Resolve the Conan remote credentials without ever echoing the password.

    Username order: the ``username`` argument -> ``$CONAN_LOGIN_USERNAME`` ->
    the ``[conan]`` section of ``~/.xmsconan.toml`` -> ``"aquaveo"``.
    Password order: ``password_file`` -> ``$CONAN_PASSWORD`` -> the same
    ``[conan]`` section.

    Both halves are resolved here, together, so ``~/.xmsconan.toml`` is read
    at most once and so a developer with a personal Artifactory account gets
    *their* username with *their* password.  Resolving only the password here
    and leaving the username to :func:`~xmsconan.ci_tools.conan_setup` meant
    the shared ``aquaveo`` username was always passed, which is truthy, which
    made the config-file username fallback unreachable -- the login went out
    as ``aquaveo`` with a personal password and failed with no hint why.
    (:func:`setup` passes ``use_config_file=False`` to keep the "at most
    once" half of that true: a password still None after this function ran is
    a resolved *absence*, not a source left unconsulted.)

    Args:
        password_file: Path to a file holding the password on its own.
            Trailing whitespace (the editor's newline) is stripped.
        username: Username from the command line, or None.

    Returns:
        A :class:`Credentials` named tuple.

    Raises:
        ValueError: When ``password_file`` is given but does not exist, holds
            no password, or cannot be read.  An explicitly supplied file must
            not silently fall through to another source -- that is the whole
            reason the flag exists -- and a file that is empty because the
            secret never landed in it looks exactly like a typo'd path from
            the other end of the login.
    """
    if password_file:
        password = read_password_file(password_file)
    else:
        password = os.environ.get("CONAN_PASSWORD")
    username = username or os.environ.get("CONAN_LOGIN_USERNAME")
    if not username or not password:
        credentials = load_conan_credentials()
        username = username or credentials.get("username")
        password = password or credentials.get("password")
    return Credentials(username or DEFAULT_REMOTE_USERNAME, password)


# --- preflight -----------------------------------------------------------


def _vswhere_path():
    """Return the canonical ``vswhere.exe`` path for this machine."""
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return os.path.join(
        program_files, "Microsoft Visual Studio", "Installer", "vswhere.exe"
    )


def _msvc_toolset_version(install_path):
    """Return the default MSVC toolset version of a VS install, or None.

    Args:
        install_path: ``installationPath`` reported by vswhere.

    Returns:
        The toolset version string, or None when the marker file is absent --
        which is exactly what a Visual Studio install without the C++ tools
        looks like.

    Raises:
        ValueError: When the marker file exists but cannot be read.  That is
            a permission or disk fault on this machine, not a missing C++
            workload, so it must not be reported as one -- nor, by escaping
            as an :class:`OSError`, as ``conan`` missing from ``PATH``.
    """
    version_file = os.path.join(
        install_path, "VC", "Auxiliary", "Build", "Microsoft.VCToolsVersion.default.txt"
    )
    if not os.path.isfile(version_file):
        return None
    try:
        with open(version_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        raise ValueError(f"could not read {version_file} ({exc}).") from exc


def check_visual_studio_2019():
    """Check that Visual Studio 2019 (16.x) is installed *with the C++ tools*.

    Locates the install with ``vswhere.exe``, restricted to instances that
    carry the C++ toolset component, and reports the install path and the
    default MSVC toolset version.  A VS2019 carrying only the .NET workload
    is a failure here rather than a crash on the first ``conan create``.

    Returns:
        A :class:`CheckResult`.
    """
    name = "Visual Studio 2019"
    workload_fix = (
        "Install Visual Studio 2019 with the 'Desktop development with C++' "
        "workload; msvc 192 packages can only be built on a machine that has it."
    )
    vswhere = _vswhere_path()
    if not os.path.isfile(vswhere):
        return CheckResult(
            name, False, f"vswhere.exe not found at {vswhere}. {workload_fix}",
        )
    command = [
        vswhere, "-products", "*", "-version", "[16.0,17.0)",
        "-requires", VC_TOOLS_COMPONENT, "-format", "json", "-utf8",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        return CheckResult(name, False, f"could not run vswhere ({exc}).")
    try:
        instances = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return CheckResult(name, False, f"could not parse vswhere output ({exc}).")
    if not instances:
        return CheckResult(
            name, False,
            f"no Visual Studio 2019 (16.x) installation with the C++ toolset "
            f"({VC_TOOLS_COMPONENT}) found. {workload_fix} Or build the msvc 194 "
            f"matrix in CI instead.",
        )
    instance = instances[0]
    install_path = instance.get("installationPath", "")
    try:
        toolset = _msvc_toolset_version(install_path)
    except ValueError as exc:
        # An unreadable marker file is a fault on this machine, not a
        # verdict on the workload: report what actually failed and let the
        # remaining checks still run.
        return CheckResult(name, False, str(exc))
    if toolset is None:
        return CheckResult(
            name, False,
            f"{install_path} has no Microsoft.VCToolsVersion.default.txt, so it "
            f"carries no MSVC toolset. {workload_fix}",
        )
    return CheckResult(
        name, True,
        f"{install_path} (VS {instance.get('installationVersion', 'unknown')}, "
        f"MSVC toolset {toolset})",
    )


_CONAN_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def check_conan_version():
    """Check that the conan client matches the pinned patch series.

    CI pins ``conan~=2.31.0`` because a minor bump can change package_id
    computation, which would silently detach locally built packages from
    the published binaries.  The same reasoning applies to a manual build,
    so a mismatch is an error rather than a note.

    Returns:
        A :class:`CheckResult`.
    """
    name = "conan client"
    fix = f'Run: pip install "conan{CONAN_PIN}"'
    try:
        result = subprocess.run(
            ["conan", "--version"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return CheckResult(name, False, f"could not run `conan --version` ({exc}). {fix}")
    match = _CONAN_VERSION_RE.search(result.stdout or "")
    if match is None:
        return CheckResult(
            name, False,
            f"could not parse a version from `conan --version` output "
            f"{(result.stdout or '').strip()!r}. {fix}",
        )
    if (int(match.group(1)), int(match.group(2))) != CONAN_PINNED_SERIES:
        return CheckResult(
            name, False,
            f"conan {match.group(0)} is outside the pinned {CONAN_PIN} series. A "
            f"minor bump can change package_id computation and silently detach "
            f"these builds from the published binaries. {fix}",
        )
    return CheckResult(name, True, f"conan {match.group(0)}")


def check_remote_configured(remote=VS2019_REMOTE_NAME):
    """Check that ``remote`` is present *and enabled* in ``conan remote list``.

    ``conan remote list`` prints one ``name: url [Verify SSL: …, Enabled: …]``
    line per remote and keeps printing disabled ones, so matching the name
    alone passes a remote that then fails every download.

    Args:
        remote: Remote name to look for.

    Returns:
        A :class:`CheckResult`.
    """
    name = f"conan remote {remote}"
    try:
        result = subprocess.run(
            ["conan", "remote", "list"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return CheckResult(name, False, f"could not run `conan remote list` ({exc}).")
    prefix = f"{remote}:"
    matching = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith(prefix)
    ]
    if not matching:
        return CheckResult(
            name, False,
            f"remote {remote!r} is not configured. Run: "
            f"xmsconan_vs2019 setup --password-file <path>",
        )
    if "Enabled: False" in matching[0]:
        return CheckResult(
            name, False,
            f"remote {remote!r} is configured but disabled, so every download "
            f"from it will fail. Run: conan remote enable {remote}",
        )
    return CheckResult(name, True, "configured")


def print_check(check):
    """Print one check result in the preflight's OK/FAIL format.

    Args:
        check: The :class:`CheckResult` to print.

    Returns:
        ``check``, so a caller can print and collect in one expression.
    """
    print(f"  [{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    return check


def run_preflight(remote=VS2019_REMOTE_NAME):
    """Run every *machine* preflight check and print the results.

    Only checks that need nothing but the machine live here, because this
    runs from ``setup`` too, where there is no build request to check against.
    The one check that does need the request -- the running interpreter versus
    the pybind configurations, :func:`check_python_versions` -- is run by
    ``build`` afterwards and printed into the same block.

    Args:
        remote: Remote name the build publishes to.

    Returns:
        List of :class:`CheckResult`, one per check.
    """
    print("==> Preflight")
    checks = [
        check_visual_studio_2019(),
        check_conan_version(),
        check_remote_configured(remote),
    ]
    for check in checks:
        print_check(check)
    return checks


# --- setup ---------------------------------------------------------------


def setup(password_file=None, username=None,
          remote_url=VS2019_REMOTE_URL, remote_name=VS2019_REMOTE_NAME):
    """Add and log in to the VS2019 remote, then preflight the machine.

    The remote is *appended* to the machine's remote list rather than
    inserted at index 0.  ``setup`` mutates a developer's global Conan
    configuration permanently, and Conan resolves a version range across
    every remote in order; a VS2019 remote at the front would be the first
    stop for all their unrelated msvc 194 work too, which is exactly the
    mixing this whole workflow exists to prevent.

    Args:
        password_file: Path to a file holding the remote password.
        username: Remote username.  None resolves it from the environment,
            then ``~/.xmsconan.toml``, then defaults to ``"aquaveo"``.
        remote_url: Artifactory URL for the remote.
        remote_name: Conan remote name to add.

    Returns:
        Process exit code: 0 when every preflight check passed,
        :data:`EXIT_USAGE` otherwise.  A failed preflight is "the machine was
        wrong", which is exit 2 by the contract in the module docstring, and
        it is what ``build`` already returns for the same condition.

    Raises:
        ValueError: When ``password_file`` is given but does not exist, is
            empty, or cannot be read.
        ToolNotFoundError: When ``conan`` itself would not start.
    """
    credentials = resolve_credentials(password_file, username)
    print(f"==> Adding conan remote {remote_name} -> {remote_url} (appended)")
    try:
        conan_setup(
            remote_url=remote_url,
            login=True,
            username=credentials.username,
            password=credentials.password,
            remote_name=remote_name,
            index=None,
            # The config file was already consulted by resolve_credentials;
            # a password still None here is a resolved "let conan prompt".
            use_config_file=False,
        )
    except FileNotFoundError as exc:
        raise _tool_not_found("conan", exc) from exc
    checks = run_preflight(remote=remote_name)
    return 0 if all(check.ok for check in checks) else EXIT_USAGE


# --- build ---------------------------------------------------------------


def _library_build_toml(library_dir: str) -> dict:
    """Parse a library's ``build.toml``, or return an empty table if it has none.

    A malformed file is re-raised naming the path. ``load_toml`` raises
    ``TOMLDecodeError`` -- which is a ``ValueError`` -- and the CLI's top-level
    handler catches ``ValueError`` as a bad ``--only`` / ``--from`` / ``--filter``
    argument, so an unreported decode error here surfaced as advice about flags
    the developer never typed.

    Args:
        library_dir: Directory holding the library's ``build.toml``.

    Returns:
        The parsed table, or an empty dict when the file is absent.

    Raises:
        ValueError: When the file exists but does not parse, naming it.
    """
    toml_path = os.path.join(library_dir, "build.toml")
    if not os.path.isfile(toml_path):
        return {}
    try:
        data = load_toml(toml_path)
    except ValueError as exc:
        raise ValueError(f"could not parse {toml_path}: {exc}") from exc
    validate_top_level_keys(data, toml_path)
    return data


def _library_repairs_wheel(library_dir: str) -> bool:
    """Whether this library's Windows wheel should be repaired.

    Read from the library's own ``build.toml`` through the shared resolver, so
    this track agrees with the generated CI and with ``xmsconan publish``. A
    library with no ``build.toml`` here keeps the historical behavior.

    Args:
        library_dir: Directory holding the library's ``build.toml``.

    Returns:
        True when the wheel should be repaired.
    """
    data = _library_build_toml(library_dir)
    if not data:
        return True
    return repairs_windows_wheel(data)


def _library_matrix(library_dir: str) -> dict:
    """Read the ``[matrix]`` table out of a library's ``build.toml``.

    The VS2019 driver builds each library from its own checkout rather than
    through the generated ``build.py``, so it has to pick the table up itself --
    otherwise a library that restricts its matrix would still get the full
    fan-out on this track, and one that asks for a Debug pybind build (which is
    what the desktop products link) would not get it here at all.

    Args:
        library_dir: Directory holding the library's ``build.toml``.

    Returns:
        The ``[matrix]`` table, or an empty dict when the file or the table is
        absent.

    Raises:
        ValueError: When the file exists but does not parse.
    """
    return _library_build_toml(library_dir).get("matrix", {})


def _new_packager(library, conanfile_path, python_versions, matrix=None):
    """Build a packager wired for the VS2019 matrix.

    ``apply_boost_defaults=False`` is not implied by the platform key -- see
    the module docstring for why it must be passed here.

    Args:
        library: Library / Conan package name.
        conanfile_path: Path passed to ``conan create``.
        python_versions: Python versions the pybind variants fan out across.
        matrix: The library's ``[matrix]`` table, restricting which
            configurations are produced. None or empty means the full fan-out.

    Returns:
        An :class:`~xmsconan.package_tools.packager.XmsConanPackager` with no
        configurations generated yet.

    Raises:
        ValueError: When ``matrix`` is malformed, from ``resolve_matrix``.
    """
    return XmsConanPackager(
        library,
        conanfile_path=conanfile_path,
        build_missing=True,
        apply_boost_defaults=False,
        python_versions=python_versions,
        # Passed through rather than `matrix or None`: a falsey non-dict
        # (matrix = []) has to reach resolve_matrix and be rejected, the way it
        # is on every other entry point, instead of being read as "no
        # restriction" and quietly restoring the full fan-out.
        matrix=matrix,
    )


def _configure(packager, config_filter):
    """Generate the VS2019 matrix on ``packager`` and apply ``config_filter``.

    Args:
        packager: An :class:`~xmsconan.package_tools.packager.XmsConanPackager`.
        config_filter: Filter dict for ``filter_configurations``, or None.

    Returns:
        The resulting configuration list.
    """
    packager.generate_configurations(PLATFORM_KEY)
    if config_filter:
        packager.filter_configurations(config_filter)
    return packager.configurations


def _pybind_python_versions(configurations):
    """Return the ``python_version`` values of the pybind configurations.

    Every pybind configuration carries a ``"X.Y"`` ``python_version`` -- the
    packager sets it as it fans the variants out -- so the sort is numeric
    rather than lexicographic; ``"3.7"`` sorts after ``"3.13"`` as a string.

    Args:
        configurations: A generated (and filtered) configuration list.

    Returns:
        Sorted list of ``"X.Y"`` strings; empty when nothing in
        ``configurations`` builds pybind.
    """
    versions = {
        configuration.get("options", {}).get("python_version")
        for configuration in configurations
        if configuration.get("options", {}).get("pybind")
    }
    return sorted(versions, key=lambda v: tuple(int(part) for part in v.split(".")))


def check_python_versions(libraries, python_versions=None, config_filter=None, root="."):
    """Check the interpreter running conan against the pybind configurations.

    The recipe hands CMake ``Python3_EXECUTABLE = sys.executable`` while the
    generated ``CMakeLists.txt`` requires ``find_package(Python3
    ${PYTHON_TARGET_VERSION} EXACT REQUIRED)``, so the interpreter running
    conan *is* the target Python.  CI gets that for free from
    ``actions/setup-python``; on a workstation the developer supplies it, and
    a mismatch fails every pybind configuration at configure time -- an hour
    or two in, after the twelve configurations that do not care have built.

    This is deliberately not one of the :func:`run_preflight` checks.  It has
    to see the *generated and filtered* matrix: twelve of the fourteen
    configurations have ``pybind=False`` and are indifferent to the running
    interpreter, and ``--filter`` can remove the pybind pair outright, so
    checking the flag values alone would fail builds that work today.  The
    matrix is a function of the platform key, ``python_versions``,
    ``config_filter`` and the library's own ``[matrix]`` table, so it is
    generated for the first selected library, whose table is the one read.

    Args:
        libraries: :class:`LibrarySpec` list from :func:`select_libraries`;
            must not be empty (the caller rejects an empty selection first).
        python_versions: Python versions for the pybind variants.
        config_filter: Filter dict for ``filter_configurations``, or None.
        root: Directory holding the library checkouts, used to find each
            library's ``build.toml``. Defaults to the working directory, matching
            the ``--root`` default; a missing file means the full fan-out.

    Returns:
        A :class:`CheckResult`.  ``ok`` is True when the filtered matrix has
        no pybind configuration at all, or when every pybind configuration
        targets the running version.

    Raises:
        ValueError: When ``python_versions`` holds an entry the packager
            rejects, or ``config_filter`` names an unknown key.
    """
    name = "python interpreter"
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    # Every selected library is asked, not just the first. Each one's [matrix]
    # decides whether it has pybind configurations at all, so answering from
    # libraries[0] alone would report "nothing to check" for the whole run
    # whenever the first library happened to restrict them away -- while the
    # rest went on to build against the wrong interpreter.
    requested = []
    for library in libraries:
        packager = _new_packager(
            library.name, ".", python_versions,
            matrix=_library_matrix(os.path.join(root, library.name)),
        )
        for version in _pybind_python_versions(_configure(packager, config_filter)):
            if version not in requested:
                requested.append(version)
    requested.sort(key=version_sort_key)
    if not requested:
        return CheckResult(
            name, True, f"Python {running} (no pybind configuration in this matrix)",
        )
    if requested == [running]:
        return CheckResult(name, True, f"Python {running}, matching the pybind matrix")
    return CheckResult(
        name, False,
        f"this run would build pybind configurations for Python "
        f"{', '.join(requested)}, but conan is running under Python {running} "
        f"({sys.executable}). The recipe points CMake at the interpreter "
        f"running conan and the generated CMakeLists.txt requires that exact "
        f"version, so every pybind configuration would fail at configure time. "
        f"Either re-run from a Python {requested[0]} environment (a virtualenv "
        f"of that version with conan and xmsconan installed), or pass "
        f"--python-versions {running} to build for the interpreter you are "
        f"already running.",
    )


def preview(libraries, python_versions=None, config_filter=None, root="."):
    """Print the configuration matrix for each library without building.

    Args:
        libraries: :class:`LibrarySpec` list from :func:`select_libraries`.
        python_versions: Python versions for the pybind variants.
        config_filter: Filter dict for ``filter_configurations``, or None.
        root: Directory holding the library checkouts, used to read each
            library's ``[matrix]`` table so the preview matches what a build
            would produce. Defaults to the working directory, matching the
            ``--root`` default; a library with no ``build.toml`` there previews
            the full fan-out.

    Returns:
        Process exit code (always 0 -- nothing was built).
    """
    for library in libraries:
        packager = _new_packager(
            library.name, ".", python_versions,
            matrix=_library_matrix(os.path.join(root, library.name)),
        )
        configurations = _configure(packager, config_filter)
        print(f"==> {library.name}: {len(configurations)} configuration(s)")
        packager.print_configuration_table()
    return 0


def extract_wheels(packager, configurations, wheel_dir, version=None, repair=True):
    """Copy the pybind wheels out of the Conan cache and stage the repair libs.

    Both halves are what the generated ``build.py`` does in CI, in the same
    order and for the same reasons:

    * ``extract_wheel`` returns False on a *partial* fan-out -- wheels found
      for some ``--python-versions`` entries but not all -- so its result is
      propagated rather than reduced to "something was copied".  This driver
      controls ``--python-versions``, so a partial result means the developer
      asked for two wheels and is about to publish one.
    * ``collect_dependency_libs`` fills ``<wheel_dir>/libs``, which is not
      optional for a library that *is* repaired: ``xmsconan_wheel_repair
      --platform windows`` hands ``delvewheel repair --add-path
      <wheel_dir>/libs`` unconditionally, and a repair that finds nothing there
      produces a wheel that imports on the build box and fails everywhere else.
      A library whose ``build.toml`` turns Windows repair off gets neither: the
      staging exists only to feed the repair, and this track must not hand back
      the vendored CRT that the option exists to avoid.

    An empty pybind set short-circuits: ``extract_wheel`` searches the whole
    local cache, so on a machine that built a wheel last week it would happily
    copy *that* wheel out and report success for a matrix that never built one.

    Args:
        packager: The :class:`~xmsconan.package_tools.packager.XmsConanPackager`
            that just ran; it carries the ``python_versions`` the fan-out is
            checked against.
        configurations: The configurations that were built.
        wheel_dir: Directory to copy the ``.whl`` files into.
        version: Package version, or None to match any version in the cache.
        repair: Whether this library's wheel gets repaired, from
            ``[ci].windows_wheel_repair``. False skips the dependency-libs
            staging, which exists only to feed the repair.

    Returns:
        True when a wheel was extracted for every requested Python version.
    """
    if not _pybind_python_versions(configurations):
        print(
            f"==> no pybind configuration was built, so there is no wheel to "
            f"extract into {wheel_dir}"
        )
        return False
    if not packager.extract_wheel(wheel_dir, version=version or "*"):
        return False
    if repair:
        packager.collect_dependency_libs(os.path.join(wheel_dir, "libs"))
    else:
        print(
            "==> [ci].windows_wheel_repair is false: not staging dependency "
            "libraries, and the wheel should not be repaired."
        )
    return True


def build_library(library: LibrarySpec, root, version=None, generate=True,
                  python_versions=None, config_filter=None, log_dir=None,
                  wheel_dir=None):
    """Generate build files for one library and build the VS2019 matrix.

    A missing checkout or ``build.toml`` is reported and skipped rather than
    failing the run, so a partially migrated stack still builds what it can.
    A run in which *every* library skipped is still a nonzero exit -- see
    :func:`print_summary`.

    Args:
        library: The :class:`LibrarySpec` to build.  Its ``name`` is both the
            Conan package name and the directory name under ``root``.
        root: Directory holding the library checkouts.
        version: Version passed to ``xmsconan_gen``.
        generate: Run ``xmsconan_gen`` before building.
        python_versions: Python versions for the pybind variants.
        config_filter: Filter dict for ``filter_configurations``, or None.
        log_dir: Directory for per-configuration ``conan create`` logs.
        wheel_dir: Directory to extract the pybind wheels into, or None to
            skip wheel handling entirely.  Extraction is skipped when any
            configuration failed: the cache may still hold a wheel from an
            earlier run, and staging *that* for ``xmsconan_wheel_deploy`` is
            worse than staging none.

    Returns:
        A :class:`LibraryResult`.

    Raises:
        ToolNotFoundError: When ``xmsconan_gen`` itself would not start.  That
            is the machine being wrong, not this library failing: it would
            fail identically for every remaining library, so it aborts the run
            with :data:`EXIT_USAGE` instead of being counted as a build
            failure and (without ``--continue-on-error``) exiting 1.
    """
    start = time.monotonic()
    name = library.name
    library_dir = os.path.join(root, name)
    if not os.path.isdir(library_dir):
        return LibraryResult(name, "skipped", message=f"no checkout at {library_dir}")
    if not os.path.isfile(os.path.join(library_dir, "build.toml")):
        return LibraryResult(
            name, "skipped", message=f"no build.toml in {library_dir}"
        )

    if generate:
        command = ["xmsconan_gen"]
        if version:
            command += ["--version", version]
        command.append("build.toml")
        print(f"==> {name}: generating build files")
        try:
            subprocess.run(command, cwd=library_dir, check=True)
        except FileNotFoundError as exc:
            raise _tool_not_found("xmsconan_gen", exc) from exc
        except subprocess.CalledProcessError as exc:
            return LibraryResult(
                name, "failed", elapsed=time.monotonic() - start,
                message=f"xmsconan_gen failed: {exc}",
            )

    packager = _new_packager(
        name, os.path.join(library_dir, "conanfile.py"), python_versions,
        matrix=_library_matrix(library_dir),
    )
    configurations = _configure(packager, config_filter)
    attempted = len(configurations)
    if not attempted:
        return LibraryResult(
            name, "skipped", elapsed=time.monotonic() - start,
            message="no configurations matched --filter",
        )
    print(f"==> {name}: building {attempted} configuration(s)")
    failed = packager.run(log_dir=log_dir)
    wheels = None
    if wheel_dir and not failed:
        wheels = extract_wheels(
            packager, configurations, wheel_dir, version,
            repair=_library_repairs_wheel(library_dir),
        )
    return LibraryResult(
        name,
        "failed" if failed else "ok",
        attempted=attempted,
        succeeded=attempted - failed,
        failed=failed,
        elapsed=time.monotonic() - start,
        wheels=wheels,
    )


def build(libraries, root, version=None, generate=True, python_versions=None,
          config_filter=None, log_dir=None, wheel_dir=None,
          continue_on_error=False):
    """Build every selected library in dependency order.

    ``packager.run()`` already continues past an individual failing
    configuration and returns the failure count, so ``continue_on_error``
    only controls whether the *next library* is attempted after one fails.

    Args:
        libraries: :class:`LibrarySpec` list from :func:`select_libraries`.
        root: Directory holding the library checkouts.
        version: Version passed to ``xmsconan_gen``; also exported as
            ``XMS_VERSION`` so it reaches each profile's ``[buildenv]``, the
            same way CI supplies it.
        generate: Run ``xmsconan_gen`` before building each library.
        python_versions: Python versions for the pybind variants.
        config_filter: Filter dict for ``filter_configurations``, or None.
        log_dir: Directory for per-configuration ``conan create`` logs.
        wheel_dir: Directory to extract the pybind wheels into, or None.
        continue_on_error: Keep going to the next library after a failure.

    Returns:
        List of :class:`LibraryResult`, one per library attempted.
    """
    if version:
        os.environ["XMS_VERSION"] = version
    results = []
    for library in libraries:
        results.append(build_library(
            library, root, version=version, generate=generate,
            python_versions=python_versions, config_filter=config_filter,
            log_dir=log_dir, wheel_dir=wheel_dir,
        ))
        if results[-1].status == "failed" and not continue_on_error:
            print(
                f"==> Stopping after {library.name} failed "
                f"(pass --continue-on-error to keep going)."
            )
            break
    return results


def _wheel_files(wheel_dir):
    """Return the sorted ``.whl`` filenames in ``wheel_dir``.

    Args:
        wheel_dir: Directory the wheels were extracted into.  A directory that
            was never created is the ordinary "nothing was extracted" case, so
            it yields an empty list rather than raising.

    Returns:
        Sorted list of filenames.
    """
    if not os.path.isdir(wheel_dir):
        return []
    return sorted(name for name in os.listdir(wheel_dir) if name.endswith(".whl"))


def print_wheel_summary(results, wheel_dir):
    """Print what is staged in ``wheel_dir`` and whether anything is missing.

    Everything in the directory is listed, not just what this run copied in:
    the whole directory is what ``xmsconan_wheel_repair`` and
    ``xmsconan_wheel_deploy`` act on next, so a wheel left over from an
    earlier run or a different version is about to be published too.

    Args:
        results: :class:`LibraryResult` list from :func:`build`.
        wheel_dir: Directory the wheels were extracted into.

    Returns:
        True when every library that got as far as extraction produced a
        wheel for every requested Python version.
    """
    wheels = _wheel_files(wheel_dir)
    location = os.path.abspath(wheel_dir)
    if wheels:
        print(f"\n==> Wheels: {len(wheels)} in {location}: {', '.join(wheels)}")
        print(
            f"    Next: xmsconan_wheel_repair --wheel-dir {wheel_dir} "
            f"--platform windows, then xmsconan_wheel_deploy --wheel-dir "
            f"{wheel_dir}"
        )
    else:
        print(f"\n==> Wheels: none in {location}")
    missing = [result.name for result in results if result.wheels is False]
    if missing:
        print(
            f"error: --wheel-dir was given but no complete set of wheels came "
            f"out of {', '.join(missing)}. Either the matrix built no pybind "
            f"configuration -- select it with --filter "
            f"'{{\"options\": {{\"pybind\": true}}}}' -- or only part of the "
            f"--python-versions fan-out produced one; pass exactly the version "
            f"this interpreter is running.",
            file=sys.stderr,
        )
        return False
    return True


def print_summary(results, wheel_dir=None):
    """Print the per-library summary table and return the exit code.

    Args:
        results: :class:`LibraryResult` list from :func:`build`.
        wheel_dir: Directory wheels were extracted into, or None when
            ``--wheel-dir`` was not passed.

    Returns:
        Process exit code: 1 when any library failed, 3 when the run finished
        without a single library reaching ``'ok'``, 1 again when a wheel was
        asked for and none was produced, 0 otherwise.  "Nothing happened" is
        not success -- a typo'd ``--root`` skips every library, and an ``&&``
        chain or wrapper script reading exit 0 would go on to upload or tag a
        release that was never built.  A missing wheel is the same kind of
        event as a failed configuration -- the run was asked for an artifact
        and did not produce it -- so it reuses code 1 rather than inventing a
        fourth; the following ``xmsconan_wheel_repair`` would otherwise be
        handed an empty directory.
    """
    rows = [
        [r.name, r.status, r.attempted, r.succeeded, r.failed,
         f"{r.elapsed:.1f}s", r.message]
        for r in results
    ]
    print("\n==> Summary")
    print(tabulate(
        rows,
        headers=["library", "status", "attempted", "succeeded", "failed",
                 "elapsed", "notes"],
        tablefmt="psql",
    ))
    wheels_ok = print_wheel_summary(results, wheel_dir) if wheel_dir else True
    if any(r.status == "failed" for r in results):
        return 1
    if not any(r.status == "ok" for r in results):
        print(
            "error: nothing was built -- every library was skipped. Check "
            "--root, --only/--from, and --filter.",
            file=sys.stderr,
        )
        return EXIT_NOTHING_BUILT
    return 0 if wheels_ok else 1


# --- upload --------------------------------------------------------------


def upload(library, version, remote=VS2019_REMOTE_NAME, allow_other_remote=False):
    """Upload one library's msvc 192 packages to the VS2019 remote.

    Separate from :func:`build` on purpose: a build never uploads as a side
    effect.

    Two guards keep this from contaminating the CI remote.  The upload is
    restricted to ``compiler.version=192`` binaries, because ``conan upload``
    otherwise matches by *reference* only and would push every binary of that
    version in the local cache -- and the VS2019 build box is usually also a
    normal msvc 194 dev machine.  And a remote other than ``aquaveo-vs2019``
    is refused unless the caller says so explicitly, because ``--remote
    aquaveo`` is one word away from ``--remote aquaveo-vs2019`` and publishes
    legacy binaries straight into the remote every CI consumer resolves
    against.

    Args:
        library: Library / Conan package name.
        version: Explicit package version; every ``<library>/<version>*``
            package in the local cache matching the msvc 192 query is
            uploaded.
        remote: Conan remote name.
        allow_other_remote: Permit a remote other than
            :data:`~xmsconan.constants.VS2019_REMOTE_NAME`.

    Returns:
        Process exit code: 0 when the upload succeeded, 1 when ``conan
        upload`` failed.  ``packager.upload`` already returns that shape, so
        it is propagated unchanged -- ``xmsconan_vs2019 upload`` must not
        exit 0 on a publish that never landed.

    Raises:
        ValueError: When ``remote`` is not the VS2019 remote and
            ``allow_other_remote`` is False.
        ToolNotFoundError: When ``conan`` itself would not start.
    """
    if remote != VS2019_REMOTE_NAME and not allow_other_remote:
        raise ValueError(
            f"refusing to upload msvc {MSVC_VS2019_VERSION} packages to remote "
            f"{remote!r}: they belong on {VS2019_REMOTE_NAME!r} and must not mix "
            f"with the CI-published binaries. Pass --allow-other-remote if you "
            f"really mean it."
        )
    packager = XmsConanPackager(library, apply_boost_defaults=False)
    try:
        return packager.upload(
            version, remote=remote,
            package_query=f"compiler.version={MSVC_VS2019_VERSION}",
        )
    except FileNotFoundError as exc:
        raise _tool_not_found("conan", exc) from exc


# --- CLI -----------------------------------------------------------------


def _parse_filter(text):
    """Parse the ``--filter`` JSON object.

    Args:
        text: JSON text, or None / empty for no filter.

    Returns:
        The filter dict, or None.

    Raises:
        ValueError: When ``text`` is not valid JSON or is not an object.
    """
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--filter is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(
            '--filter must be a JSON object, e.g. \'{"build_type": "Release"}\''
        )
    return value


def _add_setup_parser(subparsers):
    """Add the ``setup`` subcommand.

    Args:
        subparsers: The subparser action returned by ``add_subparsers``.
    """
    setup_parser = subparsers.add_parser(
        "setup",
        help=f"Add and log in to the {VS2019_REMOTE_NAME} remote, then preflight.",
    )
    setup_parser.add_argument(
        "--password-file", default=None,
        help="Path to a file holding the remote password on its own line, e.g. "
             "C:\\path\\to\\conan-password.txt. "
             "Falls back to $CONAN_PASSWORD, then ~/.xmsconan.toml.",
    )
    setup_parser.add_argument(
        "--username", default=None,
        help=f"Remote username. Falls back to $CONAN_LOGIN_USERNAME, then "
             f"~/.xmsconan.toml, then {DEFAULT_REMOTE_USERNAME}.",
    )
    setup_parser.add_argument(
        "--remote-url", default=VS2019_REMOTE_URL,
        help=f"Remote URL (default: {VS2019_REMOTE_URL}).",
    )
    setup_parser.add_argument(
        "--remote-name", default=VS2019_REMOTE_NAME,
        help=f"Conan remote name to add (default: {VS2019_REMOTE_NAME}). Pair it "
             f"with --remote-url when pointing at a different Artifactory repo.",
    )


def _add_build_parser(subparsers):
    """Add the ``build`` subcommand.

    Args:
        subparsers: The subparser action returned by ``add_subparsers``.
    """
    build_parser = subparsers.add_parser(
        "build", help="Build the VS2019 matrix for the enabled libraries.",
    )
    build_parser.add_argument(
        "--root", default=".",
        help="Directory holding the library checkouts (default: the current "
             "directory).",
    )
    build_parser.add_argument(
        "--only", action="append", metavar="LIB", default=None,
        help="Only build this library; repeatable. Overrides the enabled flag.",
    )
    build_parser.add_argument(
        "--from", dest="start_from", metavar="LIB", default=None,
        help="Resume the stack at this library, skipping the ones before it.",
    )
    build_parser.add_argument(
        "--preview", action="store_true",
        help="Print the configuration matrix and exit without building.",
    )
    build_parser.add_argument(
        "--continue-on-error", action="store_true",
        help="Keep going to the next library after one fails.",
    )
    build_parser.add_argument(
        "--no-generate", action="store_true",
        help="Skip the xmsconan_gen step (use the conanfile.py already there).",
    )
    build_parser.add_argument(
        "--log-dir", default=None,
        help="Write each configuration's conan create output to "
             "<log-dir>/<library>-<config>.log instead of the console.",
    )
    build_parser.add_argument(
        "--python-versions", nargs="+", metavar="X.Y",
        default=DEFAULT_PYTHON_VERSIONS,
        help=f"Python versions for the pybind variants (default: "
             f"{' '.join(DEFAULT_PYTHON_VERSIONS)}). A pybind build only works "
             f"when this matches the Python running conan, so build wheels one "
             f"version per run, from a virtual environment of that version.",
    )
    build_parser.add_argument(
        "--wheel-dir", default=None, metavar="DIR",
        help="After the build, copy each pybind package's .whl into DIR and "
             "fill DIR/libs with the shared libraries the repair step needs. "
             "Repairing and publishing stay separate commands: "
             "xmsconan_wheel_repair --wheel-dir DIR --platform windows, then "
             "xmsconan_wheel_deploy --wheel-dir DIR.",
    )
    build_parser.add_argument(
        "--filter", default=None, dest="config_filter",
        help="JSON filter for the configuration matrix, e.g. "
             "'{\"build_type\": \"Release\"}'.",
    )
    build_parser.add_argument(
        "--version", default=None,
        help="Package version; passed to xmsconan_gen and exported as "
             "XMS_VERSION.",
    )
    build_parser.add_argument(
        "--remote-name", default=VS2019_REMOTE_NAME,
        help=f"Conan remote the preflight check requires (default: "
             f"{VS2019_REMOTE_NAME}). Match it to the --remote-name you gave "
             f"setup; otherwise preflight fails on a remote you never added.",
    )


def _add_upload_parser(subparsers):
    """Add the ``upload`` subcommand.

    Args:
        subparsers: The subparser action returned by ``add_subparsers``.
    """
    upload_parser = subparsers.add_parser(
        "upload",
        help=f"Upload built msvc {MSVC_VS2019_VERSION} packages to the "
             f"{VS2019_REMOTE_NAME} remote.",
    )
    upload_parser.add_argument(
        "--library", required=True, help="Library / Conan package name.",
    )
    upload_parser.add_argument(
        "--version", required=True,
        help="Package version to upload (no wildcard default -- be explicit).",
    )
    upload_parser.add_argument(
        "--remote", default=VS2019_REMOTE_NAME,
        help=f"Conan remote to upload to (default: {VS2019_REMOTE_NAME}). Any "
             f"other remote is refused without --allow-other-remote.",
    )
    upload_parser.add_argument(
        "--allow-other-remote", action="store_true",
        help=f"Permit --remote values other than {VS2019_REMOTE_NAME}. Publishing "
             f"msvc {MSVC_VS2019_VERSION} binaries to the CI remote is almost "
             f"always a typo.",
    )


def _build_parser():
    """Build the ``xmsconan_vs2019`` argument parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    # No explicit prog: xmsconan/cli.py rewrites sys.argv[0] so the same
    # parser prints "xmsconan vs2019" when dispatched through the unified CLI.
    parser = argparse.ArgumentParser(
        description=(
            "Build and publish the Visual Studio 2019 (msvc 192) Conan "
            "packages from a developer workstation."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_setup_parser(subparsers)
    _add_build_parser(subparsers)
    _add_upload_parser(subparsers)
    return parser


def _os_failure(exc):
    """Print an OS-level failure and return the process exit code.

    A :class:`ToolNotFoundError` already names the executable and points at
    ``PATH``.  Every other :class:`OSError` -- an unreadable password file, a
    build profile that cannot be written -- is reported verbatim, because
    blaming ``PATH`` for a disk or permission fault sends the reader looking
    in the wrong place entirely.

    Args:
        exc: The raised :class:`OSError`.

    Returns:
        The process exit code to use.
    """
    print(f"error: {exc}", file=sys.stderr)
    return EXIT_USAGE


def _run_setup(args):
    """Run the ``setup`` subcommand.  Returns a process exit code."""
    try:
        return setup(
            password_file=args.password_file,
            username=args.username,
            remote_url=args.remote_url,
            remote_name=args.remote_name,
        )
    except ValueError as exc:  # unusable --password-file
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        return _os_failure(exc)
    except subprocess.CalledProcessError as exc:
        # Deliberately not printing `exc`: its .cmd is the full conan argv.
        # The password is no longer in there, but there is no reason to make
        # the next credential added to that command line a leak either.
        print(f"error: conan setup failed (exit {exc.returncode}).", file=sys.stderr)
        return exc.returncode


def _run_build(args):
    """Run the ``build`` subcommand.  Returns a process exit code."""
    try:
        libraries = select_libraries(only=args.only, start_from=args.start_from)
        config_filter = _parse_filter(args.config_filter)
        if not libraries:
            # --only and --from can disagree, and --from past the last enabled
            # library selects nothing.  Either way the developer asked for a
            # build that cannot happen; exiting 0 would tell a wrapper script
            # the stack is built.
            print(
                "error: no libraries selected -- --only and --from together "
                "match nothing, or nothing after --from is enabled.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if not os.path.isdir(args.root):
            # A missing *library* is a skip, because the stack is only
            # partially migrated.  A missing *root* is a typo -- no library
            # could possibly be found under it.
            #
            # Checked before --preview, not after: the preview reads each
            # library's [matrix] from under --root, so a mistyped root would
            # otherwise print a confident unrestricted fan-out and exit 0.
            print(
                f"error: --root directory not found: {args.root}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if args.preview:
            return preview(
                libraries, python_versions=args.python_versions,
                config_filter=config_filter, root=args.root,
            )
        # The interpreter check runs after the matrix exists and --filter has
        # been applied -- the answer depends on what is actually going to be
        # built, not on the flag values -- and prints into the same block.
        checks = run_preflight(remote=args.remote_name) + [print_check(
            check_python_versions(
                libraries, python_versions=args.python_versions,
                config_filter=config_filter, root=args.root,
            )
        )]
        if not all(check.ok for check in checks):
            print(
                "error: preflight failed; fix the items above and re-run.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        results = build(
            libraries, args.root, version=args.version,
            generate=not args.no_generate, python_versions=args.python_versions,
            config_filter=config_filter, log_dir=args.log_dir,
            wheel_dir=args.wheel_dir, continue_on_error=args.continue_on_error,
        )
        return print_summary(results, wheel_dir=args.wheel_dir)
    except ValueError as exc:
        # Bad --only / --from / --filter, or a malformed --python-versions
        # entry rejected by the packager.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        return _os_failure(exc)


def _run_upload(args):
    """Run the ``upload`` subcommand.  Returns a process exit code."""
    try:
        return upload(
            args.library, args.version, remote=args.remote,
            allow_other_remote=args.allow_other_remote,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        return _os_failure(exc)


#: Subcommand name -> handler taking the parsed args and returning an exit code.
_HANDLERS = {
    "setup": _run_setup,
    "build": _run_build,
    "upload": _run_upload,
}


def main(argv=None):
    """CLI entry point for ``xmsconan_vs2019``.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Raises:
        SystemExit: Always -- carries the process exit code so that both the
            console script and the ``xmsconan vs2019`` dispatcher propagate it.
    """
    args = _build_parser().parse_args(argv)
    sys.exit(_HANDLERS[args.command](args))
