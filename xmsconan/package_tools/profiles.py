"""Conan profiles and CMake presets: how a configuration is written down.

Split out of :mod:`xmsconan.package_tools.packager`, which still re-exports
the public names that were module-level there. Its class delegates its
profile and preset methods here.

Nothing here runs Conan or generates configurations. Every function takes
configurations, or a :func:`plan_profiles` plan of them, from its caller,
and reads nothing from the environment. Files are written only by
:func:`serialize_profile`, :func:`write_profiles` and
:func:`write_cmake_presets`, and only at the path the caller names.
"""
import json
import os
from typing import NamedTuple, Optional

from xmsconan.constants import build_folder_for_generator, is_multi_config_generator


class ProfilePlan(NamedTuple):
    """One profile that :func:`write_profiles` will write.

    A named tuple rather than a dict so every consumer spells the fields the
    same way: the dry-run path once unpacked three names from a four-key dict
    and raised on the first entry, which a plain mapping cannot make obvious.
    """

    filename: str
    configuration: dict
    conf: dict
    variant: Optional[str]


# Build-environment keys a generated profile may carry.
#
# Every profile xmsconan produces is public. The ones `write_profiles` writes
# are committed to a repository, and the ephemeral one `create_build_profile`
# hands to `conan create` is printed to the job log in full -- and conan then
# echoes whatever profile it is given under "Input profiles" at the start of
# every build. A job log outlives the temporary file and is readable by anyone
# with project access. So there is no such thing as a private [buildenv]
# entry: a secret must never reach `combination['buildenv']` at all, and
# `generate_configurations` is where credentials are kept out of it.
#
# This set is therefore not a filter applied on the way to disk. It was one,
# and a filter that guards only the committed profile leaves the printed one
# unguarded -- and fails OPEN for it, since buildenv is assembled from the
# process environment and a name nobody thought to list goes straight
# through. It is the ALLOW-list that `test_every_generated_buildenv_key_is_public`
# holds every configuration to: a new [buildenv] name fails that test until it
# is consciously admitted here, which is the moment to ask whether it belongs
# in a log.
#
# `_render_profile` refuses to write a name that is not here. That is the
# backstop, not the guard -- it fires only if a key reached a configuration in
# spite of the tests above -- and it refuses instead of filtering, so it stops
# the committed profile and the printed one together rather than quietly
# dropping an entry from one of them.
PUBLIC_BUILDENV_KEYS = frozenset({
    'XMS_VERSION',
    'PYTHON_TARGET_VERSION',
    'CI_COMMIT_TAG',
    'RELEASE_PYTHON',
    'MACOSX_DEPLOYMENT_TARGET',
    '_PYTHON_HOST_PLATFORM',
    'XMS_TEST_ARTIFACTS_DIR',
    # Added per configuration by run(), not generate_configurations, so the
    # profile conan is handed carries one name the matrix itself does not.
    'XMS_TEST_ARTIFACTS_LABEL',
})

# Default [conf] for generated profiles. Ninja Multi-Config matches what the
# hand-maintained xmsvtk profiles pin on every platform except their explicit
# Visual Studio variant. Without a generator in the profile Conan falls back to
# a platform default, which differs between machines -- the exact class of
# silent divergence these profiles exist to remove.
DEFAULT_PROFILE_CONF = {
    'tools.cmake.cmaketoolchain:generator': 'Ninja Multi-Config',
    # Disable Conan's CMakeUserPresets.json. We generate CMakePresets.json
    # ourselves with stable, readable names; Conan's file otherwise ACCUMULATES
    # an include per output folder, and because it names every preset
    # `conan-default` regardless of options, a second install makes
    # `cmake --list-presets` fail outright with "Duplicate preset".
    'tools.cmake.cmaketoolchain:user_presets': '',
}

#: Keys a ``conan_profile_variants`` entry may carry.
_VARIANT_KEYS = frozenset({'name', 'conf', 'platforms', 'kinds'})

