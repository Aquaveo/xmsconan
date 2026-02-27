"""
Build library from source.
"""
import argparse
import logging
import os
import shutil
import subprocess
import sys

GENERATORS = {
    'make': None,
    'ninja': 'Ninja',
    'vs2019': 'Visual Studio 16 2019',
    'vs2022': 'Visual Studio 17 2022',
    'xcode': 'Xcode',
}

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


def _parse_bool_option(value, allow_string_aliases=True):
    """Parse profile option values to a boolean-like string used by CMake flags."""
    if value is None:
        return 'False'
    normalized = str(value).strip().lower()
    true_values = {'true', '1', 'yes', 'on'}
    if allow_string_aliases:
        true_values.add('builtin')
    return 'True' if normalized in true_values else 'False'


def _parse_profile_options(profile_path, visited=None):
    """Parse Conan profile files (including include() directives) for root options."""
    if visited is None:
        visited = set()

    abs_profile = os.path.abspath(profile_path)
    if abs_profile in visited or not os.path.isfile(abs_profile):
        return {}
    visited.add(abs_profile)

    options = {}
    current_section = None
    profile_dir = os.path.dirname(abs_profile)

    with open(abs_profile, 'r', encoding='utf-8') as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue

            if line.lower().startswith('include(') and line.endswith(')'):
                include_target = line[len('include('):-1].strip()
                include_path = include_target if os.path.isabs(include_target) else os.path.join(profile_dir, include_target)
                options.update(_parse_profile_options(include_path, visited))
                continue

            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1].strip().lower()
                continue

            if current_section != 'options' or '=' not in line:
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()

            if key.startswith('&:'):
                key = key[2:]

            if '/' in key or ':' in key:
                continue

            options[key] = value

    return options


def _resolve_tool(tool_name):
    """
    Resolve a tool executable path.

    Checks in order:
    1. System PATH via shutil.which()
    2. Current Python venv Scripts directory
    3. Raises helpful error if not found

    Args:
        tool_name (str): Name of the tool (e.g., 'conan', 'cmake')

    Returns:
        str: Resolved path to the tool

    Raises:
        RuntimeError: If tool cannot be found
    """
    # Try system PATH first
    tool_path = shutil.which(tool_name)
    if tool_path:
        return tool_path

    # Try current venv Scripts directory
    scripts_dir = 'Scripts' if os.name == 'nt' else 'bin'
    exe_name = f'{tool_name}.exe' if os.name == 'nt' else tool_name
    venv_scripts = os.path.join(sys.prefix, scripts_dir, exe_name)
    
    if os.path.isfile(venv_scripts):
        return venv_scripts

    # Not found - provide helpful error
    raise RuntimeError(
        f"Tool '{tool_name}' not found. "
        f"Please ensure it's installed and available on PATH or in your Python environment.\n"
        f"Install via: pip install {tool_name} (if available) or download from official site."
    )


def is_dir(_dir_name):
    """
    Check if the given directory is actually a directory.

    Args:
        _dir_name (str): path to a directory

    Returns:
        The abs path to the given directory

    Raises:
        TypeError: If `_dir_name` is not a directory.
    """
    if not os.path.isdir(_dir_name):
        msg = "{} is not a directory".format(_dir_name)
        raise TypeError(msg)
    else:
        return os.path.abspath(_dir_name)


def is_file(_file_name):
    """
    Check if the given file is actually a file.

    Args:
        _file_name (str): path to a file

    Returns:
        The abs path to the given file

    Raises:
        TypeError: If `_file_name` is not a file.
    """
    if not os.path.isfile(_file_name):
        msg = "{} is not a file".format(_file_name)
        raise TypeError(msg)
    else:
        return os.path.abspath(_file_name)


