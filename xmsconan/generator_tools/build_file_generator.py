"""Script to generate files for building Conan 2 libraries from templates using TOML data."""
# 1. Standard python modules
import argparse
from dataclasses import asdict
import logging
import os
from pathlib import Path
import shutil
import sys

# 2. Third party modules
from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import UndefinedError

# 3. Aquaveo modules
from xmsconan.build_toml import BuildToml, read_build_toml, XmsDependency
from xmsconan.constants import (
    MSVC_VS2019_VERSION, PYTHON_BINDING_TYPES, TESTING_FRAMEWORKS, VS2019_PLATFORM_KEY,
    VS2019_REMOTE_NAME,
)
from xmsconan.generator_tools.build_filter import load_build_filter
from xmsconan.generator_tools.output_plan import (
    check_plan,
    describe_plan,
    write_text_lf,
)
from xmsconan.package_tools.packager import XmsConanPackager

LOGGER = logging.getLogger(__name__)


def _configure_logging(args):
    """Configure logger from CLI verbosity flags."""
    if args.quiet:
        level = logging.ERROR
    elif args.verbose > 0:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')


#: Packages the generated ``CMakeLists.txt`` already finds on its own, keyed by
#: Conan package name. An ``extra_dependencies`` entry naming one of these is
#: wired into the Conan graph but must not produce a second ``find_package``:
#: Conan publishes boost's and zlib's configs under different names (``Boost``,
#: ``ZLIB``), so the lowercase duplicate is a hard configure error, and pybind11
#: is found inside the ``IS_PYTHON_BUILD`` block where Python is available.
_CMAKE_BUILTIN_DEPENDENCIES = frozenset({
    "boost", "zlib", "pybind11", "cxxtest", "gtest",
})


def _extra_cmake_dependencies(extra_dependencies: list, overrides: dict,
                              xms_dependencies: list[XmsDependency]) -> list[dict]:
    """Resolve which ``extra_dependencies`` the generated CMakeLists should find.

    ``extra_dependencies`` entries are Conan references (``"cereal/1.3.0"``), and
    the generated ``CMakeLists.txt`` needs the *CMake* package name to call
    ``find_package`` with. That is the reference name for most packages, so it is
    the default; ``[extra_dependency_cmake_names]`` overrides an entry whose
    CMake config uses something else, and an empty override keeps a dependency in
    the Conan graph while leaving it out of the CMake file entirely.

    Entries the template already finds are dropped, mirroring the recipe-side
    dedupe in ``XmsConan2File.requirements()`` -- one list must not obey two
    rules. That covers ``xms_dependencies`` (already looped over above the extras
    in the template) and :data:`_CMAKE_BUILTIN_DEPENDENCIES`.

    Args:
        extra_dependencies: The ``extra_dependencies`` list from ``build.toml``.
        overrides: The ``[extra_dependency_cmake_names]`` table.
        xms_dependencies: The xms_dependencies entries.

    Returns:
        One ``{"name": <cmake package name>}`` entry per dependency to find, in
        declaration order, deduplicated.

    Raises:
        ValueError: When an override names a package that is not in
            ``extra_dependencies``, or gives one a non-string value.
    """
    conan_names = [reference.split("/")[0] for reference in extra_dependencies]
    unmatched = sorted(set(overrides) - set(conan_names))
    if unmatched:
        # The same rule vs2019_dependency_overrides enforces: an ignored key is
        # a typo whose only symptom is a find_package that fails naming the
        # *dependency*, pointing nowhere near the misspelled table entry.
        raise ValueError(
            f'extra_dependency_cmake_names names package(s) {", ".join(unmatched)} '
            f'that are not in extra_dependencies. Declared: '
            f'{", ".join(sorted(conan_names)) or "(none)"}.'
        )
    for name, value in overrides.items():
        if not isinstance(value, str):
            # A falsey non-string silently dropped the dependency; a truthy one
            # rendered its repr straight into find_package(3 REQUIRED).
            raise ValueError(
                f'extra_dependency_cmake_names.{name} must be a string, got '
                f'{type(value).__name__} ({value!r}). Use "" to keep the '
                f'dependency out of CMakeLists.txt.'
            )

    already_found = set(_CMAKE_BUILTIN_DEPENDENCIES)
    already_found.update(dep.name for dep in xms_dependencies)

    dependencies = []
    seen = set()
    for conan_name in conan_names:
        if conan_name in already_found or conan_name in seen:
            continue
        seen.add(conan_name)
        cmake_name = overrides.get(conan_name, conan_name)
        if cmake_name:
            dependencies.append({"name": cmake_name})
    return dependencies


