"""Run a built gtest runner as N shards in one container, and aggregate the result.

The generated GitLab ``Run C++ Tests`` job used to get its parallelism from
GitLab itself: ``parallel: N`` started N *jobs*, each on its own runner, each
paying for a container start and a download of the build artifacts -- to run
one Nth of the tests. For a suite that takes a couple of minutes, that fixed
cost was most of the wall clock, and it grew with the shard count rather than
shrinking against it.

This module does the same fan-out inside a single container: one artifact
download, one process per shard. It is not free -- the job now installs
xmsconan to get this script, which the inline shell it replaced did not need --
but that is one install against N container starts and N artifact downloads. The cost of doing so is that N processes now
write to one log, which interleaves line by line and makes a failure
unreadable. So each shard's output is captured to a buffer and replayed whole,
under a banner, once that shard finishes -- and the per-shard gtest XML reports
are merged into one JUnit file the CI can ingest.

Sharding is gtest's own: ``GTEST_TOTAL_SHARDS`` / ``GTEST_SHARD_INDEX`` make a
runner execute a disjoint slice of its cases, so the union of N shards is the
whole suite exactly once.

Related but deliberately separate: :meth:`XmsConanPackager._run_sharded_tests`
shards the same way for ``build.py --test-shards``. That one runs immediately
after the build that produced the runner and already knows the configuration
label; this one starts from downloaded artifacts and has to find them.
"""
# 1. Standard python modules
import argparse
import concurrent.futures
import copy
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
from typing import NamedTuple, Optional, Sequence
from xml.etree import ElementTree

# 2. Third party modules

# 3. Aquaveo modules

LOGGER = logging.getLogger(__name__)

#: Seconds a single shard may run before it is killed. Mirrors
#: ``XmsConanPackager.SHARD_TIMEOUT`` -- a shard is the same kind of work there,
#: including the 10-to-20-minute raise and the headroom argument behind it.
DEFAULT_SHARD_TIMEOUT = 1200

#: Where the recipe's ``_save_test_artifacts`` stages each configuration, and
#: what the generated CI passes to ``build.py --artifacts-dir``.
DEFAULT_ARTIFACTS_DIR = "test_artifacts"

#: Name of the merged JUnit report. The generated GitLab job names the same
#: file in ``artifacts:reports:junit``, so the default and the CI have to agree
#: or the MR widget reports on a file nothing wrote.
DEFAULT_REPORT_NAME = "TEST-cxxtest.xml"

#: Suffix ``_save_test_artifacts`` gives the directory of a ``testing=True``
#: configuration. The label in front of it is the configuration's own
#: (``Release-testing``), which the CI job does not know, so the directory is
#: found by this suffix instead.
_TESTING_LABEL_SUFFIX = "-testing"

#: Which testing configuration an unlabelled run picks when the build staged
#: several. The default matrix stages both ``Release-testing`` and
#: ``Debug-testing``, so "several" is the ordinary case, not an edge one.
#: Debug leads because the shell this replaced selected it -- ``ls -d
#: test_artifacts/*-testing | head -1`` is alphabetical -- and because it is
#: the configuration ``xmsconan coverage`` instruments. The pick is announced,
#: never silent; ``--label`` overrides it.
_LABEL_PREFERENCE = ("Debug-testing", "Release-testing")

#: X display numbers start here and step by one per shard. Each shard owns its
#: number for the life of the run; a stale ``/tmp/.X99-lock`` in the container
#: makes that shard fail loudly with the Xvfb log naming the display, which
#: beats ``xvfb-run -a``'s silent renumbering (and its check-then-bind race).
_XVFB_BASE_DISPLAY = 99

#: How each shard's Xvfb is started. This module owns the servers rather than
#: delegating to ``xvfb-run``, because xvfb-run launches Xvfb and the client
#: back to back with no readiness handshake: under N simultaneous cold starts
#: squeezed into one job container's CPU quota, a runner can dial its display
#: before the server answers, and a GL client that limps past that first
#: failed XOpenDisplay dies later with a fatal BadAccess on GLXMakeCurrent,
#: taking the whole shard with it (seen repeatedly on xmsvtk's runner).
#: ``-noreset`` is the other half of the fix: an X server tears down and
#: regenerates whenever its *last* client disconnects, so both a readiness
#: probe and a test that closes the final render window would otherwise leave
#: the next connection to land inside a reset window and fail the same way.
_XVFB_ARGS = ("-screen", "0", "1280x1024x24", "-noreset", "-nolisten", "tcp")

