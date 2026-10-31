"""pvelab scripts [NAME]"""

import sys

from ..helptext import bad_usage
from ..util import did_you_mean
from ..library import library, library_lines
from ..util import die


def cmd_scripts(args):
    """List the script library, or print one script (read-only: needs no root, takes no lock)."""
    if len(args) > 1:
        bad_usage("give at most one script name")
    lib = library()
    if not args:
        for line in library_lines():
            print(line)
        print("use: pvelab create ... --script docker   or   pvelab run VM... harden+docker")
        return
    name = args[0]
    if name not in lib:
        die(f"no library script '{name}' (the library: {', '.join(lib) or 'empty'})"
            + did_you_mean(name, list(lib)))
    sys.stdout.flush()
    sys.stdout.buffer.write(lib[name][1])
    sys.stdout.buffer.flush()