def _validate_vocabularies(config: BuildToml, toml_file):
    """Reject values that name a framework, binding, or dependency that does not exist.

    The recipe performs the same checks in ``configure()``, but this function
    writes ``conanfile.py`` *and* ``CMakeLists.txt`` from the same data, and
    the CMake side is where a bad ``testing_framework`` actually bites. Failing
    at generate time names the offending value in ``build.toml``; failing at
    build time names a missing CMake package instead.

    Args:
        config: The parsed build.toml.
        toml_file: Path the data came from, for the error messages.

    Raises:
        ValueError: On a value outside the documented vocabulary, or on an
            ``xms_dependency_options`` key that names no declared dependency.
    """
    for key, accepted in (("testing_framework", TESTING_FRAMEWORKS),
                          ("python_binding_type", PYTHON_BINDING_TYPES)):
        value = getattr(config, key)
        if value not in accepted:
            raise ValueError(
                f'{toml_file}: {key} must be one of {", ".join(sorted(accepted))}; '
                f'got {value!r}.'
            )

    declared = {dependency.name for dependency in config.xms_dependencies}
    unmatched = sorted(set(config.xms_dependency_options) - declared)
    if unmatched:
        raise ValueError(
            f'{toml_file}: xms_dependency_options names {", ".join(unmatched)}, which is '
            f'not declared in xms_dependencies. Declared xms_dependencies: '
            f'{", ".join(sorted(declared)) or "(none)"}.'
        )


def _render_context(config: BuildToml, version: str) -> dict:
    """Build the Jinja context the templates expect from a parsed build.toml.

    Templates read bare top-level names (``{{ library_name }}``), so the
    dataclass is flattened to a dict. Keys whose value is ``None`` are left
    out so ``StrictUndefined`` still reports a missing required field such as
    ``description`` the way it always has.
    """
    context = {key: value for key, value in asdict(config).items() if value is not None}
    context["version"] = version
    # The [filter] table is a baseline matrix restriction; the generated
    # build.py applies it before its own --filter. Validate it here so a typo
    # fails `xmsconan gen` instead of every later build.
    context["build_filter"] = load_build_filter(config)
    context["extra_cmake_dependencies"] = _extra_cmake_dependencies(
        config.extra_dependencies,
        config.extra_dependency_cmake_names,
        config.xms_dependencies,
    )
    # Rendered in rather than imported by the generated build.py. build.py
    # already calls into the installed xmsconan for the packager itself, but a
    # *new name* in xmsconan.constants would make a freshly generated build.py
    # unrunnable against an older installed client -- an ImportError on line 8,
    # before argparse, with nothing said about versions. These are three short
    # literals; carrying them keeps the generated file runnable against
    # whatever xmsconan is on the machine, and it is regenerated by
    # `xmsconan_gen` on every CI run anyway, so it cannot go stale in CI.
    context["vs2019_platform_key"] = VS2019_PLATFORM_KEY
    context["vs2019_remote_name"] = VS2019_REMOTE_NAME
    context["vs2019_msvc_version"] = MSVC_VS2019_VERSION
    return context


