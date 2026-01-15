"""The printer module."""
from contextlib import contextmanager
import sys

from tabulate import tabulate

from xmsconan.package_tools import __version__ as version


class Printer(object):
    """The printer class."""

    def __init__(self, printer=None):
        """Initialize the printer."""
        self.printer = printer or sys.stdout.write

    def print_in_docker(self, container=None):
        """Print in docker."""
        text = r"""
                    ##        .
              ## ## ##       ==
           ## ## ## ##      ===
       /*********************\___/ ===
  ~~~ {~~ ~~~~ ~~~ ~~~~ ~~ ~ /  ===- ~~~
       \______ o          __/
         \    \        __/
          \____\______/

       You are in Docker now! %s
""" % container or ""
        self.printer(text)

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

    @contextmanager
    def foldable_output(self, name):
        """Foldable output."""
        self.start_fold(name)
        yield
        sys.stderr.flush()
        sys.stdout.flush()
        self.end_fold(name)
        sys.stdout.flush()

    ACTIVE_FOLDING = False  # Not working ok because process output in wrong order

    def start_fold(self, name):
        """Start a fold."""
        self.printer("\n[%s]\n" % name)

    def end_fold(self, name):
        """End a fold."""
        pass

    def print_command(self, command):
        """Print a command."""
        self.print_rule(char="_")
        self.printer("\n >> %s\n" % command)
        self.print_rule(char="_")

    def print_message(self, title, body=""):
        """Print a message."""
        self.printer("\n >> %s\n" % title)
        if body:
            self.printer("   >> %s\n" % body)

    def print_profile(self, text):
        """Print the profile."""
        self.printer(tabulate([[text, ]], headers=["Profile"], tablefmt='psql'))
        self.printer("\n")

    def print_rule(self, char="*"):
        """Print a rule."""
        self.printer("\n")
        self.printer(char * 100)
        self.printer("\n")

    def print_current_page(self, current_page, total_pages):
        """Print the current page."""
        self.printer("Page: %s/%s" % (current_page, total_pages))
        self.printer("\n")

    def print_dict(self, data):
        """Print the dictionary in a table."""
        table = [("Configuration", "value")]
        for name, value in data.items():
            table.append((name, value))
        self.printer(tabulate(table, headers="firstrow", tablefmt='psql'))
        self.printer("\n")

    def print_jobs(self, all_jobs):
        """Print the jobs in a table."""
        initial_headers = ['#']

        compiler_headers_ext = set()
        option_headers = set()
        for build in all_jobs:
            compiler_headers_ext.update(build.settings.keys())
            option_headers.update(build.options.keys())

        compiler_headers = [it for it in compiler_headers_ext]

        table = []
        for i, build in enumerate(all_jobs):
            job_row = [str(i + 1)]
            job_row.extend([build.settings.get(it, "") for it in compiler_headers])
            job_row.extend([build.options.get(it, '') for it in option_headers])
            table.append(job_row)

        if len(table):
            self.printer(tabulate(table, headers=list(initial_headers) + list(compiler_headers) + list(option_headers),
                                  # showindex=True,
                                  tablefmt='psql'))
            self.printer("\n")
        else:
            self.printer("There are no jobs!\n")
        self.printer("\n")
