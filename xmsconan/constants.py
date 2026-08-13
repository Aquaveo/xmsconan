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
