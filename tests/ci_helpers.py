"""Shared helpers for the CI-generator tests.

Kept out of ``tests/utils.py``, which exists for environment patching -- these
are about generating and inspecting CI files, and both CI test modules need
them. Previously each module carried its own copy of the build.toml writer and
its own idea of which top-level keys are not jobs.
"""
from pathlib import Path
import re

import yaml

from xmsconan._tomllib import loads
from xmsconan.cli import COMMANDS
from xmsconan.job_tools.cli import build_parser

#: Top-level keys in a generated ``.gitlab-ci.yml`` that are not jobs, for
#: checks that inspect job *shape*.
#:
#: ``pages`` is listed because it has no ``script:`` and would fail those
#: checks -- but it IS a job and does declare ``stage: Pages``, so stage
#: validation must NOT use this set or it silently stops guarding the one
#: thing it exists to guard.
NON_JOB_SHAPE_KEYS = frozenset({"stages", "variables", "pages"})


def _toml_value(value):
    """Render a Python value as TOML.

    ``str(value).lower()`` is wrong for anything but a bool -- it would emit a
    bare, unquoted string for ``docker_image``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return f'"{value}"'


def required_ci_keys(ci_type=None):
    """The ``[ci]`` keys every build.toml must set, at the values their old defaults gave.

    ``windows_wheel_repair`` used to follow ``ci_type`` (off on GitLab, on
    everywhere else) and ``python_versions`` used to be ``["3.13"]``. Fixtures
    that are not about either key keep those values so they test what they did.
    """
    return {"python_versions": ["3.13"], "windows_wheel_repair": ci_type != "gitlab"}


#: ``required_ci_keys`` as TOML lines, for fixtures that write a raw body.
REQUIRED_CI_LINES = '[ci]\npython_versions = ["3.13"]\nwindows_wheel_repair = true\n'


def write_build_toml(tmp_path, ci_type, library_name="xmssnap", description="Snap",
                     coverage_table=None, matrix_table=None, **ci_flags):
    """Write a minimal build.toml with the given ci_type, [ci], [coverage] and [matrix] tables.

    The two required ``[ci]`` keys are filled in unless *ci_flags* names them,
    at the values their old defaults produced, so a test about some other key
    reads the same as before they became required.
    """
    lines = [
        f'library_name = "{library_name}"',
        f'description = "{description}"',
        f'ci_type = "{ci_type}"',
    ]
    ci_flags = {**required_ci_keys(ci_type), **ci_flags}
    lines += ["", "[ci]"] + [f"{key} = {_toml_value(value)}" for key, value in ci_flags.items()]
    if coverage_table:
        lines += ["", "[coverage]"] + [
            f"{key} = {_toml_value(value)}" for key, value in coverage_table.items()
        ]
    if matrix_table:
        lines += ["", "[matrix]"] + [
            f"{key} = {_toml_value(value)}" for key, value in matrix_table.items()
        ]
    toml_file = tmp_path / "build.toml"
    toml_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml_file


def write_gitlab_toml(tmp_path, **ci_flags):
    """Write a minimal GitLab build.toml."""
    return write_build_toml(tmp_path, "gitlab", **ci_flags)


#: The [matrix] table that opts a repository into the concurrent build stage.
#: Spelled out at each call site rather than defaulted, because whether a
#: pipeline has one build job per configuration or one job looping all of them
#: is exactly what these tests are about.
WHEEL_ONLY = {"wheel_only": True}

#: A legacy ``xmsconan_*`` console script's name, as a generated command line
#: would spell it. The scripts stay installed as aliases; what this guards is a
#: generated job calling one instead of ``xmsconan <cmd>``.
LEGACY_SCRIPT = re.compile(r"\bxmsconan_\w+")

#: A call through the dispatcher, ``xmsconan <cmd> [<word>]``, in one line of a
#: generated file: ``xmsconan`` at the start of the line, or after whitespace or
#: a path separator (``/opt/python/cp313-cp313/bin/xmsconan job package``) --
#: the shapes the templates render -- or after a Windows path's backslash or a
#: quote, so a call reshaped that way is still checked. A call inside ``$(...)``
#: or after a bare ``;`` would need the class widened. The groups are the
#: subcommand and the word after it, if any; either may be quoted, and each runs
#: to whitespace or a quote, so a capitalized, misspelled or quoted word is
#: captured rather than passed over. ``xmsconan[ci]`` and ``xmsconan_ci`` never
#: match.
DISPATCHER_CALL = re.compile(r"""(?:^|[\s/\\"'])xmsconan\s+["']?([^\s"']+)["']?(?:\s+["']?([^\s"']+))?""")


def uncommented_lines(text):
    """Each line of a rendered CI file that is neither blank nor a ``#`` comment, stripped.

    The scans that ask what a pipeline runs read these. Comments are prose
    about the pipeline, and prose names commands too: the header still names
    ``xmsconan_ci`` as the generator, and the GitLab coverage job's comment
    says what ``xmsconan coverage`` exports.
    """
    return [stripped for line in text.splitlines() if (stripped := line.strip()) and not stripped.startswith("#")]


def dispatched_calls(text):
    """The ``(subcommand, next word)`` of each ``xmsconan`` call a rendered CI file makes, in order.

    The next word is None for a call that ends at its subcommand.
    """
    return [match.groups() for line in uncommented_lines(text) for match in DISPATCHER_CALL.finditer(line)]


def dispatched_commands(text):
    """The subcommand of each ``xmsconan <cmd>`` call a rendered CI file makes, in order."""
    return [command for command, _ in dispatched_calls(text)]


def job_kinds():
    """The kinds ``xmsconan job`` takes, read from its own parser rather than listed again."""
    (kinds,) = [action.choices for action in build_parser()._actions if action.dest == "kind"]
    return set(kinds)


def unknown_calls(text):
    """Each ``xmsconan`` call in a rendered CI file whose subcommand or job kind does not exist.

    The dispatcher exits 1 on a subcommand missing from its table, and
    ``xmsconan job`` exits 2 on a kind its parser lacks -- both only at run
    time, after a template that misspelled one has rendered, passed, and been
    copied into the golden files by ``--update-golden``. A word starting with
    ``-`` in the subcommand's place is reported like any other, since the
    dispatcher reads ``xmsconan -v coverage`` as subcommand ``-v``; that
    includes its own ``--version`` and ``--help``, which no generated line
    calls.
    """
    kinds = job_kinds()
    unknown = []
    for command, word in dispatched_calls(text):
        if command not in COMMANDS:
            unknown.append(f"xmsconan {command}")
        elif command == "job" and word not in kinds:
            unknown.append(f"xmsconan job {word}")
    return unknown


def write_github_toml(tmp_path, **ci_flags):
    """Write a minimal GitHub build.toml."""
    return write_build_toml(tmp_path, "github", library_name="xmscore", description="Core", **ci_flags)


def workflow_document(path):
    """The parsed document of an already rendered CI file."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def steps_running(job, command):
    """The steps of one job whose ``run:`` line invokes *command*."""
    return [step for step in job["steps"] if command in step.get("run", "")]


