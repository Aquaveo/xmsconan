"""Tests for generator_tools.build_file_generator."""
import pytest

from xmsconan.generator_tools.build_file_generator import (
    _write_text_lf,
    copy_xms_conan2_file,
    render_template_with_toml,
)


# --- Converted from existing unittest tests ---


def test_dry_run_does_not_write_output(build_toml, template_dir, tmp_path):
    """Dry-run mode renders without writing files."""
    output_dir = tmp_path / "output"

    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.2.3",
        template_dir=str(template_dir),
        output_dir=str(output_dir),
        dry_run=True,
    )

    assert not (output_dir / "sample.txt").exists()


def test_writes_rendered_output(build_toml, template_dir, tmp_path):
    """Normal mode writes rendered output files."""
    output_dir = tmp_path / "output"

    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.2.3",
        template_dir=str(template_dir),
        output_dir=str(output_dir),
        dry_run=False,
    )

    rendered = output_dir / "sample.txt"
    assert rendered.exists()
    assert rendered.read_text(encoding="utf-8") == "version=1.2.3\n"


# --- New tests ---


def test_missing_toml_raises_file_not_found(template_dir, tmp_path):
    """Raises FileNotFoundError when TOML file is missing."""
    with pytest.raises(FileNotFoundError):
        render_template_with_toml(
            toml_file_path=str(tmp_path / "nonexistent.toml"),
            version="1.0.0",
            template_dir=str(template_dir),
            output_dir=str(tmp_path / "output"),
        )


def test_missing_template_dir_raises_file_not_found(build_toml, tmp_path):
    """Raises FileNotFoundError when template directory is missing."""
    with pytest.raises(FileNotFoundError):
        render_template_with_toml(
            toml_file_path=str(build_toml),
            version="1.0.0",
            template_dir=str(tmp_path / "no_such_dir"),
            output_dir=str(tmp_path / "output"),
        )


def test_no_templates_raises_file_not_found(build_toml, tmp_path):
    """Raises FileNotFoundError when template dir has no .jinja files."""
    empty_tpl = tmp_path / "empty_templates"
    empty_tpl.mkdir()

    with pytest.raises(FileNotFoundError):
        render_template_with_toml(
            toml_file_path=str(build_toml),
            version="1.0.0",
            template_dir=str(empty_tpl),
            output_dir=str(tmp_path / "output"),
        )


def test_toml_defaults_applied(tmp_path):
    """Optional keys get defaults so Jinja2 StrictUndefined doesn't fail."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "out.txt.jinja").write_text(
        "framework={{ testing_framework }}\nbinding={{ python_binding_type }}\n",
        encoding="utf-8",
    )

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "out.txt").read_text(encoding="utf-8")
    assert "framework=cxxtest" in content
    assert "binding=pybind11" in content


def test_xms_dependencies_normalized(tmp_path):
    """Dependencies get no_python=False added if missing."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsgrid"\n'
        'description = "desc"\n'
        '[[xms_dependencies]]\n'
        'name = "xmscore"\n'
        'version = "7.0.0"\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    # Render the no_python value from the first dependency
    (tpl_dir / "out.txt.jinja").write_text(
        "{% for dep in xms_dependencies %}{{ dep.no_python }}{% endfor %}\n",
        encoding="utf-8",
    )

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "out.txt").read_text(encoding="utf-8")
    assert "False" in content


def test_version_injected_into_context(build_toml, template_dir, tmp_path):
    """{{ version }} renders the supplied version string."""
    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="5.6.7",
        template_dir=str(template_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "sample.txt").read_text(encoding="utf-8")
    assert content == "version=5.6.7\n"


def test_write_text_lf_normalizes_crlf(tmp_path):
    """_write_text_lf converts CRLF to LF."""
    out = tmp_path / "lf_test.txt"
    _write_text_lf(out, "line1\r\nline2\r\n")

    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert raw == b"line1\nline2\n"


def test_copy_xms_conan2_file_dry_run(tmp_path):
    """Dry-run does not copy xms_conan2_file.py."""
    copy_xms_conan2_file(str(tmp_path), dry_run=True)
    assert not (tmp_path / "xms_conan2_file.py").exists()


def test_copy_xms_conan2_file_copies(tmp_path):
    """Normal mode copies xms_conan2_file.py to output dir."""
    copy_xms_conan2_file(str(tmp_path), dry_run=False)
    assert (tmp_path / "xms_conan2_file.py").exists()