#: Values ``kinds`` may name; the return values of :func:`configuration_kind`.
_VARIANT_KINDS = frozenset({'library', 'python', 'testing'})

# Conan `os` value -> the platform key used in profile filenames and in a
# variant's `platforms` filter. Kept in one place so the two cannot drift.
_PLATFORM_KEYS = {'Macos': 'mac_os', 'Linux': 'linux', 'Windows': 'windows'}


def resolve_profile_conf(profile_conf):
    """Return the ``[conf]`` entries every generated profile carries.

    None means :data:`DEFAULT_PROFILE_CONF`; an empty mapping omits the
    section. The result is always a copy, so neither the caller's mapping
    nor the module default is the one a plan holds.
    """
    return dict(DEFAULT_PROFILE_CONF) if profile_conf is None else dict(profile_conf)


def resolve_profile_variants(profile_variants):
    """Validate and normalize the ``conan_profile_variants`` list.

    Checked here rather than where it is used because both failure modes are
    otherwise invisible: a missing ``name`` raises a bare ``KeyError`` deep
    in :func:`plan_profiles`, and a misspelled filter -- ``platforms =
    ["macos"]`` when the accepted spelling is ``mac_os`` -- raises nothing
    at all. The variant is simply never emitted, and the omission surfaces
    much later as a missing binary.

    Raises:
        ValueError: When an entry is not a mapping, omits ``name``, carries
            an unknown key, or names a platform or kind outside the
            accepted vocabulary.
    """
    if profile_variants is None:
        return []
    if not isinstance(profile_variants, (list, tuple)):
        raise ValueError(
            f'conan_profile_variants must be a list, got {type(profile_variants).__name__}'
        )

    platforms = sorted(set(_PLATFORM_KEYS.values()))
    kinds = sorted(_VARIANT_KINDS)
    resolved = []
    for variant in profile_variants:
        if not isinstance(variant, dict):
            raise ValueError(
                f'conan_profile_variants entries must be tables, got {type(variant).__name__}'
            )
        name = variant.get('name')
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f'conan_profile_variants entry {variant!r} must have a non-empty "name"'
            )
        unknown = sorted(set(variant) - _VARIANT_KEYS)
        if unknown:
            raise ValueError(
                f'conan_profile_variants entry {name!r} has unknown key(s) '
                f'{", ".join(unknown)}. Accepted keys: {", ".join(sorted(_VARIANT_KEYS))}.'
            )
        conf = variant.get('conf')
        if conf is not None and not isinstance(conf, dict):
            raise ValueError(
                f'conan_profile_variants entry {name!r} has a non-table "conf"'
            )
        for key, accepted in (('platforms', platforms), ('kinds', kinds)):
            values = variant.get(key)
            if values is None:
                continue
            if not isinstance(values, (list, tuple)):
                raise ValueError(
                    f'conan_profile_variants entry {name!r} has a non-list "{key}"'
                )
            invalid = sorted(str(v) for v in values if v not in accepted)
            if invalid:
                raise ValueError(
                    f'conan_profile_variants entry {name!r} names unknown {key} '
                    f'{", ".join(invalid)}. Accepted values: {", ".join(accepted)}.'
                )
        resolved.append(dict(variant))
    return resolved


