"""Rendering and comparison helpers for the golden-file tests.

Kept out of ``tests/ci_helpers.py`` because these do not inspect a rendered
CI file -- they render a whole tree and compare it byte for byte against one
checked in under ``tests/golden/``. The two answer different questions: the
CI helpers ask "does this job hold that", these ask "did anything at all
change".

Two values are pinned rather than read from the environment. ``xmsconan``'s
own version is interpolated into every install line; left live, a release
would rewrite every golden and bury the one template line that actually
changed. A golden that churns on its own is not a review artifact. The build
version reaches no golden today -- ``generate_ci`` requires one, but no
template renders it -- and is pinned so that the day a template does, the
golden shows the template change and not whatever ``git describe`` said.
"""
import difflib
from pathlib import Path
import re

import jinja2

import xmsconan
from xmsconan.generator_tools.ci_file_generator import generate_ci

#: Directory holding one subdirectory per variant.
GOLDEN_DIR = Path(__file__).parent / "golden"

#: The templates the variants render.
CI_TEMPLATE_DIR = Path(xmsconan.__file__).parent / "generator_tools" / "ci_templates"

#: The build.toml each variant is rendered from, stored beside its output so
#: the input and the result move together in one review diff.
INPUT_NAME = "build.toml"

#: Pinned so a release does not rewrite every golden. See the module docstring.
GOLDEN_VERSION = "1.2.3"
GOLDEN_XMSCONAN_VERSION = "2.18.0"

#: The real render, captured before any test installs a spy in its place.
#: Looked up per call it would be the previous variant's spy, and the spies
#: would nest one deeper for every variant rendered in a loop.
_RENDER = jinja2.Template.render


def variants():
    """Every golden variant, discovered from disk.

    Adding a variant is adding a directory with a ``build.toml`` in it, so
    the parametrization cannot fall behind the fixtures on disk.
    """
    return sorted(path.name for path in GOLDEN_DIR.iterdir() if path.is_dir())


def render_variant(variant, tmp_path, monkeypatch):
    """Render one variant's build.toml into *tmp_path*.

    Returns:
        Mapping of POSIX-style relative path to rendered text.
    """
    monkeypatch.setattr(xmsconan, "__version__", GOLDEN_XMSCONAN_VERSION)
    output_dir = tmp_path / "output"
    generate_ci(str(GOLDEN_DIR / variant / INPUT_NAME), GOLDEN_VERSION, str(output_dir))
    return {
        path.relative_to(output_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }


def capture_contexts(variant, tmp_path, monkeypatch):
    """Render one variant, returning the context each template was rendered with.

    A spy on ``jinja2.Template.render`` rather than a second copy of the
    context assembly. ``generate_ci`` builds that dict inline over some 180
    lines of derivation, so anything that rebuilt it here to inspect it
    would be free to disagree with the real one -- and would then report
    coverage of branches the templates never took.
    """
    seen = []

    def recording_render(self, *args, **kwargs):
        seen.append(dict(*args, **kwargs))
        return _RENDER(self, *args, **kwargs)

    monkeypatch.setattr(jinja2.Template, "render", recording_render)
    render_variant(variant, tmp_path, monkeypatch)
    return seen


def gated_context_names(known):
    """Context names from *known* that some template conditional tests.

    The intersection is what makes this mechanical: a condition also names
    loop variables and Jinja builtins, and only the ones that are actually
    context keys are flags a build.toml can turn on.
    """
    names = set()
    for template in sorted(CI_TEMPLATE_DIR.glob("*.jinja")):
        text = template.read_text(encoding="utf-8")
        for condition in re.findall(r"<%-?\s*(?:if|elif)\s+(.*?)%>", text, flags=re.DOTALL):
            names |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", condition))
    return names & set(known)


def golden_files(variant):
    """Mapping of POSIX-style relative path to path, for one variant's checked-in output."""
    root = GOLDEN_DIR / variant
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != INPUT_NAME
    }


def write_golden(variant, rendered):
    """Rewrite one variant's golden files to *rendered*, deleting any that are stale."""
    root = GOLDEN_DIR / variant
    for name, path in golden_files(variant).items():
        if name not in rendered:
            path.unlink()
    for name, text in rendered.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # LF on every platform, matching what the generator writes, so a
        # Windows update run does not rewrite every line as CRLF.
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.replace("\r\n", "\n"))


def diff_text(variant, name, golden, rendered):
    """A unified diff of one golden file against what was just rendered."""
    return "\n".join(difflib.unified_diff(
        golden.splitlines(),
        rendered.splitlines(),
        fromfile=f"tests/golden/{variant}/{name}",
        tofile=f"rendered/{name}",
        lineterm="",
    ))
