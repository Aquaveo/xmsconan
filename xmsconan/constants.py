"""Shared constants for the xmsconan tools.

Everything here names a Conan remote or a compiler identity that more than
one module needs.  They live in one place so a remote rename or a toolchain
bump is a single edit rather than a grep-and-hope across the package.

Note: :mod:`xmsconan.xms_conan2_file` deliberately does **not** import from
this module.  ``build_file_generator.copy_xms_conan2_file()`` copies that file
next to each library's generated ``conanfile.py``, where it is imported as a
top-level ``xms_conan2_file`` module with no ``xmsconan`` package around it,
so it has to stay standalone and repeat the msvc 192 literal.
"""
import re

#: Base URL of the Aquaveo Artifactory Conan 2 repositories.  Every remote
#: URL below is this plus the repository name.
ARTIFACTORY_BASE_URL = "https://conan2.aquaveo.com/artifactory/api/conan"

#: Conan remote name for the CI-published packages (msvc 194, gcc, clang).
DEFAULT_REMOTE_NAME = "aquaveo"

#: Artifactory URL backing :data:`DEFAULT_REMOTE_NAME` (the
#: ``aquaveo-stable`` repository).
DEFAULT_REMOTE_URL = f"{ARTIFACTORY_BASE_URL}/aquaveo-stable"

#: Conan remote name for the manually built Visual Studio 2019 (msvc 192)
#: packages.  Kept separate from :data:`DEFAULT_REMOTE_NAME` so the legacy
#: binaries never mix with the CI-published ones.
VS2019_REMOTE_NAME = "aquaveo-vs2019"

#: Artifactory URL backing :data:`VS2019_REMOTE_NAME`.
VS2019_REMOTE_URL = f"{ARTIFACTORY_BASE_URL}/aquaveo-vs2019"

#: Conan ``compiler.version`` value identifying the Visual Studio 2019
#: toolset.  GitHub retired the ``windows-2019`` runner image, so packages
#: with this compiler version are built by hand and published to
#: :data:`VS2019_REMOTE_NAME`.
MSVC_VS2019_VERSION = "192"


#: Build-folder suffix for each CMake generator.  CMake refuses to reuse a
#: binary directory configured by a different generator, so a Ninja build and a
#: Visual Studio build of the same recipe need separate folders.
#:
#: :mod:`xmsconan.xms_conan2_file` repeats this mapping for the same reason it
#: repeats the msvc 192 literal (see the module note above): it is copied next
#: to each generated ``conanfile.py`` and must stay standalone.  The two copies
#: are pinned together by ``test_generator_folder_suffixes_match_recipe``.
GENERATOR_FOLDER_SUFFIXES = {
    "ninja multi-config": "ninja",
    "ninja": "ninja",
    "xcode": "xcode",
    "unix makefiles": "make",
}

#: Generators that build several configurations from one configure step.  These
#: collapse Debug/Release into build presets rather than separate configure
#: presets.
MULTI_CONFIG_GENERATOR_MARKERS = ("multi-config", "visual studio", "xcode")


def build_folder_for_generator(generator, kind=None, discriminators=()):
    """Return the build folder name for ``generator``, build ``kind`` and settings.

    Three axes, all of which must separate folders:

    * generator -- CMake refuses to reuse a binary directory configured by a
      different generator.
    * kind (``testing`` / ``python`` / ``library``) -- each installs a different
      Conan dependency set (gtest, pybind11, neither), so sharing a folder means
      the second install overwrites the first one's generated toolchain.
    * discriminators -- everything else that changes the package id, currently
      python version, ``wchar_t`` and MSVC runtime. The Windows matrix fans out
      over runtime and ``wchar_t``, so without these four library configurations
      and two testing configurations collapse onto one folder. They configure
      with the same generator, so CMake does not object the way it does on a
      generator mismatch: the second install silently overwrites the first one's
      toolchain and the build links against the wrong runtime.

    An unset generator keeps the historical ``build`` folder, which is what the
    ephemeral profile used by ``build.py`` produces.
    """
    if not generator:
        return "build"
    key = str(generator).strip().lower()
    suffix = GENERATOR_FOLDER_SUFFIXES.get(key)
    if suffix is None:
        suffix = "vs" if key.startswith("visual studio") else re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    # Underscore, not hyphen: every xms repo already ignores `build_*/`, so the
    # generated folders are covered without touching ten .gitignore files.
    parts = ["build", kind, suffix, *discriminators]
    return "_".join(str(part) for part in parts if part) or "build"


def is_multi_config_generator(generator):
    """Whether ``generator`` configures several build types at once."""
    key = str(generator or "").strip().lower()
    return any(marker in key for marker in MULTI_CONFIG_GENERATOR_MARKERS)
