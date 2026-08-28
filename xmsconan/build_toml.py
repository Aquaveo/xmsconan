"""The ``build.toml`` reader.

Every tool reads the file through this module, so they cannot disagree about
what it contains. ``~/.xmsconan.toml`` is a different file with a different
contract; see :mod:`xmsconan.ci_tools.credentials`.
"""
# 1. Standard python modules
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

# 2. Third party modules
try:
    from tomllib import loads as parse_toml_text
except ModuleNotFoundError:  # Python < 3.11
    from toml import loads as parse_toml_text


def _load_toml(toml_path):
    """Parse a TOML file.

    Args:
        toml_path: Path to the file, as ``str`` or ``Path``.

    Returns:
        The parsed document as a dict.
    """
    text = Path(toml_path).read_text(encoding="utf-8")
    return parse_toml_text(text)


_DEFAULT_COVERAGE_EXCLUDES = (r".*\.t\.h$", r".*/_package/tests/.*")


@dataclass(frozen=True)
class XmsDependency:
    """One ``xms_dependencies`` entry.

    Attributes:
        name: Conan package name, e.g. ``"xmscore"``.
        version: Conan version, e.g. ``"7.0.0"``.
        no_python: Leave this dependency out of ``_package/pyproject.toml``.
    """

    name: str
    version: str
    no_python: bool = False


@dataclass(frozen=True)
class CiTable:
    """The ``[ci]`` table with its documented defaults applied.

    Three fields keep ``None`` for "not set" rather than a default, because a
    reader needs to tell the two apart:

    * ``windows`` and ``linux``: the GitHub generator warns about *any*
      explicit setting, true or false, since it ignores both. Read the
      effective value through :attr:`windows_enabled` / :attr:`linux_enabled`.
    * ``windows_wheel_repair``: the default follows ``ci_type``, resolved by
      :func:`xmsconan.ci_options.repairs_windows_wheel`.
    * ``linux_python_versions`` / ``mac_python_versions``: the default is the
      highest entry of ``python_versions``, resolved by the CI generator.
    """

    windows: Optional[bool] = None
    linux: Optional[bool] = None
    linux_arm: bool = False
    deploy: bool = True
    coverage: bool = False
    xvfb: bool = False
    split_tests: bool = False
    windows_wheel_repair: Optional[bool] = None
    test_shards: int = 0
    docker_image: str = ""
    python_versions: list = field(default_factory=lambda: ["3.13"])
    linux_python_versions: Optional[list] = None
    mac_python_versions: Optional[list] = None

    @property
    def windows_enabled(self) -> bool:
        """Whether the Windows jobs are emitted; unset means yes."""
        return self.windows is not False

    @property
    def linux_enabled(self) -> bool:
        """Whether the Linux jobs are emitted; unset means yes."""
        return self.linux is not False


@dataclass(frozen=True)
class CoverageTable:
    """The ``[coverage]`` table with its documented defaults applied.

    ``filters`` stays ``None`` when unset because its default names the
    library (``["<library_name>/"]``); the coverage context resolves it.

    ``excludes`` does not exclude the binding directory. It used to, back when
    only the testing build was read -- and that build does not compile the
    bindings, so the exclude removed nothing that existed. Now that the pybind
    build is instrumented and merged in, excluding it would collect the
    binding layer's coverage and then discard it.
    """

    cpp_threshold: float = 0.0
    python_threshold: float = 0.0
    filters: Optional[list] = None
    excludes: list = field(default_factory=lambda: list(_DEFAULT_COVERAGE_EXCLUDES))
    python_version: Optional[str] = None