def serialize_profile(configuration, path, *, profile_options, skip_empty=False, conf=None):
    """Write one configuration to a Conan profile file.

    A thin writer over :func:`_render_profile`, which holds the format and
    the allow-list check. The split exists so ``--check`` can compare a
    profile it has not written against the one on disk; both kinds of
    write still go through the one renderer, so the refusal below covers
    them together.

    Rendered before the file is opened, not into it: a refused profile
    must leave nothing behind, and ``open(path, 'w')`` truncates before
    the renderer gets to raise.

    Args:
        configuration: One configuration from the build matrix.
        path: Destination file path. Named in the refusal, so the message
            says which profile was rejected even when nothing is being
            written.
        profile_options: Per-dependency option overrides, e.g.
            ``{'boost': {'shared': True}}``, each written as a
            ``pkg/*:opt=value`` line in ``[options]``. Pass the packager's
            resolved options: unless ``apply_boost_defaults`` is off, its
            constructor adds the boost defaults there, and a profile
            written without them does not match the build's. There is no
            default, so a caller chooses rather than getting an empty
            mapping by omission.
        skip_empty: Drop buildenv entries whose value is None. Without this
            an unset variable serializes as the literal string ``None``,
            which Conan would faithfully export into the build.
        conf: Mapping written as a ``[conf]`` section. None omits the
            section entirely, preserving the ephemeral profile's shape.

    Returns:
        ``path``.

    Raises:
        ValueError: A ``[buildenv]`` name is not in
            ``PUBLIC_BUILDENV_KEYS``. Nothing is written.
    """
    content = _render_profile(configuration, path, profile_options=profile_options, skip_empty=skip_empty,
                              conf=conf)
    with open(path, 'w') as f:
        f.write(content)
    return path


def _render_profile(configuration, path, *, profile_options, skip_empty=False, conf=None):
    """Render one configuration as Conan profile text.

    Single serialization path shared by the ephemeral build profile and the
    profiles written into a repository by :func:`write_profiles`. Every
    profile is public -- the committed one obviously, and the ephemeral one
    because the packager's ``create_build_profile`` prints it and conan echoes it
    under "Input profiles" -- so a ``[buildenv]`` name outside
    ``PUBLIC_BUILDENV_KEYS`` stops the write.

    It refuses rather than filters, which is the whole difference. A filter
    drops the offending entry and lets the build go on with a profile
    nobody was told had changed, and it guards whichever profile it sits
    in front of. Raising at the one path both *kinds* of profile go
    through covers them with one check, and stops the ephemeral one before
    conan can echo it. It is not atomic across a :func:`write_profiles`
    run: the check is per file, so profiles serialized before the raise
    are already on disk. None of them holds the refused name -- the file
    that would have is the one that raised.
    The keys are still kept out of ``combination['buildenv']`` in
    ``generate_configurations``; this is the backstop for that, not a
    replacement for it.

    The arguments are as for :func:`serialize_profile`.

    Returns:
        The profile text.

    Raises:
        ValueError: A ``[buildenv]`` name is not in
            ``PUBLIC_BUILDENV_KEYS``.
    """
    settings = {k: v for k, v in configuration.items() if k not in ['options', 'buildenv']}

    buildenv = configuration['buildenv']
    outside = sorted(set(buildenv) - PUBLIC_BUILDENV_KEYS)
    if outside:
        raise ValueError(
            f'Refusing to write {path}: [buildenv] names outside '
            f'PUBLIC_BUILDENV_KEYS: {outside}. Every profile xmsconan writes is '
            f'public -- the committed one, and the ephemeral one conan echoes '
            f'under "Input profiles" -- so a name that is not on the allow-list '
            f'must not reach a profile at all. Add it to PUBLIC_BUILDENV_KEYS if '
            f'it is safe to print, or keep it out of the configuration.'
        )

    lines = ['[settings]\n']
    for k, v in settings.items():
        lines.append(f'{k}={v}\n')

    lines.append('\n[options]\n')
    for k, v in configuration['options'].items():
        lines.append(f'&:{k}={v}\n')

    for dep_name, dep_opts in _profile_order(profile_options):
        for opt_name, opt_value in dep_opts.items():
            lines.append(f'{dep_name}/*:{opt_name}={opt_value}\n')

    lines.append('\n[buildenv]\n')
    for k, v in buildenv.items():
        if skip_empty and v is None:
            continue
        lines.append(f'{k}={v}\n')

    if conf:
        lines.append('\n[conf]\n')
        for k, v in conf.items():
            lines.append(f'{k}={v}\n')

    return ''.join(lines)


