"""
Build library from source.
"""
import argparse
import os
import subprocess

GENERATORS = {
    'make': None,
    'ninja': 'Ninja',
    'vs2019': 'Visual Studio 16 2019',
    'vs2022': 'Visual Studio 17 2022',
}


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
        help='location of CMakeList.txt'
    )
    arguments.add_argument(
        '--build_dir', '-b', type=str, nargs='?',
        help='location of build files'
    )
    arguments.add_argument(
        '--profile', '-p', type=str, nargs='?',
        help='profile to build'
    )
    arguments.add_argument(
        '--generator', '-g', type=str, nargs='?',
        help='files to generate. (vs2013, vs2015, or make)'
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
    parsed_args = arguments.parse_args()

    # Profiles
    precompile_profiles = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    profile_path = os.path.join(script_dir, 'profiles')
    for root, _, files in os.walk(profile_path):
        for f in files:
            precompile_profiles[f] = os.path.abspath(os.path.join(root, f))

    if not parsed_args.cmake_dir:
        parsed_args.cmake_dir = input("CMakeList.txt location [{}]:".format(
            parsed_args.cmake_dir or '.') or parsed_args.cmake_dir or '.')

    if not parsed_args.build_dir:
        parsed_args.build_dir = input("build location [{}]:".format(
            parsed_args.build_dir or '.') or parsed_args.build_dir or '.')

    if not parsed_args.profile or parsed_args.profile not in precompile_profiles.keys():
        print("Available Profiles: {}".format(', '.join(precompile_profiles.keys())))
        parsed_args.profile = input("profile [{}]:".format(
            parsed_args.profile or '.\\default') or parsed_args.profile or '.\\default')

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


def conan_install(_profile, _cmake_dir, _build_dir):
    """Install conan dependencies."""
    print("------------------------------------------------------------------")
    print(" Generating conan info")
    print("------------------------------------------------------------------")
    print(_profile)
    if not os.path.isdir(_build_dir):
        print("Creating build directory: {}".format(_build_dir))
        os.makedirs(_build_dir)

    subprocess.call([
        'conan', 'install', '-of', _build_dir,
        '-pr', _profile, _cmake_dir, '--build=missing'
    ])


def get_cmake_options(args):
    """Get cmake options."""
    print("------------------------------------------------------------------")
    print(" Setting up cmake options")
    print("------------------------------------------------------------------")
    conan_options = {
        'testing': 'False',
        'pybind': 'False',
        'wchar_t': 'False'
    }

    profile = os.path.basename(args.profile)
    print(profile)
    if 'testing' in profile.lower():
        conan_options['testing'] = 'True'

    if 'pybind' in profile.lower():
        conan_options['pybind'] = 'True'

    if 'wchar_t' in profile.lower():
        conan_options['wchar_t'] = 'True'

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
            python_target_version = input('Target Python Version [3.6]:') or "3.6"
        cmake_options.append('-DPYTHON_TARGET_VERSION={}'.format(
            python_target_version
        ))
    elif is_testing != 'False':
        test_files = args.test_files
        if not test_files:
            test_files = input('Path to test files [./test_files]:') or "./test_files"
        has_test_files = test_files not in ['NONE', '']
        if not os.path.isdir(test_files) and has_test_files:
            print("Specified path to test files does not exist! Aborting...")
            exit(1)
        else:
            test_files = os.path.abspath(test_files)

        if has_test_files:
            cmake_options.append('-DXMS_TEST_PATH={}'.format(
                test_files
            ))

    if args.xms_version:
        lib_version = args.xms_version
    else:
        lib_version = input('Library Version [99.99.99]:') or "99.99.99"
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

    print("Cmake Options:")
    for o in cmake_options:
        print("\t{}".format(o))
    return cmake_options


def run_cmake(_cmake_dir, _build_dir, _generator, _cmake_options):
    """Run cmake."""
    print("------------------------------------------------------------------")
    print(" Running cmake")
    print("------------------------------------------------------------------")
    cmd = ['cmake']
    gen = GENERATORS[_generator]
    if gen:
        cmd += ['-G', '{}'.format(gen)]
    cmd += _cmake_options
    cmd += ['-S', _cmake_dir, '-B', _build_dir]
    print(' '.join(cmd))
    subprocess.run(cmd)


def main():
    """Main function."""
    args = get_args()
    conan_install(args.profile, args.cmake_dir, args.build_dir)
    my_cmake_options = get_cmake_options(args)
    run_cmake(args.cmake_dir, args.build_dir, args.generator, my_cmake_options)


if __name__ == "__main__":
    main()