@dataclass(frozen=True)
class BuildToml:
    """A parsed ``build.toml`` with every optional key at its documented default.

    Field names are the file's top-level keys; see ``docs/USAGE.md`` §5 for
    what each one means. ``[matrix]`` and ``[filter]`` stay plain dicts: the
    packager owns their vocabulary and both are written verbatim into generated
    files.
    """

    library_name: str
    description: Optional[str] = None
    version: Optional[str] = None
    ci_type: Optional[str] = None
    library_sources: list = field(default_factory=list)
    library_headers: list = field(default_factory=list)
    testing_sources: list = field(default_factory=list)
    testing_headers: list = field(default_factory=list)
    python_library_sources: list = field(default_factory=list)
    python_library_headers: list = field(default_factory=list)
    pybind_sources: list = field(default_factory=list)
    pybind_headers: list = field(default_factory=list)
    extra_export_sources: list = field(default_factory=list)
    xms_dependencies: list = field(default_factory=list)
    xms_python_dependencies: list = field(default_factory=list)
    xms_dependency_options: dict = field(default_factory=dict)
    extra_dependencies: list = field(default_factory=list)
    extra_dependency_cmake_names: dict = field(default_factory=dict)
    vs2019_dependency_overrides: dict = field(default_factory=dict)
    testing_framework: str = "cxxtest"
    python_binding_type: str = "pybind11"
    python_namespaced_dir: Optional[str] = None
    pybind_root: bool = False
    pybind_advertises_module: bool = False
    extra_cmake_text: str = ""
    post_library_cmake_text: str = ""
    conan_profile_conf: Optional[dict] = None
    conan_profile_options: dict = field(default_factory=dict)
    conan_profile_variants: Optional[list] = None
    ci: CiTable = field(default_factory=CiTable)
    coverage: CoverageTable = field(default_factory=CoverageTable)
    filter: dict = field(default_factory=dict)
    matrix: dict = field(default_factory=dict)

    def __post_init__(self):
        """Derive ``python_namespaced_dir`` from ``library_name`` when unset."""
        if self.python_namespaced_dir is None:
            object.__setattr__(self, "python_namespaced_dir", self.library_name[3:])


#: Every top-level key a build.toml may carry -- the fields of :class:`BuildToml`.
#: A key absent from here is a typo: the readers fall back to a default, so a
#: misspelling has no symptom until a generated artifact is not what was asked
#: for. The sub-tables ``[ci]`` (_validate_ci_table), ``[matrix]``
#: (XmsConanPackager.resolve_matrix), ``[filter]`` (build_filter.load_build_filter),
#: ``conan_profile_variants`` and ``vs2019_dependency_overrides`` enforce the same
#: rule for their own keys.
_KNOWN_KEYS = frozenset(f.name for f in fields(BuildToml))


def _validate_top_level_keys(toml_data, toml_path):
    """Reject a build.toml key that no generator, template, or doc knows.

    Args:
        toml_data: The parsed build.toml.
        toml_path: Path the data came from, for the error message.

    Raises:
        ValueError: When *toml_data* carries a key outside :data:`_KNOWN_KEYS`.
    """
    unknown = sorted(set(toml_data) - _KNOWN_KEYS)
    if unknown:
        unknown_keys = ", ".join(unknown)
        accepted_keys = ", ".join(sorted(_KNOWN_KEYS))
        raise ValueError(f'{toml_path} has unknown top-level key(s) {unknown_keys}. Accepted keys: {accepted_keys}.')


_COVERAGE_KEYS = frozenset(f.name for f in fields(CoverageTable))
_XMS_DEPENDENCY_KEYS = frozenset(f.name for f in fields(XmsDependency))

#: Every key the ``[ci]`` table accepts, mapped to the type a value must have.
#: An explicit table rather than one derived from :class:`CiTable`, whose
#: annotations carry ``Optional`` and would need unwrapping; the tests check
#: that the two name the same keys.
_CI_KEY_TYPES = {
    "windows": bool,
    "linux": bool,
    "linux_arm": bool,
    "deploy": bool,
    "coverage": bool,
    "xvfb": bool,
    "split_tests": bool,
    "windows_wheel_repair": bool,
    "test_shards": int,
    "docker_image": str,
    "python_versions": list,
    "linux_python_versions": list,
    "mac_python_versions": list,
}


def _reject_unknown_keys(table: dict, accepted: frozenset, where: str) -> None:
    """Raise a ``ValueError`` naming ``where`` when ``table`` has a key outside ``accepted``."""
    unknown = sorted(set(table) - accepted)
    if unknown:
        unknown_keys = ", ".join(unknown)
        accepted_keys = ", ".join(sorted(accepted))
        raise ValueError(f'{where} has unknown key(s) {unknown_keys}. Accepted keys: {accepted_keys}.')


