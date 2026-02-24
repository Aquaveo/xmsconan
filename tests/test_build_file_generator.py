"""Tests for generator_tools.build_file_generator."""

from pathlib import Path
import tempfile
import unittest

from xmsconan.generator_tools.build_file_generator import render_template_with_toml


class BuildFileGeneratorTests(unittest.TestCase):
    """Tests for template rendering and dry-run behavior."""

    def test_dry_run_does_not_write_output_file(self):
        """Ensure dry-run mode renders logically without writing files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            template_dir = root / "templates"
            output_dir = root / "output"
            toml_path = root / "build.toml"

            template_dir.mkdir(parents=True, exist_ok=True)
            (template_dir / "sample.txt.jinja").write_text("version={{ version }}\n", encoding="utf-8")
            toml_path.write_text('library_name="xmscore"\ndescription="desc"\n', encoding="utf-8")

            render_template_with_toml(
                toml_file_path=str(toml_path),
                version="1.2.3",
                template_dir=str(template_dir),
                output_dir=str(output_dir),
                dry_run=True,
            )

            self.assertFalse((output_dir / "sample.txt").exists())

    def test_non_dry_run_writes_output_file(self):
        """Ensure normal mode writes rendered output files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            template_dir = root / "templates"
            output_dir = root / "output"
            toml_path = root / "build.toml"

            template_dir.mkdir(parents=True, exist_ok=True)
            (template_dir / "sample.txt.jinja").write_text("version={{ version }}\n", encoding="utf-8")
            toml_path.write_text('library_name="xmscore"\ndescription="desc"\n', encoding="utf-8")

            render_template_with_toml(
                toml_file_path=str(toml_path),
                version="1.2.3",
                template_dir=str(template_dir),
                output_dir=str(output_dir),
                dry_run=False,
            )

            rendered = output_dir / "sample.txt"
            self.assertTrue(rendered.exists())
            self.assertEqual(rendered.read_text(encoding="utf-8"), "version=1.2.3\n")


if __name__ == "__main__":
    unittest.main()
