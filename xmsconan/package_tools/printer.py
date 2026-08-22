"""The printer module."""
import sys

from tabulate import tabulate

from xmsconan.package_tools import __version__ as version


class Printer(object):
    """The printer class.

    Deliberately small: the three methods below are the whole surface
    :class:`~xmsconan.package_tools.packager.XmsConanPackager` uses.  A larger
    set of Conan-1-era helpers (docker banners, Travis-style output folding,
    job tables built from ``build.settings``/``build.options`` objects nothing
    in this codebase constructs) lived here with no caller; they were removed
    rather than carried, since a method that is never called is a method whose
    output nobody has looked at.
    """

    def __init__(self, printer=None):
        """Initialize the printer."""
        self.printer = printer or sys.stdout.write

    def print_ascci_art(self):
        """Print the ascii art."""
        text = r"""
   ____ ____ _____    __   ____                          ____            _                      _____           _      __
  / ___|  _ \_   _|  / /  / ___|___  _ __   __ _ _ __   |  _ \ __ _  ___| | ____ _  __ _  ___  |_   _|__   ___ | |___  \ \
 | |   | |_) || |   | |  | |   / _ \| '_ \ / _` | '_ \  | |_) / _` |/ __| |/ / _` |/ _` |/ _ \   | |/ _ \ / _ \| / __|  | |
 | |___|  __/ | |   | |  | |__| (_) | | | | (_| | | | | |  __/ (_| | (__|   < (_| | (_| |  __/   | | (_) | (_) | \__ \  | |
  \____|_|    |_|   | |   \____\___/|_| |_|\__,_|_| |_| |_|   \__,_|\___|_|\_\__,_|\__, |\___|   |_|\___/ \___/|_|___/  | |
                     \_\                                                           |___/                               /_/
"""
        self.printer(text)
        self.printer("\nVersion: %s" % version)

    def print_message(self, title, body=""):
        """Print a message."""
        self.printer("\n >> %s\n" % title)
        if body:
            self.printer("   >> %s\n" % body)

    def print_profile(self, text):
        """Print the profile."""
        self.printer(tabulate([[text, ]], headers=["Profile"], tablefmt='psql'))
        self.printer("\n")
