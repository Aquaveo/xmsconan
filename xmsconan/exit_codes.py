"""The exit-code vocabulary every ``xmsconan`` console script shares.

One number means one thing across commands, because the generated CI keys
off the numbers: GitLab's ``allow_failure: exit_codes:`` forgives a coverage
gate miss (3) and nothing else, and a wrapper script or ``&&`` chain reads
any nonzero code as "stop". A command that reused 3 for a different event
would be forgiven by a job that meant to forgive only the gate.

The values are spaced around what argparse already owns: argparse exits 2
on a usage error, so a mistyped invocation must never read as a gate miss
or as "nothing was built".
"""

#: The command did what it was asked.
EXIT_OK = 0

#: The tool failed: an unhandled exception, a child process that did not
#: run, a file that could not be written. Everything that is neither a
#: usage error nor one of the named outcomes below.
EXIT_ERROR = 1

#: A bad request or a machine that cannot honour it. argparse produces this
#: for a bad flag; ``xmsconan vs2019`` produces it for a ``--root`` that does
#: not exist or a preflight check that failed.
EXIT_USAGE = 2

#: A gate the run was asked to enforce did not clear -- a coverage layer
#: below its threshold -- while the tool itself worked and its reports were
#: produced. The one code the generated CI forgives, and only where the
#: tests already gate the pipeline from a job of their own (USAGE §11.6).
EXIT_GATE_FAILED = 3

#: The run completed but produced nothing: every library ``xmsconan vs2019
#: build`` selected was skipped. Not success -- a typo in ``--root`` used to
#: print a table of skips and exit 0 -- and not a gate miss either, so it
#: does not share the number CI forgives.
EXIT_NOTHING_BUILT = 4