def plan_template_render(
    toml_file_path: str,
    version: str,
    template_dir: str,
    output_dir: str,
):
    """
    Render templates with the data in a single TOML file, without writing them.

    Args:
        toml_file_path (str): Path to the TOML file.
        version (str): The build version.
        template_dir (str): Path to the directory containing template files.
        output_dir (str): Directory the rendered files would go in.

    Returns:
        Mapping of output path to rendered content. Writing, ``--dry-run``
        and ``--check`` all act on this one plan, so none of them can
        disagree with the others about what a real run produces.
    """
    toml_file = Path(toml_file_path)
    template_dir = Path(template_dir)
    output_dir = Path(output_dir)

    if not toml_file.exists():
        raise FileNotFoundError(f"The specified TOML file does not exist: {toml_file_path}")

    if not template_dir.exists() or not template_dir.is_dir():
        raise FileNotFoundError(f"The specified template directory does not exist: {template_dir}")

    config = read_build_toml(toml_file)
    # Validated here rather than only where it is consumed: this function writes
    # [matrix] verbatim into the generated conanfile.py, so a caller that renders
    # templates without going on to generate profiles would otherwise produce an
    # artifact from unvalidated input. XmsConanPackager owns the vocabulary.
    XmsConanPackager.resolve_matrix(config.matrix)
    _validate_vocabularies(config, toml_file)
    context = _render_context(config, version)

    # Get all template files in the specified template directory
    template_files = sorted(template_dir.glob("*.jinja"))
    if not template_files:
        raise FileNotFoundError(
            f"No template files (with .jinja extension) were found in the directory: {template_dir}"
        )

    env = Environment(
        keep_trailing_newline=True,
        newline_sequence="\n",
        undefined=StrictUndefined,
    )

    # Iterate through each template file and render it with TOML data
    plan = {}
    for template_file in template_files:
        # Read the template content
        template_content = template_file.read_text(encoding="utf-8")

        # Load the template with Jinja2, force LF for generated newlines
        template = env.from_string(template_content)

        # Render the template with the TOML data
        try:
            rendered_content = template.render(context)
        except UndefinedError as e:
            raise ValueError(f'Missing field in build.toml: {e}.') from e

        # Determine the output file name (strip `.jinja` extension)
        output_file_name = template_file.stem

        plan[output_dir / output_file_name] = rendered_content

    return plan


def render_template_with_toml(
    toml_file_path: str,
    version: str,
    template_dir: str,
    output_dir: str,
    dry_run: bool = False,
):
    """
    Render templates with the data contained in a single TOML file.

    Args:
        toml_file_path (str): Path to the TOML file.
        version (str): The build version.
        template_dir (str): Path to the directory containing template files.
        output_dir (str): Directory to store rendered output files.
        dry_run (bool): If True, only log output files without writing them.
    """
    plan = plan_template_render(toml_file_path, version, template_dir, output_dir)

    if dry_run:
        describe_plan(plan, "template output")
        LOGGER.info("[DRY-RUN] Completed template rendering simulation for TOML: %s", toml_file_path)
        return

    for path, content in plan.items():
        write_text_lf(path, content)
    LOGGER.info("Templates rendered successfully using the TOML file: %s", toml_file_path)


def copy_xms_conan2_file(output_dir: str, dry_run: bool = False) -> None:
    """Copy xms_conan2_file.py to the output directory so conanfile.py can import it locally."""
    src = Path(__file__).parent.parent / "xms_conan2_file.py"
    dst = Path(output_dir) / "xms_conan2_file.py"
    if dry_run:
        LOGGER.info("[DRY-RUN] Would copy %s -> %s", src, dst)
    else:
        shutil.copy2(src, dst)
        LOGGER.info("Copied %s -> %s", src, dst)


