"""Script to generate files for building Conan 2 libraries from templates using TOML data."""
# 1. Standard python modules
import argparse
import logging
import os
from pathlib import Path
import shutil
import sys

# 2. Third party modules
from jinja2 import Environment, StrictUndefined
from jinja2.exceptions import UndefinedError

# 3. Aquaveo modules
from xmsconan.constants import PYTHON_BINDING_TYPES, TESTING_FRAMEWORKS
from xmsconan.generator_tools.build_filter import load_build_filter
from xmsconan.package_tools.packager import XmsConanPackager
from xmsconan.toml_utils import load_toml, validate_top_level_keys

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


def _write_text_lf(path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    Write text using LF line endings on all platforms.
    """
    # Normalize CRLF -> LF just in case
    content = content.replace("\r\n", "\n")
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(content)


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
                              xms_dependencies: list) -> list[dict]:
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
        xms_dependencies: The normalized ``xms_dependencies`` entries.

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
    already_found.update(
        dep["name"] if isinstance(dep, dict) else str(dep).split("/")[0]
        for dep in xms_dependencies
    )

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


def _validate_vocabularies(toml_data, toml_file):
    """Reject values that name a framework, binding, or dependency that does not exist.

    The recipe performs the same checks in ``configure()``, but this function
    writes ``conanfile.py`` *and* ``CMakeLists.txt`` from the same data, and
    the CMake side is where a bad ``testing_framework`` actually bites. Failing
    at generate time names the offending value in ``build.toml``; failing at
    build time names a missing CMake package instead.

    Args:
        toml_data: The parsed build.toml, after defaults have been applied.
        toml_file: Path the data came from, for the error messages.

    Raises:
        ValueError: On a value outside the documented vocabulary, or on an
            ``xms_dependency_options`` key that names no declared dependency.
    """
    for key, accepted in (("testing_framework", TESTING_FRAMEWORKS),
                          ("python_binding_type", PYTHON_BINDING_TYPES)):
        value = toml_data[key]
        if value not in accepted:
            raise ValueError(
                f'{toml_file}: {key} must be one of {", ".join(sorted(accepted))}; '
                f'got {value!r}.'
            )

    declared = {
        dependency["name"] if isinstance(dependency, dict) else str(dependency).split("/")[0]
        for dependency in toml_data["xms_dependencies"]
    }
    unmatched = sorted(set(toml_data["xms_dependency_options"]) - declared)
    if unmatched:
        raise ValueError(
            f'{toml_file}: xms_dependency_options names {", ".join(unmatched)}, which is '
            f'not declared in xms_dependencies. Declared xms_dependencies: '
            f'{", ".join(sorted(declared)) or "(none)"}.'
        )


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
    toml_file = Path(toml_file_path)
    template_dir = Path(template_dir)
    output_dir = Path(output_dir)

    if not toml_file.exists():
        raise FileNotFoundError(f"The specified TOML file does not exist: {toml_file_path}")

    if not template_dir.exists() or not template_dir.is_dir():
        raise FileNotFoundError(f"The specified template directory does not exist: {template_dir}")

    # Parse the TOML file into a dictionary
    toml_data = load_toml(toml_file)
    # Before the setdefault block below, which is what makes a misspelled key
    # invisible: every optional key gets its default whether or not the file
    # meant to set it.
    validate_top_level_keys(toml_data, toml_file)
    toml_data["version"] = version

    # Set defaults for optional keys to prevent StrictUndefined template errors
    toml_data.setdefault("testing_framework", "cxxtest")
    toml_data.setdefault("python_binding_type", "pybind11")
    toml_data.setdefault("extra_cmake_text", "")
    toml_data.setdefault("post_library_cmake_text", "")
    toml_data.setdefault("extra_dependencies", [])
    toml_data.setdefault("extra_export_sources", [])
    toml_data.setdefault("library_sources", [])
    toml_data.setdefault("library_headers", [])
    toml_data.setdefault("testing_sources", [])
    toml_data.setdefault("testing_headers", [])
    toml_data.setdefault("python_library_sources", [])
    toml_data.setdefault("python_library_headers", [])
    toml_data.setdefault("pybind_sources", [])
    toml_data.setdefault("pybind_headers", [])
    toml_data.setdefault("xms_dependency_options", {})
    toml_data.setdefault("extra_dependency_cmake_names", {})
    toml_data.setdefault("matrix", {})
    toml_data.setdefault("pybind_advertises_module", False)
    toml_data.setdefault("vs2019_dependency_overrides", {})
    toml_data.setdefault("xms_dependencies", [])
    toml_data.setdefault("xms_python_dependencies", [])
    toml_data.setdefault("conan_profile_options", {})

    # The [filter] table is a baseline matrix restriction; the generated
    # build.py applies it before its own --filter. Validate it here so a typo
    # fails `xmsconan gen` instead of every later build.
    toml_data["build_filter"] = load_build_filter(toml_data)

    if "library_name" in toml_data:
        toml_data.setdefault("python_namespaced_dir", toml_data["library_name"][3:])

    # Normalize xms_dependencies: add no_python=False if missing
    if "xms_dependencies" in toml_data:
        for dep in toml_data["xms_dependencies"]:
            if isinstance(dep, dict):
                dep.setdefault("no_python", False)

    # Validated here rather than only where it is consumed: this function writes
    # [matrix] verbatim into the generated conanfile.py, so a caller that renders
    # templates without going on to generate profiles would otherwise produce an
    # artifact from unvalidated input. XmsConanPackager owns the vocabulary.
    XmsConanPackager.resolve_matrix(toml_data["matrix"])
    _validate_vocabularies(toml_data, toml_file)

    toml_data["extra_cmake_dependencies"] = _extra_cmake_dependencies(
        toml_data["extra_dependencies"],
        toml_data["extra_dependency_cmake_names"],
        toml_data["xms_dependencies"],
    )

    # Ensure the output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

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
    for template_file in template_files:
        # Read the template content
        template_content = template_file.read_text(encoding="utf-8")

        # Load the template with Jinja2, force LF for generated newlines
        template = env.from_string(template_content)

        # Render the template with the TOML data
        try:
            rendered_content = template.render(toml_data)
        except UndefinedError as e:
            raise ValueError(f'Missing field in build.toml: {e}.') from e

        # Determine the output file name (strip `.jinja` extension)
        output_file_name = template_file.stem

        # Write the rendered content directly to the output directory with LF endings
        output_file = output_dir / output_file_name
        if dry_run:
            LOGGER.info("[DRY-RUN] Would write template output: %s", output_file)
        else:
            _write_text_lf(output_file, rendered_content)

    if dry_run:
        LOGGER.info("[DRY-RUN] Completed template rendering simulation for TOML: %s", toml_file_path)
    else:
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