def platform_key(configuration):
    """Return the filename platform key for a configuration."""
    os_value = configuration.get('os')
    return _PLATFORM_KEYS.get(os_value, str(os_value or 'unknown').lower())


def configuration_kind(configuration):
    """Return 'testing', 'python' or 'library' for a configuration.

    Mirrors how the hand-maintained xmsvtk profiles are grouped, and is what
    a variant's ``kinds`` filter matches against.
    """
    options = configuration.get('options', {})
    if options.get('testing'):
        return 'testing'
    if options.get('pybind'):
        return 'python'
    return 'library'


def variant_applies(variant, configuration):
    """Whether a generator variant should be emitted for a configuration.

    An absent filter means "no restriction", so a variant with neither
    ``platforms`` nor ``kinds`` applies everywhere.
    """
    platforms = variant.get('platforms')
    if platforms and platform_key(configuration) not in platforms:
        return False
    kinds = variant.get('kinds')
    if kinds and configuration_kind(configuration) not in kinds:
        return False
    return True


def profile_name(configuration):
    """Return the file stem for a configuration, e.g. ``mac_os_testing_debug``.

    Follows the naming convention already used by the hand-maintained
    profiles in xmsvtk so generated profiles are recognizable to anyone who
    has used those.
    """
    parts = [platform_key(configuration), configuration_kind(configuration),
             str(configuration.get('build_type', '')).lower()]
    parts.extend(_discriminator_parts(configuration))
    return '_'.join(part for part in parts if part)


def _discriminator_parts(configuration):
    """Return the name parts that separate otherwise identical configurations.

    Shared by :func:`profile_name` and :func:`preset_name`, which differ
    only in their prefix and separator: two copies of this would let a
    profile and the preset that consumes it drift apart on a new setting.
    """
    options = configuration.get('options', {})
    parts = []
    if options.get('pybind') and options.get('python_version'):
        parts.append('py' + str(options['python_version']).replace('.', ''))
    if options.get('wchar_t') and options['wchar_t'] != 'builtin':
        parts.append(str(options['wchar_t']))
    if configuration.get('compiler.runtime'):
        parts.append(str(configuration['compiler.runtime']))
    return parts


def write_profiles(plan, output_dir, *, profile_options):
    """Write one Conan profile per configuration into ``output_dir``.

    These are generated artifacts, regenerated from build.toml like the
    other generated build files — not local state to be hand-edited. They
    exist so that entry points other than ``build.py`` (a bare
    ``conan install``, ``conan editable``, an IDE, a fresh worktree) resolve
    the same package ids the build does, instead of whatever
    ``conan profile detect`` happens to produce.

    Args:
        plan: What :func:`plan_profiles` returns.
        output_dir: The directory to write into; created if missing.
        profile_options: As for :func:`serialize_profile`.

    Returns:
        List of written profile paths, sorted.
    """
    os.makedirs(output_dir, exist_ok=True)
    written = []
    for entry in plan:
        path = os.path.join(output_dir, entry.filename)
        serialize_profile(entry.configuration, path, profile_options=profile_options, skip_empty=True,
                          conf=entry.conf)
        written.append(path)

    return sorted(written)


def render_profiles(plan, output_dir, *, profile_options):
    """Render every profile :func:`write_profiles` would write, without writing.

    Same plan and same renderer as the write path, so ``--check`` cannot
    report a tree as up to date that a real run would change. It does not
    share :func:`write_profiles`' loop on purpose: that one serializes
    each profile as it goes and is documented as not atomic, and folding
    the two together would quietly make a refusal leave nothing behind
    rather than leaving the profiles already written.

    Args:
        plan: What :func:`plan_profiles` returns.
        output_dir: The directory the paths are under. Nothing is read
            from it or written to it.
        profile_options: As for :func:`serialize_profile`.

    Returns:
        Mapping of profile path under *output_dir* to its text, in plan order.
    """
    rendered = {}
    for entry in plan:
        path = os.path.join(output_dir, entry.filename)
        rendered[path] = _render_profile(
            entry.configuration, path, profile_options=profile_options, skip_empty=True, conf=entry.conf,
        )
    return rendered


