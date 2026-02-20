"""Script to generate files for building Conan 2 libraries from templates using TOML data."""
# 1. Standard python modules
import argparse
import os
import sys
from pathlib import Path

# 2. Third party modules
from jinja2 import Environment, StrictUndefined
import toml

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


def _write_text_lf(path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    Write text using LF line endings on all platforms.
    """
    # Normalize CRLF -> LF just in case
    content = content.replace("\r\n", "\n")
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(content)


def render_template_with_toml(toml_file_path: str, version: str, template_dir: str, output_dir: str):
    """
    Render templates with the data contained in a single TOML file.

    Args:
        toml_file_path (str): Path to the TOML file.
        version (str): The build version.
        template_dir (str): Path to the directory containing template files.
        output_dir (str): Directory to store rendered output files.
    """
    toml_file = Path(toml_file_path)
    template_dir = Path(template_dir)
    output_dir = Path(output_dir)

    if not toml_file.exists():
        raise FileNotFoundError(f"The specified TOML file does not exist: {toml_file_path}")

    if not template_dir.exists() or not template_dir.is_dir():
        raise FileNotFoundError(f"The specified template directory does not exist: {template_dir}")

    # Parse the TOML file into a dictionary
    if tomllib:
        toml_data = tomllib.loads(toml_file.read_text(encoding="utf-8"))
    else:
        toml_data = toml.loads(toml_file.read_text(encoding="utf-8"))
    toml_data["version"] = version

    # Set defaults for optional keys
    toml_data.setdefault("testing_framework", "cxxtest")

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
        rendered_content = template.render(toml_data)

        # Determine the output file name (strip `.jinja` extension)
        output_file_name = template_file.stem

        # Write the rendered content directly to the output directory with LF endings
        output_file = output_dir / output_file_name
        _write_text_lf(output_file, rendered_content)

    print(f"Templates rendered successfully using the TOML file: {toml_file_path}")


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
    parser.add_argument("--version", default="99.99.99", help="The build version.")
    parser.add_argument("toml_file", help="Path to the required TOML file (always the last argument).")

    args = parser.parse_args()

    try:
        render_template_with_toml(
            toml_file_path=args.toml_file,
            version=args.version,
            template_dir=args.template_dir,
            output_dir=args.output_dir,
        )

        package_template_dir = os.path.join(args.template_dir, "_package")
        if os.path.isdir(package_template_dir):
            render_template_with_toml(
                toml_file_path=args.toml_file,
                version=args.version,
                template_dir=package_template_dir,
                output_dir=os.path.join(args.output_dir, "_package"),
            )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
