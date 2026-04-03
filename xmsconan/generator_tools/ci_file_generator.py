"""Script to generate CI configuration files from build.toml."""
# 1. Standard python modules
import argparse
import logging
from pathlib import Path
import sys

# 2. Third party modules
from jinja2 import Environment, StrictUndefined
import toml

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


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
    if tomllib:
        toml_data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
    else:
        toml_data = toml.loads(toml_file.read_text(encoding="utf-8"))

    ci_type = toml_data.get("ci_type")
    if not ci_type:
        raise ValueError("build.toml must include a 'ci_type' key ('github' or 'gitlab')")
    if ci_type not in ("github", "gitlab"):
        raise ValueError(f"ci_type must be 'github' or 'gitlab', got '{ci_type}'")

    library_name = toml_data["library_name"]
    display = _display_name(library_name)

    # CI-specific options (for GitLab conditional sections)
    ci_config = toml_data.get("ci", {})

    from xmsconan import __version__ as xmsconan_version

    # Build template context
    context = {
        "xmsconan_version": xmsconan_version,
        "library_name": library_name,
        "display_name": display,
        "version": version,
        "python_namespaced_dir": toml_data.get("python_namespaced_dir", library_name[3:]),
        "ci_windows": ci_config.get("windows", True),
        "ci_deploy": ci_config.get("deploy", True),
        "ci_coverage": ci_config.get("coverage", False),
        "ci_xvfb": ci_config.get("xvfb", False),
        "ci_linux_arm": ci_config.get("linux_arm", False),
        "docker_image": ci_config.get("docker_image", ""),
        "ci_split_tests": ci_config.get("split_tests", False),
    }

    # Select template and output path
    template_dir = Path(__file__).parent / "ci_templates"
    if ci_type == "github":
        template_file = template_dir / "github-ci.yaml.jinja"
        output_path = output_dir / ".github" / "workflows" / f"{display}-CI.yaml"
    else:
        template_file = template_dir / "gitlab-ci.yml.jinja"
        output_path = output_dir / ".gitlab-ci.yml"

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