def requirement_names(line):
    """The bare distribution names on one ``pip install`` line, case-folded.

    A substring match over the whole line is wrong in one direction: it also
    matches ``tomli`` and ``tomlkit``, which are different distributions, and
    would match a ``build.toml`` path if an install line ever carried one. A
    bare ``line.split()`` match is wrong in the other -- it misses every
    decorated form a re-added requirement would arrive in: ``toml==0.10``,
    ``"toml"``, ``toml>=0.10``, ``toml@https://...``. Names are folded because
    distribution names are matched case-insensitively.
    """
    names = set()
    for token in line.split():
        names.add(re.split(r"[=<>!~\[;@]", token.strip("\"'"), maxsplit=1)[0].casefold())
    return names


def _project_table():
    """pyproject.toml's ``[project]`` table.

    Read from the file rather than ``importlib.metadata``: an editable
    install's metadata is written at sync time, so an unsynced pyproject.toml
    edit would be tested against the previous requirements.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    return loads(pyproject.read_text(encoding="utf-8"))["project"]


def ci_extra():
    """The ``[ci]`` extra as pyproject.toml declares it: one requirement string per entry."""
    return _project_table()["optional-dependencies"]["ci"]


def runtime_dependencies():
    """The dependencies xmsconan declares: what every ``pip install xmsconan`` brings, one string per entry."""
    return _project_table()["dependencies"]


# --- the pipelines GitLab would create from a generated file ---------------
#
# A model of three GitLab rules, small enough to read: which jobs a branch or
# tag pipeline runs (`only:`/`except:`), what a job waits for (its `needs:`, or
# every earlier stage without them), and which needs fail pipeline creation.

#: The refs a job may be limited to that neither modelled pipeline runs: the
#: default branch's, as Pages is. The branch pipeline modelled is a topic
#: branch's, the stricter of the two for a need.
_DEFAULT_BRANCH_REFS = {"master", "main"}


def job_gate(job):
    """How a GitLab job is gated: "always", "only:<refs>" or "except:<refs>"."""
    if "only" in job:
        return "only:" + ",".join(job["only"])
    if "except" in job:
        return "except:" + ",".join(job["except"])
    return "always"


def _pipelines_running(name, job):
    """Which pipelines run a job, as (topic branch, tag).

    Only the gates the template writes are known. Any other raises rather than
    leaving the job out of both pipelines, which would pass every check on it.
    """
    gate = job_gate(job)
    if gate == "always":
        return True, True
    if gate == "only:tags":
        return False, True
    if gate == "except:tags":
        return True, False
    if isinstance(job.get("only"), list) and set(job["only"]) <= _DEFAULT_BRANCH_REFS:
        return False, False
    raise ValueError(f"{name}: the pipeline model does not know the gate {gate!r}")


def jobs_reaching(parsed, *, tags):
    """The jobs GitLab would put in a tag pipeline, or in a topic branch's."""
    reaching = {}
    for name, job in parsed.items():
        if not isinstance(job, dict) or "script" not in job:
            continue
        branch, tag = _pipelines_running(name, job)
        runs = tag if tags else branch
        if runs:
            reaching[name] = job
    return reaching