def _validate_ci_table(raw, toml_path) -> None:
    """Raise a ``ValueError`` naming ``toml_path`` when ``raw`` is not a valid ``[ci]`` table."""
    if not isinstance(raw, dict):
        raise ValueError(f"{toml_path}: [ci] must be a table, got {type(raw).__name__}")
    _reject_unknown_keys(raw, frozenset(_CI_KEY_TYPES), f"{toml_path}: [ci]")
    for key, value in raw.items():
        expected = _CI_KEY_TYPES[key]
        # bool is a subclass of int, so an int-typed key must not accept True.
        if expected is int and isinstance(value, bool):
            raise ValueError(f"{toml_path}: [ci].{key} must be an integer, got a boolean")
        if not isinstance(value, expected):
            raise ValueError(
                f"{toml_path}: [ci].{key} must be {expected.__name__}, got "
                f"{type(value).__name__} ({value!r})"
            )


def _ci_table(raw, toml_path) -> CiTable:
    """Validate ``raw`` as a ``[ci]`` table and return it as a :class:`CiTable`."""
    _validate_ci_table(raw, toml_path)
    return CiTable(**raw)


def _coverage_table(raw, toml_path) -> CoverageTable:
    """Validate ``raw`` as a ``[coverage]`` table and return it as a :class:`CoverageTable`."""
    if not isinstance(raw, dict):
        raise ValueError(f"{toml_path}: [coverage] must be a table, got {type(raw).__name__}")
    _reject_unknown_keys(raw, _COVERAGE_KEYS, f"{toml_path}: [coverage]")
    values = dict(raw)
    for key in ("cpp_threshold", "python_threshold"):
        if key in values:
            try:
                values[key] = float(values[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{toml_path}: [coverage].{key} must be a number, got {values[key]!r}"
                ) from exc
    if values.get("python_version") is not None:
        # An unquoted `python_version = 3.13` is a TOML float; every consumer
        # wants the "X.Y" string.
        values["python_version"] = str(values["python_version"])
    return CoverageTable(**values)


def _xms_dependency(entry, toml_path) -> XmsDependency:
    """Validate one ``xms_dependencies`` entry and return it as an :class:`XmsDependency`."""
    where = f"{toml_path}: xms_dependencies entry"
    if not isinstance(entry, dict):
        raise ValueError(f"{where} {entry!r} must be a table with name and version")
    _reject_unknown_keys(entry, _XMS_DEPENDENCY_KEYS, f"{where} {entry.get('name', entry)!r}")
    if "name" not in entry or "version" not in entry:
        raise ValueError(f"{where} {entry!r} requires name and version")
    return XmsDependency(**entry)


def _toml_to_dataclass(toml_data: dict, toml_path) -> BuildToml:
    """Convert a parsed ``build.toml`` into a :class:`BuildToml`.

    Args:
        toml_data: The parsed file, already checked by
            :func:`_validate_top_level_keys`.
        toml_path: Where the data came from, for error messages.

    Returns:
        The typed configuration with every default applied.

    Raises:
        ValueError: When ``library_name`` is missing, or a sub-table carries an
            unknown key or the wrong shape.
    """
    if not toml_data.get("library_name"):
        raise ValueError(f"{toml_path} does not define library_name")
    values = dict(toml_data)
    values["ci"] = _ci_table(values.get("ci", {}), toml_path)
    values["coverage"] = _coverage_table(values.get("coverage", {}), toml_path)
    values["xms_dependencies"] = [
        _xms_dependency(entry, toml_path) for entry in values.get("xms_dependencies", [])
    ]
    return BuildToml(**values)


def read_build_toml(toml_path) -> BuildToml:
    """Read a ``build.toml`` into a :class:`BuildToml`.

    Args:
        toml_path: Path to the file, as ``str`` or ``Path``.

    Returns:
        The typed configuration with every default applied.

    Raises:
        FileNotFoundError: The file does not exist.
        ValueError: The file does not parse, carries a key nothing knows, or is
            missing ``library_name``. Parse errors name the file.
    """
    try:
        data = _load_toml(toml_path)
    except ValueError as exc:
        raise ValueError(f"could not parse {toml_path}: {exc}") from exc
    _validate_top_level_keys(data, toml_path)
    return _toml_to_dataclass(data, toml_path)


def read_optional_build_toml(toml_path) -> Optional[BuildToml]:
    """Like :func:`read_build_toml`, but ``None`` when the file does not exist."""
    if not Path(toml_path).is_file():
        return None
    return read_build_toml(toml_path)
