"""What a generator would write, decided before anything is written.

Each generator renders into a mapping of output path to content -- the same
files a real run writes -- and ``--check`` compares that plan with the files
already on disk, so it cannot pass a file a real run would change.

``--check`` exists because ``CLAUDE.md`` tells every consumer to regenerate
after any ``build.toml`` edit and nothing enforces it. Generated build files
are gitignored in the consumers, so a stale one is not a diff anyone sees --
it is a working copy quietly building something the repository no longer
describes. A CI job that runs ``xmsconan gen --check`` turns that into a red
pipeline.
"""
from collections.abc import Iterable, Mapping
import difflib
import logging
from pathlib import Path
import sys
from typing import TextIO

LOGGER = logging.getLogger(__name__)


def write_text_lf(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* with LF line endings on every platform.

    Creates the parent directory. Generated files are compared byte for byte
    by ``--check`` and by the golden tests, so the line endings have to be
    the same on the Windows runner that writes them and the Linux one that
    reads them back.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="\n") as handle:
        handle.write(content.replace("\r\n", "\n"))


def write_plan(plan: Mapping[str | Path, str], label: str, encoding: str = "utf-8") -> list[Path]:
    """Write every file in *plan*, logging each as *label*.

    Args:
        plan: Mapping of output path to content.
        label: What these files are, for the log line ("CI file", "profile").
        encoding: Passed through to :func:`write_text_lf`.

    Returns:
        The paths written, in plan order.
    """
    written = []
    for path, content in plan.items():
        write_text_lf(path, content, encoding)
        LOGGER.info("Generated %s: %s", label, path)
        written.append(Path(path))
    return written


def describe_plan(plan: Mapping[str | Path, str], label: str) -> None:
    """Log what a real run would write, without writing it."""
    for path in plan:
        LOGGER.info("[DRY-RUN] Would write %s: %s", label, path)


def report_drift(
    plan: Mapping[str | Path, str],
    stale: Iterable[str | Path] = (),
    stream: TextIO | None = None,
    encoding: str = "utf-8",
) -> int:
    """Print a unified diff for everything on disk that disagrees with *plan*.

    Args:
        plan: Mapping of output path to the content a real run would write.
        stale: Paths a real run would delete. Reported, not diffed -- their
            content is beside the point, their existence is the finding.
        stream: Where the report goes. Defaults to stdout.
        encoding: Used to read the files being compared against. A file
            that does not decode, or cannot be read at all, is reported as
            drift too: whatever it is, it is not the generated file.

    Returns:
        The number of files that differ, are missing, are stale, or cannot
        be compared. Zero means a real run would leave the tree exactly as
        it is.

    Both sides are compared with LF line endings, because neither side is
    reliably LF on Windows: the working copy may have been checked out with
    CRLF, and a template checked out the same way renders CRLF into the
    plan. Neither is drift -- the file a real run writes is byte for byte
    the committed one -- and reporting them as drift would fail ``--check``
    on every Windows runner for a reason no regeneration could fix.
    """
    stream = sys.stdout if stream is None else stream
    differences = 0

    for path, content in plan.items():
        path = Path(path)
        expected = content.replace("\r\n", "\n")
        if not path.exists():
            print(f"--- {path} (missing)\n+++ {path} (would be generated)", file=stream)
            differences += 1
            continue
        # Text mode already translates the file's line endings to \n, so a
        # CRLF working copy compares equal without normalizing it here.
        try:
            actual = path.read_text(encoding=encoding)
        except (OSError, UnicodeDecodeError) as reason:
            # A directory at the path, a file this user cannot read, bytes
            # that are not the encoding: whatever is there, it is not the
            # generated file, and letting the error escape would end the
            # report at this file with the same exit code drift produces.
            problem = f"not {encoding}" if isinstance(reason, UnicodeDecodeError) else f"unreadable: {reason.strerror}"
            print(f"--- {path} (on disk, {problem})\n+++ {path} (would be generated)", file=stream)
            differences += 1
            continue
        if actual == expected:
            continue
        diff = difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=f"{path} (on disk)",
            tofile=f"{path} (would be generated)",
            lineterm="",
        )
        print("\n".join(diff), file=stream)
        differences += 1

    for path in stale:
        print(f"--- {path} (on disk)\n+++ {path} (would be removed)", file=stream)
        differences += 1

    return differences


def check_plan(
    plan: Mapping[str | Path, str],
    stale: Iterable[str | Path] = (),
    stream: TextIO | None = None,
    encoding: str = "utf-8",
) -> int:
    """Report drift and return the process exit code: 1 if anything differs.

    The message on success names the count, because "no output" and "the
    tool did not run" look identical in a CI log.
    """
    stream = sys.stdout if stream is None else stream
    differences = report_drift(plan, stale=stale, stream=stream, encoding=encoding)
    if differences:
        print(
            f"\n{differences} generated file(s) are out of date. "
            f"Re-run the generator without --check and commit the result.",
            file=stream,
        )
        return 1
    print(f"{len(plan)} generated file(s) are up to date.", file=stream)
    return 0
