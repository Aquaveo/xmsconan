"""Tests for generator_tools.build_file_generator."""
from pathlib import Path

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


# --- Template generation tests ---

REAL_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "xmsconan" / "generator_tools" / "templates"


def _copy_template(name, dest_dir):
    """Copy a single template from the real template dir into dest_dir."""
    src = REAL_TEMPLATE_DIR / name
    dst = dest_dir / name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def test_generates_pytest_ini(build_toml, tmp_path):
    """pytest.ini is generated with *_pyt.py discovery pattern."""
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("pytest.ini.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    pytest_ini = output_dir / "pytest.ini"
    assert pytest_ini.exists()
    content = pytest_ini.read_text(encoding="utf-8")
    assert "*_pyt.py" in content
    assert "testpaths = _package/tests" in content


def test_generates_flake8(build_toml, tmp_path):
    """.flake8 is generated with library_name substituted."""
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template(".flake8.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    flake8 = output_dir / ".flake8"
    assert flake8.exists()
    content = flake8.read_text(encoding="utf-8")
    assert "application-import-names = xms.core" in content
    assert "application-package-names = xms" in content


def test_conan_profile_options_reaches_template_context(tmp_path):
    """Nested TOML tables for conan_profile_options reach the template as a dict."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        '[conan_profile_options.boost]\n'
        'wchar_t = "builtin"\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "templates"
    tpl_dir.mkdir()
    (tpl_dir / "out.txt.jinja").write_text(
        "opts={{ conan_profile_options }}\n",
        encoding="utf-8",
    )

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    assert (output_dir / "out.txt").read_text(encoding="utf-8") == "opts={'boost': {'wchar_t': 'builtin'}}\n"


def test_generates_conanfile_with_profile_options(tmp_path):
    """Generated conanfile.py defines CONAN_PROFILE_OPTIONS at module scope when set in TOML."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        '[conan_profile_options.boost]\n'
        'wchar_t = "builtin"\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("conanfile.py.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "conanfile.py").read_text(encoding="utf-8")
    assert "CONAN_PROFILE_OPTIONS = {'boost': {'wchar_t': 'builtin'}}" in content


def test_generates_conanfile_without_profile_options(tmp_path):
    """Generated conanfile.py defines CONAN_PROFILE_OPTIONS = {} when TOML omits the key."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("conanfile.py.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "conanfile.py").read_text(encoding="utf-8")
    assert "CONAN_PROFILE_OPTIONS = {}" in content


def test_generates_build_py_with_test_shards(build_toml, tmp_path):
    """build.py is generated with --test-shards argument and auto mode logic."""
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("build.py.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    build_py = output_dir / "build.py"
    assert build_py.exists()
    content = build_py.read_text(encoding="utf-8")
    assert "--test-shards" in content
    assert "auto" in content
    assert "os.cpu_count()" in content