def plan_profiles(configurations, *, profile_conf=None, profile_variants=None):
    """Return a :class:`ProfilePlan` for every profile to write.

    The single source of truth for what :func:`write_profiles` and
    :func:`plan_cmake_presets` produce, so a dry run reports exactly what a
    real run writes rather than re-deriving the names and drifting from it.

    Args:
        configurations: The configurations to plan, e.g. what
            :func:`xmsconan.package_tools.matrix.generate_configurations`
            returns.
        profile_conf: ``[conf]`` entries for every profile. None uses
            :data:`DEFAULT_PROFILE_CONF`; an empty dict omits the section.
        profile_variants: The ``conan_profile_variants`` list, raw or as
            :func:`resolve_profile_variants` returns it; None is no variants.

    Raises:
        ValueError: When ``profile_variants`` fails
            :func:`resolve_profile_variants`.
    """
    profile_conf = resolve_profile_conf(profile_conf)
    profile_variants = resolve_profile_variants(profile_variants)

    planned = []
    used = {}
    for configuration in configurations:
        base_stem = profile_name(configuration)

        # Base rendering, plus one per generator variant that matches this
        # configuration. A variant only overlays [conf]; settings and
        # options are identical, which is what makes the pair meaningful.
        renderings = [(base_stem, profile_conf, None)]
        for variant in profile_variants:
            if not variant_applies(variant, configuration):
                continue
            merged_conf = dict(profile_conf)
            merged_conf.update(variant.get('conf') or {})
            renderings.append((f"{base_stem}_{variant['name']}", merged_conf, variant['name']))

        for stem, conf, variant_name in renderings:
            # Deterministic disambiguation: identical stems would otherwise
            # silently overwrite one another and drop configurations.
            seen = used.get(stem, 0)
            used[stem] = seen + 1
            filename = stem if seen == 0 else f'{stem}_{seen + 1}'
            planned.append(ProfilePlan(
                filename=f'{filename}.txt',
                configuration=configuration,
                conf=conf,
                variant=variant_name,
            ))

    return planned


def preset_name(configuration, variant_name=None, include_build_type=False):
    """Return the CMake preset name for a configuration.

    Deliberately shorter than :func:`profile_name`: a presets file is
    consumed on the machine it was generated for, so the platform prefix
    would be noise. Build type is omitted for multi-config generators,
    which express it as a build preset instead of a second configure step.
    """
    parts = [configuration_kind(configuration)]
    if include_build_type:
        parts.append(str(configuration.get('build_type', '')).lower())
    parts.extend(_discriminator_parts(configuration))
    if variant_name:
        parts.append(variant_name)
    return '-'.join(part for part in parts if part)


