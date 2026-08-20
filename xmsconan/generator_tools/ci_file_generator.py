"""Script to generate CI configuration files from build.toml."""
# 1. Standard python modules
import argparse
import logging
from pathlib import Path
import sys

# 2. Third party modules
from jinja2 import Environment, StrictUndefined

# 3. Aquaveo modules
from xmsconan.ci_options import repairs_windows_wheel, validate_ci_table
from xmsconan.constants import SUPPORTED_PYTHON_VERSIONS, version_sort_key
from xmsconan.toml_utils import load_toml


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
    """Write text using LF line endings on all platforms."""
    content = content.replace("\r\n", "\n")
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(content)


def _display_name(library_name: str) -> str:
    """Convert library_name to display format (e.g., 'xmscore' -> 'XmsCore')."""
    return "Xms" + library_name[3:].title()


def _job_name_py(platform_python_versions: list) -> str:
    """Return the ``name:`` fragment that keeps fanned-out legs distinguishable.

    Empty on a single-version platform. GitHub uses an explicit ``name:``
    verbatim and only auto-appends matrix values when none is given, so legs
    differing solely by ABI would otherwise share one status-check name --
    ambiguous both in the checks list and in branch-protection matching. Held
    to the fan-out case for the same reason as :func:`_py_suffix`: adding it
    unconditionally would rename the required check in every repo that has not
    opted in.
    """
    if len(platform_python_versions) > 1:
        return ", ${{ matrix.python-version }}"
    return ""


def _platform_python_versions(ci_config: dict, platform: str, ci_python_versions: list) -> list:
    """Resolve the Python fan-out for one non-Windows platform.

    ``[ci].python_versions`` fans out Windows only, because Windows is the
    one platform whose interpreters all come from ``actions/setup-python``
    and therefore cost nothing but runner minutes. macOS and Linux opt in
    separately via ``[ci].mac_python_versions`` / ``[ci].linux_python_versions``:

    * macOS, so that adding a Windows-only ABI (3.10 ships in Windows
      wheels for the desktop products) does not silently triple the mac
      matrix.
    * Linux, because each entry needs a matching
      ``ghcr.io/aquaveo/conan-gcc13-py<version>`` container to exist. Naming
      a version with no published image yields a job that cannot start, so
      this list tracks the images that are actually built rather than
      whatever ``python_versions`` happens to say.

    Defaults to the single highest entry of ``ci_python_versions``, which is
    the behavior these platforms had when the version was hardcoded.
    """
    configured = ci_config.get(f"{platform}_python_versions")
    if configured:
        return list(configured)
    return [max(ci_python_versions, key=version_sort_key)]


def _py_suffix(platform_python_versions: list) -> str:
    """Return the per-ABI suffix for artifact and release-asset names.

    Empty on a single-version platform. Job names, uploaded artifacts and the
    release-asset tarball are all keyed off ``MATRIX_NAME``, so a platform that
    fans out across ABIs needs the version in that name or the legs overwrite
    each other. Suppressing the suffix when there is nothing to disambiguate
    keeps the asset names of every project that has not opted in byte-identical
    to what it published before -- release assets are fetched by exact name.
    """
    if len(platform_python_versions) > 1:
        return "-py${{ matrix.python-version }}"
    return ""


_DEFAULT_COVERAGE_PYTHON_VERSION = "3.13"


def _resolve_coverage_python_version(toml_data: dict) -> str:
    """Pick the single python_version the coverage build should pin to.

    Precedence: ``[coverage].python_version`` (explicit opt-in) > highest
    entry in the resolved Linux list > the global default (``"3.13"``).
    Coverage runs a single instrumented build, so we must commit to one ABI up
    front rather than let ``_find_coverage_package`` return whichever pybind
    config happened to finish last (see issue #65).

    The fallback reads the *Linux* list rather than ``[ci].python_versions``
    because coverage only ever runs on Linux, and on GitLab the resolved
    version also selects the container image. Taking the highest Windows entry
    would pick a version with no published ``conan-gcc13-py<version>`` image --
    3.10 through 3.12 have none -- and the job would die pulling its container.
    """
    coverage_cfg = toml_data.get("coverage", {})
    explicit = coverage_cfg.get("python_version")
    if explicit:
        return str(explicit)
    ci_config = toml_data.get("ci", {})
    linux_versions = _platform_python_versions(
        ci_config, "linux",
        list(ci_config.get("python_versions") or [_DEFAULT_COVERAGE_PYTHON_VERSION]),
    )
    return max(linux_versions, key=version_sort_key)