def get_args():
    """
    Get arguments for test script.

    Returns:
        parsed used args to be used with the run_tests function

    Raises:
        TypeError: If any of the arguments are not the correct type
    """
    arguments = argparse.ArgumentParser(description="Run Conan Python tests.")
    arguments.add_argument(
        '--cmake_dir', '-c', type=str, nargs='?',
        default='.',
        help='location of CMakeList.txt'
    )
    arguments.add_argument(
        '--build_dir', '-b', type=str, nargs='?',
        default=os.path.join('.', 'builds'),
        help='location of build files'
    )
    arguments.add_argument(
        '--profile', '-p', type=str, nargs='?',
        help='profile to build'
    )
    arguments.add_argument(
        '--generator', '-g', type=str, nargs='?',
        default='vs2022' if os.name == 'nt' else 'ninja',
        help='files to generate. (ninja, vs2019, vs2022, make, xcode)'
    )
    arguments.add_argument(
        '--python_version', type=str, nargs='?',
        help='version for python'
    )
    arguments.add_argument(
        '--xms_version', '-x', type=str, nargs='?',
        help='version for xms'
    )
    arguments.add_argument(
        '--test_files', '-t', type=str, nargs='?',
        help='path to test files'
    )
    arguments.add_argument(
        '--allow-missing-test-files', action='store_true',
        help='Continue build even if test files directory is missing'
    )
    arguments.add_argument(
        '--dry-run', action='store_true',
        help='Show commands and options without executing Conan/CMake'
    )
    arguments.add_argument(
        '-v', '--verbose', action='count', default=0,
        help='Increase output verbosity (use -v for debug details)'
    )
    arguments.add_argument(
        '-q', '--quiet', action='store_true',
        help='Only show errors'
    )
    parsed_args = arguments.parse_args()

    # Profiles
    precompile_profiles = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(script_dir, 'profiles')
    for root, _, files in os.walk(profile_path):
        for f in files:
            precompile_profiles[f] = os.path.abspath(os.path.join(root, f))

    if not parsed_args.profile or parsed_args.profile not in precompile_profiles.keys():
        if parsed_args.profile and os.path.isfile(parsed_args.profile):
            parsed_args.profile = is_file(parsed_args.profile)
        else:
            available = ', '.join(sorted(precompile_profiles.keys()))
            msg = 'A valid --profile is required. Available profiles: [{}]'.format(available)
            raise TypeError(msg)

    if parsed_args.profile not in precompile_profiles.keys():
        parsed_args.profile = is_file(parsed_args.profile)
    else:
        parsed_args.profile = precompile_profiles[parsed_args.profile]

    # Generators
    if parsed_args.generator not in GENERATORS:
        msg = 'specified generator not supported "{}". ' \
              'Must be one of [{}]'.format(parsed_args.generator, ", ".join(GENERATORS.keys()))
        raise TypeError(msg)

    return parsed_args


def conan_install(_profile, _cmake_dir, _build_dir, dry_run=False):
    """Install conan dependencies."""
    LOGGER.info("------------------------------------------------------------------")
    LOGGER.info(" Generating conan info")
    LOGGER.info("------------------------------------------------------------------")
    LOGGER.info(_profile)
    if not dry_run and not os.path.isdir(_build_dir):
        LOGGER.info("Creating build directory: %s", _build_dir)
        os.makedirs(_build_dir)

    conan_exe = _resolve_tool('conan')
    cmd = [
        conan_exe, 'install', '-of', _build_dir,
        '-pr', _profile, _cmake_dir, '--build=missing'
    ]
    LOGGER.debug("Conan command: %s", ' '.join(cmd))
    return cmd