#: Seconds to wait for a shard's Xvfb to answer the X11 handshake before the
#: shard is failed with the server's own log. Generous because N servers
#: initialize at once against one CPU quota.
_XVFB_START_TIMEOUT = 60

#: Where X servers put their unix sockets.
_X_SOCKET_DIR = "/tmp/.X11-unix"


def _ensure_x_socket_dir() -> None:
    """Create the X socket directory before the server storm needs it.

    Concurrent cold-starting Xvfbs race each other creating it in a fresh
    container: the loser's mkdir fails with EEXIST, its unix listener is
    never created, and its display never answers (seen on xmsvtk's runner --
    one Xvfb per container never hit this because the first server created
    the directory alone). One mkdir up front removes the race. The chmod
    matches the sticky world-writable mode X expects but does not insist:
    on a host with a real X server the directory already exists, owned by
    root, already correct, and not ours to touch.
    """
    os.makedirs(_X_SOCKET_DIR, exist_ok=True)
    try:
        os.chmod(_X_SOCKET_DIR, 0o1777)
    except OSError:
        pass


def _x_server_answers(display_number: int) -> bool:
    """One X11 connection-setup handshake against *display_number*.

    Sends the 12-byte setup request (little-endian byte order, protocol 11.0,
    no authorization) and requires the server's success reply. Merely seeing
    the socket file is not enough -- Xvfb binds it early in startup, before it
    can answer a client, and a connection accepted that early still fails.
    The Linux abstract namespace is tried first because that is Xlib's own
    first choice, so a server that lost its filesystem listener but holds the
    abstract one still serves the runner and counts as ready.
    """
    socket_path = f"{_X_SOCKET_DIR}/X{display_number}"
    for address in (f"\0{socket_path}", socket_path):
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(2)
        try:
            probe.connect(address)
            probe.sendall(struct.pack("<BxHHHHxx", ord("l"), 11, 0, 0, 0))
            if probe.recv(1) == b"\x01":
                return True
        except OSError:
            pass
        finally:
            probe.close()
    return False


def _start_xvfb(display_number: int, log_path: Path):
    """Start one Xvfb and wait until it answers the handshake.

    Returns:
        ``(process, None)`` once the server answers, or ``(process, error)``
        with the server's captured output when it exited early or never
        became ready -- the shard fails loudly instead of running against a
        display that cannot serve it.
    """
    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            ["Xvfb", f":{display_number}", *_XVFB_ARGS],
            stdout=log, stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + _XVFB_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process, (
                f"Xvfb :{display_number} exited with code {process.returncode} "
                f"before serving: {log_path.read_text(errors='replace').strip()}"
            )
        if _x_server_answers(display_number):
            return process, None
        time.sleep(0.2)
    _stop_xvfb(process)
    return process, (
        f"Xvfb :{display_number} did not answer within {_XVFB_START_TIMEOUT}s: "
        f"{log_path.read_text(errors='replace').strip()}"
    )


def _stop_xvfb(process) -> None:
    """Terminate a shard's Xvfb, escalating to kill if it lingers."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


#: Counter attributes gtest writes on ``<testsuites>`` and ``<testsuite>``.
#: Summed across shards on merge. ``skipped`` is absent in older gtest, hence
#: the default of 0 rather than a KeyError.
_COUNTER_ATTRIBUTES = ("tests", "failures", "disabled", "errors", "skipped")


class ShardResult(NamedTuple):
    """What one shard did.

    Attributes:
        index: Zero-based shard number, matching ``GTEST_SHARD_INDEX``.
        returncode: The runner's exit status; None when it never ran to
            completion (timeout, or the process could not be started).
        duration: Wall-clock seconds the shard took.
        output: The shard's combined stdout and stderr, held rather than
            streamed so it can be replayed without interleaving.
        xml_path: Where this shard was told to write its gtest XML. The file
            may not exist if the shard died first.
        error: A one-line description when the shard did not complete
            normally, else None.
    """

    index: int
    returncode: Optional[int]
    duration: float
    output: str
    xml_path: Path
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        """Whether this shard reported success.

        Both halves, so the property states the invariant instead of relying on
        it: every constructor that sets *error* also leaves *returncode* None,
        and a future one that forgets would otherwise report a failed shard as
        passed.
        """
        return self.returncode == 0 and self.error is None


class MergeTotals(NamedTuple):
    """Summed counters from every shard's gtest XML."""

    tests: int
    failures: int
    errors: int
    disabled: int
    skipped: int


