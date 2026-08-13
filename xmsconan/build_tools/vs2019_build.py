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

Process exit codes are the contract with whatever wrapper script drives
this, so they are distinct:

* ``0`` -- everything asked for happened.
* ``1`` -- a library failed to build, or ``conan upload`` failed.
* ``2`` -- the request or the machine was wrong: a bad flag, a ``--root``
  that does not exist, a selection that matches no library, a failed
  preflight, a missing password file, or ``conan`` not on ``PATH``.
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
from pathlib import Path
import re
import subprocess
import sys
import time

from tabulate import tabulate

from xmsconan.ci_tools.conan_setup import conan_setup
from xmsconan.ci_tools.credentials import load_conan_credentials
from xmsconan.constants import (
    MSVC_VS2019_VERSION, VS2019_REMOTE_NAME, VS2019_REMOTE_URL,
)
from xmsconan.package_tools.packager import XmsConanPackager

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
DEFAULT_PYTHON_VERSIONS = ["3.10", "3.13"]

#: Process exit code for "the run completed but produced nothing".
EXIT_NOTHING_BUILT = 3
#: Process exit code for a bad request or an unusable machine.
EXIT_USAGE = 2


@dataclass(frozen=True)
class LibrarySpec:
    """One XMS library in the build stack.

    Attributes:
        name: Directory name of the checkout under ``--root``, which is also
            the Conan package name.
        enabled: Whether a plain ``build`` run includes this library.  Flip a
            library on (a one-line change) once it has a Conan 2 recipe.
        note: Why the library is disabled, or what makes it special.
    """

    name: str
    enabled: bool
    note: str = ""


#: The XMS stack in dependency order -- each library is built against the
#: packages produced by the ones above it, so the order matters.  Only
#: ``xmscore`` has a Conan 2 recipe today; the rest are listed so that
#: enabling one as it migrates is a single-flag change.
LIBRARIES = (
    LibrarySpec("xmscore", True, "The only library with a Conan 2 recipe today."),
    LibrarySpec("xmsgrid", False, "Awaiting Conan 2 migration."),
    LibrarySpec("xmsinterp", False, "Awaiting Conan 2 migration."),
    LibrarySpec("xmsmesher", False, "Awaiting Conan 2 migration."),
    LibrarySpec("xmsextractor", False, "Awaiting Conan 2 migration."),
    LibrarySpec("xmsstamper", False, "Awaiting Conan 2 migration."),
    LibrarySpec("xmsconstraint", False, "Awaiting Conan 2 migration."),
    LibrarySpec("xmsgridtrace", False, "Awaiting Conan 2 migration."),
)


