"""pvelab's marked sections in /etc/network/interfaces.

A net made by pvelab is one block, "# lab.sh net N begin" .. "# lab.sh net N end". Inside it, each
feature that adds rules (networks.SETTERS) keeps its own section of post-up / post-down lines, so the rules come
back after a reboot and go away with the net.
"""

import re
import shlex
import subprocess

from .config import IFACES
from .util import run

def bridge_is_ours(n):
    return re.search(rf"^# lab\.sh net {n} begin", open(IFACES).read(), re.M) is not None


def add_net_block(n, body):
    """Append net n's block (its bridge stanza); feature sections go in later with write_section."""
    save_ifaces(open(IFACES).read() + f"\n# lab.sh net {n} begin (managed by lab - removed automatically with its "
                f"last VM)\n{body}# lab.sh net {n} end\n")


def remove_net_block(n):
    save_ifaces(re.sub(rf"\n?# lab\.sh net {n} begin.*?# lab\.sh net {n} end\n", "", open(IFACES).read(), flags=re.S))


def section_re(kind, n):
    return re.compile(rf"^        # {kind} net {n} begin\n.*?^        # {kind} net {n} end\n", re.M | re.S)


def has_section(kind, n):
    return section_re(kind, n).search(open(IFACES).read()) is not None


def save_ifaces(text):
    run("cp", "-a", IFACES, IFACES + ".lab-bak")
    with open(IFACES, "w") as f:
        f.write(text)


def write_section(kind, n, up, down):
    """Replace net n's <kind> section: undo the old one's rules (its post-down lines), then write and
    run the new commands. up/down: shell command lines; no up = just remove the section."""
    s = open(IFACES).read()
    old = section_re(kind, n).search(s)
    if old:
        for line in old.group(0).splitlines():
            if line.strip().startswith("post-down"):
                subprocess.run(shlex.split(line.strip()[len("post-down"):]), stderr=subprocess.DEVNULL)
        s = s.replace(old.group(0), "")
    if up:
        section = (f"        # {kind} net {n} begin\n"
                   + "".join(f"        post-up   {c}\n" for c in up)
                   + "".join(f"        post-down {c}\n" for c in down)
                   + f"        # {kind} net {n} end\n")
        s = s.replace(f"# lab.sh net {n} end\n", section + f"# lab.sh net {n} end\n")
    save_ifaces(s)
    for c in up:
        run(*shlex.split(c))