def resolve_shard_count(value) -> int:
    """Resolve ``--shards`` to a concrete count.

    ``auto`` is half the visible CPUs, floor 2 -- the same rule ``build.py``
    applies to ``--test-shards auto``. Half rather than all because a gtest
    case is rarely single-threaded end to end, and oversubscribing a shared CI
    runner makes every shard slower than it makes the set faster.

    Args:
        value: The flag's value: ``"auto"`` or an integer-ish.

    Returns:
        The number of shards, at least 1.

    Raises:
        ValueError: When *value* is neither ``auto`` nor an integer.
    """
    if str(value).strip().lower() == "auto":
        return max((os.cpu_count() or 4) // 2, 2)
    count = int(value)
    if count < 1:
        raise ValueError(f"--shards must be at least 1, got {count}")
    return count


def find_artifact_dir(artifacts_dir, label: Optional[str] = None) -> Path:
    """Locate the staged ``testing=True`` artifacts.

    Args:
        artifacts_dir: The directory ``build.py --artifacts-dir`` wrote into.
        label: An exact configuration label (``"Release-testing"``). When None
            the single ``*-testing`` subdirectory is used.

    Returns:
        Path to the artifact directory.

    Raises:
        FileNotFoundError: When the directory, or a matching subdirectory,
            does not exist. The message lists what *was* there: the failure
            this replaces was a bare ``ls | head -1`` producing an empty
            string, and then a "runner not found at /runner" naming a path
            nobody wrote.
        ValueError: When several ``*-testing`` directories match, no label says
            which, and none of them is a build type :data:`_LABEL_PREFERENCE`
            can fall back to. Picking one out of an unrecognized set would
            silently test a configuration the operator did not name.
    """
    root = Path(artifacts_dir)
    if label:
        candidate = root / label
        if not candidate.is_dir():
            raise FileNotFoundError(
                f"No artifact directory {candidate}. "
                f"{_describe_contents(root)}"
            )
        return candidate

    if not root.is_dir():
        raise FileNotFoundError(
            f"No artifacts directory {root}. The build job stages it with "
            f"`build.py --artifacts-dir {root}`; check that it ran and that "
            f"its artifacts reached this job."
        )
    matches = sorted(
        child for child in root.iterdir()
        if child.is_dir() and child.name.endswith(_TESTING_LABEL_SUFFIX)
    )
    if not matches:
        raise FileNotFoundError(
            f"No *{_TESTING_LABEL_SUFFIX} directory under {root}. "
            f"{_describe_contents(root)}"
        )
    if len(matches) > 1:
        # The default matrix stages a testing copy of every build type, so
        # arriving here is normal rather than exceptional. Fall back in a fixed,
        # documented order and say which one won -- the objection to
        # `ls | head -1` was that it chose silently, not that it chose.
        #
        # This is now an interactive convenience only: the generated GitLab
        # pipeline emits one job per staged testing configuration and each names
        # its own --label, so no CI run reaches this branch. It used to, and
        # that is how a matrix building both build types tested only Debug for
        # as long as it did -- the warning below went to a job log nobody read.
        # Keep the fallback for a developer running the tool by hand after a
        # local build.py; do not let CI depend on it again.
        by_name = {match.name: match for match in matches}
        for preferred in _LABEL_PREFERENCE:
            if preferred in by_name:
                LOGGER.warning(
                    "Several testing configurations are staged under %s: %s. "
                    "Testing %s; pass --label to test another.",
                    root, ", ".join(sorted(by_name)), preferred,
                )
                return by_name[preferred]
        raise ValueError(
            f"Several testing configurations are staged under {root}: "
            f"{', '.join(m.name for m in matches)}, and none of them is one of "
            f"{', '.join(_LABEL_PREFERENCE)} to fall back to. "
            f"Name one with --label."
        )
    return matches[0]


def _describe_contents(root: Path) -> str:
    """One sentence naming what is in *root*, for a not-found message."""
    if not root.is_dir():
        return f"{root} does not exist."
    names = sorted(child.name for child in root.iterdir())
    if not names:
        return f"{root} is empty."
    return f"{root} holds: {', '.join(names)}."


def find_runner(artifact_dir: Path) -> Path:
    """Return the test runner inside *artifact_dir*, made executable.

    The executable bit does not survive a GitLab artifact round trip, so it is
    re-applied here rather than assumed.

    Raises:
        FileNotFoundError: When no runner was staged. That is a real failure,
            not a skip: with sharding on, the recipe sets ``XMS_SKIP_CXX_TESTS``
            and these shards are the only thing that runs the C++ suite, so a
            missing runner means no test ran at all.
    """
    runner_name = "runner.exe" if sys.platform == "win32" else "runner"
    runner = artifact_dir / runner_name
    if not runner.is_file():
        raise FileNotFoundError(
            f"No {runner_name} in {artifact_dir}. With sharding enabled the "
            f"recipe skips cmake.test(), so this binary is the only thing that "
            f"runs the C++ tests -- an absent one means the suite did not run. "
            f"{_describe_contents(artifact_dir)}"
        )
    os.chmod(runner, 0o755)
    return runner


def link_test_files(artifact_dir: Path) -> Optional[Path]:
    """Put ``test_files/`` back at the absolute path the runner was built with.

    The tests resolve their data through a path baked in at compile time (the
    recipe records it in ``test_metadata.json``), which is a conan build folder
    that does not exist in the job that downloads the artifacts. A symlink from
    that path to the staged copy is what makes the runner find its data.

    Args:
        artifact_dir: The staged artifact directory.

    Returns:
        The path that now resolves to the staged ``test_files``, or None when
        there was nothing to link -- a library with no test data has no
        ``test_files/`` to stage, which is not an error.
    """
    metadata_path = artifact_dir / "test_metadata.json"
    staged = artifact_dir / "test_files"
    if not metadata_path.is_file() or not staged.is_dir():
        return None

    try:
        test_path = json.loads(metadata_path.read_text(encoding="utf-8"))["test_path"]
    except (ValueError, KeyError, TypeError) as exc:
        # TypeError covers a metadata file holding a JSON array or scalar:
        # subscripting that raises before KeyError can, and main() only handles
        # FileNotFoundError and ValueError, so it would surface as a traceback.
        LOGGER.warning(
            "Could not read test_path from %s (%s); tests that load data from "
            "test_files/ will fail to find it.", metadata_path, exc,
        )
        return None

    # The recipe writes the path with a trailing separator. Path.parent of a
    # trailing-slash string is the directory *itself*, not its parent, so the
    # separator has to come off before the parent is taken or the link lands
    # one level too deep.
    target = Path(str(test_path).rstrip("/\\"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    try:
        target.symlink_to(staged.resolve(), target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        # Windows needs a privilege for symlinks that a CI account may not
        # hold. A copy costs disk but is otherwise equivalent, and losing the
        # test data is not an acceptable alternative. Name the cause anyway:
        # the fallback also covers a full disk and a read-only parent, which
        # copytree is about to fail on for a reason worth having in the log.
        LOGGER.info("Could not symlink %s (%s); copying instead.", target, exc)
        shutil.copytree(staged, target)
    return target


def _run_one_shard(runner: Path, index: int, total: int, output_dir: Path,
                   xvfb: bool, timeout: int, runner_args) -> ShardResult:
    """Run a single shard to completion, capturing everything it prints."""
    xml_path = output_dir / f"TEST-shard-{index}.xml"
    env = os.environ.copy()
    env["GTEST_TOTAL_SHARDS"] = str(total)
    env["GTEST_SHARD_INDEX"] = str(index)
    # gtest colorizes on a tty and this is a pipe, so ask for it explicitly --
    # the captured buffer is replayed into the CI log, which does render color.
    env.setdefault("GTEST_COLOR", "yes")

    server = None
    started = time.monotonic()
    try:
        if xvfb:
            display_number = _XVFB_BASE_DISPLAY + index
            server, error = _start_xvfb(display_number,
                                        output_dir / f"xvfb-{index}.log")
            if error:
                return ShardResult(
                    index=index, returncode=None,
                    duration=time.monotonic() - started, output="",
                    xml_path=xml_path, error=error,
                )
            env["DISPLAY"] = f":{display_number}"
        command = [str(runner), f"--gtest_output=xml:{xml_path}", *runner_args]
        try:
            completed = subprocess.run(
                command, env=env, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return ShardResult(
                index=index, returncode=None,
                duration=time.monotonic() - started, output=output,
                xml_path=xml_path, error=f"timed out after {timeout}s",
            )
        except OSError as exc:
            return ShardResult(
                index=index, returncode=None,
                duration=time.monotonic() - started, output="",
                xml_path=xml_path, error=f"could not start: {exc}",
            )
        return ShardResult(
            index=index, returncode=completed.returncode,
            duration=time.monotonic() - started, output=completed.stdout or "",
            xml_path=xml_path,
        )
    finally:
        if server is not None:
            _stop_xvfb(server)


def _print_shard_output(result: ShardResult, total: int, stream=None) -> None:
    """Replay one shard's captured output under a banner.

    Whole buffers, one shard at a time: N runners writing to the same log
    interleave per line, which splits a failing test's assertion away from the
    test name that identifies it.
    """
    stream = stream or sys.stdout
    status = "PASSED" if result.passed else "FAILED"
    if result.error:
        status = f"FAILED ({result.error})"
    rule = "=" * 72
    print(rule, file=stream)
    print(f"Shard {result.index + 1}/{total} -- {status} in {result.duration:.1f}s",
          file=stream)
    print(rule, file=stream)
    if result.output:
        print(result.output.rstrip("\n"), file=stream)
    else:
        print("(no output)", file=stream)
    print("", file=stream)


def run_shards(runner: Path, shards: int, output_dir: Path, *, xvfb: bool = False,
               timeout: int = DEFAULT_SHARD_TIMEOUT,
               runner_args: Sequence[str] = ()) -> list[ShardResult]:
    """Run *shards* shards of *runner* concurrently, in this container.

    Args:
        runner: The gtest binary.
        shards: How many shards to split the suite into.
        output_dir: Where each shard's XML report is written.
        xvfb: Give each shard its own Xvfb server, started and readiness-
            checked by this module, on its own display.
        timeout: Seconds a single shard may take.
        runner_args: Extra arguments passed through to every shard.

    Returns:
        One :class:`ShardResult` per shard, in shard-index order. Each shard's
        output is printed as it finishes, so a long run still reports progress
        while staying un-interleaved.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("TEST-shard-*.xml"):
        # A shard that dies before writing its report would otherwise be merged
        # from the previous run's file and look like it passed.
        stale.unlink()

    if xvfb:
        _ensure_x_socket_dir()

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=shards) as executor:
        futures = [
            executor.submit(_run_one_shard, runner, index, shards, output_dir,
                            xvfb, timeout, list(runner_args))
            for index in range(shards)
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            _print_shard_output(result, shards)
            results.append(result)
    return sorted(results, key=lambda r: r.index)


def _sum_counters(into: ElementTree.Element, source: ElementTree.Element) -> None:
    """Add *source*'s counter attributes into *into*."""
    for name in _COUNTER_ATTRIBUTES:
        total = int(float(into.get(name, 0))) + int(float(source.get(name, 0)))
        into.set(name, str(total))
    into.set("time", f'{float(into.get("time", 0)) + float(source.get("time", 0)):.3f}')


def _synthetic_failure_suite(result: ShardResult) -> ElementTree.Element:
    """Build a one-case ``<testsuite>`` standing in for a shard that wrote no XML.

    Without this the merged report is silently short: a shard that segfaults or
    times out before gtest serializes its results contributes nothing, so the
    JUnit view shows every case green next to a red job. An explicit error case
    makes the gap visible where the reader is already looking.
    """
    reason = result.error or f"exited {result.returncode} without writing a report"
    suite = ElementTree.Element("testsuite", {
        "name": f"shard-{result.index}",
        "tests": "1", "failures": "0", "disabled": "0", "errors": "1",
        "skipped": "0", "time": f"{result.duration:.3f}",
    })
    case = ElementTree.SubElement(suite, "testcase", {
        "name": "shard_completed",
        "classname": f"shard-{result.index}",
        "status": "run", "result": "completed",
        "time": f"{result.duration:.3f}",
    })
    error = ElementTree.SubElement(case, "error", {
        "message": f"Shard {result.index} produced no test report: {reason}",
        "type": "ShardIncomplete",
    })
    error.text = result.output[-4000:] if result.output else reason
    return suite


def merge_reports(results, output_path: Path, elapsed: float) -> MergeTotals:
    """Merge every shard's gtest XML into one JUnit file.

    gtest shards at the *case* level, so one suite is normally split across
    several shards. Suites are therefore merged by name -- cases appended,
    counters summed -- rather than concatenated, which would show the reader
    N partial copies of every suite.

    Args:
        results: The :class:`ShardResult` list from :func:`run_shards`.
        output_path: Where the merged report is written.
        elapsed: Wall-clock seconds the whole sharded run took. Written as the
            root ``time`` because summing the shards' times would report the
            work done, not the time the job spent -- and the point of sharding
            is that those two numbers differ.

    Returns:
        The summed counters.
    """
    merged_suites = {}
    order = []
    for result in results:
        root = None
        if result.xml_path.is_file():
            try:
                root = ElementTree.parse(result.xml_path).getroot()
            except ElementTree.ParseError as exc:
                LOGGER.warning("Shard %s wrote an unparseable report (%s); "
                               "recording it as an error.", result.index, exc)
                root = None
        if root is None:
            suite = _synthetic_failure_suite(result)
            order.append(suite.get("name"))
            merged_suites[suite.get("name")] = suite
            continue
        for suite in root.findall("testsuite"):
            name = suite.get("name", "")
            existing = merged_suites.get(name)
            if existing is None:
                merged_suites[name] = copy.deepcopy(suite)
                order.append(name)
                continue
            _sum_counters(existing, suite)
            for case in suite.findall("testcase"):
                existing.append(copy.deepcopy(case))

    merged_root = ElementTree.Element("testsuites", {"name": "AllTests"})
    for name in _COUNTER_ATTRIBUTES:
        merged_root.set(name, "0")
    for name in order:
        suite = merged_suites[name]
        _sum_counters(merged_root, suite)
        merged_root.append(suite)
    merged_root.set("time", f"{elapsed:.3f}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree.ElementTree(merged_root).write(
        output_path, encoding="utf-8", xml_declaration=True,
    )
    # By name, not position: _COUNTER_ATTRIBUTES is ordered to match gtest's
    # own attribute order and MergeTotals to read well, and the two disagree
    # about where "errors" sits. Splatting positionally swapped it with
    # "disabled" -- both plausible small integers, so nothing looked wrong.
    return MergeTotals(**{name: int(merged_root.get(name, 0))
                          for name in _COUNTER_ATTRIBUTES})


def _print_summary(results, totals: MergeTotals, elapsed: float,
                   output_path: Path, stream=None) -> None:
    """Print the per-shard table and the merged totals."""
    stream = stream or sys.stdout
    print("=" * 72, file=stream)
    print("Shard summary", file=stream)
    print("=" * 72, file=stream)
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        detail = f" ({result.error})" if result.error else ""
        print(f"  shard {result.index + 1}/{len(results)}: {status} "
              f"in {result.duration:.1f}s{detail}", file=stream)
    print(f"\n  {totals.tests} test(s): {totals.failures} failed, "
          f"{totals.errors} errored, {totals.disabled} disabled, "
          f"{totals.skipped} skipped", file=stream)
    print(f"  wall clock {elapsed:.1f}s across {len(results)} shard(s)", file=stream)
    print(f"  merged report: {output_path}\n", file=stream)


def run(artifacts_dir, shards: int, *, label=None, output=None, xvfb=False,
        timeout=DEFAULT_SHARD_TIMEOUT, runner_args=()) -> int:
    """Locate the runner, shard it, and merge the reports.

    Returns:
        A process exit code: 0 when every shard passed and the merged report
        records no failure or error, 1 otherwise.
    """
    artifact_dir = find_artifact_dir(artifacts_dir, label)
    runner = find_runner(artifact_dir)
    linked = link_test_files(artifact_dir)
    if linked:
        LOGGER.info("test_files/ linked at %s", linked)

    output_path = Path(output) if output else Path(DEFAULT_REPORT_NAME)
    LOGGER.info("Running %s shard(s) of %s", shards, runner)

    started = time.monotonic()
    results = run_shards(runner, shards, artifact_dir, xvfb=xvfb,
                         timeout=timeout, runner_args=runner_args)
    elapsed = time.monotonic() - started

    totals = merge_reports(results, output_path, elapsed)
    _print_summary(results, totals, elapsed, output_path)

    # Both conditions, not just the exit codes: a shard can exit 0 having run
    # nothing, and a failure recorded in the XML that no shard's status
    # reflected would otherwise ship green.
    failed_shards = [r for r in results if not r.passed]
    if failed_shards:
        LOGGER.error("%s of %s shard(s) failed: %s", len(failed_shards), len(results),
                     ", ".join(str(r.index) for r in failed_shards))
        return 1
    if totals.failures or totals.errors:
        LOGGER.error("Every shard exited 0, but the merged report records "
                     "%s failure(s) and %s error(s).", totals.failures, totals.errors)
        return 1
    if not totals.tests:
        # The comment above is only true with this branch present. A runner
        # given a filter that matches nothing exits 0 and writes a well-formed
        # report holding no cases, which passes both checks above and ships an
        # empty, entirely green JUnit tab as a successful test run.
        LOGGER.error("Every shard exited 0 but no test ran: the merged report "
                     "records 0 tests. With sharding on, the recipe skips "
                     "cmake.test(), so this is an empty run, not a clean one.")
        return 1
    return 0


def main():
    """Entry point for ``xmsconan test-shards`` (and ``xmsconan_test_shards``)."""
    parser = argparse.ArgumentParser(
        description="Run a staged gtest runner as N shards in this container, "
                    "replaying each shard's output un-interleaved and merging "
                    "the per-shard reports into one JUnit file.",
    )
    parser.add_argument(
        "--artifacts-dir", default=DEFAULT_ARTIFACTS_DIR,
        help=f"Directory the build job staged test artifacts into "
             f"(default: {DEFAULT_ARTIFACTS_DIR}).",
    )
    parser.add_argument(
        "--label", default=None,
        help="Configuration label to test, e.g. 'Release-testing'. Only needed "
             "when several testing configurations are staged.",
    )
    parser.add_argument(
        "--shards", default="auto",
        help="Number of shards, or 'auto' for half the visible CPUs "
             "(minimum 2). Default: auto.",
    )
    parser.add_argument(
        "--output", default=DEFAULT_REPORT_NAME,
        help=f"Path of the merged JUnit report (default: {DEFAULT_REPORT_NAME}).",
    )
    parser.add_argument(
        "--xvfb", action="store_true",
        help="Give each shard its own readiness-checked Xvfb display. For "
             "libraries that link X11 or VTK.",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_SHARD_TIMEOUT,
        help=f"Seconds a single shard may run (default: {DEFAULT_SHARD_TIMEOUT}).",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase output verbosity.",
    )
    parser.add_argument(
        "runner_args", nargs="*",
        help="Extra arguments passed through to every shard, e.g. "
             "--gtest_filter=Foo*. Put them after a bare --.",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s", force=True,
    )

    try:
        shards = resolve_shard_count(args.shards)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        exit_code = run(
            args.artifacts_dir, shards, label=args.label, output=args.output,
            xvfb=args.xvfb, timeout=args.timeout, runner_args=args.runner_args,
        )
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