def _coverage_context(coverage_config: dict, library_name: str) -> dict:
    """Build the coverage template context, applying sensible defaults."""
    default_filters = [f"{library_name}/"]
    default_excludes = [
        r".*\.t\.h$",
        f".*/{library_name}/python/.*",
        r".*/_package/tests/.*",
    ]
    return {
        "cpp_threshold": float(coverage_config.get("cpp_threshold", 0)),
        "python_threshold": float(coverage_config.get("python_threshold", 0)),
        "filters": list(coverage_config.get("filters", default_filters)),
        "excludes": list(coverage_config.get("excludes", default_excludes)),
    }


def generate_ci(
    toml_file_path: str,
    version: str,
    output_dir: str,
    dry_run: bool = False,
):
    """
    Generate CI configuration file from build.toml.

    Args:
        toml_file_path (str): Path to the build.toml file.
        version (str): The build version.
        output_dir (str): Root directory for CI file output.
        dry_run (bool): If True, only log output files without writing them.
    """
    toml_file = Path(toml_file_path)
    output_dir = Path(output_dir)

    if not toml_file.exists():
        raise FileNotFoundError(f"The specified TOML file does not exist: {toml_file_path}")

    # Parse the TOML file
    toml_data = load_toml(toml_file)

    ci_type = toml_data.get("ci_type")
    if not ci_type:
        raise ValueError("build.toml must include a 'ci_type' key ('github' or 'gitlab')")
    if ci_type not in ("github", "gitlab"):
        raise ValueError(f"ci_type must be 'github' or 'gitlab', got '{ci_type}'")

    library_name = toml_data["library_name"]
    display = _display_name(library_name)

    # CI-specific options (for GitLab conditional sections)
    ci_config = toml_data.get("ci", {})

    # A misspelled key or a quoted boolean here is otherwise invisible: the
    # reader falls back to a default, and for a switch that turns work off the
    # work simply keeps happening.
    validate_ci_table(ci_config)

    # A GitLab pipeline with neither platform builds nothing, and coverage runs
    # only under gcc.  Reject the impossible combinations here rather than
    # emitting a pipeline that fails opaquely in CI.  Each platform now stages
    # and deploys its own wheel -- Linux through the Package-stage Repair Wheel
    # job, Windows in place inside its build job -- so a Windows-only pipeline
    # publishes wheels and only the coverage rule below still needs Linux.
    if ci_type == "gitlab":
        if not ci_config.get("linux", True) and not ci_config.get("windows", True):
            raise ValueError(
                "build.toml sets both [ci].linux and [ci].windows to false, "
                "which would generate a pipeline with nothing to build."
            )
        if ci_config.get("coverage", False) and not ci_config.get("linux", True):
            raise ValueError(
                "build.toml sets [ci].coverage = true with [ci].linux = false. "
                "Coverage builds with --coverage under gcc; the generated "
                "CMakeLists rejects MSVC when XMS_COVERAGE is set."
            )
    # [ci].linux and [ci].windows are GitLab-only (see docs/USAGE.md, "CI
    # options"). The GitHub templates ignore both, so a project that sets
    # either here gets the full matrix and no indication its setting did
    # nothing. Documented is not the same as discoverable -- say so at
    # generation time. Warn on any explicit setting, not just false: setting
    # one to true is equally inert and equally worth knowing.
    if ci_type == "github":
        ignored = [key for key in ("linux", "windows") if key in ci_config]
        if ignored:
            LOGGER.warning(
                "build.toml sets %s, but %s GitLab-only; the generated GitHub "
                "workflow ignores %s and emits the full matrix.",
                " and ".join(f"[ci].{key}" for key in ignored),
                "these are" if len(ignored) > 1 else "this is",
                "them" if len(ignored) > 1 else "it",
            )

    ci_python_versions = list(ci_config.get("python_versions", ["3.13"]))
    ci_mac_python_versions = _platform_python_versions(ci_config, "mac", ci_python_versions)
    ci_linux_python_versions = _platform_python_versions(ci_config, "linux", ci_python_versions)

    for key, versions in (("python_versions", ci_python_versions),
                          ("mac_python_versions", ci_mac_python_versions),
                          ("linux_python_versions", ci_linux_python_versions)):
        unsupported = [str(v) for v in versions if str(v) not in SUPPORTED_PYTHON_VERSIONS]
        if unsupported:
            raise ValueError(
                f"build.toml [ci].{key} names Python {', '.join(unsupported)}, which "
                f"the conanfile's python_version option does not allow (supported: "
                f"{', '.join(SUPPORTED_PYTHON_VERSIONS)}). Generating that matrix leg "
                "would only defer the failure to conan configure time in CI."
            )

    # This guard is about the Linux fan-out, so it is moot when Linux is
    # switched off entirely -- the list is inert in that case.
    gitlab_split_tests = ci_type == "gitlab" and ci_config.get("split_tests", False)
    linux_enabled = ci_config.get("linux", True)
    if gitlab_split_tests and linux_enabled and len(ci_linux_python_versions) > 1:
        raise ValueError(
            "build.toml combines [ci].split_tests with more than one "
            "[ci].linux_python_versions. The GitLab C++ test job consumes the "
            "build job's artifacts by name, so a multi-ABI build would leave it "
            "testing an indeterminate one. Drop split_tests or keep "
            "linux_python_versions to a single entry."
        )

    coverage_config = toml_data.get("coverage", {})

    from xmsconan import __version__ as xmsconan_version

    # Build template context
    context = {
        "xmsconan_version": xmsconan_version,
        "library_name": library_name,
        "display_name": display,
        "version": version,
        "python_namespaced_dir": toml_data.get("python_namespaced_dir", library_name[3:]),
        "ci_windows": ci_config.get("windows", True),
        # Windows-scoped on purpose: a manylinux wheel has to be repaired to be
        # installable, so there is no equivalent switch for Linux or macOS. The
        # default follows ci_type -- see repairs_windows_wheel.
        "ci_windows_wheel_repair": repairs_windows_wheel(toml_data),
        "ci_linux": ci_config.get("linux", True),
        "ci_deploy": ci_config.get("deploy", True),
        "ci_coverage": ci_config.get("coverage", False),
        "ci_xvfb": ci_config.get("xvfb", False),
        "ci_linux_arm": ci_config.get("linux_arm", False),
        "docker_image": ci_config.get("docker_image", ""),
        "ci_split_tests": ci_config.get("split_tests", False),
        "ci_test_shards": ci_config.get("test_shards", 0),
        "ci_python_versions": ci_python_versions,
        "ci_mac_python_versions": ci_mac_python_versions,
        "ci_linux_python_versions": ci_linux_python_versions,
        "ci_mac_py_suffix": _py_suffix(ci_mac_python_versions),
        "gitlab_linux_fanout": len(ci_linux_python_versions) > 1,
        "gitlab_linux_image_py": (
            "${PYTHON_TARGET_VERSION}" if len(ci_linux_python_versions) > 1
            else ci_linux_python_versions[0]
        ),
        "gitlab_linux_single_py": max(ci_linux_python_versions, key=version_sort_key),
        "gitlab_linux_py_suffix": (
            "-py${PYTHON_TARGET_VERSION}" if len(ci_linux_python_versions) > 1 else ""
        ),
        "ci_linux_py_suffix": _py_suffix(ci_linux_python_versions),
        "ci_linux_name_py": _job_name_py(ci_linux_python_versions),
        "coverage": _coverage_context(coverage_config, library_name),
        "coverage_python_version": _resolve_coverage_python_version(toml_data),
    }

    # Select templates and output paths
    template_dir = Path(__file__).parent / "ci_templates"
    if ci_type == "github":
        renders = [(template_dir / "github-ci.yaml.jinja",
                    output_dir / ".github" / "workflows" / f"{display}-CI.yaml")]
        if context["ci_coverage"]:
            renders.append((template_dir / "github-coverage.yaml.jinja",
                            output_dir / ".github" / "workflows" / "Coverage.yaml"))
    else:
        renders = [(template_dir / "gitlab-ci.yml.jinja", output_dir / ".gitlab-ci.yml")]

    for template_file, _ in renders:
        if not template_file.exists():
            raise FileNotFoundError(f"CI template not found: {template_file}")

    # Use custom delimiters to avoid conflicts with GitHub Actions ${{ }}
    env = Environment(
        variable_start_string="<<",
        variable_end_string=">>",
        block_start_string="<%",
        block_end_string="%>",
        comment_start_string="<#",
        comment_end_string="#>",
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        newline_sequence="\n",
        undefined=StrictUndefined,
    )

    for template_file, output_path in renders:
        template_content = template_file.read_text(encoding="utf-8")
        template = env.from_string(template_content)
        rendered = template.render(context)

        if dry_run:
            LOGGER.info("[DRY-RUN] Would write CI file: %s", output_path)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _write_text_lf(output_path, rendered)
            LOGGER.info("Generated CI file: %s", output_path)


def main():
    """Main function to parse arguments and generate CI configuration."""
    parser = argparse.ArgumentParser(description="Generate CI configuration from build.toml.")
    parser.add_argument(
        "--output_dir",
        default=".",
        help="Root directory for CI file output. Defaults to current directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show files that would be generated without writing them.",
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
                        help="Path to the build.toml file. Defaults to build.toml in the current directory.")

    args = parser.parse_args()
    _configure_logging(args)

    from xmsconan.generator_tools.version import resolve_version
    version = resolve_version(args.version)

    try:
        generate_ci(
            toml_file_path=args.toml_file,
            version=version,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
