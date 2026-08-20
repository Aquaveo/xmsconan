"""Tests for generator_tools.build_file_generator."""
from pathlib import Path
import re

import pytest

from xmsconan.generator_tools import build_file_generator as build_file_generator_module
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


def test_flake8_excludes_both_sphinx_conf_locations(build_toml, tmp_path):
    """.flake8 excludes the sphinx conf.py under either docs/ or pydocs/.

    Repos differ: some use docs/, some pydocs/. Missing one meant a local
    flake8 run reported D100/E402 on a file CI skipped.
    """
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

    content = (output_dir / ".flake8").read_text(encoding="utf-8")
    assert "docs/source/conf.py" in content
    assert "pydocs/source/conf.py" in content
    # "isolated" is a CLI-only flag; inside a config file it is meaningless,
    # and CI now reads this file rather than passing --isolated.
    assert "isolated" not in content


def test_cmakelists_has_no_emscripten_branch(build_toml, tmp_path):
    """Generated CMakeLists omits a build-info file xmsconan never ships.

    emscriptenbuildinfo.cmake was referenced but never generated by xmsconan
    nor committed by any repo, so the branch could only ever fail.
    """
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("CMakeLists.txt.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "emscriptenbuildinfo.cmake" not in content
    assert "IS_EMSCRIPTEN_BUILD" not in content
    # condabuildinfo.cmake IS shipped per-repo, so it must survive.
    assert "condabuildinfo.cmake" in content


def _render_cmakelists(build_toml, tmp_path):
    """Render the real CMakeLists template for a build.toml and return its text."""
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("CMakeLists.txt.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(build_toml),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )
    return (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")


def _advertising_toml(tmp_path):
    """Write a build.toml that opts in to advertising the pybind module."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'python_namespaced_dir = "core"\n'
        'pybind_advertises_module = true\n',
        encoding="utf-8",
    )
    return toml_file


def _module_install_branch(content):
    """Return the body of the module install's ``if (WIN32)`` branch.

    Sliced to the branch rather than to end-of-file: an assertion over
    everything after the marker comment passes just as happily when the
    destinations are swapped between branches, which is the bug this block
    exists to fix.
    """
    block = content.split("# Consumable module install")[1]
    body = block.split("if (WIN32)", 1)[1]
    return body.split("endif ()", 1)[0]


def test_pybind_module_is_installed_where_a_cpp_consumer_can_find_it(tmp_path):
    """An opted-in library installs the module to bin/ and its import library to lib/.

    Installing the module only under ``_package/`` parks it where no Conan
    generator looks. The import library is installed *by name* rather than
    through ``install(TARGETS ... ARCHIVE)``: CMake tracks no ARCHIVE artifact
    for a MODULE target, so an ARCHIVE clause installs nothing and does not say
    so (verified against CMake 3.28 + MSVC v143).
    """
    branch = _module_install_branch(_render_cmakelists(_advertising_toml(tmp_path), tmp_path))

    assert 'LIBRARY DESTINATION "bin"' in branch
    assert "$<TARGET_FILE_BASE_NAME:_xmscore>.lib" in branch
    assert 'DESTINATION "lib"' in branch
    # An ARCHIVE clause here would be the silent no-op described above.
    assert "ARCHIVE" not in branch


def test_pybind_module_install_is_windows_only(tmp_path):
    """There is no non-Windows branch: the module is not linkable off Windows.

    A macOS MODULE target is a bundle, and a Linux module's SOABI suffix does
    not match the ``find_library(NAMES _<name>)`` CMakeDeps generates.
    """
    content = _render_cmakelists(_advertising_toml(tmp_path), tmp_path)
    block = content.split("# Consumable module install")[1]
    branch_head = block.split("endif ()", 1)[0]

    assert "if (WIN32)" in branch_head
    assert "else ()" not in branch_head


def test_module_install_is_absent_unless_the_library_opts_in(build_toml, tmp_path):
    """Advertising the module is per-library, so the extra install is too.

    ``pybind`` propagates down the dependency graph, so a template that
    installed the module unconditionally would change every sister library's
    package contents.
    """
    content = _render_cmakelists(build_toml, tmp_path)

    assert "# Consumable module install" not in content
    assert "TARGET_FILE_BASE_NAME" not in content
    # The wheel tree install is unconditional and must survive.
    assert 'LIBRARY DESTINATION "_package/xms/core"' in content


def test_rendered_cmakelists_has_balanced_conditionals(tmp_path):
    """Every ``if (`` in the rendered file is closed.

    A template edit that drops one branch keyword still renders, and every
    substring assertion still passes -- the file just stops parsing in CMake.
    """
    content = _render_cmakelists(_advertising_toml(tmp_path), tmp_path)

    opens = len(re.findall(r"^\s*if\s*\(", content, re.MULTILINE))
    closes = len(re.findall(r"^\s*endif\s*\(", content, re.MULTILINE))
    assert opens == closes, f"{opens} if( vs {closes} endif("


def test_pybind_module_still_installs_into_the_wheel_tree(build_toml, tmp_path):
    """The _package install must survive: it is what the wheel is built from."""
    content = _render_cmakelists(build_toml, tmp_path)
    assert 'ARCHIVE DESTINATION "_package/xms/core"' in content
    assert 'LIBRARY DESTINATION "_package/xms/core"' in content


def _write_extra_dependency_toml(tmp_path, extra_lines=""):
    """Write a build.toml declaring two extra_dependencies, plus optional extra lines."""
    toml_file = tmp_path / "build.toml"
    base = (
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'python_namespaced_dir = "core"\n'
        'extra_dependencies = ["cereal/1.3.0", "xmdf/2.0.1"]\n'
    )
    toml_file.write_text(base + extra_lines, encoding="utf-8")
    return toml_file


def test_extra_dependencies_reach_cmake(tmp_path):
    """A declared extra dependency is found and put on the compile line.

    extra_dependencies reached the Conan graph but never the CMake template, so
    a package was downloaded, CMakeDeps generated its config, and the headers
    were still missing at compile time with nothing pointing at the cause.
    """
    content = _render_cmakelists(_write_extra_dependency_toml(tmp_path), tmp_path)

    for name in ("cereal", "xmdf"):
        assert f"find_package({name} REQUIRED)" in content
        assert f"list(APPEND EXT_INCLUDE_DIRS ${{{name}_INCLUDE_DIRS}})" in content
        assert f"list(APPEND EXT_LIB_DIRS ${{{name}_LIBRARY_DIRS}})" in content
        assert f"list(APPEND EXT_LIBS ${{{name}_LIBRARIES}})" in content


def test_extra_dependency_cmake_name_override_is_used(tmp_path):
    """A Conan reference whose CMake config uses a different name is findable.

    Not every Conan package's CMake package name matches its reference name, and
    find_package is what the generated file actually calls.
    """
    content = _render_cmakelists(
        _write_extra_dependency_toml(
            tmp_path, '\n[extra_dependency_cmake_names]\nxmdf = "Xmdf"\n'
        ),
        tmp_path,
    )

    assert "find_package(Xmdf REQUIRED)" in content
    assert "list(APPEND EXT_INCLUDE_DIRS ${Xmdf_INCLUDE_DIRS})" in content
    assert "find_package(xmdf REQUIRED)" not in content
    # The untouched entry keeps its own name.
    assert "find_package(cereal REQUIRED)" in content


def test_extra_dependency_can_opt_out_of_cmake(tmp_path):
    """An empty CMake name keeps the dep in the graph but out of the CMake file.

    For a dependency that ships no CMake config, or one the template already
    finds on its own -- find_package(pybind11) inside the IS_PYTHON_BUILD block,
    for instance -- a second unconditional find_package is wrong.
    """
    content = _render_cmakelists(
        _write_extra_dependency_toml(
            tmp_path, '\n[extra_dependency_cmake_names]\nxmdf = ""\n'
        ),
        tmp_path,
    )

    assert "find_package(xmdf REQUIRED)" not in content
    assert "xmdf_INCLUDE_DIRS" not in content
    assert "find_package(cereal REQUIRED)" in content


def test_no_extra_dependencies_leaves_the_conan_block_alone(tmp_path, build_toml):
    """A library declaring none renders exactly the Boost/ZLIB block it always did.

    Named rather than counted: counting ``find_package(`` between a comment and
    the first ``endif ()`` breaks on any nested conditional and raises
    IndexError if the comment is ever reworded.
    """
    content = _render_cmakelists(build_toml, tmp_path)
    found = re.findall(r"find_package\((\w+) REQUIRED\)", content)

    assert "Boost" in found
    assert "ZLIB" in found
    # Neither the cereal/xmdf-style extras nor a lowercase duplicate of a
    # built-in should appear for a library that declared none.
    assert "zlib" not in found
    assert "boost" not in found


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


def _render_conanfile(toml_text, tmp_path):
    """Render only conanfile.py.jinja from ``toml_text`` and return its content."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(toml_text, encoding="utf-8")

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
    return (output_dir / "conanfile.py").read_text(encoding="utf-8")


def test_generates_conanfile_with_profile_options(tmp_path):
    """Generated conanfile.py defines CONAN_PROFILE_OPTIONS at module scope when set in TOML."""
    content = _render_conanfile(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        '[conan_profile_options.boost]\n'
        'wchar_t = "builtin"\n',
        tmp_path,
    )

    assert "CONAN_PROFILE_OPTIONS = {'boost': {'wchar_t': 'builtin'}}" in content


def test_generates_conanfile_without_profile_options(tmp_path):
    """Generated conanfile.py defines CONAN_PROFILE_OPTIONS = {} when TOML omits the key."""
    content = _render_conanfile(
        'library_name = "xmscore"\ndescription = "desc"\n', tmp_path
    )

    assert "CONAN_PROFILE_OPTIONS = {}" in content


def test_generates_conanfile_with_matrix_table(tmp_path):
    """A [matrix] table reaches the generated conanfile as CONAN_MATRIX.

    That constant is how build.py hands the table to the packager, so a table
    that stops here restricts nothing.
    """
    content = _render_conanfile(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        '[matrix]\n'
        'compiler_runtime = ["dynamic"]\n'
        'pybind_build_types = ["Release", "Debug"]\n',
        tmp_path,
    )

    assert "CONAN_MATRIX = {'compiler_runtime': ['dynamic']," in content
    assert "'pybind_build_types': ['Release', 'Debug']}" in content


def test_generates_conanfile_without_matrix_table(tmp_path):
    """Omitting [matrix] yields an empty table, i.e. the full fan-out."""
    content = _render_conanfile(
        'library_name = "xmscore"\ndescription = "desc"\n', tmp_path
    )

    assert "CONAN_MATRIX = {}" in content


def test_generated_build_py_passes_the_matrix_to_the_packager(tmp_path):
    """build.py must forward CONAN_MATRIX; the constant alone changes nothing."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\n', encoding="utf-8"
    )
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("build.py.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "build.py").read_text(encoding="utf-8")
    assert "matrix=conanfile.CONAN_MATRIX," in content


def test_generated_build_py_can_skip_the_dependency_libs_pass(tmp_path):
    """build.py exposes --skip-dependency-libs, which the CI passes when repair is off.

    collect_dependency_libs copies every DLL in the Conan cache -- hundreds of
    them -- purely so the repair tools can resolve imports. With repair off it is
    artifact bloat and nothing else.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "desc"\n', encoding="utf-8"
    )
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("build.py.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "build.py").read_text(encoding="utf-8")
    assert '"--skip-dependency-libs"' in content
    assert "if not args.skip_dependency_libs:" in content


def test_generates_conanfile_with_vs2019_dependency_overrides(tmp_path):
    """A [vs2019_dependency_overrides] table lands on the generated recipe subclass.

    Without the build.toml key the attribute documented on XmsConan2File is
    unreachable: `xmsconan gen` rewrites conanfile.py and re-copies
    xms_conan2_file.py on every run, so a hand-edited value never survives
    regeneration.
    """
    content = _render_conanfile(
        'library_name = "xmsgrid"\n'
        'description = "desc"\n'
        '[vs2019_dependency_overrides]\n'
        'xmscore = "xmscore/[>=6.0.1 <7.0.0]"\n',
        tmp_path,
    )

    # Exact text: the value is a version range full of brackets and spaces,
    # so a quoting slip here would produce a recipe that doesn't import.
    assert (
        "    vs2019_dependency_overrides = "
        "{'xmscore': 'xmscore/[>=6.0.1 <7.0.0]'}\n" in content
    )
    compile(content, "conanfile.py", "exec")


def test_generates_conanfile_without_vs2019_dependency_overrides(tmp_path):
    """The attribute is omitted when build.toml has no table for it.

    The recipe then inherits XmsConan2File's empty default rather than
    carrying a redundant `= {}` line, matching xms_dependency_options.
    """
    content = _render_conanfile(
        'library_name = "xmscore"\ndescription = "desc"\n', tmp_path
    )

    assert "vs2019_dependency_overrides" not in content


def test_cmake_template_emits_source_group_tree(tmp_path):
    """Generated CMakeLists.txt contains a source_group(TREE) call with unquoted variables.

    The unquoted form is required: a quoted "${var}" with an empty variable
    would expand to a literal empty-string filename and fail CMake's TREE-root
    check at configure time. This test locks in the shape so future edits
    can't silently introduce the broken form.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'python_namespaced_dir = "core"\n'
        'library_sources = []\n'
        'library_headers = []\n'
        'testing_headers = []\n'
        'pybind_sources = []\n'
        'pybind_headers = []\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("CMakeLists.txt.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")

    # The exact call must be present.
    assert "source_group(TREE ${CMAKE_CURRENT_SOURCE_DIR}" in content

    # Each variable must be unquoted to avoid the empty-string-as-filename
    # failure mode in configurations where some lists are empty.
    for var in (
        "${xmscore_sources}",
        "${xmscore_headers}",
        "${xmscore_py}",
        "${xmscore_py_headers}",
        "${test_cpp_sources}",
        "${test_headers}",
    ):
        assert var in content, f"expected {var} in source_group call"
        # Quoted form would surround the variable with double-quotes.
        assert f'"{var}"' not in content, f"{var} must not be quoted in source_group call"


@pytest.mark.parametrize("framework", ["cxxtest", "gtest"])
def test_testing_sources_are_compiled_into_the_library(framework, tmp_path):
    """testing_sources go into the library, guarded against Python builds.

    xms libraries publish testing helpers (xmscore's ``ttEqualPointsXYZ``,
    ``ttTextFilesEqual``, ...) that dependent libraries link out of the package
    they already consume, so the helpers must be in the packaged library rather
    than only in the test runner.

    The ``IS_PYTHON_BUILD`` guard keeps them out of the pybind module, whose
    link closure contains no definition of cxxtest's ``CxxTest::charToString``
    (defined only in ``cxxtest/ValueTraits.cpp``, pulled in solely by the
    cxxtestgen-generated ``runner.cpp``).

    Both directions are asserted, because losing either one is silent: the
    library loses helpers downstream libraries link, or the pybind module
    regains an undefined symbol that only shows up at dlopen time.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'python_namespaced_dir = "core"\n'
        f'testing_framework = "{framework}"\n'
        'library_sources = []\n'
        'library_headers = []\n'
        'testing_sources = ["xmscore/testing/TestTools.cpp"]\n'
        'testing_headers = []\n'
        'pybind_sources = []\n'
        'pybind_headers = []\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    _copy_template("CMakeLists.txt.jinja", tpl_dir)

    output_dir = tmp_path / "output"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(tpl_dir),
        output_dir=str(output_dir),
    )

    content = (output_dir / "CMakeLists.txt").read_text(encoding="utf-8")

    # The library picks them up for non-Python builds.
    assert "if (NOT IS_PYTHON_BUILD)" in content
    assert "list(APPEND xmscore_sources ${xmscore_testing_sources})" in content

    # The runner compiles them itself ONLY for Python builds, where the library
    # does not carry them. Compiling them unconditionally would duplicate every
    # translation unit against the library's copy.
    assert "if (IS_PYTHON_BUILD AND xmscore_testing_sources)" in content
    assert "target_sources(runner PRIVATE ${xmscore_testing_sources})" in content


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


def test_generated_build_py_exits_nonzero_on_upload_failure(build_toml, tmp_path):
    """`build.py --upload` propagates packager.upload()'s nonzero exit code.

    A failed publish to a shared remote used to exit 0 because upload()
    swallowed the error and returned None.
    """
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

    content = (output_dir / "build.py").read_text(encoding="utf-8")
    # Matched loosely: the guarantee is that upload()'s return code is compared
    # against 0 and drives exit(1), not the exact spacing of the generated line.
    assert re.search(r"if\s+builder\.upload\(version=args\.version\)\s*!=\s*0\s*:", content)
    assert "exit(1)" in content
    compile(content, "build.py", "exec")


def test_render_raises_clear_error_for_missing_toml_key(tmp_path):
    """Missing required keys produce a 'Missing field in build.toml' error message."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "x"\ndescription = "y"\n',
        encoding="utf-8",
    )

    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    (tpl_dir / "needs_var.txt.jinja").write_text(
        "{{ missing_key }}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        render_template_with_toml(
            toml_file_path=str(toml_file),
            version="1.0",
            template_dir=str(tpl_dir),
            output_dir=str(tmp_path / "out"),
        )

    msg = str(exc_info.value)
    assert "Missing field in build.toml" in msg
    assert "missing_key" in msg


# --- xms_python_dependencies ---------------------------------------------

SHIPPED_PACKAGE_TEMPLATES = (
    Path(build_file_generator_module.__file__).parent / "templates" / "_package"
)

BASE_TOML = (
    'library_name = "xmssnap"\n'
    'description = "Snap"\n'
    'python_namespaced_dir = "snap"\n'
    'xms_dependencies = [{ name = "xmscore", version = "7.0.12" }]\n'
)


def _render_pyproject(tmp_path, toml_body):
    """Render the shipped ``_package`` templates against ``toml_body``.

    Args:
        tmp_path: pytest temporary directory.
        toml_body: Contents of the ``build.toml`` to render with.

    Returns:
        The rendered ``pyproject.toml`` text.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(toml_body, encoding="utf-8")
    out = tmp_path / "out"
    render_template_with_toml(
        toml_file_path=str(toml_file),
        version="1.0.0",
        template_dir=str(SHIPPED_PACKAGE_TEMPLATES),
        output_dir=str(out),
    )
    return (out / "pyproject.toml").read_text(encoding="utf-8")


def _dependency_block(content):
    """Return the body of the rendered ``dependencies = [...]`` list.

    Args:
        content: Rendered ``pyproject.toml`` text.

    Returns:
        The text between the brackets of the dependencies list.
    """
    return content.split("dependencies = [", 1)[1].split("]", 1)[0]


def test_xms_python_dependencies_reach_the_wheel_metadata(tmp_path):
    """Non-XMS Python requirements land in the generated pyproject.toml.

    Without them the wheel installs and then fails at import: xmssnap needs
    geopandas and data_objects, neither of which is an xms_dependency.
    """
    content = _render_pyproject(
        tmp_path,
        BASE_TOML + 'xms_python_dependencies = ["geopandas", "data_objects>=4.0.0"]\n',
    )
    block = _dependency_block(content)
    assert '"numpy",' in block
    assert '"xmscore>=7.0.12",' in block
    assert '"geopandas",' in block
    assert '"data_objects>=4.0.0",' in block


def test_xms_python_dependencies_keep_declared_order(tmp_path):
    """Entries land after the xms dependencies, in the order given."""
    block = _dependency_block(
        _render_pyproject(tmp_path, BASE_TOML + 'xms_python_dependencies = ["aaa", "zzz"]\n')
    )
    assert block.index("xmscore") < block.index("aaa") < block.index("zzz")


def test_xms_python_dependencies_is_optional(tmp_path):
    """Omitting the key renders exactly what it did before the key existed."""
    block = _dependency_block(_render_pyproject(tmp_path, BASE_TOML))
    assert [line.strip() for line in block.strip().splitlines()] == [
        '"numpy",',
        '"xmscore>=7.0.12",',
    ]


def test_xms_python_dependencies_stay_out_of_the_conan_graph(tmp_path):
    """Python-only requirements never reach conanfile.py.

    The key exists to add wheel metadata; the PR claims the Conan graph is
    untouched, and nothing asserted it. If one of these leaked into the recipe
    Conan would try to resolve it as a package and the build would fail on a
    reference that only exists on PyPI.
    """
    content = _render_conanfile(
        'library_name = "xmscore"\n'
        'description = "desc"\n'
        'xms_dependencies = [{ name = "xmscore", version = "7.0.0" }]\n'
        'xms_python_dependencies = ["geopandas", "data_objects>=4.0.0"]\n',
        tmp_path,
    )

    # Positive control: an XMS dependency in the same render DOES reach the
    # recipe, so the negatives above cannot pass merely because nothing
    # rendered at all.
    assert "xmscore/7.0.0" in content
    assert "geopandas" not in content
    assert "data_objects" not in content
    assert "xms_python_dependencies" not in content


# --- profile generation is not silently degraded ---


def _minimal_build_toml(tmp_path):
    """A build.toml complete enough for a full xmsconan_gen run."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\ndescription = "Core library"\n'
        'python_namespaced_dir = "core"\nci_type = "github"\n',
        encoding="utf-8",
    )
    return toml_file


def _run_gen(tmp_path, monkeypatch, *extra_args):
    """Run xmsconan_gen's main() against a build.toml in tmp_path."""
    _minimal_build_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["xmsconan_gen", "build.toml", *extra_args])
    build_file_generator_module.main()


def test_gen_exits_nonzero_when_profile_generation_fails(tmp_path, monkeypatch):
    """A profile failure is a failure, not a warning with exit code 0.

    The broad catch this replaces turned an exception into a green run, so a
    defect in the dry-run path shipped while CI stayed green.
    """
    _minimal_build_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["xmsconan_gen", "build.toml"])
    monkeypatch.setattr(
        "xmsconan.generator_tools.profile_generator.generate_profiles",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("no library_name")),
    )

    with pytest.raises(SystemExit) as excinfo:
        build_file_generator_module.main()

    assert excinfo.value.code == 1


def test_gen_keeps_build_files_when_profile_generation_fails(tmp_path, monkeypatch):
    """The build files already written stay written, and the message says so."""
    _minimal_build_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["xmsconan_gen", "build.toml"])
    monkeypatch.setattr(
        "xmsconan.generator_tools.profile_generator.generate_profiles",
        lambda **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(SystemExit):
        build_file_generator_module.main()

    assert (tmp_path / "CMakeLists.txt").exists()
    assert (tmp_path / "conanfile.py").exists()


def test_gen_generates_profiles_by_default(tmp_path, monkeypatch):
    """Profiles and presets come out of a plain xmsconan_gen run."""
    _run_gen(tmp_path, monkeypatch)

    assert (tmp_path / "conan_profiles").is_dir()
    assert (tmp_path / "CMakePresets.json").exists()


def test_gen_no_profiles_skips_them(tmp_path, monkeypatch):
    """--no-profiles leaves both artifacts alone."""
    _run_gen(tmp_path, monkeypatch, "--no-profiles")

    assert not (tmp_path / "conan_profiles").exists()
    assert not (tmp_path / "CMakePresets.json").exists()


def test_extra_dependency_cmake_name_typo_is_rejected(tmp_path):
    """An override naming no declared package fails generation.

    Ignoring it produced a find_package that failed naming the *dependency*,
    pointing nowhere near the misspelled table key -- the same failure mode
    vs2019_dependency_overrides already refuses.
    """
    toml_file = _write_extra_dependency_toml(
        tmp_path, '\n[extra_dependency_cmake_names]\nxmdff = "Xmdf"\n'
    )
    with pytest.raises(ValueError, match="xmdff"):
        _render_cmakelists(toml_file, tmp_path)


@pytest.mark.parametrize("value", ["false", "0", "3", "true"], ids=[
    "false-bare-word", "zero", "integer", "true-bare-word",
])
def test_extra_dependency_cmake_name_must_be_a_string(tmp_path, value):
    """A non-string override is refused rather than silently acted on.

    A falsey value dropped the dependency from find_package and every EXT_*
    list, surfacing as a missing header; a truthy one rendered its Python repr
    into the CMake file as find_package(3 REQUIRED).
    """
    toml_file = _write_extra_dependency_toml(
        tmp_path, f'\n[extra_dependency_cmake_names]\nxmdf = {value}\n'
    )
    with pytest.raises(ValueError, match="must be a string"):
        _render_cmakelists(toml_file, tmp_path)


def test_extra_dependencies_are_deduped_against_the_template_builtins(tmp_path):
    """An entry the template already finds does not get a second find_package.

    Conan publishes zlib's and boost's configs as ZLIB and Boost, so the
    lowercase duplicate is a hard configure error -- and USAGE advertises the
    recipe-side dedupe as the feature that lets a library list a dep the recipe
    already supplies, so users following that advice land exactly here.
    """
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmscore"\n'
        'description = "Core library"\n'
        'python_namespaced_dir = "core"\n'
        'extra_dependencies = ["zlib/1.3.1", "boost/1.86.0", "pybind11/3.0.1", "cereal/1.3.0"]\n',
        encoding="utf-8",
    )
    content = _render_cmakelists(toml_file, tmp_path)
    found = re.findall(r"find_package\((\w+) REQUIRED\)", content)

    assert "cereal" in found
    assert "zlib" not in found
    assert "boost" not in found
    # pybind11 is found inside the IS_PYTHON_BUILD block, where Python exists;
    # a second unconditional call is not wanted.
    assert found.count("pybind11") == 1


def test_extra_dependency_duplicating_an_xms_dependency_is_not_found_twice(tmp_path):
    """The CMake side dedupes against xms_dependencies, as the recipe side does."""
    toml_file = tmp_path / "build.toml"
    toml_file.write_text(
        'library_name = "xmsgrid"\n'
        'description = "Grid"\n'
        'python_namespaced_dir = "grid"\n'
        'extra_dependencies = ["xmscore/7.1.0"]\n'
        '\n[[xms_dependencies]]\n'
        'name = "xmscore"\n'
        'version = "7.0.0"\n',
        encoding="utf-8",
    )
    content = _render_cmakelists(toml_file, tmp_path)
    found = re.findall(r"find_package\((\w+) REQUIRED\)", content)

    assert found.count("xmscore") == 1
