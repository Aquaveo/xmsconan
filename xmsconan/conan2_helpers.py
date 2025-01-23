import copy
import itertools
import os
import json
import sys
import platform
import subprocess

configurations = {
    'windows':{
        'cppstd': ['17'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['Visual Studio'],
        'compiler.version': ['16'],
        'compiler.runtime': ['MD', 'MT'],
    },
    'linux':{
        'cppstd': ['17'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['gcc'],
        'compiler.version': ['7'],
    },
    'macos':{
        'cppstd': ['gnu17'],
        'build_type': ['Release', 'Debug'],
        'arch': ['x86_64'],
        'compiler': ['apple-clang'],
        'compiler.version': ['14'],
    },
}


def generate_configurations(platform='windows'):
    # Get the current platform configuration
    platform_configuration = configurations.get(platform)

    # Get the cartesian product of all the configurations
    keys = platform_configuration.keys()
    values = (platform_configuration[key] for key in keys)
    all_combinations = [dict(zip(keys, combination)) for combination in itertools.product(*values)]

    # filter out combos we don't want
    combinations = []
    for combination in all_combinations:
        combinations.append(combination)

    xms_version = os.getenv('XMS_VERSION', None)
    python_target_version = os.getenv('PYTHON_TARGET_VERSION', "3.12")
    ci_commit_tag = os.environ.get('CI_COMMIT_TAG', 'False')  # Gitlab
    release_python = os.getenv('RELEASE_PYTHON', 'False')
    aquapi_username = os.getenv('AQUAPI_USERNAME', None)
    aquapi_password = os.getenv('AQUAPI_PASSWORD', None)
    aquapi_url = os.getenv('AQUAPI_URL', None)

    if ci_commit_tag != 'False':
        release_python = 'True'

    for combination in combinations:
        combination['options'] = {
            'wchar_t': 'builtin',
            'pybind': False,
            'testing': False,
        }
        combination['buildenv'] = {
            'XMS_VERSION': xms_version,
            'PYTHON_TARGET_VERSION': python_target_version,
            'CI_COMMIT_TAG': ci_commit_tag,
            'RELEASE_PYTHON': release_python,
            'AQUAPI_USERNAME': aquapi_username,
            'AQUAPI_PASSWORD': aquapi_password,
            'AQUAPI_URL': aquapi_url,
        }

    wchar_t_updated_builds = []
    for combination in combinations:
        if combination['compiler'] == 'Visual Studio':
            wchar_t_options = copy.deepcopy(combination)
            wchar_t_options['options'].update({
                'wchar_t': 'typedef',
            })
            wchar_t_updated_builds.append(wchar_t_options)

    pybind_updated_builds = []
    for combination in combinations:
        if combination['build_type'] != 'Debug' and \
           (combination['compiler'] != 'Visual Studio' or combination['compiler.runtime'] in ['MD', 'MDd']):
            if combination['compiler'] == 'Visual Studio' and int(combination['compiler.version']) <= 12:
                continue
            pybind_options = copy.deepcopy(combination)
            pybind_options['options'].update({
                'pybind': True,
            })
            pybind_updated_builds.append(pybind_options)

    testing_updated_builds = []
    for combination in combinations:
        testing_options = copy.deepcopy(combination)
        testing_options['options'].update({
            'testing': True,
        })
        testing_updated_builds.append(testing_options)

    combinations = combinations + wchar_t_updated_builds + pybind_updated_builds + testing_updated_builds

    return combinations

    
def build_packages_from_configurations(platform='windows', filter=None):

    # Get the current platform configuration
    platform_configurations = generate_configurations(platform)

    # filter out builds that don't match the filter
    # if filter:
    #     platform_configurations = [config for config in platform_configurations if filter(config)]
    print('\n')
    print_configuration_table(platform_configurations)


def pretty_print_profile(configuration):
    pass

def print_configuration_table(configurations):
    headers = ["#", "cppstd", "run_time", "build_type", "compiler", "compiler.version", "arch", "xmscore:wchar_t", "xmscore:pybind", "xmscore:testing"]
    table = []

    # Create the header row
    header_row = "| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} | {:^17} | {:^16} | {:^17} |".format(*headers)
    separator = "+-----+----------+----------+--------------+----------------+--------------------+--------+-------------------+------------------+-------------------+"

    # Add the header row and separator to the table
    table.append(separator)
    table.append(header_row)
    table.append(separator)

    # Create the data rows
    for i, config in enumerate(configurations, start=1):
        wchar_t_option = config['options'].get('wchar_t', False)
        pybind_option = config['options'].get('pybind', False)
        testing_option = config['options'].get('testing', False)
        row = "| {:^3} | {:^8} | {:^8} | {:^12} | {:^14} | {:^18} | {:^6} | {:^17} | {:^16} | {:^17} |".format(
            i,
            config.get("cppstd", ""),
            config.get("comipiler.runtime", ""),
            config.get("build_type", ""),
            config.get("compiler", ""),
            config.get("compiler.version", ""),
            config.get("arch", ""),
            wchar_t_option,
            'True' if pybind_option else 'False',
            'True' if testing_option else 'False',
        )
        table.append(row)
        table.append(separator)

    # Print the table
    for line in table:
        print(line)

    for i, config in enumerate(configurations, start=1):
        print(f'Building configuration {i} of {len(configurations)}')
        create_build_profile(config)


def create_build_profile(configuration):
    settings = {k: v for k, v in configuration.items() if k not in ['options', 'buildenv']}

    # remove temp profile if it exists
    if os.path.exists('temp_profile'):
        os.remove('temp_profile')

    with open('temp_profile', 'w') as f:
        f.write('[settings]\n')
        for k, v in settings.items():
            f.write(f'{k}={v}\n')

        f.write('\n[options]\n')
        for k, v in configuration['options'].items():
            f.write(f'{k}={v}\n')

        f.write('\n[buildenv]\n')
        for k, v in configuration['buildenv'].items():
            f.write(f'{k}={v}\n')