def get_cmake_options(args):
    """Get cmake options."""
    LOGGER.info("------------------------------------------------------------------")
    LOGGER.info(" Setting up cmake options")
    LOGGER.info("------------------------------------------------------------------")
    conan_options = {
        'testing': 'False',
        'pybind': 'False',
        'wchar_t': 'False'
    }

    profile = os.path.basename(args.profile)
    LOGGER.info(profile)
    profile_options = _parse_profile_options(args.profile)
    conan_options['testing'] = _parse_bool_option(profile_options.get('testing', 'False'))
    conan_options['pybind'] = _parse_bool_option(profile_options.get('pybind', 'False'))
    conan_options['wchar_t'] = _parse_bool_option(
        profile_options.get('wchar_t', 'False'),
        allow_string_aliases=False,
    )
    LOGGER.debug("Parsed profile options: %s", profile_options)

    build_type = 'Release'
    if args.profile.lower().endswith('_d'):
        build_type = 'Debug'

    cmake_options = []
    cmake_options.append('-DBUILD_TESTING={}'.format(
        conan_options.get('testing', 'False')))
    cmake_options.append('-DIS_PYTHON_BUILD={}'.format(
        conan_options.get('pybind', 'False')))
    cmake_options.append('-DXMS_BUILD={}'.format(
        conan_options.get('wchar_t', 'False')))
    cmake_options.append('-DCMAKE_INSTALL_PREFIX={}'.format(
        os.path.join(args.build_dir, "install")
    ))
    cmake_options.append('-DCMAKE_BUILD_TYPE={}'.format(build_type))

    uses_python = conan_options.get('pybind', 'False')
    is_testing = conan_options.get('testing', 'False')
    if uses_python != 'False':
        if args.python_version:
            python_target_version = args.python_version
        else:
            python_target_version = "3.13"
        cmake_options.append('-DPYTHON_TARGET_VERSION={}'.format(
            python_target_version
        ))
    elif is_testing != 'False':
        test_files = args.test_files
        if not test_files:
            test_files = "./test_files"
        has_test_files = test_files not in ['NONE', '']
        if not os.path.isdir(test_files) and has_test_files:
            if not args.allow_missing_test_files:
                raise RuntimeError(
                    f"Test files path does not exist: {test_files}\n"
                    f"Either create the directory, specify a valid path with --test_files, "
                    f"or use --allow-missing-test-files to skip this check."
                )
            else:
                LOGGER.warning("Test files not found at %s, skipping XMS_TEST_PATH", test_files)
                has_test_files = False
        elif has_test_files:
            test_files = os.path.abspath(test_files)

        if has_test_files:
            cmake_options.append('-DXMS_TEST_PATH={}'.format(
                test_files
            ))

    if args.xms_version:
        lib_version = args.xms_version
    else:
        lib_version = "99.99.99"
    cmake_options.append('-DXMS_VERSION={}'.format(lib_version))

    toolchain_path = 'build/generators/conan_toolchain.cmake',
    # if not os.name == 'nt':
    build_dir = args.build_dir if args.build_dir else "build"
    toolchain_path = f'{build_dir}/build/generators/conan_toolchain.cmake'

    # Extra toolchains
    exta_toolchains = [
        toolchain_path,
    ]
    for tc in exta_toolchains:
        cmake_options.append(f'-DCMAKE_TOOLCHAIN_FILE={tc}')

    LOGGER.info("Cmake Options:")
    for o in cmake_options:
        LOGGER.info("\t%s", o)
    return cmake_options


def run_cmake(_cmake_dir, _build_dir, _generator, _cmake_options):
    """Run cmake."""
    LOGGER.info("------------------------------------------------------------------")
    LOGGER.info(" Running cmake")
    LOGGER.info("------------------------------------------------------------------")
    cmake_exe = _resolve_tool('cmake')
    cmd = [cmake_exe]
    gen = GENERATORS[_generator]
    if gen:
        cmd += ['-G', '{}'.format(gen)]
    cmd += _cmake_options
    cmd += ['-S', _cmake_dir, '-B', _build_dir]
    LOGGER.info('%s', ' '.join(cmd))
    return cmd


def main():
    """Main function."""
    args = get_args()
    _configure_logging(args)
    conan_cmd = conan_install(args.profile, args.cmake_dir, args.build_dir, args.dry_run)
    my_cmake_options = get_cmake_options(args)
    cmake_cmd = run_cmake(args.cmake_dir, args.build_dir, args.generator, my_cmake_options)

    if args.dry_run:
        LOGGER.info("[DRY-RUN] Conan command: %s", ' '.join(conan_cmd))
        LOGGER.info("[DRY-RUN] CMake command: %s", ' '.join(cmake_cmd))
        return

    subprocess.run(conan_cmd, check=True)
    subprocess.run(cmake_cmd, check=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
