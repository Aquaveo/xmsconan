"""
Methods and Modules used to aid in xmsconan projects.
"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("xmsconan")
except PackageNotFoundError:
    __version__ = "0.0.0"
