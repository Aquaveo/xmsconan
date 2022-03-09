"""
Setup.py file for the xms.core python package.
"""
from setuptools import setup

requires = []


version = '0.0.0'

setup(
    python_requires='>=3.10',
    name='xmsconan',
    version=version,
    packages=['xmsconan'],
    include_package_data=True,
    license='BSD 2-Clause License',
    description='',
    author='Gage Larsen',
    install_requires=requires,
)
