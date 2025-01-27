"""
Setup.py file for the xms.core python package.
"""
from setuptools import setup

from xmsconan import __version__

requires = [
    'tabulate'
]


version = __version__

setup(
    python_requires='>=3.6',
    name='xmsconan',
    version=version,
    packages=['xmsconan'],
    include_package_data=True,
    license='BSD 2-Clause License',
    description='',
    author='Gage Larsen',
    install_requires=requires,
    entry_points={
        'console_scripts': [
            'xmsconan_gen = xmsconan.generator_tools.build_file_generator:main',
        ]
    }
)
