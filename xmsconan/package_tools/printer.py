import sys
from tabulate import tabulate
from contextlib import contextmanager


class Printer(object):

    def __init__(self, printer=None):
        self.printer = printer or sys.stdout.write

    def print_ascci_art(self):

        text = """
 ____ _____    __  ____            _                      _____           _      __  
|  _ \_   _|  / / |  _ \ __ _  ___| | ____ _  __ _  ___  |_   _|__   ___ | |___  \ \ 
| |_) || |   | |  | |_) / _` |/ __| |/ / _` |/ _` |/ _ \   | |/ _ \ / _ \| / __|  | |
|  __/ | |   | |  |  __/ (_| | (__|   < (_| | (_| |  __/   | | (_) | (_) | \__ \  | |
|_|    |_|   | |  |_|   \__,_|\___|_|\_\__,_|\__, |\___|   |_|\___/ \___/|_|___/  | |
              \_\                            |___/                               /_/ 
"""
        self.printer(text)
        self.printer("\nVersion: %s" % '0.0.0\n\n')

    def print_command(self, command):
        self.print_rule(char="_")
        self.printer("\n >> %s\n" % command)
        self.print_rule(char="_")

    def print_message(self, title, body=""):
        self.printer("\n >> %s\n" % title)
        if body:
            self.printer("   >> %s\n" % body)

    def print_profile(self, profile_file):
        self.printer('==========================================================\n')
        self.printer('Profile\n')
        self.printer('==========================================================\n')
        with open(profile_file, 'r') as f:
            self.printer(f.read())
        self.printer('==========================================================\n')

    def print_dict(self, data):
        table = [("Configuration", "value")]
        for name, value in data.items():
            table.append((name, value))
        self.printer(tabulate(table, headers="firstrow", tablefmt='psql'))
        self.printer("\n")