def _need_name(need):
    """The job a `needs:` entry names, written as a bare name or as a `job:` mapping."""
    return need["job"] if isinstance(need, dict) else need


def needed_jobs(job):
    """The jobs a job names under `needs:`, optional or not, artifacts or not."""
    for need in job.get("needs") or []:
        yield _need_name(need)


def job_platform(name):
    """The platform a job builds for or publishes, read from its name.

    VS2019 is a platform of its own: a second Windows toolchain with its own
    build and its own deploy to its own remote, and neither Windows pair has
    ever waited on the other.
    """
    if name.endswith(" - macOS"):
        return "macOS"
    if "VS2019" in name:
        return "Windows VS2019"
    if "Windows" in name:
        return "Windows"
    return "Linux"


def waits_on(name, reaching, stages):
    """Every job GitLab holds `name` back for in this pipeline, transitively.

    A job with `needs:` waits on the jobs it names that are in the pipeline --
    an optional need on a job that is not there is no wait -- and on whatever
    they wait on. A job without `needs:` waits on every job in an earlier stage.
    """
    job = reaching[name]
    if "needs" in job:
        direct = [up for up in needed_jobs(job) if up in reaching]
    else:
        rank = stages.index(job["stage"])
        direct = [other for other, upstream in reaching.items()
                  if stages.index(upstream["stage"]) < rank]
    waits = set(direct)
    for up in direct:
        waits |= waits_on(up, reaching, stages)
    return waits


def need_violations(parsed):
    """Each `needs:` GitLab would refuse to create a branch or tag pipeline with.

    An empty `needs:` key parses as null and fails the whole file. Past that it
    is per pipeline: a need that is not `optional: true` must name a job the
    same pipeline runs -- defined somewhere in the file is not enough, since
    most jobs carry a tag gate -- and a job cannot need one from a later stage.
    """
    stages = parsed["stages"]
    problems = [f"{name} has an empty `needs:`" for name, job in parsed.items()
                if isinstance(job, dict) and "needs" in job and job["needs"] is None]
    for tags in (False, True):
        pipeline = "tag" if tags else "branch"
        reaching = jobs_reaching(parsed, tags=tags)
        for name, job in reaching.items():
            for need in job.get("needs") or []:
                if isinstance(need, dict) and need.get("optional"):
                    continue
                needed = _need_name(need)
                if needed not in reaching:
                    problems.append(f"{pipeline}: {name} needs {needed!r}, which it does not run")
                elif stages.index(reaching[needed]["stage"]) > stages.index(job["stage"]):
                    problems.append(f"{pipeline}: {name} needs {needed!r} from a later stage")
    return problems


def _holds_back_a_deploy(job, stages):
    """Whether stage order held a deploy back for `job`: an earlier stage, and a failure that counts."""
    earlier = stages.index(job["stage"]) < stages.index("Deploy")
    return earlier and job.get("allow_failure") is not True


def deploy_wait_violations(parsed):
    """How each deploy in a tag pipeline waits on the wrong jobs.

    A deploy must wait on every job of its own platform that a tag runs in an
    earlier stage -- what stage order held it back for, before `needs:` -- and
    on no other platform's. An `allow_failure: true` job gates nothing, so it
    is not required; one allowed only some exit codes fails on the rest, so it
    is. Computed through the wait GitLab would compute rather than
    by reading each deploy's keys, so a need on a job that itself waits on
    stage order counts as waiting on everything that job does.
    """
    stages = parsed["stages"]
    reaching = jobs_reaching(parsed, tags=True)
    problems = []
    for name, job in reaching.items():
        if job["stage"] != "Deploy":
            continue
        platform = job_platform(name)
        waits = waits_on(name, reaching, stages)
        own = {other for other, upstream in reaching.items()
               if job_platform(other) == platform and _holds_back_a_deploy(upstream, stages)}
        if not waits:
            problems.append(f"{name} waits on nothing, so it could publish before its build")
        foreign = sorted(up for up in waits if job_platform(up) != platform)
        if foreign:
            problems.append(f"{name} waits on another platform's jobs: {foreign}")
        missing = sorted(own - waits)
        if missing:
            problems.append(f"{name} no longer waits on its own platform's {missing}")
    return problems