@dataclass
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
    """

    name: str
    status: str
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    elapsed: float = 0.0
    message: str = ""


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

    Args:
        password_file: Path to a file holding the password on its own.
            Trailing whitespace (the editor's newline) is stripped.
        username: Username from the command line, or None.

    Returns:
        A ``(username, password)`` tuple.  ``password`` is None when no
        source supplied one -- in which case Conan is left to prompt
        interactively.  ``username`` is never None.

    Raises:
        ValueError: When ``password_file`` is given but does not exist.  A
            typo'd path must not silently fall through to another source.
    """
    password = None
    if password_file:
        path = Path(password_file)
        if not path.is_file():
            raise ValueError(f"password file not found: {password_file}")
        password = path.read_text(encoding="utf-8").rstrip()
    else:
        password = os.environ.get("CONAN_PASSWORD")
    username = username or os.environ.get("CONAN_LOGIN_USERNAME")
    if not username or not password:
        credentials = load_conan_credentials()
        username = username or credentials.get("username")
        password = password or credentials.get("password")
    return username or DEFAULT_REMOTE_USERNAME, password


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
    """
    version_file = os.path.join(
        install_path, "VC", "Auxiliary", "Build", "Microsoft.VCToolsVersion.default.txt"
    )
    if not os.path.isfile(version_file):
        return None
    with open(version_file, "r", encoding="utf-8") as handle:
        return handle.read().strip()


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
    toolset = _msvc_toolset_version(install_path)
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


def run_preflight(remote=VS2019_REMOTE_NAME):
    """Run every preflight check and print the results.

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
        print(f"  [{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
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
        Process exit code: 0 when every preflight check passed, 1 otherwise.

    Raises:
        ValueError: When ``password_file`` is given but does not exist.
    """
    username, password = resolve_credentials(password_file, username)
    print(f"==> Adding conan remote {remote_name} -> {remote_url} (appended)")
    conan_setup(
        remote_url=remote_url,
        login=True,
        username=username,
        password=password,
        remote_name=remote_name,
        index=None,
    )
    checks = run_preflight(remote=remote_name)
    return 0 if all(check.ok for check in checks) else 1


# --- build ---------------------------------------------------------------


def _new_packager(library, conanfile_path, python_versions):
    """Build a packager wired for the VS2019 matrix.

    ``apply_boost_defaults=False`` is not implied by the platform key -- see
    the module docstring for why it must be passed here.

    Args:
        library: Library / Conan package name.
        conanfile_path: Path passed to ``conan create``.
        python_versions: Python versions the pybind variants fan out across.

    Returns:
        An :class:`~xmsconan.package_tools.packager.XmsConanPackager` with no
        configurations generated yet.
    """
    return XmsConanPackager(
        library,
        conanfile_path=conanfile_path,
        build_missing=True,
        apply_boost_defaults=False,
        python_versions=python_versions,
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


def preview(libraries, python_versions=None, config_filter=None):
    """Print the configuration matrix for each library without building.

    Args:
        libraries: :class:`LibrarySpec` list from :func:`select_libraries`.
        python_versions: Python versions for the pybind variants.
        config_filter: Filter dict for ``filter_configurations``, or None.

    Returns:
        Process exit code (always 0 -- nothing was built).
    """
    for library in libraries:
        packager = _new_packager(library.name, ".", python_versions)
        configurations = _configure(packager, config_filter)
        print(f"==> {library.name}: {len(configurations)} configuration(s)")
        packager.print_configuration_table()
    return 0


def build_library(library, root, version=None, generate=True, python_versions=None,
                  config_filter=None, log_dir=None):
    """Generate build files for one library and build the VS2019 matrix.

    A missing checkout or ``build.toml`` is reported and skipped rather than
    failing the run, so a partially migrated stack still builds what it can.
    A run in which *every* library skipped is still a nonzero exit -- see
    :func:`print_summary`.

    Args:
        library: Library name; also its directory name under ``root``.
        root: Directory holding the library checkouts.
        version: Version passed to ``xmsconan_gen``.
        generate: Run ``xmsconan_gen`` before building.
        python_versions: Python versions for the pybind variants.
        config_filter: Filter dict for ``filter_configurations``, or None.
        log_dir: Directory for per-configuration ``conan create`` logs.

    Returns:
        A :class:`LibraryResult`.
    """
    start = time.monotonic()
    library_dir = os.path.join(root, library)
    if not os.path.isdir(library_dir):
        return LibraryResult(library, "skipped", message=f"no checkout at {library_dir}")
    if not os.path.isfile(os.path.join(library_dir, "build.toml")):
        return LibraryResult(
            library, "skipped", message=f"no build.toml in {library_dir}"
        )

    if generate:
        command = ["xmsconan_gen"]
        if version:
            command += ["--version", version]
        command.append("build.toml")
        print(f"==> {library}: generating build files")
        try:
            subprocess.run(command, cwd=library_dir, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            return LibraryResult(
                library, "failed", elapsed=time.monotonic() - start,
                message=f"xmsconan_gen failed: {exc}",
            )

    packager = _new_packager(
        library, os.path.join(library_dir, "conanfile.py"), python_versions
    )
    configurations = _configure(packager, config_filter)
    attempted = len(configurations)
    if not attempted:
        return LibraryResult(
            library, "skipped", elapsed=time.monotonic() - start,
            message="no configurations matched --filter",
        )
    print(f"==> {library}: building {attempted} configuration(s)")
    failed = packager.run(log_dir=log_dir)
    return LibraryResult(
        library,
        "failed" if failed else "ok",
        attempted=attempted,
        succeeded=attempted - failed,
        failed=failed,
        elapsed=time.monotonic() - start,
    )


def build(libraries, root, version=None, generate=True, python_versions=None,
          config_filter=None, log_dir=None, continue_on_error=False):
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
        continue_on_error: Keep going to the next library after a failure.

    Returns:
        List of :class:`LibraryResult`, one per library attempted.
    """
    if version:
        os.environ["XMS_VERSION"] = version
    results = []
    for library in libraries:
        results.append(build_library(
            library.name, root, version=version, generate=generate,
            python_versions=python_versions, config_filter=config_filter,
            log_dir=log_dir,
        ))
        if results[-1].status == "failed" and not continue_on_error:
            print(
                f"==> Stopping after {library.name} failed "
                f"(pass --continue-on-error to keep going)."
            )
            break
    return results


def print_summary(results):
    """Print the per-library summary table and return the exit code.

    Args:
        results: :class:`LibraryResult` list from :func:`build`.

    Returns:
        Process exit code: 1 when any library failed, 3 when the run finished
        without a single library reaching ``'ok'``, 0 otherwise.  "Nothing
        happened" is not success -- a typo'd ``--root`` skips every library,
        and an ``&&`` chain or wrapper script reading exit 0 would go on to
        upload or tag a release that was never built.
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
    if any(r.status == "failed" for r in results):
        return 1
    if not any(r.status == "ok" for r in results):
        print(
            "error: nothing was built -- every library was skipped. Check "
            "--root, --only/--from, and --filter.",
            file=sys.stderr,
        )
        return EXIT_NOTHING_BUILT
    return 0


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
    """
    if remote != VS2019_REMOTE_NAME and not allow_other_remote:
        raise ValueError(
            f"refusing to upload msvc {MSVC_VS2019_VERSION} packages to remote "
            f"{remote!r}: they belong on {VS2019_REMOTE_NAME!r} and must not mix "
            f"with the CI-published binaries. Pass --allow-other-remote if you "
            f"really mean it."
        )
    packager = XmsConanPackager(library, apply_boost_defaults=False)
    return packager.upload(
        version, remote=remote,
        package_query=f"compiler.version={MSVC_VS2019_VERSION}",
    )


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


def _build_parser():
    """Build the ``xmsconan_vs2019`` argument parser."""
    # No explicit prog: xmsconan/cli.py rewrites sys.argv[0] so the same
    # parser prints "xmsconan vs2019" when dispatched through the unified CLI.
    parser = argparse.ArgumentParser(
        description=(
            "Build and publish the Visual Studio 2019 (msvc 192) Conan "
            "packages from a developer workstation."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
             f"{' '.join(DEFAULT_PYTHON_VERSIONS)}).",
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
    return parser


def _conan_missing(exc):
    """Print the message for a ``conan``/``xmsconan_gen`` that would not run.

    Args:
        exc: The :class:`OSError` raised by :mod:`subprocess`.

    Returns:
        The process exit code to use.
    """
    print(f"error: could not run conan ({exc}). Is it on PATH?", file=sys.stderr)
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
    except ValueError as exc:  # missing --password-file
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        return _conan_missing(exc)
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
        if args.preview:
            return preview(
                libraries, python_versions=args.python_versions,
                config_filter=config_filter,
            )
        if not os.path.isdir(args.root):
            # A missing *library* is a skip, because the stack is only
            # partially migrated.  A missing *root* is a typo -- no library
            # could possibly be found under it.
            print(
                f"error: --root directory not found: {args.root}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if not all(check.ok for check in run_preflight()):
            print(
                "error: preflight failed; fix the items above and re-run.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        results = build(
            libraries, args.root, version=args.version,
            generate=not args.no_generate, python_versions=args.python_versions,
            config_filter=config_filter, log_dir=args.log_dir,
            continue_on_error=args.continue_on_error,
        )
        return print_summary(results)
    except ValueError as exc:
        # Bad --only / --from / --filter, or a malformed --python-versions
        # entry rejected by the packager.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        return _conan_missing(exc)


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
        return _conan_missing(exc)


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