def plan_cmake_presets(plan, *, coverage=False):
    """Return the CMakePresets.json document for this repository.

    Derived from the same plan as the profiles, so a preset and the profile
    that provisions it always name the same generator and build folder --
    the pair previously had to be kept in sync by hand.

    Configurations whose profile pins no generator are skipped: without one
    there is nothing to express that Conan's own generated presets do not
    already cover.

    Args:
        plan: What :func:`plan_profiles` returns.
        coverage: Whether every configure preset sets ``XMS_COVERAGE`` to
            ``'1'`` rather than ``'0'``. Never read from ``XMS_COVERAGE``
            here; the packager's constructor does that.
    """
    configure_presets = {}
    # Build types per preset, kept beside the document rather than inside
    # it: this is bookkeeping for the loop, and a stray key in a preset is
    # serialized straight into CMakePresets.json.
    preset_build_types = {}
    build_presets = []

    for entry in plan:
        configuration = entry.configuration
        generator = (entry.conf or {}).get('tools.cmake.cmaketoolchain:generator')
        if not generator:
            continue

        multi_config = is_multi_config_generator(generator)
        build_type = str(configuration.get('build_type', 'Release'))
        # The same discriminators preset_name uses. Names and folders have
        # to agree on what makes a configuration distinct, or two presets
        # get different names and one binary directory.
        base_folder = build_folder_for_generator(
            generator,
            configuration_kind(configuration),
            _discriminator_parts(configuration),
        )
        # Conan's cmake_layout appends the build type for a single-config
        # generator and only collapses to the bare folder for multi-config
        # (conan/tools/cmake/layout.py). The preset has to name the same
        # path, or it points at a conan_toolchain.cmake that was never
        # written -- and both build types would share one binary dir.
        folder = base_folder if multi_config else f'{base_folder}/{build_type}'
        name = preset_name(configuration, entry.variant, include_build_type=not multi_config)
        options = configuration.get('options', {})

        preset = configure_presets.get(name)
        if preset is None:
            cache_variables = {
                'CMAKE_EXPORT_COMPILE_COMMANDS': 'ON',
                'BUILD_TESTING': 'ON' if options.get('testing') else 'OFF',
                'IS_PYTHON_BUILD': 'YES' if options.get('pybind') else 'NO',
                # Relative to the preset file, not to wherever cmake was
                # invoked from.
                'CMAKE_INSTALL_PREFIX': '${sourceDir}/_install',
            }
            # Raw `cmake --preset` builds never run the recipe's build(),
            # so the coverage option cannot reach them; the regenerated
            # preset carries the flag instead of the retired
            # $ENV{XMS_COVERAGE} fallback in CMakeLists.txt. Written in
            # BOTH modes: a CMake cache variable persists across
            # reconfigures, so omitting the key after a coverage run
            # would leave a previously-instrumented build dir
            # instrumented — the explicit "0" overwrites the stale entry.
            cache_variables['XMS_COVERAGE'] = '1' if coverage else '0'
            if not multi_config:
                cache_variables['CMAKE_BUILD_TYPE'] = build_type
            preset = {
                'name': name,
                'displayName': f'{name} ({generator})',
                'generator': generator,
                'binaryDir': folder,
                'toolchainFile': f'{folder}/generators/conan_toolchain.cmake',
                'cacheVariables': cache_variables,
            }
            configure_presets[name] = preset
            preset_build_types[name] = []

        if build_type not in preset_build_types[name]:
            preset_build_types[name].append(build_type)
            build_presets.append({
                'name': f'{name}-{build_type.lower()}' if multi_config else name,
                'displayName': f'{name} {build_type}',
                'configurePreset': name,
                'configuration': build_type,
            })

    ordered = []
    for name in sorted(configure_presets):
        preset = configure_presets[name]
        if is_multi_config_generator(preset['generator']):
            preset['cacheVariables']['CMAKE_CONFIGURATION_TYPES'] = ';'.join(
                sorted(preset_build_types[name]))
        ordered.append(preset)

    return {
        'version': 6,
        'configurePresets': ordered,
        'buildPresets': sorted(build_presets, key=lambda b: b['name']),
    }


def write_cmake_presets(plan, path, *, coverage=False):
    """Write CMakePresets.json to ``path``. Returns the path, or None.

    None when no configuration in ``plan`` pins a generator, so there is no
    preset to write; the arguments are as for :func:`plan_cmake_presets`.
    """
    document = plan_cmake_presets(plan, coverage=coverage)
    if not document['configurePresets']:
        return None
    with open(path, 'w') as presets_file:
        json.dump(document, presets_file, indent=2)
        presets_file.write('\n')
    return path


def _profile_order(packages: dict):
    """Yield the keys and values in a profile options dict in the order they should be written to the profile."""
    # Conan2 uses last-wins resolution, but most-specific-wins seems more reasonable.
    # Put the wildcards first so they can be overridden.
    if '*' in packages:
        yield '*', packages['*']

    # The rest are sorted for easy scanning.
    for key in sorted(packages.keys()):
        if key != '*':
            yield key, packages[key]