def plan_build_files(toml_file_path, version, template_dir, output_dir, with_profiles=True):
    """Render everything ``xmsconan gen`` produces, without writing any of it.

    Covers all four kinds of output, because a check that skipped one would
    call a tree up to date that a real run would change: the top-level
    templates, the ``_package/`` templates, the copied ``xms_conan2_file.py``
    -- which is a file the recipe imports, not documentation, and goes stale
    with every xmsconan release -- and the Conan profiles, which ``gen``
    writes by default.

    Args:
        toml_file_path: Path to the repository's build.toml.
        version: The build version.
        template_dir: Directory holding the top-level templates.
        output_dir: Directory the generated files would go in.
        with_profiles: Include the profiles and presets, matching the
            default that ``--no-profiles`` turns off.

    Returns:
        ``(plan, stale)`` -- a mapping of path to content, and the paths a
        real run would delete.
    """
    output_dir = Path(output_dir)
    plan = dict(plan_template_render(toml_file_path, version, template_dir, output_dir))

    source = Path(__file__).parent.parent / "xms_conan2_file.py"
    plan[output_dir / "xms_conan2_file.py"] = source.read_text(encoding="utf-8")

    package_template_dir = os.path.join(template_dir, "_package")
    if os.path.isdir(package_template_dir):
        plan.update(plan_template_render(
            toml_file_path, version, package_template_dir, output_dir / "_package",
        ))

    stale = []
    if with_profiles:
        from xmsconan.generator_tools.profile_generator import plan_profile_files
        profile_plan, stale = plan_profile_files(toml_file_path=toml_file_path)
        plan.update(profile_plan)

    return plan, stale


def main():
    """Main function to parse arguments and render templates using TOML data."""
    default_template_dir = Path(__file__).parent / "templates"
    parser = argparse.ArgumentParser(description="Render templates using a single TOML file.")
    parser.add_argument("--template_dir", default=default_template_dir, help="Directory containing template files.")
    parser.add_argument(
        "--output_dir",
        default=".",
        help="Directory to store rendered output files. Defaults to the TOML file's directory if not specified.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show files that would be generated without writing them.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 with a diff if any generated file is out of date. Writes nothing.",
    )
    parser.add_argument(
        "--no-profiles",
        action="store_true",
        help="Skip generating conan_profiles/. Profiles are generated by default.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase output verbosity (use -v for debug details).",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Only show errors.")
    parser.add_argument(
        "--version", default=None,
        help="The build version. If omitted, tries setuptools-scm then falls back to 0.0.0.",
    )
    parser.add_argument("toml_file", nargs="?", default="build.toml",
                        help="Path to the TOML file. Defaults to build.toml in the current directory.")

    args = parser.parse_args()
    _configure_logging(args)

    from xmsconan.generator_tools.version import resolve_version
    version = resolve_version(args.version)

    try:
        if args.check:
            plan, stale = plan_build_files(
                toml_file_path=args.toml_file,
                version=version,
                template_dir=args.template_dir,
                output_dir=args.output_dir,
                with_profiles=not args.no_profiles,
            )
            raise SystemExit(check_plan(plan, stale=stale))

        render_template_with_toml(
            toml_file_path=args.toml_file,
            version=version,
            template_dir=args.template_dir,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )

        copy_xms_conan2_file(output_dir=args.output_dir, dry_run=args.dry_run)

        package_template_dir = os.path.join(args.template_dir, "_package")
        if os.path.isdir(package_template_dir):
            render_template_with_toml(
                toml_file_path=args.toml_file,
                version=version,
                template_dir=package_template_dir,
                output_dir=os.path.join(args.output_dir, "_package"),
                dry_run=args.dry_run,
            )

        if not args.no_profiles:
            # Conan profiles come from the same build.toml as everything else.
            # The build files above are already written and stay written, so the
            # message says what is on disk -- but the command still exits
            # non-zero, because a warning is invisible to CI. Only configuration
            # and I/O errors are caught: anything else is a defect in this tool
            # and belongs in a traceback, not in a one-line summary.
            from xmsconan.generator_tools.profile_generator import generate_profiles
            try:
                generate_profiles(toml_file_path=args.toml_file, dry_run=args.dry_run)
            except (OSError, ValueError) as profile_error:
                LOGGER.error(
                    "Build files were generated, but Conan profile generation failed: %s. "
                    "Profiles are written one at a time, so conan_profiles/ may hold a mix "
                    "of fresh and stale files; re-run `xmsconan_profiles %s` once the error "
                    "above is fixed.",
                    profile_error, args.toml_file, exc_info=True,
                )
                raise SystemExit(1) from profile_error
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
